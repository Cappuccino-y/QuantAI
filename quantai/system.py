"""IMTradingSystem 编排器：仅做依赖装配 + 主循环调度.

所有具体业务逻辑已下沉到各 ``quantai.*`` 模块，本类只做：
- 启动时依赖装配（DI 容器）
- 主循环 tick 调度（行情 / 条件单 / 止损止盈 / AI 决策 / 换月）
- 应急模式开关与自动复位
- 优雅退出
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time, timedelta
from typing import Optional

from tqsdk import TqApi, TqAuth, TqKq

from .ai_decision import AIDecisionEngine, DecisionContext
from .ai_logger import AIDecisionLogger
from .conditional_orders import ConditionalOrderManager
from .config import account, dingtalk, ensure_credentials, trading
from .jp_indices import JapanKoreaAnalyzer
from .logger import TradeLogger, setup_logging
from .market_data import ContractResolver, MarketDataProvider
from .models import AIData, Position, TradeEvent
from .news_manager import NewsManager
from .notifier import DingTalkNotifier
from .order_executor import OrderExecutor
from .performance import PerformanceMetrics
from .position_manager import PositionManager
from .risk_manager import RiskManager
from .rollover_manager import RolloverManager

logger = logging.getLogger(__name__)


class IMTradingSystem:
    """中证 1000 IM 股指期货 T+0 LLM 量化交易主系统."""

    def __init__(self, *, dry_run: bool = False) -> None:
        ensure_credentials(require_llm=not dry_run)
        setup_logging()
        self.dry_run = dry_run

        logger.info(
            "Bootstrapping IMTradingSystem (env=%s, use_sim=%s, dry_run=%s)",
            __import__("quantai.config", fromlist=["runtime"]).runtime.env,
            account.use_sim, dry_run,
        )

        self.api = TqApi(
            TqKq() if account.use_sim else None,
            auth=TqAuth(account.account, account.password),
        )

        self.contract_resolver = ContractResolver(self.api, trading.symbol_prefix)
        symbol = self.contract_resolver.dominant()
        self.market_data = MarketDataProvider(self.api, symbol)
        self.market_data.update_index_price()

        self.notifier = DingTalkNotifier()
        self.trade_logger = TradeLogger()
        self.position_manager = PositionManager()
        self.position_manager.load()

        self.risk_manager = RiskManager()
        self.performance = PerformanceMetrics()
        self.atr_data = AIData()

        self.order_executor = OrderExecutor(
            self.api, self.trade_logger, on_fill=self._on_fill,
        )

        self.conditional_orders = ConditionalOrderManager(
            position_manager=self.position_manager,
            order_executor=self.order_executor,
            market_data=self.market_data,
            notifier=self.notifier,
        )

        self.rollover = RolloverManager(
            api=self.api,
            market_data=self.market_data,
            contract_resolver=self.contract_resolver,
            position_manager=self.position_manager,
            order_executor=self.order_executor,
            notifier=self.notifier,
            days_threshold=trading.rollover_days_threshold,
        )

        self.news_manager = NewsManager(
            previous_trading_day_resolver=self.market_data.calendar.previous_trading_day,
        )
        self.jp_analyzer = JapanKoreaAnalyzer(self.market_data.index_fetcher)

        self.ai_engine = AIDecisionEngine(
            decision_logger=AIDecisionLogger(),
        )

        self._validate_position_state()
        self.market_data.refresh_tech_data()
        self._last_equity_update: Optional[datetime] = None
        self._closing = False

        self.news_manager.start()

    def _validate_position_state(self) -> None:
        try:
            symbol = self.market_data.symbol
            pos = self.api.get_position(symbol)
            for _ in range(3):
                self.api.wait_update(deadline=time.time() + 2)
                pos = self.api.get_position(symbol)
                if pos is not None:
                    break
            if pos.volume_long > 0:
                drift = self.position_manager.reconcile_with_broker(
                    "LONG", pos.volume_long, pos.open_price_long
                )
            elif pos.volume_short > 0:
                drift = self.position_manager.reconcile_with_broker(
                    "SHORT", pos.volume_short, pos.open_price_short
                )
            else:
                drift = self.position_manager.reconcile_with_broker(None, 0, 0.0)
            if drift:
                self.notifier.send("⚠️ 持仓状态已用云端数据纠正")
        except Exception as exc:
            logger.error("Validate position state failed: %s", exc)

    def _on_fill(
        self, symbol: str, direction: str, offset: str,
        volume: int, trade_price: float, limit_price: float,
    ) -> None:
        pos = self.position_manager.position
        msg = (
            f"✅ 成交: {direction} {offset} {symbol} {volume}手 @ {trade_price:.2f}\n"
            f"限价={limit_price or '市价'}"
        )
        if offset == "OPEN" and pos.stop_loss > 0 and pos.take_profit > 0:
            sl_dist = abs(trade_price - pos.stop_loss)
            tp_dist = abs(pos.take_profit - trade_price)
            rr = tp_dist / sl_dist if sl_dist > 0 else 0
            msg += (
                f"\n止损 {pos.stop_loss:.2f} (-{sl_dist:.1f}点)"
                f"\n止盈 {pos.take_profit:.2f} (+{tp_dist:.1f}点)"
                f"\n盈亏比 1:{rr:.2f}"
            )
        elif offset == "CLOSE" and self.position_manager.last_pnl:
            pnl = self.position_manager.last_pnl
            emoji = "🟢" if pnl > 0 else "🔴"
            msg += f"\n{emoji} 盈亏: {pnl:+.0f}元"
        logger.info(msg.replace("\n", " | "))
        self.notifier.send(msg)

    def close_position(self, reason: str, is_emergency: bool = False) -> bool:
        if self._closing:
            return False
        self._closing = True
        try:
            symbol = self.market_data.symbol
            pos = self.api.get_position(symbol)
            if pos.volume_long == 0 and pos.volume_short == 0:
                return True
            if pos.volume_long > 0:
                volume, direction_close, direction_full = pos.volume_long, "SELL", "LONG"
            elif pos.volume_short > 0:
                volume, direction_close, direction_full = pos.volume_short, "BUY", "SHORT"
            else:
                return True
            self.api.wait_update(deadline=time.time() + 2)
            quote = self.market_data.im_quote
            if direction_full == "LONG":
                limit_price = quote.bid_price1 if quote.bid_price1 > 0 else quote.last_price
            else:
                limit_price = quote.ask_price1 if quote.ask_price1 > 0 else quote.last_price

            current = self.position_manager.position
            entry_snapshot = current.entry_price
            entry_time_snapshot = current.entry_time or datetime.now()

            avg_price = self.order_executor.execute_safe(
                symbol=symbol, direction=direction_close, offset="CLOSE",
                volume=volume, limit_price=limit_price,
            )
            if avg_price is None:
                logger.error("Close failed direction=%s vol=%s", direction_full, volume)
                self.notifier.send(
                    f"⚠️ IM 平仓失败！{reason}, {direction_full} {volume}手 请立即处理"
                )
                if is_emergency:
                    time.sleep(3)
                    return self.close_position(reason, is_emergency=True)
                return False

            multiplier = trading.contract_multiplier
            pnl = (
                (avg_price - entry_snapshot) * volume * multiplier
                if direction_full == "LONG"
                else (entry_snapshot - avg_price) * volume * multiplier
            )
            self.position_manager.last_pnl = pnl

            account_data = self.api.get_account()
            balance = (
                account_data.balance + account_data.position_profit
                if account_data else 0
            )
            self.trade_logger.log_event(TradeEvent(
                event_type="CLOSE", symbol=symbol, direction=direction_full,
                volume=volume, price=avg_price, pnl=pnl,
                balance_after=balance, ai_reason=reason,
            ))
            logger.info("Close success: %s pnl=%.2f", reason, pnl)
            self.notifier.send(f"IM 平仓成功: {reason}, 盈亏 {pnl:.2f}")
            try:
                self.performance.record_trade(
                    pnl=pnl, direction=direction_full, volume=volume,
                    entry_price=entry_snapshot, exit_price=avg_price,
                    entry_time=entry_time_snapshot, exit_time=datetime.now(),
                )
                self.performance.update_equity(balance)
            except Exception as exc:
                logger.error("Record performance failed: %s", exc)
            self.position_manager.clear_position()
            return True
        finally:
            self._closing = False

    def check_stop_profit(self) -> None:
        if self._closing:
            return
        pos = self.position_manager.position
        if pos.is_empty:
            return
        price = self.market_data.im_quote.last_price
        if price <= 0:
            return
        trigger_reason: Optional[str] = None
        if pos.direction == "LONG":
            if price <= pos.stop_loss:
                trigger_reason = "止损触发"
            elif price >= pos.take_profit:
                trigger_reason = "止盈触发"
        else:
            if price >= pos.stop_loss:
                trigger_reason = "止损触发"
            elif price <= pos.take_profit:
                trigger_reason = "止盈触发"
        if not trigger_reason:
            return
        if trigger_reason == "止损触发":
            self.risk_manager.cooldown.record(pos.direction)
        success = self.close_position(trigger_reason)
        if not success:
            self.emergency_close(trigger_reason)

    def emergency_close(self, reason: str) -> None:
        logger.critical("Emergency close start: %s", reason)
        self.notifier.send(f"🚨 应急平仓启动：{reason}")
        self.risk_manager.emergency.trigger()
        while True:
            if self.close_position(reason + " (应急)", is_emergency=True):
                break
            time.sleep(3)
        self.risk_manager.emergency.reset()
        self.notifier.send("应急平仓完成，系统恢复")

    def _build_decision_context(self) -> DecisionContext:
        basis = self.market_data.get_basis_info()
        account_data = self.api.get_account()
        balance = (
            account_data.balance + account_data.position_profit
            if account_data else 0
        )
        im_price = self.market_data.im_quote.last_price
        margin_per_lot = (
            im_price * trading.contract_multiplier * trading.margin_rate
            if im_price > 0 else 0
        )
        max_lots = self.risk_manager.sizer.max_lots(balance, im_price)
        return DecisionContext(
            position=self.position_manager.position,
            atr=self.atr_data,
            basis=basis,
            balance=balance,
            margin_per_lot=margin_per_lot,
            max_lots=max_lots,
            news_text=self.news_manager.to_prompt_block(),
            tech_text=self.market_data.tech_data_text,
        )

    def analyze_market_state(self) -> str:
        if not self.market_data.is_trading_time():
            return "IDLE"
        position_empty = self.position_manager.position.is_empty
        if self.atr_data.stress_level >= trading.stress_threshold_pause and position_empty:
            return "IDLE"
        if self.atr_data.atr_15 > 0 and self.atr_data.atr_5 > 0:
            if self.atr_data.atr_5 / self.atr_data.atr_15 > trading.scalping_atr_ratio:
                return "SCALPING"
        return "SWING"

    def execute_ai_cycle(self, mode: str) -> int:
        self.market_data.update_index_price()
        self.market_data.refresh_tech_data()
        self.atr_data = self.market_data.atr.calc(trading.kline_data_length)
        ctx = self._build_decision_context()
        decision = self.ai_engine.decide(ctx, mode)
        if decision is None:
            return (
                trading.base_decision_interval
                if mode == "SWING"
                else trading.short_term_interval
            )
        from .execution_pipeline import execute_decision

        execute_decision(self, decision)
        return max(
            trading.min_decision_interval,
            min(decision.next_interval_sec, trading.max_decision_interval),
        )

    def run(self) -> None:
        logger.info("IMTradingSystem started.")
        self.order_executor.cancel_all()

        last_swing = datetime.now() - timedelta(minutes=15)
        last_scalping = datetime.now() - timedelta(minutes=5)
        ai_swing_interval = trading.base_decision_interval
        ai_scalping_interval = trading.short_term_interval

        try:
            while True:
                if self.market_data.is_trading_time():
                    self.api.wait_update()
                else:
                    self.api.wait_update(deadline=time.time() + 1)
                now = datetime.now()

                if self.risk_manager.emergency.active:
                    pos = self.api.get_position(self.market_data.symbol)
                    flat = pos.volume_long == 0 and pos.volume_short == 0
                    if self.risk_manager.emergency.should_auto_reset(flat):
                        self.risk_manager.emergency.reset()
                        self.notifier.send("⚠️ emergency_mode 自动重置（已空仓超时）")
                    self.check_stop_profit()
                    time.sleep(1)
                    continue

                self.conditional_orders.tick()
                self.check_stop_profit()
                self._maybe_update_equity(now)

                if self.market_data.is_near_close():
                    continue

                if not self.market_data.is_trading_time(now):
                    continue

                market_state = self.analyze_market_state()
                if market_state == "IDLE":
                    continue

                swing_elapsed = (now - last_swing).total_seconds()
                scalping_elapsed = (now - last_scalping).total_seconds()

                if swing_elapsed >= ai_swing_interval:
                    last_swing = now
                    ai_swing_interval = self.execute_ai_cycle("SWING")
                    continue
                if scalping_elapsed >= ai_scalping_interval and market_state == "SCALPING":
                    last_scalping = now
                    ai_scalping_interval = self.execute_ai_cycle("SCALPING")
                    last_swing = now

                self.rollover.rollover_if_needed(self.close_position)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received; shutting down.")
        finally:
            self.stop()

    def _maybe_update_equity(self, now: datetime) -> None:
        try:
            if self._last_equity_update and (now - self._last_equity_update).total_seconds() < 30:
                return
            account_data = self.api.get_account()
            if account_data:
                balance = account_data.balance + account_data.position_profit
                self.performance.update_equity(balance, now)
                self._last_equity_update = now
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self.order_executor.cancel_all()
        finally:
            self.news_manager.stop()
            try:
                self.api.close()
            except Exception:
                pass
            self.position_manager.save()
            logger.info("System gracefully stopped.")


__all__ = ["IMTradingSystem"]
