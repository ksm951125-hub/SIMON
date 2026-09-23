from __future__ import annotations

import math
from datetime import date


REQUIRED_PRICE_FIELDS = ("previous_close", "close", "open", "low", "volume")


def validate_price_row(row: dict, session_date: date) -> list[str]:
    errors: list[str] = []
    if row.get("session_date") != session_date:
        errors.append("거래일 불일치")
    for field in REQUIRED_PRICE_FIELDS:
        value = row.get(field)
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
            errors.append(f"{field} 누락/NaN")
    for field in ("previous_close", "close", "open", "low"):
        value = row.get(field)
        if isinstance(value, (int, float)) and math.isfinite(value) and value <= 0:
            errors.append(f"{field} <= 0")
    volume = row.get("volume")
    if isinstance(volume, (int, float)) and math.isfinite(volume) and volume < 0:
        errors.append("volume < 0")
    return errors
