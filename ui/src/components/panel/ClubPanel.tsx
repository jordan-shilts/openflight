import { useMemo, useRef, type ReactNode } from 'react';
import { useDragScroll } from '../../hooks/useDragScroll';
import type { Shot } from '../../types/shot';
import { filterShotsByProfile } from '../../types/shot';
import { getClubName } from '../../data/clubs';
import { useI18n } from '../../i18n/useI18n';
import type { MessageKey } from '../../i18n';
import { useUnitPreference } from '../../state/useUnitPreference';
import { formatSpeed, getSpeedUnit } from '../../utils/units';
import { PanelHeader } from './PanelHeader';
import { NO_VALUE } from './liveMetrics';
import { buildClubDelivery, type DeliveryAngle, type DeliverySource } from './clubDelivery';

/** Path, face and attack are a few degrees; draw them larger so they read. */
export const DIAGRAM_SCALE = 3;
const MAX_DRAWN_DEG = 35;

interface ClubPanelProps {
  shots: Shot[];
  profileId: string;
  profileName: string;
  clubLabel: string;
  /** Pinned header control, e.g. Change club. */
  headerAction?: ReactNode;
}

interface Reading {
  id: string;
  label: string;
  value: string;
  unit?: string;
  source: DeliverySource;
  note?: string;
}

const BADGE_KEYS: Record<DeliverySource, MessageKey> = {
  measured: 'club.badgeMeasured',
  experimental: 'club.badgeExperimental',
  calculated: 'club.badgeCalculated',
};

function formatAngle(value: number | null, signed: boolean): string {
  if (value === null) return NO_VALUE;
  return `${signed && value >= 0 ? '+' : ''}${value.toFixed(1)}`;
}

function angleReading(id: string, label: string, angle: DeliveryAngle, signed = true): Reading {
  return {
    id,
    label,
    value: formatAngle(angle.value, signed),
    unit: angle.value === null ? undefined : '°',
    source: angle.source,
    note: angle.note,
  };
}

function drawn(deg: number | null, scale = DIAGRAM_SCALE): number | null {
  if (deg === null) return null;
  return Math.max(-MAX_DRAWN_DEG, Math.min(MAX_DRAWN_DEG, deg * scale));
}

const rad = (deg: number) => (deg * Math.PI) / 180;
const pt = (x: number, y: number) => `${x.toFixed(1)},${y.toFixed(1)}`;

function ArrowMarker({ id, className }: { id: string; className: string }) {
  return (
    <marker id={id} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" className={className} />
    </marker>
  );
}

/** Looking down, target up the page: path through the ball, face square to its aim. */
function TopDiagram({ path, face }: { path: number | null; face: number | null }) {
  const ball = { x: 100, y: 30, r: 9 };
  const contact = { x: ball.x, y: ball.y + ball.r + 2 };
  const pathDeg = drawn(path);
  const faceDeg = drawn(face);
  return (
    <svg viewBox="0 0 200 120" className="club-panel__svg">
      <defs>
        <ArrowMarker id="club-top-path-arrow" className="club-panel__fill-path" />
        <ArrowMarker id="club-top-face-arrow" className="club-panel__fill-face" />
      </defs>
      <line x1="100" y1="4" x2="100" y2="116" className="club-panel__guide" />
      {pathDeg !== null ? (
        <line
          x1={contact.x - Math.sin(rad(pathDeg)) * 78}
          y1={contact.y + Math.cos(rad(pathDeg)) * 78}
          x2={contact.x - Math.sin(rad(pathDeg)) * 6}
          y2={contact.y + Math.cos(rad(pathDeg)) * 6}
          className="club-panel__path"
          markerEnd="url(#club-top-path-arrow)"
        />
      ) : null}
      {faceDeg !== null ? (
        <g transform={`rotate(${faceDeg.toFixed(1)} ${contact.x} ${contact.y + 4})`}>
          <line
            x1={contact.x}
            y1={contact.y + 4}
            x2={contact.x}
            y2={contact.y - 34}
            className="club-panel__aim"
            markerEnd="url(#club-top-face-arrow)"
          />
          <line
            x1={contact.x - 24}
            y1={contact.y + 4}
            x2={contact.x + 24}
            y2={contact.y + 4}
            className="club-panel__face"
          />
        </g>
      ) : null}
      <circle cx={ball.x} cy={ball.y} r={ball.r} className="club-panel__ball" />
    </svg>
  );
}

/** From the side, target to the right: attack into the ball, face leaning back by its loft. */
function SideDiagram({ attack, loft }: { attack: number | null; loft: number | null }) {
  const ground = 104;
  const ball = { x: 132, y: ground - 9, r: 9 };
  const contact = { x: ball.x - ball.r - 2, y: ball.y };
  const attackDeg = drawn(attack);
  const loftDeg = loft === null ? null : Math.max(0, Math.min(60, loft));
  return (
    <svg viewBox="0 0 200 120" className="club-panel__svg">
      <defs>
        <ArrowMarker id="club-side-attack-arrow" className="club-panel__fill-path" />
        <ArrowMarker id="club-side-loft-arrow" className="club-panel__fill-face" />
      </defs>
      <line x1="6" y1={ground} x2="194" y2={ground} className="club-panel__ground" />
      {attackDeg !== null ? (
        <line
          x1={contact.x - Math.cos(rad(attackDeg)) * 82}
          y1={contact.y + Math.sin(rad(attackDeg)) * 82}
          x2={contact.x - Math.cos(rad(attackDeg)) * 6}
          y2={contact.y + Math.sin(rad(attackDeg)) * 6}
          className="club-panel__path"
          markerEnd="url(#club-side-attack-arrow)"
        />
      ) : null}
      {loftDeg !== null ? (
        <g>
          <line
            x1={contact.x}
            y1={contact.y}
            x2={contact.x + Math.cos(rad(loftDeg)) * 44}
            y2={contact.y - Math.sin(rad(loftDeg)) * 44}
            className="club-panel__aim"
            markerEnd="url(#club-side-loft-arrow)"
          />
          <polyline
            points={`${pt(contact.x + Math.sin(rad(loftDeg)) * 8, contact.y + Math.cos(rad(loftDeg)) * 8)} ${pt(
              contact.x - Math.sin(rad(loftDeg)) * 34,
              contact.y - Math.cos(rad(loftDeg)) * 34
            )}`}
            className="club-panel__face"
          />
        </g>
      ) : null}
      <circle cx={ball.x} cy={ball.y} r={ball.r} className="club-panel__ball" />
    </svg>
  );
}

type FaceShape = 'wood' | 'putter' | 'iron';

function faceShape(clubId: string): FaceShape {
  if (clubId === 'putter') return 'putter';
  if (clubId === 'driver' || clubId.endsWith('-wood')) return 'wood';
  return 'iron';
}

/** Looking at the face. No contact mark: impact location isn't measured yet. */
function FaceDiagram({ clubId }: { clubId: string }) {
  const shape = faceShape(clubId);
  const outline =
    shape === 'wood'
      ? 'M38,46 C40,24 160,20 168,40 C176,62 168,92 150,96 L52,96 C36,92 34,70 38,46 Z'
      : shape === 'putter'
        ? 'M26,52 L174,52 L174,84 L26,84 Z'
        : 'M34,42 L168,26 C176,26 178,32 176,40 L168,94 L40,96 C32,96 28,90 30,82 Z';
  const grooves = shape === 'iron' ? [48, 58, 68, 78, 88] : [];
  return (
    <svg viewBox="0 0 200 120" className="club-panel__svg">
      <path d={outline} className="club-panel__outline" />
      {grooves.map((y) => (
        <line key={y} x1="48" y1={y} x2="158" y2={y - 6} className="club-panel__groove" />
      ))}
      <line x1="100" y1="54" x2="100" y2="84" className="club-panel__guide" />
      <line x1="84" y1="69" x2="116" y2="69" className="club-panel__guide" />
    </svg>
  );
}

function SourceBadge({ source }: { source: DeliverySource }) {
  const { t } = useI18n();
  return <span className={`club-panel__badge club-panel__badge--${source}`}>{t(BADGE_KEYS[source])}</span>;
}

function ClubCard({
  id,
  title,
  diagram,
  readings,
  footnote,
}: {
  id: string;
  title: string;
  diagram: ReactNode;
  readings: Reading[];
  footnote?: string;
}) {
  return (
    <section className={`club-panel__card club-panel__card--${id}`} aria-label={title}>
      <div className="club-panel__diagram" aria-hidden="true">
        {diagram}
      </div>
      <h3 className="club-panel__card-title">{title}</h3>
      <div className="club-panel__readings">
        {readings.map((reading) => (
          <div key={reading.id} className="club-panel__reading" data-reading={reading.id}>
            <span className="club-panel__reading-label">{reading.label}</span>
            <span className="club-panel__reading-value">
              {reading.value}
              {reading.unit ? <span className="club-panel__reading-unit">{reading.unit}</span> : null}
            </span>
            <SourceBadge source={reading.source} />
            {reading.note ? <span className="club-panel__reading-note">{reading.note}</span> : null}
          </div>
        ))}
      </div>
      {footnote ? <p className="club-panel__footnote">{footnote}</p> : null}
    </section>
  );
}

/**
 * Club delivery for the active golfer's latest shot: a top view (face, path,
 * face to path), a side view (attack, dynamic loft, spin loft) and the strike.
 * Every number carries its source, so measured, experimental and calculated
 * values are never confused.
 */
export function ClubPanel({ shots, profileId, profileName, clubLabel, headerAction }: ClubPanelProps) {
  const { t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const profileShots = useMemo(() => filterShotsByProfile(shots, profileId), [shots, profileId]);
  const shot = profileShots[profileShots.length - 1] ?? null;
  const delivery = useMemo(() => (shot ? buildClubDelivery(shot) : null), [shot]);
  // Cards stack and scroll on a portrait phone; the kiosk fits them in one row.
  const bodyRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(bodyRef);
  const header = <PanelHeader title={t('nav.club')} subtitle={profileName} club={clubLabel} actions={headerAction} />;

  if (!shot || !delivery) {
    return (
      <div className="panel">
        {header}
        <div className="panel__body panel__body--empty">
          <span className="panel__empty-title">{t('club.empty')}</span>
          <span className="panel__empty-detail">{t('club.emptyDetail')}</span>
        </div>
      </div>
    );
  }

  const speedUnit = getSpeedUnit(unitSystem);
  const speed = (id: string, label: string, mph: number | null): Reading => ({
    id,
    label,
    value: mph === null ? NO_VALUE : formatSpeed(mph, unitSystem, 1),
    unit: mph === null ? undefined : speedUnit,
    source: 'measured',
  });
  const shotNumber = shot.shot_number ?? profileShots.length;

  return (
    <div className="panel club-panel">
      {header}
      <div
        ref={bodyRef}
        className="panel__body club-panel__body"
        onPointerDown={dragScroll.onPointerDown}
        onPointerMove={dragScroll.onPointerMove}
        onPointerUp={dragScroll.onPointerUp}
        onPointerCancel={dragScroll.onPointerCancel}
        onClickCapture={dragScroll.onClickCapture}
      >
        <div className="club-panel__cards">
          <ClubCard
            id="top"
            title={t('club.top')}
            diagram={<TopDiagram path={delivery.clubPath.value} face={delivery.faceAngle.value} />}
            readings={[
              angleReading('face_angle', t('club.faceAngle'), delivery.faceAngle),
              angleReading('club_path', t('metric.clubPath'), delivery.clubPath),
              angleReading('face_to_path', t('club.faceToPath'), delivery.faceToPath),
            ]}
          />
          <ClubCard
            id="side"
            title={t('club.side')}
            diagram={<SideDiagram attack={delivery.attackAngle.value} loft={delivery.dynamicLoft.value} />}
            readings={[
              angleReading('attack_angle', t('club.attackAngle'), delivery.attackAngle),
              angleReading('dynamic_loft', t('club.dynamicLoft'), delivery.dynamicLoft, false),
              angleReading('spin_loft', t('club.spinLoft'), delivery.spinLoft, false),
            ]}
          />
          <ClubCard
            id="strike"
            title={t('club.strike')}
            diagram={<FaceDiagram clubId={delivery.clubId} />}
            readings={[
              speed('club_speed', t('metric.clubSpeed'), shot.club_speed_mph),
              speed('ball_speed', t('metric.ballSpeed'), shot.ball_speed_mph),
              {
                id: 'smash',
                label: t('metric.smash'),
                value: shot.smash_factor === null ? NO_VALUE : shot.smash_factor.toFixed(2),
                source: 'measured',
              },
            ]}
            footnote={t('club.impactNote')}
          />
        </div>
        <p className="club-panel__legend">
          <span className="club-panel__legend-shot">
            {t('club.shot', { number: shotNumber, club: getClubName(delivery.clubId) })}
          </span>
          <span>{t('club.legend', { share: Math.round(delivery.faceShare * 100), scale: DIAGRAM_SCALE })}</span>
        </p>
      </div>
    </div>
  );
}
