"""Exchange-designated states (정리매매, 관리종목, ...) are shown next to a detected stock.

Found in the 2026-10-09 sample mail: 부산주공(005030) and 한창(005110), both in 정리매매
(no price limit, delisting ahead), and 관리종목 SG글로벌 were listed like ordinary crashes.
The label is display-only: detection, severity and the market status stay untouched."""
from datetime import date, datetime, timedelta

import pandas as pd

from helpers import fixture
from market_calendar import KST
from market_result import NORMAL, MarketResult
from providers import daum
from report import build_combined_subject, build_html_report, build_plain_text_report
from special_trading import MarketState, state_tags

DAY, NEXT = date(2026, 10, 8), date(2026, 10, 12)


def state(**changes) -> MarketState:
    values = {"source": "Daum", "as_of": DAY}
    values.update(changes)
    return MarketState(**values)


def test_labels_follow_the_reported_state():
    assert state_tags(None, DAY) == []
    assert state_tags(state(), DAY) == []
    assert state_tags(state(pre_delisting_trading=True, administrative_issue=True), DAY) == ["정리매매"]
    assert state_tags(state(administrative_issue=True), DAY) == ["관리종목"]
    assert state_tags(state(trading_suspended=True), DAY) == ["거래정지"]
    assert state_tags(state(delisted=True, pre_delisting_trading=True), DAY) == ["상장폐지", "정리매매"]
    assert state_tags(state(listing_date=DAY - timedelta(days=3)), DAY) == ["신규상장"]
    assert state_tags(state(listing_date=DAY - timedelta(days=11)), DAY) == []


def test_snapshot_must_belong_to_the_analysis_day():
    flagged = {"pre_delisting_trading": True}
    assert state_tags(state(**flagged), DAY, NEXT) == ["정리매매"]
    assert state_tags(state(as_of=NEXT, **flagged), DAY, NEXT) == ["정리매매"]       # snapshot already rolled over
    assert state_tags(state(as_of=date(2026, 10, 20), **flagged), DAY, NEXT) == []   # today's state, old replay date
    assert state_tags(state(as_of=None, **flagged), DAY) == []
    assert state_tags(state(as_of=None, **flagged), DAY, None) == []                  # unknown never matches unknown


def test_real_daum_payload_of_a_정리매매_stock():
    payload = fixture("daum-quote-005030-2026-09-30.json")
    assert state_tags(daum.parse_state(payload), date(2026, 9, 29), date(2026, 9, 30)) == ["정리매매"]


def candidates(count: int, tags: dict[int, object]) -> pd.DataFrame:
    rows = []
    for i in range(count):
        rows.append({"code": f"{5000 + i:06d}", "company_name": f"종목{i}", "previous_close": 10000.0 + i,
                     "close": 9000.0 + i, "change_pct": -10.0 - i / 10, "source": "Yahoo", "note": "",
                     "validation_status": "CROSS_VALIDATED", "special_label": "LIMIT_OK",
                     "state_tags": tags.get(i, "")})
    return pd.DataFrame(rows)


def kr_result(frame: pd.DataFrame) -> MarketResult:
    return MarketResult("KR", "KOSPI 급락 모니터", -7, "code", "KRW", status=NORMAL, total_count=900, analyzed_count=900,
                        candidates=frame, session_date=DAY, previous_session_date=DAY - timedelta(days=1))


def test_mail_shows_the_labels_without_changing_the_outcome():
    # top rows, a lean tail row, a blank value and a NaN (column from an older source) all render safely
    frame = candidates(14, {0: "정리매매", 1: "관리종목", 12: "정리매매", 2: float("nan")})
    kr = kr_result(frame)
    us = MarketResult("US", "S&P 500 급락 모니터", -10, "ticker", "USD", status=NORMAL, total_count=500, analyzed_count=500,
                      session_date=DAY, previous_session_date=DAY - timedelta(days=1))
    html = build_html_report(us, kr, datetime(2026, 10, 9, 7, 30, tzinfo=KST))
    plain = build_plain_text_report(us, kr, datetime(2026, 10, 9, 7, 30, tzinfo=KST))
    assert html.count(">정리매매</span>") == 2 and html.count(">관리종목</span>") == 1
    assert "종목0 [정리매매]" in plain and "종목1 [관리종목]" in plain and "종목12 [정리매매]" in plain
    assert "종목2 [" not in plain and "nan" not in plain.lower()
    assert html.count("<table") == html.count("</table>") and "DATA WARNING" not in html
    assert kr.status == NORMAL and len(kr.candidates) == 14
    assert build_combined_subject(us, kr) == "[급락 모니터] S&P500 0개 · KOSPI 14개"   # counts are not altered


def test_frames_without_the_column_still_render():
    frame = candidates(3, {}).drop(columns=["state_tags"])
    html = build_html_report(kr_result(frame), kr_result(frame), datetime(2026, 10, 9, 7, 30, tzinfo=KST))
    assert "종목0" in html and "배지" in html  # the explanatory footnote is static text
