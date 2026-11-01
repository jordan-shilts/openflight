import { describe, expect, it } from 'vitest';
import type { Shot } from './shot';
import {
  computeStats,
  countShotsByProfile,
  filterShotsByProfile,
  excludeShotsByProfile,
  getDisplayCarryYards,
  isPuttShot,
  statsCarryYards,
} from './shot';

describe('getDisplayCarryYards', () => {
  it('prefers the committed carry and falls back to the speed estimate', () => {
    expect(getDisplayCarryYards({ carry_spin_adjusted: 143, estimated_carry_yards: 151 } as Shot)).toBe(143);
    expect(getDisplayCarryYards({ carry_spin_adjusted: null, estimated_carry_yards: 151 } as Shot)).toBe(151);
  });
});

describe('computeStats', () => {
  const carryShot = (committed: number | null, estimate: number): Shot =>
    ({
      ball_speed_mph: 100,
      club_speed_mph: null,
      smash_factor: null,
      carry_spin_adjusted: committed,
      estimated_carry_yards: estimate,
    }) as Shot;

  const putt = (rollFt: number): Shot => ({ ...carryShot(0, 8), club: 'putter', roll_distance_ft: rollFt }) as Shot;

  it('averages the carry Live shows, committed carry first', () => {
    const stats = computeStats([carryShot(143, 119), carryShot(null, 151)]);

    expect(stats.avg_carry_est).toBe(147);
  });

  it('leaves putts out of the carry average and averages their roll instead', () => {
    const stats = computeStats([carryShot(143, 119), putt(10), putt(20)]);

    expect(stats.avg_carry_est).toBe(143);
    expect(stats.avg_roll_ft).toBe(15);
    expect(stats.shot_count).toBe(3);
  });

  it('reports no carry and only a roll for a putting-only set', () => {
    const stats = computeStats([putt(12)]);

    expect(stats.avg_carry_est).toBe(0);
    expect(stats.avg_roll_ft).toBe(12);
    expect(computeStats([carryShot(143, 119)]).avg_roll_ft).toBeNull();
  });
});

describe('isPuttShot', () => {
  it('is true only for shots the server gave a roll', () => {
    expect(isPuttShot({ roll_distance_ft: 11.1 } as Shot)).toBe(true);
    expect(isPuttShot({ roll_distance_ft: 0 } as Shot)).toBe(true);
    expect(isPuttShot({ roll_distance_ft: null } as Shot)).toBe(false);
    expect(isPuttShot({} as Shot)).toBe(false);
  });

  it('drops putts from the carries behind carry stats', () => {
    const drive = { carry_spin_adjusted: 250, estimated_carry_yards: 240 } as Shot;
    const putted = { carry_spin_adjusted: 0, estimated_carry_yards: 8, roll_distance_ft: 9 } as Shot;

    expect(statsCarryYards([drive, putted])).toEqual([250]);
  });
});

describe('filterShotsByProfile', () => {
  const shotWith = (profileId: string | undefined): Shot => ({ profile_id: profileId, ball_speed_mph: 100 }) as Shot;

  it('keeps only shots stamped with the given profile id', () => {
    const shots = [shotWith('aaa'), shotWith('bbb'), shotWith('aaa')];

    expect(filterShotsByProfile(shots, 'aaa')).toHaveLength(2);
  });

  it('matches exactly, without folding case', () => {
    const shots = [shotWith('AAA'), shotWith('aaa')];

    expect(filterShotsByProfile(shots, 'aaa')).toEqual([shots[1]]);
  });

  it('excludes unstamped shots from every profile', () => {
    const shots = [shotWith(undefined), shotWith('')];

    expect(filterShotsByProfile(shots, 'aaa')).toEqual([]);
    expect(filterShotsByProfile(shots, 'bbb')).toEqual([]);
  });

  it('returns nothing for a blank profile id', () => {
    const shots = [shotWith('aaa'), shotWith(undefined)];

    expect(filterShotsByProfile(shots, '')).toEqual([]);
  });
});

describe('excludeShotsByProfile', () => {
  const shotWith = (profileId: string | undefined): Shot => ({ profile_id: profileId, ball_speed_mph: 100 }) as Shot;

  it('drops only the given profile and keeps unstamped shots', () => {
    const shots = [shotWith('aaa'), shotWith('bbb'), shotWith(undefined)];

    expect(excludeShotsByProfile(shots, 'aaa')).toEqual([shots[1], shots[2]]);
  });

  it('excludes nothing for a blank profile id', () => {
    const shots = [shotWith('aaa'), shotWith('bbb')];

    expect(excludeShotsByProfile(shots, '')).toEqual(shots);
  });
});

describe('countShotsByProfile', () => {
  const shotWith = (profileId: string | undefined): Shot => ({ profile_id: profileId, ball_speed_mph: 100 }) as Shot;

  it('counts shots per profile id', () => {
    const shots = [shotWith('aaa'), shotWith('bbb'), shotWith('aaa')];

    expect(countShotsByProfile(shots)).toEqual({ aaa: 2, bbb: 1 });
  });

  it('skips unstamped shots and returns an empty map for no shots', () => {
    expect(countShotsByProfile([shotWith(undefined), shotWith('')])).toEqual({});
    expect(countShotsByProfile([])).toEqual({});
  });
});
