import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import type { PhoneLinkChoice } from '../../utils/phoneLink';
import { PhoneLinkOverlay, PhoneLinkView, type PhoneLinkLookup } from './PhoneLinkOverlay';

const noop = () => {};

const choice: PhoneLinkChoice = {
  primary: { kind: 'ipv4', host: '192.168.1.23', url: 'http://192.168.1.23:8080' },
  alternates: [{ kind: 'mdns', host: 'openflight.local', url: 'http://openflight.local:8080' }],
};

function render(lookup: PhoneLinkLookup) {
  return renderToString(<PhoneLinkView lookup={lookup} onRetry={noop} onClose={noop} />);
}

describe('PhoneLinkView', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('is a dialog named for what it does, with a way out', () => {
    const html = render(choice);

    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('aria-label="Open on your phone"');
    expect(html).toContain('aria-label="Close Open on your phone"');
  });

  it('shows the QR code for the chosen address and the address as text', () => {
    const html = render(choice);

    expect(html).toContain('class="qr-code"');
    expect(html).toContain('aria-label="QR code for http://192.168.1.23:8080"');
    expect(html).toMatch(/phone-link__url">http:\/\/192\.168\.1\.23:8080</);
  });

  it('draws the code black on white whatever the theme', () => {
    const html = render(choice);

    expect(html).toMatch(/<rect width="(\d+)" height="\1" fill="#ffffff"/);
    expect(html).toMatch(/<path d="M\d+ \d+h1v1h-1z[^"]*" fill="#000000"/);
    expect(html).not.toContain('var(--color');
  });

  it('gives the three steps in order', () => {
    const html = render(choice);
    const steps = [...html.matchAll(/<li>([^<]+)<\/li>/g)].map((match) => match[1]);

    expect(steps).toEqual([
      'Join the same Wi-Fi as this screen.',
      'Scan the code with your phone camera.',
      'Tap Share (or the ⋮ menu), then Add to Home Screen.',
    ]);
  });

  it('offers the .local name in case the address does not load', () => {
    const html = render(choice);

    expect(html).toContain('If it does not load, try');
    expect(html).toContain('<span>http://openflight.local:8080</span>');
  });

  it('leaves the alternates line out when there are none', () => {
    const html = render({ ...choice, alternates: [] });

    expect(html).not.toContain('phone-link__alternates');
  });

  it('says it only works on this Wi-Fi and that anyone on it can open it', () => {
    expect(render(choice)).toContain('Works on this Wi-Fi only. Anyone on it can open the app.');
  });

  it('says it is looking while the server has not answered', () => {
    const html = render('loading');

    expect(html).toContain('Finding this screen&#x27;s address…');
    expect(html).not.toContain('qr-code');
  });

  it('explains and offers a retry when no phone could reach the kiosk', () => {
    const html = render(null);

    expect(html).toContain('role="alert"');
    expect(html).toContain('No address found that a phone can reach.');
    expect(html).toMatch(/panel-action--secondary[^>]*>Try again</);
    expect(html).not.toContain('qr-code');
  });

  it('follows the chosen language', () => {
    setActiveLocale('es');

    const html = render(choice);

    expect(html).toContain('aria-label="Abrir en tu teléfono"');
    expect(html).toContain('Escanea el código con la cámara del teléfono.');
  });
});

describe('PhoneLinkOverlay', () => {
  it('starts out looking for the address', () => {
    const html = renderToString(<PhoneLinkOverlay onClose={noop} />);

    expect(html).toContain('Finding this screen&#x27;s address…');
  });
});
