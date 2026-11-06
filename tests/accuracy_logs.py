"""Write session logs for the accuracy tests through the production logger.

Building fixtures with ``SessionLogger``, ``Shot``, ``LCMFResult`` and the
inclinometer value objects keeps them in the exact format the server writes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from openflight.inclinometer.models import OrientationSnapshot, SnapshotSelection
from openflight.iwr6843.lcmf import LCMFResult
from openflight.launch_monitor import ClubType, Shot
from openflight.session_logger import SessionLogger


def make_shot(
    *,
    ball_speed: float = 100.0,
    club_speed: Optional[float] = 75.0,
    vertical: Optional[float] = 18.0,
    vertical_source: Optional[str] = "radar",
    horizontal: Optional[float] = 1.0,
    horizontal_source: Optional[str] = "radar",
    raw_ball_speed: Optional[float] = None,
    spin_source: Optional[str] = None,
    inclinometer: Optional[dict] = None,
) -> Shot:
    """A 7-iron shot with only the fields the accuracy report reads."""
    return Shot(
        ball_speed_mph=ball_speed,
        timestamp=datetime(2026, 11, 6, 10, 0, 0),
        club=ClubType.IRON_7,
        club_speed_mph=club_speed,
        launch_angle_vertical=vertical,
        launch_angle_vertical_source=vertical_source,
        launch_angle_horizontal=horizontal,
        launch_angle_horizontal_source=horizontal_source,
        ball_speed_raw_mph=raw_ball_speed,
        spin_source=spin_source,
        inclinometer=inclinometer,
    )


def inclinometer_reading(pitch_deg: float, *, applied: bool = True, status: str = "ok") -> dict:
    """The per-shot inclinometer block the server attaches to a shot."""
    snapshot = OrientationSnapshot(
        timestamp=0.0,
        x_g=0.0,
        y_g=0.0,
        z_g=1.0,
        gravity_g=1.0,
        raw_pitch_deg=pitch_deg,
        calibrated_pitch_deg=pitch_deg,
        pitch_std_deg=0.02,
        sample_count=50,
    )
    data = SnapshotSelection(snapshot=snapshot, status=status, age_s=0.05).to_dict()
    data["applied"] = applied
    return data


def lcmf(
    status: str = "accepted",
    *,
    track_speed: Optional[float] = 100.0,
    agreement_deg: float = 0.5,
    single_channel: bool = False,
) -> dict:
    """An LCMF-v1 measurement as logged in an ``iwr6843_capture`` entry."""
    accepted = status.startswith("accepted")
    return LCMFResult(
        status=status,
        angle_deg=18.0 if accepted else None,
        components_deg={"two8": 17.5, "four4_path_tdm": 18.5},
        component_std_deg=agreement_deg if accepted else None,
        single_channel=single_channel,
        track_speed_mph=track_speed,
    ).to_dict()


def write_session(
    log_dir: Path,
    name: str,
    *,
    mode: str = "rolling-buffer",
    triggers: Iterable[tuple[bool, str]] = (),
    shots: Iterable[Shot] = (),
    captures: Iterable[tuple[float, Optional[dict], Optional[str]]] = (),
    error_components: Iterable[Optional[str]] = (),
    sim_sends: Iterable[tuple[str, int]] = (),
) -> Path:
    """Write one session; ``name`` keeps files created in the same second apart.

    ``captures`` are ``(ops_ball_speed_mph, measurement, capture_error)``.
    """
    logger = SessionLogger(log_dir=log_dir, location=name)
    logger.start_session(mode=mode, trigger_type=None if mode == "mock" else "sound")
    for accepted, reason in triggers:
        logger.log_trigger_event(trigger_type="sound", accepted=accepted, reason=reason)
    for shot in shots:
        logger.log_shot(shot, pipeline_ms={"iwr6843": 900.0, "kld7": None})
    for shot_number, (ops_speed, measurement, capture_error) in enumerate(captures, start=1):
        logger.log_iwr6843_capture(
            shot_number=shot_number,
            shot_timestamp=None,
            trigger_timestamp=None,
            capture_path=None,
            capture_bytes=0,
            dump_duration_s=None,
            capture_error=capture_error,
            ball_speed_mph=ops_speed,
            measurement=measurement,
        )
    for component in error_components:
        context = {"component": component} if component else None
        logger.log_error("processing failed", context=context)
    for target, sim_shot_number in sim_sends:
        logger.log_sim_send(target=target, shot_number=sim_shot_number, provenance={})
    logger.end_session()
    return logger.session_path
