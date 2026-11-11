import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const uiPath = (relative: string) => fileURLToPath(new URL(`../${relative}`, import.meta.url));
const read = (relative: string) => readFileSync(uiPath(relative), 'utf8');

const html = read('index.html');
const manifest = JSON.parse(read('public/manifest.webmanifest')) as {
  name: string;
  short_name: string;
  start_url: string;
  scope: string;
  display: string;
  background_color: string;
  theme_color: string;
  icons: Array<{ src: string; sizes: string; type: string; purpose: string }>;
};

function darkToken(name: string): string {
  const dark = read('src/index.css').match(/\[data-theme='dark'\] \{([^}]*)\}/)?.[1] ?? '';
  return dark.match(new RegExp(`${name}: (#[0-9a-f]{6});`))?.[1] ?? `missing ${name}`;
}

function pngSize(relative: string): string {
  const data = readFileSync(uiPath(relative));
  expect(data.subarray(1, 4).toString('latin1'), relative).toBe('PNG');
  return `${data.readUInt32BE(16)}x${data.readUInt32BE(20)}`;
}

function metaContent(name: string): string | undefined {
  return html.match(new RegExp(`<meta name="${name}" content="([^"]*)"`))?.[1];
}

describe('web app manifest', () => {
  it('opens full screen at the root', () => {
    expect(manifest).toMatchObject({
      name: 'OpenFlight',
      short_name: 'OpenFlight',
      start_url: '/',
      scope: '/',
      display: 'standalone',
    });
  });

  it('takes its colours from the dark theme, so the splash matches the app', () => {
    expect(manifest.background_color).toBe(darkToken('--color-bg'));
    expect(manifest.theme_color).toBe(darkToken('--color-surface'));
  });

  it('lists a 192, a 512 and a maskable 512 icon', () => {
    expect(manifest.icons.map((icon) => `${icon.sizes} ${icon.purpose} ${icon.type}`)).toEqual([
      '192x192 any image/png',
      '512x512 any image/png',
      '512x512 maskable image/png',
    ]);
  });

  it('points at icon files of the size it claims', () => {
    for (const icon of manifest.icons) {
      expect(pngSize(`public${icon.src}`), icon.src).toBe(icon.sizes);
    }
  });
});

describe('index.html head', () => {
  it('links the manifest and a 180px apple-touch-icon that exists', () => {
    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest" />');
    expect(html).toContain('<link rel="apple-touch-icon" sizes="180x180" href="/icons/apple-touch-icon.png" />');
    expect(pngSize('public/icons/apple-touch-icon.png')).toBe('180x180');
  });

  it('asks both platforms to open it as an app', () => {
    expect(metaContent('apple-mobile-web-app-capable')).toBe('yes');
    expect(metaContent('mobile-web-app-capable')).toBe('yes');
    expect(metaContent('apple-mobile-web-app-title')).toBe('OpenFlight');
  });

  it('draws under the status bar and lets CSS read the safe-area insets', () => {
    expect(metaContent('apple-mobile-web-app-status-bar-style')).toBe('black-translucent');
    expect(metaContent('viewport')).toContain('viewport-fit=cover');
  });

  it('tints the browser chrome like the header', () => {
    expect(metaContent('theme-color')).toBe(darkToken('--color-surface'));
  });

  it('does not register a service worker, which plain HTTP cannot run', () => {
    expect(html).not.toContain('serviceWorker');
    expect(existsSync(uiPath('public/sw.js'))).toBe(false);
  });
});

describe('safe-area insets', () => {
  const panelCss = read('src/components/panel/panel.css');

  it('keeps the app shell clear of the notch with borders, so overlays follow', () => {
    expect(panelCss).toMatch(
      /\.panel-app \{[^}]*border-width: env\(safe-area-inset-top, 0px\) env\(safe-area-inset-right, 0px\) 0 env\(safe-area-inset-left, 0px\);/
    );
  });

  it('lifts the tab bar and full-screen sheets above the home indicator', () => {
    expect(panelCss).toMatch(/\.panel-footer \{[^}]*border-bottom: env\(safe-area-inset-bottom, 0px\) solid/);
    expect(panelCss).toMatch(
      /\.picker-overlay,\n\.profile-name-modal \{\n {2}padding-bottom: env\(safe-area-inset-bottom, 0px\);/
    );
    expect(panelCss).toMatch(/\.menu-sheet \{[^}]*margin-bottom: env\(safe-area-inset-bottom, 0px\);/);
  });

  it('keeps the status bar strip dark in both themes, where iOS draws white text', () => {
    const css = read('src/index.css');

    expect(css).toMatch(/\[data-theme='dark'\] \{[^}]*--color-status-bar: var\(--color-surface\);/);
    expect(css).toMatch(/\[data-theme='light'\] \{[^}]*--color-status-bar: var\(--color-text\);/);
  });
});
