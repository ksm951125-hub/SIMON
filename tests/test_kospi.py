"""KOSPI: fail-soft listing, KRX regular-close pairs, validation and fallback."""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import kospi
from config import Settings
from kospi import (KospiSessionContext, KrPair, ListingError, NaverDay, _parse_yahoo_kr, download_kospi_market_data,
                   get_kospi_session_context, load_kospi_constituents, reconcile_kr)
from market_calendar import KST

PREV, DAY = date(2026, 9, 28), date(2026, 9, 29)
FAST = Settings(download_retries=1, retry_backoff_seconds=0, missing_retry_pause_seconds=0, availability_wait_seconds=0, max_workers=4)


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


def yahoo_kr(code="005930", days=(PREV, DAY), closes=(270000.0, 272500.0)):
    return {"chart": {"result": [{
        "meta": {"symbol": f"{code}.KS", "exchangeTimezoneName": "Asia/Seoul", "dataGranularity": "1d"},
        "timestamp": [int(datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc).timestamp()) for d in days],
        "indicators": {"quote": [{"close": list(closes), "volume": [1] * len(days)}]}}]}}


def test_yahoo_kr_parser_keys_by_kst_session_date():
    values = _parse_yahoo_kr(yahoo_kr(closes=(269999.99, 272500.01)), "005930")
    assert values[PREV][0] == 270000 and values[DAY][0] == 272500
    with pytest.raises(ValueError, match="symbol"):
        _parse_yahoo_kr(yahoo_kr("000660"), "005930")


def naver(close, base, volume=100, ratio=None, day=DAY):
    return {day: NaverDay(close, base, volume, ratio)}


@pytest.mark.parametrize("close,detected", [(93.01, False), (93.0, True), (92.99, True)])
def test_kr_threshold_boundaries_use_raw_change(close, detected):
    """TEST 5-7: -6.99% not detected, -7.00% and -7.01% detected."""
    row, _ = reconcile_kr("005930", KrPair(100.0, close), naver(close, 100.0), None, PREV, DAY, -7.0)
    from detector import is_drop
    assert is_drop(row["change_pct"], -7.0) is detected


def test_krx_close_is_used_not_nxt_integrated_price():
    # SK이노베이션 2026-09-29: KRX 158,200 -> 145,900; Naver integrated 147,200.
    row, _ = reconcile_kr("096770", KrPair(158200.0, 145900.0), naver(147200.0, 158200.0), None, PREV, DAY, -7.0)
    assert (row["previous_close"], row["close"]) == (158200.0, 145900.0)
    assert row["change_pct"] == pytest.approx(-7.774968, abs=1e-6)
    assert row["validation"] == "Naver KRX 기준가 일치" and not row["mismatch"]


def test_previous_close_mismatch_is_flagged_and_recall_preserved():
    row, _ = reconcile_kr("000001", KrPair(100.0, 93.5), naver(93.5, 101.0), None, PREV, DAY, -7.0)
    assert row["mismatch"] and row["change_pct"] <= -7.0 and "Naver KRX 기준가" in row["source"]


def test_garbage_yahoo_previous_close_replaced_by_krx_base():
    row, _ = reconcile_kr("000300", KrPair(21086206.0, 4200.0), naver(4200.0, 4200.0), None, PREV, DAY, -7.0)
    assert row["change_pct"] == 0.0


def test_halted_stock_is_valid_with_zero_change():
    row, reason = reconcile_kr("001470", KrPair(error="Yahoo 누락"), naver(5820.0, 5820.0, volume=0), None, PREV, DAY, -7.0)
    assert reason is None and row["change_pct"] == 0.0 and not row["fallback"]


def test_liquidation_trading_beyond_price_limit_is_kept_and_flagged():
    # 부산주공 2026-09-29: KRX base 486 -> 37 (정리매매, no ±30% limit).
    row, _ = reconcile_kr("005030", KrPair(error="Yahoo 전일 누락"), naver(37.0, 486.0, volume=28872090), None, PREV, DAY, -7.0)
    assert row["fallback"] and row["limit_exceeded"] and row["change_pct"] == pytest.approx(-92.386831, abs=1e-5)


@pytest.mark.parametrize("pair", [KrPair(error="current NaN"), KrPair(error="previous NaN")])
def test_nan_prices_without_backup_are_missing(pair):
    """TEST 13/14."""
    row, reason = reconcile_kr("000002", pair, None, "Naver down", PREV, DAY, -7.0)
    assert row is None and "Naver down" in reason


def fake_markets(monkeypatch, yahoo: dict, naver_days: dict):
    monkeypatch.setattr(kospi, "fetch_yahoo_kr_pair", lambda c, p, d, s: yahoo.get(c, KrPair(error=f"Yahoo 누락 {c}")))

    def fetch(code, settings, page_size):
        if code not in naver_days:
            raise RuntimeError("HTTP 404")
        return naver_days[code]

    monkeypatch.setattr(kospi, "fetch_naver_days", fetch)


def test_one_missing_stock_does_not_fail_market(monkeypatch):
    """TEST 11/16: provider gap on one symbol -> missing 1, the rest analyzed."""
    codes = [f"{i:06d}" for i in range(50)]
    universe = pd.DataFrame({"code": codes, "company_name": codes})
    yahoo = {c: KrPair(1000.0, 1000.0) for c in codes[1:]}
    yahoo["000010"] = KrPair(1000.0, 930.0)
    days = {c: naver(1000.0, 1000.0) for c in codes[1:]}
    fake_markets(monkeypatch, yahoo, days)
    result = download_kospi_market_data(universe, KospiSessionContext(DAY, PREV), FAST)
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
