from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

import pandas as pd

from config import SETTINGS
from detector import is_drop
from market_calendar import KST, SessionContext, get_session_context
from market_data import download_market_data, verify_candidates
from news import fetch_news
from notifier import send_gmail_email
from report import render_markdown, write_reports
from sp500 import load_constituents

LOGGER = logging.getLogger("sp500_monitor")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S&P 500 일일 10% 급락 모니터")
    parser.add_argument("--dry-run", action="store_true", help="이메일 알림을 보내지 않습니다")
    parser.add_argument("--notify", action="store_true", help="Gmail Secret이 있으면 이메일 알림을 보냅니다")
    parser.add_argument("--session-date", type=date.fromisoformat, help="테스트용 미국 거래일(YYYY-MM-DD)")
    return parser.parse_args()


def _context_for_override(session_date: date) -> SessionContext:
    from datetime import timedelta
    import pandas_market_calendars as mcal

    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=session_date - timedelta(days=14), end_date=session_date
    )
    keys = list(schedule.index.date)
    if session_date not in keys:
        return SessionContext(session_date, None, None, True, "미국 증시 휴장 - 분석 대상 없음")
    position = keys.index(session_date)
    return SessionContext(session_date, session_date, keys[position - 1], False)


def run(args: argparse.Namespace) -> int:
    LOGGER.info("[1/6] US market session 확인")
    context = _context_for_override(args.session_date) if args.session_date else get_session_context()
    report_date = context.session_date or context.expected_date
    if context.is_holiday:
        markdown = render_markdown(report_date, 0, pd.DataFrame(), {}, context.reason)
        paths = write_reports(SETTINGS.output_dir, report_date, 0, pd.DataFrame(), {}, markdown, context.reason)
        LOGGER.info(context.reason)
        LOGGER.info("결과 파일: %s", ", ".join(str(path) for path in paths))
        if args.notify and not args.dry_run:
            send_gmail_email(markdown, f"[S&P 500 급락 모니터링] {report_date.isoformat()} 휴장")
        print(markdown)
        return 0

    LOGGER.info("기준 거래일 %s / 직전 거래일 %s", context.session_date, context.previous_session_date)
    LOGGER.info("[2/6] S&P 500 constituents 로딩")
    constituents = load_constituents()
    LOGGER.info("구성종목 수: %d", len(constituents))

    LOGGER.info("[3/6] 가격 데이터 다운로드")
    prices, missing = download_market_data(
        constituents, context.session_date, context.previous_session_date
    )
    LOGGER.info("정상 수집: %d / 누락: %d", len(prices), len(missing))
    for ticker, reason in sorted(missing.items()):
        LOGGER.warning("누락 %s: %s", ticker, reason)
    if prices.empty:
        raise RuntimeError("유효한 가격 데이터를 한 종목도 수집하지 못했습니다")
    coverage = len(prices) / len(constituents)
    if coverage < SETTINGS.minimum_coverage_ratio:
        raise RuntimeError(
            f"가격 데이터 커버리지 부족: {len(prices)}/{len(constituents)} "
            f"({coverage:.1%}, 최소 {SETTINGS.minimum_coverage_ratio:.0%})"
        )

    LOGGER.info("[4/6] 데이터 검증")
    raw_candidates = prices.loc[prices["change_pct"].map(is_drop)].copy()
    candidates, rejected = verify_candidates(
        raw_candidates, context.session_date, context.previous_session_date
    )
    for ticker, reason in rejected.items():
        LOGGER.warning("후보 제외 %s: %s", ticker, reason)
        missing[f"{ticker} (candidate verification)"] = reason

    LOGGER.info("[5/6] -10%% 종목 탐지: %d개", len(candidates))
    if not candidates.empty:
        news_results = [
            fetch_news(row["ticker"], row["company_name"], context.session_date)
            for _, row in candidates.iterrows()
        ]
        candidates["news_cause"] = [item["cause"] for item in news_results]
        candidates["news_items"] = [item["items"] for item in news_results]
        candidates["news_error"] = [item["error"] for item in news_results]
        candidates = candidates.sort_values("change_pct")

    markdown = render_markdown(context.session_date, len(prices), candidates, missing)
    paths = write_reports(
        SETTINGS.output_dir, context.session_date, len(prices), candidates, missing, markdown
    )
    LOGGER.info("[6/6] 결과 저장 및 전송")
    LOGGER.info("결과 파일: %s", ", ".join(str(path) for path in paths))
    if args.notify and not args.dry_run:
        subject = f"[S&P 500 급락 모니터링] {context.session_date.isoformat()} - {len(candidates)}개"
        sent = send_gmail_email(markdown, subject)
        LOGGER.info("Gmail 전송: %s", "완료" if sent else "건너뜀")
    else:
        LOGGER.info("dry-run/알림 비활성: 이메일 전송 안 함")
    print(markdown)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        return run(_parse_args())
    except Exception:
        LOGGER.exception("모니터 실행 실패")
        return 1


if __name__ == "__main__":
    sys.exit(main())
