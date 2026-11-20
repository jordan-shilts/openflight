"""Calculated spin from measured ball speed and launch angle.

The OPS243 return carries no usable spin line: golf-ball dimples
(~0.3 mm) are Rayleigh-smooth at 24 GHz (λ = 12.4 mm) and the specular
point does not rotate with the ball, so rotation barely modulates the
echo within our ~65 ms ball dwell. Offline analysis of the 2026-06-08
TrackMan session (131 paired shots, PW-3h) found no spectral line at
the TrackMan spin frequency on any club after dechirp and carrier
removal — and the production envelope estimator's output had ~zero
within-club correlation with TrackMan spin (r ≈ +0.19).

What does work is impact kinematics. Spin loft tracks launch angle and
the friction impulse gives spin ∝ v·sin(spin loft), yielding a single
global formula with no club input:

    spin_rpm = 170 · ball_speed_mph · sin(LA)^1.2

Validation against the 2026-06-08 TrackMan truth set:
- TrackMan inputs (physics ceiling): 10.5% median error across the
  bag — better than an oracle per-club median table (12.0%)
- Leave-one-club-out blind: 11.3% median, 97/131 within 25%; the
  fitted (coefficient, exponent) were stable across all folds
- Simulated with production launch-angle noise (2.5° MAE two-ray):
  ~21% median expected live

Caveats: calibrated on one player/session (range balls). Attack-angle
style shifts spin loft at a fixed launch angle, so cross-player error
is likely a few points worse. Accuracy degrades at low launch angles
(cot(LA) error amplification) — the same clubs where measured launch
angle is weakest.
"""

import math
from typing import Optional

from .clubs import ClubType
from .clubs.physics import CLUB_PHYSICS

# Fitted on the 2026-06-08 TrackMan session (131 shots, PW-3h);
# leave-one-club-out stable at (170, 1.2) across all folds.
SPIN_COEFF_RPM_PER_MPH = 170.0
SPIN_LA_EXPONENT = 1.2

# Outside these bounds the kinematic model is extrapolating into
# regimes it was never calibrated on (top-spinned thins, pop-ups).
MIN_LAUNCH_ANGLE_DEG = 2.0
MAX_LAUNCH_ANGLE_DEG = 60.0

# Physical ceiling — beyond fresh-groove lob wedge territory.
MAX_SPIN_RPM = 13000.0


def calculated_spin_rpm(
    ball_speed_mph: float,
    launch_angle_deg: float,
) -> Optional[float]:
    """Kinematic spin estimate from ball speed and vertical launch angle.

    Use the true (cosine-corrected) ball speed when available — the
    model was calibrated against TrackMan ball speed.

    Returns None when inputs are missing or outside the calibrated
    range; callers should fall back to club-typical spin.
    """
    if ball_speed_mph is None or launch_angle_deg is None:
        return None
    if ball_speed_mph <= 0:
        return None
    if not MIN_LAUNCH_ANGLE_DEG <= launch_angle_deg <= MAX_LAUNCH_ANGLE_DEG:
        return None

    sin_la = math.sin(math.radians(launch_angle_deg))
    spin = SPIN_COEFF_RPM_PER_MPH * ball_speed_mph * sin_la**SPIN_LA_EXPONENT
    return float(min(spin, MAX_SPIN_RPM))


# --- Club-anchored model -----------------------------------------------------
#
# The kinematic formula above was fitted on one player's range-ball session.
# On a rig whose launch angle is noisy (garage, 2026-11-19: one 6-iron session
# read 9-28 deg) it turns every launch misread into a spin swing of thousands
# of rpm, and its level sits well under real-ball irons (a 115 mph, 15 deg
# 6-iron gives ~3,900 rpm; tour 6-irons average ~6,200). The club-anchored
# model starts from the club's typical spin (CLUB_PHYSICS.typical_spin_rpm,
# the TrackMan PGA Tour averages ballistics already falls back to) and
# nudges it, within tight limits, for how hard the ball was hit and for how
# its launch compares with the tour launch that spin belongs to.

# TrackMan PGA Tour average launch angles. Clubs without a published tour
# average take their neighbour's (woods/hybrids by loft, wedges +3 deg a club).
TOUR_LAUNCH_DEG: dict[ClubType, float] = {
    ClubType.DRIVER: 10.9,
    ClubType.WOOD_3: 9.2,
    ClubType.WOOD_5: 9.4,
    ClubType.WOOD_7: 10.2,
    ClubType.HYBRID_3: 10.2,
    ClubType.HYBRID_5: 11.0,
    ClubType.HYBRID_7: 12.1,
    ClubType.HYBRID_9: 14.1,
    ClubType.IRON_2: 10.0,
    ClubType.IRON_3: 10.4,
    ClubType.IRON_4: 11.0,
    ClubType.IRON_5: 12.1,
    ClubType.IRON_6: 14.1,
    ClubType.IRON_7: 16.3,
    ClubType.IRON_8: 18.1,
    ClubType.IRON_9: 20.4,
    ClubType.PW: 24.2,
    ClubType.GW: 27.0,
    ClubType.SW: 30.0,
    ClubType.LW: 33.0,
}

# Harder hits spin more at the same loft; square-root and clamped so a speed
# misread cannot run away with it.
CLUB_SPIN_SPEED_EXPONENT = 0.5
CLUB_SPIN_SPEED_LIMITS = (0.85, 1.15)
# Higher launch at the same club means more spin loft; square-root and clamped
# because the launch angle is the noisiest input.
CLUB_SPIN_LAUNCH_EXPONENT = 0.5
CLUB_SPIN_LAUNCH_LIMITS = (0.80, 1.25)


def _clamped(value: float, limits: tuple[float, float]) -> float:
    low, high = limits
    return min(max(value, low), high)


def club_anchored_spin_rpm(
    club: Optional[ClubType],
    ball_speed_mph: Optional[float],
    launch_angle_deg: Optional[float] = None,
) -> Optional[float]:
    """Spin from the club's typical spin, adjusted for ball speed and launch.

    ``launch_angle_deg`` is optional: without a measured launch (or one
    outside MIN/MAX_LAUNCH_ANGLE_DEG) only the speed adjustment applies.
    Returns None for the putter, an unknown club, or a missing/invalid speed;
    callers then keep whatever spin they had.
    """
    if club is None or club in (ClubType.PUTTER, ClubType.UNKNOWN):
        return None
    physics = CLUB_PHYSICS.get(club)
    if physics is None:
        return None
    if ball_speed_mph is None or not math.isfinite(ball_speed_mph) or ball_speed_mph <= 0:
        return None

    speed_factor = (ball_speed_mph / physics.average_ball_speed_mph) ** CLUB_SPIN_SPEED_EXPONENT
    spin = physics.typical_spin_rpm * _clamped(speed_factor, CLUB_SPIN_SPEED_LIMITS)

    if (
        launch_angle_deg is not None
        and math.isfinite(launch_angle_deg)
        and MIN_LAUNCH_ANGLE_DEG <= launch_angle_deg <= MAX_LAUNCH_ANGLE_DEG
    ):
        reference = TOUR_LAUNCH_DEG.get(club, physics.optimal_launch_deg)
        ratio = math.sin(math.radians(launch_angle_deg)) / math.sin(math.radians(reference))
        spin *= _clamped(ratio**CLUB_SPIN_LAUNCH_EXPONENT, CLUB_SPIN_LAUNCH_LIMITS)

    return float(min(spin, MAX_SPIN_RPM))
