"""Regression guard: D-plane face angle and spin axis against committed TrackMan data.

``session_logs/OpenFlight-Test.Normalized.csv`` carries TrackMan's own launch
direction, launch angle, club path, attack angle, face angle, dynamic loft and
spin axis for 24 shots. Feeding TrackMan's launch and delivery through the
production ``estimate_club_delivery`` and comparing with what TrackMan measured
isolates the D-plane model from OpenFlight's sensor noise: these are the best
numbers the model can reach, not what a live rig will show.

Budgets sit just above the measured error to absorb float drift. Lower them if
the model improves; never raise them.
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ANALYSIS = _REPO_ROOT / "scripts" / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

from validate_ballistics import load_trackman  # noqa: E402  (path set up above)

from openflight.clubs import ClubType  # noqa: E402
from openflight.d_plane import estimate_club_delivery  # noqa: E402
from openflight.launch_monitor import Shot  # noqa: E402

TRACKMAN_CSV = _REPO_ROOT / "session_logs" / "OpenFlight-Test.Normalized.csv"

# The three clubs in the session, by TrackMan's own names. Spelled out here
# because the validator's normaliser leaves "Pitching Wedge" unmapped.
_CLUBS = {"Driver": ClubType.DRIVER, "7 Iron": ClubType.IRON_7, "Pitching Wedge": ClubType.PW}


def _club_type(raw: str) -> ClubType:
    return _CLUBS[raw]


#                        measured mean abs error (deg)
#   club            face angle   spin axis   old rule (launch dir - path)
#   7-iron             0.18        0.59          4.51
#   pitching wedge     0.03        0.14          1.06
#   driver             0.12       17.61          9.64   <- gear effect, see below
FACE_ANGLE_BUDGET_DEG = 0.3
SPIN_AXIS_BUDGET_DEG = {ClubType.IRON_7: 0.8, ClubType.PW: 0.3}
# The driver in this session was struck 5-27 mm off-centre. The D-plane cannot
# see that, so its spin axis is far out; this floor documents why the spin axis
# of a wood is withheld by default. If it ever drops below the floor, strike
# location is being accounted for and the default can be revisited.
DRIVER_SPIN_AXIS_ERROR_FLOOR_DEG = 10.0


def _delivery_rows():
    rows = []
    for tm in load_trackman(TRACKMAN_CSV):
        needed = (
            tm.launch_h_deg,
            tm.launch_v_deg,
            tm.club_path_deg,
            tm.attack_angle_deg,
            tm.face_angle_deg,
            tm.spin_axis_deg,
        )
        if any(value is None for value in needed):
            continue
        shot = Shot(
            ball_speed_mph=tm.ball_speed_mph,
            timestamp=datetime(2026, 5, 6, 15, 0, 0),
            club=_club_type(tm.club_raw),
            launch_angle_vertical=tm.launch_v_deg,
            launch_angle_vertical_source="radar",
            launch_angle_horizontal=tm.launch_h_deg,
            launch_angle_horizontal_source="radar",
            launch_angle_horizontal_confidence=1.0,
            club_path_deg=tm.club_path_deg,
            club_angle_deg=tm.attack_angle_deg,
        )
        rows.append((tm, estimate_club_delivery(shot, include_gear_effect_clubs=True)))
    return rows


@pytest.fixture(scope="module")
def delivery_rows():
    rows = _delivery_rows()
    assert len(rows) == 23, "one of the 24 TrackMan rows has no face angle; expected 23 usable"
    return rows


def _mean_abs(values):
    return statistics.fmean(abs(value) for value in values)


def test_face_angle_matches_trackman(delivery_rows):
    errors = [delivery.face_angle_deg - tm.face_angle_deg for tm, delivery in delivery_rows]
    assert _mean_abs(errors) <= FACE_ANGLE_BUDGET_DEG


@pytest.mark.parametrize("club", sorted(SPIN_AXIS_BUDGET_DEG, key=lambda c: c.value))
def test_iron_and_wedge_spin_axis_matches_trackman(delivery_rows, club):
    errors = [
        delivery.spin_axis_deg - tm.spin_axis_deg
        for tm, delivery in delivery_rows
        if _club_type(tm.club_raw) is club
    ]
    assert len(errors) >= 6
    assert _mean_abs(errors) <= SPIN_AXIS_BUDGET_DEG[club]


def test_iron_and_wedge_spin_axis_beats_launch_direction_minus_path(delivery_rows):
    """The rule this replaced read about 40% of the true tilt on an iron."""
    d_plane_errors, old_rule_errors = [], []
    for tm, delivery in delivery_rows:
        if _club_type(tm.club_raw) not in SPIN_AXIS_BUDGET_DEG:
            continue
        d_plane_errors.append(delivery.spin_axis_deg - tm.spin_axis_deg)
        old_rule_errors.append((tm.launch_h_deg - tm.club_path_deg) - tm.spin_axis_deg)
    assert _mean_abs(d_plane_errors) < _mean_abs(old_rule_errors) / 4


def test_draw_or_fade_matches_trackman_for_irons_and_wedges(delivery_rows):
    for tm, delivery in delivery_rows:
        if _club_type(tm.club_raw) not in SPIN_AXIS_BUDGET_DEG:
            continue
        if abs(tm.spin_axis_deg) < 1.0:
            continue  # within a degree of straight the sign carries no information
        assert (delivery.spin_axis_deg > 0) == (tm.spin_axis_deg > 0), tm.shot_index


def test_driver_spin_axis_is_not_recoverable_without_strike_location(delivery_rows):
    errors = [
        delivery.spin_axis_deg - tm.spin_axis_deg
        for tm, delivery in delivery_rows
        if _club_type(tm.club_raw) is ClubType.DRIVER
    ]
    assert len(errors) == 8
    assert _mean_abs(errors) >= DRIVER_SPIN_AXIS_ERROR_FLOOR_DEG


def test_driver_spin_axis_is_withheld_by_default():
    tm = next(t for t in load_trackman(TRACKMAN_CSV) if _club_type(t.club_raw) is ClubType.DRIVER)
    shot = Shot(
        ball_speed_mph=tm.ball_speed_mph,
        timestamp=datetime(2026, 5, 6, 15, 0, 0),
        club=ClubType.DRIVER,
        launch_angle_vertical=tm.launch_v_deg,
        launch_angle_vertical_source="radar",
        launch_angle_horizontal=tm.launch_h_deg,
        launch_angle_horizontal_source="radar",
        launch_angle_horizontal_confidence=1.0,
        club_path_deg=tm.club_path_deg,
        club_angle_deg=tm.attack_angle_deg,
    )
    assert estimate_club_delivery(shot).spin_axis_deg is None
