"""Tests for the accuracy report's truth-free consistency checks."""

import math

import pytest
from accuracy_logs import inclinometer_reading, lcmf

from openflight.accuracy import checks
from openflight.iwr6843.runtime import OPS_TRACK_SPEED_TOLERANCE_FRAC


def _capture(measurement=None, *, ops_speed=100.0, capture_error=None) -> dict:
    return {
        "type": "iwr6843_capture",
        "ball_speed_mph": ops_speed,
        "measurement": measurement,
        "capture_error": capture_error,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, 3.0), (2.5, 2.5), (None, None), ("1.0", None), (True, None), (math.nan, None)],
)
def test_number_accepts_only_finite_numbers(value, expected):
    assert checks.number(value) == expected


def test_number_rejects_infinity():
    assert checks.number(math.inf) is None


def test_angle_source_reads_per_axis_sources():
    shot = {
        "launch_angle_vertical": 18.0,
        "launch_angle_vertical_source": "radar",
        "launch_angle_horizontal": 1.0,
        "launch_angle_horizontal_source": "estimated",
        "angle_source": "radar",
    }

    assert checks.angle_source(shot, "vertical") == "radar"
    assert checks.angle_source(shot, "horizontal") == "estimated"
    assert checks.angle_measured(shot, "vertical")
    assert not checks.angle_measured(shot, "horizontal")


def test_angle_source_falls_back_to_the_shared_source_in_older_logs():
    shot = {"launch_angle_vertical": 11.7, "launch_angle_horizontal": None, "angle_source": "radar"}

    assert checks.angle_source(shot, "vertical") == "radar"
    assert checks.angle_source(shot, "horizontal") is None
    assert not checks.angle_measured(shot, "horizontal")


def test_camera_angles_count_as_measured():
    shot = {"launch_angle_vertical": 20.0, "launch_angle_vertical_source": "camera"}

    assert checks.angle_measured(shot, "vertical")


@pytest.mark.parametrize(
    ("ball", "club", "expected"),
    [(150.0, 100.0, False), (151.0, 100.0, True), (100.0, None, None), (100.0, 0.0, None)],
)
def test_club_speed_over_smash_limit_uses_the_live_limit(ball, club, expected):
    shot = {"ball_speed_mph": ball, "club_speed_mph": club}

    assert checks.club_speed_over_smash_limit(shot) is expected


def test_mount_pitch_reads_the_inclinometer_block():
    assert checks.mount_pitch_deg({"inclinometer": inclinometer_reading(1.25)}) == 1.25
    assert checks.mount_pitch_deg({"inclinometer": {"applied": False, "status": "stale"}}) is None
    assert checks.mount_pitch_deg({"inclinometer": None}) is None


def test_iwr6843_status_explains_captures_without_a_measurement():
    assert checks.iwr6843_status(_capture(capture_error="no capture matched")) == "capture_error"
    assert checks.iwr6843_status(_capture(None)) == "no_measurement"
    assert checks.iwr6843_status(_capture(lcmf("rejected_no_ball"))) == "rejected_no_ball"
    assert checks.iwr6843_accepted(_capture(lcmf("accepted_ops_guided")))
    assert not checks.iwr6843_accepted(_capture(lcmf("rejected_no_ball")))


def test_ops_track_speed_ratio_divides_track_by_ops_speed():
    assert checks.ops_track_speed_ratio(_capture(lcmf(track_speed=90.0))) == pytest.approx(0.9)
    assert checks.ops_track_speed_ratio(_capture(lcmf(track_speed=None))) is None
    assert checks.ops_track_speed_ratio(_capture(lcmf(), ops_speed=0.0)) is None
    assert checks.ops_track_speed_ratio(_capture(None)) is None


def test_speed_mismatch_matches_the_live_recovery_threshold():
    assert not checks.speed_mismatch(1.0 + OPS_TRACK_SPEED_TOLERANCE_FRAC)
    assert checks.speed_mismatch(1.0 + OPS_TRACK_SPEED_TOLERANCE_FRAC + 1e-6)
    assert checks.speed_mismatch(1.0 - OPS_TRACK_SPEED_TOLERANCE_FRAC - 1e-6)


def test_agreement_is_only_reported_for_corroborated_angles():
    corroborated = _capture(lcmf(agreement_deg=0.8))
    single = _capture(lcmf(single_channel=True, agreement_deg=0.0))
    rejected = _capture(lcmf("rejected_no_ball"))

    assert checks.lcmf_single_channel(corroborated) is False
    assert checks.lcmf_agreement_deg(corroborated) == pytest.approx(0.8)
    assert checks.lcmf_single_channel(single) is True
    assert checks.lcmf_agreement_deg(single) is None
    assert checks.lcmf_single_channel(rejected) is None
    assert checks.lcmf_agreement_deg(rejected) is None
