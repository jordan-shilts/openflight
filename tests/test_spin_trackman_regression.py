"""Yardstick: the spin fallbacks against TrackMan-measured spin.

OpenFlight cannot measure spin reliably (see ``openflight.spin_estimate`` and
docs/how-it-works/rolling-buffer.md), so carry runs on a stand-in. Nothing in
the suite scored those stand-ins against a reference monitor; this does, on the
22 shots of ``session_logs/OpenFlight-Test.Normalized.csv`` whose spin TrackMan
measured rather than estimated.

  median error            7-iron   driver   pitching wedge   all 22
  club-typical (default)   14.6%    20.1%       11.5%         14.3%
  --calculated-spin        22.2%    80.1%       37.4%         36.3%

Calculated spin is fed TrackMan's own ball speed and launch angle here, so
that row is its ceiling. It was fitted on a different player and session
(PW to 3-hybrid, no driver), where it scored 10.5%; on this one it trails the
per-club table on every club. That is the evidence for keeping it opt-in.

Ceilings sit just above the measured error. Lower them when a stand-in
improves; never raise them. If calculated spin ever beats the table here, the
last test fails on purpose: that is the moment to revisit the default.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ANALYSIS = _REPO_ROOT / "scripts" / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

from validate_ballistics import load_trackman  # noqa: E402  (path set up above)

from openflight.ballistics import CLUB_TYPICAL_SPIN_RPM  # noqa: E402
from openflight.clubs import ClubType  # noqa: E402
from openflight.spin_estimate import calculated_spin_rpm  # noqa: E402

TRACKMAN_CSV = _REPO_ROOT / "session_logs" / "OpenFlight-Test.Normalized.csv"

# The three clubs in the session, by TrackMan's own names.
_CLUBS = {"Driver": ClubType.DRIVER, "7 Iron": ClubType.IRON_7, "Pitching Wedge": ClubType.PW}

CLUB_TYPICAL_MEDIAN_ERROR_CEILING_PCT = {
    ClubType.IRON_7: 15.5,
    ClubType.DRIVER: 21.0,
    ClubType.PW: 12.5,
}
CALCULATED_MEDIAN_ERROR_CEILING_PCT = 37.5


def _error_pct(estimate: float, truth: float) -> float:
    return abs(estimate - truth) / truth * 100.0


@pytest.fixture(scope="module")
def measured_spin_shots():
    shots = [
        (_CLUBS[tm.club_raw], tm)
        for tm in load_trackman(TRACKMAN_CSV)
        if tm.spin_rate_type == "Measured"
    ]
    assert len(shots) == 22, "expected 22 shots with TrackMan-measured spin"
    return shots


@pytest.mark.parametrize(
    "club", sorted(CLUB_TYPICAL_MEDIAN_ERROR_CEILING_PCT, key=lambda c: c.value)
)
def test_club_typical_spin_stays_within_its_ceiling(measured_spin_shots, club):
    errors = [
        _error_pct(CLUB_TYPICAL_SPIN_RPM[club], tm.spin_rpm)
        for shot_club, tm in measured_spin_shots
        if shot_club is club
    ]
    assert len(errors) >= 6
    assert statistics.median(errors) <= CLUB_TYPICAL_MEDIAN_ERROR_CEILING_PCT[club]


def test_calculated_spin_stays_within_its_ceiling(measured_spin_shots):
    errors = [
        _error_pct(calculated_spin_rpm(tm.ball_speed_mph, tm.launch_v_deg), tm.spin_rpm)
        for _club, tm in measured_spin_shots
    ]
    assert statistics.median(errors) <= CALCULATED_MEDIAN_ERROR_CEILING_PCT


def test_club_typical_spin_is_the_better_default_on_this_session(measured_spin_shots):
    typical = [
        _error_pct(CLUB_TYPICAL_SPIN_RPM[club], tm.spin_rpm) for club, tm in measured_spin_shots
    ]
    calculated = [
        _error_pct(calculated_spin_rpm(tm.ball_speed_mph, tm.launch_v_deg), tm.spin_rpm)
        for _club, tm in measured_spin_shots
    ]
    assert statistics.median(typical) < statistics.median(calculated)
