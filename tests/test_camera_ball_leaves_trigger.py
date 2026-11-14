"""The ball-leaves trigger fires on a struck ball, not on a waggle or a rake."""

import numpy as np

from openflight.camera.ball_leaves_trigger import BallLeavesSettings, BallLeavesTrigger
from openflight.camera.motion_trigger import MotionTriggerSettings

FPS = 230.0
MOTION = MotionTriggerSettings(
    roi=(0.2, 0.5, 0.6, 0.4), min_fraction=0.002, min_delta=30, warmup_frames=3
)
BALL = BallLeavesSettings(search_every=4, stable_looks=3, fallback_after_s=99)


def scene(ball=(232, 320), club_x=None):
    rng = np.random.default_rng(7)
    image = np.full((400, 640), 30, dtype=np.float32) + rng.normal(0, 1.0, (400, 640))
    if ball is not None:
        row, column = ball
        yy, xx = np.ogrid[:400, :640]
        image[(yy - row) ** 2 + (xx - column) ** 2 <= 25] = 200
    if club_x is not None:
        image[215:300, club_x : club_x + 6] = 180  # a shaft through the zone
    return np.clip(image, 0, 255).astype(np.uint8)


def run(frames):
    trigger = BallLeavesTrigger(MOTION, BALL)
    fired = []
    for index, image in enumerate(frames):
        sample = trigger.feed(image, timestamp_s=index / FPS)
        if sample is not None and sample.triggered:
            fired.append(index)
    return trigger, fired


def test_a_struck_ball_fires_once_the_resting_ball_is_known():
    still = [scene() for _ in range(30)]
    strike = [scene(ball=None, club_x=300), scene(ball=None, club_x=360)]
    trigger, fired = run(still + strike + [scene(ball=None)] * 5)

    assert fired == [30]
    assert trigger.last_reason == "ball left"


def test_a_waggle_next_to_the_ball_does_not_fire():
    still = [scene(club_x=330) for _ in range(30)]  # club at address
    waggle = [scene(club_x=330 + abs((i + 10) % 20 - 10)) for i in range(60)]  # a pixel a frame

    _trigger, fired = run(still + waggle)

    assert fired == []


def test_a_ball_rolled_away_slowly_does_not_fire():
    still = [scene(club_x=300) for _ in range(30)]  # club resting by the ball
    rake = [scene(ball=(232, 320 + i), club_x=300 + i) for i in range(1, 40)]

    _trigger, fired = run(still + rake)

    assert fired == []


def test_with_no_ball_in_view_it_falls_back_to_motion():
    settings = BallLeavesSettings(search_every=4, stable_looks=3, fallback_after_s=0.05)
    trigger = BallLeavesTrigger(MOTION, settings)
    fired = []
    frames = [scene(ball=None) for _ in range(30)] + [scene(ball=None, club_x=300)]
    for index, image in enumerate(frames):
        sample = trigger.feed(image, timestamp_s=index / FPS)
        if sample is not None and sample.triggered:
            fired.append(index)

    assert fired == [30]
    assert trigger.last_reason.startswith("motion")


def glint_scene(ball=(232, 320), club_x=None):
    image = scene(ball=ball, club_x=club_x).astype(np.int16)
    image[236:239, 360:372] = 255  # a fixed glint brighter than the ball, beside it
    return image.clip(0, 255).astype(np.uint8)


def test_a_brighter_fixed_glint_does_not_hide_the_ball():
    still = [glint_scene() for _ in range(30)]
    strike = [glint_scene(ball=None, club_x=300), glint_scene(ball=None, club_x=360)]
    trigger, fired = run(still + strike + [glint_scene(ball=None)] * 5)

    assert fired == [30]
    assert trigger.last_reason == "ball left"


def test_very_strong_motion_fires_even_when_no_spot_left():
    still = [scene(ball=None) for _ in range(30)]
    big = scene(ball=None)
    big[215:330, 150:500] = 200  # most of the zone changes at once
    trigger, fired = run(still + [big])

    assert fired == [30]
    assert trigger.last_reason in ("strong motion", "motion (no resting ball seen)")
