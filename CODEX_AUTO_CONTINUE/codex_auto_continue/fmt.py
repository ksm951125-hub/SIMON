"""화면 표시용 포맷터."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .timeparse import KST


def fmt_clock(dt: Optional[datetime], now: Optional[datetime] = None, seconds: bool = False) -> str:
    """오전 11:35 형식. 오늘이 아니면 '내일'/'M월 D일' 접두."""
    if dt is None:
        return "-"
    dt = dt.astimezone(KST)
    h12 = dt.hour % 12 or 12
    s = f"{'오전' if dt.hour < 12 else '오후'} {h12}:{dt.minute:02d}"
    if seconds:
        s += f":{dt.second:02d}"
    if now is not None:
        days = (dt.date() - now.astimezone(KST).date()).days
        if days == 1:
            s = "내일 " + s
        elif days != 0:
            s = f"{dt.month}월 {dt.day}일 " + s
    return s


def fmt_remaining(td: Optional[timedelta]) -> str:
    if td is None:
        return "-"
    secs = int(td.total_seconds())
    if secs <= 0:
        return "도달"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분"
    return "1분 미만"


def fmt_full(dt: Optional[datetime]) -> str:
    return "-" if dt is None else dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
