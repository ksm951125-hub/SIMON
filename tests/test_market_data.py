from datetime import date

import pandas as pd
import requests

from market_data import (
    _decode_spark_payload,
    _download_close_batch,
    _download_nasdaq_snapshot,
    _parse_nasdaq_number,
    _parse_spark_response,
    _parse_ticker,
    download_market_data,
)
from config import Settings


def test_nasdaq_unchanged_value_is_zero():
    assert _parse_nasdaq_number("UNCH") == 0.0


def test_nasdaq_snapshot_parses_prices_and_symbol_alias(tmp_path, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "table": {
                        "rows": [
                            {
                                "symbol": "BRK/B",
                                "lastsale": "$450.00",
                                "netchange": "-50.00",
                                "pctchange": "-10.000%",
                            }
                        ]
                    }
                }
            }

    monkeypatch.setattr("market_data.requests.get", lambda *args, **kwargs: FakeResponse())
    constituents = pd.DataFrame(
        {
            "ticker": ["BRK.B"],
            "yahoo_ticker": ["BRK-B"],
            "company_name": ["Berkshire Hathaway"],
            "sector": ["Financials"],
        }
    )
    settings = Settings(
        cache_file=tmp_path / "constituents.csv",
        output_dir=tmp_path / "output",
        yfinance_cache_dir=tmp_path / "yf-cache",
    )

    prices, missing = _download_nasdaq_snapshot(constituents, date(2025, 1, 3), settings)

    assert missing == {}
    assert prices.iloc[0]["previous_close"] == pytest.approx(500.0)
    assert prices.iloc[0]["close"] == pytest.approx(450.0)
    assert prices.iloc[0]["change_pct"] == pytest.approx(-10.0)


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


class _WrappedResponse:
    text = 'Title: Yahoo\n\nMarkdown Content:\n{"spark":{"result":[]}}\n'

    def json(self):
        raise requests.exceptions.JSONDecodeError("not json", self.text, 0)


def test_decode_spark_payload_from_reader_wrapper():
    assert _decode_spark_payload(_WrappedResponse()) == {"spark": {"result": []}}


def test_query1_429_uses_query2(monkeypatch, tmp_path):
    payload = (
        '{"spark":{"result":[{"symbol":"AAPL","response":[{'
        '"timestamp":[1735851600],"indicators":{"quote":[{"close":[100.0]}]}'
        '}]}]}}'
    )
    calls = []

    class FakeResponse:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                error = requests.HTTPError(f"status {self.status_code}")
                error.response = self
                raise error

        def json(self):
            if self.text.startswith("Title:"):
                raise requests.exceptions.JSONDecodeError("wrapped", self.text, 0)
            if self.text:
                return {
                    "spark": {
                        "result": [
                            {
                                "symbol": "AAPL",
                                "response": [
                                    {
                                        "timestamp": [1735851600],
                                        "indicators": {"quote": [{"close": [100.0]}]},
                                    }
                                ],
                            }
                        ]
                    }
                }
            return {"spark": {"result": []}}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if len(calls) == 1:
            return FakeResponse(429)
        return FakeResponse(200, payload)

    monkeypatch.setattr("market_data.requests.get", fake_get)
    settings = Settings(
        download_retries=1,
        cache_file=tmp_path / "constituents.csv",
        output_dir=tmp_path / "output",
        yfinance_cache_dir=tmp_path / "yf-cache",
    )

    result = _download_close_batch(["AAPL"], settings)

    assert result["AAPL"][date(2025, 1, 2)] == 100.0
    assert calls[0][1] is not None
    assert calls[1][1] is not None
    assert "query1.finance.yahoo.com" in calls[0][0]
    assert "query2.finance.yahoo.com" in calls[1][0]


def test_parse_spark_response_maps_timestamps_to_new_york_dates():
    payload = {
        "spark": {
            "result": [
                {
                    "symbol": "AAPL",
                    "response": [
                        {
                            "timestamp": [1735851600, 1735938000],
                            "indicators": {"quote": [{"close": [100.0, 89.0]}]},
                        }
                    ],
                }
            ]
        }
    }

    parsed = _parse_spark_response(payload)

    assert parsed["AAPL"][date(2025, 1, 2)] == 100.0
    assert parsed["AAPL"][date(2025, 1, 3)] == 89.0


def test_large_partial_spark_failure_retries_in_batches(monkeypatch, tmp_path):
    session_date = date(2025, 1, 3)
    previous_session_date = date(2025, 1, 2)
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

    def fake_download(requested, settings):
        calls.append(list(requested))
        returned = requested[:10] if len(calls) == 1 else requested
        data = {
            ticker: {previous_session_date: 100.0, session_date: 89.0}
            for ticker in returned
        }
        return data

    monkeypatch.setattr("market_data._download_close_batch", fake_download)
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
    assert [len(batch) for batch in calls] == [30, 20]
