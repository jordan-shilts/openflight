"""A resting ball's apparent width, measured from the spot it leaves at impact."""

import numpy as np
import pytest

from openflight.camera.ball_size import measure_ball_before_impact, measure_ball_in_clip

HEIGHT, WIDTH = 400, 640
TRIGGER = 35
FRAMES = 60


def disc_mask(diameter, cx=416.0, cy=242.0, oversample=8):
    """Anti-aliased coverage of a disc: the fraction of each pixel it fills."""
    yy, xx = np.mgrid[0 : HEIGHT * oversample, 0 : WIDTH * oversample]
    xx = (xx + 0.5) / oversample - 0.5
    yy = (yy + 0.5) / oversample - 0.5
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= (diameter / 2.0) ** 2
    return inside.reshape(HEIGHT, oversample, WIDTH, oversample).mean(axis=(1, 3))


def clip(
    diameter=15.0,
    *,
    background=30.0,
    ball=160.0,
    noise=2.0,
    seed=1,
    leaves=True,
    edit_before=None,
    edit_all=None,
):
    """A swing clip: the ball rests until impact and is gone after it."""
    rng = np.random.default_rng(seed)
    scene = np.full((HEIGHT, WIDTH), background, dtype=np.float32)
    scene[120:160, 150:520] = 230.0  # the bright strip of light under the door
    coverage = disc_mask(diameter)
    with_ball = scene * (1.0 - coverage) + ball * coverage
    if edit_before is not None:
        with_ball = edit_before(with_ball.copy(), scene)
    frames = []
    for index in range(FRAMES):
        frame = (with_ball if index <= TRIGGER or not leaves else scene).copy()
        if edit_all is not None:
            edit_all(frame, index)
        frames.append(frame + rng.normal(0.0, noise, frame.shape))
    return np.clip(np.rint(frames), 0, 255).astype(np.uint8)


@pytest.mark.parametrize("diameter", [10.0, 15.0, 22.5])
def test_the_width_of_a_resting_ball_is_measured_to_a_fraction_of_a_pixel(diameter):
    ball = measure_ball_before_impact(clip(diameter), trigger_frame_index=TRIGGER)

    assert ball is not None
    assert ball.diameter_px == pytest.approx(diameter, abs=0.35)
    assert ball.x == pytest.approx(416.0, abs=1.0)
    assert ball.y == pytest.approx(242.0, abs=1.0)


def test_a_dim_scene_still_measures():
    frames = clip(15.0, background=18.0, ball=60.0, noise=3.0)

    ball = measure_ball_before_impact(frames, trigger_frame_index=TRIGGER)

    assert ball is not None
    assert ball.diameter_px == pytest.approx(15.0, abs=0.7)


def test_width_survives_grass_hiding_the_bottom_of_the_ball():
    def hide_bottom(frame, scene):
        frame[248:] = scene[248:]  # everything below the ball's centre line + 6 px
        return frame

    ball = measure_ball_before_impact(
        clip(15.0, edit_before=hide_bottom), trigger_frame_index=TRIGGER
    )

    assert ball is not None
    assert ball.diameter_px == pytest.approx(15.0, abs=0.7)


def test_the_club_passing_through_does_not_disturb_the_measurement():
    def club(frame, index):
        if TRIGGER - 6 <= index <= TRIGGER + 4:
            x = 300 + (index - TRIGGER) * 30
            frame[225:255, x : x + 45] = 210.0

    ball = measure_ball_before_impact(clip(15.0, edit_all=club), trigger_frame_index=TRIGGER)

    assert ball is not None
    assert ball.diameter_px == pytest.approx(15.0, abs=0.35)


def test_a_ball_that_never_left_is_not_measured():
    assert measure_ball_before_impact(clip(leaves=False), trigger_frame_index=TRIGGER) is None


def test_something_large_leaving_is_not_a_ball():
    def foot(frame, scene):
        frame[250:330, 200:380] = 150.0
        return frame

    frames = clip(leaves=False)
    moved = clip(leaves=True, ball=30.0, edit_before=foot)

    assert measure_ball_before_impact(moved, trigger_frame_index=TRIGGER) is None
    assert frames is not None


def test_a_thin_streak_leaving_is_not_a_ball():
    def shaft(frame, scene):
        frame[200:300, 300:304] = 200.0
        return frame

    assert (
        measure_ball_before_impact(clip(ball=30.0, edit_before=shaft), trigger_frame_index=TRIGGER)
        is None
    )


@pytest.mark.parametrize("trigger", [-1, FRAMES, 5])
def test_an_impossible_trigger_position_measures_nothing(trigger):
    assert measure_ball_before_impact(clip(), trigger_frame_index=trigger) is None


def test_a_clip_too_short_or_the_wrong_shape_measures_nothing():
    assert measure_ball_before_impact(clip()[:20], trigger_frame_index=10) is None
    assert measure_ball_before_impact(clip()[0], trigger_frame_index=10) is None


def test_a_saved_clip_is_measured(tmp_path):
    np.savez(tmp_path / "frames.npz", frames=clip(15.0), pre_trigger_count=TRIGGER + 1)

    ball = measure_ball_in_clip(tmp_path)

    assert ball is not None
    assert ball.diameter_px == pytest.approx(15.0, abs=0.35)


def test_a_missing_or_incomplete_clip_measures_nothing(tmp_path):
    assert measure_ball_in_clip(tmp_path / "nope") is None
    np.savez(tmp_path / "frames.npz", frames=clip(15.0))
    assert measure_ball_in_clip(tmp_path) is None
