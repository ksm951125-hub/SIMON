from datetime import date, datetime
import time

import pytest

from main import _context_for_override
from market_calendar import KST, get_session_context
from runtime import execute_market


@pytest.mark.parametrize("day", [(2025, 3, 11), (2025, 11, 4)])
def test_0730_kst_works_on_both_sides_of_dst(day):
    # 22:30 UTC = 18:30 EDT (2.5h after close) / 17:30 EST (1.5h after close).
    result = get_session_context(datetime(*day, 7, 30, tzinfo=KST))
    assert result.session_date.day == day[2] - 1


def test_us_unscheduled_mourning_holiday():
    result = get_session_context(datetime(2025, 1, 10, 8, tzinfo=KST))
    assert result.session_date == date(2025, 1, 8)


def test_future_us_override_rejected():
    with pytest.raises(ValueError):
        _context_for_override(date(2030, 1, 2))


def test_us_override_rejects_non_session():
    with pytest.raises(ValueError, match="not an NYSE trading session"):
        _context_for_override(date(2026, 9, 7))  # Labor Day


def slow_worker(_):
    time.sleep(20)


def test_hard_deadline_terminates_hung_worker():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        execute_market(slow_worker, None, timeout_seconds=0.3)
    assert time.monotonic() - started < 8


def test_smtp_ambiguous_failure_is_not_retried(monkeypatch):
    import main
    import smtplib
    from argparse import Namespace
    from market_result import MarketResult
    args = Namespace(notify=True, dry_run=False, market="all", session_date_us=None, session_date_kr=None)
    monkeypatch.setattr(main, "_parse_args", lambda: args)
    result = MarketResult(market="US", title="Test", threshold_pct=-10, code_column="ticker", currency="USD",
                          status="NORMAL", total_count=1, analyzed_count=1)
    monkeypatch.setattr(main, "execute_market", lambda *args: result)
    monkeypatch.setattr(main, "_write_outputs", lambda *args: None)
    calls = []

    def fail(*args):
        calls.append(1)
        raise smtplib.SMTPServerDisconnected("unknown acceptance")

    monkeypatch.setattr(main, "send_gmail_email", fail)
    assert main.main() == 1
    assert len(calls) == 1
