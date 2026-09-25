from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd


def _money(value: float) -> str:
    return f"${value:,.2f}"


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
