"""Field-level consensus, validation status and severity (source-agnostic)."""
from dataclasses import replace
from datetime import date

import pytest

from helpers import obs
from validation import (CROSS_SOURCE_MISMATCH, CROSS_VALIDATED, FALLBACK_UNVERIFIED, FALLBACK_VALIDATED, INFO,
                        MISMATCH, NORMAL, PRIMARY_MISSING, PRIMARY_ONLY, SINGLE, VERIFIED, WARNING, assess,
                        field_consensus)

PREV, DAY = date(2026, 9, 28), date(2026, 9, 29)


def run(*observations, threshold=-10.0, band=-8.0, tolerance=0.011):
    return assess("T", list(observations), primary="Yahoo", tolerance=tolerance,
                  threshold_pct=threshold, validation_band_pct=band)


def o(source, prev, close):
    return obs("T", source, prev, close, prev_date=PREV, day=DAY)


def test_consensus_rules():
    assert field_consensus([("Yahoo", 10.0), ("Nasdaq", 10.0)], "Yahoo", 0.011).status == VERIFIED
    assert field_consensus([("Yahoo", 10.0)], "Yahoo", 0.011).status == SINGLE
    mismatch = field_consensus([("Yahoo", 10.0), ("Nasdaq", 11.0)], "Yahoo", 0.011)
    assert mismatch.status == MISMATCH and mismatch.value == 10.0
    majority = field_consensus([("Yahoo", 99.0), ("Naver", 10.0), ("Daum", 10.0)], "Yahoo", 0.5)
    assert majority.status == VERIFIED and majority.value == 10.0 and majority.alternatives == [("Yahoo", 99.0)]


def test_all_sources_agree_is_normal():
    result = run(o("Yahoo", 100, 95), o("Nasdaq", 100, 95), o("CNBC", None, 95))
    assert (result.validation_status, result.severity) == (CROSS_VALIDATED, NORMAL)


def test_stale_secondary_verified_by_tertiary_is_not_a_warning():
    # FICO pattern: Nasdaq has no analysis-day row yet, CNBC confirms the close.
    result = run(o("Yahoo", 840.89, 617.87), o("Nasdaq", 840.89, "stale"), o("CNBC", None, 617.87))
    assert result.validation_status == CROSS_VALIDATED and result.detected
    assert result.severity == INFO and "Nasdaq" in result.stale_sources
    assert result.change_pct == pytest.approx(-26.5218, abs=1e-4)


def test_stale_secondary_far_from_threshold_is_normal():
    result = run(o("Yahoo", 100, 99), o("Nasdaq", 100, "stale"), o("CNBC", None, 99))
    assert result.severity == NORMAL


def test_primary_only_near_threshold_is_warning_far_is_info():
    near = run(o("Yahoo", 100, 89), o("Nasdaq", "down", "down"), o("CNBC", None, "down"))
    assert (near.validation_status, near.severity, near.detected) == (PRIMARY_ONLY, WARNING, True)
    far = run(o("Yahoo", 100, 99), o("Nasdaq", "down", "down"), o("CNBC", None, "down"))
    assert (far.validation_status, far.severity) == (PRIMARY_ONLY, INFO)


def test_unresolved_mismatch_detects_if_either_qualifies():
    result = run(o("Yahoo", 100, 91), o("Nasdaq", 100, 89))
    assert result.validation_status == CROSS_SOURCE_MISMATCH and result.detected and result.severity == WARNING


def test_fallback_validated_by_two_other_sources():
    result = run(o("Yahoo", "down", "down"), o("Nasdaq", 100, 85), o("KRX", 100, 85))
    assert result.validation_status == FALLBACK_VALIDATED and result.severity == INFO and result.fallback


def test_fallback_with_single_source_for_a_field_is_unverified():
    # CNBC confirms the close but nothing confirms Nasdaq's previous close.
    result = run(o("Yahoo", "down", "down"), o("Nasdaq", 100, 85), o("CNBC", None, 85))
    assert result.validation_status == FALLBACK_UNVERIFIED and result.severity == WARNING and result.detected


def test_fallback_single_source_is_warning_near_threshold_info_far():
    near = run(o("Yahoo", "down", "down"), o("Nasdaq", 100, 91), o("CNBC", None, "down"))
    assert (near.validation_status, near.severity) == (FALLBACK_UNVERIFIED, WARNING)
    far = run(o("Yahoo", "down", "down"), o("Nasdaq", 100, 99), o("CNBC", None, "down"))
    assert (far.validation_status, far.severity) == (FALLBACK_UNVERIFIED, INFO)


def test_no_value_is_missing_with_reason():
    result = run(o("Yahoo", "down", "down"), o("Nasdaq", "stale", "stale"))
    assert result.validation_status == PRIMARY_MISSING and "Nasdaq" in result.reason


def test_source_contradicted_by_majority_is_discredited_for_both_fields():
    # Halted stock: Yahoo returns a corrupt bar; KRX base sources agree.
    result = run(o("Yahoo", 21086206, 4200), o("Naver", 4200, 4200), o("Daum", 4200, 4200), tolerance=0.5)
    assert result.change_pct == 0 and not result.detected
    assert result.validation_status == FALLBACK_VALIDATED and result.outliers


def test_integrated_price_is_ignored_not_counted_as_stale():
    naver = obs("T", "Naver", 100, 90, prev_date=PREV, day=DAY, close_type="INTEGRATED")
    result = run(o("Yahoo", 100, 95), o("Daum", None, 95), naver)
    assert result.validation_status == CROSS_VALIDATED and not result.stale_sources


@pytest.mark.parametrize("close,detected", [(90.01, False), (90.00, True), (89.99, True)])
def test_threshold_uses_raw_change(close, detected):
    assert run(o("Yahoo", 100, close), o("Nasdaq", 100, close)).detected is detected


def test_supporting_value_resolves_one_against_one_disagreement():
    naver = obs("T", "Naver", None, 6280, prev_date=PREV, day=DAY, close_type="INTEGRATED")
    naver.close = replace(naver.close, corroborative=True)
    result = run(o("Yahoo", 6330, 6280), o("Daum", 6330, 6270), naver, tolerance=0.5)
    assert result.validation_status in (CROSS_VALIDATED, FALLBACK_VALIDATED) and not result.mismatch
    assert result.close == 6280 and result.outliers


def test_supporting_value_breaks_a_tie_against_the_primary():
    """094800 on 2026-10-02: Yahoo alone said 11,531 while the next-day KRX base and
    the integrated last trade both said 7,870. The independent evidence wins."""
    naver_next = obs("T", "Naver 익일 기준가", None, 7870, prev_date=PREV, day=DAY)
    integrated = obs("T", "Naver", None, 7870, prev_date=PREV, day=DAY, close_type="INTEGRATED")
    integrated.close = replace(integrated.close, corroborative=True)
    prev_a = obs("T", "Daum days", 7880, None, prev_date=PREV, day=DAY)
    prev_b = obs("T", "Naver", 7880, None, prev_date=PREV, day=DAY)
    result = run(o("Yahoo", 7880, 11531), naver_next, integrated, prev_a, prev_b, tolerance=0.5)
    assert result.close == 7870 and result.validation_status == FALLBACK_VALIDATED and not result.mismatch
    assert result.outliers and "Yahoo 11,531" in result.outliers[0] and not result.detected


def test_supporting_value_never_overrides_two_regular_sources():
    integrated = obs("T", "Naver", None, 90, prev_date=PREV, day=DAY, close_type="INTEGRATED")
    integrated.close = replace(integrated.close, corroborative=True)
    result = run(o("Yahoo", 100, 95), o("Nasdaq", 100, 95), integrated)
    assert result.close == 95 and result.validation_status == CROSS_VALIDATED
