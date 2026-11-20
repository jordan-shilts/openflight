"""Launch angles from one swing clip, by fitting the ball's whole flight at once.

The recorder's camera sits in the monitor behind the ball, a few centimetres
above it, looking down the target line. In the dim garage the struck ball is a
faint smudge a few pixels across, too weak to find frame by frame (the
centroid tracker in ``ball_flight.py`` loses it), but a straight-line flight
model can be scored against all the frames together:

* the resting ball is found in the quiet frames at the start of the clip;
* each frame from just before impact onwards becomes a "blob map" of
  (frame - quiet background): the Hessian-determinant strength of small bright
  spots, which is ~0 on lines such as the shaft and the mat edge;
* the radar's ball speed is held fixed and three numbers are searched:
  horizontal launch (HLA), vertical launch (VLA) and the impact time; each
  candidate flight is projected through the camera model and scored by the
  blob maps along it; a coarse grid is refined twice.

Garage, 2026-11-19: on 104 swings the fit followed the ball itself on every
full swing (checked frame by frame against overlays); on synthetic flights it
is within 0.1 deg (HLA) and 0.2 deg (VLA) even with a shaft crossing the path.

Geometry: the camera's pitch comes from the resting ball's row together with
the camera height above the ball and the ball distance, so a monitor that
tilts is followed automatically. VLA errs 1:1 with an error in that pitch,
i.e. with the assumed camera height (1 cm ~ 0.4 deg at 1.55 m). HLA is
relative to the camera's own axis; where the target line lies in that frame is
``camera_aim``'s business. Camera roll leaks vertical motion into HLA (about
roll x tan(VLA)), so a known roll is applied in the projection.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage

BALL_DIAMETER_M = 0.04267

# The first frames of a clip, before the swing reaches the ball: the background
# and the resting ball. A clip holds ~36 frames before the trigger.
QUIET_FRAMES = 12
# Flight frames: one before the trigger (it fires about a frame before impact)
# through FLIGHT_WINDOW_S after it, twelve frames at 230 fps; by then a full
# shot has left the top of the view. In time, not frames, so a faster camera
# setting simply fits more frames.
FRAMES_BEFORE_TRIGGER = 1
FLIGHT_WINDOW_S = 0.053
BLOB_SIGMA_PX = 2.5
# Within this distance of the tee the ball still overlaps its resting spot,
# which the background subtraction has removed: those samples don't score.
MIN_TRAVEL_M = 0.04
# One very bright sample (a club glint) must not outvote a whole track.
SAMPLE_CLIP = 40.0
# A sample counts as the ball seen when it stands this far above the noise.
HIT_NOISE_K = 4.0
HIT_NOISE_FLOOR = 0.3

HLA_RANGE_DEG = (-25.0, 25.0)
VLA_RANGE_DEG = (-4.0, 50.0)
IMPACT_RANGE_S = (-0.006, 0.010)  # impact time relative to the trigger frame
COARSE_STEP_DEG = 1.0
COARSE_STEP_S = 0.0005
REFINE_STEPS = ((0.2, 0.0001), (0.05, 0.00003))
REFINE_SPAN = 6

# Accepting a fit: the ball must be seen in enough frames, and the best flight
# must not sit on the edge of the search (the real one is then outside it).
# Garage, 2026-11-19: every fit that followed a real ball had 7+ hits; the two
# that followed something else (shaft glints, a leg) had 6.
MIN_HITS = 7
MIN_MOVING_FRAMES = 6
# A hit must be new (absent this many frames earlier), so a glint that sits
# still is not read as a flight.
FRESH_GAP_FRAMES = 2
# The tee spot must turn dark by this fraction of the ball's contrast once the
# ball has gone; a ball that never left means the clip missed the strike.
BALL_SPOT_RADIUS_PX = 4
BALL_GONE_FRACTION = 0.3
# Dropped frames: a step this many times the clip's usual frame interval.
MAX_FRAME_STEP_RATIO = 1.5

# Where a resting ball can be, as fractions of the frame (rows, then columns).
REST_SEARCH = (0.50, 0.655, 0.3125, 0.8125)
REST_HINT_RADIUS_PX = 20
REST_PATCH_HALF_PX = 15
REST_DISC_RADIUS_PX = 11
REST_MIN_CONTRAST = 3.0


@dataclass(frozen=True)
class CameraGeometry:
    """How the camera sits relative to the resting ball.

    ``height_above_ball_m`` is the lens height above the ball's centre;
    ``ball_distance_m`` the horizontal lens-to-ball distance; ``roll_deg`` the
    image rotation, positive when the scene appears turned clockwise.
    """

    focal_px: float = 535.0
    height_above_ball_m: float = 0.08
    ball_distance_m: float = 1.549
    roll_deg: float = 0.0

    def __post_init__(self) -> None:
        if not (math.isfinite(self.focal_px) and self.focal_px > 0):
            raise ValueError("focal_px must be a positive number")
        if not (math.isfinite(self.ball_distance_m) and self.ball_distance_m > 0):
            raise ValueError("ball_distance_m must be a positive number")
        if not math.isfinite(self.height_above_ball_m):
            raise ValueError("height_above_ball_m must be a number")
        if not math.isfinite(self.roll_deg):
            raise ValueError("roll_deg must be a number")


@dataclass(frozen=True)
class RestingBall:
    """The ball before the swing, in pixels."""

    u: float
    v: float
    diameter_px: float
    contrast: float


@dataclass(frozen=True)
class CameraLaunch:
    """One clip's launch reading. Angles in degrees; ``accepted`` says usable.

    ``hla_axis_deg`` is relative to the camera axis, + to the right as the
    camera sees it. ``impact_ms`` is relative to the trigger frame.
    """

    accepted: bool
    status: str
    hla_axis_deg: float | None = None
    vla_deg: float | None = None
    impact_ms: float | None = None
    hits: int = 0
    moving_frames: int = 0
    score: float = 0.0
    ball_u: float | None = None
    ball_v: float | None = None
    pitch_deg: float | None = None
    ball_lateral_m: float | None = None

    def to_dict(self) -> dict:
        """JSON-ready, angles rounded to what the fit resolves."""
        out = asdict(self)
        for key in ("hla_axis_deg", "vla_deg", "pitch_deg"):
            if out[key] is not None:
                out[key] = round(out[key], 2)
        for key in ("impact_ms", "score", "ball_u", "ball_v"):
            if out[key] is not None:
                out[key] = round(out[key], 1)
        if out["ball_lateral_m"] is not None:
            out["ball_lateral_m"] = round(out["ball_lateral_m"], 3)
        return out


class CameraModel:
    """Pinhole camera behind the ball; world X right, Y toward the target, Z up.

    The pitch is set so the resting ball projects where it was seen.
    """

    def __init__(self, geometry: CameraGeometry, width: int, height: int, ball: RestingBall):
        self.f = geometry.focal_px
        self.cx = width / 2.0
        self.cy = height / 2.0
        roll = math.radians(geometry.roll_deg)
        self.cos_roll = math.cos(roll)
        self.sin_roll = math.sin(roll)
        # Undo the roll on the seen ball before reading pitch and lateral offset.
        du, dv = self._unroll(ball.u - self.cx, ball.v - self.cy)
        dh = geometry.height_above_ball_m
        dist = geometry.ball_distance_m
        depression = math.atan2(dh, dist)
        self.pitch = math.atan(dv / self.f) - depression
        depth = dist * math.cos(self.pitch) - dh * math.sin(self.pitch)
        self.ball_x = du * depth / self.f
        self.ball_y = dist
        self.ball_z = -dh

    def _unroll(self, du, dv):
        return (
            du * self.cos_roll + dv * self.sin_roll,
            -du * self.sin_roll + dv * self.cos_roll,
        )

    def project(self, x, y, z):
        """Pixel coordinates of world points (arrays broadcast)."""
        c, s = math.cos(self.pitch), math.sin(self.pitch)
        depth = y * c + z * s
        up = -y * s + z * c
        du = self.f * x / depth
        dv = -self.f * up / depth
        return (
            self.cx + du * self.cos_roll - dv * self.sin_roll,
            self.cy + du * self.sin_roll + dv * self.cos_roll,
        )

    def flight(self, hla, vla, impact_s, times_s, speed_mps):
        """Projected ball positions and distance travelled at ``times_s``."""
        travel = np.clip(times_s - impact_s, 0.0, None) * speed_mps
        x = self.ball_x + travel * np.sin(hla) * np.cos(vla)
        y = self.ball_y + travel * np.cos(hla) * np.cos(vla)
        z = self.ball_z + travel * np.sin(vla)
        u, v = self.project(x, y, z)
        return u, v, travel


def blob_map(image: np.ndarray, sigma: float = BLOB_SIGMA_PX) -> np.ndarray:
    """Bright-blob strength: sqrt of the Hessian determinant where both
    curvatures are negative, scaled by sigma^2. Lines give ~0."""
    smooth = ndimage.gaussian_filter(image.astype(np.float32), sigma, mode="nearest")
    lxx = np.zeros_like(smooth)
    lyy = np.zeros_like(smooth)
    lxy = np.zeros_like(smooth)
    lxx[:, 1:-1] = smooth[:, 2:] - 2 * smooth[:, 1:-1] + smooth[:, :-2]
    lyy[1:-1, :] = smooth[2:, :] - 2 * smooth[1:-1, :] + smooth[:-2, :]
    lxy[1:-1, 1:-1] = (smooth[2:, 2:] - smooth[2:, :-2] - smooth[:-2, 2:] + smooth[:-2, :-2]) / 4
    det = lxx * lyy - lxy**2
    ok = (lxx < 0) & (lyy < 0) & (det > 0)
    return np.where(ok, np.sqrt(np.where(ok, det, 0.0)) * sigma**2, 0.0).astype(np.float32)


def _box_mean(image: np.ndarray, radius: int) -> np.ndarray:
    return ndimage.uniform_filter(image, size=2 * radius + 1, mode="nearest")


def find_resting_ball(
    quiet_mean: np.ndarray, hint: tuple[float, float] | None = None
) -> RestingBall | None:
    """The brightest small spot where a resting ball can be, or None.

    ``hint`` (x, y) narrows the search to near a ball found another way (the
    recorder's departure detector).
    """
    height, width = quiet_mean.shape
    r0, r1 = int(REST_SEARCH[0] * height), int(REST_SEARCH[1] * height)
    c0, c1 = int(REST_SEARCH[2] * width), int(REST_SEARCH[3] * width)
    if hint is not None:
        hx, hy = hint
        if not (math.isfinite(hx) and math.isfinite(hy)):
            hint = None
        else:
            r0 = max(int(hy) - REST_HINT_RADIUS_PX, 0)
            r1 = min(int(hy) + REST_HINT_RADIUS_PX + 1, height)
            c0 = max(int(hx) - REST_HINT_RADIUS_PX, 0)
            c1 = min(int(hx) + REST_HINT_RADIUS_PX + 1, width)
    image = quiet_mean.astype(np.float32)
    dog = _box_mean(image, 2) - _box_mean(image, 7)
    window = dog[r0:r1, c0:c1]
    if window.size == 0:
        return None
    y, x = np.unravel_index(int(np.argmax(window)), window.shape)
    y, x = int(y) + r0, int(x) + c0
    half = REST_PATCH_HALF_PX
    patch = image[y - half : y + half + 1, x - half : x + half + 1]
    if patch.shape != (2 * half + 1, 2 * half + 1):
        return None
    ring = np.concatenate(
        [patch[:3].ravel(), patch[-3:].ravel(), patch[:, :3].ravel(), patch[:, -3:].ravel()]
    )
    signal = patch - float(np.median(ring))
    peak = float(signal.max())
    if peak < REST_MIN_CONTRAST:
        return None
    yy, xx = np.mgrid[-half : half + 1, -half : half + 1]
    near = (yy**2 + xx**2) <= REST_DISC_RADIUS_PX**2
    weights = np.where(near, np.clip(signal, 0, None), 0.0)
    total = float(weights.sum())
    if total <= 0:
        return None
    lit = near & (signal > peak / 2)
    return RestingBall(
        u=float((weights * xx).sum() / total) + x,
        v=float((weights * yy).sum() / total) + y,
        diameter_px=2 * math.sqrt(float(lit.sum()) / math.pi),
        contrast=peak,
    )


def _sample(maps: np.ndarray, frame_idx: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinear samples of maps[k] at (u, v); outside the maps -> 0."""
    _count, height, width = maps.shape
    inside = (u >= 0) & (u < width - 1) & (v >= 0) & (v < height - 1)
    u = np.clip(u, 0, width - 1.001)
    v = np.clip(v, 0, height - 1.001)
    u0 = np.floor(u).astype(np.intp)
    v0 = np.floor(v).astype(np.intp)
    fu = u - u0
    fv = v - v0
    k = np.broadcast_to(frame_idx, u.shape)
    value = (
        maps[k, v0, u0] * (1 - fu) * (1 - fv)
        + maps[k, v0, u0 + 1] * fu * (1 - fv)
        + maps[k, v0 + 1, u0] * (1 - fu) * fv
        + maps[k, v0 + 1, u0 + 1] * fu * fv
    )
    return np.where(inside, value, 0.0)


def _grid(low: float, high: float, step: float) -> np.ndarray:
    return np.arange(low, high + step / 2, step)


def fit_flight(
    maps: np.ndarray,
    times_s: np.ndarray,
    camera: CameraModel,
    speed_mps: float,
    *,
    origin: tuple[int, int] = (0, 0),
) -> tuple[tuple[float, float, float], float, bool]:
    """Best (hla, vla, impact) in radians/seconds, its score, and whether it
    sits on the edge of the coarse search. ``origin`` is the (row, col) of the
    maps' top-left pixel in the full frame."""
    frame_idx = np.arange(len(times_s))
    row0, col0 = origin

    def score(hla, vla, impact):
        u, v, travel = camera.flight(
            hla[..., None], vla[..., None], impact[..., None], times_s, speed_mps
        )
        value = _sample(maps, frame_idx, u - col0, v - row0)
        value = np.where(travel > MIN_TRAVEL_M, np.clip(value, 0.0, SAMPLE_CLIP), 0.0)
        return value.sum(-1)

    hla_grid = np.radians(_grid(*HLA_RANGE_DEG, COARSE_STEP_DEG))
    vla_grid = np.radians(_grid(*VLA_RANGE_DEG, COARSE_STEP_DEG))
    impact_grid = _grid(*IMPACT_RANGE_S, COARSE_STEP_S)
    grids = (hla_grid, vla_grid, impact_grid)
    values = score(*np.meshgrid(*grids, indexing="ij"))
    best_index = np.unravel_index(int(np.argmax(values)), values.shape)
    on_edge = any(i in (0, len(g) - 1) for i, g in zip(best_index, grids))
    best = tuple(float(g[i]) for i, g in zip(best_index, grids))
    best_score = float(values.max())
    for step_deg, step_s in REFINE_STEPS:
        offsets = np.arange(-REFINE_SPAN, REFINE_SPAN + 1)
        grids = (
            best[0] + np.radians(offsets * step_deg),
            best[1] + np.radians(offsets * step_deg),
            best[2] + offsets * step_s,
        )
        values = score(*np.meshgrid(*grids, indexing="ij"))
        index = np.unravel_index(int(np.argmax(values)), values.shape)
        if float(values[index]) >= best_score:
            best = tuple(float(g[i]) for i, g in zip(index, grids))
            best_score = float(values[index])
    return best, best_score, on_edge


def _flight_window(
    ball: RestingBall, width: int, height: int, focal_px: float
) -> tuple[int, int, int, int]:
    """Rows/cols the flight can reach in the fitted frames: above the ball and
    either side of it. Cropping the maps keeps the fit fast on a Pi."""
    side = int(0.30 * focal_px)  # ~ +-25 deg of HLA at the far end of the window
    return (
        0,
        min(int(ball.v + 3 * REST_PATCH_HALF_PX), height),
        max(int(ball.u) - side, 0),
        min(int(ball.u) + side, width),
    )


def _count_hits(frames, indices, maps, background, window, track) -> tuple[int, int]:
    """Frames that saw the ball where the fitted flight puts it, and frames in
    which the fitted ball had left the tee.

    A hit is a bright spot on the fitted position that is also new, i.e. was
    not there ``FRESH_GAP_FRAMES`` earlier, so a glint that sits still (a
    clubhead resting behind the ball) does not count.
    """
    r0, r1, c0, c1 = window
    u, v, travel = track
    quiet = blob_map(frames[1, r0:r1, c0:c1].astype(np.float32) - background[r0:r1, c0:c1])
    threshold = HIT_NOISE_K * max(float(np.std(quiet)), HIT_NOISE_FLOOR)
    frame_idx = np.arange(len(indices))
    seen = _sample(maps, frame_idx, u - c0, v - r0) > threshold
    fresh_maps = np.stack(
        [
            blob_map(
                frames[i, r0:r1, c0:c1].astype(np.float32)
                - frames[i - FRESH_GAP_FRAMES, r0:r1, c0:c1]
            )
            for i in indices
        ]
    )
    fresh = _sample(fresh_maps, frame_idx, u - c0, v - r0) > threshold
    moving = travel > MIN_TRAVEL_M
    return int((seen & fresh & moving).sum()), int(moving.sum())


def _ball_left(
    frames: np.ndarray, quiet_mean: np.ndarray, ball: RestingBall, frame_index: int
) -> bool:
    """Whether the tee spot is dark (ball gone) in this frame and at the clip's end.

    A struck ball uncovers the mat within a frame or two and the spot stays
    dark. Garage, 2026-11-19: on every clip the fit followed a ball, the spot
    read -0.45..-0.9 of the ball's contrast; on the waggle and address clips
    that fitted a still clubhead instead it read +0.1..+0.2.
    """
    u, v = int(round(ball.u)), int(round(ball.v))
    radius = BALL_SPOT_RADIUS_PX
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    disc = yy**2 + xx**2 <= radius**2
    if not (radius <= v < frames.shape[1] - radius and radius <= u < frames.shape[2] - radius):
        return False
    base = quiet_mean[v - radius : v + radius + 1, u - radius : u + radius + 1]
    for k in (frame_index, len(frames) - 1):
        spot = (
            frames[k, v - radius : v + radius + 1, u - radius : u + radius + 1].astype(np.float32)
            - base
        )
        if float(spot[disc].mean()) > -BALL_GONE_FRACTION * ball.contrast:
            return False
    return True


def _frame_gap(timestamps_ns: np.ndarray, first: int, last: int) -> bool:
    """Whether frames were dropped between ``first`` and ``last``: the fit's
    times would be right but the ball can jump past the blob maps."""
    steps = np.diff(timestamps_ns.astype(np.int64))
    if len(steps) == 0:
        return True
    nominal = float(np.median(steps))
    return bool(np.any(steps[first:last] > MAX_FRAME_STEP_RATIO * nominal))


def measure_launch(
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    trigger_index: int,
    ball_speed_mps: float,
    geometry: CameraGeometry,
    *,
    ball_hint: tuple[float, float] | None = None,
) -> CameraLaunch:
    """Fit the ball's flight in one clip. Never raises on bad clips: the
    reason is in ``status``."""
    if not (math.isfinite(ball_speed_mps) and ball_speed_mps > 0):
        return CameraLaunch(accepted=False, status="no_speed")
    if frames.ndim != 3 or len(frames) != len(timestamps_ns):
        return CameraLaunch(accepted=False, status="bad_clip")
    first = trigger_index - FRAMES_BEFORE_TRIGGER
    after = (timestamps_ns.astype(np.int64) - int(timestamps_ns[trigger_index])) / 1e9
    last = int(np.searchsorted(after, FLIGHT_WINDOW_S, side="right")) - 1
    last = min(max(last, trigger_index), len(frames) - 1)
    if first < QUIET_FRAMES or last - first < MIN_MOVING_FRAMES:
        return CameraLaunch(accepted=False, status="clip_too_short")
    quiet = frames[:QUIET_FRAMES].astype(np.float32)
    background = np.median(quiet, axis=0)
    ball = find_resting_ball(quiet.mean(axis=0), hint=ball_hint)
    if ball is None:
        return CameraLaunch(accepted=False, status="no_resting_ball")
    _count, height, width = frames.shape
    camera = CameraModel(geometry, width, height, ball)
    r0, r1, c0, c1 = _flight_window(ball, width, height, geometry.focal_px)
    indices = np.arange(first, last + 1)
    maps = np.stack(
        [
            blob_map(frames[i, r0:r1, c0:c1].astype(np.float32) - background[r0:r1, c0:c1])
            for i in indices
        ]
    )
    times = (timestamps_ns[indices].astype(np.int64) - int(timestamps_ns[trigger_index])) / 1e9
    (hla, vla, impact), score, on_edge = fit_flight(
        maps, times, camera, ball_speed_mps, origin=(r0, c0)
    )

    hits, moving_frames = _count_hits(
        frames,
        indices,
        maps,
        background,
        (r0, r1, c0, c1),
        camera.flight(np.array(hla), np.array(vla), np.array(impact), times, ball_speed_mps),
    )
    if _frame_gap(timestamps_ns, first, last):
        status = "frames_dropped"
    elif on_edge:
        status = "edge_of_search"
    elif moving_frames < MIN_MOVING_FRAMES:
        status = "too_few_frames"
    elif not _ball_left(frames, quiet.mean(axis=0), ball, last):
        status = "ball_stayed"
    elif hits < MIN_HITS:
        status = "ball_not_seen"
    else:
        status = "ok"
    return CameraLaunch(
        accepted=status == "ok",
        status=status,
        hla_axis_deg=math.degrees(hla),
        vla_deg=math.degrees(vla),
        impact_ms=impact * 1000.0,
        hits=hits,
        moving_frames=moving_frames,
        score=score,
        ball_u=ball.u,
        ball_v=ball.v,
        pitch_deg=math.degrees(camera.pitch),
        ball_lateral_m=camera.ball_x,
    )


def load_clip(clip_dir: Path | str) -> tuple[np.ndarray, np.ndarray, int]:
    """Frames, sensor timestamps and the trigger frame index of a saved clip."""
    with np.load(Path(clip_dir) / "frames.npz") as archive:
        frames = archive["frames"]
        stamps = archive["sensor_timestamp_ns"].astype(np.int64)
        trigger_index = int(archive["pre_trigger_count"]) - 1
    return frames, stamps, trigger_index


def measure_clip(
    clip_dir: Path | str,
    ball_speed_mps: float,
    geometry: CameraGeometry,
    *,
    ball_hint: tuple[float, float] | None = None,
) -> CameraLaunch:
    """``measure_launch`` on a clip folder; a missing or broken clip is a status."""
    try:
        frames, stamps, trigger_index = load_clip(clip_dir)
    except (OSError, KeyError, ValueError) as error:
        return CameraLaunch(accepted=False, status=f"clip_unreadable: {type(error).__name__}")
    return measure_launch(
        frames, stamps, trigger_index, ball_speed_mps, geometry, ball_hint=ball_hint
    )


def clip_trigger_timestamp(clip_dir: Path | str) -> float | None:
    """The trigger time a clip's metadata records, or None."""
    try:
        return float(
            json.loads((Path(clip_dir) / "metadata.json").read_text())["trigger_timestamp"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
