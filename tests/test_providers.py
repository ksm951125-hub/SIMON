"""Source adapters: exact-date selection, regular-session fields, stale handling."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

from config import Settings
from helpers import FAST, fixture
from providers import cnbc, daum, nasdaq, naver, yahoo
from providers.base import INTEGRATED, INVALID_PRICE, NOT_PROVIDED, OK, REGULAR, STALE_SOURCE, UNAVAILABLE

NY = ZoneInfo("America/New_York")
PREV, DAY = date(2026, 9, 28), date(2026, 9, 29)


def chart(ticker="TEST", days=(PREV, DAY), closes=(100.0, 90.0), volumes=(1000, 1000), hour=9, minute=30):
    return {"chart": {"result": [{
        "meta": {"symbol": ticker, "exchangeTimezoneName": "America/New_York", "dataGranularity": "1d",
                 "regularMarketPrice": 1, "postMarketPrice": 2},
        "timestamp": [int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=NY).timestamp()) for d in days],
        "indicators": {"quote": [{"close": list(closes), "volume": list(volumes), "open": list(closes),
                                  "high": list(closes), "low": list(closes)}],
                       "adjclose": [{"adjclose": [1.0] * len(days)}]}}]}}


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


# ---------------------------------------------------------------- Yahoo (US)
def test_yahoo_uses_raw_close_not_adjclose_or_extended_price():
    observation = yahoo.observation_from_bars("TEST", yahoo.parse_us_chart(chart(), "TEST"), PREV, DAY)
    assert (observation.previous_close.value, observation.close.value) == (100.0, 90.0)
    assert observation.close.session_type == REGULAR and observation.adjustment == "split"


def test_yahoo_strips_float_noise_to_cents():
    bars = yahoo.parse_us_chart(chart(closes=(840.8900146484375, 617.8699951171875)), "TEST")
    assert (bars[PREV].close, bars[DAY].close) == (840.89, 617.87)


@pytest.mark.parametrize("hour", [18, 7])
def test_yahoo_rejects_extended_hours_bar(hour):
    with pytest.raises(ValueError, match="정규장"):
        yahoo.parse_us_chart(chart(hour=hour), "TEST")


def test_yahoo_accepts_last_trade_timestamp_just_after_bell_and_rejects_holiday():
    assert DAY in yahoo.parse_us_chart(chart(hour=16, minute=2), "TEST")
    with pytest.raises(ValueError, match="정규장"):
        yahoo.parse_us_chart(chart(days=(date(2025, 7, 3), date(2025, 7, 4))), "TEST")


def test_yahoo_rejects_ticker_mismatch_and_duplicate_dates():
    with pytest.raises(ValueError, match="ticker"):
        yahoo.parse_us_chart(chart("OTHER"), "TEST")
    with pytest.raises(ValueError, match="중복"):
        yahoo.parse_us_chart(chart(days=(DAY, DAY)), "TEST")


def test_requested_date_must_equal_returned_date():
    # Newest row is 09-28: the 09-29 close is STALE_SOURCE, never "last row".
    bars = yahoo.parse_us_chart(chart(days=(date(2026, 9, 25), PREV)), "TEST")
    observation = yahoo.observation_from_bars("TEST", bars, PREV, DAY)
    assert observation.close.status == STALE_SOURCE and "최신 2026-09-28" in observation.close.detail
    assert observation.previous_close.status == OK


def test_yahoo_null_and_stale_bars_are_invalid():
    observation = yahoo.observation_from_bars("T", yahoo.parse_us_chart(chart(closes=(100.0, None)), "TEST"), PREV, DAY)
    assert observation.close.status == STALE_SOURCE  # null close row omitted
    observation = yahoo.observation_from_bars("T", yahoo.parse_us_chart(chart(closes=(100.0, 100.0)), "TEST"), PREV, DAY)
    assert observation.close.status == INVALID_PRICE and "stale" in observation.close.detail
    observation = yahoo.observation_from_bars("T", yahoo.parse_us_chart(chart(volumes=(1, 0)), "TEST"), PREV, DAY)
    assert observation.close.status == INVALID_PRICE


def test_yahoo_request_disables_extended_hours_and_bounds_retries(monkeypatch):
    calls = []

    def fail(url, **kwargs):
        calls.append((url, kwargs))
        raise requests.Timeout("simulated")

    monkeypatch.setattr("net.requests.get", fail)
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    observation = yahoo.fetch_us("TEST", PREV, DAY, Settings(download_retries=2))
    assert observation.close.status == UNAVAILABLE
    assert len(calls) == 4  # 2 hosts x 2 attempts, never unbounded
    assert all(kw["params"]["includePrePost"] == "false" and kw["params"]["interval"] == "1d" for _, kw in calls)
    assert all(kw["timeout"][0] <= 5 and kw["timeout"][1] <= 10 for _, kw in calls)


def test_yahoo_query1_throttled_falls_back_to_query2(monkeypatch):
    monkeypatch.setattr("net.requests.get", lambda url, **kw: Response(None, 429) if "query1" in url else Response(chart()))
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    assert yahoo.fetch_us("TEST", PREV, DAY, FAST).close.value == 90.0


def test_yahoo_split_inside_window_is_annotated(monkeypatch):
    data = chart()
    data["chart"]["result"][0]["events"] = {"splits": {"x": {"date": data["chart"]["result"][0]["timestamp"][1],
                                                               "splitRatio": "2:1"}}}
    monkeypatch.setattr("net.requests.get", lambda *a, **k: Response(data))
    assert "분할" in yahoo.fetch_us("TEST", PREV, DAY, FAST).note


def test_yahoo_real_fico_payload():
    observation = yahoo.observation_from_bars(
        "FICO", yahoo.parse_us_chart(fixture("yahoo-chart-FICO-2026-09-29.json"), "FICO"), PREV, DAY)
    assert (observation.previous_close.value, observation.close.value) == (840.89, 617.87)


# ---------------------------------------------------------------- Nasdaq
def test_nasdaq_stale_analysis_row_keeps_previous_close():
    observation = nasdaq.from_series("FICO", "Nasdaq", nasdaq.parse(fixture("nasdaq-FICO-stale-at-run.json")), PREV, DAY)
    assert observation.previous_close.value == 840.89
    assert observation.close.status == STALE_SOURCE and "2026-09-28" in observation.close.detail


def test_nasdaq_published_row():
    observation = nasdaq.from_series("FICO", "Nasdaq", nasdaq.parse(fixture("nasdaq-FICO-2026-09-30-fetch.json")), PREV, DAY)
    assert (observation.previous_close.value, observation.close.value) == (840.89, 617.87)


def test_nasdaq_empty_payload_is_error():
    with pytest.raises(ValueError):
        nasdaq.parse({"data": {"tradesTable": None}, "status": {"rCode": 200}})
    assert nasdaq.symbol_for("BRK-B") == "BRK.B"


# ---------------------------------------------------------------- CNBC
def cnbc_quote(symbol="FICO", last="617.87", prev="840.89", status="POST_MKT", ext="POST_MKT", day="2026-09-29"):
    return {"FormattedQuoteResult": {"FormattedQuote": [{
        "symbol": symbol, "last": last, "last_time": day, "previous_day_closing": prev, "curmktstatus": status,
        "ExtendedMktQuote": {"type": ext, "last": "615.00"}}]}}


def test_cnbc_same_evening_snapshot_gives_regular_close_only():
    observation = cnbc.parse(cnbc_quote(), DAY)["FICO"]
    assert observation.close.value == 617.87 and observation.close.status == OK
    assert observation.previous_close.status == NOT_PROVIDED  # never read


def test_cnbc_rolled_snapshot_is_stale_not_price_error():
    # Real 2026-09-30 pre-market snapshot: AMT/O re-based for next-day ex-dividends.
    parsed = cnbc.parse(fixture("cnbc-rolled-2026-09-30.json"), DAY)
    assert {symbol: parsed[symbol].close.status for symbol in ("AMT", "O", "FICO")} == dict.fromkeys(("AMT", "O", "FICO"), STALE_SOURCE)
    assert "배당락" in parsed["AMT"].close.detail


def test_cnbc_other_date_is_stale():
    assert cnbc.parse(cnbc_quote(day="2026-09-28"), DAY)["FICO"].close.status == STALE_SOURCE


def test_cnbc_batches_and_maps_symbols(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs["params"]["symbols"])
        return Response(cnbc_quote(symbol="BRK.B"))

    monkeypatch.setattr("net.requests.get", get)
    result = cnbc.fetch_batch(["BRK-B"] + [f"T{i}" for i in range(150)], DAY, FAST)
    assert len(calls) == 2 and "BRK.B" in calls[0]
    assert result["BRK-B"].close.value == 617.87 and result["T5"].close.status == UNAVAILABLE


# ---------------------------------------------------------------- Naver (KR)
def test_naver_close_is_integrated_and_base_is_previous_krx_close():
    days = naver.parse_daily(fixture("naver-price-005030-2026-09-30.json"))
    observation = naver.observation_from_days("005030", days, PREV, DAY)
    assert observation.previous_close.value == 486 and observation.previous_close.session_type == REGULAR
    assert observation.close.value == 37 and observation.close.session_type == INTEGRATED
    assert not observation.close.usable  # never compared as a regular close by default
    assert naver.as_regular(observation, "비NXT 종목").close.usable


# ---------------------------------------------------------------- Daum (KR)
def test_daum_quote_same_day_regular_close_and_state():
    payload = fixture("daum-quote-005030-2026-09-30.json")
    observation = daum.observation_from_quote("005030", payload, DAY, date(2026, 9, 30), date(2026, 10, 1))
    assert (observation.previous_close.value, observation.close.value) == (37, 44)
    state = daum.parse_state(payload)
    assert state.pre_delisting_trading and state.no_price_limit and state.nxt_tradable is False


def test_daum_quote_rolled_snapshot_gives_previous_session_close():
    observation = daum.observation_from_quote("005030", fixture("daum-quote-005030-2026-09-30.json"), PREV, DAY,
                                              date(2026, 9, 30))
    assert observation.close.value == 37 and observation.previous_close.status == NOT_PROVIDED


def test_daum_quote_regular_price_excludes_nxt():
    payload = {"symbolCode": "A005930", "tradeDate": "20260930", "tradePrice": 269000.0, "regularTradePrice": 268500.0,
               "prevClosingPrice": 272500.0, "afterMarketAvailable": True, "stockState": {}}
    observation = daum.observation_from_quote("005930", payload, DAY, date(2026, 9, 30), None)
    assert observation.close.value == 268500.0


def test_daum_quote_other_date_is_stale():
    observation = daum.observation_from_quote("005030", fixture("daum-quote-005030-2026-09-30.json"),
                                              date(2026, 9, 22), date(2026, 9, 23), PREV)
    assert observation.close.status == STALE_SOURCE


def test_daum_days_row_base_price():
    observation = daum.observation_from_days("005030", fixture("daum-days-005030-2026-09-30.json"), PREV, DAY)
    assert observation.previous_close.value == 486 and observation.close.session_type == INTEGRATED


def test_yahoo_kr_parser_keys_by_kst_date_and_halted_bars_allowed():
    payload = {"chart": {"result": [{
        "meta": {"symbol": "000300.KS", "exchangeTimezoneName": "Asia/Seoul", "dataGranularity": "1d"},
        "timestamp": [int(datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc).timestamp()) for d in (PREV, DAY)],
        "indicators": {"quote": [{"close": [4199.99, 4200.01], "volume": [0, 0]}]}}]}}
    observation = yahoo.observation_from_bars("000300", yahoo.parse_kr_chart(payload, "000300"), PREV, DAY,
                                              stale_checks=False)
    assert (observation.previous_close.value, observation.close.value) == (4200, 4200)
    with pytest.raises(ValueError, match="ticker"):
        yahoo.parse_kr_chart(payload, "005930")


def test_availability_gate_waits_are_bounded(monkeypatch):
    from net import retry_until_available
    sleeps, calls = [], []
    monkeypatch.setattr("net.time.sleep", lambda seconds: sleeps.append(seconds))

    def refetch(keys):
        calls.append(len(keys))
        return {key: False for key in keys}

    settings = Settings(missing_retry_pause_seconds=3, availability_retries=2, availability_wait_seconds=60)
    retry_until_available({key: False for key in range(10)}, refetch, bool, settings, "test")
    assert sleeps == [3, 60, 60] and calls == [10, 10, 10]  # never an unbounded wait


def test_naver_next_session_base_is_the_analysis_day_krx_close():
    days = naver.parse_daily(fixture("naver-price-005030-2026-09-30.json"))
    observation = naver.next_session_close("005030", days, DAY, date(2026, 9, 30))
    assert observation.close.value == 37 and observation.close.usable  # 09-30 base = 09-29 KRX close
    assert naver.next_session_close("005030", days, date(2026, 9, 30), date(2026, 10, 1)) is None


def test_next_session_base_reset_by_split_is_not_used_as_close():
    days = {DAY: naver.NaverDay(1550.0, 1550.0, 0, 0.0), date(2026, 9, 30): naver.NaverDay(7200.0, 7750.0, 10, -7.1)}
    assert naver.next_session_close("109070", days, DAY, date(2026, 9, 30)) is None


def test_daum_snapshot_rolled_to_weekend_date_still_gives_friday_close():
    # Friday 2026-10-02 session; Saturday-dated snapshot before Monday's session.
    payload = {"symbolCode": "A005930", "tradeDate": "20261003", "prevClosingPrice": 270000.0,
               "regularTradePrice": None, "stockState": {}}
    observation = daum.observation_from_quote("005930", payload, date(2026, 10, 1), date(2026, 10, 2),
                                              date(2026, 10, 5))
    assert observation.close.value == 270000.0 and observation.close.status == OK
    later = dict(payload, tradeDate="20261006")  # past the next session: stale
    assert daum.observation_from_quote("005930", later, date(2026, 10, 1), date(2026, 10, 2),
                                       date(2026, 10, 5)).close.status == STALE_SOURCE
