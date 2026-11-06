"""Tests for the accuracy report's statistics and session shift detection."""

import pytest

from openflight.accuracy import stats


def _spread(level: float, count: int, step: float = 0.01) -> list[float]:
    """``count`` values alternating just below and above ``level``."""
    return [level + (step if index % 2 else -step) for index in range(count)]


def _rate(hits: int, misses: int) -> list[float]:
    return [1.0] * hits + [0.0] * misses


def test_median_and_percentile_handle_empty_input():
    assert stats.median([]) is None
    assert stats.percentile([], 90) is None
    assert stats.median([3.0, 1.0, 2.0]) == 2.0
    assert stats.percentile([0.0, 10.0], 90) == pytest.approx(9.0)


@pytest.mark.parametrize(
    ("successes", "trials", "expected"),
    [(5, 10, (0.2366, 0.7634)), (10, 10, (0.7225, 1.0)), (0, 10, (0.0, 0.2775))],
)
def test_wilson_interval_matches_published_values(successes, trials, expected):
    low, high = stats.wilson_interval(successes, trials)

    assert low == pytest.approx(expected[0], abs=1e-4)
    assert high == pytest.approx(expected[1], abs=1e-4)


def test_wilson_interval_is_none_without_trials():
    assert stats.wilson_interval(0, 0) is None


def test_center_is_a_rate_for_binary_series_and_a_median_otherwise():
    assert stats.center([1.0, 1.0, 0.0, 1.0], binary=True) == 0.75
    assert stats.center([1.0, 2.0, 100.0], binary=False) == 2.0


def test_no_shift_between_matching_sessions():
    groups = [_spread(1.0, 40), _spread(1.0, 30), _spread(1.0, 25)]

    assert stats.find_session_shifts(groups, binary=False, min_shift=0.02) == []


def test_a_shifted_session_is_found_against_the_sessions_before_it():
    groups = [_spread(1.0, 40), _spread(1.0, 30), _spread(0.9, 20)]

    (shift,) = stats.find_session_shifts(groups, binary=False, min_shift=0.02)

    assert shift.session_index == 2
    assert shift.reference == pytest.approx(1.0)
    assert shift.level == pytest.approx(0.9)
    assert shift.p_value < 1e-6
    assert (shift.samples, shift.reference_samples) == (20, 70)


def test_a_significant_change_smaller_than_min_shift_is_not_reported():
    before, after = _spread(1.0, 400), _spread(1.012, 400)

    assert stats.shift_p_value(before, after, binary=False) < stats.SHIFT_ALPHA
    assert stats.find_session_shifts([before, after], binary=False, min_shift=0.02) == []


def test_sessions_with_too_few_samples_are_left_out():
    groups = [_spread(1.0, 40), _spread(0.5, stats.MIN_SESSION_SAMPLES - 1), _spread(1.0, 20)]

    assert stats.find_session_shifts(groups, binary=False, min_shift=0.02) == []


def test_a_single_session_has_nothing_to_shift_from():
    assert stats.find_session_shifts([_spread(0.5, 50)], binary=False, min_shift=0.0) == []


def test_the_shifted_session_starts_the_next_reference():
    groups = [_spread(1.0, 40), _spread(0.9, 30), _spread(0.9, 30), _spread(1.0, 30)]

    shifts = stats.find_session_shifts(groups, binary=False, min_shift=0.02)

    assert [(shift.session_index, round(shift.level, 3)) for shift in shifts] == [
        (1, 0.9),
        (3, 1.0),
    ]
    assert shifts[1].reference_samples == 60


def test_a_rate_drop_is_found_with_fishers_exact_test():
    (shift,) = stats.find_session_shifts([_rate(98, 2), _rate(28, 12)], binary=True, min_shift=0.10)

    assert shift.reference == pytest.approx(0.98)
    assert shift.level == pytest.approx(0.70)
    assert shift.p_value < stats.SHIFT_ALPHA


def test_a_rate_change_within_chance_is_not_reported():
    assert stats.find_session_shifts([_rate(95, 5), _rate(36, 4)], binary=True, min_shift=0.0) == []


def test_a_few_wild_readings_do_not_make_a_shift():
    before, after = _spread(1.0, 40), _spread(1.0, 40)
    after[:3] = [50.0, 50.0, 50.0]

    assert stats.shift_p_value(before, after, binary=False) > 0.05


def test_identical_measurements_give_no_evidence_of_a_shift():
    assert stats.shift_p_value([0.5] * 20, [0.5] * 20, binary=False) == 1.0
