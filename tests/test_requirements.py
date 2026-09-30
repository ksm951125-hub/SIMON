"""Operator acceptance tests TEST 1-18 (one test per requirement, offline)."""
from datetime import date, datetime

import pytest

import kospi
import main
from detector import calculate_change_pct, is_drop
from helpers import FAST, fake_us
from kospi import load_kospi_constituents
from market_calendar import KST, get_session_context
from market_data import download_market_data
from market_result import ERROR, INFO, NORMAL, WARNING, Issue, classify_status
from providers import yahoo
from test_kospi import listing_server, normal, run_kr, universe_items
from test_market_data import constituents
from test_providers import chart

US_PREV, US_DAY = date(2026, 9, 25), date(2026, 9, 28)
KR_PREV, KR_DAY = date(2026, 9, 28), date(2026, 9, 29)


def us_run(monkeypatch, yahoo_rows, nasdaq_rows=None):
    fake_us(monkeypatch, yahoo=yahoo_rows, nasdaq=nasdaq_rows)
    tickers = sorted({*yahoo_rows, *(nasdaq_rows or {})})
    return download_market_data(constituents(*tickers), US_DAY, US_PREV, FAST)


def test_01_normal_us_stock(monkeypatch):
    row = us_run(monkeypatch, {"AAPL": (341.07, 338.40)}).prices.iloc[0]
    assert not row.detected and row.validation_status == "CROSS_VALIDATED" and row.severity == NORMAL
    assert row.change_pct == pytest.approx(-0.78284, abs=1e-5)


@pytest.mark.parametrize("close,detected", [(90.01, False), (90.00, True), (89.99, True)],
                         ids=["TEST02 -9.99%", "TEST03 -10.00%", "TEST04 -10.01%"])
def test_02_03_04_us_threshold_boundary(monkeypatch, close, detected):
    assert bool(us_run(monkeypatch, {"X": (100.0, close)}).prices.iloc[0].detected) is detected
    assert is_drop(calculate_change_pct(100.0, close), -10.0) is detected


@pytest.mark.parametrize("close,detected", [(93.01, False), (93.00, True), (92.99, True)],
                         ids=["TEST05 -6.99%", "TEST06 -7.00%", "TEST07 -7.01%"])
def test_05_06_07_kr_threshold_boundary(monkeypatch, close, detected):
    assert bool(run_kr(monkeypatch, {"000001": normal(100, close)}).prices.iloc[0].detected) is detected


def test_rounding_never_decides_detection():
    raw = calculate_change_pct(100.0, 90.004)  # displays -10.00% but is -9.996%
    assert f"{raw:.2f}" == "-10.00" and not is_drop(raw, -10.0)


def test_08_previous_trading_day_skips_weekend():
    context = get_session_context(datetime(2026, 9, 29, 7, 30, tzinfo=KST))  # Monday NY session
    assert (context.session_date, context.previous_session_date) == (US_DAY, US_PREV)
    assert US_PREV.strftime("%A") == "Friday"


def test_09_us_holiday_uses_latest_completed_session():
    # 2026-09-08 07:30 KST = Monday 2026-09-07 (Labor Day) evening in New York.
    context = get_session_context(datetime(2026, 9, 8, 7, 30, tzinfo=KST))
    assert context.session_date == date(2026, 9, 4) and context.previous_session_date == date(2026, 9, 3)
    assert context.reason  # surfaced in the report as an INFO issue


def test_10_kr_holiday_uses_latest_completed_session(monkeypatch):
    rows = [{"localTradedAt": d} for d in ("2026-09-23", "2026-09-22", "2026-09-21")]
    monkeypatch.setattr(kospi, "_json", lambda url, params, settings=None: rows if params["page"] == 1 else [])
    context = kospi.get_kospi_session_context(now=datetime(2026, 9, 25, 7, 30, tzinfo=KST))  # Chuseok
    assert (context.session_date, context.previous_session_date) == (date(2026, 9, 23), date(2026, 9, 22))


def test_11_one_missing_symbol_is_warning_not_error(monkeypatch):
    rows = {f"T{i:03d}": (100, 99) for i in range(503)}
    rows["T007"] = ("down", "down")
    result = us_run(monkeypatch, rows, {k: v for k, v in rows.items()})
    assert len(result.prices) == 502 and list(result.missing) == ["T007"]
    assert classify_status(503, 502, [Issue(WARNING, "MISSING", "누락", "T007")]) == WARNING


def test_12_ticker_parsing_failure_warns_and_continues(monkeypatch):
    items = universe_items()
    items[3] = {"itemCode": "", "stockName": "", "stockEndType": "stock", "stockExchangeType": {"nameEng": "KOSPI"}}
    listing_server(monkeypatch, [(len(items), items)])
    frame = load_kospi_constituents(FAST)
    assert len(frame) == 943 and frame.attrs["warnings"]


def test_13_current_price_nan():
    observation = yahoo.observation_from_bars("T", yahoo.parse_us_chart(chart(closes=(100.0, float("nan"))), "TEST"),
                                              date(2026, 9, 28), date(2026, 9, 29))
    assert not observation.close.usable


def test_14_previous_price_nan():
    observation = yahoo.observation_from_bars("T", yahoo.parse_us_chart(chart(closes=(None, 90.0)), "TEST"),
                                              date(2026, 9, 28), date(2026, 9, 29))
    assert not observation.previous_close.usable


def test_15_wrong_date_row_is_stale_not_used():
    bars = yahoo.parse_us_chart(chart(days=(date(2026, 9, 25), date(2026, 9, 28))), "TEST")
    observation = yahoo.observation_from_bars("T", bars, date(2026, 9, 28), date(2026, 9, 29))
    assert observation.close.status == "STALE_SOURCE" and observation.close.value is None


def test_16_bulk_provider_omits_some_tickers(monkeypatch):
    yahoo_rows = {f"T{i}": ((100, 95) if i not in (2, 5) else ("down", "down")) for i in range(10)}
    nasdaq_rows = {f"T{i}": ((100, 95) if i != 2 else ("down", "down")) for i in range(10)}
    fake_us(monkeypatch, yahoo=yahoo_rows, nasdaq=nasdaq_rows,
            cnbc={f"T{i}": (None, 95 if i != 2 else "down") for i in range(10)})
    result = download_market_data(constituents(*[f"T{i}" for i in range(10)]), US_DAY, US_PREV, FAST)
    assert list(result.missing) == ["T2"] and result.fallback_count == 1  # T5 recovered via Nasdaq+CNBC


def test_17_us_2026_09_28_regression_with_recorded_closes(monkeypatch):
    from test_market_data import test_us_2026_09_28_regression_from_recorded_closes as regression
    regression(monkeypatch)


def test_18_kospi_2479_of_2480_does_not_fail_market(monkeypatch):
    items = universe_items()
    listing_server(monkeypatch, [(2480, items)] * 3)
    frame = load_kospi_constituents(FAST)
    from test_kospi import fake_kr
    fake_kr(monkeypatch, {code: normal(1000, 990) for code in frame.code})
    monkeypatch.setattr(main, "get_kospi_session_context", lambda d: kospi.KospiSessionContext(KR_DAY, KR_PREV))
    monkeypatch.setattr(main, "load_kospi_constituents", lambda: frame)
    monkeypatch.setattr(main, "optional_validator", lambda: None)
    monkeypatch.setattr(main, "_optional_krx_client", lambda: None)
    result = main.run_kr_monitor(None)
    assert result.status == WARNING and result.analyzed_count == 944 and result.coverage == 1.0
    assert [issue.category for issue in result.data_warning_issues] == ["LISTING"]
    assert "expected 2480, loaded 2479" in result.data_warning_issues[0].message


def test_status_policy():
    assert classify_status(503, 503) == NORMAL
    assert classify_status(503, 503, [Issue(INFO, "STALE_SECONDARY", "x")]) == INFO
    assert classify_status(503, 502, [Issue(WARNING, "MISSING", "x")]) == WARNING
    assert classify_status(503, 400) == ERROR
    assert classify_status(503, 0) == ERROR
