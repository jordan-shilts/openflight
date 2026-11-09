export type StepDirection = 1 | -1;

export interface StepperOptions {
  /** The saved value, or null if nothing is set yet. */
  getValue: () => number | null;
  /** Where stepping begins when nothing is set. */
  start: number;
  step: number;
  min: number;
  max: number;
  /** Show a value while the finger is still down. */
  onPreview: (value: number) => void;
  /** Save once, on release, so holding a button doesn't flood the server. */
  onCommit: (value: number) => void;
  holdDelayMs?: number;
  repeatMs?: number;
}

const DEFAULT_HOLD_DELAY_MS = 400;
const DEFAULT_REPEAT_MS = 90;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** Tap to move one step; hold to keep going. Saves once when released. */
export function createStepperController(getOptions: () => StepperOptions) {
  let pending: number | null = null;
  let delayTimer: ReturnType<typeof setTimeout> | null = null;
  let repeatTimer: ReturnType<typeof setInterval> | null = null;

  const clearTimers = () => {
    if (delayTimer !== null) clearTimeout(delayTimer);
    if (repeatTimer !== null) clearInterval(repeatTimer);
    delayTimer = null;
    repeatTimer = null;
  };

  const advance = (direction: StepDirection) => {
    const options = getOptions();
    const base = pending ?? options.getValue() ?? options.start;
    const next = Number(clamp(base + direction * options.step, options.min, options.max).toFixed(4));
    pending = next;
    options.onPreview(next);
  };

  const release = () => {
    clearTimers();
    if (pending === null) return;
    const value = pending;
    pending = null;
    getOptions().onCommit(value);
  };

  const press = (direction: StepDirection) => {
    clearTimers();
    advance(direction);
    const { holdDelayMs = DEFAULT_HOLD_DELAY_MS, repeatMs = DEFAULT_REPEAT_MS } = getOptions();
    delayTimer = setTimeout(() => {
      repeatTimer = setInterval(() => advance(direction), repeatMs);
    }, holdDelayMs);
  };

  /** A keyboard press has no release, so it moves one step and saves. */
  const nudge = (direction: StepDirection) => {
    advance(direction);
    release();
  };

  return { press, release, nudge };
}
