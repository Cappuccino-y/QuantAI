"""conditional_orders — 条件单实时检查（真源 1 个方法，design.md §4.2 conditional_orders 表）。

方法映射:
- ConditionalOrderChecker.check_conditional_order ← check_conditional_order L4944–5324
  （380 行: 触发判定 / 过期校验 / 偏差检查（方向感知 + 0.5→1.0×5minATR 容差、上限 30）
  / 三层假突破过滤器 + P2 第四层 / 熔断 / 尾盘 / 日次数 / 止损冷却 / 反转豁免 /
  反向平仓 → 同向加仓 → 新开仓 全链路）

结构差异（ARCHITECTURE.md 阶段 4 决策记录）:
- conditional_order 全局 → pm.conditional_order（PositionManager 带锁持有）
- 过滤器/豁免返回 Tuple[bool, str] → FilterResult（.allowed/.reason，阶段 3 既定）
- _check_tail_session → tail_fn 注入（SessionPlaysService.check_tail_session）
- 止损冷却 → StopOutCooldown.check(direction) → (blocked, elapsed, remaining)
- 其余依赖（行情/ATR/风控/下单/持久化）全部构造注入，无全局状态
"""
import logging
import time
from datetime import datetime
from typing import Callable, Optional

from quantai.config import STOPOUT_COOLDOWN_SEC


class ConditionalOrderChecker:
    """条件单触发检查与执行（run 主循环每周期调用）。"""

    def __init__(self, *, pm, mds, mcs, calendar, filters, exemptions,
                 sizer, daily_limiter, circuit_breaker, stopout, oe, emergency,
                 tail_fn: Callable, notifier, logger,
                 now_fn: Callable[[], datetime] = datetime.now):
        self.pm = pm                    # PositionManager
        self.mds = mds                  # MarketDataService（im_quote/symbol）
        self.mcs = mcs                  # MarketContextService（atr_5/atr_15）
        self.calendar = calendar        # TradingCalendar（is_trading_time）
        self.filters = filters          # EntryFilters
        self.exemptions = exemptions    # Exemptions
        self.sizer = sizer              # PositionSizer（get_max_lots/apply_risk_scale）
        self.daily_limiter = daily_limiter
        self.cb = circuit_breaker
        self.stopout = stopout          # StopOutCooldown
        self.oe = oe                    # OrderExecutor
        self.emergency = emergency      # EmergencyState
        self.tail_fn = tail_fn          # → SessionPlaysService.check_tail_session
        self.notifier = notifier
        self.logger = logger
        self.now_fn = now_fn

    def _send(self, msg: str) -> None:
        if self.notifier is not None:
            self.notifier.send(msg)

    # ---------- 真源 check_conditional_order L4944–5324 ----------

    def check_conditional_order(self):
        """实时检查条件单是否触发，若触发则执行开仓（带滑点保护和重试）"""
        if not self.pm.conditional_order or self.emergency.mode:
            return
        if not self.calendar.is_trading_time():
            return

        price = self.mds.im_quote.last_price
        cond = self.pm.conditional_order
        trigger_type = cond.get('trigger_type')
        trigger_price = cond.get('trigger_price')  # 已在 execute_decision 中转为期货价

        # 1. 检查触发条件
        triggered = False
        if trigger_type == 'PRICE_ABOVE' and price >= trigger_price:
            triggered = True
        elif trigger_type == 'PRICE_BELOW' and price <= trigger_price:
            triggered = True

        if not triggered:
            return

        # ========== 8/27 修复: 条件单过期校验（跨日残留防护）==========
        # 实证 8/14: 周五设的条件单周一开盘才触发，价格早已远离触发价。
        # 过期条件单一律取消，仅当天创建的条件单可执行。
        _created = cond.get('created_date')
        if _created and _created != self.now_fn().date().isoformat():
            logging.warning(
                f"⚠️ 过期条件单已取消：创建日期 {_created} 非今日，"
                f"{trigger_type} {trigger_price} 不再执行（防隔夜/周末残留）"
            )
            self._send(
                f"⏰ 过期条件单已自动取消\n"
                f"创建日期: {_created}\n"
                f"内容: {cond.get('action')} {trigger_type}@{trigger_price:.1f}"
            )
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return
        # ==========================================================

        logging.info(f"条件单触发！类型:{trigger_type}, 触发价:{trigger_price}, 现价:{price}")

        # 2. 立即获取当前对手价（买一 / 卖一），用于偏差检查
        self.mds.api.wait_update(deadline=time.time() + 2)
        cond_action = cond['action']  # "BUY" or "SELL"
        if cond_action == 'BUY':
            market_price = self.mds.im_quote.ask_price1
        else:
            market_price = self.mds.im_quote.bid_price1
        if market_price <= 0:
            market_price = price  # 兜底

        # 3. 偏差检查：对手价与触发价之差不能超过 price_tolerance
        # 6/16 bug: 硬编码 3.0 太严，12:50 顺势单 13:00 开盘跳空 4.4 点被拒
        # 修复：用 0.5×5minATR 作为动态容差（开盘跳空也是顺势单的预期效果）
        # 6/23 bug: 6/22 顺势单 SELL 条件 13:00 跳空 25 点 > 10 上限被拒，错过大行情
        # 修复：BUY 关心上滑（不利），SELL 关心下滑（不利）；同时上限 10 → 30
        # ========== P0 修复：方向感知 + 放宽上限 ==========
        if self.mcs.atr_5 > 0:
            price_tolerance = self.mcs.atr_5 * 1.0  # 0.5 → 1.0（更宽松）
        else:
            price_tolerance = 10.0  # 兜底（atr 不可用时）
        # 硬上限 30.0 点（防止极端行情）
        price_tolerance = min(price_tolerance, 30.0)

        # ========== P0 修复：方向感知（6/23 案例）==========
        # 6/23 SELL 条件触发价 8477.4，实际价 8452.4
        # 这是有利滑点（SELL 想要的价格更低）→ 旧版错误地拒绝
        # 关键洞察：BUY 和 SELL 都是"价格向上走 = 不利"
        #   BUY 触发价 8477.4，市场价 8500 → 价涨了多付 → 不利
        #   SELL 触发价 8477.4，市场价 8500 → 价涨了没按预期跌 → 不利
        #   BUY 触发价 8477.4，市场价 8452 → 价跌了少付 → 有利
        #   SELL 触发价 8477.4，市场价 8452 → 价跌了卖更高 → 有利
        # 因此统一：adverse = max(0, market - trigger)
        adverse_deviation = max(0.0, market_price - trigger_price)

        deviation = adverse_deviation
        if deviation > price_tolerance:
            logging.warning(
                f"条件单触发后滑点过大：对手价 {market_price}, 触发价 {trigger_price}, "
                f"不利偏差 {deviation:.2f} > {price_tolerance:.2f} (1.0×5minATR, 上限30)"
            )
            self._send(
                f"⚠️ 条件单触发但滑点过大（不利偏差{deviation:.2f} > {price_tolerance:.2f}），"
                f"已放弃执行：{cond_action} {cond.get('volume', '-')}手"
            )
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return

        # 3.5 三层假突破过滤器（6/29 回测驱动）
        cond_dir = "LONG" if cond_action == "BUY" else "SHORT"
        filter_rejections = []

        # 过滤器 1：多时段方向锁
        fr = self.filters.check_trend_alignment(cond_dir)
        if not fr.allowed:
            filter_rejections.append(fr.reason)

        # 过滤器 2：Session High/Low 禁区
        entry_est = market_price  # 估计入场价
        fr = self.filters.check_session_extremes(entry_est, cond_dir)
        if not fr.allowed:
            filter_rejections.append(fr.reason)

        # 过滤器 3：突破确认（影线穿刺检测）
        fr = self.filters.confirm_breakout_bar(trigger_type, trigger_price, cond_dir)
        if not fr.allowed:
            filter_rejections.append(fr.reason)

        # ========== P2 第四层过滤器（条件单）==========
        fr = self.filters.check_htf_bias(cond_dir)
        if not fr.allowed:
            filter_rejections.append(fr.reason)
        fr = self.filters.check_entry_volume(min_ratio=1.3)  # 8/14: 突破需放量确认
        if not fr.allowed:
            filter_rejections.append(fr.reason)
        fr = self.filters.check_entry_confirmation(cond_dir)
        if not fr.allowed:
            filter_rejections.append(fr.reason)
        # ==============================================

        # ========== P1 修复：熔断检查（条件单）==========
        cb_blocked, cb_reason = self.cb.check()
        if cb_blocked:
            logging.warning(f"条件单被熔断拦截: {cb_reason}")
            self._send(
                f"🚨 条件单被熔断拦截\n"
                f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                f"原因: {cb_reason}\n"
                f"条件单已自动取消"
            )
            # 8/17 修复: 熔断拦截后必须清除条件单，否则价格每次穿越触发价
            # 都会重复触发→重复拦截（8/17 实测 623 次刷屏）
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return
        # ==============================================

        # ========== 8/14 新增：尾盘禁开仓（条件单路径）==========
        tail_blocked, tail_reason = self.tail_fn()
        if tail_blocked:
            logging.warning(f"条件单被尾盘拦截: {tail_reason}")
            self._send(
                f"🛡️ 条件单被尾盘拦截\n"
                f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                f"原因: {tail_reason}\n"
                f"条件单已自动取消"
            )
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return
        # ===============================================

        # ========== 8/14 新增：单日开仓次数上限（条件单路径）==========
        daily_blocked, daily_reason = self.daily_limiter.check()
        if daily_blocked:
            logging.warning(f"条件单被日次数上限拦截: {daily_reason}")
            self._send(
                f"🛡️ 条件单被日次数上限拦截\n"
                f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                f"原因: {daily_reason}\n"
                f"条件单已自动取消"
            )
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return
        # =======================================================

        # ========== P1 修复：止损冷却检查（条件单路径，8/13 补）==========
        # AI 直接入场路径已有冷却，但条件单路径漏了 → 止损后 15 分钟内
        # 条件单仍可能触发同向开仓（8/13 案例：10:31 止损后 11:28 条件单再开 → 又亏 2640）
        cooldown_blocked, elapsed_since_stopout, remaining = self.stopout.check(cond_dir)
        if cooldown_blocked:
            logging.warning(
                f"⚠️ 条件单被止损冷却拦截：{cond_dir} 方向 {elapsed_since_stopout/60:.0f} 分钟前止损，"
                f"还需等待 {remaining/60:.1f} 分钟。条件单已取消。"
            )
            self._send(
                f"🛡️ 条件单被止损冷却拦截\n"
                f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                f"原因: {cond_dir} 方向 {elapsed_since_stopout/60:.0f} 分钟前刚止损，"
                f"冷却期 {STOPOUT_COOLDOWN_SEC//60} 分钟内禁止同向再开\n"
                f"条件单已自动取消"
            )
            # 清除条件单防止重复触发
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return
        # ==============================================================

        # ========== P2 反转豁免层（条件单路径）==========
        if filter_rejections:
            vwap = self.exemptions.vwap_alignment(cond_dir)
            if not vwap.allowed:
                logging.info(f"条件单 VWAP alignment 未通过: {vwap.reason}")
            else:
                exemptions_passed = []
                exemptions_failed = []

                ch = self.exemptions.trend_reversal_exempt(cond_dir)
                if ch.allowed:
                    exemptions_passed.append(f"CHoCH:{ch.reason}")
                else:
                    exemptions_failed.append(f"CHoCH:{ch.reason}")

                htf = self.exemptions.htf_partial_allowance(cond_dir)
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
                    reject_msg = " / ".join(filter_rejections)
                    pass_msg = " / ".join(exemptions_passed)
                    logging.warning(
                        f"⚠️ 条件单原 filter 拒绝但反转豁免触发: {reject_msg} → 豁免通过 (反向确认: {pass_msg})"
                    )
                    self._send(
                        f"⚡ 条件单反向确认豁免通过\n"
                        f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                        f"原拒绝原因: {reject_msg}\n"
                        f"反向确认 ({len(exemptions_passed)}/3): {pass_msg}"
                    )
                    filter_rejections = []
                else:
                    logging.info(f"条件单 反转豁免未通过 ({len(exemptions_passed)}/3)")
        # ===============================================

        if filter_rejections:
            reject_msg = " / ".join(filter_rejections)
            logging.warning(f"条件单被假突破过滤器拒绝: {reject_msg}")
            self._send(
                f"🛡️ 条件单被假突破过滤器拦截\n"
                f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                f"原因: {reject_msg}\n"
                f"条件单已自动取消"
            )
            # 8/26 修复: 拒绝路径必须真正清除条件单！
            # 此前只 return 未清除 → 条件单残留 → 价格在触发价附近反复穿越
            # 反复触发 → 8/26 实测 824 次触发/钉钉刷屏
            self.pm.conditional_order = None
            self.pm.save_position_state()
            return

        # 4. 清除条件单，防止重复触发
        self.pm.conditional_order = None
        self.pm.save_position_state()

        # 5. 反向持仓先平仓
        if self.pm.position['direction'] and self.pm.position['direction'] != cond_dir:
            logging.warning("存在反向持仓，条件单开仓前先平仓")
            self.oe.close_position("条件单开仓前平反向仓位")

        # 6. 同向持仓 → 加仓（使用对手价 + 重试）
        if self.pm.position['direction'] == cond_dir:
            volume = min(cond.get('volume', 1), self.sizer.get_max_lots())
            # 8/14: 条件单加仓风险兜底（用条件单止损距离）
            cond_sl_dist = abs(cond.get('stop_loss', 0) - trigger_price)
            if cond_sl_dist > 0:
                volume = self.sizer.apply_risk_scale(volume, cond_sl_dist)
            if volume <= 0:
                logging.warning("条件单加仓被风险上限拒绝（止损过宽/权益预算不足）")
                self._send(
                    f"🚫 条件单加仓被风险上限拒绝\n"
                    f"原因: 止损 {cond_sl_dist:.1f} 点 × 200元/手 超 1% 权益预算\n"
                    f"加仓已跳过"
                )
                return
            available_lots = self.sizer.get_max_lots() - self.pm.position['volume']
            if available_lots <= 0:
                logging.warning("资金不足，条件单加仓失败")
                self._send("⚠️ 条件单触发但资金不足无法加仓")
                return
            if volume > available_lots:
                volume = available_lots

            avg_price = self.oe.execute_market_order_with_retry(
                symbol=self.mds.symbol,
                direction='BUY' if cond_action == 'BUY' else 'SELL',
                offset='OPEN',
                volume=volume,
                base_market_price=market_price,  # 初次触发时的对手价，用于重试偏差控制
                tolerance=price_tolerance
            )

            if avg_price is not None:
                old_vol = self.pm.position['volume']
                old_price = self.pm.position['entry_price']
                new_vol = old_vol + volume
                new_avg_price = (old_price * old_vol + avg_price * volume) / new_vol
                self.pm.position.update({
                    "volume": new_vol,
                    "entry_price": new_avg_price,
                    "last_ai_decision": f"条件单加仓: {cond.get('reason', '')}"
                })
                account = self.mds.api.get_account()
                balance = account.balance + account.position_profit if account else 0
                self.logger.log("ADD", self.mds.symbol, cond_dir, volume, avg_price,
                                balance_after=balance, ai_reason=cond.get('reason', ''))
                # 8/14: 单日开仓次数计数
                self.daily_limiter.bump()
                self.pm.save_position_state()
                self._send(
                    f"条件单同向加仓: {cond_action} {volume}手 @ {avg_price:.2f}, 总持仓{new_vol}手"
                )
                logging.info(f"条件单加仓成功: {cond_action} {volume}手, 总{new_vol}手, 均价{new_avg_price:.2f}")
            else:
                logging.error("条件单触发后加仓失败！")
                self._send(f"⚠️ 条件单触发但加仓失败: {cond_action} {volume}手")
            return

        # 7. 新开仓（使用对手价 + 重试）
        volume = min(cond.get('volume', 1), self.sizer.get_max_lots())
        # 8/14: 条件单新开仓风险兜底（用条件单止损距离）
        cond_sl_dist = abs(cond.get('stop_loss', 0) - trigger_price)
        if cond_sl_dist > 0:
            volume = self.sizer.apply_risk_scale(volume, cond_sl_dist)
        if volume <= 0:
            logging.warning("条件单被风险上限拒绝（止损过宽/权益预算不足）")
            self._send(
                f"🚫 条件单被风险上限拒绝\n"
                f"方向: {cond_action} / 触发价: {trigger_price:.1f}\n"
                f"原因: 止损 {cond_sl_dist:.1f} 点 × 200元/手 超 1% 权益预算，1 手都无法承受\n"
                f"条件单已取消"
            )
            return
        avg_price = self.oe.execute_market_order_with_retry(
            symbol=self.mds.symbol,
            direction='BUY' if cond_action == 'BUY' else 'SELL',
            offset='OPEN',
            volume=volume,
            base_market_price=market_price,
            tolerance=price_tolerance
        )

        if avg_price is not None:
            self.pm.position.update({
                "direction": "LONG" if cond_action == 'BUY' else "SHORT",
                "volume": volume,
                "entry_price": avg_price,
                "stop_loss": cond['stop_loss'],
                "take_profit": cond['take_profit'],
                "last_ai_decision": f"条件单触发: {cond.get('reason', '')}",
                # ========== P0 修复：条件单触发开仓也要写 entry_time ==========
                # 6/15 修复 line 1401 常规开仓路径时漏了这条路径
                # 影响：14:55 _evaluate_overnight_holding 检测不到 entry_time
                #      → 强制过夜，跳过 AI 过夜评估，6/25 已触发此 bug
                "entry_time": self.now_fn().strftime('%Y-%m-%d %H:%M:%S'),
            })
            # 8/27 修复: 同步 last_entry_time —— 平仓绩效记录用的是它，
            # 此前未同步导致 26 笔中 15 笔 entry_time=0001-01-01
            self.pm.last_entry_time = self.now_fn()
            account = self.mds.api.get_account()
            balance = account.balance + account.position_profit if account else 0
            self.logger.log("OPEN", self.mds.symbol, self.pm.position['direction'], volume, avg_price,
                            balance_after=balance, ai_reason=cond.get('reason', ''))
            # 8/14: 单日开仓次数计数
            self.daily_limiter.bump()
            self.pm.save_position_state()
            self._send(
                f"条件单入场: {cond_action} {volume}手 @ {avg_price:.2f}, 止损{cond['stop_loss']:.2f}"
            )
            logging.info(f"条件单开仓成功: {cond_action} {volume}手 @ {avg_price:.2f}")
        else:
            logging.error("条件单触发后开仓失败！")
            self._send(
                f"⚠️ 条件单触发但开仓失败: {cond_action} {volume}手, 请手动处理"
            )
