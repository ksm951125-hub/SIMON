from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from config import SETTINGS, Settings
from detector import calculate_change_pct, is_drop
from validator import validate_price_row

LOGGER = logging.getLogger(__name__)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


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
    tickers = constituents["yahoo_ticker"].tolist()
    # Leave ample room for 20 completed sessions even around holiday-heavy
    # periods and occasional missing Yahoo rows.
    start = previous_session_date - timedelta(days=60)
    end = session_date + timedelta(days=2)
    missing: dict[str, str] = {}
    records: list[dict] = []

    settings.yfinance_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(settings.yfinance_cache_dir))
    # yfinance itself parallelizes each batch. Its downloader uses shared process
    # state, so multiple simultaneous yf.download calls can contaminate results.
    batches = _chunks(tickers, settings.batch_size)
    for batch_index, batch in enumerate(batches, start=1):
        try:
            data = _download_batch(batch, start, end, settings, threads=False)
        except Exception as exc:
            for ticker in batch:
                missing[ticker] = str(exc)
        else:
            for ticker in batch:
                row, error = _parse_ticker(
                    ticker,
                    _ticker_frame(data, ticker, len(batch)),
                    session_date,
                    previous_session_date,
                )
                if error:
                    missing[ticker] = error
                elif row:
                    records.append(row)
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
        retry_batch_size = min(settings.batch_size, 20)
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
                retry_data = _download_batch(
                    retry_batch, start, end, settings, threads=False
                )
            except Exception as exc:
                LOGGER.warning("누락 ticker 재조회 실패 (%d개): %s", len(retry_batch), exc)
                continue
            for ticker in retry_batch:
                row, error = _parse_ticker(
                    ticker,
                    _ticker_frame(retry_data, ticker, len(retry_batch)),
                    session_date,
                    previous_session_date,
                )
                if error:
                    missing[ticker] = error
                elif row:
                    records.append(row)
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
            verified.append(candidate)
        except Exception as exc:
            rejected[ticker] = f"2차 검증 실패: {exc}"
    return (pd.DataFrame(verified, columns=candidates.columns), rejected)
