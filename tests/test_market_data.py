from datetime import date

import pandas as pd

from market_data import _parse_ticker, download_market_data
from config import Settings


def test_missing_session_is_reported():
    frame = pd.DataFrame(
        {"Open": [100], "Low": [88], "Close": [89], "Volume": [1000]},
        index=pd.to_datetime(["2025-01-03"]),
    )
    row, error = _parse_ticker("TEST", frame, date(2025, 1, 3), date(2025, 1, 2))
    assert row is None
    assert "필수 거래일 누락" in error


def test_insufficient_volume_history_is_reported():
    dates = pd.bdate_range("2024-12-23", "2025-01-03")
    frame = pd.DataFrame(
        {"Open": 100.0, "Low": 88.0, "Close": 100.0, "Volume": 1000.0},
        index=dates,
    )
    row, error = _parse_ticker("TEST", frame, date(2025, 1, 3), date(2025, 1, 2))
    assert row is None
    assert "20일 거래량 표본 부족" in error


def test_valid_data_calculates_metrics():
    dates = pd.bdate_range("2024-12-02", "2025-01-03")
    frame = pd.DataFrame(
        {"Open": 100.0, "Low": 88.0, "Close": 100.0, "Volume": 1000.0},
        index=dates,
    )
    frame.loc[pd.Timestamp("2025-01-03"), ["Open", "Close"]] = [95.0, 89.0]
    row, error = _parse_ticker("TEST", frame, date(2025, 1, 3), date(2025, 1, 2))
    assert error is None
    assert row["change_pct"] == pytest.approx(-11.0)
    assert row["average_volume_20d"] == 1000


import pytest


def test_large_partial_failure_retries_in_small_single_threaded_batches(monkeypatch, tmp_path):
    session_date = date(2025, 1, 3)
    previous_session_date = date(2025, 1, 2)
    dates = pd.bdate_range("2024-11-25", session_date)
    tickers = [f"T{index:02d}" for index in range(30)]
    constituents = pd.DataFrame(
        {
            "ticker": tickers,
            "yahoo_ticker": tickers,
            "company_name": tickers,
            "sector": "Test",
        }
    )
    calls = []

    def fake_download(requested, start, end, settings, *, threads=True):
        calls.append((list(requested), threads))
        columns = pd.MultiIndex.from_product(
            [["Open", "Low", "Close", "Volume"], requested]
        )
        data = pd.DataFrame(100.0, index=dates, columns=columns)
        data.loc[:, pd.IndexSlice["Volume", :]] = 1000.0
        if len(calls) == 1:
            data.loc[pd.Timestamp(previous_session_date), pd.IndexSlice["Close", :]] = float("nan")
        return data

    monkeypatch.setattr("market_data._download_batch", fake_download)
    settings = Settings(
        batch_size=30,
        batch_pause_seconds=0,
        retry_cooldown_seconds=0,
        cache_file=tmp_path / "constituents.csv",
        output_dir=tmp_path / "output",
        yfinance_cache_dir=tmp_path / "yf-cache",
    )

    prices, missing = download_market_data(
        constituents, session_date, previous_session_date, settings
    )

    assert len(prices) == 30
    assert missing == {}
    assert calls[0][1] is False
    assert [len(batch) for batch, threads in calls[1:] if threads is False] == [20, 10]
