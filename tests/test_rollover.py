"""rollover_manager 单测（阶段 4）— 行为对拍真源 L3407–3518。

覆盖:
- get_next_dominant_im: 月份递增 / 12 月跨年
- rollover_if_needed: 未到期 / 无持仓 / 同合约跳过 / 完整换月（基差偏移手算 +
  symbol 双服务同步 + im_quote 切换）/ 平仓失败终止 / 开仓失败应急
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from quantai.position_manager import PositionManager
from quantai.risk_manager import EmergencyState
from quantai.rollover_manager import RolloverManager


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
    def __init__(self, symbol="CFFEX.IM2608", days_to_expiry=30, index_price=5010.0):
        self.symbol = symbol
        self.im_quote = SimpleNamespace(last_price=5000.0)
        self.index_price = index_price
        self._days = days_to_expiry

    def get_basis_info(self):
        return {"days_to_expiry": self._days, "symbol": self.symbol,
                "index_price": self.index_price}


class FakeMcs:
    def __init__(self):
        self.symbol = "CFFEX.IM2608"


class FakeApi:
    def __init__(self, cloud_pos, old_last=5000.0, new_last=5010.0):
        self.cloud_pos = cloud_pos
        self.old_last = old_last
        self.new_last = new_last
        self.quotes = {}

    def get_position(self, symbol):
        return self.cloud_pos

    def get_quote(self, symbol):
        last = self.old_last if symbol.endswith("2608") else self.new_last
        q = SimpleNamespace(last_price=last, ask_price1=last + 0.2,
                            bid_price1=last - 0.2)
        self.quotes[symbol] = q
        return q

    def wait_update(self, deadline=None):
        pass

    def get_account(self):
        return SimpleNamespace(balance=200000.0, position_profit=0.0)


def make_pm(tmp_path):
    return PositionManager(position_file=str(tmp_path / "p.pkl"),
                           now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))


def make_rm(mds, api, pm, oe, emergency=None):
    return RolloverManager(mds=mds, mcs=FakeMcs(), api=api, pm=pm, oe=oe,
                           notifier=FakeNotifier(), logger=FakeLogger(),
                           emergency=emergency or EmergencyState())


# ---------- get_next_dominant_im（真源 L3508–3518） ----------

class TestGetNextDominantIm:
    def test_next_month(self):
        mds = FakeMds(symbol="CFFEX.IM2608")
        rm = make_rm(mds, None, None, None)
        assert rm.get_next_dominant_im() == "CFFEX.IM2609"

    def test_december_cross_year(self):
        mds = FakeMds(symbol="CFFEX.IM2612")
        rm = make_rm(mds, None, None, None)
        assert rm.get_next_dominant_im() == "CFFEX.IM2701"


# ---------- rollover_if_needed（真源 L3407–3506） ----------

class TestRolloverIfNeeded:
    def _filled_pm(self, tmp_path):
        pm = make_pm(tmp_path)
        pm.position.update({"direction": "LONG", "volume": 1, "entry_price": 5000.0,
                            "stop_loss": 4950.0, "take_profit": 5100.0,
                            "last_ai_decision": "test"})
        return pm

    def test_not_near_expiry_noop(self, tmp_path):
        mds = FakeMds(days_to_expiry=3)
        api = FakeApi(SimpleNamespace(volume_long=1, volume_short=0))
        oe = SimpleNamespace(close_position=lambda *a, **k: pytest.fail("不应平仓"))
        rm = make_rm(mds, api, self._filled_pm(tmp_path), oe)
        rm.rollover_if_needed()   # days > 2 → 直接返回

    def test_no_position_noop(self, tmp_path):
        mds = FakeMds(days_to_expiry=1)
        api = FakeApi(SimpleNamespace(volume_long=0, volume_short=0))
        oe = SimpleNamespace(close_position=lambda *a, **k: pytest.fail("不应平仓"))
        rm = make_rm(mds, api, make_pm(tmp_path), oe)
        rm.rollover_if_needed()   # 云端无持仓 → 直接返回

    def test_full_rollover_success(self, tmp_path):
        """完整换月: 基差偏移手算 + symbol 双服务同步。"""
        mds = FakeMds(days_to_expiry=1, index_price=5010.0)
        api = FakeApi(SimpleNamespace(volume_long=1, volume_short=0),
                      old_last=5000.0, new_last=5010.0)
        pm = self._filled_pm(tmp_path)
        emergency = EmergencyState()
        oe = SimpleNamespace(
            close_position=lambda reason, is_emergency=False: True,
            execute_order_safe=lambda **kw: 5010.4,   # 新合约成交价
        )
        rm = make_rm(mds, api, pm, oe, emergency)
        rm.rollover_if_needed()
        # 基差偏移: old_basis = 5000-5010 = -10; new_basis = 5010-5010 = 0; shift = +10
        assert pm.position["direction"] == "LONG"
        assert pm.position["volume"] == 1
        assert pm.position["entry_price"] == 5010.4
        assert pm.position["stop_loss"] == pytest.approx(4960.0)   # 4950 + 10
        assert pm.position["take_profit"] == pytest.approx(5110.0)  # 5100 + 10
        assert "换月从 CFFEX.IM2608 迁移" in pm.position["last_ai_decision"]
        # symbol 双服务同步（ARCHITECTURE.md 阶段 3 决策 3）
        assert mds.symbol == "CFFEX.IM2609"
        assert rm.mcs.symbol == "CFFEX.IM2609"
        assert mds.im_quote.last_price == 5010.0   # im_quote 切到新合约
        assert emergency.mode is False
        # 换月开仓日志
        assert any(e[0][0] == "OPEN" and e[0][1] == "CFFEX.IM2609"
                   for e in rm.logger.events)
        assert any("IM换月完成" in m for m in rm.notifier.sent)

    def test_close_failure_aborts(self, tmp_path):
        mds = FakeMds(days_to_expiry=1)
        api = FakeApi(SimpleNamespace(volume_long=1, volume_short=0))
        pm = self._filled_pm(tmp_path)
        oe = SimpleNamespace(close_position=lambda *a, **k: False,
                             execute_order_safe=lambda **k: pytest.fail("不应开仓"))
        rm = make_rm(mds, api, pm, oe)
        rm.rollover_if_needed()
        assert mds.symbol == "CFFEX.IM2608"   # 终止换月
        assert any("换月平仓失败" in m for m in rm.notifier.sent)

    def test_open_failure_emergency_and_cleanup(self, tmp_path):
        """P3 修复: 开仓失败也要更新 symbol + 清空 current_position + 应急模式。"""
        mds = FakeMds(days_to_expiry=1)
        api = FakeApi(SimpleNamespace(volume_long=1, volume_short=0),
                      old_last=5000.0, new_last=5010.0)
        pm = self._filled_pm(tmp_path)
        emergency = EmergencyState()
        oe = SimpleNamespace(
            close_position=lambda *a, **k: True,
            execute_order_safe=lambda **k: None,   # 开仓失败
        )
        rm = make_rm(mds, api, pm, oe, emergency)
        rm.rollover_if_needed()
        assert emergency.mode is True
        assert mds.symbol == "CFFEX.IM2609"
        assert rm.mcs.symbol == "CFFEX.IM2609"
        assert pm.position["direction"] is None
        assert pm.position["volume"] == 0
        assert "换月开仓失败" in pm.position["last_ai_decision"]
        assert any("紧急：IM换月开仓失败" in m for m in rm.notifier.sent)
