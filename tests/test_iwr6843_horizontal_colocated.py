"""HLCMF-v2: horizontal launch that a floor reflection cannot bend.

The radar sits a hand's width above the floor, so the ball is heard twice: once
directly and once off the floor, from two elevations. HLCMF-v1's midpoint
reference assumes one elevation. These tests pin the property v2 exists for.
"""

import math

import numpy as np
import pytest

from openflight.iwr6843 import doa, lcmf
from openflight.iwr6843.dump import SAMPLE_RANGE_FFT_IQ16, pack_dump, parse_header
from openflight.iwr6843.lcmf import (
    HORIZONTAL_COHERENCE_MIN,
    HORIZONTAL_COLOCATED_MAX_RANGE_M,
    HORIZONTAL_COLOCATED_MIN_FRAMES,
    TX2_LOOP_PERIOD_S,
    _tx2_colocated_horizontal,
    _tx2_horizontal_proxy,
)
from openflight.iwr6843.shot import ShotMeasurement, geometry_from_header
from openflight.iwr6843.tracking import BallTrack

HORIZONTAL_PHASE = 0.25
N_RX = 4


def array_voltage(position, rays):
    """One element of the 8-element axis hearing every ray (amplitude, sin elevation)."""
    return sum(amplitude * np.exp(1j * np.pi * position * sine) for amplitude, sine in rays)


def tdm_at(rays, *, horizontal_phase=HORIZONTAL_PHASE, motion_phase=0.0):
    """A [1, 1, 3 TX, 4 RX, 1 bin] cube for a ball heard along ``rays``.

    TX1.RXk sits at position k, TX3.RXk at k + 4 and TX2.RXk at k + 2, where it
    also carries the horizontal phase. ``motion_phase`` is an uncorrected
    Doppler step per TX slot, as a ball-speed error would leave.
    """
    tdm = np.zeros((1, 1, 3, N_RX, 1), dtype=complex)
    for rx in range(N_RX):
        tdm[0, 0, 0, rx, 0] = array_voltage(rx, rays)
        tdm[0, 0, 1, rx, 0] = array_voltage(rx + 2, rays) * np.exp(
            1j * (horizontal_phase + motion_phase)
        )
        tdm[0, 0, 2, rx, 0] = array_voltage(rx + 4, rays) * np.exp(2j * motion_phase)
    return tdm


def colocated_phase(tdm):
    via_tx1, via_tx3 = doa.tx2_colocated_products_at(tdm, 0, 0, 0, velocity_ms=0.0, tdm_sign=1)
    return doa.split_reference_phases([via_tx1], [via_tx3])[0]


def midpoint_phase(tdm):
    return doa.tx2_phase_at(tdm, 0, 0, 0, velocity_ms=0.0, tdm_sign=1, n_rx=N_RX)[0]


# Angles off the radar's boresight, which is tipped up about 10 degrees: the
# ball in flight sits near it and its floor image well below.
DIRECT = (1.0, math.sin(math.radians(2.0)))


def floor_bounce(phase_rad, strength=0.8):
    """The floor image, with the path-length phase of the moment."""
    return (strength * np.exp(1j * phase_rad), math.sin(math.radians(-24.0)))


def test_one_clean_arrival_reads_the_same_both_ways():
    tdm = tdm_at([DIRECT])

    assert colocated_phase(tdm) == pytest.approx(HORIZONTAL_PHASE, abs=1e-9)
    assert midpoint_phase(tdm) == pytest.approx(HORIZONTAL_PHASE, abs=1e-9)


@pytest.mark.parametrize("bounce_phase", np.linspace(-3.0, 3.0, 13))
def test_a_floor_reflection_does_not_move_the_colocated_phase(bounce_phase):
    tdm = tdm_at([DIRECT, floor_bounce(bounce_phase)])

    assert colocated_phase(tdm) == pytest.approx(HORIZONTAL_PHASE, abs=1e-9)


def test_the_same_reflection_scatters_the_midpoint_phase():
    """Why v1 fails its coherence gate in a bay with a hard floor."""
    phases = [
        midpoint_phase(tdm_at([DIRECT, floor_bounce(bounce)]))
        for bounce in np.linspace(-3.0, 3.0, 13)
    ]
    errors = np.abs(np.angle(np.exp(1j * (np.array(phases) - HORIZONTAL_PHASE))))

    assert errors.max() > 0.5, "fixture must show the failure it guards against"
    assert abs(np.exp(1j * np.array(phases)).mean()) < HORIZONTAL_COHERENCE_MIN


@pytest.mark.parametrize("motion_phase", [-0.6, -0.2, 0.3, 0.7])
def test_a_ball_speed_error_cancels_between_the_two_sides(motion_phase):
    tdm = tdm_at([DIRECT, floor_bounce(1.1)], motion_phase=motion_phase)

    assert colocated_phase(tdm) == pytest.approx(HORIZONTAL_PHASE, abs=1e-9)


def test_the_cancellation_does_not_depend_on_equal_amplitudes():
    """The floor leaves the TX1 and TX3 sides at different strengths."""
    tdm = tdm_at([DIRECT, floor_bounce(2.4, strength=0.95)], motion_phase=0.4)
    via_tx1, via_tx3 = doa.tx2_colocated_products_at(tdm, 0, 0, 0, velocity_ms=0.0, tdm_sign=1)

    assert abs(via_tx1) != pytest.approx(abs(via_tx3), rel=0.05)
    assert doa.split_reference_phases([via_tx1], [via_tx3])[0] == pytest.approx(
        HORIZONTAL_PHASE, abs=1e-9
    )


def test_motion_correction_uses_the_ball_speed():
    velocity = 50.0
    per_slot = 4.0 * np.pi * velocity * doa.TDM_TAU_S / doa.LAM
    tdm = tdm_at([DIRECT], motion_phase=per_slot)

    via_tx1, via_tx3 = doa.tx2_colocated_products_at(tdm, 0, 0, 0, velocity_ms=velocity, tdm_sign=1)

    assert np.angle(via_tx1) == pytest.approx(HORIZONTAL_PHASE, abs=1e-9)
    assert np.angle(via_tx3) == pytest.approx(HORIZONTAL_PHASE, abs=1e-9)


def test_the_pairing_is_the_one_that_shares_a_position():
    """TX2.RXk is two elements in from TX1.RXk: its twin is TX1.RX(k+2) or TX3.RX(k-2)."""
    assert doa.TX2_COLOCATED_TX1_PAIRS == ((0, 2), (1, 3))
    assert doa.TX2_COLOCATED_TX3_PAIRS == ((2, 0), (3, 1))


def test_frames_fall_back_to_the_side_that_has_signal():
    phases = doa.split_reference_phases([np.exp(0.3j), 0j], [0j, np.exp(0.3j)])

    assert phases == pytest.approx([0.3, 0.3])


def test_a_frame_with_no_signal_is_an_error_not_a_zero():
    with pytest.raises(ValueError, match="usable product"):
        doa.split_reference_phases([0j], [0j])


def test_mismatched_sides_are_refused():
    with pytest.raises(ValueError, match="pair up"):
        doa.split_reference_phases([1 + 0j], [1 + 0j, 1 + 0j])


# -- the estimator on a capture -------------------------------------------

N_FRAMES, N_LOOPS, N_TX, N_BINS = 16, 10, 3, 120
START_BIN = 20
FRAME_PERIOD_US = 3000
RANGE_RES_M = 0.046875  # the production chirp's range bin
AMPLITUDE = 2000.0  # the dump stores int16; a unit-amplitude ball would round away
PHASE_TOLERANCE_RAD = 0.003
ANGLE_TOLERANCE_DEG = 0.06


def track(*, slope_bins=1000.0, intercept_bins=40.0, t_first=0.0, t_last=0.047):
    return BallTrack(
        speed_ms=47.0,
        slope_bins=slope_bins,
        intercept_bins=intercept_bins,
        rms_bins=0.2,
        n_inliers=60,
        t_first=t_first,
        t_last=t_last,
        low_confidence=False,
    )


def capture(ball_track, *, horizontal_phase=HORIZONTAL_PHASE, bounce=True, spoil_from_bin=None):
    """A ball flying along ``ball_track`` with a floor image whose phase turns each frame."""
    cube = np.zeros((N_FRAMES, N_LOOPS * N_TX, N_RX, N_BINS), dtype=complex)
    for frame in range(N_FRAMES):
        for loop in range(N_LOOPS):
            time_s = frame * FRAME_PERIOD_US * 1e-6 + loop * TX2_LOOP_PERIOD_S
            if not ball_track.t_first <= time_s <= ball_track.t_last:
                continue
            range_bin = int(round(ball_track.bin_at(time_s)))
            rays = [DIRECT] + ([floor_bounce(0.9 * frame)] if bounce else [])
            phase = horizontal_phase
            if spoil_from_bin is not None and range_bin >= spoil_from_bin:
                phase = horizontal_phase + (2.5 if frame % 2 else -2.5)  # the net approach
            # The Doppler step between TX slots that the estimator will remove.
            velocity = ball_track.speed_ms_at(time_s, RANGE_RES_M)
            motion = 4.0 * np.pi * velocity * doa.TDM_TAU_S / doa.LAM
            tdm = tdm_at(rays, horizontal_phase=phase, motion_phase=motion)[0, 0]
            # A different carrier phase each loop, as a moving ball has; MTI keeps it.
            carrier = AMPLITUDE * np.exp(1j * 0.7 * loop)
            for tx in range(N_TX):
                cube[frame, loop * N_TX + tx, :, range_bin - START_BIN] = carrier * tdm[tx, :, 0]
    raw = pack_dump(
        cube,
        n_tx=N_TX,
        version=3,
        frame_period_us=FRAME_PERIOD_US,
        sample_fmt=SAMPLE_RANGE_FFT_IQ16,
        range_bin_start=START_BIN,
    )
    return raw, ShotMeasurement(geometry=None, ball_found=True, track=ball_track)


def test_the_fixture_uses_the_real_range_bin():
    raw, _shot = capture(track())
    geometry = geometry_from_header(parse_header(raw), loop_period_s=TX2_LOOP_PERIOD_S)

    assert geometry.range_res_m == pytest.approx(RANGE_RES_M)


def expected_angle_deg(phase=HORIZONTAL_PHASE, reference=0.0):
    return -math.degrees(math.asin((phase - reference) / math.pi))


def test_a_swing_over_a_reflecting_floor_is_accepted():
    raw, shot = capture(track())

    angle_deg, coherence, status, phase_rad = _tx2_colocated_horizontal(raw, shot, tdm_sign=1)

    assert status == "hlcmf_v2_accepted"
    assert coherence > 0.99
    assert phase_rad == pytest.approx(HORIZONTAL_PHASE, abs=PHASE_TOLERANCE_RAD)
    assert angle_deg == pytest.approx(expected_angle_deg(), abs=ANGLE_TOLERANCE_DEG)


def test_the_midpoint_method_withholds_the_same_swing():
    raw, shot = capture(track())

    angle_deg, coherence, status = _tx2_horizontal_proxy(raw, shot, tdm_sign=1)

    assert status == "hlcmf_v1_low_coherence"
    assert angle_deg is None
    assert coherence < HORIZONTAL_COHERENCE_MIN


def test_both_methods_agree_when_the_floor_is_quiet():
    raw, shot = capture(track(), bounce=False)

    v2_angle, _c2, v2_status, _phase = _tx2_colocated_horizontal(raw, shot, tdm_sign=1)
    v1_angle, _c1, v1_status = _tx2_horizontal_proxy(raw, shot, tdm_sign=1)

    assert (v1_status, v2_status) == ("hlcmf_v1_accepted", "hlcmf_v2_accepted")
    assert v2_angle == pytest.approx(v1_angle, abs=ANGLE_TOLERANCE_DEG)
    assert v2_angle == pytest.approx(expected_angle_deg(), abs=ANGLE_TOLERANCE_DEG)


def test_the_rigs_straight_line_is_removed_before_the_angle():
    raw, shot = capture(track())

    angle_deg, _coherence, status, phase_rad = _tx2_colocated_horizontal(
        raw, shot, tdm_sign=1, phase_reference_rad=0.1
    )

    assert status == "hlcmf_v2_accepted"
    unreferenced = _tx2_colocated_horizontal(raw, shot, tdm_sign=1)[0]
    assert angle_deg - unreferenced == pytest.approx(
        expected_angle_deg(reference=0.1) - expected_angle_deg(), abs=0.02
    )
    # The phase handed back for zeroing is the raw one, whatever the reference.
    assert phase_rad == pytest.approx(HORIZONTAL_PHASE, abs=PHASE_TOLERANCE_RAD)


def test_flight_past_the_range_limit_is_not_read():
    """The last frames before the net scatter; they must not cost the swing."""
    limit_bin = int(HORIZONTAL_COLOCATED_MAX_RANGE_M / RANGE_RES_M)
    # Fast enough that the ball is past the limit well before the capture ends.
    ball_track = track(slope_bins=1300.0, intercept_bins=limit_bin - 30.0)
    raw, shot = capture(ball_track, spoil_from_bin=limit_bin + 1)
    assert ball_track.bin_at(ball_track.t_last) > limit_bin + 20, "fixture must fly past the limit"

    angle_deg, _coherence, status, _phase = _tx2_colocated_horizontal(raw, shot, tdm_sign=1)
    unlimited = _tx2_colocated_horizontal(raw, shot, tdm_sign=1, max_range_m=99.0)

    assert status == "hlcmf_v2_accepted"
    assert angle_deg == pytest.approx(expected_angle_deg(), abs=ANGLE_TOLERANCE_DEG)
    assert unlimited[2] == "hlcmf_v2_low_coherence"


def test_too_few_frames_in_range_is_said_plainly():
    short = track(t_first=0.0, t_last=(HORIZONTAL_COLOCATED_MIN_FRAMES - 2) * 0.003)
    raw, shot = capture(short)

    angle_deg, coherence, status, phase_rad = _tx2_colocated_horizontal(raw, shot, tdm_sign=1)

    assert (angle_deg, coherence, phase_rad) == (None, None, None)
    assert status == "hlcmf_v2_insufficient_frames"


def test_incoherent_frames_are_withheld_with_their_coherence(monkeypatch):
    raw, shot = capture(track())

    def flipping(_mti, frame, *_args, **_kwargs):
        product = np.exp(1j * (0.0 if frame % 2 == 0 else np.pi))
        return product, product

    monkeypatch.setattr(lcmf.doa, "tx2_colocated_products_at", flipping)

    angle_deg, coherence, status, phase_rad = _tx2_colocated_horizontal(raw, shot, tdm_sign=1)

    assert status == "hlcmf_v2_low_coherence"
    assert angle_deg is None and phase_rad is None
    assert coherence < HORIZONTAL_COHERENCE_MIN


def test_no_ball_track_or_no_tx2_means_no_reading():
    raw, _shot = capture(track())
    no_track = ShotMeasurement(geometry=None, ball_found=False, track=None)

    assert _tx2_colocated_horizontal(raw, no_track, tdm_sign=1) == (None, None, None, None)


def test_an_unknown_method_is_refused_before_any_work():
    with pytest.raises(ValueError, match="horizontal_method"):
        lcmf.estimate_lcmf_v1(b"", None, ball_speed_mph=100.0, horizontal_method="sideways")


def test_the_default_method_is_still_upstreams():
    """The OPS243 path was validated against TrackMan with v1; v2 is opt-in."""
    import inspect

    assert inspect.signature(lcmf.estimate_lcmf_v1).parameters["horizontal_method"].default == (
        "midpoint"
    )
    assert lcmf.HORIZONTAL_METHODS == ("midpoint", "colocated")
