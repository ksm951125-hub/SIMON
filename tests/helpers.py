"""Builders and fake sources shared by the tests (no network)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from config import Settings
from providers.base import NOT_PROVIDED, OK, REGULAR, STALE_SOURCE, UNAVAILABLE, FieldValue, PriceObservation

FIXTURES = Path(__file__).parent / "fixtures"
FAST = Settings(download_retries=1, retry_backoff_seconds=0, missing_retry_pause_seconds=0,
                availability_wait_seconds=0, nasdaq_min_interval_seconds=0, max_workers=4)


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def value(price, day, session_type=REGULAR) -> FieldValue:
    if price is None:
        return FieldValue(status=NOT_PROVIDED)
    if price == "stale":
        return FieldValue(date=day, status=STALE_SOURCE, detail=f"{day} 없음", session_type=session_type)
    if price == "down":
        return FieldValue(date=day, status=UNAVAILABLE, detail="HTTP 403", session_type=session_type)
    return FieldValue(float(price), day, OK, session_type=session_type)


def obs(symbol: str, source: str, prev, close, *, prev_date: date, day: date, close_type=REGULAR,
        note=None) -> PriceObservation:
    return PriceObservation(symbol, source, close=value(close, day, close_type),
                            previous_close=value(prev, prev_date), note=note)


def fake_us(monkeypatch, *, yahoo: dict, nasdaq: dict | None = None, cnbc: dict | None = None):
    """Each dict maps ticker -> (prev, close); values may be None/'stale'/'down'."""
    import market_data

    nasdaq = yahoo if nasdaq is None else nasdaq
    cnbc = {ticker: (None, pair[1]) for ticker, pair in yahoo.items()} if cnbc is None else cnbc

    def build(source, table):
        def fetch(ticker, previous_date, session_date, settings=None):
            prev, close = table.get(ticker, ("down", "down"))
            return obs(ticker, source, prev, close, prev_date=previous_date, day=session_date)
        return fetch

    monkeypatch.setattr(market_data.yahoo, "fetch_us", build("Yahoo", yahoo))
    monkeypatch.setattr(market_data.nasdaq, "fetch", build("Nasdaq", nasdaq))

    def batch(tickers, session_date, settings=None, previous_date=None):
        result = {}
        for ticker in tickers:
            prev, close = cnbc.get(ticker, (None, "down"))
            observation = obs(ticker, "CNBC", None, close, prev_date=previous_date or session_date, day=session_date)
            if isinstance(prev, (int, float)):
                observation.previous_close = replace(value(prev, previous_date or session_date), corroborative=True)
            result[ticker] = observation
        return result

    monkeypatch.setattr(market_data.cnbc, "fetch_batch", batch)
