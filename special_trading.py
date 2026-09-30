"""KRX price-limit rules and special trading states.

The KOSPI +-30% daily limit applies to the KRX BASE PRICE of the day, and not
at all in several situations (정리매매, first day of a new listing, re-listing
after 감자/병합 where the base is re-set by auction, ...). A move beyond +-30%
versus the previous close is therefore not a data error by itself: it is first
checked against the market state of the security, and only a move that no
known rule can explain is flagged for manual review.

Nothing here is keyed on a specific stock code: the decision uses only the
market-state fields reported by the data source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

NORMAL_LIMIT_PCT = 30.0
# KRX rounds limit prices to the tick size; allow a little slack when the
# source does not publish explicit limit prices.
LIMIT_SLACK_PCT = 0.5
NEW_LISTING_WINDOW_DAYS = 10

# Classification labels
LIMIT_OK = "LIMIT_OK"
SPECIAL_TRADING_VALIDATED = "SPECIAL_TRADING_VALIDATED"
SPECIAL_TRADING_UNCONFIRMED = "SPECIAL_TRADING_UNCONFIRMED"
TRADING_HALTED = "TRADING_HALTED"


@dataclass(frozen=True)
class MarketState:
    """Security state as reported by a market-data source (e.g. Daum stockState)."""
    source: str
    as_of: date | None
    pre_delisting_trading: bool = False      # 정리매매
    trading_suspended: bool = False          # 거래정지
    delisted: bool = False
    administrative_issue: bool = False       # 관리종목
    par_value_change: str = "NONE"           # 액면분할/병합
    revaluation: str = "NONE"                # 감자/재평가 등 기준가 재산정
    ex_event: str = "NONE"                   # 권리락/배당락
    listing_date: date | None = None
    upper_limit: float | None = None
    lower_limit: float | None = None
    nxt_tradable: bool | None = None         # False => integrated close == KRX close

    @property
    def no_price_limit(self) -> bool:
        # Explicit 0/0 limits on a trading (not suspended) security = no limit applies.
        return (self.upper_limit == 0 and self.lower_limit == 0 and not self.trading_suspended)


@dataclass
class PriceMoveAssessment:
    label: str
    reasons: list[str] = field(default_factory=list)
    note: str = ""


def special_reasons(state: MarketState | None, session_date: date) -> list[str]:
    if state is None:
        return []
    reasons = []
    if state.pre_delisting_trading:
        reasons.append("정리매매")
    if state.delisted:
        reasons.append("상장폐지")
    if state.no_price_limit:
        reasons.append("가격제한폭 미적용")
    if state.listing_date and 0 <= (session_date - state.listing_date).days <= NEW_LISTING_WINDOW_DAYS:
        reasons.append(f"신규/재상장({state.listing_date})")
    if state.par_value_change not in ("", "NONE", None):
        reasons.append(f"액면변경({state.par_value_change})")
    if state.revaluation not in ("", "NONE", None):
        reasons.append(f"기준가 재산정({state.revaluation})")
    if state.ex_event not in ("", "NONE", None):
        reasons.append(f"권리/배당락({state.ex_event})")
    return reasons


def assess_kr_move(previous_close: float, close: float, base_price: float | None, state: MarketState | None,
                   session_date: date) -> PriceMoveAssessment:
    """Decide whether a KOSPI close is consistent with the applicable price-limit rule."""
    reference = base_price or previous_close
    move_pct = (close / reference - 1) * 100
    state_for_day = state if state and state.as_of == session_date else None
    if state_for_day and state_for_day.upper_limit and state_for_day.lower_limit:
        within = state_for_day.lower_limit <= close <= state_for_day.upper_limit
    else:
        within = abs(move_pct) <= NORMAL_LIMIT_PCT + LIMIT_SLACK_PCT
    if within:
        note = ""
        if base_price and base_price != previous_close:
            note = f"KRX 기준가 {base_price:,.0f} ≠ 전일종가 {previous_close:,.0f}: 권리락·분할 등 기준가 조정"
        return PriceMoveAssessment(LIMIT_OK, note=note)
    reasons = special_reasons(state, session_date)
    if reasons:
        as_of = f" ({state.source} {state.as_of} 기준)" if state and state.as_of else ""
        return PriceMoveAssessment(
            SPECIAL_TRADING_VALIDATED, reasons,
            f"특수거래 확인: {', '.join(reasons)}{as_of} → 일반 ±30% 가격제한폭 검증 제외 (기준가 대비 {move_pct:+.2f}%)")
    return PriceMoveAssessment(
        SPECIAL_TRADING_UNCONFIRMED, [],
        f"기준가 대비 {move_pct:+.2f}%로 일반 가격제한폭 초과, 특수거래 상태 확인 불가 — 수동 확인 필요")
