from datetime import date

import pandas as pd

from market_data import _parse_ticker


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
