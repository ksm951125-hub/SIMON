from datetime import date, datetime

import pandas as pd

from news import fetch_news
from market_calendar import KST
from market_result import MarketResult
from report import build_combined_subject, render_combined_markdown, render_markdown


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
        code_column="ticker", currency="USD", status="OK",
        session_date=date(2026, 9, 24), previous_session_date=date(2026, 9, 23),
        total_count=503, analyzed_count=503,
    )
    kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="OK",
        session_date=date(2026, 9, 23), previous_session_date=date(2026, 9, 22),
        total_count=944, analyzed_count=944,
    )

    markdown = render_combined_markdown(us, kr, datetime(2026, 9, 25, 8, tzinfo=KST))
    subject = build_combined_subject(us, kr)

    assert "S&P 500 급락 모니터" in markdown
    assert "KOSPI 급락 모니터" in markdown
    assert markdown.count("기준 이하 급락 종목 없음") == 2
    assert subject == "[급락 모니터] S&P500 0개 / KOSPI 0개"


def test_combined_data_warning_does_not_claim_zero():
    us = MarketResult(
        market="US", title="S&P 500 급락 모니터", threshold_pct=-10.0,
        code_column="ticker", currency="USD", status="OK", total_count=503, analyzed_count=503,
    )
    kr = MarketResult(
        market="KR", title="KOSPI 급락 모니터", threshold_pct=-7.0,
        code_column="code", currency="KRW", status="DATA_INCOMPLETE",
        total_count=944, analyzed_count=100, error="simulated outage",
    )

    markdown = render_combined_markdown(us, kr, datetime(2026, 9, 25, 8, tzinfo=KST))
    subject = build_combined_subject(us, kr)

    assert "탐지: 확인 필요 (데이터 불완전)" in markdown
    assert "[DATA WARNING]" in subject
    assert "KOSPI 확인필요" in subject
