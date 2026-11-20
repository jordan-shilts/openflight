"""Where the target line lies in the camera's view, kept right when the monitor moves.

The camera fit (``flight_fit``) reads the ball's direction relative to the
camera's own axis. Turning that into a direction relative to the target line
needs the target line's direction in the same frame, ``target_yaw_deg`` (+ =
the target lies right of the camera axis). It is set once, then followed:

* Set: from two clubs or alignment sticks laid on the mat parallel to the
  target line. In the image their lines meet at a vanishing point, and the
  ray to that point is the target direction (``target_yaw_from_lines``).
  Without them, the direction can be set to the median start line of a run
  of shots (``target_yaw_from_shots``), which assumes the player's average
  start line is straight; that is a fallback, never the default.
* Followed: the garage behind and right of the ball (mat end, stairs, door,
  lamp stand) does not move, so when the monitor is bumped the whole scene
  slides sideways in the image. Each clip's quiet frames are matched against
  the scene saved when the line was set (normalised cross-correlation of
  smoothed gradients, after removing the sensor's column pattern, which
  otherwise pins any phase-correlation peak at zero shift). A slide of
  ``dx`` pixels turns the target direction by the same angle.

Garage, 2026-11-19: the monitor was turned twice during the session (27 px
at 21:52, 40-42 px by 22:06, i.e. 2.9 and 4.3 deg); the match read 0.98 in
place and ~0.70 after the turns, as low as ~0.27 with the golfer in the way.

The radar's sideways reading turns with the monitor too. Its offset from the
camera, both in the monitor's own frame, is learned from shots both measured,
so a shot the camera missed can still be given a target-relative direction.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

# The fixed scene right of the ball, as fractions of the frame (rows, cols).
SCENE_ROWS = (0.375, 0.6875)
SCENE_COLS = (0.59375, 1.0)
MAX_SHIFT_X_PX = 70  # ~ 7.5 deg either way
MAX_SHIFT_Y_PX = 8
COARSE_FACTOR = 2
REFINE_PX = 2
# Below this the scene is not recognised (golfer in the way, lights changed,
# monitor moved too far): the last good bump stands in, flagged.
MIN_SCENE_MATCH = 0.45
# A bump this large means the monitor was moved, not nudged: the straight
# line should be set again even though the scene still matches.
MOVED_DEG = 6.0
# The golfer can hide the scene for a clip or two; this many unrecognised
# clips in a row means the monitor itself was moved.
LOST_CLIPS_MEAN_MOVED = 3

RADAR_PAIRS_KEPT = 40
RADAR_PAIRS_NEEDED = 8


@dataclass(frozen=True)
class SceneShift:
    """How far the fixed scene slid since the reference, and how well it matched."""

    dx_px: float
    dy_px: float
    match: float


def scene_signature(quiet_mean: np.ndarray) -> np.ndarray:
    """The fixed scene's texture: log brightness, column pattern removed,
    smoothed gradient magnitude, zero mean."""
    height, width = quiet_mean.shape
    rows = slice(int(SCENE_ROWS[0] * height), int(SCENE_ROWS[1] * height))
    cols = slice(int(SCENE_COLS[0] * width), int(SCENE_COLS[1] * width))
    region = np.log1p(np.clip(quiet_mean.astype(np.float64), 0, None))[rows, cols].copy()
    region -= np.median(region, axis=0, keepdims=True)
    gx = np.zeros_like(region)
    gy = np.zeros_like(region)
    gx[:, 1:-1] = region[:, 2:] - region[:, :-2]
    gy[1:-1] = region[2:] - region[:-2]
    texture = ndimage.uniform_filter(np.hypot(gx, gy), size=3, mode="constant")
    return texture - texture.mean()


def _ncc(current: np.ndarray, reference: np.ndarray, dx: int, dy: int) -> float:
    """Normalised correlation with ``current`` = ``reference`` slid by (dx, dy)."""
    h, w = current.shape
    a = current[max(0, dy) : h + min(0, dy), max(0, dx) : w + min(0, dx)]
    b = reference[max(0, -dy) : h + min(0, -dy), max(0, -dx) : w + min(0, -dx)]
    denom = math.sqrt(float((a * a).sum()) * float((b * b).sum())) + 1e-12
    return float((a * b).sum()) / denom


def _best_shift(current, reference, xs, ys) -> tuple[float, int, int]:
    best = (-2.0, 0, 0)
    for dy in ys:
        for dx in xs:
            score = _ncc(current, reference, dx, dy)
            if score > best[0]:
                best = (score, dx, dy)
    return best


def _downsample(image: np.ndarray, factor: int) -> np.ndarray:
    h, w = (image.shape[0] // factor) * factor, (image.shape[1] // factor) * factor
    return image[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def scene_shift(reference: np.ndarray, current: np.ndarray) -> SceneShift:
    """How far ``current``'s scene slid from ``reference`` (+dx = to the right)."""
    if reference.shape != current.shape:
        raise ValueError("scene signatures differ in size; set the straight line again")
    f = COARSE_FACTOR
    coarse = _best_shift(
        _downsample(current, f),
        _downsample(reference, f),
        range(-MAX_SHIFT_X_PX // f, MAX_SHIFT_X_PX // f + 1),
        range(-MAX_SHIFT_Y_PX // f, MAX_SHIFT_Y_PX // f + 1),
    )
    cx, cy = coarse[1] * f, coarse[2] * f
    score, dx, dy = _best_shift(
        current,
        reference,
        range(cx - REFINE_PX, cx + REFINE_PX + 1),
        range(cy - REFINE_PX, cy + REFINE_PX + 1),
    )
    return SceneShift(dx_px=float(dx), dy_px=float(dy), match=score)


def shift_to_yaw_deg(dx_px: float, focal_px: float, width: int) -> float:
    """The turn that slides the scene region's centre by ``dx_px``.

    + = the scene slid right = the camera turned left = the target now lies
    further right of the camera axis.
    """
    centre = (0.5 * (SCENE_COLS[0] + SCENE_COLS[1]) - 0.5) * width
    return math.degrees(math.atan((centre + dx_px) / focal_px) - math.atan(centre / focal_px))


def _line(p1: tuple[float, float], p2: tuple[float, float]) -> np.ndarray:
    return np.cross([p1[0], p1[1], 1.0], [p2[0], p2[1], 1.0])


def vanishing_point(line1, line2) -> tuple[float, float]:
    """Where two image lines, each given as two (x, y) points, meet."""
    point = np.cross(_line(*line1), _line(*line2))
    if abs(point[2]) < 1e-9:
        raise ValueError("the two lines are parallel in the image; lay the clubs further apart")
    return float(point[0] / point[2]), float(point[1] / point[2])


def target_yaw_from_lines(
    line1,
    line2,
    *,
    focal_px: float,
    pitch_deg: float,
    width: int,
    height: int,
    roll_deg: float = 0.0,
) -> float:
    """Target direction (deg, + right of the camera axis) from two lines on the
    mat parallel to the target line. ``pitch_deg`` is the camera pitch (from
    ``flight_fit``'s camera model: the resting ball's row and camera height)."""
    u, v = vanishing_point(line1, line2)
    du, dv = u - width / 2.0, v - height / 2.0
    roll = math.radians(roll_deg)
    du, dv = du * math.cos(roll) + dv * math.sin(roll), -du * math.sin(roll) + dv * math.cos(roll)
    pitch = math.radians(pitch_deg)
    # Ray in camera axes (x right, y forward, z up), then level it.
    x, y, z = du / focal_px, 1.0, -dv / focal_px
    forward = y * math.cos(pitch) - z * math.sin(pitch)
    return math.degrees(math.atan2(x, forward))


def target_yaw_from_shots(hla_axis_deg: list[float]) -> float:
    """Fallback: the median start line of these shots is called straight."""
    values = [value for value in hla_axis_deg if value is not None and math.isfinite(value)]
    if len(values) < RADAR_PAIRS_NEEDED:
        raise ValueError(f"need at least {RADAR_PAIRS_NEEDED} shots, have {len(values)}")
    return float(statistics.median(values))


@dataclass(frozen=True)
class AimReading:
    """The target direction for one clip and how it was reached."""

    status: str  # "set", "unset", "moved" (set it again), "scene_lost" (last good bump used)
    target_yaw_deg: float | None = None
    bump_deg: float | None = None
    shift: SceneShift | None = None

    @property
    def usable(self) -> bool:
        return self.target_yaw_deg is not None and self.status in ("set", "scene_lost")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "target_yaw_deg": None
            if self.target_yaw_deg is None
            else round(self.target_yaw_deg, 2),
            "bump_deg": None if self.bump_deg is None else round(self.bump_deg, 2),
            "scene_dx_px": None if self.shift is None else self.shift.dx_px,
            "scene_dy_px": None if self.shift is None else self.shift.dy_px,
            "scene_match": None if self.shift is None else round(self.shift.match, 3),
        }


@dataclass
class CameraAim:
    """The saved straight line and what follows it; persisted as JSON + .npy."""

    path: Path
    focal_px: float = 535.0
    target_yaw_deg: float | None = None
    method: str | None = None
    set_at: float | None = None
    reference: np.ndarray | None = None
    last_bump_deg: float = 0.0
    radar_pairs: list[float] = field(default_factory=list)
    lost_streak: int = 0

    @property
    def reference_path(self) -> Path:
        return self.path.with_suffix(".scene.npy")

    @classmethod
    def load(cls, path: Path | str, *, focal_px: float = 535.0) -> "CameraAim":
        """The saved aim, or an unset one when there is none (or it is unreadable)."""
        aim = cls(path=Path(path), focal_px=focal_px)
        try:
            data = json.loads(aim.path.read_text())
            reference = np.load(aim.reference_path)
        except (OSError, ValueError):
            return aim
        yaw = data.get("target_yaw_deg")
        if not isinstance(yaw, (int, float)) or not math.isfinite(yaw):
            return aim
        aim.target_yaw_deg = float(yaw)
        aim.method = data.get("method")
        aim.set_at = data.get("set_at")
        aim.reference = reference
        aim.last_bump_deg = float(data.get("last_bump_deg") or 0.0)
        pairs = data.get("radar_pairs") or []
        aim.radar_pairs = [
            float(p) for p in pairs if isinstance(p, (int, float)) and math.isfinite(p)
        ]
        return aim

    def save(self) -> None:
        """Write atomically, so a power cut never leaves half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.reference is not None:
            tmp_npy = self.reference_path.with_suffix(".tmp.npy")
            np.save(tmp_npy, self.reference)
            tmp_npy.replace(self.reference_path)
        data = {
            "target_yaw_deg": self.target_yaw_deg,
            "method": self.method,
            "set_at": self.set_at,
            "last_bump_deg": self.last_bump_deg,
            "radar_pairs": self.radar_pairs[-RADAR_PAIRS_KEPT:],
            "focal_px": self.focal_px,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    def set_straight(self, quiet_mean: np.ndarray, target_yaw_deg: float, method: str) -> None:
        """Save this scene and target direction as the reference."""
        if not math.isfinite(target_yaw_deg):
            raise ValueError("target_yaw_deg must be a number")
        self.target_yaw_deg = float(target_yaw_deg)
        self.method = method
        self.set_at = time.time()
        self.reference = scene_signature(quiet_mean)
        self.last_bump_deg = 0.0
        self.radar_pairs = []
        self.save()

    def read(self, quiet_mean: np.ndarray) -> AimReading:
        """The target direction for a clip whose quiet frames average to ``quiet_mean``."""
        if self.target_yaw_deg is None or self.reference is None:
            return AimReading(status="unset")
        try:
            shift = scene_shift(self.reference, scene_signature(quiet_mean))
        except ValueError:
            return AimReading(status="moved")
        if shift.match < MIN_SCENE_MATCH:
            self.lost_streak += 1
            if self.lost_streak >= LOST_CLIPS_MEAN_MOVED:
                return AimReading(status="moved", shift=shift)
            return AimReading(
                status="scene_lost",
                target_yaw_deg=self.target_yaw_deg + self.last_bump_deg,
                bump_deg=self.last_bump_deg,
                shift=shift,
            )
        self.lost_streak = 0
        bump = shift_to_yaw_deg(shift.dx_px, self.focal_px, quiet_mean.shape[1])
        if abs(bump) > MOVED_DEG:
            return AimReading(status="moved", bump_deg=bump, shift=shift)
        self.last_bump_deg = bump
        return AimReading(
            status="set", target_yaw_deg=self.target_yaw_deg + bump, bump_deg=bump, shift=shift
        )

    def learn_radar(self, radar_hla_deg: float, camera_hla_axis_deg: float) -> None:
        """Remember one shot's radar-minus-camera sideways reading (monitor frame)."""
        diff = radar_hla_deg - camera_hla_axis_deg
        if math.isfinite(diff):
            self.radar_pairs = (self.radar_pairs + [float(diff)])[-RADAR_PAIRS_KEPT:]

    def radar_offset_deg(self) -> float | None:
        """Median radar-minus-camera offset once enough shots had both."""
        if len(self.radar_pairs) < RADAR_PAIRS_NEEDED:
            return None
        return float(statistics.median(self.radar_pairs))
