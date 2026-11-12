import { test } from '@playwright/test';
import { expect, gotoApp, KIOSK_VIEWPORTS, resetSession, setClub, simulateShot, withControlSocket } from './helpers';

/** Dismiss the club picker that opens on every load, keeping the default club. */
async function dismissPicker(page: import('@playwright/test').Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

const PHONE_PORTRAIT = { width: 390, height: 844 };

for (const viewport of [...KIOSK_VIEWPORTS, PHONE_PORTRAIT]) {
  test.describe(`flight view at ${viewport.width}×${viewport.height}`, () => {
    test.use({ hasTouch: true, viewport });

    test.beforeEach(async () => {
      await withControlSocket(async (socket) => {
        await resetSession(socket);
        await setClub(socket, '7-iron');
        await simulateShot(socket);
      });
    });

    test('shows the flight from above under the side view without squeezing it out', async ({ page }) => {
      await gotoApp(page);
      await dismissPicker(page);
      await page.getByRole('button', { name: 'Flight', exact: true }).click();

      const side = page.locator('.flight-view__chart');
      const top = page.locator('.flight-view__top');
      await expect(side).toBeVisible();
      await expect(top).toBeVisible();
      await expect(top.getByRole('img')).toHaveAccessibleName(/^Top view of the flight: /);
      // The mock shot has a measured direction, so the strip says where it landed.
      await expect(top.locator('.flight-chart__offline')).toHaveText(/(yds (left|right)|On the target line)$/);

      const sideBox = await side.boundingBox();
      const topBox = await top.boundingBox();
      const readoutBox = await page.locator('.flight-view__readout').boundingBox();
      if (!sideBox || !topBox || !readoutBox) {
        throw new Error('flight view is not laid out');
      }
      // The strip is a fraction of the side view, stacked directly beneath it.
      expect(topBox.height).toBeGreaterThanOrEqual(40);
      expect(topBox.height).toBeLessThan(sideBox.height);
      expect(sideBox.height).toBeGreaterThanOrEqual(120);
      expect(Math.abs(topBox.y - (sideBox.y + sideBox.height))).toBeLessThanOrEqual(1);
      expect(Math.abs(readoutBox.y - (topBox.y + topBox.height))).toBeLessThanOrEqual(1);
      expect(topBox.width).toBe(sideBox.width);

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      expect(overflow).toBe(0);
    });
  });
}
