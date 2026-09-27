from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import json
from datetime import date, datetime

import pandas as pd

from config import SETTINGS
from detector import is_drop
from kospi import download_kospi_market_data, get_kospi_session_context, load_kospi_constituents
from market_calendar import KST, SessionContext, get_session_context
from market_data import download_market_data, verify_candidates
from market_result import MarketResult
from news import fetch_news
from notifier import send_gmail_email
from report import build_combined_subject, build_html_report, build_plain_text_report
from sp500 import load_constituents
from runtime import execute_market
from notifier import NotificationError

LOGGER = logging.getLogger("drop_monitor")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S&P 500 and KOSPI daily drop monitor")
    parser.add_argument("--dry-run", action="store_true", help="Do not send email")
    parser.add_argument("--notify", action="store_true", help="Send one combined Gmail notification")
    parser.add_argument(
        "--session-date-us",
        "--session-date",
        dest="session_date_us",
        type=date.fromisoformat,
        help="Optional US session date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--session-date-kr",
        type=date.fromisoformat,
        help="Optional KOSPI session date (YYYY-MM-DD)",
    )
    return parser.parse_args()


def _context_for_override(session_date: date) -> SessionContext:
    from datetime import timedelta

    import pandas_market_calendars as mcal

    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=session_date - timedelta(days=14), end_date=session_date
    )
    keys = list(schedule.index.date)
    if session_date not in keys:
        raise ValueError(f"{session_date} is not an NYSE trading session")
    position = keys.index(session_date)
    if position == 0:
        raise ValueError(f"previous NYSE session for {session_date} is unavailable")
    from datetime import timezone
    from market_calendar import DATA_READY_DELAY
    if schedule.iloc[position]["market_close"].to_pydatetime() + DATA_READY_DELAY > datetime.now(timezone.utc):
        raise ValueError("US session is not completed/settled")
    return SessionContext(session_date, session_date, keys[position - 1], False)


def _status(total_count: int, analyzed_count: int, missing_count: int) -> str:
    if total_count < 0 or analyzed_count < 0 or analyzed_count > total_count:
        raise ValueError("invalid coverage counts")
    if analyzed_count == 0:
        return "FAILED"
    if total_count <= 0 or analyzed_count / total_count < SETTINGS.minimum_coverage_ratio:
        return "DATA_INCOMPLETE"
    if missing_count or analyzed_count < total_count:
        return "PARTIAL"
    return "OK"


def run_us_monitor(session_date: date | None) -> MarketResult:
    LOGGER.info("[US] market session 확인")
    context = _context_for_override(session_date) if session_date else get_session_context()
    LOGGER.info(
        "[US] 기준 거래일 %s / 직전 거래일 %s",
        context.session_date,
        context.previous_session_date,
    )
    constituents = load_constituents()
    LOGGER.info("[US] 대상 종목: %d", len(constituents))
    prices, missing = download_market_data(
        constituents,
        context.session_date,
        context.previous_session_date,
    )
    name_by_ticker = constituents.set_index("ticker")["company_name"].to_dict()
    for ticker, reason in sorted(missing.items())[:10]:
        base_ticker = ticker.split(" ", 1)[0]
        LOGGER.warning("[US] 누락 %s %s: %s", ticker, name_by_ticker.get(base_ticker, ""), reason)

    raw_candidates = prices.loc[prices["change_pct"].map(is_drop)].copy() if not prices.empty else prices
    candidates, rejected = verify_candidates(
        raw_candidates,
        context.session_date,
        context.previous_session_date,
    )
    for ticker, reason in rejected.items():
        missing[f"{ticker} (candidate verification)"] = reason
        LOGGER.warning("[US] 후보 제외 %s: %s", ticker, reason)
    if not candidates.empty:
        news_results = [
            fetch_news(row["ticker"], row["company_name"], context.session_date)
            for _, row in candidates.iterrows()
        ]
        candidates["news_cause"] = [item["cause"] for item in news_results]
        candidates["news_items"] = [item["items"] for item in news_results]
        candidates["news_error"] = [item["error"] for item in news_results]
        candidates = candidates.sort_values("change_pct").reset_index(drop=True)

    analyzed_count = len(prices) - len(rejected)
    status = _status(len(constituents), analyzed_count, len(missing))
    source_warning = constituents.attrs.get("warning")
    if "verification_status" in candidates:
        unverified = int((candidates["verification_status"] == "primary_daily_close_only").sum())
        if unverified:
            source_warning = "; ".join(filter(None, [source_warning, f"후보 {unverified}개: 2차 검증 실패, 1차 정규장 일봉만 확인"]))
    if source_warning and status == "OK":
        status = "PARTIAL"
    LOGGER.info("[US] Drop threshold: -10.00%")
    LOGGER.info(
        "[US] status=%s collected=%d/%d coverage=%.1f%% detected=%d",
        status,
        analyzed_count,
        len(constituents),
        (analyzed_count / len(constituents) * 100) if len(constituents) else 0,
        len(candidates),
    )
    return MarketResult(
        market="US",
        title="S&P 500 급락 모니터",
        threshold_pct=-10.0,
        code_column="ticker",
        currency="USD",
        status=status,
        error=source_warning,
        session_date=context.session_date,
        previous_session_date=context.previous_session_date,
        total_count=len(constituents),
        analyzed_count=analyzed_count,
        candidates=candidates,
        missing=missing,
    )


def run_kr_monitor(session_date: date | None) -> MarketResult:
    LOGGER.info("[KR] market session 확인")
    context = get_kospi_session_context(session_date)
    LOGGER.info(
        "[KR] 기준 거래일 %s / 직전 거래일 %s",
        context.session_date,
        context.previous_session_date,
    )
    constituents = load_kospi_constituents()
    LOGGER.info("[KR] 대상 종목: %d", len(constituents))
    prices, missing_raw = download_kospi_market_data(constituents, context)
    name_by_code = constituents.set_index("code")["company_name"].to_dict()
    missing = {
        f"{code} {name_by_code.get(code, '')}".strip(): reason
        for code, reason in missing_raw.items()
    }
    for code_name, reason in sorted(missing.items())[:10]:
        LOGGER.warning("[KR] 누락 %s: %s", code_name, reason)
    candidates = (
        prices.loc[prices["change_pct"].map(lambda value: is_drop(value, -7.0))]
        .sort_values("change_pct")
        .reset_index(drop=True)
        if not prices.empty
        else prices
    )
    analyzed_count = len(prices)
    status = _status(len(constituents), analyzed_count, len(missing))
    if context.warning and status == "OK":
        status = "PARTIAL"
    LOGGER.info("[KR] Drop threshold: -7.00%")
    LOGGER.info(
        "[KR] status=%s collected=%d/%d coverage=%.1f%% detected=%d",
        status,
        analyzed_count,
        len(constituents),
        (analyzed_count / len(constituents) * 100) if len(constituents) else 0,
        len(candidates),
    )
    return MarketResult(
        market="KR",
        title="KOSPI 급락 모니터",
        threshold_pct=-7.0,
        code_column="code",
        currency="KRW",
        status=status,
        error=context.warning,
        session_date=context.session_date,
        previous_session_date=context.previous_session_date,
        total_count=len(constituents),
        analyzed_count=analyzed_count,
        candidates=candidates,
        missing=missing,
    )


def _failed_result(market: str, title: str, threshold: float, code_column: str, currency: str, exc: Exception) -> MarketResult:
    LOGGER.exception("[%s] monitor failed", market)
    return MarketResult(
        market=market,
        title=title,
        threshold_pct=threshold,
        code_column=code_column,
        currency=currency,
        status="FAILED",
        error=f"{type(exc).__name__}: {exc}",
    )


def _write_outputs(
    us: MarketResult,
    kr: MarketResult,
    plain_text: str,
    html: str,
    executed_at_kst: datetime,
) -> None:
    SETTINGS.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = executed_at_kst.strftime("%Y-%m-%d-%H%M%S")
    payload = {"executed_at_kst": executed_at_kst.isoformat(), "markets": []}
    for result in (us, kr):
        payload["markets"].append({
            "market": result.market, "status": result.status,
            "session_date": result.session_date, "previous_session_date": result.previous_session_date,
            "total": result.total_count, "valid": result.analyzed_count, "coverage": result.coverage,
            "threshold": result.threshold_pct, "detected": len(result.candidates),
            "missing": result.missing, "error": result.error,
            "candidates": result.candidates.to_dict(orient="records"),
        })
    (SETTINGS.output_dir / f"combined-{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (SETTINGS.output_dir / f"combined-{stamp}.md").write_text(plain_text, encoding="utf-8")
    (SETTINGS.output_dir / f"combined-{stamp}.html").write_text(html, encoding="utf-8")
    us.candidates.to_csv(SETTINGS.output_dir / f"sp500-{us.session_date or stamp}.csv", index=False, encoding="utf-8-sig")
    kr.candidates.to_csv(SETTINGS.output_dir / f"kospi-{kr.session_date or stamp}.csv", index=False, encoding="utf-8-sig")


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    executed_at_kst = datetime.now(tz=KST)
    session_date_us = getattr(args, "session_date_us", getattr(args, "session_date", None))
    session_date_kr = getattr(args, "session_date_kr", None)
    try:
        us = execute_market(run_us_monitor, session_date_us)
    except Exception as exc:
        us = _failed_result("US", "S&P 500 급락 모니터", -10.0, "ticker", "USD", exc)
    try:
        kr = execute_market(run_kr_monitor, session_date_kr)
    except Exception as exc:
        kr = _failed_result("KR", "KOSPI 급락 모니터", -7.0, "code", "KRW", exc)

    plain_text = build_plain_text_report(us, kr, executed_at_kst)
    html = build_html_report(us, kr, executed_at_kst)
    _write_outputs(us, kr, plain_text, html, executed_at_kst)
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    subject = build_combined_subject(us, kr, "[TEST] " if is_manual else "")
    if args.notify and not args.dry_run:
        try:
            send_gmail_email(plain_text, subject, html)
        except Exception as exc:
            raise NotificationError("SMTP 발송 실패/수락 여부 불명: 중복 발송 방지를 위해 자동 재발송하지 않음") from exc
        LOGGER.info("Gmail 전송: 완료")
    else:
        LOGGER.info("dry-run/알림 비활성: 이메일 전송 안 함")
    LOGGER.info("Total elapsed=%.2fs", time.monotonic() - started)
    return 1 if any(item.status in {"DATA_INCOMPLETE", "FAILED"} for item in (us, kr)) else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = _parse_args()
    try:
        return run(args)
    except NotificationError:
        LOGGER.exception("Gmail notification failed")
        return 1
    except Exception as exc:
        LOGGER.exception("combined monitor failed")
        if args.notify and not args.dry_run:
            try:
                prefix = "[TEST] " if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" else ""
                send_gmail_email(
                    "US + KOSPI 급락 모니터 실행이 실패했습니다.\n\n"
                    f"오류: {type(exc).__name__}: {exc}\n",
                    f"{prefix}[급락 모니터][DATA WARNING] 실행 실패",
                )
                LOGGER.info("실패 알림 Gmail 전송: 완료")
            except Exception:
                LOGGER.exception("실패 알림 Gmail 전송도 실패")
        return 1


if __name__ == "__main__":
    sys.exit(main())
