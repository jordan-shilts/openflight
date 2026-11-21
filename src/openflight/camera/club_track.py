"""Club path and attack angle at impact from the rear high-speed camera (experimental).

The camera sits in the monitor behind the ball, low (~8 cm above the ball
centre), looking down the target line. At ~230 fps the clubhead is in view for
about four frames before impact and five after it, so every frame on both
sides of impact is used:

1. **Shaft line** (precise). The chrome shaft is a thin bright ridge in
   frame-minus-background. A Hough search plus a per-row sub-pixel centroid fit
   gives its image line to about half a pixel. The heel (shaft axis at sole
   height) sits on that line, which pins it *across* the shaft, a mostly
   horizontal direction in the image.
2. **Head along the shaft** (rougher). Before impact the head seen from behind
   is a faint U (heel corner, sole, toe edge, toe glint); after impact it shows
   two bright sole lines. A template of each look, sampled in shaft-aligned
   coordinates at a few scales, finds where along the line the heel is.
3. **Arc fit through impact.** Up to impact the heel moves on a circular arc
   (radius, plane tilt, speed from ball speed / smash) that reaches the ball at
   the impact time measured from the ball's flight. At impact its velocity is
   kicked by ``-kick * v_ball`` (momentum to the ball, along the ball's launch
   direction), after which it follows a second arc. Path and attack angle are
   the pre-impact velocity *at* impact. Line constraints are weighted by their
   perpendicular precision, the along-shaft head positions more loosely, and
   radius, plane, speed, kick and the heel's offset from the ball carry priors.

Angles are relative to the camera axis: path ``+`` = club moving right in the
image (in-to-out for a right-handed golfer), attack angle ``+`` = upward.
Only numpy and scipy are used, so it runs on a Raspberry Pi 5.

Status (2026-11-21): research grade, NOT validated against a reference club
measurement. On synthetic clubs rendered into real garage frames it meets
+-1.5 deg (path) on most clips, but on the 2026-11-19 garage swings the
pre-impact-only and post-impact-only halves of the fit disagree by ~+3.5 deg
(path) and ~-2.5 deg (attack angle) on average, so an unmodelled effect of that
size is present. Until a reference check explains it, every result is capped
at ``confidence="low"`` (see ``CONFIDENCE_CAP``), which the kiosk does not let
drive ball flight. ``raw_confidence`` keeps the uncapped tier for research.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace

import numpy as np
from scipy import fft as sp_fft
from scipy.optimize import least_squares

# --- what callers may rely on ---------------------------------------------------
# Highest confidence tier this estimator may report. "low" until the method is
# checked against a reference club measurement (see the module docstring).
CONFIDENCE_CAP = "low"
_TIERS = ("low", "medium", "high")

# --- frames used -----------------------------------------------------------------
WINDOW_BEFORE_IMPACT_S = 0.022
WINDOW_AFTER_IMPACT_S = 0.026
BACKGROUND_FRAMES = 12  # quiet frames at the start of the clip: no club near the ball
MIN_FRAMES_BEFORE_WINDOW = 4
# Head over the ball (just before impact) or ball over the head (just after):
# the head template is unreliable there, the shaft line is still used.
PRE_HEAD_GUARD_S = 0.003
POST_HEAD_GUARD_S = 0.001

# --- region of interest (pixels, relative to the resting ball) -------------------
ROI_LEFT_PX = 210
ROI_RIGHT_PX = 90
ROI_BELOW_PX = 24
MASK_BELOW_PX = 18  # head pixels further below the ball centre are ignored (mat edge)
BALL_MASK_RADIUS_PX = 10.0
FLYING_BALL_MASK_RADIUS_PX = 11.0

# --- shaft line ---------------------------------------------------------------------
RIDGE_HALF_PX = 4
RIDGE_CAP = 40.0
SHAFT_ANGLES_DEG = np.arange(46.0, 85.0, 2.0)
HOUGH_ROW_STEP = 3
HOUGH_COL_STEP = 2
HOUGH_TRACK_DEG = 9.0  # once the shaft is found near impact, search this close to its angle
MIN_SHAFT_ROWS = 25
MIN_SHAFT_SCORE = 300.0
LOWER_SHAFT_ROWS = 110  # shaft rows above the head used for the final line
MAX_SHAFT_RMS_PX = 1.6
LINE_ANGLE_TOL_DEG = 6.0  # lines this far from the near-impact shaft angle are dropped

# --- head templates -------------------------------------------------------------------
PRE_ROWS = (-30, 13)  # template rows relative to the heel anchor (scale 1)
PRE_COLS = (-8, 46)  # columns: horizontal offset from the shaft line (toe to the right)
PRE_CLIP = (-10.0, 15.0)
PRE_SCALES = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3)
POST_ROWS = (-36, 8)
POST_COLS = (-8, 40)
POST_CLIP = (-10.0, 30.0)
POST_SCALES = (0.85, 1.0, 1.15)
MIN_PRE_NCC = 0.5
MIN_POST_NCC = 0.45

# --- fit --------------------------------------------------------------------------------
SIGMA_PERP_PX = 0.5  # heel distance from the measured shaft line
SIGMA_ALONG_PRE_PX = 2.0  # heel row from the pre-impact template
SIGMA_ALONG_POST_PX = 1.5  # heel row from the post-impact template
HEAD_GATE_PX = 6.0
SEED_GATE_PX = 12.0  # first gate, around the arc fitted to the post-impact heads
ROBUST_SCALE = 2.0  # soft-L1 loss scale (in sigmas)
MIN_LINES = 4
MIN_HEADS = 3
MAX_ABS_PATH_DEG = 20.0
MAT_FLOOR = True
FLOOR_SOFTNESS_M = 0.002
MAX_ABS_ATTACK_DEG = 20.0
MAX_NUISANCE_Z = 3.0  # swing-shape parameters further than this from their priors: implausible
MIN_BALL_SPEED_MPS = 10.0


@dataclass(frozen=True)
class ClubTrackGeometry:
    """Rear-camera geometry (same numbers as the ball-flight fit).

    The camera is level in roll; its pitch comes from the resting ball's image
    row. ``camera_height_m`` is the lens above the ball centre and
    ``ball_distance_m`` the horizontal lens-to-ball distance. ``lens`` is
    ``"ftheta"`` (angle maps evenly to pixels, as measured on the garage
    camera) or ``"pinhole"``.
    """

    focal_px: float = 535.0
    camera_height_m: float = 0.08
    ball_distance_m: float = 1.549
    principal_point: tuple[float, float] | None = None  # default: image centre
    lens: str = "ftheta"


@dataclass(frozen=True)
class ClubTrackPriors:  # pylint: disable=too-many-instance-attributes
    """Swing priors (mean, standard deviation); deliberately broad.

    Heel offset: the tracked point (shaft axis at sole height) relative to the
    ball centre at impact -- toward the golfer, behind the ball, near the mat.
    ``kick`` is ball mass / effective head mass: the head loses ``kick * v_ball``
    at impact. ``post_*`` describe the heel's arc after impact.
    """

    path_deg: tuple[float, float] = (0.0, 10.0)
    attack_deg: tuple[float, float] = (-3.0, 8.0)
    plane_deg: tuple[float, float] = (62.0, 8.0)
    radius_m: tuple[float, float] = (1.3, 0.35)
    smash: float = 1.38
    speed_sd_frac: float = 0.05
    heel_offset_m: tuple[float, float, float] = (-0.035, -0.035, -0.018)
    heel_offset_sd_m: tuple[float, float, float] = (0.02, 0.02, 0.008)
    kick: tuple[float, float] = (0.18, 0.03)
    post_anchor_dz_m: tuple[float, float] = (0.0, 0.004)
    post_plane_deg: tuple[float, float] = (68.0, 12.0)
    post_radius_m: tuple[float, float] = (1.6, 0.5)
    # Lowest heel-anchor height relative to the ball centre (mat surface at
    # -ball radius, less how far the head presses into the mat).
    floor_m: tuple[float, float] = (-0.025, 0.004)
    # Ball launch assumed for the kick direction when none is given.
    assumed_launch_deg: tuple[float, float] = (0.0, 14.0)


@dataclass(frozen=True)
class ClubFramePoint:  # pylint: disable=too-many-instance-attributes
    """One frame's club measurement (full-image pixels)."""

    tau_s: float  # frame time relative to impact
    post: bool  # after impact
    line_u_at_ref: float  # shaft line: u = line_u_at_ref + line_slope * (v - ref_row)
    line_slope: float
    ref_row: float
    line_rms_px: float
    head_u: float | None  # heel anchor (None when the head was not found)
    head_v: float | None
    head_ncc: float
    head_scale: float | None
    used_head: bool = False  # head position entered the final fit


@dataclass(frozen=True)
class ClubDelivery:  # pylint: disable=too-many-instance-attributes
    """Club path / attack angle at impact, or why there is none."""

    path_deg: float | None
    attack_angle_deg: float | None
    status: str
    confidence: str | None  # "high" | "medium" | "low" | None (capped, see CONFIDENCE_CAP)
    n_points: int  # head positions used by the fit (pre + post)
    n_lines: int  # frames whose shaft line entered the fit
    rms_px: float | None  # robust fit residual in sigmas (~1 = as expected)
    n_pre: int = 0
    n_post: int = 0
    path_se_deg: float | None = None
    attack_se_deg: float | None = None
    plane_deg: float | None = None
    radius_m: float | None = None
    club_speed_mps: float | None = None
    kick: float | None = None
    raw_confidence: str | None = None  # tier before the cap
    launch_assumed: bool = False  # ball launch direction not given; kick direction assumed
    frames: tuple[ClubFramePoint, ...] = field(default_factory=tuple)
    fit_params: tuple[float, ...] | None = None
    elapsed_ms: float = 0.0


# --- camera model ---------------------------------------------------------------------


class _Camera:  # pylint: disable=too-few-public-methods
    """Camera behind the ball (world X right, Y toward the target, Z up; metres).

    Pitch and the ball's lateral offset are solved so the resting ball centre
    projects where it was seen.
    """

    def __init__(
        self, geometry: ClubTrackGeometry, ball_uv: tuple[float, float], shape: tuple[int, int]
    ):
        height, width = shape
        if geometry.principal_point is None:
            self.cx, self.cy = width / 2.0, height / 2.0
        else:
            self.cx, self.cy = geometry.principal_point
        self.f = geometry.focal_px
        self.ftheta = geometry.lens == "ftheta"
        dist, dh = geometry.ball_distance_m, geometry.camera_height_m
        target = np.array(ball_uv, dtype=float)
        pitch = math.atan((target[1] - self.cy) / self.f) - math.atan2(dh, dist)
        xb = (target[0] - self.cx) * dist / self.f
        for _ in range(10):
            self._set_pitch(pitch)
            uv = self.project(np.array([xb, dist, -dh]))
            eps = 1e-6
            self._set_pitch(pitch + eps)
            d_pitch = (self.project(np.array([xb, dist, -dh])) - uv) / eps
            self._set_pitch(pitch)
            d_x = (self.project(np.array([xb + eps, dist, -dh])) - uv) / eps
            step = np.linalg.solve(np.column_stack([d_pitch, d_x]), target - uv)
            pitch += float(step[0])
            xb += float(step[1])
            if abs(step[0]) < 1e-10 and abs(step[1]) < 1e-9:
                break
        self._set_pitch(pitch)
        self.ball = np.array([xb, dist, -dh])

    def _set_pitch(self, pitch: float) -> None:
        self.pitch = pitch
        self._c, self._s = math.cos(pitch), math.sin(pitch)

    def project(self, points: np.ndarray) -> np.ndarray:
        """Pixel coordinates (..., 2) of world points (..., 3)."""
        points = np.asarray(points, dtype=float)
        x, y, z = points[..., 0], points[..., 1], points[..., 2]
        yc = y * self._c + z * self._s
        zc = -y * self._s + z * self._c
        if self.ftheta:
            rho = np.hypot(x, zc)
            k = self.f * np.arctan2(rho, yc) / np.maximum(rho, 1e-12)
        else:
            k = self.f / yc
        return np.stack([self.cx + k * x, self.cy - k * zc], axis=-1)


# --- swing model ------------------------------------------------------------------------


def _arc_basis(path: float, aoa: float, plane: float) -> tuple[np.ndarray, np.ndarray]:
    """Unit tangent and the in-plane normal toward the arc centre (up and toward the golfer)."""
    sp, cp, sa, ca = math.sin(path), math.cos(path), math.sin(aoa), math.cos(aoa)
    tangent = np.array([sp * ca, cp * ca, sa])
    up = np.array([-sa * sp, -sa * cp, ca])
    toward_golfer = np.array([-cp, sp, 0.0])
    return tangent, math.sin(plane) * up + math.cos(plane) * toward_golfer


def heel_points(
    params: np.ndarray, tau_s: np.ndarray, ball_xyz: np.ndarray, ball_velocity: np.ndarray
) -> np.ndarray:
    """Heel positions (N, 3) at times ``tau_s`` (relative to impact).

    ``params`` = path, attack, plane (rad), radius (m), club speed (m/s), heel
    offset from the ball centre at impact (3, m), kick, post-impact anchor
    height offset (m), post-impact plane (rad), post-impact radius (m), and the
    mat floor for the heel (m below the ball centre, negative).
    """
    path, aoa, plane, radius, speed, ox, oy, oz, kick, dz, plane_p, radius_p, floor = params
    tau_s = np.asarray(tau_s, dtype=float)
    tangent, normal = _arc_basis(path, aoa, plane)
    start = ball_xyz + np.array([ox, oy, oz])
    out = np.empty((tau_s.size, 3))
    pre = tau_s <= 0.0
    if pre.any():
        phase = (speed / radius) * tau_s[pre, None]
        out[pre] = (
            start + radius * np.sin(phase) * tangent + radius * (1.0 - np.cos(phase)) * normal
        )
    if (~pre).any():
        v_post = speed * tangent - kick * ball_velocity
        speed_p = float(np.linalg.norm(v_post))
        t_p = v_post / speed_p
        _, n_p = _arc_basis(
            math.atan2(t_p[0], t_p[1]), math.asin(min(1.0, max(-1.0, t_p[2]))), plane_p
        )
        phase = (speed_p / radius_p) * tau_s[~pre, None]
        out[~pre] = start + radius_p * np.sin(phase) * t_p + radius_p * (1.0 - np.cos(phase)) * n_p
        out[~pre, 2] += dz
    if MAT_FLOOR:
        # The mat holds the head up: the heel cannot sink much below its surface.
        rel = (out[:, 2] - ball_xyz[2] - floor) / FLOOR_SOFTNESS_M
        out[:, 2] = ball_xyz[2] + floor + FLOOR_SOFTNESS_M * np.logaddexp(0.0, rel)
    return out


def ball_velocity_vector(speed_mps: float, hla_deg: float, vla_deg: float) -> np.ndarray:
    """Ball launch velocity (m/s) in the camera's world frame."""
    h, v = math.radians(hla_deg), math.radians(vla_deg)
    return speed_mps * np.array([math.sin(h) * math.cos(v), math.cos(h) * math.cos(v), math.sin(v)])


# --- head templates -------------------------------------------------------------------


def pre_impact_template(scale: float) -> np.ndarray:
    """Rear view of an iron head before impact, in shaft-aligned coordinates.

    Rows are image rows relative to the heel anchor (the sole's height on the
    shaft line); columns the horizontal offset from the shaft line (toe to the
    right), both at 1/``scale``. Fitted (NCC 0.94) to the average of 63 well-lit
    6-iron frames from the 2026-11-19 session: shaft ridge ending ~10 px above
    the sole, heel corner just left of the shaft axis, a faint sole band whose
    lowest point is ~18 px out, the toe edge curling up to a toe highlight, a
    slightly dark back of the head inside the U.
    """
    rows = np.arange(PRE_ROWS[0], PRE_ROWS[1] + 1, dtype=float)[:, None] / scale
    cols = np.arange(PRE_COLS[0], PRE_COLS[1] + 1, dtype=float)[None, :] / scale
    shaft_end = -9.5
    tpl = 19.7 * np.clip((shaft_end - rows) / 0.9, 0.0, 1.0) * np.exp(-0.5 * (cols / 2.3) ** 2)
    tpl = tpl + 4.7 * ((rows > shaft_end + 1.0) & (rows < -1.0)) * np.exp(
        -0.5 * ((cols + 3.75) / 2.35) ** 2
    )
    x = np.clip(cols, 0.0, 32.2)
    centre = np.where(x < 18.0, 0.5 - 0.003 * (x - 18.0) ** 2, 0.5 - 0.032 * (x - 18.0) ** 2)
    amp = np.where(cols < -4.9, 0.0, np.where(cols <= 28.0, 7.2, np.where(cols <= 32.2, 6.4, 0.0)))
    tpl = tpl + amp * np.exp(-0.5 * ((rows - centre) / 2.5) ** 2)
    inside = (cols > 3.0) & (cols < 27.0) & (rows < centre - 3.0) & (rows > -16.0)
    tpl = tpl - 0.8 * inside
    tpl = tpl + 13.5 * np.exp(-0.5 * (((rows + 19.7) / 3.0) ** 2 + ((cols - 35.6) / 2.7) ** 2))
    return np.clip(tpl, *PRE_CLIP)


def post_impact_template(scale: float) -> np.ndarray:
    """Rear view of an iron head after impact (lit sole lines), shaft-aligned.

    Rows relative to the anchor at the bottom edge of the lit heel; fitted
    (NCC 0.98) to the average of 391 post-impact frames from the 2026-11-19
    session: shaft, a bright heel corner, a wide lit band along the sole and a
    thinner line above it rising toward the toe.
    """
    rows = np.arange(POST_ROWS[0], POST_ROWS[1] + 1, dtype=float)[:, None] / scale
    cols = np.arange(POST_COLS[0], POST_COLS[1] + 1, dtype=float)[None, :] / scale

    def window(c, lo, hi):
        return 1.0 / (1.0 + np.exp(-(c - lo) / 1.5)) / (1.0 + np.exp((c - hi) / 1.5))

    tpl = 17.3 * np.exp(-0.5 * ((cols + 0.96) / 1.54) ** 2) * (rows < -12.0)
    tpl = tpl + 28.6 * np.exp(-0.5 * ((cols + 0.69) / 2.10) ** 2) * ((rows > -12.0) & (rows < 0.0))
    tpl = tpl + 38.6 * np.exp(-0.5 * ((rows - (-6.59 - 0.263 * cols)) / 1.33) ** 2) * window(
        cols, -1.9, 24.3
    )
    tpl = tpl + 60.0 * np.exp(-0.5 * ((rows - (-2.02 - 0.063 * cols)) / 2.18) ** 2) * window(
        cols, 0.37, 16.7
    )
    return np.clip(0.5 * tpl, *POST_CLIP)


_PRE_TEMPLATES = {s: pre_impact_template(s) for s in PRE_SCALES}
_POST_TEMPLATES = {s: post_impact_template(s) for s in POST_SCALES}


# --- detection --------------------------------------------------------------------------


def _ridge(diff: np.ndarray) -> np.ndarray:
    out = np.zeros_like(diff)
    h = RIDGE_HALF_PX
    out[:, h:-h] = diff[:, h:-h] - 0.5 * (diff[:, : -2 * h] + diff[:, 2 * h :])
    return np.clip(out, 0.0, RIDGE_CAP)


def _hough(
    ridge: np.ndarray, rows: np.ndarray, ref_row: float, angles: np.ndarray
) -> tuple[float, float, float]:
    """Best line u(row) = u_ref + (row - ref_row) * cot(angle) by summed ridge."""
    width = ridge.shape[1]
    cols = np.arange(0, width, HOUGH_COL_STEP)
    cot = 1.0 / np.tan(np.radians(angles))
    u = cols[None, :, None] + (rows[None, None, :] - ref_row) * cot[:, None, None]
    ui = np.rint(u).astype(np.int32)
    inside = (ui >= 0) & (ui < width)
    vals = ridge[rows[None, None, :], np.clip(ui, 0, width - 1)]
    scores = np.where(inside, vals, 0.0).sum(axis=2)
    best = int(np.argmax(scores))
    ia, iu = divmod(best, scores.shape[1])
    return float(scores[ia, iu]), float(cols[iu]), float(angles[ia])


def _refine_line(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    diff: np.ndarray, u_ref: float, slope: float, ref_row: float, rows: np.ndarray, half: int = 5
):
    """Per-row intensity centroid across the ridge, robust weighted line fit.

    Returns (u at ref_row, slope du/dv, rms px, rows used) or None.
    """
    width = diff.shape[1]
    offset = u_ref
    offsets = np.arange(-half, half + 1)
    for _ in range(3):
        centre = offset + slope * (rows - ref_row)
        cols = np.rint(centre).astype(np.int32)[:, None] + offsets[None, :]
        prof = diff[rows[:, None], np.clip(cols, 0, width - 1)]
        prof = np.where((cols >= 0) & (cols < width), prof, 0.0)
        base = np.minimum(prof[:, :2].mean(axis=1), prof[:, -2:].mean(axis=1))[:, None]
        weight = np.minimum(np.clip(prof - base, 0.0, None), 60.0)
        total = weight.sum(axis=1)
        centroid = (weight * cols).sum(axis=1) / np.maximum(total, 1e-6)
        good = total > 25.0
        if good.sum() < MIN_SHAFT_ROWS:
            return None
        x = rows[good] - ref_row
        y = centroid[good]
        w = np.sqrt(np.minimum(total[good], 200.0))
        design = np.stack([np.ones_like(x), x], axis=1)
        sol = np.linalg.lstsq(design * w[:, None], y * w, rcond=None)[0]
        resid = y - design @ sol
        keep = np.abs(resid) < 3.0 * (1.4826 * np.median(np.abs(resid)) + 0.2)
        if keep.sum() < MIN_SHAFT_ROWS:
            return None
        sol = np.linalg.lstsq(design[keep] * w[keep, None], y[keep] * w[keep], rcond=None)[0]
        offset, slope = float(sol[0]), float(sol[1])
    resid = y[keep] - (offset + slope * x[keep])
    return offset, slope, float(np.sqrt(np.mean(resid**2))), int(keep.sum())


def _strip(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    diff: np.ndarray,
    offset: float,
    slope: float,
    ref_row: float,
    row_lo: int,
    row_hi: int,
    cols: np.ndarray,
):
    """Bilinear samples of ``diff`` at rows row_lo..row_hi-1, columns offset from the line."""
    height, width = diff.shape
    rows = np.arange(row_lo, row_hi)
    u = offset + slope * (rows[:, None] - ref_row) + cols[None, :]
    u0 = np.floor(u).astype(np.int32)
    frac = u - u0
    valid = (u0 >= 0) & (u0 < width - 1) & (rows[:, None] >= 0) & (rows[:, None] < height)
    u0 = np.clip(u0, 0, width - 2)
    r = np.clip(rows, 0, height - 1)[:, None]
    vals = diff[r, u0] * (1.0 - frac) + diff[r, u0 + 1] * frac
    return np.where(valid, vals, 0.0), valid, rows, u


def _sliding_sum(values: np.ndarray, length: int) -> np.ndarray:
    csum = np.concatenate([[0.0], np.cumsum(values)])
    return csum[length:] - csum[:-length]


_SPECTRA: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}


def _template_spectra(tpl: np.ndarray, n_fft: int) -> tuple[np.ndarray, np.ndarray]:
    """Conjugate row-FFTs of a template and of its square (cached per FFT length)."""
    key = (id(tpl), n_fft)
    if key not in _SPECTRA:
        _SPECTRA[key] = (
            np.conj(sp_fft.rfft(tpl, n=n_fft, axis=0)),
            np.conj(sp_fft.rfft(tpl * tpl, n=n_fft, axis=0)),
        )
    return _SPECTRA[key]


def _masked_ncc(strip: np.ndarray, mask: np.ndarray, templates: dict) -> dict:
    """NCC of each template at every row offset of ``strip``, ignoring masked pixels.

    The strip and the templates share their columns, so the correlation runs
    along rows only: one FFT per column, summed across columns in frequency.
    Returns {scale: ncc array}; offsets with too few unmasked pixels get -1.
    """
    rows = strip.shape[0]
    xm = strip * mask
    n_fft = sp_fft.next_fast_len(rows)
    fx = sp_fft.rfft(xm, n=n_fft, axis=0)
    fm = sp_fft.rfft(mask, n=n_fft, axis=0)
    mask_rows, x_rows, xx_rows = mask.sum(axis=1), xm.sum(axis=1), (xm * strip).sum(axis=1)
    out = {}
    for scale, tpl in templates.items():
        th = tpl.shape[0]
        if rows < th:
            continue
        valid = rows - th + 1
        ft, ft2 = _template_spectra(tpl, n_fft)
        sxt = sp_fft.irfft((fx * ft).sum(axis=1), n=n_fft)[:valid]
        st = sp_fft.irfft((fm * ft).sum(axis=1), n=n_fft)[:valid]
        stt = sp_fft.irfft((fm * ft2).sum(axis=1), n=n_fft)[:valid]
        n = _sliding_sum(mask_rows, th)
        sx = _sliding_sum(x_rows, th)
        sxx = _sliding_sum(xx_rows, th)
        nn = np.maximum(n, 1.0)
        cov = sxt - sx * st / nn
        var_x = sxx - sx * sx / nn
        var_t = stt - st * st / nn
        ncc = cov / np.sqrt(np.maximum(var_x * var_t, 1e-9))
        out[scale] = np.where(n >= 0.7 * tpl.size, ncc, -1.0)
    return out


def _locate_head(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    diff, line, templates, rows_cols, clip, masked, search
):
    """Heel anchor row along the shaft line (sub-pixel), its NCC and scale."""
    offset, slope, ref_row = line
    (r0, r1), (c0, c1) = rows_cols
    cols = np.arange(c0, c1 + 1)
    lo = search[0] + r0
    strip, valid, rows, u = _strip(diff, offset, slope, ref_row, lo, search[1] + r1 + 1, cols)
    mask = (valid & ~masked(u, np.broadcast_to(rows[:, None], u.shape))).astype(float)
    strip = np.clip(strip, *clip)
    best = (-1.0, None, None)
    for scale, ncc in _masked_ncc(strip, mask, templates).items():
        j = int(np.argmax(ncc))
        if ncc[j] > best[0]:
            sub = 0.0
            if 0 < j < ncc.size - 1 and ncc[j - 1] > -1.0 and ncc[j + 1] > -1.0:
                den = ncc[j - 1] - 2.0 * ncc[j] + ncc[j + 1]
                if den < 0.0:
                    sub = float(np.clip(0.5 * (ncc[j - 1] - ncc[j + 1]) / den, -0.5, 0.5))
            best = (float(ncc[j]), lo - r0 + j + sub, scale)
    return best


def _measure_frames(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    frames, timestamps_ns, trigger_index, ball_uv, impact_time_s, cam, ball_velocity
):
    """Shaft line + heel candidate for every frame in the window around impact."""
    _, height, width = frames.shape
    u0, v0 = ball_uv
    c_lo = max(int(u0) - ROI_LEFT_PX, 0)
    c_hi = min(int(u0) + ROI_RIGHT_PX, width)
    r_hi = min(int(v0) + ROI_BELOW_PX, height)
    quiet = np.sort(frames[:BACKGROUND_FRAMES, :r_hi, c_lo:c_hi], axis=0)
    mid = BACKGROUND_FRAMES // 2
    background = 0.5 * (quiet[mid - 1].astype(np.float32) + quiet[mid])
    t_rel = (np.asarray(timestamps_ns, dtype=np.int64) - int(timestamps_ns[trigger_index])) / 1e9
    tau = t_rel - impact_time_s
    bu, bv = u0 - c_lo, v0
    ref_row = float(int(v0) - 40)
    shaft_rows = np.arange(4, int(v0) - 4)
    hough_rows = shaft_rows[::HOUGH_ROW_STEP]
    out = []
    found_angles: list[float] = []
    window = np.where((tau > -WINDOW_BEFORE_IMPACT_S) & (tau < WINDOW_AFTER_IMPACT_S))[0]
    for k in window[window >= BACKGROUND_FRAMES]:
        post = bool(tau[k] > 0.0)
        diff = frames[k, :r_hi, c_lo:c_hi].astype(np.float32) - background
        angles = SHAFT_ANGLES_DEG
        if found_angles:
            angles = angles[np.abs(angles - np.median(found_angles)) <= HOUGH_TRACK_DEG]
        score, u_ref, angle = _hough(_ridge(diff), hough_rows, ref_row, angles)
        if score < MIN_SHAFT_SCORE:
            continue
        line = _refine_line(diff, u_ref, 1.0 / math.tan(math.radians(angle)), ref_row, shaft_rows)
        if line is None:
            continue
        offset, slope, rms, _ = line
        if post:
            templates, rows_cols, clip, min_ncc = (
                _POST_TEMPLATES,
                (POST_ROWS, POST_COLS),
                POST_CLIP,
                MIN_POST_NCC,
            )
            search = (int(v0) - 70, int(v0) + 12)
            flying = None
            if ball_velocity is not None:
                flying = cam.project(cam.ball + ball_velocity * tau[k]) - np.array([c_lo, 0.0])
        else:
            templates, rows_cols, clip, min_ncc = (
                _PRE_TEMPLATES,
                (PRE_ROWS, PRE_COLS),
                PRE_CLIP,
                MIN_PRE_NCC,
            )
            search = (30, int(v0) + 12)
            flying = None

        def masked(uu, vv, flying=flying):
            m = ((uu - bu) ** 2 + (vv - bv) ** 2 < BALL_MASK_RADIUS_PX**2) | (
                vv > v0 + MASK_BELOW_PX
            )
            if flying is not None:
                m |= (uu - flying[0]) ** 2 + (vv - flying[1]) ** 2 < FLYING_BALL_MASK_RADIUS_PX**2
            return m

        ncc, row, scale = _locate_head(
            diff, (offset, slope, ref_row), templates, rows_cols, clip, masked, search
        )
        if row is not None and ncc >= min_ncc:
            # Re-fit the line on the shaft just above the head: the heel sits at
            # its bottom, and the upper shaft bends and blurs.
            lower = shaft_rows[
                (shaft_rows > row - LOWER_SHAFT_ROWS) & (shaft_rows < row - (12 if post else 6))
            ]
            if lower.size >= MIN_SHAFT_ROWS:
                local = _refine_line(diff, offset, slope, ref_row, lower)
                if local is not None and local[2] <= MAX_SHAFT_RMS_PX:
                    offset, slope, rms, _ = local
                    ncc, row, scale = _locate_head(
                        diff, (offset, slope, ref_row), templates, rows_cols, clip, masked, search
                    )
        if rms > MAX_SHAFT_RMS_PX:
            continue
        if abs(tau[k]) < 0.012:
            found_angles.append(math.degrees(math.atan2(1.0, slope)))
        head_ok = row is not None and ncc >= min_ncc
        out.append(
            ClubFramePoint(
                tau_s=float(tau[k]),
                post=post,
                line_u_at_ref=offset + c_lo,
                line_slope=slope,
                ref_row=ref_row,
                line_rms_px=rms,
                head_u=(offset + slope * (row - ref_row) + c_lo) if head_ok else None,
                head_v=float(row) if head_ok else None,
                head_ncc=float(ncc),
                head_scale=scale if head_ok else None,
            )
        )
    return out


# --- fit ----------------------------------------------------------------------------------


def _prior_vectors(club_speed: float, priors: ClubTrackPriors) -> tuple[np.ndarray, np.ndarray]:
    rad = math.radians
    mean = np.array(
        [
            rad(priors.path_deg[0]),
            rad(priors.attack_deg[0]),
            rad(priors.plane_deg[0]),
            priors.radius_m[0],
            club_speed,
            *priors.heel_offset_m,
            priors.kick[0],
            priors.post_anchor_dz_m[0],
            rad(priors.post_plane_deg[0]),
            priors.post_radius_m[0],
            priors.floor_m[0],
        ]
    )
    sd = np.array(
        [
            rad(priors.path_deg[1]),
            rad(priors.attack_deg[1]),
            rad(priors.plane_deg[1]),
            priors.radius_m[1],
            priors.speed_sd_frac * club_speed,
            *priors.heel_offset_sd_m,
            priors.kick[1],
            priors.post_anchor_dz_m[1],
            rad(priors.post_plane_deg[1]),
            priors.post_radius_m[1],
            priors.floor_m[1],
        ]
    )
    return mean, sd


def _select_frames(points: list[ClubFramePoint]) -> list[ClubFramePoint]:
    """Drop lines that are not the shaft (a leg, an arm): angle far from the near-impact shaft."""
    if not points:
        return points
    angles = np.array([math.degrees(math.atan2(1.0, p.line_slope)) for p in points])
    near = np.array([abs(p.tau_s) < 0.012 for p in points])
    ref = float(np.median(angles[near] if near.any() else angles))
    return [p for p, a in zip(points, angles) if abs(a - ref) <= LINE_ANGLE_TOL_DEG]


def _soft_l1(r: np.ndarray) -> np.ndarray:
    """Residuals whose squares are the soft-L1 cost: quadratic near 0, linear far out."""
    c = ROBUST_SCALE
    return np.sign(r) * c * np.sqrt(2.0 * (np.sqrt(1.0 + (r / c) ** 2) - 1.0))


def _fit(
    points, cam: _Camera, club_speed: float, ball_velocity: np.ndarray, priors: ClubTrackPriors
):
    """Robust two-pass fit: all head candidates, then those within HEAD_GATE_PX of that arc."""
    mean, sd = _prior_vectors(club_speed, priors)
    tau = np.array([p.tau_s for p in points])
    offset = np.array([p.line_u_at_ref for p in points])
    slope = np.array([p.line_slope for p in points])
    ref = np.array([p.ref_row for p in points])
    post = np.array([p.post for p in points])
    head_v = np.array([p.head_v if p.head_v is not None else np.nan for p in points])
    candidates = np.isfinite(head_v) & np.where(
        post, tau >= POST_HEAD_GUARD_S, tau <= -PRE_HEAD_GUARD_S
    )
    sigma_along = np.where(post, SIGMA_ALONG_POST_PX, SIGMA_ALONG_PRE_PX)
    perp_scale = 1.0 / np.sqrt(1.0 + slope**2) / SIGMA_PERP_PX

    def solve(use, starts):
        def residuals(q):
            uv = cam.project(heel_points(q, tau, cam.ball, ball_velocity))
            perp = (uv[:, 0] - offset - slope * (uv[:, 1] - ref)) * perp_scale
            along = (uv[use, 1] - head_v[use]) / sigma_along[use]
            # Robust (soft-L1) on the measurements only; the priors stay Gaussian.
            return np.concatenate([_soft_l1(np.concatenate([perp, along])), (q - mean) / sd])

        best = None
        for x0 in starts:
            try:
                sol = least_squares(residuals, x0, x_scale=sd, method="trf", max_nfev=150)
            except (ValueError, np.linalg.LinAlgError):
                continue
            if best is None or sol.cost < best.cost:
                best = sol
        return best

    if candidates.sum() < MIN_HEADS:
        return None
    # Pass 1 trusts the post-impact heads (the lit sole is rarely mistaken)
    # when there are enough of them; pre-impact candidates far from that arc
    # (e.g. the end of a shaft glint taken for the head) are dropped next.
    seed = candidates & post
    if seed.sum() < MIN_HEADS:
        seed = candidates
    best = solve(seed, [mean])
    for gate in (SEED_GATE_PX, HEAD_GATE_PX):
        if best is None:
            return None
        predicted = cam.project(heel_points(best.x, tau, cam.ball, ball_velocity))[:, 1]
        agree = candidates & (np.abs(head_v - predicted) <= gate)
        if agree.sum() < MIN_HEADS:
            return None
        best = solve(agree, [best.x])
    if best is None:
        return None
    try:
        cov = np.linalg.inv(best.jac.T @ best.jac)
        se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    except np.linalg.LinAlgError:
        se = np.full(best.x.size, np.nan)
    n_data = len(points) + int(agree.sum())
    rms = float(np.sqrt(np.mean(best.fun[:n_data] ** 2)))
    return best.x, se, rms, agree


def _nuisance_z(params: np.ndarray, club_speed: float, priors: ClubTrackPriors) -> float:
    """Largest |z| of the fitted swing-shape parameters against their priors (path/AoA excluded)."""
    mean, sd = _prior_vectors(club_speed, priors)
    return float(np.max(np.abs((params[2:] - mean[2:]) / sd[2:])))


def _confidence(n_pre: int, n_post: int, path_se: float, aoa_se: float, rms: float) -> str:
    """Uncapped tier from head counts on both sides, standard errors and the fit residual."""
    heads_both_sides = (n_pre >= 2, n_post >= 3)
    if all(heads_both_sides) and max(path_se / 0.8, aoa_se / 1.2, rms / 1.5) <= 1.0:
        return "high"
    heads_some = (n_pre >= 1, n_post >= 2)
    if all(heads_some) and max(path_se / 1.2, aoa_se / 2.0, rms / 2.0) <= 1.0:
        return "medium"
    return "low"


def _cap(tier: str) -> str:
    return _TIERS[min(_TIERS.index(tier), _TIERS.index(CONFIDENCE_CAP))]


def estimate_club_delivery(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements,too-many-branches
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    trigger_index: int,
    ball_uv: tuple[float, float] | None,
    ball_diameter_px: float | None,
    impact_time_s: float | None,
    ball_speed_mps: float | None,
    geometry: ClubTrackGeometry | None = None,
    priors: ClubTrackPriors | None = None,
    ball_hla_deg: float | None = None,
    ball_vla_deg: float | None = None,
) -> ClubDelivery:
    """Club path and attack angle at impact from one rear-camera clip.

    Args:
        frames: (N, H, W) uint8 frames, upright (as saved by the recorder).
        timestamps_ns: per-frame sensor timestamps.
        trigger_index: index of the trigger frame (``pre_trigger_count - 1``).
        ball_uv: resting ball centre (pixels), or None when it was not found.
        ball_diameter_px: resting ball diameter (pixels); only sanity-checked.
        impact_time_s: impact time relative to the trigger frame's timestamp,
            from the ball-flight fit.
        ball_speed_mps: ball speed (radar); club speed = ball speed / smash.
        geometry: camera geometry; defaults to the Nov-2026 garage rig.
        priors: swing priors; defaults to broad iron values.
        ball_hla_deg, ball_vla_deg: ball launch from the ball-flight fit (same
            geometry), the direction of the impact kick. Without them a
            straight 14 deg launch is assumed (``launch_assumed``).

    Returns:
        ``ClubDelivery``; ``path_deg`` / ``attack_angle_deg`` are set only
        when ``status == "ok"``.
    """
    started = time.perf_counter()
    geometry = geometry or ClubTrackGeometry()
    priors = priors or ClubTrackPriors()

    def result(status, **kw):
        base = {
            "path_deg": None,
            "attack_angle_deg": None,
            "status": status,
            "confidence": None,
            "n_points": 0,
            "n_lines": 0,
            "rms_px": None,
        }
        base.update(kw)
        base["elapsed_ms"] = (time.perf_counter() - started) * 1000.0
        return ClubDelivery(**base)

    frames = np.asarray(frames)
    if frames.ndim != 3 or frames.shape[0] < BACKGROUND_FRAMES + MIN_FRAMES_BEFORE_WINDOW:
        return result("clip_too_short")
    if len(timestamps_ns) != frames.shape[0] or not 0 <= trigger_index < frames.shape[0]:
        return result("clip_too_short")
    height, width = frames.shape[1:]
    if ball_uv is None or not all(np.isfinite(ball_uv)):
        return result("ball_not_found")
    u0, v0 = float(ball_uv[0]), float(ball_uv[1])
    if not (20.0 <= u0 < width - 20.0 and 60.0 <= v0 < height - 4.0):
        return result("ball_not_found")
    if ball_diameter_px is not None and not 2.0 <= ball_diameter_px <= 60.0:
        return result("ball_not_found")
    if impact_time_s is None or not np.isfinite(impact_time_s):
        return result("no_impact_time")
    if (
        ball_speed_mps is None
        or not np.isfinite(ball_speed_mps)
        or ball_speed_mps < MIN_BALL_SPEED_MPS
    ):
        return result("no_ball_speed")
    t_rel = (np.asarray(timestamps_ns, dtype=np.int64) - int(timestamps_ns[trigger_index])) / 1e9
    before = np.where((t_rel - impact_time_s > -WINDOW_BEFORE_IMPACT_S) & (t_rel < impact_time_s))[
        0
    ]
    after = np.where((t_rel > impact_time_s) & (t_rel - impact_time_s < WINDOW_AFTER_IMPACT_S))[0]
    if before.size < 2 or after.size < 2 or before.min() < BACKGROUND_FRAMES:
        return result("clip_too_short")

    launch_assumed = ball_hla_deg is None or ball_vla_deg is None
    hla, vla = (
        priors.assumed_launch_deg if launch_assumed else (float(ball_hla_deg), float(ball_vla_deg))
    )
    ball_velocity = ball_velocity_vector(float(ball_speed_mps), hla, vla)
    cam = _Camera(geometry, (u0, v0), (height, width))
    points = _select_frames(
        _measure_frames(
            frames, timestamps_ns, trigger_index, (u0, v0), impact_time_s, cam, ball_velocity
        )
    )
    n_lines = len(points)
    n_heads = sum(p.head_v is not None for p in points)
    common = {
        "n_lines": n_lines,
        "n_points": n_heads,
        "frames": tuple(points),
        "launch_assumed": launch_assumed,
    }
    if n_lines == 0:
        return result("no_club", **common)
    if n_lines < MIN_LINES or n_heads < MIN_HEADS:
        return result("too_few_frames", **common)

    club_speed = float(ball_speed_mps) / priors.smash
    fitted = _fit(points, cam, club_speed, ball_velocity, priors)
    if fitted is None:
        return result("fit_failed", **common)
    params, se, rms, used = fitted
    points = [replace(p, used_head=bool(flag)) for p, flag in zip(points, used)]
    n_pre = int(sum(1 for i, p in enumerate(points) if used[i] and not p.post))
    n_post = int(sum(1 for i, p in enumerate(points) if used[i] and p.post))
    path, aoa = math.degrees(params[0]), math.degrees(params[1])
    path_se, aoa_se = math.degrees(se[0]), math.degrees(se[1])
    common.update(
        n_points=n_pre + n_post,
        n_pre=n_pre,
        n_post=n_post,
        rms_px=rms,
        path_se_deg=path_se,
        attack_se_deg=aoa_se,
        plane_deg=math.degrees(params[2]),
        radius_m=float(params[3]),
        club_speed_mps=float(params[4]),
        kick=float(params[8]),
        frames=tuple(points),
        fit_params=tuple(float(x) for x in params),
    )
    if abs(path) > MAX_ABS_PATH_DEG or abs(aoa) > MAX_ABS_ATTACK_DEG or not np.isfinite(path_se):
        return result("implausible", **common)
    if _nuisance_z(params, club_speed, priors) > MAX_NUISANCE_Z:
        # The swing shape needed to fit the frames (plane, radius, where the
        # heel met the ball, the kick) is far outside what a swing does.
        return result("implausible", **common)
    raw = _confidence(n_pre, n_post, path_se, aoa_se, rms)
    if launch_assumed:
        raw = "low"
    return result(
        "ok",
        path_deg=path,
        attack_angle_deg=aoa,
        confidence=_cap(raw),
        raw_confidence=raw,
        **common,
    )
