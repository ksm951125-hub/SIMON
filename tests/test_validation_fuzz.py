"""Randomized checks of the consensus engine: whatever the sources return
(missing, stale, noisy, wildly wrong, supporting-only), it must never raise and
must keep a few invariants that matter for not missing / not inventing drops."""
import math
import random
from dataclasses import replace
from datetime import date

import pytest

from detector import calculate_change_pct, is_drop
from providers.base import (INVALID_PRICE, NOT_PROVIDED, OK, STALE_SOURCE, UNAVAILABLE, FieldValue, PriceObservation)
from validation import PRIMARY_MISSING, assess

PREV, DAY = date(2026, 10, 7), date(2026, 10, 8)
SOURCES = ("Yahoo", "Nasdaq", "CNBC", "Daum", "Naver")
THRESHOLD, BAND = -10.0, -8.0


def field_value(rng: random.Random, truth: float, mode: str, corroborative: bool, day: date) -> FieldValue:
    if mode == "ok":
        return FieldValue(truth, day, OK, corroborative=corroborative)
    if mode == "noisy":
        return FieldValue(round(truth * (1 + rng.uniform(-0.004, 0.004)), 2), day, OK, corroborative=corroborative)
    if mode == "wrong":
        factor = rng.choice([0.5, 2.0, 10.0, 0.1, 1.3, 0.7, 1000.0])
        return FieldValue(round(truth * factor, 2), day, OK, corroborative=corroborative)
    if mode == "bad":
        return FieldValue(rng.choice([0.0, -5.0, math.nan, math.inf, None]), day, OK, corroborative=corroborative)
    status = {"stale": STALE_SOURCE, "down": UNAVAILABLE, "invalid": INVALID_PRICE, "none": NOT_PROVIDED}[mode]
    return FieldValue(date=day, status=status, detail=mode)


def random_observations(rng: random.Random, prev: float, close: float, modes: tuple[str, ...]):
    observations = []
    for source in SOURCES:
        corroborative = rng.random() < 0.2
        observation = PriceObservation(
            "T", source,
            close=field_value(rng, close, rng.choice(modes), corroborative, DAY),
            previous_close=field_value(rng, prev, rng.choice(modes), corroborative, PREV))
        observation.split_factor = 1.0
        observations.append(observation)
    rng.shuffle(observations)
    return observations


def run(observations):
    return assess("T", observations, primary="Yahoo", tolerance=0.011, threshold_pct=THRESHOLD, validation_band_pct=BAND)


@pytest.mark.parametrize("seed", range(6))
def test_never_raises_and_results_are_coherent(seed):
    rng = random.Random(seed)
    modes = ("ok", "ok", "noisy", "wrong", "bad", "stale", "down", "invalid", "none")
    for _ in range(600):
        prev = round(rng.uniform(1, 5000), 2)
        close = round(prev * rng.uniform(0.5, 1.2), 2)
        result = run(random_observations(rng, prev, close, modes))
        assert result.severity in {"NORMAL", "INFO", "WARNING"}
        if not result.valid:
            assert result.validation_status == PRIMARY_MISSING and result.reason
            continue
        assert result.previous_close > 0 and result.close > 0
        assert math.isfinite(result.previous_close) and math.isfinite(result.close)
        assert result.change_pct == pytest.approx(calculate_change_pct(result.previous_close, result.close))
        if is_drop(result.change_pct, THRESHOLD):
            assert result.detected  # the chosen reading itself qualifies: never lost
        assert result.source  # something always backs the chosen value


@pytest.mark.parametrize("seed", range(4))
def test_unanimous_sources_decide_exactly_by_the_threshold(seed):
    rng = random.Random(100 + seed)
    for _ in range(400):
        prev = round(rng.uniform(1, 5000), 2)
        close = round(prev * rng.uniform(0.6, 1.1), 2)
        present = rng.sample(SOURCES, rng.randint(1, len(SOURCES)))
        observations = []
        for source in SOURCES:
            if source in present:
                observations.append(PriceObservation("T", source, close=FieldValue(close, DAY, OK),
                                                     previous_close=FieldValue(prev, PREV, OK)))
            else:
                observations.append(PriceObservation("T", source, close=field_value(rng, close, "down", False, DAY),
                                                     previous_close=field_value(rng, prev, "stale", False, PREV)))
        result = run(observations)
        assert result.valid and result.close == close and result.previous_close == prev
        assert result.detected is is_drop(calculate_change_pct(prev, close), THRESHOLD)
        assert not result.mismatch


@pytest.mark.parametrize("seed", range(4))
def test_a_corrupt_minority_cannot_hide_or_invent_a_drop(seed):
    """Three agreeing sources plus one or two wildly wrong ones: the agreed values win."""
    rng = random.Random(200 + seed)
    for _ in range(300):
        prev = round(rng.uniform(5, 5000), 2)
        close = round(prev * rng.choice([0.7, 0.85, 0.89, 0.95, 1.02]), 2)
        good = [PriceObservation("T", s, close=FieldValue(close, DAY, OK), previous_close=FieldValue(prev, PREV, OK))
                for s in ("Nasdaq", "Daum", "Naver")]
        bad = [PriceObservation("T", "Yahoo", close=field_value(rng, close, "wrong", False, DAY),
                                previous_close=field_value(rng, prev, "wrong", False, PREV))]
        result = run(good + bad)
        assert result.valid and result.close == close and result.previous_close == prev
        assert result.detected is is_drop(calculate_change_pct(prev, close), THRESHOLD)


def test_split_factor_field_is_inert_without_an_event():
    observation = PriceObservation("T", "Yahoo", close=FieldValue(10.0, DAY, OK), previous_close=FieldValue(11.0, PREV, OK))
    assert replace(observation, split_factor=1.0).split_factor == 1.0
