"""strategies.left_side — 左侧信号（真源 _compute_left_side_signals L1608–2049，442 行）。

design.md §三 设计要点 1（计算与渲染分离）的三段落地:
- compute_signals()          → 计算段: 指标计算 + LeftSideSignal 结构化信号
- render_regime/render_signals → 渲染段: 真源 prompt 文本逐字保真
                               （阶段 4 可整体迁往 ai_decision.PromptBuilder，计算段不动）
- dispatch_alerts()          → 告警段: 5min 节流 + SL/TP 建议 + notifier
                               （真源 notifycation.send_dingtalk_message → 注入 DingTalkNotifier）
- compute_left_side_signals() → 组合入口，返回值与真源方法一致（prompt 文本）

依赖注入（默认值 = 真源行为，阶段 5 system.py 装配接线）:
- index_price_fn    ← mds.index_price（真源 self.index_price）
- yesterday_close_fn ← mds.get_yesterday_index_close（真源 L1895）
- dynamic_levels_fn ← mcs.compute_dynamic_levels（真源 L1961）
- notifier          ← DingTalkNotifier（真源 notifycation.send_dingtalk_message）
- warn_fn           ← 真源 _warn_once_per_session（L1276–1284，按 key 按天去重）同款默认实现；
                      ai_decision 迁移后可注入统一实现
- now_fn            ← datetime.now（默认）

行为保持: 全部阈值（RSI 40/45、量比 1.5/2.0、日线 RSI>65、缺口 ±1%、背离 +2/-3、
节流 300s、SL 30/15 点）、回测结论与 8/14 降级规则文案、告警消息模板逐字对齐真源。

结构差异（ARCHITECTURE.md 阶段 3 决策记录）:
- 真源 _last_left_signal_alerts / _warn_log 懒初始化（hasattr）→ 构造初始化（行为等价）
- 真源 L1702–1709 is_yang/body_pct 计算后未使用 → 保真保留
- 真源 L1755 long_complete 计算后未使用 → 保真保留
"""
import logging
from datetime import datetime
from typing import Callable, List, Optional

import pandas as pd

from quantai.models import LeftSideSignal


def _default_warn_once_per_session(key: str, msg: str) -> None:
    """真源 _warn_once_per_session（L1276–1284）同款默认实现: 同一 key 每天只告警一次。"""
    if not hasattr(_default_warn_once_per_session, "_warn_log"):
        _default_warn_once_per_session._warn_log = {}
    today = datetime.now().date()
    last_date = _default_warn_once_per_session._warn_log.get(key)
    if last_date == today:
        return
    _default_warn_once_per_session._warn_log[key] = today
    logging.warning(msg)


class LeftSideStrategy:
    """左侧信号策略（大盘定调 + L12a/L3/D17/D0 + 缺口回补 + 约束规则 + 告警）。"""

    def __init__(self, index_fetcher, index_name: str = "中证1000",
                 index_price_fn: Optional[Callable[[], float]] = None,
                 yesterday_close_fn: Optional[Callable[[], Optional[float]]] = None,
                 dynamic_levels_fn: Optional[Callable] = None,
                 notifier=None,
                 warn_fn: Optional[Callable[[str, str], None]] = None,
                 now_fn: Callable[[], datetime] = datetime.now):
        self.index_fetcher = index_fetcher
        self.index_name = index_name
        self.index_price_fn = index_price_fn or (lambda: 0.0)
        self.yesterday_close_fn = yesterday_close_fn or (lambda: None)
        self.dynamic_levels_fn = dynamic_levels_fn
        self.notifier = notifier
        self.warn_fn = warn_fn or _default_warn_once_per_session
        self.now_fn = now_fn
        # 真源 L1936–1937 懒初始化 → 构造初始化（行为等价）
        self._last_left_signal_alerts = {}

    # ========== 组合入口（真源 _compute_left_side_signals L1608–2049 整体行为） ==========

    def compute_left_side_signals(self) -> str:
        """计算 + 渲染 + 告警，返回 prompt 文本（真源返回值语义）。

        异常路径对齐真源 L2045–2047: 记日志 + 追加"计算失败，跳过"（保留已渲染段落）。
        """
        lines: List[str] = []
        regime = self.compute_regime()
        self.render_regime(lines, regime)
        try:
            sig = self.compute_signals()
            if sig.get("insufficient_5min"):
                # 真源 L1650–1653 提前返回路径
                lines.append("## 🔄 左侧机会信号\n")
                lines.append("⚠️ 5min 数据不足或获取失败，跳过信号计算\n")
                return "\n".join(lines)
            self.render_signals(lines, sig)
            self.dispatch_alerts(sig)
        except Exception as e:
            logging.warning(f"左侧信号计算失败: {e}")
            lines.append("## 🔄 左侧机会信号\n计算失败，跳过\n")
        return "\n".join(lines)

    # ========== 1. 大盘定调（真源 L1615–1645） ==========

    def compute_regime(self) -> Optional[dict]:
        """大盘定调计算段（MA60/200 + 20 日趋势）。数据不足/异常返回 None（真源 L1644–1645）。"""
        try:
            df_daily = self.index_fetcher.get_kline_data(self.index_name, "日线")
            if df_daily is not None and len(df_daily) >= 250:
                closes_d = list(df_daily['close'].values)
                n_d = len(closes_d)
                latest_close_d = float(closes_d[-1])

                ma60 = sum(closes_d[-60:]) / 60
                ma200 = sum(closes_d[-200:]) / 200
                trend_20d = (closes_d[-1] - closes_d[-20]) / closes_d[-20] * 100 if n_d >= 20 else 0

                if latest_close_d > ma200 and trend_20d > 3:
                    regime = "🐂 牛市（上行趋势）"
                elif latest_close_d < ma200 and trend_20d < -3:
                    regime = "🐻 熊市（下行趋势）"
                else:
                    regime = "📊 震荡市"

                ma60_dist = (latest_close_d - ma60) / ma60 * 100
                ma200_dist = (latest_close_d - ma200) / ma200 * 100
                return {
                    "latest_close_d": latest_close_d,
                    "ma60": ma60,
                    "ma200": ma200,
                    "trend_20d": trend_20d,
                    "regime": regime,
                    "ma60_dist": ma60_dist,
                    "ma200_dist": ma200_dist,
                }
        except Exception as e:
            logging.warning(f"大盘定调计算失败: {e}")
        return None

    def render_regime(self, lines: List[str], regime: Optional[dict]) -> None:
        """大盘定调渲染段（真源 L1637–1643 文本逐字）。"""
        if regime is None:
            return
        lines.append("## 📊 大盘定调（日线级别）")
        lines.append(f"- 中证1000 最新收盘: {regime['latest_close_d']:.2f}")
        lines.append(f"- 60日均线: {regime['ma60']:.2f}（{'上方' if regime['latest_close_d'] > regime['ma60'] else '下方'} {abs(regime['ma60_dist']):.1f}%）")
        lines.append(f"- 200日均线: {regime['ma200']:.2f}（{'上方' if regime['latest_close_d'] > regime['ma200'] else '下方'} {abs(regime['ma200_dist']):.1f}%）")
        lines.append(f"- 近20日涨跌: {regime['trend_20d']:+.1f}%")
        lines.append(f"- 定调: {regime['regime']}")
        lines.append("")

    # ========== 2. 左侧信号计算段（真源 L1647–1811） ==========

    def compute_signals(self) -> dict:
        """5min 信号计算段（逐行保真）。

        返回 dict 携带渲染/告警所需全部值 + signals（LeftSideSignal 结构化输出）；
        5min 数据不足返回 {"insufficient_5min": True}（真源 L1650–1653）；
        异常向上抛（由 compute_left_side_signals 捕获，真源 L2045–2047）。
        """
        signals: List[LeftSideSignal] = [
            LeftSideSignal(name="L12a", direction="LONG",
                           detail="5m RSI<40 + 15m RSI<45 + 量比≥1.5（双周期超卖共振）"),
            LeftSideSignal(name="L3", direction="LONG",
                           detail="5m 底背离 + 量比≥2（精准捕捉）"),
            LeftSideSignal(name="D17", direction="SHORT",
                           detail="5m 顶背离 + 日线超买"),
            LeftSideSignal(name="D0", direction="SHORT",
                           detail="5m 新高回落 + 量比≥2"),
        ]
        df_5 = self.index_fetcher.get_kline_data(self.index_name, "5min")
        if df_5 is None or len(df_5) < 25:
            return {"insufficient_5min": True, "signals": signals}
        # 检查数据时效性 (仅告警，不阻断计算)
        # 修复 M2: df index 是 RangeIndex（int），原逻辑把 int 索引当时间戳 → 算出 1970 年
        # → has_today 永远 False → 每天误报"5min 无今日数据"。改用 fetcher 输出的 'datetime' 列。
        try:
            today_date = self.now_fn().date()
            if 'datetime' in df_5.columns:
                sample_dt = df_5['datetime'].iloc[-5:]
                has_today = any(pd.to_datetime(dt).date() == today_date for dt in sample_dt)
                latest_label = df_5['datetime'].iloc[-1]
            else:
                # 兜底：无 datetime 列时按原逻辑尽力而为
                has_today = any(
                    datetime.fromtimestamp(int(idx) // 10**9 if int(idx) > 10**12 else int(idx)).date() == today_date
                    for idx in df_5.index[-5:]
                )
                latest_label = df_5.index[-1]
            if not has_today:
                self.warn_fn("stale_5min",
                             f"5min 数据无今日数据，最新时间: {latest_label}，继续用最近数据")
        except Exception:
            logging.warning("5min 数据时效性检查失败，跳过检查继续计算")

        closes_5 = list(df_5['close'].values)
        volumes_5 = list(df_5['volume'].values)
        n5 = len(closes_5)

        # RSI(14)
        period = 14
        if n5 >= period + 1:
            win = [closes_5[i] - closes_5[i - 1] for i in range(n5 - period, n5)]
            avg_gain = sum(d for d in win if d > 0) / period
            avg_loss = sum(-d for d in win if d < 0) / period
            rsi_14 = 100.0 if avg_loss == 0 else (100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        else:
            rsi_14 = 50.0

        # 成交量比值
        if n5 >= 21:
            vol_ma = sum(volumes_5[-21:-1]) / 20
        elif n5 > 1:
            vol_ma = sum(volumes_5[:-1]) / (n5 - 1)
        else:
            vol_ma = volumes_5[-1]
        vol_cur = volumes_5[-1]
        vol_ratio = vol_cur / vol_ma if vol_ma > 0 else 1.0
        prev_vol_ratio = (volumes_5[-2] / vol_ma) if n5 >= 2 and vol_ma > 0 else 1.0

        # K线形态（真源计算后未使用，保真保留）
        if n5 >= 2:
            prev_c = closes_5[-2]
            cur_c = closes_5[-1]
            is_yang = cur_c > prev_c
            body_pct = abs(cur_c - prev_c) / prev_c * 100
        else:
            is_yang = False
            body_pct = 0

        # 信号判定 (LONG 超卖反弹)
        rsi_oversold = rsi_14 < 40
        vol_peak = vol_ratio >= 1.5 or prev_vol_ratio >= 1.5

        # L12a 需要 15m RSI: 单独获取 15min K 线
        rsi_15_current = 50.0
        try:
            df_15_check = self.index_fetcher.get_kline_data(self.index_name, "15min")
            if df_15_check is not None and len(df_15_check) >= 15:
                closes_15_check = list(df_15_check['close'].values)
                n15_check = len(closes_15_check)
                win_15 = [closes_15_check[i] - closes_15_check[i - 1]
                          for i in range(n15_check - 14, n15_check)]
                avg_g_15 = sum(d for d in win_15 if d > 0) / 14
                avg_l_15 = sum(-d for d in win_15 if d < 0) / 14
                rsi_15_current = 100.0 if avg_l_15 == 0 else (100.0 - 100.0 / (1.0 + avg_g_15 / avg_l_15))
        except Exception:
            rsi_15_current = 50.0
        l12a_rsi_15_ok = rsi_15_current < 45

        # L3 / L22 5m 底背离: 价格新低 + RSI 没新低
        long_div_ok = False
        min_low_20 = 0.0
        min_rsi_20 = 100.0
        if n5 >= 21:
            min_low_20 = min([df_5['low'].iloc[i] for i in range(n5 - 20, n5)])
            # 算 20 根内每根的 RSI(14) → 找最低
            rsi_window = []
            for k in range(n5 - 20, n5):
                if k >= period + 1:
                    win_k = [closes_5[i] - closes_5[i - 1] for i in range(k - period, k)]
                    avg_g_k = sum(d for d in win_k if d > 0) / period
                    avg_l_k = sum(-d for d in win_k if d < 0) / period
                    rsi_k = 100.0 if avg_l_k == 0 else (100.0 - 100.0 / (1.0 + avg_g_k / avg_l_k))
                    rsi_window.append(rsi_k)
            min_rsi_20 = min(rsi_window) if rsi_window else 100
            price_new_low = df_5['low'].iloc[-1] <= min_low_20 * 1.0001
            rsi_not_new_low = rsi_14 > min_rsi_20 + 2.0
            long_div_ok = price_new_low and rsi_not_new_low

        # L12a: 5m RSI<40 + 15m RSI<45 + 量比≥1.5
        l12a_complete = rsi_oversold and l12a_rsi_15_ok and vol_peak
        # L3: 5m 底背离 + 量比≥2
        l3_complete = long_div_ok and (vol_ratio >= 2.0 or prev_vol_ratio >= 2.0)
        long_complete = l12a_complete or l3_complete  # 真源计算后未使用，保真保留

        # ========== SHORT 摸顶信号 D17 + D0 ==========
        # 60 天 5min 数据回测结论：
        #   D17 (5m顶背离+日线超买) → 41次, 68%胜率, 4h累计+22.97%
        #   D0  (5m新高回落+量比≥2) → 8次, 88%胜率, 4h累计+11.18%

        # D17: 5m 顶背离 (价格新高但 RSI 没新高)
        if n5 >= 21:
            max_high_20 = max([df_5['high'].iloc[i] for i in range(n5 - 20, n5)])
            # 算 20 根内最大 RSI (复用上面 rsi_window 的逻辑，但找 max)
            rsi_20_max_values = []
            for k in range(n5 - 20, n5):
                if k >= period + 1:
                    win_k = [closes_5[i] - closes_5[i - 1] for i in range(k - period, k)]
                    avg_g_k = sum(d for d in win_k if d > 0) / period
                    avg_l_k = sum(-d for d in win_k if d < 0) / period
                    rsi_k = 100.0 if avg_l_k == 0 else (100.0 - 100.0 / (1.0 + avg_g_k / avg_l_k))
                    rsi_20_max_values.append(rsi_k)
            max_rsi_20 = max(rsi_20_max_values) if rsi_20_max_values else 100

            is_price_new_high = df_5['high'].iloc[-1] >= max_high_20
            is_rsi_not_new_high = rsi_14 < max_rsi_20 - 3
            top_divergence = is_price_new_high and is_rsi_not_new_high
        else:
            top_divergence = False
            max_rsi_20 = 100

        # D17 加成: 日线 RSI>65
        try:
            df_d_check = self.index_fetcher.get_kline_data(self.index_name, "日线")
            daily_overbought = False
            daily_rsi = 50.0
            if df_d_check is not None and len(df_d_check) >= 20:
                closes_d_check = list(df_d_check['close'].values)
                n_d_check = len(closes_d_check)
                if n_d_check >= 15:
                    win_d = [closes_d_check[i] - closes_d_check[i - 1] for i in range(n_d_check - 14, n_d_check)]
                    avg_g_d = sum(d for d in win_d if d > 0) / 14
                    avg_l_d = sum(-d for d in win_d if d < 0) / 14
                    daily_rsi = 100.0 if avg_l_d == 0 else (100.0 - 100.0 / (1.0 + avg_g_d / avg_l_d))
                    daily_overbought = daily_rsi > 65
        except Exception:
            daily_overbought = False
            daily_rsi = 50.0

        d17_complete = top_divergence and daily_overbought

        # D0: 5m 新高回落(20根)+量比≥2
        if n5 >= 21:
            max_high_20_d0 = max([df_5['high'].iloc[i] for i in range(n5 - 20, n5)])
            is_new_high_d0 = df_5['high'].iloc[-1] >= max_high_20_d0
            is_pullback = closes_5[-1] < closes_5[-2]
            vol_big_d0 = vol_ratio >= 2.0 or prev_vol_ratio >= 2.0
            d0_complete = is_new_high_d0 and is_pullback and vol_big_d0
        else:
            d0_complete = False

        # 缺口回补
        prev_close = self.yesterday_close_fn()
        gap_pct = None
        if prev_close and prev_close > 0:
            gap_pct = (self.index_price_fn() - prev_close) / prev_close * 100

        # 结构化信号标记（bool() 归一: 真源 df.values 参与比较产生 numpy.bool_，
        # 计算逻辑不变，仅结构化输出字段类型归一）
        by_name = {s.name: s for s in signals}
        by_name["L12a"].triggered = bool(l12a_complete)
        by_name["L3"].triggered = bool(l3_complete)
        by_name["D17"].triggered = bool(d17_complete)
        by_name["D0"].triggered = bool(d0_complete)

        return {
            "insufficient_5min": False,
            "signals": signals,
            "df_5": df_5,
            "n5": n5,
            "rsi_14": rsi_14,
            "rsi_15_current": rsi_15_current,
            "vol_ratio": vol_ratio,
            "prev_vol_ratio": prev_vol_ratio,
            "rsi_oversold": rsi_oversold,
            "l12a_rsi_15_ok": l12a_rsi_15_ok,
            "vol_peak": vol_peak,
            "l12a_complete": l12a_complete,
            "min_low_20": min_low_20,
            "min_rsi_20": min_rsi_20,
            "l3_complete": l3_complete,
            "is_price_new_high": is_price_new_high,
            "is_rsi_not_new_high": is_rsi_not_new_high,
            "max_rsi_20": max_rsi_20,
            "daily_overbought": daily_overbought,
            "daily_rsi": daily_rsi,
            "d17_complete": d17_complete,
            "is_new_high_d0": is_new_high_d0,
            "is_pullback": is_pullback,
            "vol_big_d0": vol_big_d0,
            "d0_complete": d0_complete,
            "gap_pct": gap_pct,
            "gap_prev_close": prev_close,
        }

    # ========== 渲染段（真源 L1813–1932 文本逐字） ==========

    def render_signals(self, lines: List[str], sig: dict) -> None:
        """信号 prompt 渲染段（逐字保真；阶段 4 可整体迁往 ai_decision.PromptBuilder）。"""
        closes_5 = list(sig["df_5"]['close'].values)
        n5 = sig["n5"]

        lines.append("## 🔄 左侧机会信号（预计算，仅供参考，AI 独立判断）")
        lines.append("")
        lines.append("**6/25 回测结论** (60 天 5min):")
        lines.append("  - 新 LONG 抄底: L12a (12次+8.03% 67%胜率) + L3 (8次+5.74% 88%胜率)")
        lines.append("  - 新 SHORT 摸顶: D17 (41次+22.97% 68%胜率) + D0 (8次+11.18% 88%胜率)")
        lines.append("")
        lines.append("**8/14 降级规则：左侧信号只是『观察信号』，不构成入场建议！**")
        lines.append("  左侧（超卖/背离）直接入场在指数期货上实证效果差（MNQ 研究：回调/超卖直接入场止损率 80%+），")
        lines.append("  中证1000 是量化资金主阵地，急涨急跌勿盲目追/抄。")
        lines.append("  必须等待右侧确认后才可考虑入场：")
        lines.append("  - LONG 右侧确认 = 价格站上 VWAP 且出现一根阳线收盘确认（收盘价 > 开盘价且 > 前一根收盘）")
        lines.append("  - SHORT 右侧确认 = 价格跌破 VWAP 且出现一根阴线收盘确认（收盘价 < 开盘价且 < 前一根收盘）")
        lines.append("  若 VWAP 确认未满足，即使左侧信号完整，也必须 WAIT 或仅设条件单等待突破")
        lines.append("")

        # LONG L12a: 5m RSI<40 + 15m RSI<45 + 量比≥1.5
        lines.append("### LONG 左侧：L12a 双周期超卖共振（主信号）")
        lines.append(f"- {'✅' if sig['rsi_oversold'] else '❌'} RSI(14,5min)={sig['rsi_14']:.0f} （阈值 < 40）")
        lines.append(f"- {'✅' if sig['l12a_rsi_15_ok'] else '❌'} RSI(14,15min)={sig['rsi_15_current']:.0f} （阈值 < 45）")
        lines.append(f"- {'✅' if sig['vol_peak'] else '❌'} 温和放量 （当前量比={sig['vol_ratio']:.1f}x，前根={sig['prev_vol_ratio']:.1f}x，需 ≥ 1.5x）")
        if sig["l12a_complete"]:
            lines.append("  → 🔎 L12a 左侧信号完整（观察级）！不可直接入场，等待右侧确认（站上 VWAP + 阳线收盘确认）后才可考虑 1 手试多，止损=今低-5点，止盈=布林中线/VWAP")
        else:
            missing = []
            if not sig["rsi_oversold"]: missing.append("5m RSI未超卖(<40)")
            if not sig["l12a_rsi_15_ok"]: missing.append("15m RSI未超卖(<45)")
            if not sig["vol_peak"]: missing.append("量比不够(≥1.5)")
            lines.append(f"  → ⚠️ 触发但缺: {' / '.join(missing)}")
        lines.append("")

        # LONG L3: 5m 底背离 + 量比≥2 (辅助精准信号)
        if n5 >= 21:
            l3_price_ok = bool(sig["df_5"]["low"].iloc[-1] <= sig["min_low_20"] * 1.0001)
            l3_rsi_ok = bool(sig["rsi_14"] > sig["min_rsi_20"] + 2.0)
            l3_vol_ok = bool(sig["vol_ratio"] >= 2.0 or sig["prev_vol_ratio"] >= 2.0)
            lines.append("### LONG 左侧：L3 5m 底背离（辅助精准信号）")
            lines.append(f"- {'✅' if l3_price_ok else '❌'} 价格新低 (5min low ≤ 20 根内最低)")
            lines.append(f"- {'✅' if l3_rsi_ok else '❌'} RSI 底背离 (5min RSI={sig['rsi_14']:.0f} > 20根内最低 {sig['min_rsi_20']:.0f}+2)")
            lines.append(f"- {'✅' if l3_vol_ok else '❌'} 放量确认 (量比={sig['vol_ratio']:.1f}x / {sig['prev_vol_ratio']:.1f}x，需 ≥ 2x)")
            if sig["l3_complete"]:
                lines.append("  → 🔎 L3 左侧信号完整（观察级）！不可直接入场，等待右侧确认（站上 VWAP + 阳线收盘确认）后才可考虑 1 手试多，止损=今低-5点，止盈=布林中线/VWAP")
            else:
                missing = []
                if not l3_price_ok: missing.append("价格未新低")
                if not l3_rsi_ok: missing.append("RSI 未底背离")
                if not l3_vol_ok: missing.append("量比不够")
                lines.append(f"  → ⚠️ 触发但缺: {' / '.join(missing)}")
            lines.append("")

        # SHORT 摸顶 D17: 5m 顶背离 + 日线超买
        lines.append("### SHORT 左侧：D17 摸顶（5m 顶背离 + 日线超买）")
        lines.append(f"- {'✅' if sig['is_price_new_high'] else '❌'} 价格新高 (5min high ≥ 20 根内最高)")
        lines.append(f"- {'✅' if sig['is_rsi_not_new_high'] else '❌'} RSI 顶背离 (5min RSI={sig['rsi_14']:.0f} < 20根内最高 {sig['max_rsi_20']:.0f}-3)")
        lines.append(f"- {'✅' if sig['daily_overbought'] else '❌'} 日线超买 (日RSI={sig['daily_rsi']:.0f} > 65)")
        if sig["d17_complete"]:
            lines.append("  → 🔎 D17 左侧信号完整（观察级）！不可直接入场，等待右侧确认（跌破 VWAP + 阴线收盘确认）后才可考虑 1 手试空，止损=今高+5点，止盈=VWAP/布林中线")
        else:
            missing = []
            if not sig["is_price_new_high"]: missing.append("价格未新高")
            if not sig["is_rsi_not_new_high"]: missing.append("RSI 未顶背离")
            if not sig["daily_overbought"]: missing.append("日线未超买")
            lines.append(f"  → ⚠️ 触发但缺: {'+'.join(missing)}")
        lines.append("")

        # SHORT 摸顶 D0: 新高回落 + 量比≥2
        if n5 >= 21:
            lines.append("### SHORT 左侧：D0 摸顶（5m 新高回落 + 量比 ≥ 2）")
            lines.append(f"- {'✅' if sig['is_new_high_d0'] else '❌'} 价格新高 (5min high ≥ 20 根内最高)")
            lines.append(f"- {'✅' if sig['is_pullback'] else '❌'} 立即回落 (close{closes_5[-1]:.2f} < 前根{closes_5[-2]:.2f})")
            lines.append(f"- {'✅' if sig['vol_big_d0'] else '❌'} 放量确认 (当前量比={sig['vol_ratio']:.1f}x，前根={sig['prev_vol_ratio']:.1f}x，需 ≥ 2x)")
            if sig["d0_complete"]:
                lines.append("  → 🔎 D0 左侧信号完整（观察级）！不可直接入场，等待右侧确认（跌破 VWAP + 阴线收盘确认）后才可考虑 1 手试空，止损=今高+5点，止盈=VWAP/布林中线")
            else:
                missing = []
                if not sig["is_new_high_d0"]: missing.append("价格未新高")
                if not sig["is_pullback"]: missing.append("未立即回落")
                if not sig["vol_big_d0"]: missing.append("量比不够")
                lines.append(f"  → ⚠️ 触发但缺: {'+'.join(missing)}")
            lines.append("")

        # 缺口回补
        lines.append("### 缺口回补机会")
        prev_close = sig["gap_prev_close"]
        if prev_close and prev_close > 0:
            gap_pct = sig["gap_pct"]
            cur_price = self.index_price_fn()
            if abs(gap_pct) > 1.0:
                if gap_pct > 0:
                    lines.append(f"- 📉 高开缺口 {gap_pct:+.2f}%（{cur_price:.2f} vs 昨收{prev_close:.2f}）→ SHORT 左侧机会")
                else:
                    lines.append(f"- 📈 低开缺口 {gap_pct:+.2f}%（{cur_price:.2f} vs 昨收{prev_close:.2f}）→ LONG 左侧机会")
            else:
                lines.append(f"- ❌ 无明显缺口（{gap_pct:+.2f}%，阈 1%）")
        else:
            lines.append("- ⚠️ 昨收数据不可用")
        lines.append("")

        # 左侧操作约束
        lines.append("### ⚠️ 左侧交易约束（AI 决策时必须遵守）")
        lines.append("- **核心原则：右侧（趋势跟随）为主，左侧为副。右侧信号明确时禁止使用左侧**")
        lines.append("- **8/14 硬规则：左侧信号必须先有右侧确认才能入场！**")
        lines.append("  - LONG 需：价格站上 VWAP + 阳线收盘确认（收盘>开盘 且 收盘>前根收盘）")
        lines.append("  - SHORT 需：价格跌破 VWAP + 阴线收盘确认（收盘<开盘 且 收盘<前根收盘）")
        lines.append("  - 无右侧确认 → 输出 WAIT 或设条件单等突破，绝不直接市价抄底/摸顶")
        lines.append("- 右侧信号明确 = 至少 4 个周期均线同向排列（多头或空头）= 禁止左侧")
        lines.append("- 左侧仅在右侧信号模糊（均线缠绕、多空冲突、WAIT）时才可以考虑")
        lines.append("- LONG 左侧：🐂牛市或📊震荡市可做，🐻熊市禁止抄底")
        lines.append("- SHORT 摸顶：🐻熊市/📊震荡市做 D17+D0，🐂牛市禁止（指数长期向上漂移）")
        lines.append("- 抄底 L12a 要求 5m RSI<40 + 15m RSI<45 + 量比≥1.5（双周期超卖共振）")
        lines.append("- 抄底 L3 要求 5m 底背离 + 量比≥2（精准捕捉）")
        lines.append("- L12a 持仓 4h 最佳 (累计 +8.03%)，L3 持仓 2h 最佳 (累计 +5.74% 88%胜率)")
        lines.append("- 抄底止损 = 今低 - 5 点（跌破前低就认错）")
        lines.append("- 抄底止盈 = VWAP / 布林中线 / 昨收（均值回归，不贪趋势）")
        lines.append("- 摸顶信号 D17 要求日线 RSI>65 (已超买)，D0 要求放量 ≥ 2x")
        lines.append("- 摸顶止损 = 今高 + 5 点（突破前高就认错）")
        lines.append("- 摸顶止盈 = VWAP / 布林中线 / 昨收（均值回归，不贪趋势）")
        lines.append("- 摸顶持仓时间建议 1-4 小时 (回测最优)")
        lines.append("- 左侧仓位 ≤ 常规仓位 50%（最多 1 手）")
        lines.append("- 左侧只在 9:30-14:00 触发 (避开 9:30 跳空和 14:00 后尾盘)")
        lines.append("- **8/14 提醒：多空同等重要！中证1000 目前贴水（期货低于指数），机构常借 IM 做空对冲，下跌趋势中做空与做多一样有价值。不要只做多！**")
        lines.append("")

    # ========== 告警段（真源 L1934–2044 逐行，notifycation → 注入 notifier） ==========

    def dispatch_alerts(self, sig: dict) -> None:
        """检测完整信号时单独提醒用户（节流: 每个信号 ID 5 分钟内只发一次）。"""
        if self.notifier is None:
            return
        df_5 = sig["df_5"]
        triggered_alerts = []
        if sig["l12a_complete"]:
            triggered_alerts.append(("L12a_long", "LONG 左侧 L12a 观察级（超卖共振，等右侧确认再入）"))
        if sig["l3_complete"]:
            triggered_alerts.append(("L3_long", "LONG 左侧 L3 观察级（底背离，等右侧确认再入）"))
        if sig["d17_complete"]:
            triggered_alerts.append(("D17_short", "SHORT 左侧 D17 观察级（顶背离+日线超买，等右侧确认再入）"))
        if sig["d0_complete"]:
            triggered_alerts.append(("D0_short", "SHORT 左侧 D0 观察级（新高回落+放量，等右侧确认再入）"))
        if not triggered_alerts:
            return

        now_ts = self.now_fn()
        throttle_seconds = 300

        by_name = {s.name: s for s in sig["signals"]}
        for sig_id, sig_name in triggered_alerts:
            last_alert = self._last_left_signal_alerts.get(sig_id)
            if last_alert and (now_ts - last_alert).total_seconds() < throttle_seconds:
                continue
            self._last_left_signal_alerts[sig_id] = now_ts

            try:
                cur_price = self.index_price_fn()
                direction = 'long' if sig_id.endswith("_long") else 'short'
                resistance_levels, support_levels = self.dynamic_levels_fn(df_5, cur_price, direction)

                if direction == 'long':
                    trail_table = (
                        f"  浮盈 +10 点 → 移止损到入场价 (保本)\n"
                        f"  浮盈 +20 点 → 移止损到 +8 点 (锁利)\n"
                        f"  浮盈 +40 点 → 移止损到 +20 点 (扩利)"
                    )
                    tp_method = "n50_high (50根内最高-远端阻力)"
                    sl_init_pts = 30
                    time_stop_h = 1.0
                else:
                    trail_table = (
                        f"  浮盈 +10 点 → 移止损到入场价 (保本)\n"
                        f"  浮盈 +20 点 → 移止损到 -8 点 (锁利)\n"
                        f"  浮盈 +40 点 → 移止损到 -20 点 (扩利)"
                    )
                    tp_method = "VWAP / 布林下轨 (近端支撑)"
                    sl_init_pts = 15
                    time_stop_h = 4.0

                if direction == 'long':
                    valid_tp = [r for r in resistance_levels if r > cur_price + 5]
                    tp_price = valid_tp[-1] if valid_tp else cur_price + 30
                    tp_targets = sorted([r for r in resistance_levels if r > cur_price + 5])
                    sl_init = cur_price - sl_init_pts
                    near_support = [x for x in support_levels if x < cur_price]
                    sl_tech = near_support[-1] if near_support else cur_price - 30
                    try:
                        today_low = float(df_5['low'].iloc[-48:].min()) if len(df_5) >= 48 else cur_price - 20
                        sl_pattern = today_low - 5
                    except Exception:
                        sl_pattern = sl_init
                    direction_emoji = "📈"
                    action = "LONG 多"
                else:
                    valid_tp = [s for s in support_levels if s < cur_price - 5]
                    tp_price = valid_tp[0] if valid_tp else cur_price - 30
                    tp_targets = sorted([s for s in support_levels if s < cur_price - 5], reverse=True)
                    sl_init = cur_price + sl_init_pts
                    near_resist = [x for x in resistance_levels if x > cur_price]
                    sl_tech = near_resist[-1] if near_resist else cur_price + 30
                    try:
                        today_high = float(df_5['high'].iloc[-48:].max()) if len(df_5) >= 48 else cur_price + 20
                        sl_pattern = today_high + 5
                    except Exception:
                        sl_pattern = sl_init
                    direction_emoji = "📉"
                    action = "SHORT 空"

                try:
                    bb_window = list(df_5['close'].iloc[-20:].values)
                    bb_mid = sum(bb_window) / len(bb_window)
                except Exception:
                    bb_mid = cur_price

                msg = (
                    f"🔔 左侧信号触发！\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"{direction_emoji} {action} - {sig_name}\n"
                    f"📍 当前价: {cur_price:.2f}\n"
                    f"⏰ 触发时间: {now_ts.strftime('%H:%M:%S')}\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"🛡️ 建议止损 (绝对点位):\n"
                    f"  • 初始止损: {sl_init:.2f} (入场价 {cur_price:.2f} {'-' if direction == 'long' else '+'} {sl_init_pts} 点)\n"
                    f"  • 形态止损: {sl_pattern:.2f} (今低/今高 ± 5 点)\n"
                    f"  • 关键位止损: {sl_tech:.2f} (近端支撑/阻力 - 5 点)\n"
                    f"🎯 建议止盈 (动态预测 {tp_method}):\n"
                    f"  • 第一目标 TP: {tp_price:.2f} ({abs(tp_price - cur_price):.0f} 点) - 优先止盈 50%\n"
                    f"  • 突破后可看: {' → '.join([f'{t:.0f}' for t in tp_targets[:3]])}\n"
                    f"  • 备选阻力/支撑: 阻力 [{', '.join([f'{r:.0f}' for r in resistance_levels[:4]])}] / 支撑 [{', '.join([f'{s:.0f}' for s in support_levels[:4]])}]\n"
                    f"  • (VWP/布林中轨参考: {bb_mid:.2f})\n"
                    f"📈 阶梯追踪止损 (保本后上移):\n"
                    f"{trail_table}\n"
                    f"━━━━━━━━━━━━━━━━━\n"
                    f"⏳ 时间止损: {time_stop_h:.0f} 小时 (回测最优)\n"
                    f"💰 仓位: 1 手 (左侧 ≤ 常规 50%)\n"
                    f"📊 60 天回测: {sig_name.split('(')[0].strip()} 详见 prompt\n"
                    f"⚠️ AI 可拒绝采纳 (右侧为主) - 仅作左侧参考"
                )
                self.notifier.send(msg)
                logging.info(f"左侧信号钉钉通知: {sig_id} @ {cur_price:.2f} TP={tp_price:.2f} SL={sl_init:.2f}")
                # 结构化信号回填 SL/TP 建议（LeftSideSignal 载荷细化）
                signal_obj = by_name.get(sig_id.split("_")[0])
                if signal_obj is not None:
                    signal_obj.sl_suggestion = sl_init
                    signal_obj.tp_suggestion = tp_price
                    signal_obj.created_at = now_ts
            except Exception as e_notify:
                logging.warning(f"左侧信号钉钉通知失败 ({sig_id}): {e_notify}")
