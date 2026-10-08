"""Regression: 2026-10-08 07:38 KST mail (analysis date 2026-10-07).

The constituent list (Wikipedia) still had WBD and PSKY:
- WBD: acquisition completed, last trade 2026-10-05 -> no trade on 10-06/10-07.
- PSKY: renamed Skydance Corporation, new ticker SKYD (NYSE); PSKY vanished from
  Nasdaq/CNBC while its 10-07 close (8.89, -6.7%) existed under SKYD.
- CNBC now maps "WBD" to Webuild SpA (Milan, EUR): another market's company.
Payloads are real captures; the run-time Yahoo PSKY state (bars to 10-06) is
reconstructed from the production log. No rule is keyed on these symbols.
"""
from datetime import date

import pandas as pd
import pytest

import main
import market_data
from helpers import FAST, fixture
from market_calendar import SessionContext
from market_data import download_market_data, listing_gap
from market_result import INFO, NORMAL, WARNING
from providers import cnbc, nasdaq, yahoo
from providers.base import OK, STALE_SOURCE, FieldValue, PriceObservation
from report import build_combined_subject, build_html_report

PREV, DAY = date(2026, 10, 6), date(2026, 10, 7)


class Response:
    def __init__(self, payload):
        self.payload, self.status_code = payload, 200

    def json(self):
        return self.payload


def test_nasdaq_reports_unknown_symbol():
    with pytest.raises(nasdaq.SymbolUnknown):
        nasdaq.parse(fixture("nasdaq-PSKY-2026-10-07.json"))


def test_cnbc_rejects_unknown_and_foreign_symbols():
    parsed = cnbc.parse(fixture("cnbc-PSKY-WBD-2026-10-08.json"), DAY, PREV)
    assert parsed["PSKY"].symbol_unknown and not parsed["PSKY"].close.usable
    webuild = parsed["WBD"]  # Webuild SpA, IT/EUR - must never price Warner Bros. Discovery
    assert webuild.symbol_unknown and not webuild.close.usable and "IT" in webuild.close.detail


def search(monkeypatch):
    payloads = {"Paramount Skydance Corporation": "yahoo-search-paramount-skydance.json",
                "Warner Bros. Discovery": "yahoo-search-warner-bros-discovery.json"}
    monkeypatch.setattr(yahoo, "get_with_retry",
                        lambda url, params=None, **kw: Response(fixture(payloads[params["q"]]) if params["q"] in payloads
                                                                else {"quotes": []}))


def test_successor_found_by_company_name(monkeypatch):
    search(monkeypatch)
    assert yahoo.find_successor_symbols("PSKY", "Paramount Skydance Corporation", set(), FAST) == ["SKYD"]
    assert yahoo.find_successor_symbols("WBD", "Warner Bros. Discovery", set(), FAST) == []
    assert yahoo.company_names_match("Alphabet Inc. (Class A)", "Alphabet Inc.")
    assert not yahoo.company_names_match("Apple Inc.", "Applied Materials")


def yahoo_obs(name, symbol, previous=PREV, day=DAY):
    payload = fixture(name)
    observation = yahoo.observation_from_bars(symbol, yahoo.parse_us_chart(payload, symbol), previous, day)
    observation.latest_date = max(filter(None, [observation.latest_date, yahoo.last_trade_date(payload, yahoo.NEW_YORK)]))
    return observation


def psky_at_run_time():
    # Production log: "Yahoo 지연(2026-10-07 없음, 최신 2026-10-06)".
    return PriceObservation("PSKY", "Yahoo", close=FieldValue(date=DAY, status=STALE_SOURCE, detail="2026-10-07 없음"),
                            previous_close=FieldValue(9.53, PREV, OK), latest_date=PREV, adjustment="split")


SKYD_SAME_EVENING = {"FormattedQuoteResult": {"FormattedQuote": [{
    "symbol": "SKYD", "code": 0, "countryCode": "US", "currencyCode": "USD", "last": "8.89",
    "last_time": "2026-10-07", "previous_day_closing": "9.53", "curmktstatus": "POST_MKT",
    "ExtendedMktQuote": {"type": "POST_MKT"}}]}}


def wire_sources(monkeypatch, successors=True):
    real_cnbc = cnbc.parse(fixture("cnbc-PSKY-WBD-2026-10-08.json"), DAY, PREV)
    yahoo_table = {"WBD": yahoo_obs("yahoo-chart-WBD-2026-10-07.json", "WBD"), "PSKY": psky_at_run_time(),
                   "SKYD": yahoo_obs("yahoo-chart-SKYD-2026-10-07.json", "SKYD"),
                   **{t: PriceObservation(t, "Yahoo", close=FieldValue(250.0, DAY, OK),
                                          previous_close=FieldValue(251.0, PREV, OK)) for t in ["AAPL"] + FILLERS}}
    nasdaq_table = {"PSKY": "nasdaq-PSKY-2026-10-07.json", "WBD": "nasdaq-PSKY-2026-10-07.json",
                    "SKYD": "nasdaq-SKYD-2026-10-07.json"}

    def nasdaq_fetch(ticker, previous, current, settings=None):
        if ticker == "AAPL" or ticker in FILLERS:
            return PriceObservation(ticker, "Nasdaq", close=FieldValue(250.0, DAY, OK),
                                    previous_close=FieldValue(251.0, PREV, OK))
        try:
            series = nasdaq.parse(fixture(nasdaq_table[ticker]))
        except nasdaq.SymbolUnknown as exc:
            return nasdaq.failed(ticker, "Nasdaq", str(exc), symbol_unknown=True)
        return nasdaq.from_series(ticker, "Nasdaq", series, previous, current, adjustment="split")

    def cnbc_batch(tickers, current, settings=None, previous=None):
        out = {}
        for ticker in tickers:
            if ticker == "SKYD":
                out[ticker] = cnbc.parse(SKYD_SAME_EVENING, current, previous)["SKYD"]
            elif ticker in real_cnbc:
                out[ticker] = real_cnbc[ticker]
            else:
                out[ticker] = PriceObservation(ticker, "CNBC", close=FieldValue(250.0, DAY, OK),
                                               previous_close=FieldValue(251.0, PREV, OK, corroborative=True))
        return out

    monkeypatch.setattr(market_data.yahoo, "fetch_us", lambda t, p, d, s=None: yahoo_table[t])
    monkeypatch.setattr(market_data.nasdaq, "fetch", nasdaq_fetch)
    monkeypatch.setattr(market_data.cnbc, "fetch_batch", cnbc_batch)
    if successors:
        search(monkeypatch)
    else:
        monkeypatch.setattr(market_data.yahoo, "find_successor_symbols", lambda *a, **k: [])


FILLERS = [f"F{i:03d}" for i in range(40)]


def universe():
    tickers = ["AAPL", "PSKY", "WBD"] + FILLERS
    names = ["Apple Inc.", "Paramount Skydance Corporation", "Warner Bros. Discovery"] + [f"{t} Corp" for t in FILLERS]
    return pd.DataFrame({"ticker": tickers, "yahoo_ticker": tickers, "company_name": names, "sector": "X"})


def test_listing_gap_classification():
    wbd = [yahoo_obs("yahoo-chart-WBD-2026-10-07.json", "WBD"),
           nasdaq.failed("WBD", "Nasdaq", "unknown", symbol_unknown=True)]
    assert listing_gap(wbd, PREV, DAY) == "ENDED"
    psky = [psky_at_run_time(), nasdaq.failed("PSKY", "Nasdaq", "unknown", symbol_unknown=True)]
    assert listing_gap(psky, PREV, DAY) == "TRANSITION"
    outage = [nasdaq.failed("AAPL", "Yahoo", "HTTP 503"), nasdaq.failed("AAPL", "Nasdaq", "HTTP 503")]
    assert listing_gap(outage, PREV, DAY) is None  # an outage is never mistaken for a delisting


def test_ended_symbol_excluded_and_renamed_symbol_followed(monkeypatch):
    wire_sources(monkeypatch)
    result = download_market_data(universe(), DAY, PREV, FAST)
    assert set(result.excluded) == {"WBD"} and not result.missing
    assert result.renamed == {"PSKY": "SKYD"}
    skyd = result.prices.set_index("ticker").loc["SKYD"]
    assert (skyd.previous_close, skyd.close) == (9.53, 8.89)
    assert skyd.change_pct == pytest.approx(-6.7156, abs=1e-4) and not skyd.detected
    assert skyd.validation_status == "CROSS_VALIDATED" and "PSKY → SKYD" in skyd.note


def test_unresolved_rename_is_a_specific_warning(monkeypatch):
    wire_sources(monkeypatch, successors=False)
    result = download_market_data(universe(), DAY, PREV, FAST)
    assert set(result.missing) == {"PSKY"} and "PSKY" in result.transitions
    assert set(result.excluded) == {"WBD"}


def run_monitor(monkeypatch, successors=True):
    wire_sources(monkeypatch, successors)
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    monkeypatch.setattr("market_data.time.sleep", lambda *_: None)
    monkeypatch.setattr(main, "get_session_context", lambda: SessionContext(DAY, DAY, PREV, False))
    monkeypatch.setattr(main, "load_constituents", universe)
    monkeypatch.setattr(main, "fetch_news", lambda *a, **k: {"cause": "", "items": [], "error": None})
    return main.run_us_monitor(None)


def test_mail_no_longer_warns_for_listing_changes(monkeypatch):
    result = run_monitor(monkeypatch)
    assert result.status == INFO and not result.data_warning_issues
    assert result.total_count == 42 and result.analyzed_count == 42 and result.coverage == 1.0
    assert {issue.category for issue in result.info_issues} == {"LISTING_ENDED", "TICKER_CHANGE"}
    kr = main._skipped_result("KR")
    assert build_combined_subject(result, kr) == "[급락 모니터] S&P500 0개 · KOSPI 제외"
    html = build_html_report(result, kr, pd.Timestamp("2026-10-08 07:38", tz="Asia/Seoul").to_pydatetime())
    assert "DATA WARNING" not in html and "PSKY → SKYD" in html and "거래 종료" in html


def test_mail_unresolved_rename_names_the_cause(monkeypatch):
    result = run_monitor(monkeypatch, successors=False)
    assert result.status == WARNING
    assert [issue.category for issue in result.data_warning_issues] == ["LISTING_CHANGE"]
    assert "티커 변경" in result.data_warning_issues[0].message
    assert result.status != NORMAL


def test_many_ended_symbols_are_not_silently_excluded(monkeypatch):
    stale = {f"X{i:02d}": PriceObservation(f"X{i:02d}", "Yahoo", close=FieldValue(date=DAY, status=STALE_SOURCE),
                                           previous_close=FieldValue(date=PREV, status=STALE_SOURCE),
                                           latest_date=date(2026, 10, 2)) for i in range(10)}
    monkeypatch.setattr(market_data.yahoo, "fetch_us", lambda t, p, d, s=None: stale[t])
    monkeypatch.setattr(market_data.nasdaq, "fetch",
                        lambda t, p, d, s=None: nasdaq.failed(t, "Nasdaq", "unknown", symbol_unknown=True))
    monkeypatch.setattr(market_data.cnbc, "fetch_batch",
                        lambda tickers, d, s=None, p=None: {t: nasdaq.failed(t, "CNBC", "x") for t in tickers})
    monkeypatch.setattr(market_data.yahoo, "find_successor_symbols", lambda *a, **k: [])
    frame = pd.DataFrame({"ticker": list(stale), "yahoo_ticker": list(stale), "company_name": list(stale), "sector": "X"})
    result = download_market_data(frame, DAY, PREV, FAST)
    assert not result.excluded and len(result.missing) == 10
