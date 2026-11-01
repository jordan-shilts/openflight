import { useI18n } from '../../i18n/useI18n';
import { otherLiveView, type LiveView } from './liveView';
import { PanelAction } from './PanelAction';

interface LiveViewToggleProps {
  view: LiveView;
  onChange: (view: LiveView) => void;
}

/** Swaps Live between the metric table and the flight; upright phones show only the icon. */
export function LiveViewToggle({ view, onChange }: LiveViewToggleProps) {
  const { t } = useI18n();
  const opensFlight = view === 'metrics';
  const label = opensFlight ? t('live.showFlight') : t('live.showData');

  return (
    <PanelAction
      variant="secondary"
      className="live-view-toggle"
      aria-label={label}
      onClick={() => onChange(otherLiveView(view))}
    >
      <svg className="live-view-toggle__icon" viewBox="0 0 24 24" aria-hidden="true">
        {opensFlight ? (
          <>
            <path d="M3 19c3-11 12-13 17-3" />
            <circle cx="20" cy="16" r="1.6" />
            <path d="M2 21h20" />
          </>
        ) : (
          <>
            <rect x="3.5" y="3.5" width="7" height="7" />
            <rect x="13.5" y="3.5" width="7" height="7" />
            <rect x="3.5" y="13.5" width="7" height="7" />
            <rect x="13.5" y="13.5" width="7" height="7" />
          </>
        )}
      </svg>
      <span className="live-view-toggle__label">{label}</span>
    </PanelAction>
  );
}
