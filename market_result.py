from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from config import SETTINGS

OK = "OK"
WARNING = "WARNING"
FAILED = "FAILED"
SKIPPED = "SKIPPED"


@dataclass
class MarketResult:
    market: str
    title: str
    threshold_pct: float
    code_column: str
    currency: str
    status: str = FAILED
    session_date: date | None = None
    previous_session_date: date | None = None
    # Listing = symbols in the analysis universe; analyzed = symbols with a
    # validated regular-close pair for BOTH sessions.
    total_count: int = 0
    analyzed_count: int = 0
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)
    missing: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    source: str = "unverified"
    prices: pd.DataFrame = field(default_factory=pd.DataFrame)
    fallback_count: int = 0
    # Symbols whose primary and secondary sources disagreed on a close.
    mismatch_count: int = 0
    # Symbols cross-checked against an independent source (both closes).
    cross_checked_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # Informational (not a data problem), e.g. "US holiday: latest session re-analyzed".
    notice: str | None = None

    @property
    def coverage(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return self.analyzed_count / self.total_count

    @property
    def missing_count(self) -> int:
        return max(self.total_count - self.analyzed_count, 0)

    @property
    def has_data_warning(self) -> bool:
        return self.status in {WARNING, FAILED}

    @property
    def detection_known(self) -> bool:
        """Whether '0 detected' can be read as 'no drops' for this market."""
        return self.status in {OK, WARNING}


def count_critical_mismatches(prices: pd.DataFrame, validation_band_pct: float) -> int:
    """Source disagreements that could change the alert outcome.

    A disagreement on a symbol far from the threshold under BOTH sources is
    logged and shown, but does not degrade the market status.
    """
    if prices.empty or "mismatch" not in prices:
        return 0
    flagged = prices.loc[prices["mismatch"].fillna(False).astype(bool)]
    if flagged.empty:
        return 0
    alt = flagged["alt_change_pct"] if "alt_change_pct" in flagged else flagged["change_pct"]
    worst = pd.concat([flagged["change_pct"], alt.fillna(flagged["change_pct"])], axis=1).min(axis=1)
    return int((worst <= validation_band_pct).sum())


def classify_status(
    total_count: int,
    analyzed_count: int,
    *,
    missing_count: int = 0,
    fallback_count: int = 0,
    mismatch_count: int = 0,
    warnings: list[str] | None = None,
    failed_coverage_ratio: float = SETTINGS.failed_coverage_ratio,
) -> str:
    """OK: every symbol validated with no degraded path.

    WARNING: results are trustworthy for the validated symbols but some symbols
    are missing, used a fallback source, disagreed between sources, or a
    listing/calendar integrity warning was raised.

    FAILED: the market as a whole cannot be trusted (no data, or coverage below
    the market-wide outage threshold). A single missing symbol is never FAILED.
    """
    if total_count < 0 or analyzed_count < 0 or analyzed_count > total_count:
        raise ValueError("invalid coverage counts")
    if total_count == 0 or analyzed_count == 0:
        return FAILED
    if analyzed_count / total_count < failed_coverage_ratio:
        return FAILED
    if missing_count or analyzed_count < total_count or fallback_count or mismatch_count or warnings:
        return WARNING
    return OK
