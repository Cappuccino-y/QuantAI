"""风控模块关键路径单测."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from quantai.models import AIData, Position
from quantai.risk_manager import (
    AddPositionGuard,
    EmergencyState,
    PositionSizer,
    RiskManager,
    StopLossGuard,
    StopOutCooldown,
)


class TestStopOutCooldown:
    def test_no_block_when_no_prior_stopout(self) -> None:
        cd = StopOutCooldown(cooldown_sec=900)
        assert cd.check("LONG").allowed

    def test_block_same_direction_within_window(self) -> None:
        cd = StopOutCooldown(cooldown_sec=900)
        cd.record("LONG")
        res = cd.check("LONG")
        assert not res.allowed
        assert "止损冷却期" in res.reason

    def test_allow_opposite_direction(self) -> None:
        cd = StopOutCooldown(cooldown_sec=900)
        cd.record("LONG")
        assert cd.check("SHORT").allowed

    def test_unblock_after_cooldown_elapsed(self) -> None:
        cd = StopOutCooldown(cooldown_sec=1)
        cd.record("LONG")
        time.sleep(1.1)
        assert cd.check("LONG").allowed


class TestStopLossGuard:
    def test_no_change_allowed(self) -> None:
        g = StopLossGuard()
        pos = Position(direction="LONG", volume=1,
                       entry_price=6000, stop_loss=5980)
        res = g.ratchet_check(pos, new_stop_loss=5980, confidence=0.4)
        assert res.allowed

    def test_long_tighten_allowed_any_confidence(self) -> None:
        g = StopLossGuard()
        pos = Position(direction="LONG", volume=1,
                       entry_price=6000, stop_loss=5980)
        res = g.ratchet_check(pos, new_stop_loss=5990, confidence=0.40)
        assert res.allowed

    def test_long_widen_blocked_with_low_confidence(self) -> None:
        g = StopLossGuard()
        pos = Position(direction="LONG", volume=1,
                       entry_price=6000, stop_loss=5980)
        res = g.ratchet_check(pos, new_stop_loss=5950, confidence=0.60)
        assert not res.allowed

    def test_long_widen_allowed_with_high_confidence(self) -> None:
        g = StopLossGuard()
        pos = Position(direction="LONG", volume=1,
                       entry_price=6000, stop_loss=5980)
        res = g.ratchet_check(pos, new_stop_loss=5950, confidence=0.80)
        assert res.allowed

    def test_short_widen_direction(self) -> None:
        g = StopLossGuard()
        pos = Position(direction="SHORT", volume=1,
                       entry_price=6000, stop_loss=6020)
        res = g.ratchet_check(pos, new_stop_loss=6050, confidence=0.60)
        assert not res.allowed

    def test_widen_too_tight_buy(self) -> None:
        g = StopLossGuard()
        atr = AIData(atr_5=20, atr_15=40, atr_60=30, stress_level=1.0)
        res = g.widen_if_too_tight(
            entry_price=6000, stop_loss=5995, action="BUY", atr_data=atr,
        )
        assert res.allowed
        assert res.adjusted_stop_loss is not None
        assert res.adjusted_stop_loss < 6000

    def test_widen_too_tight_sell(self) -> None:
        g = StopLossGuard()
        atr = AIData(atr_5=20, atr_15=40, atr_60=30, stress_level=1.0)
        res = g.widen_if_too_tight(
            entry_price=6000, stop_loss=6005, action="SELL", atr_data=atr,
        )
        assert res.adjusted_stop_loss is not None
        assert res.adjusted_stop_loss > 6000


class TestAddPositionGuard:
    def _atr(self) -> AIData:
        return AIData(atr_5=20, atr_15=40, atr_60=30, stress_level=1.0)

    def test_low_confidence_blocked(self) -> None:
        g = AddPositionGuard()
        pos = Position(direction="LONG", volume=1, entry_price=6000)
        res = g.check(pos, "LONG", confidence=0.70,
                      current_price=6050, atr_data=self._atr())
        assert not res.allowed
        assert "信心" in res.reason

    def test_max_lots_reached(self) -> None:
        g = AddPositionGuard()
        pos = Position(direction="LONG", volume=g.max_lots, entry_price=6000)
        res = g.check(pos, "LONG", confidence=0.90,
                      current_price=6080, atr_data=self._atr())
        assert not res.allowed
        assert "最大持仓" in res.reason

    def test_price_gap_too_small(self) -> None:
        g = AddPositionGuard()
        pos = Position(direction="LONG", volume=1, entry_price=6000)
        res = g.check(pos, "LONG", confidence=0.90,
                      current_price=6010, atr_data=self._atr())
        assert not res.allowed
        assert "价格错开" in res.reason

    def test_drawdown_too_large(self) -> None:
        g = AddPositionGuard()
        pos = Position(direction="LONG", volume=1, entry_price=6000)
        no_atr = AIData(atr_5=0, atr_15=0, atr_60=0, stress_level=1.0)
        res = g.check(pos, "LONG", confidence=0.90,
                      current_price=5800, atr_data=no_atr)
        assert not res.allowed
        assert "浮亏" in res.reason

    def test_price_gap_blocked_before_drawdown(self) -> None:
        g = AddPositionGuard()
        pos = Position(direction="LONG", volume=1, entry_price=6000)
        res = g.check(pos, "LONG", confidence=0.90,
                      current_price=5800, atr_data=self._atr())
        assert not res.allowed
        assert "价格错开" in res.reason

    def test_pass_all(self) -> None:
        g = AddPositionGuard()
        pos = Position(direction="LONG", volume=1, entry_price=6000)
        res = g.check(pos, "LONG", confidence=0.90,
                      current_price=6080, atr_data=self._atr())
        assert res.allowed

    def test_empty_position_passes(self) -> None:
        g = AddPositionGuard()
        res = g.check(Position(), "LONG", confidence=0.50,
                      current_price=6000, atr_data=self._atr())
        assert res.allowed


class TestPositionSizer:
    def test_zero_balance(self) -> None:
        s = PositionSizer()
        assert s.max_lots(0, 6000) == 0

    def test_zero_price(self) -> None:
        s = PositionSizer()
        assert s.max_lots(100000, 0) == 0

    def test_max_lots_normal(self) -> None:
        s = PositionSizer()
        margin_per_lot = 6000 * 200 * 0.15
        lots = s.max_lots(margin_per_lot * 5, 6000)
        assert lots >= 1
        assert lots <= 5

    def test_confidence_tier_progression(self) -> None:
        s = PositionSizer()
        l1 = s.lots_for_confidence(0.55, 20)
        l2 = s.lots_for_confidence(0.70, 20)
        l3 = s.lots_for_confidence(0.80, 20)
        l4 = s.lots_for_confidence(0.90, 20)
        assert l1 <= l2 <= l3 <= l4
        assert l1 >= 1
        assert l4 <= 10

    def test_confidence_below_min(self) -> None:
        s = PositionSizer()
        assert s.lots_for_confidence(0.30, 20) == 0

    def test_cap_by_risk(self) -> None:
        s = PositionSizer()
        capped = s.cap_by_risk(
            volume=5, entry_price=6000, stop_loss=5900, balance=100000,
        )
        assert 0 <= capped <= 5


class TestEmergencyState:
    def test_initial(self) -> None:
        e = EmergencyState()
        assert not e.active
        assert e.entered_at is None

    def test_trigger_and_reset(self) -> None:
        e = EmergencyState()
        e.trigger()
        assert e.active
        assert e.entered_at is not None
        e.reset()
        assert not e.active

    def test_auto_reset_requires_empty_position(self) -> None:
        e = EmergencyState()
        e.trigger()
        e.entered_at = datetime.now() - timedelta(seconds=e.reset_timeout + 1)
        assert not e.should_auto_reset(position_is_empty=False)
        assert e.should_auto_reset(position_is_empty=True)


class TestRiskManager:
    def test_default_stop_adjust_allowed(self) -> None:
        rm = RiskManager()
        assert rm.can_adjust_stop()

    def test_after_mark_in_cooldown(self) -> None:
        rm = RiskManager()
        rm.mark_stop_adjusted()
        assert not rm.can_adjust_stop()
