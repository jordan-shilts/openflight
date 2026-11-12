import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react';
import type { Shot, ShotTrajectory } from '../../types/shot';
import { getDisplayCarryYards, isPuttShot } from '../../types/shot';
import { useI18n } from '../../i18n/useI18n';
import type { UnitSystem } from '../../utils/units';
import {
  convertHeightFromFeet,
  formatDistance,
  formatRollDistance,
  getDistanceUnit,
  getHeightUnit,
  getRollDistanceUnit,
} from '../../utils/units';
import { MetricCard } from '../ui/MetricCard';
import {
  apexPoint,
  areaPoints,
  axisTicks,
  flightAxes,
  flightTimeAt,
  flownBy,
  formatTick,
  lateralExtent,
  polylinePoints,
  projectLateral,
  projectX,
  projectY,
  toFlightPoints,
  topViewPoints,
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

/** Same left and right as the side view, so one distance lines up in both. */
function topViewFrame(size: ChartSize): ChartFrame {
  const tickPx = size.rem * TICK_REM;
  return { ...chartFrame(size), top: tickPx * 0.9, bottom: tickPx * 0.9 };
}

/** Used until the first measurement, and for server rendering in tests. */
const DEFAULT_TOP_SIZE: ChartSize = { width: 800, height: 72, rem: 16 };

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

function useChartSize(ref: RefObject<HTMLDivElement | null>, initial: ChartSize = DEFAULT_SIZE): ChartSize {
  const [size, setSize] = useState(initial);

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
        rem: parseFloat(getComputedStyle(document.documentElement).fontSize) || initial.rem,
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, initial.rem]);

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
  time,
  unitSystem,
  carryLabel,
  ariaLabel,
}: {
  trajectory: ShotTrajectory;
  ghosts: ShotTrajectory[];
  /** Seconds of flight drawn so far. */
  time: number;
  unitSystem: UnitSystem;
  carryLabel: string;
  ariaLabel: string;
}) {
  const { t } = useI18n();
  const chartRef = useRef<HTMLDivElement>(null);
  const size = useChartSize(chartRef);

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

/** How far the flight lands from the target line, e.g. "12 yds right". */
function offlineLabel(trajectory: ShotTrajectory, unitSystem: UnitSystem, t: ReturnType<typeof useI18n>['t']): string {
  const lateralYd = trajectory.points[trajectory.points.length - 1][2];
  const distance = formatDistance(Math.abs(lateralYd), unitSystem, 0);
  if (Number(distance) === 0) {
    return t('flight.onLine');
  }
  return t(lateralYd > 0 ? 'flight.offlineRight' : 'flight.offlineLeft', {
    distance,
    unit: getDistanceUnit(unitSystem),
  });
}

/** Looking down on the flight: the target line along the middle, the ball curving off it. */
function FlightTopView({
  trajectory,
  ghosts,
  time,
  unitSystem,
}: {
  trajectory: ShotTrajectory;
  ghosts: ShotTrajectory[];
  time: number;
  unitSystem: UnitSystem;
}) {
  const { t } = useI18n();
  const chartRef = useRef<HTMLDivElement>(null);
  const size = useChartSize(chartRef, DEFAULT_TOP_SIZE);

  const points = useMemo(() => toFlightPoints(trajectory, unitSystem), [trajectory, unitSystem]);
  const ghostPoints = useMemo(() => ghosts.map((ghost) => toFlightPoints(ghost, unitSystem)), [ghosts, unitSystem]);
  const axes = useMemo(() => flightAxes([points, ...ghostPoints]), [points, ghostPoints]);
  const extent = useMemo(() => lateralExtent([points, ...ghostPoints]), [points, ghostPoints]);
  const frame = useMemo(() => topViewFrame(size), [size]);
  const ghostLines = useMemo(
    () => ghostPoints.map((ghost) => topViewPoints(ghost, axes, extent, frame)),
    [ghostPoints, axes, extent, frame]
  );

  const tickPx = size.rem * TICK_REM;
  const ballRadius = size.rem * 0.3;
  const flown = flownBy(points, time);
  const ball = flown[flown.length - 1];
  const landed = time >= trajectory.flight_time_s;
  const plotLeft = frame.left;
  const plotRight = frame.width - frame.right;
  const targetLine = projectLateral(0, extent, frame);
  const ballX = projectX(ball.distance, axes, frame);
  const ballY = projectLateral(ball.lateral, extent, frame);
  const labelAfter = ballX < plotLeft + (plotRight - plotLeft) * 0.25;
  // The label sits on the far side of the ball from the target line, clear of
  // the curve, until the ball is too near the strip's edge for it to fit there.
  const labelBelow = ballY + ballRadius * 1.6 + tickPx;
  const labelAbove = ballY - ballRadius * 1.6;
  const fitsBelow = labelBelow <= frame.height - tickPx * 0.3;
  const fitsAbove = labelAbove - tickPx >= tickPx * 0.3;
  const labelY = ball.lateral >= 0 ? (fitsBelow ? labelBelow : labelAbove) : fitsAbove ? labelAbove : labelBelow;
  const offline = offlineLabel(trajectory, unitSystem, t);
  const extentLabel = `${formatTick(extent)} ${getDistanceUnit(unitSystem)}`;

  return (
    <div ref={chartRef} className="flight-view__top">
      <svg
        className="flight-chart flight-chart--top"
        viewBox={`0 0 ${size.width} ${size.height}`}
        role="img"
        aria-label={t('flight.topLabel', { offline })}
      >
        <g className="flight-chart__grid" aria-hidden="true">
          <line className="flight-chart__target" x1={plotLeft} x2={plotRight} y1={targetLine} y2={targetLine} />
          <text
            className="flight-chart__tick"
            x={plotLeft - tickPx * 0.5}
            y={projectLateral(-extent, extent, frame) + tickPx * 0.9}
            textAnchor="end"
          >
            {t('flight.leftShort')}
          </text>
          <text
            className="flight-chart__tick"
            x={plotLeft - tickPx * 0.5}
            y={projectLateral(extent, extent, frame)}
            textAnchor="end"
          >
            {t('flight.rightShort')}
          </text>
          {/* Top-left stays clear: every flight starts on the target line. */}
          <text
            className="flight-chart__tick"
            x={plotLeft + tickPx * 0.6}
            y={projectLateral(-extent, extent, frame) + tickPx * 0.9}
            textAnchor="start"
          >
            {`± ${extentLabel}`}
          </text>
        </g>

        <g className="flight-chart__ghosts" aria-hidden="true">
          {ghostLines.map((line, index) => (
            <polyline key={index} className="flight-chart__ghost flight-chart__ghost--top" points={line} />
          ))}
        </g>

        <polyline
          className="flight-chart__flight flight-chart__flight--top"
          points={topViewPoints(flown, axes, extent, frame)}
        />
        <circle className="flight-chart__ball flight-chart__ball--top" cx={ballX} cy={ballY} r={ballRadius} />
        {landed ? (
          <text
            className="flight-chart__label flight-chart__offline"
            x={labelAfter ? ballX + ballRadius * 2 : ballX - ballRadius * 2}
            y={labelY}
            textAnchor={labelAfter ? 'start' : 'end'}
          >
            {offline}
          </text>
        ) : null}
      </svg>
    </div>
  );
}

/** Both views play the same flight clock, so the ball is at one place in each. */
function FlightCharts({
  trajectory,
  ghosts,
  animate,
  showTopView,
  unitSystem,
  carryLabel,
  ariaLabel,
}: {
  trajectory: ShotTrajectory;
  ghosts: ShotTrajectory[];
  animate: boolean;
  showTopView: boolean;
  unitSystem: UnitSystem;
  carryLabel: string;
  ariaLabel: string;
}) {
  const time = useFlightTime(trajectory.flight_time_s, animate);
  return (
    <>
      <FlightChart
        trajectory={trajectory}
        ghosts={ghosts}
        time={time}
        unitSystem={unitSystem}
        carryLabel={carryLabel}
        ariaLabel={ariaLabel}
      />
      {showTopView ? (
        <FlightTopView trajectory={trajectory} ghosts={ghosts} time={time} unitSystem={unitSystem} />
      ) : null}
    </>
  );
}

/**
 * A top view only says something when the ball's direction was measured: with
 * an estimated start line and no spin axis every shot would be drawn dead straight.
 */
function hasMeasuredDirection(shot: Shot): boolean {
  const startMeasured =
    shot.launch_angle_horizontal !== null &&
    shot.angle_source !== 'estimated' &&
    shot.launch_angle_horizontal_source !== 'estimated';
  return startMeasured || shot.spin_axis_deg !== null;
}

/**
 * Side view of the latest shot over the player's earlier flights; it lands at
 * the Carry tile's value. A top view joins it when the direction was measured.
 */
export function FlightView({ shot, earlierShots, animate, unitSystem }: FlightViewProps) {
  const { t } = useI18n();
  // An enriched shot can get its trajectory after mounting; play it when it lands.
  const [mountedWithoutTrajectory] = useState(() => !shot.trajectory);
  const ghosts = useMemo(
    () => earlierShots.flatMap((earlier) => (earlier.trajectory ? [earlier.trajectory] : [])),
    [earlierShots]
  );
  const trajectory = shot.trajectory && shot.trajectory.points.length >= 2 ? shot.trajectory : null;

  if (isPuttShot(shot)) {
    return (
      <div className="flight-view flight-view--empty">
        <span className="panel__empty-title">{t('flight.putt')}</span>
        <span className="panel__empty-detail">
          {t('flight.puttDetail', {
            distance: formatRollDistance(shot.roll_distance_ft, unitSystem),
            unit: getRollDistanceUnit(unitSystem),
          })}
        </span>
      </div>
    );
  }

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
      <FlightCharts
        trajectory={trajectory}
        ghosts={ghosts}
        animate={animate || mountedWithoutTrajectory}
        showTopView={hasMeasuredDirection(shot)}
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
