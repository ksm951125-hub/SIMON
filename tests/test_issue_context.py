"""A near-threshold notice states the move itself.

2026-10-09 sample mail: "COHR Coherent Corp." appeared in the notice box with two prices and
a source lag, but not the -9.63% that made it worth a mention (it is below the -10% threshold, so
it is not in the table)."""
from datetime import date

import pandas as pd

from helpers import FAST, fake_us
from market_data import download_market_data
from market_result import issues_from_prices

PREV, DAY = date(2026, 10, 7), date(2026, 10, 8)


def universe(*tickers):
    return pd.DataFrame({"ticker": list(tickers), "yahoo_ticker": list(tickers),
                         "company_name": [f"{t} Corp" for t in tickers], "sector": "X"})


def test_notice_leads_with_the_move(monkeypatch):
    # Nasdaq has not published the analysis day; Yahoo and CNBC agree on -9.63%: INFO, not detected.
    fake_us(monkeypatch, yahoo={"COHX": (334.56, 302.35)}, nasdaq={"COHX": (334.56, "stale")},
            cnbc={"COHX": (None, 302.35)})
    prices = download_market_data(universe("COHX"), DAY, PREV, FAST).prices
    assert not prices.iloc[0].detected
    issues, _ = issues_from_prices(prices, "ticker", -8.0)
    assert [(issue.level, issue.category) for issue in issues] == [("INFO", "STALE_SECONDARY")]
    assert issues[0].message.startswith("등락 -9.63% · ") and "교차검증 완료" in issues[0].message
    assert issues[0].symbol == "COHX"


def test_stocks_far_from_the_threshold_stay_out_of_the_notices(monkeypatch):
    fake_us(monkeypatch, yahoo={"CALM": (100.0, 99.0)}, nasdaq={"CALM": (100.0, "stale")}, cnbc={"CALM": (None, 99.0)})
    prices = download_market_data(universe("CALM"), DAY, PREV, FAST).prices
    issues, notes = issues_from_prices(prices, "ticker", -8.0)
    assert issues == [] and not any("CALM" in note for note in notes)
