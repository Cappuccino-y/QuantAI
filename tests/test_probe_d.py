import logging

import tqsdk  # noqa: F401


def test_caplog_after_tqsdk(caplog):
    with caplog.at_level(logging.WARNING):
        logging.warning("tqsdk-probe")
    assert any("tqsdk-probe" in r.getMessage() for r in caplog.records)
