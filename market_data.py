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

Fault isolation: adapters never raise; one symbol that cannot be processed is
reported as missing instead of failing the market; a provider that is clearly
down (consecutive failures) or past its time budget is skipped so the remaining
symbols fall through to the other sources.

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
from net import Budget, FailFast, retry_until_available
from providers import cnbc, nasdaq, yahoo
from providers.base import (NOT_PROVIDED, OK, UNAVAILABLE, FieldValue, PriceObservation, failed, internal_error)
from validation import assess

LOGGER = logging.getLogger(__name__)
# Closes are quoted in cents; Yahoo floats carry float32 noise (262.8699951).
PRICE_TOLERANCE = 0.011
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
    health: list[str] = field(default_factory=list)
    # Listing changes detected from the data itself (constituent lists lag):
    excluded: dict[str, str] = field(default_factory=dict)     # no trade on either session -> not analyzed
    renamed: dict[str, str] = field(default_factory=dict)      # old ticker -> successor ticker
    transitions: dict[str, str] = field(default_factory=dict)  # missing because the symbol vanished
    new_listings: dict[str, str] = field(default_factory=dict)  # first trading day: no previous close exists


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


def new_listing(observations: list[PriceObservation], previous_date: date, session_date: date) -> date | None:
    """First trading day of a newly listed symbol (spin-off, IPO), or None.

    The exchange data itself says the symbol had not traded before the analysis
    day (Yahoo ``firstTradeDate`` after the previous session) and no source has
    a regular previous close (a supporting-only reference price, e.g. CNBC's
    ``previous_day_closing`` for a spin-off, is not one): there is nothing to
    compare with, which is expected on a first day and not a data failure."""
    if any(o.previous_close.usable and not o.previous_close.corroborative for o in observations):
        return None
    first = next((o.first_trade_date for o in observations if o.first_trade_date), None)
    return first if first is not None and first > previous_date else None


def align_split_basis(observations: list[PriceObservation]) -> list[PriceObservation]:
    """Put every source's previous close on the same share basis.

    When Yahoo reports a split/merge taking effect on the analysis day, its history
    is split-adjusted. A source that still shows the raw pre-split close would look
    like a crash (200 -> 100 on a 2:1 split) or a mismatch. A value that equals
    Yahoo's previous close x the split factor is the unadjusted version of the same
    price: rebase it. Values that match neither are left alone (real disagreement).
    """
    anchor = next((o for o in observations if o.source == yahoo.SOURCE and o.split_factor != 1.0
                   and o.previous_close.usable), None)
    if anchor is None:
        return observations
    factor, reference = anchor.split_factor, anchor.previous_close.value
    tolerance = max(PRICE_TOLERANCE, reference * 0.003)
    aligned: list[PriceObservation] = []
    for observation in observations:
        field_value = observation.previous_close
        if (observation is not anchor and field_value.status == OK and field_value.value is not None
                and abs(field_value.value - reference) > tolerance
                and abs(field_value.value / factor - reference) <= tolerance):
            observation = replace(
                observation,
                previous_close=replace(field_value, value=round(field_value.value / factor, 4)),
                note="; ".join(filter(None, [observation.note, f"{observation.source} 분할 전 가격 "
                                             f"{field_value.value:,.2f}을 분할비율 {factor:g}로 조정"])))
        aligned.append(observation)
    return aligned


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


def _reachable(observation: PriceObservation) -> bool:
    """Did the provider answer (even "no data for that date")? Only transport/HTTP
    failures count towards declaring a provider down."""
    return observation.close.status != UNAVAILABLE or observation.symbol_unknown


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
    health: list[str] = []

    def run_pass(function, symbols: list[str], workers: int = settings.max_workers,
                 breaker: FailFast | None = None) -> dict[str, PriceObservation]:
        def call(symbol: str) -> PriceObservation:
            if breaker is not None:
                reason = breaker.blocked()
                if reason:
                    return failed(symbol, breaker.name, reason)
            try:
                observation = function(symbol, previous_session_date, session_date, settings)
            except Exception as exc:  # adapters are isolated already; this is the second line
                observation = internal_error(symbol, breaker.name if breaker else "source", exc)
            if breaker is not None:
                breaker.record(_reachable(observation))
            return observation

        with ThreadPoolExecutor(max_workers=workers) as executor:
            return dict(zip(symbols, executor.map(call, symbols)))

    started = time.monotonic()
    primary_budget = Budget(settings.primary_budget_seconds)
    yahoo_breakers: list[FailFast] = []

    def yahoo_pass(symbols: list[str]) -> dict[str, PriceObservation]:
        breaker = FailFast("Yahoo", settings.provider_failure_threshold, primary_budget)
        yahoo_breakers.append(breaker)
        return run_pass(yahoo.fetch_us, symbols, breaker=breaker)

    primary = retry_until_available(yahoo_pass(tickers), yahoo_pass, lambda observation: observation.close.usable,
                                    settings, "[US] Yahoo", primary_budget)
    health.extend(breaker.summary() for breaker in yahoo_breakers)
    LOGGER.info("[US] Yahoo %.1fs: 양일 유효 %d / %d | %s", time.monotonic() - started,
                sum(o.close.usable and o.previous_close.usable for o in primary.values()), len(tickers),
                health[-1] if health else "")

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
        nasdaq_breaker = FailFast("Nasdaq", settings.provider_failure_threshold,
                                  Budget(settings.secondary_budget_seconds))
        pace_lock = threading.Lock()
        pace = {"next": 0.0}

        def paced_nasdaq(ticker, previous, current, config):
            # Nasdaq throttles bursts from one IP (HTTP 403 after ~200 fast requests).
            with pace_lock:
                wait = pace["next"] - time.monotonic()
                pace["next"] = max(pace["next"], time.monotonic()) + settings.nasdaq_min_interval_seconds
            if wait > 0:
                time.sleep(wait)
            return nasdaq.fetch(ticker, previous, current, config)

        # Most drop-prone first, so throttling or the budget never skips the relevant symbols.
        def order(ticker):
            observation = primary[ticker]
            if observation.close.usable and observation.previous_close.usable:
                return observation.close.value / observation.previous_close.value
            return -1.0

        stage = time.monotonic()
        secondary = (run_pass(paced_nasdaq, sorted(nasdaq_targets, key=order),
                              min(settings.max_workers, NASDAQ_WORKERS), nasdaq_breaker)
                     if nasdaq_targets else {})
        health.append(nasdaq_breaker.summary())
        LOGGER.info("[US] Nasdaq %.1fs: 대상 %d (Yahoo+CNBC 미확정·검증구간), %s, 분석일 종가 상태 %s",
                    time.monotonic() - stage, len(nasdaq_targets), nasdaq_breaker.summary(), _stats(secondary))

    records, missing = [], {}
    excluded: dict[str, str] = {}
    renamed: dict[str, str] = {}
    transitions: dict[str, str] = {}
    new_listings: dict[str, str] = {}
    names = constituents.set_index("yahoo_ticker")
    current_symbols = set(constituents["ticker"].str.upper()) | set(tickers)

    def assess_symbol(symbol: str, observations: list[PriceObservation]):
        return assess(symbol, align_split_basis(observations), primary=yahoo.SOURCE, tolerance=PRICE_TOLERANCE,
                      threshold_pct=threshold_pct, validation_band_pct=settings.us_validation_band_pct)

    def process(ticker: str) -> None:
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
                return
            elif (first_day := new_listing(observations, previous_session_date, session_date)) is not None:
                new_listings[ticker] = (f"첫 거래일({first_day}): 전일 종가가 없어 등락률 계산 대상 아님 "
                                        "(스핀오프·신규 상장) — 다음 거래일부터 분석")
                return
            else:
                missing[ticker] = assessment.reason
                if gap == "TRANSITION":
                    transitions[ticker] = (f"분석일 거래 기록 없음(최종 거래일 {latest}), 거래소 심볼 미인식 — "
                                           "티커 변경·상장폐지 추정, 새 티커를 찾지 못함: 확인 필요")
                return
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

    for ticker in tickers:
        try:
            process(ticker)
        except Exception as exc:  # one unprocessable symbol must never fail the whole market
            LOGGER.exception("[US] %s 처리 중 내부 오류", ticker)
            renamed.pop(ticker, None)
            excluded.pop(ticker, None)
            new_listings.pop(ticker, None)
            missing[ticker] = f"내부 처리 오류: {type(exc).__name__}: {exc}"

    # Real delistings/mergers are rare. Many "ended" symbols at once means a data
    # problem, so they stay missing (WARNING) instead of leaving the universe.
    if len(excluded) > max(MAX_ENDED_SYMBOLS, len(tickers) * 0.01):
        LOGGER.warning("[US] 거래 종료 판정 %d종목 과다 → 제외하지 않고 누락 처리", len(excluded))
        for ticker, reason in excluded.items():
            missing[ticker] = f"거래 종료 판정 과다({len(excluded)}종목) — 데이터 소스 이상 가능: {reason}"
        excluded = {}
    # A handful of spin-offs/IPOs on one day is normal; a flood means the "first
    # trade" evidence itself is unreliable, so those stay visible as missing data.
    if len(new_listings) > max(MAX_ENDED_SYMBOLS, len(tickers) * 0.01):
        LOGGER.warning("[US] 신규 상장 판정 %d종목 과다 → 제외하지 않고 누락 처리", len(new_listings))
        for ticker, reason in new_listings.items():
            missing[ticker] = f"신규 상장 판정 과다({len(new_listings)}종목) — 데이터 소스 이상 가능: {reason}"
        new_listings = {}
    prices = pd.DataFrame(records)
    collection = UsCollection(
        prices=prices, missing=missing,
        fallback_count=int(prices["fallback"].sum()) if not prices.empty else 0,
        mismatch_count=int(prices["mismatch"].sum()) if not prices.empty else 0,
        cross_checked_count=int(prices["cross_checked"].sum()) if not prices.empty else 0,
        source_stats={"Nasdaq": _stats(secondary), "CNBC": _stats(tertiary)},
        excluded=excluded, renamed=renamed, transitions=transitions, health=health, new_listings=new_listings,
    )
    nasdaq_stale = collection.source_stats["Nasdaq"].get("STALE_SOURCE", 0)
    if nasdaq_stale:
        collection.notices.append(f"Nasdaq 분석일 종가 미게시 {nasdaq_stale}종목")
    cnbc_stale = collection.source_stats["CNBC"].get("STALE_SOURCE", 0)
    if cnbc_stale:
        collection.notices.append(f"CNBC 분석일 스냅샷 아님 {cnbc_stale}종목")
    tripped = [line for line in health if "장애" in line or "소진" in line]
    if tripped:
        collection.notices.append("데이터 소스 장애 감지: " + "; ".join(tripped))
    return collection
