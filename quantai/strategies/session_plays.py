"""strategies.session_plays — 时段策略（真源 L3520–3683 + L3809–4421，9 个方法）。

方法映射（design.md §4.2 session_plays 表）:
- SessionPlaysService.evaluate_overnight_holding  ← _evaluate_overnight_holding L3520–3646
- SessionPlaysService.check_tail_session          ← _check_tail_session L3659–3667
- SessionPlaysService.morning_pre_open_analysis   ← _morning_pre_open_analysis L3809–3858
- SessionPlaysService.check_overnight_gap_risk    ← _check_overnight_gap_risk L3860–3974
- SessionPlaysService.check_overnight_reversal_risk ← _check_overnight_reversal_risk L3976–4046
- SessionPlaysService.lunch_breakout_preview      ← _lunch_breakout_preview L4048–4099
- SessionPlaysService.lunch_breakout_check        ← _lunch_breakout_check L4101–4272
- SessionPlaysService.lunch_force_close_check     ← _lunch_force_close_check L4298–4312
- SessionPlaysService.post_open_analysis          ← _post_open_analysis L4314–4412

纯决策原则（design.md §5.4 / §4.2 表注）: strategies 不直接调 order_executor /
不改全局状态——真源的下单/清仓/条件单写入改为返回 SessionAction 建议或条件单 dict，
由编排层（阶段 4/5）执行。AI 调用经 ai_chat_fn 注入（阶段 4 接 vendor llm_client）。

行为保持: 全部阈值（跳空冲突 ±2%、平仓阈值 3000 元、KOSPI 反转 -3%/-5%/4%、
午盘预览 0.5%/0.3%、顺势单 1.0%/0.5%、SL 0.2×ATR/TP 0.7×ATR、节流 300s/5min、
尾盘 14:45-15:00、隔夜评估 14:55/14:30）与全部日志/钉钉文案逐行对齐真源，
含 6/12、6/15、6/16、6/17 案例注释与 C2/C3 修复注释。

结构差异（ARCHITECTURE.md 阶段 3 决策记录）:
- current_position 全局 dict → 方法参数 position（pkl 兼容 plain dict，阶段 4 PositionManager 提供）
- conditional_order 全局写入 → lunch_breakout_check 返回条件单 dict（键集 = 真源 L4238–4250）
- close_position/execute_order_safe 调用 → SessionAction 返回（编排层执行 + 发送成交结果通知）
- save_position_state → 编排层在应用建议后持久化
- AI_CLIENT 全局 → ai_chat_fn 注入；notifycation → notifier.send
- 真源 L4274–4296（lunch_breakout_check 末尾）为 return 后不可达代码（引用未定义
  avg_price），未迁移
- 真源 quirk 保真: lunch_breakout_today['force_close_deadline'] 在真源活代码中从未
  赋值（唯一赋值点 L4287 在不可达块内）→ 14:00 强平实际永不触发；本版保真保留，
  编排层若要激活须显式设置 deadline（ARCHITECTURE.md 阶段 5 备忘）
- 真源 quirk 保真（basis 单位放大，阶段 3 验收 minor1）: check_overnight_gap_risk 的
  估算公式 `expected_futures_open = index_price + basis / 100 * index_price`（真源 L3919）
  中 basis 是点值（get_basis_info()['basis'] = im_price - index_price）却被当百分比代入
  → 放大 index_price/100 ≈ 40-50 倍；后果: LONG+跳空冲突 → 预期亏损恒 ≥ 3000 元
  （平仓阈值触发面放大），SHORT+跳空冲突 → 预期亏损为负（主动平仓永不触发）。
  本版逐行保真该公式，修复方案留编排期与用户确认（ARCHITECTURE.md 阶段 5 备忘 item 6）
- 真源 L3584 pos = api.get_position(...) 赋值后未使用 → 保真保留（经 mds.api）
- 真源 L3571 check_overnight_reversal_risk 守卫实际恒 False（该方法永远返回 False）
  → 保真保留调用
- _lunch_preview_sent_date / _overnight_eval_last_log 懒初始化 → 构造初始化（行为等价）
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any, Callable, Dict, List, Optional, Tuple

from quantai.jp_indices import create_default_lunch_context, refresh_lunch_context
from quantai.models import LunchContext


@dataclass
class SessionAction:
    """时段策略产出的动作建议（strategies 纯决策，编排层执行）。

    action:
      CLOSE_POSITION — 盘前跳空主动平仓（真源 L3940–3974 的下单+清仓+持久化部分）
      FORCE_CLOSE    — 14:00 强制平仓（真源 L4308–4311 的 close_position 部分）
    """
    action: str
    reason: str = ""
    close_direction: str = ""      # CLOSE_POSITION: "BUY"/"SELL"
    volume: int = 0
    expected_loss: float = 0.0


class SessionPlaysService:
    """时段策略服务（早盘前/盘中节点/午盘顺势/尾盘/隔夜评估）。

    持有真源上帝类的时段策略状态字段:
    - lunch_context（真源 L438–447，可注入共享实例）
    - lunch_breakout_today（真源 L449–454）
    - _lunch_preview_sent_date（真源 L4056 懒初始化）
    - _overnight_eval_last_log（真源 L3526–3527 懒初始化）
    """

    def __init__(self, *, jp_service, mds, mcs, notifier=None,
                 ai_chat_fn: Optional[Callable] = None, logger=None,
                 news_items_fn: Optional[Callable[[], List[dict]]] = None,
                 lunch_context: Optional[LunchContext] = None,
                 now_fn: Callable[[], datetime] = datetime.now):
        self.jp = jp_service                      # JPIndicesService（阶段 2）
        self.mds = mds                            # MarketDataService（阶段 2）
        self.mcs = mcs                            # MarketContextService（阶段 3，atr_15/calculate_fut_atr）
        self.notifier = notifier                  # DingTalkNotifier（None → 构造默认 vendor sender）
        self.ai_chat_fn = ai_chat_fn              # messages=[...] → str（阶段 4 接 llm_client）
        self.logger = logger                      # TradeLogger（ADJUST_*/FAILED 落 CSV）
        self.news_items_fn = news_items_fn or (lambda: [])  # NewsManager.get_news 注入
        self.lunch_context = lunch_context or create_default_lunch_context()
        self.now_fn = now_fn
        # 真源 L449–454
        self.lunch_breakout_today = {
            'triggered': False,    # 当日是否已触发
            'direction': None,     # 触发方向 LONG/SHORT
            'entry_price': None,   # 入场价
            'force_close_deadline': None,  # 强制平仓时间 (14:00)
        }
        self._lunch_preview_sent_date = None   # 真源 L4056 懒初始化 → 构造初始化
        self._overnight_eval_last_log = 0      # 真源 L3526–3527 懒初始化 → 构造初始化

    def _send(self, msg: str) -> None:
        """钉钉发送（真源 notifycation.send_dingtalk_message → 注入 notifier）。"""
        if self.notifier is not None:
            self.notifier.send(msg)

    def _news_text(self) -> str:
        """新闻摘要文本（真源 L3577–3581 / L4357–4361 同款构建，快照经 news_items_fn）。"""
        news_items = self.news_items_fn()
        return "\n".join([
            f"- {item.get('time', '未知时间')}: {item.get('data', {}).get('content', '无内容')}"
            for item in news_items
        ]) if news_items else "（无重要快讯）"

    # ========== 尾盘禁开（真源 _check_tail_session L3659–3667） ==========

    def check_tail_session(self, now: Optional[dt_time] = None) -> Tuple[bool, str]:
        """尾盘禁开新仓硬拦截（8/14 新增）。
        BigQuant 中证1000 实证：14:45-15:00 滑点大、噪声重
        14:45 后禁止一切新开仓（含条件单），但允许调整已有持仓
        """
        now = now or self.now_fn().time()
        if dt_time(14, 45) <= now <= dt_time(15, 0):
            return True, "尾盘时段（14:45-15:00）滑点大，禁止新开仓（仅允许调整已有持仓）"
        return False, ""

    # ========== 早盘前分析（真源 _morning_pre_open_analysis L3809–3858） ==========

    def morning_pre_open_analysis(self, position: dict,
                                  now: Optional[datetime] = None) -> List[SessionAction]:
        """
        早盘前分析（9:00 和 9:25:30 各调用一次）。
        输入：日经 N225 早盘 + Topix 早盘 + 隔夜新闻 + 昨日收盘。
        输出：写入 lunch_context['index_call_auction']，并作为 9:30 开盘后
              AI 决策的"市场氛围"参考。
        重要：检测隔夜跳空风险，必要时调整/平仓现有持仓。
        返回: 跳空风控产出的动作建议列表（编排层执行；真源直接市价平仓）。
        """
        now = now or self.now_fn()
        is_auction_time = now.time() >= dt_time(9, 25, 30)
        actions: List[SessionAction] = []

        # 1. 拉日韩
        jp = self.jp.fetch_jp_indices()
        if jp:
            refresh_lunch_context(self.lunch_context, 'nk225_9am_pct', jp['nk225_pct'])
            # 修复：原变量名 topix_9am_pct 是错误的，实际存的是 kospi_pct
            # 原因：fetch_jp_indices 只取 ^N225 和 ^KS11，没有 Topix 数据
            refresh_lunch_context(self.lunch_context, 'kospi_9am_pct', jp.get('kospi_pct'))
        else:
            logging.warning("日韩数据未取到，跳过 9:00 节点")

        # 2. 拉集合竞价指数（仅 9:25:30 之后才取，避免拿到旧的 9:00 5min 收盘价）
        if is_auction_time:
            try:
                self.mds.update_index_price()
                refresh_lunch_context(self.lunch_context, 'index_call_auction', self.mds.index_price)
            except Exception as e:
                logging.error(f"集合竞价指数拉取失败: {e}")
        else:
            logging.info("9:25:30 前不拉集合竞价指数（避免拿到 9:00 旧数据）")

        # 3. ========== 盘前跳空风控检查 ==========
        #    6/12 案例: KOSPI 09:00 +8.34% 跳空，SHORT 8103.8 止损 8140.8 (37点)
        #    实际开盘跳空 146 点直接穿止损 -29240 元
        #    修复: 9:00 早盘前 + 9:25:30 集合竞价时 各检查一次
        #    如果 KOSPI 涨跌幅方向 跟持仓方向相反 且 |涨跌幅| >= 2% → 钉钉告警
        #    如果有 SHORT 持仓 + KOSPI 涨 >= 2% → 调整止损到 9:25:30 集合竞价 ± 10点
        # ==============================================
        if jp and position.get('direction') and position.get('volume', 0) > 0:
            action = self.check_overnight_gap_risk(jp, is_auction_time, position)
            if action is not None:
                actions.append(action)

        # 4. 钉钉通知
        msg_parts = ["📊 早盘前市场氛围"]
        if jp:
            msg_parts.append(f"日经: {jp.get('nk225_pct', 0):+.2f}%, KOSPI: {jp.get('kospi_pct', 0):+.2f}%")
        if is_auction_time and self.mds.index_price > 0:
            msg_parts.append(f"集合竞价指数: {self.mds.index_price:.2f}")
        msg = " | ".join(msg_parts)
        logging.info(msg)
        self._send(msg)
        return actions

    # ========== 盘前跳空风控（真源 _check_overnight_gap_risk L3860–3974） ==========

    def check_overnight_gap_risk(self, jp: dict, is_auction_time: bool,
                                 position: dict) -> Optional[SessionAction]:
        """
        盘前跳空风控检查。
        - KOSPI 涨跌幅方向 跟持仓方向相反 且 |涨跌幅| >= 2% → 高风险告警
        - 集合竞价时 (is_auction_time) 估算预期亏损 ≥ 3000 元 → 返回主动平仓建议
          （真源直接市价平仓 + 清空 current_position + save；重构后由编排层执行，
          成交结果通知 ✅/❌ 随编排层动作发送）
        """
        kospi_pct = jp.get('kospi_pct') or 0
        # 修复 C3: fetch_jp_indices 返回键名是 'nk225_pct'（原写 'nk_pct' 恒为 0，日经跳空通道从未生效）
        nk_pct = jp.get('nk225_pct') or 0
        pos_direction = position.get('direction', '')
        pos_entry = position.get('entry_price', 0)
        pos_sl = position.get('stop_loss', 0)
        pos_tp = position.get('take_profit', 0)

        # 1. 方向冲突检测
        gap_risk = False
        risk_reason = ""
        if pos_direction == 'SHORT' and kospi_pct >= 2.0:
            gap_risk = True
            risk_reason = f"SHORT 持仓 + KOSPI 涨 {kospi_pct:+.2f}% ≥ 2% (方向冲突)"
        elif pos_direction == 'LONG' and kospi_pct <= -2.0:
            gap_risk = True
            risk_reason = f"LONG 持仓 + KOSPI 跌 {kospi_pct:+.2f}% ≤ -2% (方向冲突)"
        elif pos_direction == 'SHORT' and nk_pct >= 2.0:
            gap_risk = True
            risk_reason = f"SHORT 持仓 + 日经 涨 {nk_pct:+.2f}% ≥ 2% (方向冲突)"
        elif pos_direction == 'LONG' and nk_pct <= -2.0:
            gap_risk = True
            risk_reason = f"LONG 持仓 + 日经 跌 {nk_pct:+.2f}% ≤ -2% (方向冲突)"

        if not gap_risk:
            return None  # 风险低，不需要告警

        # 2. 高风险告警（9:00 早盘前必发）
        if not is_auction_time:
            logging.warning(f"⚠️ 盘前跳空风险: {risk_reason}")
            self._send(
                f"⚠️ 盘前跳空风险: {risk_reason}\n"
                f"持仓 {pos_direction} @ {pos_entry:.2f}, 止损 {pos_sl:.2f}\n"
                f"集合竞价出来后会自动调整止损"
            )
            return None

        # 3. 集合竞价出来后主动市价平仓（9:25:30 后）
        #    6/12 案例: KOSPI 涨 8% + 集合竞价 8294.9
        #    如果 SHORT 持仓 → 主动市价平仓，限制最大亏损为 跳空幅度 + 10点滑点
        #    9:25:30 ~ 9:30 之间还有 4-5 分钟可用市价单成交
        if self.mds.index_price <= 0:
            self.mds.update_index_price()
        if self.mds.index_price <= 0:
            logging.warning("集合竞价指数仍未就绪，跳过主动平仓")
            return None

        # 估算期货开盘价 = 指数 + 基差
        try:
            basis_info = self.mds.get_basis_info()
            basis = basis_info.get('basis', -16) if isinstance(basis_info, dict) else -16
        except Exception:
            basis = -16
        expected_futures_open = self.mds.index_price + basis / 100 * self.mds.index_price

        # 计算预期亏损
        if pos_direction == 'SHORT':
            expected_loss = (expected_futures_open - pos_entry) * 200  # 正数=亏损
        else:
            expected_loss = (pos_entry - expected_futures_open) * 200

        # 主动市价平仓阈值：预期亏损 >= 3000 元
        # (避免 7-8% 跳空造成 5000-10000 元亏损，限制到 ~3000 元)
        AUTO_CLOSE_LOSS_THRESHOLD = 3000
        if expected_loss >= AUTO_CLOSE_LOSS_THRESHOLD:
            logging.warning(
                f"⚠️ 盘前跳空: 主动市价平仓 {pos_direction} @ {pos_entry:.2f}, "
                f"集合竞价估算 {expected_futures_open:.2f}, 预期亏损 {expected_loss:.0f}元"
            )
            self._send(
                f"🚨 盘前跳空主动平仓: {pos_direction} @ {pos_entry:.2f}\n"
                f"集合竞价 {self.mds.index_price:.2f}, 估算开盘 {expected_futures_open:.2f}\n"
                f"预期亏损 {expected_loss:.0f}元 ≥ 阈值 {AUTO_CLOSE_LOSS_THRESHOLD}元"
            )
            # 市价平仓（真源 L3940–3974: execute_order_safe + 清仓 + save + ✅/❌ 通知
            # → 重构: 返回动作建议由编排层执行）
            close_direction = 'BUY' if pos_direction == 'SHORT' else 'SELL'
            return SessionAction(
                action="CLOSE_POSITION",
                reason=f"盘前跳空主动平仓: {risk_reason}",
                close_direction=close_direction,
                volume=position.get('volume', 1),
                expected_loss=expected_loss,
            )
        return None

    # ========== 隔夜反转风险（真源 _check_overnight_reversal_risk L3976–4046） ==========

    def check_overnight_reversal_risk(self, position: dict) -> bool:
        """
        隔夜反弹/跳空风险检测（14:55 收盘前调用，只告警不影响决策）。
        6/12 案例: KOSPI 6/11 跌 -4.31% → 次日跳空 +6.44% 穿止损 -29240

        设计原则：只发钉钉告警，不自动平仓，让用户自己决定。
        检测信号：
        1. KOSPI 当日跌 ≥ 3%
        2. KOSPI 2日累计跌 ≥ 5%
        3. KOSPI 当日振幅 ≥ 4%
        4. 持仓方向与 KOSPI 趋势相反
        （真源永远返回 False，不影响主流程——保真保留）
        """
        if not position.get('direction'):
            return False  # 无持仓，不需要评估

        try:
            # 修复 C2: 原实现 get_kline_data('000852.SS', '5min') 不在 INDEX_NAME_MAP 中恒返回 None，
            # 且用大写列名 'Open'/'Close' 与 index.date（RangeIndex 无该属性）→ 整个隔夜反转风控从未生效。
            # 改用 fetch_jp_indices 的 KOSPI 5min 数据（小写键 open/high/low/close + prev_close）。
            jp = self.jp.fetch_jp_indices()
            kospi_bars = jp.get('kospi_5min', []) if jp else []
            kospi_prev_close = jp.get('kospi_prev_close') if jp else None
            if not kospi_bars or not kospi_prev_close:
                logging.warning("隔夜反弹风险检测: KOSPI 数据不可用，跳过")
                return False

            # 当日涨跌幅/振幅以"昨日收盘"为基准（含隔夜跳空，比原 open→close 口径更贴近跳空风险）
            today_open = kospi_bars[0]['open']
            today_close = kospi_bars[-1]['close']
            today_high = max(b['high'] for b in kospi_bars)
            today_low = min(b['low'] for b in kospi_bars)
            kospi_today_pct = (today_close - kospi_prev_close) / kospi_prev_close * 100
            kospi_today_amp = (today_high - today_low) / kospi_prev_close * 100
            # 2日累计：yfinance 只回传当日 5min bars + 昨日收盘，2日 ≈ 当日（以昨收起算）
            kospi_2day_pct = kospi_today_pct

            pos_dir = position.get('direction', '')

            # 收集信号
            signals = []
            if kospi_today_pct <= -3.0:
                signals.append(f"KOSPI 今日 {kospi_today_pct:+.2f}%")
            if kospi_2day_pct <= -5.0:
                signals.append(f"KOSPI 2日累计 {kospi_2day_pct:+.2f}%")
            if kospi_today_amp >= 4.0:
                signals.append(f"KOSPI 振幅 {kospi_today_amp:+.2f}%")

            if not signals:
                return False  # 风险低，不告警

            # 检查持仓方向是否与 KOSPI 趋势冲突
            conflict_msg = ""
            if pos_dir == 'SHORT' and kospi_today_pct <= -3.0:
                conflict_msg = f"\n⚠️ 持仓 {pos_dir} @ {position.get('entry_price', 0):.2f} 方向冲突"
            elif pos_dir == 'LONG' and kospi_today_pct >= 3.0:
                conflict_msg = f"\n⚠️ 持仓 {pos_dir} @ {position.get('entry_price', 0):.2f} 方向冲突"

            # 只告警，不自动操作
            logging.info(f"隔夜反弹风险告警: KOSPI 今日 {kospi_today_pct:+.2f}%, 命中 {len(signals)} 个信号")
            self._send(
                f"📊 14:55 隔夜反弹风险告警（仅供参考，不自动平仓）\n"
                f"KOSPI: 今日 {kospi_today_pct:+.2f}% / 2日 {kospi_2day_pct:+.2f}% / 振幅 {kospi_today_amp:+.2f}%\n"
                f"触发信号:\n" + "\n".join(f"  • {s}" for s in signals) +
                f"{conflict_msg}\n"
                f"参考 6/12 案例：KOSPI -4.31% 后次日跳空 +6.44% 穿止损 -29240\n"
                f"请自行决定：保留过夜 / 主动平仓 / 调整止损"
            )
            return False  # 永远返回 False，不影响主流程
        except Exception as e:
            logging.error(f"隔夜反弹风险检测异常: {e}", exc_info=True)
            return False

    # ========== 12:30 预览（真源 _lunch_breakout_preview L4048–4099） ==========

    def lunch_breakout_preview(self, now: Optional[datetime] = None) -> None:
        """
        12:30 顺势单早期预览（KOSPI 午盘开始 1h 后）。
        用途：让用户在 12:50 顺势单实际触发前 20 分钟看到"如果触发会怎样"，
             包括预估方向、入场价、止损/止盈点、最大盈亏金额。
        触发条件：KOSPI 11:30-12:30 振幅 >= 0.5% 或 |变动| >= 0.3%
        不会实际下单，仅通知。
        """
        now = now or self.now_fn()
        if self._lunch_preview_sent_date is None or self._lunch_preview_sent_date != now.date():
            pass  # 第一次进入，初始化
        else:
            return  # 今天已发过预览

        # 拉日韩数据
        jp = self.jp.fetch_jp_indices()
        if not jp:
            return

        # 计算 11:30-12:30 KOSPI 午盘窗口指标
        win = self.jp.calc_kospi_amp_delta_in_window('11:30', '12:30')
        if win is None:
            return

        max_move = win['amp']
        delta = win['delta']

        # 触发条件：振幅 >= 0.5% 或 |变动| >= 0.3%（提前 20 分钟预警）
        if max_move < 0.5 and abs(delta) < 0.3:
            return

        # 还需要 ATR_15 已就绪
        if self.mcs.atr_15 <= 0:
            return

        # 标记今日已发
        self._lunch_preview_sent_date = now.date()

        # 简化 12:30 预览：只展示数据，不预测方向和 SL/TP
        # 6/17 教训: 12:30 太早，方向无法准确判断（KOSPI 实际涨 +0.9%，系统判跌）
        # 12:50 顺势单会用 kospi_pct（vs昨收全天的累积方向）做最终决定
        kospi_pct = jp.get('kospi_pct') if jp else 0
        nk_pct = jp.get('nk225_pct') if jp else 0
        msg = (
            f"⚠️ 12:30 顺势单预览（12:50 可能触发）\n"
            f"📊 KOSPI 11:30-12:30: 振幅 {max_move:+.2f}%, 变动 {delta:+.2f}%\n"
            f"📈 当日累计: KOSPI {kospi_pct:+.2f}% (vs昨收) / 日经 {nk_pct:+.2f}%\n"
            f"⏰ 12:50 顺势单触发条件: 振幅≥1.0% & |变动|≥0.5%\n"
            f"🕐 强制平仓时间: 14:00\n"
            f"\n💡 12:50 系统会用 KOSPI 全天趋势（vs昨收）决定方向，现在请勿据此操作"
        )
        logging.info(msg)
        self._send(msg)

    # ========== 12:50 顺势单（真源 _lunch_breakout_check L4101–4272） ==========

    def lunch_breakout_check(self, position: dict,
                             now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """
        12:50 顺势单检查。
        逻辑：
          1. 拉 12:30 日经/Topix 涨跌幅
          2. 若 |日经涨跌幅| >= 0.6% 且 11:30→12:30 期间变动 >= 0.5%
             → 顺势开仓（LONG/SHORT）
          3. 风控：止损 0.2×ATR / 止盈 0.7×ATR / 强制 14:00 平仓
          4. 钉钉通知
        注意：每日只触发一次，由 self.lunch_breakout_today 守护。

        返回: 条件单 dict（键集 = 真源 L4238–4250，编排层存入条件单管理器并持久化）；
              不触发返回 None。
        （真源 L4274–4296 为 return 后不可达代码，未迁移——见模块 docstring）
        """
        now = now or self.now_fn()
        # 1. 每日只触发一次
        if self.lunch_breakout_today['triggered']:
            return None

        # 2. 必须有 ATR（期货已开盘 1.5h，15min K 线应已成型）
        if self.mcs.atr_15 <= 0:
            logging.warning("12:50 顺势单跳过：atr_15 还未就绪")
            return None

        # 1.5 立即设置 triggered=True（防 12:50-12:51 重复触发）
        #     下单失败也会保留此标记（避免 17 次 FAILED 重试）
        self.lunch_breakout_today['triggered'] = True
        self.lunch_breakout_today['trigger_time'] = now.strftime('%Y-%m-%d %H:%M:%S')
        logging.info("12:50 顺势单已标记 triggered=True（防重复触发）")

        # 3. 已有持仓 → 不与顺势单叠加（避免双重仓位）
        if position['direction']:
            logging.info("12:50 顺势单跳过：当前已持仓")
            return None

        # 4. 拉 12:30 日韩数据
        jp = self.jp.fetch_jp_indices()
        if not jp:
            logging.warning("12:50 顺势单跳过：日韩数据未取到")
            return None

        nk_pct = jp.get('nk225_pct')
        kospi_pct = jp.get('kospi_pct')

        # 5. 写 lunch_context
        if nk_pct is not None:
            refresh_lunch_context(self.lunch_context, 'nk225_1230_pct', nk_pct)
        if kospi_pct is not None:
            refresh_lunch_context(self.lunch_context, 'kospi_1230_pct', kospi_pct)

        # 6. 计算 11:30-12:50 区间 KOSPI 振幅+变动（最关键指标）
        #    窗口覆盖整个午休前段，12:50 顺势单依据"日内累计方向"判断
        win = self.jp.calc_kospi_amp_delta_in_window('11:30', '12:50')
        if win is None:
            max_move = abs(kospi_pct) if kospi_pct is not None else 0
            delta = kospi_pct if kospi_pct is not None else 0
            logging.warning(f"12:50 顺势单：calc_kospi_amp_delta_in_window 失败，fallback 到 {kospi_pct}")
        else:
            max_move = win['amp']
            delta = win['delta']
        refresh_lunch_context(self.lunch_context, 'kospi_1230_max_move', max_move)
        refresh_lunch_context(self.lunch_context, 'kospi_1230_delta', delta)

        # 7. 触发条件 (回放 6/1-6/9 7天 + 6/16 加严):
        #    11:30→12:50 KOSPI 区间最大振幅 >= 1.0% **且** |变动| >= 0.5%
        #    加 |delta| >= 0.5% 是为了过滤"双向震荡"假信号（6/16 1.09% 振幅 + 0.3% 变动 是边缘）
        if max_move < 1.0:
            logging.info(
                f"12:50 顺势单不触发：KOSPI 11:30-12:50 振幅 {max_move:.2f}% < 1.0% "
                f"(参数: amp>=1.0% & |delta|>=0.5%, sl=0.2×ATR, tp=0.7×ATR, 回放 7天 +8000元)"
            )
            return None
        if abs(delta) < 0.5:
            logging.info(
                f"12:50 顺势单不触发：KOSPI 11:30-12:50 变动 {delta:+.2f}% |delta|<0.5% "
                f"（振幅 {max_move:.2f}% 过阈值，但变动不充分，避免双向震荡假信号）"
            )
            return None

        # 8. 顺势方向：用 KOSPI 全天趋势（vs 昨收）而非 1h 窗口
        #    6/17 bug: 11:30-12:50 delta=-0.04% → SELL，但 KOSPI 全天涨 +1.3%（vs 今早）
        #    修复：用 kospi_pct（从昨收的累积方向）决定方向，更稳健
        if kospi_pct is not None and kospi_pct != 0:
            direction_full = 'BUY' if kospi_pct > 0 else 'SELL'
        else:
            direction_full = 'BUY' if delta > 0 else 'SELL'  # fallback
        cur_price = self.mds.im_quote.last_price
        if cur_price <= 0:
            logging.warning("12:50 顺势单跳过：last_price 异常")
            return None

        # 8. 风控：止损 0.2×ATR / 止盈 0.7×ATR / 强制 14:00 平仓
        #    (回放 6/1-6/9 7天验证: 5触发 2胜3负 +8000元, 最高盈利组合)
        LUNCH_SL_MULT = 0.2
        LUNCH_TP_MULT = 0.7
        sl_dist = self.mcs.atr_15 * LUNCH_SL_MULT
        tp_dist = self.mcs.atr_15 * LUNCH_TP_MULT
        if direction_full == 'BUY':
            stop_loss = cur_price - sl_dist
            take_profit = cur_price + tp_dist
        else:
            stop_loss = cur_price + sl_dist
            take_profit = cur_price - tp_dist
        volume = 1  # 顺势单默认 1 手（小仓）

        # 9. 钉钉预通知（强化 SL/TP 显示）
        # 修复 6/16: 标明 KOSPI +X.XX% 是从 prev_close 累计的（不是午盘 11:30-12:50 内的）
        # max_move 和 delta 才是 11:30→12:50 午盘窗口的实际指标
        sl_pnl = -sl_dist * 1 * 200
        tp_pnl = tp_dist * 1 * 200
        risk_reward = tp_dist / sl_dist if sl_dist > 0 else 0
        msg = (
            f"🔥 12:50 顺势单触发: {direction_full} {volume}手\n"
            f"📊 KOSPI 11:30→12:50 午盘 振幅 {max_move:+.2f}%, 变动 {delta:+.2f}%\n"
            f"📈 当日累计: 日经 {nk_pct:+.2f}%, KOSPI {kospi_pct if kospi_pct is not None else 0:+.2f}%\n"
            f"\n"
            f"💰 入场价: {cur_price:.2f} (ATR_15={self.mcs.atr_15:.1f})\n"
            f"🛑 止损: {stop_loss:.2f} (-{sl_dist:.1f}点 / {sl_pnl:.0f}元)\n"
            f"🎯 止盈: {take_profit:.2f} (+{tp_dist:.1f}点 / {tp_pnl:.0f}元)\n"
            f"⚖️ 盈亏比 1:{risk_reward:.2f}\n"
            f"\n"
            f"⏰ 强制平仓时间: 14:00 (1h10m后)"
        )
        logging.info(msg)
        self._send(msg)

        # 10. 走条件单（不在 12:50 市价下单——等 13:00 期货开盘瞬间用条件单触发）
        #    设计: 12:50 设置一个永远会被触发的条件单（PRICE_ABOVE/BELOW + 极小偏移）
        #    主循环 check_conditional_order() 会在 13:00+ 自动监控并成交
        #    避免 12:50-12:51 重复 17 次市价单问题
        #    （真源写全局 conditional_order + save_position_state → 重构返回 dict
        #      由编排层存储并持久化）
        try:
            if direction_full == 'BUY':
                # 做多: 触发价 = last_price - 5 (确保 13:00 任何价格都 PRICE_ABOVE 触发)
                trigger_type = 'PRICE_ABOVE'
                trigger_price_futures = cur_price - 5
            else:
                # 做空: 触发价 = last_price + 5
                trigger_type = 'PRICE_BELOW'
                trigger_price_futures = cur_price + 5

            order = {
                'action': direction_full,
                'trigger_type': trigger_type,
                'trigger_price': trigger_price_futures,
                'limit_price': 0,  # 0 = 用对手价成交
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'volume': volume,
                'source': '12:50_lunch_breakout',  # 标记来源
                'kospi_amp': max_move,
                'kospi_delta': delta,
                'force_close_time': '14:00',
            }
            logging.info(
                f"12:50 顺势单已设置为条件单: {direction_full} {volume}手, "
                f"触发价 {trigger_price_futures:.2f} ({trigger_type}), "
                f"止损 {stop_loss:.2f}, 止盈 {take_profit:.2f}"
            )
            self._send(
                f"📌 12:50 顺势单条件单已挂: {direction_full} {volume}手, "
                f"触发价 {trigger_price_futures:.2f} ({trigger_type})\n"
                f"止损 {stop_loss:.2f} (0.2×ATR), "
                f"止盈 {take_profit:.2f} (0.7×ATR), "
                f"强制平 14:00"
            )
            return order
        except Exception as cond_exc:
            logging.error(f"[12:50 顺势单条件单异常] {cond_exc}", exc_info=True)
            if self.logger is not None:
                self.logger.log("FAILED", self.mds.symbol, direction_full, volume, 0.0,
                                ai_reason=f"12:50顺势单条件单异常: {cond_exc}")
            return None

    # ========== 14:00 强平（真源 _lunch_force_close_check L4298–4312） ==========

    def lunch_force_close_check(self, position: dict,
                                now: Optional[datetime] = None) -> bool:
        """
        14:00 强制平仓检查：12:50 顺势单到 14:00 不论盈亏都平。
        返回 True = 编排层应立即平仓（真源直接调 close_position）。

        真源 quirk 保真: force_close_deadline 在真源活代码中从未赋值（唯一赋值点
        L4287 在 return 后不可达块内）→ deadline 恒 None → 强平实际永不触发。
        编排层若要激活 14:00 强平，须显式设置 lunch_breakout_today['force_close_deadline']。
        """
        now = now or self.now_fn()
        if not self.lunch_breakout_today['triggered']:
            return False
        if not position['direction']:
            return False
        deadline = self.lunch_breakout_today.get('force_close_deadline')
        if deadline and now >= deadline:
            # 强制平
            logging.info("[12:50顺势单] 14:00 强制平仓")
            self._send("⏰ 12:50顺势单 14:00 强制平仓")
            # 真源: self.close_position(reason="12:50顺势单14:00强制平")
            # → 重构: 返回 True 由编排层执行（标记本单完成不重置 triggered）
            return True
        return False

    # ========== 隔夜持仓评估（真源 _evaluate_overnight_holding L3520–3646） ==========

    def evaluate_overnight_holding(self, position: dict,
                                   now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """
        14:55 隔夜持仓评估（AI 参与决策）。
        返回: {"action": "HOLD"/"CLOSE", "reason": ...}（CLOSE 由编排层执行平仓）；
              本 tick 不评估返回 None（空仓/节流/未到时点/尾盘新开/AI 异常）。
        """
        now = now or self.now_fn()
        if not position['direction']:
            return None

        # 6/15 bug 修复: 节流（5 分钟 1 次），避免每 0.5s 重复打印刷爆 log
        # 6/15 14:55-15:00 共打印 600+ 条重复 INFO
        now_ts = time.time()
        if now_ts - self._overnight_eval_last_log < 300:  # 5 分钟节流
            return None
        # 跳过纯空跑（持仓但不到 14:55 评估时机），保留 check_overnight_reversal_risk 等关键逻辑
        # 但如果还没到 14:55，直接 return（不打印）
        if now.time() < dt_time(14, 55):
            return None

        # 6/12 案例修复: 不要强制保留 14:30 后所有持仓
        # 区分"今日 14:30 之后新开仓"和"14:30 之前开仓"
        # 14:30 之前开仓的 → 应该让 AI 评估过夜风险（可能平仓）
        # 14:30 之后开仓的（尾盘新开）→ 强制保留过夜（信号刚发出，不应立刻平）
        entry_time_str = position.get('entry_time', '')
        is_recent_entry = False
        if entry_time_str:
            try:
                # entry_time 可能是 datetime 对象（6/15 修复后）也可能是字符串（兼容旧数据）
                if isinstance(entry_time_str, datetime):
                    entry_dt = entry_time_str
                else:
                    entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %H:%M:%S')
                # 如果开仓时间在今日 14:30 之后，视为"尾盘新开"
                if entry_dt.date() == now.date() and entry_dt.time() >= dt_time(14, 30):
                    is_recent_entry = True
            except Exception:
                pass
        # 如果没有 entry_time 记录（兼容旧数据），默认强制过夜
        if not entry_time_str:
            logging.info("持仓无 entry_time 记录，默认强制保留过夜")
            is_recent_entry = True

        # 更新节流时间戳
        self._overnight_eval_last_log = now_ts

        if is_recent_entry:
            logging.info("尾盘新开仓（14:30 后开），强制保留过夜")
            return None

        # ========== 隔夜反弹风险检测（14:55 收盘前自动评估）==========
        # 6/12 案例: KOSPI 6/11 跌 -4.31% + 振幅大 → 次日跳空 +8% 穿止损 -29240
        # 即使不是尾盘新仓，也要在收盘前识别这种"次日必跳空"风险
        # （真源该方法永远返回 False，此守卫实际不触发——保真保留）
        # ============================================================
        if self.check_overnight_reversal_risk(position):
            # 高风险已主动平仓 + 清空 current_position
            return None

        # 刷新数据（真源 _refresh_tech_data + _calculate_fut_atr → mds/mcs）
        self.mds.refresh_tech_data()
        self.mcs.calculate_fut_atr()
        news_text = self._news_text()

        # 计算浮动盈亏
        pos = self.mds.api.get_position(self.mds.symbol)  # 真源 L3584，赋值后未使用（保真保留）
        if position['direction'] == 'LONG':
            unreal_pnl = (self.mds.im_quote.last_price - position['entry_price']) * position['volume'] * 200
        else:
            unreal_pnl = (position['entry_price'] - self.mds.im_quote.last_price) * position['volume'] * 200

        sys_prompt = """你是一个期货持仓过夜风险评估专家。你的默认立场是**趋势未完，持仓应延续**，只在出现明确不利变化时才平仓。

        **输出要求：**
        - 纯JSON：{"action": "HOLD" | "CLOSE", "reason": "简短理由"}
        - 如果决定 CLOSE，必须指出至少一条客观触发条件被满足；否则必须 HOLD。

        **决策规则（按优先级）：**
        1. **强制CLOSE条件**（满足任一即平仓）：
           - 市场出现重大黑天鹅事件（如战争爆发、交易所紧急停牌等），且该事件直接冲击你的持仓方向。
           - 你的止损或止盈已被触及（但系统已在实时监控，这里仅作为最后一道确认）。
           - 持仓方向的技术结构已明确反转：例如做多时，30分钟K线收盘跌破前低且成交量放大；做空时反之。

        2. **强烈建议CLOSE条件**（满足时应重点考虑，但非必须）：
           - 距休市<5分钟，且浮动亏损已达到账户权益的1%以上，且无明确趋势保护。
           - 重要隔夜新闻（如央行决议、重磅数据）即将公布，且方向不可预测。

        3. **如果以上条件均不满足，你必须 HOLD**，哪怕市场暂时小幅不利于你。

        **持有信心加分项（帮助你在理由中解释为何持有）：**
        - 持仓方向与至少3个周期的均线排列方向一致：+1级信心
        - 价格在开仓后已脱离成本区（盈利>0.3%）且回调未破关键均线：+1级
        - 隔夜风险可控（如无重大事件、VIX稳定、外围市场平静）：+1级

        请在reason中简述当前处于哪一类决策，并提及是否满足上述条件。
        """
        user_prompt = f"""请基于以下信息决定是否持仓过夜：

    ## 当前持仓（价格均为 IM 期货价格）
    - 方向: {position['direction']}
    - 手数: {position['volume']}
    - 开仓均价（期货）: {position['entry_price']:.2f}
    - 当前价格（期货）: {self.mds.im_quote.last_price:.2f}
    - 浮动盈亏: {unreal_pnl:.2f} 元
    - 当前止损（期货）: {position['stop_loss']:.2f}
    - 当前止盈（期货）: {position['take_profit']:.2f}
    - 开仓理由: {position.get('last_ai_decision', '无记录')}

    ## 市场数据
    {self.mds.tech_data_text}

    ## 重要新闻
    {news_text}

    注意：当前时间 {now.strftime('%H:%M:%S')}，即将收盘。请决策。
    """
        try:
            response = self.ai_chat_fn(messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ])
            decision = json.loads(response)
            if decision.get('action') == 'CLOSE':
                # 真源: self.close_position(f"收盘前平仓（AI建议不过夜，理由：{...}）")
                # → 重构: 返回 CLOSE 建议由编排层执行
                return {"action": "CLOSE",
                        "reason": f"收盘前平仓（AI建议不过夜，理由：{decision.get('reason')}）"}
            logging.info(f"AI建议持仓过夜，理由：{decision.get('reason')}")
            return {"action": "HOLD", "reason": decision.get('reason')}
        except Exception as e:
            logging.error(f"过夜评估失败: {e}，默认保留持仓")
            return None

    # ========== 开盘后持仓分析（真源 _post_open_analysis L4314–4412） ==========

    def post_open_analysis(self, position: dict) -> Optional[Dict[str, Any]]:
        """
        开盘后持仓分析（9:25执行）。
        仅当有持仓时调用AI评估是否调整止盈止损。
        若AI建议调整，则更新持仓并保存。

        返回: {"adjust_stop_loss": ..., "adjust_take_profit": ..., "reason": ...}
              （仅含实际变更的字段，编排层写入 PositionManager 并持久化）；
              无调整/无决策返回 None。
        """
        if not position['direction']:
            logging.info("当前空仓，跳过开盘后持仓分析")
            return None

        # 刷新数据
        self.mds.update_index_price()  # 今日开盘价
        self.mds.refresh_tech_data()
        self.mcs.calculate_fut_atr()
        # 获取昨日收盘基差
        yesterday_index_close = self.mds.get_yesterday_index_close()
        yesterday_im_close = self.mds.im_quote.last_price  # 昨收（期货未开盘时为昨收）

        basis_info = ""
        if yesterday_index_close is not None and yesterday_im_close > 0:
            yesterday_basis = yesterday_im_close - yesterday_index_close
            yesterday_basis_pct = (yesterday_basis / yesterday_index_close) * 100
            basis_info = f"""
    ## 昨日收盘基差（参考）
    - 昨日期货收盘: {yesterday_im_close:.2f}
    - 昨日指数收盘: {yesterday_index_close:.2f}
    - 昨日基差: {yesterday_basis:.2f}点 ({yesterday_basis_pct:.2f}%)
    - 状态: {"贴水" if yesterday_basis < 0 else "升水"}
    """

        sys_prompt = """你是一个风险控制专家。当前为开盘后（9:25），指数已产生开盘价，但期货尚未开盘。
    用户持有IM期货隔夜仓位。请根据指数开盘价、隔夜新闻、技术面数据以及昨日收盘基差，判断是否需要调整现有持仓的止损价或止盈价。
    输出严格JSON格式：
    {
      "adjust_stop_loss": 数字|null,
      "adjust_take_profit": 数字|null,
      "reason": "简短理由"
    }
    如果无需调整，对应字段设为null。"""

        pos_desc = (f"{position['direction']} {position['volume']}手，"
                    f"开仓均价{position['entry_price']:.2f}，"
                    f"当前止损{position['stop_loss']:.2f}，止盈{position['take_profit']:.2f}")
        news_all = self._news_text()
        user_prompt = f"""
    ## 指数开盘状态
    - 中证1000指数今日开盘价：{self.mds.index_price:.2f}
    {basis_info}

    ## 当前持仓
    {pos_desc}

    ## 隔夜重要新闻摘要
    {news_all}

    ## 指数技术面数据（包含今日开盘信息）
    {self.mds.tech_data_text}

    请判断是否需要调整止盈/止损。
    """

        try:
            response = self.ai_chat_fn(messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ])
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                logging.warning("开盘后分析未返回有效JSON")
                return None
            decision = json.loads(json_match.group())

            new_sl = decision.get('adjust_stop_loss')
            new_tp = decision.get('adjust_take_profit')
            changed = False
            sl_changed = False
            tp_changed = False
            if new_sl is not None and new_sl != position['stop_loss']:
                # 真源: current_position['stop_loss'] = new_sl（写全局）
                # → 重构: 记日志 + 返回建议，由编排层写入 PositionManager
                if self.logger is not None:
                    self.logger.log("ADJUST_STOP", self.mds.symbol, position['direction'],
                                    position['volume'], new_sl, ai_reason=decision.get('reason', ''))
                logging.info(f"开盘后止损调整为 {new_sl}")
                changed = True
                sl_changed = True
            if new_tp is not None and new_tp != position['take_profit']:
                if self.logger is not None:
                    self.logger.log("ADJUST_PROFIT", self.mds.symbol, position['direction'],
                                    position['volume'], new_tp, ai_reason=decision.get('reason', ''))
                logging.info(f"开盘后止盈调整为 {new_tp}")
                changed = True
                tp_changed = True
            if changed:
                # 真源: save_position_state + 钉钉通知 → 重构: 通知保留，持久化随编排层应用建议
                self._send(
                    f"IM开盘后调整: 止损{new_sl if new_sl else '不变'}, 止盈{new_tp if new_tp else '不变'}, 理由:{decision.get('reason')}")
                return {
                    "adjust_stop_loss": new_sl if sl_changed else None,
                    "adjust_take_profit": new_tp if tp_changed else None,
                    "reason": decision.get('reason'),
                }
            logging.info("开盘后分析：无需调整止盈止损")
            return None
        except Exception as e:
            logging.error(f"开盘后分析失败: {e}")
            return None
