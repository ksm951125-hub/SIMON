from __future__ import annotations

import logging
import ast
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from market_calendar import KST

import pandas as pd
import requests

from detector import calculate_change_pct

LOGGER = logging.getLogger(__name__)

NAVER_MARKET_URL = "https://m.stock.naver.com/api/stocks/marketValue/KOSPI"
NAVER_PRICE_URL = "https://m.stock.naver.com/api/stock/{code}/price"
NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://m.stock.naver.com/",
}
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 2
LIST_PAGE_SIZE = 100
PRICE_PAGE_SIZE = 60


@dataclass(frozen=True)
class KospiSessionContext:
    session_date: date
    previous_session_date: date
    price_page: int
    warning: str | None = None


def _request_json(url: str, params: dict) -> object:
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=NAVER_HEADERS,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(1)
    raise RuntimeError(f"Naver Finance request failed: {type(last_error).__name__}: {last_error}")


def load_kospi_constituents() -> pd.DataFrame:
    first = _request_json(NAVER_MARKET_URL, {"page": 1, "pageSize": LIST_PAGE_SIZE})
    if not isinstance(first, dict):
        raise ValueError("KOSPI listing response is not an object")
    total_count = int(first.get("totalCount") or 0)
    if total_count <= 0:
        raise ValueError("KOSPI listing totalCount is empty")
    pages = math.ceil(total_count / LIST_PAGE_SIZE)
    stocks = list(first.get("stocks") or [])
    for page in range(2, pages + 1):
        payload = _request_json(
            NAVER_MARKET_URL,
            {"page": page, "pageSize": LIST_PAGE_SIZE},
        )
        if not isinstance(payload, dict):
            raise ValueError(f"KOSPI listing page {page} is not an object")
        if int(payload.get("totalCount") or 0) != total_count:
            raise ValueError("KOSPI listing changed during pagination")
        stocks.extend(payload.get("stocks") or [])

    if len(stocks) != total_count:
        raise ValueError(f"KOSPI listing incomplete: {len(stocks)}/{total_count}")
    codes = [str(item.get("itemCode") or "") for item in stocks]
    if len(set(codes)) != len(codes):
        raise ValueError("KOSPI listing contains duplicate codes")
    records = []
    for item in stocks:
        exchange = item.get("stockExchangeType") or {}
        if item.get("stockEndType") != "stock":
            continue
        if exchange.get("nameEng") != "KOSPI":
            continue
        code = str(item.get("itemCode") or "").strip()
        company_name = str(item.get("stockName") or "").strip()
        if not code or not company_name:
            raise ValueError("KOSPI stock code/name is missing")
        records.append({"code": code, "company_name": company_name})
    if not records:
        raise ValueError("KOSPI stock listing is empty after product filtering")
    frame = pd.DataFrame(records).sort_values("code").reset_index(drop=True)
    return frame


def _price_rows(code: str, page: int, page_size: int = PRICE_PAGE_SIZE) -> list[dict]:
    payload = _request_json(
        NAVER_PRICE_URL.format(code=code),
        {"page": page, "pageSize": page_size},
    )
    if not isinstance(payload, list):
        raise ValueError(f"{code} daily price response is not a list")
    return payload


def _index_rows(page: int, page_size: int = PRICE_PAGE_SIZE) -> list[dict]:
    payload = _request_json("https://m.stock.naver.com/api/index/KOSPI/price", {"page": page, "pageSize": page_size})
    if not isinstance(payload, list):
        raise ValueError("KOSPI index history is not a list")
    return payload


def _parse_close(value: object) -> float:
    text = str(value or "").replace(",", "").strip()
    if not text:
        raise ValueError("closePrice is empty")
    close = float(text)
    if not math.isfinite(close) or close <= 0:
        raise ValueError("closePrice must be positive")
    return close


def get_kospi_session_context(session_date: date | None = None, now: datetime | None = None) -> KospiSessionContext:
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(KST)
    cutoff = local_now.date() if local_now.hour >= 18 else local_now.date() - timedelta(days=1)
    if session_date is not None and session_date > cutoff:
        raise ValueError("KOSPI session is not completed/settled")
    if session_date is None:
        rows = _index_rows(1, 10)
        raw_dates = [date.fromisoformat(row["localTradedAt"]) for row in rows]
        if len(raw_dates) != len(set(raw_dates)):
            raise ValueError("duplicate KOSPI index sessions")
        dates = sorted((day for day in raw_dates if day <= cutoff), reverse=True)
        if len(dates) < 2:
            raise ValueError("KOSPI reference history has fewer than two sessions")
        import pandas_market_calendars as mcal
        schedule = mcal.get_calendar("XKRX").schedule(start_date=cutoff - timedelta(days=45), end_date=cutoff)
        expected = list(schedule.index.date)[-2:]
        warning = None
        if expected != [dates[1], dates[0]]:
            warning = f"KRX 캘린더와 지수 데이터 거래일 불일치: 공급 지연/임시 휴장 확인 필요 ({dates[1]}, {dates[0]})"
        return KospiSessionContext(dates[0], dates[1], 1, warning)

    history: dict[date, int] = {}
    for page in range(1, 21):
        rows = _index_rows(page)
        if not rows:
            break
        for row in rows:
            history[date.fromisoformat(row["localTradedAt"])] = page
        older_dates = sorted((item for item in history if item < session_date), reverse=True)
        if session_date in history and older_dates:
            return KospiSessionContext(session_date, older_dates[0], history[session_date])
        if min(history) < session_date and session_date not in history:
            raise ValueError(f"{session_date} is not a KOSPI trading session")
    raise ValueError(f"KOSPI session {session_date} was not found in available history")


def _chart_price_pair(code: str, previous: date, session: date) -> tuple[float, float]:
    """Cross-check price history; never use the current quote or percent field."""
    response = requests.get(
        "https://api.finance.naver.com/siseJson.naver",
        params={"symbol": code, "requestType": 1, "startTime": previous.strftime("%Y%m%d"),
                "endTime": session.strftime("%Y%m%d"), "timeframe": "day"},
        headers=NAVER_HEADERS, timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    rows = ast.literal_eval(response.text.strip())
    values = {}
    for row in rows[1:]:
        day = datetime.strptime(str(row[0]), "%Y%m%d").date()
        if day in values:
            raise ValueError("duplicate Naver chart session")
        values[day] = _parse_close(row[4])
    if previous not in values or session not in values:
        raise ValueError("cross-check chart required sessions missing")
    return values[previous], values[session]


def _download_price_pair(
    code: str,
    session_date: date,
    previous_session_date: date,
    price_page: int,
) -> tuple[float, float]:
    rows = _price_rows(code, price_page)
    dates_present = {date.fromisoformat(row["localTradedAt"]) for row in rows}
    if previous_session_date not in dates_present and session_date in dates_present and rows:
        if min(dates_present) > previous_session_date:
            rows += _price_rows(code, price_page + 1)
    values = {}
    target_row = None
    for row in rows:
        day = date.fromisoformat(row["localTradedAt"])
        if day not in {previous_session_date, session_date}:
            continue
        if day in values:
            raise ValueError("duplicate daily price row")
        if row.get("accumulatedTradingVolume") == 0:
            raise ValueError("거래량 0: 거래정지/미확정 데이터 확인 필요")
        values[day] = _parse_close(row.get("closePrice"))
        if day == session_date:
            target_row = row
    previous_close = values.get(previous_session_date)
    close = values.get(session_date)
    if previous_close is None or close is None:
        raise ValueError(f"required sessions missing ({previous_session_date}, {session_date})")
    change = calculate_change_pct(previous_close, close)
    # Ordinary KOSPI stocks have a 30% daily limit. Larger close-to-close
    # changes may indicate re-listing, splits or a changed reference price.
    if abs(change) > 30.01:
        raise ValueError("Corporate action/reference-price anomaly: 수동 확인 필요")
    if target_row and target_row.get("fluctuationsRatio") is not None:
        quoted = float(str(target_row["fluctuationsRatio"]).replace(",", ""))
        if not math.isfinite(quoted) or abs(quoted - change) > 0.1:
            # Naver's percent/reference fields can disagree with its historical
            # closes. Accept only when a separate dated chart confirms BOTH closes.
            cross_previous, cross_close = _chart_price_pair(code, previous_session_date, session_date)
            if abs(cross_previous - previous_close) > 0.01 or abs(cross_close - close) > 0.01:
                raise ValueError("종가/차트/등락률 불일치: 권리락/데이터 조정 확인 필요")
    return previous_close, close


def download_kospi_market_data(
    constituents: pd.DataFrame,
    context: KospiSessionContext,
) -> tuple[pd.DataFrame, dict[str, str]]:
    records: list[dict] = []
    missing: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(
                _download_price_pair,
                row["code"],
                context.session_date,
                context.previous_session_date,
                context.price_page,
            ): row
            for _, row in constituents.iterrows()
        }
        for future in as_completed(futures):
            row = futures[future]
            code = row["code"]
            try:
                previous_close, close = future.result()
                records.append(
                    {
                        "code": code,
                        "company_name": row["company_name"],
                        "session_date": context.session_date,
                        "previous_close": previous_close,
                        "close": close,
                        "change_pct": calculate_change_pct(previous_close, close),
                    }
                )
            except Exception as exc:
                missing[code] = str(exc)
    prices = pd.DataFrame(records)
    if prices.empty:
        return prices, missing
    return prices.sort_values("code").reset_index(drop=True), missing
