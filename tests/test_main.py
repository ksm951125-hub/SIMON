from argparse import Namespace

import pytest

import main
from market_result import MarketResult


def test_failure_sends_notification_when_enabled(monkeypatch):
    sent = {}
    args = Namespace(notify=True, dry_run=False, session_date=None)

    monkeypatch.setattr(main, "_parse_args", lambda: args)
    monkeypatch.setattr(main, "run", lambda parsed: (_ for _ in ()).throw(RuntimeError("boom")))

    def fake_send(markdown, subject):
        sent["markdown"] = markdown
        sent["subject"] = subject
        return True

    monkeypatch.setattr(main, "send_gmail_email", fake_send)

    assert main.main() == 1
    assert "RuntimeError: boom" in sent["markdown"]
    assert "실행 실패" in sent["subject"]


def test_failure_notification_is_skipped_for_dry_run(monkeypatch):
    args = Namespace(notify=True, dry_run=True, session_date=None)
    monkeypatch.setattr(main, "_parse_args", lambda: args)
    monkeypatch.setattr(main, "run", lambda parsed: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(
        main,
        "send_gmail_email",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    assert main.main() == 1


@pytest.mark.parametrize("failed_market", ["US", "KR"])
def test_market_failures_are_isolated_in_combined_report(monkeypatch, failed_market):
    args = Namespace(
        notify=False,
        dry_run=True,
        session_date_us=None,
        session_date_kr=None,
    )
    ok_us = MarketResult(
        market="US", title="S&P 500 급락 모니터", threshold_pct=-10.0,
        code_column="ticker", currency="USD", status="OK", total_count=503, analyzed_count=503,
    )
    ok_kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="OK", total_count=944, analyzed_count=944,
    )
    monkeypatch.setattr(
        main,
        "run_us_monitor",
        (lambda *_: (_ for _ in ()).throw(RuntimeError("US outage")))
        if failed_market == "US" else (lambda *_: ok_us),
    )
    monkeypatch.setattr(
        main,
        "run_kr_monitor",
        (lambda *_: (_ for _ in ()).throw(RuntimeError("KR outage")))
        if failed_market == "KR" else (lambda *_: ok_kr),
    )
    captured = {}
    monkeypatch.setattr(main, "_write_outputs", lambda us, kr, markdown, executed: captured.setdefault("markdown", markdown))

    assert main.run(args) == 1
    assert f"{failed_market} outage" in captured["markdown"]
    assert "상태: OK" in captured["markdown"]
    assert "상태: FAILED" in captured["markdown"]
