"""KOSPI regular-session close collection.

Universe: Naver KOSPI market listing, filtered to common/preferred stocks
(ETF/ETN excluded). The listing is FAIL-SOFT: a count discrepancy is logged and
reported as a WARNING while every loaded stock is still analyzed; only a
structurally broken listing (large loss, too few stocks) fails the market.

Prices: see the "Prices" section below (Yahoo KRX close + Daum + Naver, field-level
consensus in validation.py, special-trading rules in special_trading.py).
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta

import pandas as pd
import pandas_market_calendars as mcal

from config import SETTINGS, Settings
from market_calendar import KST
from net import retry_until_available
from providers import daum, krx_official, naver, yahoo
from providers.base import NOT_PROVIDED, OK, FieldValue, PriceObservation
from special_trading import SPECIAL_TRADING_UNCONFIRMED, SPECIAL_TRADING_VALIDATED, assess_kr_move
from validation import INFO, NORMAL, WARNING, assess

LOGGER = logging.getLogger(__name__)

NAVER_MARKET_URL = "https://m.stock.naver.com/api/stocks/marketValue/KOSPI"
NAVER_INDEX_URL = "https://m.stock.naver.com/api/index/KOSPI/price"
LIST_PAGE_SIZE = 100
LISTING_ATTEMPTS = 3
# Listing integrity limits (no hard-coded exact count: listings change daily).
MIN_LISTING_RATIO = 0.98
MIN_STOCK_COUNT = 500


class ListingError(RuntimeError):
    pass


@dataclass(frozen=True)
class KospiSessionContext:
    session_date: date
    previous_session_date: date
    warning: str | None = None
    warning_level: str = "WARNING"


@dataclass
class KrCollection:
    prices: pd.DataFrame
    missing: dict[str, str]
    fallback_count: int = 0
    mismatch_count: int = 0
    cross_checked_count: int = 0
    notices: list[str] = field(default_factory=list)


def _json(url: str, params: dict, settings: Settings = SETTINGS):
    return naver.json_get(url, params, settings)


# --------------------------------------------------------------------------
# Trading calendar
# --------------------------------------------------------------------------
def _xkrx_sessions(start: date, end: date) -> list[date]:
    return list(mcal.get_calendar("XKRX").schedule(start_date=start, end_date=end).index.date)


def get_kospi_session_context(session_date: date | None = None, now: datetime | None = None,
                              settings: Settings = SETTINGS) -> KospiSessionContext:
    """Analysis/previous sessions from ACTUAL traded dates (KOSPI index history).

    The XKRX calendar is an independent cross-check; if the index history is
    unavailable the calendar alone is used with a warning (fail-soft).
    """
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(KST)
    # KRX close 15:30, closing prices final by ~16:30 (Naver closePriceSendTime).
    cutoff = local_now.date() if local_now.hour >= 18 else local_now.date() - timedelta(days=1)
    if session_date is not None and session_date > cutoff:
        raise ValueError("KOSPI session is not completed/settled")
    end = session_date or cutoff
    calendar = [day for day in _xkrx_sessions(end - timedelta(days=60), end) if day <= end]

    try:
        traded: set[date] = set()
        for page in range(1, 8):
            rows = _json(NAVER_INDEX_URL, {"page": page, "pageSize": 20}, settings)
            if not rows:
                break
            traded.update(date.fromisoformat(str(row["localTradedAt"])[:10]) for row in rows)
            eligible = sorted(day for day in traded if day <= end)
            if len(eligible) >= 2 and eligible[0] < (session_date or eligible[-1]):
                break
    except Exception as exc:
        if len(calendar) < 2:
            raise ValueError("KOSPI 거래일 확인 불가: 지수 이력과 XKRX 캘린더 모두 실패") from exc
        if session_date is not None and session_date not in calendar:
            raise ValueError(f"{session_date} is not a KOSPI trading session") from exc
        current = session_date or calendar[-1]
        previous = max(day for day in calendar if day < current)
        LOGGER.warning("[KR] 지수 이력 조회 실패 → XKRX 캘린더 사용: %s", exc)
        # The exchange calendar alone is a reliable basis: informational only.
        return KospiSessionContext(current, previous, f"KOSPI 지수 이력 조회 실패, XKRX 캘린더로 거래일 판정 ({exc})", "INFO")

    eligible = sorted(day for day in traded if day <= end)
    if session_date is not None:
        if session_date not in traded:
            raise ValueError(f"{session_date} is not a KOSPI trading session")
        current = session_date
    else:
        if not eligible:
            raise ValueError("KOSPI reference history is empty")
        current = eligible[-1]
    older = [day for day in eligible if day < current]
    if not older:
        raise ValueError("KOSPI previous session unavailable")
    previous = older[-1]
    warning = None
    calendar_pair = [day for day in calendar if day <= current][-2:]
    if calendar_pair != [previous, current]:
        warning = (f"KRX 캘린더와 지수 거래일 불일치: 지수 ({previous}, {current}) / 캘린더 {calendar_pair} "
                   "— 임시휴장·공급 지연 확인 필요")
    return KospiSessionContext(current, previous, warning)


# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------
def _fetch_listing_pass(settings: Settings) -> tuple[int, list[dict]]:
    first = _json(NAVER_MARKET_URL, {"page": 1, "pageSize": LIST_PAGE_SIZE}, settings)
    if not isinstance(first, dict):
        raise ListingError("KOSPI listing response is not an object")
    total = int(first.get("totalCount") or 0)
    if total <= 0:
        raise ListingError("KOSPI listing totalCount is empty")
    items = list(first.get("stocks") or [])
    for page in range(2, math.ceil(total / LIST_PAGE_SIZE) + 1):
        payload = _json(NAVER_MARKET_URL, {"page": page, "pageSize": LIST_PAGE_SIZE}, settings)
        if not isinstance(payload, dict):
            raise ListingError(f"KOSPI listing page {page} is not an object")
        page_total = int(payload.get("totalCount") or 0)
        if page_total != total:
            LOGGER.warning("[KR] listing totalCount 변경 (page %d: %d → %d)", page, total, page_total)
            total = max(total, page_total)
        items.extend(payload.get("stocks") or [])
    return total, items


def load_kospi_constituents(settings: Settings = SETTINGS) -> pd.DataFrame:
    """Return the KOSPI stock universe; integrity notes live in frame.attrs."""
    expected, merged, warnings = 0, {}, []
    last_error: Exception | None = None
    for attempt in range(1, LISTING_ATTEMPTS + 1):
        try:
            total, items = _fetch_listing_pass(settings)
        except Exception as exc:
            last_error = exc
            LOGGER.warning("[KR] listing 조회 실패 (시도 %d/%d): %s", attempt, LISTING_ATTEMPTS, exc)
            time.sleep(settings.retry_backoff_seconds * attempt)
            continue
        expected = max(expected, total)
        codes = [str(item.get("itemCode") or "").strip() for item in items]
        duplicates = sorted({code for code in codes if code and codes.count(code) > 1})
        if duplicates:
            LOGGER.warning("[KR] listing 페이지 중복 %d개 (정렬 변동 가능): %s", len(duplicates), duplicates[:20])
        for item, code in zip(items, codes):
            merged.setdefault(code or f"<blank:{item.get('stockName')}>", item)
        if len(items) == total and not duplicates:
            break
        LOGGER.warning("[KR] listing 불일치 (시도 %d/%d): expected %d, loaded %d, 누적 고유 %d",
                       attempt, LISTING_ATTEMPTS, total, len(items), len(merged))
        time.sleep(settings.retry_backoff_seconds * attempt)
    if not merged:
        raise ListingError(f"KOSPI listing unavailable: {last_error}")

    loaded = len(merged)
    unresolved = max(expected - loaded, 0)
    LOGGER.info("[KR] KOSPI listing: expected %d / loaded %d / missing-unresolved %d", expected, loaded, unresolved)
    if loaded / expected < MIN_LISTING_RATIO:
        raise ListingError(f"KOSPI listing broken: {loaded}/{expected} loaded (< {MIN_LISTING_RATIO:.0%})")
    if unresolved:
        warnings.append(f"KOSPI listing: expected {expected}, loaded {loaded}, missing/unresolved {unresolved} "
                        "(ETF/ETN 포함 전체 목록 기준; 누락 항목은 공급자 목록에서 식별 불가)")

    records, skipped, product_counts = [], [], {}
    for key, item in merged.items():
        end_type = item.get("stockEndType")
        product_counts[end_type] = product_counts.get(end_type, 0) + 1
        if end_type != "stock":
            continue
        exchange = (item.get("stockExchangeType") or {}).get("nameEng")
        code = str(item.get("itemCode") or "").strip().upper()
        name = str(item.get("stockName") or "").strip()
        if exchange != "KOSPI":
            skipped.append(f"{key}: 시장 {exchange}")
            continue
        if len(code) != 6 or not code.isalnum() or not name:
            skipped.append(f"{key}: 코드/종목명 파싱 실패 ({code!r}, {name!r})")
            continue
        records.append({"code": code, "company_name": name})
    if skipped:
        warnings.append(f"KOSPI listing 파싱 제외 {len(skipped)}건: " + "; ".join(skipped[:10]))
        LOGGER.warning("[KR] listing 파싱 제외 %d건: %s", len(skipped), skipped)
    if len(records) < MIN_STOCK_COUNT:
        raise ListingError(f"KOSPI stock universe too small: {len(records)} (< {MIN_STOCK_COUNT})")
    frame = pd.DataFrame(records).sort_values("code").reset_index(drop=True)
    frame.attrs.update(expected=expected, loaded=loaded, unresolved=unresolved, warnings=warnings,
                       product_counts=product_counts)
    LOGGER.info("[KR] 상품 구성 %s → 분석 대상 주식 %d개", product_counts, len(frame))
    return frame


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------
# Sources (providers/), all for the EXACT analysis/previous dates:
#   PRIMARY    Yahoo <code>.KS            KRX regular close (prev + close)
#   SECONDARY  Daum quote snapshot        KRX regular close (regularTradePrice), KRX base,
#                                         market state (정리매매/거래정지/액면변경/...)
#   TERTIARY   Naver daily                KRX base of the analysis day (= prev close);
#                                         integrated close only when the stock has no NXT
#                                         trading or had no trades at all
#   ON DEMAND  Daum daily rows            KRX base (when prev is not yet confirmed by 2)
#   OPTIONAL   KRX Open API (KRX_API_KEY), KIS (KIS_VALIDATE=1)
# Naver/Daum "close" is the KRX+NXT integrated last trade and is never used as a
# regular close for NXT-traded stocks.
PRICE_TOLERANCE_KRW = 0.5


def _xkrx_next_session(day: date) -> date | None:
    sessions = [session for session in _xkrx_sessions(day, day + timedelta(days=20)) if session > day]
    return sessions[0] if sessions else None


def download_kospi_market_data(
    constituents: pd.DataFrame,
    context: KospiSessionContext,
    settings: Settings = SETTINGS,
    *,
    krx_client=None,
    validator=None,
    threshold_pct: float | None = None,
) -> KrCollection:
    threshold_pct = settings.kr_drop_threshold_pct if threshold_pct is None else threshold_pct
    previous_date, session_date = context.previous_session_date, context.session_date
    next_session = _xkrx_next_session(session_date)
    codes = constituents["code"].tolist()
    names = dict(zip(constituents["code"], constituents["company_name"]))
    notices: list[str] = []
    # Older manual sessions need more Naver history rows.
    page_size = 10 if (date.today() - session_date).days <= 7 else 60

    def pool_map(function, symbols):
        with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
            return dict(zip(symbols, executor.map(function, symbols)))

    def fetch_yahoo(symbols):
        return pool_map(lambda code: yahoo.fetch_kr(code, previous_date, session_date, settings), symbols)

    started = time.monotonic()
    # Availability is judged on the analysis-day close only: a missing previous
    # close (e.g. halted the day before) is not "data not published yet".
    primary = retry_until_available(fetch_yahoo(codes), fetch_yahoo, lambda o: o.close.usable, settings,
                                    "[KR] Yahoo(KRX)")
    stage = time.monotonic()
    daum_quotes = pool_map(lambda code: daum.fetch_quote(code, previous_date, session_date, next_session, settings),
                           codes)
    naver_rows = pool_map(lambda code: naver.fetch_daily(code, previous_date, session_date, settings, page_size,
                                                         next_session), codes)
    daum_status: dict[str, int] = {}
    for observation, _state in daum_quotes.values():
        daum_status[observation.close.status] = daum_status.get(observation.close.status, 0) + 1
    LOGGER.info("[KR] Yahoo(KRX) %.1fs 양일 유효 %d / %d | Daum 분석일 종가 상태 %s | Naver 기준가 %d (%.1fs)",
                stage - started, sum(o.close.usable and o.previous_close.usable for o in primary.values()), len(codes),
                daum_status, sum(row[0].previous_close.usable for row in naver_rows.values()), time.monotonic() - stage)

    official: dict[str, PriceObservation] = {}
    if krx_client is not None:
        try:
            official = krx_official.observations(krx_client, codes, previous_date, session_date)
            LOGGER.info("[KR] KRX 공식 종가 교차검증 사용: %d 종목", len(official))
        except Exception as exc:
            notices.append(f"KRX 공식 교차검증 불가: {exc}")
            LOGGER.warning("[KR] KRX 공식 교차검증 불가: %s", exc)

    def observations_for(code: str, extra: list[PriceObservation]) -> list[PriceObservation]:
        quote, state = daum_quotes[code]
        naver_observation, naver_day, naver_next = naver_rows[code]
        # The integrated close equals the KRX close when the stock has no NXT
        # trading, or when nothing traded at all (close == base, volume 0).
        if state is not None and state.nxt_tradable is False:
            naver_observation = naver.as_regular(naver_observation, "비NXT 종목")
            extra = [naver.as_regular(item, "비NXT 종목") for item in extra]
        no_trade = bool(naver_day and naver_day.volume == 0 and naver_day.integrated_close == naver_day.krx_base_price)
        if no_trade:
            if naver_observation.close.session_type != "REGULAR":
                naver_observation = naver.as_regular(naver_observation, "무거래")
            # A day without trades keeps the base price: when Daum's KRX close for the
            # day independently equals that base, it also confirms the previous close.
            if quote.close.usable and quote.close.value == naver_day.krx_base_price:
                quote = replace(quote, source=f"{quote.source}·무거래",
                                previous_close=FieldValue(quote.close.value, previous_date, OK))
            # KRX rule: without trades the close is the base price. When Yahoo's own
            # previous close independently equals Naver's base, that confirms the close.
            yahoo_previous = primary[code].previous_close
            if yahoo_previous.usable and yahoo_previous.value == naver_day.krx_base_price:
                extra = [*extra, PriceObservation(code, "무거래 규칙(Yahoo 전일종가=KRX 기준가)",
                                                  close=FieldValue(yahoo_previous.value, session_date, OK),
                                                  previous_close=FieldValue(status=NOT_PROVIDED))]
        observations = [primary[code], quote, naver_observation, *([naver_next] if naver_next else []), *extra]
        if code in official:
            observations.append(official[code])
        return observations

    # Confirm previous closes that do not yet have two agreeing sources.
    def needs_days(code: str) -> bool:
        values = [o.previous_close.value for o in observations_for(code, []) if o.previous_close.usable]
        return not any(values.count(value) >= 2 for value in values) or not primary[code].close.usable

    on_demand = [code for code in codes if needs_days(code)]
    daum_days = pool_map(lambda code: daum.fetch_days(code, previous_date, session_date, settings), on_demand)
    if on_demand:
        LOGGER.info("[KR] Daum 일별 기준가 추가 조회 %d종목", len(on_demand))

    records, missing = [], {}
    for code in codes:
        observations = observations_for(code, [daum_days[code]] if code in daum_days else [])
        assessment = assess(code, observations, primary=yahoo.SOURCE, tolerance=PRICE_TOLERANCE_KRW,
                            threshold_pct=threshold_pct, validation_band_pct=settings.kr_validation_band_pct,
                            authoritative=krx_official.SOURCE if official else None)
        if not assessment.valid:
            missing[code] = assessment.reason
            continue
        quote, state = daum_quotes[code]
        naver_day = naver_rows[code][1]
        base = (naver_day.krx_base_price if naver_day and naver_day.krx_base_price
                else quote.previous_close.value if quote.previous_close.usable else None)
        move = assess_kr_move(assessment.previous_close, assessment.close, base, state, session_date)
        row = asdict(assessment)
        notes = row.pop("notes")
        if move.note:
            notes.append(move.note)
        no_trade = bool(naver_day and naver_day.volume == 0 and naver_day.integrated_close == naver_day.krx_base_price)
        if no_trade and row["validation_status"] == "FALLBACK_VALIDATED" and row["change_pct"] == 0:
            # Yahoo prints no bar for a day without trades; the close is the base
            # price by exchange rule, confirmed by two sources: not a fallback.
            row.update(fallback=False, validation="무거래(종가=기준가) 교차검증", severity=NORMAL)
        if move.label == SPECIAL_TRADING_VALIDATED and row["severity"] == NORMAL:
            row["severity"] = INFO
        elif move.label == SPECIAL_TRADING_UNCONFIRMED:
            row["severity"] = WARNING
        row.update(note="; ".join(notes), outliers="; ".join(row["outliers"]),
                   stale_sources=",".join(row["stale_sources"]), special_label=move.label,
                   special_note=move.note, special_reasons=", ".join(move.reasons),
                   krx_base_price=base, nxt_tradable=None if state is None else state.nxt_tradable)
        if validator is not None and row["detected"]:
            try:
                validator.validate(code, previous_date, session_date, row["previous_close"], row["close"])
                row["validation"] += " · KIS 일치"
            except Exception as exc:
                row["mismatch"] = True
                row["severity"] = WARNING
                row["note"] = "; ".join(filter(None, [row["note"], f"KIS 검증: {exc}"]))
        records.append({"code": code, "company_name": names[code], "session_date": session_date,
                        "previous_session_date": previous_date, **row})

    prices = pd.DataFrame(records)
    return KrCollection(
        prices=prices, missing=missing, notices=notices,
        fallback_count=int(prices["fallback"].sum()) if not prices.empty else 0,
        mismatch_count=int(prices["mismatch"].sum()) if not prices.empty else 0,
        cross_checked_count=int(prices["cross_checked"].sum()) if not prices.empty else 0,
    )
