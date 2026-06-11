"""金十快讯抓取后台循环.

封装 ``Jin10FlashFetcher``（位于 :mod:`quantai.vendor`），
提供历史回补 + 周期增量拉取 + 线程安全读取。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from .config import trading
from .vendor.jin10_news_fetcher import Jin10FlashFetcher

logger = logging.getLogger(__name__)


class NewsManager:
    """金十快讯订阅器：5 分钟轮询 + 历史回补."""

    def __init__(
        self,
        fetcher: Optional[Jin10FlashFetcher] = None,
        fetch_interval_sec: int = 300,
        previous_trading_day_resolver=None,
    ) -> None:
        self.fetcher = fetcher or Jin10FlashFetcher()
        self.fetch_interval_sec = fetch_interval_sec
        self._cache: list[dict] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._backfilled = False
        self._resolver = previous_trading_day_resolver

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="NewsManager")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._cache)

    def to_prompt_block(self) -> str:
        items = self.snapshot()
        if not items:
            return "（无重要快讯）"
        return "\n".join(
            f"- {it.get('time', '未知时间')}: {it.get('data', {}).get('content', '无内容')}"
            for it in items
        )

    def _loop(self) -> None:
        last_fetch = datetime.now()
        while not self._stop_event.is_set():
            try:
                if not self._backfilled:
                    self._backfill_history()
                    self._backfilled = True
                    last_fetch = datetime.now()

                now = datetime.now()
                start_str = last_fetch.strftime("%Y-%m-%d %H:%M:%S")
                end_str = now.strftime("%Y-%m-%d %H:%M:%S")
                news = self.fetcher.fetch_important_news(start_str, end_str)
                if news:
                    with self._lock:
                        self._cache.extend(news)
                last_fetch = now
            except Exception as exc:
                logger.warning("Jin10 news fetch failed: %s", exc)
            self._stop_event.wait(self.fetch_interval_sec)

    def _backfill_history(self) -> None:
        prev_day = self._resolve_previous_trading_day()
        start_str = prev_day.strftime("%Y-%m-%d %H:%M:%S")
        end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("Backfilling news: %s -> %s", start_str, end_str)
        try:
            news = self.fetcher.fetch_important_news(start_str, end_str)
            if news:
                with self._lock:
                    self._cache.extend(news)
        except Exception as exc:
            logger.error("Backfill failed: %s", exc)

    def _resolve_previous_trading_day(self) -> datetime:
        if self._resolver:
            try:
                return self._resolver(datetime.now())
            except Exception as exc:
                logger.warning("Custom resolver failed: %s; fallback to T-1.", exc)
        return (datetime.now() - timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )


__all__ = ["NewsManager"]
