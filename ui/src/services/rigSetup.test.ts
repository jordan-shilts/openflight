import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useRigSetupStore, type RigSetupSnapshot } from '../stores/useRigSetupStore';
import { changeRigSetup, refreshRigSetup } from './rigSetup';

const snapshot = { active: 'course' } as RigSetupSnapshot;

function respondWith(ok: boolean, body: unknown = snapshot) {
  const fetchMock = vi.fn(async () => ({ ok, json: async () => body }) as Response);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('rig setup service', () => {
  beforeEach(() => useRigSetupStore.setState({ snapshot: null, saveFailed: false }));
  afterEach(() => vi.unstubAllGlobals());

  it('loads the saved setups into the store', async () => {
    respondWith(true);

    await expect(refreshRigSetup()).resolves.toBe(true);

    expect(useRigSetupStore.getState().snapshot).toBe(snapshot);
  });

  it('posts a change and takes the server answer', async () => {
    const fetchMock = respondWith(true);

    await expect(changeRigSetup({ ball_distance_m: 1.4 })).resolves.toBe(true);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain('/api/rig-setup');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ ball_distance_m: 1.4 });
    expect(useRigSetupStore.getState().snapshot).toBe(snapshot);
  });

  it('flags a refused change and keeps what is showing', async () => {
    useRigSetupStore.setState({ snapshot });
    respondWith(false);

    await expect(changeRigSetup({ ball_distance_m: 99 })).resolves.toBe(false);

    expect(useRigSetupStore.getState().saveFailed).toBe(true);
    expect(useRigSetupStore.getState().snapshot).toBe(snapshot);
  });

  it('flags a network failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new Error('down')))
    );

    await expect(changeRigSetup({ auto_distance: true })).resolves.toBe(false);

    expect(useRigSetupStore.getState().saveFailed).toBe(true);
  });

  it('clears the failure once a later change saves', async () => {
    useRigSetupStore.setState({ saveFailed: true });
    respondWith(true);

    await changeRigSetup({ auto_distance: true });

    expect(useRigSetupStore.getState().saveFailed).toBe(false);
  });
});
