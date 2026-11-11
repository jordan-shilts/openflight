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

  it('says a putt has no flight and how far it rolled', () => {
    const putt = makeShot({ club: 'putter', carry_spin_adjusted: 0, trajectory: null, roll_distance_ft: 11.1 });

    const html = render(putt);

    expect(html).toContain('flight-view--empty');
    expect(html).toContain('No flight for a putt');
    expect(html).toContain('It rolled 11 ft along the green');
    expect(html).not.toContain('<svg');
    expect(render(putt, [], false, 'metric')).toContain('It rolled 3.4 m along the green');
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

  describe('top view', () => {
    function curving(lateralAtLanding: number): ShotTrajectory {
      const flight = flightOf(263.4, 92.6);
      return {
        ...flight,
        points: flight.points.map(([distance, height, , time], index) => [
          distance,
          height,
          (lateralAtLanding * index * index) / 16,
          time,
        ]),
      };
    }

    it('joins the side view when the start direction was measured', () => {
      const html = render(makeShot());

      expect(html).toContain('class="flight-view__top"');
      expect(html).toContain('aria-label="Top view of the flight: 2 yds left"');
      expect(html).toMatch(/flight-chart__offline"[^>]*>2 yds left</);
      expect(polylinePointCount(html, 'flight-chart__flight flight-chart__flight--top')).toEqual([5]);
    });

    it('says which side a curving shot finished on, in the chosen units', () => {
      const fade = makeShot({ trajectory: curving(18.4), spin_axis_deg: 9.5 });

      expect(render(fade)).toMatch(/flight-chart__offline"[^>]*>18 yds right</);
      expect(render(fade, [], false, 'metric')).toMatch(/flight-chart__offline"[^>]*>17 m right</);
      expect(render(makeShot({ trajectory: curving(-31) }))).toMatch(/flight-chart__offline"[^>]*>31 yds left</);
    });

    it('calls a shot that lands within half a yard of the line on line', () => {
      const html = render(makeShot({ trajectory: curving(0.4) }));

      expect(html).toMatch(/flight-chart__offline"[^>]*>On the target line</);
    });

    it('draws right of the target line below it and left above it', () => {
      const ballY = (html: string) => Number(html.match(/flight-chart__ball--top" cx="[\d.]+" cy="([\d.]+)"/)?.[1]);
      const targetY = (html: string) => Number(html.match(/flight-chart__target"[^>]* y1="([\d.]+)"/)?.[1]);
      const right = render(makeShot({ trajectory: curving(18) }));
      const left = render(makeShot({ trajectory: curving(-18) }));

      expect(ballY(right)).toBeGreaterThan(targetY(right));
      expect(ballY(left)).toBeLessThan(targetY(left));
    });

    it('keeps its landing label on the far side of the ball from the target line', () => {
      const labelY = (html: string) => Number(html.match(/flight-chart__offline" x="[\d.]+" y="([\d.]+)"/)?.[1]);
      const ballY = (html: string) => Number(html.match(/flight-chart__ball--top" cx="[\d.]+" cy="([\d.]+)"/)?.[1]);
      const right = render(makeShot({ trajectory: curving(3) }));
      const left = render(makeShot({ trajectory: curving(-3) }));

      expect(labelY(right)).toBeGreaterThan(ballY(right));
      expect(labelY(left)).toBeLessThan(ballY(left));
    });

    it('moves that label inside when the ball finishes too near the edge of the strip', () => {
      const labelY = (html: string) => Number(html.match(/flight-chart__offline" x="[\d.]+" y="([\d.]+)"/)?.[1]);
      const ballY = (html: string) => Number(html.match(/flight-chart__ball--top" cx="[\d.]+" cy="([\d.]+)"/)?.[1]);
      const right = render(makeShot({ trajectory: curving(7.5) }));
      const left = render(makeShot({ trajectory: curving(-7.5) }));

      expect(labelY(right)).toBeLessThan(ballY(right));
      expect(labelY(right)).toBeGreaterThan(0);
      expect(labelY(left)).toBeGreaterThan(ballY(left));
      expect(labelY(left)).toBeLessThan(72);
    });

    it('labels its scale and widens it for the widest flight drawn', () => {
      expect(render(makeShot())).toContain('± 10 yds');
      expect(render(makeShot(), [makeShot({ trajectory: curving(34) })])).toContain('± 50 yds');
    });

    it('draws earlier flights as ghosts on the same scale', () => {
      const html = render(makeShot(), [makeShot({ trajectory: curving(12) }), makeShot({ trajectory: null })]);

      expect(polylinePointCount(html, 'flight-chart__ghost flight-chart__ghost--top')).toEqual([5]);
    });

    it('is left out when the direction was only estimated', () => {
      const html = render(makeShot({ angle_source: 'estimated', launch_angle_horizontal_source: 'estimated' }));

      expect(html).not.toContain('flight-view__top');
      expect(html).toContain('<svg class="flight-chart"');
    });

    it('is left out when only the vertical launch was measured', () => {
      const html = render(makeShot({ angle_source: 'radar', launch_angle_horizontal_source: 'estimated' }));

      expect(html).not.toContain('flight-view__top');
    });

    it('shows a derived spin axis even when the start line was estimated', () => {
      const html = render(
        makeShot({ angle_source: 'estimated', launch_angle_horizontal_source: 'estimated', spin_axis_deg: 6 })
      );

      expect(html).toContain('flight-view__top');
    });

    it('holds its landing label back until the animated ball lands', () => {
      const html = render(makeShot(), [], true);

      expect(html).toContain('flight-view__top');
      expect(html).not.toContain('flight-chart__offline');
    });

    it('is a short strip sized in rem that the side view gives way to', () => {
      expect(css).toMatch(/\.flight-view__top \{[^}]*flex: 0 0 auto;[^}]*height: clamp\([\d.]+rem, [\d.]+vh, [\d.]+rem\)/);
      expect(css).toMatch(/\.flight-chart__target \{[^}]*stroke-dasharray/);
    });
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
