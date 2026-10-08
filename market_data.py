"""US regular-session close collection for the S&P 500 universe.

Sources (providers/):
    PRIMARY   Yahoo chart 1d ``quote.close`` (prev + close)
    SECONDARY CNBC quote, batched: analysis-day regular close ``last`` (+ the
              same-evening ``previous_day_closing`` as a supporting-only value)
    TERTIARY  Nasdaq historical Close/Last (prev + close; the analysis-day row is
              often not published until late evening -> STALE_SOURCE). Called
              only where Yahoo+CNBC did not already confirm both fields, and for
              every near-threshold symbol, to stay well below its rate limit.

Every field is validated separately (validation.assess). A late or failing
cross source is never treated as a price error by itself.

Detection uses the raw, unrounded change; rounding is display-only.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date

import pandas as pd

from config import SETTINGS, Settings
from net import retry_until_available
from providers import cnbc, nasdaq, yahoo
from providers.base import NOT_PROVIDED, FieldValue, PriceObservation, failed
from validation import assess

LOGGER = logging.getLogger(__name__)
# Closes are quoted in cents; Yahoo floats carry float32 noise (262.8699951).
PRICE_TOLERANCE = 0.011
# After this many CONSECUTIVE Nasdaq failures (blocked IP / throttling) the
# source is skipped for the rest of the run.
NASDAQ_CIRCUIT_BREAKER = 25
NASDAQ_WORKERS = 3
# More "trading ended" symbols than this in one run is treated as a data problem.
MAX_ENDED_SYMBOLS = 5


@dataclass
class UsCollection:
    prices: pd.DataFrame
    missing: dict[str, str]
    fallback_count: int = 0
    mismatch_count: int = 0
    cross_checked_count: int = 0
    source_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    # Listing changes detected from the data itself (constituent lists lag):
    excluded: dict[str, str] = field(default_factory=dict)     # no trade on either session -> not analyzed
    renamed: dict[str, str] = field(default_factory=dict)      # old ticker -> successor ticker
    transitions: dict[str, str] = field(default_factory=dict)  # missing because the symbol vanished


def listing_gap(observations: list[PriceObservation], previous_date: date, session_date: date) -> str | None:
    """Classify a symbol for which no source has a usable analysis-day close.

    "ENDED":      every source's last trade is before the previous session
                  (merger completed, delisted or halted): no regular close on
                  either day, so no drop can have happened.
    "TRANSITION": no analysis-day trade anywhere and at least one source no
                  longer knows the symbol (ticker change in progress, delisting).
    None:         ordinary data gap.
    """
    if any(o.close.usable for o in observations):
        return None
    latest = max((o.latest_date for o in observations if o.latest_date), default=None)
    unknown = any(o.symbol_unknown for o in observations)
    if latest is not None and latest < previous_date and not any(o.previous_close.usable for o in observations):
        return "ENDED"
    if unknown and (latest is None or latest < session_date):
        return "TRANSITION"
    return None


def _resolve_successor(ticker: str, company_name: str, old_primary: PriceObservation, exclude: set[str],
                       previous_date: date, session_date: date, settings: Settings, assess_symbol):
    """Analyze the successor symbol of a renamed constituent, or return None."""
    for candidate in yahoo.find_successor_symbols(ticker, company_name, exclude, settings):
        observations = [yahoo.fetch_us(candidate, previous_date, session_date, settings),
                        nasdaq.fetch(candidate, previous_date, session_date, settings),
                        cnbc.fetch_batch([candidate], session_date, settings, previous_date)[candidate]]
        if old_primary.previous_close.usable:
            # The old symbol's last close is the same security's previous close.
            observations.append(replace(old_primary, source=f"Yahoo(이전 티커 {ticker})",
                                        close=FieldValue(status=NOT_PROVIDED)))
        assessment = assess_symbol(candidate, observations)
        if assessment.valid:
            return candidate, assessment
        LOGGER.warning("[US] %s 후보 %s: 양일 종가 확보 실패 (%s)", ticker, candidate, assessment.reason)
    return None


def _stats(observations: dict[str, PriceObservation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for observation in observations.values():
        key = observation.close.status
        counts[key] = counts.get(key, 0) + 1
    return counts


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

    def run_pass(function, symbols: list[str], workers: int = settings.max_workers) -> dict[str, PriceObservation]:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(lambda symbol: function(symbol, previous_session_date, session_date, settings), symbols)
            return dict(zip(symbols, results))

    started = time.monotonic()
    primary = retry_until_available(
        run_pass(yahoo.fetch_us, tickers), lambda failed_symbols: run_pass(yahoo.fetch_us, failed_symbols),
        lambda observation: observation.close.usable, settings, "[US] Yahoo")
    LOGGER.info("[US] Yahoo %.1fs: 양일 유효 %d / %d", time.monotonic() - started,
                sum(o.close.usable and o.previous_close.usable for o in primary.values()), len(tickers))

    secondary: dict[str, PriceObservation] = {}
    tertiary: dict[str, PriceObservation] = {}
    if cross_check:
        stage = time.monotonic()
        tertiary = cnbc.fetch_batch(tickers, session_date, settings, previous_session_date)
        LOGGER.info("[US] CNBC %.1fs: 분석일 종가 상태 %s", time.monotonic() - stage, _stats(tertiary))

        def confirmed(ticker: str) -> bool:
            yahoo_obs, cnbc_obs = primary[ticker], tertiary.get(ticker)
            if cnbc_obs is None or not (yahoo_obs.close.usable and yahoo_obs.previous_close.usable):
                return False
            if not (cnbc_obs.close.usable and cnbc_obs.previous_close.usable):
                return False
            change = yahoo_obs.close.value / yahoo_obs.previous_close.value * 100 - 100
            return (abs(cnbc_obs.close.value - yahoo_obs.close.value) <= PRICE_TOLERANCE
                    and abs(cnbc_obs.previous_close.value - yahoo_obs.previous_close.value) <= PRICE_TOLERANCE
                    and change > settings.us_validation_band_pct)
        nasdaq_targets = [ticker for ticker in tickers if not confirmed(ticker)]

        lock = threading.Lock()
        counters = {"ok": 0, "fail": 0, "streak": 0, "next": 0.0}

        def guarded(ticker, previous, current, config):
            with lock:
                if counters["streak"] >= NASDAQ_CIRCUIT_BREAKER:
                    return failed(ticker, nasdaq.SOURCE, "Nasdaq 교차검증 중단(연속 실패)")
                wait = counters["next"] - time.monotonic()
                counters["next"] = max(counters["next"], time.monotonic()) + settings.nasdaq_min_interval_seconds
            if wait > 0:
                time.sleep(wait)
            result = nasdaq.fetch(ticker, previous, current, config)
            with lock:
                counters["ok" if result.has_any else "fail"] += 1
                counters["streak"] = 0 if result.has_any else counters["streak"] + 1
            return result

        # Most drop-prone first, so throttling never skips the relevant symbols.
        def order(ticker):
            observation = primary[ticker]
            if observation.close.usable and observation.previous_close.usable:
                return observation.close.value / observation.previous_close.value
            return -1.0

        stage = time.monotonic()
        # Nasdaq throttles bursts from one IP (HTTP 403 after ~200 fast requests): low concurrency.
        secondary = (run_pass(guarded, sorted(nasdaq_targets, key=order), min(settings.max_workers, NASDAQ_WORKERS))
                     if nasdaq_targets else {})
        LOGGER.info("[US] Nasdaq %.1fs: 대상 %d (Yahoo+CNBC 미확정·검증구간), 응답 %d / 실패 %d, 분석일 종가 상태 %s",
                    time.monotonic() - stage, len(nasdaq_targets), counters["ok"], counters["fail"], _stats(secondary))

    records, missing = [], {}
    excluded: dict[str, str] = {}
    renamed: dict[str, str] = {}
    transitions: dict[str, str] = {}
    names = constituents.set_index("yahoo_ticker")
    current_symbols = set(constituents["ticker"].str.upper()) | set(tickers)

    def assess_symbol(symbol: str, observations: list[PriceObservation]):
        return assess(symbol, observations, primary=yahoo.SOURCE, tolerance=PRICE_TOLERANCE,
                      threshold_pct=threshold_pct, validation_band_pct=settings.us_validation_band_pct)

    for ticker in tickers:
        observations = [primary[ticker]] + [source[ticker] for source in (secondary, tertiary) if ticker in source]
        assessment = assess_symbol(ticker, observations)
        listing_note = ""
        if not assessment.valid:
            gap = listing_gap(observations, previous_session_date, session_date) if cross_check else None
            latest = max((o.latest_date for o in observations if o.latest_date), default=None)
            successor = None
            if gap and any(o.symbol_unknown for o in observations):
                successor = _resolve_successor(ticker, names.loc[ticker]["company_name"], primary[ticker],
                                               current_symbols, previous_session_date, session_date, settings,
                                               assess_symbol)
            if successor is not None:
                new_ticker, assessment = successor
                renamed[ticker] = new_ticker
                listing_note = f"티커 변경 추정: {ticker} → {new_ticker} (회사명 일치, 구성종목 목록 갱신 전)"
                LOGGER.warning("[US] 티커 변경 추정 %s → %s", ticker, new_ticker)
            elif gap == "ENDED":
                excluded[ticker] = (f"최종 거래일 {latest}: {previous_session_date}·{session_date} 거래 없음 "
                                    "(인수·상장폐지·거래정지 추정) → 분석 대상 제외")
                continue
            else:
                missing[ticker] = assessment.reason
                if gap == "TRANSITION":
                    transitions[ticker] = (f"분석일 거래 기록 없음(최종 거래일 {latest}), 거래소 심볼 미인식 — "
                                           "티커 변경·상장폐지 추정, 새 티커를 찾지 못함: 확인 필요")
                continue
        info = names.loc[ticker]
        row = asdict(assessment)
        row["note"] = "; ".join(row.pop("notes"))
        row["outliers"] = "; ".join(row["outliers"])
        row["stale_sources"] = ",".join(row["stale_sources"])
        if listing_note:
            row["note"] = "; ".join(filter(None, [listing_note, row["note"]]))
            if row["severity"] == "NORMAL":
                row["severity"] = "INFO"
        display = renamed.get(ticker)
        records.append({"ticker": display or info["ticker"], "yahoo_ticker": display or ticker,
                        "company_name": info["company_name"], "sector": info.get("sector", ""),
                        "session_date": session_date, "previous_session_date": previous_session_date,
                        "listing_note": listing_note, "previous_ticker": ticker if display else "", **row})
    # Real delistings/mergers are rare. Many "ended" symbols at once means a data
    # problem, so they stay missing (WARNING) instead of leaving the universe.
    if len(excluded) > max(MAX_ENDED_SYMBOLS, len(tickers) * 0.01):
        LOGGER.warning("[US] 거래 종료 판정 %d종목 과다 → 제외하지 않고 누락 처리", len(excluded))
        for ticker, reason in excluded.items():
            missing[ticker] = f"거래 종료 판정 과다({len(excluded)}종목) — 데이터 소스 이상 가능: {reason}"
        excluded = {}
    prices = pd.DataFrame(records)
    collection = UsCollection(
        prices=prices, missing=missing,
        fallback_count=int(prices["fallback"].sum()) if not prices.empty else 0,
        mismatch_count=int(prices["mismatch"].sum()) if not prices.empty else 0,
        cross_checked_count=int(prices["cross_checked"].sum()) if not prices.empty else 0,
        source_stats={"Nasdaq": _stats(secondary), "CNBC": _stats(tertiary)},
        excluded=excluded, renamed=renamed, transitions=transitions,
    )
    nasdaq_stale = collection.source_stats["Nasdaq"].get("STALE_SOURCE", 0)
    if nasdaq_stale:
        collection.notices.append(f"Nasdaq 분석일 종가 미게시 {nasdaq_stale}종목")
    cnbc_stale = collection.source_stats["CNBC"].get("STALE_SOURCE", 0)
    if cnbc_stale:
        collection.notices.append(f"CNBC 분석일 스냅샷 아님 {cnbc_stale}종목")
    return collection
