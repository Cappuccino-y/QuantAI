"""strategies.exemptions — 反转豁免链（真源 L4697–4943，4 个方法）。

方法映射（design.md §4.2 exemptions 表）:
- Exemptions.trend_reversal_exempt  ← _trend_reversal_exempt L4707–4811
- Exemptions.htf_partial_allowance  ← _htf_partial_allowance L4813–4847
- Exemptions.volume_vcp_check       ← _volume_vcp_check L4849–4883
- Exemptions.vwap_alignment         ← _vwap_alignment L4885–4942

行为保持: CHoCH 实体 ≥60% + 5 根 3 根站稳 + EMA slope ±5.0、HTF partial 最近 2 根
上/下穿、VCP 3 根量价连增、VWAP slope ±1.0，全部判定与文案逐行对齐真源，
含 7/9 V 型反转错失背景注释。

结构差异（ARCHITECTURE.md 阶段 3 决策记录）:
- 真源返回 Tuple[bool, str] → 本版返回 models.FilterResult（同 entry_filters）
- 真源 _vwap_alignment 的 self.current_date_str 在上帝类中从未赋值（全文件仅 L4901
  一处 hasattr 读），hasattr 恒 False → 恒走 df_5.tail(48) 分支；本版保留 hasattr
  检查原样（属性缺省不存在，行为一致），单测锁定 tail(48) 路径
- 豁免失败语义保真: trend_reversal_exempt/htf_partial_allowance/volume_vcp_check
  异常 → allowed=False（"不豁免"）；vwap_alignment 异常 → allowed=True（"放行"，
  真源注明其仅作过滤器使用）
"""
import logging
from typing import Tuple

from quantai.models import FilterResult


class Exemptions:
    """反转豁免（4 个，7/9 V 型反转错失后加；命中率 > 80% 时豁免原 filter）。"""

    def __init__(self, index_fetcher, index_name: str = "中证1000"):
        self.index_fetcher = index_fetcher
        self.index_name = index_name

    def trend_reversal_exempt(self, direction: str) -> FilterResult:
        """
        60min 反转豁免 (CHoCH 1-Bar 规则):
        即使 60min 仍是 3/3 空头，但只要最新 2 根 60min bar 形成 "CHoCH 信号"
        （最近 bar 阳线实体 ≥ 60% + 最近 5 根 bar 中至少 3 根 close > EMA10，
        表示从下方上穿已站稳），就允许 LONG 入场。
        这是"反转初期"的关键信号 (fxnx.com CHoCH 1-bar rule 改良版)。
        SHORT 方向镜像。
        返回 (allowed, reason)
        （真源 _trend_reversal_exempt L4707–4811 逐行保真）
        """
        try:
            df_60 = self.index_fetcher.get_kline_data(self.index_name, "60min")
            if df_60 is None or len(df_60) < 5:
                return FilterResult(False, "60min 数据不足，无法判定反转豁免", "trend_reversal_exempt")

            n60 = len(df_60)
            closes_60 = list(df_60['close'].values)
            ema10 = sum(closes_60[-10:]) / 10 if n60 >= 10 else closes_60[-1]
            ema20 = sum(closes_60[-20:]) / 20 if n60 >= 20 else ema10

            # 当前 bar
            cur_bar = df_60.iloc[-1]
            cur_close = float(cur_bar['close'])
            cur_open = float(cur_bar['open'])
            cur_high = float(cur_bar['high'])
            cur_low = float(cur_bar['low'])
            cur_range = cur_high - cur_low

            # EMA10 slope: 最近 3 根 vs 前 3 根 (反映趋势方向)
            if n60 >= 6:
                recent_ema10 = sum(closes_60[-3:]) / 3
                earlier_ema10 = sum(closes_60[-6:-3]) / 3
                ema10_slope = recent_ema10 - earlier_ema10  # 上升=正
            else:
                ema10_slope = 0.0

            if direction == "LONG":
                # 改良 CHoCH 上行信号:
                # 1) EMA10 slope > 0 (EMA 正在从下降转上升) 或 cur close > EMA10 且 close > EMA20 (已转多)
                # 2) 最近 5 根 bar 中至少 3 根 close > EMA10 (确认从下方上穿站稳)
                # 3) 当前 bar 为阳线 + 实体 >= 60% (强反转，非十字星)

                # 条件 1: 趋势转多
                trend_flip = (
                    (cur_close > ema10 and cur_close > ema20) or  # 已转多
                    (ema10_slope > 5.0 and cur_close > ema10)        # EMA 上升 + 当前在 EMA 上方
                )
                if not trend_flip:
                    return FilterResult(False, (
                        f"趋势未转多：close={cur_close:.0f} vs EMA10={ema10:.0f}/EMA20={ema20:.0f}，"
                        f"EMA10 slope={ema10_slope:.1f}"
                    ), "trend_reversal_exempt")

                # 条件 2: 最近 5 根中至少 3 根 close > EMA10
                recent_5 = closes_60[-5:]
                above_count = sum(1 for c in recent_5 if c > ema10)
                if above_count < 3:
                    return FilterResult(False, f"最近 5 根 60min bar 仅 {above_count}/5 根 close > EMA10，未站稳", "trend_reversal_exempt")

                # 条件 3: 当前 bar 为阳线 + 实体 >= 60%
                body = abs(cur_close - cur_open)
                if cur_close <= cur_open:
                    return FilterResult(False, "当前 60min 为阴线/十字星，未形成阳线反转", "trend_reversal_exempt")
                if cur_range > 0 and body / cur_range < 0.6:
                    return FilterResult(False, f"当前 60min 实体仅 {body/cur_range*100:.0f}%，反转强度不足", "trend_reversal_exempt")

                # 通过
                return FilterResult(True, (
                    f"60min CHoCH 反转豁免触发：close={cur_close:.0f} > EMA10={ema10:.0f} > EMA20={ema20:.0f}，"
                    f"EMA10 slope={ema10_slope:.1f}（转正），最近 5 根 {above_count}/5 上穿，"
                    f"阳线实体 {body/cur_range*100:.0f}%"
                ), "trend_reversal_exempt")

            elif direction == "SHORT":
                # SHORT 镜像
                trend_flip = (
                    (cur_close < ema10 and cur_close < ema20) or
                    (ema10_slope < -5.0 and cur_close < ema10)
                )
                if not trend_flip:
                    return FilterResult(False, (
                        f"趋势未转空：close={cur_close:.0f} vs EMA10={ema10:.0f}/EMA20={ema20:.0f}，"
                        f"EMA10 slope={ema10_slope:.1f}"
                    ), "trend_reversal_exempt")

                recent_5 = closes_60[-5:]
                below_count = sum(1 for c in recent_5 if c < ema10)
                if below_count < 3:
                    return FilterResult(False, f"最近 5 根 60min bar 仅 {below_count}/5 根 close < EMA10，未站稳", "trend_reversal_exempt")

                body = abs(cur_close - cur_open)
                if cur_close >= cur_open:
                    return FilterResult(False, "当前 60min 为阳线，未形成阴线反转", "trend_reversal_exempt")
                if cur_range > 0 and body / cur_range < 0.6:
                    return FilterResult(False, f"当前 60min 实体仅 {body/cur_range*100:.0f}%，反转强度不足", "trend_reversal_exempt")

                return FilterResult(True, (
                    f"60min CHoCH 反转豁免触发：close={cur_close:.0f} < EMA10={ema10:.0f} < EMA20={ema20:.0f}，"
                    f"EMA10 slope={ema10_slope:.1f}（转负），最近 5 根 {below_count}/5 下穿，"
                    f"阴线实体 {body/cur_range*100:.0f}%"
                ), "trend_reversal_exempt")
            return FilterResult(False, "未知方向", "trend_reversal_exempt")
        except Exception as e:
            logging.warning(f"反转豁免检查失败: {e}")
            return FilterResult(False, "反转豁免异常，不豁免", "trend_reversal_exempt")

    def htf_partial_allowance(self, direction: str) -> FilterResult:
        """
        HTF 反转豁免 (8/6 简化版):
        Daily/Weekly 已降级为评分（check_htf_bias 不再拦截）。
        此豁免只检查 60min 是否已反转（close vs EMA10），
        60min 反转是入场的主要方向依据。
        SHORT 方向镜像。
        （真源 _htf_partial_allowance L4813–4847 逐行保真）
        """
        try:
            df_60 = self.index_fetcher.get_kline_data(self.index_name, "60min")
            if df_60 is None or len(df_60) < 15:
                return FilterResult(False, "60min 数据不足", "htf_partial_allowance")

            closes_60 = list(df_60['close'].values)
            ema10_60 = sum(closes_60[-10:]) / 10
            cur_close_60 = closes_60[-1]

            if direction == "LONG":
                if cur_close_60 <= ema10_60:
                    return FilterResult(False, f"60min close={cur_close_60:.1f} <= EMA10={ema10_60:.1f}，未反转", "htf_partial_allowance")
                recent_2 = closes_60[-2:]
                if not any(c > ema10_60 for c in recent_2):
                    return FilterResult(False, "60min 最近 2 根 bar 均未上穿 EMA10", "htf_partial_allowance")
                return FilterResult(True, f"60min close={cur_close_60:.1f} > EMA10={ema10_60:.1f}，已反转可做多", "htf_partial_allowance")
            else:  # SHORT
                if cur_close_60 >= ema10_60:
                    return FilterResult(False, f"60min close={cur_close_60:.1f} >= EMA10={ema10_60:.1f}，未反转", "htf_partial_allowance")
                recent_2 = closes_60[-2:]
                if not any(c < ema10_60 for c in recent_2):
                    return FilterResult(False, "60min 最近 2 根 bar 均未下穿 EMA10", "htf_partial_allowance")
                return FilterResult(True, f"60min close={cur_close_60:.1f} < EMA10={ema10_60:.1f}，已反转可做空", "htf_partial_allowance")
            return FilterResult(False, "未知方向", "htf_partial_allowance")
        except Exception as e:
            logging.warning(f"HTF partial 检查失败: {e}")
            return FilterResult(False, "HTF partial 异常，不豁免", "htf_partial_allowance")

    def volume_vcp_check(self) -> FilterResult:
        """
        量能 VCP (Volatility Contraction Pattern) 检查:
        即使当前 bar 量未达 1.5x，但只要:
        1) 最近 3 根 5min bar 量连续递增
        2) 最近 3 根 5min bar close 持续走高（LONG）/持续走低（SHORT）
        3) 当前 close 高于前 2 根 close
        就豁免 Volume 阈值。这是量价齐升的标准模式。
        （真源 _volume_vcp_check L4849–4883 逐行保真）
        """
        try:
            df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df_5 is None or len(df_5) < 4:
                return FilterResult(False, "5min 数据不足", "volume_vcp_check")

            recent_3 = df_5.tail(3)
            if len(recent_3) < 3:
                return FilterResult(False, "5min 最近 3 根 bar 不足", "volume_vcp_check")

            vols = list(recent_3['volume'].values)
            closes = list(recent_3['close'].values)

            # 1) 量连续递增
            if not (vols[0] < vols[1] < vols[2]):
                return FilterResult(False, f"量未连续递增 (vol={vols[0]:.0f}/{vols[1]:.0f}/{vols[2]:.0f})", "volume_vcp_check")
            # 2) close 持续走高
            if not (closes[0] < closes[1] < closes[2]):
                return FilterResult(False, f"close 未持续走高 (c={closes[0]:.1f}/{closes[1]:.1f}/{closes[2]:.1f})", "volume_vcp_check")
            # 3) 当前 close 高于前 2 根 close（已包含在 2 中）
            return FilterResult(True, (
                f"VCP 量价齐升豁免：3 根 bar 量连续递增 ({vols[0]:.0f}→{vols[1]:.0f}→{vols[2]:.0f})，"
                f"close 持续走高 ({closes[0]:.1f}→{closes[1]:.1f}→{closes[2]:.1f})"
            ), "volume_vcp_check")
        except Exception as e:
            logging.warning(f"VCP 检查失败: {e}")
            return FilterResult(False, "VCP 异常，不豁免", "volume_vcp_check")

    def vwap_alignment(self, direction: str) -> FilterResult:
        """
        VWAP alignment check (ORB 行业共识):
        - LONG: 要求 close > VWAP 且 VWAP slope >= 0 (VWAP 走平/向上)
        - SHORT: 要求 close < VWAP 且 VWAP slope <= 0
        这是行业 4-6% WR 提升的最稳定过滤器之一。
        返回 (allowed, reason)
        注意: 仅作为过滤器使用，不豁免任何 filter。
        （真源 _vwap_alignment L4885–4942 逐行保真）
        """
        try:
            df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df_5 is None or len(df_5) < 20:
                return FilterResult(True, "5min 数据不足，跳过 VWAP 检查", "vwap_alignment")

            cur_close = float(df_5['close'].iloc[-1])
            # 用今天所有 5min bar 计算 VWAP (典型定义: tp*vol 累计 / vol 累计)
            # 真源 current_date_str 从未赋值 → hasattr 恒 False → 恒走 tail(48)（保真）
            today_df = df_5[df_5['datetime'].astype(str).str.startswith(self.current_date_str)] if hasattr(self, 'current_date_str') else df_5.tail(48)
            if today_df is None or len(today_df) < 5:
                return FilterResult(True, "今日数据不足，跳过 VWAP 检查", "vwap_alignment")

            tp_arr = (today_df['high'] + today_df['low'] + today_df['close']) / 3
            vol_arr = today_df['volume'].values
            cum_tp_vol = (tp_arr * vol_arr).cumsum()
            cum_vol = vol_arr.cumsum()
            vwap_series = cum_tp_vol / cum_vol
            if len(vwap_series) < 5:
                return FilterResult(True, "VWAP 数据不足，跳过", "vwap_alignment")

            cur_vwap = float(vwap_series.iloc[-1])
            # VWAP slope: 比较最近 5 根的 VWAP 与更早 5 根
            if len(vwap_series) >= 10:
                recent_vwap = float(vwap_series.iloc[-5:].mean())
                earlier_vwap = float(vwap_series.iloc[-10:-5].mean())
                slope = recent_vwap - earlier_vwap
            else:
                slope = 0.0

            if direction == "LONG":
                if cur_close < cur_vwap:
                    return FilterResult(False, (
                        f"VWAP alignment 失败：当前 close={cur_close:.1f} < VWAP={cur_vwap:.1f}，"
                        f"价格位于 VWAP 之下，禁止开多（行业共识）"
                    ), "vwap_alignment")
                if slope < -1.0:  # VWAP 向下倾斜超过 1 点
                    return FilterResult(False, f"VWAP 向下倾斜 ({slope:.1f})，不允许做多", "vwap_alignment")
                return FilterResult(True, f"VWAP alignment OK: close={cur_close:.1f} > VWAP={cur_vwap:.1f}, slope={slope:.1f}", "vwap_alignment")
            else:  # SHORT
                if cur_close > cur_vwap:
                    return FilterResult(False, (
                        f"VWAP alignment 失败：当前 close={cur_close:.1f} > VWAP={cur_vwap:.1f}，"
                        f"价格位于 VWAP 之上，禁止开空"
                    ), "vwap_alignment")
                if slope > 1.0:
                    return FilterResult(False, f"VWAP 向上倾斜 ({slope:.1f})，不允许做空", "vwap_alignment")
                return FilterResult(True, f"VWAP alignment OK: close={cur_close:.1f} < VWAP={cur_vwap:.1f}, slope={slope:.1f}", "vwap_alignment")
        except Exception as e:
            logging.warning(f"VWAP alignment 检查失败: {e}")
            return FilterResult(True, "VWAP 检查异常，放行", "vwap_alignment")
