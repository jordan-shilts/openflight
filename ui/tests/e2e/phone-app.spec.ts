import { test, type Locator, type Page } from '@playwright/test';
import { expect, gotoApp, KIOSK_VIEWPORTS, resetSession, setClub, simulateShot, withControlSocket } from './helpers';

const PHONE = { width: 390, height: 844 };
/** An iPhone's notch and home indicator, as env(safe-area-inset-*) reports them. */
const SAFE_AREA = { top: 47, bottom: 34, left: 0, right: 0 };
/** Rows that scroll sideways by design; everything else has to fit the screen. */
const SIDEWAYS_SCROLLERS = '.panel-footer__tabs, .panel-chips';
const PHONE_URL = 'http://192.168.1.23:8080';

async function dismissPicker(page: Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

function footerTab(page: Page, label: string): Locator {
  return page.locator('.panel-footer__tabs').getByRole('button', { name: new RegExp(`^${label}`) });
}

async function openTab(page: Page, label: string) {
  const tab = footerTab(page, label);
  await tab.scrollIntoViewIfNeeded();
  await tab.tap();
  await expect(tab).toHaveAttribute('aria-pressed', 'true');
}

/** Anything pushed off the sides of the screen, or cut off inside a box that clips it. */
async function sidewaysOverflow(page: Page): Promise<string[]> {
  await page.evaluate(() => document.fonts.ready);
  return page.evaluate((scrollers) => {
    const viewport = document.documentElement.clientWidth;
    const problems: string[] = [];
    if (document.documentElement.scrollWidth > viewport) problems.push('the page scrolls sideways');
    for (const element of document.querySelectorAll('body *')) {
      if (!(element instanceof HTMLElement) || element.closest(scrollers)) continue;
      const rect = element.getBoundingClientRect();
      // Not drawn, or a 1px screen-reader-only label.
      if (rect.width <= 1 || rect.height <= 1) continue;
      const name = `${element.tagName.toLowerCase()}.${element.className}`;
      const style = getComputedStyle(element);
      if (rect.left < -1 || rect.right > viewport + 1) {
        problems.push(`${name} is off screen`);
      } else if (
        element.scrollWidth > element.clientWidth + 1 &&
        style.overflowX !== 'visible' &&
        style.textOverflow !== 'ellipsis'
      ) {
        problems.push(`${name} cuts off its content`);
      }
    }
    return problems;
  }, SIDEWAYS_SCROLLERS);
}

function overlap(a: { x: number; y: number; width: number; height: number } | null, b: typeof a): boolean {
  if (!a || !b) return false;
  return a.x < b.x + b.width - 1 && b.x < a.x + a.width - 1 && a.y < b.y + b.height - 1 && b.y < a.y + a.height - 1;
}

/** What each tab used to get wrong on a phone, checked on top of the shared rules. */
const TAB_CHECKS: Record<string, (page: Page) => Promise<void>> = {
  Live: async (page) => {
    await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(10);
  },
  Club: async (page) => {
    const cards = page.locator('.club-panel__card');
    await expect(cards).not.toHaveCount(0);
    const squeezed = await cards.evaluateAll(
      (all) => all.filter((card) => card.scrollHeight > card.clientHeight + 1).length
    );
    expect(squeezed).toBe(0);
  },
  Stats: async (page) => {
    const tiles = page.locator('.stats-panel__grid .metric-card');
    await expect(tiles).toHaveCount(6);
    const clipped = await tiles.evaluateAll(
      (cards) =>
        cards.filter((card) => {
          const row = card.querySelector('.metric-card__value-row');
          const unit = card.querySelector('.metric-card__unit') ?? card.querySelector('.metric-card__value');
          return !row || !unit || unit.getBoundingClientRect().right > card.getBoundingClientRect().right - 1;
        }).length
    );
    expect(clipped).toBe(0);
  },
  Shots: async (page) => {
    const row = page.locator('.shots-panel__row').first();
    const remove = await row.locator('.shots-panel__delete').boundingBox();
    expect(remove?.height).toBeGreaterThanOrEqual(44);
    const values = row.locator('.shots-panel__value');
    await expect(values).toHaveCount(5);
    for (const value of await values.all()) {
      expect(overlap(await value.boundingBox(), remove)).toBe(false);
    }
    expect(overlap(await row.locator('.shots-panel__profile-club').boundingBox(), remove)).toBe(false);
  },
  Camera: async (page) => {
    const message = await page.locator('.camera-feed__message p').boundingBox();
    expect(message!.x).toBeGreaterThanOrEqual(12);
    expect(message!.x + message!.width).toBeLessThanOrEqual(PHONE.width - 12);
  },
  Setup: async (page) => {
    const rows = page.locator('.setup-panel__row');
    await expect(rows).toHaveCount(4);
    for (const row of await rows.all()) {
      const label = row.locator('.setup-panel__label');
      expect(await label.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
      for (const control of await row.locator('button').all()) {
        expect(overlap(await label.boundingBox(), await control.boundingBox())).toBe(false);
      }
    }
  },
  Profiles: async (page) => {
    const name = page.locator('.profiles-panel__name').first();
    await expect(name).toHaveText('Profile 1');
    expect(await name.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  },
  Debug: async (page) => {
    await expect(page.getByText('Diagnostics')).toBeVisible();
  },
};

test.describe('phone held upright (390×844)', () => {
  test.use({ hasTouch: true, viewport: PHONE });

  test.beforeEach(async () => {
    await withControlSocket(async (socket) => {
      await resetSession(socket);
      await setClub(socket, 'driver');
      await simulateShot(socket);
      await simulateShot(socket);
    });
  });

  for (const [label, checkTab] of Object.entries(TAB_CHECKS)) {
    test(`${label} fits the screen and leaves the tab bar in reach`, async ({ page }) => {
      await gotoApp(page);
      await dismissPicker(page);
      await openTab(page, label);

      await checkTab(page);
      expect(await sidewaysOverflow(page)).toEqual([]);

      const footer = await page.locator('.panel-footer').boundingBox();
      expect(footer).not.toBeNull();
      expect(footer!.y + footer!.height).toBeLessThanOrEqual(PHONE.height);
      await expect(footerTab(page, label)).toBeInViewport();

      await openTab(page, label === 'Stats' ? 'Live' : 'Stats');
    });
  }

  test('keeps the club picker, menu and shutdown dialog inside the screen', async ({ page }) => {
    await gotoApp(page);
    await expect(page.getByRole('dialog', { name: 'Select club' })).toBeVisible();
    expect(await sidewaysOverflow(page)).toEqual([]);
    await page.getByRole('button', { name: 'Irons' }).tap();
    await expect(page.getByRole('button', { name: 'LW', exact: true })).toBeInViewport({ ratio: 1 });
    await dismissPicker(page);

    await page.getByRole('button', { name: 'Open menu' }).tap();
    await expect(page.getByRole('dialog', { name: 'Menu' })).toBeInViewport({ ratio: 1 });
    expect(await sidewaysOverflow(page)).toEqual([]);
    await page.getByRole('dialog', { name: 'Menu' }).getByRole('button', { name: 'Shut down' }).tap();

    await expect(page.getByRole('dialog', { name: 'Shut down OpenFlight?' })).toBeVisible();
    expect(await sidewaysOverflow(page)).toEqual([]);
    await expect(page.getByRole('button', { name: 'Cancel' })).toBeInViewport({ ratio: 1 });
  });

  test('does not offer "Open on your phone" when it already is the phone', async ({ page }) => {
    await gotoApp(page);
    await dismissPicker(page);
    await page.getByRole('button', { name: 'Open menu' }).tap();

    await expect(page.getByRole('dialog', { name: 'Menu' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open on your phone' })).toBeHidden();
  });

  test('keeps clear of the notch and the home indicator when launched from the home screen', async ({ page }) => {
    const devtools = await page.context().newCDPSession(page);
    await devtools.send('Emulation.setSafeAreaInsetsOverride', { insets: SAFE_AREA });
    await gotoApp(page);

    const closePicker = await page.getByRole('button', { name: 'Close Select club' }).boundingBox();
    expect(closePicker!.y).toBeGreaterThanOrEqual(SAFE_AREA.top);
    await dismissPicker(page);

    const header = await page.locator('.panel-header').boundingBox();
    expect(header!.y).toBe(SAFE_AREA.top);
    const tabs = await page.locator('.panel-footer__tabs').boundingBox();
    expect(tabs!.y + tabs!.height).toBeLessThanOrEqual(PHONE.height - SAFE_AREA.bottom);
    const footer = await page.locator('.panel-footer').boundingBox();
    expect(footer!.y + footer!.height).toBe(PHONE.height);

    await openTab(page, 'Setup');
    const location = page.getByRole('group', { name: 'Location' });
    await expect(location).toBeVisible();
    expect((await location.boundingBox())!.y).toBeGreaterThanOrEqual(SAFE_AREA.top);
  });
});

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`open on your phone at ${viewport.width}×${viewport.height}`, () => {
    test.use({ hasTouch: true, viewport });

    test('is in the menu and shows a QR code for the address the server reports', async ({ page }) => {
      await page.route('**/api/phone-link', (route) =>
        route.fulfill({
          json: {
            available: true,
            port: 8080,
            urls: [
              { kind: 'ipv4', host: '192.168.1.23', url: PHONE_URL },
              { kind: 'mdns', host: 'openflight.local', url: 'http://openflight.local:8080' },
            ],
          },
        })
      );
      await gotoApp(page);
      await dismissPicker(page);

      await page.getByRole('button', { name: 'Open menu' }).tap();
      const menu = page.getByRole('dialog', { name: 'Menu' });
      await expect(menu.getByRole('button', { name: 'Shut down' })).toBeInViewport({ ratio: 1 });
      await expect(menu.getByLabel('Language')).toBeInViewport({ ratio: 1 });
      expect(await menu.evaluate((sheet) => sheet.scrollHeight <= sheet.clientHeight)).toBe(true);
      await menu.getByRole('button', { name: 'Open on your phone' }).tap();
      await expect(menu).toBeHidden();

      const screen = page.getByRole('dialog', { name: 'Open on your phone' });
      const code = screen.getByRole('img', { name: `QR code for ${PHONE_URL}` });
      await expect(code).toBeInViewport({ ratio: 1 });
      const box = await code.boundingBox();
      expect(box!.width).toBeGreaterThanOrEqual(240);
      expect(Math.abs(box!.width - box!.height)).toBeLessThanOrEqual(1);
      await expect(code.locator('rect')).toHaveAttribute('fill', '#ffffff');
      await expect(screen.getByText(PHONE_URL, { exact: true })).toBeInViewport({ ratio: 1 });
      await expect(screen.getByText('http://openflight.local:8080')).toBeInViewport({ ratio: 1 });
      await expect(screen.getByRole('listitem')).toHaveCount(3);
      await expect(screen.getByText('Works on this Wi-Fi only.')).toBeInViewport({ ratio: 1 });
      expect(overlap(box, await screen.locator('.phone-link__guide').boundingBox())).toBe(false);

      await screen.getByRole('button', { name: 'Close Open on your phone' }).tap();
      await expect(screen).toBeHidden();
      await expect(page.locator('.live-panel__grid, .live-panel__empty').first()).toBeVisible();
    });
  });
}

test('says so, and retries, when no phone can reach the server', async ({ page }) => {
  // The E2E backend listens on loopback only, which is exactly that case.
  await gotoApp(page);
  await dismissPicker(page);
  await page.getByRole('button', { name: 'Open menu' }).click();
  await page.getByRole('button', { name: 'Open on your phone' }).click();

  const screen = page.getByRole('dialog', { name: 'Open on your phone' });
  await expect(screen.getByRole('alert')).toContainText('No address found that a phone can reach.');
  await expect(screen.locator('.qr-code')).toHaveCount(0);

  const asked = page.waitForResponse('**/api/phone-link');
  await screen.getByRole('button', { name: 'Try again' }).click();
  expect((await (await asked).json()).available).toBe(false);
  await expect(screen.getByRole('alert')).toBeVisible();
});

test('can be added to a phone home screen: manifest, icons and head tags', async ({ page }) => {
  await gotoApp(page);

  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute('href', '/manifest.webmanifest');
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute('href', '/icons/apple-touch-icon.png');
  await expect(page.locator('meta[name="viewport"]')).toHaveAttribute('content', /viewport-fit=cover/);
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute('content', '#16181a');
  await expect(page.locator('meta[name="apple-mobile-web-app-capable"]')).toHaveAttribute('content', 'yes');
  await expect(page.locator('meta[name="mobile-web-app-capable"]')).toHaveAttribute('content', 'yes');
  await expect(page.locator('meta[name="apple-mobile-web-app-title"]')).toHaveAttribute('content', 'OpenFlight');
  await expect(page.locator('meta[name="apple-mobile-web-app-status-bar-style"]')).toHaveAttribute(
    'content',
    'black-translucent'
  );

  const response = await page.request.get('/manifest.webmanifest');
  expect(response.ok()).toBe(true);
  expect(response.headers()['content-type']).toContain('application/manifest+json');
  const manifest = await response.json();
  expect(manifest).toMatchObject({ name: 'OpenFlight', start_url: '/', scope: '/', display: 'standalone' });
  expect(manifest.icons.map((icon: { sizes: string; purpose: string }) => `${icon.sizes} ${icon.purpose}`)).toEqual([
    '192x192 any',
    '512x512 any',
    '512x512 maskable',
  ]);

  const icons = [...manifest.icons.map((icon: { src: string }) => icon.src), '/icons/apple-touch-icon.png'];
  for (const src of icons) {
    const icon = await page.request.get(src);
    expect(icon.ok(), src).toBe(true);
    expect(icon.headers()['content-type'], src).toBe('image/png');
  }
});
