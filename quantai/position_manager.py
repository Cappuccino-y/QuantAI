"""position_manager — 持仓状态 + 条件单持久化（真源 4 个方法，design.md §4.2 position_manager 表）。

方法映射:
- PositionManager.validate_position_state ← _validate_position_state L555–617（云端 reconcile）
- PositionManager.load_position_state     ← load_position_state L619–658（pkl 加载）
- PositionManager.save_position_state     ← save_position_state L659–672（pkl 保存）
- PositionManager.check_stop_profit       ← check_stop_profit L2926–2957（SL/TP 监控）

结构差异（ARCHITECTURE.md 阶段 4 决策记录）:
- 真源全局 current_position（L147–154）/ conditional_order（L157）→ PositionManager
  带锁（RLock）持有，pkl 格式保持 plain dict 兼容（design.md 设计要点 3/5）
- **pkl plain-dict 守护**（design.md §5.2 阶段 4 备忘）: load 时校验 pkl 内容为
  plain dict（防新版把 dataclass 对象写进 pkl 后，旧版 autotrade_fix.py 读新 pkl 出错），
  并兼容旧版写出的两种格式（{position, conditional_order} 包裹 / 裸 position dict）
- 当日开仓次数恢复（L632–643）→ 委托 DailyTradeLimiter.restore
- check_stop_profit 纯决策化: 返回 trigger_reason 由编排层执行平仓
  （真源直接调 close_position/emergency_close）；止损冷却记录经 on_stopout 回调
  （接线 StopOutCooldown.record）；_closing 守卫改为参数传入（编排层读
  OrderExecutor.is_closing），保持"平仓进行中不触发应急平仓"的真源语义
- last_entry_time（真源 L428，平仓绩效快照回退用）归本类持有
"""
import logging
import pickle
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Optional

from quantai.config import POSITION_FILE


def _is_plain_value(v: Any, depth: int = 0) -> bool:
    """pkl plain-dict 守护的值类型白名单（递归，限深）。

    允许: 标量（str/int/float/bool/None）+ datetime/date（entry_time 双格式）
    + list/dict 嵌套（条件单 kospi_amp 等扩展键）。dataclass/自定义对象 → False。
    """
    if depth > 4:
        return False
    if v is None or isinstance(v, (str, int, float, bool, datetime, date)):
        return True
    if isinstance(v, dict):
        return all(isinstance(k, str) and _is_plain_value(val, depth + 1)
                   for k, val in v.items())
    if isinstance(v, (list, tuple)):
        return all(_is_plain_value(item, depth + 1) for item in v)
    return False


class PositionManager:
    """持仓状态 + 条件单（真源全局状态 → 带锁管理，pkl 格式兼容）。"""

    def __init__(self, *, position_file: str = POSITION_FILE, notifier=None,
                 daily_limiter=None, now_fn: Callable[[], datetime] = datetime.now):
        self.position_file = position_file
        self.notifier = notifier                  # 启动清理/云端纠正告警
        self.daily_limiter = daily_limiter        # DailyTradeLimiter（pkl 恢复/持久化）
        self.now_fn = now_fn
        self._lock = threading.RLock()
        # 真源 L147–154 逐键一致（plain dict，pkl 兼容）
        self.position = {
            "direction": None,       # "LONG" or "SHORT"
            "volume": 0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "last_ai_decision": ""
        }
        # 真源 L157
        self.conditional_order = None   # 存储AI给出的待执行条件单，格式：dict 或 None
        # 真源 L428（平仓绩效快照 entry_time 回退用；execute_decision/条件单路径写入）
        self.last_entry_time = datetime.min

    # ---------- 真源 _validate_position_state L555–617 ----------

    def validate_position_state(self, api, symbol: str):
        """
        启动时用云端真实持仓校验本地状态，若不一致则纠正并告警
        """
        try:
            pos = api.get_position(symbol)
            # 增加等待时间到 5 秒，并多次确认，避免天勤账户状态延迟导致误判
            for _ in range(3):
                api.wait_update(deadline=time.time() + 2)
                pos = api.get_position(symbol)
                if pos is not None:
                    break

            # 获取云端真实持仓方向与手数
            if pos.volume_long > 0:
                cloud_direction = "LONG"
                cloud_volume = pos.volume_long
                cloud_entry = pos.open_price_long
            elif pos.volume_short > 0:
                cloud_direction = "SHORT"
                cloud_volume = pos.volume_short
                cloud_entry = pos.open_price_short
            else:
                cloud_direction = None
                cloud_volume = 0
                cloud_entry = 0.0

            with self._lock:
                local_dir = self.position.get('direction')
                local_vol = self.position.get('volume', 0)

                # 检查方向或手数是否一致
                inconsistent = False
                if cloud_direction != local_dir or cloud_volume != local_vol:
                    inconsistent = True
                elif cloud_direction is not None:
                    # 手数一致，但开仓均价可能有微小差异（如换月后），可考虑更新
                    if abs(cloud_entry - self.position.get('entry_price', 0)) > 0.01:
                        # 仅更新均价，不算严重不一致
                        self.position['entry_price'] = cloud_entry
                        self.save_position_state()
                        logging.info(f"开仓均价已同步为云端值: {cloud_entry:.2f}")

                if inconsistent:
                    logging.warning(
                        f"本地持仓状态与云端不一致！本地: {local_dir} {local_vol}手，云端: {cloud_direction} {cloud_volume}手")
                    # 用云端数据覆盖本地状态
                    self.position['direction'] = cloud_direction
                    self.position['volume'] = cloud_volume
                    self.position['entry_price'] = cloud_entry
                    # 注意：止损止盈云端没有，保留原值不变（若云端空仓则清零）
                    if cloud_direction is None:
                        self.position['stop_loss'] = 0.0
                        self.position['take_profit'] = 0.0
                    self.save_position_state()
                    if self.notifier is not None:
                        self.notifier.send(
                            f"⚠️ 持仓状态已用云端数据纠正：{cloud_direction} {cloud_volume}手"
                        )
                    logging.info("本地状态已纠正并与云端同步")
                else:
                    logging.info("本地持仓状态与云端一致，校验通过")

        except Exception as e:
            logging.error(f"校验持仓状态失败: {e}，继续使用本地状态")

    # ---------- 真源 load_position_state L619–658 + plain-dict 守护 ----------

    def load_position_state(self):
        with self._lock:
            import os
            if os.path.exists(self.position_file):
                try:
                    with open(self.position_file, 'rb') as f:
                        state = pickle.load(f)

                    # ========== pkl plain-dict 守护（design.md §5.2 阶段 4 备忘）==========
                    # 防新版代码把 dataclass 对象直接写进 pkl 后，旧版 autotrade_fix.py
                    # 读新 pkl 出错；同时兼容旧版写出的两种格式
                    if not isinstance(state, dict) or not _is_plain_value(state):
                        logging.error(
                            "pkl 内容非 plain dict（可能由不兼容版本写出），"
                            "拒绝加载以保护旧版可读性，使用默认空仓状态"
                        )
                        return
                    # =====================================================================

                    # 兼容旧版只有持仓的情况
                    if isinstance(state, dict) and 'position' in state:
                        if not isinstance(state['position'], dict) \
                                or not _is_plain_value(state['position']):
                            logging.error(
                                "pkl position 字段非 plain dict，拒绝加载，使用默认空仓状态"
                            )
                            return
                        self.position.update(state['position'])
                        self.conditional_order = state.get('conditional_order', None)
                    else:
                        self.position.update(state)
                        self.conditional_order = None
                    # 条件单同样守护（plain dict 或 None）
                    if self.conditional_order is not None \
                            and (not isinstance(self.conditional_order, dict)
                                 or not _is_plain_value(self.conditional_order)):
                        logging.error("pkl conditional_order 非 plain dict，置为 None")
                        self.conditional_order = None
                    # 8/27 修复: 恢复当日开仓次数计数（M8保护: 盘中重启不绕过日次数上限）
                    if self.daily_limiter is not None:
                        te = state.get('today_entries', 0) if isinstance(state, dict) else 0
                        ted = state.get('today_entries_date') if isinstance(state, dict) else None
                        self.daily_limiter.restore(te, ted)
                    # 8/27 修复: 启动时清掉过期条件单（隔夜/周末残留不入市）
                    if self.conditional_order and isinstance(self.conditional_order, dict):
                        cd = self.conditional_order.get('created_date')
                        if cd and cd != self.now_fn().date().isoformat():
                            logging.warning(
                                f"⚠️ 启动清理: 过期条件单(创建于 {cd})已移除，"
                                f"{self.conditional_order.get('trigger_type')}@{self.conditional_order.get('trigger_price')}"
                            )
                            if self.notifier is not None:
                                self.notifier.send(
                                    f"⏰ 启动时清理过期条件单（创建于 {cd}）"
                                )
                            self.conditional_order = None
                    logging.info(f"加载持仓状态: {self.position}, 条件单: {self.conditional_order}")
                except Exception as e:
                    logging.error(f"加载状态失败: {e}")

    # ---------- 真源 save_position_state L659–672 ----------

    def save_position_state(self):
        """保存持仓状态和条件单到文件"""
        with self._lock:
            state = {
                "position": self.position,
                "conditional_order": self.conditional_order,
                # 8/27: 当日开仓次数持久化
                "today_entries": self.daily_limiter._today_entries
                if self.daily_limiter is not None else 0,
                "today_entries_date": self.now_fn().date().strftime('%Y-%m-%d')
            }
            try:
                with open(self.position_file, 'wb') as f:
                    pickle.dump(state, f)
            except Exception as e:
                logging.error(f"保存状态失败: {e}")

    # ---------- 真源 check_stop_profit L2926–2957（纯决策化） ----------

    def check_stop_profit(self, position: dict, last_price: float,
                          closing: bool = False,
                          on_stopout: Optional[Callable[[str], None]] = None) -> Optional[str]:
        """SL/TP 监控（真源 check_stop_profit L2926–2957）。

        返回 trigger_reason（"止损触发"/"止盈触发"）或 None；
        平仓执行（close_position + 失败转 emergency_close）由编排层完成。
        closing: 编排层传入 OrderExecutor.is_closing()（真源 self._closing 守卫，
        防止平仓进行中重复触发/误入应急模式）。
        on_stopout: 止损触发记录回调（接线 StopOutCooldown.record，真源 L2946–2953）。
        """
        if closing or not position['direction']:
            return None
        if last_price <= 0:
            return None

        trigger_reason = None
        if position['direction'] == 'LONG':
            if last_price <= position['stop_loss']:
                trigger_reason = "止损触发"
            elif last_price >= position['take_profit']:
                trigger_reason = "止盈触发"
        elif position['direction'] == 'SHORT':
            if last_price >= position['stop_loss']:
                trigger_reason = "止损触发"
            elif last_price <= position['take_profit']:
                trigger_reason = "止盈触发"

        if trigger_reason:
            # P1：记录止损冷却时间（同向禁开 15 分钟）
            if trigger_reason == "止损触发" and on_stopout is not None:
                on_stopout(position['direction'])
        return trigger_reason
