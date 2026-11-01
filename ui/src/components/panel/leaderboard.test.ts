import { describe, expect, it } from 'vitest';
import type { Profile } from '../../types/profile';
import type { Shot } from '../../types/shot';
import { buildLeaderboard, LEADERBOARD_COLUMNS, leaderboardShots, type LeaderboardRow } from './leaderboard';
import { NO_VALUE } from './liveMetrics';

function profile(id: string, name: string): Profile {
  return { id, name, created_at: '2026-11-01T10:00:00Z', settings: {} };
}

function shot(profileId: string, ballSpeed: number, carry: number, overrides: Partial<Shot> = {}): Shot {
  return {
    profile_id: profileId,
    ball_speed_mph: ballSpeed,
    club_speed_mph: null,
    smash_factor: null,
    estimated_carry_yards: carry,
    carry_range: [carry - 5, carry + 5],
    club: 'driver',
    timestamp: `${profileId}-${ballSpeed}-${carry}`,
    peak_magnitude: null,
    launch_angle_vertical: null,
    launch_angle_horizontal: null,
    launch_angle_confidence: null,
    angle_source: null,
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_rpm: null,
    spin_confidence: null,
    spin_quality: null,
    spin_source: null,
    carry_spin_adjusted: null,
    ...overrides,
  };
}

const roster = [profile('jor', 'Jordan'), profile('alx', 'Alex'), profile('gst', 'Guest')];

function texts(row: LeaderboardRow) {
  return LEADERBOARD_COLUMNS.map((column) => row.cells[column].text);
}

function best(row: LeaderboardRow) {
  return LEADERBOARD_COLUMNS.filter((column) => row.cells[column].best);
}

describe('buildLeaderboard', () => {
  it('returns one row per profile in roster order, including profiles with no shots', () => {
    const rows = buildLeaderboard(roster, [shot('alx', 140, 230)], 'imperial');

    expect(rows.map((row) => row.name)).toEqual(['Jordan', 'Alex', 'Guest']);
    expect(rows.map((row) => row.profileId)).toEqual(['jor', 'alx', 'gst']);
  });

  it('computes shots, average and best ball speed, average and longest carry per profile', () => {
    const rows = buildLeaderboard(
      roster,
      [shot('jor', 150, 260), shot('jor', 140, 240), shot('jor', 146, 250), shot('alx', 130, 210)],
      'imperial'
    );

    expect(rows[0].shotCount).toBe(3);
    expect(texts(rows[0])).toEqual(['145.3', '150.0', '250', '260']);
    expect(rows[1].shotCount).toBe(1);
    expect(texts(rows[1])).toEqual(['130.0', '130.0', '210', '210']);
  });

  it('shows dashes and zero shots for a profile with none', () => {
    const [, , guest] = buildLeaderboard(roster, [shot('jor', 150, 260)], 'imperial');

    expect(guest.shotCount).toBe(0);
    expect(texts(guest)).toEqual([NO_VALUE, NO_VALUE, NO_VALUE, NO_VALUE]);
    expect(best(guest)).toEqual([]);
  });

  it('marks the best value in each column, which can belong to different profiles', () => {
    const rows = buildLeaderboard(
      roster,
      [shot('jor', 160, 280), shot('jor', 120, 200), shot('alx', 150, 250), shot('alx', 148, 252)],
      'imperial'
    );

    expect(best(rows[0])).toEqual(['max_ball', 'max_carry']);
    expect(best(rows[1])).toEqual(['avg_ball', 'avg_carry']);
    expect(best(rows[2])).toEqual([]);
  });

  it('highlights every profile tied on a column', () => {
    const rows = buildLeaderboard(roster, [shot('jor', 150, 250), shot('alx', 150, 250)], 'imperial');

    expect(best(rows[0])).toEqual([...LEADERBOARD_COLUMNS]);
    expect(best(rows[1])).toEqual([...LEADERBOARD_COLUMNS]);
  });

  it('ties values that only differ below the displayed precision', () => {
    // 150.04 and 150.01 both read 150.0; highlighting one would look arbitrary.
    const rows = buildLeaderboard(roster, [shot('jor', 150.04, 250.2), shot('alx', 150.01, 249.9)], 'imperial');

    expect(texts(rows[0]).slice(0, 2)).toEqual(['150.0', '150.0']);
    expect(texts(rows[1]).slice(0, 2)).toEqual(['150.0', '150.0']);
    expect(rows[0].cells.max_ball.best).toBe(true);
    expect(rows[1].cells.max_ball.best).toBe(true);
    expect(rows[0].cells.max_carry.best).toBe(true);
    expect(rows[1].cells.max_carry.best).toBe(true);
  });

  it('still highlights a lone profile with shots', () => {
    const rows = buildLeaderboard(roster, [shot('alx', 120, 190)], 'imperial');

    expect(best(rows[1])).toEqual([...LEADERBOARD_COLUMNS]);
    expect(best(rows[0])).toEqual([]);
  });

  it('highlights nothing when nobody has shots', () => {
    const rows = buildLeaderboard(roster, [], 'imperial');

    expect(rows.every((row) => best(row).length === 0)).toBe(true);
    expect(rows.every((row) => row.shotCount === 0)).toBe(true);
  });

  it('formats in the display unit', () => {
    const [jordan] = buildLeaderboard(roster, [shot('jor', 100, 100)], 'metric');

    expect(texts(jordan)).toEqual(['160.9', '160.9', '91', '91']);
  });

  it('ignores shots stamped with a profile that is not on the roster', () => {
    const rows = buildLeaderboard(roster, [shot('gone', 170, 300), shot('jor', 140, 240)], 'imperial');

    expect(rows).toHaveLength(3);
    expect(rows[0].cells.max_ball).toEqual({ text: '140.0', best: true });
  });
});

describe('leaderboardShots', () => {
  it('drops swing-speed readings and keeps ball strikes', () => {
    const ball = shot('jor', 150, 250);
    const swing = shot('jor', 95, 0, { mode: 'swing-speed', club: 'Swing Speed' });
    const legacySwing = shot('jor', 90, 0, { club: 'Swing Speed' });

    expect(leaderboardShots([ball, swing, legacySwing])).toEqual([ball]);
  });
});
