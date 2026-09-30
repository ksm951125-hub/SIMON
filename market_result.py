"""Market result model and status policy.

Status levels (market):
    NORMAL   all required data collected and cross-validated
    INFO     results trustworthy; informational notes only (a secondary source
             was late but another source confirmed, special trading confirmed by
             market state, fallback value confirmed by two sources, holiday ...)
    WARNING  results produced, but some validation is insufficient (missing
             symbols, unverified fallback, near-threshold symbol with no second
             source or with conflicting sources, unconfirmed special move ...)
    ERROR    core data could not be obtained (coverage below threshold, no data,
             trading date undetermined, universe unavailable)

Only WARNING/ERROR are "DATA WARNING". Every issue carries its own level and
category, so the report wording is derived from actual causes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from config import SETTINGS

NORMAL = "NORMAL"
INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"
SKIPPED = "SKIPPED"
# Backward-compatible aliases.
OK = NORMAL
FAILED = ERROR
LEVEL_ORDER = {NORMAL: 0, INFO: 1, WARNING: 2, ERROR: 3}

# Issue categories -> report wording
CATEGORY_TEXT = {
    "MISSING": "데이터 누락",
    "FALLBACK": "대체 데이터 사용",
    "FALLBACK_SINGLE": "대체 데이터 단독(검증 불가)",
    "MISMATCH": "소스 간 가격 불일치",
    "UNVERIFIED": "2차 검증 불가",
    "STALE_SECONDARY": "2차 검증 소스 지연(다른 소스로 검증)",
    "OUTLIER": "소스 이상치 제외(다수 소스로 확정)",
    "SPECIAL_TRADING": "특수거래 종목",
    "SPECIAL_UNCONFIRMED": "가격제한폭 초과(특수거래 미확인)",
    "LISTING": "종목 목록 불일치",
    "CALENDAR": "거래일 안내",
    "SOURCE": "데이터 소스 장애",
    "NOTICE": "참고",
}


@dataclass(frozen=True)
class Issue:
    level: str
    category: str
    message: str
    symbol: str = ""
    name: str = ""

    @property
    def is_data_warning(self) -> bool:
        return self.level in (WARNING, ERROR)


@dataclass
class MarketResult:
    market: str
    title: str
    threshold_pct: float
    code_column: str
    currency: str
    status: str = ERROR
    session_date: date | None = None
    previous_session_date: date | None = None
    # Listing = symbols in the analysis universe; analyzed = symbols with a
    # usable regular-close pair for BOTH sessions (exact dates).
    total_count: int = 0
    analyzed_count: int = 0
    candidates: pd.DataFrame = field(default_factory=pd.DataFrame)
    missing: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    source: str = "unverified"
    prices: pd.DataFrame = field(default_factory=pd.DataFrame)
    fallback_count: int = 0
    mismatch_count: int = 0
    cross_checked_count: int = 0
    issues: list[Issue] = field(default_factory=list)
    # Neutral source facts (e.g. "Nasdaq 분석일 종가 미게시 → CNBC로 검증"); never affect status.
    source_notes: list[str] = field(default_factory=list)

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
        return self.status in {WARNING, ERROR}

    @property
    def detection_known(self) -> bool:
        """Whether '0 detected' can be read as 'no drops' for this market."""
        return self.status in {NORMAL, INFO, WARNING}

    @property
    def data_warning_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_data_warning]

    @property
    def info_issues(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == INFO]

    @property
    def warnings(self) -> list[str]:
        return [issue.message for issue in self.data_warning_issues]

    def causes(self, levels: tuple[str, ...]) -> list[str]:
        """Distinct cause labels for the given levels: per-symbol issues are
        counted, market-level issues (no symbol) are named once."""
        symbols: dict[str, int] = {}
        market_level: set[str] = set()
        order: list[str] = []
        for issue in self.issues:
            if issue.level not in levels:
                continue
            if issue.category not in order:
                order.append(issue.category)
            if issue.symbol:
                symbols[issue.category] = symbols.get(issue.category, 0) + 1
            else:
                market_level.add(issue.category)
        labels = []
        for category in order:
            label = CATEGORY_TEXT.get(category, category)
            labels.append(f"{label} {symbols[category]}건" if category in symbols else label)
        return labels


def classify_status(total_count: int, analyzed_count: int, issues: list[Issue] | None = None,
                    failed_coverage_ratio: float = SETTINGS.failed_coverage_ratio) -> str:
    """ERROR only for market-wide failure; otherwise the worst issue level.
    A single missing symbol is a WARNING issue, never ERROR."""
    if total_count < 0 or analyzed_count < 0 or analyzed_count > total_count:
        raise ValueError("invalid coverage counts")
    if total_count == 0 or analyzed_count == 0 or analyzed_count / total_count < failed_coverage_ratio:
        return ERROR
    levels = [issue.level for issue in issues or []]
    return max(levels, key=LEVEL_ORDER.__getitem__, default=NORMAL)


def issues_from_prices(prices: pd.DataFrame, code_column: str, validation_band_pct: float,
                       cross_check_min_ratio: float = 0.5,
                       aggregate_warning_ratio: float = 0.05) -> tuple[list[Issue], list[str]]:
    """Per-symbol issues for everything that can affect the alert outcome.

    Returns (issues, source_notes). A source-level condition on symbols far from
    the threshold cannot change the result, so it is summarized as a neutral
    source note - unless it affects a large share of the market (WARNING).
    """
    issues: list[Issue] = []
    notes: list[str] = []
    if prices.empty:
        return issues, notes
    categories = {
        "PRIMARY_ONLY": "UNVERIFIED", "CROSS_SOURCE_MISMATCH": "MISMATCH",
        "FALLBACK_UNVERIFIED": "FALLBACK_SINGLE", "FALLBACK_VALIDATED": "FALLBACK",
    }
    aggregated: dict[str, int] = {}
    for _, row in prices.iterrows():
        code, name, severity = row[code_column], row.get("company_name", ""), row.get("severity", NORMAL)
        note = row.get("note") or ""
        in_band = min(row["change_pct"], row.get("alt_change_pct") if pd.notna(row.get("alt_change_pct")) else row["change_pct"]) <= validation_band_pct
        special = row.get("special_label") or ""
        if special == "SPECIAL_TRADING_VALIDATED":
            issues.append(Issue(INFO, "SPECIAL_TRADING", row.get("special_note") or "특수거래 확인", code, name))
        elif special == "SPECIAL_TRADING_UNCONFIRMED":
            issues.append(Issue(WARNING, "SPECIAL_UNCONFIRMED", row.get("special_note") or "특수거래 미확인", code, name))
        status = row.get("validation_status")
        category = categories.get(status)
        if status == "CROSS_VALIDATED" and severity == INFO:
            category = "OUTLIER" if row.get("outliers") else "STALE_SECONDARY"
        if category is None or severity == NORMAL:
            continue
        special_note = row.get("special_note") or ""
        if special_note:
            note = note.replace(f"; {special_note}", "").replace(special_note, "")
        if severity == WARNING or in_band:
            issues.append(Issue(severity, category, f"{row.get('validation', '')}: {note}".strip(": "), code, name))
        else:
            aggregated[category] = aggregated.get(category, 0) + 1
    valid = len(prices)
    for category, count in aggregated.items():
        text = f"{CATEGORY_TEXT[category]} {count}종목 (기준선과 먼 종목)"
        if category in ("UNVERIFIED", "MISMATCH", "FALLBACK_SINGLE") and count > valid * aggregate_warning_ratio:
            issues.append(Issue(WARNING, category, f"{text}: 전체의 {count / valid:.0%}로 과다 — 소스 장애 가능"))
        else:
            notes.append(f"{text}, 결과 영향 없음")
    verified = int(prices["cross_checked"].sum()) if "cross_checked" in prices else 0
    if valid and verified / valid < cross_check_min_ratio:
        issues.append(Issue(WARNING, "SOURCE", f"교차검증 완료 종목 부족: {verified}/{valid} — 2차/3차 소스 장애 가능"))
    return issues, notes
