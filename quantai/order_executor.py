"""订单执行层：下单 / 撤单 / 超时控制 / 对手价追价重试.

设计目标：
- 单一入口 :py:meth:`OrderExecutor.execute_safe`，承接所有非应急下单
- 价格防呆（限价 vs 昨结 ±50%）
- 超时 30s 自动撤单
- 失败必记 TradeLogger（FAILED 事件）
- 成交后回调 ``on_fill`` 触发钉钉通知 / 持仓更新
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from .logger import TradeLogger

logger = logging.getLogger(__name__)

FillCallback = Callable[[str, str, str, int, float, float], None]


class OrderExecutor:
    """安全下单器：组合"诊断 + 防呆 + 超时撤单 + 失败日志"."""

    def __init__(
        self,
        api: Any,
        trade_logger: Optional[TradeLogger] = None,
        on_fill: Optional[FillCallback] = None,
    ) -> None:
        self.api = api
        self.trade_logger = trade_logger or TradeLogger()
        self.on_fill = on_fill
        self._orders: list = []
        self._orders_lock = threading.Lock()

    def execute_safe(
        self,
        symbol: str,
        direction: str,
        offset: str,
        volume: int,
        limit_price: Optional[float],
        timeout: int = 30,
    ) -> Optional[float]:
        try:
            quote = self.api.get_quote(symbol)
            self.api.wait_update(deadline=time.time() + 1)
            logger.info(
                "[ORDER] %s %s %s %s @ %s | last=%s ask1=%s bid1=%s settle=%s up=%s low=%s",
                symbol, direction, offset, volume, limit_price,
                quote.last_price, quote.ask_price1, quote.bid_price1,
                quote.settlement, quote.upper_limit, quote.lower_limit,
            )

            order_price = limit_price
            if limit_price and quote.settlement and quote.settlement > 0:
                if limit_price < quote.settlement * 0.5 or limit_price > quote.settlement * 2.0:
                    logger.error(
                        "[ORDER REJECT-LOCAL] limit=%s far from settle=%s; switch to market.",
                        limit_price, quote.settlement,
                    )
                    order_price = None

            order = self.api.insert_order(symbol, direction, offset, volume, order_price)
            with self._orders_lock:
                self._orders.append(order)
            self.api.wait_update(deadline=time.time() + 2)

            start = time.time()
            while True:
                self.api.wait_update(deadline=time.time() + 2)
                if order.is_error or order.status == "REJECTED":
                    logger.error("Order rejected: %s", order.last_msg)
                    self.trade_logger.log(
                        "FAILED", symbol, direction, volume, 0.0,
                        ai_reason=f"下单失败: {order.last_msg}",
                    )
                    self._drop(order)
                    return None
                if order.status == "FINISHED":
                    self._drop(order)
                    if order.volume_left == 0:
                        trade_price = order.trade_price
                        if self.on_fill:
                            try:
                                self.on_fill(symbol, direction, offset, volume,
                                             trade_price, limit_price or 0.0)
                            except Exception as cb_exc:
                                logger.warning("on_fill callback failed: %s", cb_exc)
                        return trade_price
                    logger.warning("Partial fill: %s lots left.", order.volume_left)
                    return None
                if time.time() - start > timeout:
                    logger.error("Order timeout; cancelling.")
                    self.api.cancel_order(order)
                    self.api.wait_update(deadline=time.time() + 2)
                    self._drop(order)
                    return None
        except Exception as exc:
            logger.error("Order exception: %s", exc, exc_info=True)
            return None

    def execute_market_with_retry(
        self,
        symbol: str,
        direction: str,
        offset: str,
        volume: int,
        max_retries: int = 3,
        base_market_price: Optional[float] = None,
        tolerance: float = 2.0,
    ) -> Optional[float]:
        """对手价追价，最多重试 max_retries 次；偏差超阈值则中止."""
        quote = self.api.get_quote(symbol)
        for attempt in range(max_retries):
            self.api.wait_update(deadline=time.time() + 2)
            ask, bid, last = quote.ask_price1, quote.bid_price1, quote.last_price
            if direction == "BUY":
                price = ask if ask > 0 else last
                current_market = ask if ask > 0 else last
            else:
                price = bid if bid > 0 else last
                current_market = bid if bid > 0 else last
            if price <= 0:
                logger.error("No valid counter-price; abort.")
                return None
            if base_market_price is not None and attempt > 0:
                if abs(current_market - base_market_price) > tolerance:
                    logger.warning(
                        "Retry counter-price drift too large (%.2f > %.2f); stop chasing.",
                        abs(current_market - base_market_price), tolerance,
                    )
                    return None
            logger.info("Retry attempt %d: %s %s lots @ %.2f", attempt + 1, direction, volume, price)
            filled = self.execute_safe(symbol, direction, offset, volume, price, timeout=5)
            if filled is not None:
                return filled
        return None

    def cancel_all(self) -> int:
        with self._orders_lock:
            alive = [o for o in self._orders if o.status == "ALIVE"]
        if not alive:
            logger.info("No alive orders to cancel.")
            return 0
        for order in alive:
            try:
                logger.info("Cancel order %s", order.order_id)
                self.api.cancel_order(order)
            except Exception as exc:
                logger.warning("Cancel %s failed: %s", order.order_id, exc)
        self.api.wait_update(deadline=time.time() + 2)
        with self._orders_lock:
            self._orders = [o for o in self._orders if o.status == "ALIVE"]
        logger.info("Cancelled %d orders.", len(alive))
        return len(alive)

    def _drop(self, order) -> None:
        with self._orders_lock:
            if order in self._orders:
                self._orders.remove(order)


__all__ = ["OrderExecutor", "FillCallback"]
