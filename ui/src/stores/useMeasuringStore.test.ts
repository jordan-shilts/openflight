import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MEASURING_NOTICE_MS, useMeasuringStore } from './useMeasuringStore';

describe('useMeasuringStore', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useMeasuringStore.getState().goOffline();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('starts offline, so a kiosk without a recorder shows nothing', () => {
    expect(useMeasuringStore.getState().state).toBe('offline');
    expect(useMeasuringStore.getState().notice).toBeNull();
  });

  it('starts a new wait with its expected length', () => {
    const before = useMeasuringStore.getState().waitId;

    useMeasuringStore.getState().ingest({ state: 'measuring', expected_s: 7.3, elapsed_s: 0 });

    const state = useMeasuringStore.getState();
    expect(state.state).toBe('measuring');
    expect(state.expectedS).toBe(7.3);
    expect(state.elapsedS).toBe(0);
    expect(state.waitId).toBe(before + 1);
  });

  it('joins a wait part-way through', () => {
    useMeasuringStore.getState().ingest({ state: 'measuring', expected_s: 7.3, elapsed_s: 2.5 });

    expect(useMeasuringStore.getState().elapsedS).toBe(2.5);
  });

  it('gives every wait its own id so the bar restarts', () => {
    const { ingest } = useMeasuringStore.getState();
    ingest({ state: 'measuring', expected_s: 7.3 });
    const first = useMeasuringStore.getState().waitId;
    ingest({ state: 'ready', result: 'shot' });
    ingest({ state: 'measuring', expected_s: 7.3 });

    expect(useMeasuringStore.getState().waitId).toBe(first + 1);
  });

  it('goes ready without a notice after a measured shot', () => {
    useMeasuringStore.getState().ingest({ state: 'measuring', expected_s: 7.3 });
    useMeasuringStore.getState().ingest({ state: 'ready', result: 'shot' });

    expect(useMeasuringStore.getState().state).toBe('ready');
    expect(useMeasuringStore.getState().notice).toBeNull();
  });

  it.each(['no_ball', 'failed'] as const)('says %s for a moment, then just ready', (result) => {
    useMeasuringStore.getState().ingest({ state: 'ready', result });
    expect(useMeasuringStore.getState().notice).toBe(result);

    vi.advanceTimersByTime(MEASURING_NOTICE_MS - 1);
    expect(useMeasuringStore.getState().notice).toBe(result);
    vi.advanceTimersByTime(1);

    expect(useMeasuringStore.getState().notice).toBeNull();
    expect(useMeasuringStore.getState().state).toBe('ready');
  });

  it('keeps the notice when the server repeats Ready for another screen', () => {
    useMeasuringStore.getState().ingest({ state: 'ready', result: 'no_ball' });

    useMeasuringStore.getState().ingest({ state: 'ready' });

    expect(useMeasuringStore.getState().notice).toBe('no_ball');
    vi.advanceTimersByTime(MEASURING_NOTICE_MS);
    expect(useMeasuringStore.getState().notice).toBeNull();
  });

  it('drops the notice as soon as the next swing starts', () => {
    useMeasuringStore.getState().ingest({ state: 'ready', result: 'no_ball' });
    useMeasuringStore.getState().ingest({ state: 'measuring', expected_s: 7.3 });
    vi.advanceTimersByTime(MEASURING_NOTICE_MS);

    expect(useMeasuringStore.getState().notice).toBeNull();
    expect(useMeasuringStore.getState().state).toBe('measuring');
  });

  it('goes offline when the recorder stops or the socket drops', () => {
    useMeasuringStore.getState().ingest({ state: 'measuring', expected_s: 7.3 });

    useMeasuringStore.getState().ingest({ state: 'offline' });
    expect(useMeasuringStore.getState().state).toBe('offline');

    useMeasuringStore.getState().ingest({ state: 'ready', result: 'no_ball' });
    useMeasuringStore.getState().goOffline();
    expect(useMeasuringStore.getState().state).toBe('offline');
    expect(useMeasuringStore.getState().notice).toBeNull();
  });
});
