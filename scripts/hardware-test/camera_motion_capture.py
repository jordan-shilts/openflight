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
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402

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
    return parser.parse_args(argv)


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

    def __init__(self, runtime, trigger: MotionTrigger, *, roi, outdir: Path, calibrate: bool):
        super().__init__(name="motion-trigger", daemon=True)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
        self.runtime = runtime
        self.trigger = trigger
        self.roi = roi
        self.outdir = outdir
        self.calibrate = calibrate
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
    worker_holder: dict[str, TriggerWorker] = {}
    runtime = CameraCaptureRuntime(
        output_dir=session_dir,
        settings=settings,
        use_gpio_trigger=False,
        frame_listener=lambda frame: worker_holder["worker"].listen(frame),
    )
    worker = TriggerWorker(
        runtime, trigger, roi=args.roi, outdir=session_dir, calibrate=args.calibrate
    )
    worker_holder["worker"] = worker

    runtime.start()
    status = runtime.auto_exposure_status()
    print(
        f"Camera armed at {args.width}x{args.height}, {args.fps:.0f} fps requested, "
        f"{runtime.settings.exposure_us}us x{runtime.settings.gain:g} "
        f"({status.get('status')}: {status.get('message')})"
    )
    print(f"Swing away. Clips go to {session_dir}; Ctrl-C to stop.")
    worker.start()
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
            peak = save_contact_sheet(Path(capture.path), args.roi)
            where = "" if peak is None else f", biggest change at frame {peak:+d} from the trigger"
            print(f"clip {saved}: {describe(capture)}{where} -> {capture.path}")
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop_event.set()
        runtime.stop()
    print(f"{saved} clip(s), {worker.triggers} trigger(s), {worker.dropped} frame(s) dropped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
