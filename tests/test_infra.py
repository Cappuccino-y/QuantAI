"""logger + performance + news_manager 行为测试。"""
import csv
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quantai.logger import TradeLogger
from quantai.news_manager import NewsManager
from quantai.performance import PerformanceMetrics


def test_trade_logger_csv_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "trade_log.csv")
        tl = TradeLogger(log_file=path)
        tl.log("OPEN", "CFFEX.IM2609", "LONG", 1, 4000.5, ai_reason="L12a")
        tl.log("CLOSE", "CFFEX.IM2609", "LONG", 1, 4020.0, pnl=19.5, balance_after=100019.5)
        with open(path, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0][:4] == ["timestamp", "event_type", "symbol", "direction"]
        assert len(rows) == 3
        assert rows[1][1] == "OPEN" and rows[1][4] == "1" and rows[1][5] == "4000.50"
        assert rows[2][1] == "CLOSE" and rows[2][6] == "19.50"


def test_performance_summary_and_streak():
    with tempfile.TemporaryDirectory() as td:
        _isolate_perf_files(td)
        pm = PerformanceMetrics()
        now = datetime.now()
        pm.record_trade(100.0, "LONG", 1, 4000, 4050, now, now)
        pm.record_trade(-50.0, "SHORT", 1, 4100, 4075, now, now)
        pm.record_trade(30.0, "LONG", 1, 4000, 4030, now, now)
        s = pm.summary()
        assert s["trade_count"] == 3
        assert s["win_count"] == 2 and s["loss_count"] == 1
        assert abs(s["win_rate"] - 66.67) < 0.01
        assert s["current_streak"] == 1  # 最后一笔为胜


def test_performance_equity_drawdown():
    with tempfile.TemporaryDirectory() as td:
        _isolate_perf_files(td)
        pm = PerformanceMetrics()
        now = datetime.now()
        pm.update_equity(100000, now)
        pm.update_equity(105000, now)
        pm.update_equity(98000, now)   # 回撤 (105000-98000)/105000 ≈ 6.67%
        s = pm.summary()
        assert pm.peak_balance == 105000
        assert abs(s["max_drawdown"] - 6.6667) < 0.01


def test_news_manager_cache_cap_and_injection():
    """缓存上限 + 依赖注入（prev_trading_day_fn 缺省不阻塞）。"""
    with tempfile.TemporaryDirectory() as td:
        _isolate_perf_files(td)
        calls = []

        class FakeFetcher:
            def fetch_important_news(self, start_str, end_str):
                calls.append((start_str, end_str))
                return [{"title": f"news@{start_str}"}]

        nm = NewsManager(fetcher=FakeFetcher(), prev_trading_day_fn=None)
        nm._backfill_historical_news()
        nm._backfill_historical_news()
        assert len(nm.get_news()) == 2
        assert len(calls) == 2


def _isolate_perf_files(td):
    """把 performance 持久化文件隔离到临时目录，避免污染真实 data/。"""
    from quantai import config
    config.TRADES_HISTORY_FILE = os.path.join(td, "trades_history.jsonl")
    config.PERF_STATE_FILE = os.path.join(td, "performance_state.json")
    config.METRICS_FILE = os.path.join(td, "performance_metrics.csv")
