"""execution_pipeline — AI 决策编排（真源 2 个方法，design.md §4.2 execution_pipeline 表）。

方法映射:
- ExecutionPipeline.execute_decision ← execute_decision L2108–2925（817 行八步编排，
  内含嵌套 conv L2112，随宿主方法保留为嵌套闭包）
  八步: P1 止损冷却 → P0 ratchet → 持仓调整 → 条件单处理 → 即时入场
        → 同向加仓 → 反向平仓 → 新开仓
- ExecutionPipeline.execute_ai_cycle ← _execute_ai_cycle L5376–5433（单次 AI 决策循环）

结构差异（ARCHITECTURE.md 阶段 4 决策记录）:
- 全局 current_position/conditional_order 读写 → pm（PositionManager）——
  design.md §5.4 "execute_decision 817 行全局状态读写密集" 的状态归属落点
- 过滤器/豁免 Tuple[bool, str] → FilterResult（.allowed/.reason）
- _check_tail_session → tail_fn 注入；止损冷却 → StopOutCooldown；
  风控兜底 → PositionSizer/DailyTradeLimiter/CircuitBreaker
- _failed_order_window 懒初始化（真源 L2872）→ 构造初始化（行为等价）
- execute_ai_cycle 的 prompt 构建/AI 调用/决策落盘 → prompt_fn/ai_chat_fn/
  save_decision_fn 注入（ai_decision 模块 9 方法按 design.md §5.2 不属阶段 4，
  阶段 5 落位后接线；本阶段测试注入假实现）
"""
import json
import logging
import re
from datetime import datetime
from typing import Callable, Dict, Optional

from quantai.config import (ADD_MAX_DRAWDOWN_PCT, ADD_MIN_PRICE_GAP_ATR,
                            ADD_REQUIRED_CONFIDENCE, BASE_DECISION_INTERVAL,
                            MAX_DECISION_INTERVAL, MAX_POSITION_LOTS,
                            MIN_CONFIDENCE, MIN_DECISION_INTERVAL,
                            MIN_STOP_DISTANCE_ATR_MULT,
                            MIN_STOP_DISTANCE_ATR_MULT_COND,
                            MAX_STOP_DISTANCE_ATR_MULT,
                            SHORT_TERM_INTERVAL,
                            STOP_ADJUST_COOLDOWN,
                            STOP_RELAX_REQUIRED_CONFIDENCE)


class ExecutionPipeline:
    """execute_decision 八步编排 + execute_ai_cycle（依赖注入、无全局状态）。"""

    def __init__(self, *, pm, mds, mcs, sizer, daily_limiter, circuit_breaker,
                 stopout, filters, exemptions, oe, tail_fn: Callable,
                 notifier, logger,
                 prompt_fn: Optional[Callable] = None,
                 ai_chat_fn: Optional[Callable] = None,
                 save_decision_fn: Optional[Callable] = None,
                 now_fn: Callable[[], datetime] = datetime.now):
        self.pm = pm                    # PositionManager
        self.mds = mds                  # MarketDataService（im_quote/symbol/index_to_future_price）
        self.mcs = mcs                  # MarketContextService（atr_5/atr_15）
        self.sizer = sizer              # PositionSizer
        self.daily_limiter = daily_limiter
        self.cb = circuit_breaker
        self.stopout = stopout          # StopOutCooldown
        self.filters = filters          # EntryFilters
        self.exemptions = exemptions    # Exemptions
        self.oe = oe                    # OrderExecutor
        self.tail_fn = tail_fn          # → SessionPlaysService.check_tail_session
        self.notifier = notifier
        self.logger = logger            # TradeLogger
        self.prompt_fn = prompt_fn      # mode → (sys_prompt, user_prompt)（阶段 5 接 ai_decision）
        self.ai_chat_fn = ai_chat_fn    # messages → str（阶段 5 接 vendor llm_client）
        self.save_decision_fn = save_decision_fn  # decision → None（阶段 5 接 save_ai_decision）
        self.now_fn = now_fn
        # 真源 L427（止损调整冷却）
        self.last_stop_adjust_time = datetime.min
        # 真源 L2872 懒初始化 → 构造初始化（行为等价）
        self._failed_order_window = []  # [(time, action, limit_price), ...]

    def _send(self, msg: str) -> None:
        if self.notifier is not None:
            self.notifier.send(msg)

    # ---------- 真源 execute_decision L2108–2925 ----------

    def execute_decision(self, decision: Dict):
        # ---------- 统一转换辅助函数 ----------
        def conv(p):
            """None 或无效价格则返回原值，否则转为期货价"""
            if p is None or p <= 0:
                return p
            return self.mds.index_to_future_price(p)

        # ============================================================
        # P1：止损后冷却期检查（止损平仓后 15 分钟内禁开同向新仓）
        # ============================================================
        action = decision.get('action', 'WAIT')
        confidence = decision.get('confidence', 0)
        action_dir = 'LONG' if action == 'BUY' else ('SHORT' if action == 'SELL' else None)
        now = self.now_fn()
        cooldown_blocked, elapsed_since_stopout, remaining = self.stopout.check(action_dir, now)
        if cooldown_blocked:
            logging.warning(
                f"⚠️ 止损冷却期内：{action_dir} 方向在 {remaining:.0f} 秒前触发止损，"
                f"还需等待 {remaining/60:.1f} 分钟。拒绝开仓/加仓。"
            )
            # 冷却期内完全禁止同向开仓和加仓，仅允许 adjust_existing 调整止损
            # 但若 adjust_existing 仍想放宽止损，由 ratchet 校验负责拦截
            if not decision.get('adjust_existing'):
                return
            # 如果是 adjust_existing + 同向 action 一起（想加仓），也拒
            if self.pm.position['direction'] == action_dir:
                logging.warning(f"⚠️ 冷却期内禁止加仓")
                return
            # 反向开仓：会先平仓再开，等价于"换仓"，不属"同向重开"，放行
            if self.pm.position['direction'] and self.pm.position['direction'] != action_dir:
                # 反向开新仓不在冷却限制内
                pass

        # ============================================================
        # P0：止损 ratchet 校验（adjust_existing 必须在保护利润方向）
        # ============================================================
        adjust = decision.get('adjust_existing')
        if adjust and self.pm.position['direction']:
            new_sl_raw = adjust.get('new_stop_loss')
            cur_sl = self.pm.position['stop_loss']
            cur_dir = self.pm.position['direction']
            if new_sl_raw is not None and new_sl_raw > 0:
                new_sl = conv(new_sl_raw)
                # 判断方向
                is_relaxing = False
                if cur_dir == 'LONG' and new_sl < cur_sl:
                    is_relaxing = True   # 做多时把止损往下移 = 放宽风险
                elif cur_dir == 'SHORT' and new_sl > cur_sl:
                    is_relaxing = True   # 做空时把止损往上移 = 放宽风险
                if is_relaxing and confidence < STOP_RELAX_REQUIRED_CONFIDENCE:
                    logging.warning(
                        f"⚠️ 止损放宽被拒：当前方向 {cur_dir}，"
                        f"新止损 {new_sl:.2f} 比当前 {cur_sl:.2f} 风险更大，"
                        f"需要 confidence >= {STOP_RELAX_REQUIRED_CONFIDENCE}, "
                        f"实际 {confidence}"
                    )
                    self._send(
                        f"⚠️ 止损放宽被拒：方向 {cur_dir} 信心 {confidence} < {STOP_RELAX_REQUIRED_CONFIDENCE}"
                    )
                    # 移除放宽部分，保留收紧部分
                    adjust = {**adjust, 'new_stop_loss': None}

        # ---------- 处理已有持仓的止损止盈调整 ----------
        if adjust and self.pm.position['direction']:
            # 止损调整冷却：最近STOP_ADJUST_COOLDOWN秒内已调过则跳过
            now_check = self.now_fn()
            if (now_check - self.last_stop_adjust_time).total_seconds() < STOP_ADJUST_COOLDOWN:
                logging.info(f"止损冷却期内（距上次调整{(now_check-self.last_stop_adjust_time).total_seconds():.0f}秒），跳过本次调整")
            else:
                changed = False
                new_sl = conv(adjust.get('new_stop_loss'))
                new_tp = conv(adjust.get('new_take_profit'))
                reason = decision.get('reason', '')
                # ============================================================
                # P0：ADJUST_STOP 方向校验（防 6/15 -4200 bug）
                # 6/15 10:40 LONG 持仓，AI 把止损从 8370 调到 8169.60（亏本 20 点）
                # 触发后 -4200 元。修复：LONG 时 new_sl ≥ max(当前止损, 入场价 - 1×5minATR)
                #                                SHORT 时 new_sl ≤ min(当前止损, 入场价 + 1×5minATR)
                # ============================================================
                if new_sl is not None and self.pm.position.get('entry_price'):
                    cur_sl = self.pm.position.get('stop_loss', new_sl)
                    cur_dir = self.pm.position['direction']
                    entry = self.pm.position['entry_price']
                    if cur_dir == 'LONG':
                        # LONG 止损必须 ≥ max(当前止损, 入场价 - 1×5minATR 保护位)
                        protected_sl = entry - (self.mcs.atr_5 if self.mcs.atr_5 > 0 else 0)
                        floor_sl = max(cur_sl, protected_sl)
                        if new_sl < floor_sl:
                            logging.warning(
                                f"⚠️ ADJUST_STOP 方向错误：LONG 持仓，"
                                f"new_sl={new_sl:.2f} < max(当前止损{cur_sl:.2f}, 保护位{protected_sl:.2f})={floor_sl:.2f}，"
                                f"自动纠正为 {floor_sl:.2f}"
                            )
                            self._send(
                                f"⚠️ ADJUST_STOP 方向纠错：LONG 持仓 new_sl={new_sl:.2f} → 强制 {floor_sl:.2f}（保护位）"
                            )
                            new_sl = floor_sl
                    elif cur_dir == 'SHORT':
                        # SHORT 止损必须 ≤ min(当前止损, 入场价 + 1×5minATR 保护位)
                        protected_sl = entry + (self.mcs.atr_5 if self.mcs.atr_5 > 0 else 0)
                        cap_sl = min(cur_sl, protected_sl)
                        if new_sl > cap_sl:
                            logging.warning(
                                f"⚠️ ADJUST_STOP 方向错误：SHORT 持仓，"
                                f"new_sl={new_sl:.2f} > min(当前止损{cur_sl:.2f}, 保护位{protected_sl:.2f})={cap_sl:.2f}，"
                                f"自动纠正为 {cap_sl:.2f}"
                            )
                            self._send(
                                f"⚠️ ADJUST_STOP 方向纠错：SHORT 持仓 new_sl={new_sl:.2f} → 强制 {cap_sl:.2f}（保护位）"
                            )
                            new_sl = cap_sl
                if new_sl is not None:
                    self.pm.position['stop_loss'] = new_sl
                    self.logger.log("ADJUST_STOP", self.mds.symbol, self.pm.position['direction'],
                                    self.pm.position['volume'], new_sl, ai_reason=reason)
                    logging.info(f"止损更新为 {new_sl}")
                    changed = True
                if new_tp is not None:
                    self.pm.position['take_profit'] = new_tp
                    self.logger.log("ADJUST_PROFIT", self.mds.symbol, self.pm.position['direction'],
                                    self.pm.position['volume'], new_tp, ai_reason=reason)
                    logging.info(f"止盈更新为 {new_tp}")
                    changed = True
                if changed:
                    self.last_stop_adjust_time = now_check
                    self.pm.save_position_state()
                    self._send(
                        f"⚙️ 日间调整: 止损{new_sl if new_sl else '不变'}, "
                        f"止盈{new_tp if new_tp else '不变'}, 理由:{reason}"
                    )

        # ---------- 条件单处理 ----------
        cond = decision.get('conditional_entry')
        action = decision.get('action', 'WAIT')
        confidence = decision.get('confidence', 0)

        # 统一方向（BUY/SELL → LONG/SHORT）
        action_dir = None
        if action == 'BUY':
            action_dir = 'LONG'
        elif action == 'SELL':
            action_dir = 'SHORT'

        if cond and action != 'WAIT' and confidence >= MIN_CONFIDENCE:
            # 全部转换为期货价
            cond['trigger_price'] = conv(cond.get('trigger_price', 0))
            cond['stop_loss'] = conv(cond.get('stop_loss', 0))
            cond['take_profit'] = conv(cond.get('take_profit', 0))
            cond['action'] = action

            # ============================================================
            # P0：条件单止损距离校验（条件单止损 ≥ 0.6×5minATR）
            # (从15minATR改为5minATR，紧凑型止损)
            # ============================================================
            if self.mcs.atr_5 > 0 and cond['trigger_price'] > 0 and cond['stop_loss'] > 0:
                sl_distance = abs(cond['trigger_price'] - cond['stop_loss'])
                min_dist = self.mcs.atr_5 * MIN_STOP_DISTANCE_ATR_MULT_COND
                if sl_distance < min_dist:
                    logging.warning(
                        f"⚠️ 条件单止损过紧：距离 {sl_distance:.2f} < {min_dist:.2f} "
                        f"({MIN_STOP_DISTANCE_ATR_MULT_COND}×5minATR)，自动放宽止损到 {min_dist:.2f}点距离"
                    )
                    if action == 'BUY':
                        cond['stop_loss'] = cond['trigger_price'] - min_dist
                    else:
                        cond['stop_loss'] = cond['trigger_price'] + min_dist
                    self._send(
                        f"⚠️ 条件单止损自动放宽至 {cond['stop_loss']:.0f}"
                    )
                # ========== 8/14 新增：条件单止损距离硬上限 ==========
                # 止损距离 > 3×15minATR → 自动收紧（防 AI 设超宽止损）
                if self.mcs.atr_15 > 0:
                    max_sl = self.mcs.atr_15 * MAX_STOP_DISTANCE_ATR_MULT
                    if sl_distance > max_sl:
                        old_sl = cond['stop_loss']
                        if action == 'BUY':
                            cond['stop_loss'] = cond['trigger_price'] - max_sl
                        else:
                            cond['stop_loss'] = cond['trigger_price'] + max_sl
                        logging.warning(
                            f"⚠️ 条件单止损过宽自动收紧：{old_sl:.2f} 距离 {sl_distance:.2f} > "
                            f"{max_sl:.2f} ({MAX_STOP_DISTANCE_ATR_MULT}×15minATR) "
                            f"→ 新止损 {cond['stop_loss']:.2f}"
                        )
                        self._send(
                            f"⚠️ 条件单止损过宽自动收紧：{sl_distance:.0f}点 > {max_sl:.0f}点"
                        )
                # =========================================================

            self.pm.conditional_order = cond
            # 8/27 修复: 记录创建日期，防止条件单跨日/跨周末残留
            # （8/14 实证: 周五设的条件单挂到周一才触发，价格已远离触发价 → 亏损单）
            cond['created_date'] = self.now_fn().date().isoformat()
            logging.info(f"更新条件单(已转期货价): {cond['trigger_type']} {cond['trigger_price']}")
            self.pm.save_position_state()
            self._send(
                f"📌 新条件单: {action} {cond.get('volume', '-')}手, "
                f"触发方式:{cond['trigger_type']}@{cond['trigger_price']:.2f}, "
                f"止损:{cond['stop_loss']:.2f}, 止盈:{cond['take_profit']:.2f}"
            )
            return

        if cond is None and self.pm.conditional_order is not None:
            logging.info("AI未提供新条件单，清除旧条件单")
            self._send("📌 条件单已清除（AI未提供新条件单）")
            self.pm.conditional_order = None
            self.pm.save_position_state()

        # ---------- 即时入场（无新条件单时） ----------
        if action == 'WAIT' or confidence < MIN_CONFIDENCE:
            return

        max_lots = self.sizer.get_max_lots()
        volume = decision.get('volume', 1)
        if volume > max_lots:
            volume = max_lots
            logging.warning(f"AI请求手数超过最大，调整为{max_lots}")

        if confidence >= 0.85:
            expected_volume = min(2, max(1, int(max_lots * 0.35)))
        elif confidence >= 0.75:
            expected_volume = min(2, max(1, int(max_lots * 0.25)))
        elif confidence >= 0.65:
            expected_volume = 1
        elif confidence >= 0.55:
            expected_volume = 1

        if volume < expected_volume:
            logging.warning(f"AI 输出手数({volume})低于系统期望({expected_volume})，但仍以 AI 为准")
        # ------------------------------------

        # ---------- 同向加仓 ----------
        if self.pm.position['direction'] and action_dir and self.pm.position['direction'] == action_dir:
            # ============================================================
            # P0：加仓硬性控制
            # ============================================================
            # 1. 信心门槛
            if confidence < ADD_REQUIRED_CONFIDENCE:
                logging.warning(
                    f"⚠️ 加仓被拒：信心 {confidence} < {ADD_REQUIRED_CONFIDENCE}，"
                    f"方向 {action_dir}。需要更强信号才加仓。"
                )
                self._send(
                    f"⚠️ 加仓被拒：信心{confidence} < {ADD_REQUIRED_CONFIDENCE}"
                )
                return
            # 2. 仓位上限
            if self.pm.position['volume'] >= MAX_POSITION_LOTS:
                logging.warning(f"⚠️ 加仓被拒：已达最大持仓 {MAX_POSITION_LOTS} 手")
                self._send(
                    f"⚠️ 加仓被拒：已达最大 {MAX_POSITION_LOTS} 手"
                )
                return
            # 3. 价格错开（加仓价需与首仓价差 ≥ 1.0×15minATR，防追高）
            if self.mcs.atr_15 > 0:
                cur_entry = self.pm.position['entry_price']
                cur_price = self.mds.im_quote.last_price
                if cur_price > 0:
                    if action_dir == 'LONG':
                        price_gap = cur_price - cur_entry
                    else:
                        price_gap = cur_entry - cur_price
                    min_gap = self.mcs.atr_15 * ADD_MIN_PRICE_GAP_ATR
                    if price_gap < min_gap:
                        logging.warning(
                            f"⚠️ 加仓被拒：价格错开不足 {price_gap:.2f} < {min_gap:.2f} "
                            f"({ADD_MIN_PRICE_GAP_ATR}×ATR)，防追高/追跌"
                        )
                        self._send(
                            f"⚠️ 加仓被拒：价差{price_gap:.0f} < {min_gap:.0f}点"
                        )
                        return
            # 4. 浮亏上限（防套牢加仓）
            cur_price = self.mds.im_quote.last_price
            if cur_price > 0:
                cur_entry = self.pm.position['entry_price']
                if action_dir == 'LONG':
                    unreal_pnl_pct = (cur_price - cur_entry) / cur_entry * 100
                else:
                    unreal_pnl_pct = (cur_entry - cur_price) / cur_entry * 100
                if unreal_pnl_pct < -ADD_MAX_DRAWDOWN_PCT:
                    logging.warning(
                        f"⚠️ 加仓被拒：浮亏 {unreal_pnl_pct:.2f}% < -{ADD_MAX_DRAWDOWN_PCT}%，"
                        f"禁止套牢加仓"
                    )
                    self._send(
                        f"⚠️ 加仓被拒：浮亏{unreal_pnl_pct:.2f}%"
                    )
                    return

            available_lots = max_lots - self.pm.position['volume']
            if available_lots <= 0:
                logging.warning("资金不足以加仓")
                self._send("⚠️ 加仓信号出现，但资金不足无法加仓")
                return

            if volume > available_lots:
                volume = available_lots
                logging.warning(f"加仓手数调整为最大可加: {available_lots}")

            # ========== 8/14 新增：加仓风险兜底 ==========
            # 加仓后总风险仍 ≤ 1% 权益（用当前持仓止损距离 + 新增手数计算）
            add_sl_dist = abs(self.pm.position.get('stop_loss', 0) - self.mds.im_quote.last_price)
            if add_sl_dist > 0:
                volume = self.sizer.apply_risk_scale(volume, add_sl_dist)
            if volume <= 0:
                logging.warning("加仓被风险上限拒绝（止损过宽/权益预算不足）")
                self._send(
                    f"🚫 加仓被风险上限拒绝\n"
                    f"原因: 止损 {add_sl_dist:.1f} 点 × 200元/手 超 1% 权益预算\n"
                    f"加仓已跳过"
                )
                return
            # 单日开仓次数上限（加仓路径）
            add_daily_blocked, add_daily_reason = self.daily_limiter.check()
            if add_daily_blocked:
                logging.warning(f"加仓被日次数上限拦截: {add_daily_reason}")
                self._send(
                    f"🛡️ 加仓被日次数上限拦截\n"
                    f"原因: {add_daily_reason}\n加仓已跳过"
                )
                return
            # =============================================

            limit_price = conv(decision.get('limit_price', 0))
            if limit_price <= 0:
                limit_price = None  # 市价单优先

            direction_full = 'BUY' if action == 'BUY' else 'SELL'

            # ========== 三层假突破过滤器（加仓路径，6/30 一周回测驱动） ==========
            # 7/3 10:52 + 7/7 10:35 + 7/7 13:26 的 3 笔加仓 (-4520, -6760, +5480) 均绕过 filter
            # 加仓路径调 execute_order_safe 但不经过 line ~1880 的 filter 块
            add_dir = "LONG" if action == "BUY" else "SHORT"
            add_rejections = []
            fr = self.filters.check_trend_alignment(add_dir)
            if not fr.allowed:
                add_rejections.append(fr.reason)
            est_entry = limit_price if limit_price else self.mds.im_quote.last_price
            if est_entry and est_entry > 0:
                fr = self.filters.check_session_extremes(est_entry, add_dir)
                if not fr.allowed:
                    add_rejections.append(fr.reason)
            # ========== P2 第四层过滤器（加仓路径）==========
            fr = self.filters.check_htf_bias(add_dir)
            if not fr.allowed:
                add_rejections.append(fr.reason)
            fr = self.filters.check_entry_volume()
            if not fr.allowed:
                add_rejections.append(fr.reason)
            fr = self.filters.check_entry_confirmation(add_dir)
            if not fr.allowed:
                add_rejections.append(fr.reason)
            # ================================================

            # ========== P1 修复：熔断检查（加仓路径）==========
            # 6/7 加仓绕过滤导致 -6,760。3 连亏后还在加仓 → 熔断拦截
            cb_blocked, cb_reason = self.cb.check()
            if cb_blocked:
                logging.warning(f"加仓被熔断拦截: {cb_reason}")
                self._send(
                    f"🚨 加仓被熔断拦截\n"
                    f"方向: {action} {volume}手 @ ~{est_entry:.1f}\n"
                    f"原因: {cb_reason}\n"
                    f"加仓已跳过，等待熔断解除（明日重置）"
                )
                return
            # ========== 8/14 新增：尾盘禁开仓（加仓路径）==========
            tail_blocked, tail_reason = self.tail_fn()
            if tail_blocked:
                logging.warning(f"加仓被尾盘拦截: {tail_reason}")
                self._send(
                    f"🛡️ 加仓被尾盘拦截\n原因: {tail_reason}\n加仓已跳过"
                )
                return
            # =================================================

            # ========== P2 反转豁免层（加仓路径）==========
            if add_rejections:
                vwap = self.exemptions.vwap_alignment(add_dir)
                if not vwap.allowed:
                    logging.info(f"加仓 VWAP alignment 未通过: {vwap.reason}")
                else:
                    exemptions_passed = []
                    exemptions_failed = []

                    ch = self.exemptions.trend_reversal_exempt(add_dir)
                    if ch.allowed:
                        exemptions_passed.append(f"CHoCH:{ch.reason}")
                    else:
                        exemptions_failed.append(f"CHoCH:{ch.reason}")

                    htf = self.exemptions.htf_partial_allowance(add_dir)
                    if htf.allowed:
                        exemptions_passed.append(f"HTF:{htf.reason}")
                    else:
                        exemptions_failed.append(f"HTF:{htf.reason}")

                    vcp = self.exemptions.volume_vcp_check()
                    if vcp.allowed:
                        exemptions_passed.append(f"VCP:{vcp.reason}")
                    else:
                        exemptions_failed.append(f"VCP:{vcp.reason}")

                    if len(exemptions_passed) >= 2:
                        reject_msg = " / ".join(add_rejections)
                        pass_msg = " / ".join(exemptions_passed)
                        logging.warning(
                            f"⚠️ 加仓原 filter 拒绝但反转豁免触发: {reject_msg} → 豁免通过 (反向确认: {pass_msg})"
                        )
                        self._send(
                            f"⚡ 加仓反向确认豁免通过\n"
                            f"方向: {action} {volume}手 @ ~{est_entry:.1f}\n"
                            f"原拒绝原因: {reject_msg}\n"
                            f"反向确认 ({len(exemptions_passed)}/3): {pass_msg}"
                        )
                        add_rejections = []
                    else:
                        logging.info(f"加仓 反转豁免未通过 ({len(exemptions_passed)}/3)")
            # ==================================================

            if add_rejections:
                reject_msg = " / ".join(add_rejections)
                logging.warning(f"加仓被假突破过滤器拒绝: {reject_msg}")
                self._send(
                    f"🛡️ 加仓被假突破过滤器拦截\n"
                    f"方向: {action} {volume}手 @ ~{est_entry:.1f}\n"
                    f"原因: {reject_msg}\n"
                    f"加仓已跳过，等待下次 SWING 周期"
                )
                return
            # ===================================================================

            avg_price = self.oe.execute_order_safe(
                symbol=self.mds.symbol,
                direction=direction_full,
                offset='OPEN',
                volume=volume,
                limit_price=limit_price
            )

            if avg_price is not None:
                old_vol = self.pm.position['volume']
                old_price = self.pm.position['entry_price']
                new_vol = old_vol + volume
                new_avg_price = (old_price * old_vol + avg_price * volume) / new_vol
                self.pm.position.update({
                    "volume": new_vol,
                    "entry_price": new_avg_price,
                    "last_ai_decision": decision.get('reason', '')
                })
                account = self.mds.api.get_account()
                balance = account.balance + account.position_profit if account else 0
                self.logger.log("ADD", self.mds.symbol, action_dir, volume, avg_price,
                                balance_after=balance, ai_reason=decision.get('reason', ''))
                # 8/14: 单日开仓次数计数
                self.daily_limiter.bump()
                self.pm.save_position_state()
                self._send(
                    f"IM同向加仓: {action_dir} {volume}手 @ {avg_price:.2f}, 总持仓{new_vol}手, 均价{new_avg_price:.2f}"
                )
                logging.info(f"加仓成功: {action_dir} {volume}手, 总手数{new_vol}, 均价{new_avg_price:.2f}")

                # 加仓之后可再次应用 adjust_existing，若AI提供了新的止损止盈建议
                if adjust and self.pm.position['direction']:
                    new_sl = conv(adjust.get('new_stop_loss'))
                    new_tp = conv(adjust.get('new_take_profit'))
                    if new_sl is not None or new_tp is not None:
                        if new_sl is not None:
                            self.pm.position['stop_loss'] = new_sl
                        if new_tp is not None:
                            self.pm.position['take_profit'] = new_tp
                        self.pm.save_position_state()
                        logging.info(f"加仓后应用止损/止盈调整")
            else:
                logging.error("同向加仓失败")
                self._send(f"⚠️ IM同向加仓失败: {action_dir} {volume}手")
            return   # 加仓处理完后直接返回，不再执行后续的开新仓/反向平仓逻辑

        # 反向持仓先平仓（使用统一后的方向）
        if self.pm.position['direction'] and action_dir and self.pm.position['direction'] != action_dir:
            logging.warning("存在反向持仓，先平仓再开新仓")
            self.oe.close_position("反向开仓前平仓")

        if not self.pm.position['direction']:
            limit_price = conv(decision.get('limit_price', 0))
            # 兜底：用 ask_price1/bid_price1（带滑点保护）
            if limit_price <= 0:
                if action == 'BUY':
                    ask = self.mds.im_quote.ask_price1
                    limit_price = ask if ask > 0 else self.mds.im_quote.last_price
                else:
                    bid = self.mds.im_quote.bid_price1
                    limit_price = bid if bid > 0 else self.mds.im_quote.last_price
                # 滑点保护：超过 5 倍 ATR 视为异常，强制用 last_price
                if limit_price > 0 and self.mcs.atr_15 > 0:
                    last = self.mds.im_quote.last_price
                    if last > 0 and abs(limit_price - last) > self.mcs.atr_15 * 5:
                        logging.warning(
                            f"⚠️ 盘口价异常 {limit_price} 距 last={last} 过大，"
                            f"改用 last_price"
                        )
                        limit_price = last

            # 新增：有效性检查
            if action not in ('WAIT',) and not cond:  # 既不是等待，也不是条件单（立即单）
                stop_loss = conv(decision.get('stop_loss', 0))
                take_profit = conv(decision.get('take_profit', 0))
                if not stop_loss or not take_profit or stop_loss <= 0 or take_profit <= 0:
                    logging.error("AI 为立即单提供了无效的止损/止盈，拒绝开仓")
                    self._send(
                        f"⚠️ AI 决策异常：立即单缺少止损/止盈，action={action}, sl={stop_loss}, tp={take_profit}"
                    )
                    return  # 直接跳过本次决策

                # ============================================================
                # P0：硬性最低止损距离校验（止损不能 < 0.8×5minATR）
                # < 0.8×5minATR 自动放宽到 MIN_STOP_DISTANCE_ATR_MULT×5minATR
                # (从15minATR改为5minATR，紧凑型止损，6/11案例：23点而非75点)
                # ============================================================
                cur_price = self.mds.im_quote.last_price
                if cur_price <= 0:
                    # 行情异常，跳过 P0 但记告警
                    logging.error(f"⚠️ last_price={cur_price} 异常，跳过止损距离校验")
                elif self.mcs.atr_5 <= 0:
                    logging.error(f"⚠️ atr_5={self.mcs.atr_5} 异常，跳过止损距离校验")
                else:
                    sl_distance = abs(cur_price - stop_loss)
                    # 阈值分层：< 0.8×5minATR 自动放宽；>= 0.8×5minATR 保持 AI 原值
                    auto_widen_threshold = self.mcs.atr_5 * 0.8
                    if sl_distance < auto_widen_threshold:
                        # 自动放宽到 MIN_STOP_DISTANCE_ATR_MULT × 5minATR（不拒绝，只调整）
                        min_dist = self.mcs.atr_5 * MIN_STOP_DISTANCE_ATR_MULT
                        old_sl = stop_loss
                        if action == 'BUY':
                            stop_loss = cur_price - min_dist
                        else:
                            stop_loss = cur_price + min_dist
                        logging.warning(
                            f"⚠️ 止损过紧自动放宽：{old_sl:.2f} 距离 {sl_distance:.2f} < "
                            f"{auto_widen_threshold:.2f} (0.8×5minATR) → 新止损 {stop_loss:.2f} "
                            f"距离 {min_dist:.2f} ({MIN_STOP_DISTANCE_ATR_MULT}×5minATR)"
                        )
                        self._send(
                            f"⚠️ 止损过紧自动放宽：{sl_distance:.0f}点<{auto_widen_threshold:.0f}点 → {min_dist:.0f}点"
                        )
                        # 重算盈亏比
                        sl_distance = min_dist
                    # ========== 8/14 新增：单笔止损距离硬上限 ==========
                    # 止损距离 > 3×15minATR → 自动收紧（防 AI 设超宽止损，单笔风险失控）
                    # 案例：8/13 -5080 一笔 = 254 点止损，15minATR≈50 时已是 5× 上限
                    if self.mcs.atr_15 > 0:
                        max_sl = self.mcs.atr_15 * MAX_STOP_DISTANCE_ATR_MULT
                        if sl_distance > max_sl:
                            old_sl = stop_loss
                            if action == 'BUY':
                                stop_loss = cur_price - max_sl
                            else:
                                stop_loss = cur_price + max_sl
                            logging.warning(
                                f"⚠️ 止损过宽自动收紧：{old_sl:.2f} 距离 {sl_distance:.2f} > "
                                f"{max_sl:.2f} ({MAX_STOP_DISTANCE_ATR_MULT}×15minATR) "
                                f"→ 新止损 {stop_loss:.2f} 距离 {max_sl:.2f}"
                            )
                            self._send(
                                f"⚠️ 止损过宽自动收紧：{sl_distance:.0f}点 > {max_sl:.0f}点 "
                                f"({MAX_STOP_DISTANCE_ATR_MULT}×15minATR)"
                            )
                            sl_distance = max_sl
                    # ======================================================
                    # 盈亏比校验（至少 1.2:1，避免盈亏比失衡）
                    tp_distance = abs(take_profit - cur_price)
                    if tp_distance > 0:
                        risk_reward = tp_distance / sl_distance
                        if risk_reward < 1.2:
                            logging.warning(
                                f"⚠️ 盈亏比偏低 {risk_reward:.2f} < 1.2，仍允许开仓（仅警告）"
                            )
                            # 仅警告不拦截（条件单环境下可能合理）

            volume = min(volume, self.sizer.get_max_lots())
            # ========== 8/14 新增：代码级风险兜底（AI 直接入场） ==========
            # 1. 按单笔风险 1% 权益上限 + 降档减半
            try:
                risk_sl = abs(stop_loss - self.mds.im_quote.last_price) if stop_loss > 0 else 0
            except Exception:
                risk_sl = 0
            if risk_sl > 0:
                volume = self.sizer.apply_risk_scale(volume, risk_sl)
            if volume <= 0:
                logging.warning("AI 入场被风险上限拒绝（止损过宽/权益预算不足）")
                self._send(
                    f"🚫 AI 入场被风险上限拒绝\n"
                    f"方向: {action} @ ~{limit_price:.1f}\n"
                    f"原因: 止损 {risk_sl:.1f} 点 × 200元/手 超 1% 权益预算，1 手都无法承受\n"
                    f"决策已跳过"
                )
                return
            # 2. 单日开仓次数上限
            daily_blocked, daily_reason = self.daily_limiter.check()
            if daily_blocked:
                logging.warning(f"日次数上限拦截: {daily_reason}")
                self._send(
                    f"🛡️ AI 入场被日次数上限拦截\n"
                    f"原因: {daily_reason}\n决策已跳过"
                )
                return
            # ===========================================================
            direction_full = 'BUY' if action == 'BUY' else 'SELL'

            # ========== 三层假突破过滤器（AI 直接入场路径） ==========
            ai_dir = "LONG" if action == "BUY" else "SHORT"
            filter_rejections_ai = []
            fr = self.filters.check_trend_alignment(ai_dir)
            if not fr.allowed:
                filter_rejections_ai.append(fr.reason)
            if limit_price > 0:
                fr = self.filters.check_session_extremes(limit_price, ai_dir)
                if not fr.allowed:
                    filter_rejections_ai.append(fr.reason)

            # ========== P2 第四层过滤器：HTF + Volume + 入场确认 ==========
            # 6/30 业界研究 (240k ORB 样本): HTF + Volume + 5min close confirm
            # 这三个过滤器单独每个加 3-10% win rate，组合能加 20%+
            fr = self.filters.check_htf_bias(ai_dir)
            if not fr.allowed:
                filter_rejections_ai.append(fr.reason)
            fr = self.filters.check_entry_volume()
            if not fr.allowed:
                filter_rejections_ai.append(fr.reason)
            fr = self.filters.check_entry_confirmation(ai_dir)
            if not fr.allowed:
                filter_rejections_ai.append(fr.reason)
            # ==============================================================

            # ========== P1 修复：熔断检查（AI 直接入场）==========
            # 6/7 当天已有 -4,800 累计亏，还在开新仓 → 熔断拦截
            cb_blocked, cb_reason = self.cb.check()
            if cb_blocked:
                logging.warning(f"AI 直接入场被熔断拦截: {cb_reason}")
                self._send(
                    f"🚨 AI 入场被熔断拦截\n"
                    f"方向: {action} @ ~{limit_price:.1f}\n"
                    f"原因: {cb_reason}\n"
                    f"决策已跳过，等待熔断解除（明日重置）"
                )
                return
            # ========== 8/14 新增：尾盘禁开仓（AI 直接入场）==========
            tail_blocked, tail_reason = self.tail_fn()
            if tail_blocked:
                logging.warning(f"AI 入场被尾盘拦截: {tail_reason}")
                self._send(
                    f"🛡️ AI 入场被尾盘拦截\n"
                    f"方向: {action} @ ~{limit_price:.1f}\n原因: {tail_reason}\n决策已跳过"
                )
                return
            # ======================================================

            # ========== P2 反转豁免层（7/9 V 型反转错失后加）==========
            # 7/9 14:30 后市场 V 反转但被 Filter1/HTF/Volume 全拦 7 次
            # 加 4 个 exemption: CHoCH / HTF partial / VCP volume / VWAP
            # 策略: 4 个 exemption 任意 ≥ 2 个通过 → 视为"反转确认" → 豁免原 filter
            # 应用研究:
            #   - CHoCH 1-Bar 规则 (fxnx.com): 突破 K + 下一根 K 都 close 在新方向
            #   - HTF partial (ICT): Daily 距 EMA20 < 0.5×15minATR + 60min 已反转
            #   - VCP 量价齐升 (GrandAlgo): 连续 3 根量递增 + close 持续走高
            #   - VWAP alignment (行业共识): close > VWAP + slope >= 0
            if filter_rejections_ai:
                # 先做 VWAP alignment（硬指标, 必须通过）
                vwap = self.exemptions.vwap_alignment(ai_dir)
                if not vwap.allowed:
                    logging.info(f"VWAP alignment 未通过，不豁免: {vwap.reason}")
                else:
                    # 检查 3 个 exemption
                    exemptions_passed = []
                    exemptions_failed = []

                    # 1. CHoCH 反转豁免 (替换 Filter1 60min trend)
                    ch = self.exemptions.trend_reversal_exempt(ai_dir)
                    if ch.allowed:
                        exemptions_passed.append(f"CHoCH:{ch.reason}")
                    else:
                        exemptions_failed.append(f"CHoCH:{ch.reason}")

                    # 2. HTF partial 豁免 (替换 HTF bias)
                    htf = self.exemptions.htf_partial_allowance(ai_dir)
                    if htf.allowed:
                        exemptions_passed.append(f"HTF:{htf.reason}")
                    else:
                        exemptions_failed.append(f"HTF:{htf.reason}")

                    # 3. VCP 量价齐升 (替换 Volume 阈值)
                    vcp = self.exemptions.volume_vcp_check()
                    if vcp.allowed:
                        exemptions_passed.append(f"VCP:{vcp.reason}")
                    else:
                        exemptions_failed.append(f"VCP:{vcp.reason}")

                    # 决策: 3 个 exemption 中至少 2 个通过 → 豁免
                    if len(exemptions_passed) >= 2:
                        reject_msg = " / ".join(filter_rejections_ai)
                        pass_msg = " / ".join(exemptions_passed)
                        logging.warning(
                            f"⚠️ AI 入场原 filter 拒绝但反转豁免触发: {reject_msg} → 豁免通过 (反向确认: {pass_msg})"
                        )
                        self._send(
                            f"⚡ AI 入场反向确认豁免通过\n"
                            f"方向: {action} @ ~{limit_price:.1f}\n"
                            f"原拒绝原因: {reject_msg}\n"
                            f"反向确认 ({len(exemptions_passed)}/3): {pass_msg}\n"
                            f"决策: 放行（V 型反转初期，符合 ORB retest + HTF partial 行业共识）"
                        )
                        # 清空 filter rejections 让后续开仓逻辑继续
                        filter_rejections_ai = []
                    else:
                        fail_msg = " / ".join(exemptions_failed)
                        logging.info(f"反转豁免未通过 ({len(exemptions_passed)}/3): {fail_msg}")
            # ============================================================

            if filter_rejections_ai:
                reject_msg = " / ".join(filter_rejections_ai)
                logging.warning(f"AI 直接入场被假突破过滤器拒绝: {reject_msg}")
                self._send(
                    f"🛡️ AI 入场被假突破过滤器拦截\n"
                    f"方向: {action} @ ~{limit_price:.1f}\n"
                    f"原因: {reject_msg}\n"
                    f"决策已跳过，等待下次 SWING 周期"
                )
                return
            # ================================================================

            # 加一层异常保护 + 失败必记日志
            try:
                avg_price = self.oe.execute_order_safe(
                    symbol=self.mds.symbol,
                    direction=direction_full,
                    offset='OPEN',
                    volume=volume,
                    limit_price=limit_price
                )
            except Exception as open_exc:
                logging.error(f"[开仓异常] {action} {volume}手 异常: {open_exc}", exc_info=True)
                self.logger.log("FAILED", self.mds.symbol, direction_full, volume, 0.0,
                                ai_reason=f"开仓异常: {open_exc}")
                self._send(
                    f"⚠️ 开仓异常：{action} {volume}手 错误 {open_exc}"
                )
                return

            if avg_price is None:
                # 下单失败（被拒/超时）—— 记日志 + 节流告警（5 分钟合并 1 次，避免刷屏）
                logging.error(
                    f"[开仓失败] {action} {volume}手 limit={limit_price} "
                    f"原因：下单被拒/超时（详见 execute_order_safe 内部日志）"
                )
                self.logger.log("FAILED", self.mds.symbol, direction_full, volume, 0.0,
                                ai_reason=f"下单被拒/超时 limit={limit_price}")
                # 注意：execute_order_safe 内部已有 logger.log("FAILED", ...)，但兜底再记一次
                # 节流告警：5 分钟内连续失败 ≥ 3 次才发钉钉
                import time as _time
                self._failed_order_window.append((_time.time(), action, limit_price))
                # 清理 5 分钟外的旧记录
                cutoff = _time.time() - 300
                self._failed_order_window = [
                    t for t in self._failed_order_window if t[0] >= cutoff
                ]
                if len(self._failed_order_window) >= 3:
                    actions = [a[1] for a in self._failed_order_window]
                    limits = [a[2] for a in self._failed_order_window]
                    self._send(
                        f"⚠️ 近 5 分钟内 {len(self._failed_order_window)} 次开仓失败！\n"
                        f"方向: {actions}\n"
                        f"限价: {limits}\n"
                        f"可能原因：限价排队超时 / 合约错误 / 涨跌停封板"
                    )
                    self._failed_order_window = []  # 告警后清空，避免 1 分钟内重复
                return

            if avg_price is not None:
                self.pm.position.update({
                    "direction": "LONG" if action == "BUY" else "SHORT",
                    "volume": volume,
                    "entry_price": avg_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "last_ai_decision": decision.get('reason', ''),
                    "entry_time": self.now_fn()  # 6/15 bug: 不写 entry_time → 14:55 重复 spam 600+ 条
                })
                self.pm.last_entry_time = self.now_fn()
                # 8/14: 单日开仓次数计数
                self.daily_limiter.bump()
                account = self.mds.api.get_account()
                balance = account.balance + account.position_profit if account else 0
                self.logger.log("OPEN", self.mds.symbol, self.pm.position['direction'], volume, avg_price,
                                balance_after=balance, ai_reason=decision.get('reason', ''))
                logging.info(f"开仓成功: {action} {volume}手 @ {avg_price:.2f}")
                self.pm.save_position_state()
                # 钉钉通知：带止损/止盈/盈亏比
                sl = self.pm.position.get('stop_loss', 0)
                tp = self.pm.position.get('take_profit', 0)
                sl_dist = abs(avg_price - sl)
                tp_dist = abs(tp - avg_price)
                risk_reward = tp_dist / sl_dist if sl_dist > 0 else 0
                self._send(
                    f"✅ IM开仓: {action} {volume}手 @ {avg_price:.2f}\n"
                    f"止损 {sl:.2f} (-{sl_dist:.1f}点) / 止盈 {tp:.2f} (+{tp_dist:.1f}点)\n"
                    f"盈亏比 1:{risk_reward:.2f}"
                )
            else:
                logging.error(f"开仓失败，信号丢失")
                self._send(f"IM开仓失败: {action} {volume}手 限价{limit_price:.2f}")

    # ---------- 真源 _execute_ai_cycle L5376–5433 ----------

    def execute_ai_cycle(self, mode: str) -> int:
        """
        执行一次AI决策循环
        mode: "SWING" | "SCALPING"
        返回: AI建议的下次调用间隔（秒）
        """
        self.mds.update_index_price()
        self.mds.refresh_tech_data()
        self.mcs.calculate_fut_atr()

        # ========== 8/17 修复: 熔断期间跳过 AI 决策 ==========
        # 8/17 实测: 熔断触发后 AI 全天 23 次决策仍反复给 BUY/设条件单，
        # 全部被拦截（628 次拦截刷屏），浪费 token 且制造噪音。
        # 熔断禁开新仓，无持仓时 AI 决策无意义 → 直接跳过调用。
        # 有持仓时仍调用（仅允许持仓管理/止损调整，prompt 会注入熔断状态）。
        cb_blocked, cb_reason = self.cb.check()
        if cb_blocked:
            if not self.pm.position['direction']:
                logging.warning(f"熔断期间跳过 {mode} AI 决策（空仓，无意义）: {cb_reason}")
                # 顺带清除遗留条件单，防止残留触发刷屏
                if self.pm.conditional_order:
                    self.pm.conditional_order = None
                    self.pm.save_position_state()
                    logging.warning("已清除熔断期间残留的条件单")
                return BASE_DECISION_INTERVAL if mode == "SWING" else SHORT_TERM_INTERVAL
            logging.info(f"熔断期间有持仓，继续 {mode} AI 决策（仅持仓管理）: {cb_reason}")
        # =====================================================

        if self.prompt_fn is None:
            logging.error("prompt_fn 未注入（ai_decision 模块阶段 5 落位后接线），跳过本次 AI 决策")
            return BASE_DECISION_INTERVAL if mode == "SWING" else SHORT_TERM_INTERVAL

        if mode == "SWING":
            sys_prompt, user_prompt = self.prompt_fn("SWING")
            default_interval = BASE_DECISION_INTERVAL
        else:
            sys_prompt, user_prompt = self.prompt_fn("SCALPING")
            default_interval = SHORT_TERM_INTERVAL

        logging.info(f"触发{mode} AI决策...")
        try:
            response = self.ai_chat_fn(messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ])
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                decision = json.loads(json_match.group())
                decision['_mode'] = mode
                if self.save_decision_fn is not None:
                    self.save_decision_fn(decision)
                self.execute_decision(decision)
                # AI建议的下次间隔（防御 null/非数值）
                next_interval = decision.get('next_interval_sec', default_interval)
                if not isinstance(next_interval, (int, float)):
                    next_interval = default_interval
                return max(MIN_DECISION_INTERVAL, min(int(next_interval), MAX_DECISION_INTERVAL))
            else:
                logging.warning("AI返回无有效JSON")
        except Exception as e:
            logging.error(f"AI决策失败: {e}")
        return default_interval
