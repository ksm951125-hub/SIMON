"""KOSPI regular-session close collection.

Universe: Naver KOSPI market listing, filtered to common/preferred stocks
(ETF/ETN excluded). The listing is FAIL-SOFT: a count discrepancy is logged and
reported as a WARNING while every loaded stock is still analyzed; only a
structurally broken listing (large loss, too few stocks) fails the market.

Prices: Yahoo `<code>.KS` daily bars = KRX regular-session closes (primary).
Naver daily rows provide the KRX base price of the analysis day (independent
check of the previous close) and a flagged fallback. Optional official checks
when configured: KRX Open API bulk closes (KRX_API_KEY), KIS (KIS_VALIDATE=1).
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pandas_market_calendars as mcal

from config import SETTINGS, Settings
from detector import calculate_change_pct, is_drop
from market_calendar import KST
from net import get_with_retry, retry_until_available

LOGGER = logging.getLogger(__name__)

NAVER_MARKET_URL = "https://m.stock.naver.com/api/stocks/marketValue/KOSPI"
NAVER_PRICE_URL = "https://m.stock.naver.com/api/stock/{code}/price"
NAVER_INDEX_URL = "https://m.stock.naver.com/api/index/KOSPI/price"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://m.stock.naver.com/"}
LIST_PAGE_SIZE = 100
LISTING_ATTEMPTS = 3
# Listing integrity limits (no hard-coded exact count: listings change daily).
MIN_LISTING_RATIO = 0.98
MIN_STOCK_COUNT = 500
# KOSPI daily price limit is +-30%; anything beyond implies a reference-price
# reset (split/reverse split/re-listing) and needs manual confirmation.
PRICE_LIMIT_PCT = 30.01


class ListingError(RuntimeError):
    pass


@dataclass(frozen=True)
class KospiSessionContext:
    session_date: date
    previous_session_date: date
    warning: str | None = None


@dataclass
class KrCollection:
    prices: pd.DataFrame
    missing: dict[str, str]
    fallback_count: int = 0
    mismatch_count: int = 0
    cross_checked_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _json(url: str, params: dict, settings: Settings = SETTINGS):
    return get_with_retry(url, params=params, headers=NAVER_HEADERS, settings=settings).json()


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
        return KospiSessionContext(current, previous, f"KOSPI 지수 이력 조회 실패, XKRX 캘린더로 거래일 판정 ({exc})")

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
# Why not Naver closes: since Nextrade (NXT) launched, Naver/Daum daily
# "closePrice" is the integrated last trade, which includes NXT after-market
# prints until 20:00 KST. It differs from the KRX regular close for most
# stocks. Naver's `closePrice - compareToPreviousClosePrice` however is the
# KRX base price of that day (= previous KRX regular close unless a corporate
# action reset it), which makes it an independent check of the previous close.
@dataclass
class KrPair:
    previous_close: float | None = None
    close: float | None = None
    volume: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.previous_close is not None and self.close is not None


@dataclass
class NaverDay:
    """Naver mobile daily row: integrated close + KRX base price for the day."""
    integrated_close: float | None
    krx_base_price: float | None
    volume: float | None
    official_change_pct: float | None


def _positive(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _signed(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def yahoo_kr_symbol(code: str) -> str:
    return f"{code}.KS"


def _parse_yahoo_kr(payload: dict, code: str) -> dict[date, tuple[float | None, float | None]]:
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise ValueError(f"Yahoo 응답에 {code} 데이터 없음: {chart.get('error')}")
    result = results[0]
    meta = result.get("meta") or {}
    if str(meta.get("symbol", yahoo_kr_symbol(code))).upper() != yahoo_kr_symbol(code).upper():
        raise ValueError(f"응답 symbol 불일치 ({meta.get('symbol')})")
    if meta.get("dataGranularity", "1d") != "1d":
        raise ValueError("일봉 이외 데이터 거부")
    if (meta.get("exchangeTimezoneName") or "Asia/Seoul") != "Asia/Seoul":
        raise ValueError("unexpected KRX timezone")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or [None] * len(timestamps)
    if len(closes) != len(timestamps) or len(volumes) != len(timestamps):
        raise ValueError("일봉 timestamp/close 길이 불일치")
    values: dict[date, tuple[float | None, float | None]] = {}
    for timestamp, close, volume in zip(timestamps, closes, volumes):
        day = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(KST).date()
        if day in values:
            raise ValueError(f"중복 거래일 데이터 ({day})")
        price = _positive(close)
        # KRW prices are whole won; strip float32 noise.
        values[day] = (None if price is None else float(round(price)), None if volume is None else float(volume))
    return values


def fetch_yahoo_kr_pair(code: str, previous_date: date, session_date: date, settings: Settings = SETTINGS) -> KrPair:
    start, end = previous_date - timedelta(days=7), session_date + timedelta(days=2)
    params = {
        "period1": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()),
        "interval": "1d", "includePrePost": "false", "events": "div,splits",
    }
    errors = []
    for host in ("query1", "query2"):
        try:
            response = get_with_retry(f"https://{host}.finance.yahoo.com/v8/finance/chart/{yahoo_kr_symbol(code)}",
                                      params=params, headers=YAHOO_HEADERS, settings=settings)
            values = _parse_yahoo_kr(response.json(), code)
        except Exception as exc:
            errors.append(f"{host}: {exc}")
            continue
        previous, current = values.get(previous_date), values.get(session_date)
        missing = [str(day) for day, item in ((previous_date, previous), (session_date, current))
                   if item is None or item[0] is None]
        if missing:
            return KrPair(error=f"Yahoo(KRX) 필수 거래일 종가 누락 ({', '.join(missing)}; 최신 {max(values) if values else None})")
        return KrPair(previous[0], current[0], current[1])
    return KrPair(error="Yahoo(KRX) 조회 실패: " + " / ".join(errors))


def fetch_naver_days(code: str, settings: Settings = SETTINGS, page_size: int = 10) -> dict[date, NaverDay]:
    rows = _json(NAVER_PRICE_URL.format(code=code), {"page": 1, "pageSize": page_size}, settings)
    if not isinstance(rows, list):
        raise ValueError("Naver daily price response is not a list")
    days: dict[date, NaverDay] = {}
    for row in rows:
        day = date.fromisoformat(str(row["localTradedAt"])[:10])
        if day in days:
            raise ValueError(f"Naver 중복 거래일 ({day})")
        close, delta = _positive(row.get("closePrice")), _signed(row.get("compareToPreviousClosePrice"))
        base = close - delta if close is not None and delta is not None else None
        volume = _signed(row.get("accumulatedTradingVolume"))
        days[day] = NaverDay(close, base if base and base > 0 else None, volume, _signed(row.get("fluctuationsRatio")))
    return days


def limit_note(change_pct: float) -> str | None:
    """KOSPI's +-30% daily limit does not apply to 정리매매, new listings or
    re-listings. Such moves are kept (recall first) but flagged for review."""
    if abs(change_pct) > PRICE_LIMIT_PCT:
        return f"가격제한폭(±30%) 초과 {change_pct:+.2f}%: 정리매매·신규/재상장·기준가 변경 여부 확인 필요"
    return None


def _krx_closes(client, day: date) -> dict[str, float]:
    closes = {}
    for row in client.rows("stk_bydd_trd", day):
        raw = str(row.get("ISU_CD") or row.get("ISU_SRT_CD") or "")
        code = raw[3:9] if len(raw) == 12 else raw
        value = _positive(row.get("TDD_CLSPRC"))
        if code and value is not None:
            closes[code] = value
    return closes


def reconcile_kr(code: str, primary: KrPair, naver: dict[date, NaverDay] | None, naver_error: str | None,
                 previous_date: date, session_date: date, threshold_pct: float) -> tuple[dict | None, str | None]:
    """Combine Yahoo(KRX close) with Naver's KRX base price into one row."""
    today = (naver or {}).get(session_date)
    base = today.krx_base_price if today else None
    notes: list[str] = []
    # A stock with no trades at all keeps its KRX base price as the close.
    if today and today.volume == 0 and base and today.integrated_close == base:
        return {"previous_close": base, "close": base, "change_pct": 0.0, "volume": 0.0,
                "source": "Naver 기준가(무거래)", "fallback": False, "mismatch": False, "cross_checked": True,
                "validation": "무거래: 종가=기준가", "note": "거래 없음(거래정지 가능)"}, None
    if primary.ok:
        change = calculate_change_pct(primary.previous_close, primary.close)
        row = {"previous_close": primary.previous_close, "close": primary.close, "change_pct": change,
               "volume": primary.volume, "source": "Yahoo(KRX 정규장)", "fallback": False, "mismatch": False,
               "cross_checked": False, "validation": "교차검증 불가"}
        if base is None:
            notes.append(f"Naver KRX 기준가 확인 불가: {naver_error or '분석일 행 없음'}")
        elif base == primary.previous_close:
            row.update(cross_checked=True, validation="Naver KRX 기준가 일치")
        else:
            other = calculate_change_pct(base, primary.close)
            row.update(mismatch=True, cross_checked=True, alt_change_pct=other,
                       validation=f"전일종가 불일치: Naver KRX 기준가 {base:,.0f} ({other:+.2f}%)")
            notes.append(f"Yahoo 전일종가 {primary.previous_close:,.0f} ≠ KRX 기준가 {base:,.0f}: "
                         "권리락·배당락 등 기준가 조정 또는 데이터 오류 가능")
            yahoo_implausible = abs(change) > PRICE_LIMIT_PCT >= abs(other)
            if yahoo_implausible or (is_drop(other, threshold_pct) and not is_drop(change, threshold_pct)):
                row.update(previous_close=base, change_pct=other, source="Yahoo 종가 + Naver KRX 기준가")
        if today and today.integrated_close and abs(today.integrated_close / primary.close - 1) > 0.05:
            notes.append(f"Naver 통합(NXT 포함) 최종가 {today.integrated_close:,.0f}와 5% 이상 차이")
    elif base and today.integrated_close:
        # Fallback: KRX base price (exact previous regular close) + Naver's
        # integrated last price, which can include NXT after-market trades.
        row = {"previous_close": base, "close": today.integrated_close,
               "change_pct": calculate_change_pct(base, today.integrated_close), "volume": today.volume,
               "source": "Naver fallback(통합가 근사)", "fallback": True, "mismatch": False,
               "cross_checked": False, "validation": "Yahoo(KRX) 실패 → Naver fallback"}
        notes.extend([primary.error, "분석일 종가는 NXT 시간외 포함 가능 근사치"])
    else:
        return None, f"{primary.error} | Naver: {naver_error or ('분석일 행 없음' if today is None else 'KRX 기준가/종가 없음')}"
    note = limit_note(row["change_pct"])
    row["limit_exceeded"] = note is not None
    if note:
        notes.append(note)
    row["note"] = "; ".join(notes)
    return row, None


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
    codes = constituents["code"].tolist()
    names = dict(zip(constituents["code"], constituents["company_name"]))
    warnings: list[str] = []
    # Older manual sessions need more Naver history rows.
    page_size = 10 if (date.today() - session_date).days <= 7 else 60

    def pool_map(function, symbols):
        with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
            return dict(zip(symbols, executor.map(function, symbols)))

    def naver_safe(code):
        try:
            return fetch_naver_days(code, settings, page_size), None
        except Exception as exc:
            return None, f"Naver daily 조회 실패: {exc}"

    started = time.monotonic()
    def fetch_yahoo(symbols):
        return pool_map(lambda code: fetch_yahoo_kr_pair(code, previous_date, session_date, settings), symbols)

    primary = retry_until_available(fetch_yahoo(codes), fetch_yahoo, lambda pair: pair.ok, settings, "[KR] Yahoo(KRX)")
    stage = time.monotonic()
    naver = pool_map(naver_safe, codes)
    LOGGER.info("[KR] Yahoo(KRX) %.1fs 유효 %d / %d, Naver 기준가 %.1fs 성공 %d", stage - started,
                sum(pair.ok for pair in primary.values()), len(codes), time.monotonic() - stage,
                sum(result[0] is not None for result in naver.values()))
    if not any(result[0] is not None for result in naver.values()):
        warnings.append("Naver KRX 기준가 교차검증 전체 불가")

    krx_previous = krx_current = None
    if krx_client is not None:
        try:
            krx_previous, krx_current = _krx_closes(krx_client, previous_date), _krx_closes(krx_client, session_date)
            LOGGER.info("[KR] KRX 공식 종가 교차검증 사용: %d / %d 종목", len(krx_current), len(codes))
        except Exception as exc:
            warnings.append(f"KRX 공식 교차검증 불가: {exc}")
            LOGGER.warning("[KR] KRX 공식 교차검증 불가: %s", exc)

    records, missing = [], {}
    for code in codes:
        days, naver_error = naver[code]
        row, reason = reconcile_kr(code, primary[code], days, naver_error, previous_date, session_date, threshold_pct)
        if row is None:
            missing[code] = reason
            continue
        if krx_current is not None:
            official = (krx_previous.get(code), krx_current.get(code))
            if None not in official:
                if official == (row["previous_close"], row["close"]):
                    row["validation"] = (row["validation"] + " · KRX 공식 일치").strip(" ·")
                else:
                    other = calculate_change_pct(*official)
                    row["mismatch"] = True
                    row["alt_change_pct"] = other
                    row["note"] = "; ".join(filter(None, [row["note"], f"KRX 공식 {official[0]:,.0f}→{official[1]:,.0f} ({other:+.2f}%)"]))
                    if is_drop(other, threshold_pct) and not is_drop(row["change_pct"], threshold_pct):
                        row.update(previous_close=official[0], close=official[1], change_pct=other, source="KRX 공식")
        if validator is not None and is_drop(row["change_pct"], threshold_pct):
            try:
                validator.validate(code, previous_date, session_date, row["previous_close"], row["close"])
                row["validation"] = (row["validation"] + " · KIS 일치").strip(" ·")
            except Exception as exc:
                row["mismatch"] = True
                row["note"] = "; ".join(filter(None, [row["note"], f"KIS 검증: {exc}"]))
        records.append({"code": code, "company_name": names[code], "session_date": session_date,
                        "previous_session_date": previous_date, **row})

    prices = pd.DataFrame(records)
    if not prices.empty:
        prices["detected"] = prices["change_pct"].map(lambda value: is_drop(value, threshold_pct))
    collection = KrCollection(
        prices=prices, missing=missing, warnings=warnings,
        fallback_count=int(prices["fallback"].sum()) if not prices.empty else 0,
        mismatch_count=int(prices["mismatch"].sum()) if not prices.empty else 0,
        cross_checked_count=int(prices["cross_checked"].sum()) if not prices.empty else 0,
    )
    if not prices.empty:
        band = prices["change_pct"] <= settings.kr_validation_band_pct
        unverified = prices.loc[band & ~prices["cross_checked"] & ~prices["fallback"]]
        if len(unverified):
            collection.warnings.append(f"검증 구간({settings.kr_validation_band_pct:.0f}%) 이하 {len(unverified)}개 2차 검증 불가: "
                                       + ", ".join(unverified["code"]))
        beyond = prices.loc[prices["detected"] & prices["limit_exceeded"]]
        if len(beyond):
            collection.warnings.append(f"가격제한폭 초과 급락 {len(beyond)}개 확인 필요: " + ", ".join(beyond["code"]))
    return collection
