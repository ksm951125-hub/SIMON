from datetime import datetime

from market_calendar import KST, get_session_context


def test_us_holiday_is_skipped():
    # 2025-07-04 08:00 KST corresponds to 2025-07-03 in New York (an early-close session).
    # The next KST morning represents the Independence Day holiday.
    context = get_session_context(datetime(2025, 7, 5, 8, 0, tzinfo=KST))
    assert context.expected_date.isoformat() == "2025-07-04"
    assert context.is_holiday is True
    assert context.reason == "미국 증시 휴장 - 분석 대상 없음"


def test_monday_close_maps_to_tuesday_kst():
    context = get_session_context(datetime(2025, 7, 8, 8, 0, tzinfo=KST))
    assert context.session_date.isoformat() == "2025-07-07"
    assert context.previous_session_date.isoformat() == "2025-07-03"
