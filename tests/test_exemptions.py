"""exemptions 单测（阶段 3 第二批）— 行为对拍真源 L4707–4943。

覆盖:
- trend_reversal_exempt: 数据不足不豁免 / 趋势未转 / 2/5 未站稳 / 阴线 /
  实体 <60% / 完整通过（实体 71% 手算）/ SHORT 镜像 / 异常不豁免
- htf_partial_allowance: 未反转 / 已反转双向 / 数据不足
- volume_vcp_check: 量未递增 / close 未走高 / 量价齐升通过 / 数据不足
- vwap_alignment: tail(48) 路径（current_date_str 从未赋值的真源 quirk）/
  close vs VWAP 双向拦截 / slope ±1.0 拦截（手算对拍）/ 数据不足放行
"""
import pandas as pd
import pytest

from quantai.strategies.exemptions import Exemptions


class FakeIndexFetcher:
    def __init__(self, klines=None):
        self.klines = klines or {}

    def get_kline_data(self, index_name, frequency):
        return self.klines.get(frequency)


def make_df(close, open_=None, high=None, low=None, volume=None):
    n = len(close)
    data = {
        "close": list(close),
        "open": list(open_) if open_ is not None else list(close),
        "high": list(high) if high is not None else [c + 2 for c in close],
        "low": list(low) if low is not None else [c - 2 for c in close],
        "volume": list(volume) if volume is not None else [100.0] * n,
    }
    return pd.DataFrame(data)


def make_exemptions(klines=None):
    return Exemptions(FakeIndexFetcher(klines=klines))


# ---------- trend_reversal_exempt ----------

def _choch_df(closes, last_open, last_high, last_low):
    """10 根 60min: 前 9 根 open=close（十字），末根自定义 OHLC。"""
    opens = [closes[0]] * 9 + [last_open]
    highs = [c + 2 for c in closes[:9]] + [last_high]
    lows = [c - 2 for c in closes[:9]] + [last_low]
    return make_df(closes, open_=opens, high=highs, low=lows)


def test_choch_insufficient_data_no_exempt():
    r = make_exemptions({"60min": make_df([5000.0] * 4)}).trend_reversal_exempt("LONG")
    assert r.allowed is False
    assert r.reason == "60min 数据不足，无法判定反转豁免"


def test_choch_trend_not_flipped():
    """平盘 → cur == ema10 → 未转多（slope=0）。"""
    r = make_exemptions({"60min": make_df([4900.0] * 10)}).trend_reversal_exempt("LONG")
    assert r.allowed is False
    assert r.reason == (
        "趋势未转多：close=4900 vs EMA10=4900/EMA20=4900，EMA10 slope=0.0"
    )


def test_choch_not_standing_above():
    """[5000]×5 + [4800]×4 + [5200] → ema10=4940，cur=5200 已转多，
    但近 5 根（索引 5-9 = [4800]×4 + [5200]）仅 1 根 > ema10 → 未站稳。"""
    closes = [5000.0] * 5 + [4800.0] * 4 + [5200.0]
    r = make_exemptions({"60min": _choch_df(closes, 5190.0, 5210.0, 5180.0)}).trend_reversal_exempt("LONG")
    assert r.allowed is False
    assert r.reason == "最近 5 根 60min bar 仅 1/5 根 close > EMA10，未站稳"


def test_choch_yin_bar_rejected():
    """趋势转多 + 5/5 站稳，但末根阴线 → 未形成阳线反转。"""
    closes = [4900.0] * 5 + [4990.0, 4995.0, 5000.0, 5005.0, 5010.0]
    r = make_exemptions({"60min": _choch_df(closes, 5020.0, 5022.0, 5008.0)}).trend_reversal_exempt("LONG")
    assert r.allowed is False
    assert r.reason == "当前 60min 为阴线/十字星，未形成阳线反转"


def test_choch_body_too_small():
    """末根实体 2/14 ≈ 14% < 60% → 反转强度不足。"""
    closes = [4900.0] * 5 + [4990.0, 4995.0, 5000.0, 5005.0, 5010.0]
    r = make_exemptions({"60min": _choch_df(closes, 5008.0, 5012.0, 4998.0)}).trend_reversal_exempt("LONG")
    assert r.allowed is False
    assert r.reason == "当前 60min 实体仅 14%，反转强度不足"


def test_choch_long_full_pass():
    """完整通过: ema10=4950、cur=5010 已转多、5/5 上穿、阳线实体 10/14≈71%（手算）。"""
    closes = [4900.0] * 5 + [4990.0, 4995.0, 5000.0, 5005.0, 5010.0]
    r = make_exemptions({"60min": _choch_df(closes, 5000.0, 5012.0, 4998.0)}).trend_reversal_exempt("LONG")
    assert r.allowed is True
    assert r.reason == (
        "60min CHoCH 反转豁免触发：close=5010 > EMA10=4950 > EMA20=4950，"
        "EMA10 slope=43.3（转正），最近 5 根 5/5 上穿，阳线实体 71%"
    )


def test_choch_short_mirror_full_pass():
    """SHORT 镜像: ema10=5050、cur=4990 已转空、5/5 下穿、阴线实体 71%。"""
    closes = [5100.0] * 5 + [5010.0, 5005.0, 5000.0, 4995.0, 4990.0]
    r = make_exemptions({"60min": _choch_df(closes, 5000.0, 5002.0, 4988.0)}).trend_reversal_exempt("SHORT")
    assert r.allowed is True
    assert r.reason == (
        "60min CHoCH 反转豁免触发：close=4990 < EMA10=5050 < EMA20=5050，"
        "EMA10 slope=-43.3（转负），最近 5 根 5/5 下穿，阴线实体 71%"
    )


def test_choch_unknown_direction():
    closes = [4900.0] * 5 + [4990.0, 4995.0, 5000.0, 5005.0, 5010.0]
    r = make_exemptions({"60min": _choch_df(closes, 5000.0, 5012.0, 4998.0)}).trend_reversal_exempt("HOLD")
    assert r.allowed is False
    assert r.reason == "未知方向"


# ---------- htf_partial_allowance ----------

def test_htf_partial_long_reversed():
    """15 根: [5000]×14 + [5020] → ema10=5002，cur=5020 > ema10 且近 2 根有上穿 → 豁免。"""
    r = make_exemptions({"60min": make_df([5000.0] * 14 + [5020.0])}).htf_partial_allowance("LONG")
    assert r.allowed is True
    assert r.reason == "60min close=5020.0 > EMA10=5002.0，已反转可做多"


def test_htf_partial_long_not_reversed():
    r = make_exemptions({"60min": make_df([5000.0] * 15)}).htf_partial_allowance("LONG")
    assert r.allowed is False
    assert r.reason == "60min close=5000.0 <= EMA10=5000.0，未反转"


def test_htf_partial_short_reversed():
    r = make_exemptions({"60min": make_df([5000.0] * 14 + [4980.0])}).htf_partial_allowance("SHORT")
    assert r.allowed is True
    assert r.reason == "60min close=4980.0 < EMA10=4998.0，已反转可做空"


def test_htf_partial_insufficient_data():
    r = make_exemptions({"60min": make_df([5000.0] * 14)}).htf_partial_allowance("LONG")
    assert r.allowed is False
    assert r.reason == "60min 数据不足"


# ---------- volume_vcp_check ----------

def test_vcp_full_pass():
    """3 根量价齐升 → 豁免（文案含递增序列；tail(3) = 最后 3 根）。"""
    df = make_df([5000.0, 5001.0, 5002.0, 5003.0],
                 volume=[50.0, 100.0, 200.0, 300.0])
    r = make_exemptions({"5min": df}).volume_vcp_check()
    assert r.allowed is True
    assert r.reason == (
        "VCP 量价齐升豁免：3 根 bar 量连续递增 (100→200→300)，"
        "close 持续走高 (5001.0→5002.0→5003.0)"
    )


def test_vcp_volume_not_increasing():
    df = make_df([5000.0, 5001.0, 5002.0, 5003.0],
                 volume=[300.0, 200.0, 100.0, 100.0])
    r = make_exemptions({"5min": df}).volume_vcp_check()
    assert r.allowed is False
    assert r.reason == "量未连续递增 (vol=200/100/100)"


def test_vcp_close_not_rising():
    """tail(3) = [5001, 5002, 5001] → close 未持续走高。"""
    df = make_df([5000.0, 5001.0, 5002.0, 5001.0],
                 volume=[50.0, 100.0, 200.0, 300.0])
    r = make_exemptions({"5min": df}).volume_vcp_check()
    assert r.allowed is False
    assert r.reason == "close 未持续走高 (c=5001.0/5002.0/5001.0)"


def test_vcp_insufficient_data():
    r = make_exemptions({"5min": make_df([5000.0] * 3)}).volume_vcp_check()
    assert r.allowed is False
    assert r.reason == "5min 数据不足"


# ---------- vwap_alignment ----------

def _vwap_df(last_close, last_high, last_low, base_close=5000.0, base_high=5010.0,
             base_low=4990.0, n=20):
    """前 n-1 根常数（tp=5000），末根自定义 → VWAP/slope 手算可控。"""
    closes = [base_close] * (n - 1) + [last_close]
    highs = [base_high] * (n - 1) + [last_high]
    lows = [base_low] * (n - 1) + [last_low]
    return make_df(closes, high=highs, low=lows, volume=[1.0] * n)


def test_vwap_long_ok():
    """末根 tp=(5020+5000+5010)/3=5010 → vwap=(19×5000+5010)/20=5000.5，
    slope=(4×5000+5000.5)/5-5000=0.1 → LONG 放行（手算对拍）。"""
    r = make_exemptions({"5min": _vwap_df(5010.0, 5020.0, 5000.0)}).vwap_alignment("LONG")
    assert r.allowed is True
    assert r.reason == "VWAP alignment OK: close=5010.0 > VWAP=5000.5, slope=0.1"


def test_vwap_long_below_blocked():
    """末根 tp=(5000+4980+4990)/3=4990 → vwap=4999.5，close 4990 < vwap → 拦截。"""
    r = make_exemptions({"5min": _vwap_df(4990.0, 5000.0, 4980.0)}).vwap_alignment("LONG")
    assert r.allowed is False
    assert r.reason == (
        "VWAP alignment 失败：当前 close=4990.0 < VWAP=4999.5，"
        "价格位于 VWAP 之下，禁止开多（行业共识）"
    )


def test_vwap_long_downslope_blocked():
    """前 10 根 tp=5010、后 9 根 tp=4990、末根 tp=5010（close=5010）：
    vwap=5001.0、slope=5001.38-5005.57=-4.19 → :.1f=-4.2 < -1 → LONG 拦截
    （VWAP 向下倾斜；cur_close 5010 > vwap 5001 先过第一关）。"""
    closes = [5010.0] * 10 + [4990.0] * 9 + [5010.0]
    highs = [5020.0] * 10 + [5000.0] * 9 + [5020.0]
    lows = [5000.0] * 10 + [4980.0] * 9 + [5000.0]
    df = make_df(closes, high=highs, low=lows, volume=[1.0] * 20)
    r = make_exemptions({"5min": df}).vwap_alignment("LONG")
    assert r.allowed is False
    assert r.reason == "VWAP 向下倾斜 (-4.2)，不允许做多"


def test_vwap_short_ok():
    """末根 tp=(5000+4980+4990)/3=4990 → vwap=4999.5，close 4990 < vwap 且
    slope=-0.1 → SHORT 放行。"""
    r = make_exemptions({"5min": _vwap_df(4990.0, 5000.0, 4980.0)}).vwap_alignment("SHORT")
    assert r.allowed is True
    assert r.reason == "VWAP alignment OK: close=4990.0 < VWAP=4999.5, slope=-0.1"


def test_vwap_short_above_blocked():
    r = make_exemptions({"5min": _vwap_df(5010.0, 5020.0, 5000.0)}).vwap_alignment("SHORT")
    assert r.allowed is False
    assert r.reason == (
        "VWAP alignment 失败：当前 close=5010.0 > VWAP=5000.5，价格位于 VWAP 之上，禁止开空"
    )


def test_vwap_short_upslope_blocked():
    """前 10 根 tp=4990、后 9 根 tp=5010、末根 tp=4990（close=4990）：
    vwap=4999、slope≈+4.2 > 1 → SHORT 拦截（VWAP 向上倾斜）。"""
    closes = [4990.0] * 10 + [5010.0] * 9 + [4990.0]
    highs = [5000.0] * 10 + [5020.0] * 9 + [5000.0]
    lows = [4980.0] * 10 + [5000.0] * 9 + [4980.0]
    df = make_df(closes, high=highs, low=lows, volume=[1.0] * 20)
    r = make_exemptions({"5min": df}).vwap_alignment("SHORT")
    assert r.allowed is False
    assert r.reason == "VWAP 向上倾斜 (4.2)，不允许做空"


def test_vwap_insufficient_data_allows():
    """< 20 根 → 放行跳过（真源 L4896–4897）。"""
    r = make_exemptions({"5min": make_df([5000.0] * 19)}).vwap_alignment("LONG")
    assert r.allowed is True
    assert r.reason == "5min 数据不足，跳过 VWAP 检查"


def test_vwap_tail48_path_no_datetime_needed():
    """真源 quirk 锁定: current_date_str 从未赋值 → hasattr 恒 False → 恒走
    tail(48) 分支 → 无 datetime 列也不报错（真源 L4901）。"""
    df = _vwap_df(5010.0, 5020.0, 5000.0)  # 无 datetime 列
    assert "datetime" not in df.columns
    r = make_exemptions({"5min": df}).vwap_alignment("LONG")
    assert r.allowed is True
