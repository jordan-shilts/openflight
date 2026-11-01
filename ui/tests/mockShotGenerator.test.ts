import { describe, expect, it } from 'vitest';
import { generateShot, puttRollFeet } from '../mock-server/shotGenerator';

const profile = { profileId: 'james', profileName: 'James' };

describe('mock-server putts', () => {
  it("rolls Open-Birdie's 4.5 mph reference putt about 11 ft", () => {
    expect(puttRollFeet(4.5)).toBeCloseTo(11.06, 2);
  });

  it('generates slow, flat putts with a roll and no carry when the putter is selected', () => {
    const shots = Array.from({ length: 200 }, () => generateShot({ club: 'putter', ...profile }));

    for (const shot of shots) {
      expect(shot.club).toBe('putter');
      expect(shot.ball_speed_mph).toBeGreaterThanOrEqual(1.5);
      expect(shot.ball_speed_mph).toBeLessThan(20);
      expect(shot.launch_angle_vertical).toBeGreaterThanOrEqual(0.5);
      expect(shot.launch_angle_vertical).toBeLessThan(2);
      expect(shot.spin_rpm).toBeLessThan(300);
      expect(shot.carry_spin_adjusted).toBe(0);
      expect(shot.roll_distance_ft).toBeCloseTo(puttRollFeet(shot.ball_speed_mph), 0);
    }
  });

  it('keeps full swings on the full-swing clamps with no roll', () => {
    const shot = generateShot({ club: 'lw', ...profile });

    expect(shot.ball_speed_mph).toBeGreaterThanOrEqual(50);
    expect(shot.launch_angle_vertical).toBeGreaterThanOrEqual(5);
    expect(shot.roll_distance_ft).toBeNull();
  });
});
