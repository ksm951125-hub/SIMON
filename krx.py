"""Official KRX bulk daily data. No provider fallback and no disk cache reads."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

SOURCE = "KRX"
SCHEMA_VERSION = "krx-regular-v1"
BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/"
ENDPOINTS = {"stk_isu_base_info", "stk_bydd_trd"}


def number(value: object, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid {field}") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"invalid {field}")
    return result


def short_code(value: object) -> str:
    # Preserve leading zeros and alphabetic share-class characters at ANY position.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Z]{6}", value):
        raise ValueError(f"invalid KRX short code: {value!r}")
    return value


class KrxClient:
    def __init__(self, api_key: str | None = None, audit_dir: Path | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("KRX_API_KEY", "")
        if not self.api_key.strip():
            raise RuntimeError("DATA WARNING: KRX_API_KEY is required; no fallback permitted")
        self.audit_dir = audit_dir
        # Memoization only within one run; old provider files are never opened.
        self._responses: dict[tuple, list[dict]] = {}

    def rows(self, endpoint: str, day: date) -> list[dict]:
        if endpoint not in ENDPOINTS:
            raise ValueError("unapproved KRX endpoint")
        key = (SOURCE, SCHEMA_VERSION, endpoint, day.isoformat())
        if key in self._responses:
            return self._responses[key]
        for attempt in range(3):
            try:
                response = requests.get(
                    BASE_URL + endpoint,
                    params={"basDd": day.strftime("%Y%m%d")},
                    headers={"AUTH_KEY": self.api_key, "Accept": "application/json"},
                    timeout=(5, 20), allow_redirects=False,
                )
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError("KRX transport failure after 3 attempts") from None
                time.sleep(attempt + 1)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(attempt + 1)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"KRX HTTP {response.status_code}: {endpoint}; check key/service approval")
            try:
                payload = response.json()
            except ValueError:
                raise ValueError("KRX returned non-JSON data") from None
            if not isinstance(payload, dict) or not isinstance(payload.get("OutBlock_1"), list):
                raise ValueError("KRX response missing OutBlock_1; authentication/schema failure")
            if payload.get("respCode") not in (None, "", "0", "00", "200"):
                raise ValueError("KRX API reported an error")
            rows = payload["OutBlock_1"]
            if not rows or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"KRX empty/invalid {endpoint} on expected session {day}; closure or publication must be verified")
            if self.audit_dir:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                evidence = {"source": SOURCE, "schema_version": SCHEMA_VERSION,
                            "endpoint": endpoint, "requested_date": str(day),
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "sha256": hashlib.sha256(raw.encode()).hexdigest(), "payload": payload}
                (self.audit_dir / f"{SCHEMA_VERSION}-{endpoint}-{day}.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            self._responses[key] = rows
            return rows
        raise RuntimeError("KRX retry exhaustion")
