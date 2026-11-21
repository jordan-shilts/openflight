"""Synthetic iron swings rendered into real garage background frames (club-track tests).

The background is four real club-free frames from the 2026-11-19 garage
session (``fixtures/camera_club_background.npz``: a 262 x 320 crop around the
resting ball, placed back into a full 400 x 640 frame and cycled through the
clip, so the sensor noise is real).

Truth model: the heel point (shaft axis at sole height) moves on a circular arc
up to impact, is kicked by ``-kick * v_ball`` at impact and then follows a
second arc; the mat holds it up (soft floor). The shaft runs from the heel
toward the hands in the swing plane. The head is drawn with the estimator's
own appearance templates (pre-impact U, post-impact lit sole) in
shaft-aligned coordinates, scaled with depth, so these tests exercise the
geometry, detection plumbing and fit end to end, not the template's realism.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage

from openflight.camera import club_track as ct

FIXTURE = Path(__file__).parent / "fixtures" / "camera_club_background.npz"
FRAME_DT_S = 1.0 / 230.4
TRIGGER_INDEX = 35
N_FRAMES = 60
PRE_REF_DEPTH_M = 1.30  # depth at which the pre-impact template is at scale 1
POST_REF_DEPTH_M = 1.90


@dataclass
class SwingTruth:  # pylint: disable=too-many-instance-attributes
    """Known club delivery and the swing details around it."""

    path_deg: float
    aoa_deg: float
    plane_deg: float = 62.0
    radius_m: float = 1.15
    club_speed_mps: float = 36.0
    heel_offset_m: tuple[float, float, float] = (-0.035, -0.035, -0.017)
    kick: float = 0.18
    post_plane_deg: float = 68.0
    post_radius_m: float = 1.3
    floor_m: float = -0.025
    lean_deg: float = 4.0
    ball_speed_mps: float = 49.7
    hla_deg: float = 1.0
    vla_deg: float = 14.0


def load_background() -> tuple[np.ndarray, tuple[float, float]]:
    """Real club-free frames as full-size (4, 400, 640) uint8, and the resting ball (u, v)."""
    with np.load(FIXTURE) as data:
        crop = data["frames"]
        row0, col0 = (int(x) for x in data["origin"])
        height, width = (int(x) for x in data["full_shape"])
        ball_uv = (float(data["ball_uv"][0]), float(data["ball_uv"][1]))
    fill = np.median(crop).astype(np.uint8)
    frames = np.full((crop.shape[0], height, width), fill, dtype=np.uint8)
    frames[:, row0 : row0 + crop.shape[1], col0 : col0 + crop.shape[2]] = crop
    return frames, ball_uv


def _strip_shaft(tpl: np.ndarray, rows0: int, cols0: int, below_row: float) -> np.ndarray:
    """Template without its shaft part (the renderer draws the shaft itself)."""
    out = tpl.copy()
    rows = np.arange(tpl.shape[0])[:, None] + rows0
    cols = np.arange(tpl.shape[1])[None, :] + cols0
    out[(rows < below_row) & (np.abs(cols) <= 4)] = 0.0
    return out


PRE_TPL = _strip_shaft(ct.pre_impact_template(1.0), ct.PRE_ROWS[0], ct.PRE_COLS[0], -9.0)
POST_TPL = _strip_shaft(ct.post_impact_template(1.0), ct.POST_ROWS[0], ct.POST_COLS[0], -12.0)


def heel_and_shaft(truth: SwingTruth, ball: np.ndarray, tau: np.ndarray):
    """Heel positions (N, 3) and unit shaft directions toward the hands (N, 3)."""
    path, aoa = math.radians(truth.path_deg), math.radians(truth.aoa_deg)
    tangent, normal = ct._arc_basis(path, aoa, math.radians(truth.plane_deg))  # pylint: disable=protected-access
    vb = ct.ball_velocity_vector(truth.ball_speed_mps, truth.hla_deg, truth.vla_deg)
    v_post = truth.club_speed_mps * tangent - truth.kick * vb
    speed_p = float(np.linalg.norm(v_post))
    t_p = v_post / speed_p
    _, n_p = ct._arc_basis(  # pylint: disable=protected-access
        math.atan2(t_p[0], t_p[1]), math.asin(t_p[2]), math.radians(truth.post_plane_deg)
    )
    start = ball + np.array(truth.heel_offset_m)
    lean = math.radians(truth.lean_deg)
    heels, shafts = [], []
    for t in np.asarray(tau, dtype=float):
        if t <= 0.0:
            ph = truth.club_speed_mps * t / truth.radius_m
            p = start + truth.radius_m * (math.sin(ph) * tangent + (1.0 - math.cos(ph)) * normal)
        else:
            ph = speed_p * t / truth.post_radius_m
            p = start + truth.post_radius_m * (math.sin(ph) * t_p + (1.0 - math.cos(ph)) * n_p)
        ph0 = truth.club_speed_mps * t / truth.radius_m
        t_now = math.cos(ph0) * tangent + math.sin(ph0) * normal
        n_now = math.cos(ph0) * normal - math.sin(ph0) * tangent
        d = math.cos(lean) * n_now + math.sin(lean) * t_now
        heels.append(p)
        shafts.append(d / np.linalg.norm(d))
    heels = np.array(heels)
    rel = (heels[:, 2] - ball[2] - truth.floor_m) / 0.002
    heels[:, 2] = ball[2] + truth.floor_m + 0.002 * np.logaddexp(0.0, rel)
    return heels, np.array(shafts)


def _paste(img, tpl, rows0, cols0, anchor_uv, slope, scale, amp):
    """Add ``amp * tpl`` (shaft-aligned coordinates, anchor at row 0 / column 0)."""
    height, width = img.shape
    ua, va = anchor_uv
    v_lo = max(int(math.floor(va + rows0 * scale)) - 1, 0)
    v_hi = min(int(math.ceil(va + (rows0 + tpl.shape[0]) * scale)) + 1, height - 1)
    if v_hi <= v_lo:
        return
    u_lo = max(int(ua - 80 * scale), 0)
    u_hi = min(int(ua + 80 * scale), width - 1)
    vv, uu = np.mgrid[v_lo : v_hi + 1, u_lo : u_hi + 1]
    r = (vv - va) / scale - rows0
    c = (uu - (ua + slope * (vv - va))) / scale - cols0
    inside = (r >= 0) & (r <= tpl.shape[0] - 1) & (c >= 0) & (c <= tpl.shape[1] - 1)
    vals = ndimage.map_coordinates(tpl, [r[inside], c[inside]], order=1)
    img[vv[inside], uu[inside]] += amp * vals


def _shaft(img, bottom_uv, top_uv, sigma, amp):
    height, width = img.shape
    p, q = np.asarray(bottom_uv, float), np.asarray(top_uv, float)
    length = float(np.linalg.norm(q - p))
    e = (q - p) / length
    n = np.array([-e[1], e[0]])
    v_lo, v_hi = int(max(min(p[1], q[1]) - 4, 0)), int(min(max(p[1], q[1]) + 4, height - 1))
    u_lo, u_hi = int(max(min(p[0], q[0]) - 4, 0)), int(min(max(p[0], q[0]) + 4, width - 1))
    if v_hi <= v_lo or u_hi <= u_lo:
        return
    vv, uu = np.mgrid[v_lo : v_hi + 1, u_lo : u_hi + 1]
    t = (uu - p[0]) * e[0] + (vv - p[1]) * e[1]
    s = (uu - p[0]) * n[0] + (vv - p[1]) * n[1]
    m = (t >= 0) & (t <= length) & (np.abs(s) < 4 * sigma)
    glint = 60.0 * np.exp(-0.5 * ((t[m] - 60.0) / 8.0) ** 2)
    img[vv[m], uu[m]] += (amp + glint) * np.exp(-0.5 * (s[m] / sigma) ** 2)


def render_swing(  # pylint: disable=too-many-locals
    truth: SwingTruth,
    impact_after_trigger_s: float = 0.002,
    club: bool = True,
    occluder=None,
    seed: int = 0,
):
    """Synthetic clip: (frames (60, 400, 640) uint8, timestamps_ns, trigger index, ball_uv).

    ``occluder(img, tau)`` may paint over a frame (float image) after the club is drawn.
    """
    background, ball_uv = load_background()
    rng = np.random.default_rng(seed)
    cam = ct._Camera(ct.ClubTrackGeometry(), ball_uv, background.shape[1:])  # pylint: disable=protected-access
    ts_s = (np.arange(N_FRAMES) - TRIGGER_INDEX) * FRAME_DT_S
    timestamps_ns = np.round((ts_s + 100.0) * 1e9).astype(np.int64)
    tau = ts_s - impact_after_trigger_s
    vv, uu = np.mgrid[0 : background.shape[1], 0 : background.shape[2]]
    u0, v0 = ball_uv
    resting = (uu - u0) ** 2 + (vv - v0) ** 2 < 10.0**2
    vb = ct.ball_velocity_vector(truth.ball_speed_mps, truth.hla_deg, truth.vla_deg)
    frames = np.empty((N_FRAMES,) + background.shape[1:], dtype=np.uint8)
    for k in range(N_FRAMES):
        img = background[k % len(background)].astype(np.float32)
        img += rng.normal(0.0, 1.0, img.shape).astype(np.float32)  # fresh noise per frame
        t = tau[k]
        if t > 0.0:
            img[resting] = np.roll(img, -24, axis=1)[resting]  # ball gone from the tee
            buv = cam.project(cam.ball + vb * t)
            img[(uu - buv[0]) ** 2 + (vv - buv[1]) ** 2 < 6.5**2] = 170.0
        if club and abs(t) < 0.03:
            heel, shaft = heel_and_shaft(truth, cam.ball, np.array([t]))
            heel, shaft = heel[0], shaft[0]
            uvh = cam.project(heel)
            uvn = cam.project(heel + 0.05 * shaft)
            slope = (uvn[0] - uvh[0]) / (uvn[1] - uvh[1])
            depth = heel[1]
            layer = np.zeros_like(img)
            if t > 0.0:
                _paste(
                    layer,
                    POST_TPL,
                    ct.POST_ROWS[0],
                    ct.POST_COLS[0],
                    uvh,
                    slope,
                    POST_REF_DEPTH_M / depth,
                    1.0,
                )
            else:
                _paste(
                    layer,
                    PRE_TPL,
                    ct.PRE_ROWS[0],
                    ct.PRE_COLS[0],
                    uvh,
                    slope,
                    PRE_REF_DEPTH_M / depth,
                    1.0,
                )
            _shaft(
                layer, cam.project(heel + 0.03 * shaft), cam.project(heel + 0.9 * shaft), 1.1, 28.0
            )
            img += layer
        if occluder is not None:
            occluder(img, t)
        frames[k] = np.clip(np.rint(img), 0, 255).astype(np.uint8)
    return frames, timestamps_ns, TRIGGER_INDEX, ball_uv
