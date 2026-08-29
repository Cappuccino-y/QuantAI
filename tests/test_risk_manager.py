"""risk_manager 单测（阶段 4）— 行为对拍真源 L786–903 / L1365–1518 / L823–853。

手算对拍锚点:
- get_max_lots: balance=1,000,000, im=5000 → margin/lot=150,000 → max=6, safe=int(600000//150000)=4 → 4
- max_lots_by_risk: equity=200,000, sl=20 → 2000/4000=0.5 → 0; sl=10 → 1
- get_risk_scale: 阈值 = 0.015×0.6 = 0.9%; dd=0.5% → 1.0; dd=1.0% → 0.5
- 熔断: 当日连亏 3 笔 → blocked; 日亏 ≥1.5% 权益 → blocked; 跨日惰性重置
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from quantai.risk_manager import (CircuitBreaker, DailyTradeLimiter,
                                   EmergencyState, PositionSizer,
                                   StopOutCooldown)


class FakeAccount:
    def __init__(self, balance, position_profit=0.0):
        self.balance = balance
        self.position_profit = position_profit


def make_sizer(equity=200000.0, last_price=5000.0, daily_loss=None):
    return PositionSizer(
        account_fn=lambda: FakeAccount(equity),
        last_price_fn=lambda: last_price,
        equity_fn=lambda: equity,
        daily_loss_fn=lambda: daily_loss,
    )


# ---------- StopOutCooldown ----------

class TestStopOutCooldown:
    def test_record_sets_state(self):
        soc = StopOutCooldown(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        soc.record("LONG")
        assert soc.last_stopout_dir == "LONG"
        assert soc.last_stopout_time == datetime(2026, 8, 28, 10, 0, 0)

    def test_check_different_direction_not_blocked(self):
        soc = StopOutCooldown()
        soc.record("LONG", when=datetime(2026, 8, 28, 10, 0, 0))
        blocked, elapsed, remaining = soc.check(
            "SHORT", now=datetime(2026, 8, 28, 10, 5, 0))
        assert blocked is False

    def test_check_same_direction_within_cooldown(self):
        soc = StopOutCooldown()
        soc.record("LONG", when=datetime(2026, 8, 28, 10, 0, 0))
        blocked, elapsed, remaining = soc.check(
            "LONG", now=datetime(2026, 8, 28, 10, 5, 0))
        assert blocked is True
        assert elapsed == 300.0
        assert remaining == 600.0  # 900 - 300

    def test_check_after_cooldown_expired(self):
        soc = StopOutCooldown()
        soc.record("LONG", when=datetime(2026, 8, 28, 10, 0, 0))
        blocked, _, _ = soc.check("LONG", now=datetime(2026, 8, 28, 10, 16, 0))
        assert blocked is False

    def test_check_never_recorded(self):
        soc = StopOutCooldown()
        blocked, _, _ = soc.check("LONG", now=datetime(2026, 8, 28, 10, 0, 0))
        assert blocked is False  # last_stopout_dir=None ≠ LONG

    def test_check_none_direction(self):
        soc = StopOutCooldown()
        soc.record("LONG", when=datetime(2026, 8, 28, 10, 0, 0))
        blocked, _, _ = soc.check(None, now=datetime(2026, 8, 28, 10, 1, 0))
        assert blocked is False  # 真源 `if action_dir and ...` 守卫


# ---------- DailyTradeLimiter ----------

class TestDailyTradeLimiter:
    def test_initial_check(self):
        lim = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        blocked, reason = lim.check()
        assert blocked is False
        assert reason == "今日开仓 0/6 次"

    def test_bump_and_limit(self):
        lim = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        for _ in range(6):
            lim.bump()
        blocked, reason = lim.check()
        assert blocked is True
        assert reason == ("今日已开仓 6 次 (≥6)，触发日次数上限，禁止新开仓")

    def test_cross_day_reset(self):
        now = {"v": datetime(2026, 8, 28, 10, 0, 0)}
        lim = DailyTradeLimiter(now_fn=lambda: now["v"])
        for _ in range(6):
            lim.bump()
        assert lim.check()[0] is True
        now["v"] = datetime(2026, 8, 29, 9, 0, 0)  # 次日
        blocked, reason = lim.check()
        assert blocked is False
        assert reason == "今日开仓 0/6 次"

    def test_restore_same_day(self):
        lim = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        lim.restore(4, "2026-08-28")
        blocked, reason = lim.check()
        assert blocked is False
        assert reason == "今日开仓 4/6 次"

    def test_restore_other_day_ignored(self):
        lim = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        lim.restore(4, "2026-08-27")
        assert lim.check() == (False, "今日开仓 0/6 次")

    def test_restore_invalid_date_ignored(self):
        lim = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        lim.restore(4, "not-a-date")
        assert lim.check() == (False, "今日开仓 0/6 次")

    def test_restore_zero_or_negative_ignored(self):
        lim = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        lim.restore(0, "2026-08-28")
        assert lim.check() == (False, "今日开仓 0/6 次")


# ---------- PositionSizer ----------

class TestPositionSizer:
    def test_get_max_lots_account_none(self):
        sizer = PositionSizer(account_fn=lambda: None, last_price_fn=lambda: 5000.0,
                              equity_fn=lambda: 0.0, daily_loss_fn=lambda: None)
        assert sizer.get_max_lots() == 0

    def test_get_max_lots_price_invalid(self):
        sizer = make_sizer(last_price=0.0)
        assert sizer.get_max_lots() == 0

    def test_get_max_lots_hand_computed(self):
        # balance=1,000,000 → margin/lot = 5000×200×0.15 = 150,000
        # max_lots = 6, max_lots_safe = int(600,000 // 150,000) = 4 → min = 4
        sizer = make_sizer(equity=1000000.0, last_price=5000.0)
        assert sizer.get_max_lots() == 4

    def test_max_lots_by_risk_over_budget(self):
        # 200,000 × 1% / (20 × 200) = 2000/4000 = 0.5 → int = 0
        sizer = make_sizer(equity=200000.0)
        assert sizer.max_lots_by_risk(20.0) == 0

    def test_max_lots_by_risk_exact_one(self):
        # 200,000 × 1% / (10 × 200) = 1.0 → 1
        sizer = make_sizer(equity=200000.0)
        assert sizer.max_lots_by_risk(10.0) == 1

    def test_max_lots_by_risk_invalid_inputs(self):
        sizer = make_sizer(equity=0.0)
        assert sizer.max_lots_by_risk(10.0) == 0
        sizer = make_sizer(equity=200000.0)
        assert sizer.max_lots_by_risk(0.0) == 0

    def test_get_risk_scale_no_history(self):
        sizer = make_sizer(daily_loss=None)
        assert sizer.get_risk_scale() == 1.0

    def test_get_risk_scale_below_threshold(self):
        # dd = 1000/200000 = 0.5% < 0.9% → 1.0
        sizer = make_sizer(equity=200000.0, daily_loss=-1000.0)
        assert sizer.get_risk_scale() == 1.0

    def test_get_risk_scale_halved(self):
        # dd = 2000/200000 = 1.0% ≥ 0.9% → 0.5
        sizer = make_sizer(equity=200000.0, daily_loss=-2000.0)
        assert sizer.get_risk_scale() == 0.5

    def test_get_risk_scale_profit_no_halving(self):
        sizer = make_sizer(equity=200000.0, daily_loss=500.0)
        assert sizer.get_risk_scale() == 1.0

    def test_apply_risk_scale_rejected(self, caplog):
        # sl=20 → risk_lots=0 → 拒绝
        sizer = make_sizer(equity=200000.0)
        assert sizer.apply_risk_scale(2, 20.0) == 0

    def test_apply_risk_scale_clamped(self):
        # sl=10 → risk_lots=1；请求 3 手 → 1
        sizer = make_sizer(equity=200000.0)
        assert sizer.apply_risk_scale(3, 10.0) == 1

    def test_apply_risk_scale_halved(self):
        # daily_loss=-2000 → scale=0.5；sl=10 → risk_lots=1... 需要更大权益让 risk_lots≥2
        # equity=1,000,000: risk_lots = 10000/(10×200) = 5；请求 4 手 → 降档 2
        sizer = make_sizer(equity=1000000.0, daily_loss=-20000.0)  # dd=2% ≥ 0.9%
        assert sizer.apply_risk_scale(4, 10.0) == 2

    def test_apply_risk_scale_halve_floor_one(self):
        # 降档后 max(1, int(v×0.5))：请求 1 手 → 仍 1
        sizer = make_sizer(equity=1000000.0, daily_loss=-20000.0)
        assert sizer.apply_risk_scale(1, 10.0) == 1


# ---------- CircuitBreaker ----------

class FakeEquityAccount:
    def __init__(self, balance, position_profit=0.0):
        self.balance = balance
        self.position_profit = position_profit


class TestCircuitBreaker:
    def test_check_no_history(self):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        assert cb.check() == (False, "无交易历史")

    def test_record_three_consecutive_losses_blocks(self):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        cb.record_trade_result(-500.0)
        cb.record_trade_result(-600.0)
        cb.record_trade_result(-700.0)
        blocked, reason = cb.check()
        assert blocked is True
        assert "今日连亏 3 笔 (≥3)" in reason

    def test_profit_resets_today_cl(self):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        cb.record_trade_result(-500.0)
        cb.record_trade_result(-500.0)
        cb.record_trade_result(300.0)   # 盈利清零当日连亏
        cb.record_trade_result(-500.0)
        cb.record_trade_result(-500.0)
        blocked, _ = cb.check()
        assert blocked is False  # 当日连亏仅 2

    def test_daily_loss_circuit(self):
        # 日亏 -4000 / 权益 200,000 = 2% ≥ 1.5% → blocked
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        cb.record_trade_result(-2000.0)
        cb.record_trade_result(-2000.0)
        blocked, reason = cb.check()
        assert blocked is True
        assert "≥ 1.5% 权益" in reason
        assert "(2.00%)" in reason

    def test_cross_day_lazy_reset(self):
        now = {"v": datetime(2026, 8, 28, 15, 0, 0)}
        cb = CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: now["v"])
        cb.record_trade_result(-4000.0)   # 触发日亏熔断
        assert cb.check()[0] is True
        now["v"] = datetime(2026, 8, 29, 9, 30, 0)  # 次日
        blocked, reason = cb.check()
        assert blocked is False
        assert reason == "熔断未触发"     # 跨日惰性重置（防死锁）

    def test_save_and_load_roundtrip(self, tmp_path):
        f = str(tmp_path / "cb.json")
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0),
                            state_file=f)
        cb.record_trade_result(-500.0)
        cb.record_trade_result(-500.0)
        # 新实例同日恢复
        cb2 = CircuitBreaker(equity_fn=lambda: 200000.0,
                             now_fn=lambda: datetime(2026, 8, 28, 11, 0, 0),
                             state_file=f)
        cb2.load_state()
        blocked, reason = cb2.check()
        assert blocked is False
        assert "今日连亏 2 笔" not in reason
        assert cb2._today_cl == 2        # 同日重启不绕过熔断计数（M8 保护）
        assert cb2._daily_loss == -1000.0

    def test_load_cross_day_resets_today_cl(self, tmp_path):
        import json
        f = str(tmp_path / "cb.json")
        with open(f, "w", encoding="utf-8") as fp:
            json.dump({
                "consecutive_losses": 5,
                "daily_loss": -3000.0,
                "daily_loss_date": "2026-08-27",
                "consecutive_losses_date": "2026-08-27",
                "today_consecutive_losses": 3,
                "today_cl_date": "2026-08-27",   # 非今日 → 当日连亏清零
            }, fp, ensure_ascii=False)
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0),
                            state_file=f)
        cb.load_state()
        assert cb._today_cl == 0
        assert cb._consecutive_losses == 5   # 跨日累计仅统计

    def test_load_missing_file_no_state(self, tmp_path):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0),
                            state_file=str(tmp_path / "none.json"))
        cb.load_state()
        assert cb.check() == (False, "无交易历史")

    def test_daily_loss_property(self):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0)
        assert cb.daily_loss is None       # 未记录前 None（hasattr 语义）
        cb.record_trade_result(-100.0)
        assert cb.daily_loss == -100.0


# ---------- EmergencyState ----------

class TestEmergencyState:
    def test_activate_deactivate(self):
        es = EmergencyState()
        assert es.mode is False
        assert es.enter_time is None
        es.activate(when=datetime(2026, 8, 28, 10, 0, 0))
        assert es.mode is True
        assert es.enter_time == datetime(2026, 8, 28, 10, 0, 0)
        es.deactivate()
        assert es.mode is False

    def test_activate_default_now(self):
        es = EmergencyState()
        es.activate()
        assert es.mode is True
        assert es.enter_time is not None
