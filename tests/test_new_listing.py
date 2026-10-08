"""Regression: a stock listed on the analysis day has no previous close.

Real payloads (captured 2026-10-09): VYLR (Vylor Inc., a spin-off) first traded
on 2026-10-01 at $68.26. The 2026-10-01 replay used to report "데이터 누락 US 1건"
(DATA WARNING) for it: Yahoo/Nasdaq/CNBC simply have no 2026-09-30 close. The
exchange data says why (Yahoo meta.firstTradeDate), so it is an INFO line, not a
failure. No rule is keyed on this symbol."""
from datetime import date

import copy

import pandas as pd
import pytest

import main
import market_data
from helpers import FAST, fixture
from market_calendar import SessionContext
from market_data import download_market_data, new_listing
from market_result import INFO, WARNING
from providers import nasdaq, yahoo
from providers.base import OK, STALE_SOURCE, FieldValue, PriceObservation
from report import build_combined_subject, build_html_report

PREV, DAY = date(2026, 9, 30), date(2026, 10, 1)
FILLERS = [f"F{i:03d}" for i in range(40)]


class Response:
    def __init__(self, payload):
        self.payload, self.status_code = payload, 200

    def json(self):
        return self.payload


def test_yahoo_reports_the_first_trade_date():
    payload = fixture("yahoo-chart-VYLR-2026-10-01.json")
    assert yahoo.first_trade_date(payload, yahoo.NEW_YORK) == date(2026, 10, 1)


@pytest.mark.parametrize("meta", [{}, {"firstTradeDate": None}, {"firstTradeDate": "x"}, {"firstTradeDate": True},
                                  {"firstTradeDate": 10 ** 18}, {"firstTradeDate": float("nan")}])
def test_first_trade_date_is_optional_and_robust(meta):
    assert yahoo.first_trade_date({"chart": {"result": [{"meta": meta}]}}, yahoo.NEW_YORK) is None
    assert yahoo.first_trade_date({}, yahoo.NEW_YORK) is None
    assert yahoo.first_trade_date(None, yahoo.NEW_YORK) is None


def test_old_listings_carry_negative_epochs():
    # e.g. a 1962 listing: -252322200 -> 1962-01-02 (must not raise on any platform)
    payload = {"chart": {"result": [{"meta": {"firstTradeDate": -252322200}}]}}
    assert yahoo.first_trade_date(payload, yahoo.NEW_YORK) == date(1962, 1, 2)


def vylr_observations(monkeypatch, previous=PREV, day=DAY):
    """The real Yahoo adapter and Nasdaq adapter fed with the captured payloads."""
    monkeypatch.setattr(yahoo, "_fetch", lambda *a, **k: (fixture("yahoo-chart-VYLR-2026-10-01.json"), None))
    monkeypatch.setattr(nasdaq, "get_with_retry", lambda *a, **k: Response(fixture("nasdaq-VYLR-2026-10-01.json")))
    return yahoo.fetch_us("VYLR", previous, day, FAST), nasdaq.fetch("VYLR", previous, day, FAST)


def test_new_listing_is_recognised_only_with_exchange_evidence(monkeypatch):
    primary, secondary = vylr_observations(monkeypatch)
    assert primary.first_trade_date == DAY and not primary.previous_close.usable and primary.close.usable
    assert new_listing([primary, secondary], PREV, DAY) == DAY
    # No evidence from the exchange -> an ordinary data gap, never a new listing.
    primary.first_trade_date = None
    assert new_listing([primary, secondary], PREV, DAY) is None
    # An old listing (first trade long ago) that merely lacks yesterday's bar stays a data gap.
    primary.first_trade_date = date(1999, 5, 4)
    assert new_listing([primary, secondary], PREV, DAY) is None
    # Any source that does have a regular previous close means there is something to compare.
    primary.first_trade_date = DAY
    secondary.previous_close = FieldValue(70.0, PREV, OK)
    assert new_listing([primary, secondary], PREV, DAY) is None
    # A supporting-only reference price (CNBC) is not a previous close.
    secondary.previous_close = FieldValue(66.0, PREV, OK, corroborative=True)
    assert new_listing([primary, secondary], PREV, DAY) == DAY


def test_second_day_is_analysed_like_any_other_stock(monkeypatch):
    primary, secondary = vylr_observations(monkeypatch, previous=date(2026, 10, 1), day=date(2026, 10, 2))
    assert (primary.previous_close.value, primary.close.value) == (pytest.approx(68.26, abs=0.005),
                                                                   pytest.approx(67.26, abs=0.005))
    assert new_listing([primary, secondary], date(2026, 10, 1), date(2026, 10, 2)) is None


def universe(extra=("VYLR",), fillers=FILLERS):
    tickers = ["AAPL", *extra, *fillers]
    return pd.DataFrame({"ticker": tickers, "yahoo_ticker": tickers, "company_name": [f"{t} Inc." for t in tickers],
                         "sector": "X"})


def chart_for(symbol):
    payload = copy.deepcopy(fixture("yahoo-chart-VYLR-2026-10-01.json"))
    payload["chart"]["result"][0]["meta"]["symbol"] = symbol
    return payload


def wire(monkeypatch, tickers_new=("VYLR",), cnbc_previous=None):
    real_yahoo = yahoo.fetch_us
    monkeypatch.setattr(yahoo, "_fetch", lambda symbol, *a, **k: (chart_for(symbol), None))
    monkeypatch.setattr(nasdaq, "get_with_retry", lambda *a, **k: Response(fixture("nasdaq-VYLR-2026-10-01.json")))
    real_nasdaq = nasdaq.fetch

    def plain(ticker, source):
        return PriceObservation(ticker, source, close=FieldValue(250.0, DAY, OK),
                                previous_close=FieldValue(251.0, PREV, OK))

    def yahoo_fetch(ticker, previous, current, settings=None):
        if ticker in tickers_new:
            observation = real_yahoo(ticker, previous, current, FAST)
            observation.symbol = ticker
            return observation
        return plain(ticker, "Yahoo")

    def nasdaq_fetch(ticker, previous, current, settings=None):
        return real_nasdaq(ticker, previous, current, FAST) if ticker in tickers_new else plain(ticker, "Nasdaq")

    def cnbc_batch(tickers, current, settings=None, previous=None):
        out = {}
        for ticker in tickers:
            if ticker in tickers_new:
                # Either CNBC does not know yesterday, or it quotes a reference price (supporting-only).
                previous = (FieldValue(cnbc_previous, PREV, OK, corroborative=True) if cnbc_previous
                            else FieldValue(date=PREV, status=STALE_SOURCE, detail="전일 종가 없음"))
                out[ticker] = PriceObservation(ticker, "CNBC", close=FieldValue(68.26, DAY, OK), previous_close=previous)
            else:
                out[ticker] = PriceObservation(ticker, "CNBC", close=FieldValue(250.0, DAY, OK),
                                               previous_close=FieldValue(251.0, PREV, OK, corroborative=True))
        return out

    monkeypatch.setattr(market_data.yahoo, "fetch_us", yahoo_fetch)
    monkeypatch.setattr(market_data.nasdaq, "fetch", nasdaq_fetch)
    monkeypatch.setattr(market_data.cnbc, "fetch_batch", cnbc_batch)
    monkeypatch.setattr(market_data.yahoo, "find_successor_symbols", lambda *a, **k: [])


@pytest.mark.parametrize("cnbc_previous", [None, 66.0])
def test_collection_separates_the_new_listing(monkeypatch, cnbc_previous):
    wire(monkeypatch, cnbc_previous=cnbc_previous)
    result = download_market_data(universe(), DAY, PREV, FAST)
    assert set(result.new_listings) == {"VYLR"} and "2026-10-01" in result.new_listings["VYLR"]
    assert not result.missing and not result.excluded
    assert "VYLR" not in set(result.prices["ticker"]) and len(result.prices) == len(FILLERS) + 1


def run_monitor(monkeypatch, frame=None):
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    monkeypatch.setattr("market_data.time.sleep", lambda *_: None)
    monkeypatch.setattr(main, "get_session_context", lambda: SessionContext(DAY, DAY, PREV, False))
    monkeypatch.setattr(main, "load_constituents", lambda: universe() if frame is None else frame)
    monkeypatch.setattr(main, "fetch_news", lambda *a, **k: {"cause": "", "items": [], "error": None})
    return main.run_us_monitor(None)


def test_mail_shows_info_not_data_warning(monkeypatch):
    wire(monkeypatch)
    result = run_monitor(monkeypatch)
    assert result.status == INFO and not result.data_warning_issues and not result.missing
    assert result.total_count == len(FILLERS) + 1 == result.analyzed_count and result.coverage == 1.0
    assert [(issue.level, issue.category, issue.symbol) for issue in result.info_issues] == [
        (INFO, "NEW_LISTING", "VYLR")]
    kr = main._skipped_result("KR")
    assert build_combined_subject(result, kr) == "[급락 모니터] S&P500 0개 · KOSPI 제외"
    html = build_html_report(result, kr, pd.Timestamp("2026-10-02 07:30", tz="Asia/Seoul").to_pydatetime())
    assert "DATA WARNING" not in html and "신규 상장" in html and "VYLR" in html


def test_a_flood_of_new_listings_is_not_silently_hidden(monkeypatch):
    names = tuple(f"N{i:02d}" for i in range(10))
    large = [f"G{i:03d}" for i in range(190)]  # 10 of 201 symbols missing: still above the 90% coverage floor
    wire(monkeypatch, tickers_new=names)
    result = download_market_data(universe(extra=names, fillers=large), DAY, PREV, FAST)
    assert not result.new_listings and set(result.missing) == set(names)
    assert all("신규 상장 판정 과다" in reason for reason in result.missing.values())
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    monkeypatch.setattr(main, "get_session_context", lambda: SessionContext(DAY, DAY, PREV, False))
    monkeypatch.setattr(main, "load_constituents", lambda: universe(extra=names, fillers=large))
    monkeypatch.setattr(main, "fetch_news", lambda *a, **k: {"cause": "", "items": [], "error": None})
    flooded = main.run_us_monitor(None)
    assert flooded.status == WARNING and len(flooded.data_warning_issues) == len(names)
