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

With --iwr6843 each trigger also captures the TI radar. --rig-setup-url follows the
kiosk's Setup tab on every swing: the ball's apparent size and the case tilt go to
the kiosk, and the geometry for that swing comes back. --status-to tells the kiosk
when the radar is ready and when it is busy with a dump, so the Live screen can
say Ready or Measuring.
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
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402

from openflight.camera.ball_size import measure_ball_in_clip  # noqa: E402
from openflight.camera.capture_runtime import (  # noqa: E402
    CameraCaptureRuntime,
    CameraCaptureSettings,
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
        default=REPO_ROOT / "config" / "iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg",
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
    args = parser.parse_args(argv)
    for name in ("tee_m", "net_m", "radar_height_m"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.ball_height_m is not None and args.ball_height_m < 0:
        parser.error("--ball-height-m cannot be negative")
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
    ):
        super().__init__(name="motion-trigger", daemon=True)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self.runtime = runtime
        self.trigger = trigger
        self.roi = roi
        self.outdir = outdir
        self.calibrate = calibrate
        self.on_trigger = on_trigger
        self.dropped = 0
        self.triggers = 0
        self.stop_event = threading.Event()
        self._saved_aim = False

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
            sample = self.trigger.feed(image)
            seen += 1
            if sample is None:
                continue
            last = sample
            peak = max(peak, sample.fraction)
            if sample.triggered:
                # Freeze the radar first: its ring is only ~72 ms long.
                if self.on_trigger is not None:
                    self.on_trigger(time.time())
                if self.runtime.notify_trigger():
                    self.triggers += 1
                    print(f"TRIGGER #{self.triggers}: {sample.fraction:.1%} of the zone changed")
                else:
                    print("trigger ignored: previous clip still saving")
            now = time.monotonic()
            if self.calibrate and now - window_start >= 1.0 and last is not None:
                print(
                    f"  {seen / (now - window_start):5.0f} fps | peak change {peak:.2%} "
                    f"(fires at {self.trigger.settings.min_fraction:.1%}) | threshold "
                    f"{last.threshold:.0f} counts | noise {last.diff_noise:.1f} | "
                    f"{'armed' if last.armed else 'not armed'} | dropped {self.dropped}"
                )
                window_start, seen, peak = now, 0, 0.0


class NoTriggerInput:
    """Stands in for the BCM17 sound-trigger input: the camera triggers the radar."""

    when_pressed = None

    def __init__(self, *_args, **_kwargs):
        pass

    def close(self) -> None:
        """Nothing to release."""


MAX_POSTED_HORIZONTAL_DEG = 45.0  # the kiosk refuses a shot whose horizontal exceeds this


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


def radar_line(measurement, *, launch=None, tee_known: bool) -> str:
    """One line per swing: ball speed, then LCMF-v1 launch or why there is none."""
    if not measurement.ball_found or measurement.track is None:
        return "no ball in the radar data"
    track = measurement.track
    parts = [f"ball {ball_speed(measurement):.1f} mph"]
    if launch is not None and launch.accepted:
        side = (
            "" if launch.horizontal_deg is None else f", {launch.horizontal_deg:+.1f} deg sideways"
        )
        parts.append(f"launch {launch.angle_deg:.1f} deg up{side}")
    elif launch is not None:
        parts.append(f"launch withheld ({launch.status})")
    elif not tee_known:
        parts.append("launch needs a ball distance")
    parts.append(f"({track.n_inliers} points, quality {measurement.quality})")
    if track.low_confidence:
        parts.append("LOW CONFIDENCE")
    return ", ".join(parts)


def shot_payload(measurement, *, launch=None) -> dict | None:
    """What the kiosk gets for one swing, or None without a credible ball.

    Launch angles come only from an accepted LCMF-v1 result, with the evidence
    the kiosk derives its confidences from.
    """
    if not credible_ball(measurement):
        return None
    payload = {"ball_speed_mph": round(ball_speed(measurement), 1)}
    if launch is None or not launch.accepted:
        return payload
    payload["launch_angle_vertical"] = round(launch.angle_deg, 2)
    payload["vertical_single_channel"] = bool(launch.single_channel)
    if launch.component_std_deg is not None:
        payload["vertical_component_std_deg"] = round(launch.component_std_deg, 3)
    if (
        launch.horizontal_deg is not None
        and abs(launch.horizontal_deg) <= MAX_POSTED_HORIZONTAL_DEG
    ):
        payload["launch_angle_horizontal"] = round(launch.horizontal_deg, 2)
        if launch.horizontal_confidence is not None:
            payload["horizontal_coherence"] = round(launch.horizontal_confidence, 3)
    return payload


class BallSizes:
    """Ball widths by trigger time, from the camera loop to the radar worker."""

    def __init__(self, keep: int = 16):
        self._items: deque[tuple[float, float | None]] = deque(maxlen=keep)
        self._lock = threading.Lock()

    def add(self, trigger_timestamp: float, diameter_px: float | None) -> None:
        """Remember one clip's result, including that it found no ball."""
        with self._lock:
            self._items.append((trigger_timestamp, diameter_px))

    def near(self, timestamp: float, tolerance_s: float = 1.0) -> float | None:
        """The width from the clip triggered closest to ``timestamp``, if any."""
        with self._lock:
            items = list(self._items)
        if not items:
            return None
        when, diameter = min(items, key=lambda item: abs(item[0] - timestamp))
        return diameter if abs(when - timestamp) <= tolerance_s else None


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


class KioskStatus(threading.Thread):
    """Tells the kiosk when the radar is ready and when it is busy with a dump.

    Posts happen on this thread so the trigger path never waits on HTTP, and the
    current state is repeated as a heartbeat: the kiosk drops back to "offline"
    when the heartbeats stop, so it never shows Ready for a dead recorder.
    """

    # A wide-config dump takes 7.1 s; the analysis and post add about 0.2 s.
    DEFAULT_EXPECTED_S = 7.3
    ANALYSIS_S = 0.2

    def __init__(self, url: str, *, heartbeat_s: float = 10.0, opener=urllib.request.urlopen):
        super().__init__(name="kiosk-status", daemon=True)
        self.url = url
        self.heartbeat_s = heartbeat_s
        self.opener = opener
        self.expected_s = self.DEFAULT_EXPECTED_S
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
            self.expected_s = dump_s + self.ANALYSIS_S

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
        self.tx_order = tx_order_from_config(args.iwr6843_config)
        self.results_path = outdir / "radar.jsonl"
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            capture = self.monitor.capture_for_shot(None, timeout_s=0.5)
            if capture is not None:
                self.handle(capture)

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

    def measure_launch(self, capture, measurement, *, club, calibration, net_m):
        """LCMF-v1, the live kiosk's launch method, fed the radar's own ball speed.

        The live path feeds it the OPS243's radial speed; the range walk's
        radial speed stands in. None without a tee range or a credible ball.
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
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(f"RADAR #{capture.sequence}: launch analysis failed: {error}")
            return None

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
                launch = self.measure_launch(
                    capture, measurement, club=club, calibration=calibration, net_m=net_m
                )
                tee_known = calibration.tee_range_m is not None
                line = radar_line(measurement, launch=launch, tee_known=tee_known)
                print(f"RADAR #{capture.sequence} ({club or 'no club'}): {line}")
                record.update(measurement.to_dict())
                if launch is not None:
                    record["lcmf"] = launch.to_dict()
                payload = shot_payload(measurement, launch=launch)
                result = "no_ball" if payload is None else "shot"
                if self.args.post_to and payload is not None:
                    status = post_shot(self.args.post_to, payload)
                    print(f"RADAR #{capture.sequence}: {status}")
                    record["kiosk"] = status
        with self.results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
        return result


def radar_trigger(monitor, kiosk_status: KioskStatus | None) -> Callable[[float], bool]:
    """Freeze the radar on a swing and, when it took the trigger, start the wait on screen.

    A trigger that arrives while a dump is in flight is refused by the monitor
    and changes nothing: the screen is already showing the wait.
    """

    def on_trigger(timestamp: float) -> bool:
        accepted = monitor.notify_trigger(timestamp)
        if accepted and kiosk_status is not None:
            kiosk_status.measuring()
        return accepted

    return on_trigger


def start_radar(args: argparse.Namespace, session_dir: Path):
    """Configure the TI radar for software triggers; return (monitor, calibration)."""
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
    monitor = IWR6843CaptureMonitor(
        config_path=args.iwr6843_config,
        output_dir=session_dir / "iwr6843",
        port=args.iwr6843_port,
        button_factory=NoTriggerInput,
        save_dumps=True,
    )
    monitor.start(armed=True)
    return monitor, calibration


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
    trigger = MotionTrigger(
        MotionTriggerSettings(
            roi=args.roi,
            min_fraction=args.min_fraction,
            min_delta=args.min_delta,
            cooldown_s=args.cooldown,
        )
    )
    radar_monitor = radar_worker = ball_sizes = kiosk_status = None
    if args.iwr6843:
        radar_monitor, calibration = start_radar(args, session_dir)
        ball_sizes = BallSizes()
        if args.status_to:
            kiosk_status = KioskStatus(args.status_to)
        radar_worker = RadarWorker(
            radar_monitor,
            calibration,
            args=args,
            outdir=session_dir,
            ball_sizes=ball_sizes,
            case_pitch=CasePitch(args.pitch_axis) if args.pitch_axis else None,
            kiosk_status=kiosk_status,
        )
        geometry = "no --tee-m: ball speed only" if args.tee_m is None else f"tee {args.tee_m} m"
        print(f"TI radar armed on {radar_monitor.port} ({geometry})")
    worker_holder: dict[str, TriggerWorker] = {}
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
        on_trigger=radar_trigger(radar_monitor, kiosk_status) if radar_monitor else None,
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
                ball_sizes.add(capture.trigger_timestamp, ball.diameter_px if ball else None)
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
        if kiosk_status is not None:
            kiosk_status.close()
    print(f"{saved} clip(s), {worker.triggers} trigger(s), {worker.dropped} frame(s) dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
