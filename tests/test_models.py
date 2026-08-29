"""models 数据模型测试 — 重点验证 pkl 兼容性（design.md §三.5）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quantai.models import (AIDecision, ConditionalOrder, FilterResult,
                            LunchContext, Position, SignalRegime)


def test_position_matches_source_dict():
    """Position 字段必须与真源 current_position dict（L147–154）逐键一致。"""
    src_keys = {"direction", "volume", "entry_price", "stop_loss",
                "take_profit", "last_ai_decision"}
    p = Position()
    assert set(p.to_dict().keys()) == src_keys
    # 空仓默认值与真源 L147–154 一致
    assert p.direction is None
    assert p.volume == 0
    assert p.entry_price == 0.0
    assert p.is_empty()


def test_position_roundtrip():
    d = {"direction": "SHORT", "volume": 2, "entry_price": 4123.4,
         "stop_loss": 4150.0, "take_profit": 4050.0, "last_ai_decision": "L12a"}
    p = Position.from_dict(d)
    assert p.to_dict() == d


def test_conditional_order_lunch_fields():
    """午盘路径字段（真源 L4238–4250）完整承载。

    注意: 真源午盘 dict 不含 created_date（那是 AI 路径 L2305 追加的），
    from_dict 容忍缺失 → to_dict 恒输出 created_date=""（多余空键对 pkl 兼容无害）。
    """
    d = {
        "action": "SELL", "trigger_type": "price_break",
        "trigger_price": 4050.0, "limit_price": 0,
        "stop_loss": 4060.0, "take_profit": 4020.0,
        "volume": 1, "source": "12:50_lunch_breakout",
        "kospi_amp": 1.2, "kospi_delta": -0.6,
        "force_close_time": "14:00",
    }
    co = ConditionalOrder.from_dict(d)
    out = co.to_dict()
    for k, v in d.items():
        assert out[k] == v, f"field {k}: {out[k]!r} != {v!r}"
    assert out["created_date"] == ""


def test_conditional_order_tolerates_missing_keys():
    """旧 pkl 条件单可能缺 created_date 等字段，from_dict 必须容忍。"""
    co = ConditionalOrder.from_dict({"action": "BUY", "trigger_price": 4000.0})
    assert co.created_date == ""
    assert co.kospi_amp is None
    assert co.to_dict()["volume"] == 0


def test_ai_decision_keys():
    """AIDecision 键集合按真源 grep 核实（见 models.py docstring）。"""
    d = {
        "action": "BUY", "confidence": 0.8, "reason": "L12a 共振",
        "stop_loss": 3980.0, "take_profit": 4060.0, "volume": 1,
        "conditional_entry": {"trigger_price": 4000.0},
        "next_interval_sec": 300,
    }
    a = AIDecision.from_dict(d)
    assert a.action == "BUY"
    assert a.confidence == 0.8
    assert a.conditional_entry == {"trigger_price": 4000.0}
    # 缺省 action = WAIT（真源 L2246）
    assert AIDecision.from_dict({}).action == "WAIT"


def test_filter_result():
    f = FilterResult(allowed=False, reason="60min 方向锁: 空头排列", filter_name="TrendAlignment")
    assert not f.allowed
    assert f.reason
    assert FilterResult(allowed=True).reason == ""


def test_lunch_context_set_refreshes_time():
    lc = LunchContext()
    lc.set("kospi_amp", 1.1)
    assert lc.get("kospi_amp") == 1.1
    assert lc.update_time != ""
    assert lc.get("missing", "dft") == "dft"


def test_signal_regime():
    assert SignalRegime("BULL") == SignalRegime.BULL
    assert {r.value for r in SignalRegime} == {"BULL", "BEAR", "NEUTRAL"}
