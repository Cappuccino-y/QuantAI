"""config 常量与真源逐项对拍测试。

真源: D:/PythonProject/MainToy/trade/autotrade_fix.py L27–157
验收标准（design.md §三.4）: 所有阈值原样迁移，作为行为等价 checklist 的一部分。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quantai import config


def test_risk_constants_match_source():
    """P0/P1 风控常量逐项对拍（真源 L107–143）。"""
    assert config.BASE_DECISION_INTERVAL == 900
    assert config.SHORT_TERM_INTERVAL == 300
    assert config.MIN_DECISION_INTERVAL == 300
    assert config.MAX_DECISION_INTERVAL == 1200
    assert config.SCALPING_ATR_RATIO == 1.3
    assert config.BREAKOUT_THRESHOLD == 0.3
    assert config.STOP_ADJUST_COOLDOWN == 300
    assert config.STOP_RELAX_REQUIRED_CONFIDENCE == 0.75
    assert config.MIN_STOP_DISTANCE_ATR_MULT == 0.8
    assert config.MIN_STOP_DISTANCE_ATR_MULT_COND == 0.6
    assert config.ADD_REQUIRED_CONFIDENCE == 0.70
    assert config.ADD_MIN_PRICE_GAP_ATR == 1.0
    assert config.ADD_MAX_DRAWDOWN_PCT == 1.5
    assert config.MAX_POSITION_LOTS == 3
    assert config.STOPOUT_COOLDOWN_SEC == 900
    assert config.EMERGENCY_AUTO_RESET_SEC == 1800
    assert config.MAX_RISK_PCT == 0.01
    assert config.MAX_STOP_DISTANCE_ATR_MULT == 3.0
    assert config.MAX_ROUND_TRIPS_PER_DAY == 6
    assert config.DAILY_LOSS_WARN_RATIO == 0.6


def test_notify_constants_match_source():
    """通知常量对拍（真源 L58–69, L38）。"""
    assert config.NOTIFY_RATE_LIMIT == 10
    assert config.NOTIFY_DEDUP_WINDOW == 300
    assert config.NEWS_CACHE_MAX == 200
    assert config.NOTIFY_DEDUP_TABLE_MAX == 200
    for kw in ("平仓成功", "开仓成功", "条件单入场", "成交:", "熔断", "失败",
               "紧急", "请手动", "手动处理", "止损触发", "过期条件单", "重连"):
        assert kw in config.NOTIFY_CRITICAL_KEYWORDS


def test_min_confidence():
    assert config.MIN_CONFIDENCE == 0.55


def test_no_hardcoded_credentials():
    """账密必须来自 .env，源码中不得出现硬编码（真源 L99-100 的修复项）。"""
    src = open(config.__file__, encoding="utf-8").read()
    assert '"lyy121200"' not in src
    assert '"Lyy121200@"' not in src


def test_paths_under_data_dir():
    assert config.POSITION_FILE.endswith("position_state.pkl")
    assert config.LOG_FILE.endswith("trading.log")
    assert config.TRADE_LOG_FILE.endswith("trade_log.csv")
    assert config.METRICS_FILE.endswith("performance_metrics.csv")
    assert config.AI_DECISIONS_FILE.endswith("ai_decisions.jsonl")
    assert config.TRADES_HISTORY_FILE.endswith("trades_history.jsonl")
    assert config.CIRCUIT_BREAKER_FILE.endswith("circuit_breaker_state.json")
    assert config.PERF_STATE_FILE.endswith("performance_state.json")
    for p in (config.POSITION_FILE, config.LOG_FILE, config.TRADE_LOG_FILE,
              config.METRICS_FILE, config.AI_DECISIONS_FILE,
              config.TRADES_HISTORY_FILE, config.CIRCUIT_BREAKER_FILE,
              config.PERF_STATE_FILE):
        assert os.path.dirname(p) == config.DATA_DIR
