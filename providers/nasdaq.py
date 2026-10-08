"""Nasdaq historical quotes (US cross-validation).

``Close/Last`` of the historical table is the official consolidated regular
close (split-adjusted). The table often publishes the latest session late in
the evening, so an analysis-day row that is not there yet is STALE_SOURCE for
the close field only; the previous close stays usable.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from config import SETTINGS, Settings
from net import get_with_retry
from providers.base import PriceObservation, failed, from_series, positive

SOURCE = "Nasdaq"
URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def symbol_for(ticker: str) -> str:
    return ticker.strip().upper().replace("-", ".")


class SymbolUnknown(ValueError):
    """Nasdaq reports that the symbol does not exist (renamed or delisted)."""


def parse(payload: dict) -> dict[date, float | None]:
    table = ((payload or {}).get("data") or {}).get("tradesTable") or {}
    rows = table.get("rows") or []
    if not rows:
        status = (payload or {}).get("status") or {}
        messages = status.get("bCodeMessage") or []
        if any(isinstance(item, dict) and item.get("code") == 1001 for item in messages):
            raise SymbolUnknown("Nasdaq 심볼 없음(Symbol not exists): 티커 변경·상장폐지 가능")
        raise ValueError(f"Nasdaq 데이터 없음 ({messages or status.get('rCode')})")
    series: dict[date, float | None] = {}
    for row in rows:
        day = datetime.strptime(row["date"], "%m/%d/%Y").date()
        if day in series:
            raise ValueError(f"Nasdaq 중복 거래일 ({day})")
        series[day] = positive(row.get("close"))
    return series


def fetch(ticker: str, previous_date: date, session_date: date, settings: Settings = SETTINGS) -> PriceObservation:
    params = {"assetclass": "stocks", "fromdate": (previous_date - timedelta(days=5)).isoformat(),
              "todate": (session_date + timedelta(days=1)).isoformat(), "limit": 20}
    try:
        response = get_with_retry(URL.format(symbol=symbol_for(ticker)), params=params, headers=HEADERS,
                                  settings=settings, attempts=2)
        series = parse(response.json())
    except SymbolUnknown as exc:
        return failed(ticker, SOURCE, str(exc), symbol_unknown=True)
    except Exception as exc:
        return failed(ticker, SOURCE, f"Nasdaq 조회 실패: {exc}")
    return from_series(ticker, SOURCE, series, previous_date, session_date, adjustment="split")
