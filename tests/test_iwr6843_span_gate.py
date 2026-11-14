"""Span floor at 15 ms, and span recorded so the gate is visible.

Session 140533 went 6/7 to 7/7 accepted at 15 ms, recovering shot 1 at
19.25 deg. Session 130951 went 1/7 to 2/7. Span was the gate that rejected
7 of 8 shots in the first session and was invisible in the log.
"""

from openflight.iwr6843 import shot as shotmod
from openflight.iwr6843.lcmf import LCMFResult


def test_span_floor_is_15ms():
    assert shotmod.REJECT_MIN_SPAN_S == 0.015


def test_track_broken_boundary():
    class FakeTrack:
        rms_bins = 0.20
        n_inliers = 40

        def __init__(self, span_s):
            self.t_first = 0.0
            self.t_last = span_s

    assert shotmod.track_broken(FakeTrack(0.0149)) is True
    assert shotmod.track_broken(FakeTrack(0.0151)) is False


def test_track_broken_still_enforces_rms_and_inliers():
    """Relaxing span must not weaken the other two gates."""

    class FakeTrack:
        def __init__(self, rms, inliers):
            self.rms_bins = rms
            self.n_inliers = inliers
            self.t_first = 0.0
            self.t_last = 0.033

    assert shotmod.track_broken(FakeTrack(0.60, 40)) is True   # rms >= 0.50
    assert shotmod.track_broken(FakeTrack(0.20, 27)) is True   # inliers < 28
    assert shotmod.track_broken(FakeTrack(0.20, 40)) is False


def test_result_exposes_track_span():
    payload = LCMFResult(status="accepted", track_span_s=0.0331).to_dict()
    assert payload["track_span_s"] == 0.0331


def test_thin_ball_seen_across_the_capture_is_not_junk():
    """2026-11-13 5-iron swings: 100-107 mph on 14-27 clean points over 34-46 ms."""

    class FakeTrack:
        def __init__(self, inliers, span_s, rms=0.40):
            self.rms_bins = rms
            self.n_inliers = inliers
            self.t_first = 0.003
            self.t_last = 0.003 + span_s

    assert shotmod.track_broken(FakeTrack(23, 0.043)) is True    # under the full floor
    assert shotmod.thin_but_whole(FakeTrack(23, 0.043)) is True  # but a whole, clean read
    assert shotmod.thin_but_whole(FakeTrack(14, 0.030)) is True
    assert shotmod.thin_but_whole(FakeTrack(13, 0.043)) is False  # too few points
    assert shotmod.thin_but_whole(FakeTrack(23, 0.029)) is False  # short: clubhead territory
    assert shotmod.thin_but_whole(FakeTrack(23, 0.043, rms=0.50)) is False  # ragged
    assert shotmod.thin_but_whole(None) is False
