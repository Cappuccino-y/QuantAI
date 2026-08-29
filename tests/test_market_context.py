"""market_context + indicators 单测（阶段 3）— 行为对拍真源 autotrade_fix.py。

覆盖:
- indicators.calc_atr: 数据不足 → 0.0 / 常数波幅精确 ATR / 跳空缺口 TR /
  自定义 period（真源 L473–486）
- MarketContextService 状态字段默认值（真源 __init__ L399/L421–424）
- calculate_fut_atr: 正常路径 / OI 异常兜底 / atr_60=0 应激兜底 /
  外层异常保持默认不重置（真源 L459–513）
- compute_oi_state: 四态 + 平稳 + 数据不可用/异常（真源 L516–552）
- compute_dynamic_levels: n<5 兜底 / 正常路径（布林/20·50 高低/整数关口/VWAP
  去重排序）/ VWAP 缺 datetime 列 → 现价 / VWAP 异常 → 现价 / step=100 分档 /
  n<20 布林除 20 quirk 保真 / 外层异常兜底（真源 L1521–1606）
"""
import time
from datetime import datetime

import pandas as pd
import pytest

from quantai.strategies.indicators import calc_atr
from quantai.strategies.market_context import MarketContextService


# ---------- 测试替身 ----------

class FakeKlineApi:
    """tqsdk api 替身: 按周期返回预置 K 线 DataFrame，记录调用参数。"""

    def __init__(self, klines=None, raise_get=False):
        self.klines = klines or {}
        self.raise_get = raise_get
        self.wait_deadlines = []
        self.kline_calls = []

    def get_kline_serial(self, symbol, duration_seconds, data_length=None):
        self.kline_calls.append((symbol, duration_seconds, data_length))
        if self.raise_get:
            raise RuntimeError("kline down")
        return self.klines[duration_seconds]

    def wait_update(self, deadline=None):
        self.wait_deadlines.append(deadline)


def make_kline(n, high, low, close, open_oi=None, volume=None, datetime_col=None):
    """构造常数/逐值 K 线 DataFrame；参数为标量时填充 n 行，为序列时原样使用。"""
    def _col(v):
        return v if isinstance(v, (list, pd.Series)) else [v] * n

    data = {"high": _col(high), "low": _col(low), "close": _col(close)}
    if open_oi is not None:
        data["open_oi"] = _col(open_oi)
    if volume is not None:
        data["volume"] = _col(volume)
    if datetime_col is not None:
        data["datetime"] = _col(datetime_col)
    return pd.DataFrame(data)


def make_service(api=None, symbol="CFFEX.IM2608"):
    api = api or FakeKlineApi()
    return MarketContextService(api, symbol=symbol), api


# ---------- indicators.calc_atr ----------

def test_calc_atr_none_df_returns_zero():
    assert calc_atr(None) == 0.0


def test_calc_atr_insufficient_length_returns_zero():
    """len < period+1 → 0.0（真源 L474–475）。"""
    df = make_kline(14, high=10, low=8, close=9)  # 14 < 14+1
    assert calc_atr(df) == 0.0
    assert calc_atr(make_kline(3, high=10, low=8, close=9), period=3) == 0.0  # 3 < 4


def test_calc_atr_constant_range_exact():
    """常数 high=10/low=8/close=9 → 每根 TR=2 → ATR=2.0（手算对拍）。"""
    df = make_kline(15, high=10, low=8, close=9)
    assert calc_atr(df) == pytest.approx(2.0)


def test_calc_atr_gap_row_dominates_tr():
    """末根跳空: 前 14 根 TR=2，末根 high=21/low=19/close=20（prev_close=9）
    → TR=max(2,12,10)=12 → ATR=(13×2+12)/14=38/14（手算对拍）。"""
    highs = [10] * 14 + [21]
    lows = [8] * 14 + [19]
    closes = [9] * 14 + [20]
    df = make_kline(15, high=highs, low=lows, close=closes)
    assert calc_atr(df) == pytest.approx(38.0 / 14.0)


def test_calc_atr_custom_period():
    """period=3、len=4（恰好 period+1）→ 常数 TR=2 → ATR=2.0。"""
    df = make_kline(4, high=10, low=8, close=9)
    assert calc_atr(df, period=3) == pytest.approx(2.0)


# ---------- MarketContextService 状态字段默认值 ----------

def test_init_state_defaults_match_true_source():
    """真源 __init__ L399/L421–424 的 5 个状态字段默认值。"""
    svc, _ = make_service()
    assert svc.atr_5 == 0.0
    assert svc.atr_15 == 0.0
    assert svc.atr_60 == 0.0
    assert svc.stress_level == 1.0
    assert svc.oi_state_text == "持仓量数据不可用"


# ---------- calculate_fut_atr ----------

def test_calculate_fut_atr_happy_path():
    """正常路径: 三周期 ATR + stress + OI 四态文本 + wait_update(deadline≈now+5)。"""
    kline5 = make_kline(21, high=10, low=8, close=[9] * 20 + [9.5],
                        open_oi=[1000] * 20 + [1010])
    kline15 = make_kline(15, high=10, low=8, close=9)
    kline60 = make_kline(15, high=10, low=8, close=9)
    api = FakeKlineApi(klines={300: kline5, 900: kline15, 3600: kline60})
    svc, _ = make_service(api=api)

    before = time.time()
    svc.calculate_fut_atr()

    assert svc.atr_5 == pytest.approx(2.0)
    assert svc.atr_15 == pytest.approx(2.0)
    assert svc.atr_60 == pytest.approx(2.0)
    assert svc.stress_level == pytest.approx(1.0)  # atr_5/atr_60 = 2/2
    assert svc.oi_state_text == (
        "增仓上行（多头主动进攻，强）"
        "（当前OI=1010，前20根均值=1000，OI变化+1.0%）"
    )
    # 订阅参数保真: symbol + 5/15/60 分钟周期 + data_length=200（真源 L466–468）
    assert api.kline_calls == [
        ("CFFEX.IM2608", 300, 200),
        ("CFFEX.IM2608", 900, 200),
        ("CFFEX.IM2608", 3600, 200),
    ]
    assert len(api.wait_deadlines) == 1
    # 容差 abs=1（阶段 3 二批 minor 修正: 原 abs=5 无法区分 +0 与 +5 的错误实现）
    assert api.wait_deadlines[0] == pytest.approx(before + 5, abs=1)


def test_calculate_fut_atr_oi_exception_fallback():
    """OI 计算抛异常 → 兜底"持仓量数据不可用"，ATR 照常更新（真源 L495–500）。"""
    kline5 = make_kline(21, high=10, low=8, close=9,
                        open_oi=["abc"] * 21)  # float() 抛 ValueError
    api = FakeKlineApi(klines={
        300: kline5,
        900: make_kline(15, high=10, low=8, close=9),
        3600: make_kline(15, high=10, low=8, close=9),
    })
    svc, _ = make_service(api=api)
    svc.calculate_fut_atr()

    assert svc.oi_state_text == "持仓量数据不可用"
    assert svc.atr_5 == pytest.approx(2.0)
    assert svc.stress_level == pytest.approx(1.0)


def test_calculate_fut_atr_zero_atr60_stress_default():
    """atr_60=0（数据不足）→ stress_level 兜底 1.0（真源 L504–507 else 分支）。"""
    api = FakeKlineApi(klines={
        300: make_kline(21, high=10, low=8, close=9, open_oi=[1000.0] * 21),
        900: make_kline(15, high=10, low=8, close=9),
        3600: make_kline(5, high=10, low=8, close=9),  # 5 < 15 → calc_atr=0.0
    })
    svc, _ = make_service(api=api)
    svc.calculate_fut_atr()

    assert svc.atr_5 == pytest.approx(2.0)
    assert svc.atr_15 == pytest.approx(2.0)
    assert svc.atr_60 == 0.0
    assert svc.stress_level == 1.0


def test_calculate_fut_atr_outer_exception_keeps_state():
    """外层异常（K 线拉取失败）→ 仅记日志，状态字段保持原值不重置（真源 L512–513）。"""
    api = FakeKlineApi(raise_get=True)
    svc, _ = make_service(api=api)
    svc.atr_5 = 12.0   # 模拟上次计算残留
    svc.stress_level = 1.7
    svc.oi_state_text = "增仓上行（多头主动进攻，强）（当前OI=1，前20根均值=1，OI变化+0.0%）"

    svc.calculate_fut_atr()

    assert svc.atr_5 == 12.0
    assert svc.atr_15 == 0.0
    assert svc.atr_60 == 0.0
    assert svc.stress_level == 1.7
    assert svc.oi_state_text.startswith("增仓上行")


# ---------- compute_oi_state ----------

def test_oi_state_none_kline():
    svc, _ = make_service()
    assert svc.compute_oi_state(None) == "持仓量数据不可用（<21根或无 open_oi 列）"


def test_oi_state_too_few_rows():
    svc, _ = make_service()
    df = make_kline(20, high=10, low=8, close=9, open_oi=1000.0)
    assert svc.compute_oi_state(df) == "持仓量数据不可用（<21根或无 open_oi 列）"


def test_oi_state_missing_open_oi_column():
    svc, _ = make_service()
    df = make_kline(21, high=10, low=8, close=9)
    assert svc.compute_oi_state(df) == "持仓量数据不可用（<21根或无 open_oi 列）"


def test_oi_state_prev_mean_zero():
    svc, _ = make_service()
    df = make_kline(21, high=10, low=8, close=9, open_oi=0.0)
    assert svc.compute_oi_state(df) == "持仓量数据异常（前20根均值为0）"


@pytest.mark.parametrize("last_oi,last_close,expected_prefix", [
    (1010.0, 9.5, "增仓上行（多头主动进攻，强）"),
    (1010.0, 8.5, "增仓下行（空头主动进攻，强）"),
    (985.0, 9.5, "减仓上行（空头平仓驱动，弱反弹，禁止追多）"),
    (985.0, 8.5, "减仓下行（多头平仓驱动，弱回落，禁止追空）"),
])
def test_oi_state_four_states(last_oi, last_close, expected_prefix):
    """四态判定: 前 20 根 OI=1000，末根 ±；末根收盘 vs 前一根定价格方向。"""
    svc, _ = make_service()
    df = make_kline(21, high=10, low=8, close=[9] * 20 + [last_close],
                    open_oi=[1000.0] * 20 + [last_oi])
    assert svc.compute_oi_state(df).startswith(expected_prefix)


def test_oi_state_flat_exact_text():
    """|OI变化| ≤ 0.5% → 持仓量平稳，完整文案逐字对拍（真源 L551–552）。"""
    svc, _ = make_service()
    df = make_kline(21, high=10, low=8, close=[9] * 20 + [9.5],
                    open_oi=[1000.0] * 20 + [1003.0])
    assert svc.compute_oi_state(df) == (
        "持仓量平稳（+0.3%）（当前OI=1003，前20根均值=1000，OI变化+0.3%）"
    )


# ---------- compute_dynamic_levels ----------

def _levels_df_50():
    """50 根 K 线: 布林中轨 5000（std=0）/ n20 高低 5010·4990 / n50 高低 5020·4980 /
    前一根 5005·4995 / 两日 datetime（VWAP=5000）/ volume=1。"""
    highs = [5020.0] * 5 + [5010.0] * 43 + [5005.0, 5008.0]
    lows = [4980.0] * 5 + [4990.0] * 43 + [4995.0, 4992.0]
    closes = [5000.0] * 50
    dts = [pd.Timestamp("2026-08-27")] * 25 + [pd.Timestamp("2026-08-28")] * 25
    return make_kline(50, high=highs, low=lows, close=closes,
                      volume=[1.0] * 50, datetime_col=dts)


def test_levels_insufficient_data_fallback():
    """n<5 → ±30 兜底（真源 L1538–1539）。"""
    svc, _ = make_service()
    df = make_kline(4, high=10, low=8, close=9)
    resistance, support = svc.compute_dynamic_levels(df, 5007.0, "LONG")
    assert resistance == [5037.0]
    assert support == [4977.0]


def test_levels_normal_path_with_vwap():
    """正常路径: 布林/20·50 高低/前一根/整数关口/VWAP 全部命中，set 去重 + 双向排序。"""
    svc, _ = make_service()
    resistance, support = svc.compute_dynamic_levels(_levels_df_50(), 5007.0, "LONG")
    # bb_upper=5000, n20_high=5010, n50_high=5020, prev_high=5005, rn_above=5050, vwap=5000
    assert resistance == [5050.0, 5020.0, 5010.0, 5005.0, 5000.0]
    # bb_lower=5000, n20_low=4990, n50_low=4980, prev_low=4995, rn_below=5000, vwap=5000
    assert support == [4980.0, 4990.0, 4995.0, 5000.0]


def test_levels_vwap_missing_datetime_column_uses_cur_price():
    """缺 datetime 列 → inner except → vwap=cur_price（真源 L1568–1569/L1581–1582）。"""
    svc, _ = make_service()
    df = _levels_df_50().drop(columns=["datetime"])
    resistance, support = svc.compute_dynamic_levels(df, 5007.0, "LONG")
    assert 5007.0 in resistance
    assert 5007.0 in support
    assert resistance == [5050.0, 5020.0, 5010.0, 5007.0, 5005.0, 5000.0]
    assert support == [4980.0, 4990.0, 4995.0, 5000.0, 5007.0]


def test_levels_vwap_exception_uses_cur_price():
    """datetime 列存在但值无 .date()（字符串）→ inner except → vwap=cur_price。"""
    svc, _ = make_service()
    df = _levels_df_50()
    df["datetime"] = ["2026-08-28"] * 50
    resistance, support = svc.compute_dynamic_levels(df, 5007.0, "LONG")
    assert 5007.0 in resistance
    assert 5007.0 in support


def test_levels_step_100_above_9000():
    """cur_price ≥ 9000 → 整数关口步长 100（真源 L1559）。"""
    svc, _ = make_service()
    df = make_kline(20, high=9060.0, low=9040.0, close=9050.0)
    resistance, support = svc.compute_dynamic_levels(df, 9050.0, "SHORT")
    # rn_above=(90+1)×100=9100, rn_below=90×100=9000；vwap=9050（无 datetime 列）
    assert resistance == [9100.0, 9060.0, 9050.0]
    assert support == [9000.0, 9040.0, 9050.0]


def test_levels_n_lt_20_bb_divides_by_20_quirk():
    """真源 quirk 保真: n<20 时 bb_mid 仍除以 20（L1542）。5 根 close=5000
    → bb_mid=1250，但 std 围绕 1250 计算=1875 → bb_upper=1250+3750=5000、
    bb_lower=1250-3750=-2500（布林轨退化为垃圾值）。生产 200 根 K 线不触发，
    测试锁定该行为防漂移。"""
    svc, _ = make_service()
    df = make_kline(5, high=5010.0, low=4990.0, close=5000.0)
    resistance, support = svc.compute_dynamic_levels(df, 5007.0, "LONG")
    # resistance = sorted(set([bb_upper=5000, 5010, 5010, 5010, rn_above=5050, vwap=5007]), reverse=True)
    assert resistance == [5050.0, 5010.0, 5007.0, 5000.0]
    # support = sorted(set([bb_lower=-2500, 4990, 4990, 4990, rn_below=5000, vwap=5007]))
    assert support == [-2500.0, 4990.0, 5000.0, 5007.0]


def test_levels_outer_exception_fallback():
    """外层异常（缺 close 列）→ 告警日志 + ±30 兜底（真源 L1604–1606）。"""
    svc, _ = make_service()
    df = pd.DataFrame({"open": [1.0] * 10})
    resistance, support = svc.compute_dynamic_levels(df, 5007.0, "LONG")
    assert resistance == [5037.0]
    assert support == [4977.0]
