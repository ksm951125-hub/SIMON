"""US orchestration: Yahoo primary, Nasdaq secondary, CNBC tertiary."""
from datetime import date

import pandas as pd
import pytest

import market_data
from config import Settings
from helpers import FAST, FIXTURES, fake_us, obs
from market_data import download_market_data
from providers.base import PriceObservation

PREV, DAY = date(2026, 9, 25), date(2026, 9, 28)


def constituents(*tickers):
    return pd.DataFrame({"ticker": list(tickers), "yahoo_ticker": [t.replace(".", "-") for t in tickers],
                         "company_name": [f"{t} Corp" for t in tickers], "sector": "X"})


def test_nasdaq_stale_close_is_verified_by_cnbc(monkeypatch):
    fake_us(monkeypatch, yahoo={"AAA": (100, 80)}, nasdaq={"AAA": (100, "stale")}, cnbc={"AAA": (None, 80)})
    result = download_market_data(constituents("AAA"), DAY, PREV, FAST)
    row = result.prices.iloc[0]
    assert row.validation_status == "CROSS_VALIDATED" and row.detected and not result.missing
    assert row.severity == "INFO" and "Nasdaq" in row.stale_sources


def nasdaq_calls(monkeypatch):
    calls = []
    original = market_data.nasdaq.fetch

    def spy(ticker, previous, current, settings=None):
        calls.append(ticker)
        return original(ticker, previous, current, settings)

    monkeypatch.setattr(market_data.nasdaq, "fetch", spy)
    return calls


def test_cnbc_confirms_both_fields_so_nasdaq_is_only_used_near_threshold(monkeypatch):
    rows = {"AAA": (100, 99), "BBB": (100, 50), "CCC": (100, 101)}
    fake_us(monkeypatch, yahoo=rows, cnbc={t: pair for t, pair in rows.items()})
    calls = nasdaq_calls(monkeypatch)
    result = download_market_data(constituents(*rows), DAY, PREV, FAST)
    assert calls == ["BBB"]  # only the near-threshold symbol gets a third source
    assert result.prices["validation_status"].eq("CROSS_VALIDATED").all()


def test_ex_dividend_rebased_cnbc_previous_close_is_ignored_not_a_mismatch(monkeypatch):
    # CNBC re-bases the previous close for an ex-dividend on the analysis day.
    fake_us(monkeypatch, yahoo={"AMT": (168.00, 167.35)}, nasdaq={"AMT": (168.00, "stale")},
            cnbc={"AMT": (166.21, 167.35)})
    calls = nasdaq_calls(monkeypatch)
    row = download_market_data(constituents("AMT"), DAY, PREV, FAST).prices.iloc[0]
    assert calls == ["AMT"] and row.validation_status == "CROSS_VALIDATED" and not row.mismatch


def test_all_cross_sources_down_keeps_primary_and_flags_band(monkeypatch):
    fake_us(monkeypatch, yahoo={"AAA": (100, 91), "BBB": (100, 99)}, nasdaq={}, cnbc={})
    result = download_market_data(constituents("AAA", "BBB"), DAY, PREV, FAST)
    rows = result.prices.set_index("ticker")
    assert rows.loc["AAA", "severity"] == "WARNING" and rows.loc["BBB", "severity"] == "INFO"
    assert len(result.prices) == 2 and not result.missing


def test_primary_gap_uses_fallback_and_is_never_dropped(monkeypatch):
    fake_us(monkeypatch, yahoo={}, nasdaq={"AAA": (100, 85)}, cnbc={"AAA": (None, 85)})
    result = download_market_data(constituents("AAA"), DAY, PREV, FAST)
    row = result.prices.iloc[0]
    # Close confirmed by Nasdaq+CNBC, previous close by Nasdaq alone -> unverified fallback.
    assert row.validation_status == "FALLBACK_UNVERIFIED" and row.detected and row.severity == "WARNING"
    assert result.fallback_count == 1


def test_symbol_with_no_data_is_missing_with_reasons(monkeypatch):
    fake_us(monkeypatch, yahoo={"AAA": (100, 99)}, nasdaq={"AAA": (100, 99)}, cnbc={})
    result = download_market_data(constituents("AAA", "ZZZ"), DAY, PREV, FAST)
    assert list(result.missing) == ["ZZZ"] and "Yahoo" in result.missing["ZZZ"]


def test_share_class_symbol_mapping(monkeypatch):
    fake_us(monkeypatch, yahoo={"BRK-B": (400, 401)})
    result = download_market_data(constituents("BRK.B"), DAY, PREV, FAST)
    assert result.prices.iloc[0].ticker == "BRK.B" and result.prices.iloc[0].yahoo_ticker == "BRK-B"


def test_nasdaq_throttling_trips_breaker_after_consecutive_failures(monkeypatch):
    tickers = [f"T{i:03d}" for i in range(100)]
    fake_us(monkeypatch, yahoo={t: (100, 100 - int(t[1:]) / 10) for t in tickers})
    calls = []

    def nasdaq_fetch(ticker, previous, current, settings=None):
        calls.append(ticker)
        if len(calls) <= 10:
            return obs(ticker, "Nasdaq", 100, 100 - int(ticker[1:]) / 10, prev_date=previous, day=current)
        return obs(ticker, "Nasdaq", "down", "down", prev_date=previous, day=current)

    monkeypatch.setattr(market_data.nasdaq, "fetch", nasdaq_fetch)
    result = download_market_data(constituents(*tickers), DAY, PREV, Settings(max_workers=1, missing_retry_pause_seconds=0,
                                                                                nasdaq_min_interval_seconds=0))
    assert len(calls) == 10 + market_data.NASDAQ_CIRCUIT_BREAKER
    # The largest decliners were checked first.
    verified = set(result.prices.loc[result.prices.cross_checked, "ticker"])
    assert verified == {f"T{i:03d}" for i in range(90, 100)}


def test_us_2026_09_28_regression_from_recorded_closes(monkeypatch):
    """Real regular closes recorded from Yahoo and Nasdaq for all 503 symbols."""
    frame = pd.read_csv(FIXTURES / "us-2026-09-28-closes.csv", dtype={"ticker": str})
    yahoo = {r.ticker.replace(".", "-"): (round(r.yahoo_prev_2026_09_25, 2), round(r.yahoo_close_2026_09_28, 2))
             for r in frame.itertuples()}
    nasdaq = {r.ticker.replace(".", "-"): (float(r.nasdaq_prev_2026_09_25), float(r.nasdaq_close_2026_09_28))
              for r in frame.itertuples() if pd.notna(r.nasdaq_prev_2026_09_25)}
    fake_us(monkeypatch, yahoo=yahoo, nasdaq=nasdaq)
    result = download_market_data(constituents(*frame.ticker), DAY, PREV, FAST)
    assert len(result.prices) == 503 and not result.missing and result.mismatch_count == 0
    assert result.cross_checked_count == 502  # BF.B: Nasdaq unsupported, close via CNBC only
    assert result.prices["detected"].sum() == 0
    worst = result.prices.sort_values("change_pct").iloc[0]
    assert worst.ticker == "BE" and worst.change_pct == pytest.approx(-8.947, abs=1e-3)


def test_observation_type_contract():
    assert isinstance(obs("T", "X", 1, 2, prev_date=PREV, day=DAY), PriceObservation)


def test_many_single_source_fallbacks_escalate_to_warning(monkeypatch):
    from market_result import issues_from_prices
    # Yahoo down for everything, only Nasdaq answers (CNBC down): single-source values.
    rows = {f"T{i:02d}": (100, 99) for i in range(40)}
    fake_us(monkeypatch, yahoo={}, nasdaq=rows, cnbc={})
    result = download_market_data(constituents(*rows), DAY, PREV, FAST)
    assert set(result.prices["validation_status"]) == {"FALLBACK_UNVERIFIED"}
    issues, notes = issues_from_prices(result.prices, "ticker", -8.0)
    assert any(issue.level == "WARNING" and issue.category == "FALLBACK_SINGLE" for issue in issues)
