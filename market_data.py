from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

from config import SETTINGS, Settings
from detector import calculate_change_pct, is_drop
from validator import validate_price_row

LOGGER = logging.getLogger(__name__)
# Yahoo serves the same spark resource from two hosts. GitHub-hosted runner IPs
# can throttle either host independently, so try both before the reader proxy.
SPARK_URLS = (
    ("query1", "https://query1.finance.yahoo.com/v7/finance/spark"),
    ("query2", "https://query2.finance.yahoo.com/v7/finance/spark"),
)
SPARK_PROXY_URL = "https://r.jina.ai/http://query2.finance.yahoo.com/v7/finance/spark"
CHART_URLS = (
    ("query1", "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"),
    ("query2", "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"),
)
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
SPARK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
CHART_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}
NASDAQ_HEADERS = {
    **SPARK_HEADERS,
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
}


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _nasdaq_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def _parse_nasdaq_number(value: object) -> float:
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if text.upper() == "UNCH":
        return 0.0
    if not text or text == "--":
        raise ValueError("Nasdaq 가격 값 누락")
    return float(text)


def _download_nasdaq_snapshot(
    constituents: pd.DataFrame,
    session_date: date,
    settings: Settings,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Download the latest completed regular-session snapshot in one request."""
    response = requests.get(
        NASDAQ_SCREENER_URL,
        params={"tableonly": "true", "limit": "10000"},
        headers=NASDAQ_HEADERS,
        timeout=settings.download_timeout_seconds,
    )
    response.raise_for_status()
    rows = (((response.json().get("data") or {}).get("table") or {}).get("rows") or [])
    if not rows:
        raise ValueError("Nasdaq screener 응답에 종목 데이터가 없습니다")

    by_symbol = {_nasdaq_symbol(row.get("symbol", "")): row for row in rows}
    records: list[dict] = []
    missing: dict[str, str] = {}
    for _, constituent in constituents.iterrows():
        yahoo_ticker = constituent["yahoo_ticker"]
        row = by_symbol.get(_nasdaq_symbol(yahoo_ticker))
        if row is None:
            missing[yahoo_ticker] = "Nasdaq screener 종목 누락"
            continue
        try:
            close = _parse_nasdaq_number(row.get("lastsale"))
            net_change = _parse_nasdaq_number(row.get("netchange"))
            previous_close = close - net_change
            change_pct = calculate_change_pct(previous_close, close)
        except (TypeError, ValueError) as exc:
            missing[yahoo_ticker] = str(exc)
            continue
        records.append(
            {
                "yahoo_ticker": yahoo_ticker,
                "session_date": session_date,
                "previous_close": previous_close,
                "close": close,
                "change_pct": change_pct,
            }
        )

    prices = pd.DataFrame(records)
    if prices.empty:
        return prices, missing
    prices = prices.merge(constituents, on="yahoo_ticker", how="left", validate="one_to_one")
    return prices.sort_values("ticker").reset_index(drop=True), missing


def _download_batch(
    tickers: list[str],
    start: date,
    end: date,
    settings: Settings,
    *,
    threads: bool | int = True,
) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, settings.download_retries + 1):
        try:
            data = yf.download(
                tickers=tickers,
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                auto_adjust=True,
                actions=False,
                group_by="column",
                threads=threads,
                timeout=settings.download_timeout_seconds,
                progress=False,
            )
            if data.empty:
                raise ValueError("빈 가격 데이터")
            return data
        except Exception as exc:
            last_error = exc
            LOGGER.warning("가격 batch 재시도 %d/%d: %s", attempt, settings.download_retries, exc)
            if attempt < settings.download_retries:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"가격 batch 다운로드 실패: {last_error}")


def _parse_spark_response(payload: dict) -> dict[str, dict[date, float]]:
    parsed: dict[str, dict[date, float]] = {}
    results = payload.get("spark", {}).get("result") or []
    for item in results:
        ticker = str(item.get("symbol", "")).upper()
        responses = item.get("response") or []
        if not ticker or not responses:
            continue
        response = responses[0]
        timestamps = response.get("timestamp") or []
        quote = ((response.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or response.get("close") or []
        values: dict[date, float] = {}
        for timestamp, close in zip(timestamps, closes):
            if close is None:
                continue
            session_day = (
                pd.Timestamp(timestamp, unit="s", tz="UTC")
                .tz_convert("America/New_York")
                .date()
            )
            values[session_day] = float(close)
        if values:
            parsed[ticker] = values
    return parsed


def _parse_chart_response(payload: dict, ticker: str) -> dict[date, float]:
    """Return regular-session raw closes keyed by the exchange session date."""
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise ValueError(f"Yahoo chart 응답에 {ticker} 데이터가 없습니다: {chart.get('error')}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    timezone_name = (result.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York"
    exchange_timezone = ZoneInfo(timezone_name)
    values: dict[date, float] = {}
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        session_day = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(exchange_timezone).date()
        values[session_day] = float(close)
    if not values:
        raise ValueError(f"Yahoo chart 응답에 {ticker} 종가가 없습니다")
    return values


def _download_chart_ticker(
    ticker: str,
    previous_session_date: date,
    session_date: date,
    settings: Settings,
) -> dict[date, float]:
    period_start = previous_session_date - timedelta(days=1)
    period_end = session_date + timedelta(days=2)
    params = {
        "period1": int(datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc).timestamp()),
        "period2": int(datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc).timestamp()),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
        "includePrePost": "false",
    }
    last_error: Exception | None = None
    for attempt in range(1, settings.download_retries + 1):
        for source, template in CHART_URLS:
            try:
                response = requests.get(
                    template.format(ticker=ticker),
                    params=params,
                    headers=CHART_HEADERS,
                    timeout=settings.download_timeout_seconds,
                )
                response.raise_for_status()
                values = _parse_chart_response(response.json(), ticker)
                LOGGER.debug("Yahoo chart %s 사용: %s", source, ticker)
                return values
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                LOGGER.warning(
                    "Yahoo chart %s 실패: %s (시도 %d/%d, status=%s)",
                    source,
                    ticker,
                    attempt,
                    settings.download_retries,
                    status or "n/a",
                )
        if attempt < settings.download_retries:
            time.sleep(1)
    raise RuntimeError(f"Yahoo chart {ticker} 다운로드 실패: {last_error}")


def _decode_spark_payload(response: requests.Response) -> dict:
    """Decode direct Yahoo JSON or the same JSON wrapped by Jina Reader."""
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        start = response.text.find('{"spark"')
        if start < 0:
            raise ValueError("Yahoo spark JSON을 응답에서 찾지 못했습니다")
        payload, _ = json.JSONDecoder().raw_decode(response.text[start:])
        return payload


def _download_close_batch(
    tickers: list[str],
    previous_session_date: date,
    session_date: date,
    settings: Settings,
) -> dict[str, dict[date, float]]:
    results: dict[str, dict[date, float]] = {}
    failures: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as executor:
        futures = {
            executor.submit(
                _download_chart_ticker,
                ticker,
                previous_session_date,
                session_date,
                settings,
            ): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except Exception as exc:
                failures[ticker] = exc
    if not results:
        sample = next(iter(failures.values()), "no response")
        raise RuntimeError(f"Yahoo chart batch 다운로드 실패: {sample}")
    return results


def _ticker_frame(data: pd.DataFrame, ticker: str, ticker_count: int) -> pd.DataFrame:
    del ticker_count  # Kept for compatibility with tests/callers.
    if not isinstance(data.columns, pd.MultiIndex):
        return data.copy()
    for level in range(data.columns.nlevels):
        if ticker in data.columns.get_level_values(level):
            return data.xs(ticker, axis=1, level=level, drop_level=True).copy()
    return pd.DataFrame()


def _parse_ticker(
    ticker: str,
    frame: pd.DataFrame,
    session_date: date,
    previous_session_date: date,
) -> tuple[dict | None, str | None]:
    if frame.empty:
        return None, "ticker 데이터 없음"
    frame.index = pd.to_datetime(frame.index).date
    if session_date not in frame.index or previous_session_date not in frame.index:
        return None, f"필수 거래일 누락 ({previous_session_date}, {session_date})"
    target = frame.loc[session_date]
    previous = frame.loc[previous_session_date]
    if isinstance(target, pd.DataFrame) or isinstance(previous, pd.DataFrame):
        return None, "중복 거래일 데이터"

    # Compare today's volume with the 20 completed sessions before today.
    history = frame.loc[[index for index in frame.index if index < session_date]].tail(20)
    volume_history = history["Volume"].dropna()
    average_volume = float(volume_history.mean())
    row = {
        "yahoo_ticker": ticker,
        "session_date": session_date,
        "previous_close": float(previous["Close"]),
        "close": float(target["Close"]),
        "open": float(target["Open"]),
        "low": float(target["Low"]),
        "volume": float(target["Volume"]),
        "average_volume_20d": average_volume,
    }
    errors = validate_price_row(row, session_date)
    if len(volume_history) < 20:
        errors.append(f"20일 거래량 표본 부족 ({len(volume_history)}/20)")
    if not pd.notna(average_volume) or average_volume <= 0:
        errors.append("20일 평균 거래량 누락/비정상")
    if errors:
        return None, ", ".join(errors)

    row["change_pct"] = calculate_change_pct(row["previous_close"], row["close"])
    row["volume_change_pct"] = (row["volume"] / average_volume - 1.0) * 100.0
    row["open_to_close_pct"] = (row["close"] / row["open"] - 1.0) * 100.0
    return row, None


def download_market_data(
    constituents: pd.DataFrame,
    session_date: date,
    previous_session_date: date,
    settings: Settings = SETTINGS,
) -> tuple[pd.DataFrame, dict[str, str]]:
    # Nasdaq's screener exposes an undated last-sale snapshot. It can reset or
    # include extended-hours activity before the next regular session, so it
    # cannot prove which two historical sessions are being compared. Always
    # use dated daily bars for drop detection.
    tickers = constituents["yahoo_ticker"].tolist()
    missing: dict[str, str] = {}
    records: list[dict] = []

    settings.yfinance_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(settings.yfinance_cache_dir))
    # Fetch dated regular-session closes concurrently in small batches. Full
    # OHLCV is fetched only for candidates during secondary verification.
    batches = _chunks(tickers, settings.batch_size)
    for batch_index, batch in enumerate(batches, start=1):
        try:
            close_data = _download_close_batch(
                batch,
                previous_session_date,
                session_date,
                settings,
            )
        except Exception as exc:
            for ticker in batch:
                missing[ticker] = str(exc)
        else:
            for ticker in batch:
                values = close_data.get(ticker, {})
                previous_close = values.get(previous_session_date)
                close = values.get(session_date)
                if previous_close is None or close is None:
                    missing[ticker] = (
                        f"Yahoo chart 필수 거래일 누락 ({previous_session_date}, {session_date})"
                    )
                    continue
                try:
                    change_pct = calculate_change_pct(float(previous_close), float(close))
                except ValueError as exc:
                    missing[ticker] = str(exc)
                    continue
                records.append(
                    {
                        "yahoo_ticker": ticker,
                        "session_date": session_date,
                        "previous_close": float(previous_close),
                        "close": float(close),
                        "change_pct": change_pct,
                    }
                )
        if batch_index < len(batches) and settings.batch_pause_seconds > 0:
            LOGGER.info(
                "Yahoo 요청 속도 제한 대기: 초기 %d/%d batch 후 %.0f초",
                batch_index,
                len(batches),
                settings.batch_pause_seconds,
            )
            time.sleep(settings.batch_pause_seconds)

    prices = pd.DataFrame(records)
    if prices.empty:
        return prices, missing
    prices = prices.merge(constituents, on="yahoo_ticker", how="left", validate="one_to_one")
    return prices.sort_values("ticker").reset_index(drop=True), missing


def verify_candidates(
    candidates: pd.DataFrame,
    session_date: date,
    previous_session_date: date,
    settings: Settings = SETTINGS,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Re-download only candidates and require agreement within 0.15 percentage point."""
    verified: list[pd.Series] = []
    rejected: dict[str, str] = {}
    for _, candidate in candidates.iterrows():
        ticker = candidate["yahoo_ticker"]
        try:
            data = _download_batch(
                [ticker], previous_session_date - timedelta(days=60), session_date + timedelta(days=2), settings
            )
            row, error = _parse_ticker(ticker, _ticker_frame(data, ticker, 1), session_date, previous_session_date)
            if error or row is None:
                rejected[ticker] = error or "2차 검증 데이터 없음"
                continue
            if abs(row["change_pct"] - float(candidate["change_pct"])) > 0.15:
                rejected[ticker] = "1차/2차 등락률 불일치"
                continue
            if not is_drop(row["change_pct"], settings.drop_threshold_pct):
                rejected[ticker] = "2차 검증에서 임계치 미충족"
                continue
            enriched = candidate.copy()
            for field, value in row.items():
                enriched[field] = value
            verified.append(enriched)
        except Exception as exc:
            rejected[ticker] = f"2차 검증 실패: {exc}"
    return (pd.DataFrame(verified), rejected)
