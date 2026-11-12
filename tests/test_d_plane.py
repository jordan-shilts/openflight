"""Tests for d_plane: face angle, lofts and spin axis from launch and delivery."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from openflight.clubs import ClubType
from openflight.d_plane import (
    SPIN_AXIS_MAX_ABS_DEG,
    SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE,
    TRUSTED_DELIVERY_TIERS,
    d_plane_spin_axis_deg,
    estimate_club_delivery,
    face_from_start,
    face_share,
    has_unmeasured_gear_effect,
    trusted_attack_angle,
    trusted_club_path,
    typical_attack_angle_deg,
)
from openflight.launch_monitor import Shot

FACE_SHARE_FIXTURE = Path(__file__).parent / "fixtures" / "d_plane_face_share.json"


def _shot(**overrides) -> Shot:
    """A 7-iron with every D-plane input measured and trusted."""
    base = dict(
        ball_speed_mph=120.0,
        club_speed_mph=85.0,
        timestamp=datetime(2026, 11, 11, 12, 0, 0),
        club=ClubType.IRON_7,
        launch_angle_vertical=16.0,
        launch_angle_vertical_source="radar",
        launch_angle_horizontal=1.0,
        launch_angle_horizontal_source="radar",
        launch_angle_horizontal_confidence=0.9,
        club_path_deg=-3.0,
        club_angle_deg=-5.0,
    )
    base.update(overrides)
    return Shot(**base)


class TestFaceShare:
    def test_matches_the_table_shared_with_the_kiosk(self):
        table = json.loads(FACE_SHARE_FIXTURE.read_text())
        table.pop("_comment")
        assert set(table) == {club.value for club in ClubType}
        for club in ClubType:
            assert face_share(club) == table[club.value], club

    def test_inversion_round_trips(self):
        share = face_share(ClubType.DRIVER)
        start = share * 2.0 + (1 - share) * -4.0
        assert face_from_start(start, -4.0, share) == pytest.approx(2.0)


class TestSpinAxisGeometry:
    def test_square_face_to_path_is_pure_backspin(self):
        assert d_plane_spin_axis_deg(2.0, 2.0, 20.0, -4.0) == pytest.approx(0.0)

    def test_face_open_to_path_tilts_right(self):
        assert d_plane_spin_axis_deg(2.0, -2.0, 20.0, -4.0) > 0

    def test_face_closed_to_path_tilts_left(self):
        assert d_plane_spin_axis_deg(-2.0, 2.0, 20.0, -4.0) < 0

    def test_is_antisymmetric(self):
        right = d_plane_spin_axis_deg(1.5, -1.5, 22.0, -4.0)
        left = d_plane_spin_axis_deg(-1.5, 1.5, 22.0, -4.0)
        assert left == pytest.approx(-right)

    def test_less_spin_loft_curves_more_for_the_same_face_to_path(self):
        driver_like = d_plane_spin_axis_deg(2.0, 0.0, 12.0, 1.0)
        wedge_like = d_plane_spin_axis_deg(2.0, 0.0, 30.0, -5.0)
        assert driver_like > 2 * wedge_like > 0

    def test_matches_the_small_angle_relation(self):
        # tan(axis) ~ sin(face - path) * cos(loft) / sin(loft - attack)
        axis = d_plane_spin_axis_deg(0.0, -3.0, 20.0, -5.0)
        assert axis == pytest.approx(6.7, abs=0.2)


class TestTrustedInputs:
    def test_measured_club_path_wins(self):
        shot = _shot(
            experimental_fused_club_path_deg=4.0,
            experimental_fused_club_path_confidence="high",
        )
        assert trusted_club_path(shot) == (-3.0, "measured")

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("high", (4.0, "camera")), ("medium", (4.0, "camera")), ("low", None), ("withheld", None)],
    )
    def test_camera_club_path_needs_a_trusted_tier(self, tier, expected):
        shot = _shot(
            club_path_deg=None,
            experimental_fused_club_path_deg=4.0,
            experimental_fused_club_path_confidence=tier,
        )
        assert trusted_club_path(shot) == expected

    def test_radar_only_candidate_path_is_never_trusted(self):
        shot = _shot(club_path_deg=None, experimental_club_path_deg=2.0)
        assert trusted_club_path(shot) is None

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("high", (-2.0, "camera")), ("medium", (-2.0, "camera")), ("low", None)],
    )
    def test_camera_attack_angle_needs_a_trusted_tier(self, tier, expected):
        shot = _shot(
            club_angle_deg=None,
            experimental_fused_attack_angle_deg=-2.0,
            experimental_fused_attack_angle_confidence=tier,
        )
        assert trusted_attack_angle(shot) == expected

    def test_trusted_tiers_are_high_and_medium(self):
        assert TRUSTED_DELIVERY_TIERS == ("high", "medium")


class TestEstimateClubDelivery:
    def test_all_inputs_measured(self):
        delivery = estimate_club_delivery(_shot())

        share = face_share(ClubType.IRON_7)
        face = (1.0 - (1 - share) * -3.0) / share
        loft = (16.0 - (1 - share) * -5.0) / share
        assert delivery.face_angle_deg == pytest.approx(face)
        assert delivery.face_to_path_deg == pytest.approx(face + 3.0)
        assert delivery.dynamic_loft_deg == pytest.approx(loft)
        assert delivery.spin_loft_deg == pytest.approx(loft + 5.0)
        assert delivery.spin_axis_deg == pytest.approx(
            d_plane_spin_axis_deg(face, -3.0, loft, -5.0)
        )
        assert delivery.spin_axis_deg > 0  # face open to path: fade
        assert delivery.club_path_source == "measured"
        assert delivery.attack_angle_source == "measured"
        assert delivery.spin_axis_withheld is None

    def test_camera_fused_inputs_are_used_when_trusted(self):
        delivery = estimate_club_delivery(
            _shot(
                club_path_deg=None,
                club_angle_deg=None,
                experimental_fused_club_path_deg=-3.0,
                experimental_fused_club_path_confidence="high",
                experimental_fused_attack_angle_deg=-5.0,
                experimental_fused_attack_angle_confidence="medium",
            )
        )

        assert delivery.club_path_source == "camera"
        assert delivery.attack_angle_source == "camera"
        assert delivery.spin_axis_deg == pytest.approx(estimate_club_delivery(_shot()).spin_axis_deg)

    def test_no_trusted_club_path_gives_nothing_horizontal(self):
        delivery = estimate_club_delivery(
            _shot(
                club_path_deg=None,
                experimental_fused_club_path_deg=-3.0,
                experimental_fused_club_path_confidence="low",
            )
        )

        assert delivery.face_angle_deg is None
        assert delivery.face_to_path_deg is None
        assert delivery.spin_axis_deg is None
        assert delivery.spin_axis_withheld == "needs measured horizontal launch and club path"
        assert delivery.dynamic_loft_deg is not None  # the vertical side still stands

    def test_estimated_horizontal_launch_gives_nothing_horizontal(self):
        delivery = estimate_club_delivery(_shot(launch_angle_horizontal_source="estimated"))

        assert delivery.face_angle_deg is None
        assert delivery.spin_axis_deg is None

    def test_missing_attack_angle_uses_the_typical_one_for_spin_axis_only(self):
        delivery = estimate_club_delivery(_shot(club_angle_deg=None))

        assert delivery.dynamic_loft_deg is None
        assert delivery.spin_loft_deg is None
        assert delivery.attack_angle_source == "typical"
        share = face_share(ClubType.IRON_7)
        typical = typical_attack_angle_deg(ClubType.IRON_7)
        loft = (16.0 - (1 - share) * typical) / share
        assert delivery.spin_axis_deg == pytest.approx(
            d_plane_spin_axis_deg(delivery.face_angle_deg, -3.0, loft, typical)
        )

    def test_typical_attack_angle_moves_spin_axis_only_slightly(self):
        measured = estimate_club_delivery(_shot()).spin_axis_deg
        assumed = estimate_club_delivery(_shot(club_angle_deg=None)).spin_axis_deg
        assert assumed == pytest.approx(measured, rel=0.1)

    def test_estimated_vertical_launch_withholds_lofts_and_spin_axis(self):
        delivery = estimate_club_delivery(_shot(launch_angle_vertical_source="estimated"))

        assert delivery.face_angle_deg is not None
        assert delivery.dynamic_loft_deg is None
        assert delivery.spin_axis_deg is None
        assert delivery.spin_axis_withheld == "needs measured launch angle"

    def test_low_horizontal_confidence_keeps_face_but_withholds_spin_axis(self):
        delivery = estimate_club_delivery(
            _shot(launch_angle_horizontal_confidence=SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE - 0.01)
        )

        assert delivery.face_angle_deg is not None
        assert delivery.spin_axis_deg is None
        assert delivery.spin_axis_withheld == "horizontal launch confidence too low"

    def test_confidence_exactly_at_the_floor_passes(self):
        delivery = estimate_club_delivery(
            _shot(launch_angle_horizontal_confidence=SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE)
        )
        assert delivery.spin_axis_deg is not None

    def test_missing_horizontal_confidence_withholds_spin_axis(self):
        delivery = estimate_club_delivery(_shot(launch_angle_horizontal_confidence=None))
        assert delivery.spin_axis_deg is None

    @pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.HYBRID_5])
    def test_gear_effect_clubs_get_face_angle_but_no_spin_axis_by_default(self, club):
        delivery = estimate_club_delivery(_shot(club=club))

        assert delivery.face_angle_deg is not None
        assert delivery.spin_axis_deg is None
        assert delivery.spin_axis_withheld == "strike location not measured for this club"

    @pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.HYBRID_5])
    def test_gear_effect_clubs_get_a_spin_axis_when_opted_in(self, club):
        delivery = estimate_club_delivery(_shot(club=club), include_gear_effect_clubs=True)
        assert delivery.spin_axis_deg is not None

    def test_putt_never_gets_a_spin_axis(self):
        delivery = estimate_club_delivery(
            _shot(club=ClubType.PUTTER), include_gear_effect_clubs=True
        )
        assert delivery.spin_axis_deg is None
        assert delivery.spin_axis_withheld == "putt"

    @pytest.mark.parametrize("launch_h", [14.0, -14.0])
    def test_spin_axis_is_limited(self, launch_h):
        delivery = estimate_club_delivery(_shot(launch_angle_horizontal=launch_h, club_path_deg=0.0))
        assert abs(delivery.spin_axis_deg) == pytest.approx(SPIN_AXIS_MAX_ABS_DEG)
        assert (delivery.spin_axis_deg > 0) == (launch_h > 0)


class TestClubFamilies:
    @pytest.mark.parametrize("club", list(ClubType))
    def test_every_club_has_a_typical_attack_angle(self, club):
        assert -6.0 <= typical_attack_angle_deg(club) <= 0.0

    def test_only_drivers_woods_and_hybrids_carry_gear_effect(self):
        with_gear = {club for club in ClubType if has_unmeasured_gear_effect(club)}
        assert with_gear == {
            ClubType.DRIVER,
            ClubType.WOOD_3,
            ClubType.WOOD_5,
            ClubType.WOOD_7,
            ClubType.HYBRID_3,
            ClubType.HYBRID_5,
            ClubType.HYBRID_7,
            ClubType.HYBRID_9,
        }
