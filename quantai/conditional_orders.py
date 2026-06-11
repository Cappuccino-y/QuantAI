"""条件单管理：AI 设单 → tick 监控触发 → 反向/同向智能处理.

条件单逻辑独立于 ``OrderExecutor``：
- ``ConditionalOrderManager`` 仅负责"是否触发"与"触发后调度"
- 实际下单委托给注入的 :class:`OrderExecutor`
- 触发后立即清除，避免重复
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .models import ConditionalOrder

logger = logging.getLogger(__name__)


class ConditionalOrderManager:
    """条件单中央调度器."""

    def __init__(
        self,
        position_manager: Any,
        order_executor: Any,
        market_data: Any,
        notifier: Optional[Any] = None,
        price_tolerance: float = 3.0,
    ) -> None:
        self.position_manager = position_manager
        self.order_executor = order_executor
        self.market_data = market_data
        self.notifier = notifier
        self.price_tolerance = price_tolerance

    def set(self, cond: Optional[ConditionalOrder]) -> None:
        self.position_manager.set_conditional(cond)
        if cond and self.notifier:
            self.notifier.send(
                f"📌 新条件单：{cond.action} {cond.volume}手, "
                f"{cond.trigger_type}@{cond.trigger_price:.2f}, "
                f"SL {cond.stop_loss:.2f} / TP {cond.take_profit:.2f}"
            )

    def clear(self, reason: str = "") -> None:
        if self.position_manager.conditional_order:
            self.position_manager.set_conditional(None)
            if self.notifier:
                self.notifier.send(f"📌 条件单已清除：{reason}" if reason else "📌 条件单已清除")

    def tick(self) -> None:
        """每个行情 tick 调用：判断是否触发；触发则下单."""
        cond = self.position_manager.conditional_order
        if cond is None:
            return
        if not self.market_data.is_trading_time():
            return

        price = self.market_data.im_quote.last_price
        triggered = (
            (cond.trigger_type == "PRICE_ABOVE" and price >= cond.trigger_price)
            or (cond.trigger_type == "PRICE_BELOW" and price <= cond.trigger_price)
        )
        if not triggered:
            return

        logger.info(
            "Conditional order triggered: %s @ %.2f (current=%.2f)",
            cond.trigger_type, cond.trigger_price, price,
        )
        market_price = self._counter_price(cond.action)
        deviation = abs(market_price - cond.trigger_price)
        if deviation > self.price_tolerance:
            logger.warning(
                "Triggered but slippage too large (%.2f > %.2f); abandon.",
                deviation, self.price_tolerance,
            )
            if self.notifier:
                self.notifier.send(
                    f"⚠️ 条件单触发但滑点过大（偏差 {deviation:.2f}），已放弃执行"
                )
            self.clear("滑点过大")
            return

        self.clear("已触发")
        self._execute(cond, market_price)

    def _counter_price(self, action: str) -> float:
        quote = self.market_data.im_quote
        if action == "BUY":
            return quote.ask_price1 if quote.ask_price1 > 0 else quote.last_price
        return quote.bid_price1 if quote.bid_price1 > 0 else quote.last_price

    def _execute(self, cond: ConditionalOrder, market_price: float) -> None:
        position = self.position_manager.position
        target_dir = "LONG" if cond.action == "BUY" else "SHORT"

        if position.direction and position.direction != target_dir:
            logger.warning("Reverse position present; close first before conditional fill.")

        avg_price = self.order_executor.execute_market_with_retry(
            symbol=self.market_data.symbol,
            direction=cond.action,
            offset="OPEN",
            volume=cond.volume,
            base_market_price=market_price,
            tolerance=self.price_tolerance,
        )
        if avg_price is None:
            logger.error("Conditional order fill failed after retries.")
            if self.notifier:
                self.notifier.send(
                    f"⚠️ 条件单触发但开仓失败：{cond.action} {cond.volume}手"
                )
            return

        if position.direction == target_dir:
            new_vol = position.volume + cond.volume
            new_avg = (position.entry_price * position.volume + avg_price * cond.volume) / new_vol
            self.position_manager.update_position(
                volume=new_vol, entry_price=new_avg,
                last_ai_decision=f"条件单加仓 source={cond.source}",
            )
            if self.notifier:
                self.notifier.send(
                    f"条件单同向加仓：{cond.action} {cond.volume}手 @ {avg_price:.2f}，"
                    f"总持仓 {new_vol} 手"
                )
        else:
            self.position_manager.update_position(
                direction=target_dir,
                volume=cond.volume,
                entry_price=avg_price,
                stop_loss=cond.stop_loss,
                take_profit=cond.take_profit,
                last_ai_decision=f"条件单触发 source={cond.source}",
            )
            if self.notifier:
                self.notifier.send(
                    f"条件单入场：{cond.action} {cond.volume}手 @ {avg_price:.2f}，"
                    f"SL {cond.stop_loss:.2f} / TP {cond.take_profit:.2f}"
                )


__all__ = ["ConditionalOrderManager"]
