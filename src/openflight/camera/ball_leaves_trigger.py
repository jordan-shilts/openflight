"""Camera trigger that fires when the resting ball suddenly leaves its spot.

The motion trigger fires on any burst of change in the hitting zone, so a
waggle over the ball sets it off too. Each trigger costs the TI radar a ~2.5 s
readout, and a swing a second after a waggle lands in that dead time (seen on
2026-11-13: the ball on the tee at the end of a waggle clip, gone 3 s later,
nothing measured). A struck ball is gone from its spot between two frames; a
waggle, a rake or a hand moves it, or covers it, a pixel or two per frame. So
this trigger finds the resting ball while the zone is quiet and fires only on
that sudden drop, with club motion in the zone at the same time.

Every bright, still spot in the zone is tracked, not just the brightest: on
the first live test a fixed glint beside the ball outshone it, the trigger
watched the glint, and no swing fired. With no spot found (too dark, ball out
of the zone), or on very strong motion that no tracked spot explains, it falls
back to the motion trigger, so a swing is never ignored just because the ball
can't be seen. Temporary, until the OPS243 speed radar gives a strike signal of
its own.
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
    # Spots tracked at once (the ball plus glints, tees, a second ball).
    max_spots: int = 6
    min_spot_gap_px: int = 10
    # Motion this many times the motion trigger's threshold fires even when no
    # tracked spot vanished: a swing whose ball the tracker never picked up.
    strong_motion_factor: float = 3.0


def _box_mean(image: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2r+1)^2 box at every pixel, edges padded."""
    size = 2 * radius + 1
    padded = np.pad(image, radius + 1, mode="edge").astype(np.float64)
    total = padded.cumsum(0).cumsum(1)
    sums = total[size:, size:] - total[:-size, size:] - total[size:, :-size] + total[:-size, :-size]
    return sums[: image.shape[0], : image.shape[1]] / (size * size)


class _Spot:
    """One bright, still spot and what the trigger knows about it."""

    def __init__(self, where: tuple[int, int], contrast: float):
        self.where = where
        self.rest = contrast
        self.looks = 1
        self.present_run = 0
        self.last_solid = -(10**9)
        self.vanished_at: int | None = None
        self.covered_since: float | None = None
        self.seen_in_search = True


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
        self._spots: list[_Spot] = []
        self._cooldown_until = float("-inf")
        self._last_ball_s = float("-inf")
        self._started_s: float | None = None

    @property
    def tracked(self) -> list[tuple[int, int]]:
        """(row, column) of every established spot."""
        return [spot.where for spot in self._spots if self._is_established(spot)]

    @property
    def ball(self) -> tuple[int, int] | None:
        """The first established spot, for replays and logs."""
        tracked = self.tracked
        return tracked[0] if tracked else None

    def status(self) -> str:
        """Short note for the once-a-second stats line."""
        return f"spots {len(self.tracked)}"

    def _is_established(self, spot: _Spot) -> bool:
        return spot.looks >= self.ball_settings.stable_looks

    def _zone_box(self, shape) -> tuple[int, int, int, int]:
        height, width = shape[:2]
        x, y, w, h = self.settings.roi
        return round(height * y), round(height * (y + h)), round(width * x), round(width * (x + w))

    def _find_spots(self, image: np.ndarray) -> list[tuple[tuple[int, int], float]]:
        """The brightest compact spots in the zone, strongest first."""
        top, bottom, left, right = self._zone_box(image.shape)
        zone = image[top:bottom, left:right].astype(np.float64)
        if zone.shape[0] < 20 or zone.shape[1] < 20:
            return []
        core = _box_mean(zone, 2)
        ring = (_box_mean(zone, 7) * 225 - _box_mean(zone, 3) * 49) / 176
        contrast = core - ring
        contrast[:7], contrast[-7:], contrast[:, :7], contrast[:, -7:] = 0, 0, 0, 0
        bs = self.ball_settings
        found = []
        gap = bs.min_spot_gap_px
        for _ in range(bs.max_spots):
            row, column = np.unravel_index(int(np.argmax(contrast)), contrast.shape)
            best = float(contrast[row, column])
            if best < bs.min_ball_contrast:
                break
            found.append(((int(row) + top, int(column) + left), best))
            contrast[max(0, row - gap) : row + gap + 1, max(0, column - gap) : column + gap + 1] = 0
        return found

    def _find_ball(self, image: np.ndarray) -> tuple[tuple[int, int], float] | None:
        found = self._find_spots(image)
        return found[0] if found else None

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

    def _search(self, image: np.ndarray, now: float) -> None:
        """Match this look's spots to the tracked ones; new ones start counting."""
        bs = self.ball_settings
        for spot in self._spots:
            spot.seen_in_search = False
        for where, contrast in self._find_spots(image):
            match = next(
                (
                    spot
                    for spot in self._spots
                    if abs(spot.where[0] - where[0]) <= bs.max_shift_px
                    and abs(spot.where[1] - where[1]) <= bs.max_shift_px
                ),
                None,
            )
            if match is None:
                self._spots.append(_Spot(where, contrast))
            else:
                match.seen_in_search = True
                if not self._is_established(match):
                    match.looks += 1
                    match.rest = contrast
        # a spot not seen in a quiet look is gone (picked up, or a passing glint);
        # established ones are watched frame by frame and dropped there instead
        self._spots = [s for s in self._spots if s.seen_in_search or self._is_established(s)]
        if self.tracked:
            self._last_ball_s = now

    def _watch(self, spot: _Spot, image: np.ndarray, now: float, moving: bool, armed: bool):
        """Per-frame check of one established spot: 'fired', 'drop' or None."""
        bs = self.ball_settings
        contrast = self._contrast_at(image, spot.where)
        gone = contrast <= bs.gone_share * spot.rest
        # "There" means seen solidly a few frames in a row, so a club shaft
        # crossing an empty spot for a frame doesn't count as the ball.
        was_there = self._frames - spot.last_solid <= bs.sudden_frames
        if contrast >= bs.present_share * spot.rest:
            spot.present_run += 1
            if spot.present_run >= bs.solid_frames:
                spot.last_solid = self._frames
        else:
            spot.present_run = 0
        if gone and was_there:
            spot.vanished_at = self._frames
        # The ball can vanish a frame or two before the club's motion in the
        # zone reaches the share (2026-11-13 clip 25: gone at -3, motion at -1).
        vanished = (
            spot.vanished_at is not None and self._frames - spot.vanished_at <= bs.sudden_frames
        )
        if gone and vanished and moving and armed:
            return "fired"
        if gone:
            # covered or carried off slowly: keep it a moment, then let it go
            if spot.covered_since is None:
                spot.covered_since = now
            elif now - spot.covered_since >= bs.keep_covered_s:
                return "drop"
        else:
            spot.covered_since = None
        return None

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
        moving = sample.fraction >= bs.motion_share * self.settings.min_fraction

        fired = False
        reason = ""
        keep = []
        for spot in self._spots:
            if not self._is_established(spot):
                keep.append(spot)
                continue
            verdict = self._watch(spot, image, now, moving, armed)
            if verdict == "fired":
                fired, reason = True, "ball left"
            elif verdict != "drop":
                keep.append(spot)
        self._spots = keep
        if fired:
            # the struck ball and anything near it: start over for the next ball
            self._spots = []
        elif self._frames % bs.search_every == 0 and quiet:
            self._search(image, now)
        if self.tracked:
            self._last_ball_s = now

        if not fired and armed:
            no_ball_for = now - max(self._last_ball_s, self._started_s)
            if no_ball_for >= bs.fallback_after_s and sample.fraction >= self.settings.min_fraction:
                fired, reason = True, "motion (no resting ball seen)"
            elif sample.fraction >= bs.strong_motion_factor * self.settings.min_fraction:
                fired, reason = True, "strong motion"
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
