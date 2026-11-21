"""Tests for the experimental rear-camera club path / attack angle estimator."""

import math

import numpy as np
import pytest

from openflight.camera import club_track as ct
from tests.club_track_synth import SwingTruth, load_background, render_swing

IMPACT_S = 0.0015  # impact time after the trigger frame


def _estimate(frames, ts, trigger, ball_uv, truth, **overrides):
    kwargs = {
        "ball_diameter_px": 8.0,
        "impact_time_s": IMPACT_S,
        "ball_speed_mps": truth.ball_speed_mps,
        "ball_hla_deg": truth.hla_deg,
        "ball_vla_deg": truth.vla_deg,
    }
    kwargs.update(overrides)
    return ct.estimate_club_delivery(frames, ts, trigger, ball_uv, **kwargs)


@pytest.fixture(scope="module")
def square_swing():
    truth = SwingTruth(path_deg=1.5, aoa_deg=-4.0, hla_deg=2.0)
    return truth, render_swing(truth, impact_after_trigger_s=IMPACT_S)


# --- known deliveries rendered into real garage frames --------------------------------


@pytest.mark.parametrize(
    ("path_deg", "aoa_deg", "plane_deg", "radius_m"),
    [
        (-6.0, -5.0, 60.0, 1.1),
        (0.0, -3.0, 62.0, 1.2),
        (6.0, -1.0, 65.0, 1.3),
        (3.0, 1.5, 58.0, 1.0),
        (-3.0, -7.0, 66.0, 1.4),
    ],
)
def test_recovers_known_path_and_attack_angle(path_deg, aoa_deg, plane_deg, radius_m):
    truth = SwingTruth(
        path_deg=path_deg,
        aoa_deg=aoa_deg,
        plane_deg=plane_deg,
        radius_m=radius_m,
        hla_deg=0.75 * path_deg + 0.5,
    )
    frames, ts, trigger, ball_uv = render_swing(truth, impact_after_trigger_s=IMPACT_S)

    d = _estimate(frames, ts, trigger, ball_uv, truth)

    assert d.status == "ok"
    assert d.path_deg == pytest.approx(path_deg, abs=1.5)
    assert d.attack_angle_deg == pytest.approx(aoa_deg, abs=2.0)
    assert d.n_pre >= 1 and d.n_post >= 2
    assert d.n_lines >= ct.MIN_LINES


def test_sign_convention_in_to_out_is_positive_and_upward_is_positive():
    results = {}
    for path_deg, aoa_deg in ((-5.0, -6.0), (5.0, 1.0)):
        truth = SwingTruth(path_deg=path_deg, aoa_deg=aoa_deg, hla_deg=0.75 * path_deg)
        frames, ts, trigger, ball_uv = render_swing(truth, impact_after_trigger_s=IMPACT_S)
        results[path_deg] = _estimate(frames, ts, trigger, ball_uv, truth)
    assert results[5.0].path_deg > 3.0 > -3.0 > results[-5.0].path_deg
    assert results[5.0].attack_angle_deg > -1.0 > -4.0 > results[-5.0].attack_angle_deg


def test_result_carries_fit_details_and_frames(square_swing):
    truth, (frames, ts, trigger, ball_uv) = square_swing

    d = _estimate(frames, ts, trigger, ball_uv, truth)

    assert d.status == "ok"
    assert d.path_se_deg is not None and 0.0 < d.path_se_deg < 3.0
    assert d.attack_se_deg is not None and 0.0 < d.attack_se_deg < 4.0
    assert d.club_speed_mps == pytest.approx(truth.ball_speed_mps / 1.38, rel=0.15)
    assert 40.0 < d.plane_deg < 85.0
    assert d.fit_params is not None and len(d.fit_params) == 13
    assert any(p.post for p in d.frames) and any(not p.post for p in d.frames)
    assert sum(p.used_head for p in d.frames) == d.n_points == d.n_pre + d.n_post
    assert d.elapsed_ms > 0.0
    assert not d.launch_assumed


def test_confidence_never_exceeds_the_cap(square_swing):
    truth, (frames, ts, trigger, ball_uv) = square_swing

    d = _estimate(frames, ts, trigger, ball_uv, truth)

    assert ct.CONFIDENCE_CAP == "low"  # not validated against a reference: kiosk must not use it
    assert d.raw_confidence in ("high", "medium", "low")
    assert d.confidence == "low"


def test_cap_helper_limits_tiers(monkeypatch):
    assert ct._cap("high") == "low"  # pylint: disable=protected-access
    monkeypatch.setattr(ct, "CONFIDENCE_CAP", "medium")
    assert ct._cap("high") == "medium"  # pylint: disable=protected-access
    assert ct._cap("low") == "low"  # pylint: disable=protected-access


def test_missing_launch_angles_assume_a_launch_and_stay_low(square_swing):
    truth, (frames, ts, trigger, ball_uv) = square_swing

    d = _estimate(frames, ts, trigger, ball_uv, truth, ball_hla_deg=None, ball_vla_deg=None)

    assert d.status == "ok"
    assert d.launch_assumed
    assert d.raw_confidence == "low" and d.confidence == "low"
    assert d.path_deg == pytest.approx(truth.path_deg, abs=2.5)


# --- edge cases -------------------------------------------------------------------------


def test_no_club_in_view():
    truth = SwingTruth(path_deg=0.0, aoa_deg=-4.0)
    frames, ts, trigger, ball_uv = render_swing(truth, impact_after_trigger_s=IMPACT_S, club=False)

    d = _estimate(frames, ts, trigger, ball_uv, truth)

    assert d.status == "no_club"
    assert d.path_deg is None and d.attack_angle_deg is None and d.confidence is None


def test_golfer_occluding_the_club_gives_no_reading():
    def trouser_leg(img, tau):
        if tau > -0.03:
            img[0:260, 230:410] = 45.0 + 8.0 * np.sin(np.arange(180) / 3.0)[None, :]

    truth = SwingTruth(path_deg=2.0, aoa_deg=-4.0)
    frames, ts, trigger, ball_uv = render_swing(
        truth, impact_after_trigger_s=IMPACT_S, occluder=trouser_leg
    )

    d = _estimate(frames, ts, trigger, ball_uv, truth)

    assert d.status in ("no_club", "too_few_frames", "fit_failed")
    assert d.path_deg is None and d.confidence is None


def test_no_ball(square_swing):
    truth, (frames, ts, trigger, _) = square_swing
    for ball_uv in (None, (float("nan"), 230.0), (3.0, 230.0), (390.0, 20.0)):
        d = _estimate(frames, ts, trigger, ball_uv, truth)
        assert d.status == "ball_not_found"
        assert d.path_deg is None
    d = _estimate(frames, ts, trigger, (390.0, 230.0), truth, ball_diameter_px=200.0)
    assert d.status == "ball_not_found"


def test_short_clip(square_swing):
    truth, (frames, ts, trigger, ball_uv) = square_swing

    assert _estimate(frames[:10], ts[:10], 5, ball_uv, truth).status == "clip_too_short"
    assert _estimate(frames, ts[:-1], trigger, ball_uv, truth).status == "clip_too_short"
    assert _estimate(frames, ts, 99, ball_uv, truth).status == "clip_too_short"
    # clip ends right after impact: nothing to see on the far side
    assert _estimate(frames[:37], ts[:37], trigger, ball_uv, truth).status == "clip_too_short"
    # impact so early that the pre-impact window overlaps the background frames
    early = _estimate(frames, ts, trigger, ball_uv, truth, impact_time_s=-0.12)
    assert early.status == "clip_too_short"


def test_missing_impact_time_or_ball_speed(square_swing):
    truth, (frames, ts, trigger, ball_uv) = square_swing

    assert (
        _estimate(frames, ts, trigger, ball_uv, truth, impact_time_s=None).status
        == "no_impact_time"
    )
    assert (
        _estimate(frames, ts, trigger, ball_uv, truth, ball_speed_mps=None).status
        == "no_ball_speed"
    )
    assert (
        _estimate(frames, ts, trigger, ball_uv, truth, ball_speed_mps=3.0).status == "no_ball_speed"
    )


def test_not_a_clip():
    d = ct.estimate_club_delivery(
        np.zeros((40, 64), np.uint8), np.arange(40), 5, (30, 30), 8, 0.0, 40.0
    )
    assert d.status == "clip_too_short"


# --- building blocks ----------------------------------------------------------------------


@pytest.mark.parametrize("lens", ["ftheta", "pinhole"])
def test_camera_puts_the_ball_where_it_was_seen(lens):
    geometry = ct.ClubTrackGeometry(lens=lens)
    cam = ct._Camera(geometry, (430.0, 236.0), (400, 640))  # pylint: disable=protected-access
    assert cam.project(cam.ball) == pytest.approx([430.0, 236.0], abs=1e-6)
    assert cam.ball[1] == pytest.approx(geometry.ball_distance_m)
    assert cam.ball[2] == pytest.approx(-geometry.camera_height_m)
    assert cam.ball[0] > 0.0  # right of the axis


def test_heel_points_tangent_and_kick():
    path, aoa = math.radians(4.0), math.radians(-5.0)
    vb = ct.ball_velocity_vector(50.0, 2.0, 15.0)
    params = np.array([path, aoa, math.radians(62.0), 1.2, 36.0, -0.03, -0.03, 0.05, 0.2, 0.0])
    params = np.concatenate([params, [math.radians(68.0), 1.4, -0.5]])  # floor far below
    ball = np.array([0.1, 1.5, -0.08])
    eps = 1e-7
    before = ct.heel_points(params, np.array([-eps, 0.0]), ball, vb)
    after = ct.heel_points(params, np.array([0.0, eps]), ball, vb)

    v_pre = (before[1] - before[0]) / eps
    v_post = (after[1] - after[0]) / eps
    assert math.degrees(math.atan2(v_pre[0], v_pre[1])) == pytest.approx(4.0, abs=1e-3)
    horizontal = math.hypot(v_pre[0], v_pre[1])
    assert math.degrees(math.atan2(v_pre[2], horizontal)) == pytest.approx(-5.0, abs=1e-3)
    assert np.linalg.norm(v_pre) == pytest.approx(36.0, rel=1e-4)
    assert v_post == pytest.approx(v_pre - 0.2 * vb, abs=1e-3)
    assert before[1] == pytest.approx(ball + np.array([-0.03, -0.03, 0.05]))


def test_heel_points_respect_the_mat_floor():
    params = np.array([0.0, math.radians(-8.0), math.radians(62.0), 1.2, 36.0, -0.03, -0.03])
    params = np.concatenate([params, [-0.02, 0.18, 0.0, math.radians(68.0), 1.4, -0.025]])
    ball = np.array([0.0, 1.5, -0.08])
    vb = ct.ball_velocity_vector(50.0, 0.0, 15.0)
    heels = ct.heel_points(params, np.linspace(-0.02, 0.02, 41), ball, vb)
    assert np.all(heels[:, 2] - ball[2] >= -0.025 - 1e-9)


def test_masked_ncc_matches_direct_computation():
    rng = np.random.default_rng(3)
    strip = rng.normal(size=(60, 12))
    mask = (rng.random((60, 12)) > 0.1).astype(float)
    tpl = rng.normal(size=(9, 12))

    fast = ct._masked_ncc(strip, mask, {1.0: tpl})[1.0]  # pylint: disable=protected-access

    for k in range(0, 52, 7):
        x, m = strip[k : k + 9], mask[k : k + 9]
        n = m.sum()
        xm = (x * m).sum() / n
        tm = (tpl * m).sum() / n
        cov = ((x - xm) * (tpl - tm) * m).sum()
        direct = cov / math.sqrt(((x - xm) ** 2 * m).sum() * ((tpl - tm) ** 2 * m).sum())
        assert fast[k] == pytest.approx(direct, abs=1e-9)


def test_templates_have_a_fixed_window_at_every_scale():
    for scale in ct.PRE_SCALES:
        assert ct.pre_impact_template(scale).shape == (44, 55)
    for scale in ct.POST_SCALES:
        assert ct.post_impact_template(scale).shape == (45, 49)


def test_background_fixture_is_real_and_club_free():
    frames, (u0, v0) = load_background()
    assert frames.shape == (4, 400, 640)
    assert 15.0 < frames.mean() < 60.0  # dim garage
    patch = frames[:, int(v0) - 3 : int(v0) + 4, int(u0) - 3 : int(u0) + 4]
    assert patch.mean() > frames.mean() + 40.0  # the resting ball is there
