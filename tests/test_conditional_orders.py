"""conditional_orders 单测（阶段 4）— 行为对拍真源 L4944–5324。

覆盖:
- 守卫链: 无条件单 / emergency_mode / 非交易时段 / 未触发
- 过期校验（8/27）/ 偏差检查（方向感知: 不利拒、有利放）/ 上限 30 点
- 八道拦截: 过滤器拒绝（8/26 清除修复）/ 熔断 / 尾盘 / 日次数 / 止损冷却
- 新开仓: entry_time 字符串写入 + last_entry_time 同步（8/27）+ bump
- 同向加仓: 均价合并手算
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from quantai.conditional_orders import ConditionalOrderChecker
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
    def __init__(self, last=5001.0, ask=5001.2, bid=5000.8):
        self.symbol = "CFFEX.IM2608"
        self.im_quote = SimpleNamespace(last_price=last, ask_price1=ask,
                                        bid_price1=bid)
        self.api = SimpleNamespace(wait_update=lambda deadline=None: None,
                                   get_account=lambda: SimpleNamespace(
                                       balance=200000.0, position_profit=0.0))


class FakeMcs:
    def __init__(self, atr5=20.0, atr15=50.0):
        self.atr_5 = atr5
        self.atr_15 = atr15


class FakeCalendar:
    def __init__(self, trading=True):
        self.trading = trading

    def is_trading_time(self):
        return self.trading


class AllPassFilters:
    def check_trend_alignment(self, direction):
        return FilterResult(allowed=True, reason="trend ok")

    def check_session_extremes(self, entry_price, direction):
        return FilterResult(allowed=True, reason="session ok")

    def confirm_breakout_bar(self, trigger_type, trigger_price, direction):
        return FilterResult(allowed=True, reason="breakout ok")

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
    """全失败豁免（vwap 不通过 → 不进入豁免分支，真源 L5142-5144）。"""

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
        return volume   # 直通（风险兜底单测见 test_risk_manager）


class FakeOE:
    def __init__(self, retry_price=5001.0):
        self.retry_price = retry_price
        self.retry_calls = []
        self.close_calls = []

    def execute_market_order_with_retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        return self.retry_price

    def close_position(self, reason, is_emergency=False):
        self.close_calls.append(reason)
        return True


def make_cond(**overrides):
    cond = {"action": "BUY", "trigger_type": "PRICE_ABOVE",
            "trigger_price": 5000.0, "stop_loss": 4960.0,
            "take_profit": 5100.0, "volume": 2, "reason": "test",
            "created_date": "2026-08-28"}
    cond.update(overrides)
    return cond


def make_checker(tmp_path, *, cond=None, mds=None, mcs=None, calendar=None,
                 filters=None, exemptions=None, cb=None, stopout=None,
                 limiter=None, oe=None, emergency=None, tail=(False, ""),
                 now=datetime(2026, 8, 28, 10, 0, 0)):
    pm = PositionManager(position_file=str(tmp_path / "p.pkl"), now_fn=lambda: now)
    pm.conditional_order = cond if cond is not None else make_cond()
    return (ConditionalOrderChecker(
        pm=pm, mds=mds or FakeMds(), mcs=mcs or FakeMcs(),
        calendar=calendar or FakeCalendar(),
        filters=filters or AllPassFilters(),
        exemptions=exemptions or AllPassExemptions(),
        sizer=FakeSizer(), daily_limiter=limiter or DailyTradeLimiter(lambda: now),
        circuit_breaker=cb or CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: now),
        stopout=stopout or StopOutCooldown(lambda: now),
        oe=oe or FakeOE(), emergency=emergency or EmergencyState(),
        tail_fn=lambda: tail, notifier=FakeNotifier(), logger=FakeLogger(),
        now_fn=lambda: now,
    ), pm)


# ---------- 守卫链 ----------

class TestGuards:
    def test_no_conditional_noop(self, tmp_path):
        chk, pm = make_checker(tmp_path, cond=None)
        chk.check_conditional_order()
        assert pm.conditional_order is None

    def test_emergency_mode_noop(self, tmp_path):
        es = EmergencyState()
        es.activate()
        chk, pm = make_checker(tmp_path, emergency=es)
        chk.check_conditional_order()
        assert pm.conditional_order is not None   # 未消费

    def test_not_trading_time_noop(self, tmp_path):
        chk, pm = make_checker(tmp_path, calendar=FakeCalendar(trading=False))
        chk.check_conditional_order()
        assert pm.conditional_order is not None

    def test_not_triggered_noop(self, tmp_path):
        # PRICE_ABOVE 5000 但现价 4990 → 不触发
        chk, pm = make_checker(tmp_path, mds=FakeMds(last=4990.0, ask=4990.2))
        chk.check_conditional_order()
        assert pm.conditional_order is not None

    def test_price_below_triggered(self, tmp_path):
        cond = make_cond(trigger_type="PRICE_BELOW", trigger_price=5010.0,
                         stop_loss=5050.0, take_profit=4900.0)
        chk, pm = make_checker(tmp_path, cond=cond,
                               mds=FakeMds(last=5009.0, ask=5009.2, bid=5008.8))
        chk.check_conditional_order()
        assert pm.position["direction"] == "LONG"   # 触发并开仓


# ---------- 过期与偏差 ----------

class TestExpiryAndDeviation:
    def test_expired_conditional_cancelled(self, tmp_path):
        cond = make_cond(created_date="2026-08-27")   # 昨日
        chk, pm = make_checker(tmp_path, cond=cond)
        chk.check_conditional_order()
        assert pm.conditional_order is None
        assert any("过期条件单已自动取消" in m for m in chk.notifier.sent)

    def test_adverse_deviation_too_large_cancelled(self, tmp_path):
        """BUY: 对手价 5050 - 触发价 5000 = 50 > min(1.0×20, 30)=20 → 放弃。"""
        chk, pm = make_checker(tmp_path, mds=FakeMds(last=5050.0, ask=5050.0))
        chk.check_conditional_order()
        assert pm.conditional_order is None
        assert any("滑点过大" in m for m in chk.notifier.sent)

    def test_favorable_deviation_passes(self, tmp_path):
        """SELL: 触发价 5000，bid 4990 → adverse = max(0, 4990-5000) = 0 → 有利放行。"""
        cond = make_cond(action="SELL", trigger_type="PRICE_BELOW",
                         stop_loss=5040.0, take_profit=4900.0)
        chk, pm = make_checker(tmp_path, cond=cond,
                               mds=FakeMds(last=4990.0, ask=4990.2, bid=4990.0))
        chk.check_conditional_order()
        assert pm.position["direction"] == "SHORT"

    def test_tolerance_capped_at_30(self, tmp_path):
        """atr_5=100 → 容差 min(100, 30)=30；偏差 40 > 30 → 放弃。"""
        chk, pm = make_checker(tmp_path, mcs=FakeMcs(atr5=100.0),
                               mds=FakeMds(last=5040.0, ask=5040.0))
        chk.check_conditional_order()
        assert pm.conditional_order is None


# ---------- 拦截链 ----------

class TestInterceptions:
    def test_filter_rejection_cancels(self, tmp_path):
        """8/26 修复: 拒绝路径必须真正清除条件单。"""
        class RejectTrend(AllPassFilters):
            def check_trend_alignment(self, direction):
                return FilterResult(allowed=False, reason="60min 方向未确认")

        chk, pm = make_checker(tmp_path, filters=RejectTrend(),
                               exemptions=AllFailExemptions())
        chk.check_conditional_order()
        assert pm.conditional_order is None
        assert any("假突破过滤器拦截" in m for m in chk.notifier.sent)

    def test_filter_rejection_saved_by_exemptions(self, tmp_path):
        """≥2 豁免通过 → 放行开仓。"""
        class RejectTrend(AllPassFilters):
            def check_trend_alignment(self, direction):
                return FilterResult(allowed=False, reason="60min 方向未确认")

        chk, pm = make_checker(tmp_path, filters=RejectTrend())   # 豁免全通过
        chk.check_conditional_order()
        assert pm.position["direction"] == "LONG"   # 豁免通过继续开仓

    def test_circuit_breaker_cancels(self, tmp_path):
        cb = CircuitBreaker(equity_fn=lambda: 200000.0,
                            now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        cb.record_trade_result(-500.0)
        cb.record_trade_result(-500.0)
        cb.record_trade_result(-500.0)   # 当日连亏 3
        chk, pm = make_checker(tmp_path, cb=cb)
        chk.check_conditional_order()
        assert pm.conditional_order is None   # 8/17 修复: 熔断拦截后清除
        assert any("熔断拦截" in m for m in chk.notifier.sent)

    def test_tail_session_cancels(self, tmp_path):
        chk, pm = make_checker(tmp_path, tail=(True, "尾盘时段 14:45 后禁止开仓"))
        chk.check_conditional_order()
        assert pm.conditional_order is None

    def test_daily_limit_cancels(self, tmp_path):
        lim = DailyTradeLimiter(lambda: datetime(2026, 8, 28, 10, 0, 0))
        for _ in range(6):
            lim.bump()
        chk, pm = make_checker(tmp_path, limiter=lim)
        chk.check_conditional_order()
        assert pm.conditional_order is None

    def test_stopout_cooldown_cancels(self, tmp_path):
        now = datetime(2026, 8, 28, 10, 0, 0)
        stopout = StopOutCooldown(lambda: now)
        stopout.record("LONG", when=now - timedelta(seconds=300))   # 5 分钟前止损
        chk, pm = make_checker(tmp_path, stopout=stopout)   # cond_dir=LONG 同向
        chk.check_conditional_order()
        assert pm.conditional_order is None
        assert any("止损冷却拦截" in m for m in chk.notifier.sent)


# ---------- 执行路径 ----------

class TestExecution:
    def test_new_open_success(self, tmp_path):
        now = datetime(2026, 8, 28, 10, 0, 0)
        oe = FakeOE(retry_price=5001.0)
        lim = DailyTradeLimiter(lambda: now)
        chk, pm = make_checker(tmp_path, oe=oe, limiter=lim, now=now)
        chk.check_conditional_order()
        pos = pm.position
        assert pos["direction"] == "LONG"
        assert pos["volume"] == 2                     # min(cond.volume=2, max_lots=3)
        assert pos["entry_price"] == 5001.0
        assert pos["stop_loss"] == 4960.0
        assert pos["take_profit"] == 5100.0
        assert pos["entry_time"] == "2026-08-28 10:00:00"   # 字符串格式（P0 修复）
        assert pm.last_entry_time == now               # 8/27 同步
        assert lim.check() == (False, "今日开仓 1/6 次")   # bump 生效
        assert pm.conditional_order is None            # 触发前已清除
        assert oe.retry_calls[0]["base_market_price"] == 5001.2   # ask1
        assert oe.retry_calls[0]["tolerance"] == 20.0  # 1.0×atr_5
        assert any("条件单入场" in m for m in chk.notifier.sent)

    def test_add_position_merges_avg_price(self, tmp_path):
        now = datetime(2026, 8, 28, 10, 0, 0)
        pm = PositionManager(position_file=str(tmp_path / "p.pkl"), now_fn=lambda: now)
        pm.position.update({"direction": "LONG", "volume": 1, "entry_price": 5000.0,
                            "stop_loss": 4960.0, "take_profit": 5100.0})
        pm.conditional_order = make_cond()
        oe = FakeOE(retry_price=5001.0)
        chk = ConditionalOrderChecker(
            pm=pm, mds=FakeMds(), mcs=FakeMcs(), calendar=FakeCalendar(),
            filters=AllPassFilters(), exemptions=AllPassExemptions(),
            sizer=FakeSizer(), daily_limiter=DailyTradeLimiter(lambda: now),
            circuit_breaker=CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: now),
            stopout=StopOutCooldown(lambda: now), oe=oe, emergency=EmergencyState(),
            tail_fn=lambda: (False, ""), notifier=FakeNotifier(), logger=FakeLogger(),
            now_fn=lambda: now,
        )
        chk.check_conditional_order()
        # 加仓: volume=2, available=3-1=2 → new_vol=3
        # new_avg = (5000×1 + 5001×2)/3 = 15002/3 = 5000.666...
        assert pm.position["volume"] == 3
        assert pm.position["entry_price"] == pytest.approx(15002.0 / 3.0)
        assert any("条件单同向加仓" in m for m in chk.notifier.sent)

    def test_add_position_insufficient_funds(self, tmp_path):
        now = datetime(2026, 8, 28, 10, 0, 0)
        pm = PositionManager(position_file=str(tmp_path / "p.pkl"), now_fn=lambda: now)
        pm.position.update({"direction": "LONG", "volume": 3, "entry_price": 5000.0})
        pm.conditional_order = make_cond()
        chk = ConditionalOrderChecker(
            pm=pm, mds=FakeMds(), mcs=FakeMcs(), calendar=FakeCalendar(),
            filters=AllPassFilters(), exemptions=AllPassExemptions(),
            sizer=FakeSizer(max_lots=3), daily_limiter=DailyTradeLimiter(lambda: now),
            circuit_breaker=CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: now),
            stopout=StopOutCooldown(lambda: now), oe=FakeOE(),
            emergency=EmergencyState(), tail_fn=lambda: (False, ""),
            notifier=FakeNotifier(), logger=FakeLogger(), now_fn=lambda: now,
        )
        chk.check_conditional_order()
        assert pm.position["volume"] == 3   # 资金不足 → 不加仓
        assert any("资金不足" in m for m in chk.notifier.sent)

    def test_reverse_position_closed_first(self, tmp_path):
        now = datetime(2026, 8, 28, 10, 0, 0)
        pm = PositionManager(position_file=str(tmp_path / "p.pkl"), now_fn=lambda: now)
        pm.position.update({"direction": "SHORT", "volume": 1, "entry_price": 5000.0,
                            "stop_loss": 5050.0, "take_profit": 4900.0})
        pm.conditional_order = make_cond()
        oe = FakeOE(retry_price=5001.0)

        def fake_close(reason, is_emergency=False):
            oe.close_calls.append(reason)
            # 真实 close_position 会清空 current_position（模拟）
            pm.position.update({"direction": None, "volume": 0,
                                "entry_price": 0.0, "stop_loss": 0.0,
                                "take_profit": 0.0, "last_ai_decision": None,
                                "entry_time": None})
            return True

        oe.close_position = fake_close
        chk = ConditionalOrderChecker(
            pm=pm, mds=FakeMds(), mcs=FakeMcs(), calendar=FakeCalendar(),
            filters=AllPassFilters(), exemptions=AllPassExemptions(),
            sizer=FakeSizer(), daily_limiter=DailyTradeLimiter(lambda: now),
            circuit_breaker=CircuitBreaker(equity_fn=lambda: 200000.0, now_fn=lambda: now),
            stopout=StopOutCooldown(lambda: now), oe=oe, emergency=EmergencyState(),
            tail_fn=lambda: (False, ""), notifier=FakeNotifier(), logger=FakeLogger(),
            now_fn=lambda: now,
        )
        chk.check_conditional_order()
        assert oe.close_calls == ["条件单开仓前平反向仓位"]
        assert pm.position["direction"] == "LONG"   # 平仓后新开
