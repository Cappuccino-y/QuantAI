"""持仓管理单测."""
from __future__ import annotations

from pathlib import Path

import pytest

from quantai.models import ConditionalOrder, Position
from quantai.position_manager import PositionManager


@pytest.fixture
def manager(tmp_path: Path) -> PositionManager:
    return PositionManager(state_file=tmp_path / "pos.pkl")


class TestPositionManagerBasics:
    def test_initial_empty(self, manager: PositionManager) -> None:
        assert manager.position.is_empty
        assert manager.conditional_order is None

    def test_update_then_load(self, manager: PositionManager) -> None:
        manager.update_position(
            direction="LONG", volume=2,
            entry_price=6000, stop_loss=5950, take_profit=6100,
        )
        reload = PositionManager(state_file=manager.state_file)
        reload.load()
        assert reload.position.direction == "LONG"
        assert reload.position.volume == 2
        assert reload.position.entry_price == 6000

    def test_clear(self, manager: PositionManager) -> None:
        manager.update_position(direction="SHORT", volume=1)
        manager.clear_position()
        assert manager.position.is_empty

    def test_conditional_roundtrip(self, manager: PositionManager) -> None:
        cond = ConditionalOrder(
            trigger_type="PRICE_ABOVE", trigger_price=6050,
            stop_loss=6020, take_profit=6090, action="BUY",
        )
        manager.set_conditional(cond)
        reload = PositionManager(state_file=manager.state_file)
        reload.load()
        assert reload.conditional_order is not None
        assert reload.conditional_order.trigger_type == "PRICE_ABOVE"
        assert reload.conditional_order.action == "BUY"


class TestReconciliation:
    def test_no_drift_returns_false(self, manager: PositionManager) -> None:
        manager.update_position(direction="LONG", volume=2, entry_price=6000)
        drift = manager.reconcile_with_broker("LONG", 2, 6000)
        assert drift is False

    def test_volume_drift_corrects(self, manager: PositionManager) -> None:
        manager.update_position(direction="LONG", volume=2, entry_price=6000)
        drift = manager.reconcile_with_broker("LONG", 1, 6000)
        assert drift is True
        assert manager.position.volume == 1

    def test_direction_drift_corrects(self, manager: PositionManager) -> None:
        manager.update_position(direction="LONG", volume=2, entry_price=6000)
        drift = manager.reconcile_with_broker("SHORT", 1, 5900)
        assert drift is True
        assert manager.position.direction == "SHORT"
        assert manager.position.entry_price == 5900

    def test_broker_flat_clears_sl_tp(self, manager: PositionManager) -> None:
        manager.update_position(
            direction="LONG", volume=2, entry_price=6000,
            stop_loss=5950, take_profit=6100,
        )
        drift = manager.reconcile_with_broker(None, 0, 0.0)
        assert drift is True
        assert manager.position.is_empty
        assert manager.position.stop_loss == 0
        assert manager.position.take_profit == 0

    def test_entry_price_sync_only(self, manager: PositionManager) -> None:
        manager.update_position(direction="LONG", volume=2, entry_price=6000.0)
        drift = manager.reconcile_with_broker("LONG", 2, 6000.50)
        assert drift is False
        assert manager.position.entry_price == 6000.50
