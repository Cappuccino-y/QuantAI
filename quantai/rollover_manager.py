"""合约换月：到期前 N 天自动迁移持仓到次月主力.

流程：
1. 检查 days_to_expiry <= 阈值
2. 平掉旧合约持仓（如有）
3. 计算新旧基差差，调整止损/止盈
4. 用对手价开新合约同向同手数
5. 失败则进入应急模式（仓位状态可能不一致）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RolloverManager:
    """换月自动化：平旧 + 开新 + 止损止盈基差矫正."""

    def __init__(
        self,
        api: Any,
        market_data: Any,
        contract_resolver: Any,
        position_manager: Any,
        order_executor: Any,
        notifier: Optional[Any] = None,
        days_threshold: int = 2,
    ) -> None:
        self.api = api
        self.market_data = market_data
        self.contract_resolver = contract_resolver
        self.position_manager = position_manager
        self.order_executor = order_executor
        self.notifier = notifier
        self.days_threshold = days_threshold

    def rollover_if_needed(self, close_position_func) -> bool:
        """``close_position_func(reason)`` 来自 TradingSystem，复用平仓逻辑."""
        basis = self.market_data.get_basis_info()
        if basis.days_to_expiry > self.days_threshold:
            return False
        symbol = self.market_data.symbol
        pos = self.api.get_position(symbol)
        if pos.volume_long == 0 and pos.volume_short == 0:
            return False
        new_symbol = self.contract_resolver.next_dominant(symbol)
        if not new_symbol or new_symbol == symbol:
            return False

        logger.info("Rollover start: %s -> %s", symbol, new_symbol)
        if self.notifier:
            self.notifier.send(f"⏳ 开始换月：{symbol} → {new_symbol}")

        old_direction = "LONG" if pos.volume_long > 0 else "SHORT"
        old_volume = pos.volume_long if old_direction == "LONG" else pos.volume_short
        current = self.position_manager.position
        old_sl, old_tp = current.stop_loss, current.take_profit

        if not close_position_func("换月平仓"):
            logger.error("Rollover close failed; abort.")
            if self.notifier:
                self.notifier.send("⚠️ 换月平仓失败，请手动处理")
            return False
        self.api.wait_update(deadline=time.time() + 2)

        old_quote = self.api.get_quote(symbol)
        new_quote = self.api.get_quote(new_symbol)
        old_index = self.market_data.index_price
        old_basis = (old_quote.last_price - old_index) if old_quote.last_price else 0.0
        new_basis = (new_quote.last_price - old_index) if new_quote.last_price else 0.0
        basis_shift = new_basis - old_basis

        if old_direction == "LONG":
            limit_price = new_quote.ask_price1 if new_quote.ask_price1 > 0 else new_quote.last_price
        else:
            limit_price = new_quote.bid_price1 if new_quote.bid_price1 > 0 else new_quote.last_price

        new_sl = old_sl + basis_shift
        new_tp = old_tp + basis_shift

        avg_price = self.order_executor.execute_safe(
            symbol=new_symbol,
            direction="BUY" if old_direction == "LONG" else "SELL",
            offset="OPEN",
            volume=old_volume,
            limit_price=limit_price,
            timeout=30,
        )

        if avg_price is None:
            logger.critical("Rollover open NEW failed; account left FLAT.")
            if self.notifier:
                self.notifier.send(
                    f"🚨 换月开仓失败！原 {old_direction} {old_volume}手 已平，"
                    f"新合约 {new_symbol} 开仓失败，请手动处理"
                )
            return False

        self.position_manager.update_position(
            direction=old_direction,
            volume=old_volume,
            entry_price=avg_price,
            stop_loss=new_sl,
            take_profit=new_tp,
            last_ai_decision=f"换月迁移 {symbol} -> {new_symbol}",
        )
        self.market_data.symbol = new_symbol
        logger.info("Rollover finished: %s %s lots @ %.2f", old_direction, old_volume, avg_price)
        if self.notifier:
            self.notifier.send(
                f"✅ 换月完成：{old_direction} {old_volume}手 @ {avg_price:.2f}, "
                f"新合约 {new_symbol}"
            )
        return True


__all__ = ["RolloverManager"]
