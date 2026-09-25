from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date

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
        stocks.extend(payload.get("stocks") or [])

    records = []
    for item in stocks:
        exchange = item.get("stockExchangeType") or {}
        if item.get("stockEndType") != "stock":
            continue
        if exchange.get("nameEng") != "KOSPI":
            continue
        code = str(item.get("itemCode") or "").strip()
        company_name = str(item.get("stockName") or "").strip()
        if code and company_name:
            records.append({"code": code, "company_name": company_name})
    frame = pd.DataFrame(records).drop_duplicates("code").sort_values("code").reset_index(drop=True)
    if frame.empty:
        raise ValueError("KOSPI stock listing is empty after product filtering")
    return frame


def _price_rows(code: str, page: int, page_size: int = PRICE_PAGE_SIZE) -> list[dict]:
    payload = _request_json(
        NAVER_PRICE_URL.format(code=code),
        {"page": page, "pageSize": page_size},
    )
    if not isinstance(payload, list):
        raise ValueError(f"{code} daily price response is not a list")
    return payload


def _parse_close(value: object) -> float:
    text = str(value or "").replace(",", "").strip()
    if not text:
        raise ValueError("closePrice is empty")
    close = float(text)
    if close <= 0:
        raise ValueError("closePrice must be positive")
    return close


def get_kospi_session_context(session_date: date | None = None) -> KospiSessionContext:
    if session_date is None:
        rows = _price_rows("005930", 1, 10)
        dates = [date.fromisoformat(row["localTradedAt"]) for row in rows]
        if len(dates) < 2:
            raise ValueError("KOSPI reference history has fewer than two sessions")
        return KospiSessionContext(dates[0], dates[1], 1)

    history: dict[date, int] = {}
    for page in range(1, 21):
        rows = _price_rows("005930", page)
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


def _download_price_pair(
    code: str,
    session_date: date,
    previous_session_date: date,
    price_page: int,
) -> tuple[float, float]:
    rows = _price_rows(code, price_page)
    values = {
        date.fromisoformat(row["localTradedAt"]): _parse_close(row.get("closePrice"))
        for row in rows
    }
    previous_close = values.get(previous_session_date)
    close = values.get(session_date)
    if previous_close is None or close is None:
        raise ValueError(f"required sessions missing ({previous_session_date}, {session_date})")
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
