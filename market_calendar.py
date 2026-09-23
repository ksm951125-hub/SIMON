from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SessionContext:
    expected_date: date
    session_date: date | None
    previous_session_date: date | None
    is_holiday: bool
    reason: str | None = None


def get_session_context(now: datetime | None = None) -> SessionContext:
    """Return the US session expected at this KST run and its predecessor.

    At 08:00 KST, New York is still on the previous calendar date. A run whose
    New York calendar date is not an NYSE session is intentionally reported as
    a holiday instead of silently re-processing an older session.
    """
    if now is None:
        now = datetime.now(tz=KST)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    now_utc = now.astimezone(timezone.utc)
    expected = now.astimezone(NEW_YORK).date()
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(
        start_date=expected - timedelta(days=14),
        end_date=expected,
    )

    expected_key = str(expected)
    if expected_key not in schedule.index.strftime("%Y-%m-%d"):
        return SessionContext(
            expected_date=expected,
            session_date=None,
            previous_session_date=None,
            is_holiday=True,
            reason="미국 증시 휴장 - 분석 대상 없음",
        )

    row_position = list(schedule.index.strftime("%Y-%m-%d")).index(expected_key)
    market_close = schedule.iloc[row_position]["market_close"].to_pydatetime()
    if market_close > now_utc:
        return SessionContext(
            expected_date=expected,
            session_date=None,
            previous_session_date=None,
            is_holiday=True,
            reason="미국 정규장이 아직 종료되지 않음 - 분석 대상 없음",
        )
    if row_position == 0:
        raise RuntimeError("직전 거래일을 계산할 수 없습니다")

    previous = schedule.index[row_position - 1].date()
    return SessionContext(expected, expected, previous, False)
