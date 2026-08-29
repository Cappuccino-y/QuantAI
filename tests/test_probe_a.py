import logging


def test_caplog_bare(caplog):
    with caplog.at_level(logging.WARNING):
        logging.warning("bare-probe")
    assert any("bare-probe" in r.getMessage() for r in caplog.records)
