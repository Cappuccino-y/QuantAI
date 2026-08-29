"""market_data 单测（阶段 2）— 行为对拍真源 autotrade_fix.py。

覆盖:
- format_code 格式化（真源 L693）
- ContractResolver: 候选推算（含 12 月跨年）/ OI 选主 / 无 OI 兜底（真源 L687–745）
- TradingCalendar: 交易日 akshare 路径 + weekday 兜底 / 上一交易日 15:00 /
  交易时段三窗口边界 / 临近休市窗口（真源 L949–967 / L5340–5351 / L3648–3657）
- AccountView.get_equity 三路径（真源 L805–811）
- MarketDataService: 指数价刷新 / 技术面刷新 / 基差（含到期日兜底）/ 指数→期货换算
  0.2 圆整 / 昨收获取（真源 L675–684 / L748–784 / L969–976 / L3669–3680）
"""
import sys
import types
from datetime import datetime

import pandas as pd
import pytest

from quantai.market_data import (AccountView, ContractResolver, MarketDataService,
                                 TradingCalendar, format_code)


# ---------- 测试替身 ----------

class FakeQuote:
    def __init__(self, open_interest=None, last_price=0.0):
        self.open_interest = open_interest
        self.last_price = last_price


class FakeApi:
    """tqsdk api 替身: 按合约代码返回报价，记录调用顺序。"""

    def __init__(self, quotes=None, contract_info=None, account=None,
                 raise_contract_info=False):
        self.quotes = quotes or {}
        self.contract_info = contract_info
        self.account = account
        self.raise_contract_info = raise_contract_info
        self.requested = []

    def get_quote(self, sym):
        self.requested.append(sym)
        q = self.quotes.get(sym)
        if q is None:
            q = FakeQuote(open_interest=None)
            self.quotes[sym] = q
        return q

    def get_contract_info(self, sym):
        if self.raise_contract_info:
            raise RuntimeError("no contract info")
        return self.contract_info

    def get_account(self):
        if self.account is None:
            return None
        return self.account


class FakeAccount:
    def __init__(self, balance, position_profit):
        self.balance = balance
        self.position_profit = position_profit


class FakeFetcher:
    """IndexDataFetcher 替身: 返回预置 K 线 / prompt，可注入异常。"""

    def __init__(self, kline_df=None, prompt="PROMPT", raise_kline=False):
        self.kline_df = kline_df
        self.prompt = prompt
        self.raise_kline = raise_kline
        self.kline_calls = []

    def get_kline_data(self, index_name, frequency):
        self.kline_calls.append((index_name, frequency))
        if self.raise_kline:
            raise RuntimeError("network down")
        return self.kline_df

    def generate_ai_prompt(self, index_name, periods):
        return self.prompt


def make_service(api=None, fetcher=None, **kw):
    api = api or FakeApi()
    fetcher = fetcher or FakeFetcher()
    svc = MarketDataService(api, fetcher, symbol="CFFEX.IM2608",
                            im_quote=FakeQuote(last_price=5000.0), **kw)
    return svc, api, fetcher


# ---------- format_code ----------

def test_format_code_zero_padding():
    assert format_code(26, 3) == "CFFEX.IM2603"
    assert format_code(26, 12) == "CFFEX.IM2612"
    assert format_code(27, 1) == "CFFEX.IM2701"


# ---------- ContractResolver ----------

def test_resolver_picks_max_oi():
    api = FakeApi(quotes={
        "CFFEX.IM2608": FakeQuote(open_interest=100),
        "CFFEX.IM2609": FakeQuote(open_interest=200),
        "CFFEX.IM2612": FakeQuote(open_interest=50),
    })
    resolver = ContractResolver(api, now_fn=lambda: datetime(2026, 8, 15))
    assert resolver.get_dominant_im() == "CFFEX.IM2609"


def test_resolver_candidates_august():
    """8 月候选: 当月/次月/次季/次次季（次月与次季重合是真源行为）。"""
    api = FakeApi()
    ContractResolver(api, now_fn=lambda: datetime(2026, 8, 15)).get_dominant_im()
    assert api.requested == ["CFFEX.IM2608", "CFFEX.IM2609", "CFFEX.IM2609", "CFFEX.IM2612"]


def test_resolver_candidates_december_cross_year():
    """12 月候选跨年: 次月=次年01、次季=次年03、次次季=次年06（真源 L698–724）。"""
    api = FakeApi()
    ContractResolver(api, now_fn=lambda: datetime(2026, 12, 15)).get_dominant_im()
    assert api.requested == ["CFFEX.IM2612", "CFFEX.IM2701", "CFFEX.IM2703", "CFFEX.IM2706"]


def test_resolver_fallback_to_current_when_no_oi():
    """全部候选 open_interest=0（oi 非 None 但不 >0）→ 兜底当月合约（真源 L96–104）。

    阶段 2 验收 minor3: 原写法 `quotes={s: FakeQuote(open_interest=0) for s in []}`
    是空字典推导，等价于 FakeApi()，实际走的是 oi=None 缺省分支；本用例显式构造
    oi=0，覆盖 `oi is not None and oi > 0` 的后半分支。
    """
    api = FakeApi(quotes={
        "CFFEX.IM2608": FakeQuote(open_interest=0),
        "CFFEX.IM2609": FakeQuote(open_interest=0),
        "CFFEX.IM2612": FakeQuote(open_interest=0),
    })
    resolver = ContractResolver(api, now_fn=lambda: datetime(2026, 8, 15))
    assert resolver.get_dominant_im() == "CFFEX.IM2608"


# ---------- TradingCalendar ----------

def _fake_akshare(trade_dates):
    mod = types.ModuleType("akshare")

    def tool_trade_date_hist_sina():
        return pd.DataFrame({"trade_date": pd.to_datetime(trade_dates)})

    mod.tool_trade_date_hist_sina = tool_trade_date_hist_sina
    return mod


def test_is_trading_day_akshare_path(monkeypatch):
    """akshare 日历优先: 工作日但不在日历 → False（证明走的是日历而非 weekday）。

    注: akshare 路径返回 numpy.bool_（真源同款，真值性一致），故用真值断言而非 is True。
    """
    monkeypatch.setitem(sys.modules, "akshare",
                        _fake_akshare(["2026-08-27", "2026-08-28"]))
    cal = TradingCalendar()
    assert cal.is_trading_day(datetime(2026, 8, 28))       # 周五且在日历
    assert not cal.is_trading_day(datetime(2026, 8, 31))   # 周一但不在日历


def test_is_trading_day_weekday_fallback(monkeypatch):
    """akshare 不可用 → 周一至周五简易判断（真源 L965–967 兜底）。"""
    monkeypatch.setitem(sys.modules, "akshare", None)  # import 即失败
    cal = TradingCalendar()
    assert cal.is_trading_day(datetime(2026, 8, 28)) is True   # 周五
    assert cal.is_trading_day(datetime(2026, 8, 29)) is False  # 周六
    assert cal.is_trading_day(datetime(2026, 8, 30)) is False  # 周日


def test_previous_trading_day_15_skips_weekend(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", None)  # 走 weekday 兜底
    cal = TradingCalendar()
    # 周一 2026-08-31 → 上一交易日 = 周五 2026-08-28 15:00
    result = cal.get_previous_trading_day_15(datetime(2026, 8, 31, 10, 30))
    assert result == datetime(2026, 8, 28, 15, 0, 0, 0)
    # 周五 → 周四 15:00
    result = cal.get_previous_trading_day_15(datetime(2026, 8, 28, 9, 0))
    assert result == datetime(2026, 8, 27, 15, 0, 0, 0)


@pytest.mark.parametrize("dt,expected", [
    (datetime(2026, 8, 28, 9, 29), False),
    (datetime(2026, 8, 28, 9, 30), True),
    (datetime(2026, 8, 28, 11, 30), True),
    (datetime(2026, 8, 28, 11, 31), False),
    (datetime(2026, 8, 28, 12, 30), False),
    (datetime(2026, 8, 28, 13, 0), True),
    (datetime(2026, 8, 28, 15, 0), True),
    (datetime(2026, 8, 28, 15, 1), False),
    (datetime(2026, 8, 28, 21, 0), True),
    (datetime(2026, 8, 28, 23, 0), True),
    (datetime(2026, 8, 28, 23, 1), False),
    (datetime(2026, 8, 29, 10, 0), False),  # 周六
])
def test_is_trading_time_windows(dt, expected):
    cal = TradingCalendar()
    assert cal.is_trading_time(dt) is expected


@pytest.mark.parametrize("hh,mm,expected", [
    (11, 24, False),
    (11, 25, True),
    (11, 30, True),
    (14, 54, False),
    (14, 55, True),
    (15, 0, True),
    (10, 0, False),
])
def test_is_near_close_windows(hh, mm, expected):
    cal = TradingCalendar(now_fn=lambda: datetime(2026, 8, 28, hh, mm))
    assert cal.is_near_close() is expected


# ---------- AccountView ----------

def test_get_equity_balance_plus_position_profit():
    api = FakeApi(account=FakeAccount(100000.0, 500.0))
    assert AccountView(api).get_equity() == 100500.0


def test_get_equity_none_account():
    assert AccountView(FakeApi(account=None)).get_equity() == 0.0


def test_get_equity_exception_returns_zero():
    class BoomApi:
        def get_account(self):
            raise RuntimeError("boom")

    assert AccountView(BoomApi()).get_equity() == 0.0


# ---------- MarketDataService ----------

def test_update_index_price_takes_last_close():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    svc, _, fetcher = make_service(fetcher=FakeFetcher(kline_df=df))
    svc.update_index_price()
    assert svc.index_price == 3.0
    assert fetcher.kline_calls == [("中证1000", "5min")]


def test_update_index_price_empty_df_keeps_old(monkeypatch):
    svc, _, _ = make_service(fetcher=FakeFetcher(kline_df=pd.DataFrame({"close": []})))
    svc.index_price = 42.0
    svc.update_index_price()
    assert svc.index_price == 42.0  # 空数据 → 告警且不覆盖


def test_update_index_price_exception_keeps_old():
    svc, _, _ = make_service(fetcher=FakeFetcher(raise_kline=True))
    svc.index_price = 42.0
    svc.update_index_price()
    assert svc.index_price == 42.0


def test_refresh_tech_data_sets_text():
    svc, _, _ = make_service(fetcher=FakeFetcher(prompt="TECH-PROMPT"))
    svc.refresh_tech_data()
    assert svc.tech_data_text == "TECH-PROMPT"


def test_refresh_tech_data_exception_keeps_old():
    class BoomFetcher(FakeFetcher):
        def generate_ai_prompt(self, index_name, periods):
            raise RuntimeError("boom")

    svc, _, _ = make_service(fetcher=BoomFetcher())
    svc.tech_data_text = "OLD"
    svc.refresh_tech_data()
    assert svc.tech_data_text == "OLD"


def test_get_basis_info_with_contract_info():
    expiry_epoch = datetime(2026, 9, 18, 0, 0).timestamp()
    api = FakeApi(contract_info={"expire_datetime": expiry_epoch})
    svc, _, _ = make_service(api=api)
    svc.index_price = 4900.0
    info = svc.get_basis_info()
    assert info["index_price"] == 4900.0
    assert info["im_price"] == 5000.0
    assert info["basis"] == 100.0
    assert info["basis_pct"] == pytest.approx(100.0 / 4900.0 * 100)
    assert info["days_to_expiry"] == (datetime.fromtimestamp(expiry_epoch) - datetime.now()).days
    assert info["symbol"] == "CFFEX.IM2608"


def test_get_basis_info_fallback_expiry_day15():
    """get_contract_info 失败 → 代码解析兜底: IM2609 → 2026-09-15（真源 L770–774）。"""
    api = FakeApi(raise_contract_info=True)
    svc, _, _ = make_service(api=api, )
    svc.symbol = "CFFEX.IM2609"
    svc.index_price = 4900.0
    info = svc.get_basis_info()
    expected_days = (datetime(2026, 9, 15) - datetime.now()).days
    assert info["days_to_expiry"] == expected_days


def test_index_to_future_price_basis_rate_and_tick():
    svc, _, _ = make_service()
    svc.index_price = 4900.0  # fut last_price = 5000
    # 4900 × (5000/4900) = 5000 → 圆整 0.2 后仍 5000
    assert svc.index_to_future_price(4900.0) == pytest.approx(5000.0)


def test_index_to_future_price_fallback_when_no_state():
    """index_price/fut 未就绪 → 直接按 0.2 圆整（真源 L974 fallback）。"""
    svc, _, _ = make_service()
    svc.index_price = 0.0
    assert svc.index_to_future_price(100.0) == pytest.approx(100.0)


def test_get_yesterday_index_close():
    df = pd.DataFrame({"close": [100.0, 200.0, 300.0]})
    svc, _, _ = make_service(fetcher=FakeFetcher(kline_df=df))
    assert svc.get_yesterday_index_close() == 200.0


def test_get_yesterday_index_close_insufficient_data():
    svc, _, _ = make_service(fetcher=FakeFetcher(kline_df=pd.DataFrame({"close": [100.0]})))
    assert svc.get_yesterday_index_close() is None


def test_get_yesterday_index_close_exception():
    svc, _, _ = make_service(fetcher=FakeFetcher(raise_kline=True))
    assert svc.get_yesterday_index_close() is None
