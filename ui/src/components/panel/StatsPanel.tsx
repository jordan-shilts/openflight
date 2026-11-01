import { useMemo, useRef, useState, type ReactNode } from 'react';
import type { Profile } from '../../types/profile';
import type { Shot } from '../../types/shot';
import {
  computeStats,
  computeSwingSpeedStats,
  filterShotsByProfile,
  getUniqueClubs,
  isPuttShot,
  isSwingSpeedShot,
} from '../../types/shot';
import { useDragScroll } from '../../hooks/useDragScroll';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useI18n, type MessageKey } from '../../i18n/useI18n';
import {
  formatDistance,
  formatRollDistance,
  formatSpeed,
  getDistanceUnit,
  getRollDistanceUnit,
  getSpeedUnit,
} from '../../utils/units';
import { MetricCard } from '../ui/MetricCard';
import { buildLeaderboard, LEADERBOARD_COLUMNS, leaderboardShots, type LeaderboardColumn } from './leaderboard';
import { PanelAction } from './PanelAction';
import { PanelHeader } from './PanelHeader';

export type StatsView = 'me' | 'leaderboard';

interface StatsPanelProps {
  shots: Shot[];
  activeClub: string;
  profileId: string;
  profileName: string;
  profiles: Profile[];
  view: StatsView;
  onChangeView: (view: StatsView) => void;
  /** Pinned header control, e.g. Clear session. */
  headerAction?: ReactNode;
}

const LEADERBOARD_LABEL_KEYS: Record<LeaderboardColumn, MessageKey> = {
  avg_ball: 'stats.avgBall',
  max_ball: 'stats.maxBall',
  avg_carry: 'stats.avgCarry',
  max_carry: 'stats.longestCarry',
};

interface StatTile {
  id: string;
  label: string;
  value: string;
  unit?: string;
}

/**
 * Design doc 7a: session summary as a hairline tile grid, with the per-club
 * filter chips above the tiles. Six tiles for a ball-strike session (3x2 as
 * drawn), four for a swing-speed one.
 */
export function StatsPanel({
  shots,
  activeClub,
  profileId,
  profileName,
  profiles,
  view,
  onChangeView,
  headerAction,
}: StatsPanelProps) {
  const { t } = useI18n();
  const canCompare = profiles.length > 1;
  const showLeaderboard = canCompare && view === 'leaderboard';
  const profileShots = useMemo(() => filterShotsByProfile(shots, profileId), [shots, profileId]);
  const comparableShots = useMemo(() => leaderboardShots(shots), [shots]);
  const scopeShots = showLeaderboard ? comparableShots : profileShots;
  const hasShotsForActiveClub = scopeShots.some((shot) => shot.club === activeClub);
  const [selectedClub, setSelectedClub] = useState<string | null>(hasShotsForActiveClub ? activeClub : null);
  const [prevActiveClub, setPrevActiveClub] = useState(activeClub);
  const [prevProfileId, setPrevProfileId] = useState(profileId);
  const [prevShowLeaderboard, setPrevShowLeaderboard] = useState(showLeaderboard);
  const chipRef = useRef<HTMLDivElement>(null);
  const chipScroll = useDragScroll(chipRef, 'x');
  const tableRef = useRef<HTMLDivElement>(null);
  const tableScroll = useDragScroll(tableRef);

  // Update state during render when the prop changes, rather than in an effect.
  if (activeClub !== prevActiveClub || profileId !== prevProfileId) {
    setPrevActiveClub(activeClub);
    setPrevProfileId(profileId);
    setSelectedClub(hasShotsForActiveClub ? activeClub : null);
  }
  if (showLeaderboard !== prevShowLeaderboard) {
    setPrevShowLeaderboard(showLeaderboard);
    if (selectedClub !== null && !scopeShots.some((shot) => shot.club === selectedClub)) {
      setSelectedClub(null);
    }
  }

  const { unitSystem } = useUnitPreference();
  const speedUnit = getSpeedUnit(unitSystem);
  const distanceUnit = getDistanceUnit(unitSystem);

  const availableClubs = useMemo(() => getUniqueClubs(scopeShots), [scopeShots]);
  const clubCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const shot of scopeShots) {
      counts[shot.club] = (counts[shot.club] ?? 0) + 1;
    }
    return counts;
  }, [scopeShots]);

  const filteredShots = useMemo(
    () => (selectedClub === null ? scopeShots : scopeShots.filter((shot) => shot.club === selectedClub)),
    [scopeShots, selectedClub]
  );
  const leaderboard = useMemo(
    () => (showLeaderboard ? buildLeaderboard(profiles, filteredShots, unitSystem) : []),
    [showLeaderboard, profiles, filteredShots, unitSystem]
  );

  const stats = useMemo(() => computeStats(filteredShots), [filteredShots]);
  const swingStats = useMemo(() => computeSwingSpeedStats(filteredShots), [filteredShots]);
  const isSwingSpeedSession = filteredShots.length > 0 && filteredShots.every(isSwingSpeedShot);
  const isPuttingOnly = filteredShots.length > 0 && filteredShots.every(isPuttShot);

  const tiles: StatTile[] = useMemo(() => {
    if (isSwingSpeedSession) {
      return [
        { id: 'count', label: t('metric.swings'), value: String(swingStats.count) },
        {
          id: 'last',
          label: t('stats.last'),
          value: formatSpeed(swingStats.last_speed_mph, unitSystem, 1),
          unit: speedUnit,
        },
        {
          id: 'best',
          label: t('metric.best'),
          value: formatSpeed(swingStats.best_speed_mph, unitSystem, 1),
          unit: speedUnit,
        },
        {
          id: 'avg',
          label: t('metric.average'),
          value: formatSpeed(swingStats.avg_speed_mph, unitSystem, 1),
          unit: speedUnit,
        },
      ];
    }

    return [
      { id: 'count', label: t('stats.shots'), value: String(stats.shot_count) },
      {
        id: 'avg_ball',
        label: t('stats.avgBall'),
        value: formatSpeed(stats.avg_ball_speed, unitSystem, 1),
        unit: speedUnit,
      },
      {
        id: 'max_ball',
        label: t('stats.maxBall'),
        value: formatSpeed(stats.max_ball_speed, unitSystem, 1),
        unit: speedUnit,
      },
      isPuttingOnly
        ? {
            id: 'avg_roll',
            label: t('stats.avgRoll'),
            value: formatRollDistance(stats.avg_roll_ft ?? 0, unitSystem),
            unit: getRollDistanceUnit(unitSystem),
          }
        : {
            id: 'avg_carry',
            label: t('stats.avgCarry'),
            value: formatDistance(stats.avg_carry_est, unitSystem, 0),
            unit: distanceUnit,
          },
      {
        id: 'avg_club',
        label: t('stats.avgClub'),
        value: stats.avg_club_speed === null ? '—' : formatSpeed(stats.avg_club_speed, unitSystem, 1),
        unit: stats.avg_club_speed === null ? undefined : speedUnit,
      },
      {
        id: 'avg_smash',
        label: t('stats.avgSmash'),
        value: stats.avg_smash_factor === null ? '—' : stats.avg_smash_factor.toFixed(2),
      },
    ];
  }, [isSwingSpeedSession, isPuttingOnly, stats, swingStats, unitSystem, speedUnit, distanceUnit, t]);

  const clubFilters = (
    <div className="stats-panel__chips" role="group" aria-label={t('stats.filterByClub')}>
      <button
        type="button"
        className={`panel-chip${selectedClub === null ? ' panel-chip--active' : ''}`}
        aria-pressed={selectedClub === null}
        onClick={() => setSelectedClub(null)}
      >
        {t('stats.all', { count: scopeShots.length })}
      </button>
      <div
        className="panel-chips stats-panel__chip-scroll"
        ref={chipRef}
        onPointerDown={chipScroll.onPointerDown}
        onPointerMove={chipScroll.onPointerMove}
        onPointerUp={chipScroll.onPointerUp}
        onPointerCancel={chipScroll.onPointerCancel}
        onClickCapture={chipScroll.onClickCapture}
      >
        {availableClubs.map((club) => (
          <button
            key={club}
            type="button"
            className={`panel-chip${selectedClub === club ? ' panel-chip--active' : ''}`}
            aria-pressed={selectedClub === club}
            onClick={() => setSelectedClub(club)}
          >
            {club.toUpperCase()} ({clubCounts[club] ?? 0})
          </button>
        ))}
      </div>
    </div>
  );

  const viewToggle = canCompare ? (
    <PanelAction variant="secondary" onClick={() => onChangeView(showLeaderboard ? 'me' : 'leaderboard')}>
      {showLeaderboard ? t('stats.justMe') : t('stats.leaderboard')}
    </PanelAction>
  ) : null;

  const leaderboardTable = (
    <div
      className="stats-leaderboard"
      ref={tableRef}
      onPointerDown={tableScroll.onPointerDown}
      onPointerMove={tableScroll.onPointerMove}
      onPointerUp={tableScroll.onPointerUp}
      onPointerCancel={tableScroll.onPointerCancel}
      onClickCapture={tableScroll.onClickCapture}
    >
      <table className="stats-leaderboard__table">
        <thead>
          <tr>
            <th scope="col">{t('shots.colProfile')}</th>
            <th scope="col" className="stats-leaderboard__num">
              {t('stats.shots')}
            </th>
            {LEADERBOARD_COLUMNS.map((column) => (
              <th key={column} scope="col" className="stats-leaderboard__num">
                {t(LEADERBOARD_LABEL_KEYS[column])}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {leaderboard.map((row) => {
            const classes = ['stats-leaderboard__row'];
            if (row.profileId === profileId) classes.push('stats-leaderboard__row--active');
            if (row.shotCount === 0) classes.push('stats-leaderboard__row--empty');

            return (
              <tr
                key={row.profileId}
                className={classes.join(' ')}
                aria-current={row.profileId === profileId ? 'true' : undefined}
              >
                <th scope="row" className="stats-leaderboard__name">
                  {row.name}
                </th>
                <td className="stats-leaderboard__num">{row.shotCount}</td>
                {LEADERBOARD_COLUMNS.map((column) => (
                  <td
                    key={column}
                    className={`stats-leaderboard__num${row.cells[column].best ? ' stats-leaderboard__best' : ''}`}
                  >
                    {row.cells[column].text}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );

  return (
    <div className="panel">
      <PanelHeader
        title={t('nav.stats')}
        subtitle={showLeaderboard ? t('stats.leaderboard') : profileName}
        actions={
          viewToggle || headerAction ? (
            <>
              {viewToggle}
              {headerAction}
            </>
          ) : undefined
        }
      />
      <div className="panel__body stats-panel">
        {scopeShots.length > 0 ? clubFilters : null}
        {scopeShots.length === 0 ? (
          <div className="panel__body--empty">
            <span className="panel__empty-title">{t('stats.noShots')}</span>
            <span className="panel__empty-detail">{t('stats.noShotsDetail')}</span>
          </div>
        ) : showLeaderboard ? (
          leaderboardTable
        ) : (
          <div className={`stats-panel__grid stats-panel__grid--of-${tiles.length}`}>
            {tiles.map((tile) => (
              <MetricCard key={tile.id} label={tile.label} value={tile.value} unit={tile.unit} labelPosition="above" />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
