"""数据模型行为测试."""
from __future__ import annotations

from datetime import datetime

from quantai.models import ConditionalOrder, Position, TradeEvent


class TestPosition:
    def test_default_is_empty(self) -> None:
        p = Position()
        assert p.is_empty
        assert p.direction is None
        assert p.volume == 0

    def test_with_long_position_not_empty(self) -> None:
        p = Position(direction="LONG", volume=2, entry_price=6000.0)
        assert not p.is_empty
        assert p.direction == "LONG"

    def test_copy_is_independent(self) -> None:
        p = Position(direction="LONG", volume=2, entry_price=6000.0)
        clone = p.copy()
        clone.volume = 99
        assert p.volume == 2
        assert clone.volume == 99

    def test_roundtrip_dict(self) -> None:
        p = Position(direction="SHORT", volume=1, entry_price=5800.5,
                     stop_loss=5850.0, take_profit=5700.0,
                     entry_time=datetime(2026, 6, 11, 10, 30))
        d = p.to_dict()
        d["entry_time"] = d["entry_time"].strftime("%Y-%m-%d %H:%M:%S")
        restored = Position.from_dict(d)
        assert restored.direction == "SHORT"
        assert restored.volume == 1
        assert restored.entry_price == 5800.5
        assert restored.stop_loss == 5850.0
        assert restored.entry_time == datetime(2026, 6, 11, 10, 30)


class TestConditionalOrder:
    def test_construct_minimal(self) -> None:
        c = ConditionalOrder(
            trigger_type="PRICE_ABOVE", trigger_price=6000,
            stop_loss=5980, take_profit=6040, action="BUY",
        )
        assert c.volume == 1
        assert c.source == "ai"

    def test_from_dict_with_extra(self) -> None:
        c = ConditionalOrder.from_dict({
            "trigger_type": "PRICE_BELOW",
            "trigger_price": 5900,
            "stop_loss": 5920,
            "take_profit": 5860,
            "action": "SELL",
            "volume": 2,
            "kospi_amp": 1.2,
        })
        assert c.action == "SELL"
        assert c.extra == {"kospi_amp": 1.2}


class TestTradeEvent:
    def test_to_row_length_matches_header(self) -> None:
        from quantai.logger import TradeLogger
        ev = TradeEvent(
            event_type="OPEN", symbol="CFFEX.IM2606", direction="LONG",
            volume=2, price=6000.5, balance_after=200000.0,
            ai_reason="多周期均线同向",
        )
        row = ev.to_row()
        assert len(row) == len(TradeLogger.HEADER)
        assert "OPEN" in row
        assert "CFFEX.IM2606" in row
