import type { ShotTrajectory } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import { convertDistanceFromYards, convertHeightFromFeet } from '../../utils/units';

/** A trajectory sample in display units (yds and ft, or m and m) and seconds. */
export interface FlightPoint {
  distance: number;
  height: number;
  /** Offset from the target line; right of it is positive. */
  lateral: number;
  time: number;
}

export interface FlightAxes {
  maxDistance: number;
  distanceStep: number;
  maxHeight: number;
  heightStep: number;
}

export interface ChartFrame {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

const NICE_FACTORS = [1, 2, 2.5, 5, 10];
const MIN_DISTANCE = 10;
const MIN_HEIGHT = 10;
// The tallest flight fills ~3/4 of the plot so its apex label stays inside.
const HEIGHT_HEADROOM = 1.3;
const MAX_DISTANCE_TICKS = 6;
const MAX_HEIGHT_TICKS = 4;
// A top view never zooms in past this half-width, so a near-straight shot looks straight.
const MIN_LATERAL = 10;
const LATERAL_HEADROOM = 1.25;
const MAX_LATERAL_TICKS = 2;
const ANIMATION_SPEEDUP = 2;
const MIN_ANIMATION_MS = 1200;
const MAX_ANIMATION_MS = 3500;

export function toFlightPoints(trajectory: ShotTrajectory, unitSystem: UnitSystem): FlightPoint[] {
  return trajectory.points.map(([distanceYd, heightFt, lateralYd, timeS]) => ({
    distance: convertDistanceFromYards(distanceYd, unitSystem),
    height: convertHeightFromFeet(heightFt, unitSystem),
    lateral: convertDistanceFromYards(lateralYd, unitSystem),
    time: timeS,
  }));
}

/** Smallest 1, 2, 2.5 or 5 × 10ⁿ step that splits `span` into at most `maxTicks` intervals. */
export function niceStep(span: number, maxTicks: number): number {
  const rough = span / maxTicks;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const factor = NICE_FACTORS.find((candidate) => candidate * magnitude >= rough) ?? 10;
  return factor * magnitude;
}

/** One scale for every drawn flight, so the current shot and its ghosts compare honestly. */
export function flightAxes(flights: FlightPoint[][]): FlightAxes {
  let farthest = MIN_DISTANCE;
  let highest = MIN_HEIGHT;
  for (const flight of flights) {
    for (const point of flight) {
      farthest = Math.max(farthest, point.distance);
      highest = Math.max(highest, point.height);
    }
  }
  const heightSpan = highest * HEIGHT_HEADROOM;
  const distanceStep = niceStep(farthest, MAX_DISTANCE_TICKS);
  const heightStep = niceStep(heightSpan, MAX_HEIGHT_TICKS);
  return {
    maxDistance: Math.ceil(farthest / distanceStep) * distanceStep,
    distanceStep,
    maxHeight: Math.ceil(heightSpan / heightStep) * heightStep,
    heightStep,
  };
}

export function axisTicks(max: number, step: number): number[] {
  const count = Math.round(max / step);
  return Array.from({ length: count + 1 }, (_, index) => Number((index * step).toFixed(6)));
}

export function formatTick(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

export function projectX(distance: number, axes: FlightAxes, frame: ChartFrame): number {
  return frame.left + (distance / axes.maxDistance) * (frame.width - frame.left - frame.right);
}

export function projectY(height: number, axes: FlightAxes, frame: ChartFrame): number {
  return frame.height - frame.bottom - (height / axes.maxHeight) * (frame.height - frame.top - frame.bottom);
}

export function polylinePoints(points: FlightPoint[], axes: FlightAxes, frame: ChartFrame): string {
  return points
    .map(
      (point) => `${projectX(point.distance, axes, frame).toFixed(1)},${projectY(point.height, axes, frame).toFixed(1)}`
    )
    .join(' ');
}

export function areaPoints(points: FlightPoint[], axes: FlightAxes, frame: ChartFrame): string {
  if (points.length === 0) {
    return '';
  }
  const ground = projectY(0, axes, frame).toFixed(1);
  const first = projectX(points[0].distance, axes, frame).toFixed(1);
  const last = projectX(points[points.length - 1].distance, axes, frame).toFixed(1);
  return `${polylinePoints(points, axes, frame)} ${last},${ground} ${first},${ground}`;
}

export function flownBy(points: FlightPoint[], time: number): FlightPoint[] {
  const flown: FlightPoint[] = [];
  for (const point of points) {
    if (point.time <= time || flown.length === 0) {
      flown.push(point);
      continue;
    }
    const previous = flown[flown.length - 1];
    const fraction = (time - previous.time) / (point.time - previous.time);
    flown.push({
      distance: previous.distance + fraction * (point.distance - previous.distance),
      height: previous.height + fraction * (point.height - previous.height),
      lateral: previous.lateral + fraction * (point.lateral - previous.lateral),
      time,
    });
    break;
  }
  return flown;
}

/** Half-width of the top view: one symmetric scale for every drawn flight. */
export function lateralExtent(flights: FlightPoint[][]): number {
  let widest = 0;
  for (const flight of flights) {
    for (const point of flight) {
      widest = Math.max(widest, Math.abs(point.lateral));
    }
  }
  const span = Math.max(MIN_LATERAL, widest * LATERAL_HEADROOM);
  const step = niceStep(span, MAX_LATERAL_TICKS);
  return Math.ceil(span / step) * step;
}

/** Top view: the target line runs along the middle and right of it is drawn below. */
export function projectLateral(lateral: number, extent: number, frame: ChartFrame): number {
  const half = (frame.height - frame.top - frame.bottom) / 2;
  return frame.top + half + (lateral / extent) * half;
}

export function topViewPoints(points: FlightPoint[], axes: FlightAxes, extent: number, frame: ChartFrame): string {
  return points
    .map(
      (point) =>
        `${projectX(point.distance, axes, frame).toFixed(1)},${projectLateral(point.lateral, extent, frame).toFixed(1)}`
    )
    .join(' ');
}

export function apexPoint(points: FlightPoint[]): FlightPoint | null {
  return points.reduce<FlightPoint | null>((best, point) => (!best || point.height > best.height ? point : best), null);
}

export function flightAnimationMs(flightTimeS: number): number {
  return Math.min(MAX_ANIMATION_MS, Math.max(MIN_ANIMATION_MS, (flightTimeS * 1000) / ANIMATION_SPEEDUP));
}

export function flightTimeAt(elapsedMs: number, flightTimeS: number): number {
  return Math.min(1, Math.max(0, elapsedMs / flightAnimationMs(flightTimeS))) * flightTimeS;
}
