"""Opt-in network checks (RUN_LIVE_TESTS=1). Never send mail or use SMTP credentials."""
import os
from datetime import date

import pytest

from config import Settings
from kospi import fetch_naver_days, fetch_yahoo_kr_pair
from market_data import fetch_nasdaq_pair, fetch_yahoo_pair

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to query live public sources")]
SETTINGS = Settings(download_retries=2)


@pytest.mark.parametrize("ticker", ["BE", "AAPL", "BRK-B"])
def test_us_2026_09_28_yahoo_matches_nasdaq_official_close(ticker):
    previous, session = date(2026, 9, 25), date(2026, 9, 28)
    yahoo = fetch_yahoo_pair(ticker, previous, session, SETTINGS)
    nasdaq = fetch_nasdaq_pair(ticker, previous, session, SETTINGS)
    assert yahoo.ok and nasdaq.ok, (yahoo.error, nasdaq.error)
    assert (yahoo.previous_close, yahoo.close) == pytest.approx((nasdaq.previous_close, nasdaq.close), abs=0.011)


def test_kr_yahoo_close_matches_naver_krx_base_price():
    # SK이노베이션: KRX 2026-09-28 close 158,200 (Naver integrated/NXT last 158,900).
    pair = fetch_yahoo_kr_pair("096770", date(2026, 9, 28), date(2026, 9, 29), SETTINGS)
    days = fetch_naver_days("096770", SETTINGS, page_size=10)
    assert pair.ok, pair.error
    assert days[date(2026, 9, 29)].krx_base_price == pair.previous_close == 158200
