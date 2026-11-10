import { useMemo, type ReactNode } from 'react';
import type { Shot } from '../../types/shot';
import { computeSwingSpeedStats, filterShotsByProfile } from '../../types/shot';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useI18n } from '../../i18n/useI18n';
import { useSharedFitFontSize } from '../../hooks/useFitFontSize';
import { MetricCard } from '../ui/MetricCard';
import { FlightView } from './FlightView';
import { PanelHeader } from './PanelHeader';
import { buildLiveMetrics, leadMetricIds, pinSelectedMetric } from './liveMetrics';
import type { LiveView } from './liveView';

interface LivePanelProps {
  shot: Shot | null;
  shots: Shot[];
  profileId: string;
  profileName: string;
  clubLabel: string;
  /** Undefined outside swing-speed mode. Scopes the swing stats to one implement. */
  activeTrainingImplement?: string;
  /** Metric pinned top-left while the full table remains visible. */
  selectedMetricId?: string | null;
  onSelectMetric?: (id: string) => void;
  /** True for a freshly captured shot (not a restored session). */
  isNewShot?: boolean;
  /** Pinned header control, e.g. Change club. */
  headerAction?: ReactNode;
  playerStrip?: ReactNode;
  /** Ready / measuring line for the camera-triggered radar recorder. */
  statusBanner?: ReactNode;
  /** True while that recorder is reporting; the banner then speaks for readiness. */
  recorderLive?: boolean;
  view?: LiveView;
}

/**
 * Ten-metric table. Tapping a tile selects it: the title turns accent yellow
 * and the tile moves to the top-left without hiding the remaining metrics.
 */
export function LivePanel({
  shot,
  shots,
  profileId,
  profileName,
  clubLabel,
  activeTrainingImplement,
  selectedMetricId = null,
  onSelectMetric,
  isNewShot = false,
  headerAction,
  playerStrip,
  statusBanner,
  recorderLive = false,
  view = 'metrics',
}: LivePanelProps) {
  const { locale, t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const profileShots = useMemo(() => filterShotsByProfile(shots, profileId), [shots, profileId]);
  const displayedShot = profileShots[profileShots.length - 1] ?? null;
  const earlierShots = useMemo(() => profileShots.slice(0, -1), [profileShots]);
  const isProfileNewShot = Boolean(isNewShot && shot && displayedShot && shot.timestamp === displayedShot.timestamp);

  const swingStats = useMemo(
    () => computeSwingSpeedStats(profileShots, { profileId, trainingImplement: activeTrainingImplement }),
    [profileShots, profileId, activeTrainingImplement]
  );
  const metrics = useMemo(
    () =>
      displayedShot ? pinSelectedMetric(buildLiveMetrics(displayedShot, unitSystem, swingStats), selectedMetricId) : [],
    // locale is not read in the factory; t() is a stable import. Without it,
    // changing language would keep stale metric labels.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see above
    [displayedShot, unitSystem, swingStats, selectedMetricId, locale]
  );
  const selected = metrics[0] ?? null;
  const leadIds = leadMetricIds(metrics);
  const gridRef = useSharedFitFontSize(
    metrics.length > 0 && view === 'metrics',
    metrics.map((metric) => `${metric.value}:${metric.unit ?? ''}`).join('|')
  );
  const header = (
    <PanelHeader
      title={t('nav.live')}
      subtitle={profileName}
      club={clubLabel}
      actions={headerAction}
      toolbar={playerStrip}
    />
  );

  if (!selected) {
    return (
      <div className="panel">
        {header}
        {statusBanner}
        <div className="panel__body panel__body--empty live-panel__empty">
          {recorderLive ? (
            // The banner already says Ready or Measuring; don't say it twice.
            <span className="panel__empty-detail live-panel__empty-detail">{t('measuring.firstSwing')}</span>
          ) : (
            <>
              <span className="panel__empty-title live-panel__empty-title">{t('live.ready')}</span>
              <span className="panel__empty-detail live-panel__empty-detail">{t('live.readyDetail')}</span>
            </>
          )}
        </div>
      </div>
    );
  }

  if (view === 'flight' && displayedShot) {
    return (
      <div className="panel">
        {header}
        {statusBanner}
        <div className="panel__body live-panel__body live-panel__body--flight">
          <FlightView
            key={displayedShot.timestamp}
            shot={displayedShot}
            earlierShots={earlierShots}
            animate={isProfileNewShot}
            unitSystem={unitSystem}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      {header}
      {statusBanner}
      <div className="panel__body live-panel__body">
        {isProfileNewShot ? <div className="shot-flash" /> : null}
        <div ref={gridRef} className={`live-panel__grid live-panel__grid--of-${metrics.length}`}>
          {metrics.map((metric) => (
            <MetricCard
              key={metric.id}
              label={metric.label}
              value={metric.value}
              unit={metric.unit}
              subtext={metric.subtext}
              estimated={metric.estimated}
              confidence={metric.confidence}
              confidenceLabel={metric.confidenceLabel}
              labelPosition="above"
              selected={metric.id === selected.id}
              className={leadIds.includes(metric.id) ? 'live-panel__lead' : undefined}
              onClick={onSelectMetric ? () => onSelectMetric(metric.id) : undefined}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
