import { describe, expect, it } from 'vitest';
import type { Shot } from '../../types/shot';
import { buildClubDelivery, faceShare, selectAttackAngle, selectClubPath } from './clubDelivery';

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 112,
    club_speed_mph: 84,
    smash_factor: 1.33,
    estimated_carry_yards: 160,
    carry_range: [155, 165],
    club: '7-iron',
    timestamp: '2026-11-07T22:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 16.4,
    launch_angle_horizontal: 1.5,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: -4.0,
    club_path_deg: 2.5,
    spin_axis_deg: -1.0,
    spin_rpm: 6800,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    carry_spin_adjusted: 161,
    ...overrides,
  };
}

describe('selectClubPath', () => {
  it('uses a measured path as measured', () => {
    expect(selectClubPath(makeShot())).toEqual({
      value: 2.5,
      source: 'measured',
      note: undefined,
      confidence: null,
      confidenceLabel: undefined,
    });
  });

  it('marks a camera-fused path experimental with its confidence', () => {
    const path = selectClubPath(
      makeShot({
        club_path_deg: null,
        experimental_fused_status: 'fused',
        experimental_fused_club_path_deg: 3.1,
        experimental_fused_club_path_confidence: 'medium',
      })
    );

    expect(path).toMatchObject({ value: 3.1, source: 'experimental', note: 'camera fused', confidence: 'medium' });
    expect(path.confidenceLabel).toBe('experimental');
  });

  it('drops the radar-only estimate once camera fusion was attempted and rejected', () => {
    const path = selectClubPath(
      makeShot({
        club_path_deg: null,
        experimental_club_path_deg: 4.0,
        experimental_fused_status: 'rejected_lighting_quality',
      })
    );

    expect(path).toMatchObject({ value: null, source: 'experimental', note: 'rejected: lighting quality' });
  });

  it('uses the radar-only estimate when fusion never ran', () => {
    const path = selectClubPath(
      makeShot({ club_path_deg: null, experimental_club_path_deg: 1.8, experimental_club_path_status: null })
    );

    expect(path).toMatchObject({ value: 1.8, source: 'experimental', note: 'candidate', confidence: 'experimental' });
  });

  it('is empty, not experimental, when nothing measured it', () => {
    expect(selectClubPath(makeShot({ club_path_deg: null }))).toMatchObject({ value: null, source: 'measured' });
  });
});

describe('selectAttackAngle', () => {
  it('prefers the measured attack angle, then the fused one', () => {
    expect(selectAttackAngle(makeShot()).value).toBe(-4.0);
    const fused = selectAttackAngle(
      makeShot({
        club_angle_deg: null,
        experimental_fused_status: 'fused',
        experimental_fused_attack_angle_deg: -3.2,
        experimental_fused_attack_angle_confidence: 'high',
      })
    );
    expect(fused).toMatchObject({ value: -3.2, source: 'experimental', confidence: 'high' });
  });
});

describe('faceShare', () => {
  it.each([
    ['driver', 0.85],
    ['3-wood', 0.82],
    ['5-hybrid', 0.8],
    ['5-iron', 0.77],
    ['7-iron', 0.77],
    ['9-iron', 0.74],
    ['pw', 0.74],
    ['lw', 0.74],
    ['putter', 0.9],
    ['unknown', 0.78],
    ['', 0.78],
  ])('%s starts the ball %s on the face', (club, share) => {
    expect(faceShare(club)).toBe(share);
  });
});

describe('buildClubDelivery', () => {
  it('derives face, face to path, dynamic loft and spin loft from launch and delivery', () => {
    const delivery = buildClubDelivery(makeShot());

    expect(delivery.faceShare).toBe(0.77);
    expect(delivery.faceAngle.value).toBeCloseTo(1.2, 1);
    expect(delivery.faceToPath.value).toBeCloseTo(-1.3, 1);
    expect(delivery.dynamicLoft.value).toBeCloseTo(22.5, 1);
    expect(delivery.spinLoft.value).toBeCloseTo(26.5, 1);
    for (const angle of [delivery.faceAngle, delivery.faceToPath, delivery.dynamicLoft, delivery.spinLoft]) {
      expect(angle.source).toBe('calculated');
    }
  });

  it('puts the start direction back together from face and path', () => {
    const shot = makeShot({ club: 'driver', launch_angle_horizontal: -2.0, club_path_deg: 3.0 });
    const { faceAngle, faceShare: share } = buildClubDelivery(shot);

    expect(share * (faceAngle.value as number) + (1 - share) * 3.0).toBeCloseTo(-2.0, 9);
  });

  it('leaves calculated values empty when launch was only estimated', () => {
    const delivery = buildClubDelivery(makeShot({ angle_source: 'estimated' }));

    expect(delivery.clubPath.value).toBe(2.5);
    expect(delivery.attackAngle.value).toBe(-4.0);
    expect(delivery.faceAngle).toMatchObject({ value: null, note: 'needs measured launch' });
    expect(delivery.dynamicLoft).toMatchObject({ value: null, note: 'needs measured launch' });
    expect(delivery.spinLoft.value).toBeNull();
  });

  it('says which delivery input is missing', () => {
    const delivery = buildClubDelivery(makeShot({ club_path_deg: null, club_angle_deg: null }));

    expect(delivery.faceAngle).toMatchObject({ value: null, note: 'needs club path' });
    expect(delivery.faceToPath).toMatchObject({ value: null, note: 'needs club path' });
    expect(delivery.dynamicLoft).toMatchObject({ value: null, note: 'needs attack angle' });
  });

  it('handles a missing launch angle', () => {
    const delivery = buildClubDelivery(makeShot({ launch_angle_vertical: null, launch_angle_horizontal: null }));

    expect(delivery.faceAngle.value).toBeNull();
    expect(delivery.dynamicLoft.value).toBeNull();
  });
});
