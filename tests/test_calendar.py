from datetime import datetime

from market_calendar import KST, get_session_context


def test_us_holiday_uses_most_recent_completed_session():
    # 2025-07-04 08:00 KST corresponds to 2025-07-03 in New York (an early-close session).
    # The next KST morning represents the Independence Day holiday.
    context = get_session_context(datetime(2025, 7, 5, 8, 0, tzinfo=KST))
    assert context.expected_date.isoformat() == "2025-07-04"
    assert context.session_date.isoformat() == "2025-07-03"
    assert context.previous_session_date.isoformat() == "2025-07-02"
    assert context.is_holiday is False


def test_us_weekend_uses_friday_and_thursday():
    context = get_session_context(datetime(2025, 7, 13, 8, 0, tzinfo=KST))
    assert context.session_date.isoformat() == "2025-07-11"
    assert context.previous_session_date.isoformat() == "2025-07-10"


def test_settled_monday_close_maps_to_tuesday_kst():
    context = get_session_context(datetime(2025, 7, 8, 12, 30, tzinfo=KST))
    assert context.session_date.isoformat() == "2025-07-07"
    assert context.previous_session_date.isoformat() == "2025-07-03"
