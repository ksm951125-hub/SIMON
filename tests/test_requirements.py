"""Operator acceptance tests TEST 1-18 (one test per requirement, offline)."""
from datetime import date, datetime

import pandas as pd
import pytest

import kospi
import main
import market_data
from config import Settings
from detector import calculate_change_pct, is_drop
from kospi import KospiSessionContext, KrPair, NaverDay, download_kospi_market_data, load_kospi_constituents
from market_calendar import KST, get_session_context
from market_data import PairResult, download_market_data, select_pair, _parse_chart_response
from market_result import FAILED, OK, WARNING, classify_status
from test_kospi import listing_server, universe_items
from test_market_data import chart, constituents

FAST = Settings(download_retries=1, retry_backoff_seconds=0, missing_retry_pause_seconds=0, availability_wait_seconds=0, max_workers=4)
US_PREV, US_DAY = date(2026, 9, 25), date(2026, 9, 28)
KR_PREV, KR_DAY = date(2026, 9, 28), date(2026, 9, 29)


def us_run(monkeypatch, yahoo, nasdaq=None):
    nasdaq = yahoo if nasdaq is None else nasdaq
    monkeypatch.setattr(market_data, "fetch_yahoo_pair", lambda t, p, d, s: yahoo.get(t, PairResult(error="Yahoo 누락")))
    monkeypatch.setattr(market_data, "fetch_nasdaq_pair", lambda t, p, d, s: nasdaq.get(t, PairResult(error="Nasdaq 누락")))
    return download_market_data(constituents(*sorted({*yahoo, *nasdaq})), US_DAY, US_PREV, FAST)


def test_01_normal_us_stock(monkeypatch):
    result = us_run(monkeypatch, {"AAPL": PairResult(341.07, 338.40)})
    row = result.prices.iloc[0]
    assert not row.detected and row.validation == "Nasdaq 일치" and row.change_pct == pytest.approx(-0.78284, abs=1e-5)


@pytest.mark.parametrize("close,detected", [(90.01, False), (90.00, True), (89.99, True)],
                         ids=["TEST02 -9.99%", "TEST03 -10.00%", "TEST04 -10.01%"])
def test_02_03_04_us_threshold_boundary(monkeypatch, close, detected):
    result = us_run(monkeypatch, {"X": PairResult(100.0, close)})
    assert bool(result.prices.iloc[0].detected) is detected
    # Detection uses the raw value, display rounding never feeds the decision.
    raw = calculate_change_pct(100.0, close)
    assert is_drop(raw, -10.0) is detected


@pytest.mark.parametrize("close,detected", [(93.01, False), (93.00, True), (92.99, True)],
                         ids=["TEST05 -6.99%", "TEST06 -7.00%", "TEST07 -7.01%"])
def test_05_06_07_kr_threshold_boundary(monkeypatch, close, detected):
    monkeypatch.setattr(kospi, "fetch_yahoo_kr_pair", lambda c, p, d, s: KrPair(100.0, close))
    monkeypatch.setattr(kospi, "fetch_naver_days", lambda c, s, n: {KR_DAY: NaverDay(close, 100.0, 1, None)})
    result = download_kospi_market_data(pd.DataFrame({"code": ["000001"], "company_name": ["A"]}),
                                        KospiSessionContext(KR_DAY, KR_PREV), FAST)
    assert bool(result.prices.iloc[0].detected) is detected


def test_rounding_never_decides_detection():
    # -9.996% displays as -10.00% but is NOT a -10% drop.
    raw = calculate_change_pct(100.0, 90.004)
    assert f"{raw:.2f}" == "-10.00" and not is_drop(raw, -10.0)


def test_08_previous_trading_day_skips_weekend():
    context = get_session_context(datetime(2026, 9, 29, 8, 0, tzinfo=KST))  # Monday NY session
    assert (context.session_date, context.previous_session_date) == (US_DAY, US_PREV)
    assert US_PREV.strftime("%A") == "Friday"


def test_09_us_holiday_uses_latest_completed_session():
    # 2026-09-08 08:00 KST = Monday 2026-09-07 (Labor Day) evening in New York.
    context = get_session_context(datetime(2026, 9, 8, 8, 0, tzinfo=KST))
    assert context.session_date == date(2026, 9, 4) and context.previous_session_date == date(2026, 9, 3)
    assert context.reason  # surfaced in the report as a notice


def test_10_kr_holiday_uses_latest_completed_session(monkeypatch):
    rows = [{"localTradedAt": d} for d in ("2026-09-23", "2026-09-22", "2026-09-21")]
    monkeypatch.setattr(kospi, "_json", lambda url, params, settings=None: rows if params["page"] == 1 else [])
    context = kospi.get_kospi_session_context(now=datetime(2026, 9, 25, 8, tzinfo=KST))  # Chuseok
    assert (context.session_date, context.previous_session_date) == (date(2026, 9, 23), date(2026, 9, 22))


def test_11_one_missing_symbol_is_warning_not_failed(monkeypatch):
    yahoo = {f"T{i:03d}": PairResult(100, 99) for i in range(503)}
    nasdaq = dict(yahoo)
    yahoo.pop("T007"), nasdaq.pop("T007")
    monkeypatch.setattr(market_data, "fetch_yahoo_pair", lambda t, p, d, s: yahoo.get(t, PairResult(error="Yahoo 누락")))
    monkeypatch.setattr(market_data, "fetch_nasdaq_pair", lambda t, p, d, s: nasdaq.get(t, PairResult(error="Nasdaq 누락")))
    result = download_market_data(constituents(*[f"T{i:03d}" for i in range(503)]), US_DAY, US_PREV, FAST)
    assert len(result.prices) == 502 and list(result.missing) == ["T007"]
    assert classify_status(503, 502, missing_count=1) == WARNING


def test_12_ticker_parsing_failure_warns_and_continues(monkeypatch):
    items = universe_items()
    items[3] = {"itemCode": "", "stockName": "", "stockEndType": "stock", "stockExchangeType": {"nameEng": "KOSPI"}}
    listing_server(monkeypatch, [(len(items), items)])
    frame = load_kospi_constituents(FAST)
    assert len(frame) == 943 and frame.attrs["warnings"]


def test_13_current_price_nan(monkeypatch):
    bars = _parse_chart_response(chart(closes=(100.0, float("nan"))), "TEST")
    assert not select_pair(bars, US_PREV, US_DAY, "Yahoo").ok


def test_14_previous_price_nan(monkeypatch):
    bars = _parse_chart_response(chart(closes=(None, 90.0)), "TEST")
    assert not select_pair(bars, US_PREV, US_DAY, "Yahoo").ok


def test_15_wrong_date_row_rejected():
    bars = _parse_chart_response(chart(days=(date(2026, 9, 24), US_PREV)), "TEST")  # stale: newest row is 09-25
    pair = select_pair(bars, US_PREV, US_DAY, "Yahoo")
    assert not pair.ok and "2026-09-28" in pair.error


def test_16_bulk_provider_omits_some_tickers(monkeypatch):
    yahoo = {f"T{i}": PairResult(100, 95) for i in range(10) if i not in (2, 5)}
    nasdaq = {f"T{i}": PairResult(100, 95) for i in range(10) if i != 2}
    monkeypatch.setattr(market_data, "fetch_yahoo_pair", lambda t, p, d, s: yahoo.get(t, PairResult(error="omitted")))
    monkeypatch.setattr(market_data, "fetch_nasdaq_pair", lambda t, p, d, s: nasdaq.get(t, PairResult(error="omitted")))
    result = download_market_data(constituents(*[f"T{i}" for i in range(10)]), US_DAY, US_PREV, FAST)
    assert list(result.missing) == ["T2"] and result.fallback_count == 1  # T5 recovered via Nasdaq


def test_17_us_2026_09_28_regression_with_recorded_closes(monkeypatch):
    from test_market_data import test_us_2026_09_28_regression_from_recorded_closes as regression
    regression(monkeypatch)


def test_18_kospi_2479_of_2480_does_not_fail_market(monkeypatch):
    items = universe_items()
    listing_server(monkeypatch, [(2480, items)] * 3)
    frame = load_kospi_constituents(FAST)
    monkeypatch.setattr(kospi, "fetch_yahoo_kr_pair", lambda c, p, d, s: KrPair(1000.0, 990.0))
    monkeypatch.setattr(kospi, "fetch_naver_days", lambda c, s, n: {KR_DAY: NaverDay(990.0, 1000.0, 1, -1.0)})
    monkeypatch.setattr(main, "get_kospi_session_context", lambda d: KospiSessionContext(KR_DAY, KR_PREV))
    monkeypatch.setattr(main, "load_kospi_constituents", lambda: frame)
    monkeypatch.setattr(main, "optional_validator", lambda: None)
    monkeypatch.setattr(main, "_optional_krx_client", lambda: None)
    result = main.run_kr_monitor(None)
    assert result.status == WARNING and result.analyzed_count == 944 and result.coverage == 1.0
    assert any("expected 2480, loaded 2479" in warning for warning in result.warnings)


def test_status_policy():
    assert classify_status(503, 503) == OK
    assert classify_status(503, 502, missing_count=1) == WARNING
    assert classify_status(503, 503, fallback_count=1) == WARNING
    assert classify_status(503, 400) == FAILED
    assert classify_status(503, 0) == FAILED
