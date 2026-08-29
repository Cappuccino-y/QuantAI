"""position_manager 单测（阶段 4）— 行为对拍真源 L555–672 / L2926–2957。

重点覆盖（design.md §5.2 阶段 4 备忘）:
- pkl plain-dict 守护: dataclass 对象写入 pkl → 拒绝加载（防旧版读新 pkl 出错）
- 旧版两种 pkl 格式兼容: {position, conditional_order} 包裹 / 裸 position dict
- 当日开仓次数恢复 / 过期条件单启动清理 / 云端 reconcile / SL/TP 监控
"""
import pickle
from dataclasses import dataclass
from datetime import datetime

import pytest

from quantai.position_manager import PositionManager, _is_plain_value
from quantai.risk_manager import DailyTradeLimiter


@dataclass
class BadPosition:
    """模块级 dataclass（pickle 要求可定位类）。"""
    direction: str = "LONG"


@dataclass
class Weird:
    x: int = 1


@dataclass
class BadCond:
    action: str = "BUY"


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class FakeCloudPos:
    def __init__(self, volume_long=0, volume_short=0,
                 open_price_long=0.0, open_price_short=0.0):
        self.volume_long = volume_long
        self.volume_short = volume_short
        self.open_price_long = open_price_long
        self.open_price_short = open_price_short


class FakeApi:
    def __init__(self, cloud_pos=None):
        self.cloud_pos = cloud_pos or FakeCloudPos()
        self.wait_calls = 0

    def get_position(self, symbol):
        return self.cloud_pos

    def wait_update(self, deadline=None):
        self.wait_calls += 1


def make_pm(tmp_path, notifier=None, limiter=None,
            now=datetime(2026, 8, 28, 10, 0, 0)):
    return PositionManager(
        position_file=str(tmp_path / "position_state.pkl"),
        notifier=notifier,
        daily_limiter=limiter,
        now_fn=lambda: now,
    )


# ---------- 初始状态（真源 L147–157 逐键） ----------

class TestInitialState:
    def test_position_dict_keys_match_true_source(self, tmp_path):
        pm = make_pm(tmp_path)
        assert pm.position == {
            "direction": None,
            "volume": 0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "last_ai_decision": ""
        }

    def test_conditional_order_initial_none(self, tmp_path):
        pm = make_pm(tmp_path)
        assert pm.conditional_order is None
        assert pm.last_entry_time == datetime.min


# ---------- save / load 往返 + plain-dict 守护 ----------

class TestSaveLoad:
    def test_roundtrip(self, tmp_path):
        pm = make_pm(tmp_path)
        pm.position.update({"direction": "LONG", "volume": 2, "entry_price": 5000.0,
                            "stop_loss": 4950.0, "take_profit": 5100.0,
                            "last_ai_decision": "test", "entry_time": datetime(2026, 8, 28, 9, 31)})
        pm.conditional_order = {"action": "BUY", "trigger_price": 5010.0,
                                "created_date": "2026-08-28"}
        pm.save_position_state()

        pm2 = make_pm(tmp_path)
        pm2.load_position_state()
        assert pm2.position["direction"] == "LONG"
        assert pm2.position["volume"] == 2
        assert pm2.position["entry_time"] == datetime(2026, 8, 28, 9, 31)
        assert pm2.conditional_order["trigger_price"] == 5010.0

    def test_plain_dict_guard_rejects_dataclass(self, tmp_path):
        """design.md §5.2 阶段 4 备忘: dataclass 对象写进 pkl → 拒绝加载。"""
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"position": BadPosition(), "conditional_order": None,
                         "today_entries": 1, "today_entries_date": "2026-08-28"}, fp)
        pm = make_pm(tmp_path)
        pm.load_position_state()
        # 拒绝加载 → 保持默认空仓
        assert pm.position["direction"] is None
        assert pm.position["volume"] == 0

    def test_plain_dict_guard_rejects_non_dict_state(self, tmp_path):
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump(Weird(), fp)
        pm = make_pm(tmp_path)
        pm.load_position_state()
        assert pm.position["direction"] is None

    def test_plain_dict_guard_rejects_bad_conditional(self, tmp_path):
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"position": {"direction": None}, "conditional_order": BadCond()}, fp)
        pm = make_pm(tmp_path)
        pm.load_position_state()
        assert pm.conditional_order is None

    def test_legacy_bare_position_dict_compat(self, tmp_path):
        """旧版写出的裸 position dict（无 'position' 包裹键）→ 可读。"""
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"direction": "SHORT", "volume": 1, "entry_price": 5000.0,
                         "stop_loss": 5050.0, "take_profit": 4900.0,
                         "last_ai_decision": "old"}, fp)
        pm = make_pm(tmp_path)
        pm.load_position_state()
        assert pm.position["direction"] == "SHORT"
        assert pm.conditional_order is None   # 裸格式 → 条件单置 None（真源 L630-631）

    def test_legacy_wrapped_format_compat(self, tmp_path):
        """旧版 {position, conditional_order} 包裹格式（无 today_entries 键）→ 可读。"""
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"position": {"direction": "LONG", "volume": 1},
                         "conditional_order": {"action": "SELL", "trigger_price": 4990.0}}, fp)
        pm = make_pm(tmp_path)
        pm.load_position_state()
        assert pm.position["direction"] == "LONG"
        assert pm.conditional_order["action"] == "SELL"

    def test_stale_conditional_cleared_on_load(self, tmp_path):
        """8/27 修复: 启动清理过期条件单（created_date 非今日）。"""
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"position": {"direction": None},
                         "conditional_order": {"action": "BUY", "trigger_price": 5000.0,
                                               "trigger_type": "PRICE_ABOVE",
                                               "created_date": "2026-08-27"}},
                        fp)
        notifier = FakeNotifier()
        pm = make_pm(tmp_path, notifier=notifier)
        pm.load_position_state()
        assert pm.conditional_order is None
        assert any("启动时清理过期条件单" in m for m in notifier.sent)

    def test_today_conditional_kept_on_load(self, tmp_path):
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"position": {"direction": None},
                         "conditional_order": {"action": "BUY", "trigger_price": 5000.0,
                                               "created_date": "2026-08-28"}},
                        fp)
        pm = make_pm(tmp_path)
        pm.load_position_state()
        assert pm.conditional_order is not None

    def test_today_entries_restored_via_limiter(self, tmp_path):
        f = str(tmp_path / "position_state.pkl")
        with open(f, "wb") as fp:
            pickle.dump({"position": {"direction": None}, "conditional_order": None,
                         "today_entries": 4, "today_entries_date": "2026-08-28"}, fp)
        limiter = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        pm = make_pm(tmp_path, limiter=limiter)
        pm.load_position_state()
        assert limiter.check() == (False, "今日开仓 4/6 次")

    def test_save_persists_today_entries(self, tmp_path):
        limiter = DailyTradeLimiter(now_fn=lambda: datetime(2026, 8, 28, 10, 0, 0))
        limiter.bump()
        limiter.bump()
        pm = make_pm(tmp_path, limiter=limiter)
        pm.save_position_state()
        with open(pm.position_file, "rb") as fp:
            state = pickle.load(fp)
        assert state["today_entries"] == 2
        assert state["today_entries_date"] == "2026-08-28"

    def test_load_missing_file_noop(self, tmp_path):
        pm = make_pm(tmp_path)
        pm.load_position_state()   # 不抛异常
        assert pm.position["direction"] is None


# ---------- 云端 reconcile（真源 _validate_position_state L555–617） ----------

class TestValidatePositionState:
    def test_cloud_matches_no_change(self, tmp_path, caplog):
        pm = make_pm(tmp_path)
        pm.position.update({"direction": "LONG", "volume": 2, "entry_price": 5000.0})
        api = FakeApi(FakeCloudPos(volume_long=2, open_price_long=5000.0))
        pm.validate_position_state(api, "CFFEX.IM2608")
        assert pm.position["direction"] == "LONG"
        assert pm.position["volume"] == 2

    def test_cloud_differs_corrected(self, tmp_path):
        notifier = FakeNotifier()
        pm = make_pm(tmp_path, notifier=notifier)
        pm.position.update({"direction": "LONG", "volume": 2, "entry_price": 5000.0,
                            "stop_loss": 4950.0})
        api = FakeApi(FakeCloudPos(volume_short=1, open_price_short=5010.0))
        pm.validate_position_state(api, "CFFEX.IM2608")
        assert pm.position["direction"] == "SHORT"
        assert pm.position["volume"] == 1
        assert pm.position["entry_price"] == 5010.0
        assert pm.position["stop_loss"] == 4950.0   # 止损止盈云端没有，保留原值
        assert any("持仓状态已用云端数据纠正" in m for m in notifier.sent)

    def test_cloud_empty_clears_direction_keeps_sl_reset(self, tmp_path):
        pm = make_pm(tmp_path)
        pm.position.update({"direction": "LONG", "volume": 2, "entry_price": 5000.0,
                            "stop_loss": 4950.0, "take_profit": 5100.0})
        api = FakeApi(FakeCloudPos())   # 云端空仓
        pm.validate_position_state(api, "CFFEX.IM2608")
        assert pm.position["direction"] is None
        assert pm.position["volume"] == 0
        assert pm.position["stop_loss"] == 0.0    # 云端空仓 → 清零（真源 L605-607）
        assert pm.position["take_profit"] == 0.0

    def test_entry_price_drift_sync_only(self, tmp_path):
        """手数一致但均价漂移（如换月后）→ 仅更新均价，不算严重不一致。"""
        notifier = FakeNotifier()
        pm = make_pm(tmp_path, notifier=notifier)
        pm.position.update({"direction": "LONG", "volume": 2, "entry_price": 5000.0})
        api = FakeApi(FakeCloudPos(volume_long=2, open_price_long=5002.0))
        pm.validate_position_state(api, "CFFEX.IM2608")
        assert pm.position["entry_price"] == 5002.0
        assert notifier.sent == []   # 无纠正告警

    def test_api_exception_keeps_local(self, tmp_path):
        class BoomApi:
            def get_position(self, symbol):
                raise RuntimeError("cloud down")

            def wait_update(self, deadline=None):
                pass

        pm = make_pm(tmp_path)
        pm.position.update({"direction": "LONG", "volume": 2})
        pm.validate_position_state(BoomApi(), "CFFEX.IM2608")
        assert pm.position["direction"] == "LONG"   # 继续使用本地状态


# ---------- SL/TP 监控（真源 check_stop_profit L2926–2957） ----------

class TestCheckStopProfit:
    def test_long_stop_hit(self, tmp_path):
        pm = make_pm(tmp_path)
        pos = {"direction": "LONG", "stop_loss": 4950.0, "take_profit": 5100.0}
        assert pm.check_stop_profit(pos, 4949.9) == "止损触发"

    def test_long_tp_hit(self, tmp_path):
        pm = make_pm(tmp_path)
        pos = {"direction": "LONG", "stop_loss": 4950.0, "take_profit": 5100.0}
        assert pm.check_stop_profit(pos, 5100.0) == "止盈触发"   # >= 双端闭

    def test_short_stop_hit(self, tmp_path):
        pm = make_pm(tmp_path)
        pos = {"direction": "SHORT", "stop_loss": 5050.0, "take_profit": 4900.0}
        assert pm.check_stop_profit(pos, 5050.0) == "止损触发"

    def test_short_tp_hit(self, tmp_path):
        pm = make_pm(tmp_path)
        pos = {"direction": "SHORT", "stop_loss": 5050.0, "take_profit": 4900.0}
        assert pm.check_stop_profit(pos, 4899.9) == "止盈触发"

    def test_no_trigger_in_range(self, tmp_path):
        pm = make_pm(tmp_path)
        pos = {"direction": "LONG", "stop_loss": 4950.0, "take_profit": 5100.0}
        assert pm.check_stop_profit(pos, 5000.0) is None

    def test_guards(self, tmp_path):
        pm = make_pm(tmp_path)
        pos = {"direction": "LONG", "stop_loss": 4950.0, "take_profit": 5100.0}
        assert pm.check_stop_profit(pos, 4949.9, closing=True) is None   # 平仓进行中
        assert pm.check_stop_profit({"direction": None}, 4949.9) is None  # 空仓
        assert pm.check_stop_profit(pos, 0.0) is None                     # 行情异常

    def test_stopout_callback_only_for_stop(self, tmp_path):
        pm = make_pm(tmp_path)
        calls = []
        pos = {"direction": "LONG", "stop_loss": 4950.0, "take_profit": 5100.0}
        pm.check_stop_profit(pos, 5100.0, on_stopout=calls.append)   # 止盈 → 不记录
        assert calls == []
        pm.check_stop_profit(pos, 4949.9, on_stopout=calls.append)   # 止损 → 记录
        assert calls == ["LONG"]


# ---------- _is_plain_value 白名单 ----------

class TestIsPlainValue:
    def test_allowed_types(self):
        assert _is_plain_value({"a": 1, "b": "x", "c": 1.5, "d": None, "e": True})
        assert _is_plain_value({"entry_time": datetime(2026, 8, 28)})
        assert _is_plain_value({"nested": {"k": [1, 2, 3]}})

    def test_rejected_types(self):
        class Custom:
            pass

        assert not _is_plain_value(Custom())
        assert not _is_plain_value({"pos": Custom()})
        assert not _is_plain_value([Custom()])
