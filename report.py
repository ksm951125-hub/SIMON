from __future__ import annotations

import json
from datetime import date, datetime
from html import escape
from pathlib import Path

import pandas as pd

from market_result import FAILED, OK, SKIPPED, WARNING, MarketResult

MAX_WARNING_ITEMS = 30


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _market_price(value: float, currency: str) -> str:
    if currency == "KRW":
        return f"KRW {value:,.0f}"
    return f"${value:,.2f}"


def _date_text(value: date | None) -> str:
    return value.isoformat() if value else "확인 불가"


def _detection_text(result: MarketResult) -> str:
    if result.status == SKIPPED:
        return "제외"
    if result.status == FAILED:
        return f"{len(result.candidates)}개 (불완전)" if not result.candidates.empty else "확인 필요"
    return f"{len(result.candidates)}개"


def _metrics(result: MarketResult) -> list[tuple[str, str]]:
    return [
        ("Listing", f"{result.total_count:,}"),
        ("Valid Prices", f"{result.analyzed_count:,}"),
        ("Coverage", f"{result.coverage:.1%}"),
        ("Fallback", f"{result.fallback_count:,}"),
        ("Missing", f"{result.missing_count:,}"),
        ("Detected", _detection_text(result)),
    ]


def _source_text(row: pd.Series, result: MarketResult) -> str:
    source = str(row.get("source") or result.source)
    validation = row.get("validation")
    return f"{source} · {validation}" if isinstance(validation, str) and validation else source


def _source_tag(row: pd.Series, result: MarketResult) -> str:
    """Compact source label for the mail table; details live in the note line."""
    source = str(row.get("source") or result.source)
    short = source.split("(")[0].split(" ")[0] or source
    if bool(row.get("fallback", False)):
        return f"{short}(대체)"
    if bool(row.get("mismatch", False)):
        return f"{short}·불일치"
    if bool(row.get("cross_checked", False)):
        return f"{short}✓"
    return short


def _status_message(result: MarketResult) -> str | None:
    if result.status == FAILED:
        return "DATA FAILED: 시장 전체 결과를 신뢰할 수 없습니다. 누락 종목에 급락이 존재할 수 있습니다."
    if result.status == WARNING:
        return ("DATA WARNING: 일부 데이터 누락·대체·불일치가 있습니다. 검증된 종목 기준 결과이며 "
                "누락 종목은 아래 목록을 확인하세요.")
    return None


def build_plain_text_report(us: MarketResult, kr: MarketResult, executed_at_kst: datetime) -> str:
    lines = ["# US + KOSPI 급락 모니터", "", f"실행 시각(KST): {executed_at_kst:%Y-%m-%d %H:%M:%S %Z}", ""]
    for result in (us, kr):
        lines.extend([
            "=" * 64,
            f"## {result.title}",
            f"기준: 정규장 종가 전일 대비 {result.threshold_pct:.1f}% 이하",
            "",
            f"상태: {result.status}",
            f"분석일: {_date_text(result.session_date)}",
            f"이전 거래일: {_date_text(result.previous_session_date)}",
        ])
        lines.extend(f"{label}: {value}" for label, value in _metrics(result))
        lines.append(f"Source: {result.source}")
        if result.notice:
            lines.append(f"참고: {result.notice}")
        message = _status_message(result)
        if message:
            lines.append(message)
        if result.error:
            lines.append(f"오류/경고: {result.error}")
        lines.append("")

        if not result.candidates.empty:
            code_label = "Ticker" if result.market == "US" else "Code"
            lines.extend([f"| {code_label} | Company | Prev Close | Close | Change | Source |",
                          "|---|---|---:|---:|---:|---|"])
            for _, row in result.candidates.iterrows():
                lines.append(
                    f"| {row[result.code_column]} | {row['company_name']} | "
                    f"{_market_price(row['previous_close'], result.currency)} | "
                    f"{_market_price(row['close'], result.currency)} | "
                    f"{row['change_pct']:+.2f}% | {_source_text(row, result)} |"
                )
            notes = [(row[result.code_column], row.get("note")) for _, row in result.candidates.iterrows()]
            for code, note in notes:
                if isinstance(note, str) and note:
                    lines.append(f"  * {code}: {note}")
        elif result.status in {OK, WARNING}:
            lines.append("기준 이하 급락 종목 없음")

        if result.missing:
            lines.extend(["", f"데이터 누락 ({len(result.missing)}건):"])
            lines.extend(f"- {code}: {reason}" for code, reason in sorted(result.missing.items()))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Backward-compatible name used by existing callers and external scripts.
render_combined_markdown = build_plain_text_report


def _html_price(value: float, currency: str) -> str:
    if currency == "KRW":
        return f"&#8361;{value:,.0f}"
    return f"${value:,.2f}"


def _status_style(status: str) -> tuple[str, str, str]:
    if status == OK:
        return "&#9679; OK", "#166534", "#dcfce7"
    if status == WARNING:
        return "&#9888; WARNING", "#9a3412", "#ffedd5"
    if status == SKIPPED:
        return "&#8212; SKIPPED", "#475569", "#e2e8f0"
    return "&#9888; FAILED", "#991b1b", "#fee2e2"


def _summary_cell(label: str, value: str, width: str) -> str:
    return f"""
      <td class="summary-cell" width="{width}" valign="top" style="padding:12px 8px;border-right:1px solid #e5e7eb;text-align:center;">
        <div style="font-size:11px;line-height:16px;color:#64748b;">{escape(label)}</div>
        <div style="margin-top:3px;font-size:14px;line-height:20px;font-weight:bold;color:#0f172a;white-space:nowrap;">{escape(value)}</div>
      </td>"""


def _candidate_rows(result: MarketResult) -> str:
    rows: list[str] = []
    total = len(result.candidates)
    for position, (_, row) in enumerate(result.candidates.iterrows()):
        if position == 10 and total > 10:
            rows.append(
                f'<tr><td colspan="6" style="padding:9px 10px;background:#f8fafc;border-top:2px solid #e2e8f0;'
                f'font-size:12px;font-weight:bold;color:#475569;">외 {total - 10}개 종목 (전체 목록)</td></tr>'
            )
        compact = position >= 10
        padding = "7px 8px" if compact else "10px 8px"
        code = escape(str(row[result.code_column]))
        company = escape(str(row["company_name"]))
        previous = _html_price(float(row["previous_close"]), result.currency)
        close = _html_price(float(row["close"]), result.currency)
        change = float(row["change_pct"])
        note = row.get("note")
        note_html = (f'<div style="margin-top:3px;font-size:10px;line-height:14px;color:#9a3412;">&#9888; {escape(note)}</div>'
                     if isinstance(note, str) and note else "")
        rows.append(
            f"""<tr class="candidate-row">
              <td class="c-code" style="padding:{padding};border-bottom:1px solid #e5e7eb;font-size:12px;font-weight:bold;color:#0f172a;white-space:nowrap;">{code}</td>
              <td class="c-company" style="padding:{padding};border-bottom:1px solid #e5e7eb;font-size:12px;line-height:17px;color:#334155;word-break:break-word;overflow-wrap:anywhere;">{company}<span class="mobile-prev" style="display:none;">Prev {previous}</span>{note_html}</td>
              <td class="c-prev" align="right" style="padding:{padding};border-bottom:1px solid #e5e7eb;font-size:12px;color:#475569;white-space:nowrap;"><span class="mobile-label" style="display:none;">Prev</span>{previous}</td>
              <td class="c-close" align="right" style="padding:{padding};border-bottom:1px solid #e5e7eb;font-size:12px;color:#0f172a;white-space:nowrap;"><span class="mobile-label" style="display:none;">Close</span>{close}</td>
              <td class="c-change" align="right" style="padding:{padding};border-bottom:1px solid #e5e7eb;font-size:12px;font-weight:bold;color:#b91c1c;white-space:nowrap;"><span class="mobile-label" style="display:none;">Change</span>&#9660;&nbsp;{change:.2f}%</td>
              <td class="c-source" style="padding:{padding};font-size:10px;border-bottom:1px solid #e5e7eb;word-break:break-word;" title="{escape(_source_text(row, result))}">{escape(_source_tag(row, result))}</td>
            </tr>"""
        )
    return "".join(rows)


def _warning_items(result: MarketResult) -> str:
    items: list[str] = []
    missing = sorted(result.missing.items())
    for code_name, reason in missing[:MAX_WARNING_ITEMS]:
        parts = str(code_name).split(" ", 1)
        code = escape(parts[0])
        company = escape(parts[1]) if len(parts) > 1 else ""
        items.append(
            f"""<tr>
              <td valign="top" style="padding:10px 0;border-top:1px solid #fed7aa;">
                <div style="font-size:13px;font-weight:bold;color:#7c2d12;">{code}</div>
                {f'<div style="margin-top:2px;font-size:12px;color:#7c2d12;">{company}</div>' if company else ''}
                <div style="margin-top:5px;font-size:12px;line-height:18px;color:#9a3412;word-break:break-word;">{escape(str(reason))}</div>
              </td>
            </tr>"""
        )
    if len(missing) > MAX_WARNING_ITEMS:
        items.append(
            '<tr><td style="padding:10px 0;border-top:1px solid #fed7aa;font-size:12px;color:#9a3412;">'
            f"외 {len(missing) - MAX_WARNING_ITEMS}건 — 전체 목록은 실행 artifact(JSON)와 텍스트 본문 참조</td></tr>"
        )
    if result.error:
        items.append(
            '<tr><td style="padding:10px 0;border-top:1px solid #fed7aa;font-size:12px;line-height:18px;color:#9a3412;">'
            + escape(result.error) + "</td></tr>"
        )
    return "".join(items)


def _market_card(result: MarketResult) -> str:
    flag = "&#127482;&#127480;" if result.market == "US" else "&#127472;&#127479;"
    accent = "#1d4ed8" if result.market == "KR" else "#0f172a"
    badge_text, badge_color, badge_bg = _status_style(result.status)
    dates = "".join(_summary_cell(label, value, "50%") for label, value in (
        ("분석일", _date_text(result.session_date)), ("이전 거래일", _date_text(result.previous_session_date))))
    metrics = "".join(_summary_cell(label, value, "16%") for label, value in _metrics(result))
    code_label = "Ticker" if result.market == "US" else "Code"
    if result.candidates.empty:
        if result.status in {FAILED, SKIPPED}:
            text = "이번 실행에서 제외된 시장입니다." if result.status == SKIPPED else "데이터가 불완전하여 탐지 결과를 확정할 수 없습니다."
            body = f'<div style="padding:18px;text-align:center;font-size:13px;color:#9a3412;background:#fff7ed;">{text}</div>'
        else:
            body = '<div style="padding:22px;text-align:center;font-size:13px;font-weight:bold;color:#166534;background:#f0fdf4;">&#10003; 기준 이하 급락 종목 없음</div>'
    else:
        body = f"""<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;border-collapse:collapse;table-layout:fixed;">
            <tr class="candidate-header" style="background:#f8fafc;">
              <th class="h-code" width="10%" align="left" style="padding:9px 8px;border-bottom:1px solid #cbd5e1;font-size:10px;color:#64748b;">{code_label}</th>
              <th class="h-company" width="27%" align="left" style="padding:9px 8px;border-bottom:1px solid #cbd5e1;font-size:10px;color:#64748b;">Company</th>
              <th class="h-prev" width="16%" align="right" style="padding:9px 8px;border-bottom:1px solid #cbd5e1;font-size:10px;color:#64748b;">Prev Close</th>
              <th class="h-close" width="16%" align="right" style="padding:9px 8px;border-bottom:1px solid #cbd5e1;font-size:10px;color:#64748b;">Close</th>
              <th class="h-change" width="15%" align="right" style="padding:9px 8px;border-bottom:1px solid #cbd5e1;font-size:10px;color:#64748b;">Change</th>
              <th class="h-source" width="16%" style="font-size:10px;color:#64748b;">Source</th>
            </tr>
            {_candidate_rows(result)}
          </table>
        </div>"""
    message = _status_message(result)
    data_warning = (f'<div style="padding:12px;color:#9a3412;background:#fff7ed;font-size:12px;line-height:18px;">'
                    f'<strong>Missing: {result.missing_count} · Fallback: {result.fallback_count} · 불일치: {result.mismatch_count}</strong>'
                    f'<br>{escape(message)}</div>' if message else "")
    notice = (f'<div style="padding:10px 12px;color:#1e3a8a;background:#eff6ff;font-size:12px;">&#8505; {escape(result.notice)}</div>'
              if result.notice else "")
    return f"""
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin:0 0 18px;border:1px solid #dbe2ea;border-collapse:separate;border-spacing:0;background:#ffffff;">
        <tr><td style="height:4px;background:{accent};font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr>
          <td style="padding:18px 18px 14px;">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>
              <td valign="middle" style="font-size:18px;line-height:25px;font-weight:bold;color:#0f172a;">{flag}&nbsp; {escape(result.title)}</td>
              <td align="right" valign="middle" style="white-space:nowrap;">
                <span style="display:inline-block;padding:5px 9px;background:{badge_bg};color:{badge_color};font-size:11px;line-height:14px;font-weight:bold;border-radius:12px;">STATUS: {badge_text}</span>
              </td>
            </tr></table>
            <div style="margin-top:6px;font-size:12px;color:#64748b;">기준: <strong style="color:#b91c1c;">정규장 종가 {result.threshold_pct:.1f}% 이하</strong> · Source: {escape(result.source)}</div>
          </td>
        </tr>
        <tr><td style="padding:0 18px 16px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #e5e7eb;border-collapse:collapse;background:#f8fafc;"><tr>{dates}</tr></table>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #e5e7eb;border-top:0;border-collapse:collapse;background:#f8fafc;"><tr>{metrics}</tr></table>
        </td></tr>
        <tr><td>{notice}{data_warning}{body}</td></tr>
      </table>"""


def build_html_report(us: MarketResult, kr: MarketResult, executed_at_kst: datetime) -> str:
    warning_results = [result for result in (us, kr)
                       if result.status != SKIPPED and (result.missing or result.error or result.has_data_warning)]
    warning_count = sum(len(result.missing) + bool(result.error) for result in warning_results)
    warnings = ""
    if warning_results:
        warning_rows = "".join(
            '<tr><td style="padding:8px 0;font-weight:bold;color:#92400e;">'
            + escape(result.title) + " — " + escape(result.status) + "</td></tr>"
            + _warning_items(result) for result in warning_results
        )
        warnings = f"""
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin:0 0 18px;border:1px solid #f59e0b;border-collapse:separate;border-spacing:0;background:#fffbeb;">
        <tr><td style="padding:15px 18px 8px;font-size:15px;font-weight:bold;color:#92400e;">&#9888; DATA WARNING — 데이터 누락·경고 · {warning_count}건</td></tr>
        <tr><td style="padding:0 18px 8px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">{warning_rows}</table></td></tr>
      </table>"""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
@media only screen and (max-width:600px) {{
  .email-shell {{ width:100% !important; }}
  .outer-pad {{ padding:10px !important; }}
  .header-pad {{ padding:22px 18px !important; }}
  .summary-cell {{ box-sizing:border-box !important; padding:8px 2px !important; border-bottom:1px solid #e5e7eb !important; }}
  .summary-cell div {{ font-size:10px !important; line-height:14px !important; white-space:normal !important; }}
  .candidate-header {{ display:table-row !important; }}
  .candidate-header th {{ box-sizing:border-box !important; padding:7px 3px !important; font-size:8px !important; }}
  .candidate-row {{ display:table-row !important; }}
  .c-code, .c-company, .c-prev, .c-close, .c-change, .c-source {{ display:table-cell !important; box-sizing:border-box !important; padding:8px 3px !important; font-size:10px !important; }}
  .h-code, .c-code {{ width:12% !important; }}
  .h-company, .c-company {{ width:24% !important; }}
  .h-prev, .c-prev {{ width:17% !important; }}
  .h-close, .c-close {{ width:17% !important; }}
  .h-change, .c-change {{ width:18% !important; }}
  .h-source, .c-source {{ width:12% !important; }}
  .header-meta-left, .header-meta-right {{ display:block !important; box-sizing:border-box !important; width:100% !important; text-align:left !important; }}
  .header-meta-right {{ padding-top:6px !important; }}
}}
</style></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#0f172a;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:#f1f5f9;"><tr><td class="outer-pad" align="center" style="padding:24px 12px;">
  <table class="email-shell" role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:760px;border-collapse:collapse;table-layout:fixed;">
    <tr><td class="header-pad" style="padding:28px 30px;background:#0b1f3a;color:#ffffff;">
      <div style="font-size:11px;line-height:16px;font-weight:bold;letter-spacing:1.2px;color:#93c5fd;">급락 모니터 리포트</div>
      <div style="margin-top:7px;font-size:24px;line-height:32px;font-weight:bold;">US + KOSPI 급락 모니터</div>
      <div style="margin-top:5px;font-size:13px;line-height:20px;color:#cbd5e1;">전일 대비 급락 종목 현황을 알려드립니다.</div>
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:20px;border-top:1px solid #334155;"><tr>
        <td class="header-meta-left" valign="top" style="padding-top:12px;font-size:11px;line-height:17px;color:#94a3b8;">실행 시각<br><strong style="font-size:13px;color:#ffffff;">{executed_at_kst:%Y-%m-%d %H:%M} KST</strong></td>
        <td class="header-meta-right" align="right" valign="bottom" style="padding-top:12px;font-size:11px;color:#94a3b8;">자동 발송 리포트</td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:20px 0 0;">{warnings}{_market_card(us)}{_market_card(kr)}</td></tr>
    <tr><td style="padding:18px 20px;background:#ffffff;border:1px solid #e2e8f0;font-size:11px;line-height:19px;color:#64748b;">
      <strong style="color:#334155;">참고사항</strong><br>
      &#8226; 시장별 표시된 분석일/이전 거래일(각 거래소 실제 거래일)을 기준으로 생성됩니다.<br>
      &#8226; 등락률은 직전 거래일 정규장 종가 대비 분석일 정규장 종가 기준입니다(시간외·프리/애프터마켓 제외).<br>
      &#8226; Valid Prices는 양일 종가가 모두 검증된 종목 수이며, Coverage = Valid Prices / Listing 입니다.
    </td></tr>
    <tr><td align="center" style="padding:18px 0 8px;font-size:10px;letter-spacing:.4px;color:#94a3b8;">Automated Market Drop Monitor</td></tr>
  </table>
</td></tr></table></body></html>"""


def build_combined_subject(us: MarketResult, kr: MarketResult, test_prefix: str = "") -> str:
    active = [result for result in (us, kr) if result.status != SKIPPED]
    tags = ""
    failed = [result.market for result in active if result.status == FAILED]
    if failed:
        tags += f"[FAILED {'/'.join(failed)}]"
    if any(result.status == WARNING for result in active):
        tags += "[DATA WARNING]"

    def count_text(result: MarketResult) -> str:
        detection = _detection_text(result)
        return "확인필요" if detection == "확인 필요" else detection.replace(" ", "")

    subject = f"{test_prefix.strip()}[급락 모니터]{tags} S&P500 {count_text(us)} · KOSPI {count_text(kr)}"
    if tags:
        def missing_text(result: MarketResult) -> str:
            if result.status == SKIPPED:
                return "-"
            if result.total_count == 0:
                return "확인불가"
            fallback = f"(대체 {result.fallback_count})" if result.fallback_count else ""
            return f"{result.missing_count}건{fallback}"

        subject += f" | 데이터 누락 US {missing_text(us)} / KR {missing_text(kr)}"
    return subject


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
