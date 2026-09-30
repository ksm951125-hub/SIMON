"""End-to-end orchestration, optional official validators, resend safety."""
import hashlib
import json
from argparse import Namespace
from datetime import date, datetime

import pandas as pd
import pytest

import main
from config import Settings
from kospi import KospiSessionContext, download_kospi_market_data
from market_calendar import KST
from market_result import ERROR, INFO, NORMAL, WARNING, Issue, MarketResult
from report import build_combined_subject, build_html_report, build_plain_text_report

FAST = Settings(download_retries=1, retry_backoff_seconds=0, missing_retry_pause_seconds=0, availability_wait_seconds=0,
                max_workers=2)
PREV, DAY = date(2026, 9, 28), date(2026, 9, 29)


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


def result(market, status, **kw):
    title, column, currency, threshold = (("S&P 500 급락 모니터", "ticker", "USD", -10.0) if market == "US"
                                          else ("KOSPI 급락 모니터", "code", "KRW", -7.0))
    return MarketResult(market=market, title=title, threshold_pct=threshold, code_column=column,
                        currency=currency, status=status, **kw)


def test_krx_official_close_is_authoritative_when_configured(monkeypatch):
    class FakeKrx:
        def rows(self, endpoint, day):
            close = "100" if day == PREV else "92"
            return [{"ISU_CD": "KR7005930003", "TDD_CLSPRC": close}]

    from test_kospi import fake_kr, normal
    fake_kr(monkeypatch, {"005930": normal(100, 94)})
    collection = download_kospi_market_data(pd.DataFrame({"code": ["005930"], "company_name": ["삼성전자"]}),
                                            KospiSessionContext(DAY, PREV), FAST, krx_client=FakeKrx())
    row = collection.prices.iloc[0]
    assert row.mismatch and row.detected and row.close == 92 and row.change_pct == pytest.approx(-8.0)
    assert row.severity == "WARNING"


def test_krx_outage_is_a_source_note_only(monkeypatch):
    class Broken:
        def rows(self, endpoint, day):
            raise RuntimeError("KRX HTTP 401")

    from test_kospi import fake_kr, normal
    fake_kr(monkeypatch, {"005930": normal(100, 99)})
    collection = download_kospi_market_data(pd.DataFrame({"code": ["005930"], "company_name": ["삼성전자"]}),
                                            KospiSessionContext(DAY, PREV), FAST, krx_client=Broken())
    assert len(collection.prices) == 1 and any("KRX HTTP 401" in note for note in collection.notices)


def test_krx_client_requires_key(monkeypatch):
    from krx import KrxClient
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="KRX_API_KEY"):
        KrxClient()
    assert main._optional_krx_client() is None


def test_kis_request_uses_krx_market_unadjusted_daily(monkeypatch):
    from kis_validation import KisValidator
    monkeypatch.setenv("KIS_APP_KEY", "testkey")
    monkeypatch.setenv("KIS_APP_SECRET", "testsecret")
    monkeypatch.setattr("kis_validation.requests.post", lambda *a, **k: Response({"access_token": "testtoken"}))
    calls = []

    def get(url, **kw):
        calls.append(kw)
        return Response({"rt_cd": "0", "output2": [{"stck_bsop_date": "20260928", "stck_clpr": "100"},
                                                   {"stck_bsop_date": "20260929", "stck_clpr": "93"}]})

    monkeypatch.setattr("kis_validation.requests.get", get)
    validator = KisValidator()
    validator.validate("38380K", PREV, DAY, 100, 93)
    params = calls[0]["params"]
    assert params["FID_COND_MRKT_DIV_CODE"] == "J" and params["FID_ORG_ADJ_PRC"] == "1"
    with pytest.raises(ValueError, match="excluded"):
        validator.validate("38380K", PREV, DAY, 100, 92)


@pytest.mark.parametrize("us_status,kr_status,code", [(NORMAL, NORMAL, 0), (INFO, WARNING, 0), (ERROR, NORMAL, 1)])
def test_exit_code_only_fails_on_market_failure(monkeypatch, us_status, kr_status, code):
    results = {"run_us_monitor": result("US", us_status, total_count=1, analyzed_count=1),
               "run_kr_monitor": result("KR", kr_status, total_count=1, analyzed_count=1)}
    monkeypatch.setattr(main, "execute_market", lambda function, argument: results[function.__name__])
    monkeypatch.setattr(main, "_write_outputs", lambda *a: None)
    assert main.run(Namespace(dry_run=True, notify=False, market="all")) == code


def test_single_market_dry_run_marks_other_market_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "execute_market",
                        lambda function, argument: calls.append(function.__name__) or result("US", NORMAL, total_count=1, analyzed_count=1))
    captured = {}
    monkeypatch.setattr(main, "_write_outputs", lambda us, kr, plain, html, now: captured.update(kr=kr, plain=plain))
    assert main.run(Namespace(dry_run=True, notify=False, market="us")) == 0
    assert calls == ["run_us_monitor"] and captured["kr"].status == "SKIPPED"
    assert "상태: SKIPPED" in captured["plain"]


def test_subject_formats():
    candidates = pd.DataFrame([{"ticker": "X", "company_name": "X", "previous_close": 100, "close": 80, "change_pct": -20}] * 2)
    kr_c = pd.DataFrame([{"code": "000001", "company_name": "A", "previous_close": 100, "close": 90, "change_pct": -10}] * 5)
    us = result("US", NORMAL, total_count=503, analyzed_count=503, candidates=candidates)
    kr = result("KR", NORMAL, total_count=944, analyzed_count=944, candidates=kr_c)
    assert build_combined_subject(us, kr) == "[급락 모니터] S&P500 2개 · KOSPI 5개"
    us_i = result("US", INFO, total_count=503, analyzed_count=503, candidates=candidates,
                  issues=[Issue(INFO, "STALE_SECONDARY", "Nasdaq 지연 → CNBC 검증", "X")])
    assert build_combined_subject(us_i, kr) == "[급락 모니터] S&P500 2개 · KOSPI 5개"  # INFO never tags
    us_w = result("US", WARNING, total_count=503, analyzed_count=502, candidates=candidates,
                  issues=[Issue(WARNING, "MISSING", "누락", "Y")])
    kr_w = result("KR", WARNING, total_count=944, analyzed_count=942, candidates=kr_c,
                  issues=[Issue(WARNING, "MISSING", "누락", "1"), Issue(WARNING, "MISSING", "누락", "2")])
    assert build_combined_subject(us_w, kr_w) == ("[급락 모니터][DATA WARNING] S&P500 2개 · KOSPI 5개 | "
                                                  "데이터 누락 US 1건 / KR 2건")
    failed = result("US", ERROR, error="RuntimeError: outage", issues=[Issue(ERROR, "SOURCE", "RuntimeError: outage")])
    assert build_combined_subject(failed, kr, "[TEST] ") == ("[TEST][급락 모니터][ERROR US] S&P500 확인필요 · KOSPI 5개 | "
                                                              "데이터 누락 US 확인불가 / KR 0건")


def test_report_shows_listing_valid_coverage_fallback_missing_detected():
    us = result("US", WARNING, total_count=503, analyzed_count=502, fallback_count=3,
                session_date=date(2026, 9, 28), previous_session_date=date(2026, 9, 25),
                missing={"XYZ XYZ Corp": "Yahoo 누락 | Nasdaq 누락"},
                issues=[Issue(WARNING, "MISSING", "Yahoo 누락 | Nasdaq 누락", "XYZ", "XYZ Corp")])
    kr = result("KR", NORMAL, total_count=944, analyzed_count=944)
    now = datetime(2026, 9, 29, 7, 30, tzinfo=KST)
    plain, html = build_plain_text_report(us, kr, now), build_html_report(us, kr, now)
    for label in ("Listing: 503", "Valid Prices: 502", "Coverage: 99.8%", "Fallback: 3", "Missing: 1", "Detected: 0개"):
        assert label in plain
    for label in ("Listing", "Valid Prices", "Coverage", "Fallback", "Missing", "Detected", "99.8%", "XYZ Corp"):
        assert label in html
    assert "2026-09-25" in html and "2026-09-28" in html
    assert "DATA WARNING: 데이터 누락 1건" in html and "DATA WARNING · 1건" in html


def test_warning_text_matches_actual_cause_only():
    # Missing 0 / Fallback 0 / mismatch 0 but a near-threshold symbol lacks a 2nd source.
    us = result("US", WARNING, total_count=503, analyzed_count=503,
                issues=[Issue(WARNING, "UNVERIFIED", "단일소스", "AAA")])
    kr = result("KR", NORMAL, total_count=944, analyzed_count=944)
    html = build_html_report(us, kr, datetime(2026, 9, 30, 7, 30, tzinfo=KST))
    assert "2차 검증 불가 1건" in html
    for wrong in ("누락·대체·불일치", "데이터 누락 1건", "대체 데이터", "소스 간 가격 불일치"):
        assert wrong not in html


def test_normal_and_info_markets_have_no_data_warning():
    us = result("US", INFO, total_count=503, analyzed_count=503,
                issues=[Issue(INFO, "STALE_SECONDARY", "Nasdaq 지연 → CNBC로 검증", "FICO", "Fair Isaac")])
    kr = result("KR", NORMAL, total_count=944, analyzed_count=944)
    now = datetime(2026, 9, 30, 7, 30, tzinfo=KST)
    html, plain = build_html_report(us, kr, now), build_plain_text_report(us, kr, now)
    assert "DATA WARNING" not in html and "DATA WARNING" not in plain
    assert "참고 (정상 처리) · 1건" in html and "&#8505; INFO" in html and "&#9679; NORMAL" in html


def test_error_market_with_partial_data_still_lists_found_drops():
    frame = pd.DataFrame([dict(code="000001", company_name="부분탐지", previous_close=100, close=90, change_pct=-10,
                               source="Yahoo")])
    kr = result("KR", ERROR, total_count=944, analyzed_count=500, candidates=frame)
    us = result("US", NORMAL, total_count=1, analyzed_count=1)
    html = build_html_report(us, kr, datetime(2026, 9, 29, 8, tzinfo=KST))
    assert "부분탐지" in html and "DATA ERROR" in html


def test_resend_rejects_legacy_or_mismatched_artifacts(tmp_path):
    from saved_report import validate_saved_report
    path = tmp_path / "report.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Legacy"):
        validate_saved_report(path, "p", "h", "subject")
    payload = {"report_schema": "regular-close-v4",
               "report_hashes": {"plain": hashlib.sha256(b"p").hexdigest(), "html": hashlib.sha256(b"h").hexdigest()},
               "markets": [{"market": "US", "status": "OK"}, {"market": "KR", "status": "WARNING"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="DATA WARNING"):
        validate_saved_report(path, "p", "h", "subject")
    with pytest.raises(ValueError, match="mismatch"):
        validate_saved_report(path, "p", "old HTML", "DATA WARNING")
    validate_saved_report(path, "p", "h", "[급락 모니터][DATA WARNING] ...")


def test_written_json_matches_resend_schema(monkeypatch, tmp_path):
    from saved_report import REPORT_SCHEMA
    monkeypatch.setattr(main, "SETTINGS", Settings(output_dir=tmp_path))
    us = result("US", NORMAL, total_count=1, analyzed_count=1)
    kr = result("KR", NORMAL, total_count=1, analyzed_count=1)
    main._write_outputs(us, kr, "plain", "<html>", datetime(2026, 9, 29, 8, tzinfo=KST))
    payload = json.loads(next(tmp_path.glob("combined-*.json")).read_text(encoding="utf-8"))
    assert payload["report_schema"] == REPORT_SCHEMA == main.REPORT_SCHEMA
    assert {m["market"] for m in payload["markets"]} == {"US", "KR"}
