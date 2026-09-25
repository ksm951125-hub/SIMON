from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

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
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
SPARK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
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


def _download_close_batch(tickers: list[str], settings: Settings) -> dict[str, dict[date, float]]:
    last_error: Exception | None = None
    params = {
        "symbols": ",".join(tickers),
        "range": "3mo",
        "interval": "1d",
        "indicators": "close",
        "includeTimestamps": "true",
        "includePrePost": "false",
        "corsDomain": "finance.yahoo.com",
        ".tsrc": "finance",
        # Keep the unauthenticated reader fallback from serving yesterday's
        # cached response while remaining harmless to Yahoo itself.
        "cacheBust": datetime.now(timezone.utc).strftime("%Y%m%d%H"),
    }
    for attempt in range(1, settings.download_retries + 1):
        sources = (*SPARK_URLS, ("reader fallback", SPARK_PROXY_URL))
        for source, url in sources:
            try:
                request_url = url
                request_params = params
                if source == "reader fallback":
                    # Reader treats its own query string separately from the
                    # nested target URL. Encode target separators so the full
                    # Yahoo query reaches the upstream endpoint intact.
                    request_url = f"{url}?{urlencode(params).replace('&', '%26')}"
                    request_params = None
                response = requests.get(
                    request_url,
                    params=request_params,
                    headers=SPARK_HEADERS,
                    timeout=settings.download_timeout_seconds,
                )
                response.raise_for_status()
                parsed = _parse_spark_response(_decode_spark_payload(response))
                if not parsed:
                    raise ValueError("Yahoo spark 응답에 가격 데이터가 없습니다")
                if source == "reader fallback":
                    LOGGER.info("Yahoo reader fallback 사용: %d개", len(parsed))
                else:
                    LOGGER.info("Yahoo %s spark 사용: %d개", source, len(parsed))
                return parsed
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                LOGGER.warning(
                    "Yahoo spark %s 실패 (시도 %d/%d, status=%s): %s",
                    source,
                    attempt,
                    settings.download_retries,
                    status or "n/a",
                    type(exc).__name__,
                )
        if attempt < settings.download_retries:
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Yahoo spark batch 다운로드 실패: {last_error}")


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
    try:
        nasdaq_prices, nasdaq_missing = _download_nasdaq_snapshot(
            constituents, session_date, settings
        )
        nasdaq_coverage = len(nasdaq_prices) / len(constituents)
        if nasdaq_coverage >= settings.minimum_coverage_ratio:
            LOGGER.info(
                "Nasdaq screener 시세 수집: %d/%d (%.1f%%)",
                len(nasdaq_prices),
                len(constituents),
                nasdaq_coverage * 100,
            )
            return nasdaq_prices, nasdaq_missing
        LOGGER.warning(
            "Nasdaq screener 커버리지 부족: %d/%d; Yahoo fallback 사용",
            len(nasdaq_prices),
            len(constituents),
        )
    except Exception as exc:
        LOGGER.warning("Nasdaq screener 시세 수집 실패; Yahoo fallback 사용: %s", exc)

    tickers = constituents["yahoo_ticker"].tolist()
    missing: dict[str, str] = {}
    records: list[dict] = []

    settings.yfinance_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(settings.yfinance_cache_dir))
    # Use Yahoo's multi-symbol close endpoint for universe-wide drop detection.
    # Fetching full chart history for all 503 tickers triggers throttling on
    # shared GitHub-hosted runner IPs. Full OHLCV is fetched only for candidates.
    batches = _chunks(tickers, settings.batch_size)
    for batch_index, batch in enumerate(batches, start=1):
        try:
            close_data = _download_close_batch(batch, settings)
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
                        f"Yahoo spark 필수 거래일 누락 ({previous_session_date}, {session_date})"
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

    # A batch can be non-empty while Yahoo silently returns NaN rows for many
    # symbols. This happened in production with 443/503 symbols: because the
    # response itself was non-empty, the ordinary download retry never ran.
    # Retry every parse failure in smaller, single-threaded batches. Limiting
    # retries to only a handful of missing symbols makes a transient provider
    # problem indistinguishable from a permanent outage.
    retry_tickers = list(missing)
    if retry_tickers:
        retry_batch_size = settings.batch_size
        if settings.retry_cooldown_seconds > 0:
            LOGGER.info(
                "Yahoo 재조회 전 속도 제한 cooldown: %.0f초",
                settings.retry_cooldown_seconds,
            )
            time.sleep(settings.retry_cooldown_seconds)
        LOGGER.info(
            "누락 ticker %d개를 %d개씩 단일 스레드로 재조회합니다",
            len(retry_tickers),
            retry_batch_size,
        )
        retry_batches = _chunks(retry_tickers, retry_batch_size)
        for batch_index, retry_batch in enumerate(retry_batches, start=1):
            try:
                retry_data = _download_close_batch(retry_batch, settings)
            except Exception as exc:
                LOGGER.warning("누락 ticker 재조회 실패 (%d개): %s", len(retry_batch), exc)
                continue
            for ticker in retry_batch:
                values = retry_data.get(ticker, {})
                previous_close = values.get(previous_session_date)
                close = values.get(session_date)
                if previous_close is None or close is None:
                    continue
                try:
                    change_pct = calculate_change_pct(float(previous_close), float(close))
                except ValueError:
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
                missing.pop(ticker, None)
            if batch_index < len(retry_batches) and settings.batch_pause_seconds > 0:
                LOGGER.info(
                    "Yahoo 요청 속도 제한 대기: 재조회 %d/%d batch 후 %.0f초",
                    batch_index,
                    len(retry_batches),
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
