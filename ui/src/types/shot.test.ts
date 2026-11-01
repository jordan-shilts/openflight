import { describe, expect, it } from 'vitest';
import type { Shot } from './shot';
import { countShotsByProfile, filterShotsByProfile, excludeShotsByProfile, getDisplayCarryYards } from './shot';

describe('getDisplayCarryYards', () => {
  it('prefers the committed carry and falls back to the speed estimate', () => {
    expect(getDisplayCarryYards({ carry_spin_adjusted: 143, estimated_carry_yards: 151 } as Shot)).toBe(143);
    expect(getDisplayCarryYards({ carry_spin_adjusted: null, estimated_carry_yards: 151 } as Shot)).toBe(151);
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
