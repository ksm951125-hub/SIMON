from datetime import date

import pandas as pd
import pytest

from detector import calculate_change_pct, is_drop
from kospi import (
    KospiSessionContext,
    _parse_close,
    download_kospi_market_data,
    get_kospi_session_context,
    load_kospi_constituents,
)


@pytest.mark.parametrize(
    ("latest", "expected", "detected"),
    [
        (93.01, -6.99, False),
        (93.00, -7.00, True),
        (92.99, -7.01, True),
    ],
)
def test_kospi_drop_boundary(latest, expected, detected):
    change = calculate_change_pct(100.0, latest)
    assert change == pytest.approx(expected)
    assert is_drop(change, -7.0) is detected


@pytest.mark.parametrize(
    ("latest", "expected", "detected"),
    [
        (90.01, -9.99, False),
        (90.00, -10.00, True),
        (89.99, -10.01, True),
    ],
)
def test_sp500_boundary_is_unchanged(latest, expected, detected):
    change = calculate_change_pct(100.0, latest)
    assert change == pytest.approx(expected)
    assert is_drop(change, -10.0) is detected


def test_kospi_listing_excludes_non_stock_products(monkeypatch):
    payload = {
        "totalCount": 4,
        "stocks": [
            {"itemCode": "000001", "stockName": "Common", "stockEndType": "stock", "stockExchangeType": {"nameEng": "KOSPI"}},
            {"itemCode": "000002", "stockName": "Preferred", "stockEndType": "stock", "stockExchangeType": {"nameEng": "KOSPI"}},
            {"itemCode": "100001", "stockName": "ETF", "stockEndType": "etf", "stockExchangeType": {"nameEng": "KOSPI"}},
            {"itemCode": "200001", "stockName": "ETN", "stockEndType": "etn", "stockExchangeType": {"nameEng": "KOSPI"}},
        ],
    }
    monkeypatch.setattr("kospi._request_json", lambda *args, **kwargs: payload)

    frame = load_kospi_constituents()

    assert frame["code"].tolist() == ["000001", "000002"]


def test_kospi_historical_regression(monkeypatch):
    constituents = pd.DataFrame(
        [
            {"code": "001740", "company_name": "SK Networks"},
            {"code": "005930", "company_name": "Samsung Electronics"},
        ]
    )
    rows = {
        "001740": [
            {"localTradedAt": "2026-06-05", "closePrice": "10,900"},
            {"localTradedAt": "2026-06-04", "closePrice": "13,010"},
        ],
        "005930": [
            {"localTradedAt": "2026-06-05", "closePrice": "329,000"},
            {"localTradedAt": "2026-06-04", "closePrice": "351,500"},
        ],
    }
    monkeypatch.setattr("kospi._price_rows", lambda code, page: rows[code])
    context = KospiSessionContext(date(2026, 6, 5), date(2026, 6, 4), 2)

    prices, missing = download_kospi_market_data(constituents, context)
    detected = prices.loc[prices["change_pct"].map(lambda value: is_drop(value, -7.0))]

    assert missing == {}
    assert detected["code"].tolist() == ["001740"]
    assert detected.iloc[0]["change_pct"] == pytest.approx(-16.218293620292)
    assert _parse_close("1,234") == 1234.0


def test_kospi_weekend_uses_latest_two_data_sessions(monkeypatch):
    rows = [
        {"localTradedAt": "2026-06-12", "closePrice": "100"},
        {"localTradedAt": "2026-06-11", "closePrice": "101"},
    ]
    monkeypatch.setattr("kospi._price_rows", lambda *args, **kwargs: rows)

    context = get_kospi_session_context()

    assert context.session_date == date(2026, 6, 12)
    assert context.previous_session_date == date(2026, 6, 11)


def test_kospi_holiday_override_is_rejected(monkeypatch):
    rows = [
        {"localTradedAt": "2026-06-05", "closePrice": "100"},
        {"localTradedAt": "2026-06-04", "closePrice": "101"},
    ]
    monkeypatch.setattr("kospi._price_rows", lambda *args, **kwargs: rows)

    with pytest.raises(ValueError, match="not a KOSPI trading session"):
        get_kospi_session_context(date(2026, 6, 6))


def test_kospi_long_holiday_keeps_actual_data_dates(monkeypatch):
    rows = [
        {"localTradedAt": "2026-10-12", "closePrice": "100"},
        {"localTradedAt": "2026-10-02", "closePrice": "101"},
    ]
    monkeypatch.setattr("kospi._price_rows", lambda *args, **kwargs: rows)

    context = get_kospi_session_context()

    assert context.session_date == date(2026, 10, 12)
    assert context.previous_session_date == date(2026, 10, 2)
