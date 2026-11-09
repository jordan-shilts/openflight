import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createStepperController, type StepperOptions } from './stepperController';

function harness(overrides: Partial<StepperOptions> = {}) {
  const previews: number[] = [];
  const commits: number[] = [];
  const options: StepperOptions = {
    getValue: () => 1.0,
    start: 1.5,
    step: 0.1,
    min: 0.6,
    max: 2.0,
    onPreview: (value) => previews.push(value),
    onCommit: (value) => commits.push(value),
    holdDelayMs: 400,
    repeatMs: 100,
    ...overrides,
  };
  return { controller: createStepperController(() => options), previews, commits };
}

describe('createStepperController', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('moves one step on a tap and saves once on release', () => {
    const { controller, previews, commits } = harness();

    controller.press(1);
    controller.release();

    expect(previews).toEqual([1.1]);
    expect(commits).toEqual([1.1]);
  });

  it('does not repeat before the hold delay', () => {
    const { controller, previews } = harness();

    controller.press(1);
    vi.advanceTimersByTime(399);

    expect(previews).toEqual([1.1]);
  });

  it('keeps stepping while held and saves only the final value', () => {
    const { controller, previews, commits } = harness();

    controller.press(1);
    vi.advanceTimersByTime(400 + 300);
    controller.release();

    expect(previews).toEqual([1.1, 1.2, 1.3, 1.4]);
    expect(commits).toEqual([1.4]);
  });

  it('stops repeating after release', () => {
    const { controller, previews } = harness();

    controller.press(-1);
    vi.advanceTimersByTime(500);
    controller.release();
    const stopped = previews.length;
    vi.advanceTimersByTime(1000);

    expect(previews).toHaveLength(stopped);
  });

  it('never goes past its limits', () => {
    const atTop = harness({ getValue: () => 1.95 });
    atTop.controller.press(1);
    vi.advanceTimersByTime(400 + 500);
    atTop.controller.release();
    expect(atTop.commits).toEqual([2.0]);

    const atBottom = harness({ getValue: () => 0.65 });
    atBottom.controller.press(-1);
    atBottom.controller.release();
    expect(atBottom.commits).toEqual([0.6]);
  });

  it('starts from the default when nothing is set yet', () => {
    const { controller, commits } = harness({ getValue: () => null });

    controller.press(1);
    controller.release();

    expect(commits).toEqual([1.6]);
  });

  it('saves nothing when released without a press', () => {
    const { controller, commits } = harness();

    controller.release();

    expect(commits).toEqual([]);
  });

  it('moves one step and saves for a keyboard press', () => {
    const { controller, commits } = harness();

    controller.nudge(-1);

    expect(commits).toEqual([0.9]);
    vi.advanceTimersByTime(2000);
    expect(commits).toEqual([0.9]);
  });

  it('avoids floating point drift', () => {
    const { controller, commits } = harness({ getValue: () => 1.1, step: 0.0254 });

    controller.press(1);
    controller.release();

    expect(commits).toEqual([1.1254]);
  });
});
