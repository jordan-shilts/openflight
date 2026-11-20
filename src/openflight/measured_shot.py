"""A swing the recorder posts to /api/shots/measured, validated.

The camera-triggered swing recorder measures ball speed with the TI radar,
launch angles with the radar or its camera's ball-flight fit (each axis names
its sensor), and optionally club path/attack angle from the camera and club
speed from the OPS243. ``parse_measured_shot`` checks every field before the
kiosk records anything: a wrong type, a non-finite or out-of-range number, or
an unknown enum value raises ValueError naming the field, and the endpoint
refuses the whole post. Every field but ``ball_speed_mph`` is optional, and a
payload without the camera-era fields parses exactly as it always did.

``null`` counts as absent for numbers, enums and text; a true/false field must
be true or false when present, as ``vertical_single_channel`` always was.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import SimpleNamespace

from .rig_setup import CAMERA_AIM_STATUSES

BALL_SPEED_MPH = (5.0, 250.0)
CLUB_SPEED_MPH = (5.0, 200.0)
LAUNCH_DEG = (-30.0, 80.0)
HORIZONTAL_DEG = (-45.0, 45.0)
CLUB_PATH_DEG = (-30.0, 30.0)
ATTACK_ANGLE_DEG = (-30.0, 30.0)
COMPONENT_STD_DEG = (0.0, 90.0)
COHERENCE = (0.0, 1.0)
PHASE_RAD = (-math.pi, math.pi)
# Diagnostics record what a sensor read, including readings the recorder did
# not use, so an angle there is refused only when it cannot be an angle.
DIAGNOSTIC_ANGLE_DEG = (-180.0, 180.0)
FIT_COUNT = (0, 10_000)
FIT_T0_MS = (-1000.0, 1000.0)
FIT_SCORE = (0.0, 1_000_000.0)
CAMERA_STATUS_MAX_CHARS = 40
# Which sensor read a launch axis. Absent means "radar", as before the camera.
LAUNCH_SOURCES = ("radar", "camera")
CLUB_DELIVERY_SOURCES = ("camera",)
CLUB_DELIVERY_CONFIDENCES = ("high", "medium", "low")
CLUB_SPEED_SOURCES = ("ops243",)
CAMERA_LAUNCH_ANGLE_KEYS = ("vla_deg", "hla_deg", "hla_axis_deg", "aim_yaw_deg", "bump_yaw_deg")


def _number(
    payload: dict,
    key: str,
    bounds: tuple[float, float],
    *,
    required: bool = False,
    label: str | None = None,
) -> float | None:
    """One validated number; ``label`` names a nested field (``camera_launch.t0_ms``)."""
    name = label or key
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a number")
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return float(value)


def _count(
    payload: dict, key: str, bounds: tuple[int, int], *, label: str | None = None
) -> int | None:
    """One validated whole number (a count), or None when absent."""
    name = label or key
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _flag(payload: dict, key: str, *, label: str | None = None) -> bool | None:
    """A true/false field, or None when absent; ``null`` is refused."""
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{label or key} must be true or false")
    return value


def _choice(
    payload: dict,
    key: str,
    choices: tuple[str, ...],
    *,
    default: str | None = None,
    label: str | None = None,
) -> str | None:
    """One of ``choices``, or ``default`` when absent."""
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label or key} must be one of: {', '.join(choices)}")
    return value


def _text(payload: dict, key: str, max_chars: int, *, label: str | None = None) -> str | None:
    """A short non-empty string (a status word), or None when absent."""
    name = label or key
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters")
    return value


def _object(payload: dict, key: str) -> dict | None:
    """A nested JSON object, or None when absent."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


@dataclass(frozen=True)
class MeasuredLaunch:
    """The launch angles, split by the sensor that read each axis.

    ``radar`` is LCMF-v1 evidence shaped for the server's
    ``_apply_radar_launch`` (None when no axis came from the radar).
    """

    radar: SimpleNamespace | None = None
    camera_vertical_deg: float | None = None
    camera_horizontal_deg: float | None = None


@dataclass(frozen=True)
class ClubDelivery:
    """Club path and/or attack angle, with what measured them and its tier."""

    club_path_deg: float | None
    attack_angle_deg: float | None
    source: str
    confidence: str


@dataclass(frozen=True)
class MeasuredSwing:
    """Everything a valid post carries."""

    ball_speed_mph: float
    launch: MeasuredLaunch
    horizontal_phase_rad: float | None = None
    camera_launch: dict | None = None
    radar_launch: dict | None = None
    club_delivery: ClubDelivery | None = None
    club_speed_mph: float | None = None
    club_speed_source: str | None = None


def parse_measured_shot(payload: object) -> MeasuredSwing:
    """The validated swing; ValueError names the first bad field."""
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object")
    ball_speed = _number(payload, "ball_speed_mph", BALL_SPEED_MPH, required=True)
    launch = _launch(payload)
    horizontal_phase = _number(payload, "horizontal_phase_rad", PHASE_RAD)
    camera_launch = _camera_launch(payload)
    _check_camera_angles_accepted(launch, camera_launch)
    radar_launch = _radar_launch(payload)
    club_delivery = _club_delivery(payload)
    club_speed = _number(payload, "club_speed_mph", CLUB_SPEED_MPH)
    club_speed_source = _choice(payload, "club_speed_source", CLUB_SPEED_SOURCES)
    if club_speed is None:
        club_speed_source = None
    elif club_speed_source is None:
        raise ValueError("club_speed_source is required with club_speed_mph")
    return MeasuredSwing(
        ball_speed_mph=ball_speed,
        launch=launch,
        horizontal_phase_rad=horizontal_phase,
        camera_launch=camera_launch,
        radar_launch=radar_launch,
        club_delivery=club_delivery,
        club_speed_mph=club_speed,
        club_speed_source=club_speed_source,
    )


def _launch(payload: dict) -> MeasuredLaunch:
    """Launch angles and their evidence.

    Each axis names its sensor (``launch_angle_*_source``, "radar" when absent).
    Either axis may come alone: sideways rests on the ball track alone, so the
    recorder sends it even when the vertical was withheld, and the club/speed
    estimate fills the vertical in. Confidences are derived from the sensors'
    own evidence, never trusted from the caller. The radar evidence fields
    (``vertical_single_channel``, ``vertical_component_std_deg``,
    ``horizontal_coherence``) are validated whenever present but only describe
    an axis the radar read.
    """
    vertical = _number(payload, "launch_angle_vertical", LAUNCH_DEG)
    horizontal = _number(payload, "launch_angle_horizontal", HORIZONTAL_DEG)
    vertical_source = _choice(
        payload, "launch_angle_vertical_source", LAUNCH_SOURCES, default="radar"
    )
    horizontal_source = _choice(
        payload, "launch_angle_horizontal_source", LAUNCH_SOURCES, default="radar"
    )
    single_channel = _flag(payload, "vertical_single_channel")
    component_std_deg = _number(payload, "vertical_component_std_deg", COMPONENT_STD_DEG)
    coherence = _number(payload, "horizontal_coherence", COHERENCE)

    radar_vertical = vertical if vertical_source == "radar" else None
    radar_horizontal = horizontal if horizontal_source == "radar" else None
    radar = None
    if radar_vertical is not None or radar_horizontal is not None:
        radar = SimpleNamespace(
            angle_deg=radar_vertical,
            single_channel=bool(single_channel),
            component_std_deg=component_std_deg,
            horizontal_deg=radar_horizontal,
            horizontal_confidence=coherence,
        )
    return MeasuredLaunch(
        radar=radar,
        camera_vertical_deg=vertical if vertical_source == "camera" else None,
        camera_horizontal_deg=horizontal if horizontal_source == "camera" else None,
    )


def _camera_launch(payload: dict) -> dict | None:
    """The camera ball-flight fit's diagnostics (``camera_launch``).

    The recorder sends them for every clip it fitted, accepted or not; only
    the top-level ``launch_angle_*_source`` says whether a camera angle is
    used. They are kept with the shot for analysis, and ``hits`` and
    ``aim_status`` also set the used camera angles' confidences. Every known
    key is kept (None when absent); unknown keys are dropped, as unknown
    top-level fields are ignored.
    """
    camera = _object(payload, "camera_launch")
    if camera is None:
        return None
    diagnostics: dict = {
        "status": _text(camera, "status", CAMERA_STATUS_MAX_CHARS, label="camera_launch.status"),
        "accepted": _flag(camera, "accepted", label="camera_launch.accepted"),
    }
    for key in CAMERA_LAUNCH_ANGLE_KEYS:
        diagnostics[key] = _number(camera, key, DIAGNOSTIC_ANGLE_DEG, label=f"camera_launch.{key}")
    diagnostics["aim_status"] = _choice(
        camera, "aim_status", CAMERA_AIM_STATUSES, label="camera_launch.aim_status"
    )
    for key in ("hits", "frames"):
        diagnostics[key] = _count(camera, key, FIT_COUNT, label=f"camera_launch.{key}")
    diagnostics["t0_ms"] = _number(camera, "t0_ms", FIT_T0_MS, label="camera_launch.t0_ms")
    diagnostics["fit_score"] = _number(
        camera, "fit_score", FIT_SCORE, label="camera_launch.fit_score"
    )
    return diagnostics


def _check_camera_angles_accepted(launch: MeasuredLaunch, camera_launch: dict | None) -> None:
    """Refuse a camera angle the camera's own fit did not accept.

    With ``camera_launch.accepted`` false its angles are diagnostic only and
    the recorder keeps the top-level launch on the radar; a camera source
    alongside it contradicts that, and guessing which half is right could put
    a rejected fit on screen.
    """
    if camera_launch is None or camera_launch["accepted"] is not False:
        return
    for key, angle in (
        ("launch_angle_vertical_source", launch.camera_vertical_deg),
        ("launch_angle_horizontal_source", launch.camera_horizontal_deg),
    ):
        if angle is not None:
            raise ValueError(f"{key} is camera but camera_launch.accepted is false")


def _radar_launch(payload: dict) -> dict | None:
    """The radar's own launch reading (``radar_launch``), for analysis only.

    The recorder sends it when the camera's reading replaced the radar's. It
    never sets an angle on the shot, so the single-channel gate has nothing to
    judge here.
    """
    radar = _object(payload, "radar_launch")
    if radar is None:
        return None
    return {
        "vla_deg": _number(radar, "vla_deg", DIAGNOSTIC_ANGLE_DEG, label="radar_launch.vla_deg"),
        "hla_deg": _number(radar, "hla_deg", DIAGNOSTIC_ANGLE_DEG, label="radar_launch.hla_deg"),
        "single_channel": _flag(radar, "single_channel", label="radar_launch.single_channel"),
        "component_std_deg": _number(
            radar, "component_std_deg", COMPONENT_STD_DEG, label="radar_launch.component_std_deg"
        ),
        "horizontal_coherence": _number(
            radar, "horizontal_coherence", COHERENCE, label="radar_launch.horizontal_coherence"
        ),
    }


def _club_delivery(payload: dict) -> ClubDelivery | None:
    """Club path and attack angle the recorder measured, or None when neither came.

    Either needs ``club_delivery_source`` and ``club_delivery_confidence``, so
    it is never mislabelled or trusted by default. A source or tier with
    neither angle is validated and then has nothing to describe.
    """
    path = _number(payload, "club_path_deg", CLUB_PATH_DEG)
    attack = _number(payload, "attack_angle_deg", ATTACK_ANGLE_DEG)
    source = _choice(payload, "club_delivery_source", CLUB_DELIVERY_SOURCES)
    confidence = _choice(payload, "club_delivery_confidence", CLUB_DELIVERY_CONFIDENCES)
    if path is None and attack is None:
        return None
    if source is None:
        raise ValueError("club_delivery_source is required with club_path_deg or attack_angle_deg")
    if confidence is None:
        raise ValueError(
            "club_delivery_confidence is required with club_path_deg or attack_angle_deg"
        )
    return ClubDelivery(
        club_path_deg=path, attack_angle_deg=attack, source=source, confidence=confidence
    )
