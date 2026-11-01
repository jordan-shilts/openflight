import { test, type Page } from '@playwright/test';
import type { Socket } from 'socket.io-client';
import {
  expect,
  gotoApp,
  KIOSK_VIEWPORTS,
  overflowingLiveMetricMetadata,
  overflowingLiveMetricValues,
  resetSession,
  setClub,
  simulateShot,
  waitForEvent,
  withControlSocket,
} from './helpers';

interface ProfilesSnapshot {
  profiles: Array<{ id: string; name: string }>;
  active_profile_id: string;
}

async function dismissPicker(page: Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

/** The server makes a new profile active, as the UI's add dialog does. */
async function addProfile(socket: Socket, name: string): Promise<ProfilesSnapshot> {
  const snapshot = waitForEvent<ProfilesSnapshot>(socket, 'profiles');
  socket.emit('add_profile', { name });
  return snapshot;
}

async function activate(socket: Socket, name: string) {
  const current = waitForEvent<ProfilesSnapshot>(socket, 'profiles');
  socket.emit('get_profiles');
  const { profiles } = await current;
  const target = profiles.find((profile) => profile.name === name);
  expect(target).toBeTruthy();

  const switched = waitForEvent<ProfilesSnapshot>(socket, 'profiles');
  socket.emit('set_active_profile', { profile_id: target!.id });
  await switched;
}

test.beforeEach(async () => {
  await withControlSocket(async (socket) => {
    await resetSession(socket);
    await setClub(socket, 'driver');
  });
});

test.describe('player strip', () => {
  test.use({ viewport: { width: 1024, height: 600 } });

  test('stays hidden for a single profile', async ({ page }) => {
    await withControlSocket(async (socket) => {
      await simulateShot(socket);
    });

    await gotoApp(page);
    await dismissPicker(page);

    await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(10);
    await expect(page.locator('.player-strip')).toHaveCount(0);
  });

  test('switches the active profile with one tap and stays on Live', async ({ page }) => {
    await withControlSocket(async (socket) => {
      await addProfile(socket, 'Alex');
      await simulateShot(socket);
      await activate(socket, 'Profile 1');
    });

    await gotoApp(page);
    await dismissPicker(page);

    const strip = page.getByRole('group', { name: 'Switch profile' });
    await expect(strip.getByRole('button', { name: 'Profile 1 0' })).toHaveAttribute('aria-pressed', 'true');
    await strip.getByRole('button', { name: 'Alex 1' }).click();

    await expect(strip.getByRole('button', { name: 'Alex 1' })).toHaveAttribute('aria-pressed', 'true');
    await expect(strip.getByRole('button', { name: 'Profile 1 0' })).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('.panel-header__title')).toHaveText('Live');
    await expect(page.locator('.panel-header__subtitle')).toHaveText('Alex');
    await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(10);
  });

  test('opens the add-profile dialog from the + chip', async ({ page }) => {
    await withControlSocket(async (socket) => {
      await addProfile(socket, 'Alex');
    });

    await gotoApp(page);
    await dismissPicker(page);
    await page.getByRole('group', { name: 'Switch profile' }).getByRole('button', { name: 'Add profile' }).click();

    await expect(page.getByRole('dialog', { name: 'Add profile' })).toBeVisible();
  });

  test('scrolls by dragging without switching profiles', async ({ page }) => {
    await withControlSocket(async (socket) => {
      for (const name of ['Alexandra Long', 'Benjamin Long', 'Charlotte Long', 'Dominique Long', 'Evangeline Long']) {
        await addProfile(socket, name);
      }
      await activate(socket, 'Profile 1');
    });

    await gotoApp(page);
    await dismissPicker(page);

    const scroller = page.locator('.player-strip__scroll');
    const box = await scroller.boundingBox();
    expect(box).toBeTruthy();
    expect(await scroller.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);

    await page.mouse.move(box!.x + box!.width - 40, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + 40, box!.y + box!.height / 2, { steps: 12 });
    await page.mouse.up();

    await expect.poll(async () => scroller.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
    await expect(page.locator('.panel-header__subtitle')).toHaveText('Profile 1');
    await expect(page.getByRole('button', { name: 'Add profile' })).toBeVisible();
  });
});

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`player strip at ${viewport.width}×${viewport.height}`, () => {
    test.use({ hasTouch: true, viewport });

    test('leaves every live tile readable', async ({ page }) => {
      await withControlSocket(async (socket) => {
        await addProfile(socket, 'Alex');
        await simulateShot(socket);
      });

      await gotoApp(page);
      await dismissPicker(page);

      await expect(page.locator('.player-strip')).toBeVisible();
      await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(10);
      expect(await overflowingLiveMetricValues(page)).toEqual([]);
      expect(await overflowingLiveMetricMetadata(page)).toEqual([]);

      const title = await page.locator('.panel-header__title').boundingBox();
      const strip = await page.locator('.player-strip').boundingBox();
      const sharesTitleRow = strip!.y < title!.y + title!.height;
      expect(sharesTitleRow).toBe(viewport.height <= 440);
    });
  });
}

test.describe('player strip on a touchscreen', () => {
  test.use({ hasTouch: true, viewport: { width: 1024, height: 600 } });

  test('taps a chip to switch', async ({ page }) => {
    await withControlSocket(async (socket) => {
      await addProfile(socket, 'Alex');
      await activate(socket, 'Profile 1');
    });

    await gotoApp(page);
    await dismissPicker(page);
    await page.getByRole('button', { name: 'Alex 0' }).tap();

    await expect(page.locator('.panel-header__subtitle')).toHaveText('Alex');
  });
});

test.describe('stats leaderboard', () => {
  test.use({ viewport: { width: 1024, height: 600 } });

  test('compares every profile and returns to Just me', async ({ page }) => {
    await withControlSocket(async (socket) => {
      await simulateShot(socket);
      await addProfile(socket, 'Alex');
      await simulateShot(socket);
      await addProfile(socket, 'Guest');
      await activate(socket, 'Profile 1');
    });

    await gotoApp(page);
    await dismissPicker(page);
    await page.getByRole('button', { name: 'Stats', exact: true }).click();
    await expect(page.locator('.stats-panel__grid .metric-card')).toHaveCount(6);

    await page.getByRole('button', { name: 'Leaderboard' }).click();

    const rows = page.locator('.stats-leaderboard__row');
    await expect(rows).toHaveCount(3);
    await expect(rows.filter({ hasText: 'Profile 1' })).toHaveAttribute('aria-current', 'true');
    await expect(rows.filter({ hasText: 'Guest' })).toHaveClass(/stats-leaderboard__row--empty/);
    await expect(page.locator('.stats-leaderboard__best')).not.toHaveCount(0);

    await page.getByRole('button', { name: 'Just me' }).click();
    await expect(page.locator('.stats-leaderboard')).toHaveCount(0);
    await expect(page.locator('.stats-panel__grid .metric-card')).toHaveCount(6);
  });
});
