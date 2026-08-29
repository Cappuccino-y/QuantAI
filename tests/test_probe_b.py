import logging

from quantai.performance import PerformanceMetrics  # noqa: F401


def test_caplog_after_performance(caplog):
    with caplog.at_level(logging.WARNING):
        logging.warning("perf-probe")
    assert any("perf-probe" in r.getMessage() for r in caplog.records)
