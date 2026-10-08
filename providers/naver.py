"""Naver mobile stock API (KR).

Since Nextrade (NXT) launched, Naver's daily ``closePrice`` is the integrated
KRX+NXT last trade (NXT after-market until 20:00 KST) - NOT the KRX regular
close for NXT-traded stocks. ``closePrice - compareToPreviousClosePrice`` is
the KRX base price of that day (= previous KRX regular close, on the day's
share basis), which is exact and used to validate the previous close.

The integrated close is returned with ``session_type=INTEGRATED`` so it is never
compared with regular closes, unless the caller has independent evidence that
the stock does not trade on NXT (then integrated == KRX regular close).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from config import SETTINGS, Settings
from net import get_with_retry
from providers.base import (INTEGRATED, NOT_PROVIDED, OK, REGULAR, STALE_SOURCE, FieldValue, PriceObservation, failed,
                            never_raises, positive)

SOURCE = "Naver"
PRICE_URL = "https://m.stock.naver.com/api/stock/{code}/price"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://m.stock.naver.com/"}


def json_get(url: str, params: dict, settings: Settings = SETTINGS):
    return get_with_retry(url, params=params, headers=HEADERS, settings=settings).json()


def _signed(value) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class NaverDay:
    integrated_close: float | None
    krx_base_price: float | None
    volume: float | None
    official_change_pct: float | None


def parse_daily(rows) -> dict[date, NaverDay]:
    if not isinstance(rows, list):
        raise ValueError("Naver daily price response is not a list")
    days: dict[date, NaverDay] = {}
    for row in rows:
        day = date.fromisoformat(str(row["localTradedAt"])[:10])
        if day in days:
            raise ValueError(f"Naver 중복 거래일 ({day})")
        close, delta = positive(row.get("closePrice")), _signed(row.get("compareToPreviousClosePrice"))
        base = close - delta if close is not None and delta is not None else None
        days[day] = NaverDay(close, base if base and base > 0 else None,
                             _signed(row.get("accumulatedTradingVolume")), _signed(row.get("fluctuationsRatio")))
    return days


def observation_from_days(code: str, days: dict[date, NaverDay], previous_date: date, session_date: date) -> PriceObservation:
    today = days.get(session_date)
    if today is None:
        latest = max(days) if days else None
        detail = f"{session_date} 행 없음, 최신 {latest}"
        return failed(code, SOURCE, detail, STALE_SOURCE)
    base = (FieldValue(today.krx_base_price, previous_date, OK) if today.krx_base_price
            else FieldValue(date=previous_date, status=STALE_SOURCE, detail="KRX 기준가 계산 불가"))
    close = (FieldValue(today.integrated_close, session_date, OK, session_type=INTEGRATED, corroborative=True)
             if today.integrated_close
             else FieldValue(date=session_date, status=STALE_SOURCE, detail="종가 없음", session_type=INTEGRATED))
    return PriceObservation(code, SOURCE, close=close, previous_close=base, adjustment="split", volume=today.volume)


def next_session_close(code: str, days: dict[date, NaverDay], session_date: date,
                       next_session: date | None) -> PriceObservation | None:
    """KRX base price of the NEXT session = KRX regular close of the analysis day.

    Only exists when the next session already traded (replays of past dates);
    never at the 07:30 KST run. A base re-set on that next day (split, rights)
    would show up as a disagreement and be flagged, not silently used.
    """
    following = days.get(next_session) if next_session else None
    today = days.get(session_date)
    if following is None or not following.krx_base_price:
        return None
    # A base re-set on the next day (split/merge, re-listing) cannot be the close:
    # it would differ from the day's own last trade by more than a price limit.
    if today and today.integrated_close and abs(following.krx_base_price / today.integrated_close - 1) > 0.3:
        return None
    return PriceObservation(code, f"{SOURCE} 익일 기준가",
                            close=FieldValue(following.krx_base_price, session_date, OK),
                            previous_close=FieldValue(status=NOT_PROVIDED))


@never_raises(SOURCE, extras=2)
def fetch_daily(code: str, previous_date: date, session_date: date, settings: Settings = SETTINGS,
                page_size: int = 10, next_session: date | None = None
                ) -> tuple[PriceObservation, NaverDay | None, PriceObservation | None]:
    try:
        days = parse_daily(json_get(PRICE_URL.format(code=code), {"page": 1, "pageSize": page_size}, settings))
    except Exception as exc:
        return failed(code, SOURCE, f"Naver daily 조회 실패: {exc}"), None, None
    return (observation_from_days(code, days, previous_date, session_date), days.get(session_date),
            next_session_close(code, days, session_date, next_session))


def as_regular(observation: PriceObservation, reason: str) -> PriceObservation:
    """Promote an integrated close to a regular close when the stock is known
    not to trade on NXT (then the integrated last trade IS the KRX close)."""
    if observation.close.status != OK or observation.close.session_type != INTEGRATED:
        return observation
    return replace(observation, source=f"{observation.source}({reason})",
                   close=replace(observation.close, session_type=REGULAR, corroborative=False))
