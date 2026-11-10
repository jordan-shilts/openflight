import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { MeasuringBannerView, type MeasuringBannerViewProps } from './MeasuringBanner';

function render(overrides: Partial<MeasuringBannerViewProps> = {}): string {
  return renderToString(
    <MeasuringBannerView state="ready" expectedS={null} elapsedS={0} waitId={1} notice={null} {...overrides} />
  ).replace(/<!-- -->/g, '');
}

describe('MeasuringBannerView', () => {
  it('renders nothing when no recorder is reporting', () => {
    expect(render({ state: 'offline' })).toBe('');
  });

  it('says Ready, and that the next swing can go', () => {
    const html = render();

    expect(html).toContain('measuring-banner--ready');
    expect(html).toContain('Ready');
    expect(html).toContain('Swing when you like');
    expect(html).not.toContain('measuring-banner__fill');
  });

  it('says to wait while the radar is read, with a bar timed to the read', () => {
    const html = render({ state: 'measuring', expectedS: 7.3 });

    expect(html).toContain('measuring-banner--measuring');
    expect(html).toContain('Measuring');
    expect(html).toContain('Wait for Ready before the next swing');
    expect(html).toContain('--measuring-from:0.000');
    expect(html).toContain('--measuring-duration:7.30s');
    expect(html).not.toContain('Swing when you like');
  });

  it('starts the bar part-way for a screen that joined mid-wait', () => {
    const html = render({ state: 'measuring', expectedS: 8, elapsedS: 2 });

    expect(html).toContain('--measuring-from:0.250');
    expect(html).toContain('--measuring-duration:6.00s');
  });

  it('holds a full bar when the read runs long', () => {
    const html = render({ state: 'measuring', expectedS: 7, elapsedS: 9 });

    expect(html).toContain('--measuring-from:1.000');
    expect(html).toContain('--measuring-duration:0.00s');
  });

  it('falls back to the usual read time when none was given', () => {
    expect(render({ state: 'measuring', expectedS: null })).toContain('--measuring-duration:7.30s');
  });

  it.each([
    ['no_ball', 'No ball seen on that one'],
    ['failed', 'The radar read failed on that one'],
  ] as const)('explains a wait that produced no shot (%s)', (notice, copy) => {
    const html = render({ notice });

    expect(html).toContain('Ready');
    expect(html).toContain(copy);
    expect(html).not.toContain('Swing when you like');
  });

  it('announces changes to assistive tech', () => {
    expect(render()).toContain('role="status"');
    expect(render({ state: 'measuring' })).toContain('aria-live="polite"');
  });

  it('sizes its type in rem so it follows the kiosk', () => {
    const css = readFileSync(fileURLToPath(new URL('./MeasuringBanner.css', import.meta.url)), 'utf8');

    expect(css).not.toMatch(/font-size:\s*[\d.]+px/);
    expect(css).toMatch(/\.measuring-banner__label\s*{[^}]*font-size:\s*[\d.]+rem/);
  });
});
