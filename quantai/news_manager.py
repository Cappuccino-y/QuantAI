"""news_manager — Jin10 新闻后台抓取（真源 L907–947 类化迁移）。

行为保持:
- 后台线程每 300s 抓取一次增量新闻
- 首次运行先回补上一交易日 15:00 以来的历史新闻
- 缓存上限 NEWS_CACHE_MAX(200)，增量与回补均遵守（修复 M3）

与原版差异（依赖注入，解耦 market_data）:
- 原版 _backfill_historical_news 直接调 self._get_previous_trading_day_15；
  本版通过 prev_trading_day_fn 注入（phase 2 由 TradingCalendar 提供），
  未注入时回补退化为"从现在开始"，不阻塞骨架期使用。
"""
import logging
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from . import config


class NewsManager:
    def __init__(self,
                 fetcher=None,
                 prev_trading_day_fn: Optional[Callable[[datetime], datetime]] = None):
        if fetcher is None:
            from .vendor.jin10_news_fetcher import Jin10FlashFetcher
            fetcher = Jin10FlashFetcher()
        self.fetcher = fetcher
        self.prev_trading_day_fn = prev_trading_day_fn
        self.news_cache: List[dict] = []
        self.news_lock = threading.Lock()
        self.news_thread_running = False
        self.history_backfilled = False
        self._thread: Optional[threading.Thread] = None

    # ---------- 线程生命周期 ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.news_thread_running = True
        self._thread = threading.Thread(target=self._news_fetcher_loop, daemon=True)
        self._thread.start()
        logging.info("新闻抓取线程已启动")

    def stop(self) -> None:
        self.news_thread_running = False

    # ---------- 真源 L907–931 ----------
    def _news_fetcher_loop(self):
        last_fetch = datetime.now()
        while self.news_thread_running:
            # 首次运行时进行历史回补
            if not self.history_backfilled:
                self._backfill_historical_news()
                self.history_backfilled = True
                # 回补完成后，将 last_fetch 设为当前，避免重复抓取
                last_fetch = datetime.now()

            now = datetime.now()
            start_str = last_fetch.strftime("%Y-%m-%d %H:%M:%S")
            end_str = now.strftime("%Y-%m-%d %H:%M:%S")
            try:
                new_news = self.fetcher.fetch_important_news(start_str, end_str)
                if new_news:
                    with self.news_lock:
                        self.news_cache.extend(new_news)
                        # 修复 M3: 缓存上限，防止长时间运行无限膨胀
                        if len(self.news_cache) > config.NEWS_CACHE_MAX:
                            self.news_cache = self.news_cache[-config.NEWS_CACHE_MAX:]
            except Exception as e:
                logging.warning(f"新闻抓取失败: {e}")
            last_fetch = now
            time.sleep(300)  # 5分钟抓取一次

    # ---------- 真源 L933–947 ----------
    def _backfill_historical_news(self):
        if self.prev_trading_day_fn is not None:
            prev_trading_day = self.prev_trading_day_fn(datetime.now())
        else:
            # 注入缺省: 从当前时刻开始（骨架期可用；phase 2 接 TradingCalendar 后恢复原行为）
            prev_trading_day = datetime.now()
            logging.warning("prev_trading_day_fn 未注入，历史新闻回补退化为从当前时刻开始")
        start_str = prev_trading_day.strftime("%Y-%m-%d %H:%M:%S")
        end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"回补历史新闻: {start_str} → {end_str}")
        try:
            news = self.fetcher.fetch_important_news(start_str, end_str)
            if news:
                with self.news_lock:
                    self.news_cache.extend(news)
                    # 修复 M3: 回补也遵守缓存上限
                    if len(self.news_cache) > config.NEWS_CACHE_MAX:
                        self.news_cache = self.news_cache[-config.NEWS_CACHE_MAX:]
        except Exception as e:
            logging.error(f"历史新闻回补失败: {e}")

    # ---------- 读取接口 ----------
    def get_news(self) -> List[dict]:
        """线程安全读取当前新闻缓存快照。"""
        with self.news_lock:
            return list(self.news_cache)
