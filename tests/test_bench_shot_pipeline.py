"""The shot pipeline benchmark runs without hardware and times every stage."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import bench_shot_pipeline as bench  # noqa: E402

from openflight.clubs import ClubType  # noqa: E402
from openflight.rolling_buffer.processor import RollingBufferProcessor  # noqa: E402


def test_synthetic_capture_is_a_shot_the_processor_accepts():
    """The stand-in capture must exercise the real pipeline, not an early exit."""
    i_samples, q_samples = bench.synthetic_samples(145.0, seed=1)
    processor = RollingBufferProcessor()
    capture = processor.parse_capture(bench.wire_text(i_samples, q_samples))

    assert capture is not None
    assert capture.num_samples == bench.N_SAMPLES
    processed = processor.process_capture(capture, club_type=ClubType.DRIVER)
    assert processed is not None
    assert processed.ball_speed_mph == pytest.approx(145.0, abs=4.0)


def test_wire_text_is_the_four_lines_the_radar_sends():
    lines = bench.wire_text([1, 2, 3], [4, 5, 6]).strip().split("\n")

    assert [list(json.loads(line))[0] for line in lines] == [
        "sample_time",
        "trigger_time",
        "I",
        "Q",
    ]


def test_log_samples_are_read_from_a_session_log(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "rolling_buffer_capture", "i_samples": [1, 2], "q_samples": [3, 4]})
        + "\nnot json\n"
        + json.dumps({"type": "rolling_buffer_capture", "i_samples": [1], "q_samples": [3, 4]})
        + "\n"
        + json.dumps({"type": "shot_detected", "i_samples": [9], "q_samples": [9]})
        + "\n"
    )

    # a length mismatch and another entry type are both left out
    assert bench.load_log_samples([str(path)]) == [([1, 2], [3, 4])]


def test_report_times_every_stage_of_a_shot():
    report = bench.run([], repeat=3)

    stages = report["stages"]
    for prefix in (
        "radar: clock sync",
        "radar: re-arm",
        "parse",
        "process",
        "capture",
        "ui",
        "sim",
    ):
        assert any(name.startswith(prefix) for name in stages), prefix
    assert sum(1 for name in stages if name.startswith("carry")) == len(bench.FLIGHTS)
    for timing in stages.values():
        assert 0 < timing["median_ms"] <= timing["max_ms"]
    assert report["captures"] == "synthetic captures"
    assert report["cpu_per_driver_shot_ms"] > 0
    # the re-arm alone sleeps for more than half a second
    assert report["radar_waits_per_shot_ms"] > 500


def test_radar_waits_cover_both_clock_kinds():
    """An integer-only radar clock is waited on until it rolls over."""
    fractional = bench.time_radar_waits(1, fractional_clock=True)
    integer = bench.time_radar_waits(1, fractional_clock=False)

    assert set(fractional) == set(integer)
    assert fractional["radar: re-arm (fixed waits)"]["median_ms"] > 500
