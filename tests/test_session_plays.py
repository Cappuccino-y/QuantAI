"""session_plays 单测（阶段 3 第二批）— 行为对拍真源 L3520–3683 + L3809–4421。

覆盖:
- check_tail_session: 14:45-15:00 边界（含双端闭区间）
- morning_pre_open_analysis: lunch_context 写入 / 集合竞价指数 / 盘前告警 / 通知文案
- check_overnight_gap_risk: 四态方向冲突 / 集合竞价估算平仓（basis=-16 → 估算开盘
  3360，预期亏损手算）/ 阈值 3000 元 / 指数未就绪 / basis 异常兜底
- check_overnight_reversal_risk: 信号收集 / 方向冲突文案 / 永远返回 False（真源语义）
- lunch_breakout_preview: 振幅/变动阈值 / atr_15 就绪 / 当日一次守护
- lunch_breakout_check: 守护链 / 6/17 方向修复（kospi_pct 决定方向，窗口 delta 反向）/ 
  SL/TP 手算 / 条件单 dict 键集 / fallback 路径
- lunch_force_close_check: 真源 quirk（deadline 恒 None → 永不触发）+ 手动设置后触发
- evaluate_overnight_holding: 节流/时点/尾盘新开守卫 / AI HOLD/CLOSE / 浮动盈亏手算
- post_open_analysis: ADJUST_STOP/ADJUST_PROFIT / 基差文案 / 无调整 / JSON 解析失败
"""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from quantai.jp_indices import JPIndicesService
from quantai.market_data import MarketDataService
from quantai.strategies.market_context import MarketContextService
from quantai.strategies.session_plays import SessionAction, SessionPlaysService


# ---------- 测试替身 ----------

class FakeTqApi:
    """tqsdk api 替身: 常数 K 线（ATR=2.0），get_position 记录调用。"""

    def __init__(self):
        self.get_position_calls = []

    def get_kline_serial(self, symbol, duration_seconds, data_length=None):
        return pd.DataFrame({"high": [10.0] * 21, "low": [8.0] * 21,
                             "close": [9.0] * 21})

    def wait_update(self, deadline=None):
        pass

    def get_position(self, symbol):
        self.get_position_calls.append(symbol)
        return {}


class FakeIndexFetcher:
    def __init__(self, klines=None, asian=None):
        self.klines = klines or {}
        self.asian = asian

    def get_kline_data(self, index_name, frequency):
        return self.klines.get(frequency)

    def get_asian_indices_5min_bars(self):
        if self.asian is None:
            raise RuntimeError("asian down")
        return self.asian


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


def make_bar(hm, open_, high, low, close, pct):
    return {"time": f"2026-08-28 {hm}:00", "open": open_, "high": high,
            "low": low, "close": close,
            "change_pct_from_prev_close": pct}


def make_asian(kospi_bars, kospi_prev=300.0, nk_pct=0.5, nk_prev=40000.0):
    nk_bars = [make_bar("09:30", 40000.0, 40100.0, 39900.0, 40050.0, nk_pct)]
    return {
        "indices": {
            "nikkei225": {"5min_bars": nk_bars, "prev_close": nk_prev},
            "kospi": {"5min_bars": kospi_bars, "prev_close": kospi_prev},
        },
        "timestamp": "2026-08-28 12:50:00",
    }


POSITION_SHORT = {"direction": "SHORT", "volume": 1, "entry_price": 3300.0,
                  "stop_loss": 3340.0, "take_profit": 3200.0,
                  "last_ai_decision": "test"}
POSITION_LONG = {"direction": "LONG", "volume": 1, "entry_price": 5000.0,
                 "stop_loss": 4900.0, "take_profit": 5100.0,
                 "last_ai_decision": "突破买入", "entry_time": "2026-08-28 10:00:00"}
# 空持仓（真源 current_position 初始键集 L147–154；方法内直接取键，须带全键）
EMPTY_POSITION = {"direction": None, "volume": 0, "entry_price": 0.0,
                  "stop_loss": 0.0, "take_profit": 0.0, "last_ai_decision": ""}


def make_service(asian=None, klines=None, im_last=5000.0, atr15=10.0,
                 ai=None, logger=None, news_items=None,
                 now=datetime(2026, 8, 28, 12, 50, 0)):
    api = FakeTqApi()
    fetcher = FakeIndexFetcher(klines=klines, asian=asian)
    mds = MarketDataService(api, fetcher, symbol="CFFEX.IM2608",
                            im_quote=SimpleNamespace(last_price=im_last))
    mcs = MarketContextService(api, symbol="CFFEX.IM2608")
    mcs.atr_15 = atr15
    notifier = FakeNotifier()
    holder = {"now": now}
    svc = SessionPlaysService(
        jp_service=JPIndicesService(fetcher), mds=mds, mcs=mcs,
        notifier=notifier, ai_chat_fn=ai, logger=logger,
        news_items_fn=(lambda: news_items) if news_items is not None else None,
        now_fn=lambda: holder["now"],
    )
    return svc, notifier, api, mds, mcs, holder


# ---------- check_tail_session ----------

@pytest.mark.parametrize("t,expected", [
    (datetime(2026, 8, 28, 14, 44).time(), False),
    (datetime(2026, 8, 28, 14, 45).time(), True),
    (datetime(2026, 8, 28, 14, 46).time(), True),
    (datetime(2026, 8, 28, 15, 0).time(), True),
])
def test_tail_session_boundaries(t, expected):
    svc, *_ = make_service()
    blocked, reason = svc.check_tail_session(t)
    assert blocked is expected
    if expected:
        assert reason == "尾盘时段（14:45-15:00）滑点大，禁止新开仓（仅允许调整已有持仓）"
    else:
        assert reason == ""


# ---------- morning_pre_open_analysis ----------

def test_morning_pre_auction_writes_context_and_notifies():
    """9:00 节点: 写 nk225_9am_pct（日经 pct=0.5）/kospi_9am_pct（0.8），
    不拉集合竞价，通知文案对拍。"""
    jp = make_asian([make_bar("09:00", 300.0, 301.0, 299.0, 300.5, 0.8)])
    svc, notifier, *_ = make_service(asian=jp, now=datetime(2026, 8, 28, 9, 0, 0))
    actions = svc.morning_pre_open_analysis(dict(EMPTY_POSITION))
    assert actions == []
    assert svc.lunch_context.get("nk225_9am_pct") == 0.5
    assert svc.lunch_context.get("kospi_9am_pct") == 0.8
    assert svc.lunch_context.get("index_call_auction") is None
    assert notifier.sent == ["📊 早盘前市场氛围 | 日经: +0.50%, KOSPI: +0.80%"]


def test_morning_auction_time_updates_index_price():
    """9:25:30 后: update_index_price + index_call_auction 写入。"""
    jp = make_asian([make_bar("09:00", 300.0, 301.0, 299.0, 300.5, 0.8)])
    klines = {"5min": pd.DataFrame({"close": [3990.0, 4000.0]})}
    svc, notifier, *_ = make_service(asian=jp, klines=klines,
                                     now=datetime(2026, 8, 28, 9, 26, 0))
    svc.morning_pre_open_analysis(dict(EMPTY_POSITION))
    assert svc.lunch_context.get("index_call_auction") == 4000.0
    assert notifier.sent[0] == ("📊 早盘前市场氛围 | 日经: +0.50%, KOSPI: +0.80% "
                                "| 集合竞价指数: 4000.00")


def test_morning_gap_alert_pre_auction():
    """9:00 节点 + SHORT + KOSPI +2.5% → 告警但不产生动作（真源 L3894–3901）。"""
    jp = make_asian([make_bar("09:00", 300.0, 306.0, 299.0, 307.5, 2.5)])
    svc, notifier, *_ = make_service(asian=jp, now=datetime(2026, 8, 28, 9, 0, 0))
    actions = svc.morning_pre_open_analysis(dict(POSITION_SHORT))
    assert actions == []
    assert notifier.sent[0] == (
        "⚠️ 盘前跳空风险: SHORT 持仓 + KOSPI 涨 +2.50% ≥ 2% (方向冲突)\n"
        "持仓 SHORT @ 3300.00, 止损 3340.00\n"
        "集合竞价出来后会自动调整止损"
    )


# ---------- check_overnight_gap_risk ----------

def test_gap_risk_no_conflict():
    """KOSPI +0.5% 无冲突 → None 且不告警。"""
    svc, notifier, *_ = make_service(
        asian=make_asian([make_bar("09:00", 300.0, 301.0, 299.0, 301.5, 0.5)]))
    jp = svc.jp.fetch_jp_indices()  # 传 fetch_jp_indices 输出格式（含 kospi_pct 顶层键）
    assert svc.check_overnight_gap_risk(jp, True, dict(POSITION_SHORT)) is None
    assert notifier.sent == []


def test_gap_risk_auction_close_action_hand_calculated():
    """集合竞价: index=4000、basis=-16 → 估算开盘 3360；SHORT 3300 →
    预期亏损 (3360-3300)×200 = 12000 ≥ 3000 → CLOSE_POSITION/BUY（手算对拍）。"""
    svc, notifier, _, mds, _, _ = make_service(
        asian=make_asian([make_bar("09:00", 300.0, 306.0, 299.0, 307.5, 2.5)]),
        klines={"5min": pd.DataFrame({"close": [4000.0]})}, im_last=3984.0)
    jp = svc.jp.fetch_jp_indices()
    mds.index_price = 4000.0
    action = svc.check_overnight_gap_risk(jp, True, dict(POSITION_SHORT))
    assert isinstance(action, SessionAction)
    assert action.action == "CLOSE_POSITION"
    assert action.close_direction == "BUY"
    assert action.volume == 1
    assert action.expected_loss == pytest.approx(12000.0)
    assert "盘前跳空主动平仓" in action.reason
    assert notifier.sent[0] == (
        "🚨 盘前跳空主动平仓: SHORT @ 3300.00\n"
        "集合竞价 4000.00, 估算开盘 3360.00\n"
        "预期亏损 12000元 ≥ 阈值 3000元"
    )


def test_gap_risk_long_conflict_sell():
    """LONG + KOSPI -2.5% → SELL 平仓建议。"""
    svc, _, _, mds, _, _ = make_service(
        asian=make_asian([make_bar("09:00", 300.0, 301.0, 293.0, 292.5, -2.5)]),
        klines={"5min": pd.DataFrame({"close": [4000.0]})}, im_last=3984.0)
    jp = svc.jp.fetch_jp_indices()
    mds.index_price = 4000.0
    pos = dict(POSITION_LONG, entry_price=4700.0)
    action = svc.check_overnight_gap_risk(jp, True, pos)
    assert action.close_direction == "SELL"
    # (4700-3360)×200 = 268000
    assert action.expected_loss == pytest.approx(268000.0)


def test_gap_risk_below_threshold_no_action():
    """SHORT 3350 → 预期亏损 (3360-3350)×200 = 2000 < 3000 → 无动作。"""
    svc, _, _, mds, _, _ = make_service(
        asian=make_asian([make_bar("09:00", 300.0, 306.0, 299.0, 307.5, 2.5)]),
        klines={"5min": pd.DataFrame({"close": [4000.0]})}, im_last=3984.0)
    jp = svc.jp.fetch_jp_indices()
    mds.index_price = 4000.0
    pos = dict(POSITION_SHORT, entry_price=3350.0)
    assert svc.check_overnight_gap_risk(jp, True, pos) is None


def test_gap_risk_index_not_ready():
    """集合竞价指数仍未就绪（update 后仍 0）→ 跳过平仓（真源 L3907–3911）。"""
    svc, *_ = make_service(
        asian=make_asian([make_bar("09:00", 300.0, 306.0, 299.0, 307.5, 2.5)]),
        klines={})  # 无 5min → update 失败
    jp = svc.jp.fetch_jp_indices()
    assert svc.check_overnight_gap_risk(jp, True, dict(POSITION_SHORT)) is None


def test_gap_risk_basis_exception_fallback_minus16(monkeypatch):
    """get_basis_info 异常 → basis=-16 兜底（真源 L3914–3918），估算结果一致。"""
    svc, _, _, mds, _, _ = make_service(
        asian=make_asian([make_bar("09:00", 300.0, 306.0, 299.0, 307.5, 2.5)]),
        klines={"5min": pd.DataFrame({"close": [4000.0]})}, im_last=3984.0)
    jp = svc.jp.fetch_jp_indices()
    mds.index_price = 4000.0

    def _boom():
        raise RuntimeError("basis down")

    monkeypatch.setattr(mds, "get_basis_info", _boom)
    action = svc.check_overnight_gap_risk(jp, True, dict(POSITION_SHORT))
    assert action is not None
    assert action.expected_loss == pytest.approx(12000.0)


# ---------- check_overnight_reversal_risk ----------

def test_reversal_no_position():
    svc, *_ = make_service()
    assert svc.check_overnight_reversal_risk({}) is False


def test_reversal_kospi_data_unavailable():
    svc, notifier, *_ = make_service(asian=None)
    assert svc.check_overnight_reversal_risk(dict(POSITION_SHORT)) is False
    assert notifier.sent == []


def test_reversal_signal_alert_and_always_false():
    """KOSPI 今日 -4%（≤-3）→ 1 个信号 → 告警；返回值恒 False（真源 L4043）。"""
    jp = make_asian([make_bar("09:30", 290.0, 292.0, 287.0, 288.0, -4.0)])
    svc, notifier, *_ = make_service(asian=jp)
    result = svc.check_overnight_reversal_risk(dict(POSITION_SHORT))
    assert result is False
    assert len(notifier.sent) == 1
    msg = notifier.sent[0]
    assert msg.startswith("📊 14:55 隔夜反弹风险告警（仅供参考，不自动平仓）")
    assert "KOSPI: 今日 -4.00% / 2日 -4.00% / 振幅 +1.67%" in msg
    assert "  • KOSPI 今日 -4.00%" in msg
    assert "⚠️ 持仓 SHORT @ 3300.00 方向冲突" in msg
    assert "参考 6/12 案例：KOSPI -4.31% 后次日跳空 +6.44% 穿止损 -29240" in msg


def test_reversal_no_signal_no_alert():
    """KOSPI -1% 且振幅小 → 无信号不告警。"""
    jp = make_asian([make_bar("09:30", 299.0, 300.0, 298.5, 297.0, -1.0)])
    svc, notifier, *_ = make_service(asian=jp)
    assert svc.check_overnight_reversal_risk(dict(POSITION_SHORT)) is False
    assert notifier.sent == []


# ---------- lunch_breakout_preview ----------

def test_preview_triggered_once_per_day():
    """振幅 1.33% ≥ 0.5 → 预览通知；当日第二次不再发（真源 L4056–4059）。"""
    jp = make_asian([make_bar("11:30", 300.0, 303.0, 299.0, 302.0, 0.9)])
    svc, notifier, *_ = make_service(asian=jp, atr15=5.0)
    svc.lunch_breakout_preview()
    assert len(notifier.sent) == 1
    assert notifier.sent[0].startswith("⚠️ 12:30 顺势单预览（12:50 可能触发）")
    assert "📊 KOSPI 11:30-12:30: 振幅 +1.33%, 变动 +0.67%" in notifier.sent[0]
    assert "📈 当日累计: KOSPI +0.90% (vs昨收) / 日经 +0.50%" in notifier.sent[0]
    svc.lunch_breakout_preview()
    assert len(notifier.sent) == 1


def test_preview_below_threshold_skipped():
    """振幅 0.33% < 0.5 且 |变动| 0.1% < 0.3 → 不发。"""
    jp = make_asian([make_bar("11:30", 300.0, 300.9, 299.9, 300.3, 0.1)])
    svc, notifier, *_ = make_service(asian=jp, atr15=5.0)
    svc.lunch_breakout_preview()
    assert notifier.sent == []


def test_preview_atr_not_ready_not_marked():
    """atr_15=0 → 不发且不标记当日已发（真源 L4079–4080 在标记之前）。"""
    jp = make_asian([make_bar("11:30", 300.0, 303.0, 299.0, 302.0, 0.9)])
    svc, notifier, _, _, mcs, _ = make_service(asian=jp, atr15=0.0)
    svc.lunch_breakout_preview()
    assert notifier.sent == []
    mcs.atr_15 = 5.0
    svc.lunch_breakout_preview()
    assert len(notifier.sent) == 1


def test_preview_window_none_skipped():
    """窗口无数据（bars 在 09:30）→ calc None → 不发。"""
    jp = make_asian([make_bar("09:30", 300.0, 306.0, 299.0, 302.0, 0.9)])
    svc, notifier, *_ = make_service(asian=jp, atr15=5.0)
    svc.lunch_breakout_preview()
    assert notifier.sent == []


# ---------- lunch_breakout_check ----------

def test_breakout_already_triggered():
    svc, *_ = make_service()
    svc.lunch_breakout_today['triggered'] = True
    assert svc.lunch_breakout_check(dict(POSITION_SHORT)) is None


def test_breakout_atr_not_ready():
    svc, *_ = make_service(asian=make_asian([]), atr15=0.0)
    assert svc.lunch_breakout_check(dict(POSITION_SHORT)) is None
    assert svc.lunch_breakout_today['triggered'] is False


def test_breakout_marks_triggered_before_position_check():
    """triggered 先置位（防 12:50-12:51 重复），已持仓 → 跳过但标记保留。"""
    svc, *_ = make_service(asian=make_asian([]), atr15=10.0)
    assert svc.lunch_breakout_check(dict(POSITION_SHORT)) is None
    assert svc.lunch_breakout_today['triggered'] is True


def test_breakout_jp_failure_keeps_triggered():
    """日韩数据未取到 → None，triggered 仍 True（避免 17 次重试，真源 L4121–4136）。"""
    svc, *_ = make_service(asian=None, atr15=10.0)
    assert svc.lunch_breakout_check(dict(EMPTY_POSITION)) is None
    assert svc.lunch_breakout_today['triggered'] is True


def test_breakout_amp_below_threshold():
    """振幅 0.67% < 1.0 → 不触发。"""
    jp = make_asian([make_bar("11:30", 300.0, 302.0, 300.0, 301.5, 0.9)])
    svc, notifier, *_ = make_service(asian=jp, atr15=10.0)
    assert svc.lunch_breakout_check(dict(EMPTY_POSITION)) is None
    assert notifier.sent == []


def test_breakout_delta_below_threshold():
    """振幅 1.33% 过阈值但变动 0.2% < 0.5 → 不触发（6/16 双向震荡过滤）。"""
    jp = make_asian([make_bar("11:30", 300.0, 304.0, 300.0, 300.6, 0.9)])
    svc, notifier, *_ = make_service(asian=jp, atr15=10.0)
    assert svc.lunch_breakout_check(dict(EMPTY_POSITION)) is None
    assert notifier.sent == []


def test_breakout_buy_full_payload_hand_calculated():
    """完整触发（BUY）: 振幅 1.33%、变动 -0.60%、kospi_pct=+0.9 → 6/17 修复锁定
    （方向取 vs昨收累积，窗口 delta 反向不影响）；SL=5000-0.2×10=4998、
    TP=5000+0.7×10=5007、触发价 4995（手算对拍）。"""
    jp = make_asian([make_bar("11:30", 300.0, 304.0, 300.0, 298.2, 0.9)], nk_pct=0.5)
    svc, notifier, *_ = make_service(asian=jp, im_last=5000.0, atr15=10.0)
    order = svc.lunch_breakout_check(dict(EMPTY_POSITION))
    assert order == {
        'action': 'BUY',
        'trigger_type': 'PRICE_ABOVE',
        'trigger_price': 4995.0,
        'limit_price': 0,
        'stop_loss': 4998.0,
        'take_profit': 5007.0,
        'volume': 1,
        'source': '12:50_lunch_breakout',
        'kospi_amp': 1.33,
        'kospi_delta': -0.6,
        'force_close_time': '14:00',
    }
    assert "🔥 12:50 顺势单触发: BUY 1手" in notifier.sent[0]
    assert "📊 KOSPI 11:30→12:50 午盘 振幅 +1.33%, 变动 -0.60%" in notifier.sent[0]
    assert "📈 当日累计: 日经 +0.50%, KOSPI +0.90%" in notifier.sent[0]
    assert "🛑 止损: 4998.00 (-2.0点 / -400元)" in notifier.sent[0]
    assert "🎯 止盈: 5007.00 (+7.0点 / 1400元)" in notifier.sent[0]
    assert "⚖️ 盈亏比 1:3.50" in notifier.sent[0]
    assert "📌 12:50 顺势单条件单已挂: BUY 1手, 触发价 4995.00 (PRICE_ABOVE)" in notifier.sent[1]
    # lunch_context 写入
    assert svc.lunch_context.get('kospi_1230_max_move') == 1.33
    assert svc.lunch_context.get('kospi_1230_delta') == -0.6
    assert svc.lunch_context.get('kospi_1230_pct') == 0.9
    assert svc.lunch_breakout_today['triggered'] is True
    assert 'trigger_time' in svc.lunch_breakout_today


def test_breakout_sell_direction():
    """kospi_pct=-0.9 → SELL，触发价 = last+5（PRICE_BELOW）。"""
    jp = make_asian([make_bar("11:30", 300.0, 304.0, 300.0, 301.8, -0.9)])
    svc, *_ = make_service(asian=jp, im_last=5000.0, atr15=10.0)
    order = svc.lunch_breakout_check(dict(EMPTY_POSITION))
    assert order['action'] == 'SELL'
    assert order['trigger_type'] == 'PRICE_BELOW'
    assert order['trigger_price'] == 5005.0
    assert order['stop_loss'] == 5002.0
    assert order['take_profit'] == 4993.0


def test_breakout_window_none_fallback_to_kospi_pct():
    """窗口计算失败 → fallback 到 kospi_pct（1.2/1.2 过双阈值，真源 L4150–4153）。"""
    jp = make_asian([make_bar("09:30", 300.0, 306.0, 299.0, 303.6, 1.2)])
    svc, *_ = make_service(asian=jp, im_last=5000.0, atr15=10.0)
    order = svc.lunch_breakout_check(dict(EMPTY_POSITION))
    assert order is not None
    assert order['kospi_amp'] == 1.2
    assert order['kospi_delta'] == 1.2
    assert svc.lunch_context.get('kospi_1230_max_move') == 1.2


def test_breakout_last_price_abnormal():
    """last_price=0 → 跳过（真源 L4184–4186）。"""
    jp = make_asian([make_bar("11:30", 300.0, 304.0, 300.0, 298.2, 0.9)])
    svc, *_ = make_service(asian=jp, im_last=0.0, atr15=10.0)
    assert svc.lunch_breakout_check(dict(EMPTY_POSITION)) is None


# ---------- lunch_force_close_check ----------

def test_force_close_not_triggered():
    svc, *_ = make_service()
    assert svc.lunch_force_close_check(dict(POSITION_SHORT)) is False


def test_force_close_no_position():
    svc, *_ = make_service()
    svc.lunch_breakout_today['triggered'] = True
    assert svc.lunch_force_close_check(dict(EMPTY_POSITION)) is False


def test_force_close_deadline_never_set_quirk():
    """真源 quirk 锁定: force_close_deadline 在活代码中从未赋值（唯一赋值点
    L4287 在 return 后不可达块）→ deadline 恒 None → 14:00 强平永不触发。"""
    svc, notifier, *_ = make_service(now=datetime(2026, 8, 28, 14, 1, 0))
    svc.lunch_breakout_today['triggered'] = True
    assert svc.lunch_breakout_today['force_close_deadline'] is None
    assert svc.lunch_force_close_check(dict(POSITION_SHORT)) is False
    assert notifier.sent == []


def test_force_close_fires_when_deadline_set():
    """编排层显式设置 deadline 后 → 14:00 触发平仓建议 + 通知。"""
    svc, notifier, *_ = make_service(now=datetime(2026, 8, 28, 14, 1, 0))
    svc.lunch_breakout_today['triggered'] = True
    svc.lunch_breakout_today['force_close_deadline'] = datetime(2026, 8, 28, 14, 0, 0)
    assert svc.lunch_force_close_check(dict(POSITION_SHORT)) is True
    assert notifier.sent == ["⏰ 12:50顺势单 14:00 强制平仓"]


def test_force_close_before_deadline():
    svc, notifier, *_ = make_service(now=datetime(2026, 8, 28, 13, 59, 0))
    svc.lunch_breakout_today['triggered'] = True
    svc.lunch_breakout_today['force_close_deadline'] = datetime(2026, 8, 28, 14, 0, 0)
    assert svc.lunch_force_close_check(dict(POSITION_SHORT)) is False
    assert notifier.sent == []


# ---------- evaluate_overnight_holding ----------

def test_overnight_no_position():
    svc, *_ = make_service()
    assert svc.evaluate_overnight_holding(dict(EMPTY_POSITION)) is None


def test_overnight_before_1455():
    svc, *_ = make_service(now=datetime(2026, 8, 28, 14, 54, 0))
    assert svc.evaluate_overnight_holding(dict(POSITION_LONG)) is None


def test_overnight_recent_entry_forced_hold():
    """14:30 后开仓 → 尾盘新开强制保留过夜（真源 L3563–3565）。"""
    svc, *_ = make_service(now=datetime(2026, 8, 28, 14, 55, 0))
    pos = dict(POSITION_LONG, entry_time="2026-08-28 14:30:00")
    assert svc.evaluate_overnight_holding(pos) is None


def test_overnight_recent_entry_datetime_object():
    """entry_time 为 datetime 对象（6/15 修复后格式）→ 同样识别尾盘新开。"""
    svc, *_ = make_service(now=datetime(2026, 8, 28, 14, 55, 0))
    pos = dict(POSITION_LONG, entry_time=datetime(2026, 8, 28, 14, 40, 0))
    assert svc.evaluate_overnight_holding(pos) is None


def test_overnight_no_entry_time_defaults_hold():
    """无 entry_time 记录（旧数据）→ 默认强制过夜（真源 L3556–3558）。"""
    svc, *_ = make_service(now=datetime(2026, 8, 28, 14, 55, 0))
    pos = {k: v for k, v in POSITION_LONG.items() if k != 'entry_time'}
    assert svc.evaluate_overnight_holding(pos) is None


def test_overnight_ai_hold_with_prompt_hand_calculated():
    """AI HOLD: 浮动盈亏 (5010-5000)×1×200 = 2000 元进 prompt（手算对拍）。"""
    captured = {}

    def ai(messages):
        captured['messages'] = messages
        return '{"action": "HOLD", "reason": "趋势未完"}'

    svc, notifier, api, *_ = make_service(
        asian=make_asian([make_bar("09:30", 299.0, 300.0, 298.5, 297.0, -1.0)]),
        im_last=5010.0, ai=ai, now=datetime(2026, 8, 28, 14, 55, 0),
        news_items=[{"time": "14:00", "data": {"content": "央行决议"}}])
    result = svc.evaluate_overnight_holding(dict(POSITION_LONG))
    assert result == {"action": "HOLD", "reason": "趋势未完"}
    # 真源 L3584 pos 赋值保留（经 mds.api）
    assert api.get_position_calls == ["CFFEX.IM2608"]
    user_prompt = captured['messages'][1]['content']
    assert "方向: LONG" in user_prompt
    assert "浮动盈亏: 2000.00 元" in user_prompt
    assert "当前价格（期货）: 5010.00" in user_prompt
    assert "- 14:00: 央行决议" in user_prompt
    assert "注意：当前时间 14:55:00" in user_prompt
    sys_prompt = captured['messages'][0]['content']
    assert "期货持仓过夜风险评估专家" in sys_prompt


def test_overnight_ai_close_suggestion():
    """AI CLOSE → 返回 CLOSE 建议（编排层执行平仓，真源 L3641–3642）。"""
    svc, *_ = make_service(
        asian=make_asian([make_bar("09:30", 299.0, 300.0, 298.5, 297.0, -1.0)]),
        ai=lambda messages: '{"action": "CLOSE", "reason": "黑天鹅事件"}',
        now=datetime(2026, 8, 28, 14, 55, 0))
    result = svc.evaluate_overnight_holding(dict(POSITION_LONG))
    assert result == {"action": "CLOSE",
                      "reason": "收盘前平仓（AI建议不过夜，理由：黑天鹅事件）"}


def test_overnight_ai_exception_defaults_hold():
    """AI 异常 → 记日志默认保留持仓（真源 L3645–3646）。"""
    def ai(messages):
        raise RuntimeError("llm down")

    svc, *_ = make_service(
        asian=make_asian([make_bar("09:30", 299.0, 300.0, 298.5, 297.0, -1.0)]),
        ai=ai, now=datetime(2026, 8, 28, 14, 55, 0))
    assert svc.evaluate_overnight_holding(dict(POSITION_LONG)) is None


def test_overnight_throttle_300s():
    """6/15 节流: 5 分钟内第二次调用直接跳过（真源 L3526–3530）。"""
    calls = []

    def ai(messages):
        calls.append(1)
        return '{"action": "HOLD", "reason": "ok"}'

    svc, *_ = make_service(
        asian=make_asian([make_bar("09:30", 299.0, 300.0, 298.5, 297.0, -1.0)]),
        ai=ai, now=datetime(2026, 8, 28, 14, 55, 0))
    assert svc.evaluate_overnight_holding(dict(POSITION_LONG)) is not None
    assert svc.evaluate_overnight_holding(dict(POSITION_LONG)) is None
    assert len(calls) == 1


# ---------- post_open_analysis ----------

def _post_open_klines():
    return {
        "5min": pd.DataFrame({"close": [3990.0, 4000.0]}),
        "日线": pd.DataFrame({"close": [5000.0] * 18 + [4980.0, 4950.0]}),
    }


def test_post_open_no_position():
    svc, *_ = make_service(klines=_post_open_klines())
    assert svc.post_open_analysis(dict(EMPTY_POSITION)) is None


def test_post_open_adjust_stop_hand_calculated():
    """AI 调止损 4950 → ADJUST_STOP 落日志 + 通知 + 返回建议（基差 -30/-0.60% 进 prompt）。"""
    captured = {}

    def ai(messages):
        captured['messages'] = messages
        return '{"adjust_stop_loss": 4950, "adjust_take_profit": null, "reason": "收紧止损"}'

    logger = FakeLogger()
    svc, notifier, *_ = make_service(klines=_post_open_klines(), im_last=4950.0,
                                     ai=ai, logger=logger)
    result = svc.post_open_analysis(dict(POSITION_LONG))
    assert result == {"adjust_stop_loss": 4950, "adjust_take_profit": None,
                      "reason": "收紧止损"}
    assert logger.events[0][0] == ("ADJUST_STOP", "CFFEX.IM2608", "LONG", 1, 4950.0)
    assert logger.events[0][1] == {"ai_reason": "收紧止损"}
    assert notifier.sent == ["IM开盘后调整: 止损4950, 止盈不变, 理由:收紧止损"]
    user_prompt = captured['messages'][1]['content']
    assert "中证1000指数今日开盘价：4000.00" in user_prompt
    assert "昨日基差: -30.00点 (-0.60%)" in user_prompt
    assert "- 状态: 贴水" in user_prompt
    assert "LONG 1手，开仓均价5000.00，当前止损4900.00，止盈5100.00" in user_prompt


def test_post_open_adjust_take_profit_only():
    """仅调止盈 → ADJUST_PROFIT，返回 SL=None。"""
    logger = FakeLogger()
    svc, notifier, *_ = make_service(
        klines=_post_open_klines(), im_last=4950.0,
        ai=lambda messages: '{"adjust_stop_loss": null, "adjust_take_profit": 5200, "reason": "扩大止盈"}',
        logger=logger)
    result = svc.post_open_analysis(dict(POSITION_LONG))
    assert result == {"adjust_stop_loss": None, "adjust_take_profit": 5200,
                      "reason": "扩大止盈"}
    assert logger.events[0][0] == ("ADJUST_PROFIT", "CFFEX.IM2608", "LONG", 1, 5200)
    assert notifier.sent == ["IM开盘后调整: 止损不变, 止盈5200, 理由:扩大止盈"]


def test_post_open_no_change():
    """AI 返回与当前相同的 SL/TP → 无需调整（真源 L4409–4410）。"""
    svc, notifier, *_ = make_service(
        klines=_post_open_klines(), im_last=4950.0,
        ai=lambda messages: '{"adjust_stop_loss": 4900, "adjust_take_profit": 5100, "reason": "维持"}')
    assert svc.post_open_analysis(dict(POSITION_LONG)) is None
    assert notifier.sent == []


def test_post_open_invalid_json():
    """AI 未返回 JSON → 记日志返回 None（真源 L4385–4387）。"""
    svc, *_ = make_service(
        klines=_post_open_klines(), im_last=4950.0,
        ai=lambda messages: "抱歉，我无法分析当前市场。")
    assert svc.post_open_analysis(dict(POSITION_LONG)) is None


def test_post_open_ai_exception():
    def ai(messages):
        raise RuntimeError("llm down")

    svc, *_ = make_service(klines=_post_open_klines(), im_last=4950.0, ai=ai)
    assert svc.post_open_analysis(dict(POSITION_LONG)) is None
