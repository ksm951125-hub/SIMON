from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from config import SETTINGS
from kis_validation import optional_validator
from kospi import download_kospi_market_data, get_kospi_session_context, load_kospi_constituents
from market_calendar import DATA_READY_DELAY, KST, SessionContext, get_session_context
from market_data import download_market_data
from market_result import (ERROR, INFO, SKIPPED, WARNING, Issue, MarketResult, classify_status,
                           issues_from_prices)
from news import fetch_news
from notifier import NotificationError, send_gmail_email
from report import build_combined_subject, build_html_report, build_plain_text_report
from runtime import execute_market
from sp500 import load_constituents

LOGGER = logging.getLogger("drop_monitor")
REPORT_SCHEMA = "regular-close-v4"
US_TITLE, KR_TITLE = "S&P 500 급락 모니터", "KOSPI 급락 모니터"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S&P 500 and KOSPI daily drop monitor")
    parser.add_argument("--dry-run", action="store_true", help="Build reports but never send email")
    parser.add_argument("--notify", action="store_true", help="Send one combined Gmail notification")
    parser.add_argument("--market", choices=("all", "us", "kr"), default="all",
                        help="Limit the run to one market (dry-run/debugging)")
    parser.add_argument("--session-date-us", "--session-date", dest="session_date_us", type=date.fromisoformat,
                        help="Optional US session date (YYYY-MM-DD)")
    parser.add_argument("--session-date-kr", type=date.fromisoformat, help="Optional KOSPI session date (YYYY-MM-DD)")
    return parser.parse_args(argv)


def _context_for_override(session_date: date) -> SessionContext:
    import pandas_market_calendars as mcal

    schedule = mcal.get_calendar("NYSE").schedule(start_date=session_date - timedelta(days=14), end_date=session_date)
    keys = list(schedule.index.date)
    if session_date not in keys:
        raise ValueError(f"{session_date} is not an NYSE trading session")
    position = keys.index(session_date)
    if position == 0:
        raise ValueError(f"previous NYSE session for {session_date} is unavailable")
    if schedule.iloc[position]["market_close"].to_pydatetime() + DATA_READY_DELAY > datetime.now(timezone.utc):
        raise ValueError("US session is not completed/settled")
    return SessionContext(session_date, session_date, keys[position - 1], False)


def _log_rows(market: str, label: str, frame: pd.DataFrame, code_column: str) -> None:
    for _, row in frame.iterrows():
        LOGGER.info(
            "[%s] %s %s | %s | prev=%s close=%s raw_change=%.6f%% | data_source=%s | validation=%s%s",
            market, label, row[code_column], row["company_name"], row["previous_close"], row["close"],
            row["change_pct"], row["source"], row.get("validation") or "-",
            f" | note={row['note']}" if row.get("note") else "",
        )


def _log_summary(result: MarketResult, candidate_count: int) -> None:
    statuses = (result.prices["validation_status"].value_counts().to_dict()
                if not result.prices.empty and "validation_status" in result.prices else {})
    LOGGER.info(
        "[%s] SUMMARY analysis_date=%s previous_trading_date=%s listing_count=%d valid_price_count=%d "
        "missing_price_count=%d fallback_count=%d mismatch_count=%d cross_validated=%d candidate_count=%d "
        "final_alert_count=%d coverage=%.2f%% status=%s validation=%s",
        result.market, result.session_date, result.previous_session_date, result.total_count,
        result.analyzed_count, result.missing_count, result.fallback_count, result.mismatch_count,
        result.cross_checked_count, candidate_count, len(result.candidates), result.coverage * 100, result.status,
        statuses,
    )
    for issue in result.issues:
        log = LOGGER.warning if issue.is_data_warning else LOGGER.info
        log("[%s] %s %s %s %s | %s", result.market, issue.level, issue.category, issue.symbol, issue.name, issue.message)
    for note in result.source_notes:
        LOGGER.info("[%s] SOURCE %s", result.market, note)


def _missing_issues(missing: dict[str, str], names: dict[str, str]) -> tuple[dict[str, str], list[Issue]]:
    keyed = {f"{code} {names.get(code, '')}".strip(): reason for code, reason in sorted(missing.items())}
    issues = [Issue(WARNING, "MISSING", reason, code, names.get(code, "")) for code, reason in sorted(missing.items())]
    return keyed, issues


def run_us_monitor(session_date: date | None) -> MarketResult:
    context = _context_for_override(session_date) if session_date else get_session_context()
    LOGGER.info("[US] analysis_date=%s previous_trading_date=%s (%s)", context.session_date,
                context.previous_session_date, context.reason or "예상 거래일")
    constituents = load_constituents()
    LOGGER.info("[US] listing_count=%d (source=%s)", len(constituents), constituents.attrs.get("source", "cache"))
    collection = download_market_data(constituents, context.session_date, context.previous_session_date)
    prices = collection.prices

    names = constituents.set_index("yahoo_ticker")["company_name"].to_dict()
    missing, issues = _missing_issues(collection.missing, names)
    price_issues, price_notes = issues_from_prices(prices, "ticker", SETTINGS.us_validation_band_pct)
    issues += price_issues
    if constituents.attrs.get("warning"):
        issues.append(Issue(WARNING, "LISTING", constituents.attrs["warning"]))
    if context.reason:
        issues.append(Issue(INFO, "CALENDAR", f"미국 휴장/데이터 미확정: {context.reason}"))
    if session_date:
        issues.append(Issue(INFO, "NOTICE", "수동 지정일 분석: 현재 S&P500 구성종목 기준(당시 편입 목록 아님)"))

    band = prices.loc[prices["change_pct"] <= SETTINGS.us_validation_band_pct] if not prices.empty else prices
    _log_rows("US", "CANDIDATE", band.sort_values("change_pct") if not band.empty else band, "ticker")
    candidates = prices.loc[prices["detected"]].copy() if not prices.empty else pd.DataFrame()
    if not candidates.empty:
        news = [fetch_news(row["ticker"], row["company_name"], context.session_date) for _, row in candidates.iterrows()]
        candidates["news_cause"] = [item["cause"] for item in news]
        candidates["news_items"] = [item["items"] for item in news]
        candidates = candidates.sort_values("change_pct").reset_index(drop=True)

    result = MarketResult(
        market="US", title=US_TITLE, threshold_pct=SETTINGS.drop_threshold_pct, code_column="ticker",
        currency="USD", source="Yahoo 정규장 일봉 · 교차검증 Nasdaq/CNBC", prices=prices,
        session_date=context.session_date, previous_session_date=context.previous_session_date,
        total_count=len(constituents), analyzed_count=len(prices), candidates=candidates, missing=missing,
        fallback_count=collection.fallback_count, mismatch_count=collection.mismatch_count,
        cross_checked_count=collection.cross_checked_count, issues=issues,
        source_notes=collection.notices + price_notes,
    )
    result.status = classify_status(result.total_count, result.analyzed_count, issues)
    _log_summary(result, len(band))
    return result


def _optional_krx_client():
    if not os.getenv("KRX_API_KEY", "").strip():
        return None
    from krx import KrxClient

    return KrxClient()


def run_kr_monitor(session_date: date | None) -> MarketResult:
    context = get_kospi_session_context(session_date)
    LOGGER.info("[KR] analysis_date=%s previous_trading_date=%s", context.session_date, context.previous_session_date)
    constituents = load_kospi_constituents()
    LOGGER.info("[KR] listing_count=%d (Naver 전체 %s, loaded %s, unresolved %s)", len(constituents),
                constituents.attrs.get("expected"), constituents.attrs.get("loaded"),
                constituents.attrs.get("unresolved"))
    collection = download_kospi_market_data(constituents, context, krx_client=_optional_krx_client(),
                                            validator=optional_validator())
    prices = collection.prices

    names = dict(zip(constituents["code"], constituents["company_name"]))
    missing, issues = _missing_issues(collection.missing, names)
    price_issues, price_notes = issues_from_prices(prices, "code", SETTINGS.kr_validation_band_pct)
    issues += price_issues
    issues += [Issue(WARNING, "LISTING", warning) for warning in constituents.attrs.get("warnings", [])]
    if context.warning:
        issues.append(Issue(context.warning_level, "CALENDAR", context.warning))

    band = prices.loc[prices["change_pct"] <= SETTINGS.kr_validation_band_pct] if not prices.empty else prices
    _log_rows("KR", "CANDIDATE", band.sort_values("change_pct") if not band.empty else band, "code")
    candidates = (prices.loc[prices["detected"]].sort_values("change_pct").reset_index(drop=True)
                  if not prices.empty else pd.DataFrame())
    result = MarketResult(
        market="KR", title=KR_TITLE, threshold_pct=SETTINGS.kr_drop_threshold_pct, code_column="code",
        currency="KRW",
        source="Yahoo KRX 정규장 종가 · 교차검증 Daum/Naver KRX 기준가" + (" · KRX 공식" if _has_krx() else ""),
        prices=prices, session_date=context.session_date, previous_session_date=context.previous_session_date,
        total_count=len(constituents), analyzed_count=len(prices), candidates=candidates, missing=missing,
        fallback_count=collection.fallback_count, mismatch_count=collection.mismatch_count,
        cross_checked_count=collection.cross_checked_count, issues=issues,
        source_notes=collection.notices + price_notes,
    )
    result.status = classify_status(result.total_count, result.analyzed_count, issues)
    _log_summary(result, len(band))
    return result


def _has_krx() -> bool:
    return bool(os.getenv("KRX_API_KEY", "").strip())


def _failed_result(market: str, exc: Exception) -> MarketResult:
    LOGGER.error("[%s] monitor FAILED: %s: %s", market, type(exc).__name__, exc)
    title, threshold, column, currency = (
        (US_TITLE, SETTINGS.drop_threshold_pct, "ticker", "USD") if market == "US"
        else (KR_TITLE, SETTINGS.kr_drop_threshold_pct, "code", "KRW"))
    return MarketResult(market=market, title=title, threshold_pct=threshold, code_column=column,
                        currency=currency, status=ERROR, error=f"{type(exc).__name__}: {exc}",
                        issues=[Issue(ERROR, "SOURCE", f"{type(exc).__name__}: {exc}")])


def _skipped_result(market: str) -> MarketResult:
    title, threshold, column, currency = (
        (US_TITLE, SETTINGS.drop_threshold_pct, "ticker", "USD") if market == "US"
        else (KR_TITLE, SETTINGS.kr_drop_threshold_pct, "code", "KRW"))
    return MarketResult(market=market, title=title, threshold_pct=threshold, code_column=column,
                        currency=currency, status=SKIPPED, error="이번 실행에서 제외됨 (--market)")


def _records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False)) if not frame.empty else []


def _write_outputs(us: MarketResult, kr: MarketResult, plain_text: str, html: str, executed_at_kst: datetime) -> None:
    SETTINGS.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = executed_at_kst.strftime("%Y-%m-%d-%H%M%S")
    payload = {"report_schema": REPORT_SCHEMA, "executed_at_kst": executed_at_kst.isoformat(), "markets": [],
               "report_hashes": {"plain": hashlib.sha256(plain_text.encode()).hexdigest(),
                                 "html": hashlib.sha256(html.encode()).hexdigest()}}
    for result in (us, kr):
        payload["markets"].append({
            "market": result.market, "status": result.status,
            "session_date": result.session_date, "previous_session_date": result.previous_session_date,
            "listing": result.total_count, "valid": result.analyzed_count, "coverage": result.coverage,
            "fallback": result.fallback_count, "mismatch": result.mismatch_count,
            "cross_checked": result.cross_checked_count, "threshold": result.threshold_pct,
            "detected": len(result.candidates), "missing": result.missing,
            "issues": [issue.__dict__ for issue in result.issues], "source_notes": result.source_notes,
            "error": result.error, "source": result.source,
            "candidates": _records(result.candidates.drop(columns=["news_items"], errors="ignore")),
            "prices": _records(result.prices),
        })
    (SETTINGS.output_dir / f"combined-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (SETTINGS.output_dir / f"combined-{stamp}.md").write_text(plain_text, encoding="utf-8")
    (SETTINGS.output_dir / f"combined-{stamp}.html").write_text(html, encoding="utf-8")


def _run_market(market: str, function, argument) -> MarketResult:
    try:
        return execute_market(function, argument)
    except Exception as exc:
        return _failed_result(market, exc)


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    executed_at_kst = datetime.now(tz=KST)
    market = getattr(args, "market", "all")
    jobs = {"US": (run_us_monitor, getattr(args, "session_date_us", None)),
            "KR": (run_kr_monitor, getattr(args, "session_date_kr", None))}
    selected = [name for name in jobs if market == "all" or name.lower() == market]
    # Markets are independent: run them concurrently, each in its own
    # deadline-bounded process, so one hung provider cannot delay the other.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {name: executor.submit(_run_market, name, *jobs[name]) for name in selected}
    results = {name: futures[name].result() if name in futures else _skipped_result(name) for name in jobs}
    us, kr = results["US"], results["KR"]

    plain_text = build_plain_text_report(us, kr, executed_at_kst)
    html = build_html_report(us, kr, executed_at_kst)
    _write_outputs(us, kr, plain_text, html, executed_at_kst)
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    subject = build_combined_subject(us, kr, "[TEST] " if is_manual else "")
    LOGGER.info("메일 제목: %s", subject)
    if args.notify and not args.dry_run:
        try:
            send_gmail_email(plain_text, subject, html)
        except Exception as exc:
            raise NotificationError("SMTP 발송 실패/수락 여부 불명: 중복 발송 방지를 위해 자동 재발송하지 않음") from exc
        LOGGER.info("Gmail 전송: 완료")
    else:
        LOGGER.info("dry-run/알림 비활성: 이메일 전송 안 함")
    LOGGER.info("Total elapsed=%.2fs", time.monotonic() - started)
    # Only a market-wide ERROR marks the workflow run as failed; INFO/WARNING
    # results are complete, flagged reports.
    return 1 if any(item.status == ERROR for item in (us, kr)) else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
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
                    f"US + KOSPI 급락 모니터 실행이 실패했습니다.\n\n오류: {type(exc).__name__}: {exc}\n",
                    f"{prefix}[급락 모니터][ERROR] 실행 실패",
                )
                LOGGER.info("실패 알림 Gmail 전송: 완료")
            except Exception:
                LOGGER.exception("실패 알림 Gmail 전송도 실패")
        return 1


if __name__ == "__main__":
    sys.exit(main())
