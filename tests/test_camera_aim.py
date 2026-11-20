"""Target line in the camera's view (openflight.camera.aim)."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from scipy import ndimage

from openflight.camera import aim as aim_mod
from openflight.camera.aim import CameraAim, scene_shift, scene_signature
from openflight.camera.flight_fit import CameraGeometry, CameraModel, RestingBall

WIDTH, HEIGHT = 640, 400


def garage(seed: int = 3, width: int = WIDTH + 200) -> np.ndarray:
    """A dim scene with structure, wider than the frame so it can slide."""
    rng = np.random.default_rng(seed)
    scene = ndimage.gaussian_filter(rng.normal(0, 1, (HEIGHT, width)), 3) * 60 + 22
    scene[:, ::37] += 15  # door and shelf edges
    scene[180:190, :] += 10
    return np.clip(scene, 0, 255)


def frame_from(
    scene: np.ndarray, shift_px: int, *, seed: int = 0, column_pattern: bool = True
) -> np.ndarray:
    """The camera's view of ``scene`` with the content slid right by ``shift_px``."""
    rng = np.random.default_rng(seed)
    start = 100 - shift_px
    view = scene[:, start : start + WIDTH].astype(np.float64)
    view = view + rng.normal(0, 2.0, view.shape)
    if column_pattern:
        view = view + np.random.default_rng(99).normal(0, 3.0, (1, WIDTH))  # fixed per sensor
    return view


@pytest.mark.parametrize("shift", [0, 23, -17, 41, -60])
def test_scene_shift_is_recovered(shift):
    scene = garage()
    found = scene_shift(
        scene_signature(frame_from(scene, 0)), scene_signature(frame_from(scene, shift, seed=1))
    )
    assert found.dx_px == pytest.approx(shift, abs=1)
    assert found.dy_px == 0
    assert found.match > 0.6


def test_different_scene_does_not_match():
    found = scene_shift(
        scene_signature(frame_from(garage(1), 0)), scene_signature(frame_from(garage(2), 0))
    )
    assert found.match < aim_mod.MIN_SCENE_MATCH


def test_signature_sizes_must_agree():
    with pytest.raises(ValueError):
        scene_shift(np.zeros((10, 10)), np.zeros((10, 12)))


def test_shift_to_yaw_sign_and_scale():
    yaw = aim_mod.shift_to_yaw_deg(41, 535.0, WIDTH)
    assert yaw == pytest.approx(4.39, abs=0.01)  # 2026-11-19: 41 px for the 4.3 deg turn
    assert aim_mod.shift_to_yaw_deg(-27, 535.0, WIDTH) < 0
    assert aim_mod.shift_to_yaw_deg(0, 535.0, WIDTH) == 0


def _ground_line(camera: CameraModel, x0: float, yaw_deg: float, z: float):
    yaw = math.radians(yaw_deg)
    points = []
    for s in (0.3, 1.5):
        x = x0 + s * math.sin(yaw)
        y = camera.ball_y - 0.4 + s * math.cos(yaw)
        u, v = camera.project(x, y, z)
        points.append((float(u), float(v)))
    return tuple(points)


@pytest.mark.parametrize("yaw, roll", [(0.0, 0.0), (3.0, 0.0), (-5.0, 1.5), (8.0, -2.0)])
def test_target_yaw_from_two_clubs(yaw, roll):
    geometry = CameraGeometry(height_above_ball_m=0.08, roll_deg=roll)
    camera = CameraModel(geometry, WIDTH, HEIGHT, RestingBall(380.0, 232.0, 15.0, 30.0))
    mat = camera.ball_z - 0.0213
    left = _ground_line(camera, camera.ball_x - 0.35, yaw, mat)
    right = _ground_line(camera, camera.ball_x + 0.30, yaw, mat)
    found = aim_mod.target_yaw_from_lines(
        left,
        right,
        focal_px=geometry.focal_px,
        pitch_deg=math.degrees(camera.pitch),
        width=WIDTH,
        height=HEIGHT,
        roll_deg=roll,
    )
    assert found == pytest.approx(yaw, abs=0.05)


def test_parallel_image_lines_are_refused():
    with pytest.raises(ValueError):
        aim_mod.vanishing_point(((0, 0), (10, 10)), ((5, 0), (15, 10)))


def test_target_yaw_from_shots_is_the_median_and_needs_enough():
    assert aim_mod.target_yaw_from_shots([1.0, -2.0, 0.5, 3.0, -1.0, 2.0, 0.0, 1.5, None]) == 0.75
    with pytest.raises(ValueError):
        aim_mod.target_yaw_from_shots([1.0, 2.0])


def test_unset_aim(tmp_path):
    aim = CameraAim.load(tmp_path / "aim.json")
    reading = aim.read(frame_from(garage(), 0))
    assert reading.status == "unset"
    assert not reading.usable


def test_set_follow_and_reload(tmp_path):
    scene = garage()
    aim = CameraAim.load(tmp_path / "aim.json")
    aim.set_straight(frame_from(scene, 0), 1.25, "clubs")
    bumped = aim.read(frame_from(scene, 30, seed=5))
    assert bumped.status == "set" and bumped.usable
    assert bumped.bump_deg == pytest.approx(aim_mod.shift_to_yaw_deg(30, 535.0, WIDTH), abs=0.15)
    assert bumped.target_yaw_deg == pytest.approx(1.25 + bumped.bump_deg)
    aim.save()
    again = CameraAim.load(tmp_path / "aim.json")
    assert again.target_yaw_deg == 1.25
    assert again.method == "clubs"
    assert again.read(frame_from(scene, 30, seed=6)).bump_deg == pytest.approx(
        bumped.bump_deg, abs=0.15
    )
    saved = json.loads((tmp_path / "aim.json").read_text())
    assert saved["target_yaw_deg"] == 1.25


def test_scene_lost_uses_last_bump_then_says_moved(tmp_path):
    scene = garage()
    aim = CameraAim.load(tmp_path / "aim.json")
    aim.set_straight(frame_from(scene, 0), 0.0, "clubs")
    first = aim.read(frame_from(scene, 20))
    other = frame_from(garage(7), 0)
    for _ in range(aim_mod.LOST_CLIPS_MEAN_MOVED - 1):
        lost = aim.read(other)
        assert lost.status == "scene_lost" and lost.usable
        assert lost.target_yaw_deg == pytest.approx(first.target_yaw_deg)
    assert aim.read(other).status == "moved"
    assert aim.read(frame_from(scene, 20)).status == "set"  # back in place: followed again


def test_large_turn_means_moved(tmp_path):
    scene = garage(width=WIDTH + 400)
    aim = CameraAim.load(tmp_path / "aim.json")
    aim.set_straight(frame_from(scene, 0), 0.0, "clubs")
    reading = aim.read(frame_from(scene, 68))
    assert reading.status == "moved"
    assert not reading.usable


@pytest.mark.parametrize(
    "content", ["{not json", json.dumps({"target_yaw_deg": "x"}), json.dumps({})]
)
def test_unreadable_aim_is_unset(tmp_path, content):
    (tmp_path / "aim.json").write_text(content)
    np.save(tmp_path / "aim.scene.npy", np.zeros((3, 3)))
    assert CameraAim.load(tmp_path / "aim.json").target_yaw_deg is None


def test_missing_scene_file_is_unset(tmp_path):
    (tmp_path / "aim.json").write_text(json.dumps({"target_yaw_deg": 1.0}))
    assert CameraAim.load(tmp_path / "aim.json").target_yaw_deg is None


def test_set_straight_refuses_a_bad_yaw(tmp_path):
    with pytest.raises(ValueError):
        CameraAim.load(tmp_path / "aim.json").set_straight(
            frame_from(garage(), 0), float("nan"), "clubs"
        )


def test_radar_offset_is_learned_from_pairs(tmp_path):
    aim = CameraAim.load(tmp_path / "aim.json")
    for k in range(aim_mod.RADAR_PAIRS_NEEDED - 1):
        aim.learn_radar(2.0 + 0.1 * k, 0.0)
    assert aim.radar_offset_deg() is None
    aim.learn_radar(2.3, 0.0)
    assert aim.radar_offset_deg() == pytest.approx(2.35, abs=0.06)
    aim.learn_radar(float("nan"), 0.0)
    assert len(aim.radar_pairs) == aim_mod.RADAR_PAIRS_NEEDED
    for _ in range(aim_mod.RADAR_PAIRS_KEPT + 5):
        aim.learn_radar(1.0, 0.0)
    assert len(aim.radar_pairs) == aim_mod.RADAR_PAIRS_KEPT
    assert aim.radar_offset_deg() == 1.0


def test_pending_reference_is_taken_from_the_next_clip(tmp_path):
    scene = garage()
    aim = CameraAim.load(tmp_path / "aim.json")
    aim.set_straight(None, 2.0, "clubs")
    reloaded = CameraAim.load(tmp_path / "aim.json")
    assert reloaded.reference_pending and reloaded.target_yaw_deg == 2.0
    first = reloaded.read(frame_from(scene, 0))
    assert first.status == "set" and first.bump_deg == 0.0 and first.target_yaw_deg == 2.0
    later = CameraAim.load(tmp_path / "aim.json").read(frame_from(scene, 25, seed=4))
    assert later.bump_deg == pytest.approx(aim_mod.shift_to_yaw_deg(25, 535.0, WIDTH), abs=0.15)


def test_a_new_radar_zero_starts_the_learning_over(tmp_path):
    aim = CameraAim.load(tmp_path / "aim.json")
    aim.set_straight(frame_from(garage(), 0), 0.0, "clubs")
    for _ in range(aim_mod.RADAR_PAIRS_NEEDED):
        aim.learn_radar(3.0, 1.0, radar_zero=-0.8)
    assert aim.radar_offset_deg(-0.8) == 2.0
    assert aim.radar_offset_deg(-0.5) is None
    aim.learn_radar(5.0, 1.0, radar_zero=-0.5)
    assert aim.radar_pairs == [4.0]
    aim.save()
    assert CameraAim.load(tmp_path / "aim.json").radar_zero == -0.5
