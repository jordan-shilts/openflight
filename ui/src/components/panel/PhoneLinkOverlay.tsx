import { useEffect, useState } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { fetchPhoneLink } from '../../services/phoneLink';
import { choosePhoneLink, type PhoneLink, type PhoneLinkChoice } from '../../utils/phoneLink';
import { QrCode } from '../ui/QrCode';
import { PanelAction } from './PanelAction';
import './PhoneLinkOverlay.css';

/** Still asking, an address to show, or (null) nothing a phone could reach. */
export type PhoneLinkLookup = 'loading' | PhoneLinkChoice | null;

interface PhoneLinkOverlayProps {
  onClose: () => void;
}

/** "Open on your phone": asks the server for its LAN address each time it opens. */
export function PhoneLinkOverlay({ onClose }: PhoneLinkOverlayProps) {
  const [attempt, setAttempt] = useState(0);
  const [answer, setAnswer] = useState<{ attempt: number; link: PhoneLink | null } | null>(null);

  useEffect(() => {
    let stale = false;
    void fetchPhoneLink().then((link) => {
      if (!stale) setAnswer({ attempt, link });
    });
    return () => {
      stale = true;
    };
  }, [attempt]);

  const lookup: PhoneLinkLookup = answer?.attempt === attempt ? choosePhoneLink(answer.link) : 'loading';

  return <PhoneLinkView lookup={lookup} onRetry={() => setAttempt((current) => current + 1)} onClose={onClose} />;
}

interface PhoneLinkViewProps extends PhoneLinkOverlayProps {
  lookup: PhoneLinkLookup;
  onRetry: () => void;
}

/** The QR code for the kiosk's address, the address as text, and three steps. */
export function PhoneLinkView({ lookup, onRetry, onClose }: PhoneLinkViewProps) {
  const { t } = useI18n();
  const title = t('menu.openOnPhone');

  let body;
  if (lookup === 'loading') {
    body = <p className="phone-link__status">{t('phone.loading')}</p>;
  } else if (lookup === null) {
    body = (
      <div className="phone-link__status" role="alert">
        <p>{t('phone.unavailable')}</p>
        <PanelAction variant="secondary" onClick={onRetry}>
          {t('phone.retry')}
        </PanelAction>
      </div>
    );
  } else {
    const { primary, alternates } = lookup;
    body = (
      <>
        <QrCode value={primary.url} label={t('phone.qrLabel', { url: primary.url })} />
        <div className="phone-link__guide">
          <ol className="phone-link__steps">
            <li>{t('phone.stepWifi')}</li>
            <li>{t('phone.stepScan')}</li>
            <li>{t('phone.stepInstall')}</li>
          </ol>
          <p className="phone-link__url">{primary.url}</p>
          {alternates.length > 0 ? (
            <p className="phone-link__alternates">
              {t('phone.alternates')}
              {alternates.map((alternate) => (
                <span key={alternate.url}>{alternate.url}</span>
              ))}
            </p>
          ) : null}
          <p className="phone-link__note">{t('phone.note')}</p>
        </div>
      </>
    );
  }

  return (
    <div className="picker-overlay phone-link" role="dialog" aria-modal="true" aria-label={title}>
      <div className="picker-overlay__header">
        <span className="picker-overlay__title">{title}</span>
        <button
          type="button"
          className="picker-overlay__close"
          onClick={onClose}
          aria-label={t('picker.close', { title })}
        >
          ✕
        </button>
      </div>
      <div className="phone-link__body">{body}</div>
    </div>
  );
}
