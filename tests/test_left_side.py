"""left_side 单测（阶段 3 第二批）— 行为对拍真源 _compute_left_side_signals L1608–2049。

覆盖:
- 大盘定调: 牛/熊/震荡三态（MA60/200 + 20 日趋势手算对拍）/ 数据不足跳过 / 异常仅记日志
- 5min 数据不足提前返回（真源 L1650–1653）
- L12a 双周期超卖共振: RSI(14) 手算（14 根连跌 → RSI=0）+ 量比 2.0 + 15m RSI=0
- L3 底背离缺失路径（RSI 未底背离文案）/ D17 / D0 缺失文案（'+' 连接）
- 缺口回补: 高开/低开/无缺口（阈 1%）
- 告警: 节流 300s / SL/TP 载荷手算（sl_init=cur-30、sl_pattern=今低±5、tp=阻力位）
- stale_5min 告警（M2 修复路径）/ 结构化 LeftSideSignal 标记
"""
from datetime import datetime

import pandas as pd
import pytest

from quantai.strategies.left_side import LeftSideStrategy


# ---------- 测试替身 ----------

class FakeIndexFetcher:
    def __init__(self, klines=None, raise_all=False):
        self.klines = klines or {}
        self.raise_all = raise_all

    def get_kline_data(self, index_name, frequency):
        if self.raise_all:
            raise RuntimeError("fetcher down")
        return self.klines.get(frequency)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def make_df(close, high=None, low=None, volume=None, datetime_col=None):
    n = len(close)
    data = {
        "close": list(close),
        "high": list(high) if high is not None else [c + 5 for c in close],
        "low": list(low) if low is not None else [c - 5 for c in close],
        "volume": list(volume) if volume is not None else [100.0] * n,
    }
    if datetime_col is not None:
        data["datetime"] = list(datetime_col)
    return pd.DataFrame(data)


def make_5min_l12a():
    """26 根 5min: 前 12 根平盘 5010，后 14 根每根 -2 → RSI(14)=0；末根放量 200。
    量比 = 200/100 = 2.0 ≥ 1.5；high=close+5 → 末根非新高（D17/D0 不触发）。"""
    closes = [5010.0] * 12 + [5010.0 - 2 * i for i in range(1, 15)]
    volumes = [100.0] * 25 + [200.0]
    dts = pd.date_range("2026-08-29 09:30", periods=26, freq="5min")
    return make_df(closes, volume=volumes, datetime_col=dts)


def make_15min_oversold():
    """15 根 15min: 首根 5010，后 14 根每根 -1 → RSI(14)=0 < 45。"""
    closes = [5010.0] + [5010.0 - i for i in range(1, 15)]
    return make_df(closes)


def make_strategy(klines=None, notifier=None, now=None, index_price=4984.0,
                  yesterday_close=5000.0, dynamic_levels=None, warn_fn=None,
                  raise_all=False):
    notifier = notifier or FakeNotifier()
    holder = {"now": now or datetime(2026, 8, 29, 10, 0, 0)}
    strategy = LeftSideStrategy(
        FakeIndexFetcher(klines=klines, raise_all=raise_all),
        index_price_fn=lambda: index_price,
        yesterday_close_fn=lambda: yesterday_close,
        dynamic_levels_fn=dynamic_levels or (lambda df, p, d: ([5050.0], [4950.0])),
        notifier=notifier,
        warn_fn=warn_fn,
        now_fn=lambda: holder["now"],
    )
    return strategy, notifier, holder


# ---------- 大盘定调 ----------

def _daily_df(closes):
    return make_df(closes)


def test_regime_bull():
    """250 根日线: 前 230 平盘 5000 + 后 20 根每根 +10 → trend_20d 以 closes[-20]=5010
    为基准 = (5200-5010)/5010 = +3.8% > 3 且 latest 5200 > ma200 5010.5 → 牛市（手算对拍）。"""
    closes = [5000.0] * 230 + [5000.0 + 10 * i for i in range(1, 21)]
    strategy, _, _ = make_strategy(klines={"日线": _daily_df(closes)})
    out = strategy.compute_left_side_signals()
    assert "- 定调: 🐂 牛市（上行趋势）" in out
    assert "- 近20日涨跌: +3.8%" in out
    assert "- 中证1000 最新收盘: 5200.00" in out


def test_regime_bear():
    """后 20 根每根 -10 → trend_20d = (4800-4990)/4990 = -3.8% < -3 且
    latest 4800 < ma200 4989.5 → 熊市。"""
    closes = [5000.0] * 230 + [5000.0 - 10 * i for i in range(1, 21)]
    strategy, _, _ = make_strategy(klines={"日线": _daily_df(closes)})
    out = strategy.compute_left_side_signals()
    assert "- 定调: 🐻 熊市（下行趋势）" in out
    assert "- 近20日涨跌: -3.8%" in out


def test_regime_neutral():
    """平盘 → trend_20d=0 → 震荡市。"""
    closes = [5000.0] * 250
    strategy, _, _ = make_strategy(klines={"日线": _daily_df(closes)})
    out = strategy.compute_left_side_signals()
    assert "- 定调: 📊 震荡市" in out


def test_regime_insufficient_daily_skips_section():
    """日线 < 250 根 → 无大盘定调段（真源 L1618）。"""
    strategy, _, _ = make_strategy(klines={"日线": _daily_df([5000.0] * 249)})
    out = strategy.compute_left_side_signals()
    assert "大盘定调" not in out
    assert "⚠️ 5min 数据不足或获取失败，跳过信号计算" in out


def test_regime_fetcher_exception_only_logs():
    """fetcher 全挂 → 大盘定调内部异常仅记日志（真源 L1644–1645），
    外层输出为"计算失败，跳过"（5min 拉取同样失败，真源 L2045–2047）。"""
    strategy, _, _ = make_strategy(raise_all=True)
    out = strategy.compute_left_side_signals()
    assert out == "## 🔄 左侧机会信号\n计算失败，跳过\n"


# ---------- 5min 数据不足 / L12a ----------

def test_insufficient_5min_early_return():
    """5min None → 提前返回文案（真源 L1650–1653），无告警。"""
    notifier = FakeNotifier()
    strategy, notifier, _ = make_strategy(klines={}, notifier=notifier)
    out = strategy.compute_left_side_signals()
    assert "## 🔄 左侧机会信号\n" in out
    assert "⚠️ 5min 数据不足或获取失败，跳过信号计算" in out
    assert "L12a" not in out
    assert notifier.sent == []


def test_l12a_complete_renders_and_alerts():
    """L12a 完整: RSI(14)=0<40 + 15m RSI=0<45 + 量比 2.0≥1.5 → 完整文案 + 钉钉告警。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, notifier, _ = make_strategy(klines=klines)
    out = strategy.compute_left_side_signals()
    # L12a 完整文案（真源 L1834 逐字）
    assert "  → 🔎 L12a 左侧信号完整（观察级）！不可直接入场，等待右侧确认（站上 VWAP + 阳线收盘确认）后才可考虑 1 手试多，止损=今低-5点，止盈=布林中线/VWAP" in out
    # 三条件勾选行（真源 L1830–1832）
    assert "- ✅ RSI(14,5min)=0 （阈值 < 40）" in out
    assert "- ✅ RSI(14,15min)=0 （阈值 < 45）" in out
    assert "- ✅ 温和放量 （当前量比=2.0x，前根=1.0x，需 ≥ 1.5x）" in out
    # 告警已发送
    assert len(notifier.sent) == 1
    assert "🔔 左侧信号触发！" in notifier.sent[0]


def test_l12a_incomplete_missing_text():
    """L12a 不完整（量比不足）→ 缺失文案 ' / ' 连接（真源 L1840）。"""
    closes = [5010.0] * 12 + [5010.0 - 2 * i for i in range(1, 15)]
    volumes = [100.0] * 26  # 量比 1.0 < 1.5
    dts = pd.date_range("2026-08-29 09:30", periods=26, freq="5min")
    klines = {"5min": make_df(closes, volume=volumes, datetime_col=dts),
              "15min": make_15min_oversold(), "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines)
    out = strategy.compute_left_side_signals()
    assert "  → ⚠️ 触发但缺: 量比不够(≥1.5)" in out
    assert "- ❌ 温和放量 （当前量比=1.0x，前根=1.0x，需 ≥ 1.5x）" in out


def test_l3_missing_rsi_divergence_text():
    """L3: 价格新低+量比够 但 RSI 未底背离（rsi_14=0 不 > min_rsi_20+2）→ 缺失文案。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines)
    out = strategy.compute_left_side_signals()
    assert "### LONG 左侧：L3 5m 底背离（辅助精准信号）" in out
    assert "- ✅ 价格新低 (5min low ≤ 20 根内最低)" in out
    assert "- ❌ RSI 底背离 (5min RSI=0 > 20根内最低 0+2)" in out
    assert "  → ⚠️ 触发但缺: RSI 未底背离" in out


def test_d17_d0_missing_text_plus_joined():
    """D17/D0 不完整 → 缺失文案 '+' 连接（真源 L1874/L1890）。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines)
    out = strategy.compute_left_side_signals()
    assert "  → ⚠️ 触发但缺: 价格未新高+RSI 未顶背离" in out  # D17（日线超买 ✅ 不在缺失中）
    assert "  → ⚠️ 触发但缺: 价格未新高" in out  # D0（回落+放量 ✅）


# ---------- 缺口回补 ----------

def test_gap_high_open():
    """index 5100 vs 昨收 5000 → +2% > 1% → 高开缺口 SHORT 机会（真源 L1900）。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines, index_price=5100.0)
    out = strategy.compute_left_side_signals()
    assert "- 📉 高开缺口 +2.00%（5100.00 vs 昨收5000.00）→ SHORT 左侧机会" in out


def test_gap_low_open():
    """index 4900 vs 昨收 5000 → -2% → 低开缺口 LONG 机会（真源 L1902）。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines, index_price=4900.0)
    out = strategy.compute_left_side_signals()
    assert "- 📈 低开缺口 -2.00%（4900.00 vs 昨收5000.00）→ LONG 左侧机会" in out


def test_gap_below_threshold_and_no_prev_close():
    """|缺口| ≤ 1% → 无明显缺口；昨收不可用 → ⚠️ 文案（真源 L1904/L1906）。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines, index_price=4984.0)
    out = strategy.compute_left_side_signals()
    assert "- ❌ 无明显缺口（-0.32%，阈 1%）" in out

    strategy2, _, _ = make_strategy(klines=klines, yesterday_close=None)
    out2 = strategy2.compute_left_side_signals()
    assert "- ⚠️ 昨收数据不可用" in out2


# ---------- 告警载荷 / 节流 ----------

def test_alert_payload_hand_calculated():
    """告警消息手算对拍: cur=4984, LONG → sl_init=4954(=4984-30)、
    sl_pattern=4959(=len<48 → cur-20-5)、sl_tech=4950(近端支撑)、tp=5050(阻力)。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, notifier, _ = make_strategy(klines=klines)
    strategy.compute_left_side_signals()

    msg = notifier.sent[0]
    assert "📈 LONG 多 - LONG 左侧 L12a 观察级（超卖共振，等右侧确认再入）" in msg
    assert "📍 当前价: 4984.00" in msg
    assert "  • 初始止损: 4954.00 (入场价 4984.00 - 30 点)" in msg
    assert "  • 形态止损: 4959.00 (今低/今高 ± 5 点)" in msg
    assert "  • 关键位止损: 4950.00 (近端支撑/阻力 - 5 点)" in msg
    assert "  • 第一目标 TP: 5050.00 (66 点) - 优先止盈 50%" in msg
    assert "  • 突破后可看: 5050" in msg
    assert "  • (VWP/布林中轨参考: 4999.50)" in msg  # closes[-20:] 均值 = 99990/20
    assert "⏳ 时间止损: 1 小时 (回测最优)" in msg
    assert "⏰ 触发时间: 10:00:00" in msg


def test_alert_throttle_300s():
    """节流: 同一信号 ID 300s 内只发一次（真源 L1952–1956）。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, notifier, holder = make_strategy(klines=klines)
    strategy.compute_left_side_signals()
    strategy.compute_left_side_signals()
    assert len(notifier.sent) == 1  # 固定 now → 节流命中

    from datetime import timedelta
    holder["now"] = holder["now"] + timedelta(seconds=301)
    strategy.compute_left_side_signals()
    assert len(notifier.sent) == 2  # 超过节流窗口 → 再发


def test_alert_short_direction_payload():
    """SHORT 告警镜像: sl_init=cur+15、tp=支撑位、关键位止损=近端阻力 5050、
    时间止损 4 小时。构造 D0 完整（末根新高回落 + 量比≥2）；
    日线用交替涨跌（RSI=50 ≤ 65）使 D17 不触发，保证唯一告警为 D0。"""
    # 前 25 根 close 5000（high 5005/low 4995），末根 close 4990 回落但 high 5010 新高
    closes = [5000.0] * 25 + [4990.0]
    highs = [5005.0] * 25 + [5010.0]
    lows = [4995.0] * 25 + [4985.0]
    volumes = [100.0] * 25 + [300.0]  # 量比 3.0 ≥ 2
    dts = pd.date_range("2026-08-29 09:30", periods=26, freq="5min")
    daily_closes = [5000.0 + (i % 2) for i in range(20)]  # 交替 ±1 → 日RSI=50
    klines = {"5min": make_df(closes, high=highs, low=lows, volume=volumes,
                              datetime_col=dts),
              "15min": make_df([5000.0] * 15), "日线": _daily_df(daily_closes)}
    strategy, notifier, _ = make_strategy(klines=klines, index_price=4990.0)
    out = strategy.compute_left_side_signals()
    assert "  → 🔎 D0 左侧信号完整（观察级）！不可直接入场，等待右侧确认（跌破 VWAP + 阴线收盘确认）后才可考虑 1 手试空，止损=今高+5点，止盈=VWAP/布林中线" in out
    assert len(notifier.sent) == 1  # 仅 D0（D17 因日线未超买不完整）
    msg = notifier.sent[0]
    assert "📉 SHORT 空 - SHORT 左侧 D0 观察级（新高回落+放量，等右侧确认再入）" in msg
    assert "  • 初始止损: 5005.00 (入场价 4990.00 + 15 点)" in msg  # 4990+15
    assert "  • 形态止损: 5015.00 (今低/今高 ± 5 点)" in msg  # len<48 → cur+20+5
    assert "  • 关键位止损: 5050.00 (近端支撑/阻力 - 5 点)" in msg  # SHORT 取近端阻力
    assert "  • 第一目标 TP: 4950.00 (40 点) - 优先止盈 50%" in msg
    assert "⏳ 时间止损: 4 小时 (回测最优)" in msg


# ---------- stale 告警 / 结构化信号 ----------

def test_stale_5min_warn_once():
    """datetime 列无今日数据 → warn_fn("stale_5min", ...)（M2 修复路径，真源 L1670–1672）。"""
    warns = []
    closes = [5010.0] * 12 + [5010.0 - 2 * i for i in range(1, 15)]
    dts = pd.date_range("2026-08-20 09:30", periods=26, freq="5min")  # 非今日
    klines = {"5min": make_df(closes, datetime_col=dts),
              "15min": make_15min_oversold(), "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines, warn_fn=lambda k, m: warns.append((k, m)))
    strategy.compute_left_side_signals()
    assert len(warns) == 1
    assert warns[0][0] == "stale_5min"
    assert "5min 数据无今日数据" in warns[0][1]


def test_structured_signals_flags():
    """compute_signals 结构化输出: L12a triggered / 其余 False / 方向标记。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines)
    sig = strategy.compute_signals()
    by_name = {s.name: s for s in sig["signals"]}
    assert by_name["L12a"].triggered is True
    assert by_name["L12a"].direction == "LONG"
    assert by_name["L3"].triggered is False
    assert by_name["D17"].triggered is False
    assert by_name["D17"].direction == "SHORT"
    assert by_name["D0"].triggered is False
    assert sig["rsi_14"] == pytest.approx(0.0)
    assert sig["vol_ratio"] == pytest.approx(2.0)
    assert sig["gap_pct"] == pytest.approx(-0.32)


def test_alert_fills_sl_tp_on_signal_object():
    """告警触发后 LeftSideSignal 回填 sl/tp 建议（结构化载荷细化）。"""
    klines = {"5min": make_5min_l12a(), "15min": make_15min_oversold(),
              "日线": _daily_df([5000.0] * 20)}
    strategy, _, _ = make_strategy(klines=klines)
    sig = strategy.compute_signals()
    strategy.dispatch_alerts(sig)
    by_name = {s.name: s for s in sig["signals"]}
    assert by_name["L12a"].sl_suggestion == pytest.approx(4954.0)
    assert by_name["L12a"].tp_suggestion == pytest.approx(5050.0)
    assert by_name["L12a"].created_at == datetime(2026, 8, 29, 10, 0, 0)
