import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Shot, ShotTrajectory } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import { FlightView } from './FlightView';
import { buildLiveMetrics } from './liveMetrics';

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function flightOf(carry: number, apexFt: number): ShotTrajectory {
  return {
    points: [
      [0, 0, 0, 0],
      [carry * 0.3, apexFt * 0.7, -0.5, 1.5],
      [carry * 0.55, apexFt, -1, 3],
      [carry * 0.8, apexFt * 0.6, -1.5, 4.5],
      [carry, 0, -2, 6],
    ],
    carry_yards: carry,
    apex_ft: apexFt,
    landing_angle_deg: 38.4,
    flight_time_s: 6,
  };
}

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 160,
    club_speed_mph: 110,
    smash_factor: 1.45,
    estimated_carry_yards: 250,
    carry_range: [240, 260],
    club: 'driver',
    timestamp: '2026-11-01T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 12,
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
    carry_spin_adjusted: 263,
    trajectory: flightOf(263.4, 92.6),
    profile_id: 'james',
    profile_name: 'James',
    ...overrides,
  };
}

function render(shot: Shot, earlierShots: Shot[] = [], animate = false, unitSystem: UnitSystem = 'imperial'): string {
  return text(
    renderToString(<FlightView shot={shot} earlierShots={earlierShots} animate={animate} unitSystem={unitSystem} />)
  );
}

function polylinePointCount(html: string, className: string): number[] {
  return [...html.matchAll(new RegExp(`class="${className}" points="([^"]*)"`, 'g'))].map(
    (match) => match[1].split(' ').filter(Boolean).length
  );
}

describe('FlightView', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('draws the landed flight with apex and landing labels', () => {
    const html = render(makeShot());

    expect(html).toContain('<svg class="flight-chart"');
    expect(polylinePointCount(html, 'flight-chart__flight')).toEqual([5]);
    expect(html).toContain('Apex 93 ft');
    expect(html).toMatch(/flight-chart__landing"[^>]*>263 yds</);
    expect(html).toContain('class="flight-chart__ball"');
    expect(html).toContain('aria-label="Side view of the flight: carry 263 yds, apex 93 ft"');
  });

  it('labels the landing with exactly the Carry tile value', () => {
    for (const unitSystem of ['imperial', 'metric'] as const) {
      const shot = makeShot();
      const carryTile = buildLiveMetrics(shot, unitSystem, {
        count: 0,
        last_speed_mph: 0,
        best_speed_mph: 0,
        avg_speed_mph: 0,
      }).find((metric) => metric.id === 'carry');

      const html = render(shot, [], false, unitSystem);

      expect(html).toMatch(new RegExp(`flight-chart__landing"[^>]*>${carryTile?.value} ${carryTile?.unit}<`));
    }
  });

  it('shows carry, apex, landing angle and hang time underneath', () => {
    const html = render(makeShot());
    const labels = [...html.matchAll(/metric-card__label[^>]*>([^<]+)/g)].map((match) => match[1]);
    const values = [...html.matchAll(/metric-card__value">([^<]+)/g)].map((match) => match[1]);

    expect(labels).toEqual(['Carry', 'Apex', 'Land. angle', 'Hang time']);
    expect(values).toEqual(['263', '93', '38.4', '6.0']);
  });

  it('switches every label to meters for metric units', () => {
    const html = render(makeShot(), [], false, 'metric');

    expect(html).toContain('Apex 28 m');
    expect(html).toMatch(/flight-chart__landing"[^>]*>240 m</);
    expect(html).toMatch(/flight-chart__tick"[^>]*>m</);
    expect(html).not.toContain(' yds');
  });

  it('draws earlier shots that have a flight as ghost lines', () => {
    const earlier = [
      makeShot({ timestamp: '2026-11-01T09:58:00Z', trajectory: flightOf(240, 85) }),
      makeShot({ timestamp: '2026-11-01T09:59:00Z', trajectory: null }),
      makeShot({ timestamp: '2026-11-01T09:59:30Z', trajectory: flightOf(280, 110) }),
    ];

    const html = render(makeShot(), earlier);

    expect(polylinePointCount(html, 'flight-chart__ghost')).toEqual([5, 5]);
    expect(html).toContain('300 yds');
  });

  it('starts an arriving shot at launch, with the labels still hidden', () => {
    const html = render(makeShot(), [], true);

    expect(polylinePointCount(html, 'flight-chart__flight')).toEqual([2]);
    expect(html).not.toContain('Apex 93 ft');
    expect(html).not.toContain('flight-chart__landing');
  });

  it('skips the animation when the viewer prefers reduced motion', () => {
    vi.stubGlobal('window', { matchMedia: (query: string) => ({ matches: query.includes('reduce') }) });

    const html = render(makeShot(), [], true);

    expect(polylinePointCount(html, 'flight-chart__flight')).toEqual([5]);
    expect(html).toContain('Apex 93 ft');
    expect(html).toContain('flight-chart__landing');
  });

  it('explains a shot that has no simulated flight', () => {
    const html = render(makeShot({ trajectory: null, carry_spin_adjusted: null }));

    expect(html).not.toContain('<svg');
    expect(html).toContain('No flight for this shot');
    expect(html).toContain('The flight needs a launch angle and the ballistics model');
  });

  it('treats a trajectory without a launch and landing point as no flight', () => {
    const html = render(makeShot({ trajectory: { ...flightOf(263.4, 92.6), points: [[0, 0, 0, 0]] } }));

    expect(html).not.toContain('<svg');
    expect(html).toContain('No flight for this shot');
  });

  it('uses theme tokens and rem type for the chart', () => {
    expect(css).toMatch(/\.flight-chart__flight \{[^}]*stroke: var\(--color-accent\)/);
    expect(css).toMatch(/\.flight-chart__area \{[^}]*fill: color-mix\(in srgb, var\(--color-accent\)/);
    expect(css).toMatch(/\.flight-chart__ghost \{[^}]*stroke: color-mix\(in srgb, var\(--color-text\)/);
    expect(css).toMatch(/\.flight-chart__tick \{[^}]*font-size: 0\.5625rem/);
    expect(css).toMatch(/\.flight-chart__label \{[^}]*font-size: [\d.]+rem/);
  });

  it('keeps the chart in a 4:3 box with a 2x2 readout on a phone held upright', () => {
    const portrait = css.match(/@media \(max-width: 640px\) and \(orientation: portrait\) \{([\s\S]*?)\n\}/)?.[1] ?? '';

    expect(portrait).toMatch(/\.flight-view__chart \{[^}]*aspect-ratio: 4 \/ 3/);
    expect(portrait).toMatch(/\.flight-view__readout \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  });
});
