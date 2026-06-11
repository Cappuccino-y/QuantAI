"""日韩联动分析模块.

策略亮点：
- 早盘前根据日经 9:00 涨跌幅判断市场氛围
- 11:30-12:50 KOSPI 振幅 + 方向触发 12:50 顺势单（基于 7 天回测最优参数）
- 14:00 强制平仓守护

调用方需通过依赖注入提供 ``index_fetcher`` 以拉取亚洲指数 5min K 线。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class LunchContext:
    nk225_9am_pct: Optional[float] = None
    nk225_1130_pct: Optional[float] = None
    nk225_1230_pct: Optional[float] = None
    topix_1230_pct: Optional[float] = None
    topix_9am_pct: Optional[float] = None
    kospi_1230_pct: Optional[float] = None
    kospi_1230_max_move: Optional[float] = None
    kospi_1230_delta: Optional[float] = None
    nk225_1230_max_move: Optional[float] = None
    index_call_auction: Optional[float] = None
    index_last_close: Optional[float] = None
    update_time: Optional[str] = None
    extras: dict = field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        if hasattr(self, key):
            setattr(self, key, value)
        else:
            self.extras[key] = value
        self.update_time = datetime.now().strftime("%H:%M:%S")


@dataclass
class LunchBreakoutState:
    triggered: bool = False
    direction: Optional[str] = None
    entry_price: Optional[float] = None
    force_close_deadline: Optional[datetime] = None
    trigger_time: Optional[str] = None


class JapanKoreaAnalyzer:
    """日韩指数联动分析器；带 60s 内存缓存."""

    CACHE_TTL_SEC = 60

    def __init__(self, index_fetcher: Any) -> None:
        self.fetcher = index_fetcher
        self._cache: Optional[dict] = None
        self._cache_ts: float = 0.0
        self.lunch_context = LunchContext()
        self.lunch_breakout = LunchBreakoutState()

    def fetch_jp_indices(self) -> Optional[dict]:
        """拉取日经 N225 + KOSPI 实时 5min K 线，带 60s 缓存."""
        now_ts = time.time()
        if self._cache and now_ts - self._cache_ts < self.CACHE_TTL_SEC:
            return self._cache
        try:
            raw = self.fetcher.get_asian_indices_5min_bars()
            indices = raw.get("indices", {})
            nk = indices.get("nikkei225", {})
            kospi = indices.get("kospi", {})
            nk_bars = nk.get("5min_bars", [])
            kospi_bars = kospi.get("5min_bars", [])
            data = {
                "nk225_now": nk_bars[-1]["close"] if nk_bars else None,
                "nk225_pct": nk_bars[-1]["change_pct_from_prev_close"] if nk_bars else None,
                "nk225_prev_close": nk.get("prev_close"),
                "kospi_now": kospi_bars[-1]["close"] if kospi_bars else None,
                "kospi_pct": kospi_bars[-1]["change_pct_from_prev_close"] if kospi_bars else None,
                "kospi_prev_close": kospi.get("prev_close"),
                "nk225_5min": nk_bars,
                "kospi_5min": kospi_bars,
                "ts": raw.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            }
            self._cache = data
            self._cache_ts = now_ts
            return data
        except Exception as exc:
            logger.error("Fetch JP indices failed: %s", exc)
            return None

    def calc_nk225_max_move_in_window(self, start_hm: str, end_hm: str) -> Optional[float]:
        jp = self.fetch_jp_indices()
        if not jp or not jp.get("nk225_5min") or not jp.get("nk225_prev_close"):
            return None
        prev_close = jp["nk225_prev_close"]
        max_high = None
        min_low = None
        for bar in jp["nk225_5min"]:
            t = bar.get("time", "")
            if len(t) < 16:
                continue
            hm = t[11:16]
            if hm < start_hm or hm > end_hm:
                continue
            h = bar.get("high")
            low = bar.get("low")
            if h is None or low is None:
                continue
            max_high = h if max_high is None else max(max_high, h)
            min_low = low if min_low is None else min(min_low, low)
        if max_high is None or min_low is None:
            return None
        return round((max_high - min_low) / prev_close * 100, 2)

    def calc_kospi_amp_delta_in_window(
        self, start_hm: str, end_hm: str
    ) -> Optional[dict[str, float]]:
        jp = self.fetch_jp_indices()
        if not jp or not jp.get("kospi_5min") or not jp.get("kospi_prev_close"):
            return None
        max_high = min_low = first_open = last_close = None
        for bar in jp["kospi_5min"]:
            t = bar.get("time", "")
            if len(t) < 16:
                continue
            hm = t[11:16]
            if hm < start_hm or hm > end_hm:
                continue
            h, low, o, c = bar.get("high"), bar.get("low"), bar.get("open"), bar.get("close")
            if h is None or low is None:
                continue
            max_high = h if max_high is None else max(max_high, h)
            min_low = low if min_low is None else min(min_low, low)
            if first_open is None and o is not None:
                first_open = o
            if c is not None:
                last_close = c
        if None in (max_high, min_low, first_open, last_close):
            return None
        amp = (max_high - min_low) / first_open * 100
        delta = (last_close - first_open) / first_open * 100
        return {
            "amp": round(amp, 2),
            "delta": round(delta, 2),
            "max_high": max_high,
            "min_low": min_low,
            "first_open": first_open,
            "last_close": last_close,
        }


__all__ = ["LunchContext", "LunchBreakoutState", "JapanKoreaAnalyzer"]
