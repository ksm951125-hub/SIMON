from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from market_result import MarketResult


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _market_price(value: float, currency: str) -> str:
    if currency == "KRW":
        return f"KRW {value:,.0f}"
    return f"${value:,.2f}"


def render_combined_markdown(
    us: MarketResult,
    kr: MarketResult,
    executed_at_kst: datetime,
) -> str:
    lines = [
        "# US + KOSPI 급락 모니터",
        "",
        f"실행 시각(KST): {executed_at_kst:%Y-%m-%d %H:%M:%S %Z}",
        "",
    ]
    for result in (us, kr):
        lines.extend(
            [
                "=" * 64,
                f"## {result.title}",
                f"기준: {result.threshold_pct:.1f}% 이하",
                "",
                f"상태: {result.status}",
                f"분석일: {result.session_date.isoformat() if result.session_date else '확인 불가'}",
                f"이전 거래일: {result.previous_session_date.isoformat() if result.previous_session_date else '확인 불가'}",
                f"수집: {result.analyzed_count} / {result.total_count}",
                f"Coverage: {result.coverage:.1%}",
            ]
        )
        if result.status in {"DATA_INCOMPLETE", "FAILED"}:
            lines.append("탐지: 확인 필요 (데이터 불완전)")
        else:
            lines.append(f"탐지: {len(result.candidates)}개")
        if result.error:
            lines.append(f"오류: {result.error}")
        lines.append("")

        if not result.candidates.empty:
            code_label = "Ticker" if result.market == "US" else "Code"
            lines.extend(
                [
                    f"| {code_label} | Company | Prev Close | Close | Change |",
                    "|---|---|---:|---:|---:|",
                ]
            )
            for _, row in result.candidates.iterrows():
                lines.append(
                    f"| {row[result.code_column]} | {row['company_name']} | "
                    f"{_market_price(row['previous_close'], result.currency)} | "
                    f"{_market_price(row['close'], result.currency)} | "
                    f"{row['change_pct']:+.2f}% |"
                )
        elif result.status not in {"DATA_INCOMPLETE", "FAILED"}:
            lines.append("기준 이하 급락 종목 없음")

        if result.missing:
            lines.extend(["", "데이터 누락:"])
            lines.extend(f"- {code}: {reason}" for code, reason in sorted(result.missing.items()))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_combined_subject(us: MarketResult, kr: MarketResult, test_prefix: str = "") -> str:
    warning = "[DATA WARNING]" if us.has_data_warning or kr.has_data_warning else ""

    def count_text(result: MarketResult) -> str:
        if result.status in {"DATA_INCOMPLETE", "FAILED"}:
            return "확인필요"
        return f"{len(result.candidates)}개"

    return (
        f"{test_prefix}[급락 모니터]{warning} "
        f"S&P500 {count_text(us)} / KOSPI {count_text(kr)}"
    )


def render_markdown(
    session_date: date,
    analyzed_count: int,
    candidates: pd.DataFrame,
    missing: dict[str, str],
    holiday_reason: str | None = None,
    previous_session_date: date | None = None,
    executed_at_kst: datetime | None = None,
) -> str:
    lines = ["# [S&P 500 급락 모니터링]", "", f"미국 거래일: {session_date.isoformat()}"]
    if holiday_reason:
        lines.extend(["", holiday_reason])
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            f"분석 완료: {analyzed_count}개",
            f"데이터 누락: {len(missing)}개",
            f"10% 이상 하락 종목: {len(candidates)}개",
            f"이전 거래일: {previous_session_date.isoformat() if previous_session_date else '미확인'}",
            "실행 시각(KST): "
            + (executed_at_kst.strftime("%Y-%m-%d %H:%M:%S %Z") if executed_at_kst else "미확인"),
            "",
        ]
    )
    if candidates.empty:
        lines.append("10% 이상 하락 종목 없음")
    for index, row in candidates.reset_index(drop=True).iterrows():
        lines.extend(
            [
                f"## {index + 1}. {row['company_name']} ({row['ticker']})",
                "",
                f"Sector: {row['sector']}",
                "",
                f"Previous Close: {_money(row['previous_close'])}",
                f"Close: {_money(row['close'])}",
                f"Change: {row['change_pct']:+.2f}%",
                "",
                f"Volume: {row['volume']:,.0f}",
                f"20D Avg Volume: {row['average_volume_20d']:,.0f}",
                f"Volume vs 20D: {row['volume_change_pct']:+.1f}%",
                "",
                f"Intraday Low: {_money(row['low'])}",
                f"Open → Close: {row['open_to_close_pct']:+.2f}%",
                "",
                "주요 원인:",
                str(row.get("news_cause", "명확한 급락 원인 확인되지 않음")),
                "",
                "관련 뉴스:",
            ]
        )
        news_items = row.get("news_items", [])
        if news_items:
            for item in news_items:
                published = item.get("published_at") or "시각 미상"
                lines.append(f"- {item['source']} — [{item['title']}]({item['url']}) ({published})")
        else:
            lines.append("- 확인된 관련 뉴스 없음")
        lines.extend(["", "---", ""])
    if missing:
        lines.extend(["## 데이터 누락 종목", ""])
        lines.extend(f"- {ticker}: {reason}" for ticker, reason in sorted(missing.items()))
    return "\n".join(lines).rstrip() + "\n"


def write_reports(
    output_dir: Path,
    session_date: date,
    analyzed_count: int,
    candidates: pd.DataFrame,
    missing: dict[str, str],
    markdown: str,
    holiday_reason: str | None = None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / session_date.isoformat()
    csv_path = stem.with_suffix(".csv")
    json_path = stem.with_suffix(".json")
    md_path = stem.with_suffix(".md")

    csv_frame = candidates.copy()
    for column in ("news_items",):
        if column in csv_frame:
            csv_frame[column] = csv_frame[column].map(lambda value: json.dumps(value, ensure_ascii=False))
    csv_frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    payload = {
        "session_date": session_date.isoformat(),
        "analyzed_count": analyzed_count,
        "drop_count": len(candidates),
        "holiday_reason": holiday_reason,
        "missing": missing,
        "results": candidates.to_dict(orient="records"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    return [csv_path, json_path, md_path]
