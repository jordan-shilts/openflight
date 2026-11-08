"""Camera-only motion trigger: strikes fire it, slow motion and flicker don't."""

import numpy as np
import pytest

from openflight.camera.motion_trigger import MotionTrigger, MotionTriggerSettings

HEIGHT, WIDTH = 400, 640
FPS = 240.0


def scene(rng, *, background=60, noise_sd=2.0):
    """A still, noisy mat with a teed ball in the hitting zone."""
    frame = np.full((HEIGHT, WIDTH), float(background))
    yy, xx = np.indices(frame.shape)
    frame[(xx - 320) ** 2 + (yy - 300) ** 2 <= 10**2] = 220
    frame += rng.normal(0.0, noise_sd, frame.shape)
    return frame


def to_u8(frame):
    return np.clip(np.rint(frame), 0, 255).astype(np.uint8)


def run(trigger, frames, start_s=0.0):
    """Feed frames at FPS; return the indices that triggered."""
    fired = []
    for index, frame in enumerate(frames):
        sample = trigger.feed(to_u8(frame), timestamp_s=start_s + index / FPS)
        if sample is not None and sample.triggered:
            fired.append(index)
    return fired


def still_frames(rng, count, *, noise_sd=2.0, **kwargs):
    base = scene(rng, noise_sd=0.0, **kwargs)
    return [base + rng.normal(0.0, noise_sd, base.shape) for _ in range(count)]


def with_club(frames, positions, *, size=(24, 46), value=200):
    """Draw a clubhead-sized block at an x position per frame (None: absent)."""
    out = []
    for frame, x in zip(frames, positions):
        frame = frame.copy()
        if x is not None:
            top = 300 - size[0] // 2
            frame[top : top + size[0], max(0, x) : max(0, x + size[1])] = value
        out.append(frame)
    return out


def test_a_still_noisy_scene_never_fires():
    rng = np.random.default_rng(1)

    assert run(MotionTrigger(), still_frames(rng, 400)) == []


def test_a_fast_clubhead_fires_once():
    rng = np.random.default_rng(2)
    frames = still_frames(rng, 120)
    # Downswing through the ball: about 40 px per frame across the zone.
    positions = [None] * 60 + [100 + 40 * i for i in range(12)] + [None] * 48
    trigger = MotionTrigger()

    fired = run(trigger, with_club(frames, positions))

    assert len(fired) == 1
    assert 60 <= fired[0] <= 63


def test_a_slow_waggle_does_not_fire():
    rng = np.random.default_rng(3)
    frames = still_frames(rng, 200)
    # Club soled behind the ball at address, then waggled 1-2 px per frame.
    positions = [200 + round(30 * np.sin(i / 25)) for i in range(200)]

    assert run(MotionTrigger(), with_club(frames, positions)) == []


def test_lights_flickering_across_the_whole_frame_do_not_fire():
    rng = np.random.default_rng(4)
    frames = [
        frame + (18 if index % 2 else 0) for index, frame in enumerate(still_frames(rng, 200))
    ]

    assert run(MotionTrigger(), frames) == []


def test_motion_outside_the_hitting_zone_does_not_fire():
    rng = np.random.default_rng(5)
    frames = still_frames(rng, 120)
    out = []
    for index, frame in enumerate(frames):
        frame = frame.copy()
        if 50 <= index < 70:  # fast movement high in the frame, e.g. the golfer's arms
            x = 60 + 40 * (index - 50)
            frame[40:90, x : x + 60] = 230
        out.append(frame)

    assert run(MotionTrigger(), out) == []


def test_nothing_fires_during_warmup():
    rng = np.random.default_rng(6)
    frames = still_frames(rng, 40)
    positions = [None] * 5 + [100 + 40 * i for i in range(10)] + [None] * 25

    assert (
        run(MotionTrigger(MotionTriggerSettings(warmup_frames=30)), with_club(frames, positions))
        == []
    )


def test_cooldown_spaces_out_triggers():
    rng = np.random.default_rng(7)
    swing = [None] * 40 + [100 + 40 * i for i in range(12)] + [None] * 30
    frames = with_club(still_frames(rng, len(swing)), swing)
    trigger = MotionTrigger(MotionTriggerSettings(cooldown_s=2.0))

    first = run(trigger, frames, start_s=0.0)
    soon = run(trigger, frames, start_s=0.5)  # inside the cooldown
    later = run(trigger, frames, start_s=4.0)

    assert len(first) == 1 and soon == [] and len(later) == 1


def test_a_dark_scene_still_fires_on_a_bright_club():
    rng = np.random.default_rng(8)
    frames = still_frames(rng, 120, background=8, noise_sd=1.0)
    positions = [None] * 60 + [100 + 40 * i for i in range(12)] + [None] * 48

    assert len(run(MotionTrigger(), with_club(frames, positions, value=60))) == 1


def test_samples_report_what_the_trigger_saw():
    rng = np.random.default_rng(9)
    trigger = MotionTrigger()

    assert trigger.feed(to_u8(scene(rng)), timestamp_s=0.0) is None
    sample = trigger.feed(to_u8(scene(rng)), timestamp_s=1 / FPS)

    assert sample is not None
    assert sample.armed is False
    assert sample.fraction < 0.01
    assert sample.threshold >= 12.0


def test_a_frame_size_change_restarts_the_comparison():
    trigger = MotionTrigger()

    assert trigger.feed(np.zeros((400, 640), dtype=np.uint8), timestamp_s=0.0) is None
    assert trigger.feed(np.zeros((200, 320), dtype=np.uint8), timestamp_s=0.1) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"roi": (0.5, 0.5, 0.6, 0.2)},
        {"roi": (0.0, 0.0, 0.0, 0.5)},
        {"step": 0},
        {"min_fraction": 0.0},
        {"warmup_frames": 0},
    ],
)
def test_rejects_bad_settings(kwargs):
    with pytest.raises(ValueError):
        MotionTriggerSettings(**kwargs)
