from datetime import date, datetime
from zoneinfo import ZoneInfo
import math
import time

import pandas as pd
import pytest
import requests

from config import Settings
from detector import calculate_change_pct
from market_calendar import KST, get_session_context
from market_data import _download_chart_ticker, _parse_chart_response, _download_batch
from kospi import _download_price_pair, _parse_close, get_kospi_session_context, load_kospi_constituents
from main import _status, _context_for_override
from runtime import execute_market


def payload(closes=(100, 90), hours=(9, 9)):
    return {"chart": {"result": [{"meta": {"symbol": "TEST", "exchangeTimezoneName": "America/New_York", "dataGranularity": "1d", "regularMarketPrice": 1, "postMarketPrice": 2},
        "timestamp": [int(datetime(2025, 1, day, hour, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()) for day, hour in zip((2, 3), hours)],
        "indicators": {"quote": [{"close": list(closes)}], "adjclose": [{"adjclose": [1, 2]}]}}]}}


def test_regular_close_ignores_realtime_extended_and_adjusted_prices():
    assert list(_parse_chart_response(payload(), "TEST").values()) == [100, 90]


def test_extended_timestamp_rejected():
    with pytest.raises(ValueError, match="정규장"):
        _parse_chart_response(payload(hours=(9, 18)), "TEST")


def test_duplicate_us_session_rejected():
    data = payload()
    data["chart"]["result"][0]["timestamp"][1] = data["chart"]["result"][0]["timestamp"][0]
    with pytest.raises(ValueError, match="중복"):
        _parse_chart_response(data, "TEST")


def test_chart_request_disables_extended_hours_and_rejects_split(monkeypatch):
    data = payload()
    data["chart"]["result"][0]["events"] = {"splits": {"x": {"date": data["chart"]["result"][0]["timestamp"][1], "numerator": 10, "denominator": 1}}}
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return data
    def get(url, **kwargs):
        calls.append(kwargs)
        return Response()
    monkeypatch.setattr("market_data.requests.get", get)
    with pytest.raises(RuntimeError, match="Corporate action"):
        _download_chart_ticker("TEST", date(2025,1,2), date(2025,1,3), Settings(download_retries=1))
    assert all(c["params"]["includePrePost"] == "false" and c["params"]["interval"] == "1d" and c["timeout"] > 0 for c in calls)


def test_secondary_close_is_not_dividend_adjusted(monkeypatch):
    captured = {}
    def download(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame({"Close": [100, 90]})
    monkeypatch.setattr("market_data.yf.download", download)
    _download_batch(["TEST"], date(2025,1,2), date(2025,1,4), Settings())
    assert captured["auto_adjust"] is False
    assert captured["prepost"] is False


def test_timeout_retries_are_bounded(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise requests.Timeout("simulated")
    monkeypatch.setattr("market_data.requests.get", fail)
    monkeypatch.setattr("market_data.time.sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        _download_chart_ticker("TEST", date(2025,1,2), date(2025,1,3), Settings(download_retries=2))
    assert len(calls) == 4


@pytest.mark.parametrize("value", [None, "NaN", "inf", "0", "-1"])
def test_invalid_kr_close_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        _parse_close(value)


@pytest.mark.parametrize("kind", ["duplicate", "split", "ratio", "suspended", "missing"])
def test_kr_anomalies_are_not_drops(monkeypatch, kind):
    rows = [{"localTradedAt":"2025-01-03","closePrice":"90"}, {"localTradedAt":"2025-01-02","closePrice":"100"}]
    if kind == "duplicate": rows.append(rows[0])
    if kind == "split": rows[0]["closePrice"] = "10"
    if kind == "ratio": rows[0]["fluctuationsRatio"] = "0"
    if kind == "suspended": rows[0]["accumulatedTradingVolume"] = 0
    if kind == "missing": rows = rows[1:]
    monkeypatch.setattr("kospi._chart_price_pair", lambda *args: (99, 90))
    monkeypatch.setattr("kospi._price_rows", lambda *args: rows)
    with pytest.raises(ValueError):
        _download_price_pair("TEST",date(2025,1,3),date(2025,1,2),1)


def test_kr_unfinished_today_uses_previous_completed_session(monkeypatch):
    rows = [{"localTradedAt":d} for d in ["2026-09-23","2026-09-22","2026-09-21"]]
    monkeypatch.setattr("kospi._index_rows", lambda *args: rows)
    result = get_kospi_session_context(now=datetime(2026,9,23,14,tzinfo=KST))
    assert result.session_date == date(2026,9,22)
    with pytest.raises(ValueError, match="completed"):
        get_kospi_session_context(date(2026,9,23), now=datetime(2026,9,23,14,tzinfo=KST))


@pytest.mark.parametrize("day", [(2025,3,11),(2025,11,4)])
def test_0800_kst_works_on_both_sides_of_dst(day):
    result = get_session_context(datetime(*day,8,tzinfo=KST))
    assert result.session_date.day == day[2]-1


def test_us_unscheduled_mourning_holiday():
    result = get_session_context(datetime(2025,1,10,8,tzinfo=KST))
    assert result.session_date == date(2025,1,8)


@pytest.mark.parametrize("valid,missing,status", [(100,0,"OK"),(99,0,"PARTIAL"),(95,5,"PARTIAL"),(94,6,"DATA_INCOMPLETE"),(0,100,"FAILED")])
def test_coverage_states(valid, missing, status):
    assert _status(100, valid, missing) == status


def test_future_us_override_rejected():
    with pytest.raises(ValueError):
        _context_for_override(date(2030,1,2))


def slow_worker(_):
    time.sleep(20)


def test_hard_deadline_terminates_hung_worker():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        execute_market(slow_worker, None, timeout_seconds=0.3)
    assert time.monotonic() - started < 8


def test_incomplete_listing_is_not_100_percent_coverage(monkeypatch):
    monkeypatch.setattr("kospi._request_json", lambda *args: {"totalCount":2,"stocks":[{"itemCode":"1"}]})
    with pytest.raises(ValueError, match="incomplete"):
        load_kospi_constituents()


def test_kr_bad_percent_is_recovered_only_by_matching_dated_chart(monkeypatch):
    rows = [{"localTradedAt":"2025-01-03","closePrice":"90","fluctuationsRatio":"-7"}, {"localTradedAt":"2025-01-02","closePrice":"100"}]
    monkeypatch.setattr("kospi._price_rows", lambda *args: rows)
    monkeypatch.setattr("kospi._chart_price_pair", lambda *args: (100, 90))
    assert _download_price_pair("TEST",date(2025,1,3),date(2025,1,2),1) == (100,90)


def test_us_rejected_candidate_reduces_valid_coverage(monkeypatch):
    import main
    from market_calendar import SessionContext
    monkeypatch.setattr(main,"get_session_context",lambda:SessionContext(date(2025,1,3),date(2025,1,3),date(2025,1,2),False))
    constituents=pd.DataFrame({"ticker":["A","B"],"yahoo_ticker":["A","B"],"company_name":["A","B"]})
    prices=constituents.assign(change_pct=[-10.,0.])
    monkeypatch.setattr(main,"load_constituents",lambda:constituents)
    monkeypatch.setattr(main,"download_market_data",lambda *args:(prices,{}))
    monkeypatch.setattr(main,"verify_candidates",lambda *args:(pd.DataFrame(),{"A":"price disagreement"}))
    result=main.run_us_monitor(None)
    assert result.analyzed_count == 1
    assert result.coverage == .5
    assert result.status == "DATA_INCOMPLETE"


def test_smtp_ambiguous_failure_is_not_retried(monkeypatch):
    import main, smtplib
    from argparse import Namespace
    from market_result import MarketResult
    args=Namespace(notify=True,dry_run=False,session_date_us=None,session_date_kr=None)
    monkeypatch.setattr(main,"_parse_args",lambda:args)
    result=MarketResult(market="US",title="Test",threshold_pct=-10,code_column="ticker",currency="USD",status="OK",total_count=1,analyzed_count=1)
    monkeypatch.setattr(main,"execute_market",lambda *args:result)
    monkeypatch.setattr(main,"_write_outputs",lambda *args:None)
    calls=[]
    def fail(*args):
        calls.append(1)
        raise smtplib.SMTPServerDisconnected("unknown acceptance")
    monkeypatch.setattr(main,"send_gmail_email",fail)
    assert main.main() == 1
    assert len(calls) == 1
