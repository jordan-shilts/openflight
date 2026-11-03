import { useI18n } from '../i18n/useI18n';
import type { SimStatus as SimStatusData } from '../types/socket';
import './SimStatus.css';

const DISPLAY_NAMES: Record<string, string> = {
  gspro: 'GSPro',
  opengolfsim: 'OpenGolfSim',
  partee: 'PAR-TEE',
};

// Map a connection state to a visual severity bucket.
function severity(state: SimStatusData['state']): 'ok' | 'warn' | 'error' | 'off' {
  switch (state) {
    case 'connected':
      return 'ok';
    case 'connecting':
    case 'reconnecting':
      return 'warn';
    case 'error':
      return 'error';
    default:
      return 'off';
  }
}

// What the connector is connected to: the simulator's own name when it reported one
// (Open-Birdie speaks the GSPro protocol but isn't GSPro), else the protocol's sim.
function displayName(s: SimStatusData): string {
  return s.peer || DISPLAY_NAMES[s.target] || s.target;
}

function pillTitle(s: SimStatusData): string {
  const protocol = s.peer ? `${s.peer} via ${DISPLAY_NAMES[s.target] ?? s.target} protocol` : '';
  const where = [protocol, s.host ? `${s.host}:${s.port ?? ''}` : ''].filter(Boolean).join(' · ');
  if (s.state === 'reconnecting' && s.next_retry_in_s) {
    return `${where} — retry in ${s.next_retry_in_s}s (attempt ${s.attempt ?? ''})`.trim();
  }
  if (s.state === 'error' && s.message) {
    return `${where} — ${s.message}`.trim();
  }
  return where || s.state;
}

interface SimStatusProps {
  statuses: Record<string, SimStatusData>;
}

export function SimStatus({ statuses }: SimStatusProps) {
  const { t } = useI18n();
  const entries = Object.values(statuses);
  if (entries.length === 0) {
    return null; // No simulator connectors configured.
  }
  return (
    <div className="sim-status" role="group" aria-label={t('sim.connectors')}>
      {entries.map((s) => (
        <div key={s.target} className={`sim-status__pill sim-status__pill--${severity(s.state)}`} title={pillTitle(s)}>
          <span className="sim-status__dot" />
          <span className="sim-status__name">{displayName(s)}</span>
          <span className="sim-status__state">{s.state}</span>
        </div>
      ))}
    </div>
  );
}
