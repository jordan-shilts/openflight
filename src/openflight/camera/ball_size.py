"""Measure a resting golf ball's apparent width from a saved swing clip.

The ball is found the way detect_impact_reference_ball finds it, by the spot
it leaves behind at impact, because nothing else in the frame is round, small
and gone afterwards. That detector is tuned for a ball low in a brightly lit
frame, so this one sets its threshold from the clip's own noise and works
anywhere in the frame.

Width is measured across the ball, edge to edge at half height of the
before-minus-after signal. Subtracting the after-frame removes the background,
and a horizontal width still holds when grass hides the bottom of the ball.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage

BEFORE_FRAMES = 20
AFTER_START_FRAMES = 6
AFTER_FRAMES = 14
MIN_FRAMES = BEFORE_FRAMES + AFTER_FRAMES
MIN_CONTRAST = 12.0
NOISE_K = 5.0
MIN_AREA_PX = 30
MAX_AREA_PX = 1200
MIN_SIDE_PX = 7
MAX_SIDE_PX = 42
MIN_FILL = 0.55  # a disc fills pi/4 = 0.785 of its bounding box
MAX_ASPECT = 1.7
ROW_SPAN = 3  # rows either side of the widest one


@dataclass(frozen=True)
class BallSize:
    """Where the ball sat and how wide it looked, in pixels."""

    x: float
    y: float
    diameter_px: float
    contrast: float


def _half_height_width(row: np.ndarray, center: int, half: float) -> float | None:
    """Sub-pixel distance between the two half-height crossings around ``center``."""
    if not 0 < center < len(row) - 1 or row[center] < half:
        return None
    left = center
    while left > 0 and row[left] >= half:
        left -= 1
    right = center
    while right < len(row) - 1 and row[right] >= half:
        right += 1
    if row[left] >= half or row[right] >= half:
        return None  # ran off the crop without falling below half height
    left_edge = left + (half - row[left]) / (row[left + 1] - row[left])
    right_edge = right - (half - row[right]) / (row[right - 1] - row[right])
    return float(right_edge - left_edge)


def _diameter_from_chords(chords: list[tuple[int, float]]) -> float:
    """Fit a circle to the chord widths; rows off-centre are shorter by geometry.

    A chord at row y of a circle centred on row c has w^2 = D^2 - 4(y - c)^2, so
    w^2 + 4y^2 is a straight line in y whose fit gives D directly.
    """
    widest = max(width for _row, width in chords)
    usable = [(row, width) for row, width in chords if width >= 0.8 * widest]
    if len(usable) < 3:
        return widest
    rows = np.array([row for row, _width in usable], dtype=np.float64)
    rows -= rows.mean()
    widths = np.array([width for _row, width in usable], dtype=np.float64)
    design = np.column_stack((np.ones_like(rows), rows))
    (constant, slope), *_ = np.linalg.lstsq(design, widths**2 + 4.0 * rows**2, rcond=None)
    fitted = constant + slope**2 / 16.0
    if fitted <= 0.0:
        return widest
    diameter = float(np.sqrt(fitted))
    # A noisy fit may not stray far from what was actually seen.
    return diameter if widest - 0.5 <= diameter <= widest * 1.2 else widest


def _candidates(signal: np.ndarray) -> list[tuple[float, tuple[slice, slice]]]:
    noise = 1.4826 * float(np.median(np.abs(signal - np.median(signal))))
    mask = signal >= max(MIN_CONTRAST, NOISE_K * noise)
    labels, _count = ndimage.label(mask)
    found = []
    for index, box in enumerate(ndimage.find_objects(labels), start=1):
        if box is None:
            continue
        rows, columns = box
        height = rows.stop - rows.start
        width = columns.stop - columns.start
        area = int(np.count_nonzero(labels[box] == index))
        if not MIN_AREA_PX <= area <= MAX_AREA_PX:
            continue
        if not (MIN_SIDE_PX <= width <= MAX_SIDE_PX and MIN_SIDE_PX <= height <= MAX_SIDE_PX):
            continue
        # A ball half hidden in grass is wider than tall, never the reverse.
        if width / height > MAX_ASPECT or height / width > 1.25:
            continue
        if area / (width * height) < MIN_FILL:
            continue
        found.append((float(np.mean(signal[box][labels[box] == index])), box))
    return found


def measure_ball_before_impact(
    frames: np.ndarray,
    *,
    trigger_frame_index: int,
) -> BallSize | None:
    """The ball that left during this clip, or None if no ball clearly did."""
    if frames.ndim != 3 or len(frames) < MIN_FRAMES:
        return None
    if not 0 <= trigger_frame_index < len(frames):
        return None
    after_start = min(len(frames) - AFTER_FRAMES, trigger_frame_index + AFTER_START_FRAMES)
    if after_start < BEFORE_FRAMES:
        return None
    before = np.median(frames[:BEFORE_FRAMES], axis=0).astype(np.float32)
    after = np.median(frames[after_start : after_start + AFTER_FRAMES], axis=0).astype(np.float32)
    signal = ndimage.gaussian_filter(before - after, sigma=0.6)

    candidates = _candidates(signal)
    if not candidates:
        return None
    contrast, (rows, columns) = max(candidates, key=lambda item: item[0])

    margin = 4
    top = max(0, rows.start - margin)
    left = max(0, columns.start - margin)
    crop = signal[top : rows.stop + margin, left : columns.stop + margin]
    peak = float(np.percentile(signal[rows, columns], 90))
    half = peak / 2.0

    spans = np.count_nonzero(crop >= half, axis=1)
    widest_row = int(np.argmax(spans))
    chords: list[tuple[int, float]] = []
    for row_index in range(
        max(0, widest_row - ROW_SPAN), min(len(crop), widest_row + ROW_SPAN + 1)
    ):
        row = crop[row_index]
        above = np.flatnonzero(row >= half)
        if len(above) == 0:
            continue
        width = _half_height_width(row, int((above[0] + above[-1]) / 2), half)
        if width is not None:
            chords.append((row_index, width))
    if not chords:
        return None
    diameter = _diameter_from_chords(chords)
    return BallSize(
        x=float((columns.start + columns.stop - 1) / 2.0),
        y=float((rows.start + rows.stop - 1) / 2.0),
        diameter_px=diameter,
        contrast=contrast,
    )


def measure_ball_in_clip(clip_dir: Path | str) -> BallSize | None:
    """Measure the ball in a saved shot-format clip; None if unreadable or no ball."""
    try:
        with np.load(Path(clip_dir) / "frames.npz") as archive:
            frames = archive["frames"]
            trigger_frame_index = int(archive["pre_trigger_count"]) - 1
    except (OSError, KeyError, ValueError):
        return None
    return measure_ball_before_impact(frames, trigger_frame_index=trigger_frame_index)
