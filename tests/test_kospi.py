"""KOSPI: fail-soft listing, calendar, KRX regular-close consensus and special trading."""
from dataclasses import replace
from datetime import date, datetime

import pandas as pd
import pytest

import kospi
from helpers import FAST, obs
from kospi import KospiSessionContext, ListingError, download_kospi_market_data, get_kospi_session_context, load_kospi_constituents
from market_calendar import KST
from providers.base import INTEGRATED
from providers.naver import NaverDay
from special_trading import MarketState

PREV, DAY = date(2026, 9, 28), date(2026, 9, 29)


def item(code, kind="stock", name=None, market="KOSPI"):
    return {"itemCode": code, "stockName": name if name is not None else f"종목{code}", "stockEndType": kind,
            "stockExchangeType": {"nameEng": market}}


def listing_server(monkeypatch, passes):
    """passes: list of (totalCount, items) returned by successive listing passes."""
    state = {"pass": -1}

    def fake_json(url, params, settings=None):
        assert url == kospi.NAVER_MARKET_URL
        if params["page"] == 1:
            state["pass"] = min(state["pass"] + 1, len(passes) - 1)
        total, items = passes[state["pass"]]
        page = params["page"]
        return {"totalCount": total, "stocks": items[(page - 1) * 100: page * 100]}

    monkeypatch.setattr(kospi, "_json", fake_json)
    monkeypatch.setattr(kospi.time, "sleep", lambda *_: None)


def universe_items(stocks=944, etfs=1171, etns=364):
    items = [item(f"{i:06d}") for i in range(stocks)]
    items += [item(f"E{i:05d}", "etf") for i in range(etfs)]
    items += [item(f"N{i:05d}", "etn") for i in range(etns)]
    return items


def test_listing_complete_has_no_warning(monkeypatch):
    items = universe_items()
    listing_server(monkeypatch, [(len(items), items)])
    frame = load_kospi_constituents(FAST)
    assert len(frame) == 944 and frame.attrs["unresolved"] == 0 and frame.attrs["warnings"] == []


def test_2479_of_2480_listing_is_fail_soft(monkeypatch):
    """TEST 18: the production error 'KOSPI listing incomplete: 2479/2480'."""
    items = universe_items()  # 2479 items
    listing_server(monkeypatch, [(2480, items)] * 3)
    frame = load_kospi_constituents(FAST)
    assert len(frame) == 944
    assert frame.attrs["expected"] == 2480 and frame.attrs["loaded"] == 2479 and frame.attrs["unresolved"] == 1
    assert "expected 2480, loaded 2479, missing/unresolved 1" in frame.attrs["warnings"][0]


def test_listing_retry_union_recovers_item_missing_in_one_pass(monkeypatch):
    items = universe_items()
    listing_server(monkeypatch, [(2479, items[:-1]), (2479, items)])
    frame = load_kospi_constituents(FAST)
    assert frame.attrs["unresolved"] == 0 and len(frame) == 944


def test_broken_listing_still_fails(monkeypatch):
    items = universe_items()
    listing_server(monkeypatch, [(2479, items[:2000])] * 3)
    with pytest.raises(ListingError, match="broken"):
        load_kospi_constituents(FAST)
    listing_server(monkeypatch, [(300, [item(f"{i:06d}") for i in range(300)])])
    with pytest.raises(ListingError, match="too small"):
        load_kospi_constituents(FAST)


def test_one_unparseable_ticker_is_warning_not_failure(monkeypatch):
    """TEST 12: a blank/invalid code is skipped with a warning; the rest continue."""
    items = universe_items()
    items[5] = item("", name="코드없음")
    items[6] = item("12AB", name="잘못된코드")
    listing_server(monkeypatch, [(len(items), items)])
    frame = load_kospi_constituents(FAST)
    assert len(frame) == 942
    assert "파싱 제외 2건" in frame.attrs["warnings"][0]


def test_duplicate_listing_rows_are_deduplicated(monkeypatch):
    items = universe_items()
    duplicated = items[:-1] + [items[0]]
    listing_server(monkeypatch, [(len(items), duplicated), (len(items), items)])
    frame = load_kospi_constituents(FAST)
    assert frame["code"].is_unique and len(frame) == 944


def state(**kw):
    defaults = dict(source="Daum", as_of=DAY, nxt_tradable=True)
    defaults.update(kw)
    return MarketState(**defaults)


def fake_kr(monkeypatch, rows: dict):
    """rows: code -> dict(yahoo=(prev, close), daum=(prev, close), naver=(base, integrated, volume),
    days=(base, integrated), state=MarketState)."""

    def yahoo(code, previous, current, settings=None):
        prev, close = rows[code].get("yahoo", ("down", "down"))
        return obs(code, "Yahoo", prev, close, prev_date=previous, day=current)

    def quote(code, previous, current, next_session, settings=None):
        prev, close = rows[code].get("daum", ("down", "down"))
        return obs(code, "Daum", prev, close, prev_date=previous, day=current), rows[code].get("state")

    def daily(code, previous, current, settings=None, page_size=10, next_session=None):
        if "naver" not in rows[code]:
            return obs(code, "Naver", "down", "down", prev_date=previous, day=current), None, None
        base, integrated, volume = rows[code]["naver"]
        observation = obs(code, "Naver", base, integrated, prev_date=previous, day=current, close_type=INTEGRATED)
        observation.close = replace(observation.close, corroborative=True)
        return observation, NaverDay(integrated, base, volume, None), None

    def days(code, previous, current, settings=None):
        base, integrated = rows[code].get("days", ("down", "down"))
        observation = obs(code, "Daum days", base, integrated, prev_date=previous, day=current, close_type=INTEGRATED)
        observation.close = replace(observation.close, corroborative=True)
        return observation

    monkeypatch.setattr(kospi.yahoo, "fetch_kr", yahoo)
    monkeypatch.setattr(kospi.daum, "fetch_quote", quote)
    monkeypatch.setattr(kospi.naver, "fetch_daily", daily)
    monkeypatch.setattr(kospi.daum, "fetch_days", days)


def run_kr(monkeypatch, rows, threshold=None):
    fake_kr(monkeypatch, rows)
    universe = pd.DataFrame({"code": list(rows), "company_name": [f"종목{c}" for c in rows]})
    return download_kospi_market_data(universe, KospiSessionContext(DAY, PREV), FAST, threshold_pct=threshold)


def normal(prev, close, integrated=None):
    return dict(yahoo=(prev, close), daum=(prev, close), naver=(prev, integrated or close + 100, 1000), state=state())


def test_krx_close_used_not_nxt_integrated_price(monkeypatch):
    # SK이노베이션 2026-09-29: KRX 158,200 -> 145,900; Naver integrated 147,200.
    result = run_kr(monkeypatch, {"096770": normal(158200, 145900, integrated=147200)})
    row = result.prices.iloc[0]
    assert (row.previous_close, row.close) == (158200, 145900) and row.validation_status == "CROSS_VALIDATED"
    assert row.change_pct == pytest.approx(-7.774968, abs=1e-6) and row.detected


@pytest.mark.parametrize("close,detected", [(93.01, False), (93.0, True), (92.99, True)])
def test_kr_threshold_boundaries_use_raw_change(monkeypatch, close, detected):
    result = run_kr(monkeypatch, {"000001": normal(100, close)})
    assert bool(result.prices.iloc[0].detected) is detected


def test_non_nxt_stock_integrated_close_counts_as_regular(monkeypatch):
    rows = {"002785": dict(yahoo=(10110, "stale"), daum=("down", "down"), naver=(10110, 8700, 500),
                           days=(10110, 8700), state=state(nxt_tradable=False))}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.close == 8700 and row.validation_status == "FALLBACK_VALIDATED"
    assert "비NXT" in row.source


def test_nxt_stock_integrated_close_is_never_a_regular_close(monkeypatch):
    rows = {"096770": dict(yahoo=(158200, "stale"), daum=("down", "down"), naver=(158200, 147200, 1000),
                           days=(158200, 147200), state=state(nxt_tradable=True))}
    result = run_kr(monkeypatch, rows)
    assert result.prices.empty and "096770" in result.missing


def test_integrated_close_equal_to_regular_close_corroborates(monkeypatch):
    # Daum does not cover the security; Naver's integrated last trade equals Yahoo's KRX close.
    rows = {"094800": dict(yahoo=(8000, 7880), naver=(8000, 7880, 34879), state=None)}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.validation_status == "CROSS_VALIDATED" and row.severity == "NORMAL"


def test_integrated_close_different_from_regular_close_is_not_a_mismatch(monkeypatch):
    rows = {"096770": dict(yahoo=(158200, 145900), naver=(158200, 147200, 1000), state=None)}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert not row.mismatch and row.close == 145900 and row.validation_status == "PRIMARY_ONLY"


def test_halted_stock_with_corrupt_yahoo_bar(monkeypatch):
    rows = {"000300": dict(yahoo=(21086206, 4200), daum=(None, 4200), naver=(4200, 4200, 0),
                           state=state(trading_suspended=True, upper_limit=0.0, lower_limit=0.0))}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.change_pct == 0 and not row.detected and row.severity != "WARNING"


def test_no_trade_day_close_confirmed_by_base_price_rule(monkeypatch):
    # No trades (Naver volume 0, close == base); Yahoo has no bar for the day and
    # Daum does not cover the security. Yahoo's previous close equals the base.
    rows = {"0120X0": dict(yahoo=(10085, "stale"), naver=(10085, 10085, 0), state=None)}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.change_pct == 0 and row.validation_status == "FALLBACK_VALIDATED" and row.severity == "NORMAL"
    assert not row.fallback and "무거래" in row.validation


def test_special_trading_beyond_limit_is_detected_as_info(monkeypatch):
    rows = {"999990": dict(yahoo=("stale", 37), daum=(None, 37), naver=(486, 37, 28872090), days=(486, 37),
                           state=state(pre_delisting_trading=True, upper_limit=0.0, lower_limit=0.0,
                                       nxt_tradable=False))}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.detected and row.special_label == "SPECIAL_TRADING_VALIDATED" and row.severity == "INFO"


def test_unexplained_move_beyond_limit_is_warning_but_still_detected(monkeypatch):
    rows = {"999991": dict(normal(1000, 500), state=state())}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.detected and row.special_label == "SPECIAL_TRADING_UNCONFIRMED" and row.severity == "WARNING"


def test_previous_close_disagreement_near_threshold_is_warning(monkeypatch):
    rows = {"000001": dict(yahoo=(100, 93.5), daum=(None, 93.5), naver=(101, 94, 10), state=state())}
    row = run_kr(monkeypatch, rows).prices.iloc[0]
    assert row.validation_status == "CROSS_SOURCE_MISMATCH" and row.detected and row.severity == "WARNING"


def test_one_missing_stock_does_not_fail_market(monkeypatch):
    rows = {f"{i:06d}": normal(1000, 1000) for i in range(1, 50)}
    rows["000010"] = normal(1000, 930)
    rows["000000"] = {}
    result = run_kr(monkeypatch, rows)
    assert len(result.prices) == 49 and list(result.missing) == ["000000"]
    assert list(result.prices.loc[result.prices.detected, "code"]) == ["000010"]


def test_session_context_uses_actual_traded_dates_across_chuseok(monkeypatch):
    rows = [{"localTradedAt": d} for d in ("2026-09-29", "2026-09-28", "2026-09-23", "2026-09-22")]
    monkeypatch.setattr(kospi, "_json", lambda url, params, settings=None: rows if params["page"] == 1 else [])
    context = get_kospi_session_context(now=datetime(2026, 9, 30, 8, tzinfo=KST))
    assert (context.session_date, context.previous_session_date, context.warning) == (DAY, PREV, None)
    context = get_kospi_session_context(now=datetime(2026, 9, 29, 8, tzinfo=KST))
    assert (context.session_date, context.previous_session_date) == (PREV, date(2026, 9, 23))


def test_kr_holiday_morning_reuses_latest_completed_session(monkeypatch):
    """TEST 10: 2026-09-25 (Chuseok, KRX closed) morning -> 09-23 vs 09-22."""
    rows = [{"localTradedAt": d} for d in ("2026-09-23", "2026-09-22", "2026-09-21")]
    monkeypatch.setattr(kospi, "_json", lambda url, params, settings=None: rows if params["page"] == 1 else [])
    context = get_kospi_session_context(now=datetime(2026, 9, 25, 8, tzinfo=KST))
    assert (context.session_date, context.previous_session_date) == (date(2026, 9, 23), date(2026, 9, 22))


def test_session_context_falls_back_to_calendar_when_index_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(kospi, "_json", fail)
    context = get_kospi_session_context(now=datetime(2026, 9, 30, 8, tzinfo=KST))
    assert (context.session_date, context.previous_session_date) == (DAY, PREV)
    assert "XKRX" in context.warning


def test_future_or_unsettled_override_rejected():
    with pytest.raises(ValueError, match="not completed"):
        get_kospi_session_context(date(2026, 9, 29), now=datetime(2026, 9, 29, 17, tzinfo=KST))
