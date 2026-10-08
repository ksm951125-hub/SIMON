"""Fault injection: no single bad response, symbol, provider or report-building
error may cost the reader the daily mail."""
import smtplib
import time
from argparse import Namespace
from dataclasses import replace
from datetime import date, datetime

import pandas as pd
import pytest

import kospi
import main
import market_data
import notifier
from helpers import FAST, fake_us, obs
from market_calendar import KST
from market_data import align_split_basis, download_market_data
from market_result import ERROR, MarketResult, classify_status, explain_error
from net import Budget, FailFast
from news import fetch_news_many
from providers import cnbc, daum, naver, yahoo
from providers.base import INVALID_PRICE, PriceObservation, never_raises
from report import SIZE_STEPS, build_fallback_report, build_html_report
from test_kospi import fake_kr, normal, run_kr, state
from test_market_data import constituents
from test_providers import Response, chart

PREV, DAY = date(2026, 10, 7), date(2026, 10, 8)
NO_RETRY = replace(FAST, availability_retries=0)


# ------------------------------------------------------------------ adapters never raise
@pytest.mark.parametrize("payload", [None, [], "garbage", {"chart": None}, {"chart": {"result": ["x"]}},
                                     {"chart": {"result": [{"meta": "bad"}]}},
                                     {"chart": {"result": [{"meta": {"symbol": "T"}, "timestamp": [None],
                                                            "indicators": {"quote": [{"close": ["x"]}]}}]}}])
def test_yahoo_malformed_payload_gives_failed_observation(monkeypatch, payload):
    monkeypatch.setattr("net.requests.get", lambda *a, **k: Response(payload))
    observation = yahoo.fetch_us("T", PREV, DAY, FAST)
    assert isinstance(observation, PriceObservation) and not observation.close.usable


def test_yahoo_malformed_split_events_do_not_break_the_symbol(monkeypatch):
    data = chart(days=(PREV, DAY))
    data["chart"]["result"][0]["events"] = {"splits": [1, 2]}
    monkeypatch.setattr("net.requests.get", lambda *a, **k: Response(data))
    observation = yahoo.fetch_us("TEST", PREV, DAY, FAST)
    assert observation.close.usable and observation.split_factor == 1.0


def test_daum_odd_payload_returns_pair_without_raising(monkeypatch):
    payload = {"symbolCode": "A005930", "tradeDate": "20261008", "stockState": ["bad"], "regularTradePrice": None}
    monkeypatch.setattr("net.requests.get", lambda *a, **k: Response(payload))
    result = daum.fetch_quote("005930", PREV, DAY, None, FAST)
    assert isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], PriceObservation)


def test_naver_non_list_response_returns_triple(monkeypatch):
    monkeypatch.setattr("providers.naver.json_get", lambda *a, **k: {"not": "a list"})
    result = naver.fetch_daily("005930", PREV, DAY, FAST)
    assert len(result) == 3 and not result[0].close.usable and result[1] is None and result[2] is None


def test_cnbc_one_malformed_quote_does_not_discard_the_batch():
    payload = {"FormattedQuoteResult": {"FormattedQuote": [
        None, 5, {"symbol": "AAA", "code": 0, "last": "10", "last_time": "2026-10-08", "curmktstatus": "POST_MKT"},
        {"symbol": "BBB", "code": 0, "last": "x", "last_time": "2026-10-08"},
        {"symbol": "CCC", "code": 0, "last": "5", "last_time": object()}]}}
    parsed = cnbc.parse(payload, DAY, PREV)
    assert parsed["AAA"].close.value == 10.0
    assert "BBB" in parsed and not parsed["BBB"].close.usable and "CCC" in parsed


def test_never_raises_decorator_wraps_plain_and_tuple_results():
    @never_raises("X")
    def plain(symbol):
        raise RuntimeError("boom")

    @never_raises("X", extras=2)
    def triple(symbol):
        raise ValueError("boom")

    assert plain("A").close.status == INVALID_PRICE and "RuntimeError" in plain("A").close.detail
    observation, second, third = triple("B")
    assert observation.close.status == INVALID_PRICE and second is None and third is None


# ------------------------------------------------------------------ fail-fast / budgets
def test_failfast_trips_on_consecutive_failures_and_resets_on_success():
    breaker = FailFast("Src", 3)
    for ok in (False, False, True, False, False):
        breaker.record(ok)
    assert breaker.blocked() is None  # never 3 in a row
    breaker.record(False)
    assert "연속 3회" in breaker.blocked() and "장애" in breaker.summary()


def test_failfast_budget_exhaustion_blocks():
    breaker = FailFast("Src", 99, Budget(0))
    assert "시간 예산 소진" in breaker.blocked() and breaker.skipped == 1


def test_retry_waits_are_skipped_when_budget_cannot_cover_them():
    from net import retry_until_available
    sleeps = []
    results = retry_until_available({"a": False}, lambda keys: {k: False for k in keys}, bool,
                                    replace(FAST, availability_wait_seconds=60, missing_retry_pause_seconds=3),
                                    "t", Budget(10))
    assert results == {"a": False} and not sleeps


def test_yahoo_down_is_cut_short_and_other_sources_take_over(monkeypatch):
    tickers = [f"T{i:03d}" for i in range(200)]
    rows = {t: (100, 99) for t in tickers}
    fake_us(monkeypatch, yahoo={}, nasdaq=rows, cnbc=rows)
    calls = []
    inner = market_data.yahoo.fetch_us

    def spy(ticker, previous, current, settings=None):
        calls.append(ticker)
        return inner(ticker, previous, current, settings)

    monkeypatch.setattr(market_data.yahoo, "fetch_us", spy)
    result = download_market_data(constituents(*tickers), DAY, PREV, NO_RETRY)
    threshold = FAST.provider_failure_threshold
    assert len(calls) <= 2 * (threshold + FAST.max_workers)  # not 400
    assert len(result.prices) == 200 and not result.missing
    assert any("장애" in line for line in result.health) and any("장애 감지" in note for note in result.notices)


def test_exhausted_primary_budget_falls_back_without_calling_yahoo(monkeypatch):
    rows = {f"T{i}": (100, 99) for i in range(20)}
    fake_us(monkeypatch, yahoo=rows, nasdaq=rows, cnbc=rows)
    calls = []
    monkeypatch.setattr(market_data.yahoo, "fetch_us",
                        lambda t, p, d, s=None: calls.append(t) or obs(t, "Yahoo", 100, 99, prev_date=p, day=d))
    result = download_market_data(constituents(*rows), DAY, PREV, replace(NO_RETRY, primary_budget_seconds=0))
    assert not calls and len(result.prices) == 20 and any("소진" in line for line in result.health)


# ------------------------------------------------------------------ per-symbol isolation
def test_exception_from_a_provider_call_is_isolated(monkeypatch):
    rows = {"AAA": (100, 99), "BAD": (100, 99), "CCC": (100, 99)}
    fake_us(monkeypatch, yahoo=rows, nasdaq=rows, cnbc=rows)
    inner = market_data.yahoo.fetch_us

    def boom(ticker, previous, current, settings=None):
        if ticker == "BAD":
            raise RuntimeError("provider bug")
        return inner(ticker, previous, current, settings)

    monkeypatch.setattr(market_data.yahoo, "fetch_us", boom)
    result = download_market_data(constituents(*rows), DAY, PREV, NO_RETRY)
    assert set(result.prices.ticker) == {"AAA", "BAD", "CCC"}  # BAD recovered through the other sources


def test_exception_while_assessing_one_symbol_does_not_fail_the_market(monkeypatch):
    rows = {f"T{i}": (100, 99) for i in range(5)}
    fake_us(monkeypatch, yahoo=rows, nasdaq=rows, cnbc=rows)
    inner = market_data.assess

    def flaky(symbol, *args, **kwargs):
        if symbol == "T2":
            raise ZeroDivisionError("bad data")
        return inner(symbol, *args, **kwargs)

    monkeypatch.setattr(market_data, "assess", flaky)
    result = download_market_data(constituents(*rows), DAY, PREV, NO_RETRY)
    assert len(result.prices) == 4 and "내부 처리 오류" in result.missing["T2"]


def test_kr_exception_while_assessing_one_stock_does_not_fail_the_market(monkeypatch):
    inner = kospi.assess_kr_move

    def flaky(previous, close, base, market_state, session_date):
        if previous == 1234:
            raise KeyError("odd stock")
        return inner(previous, close, base, market_state, session_date)

    monkeypatch.setattr(kospi, "assess_kr_move", flaky)
    result = run_kr(monkeypatch, {"000001": normal(1234, 1200), "000002": normal(1000, 990)})
    assert list(result.prices.code) == ["000002"] and "내부 처리 오류" in result.missing["000001"]


def test_kr_yahoo_outage_is_covered_by_daum_and_naver(monkeypatch):
    rows = {f"{i:06d}": dict(daum=(1000, 990), naver=(1000, 1100, 500), state=state()) for i in range(1, 60)}
    result = run_kr(monkeypatch, rows)
    assert len(result.prices) == 59 and not result.missing
    assert set(result.prices.close) == {990.0}  # Daum's regular close, never Naver's integrated 1,100
    assert any("Yahoo" in line and "장애" in line for line in result.health)


def test_kr_all_sources_down_is_a_market_error_not_an_exception(monkeypatch):
    result = run_kr(monkeypatch, {f"{i:06d}": {} for i in range(1, 30)})
    assert result.prices.empty and len(result.missing) == 29


# ------------------------------------------------------------------ split basis
def anchor(prev, close, factor):
    observation = obs("T", "Yahoo", prev, close, prev_date=PREV, day=DAY)
    observation.split_factor = factor
    return observation


@pytest.mark.parametrize("factor,raw_prev", [(2.0, 200.0), (0.2, 20.0), (1.067, 106.7)])
def test_unadjusted_previous_close_is_rebased_on_a_split_day(factor, raw_prev):
    observations = [anchor(100.0, 99.0, factor), obs("T", "Nasdaq", raw_prev, 99.0, prev_date=PREV, day=DAY)]
    aligned = align_split_basis(observations)
    assert aligned[1].previous_close.value == pytest.approx(100.0) and "분할비율" in aligned[1].note


def test_split_rebase_leaves_adjusted_and_unrelated_values_alone():
    adjusted = obs("T", "Nasdaq", 100.0, 99.0, prev_date=PREV, day=DAY)
    unrelated = obs("T", "CNBC", 137.0, 99.0, prev_date=PREV, day=DAY)
    aligned = align_split_basis([anchor(100.0, 99.0, 2.0), adjusted, unrelated])
    assert aligned[1].previous_close.value == 100.0 and aligned[2].previous_close.value == 137.0
    # No split event: nothing is touched.
    assert align_split_basis([anchor(100.0, 99.0, 1.0), obs("T", "N", 200.0, 99.0, prev_date=PREV, day=DAY)])[1] \
        .previous_close.value == 200.0


def test_split_day_is_not_reported_as_a_crash(monkeypatch):
    # 2:1 split: Yahoo history adjusted (100), Nasdaq still shows the raw 200 -> 99.
    yahoo_obs = anchor(100.0, 99.0, 2.0)
    nasdaq_obs = obs("SPLT", "Nasdaq", 200.0, 99.0, prev_date=PREV, day=DAY)
    monkeypatch.setattr(market_data.yahoo, "fetch_us", lambda t, p, d, s=None: yahoo_obs)
    monkeypatch.setattr(market_data.nasdaq, "fetch", lambda t, p, d, s=None: nasdaq_obs)
    monkeypatch.setattr(market_data.cnbc, "fetch_batch",
                        lambda tickers, d, s=None, p=None: {t: obs(t, "CNBC", None, "down", prev_date=p or d, day=d)
                                                            for t in tickers})
    row = download_market_data(constituents("SPLT"), DAY, PREV, NO_RETRY).prices.iloc[0]
    assert not row.detected and not row.mismatch and row.change_pct == pytest.approx(-1.0)
    assert row.validation_status == "CROSS_VALIDATED"


# ------------------------------------------------------------------ status & wording
def test_many_missing_symbols_are_summarized_but_kept_in_the_dict():
    names = {f"T{i:03d}": f"Corp {i}" for i in range(60)}
    missing = {code: "Yahoo 지연: 데이터 없음" for code in names}
    keyed, issues = main._missing_issues(missing, names)
    assert len(keyed) == 60 and len(issues) == 1 and "60종목" in issues[0].message
    keyed, issues = main._missing_issues({"A": "x", "B": "y"}, {"A": "A", "B": "B"})
    assert len(issues) == 2
    keyed, issues = main._missing_issues({"A": "x", "Z": "t"}, {}, transitions={"Z": "티커 변경 추정"})
    assert {issue.category for issue in issues} == {"MISSING", "LISTING_CHANGE"}


def test_error_market_always_has_an_error_reason():
    result = MarketResult("US", "S&P 500 급락 모니터", -10, "ticker", "USD", total_count=503, analyzed_count=0)
    result.status = classify_status(503, 0, [])
    explain_error(result)
    assert result.status == ERROR and result.issues[0].level == ERROR and "Coverage 0.0%" in result.issues[0].message
    explain_error(result)
    assert sum(issue.level == ERROR for issue in result.issues) == 1


def test_kr_holiday_notice_reanalyzes_latest_session(monkeypatch):
    rows = [{"localTradedAt": d} for d in ("2026-10-08", "2026-10-07", "2026-10-06")]
    monkeypatch.setattr(kospi, "_json", lambda url, params, settings=None: rows if params["page"] == 1 else [])
    context = kospi.get_kospi_session_context(now=datetime(2026, 10, 10, 7, 30, tzinfo=KST))  # day after Hangul Day
    assert (context.session_date, context.previous_session_date) == (date(2026, 10, 8), date(2026, 10, 7))
    assert "휴장" in context.notice and "2026-10-09" in context.notice
    normal_day = kospi.get_kospi_session_context(now=datetime(2026, 10, 9, 7, 30, tzinfo=KST))
    assert normal_day.notice is None


# ------------------------------------------------------------------ optional components
def test_optional_validator_failure_does_not_fail_kr(monkeypatch):
    monkeypatch.setattr(main, "optional_validator", lambda: (_ for _ in ()).throw(RuntimeError("KIS 키 없음")))
    assert main._optional_validator() is None
    monkeypatch.setenv("KRX_API_KEY", "key")
    monkeypatch.setattr("krx.KrxClient", lambda **kw: (_ for _ in ()).throw(RuntimeError("init")), raising=False)
    assert main._optional_krx_client() is None


def test_kis_outage_is_a_note_but_a_disagreement_is_a_warning(monkeypatch):
    class Validator:
        def __init__(self, error):
            self.error = error

        def validate(self, *args):
            raise self.error

    universe = pd.DataFrame({"code": ["000001"], "company_name": ["A"]})
    context = kospi.KospiSessionContext(DAY, PREV)
    fake_kr(monkeypatch, {"000001": normal(1000, 900)})
    outage = kospi.download_kospi_market_data(universe, context, FAST, validator=Validator(RuntimeError("HTTP 500")))
    assert outage.prices.iloc[0].severity != "WARNING" and "KIS 검증 불가" in outage.prices.iloc[0].note
    mismatch = kospi.download_kospi_market_data(universe, context, FAST, validator=Validator(ValueError("종가 불일치")))
    assert mismatch.prices.iloc[0].severity == "WARNING" and mismatch.prices.iloc[0].mismatch


# ------------------------------------------------------------------ news
def test_news_is_bounded_and_never_raises():
    def slow(ticker, name, session, settings):
        if ticker == "SLOW":
            time.sleep(1.5)
        if ticker == "BOOM":
            raise RuntimeError("x")
        return {"cause": f"c-{ticker}", "items": [], "error": None}

    started = time.monotonic()
    results = fetch_news_many([("OK", "o"), ("SLOW", "s"), ("BOOM", "b")], DAY, budget_seconds=0.3, fetch=slow)
    assert time.monotonic() - started < 1.2
    assert results[0]["cause"] == "c-OK" and "시간 초과" in results[1]["error"] and "RuntimeError" in results[2]["error"]
    assert fetch_news_many([], DAY) == []


# ------------------------------------------------------------------ mail delivery
class FlakySMTP:
    instances = 0
    sent = 0
    fail_connects = 0
    login_error = None
    send_error = None

    def __init__(self, host, port, timeout, context):
        type(self).instances += 1
        if type(self).instances <= type(self).fail_connects:
            raise ConnectionResetError("connection reset")

    def login(self, address, password):
        if type(self).login_error:
            raise type(self).login_error

    def send_message(self, message, from_addr, to_addrs):
        if type(self).send_error:
            raise type(self).send_error
        type(self).sent += 1
        return {}

    def quit(self):
        pass


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdabcdabcdabcd")
    monkeypatch.setenv("ALERT_EMAIL_RECIPIENT", "r@naver.com")
    monkeypatch.setattr("notifier.time.sleep", lambda *_: None)
    monkeypatch.setattr("notifier.smtplib.SMTP_SSL", FlakySMTP)
    for name, value in dict(instances=0, sent=0, fail_connects=0, login_error=None, send_error=None).items():
        setattr(FlakySMTP, name, value)
    return FlakySMTP


def test_transient_connect_failures_are_retried_and_sent_once(smtp):
    smtp.fail_connects = 2
    notifier.send_gmail_email("body", "subject")
    assert smtp.instances == 3 and smtp.sent == 1


def test_connect_failure_gives_up_after_bounded_attempts(smtp):
    smtp.fail_connects = 99
    with pytest.raises(notifier.NotificationError, match="3회 실패"):
        notifier.send_gmail_email("body", "subject")
    assert smtp.instances == 3 and smtp.sent == 0


def test_wrong_password_is_not_retried(smtp):
    smtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with pytest.raises(smtplib.SMTPAuthenticationError):
        notifier.send_gmail_email("body", "subject")
    assert smtp.instances == 1


def test_a_failed_send_is_never_retried_so_no_duplicate_mail(smtp):
    smtp.send_error = smtplib.SMTPDataError(451, b"try again")
    with pytest.raises(smtplib.SMTPDataError):
        notifier.send_gmail_email("body", "subject")
    assert smtp.instances == 1 and smtp.sent == 0


# ------------------------------------------------------------------ report building
def big_result(market, count):
    code = "ticker" if market == "US" else "code"
    frame = pd.DataFrame([{code: f"X{i:04d}", "company_name": f"테스트 {i}", "previous_close": 1000.0 + i,
                           "close": 800.0 + i, "change_pct": -20.0 - i / 100, "source": "Yahoo", "note": "",
                           "validation_status": "CROSS_VALIDATED", "special_label": ""} for i in range(count)])
    return MarketResult(market, "T", -10 if market == "US" else -7, code, "USD" if market == "US" else "KRW",
                        status="NORMAL", total_count=900, analyzed_count=900, candidates=frame,
                        session_date=DAY, previous_session_date=PREV)


@pytest.mark.parametrize("count", [10, 100, 400])
def test_html_stays_under_the_gmail_clipping_limit(count):
    html = build_html_report(big_result("US", min(count, 120)), big_result("KR", count), datetime(2026, 10, 9, 7, 30, tzinfo=KST))
    assert len(html.encode("utf-8")) <= 90_000
    assert "X0000" in html  # the largest drops are always shown
    if count > 10:
        assert "외 " in html


def test_html_tables_are_balanced_and_top_rows_keep_the_rich_layout():
    html = build_html_report(big_result("US", 30), big_result("KR", 30), datetime(2026, 10, 9, 7, 30, tzinfo=KST))
    assert html.count("<table") == html.count("</table>") and html.count("<tr") == html.count("</tr>")
    assert html.count("<td") == html.count("</td>")
    assert html.count('class="candidate-row"') == 20  # TOP_ROWS per market
    assert "외 20개 종목 (전체 목록)" in html and html.count("&#9660;&nbsp;") == 60


def test_size_steps_end_with_no_tail_and_few_issues():
    assert SIZE_STEPS[-1] == (0, 2) and SIZE_STEPS[0][0] >= 100


def test_fallback_report_survives_broken_results():
    broken = MarketResult("US", "S&P 500 급락 모니터", -10, "ticker", "USD", status="WARNING",
                          candidates=pd.DataFrame([{"ticker": "AAA"}]))
    plain, html, subject = build_fallback_report(broken, big_result("KR", 3), datetime(2026, 10, 9, 7, 30, tzinfo=KST),
                                                 KeyError("x"), "[TEST] ")
    assert "간이 리포트" in subject and subject.startswith("[TEST]") and "DATA WARNING" in subject
    assert "X0000" in plain and "<pre" in html and "KeyError" in plain


def run_with(monkeypatch, *, html_error=False, write_error=False):
    results = {"run_us_monitor": big_result("US", 2), "run_kr_monitor": big_result("KR", 3)}
    monkeypatch.setattr(main, "execute_market", lambda function, argument: results[function.__name__])
    if html_error:
        monkeypatch.setattr(main, "build_html_report", lambda *a: (_ for _ in ()).throw(ValueError("render bug")))
    if write_error:
        monkeypatch.setattr(main, "_write_outputs", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    else:
        monkeypatch.setattr(main, "_write_outputs", lambda *a: None)
    sent = []
    monkeypatch.setattr(main, "send_gmail_email", lambda *a, **k: sent.append(a))
    code = main.run(Namespace(dry_run=False, notify=True, market="all", session_date_us=None, session_date_kr=None))
    return code, sent


def test_report_rendering_bug_still_sends_a_simplified_mail(monkeypatch):
    code, sent = run_with(monkeypatch, html_error=True)
    assert code == 0 and len(sent) == 1 and "간이 리포트" in sent[0][1] and "X0000" in sent[0][0]


def test_output_file_failure_does_not_block_the_mail(monkeypatch):
    code, sent = run_with(monkeypatch, write_error=True)
    assert code == 0 and len(sent) == 1 and "간이" not in sent[0][1]


# ------------------------------------------------------------------ workflow safety-net marker
def run_marker(monkeypatch, tmp_path, *, notify=True, send_fails=False):
    from config import Settings
    monkeypatch.setattr(main, "SETTINGS", Settings(output_dir=tmp_path))
    results = {"run_us_monitor": big_result("US", 1), "run_kr_monitor": big_result("KR", 1)}
    monkeypatch.setattr(main, "execute_market", lambda function, argument: results[function.__name__])
    monkeypatch.setattr(main, "_write_outputs", lambda *a: None)

    def send(*args, **kwargs):
        if send_fails:
            raise smtplib.SMTPDataError(451, b"later")

    monkeypatch.setattr(main, "send_gmail_email", send)
    args = Namespace(dry_run=not notify, notify=notify, market="all", session_date_us=None, session_date_kr=None)
    try:
        main.run(args)
    except main.NotificationError:
        pass
    return tmp_path / main.MAIL_SENT_FLAG


def test_marker_written_only_after_a_successful_send(monkeypatch, tmp_path):
    assert run_marker(monkeypatch, tmp_path).exists()
    assert not run_marker(monkeypatch, tmp_path / "dry", notify=False).exists()
    assert not run_marker(monkeypatch, tmp_path / "failed", send_fails=True).exists()


# ------------------------------------------------------------------ end to end through the real adapters
def http_router(monkeypatch, handler):
    """Replace the HTTP layer: handler(url, params) -> payload, or raises."""
    monkeypatch.setattr("net.time.sleep", lambda *_: None)
    monkeypatch.setattr("net.requests.get", lambda url, params=None, headers=None, timeout=None: Response(handler(url, params or {})))


def us_http(down=()):
    def handler(url, params):
        import requests
        if "finance.yahoo.com/v8/finance/chart" in url:
            if "yahoo" in down:
                raise requests.ConnectionError("yahoo down")
            return chart(ticker=url.rsplit("/", 1)[1], days=(PREV, DAY), closes=(100.0, 99.0), volumes=(1000, 1000))
        if "api.nasdaq.com" in url:
            if "nasdaq" in down:
                raise requests.ConnectionError("nasdaq down")
            return {"data": {"tradesTable": {"rows": [{"date": "10/08/2026", "close": "$99.00"},
                                                      {"date": "10/07/2026", "close": "$100.00"}]}}}
        if "quote.cnbc.com" in url:
            if "cnbc" in down:
                raise requests.ConnectionError("cnbc down")
            return {"FormattedQuoteResult": {"FormattedQuote": [
                {"symbol": symbol, "code": 0, "countryCode": "US", "currencyCode": "USD", "last": "99.00",
                 "last_time": "2026-10-08", "previous_day_closing": "100.00", "curmktstatus": "POST_MKT",
                 "ExtendedMktQuote": {"type": "POST_MKT"}} for symbol in params["symbols"].split("|")]}}
        raise AssertionError(url)
    return handler


@pytest.mark.parametrize("down,expected_status", [
    ((), "CROSS_VALIDATED"), (("yahoo",), "FALLBACK_VALIDATED"), (("nasdaq",), "CROSS_VALIDATED"),
    (("cnbc",), "CROSS_VALIDATED"), (("nasdaq", "cnbc"), "PRIMARY_ONLY"), (("yahoo", "cnbc"), "FALLBACK_UNVERIFIED")])
def test_us_run_through_real_adapters_survives_any_single_or_double_outage(monkeypatch, down, expected_status):
    tickers = [f"T{i:03d}" for i in range(40)]
    http_router(monkeypatch, us_http(down))
    result = download_market_data(constituents(*tickers), DAY, PREV, NO_RETRY)
    assert len(result.prices) == 40 and not result.missing
    assert set(result.prices.validation_status) == {expected_status}
    assert result.prices.change_pct.round(2).eq(-1.0).all()


def test_us_without_yahoo_and_nasdaq_nothing_is_invented(monkeypatch):
    # CNBC's previous close is supporting-only (it can be re-based for ex-dividends): alone it is no price.
    http_router(monkeypatch, us_http(("yahoo", "nasdaq")))
    result = download_market_data(constituents(*[f"T{i}" for i in range(30)]), DAY, PREV, NO_RETRY)
    assert result.prices.empty and len(result.missing) == 30


def test_us_total_outage_is_a_clean_empty_result(monkeypatch):
    http_router(monkeypatch, us_http(("yahoo", "nasdaq", "cnbc")))
    result = download_market_data(constituents(*[f"T{i}" for i in range(30)]), DAY, PREV, NO_RETRY)
    assert result.prices.empty and len(result.missing) == 30
    status = classify_status(30, 0, [])
    assert status == ERROR


def kr_http(down=()):
    def handler(url, params):
        import requests
        if "finance.yahoo.com/v8/finance/chart" in url:
            if "yahoo" in down:
                raise requests.ConnectionError("yahoo down")
            return {"chart": {"result": [{
                "meta": {"symbol": url.rsplit("/", 1)[1], "exchangeTimezoneName": "Asia/Seoul", "dataGranularity": "1d"},
                "timestamp": [int(datetime(2026, 10, d, 0, 0).timestamp()) + 9 * 3600 for d in (7, 8)],
                "indicators": {"quote": [{"close": [1000.0, 920.0], "volume": [10, 10]}]}}]}}
        if "finance.daum.net/api/quotes" in url:
            if "daum" in down:
                raise requests.ConnectionError("daum down")
            return {"symbolCode": "A" + url.rsplit("/A", 1)[1], "tradeDate": "20261008", "regularTradePrice": 920.0,
                    "tradePrice": 925.0, "prevClosingPrice": 1000.0, "afterMarketAvailable": True,
                    "upperLimitPrice": 1300.0, "lowerLimitPrice": 700.0, "stockState": {}}
        if "finance.daum.net/api/quote" in url:
            return {"data": [{"date": "2026-10-08 00:00:00", "tradePrice": 925.0, "prevClosingPrice": 1000.0}]}
        if "m.stock.naver.com/api/stock" in url:
            if "naver" in down:
                raise requests.ConnectionError("naver down")
            return [{"localTradedAt": "2026-10-08", "closePrice": "925", "compareToPreviousClosePrice": "-75",
                     "accumulatedTradingVolume": 10, "fluctuationsRatio": "-7.5"}]
        raise AssertionError(url)
    return handler


@pytest.mark.parametrize("down,valid", [
    ((), 30), (("yahoo",), 30), (("daum",), 30), (("naver",), 30), (("daum", "naver"), 30),
    # Only Naver's KRX+NXT integrated last trade (925) remains: it is not a regular close, so nothing is invented.
    (("yahoo", "daum"), 0)])
def test_kr_run_through_real_adapters_survives_outages(monkeypatch, down, valid):
    codes = [f"{i:06d}" for i in range(1, 31)]
    http_router(monkeypatch, kr_http(down))
    universe = pd.DataFrame({"code": codes, "company_name": codes})
    result = kospi.download_kospi_market_data(universe, kospi.KospiSessionContext(DAY, PREV), NO_RETRY)
    assert len(result.prices) == valid and len(result.missing) == 30 - valid
    # Yahoo/Daum report the KRX regular close (920); Naver's integrated 925 never replaces it.
    assert result.prices.empty or set(result.prices.close) == {920.0}


def test_successor_search_survives_odd_payloads(monkeypatch):
    for payload in ([], "x", {"quotes": "bad"}, {"quotes": [None, 5, {"symbol": None}]}):
        monkeypatch.setattr(yahoo, "get_with_retry", lambda *a, **k: Response(payload))
        assert yahoo.find_successor_symbols("OLD", "Some Company", set(), FAST) == []
