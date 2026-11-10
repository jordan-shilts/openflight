import type { Shot, SpinQuality, SwingSpeedStats } from '../../types/shot';
import {
  getDisplayCarryYards,
  getSwingSpeedMph,
  isPuttShot,
  isRadarOnlyShot,
  isSwingSpeedShot,
} from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import {
  formatDistance,
  formatRollDistance,
  formatSpeed,
  getDistanceUnit,
  getRollDistanceUnit,
  getSpeedUnit,
} from '../../utils/units';
import { getHtmlLang, t } from '../../i18n';
import { selectAttackAngle, selectClubPath } from './clubDelivery';

/** Placeholder for a metric the current shot has no value for. */
export const NO_VALUE = '—';

export interface LiveMetric {
  /** Stable key. Persisted as the promoted-hero choice, so do not rename. */
  id: string;
  label: string;
  value: string;
  unit?: string;
  subtext?: string;
  /** True when the value is modeled rather than measured. Rendered as an icon. */
  estimated?: boolean;
  confidence?: SpinQuality | null;
  /** Override confidence copy while preserving its dot level. */
  confidenceLabel?: string;
}

/**
 * Live table: ten metrics, always the same ten in the same canonical order, so
 * the grid never reflows between shots. Metrics the shot did not produce render
 * as {@link NO_VALUE} rather than disappearing. The selected metric is then
 * pinned to the top-left by {@link pinSelectedMetric}.
 */
export const LIVE_METRIC_COUNT = 10;

/** Metric count for a swing-speed session: a single 5-tile row. */
export const SWING_METRIC_COUNT = 5;

/**
 * What the camera-triggered TI radar gives on its own, in canonical order. Club
 * speed, smash, spin axis, club path and attack angle need the OPS243 or a
 * camera clip, so a radar-only shot leaves them out instead of showing five
 * dashes. The five that remain fill the same single row a swing-speed session uses.
 */
export const RADAR_ONLY_METRIC_IDS: readonly string[] = ['ball_speed', 'carry', 'launch_v', 'launch_h', 'spin'];

function formatOptionalAngle(value: number | null, signed = false): string {
  if (value === null) return NO_VALUE;
  const prefix = signed && value >= 0 ? '+' : '';
  return `${prefix}${value.toFixed(1)}`;
}

function angleUnit(value: number | null): string | undefined {
  return value === null ? undefined : '°';
}

function formatSpinRpm(rpm: number | null): string {
  if (rpm === null) return NO_VALUE;
  return rpm.toLocaleString(getHtmlLang(), { maximumFractionDigits: 0 });
}

function launchAngleQuality(confidence: number | null): SpinQuality | null {
  if (confidence === null) return null;
  if (confidence >= 0.7) return 'high';
  if (confidence >= 0.4) return 'medium';
  return 'low';
}

function shotShape(spinAxisDeg: number | null): string | undefined {
  if (spinAxisDeg === null) return undefined;
  if (spinAxisDeg > 2) return t('shape.fade');
  if (spinAxisDeg < -2) return t('shape.draw');
  return t('shape.straight');
}

function markEstimated(isEstimated: boolean): true | undefined {
  return isEstimated ? true : undefined;
}

/** Keeps the `carry` id for a putt so a pinned Carry tile stays pinned as Roll. */
function distanceMetric(shot: Shot, unitSystem: UnitSystem): LiveMetric {
  if (isPuttShot(shot)) {
    return {
      id: 'carry',
      label: t('metric.roll'),
      value: formatRollDistance(shot.roll_distance_ft, unitSystem),
      unit: getRollDistanceUnit(unitSystem),
    };
  }
  return {
    id: 'carry',
    label: t('metric.carry'),
    value: formatDistance(getDisplayCarryYards(shot), unitSystem, 0),
    unit: getDistanceUnit(unitSystem),
    subtext: shot.carry_spin_adjusted === null ? undefined : t('metric.spinAdjusted'),
    estimated: markEstimated(shot.carry_spin_adjusted === null),
  };
}

function buildBallStrikeMetrics(shot: Shot, unitSystem: UnitSystem): LiveMetric[] {
  const speedUnit = getSpeedUnit(unitSystem);
  const angleConfidence = launchAngleQuality(shot.launch_angle_confidence);
  const angleEstimated = shot.angle_source === 'estimated';
  const horizontalLaunchIsCameraAssisted = shot.launch_angle_horizontal_source === 'camera_assisted_experimental';
  const attack = selectAttackAngle(shot);
  const clubPath = selectClubPath(shot);

  return [
    {
      id: 'ball_speed',
      label: t('metric.ballSpeed'),
      value: formatSpeed(shot.ball_speed_mph, unitSystem, 1),
      unit: speedUnit,
    },
    distanceMetric(shot, unitSystem),
    {
      id: 'club_speed',
      label: t('metric.clubSpeed'),
      value: shot.club_speed_mph === null ? NO_VALUE : formatSpeed(shot.club_speed_mph, unitSystem, 1),
      unit: shot.club_speed_mph === null ? undefined : speedUnit,
    },
    {
      id: 'smash',
      label: t('metric.smash'),
      value: shot.smash_factor === null ? NO_VALUE : shot.smash_factor.toFixed(2),
    },
    {
      id: 'launch_v',
      label: t('metric.vLaunch'),
      value: formatOptionalAngle(shot.launch_angle_vertical),
      unit: angleUnit(shot.launch_angle_vertical),
      estimated: markEstimated(shot.launch_angle_vertical !== null && angleEstimated),
      confidence: shot.launch_angle_vertical === null ? null : angleConfidence,
    },
    {
      id: 'launch_h',
      label: t('metric.hLaunch'),
      value: formatOptionalAngle(shot.launch_angle_horizontal, true),
      unit: angleUnit(shot.launch_angle_horizontal),
      subtext: horizontalLaunchIsCameraAssisted ? 'camera assisted' : undefined,
      estimated: markEstimated(shot.launch_angle_horizontal !== null && angleEstimated),
      confidence: shot.launch_angle_horizontal === null ? null : angleConfidence,
      confidenceLabel: horizontalLaunchIsCameraAssisted ? 'experimental' : undefined,
    },
    {
      id: 'spin',
      label: t('metric.spin'),
      value: formatSpinRpm(shot.spin_rpm),
      unit: shot.spin_rpm === null ? undefined : 'rpm',
      estimated: markEstimated(shot.spin_rpm !== null && shot.spin_source === 'calculated'),
      confidence: shot.spin_rpm === null ? null : shot.spin_quality,
    },
    {
      id: 'spin_axis',
      label: t('metric.spinAxis'),
      value: formatOptionalAngle(shot.spin_axis_deg, true),
      unit: angleUnit(shot.spin_axis_deg),
      subtext: shotShape(shot.spin_axis_deg),
    },
    {
      id: 'club_path',
      label: t('metric.clubPath'),
      value: formatOptionalAngle(clubPath.value, true),
      unit: angleUnit(clubPath.value),
      subtext: clubPath.note,
      confidence: clubPath.confidence,
      confidenceLabel: clubPath.confidenceLabel,
    },
    {
      id: 'club_aoa',
      label: t('metric.clubAoa'),
      value: formatOptionalAngle(attack.value, true),
      unit: angleUnit(attack.value),
      subtext: attack.note,
      confidence: attack.confidence,
      confidenceLabel: attack.confidenceLabel,
    },
  ];
}

function buildSwingSpeedMetrics(shot: Shot, stats: SwingSpeedStats, unitSystem: UnitSystem): LiveMetric[] {
  const speedUnit = getSpeedUnit(unitSystem);

  return [
    {
      id: 'swing_last',
      label: t('metric.lastSwing'),
      value: formatSpeed(getSwingSpeedMph(shot), unitSystem, 1),
      unit: speedUnit,
    },
    {
      id: 'swing_best',
      label: t('metric.best'),
      value: formatSpeed(stats.best_speed_mph, unitSystem, 1),
      unit: speedUnit,
      subtext: t('metric.profileImplement'),
    },
    {
      id: 'swing_avg',
      label: t('metric.average'),
      value: formatSpeed(stats.avg_speed_mph, unitSystem, 1),
      unit: speedUnit,
      subtext: t('metric.swingsCount', { count: stats.count }),
    },
    {
      id: 'swing_count',
      label: t('metric.swings'),
      value: String(stats.count),
      subtext:
        shot.swing_speed_reading_count === undefined
          ? undefined
          : t('metric.readingsCount', { count: shot.swing_speed_reading_count }),
    },
    {
      id: 'swing_implement',
      label: t('metric.implement'),
      value: shot.training_implement_label ?? shot.club,
      subtext:
        shot.swing_speed_trigger_mph === undefined
          ? undefined
          : t('metric.trigger', {
              speed: formatSpeed(shot.swing_speed_trigger_mph, unitSystem, 1),
              unit: speedUnit,
            }),
    },
  ];
}

/**
 * Build the fixed metric list for the Live panel. Returns {@link LIVE_METRIC_COUNT}
 * entries for a ball-strike shot and {@link SWING_METRIC_COUNT} for a swing-speed
 * one; the two sets share no ids, so a selected-metric choice never leaks across
 * modes. A radar-only shot keeps the {@link RADAR_ONLY_METRIC_IDS} subset of the
 * ball-strike ten.
 */
export function buildLiveMetrics(shot: Shot, unitSystem: UnitSystem, swingStats: SwingSpeedStats): LiveMetric[] {
  if (isSwingSpeedShot(shot)) {
    return buildSwingSpeedMetrics(shot, swingStats, unitSystem);
  }
  const metrics = buildBallStrikeMetrics(shot, unitSystem);
  return isRadarOnlyShot(shot) ? metrics.filter((metric) => RADAR_ONLY_METRIC_IDS.includes(metric.id)) : metrics;
}

/**
 * Put the selected metric first (top-left of the table) and keep the rest in
 * canonical order. Falls back to the first metric when `selectedId` is absent
 * from this list — which is the normal case right after switching between
 * ball-strike and swing-speed modes.
 */
export function pinSelectedMetric(metrics: LiveMetric[], selectedId: string | null): LiveMetric[] {
  if (metrics.length === 0) {
    return [];
  }

  const selectedIndex = metrics.findIndex((metric) => metric.id === selectedId);
  const index = selectedIndex === -1 ? 0 : selectedIndex;

  return [metrics[index], ...metrics.filter((_, i) => i !== index)];
}

// Phone lead tiles: the pinned metric plus carry, or plus the next metric when
// carry is pinned or absent (swing speed). Takes pinSelectedMetric's order.
export function leadMetricIds(metrics: LiveMetric[]): string[] {
  if (metrics.length === 0) {
    return [];
  }

  const [pinned, ...rest] = metrics;
  const partner = rest.find((metric) => metric.id === 'carry') ?? rest[0];
  return partner ? [pinned.id, partner.id] : [pinned.id];
}
