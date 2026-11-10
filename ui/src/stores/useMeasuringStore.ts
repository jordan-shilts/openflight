import { create } from 'zustand';

/**
 * Whether the camera-triggered radar recorder can take a swing. `offline`
 * means no recorder is reporting, so the screen says nothing about it.
 */
export type MeasuringState = 'offline' | 'ready' | 'measuring';

/** How the last wait ended, when that is worth telling the golfer. */
export type MeasuringNotice = 'no_ball' | 'failed';

/** The server's `measuring_status` event. */
export interface MeasuringStatusMessage {
  state: MeasuringState;
  /** Seconds the radar read is expected to take; only with `measuring`. */
  expected_s?: number | null;
  /** Seconds already gone when this was sent; non-zero for a screen that joined mid-wait. */
  elapsed_s?: number;
  result?: 'shot' | MeasuringNotice;
}

/** Long enough to read from the mat, short enough not to hide Ready. */
export const MEASURING_NOTICE_MS = 4000;

interface MeasuringStoreState {
  state: MeasuringState;
  expectedS: number | null;
  /** Seconds of the wait already gone when this screen heard about it. */
  elapsedS: number;
  /** Changes on every new wait, so the progress bar restarts. */
  waitId: number;
  notice: MeasuringNotice | null;
  ingest: (message: MeasuringStatusMessage) => void;
  goOffline: () => void;
}

export const useMeasuringStore = create<MeasuringStoreState>((set, get) => {
  let noticeTimer: ReturnType<typeof setTimeout> | null = null;

  const clearNoticeTimer = () => {
    if (noticeTimer) clearTimeout(noticeTimer);
    noticeTimer = null;
  };

  return {
    state: 'offline',
    expectedS: null,
    elapsedS: 0,
    waitId: 0,
    notice: null,
    ingest: (message) => {
      // The server repeats the current state to every screen whenever one
      // connects. A repeated Ready must not cut short a notice still showing.
      if (message.state === 'ready' && !message.result && get().state === 'ready') return;
      clearNoticeTimer();
      if (message.state === 'measuring') {
        set((current) => ({
          state: 'measuring',
          expectedS: message.expected_s ?? null,
          elapsedS: message.elapsed_s ?? 0,
          waitId: current.waitId + 1,
          notice: null,
        }));
        return;
      }
      const notice =
        message.state === 'ready' && (message.result === 'no_ball' || message.result === 'failed')
          ? message.result
          : null;
      set({ state: message.state, expectedS: null, elapsedS: 0, notice });
      if (notice) {
        noticeTimer = setTimeout(() => {
          noticeTimer = null;
          set({ notice: null });
        }, MEASURING_NOTICE_MS);
      }
    },
    goOffline: () => {
      clearNoticeTimer();
      set({ state: 'offline', expectedS: null, elapsedS: 0, notice: null });
    },
  };
});
