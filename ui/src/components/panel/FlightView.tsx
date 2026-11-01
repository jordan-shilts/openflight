import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react';
import type { Shot, ShotTrajectory } from '../../types/shot';
import { getDisplayCarryYards } from '../../types/shot';
import { useI18n } from '../../i18n/useI18n';
import type { UnitSystem } from '../../utils/units';
import { convertHeightFromFeet, formatDistance, getDistanceUnit, getHeightUnit } from '../../utils/units';
import { MetricCard } from '../ui/MetricCard';
import {
  apexPoint,
  areaPoints,
  axisTicks,
  flightAxes,
  flightTimeAt,
  flownBy,
  formatTick,
  polylinePoints,
  projectX,
  projectY,
  toFlightPoints,
  type ChartFrame,
} from './flightChart';

interface FlightViewProps {
  shot: Shot;
  earlierShots: Shot[];
  /** Play the flight once on mount; otherwise draw it landed. */
  animate: boolean;
  unitSystem: UnitSystem;
}

interface ChartSize {
  width: number;
  height: number;
  rem: number;
}

/** Used until the first measurement, and for server rendering in tests. */
const DEFAULT_SIZE: ChartSize = { width: 800, height: 320, rem: 16 };
/** Tick label size; keep in step with `.flight-chart__tick` in panel.css. */
const TICK_REM = 0.5625;

function chartFrame(size: ChartSize): ChartFrame {
  const tickPx = size.rem * TICK_REM;
  return {
    width: size.width,
    height: size.height,
    left: tickPx * 3,
    right: tickPx * 1.5,
    top: tickPx * 2.2,
    bottom: tickPx * 2,
  };
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

function useChartSize(ref: RefObject<HTMLDivElement | null>): ChartSize {
  const [size, setSize] = useState(DEFAULT_SIZE);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      return undefined;
    }
    // ResizeObserver also reports the initial size right after observe().
    const observer = new ResizeObserver(() => {
      if (element.clientWidth === 0 || element.clientHeight === 0) {
        return;
      }
      setSize({
        width: element.clientWidth,
        height: element.clientHeight,
        rem: parseFloat(getComputedStyle(document.documentElement).fontSize) || DEFAULT_SIZE.rem,
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);

  return size;
}

function useFlightTime(flightTimeS: number, animate: boolean): number {
  const [playing] = useState(() => animate && !prefersReducedMotion());
  const [time, setTime] = useState(0);

  useEffect(() => {
    if (!playing) {
      return undefined;
    }
    let frame = 0;
    let start: number | null = null;
    const step = (now: number) => {
      start ??= now;
      const shown = flightTimeAt(now - start, flightTimeS);
      setTime(shown);
      if (shown < flightTimeS) {
        frame = requestAnimationFrame(step);
      }
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [playing, flightTimeS]);

  return playing ? time : flightTimeS;
}

function FlightChart({
  trajectory,
  ghosts,
  animate,
  unitSystem,
  carryLabel,
  ariaLabel,
}: {
  trajectory: ShotTrajectory;
  ghosts: ShotTrajectory[];
  animate: boolean;
  unitSystem: UnitSystem;
  carryLabel: string;
  ariaLabel: string;
}) {
  const { t } = useI18n();
  const chartRef = useRef<HTMLDivElement>(null);
  const size = useChartSize(chartRef);
  const time = useFlightTime(trajectory.flight_time_s, animate);

  const points = useMemo(() => toFlightPoints(trajectory, unitSystem), [trajectory, unitSystem]);
  const ghostPoints = useMemo(() => ghosts.map((ghost) => toFlightPoints(ghost, unitSystem)), [ghosts, unitSystem]);
  const axes = useMemo(() => flightAxes([points, ...ghostPoints]), [points, ghostPoints]);

  const tickPx = size.rem * TICK_REM;
  const frame = useMemo(() => chartFrame(size), [size]);
  const ghostLines = useMemo(
    () => ghostPoints.map((ghost) => polylinePoints(ghost, axes, frame)),
    [ghostPoints, axes, frame]
  );
  const ballRadius = size.rem * 0.3;
  const flown = flownBy(points, time);
  const ball = flown[flown.length - 1];
  const apex = apexPoint(points);
  const landed = time >= trajectory.flight_time_s;
  const showApex = apex !== null && time >= apex.time;
  const ground = projectY(0, axes, frame);
  const plotLeft = frame.left;
  const plotRight = frame.width - frame.right;
  const distanceTicks = axisTicks(axes.maxDistance, axes.distanceStep);
  const heightTicks = axisTicks(axes.maxHeight, axes.heightStep);
  const apexLabelX = apex
    ? Math.min(plotRight - tickPx * 4, Math.max(plotLeft + tickPx * 4, projectX(apex.distance, axes, frame)))
    : 0;
  const landingX = projectX(ball.distance, axes, frame);
  const landingLabelAfter = landingX < plotLeft + (plotRight - plotLeft) * 0.2;

  return (
    <div ref={chartRef} className="flight-view__chart">
      <svg className="flight-chart" viewBox={`0 0 ${size.width} ${size.height}`} role="img" aria-label={ariaLabel}>
        <g className="flight-chart__grid" aria-hidden="true">
          {distanceTicks.map((tick, index) => {
            const x = projectX(tick, axes, frame);
            const last = index === distanceTicks.length - 1;
            return (
              <g key={`d${tick}`}>
                <line className="flight-chart__gridline" x1={x} x2={x} y1={frame.top} y2={ground} />
                <text
                  className="flight-chart__tick"
                  x={x}
                  y={ground + tickPx * 1.5}
                  textAnchor={last ? 'end' : index === 0 ? 'start' : 'middle'}
                >
                  {last ? `${formatTick(tick)} ${getDistanceUnit(unitSystem)}` : formatTick(tick)}
                </text>
              </g>
            );
          })}
          {heightTicks.map((tick) => {
            const y = projectY(tick, axes, frame);
            return (
              <g key={`h${tick}`}>
                <line
                  className={tick === 0 ? 'flight-chart__ground' : 'flight-chart__gridline'}
                  x1={plotLeft}
                  x2={plotRight}
                  y1={y}
                  y2={y}
                />
                {tick === 0 ? null : (
                  <text
                    className="flight-chart__tick"
                    x={plotLeft - tickPx * 0.5}
                    y={y + tickPx * 0.35}
                    textAnchor="end"
                  >
                    {formatTick(tick)}
                  </text>
                )}
              </g>
            );
          })}
          <text
            className="flight-chart__tick"
            x={plotLeft - tickPx * 0.5}
            y={projectY(axes.maxHeight, axes, frame) - tickPx * 1.1}
            textAnchor="end"
          >
            {getHeightUnit(unitSystem)}
          </text>
        </g>

        <g className="flight-chart__ghosts" aria-hidden="true">
          {ghostLines.map((line, index) => (
            <polyline key={index} className="flight-chart__ghost" points={line} />
          ))}
        </g>

        <polygon className="flight-chart__area" points={areaPoints(flown, axes, frame)} />
        <polyline className="flight-chart__flight" points={polylinePoints(flown, axes, frame)} />

        {showApex && apex ? (
          <g className="flight-chart__apex">
            <circle
              className="flight-chart__apex-dot"
              cx={projectX(apex.distance, axes, frame)}
              cy={projectY(apex.height, axes, frame)}
              r={ballRadius * 0.6}
            />
            <text
              className="flight-chart__label"
              x={apexLabelX}
              y={projectY(apex.height, axes, frame) - tickPx * 1.1}
              textAnchor="middle"
            >
              {`${t('flight.apex')} ${convertHeightFromFeet(trajectory.apex_ft, unitSystem).toFixed(0)} ${getHeightUnit(unitSystem)}`}
            </text>
          </g>
        ) : null}

        <circle className="flight-chart__ball" cx={landingX} cy={projectY(ball.height, axes, frame)} r={ballRadius} />
        {landed ? (
          <text
            className="flight-chart__label flight-chart__landing"
            x={landingLabelAfter ? landingX + ballRadius * 2 : landingX - ballRadius * 2}
            y={ground - ballRadius * 2}
            textAnchor={landingLabelAfter ? 'start' : 'end'}
          >
            {carryLabel}
          </text>
        ) : null}
      </svg>
    </div>
  );
}

/** Side view of the latest shot over the player's earlier flights; it lands at the Carry tile's value. */
export function FlightView({ shot, earlierShots, animate, unitSystem }: FlightViewProps) {
  const { t } = useI18n();
  // An enriched shot can get its trajectory after mounting; play it when it lands.
  const [mountedWithoutTrajectory] = useState(() => !shot.trajectory);
  const ghosts = useMemo(
    () => earlierShots.flatMap((earlier) => (earlier.trajectory ? [earlier.trajectory] : [])),
    [earlierShots]
  );
  const trajectory = shot.trajectory && shot.trajectory.points.length >= 2 ? shot.trajectory : null;

  if (!trajectory) {
    return (
      <div className="flight-view flight-view--empty">
        <span className="panel__empty-title">{t('flight.unavailable')}</span>
        <span className="panel__empty-detail">{t('flight.unavailableDetail')}</span>
      </div>
    );
  }

  const distanceUnit = getDistanceUnit(unitSystem);
  const heightUnit = getHeightUnit(unitSystem);
  const carry = formatDistance(getDisplayCarryYards(shot), unitSystem, 0);
  const apex = convertHeightFromFeet(trajectory.apex_ft, unitSystem).toFixed(0);

  return (
    <div className="flight-view">
      <FlightChart
        trajectory={trajectory}
        ghosts={ghosts}
        animate={animate || mountedWithoutTrajectory}
        unitSystem={unitSystem}
        carryLabel={`${carry} ${distanceUnit}`}
        ariaLabel={t('flight.chartLabel', { carry, distanceUnit, apex, heightUnit })}
      />
      <div className="flight-view__readout">
        <MetricCard label={t('metric.carry')} value={carry} unit={distanceUnit} labelPosition="above" />
        <MetricCard label={t('flight.apex')} value={apex} unit={heightUnit} labelPosition="above" />
        <MetricCard
          label={t('flight.landingAngle')}
          value={trajectory.landing_angle_deg.toFixed(1)}
          unit="°"
          labelPosition="above"
        />
        <MetricCard
          label={t('flight.hangTime')}
          value={trajectory.flight_time_s.toFixed(1)}
          unit="s"
          labelPosition="above"
        />
      </div>
    </div>
  );
}
