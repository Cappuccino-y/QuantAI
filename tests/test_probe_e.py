import logging

from quantai.vendor.trade_data_fetcher import IndexDataFetcher  # noqa: F401


def test_caplog_after_vendor_fetcher(caplog):
    with caplog.at_level(logging.WARNING):
        logging.warning("vendor-probe")
    assert any("vendor-probe" in r.getMessage() for r in caplog.records)
