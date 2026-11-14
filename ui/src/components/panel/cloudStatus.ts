import type { MessageKey } from '../../i18n';
import type { CloudStatus } from '../../types/socket';

type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

export type CloudTone = 'ok' | 'warn' | 'error' | 'off';

/** One plain line for the menu: is the cloud reachable, and are shots waiting. */
export function cloudStatusText(status: CloudStatus, t: Translate): string {
  const pending = status.pending ?? 0;
  switch (status.state) {
    case 'online':
      if (pending === 0) return t('cloud.online');
      return pending === 1 ? t('cloud.onlineSendingOne') : t('cloud.onlineSending', { count: pending });
    case 'offline':
      if (pending === 0) return t('cloud.offlineNone');
      return pending === 1 ? t('cloud.offlineOne') : t('cloud.offline', { count: pending });
    case 'needs_attention':
      return status.reason === 'schema_missing' ? t('cloud.noTables') : t('cloud.notRegistered');
    default:
      return t('cloud.notSetUp');
  }
}

/** Severity for the status dot, matching the simulator pills. */
export function cloudStatusTone(status: CloudStatus): CloudTone {
  switch (status.state) {
    case 'online':
      return 'ok';
    case 'offline':
      return 'warn';
    case 'needs_attention':
      return 'error';
    default:
      return 'off';
  }
}
