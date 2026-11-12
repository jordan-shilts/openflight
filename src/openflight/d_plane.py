"""D-plane club delivery: face angle, dynamic loft and spin axis from launch and delivery.

A ball starts mostly where the face points and curves away from the club's
path. Two relations turn that into numbers:

1. Start direction. The ball's start is a weighted blend of the face and the
   club's delivery::

       start = share * face + (1 - share) * delivery

   with ``share`` near 0.85 for a driver and 0.75 for irons. It holds in both
   planes: horizontally (launch direction, face angle, club path) and
   vertically (launch angle, dynamic loft, attack angle). Inverting it gives
   the face term from a measured launch and a measured delivery.

2. Spin axis. The ball spins about the axis perpendicular to the D-plane, the
   plane holding the club's velocity and the face normal. The tilt of that
   axis is what curves the ball.

Validated against the committed TrackMan session
(``session_logs/OpenFlight-Test.Normalized.csv``, 2026-05-06, one player):

- Face angle from TrackMan's own launch direction and club path: 0.12 deg mean
  error over 23 shots.
- Spin axis for the 7-iron and pitching wedge (15 shots): 0.4 deg mean error,
  against 3.1 deg for the earlier ``launch direction - club path`` rule.
- Spin axis for the driver (8 shots): 18 deg mean error, worse than assuming
  no tilt at all (10 deg). Those drives were struck 5-27 mm off-centre, and on
  a wood the gear effect of an off-centre strike adds about as much side spin
  as the D-plane does. Nothing in OpenFlight measures strike location, so the
  spin axis of a driver, wood or hybrid is withheld unless the operator opts in.

``tests/test_d_plane_trackman_regression.py`` holds those numbers in place.
The accuracy above is the ceiling, reached with TrackMan-quality inputs; live
it is bounded by how well launch direction and club path are measured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

from openflight.clubs import ClubType

if TYPE_CHECKING:
    from openflight.launch_monitor import Shot

# Confidence tiers of a camera-fused club path or attack angle that are good
# enough to drive ball flight and to send to a simulator.
TRUSTED_DELIVERY_TIERS = ("high", "medium")

# A spin axis is only as good as its weaker horizontal input: derive it once
# horizontal launch clears this confidence floor, not the moment a club path
# is present.
SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE = 0.6

# Beyond this tilt the inputs are more likely wrong than the shot is that
# curved (TrackMan's largest in the validation session was 21 deg), so a
# larger result is limited to it rather than sent on as a wild hook or slice.
SPIN_AXIS_MAX_ABS_DEG = 30.0

_WOODS = frozenset({ClubType.WOOD_3, ClubType.WOOD_5, ClubType.WOOD_7})
_HYBRIDS = frozenset({ClubType.HYBRID_3, ClubType.HYBRID_5, ClubType.HYBRID_7, ClubType.HYBRID_9})
_LONG_IRONS = frozenset(
    {
        ClubType.IRON_2,
        ClubType.IRON_3,
        ClubType.IRON_4,
        ClubType.IRON_5,
        ClubType.IRON_6,
        ClubType.IRON_7,
    }
)
_SHORT_IRONS_AND_WEDGES = frozenset(
    {ClubType.IRON_8, ClubType.IRON_9, ClubType.PW, ClubType.GW, ClubType.SW, ClubType.LW}
)


def face_share(club: ClubType) -> float:
    """Share of the ball's start that follows the face rather than the delivery.

    Published fits put it near 85% for a driver and 75% for irons; a putter
    starts the ball almost on the face. The kiosk's Club tab uses the same
    table (``ui/src/components/panel/clubDelivery.ts``); a shared fixture keeps
    the two in step.
    """
    if club is ClubType.PUTTER:
        return 0.9
    if club is ClubType.DRIVER:
        return 0.85
    if club in _WOODS:
        return 0.82
    if club in _HYBRIDS:
        return 0.8
    if club in _SHORT_IRONS_AND_WEDGES:
        return 0.74
    if club in _LONG_IRONS:
        return 0.77
    return 0.78


def typical_attack_angle_deg(club: ClubType) -> float:
    """Attack angle to assume when none was measured, in degrees (down is negative).

    Rounded TrackMan tour averages by club family. Only the spin axis uses it,
    through spin loft, where a 2 degree error moves the result by under a
    tenth of its value; dynamic loft and spin loft themselves are never
    reported from it.
    """
    if club is ClubType.DRIVER:
        return -1.0
    if club in _WOODS:
        return -3.0
    if club in _HYBRIDS:
        return -3.5
    if club in _SHORT_IRONS_AND_WEDGES:
        return -4.8
    return -4.0


def has_unmeasured_gear_effect(club: ClubType) -> bool:
    """True for clubs whose off-centre strikes tilt the spin axis on their own.

    A driver, wood or hybrid has its centre of mass well behind a curved face,
    so a heel or toe strike twists the head and spins the ball sideways (gear
    effect). Irons and wedges barely do.
    """
    return club is ClubType.DRIVER or club in _WOODS or club in _HYBRIDS


def face_from_start(start_deg: float, delivery_deg: float, share: float) -> float:
    """Invert ``start = share * face + (1 - share) * delivery`` for the face term."""
    return (start_deg - (1.0 - share) * delivery_deg) / share


def d_plane_spin_axis_deg(
    face_angle_deg: float,
    club_path_deg: float,
    dynamic_loft_deg: float,
    attack_angle_deg: float,
) -> float:
    """Spin-axis tilt from the D-plane, in degrees.

    Sign convention matches ``ballistics``: 0 is pure backspin, positive tilts
    the axis so the ball curves right (a fade for a right-hander), negative
    curves it left (a draw).

    With x toward the target, y to the right and z up, the club's velocity
    direction is ``v = (cos a cos p, cos a sin p, sin a)`` and the face normal
    ``n = (cos L cos f, cos L sin f, sin L)``. The ball's spin vector is along
    ``v x n``; its tilt out of the horizontal is the spin axis.
    """
    face = math.radians(face_angle_deg)
    path = math.radians(club_path_deg)
    loft = math.radians(dynamic_loft_deg)
    attack = math.radians(attack_angle_deg)
    side = math.cos(attack) * math.cos(loft) * math.sin(face - path)
    back = math.cos(attack) * math.cos(path) * math.sin(loft) - math.sin(attack) * math.cos(
        loft
    ) * math.cos(face)
    return math.degrees(math.atan2(side, back))


@dataclass(frozen=True)
class ClubDelivery:
    """What the D-plane says about one shot. A None field had an input missing."""

    face_angle_deg: Optional[float] = None
    face_to_path_deg: Optional[float] = None
    dynamic_loft_deg: Optional[float] = None
    spin_loft_deg: Optional[float] = None
    spin_axis_deg: Optional[float] = None
    # "measured" (a club radar) or "camera" (camera-fused, tier high or medium).
    club_path_source: Optional[str] = None
    # "measured", "camera", or "typical" (the per-club assumption, spin axis only).
    attack_angle_source: Optional[str] = None
    # Why the spin axis is missing, for logs: None when it was derived.
    spin_axis_withheld: Optional[str] = None


def trusted_club_path(shot: "Shot") -> Optional[Tuple[float, str]]:
    """The club path good enough to drive ball flight, with its source."""
    if shot.club_path_deg is not None:
        return float(shot.club_path_deg), "measured"
    fused = shot.experimental_fused_club_path_deg
    if fused is not None and shot.experimental_fused_club_path_confidence in TRUSTED_DELIVERY_TIERS:
        return float(fused), "camera"
    return None


def trusted_attack_angle(shot: "Shot") -> Optional[Tuple[float, str]]:
    """The attack angle good enough to drive ball flight, with its source."""
    if shot.club_angle_deg is not None:
        return float(shot.club_angle_deg), "measured"
    fused = shot.experimental_fused_attack_angle_deg
    if (
        fused is not None
        and shot.experimental_fused_attack_angle_confidence in TRUSTED_DELIVERY_TIERS
    ):
        return float(fused), "camera"
    return None


def _is_measured(value: Optional[float], source: Optional[str]) -> bool:
    return value is not None and source != "estimated"


def estimate_club_delivery(
    shot: "Shot", *, include_gear_effect_clubs: bool = False
) -> ClubDelivery:
    """Face angle, lofts and spin axis for one shot, each only from trusted inputs.

    - Face angle and face-to-path need a measured horizontal launch and a
      trusted club path.
    - Dynamic loft and spin loft need a measured vertical launch and a trusted
      attack angle.
    - The spin axis needs the face angle, a measured vertical launch, and a
      horizontal launch confidence of at least
      ``SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE``. A missing attack angle falls
      back to the club's typical one. Putts never get one, and drivers, woods
      and hybrids only with ``include_gear_effect_clubs``.
    """
    club = shot.club
    share = face_share(club)
    path = trusted_club_path(shot)
    attack = trusted_attack_angle(shot)
    horizontal_measured = _is_measured(
        shot.launch_angle_horizontal, shot.launch_angle_horizontal_source
    )
    vertical_measured = _is_measured(shot.launch_angle_vertical, shot.launch_angle_vertical_source)

    face = None
    face_to_path = None
    if horizontal_measured and path is not None:
        face = face_from_start(float(shot.launch_angle_horizontal), path[0], share)
        face_to_path = face - path[0]

    dynamic_loft = None
    spin_loft = None
    if vertical_measured and attack is not None:
        dynamic_loft = face_from_start(float(shot.launch_angle_vertical), attack[0], share)
        spin_loft = dynamic_loft - attack[0]

    spin_axis = None
    attack_source = attack[1] if attack is not None else None
    withheld = None
    if face is None or path is None:
        withheld = "needs measured horizontal launch and club path"
    elif not vertical_measured:
        withheld = "needs measured launch angle"
    elif (shot.launch_angle_horizontal_confidence or 0.0) < SPIN_AXIS_MIN_HORIZONTAL_CONFIDENCE:
        withheld = "horizontal launch confidence too low"
    elif club is ClubType.PUTTER:
        withheld = "putt"
    elif has_unmeasured_gear_effect(club) and not include_gear_effect_clubs:
        withheld = "strike location not measured for this club"
    else:
        if attack is not None:
            attack_deg = attack[0]
            loft_deg = dynamic_loft
        else:
            attack_deg = typical_attack_angle_deg(club)
            attack_source = "typical"
            loft_deg = face_from_start(float(shot.launch_angle_vertical), attack_deg, share)
        raw = d_plane_spin_axis_deg(face, path[0], loft_deg, attack_deg)
        spin_axis = max(-SPIN_AXIS_MAX_ABS_DEG, min(SPIN_AXIS_MAX_ABS_DEG, raw))

    return ClubDelivery(
        face_angle_deg=face,
        face_to_path_deg=face_to_path,
        dynamic_loft_deg=dynamic_loft,
        spin_loft_deg=spin_loft,
        spin_axis_deg=spin_axis,
        club_path_source=path[1] if path is not None else None,
        attack_angle_source=attack_source,
        spin_axis_withheld=withheld,
    )
