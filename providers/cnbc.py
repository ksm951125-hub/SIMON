"""CNBC quote service (US tertiary verification of the analysis-day close).

``last`` is the regular-session last price; extended-hours trading is reported
separately in ``ExtendedMktQuote`` (requested with exthrs=1) and never read.
``last_time`` carries the session date of ``last``. Only the close field is used.

``previous_day_closing`` of the same-evening snapshot is the previous session
close as CNBC re-based it that morning (reduced by any ex-dividend on the
analysis day). It is returned as a corroborative-only value: it confirms the
previous close when equal, and is ignored (never a mismatch) otherwise.

Rollover: once the next session's pre-market starts, CNBC re-bases the quote for
that session - ``last`` becomes the previous close ADJUSTED for next-day
ex-dividends (observed 2026-09-30: AMT 167.35 -> 165.56, O 55.14 -> 54.87) and
``previous_day_closing == last``. Such snapshots are STALE_SOURCE; only the
same-evening post-market snapshot (the 07:30 KST run time) is accepted.

Batched: up to 100 symbols per request, no API key.
"""
from __future__ import annotations

from datetime import date

from config import SETTINGS, Settings
from net import get_with_retry
from providers.base import (INVALID_PRICE, NOT_PROVIDED, OK, STALE_SOURCE, UNAVAILABLE, FieldValue,
                            PriceObservation, failed, positive)

SOURCE = "CNBC"
URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
BATCH_SIZE = 100


def symbol_for(ticker: str) -> str:
    return ticker.strip().upper().replace("-", ".")


def parse(payload: dict, session_date: date, previous_date: date | None = None) -> dict[str, PriceObservation]:
    quotes = ((payload or {}).get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
    if isinstance(quotes, dict):
        quotes = [quotes]
    observations: dict[str, PriceObservation] = {}
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").upper()
        if not symbol:
            continue
        raw_date = str(quote.get("last_time") or "")[:10]
        try:
            quote_date = date.fromisoformat(raw_date)
        except ValueError:
            observations[symbol] = failed(symbol, SOURCE, f"CNBC 날짜 해석 불가 ({raw_date!r})", INVALID_PRICE)
            continue
        value = positive(quote.get("last"))
        extended_type = str((quote.get("ExtendedMktQuote") or {}).get("type") or "")
        rolled = (quote.get("curmktstatus") == "PRE_MKT" or extended_type.endswith("_PREV")
                  or (value is not None and positive(quote.get("previous_day_closing")) == value))
        if quote_date == session_date and rolled:
            close = FieldValue(date=quote_date, status=STALE_SOURCE,
                               detail="익일 세션으로 전환된 스냅샷(배당락 조정 가능)")
        elif quote_date != session_date:
            close = FieldValue(date=quote_date, status=STALE_SOURCE, detail=f"{session_date} 아님 ({quote_date})")
        elif value is None:
            close = FieldValue(date=quote_date, status=INVALID_PRICE, detail="last 비정상")
        else:
            close = FieldValue(value, quote_date, OK)
        previous = FieldValue(status=NOT_PROVIDED)
        previous_value = positive(quote.get("previous_day_closing"))
        if close.status == OK and previous_date is not None and previous_value is not None:
            previous = FieldValue(previous_value, previous_date, OK, corroborative=True)
        observations[symbol] = PriceObservation(symbol, SOURCE, close=close, previous_close=previous)
    return observations


def fetch_batch(tickers: list[str], session_date: date, settings: Settings = SETTINGS,
                previous_date: date | None = None) -> dict[str, PriceObservation]:
    """Return an observation for every requested ticker (keyed by the input ticker)."""
    results: dict[str, PriceObservation] = {}
    for start in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[start:start + BATCH_SIZE]
        symbols = {symbol_for(ticker): ticker for ticker in chunk}
        try:
            response = get_with_retry(URL, params={"symbols": "|".join(symbols), "requestMethod": "itv", "noform": "1",
                                                   "partnerId": "2", "fund": "1", "exthrs": "1", "output": "json"},
                                      headers=HEADERS, settings=settings)
            parsed = parse(response.json(), session_date, previous_date)
        except Exception as exc:
            parsed = {}
            error = f"CNBC 조회 실패: {exc}"
        else:
            error = "CNBC 응답에 종목 없음"
        for symbol, ticker in symbols.items():
            observation = parsed.get(symbol) or failed(ticker, SOURCE, error, UNAVAILABLE)
            observation.symbol = ticker
            if observation.previous_close.status != OK:
                observation.previous_close = FieldValue(status=NOT_PROVIDED)
            results[ticker] = observation
    return results
