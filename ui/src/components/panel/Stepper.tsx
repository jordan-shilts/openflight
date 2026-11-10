import { useMemo, useRef, useState, type MouseEvent, type PointerEvent } from 'react';
import { createStepperController, type StepDirection, type StepperOptions } from '../../utils/stepperController';

interface StepperProps extends Pick<StepperOptions, 'start' | 'step' | 'min' | 'max'> {
  name: string;
  decreaseLabel: string;
  increaseLabel: string;
  value: number | null;
  format: (value: number) => string;
  onCommit: (value: number) => Promise<unknown>;
}

const EMPTY_VALUE = '—';

/** Minus and plus around a value. Big enough for a finger, holds to repeat. */
export function Stepper({ name, decreaseLabel, increaseLabel, value, format, onCommit, ...range }: StepperProps) {
  const [preview, setPreview] = useState<number | null>(null);

  const latest = useRef({ value, onCommit, range });
  latest.current = { value, onCommit, range };

  const controller = useMemo(
    () =>
      createStepperController(() => ({
        ...latest.current.range,
        getValue: () => latest.current.value,
        onPreview: setPreview,
        onCommit: (next) => {
          void latest.current.onCommit(next).finally(() => setPreview(null));
        },
      })),
    []
  );

  const shown = preview ?? value;

  const button = (direction: StepDirection, label: string, glyph: string) => (
    <button
      type="button"
      className="stepper__button"
      aria-label={label}
      disabled={value !== null && (direction > 0 ? value >= range.max : value <= range.min)}
      onPointerDown={(event: PointerEvent<HTMLButtonElement>) => {
        event.currentTarget.setPointerCapture?.(event.pointerId);
        controller.press(direction);
      }}
      onPointerUp={controller.release}
      onPointerCancel={controller.release}
      onClick={(event: MouseEvent<HTMLButtonElement>) => {
        if (event.detail === 0) controller.nudge(direction);
      }}
    >
      {glyph}
    </button>
  );

  return (
    <div className="stepper" role="group" aria-label={name}>
      {button(-1, decreaseLabel, '−')}
      <output className="stepper__value">{shown === null ? EMPTY_VALUE : format(shown)}</output>
      {button(1, increaseLabel, '+')}
    </div>
  );
}
