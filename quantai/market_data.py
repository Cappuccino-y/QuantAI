"""市场数据层 (Tier 2).

职责：
- 主力 IM 合约识别（基于持仓量）
- 多周期 ATR + Stress Level 计算
- 中证 1000 指数最新价 + 基差信息
- 交易日历判定（缓存）
- 指数点位 ↔ 期货价格 换算
- 提供给 LLM 的技术面 prompt 块
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from .config import trading
from .models import AIData, BasisInfo
from .vendor.trade_data_fetcher import IndexDataFetcher

logger = logging.getLogger(__name__)


class TradingCalendar:
    """交易日历：缓存一次，避免每 tick 网络请求."""

    def __init__(self) -> None:
        self._cache: Optional[pd.Series] = None

    def _load(self) -> pd.Series:
        if self._cache is not None:
            return self._cache
        try:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            self._cache = pd.to_datetime(df["trade_date"])
        except Exception as exc:
            logger.warning("Load trade calendar failed: %s; fallback to weekday check.", exc)
            self._cache = pd.Series([], dtype="datetime64[ns]")
        return self._cache

    def is_trading_day(self, date: Optional[datetime] = None) -> bool:
        date = date or datetime.now()
        cache = self._load()
        if cache.empty:
            return date.weekday() < 5
        target = pd.to_datetime(date.date())
        return bool((cache == target).any())

    def previous_trading_day(self, dt: datetime, anchor_hour: int = 15) -> datetime:
        d = dt - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)


class ContractResolver:
    """主力合约识别：基于静态持仓量从候选合约中选取最大者."""

    def __init__(self, api: Any, symbol_prefix: str = "CFFEX.IM") -> None:
        self.api = api
        self.symbol_prefix = symbol_prefix

    def _format(self, year: int, month: int) -> str:
        return f"{self.symbol_prefix}{year:02d}{month:02d}"

    def candidates(self, date: Optional[datetime] = None) -> list[str]:
        date = date or datetime.now()
        year = date.year % 100
        month = date.month

        current = self._format(year, month)
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else (year + 1) % 100
        next_month_contract = self._format(next_year, next_month)

        quarter_months = [3, 6, 9, 12]
        next_q_month = next((m for m in quarter_months if m > month), 3)
        next_q_year = year if next_q_month > month else (year + 1) % 100
        next_quarter = self._format(next_q_year, next_q_month)

        idx = quarter_months.index(next_q_month)
        nq2_month = quarter_months[(idx + 1) % 4]
        nq2_year = next_q_year if nq2_month > next_q_month else (next_q_year + 1) % 100
        next_quarter2 = self._format(nq2_year, nq2_month)

        return [current, next_month_contract, next_quarter, next_quarter2]

    def dominant(self) -> str:
        candidates = self.candidates()
        logger.info("IM candidates: %s", candidates)
        max_oi = -1
        dominant = candidates[0]
        for sym in candidates:
            try:
                q = self.api.get_quote(sym)
                oi = q.open_interest
                if oi is not None and oi > 0:
                    logger.info("%s open_interest=%s", sym, oi)
                    if oi > max_oi:
                        max_oi = oi
                        dominant = sym
            except Exception as exc:
                logger.warning("Probe %s failed: %s", sym, exc)
        if max_oi == -1:
            logger.warning("No valid open_interest; default to nearest month.")
        else:
            logger.info("Dominant contract: %s (oi=%s)", dominant, max_oi)
        return dominant

    def next_dominant(self, current_symbol: str) -> str:
        code = current_symbol.split(".")[-1].replace("IM", "")
        year = 2000 + int(code[:2])
        month = int(code[2:4])
        next_year = year + 1 if month == 12 else year
        next_month = 1 if month == 12 else month + 1
        return self._format(next_year % 100, next_month)


class ATRCalculator:
    """期货 5/15/60min ATR + Stress Level."""

    def __init__(self, api: Any, symbol: str, period: int = 14) -> None:
        self.api = api
        self.symbol = symbol
        self.period = period

    def calc(self, data_length: int = 200) -> AIData:
        try:
            kline_5m = self.api.get_kline_serial(self.symbol, 5 * 60, data_length=data_length)
            kline_15m = self.api.get_kline_serial(self.symbol, 15 * 60, data_length=data_length)
            kline_60m = self.api.get_kline_serial(self.symbol, 60 * 60, data_length=data_length)
            self.api.wait_update(deadline=time.time() + 5)

            atr_5 = self._atr_from_kline(kline_5m)
            atr_15 = self._atr_from_kline(kline_15m)
            atr_60 = self._atr_from_kline(kline_60m)
            stress = atr_5 / atr_60 if atr_60 > 0 else 1.0

            data = AIData(atr_5=atr_5, atr_15=atr_15, atr_60=atr_60, stress_level=stress)
            logger.info(
                "ATR 5m=%.2f 15m=%.2f 60m=%.2f stress=%.2f",
                atr_5, atr_15, atr_60, stress,
            )
            return data
        except Exception as exc:
            logger.error("ATR calc failed: %s; return default.", exc)
            return AIData()

    def _atr_from_kline(self, df) -> float:
        if df is None or len(df) < self.period + 1:
            return 0.0
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = tr.rolling(self.period).mean().iloc[-1]
        return float(atr) if atr == atr else 0.0


class MarketDataProvider:
    """市场数据统一入口."""

    def __init__(self, api: Any, symbol: str) -> None:
        self.api = api
        self.symbol = symbol
        self.index_fetcher = IndexDataFetcher()
        self.index_name = trading.index_name
        self.index_price: float = 0.0
        self.tech_data_text: str = ""
        self.calendar = TradingCalendar()
        self.atr = ATRCalculator(api, symbol)

    @property
    def im_quote(self):
        return self.api.get_quote(self.symbol)

    def update_index_price(self) -> float:
        try:
            df = self.index_fetcher.get_kline_data(self.index_name, "5min")
            if df is not None and not df.empty:
                self.index_price = float(df.iloc[-1]["close"])
            else:
                logger.warning("Empty index kline; basis may be off.")
        except Exception as exc:
            logger.error("Update index price failed: %s", exc)
        return self.index_price

    def refresh_tech_data(
        self, periods: tuple[str, ...] = ("5min", "15min", "30min", "60min", "日线", "周线")
    ) -> str:
        try:
            self.tech_data_text = self.index_fetcher.generate_ai_prompt(
                index_name=self.index_name, periods=list(periods)
            )
            logger.info("Refreshed multi-period tech data.")
        except Exception as exc:
            logger.error("Refresh tech data failed: %s", exc)
        return self.tech_data_text

    def get_basis_info(self) -> BasisInfo:
        quote = self.im_quote
        im_price = quote.last_price
        basis = im_price - self.index_price
        basis_pct = (basis / self.index_price * 100) if self.index_price else 0.0
        expiry = self._resolve_expiry()
        days_to_expiry = (expiry - datetime.now()).days if expiry else 0
        return BasisInfo(
            index_price=self.index_price,
            im_price=im_price,
            basis=basis,
            basis_pct=basis_pct,
            days_to_expiry=days_to_expiry,
            symbol=self.symbol,
        )

    def _resolve_expiry(self) -> Optional[datetime]:
        try:
            info = self.api.get_contract_info(self.symbol)
            return datetime.fromtimestamp(info["expire_datetime"])
        except Exception:
            code = self.symbol.split(".")[-1]
            try:
                year = 2000 + int(code[2:4])
                month = int(code[4:6])
                return datetime(year, month, 15)
            except Exception:
                return None

    def index_to_future_price(self, idx_price: float) -> float:
        """指数点位 → 期货价格；按基差比例换算并圆整到最小变动价位."""
        if idx_price is None or idx_price <= 0:
            return idx_price
        tick = trading.min_price_tick
        fut_price = self.im_quote.last_price
        idx_current = self.index_price
        if idx_current <= 0 or fut_price <= 0:
            return round(idx_price / tick) * tick
        basis_rate = fut_price / idx_current
        return round(idx_price * basis_rate / tick) * tick

    def is_trading_time(self, now: Optional[datetime] = None) -> bool:
        from datetime import time as dt_time

        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return (
            (dt_time(9, 30) <= t <= dt_time(11, 30))
            or (dt_time(13, 0) <= t <= dt_time(15, 0))
            or (dt_time(21, 0) <= t <= dt_time(23, 0))
        )

    def is_near_close(self, now: Optional[datetime] = None) -> bool:
        from datetime import time as dt_time

        now = now or datetime.now()
        t = now.time()
        return (dt_time(11, 25) <= t <= dt_time(11, 30)) or (
            dt_time(14, 55) <= t <= dt_time(15, 0)
        )


__all__ = [
    "TradingCalendar",
    "ContractResolver",
    "ATRCalculator",
    "MarketDataProvider",
]
