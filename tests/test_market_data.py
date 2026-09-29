"""US regular-close collection: parsing, date matching, reconciliation, coverage."""
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

import market_data
from config import Settings
from market_data import (Bar, PairResult, _parse_chart_response, _parse_nasdaq_payload, download_market_data,
                         fetch_yahoo_pair, reconcile, select_pair)

NY = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).parent / "fixtures"
PREV, DAY = date(2026, 9, 25), date(2026, 9, 28)
FAST = Settings(download_retries=1, retry_backoff_seconds=0, missing_retry_pause_seconds=0, availability_wait_seconds=0, max_workers=4)


def chart(ticker="TEST", days=(PREV, DAY), closes=(100.0, 90.0), volumes=(1000, 1000), hour=9, minute=30):
    return {"chart": {"result": [{
        "meta": {"symbol": ticker, "exchangeTimezoneName": "America/New_York", "dataGranularity": "1d",
                 "regularMarketPrice": 1, "postMarketPrice": 2},
        "timestamp": [int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=NY).timestamp()) for d in days],
        "indicators": {"quote": [{"close": list(closes), "volume": list(volumes), "open": list(closes),
                                  "high": list(closes), "low": list(closes)}],
                       "adjclose": [{"adjclose": [1.0] * len(days)}]}}]}}


def constituents(*tickers):
    return pd.DataFrame({"ticker": list(tickers), "yahoo_ticker": [t.replace(".", "-") for t in tickers],
                         "company_name": [f"{t} Corp" for t in tickers], "sector": "X"})


def test_parser_uses_raw_close_not_adjclose_or_extended_price():
    bars = _parse_chart_response(chart(), "TEST")
    assert [bars[PREV].close, bars[DAY].close] == [100.0, 90.0]


def test_parser_strips_float_noise_to_cents():
    bars = _parse_chart_response(chart(closes=(288.70001220703125, 262.8699951171875)), "TEST")
    assert (bars[PREV].close, bars[DAY].close) == (288.70, 262.87)


def test_parser_accepts_last_trade_timestamp_just_after_bell():
    bars = _parse_chart_response(chart(hour=16, minute=2), "TEST")
    assert DAY in bars


@pytest.mark.parametrize("hour", [18, 7])
def test_parser_rejects_extended_hours_timestamp(hour):
    with pytest.raises(ValueError, match="정규장"):
        _parse_chart_response(chart(hour=hour), "TEST")


def test_parser_rejects_holiday_bar():
    with pytest.raises(ValueError, match="정규장"):
        _parse_chart_response(chart(days=(date(2025, 7, 3), date(2025, 7, 4))), "TEST")


def test_parser_rejects_ticker_mismatch_and_duplicates():
    with pytest.raises(ValueError, match="ticker"):
        _parse_chart_response(chart("OTHER"), "TEST")
    with pytest.raises(ValueError, match="중복"):
        _parse_chart_response(chart(days=(DAY, DAY)), "TEST")


def test_null_close_row_is_dropped_not_nan():
    bars = _parse_chart_response(chart(closes=(100.0, None)), "TEST")
    assert DAY not in bars
    assert "필수 거래일" in select_pair(bars, PREV, DAY, "Yahoo").error


def test_pair_is_selected_by_date_not_last_row():
    bars = _parse_chart_response(chart(days=(date(2026, 9, 24), PREV, DAY, date(2026, 9, 29)),
                                       closes=(50, 100, 90, 10), volumes=(1, 1, 1, 1)), "TEST")
    pair = select_pair(bars, PREV, DAY, "Yahoo")
    assert (pair.previous_close, pair.close) == (100, 90)


def test_wrong_date_rows_are_rejected():
    bars = _parse_chart_response(chart(days=(date(2026, 9, 23), date(2026, 9, 24))), "TEST")
    assert not select_pair(bars, PREV, DAY, "Yahoo").ok


def test_stale_rows_are_rejected():
    bars = {PREV: Bar(100, 10), DAY: Bar(100, 10)}
    assert "stale" in select_pair(bars, PREV, DAY, "Yahoo").error
    bars = {PREV: Bar(100, 10), DAY: Bar(90, 0)}
    assert "거래량 0" in select_pair(bars, PREV, DAY, "Yahoo").error


def test_request_disables_extended_hours_and_bounds_retries(monkeypatch):
    calls = []

    def fail(url, **kwargs):
        calls.append((url, kwargs))
        raise requests.Timeout("simulated")

    monkeypatch.setattr("net.requests.get", fail)
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    pair = fetch_yahoo_pair("TEST", PREV, DAY, Settings(download_retries=2))
    assert not pair.ok and "Yahoo" in pair.error
    assert len(calls) == 4  # 2 hosts x 2 attempts, never unbounded
    assert all(c[1]["params"]["includePrePost"] == "false" and c[1]["params"]["interval"] == "1d" for c in calls)
    assert all(c[1]["timeout"][0] <= 5 and c[1]["timeout"][1] <= 10 for c in calls)


def test_query1_429_falls_back_to_query2(monkeypatch):
    class Response:
        def __init__(self, status, payload=None):
            self.status_code, self.payload = status, payload
        def json(self):
            return self.payload

    def get(url, **kwargs):
        return Response(429) if "query1" in url else Response(200, chart())

    monkeypatch.setattr("net.requests.get", get)
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    pair = fetch_yahoo_pair("TEST", PREV, DAY, FAST)
    assert (pair.previous_close, pair.close) == (100, 90)


def test_split_inside_window_is_annotated(monkeypatch):
    data = chart()
    data["chart"]["result"][0]["events"] = {"splits": {"x": {"date": data["chart"]["result"][0]["timestamp"][1],
                                                               "splitRatio": "2:1"}}}

    class Response:
        status_code = 200
        def json(self):
            return data

    monkeypatch.setattr("net.requests.get", lambda *a, **k: Response())
    pair = fetch_yahoo_pair("TEST", PREV, DAY, FAST)
    assert pair.ok and "분할" in pair.note


def test_nasdaq_parser_reads_official_close():
    payload = json.loads((FIXTURES / "nasdaq-BE-2026-09-28.json").read_text(encoding="utf-8"))
    bars = _parse_nasdaq_payload(payload)
    assert (bars[PREV].close, bars[DAY].close) == (288.70, 262.87)
    with pytest.raises(ValueError):
        _parse_nasdaq_payload({"data": {"tradesTable": None}, "status": {"rCode": 200}})


def test_reconcile_agreement_mismatch_and_fallback():
    ok = PairResult(100, 91)
    row, _ = reconcile("T", ok, PairResult(100, 91), -10, -8)
    assert row["validation"] == "Nasdaq 일치" and not row["detected"]
    # Providers disagree: recall first, detected when either source qualifies.
    row, _ = reconcile("T", ok, PairResult(100, 89), -10, -8)
    assert row["mismatch"] and row["detected"] and row["source"] == "Nasdaq"
    row, _ = reconcile("T", PairResult(error="Yahoo down"), PairResult(100, 85), -10, -8)
    assert row["fallback"] and row["detected"]
    row, reason = reconcile("T", PairResult(error="Yahoo down"), PairResult(error="Nasdaq down"), -10, -8)
    assert row is None and "Yahoo down" in reason and "Nasdaq down" in reason


def _fake_sources(monkeypatch, yahoo: dict, nasdaq: dict):
    monkeypatch.setattr(market_data, "fetch_yahoo_pair",
                        lambda t, p, d, s: yahoo.get(t) or PairResult(error=f"Yahoo 필수 거래일 누락 {t}"))
    monkeypatch.setattr(market_data, "fetch_nasdaq_pair",
                        lambda t, p, d, s: nasdaq.get(t) or PairResult(error=f"Nasdaq 없음 {t}"))


def test_bulk_partial_provider_gap_counts_only_validated_pairs(monkeypatch):
    universe = constituents("AAA", "BBB", "CCC", "BRK.B")
    _fake_sources(monkeypatch,
                  yahoo={"AAA": PairResult(100, 89.99), "BBB": PairResult(100, 95), "BRK-B": PairResult(400, 401)},
                  nasdaq={"AAA": PairResult(100, 89.99), "BRK-B": PairResult(400, 401)})
    result = download_market_data(universe, DAY, PREV, FAST)
    assert len(result.prices) == 3 and set(result.missing) == {"CCC"}
    assert list(result.prices.loc[result.prices.detected, "ticker"]) == ["AAA"]
    assert "BRK.B" in set(result.prices.ticker)  # symbol mapping BRK.B <-> BRK-B


def test_nasdaq_outage_trips_circuit_breaker_and_keeps_primary(monkeypatch):
    tickers = [f"T{i:03d}" for i in range(60)]
    calls = []
    monkeypatch.setattr(market_data, "fetch_yahoo_pair", lambda t, p, d, s: PairResult(100, 99))

    def nasdaq(t, p, d, s):
        calls.append(t)
        return PairResult(error="HTTP 403")

    monkeypatch.setattr(market_data, "fetch_nasdaq_pair", nasdaq)
    result = download_market_data(constituents(*tickers), DAY, PREV, Settings(max_workers=1, missing_retry_pause_seconds=0))
    assert len(result.prices) == 60 and not result.missing
    assert not result.secondary_available and len(calls) < 60
    assert any("교차검증 불가" in n for n in result.notices) and not result.warnings


def test_us_2026_09_28_regression_from_recorded_closes(monkeypatch):
    """TEST 17: real regular closes recorded from Yahoo and Nasdaq for all 503."""
    frame = pd.read_csv(FIXTURES / "us-2026-09-28-closes.csv", dtype={"ticker": str})
    yahoo = {row.ticker.replace(".", "-"): PairResult(round(row.yahoo_prev_2026_09_25, 2), round(row.yahoo_close_2026_09_28, 2))
             for row in frame.itertuples()}
    nasdaq = {row.ticker.replace(".", "-"): PairResult(float(row.nasdaq_prev_2026_09_25), float(row.nasdaq_close_2026_09_28))
              for row in frame.itertuples() if pd.notna(row.nasdaq_prev_2026_09_25)}
    _fake_sources(monkeypatch, yahoo, nasdaq)
    result = download_market_data(constituents(*frame.ticker), DAY, PREV, FAST)
    assert len(result.prices) == 503 and not result.missing
    assert result.mismatch_count == 0 and result.cross_checked_count == 502  # BF.B unsupported by Nasdaq
    assert result.prices["detected"].sum() == 0
    worst = result.prices.sort_values("change_pct").iloc[0]
    assert worst.ticker == "BE" and worst.change_pct == pytest.approx(-8.947, abs=1e-3)
    # Independent recomputation straight from the fixture agrees.
    independent = ((frame.nasdaq_close_2026_09_28.astype(float) / frame.nasdaq_prev_2026_09_25.astype(float) - 1) * 100)
    assert (independent.dropna() <= -10).sum() == 0


def test_real_be_payload_parses_to_regular_closes():
    payload = json.loads((FIXTURES / "yahoo-chart-BE-2026-09-28.json").read_text(encoding="utf-8"))
    pair = select_pair(_parse_chart_response(payload, "BE"), PREV, DAY, "Yahoo")
    assert (pair.previous_close, pair.close) == (288.70, 262.87)


def test_unverified_candidate_in_validation_band_raises_warning(monkeypatch):
    _fake_sources(monkeypatch, yahoo={"AAA": PairResult(100, 91), "BBB": PairResult(100, 99)},
                  nasdaq={"BBB": PairResult(100, 99)})
    result = download_market_data(constituents("AAA", "BBB"), DAY, PREV, FAST)
    assert any("AAA" in warning for warning in result.warnings)
    assert "2차 검증 불가" in result.prices.set_index("ticker").loc["AAA", "note"]


def test_throttled_secondary_stops_after_consecutive_failures(monkeypatch):
    tickers = [f"T{i:03d}" for i in range(100)]
    calls = []
    monkeypatch.setattr(market_data, "fetch_yahoo_pair",
                        lambda t, p, d, s: PairResult(100, 100 - int(t[1:]) / 10))

    def nasdaq(t, p, d, s):
        calls.append(t)
        return PairResult(100, 100 - int(t[1:]) / 10) if len(calls) <= 10 else PairResult(error="HTTP 403")

    monkeypatch.setattr(market_data, "fetch_nasdaq_pair", nasdaq)
    result = download_market_data(constituents(*tickers), DAY, PREV, Settings(max_workers=1, missing_retry_pause_seconds=0))
    assert len(calls) == 10 + market_data.NASDAQ_CIRCUIT_BREAKER
    # The largest decliners were verified first.
    verified = set(result.prices.loc[result.prices.cross_checked, "ticker"])
    assert verified == {f"T{i:03d}" for i in range(90, 100)}
    assert any("10/100" in notice for notice in result.notices)
