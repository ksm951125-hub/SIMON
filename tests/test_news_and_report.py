from datetime import date, datetime

import pandas as pd

from news import fetch_news
from market_calendar import KST
from market_result import Issue, MarketResult
from report import (
    build_combined_subject,
    build_html_report,
    render_combined_markdown,
    render_markdown,
)


def test_news_failure_does_not_block_report(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr("news.requests.get", fail)
    result = fetch_news("ABC", "ABC Corp")
    assert result["items"] == []
    assert result["error"]

    frame = pd.DataFrame(
        [
            {
                "ticker": "ABC",
                "company_name": "ABC Corp",
                "sector": "Industrials",
                "previous_close": 100.0,
                "close": 89.0,
                "change_pct": -11.0,
                "volume": 2000.0,
                "average_volume_20d": 1000.0,
                "volume_change_pct": 100.0,
                "low": 88.0,
                "open_to_close_pct": -6.0,
                "news_cause": result["cause"],
                "news_items": result["items"],
            }
        ]
    )
    markdown = render_markdown(date(2025, 1, 3), 503, frame, {})
    assert "ABC Corp" in markdown
    assert "명확한 급락 원인 확인되지 않음" in markdown


def test_zero_drop_report_is_explicit():
    markdown = render_markdown(
        date(2025, 1, 3),
        503,
        pd.DataFrame(),
        {},
        previous_session_date=date(2025, 1, 2),
    )

    assert "10% 이상 하락 종목: 0개" in markdown
    assert "10% 이상 하락 종목 없음" in markdown
    assert "이전 거래일: 2025-01-02" in markdown


def test_combined_zero_drop_email_is_explicit():
    us = MarketResult(
        market="US", title="S&P 500 급락 모니터", threshold_pct=-10.0,
        code_column="ticker", currency="USD", status="NORMAL",
        session_date=date(2026, 9, 24), previous_session_date=date(2026, 9, 23),
        total_count=503, analyzed_count=503,
    )
    kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="NORMAL",
        session_date=date(2026, 9, 23), previous_session_date=date(2026, 9, 22),
        total_count=944, analyzed_count=944,
    )

    markdown = render_combined_markdown(us, kr, datetime(2026, 9, 25, 8, tzinfo=KST))
    subject = build_combined_subject(us, kr)

    assert "S&P 500 급락 모니터" in markdown
    assert "KOSPI 급락 모니터" in markdown
    assert markdown.count("기준 이하 급락 종목 없음") == 2
    assert subject == "[급락 모니터] S&P500 0개 · KOSPI 0개"


def test_combined_data_warning_does_not_claim_zero():
    us = MarketResult(
        market="US", title="S&P 500 급락 모니터", threshold_pct=-10.0,
        code_column="ticker", currency="USD", status="NORMAL", total_count=503, analyzed_count=503,
    )
    kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="ERROR",
        total_count=944, analyzed_count=100, error="simulated outage",
    )

    markdown = render_combined_markdown(us, kr, datetime(2026, 9, 25, 8, tzinfo=KST))
    subject = build_combined_subject(us, kr)

    assert "Detected: 확인 필요" in markdown
    assert "DATA ERROR" in markdown
    assert "[ERROR KR]" in subject
    assert "KOSPI 확인필요" in subject


def _candidates(market: str, count: int) -> pd.DataFrame:
    code_column = "ticker" if market == "US" else "code"
    return pd.DataFrame(
        [
            {
                code_column: f"LONG{i:03d}" if i == count - 1 else f"{i:06d}",
                "company_name": (
                    "매우 긴 한글 회사명 주식회사 글로벌 테스트 홀딩스"
                    if i == count - 1
                    else f"테스트회사{i}"
                ),
                "previous_close": 10000 + i,
                "close": 8000 + i,
                "change_pct": -20.0 + i / 10,
            }
            for i in range(count)
        ]
    )


def test_html_report_candidates_warning_and_long_korean_name():
    us = MarketResult(
        market="US", title="S&P 500 급락 모니터", threshold_pct=-10.0,
        code_column="ticker", currency="USD", status="NORMAL",
        session_date=date(2026, 9, 24), previous_session_date=date(2026, 9, 23),
        total_count=503, analyzed_count=503, candidates=_candidates("US", 3),
    )
    kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="WARNING",
        session_date=date(2026, 6, 5), previous_session_date=date(2026, 6, 4),
        total_count=944, analyzed_count=942, candidates=_candidates("KR", 37),
        missing={
            "0220W0 한화머시너리앤서비스홀딩스": "required sessions missing (2026-06-04, 2026-06-05)",
            "0220WL 한화머시너리앤서비스홀딩스3우B": "required sessions missing (2026-06-04, 2026-06-05)",
        },
        issues=[Issue("WARNING", "MISSING", "required sessions missing", "0220W0", "한화머시너리앤서비스홀딩스"),
                Issue("WARNING", "MISSING", "required sessions missing", "0220WL", "한화머시너리앤서비스홀딩스3우B")],
    )

    html = build_html_report(us, kr, datetime(2026, 9, 25, 8, tzinfo=KST))

    assert "multipart" not in html
    assert "&#9888; WARNING" in html
    assert "DATA WARNING · 2건" in html and "데이터 누락 2건" in html
    assert "외 27개 종목 (전체 목록)" in html
    assert html.count("&#9660;&nbsp;") == 40
    assert "매우 긴 한글 회사명 주식회사 글로벌 테스트 홀딩스" in html
    assert "@media only screen and (max-width:600px)" in html
    assert "max-width:760px" in html
    assert "javascript" not in html.lower()
    assert build_combined_subject(us, kr, "[TEST] ") == (
        "[TEST][급락 모니터][DATA WARNING] S&P500 3개 · KOSPI 37개 | 데이터 누락 US 0건 / KR 2건"
    )


def test_html_report_zero_candidates_has_no_warning_card():
    us = MarketResult(
        market="US", title="S&P 500 급락 모니터", threshold_pct=-10.0,
        code_column="ticker", currency="USD", status="NORMAL", total_count=503, analyzed_count=503,
    )
    kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="NORMAL", total_count=944, analyzed_count=944,
    )

    html = build_html_report(us, kr, datetime(2026, 9, 25, 8, tzinfo=KST))

    assert html.count("기준 이하 급락 종목 없음") == 2
    assert "DATA WARNING" not in html
