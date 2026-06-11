"""风控层：仓位 / 止损距离 / 止损 ratchet / 加仓门槛 / 冷却.

所有"风险阈值检查"集中于此，遵循 SRP；
任何方向的修改（放宽止损、加仓、止损后冷却）都走显式校验，
失败时返回可解释的 :class:`RiskDecision`，由编排器决定是否继续执行。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .config import trading
from .models import AIData, Position

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """风控判定结果."""

    allowed: bool
    reason: str = ""
    adjusted_stop_loss: Optional[float] = None
    adjusted_volume: Optional[int] = None


class StopOutCooldown:
    """止损平仓后，同向冷却期跟踪."""

    def __init__(self, cooldown_sec: Optional[int] = None) -> None:
        self.cooldown_sec = cooldown_sec or trading.stopout_cooldown_sec
        self.last_stopout_time: datetime = datetime.min
        self.last_stopout_dir: Optional[str] = None

    def record(self, direction: str) -> None:
        self.last_stopout_time = datetime.now()
        self.last_stopout_dir = direction
        logger.info("Stopout recorded direction=%s, %s sec cooldown active.",
                    direction, self.cooldown_sec)

    def check(self, action_dir: Optional[str]) -> RiskDecision:
        if action_dir is None or self.last_stopout_dir != action_dir:
            return RiskDecision(allowed=True)
        elapsed = (datetime.now() - self.last_stopout_time).total_seconds()
        if elapsed >= self.cooldown_sec:
            return RiskDecision(allowed=True)
        remain = self.cooldown_sec - elapsed
        return RiskDecision(
            allowed=False,
            reason=(
                f"止损冷却期内：{action_dir} 方向还需 {remain/60:.1f} 分钟才能再开。"
            ),
        )


class StopLossGuard:
    """止损相关的硬性约束：ratchet（不可放宽）+ 最小距离."""

    def __init__(self) -> None:
        self.required_confidence = trading.stop_relax_required_confidence
        self.min_dist_mult = trading.min_stop_distance_atr_mult
        self.min_dist_mult_cond = trading.min_stop_distance_atr_mult_cond

    def ratchet_check(
        self,
        current_position: Position,
        new_stop_loss: float,
        confidence: float,
    ) -> RiskDecision:
        if current_position.is_empty or new_stop_loss <= 0:
            return RiskDecision(allowed=True)
        cur_sl = current_position.stop_loss
        cur_dir = current_position.direction
        is_relaxing = (cur_dir == "LONG" and new_stop_loss < cur_sl) or (
            cur_dir == "SHORT" and new_stop_loss > cur_sl
        )
        if not is_relaxing:
            return RiskDecision(allowed=True)
        if confidence >= self.required_confidence:
            return RiskDecision(
                allowed=True,
                reason=f"放宽允许：confidence={confidence:.2f} >= {self.required_confidence}",
            )
        return RiskDecision(
            allowed=False,
            reason=(
                f"止损放宽被拒：{cur_dir} 新止损 {new_stop_loss:.2f} 比 {cur_sl:.2f} 更宽，"
                f"需要 confidence>={self.required_confidence}, 实际 {confidence:.2f}"
            ),
        )

    def widen_if_too_tight(
        self,
        entry_price: float,
        stop_loss: float,
        action: str,
        atr_data: AIData,
        *,
        is_conditional: bool = False,
    ) -> RiskDecision:
        if entry_price <= 0 or atr_data.atr_5 <= 0 or stop_loss <= 0:
            return RiskDecision(allowed=True)
        mult = self.min_dist_mult_cond if is_conditional else self.min_dist_mult
        threshold = atr_data.atr_5 * mult
        distance = abs(entry_price - stop_loss)
        if distance >= threshold:
            return RiskDecision(allowed=True)
        new_sl = entry_price - threshold if action == "BUY" else entry_price + threshold
        return RiskDecision(
            allowed=True,
            adjusted_stop_loss=new_sl,
            reason=(
                f"止损过紧自动放宽：原距离 {distance:.2f} < {threshold:.2f} "
                f"({mult}×5minATR)；新止损 {new_sl:.2f}"
            ),
        )


class AddPositionGuard:
    """加仓硬性控制：信心 / 仓位上限 / 价格错开 / 浮亏上限."""

    def __init__(self) -> None:
        self.required_confidence = trading.add_required_confidence
        self.min_price_gap_atr = trading.add_min_price_gap_atr
        self.max_drawdown_pct = trading.add_max_drawdown_pct
        self.max_lots = trading.max_position_lots

    def check(
        self,
        current_position: Position,
        action_dir: str,
        confidence: float,
        current_price: float,
        atr_data: AIData,
    ) -> RiskDecision:
        if current_position.is_empty or current_position.direction != action_dir:
            return RiskDecision(allowed=True)

        if confidence < self.required_confidence:
            return RiskDecision(
                allowed=False,
                reason=f"加仓被拒：信心 {confidence:.2f} < {self.required_confidence}",
            )
        if current_position.volume >= self.max_lots:
            return RiskDecision(
                allowed=False,
                reason=f"加仓被拒：已达最大持仓 {self.max_lots} 手",
            )
        if atr_data.atr_15 > 0 and current_price > 0:
            entry = current_position.entry_price
            gap = current_price - entry if action_dir == "LONG" else entry - current_price
            min_gap = atr_data.atr_15 * self.min_price_gap_atr
            if gap < min_gap:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"加仓被拒：价格错开 {gap:.2f} < {min_gap:.2f} "
                        f"({self.min_price_gap_atr}×15minATR)"
                    ),
                )
        if current_price > 0:
            entry = current_position.entry_price
            if entry > 0:
                pnl_pct = ((current_price - entry) if action_dir == "LONG"
                           else (entry - current_price)) / entry * 100
                if pnl_pct < -self.max_drawdown_pct:
                    return RiskDecision(
                        allowed=False,
                        reason=f"加仓被拒：浮亏 {pnl_pct:.2f}% < -{self.max_drawdown_pct}%",
                    )
        return RiskDecision(allowed=True)


class PositionSizer:
    """信心 → 手数 映射 + 资金/风险占比约束."""

    def __init__(self) -> None:
        self.margin_rate = trading.margin_rate
        self.contract_multiplier = trading.contract_multiplier
        self.max_capital_usage = trading.max_capital_usage
        self.max_risk_pct = trading.max_risk_per_trade_pct

    def max_lots(self, balance: float, im_price: float) -> int:
        if im_price <= 0 or balance <= 0:
            return 0
        margin_per_lot = im_price * self.contract_multiplier * self.margin_rate
        if margin_per_lot <= 0:
            return 0
        raw = int(balance // margin_per_lot)
        safe = int(balance * self.max_capital_usage // margin_per_lot)
        return min(raw, safe)

    def lots_for_confidence(self, confidence: float, max_lots: int) -> int:
        if max_lots <= 0:
            return 0
        import math

        if confidence >= 0.85:
            return max(1, min(math.ceil(max_lots * 0.42), math.floor(max_lots * 0.50)))
        if confidence >= 0.75:
            return max(1, math.ceil(max_lots * 0.32))
        if confidence >= 0.65:
            return max(1, math.ceil(max_lots * 0.22))
        if confidence >= trading.min_confidence:
            return max(1, math.ceil(max_lots * 0.12))
        return 0

    def cap_by_risk(
        self,
        volume: int,
        entry_price: float,
        stop_loss: float,
        balance: float,
    ) -> int:
        if volume <= 0 or balance <= 0:
            return 0
        loss_per_lot = abs(entry_price - stop_loss) * self.contract_multiplier
        if loss_per_lot <= 0:
            return volume
        max_loss = balance * self.max_risk_pct
        max_lots_by_risk = int(max_loss // loss_per_lot)
        return max(0, min(volume, max_lots_by_risk))


class EmergencyState:
    """应急模式：触发后暂停 AI 决策；超时空仓后自动复位."""

    def __init__(self) -> None:
        self.active: bool = False
        self.entered_at: Optional[datetime] = None
        self.reset_timeout = trading.emergency_auto_reset_sec

    def trigger(self) -> None:
        self.active = True
        self.entered_at = datetime.now()
        logger.critical("Emergency mode triggered.")

    def reset(self) -> None:
        self.active = False
        self.entered_at = None
        logger.warning("Emergency mode reset.")

    def should_auto_reset(self, position_is_empty: bool) -> bool:
        if not self.active or not self.entered_at:
            return False
        elapsed = (datetime.now() - self.entered_at).total_seconds()
        return elapsed > self.reset_timeout and position_is_empty


class RiskManager:
    """风控总控：聚合所有 guard，对外暴露统一接口."""

    def __init__(self) -> None:
        self.cooldown = StopOutCooldown()
        self.stop_loss = StopLossGuard()
        self.add_position = AddPositionGuard()
        self.sizer = PositionSizer()
        self.emergency = EmergencyState()
        self.last_stop_adjust_time: datetime = datetime.min
        self.stop_adjust_cooldown_sec = trading.stop_adjust_cooldown

    def can_adjust_stop(self) -> bool:
        elapsed = (datetime.now() - self.last_stop_adjust_time).total_seconds()
        return elapsed >= self.stop_adjust_cooldown_sec

    def mark_stop_adjusted(self) -> None:
        self.last_stop_adjust_time = datetime.now()


__all__ = [
    "RiskDecision",
    "StopOutCooldown",
    "StopLossGuard",
    "AddPositionGuard",
    "PositionSizer",
    "EmergencyState",
    "RiskManager",
]
