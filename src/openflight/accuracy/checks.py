"""Consistency checks on logged shots and IWR6843 captures that need no reference.

Every check returns None when the entry lacks the fields it needs, so older
session formats and builds without a given sensor are skipped, not counted.
"""

from __future__ import annotations

import math

from ..iwr6843.runtime import OPS_TRACK_SPEED_TOLERANCE_FRAC
from ..launch_monitor import compute_smash_factor

MEASURED_ANGLE_SOURCES = frozenset({"radar", "camera"})
ANGLE_AXES = ("vertical", "horizontal")


def number(value: object) -> float | None:
    """A finite float, or None for missing, non-numeric, NaN or infinite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def angle_source(shot: dict, axis: str) -> str | None:
    """Source of one launch-angle axis, or None when the shot has no angle on it.

    Logs before per-axis sources existed only carry ``angle_source``.
    """
    if number(shot.get(f"launch_angle_{axis}")) is None:
        return None
    return shot.get(f"launch_angle_{axis}_source") or shot.get("angle_source") or "unknown"


def angle_measured(shot: dict, axis: str) -> bool:
    """Whether the axis came from a sensor rather than a fallback estimate."""
    return angle_source(shot, axis) in MEASURED_ANGLE_SOURCES


def club_speed_over_smash_limit(shot: dict) -> bool | None:
    """True when the measured club speed implies an impossible smash factor."""
    ball = number(shot.get("ball_speed_mph"))
    club = number(shot.get("club_speed_mph"))
    if ball is None or club is None or club <= 0.0:
        return None
    return compute_smash_factor(ball, club) is None


def mount_pitch_deg(shot: dict) -> float | None:
    """Stable enclosure pitch the inclinometer reported for this shot."""
    inclinometer = shot.get("inclinometer")
    if not isinstance(inclinometer, dict):
        return None
    return number(inclinometer.get("calibrated_pitch_deg"))


def iwr6843_status(capture: dict) -> str:
    """LCMF status, or why the capture produced no measurement."""
    if capture.get("capture_error"):
        return "capture_error"
    measurement = capture.get("measurement")
    if not isinstance(measurement, dict):
        return "no_measurement"
    return str(measurement.get("status") or "unknown")


def iwr6843_accepted(capture: dict) -> bool:
    """Whether LCMF produced a launch angle for this capture."""
    return iwr6843_status(capture).startswith("accepted")


def ops_track_speed_ratio(capture: dict) -> float | None:
    """IWR6843 range-track speed divided by the OPS243 speed it was matched to."""
    measurement = capture.get("measurement")
    if not isinstance(measurement, dict):
        return None
    track = number(measurement.get("track_speed_mph"))
    ops = number(capture.get("ball_speed_mph"))
    if track is None or ops is None or ops <= 0.0:
        return None
    return track / ops


def speed_mismatch(ratio: float) -> bool:
    """The live runtime's rule for starting OPS-guided recovery."""
    return abs(ratio - 1.0) > OPS_TRACK_SPEED_TOLERANCE_FRAC


def lcmf_single_channel(capture: dict) -> bool | None:
    """Whether an accepted angle rests on a single LCMF channel."""
    if not iwr6843_accepted(capture):
        return None
    return bool(capture["measurement"].get("single_channel"))


def lcmf_agreement_deg(capture: dict) -> float | None:
    """Standard deviation between the LCMF channels behind a corroborated angle."""
    if lcmf_single_channel(capture) is not False:
        return None
    return number(capture["measurement"].get("component_std_deg"))
