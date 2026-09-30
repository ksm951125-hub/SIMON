"""Generic KRX price-limit / special trading classification (no stock-specific rules)."""
from datetime import date

import pytest

from special_trading import (LIMIT_OK, SPECIAL_TRADING_UNCONFIRMED, SPECIAL_TRADING_VALIDATED, MarketState,
                             assess_kr_move)

DAY = date(2026, 9, 29)


def state(**kw):
    defaults = dict(source="Daum", as_of=DAY, upper_limit=1300.0, lower_limit=700.0)
    defaults.update(kw)
    return MarketState(**defaults)


def test_normal_move_within_limit():
    assert assess_kr_move(1000, 930, 1000, state(), DAY).label == LIMIT_OK


def test_explicit_limit_prices_are_used_when_available():
    assert assess_kr_move(1000, 700, 1000, state(), DAY).label == LIMIT_OK        # exactly lower limit
    assert assess_kr_move(1000, 699, 1000, state(), DAY).label == SPECIAL_TRADING_UNCONFIRMED


@pytest.mark.parametrize("flags,reason", [
    (dict(pre_delisting_trading=True, upper_limit=0.0, lower_limit=0.0), "정리매매"),
    (dict(upper_limit=0.0, lower_limit=0.0), "가격제한폭 미적용"),
    (dict(listing_date=date(2026, 9, 25), upper_limit=None, lower_limit=None), "신규/재상장"),
    (dict(par_value_change="SPLIT", upper_limit=None, lower_limit=None), "액면변경"),
    (dict(revaluation="DRAWDOWN", upper_limit=None, lower_limit=None), "기준가 재산정"),
])
def test_beyond_limit_with_special_state_is_validated(flags, reason):
    result = assess_kr_move(486, 37, 486, state(**flags), DAY)
    assert result.label == SPECIAL_TRADING_VALIDATED and any(reason in item for item in result.reasons)
    assert "가격제한폭 검증 제외" in result.note


def test_beyond_limit_without_known_state_needs_review():
    result = assess_kr_move(486, 37, 486, None, DAY)
    assert result.label == SPECIAL_TRADING_UNCONFIRMED and "수동 확인" in result.note


def test_limit_is_measured_against_base_price_not_previous_close():
    # Base re-set (e.g. after a split): -40% vs previous close but within +-30% of base.
    result = assess_kr_move(10000, 5800, 6000, None, DAY)
    assert result.label == LIMIT_OK and "기준가" in result.note


def test_state_from_next_session_still_identifies_liquidation_trading():
    # A rolled snapshot (next day) still proves an ongoing 정리매매 period.
    rolled = state(as_of=date(2026, 9, 30), pre_delisting_trading=True, upper_limit=0.0, lower_limit=0.0)
    assert assess_kr_move(486, 37, 486, rolled, DAY).label == SPECIAL_TRADING_VALIDATED
