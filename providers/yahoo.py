"""Yahoo v8 chart daily bars.

US: ``quote.close`` of 1d bars with ``includePrePost=false`` (split-adjusted,
not dividend-adjusted; ``adjclose`` is never read). Bars whose timestamp is not
inside the NYSE regular session of their date are rejected.
KR: ``<code>.KS`` 1d bars = KRX regular-session closes (verified against the
next day's KRX base price: 913/913 on 2026-09-29).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from config import SETTINGS, Settings
from net import get_with_retry
from providers.base import INVALID_PRICE, FieldValue, PriceObservation, failed, from_series, positive

LOGGER = logging.getLogger(__name__)
SOURCE = "Yahoo"
HOSTS = ("query1", "query2")
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
NEW_YORK = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")
# Yahoo stamps a daily bar with the session open, or with the last regular
# trade time (a few seconds after the bell) for a just-closed session.
CLOSE_TIMESTAMP_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class Bar:
    close: float
    volume: float | None
    open: float | None = None
    high: float | None = None
    low: float | None = None


@lru_cache(maxsize=512)
def _regular_session_bounds(day: date):
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        raise ValueError(f"정규장 거래일이 아닌 timestamp ({day})")
    return schedule.iloc[0]["market_open"], schedule.iloc[0]["market_close"]


def _result(payload: dict, symbol: str, timezone_name: str) -> dict:
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise ValueError(f"Yahoo chart 응답에 {symbol} 데이터 없음: {chart.get('error')}")
    result = results[0]
    meta = result.get("meta") or {}
    if meta.get("dataGranularity", "1d") != "1d":
        raise ValueError("일봉 이외 데이터 거부")
    if str(meta.get("symbol", symbol)).upper() != symbol.upper():
        raise ValueError(f"응답 ticker 불일치 ({meta.get('symbol')} != {symbol})")
    if (meta.get("exchangeTimezoneName") or timezone_name) != timezone_name:
        raise ValueError(f"unexpected exchange timezone ({meta.get('exchangeTimezoneName')})")
    return result


def parse_us_chart(payload: dict, ticker: str) -> dict[date, Bar]:
    """Regular-session daily bars keyed by the exchange session date.

    Rows with a null close are omitted (the date then counts as missing).
    """
    result = _result(payload, ticker, "America/New_York")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if len(timestamps) != len(closes):
        raise ValueError("일봉 timestamp/close 길이 불일치")

    def column(name: str) -> list:
        values = quote.get(name) or []
        return values if len(values) == len(timestamps) else [None] * len(timestamps)

    volumes, opens, highs, lows = column("volume"), column("open"), column("high"), column("low")
    bars: dict[date, Bar] = {}
    for index, timestamp in enumerate(timestamps):
        local = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(NEW_YORK)
        session_day = local.date()
        if session_day in bars:
            raise ValueError(f"중복 거래일 데이터 ({session_day})")
        opening, closing = _regular_session_bounds(session_day)
        if not (opening <= local <= closing + CLOSE_TIMESTAMP_GRACE):
            raise ValueError(f"정규장 밖의 일봉 timestamp ({local.isoformat()})")
        close = positive(closes[index])
        if close is None:
            continue
        bars[session_day] = Bar(close=round(close, 2), volume=None if volumes[index] is None else float(volumes[index]),
                                open=positive(opens[index]), high=positive(highs[index]), low=positive(lows[index]))
    return bars


def parse_kr_chart(payload: dict, code: str) -> dict[date, Bar]:
    result = _result(payload, f"{code}.KS", "Asia/Seoul")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or [None] * len(timestamps)
    if len(closes) != len(timestamps) or len(volumes) != len(timestamps):
        raise ValueError("일봉 timestamp/close 길이 불일치")
    bars: dict[date, Bar] = {}
    for timestamp, close, volume in zip(timestamps, closes, volumes):
        day = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(SEOUL).date()
        if day in bars:
            raise ValueError(f"중복 거래일 데이터 ({day})")
        price = positive(close)
        if price is None:
            continue
        # KRW prices are whole won; strip float32 noise.
        bars[day] = Bar(close=float(round(price)), volume=None if volume is None else float(volume))
    return bars


def split_note(payload: dict, previous_session_date: date, session_date: date, zone: ZoneInfo) -> str | None:
    result = payload["chart"]["result"][0]
    for event in ((result.get("events") or {}).get("splits") or {}).values():
        split_day = datetime.fromtimestamp(event["date"], timezone.utc).astimezone(zone).date()
        if previous_session_date < split_day <= session_date:
            return f"주식분할/병합 {event.get('splitRatio', '')} ({split_day}): 분할 조정 종가 기준"
    return None


def observation_from_bars(symbol: str, bars: dict[date, Bar], previous_date: date, session_date: date,
                          source: str = SOURCE, *, stale_checks: bool = True) -> PriceObservation:
    """Select the intended sessions BY DATE (never the last row) and reject stale bars.

    Stale checks are for US bars; a halted KRX stock legitimately repeats its
    base price with zero volume (cross-validation catches real staleness)."""
    observation = from_series(symbol, source, {day: bar.close for day, bar in bars.items()},
                              previous_date, session_date, adjustment="split")
    current, previous = bars.get(session_date), bars.get(previous_date)
    if current is not None and stale_checks:
        observation.volume = current.volume
        if current.volume is not None and current.volume <= 0:
            observation.close = FieldValue(date=session_date, status=INVALID_PRICE,
                                           detail="분석일 거래량 0: stale/미확정 데이터 의심")
        elif previous is not None and previous == current:
            observation.close = FieldValue(date=session_date, status=INVALID_PRICE, detail="양일 OHLCV 동일: stale 의심")
    return observation


def _fetch(symbol: str, previous_date: date, session_date: date, settings: Settings):
    start, end = previous_date - timedelta(days=7), session_date + timedelta(days=2)
    params = {
        "period1": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()),
        "interval": "1d", "events": "div,splits", "includeAdjustedClose": "false", "includePrePost": "false",
    }
    errors = []
    for host in HOSTS:
        try:
            response = get_with_retry(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                                      params=params, headers=HEADERS, settings=settings)
            return response.json(), None
        except Exception as exc:  # try the other Yahoo host
            errors.append(f"{host}: {exc}")
    return None, "Yahoo 조회 실패: " + " / ".join(errors)


def fetch_us(ticker: str, previous_date: date, session_date: date, settings: Settings = SETTINGS) -> PriceObservation:
    payload, error = _fetch(ticker, previous_date, session_date, settings)
    if error:
        return failed(ticker, SOURCE, error)
    try:
        bars = parse_us_chart(payload, ticker)
    except Exception as exc:
        return failed(ticker, SOURCE, f"Yahoo 응답 거부: {exc}", INVALID_PRICE)
    observation = observation_from_bars(ticker, bars, previous_date, session_date)
    observation.note = split_note(payload, previous_date, session_date, NEW_YORK)
    return observation


def fetch_kr(code: str, previous_date: date, session_date: date, settings: Settings = SETTINGS) -> PriceObservation:
    payload, error = _fetch(f"{code}.KS", previous_date, session_date, settings)
    if error:
        return failed(code, SOURCE, error)
    try:
        bars = parse_kr_chart(payload, code)
    except Exception as exc:
        return failed(code, SOURCE, f"Yahoo 응답 거부: {exc}", INVALID_PRICE)
    observation = observation_from_bars(code, bars, previous_date, session_date, stale_checks=False)
    if session_date in bars:
        observation.volume = bars[session_date].volume
    observation.note = split_note(payload, previous_date, session_date, SEOUL)
    return observation
