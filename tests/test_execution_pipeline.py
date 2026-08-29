"""execution_pipeline 单测（阶段 4）— 行为对拍真源 L2108–2925 / L5376–5433。

覆盖（八步编排逐段）:
- P1 止损冷却: 同向拦截 / adjust_existing 放行 / 反向换仓放行
- P0 ratchet: 放宽被拒（信心不足）/ 高信心放宽后仍受 ADJUST_STOP 方向纠错（6/15 修复）
- 持仓调整: SL/TP 更新 + 调整冷却
- 条件单: conv 转期货价 + created_date + 止损过紧放宽/过宽收紧 + 无新条件单清除
- 即时入场: 无效 SL/TP 拒绝 / 止损过紧放宽（0.8×ATR 手算）/ 过宽收紧（3×ATR 手算）
  / 成功开仓（entry_time datetime + bump + 通知）
- 同向加仓: 价差不足拒绝 / 成功加仓均价合并手算
- 反向平仓后开新仓
- execute_ai_cycle: 熔断跳过 / prompt 未注入 / 正常循环间隔钳制
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from quantai.execution_pipeline import ExecutionPipeline
from quantai.models import FilterResult
from quantai.position_manager import PositionManager
from quantai.risk_manager import (CircuitBreaker, DailyTradeLimiter,
                                  EmergencyState, StopOutCooldown)


# ---------- 测试替身 ----------

class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class FakeLogger:
    def __init__(self):
        self.events = []

    def log(self, *args, **kwargs):
        self.events.append((args, kwargs))


class FakeMds:
    def __init__(self, last=5000.0, index_price=5000.0):
        self.symbol = "CFFEX.IM2608"
        self.im_quote = SimpleNamespace(last_price=last, ask_price1=last + 0.2,
                                        bid_price1=last - 0.2)
        self.index_price = index_price
        self.api = SimpleNamespace(
            get_account=lambda: SimpleNamespace(balance=200000.0, position_profit=0.0),
            wait_update=lambda deadline=None: None)
        self.update_calls = 0
        self.refresh_calls = 0

    def index_to_future_price(self, idx_price):
        fut = self.im_quote.last_price
        idx = self.index_price
        if idx <= 0 or fut <= 0:
            return round(idx_price / 0.2) * 0.2
        return round(idx_price * (fut / idx) / 0.2) * 0.2

    def update_index_price(self):
        self.update_calls += 1

    def refresh_tech_data(self):
        self.refresh_calls += 1


class FakeMcs:
    def __init__(self, atr5=20.0, atr15=50.0):
        self.atr_5 = atr5
        self.atr_15 = atr15
        self.atr_calls = 0

    def calculate_fut_atr(self):
        self.atr_calls += 1


class AllPassFilters:
    def check_trend_alignment(self, direction):
        return FilterResult(allowed=True, reason="trend ok")

    def check_session_extremes(self, entry_price, direction):
        return FilterResult(allowed=True, reason="session ok")

    def check_htf_bias(self, direction):
        return FilterResult(allowed=True, reason="htf ok")

    def check_entry_volume(self, min_ratio=1.0):
        return FilterResult(allowed=True, reason="volume ok")

    def check_entry_confirmation(self, direction):
        return FilterResult(allowed=True, reason="confirm ok")


class AllPassExemptions:
    def vwap_alignment(self, direction):
        return FilterResult(allowed=True, reason="vwap ok")

    def trend_reversal_exempt(self, direction):
        return FilterResult(allowed=True, reason="choch ok")

    def htf_partial_allowance(self, direction):
        return FilterResult(allowed=True, reason="htf partial ok")

    def volume_vcp_check(self):
        return FilterResult(allowed=True, reason="vcp ok")


class AllFailExemptions:
    """全失败豁免（vwap 不通过 → 不进入豁免分支）。"""

    def vwap_alignment(self, direction):
        return FilterResult(allowed=False, reason="vwap 未对齐")

    def trend_reversal_exempt(self, direction):
        return FilterResult(allowed=False, reason="choch 未确认")

    def htf_partial_allowance(self, direction):
        return FilterResult(allowed=False, reason="htf 未反转")

    def volume_vcp_check(self):
        return FilterResult(allowed=False, reason="vcp 未齐升")


class FakeSizer:
    def __init__(self, max_lots=3):
        self.max_lots = max_lots

    def get_max_lots(self):
        return self.max_lots

    def apply_risk_scale(self, volume, sl_distance):
        return volume


class FakeOE:
    def __init__(self, open_price=5000.4):
        self.open_price = open_price
        self.open_calls = []
        self.close_calls = []

    def execute_order_safe(self, **kwargs):
        self.open_calls.append(kwargs)
        return self.open_price

    def close_position(self, reason, is_emergency=False):
        self.close_calls.append(reason)
        return True


NOW = datetime(2026, 8, 28, 10, 0, 0)


def make_pipeline(tmp_path, *, position=None, mds=None, mcs=None, cb=None,
                  stopout=None, limiter=None, oe=None, tail=(False, ""),
                  prompt_fn=None, ai_chat_fn=None, save_decision_fn=None,
                  now=NOW):
    pm = PositionManager(position_file=str(tmp_path / "p.pkl"), now_fn=lambda: now)
    if position:
        pm.position.update(position)
    return (ExecutionPipeline(
        pm=pm, mds=mds or FakeMds(), mcs=mcs or FakeMcs(),
        sizer=FakeSizer(), daily_limiter=limiter or DailyTradeLimiter(lambda: now),
        circuit_breaker=cb or CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: now),
        stopout=stopout or StopOutCooldown(lambda: now),
        filters=AllPassFilters(), exemptions=AllPassExemptions(),
        oe=oe or FakeOE(), tail_fn=lambda: tail,
        notifier=FakeNotifier(), logger=FakeLogger(),
        prompt_fn=prompt_fn, ai_chat_fn=ai_chat_fn,
        save_decision_fn=save_decision_fn, now_fn=lambda: now,
    ), pm)


# ---------- P1 止损冷却 ----------

class TestStopoutCooldown:
    def test_same_direction_blocked(self, tmp_path):
        now = NOW
        stopout = StopOutCooldown(lambda: now)
        stopout.record("LONG", when=now - timedelta(seconds=300))
        pipe, pm = make_pipeline(tmp_path, stopout=stopout, now=now)
        oe = FakeOE()
        pipe.oe = oe
        pipe.execute_decision({"action": "BUY", "confidence": 0.9,
                               "stop_loss": 4960, "take_profit": 5100})
        assert oe.open_calls == []   # 冷却期内拒绝

    def test_adjust_existing_passes_cooldown(self, tmp_path):
        """反向换仓放行: 冷却方向 LONG + action BUY + 持仓 SHORT + adjust_existing → 放行。"""
        now = NOW
        stopout = StopOutCooldown(lambda: now)
        stopout.record("LONG", when=now - timedelta(seconds=300))
        pipe, pm = make_pipeline(
            tmp_path, stopout=stopout, now=now,
            position={"direction": "SHORT", "volume": 1, "entry_price": 5000.0,
                      "stop_loss": 5050.0, "take_profit": 4900.0})

        def fake_close(reason, is_emergency=False):
            pipe.oe.close_calls.append(reason)
            pm.position.update({"direction": None, "volume": 0,
                                "entry_price": 0.0, "stop_loss": 0.0,
                                "take_profit": 0.0, "last_ai_decision": None,
                                "entry_time": None})
            return True

        pipe.oe.close_position = fake_close
        pipe.execute_decision({"action": "BUY", "confidence": 0.9,
                               "stop_loss": 4960, "take_profit": 5100,
                               "adjust_existing": {"new_stop_loss": 5040}})
        assert pipe.oe.close_calls == ["反向开仓前平仓"]
        assert pm.position["direction"] == "LONG"

    def test_cooldown_same_dir_with_adjust_rejected(self, tmp_path):
        """冷却期内同向 + adjust_existing 一起（想加仓）→ 也拒（真源 L2137-2140）。"""
        now = NOW
        stopout = StopOutCooldown(lambda: now)
        stopout.record("LONG", when=now - timedelta(seconds=300))
        pipe, pm = make_pipeline(
            tmp_path, stopout=stopout, now=now,
            position={"direction": "LONG", "volume": 1, "entry_price": 5000.0,
                      "stop_loss": 4950.0, "take_profit": 5100.0})
        pipe.execute_decision({"action": "BUY", "confidence": 0.9,
                               "adjust_existing": {"new_stop_loss": 4960}})
        assert pipe.oe.open_calls == []
        assert pm.position["stop_loss"] == 4950.0   # 直接 return，调整未执行


# ---------- P0 ratchet + 持仓调整 ----------

class TestAdjustExisting:
    def test_ratchet_rejects_relax_low_confidence(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, position={"direction": "LONG", "volume": 1,
                                "entry_price": 5000.0, "stop_loss": 4950.0,
                                "take_profit": 5100.0})
        pipe.execute_decision({"action": "WAIT", "confidence": 0.6,
                               "adjust_existing": {"new_stop_loss": 4900}})
        assert pm.position["stop_loss"] == 4950.0   # 放宽被拒
        assert any("止损放宽被拒" in m for m in pipe.notifier.sent)

    def test_ratchet_allows_then_direction_guard_corrects(self, tmp_path):
        """高信心放宽 → ADJUST_STOP 方向纠错（6/15 修复）: LONG 保护位 = entry - atr_5。"""
        pipe, pm = make_pipeline(
            tmp_path, position={"direction": "LONG", "volume": 1,
                                "entry_price": 5000.0, "stop_loss": 4950.0,
                                "take_profit": 5100.0})
        pipe.execute_decision({"action": "WAIT", "confidence": 0.8,
                               "adjust_existing": {"new_stop_loss": 4900}})
        # 保护位 = 5000 - 20 = 4980; floor = max(4950, 4980) = 4980 → 纠正
        assert pm.position["stop_loss"] == 4980.0
        assert any("ADJUST_STOP 方向纠错" in m for m in pipe.notifier.sent)

    def test_adjust_updates_sl_tp(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, position={"direction": "LONG", "volume": 1,
                                "entry_price": 5000.0, "stop_loss": 4950.0,
                                "take_profit": 5100.0})
        # SL 4985 ≥ 保护位 floor max(4950, 5000-20)=4980 → 不触发纠错; TP 5150
        pipe.execute_decision({"action": "WAIT", "confidence": 0.6,
                               "adjust_existing": {"new_stop_loss": 4985,
                                                    "new_take_profit": 5150}})
        assert pm.position["stop_loss"] == 4985.0
        assert pm.position["take_profit"] == 5150.0
        assert any(e[0][0] == "ADJUST_STOP" for e in pipe.logger.events)
        assert any(e[0][0] == "ADJUST_PROFIT" for e in pipe.logger.events)
        assert any("日间调整" in m for m in pipe.notifier.sent)

    def test_adjust_sl_below_protection_corrected(self, tmp_path):
        """SL 4970 < 保护位 4980 → 6/15 方向纠错强制 4980（实现保真，log 已验证）。"""
        pipe, pm = make_pipeline(
            tmp_path, position={"direction": "LONG", "volume": 1,
                                "entry_price": 5000.0, "stop_loss": 4950.0,
                                "take_profit": 5100.0})
        pipe.execute_decision({"action": "WAIT", "confidence": 0.6,
                               "adjust_existing": {"new_stop_loss": 4970}})
        assert pm.position["stop_loss"] == 4980.0

    def test_adjust_cooldown_skips(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, position={"direction": "LONG", "volume": 1,
                                "entry_price": 5000.0, "stop_loss": 4950.0,
                                "take_profit": 5100.0})
        pipe.last_stop_adjust_time = NOW - timedelta(seconds=100)   # 5 分钟内已调过
        pipe.execute_decision({"action": "WAIT", "confidence": 0.6,
                               "adjust_existing": {"new_stop_loss": 4970}})
        assert pm.position["stop_loss"] == 4950.0   # 跳过


# ---------- 条件单处理 ----------

class TestConditionalEntry:
    def test_conditional_set_with_conv_and_date(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({
            "action": "BUY", "confidence": 0.8,
            "conditional_entry": {"trigger_type": "PRICE_ABOVE",
                                   "trigger_price": 4900.0, "stop_loss": 4860.0,
                                   "take_profit": 5000.0, "volume": 2}})
        cond = pm.conditional_order
        assert cond is not None
        assert cond["trigger_price"] == 4900.0    # conv（basis_rate=1.0）
        assert cond["stop_loss"] == 4860.0
        assert cond["action"] == "BUY"
        assert cond["created_date"] == "2026-08-28"   # 8/27 修复
        assert any("新条件单" in m for m in pipe.notifier.sent)
        assert pipe.oe.open_calls == []           # 条件单路径不开仓

    def test_conditional_sl_too_tight_widened(self, tmp_path):
        """条件单止损 < 0.6×5minATR=12 → 放宽到 12 点距离。"""
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({
            "action": "BUY", "confidence": 0.8,
            "conditional_entry": {"trigger_type": "PRICE_ABOVE",
                                   "trigger_price": 4900.0, "stop_loss": 4895.0,
                                   "take_profit": 5000.0, "volume": 1}})
        assert pm.conditional_order["stop_loss"] == pytest.approx(4888.0)  # 4900-12

    def test_conditional_sl_too_wide_tightened(self, tmp_path):
        """条件单止损距离 > 3×15minATR=150 → 收紧。"""
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({
            "action": "BUY", "confidence": 0.8,
            "conditional_entry": {"trigger_type": "PRICE_ABOVE",
                                   "trigger_price": 4900.0, "stop_loss": 4700.0,
                                   "take_profit": 5000.0, "volume": 1}})
        assert pm.conditional_order["stop_loss"] == pytest.approx(4750.0)  # 4900-150

    def test_no_new_cond_clears_old(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        pm.conditional_order = {"action": "BUY", "trigger_price": 5000.0}
        pipe.execute_decision({"action": "WAIT", "confidence": 0.3})
        assert pm.conditional_order is None
        assert any("条件单已清除" in m for m in pipe.notifier.sent)

    def test_low_confidence_no_conditional(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({
            "action": "BUY", "confidence": 0.5,   # < MIN_CONFIDENCE 0.55
            "conditional_entry": {"trigger_type": "PRICE_ABOVE",
                                   "trigger_price": 4900.0}})
        assert pm.conditional_order is None


# ---------- 即时入场（新开仓） ----------

class TestImmediateEntry:
    def test_invalid_sl_tp_rejected(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 0, "take_profit": 5100})
        assert pipe.oe.open_calls == []
        assert any("立即单缺少止损/止盈" in m for m in pipe.notifier.sent)

    def test_sl_too_tight_auto_widened(self, tmp_path):
        """止损距离 10 < 0.8×5minATR=16 → 放宽到 0.8×ATR_MULT=16 点。"""
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4990.0, "take_profit": 5100.0})
        assert pm.position["stop_loss"] == pytest.approx(4984.0)   # 5000-16
        assert any("止损过紧自动放宽" in m for m in pipe.notifier.sent)

    def test_sl_too_wide_auto_tightened(self, tmp_path):
        """止损距离 200 > 3×15minATR=150 → 收紧到 150 点。"""
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4800.0, "take_profit": 5100.0})
        assert pm.position["stop_loss"] == pytest.approx(4850.0)   # 5000-150
        assert any("止损过宽自动收紧" in m for m in pipe.notifier.sent)

    def test_open_success_full_state(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        lim = pipe.daily_limiter
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0,
                               "reason": "突破买入"})
        pos = pm.position
        assert pos["direction"] == "LONG"
        assert pos["volume"] == 1
        assert pos["entry_price"] == 5000.4
        assert pos["stop_loss"] == 4960.0
        assert pos["entry_time"] == NOW            # datetime（6/15 修复）
        assert pm.last_entry_time == NOW
        assert lim.check() == (False, "今日开仓 1/6 次")
        assert any(e[0][0] == "OPEN" for e in pipe.logger.events)

    def test_open_success_rr_message(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        msg = [m for m in pipe.notifier.sent if "IM开仓" in m][0]
        # sl_dist = 40.4, tp_dist = 99.6 → rr = 2.4653... → 1:2.47
        assert "盈亏比 1:2.47" in msg

    def test_open_failure_throttled_alert(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)
        oe = FakeOE()
        oe.execute_order_safe = lambda **kw: None
        pipe.oe = oe
        for _ in range(3):
            pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                                   "stop_loss": 4960.0, "take_profit": 5100.0})
        # 5 分钟内 3 次失败 → 节流告警
        assert any("次开仓失败" in m for m in pipe.notifier.sent)
        assert pipe._failed_order_window == []   # 告警后清空

    def test_filter_rejection_blocks(self, tmp_path):
        class RejectTrend(AllPassFilters):
            def check_trend_alignment(self, direction):
                return FilterResult(allowed=False, reason="60min 方向未确认")

        pipe, pm = make_pipeline(tmp_path, )
        pipe.filters = RejectTrend()
        pipe.exemptions = AllFailExemptions()   # 豁免全失败 → 拦截生效
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.open_calls == []
        assert any("假突破过滤器拦截" in m for m in pipe.notifier.sent)

    def test_filter_rejection_saved_by_exemptions(self, tmp_path):
        class RejectTrend(AllPassFilters):
            def check_trend_alignment(self, direction):
                return FilterResult(allowed=False, reason="60min 方向未确认")

        pipe, pm = make_pipeline(tmp_path)
        pipe.filters = RejectTrend()
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert len(pipe.oe.open_calls) == 1   # 豁免通过放行
        assert any("反向确认豁免通过" in m for m in pipe.notifier.sent)

    def test_circuit_breaker_blocks(self, tmp_path):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: NOW)
        cb.record_trade_result(-4000.0)   # 日亏 2% ≥ 1.5%
        pipe, pm = make_pipeline(tmp_path, cb=cb)
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.open_calls == []
        assert any("熔断拦截" in m for m in pipe.notifier.sent)

    def test_daily_limit_blocks(self, tmp_path):
        lim = DailyTradeLimiter(lambda: NOW)
        for _ in range(6):
            lim.bump()
        pipe, pm = make_pipeline(tmp_path, limiter=lim)
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.open_calls == []


# ---------- 同向加仓 ----------

class TestAddPosition:
    def _long_position(self):
        return {"direction": "LONG", "volume": 1, "entry_price": 5000.0,
                "stop_loss": 4960.0, "take_profit": 5100.0,
                "last_ai_decision": "test", "entry_time": NOW}

    def test_price_gap_insufficient_rejected(self, tmp_path):
        """加仓价差 0 < 1.0×15minATR=50 → 拒绝（防追高）。"""
        pipe, pm = make_pipeline(tmp_path, position=self._long_position())
        pipe.execute_decision({"action": "BUY", "confidence": 0.85, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.open_calls == []
        assert any("价差" in m and "50" in m for m in pipe.notifier.sent)

    def test_add_success_avg_merge(self, tmp_path):
        """价差 60 ≥ 50 → 加仓成功; 均价 = (5000×1 + 5060.4×1)/2 = 5030.2。"""
        mds = FakeMds(last=5060.0, index_price=5000.0)
        pipe, pm = make_pipeline(tmp_path, position=self._long_position(), mds=mds,
                                 oe=FakeOE(open_price=5060.4))
        lim = pipe.daily_limiter
        pipe.execute_decision({"action": "BUY", "confidence": 0.85, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pm.position["volume"] == 2
        assert pm.position["entry_price"] == pytest.approx(5030.2)
        assert lim.check() == (False, "今日开仓 1/6 次")
        assert any("IM同向加仓" in m for m in pipe.notifier.sent)

    def test_add_low_confidence_rejected(self, tmp_path):
        mds = FakeMds(last=5060.0)
        pipe, pm = make_pipeline(tmp_path, position=self._long_position(), mds=mds)
        pipe.execute_decision({"action": "BUY", "confidence": 0.65, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.open_calls == []   # 0.65 < ADD_REQUIRED_CONFIDENCE 0.70
        assert any("加仓被拒" in m for m in pipe.notifier.sent)

    def test_add_max_lots_rejected(self, tmp_path):
        pos = self._long_position()
        pos["volume"] = 3   # 已达 MAX_POSITION_LOTS
        mds = FakeMds(last=5060.0)
        pipe, pm = make_pipeline(tmp_path, position=pos, mds=mds)
        pipe.execute_decision({"action": "BUY", "confidence": 0.85, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.open_calls == []
        assert any("已达最大" in m for m in pipe.notifier.sent)

    def test_add_after_adjust_applied(self, tmp_path):
        """加仓成功后可再次应用 adjust_existing（真源 L2577–2586）。"""
        mds = FakeMds(last=5060.0, index_price=5000.0)
        pipe, pm = make_pipeline(tmp_path, position=self._long_position(), mds=mds)
        pipe.execute_decision({"action": "BUY", "confidence": 0.85, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0,
                               "adjust_existing": {"new_stop_loss": 5020.0}})
        # 加仓后 SL = conv(5020) = 5020×1.012 = 5080.24 → round/0.2 → 5080.2
        assert pm.position["stop_loss"] == pytest.approx(5080.2)


# ---------- 反向平仓 ----------

class TestReversePosition:
    def test_reverse_close_then_open(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, position={"direction": "SHORT", "volume": 1,
                                "entry_price": 5000.0, "stop_loss": 5050.0,
                                "take_profit": 4900.0})

        def fake_close(reason, is_emergency=False):
            pipe.oe.close_calls.append(reason)
            pm.position.update({"direction": None, "volume": 0,
                                "entry_price": 0.0, "stop_loss": 0.0,
                                "take_profit": 0.0, "last_ai_decision": None,
                                "entry_time": None})
            return True

        pipe.oe.close_position = fake_close
        pipe.execute_decision({"action": "BUY", "confidence": 0.8, "volume": 1,
                               "stop_loss": 4960.0, "take_profit": 5100.0})
        assert pipe.oe.close_calls == ["反向开仓前平仓"]
        assert pm.position["direction"] == "LONG"   # 平仓后新开


# ---------- execute_ai_cycle（真源 L5376–5433） ----------

class TestExecuteAiCycle:
    def test_refresh_called(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path, prompt_fn=lambda m: ("sys", "user"),
                                 ai_chat_fn=lambda messages: '{"action": "WAIT"}')
        pipe.execute_ai_cycle("SWING")
        assert pipe.mds.update_calls == 1
        assert pipe.mds.refresh_calls == 1
        assert pipe.mcs.atr_calls == 1

    def test_circuit_breaker_skips_when_flat(self, tmp_path):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: NOW)
        cb.record_trade_result(-4000.0)
        pipe, pm = make_pipeline(tmp_path, cb=cb,
                                 prompt_fn=lambda m: pytest.fail("不应构建 prompt"),
                                 ai_chat_fn=lambda messages: pytest.fail("不应调 AI"))
        pm.conditional_order = {"action": "BUY"}   # 残留条件单
        interval = pipe.execute_ai_cycle("SWING")
        assert interval == 900                     # BASE_DECISION_INTERVAL
        assert pm.conditional_order is None        # 顺带清除

    def test_circuit_breaker_continues_with_position(self, tmp_path):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: NOW)
        cb.record_trade_result(-4000.0)
        pipe, pm = make_pipeline(
            tmp_path, cb=cb,
            position={"direction": "LONG", "volume": 1, "entry_price": 5000.0,
                      "stop_loss": 4950.0, "take_profit": 5100.0},
            prompt_fn=lambda m: ("sys", "user"),
            ai_chat_fn=lambda messages: '{"action": "WAIT", "confidence": 0.3}')
        interval = pipe.execute_ai_cycle("SWING")
        assert interval == 900   # 有持仓 → 继续决策（仅持仓管理）

    def test_prompt_fn_missing_returns_default(self, tmp_path):
        pipe, pm = make_pipeline(tmp_path)   # prompt_fn=None
        interval = pipe.execute_ai_cycle("SCALPING")
        assert interval == 300   # SHORT_TERM_INTERVAL

    def test_happy_path_interval_clamped(self, tmp_path):
        saved = []
        pipe, pm = make_pipeline(
            tmp_path, prompt_fn=lambda m: ("sys", "user"),
            ai_chat_fn=lambda messages: '{"action": "WAIT", "confidence": 0.3, "next_interval_sec": 600}',
            save_decision_fn=saved.append)
        interval = pipe.execute_ai_cycle("SWING")
        assert interval == 600                    # max(300, min(600, 1200))
        assert len(saved) == 1
        assert saved[0]["_mode"] == "SWING"       # 真源 L5421

    def test_interval_floor_and_ceiling(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, prompt_fn=lambda m: ("sys", "user"),
            ai_chat_fn=lambda messages: '{"action": "WAIT", "next_interval_sec": 50}')
        assert pipe.execute_ai_cycle("SWING") == 300   # 下限钳制
        pipe2, _ = make_pipeline(
            tmp_path, prompt_fn=lambda m: ("sys", "user"),
            ai_chat_fn=lambda messages: '{"action": "WAIT", "next_interval_sec": 9999}')
        assert pipe2.execute_ai_cycle("SWING") == 1200  # 上限钳制

    def test_invalid_interval_falls_back(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, prompt_fn=lambda m: ("sys", "user"),
            ai_chat_fn=lambda messages: '{"action": "WAIT", "next_interval_sec": "abc"}')
        assert pipe.execute_ai_cycle("SWING") == 900

    def test_no_json_in_response(self, tmp_path):
        pipe, pm = make_pipeline(
            tmp_path, prompt_fn=lambda m: ("sys", "user"),
            ai_chat_fn=lambda messages: '抱歉，我无法回答')
        assert pipe.execute_ai_cycle("SWING") == 900   # default_interval

    def test_ai_exception_returns_default(self, tmp_path):
        def boom(messages):
            raise RuntimeError("llm down")

        pipe, pm = make_pipeline(tmp_path, prompt_fn=lambda m: ("sys", "user"),
                                 ai_chat_fn=boom)
        assert pipe.execute_ai_cycle("SCALPING") == 300
