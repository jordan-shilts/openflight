"""Summary statistics and session-to-session shift tests for the accuracy report."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy import stats as scipy_stats

SHIFT_ALPHA = 0.001
MIN_SESSION_SAMPLES = 10


@dataclass(frozen=True)
class SessionShift:
    """A session whose values differ from the pooled sessions before it."""

    session_index: int
    reference: float
    level: float
    p_value: float
    samples: int
    reference_samples: int


def median(values: Sequence[float]) -> float | None:
    """Median, or None without values."""
    return float(np.median(values)) if len(values) else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linearly interpolated percentile, or None without values."""
    return float(np.percentile(values, q)) if len(values) else None


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a proportion, 95% by default."""
    if trials <= 0:
        return None
    rate = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    midpoint = (rate + z2 / (2.0 * trials)) / denominator
    spread = rate * (1.0 - rate) / trials + z2 / (4.0 * trials * trials)
    half_width = z * math.sqrt(spread) / denominator
    return max(0.0, midpoint - half_width), min(1.0, midpoint + half_width)


def center(values: Sequence[float], binary: bool) -> float:
    """Rate of a 0/1 series, median of a measurement series."""
    return float(np.mean(values) if binary else np.median(values))


def shift_p_value(reference: Sequence[float], sample: Sequence[float], binary: bool) -> float:
    """Two-sided p-value for the hypothesis that both groups share one distribution.

    Rates use Fisher's exact test; measurements use the Mann-Whitney U test,
    which only ranks values, so a few wild readings cannot decide it.
    """
    if binary:
        hits_before = int(sum(reference))
        hits = int(sum(sample))
        table = [[hits_before, len(reference) - hits_before], [hits, len(sample) - hits]]
        _, p_value = scipy_stats.fisher_exact(table)
    else:
        _, p_value = scipy_stats.mannwhitneyu(reference, sample, alternative="two-sided")
    p_value = float(p_value)
    return p_value if math.isfinite(p_value) else 1.0


def find_session_shifts(
    groups: Sequence[Sequence[float]],
    *,
    binary: bool,
    min_shift: float,
) -> list[SessionShift]:
    """Compare each session with the pooled sessions since the last shift.

    Sessions with fewer than ``MIN_SESSION_SAMPLES`` values are left out. A
    shift needs ``p < SHIFT_ALPHA`` and a change in center of at least
    ``min_shift``; the session that shifted then starts the next reference.
    """
    shifts: list[SessionShift] = []
    reference: list[float] = []
    for index, values in enumerate(groups):
        if len(values) < MIN_SESSION_SAMPLES:
            continue
        if len(reference) >= MIN_SESSION_SAMPLES:
            before = center(reference, binary)
            after = center(values, binary)
            p_value = shift_p_value(reference, values, binary)
            if p_value < SHIFT_ALPHA and abs(after - before) >= min_shift:
                shifts.append(
                    SessionShift(index, before, after, p_value, len(values), len(reference))
                )
                reference = []
        reference.extend(values)
    return shifts
