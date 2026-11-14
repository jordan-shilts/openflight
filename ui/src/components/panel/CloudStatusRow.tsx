import { useI18n } from '../../i18n/useI18n';
import type { CloudStatus } from '../../types/socket';
import { cloudStatusText, cloudStatusTone } from './cloudStatus';

interface CloudStatusRowProps {
  /** Null until the server reports it: then the row is left out. */
  status: CloudStatus | null;
}

/** The menu's "Cloud" line: Supabase upload state and shots waiting, in plain words. */
export function CloudStatusRow({ status }: CloudStatusRowProps) {
  const { t } = useI18n();
  if (!status) {
    return null;
  }
  return (
    <div className="menu-sheet__status-row menu-sheet__status-row--stacked">
      <span className="menu-sheet__status-label">{t('menu.cloud')}</span>
      <span className={`menu-sheet__cloud menu-sheet__cloud--${cloudStatusTone(status)}`} role="status">
        <span className="menu-sheet__cloud-dot" aria-hidden="true" />
        {cloudStatusText(status, t)}
      </span>
    </div>
  );
}
