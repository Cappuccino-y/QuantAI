"""strategies.market_context — ATR 汇总 / OI 四态 / 动态位阶。

真源映射（autotrade_fix.py，design.md §4.2 market_context 表）:
- MarketContextService.calculate_fut_atr   ← _calculate_fut_atr L459–513
- MarketContextService.compute_oi_state    ← _compute_oi_state L516–552
- MarketContextService.compute_dynamic_levels ← _compute_dynamic_levels L1521–1606
- indicators.calc_atr（嵌套闭包提为模块级纯函数）← L473–486

行为保持: 全部阈值（OI ±0.5%、布林 (20,2)、50/100 整数关口分档、n<5 兜底 ±30）、
日志文案、异常兜底路径逐行对齐真源。
结构差异（已在 ARCHITECTURE.md 决策记录）:
- 真源上帝类的 ATR/OI 状态字段（atr_5/atr_15/atr_60/stress_level/oi_state_text，
  __init__ L399/L421–424）归本服务持有
- 真源单一 self.symbol 换月时赋值（L5456–5459）→ 阶段 4 rollover_manager 须同时
  更新 MarketDataService.symbol 与本服务 symbol
"""
import logging
import time

from quantai.strategies.indicators import calc_atr


class MarketContextService:
    """期货 ATR / 持仓量四态 / 动态技术位服务。

    持有真源上帝类的指标状态字段（__init__ L399 / L421–424）:
    - atr_5 / atr_15 / atr_60: 5/15/60 分钟 ATR（calculate_fut_atr 刷新）
    - stress_level: 应激指标 = atr_5 / atr_60（默认 1.0）
    - oi_state_text: 持仓量量仓配合状态文本（默认"持仓量数据不可用"）
    """

    def __init__(self, api, symbol: str):
        self.api = api
        self.symbol = symbol
        # 真源上帝类状态字段（__init__ L399 / L421–424）
        self.oi_state_text = "持仓量数据不可用"  # 真源 L399
        self.atr_5 = 0.0    # 5分钟期货ATR（真源 L421）
        self.atr_15 = 0.0   # 15分钟期货ATR（真源 L422）
        self.atr_60 = 0.0   # 60分钟期货ATR（真源 L423）
        self.stress_level = 1.0  # 应激指标，默认正常（真源 L424）

    # 新增方法：计算期货ATR
    def calculate_fut_atr(self):
        """
        基于IM主力合约计算5/15/60分钟ATR，并更新stress_level
        （真源 _calculate_fut_atr L459–513 逐行保真；嵌套闭包 calc_atr
        提为 strategies/indicators.calc_atr，design.md §4.2 既定）
        """
        try:
            # 获取足够长的K线序列（取200根，保证覆盖14周期平均值）
            # 天勤：get_kline_serial 返回 DataFrame，需先订阅再等待更新
            kline_5m = self.api.get_kline_serial(self.symbol, 5 * 60, data_length=200)
            kline_15m = self.api.get_kline_serial(self.symbol, 15 * 60, data_length=200)
            kline_60m = self.api.get_kline_serial(self.symbol, 60 * 60, data_length=200)

            # 等待数据到达（确保有足够历史）
            self.api.wait_update(deadline=time.time() + 5)

            self.atr_5 = calc_atr(kline_5m)
            self.atr_15 = calc_atr(kline_15m)
            self.atr_60 = calc_atr(kline_60m)

            # ========== 8/14 新增：持仓量量仓配合状态 ==========
            # 期货核心指标（指数数据没有 OI）：增仓=主动进攻，减仓=平仓驱动
            # 增仓上行=多头主动（强），减仓上行=空头平仓（弱，不可追）
            try:
                oi_state = self.compute_oi_state(kline_5m)
            except Exception as oi_e:
                logging.warning(f"量仓状态计算失败: {oi_e}")
                oi_state = "持仓量数据不可用"
            self.oi_state_text = oi_state
            # =================================================

            # 计算应激等级
            if self.atr_60 > 0:
                self.stress_level = self.atr_5 / self.atr_60
            else:
                self.stress_level = 1.0

            logging.info(
                f"期货ATR: 5min={self.atr_5:.2f}, 15min={self.atr_15:.2f}, 60min={self.atr_60:.2f}, Stress={self.stress_level:.2f} | {oi_state}")

        except Exception as e:
            logging.error(f"计算期货ATR失败: {e}，使用默认值")

    def compute_oi_state(self, kline_5m) -> str:
        """
        计算期货持仓量量仓配合状态（8/14 新增）
        天勤 K 线序列带 open_oi（开盘持仓量）列。
        判定（业界共识，中证1000 特性：量化资金集中，量仓配合最关键）：
          - 增仓上行 = 多头主动进攻（强趋势，可顺势）
          - 增仓下行 = 空头主动进攻（强趋势，可顺势）
          - 减仓上行 = 空头平仓驱动（弱反弹，禁止追多）
          - 减仓下行 = 多头平仓驱动（弱回落，禁止追空）
        （真源 _compute_oi_state L516–552 逐行保真）
        """
        if kline_5m is None or len(kline_5m) < 21 or 'open_oi' not in kline_5m.columns:
            return "持仓量数据不可用（<21根或无 open_oi 列）"
        oi = kline_5m['open_oi']
        closes = kline_5m['close']
        cur_oi = float(oi.iloc[-1])
        prev_oi = float(oi.iloc[-21:-1].mean())  # 前 20 根均值
        if prev_oi <= 0:
            return "持仓量数据异常（前20根均值为0）"
        oi_change_pct = (cur_oi - prev_oi) / prev_oi * 100
        # 价格变化（用最后两根收盘价）
        cur_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        price_up = cur_close > prev_close
        oi_up = oi_change_pct > 0.5          # 增仓阈值
        oi_down = oi_change_pct < -0.5       # 减仓阈值

        if oi_up and price_up:
            state = "增仓上行（多头主动进攻，强）"
        elif oi_up and not price_up:
            state = "增仓下行（空头主动进攻，强）"
        elif oi_down and price_up:
            state = "减仓上行（空头平仓驱动，弱反弹，禁止追多）"
        elif oi_down and not price_up:
            state = "减仓下行（多头平仓驱动，弱回落，禁止追空）"
        else:
            state = f"持仓量平稳（{oi_change_pct:+.1f}%）"
        return f"{state}（当前OI={cur_oi:.0f}，前20根均值={prev_oi:.0f}，OI变化{oi_change_pct:+.1f}%）"

    def compute_dynamic_levels(self, df_5, cur_price, direction):
        """
        动态计算阻力位 (LONG 抄底用) 和支撑位 (SHORT 摸顶用)
        返回 (resistance_levels, support_levels) - 已按距离当前价排序

        技术位:
        - 布林带 (20, 2) 上轨/中轨/下轨
        - VWAP (当日)
        - 近期 20/50 根 K 线最高/最低
        - 前一根 K 线高/低
        - 整数关口 (50 点一档)

        （真源 _compute_dynamic_levels L1521–1606 逐行保真；
        direction 参数真源函数体未使用，签名原样保留）
        """
        try:
            closes = list(df_5['close'].iloc[-50:].values)
            highs = list(df_5['high'].iloc[-50:].values)
            lows = list(df_5['low'].iloc[-50:].values)
            n = len(closes)
            if n < 5:
                return [cur_price + 30], [cur_price - 30]

            # 布林带 (20, 2)
            bb_mid = sum(closes[-20:]) / 20
            bb_var = sum((c - bb_mid) ** 2 for c in closes[-20:]) / 20
            bb_std = bb_var ** 0.5
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std

            # 近期高/低
            n20_high = max(highs[-20:])
            n20_low = min(lows[-20:])
            n50_high = max(highs)
            n50_low = min(lows)

            # 前一根 K 线
            prev_high = highs[-2] if n >= 2 else cur_price
            prev_low = lows[-2] if n >= 2 else cur_price

            # 整数关口
            step = 50 if cur_price < 9000 else 100
            rn_above = (int(cur_price / step) + 1) * step
            rn_below = (int(cur_price / step)) * step

            # VWAP (简化: 当日累计成交量加权)
            # 修复 M1: df index 是 RangeIndex 不是时间戳，原 df_5.index[-1].date() 抛 AttributeError
            # → 一直走 except → VWAP 恒等于现价，所有技术位里的 VWAP 参考失效。
            # 改用 fetcher 输出的小写 'datetime' 列。
            try:
                if 'datetime' not in df_5.columns:
                    raise ValueError("df_5 缺少 datetime 列")
                idx_today = None
                cur_date = df_5['datetime'].iloc[-1].date()
                for i in range(len(df_5) - 1, -1, -1):
                    if df_5['datetime'].iloc[i].date() != cur_date:
                        idx_today = i + 1
                        break
                if idx_today is None:
                    idx_today = max(0, len(df_5) - 48)
                day_df = df_5.iloc[idx_today:]
                tp_arr = (day_df['high'] + day_df['low'] + day_df['close']) / 3
                vwap = float((tp_arr * day_df['volume']).sum() / day_df['volume'].sum())
            except Exception:
                vwap = cur_price

            # 阻力位 (LONG 抄底用 - 上方)
            resistance = sorted(set([
                bb_upper,
                n20_high,
                n50_high,
                prev_high,
                float(rn_above),
                vwap
            ]), reverse=True)
            # 支撑位 (SHORT 摸顶用 - 下方)
            support = sorted(set([
                bb_lower,
                n20_low,
                n50_low,
                prev_low,
                float(rn_below),
                vwap
            ]))

            return resistance, support
        except Exception as e:
            logging.warning(f"动态技术位计算失败: {e}")
            return [cur_price + 30], [cur_price - 30]
