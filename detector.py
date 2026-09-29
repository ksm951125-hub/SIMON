from __future__ import annotations

import math
from decimal import Decimal


def calculate_change_pct(previous_close: float, close: float) -> float:
    if not all(math.isfinite(value) and value > 0 for value in (previous_close, close)):
        raise ValueError("종가는 양의 유한 숫자여야 합니다")
    # Normalize binary floating-point noise so an exact boundary such as
    # 100 -> 90 is treated as -10.0, not -9.999999999999998.
    return float((Decimal(str(close)) / Decimal(str(previous_close)) - 1) * 100)


def is_drop(change_pct: float, threshold_pct: float = -10.0) -> bool:
    return math.isfinite(change_pct) and change_pct <= threshold_pct
