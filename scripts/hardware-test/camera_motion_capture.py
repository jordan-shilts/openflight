#!/usr/bin/env python3
"""Capture swings with the OV9281 alone: sudden motion in the hitting zone triggers.

Use this before a sound trigger or radar is wired. It runs OpenFlight's camera
runtime (ring buffer, automatic exposure, clip format) and stands a motion
trigger in for the sound trigger: when the clubhead flashes through the hitting
zone, the ring freezes and the clip around impact is saved like a shot capture.

Run it with the Pi's system Python, which has Picamera2:

    uv run --no-project --python /usr/bin/python3 \\
      python scripts/hardware-test/camera_motion_capture.py --rotate-180

Each clip directory gets frames.npz (the shot-capture format) and sheet.jpg,
the frames around the biggest change with the hitting zone outlined. aim.jpg,
saved at start, shows the first frame and the zone for aiming. --calibrate
prints what the trigger sees once a second, for tuning --min-fraction.

With --iwr6843 each trigger also captures the TI radar. --trigger sound starts
each reading from the sound sensor instead (SEN-14262 GATE on GPIO 17): it fires
on the strike itself, in any light, and the camera saves a clip around it
without ever triggering on its own. --rig-setup-url follows the
kiosk's Setup tab on every swing: the ball's apparent size and the case tilt go to
the kiosk, and the geometry for that swing comes back. --status-to tells the kiosk
when the radar is ready and when it is busy with a dump, so the Live screen can
say Ready or Measuring. --ops243-club-speed (off until tested on the Pi) also
dumps the OPS243 speed radar on every trigger the TI takes and sends its club
speed when its ball speed agrees with the TI's (openflight/ops243_club_speed.py).
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable, NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402

from openflight.camera.aim import AimReading, CameraAim  # noqa: E402
from openflight.camera.ball_leaves_trigger import BallLeavesTrigger  # noqa: E402
from openflight.camera.ball_size import measure_ball_in_clip  # noqa: E402
from openflight.camera.capture_runtime import (  # noqa: E402
    CameraCaptureRuntime,
    CameraCaptureSettings,
)
from openflight.camera.flight_fit import (  # noqa: E402
    QUIET_FRAMES,
    CameraGeometry,
    CameraLaunch,
    load_clip,
    measure_launch,
)
from openflight.camera.motion_trigger import (  # noqa: E402
    MotionTrigger,
    MotionTriggerSettings,
)


def parse_roi(value: str) -> tuple[float, float, float, float]:
    """x,y,width,height as fractions of the frame."""
    parts = [float(part) for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--roi needs x,y,width,height fractions")
    return tuple(parts)  # type: ignore[return-value]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=400)
    parser.add_argument("--fps", type=float, default=240.0)
    parser.add_argument("--pre-ms", type=float, default=150.0)
    parser.add_argument("--post-ms", type=float, default=100.0)
    parser.add_argument("--exposure-us", type=int, default=1000, help="Starting exposure")
    parser.add_argument("--gain", type=float, default=4.0, help="Starting analogue gain")
    parser.add_argument(
        "--fixed-exposure",
        action="store_true",
        help="Keep --exposure-us/--gain instead of running automatic exposure",
    )
    parser.add_argument("--rotate-180", action="store_true", help="Camera mounted upside down")
    defaults = MotionTriggerSettings()
    parser.add_argument("--roi", type=parse_roi, default=defaults.roi, help="Hitting zone x,y,w,h")
    parser.add_argument("--min-fraction", type=float, default=defaults.min_fraction)
    parser.add_argument("--min-delta", type=float, default=defaults.min_delta)
    parser.add_argument("--cooldown", type=float, default=defaults.cooldown_s)
    parser.add_argument(
        "--trigger",
        choices=("camera", "ball", "sound"),
        default="camera",
        help=(
            "What starts each reading: camera motion in the hitting zone, the resting "
            "ball suddenly leaving its spot (ignores waggles), or the "
            "sound sensor's GATE on --sound-pin (needs --iwr6843)"
        ),
    )
    parser.add_argument(
        "--sound-pin", type=int, default=17, help="BCM pin the sound sensor's GATE drives"
    )
    parser.add_argument("--captures", type=int, default=0, help="Stop after N clips (0: no limit)")
    parser.add_argument(
        "--duration", type=float, default=0.0, help="Stop after N seconds (0: none)"
    )
    parser.add_argument("--calibrate", action="store_true", help="Print trigger stats every second")
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path.home() / "openflight_sessions" / "camera_motion",
    )
    radar = parser.add_argument_group(
        "TI IWR6843 angle radar",
        "Each camera trigger also freezes the radar ring; the radar-only pipeline "
        "then reports ball speed from the range walk and, with --tee-m, launch angle.",
    )
    radar.add_argument("--iwr6843", action="store_true", help="Capture the TI radar on triggers")
    radar.add_argument("--iwr6843-port", default=None, help="TI CLI port (default: detect)")
    radar.add_argument(
        "--iwr6843-config",
        type=Path,
        default=REPO_ROOT / "config" / "iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg",
        help="Capture profile (default: the fast ball-only one, about 2.4 s per swing)",
    )
    radar.add_argument(
        "--horizontal-method",
        choices=("colocated", "midpoint"),
        default="colocated",
        help=(
            "Horizontal launch estimator: colocated ignores floor reflections and needs "
            "the Setup tab's straight line set; midpoint is upstream HLCMF-v1"
        ),
    )
    radar.add_argument(
        "--horizontal-coherence-min",
        type=float,
        default=0.90,
        help=(
            "Phase coherence a sideways reading needs (0-1). 0.80 roughly triples "
            "sideways coverage on the 2026-11-19 garage swings while keeping the "
            "readings within the golfer's start-line spread."
        ),
    )
    radar.add_argument(
        "--iwr6843-cal",
        type=Path,
        default=REPO_ROOT / "config" / "iwr6843_calibration_reference.json",
    )
    radar.add_argument("--tee-m", type=float, default=None, help="Radar-to-ball slant range")
    radar.add_argument("--net-m", type=float, default=None, help="Radar-to-net range")
    radar.add_argument("--radar-height-m", type=float, default=None)
    radar.add_argument("--ball-height-m", type=float, default=None)
    radar.add_argument("--tilt-deg", type=float, default=None, help="Radar mount tilt")
    radar.add_argument(
        "--club", default=None, help='Club in play, e.g. "7-iron" (fallback for --club-from)'
    )
    radar.add_argument(
        "--no-camera-confirm",
        action="store_true",
        help="Post a radar ball even when the swing's clip shows no ball leaving",
    )
    radar.add_argument(
        "--club-from",
        default=None,
        help="Kiosk club URL read on every swing, e.g. http://127.0.0.1:8080/api/club",
    )
    radar.add_argument(
        "--rig-setup-url",
        default=None,
        help=(
            "Kiosk observation URL read on every swing, "
            "e.g. http://127.0.0.1:8080/api/rig-setup/observation"
        ),
    )
    radar.add_argument(
        "--pitch-axis",
        choices=("x", "y"),
        default=None,
        help="Read the case tilt sensor on this board axis each swing (default: don't)",
    )
    radar.add_argument(
        "--post-to",
        default=None,
        help="Kiosk measured-shot URL, e.g. http://127.0.0.1:8080/api/shots/measured",
    )
    radar.add_argument(
        "--status-to",
        default=None,
        help=(
            "Kiosk ready/measuring URL, so the screen shows the radar wait, "
            "e.g. http://127.0.0.1:8080/api/shots/measuring"
        ),
    )
    camera = parser.add_argument_group(
        "Camera launch",
        "With the radar, each swing's clip is also fitted for launch angles; they "
        "replace the radar's when the fit follows the ball. Sideways is relative to "
        "the straight line set with scripts/hardware-test/camera_aim.py.",
    )
    camera.add_argument(
        "--no-camera-launch",
        action="store_true",
        help="Send the radar's launch angles only (the camera still confirms the ball left)",
    )
    camera.add_argument(
        "--camera-height-above-ball-m",
        type=float,
        default=0.08,
        help="Lens height above the resting ball's centre (VLA errs ~0.4 deg per cm)",
    )
    camera.add_argument("--camera-focal-px", type=float, default=535.0)
    camera.add_argument(
        "--camera-roll-deg", type=float, default=0.0, help="Image rotation, + clockwise"
    )
    camera.add_argument(
        "--camera-aim",
        type=Path,
        default=Path.home() / ".config" / "openflight" / "camera-aim.json",
        help="Where the camera's straight line is kept",
    )
    ops = parser.add_argument_group(
        "OPS243 club speed",
        "Every trigger the TI radar takes also dumps the OPS243's rolling buffer (S!); "
        "its club speed goes to the kiosk when its ball speed agrees with the TI's and "
        "the smash factor is physical (openflight/ops243_club_speed.py).",
    )
    ops.add_argument(
        "--ops243-club-speed", action="store_true", help="Measure club speed with the OPS243"
    )
    ops.add_argument("--ops243-port", default="/dev/ttyAMA0", help="OPS243 serial port")
    ops.add_argument(
        "--ops243-pre-trigger-segments",
        type=int,
        default=16,
        help="S#n: 4.27 ms segments kept from before the trigger, of 32 (default 16: 68/68 ms)",
    )
    ops.add_argument(
        "--ops243-transmit-power", type=int, default=3, help="0 (max) to 7 (min); default 3"
    )
    args = parser.parse_args(argv)
    if args.camera_focal_px <= 0:
        parser.error("--camera-focal-px must be positive")
    for name in ("tee_m", "net_m", "radar_height_m"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.ball_height_m is not None and args.ball_height_m < 0:
        parser.error("--ball-height-m cannot be negative")
    if args.trigger == "sound" and not args.iwr6843:
        parser.error("--trigger sound drives the TI radar's capture; add --iwr6843")
    if args.ops243_club_speed and not args.iwr6843:
        parser.error("--ops243-club-speed checks the OPS ball against the TI's; add --iwr6843")
    if not 1 <= args.ops243_pre_trigger_segments <= 32:
        parser.error("--ops243-pre-trigger-segments must be 1-32")
    if not 0 <= args.ops243_transmit_power <= 7:
        parser.error("--ops243-transmit-power must be 0-7")
    return args


def zone_box(shape: tuple[int, ...], roi: tuple[float, float, float, float]) -> tuple[int, ...]:
    height, width = shape[:2]
    x, y, w, h = roi
    return (
        round(width * x),
        round(height * y),
        round(width * (x + w)) - 1,
        round(height * (y + h)) - 1,
    )


def save_with_zone(path: Path, image: np.ndarray, roi, label: str | None = None) -> None:
    """Grayscale JPEG with the hitting zone outlined."""
    try:
        from PIL import Image, ImageDraw  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    picture = Image.fromarray(image).convert("RGB")
    draw = ImageDraw.Draw(picture)
    draw.rectangle(zone_box(image.shape, roi), outline=(255, 200, 0), width=2)
    if label:
        draw.text((6, 4), label, fill=(255, 200, 0))
    picture.save(path, quality=88)


def motion_energy(frames: np.ndarray, roi) -> np.ndarray:
    """Mean absolute change in the zone from the previous frame, per frame."""
    left, top, right, bottom = zone_box(frames.shape[1:], roi)
    zone = frames[:, top : bottom + 1 : 2, left : right + 1 : 2].astype(np.float32)
    energy = np.zeros(len(frames), dtype=np.float32)
    energy[1:] = np.abs(np.diff(zone, axis=0)).mean(axis=(1, 2))
    return energy


def save_contact_sheet(clip_dir: Path, roi, columns: int = 4, rows: int = 3) -> int | None:
    """Tile the frames around the biggest change; return that frame's index."""
    try:
        from PIL import Image, ImageDraw  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    with np.load(clip_dir / "frames.npz") as archive:
        frames = archive["frames"]
        trigger_index = int(archive["pre_trigger_count"]) - 1
    peak = int(np.argmax(motion_energy(frames, roi)))
    count = columns * rows
    start = max(0, min(len(frames) - count, peak - count // 2))
    tile_w = 320
    tile_h = round(frames.shape[1] * tile_w / frames.shape[2])
    sheet = Image.new("RGB", (tile_w * columns, tile_h * rows), (16, 16, 16))
    for slot, index in enumerate(range(start, min(len(frames), start + count))):
        tile = Image.fromarray(frames[index]).convert("RGB").resize((tile_w, tile_h))
        draw = ImageDraw.Draw(tile)
        box = [round(v * tile_w / frames.shape[2]) for v in zone_box(frames.shape[1:], roi)]
        draw.rectangle(box, outline=(255, 200, 0), width=1)
        marker = " peak" if index == peak else ""
        draw.text((4, 3), f"{index - trigger_index:+d}{marker}", fill=(255, 200, 0))
        sheet.paste(tile, ((slot % columns) * tile_w, (slot // columns) * tile_h))
    sheet.save(clip_dir / "sheet.jpg", quality=85)
    return peak - trigger_index


class TriggerWorker(threading.Thread):
    """Feeds camera frames to the motion trigger off the camera thread."""

    def __init__(
        self,
        runtime,
        trigger: MotionTrigger,
        *,
        roi,
        outdir: Path,
        calibrate: bool,
        on_trigger: Callable[[float], object] | None = None,
        motion_triggers: bool = True,
        gate_state: Callable[[], bool | None] | None = None,
    ):
        super().__init__(name="motion-trigger", daemon=True)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self.runtime = runtime
        self.trigger = trigger
        self.roi = roi
        self.outdir = outdir
        self.calibrate = calibrate
        self.on_trigger = on_trigger
        # False with --trigger sound: frames still feed aim.jpg and the stats
        # line, but only the sound sensor starts a reading.
        self.motion_triggers = motion_triggers
        self.gate_state = gate_state
        self.dropped = 0
        self.triggers = 0
        self.stop_event = threading.Event()
        self._saved_aim = False
        self._aim_frames: list[np.ndarray] | None = []

    def listen(self, frame) -> None:
        """Camera-thread hook: hand the frame over without blocking."""
        try:
            self.frames.put_nowait(frame.image)
        except queue.Full:
            self.dropped += 1

    def run(self) -> None:
        window_start = time.monotonic()
        seen = 0
        peak = 0.0
        last = None
        while not self.stop_event.is_set():
            try:
                image = self.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._saved_aim:
                save_with_zone(self.outdir / "aim.jpg", image, self.roi, "hitting zone")
                self._saved_aim = True
            self.keep_aim_frame(image)
            sample = self.trigger.feed(image)
            seen += 1
            if sample is None:
                continue
            last = sample
            peak = max(peak, sample.fraction)
            self.handle_sample(sample)
            now = time.monotonic()
            if self.calibrate and now - window_start >= 1.0 and last is not None:
                print(self.stats_line(seen / (now - window_start), peak, last))
                window_start, seen, peak = now, 0, 0.0

    def keep_aim_frame(self, image: np.ndarray) -> None:
        """Average the first frames into aim_mean.npy: the raw scene at start,
        for setting the camera's straight line (camera_aim.py)."""
        if self._aim_frames is None:
            return
        self._aim_frames.append(np.asarray(image, dtype=np.float32))
        if len(self._aim_frames) >= QUIET_FRAMES:
            np.save(self.outdir / "aim_mean.npy", np.mean(self._aim_frames, axis=0))
            self._aim_frames = None

    def handle_sample(self, sample) -> None:
        """Act on a motion sample: with camera triggering, a trigger starts a reading."""
        if not (sample.triggered and self.motion_triggers):
            return
        # Freeze the radar first: its ring is only ~72 ms long.
        if self.on_trigger is not None:
            self.on_trigger(time.time())
        if self.runtime.notify_trigger():
            self.triggers += 1
            reason = getattr(self.trigger, "last_reason", "")
            why = f", {reason}" if reason else ""
            print(f"TRIGGER #{self.triggers}: {sample.fraction:.1%} of the zone changed{why}")
        else:
            print("trigger ignored: previous clip still saving")

    def sound_trigger(self, timestamp: float) -> None:
        """The radar took a sound-trigger edge: save the camera clip around it too."""
        self.triggers += 1
        saved = self.runtime.notify_trigger(timestamp)
        print(f"TRIGGER #{self.triggers}: sound" + ("" if saved else " (camera busy, no clip)"))

    def stats_line(self, fps: float, peak: float, last) -> str:
        """The once-a-second --calibrate line."""
        if self.motion_triggers:
            fires = f"fires at {self.trigger.settings.min_fraction:.1%}"
        else:
            fires = "sound triggers"
        line = (
            f"  {fps:5.0f} fps | peak change {peak:.2%} ({fires}) | threshold "
            f"{last.threshold:.0f} counts | noise {last.diff_noise:.1f} | "
            f"{'armed' if last.armed else 'not armed'} | dropped {self.dropped}"
        )
        status = getattr(self.trigger, "status", None)
        if callable(status):
            line += f" | {status()}"
        gate = None if self.gate_state is None else self.gate_state()
        if gate:
            line += " | sound GATE stuck high"
        return line


class NoTriggerInput:
    """Stands in for the BCM17 sound-trigger input: the camera triggers the radar."""

    when_pressed = None

    def __init__(self, *_args, **_kwargs):
        pass

    def close(self) -> None:
        """Nothing to release."""


MAX_POSTED_HORIZONTAL_DEG = 45.0  # the kiosk refuses a shot whose horizontal exceeds this
# No real start line is this far off; beyond it the reading is the radar's
# error (2026-11-19: -60 to -70 deg at high coherence on tracked balls),
# so the shot goes up without sideways rather than flying sideways.
MAX_SIDEWAYS_DEG = 15.0
# The swing's clip is measured a moment after it saves; the radar dump takes
# seconds, so the camera's verdict is normally in first. Wait this long for it.
CAMERA_CONFIRM_WAIT_S = 1.5
# The OPS243's dump (1.8 s) normally lands before the TI's (2.4 s); when it is
# still in flight, the shot waits this long for its club speed, then goes without.
OPS_RESULT_WAIT_S = 1.0
# A radar track cut short is kept, when the camera saw the ball leave, down to
# this span (the radar's own floor is 15 ms; the 20% 5-iron swings of
# 2026-11-15 gave clean 39-point tracks of 13-14 ms).
SHORT_TRACK_MIN_SPAN_S = 0.008


def credible_ball(measurement) -> bool:
    """A found, unrejected, confident ball track: the only swings worth showing."""
    track = measurement.track
    return bool(
        measurement.ball_found
        and track is not None
        and measurement.quality != "reject"
        and not track.low_confidence
    )


def ball_speed(measurement) -> float:
    """Cosine-corrected ball speed when the geometry allows it, else radial."""
    return measurement.ball_speed_corrected_mph or measurement.ball_speed_mph


def radar_line(measurement, *, launch=None, tee_known: bool, horizontal_zeroed: bool = True) -> str:
    """One line per swing: ball speed, then LCMF-v1 launch or why there is none."""
    if not measurement.ball_found or measurement.track is None:
        return "no ball in the radar data"
    track = measurement.track
    parts = [f"ball {ball_speed(measurement):.1f} mph"]
    side = ""
    if launch is not None and getattr(launch, "horizontal_deg", None) is not None:
        if not horizontal_zeroed:
            side = ", sideways read but the straight line is not set"
        elif abs(launch.horizontal_deg) > MAX_SIDEWAYS_DEG:
            side = f", sideways {launch.horizontal_deg:+.1f} deg dropped as implausible"
        else:
            side = f", {launch.horizontal_deg:+.1f} deg sideways"
    if launch is not None and launch.accepted:
        parts.append(f"launch {launch.angle_deg:.1f} deg up{side}")
    elif launch is not None:
        parts.append(f"launch withheld ({launch.status}){side}")
    elif not tee_known:
        parts.append("launch needs a ball distance")
    parts.append(f"({track.n_inliers} points, quality {measurement.quality})")
    if track.low_confidence:
        parts.append("LOW CONFIDENCE")
    return ", ".join(parts)


def shot_payload(measurement, *, launch=None, horizontal_zeroed: bool = True) -> dict | None:
    """What the kiosk gets for one swing, or None without a credible ball.

    Launch angles come only from an accepted LCMF-v1 result, with the evidence
    the kiosk derives its confidences from. The colocated horizontal method has
    no meaning until the rig's straight line is set (``horizontal_zeroed``):
    until then the angle is held back, and only the raw phase goes along so the
    kiosk can set that line from it.
    """
    if not credible_ball(measurement):
        return None
    payload = {"ball_speed_mph": round(ball_speed(measurement), 1)}
    if launch is None:
        return payload
    phase = getattr(launch, "horizontal_phase_rad", None)
    if phase is not None:
        payload["horizontal_phase_rad"] = round(phase, 5)
    if launch.accepted:
        payload["launch_angle_vertical"] = round(launch.angle_deg, 2)
        payload["vertical_single_channel"] = bool(launch.single_channel)
        if launch.component_std_deg is not None:
            payload["vertical_component_std_deg"] = round(launch.component_std_deg, 3)
    # Sideways rests on the ball track alone, so it goes up even when the
    # vertical was withheld; the kiosk then estimates the vertical.
    if (
        horizontal_zeroed
        and getattr(launch, "horizontal_deg", None) is not None
        and abs(launch.horizontal_deg) <= min(MAX_SIDEWAYS_DEG, MAX_POSTED_HORIZONTAL_DEG)
    ):
        payload["launch_angle_horizontal"] = round(launch.horizontal_deg, 2)
        if launch.horizontal_confidence is not None:
            payload["horizontal_coherence"] = round(launch.horizontal_confidence, 3)
    return payload


# The camera's own plausibility limit for a start line: a shank can start 20+
# deg off, and the fit searches +-25 deg. The radar keeps MAX_SIDEWAYS_DEG.
MAX_CAMERA_SIDEWAYS_DEG = 25.0


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def camera_diagnostics(camera: CameraLaunch, aim: AimReading, hla_target: float | None) -> dict:
    """The camera's reading of one swing, for the kiosk and the session log."""
    return {
        "accepted": camera.accepted,
        "status": camera.status[:40],
        "vla_deg": _rounded(camera.vla_deg),
        "hla_deg": _rounded(hla_target),
        "hla_axis_deg": _rounded(camera.hla_axis_deg),
        "aim_yaw_deg": _rounded(aim.target_yaw_deg),
        "bump_yaw_deg": _rounded(aim.bump_deg),
        "aim_status": aim.status,
        "hits": camera.hits,
        "frames": camera.moving_frames,
        "t0_ms": _rounded(camera.impact_ms),
        "fit_score": _rounded(camera.score, 1),
    }


def merge_camera_launch(
    payload: dict,
    *,
    launch,
    camera: CameraLaunch,
    aim: AimReading,
    radar_offset_deg: float | None,
) -> dict:
    """The kiosk payload with the camera's launch where it is trustworthy.

    Vertical: an accepted camera fit replaces the radar's (garage 2026-11-19:
    the radar's had no shot-to-shot relation to the ball's flight; the
    camera's follows the ball itself). Sideways, by the straight line's state:

    * set (or scene briefly hidden): the camera's direction relative to the
      target line; when the camera missed, the radar's, carried into the
      same frame by its learned offset from the camera; else none;
    * moved: none at all; the radar turned with the monitor too;
    * unset: as before the camera aim existed (the radar's own zero).

    The radar's replaced readings go along in ``radar_launch``.
    """
    out = dict(payload)
    radar_hla = getattr(launch, "horizontal_deg", None) if launch is not None else None
    radar: dict = {}
    hla_target = None
    if camera.accepted and camera.vla_deg is not None:
        if "launch_angle_vertical" in out:
            radar["vla_deg"] = out["launch_angle_vertical"]
            radar["single_channel"] = out.pop("vertical_single_channel", False)
            if "vertical_component_std_deg" in out:
                radar["component_std_deg"] = out.pop("vertical_component_std_deg")
        out["launch_angle_vertical"] = round(camera.vla_deg, 2)
        out["launch_angle_vertical_source"] = "camera"

    def drop_horizontal() -> None:
        if "launch_angle_horizontal" in out:
            radar["hla_deg"] = out.pop("launch_angle_horizontal")
        if "horizontal_coherence" in out:
            radar["horizontal_coherence"] = out.pop("horizontal_coherence")

    if aim.status == "moved":
        drop_horizontal()
    elif aim.usable:
        if camera.accepted and camera.hla_axis_deg is not None:
            hla_target = camera.hla_axis_deg - aim.target_yaw_deg
            drop_horizontal()
            if abs(hla_target) <= MAX_CAMERA_SIDEWAYS_DEG:
                out["launch_angle_horizontal"] = round(hla_target, 2)
                out["launch_angle_horizontal_source"] = "camera"
        elif radar_hla is not None and radar_offset_deg is not None:
            pointed = radar_hla - radar_offset_deg - aim.target_yaw_deg
            coherence = out.get("horizontal_coherence")
            drop_horizontal()
            if abs(pointed) <= MAX_SIDEWAYS_DEG:
                out["launch_angle_horizontal"] = round(pointed, 2)
                out["launch_angle_horizontal_source"] = "radar"
                if coherence is not None:
                    out["horizontal_coherence"] = coherence
        else:
            # No camera direction and no way to carry the radar's into the
            # target frame yet: a radar reading on its own zero would disagree
            # with the camera's line, so none goes.
            drop_horizontal()
    if (
        radar_hla is not None
        and "hla_deg" not in radar
        and out.get("launch_angle_horizontal_source") == "camera"
    ):
        radar["hla_deg"] = round(radar_hla, 2)
    if radar:
        out["radar_launch"] = radar
    out["camera_launch"] = camera_diagnostics(camera, aim, hla_target)
    return out


class CameraLaunchReader:
    """Fits each swing's clip and points it at the target line (radar worker side)."""

    def __init__(
        self, args: argparse.Namespace, ball_sizes: "BallSizes", *, aim: CameraAim | None = None
    ):
        self.args = args
        self.ball_sizes = ball_sizes
        self.aim = (
            aim
            if aim is not None
            else CameraAim.load(args.camera_aim, focal_px=args.camera_focal_px)
        )

    def geometry(self, rig: dict | None) -> CameraGeometry:
        """This swing's camera geometry; the ball distance follows the kiosk's setup."""
        distance = (rig or {}).get("tee_range_m") or self.args.tee_m or 1.549
        return CameraGeometry(
            focal_px=self.args.camera_focal_px,
            height_above_ball_m=self.args.camera_height_above_ball_m,
            ball_distance_m=float(distance),
            roll_deg=self.args.camera_roll_deg,
        )

    def apply(
        self, capture, measurement, launch, payload: dict, rig: dict | None, record: dict
    ) -> dict:
        """The payload with the camera's launch merged in; unchanged on any failure,
        so a camera problem never costs a shot."""
        clip = self.ball_sizes.clip_near(capture.trigger_timestamp)
        if clip is None or clip.clip_path is None:
            record["camera_launch"] = "no clip"
            return payload
        try:
            frames, stamps, trigger_index = load_clip(clip.clip_path)
            quiet_mean = frames[:QUIET_FRAMES].astype(np.float32).mean(axis=0)
            aim = self.aim.read(quiet_mean)
            camera = measure_launch(
                frames,
                stamps,
                trigger_index,
                ball_speed(measurement) * 0.44704,
                self.geometry(rig),
                ball_hint=clip.ball_xy,
            )
            zero = None if rig is None else rig.get("horizontal_phase_reference_rad")
            radar_hla = getattr(launch, "horizontal_deg", None) if launch is not None else None
            if camera.accepted and radar_hla is not None and camera.hla_axis_deg is not None:
                self.aim.learn_radar(radar_hla, camera.hla_axis_deg, zero)
                self.aim.save()
            merged = merge_camera_launch(
                payload,
                launch=launch,
                camera=camera,
                aim=aim,
                radar_offset_deg=self.aim.radar_offset_deg(zero),
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(f"CAMERA #{capture.sequence}: launch fit failed: {error}")
            record["camera_launch"] = f"failed: {error}"
            return payload
        record["camera_launch"] = merged["camera_launch"]
        print(f"CAMERA #{capture.sequence}: {camera_line(merged)}")
        return merged


def camera_line(payload: dict) -> str:
    """One line per swing on what the camera added."""
    camera = payload["camera_launch"]
    if not camera["accepted"]:
        return f"launch not read ({camera['status']}), radar angles kept"
    parts = [f"launch {camera['vla_deg']:.1f} deg up"]
    if payload.get("launch_angle_horizontal_source") == "camera":
        parts.append(f"{payload['launch_angle_horizontal']:+.1f} deg from the target line")
    elif camera["aim_status"] == "unset":
        parts.append("sideways needs the straight line set (camera_aim.py)")
    elif camera["aim_status"] == "moved":
        parts.append("monitor moved: set the straight line again")
    parts.append(f"{camera['hits']} frames, aim {camera['aim_status']}")
    return ", ".join(parts)


class ClipBall(NamedTuple):
    """What the camera loop learned from one clip, for the radar worker.

    (A NamedTuple, not a dataclass: the tests load this script without
    registering it as a module, which dataclasses need.)"""

    trigger_timestamp: float
    diameter_px: float | None
    clip_path: Path | None = None
    ball_xy: tuple[float, float] | None = None


class BallSizes:
    """Per-clip ball results by trigger time, from the camera loop to the radar worker."""

    def __init__(self, keep: int = 16):
        self._items: deque[ClipBall] = deque(maxlen=keep)
        self._lock = threading.Lock()

    def add(
        self,
        trigger_timestamp: float,
        diameter_px: float | None,
        clip_path: Path | str | None = None,
        ball_xy: tuple[float, float] | None = None,
    ) -> None:
        """Remember one clip's result, including that it found no ball."""
        item = ClipBall(
            trigger_timestamp,
            diameter_px,
            None if clip_path is None else Path(clip_path),
            ball_xy,
        )
        with self._lock:
            self._items.append(item)

    def clip_near(self, timestamp: float, tolerance_s: float = 1.0) -> ClipBall | None:
        """The clip triggered closest to ``timestamp``, within the tolerance."""
        with self._lock:
            items = [
                item
                for item in self._items
                if abs(item.trigger_timestamp - timestamp) <= tolerance_s
            ]
        if not items:
            return None
        return min(items, key=lambda item: abs(item.trigger_timestamp - timestamp))

    def near(self, timestamp: float, tolerance_s: float = 1.0) -> float | None:
        """The width from the clip triggered closest to ``timestamp``, if any."""
        clip = self.clip_near(timestamp, tolerance_s)
        return None if clip is None else clip.diameter_px

    def left_near(self, timestamp: float, tolerance_s: float = 1.0) -> bool | None:
        """Whether the clip triggered with this swing saw a ball leave; None without a clip."""
        clip = self.clip_near(timestamp, tolerance_s)
        return None if clip is None else clip.diameter_px is not None


class CasePitch:
    """The case tilt sensor. A missing or failing sensor just means no reading."""

    def __init__(self, axis: str, *, sensor=None, samples: int = 15):
        self.axis = axis
        self.samples = samples
        self.sensor = sensor
        if self.sensor is not None:
            return
        try:
            from openflight.inclinometer import (  # pylint: disable=import-outside-toplevel
                detect_accelerometer,
            )

            self.sensor = detect_accelerometer(bus_number=1, address=None)
            self.sensor.initialize()
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(f"Case tilt sensor unavailable: {error}")
            self.sensor = None

    def read(self) -> float | None:
        """Median pitch over a burst of samples, or None."""
        if self.sensor is None:
            return None
        from openflight.inclinometer.service import (  # pylint: disable=import-outside-toplevel
            pitch_degrees,
        )

        try:
            return statistics.median(
                pitch_degrees(self.sensor.read(), self.axis) for _ in range(self.samples)
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(f"Case tilt read failed: {error}")
            return None


def rig_geometry(
    url: str,
    *,
    diameter_px: float | None,
    pitch_deg: float | None,
    opener=urllib.request.urlopen,
) -> dict | None:
    """Give the kiosk this swing's readings; return its geometry, or None."""
    request = urllib.request.Request(
        url,
        data=json.dumps({"diameter_px": diameter_px, "pitch_deg": pitch_deg}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=3.0) as response:
            geometry = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return geometry if isinstance(geometry, dict) and "tee_range_m" in geometry else None


def kiosk_club(url: str, *, opener=urllib.request.urlopen) -> str | None:
    """The club selected on the kiosk, or None when it can't be read."""
    try:
        with opener(url, timeout=2.0) as response:
            club = json.loads(response.read() or b"{}").get("club")
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None
    return club if isinstance(club, str) and club.strip() else None


def post_shot(url: str, payload: dict, *, opener=urllib.request.urlopen) -> str:
    """Send one swing to the kiosk; return a short status for the log."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=3.0) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return f"kiosk refused it ({error.code}: {error.read()[:160].decode(errors='replace')})"
    except (urllib.error.URLError, OSError) as error:
        return f"kiosk unreachable ({error})"
    except ValueError as error:
        return f"kiosk reply unreadable ({error})"
    return f"on screen as shot {body.get('shot_number')}"


UART_BAUD = 1_041_667
UART_BITS_PER_BYTE = 10  # start + 8 data + stop
RADAR_RX_CHANNELS = 4
ANALYSIS_S = 0.5  # ball speed plus launch angles on the Pi 5


def dump_seconds_from_config(path: Path) -> float | None:
    """How long one capture takes to arrive with this profile, or None if unclear.

    The transfer is the whole wait, so the kiosk's countdown starts from the
    profile instead of from a guess: chirps x RX x saved bins x bytes per sample,
    at the firmware's fixed UART rate.
    """
    chirps = None
    bytes_per_sample = 4
    saved_bins = None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if not parts or parts[0].startswith("%"):
            continue
        try:
            if parts[0] == "frameCfg":
                chirps = (int(parts[2]) - int(parts[1]) + 1) * int(parts[3])
            elif parts[0] == "captureFormat":
                bytes_per_sample = 2 if parts[1] == "iq8" else 4
            elif parts[0] == "phaseCaptureCfg":
                values = [int(value) for value in parts[1:12]]
                saved_bins = values[1] * values[2] + values[4] * values[5] + values[7] * values[9]
        except (IndexError, ValueError):
            return None
    if not chirps or not saved_bins:
        return None
    nbytes = chirps * RADAR_RX_CHANNELS * saved_bins * bytes_per_sample
    return nbytes * UART_BITS_PER_BYTE / UART_BAUD


class KioskStatus(threading.Thread):
    """Tells the kiosk when the radar is ready and when it is busy with a dump.

    Posts happen on this thread so the trigger path never waits on HTTP, and the
    current state is repeated as a heartbeat: the kiosk drops back to "offline"
    when the heartbeats stop, so it never shows Ready for a dead recorder.
    """

    # The default profile's 7.0 s dump plus analysis, for when the profile's own
    # size cannot be read; the first real dump corrects it either way.
    DEFAULT_EXPECTED_S = 7.5

    def __init__(
        self,
        url: str,
        *,
        dump_s: float | None = None,
        heartbeat_s: float = 10.0,
        opener=urllib.request.urlopen,
    ):
        super().__init__(name="kiosk-status", daemon=True)
        self.url = url
        self.heartbeat_s = heartbeat_s
        self.opener = opener
        self.expected_s = self.DEFAULT_EXPECTED_S if dump_s is None else dump_s + ANALYSIS_S
        self.stop_event = threading.Event()
        self._reports: queue.Queue[dict] = queue.Queue()
        self._state: str | None = None

    def ready(self, result: str | None = None) -> None:
        """The radar can take a swing; ``result`` says how the last wait ended."""
        self._reports.put({"state": "ready", "result": result})

    def measuring(self) -> None:
        """A swing froze the radar; the dump is on its way."""
        self._reports.put({"state": "measuring", "expected_s": round(self.expected_s, 1)})

    def offline(self) -> None:
        """The recorder is stopping."""
        self._reports.put({"state": "offline"})

    def learn_wait(self, dump_s: float) -> None:
        """Use the last dump's real duration for the next countdown."""
        if 0.5 <= dump_s <= 30.0:
            self.expected_s = dump_s + ANALYSIS_S

    def post(self, report: dict) -> bool:
        """Send one report; False when the kiosk did not take it."""
        request = urllib.request.Request(
            self.url,
            data=json.dumps({k: v for k, v in report.items() if v is not None}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=2.0) as response:
                response.read()
        except (urllib.error.URLError, OSError):
            return False
        return True

    def run(self) -> None:
        last_post = time.monotonic()
        while not self.stop_event.is_set():
            try:
                # A short wait, so close() is noticed without waiting out a heartbeat.
                report = self._reports.get(timeout=min(self.heartbeat_s, 0.25))
            except queue.Empty:
                if self._state is None or time.monotonic() - last_post < self.heartbeat_s:
                    continue
                report = {"state": self._state}
            self._state = report["state"]
            self.post(report)
            last_post = time.monotonic()

    def close(self) -> None:
        """Say goodbye and stop; anything still queued is sent first."""
        self.offline()
        deadline = time.monotonic() + 3.0
        while not self._reports.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=3.0)


class RadarWorker(threading.Thread):
    """Analyze each TI dump with the radar-only pipeline as it arrives."""

    def __init__(
        self,
        monitor,
        calibration,
        *,
        args: argparse.Namespace,
        outdir: Path,
        ball_sizes: BallSizes | None = None,
        case_pitch: CasePitch | None = None,
        kiosk_status: KioskStatus | None = None,
        camera_launch: CameraLaunchReader | None = None,
        ops_club_speed=None,
    ):
        super().__init__(name="radar-results", daemon=True)
        from openflight.iwr6843.monitor import (  # pylint: disable=import-outside-toplevel
            tx_order_from_config,
        )

        self.monitor = monitor
        self.calibration = calibration
        self.args = args
        self.ball_sizes = ball_sizes
        self.case_pitch = case_pitch
        self.kiosk_status = kiosk_status
        self.camera_launch = camera_launch
        self.ops_club_speed = ops_club_speed
        self.tx_order = tx_order_from_config(args.iwr6843_config)
        self.results_path = outdir / "radar.jsonl"
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            capture = self.monitor.capture_for_shot(None, timeout_s=0.5)
            if capture is not None:
                self.handle(capture)

    def camera_saw_no_ball(self, capture, *, sleep=time.sleep, clock=time.monotonic) -> bool:
        """True when this swing's clip shows the resting ball never left.

        The radar finds ball-like tracks in captures with no strike: on
        2026-11-13 it read 90 mph while a new ball was being raked into place,
        and 104 mph while the camera was being handled. The clip settles it.
        With no clip (camera off, or later than the wait), nothing is held.
        """
        return self.camera_verdict(capture, sleep=sleep, clock=clock) is False

    def camera_verdict(self, capture, *, sleep=time.sleep, clock=time.monotonic) -> bool | None:
        """Whether this swing's clip saw the ball leave; None with no clip or no check."""
        if self.ball_sizes is None or getattr(self.args, "no_camera_confirm", False):
            return None
        deadline = clock() + CAMERA_CONFIRM_WAIT_S
        while True:
            left = self.ball_sizes.left_near(capture.trigger_timestamp)
            if left is not None or clock() >= deadline:
                return left
            sleep(0.05)

    def keep_short_track(self, capture, measurement) -> bool:
        """Keep a clean radar track that was cut short, when the camera saw the ball leave.

        A soft swing (a punch, or the 20% 5-iron swings of 2026-11-15 at 61 and
        77 mph) reaches the radar's ball gates later than a full one, so the
        dump holds only 13-14 ms of its flight and the radar's 15 ms floor
        rejects it, though its 39 points sat on a clean line. Such shots are
        real and must reach the screen. With the camera's word that the ball
        left, a short track is kept as low quality; a ragged or thin one is
        still refused, and nothing changes without a clip that saw the ball go.
        """
        from openflight.iwr6843.shot import (  # pylint: disable=import-outside-toplevel
            REJECT_MIN_INLIERS,
            REJECT_RMS_BINS,
        )

        track = measurement.track
        if measurement.quality != "reject" or not measurement.ball_found or track is None:
            return False
        if track.low_confidence or track.rms_bins >= REJECT_RMS_BINS:
            return False
        if track.n_inliers < REJECT_MIN_INLIERS:
            return False
        if track.t_last - track.t_first < SHORT_TRACK_MIN_SPAN_S:
            return False
        if self.camera_verdict(capture) is not True:
            return False
        measurement.quality = "low"
        return True

    def current_club(self) -> str | None:
        """The kiosk's selected club when it can be read, else --club."""
        if self.args.club_from:
            club = kiosk_club(self.args.club_from)
            if club is not None:
                return club
        return self.args.club

    def swing_geometry(self, capture) -> dict | None:
        """Tell the kiosk what the camera and tilt sensor saw; get this swing's setup."""
        if not self.args.rig_setup_url:
            return None
        diameter_px = (
            None if self.ball_sizes is None else self.ball_sizes.near(capture.trigger_timestamp)
        )
        pitch_deg = None if self.case_pitch is None else self.case_pitch.read()
        return rig_geometry(self.args.rig_setup_url, diameter_px=diameter_px, pitch_deg=pitch_deg)

    def calibration_for(self, geometry: dict | None):
        """The calibration for one swing; flags apply when the kiosk has no answer."""
        if geometry is None:
            return self.calibration
        from openflight.iwr6843.calibration_session import (  # pylint: disable=import-outside-toplevel
            clone_calibration,
        )

        return clone_calibration(
            self.calibration,
            tee_range_m=geometry.get("tee_range_m"),
            tilt_deg=math.degrees(self.calibration.tilt_rad) + geometry.get("tilt_offset_deg", 0.0),
            radar_height_m=geometry.get("radar_height_m"),
            ball_height_m=geometry.get("ball_height_m"),
        )

    def measure_launch(
        self, capture, measurement, *, club, calibration, net_m, horizontal_zero_rad=None
    ):
        """LCMF-v1, the live kiosk's launch method, fed the radar's own ball speed.

        The live path feeds it the OPS243's radial speed; the range walk's
        radial speed stands in. None without a tee range or a credible ball.
        ``horizontal_zero_rad`` is the rig's straight line for the configured
        horizontal method.
        """
        if calibration.tee_range_m is None or not credible_ball(measurement):
            return None
        from openflight.iwr6843.lcmf import (  # pylint: disable=import-outside-toplevel
            estimate_lcmf_v1,
        )

        try:
            return estimate_lcmf_v1(
                capture.raw,
                calibration,
                ball_speed_mph=measurement.ball_speed_mph,
                club=club,
                net_range_m=net_m,
                tx_order=self.tx_order,
                horizontal_method=self.args.horizontal_method,
                horizontal_phase_reference_rad=horizontal_zero_rad,
                horizontal_coherence_min=getattr(self.args, "horizontal_coherence_min", 0.90),
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(f"RADAR #{capture.sequence}: launch analysis failed: {error}")
            return None

    def add_club_speed(self, capture, measurement, payload, club, record: dict):
        """The payload with the OPS243's club speed when its gates pass, else unchanged.

        The OPS dump (1.8 s) is normally in before the TI's; this waits only
        while this swing's own dump is still in flight, and not at all for a
        swing with nothing to post. Whatever happens, the shot goes on.
        """
        ops = getattr(self, "ops_club_speed", None)
        if ops is None:
            return payload
        ti_ball = ball_speed(measurement) if credible_ball(measurement) else None
        try:
            verdict = ops.verdict_for(
                capture.trigger_timestamp,
                ti_ball,
                club=club,
                wait_s=OPS_RESULT_WAIT_S if payload is not None else 0.0,
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(f"OPS #{capture.sequence}: club speed check failed: {error}")
            record["ops243"] = {"club_speed_mph": None, "reason": f"failed: {error}"}
            return payload
        record["ops243"] = verdict.to_record()
        print(f"OPS #{capture.sequence}: {verdict.line()}")
        return payload if payload is None else verdict.add_to(payload)

    def handle(self, capture) -> None:
        """Measure one capture, then tell the kiosk the radar is ready again."""
        result = "failed"
        try:
            result = self.measure(capture)
        finally:
            if self.kiosk_status is not None:
                if capture.valid:
                    self.kiosk_status.learn_wait(capture.dump_duration_s)
                self.kiosk_status.ready(result)

    def measure(self, capture) -> str:
        """Print and log one capture's result: "shot", "no_ball" or "failed"."""
        from openflight.iwr6843.shot import process_dump  # pylint: disable=import-outside-toplevel

        result = "failed"
        club = self.current_club()
        record = {
            "sequence": capture.sequence,
            "trigger_timestamp": capture.trigger_timestamp,
            "dump_s": round(capture.dump_duration_s, 2),
            "dump_path": str(capture.path) if capture.path else None,
            "tee_m": self.args.tee_m,
            "club": club,
        }
        if not capture.valid:
            print(f"RADAR #{capture.sequence}: capture failed: {capture.error}")
            record["error"] = capture.error
        else:
            geometry = self.swing_geometry(capture)
            calibration = self.calibration_for(geometry)
            net_m = self.args.net_m if geometry is None else geometry.get("net_range_m")
            record["rig"] = geometry
            try:
                measurement = process_dump(
                    capture.raw,
                    calibration,
                    club=club,
                    net_range_m=net_m,
                    tx_order=self.tx_order,
                )
            except Exception as error:  # pylint: disable=broad-exception-caught
                print(f"RADAR #{capture.sequence}: analysis failed: {error}")
                record["error"] = f"analysis: {error}"
            else:
                if self.keep_short_track(capture, measurement):
                    print(
                        f"RADAR #{capture.sequence}: short track kept, the camera saw the ball leave"
                    )
                    record["camera"] = "short track kept"
                horizontal_zero = (
                    None if geometry is None else geometry.get("horizontal_phase_reference_rad")
                )
                launch = self.measure_launch(
                    capture,
                    measurement,
                    club=club,
                    calibration=calibration,
                    net_m=net_m,
                    horizontal_zero_rad=horizontal_zero,
                )
                tee_known = calibration.tee_range_m is not None
                # The colocated method reads about 15 degrees off until it is zeroed.
                zeroed = self.args.horizontal_method != "colocated" or horizontal_zero is not None
                line = radar_line(
                    measurement, launch=launch, tee_known=tee_known, horizontal_zeroed=zeroed
                )
                print(f"RADAR #{capture.sequence} ({club or 'no club'}): {line}")
                record.update(measurement.to_dict())
                if launch is not None:
                    record["lcmf"] = launch.to_dict()
                payload = shot_payload(measurement, launch=launch, horizontal_zeroed=zeroed)
                if payload is not None and self.camera_saw_no_ball(capture):
                    print(f"RADAR #{capture.sequence}: held back, the camera saw no ball leave")
                    record["camera"] = "no ball left"
                    payload = None
                camera_launch = getattr(self, "camera_launch", None)
                if payload is not None and camera_launch is not None:
                    payload = camera_launch.apply(
                        capture, measurement, launch, payload, geometry, record
                    )
                payload = self.add_club_speed(capture, measurement, payload, club, record)
                result = "no_ball" if payload is None else "shot"
                if self.args.post_to and payload is not None:
                    status = post_shot(self.args.post_to, payload)
                    print(f"RADAR #{capture.sequence}: {status}")
                    record["kiosk"] = status
        with self.results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return result


def radar_trigger(
    monitor, kiosk_status: KioskStatus | None, ops_club_speed=None
) -> Callable[[float], bool]:
    """Freeze the radar on a swing and, when it took the trigger, start the wait on screen.

    A trigger that arrives while a dump is in flight is refused by the monitor
    and changes nothing: the screen is already showing the wait. A trigger the
    TI takes also goes to the OPS243 (only queued there; its thread sends S!),
    so the two radars dump on the same swings and are deaf at the same times.
    """

    def on_trigger(timestamp: float) -> bool:
        accepted = monitor.notify_trigger(timestamp)
        if accepted and ops_club_speed is not None:
            ops_club_speed.trigger(timestamp)
        if accepted and kiosk_status is not None:
            kiosk_status.measuring()
        return accepted

    return on_trigger


def sound_trigger(
    worker_holder: dict, kiosk_status: KioskStatus | None, ops_club_speed=None
) -> Callable[[float], None]:
    """Called for each sound edge the radar accepted: show the wait, save the clip.

    The radar refuses echoes within 0.1 s and anything while a dump is in
    flight (the ball hitting the net, a second strike), so this runs once per
    reading. An edge before the camera is up still gets its radar reading.
    """

    def on_edge(timestamp: float) -> None:
        if ops_club_speed is not None:
            ops_club_speed.trigger(timestamp)
        if kiosk_status is not None:
            kiosk_status.measuring()
        worker = worker_holder.get("worker")
        if worker is not None:
            worker.sound_trigger(timestamp)

    return on_edge


def start_radar(
    args: argparse.Namespace,
    session_dir: Path,
    trigger_observers: list[Callable[[float], None]] | None = None,
):
    """Configure the TI radar; return (monitor, calibration).

    With --trigger camera the radar takes software triggers from the camera;
    with --trigger sound it listens to the sound sensor's GATE on --sound-pin.
    """
    # pylint: disable=import-outside-toplevel
    from openflight.iwr6843.calibration import Calibration
    from openflight.iwr6843.calibration_session import clone_calibration
    from openflight.iwr6843.monitor import IWR6843CaptureMonitor

    calibration = clone_calibration(
        Calibration.load(str(args.iwr6843_cal)),
        tee_range_m=args.tee_m,
        tilt_deg=args.tilt_deg,
        radar_height_m=args.radar_height_m,
        ball_height_m=args.ball_height_m,
    )
    sound = args.trigger == "sound"
    monitor = IWR6843CaptureMonitor(
        config_path=args.iwr6843_config,
        output_dir=session_dir / "iwr6843",
        port=args.iwr6843_port,
        gpio_pin=args.sound_pin,
        # None: the monitor opens the real GPIO input (gpiozero on lgpio).
        button_factory=None if sound else NoTriggerInput,
        save_dumps=True,
        trigger_observers=trigger_observers if sound else None,
    )
    try:
        monitor.start(armed=True)
    except Exception as error:  # pylint: disable=broad-exception-caught
        if not sound:
            raise
        raise SystemExit(
            f"Sound trigger unavailable on GPIO {args.sound_pin}: {error}. "
            "Check gpiozero and lgpio in this Python, or use --trigger camera."
        ) from error
    return monitor, calibration


def start_ops243(args: argparse.Namespace, session_dir: Path):
    """Start the OPS243 club-speed worker. It connects in the background; a
    missing or silent radar prints one warning and the recorder runs on."""
    # pylint: disable=import-outside-toplevel
    from openflight.ops243_club_speed import OPS243ClubSpeed

    ops = OPS243ClubSpeed(
        args.ops243_port,
        pre_trigger_segments=args.ops243_pre_trigger_segments,
        transmit_power=args.ops243_transmit_power,
        save_path=session_dir / "ops243.jsonl",
    )
    ops.start()
    print(f"OPS243 club speed: connecting on {args.ops243_port}")
    return ops


def describe(capture) -> str:
    meta = capture.metadata or {}
    return (
        f"{meta.get('frame_count', '?')} frames, {meta.get('delivered_fps', 0):.0f} fps, "
        f"gaps {meta.get('gap_count', '?')}, brightness {meta.get('mean_brightness', 0):.0f}/255"
    )


def main(argv: list[str] | None = None) -> int:
    """Run until the clip count, the duration or Ctrl-C."""
    args = parse_args(argv)
    session_dir = args.outdir / time.strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=True)
    settings = CameraCaptureSettings(
        width=args.width,
        height=args.height,
        fps=args.fps,
        pre_ms=args.pre_ms,
        post_ms=args.post_ms,
        exposure_us=args.exposure_us,
        gain=args.gain,
        rotate_180=args.rotate_180,
        auto_exposure=not args.fixed_exposure,
        auto_exposure_state_path=None,
    )
    motion_settings = MotionTriggerSettings(
        roi=args.roi,
        min_fraction=args.min_fraction,
        min_delta=args.min_delta,
        cooldown_s=args.cooldown,
    )
    if args.trigger == "ball":
        trigger = BallLeavesTrigger(motion_settings)
    else:
        trigger = MotionTrigger(motion_settings)
    radar_monitor = radar_worker = ball_sizes = kiosk_status = ops_club_speed = None
    worker_holder: dict[str, TriggerWorker] = {}
    sound = args.trigger == "sound"
    if args.iwr6843:
        if args.status_to:
            kiosk_status = KioskStatus(
                args.status_to, dump_s=dump_seconds_from_config(args.iwr6843_config)
            )
        if args.ops243_club_speed:
            ops_club_speed = start_ops243(args, session_dir)
        radar_monitor, calibration = start_radar(
            args,
            session_dir,
            trigger_observers=(
                [sound_trigger(worker_holder, kiosk_status, ops_club_speed)] if sound else None
            ),
        )
        ball_sizes = BallSizes()
        camera_launch = None
        if not args.no_camera_launch:
            camera_launch = CameraLaunchReader(args, ball_sizes)
            aim = camera_launch.aim
            print(
                "Camera launch on; straight line "
                + (
                    f"set ({aim.method}, target {aim.target_yaw_deg:+.2f} deg)"
                    if aim.target_yaw_deg is not None
                    else "not set: sideways stays on the radar's zero"
                )
            )
        radar_worker = RadarWorker(
            radar_monitor,
            calibration,
            args=args,
            outdir=session_dir,
            ball_sizes=ball_sizes,
            case_pitch=CasePitch(args.pitch_axis) if args.pitch_axis else None,
            kiosk_status=kiosk_status,
            camera_launch=camera_launch,
            ops_club_speed=ops_club_speed,
        )
        geometry = "no --tee-m: ball speed only" if args.tee_m is None else f"tee {args.tee_m} m"
        print(f"TI radar armed on {radar_monitor.port} ({geometry})")
        if sound:
            print(f"Sound trigger on GPIO {args.sound_pin}: the strike starts each reading")
            if radar_monitor.trigger_input_active:
                print(
                    f"WARNING: GPIO {args.sound_pin} is already high with no swing. The sound "
                    "sensor's GATE is stuck on (R17 missing or too large at 3.3 V, or it is "
                    "too sensitive), so no strike can trigger until it drops."
                )
    runtime = CameraCaptureRuntime(
        output_dir=session_dir,
        settings=settings,
        use_gpio_trigger=False,
        frame_listener=lambda frame: worker_holder["worker"].listen(frame),
    )
    worker = TriggerWorker(
        runtime,
        trigger,
        roi=args.roi,
        outdir=session_dir,
        calibrate=args.calibrate,
        on_trigger=(
            radar_trigger(radar_monitor, kiosk_status, ops_club_speed)
            if radar_monitor and not sound
            else None
        ),
        motion_triggers=not sound,
        gate_state=(lambda: radar_monitor.trigger_input_active) if sound else None,
    )
    worker_holder["worker"] = worker

    if radar_worker is not None:
        radar_worker.start()
    runtime.start()
    status = runtime.auto_exposure_status()
    print(
        f"Camera armed at {args.width}x{args.height}, {args.fps:.0f} fps requested, "
        f"{runtime.settings.exposure_us}us x{runtime.settings.gain:g} "
        f"({status.get('status')}: {status.get('message')})"
    )
    print(f"Swing away. Clips go to {session_dir}; Ctrl-C to stop.")
    worker.start()
    if kiosk_status is not None:
        kiosk_status.start()
        kiosk_status.ready()
    started = time.monotonic()
    saved = 0
    try:
        while True:
            if args.captures and saved >= args.captures:
                break
            if args.duration and time.monotonic() - started >= args.duration:
                break
            capture = runtime.capture_for_shot(None, timeout_s=0.5)
            if capture is None:
                continue
            if not capture.valid:
                print(f"clip failed to save: {capture.error}")
                continue
            saved += 1
            if ball_sizes is not None:
                ball = measure_ball_in_clip(capture.path)
                ball_sizes.add(
                    capture.trigger_timestamp,
                    ball.diameter_px if ball else None,
                    clip_path=capture.path,
                    ball_xy=(ball.x, ball.y) if ball else None,
                )
            peak = save_contact_sheet(Path(capture.path), args.roi)
            where = "" if peak is None else f", biggest change at frame {peak:+d} from the trigger"
            print(f"clip {saved}: {describe(capture)}{where} -> {capture.path}")
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop_event.set()
        runtime.stop()
        if radar_worker is not None:
            radar_worker.stop_event.set()
            radar_worker.join(timeout=2.0)
        if radar_monitor is not None:
            radar_monitor.stop()
        if ops_club_speed is not None:
            ops_club_speed.stop()
        if kiosk_status is not None:
            kiosk_status.close()
    print(f"{saved} clip(s), {worker.triggers} trigger(s), {worker.dropped} frame(s) dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
