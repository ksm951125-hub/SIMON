from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from typing import Any

import requests

from config import SETTINGS, Settings

LOGGER = logging.getLogger(__name__)

CATEGORIES = {
    "실적 쇼크": ("earnings miss", "misses estimates", "profit warning", "earnings plunge"),
    "가이던스 하향": ("cuts guidance", "lowers outlook", "forecast cut", "weak outlook"),
    "FDA 결정": ("fda", "clinical trial", "drug trial"),
    "CEO 사임": ("ceo resign", "ceo steps down", "chief executive resign"),
    "소송": ("lawsuit", "litigation", "court ruling"),
    "규제": ("regulator", "antitrust", "investigation", "probe"),
    "인수합병 관련": ("acquisition", "merger", "takeover", "deal collapses"),
    "애널리스트 하향": ("downgrade", "price target cut"),
    "제품 문제": ("recall", "product defect", "safety issue"),
}


def infer_cause(items: list[dict[str, Any]]) -> str:
    joined = " ".join(str(item.get("title", "")).lower() for item in items)
    for category, keywords in CATEGORIES.items():
        if any(keyword in joined for keyword in keywords):
            return f"관련 보도 제목에서 '{category}' 가능성이 확인됨 (기사 원문 확인 필요)"
    return "명확한 급락 원인 확인되지 않음"


NO_NEWS = {"cause": "명확한 급락 원인 확인되지 않음", "items": [], "error": None}


def fetch_news_many(targets: list[tuple[str, str]], session_date: date | None = None,
                    settings: Settings = SETTINGS, budget_seconds: float | None = None,
                    fetch=None, workers: int = 6) -> list[dict[str, Any]]:
    """News lookup for several symbols in parallel with a hard total time budget.

    News is enrichment only: whatever is not finished within the budget gets the
    neutral default, so a slow provider can never delay or fail the report."""
    fetch = fetch or fetch_news
    budget = settings.news_budget_seconds if budget_seconds is None else budget_seconds
    results = [{**NO_NEWS, "error": "시간 초과로 뉴스 조회 생략"} for _ in targets]
    if not targets:
        return results
    executor = ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets))))
    futures = {executor.submit(fetch, ticker, name, session_date, settings): index
               for index, (ticker, name) in enumerate(targets)}
    done, pending = wait(futures, timeout=budget)
    for future in done:
        try:
            results[futures[future]] = future.result()
        except Exception as exc:  # noqa: BLE001 - news must never raise
            results[futures[future]] = {**NO_NEWS, "error": f"{type(exc).__name__}: {exc}"}
    if pending:
        LOGGER.warning("뉴스 조회 %d/%d건이 %.0f초 예산 안에 끝나지 않아 생략", len(pending), len(targets), budget)
    executor.shutdown(wait=False, cancel_futures=True)
    return results


def fetch_news(
    ticker: str,
    company_name: str,
    session_date: date | None = None,
    settings: Settings = SETTINGS,
) -> dict[str, Any]:
    try:
        response = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": f"{ticker} {company_name}", "quotesCount": 0, "newsCount": 5},
            headers={"User-Agent": settings.user_agent},
            timeout=(3, 6),
        )
        response.raise_for_status()
        items = []
        for item in response.json().get("news", [])[:3]:
            published = item.get("providerPublishTime")
            published_dt = (
                datetime.fromtimestamp(published, tz=timezone.utc)
                if isinstance(published, (int, float))
                else None
            )
            # Avoid attaching unrelated current headlines to a historical run.
            if session_date and published_dt and abs((published_dt.date() - session_date).days) > 3:
                continue
            published_at = published_dt.isoformat() if published_dt else None
            items.append(
                {
                    "title": item.get("title") or "제목 없음",
                    "source": item.get("publisher") or "출처 미상",
                    "url": item.get("link") or "",
                    "published_at": published_at,
                }
            )
        return {"cause": infer_cause(items), "items": items, "error": None}
    except Exception as exc:
        LOGGER.warning("%s 뉴스 조회 실패: %s", ticker, exc)
        return {"cause": "명확한 급락 원인 확인되지 않음", "items": [], "error": str(exc)}
