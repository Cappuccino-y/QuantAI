"""order_executor 单测（阶段 4）— 行为对拍真源 L2959–3404。

覆盖:
- execute_order_safe: 正常成交 / REJECTED / 部分成交 / BUY 抢成交加价（ask1+2tick）
- notify_order_filled: OPEN 带止损止盈盈亏比 / CLOSE 死键 last_pnl（恒无盈亏行）
- cancel_all_orders: 无活跃单 / 有活跃单撤销
- close_position: 无持仓 / 成交（pnl 手算 + 状态清理 + 熔断/绩效记录）/ 失败
- emergency_close: 失败重试直到成功 + emergency 状态切换
"""
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

import quantai.order_executor as oe_mod
from quantai.order_executor import OrderExecutor
from quantai.position_manager import PositionManager
from quantai.risk_manager import CircuitBreaker, EmergencyState


# ---------- 测试替身 ----------

class FakeQuote:
    def __init__(self, last=5000.0, ask=5000.2, bid=4999.8,
                 upper=5500.0, lower=4500.0, settlement=4990.0):
        self.last_price = last
        self.ask_price1 = ask
        self.bid_price1 = bid
        self.upper_limit = upper
        self.lower_limit = lower
        self.settlement = settlement


class FakeOrder:
    def __init__(self, status="FINISHED", volume_left=0, trade_price=5000.4,
                 is_error=False, last_msg=""):
        self.order_id = "ORD-1"
        self.status = status
        self.volume_left = volume_left
        self.trade_price = trade_price
        self.is_error = is_error
        self.last_msg = last_msg


class FakeApi:
    def __init__(self, quote=None, order=None, account=None, cloud_pos=None):
        self.quote = quote or FakeQuote()
        self.order = order or FakeOrder()
        self.account = account
        self.cloud_pos = cloud_pos
        self.inserted = []
        self.cancelled = []

    def get_quote(self, symbol):
        return self.quote

    def wait_update(self, deadline=None):
        pass

    def insert_order(self, symbol, direction, offset, volume, price):
        self.inserted.append((symbol, direction, offset, volume, price))
        return self.order

    def cancel_order(self, order):
        self.cancelled.append(order)

    def get_account(self):
        return self.account

    def get_position(self, symbol):
        return self.cloud_pos


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


class FakeMetrics:
    def __init__(self):
        self.trades = []
        self.equities = []

    def record_trade(self, **kwargs):
        self.trades.append(kwargs)

    def update_equity(self, balance, when=None):
        self.equities.append(balance)


def make_oe(api, pm=None, notifier=None, cb=None, metrics=None, emergency=None,
            symbol="CFFEX.IM2608", atr5=20.0):
    return OrderExecutor(
        api=api,
        quote_fn=lambda: api.quote,
        atr5_fn=lambda: atr5,
        symbol_fn=lambda: symbol,
        logger=FakeLogger(),
        notifier=notifier or FakeNotifier(),
        position_manager=pm,
        circuit_breaker=cb,
        metrics=metrics,
        emergency=emergency,
    )


def make_pm(tmp_path):
    return PositionManager(position_file=str(tmp_path / "p.pkl"),
                           now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))


# ---------- execute_order_safe ----------

class TestExecuteOrderSafe:
    def test_happy_path_full_fill(self, tmp_path):
        api = FakeApi(order=FakeOrder(trade_price=5000.6))
        oe = make_oe(api)
        price = oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.0)
        assert price == 5000.6
        assert len(api.inserted) == 1
        assert oe._orders == []   # 成交后移除

    def test_rejected_returns_none(self, tmp_path):
        api = FakeApi(order=FakeOrder(status="REJECTED", last_msg="资金不足"))
        oe = make_oe(api)
        assert oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.0) is None

    def test_error_order_returns_none(self, tmp_path):
        api = FakeApi(order=FakeOrder(is_error=True, last_msg="合约不存在"))
        oe = make_oe(api)
        assert oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.0) is None

    def test_partial_fill_returns_none(self, tmp_path):
        api = FakeApi(order=FakeOrder(status="FINISHED", volume_left=1))
        oe = make_oe(api)
        assert oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 2, 5000.0) is None

    def test_buy_aggressive_pricing(self, tmp_path):
        """BUY 抢成交: 限价 5000 < ask1+2tick=5000.6，滑点 0.4 ≤ max_slip=10 → 修正。"""
        api = FakeApi(order=FakeOrder(trade_price=5000.6))
        oe = make_oe(api, atr5=20.0)   # max_slip = 20×0.5 = 10
        oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.0)
        assert api.inserted[0][4] == pytest.approx(5000.6)

    def test_buy_no_aggressive_when_slip_exceeded(self, tmp_path):
        """ask1 距 last 超过 max_slip → 不修正（保持原限价）。"""
        api = FakeApi(quote=FakeQuote(last=5000.0, ask=5015.0),
                      order=FakeOrder(trade_price=5015.0))
        oe = make_oe(api, atr5=20.0)   # max_slip=10 < 15
        oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 5010.0)
        assert api.inserted[0][4] == 5010.0

    def test_sell_aggressive_uses_bid1(self, tmp_path):
        """SELL 抢成交: bid1 本身（6/22 修复），限价 5000 > bid1=4999.8 → 修正。"""
        api = FakeApi(order=FakeOrder(trade_price=4999.8))
        oe = make_oe(api, atr5=20.0)
        oe.execute_order_safe("CFFEX.IM2608", "SELL", "CLOSE", 1, 5000.0)
        assert api.inserted[0][4] == pytest.approx(4999.8)

    def test_price_far_from_settlement_uses_limit_price(self, tmp_path):
        """限价严重偏离昨结（<0.5× 或 >2×）→ 涨跌停价限价（CFFEX 不支持市价单）。

        注: ask1=0 使抢成交段跳过（真源 `if direction == 'BUY' and quote.ask_price1 > 0`），
        限价 2000 直达昨结防呆分支。
        """
        api = FakeApi(quote=FakeQuote(ask=0.0, bid=0.0),
                      order=FakeOrder(trade_price=5500.0))
        oe = make_oe(api)
        oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 2000.0)  # < 4990×0.5
        assert api.inserted[0][4] == 5500.0   # upper_limit


# ---------- notify_order_filled ----------

class TestNotifyOrderFilled:
    def test_open_message_with_sl_tp(self, tmp_path):
        pm = make_pm(tmp_path)
        pm.position.update({"stop_loss": 4950.0, "take_profit": 5100.0})
        notifier = FakeNotifier()
        oe = make_oe(FakeApi(), pm=pm, notifier=notifier)
        oe.notify_order_filled("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.0, 5000.0)
        msg = notifier.sent[0]
        assert "✅ 成交: BUY OPEN CFFEX.IM2608 1手 @ 5000.00" in msg
        assert "止损 4950.00 (-50.0点)" in msg
        assert "止盈 5100.00 (+100.0点)" in msg
        assert "盈亏比 1:2.00" in msg    # 手算: 100/50

    def test_close_message_no_pnl_line(self, tmp_path):
        """真源死键 last_pnl 只读从不写入 → CLOSE 分支恒无盈亏行。"""
        pm = make_pm(tmp_path)
        notifier = FakeNotifier()
        oe = make_oe(FakeApi(), pm=pm, notifier=notifier)
        oe.notify_order_filled("CFFEX.IM2608", "SELL", "CLOSE", 1, 5000.0, 5000.0)
        msg = notifier.sent[0]
        assert "SELL CLOSE" in msg
        assert "盈亏" not in msg

    def test_no_pm_no_crash(self):
        oe = make_oe(FakeApi(), pm=None, notifier=FakeNotifier())
        oe.notify_order_filled("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.0, 5000.0)


# ---------- cancel_all_orders ----------

class TestCancelAllOrders:
    def test_no_alive_orders(self, tmp_path, caplog):
        oe = make_oe(FakeApi())
        oe.cancel_all_orders()   # 不抛异常，_orders 为空
        assert oe._orders == []

    def test_cancels_alive_orders(self, tmp_path):
        api = FakeApi()
        oe = make_oe(api)
        o1 = FakeOrder(status="ALIVE")
        o2 = FakeOrder(status="FINISHED")
        oe._orders = [o1, o2]
        oe.cancel_all_orders()
        assert api.cancelled == [o1]
        # 清理后仅保留仍 ALIVE 的（假单状态未变 → o1 保留）
        assert o1 in oe._orders
        assert o2 not in oe._orders


# ---------- close_position ----------

class TestClosePosition:
    def _make_filled_pm(self, tmp_path):
        pm = make_pm(tmp_path)
        pm.position.update({
            "direction": "LONG", "volume": 2, "entry_price": 5000.0,
            "stop_loss": 4950.0, "take_profit": 5100.0,
            "last_ai_decision": "test", "entry_time": "2026-08-28 09:31:00",
        })
        pm.conditional_order = {"action": "BUY"}
        return pm

    def test_no_cloud_position_returns_true(self, tmp_path):
        pm = self._make_filled_pm(tmp_path)
        api = FakeApi(cloud_pos=SimpleNamespace(volume_long=0, volume_short=0))
        oe = make_oe(api, pm=pm)
        assert oe.close_position("测试") is True
        assert pm.position["direction"] == "LONG"   # 状态不动

    def test_successful_close_full_orchestration(self, tmp_path):
        pm = self._make_filled_pm(tmp_path)
        cb = CircuitBreaker(equity_fn=lambda: 200000.0)
        metrics = FakeMetrics()
        notifier = FakeNotifier()
        api = FakeApi(account=SimpleNamespace(balance=190000.0, position_profit=0.0),
                      cloud_pos=SimpleNamespace(volume_long=2, volume_short=0,
                                                open_price_long=5000.0))
        oe = make_oe(api, pm=pm, cb=cb, metrics=metrics, notifier=notifier)
        # 平多: SELL @ 4960 → pnl = (4960-5000)×2×200 = -16000
        monkey_avg = 4960.0
        oe.execute_order_safe = lambda **kw: monkey_avg
        assert oe.close_position("止损触发") is True
        # pnl 手算
        assert metrics.trades[0]["pnl"] == pytest.approx(-16000.0)
        assert metrics.trades[0]["direction"] == "LONG"
        assert metrics.trades[0]["volume"] == 2
        assert metrics.trades[0]["entry_price"] == 5000.0
        assert metrics.trades[0]["entry_time"] == datetime(2026, 8, 28, 9, 31, 0)
        assert metrics.equities == [190000.0]
        # 熔断记录
        assert cb.daily_loss == pytest.approx(-16000.0)
        # 持仓清空（先清再存的 6/17+6/22 修复）
        assert pm.position["direction"] is None
        assert pm.position["volume"] == 0
        assert pm.position["entry_time"] is None
        assert pm.conditional_order is None
        # 通知
        assert any("IM平仓成功" in m and "-16000" in m for m in notifier.sent)

    def test_close_failure_returns_false(self, tmp_path):
        pm = self._make_filled_pm(tmp_path)
        notifier = FakeNotifier()
        api = FakeApi(cloud_pos=SimpleNamespace(volume_long=2, volume_short=0))
        oe = make_oe(api, pm=pm, notifier=notifier)
        oe.execute_order_safe = lambda **kw: None
        assert oe.close_position("止损触发") is False
        assert any("IM平仓失败" in m for m in notifier.sent)
        assert pm.position["direction"] == "LONG"   # 失败不清仓

    def test_closing_guard(self, tmp_path):
        pm = self._make_filled_pm(tmp_path)
        api = FakeApi(cloud_pos=SimpleNamespace(volume_long=2, volume_short=0))
        oe = make_oe(api, pm=pm)
        oe._closing = True
        assert oe.close_position("测试") is False
        assert oe.is_closing is True
        oe._closing = False

    def test_entry_time_string_parse_fallback(self, tmp_path):
        """entry_time 字符串双格式解析 + 缺失回退 last_entry_time。"""
        pm = make_pm(tmp_path)
        pm.last_entry_time = datetime(2026, 8, 28, 9, 35, 0)
        pm.position.update({"direction": "SHORT", "volume": 1, "entry_price": 5000.0})
        metrics = FakeMetrics()
        api = FakeApi(account=SimpleNamespace(balance=100000.0, position_profit=0.0),
                      cloud_pos=SimpleNamespace(volume_long=0, volume_short=1,
                                                open_price_short=5000.0))
        oe = make_oe(api, pm=pm, metrics=metrics)
        oe.execute_order_safe = lambda **kw: 4990.0   # 平空盈利 10 点 × 200
        assert oe.close_position("止盈触发") is True
        assert metrics.trades[0]["entry_time"] == datetime(2026, 8, 28, 9, 35, 0)
        assert metrics.trades[0]["pnl"] == pytest.approx(2000.0)   # (5000-4990)×1×200


# ---------- emergency_close ----------

class TestEmergencyClose:
    def test_retries_until_success(self, tmp_path, monkeypatch):
        pm = make_pm(tmp_path)
        emergency = EmergencyState()
        oe = make_oe(FakeApi(), pm=pm, emergency=emergency)
        calls = {"n": 0}

        def fake_close(reason, is_emergency=False):
            calls["n"] += 1
            assert is_emergency is True
            assert "(应急)" in reason
            return calls["n"] >= 2   # 第 2 次成功

        monkeypatch.setattr(oe, "close_position", fake_close)
        monkeypatch.setattr(oe_mod.time, "sleep", lambda s: None)
        notifier = oe.notifier
        oe.emergency_close("止损触发")
        assert calls["n"] == 2
        assert emergency.mode is False   # 完成后复位
        assert any("应急平仓启动" in m for m in notifier.sent)
        assert any("应急平仓完成" in m for m in notifier.sent)
