"""Camera trigger that fires when the resting ball suddenly leaves its spot.

The motion trigger fires on any burst of change in the hitting zone, so a
waggle over the ball sets it off too. Each trigger costs the TI radar a ~2.5 s
readout, and a swing a second after a waggle lands in that dead time (seen on
2026-11-13: the ball on the tee at the end of a waggle clip, gone 3 s later,
nothing measured). A struck ball is gone from its spot between two frames; a
waggle, a rake or a hand moves it, or covers it, a pixel or two per frame. So
this trigger finds the resting ball while the zone is quiet and fires only on
that sudden drop, with club motion in the zone at the same time.

With no resting ball found (too dark, ball out of the zone) it falls back to
the motion trigger, so a swing is never ignored just because the ball can't be
seen. Temporary, until the OPS243 speed radar gives a strike signal of its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from openflight.camera.motion_trigger import MotionSample, MotionTrigger, MotionTriggerSettings


@dataclass(frozen=True)
class BallLeavesSettings:
    """How the resting ball is found and what counts as it leaving."""

    # A spot counts as the ball when its 5x5 core is this much brighter than
    # the ring around it (counts).
    min_ball_contrast: float = 20.0
    # Look for the ball every n frames while the zone is quiet ...
    search_every: int = 12
    # ... and believe it once it has been in the same place this many looks.
    stable_looks: int = 3
    max_shift_px: int = 2
    # Gone: core contrast at or under this share of its resting contrast ...
    gone_share: float = 0.3
    # ... when it was at least this share within two frames: a struck ball is gone
    # in a frame or two, a raked or rolled one fades over several.
    present_share: float = 0.75
    sudden_frames: int = 2
    solid_frames: int = 3
    # Club motion needed in the zone at the same time, as a share of the motion
    # trigger's own threshold (light flicker moves no club).
    motion_share: float = 0.5
    # A ball covered slowly (a waggle) is kept this long before it is presumed
    # moved and searched for again.
    keep_covered_s: float = 1.0
    # Without a resting ball for this long, use the motion trigger instead.
    fallback_after_s: float = 3.0


def _box_mean(image: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2r+1)^2 box at every pixel, edges padded."""
    size = 2 * radius + 1
    padded = np.pad(image, radius + 1, mode="edge").astype(np.float64)
    total = padded.cumsum(0).cumsum(1)
    sums = total[size:, size:] - total[:-size, size:] - total[size:, :-size] + total[:-size, :-size]
    return sums[: image.shape[0], : image.shape[1]] / (size * size)


class BallLeavesTrigger:
    """Drop-in for MotionTrigger: same feed() and settings, different verdict."""

    def __init__(
        self,
        motion_settings: MotionTriggerSettings | None = None,
        settings: BallLeavesSettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.motion = MotionTrigger(motion_settings, clock=clock)
        self.settings = self.motion.settings  # the stats line reads min_fraction
        self.ball_settings = settings or BallLeavesSettings()
        self._clock = clock
        self.last_reason = ""
        self.reset()

    def reset(self) -> None:
        self.motion.reset()
        self._frames = 0
        self._ball: tuple[int, int] | None = None
        self._rest_contrast = 0.0
        self._candidate: tuple[int, int] | None = None
        self._candidate_looks = 0
        self._present_run = 0
        self._last_solid = -(10**9)
        self._covered_since: float | None = None
        self._vanished_at: int | None = None
        self._cooldown_until = float("-inf")
        self._last_ball_s = float("-inf")
        self._started_s: float | None = None

    @property
    def ball(self) -> tuple[int, int] | None:
        """(row, column) of the resting ball in the frame, once found."""
        return self._ball

    def _zone_box(self, shape) -> tuple[int, int, int, int]:
        height, width = shape[:2]
        x, y, w, h = self.settings.roi
        return round(height * y), round(height * (y + h)), round(width * x), round(width * (x + w))

    def _find_ball(self, image: np.ndarray) -> tuple[tuple[int, int], float] | None:
        top, bottom, left, right = self._zone_box(image.shape)
        zone = image[top:bottom, left:right].astype(np.float64)
        if zone.shape[0] < 20 or zone.shape[1] < 20:
            return None
        core = _box_mean(zone, 2)
        ring = (_box_mean(zone, 7) * 225 - _box_mean(zone, 3) * 49) / 176
        contrast = core - ring
        contrast[:7], contrast[-7:], contrast[:, :7], contrast[:, -7:] = 0, 0, 0, 0
        row, column = np.unravel_index(int(np.argmax(contrast)), contrast.shape)
        best = float(contrast[row, column])
        if best < self.ball_settings.min_ball_contrast:
            return None
        return (int(row) + top, int(column) + left), best

    @staticmethod
    def _contrast_at(image: np.ndarray, spot: tuple[int, int]) -> float:
        row, column = spot
        patch = image[max(0, row - 7) : row + 8, max(0, column - 7) : column + 8].astype(np.float64)
        core = image[max(0, row - 2) : row + 3, max(0, column - 2) : column + 3].astype(np.float64)
        inner = image[max(0, row - 3) : row + 4, max(0, column - 3) : column + 4].astype(np.float64)
        ring_count = patch.size - inner.size
        if ring_count <= 0:
            return 0.0
        ring = (patch.sum() - inner.sum()) / ring_count
        return float(core.mean() - ring)

    def _forget_ball(self) -> None:
        self._ball = None
        self._candidate = None
        self._candidate_looks = 0
        self._present_run = 0
        self._last_solid = -(10**9)
        self._covered_since = None
        self._vanished_at: int | None = None

    def feed(self, image: np.ndarray, timestamp_s: float | None = None) -> MotionSample | None:
        image = np.asarray(image)
        sample = self.motion.feed(image, timestamp_s)
        if sample is None:
            return None
        now = self._clock() if timestamp_s is None else timestamp_s
        if self._started_s is None:
            self._started_s = now
        self._frames += 1
        bs = self.ball_settings
        # Not the motion trigger's own armed flag: its cooldown after a waggle is
        # exactly the dead time this trigger exists to avoid.
        armed = self._frames > self.settings.warmup_frames and now >= self._cooldown_until
        quiet = sample.fraction < self.settings.min_fraction / 4

        fired = False
        reason = ""
        if self._ball is not None:
            contrast = self._contrast_at(image, self._ball)
            gone = contrast <= bs.gone_share * self._rest_contrast
            # "There" means seen solidly for a few frames in a row, so a club
            # shaft crossing an empty spot for a frame doesn't count as the ball.
            was_there = self._frames - self._last_solid <= bs.sudden_frames
            if contrast >= bs.present_share * self._rest_contrast:
                self._present_run += 1
                if self._present_run >= bs.solid_frames:
                    self._last_solid = self._frames
            else:
                self._present_run = 0
            moving = sample.fraction >= bs.motion_share * self.settings.min_fraction
            if gone and was_there:
                self._vanished_at = self._frames
            # The ball can vanish a frame or two before the club's motion in the
            # zone reaches the share (2026-11-13 clip 25: gone at -3, motion at -1).
            vanished = (
                self._vanished_at is not None
                and self._frames - self._vanished_at <= bs.sudden_frames
            )
            if gone and vanished and moving and armed:
                fired, reason = True, "ball left"
                self._forget_ball()
            elif gone:
                # covered or carried off slowly: keep it a moment, then look again
                if self._covered_since is None:
                    self._covered_since = now
                elif now - self._covered_since >= bs.keep_covered_s:
                    self._forget_ball()
            else:
                self._covered_since = None
                self._last_ball_s = now
        elif self._frames % bs.search_every == 0 and quiet:
            found = self._find_ball(image)
            if found is None:
                self._candidate, self._candidate_looks = None, 0
            else:
                spot, contrast = found
                if self._candidate is not None and (
                    abs(spot[0] - self._candidate[0]) <= bs.max_shift_px
                    and abs(spot[1] - self._candidate[1]) <= bs.max_shift_px
                ):
                    self._candidate_looks += 1
                else:
                    self._candidate, self._candidate_looks = spot, 1
                if self._candidate_looks >= bs.stable_looks:
                    self._ball = spot
                    self._rest_contrast = contrast
                    self._present_run = 0
                    self._last_ball_s = now

        if not fired and self._ball is None and armed:
            no_ball_for = now - max(self._last_ball_s, self._started_s)
            if no_ball_for >= bs.fallback_after_s and sample.fraction >= self.settings.min_fraction:
                fired, reason = True, "motion (no resting ball seen)"
        if fired:
            self._cooldown_until = now + self.settings.cooldown_s
            self.last_reason = reason
        return MotionSample(
            fraction=sample.fraction,
            threshold=sample.threshold,
            diff_noise=sample.diff_noise,
            armed=armed,
            triggered=fired,
        )
