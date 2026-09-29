"""US regular-session close collection for the S&P 500 universe.

Primary: Yahoo v8 chart daily bars (raw `quote.close`, never `adjclose`,
includePrePost=false). Secondary: Nasdaq historical quotes ("Close/Last" is the
official consolidated regular-session close). The two providers are
independent, so every symbol is cross-checked; the secondary also serves as a
fallback when the primary cannot supply a validated pair.

A symbol only counts as valid when BOTH intended session dates are present in
the returned data (matched by exchange-local session date, never "last row"),
both closes are positive finite numbers, and the analysis-date bar is not
stale. Detection uses the raw, unrounded change; rounding is display-only.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal

from config import SETTINGS, Settings
from detector import calculate_change_pct, is_drop
from net import get_with_retry, retry_until_available

LOGGER = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CHART_URLS = (
    ("query1", "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"),
    ("query2", "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"),
)
CHART_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# Yahoo stamps a daily bar with the session open, or with the last regular
# trade time (a few seconds after the bell) for a just-closed session.
CLOSE_TIMESTAMP_GRACE = timedelta(minutes=5)
# Closes are quoted in cents; Yahoo floats carry float32 noise (262.8699951).
PRICE_TOLERANCE = 0.011
# After this many CONSECUTIVE cross-check failures the secondary provider is
# treated as unavailable for the rest of the run (blocked IP / throttling).
NASDAQ_CIRCUIT_BREAKER = 25


@dataclass(frozen=True)
class Bar:
    close: float
    volume: float | None
    open: float | None = None
    high: float | None = None
    low: float | None = None


@dataclass
class PairResult:
    previous_close: float | None = None
    close: float | None = None
    error: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.previous_close is not None and self.close is not None


@dataclass
class UsCollection:
    prices: pd.DataFrame
    missing: dict[str, str]
    fallback_count: int = 0
    mismatch_count: int = 0
    cross_checked_count: int = 0
    secondary_available: bool = True
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@lru_cache(maxsize=512)
def _regular_session_bounds(day: date):
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        raise ValueError(f"정규장 거래일이 아닌 timestamp ({day})")
    return schedule.iloc[0]["market_open"], schedule.iloc[0]["market_close"]


def _finite_positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_chart_response(payload: dict, ticker: str) -> dict[date, Bar]:
    """Return regular-session daily bars keyed by the exchange session date.

    Rows with a null close are omitted (the date then counts as missing).
    """
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise ValueError(f"Yahoo chart 응답에 {ticker} 데이터 없음: {chart.get('error')}")
    result = results[0]
    meta = result.get("meta") or {}
    if meta.get("dataGranularity", "1d") != "1d":
        raise ValueError("일봉 이외 데이터 거부")
    if str(meta.get("symbol", ticker)).upper() != ticker.upper():
        raise ValueError(f"응답 ticker 불일치 ({meta.get('symbol')} != {ticker})")
    if (meta.get("exchangeTimezoneName") or "America/New_York") != "America/New_York":
        raise ValueError("unexpected US exchange timezone")
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
        close = _finite_positive(closes[index])
        if close is None:
            continue
        bars[session_day] = Bar(
            close=round(close, 2),
            volume=None if volumes[index] is None else float(volumes[index]),
            open=_finite_positive(opens[index]),
            high=_finite_positive(highs[index]),
            low=_finite_positive(lows[index]),
        )
    return bars


def _split_note(payload: dict, previous_session_date: date, session_date: date) -> str | None:
    result = payload["chart"]["result"][0]
    for event in ((result.get("events") or {}).get("splits") or {}).values():
        split_day = datetime.fromtimestamp(event["date"], timezone.utc).astimezone(NEW_YORK).date()
        if previous_session_date < split_day <= session_date:
            return f"주식분할/병합 {event.get('splitRatio', '')} ({split_day}): 분할 조정 종가 기준"
    return None


def select_pair(bars: dict[date, Bar], previous_session_date: date, session_date: date, source: str) -> PairResult:
    """Pick the two intended sessions by date and validate them."""
    previous = bars.get(previous_session_date)
    current = bars.get(session_date)
    if previous is None or current is None:
        latest = max(bars) if bars else None
        missing_days = [str(day) for day, bar in ((previous_session_date, previous), (session_date, current)) if bar is None]
        return PairResult(error=f"{source} 필수 거래일 종가 누락 ({', '.join(missing_days)}; 최신 데이터 {latest})")
    if current.volume is not None and current.volume <= 0:
        return PairResult(error=f"{source} 분석일 거래량 0: stale/미확정 데이터 의심")
    if previous == current:
        return PairResult(error=f"{source} 양일 OHLCV 동일: stale 데이터 의심")
    return PairResult(previous.close, current.close)


def fetch_yahoo_pair(ticker: str, previous_session_date: date, session_date: date, settings: Settings = SETTINGS) -> PairResult:
    period_start = previous_session_date - timedelta(days=5)
    period_end = session_date + timedelta(days=2)
    params = {
        "period1": int(datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc).timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "false",
        "includePrePost": "false",
    }
    errors = []
    for source, template in CHART_URLS:
        try:
            response = get_with_retry(template.format(ticker=ticker), params=params, headers=CHART_HEADERS, settings=settings)
            payload = response.json()
            bars = _parse_chart_response(payload, ticker)
        except Exception as exc:  # try the other Yahoo host
            errors.append(f"{source}: {exc}")
            continue
        pair = select_pair(bars, previous_session_date, session_date, "Yahoo")
        pair.note = _split_note(payload, previous_session_date, session_date)
        return pair
    return PairResult(error="Yahoo 조회 실패: " + " / ".join(errors))


def nasdaq_symbol(ticker: str) -> str:
    return ticker.strip().upper().replace("-", ".")


def _parse_nasdaq_payload(payload: dict) -> dict[date, Bar]:
    table = ((payload or {}).get("data") or {}).get("tradesTable") or {}
    rows = table.get("rows") or []
    if not rows:
        status = (payload or {}).get("status") or {}
        raise ValueError(f"Nasdaq 데이터 없음 ({status.get('bCodeMessage') or status.get('rCode')})")

    def number(text) -> float | None:
        return _finite_positive(str(text or "").replace("$", "").replace(",", "").strip())

    bars: dict[date, Bar] = {}
    for row in rows:
        day = datetime.strptime(row["date"], "%m/%d/%Y").date()
        if day in bars:
            raise ValueError(f"Nasdaq 중복 거래일 ({day})")
        close = number(row.get("close"))
        if close is None:
            continue
        volume_text = str(row.get("volume") or "").replace(",", "").strip()
        volume = float(volume_text) if volume_text.replace(".", "", 1).isdigit() else None
        bars[day] = Bar(close=close, volume=volume, open=number(row.get("open")),
                        high=number(row.get("high")), low=number(row.get("low")))
    return bars


def fetch_nasdaq_pair(ticker: str, previous_session_date: date, session_date: date, settings: Settings = SETTINGS) -> PairResult:
    params = {
        "assetclass": "stocks",
        "fromdate": (previous_session_date - timedelta(days=5)).isoformat(),
        "todate": (session_date + timedelta(days=1)).isoformat(),
        "limit": 20,
    }
    try:
        response = get_with_retry(NASDAQ_URL.format(symbol=nasdaq_symbol(ticker)), params=params,
                                  headers=NASDAQ_HEADERS, settings=settings, attempts=2)
        bars = _parse_nasdaq_payload(response.json())
    except Exception as exc:
        return PairResult(error=f"Nasdaq 조회 실패: {exc}")
    return select_pair(bars, previous_session_date, session_date, "Nasdaq")


def _agree(first: PairResult, second: PairResult) -> bool:
    return (abs(first.previous_close - second.previous_close) <= PRICE_TOLERANCE
            and abs(first.close - second.close) <= PRICE_TOLERANCE)


def reconcile(ticker: str, primary: PairResult, secondary: PairResult | None, threshold_pct: float,
              validation_band_pct: float) -> tuple[dict | None, str | None]:
    """Combine sources into one row, or return a missing reason.

    Recall first: when the providers disagree, the symbol is flagged and is a
    candidate if EITHER provider's regular-close pair meets the threshold.
    """
    secondary = secondary or PairResult(error="교차검증 미실행")
    notes = [note for note in (primary.note,) if note]
    if primary.ok:
        change = calculate_change_pct(primary.previous_close, primary.close)
        row = {"previous_close": primary.previous_close, "close": primary.close, "change_pct": change,
               "source": "Yahoo", "fallback": False, "mismatch": False}
        if secondary.ok:
            if _agree(primary, secondary):
                row["validation"] = "Nasdaq 일치"
                row["cross_checked"] = True
            else:
                other_change = calculate_change_pct(secondary.previous_close, secondary.close)
                row.update(mismatch=True, cross_checked=True, alt_change_pct=other_change,
                           validation=(f"Nasdaq 불일치: Nasdaq {secondary.previous_close:.2f}→{secondary.close:.2f} "
                                       f"({other_change:+.2f}%)"))
                if is_drop(other_change, threshold_pct) and not is_drop(change, threshold_pct):
                    row.update(previous_close=secondary.previous_close, close=secondary.close,
                               change_pct=other_change, source="Nasdaq")
                    notes.append(f"Yahoo {primary.previous_close:.2f}→{primary.close:.2f} ({change:+.2f}%)는 미충족")
        else:
            row["cross_checked"] = False
            row["validation"] = "교차검증 불가"
            if change <= validation_band_pct:
                notes.append(f"2차 검증 불가: {secondary.error}")
    elif secondary.ok:
        change = calculate_change_pct(secondary.previous_close, secondary.close)
        row = {"previous_close": secondary.previous_close, "close": secondary.close, "change_pct": change,
               "source": "Nasdaq", "fallback": True, "mismatch": False, "cross_checked": False,
               "validation": "Yahoo 실패 → Nasdaq fallback"}
        notes.append(primary.error)
    else:
        return None, f"{primary.error} | {secondary.error}"
    row["detected"] = is_drop(row["change_pct"], threshold_pct)
    row["note"] = "; ".join(notes)
    return row, None


def download_market_data(
    constituents: pd.DataFrame,
    session_date: date,
    previous_session_date: date,
    settings: Settings = SETTINGS,
    *,
    cross_check: bool = True,
    threshold_pct: float | None = None,
) -> UsCollection:
    threshold_pct = settings.drop_threshold_pct if threshold_pct is None else threshold_pct
    tickers = constituents["yahoo_ticker"].tolist()
    started = time.monotonic()

    def run_pass(function, symbols: list[str]) -> dict[str, PairResult]:
        with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
            results = executor.map(lambda symbol: function(symbol, previous_session_date, session_date, settings), symbols)
            return dict(zip(symbols, results))

    primary = retry_until_available(run_pass(fetch_yahoo_pair, tickers), lambda failed: run_pass(fetch_yahoo_pair, failed),
                                    lambda pair: pair.ok, settings, "[US] Yahoo")
    LOGGER.info("[US] Yahoo 수집 완료 %.1fs: 유효 %d / %d", time.monotonic() - started,
                sum(pair.ok for pair in primary.values()), len(tickers))

    secondary: dict[str, PairResult] = {}
    secondary_available = cross_check
    if cross_check:
        lock = threading.Lock()
        counters = {"ok": 0, "fail": 0, "streak": 0}

        def guarded(ticker, previous, current, config):
            with lock:
                if counters["streak"] >= NASDAQ_CIRCUIT_BREAKER:
                    return PairResult(error="Nasdaq 교차검증 중단(연속 실패)")
            result = fetch_nasdaq_pair(ticker, previous, current, config)
            with lock:
                counters["ok" if result.ok else "fail"] += 1
                counters["streak"] = 0 if result.ok else counters["streak"] + 1
            return result

        # Validate the most drop-prone symbols first so they are never the
        # ones skipped by the circuit breaker.
        def order(ticker):
            pair = primary[ticker]
            return calculate_change_pct(pair.previous_close, pair.close) if pair.ok else -1000.0

        stage = time.monotonic()
        secondary = run_pass(guarded, sorted(tickers, key=order))
        secondary_available = counters["ok"] > 0
        LOGGER.info("[US] Nasdaq 교차검증 %.1fs: 성공 %d / 실패 %d", time.monotonic() - stage,
                    counters["ok"], counters["fail"])

    records, missing = [], {}
    names = constituents.set_index("yahoo_ticker")
    for ticker in tickers:
        row, reason = reconcile(ticker, primary[ticker], secondary.get(ticker),
                                threshold_pct, settings.us_validation_band_pct)
        if row is None:
            missing[ticker] = reason
            continue
        info = names.loc[ticker]
        records.append({"ticker": info["ticker"], "yahoo_ticker": ticker, "company_name": info["company_name"],
                        "sector": info.get("sector", ""), "session_date": session_date,
                        "previous_session_date": previous_session_date, **row})
    prices = pd.DataFrame(records)
    collection = UsCollection(
        prices=prices,
        missing=missing,
        fallback_count=int(prices["fallback"].sum()) if not prices.empty else 0,
        mismatch_count=int(prices["mismatch"].sum()) if not prices.empty else 0,
        cross_checked_count=int(prices["cross_checked"].sum()) if not prices.empty else 0,
        secondary_available=secondary_available,
    )
    if cross_check and not secondary_available:
        collection.notices.append("Nasdaq 교차검증 불가: Yahoo 단일 소스 결과")
    elif cross_check and collection.cross_checked_count < len(prices):
        collection.notices.append(f"Nasdaq 교차검증 {collection.cross_checked_count}/{len(prices)}종목 "
                                  "(하락률 큰 종목부터 검증)")
    if not prices.empty:
        band = prices["change_pct"] <= settings.us_validation_band_pct
        unverified = prices.loc[band & ~prices["cross_checked"]]
        if len(unverified):
            collection.warnings.append(f"검증 구간({settings.us_validation_band_pct:.0f}%) 이하 {len(unverified)}개 2차 검증 불가: "
                                       + ", ".join(unverified["ticker"]))
    return collection
