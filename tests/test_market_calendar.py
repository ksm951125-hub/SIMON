from datetime import datetime, timezone

from market_calendar import get_session_context


def test_uses_previous_settled_session_during_yahoo_data_delay():
    context = get_session_context(datetime(2025, 1, 3, 23, 0, tzinfo=timezone.utc))

    assert context.expected_date.isoformat() == "2025-01-03"
    assert context.session_date.isoformat() == "2025-01-02"
    assert context.previous_session_date.isoformat() == "2024-12-31"
    assert context.is_holiday is False


def test_uses_just_closed_session_after_yahoo_data_delay():
    context = get_session_context(datetime(2025, 1, 4, 3, 30, tzinfo=timezone.utc))

    assert context.expected_date.isoformat() == "2025-01-03"
    assert context.session_date.isoformat() == "2025-01-03"
    assert context.previous_session_date.isoformat() == "2025-01-02"
    assert context.is_holiday is False
