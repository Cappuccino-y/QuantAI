"""strategies.entry_filters — 入场过滤器链（真源 L4422–4693，6 个方法）。

方法映射（design.md §4.2 entry_filters 表）:
- EntryFilters.check_trend_alignment   ← _check_trend_alignment L4422–4493
- EntryFilters.check_session_extremes  ← _check_session_extremes L4495–4538
- EntryFilters.confirm_breakout_bar    ← _confirm_breakout_bar L4540–4577
- EntryFilters.check_htf_bias          ← _check_htf_bias L4584–4619
- EntryFilters.check_entry_volume      ← _check_entry_volume L4621–4652
- EntryFilters.check_entry_confirmation ← _check_entry_confirmation L4654–4693

行为保持: 全部阈值（EMA10/20 排列、3/3 bar 确认、±10 点禁区、创新低 50 点防护、
量能 1.0x/突破 1.3x、收盘位置 0.2×ATR）与全部拦截/放行文案逐行对齐真源，
含 P1 修复（7/2 连续性、7/3 创新低抄底）与 8/6 放宽注释。

结构差异（ARCHITECTURE.md 阶段 3 决策记录）:
- 真源返回 Tuple[bool, str] → 本版返回 models.FilterResult（design.md 设计要点 1
  既定结构化输出；allowed/reason 与真源二元组逐项对应）
- 真源读上帝类 self.atr_5（现归 MarketContextService 持有）→ 注入 atr5_fn；
  阶段 5 装配: atr5_fn=lambda: mcs.atr_5（未注入时视为 0 = 真源 ATR 未就绪路径）
- 过滤器链的编排（调用顺序/豁免衔接）属 conditional_orders（阶段 4），本模块只提供
  单个过滤器，不做链调度
"""
import logging
from typing import Callable, Optional, Tuple

from quantai.models import FilterResult


class EntryFilters:
    """入场过滤器（6 个，可独立调用；链编排由阶段 4 conditional_orders 负责）。"""

    def __init__(self, index_fetcher, index_name: str = "中证1000",
                 atr5_fn: Optional[Callable[[], float]] = None):
        self.index_fetcher = index_fetcher
        self.index_name = index_name
        self.atr5_fn = atr5_fn or (lambda: 0.0)

    def check_trend_alignment(self, direction: str) -> FilterResult:
        """
        多时段方向锁：60min 趋势与入场方向一致才允许。
        返回 (allowed, reason)
        （真源 _check_trend_alignment L4422–4493 逐行保真）
        """
        try:
            df_60 = self.index_fetcher.get_kline_data(self.index_name, "60min")
            if df_60 is None or len(df_60) < 15:
                return FilterResult(True, "60min 数据不足，跳过趋势过滤", "trend_alignment")

            closes_60 = list(df_60['close'].values)
            n60 = len(closes_60)
            if n60 < 5:
                return FilterResult(True, "60min 数据过少，跳过趋势过滤", "trend_alignment")

            # 60min 趋势判定: EMA10/EMA20 排列 + 收盘 vs EMA10
            ema10 = sum(closes_60[-10:]) / 10
            ema20 = sum(closes_60[-20:]) / 20 if n60 >= 20 else ema10
            cur_close_60 = closes_60[-1]

            sixty_bullish = cur_close_60 > ema10 and ema10 > ema20
            sixty_bearish = cur_close_60 < ema10 and ema10 < ema20

            # ========== P1 修复：连续性检查 (7/2 临界点滞后案例) ==========
            # 7/2 10:41 BUY @8534，60min 当时 bullish 允许通过
            # 但 24 分钟后 60min 转空触发 -8800 止损
            # 修复：要求"近 3 根 60min bar 中至少 2 根同向确认"
            # 单纯当前 bar 的 close > EMA10 不够，单根 bar 可能跳空误导
            #
            # 8/6 放宽：确认标准从 2/3 提高到 3/3（更严才拦，反向更松）
            # 原 2/3 确认在震荡中过严，导致 60min 反向确认拦截过多
            # 现在只有"3/3 完全同向"才拦逆势，2/3 时放行（配合 HTF 放宽）
            # =================================================================
            lookback = min(3, n60 - 1)
            if lookback >= 2:
                recent_bars = closes_60[-(lookback):]
                # 计算每根 bar 的 close vs EMA10(那根 bar 时刻)
                # 简化：当前 EMA10 适用于"近期窗口"的所有 bar
                bullish_count = sum(1 for c in recent_bars if c > ema10)
                bearish_count = sum(1 for c in recent_bars if c < ema10)
            else:
                bullish_count = 1 if sixty_bullish else 0
                bearish_count = 1 if sixty_bearish else 0

            # 8/6 放宽：确认标准 3/3 (完全同向才拦)
            confirmed_bullish = sixty_bullish and bullish_count >= 3
            confirmed_bearish = sixty_bearish and bearish_count >= 3

            if direction == "LONG" and confirmed_bearish:
                return FilterResult(False, (
                    f"60min 空头排列且连续 {bearish_count}/{lookback} bar 确认 (close={cur_close_60:.0f} < EMA10={ema10:.0f} < EMA20={ema20:.0f})，"
                    f"禁止逆势开多（防止接飞刀）"
                ), "trend_alignment")
            if direction == "SHORT" and confirmed_bullish:
                return FilterResult(False, (
                    f"60min 多头排列且连续 {bullish_count}/{lookback} bar 确认 (close={cur_close_60:.0f} > EMA10={ema10:.0f} > EMA20={ema20:.0f})，"
                    f"禁止逆势开空（防止摸顶）"
                ), "trend_alignment")
            if direction == "LONG" and not confirmed_bullish and sixty_bullish:
                return FilterResult(False, (
                    f"60min 趋势刚转多但仅 {bullish_count}/{lookback} bar 确认，不稳定，"
                    f"暂禁开多（等连续 2 根 bar 确认趋势）"
                ), "trend_alignment")
            if direction == "SHORT" and not confirmed_bearish and sixty_bearish:
                return FilterResult(False, (
                    f"60min 趋势刚转空但仅 {bearish_count}/{lookback} bar 确认，不稳定，"
                    f"暂禁开空（等连续 2 根 bar 确认趋势）"
                ), "trend_alignment")
            return FilterResult(True, f"60min 趋势与 {direction} 方向一致 ({bullish_count if direction=='LONG' else bearish_count}/{lookback} bar 确认)", "trend_alignment")
        except Exception as e:
            logging.warning(f"趋势方向检查失败: {e}")
            return FilterResult(True, "趋势过滤异常，放行", "trend_alignment")

    def check_session_extremes(self, entry_price: float, direction: str) -> FilterResult:
        """
        Session High/Low 禁区：入场价不进入今高/低 ±10 点范围。
        因为日高/低点聚集大量止盈/反向订单，假突破概率最高。
        返回 (allowed, reason)
        （真源 _check_session_extremes L4495–4538 逐行保真）
        """
        try:
            df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df_5 is None or len(df_5) < 48:
                return FilterResult(True, "5min 数据不足，跳过 High/Low 过滤", "session_extremes")

            # 取今日数据 (最近 48 根 5min = 4h)
            recent = df_5.iloc[-48:]
            today_high = float(recent['high'].max())
            today_low = float(recent['low'].min())
            threshold = 10.0  # 10 点禁区

            if direction == "LONG" and entry_price >= today_high - threshold:
                return FilterResult(False, (
                    f"入场价 {entry_price:.1f} 接近今高 {today_high:.1f}（差 {today_high - entry_price:.1f} 点 < {threshold}），"
                    f"日高区域假突破概率最高，禁止入场"
                ), "session_extremes")
            if direction == "SHORT" and entry_price <= today_low + threshold:
                return FilterResult(False, (
                    f"入场价 {entry_price:.1f} 接近今低 {today_low:.1f}（差 {entry_price - today_low:.1f} 点 < {threshold}），"
                    f"日低区域假突破概率最高，禁止入场"
                ), "session_extremes")

            # ========== P1 修复：防"创今日新低抄底"（7/3 9:31 案例） ==========
            # 7/3 9:31:09 BUY @8393.40 → 13秒后 -2400
            # 原因：开盘瞬间价格比今低还低（开盘跳空）→ 抄底在最低点
            # 修复：LONG 时若入场价 < 今低 30 点以上，判定为"创今日新低抄底"→ 拒绝
            # 30 点 = 略大于 0.4% (8393 的 0.4% ≈ 33 点) 防止接飞刀
            # =================================================================
            new_low_buffer = 50.0
            if direction == "LONG" and entry_price < today_low - new_low_buffer:
                return FilterResult(False, (
                    f"入场价 {entry_price:.1f} 创今日新低（比今低 {today_low:.1f} 低 {today_low - entry_price:.1f} 点 > {new_low_buffer}），"
                    f"防接飞刀，禁止在创日内新低抄底"
                ), "session_extremes")
            return FilterResult(True, f"入场价 {entry_price:.1f} 距今{'高' if direction == 'LONG' else '低'}安全", "session_extremes")
        except Exception as e:
            logging.warning(f"Session High/Low 检查失败: {e}")
            return FilterResult(True, "High/Low 过滤异常，放行", "session_extremes")

    def confirm_breakout_bar(self, trigger_type: str, trigger_price: float,
                             direction: str) -> FilterResult:
        """
        突破确认：价格穿透触发位 且 当前 5min bar 收盘价必须在同向一侧（非影线穿透）。
        防止仅影线触及触发价就开仓（假突破的经典特征）。
        返回 (confirmed, reason)
        （真源 _confirm_breakout_bar L4540–4577 逐行保真）
        """
        try:
            df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df_5 is None or len(df_5) < 2:
                return FilterResult(True, "5min 数据不足，跳过确认", "breakout_bar")

            cur_close = float(df_5['close'].iloc[-1])
            cur_high = float(df_5['high'].iloc[-1])
            cur_low = float(df_5['low'].iloc[-1])

            if trigger_type == "PRICE_ABOVE":
                # 触发条件：价格 >= trigger_price。确认：收盘价也必须 >= trigger_price
                if cur_close < trigger_price:
                    # 影线穿刺：high 碰到了但 close 没守住
                    penetration = cur_high - trigger_price
                    return FilterResult(False, (
                        f"影线穿刺未确认：high={cur_high:.1f} 触及触发价 {trigger_price:.1f} "
                        f"(穿透 {penetration:.1f} 点)，但 close={cur_close:.1f} 回落到触发价下方，"
                        f"典型假突破特征，拒绝开仓"
                    ), "breakout_bar")
            elif trigger_type == "PRICE_BELOW":
                if cur_close > trigger_price:
                    penetration = trigger_price - cur_low
                    return FilterResult(False, (
                        f"影线穿刺未确认：low={cur_low:.1f} 触及触发价 {trigger_price:.1f} "
                        f"(穿透 {penetration:.1f} 点)，但 close={cur_close:.1f} 回升到触发价上方，"
                        f"典型假突破特征，拒绝开仓"
                    ), "breakout_bar")
            return FilterResult(True, "突破确认：收盘价同向穿透触发位", "breakout_bar")
        except Exception as e:
            logging.warning(f"突破确认检查失败: {e}")
            return FilterResult(True, "突破确认异常，放行", "breakout_bar")

    # ========== 第四层（HTF + Volume + 入场确认）==========
    # 业界共识：HTF alignment + Volume confirmation + 5min close confirmation
    # 是减少假突破最有效的三个过滤器（ORB 240k 回测：win rate 46%→68%）
    # ============================================================

    def check_htf_bias(self, direction: str) -> FilterResult:
        """
        大周期趋势对齐 (Daily + Weekly) - 激进评分版 (8/6 F方案):
        - Daily / Weekly 均降级为"评分/备注"，不再硬拦截
        - 只返回 True（放行），把趋势状态写进 reason 供日志/钉钉参考
        背景: 7/9-8/6 市场从 8300 跌到 7507，日线 EMA20≈7947 周线 EMA10≈7949
              原逻辑日线/周线空头累计 292 次硬拦做多 → 信号转化率降到 4%
              但 8/5 明确超跌反弹 +15,080 成功，说明逆大周期反弹是有利润的
        修复: 真正的方向锁交给 60min (check_trend_alignment 已放宽到 3/3)
              大周期只用于提示风险，不再决定是否开仓
        （真源 _check_htf_bias L4584–4619 逐行保真）
        """
        try:
            df_daily = self.index_fetcher.get_kline_data(self.index_name, "日线")
            df_weekly = self.index_fetcher.get_kline_data(self.index_name, "周线")

            notes = []
            if df_daily is not None and len(df_daily) >= 20:
                closes_d = list(df_daily['close'].values)
                ema20_d = sum(closes_d[-20:]) / 20
                cur_d = closes_d[-1]
                daily_state = "多头" if cur_d > ema20_d else "空头"
                notes.append(f"日线{daily_state} close={cur_d:.0f} {'>' if cur_d>ema20_d else '<'} EMA20={ema20_d:.0f}")

            if df_weekly is not None and len(df_weekly) >= 10:
                closes_w = list(df_weekly['close'].values)
                ema10_w = sum(closes_w[-10:]) / 10
                cur_w = closes_w[-1]
                weekly_state = "多头" if cur_w > ema10_w else "空头"
                notes.append(f"周线{weekly_state} close={cur_w:.0f} {'>' if cur_w>ema10_w else '<'} EMA10={ema10_w:.0f}")

            if notes:
                return FilterResult(True, f"大周期(仅评分，不拦截): " + " | ".join(notes), "htf_bias")
            return FilterResult(True, "大周期数据不足，跳过 HTF 检查", "htf_bias")
        except Exception as e:
            logging.warning(f"HTF bias 检查失败: {e}")
            return FilterResult(True, "HTF 检查异常，放行", "htf_bias")

    def check_entry_volume(self, min_ratio: float = 1.0) -> FilterResult:
        """
        入场量能确认 - 激进放宽版 (8/6):
        阈值从 1.5x 降到 1.0x（只需达到均量即可）
        背景: 8/1-8/6 量能不足拦截 146 次，是第二大拦截原因
              震荡/超跌反弹时 5min 单根量很难达到 1.5x 均量
              1.0x = "至少有量参与"（非放量暴冲，但足够确认方向）
        8/14 量能分层: 突破场景（条件单）min_ratio=1.3x 需放量确认；
                      回调/左侧场景保持 1.0x（VCP 量价配合研究）
        （真源 _check_entry_volume L4621–4652 逐行保真）
        """
        try:
            df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df_5 is None or len(df_5) < 25:
                return FilterResult(True, "5min 数据不足，跳过量能检查", "entry_volume")

            volumes = list(df_5['volume'].values)
            if len(volumes) < 21:
                return FilterResult(True, "数据不足 20 根，跳过量能检查", "entry_volume")

            vol_ma = sum(volumes[-21:-1]) / 20  # 前 20 根平均
            cur_vol = volumes[-1]
            ratio = cur_vol / vol_ma if vol_ma > 0 else 0

            if ratio < min_ratio:
                return FilterResult(False, (
                    f"入场量能不足：当前 5min 量 {cur_vol:.0f} / 20 根均量 {vol_ma:.0f} = {ratio:.2f}x，"
                    f"< {min_ratio:.1f}x 阈值（{'突破需放量' if min_ratio > 1.0 else '缩量，量能不足以确认方向'}）"
                ), "entry_volume")
            return FilterResult(True, f"入场量能确认：{ratio:.2f}x ≥ {min_ratio:.1f}x", "entry_volume")
        except Exception as e:
            logging.warning(f"量能检查失败: {e}")
            return FilterResult(True, "量能检查异常，放行", "entry_volume")

    def check_entry_confirmation(self, direction: str) -> FilterResult:
        """
        入场 K 线收盘确认 - 激进放宽版 (8/6):
        - LONG 要求最近 5min bar 收盘价 > 今低 + 0.2×5minATR (只要收盘不在最低即可)
        - SHORT 要求最近 5min bar 收盘价 < 今高 - 0.2×5minATR
        原 0.5×ATR 过严（8/6 模拟: close 距 low 0.0 点被拒，实际是平开长下影）
        0.2×ATR 只过滤"收盘在极端最低/最高"的影线穿刺
        （真源 _check_entry_confirmation L4654–4693 逐行保真）
        """
        try:
            df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df_5 is None or len(df_5) < 2:
                return FilterResult(True, "5min 数据不足，跳过确认", "entry_confirmation")

            cur_close = float(df_5['close'].iloc[-1])
            cur_high = float(df_5['high'].iloc[-1])
            cur_low = float(df_5['low'].iloc[-1])

            atr_5 = self.atr5_fn()  # 真源 self.atr_5（MarketContextService 持有）
            if atr_5 <= 0:
                return FilterResult(True, "ATR 不可用，跳过确认", "entry_confirmation")

            threshold = atr_5 * 0.2

            if direction == "LONG":
                if cur_close < cur_low + threshold:
                    return FilterResult(False, (
                        f"5min bar 收盘价 {cur_close:.1f} 处于今低 {cur_low:.1f} 附近，"
                        f"（差 {cur_close - cur_low:.1f} < {threshold:.1f}），"
                        f"收盘在最低点，拒开多"
                    ), "entry_confirmation")
            else:  # SHORT
                if cur_close > cur_high - threshold:
                    return FilterResult(False, (
                        f"5min bar 收盘价 {cur_close:.1f} 处于今高 {cur_high:.1f} 附近，"
                        f"（差 {cur_high - cur_close:.1f} < {threshold:.1f}），"
                        f"收盘在最高点，拒开空"
                    ), "entry_confirmation")
            return FilterResult(True, f"入场 K 线收盘确认 OK (close={cur_close:.1f})", "entry_confirmation")
        except Exception as e:
            logging.warning(f"入场确认检查失败: {e}")
            return FilterResult(True, "入场确认异常，放行", "entry_confirmation")
