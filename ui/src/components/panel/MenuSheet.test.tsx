import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { MenuSheet } from './MenuSheet';
import { useSystemStore } from '../../stores/useSystemStore';
import type { PowerStatus } from '../../types/power';

function renderMenu() {
  return renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} onOpenPhoneLink={() => {}} />);
}

describe('MenuSheet open on your phone', () => {
  const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

  it('offers the QR screen from the System section', () => {
    const html = renderMenu();

    expect(html).toMatch(/menu-sheet__section-title">System<.*menu-sheet__phone-link">Open on your phone</s);
  });

  it('is at least 44 CSS pixels tall for a finger', () => {
    expect(css).toMatch(/\.menu-sheet__phone-link,\n\.menu-sheet__shutdown \{\n {2}min-height: 44px;/);
  });

  it('is hidden when the app was launched from a phone home screen', () => {
    expect(css).toMatch(
      /@media \(display-mode: standalone\) \{\n {2}\.menu-sheet__phone-link \{\n {4}display: none;\n {2}\}\n\}/
    );
  });

  it('is hidden on a phone held upright', () => {
    const portraitRules = css.split('@media (max-width: 640px) and (orientation: portrait) {').slice(1).join('');

    expect(portraitRules).toMatch(/\.menu-sheet__phone-link \{\n {4}display: none;\n {2}\}/);
  });
});

describe('MenuSheet profiles', () => {
  it('does not manage profiles in the menu', () => {
    const html = renderMenu();

    expect(html).not.toContain('menu-sheet__section-title">Profile');
    expect(html).not.toContain('Add profile');
    expect(html).not.toContain('menu-sheet__input');
  });
});

describe('MenuSheet language', () => {
  it('offers a language dropdown with the shipped locales', () => {
    const html = renderMenu();

    expect(html).toContain('menu-sheet__section-title">Language');
    expect(html).toContain('aria-label="Language"');
    expect(html).toContain('>English</option>');
    expect(html).toContain('>Español</option>');
    expect(html).toContain('>Français</option>');
    expect(html).toContain('>Português</option>');
  });
});

describe('MenuSheet battery', () => {
  it('does not show battery in the menu, even when telemetry is present', () => {
    const powerStatus: PowerStatus = {
      available: true,
      provider: 'geekworm',
      state: 'on_battery',
      battery_percent: 64.2,
      battery_voltage_v: 3.81,
      external_power: false,
      updated_at: '2026-08-20T12:00:00+00:00',
      error: null,
    };
    useSystemStore.setState({ powerStatus });

    const html = renderMenu();

    expect(html).not.toContain('menu-sheet__status-label">Battery');
    expect(html).not.toContain('power-status');
    expect(html).not.toContain('64%');

    useSystemStore.setState({ powerStatus: null });
  });
});
