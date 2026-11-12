#!/usr/bin/env python3
"""
Time the CPU stages a shot passes through, from the OPS243 dump to the
simulator message.

The radar dumps themselves are hardware-bound (the OPS243 buffer takes ~1.8 s
over UART at 230,400 baud, the IWR6843 dump several seconds more). This script
measures everything the Pi's CPU adds on top, so the same numbers can be taken
on a dev machine and on the Pi and compared stage for stage:

    radar      clock sync, then re-arm        (fixed waits: the sync before a shot is
                                              processed, the re-arm after it is published)
    parse      wire text of a dump            -> IQCapture
    process    IQCapture                      -> speeds, spin, impact
    capture    raw I/Q into the session log   (written before the shot is published)
    carry      ballistic flight (RK4)         -> carry and the UI trajectory
    ui         Shot                           -> the UI's JSON payload
    sim        Shot                           -> OpenConnect bytes for the simulator

It needs no hardware. Captures come from a session log when one is given,
otherwise from a synthetic ball return, so it runs the same anywhere.

Usage:
    uv run python scripts/analysis/bench_shot_pipeline.py
    uv run python scripts/analysis/bench_shot_pipeline.py session_logs/session_*.jsonl
    uv run python scripts/analysis/bench_shot_pipeline.py --repeat 50 --json bench.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# pylint: disable=wrong-import-position
from openflight.ballistics import display_trajectory, resolve_launch, simulate
from openflight.clubs import ClubType
from openflight.gspro.codec import GSProCodec
from openflight.launch_monitor import Shot
from openflight.ops243 import OPS243Radar
from openflight.rolling_buffer.monitor import get_optimal_spin_for_ball_speed
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.trigger import SoundTrigger
from openflight.session_logger import SessionLogger
from openflight.sim.resolver import resolve_shot
from openflight.sim.types import PlayerState

WAVELENGTH_M = 0.01243
MPS_TO_MPH = 2.23694
SAMPLE_RATE = 30000
N_SAMPLES = 4096

# (label, club, ball mph, launch deg, spin rpm): the flights whose carry is timed.
FLIGHTS = (
    ("driver", ClubType.DRIVER, 160.0, 11.0, 2600.0),
    ("7-iron", ClubType.IRON_7, 120.0, 17.0, 6800.0),
    ("wedge", ClubType.PW, 85.0, 27.0, 9500.0),
)


def synthetic_samples(ball_speed_mph: float = 145.0, seed: int = 1) -> tuple[list[int], list[int]]:
    """A club approaching, then a ball leaving with a seam ripple on it.

    Enough structure that every stage of the processor does its real work:
    a club tone before impact, a ball tone after it with 1x-spin amplitude
    modulation, and ADC noise, centred on 2048 like the OPS243's 12-bit
    converter.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(N_SAMPLES) / SAMPLE_RATE
    impact_s = 0.060
    to_hz = lambda mph: 2 * (mph / MPS_TO_MPH) / WAVELENGTH_M  # noqa: E731

    # The ball: a steady tone that slows a little, rippled once per turn by the seam.
    since = np.maximum(t - impact_s, 0.0)
    ball_freq = to_hz(ball_speed_mph) - to_hz(15.0) * since
    ball_phase = 2 * np.pi * np.cumsum(ball_freq) / SAMPLE_RATE
    seam_hz = 2700.0 / 60.0
    ball = 400.0 * (1.0 + 0.03 * np.sin(2 * np.pi * seam_hz * since)) * np.exp(1j * ball_phase)
    ball *= t >= impact_s

    # The club: in view for the last 12 ms before impact, accelerating into the ball.
    club_window = (t >= impact_s - 0.012) & (t < impact_s)
    club_mph = (ball_speed_mph / 1.45) * (1.0 - 4.0 * np.maximum(impact_s - t, 0.0))
    club = 600.0 * np.exp(1j * 2 * np.pi * np.cumsum(to_hz(club_mph)) / SAMPLE_RATE)
    club *= club_window

    signal = ball + club
    i_samples = 2048 + signal.real + rng.normal(0.0, 4.0, N_SAMPLES)
    q_samples = 2048 + signal.imag + rng.normal(0.0, 4.0, N_SAMPLES)
    clip = lambda a: np.clip(np.rint(a), 0, 4095).astype(int).tolist()  # noqa: E731
    return clip(i_samples), clip(q_samples)


def load_log_samples(paths: list[str]) -> list[tuple[list[int], list[int]]]:
    """Every rolling_buffer_capture's I/Q samples from the given session logs."""
    samples = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "rolling_buffer_capture":
                    continue
                i_samples, q_samples = entry.get("i_samples"), entry.get("q_samples")
                if i_samples and q_samples and len(i_samples) == len(q_samples):
                    samples.append((i_samples, q_samples))
    return samples


def wire_text(i_samples: list[int], q_samples: list[int]) -> str:
    """The dump as the OPS243 sends it: four JSON lines."""
    return (
        '{"sample_time":"964.003"}\n{"trigger_time":"964.071"}\n'
        + json.dumps({"I": i_samples}, separators=(",", ":"))
        + "\n"
        + json.dumps({"Q": q_samples}, separators=(",", ":"))
        + "\n"
    )


# The two fixed waits around the radar's serial port. The clock sync runs before a
# capture is processed, so it is part of the time to a shot's first numbers; the
# re-arm runs after the shot is published, so it only delays the next swing.
CLOCK_SYNC_STAGE = "radar: clock sync, before the shot"
REARM_STAGE = "radar: re-arm, after the shot"


class InstantRadarSerial:
    """A serial port whose radar answers at once.

    With no transfer time left, what remains of the clock sync and the re-arm
    is their own fixed waits: the time every shot spends between the last byte
    of the dump and the start of processing, on any hardware.
    """

    def __init__(self, fractional_clock: bool = True):
        self.is_open = True
        self.baudrate = 230400
        self._fractional_clock = fractional_clock
        self._started = time.time()
        self._pending = b""

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def read(self, size: int) -> bytes:
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def reset_input_buffer(self) -> None:
        self._pending = b""

    def write(self, data: bytes) -> None:
        if data == b"C?":
            clock = 100.0 + (time.time() - self._started)
            text = f"{clock:.3f}" if self._fractional_clock else f"{int(clock)}"
            self._pending = f'{{"Clock":"{text}"}}'.encode()

    def flush(self) -> None:
        pass


def time_radar_waits(repeat: int, fractional_clock: bool = True) -> dict:
    """Time the per-shot clock sync and re-arm against a radar that answers at once."""
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = InstantRadarSerial(fractional_clock)
    timings = {"sync": [], "rearm": []}
    for _ in range(repeat):
        start = time.perf_counter()
        radar.read_clock_sync(samples=SoundTrigger.CLOCK_SYNC_SAMPLES, store=False)
        timings["sync"].append((time.perf_counter() - start) * 1000.0)
        start = time.perf_counter()
        radar.rearm_rolling_buffer(16)
        timings["rearm"].append((time.perf_counter() - start) * 1000.0)

    def stage(runs: list[float]) -> dict:
        runs = sorted(runs)
        return {
            "median_ms": round(statistics.median(runs), 3),
            "p95_ms": round(runs[min(len(runs) - 1, int(len(runs) * 0.95))], 3),
            "max_ms": round(runs[-1], 3),
            "runs": len(runs),
        }

    return {
        CLOCK_SYNC_STAGE: stage(timings["sync"]),
        REARM_STAGE: stage(timings["rearm"]),
    }


def time_ms(fn: Callable[[], object], repeat: int) -> dict:
    """Median, p95 and worst wall time of ``fn`` in milliseconds, after a warm-up."""
    fn()
    runs = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - start) * 1000.0)
    runs.sort()
    return {
        "median_ms": round(statistics.median(runs), 3),
        "p95_ms": round(runs[min(len(runs) - 1, int(len(runs) * 0.95))], 3),
        "max_ms": round(runs[-1], 3),
        "runs": repeat,
    }


def _flight_shot(club: ClubType, ball_mph: float, launch_deg: float, spin_rpm: float) -> Shot:
    from datetime import datetime  # pylint: disable=import-outside-toplevel

    return Shot(
        ball_speed_mph=ball_mph,
        timestamp=datetime.now(),
        club_speed_mph=ball_mph / 1.45,
        club=club,
        launch_angle_vertical=launch_deg,
        launch_angle_horizontal=1.5,
        spin_rpm=spin_rpm,
        spin_confidence=0.9,
        mode="rolling-buffer",
    )


def run(paths: list[str], repeat: int) -> dict:
    """Time each stage and return the report."""
    captures = load_log_samples(paths)
    source = f"{len(captures)} capture(s) from session logs"
    if not captures:
        captures = [synthetic_samples(145.0, seed=1), synthetic_samples(112.0, seed=2)]
        source = "synthetic captures"

    processor = RollingBufferProcessor(sample_rate=SAMPLE_RATE)
    texts = [wire_text(i, q) for i, q in captures]
    parsed = [processor.parse_capture(text) for text in texts]
    spin_prior = lambda mph: get_optimal_spin_for_ball_speed(mph, ClubType.DRIVER)  # noqa: E731

    # Sleeps, not CPU: they come out the same every time, so once is enough.
    stages: dict[str, dict] = dict(time_radar_waits(1))
    cursor = {"n": 0}

    def each(items):
        cursor["n"] += 1
        return items[cursor["n"] % len(items)]

    stages["parse: dump text -> IQCapture"] = time_ms(
        lambda: processor.parse_capture(each(texts)), repeat
    )
    stages["process: speeds, spin, impact"] = time_ms(
        lambda: processor.process_capture(
            each(parsed), expected_spin_for_ball_speed=spin_prior, club_type=ClubType.DRIVER
        ),
        repeat,
    )

    with tempfile.TemporaryDirectory() as log_dir:
        session_logger = SessionLogger(log_dir=Path(log_dir))
        session_logger.start_session(mode="rolling-buffer")

        def log_capture():
            capture = each(parsed)
            session_logger.log_rolling_buffer_capture(
                shot_number=1,
                sample_time=capture.sample_time,
                trigger_time=capture.trigger_time,
                i_samples=capture.i_samples,
                q_samples=capture.q_samples,
                ball_speed_mph=145.0,
            )

        stages["capture: raw I/Q into the session log"] = time_ms(log_capture, repeat)
        session_logger.end_session()

    codec = GSProCodec()
    player = PlayerState()
    for label, club, ball_mph, launch_deg, spin_rpm in FLIGHTS:
        shot = _flight_shot(club, ball_mph, launch_deg, spin_rpm)
        conditions = resolve_launch(shot)

        def carry(conditions=conditions, shot=shot):
            trajectory = simulate(conditions)
            shot.carry_spin_adjusted = trajectory.carry_yards
            shot.trajectory = display_trajectory(trajectory)
            return trajectory

        stages[f"carry: {label} flight (RK4)"] = time_ms(carry, repeat)
        stages[f"carry: {label} flight (RK4)"]["carry_yards"] = round(carry().carry_yards, 1)

    shot = _flight_shot(*FLIGHTS[0][1:])
    trajectory = simulate(resolve_launch(shot))
    shot.carry_spin_adjusted = trajectory.carry_yards
    shot.trajectory = display_trajectory(trajectory)

    def ui_payload():
        data = shot.to_dict()
        data["trajectory"] = shot.trajectory
        return json.dumps({"shot": data, "stats": {}})

    stages["ui: Shot -> JSON payload"] = time_ms(ui_payload, repeat)
    stages["sim: Shot -> OpenConnect bytes"] = time_ms(
        lambda: codec.build_shot(resolve_shot(shot, player)), repeat
    )

    def total(prefix: str) -> float:
        return sum(v["median_ms"] for k, v in stages.items() if k.startswith(prefix))

    per_shot = (
        total("parse")
        + total("process")
        + total("capture")
        + stages["carry: driver flight (RK4)"]["median_ms"]
        + total("ui")
        + total("sim")
    )
    return {
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "numpy": np.__version__,
        },
        "captures": source,
        "stages": stages,
        "cpu_per_driver_shot_ms": round(per_shot, 2),
        "wait_before_shot_ms": stages[CLOCK_SYNC_STAGE]["median_ms"],
        "rearm_after_shot_ms": stages[REARM_STAGE]["median_ms"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("logs", nargs="*", help="session JSONL files to take captures from")
    parser.add_argument("--repeat", type=int, default=30, help="timed runs per stage (default 30)")
    parser.add_argument("--json", metavar="FILE", help="also write the report as JSON")
    args = parser.parse_args()

    report = run(args.logs, max(3, args.repeat))
    machine = report["machine"]
    print(
        f"{machine['platform']} ({machine['machine']}), Python {machine['python']}, "
        f"numpy {machine['numpy']}"
    )
    print(f"captures: {report['captures']}\n")
    print(f"{'stage':<42}{'median':>10}{'p95':>10}{'worst':>10}")
    for name, timing in report["stages"].items():
        print(
            f"{name:<42}{timing['median_ms']:>8.2f} ms{timing['p95_ms']:>8.2f} ms"
            f"{timing['max_ms']:>8.2f} ms"
        )
    print(f"\nFixed wait before a shot is processed:    {report['wait_before_shot_ms']:.0f} ms")
    print(f"Re-arm, after the shot is published:      {report['rearm_after_shot_ms']:.0f} ms")
    print(f"CPU added to one driver shot (medians):   {report['cpu_per_driver_shot_ms']:.1f} ms")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
