import { describe, expect, it } from 'vitest';
import type { ShotTrajectory } from '../../types/shot';
import {
  apexPoint,
  areaPoints,
  axisTicks,
  flightAnimationMs,
  flightAxes,
  flightTimeAt,
  flownBy,
  formatTick,
  lateralExtent,
  niceStep,
  polylinePoints,
  projectLateral,
  projectX,
  projectY,
  toFlightPoints,
  topViewPoints,
  type ChartFrame,
  type FlightAxes,
  type FlightPoint,
} from './flightChart';

const trajectory: ShotTrajectory = {
  points: [
    [0, 0, 0, 0],
    [100, 60, -1, 2],
    [150, 90, -2, 3.5],
    [250, 0, -4, 6],
  ],
  carry_yards: 250,
  apex_ft: 90,
  landing_angle_deg: 38.4,
  flight_time_s: 6,
};

const flight: FlightPoint[] = [
  { distance: 0, height: 0, lateral: 0, time: 0 },
  { distance: 100, height: 60, lateral: -1, time: 2 },
  { distance: 150, height: 90, lateral: -2, time: 3.5 },
  { distance: 250, height: 0, lateral: -4, time: 6 },
];

const axes: FlightAxes = { maxDistance: 300, distanceStep: 50, maxHeight: 120, heightStep: 40 };
const frame: ChartFrame = { width: 640, height: 260, left: 30, right: 10, top: 20, bottom: 40 };

describe('toFlightPoints', () => {
  it('keeps yards and feet for imperial, with lateral and time', () => {
    expect(toFlightPoints(trajectory, 'imperial')).toEqual(flight);
  });

  it('converts distance and height to meters for metric', () => {
    const [, second] = toFlightPoints(trajectory, 'metric');

    expect(second.distance).toBeCloseTo(91.44, 2);
    expect(second.height).toBeCloseTo(18.288, 3);
    expect(second.lateral).toBeCloseTo(-0.9144, 4);
    expect(second.time).toBe(2);
  });
});

describe('niceStep', () => {
  it.each([
    [274.7, 6, 50],
    [129, 4, 50],
    [45, 4, 20],
    [100, 4, 25],
    [10, 6, 2],
    [39, 4, 10],
  ])('splits %d into at most %d steps of %d', (span, maxTicks, step) => {
    expect(niceStep(span, maxTicks)).toBe(step);
    expect(Math.ceil(span / step)).toBeLessThanOrEqual(maxTicks);
  });
});

describe('flightAxes', () => {
  it('rounds the axes up to whole steps with room above the apex for its label', () => {
    const result = flightAxes([flight]);

    expect(result.distanceStep).toBe(50);
    expect(result.maxDistance).toBe(250);
    expect(result.maxHeight).toBeGreaterThanOrEqual(90 * 1.3);
    expect(result.maxHeight % result.heightStep).toBe(0);
  });

  it('scales to the longest and highest of the current flight and its ghosts', () => {
    const ghost: FlightPoint[] = [
      { distance: 0, height: 0, time: 0 },
      { distance: 180, height: 130, time: 3.6 },
      { distance: 290, height: 0, time: 7 },
    ];

    const result = flightAxes([flight, ghost]);

    expect(result.maxDistance).toBe(300);
    expect(result.maxHeight).toBeGreaterThanOrEqual(130 * 1.3);
  });

  it('keeps a usable minimum scale for tiny or missing flights', () => {
    expect(flightAxes([])).toEqual({ maxDistance: 10, distanceStep: 2, maxHeight: 15, heightStep: 5 });
    expect(flightAxes([[{ distance: 1, height: 0.5, time: 0.2 }]]).maxDistance).toBe(10);
  });
});

describe('axisTicks and formatTick', () => {
  it('lists every step from zero to the axis maximum', () => {
    expect(axisTicks(300, 50)).toEqual([0, 50, 100, 150, 200, 250, 300]);
    expect(axisTicks(7.5, 2.5)).toEqual([0, 2.5, 5, 7.5]);
  });

  it('prints whole ticks bare and fractional ticks with one decimal', () => {
    expect(formatTick(150)).toBe('150');
    expect(formatTick(2.5)).toBe('2.5');
  });
});

describe('projection', () => {
  it('maps zero to the plot origin and the axis maxima to the far corner, y pointing up', () => {
    expect(projectX(0, axes, frame)).toBe(30);
    expect(projectX(300, axes, frame)).toBe(630);
    expect(projectY(0, axes, frame)).toBe(220);
    expect(projectY(120, axes, frame)).toBe(20);
    expect(projectY(60, axes, frame)).toBe(120);
  });

  it('formats polyline points to one decimal', () => {
    expect(polylinePoints(flight.slice(0, 2), axes, frame)).toBe('30.0,220.0 230.0,120.0');
  });

  it('closes the fill down to the ground under the first and last points', () => {
    expect(areaPoints(flight.slice(0, 2), axes, frame)).toBe('30.0,220.0 230.0,120.0 230.0,220.0 30.0,220.0');
    expect(areaPoints([], axes, frame)).toBe('');
  });
});

describe('flownBy', () => {
  it('starts with the launch point only', () => {
    expect(flownBy(flight, 0)).toEqual([flight[0], { ...flight[0] }]);
  });

  it('ends at a point interpolated between samples', () => {
    const flown = flownBy(flight, 1);

    expect(flown).toHaveLength(2);
    expect(flown[1]).toEqual({ distance: 50, height: 30, lateral: -0.5, time: 1 });
  });

  it('returns the whole flight once it has landed', () => {
    expect(flownBy(flight, 6)).toEqual(flight);
    expect(flownBy(flight, 99)).toEqual(flight);
  });
});

describe('top view', () => {
  const at = (lateral: number): FlightPoint => ({ distance: 100, height: 0, lateral, time: 1 });

  it('never zooms in past ten each side, so a near-straight shot looks straight', () => {
    expect(lateralExtent([flight])).toBe(10);
    expect(lateralExtent([])).toBe(10);
  });

  it('widens to a round number that clears the widest flight, left or right', () => {
    expect(lateralExtent([[at(18)]])).toBe(40);
    expect(lateralExtent([[at(-18)]])).toBe(40);
    expect(lateralExtent([[at(3)], [at(-34)]])).toBe(50);
  });

  it('puts the target line in the middle, left above it and right below', () => {
    expect(projectLateral(0, 20, frame)).toBe(120);
    expect(projectLateral(-20, 20, frame)).toBe(20);
    expect(projectLateral(20, 20, frame)).toBe(220);
    expect(projectLateral(10, 20, frame)).toBe(170);
  });

  it('shares the side view distance scale', () => {
    expect(topViewPoints(flight, axes, 10, frame)).toBe('30.0,120.0 230.0,110.0 330.0,100.0 530.0,80.0');
  });
});

describe('apexPoint', () => {
  it('finds the highest sample', () => {
    expect(apexPoint(flight)).toEqual(flight[2]);
    expect(apexPoint([])).toBeNull();
  });
});

describe('flight animation timing', () => {
  it('replays at twice real speed within 1.2 to 3.5 seconds', () => {
    expect(flightAnimationMs(5)).toBe(2500);
    expect(flightAnimationMs(1)).toBe(1200);
    expect(flightAnimationMs(9)).toBe(3500);
  });

  it('maps elapsed replay time to flight time and stops at landing', () => {
    expect(flightTimeAt(0, 5)).toBe(0);
    expect(flightTimeAt(1250, 5)).toBe(2.5);
    expect(flightTimeAt(2500, 5)).toBe(5);
    expect(flightTimeAt(10_000, 5)).toBe(5);
    expect(flightTimeAt(-50, 5)).toBe(0);
  });
});
