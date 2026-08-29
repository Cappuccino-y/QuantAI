"""risk_manager — 风控层（真源 10 个方法，design.md §4.2 risk_manager 表逐行映射）。

方法映射:
- StopOutCooldown.record/check        ← 止损冷却状态（真源 __init__ L431–432）+
                                        记录（check_stop_profit L2946–2953）+
                                        检查（execute_decision P1 L2125–2144 /
                                        check_conditional_order L5118–5137）
- DailyTradeLimiter.check/bump/restore ← _check_daily_trade_limit L823–840 /
                                        _bump_daily_trade_count L842–853 /
                                        pkl 恢复内联段 L632–643
- PositionSizer.get_max_lots          ← get_max_lots L786–802
- PositionSizer.max_lots_by_risk      ← _max_lots_by_risk L813–821
- PositionSizer.get_risk_scale        ← _get_risk_scale L855–872
- PositionSizer.apply_risk_scale      ← _apply_risk_scale L874–903
- CircuitBreaker.record_trade_result  ← _record_trade_result L1365–1417
- CircuitBreaker.load_state           ← _load_circuit_breaker_state L1419–1452
- CircuitBreaker.save_state           ← _save_circuit_breaker_state L1454–1470
- CircuitBreaker.check                ← _check_circuit_breaker L1472–1517

结构差异（ARCHITECTURE.md 阶段 4 决策记录）:
- 真源上帝类的风控状态字段（_today_entries/_consecutive_losses/_daily_loss/_today_cl/
  last_stopout_time/last_stopout_dir/emergency_mode/emergency_enter_time）按职责拆归
  四个类持有；CircuitBreaker 状态字段保持真源 hasattr 懒初始化模式（未记录前不存在）
- api/im_quote/权益访问 → account_fn/last_price_fn/equity_fn 注入
  （equity_fn 接线 AccountView.get_equity，真源 _get_equity L805–811 已在阶段 2 迁移）
- get_risk_scale 读的 _daily_loss 归 CircuitBreaker 持有 → daily_loss 属性注入
- emergency_mode 自动重置（EMERGENCY_AUTO_RESET_SEC，真源 run 主循环 L5545–5556）
  属编排层职责，阶段 5 system.run 实现；本模块仅提供状态容器
"""
import json
import logging
import os
from datetime import datetime
from typing import Callable, Optional, Tuple

from quantai.config import (CIRCUIT_BREAKER_FILE, DAILY_LOSS_WARN_RATIO,
                            MAX_RISK_PCT, MAX_ROUND_TRIPS_PER_DAY,
                            STOPOUT_COOLDOWN_SEC)


# ========== P1：止损后冷却（真源 L431–432 状态 + L2946–2953 记录 + L2125/L5118 检查） ==========

class StopOutCooldown:
    """止损平仓后 15 分钟内禁开同向新仓（STOPOUT_COOLDOWN_SEC=900）。"""

    def __init__(self, now_fn: Callable[[], datetime] = datetime.now):
        self.now_fn = now_fn
        self.last_stopout_time = datetime.min   # 真源 L431
        self.last_stopout_dir = None            # 真源 L432

    def record(self, direction: str, when: Optional[datetime] = None) -> None:
        """止损触发记录（真源 check_stop_profit L2946–2953 逐行保真）。"""
        self.last_stopout_time = when or self.now_fn()
        self.last_stopout_dir = direction
        logging.info(
            f"止损触发记录：方向={self.last_stopout_dir}, "
            f"15 分钟内禁开同向新仓"
        )

    def check(self, direction: Optional[str],
              now: Optional[datetime] = None) -> Tuple[bool, float, float]:
        """同向冷却检查（真源 execute_decision L2125–2131 / 条件单 L5119–5126 语义）。

        返回 (blocked, elapsed_seconds, remaining_seconds)。
        direction 为 None 或非同向 → 不拦截（真源 `if action_dir and
        self.last_stopout_dir == action_dir` 守卫）。
        """
        if not direction or self.last_stopout_dir != direction:
            return False, 0.0, 0.0
        now = now or self.now_fn()
        elapsed = (now - self.last_stopout_time).total_seconds()
        if elapsed < STOPOUT_COOLDOWN_SEC:
            return True, elapsed, STOPOUT_COOLDOWN_SEC - elapsed
        return False, elapsed, 0.0


# ========== 8/14：单日开仓次数上限（真源 L823–853 + pkl 恢复 L632–643） ==========

class DailyTradeLimiter:
    """单日开仓次数上限（止损→报复性再进→再止损 循环的硬截断）。"""

    def __init__(self, now_fn: Callable[[], datetime] = datetime.now):
        self.now_fn = now_fn
        # 真源懒初始化（hasattr 模式）→ 构造初始化（行为等价：首查必走跨日重置分支）
        self._today_entries = 0
        self._today_entries_date = None

    def _roll_date(self) -> None:
        """跨日重置（真源 L827–834 / L844–851 同款内联段）。"""
        today = self.now_fn().date()
        if self._today_entries_date != today:
            self._today_entries = 0
            self._today_entries_date = today

    def check(self) -> Tuple[bool, str]:
        """单日开仓次数上限（真源 _check_daily_trade_limit L823–840 逐行保真）。"""
        self._roll_date()
        if self._today_entries >= MAX_ROUND_TRIPS_PER_DAY:
            return True, (
                f"今日已开仓 {self._today_entries} 次 (≥{MAX_ROUND_TRIPS_PER_DAY})，"
                f"触发日次数上限，禁止新开仓"
            )
        return False, f"今日开仓 {self._today_entries}/{MAX_ROUND_TRIPS_PER_DAY} 次"

    def bump(self) -> None:
        """开仓成功后计数+1（真源 _bump_daily_trade_count L842–853 逐行保真）。"""
        self._roll_date()
        self._today_entries += 1
        logging.info(f"今日开仓次数: {self._today_entries}/{MAX_ROUND_TRIPS_PER_DAY}")

    def restore(self, today_entries, today_entries_date) -> None:
        """pkl 恢复当日开仓次数（真源 load_position_state 内联段 L632–643 逐行保真）。

        8/27 修复: 恢复当日开仓次数计数（M8保护: 盘中重启不绕过日次数上限）。
        """
        te = today_entries if isinstance(today_entries, int) else 0
        today = self.now_fn().date()
        try:
            ted_date = datetime.strptime(today_entries_date, '%Y-%m-%d').date() \
                if today_entries_date else None
        except ValueError:
            ted_date = None
        if ted_date == today and isinstance(te, int) and te > 0:
            self._today_entries = te
            self._today_entries_date = today
            logging.info(f"恢复当日开仓次数: {te}/{MAX_ROUND_TRIPS_PER_DAY}")


# ========== 资金手数 + 代码级风险兜底（真源 L786–903） ==========

class PositionSizer:
    """最大手数（资金面）+ 单笔风险兜底（LLM proposes, risk layer disposes）。

    注入契约:
    - account_fn(): 真源 self.api.get_account()
    - last_price_fn(): 真源 self.im_quote.last_price
    - equity_fn(): 真源 _get_equity()（阶段 2 AccountView.get_equity 同款）
    - daily_loss_fn(): 真源 self._daily_loss（CircuitBreaker 持有，未记录前 None）
    """

    def __init__(self, *, account_fn: Callable, last_price_fn: Callable,
                 equity_fn: Callable, daily_loss_fn: Callable):
        self.account_fn = account_fn
        self.last_price_fn = last_price_fn
        self.equity_fn = equity_fn
        self.daily_loss_fn = daily_loss_fn

    def get_max_lots(self) -> int:
        """资金面最大手数（真源 get_max_lots L786–802 逐行保真）。

        真源 quirk 保真: `if account is None: return 0` 之后 `else: balance = 0`
        为死分支（account 非 None 恒真），原样保留。
        """
        account = self.account_fn()
        if account is None:
            return 0
        if account:
            balance = account.balance + account.position_profit
        else:
            balance = 0
        im_price = self.last_price_fn()
        if im_price <= 0:
            return 0
        margin_rate = 0.15
        contract_multiplier = 200
        margin_per_lot = im_price * contract_multiplier * margin_rate
        max_lots = int(balance // margin_per_lot)
        max_lots_safe = int(balance * 0.6 // margin_per_lot)
        return min(max_lots, max_lots_safe)

    def max_lots_by_risk(self, sl_distance: float) -> int:
        """按单笔风险上限计算最大手数（真源 _max_lots_by_risk L813–821 逐行保真）。

        max_lots = (权益 × MAX_RISK_PCT) / (止损距离 × 200元/点)
        返回 0 表示"1 手都超风险预算"→ 调用方应拒绝开仓
        """
        equity = self.equity_fn()
        if equity <= 0 or sl_distance <= 0:
            return 0
        return int(equity * MAX_RISK_PCT / (sl_distance * 200))

    def get_risk_scale(self) -> float:
        """降档预警（真源 _get_risk_scale L855–872 逐行保真）。

        日亏达熔断阈值 60% → 仓位减半（而非全停）
        返回 1.0（正常）或 0.5（降档）
        """
        daily_loss = self.daily_loss_fn()
        if daily_loss is None:   # 真源 hasattr 懒初始化等价（未记录过交易）
            return 1.0
        if daily_loss < 0:
            equity = self.equity_fn()
            if equity > 0:
                dd_ratio = abs(daily_loss) / equity
                warn_threshold = 0.015 * DAILY_LOSS_WARN_RATIO  # 0.9%
                if dd_ratio >= warn_threshold:
                    logging.warning(
                        f"⚠️ 降档预警：日亏 {daily_loss:.0f} ({dd_ratio*100:.2f}%) "
                        f"≥ 熔断阈值 60% ({warn_threshold*100:.2f}%)，仓位减半"
                    )
                    return 0.5
        return 1.0

    def apply_risk_scale(self, volume: int, sl_distance: float) -> int:
        """综合应用风险兜底（真源 _apply_risk_scale L874–903 逐行保真）。

        volume: AI 请求手数
        sl_distance: 止损距离（期货价）
        返回 0 表示风险超限（1 手都超 1% 权益预算）→ 调用方应拒绝开仓
        """
        # 1. 按单笔风险 1% 权益兜底
        risk_lots = self.max_lots_by_risk(sl_distance)
        if risk_lots <= 0:
            equity = self.equity_fn()
            logging.warning(
                f"🚫 仓位被风险上限拒绝：止损 {sl_distance:.1f} 点 × 200元/点 = "
                f"{sl_distance*200:.0f} 元/手，超过 1% 权益 "
                f"({equity*MAX_RISK_PCT:.0f} 元)，1 手都无法承受，拒绝开仓"
            )
            return 0
        if volume > risk_lots:
            logging.warning(
                f"⚠️ 仓位被风险上限拦截：AI请求 {volume} 手，"
                f"按 1% 权益/止损 {sl_distance:.1f}点 上限为 {risk_lots} 手"
            )
            volume = risk_lots
        # 2. 降档减半（日亏接近熔断阈值时）
        scale = self.get_risk_scale()
        if scale < 1.0:
            halved = max(1, int(volume * scale))
            if halved < volume:
                logging.warning(f"⚠️ 降档减仓: {volume} 手 → {halved} 手")
                volume = halved
        return volume


# ========== 三层熔断保护（真源 L1360–1518） ==========

class CircuitBreaker:
    """熔断状态机（当日连亏 3 笔 或 日亏 > 1.5% 权益 → 禁开新仓）。

    状态字段保持真源 hasattr 懒初始化模式（_record_trade_result L1373–1378 /
    _check_circuit_breaker L1479 "无交易历史"分支依赖"未初始化"语义）。
    """

    def __init__(self, *, equity_fn: Callable,
                 now_fn: Callable[[], datetime] = datetime.now,
                 state_file: str = None):
        self.equity_fn = equity_fn
        self.now_fn = now_fn
        self.state_file = state_file or CIRCUIT_BREAKER_FILE

    # ---- 真源 _record_trade_result L1365–1417 ----
    def record_trade_result(self, pnl: float):
        """记录交易结果，更新连亏计数 + 当日累计 P&L
        - 当日连亏 (_today_cl): 只统计今天发生的连续亏损，达到阈值才触发熔断
          （8/27 方案A修复: 旧实现用跨日累计连亏+今日有过亏损就锁全天，
          导致长期连亏状态下每天亏第一笔就被锁死、单边行情全程缺席）
        - 跨日累计连亏 (_consecutive_losses): 仅供统计展示
        - 日亏: 每日重置 (新一天从 0 开始)
        """
        if not hasattr(self, '_consecutive_losses'):
            self._consecutive_losses = 0
        if not hasattr(self, '_daily_loss'):
            self._daily_loss = 0.0
        if not hasattr(self, '_daily_loss_date'):
            self._daily_loss_date = None

        today = self.now_fn().date()

        # ===== 8/27 方案A: 当日连亏追踪 =====
        if not hasattr(self, '_today_cl'):
            self._today_cl = 0
        if not hasattr(self, '_today_cl_date'):
            self._today_cl_date = today
        if self._today_cl_date != today:
            self._today_cl = 0          # 新交易日，当日连亏从 0 计数
            self._today_cl_date = today

        if self._daily_loss_date != today:
            self._daily_loss = 0.0
            self._daily_loss_date = today
            # 跨日累计连亏仅统计，不再参与熔断判断
            logging.info(f"新交易日 {today}，日亏重置为 0，历史连亏累计 {self._consecutive_losses}")

        if pnl < 0:
            self._consecutive_losses += 1   # 跨日累计（统计）
            self._today_cl += 1             # 当日连亏（熔断依据）
            self._daily_loss += pnl
        else:
            self._consecutive_losses = 0    # 盈利清零跨日累计
            self._today_cl = 0              # 盈利清零当日连亏
            if pnl > 0:
                self._daily_loss += pnl     # 盈利可能抵消日亏

        # 记录最近一次亏损发生日期（统计用途）
        if pnl < 0:
            self._consecutive_losses_date = today

        # 记录到日志
        logging.info(
            f"交易结果: 盈亏 {pnl:+.0f} | 当日连亏 {self._today_cl} | "
            f"历史连亏 {self._consecutive_losses} | 今日累计 {self._daily_loss:+.0f}"
        )
        # 修复 M8: 熔断状态持久化，重启后不丢失（防止 7/3 连亏后 7/7 重启绕过熔断）
        self.save_state()

    # ---- 真源 _load_circuit_breaker_state L1419–1452 ----
    def load_state(self):
        """恢复熔断状态（跨日累计连亏、当日连亏、当日日亏）"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    st = json.load(f)
                self._consecutive_losses = st.get('consecutive_losses', 0)
                self._daily_loss = st.get('daily_loss', 0.0)
                d = st.get('daily_loss_date')
                self._daily_loss_date = datetime.strptime(d, '%Y-%m-%d').date() if d else None
                # 8/14 修复: 记录连亏发生日期（统计用途）
                cd = st.get('consecutive_losses_date')
                self._consecutive_losses_date = (
                    datetime.strptime(cd, '%Y-%m-%d').date() if cd else None
                )
                # 8/27 方案A: 当日连亏持久化（M8保护: 同日重启不绕过熔断）
                tcl = st.get('today_consecutive_losses', 0)
                tcld = st.get('today_cl_date')
                today = self.now_fn().date()
                if tcld:
                    tcld_date = datetime.strptime(tcld, '%Y-%m-%d').date()
                    # 日期不是今天 → 当日连亏自动清零（跨日重置）
                    self._today_cl = tcl if tcld_date == today else 0
                    self._today_cl_date = tcld_date
                else:
                    self._today_cl = 0
                    self._today_cl_date = today
                logging.info(
                    f"恢复熔断状态: 当日连亏 {self._today_cl}, "
                    f"历史连亏 {self._consecutive_losses}, "
                    f"日亏 {self._daily_loss:.0f}, 日期 {self._daily_loss_date}"
                )
        except Exception as e:
            logging.warning(f"加载熔断状态失败: {e}")

    # ---- 真源 _save_circuit_breaker_state L1454–1470 ----
    def save_state(self):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'consecutive_losses': getattr(self, '_consecutive_losses', 0),
                    'daily_loss': getattr(self, '_daily_loss', 0.0),
                    'daily_loss_date': self._daily_loss_date.strftime('%Y-%m-%d')
                    if getattr(self, '_daily_loss_date', None) else None,
                    'consecutive_losses_date': getattr(self, '_consecutive_losses_date', None).strftime('%Y-%m-%d')
                    if getattr(self, '_consecutive_losses_date', None) else None,
                    # 8/27 方案A: 当日连亏持久化
                    'today_consecutive_losses': getattr(self, '_today_cl', 0),
                    'today_cl_date': getattr(self, '_today_cl_date', None).strftime('%Y-%m-%d')
                    if getattr(self, '_today_cl_date', None) else None,
                }, f, ensure_ascii=False)
        except Exception as e:
            logging.warning(f"保存熔断状态失败: {e}")

    # ---- 真源 _check_circuit_breaker L1472–1517 ----
    def check(self) -> Tuple[bool, str]:
        """
        熔断检查: 当日连亏 3 笔 或 日亏 > 1.5% 权益 → 禁开新仓
        返回 (blocked, reason)
        （8/27 方案A: 连亏拦截改为"真·当日连亏"——跨日历史连亏不再参与，
        避免长期连亏状态下每天第一笔亏损就锁全天）
        """
        if not hasattr(self, '_daily_loss'):
            return False, "无交易历史"

        # ===== 8/27 关键修复: 跨日惰性重置（防死锁）=====
        # 此前日亏/当日连亏的重置只发生在"平仓记录结果"之后，
        # 但若昨日日亏已超限熔断，今日开仓全被拦 → 永远没有平仓
        # → 昨日的 _daily_loss 永远不被清零 → 永久死锁。
        # 必须在 check 入口先按日期重置。
        _today = self.now_fn().date()
        if getattr(self, '_daily_loss_date', None) != _today:
            self._daily_loss = 0.0
            self._daily_loss_date = _today
            logging.info(f"跨日惰性重置: 日亏清零({_today})")
        if getattr(self, '_today_cl_date', None) != _today:
            self._today_cl = 0
            self._today_cl_date = _today

        # 检查当日连亏（8/27 方案A: 只统计今天发生的连续亏损，跨日清零）
        today_cl = getattr(self, '_today_cl', 0)
        if today_cl >= 3:
            return True, (
                f"今日连亏 {today_cl} 笔 (≥3)，触发熔断，"
                f"禁止当日新开仓（明日自动解除）"
            )

        # 检查日亏
        if self._daily_loss < 0:
            try:
                equity = self.equity_fn()
            except Exception:
                equity = 0
            if equity > 0 and abs(self._daily_loss) / equity >= 0.015:
                return True, (
                    f"今日累计亏 {self._daily_loss:.0f} ≥ 1.5% 权益 "
                    f"({abs(self._daily_loss)/equity*100:.2f}%)，触发日亏熔断，"
                    f"禁止当日新开仓"
                )
        return False, "熔断未触发"

    @property
    def daily_loss(self) -> Optional[float]:
        """真源 self._daily_loss 安全读取（未记录前 None，供 PositionSizer 降档判断）。"""
        return getattr(self, '_daily_loss', None)


# ========== emergency_mode 状态容器（真源 L389/L433 + L3391–3392/L3488–3489） ==========

class EmergencyState:
    """emergency_mode 标志 + 进入时间。

    设置点: order_executor.emergency_close（真源 L3391–3392）、
    rollover 开仓失败（真源 L3488–3489）；清除点: emergency_close 完成（L3401）。
    自动重置（EMERGENCY_AUTO_RESET_SEC=1800，真源 run 主循环 L5545–5556）
    属编排层职责，阶段 5 system.run 实现。
    """

    def __init__(self):
        self.mode = False                 # 真源 self.emergency_mode（L389）
        self.enter_time = None            # 真源 self.emergency_enter_time（L433）

    def activate(self, when: Optional[datetime] = None) -> None:
        """进入应急模式（真源 L3391–3392 / L3488–3489）。"""
        self.mode = True
        self.enter_time = when or datetime.now()   # P2：记录进入时间，便于自动重置

    def deactivate(self) -> None:
        """退出应急模式（真源 L3401）。"""
        self.mode = False
