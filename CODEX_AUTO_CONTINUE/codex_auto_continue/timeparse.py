"""Codex 안내문에서 초기화(재설정) 시각을 읽어 한국 표준시(KST) datetime 으로 변환."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

KST = timezone(timedelta(hours=9), "KST")   # 한국은 DST 없음

MAX_HORIZON = timedelta(days=14)


def now_kst() -> datetime:
    return datetime.now(KST)


@dataclass(frozen=True)
class ResetInfo:
    reset_at: datetime
    kind: str          # "absolute" | "relative"
    raw: str           # 같은 안내문인지 비교하기 위한 정규화 문자열
    has_date: bool = False


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_TIME_RE = re.compile(
    r"(?:(?P<pre>오전|오후|\bAM|\bPM)\s*)?"
    r"(?P<h>\d{1,2})\s*(?::\s*(?P<m>\d{2})|시(?!간)(?:\s*(?P<m2>\d{1,2})\s*분)?)"
    r"(?:\s*(?P<post>AM\b|PM\b|a\.m\.|p\.m\.))?",
    re.I,
)
_TIME_AMPM_ONLY = re.compile(r"\b(?P<h>\d{1,2})\s*(?P<post>am|pm)\b", re.I)   # "5pm"
_DATE_KO = re.compile(r"(?:(?P<y>\d{4})\s*년\s*)?(?P<mo>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일")
_DATE_ISO = re.compile(r"(?P<y>\d{4})[-./](?P<mo>\d{1,2})[-./](?P<d>\d{1,2})")
_DATE_EN = re.compile(
    r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<y>\d{4}))?\b", re.I)
_DAY_WORD = re.compile(r"(오늘|내일|모레|\btoday\b|\btomorrow\b)", re.I)
_REL_KO = re.compile(r"(?:(?P<h>\d+)\s*시간)?\s*(?:(?P<m>\d+)\s*분)?\s*(?:후|뒤|이후)")
_REL_EN = re.compile(
    r"\bin\s+(?:(?P<h>\d+)\s*(?:hours?|hrs?|h)\b)?\s*(?:and\s+)?"
    r"(?:(?P<m>\d+)\s*(?:minutes?|mins?|m)\b)?", re.I)


def _hour_24(h: int, ampm: str) -> Optional[int]:
    ampm = ampm.lower().replace(".", "")
    if ampm in ("오전", "am"):
        if h == 12:
            return 0
        return h if 0 <= h <= 12 else None
    if ampm in ("오후", "pm"):
        if h == 12:
            return 12
        if 1 <= h < 12:
            return h + 12
        return h if 12 < h < 24 else None   # '오후 13:00' 같은 24시간 표기는 그대로 허용
    return h if 0 <= h < 24 else None


def parse_reset_time(text: str, now: datetime,
                     past_grace: timedelta = timedelta(hours=3)) -> Optional[ResetInfo]:
    """text 안의 첫 시각 표현을 해석한다.

    - 날짜가 없으면 '지금 이후 가장 가까운 해당 시각'. 단, 방금(past_grace 이내) 지난 시각은
      오늘로 유지한다(= 초기화는 지났지만 안내가 아직 남아 있는 상황).
    - 자정 넘김: 23:50 에 '오전 12:10' → 다음 날 00:10.
    """
    now = now.astimezone(KST)
    t = unicodedata.normalize("NFKC", text or "")

    m = _TIME_RE.search(t) or _TIME_AMPM_ONLY.search(t)
    if m:
        h = int(m["h"])
        mi = int((m.groupdict().get("m") or m.groupdict().get("m2")) or 0)
        ampm = m.groupdict().get("pre") or m["post"] or ""
        hh = _hour_24(h, ampm) if ampm else (h if 0 <= h < 24 else None)
        if hh is None or not 0 <= mi < 60:
            return None
        return _resolve_absolute(t, m, hh, mi, now, past_grace)

    rel = _REL_KO.search(t) or _REL_EN.search(t)
    if rel and (rel["h"] or rel["m"]):
        delta = timedelta(hours=int(rel["h"] or 0), minutes=int(rel["m"] or 0))
        if delta <= MAX_HORIZON:
            return ResetInfo(now + delta, "relative", "rel")
    return None


def _resolve_absolute(t, tm, hh, mi, now, past_grace) -> Optional[ResetInfo]:
    from .models import norm_text

    date_txt = ""
    dt: Optional[datetime] = None
    has_date = False
    try:
        d = _DATE_ISO.search(t) or _DATE_KO.search(t)
        e = None if d else _DATE_EN.search(t)
        if d or e:
            has_date = True
            src = d or e
            date_txt = src.group(0)
            if d:
                y, mo, dd = src["y"], int(src["mo"]), int(src["d"])
            else:
                y, mo, dd = src["y"], _MONTHS[src["mon"][:3].lower()], int(src["d"])
            year = int(y) if y else now.year
            dt = datetime(year, mo, dd, hh, mi, tzinfo=KST)
            if not y and dt < now - past_grace:
                dt = datetime(year + 1, mo, dd, hh, mi, tzinfo=KST)
        else:
            w = _DAY_WORD.search(t)
            if w:
                has_date = True
                date_txt = w.group(0)
                word = w.group(0).lower()
                off = 0 if word in ("오늘", "today") else 2 if word == "모레" else 1
                day = (now + timedelta(days=off)).date()
                dt = datetime(day.year, day.month, day.day, hh, mi, tzinfo=KST)
            else:
                dt = now.replace(hour=hh, minute=mi, second=0, microsecond=0)
                if dt < now - past_grace:
                    dt += timedelta(days=1)
    except ValueError:      # 존재하지 않는 날짜
        return None

    if dt - now > MAX_HORIZON:
        return None
    raw = norm_text(tm.group(0)) + "|" + norm_text(date_txt)
    return ResetInfo(dt, "absolute", raw, has_date)
