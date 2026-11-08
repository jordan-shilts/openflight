import type { Shot } from '../../types/shot';

/**
 * Where a club-delivery number comes from. `measured` is a sensor reading,
 * `experimental` a quality-gated camera or radar estimate, and `calculated`
 * derived from measured launch and delivery through the D-plane relation below.
 */
export type DeliverySource = 'measured' | 'experimental' | 'calculated';

export type DeliveryConfidence = 'high' | 'medium' | 'low' | 'withheld' | 'experimental';

export interface DeliveryAngle {
  value: number | null;
  source: DeliverySource;
  /** Short provenance note, e.g. "camera fused" or "rejected: lighting quality". */
  note?: string;
  confidence: DeliveryConfidence | null;
  /** Overrides the confidence copy while keeping its dot level. */
  confidenceLabel?: string;
}

/** Turns an experimental pipeline status into short display copy. */
export function experimentalStatus(status: string | null | undefined): string {
  if (!status || status === 'candidate_available') return 'candidate';
  return status.replace(/^rejected_/, 'rejected: ').replaceAll('_', ' ');
}

interface SensorAngleFields {
  measured: number | null;
  fused: number | null | undefined;
  experimental: number | null | undefined;
  experimentalStatus: string | null | undefined;
  fusedConfidence: Exclude<DeliveryConfidence, 'experimental'> | null | undefined;
}

/**
 * A measured club angle wins. Otherwise the camera-fused estimate, and the
 * radar-only experimental one only when fusion was never attempted.
 */
function selectSensorAngle(shot: Shot, fields: SensorAngleFields): DeliveryAngle {
  const fusedAttempted = shot.experimental_fused_status != null;
  const value = fields.measured ?? fields.fused ?? (!fusedAttempted ? fields.experimental : null) ?? null;
  const experimental =
    fields.measured === null &&
    (fields.fused != null || fusedAttempted || fields.experimental != null || fields.experimentalStatus != null);
  const note =
    fields.measured !== null
      ? undefined
      : fusedAttempted
        ? fields.fused != null
          ? 'camera fused'
          : experimentalStatus(shot.experimental_fused_status)
        : experimental
          ? experimentalStatus(fields.experimentalStatus)
          : undefined;
  return {
    value,
    source: experimental ? 'experimental' : 'measured',
    note,
    confidence: experimental ? (fields.fusedConfidence ?? 'experimental') : null,
    confidenceLabel: fields.fusedConfidence ? 'experimental' : undefined,
  };
}

export function selectClubPath(shot: Shot): DeliveryAngle {
  return selectSensorAngle(shot, {
    measured: shot.club_path_deg,
    fused: shot.experimental_fused_club_path_deg,
    experimental: shot.experimental_club_path_deg,
    experimentalStatus: shot.experimental_club_path_status,
    fusedConfidence: shot.experimental_fused_club_path_confidence,
  });
}

export function selectAttackAngle(shot: Shot): DeliveryAngle {
  return selectSensorAngle(shot, {
    measured: shot.club_angle_deg,
    fused: shot.experimental_fused_attack_angle_deg,
    experimental: shot.experimental_attack_angle_deg,
    experimentalStatus: shot.experimental_attack_angle_status,
    fusedConfidence: shot.experimental_fused_attack_angle_confidence,
  });
}

const WEDGES = new Set(['pw', 'gw', 'sw', 'lw']);
const SHORT_IRONS = new Set(['8-iron', '9-iron']);

/**
 * D-plane share of the ball's start direction (and launch) that follows the
 * face rather than the club's delivery, by club. Published fits put it near
 * 85% for a driver and 75% for irons; a putter starts the ball almost on the face.
 */
export function faceShare(clubId: string | null | undefined): number {
  const id = (clubId ?? '').toLowerCase();
  if (id === 'putter') return 0.9;
  if (id === 'driver') return 0.85;
  if (id.endsWith('-wood')) return 0.82;
  if (id.endsWith('-hybrid')) return 0.8;
  if (WEDGES.has(id) || SHORT_IRONS.has(id)) return 0.74;
  if (id.endsWith('-iron')) return 0.77;
  return 0.78;
}

/** Inverts start = share * face + (1 - share) * delivery for the face term. */
function faceFromStart(start: number, delivery: number, share: number): number {
  return (start - (1 - share) * delivery) / share;
}

function calculated(value: number | null, note?: string): DeliveryAngle {
  return { value, source: 'calculated', note: value === null ? note : undefined, confidence: null };
}

function isLaunchMeasured(shot: Shot, axis: 'vertical' | 'horizontal'): boolean {
  const launch = axis === 'vertical' ? shot.launch_angle_vertical : shot.launch_angle_horizontal;
  return launch !== null && shot.angle_source !== 'estimated';
}

export interface ClubDelivery {
  clubId: string;
  faceShare: number;
  /** Top view: horizontal delivery. */
  faceAngle: DeliveryAngle;
  clubPath: DeliveryAngle;
  faceToPath: DeliveryAngle;
  /** Side view: vertical delivery. */
  attackAngle: DeliveryAngle;
  dynamicLoft: DeliveryAngle;
  spinLoft: DeliveryAngle;
}

/**
 * Club delivery for one shot. Face angle and dynamic loft come from measured
 * launch and the measured (or experimental) path and attack angle; with an
 * estimated launch they would only echo the estimate, so they stay empty.
 */
export function buildClubDelivery(shot: Shot): ClubDelivery {
  const share = faceShare(shot.club);
  const clubPath = selectClubPath(shot);
  const attackAngle = selectAttackAngle(shot);

  const faceValue =
    isLaunchMeasured(shot, 'horizontal') && clubPath.value !== null
      ? faceFromStart(shot.launch_angle_horizontal as number, clubPath.value, share)
      : null;
  const loftValue =
    isLaunchMeasured(shot, 'vertical') && attackAngle.value !== null
      ? faceFromStart(shot.launch_angle_vertical as number, attackAngle.value, share)
      : null;
  const missingTop = clubPath.value === null ? 'needs club path' : 'needs measured launch';
  const missingSide = attackAngle.value === null ? 'needs attack angle' : 'needs measured launch';

  return {
    clubId: shot.club,
    faceShare: share,
    faceAngle: calculated(faceValue, missingTop),
    clubPath,
    faceToPath: calculated(
      faceValue === null || clubPath.value === null ? null : faceValue - clubPath.value,
      missingTop
    ),
    attackAngle,
    dynamicLoft: calculated(loftValue, missingSide),
    spinLoft: calculated(
      loftValue === null || attackAngle.value === null ? null : loftValue - attackAngle.value,
      missingSide
    ),
  };
}
