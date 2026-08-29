"""system 单测（阶段 5）— 装配接线 / 节点执行半边 / 重连看门狗 / run 主循环 / DryRunApiProxy。

覆盖:
- 装配: dry_run 代理包装 / 启动即拉指数价+刷新技术面（真源 L405/L418）/
  备忘 item 1/2/3 接线断言（filters.atr5_fn / left_side 四注入 / sps 共享 lunch_context）/
  pipeline 三注入 / stop 清理
- DryRunApiProxy（决策 12）: insert_order 拦截 REJECTED / cancel_order 空转 /
  其余委托真实 api / 下划线属性 AttributeError / OrderExecutor 端到端零真实委托
- 节点执行半边（备忘 item 5）: _execute_close_action（pnl *200 quirk 手算 ±8000）/
  _post_open_node / _overnight_node / _lunch_breakout_node / _lunch_force_close_node /
  _check_stop_profit（close 失败转 emergency）
- _reconnect_api: 成功重绑五服务 + symbol 双服务同步 / 失败指数退避后 False+🚨
- run 主循环: KeyboardInterrupt 优雅退出 / 单 tick 波段先触发（900s 手算）/
  双频第二 tick SCALPING（660s≥300 手算）/ 隔夜节点每日一次 /
  应急自动重置（空仓 1900s>1800s）/ 应急持仓不重置 / 重连失败 raise / 重连成功续跑
"""
import logging
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import quantai.system as sys_mod
from quantai import config
from quantai.ai_decision import save_ai_decision
from quantai.performance import PerformanceMetrics
from quantai.strategies.session_plays import SessionAction
from quantai.system import DryRunApiProxy, IMTradingSystem


# ---------- 测试替身 ----------

class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class FakeApi:
    """假 TqApi: 只读面（quote/position/account/wait_update）+ 下单记录面。"""

    def __init__(self):
        self.closed = False
        self.insert_orders = []
        self.cancel_calls = []
        self.get_position_calls = []
        self.pos = SimpleNamespace(volume_long=0, volume_short=0,
                                   open_price_long=0.0, open_price_short=0.0)

    def get_quote(self, sym):
        return SimpleNamespace(open_interest=10000, last_price=5000.0,
                               ask_price1=5000.2, bid_price1=4999.8,
                               settlement=4990.0, upper_limit=5500.0,
                               lower_limit=4500.0)

    def get_position(self, sym):
        self.get_position_calls.append(sym)
        return self.pos

    def get_account(self):
        return SimpleNamespace(balance=200000.0, position_profit=0.0)

    def wait_update(self, deadline=None):
        return None

    def insert_order(self, symbol, direction, offset, volume, limit_price):
        self.insert_orders.append((symbol, direction, offset, volume, limit_price))
        return SimpleNamespace(is_error=False, status="FINISHED",
                               volume_left=0, trade_price=5000.0, last_msg="")

    def cancel_order(self, order):
        self.cancel_calls.append(order)

    def close(self):
        self.closed = True


class ScriptedApi(FakeApi):
    """wait_update 按脚本逐个弹出行为（装配期不消费，armed 后才生效）。"""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.armed = False

    def wait_update(self, deadline=None):
        self.wait_calls = getattr(self, "wait_calls", 0) + 1
        if not self.armed:
            return None
        act = self.script.pop(0) if self.script else None
        if isinstance(act, BaseException):   # KeyboardInterrupt 不是 Exception 子类
            raise act
        if callable(act):
            act()
        return None


class FakeIndexFetcher:
    def get_kline_data(self, index_name, frequency):
        import pandas as pd
        return pd.DataFrame({"close": [5001.0]})

    def get_asian_indices_5min_bars(self):
        return {}

    def generate_ai_prompt(self, index_name, periods):
        return "FAKE_TECH"


class FakeNewsFetcher:
    def fetch_important_news(self, start_str, end_str):
        return []


class FakeCalendar:
    def __init__(self, is_trading_day=True, is_trading_time=True,
                 is_near_close=False):
        self._td = is_trading_day
        self._tt = is_trading_time
        self._nc = is_near_close

    def is_trading_day(self, now=None):
        return self._td

    def is_trading_time(self, now=None):
        return self._tt

    def is_near_close(self):
        return self._nc


class FakeSps:
    """时段策略替身: 记录调用 + 可编程返回值。"""

    def __init__(self):
        self.calls = []
        self.pre_open_result = []
        self.post_open_result = None
        self.overnight_result = None
        self.breakout_order = None
        self.force_close = False

    def morning_pre_open_analysis(self, position):
        self.calls.append(("pre_open", dict(position)))
        return self.pre_open_result

    def post_open_analysis(self, position):
        self.calls.append(("post_open", dict(position)))
        return self.post_open_result

    def evaluate_overnight_holding(self, position):
        self.calls.append(("overnight", dict(position)))
        return self.overnight_result

    def lunch_breakout_check(self, position):
        self.calls.append(("breakout", dict(position)))
        return self.breakout_order

    def lunch_force_close_check(self, position):
        self.calls.append(("force_close", dict(position)))
        return self.force_close

    def check_tail_session(self, now=None):
        return False, ""

    def lunch_breakout_preview(self):
        self.calls.append(("preview", None))


class FakeOE:
    def __init__(self):
        self.safe_calls = []
        self.close_calls = []
        self.emergency_calls = []
        self.safe_result = 4960.0
        self.close_result = True
        self._closing = False

    @property
    def is_closing(self):
        return self._closing

    def execute_order_safe(self, **kwargs):
        self.safe_calls.append(kwargs)
        return self.safe_result

    def close_position(self, reason, is_emergency=False):
        self.close_calls.append((reason, is_emergency))
        return self.close_result

    def emergency_close(self, reason):
        self.emergency_calls.append(reason)

    def cancel_all_orders(self):
        pass


class FakePipeline:
    def __init__(self):
        self.cycle_calls = []
        self.result = 900

    def execute_ai_cycle(self, mode):
        self.cycle_calls.append(mode)
        return self.result


class FakeConditional:
    def __init__(self):
        self.checks = 0

    def check_conditional_order(self):
        self.checks += 1


class FakeMetrics:
    def __init__(self):
        self.equity = []
        self.reports = 0

    def update_equity(self, balance, now):
        self.equity.append((balance, now))

    def print_daily_report(self):
        self.reports += 1


class FakeRollover:
    def __init__(self):
        self.calls = 0

    def rollover_if_needed(self):
        self.calls += 1


# ---------- 夹具 ----------

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """config 路径与日志重定向到 tmp，避免测试写真实 data/ 目录。"""
    monkeypatch.setattr(config, "TRADES_HISTORY_FILE",
                        str(tmp_path / "trades_history.jsonl"))
    monkeypatch.setattr(config, "PERF_STATE_FILE",
                        str(tmp_path / "perf_state.json"))
    monkeypatch.setattr(config, "METRICS_FILE", str(tmp_path / "metrics.csv"))
    monkeypatch.setattr(config, "TRADE_LOG_FILE", str(tmp_path / "trade_log.csv"))
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "trading.log"))
    monkeypatch.setattr(config, "AI_DECISIONS_FILE",
                        str(tmp_path / "ai_decisions.jsonl"))
    monkeypatch.setattr(sys_mod, "setup_logging", lambda *a, **k: None)


def _make_system(tmp_path, dry_run=True, api=None, **overrides):
    kwargs = dict(
        dry_run=dry_run,
        api=api if api is not None else FakeApi(),
        index_fetcher=FakeIndexFetcher(),
        news_fetcher=FakeNewsFetcher(),
        ai_chat_fn=lambda messages: '{"action": "WAIT", "confidence": 0.3}',
        notifier=FakeNotifier(),
        metrics=PerformanceMetrics(),
        position_file=str(tmp_path / "position.pkl"),
        cb_state_file=str(tmp_path / "cb.json"))
    kwargs.update(overrides)
    return IMTradingSystem(**kwargs)


def _install_run_fakes(s):
    """run 主循环测试用: 替换可记录替身（保持 pm/mds 真实实例）。"""
    s.calendar = FakeCalendar()
    s.sps = FakeSps()
    s.conditional = FakeConditional()
    s.pipeline = FakePipeline()
    s.metrics = FakeMetrics()
    s.rollover = FakeRollover()


# ---------- 装配 ----------

def test_assembly_dry_run_wraps_proxy(tmp_path, isolated_config):
    api = FakeApi()
    s = _make_system(tmp_path, dry_run=True, api=api)
    assert isinstance(s.api, DryRunApiProxy)
    assert s.api._real_api is api
    s.stop()


def test_assembly_live_api_unwrapped(tmp_path, isolated_config):
    api = FakeApi()
    s = _make_system(tmp_path, dry_run=False, api=api)
    assert s.api is api          # 非 dry_run 不包装
    s.stop()


def test_assembly_startup_refresh(tmp_path, isolated_config):
    # 真源 L405/L418: 装配即 update_index_price + refresh_tech_data
    s = _make_system(tmp_path)
    assert s.mds.index_price == 5001.0           # FakeIndexFetcher 5min close
    assert s.mds.tech_data_text == "FAKE_TECH"   # generate_ai_prompt 假数据
    s.stop()


def test_assembly_wiring(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    # 备忘 item 1: filters.atr5_fn → mcs.atr_5（未接线视为 0 = 静默放行）
    assert s.filters.atr5_fn() == s.mcs.atr_5
    # 备忘 item 2: left_side 四注入 + warner 接线
    assert s.left_side.index_price_fn() == s.mds.index_price
    assert s.left_side.yesterday_close_fn == s.mds.get_yesterday_index_close
    assert s.left_side.dynamic_levels_fn == s.mcs.compute_dynamic_levels
    assert s.left_side.warn_fn == s.warner.warn
    # 备忘 item 3: sps 共享 lunch_context + news_items_fn + ai_chat_fn
    assert s.sps.lunch_context is s.lunch_context
    assert s.sps.news_items_fn == s.news_manager.get_news
    assert s.sps.ai_chat_fn is s.ai_chat_fn
    # pipeline 三注入（prompt_fn / ai_chat_fn / save_decision_fn）
    assert s.pipeline.prompt_fn == s.prompt_builder.build_prompt
    assert s.pipeline.ai_chat_fn is s.ai_chat_fn
    assert s.pipeline.save_decision_fn is save_ai_decision
    # sizer 经注入读取 account/equity/daily_loss
    assert s.sizer.account_fn().balance == 200000.0
    assert s.sizer.daily_loss_fn == s.cb.daily_loss
    # 条件单/换月/prompt_builder 的 tail_fn / left_side_fn 接线
    assert s.conditional.tail_fn == s.sps.check_tail_session
    assert s.prompt_builder.tail_fn == s.sps.check_tail_session
    assert s.prompt_builder.left_side_fn == s.left_side.compute_left_side_signals
    s.stop()


def test_stop_cleanup(tmp_path, isolated_config):
    api = FakeApi()
    s = _make_system(tmp_path, dry_run=False, api=api)
    s.stop()
    assert api.closed is True
    assert s.news_manager.news_thread_running is False
    assert (tmp_path / "position.pkl").exists()   # pm.save_position_state


# ---------- DryRunApiProxy（决策 12） ----------

def test_dry_run_proxy_intercepts_insert_order():
    real = FakeApi()
    proxy = DryRunApiProxy(real)
    order = proxy.insert_order("CFFEX.IM2608", "BUY", "OPEN", 2, 5000.2)
    assert order.is_error is True
    assert order.status == "REJECTED"
    assert "DRY_RUN" in order.last_msg
    assert order.volume_left == 2
    assert proxy.intercepted_orders == 1
    assert real.insert_orders == []               # 未触达真实 api


def test_dry_run_proxy_cancel_order_noop():
    real = FakeApi()
    proxy = DryRunApiProxy(real)
    proxy.cancel_order(object())                  # 不抛异常
    assert real.cancel_calls == []


def test_dry_run_proxy_delegates_readonly_calls():
    real = FakeApi()
    proxy = DryRunApiProxy(real)
    q = proxy.get_quote("CFFEX.IM2608")
    assert q.last_price == 5000.0
    assert proxy.wait_update(deadline=1) is None
    assert proxy.get_account().balance == 200000.0


def test_dry_run_proxy_underscore_attr_raises():
    proxy = DryRunApiProxy(FakeApi())
    with pytest.raises(AttributeError):
        proxy._nonexistent_attr


def test_dry_run_end_to_end_order_blocked(tmp_path, isolated_config):
    # 决策 12 端到端: OrderExecutor 经代理下单 → 立即 REJECTED → 返回 None，
    # 真实 api 零委托（execute_order_safe 对 REJECTED 第一轮即快速失败）
    real = FakeApi()
    s = _make_system(tmp_path, dry_run=True, api=real)
    price = s.oe.execute_order_safe("CFFEX.IM2608", "BUY", "OPEN", 1, 5000.2,
                                    timeout=5)
    assert price is None
    assert s.api.intercepted_orders == 1
    assert real.insert_orders == []
    s.stop()


# ---------- 节点执行半边（备忘 item 5） ----------

def test_execute_close_action_success_short(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    fake_oe.safe_result = 4960.0
    s.oe = fake_oe
    s.pm.position.update({"direction": "SHORT", "volume": 2,
                          "entry_price": 5000.0})
    act = SessionAction(action="CLOSE_POSITION", close_direction="SELL",
                        volume=2)
    s._execute_close_action(act)
    assert fake_oe.safe_calls == [dict(symbol=s.mds.symbol, direction="SELL",
                                       offset="CLOSE", volume=2,
                                       limit_price=None, timeout=15)]
    # 手算（真源 L3952 quirk: *200 未乘 volume）: (5000-4960)*200 = +8000
    # （notifier 文案为"完成"，"成功"仅出现在日志 L307）
    assert "✅ 盘前主动平仓完成 @ 4960.00, 盈亏 +8000元" in s.notifier.sent
    assert s.pm.position["direction"] is None
    assert s.pm.position["volume"] == 0
    assert s.pm.position["last_ai_decision"] == "盘前跳空主动平仓 @ 4960.00"
    s.stop()


def test_execute_close_action_success_long_negative_pnl(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    fake_oe.safe_result = 4960.0
    s.oe = fake_oe
    s.pm.position.update({"direction": "LONG", "volume": 1,
                          "entry_price": 5000.0})
    act = SessionAction(action="CLOSE_POSITION", close_direction="BUY",
                        volume=1)
    s._execute_close_action(act)
    # 手算: (4960-5000)*200 = -8000（LONG 方向公式）
    assert "盈亏 -8000元" in s.notifier.sent[0]
    assert s.pm.position["direction"] is None
    s.stop()


def test_execute_close_action_failure_notifies_and_keeps_position(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    fake_oe.safe_result = None
    s.oe = fake_oe
    s.pm.position.update({"direction": "SHORT", "volume": 2,
                          "entry_price": 5000.0})
    act = SessionAction(action="CLOSE_POSITION", close_direction="SELL",
                        volume=2)
    s._execute_close_action(act)
    assert "❌ 盘前主动平仓失败！请手动处理 SHORT @ 5000.00" in s.notifier.sent
    assert s.pm.position["direction"] == "SHORT"   # 失败不清仓
    s.stop()


def test_execute_close_action_exception_notifies(tmp_path, isolated_config):
    s = _make_system(tmp_path)

    class BoomOE(FakeOE):
        def execute_order_safe(self, **kwargs):
            raise RuntimeError("boom")

    s.oe = BoomOE()
    s.pm.position.update({"direction": "SHORT", "volume": 2,
                          "entry_price": 5000.0})
    s._execute_close_action(SessionAction(action="CLOSE_POSITION",
                                          close_direction="SELL", volume=2))
    assert any("❌ 盘前主动平仓异常: boom" in m for m in s.notifier.sent)
    s.stop()


def test_execute_close_action_unknown_action_ignored(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    s.oe = fake_oe
    s._execute_close_action(SessionAction(action="FORCE_CLOSE"))
    assert fake_oe.safe_calls == []
    s.stop()


def test_post_open_node_adjust_both(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s.sps.post_open_result = {"adjust_stop_loss": 4980.0,
                              "adjust_take_profit": 5100.0}
    s.pm.position.update({"direction": "LONG", "volume": 1,
                          "entry_price": 5000.0, "stop_loss": 4950.0,
                          "take_profit": 5050.0})
    s._post_open_node()
    assert s.pm.position["stop_loss"] == 4980.0
    assert s.pm.position["take_profit"] == 5100.0
    s.stop()


def test_post_open_node_none_untouched(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s.sps.post_open_result = None
    s.pm.position.update({"direction": "LONG", "volume": 1,
                          "entry_price": 5000.0, "stop_loss": 4950.0,
                          "take_profit": 5050.0})
    s._post_open_node()
    assert s.pm.position["stop_loss"] == 4950.0   # 未被改写
    assert s.pm.position["take_profit"] == 5050.0
    s.stop()


def test_overnight_node_close_suggestion(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s.oe = FakeOE()
    reason = "收盘前平仓（AI建议不过夜，理由：尾盘走弱）"
    s.sps.overnight_result = {"action": "CLOSE", "reason": reason}
    s._overnight_node()
    assert s.oe.close_calls == [(reason, False)]
    s.stop()


def test_overnight_node_hold_no_close(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s.oe = FakeOE()
    s.sps.overnight_result = {"action": "HOLD", "reason": "趋势延续"}
    s._overnight_node()
    assert s.oe.close_calls == []
    s.sps.overnight_result = None
    s._overnight_node()
    assert s.oe.close_calls == []
    s.stop()


def test_lunch_breakout_node_sets_conditional_order(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    order = {"action": "BUY", "trigger_type": "PRICE_ABOVE",
             "trigger_price": 5010.0, "stop_loss": 4990.0,
             "take_profit": 5060.0, "volume": 1,
             "source": "lunch_breakout", "created_date": "2026-08-28"}
    s.sps.breakout_order = order
    s._lunch_breakout_node()
    assert s.pm.conditional_order is order
    s.stop()


def test_lunch_breakout_node_none_unchanged(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s._lunch_breakout_node()
    assert s.pm.conditional_order is None
    s.stop()


def test_lunch_force_close_node_triggers_close(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s.oe = FakeOE()
    s.sps.force_close = True
    s._lunch_force_close_node()
    assert s.oe.close_calls == [("12:50顺势单14:00强平", False)]
    s.stop()


def test_lunch_force_close_node_false_no_close(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    s.sps = FakeSps()
    s.oe = FakeOE()
    s._lunch_force_close_node()
    assert s.oe.close_calls == []
    s.stop()


def test_check_stop_profit_trigger_closes(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    s.oe = fake_oe
    recorded = []

    def _fake_check(position, last_price, closing=False, on_stopout=None):
        recorded.append((closing, on_stopout))
        return "止损触发"

    s.pm.check_stop_profit = _fake_check
    s._check_stop_profit()
    assert fake_oe.close_calls == [("止损触发", False)]
    assert fake_oe.emergency_calls == []          # 平仓成功不转应急
    assert recorded[0][0] is False                # closing=oe.is_closing
    assert recorded[0][1] == s.stopout.record     # on_stopout 接线
    s.stop()


def test_check_stop_profit_close_fail_falls_to_emergency(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    fake_oe.close_result = False
    s.oe = fake_oe
    s.pm.check_stop_profit = lambda *a, **k: "止盈触发"
    s._check_stop_profit()
    assert fake_oe.emergency_calls == ["止盈触发"]   # 阶段 4 决策 5 消费方
    s.stop()


def test_check_stop_profit_no_trigger_noop(tmp_path, isolated_config):
    s = _make_system(tmp_path)
    fake_oe = FakeOE()
    s.oe = fake_oe
    s.pm.check_stop_profit = lambda *a, **k: None
    s._check_stop_profit()
    assert fake_oe.close_calls == []
    assert fake_oe.emergency_calls == []
    s.stop()


# ---------- 重连看门狗（真源 _reconnect_api L5436–5473） ----------

def test_reconnect_success_rebinds_all_services(tmp_path, isolated_config, monkeypatch):
    s = _make_system(tmp_path, dry_run=False)
    new_api = FakeApi()
    monkeypatch.setattr(sys_mod, "TqApi", lambda *a, **k: new_api)
    monkeypatch.setattr(sys_mod, "TqKq", lambda: None)
    monkeypatch.setattr(sys_mod, "TqAuth", lambda a, p: None)
    ok = s._reconnect_api(max_attempts=2)
    assert ok is True
    # _bind_api 重绑五服务
    assert s.api is new_api
    assert s.mds.api is new_api
    assert s.mcs.api is new_api
    assert s.oe.api is new_api
    assert s.acct.api is new_api
    assert s.rollover.api is new_api
    # 重新识别主力 + 重新订阅行情（假 api 持仓量相同 → 当月合约 IM2608）
    assert s.mds.symbol == "CFFEX.IM2608"
    assert s.mcs.symbol == "CFFEX.IM2608"          # symbol 双服务同步
    assert s.mds.im_quote.last_price == 5000.0
    # 重连后重新校验持仓一致性
    assert new_api.get_position_calls
    assert any("✅ 天勤重连成功" in m for m in s.notifier.sent)
    s.stop()


def test_reconnect_failure_exhausts_attempts(tmp_path, isolated_config, monkeypatch):
    s = _make_system(tmp_path, dry_run=False)
    attempts = {"n": 0}

    def _boom(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("网络断开")

    monkeypatch.setattr(sys_mod, "TqApi", _boom)
    monkeypatch.setattr(sys_mod, "TqKq", lambda: None)
    monkeypatch.setattr(sys_mod, "TqAuth", lambda a, p: None)
    sleeps = []
    monkeypatch.setattr(sys_mod, "time",
                        SimpleNamespace(sleep=lambda sec: sleeps.append(sec),
                                        time=time.time))
    ok = s._reconnect_api(max_attempts=3)
    assert ok is False
    assert attempts["n"] == 3
    assert sleeps == [10, 20]                      # 指数退避（最后一次不 sleep）
    assert any("🚨 天勤重连失败" in m for m in s.notifier.sent)
    s.stop()


# ---------- run 主循环（真源 run L5475–5646） ----------

def test_caplog_probe(caplog):
    """临时探针：验证 caplog 在本文件上下文是否可用（诊断后删除）。"""
    with caplog.at_level(logging.WARNING):
        logging.warning("hello-probe")
    assert any("hello-probe" in r.getMessage() for r in caplog.records)


def test_run_keyboard_interrupt_stops_cleanly(tmp_path, isolated_config, monkeypatch):
    api = ScriptedApi([KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    s.run()
    assert api.closed is True                      # finally → stop()
    assert s.news_manager.news_thread_running is False


def test_run_single_tick_swing_fires_first(tmp_path, isolated_config, monkeypatch):
    # 手算: last_swing 初始化为 now-15min → swing_elapsed=900 ≥ 900 → 波段先触发
    frozen = datetime(2026, 8, 28, 10, 30, 0)

    class _T(datetime):
        @classmethod
        def now(cls):
            return frozen

    monkeypatch.setattr(sys_mod, "datetime", _T)
    api = ScriptedApi([None, KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    s.run()
    assert s.conditional.checks == 1               # 每 tick 条件单检查
    assert s.pipeline.cycle_calls == ["SWING"]     # 波段先触发后 continue
    assert s.rollover.calls == 0                   # continue 跳过换月
    assert s.metrics.equity == [(200000.0, frozen)]  # balance+position_profit 手算


def test_run_dual_freq_scalping_second_tick(tmp_path, isolated_config, monkeypatch):
    # 手算: tick1 SWING(900≥900) → 时钟+6min → tick2 swing_elapsed=360<900,
    # scalping_elapsed=(T+6)-(T-5)=660≥300 且 SCALPING(atr5/atr15=30/20=1.5>1.3)
    # → SCALPING 触发 + 更新波段计时器 → rollover 执行
    class _Adv(datetime):
        _cur = datetime(2026, 8, 28, 10, 30, 0)

        @classmethod
        def now(cls):
            return cls._cur

    def _advance():
        _Adv._cur = _Adv._cur + timedelta(minutes=6)

    monkeypatch.setattr(sys_mod, "datetime", _Adv)
    api = ScriptedApi([None, _advance, None, KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    s.mcs = SimpleNamespace(stress_level=1.0, atr_15=20.0, atr_5=30.0)
    s.run()
    assert s.pipeline.cycle_calls == ["SWING", "SCALPING"]
    assert s.rollover.calls == 2                   # tick2/tick3 各一次
    assert s.conditional.checks == 3
    assert len(s.metrics.equity) == 2              # tick1 + tick2（间隔>30s）


def test_run_overnight_node_once_per_day(tmp_path, isolated_config, monkeypatch):
    frozen = datetime(2026, 8, 28, 14, 56, 0)

    class _T(datetime):
        @classmethod
        def now(cls):
            return frozen

    monkeypatch.setattr(sys_mod, "datetime", _T)
    api = ScriptedApi([None, KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    s.run()
    kinds = [c[0] for c in s.sps.calls]
    assert kinds.count("overnight") == 1           # 每日仅一次
    assert s.metrics.reports == 1                  # 日报随隔夜节点打印
    assert s._overnight_done_date == date(2026, 8, 28)


def test_run_emergency_auto_reset_when_flat(tmp_path, isolated_config, monkeypatch):
    # 手算: enter_time 距今 1900s > EMERGENCY_AUTO_RESET_SEC(1800) 且已空仓 → 自动重置
    frozen = datetime(2026, 8, 28, 10, 30, 0)

    class _T(datetime):
        @classmethod
        def now(cls):
            return frozen

    monkeypatch.setattr(sys_mod, "datetime", _T)
    api = ScriptedApi([None, KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    s.emergency.mode = True
    s.emergency.enter_time = frozen - timedelta(seconds=1900)
    sleeps = []
    monkeypatch.setattr(sys_mod, "time",
                        SimpleNamespace(sleep=lambda sec: sleeps.append(sec),
                                        time=time.time))
    s.run()
    assert s.emergency.mode is False
    assert s.emergency.enter_time is None
    assert any("emergency_mode 自动重置" in m for m in s.notifier.sent)
    assert 1 in sleeps                             # 应急分支 sleep(1)
    assert s.conditional.checks == 0               # 应急 continue → 不做常规检查
    assert s.pipeline.cycle_calls == []


def test_run_emergency_holding_keeps_waiting(tmp_path, isolated_config,
                                             monkeypatch, caplog):
    frozen = datetime(2026, 8, 28, 10, 30, 0)

    class _T(datetime):
        @classmethod
        def now(cls):
            return frozen

    monkeypatch.setattr(sys_mod, "datetime", _T)
    api = ScriptedApi([None, KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    api.pos.volume_long = 2                        # 云端仍持仓
    s.emergency.mode = True
    s.emergency.enter_time = frozen - timedelta(seconds=1900)
    sleeps = []
    monkeypatch.setattr(sys_mod, "time",
                        SimpleNamespace(sleep=lambda sec: sleeps.append(sec),
                                        time=time.time))
    with caplog.at_level(logging.WARNING):
        logging.warning("DIRECT-PROBE-BEFORE-RUN")
        s.run()
        logging.warning("DIRECT-PROBE-AFTER-RUN")
    # --- 临时探针（诊断后删除） ---
    import sys as _sys
    print("PROBE module-identity:", id(logging) == id(_sys.modules["logging"]),
          file=_sys.stderr)
    print("PROBE caplog.records=", [(r.levelno, r.getMessage()[:40]) for r in caplog.records],
          file=_sys.stderr)
    print("PROBE handler.records=", [(r.levelno, r.getMessage()[:40]) for r in caplog.handler.records],
          file=_sys.stderr)
    print("PROBE handler.level=", caplog.handler.level,
          "root.level=", logging.getLogger().level,
          "root.handlers=", logging.getLogger().handlers,
          "disable=", logging.root.manager.disable, file=_sys.stderr)
    # --- 探针结束 ---
    assert s.emergency.mode is True                # 持仓中不重置
    # 仍持仓分支仅记日志不发通知（真源 L527–530 语义）
    assert any("仍持仓中" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)
    assert 1 in sleeps


def test_run_reconnect_failure_raises_and_stops(tmp_path, isolated_config, monkeypatch):
    api = ScriptedApi([ConnectionError("断线")])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    reconnect_calls = []

    def _fake_reconnect(max_attempts=5):
        reconnect_calls.append(1)
        return False

    monkeypatch.setattr(s, "_reconnect_api", _fake_reconnect)
    with pytest.raises(ConnectionError):
        s.run()
    assert reconnect_calls == [1]
    assert api.closed is True                      # finally → stop()


def test_run_reconnect_success_continues_loop(tmp_path, isolated_config, monkeypatch):
    api = ScriptedApi([ConnectionError("断线"), None, KeyboardInterrupt()])
    s = _make_system(tmp_path, dry_run=False, api=api)
    api.armed = True
    _install_run_fakes(s)
    reconnect_calls = []

    def _fake_reconnect(max_attempts=5):
        reconnect_calls.append(1)
        return True

    monkeypatch.setattr(s, "_reconnect_api", _fake_reconnect)
    s.run()                                        # 不抛（C1 修复: 断线不退出）
    assert reconnect_calls == [1]
    assert s.pipeline.cycle_calls == ["SWING"]     # 重连后继续双频决策
    assert api.closed is True
