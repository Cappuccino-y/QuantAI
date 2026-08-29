"""jp_indices 单测（阶段 2）— 窗口计算纯逻辑对拍真源 autotrade_fix.py。

覆盖:
- fetch_jp_indices: 数据组装 / 60s 缓存命中与过期 / 异常返回 None（真源 L3684–3723）
- calc_nk225_max_move_in_window: 窗口过滤（双端闭区间）/ 振幅公式 / 缺数据兜底（L3725–3754）
- calc_kospi_amp_delta_in_window: amp/delta 公式 / 首开末收 / 缺数据兜底（L3756–3801）
- refresh_lunch_context: 写键 + update_time + 日志文案（L3803–3807）
- create_default_lunch_context: 真源 L438–447 初始键集
- news_manager prev_trading_day_fn 接线契约（阶段 2 → TradingCalendar 注入）
"""
import sys
from datetime import datetime

import pytest

from quantai.jp_indices import (JPIndicesService, create_default_lunch_context,
                                refresh_lunch_context)
from quantai.models import LunchContext


RAW = {
    'indices': {
        'nikkei225': {
            'prev_close': 40000.0,
            '5min_bars': [
                {'time': '2026-08-28 11:25:00', 'high': 40100.0, 'low': 40050.0,
                 'open': 40060.0, 'close': 40090.0, 'change_pct_from_prev_close': 0.2},
                {'time': '2026-08-28 11:30:00', 'high': 40200.0, 'low': 40080.0,
                 'open': 40090.0, 'close': 40150.0, 'change_pct_from_prev_close': 0.3},
                {'time': '2026-08-28 12:00:00', 'high': 40300.0, 'low': 40100.0,
                 'open': 40150.0, 'close': 40250.0, 'change_pct_from_prev_close': 0.4},
                {'time': '2026-08-28 12:30:00', 'high': 40250.0, 'low': 40120.0,
                 'open': 40250.0, 'close': 40180.0, 'change_pct_from_prev_close': 0.45},
                {'time': '2026-08-28 12:50:00', 'high': 40220.0, 'low': 40100.0,
                 'open': 40180.0, 'close': 40200.0, 'change_pct_from_prev_close': 0.5},
            ],
        },
        'kospi': {
            'prev_close': 300.0,
            '5min_bars': [
                {'time': '2026-08-28 11:30:00', 'high': 301.0, 'low': 300.5,
                 'open': 300.6, 'close': 300.8, 'change_pct_from_prev_close': 0.2},
                {'time': '2026-08-28 12:00:00', 'high': 302.0, 'low': 300.7,
                 'open': 300.8, 'close': 301.5, 'change_pct_from_prev_close': 0.3},
                {'time': '2026-08-28 12:50:00', 'high': 301.8, 'low': 301.0,
                 'open': 301.5, 'close': 301.2, 'change_pct_from_prev_close': 0.4},
            ],
        },
    },
    'timestamp': '2026-08-28 12:51:00',
}


class FakeJpFetcher:
    def __init__(self, raw=None, raise_error=False):
        self.raw = raw if raw is not None else RAW
        self.raise_error = raise_error
        self.call_count = 0

    def get_asian_indices_5min_bars(self):
        self.call_count += 1
        if self.raise_error:
            raise RuntimeError("network down")
        return self.raw


# ---------- fetch_jp_indices ----------

def test_fetch_assembles_snapshot():
    fetcher = FakeJpFetcher()
    svc = JPIndicesService(fetcher)
    data = svc.fetch_jp_indices()
    assert data['nk225_now'] == 40200.0          # 末根 bar 收盘
    assert data['nk225_pct'] == 0.5              # 末根 change_pct_from_prev_close
    assert data['nk225_prev_close'] == 40000.0
    assert data['kospi_now'] == 301.2
    assert data['kospi_pct'] == 0.4
    assert data['kospi_prev_close'] == 300.0
    assert len(data['nk225_5min']) == 5
    assert len(data['kospi_5min']) == 3
    assert data['ts'] == '2026-08-28 12:51:00'


def test_fetch_60s_cache_hit():
    fetcher = FakeJpFetcher()
    svc = JPIndicesService(fetcher)
    svc.fetch_jp_indices()
    svc.fetch_jp_indices()
    svc.fetch_jp_indices()
    assert fetcher.call_count == 1  # 60s 内命中缓存


def test_fetch_cache_expires_after_60s():
    fetcher = FakeJpFetcher()
    svc = JPIndicesService(fetcher)
    svc.fetch_jp_indices()
    svc._jp_cache['time'] -= 61  # 模拟缓存过期
    svc.fetch_jp_indices()
    assert fetcher.call_count == 2


def test_fetch_exception_returns_none():
    svc = JPIndicesService(FakeJpFetcher(raise_error=True))
    assert svc.fetch_jp_indices() is None


# ---------- calc_nk225_max_move_in_window ----------

def test_nk225_window_max_move():
    """11:30–12:30 窗口: max_high=40300, min_low=40080 → (220/40000)×100 = 0.55。"""
    svc = JPIndicesService(FakeJpFetcher())
    assert svc.calc_nk225_max_move_in_window('11:30', '12:30') == 0.55


def test_nk225_window_inclusive_bounds():
    """双端闭区间: 仅 11:30 与 12:30 两根 → (40250-40080)/40000×100 = 0.425 → 0.43。"""
    # bar 需带 close/change_pct_from_prev_close（真源 fetch_jp_indices 取末根这两键）
    raw = {'indices': {'nikkei225': {'prev_close': 40000.0, '5min_bars': [
        {'time': '2026-08-28 11:30:00', 'high': 40200.0, 'low': 40080.0,
         'open': 40090.0, 'close': 40150.0, 'change_pct_from_prev_close': 0.3},
        {'time': '2026-08-28 12:30:00', 'high': 40250.0, 'low': 40120.0,
         'open': 40250.0, 'close': 40180.0, 'change_pct_from_prev_close': 0.45},
    ]}}, 'timestamp': 't'}
    svc2 = JPIndicesService(FakeJpFetcher(raw=raw))
    assert svc2.calc_nk225_max_move_in_window('11:30', '12:30') == round(
        (40250.0 - 40080.0) / 40000.0 * 100, 2)


def test_nk225_empty_window_returns_none():
    svc = JPIndicesService(FakeJpFetcher())
    assert svc.calc_nk225_max_move_in_window('12:31', '12:49') is None


def test_nk225_missing_prev_close_returns_none():
    raw = {'indices': {'nikkei225': {'5min_bars': [
        {'time': '2026-08-28 12:00:00', 'high': 40300.0, 'low': 40100.0},
    ]}}}
    svc = JPIndicesService(FakeJpFetcher(raw=raw))
    assert svc.calc_nk225_max_move_in_window('11:30', '12:30') is None


# ---------- calc_kospi_amp_delta_in_window ----------

def test_kospi_amp_delta():
    """11:30–12:50: first_open=300.6, last_close=301.2, high=302.0, low=300.5。"""
    svc = JPIndicesService(FakeJpFetcher())
    r = svc.calc_kospi_amp_delta_in_window('11:30', '12:50')
    assert r['amp'] == round((302.0 - 300.5) / 300.6 * 100, 2)
    assert r['delta'] == round((301.2 - 300.6) / 300.6 * 100, 2)
    assert r['max_high'] == 302.0
    assert r['min_low'] == 300.5
    assert r['first_open'] == 300.6
    assert r['last_close'] == 301.2


def test_kospi_empty_window_returns_none():
    svc = JPIndicesService(FakeJpFetcher())
    assert svc.calc_kospi_amp_delta_in_window('13:00', '14:00') is None


def test_kospi_missing_prev_close_returns_none():
    raw = {'indices': {'kospi': {'5min_bars': [
        {'time': '2026-08-28 12:00:00', 'high': 302.0, 'low': 300.7,
         'open': 300.8, 'close': 301.5},
    ]}}}
    svc = JPIndicesService(FakeJpFetcher(raw=raw))
    assert svc.calc_kospi_amp_delta_in_window('11:30', '12:50') is None


# ---------- lunch_context ----------

def test_default_lunch_context_keys():
    """真源 L438–447 初始键集（update_time 由模型字段承载）。"""
    ctx = create_default_lunch_context()
    for k in ('nk225_9am_pct', 'nk225_1130_pct', 'nk225_1230_pct',
              'topix_1230_pct', 'nk225_1230_max_move',
              'index_call_auction', 'index_last_close'):
        assert ctx.get(k) is None
    assert ctx.update_time == ""


def test_refresh_lunch_context_writes_key_and_time(caplog):
    ctx = create_default_lunch_context()
    with caplog.at_level("INFO"):
        refresh_lunch_context(ctx, 'nk225_9am_pct', -0.52)
    assert ctx.get('nk225_9am_pct') == -0.52
    # update_time 与真源同格式 %H:%M:%S
    datetime.strptime(ctx.update_time, '%H:%M:%S')
    # 日志文案逐字对齐真源 L3807
    assert "[日韩联动] nk225_9am_pct = -0.52 @" in caplog.text


def test_refresh_lunch_context_updates_time_each_call():
    ctx = LunchContext()
    refresh_lunch_context(ctx, 'a', 1)
    t1 = ctx.update_time
    refresh_lunch_context(ctx, 'b', 2)
    assert ctx.get('b') == 2
    assert ctx.update_time >= t1  # 单调刷新


# ---------- news_manager 接线契约（阶段 2 交付点） ----------

def test_news_manager_wires_trading_calendar(monkeypatch):
    """TradingCalendar.get_previous_trading_day_15 可注入 NewsManager，
    回补起点 = 注入函数的返回值（阶段 1 遗留接线的验证）。"""
    import quantai.news_manager as nm_module
    from quantai.market_data import TradingCalendar
    from quantai.news_manager import NewsManager

    monkeypatch.setattr(nm_module.time, "sleep", lambda s: None)  # 阻止真睡眠
    monkeypatch.setitem(sys.modules, "akshare", None)  # 日历走 weekday 兜底，离线确定

    # 固定 NewsManager 内部的 datetime.now()，消除真实日期依赖
    class FakeDateTime(datetime):
        @classmethod
        def now(cls):
            return datetime(2026, 8, 28, 9, 0)

    monkeypatch.setattr(nm_module, "datetime", FakeDateTime)

    captured = {}

    class RecordingFetcher:
        def fetch_important_news(self, start_str, end_str):
            captured['start_str'] = start_str
            return []

    cal = TradingCalendar(now_fn=lambda: datetime(2026, 8, 28, 9, 0))
    nm = NewsManager(fetcher=RecordingFetcher(),
                     prev_trading_day_fn=cal.get_previous_trading_day_15)
    nm._backfill_historical_news()
    # 2026-08-28 是周五 → 上一交易日 = 周四 2026-08-27 15:00
    assert captured['start_str'] == "2026-08-27 15:00:00"
