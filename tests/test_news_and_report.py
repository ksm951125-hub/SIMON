from datetime import date

import pandas as pd

from news import fetch_news
from report import render_markdown


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
