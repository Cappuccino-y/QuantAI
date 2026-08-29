import logging

import quantai.system as sys_mod  # noqa: F401


def test_caplog_after_system(caplog):
    with caplog.at_level(logging.WARNING):
        logging.warning("system-probe")
    assert any("system-probe" in r.getMessage() for r in caplog.records)
