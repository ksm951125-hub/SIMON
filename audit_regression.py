"""Re-run an old KOSPI report on KRX regular closes. Never sends mail; OLD is not an oracle."""
from __future__ import annotations
import argparse
from datetime import date
import json
from pathlib import Path

from main import run_kr_monitor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=Path("tests/fixtures/legacy-2026-09-23.json"))
    parser.add_argument("--output", type=Path, default=Path("output/krx-regression"))
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))["market"]
    args.output.mkdir(parents=True, exist_ok=True)
    error = None
    try:
        result = run_kr_monitor(date.fromisoformat(baseline["session_date"]))
        prices = {r["code"]: r for r in result.prices.to_dict("records")}
    except (RuntimeError, ValueError) as exc:
        result, prices, error = None, {}, str(exc)
    rows = []
    for old in baseline["candidates"]:
        new = prices.get(old["code"])
        reason = error or (result.missing.get(old["code"] + " " + old["company_name"]) if result else "unavailable")
        rows.append({"Code": old["code"], "Company": old["company_name"],
                     "OLD Prev": old["previous_close"], "OLD Close": old["close"], "OLD %": old["change_pct"],
                     "NEW Prev": new["previous_close"] if new else None,
                     "NEW Close": new["close"] if new else None,
                     "NEW %": new["change_pct"] if new else None,
                     "Source": new["source"] if new else "unavailable",
                     "Result": ("DETECTED" if new["change_pct"] <= -7 else "NOT DETECTED") if new else reason or "not in dated universe"})
    summary = {"session_date": baseline["session_date"], "official_verification": "BLOCKED" if error else result.status,
               "error": error, "comparison": rows, "legacy_missing": baseline["missing"],
               "coverage": result.coverage if result else None,
               "missing": result.missing if result else None,
               "all_detected": result.candidates.to_dict("records") if result else None}
    (args.output / "comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fields = list(rows[0])
    def cell(value):
        if value is None: return "미확정"
        if isinstance(value, float): return f"{value:,.2f}"
        return str(value).replace("|", "/").replace("\n", " ")
    text = "# KRX regression verification\n\n" + str(summary["official_verification"]) + "\n\n"
    text += "| " + " | ".join(fields) + " |\n|" + "---|" * len(fields) + "\n"
    text += "\n".join("| " + " | ".join(cell(row[f]) for f in fields) + " |" for row in rows)
    text += "\n\nOLD는 과거 오류 보고서이며 공식 정답이 아닙니다. 새 값 미확정 상태에서 전체 급락 목록을 확정하지 않습니다.\n"
    (args.output / "comparison.md").write_text(text, encoding="utf-8")
    print(text)
    return 2 if error else (0 if result.status == "OK" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
