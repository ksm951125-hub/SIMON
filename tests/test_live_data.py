"""Opt-in network checks (RUN_LIVE_TESTS=1). Never send mail or use SMTP credentials."""
import os
from datetime import date

import pytest

from config import Settings
from providers import cnbc, daum, nasdaq, naver, yahoo

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to query live public sources")]
SETTINGS = Settings(download_retries=2)


@pytest.mark.parametrize("ticker", ["FICO", "AAPL", "BRK-B"])
def test_us_yahoo_matches_nasdaq_regular_close(ticker):
    previous, session = date(2026, 9, 28), date(2026, 9, 29)
    primary = yahoo.fetch_us(ticker, previous, session, SETTINGS)
    secondary = nasdaq.fetch(ticker, previous, session, SETTINGS)
    assert primary.close.usable and primary.previous_close.usable, primary.describe()
    if secondary.close.usable:  # may be throttled (HTTP 403) after heavy use from one IP
        assert (primary.previous_close.value, primary.close.value) == pytest.approx(
            (secondary.previous_close.value, secondary.close.value), abs=0.011)


def test_cnbc_answers_with_dated_regular_quote():
    result = cnbc.fetch_batch(["FICO", "BRK-B"], date(2026, 9, 29), SETTINGS)
    assert set(result) == {"FICO", "BRK-B"}
    assert all(item.close.date is not None for item in result.values())


def test_kr_yahoo_previous_close_matches_naver_and_daum_krx_base():
    previous, session = date(2026, 9, 28), date(2026, 9, 29)
    primary = yahoo.fetch_kr("096770", previous, session, SETTINGS)
    base, _ = naver.fetch_daily("096770", previous, session, SETTINGS)
    days = daum.fetch_days("096770", previous, session, SETTINGS)
    assert primary.previous_close.value == base.previous_close.value == days.previous_close.value == 158200


def test_daum_reports_market_state():
    _, state = daum.fetch_quote("005930", date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), SETTINGS)
    assert state is not None and state.nxt_tradable is True
