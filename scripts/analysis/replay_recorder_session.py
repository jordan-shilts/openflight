#!/usr/bin/env python3
"""Replay recorded swings through the swing recorder's measuring path, offline.

Each swing the recorder saw left a radar dump (iwr6843/*.l3dump), a camera
clip (camera_*/frames.npz) and a line in radar.jsonl with the rig geometry the
kiosk gave it. This rebuilds every capture from those files and runs the
recorder's own RadarWorker.measure on it, with today's code: ball speed and
LCMF launch from the radar, the camera's launch fit and aim, the merge, the
payload. Results go to OUT/radar.jsonl; with --post-to each payload is also
posted to a kiosk, which must be a TEST instance (--mock), never the live one.

    uv run python scripts/analysis/replay_recorder_session.py SESSION... --out DIR \
        [--aim AIM.json] [--post-to http://127.0.0.1:8090/api/shots/measured]

Paths in radar.jsonl point at the Pi (/home/jordan/...); dumps are looked up
by file name in each session's iwr6843 folder instead.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from openflight.camera.ball_size import measure_ball_in_clip  # noqa: E402
from openflight.camera.flight_fit import clip_trigger_timestamp  # noqa: E402
from openflight.iwr6843 import Calibration  # noqa: E402
from openflight.iwr6843.dump import parse_dump  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "hardware-test" / "camera_motion_capture.py"
BALL_CONFIG = REPO_ROOT / "config" / "iwr6843_l3dump_ball_16f3ms_53bin_iq8.cfg"
CLUB_CONFIG = REPO_ROOT / "config" / "iwr6843_l3dump_club_14pre_16f3ms_53bin_iq8.cfg"


def load_recorder():
    spec = importlib.util.spec_from_file_location("camera_motion_capture_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture_config(raw: bytes) -> Path:
    """The capture profile a dump was taken with: the club one has more range bins."""
    meta, _ = parse_dump(raw)
    return CLUB_CONFIG if len(meta.get("range_bin_starts") or []) > 16 else BALL_CONFIG


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("session", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--aim", type=Path, default=None, help="camera aim file (default: OUT/camera-aim.json)"
    )
    parser.add_argument("--post-to", default=None, help="a TEST kiosk's /api/shots/measured")
    parser.add_argument("--since", default="00:00:00", help="first swing to replay, HH:MM:SS")
    parser.add_argument(
        "--recorder-arg", action="append", default=[], help="extra recorder flag, repeatable"
    )
    a = parser.parse_args(argv)

    recorder = load_recorder()
    a.out.mkdir(parents=True, exist_ok=True)
    aim_path = a.aim or a.out / "camera-aim.json"
    base_args = [
        "--iwr6843",
        "--horizontal-coherence-min",
        "0.8",
        "--camera-aim",
        str(aim_path),
        *a.recorder_arg,
    ]
    if a.post_to:
        base_args += ["--post-to", a.post_to]
    calibration = Calibration.load(str(REPO_ROOT / "config" / "iwr6843_calibration_reference.json"))

    from datetime import datetime  # pylint: disable=import-outside-toplevel

    ball_sizes = recorder.BallSizes(keep=4096)
    swings = []
    for session in a.session:
        for clip in sorted(session.glob("camera_*")):
            when = clip_trigger_timestamp(clip)
            if when is None or not (clip / "frames.npz").exists():
                continue
            ball = measure_ball_in_clip(clip)
            ball_sizes.add(
                when,
                ball.diameter_px if ball else None,
                clip_path=clip,
                ball_xy=(ball.x, ball.y) if ball else None,
            )
        for line in (session / "radar.jsonl").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            dump = session / "iwr6843" / Path(record.get("dump_path") or "").name
            if not record.get("dump_path") or not dump.exists():
                continue
            if datetime.fromtimestamp(record["trigger_timestamp"]).strftime("%H:%M:%S") < a.since:
                continue
            swings.append((record["trigger_timestamp"], record, dump))
    swings.sort(key=lambda item: item[0])

    workers = {}
    for when, record, dump in swings:
        raw = dump.read_bytes()
        config = capture_config(raw)
        if config not in workers:
            args = recorder.parse_args(
                base_args
                + ["--iwr6843-config", str(config), "--club", record.get("club") or "6-iron"]
            )
            reader = (
                None if args.no_camera_launch else recorder.CameraLaunchReader(args, ball_sizes)
            )
            workers[config] = recorder.RadarWorker(
                None,
                calibration,
                args=args,
                outdir=a.out,
                ball_sizes=ball_sizes,
                camera_launch=reader,
            )
            if len(workers) > 1:  # one aim shared across profiles
                workers[config].camera_launch = next(iter(workers.values())).camera_launch
        worker = workers[config]
        rig = record.get("rig")
        worker.swing_geometry = lambda _capture, rig=rig: rig
        worker.current_club = lambda record=record: record.get("club") or worker.args.club
        capture = SimpleNamespace(
            raw=raw,
            sequence=record.get("sequence"),
            trigger_timestamp=when,
            valid=True,
            dump_duration_s=record.get("dump_s") or 0.0,
            path=dump,
            error=None,
        )
        clock = datetime.fromtimestamp(when).strftime("%H:%M:%S")
        print(f"--- {clock}", flush=True)
        worker.measure(capture)
    print(f"{len(swings)} swings replayed -> {a.out / 'radar.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
