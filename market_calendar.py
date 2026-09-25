from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")
# Yahoo's completed daily bars are not consistently available immediately at
# the NYSE close. Production runs have returned mostly-NaN universes for the
# just-closed session, so only select it after a conservative settlement lag.
DATA_READY_DELAY = timedelta(hours=2)


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
    selected_position = row_position
    if market_close + DATA_READY_DELAY > now_utc:
        # The expected session is still trading or Yahoo's end-of-day bars are
        # within their observed incomplete-data window. Process the preceding
        # fully settled session instead of treating partial bars as final.
        selected_position -= 1

    if selected_position <= 0:
        raise RuntimeError("직전 거래일을 계산할 수 없습니다")

    selected = schedule.index[selected_position].date()
    previous = schedule.index[selected_position - 1].date()
    return SessionContext(expected, selected, previous, False)
