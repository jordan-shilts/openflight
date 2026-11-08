"""Camera-only shot trigger: fire on sudden motion in the hitting zone.

Before a sound trigger or radar is wired, the camera can freeze its own ring on
the strike. At impact the clubhead moves several centimetres per frame, so a
large patch of the hitting zone changes between two consecutive frames. A
waggle, a slow hand or the golfer settling moves a few pixels per frame and
changes only thin edges. Comparing consecutive frames (not a background) makes
the trigger insensitive to slow scene changes, and removing each difference's
median ignores a whole-frame brightness step from lights or exposure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass(frozen=True)
class MotionTriggerSettings:
    """Where to look and how much sudden change counts as a strike."""

    # Hitting zone as fractions of the frame: x, y, width, height. The default is
    # the centre-lower region auto exposure also rates.
    roi: tuple[float, float, float, float] = (0.2, 0.45, 0.6, 0.45)
    # Sample every n-th pixel in both directions to keep up at high frame rates.
    step: int = 2
    # A sampled pixel counts as changed when it moves by at least this many counts
    # frame to frame, or noise_k times the difference noise, whichever is larger.
    min_delta: float = 12.0
    noise_k: float = 6.0
    # Share of the zone that must change at once.
    min_fraction: float = 0.01
    # Frames to learn the noise before the trigger arms.
    warmup_frames: int = 30
    # Quiet time after a trigger while the capture is saved.
    cooldown_s: float = 2.0

    def __post_init__(self) -> None:
        x, y, width, height = self.roi
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x and 0 < height <= 1 - y):
            raise ValueError("roi must be x, y, width, height fractions inside the frame")
        if self.step < 1 or self.warmup_frames < 1:
            raise ValueError("step and warmup_frames must be at least 1")
        if not 0 < self.min_fraction <= 1:
            raise ValueError("min_fraction must be in (0, 1]")


@dataclass(frozen=True)
class MotionSample:
    """One frame's verdict, for logging and tuning."""

    fraction: float
    threshold: float
    diff_noise: float
    armed: bool
    triggered: bool


class MotionTrigger:
    """Feed it frames in order; it says when to freeze the capture ring."""

    def __init__(
        self,
        settings: MotionTriggerSettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings or MotionTriggerSettings()
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        """Forget the scene, for example after the camera restarts."""
        self._previous: np.ndarray | None = None
        self._frames = 0
        self._diff_noise = 0.0
        self._cooldown_until = float("-inf")

    def _zone(self, image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        x, y, w, h = self.settings.roi
        step = self.settings.step
        return image[
            round(height * y) : round(height * (y + h)) : step,
            round(width * x) : round(width * (x + w)) : step,
        ].astype(np.float32)

    def feed(self, image: np.ndarray, timestamp_s: float | None = None) -> MotionSample | None:
        """Process the next frame; None for the first frame or a size change."""
        zone = self._zone(np.asarray(image))
        previous, self._previous = self._previous, zone
        if previous is None or previous.shape != zone.shape or not zone.size:
            return None
        self._frames += 1
        now = self._clock() if timestamp_s is None else timestamp_s

        difference = zone - previous
        difference -= float(np.median(difference))  # a whole-frame brightness step
        magnitude = np.abs(difference)
        frame_noise = 1.4826 * float(np.median(magnitude))
        threshold = max(self.settings.min_delta, self.settings.noise_k * self._diff_noise)
        fraction = float(np.mean(magnitude >= threshold))

        armed = self._frames > self.settings.warmup_frames and now >= self._cooldown_until
        triggered = armed and fraction >= self.settings.min_fraction
        if triggered:
            self._cooldown_until = now + self.settings.cooldown_s
        elif fraction < self.settings.min_fraction / 4:
            # Learn the noise only from quiet frames, so motion can't inflate it.
            weight = 1.0 / min(self._frames, 50)
            self._diff_noise += (frame_noise - self._diff_noise) * weight
        return MotionSample(
            fraction=fraction,
            threshold=threshold,
            diff_noise=self._diff_noise,
            armed=armed,
            triggered=triggered,
        )
