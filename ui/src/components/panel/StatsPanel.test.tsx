import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';
import type { Profile } from '../../types/profile';
import type { Shot } from '../../types/shot';
import { StatsPanel, type StatsView } from './StatsPanel';

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 90,
    club_speed_mph: 67,
    smash_factor: 1.34,
    estimated_carry_yards: 200,
    carry_range: [195, 205],
    club: 'driver',
    profile_id: 'james',
    profile_name: 'James',
    timestamp: '2026-08-19T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 13,
    launch_angle_horizontal: 0,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_rpm: 2600,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    spin_method: null,
    carry_spin_adjusted: null,
    ...overrides,
  };
}

function profile(id: string, name: string): Profile {
  return { id, name, created_at: '2026-11-01T10:00:00Z', settings: {} };
}

const SOLO = [profile('james', 'James')];
const GROUP = [profile('james', 'James'), profile('alex', 'Alex'), profile('guest', 'Guest')];

const render = (
  shots: Shot[],
  activeClub = 'driver',
  headerAction?: ReactNode,
  profiles: Profile[] = SOLO,
  view: StatsView = 'me'
) =>
  text(
    renderToString(
      <StatsPanel
        shots={shots}
        activeClub={activeClub}
        profileId="james"
        profileName="James"
        profiles={profiles}
        view={view}
        onChangeView={() => {}}
        headerAction={headerAction}
      />
    )
  );

const renderLeaderboard = (shots: Shot[], activeClub = 'driver', profiles: Profile[] = GROUP) =>
  render(shots, activeClub, undefined, profiles, 'leaderboard');

function leaderboardRow(html: string, name: string): string {
  const rows = [...html.matchAll(/<tr class="stats-leaderboard__row[\s\S]*?<\/tr>/g)].map((match) => match[0]);
  return rows.find((row) => row.includes(`>${name}<`)) ?? '';
}

function header(html: string): string {
  return html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';
}

describe('StatsPanel', () => {
  it('shows an empty state before any shots', () => {
    const html = render([]);

    expect(html).toContain('No shots yet');
    expect(html).not.toContain('stats-panel__grid');
    expect(html).not.toContain('All (0)');
    expect(html).not.toContain('Filter by club');
  });

  it('renders six tiles for a ball-strike session', () => {
    const html = render([makeShot(), makeShot({ ball_speed_mph: 96, timestamp: 'b' })]);

    expect(html).toContain('stats-panel__grid--of-6');
    for (const label of ['Shots', 'Avg ball', 'Max ball', 'Avg carry', 'Avg club', 'Avg smash']) {
      expect(html).toContain(`>${label}<`);
    }
    expect(html).toContain('metric-card__value">93.0<'); // avg of 90 and 96
    expect(html).toContain('metric-card__value">96.0<'); // max
  });

  it('averages the carry Live shows, committed carry first', () => {
    const html = render([
      makeShot({ estimated_carry_yards: 119, carry_spin_adjusted: 143, timestamp: 'a' }),
      makeShot({ estimated_carry_yards: 151, carry_spin_adjusted: null, timestamp: 'b' }),
    ]);

    expect(html).toContain('metric-card__value">147<');
  });

  it('keeps the tile count stable when club speed is missing', () => {
    // Six tiles either way, so the 3x2 grid never reflows.
    const html = render([makeShot({ club_speed_mph: null, smash_factor: null })]);

    expect(html).toContain('stats-panel__grid--of-6');
    expect(html).toContain('>Avg club<');
    expect(html).toContain('metric-card__value">—<');
  });

  it('renders four tiles for a swing-speed session', () => {
    const swing = makeShot({ mode: 'swing-speed', club: 'Swing Speed' });
    const html = render([swing], 'Swing Speed');

    expect(html).toContain('stats-panel__grid--of-4');
    for (const label of ['Swings', 'Last', 'Best', 'Average']) {
      expect(html).toContain(`>${label}<`);
    }
  });

  it('offers an All chip plus one per club, with counts', () => {
    const html = render([makeShot(), makeShot({ club: '7-iron', timestamp: 'b' })]);

    expect(html).toContain('All (2)');
    expect(html).toContain('DRIVER (1)');
    expect(html).toContain('7-IRON (1)');
  });

  it('puts club filters above the metric tiles, not in the header', () => {
    // Extra clubs used to live in panel-header__actions and clip the title.
    const html = render([
      makeShot(),
      makeShot({ club: '3-wood', timestamp: 'b' }),
      makeShot({ club: '5-iron', timestamp: 'c' }),
      makeShot({ club: 'pw', timestamp: 'd' }),
    ]);
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';

    expect(header).not.toContain('Filter by club');
    expect(html).toContain('stats-panel__chips');
    expect(html.indexOf('stats-panel__chips')).toBeLessThan(html.indexOf('stats-panel__grid'));
    expect(html).toContain('PW (1)');
  });

  it('places Clear session in the header', () => {
    const html = render(
      [],
      'driver',
      <button type="button" className="panel-action">
        Clear session
      </button>
    );
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';

    expect(header).toContain('Clear session');
  });

  it('keeps All fixed while the club chips scroll', () => {
    const html = render([
      makeShot(),
      makeShot({ club: '3-wood', timestamp: 'b' }),
      makeShot({ club: 'pw', timestamp: 'c' }),
    ]);
    const scroller = html.match(/stats-panel__chip-scroll[\s\S]*?<\/div>/)?.[0] ?? '';

    expect(html.indexOf('All (3)')).toBeLessThan(html.indexOf('stats-panel__chip-scroll'));
    expect(scroller).not.toContain('All (');
    expect(scroller).toContain('DRIVER (1)');
    expect(scroller).toContain('PW (1)');
  });

  it('preselects the active club when it has shots', () => {
    const html = render([makeShot(), makeShot({ club: '7-iron', timestamp: 'b' })], '7-iron');
    const activeChip = html.match(/<button[^>]*panel-chip--active[^>]*>([^<]*)</)?.[1];

    expect(activeChip).toBe('7-IRON (1)');
    // Stats reflect the filter, not the whole session.
    expect(html).toContain('metric-card__value">1<');
  });

  it('falls back to All when the active club has no shots yet', () => {
    const html = render([makeShot()], 'sw');
    const activeChip = html.match(/<button[^>]*panel-chip--active[^>]*>([^<]*)</)?.[1];

    expect(activeChip).toBe('All (1)');
  });

  it('computes averages and club chips from the current profile only', () => {
    const html = render([
      makeShot({ ball_speed_mph: 90, timestamp: 'a' }),
      makeShot({ profile_id: 'alex', profile_name: 'Alex', ball_speed_mph: 150, club: '7-iron', timestamp: 'b' }),
    ]);

    expect(html).toContain('All (1)');
    expect(html).toContain('DRIVER (1)');
    expect(html).not.toContain('7-IRON');
    expect(html).toContain('metric-card__value">1<');
    expect(html).toContain('metric-card__value">90.0<');
    expect(html).not.toContain('metric-card__value">150.0<');
    expect(html).not.toContain('metric-card__value">120.0<');
  });

  it('shows the empty state when only other profiles have shots', () => {
    const html = render([makeShot({ profile_id: 'alex', profile_name: 'Alex' })]);

    expect(html).toContain('No shots yet');
    expect(html).not.toContain('stats-panel__grid');
    expect(html).not.toContain('All (0)');
    expect(html).not.toContain('Filter by club');
  });

  it('paints chips and tiles on the same surface', () => {
    // Light theme made this look like a cream chip gutter over a white spreadsheet.
    expect(css).toMatch(/\.stats-panel \{[^}]*background: var\(--color-surface\)/);
    expect(css).not.toMatch(/\.stats-panel__grid \.metric-card--label-above \{[^}]*align-content: center/);
  });

  describe('leaderboard', () => {
    const groupShots = [
      makeShot({ ball_speed_mph: 140, estimated_carry_yards: 240, timestamp: 'j1' }),
      makeShot({ ball_speed_mph: 150, estimated_carry_yards: 262, timestamp: 'j2' }),
      makeShot({ ball_speed_mph: 120, estimated_carry_yards: 160, club: '7-iron', timestamp: 'j3' }),
      makeShot({
        profile_id: 'alex',
        profile_name: 'Alex',
        ball_speed_mph: 147,
        estimated_carry_yards: 255,
        timestamp: 'a1',
      }),
      makeShot({
        profile_id: 'alex',
        profile_name: 'Alex',
        ball_speed_mph: 128,
        estimated_carry_yards: 175,
        club: '7-iron',
        timestamp: 'a2',
      }),
    ];

    it('offers no toggle to a single profile, so solo Stats is unchanged', () => {
      const html = render(groupShots, 'driver', undefined, SOLO);

      expect(html).not.toContain('>Leaderboard<');
      expect(html).not.toContain('>Just me<');
      expect(header(html)).toContain('panel-header__subtitle">James<');
    });

    it('stays on Just me for a single profile even if the leaderboard was chosen earlier', () => {
      const html = render(groupShots, 'driver', undefined, SOLO, 'leaderboard');

      expect(html).not.toContain('stats-leaderboard');
      expect(html).toContain('stats-panel__grid--of-6');
    });

    it('offers the leaderboard from Just me once two profiles exist', () => {
      const html = render(groupShots, 'driver', undefined, GROUP);

      expect(header(html)).toContain('>Leaderboard<');
      expect(html).toContain('stats-panel__grid--of-6');
      expect(html).not.toContain('stats-leaderboard');
    });

    it('keeps Clear session next to the toggle', () => {
      const html = render(
        groupShots,
        'driver',
        <button type="button" className="panel-action">
          Clear session
        </button>,
        GROUP
      );

      expect(header(html).indexOf('>Leaderboard<')).toBeLessThan(header(html).indexOf('Clear session'));
    });

    it('renders one row per profile with the five columns, titled Leaderboard, toggling back to Just me', () => {
      const html = renderLeaderboard(groupShots);
      const headings = [...html.matchAll(/<th scope="col"[^>]*>([^<]+)</g)].map((match) => match[1]);

      expect(header(html)).toContain('panel-header__subtitle">Leaderboard<');
      expect(header(html)).toContain('>Just me<');
      expect(headings).toEqual(['Profile', 'Shots', 'Avg ball', 'Max ball', 'Avg carry', 'Longest']);
      expect(html.match(/<tr class="stats-leaderboard__row/g)).toHaveLength(3);
      expect(html).not.toContain('stats-panel__grid');
    });

    it('filters by the preselected club chip', () => {
      const html = renderLeaderboard(groupShots, 'driver');
      const james = leaderboardRow(html, 'James');
      const alex = leaderboardRow(html, 'Alex');

      expect(html).toMatch(/panel-chip--active[^>]*>DRIVER \(3\)</);
      expect(james).toMatch(/stats-leaderboard__num">2<\/td>/);
      expect(james).toContain('>145.0<');
      expect(james).toContain('>150.0<');
      expect(james).toContain('>251<');
      expect(james).toContain('>262<');
      expect(alex).toMatch(/stats-leaderboard__num">1<\/td>/);
      expect(alex).toContain('>147.0<');
    });

    it('lists clubs from every profile, so a club only someone else hit can be compared', () => {
      const html = renderLeaderboard([groupShots[0], groupShots[4]], '7-iron');

      expect(html).toContain('All (2)');
      expect(html).toMatch(/panel-chip--active[^>]*>7-IRON \(1\)</);
      expect(leaderboardRow(html, 'Alex')).toContain('>128.0<');
      expect(leaderboardRow(html, 'James')).toMatch(/stats-leaderboard__num">0<\/td>/);
    });

    it('highlights the best value per column in the accent class', () => {
      const html = renderLeaderboard(groupShots, 'driver');
      const bestCells = (row: string) =>
        [...row.matchAll(/stats-leaderboard__best">([^<]+)</g)].map((match) => match[1]);

      expect(bestCells(leaderboardRow(html, 'James'))).toEqual(['150.0', '262']);
      expect(bestCells(leaderboardRow(html, 'Alex'))).toEqual(['147.0', '255']);
      expect(bestCells(leaderboardRow(html, 'Guest'))).toEqual([]);
    });

    it('marks the active profile and dims profiles without shots', () => {
      const html = renderLeaderboard(groupShots);

      expect(leaderboardRow(html, 'James')).toContain('stats-leaderboard__row--active');
      expect(leaderboardRow(html, 'James')).toContain('aria-current="true"');
      expect(leaderboardRow(html, 'Alex')).not.toContain('stats-leaderboard__row--active');
      expect(leaderboardRow(html, 'Guest')).toContain('stats-leaderboard__row--empty');
      expect(leaderboardRow(html, 'Guest')).toContain('>—<');
      expect(leaderboardRow(html, 'Alex')).not.toContain('stats-leaderboard__row--empty');
    });

    it('leaves swing-speed readings out of the comparison', () => {
      const swing = makeShot({ mode: 'swing-speed', club: 'Swing Speed', ball_speed_mph: 99, timestamp: 's1' });
      const html = renderLeaderboard([groupShots[0], swing], 'driver');

      expect(html).not.toContain('SWING SPEED');
      expect(html).toContain('All (1)');
      expect(leaderboardRow(html, 'James')).toMatch(/stats-leaderboard__num">1<\/td>/);
    });

    it('shows the empty state when nobody has a ball shot yet', () => {
      const html = renderLeaderboard([]);

      expect(html).toContain('No shots yet');
      expect(html).not.toContain('stats-leaderboard__table');
      expect(header(html)).toContain('>Just me<');
    });

    it('drag-scrolls a long roster under a pinned header row', () => {
      expect(css).toMatch(/\.stats-leaderboard \{[^}]*overflow-y: auto/);
      expect(css).toMatch(/\.stats-leaderboard \{[^}]*touch-action: none/);
      expect(css).toMatch(/\.stats-leaderboard__table thead th \{[^}]*position: sticky/);
    });
  });
});
