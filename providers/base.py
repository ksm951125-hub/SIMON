"""Standardized price observations returned by every source adapter.

Field semantics (identical for every adapter, so cross-validation never compares
different kinds of prices):

- ``close``: the regular-session close of ``trading_date`` (US: NYSE/Nasdaq
  4pm close; KR: KRX close). Never pre/post-market, NXT or dividend-adjusted.
- ``previous_close``: the regular-session close of the previous trading date,
  expressed on the analysis day's share basis (i.e. split-adjusted when a split
  takes effect on the analysis date, as both Yahoo and the KRX base price are).
  Never dividend-adjusted.

Each field carries its own status so a source can, for example, provide a
valid previous close while its analysis-day close is not published yet
(``STALE_SOURCE``). Values are only ever accepted for the exact requested date.
"""
from __future__ import annotations

import functools
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

LOGGER = logging.getLogger(__name__)

# Field statuses
OK = "OK"
STALE_SOURCE = "STALE_SOURCE"      # source answered, but not for the requested date
UNAVAILABLE = "UNAVAILABLE"        # request/parse failed
INVALID_PRICE = "INVALID_PRICE"    # non-numeric, <= 0, stale bar
NOT_PROVIDED = "NOT_PROVIDED"      # this source never provides the field

# Session types
REGULAR = "REGULAR"
INTEGRATED = "INTEGRATED"          # KRX+NXT last trade (Naver/Daum "close"); not a regular close


@dataclass(frozen=True)
class FieldValue:
    value: float | None = None
    date: date | None = None
    status: str = NOT_PROVIDED
    detail: str = ""
    session_type: str = REGULAR
    # Supporting-only value: counts when it AGREES with other sources but is never
    # treated as a contradiction (its definition may legitimately differ, e.g. a
    # previous close re-based for an ex-dividend on the analysis day).
    corroborative: bool = False

    @property
    def sane(self) -> bool:
        """A finite, positive number (adapters guarantee it; checked again here)."""
        return (self.status == OK and isinstance(self.value, (int, float)) and math.isfinite(self.value)
                and self.value > 0)

    @property
    def usable(self) -> bool:
        return self.sane and self.session_type == REGULAR


@dataclass
class PriceObservation:
    symbol: str
    source: str
    close: FieldValue = field(default_factory=FieldValue)
    previous_close: FieldValue = field(default_factory=FieldValue)
    adjustment: str = "none"       # "none" | "split" (split-adjusted, never dividend-adjusted)
    volume: float | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    note: str | None = None
    # Listing evidence: last date this source has a price for the symbol, and
    # whether the source says the symbol does not exist (renamed/delisted).
    latest_date: date | None = None
    symbol_unknown: bool = False
    # Corporate action inside (previous session, analysis session]: raw pre-event
    # prices equal split-adjusted prices x split_factor (2:1 split -> 2.0).
    split_factor: float = 1.0
    # First regular trading day the source knows for the symbol (new listing,
    # spin-off): there is no previous close on or before that day.
    first_trade_date: date | None = None

    @property
    def has_any(self) -> bool:
        return self.close.usable or self.previous_close.usable

    def describe(self) -> str:
        parts = []
        for name, value in (("prev", self.previous_close), ("close", self.close)):
            if value.status == NOT_PROVIDED:
                continue
            parts.append(f"{name}={value.value}" if value.status == OK else f"{name}:{value.status}({value.detail})")
        return f"{self.source}[{', '.join(parts)}]"


def positive(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def failed(symbol: str, source: str, detail: str, status: str = UNAVAILABLE, *,
           symbol_unknown: bool = False) -> PriceObservation:
    return PriceObservation(symbol, source,
                            close=FieldValue(status=status, detail=detail),
                            previous_close=FieldValue(status=status, detail=detail), symbol_unknown=symbol_unknown)


def internal_error(symbol: str, source: str, exc: BaseException) -> PriceObservation:
    """An adapter hit an unexpected payload/bug: report it as a failed observation
    for that one symbol. One odd response must never take down a whole market."""
    LOGGER.warning("%s %s 처리 오류(해당 종목만 제외): %s: %s", source, symbol, type(exc).__name__, exc)
    return failed(symbol, source, f"{source} 처리 오류: {type(exc).__name__}: {exc}", INVALID_PRICE)


def never_raises(source: str, extras: int = 0):
    """Decorator for adapter entry points whose first argument is the symbol.

    ``extras`` is the number of additional values (None) in a tuple return, e.g.
    ``(observation, market_state)`` -> extras=1."""
    def decorate(function):
        @functools.wraps(function)
        def wrapper(symbol, *args, **kwargs):
            try:
                return function(symbol, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - this is the isolation boundary
                observation = internal_error(symbol, source, exc)
                return (observation, *([None] * extras)) if extras else observation
        return wrapper
    return decorate


def from_series(symbol: str, source: str, series: dict[date, float | None], previous_date: date, session_date: date,
                *, adjustment: str = "none", session_type: str = REGULAR) -> PriceObservation:
    """Build an observation from a date-keyed close series. Missing dates are
    STALE_SOURCE when the source has other data, never silently another day."""
    latest = max(series) if series else None

    def pick(day: date) -> FieldValue:
        if day in series:
            value = positive(series[day])
            if value is None:
                return FieldValue(date=day, status=INVALID_PRICE, detail="null/비정상 종가", session_type=session_type)
            return FieldValue(value, day, OK, session_type=session_type)
        return FieldValue(date=day, status=STALE_SOURCE,
                          detail=f"{day} 없음, 최신 {latest}" if latest else "데이터 없음", session_type=session_type)

    priced = [day for day, value in series.items() if positive(value) is not None]
    return PriceObservation(symbol, source, close=pick(session_date), previous_close=pick(previous_date),
                            adjustment=adjustment, latest_date=max(priced) if priced else None)
