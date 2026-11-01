import { test } from '@playwright/test';
import {
  expect,
  gotoApp,
  KIOSK_VIEWPORTS,
  liveMetricValueFontSizes,
  overflowingLiveMetricMetadata,
  overflowingLiveMetricValues,
  simulateShot,
  WIDEST_LIVE_METRIC_VALUE,
  withControlSocket,
} from './helpers';

/** Dismiss the club picker that opens on every load, keeping the default club. */
async function dismissPicker(page: import('@playwright/test').Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`live metrics at ${viewport.width}×${viewport.height}`, () => {
    test.use({ hasTouch: true, viewport });

    test('keeps metric value text inside each live tile', async ({ page }) => {
      await withControlSocket(async (socket) => {
        await simulateShot(socket);
      });

      await gotoApp(page);
      await dismissPicker(page);

      await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(10);

      const sizes = await liveMetricValueFontSizes(page);
      expect(sizes.length).toBe(10);
      expect(new Set(sizes.map((size) => size.toFixed(2))).size).toBe(1);

      await page.locator('.live-panel__grid .metric-card__value').evaluateAll((values, widest) => {
        for (const value of values) {
          value.textContent = widest;
        }
      }, WIDEST_LIVE_METRIC_VALUE);
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

      expect(await overflowingLiveMetricValues(page)).toEqual([]);
    });

    test('keeps long metric subtitles readable inside each live tile', async ({ page }) => {
      await withControlSocket(async (socket) => {
        await simulateShot(socket);
      });

      await gotoApp(page);
      await dismissPicker(page);

      const subtitles = [
        'spin-adjusted',
        'camera assisted (experimental)',
        'camera fused (experimental)',
        'rejected: insufficient post trigger frames',
        'player + implement',
        '99 swings',
        '99 readings',
        '199.9 mph trigger',
        'straight',
        'candidate',
      ];
      await page.locator('.live-panel__grid .metric-card').evaluateAll((cards, text) => {
        cards.forEach((card, index) => {
          const meta = card.querySelector('.metric-card__meta');
          if (!(meta instanceof HTMLElement)) return;

          const subtitle = document.createElement('span');
          subtitle.className = 'metric-card__subtext metric-card__confidence-label';
          subtitle.textContent = text[index];
          meta.replaceChildren(subtitle);

          if (index === 2 || index === 3) {
            const confidence = document.createElement('div');
            confidence.className = 'metric-card__confidence metric-card__confidence--high';
            confidence.innerHTML =
              '<span class="metric-card__confidence-dots"><span class="dot filled"></span><span class="dot filled"></span><span class="dot filled"></span></span><span class="metric-card__confidence-label">experimental</span>';
            meta.append(confidence);
          }
        });
      }, subtitles);

      expect(await overflowingLiveMetricMetadata(page)).toEqual([]);
    });
  });
}

test.describe('live metrics on a phone held upright (390×844)', () => {
  test.use({ hasTouch: true, viewport: { width: 390, height: 844 } });

  test('puts the pinned metric and carry on top, the rest in two columns, all inside their tiles', async ({ page }) => {
    await withControlSocket(async (socket) => {
      await simulateShot(socket);
    });

    await gotoApp(page);
    await dismissPicker(page);

    const grid = page.locator('.live-panel__grid');
    await expect(grid.locator('.metric-card')).toHaveCount(10);
    expect(await grid.evaluate((node) => getComputedStyle(node).gridTemplateColumns.trim().split(/\s+/).length)).toBe(
      2
    );

    const tops = await grid.locator('.metric-card').evaluateAll((cards) =>
      cards.map((card) => ({
        label: card.querySelector('.metric-card__label')?.textContent?.trim() ?? '',
        top: Math.round(card.getBoundingClientRect().top),
      }))
    );
    const firstRowTop = Math.min(...tops.map((tile) => tile.top));
    expect(tops.filter((tile) => tile.top === firstRowTop).map((tile) => tile.label)).toEqual([
      'Ball speed',
      expect.stringContaining('Carry'),
    ]);

    const sizes = await liveMetricValueFontSizes(page);
    expect(sizes[0]).toBeGreaterThan(sizes[2]);
    expect(await overflowingLiveMetricValues(page)).toEqual([]);

    await page.locator('.live-panel__grid .metric-card__value').evaluateAll((values, widest) => {
      for (const value of values) {
        value.textContent = widest;
      }
    }, WIDEST_LIVE_METRIC_VALUE);
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(await overflowingLiveMetricValues(page)).toEqual([]);

    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await expect(page.getByRole('button', { name: 'Change club' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Stats', exact: true })).toBeVisible();
  });
});
