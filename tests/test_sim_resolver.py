"""Tests for sim.resolver — the shared fallback table + provenance."""
import math
from datetime import datetime

import pytest

from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.sim.resolver import resolve_shot, SPIN_MODEL_RPM
from openflight.sim.types import IncompleteShotError, PlayerState


def _shot(**kw) -> Shot:
    base = dict(ball_speed_mph=140.0, timestamp=datetime(2026, 4, 26, 12, 0, 0),
                club=ClubType.DRIVER)
    base.update(kw)
    return Shot(**base)


def test_full_measured_shot():
    shot = _shot(
        club_speed_mph=110.0, launch_angle_vertical=12.0,
        launch_angle_horizontal=1.5, spin_rpm=2500.0, spin_confidence=0.9,
        spin_axis_deg=-3.0, club_path_deg=0.5,
    )
    r = resolve_shot(shot, PlayerState())
    assert r.ball_speed_mph == 140.0
    assert r.vla == 12.0
    assert r.hla == 1.5
    assert r.total_spin_rpm == 2500.0
    assert r.spin_axis_deg == -3.0
    assert math.isclose(r.back_spin_rpm, 2500 * math.cos(math.radians(-3.0)), rel_tol=0.01)
    assert math.isclose(r.side_spin_rpm, 2500 * math.sin(math.radians(-3.0)), rel_tol=0.01)
    assert r.club_speed_mph == 110.0
    assert r.club_path_deg == 0.5
    for f in ("ball_speed", "vla", "hla", "total_spin", "spin_axis",
              "back_spin", "side_spin", "club_speed", "club_path"):
        assert r.provenance[f] == "measured", f


def test_missing_vla_falls_back_to_optimal_launch():
    shot = _shot(spin_rpm=2500.0, spin_confidence=0.9, club=ClubType.IRON_7)
    r = resolve_shot(shot, PlayerState())
    assert r.vla == 20.5  # _OPTIMAL_LAUNCH[IRON_7]
    assert r.provenance["vla"] == "estimated"


def test_missing_hla_falls_back_to_zero():
    r = resolve_shot(_shot(spin_rpm=2500.0, spin_confidence=0.9), PlayerState())
    assert r.hla == 0.0
    assert r.provenance["hla"] == "estimated"


def test_low_spin_confidence_uses_model():
    shot = _shot(spin_rpm=2500.0, spin_confidence=0.4, club=ClubType.DRIVER)
    r = resolve_shot(shot, PlayerState())
    assert r.total_spin_rpm == SPIN_MODEL_RPM[ClubType.DRIVER]
    assert r.provenance["total_spin"] == "estimated"


def test_missing_spin_uses_model():
    r = resolve_shot(_shot(club=ClubType.IRON_7), PlayerState())
    assert r.total_spin_rpm == SPIN_MODEL_RPM[ClubType.IRON_7]
    assert r.provenance["total_spin"] == "estimated"


def test_spin_model_covers_every_club():
    assert set(SPIN_MODEL_RPM) == set(ClubType)


def test_putter_without_reliable_spin_sends_putt_spin():
    shot = _shot(club=ClubType.PUTTER, ball_speed_mph=6.0, launch_angle_vertical=1.4,
                 spin_rpm=90.0, spin_confidence=0.3)
    r = resolve_shot(shot, PlayerState())
    assert r.total_spin_rpm == SPIN_MODEL_RPM[ClubType.PUTTER] < 200


def test_missing_spin_axis_falls_back_to_zero():
    shot = _shot(spin_rpm=2500.0, spin_confidence=0.9)  # no spin_axis_deg
    r = resolve_shot(shot, PlayerState())
    assert r.spin_axis_deg == 0.0
    assert r.provenance["spin_axis"] == "estimated"
    assert r.back_spin_rpm == 2500.0
    assert r.side_spin_rpm == 0.0


def test_derived_spin_provenance_estimated_when_either_input_estimated():
    shot = _shot(spin_rpm=2500.0, spin_confidence=0.9)  # axis missing
    r = resolve_shot(shot, PlayerState())
    assert r.provenance["back_spin"] == "estimated"
    assert r.provenance["side_spin"] == "estimated"


def test_missing_club_speed_is_none():
    r = resolve_shot(_shot(spin_rpm=2500.0, spin_confidence=0.9), PlayerState())
    assert r.club_speed_mph is None
    assert r.provenance["club_speed"] == "estimated"


def test_missing_club_path_falls_back_to_zero():
    shot = _shot(spin_rpm=2500.0, spin_confidence=0.9, club_speed_mph=100.0)
    r = resolve_shot(shot, PlayerState())
    assert r.club_path_deg == 0.0
    assert r.provenance["club_path"] == "estimated"


@pytest.mark.parametrize("tier,sent", [("high", True), ("medium", True), ("low", False), ("withheld", False), (None, False)])
def test_camera_attack_angle_is_sent_only_when_confident(tier, sent):
    shot = _shot(spin_rpm=2500.0, spin_confidence=0.9)
    shot.experimental_fused_attack_angle_deg = -3.4
    shot.experimental_fused_attack_angle_confidence = tier
    r = resolve_shot(shot, PlayerState())
    if sent:
        assert r.attack_angle_deg == -3.4
        assert r.provenance["attack_angle"] == "measured"
    else:
        assert r.attack_angle_deg is None
        assert r.provenance["attack_angle"] == "estimated"
    assert r.as_values()["attack_angle"] == r.attack_angle_deg


def test_no_camera_attack_angle_is_none():
    r = resolve_shot(_shot(spin_rpm=2500.0, spin_confidence=0.9), PlayerState())
    assert r.attack_angle_deg is None


def test_carry_provenance_tracks_launch_angle():
    measured = resolve_shot(_shot(launch_angle_vertical=12.0), PlayerState())
    assert measured.provenance["carry"] == "measured"
    estimated = resolve_shot(_shot(), PlayerState())
    assert estimated.provenance["carry"] == "estimated"


def test_missing_ball_speed_raises():
    with pytest.raises(IncompleteShotError):
        resolve_shot(_shot(ball_speed_mph=0.0), PlayerState())


def test_shot_number_uses_player_state():
    ps = PlayerState()
    ps.next_shot_number()  # consume one
    r = resolve_shot(_shot(spin_rpm=2500.0, spin_confidence=0.9), ps)
    assert r.shot_number == 2


def test_carry_prefers_committed_spin_adjusted_carry():
    shot = _shot(launch_angle_vertical=12.0, carry_spin_adjusted=244.0)
    assert resolve_shot(shot, PlayerState()).carry_yards == pytest.approx(244.0)


def test_carry_falls_back_to_table_when_nothing_committed():
    shot = _shot(launch_angle_vertical=12.0)
    assert resolve_shot(shot, PlayerState()).carry_yards == pytest.approx(
        shot.estimated_carry_yards
    )


def test_server_estimated_launch_angles_are_tagged_estimated():
    # server._ensure_user_facing_launch_angles fills missing angles before the
    # sim fan-out and marks them with source "estimated".
    shot = _shot(
        launch_angle_vertical=11.0, launch_angle_vertical_source="estimated",
        launch_angle_horizontal=0.0, launch_angle_horizontal_source="estimated",
    )
    r = resolve_shot(shot, PlayerState())
    assert r.vla == 11.0
    assert r.hla == 0.0
    assert r.provenance["vla"] == "estimated"
    assert r.provenance["hla"] == "estimated"
    assert r.provenance["carry"] == "estimated"


def test_measured_vla_keeps_measured_carry_when_only_hla_is_estimated():
    # A vertical angle radar alone gives a measured VLA, and the server fills in
    # the neutral 0.0 HLA.
    shot = _shot(
        launch_angle_vertical=12.0, launch_angle_vertical_source="radar",
        launch_angle_horizontal=0.0, launch_angle_horizontal_source="estimated",
    )
    r = resolve_shot(shot, PlayerState())
    assert r.provenance["vla"] == "measured"
    assert r.provenance["hla"] == "estimated"
    assert r.provenance["carry"] == "measured"
