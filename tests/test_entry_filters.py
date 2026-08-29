"""entry_filters 单测（阶段 3 第二批）— 行为对拍真源 L4422–4693。

覆盖:
- check_trend_alignment: 数据不足放行 / 3/3 空头拦 LONG（接飞刀）/ 3/3 多头拦 SHORT /
  2/3 刚转空拦 SHORT（暂禁）/ 混杂形态放行 / EMA 手算对拍
- check_session_extremes: ±10 禁区双向 / 创新低 50 点防护（7/3 案例）/ 安全放行
- confirm_breakout_bar: 影线穿刺双向拦截（穿透点数手算）/ 收盘确认放行
- check_htf_bias: 日/周线评分不拦截 / 数据不足
- check_entry_volume: 0.5x 拦截 / 1.3x 放行 / 突破 1.3x 阈值分层
- check_entry_confirmation: 0.2×ATR 手算 / ATR 不可用放行
"""
import pandas as pd
import pytest

from quantai.strategies.entry_filters import EntryFilters


class FakeIndexFetcher:
    def __init__(self, klines=None):
        self.klines = klines or {}

    def get_kline_data(self, index_name, frequency):
        return self.klines.get(frequency)


def make_df(close, high=None, low=None, volume=None):
    n = len(close)
    return pd.DataFrame({
        "close": list(close),
        "high": list(high) if high is not None else [c + 5 for c in close],
        "low": list(low) if low is not None else [c - 5 for c in close],
        "volume": list(volume) if volume is not None else [100.0] * n,
    })


def make_filters(klines=None, atr5=10.0):
    return EntryFilters(FakeIndexFetcher(klines=klines), atr5_fn=lambda: atr5)


# ---------- check_trend_alignment ----------

def test_trend_insufficient_data_allows():
    """60min < 15 根 → 放行跳过（真源 L4429–4430）。"""
    f = make_filters({"60min": make_df([5000.0] * 14)})
    r = f.check_trend_alignment("LONG")
    assert r.allowed is True
    assert r.reason == "60min 数据不足，跳过趋势过滤"


def test_trend_confirmed_bearish_blocks_long():
    """20 根: [5010]×9 + [4990]×8 + [4985, 4984, 4980]
    → ema10=4987.9 / ema20=4997.95 / cur=4980，近 3 根全 < ema10 → 3/3 空头
    → LONG 被拦（防止接飞刀，手算对拍文案取整 4988/4998）。"""
    closes = [5010.0] * 9 + [4990.0] * 8 + [4985.0, 4984.0, 4980.0]
    f = make_filters({"60min": make_df(closes)})
    r = f.check_trend_alignment("LONG")
    assert r.allowed is False
    assert r.reason == (
        "60min 空头排列且连续 3/3 bar 确认 (close=4980 < EMA10=4988 < EMA20=4998)，"
        "禁止逆势开多（防止接飞刀）"
    )


def test_trend_confirmed_bullish_blocks_short():
    """镜像: 3/3 多头拦 SHORT（防止摸顶）。"""
    closes = [4990.0] * 9 + [5010.0] * 8 + [5015.0, 5016.0, 5020.0]
    f = make_filters({"60min": make_df(closes)})
    r = f.check_trend_alignment("SHORT")
    assert r.allowed is False
    assert "60min 多头排列且连续 3/3 bar 确认" in r.reason
    assert "禁止逆势开空（防止摸顶）" in r.reason


def test_trend_2of3_just_flipped_blocks_short():
    """[5010]×9 + [4990]×8 + [4985, 4995, 4980] → ema10=4989，近 3 根 2 根 < ema10
    → 空头排列但未 3/3 → SHORT 暂禁（8/6 放宽：2/3 放行逆势？不——2/3 时拦同向
    未确认，真源 L4485–4489）。"""
    closes = [5010.0] * 9 + [4990.0] * 8 + [4985.0, 4995.0, 4980.0]
    f = make_filters({"60min": make_df(closes)})
    r = f.check_trend_alignment("SHORT")
    assert r.allowed is False
    assert r.reason == (
        "60min 趋势刚转空但仅 2/3 bar 确认，不稳定，暂禁开空（等连续 2 根 bar 确认趋势）"
    )


def test_trend_mixed_allows_long():
    """同上数据 + LONG：非 confirmed_bullish 且非 sixty_bullish → 放行。
    reason 计数取 bullish_count（真源 L4490 三元表达式）= 1（4995 > ema10 4989）。"""
    closes = [5010.0] * 9 + [4990.0] * 8 + [4985.0, 4995.0, 4980.0]
    f = make_filters({"60min": make_df(closes)})
    r = f.check_trend_alignment("LONG")
    assert r.allowed is True
    assert r.reason == "60min 趋势与 LONG 方向一致 (1/3 bar 确认)"


def test_trend_aligned_allows():
    """多头排列 3/3 + LONG → 放行 (3/3)。"""
    closes = [4990.0] * 9 + [5010.0] * 8 + [5015.0, 5016.0, 5020.0]
    f = make_filters({"60min": make_df(closes)})
    r = f.check_trend_alignment("LONG")
    assert r.allowed is True
    assert r.reason == "60min 趋势与 LONG 方向一致 (3/3 bar 确认)"


# ---------- check_session_extremes ----------

def _df48():
    return make_df([5000.0] * 48, high=[5010.0] * 48, low=[4990.0] * 48)


def test_session_long_near_today_high_blocked():
    """LONG 5005 → 距今高 5010 差 5.0 < 10 → 拦截（真源 L4512–4516）。"""
    f = make_filters({"5min": _df48()})
    r = f.check_session_extremes(5005.0, "LONG")
    assert r.allowed is False
    assert r.reason == (
        "入场价 5005.0 接近今高 5010.0（差 5.0 点 < 10.0），日高区域假突破概率最高，禁止入场"
    )


def test_session_short_near_today_low_blocked():
    """SHORT 4985 → 真源公式 entry_price - today_low = 4985-4990 = -5.0（入场价
    低于今低时差值为负——真源 L4519 原样行为，保真锁定）。"""
    f = make_filters({"5min": _df48()})
    r = f.check_session_extremes(4985.0, "SHORT")
    assert r.allowed is False
    assert r.reason == (
        "入场价 4985.0 接近今低 4990.0（差 -5.0 点 < 10.0），日低区域假突破概率最高，禁止入场"
    )


def test_session_new_low_dip_blocked():
    """LONG 4930 < 今低 4990 - 50 → 创新低抄底拦截（7/3 案例防护，真源 L4529–4534）。"""
    f = make_filters({"5min": _df48()})
    r = f.check_session_extremes(4930.0, "LONG")
    assert r.allowed is False
    assert r.reason == (
        "入场价 4930.0 创今日新低（比今低 4990.0 低 60.0 点 > 50.0），"
        "防接飞刀，禁止在创日内新低抄底"
    )


def test_session_safe_allows():
    f = make_filters({"5min": _df48()})
    r = f.check_session_extremes(4950.0, "LONG")
    assert r.allowed is True
    assert r.reason == "入场价 4950.0 距今高安全"


def test_session_insufficient_data_allows():
    f = make_filters({"5min": make_df([5000.0] * 47)})
    r = f.check_session_extremes(5005.0, "LONG")
    assert r.allowed is True
    assert r.reason == "5min 数据不足，跳过 High/Low 过滤"


# ---------- confirm_breakout_bar ----------

def _df2_breakout():
    """2 根常数 K 线（过滤器要求 len >= 2，真源 L4549）。"""
    return make_df([5000.0, 5000.0], high=[5010.0, 5010.0], low=[4990.0, 4990.0])


def test_breakout_above_wick_rejected():
    """PRICE_ABOVE: close 5000 < trigger 5005 → 影线穿刺拦截，穿透 = 5010-5005 = 5.0。"""
    f = make_filters({"5min": _df2_breakout()})
    r = f.confirm_breakout_bar("PRICE_ABOVE", 5005.0, "LONG")
    assert r.allowed is False
    assert r.reason == (
        "影线穿刺未确认：high=5010.0 触及触发价 5005.0 (穿透 5.0 点)，"
        "但 close=5000.0 回落到触发价下方，典型假突破特征，拒绝开仓"
    )


def test_breakout_above_confirmed():
    f = make_filters({"5min": _df2_breakout()})
    r = f.confirm_breakout_bar("PRICE_ABOVE", 4995.0, "LONG")
    assert r.allowed is True
    assert r.reason == "突破确认：收盘价同向穿透触发位"


def test_breakout_below_wick_rejected():
    """PRICE_BELOW: close 5000 > trigger 4995 → 穿透 = 4995-4990 = 5.0。"""
    f = make_filters({"5min": _df2_breakout()})
    r = f.confirm_breakout_bar("PRICE_BELOW", 4995.0, "SHORT")
    assert r.allowed is False
    assert "low=4990.0 触及触发价 4995.0 (穿透 5.0 点)" in r.reason
    assert "close=5000.0 回升到触发价上方" in r.reason


def test_breakout_below_confirmed():
    f = make_filters({"5min": _df2_breakout()})
    r = f.confirm_breakout_bar("PRICE_BELOW", 5005.0, "SHORT")
    assert r.allowed is True
    assert r.reason == "突破确认：收盘价同向穿透触发位"


def test_breakout_insufficient_data_allows():
    f = make_filters({"5min": make_df([5000.0])})
    r = f.confirm_breakout_bar("PRICE_ABOVE", 4995.0, "LONG")
    assert r.allowed is True
    assert r.reason == "5min 数据不足，跳过确认"


# ---------- check_htf_bias ----------

def test_htf_bias_scores_not_blocks():
    """日线多头 + 周线空头 → 仅评分放行（8/6 F 方案，真源 L4614–4615）。"""
    daily = make_df([5000.0] * 19 + [5100.0])   # ema20=5005, cur=5100 → 多头
    weekly = make_df([5000.0] * 9 + [4900.0])   # ema10=4990, cur=4900 → 空头
    f = make_filters({"日线": daily, "周线": weekly})
    r = f.check_htf_bias("LONG")
    assert r.allowed is True
    assert r.reason == (
        "大周期(仅评分，不拦截): 日线多头 close=5100 > EMA20=5005 | "
        "周线空头 close=4900 < EMA10=4990"
    )


def test_htf_bias_no_data():
    f = make_filters({})
    r = f.check_htf_bias("LONG")
    assert r.allowed is True
    assert r.reason == "大周期数据不足，跳过 HTF 检查"


# ---------- check_entry_volume ----------

def test_volume_insufficient_blocked():
    """末根量 50 / 均量 100 = 0.50x < 1.0x → 拦截（缩量文案）。"""
    volumes = [100.0] * 24 + [50.0]
    f = make_filters({"5min": make_df([5000.0] * 25, volume=volumes)})
    r = f.check_entry_volume()
    assert r.allowed is False
    assert r.reason == (
        "入场量能不足：当前 5min 量 50 / 20 根均量 100 = 0.50x，"
        "< 1.0x 阈值（缩量，量能不足以确认方向）"
    )


def test_volume_ok_allowed():
    volumes = [100.0] * 24 + [130.0]
    f = make_filters({"5min": make_df([5000.0] * 25, volume=volumes)})
    r = f.check_entry_volume()
    assert r.allowed is True
    assert r.reason == "入场量能确认：1.30x ≥ 1.0x"


def test_volume_breakout_threshold_13x():
    """8/14 量能分层: min_ratio=1.3（突破场景），1.2x → 拦截（突破需放量文案）。"""
    volumes = [100.0] * 24 + [120.0]
    f = make_filters({"5min": make_df([5000.0] * 25, volume=volumes)})
    r = f.check_entry_volume(min_ratio=1.3)
    assert r.allowed is False
    assert "1.20x，< 1.3x 阈值（突破需放量）" in r.reason


def test_volume_insufficient_data_allows():
    f = make_filters({"5min": make_df([5000.0] * 24)})
    r = f.check_entry_volume()
    assert r.allowed is True
    assert r.reason == "5min 数据不足，跳过量能检查"


# ---------- check_entry_confirmation ----------

def _df2_confirmation(close, high=5010.0, low=5000.0):
    """2 根 K 线（过滤器要求 len >= 2，真源 L4664）。"""
    return make_df([close, close], high=[high, high], low=[low, low])


def test_confirmation_long_close_at_low_blocked():
    """atr_5=10 → threshold=2；close 5001 < low 5000+2 → 拦截（差 1.0 < 2.0）。"""
    f = make_filters({"5min": _df2_confirmation(5001.0)}, atr5=10.0)
    r = f.check_entry_confirmation("LONG")
    assert r.allowed is False
    assert r.reason == (
        "5min bar 收盘价 5001.0 处于今低 5000.0 附近，（差 1.0 < 2.0），收盘在最低点，拒开多"
    )


def test_confirmation_long_ok():
    f = make_filters({"5min": _df2_confirmation(5003.0)}, atr5=10.0)
    r = f.check_entry_confirmation("LONG")
    assert r.allowed is True
    assert r.reason == "入场 K 线收盘确认 OK (close=5003.0)"


def test_confirmation_short_close_at_high_blocked():
    """close 5009 > high 5010-2=5008 → 拦截（差 1.0 < 2.0）。"""
    f = make_filters({"5min": _df2_confirmation(5009.0, high=5010.0, low=4990.0)}, atr5=10.0)
    r = f.check_entry_confirmation("SHORT")
    assert r.allowed is False
    assert r.reason == (
        "5min bar 收盘价 5009.0 处于今高 5010.0 附近，（差 1.0 < 2.0），收盘在最高点，拒开空"
    )


def test_confirmation_atr_unavailable_allows():
    """atr_5=0 → 放行跳过（真源 L4671–4672）。"""
    f = make_filters({"5min": _df2_confirmation(5001.0)}, atr5=0.0)
    r = f.check_entry_confirmation("LONG")
    assert r.allowed is True
    assert r.reason == "ATR 不可用，跳过确认"


def test_filter_result_carries_filter_name():
    """FilterResult.filter_name 标注（结构化输出元数据）。"""
    f = make_filters({})
    assert f.check_trend_alignment("LONG").filter_name == "trend_alignment"
    assert f.check_entry_volume().filter_name == "entry_volume"
