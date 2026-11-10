import type { CSSProperties } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { useMeasuringStore, type MeasuringNotice, type MeasuringState } from '../../stores/useMeasuringStore';
import './MeasuringBanner.css';

/** Used when the recorder did not say how long the radar read takes. */
const DEFAULT_WAIT_S = 7.3;

export interface MeasuringBannerViewProps {
  state: MeasuringState;
  expectedS: number | null;
  elapsedS: number;
  waitId: number;
  notice: MeasuringNotice | null;
}

const NOTICE_KEYS = {
  no_ball: 'measuring.noBall',
  failed: 'measuring.failed',
} as const;

/**
 * One line across the Live screen: green when the radar can take a swing,
 * filling amber while it reads the last one. A swing made during the read is
 * not measured, so the wait has to be readable from the mat. Renders nothing
 * when no recorder is reporting.
 */
export function MeasuringBannerView({ state, expectedS, elapsedS, waitId, notice }: MeasuringBannerViewProps) {
  const { t } = useI18n();
  if (state === 'offline') return null;

  if (state === 'measuring') {
    const total = expectedS && expectedS > 0 ? expectedS : DEFAULT_WAIT_S;
    const done = Math.min(Math.max(elapsedS / total, 0), 1);
    const fill = {
      '--measuring-from': done.toFixed(3),
      '--measuring-duration': `${Math.max(total - elapsedS, 0).toFixed(2)}s`,
    } as CSSProperties;
    return (
      <div className="measuring-banner measuring-banner--measuring" role="status" aria-live="polite">
        <span key={waitId} className="measuring-banner__fill" style={fill} aria-hidden="true" />
        <span className="measuring-banner__label">{t('measuring.busy')}</span>
        <span className="measuring-banner__detail">{t('measuring.busyDetail')}</span>
      </div>
    );
  }

  return (
    <div className="measuring-banner measuring-banner--ready" role="status" aria-live="polite">
      <span className="measuring-banner__dot" aria-hidden="true" />
      <span className="measuring-banner__label">{t('live.ready')}</span>
      <span className="measuring-banner__detail">{notice ? t(NOTICE_KEYS[notice]) : t('measuring.readyDetail')}</span>
    </div>
  );
}

export function MeasuringBanner() {
  const state = useMeasuringStore((store) => store.state);
  const expectedS = useMeasuringStore((store) => store.expectedS);
  const elapsedS = useMeasuringStore((store) => store.elapsedS);
  const waitId = useMeasuringStore((store) => store.waitId);
  const notice = useMeasuringStore((store) => store.notice);
  return (
    <MeasuringBannerView state={state} expectedS={expectedS} elapsedS={elapsedS} waitId={waitId} notice={notice} />
  );
}
