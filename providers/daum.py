"""Daum Finance (KR secondary source + market state).

Quote snapshot (``/api/quotes/A{code}``):
- ``regularTradePrice``: KRX regular-session close, separate from the
  integrated/NXT ``tradePrice``. For stocks without NXT trading
  (``afterMarketAvailable=false``) ``tradePrice`` is the KRX close.
- ``basePrice``/``prevClosingPrice``: KRX base / previous KRX close.
- ``stockState`` (정리매매, 거래정지, 액면변경, 감자/재평가, 권리락, ...) and
  ``upperLimitPrice``/``lowerLimitPrice`` (0/0 = no price limit).
The snapshot is dated by ``tradeDate``. If it already rolled past the analysis
day (to any date up to the next trading session - weekends/holidays included),
its ``prevClosingPrice`` is the analysis-day close; any other date is STALE_SOURCE.

Daily rows (``/api/quote/A{code}/days``): ``prevClosingPrice`` of the analysis
row is the KRX base of that day; ``tradePrice`` is integrated (NXT-inclusive).
"""
from __future__ import annotations

from datetime import date, datetime

from config import SETTINGS, Settings
from net import get_with_retry
from providers.base import (INTEGRATED, INVALID_PRICE, NOT_PROVIDED, OK, STALE_SOURCE, FieldValue, PriceObservation,
                            failed, never_raises, positive)
from special_trading import MarketState

SOURCE = "Daum"
QUOTE_URL = "https://finance.daum.net/api/quotes/A{code}"
DAYS_URL = "https://finance.daum.net/api/quote/A{code}/days"


def _headers(code: str) -> dict:
    return {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
            "Referer": f"https://finance.daum.net/quotes/A{code}"}


def _parse_date(text) -> date | None:
    text = str(text or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10] if "-" in text else text[:8], fmt).date()
        except ValueError:
            continue
    return None


def _limit(value) -> float | None:
    """Limit prices: a finite number >= 0 (0 means "no limit applies"), else unknown."""
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) and number >= 0 else None


def parse_state(payload: dict) -> MarketState:
    state = payload.get("stockState")
    state = state if isinstance(state, dict) else {}
    return MarketState(
        source=SOURCE,
        as_of=_parse_date(payload.get("tradeDate") or payload.get("date")),
        pre_delisting_trading=bool(state.get("isPreDelistingTrading")),
        trading_suspended=bool(state.get("isTradingSuspended")),
        delisted=bool(state.get("isDelisted") or payload.get("isDelisted")),
        administrative_issue=bool(state.get("isAdministrativeIssue")),
        par_value_change=str(state.get("parValueChange") or "NONE"),
        revaluation=str(state.get("revaluation") or "NONE"),
        ex_event=str(state.get("ex") or "NONE"),
        listing_date=_parse_date(payload.get("listingDate")),
        upper_limit=_limit(payload.get("upperLimitPrice")),
        lower_limit=_limit(payload.get("lowerLimitPrice")),
        nxt_tradable=payload.get("afterMarketAvailable") if isinstance(payload.get("afterMarketAvailable"), bool) else None,
    )


def observation_from_quote(code: str, payload: dict, previous_date: date, session_date: date,
                           next_session: date | None) -> PriceObservation:
    quote_date = _parse_date(payload.get("tradeDate") or payload.get("date"))
    state = payload.get("stockState")
    state = state if isinstance(state, dict) else {}
    if quote_date == session_date:
        regular = positive(payload.get("regularTradePrice"))
        if regular is None and payload.get("afterMarketAvailable") is False:
            regular = positive(payload.get("tradePrice"))
        if regular is None and state.get("isTradingSuspended"):
            regular = positive(payload.get("prevClosingPrice"))  # no trade: close = base
        close = (FieldValue(regular, session_date, OK) if regular is not None
                 else FieldValue(date=session_date, status=INVALID_PRICE, detail="정규장 종가 필드 없음"))
        base = positive(payload.get("prevClosingPrice")) or positive(payload.get("basePrice"))
        previous = (FieldValue(base, previous_date, OK) if base is not None
                    else FieldValue(date=previous_date, status=INVALID_PRICE, detail="기준가 없음"))
        return PriceObservation(code, SOURCE, close=close, previous_close=previous, adjustment="split")
    if next_session is not None and quote_date is not None and session_date < quote_date <= next_session:
        # Snapshot already rolled past the analysis day (no session in between):
        # its previous close is our close.
        value = positive(payload.get("prevClosingPrice"))
        close = (FieldValue(value, session_date, OK, detail="익일 스냅샷의 전일종가") if value is not None
                 else FieldValue(date=session_date, status=INVALID_PRICE, detail="prevClosingPrice 없음"))
        return PriceObservation(code, f"{SOURCE}(익일 전일종가)", close=close,
                                previous_close=FieldValue(status=NOT_PROVIDED), adjustment="split")
    detail = f"스냅샷 날짜 {quote_date} ≠ {session_date}"
    return failed(code, SOURCE, detail, STALE_SOURCE)


@never_raises(SOURCE, extras=1)
def fetch_quote(code: str, previous_date: date, session_date: date, next_session: date | None,
                settings: Settings = SETTINGS) -> tuple[PriceObservation, MarketState | None]:
    try:
        payload = get_with_retry(QUOTE_URL.format(code=code), params={"summary": "false", "changeStatistics": "true"},
                                 headers=_headers(code), settings=settings, attempts=2).json()
        if str(payload.get("symbolCode", f"A{code}")).upper() != f"A{code}".upper():
            raise ValueError(f"응답 종목 불일치 ({payload.get('symbolCode')})")
    except Exception as exc:
        return failed(code, SOURCE, f"Daum 조회 실패: {exc}"), None
    return observation_from_quote(code, payload, previous_date, session_date, next_session), parse_state(payload)


def observation_from_days(code: str, payload: dict, previous_date: date, session_date: date) -> PriceObservation:
    rows = {}
    for row in (payload or {}).get("data") or []:
        day = _parse_date(row.get("date"))
        if day in rows:
            raise ValueError(f"Daum 중복 거래일 ({day})")
        rows[day] = row
    row = rows.get(session_date)
    if row is None:
        latest = max((day for day in rows if day), default=None)
        return failed(code, f"{SOURCE} days", f"{session_date} 행 없음, 최신 {latest}", STALE_SOURCE)
    base, close = positive(row.get("prevClosingPrice")), positive(row.get("tradePrice"))
    return PriceObservation(
        code, f"{SOURCE} days",
        previous_close=(FieldValue(base, previous_date, OK) if base
                        else FieldValue(date=previous_date, status=INVALID_PRICE, detail="기준가 없음")),
        close=(FieldValue(close, session_date, OK, session_type=INTEGRATED, corroborative=True) if close
               else FieldValue(date=session_date, status=INVALID_PRICE, detail="종가 없음", session_type=INTEGRATED)),
        adjustment="split", volume=row.get("accTradeVolume"))


@never_raises(f"{SOURCE} days")
def fetch_days(code: str, previous_date: date, session_date: date, settings: Settings = SETTINGS) -> PriceObservation:
    try:
        payload = get_with_retry(DAYS_URL.format(code=code),
                                 params={"symbolCode": f"A{code}", "page": 1, "perPage": 30, "pagination": "true"},
                                 headers=_headers(code), settings=settings, attempts=2).json()
        return observation_from_days(code, payload, previous_date, session_date)
    except Exception as exc:
        return failed(code, f"{SOURCE} days", f"Daum days 조회 실패: {exc}")
