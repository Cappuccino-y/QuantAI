"""决策→执行管道：把 AIDecision 翻译为下单序列.

抽出此文件以避免 system.py 过大；
核心顺序与 ``autotrade_fix.py::execute_decision`` 完全一致：

1. 止损冷却期检查
2. adjust_existing 止损 ratchet
3. 新条件单 / 清除旧条件单
4. WAIT / 信心不足 → 直接返回
5. 同向加仓校验链（信心 / 上限 / 价格错开 / 浮亏）
6. 反向先平仓
7. 新开仓 + 最小止损距离自动放宽
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from .models import AIDecision, ConditionalOrder, TradeEvent

if TYPE_CHECKING:
    from .system import IMTradingSystem

logger = logging.getLogger(__name__)


def execute_decision(system: "IMTradingSystem", decision: AIDecision) -> None:
    action = decision.action
    confidence = decision.confidence
    action_dir = "LONG" if action == "BUY" else ("SHORT" if action == "SELL" else None)
    md = system.market_data
    pm = system.position_manager
    rm = system.risk_manager
    notifier = system.notifier

    def conv(p: Optional[float]) -> Optional[float]:
        if p is None or p <= 0:
            return p
        return md.index_to_future_price(p)

    cooldown_res = rm.cooldown.check(action_dir)
    if not cooldown_res.allowed:
        logger.warning(cooldown_res.reason)
        position = pm.position
        adjust = decision.adjust_existing
        if not adjust:
            return
        if position.direction == action_dir:
            logger.warning("Cooldown active; skip add-position request.")
            return

    adjust = decision.adjust_existing
    position = pm.position
    if adjust and not position.is_empty:
        new_sl_raw = adjust.get("new_stop_loss")
        if new_sl_raw is not None and new_sl_raw > 0:
            new_sl = conv(new_sl_raw)
            ratchet = rm.stop_loss.ratchet_check(position, new_sl, confidence)
            if not ratchet.allowed:
                logger.warning(ratchet.reason)
                notifier.send(f"⚠️ 止损放宽被拒：confidence={confidence:.2f}")
                adjust = {**adjust, "new_stop_loss": None}

    if adjust and not pm.position.is_empty:
        if rm.can_adjust_stop():
            changed = False
            new_sl = conv(adjust.get("new_stop_loss"))
            new_tp = conv(adjust.get("new_take_profit"))
            reason = decision.reason
            if new_sl is not None:
                pm.update_position(stop_loss=new_sl)
                system.trade_logger.log_event(TradeEvent(
                    event_type="ADJUST_STOP", symbol=md.symbol,
                    direction=pm.position.direction or "", volume=pm.position.volume,
                    price=new_sl, ai_reason=reason,
                ))
                logger.info("Stop-loss updated to %s", new_sl)
                changed = True
            if new_tp is not None:
                pm.update_position(take_profit=new_tp)
                system.trade_logger.log_event(TradeEvent(
                    event_type="ADJUST_PROFIT", symbol=md.symbol,
                    direction=pm.position.direction or "", volume=pm.position.volume,
                    price=new_tp, ai_reason=reason,
                ))
                logger.info("Take-profit updated to %s", new_tp)
                changed = True
            if changed:
                rm.mark_stop_adjusted()
                notifier.send(
                    f"⚙️ 日间调整: SL={new_sl or '不变'} TP={new_tp or '不变'} 理由:{reason}"
                )
        else:
            logger.info("Stop-loss in cooldown; skip adjustment.")

    cond_raw = decision.conditional_entry
    if cond_raw and action != "WAIT" and confidence >= md_min_confidence():
        cond = ConditionalOrder(
            trigger_type=cond_raw.trigger_type,
            trigger_price=conv(cond_raw.trigger_price) or 0,
            stop_loss=conv(cond_raw.stop_loss) or 0,
            take_profit=conv(cond_raw.take_profit) or 0,
            action=action,
            volume=decision.volume or 1,
            limit_price=conv(cond_raw.limit_price) or 0,
            reason=decision.reason,
            source="ai",
        )
        widen = rm.stop_loss.widen_if_too_tight(
            cond.trigger_price, cond.stop_loss, action,
            system.atr_data, is_conditional=True,
        )
        if widen.adjusted_stop_loss is not None:
            logger.warning(widen.reason)
            cond.stop_loss = widen.adjusted_stop_loss
            notifier.send(f"⚠️ 条件单止损自动放宽至 {cond.stop_loss:.2f}")
        system.conditional_orders.set(cond)
        return

    if cond_raw is None and pm.conditional_order is not None:
        system.conditional_orders.clear("AI 未提供新条件单")

    if action == "WAIT" or confidence < md_min_confidence():
        return

    quote = md.im_quote
    account_data = system.api.get_account()
    balance = (account_data.balance + account_data.position_profit) if account_data else 0
    max_lots = rm.sizer.max_lots(balance, quote.last_price)
    volume = min(decision.volume, max_lots) if max_lots > 0 else 0
    if volume <= 0:
        logger.warning("max_lots is 0; cannot open position.")
        return

    position = pm.position
    if not position.is_empty and action_dir and position.direction == action_dir:
        guard = rm.add_position.check(
            position, action_dir, confidence, quote.last_price, system.atr_data,
        )
        if not guard.allowed:
            logger.warning(guard.reason)
            notifier.send(f"⚠️ {guard.reason}")
            return
        available = max_lots - position.volume
        if available <= 0:
            notifier.send("⚠️ 加仓信号出现，但资金不足无法加仓")
            return
        volume = min(volume, available)
        avg_price = system.order_executor.execute_safe(
            symbol=md.symbol, direction=action, offset="OPEN",
            volume=volume, limit_price=None,
        )
        if avg_price is None:
            notifier.send(f"⚠️ IM 同向加仓失败：{action_dir} {volume}手")
            return
        old_vol, old_price = position.volume, position.entry_price
        new_vol = old_vol + volume
        new_avg = (old_price * old_vol + avg_price * volume) / new_vol
        pm.update_position(volume=new_vol, entry_price=new_avg, last_ai_decision=decision.reason)
        system.trade_logger.log_event(TradeEvent(
            event_type="ADD", symbol=md.symbol, direction=action_dir,
            volume=volume, price=avg_price, balance_after=balance,
            ai_reason=decision.reason,
        ))
        notifier.send(
            f"IM 同向加仓：{action_dir} {volume}手 @ {avg_price:.2f}, 均价 {new_avg:.2f}"
        )
        return

    if not position.is_empty and action_dir and position.direction != action_dir:
        logger.warning("Reverse position present; close before open new direction.")
        system.close_position("反向开仓前平仓")

    if pm.position.is_empty:
        stop_loss = conv(decision.stop_loss) or 0
        take_profit = conv(decision.take_profit) or 0
        if stop_loss <= 0 or take_profit <= 0:
            logger.error("Immediate order missing valid SL/TP; reject.")
            notifier.send(
                f"⚠️ AI 决策异常：立即单缺失 SL/TP, action={action}"
            )
            return
        widen = rm.stop_loss.widen_if_too_tight(
            quote.last_price, stop_loss, action, system.atr_data, is_conditional=False,
        )
        if widen.adjusted_stop_loss is not None:
            logger.warning(widen.reason)
            stop_loss = widen.adjusted_stop_loss
            notifier.send(f"⚠️ 止损过紧自动放宽至 {stop_loss:.2f}")

        volume = rm.sizer.cap_by_risk(volume, quote.last_price, stop_loss, balance)
        if volume <= 0:
            logger.warning("Volume capped to 0 by risk cap; skip.")
            return

        avg_price = system.order_executor.execute_safe(
            symbol=md.symbol, direction=action, offset="OPEN",
            volume=volume, limit_price=None,
        )
        if avg_price is None:
            notifier.send(f"⚠️ IM 开仓失败：{action} {volume}手")
            return
        pm.update_position(
            direction="LONG" if action == "BUY" else "SHORT",
            volume=volume,
            entry_price=avg_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            last_ai_decision=decision.reason,
            entry_time=datetime.now(),
        )
        system.trade_logger.log_event(TradeEvent(
            event_type="OPEN", symbol=md.symbol,
            direction=pm.position.direction or "", volume=volume,
            price=avg_price, balance_after=balance, ai_reason=decision.reason,
        ))
        notifier.send(f"IM 开仓: {action} {volume}手 @ {avg_price:.2f}")


def md_min_confidence() -> float:
    from .config import trading
    return trading.min_confidence


__all__ = ["execute_decision"]
