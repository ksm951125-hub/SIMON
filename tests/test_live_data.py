"""Opt-in network checks. These never send mail or use SMTP credentials."""
import json
import os
from pathlib import Path

import pytest

from config import Settings
from detector import calculate_change_pct
from kospi import _price_rows, _chart_price_pair, _download_price_pair, get_kospi_session_context
from market_calendar import get_session_context
from market_data import _download_chart_ticker, _download_batch, _ticker_frame

pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to query live public sources")]


def save(name, payload):
    path = Path("output/audit/live")
    path.mkdir(parents=True, exist_ok=True)
    (path / (name + ".json")).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "MGM"])
def test_us_regular_daily_close_matches_unadjusted_history(ticker):
    from datetime import timedelta
    import yfinance as yf
    cache = Path("output/audit/yf-cache")
    cache.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(cache))
    context = get_session_context()
    previous, session = context.previous_session_date, context.session_date
    values = _download_chart_ticker(ticker, previous, session, Settings(download_retries=1))
    data = _ticker_frame(_download_batch([ticker], previous, session+timedelta(days=1), Settings(download_retries=1), threads=False), ticker, 1)
    data.index = data.index.date
    cross = [round(float(data.loc[day, "Close"]),2) for day in [previous,session]]
    pair = [values[day] for day in [previous,session]]
    save(ticker,{"previous_session":previous,"session":session,"program_close":pair,"yfinance_auto_adjust_false_close":cross,"change_pct":calculate_change_pct(*pair),"sources_independent":False,"includePrePost":False})
    assert pair == pytest.approx(cross, abs=0.005)


@pytest.mark.parametrize("code", ["005930", "000660", "005380"])
def test_kr_daily_close_matches_separate_dated_chart(code):
    context = get_kospi_session_context()
    previous, session = context.previous_session_date, context.session_date
    pair = _download_price_pair(code, session, previous, context.price_page)
    cross = _chart_price_pair(code, previous, session)
    rows = _price_rows(code,1)
    quoted = next(row.get("fluctuationsRatio") for row in rows if row["localTradedAt"] == str(session))
    save(code,{"previous_session":previous,"session":session,"program_close":pair,"naver_chart_close":cross,"change_pct":calculate_change_pct(*pair),"naver_displayed_pct":quoted,"sources_independent":False})
    assert pair == pytest.approx(cross,abs=0.01)
