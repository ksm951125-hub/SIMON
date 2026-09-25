from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class MarketResult:
    market: str
    title: str
    threshold_pct: float
    code_column: str
    currency: str
    status: str = "FAILED"
    session_date: date | None = None
    previous_session_date: date | None = None
    total_count: int = 0
    analyzed_count: int = 0
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)
    missing: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def coverage(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return self.analyzed_count / self.total_count

    @property
    def has_data_warning(self) -> bool:
        return self.status in {"PARTIAL", "DATA_INCOMPLETE", "FAILED"}
