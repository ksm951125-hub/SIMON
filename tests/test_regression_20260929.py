"""Regression: 2026-09-30 07:37 KST production mail (analysis date 2026-09-29).

FICO: Yahoo 840.89 -> 617.87 (-26.52%) while Nasdaq had not published 09-29 yet.
005030 부산주공: KRX base 486 -> 37 (-92.39%) during 정리매매 (no price limit).

Payloads are real captures (tests/fixtures). Two run-time states that no
longer exist were reconstructed from the production log and are marked below:
Nasdaq without the 09-29 row (real payload minus that row), and Yahoo 005030
holding only the 09-29 bar ("최신 2026-09-29" in the log).
No rule in the code is keyed on these symbols.
"""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import kospi
import main
import market_data
from helpers import fixture
from kospi import KospiSessionContext
from market_calendar import KST, SessionContext
from market_result import INFO, NORMAL, WARNING
from providers import cnbc, daum, nasdaq, naver, yahoo
from report import build_combined_subject, build_html_report, build_plain_text_report

PREV, DAY, NEXT = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)


# ------------------------------------------------------------------ FICO
def fico_sources(cnbc_payload):
    yahoo_obs = yahoo.observation_from_bars("FICO", yahoo.parse_us_chart(fixture("yahoo-chart-FICO-2026-09-29.json"), "FICO"),
                                            PREV, DAY)
    nasdaq_obs = nasdaq.from_series("FICO", nasdaq.SOURCE, nasdaq.parse(fixture("nasdaq-FICO-stale-at-run.json")),
                                    PREV, DAY, adjustment="split")
    cnbc_obs = cnbc.parse(cnbc_payload, DAY, PREV)["FICO"]
    return yahoo_obs, nasdaq_obs, cnbc_obs


SAME_EVENING_CNBC = {"FormattedQuoteResult": {"FormattedQuote": [{
    "symbol": "FICO", "last": "617.87", "last_time": "2026-09-29", "previous_day_closing": "840.89",
    "curmktstatus": "POST_MKT", "ExtendedMktQuote": {"type": "POST_MKT", "last": "612.00"}}]}}


def run_us(monkeypatch, cnbc_payload, nasdaq_payload="nasdaq-FICO-stale-at-run.json"):
    yahoo_obs, _, cnbc_obs = fico_sources(cnbc_payload)
    nasdaq_obs = nasdaq.from_series("FICO", nasdaq.SOURCE, nasdaq.parse(fixture(nasdaq_payload)), PREV, DAY,
                                    adjustment="split")
    monkeypatch.setattr(market_data.yahoo, "fetch_us", lambda t, p, d, s=None: yahoo_obs)
    monkeypatch.setattr(market_data.nasdaq, "fetch", lambda t, p, d, s=None: nasdaq_obs)
    monkeypatch.setattr(market_data.cnbc, "fetch_batch", lambda tickers, d, s=None, p=None: {"FICO": cnbc_obs})
    monkeypatch.setattr(main, "get_session_context", lambda: SessionContext(DAY, DAY, PREV, False))
    monkeypatch.setattr(main, "load_constituents", lambda: pd.DataFrame(
        {"ticker": ["FICO"], "company_name": ["Fair Isaac"], "sector": ["IT"], "yahoo_ticker": ["FICO"]}))
    monkeypatch.setattr(main, "fetch_news", lambda *a, **k: {"cause": "", "items": [], "error": None})
    return main.run_us_monitor(None)


def test_fico_detected_and_nasdaq_lag_is_not_a_data_warning(monkeypatch):
    result = run_us(monkeypatch, SAME_EVENING_CNBC)
    fico = result.candidates.iloc[0]
    assert (fico.previous_close, fico.close) == (840.89, 617.87)
    assert fico.change_pct == pytest.approx(-26.5219, abs=1e-4) and fico.detected
    assert fico.validation_status == "CROSS_VALIDATED"  # Yahoo+Nasdaq prev, Yahoo+CNBC close
    assert result.status == INFO and not result.data_warning_issues
    assert [issue.category for issue in result.info_issues] == ["STALE_SECONDARY"]
    assert "Nasdaq" in result.info_issues[0].message and "CNBC" in result.info_issues[0].message


def test_fico_verified_by_nasdaq_when_cnbc_snapshot_rolled(monkeypatch):
    # Replay after Nasdaq published 09-29 and CNBC re-based for 09-30: still verified.
    result = run_us(monkeypatch, fixture("cnbc-rolled-2026-09-30.json"), "nasdaq-FICO-2026-09-30-fetch.json")
    fico = result.candidates.iloc[0]
    assert fico.detected and fico.validation_status == "CROSS_VALIDATED" and "CNBC" in fico.stale_sources
    assert result.status == INFO and not result.data_warning_issues


def test_fico_all_sources_agree_is_normal(monkeypatch):
    result = run_us(monkeypatch, SAME_EVENING_CNBC, "nasdaq-FICO-2026-09-30-fetch.json")
    assert result.candidates.iloc[0].detected and result.status == NORMAL and not result.issues


def test_fico_all_cross_sources_unavailable_is_a_real_warning(monkeypatch):
    # Only now is verification genuinely insufficient: rolled CNBC + stale Nasdaq.
    result = run_us(monkeypatch, fixture("cnbc-rolled-2026-09-30.json"))
    assert result.candidates.iloc[0].detected  # never dropped
    assert result.status == WARNING and result.data_warning_issues[0].category == "UNVERIFIED"


# ------------------------------------------------------------------ 005030
def yahoo_005030_at_run():
    payload = fixture("yahoo-chart-005030-2026-09-29.json")
    result = payload["chart"]["result"][0]
    result["timestamp"] = [int(datetime(2026, 9, 29, 6, 30, tzinfo=timezone.utc).timestamp())]
    result["indicators"]["quote"] = [{"close": [37.0], "volume": [28872090]}]
    return payload


def kr_sources():
    yahoo_obs = yahoo.observation_from_bars("005030", yahoo.parse_kr_chart(yahoo_005030_at_run(), "005030"), PREV, DAY,
                                            stale_checks=False)
    quote = fixture("daum-quote-005030-2026-09-30.json")
    daum_obs = daum.observation_from_quote("005030", quote, PREV, DAY, NEXT)
    days = naver.parse_daily(fixture("naver-price-005030-2026-09-30.json"))
    naver_obs = naver.observation_from_days("005030", days, PREV, DAY)
    days_obs = daum.observation_from_days("005030", fixture("daum-days-005030-2026-09-30.json"), PREV, DAY)
    return yahoo_obs, (daum_obs, daum.parse_state(quote)), (naver_obs, days[DAY], None), days_obs


def run_kr(monkeypatch):
    yahoo_obs, quote, naver_row, days_obs = kr_sources()
    monkeypatch.setattr(kospi.yahoo, "fetch_kr", lambda c, p, d, s=None: yahoo_obs)
    monkeypatch.setattr(kospi.daum, "fetch_quote", lambda c, p, d, n, s=None: quote)
    monkeypatch.setattr(kospi.naver, "fetch_daily", lambda c, p, d, s=None, page_size=10, n=None: naver_row)
    monkeypatch.setattr(kospi.daum, "fetch_days", lambda c, p, d, s=None: days_obs)
    monkeypatch.setattr(main, "get_kospi_session_context", lambda d: KospiSessionContext(DAY, PREV))
    monkeypatch.setattr(main, "load_kospi_constituents", lambda: pd.DataFrame({"code": ["005030"], "company_name": ["부산주공"]}))
    monkeypatch.setattr(main, "optional_validator", lambda: None)
    monkeypatch.setattr(main, "_optional_krx_client", lambda: None)
    return main.run_kr_monitor(None)


def test_005030_raw_prices_and_change():
    yahoo_obs, (daum_obs, state), (naver_obs, _, _), days_obs = kr_sources()
    assert yahoo_obs.close.value == 37 and not yahoo_obs.previous_close.usable  # Yahoo gap on the halted prev day
    assert daum_obs.close.value == 37 and naver_obs.previous_close.value == 486 and days_obs.previous_close.value == 486
    assert state.pre_delisting_trading and state.no_price_limit and state.nxt_tradable is False


def test_005030_special_trading_is_validated_not_a_data_error(monkeypatch):
    result = run_kr(monkeypatch)
    row = result.candidates.iloc[0]
    assert (row.previous_close, row.close) == (486, 37)
    assert row.change_pct == pytest.approx(-92.3868, abs=1e-4) and row.detected
    assert row.special_label == "SPECIAL_TRADING_VALIDATED" and "정리매매" in row.special_reasons
    assert row.state_tags == "정리매매"  # shown next to the name in the mail (display only)
    assert row.validation_status == "FALLBACK_VALIDATED"  # prev from 2 KRX-base sources, close from 3
    assert "근사치" not in row.note
    assert result.status == INFO and not result.data_warning_issues
    assert {issue.category for issue in result.info_issues} == {"SPECIAL_TRADING", "FALLBACK"}


def test_mail_for_2026_09_29_has_no_contradictory_warning(monkeypatch):
    us = run_us(monkeypatch, SAME_EVENING_CNBC)
    kr = run_kr(monkeypatch)
    now = datetime(2026, 9, 30, 7, 37, tzinfo=KST)
    subject = build_combined_subject(us, kr)
    html, plain = build_html_report(us, kr, now), build_plain_text_report(us, kr, now)
    assert subject == "[급락 모니터] S&P500 1개 · KOSPI 1개"
    assert "DATA WARNING" not in html and "DATA WARNING" not in plain
    assert "누락·대체·불일치" not in html
    assert "참고 (정상 처리) · 3건" in html
    assert "특수거래" in html and "FICO" in html and "부산주공" in html
    assert ">정리매매</span>" in html and "부산주공 [정리매매]" in plain
