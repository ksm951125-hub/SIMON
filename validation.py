"""Source-independent price validation.

Each field (previous close, analysis-day close) is validated separately from
the observations of every source for the EXACT requested date:

    VERIFIED  >= 2 sources agree within tolerance
    SINGLE    exactly one source has the value (others stale/unavailable)
    MISMATCH  sources disagree and no agreeing pair includes the chosen value
    MISSING   no source has the value

Symbol validation status (internal):

    CROSS_VALIDATED       both fields VERIFIED, primary among the agreeing sources
    FALLBACK_VALIDATED    both fields VERIFIED, primary missing for a field
    PRIMARY_ONLY          primary valid, cross sources stale/unavailable for a field
    FALLBACK_UNVERIFIED   primary missing, a single other source supplies a field
    CROSS_SOURCE_MISMATCH sources disagree
    PRIMARY_MISSING       no usable value -> symbol missing

Severity per symbol: NORMAL < INFO < WARNING (ERROR is market-level only).
A source that is merely stale/unavailable is never treated as a price error:
it only matters when no other source can confirm the primary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from detector import calculate_change_pct, is_drop
from providers.base import NOT_PROVIDED, OK, STALE_SOURCE, PriceObservation

VERIFIED, SINGLE, MISMATCH, MISSING = "VERIFIED", "SINGLE", "MISMATCH", "MISSING"
CROSS_VALIDATED = "CROSS_VALIDATED"
FALLBACK_VALIDATED = "FALLBACK_VALIDATED"
PRIMARY_ONLY = "PRIMARY_ONLY"
FALLBACK_UNVERIFIED = "FALLBACK_UNVERIFIED"
CROSS_SOURCE_MISMATCH = "CROSS_SOURCE_MISMATCH"
PRIMARY_MISSING = "PRIMARY_MISSING"

NORMAL, INFO, WARNING, ERROR = "NORMAL", "INFO", "WARNING", "ERROR"
DISCREDIT_RATIO = 0.2


@dataclass
class FieldConsensus:
    value: float | None
    status: str
    sources: list[str] = field(default_factory=list)      # sources agreeing with value
    alternatives: list[tuple[str, float]] = field(default_factory=list)  # disagreeing values
    unavailable: list[str] = field(default_factory=list)  # "source: reason" for stale/failed sources

    @property
    def primary_used(self) -> bool:
        return bool(self.sources) and self.sources[0].startswith("*")


def field_consensus(values: list[tuple[str, float]], primary: str, tolerance: float,
                    unavailable: list[str] | None = None, authoritative: str | None = None,
                    supporting: list[tuple[str, float]] | None = None) -> FieldConsensus:
    """values: (source, value) pairs from usable observations.

    ``supporting`` holds corroborative-only values (e.g. a KRX+NXT integrated last
    trade): they never form a group of their own, but each one agreeing with a
    group adds half a vote to it, so independent evidence can break a 1-vs-1 tie
    against the primary. Values disagreeing with the winner are simply ignored.

    The group with the most votes wins (ties prefer the primary). A value backed
    by >= 2 votes' worth of sources is VERIFIED even if a lone source disagrees
    (that source is recorded as an outlier in ``alternatives``). Without any
    agreeing pair the primary's value is kept and the field is a MISMATCH.
    """
    unavailable = unavailable or []
    supporting = supporting or []
    if not values:
        return FieldConsensus(None, MISSING, unavailable=unavailable)

    def agreeing(value: float, pool: list[tuple[str, float]]) -> list[tuple[str, float]]:
        return [(source, other) for source, other in pool if abs(other - value) <= tolerance]

    primary_value = next((value for source, value in values if source == primary), None)

    def rank(value: float):
        members = agreeing(value, values)
        return (len(members) + 0.5 * len(agreeing(value, supporting)), any(source == primary for source, _ in members))

    official = next((value for source, value in values if source == authoritative), None)
    anchor = max((value for _, value in values), key=rank)
    if official is not None:
        anchor = official  # the exchange's own close wins over any majority
    elif (len(agreeing(anchor, values)) + len(agreeing(anchor, supporting)) < 2 and primary_value is not None):
        anchor = primary_value
    members = agreeing(anchor, values)
    support = agreeing(anchor, supporting)
    chosen = official if official is not None else next((value for source, value in members if source == primary),
                                                          members[0][1])
    member_names = {source for source, _ in members}
    alternatives = [(source, value) for source, value in values if source not in member_names]
    names = sorted((("*" + source) if source == primary else source for source, _ in members),
                   key=lambda name: not name.startswith("*")) + [source for source, _ in support]
    if len(members) + len(support) >= 2:
        status = VERIFIED
    else:
        status = MISMATCH if alternatives else SINGLE
    return FieldConsensus(chosen, status, names, alternatives, unavailable)


def _field_inputs(observations: list[PriceObservation], attribute: str, exclude: frozenset[str] = frozenset()
                  ) -> tuple[list[tuple[str, float]], list[tuple[str, float]], list[str]]:
    values, supporting, unavailable = [], [], []
    for observation in observations:
        field_value = getattr(observation, attribute)
        if observation.source in exclude:
            continue
        if field_value.sane and field_value.corroborative:
            # Supporting-only (e.g. KRX+NXT integrated last trade): confirms when equal.
            supporting.append((observation.source, field_value.value))
        elif field_value.usable:
            values.append((observation.source, field_value.value))
        elif field_value.status == OK:
            continue  # a valid non-regular price that may not be compared
        elif field_value.status != NOT_PROVIDED:
            label = "지연" if field_value.status == STALE_SOURCE else field_value.status
            unavailable.append(f"{observation.source} {label}({field_value.detail})"
                               if field_value.detail else f"{observation.source} {label}")
    return values, supporting, unavailable


@dataclass
class Assessment:
    symbol: str
    previous_close: float | None
    close: float | None
    change_pct: float | None
    validation_status: str
    severity: str
    detected: bool = False
    source: str = ""
    validation: str = ""
    fallback: bool = False
    mismatch: bool = False
    cross_checked: bool = False
    stale_sources: list[str] = field(default_factory=list)
    alt_change_pct: float | None = None
    notes: list[str] = field(default_factory=list)
    outliers: list[str] = field(default_factory=list)
    reason: str = ""   # why the symbol is missing (PRIMARY_MISSING)

    @property
    def valid(self) -> bool:
        return self.validation_status != PRIMARY_MISSING


def assess(symbol: str, observations: list[PriceObservation], *, primary: str, tolerance: float,
           threshold_pct: float, validation_band_pct: float, authoritative: str | None = None) -> Assessment:
    """Combine all sources for one symbol into a validated close pair."""
    def consensus(exclude: frozenset[str] = frozenset()) -> tuple[FieldConsensus, FieldConsensus]:
        results = []
        for attribute in ("previous_close", "close"):
            values, supporting, unavailable = _field_inputs(observations, attribute, exclude)
            results.append(field_consensus(values, primary, tolerance, unavailable, authoritative, supporting))
        return results[0], results[1]

    previous, close = consensus()
    # A source grossly contradicted by an agreeing majority on one field (a corrupt
    # bar, e.g. 21,086,206 vs 4,200) is not trusted for the other field either.
    discredited = frozenset(source for field_result in (previous, close) if field_result.status == VERIFIED
                            for source, value in field_result.alternatives
                            if source != authoritative and abs(value / field_result.value - 1) > DISCREDIT_RATIO)
    outliers = [f"{source} {value:,.2f}" for field_result in (previous, close) if field_result.status == VERIFIED
                for source, value in field_result.alternatives]
    if discredited:
        previous, close = consensus(discredited)
    notes = [observation.note for observation in observations
             if observation.note and observation.source not in discredited]
    if previous.value is None or close.value is None:
        missing = [name for name, consensus in (("전일종가", previous), ("분석일 종가", close)) if consensus.value is None]
        causes = previous.unavailable + [item for item in close.unavailable if item not in previous.unavailable]
        reason = f"{'/'.join(missing)} 확보 실패: " + ("; ".join(causes) or "모든 소스에 해당 거래일 데이터 없음")
        return Assessment(symbol, None, None, None, PRIMARY_MISSING, WARNING, reason=reason)

    change = calculate_change_pct(previous.value, close.value)
    fields = (previous, close)
    primary_complete = all(consensus.primary_used for consensus in fields) and primary not in discredited
    verified = all(consensus.status == VERIFIED for consensus in fields)
    any_mismatch = any(consensus.status == MISMATCH for consensus in fields)
    stale = sorted({item.split(" ")[0] for consensus in fields for item in consensus.unavailable})

    alt_changes = []
    if any_mismatch:
        alt_previous = [value for _, value in previous.alternatives] or [previous.value]
        alt_close = [value for _, value in close.alternatives] or [close.value]
        alt_changes = [calculate_change_pct(p, c) for p in alt_previous for c in alt_close]
    # Unresolved disagreement: recall first, detected when either reading qualifies.
    detected = is_drop(change, threshold_pct) or any(is_drop(value, threshold_pct) for value in alt_changes)

    if any_mismatch:
        status = CROSS_SOURCE_MISMATCH
    elif verified:
        status = CROSS_VALIDATED if primary_complete else FALLBACK_VALIDATED
    elif primary_complete:
        status = PRIMARY_ONLY
    else:
        status = FALLBACK_UNVERIFIED

    in_band = min([change] + alt_changes) <= validation_band_pct
    if status == CROSS_VALIDATED:
        severity = INFO if (in_band and (stale or outliers)) else NORMAL
    elif status == FALLBACK_VALIDATED:
        severity = INFO
    else:  # PRIMARY_ONLY, CROSS_SOURCE_MISMATCH, FALLBACK_UNVERIFIED: single/conflicting evidence
        severity = WARNING if in_band else INFO
    if outliers:
        notes.append("다수 소스와 다른 값(제외): " + ", ".join(outliers))

    def describe(label: str, consensus: FieldConsensus) -> str:
        agreed = ",".join(name.lstrip("*") for name in consensus.sources)
        text = f"{label} {consensus.value:,.2f}({agreed})"
        if consensus.alternatives:
            text += " vs " + ",".join(f"{source} {value:,.2f}" for source, value in consensus.alternatives)
        return text

    validation = {
        CROSS_VALIDATED: "교차검증 완료", FALLBACK_VALIDATED: "대체소스 교차검증", PRIMARY_ONLY: "단일소스(교차검증 불가)",
        FALLBACK_UNVERIFIED: "대체소스 단독(검증 불가)", CROSS_SOURCE_MISMATCH: "소스 간 불일치",
    }[status]
    # Per-symbol source details only where they matter: anything not fully
    # cross-validated, or a validated symbol near the threshold.
    explain = status != CROSS_VALIDATED or (in_band and (stale or outliers))
    if explain:
        notes.append(f"{describe('전일', previous)} / {describe('종가', close)}")
    if explain and stale:
        suffix = " → 다른 소스로 검증 완료" if verified else ""
        notes.append(("지연/불가 소스: " if verified else "검증 불가 소스: ")
                     + "; ".join(previous.unavailable + close.unavailable) + suffix)
    display_source = "+".join(dict.fromkeys(name.lstrip("*") for name in previous.sources + close.sources))
    return Assessment(
        symbol, previous.value, close.value, change, status, severity, detected=detected, source=display_source,
        validation=validation, fallback=not primary_complete, mismatch=any_mismatch,
        cross_checked=verified, stale_sources=stale,
        alt_change_pct=min(alt_changes) if alt_changes else None, notes=notes, outliers=outliers)
