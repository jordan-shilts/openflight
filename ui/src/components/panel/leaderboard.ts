import type { Profile } from '../../types/profile';
import type { Shot } from '../../types/shot';
import { computeStats, filterShotsByProfile, isSwingSpeedShot, statsCarryYards } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import { formatDistance, formatSpeed } from '../../utils/units';
import { NO_VALUE } from './liveMetrics';

export const LEADERBOARD_COLUMNS = ['avg_ball', 'max_ball', 'avg_carry', 'max_carry'] as const;

export type LeaderboardColumn = (typeof LEADERBOARD_COLUMNS)[number];

export interface LeaderboardCell {
  text: string;
  best: boolean;
}

export interface LeaderboardRow {
  profileId: string;
  name: string;
  shotCount: number;
  cells: Record<LeaderboardColumn, LeaderboardCell>;
}

/** Ball-strike shots only: swing-speed readings have no carry and would skew ball speed. */
export function leaderboardShots(shots: Shot[]): Shot[] {
  return shots.filter((shot) => !isSwingSpeedShot(shot));
}

function columnTexts(shots: Shot[], unitSystem: UnitSystem): Record<LeaderboardColumn, string> {
  if (shots.length === 0) {
    return { avg_ball: NO_VALUE, max_ball: NO_VALUE, avg_carry: NO_VALUE, max_carry: NO_VALUE };
  }

  const stats = computeStats(shots);
  // Same carry source as computeStats' average, so a row matches its Just me tiles.
  const carries = statsCarryYards(shots);

  return {
    avg_ball: formatSpeed(stats.avg_ball_speed, unitSystem, 1),
    max_ball: formatSpeed(stats.max_ball_speed, unitSystem, 1),
    avg_carry: carries.length > 0 ? formatDistance(stats.avg_carry_est, unitSystem, 0) : NO_VALUE,
    max_carry: carries.length > 0 ? formatDistance(Math.max(...carries), unitSystem, 0) : NO_VALUE,
  };
}

// Leaders compare the displayed (rounded, converted) values, so cells that read
// the same tie. Profiles without shots never lead.
export function buildLeaderboard(profiles: Profile[], shots: Shot[], unitSystem: UnitSystem): LeaderboardRow[] {
  const rows = profiles.map((profile) => {
    const profileShots = filterShotsByProfile(shots, profile.id);
    return { profile, shotCount: profileShots.length, texts: columnTexts(profileShots, unitSystem) };
  });

  const bestByColumn = {} as Record<LeaderboardColumn, number>;
  for (const column of LEADERBOARD_COLUMNS) {
    const values = rows
      .filter((row) => row.shotCount > 0)
      .map((row) => Number(row.texts[column]))
      .filter(Number.isFinite);
    bestByColumn[column] = values.length > 0 ? Math.max(...values) : Number.NaN;
  }

  return rows.map(({ profile, shotCount, texts }) => {
    const cells = {} as Record<LeaderboardColumn, LeaderboardCell>;
    for (const column of LEADERBOARD_COLUMNS) {
      cells[column] = {
        text: texts[column],
        best: shotCount > 0 && Number(texts[column]) === bestByColumn[column],
      };
    }
    return { profileId: profile.id, name: profile.name, shotCount, cells };
  });
}
