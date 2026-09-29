"""Reject legacy/mixed artifacts before any manual resend. Standard library only."""
import hashlib
import json
from pathlib import Path

REPORT_SCHEMA = "regular-close-v3"


def validate_saved_report(path: Path, plain: str, html: str, subject: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("report_schema") != REPORT_SCHEMA:
        raise ValueError("Legacy unverified report: rerun the monitor before resending")
    for key, content in [("plain", plain), ("html", html)]:
        if payload.get("report_hashes", {}).get(key) != hashlib.sha256(content.encode()).hexdigest():
            raise ValueError("Saved report content/provenance mismatch")
    markets = payload.get("markets", [])
    if {m.get("market") for m in markets} != {"US", "KR"} or len(markets) != 2:
        raise ValueError("Saved market metadata missing")
    for market in markets:
        if market.get("status") in {"WARNING", "FAILED"} and not any(tag in subject for tag in ("DATA WARNING", "FAILED")):
            raise ValueError("Warning report must retain DATA WARNING/FAILED in subject")
