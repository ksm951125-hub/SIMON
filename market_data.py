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
from dataclasses import asdict, dataclass, field
from datetime import date

import pandas as pd

from config import SETTINGS, Settings
from net import retry_until_available
from providers import cnbc, nasdaq, yahoo
from providers.base import PriceObservation, failed
from validation import assess

LOGGER = logging.getLogger(__name__)
# Closes are quoted in cents; Yahoo floats carry float32 noise (262.8699951).
PRICE_TOLERANCE = 0.011
# After this many CONSECUTIVE Nasdaq failures (blocked IP / throttling) the
# source is skipped for the rest of the run.
NASDAQ_CIRCUIT_BREAKER = 25
NASDAQ_WORKERS = 3


@dataclass
class UsCollection:
    prices: pd.DataFrame
    missing: dict[str, str]
    fallback_count: int = 0
    mismatch_count: int = 0
    cross_checked_count: int = 0
    source_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


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
    names = constituents.set_index("yahoo_ticker")
    for ticker in tickers:
        observations = [primary[ticker]] + [source[ticker] for source in (secondary, tertiary) if ticker in source]
        assessment = assess(ticker, observations, primary=yahoo.SOURCE, tolerance=PRICE_TOLERANCE,
                            threshold_pct=threshold_pct, validation_band_pct=settings.us_validation_band_pct)
        if not assessment.valid:
            missing[ticker] = assessment.reason
            continue
        info = names.loc[ticker]
        row = asdict(assessment)
        row["note"] = "; ".join(row.pop("notes"))
        row["outliers"] = "; ".join(row["outliers"])
        row["stale_sources"] = ",".join(row["stale_sources"])
        records.append({"ticker": info["ticker"], "yahoo_ticker": ticker, "company_name": info["company_name"],
                        "sector": info.get("sector", ""), "session_date": session_date,
                        "previous_session_date": previous_session_date, **row})
    prices = pd.DataFrame(records)
    collection = UsCollection(
        prices=prices, missing=missing,
        fallback_count=int(prices["fallback"].sum()) if not prices.empty else 0,
        mismatch_count=int(prices["mismatch"].sum()) if not prices.empty else 0,
        cross_checked_count=int(prices["cross_checked"].sum()) if not prices.empty else 0,
        source_stats={"Nasdaq": _stats(secondary), "CNBC": _stats(tertiary)},
    )
    nasdaq_stale = collection.source_stats["Nasdaq"].get("STALE_SOURCE", 0)
    if nasdaq_stale:
        collection.notices.append(f"Nasdaq 분석일 종가 미게시 {nasdaq_stale}종목")
    cnbc_stale = collection.source_stats["CNBC"].get("STALE_SOURCE", 0)
    if cnbc_stale:
        collection.notices.append(f"CNBC 분석일 스냅샷 아님 {cnbc_stale}종목")
    return collection
