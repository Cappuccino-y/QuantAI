"""strategies.indicators — 纯函数指标库（阶段 3 起步）。

design.md §4.2 indicators 表当前仅 1 项:
- calc_atr ← 真源 autotrade_fix.py L473 嵌套闭包（随 _calculate_fut_atr
  迁移提为模块级纯函数，design.md 既定）

rsi/ema/vwap/bollinger/vol_ratio/背离检测等纯函数随阶段 4 left_side /
entry_filters / exemptions 迁移时逐个落位（严格不越界，本阶段不预建）。
"""
import pandas as pd


def calc_atr(df, period=14):
    """ATR 纯计算，无状态（真源 L473–486 逐行保真）。

    df: 含 high/low/close 列的 K 线 DataFrame（天勤 get_kline_serial 输出）。
    数据不足（< period+1 根）或 df 为 None 时返回 0.0。
    """
    if df is None or len(df) < period + 1:
        return 0.0
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return atr
