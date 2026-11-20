"""Camera ball-flight fit (openflight.camera.flight_fit) on synthetic clips."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from openflight.camera import flight_fit as ff
from openflight.camera.flight_fit import CameraGeometry, CameraModel, RestingBall

WIDTH, HEIGHT = 640, 400
FPS = 230.0
TRIGGER = 35  # pre_trigger_count 36
BALL_UV = (380.0, 232.0)
SPEED_MPS = 50.0


def _disc(frame: np.ndarray, u: float, v: float, radius: float, level: float) -> None:
    """Add a soft-edged bright disc."""
    r = int(math.ceil(radius + 2))
    u0, v0 = int(round(u)), int(round(v))
    ys, xs = np.mgrid[v0 - r : v0 + r + 1, u0 - r : u0 + r + 1]
    inside = (ys >= 0) & (ys < frame.shape[0]) & (xs >= 0) & (xs < frame.shape[1])
    dist = np.hypot(xs - u, ys - v)
    weight = np.clip(radius + 0.5 - dist, 0.0, 1.0)
    frame[ys[inside], xs[inside]] += level * weight[inside]


def make_clip(
    hla_deg: float = 3.0,
    vla_deg: float = 14.0,
    impact_s: float = 0.002,
    *,
    geometry: CameraGeometry | None = None,
    frames: int = 60,
    seed: int = 1,
    ball: bool = True,
    flies: bool = True,
    still_club: bool = False,
    drop_after: int | None = None,
):
    """Frames, timestamps and trigger index of a dim garage-like swing."""
    geometry = geometry or CameraGeometry()
    rng = np.random.default_rng(seed)
    step_ns = int(1e9 / FPS)
    stamps = np.arange(frames, dtype=np.int64) * step_ns
    if drop_after is not None:
        stamps[drop_after:] += 3 * step_ns
    times = (stamps - stamps[TRIGGER]) / 1e9
    camera = CameraModel(geometry, WIDTH, HEIGHT, RestingBall(*BALL_UV, 15.0, 40.0))
    u, v, travel = camera.flight(
        math.radians(hla_deg), math.radians(vla_deg), impact_s, times, SPEED_MPS
    )
    depth0 = geometry.ball_distance_m
    out = np.empty((frames, HEIGHT, WIDTH), dtype=np.float32)
    for k in range(frames):
        frame = np.full((HEIGHT, WIDTH), 20.0, dtype=np.float32)
        frame += rng.normal(0.0, 3.0, frame.shape)
        if ball:
            if flies and travel[k] > 0:
                depth = depth0 + travel[k] * math.cos(math.radians(vla_deg))
                _disc(frame, u[k], v[k], 7.5 * depth0 / depth, 35.0)
            else:
                _disc(frame, *BALL_UV, 7.5, 35.0)
        if still_club and k >= TRIGGER - 6:
            _disc(frame, BALL_UV[0] - 14, BALL_UV[1] - 2, 6.0, 60.0)
        out[k] = frame
    return np.clip(out, 0, 255).astype(np.uint8), stamps, TRIGGER


@pytest.mark.parametrize(
    "hla, vla",
    [(0.0, 14.0), (6.0, 10.0), (-8.0, 22.0), (2.0, 35.0), (-15.0, 6.0)],
)
def test_recovers_launch(hla, vla):
    frames, stamps, trig = make_clip(hla, vla)
    result = ff.measure_launch(frames, stamps, trig, SPEED_MPS, CameraGeometry())
    assert result.accepted, result.status
    assert result.hla_axis_deg == pytest.approx(hla, abs=0.3)
    assert result.vla_deg == pytest.approx(vla, abs=0.4)
    # Impact time is coarse: the first few millimetres of flight overlap the
    # tee spot, which the background removes. It only anchors the track.
    assert result.impact_ms == pytest.approx(2.0, abs=1.5)
    assert result.hits >= ff.MIN_HITS


def test_recovers_launch_with_roll_and_other_geometry():
    geometry = CameraGeometry(
        focal_px=520.0, height_above_ball_m=0.11, ball_distance_m=1.8, roll_deg=2.0
    )
    frames, stamps, trig = make_clip(4.0, 16.0, geometry=geometry)
    result = ff.measure_launch(frames, stamps, trig, SPEED_MPS, geometry)
    assert result.accepted, result.status
    assert result.hla_axis_deg == pytest.approx(4.0, abs=0.3)
    assert result.vla_deg == pytest.approx(16.0, abs=0.4)


def test_ignoring_roll_shifts_hla_by_roll_times_tan_vla():
    """Why roll is in the model: 2 deg of roll at 20 deg launch moves HLA ~0.7 deg."""
    rolled = CameraGeometry(roll_deg=2.0)
    frames, stamps, trig = make_clip(0.0, 20.0, geometry=rolled)
    level = ff.measure_launch(frames, stamps, trig, SPEED_MPS, CameraGeometry())
    assert level.accepted
    assert abs(level.hla_axis_deg) > 0.4


def test_wrong_camera_height_biases_vla_not_hla():
    frames, stamps, trig = make_clip(3.0, 14.0, geometry=CameraGeometry(height_above_ball_m=0.08))
    high = ff.measure_launch(
        frames, stamps, trig, SPEED_MPS, CameraGeometry(height_above_ball_m=0.10)
    )
    assert high.accepted
    assert high.hla_axis_deg == pytest.approx(3.0, abs=0.3)
    assert high.vla_deg < 14.0 - 0.4


def test_no_ball_in_clip():
    frames, stamps, trig = make_clip(ball=False)
    result = ff.measure_launch(frames, stamps, trig, SPEED_MPS, CameraGeometry())
    assert not result.accepted
    assert result.status in ("no_resting_ball", "ball_stayed", "ball_not_seen", "edge_of_search")


def test_ball_that_never_left_is_refused_even_with_a_bright_club_behind_it():
    frames, stamps, trig = make_clip(flies=False, still_club=True)
    result = ff.measure_launch(frames, stamps, trig, SPEED_MPS, CameraGeometry())
    assert not result.accepted
    assert result.status in ("ball_stayed", "ball_not_seen", "edge_of_search")


def test_dropped_frames_are_refused():
    frames, stamps, trig = make_clip(drop_after=TRIGGER + 3)
    result = ff.measure_launch(frames, stamps, trig, SPEED_MPS, CameraGeometry())
    assert not result.accepted
    assert result.status == "frames_dropped"


def test_short_clip_is_refused():
    frames, stamps, trig = make_clip()
    # The flight starts before the quiet frames end: nothing to subtract.
    result = ff.measure_launch(
        frames[:30], stamps[:30], ff.QUIET_FRAMES, SPEED_MPS, CameraGeometry()
    )
    assert result.status == "clip_too_short"


@pytest.mark.parametrize("speed", [0.0, -3.0, float("nan")])
def test_needs_a_ball_speed(speed):
    frames, stamps, trig = make_clip()
    assert ff.measure_launch(frames, stamps, trig, speed, CameraGeometry()).status == "no_speed"


def test_mismatched_timestamps_are_a_bad_clip():
    frames, stamps, trig = make_clip()
    assert (
        ff.measure_launch(frames, stamps[:-1], trig, SPEED_MPS, CameraGeometry()).status
        == "bad_clip"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"focal_px": 0.0},
        {"ball_distance_m": -1.0},
        {"height_above_ball_m": float("nan")},
        {"roll_deg": float("inf")},
    ],
)
def test_geometry_is_validated(kwargs):
    with pytest.raises(ValueError):
        CameraGeometry(**kwargs)


def test_resting_ball_hint_narrows_the_search():
    frames, _stamps, _trig = make_clip()
    quiet = frames[:12].astype(np.float32).mean(axis=0)
    _disc(quiet, 260.0, 228.0, 7.5, 60.0)  # a brighter decoy elsewhere in the zone
    assert ff.find_resting_ball(quiet).u == pytest.approx(260.0, abs=1.0)
    hinted = ff.find_resting_ball(quiet, hint=BALL_UV)
    assert hinted.u == pytest.approx(BALL_UV[0], abs=1.0)
    assert hinted.v == pytest.approx(BALL_UV[1], abs=1.0)


def test_measure_clip_reads_a_saved_clip(tmp_path):
    frames, stamps, trig = make_clip(5.0, 12.0)
    np.savez(
        tmp_path / "frames.npz",
        frames=frames,
        sensor_timestamp_ns=stamps,
        pre_trigger_count=trig + 1,
    )
    (tmp_path / "metadata.json").write_text(json.dumps({"trigger_timestamp": 1795207154.5}))
    result = ff.measure_clip(tmp_path, SPEED_MPS, CameraGeometry())
    assert result.accepted
    assert result.hla_axis_deg == pytest.approx(5.0, abs=0.3)
    assert ff.clip_trigger_timestamp(tmp_path) == pytest.approx(1795207154.5)
    as_dict = result.to_dict()
    assert as_dict["status"] == "ok" and isinstance(as_dict["vla_deg"], float)


def test_missing_clip_is_a_status_not_an_exception(tmp_path):
    result = ff.measure_clip(tmp_path / "nope", SPEED_MPS, CameraGeometry())
    assert not result.accepted
    assert result.status.startswith("clip_unreadable")
    assert ff.clip_trigger_timestamp(tmp_path / "nope") is None
