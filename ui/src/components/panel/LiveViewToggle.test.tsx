import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { LiveViewToggle } from './LiveViewToggle';
import { otherLiveView } from './liveView';

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

function render(view: 'metrics' | 'flight'): string {
  return renderToString(<LiveViewToggle view={view} onChange={() => {}} />);
}

describe('LiveViewToggle', () => {
  it('offers the flight from the metric table and the data from the flight', () => {
    expect(render('metrics')).toContain('aria-label="Flight"');
    expect(render('metrics')).toContain('live-view-toggle__label">Flight<');
    expect(render('flight')).toContain('aria-label="Data"');
    expect(render('flight')).toContain('live-view-toggle__label">Data<');
  });

  it('is a secondary header action', () => {
    expect(render('metrics')).toMatch(/class="panel-action panel-action--secondary live-view-toggle"/);
  });

  it('switches to the other view', () => {
    expect(otherLiveView('metrics')).toBe('flight');
    expect(otherLiveView('flight')).toBe('metrics');
  });

  it('shrinks to its icon on a phone held upright and shows only text on kiosks', () => {
    const portrait = css.match(/@media \(max-width: 640px\) and \(orientation: portrait\) \{([\s\S]*?)\n\}/)?.[1] ?? '';

    expect(css).toMatch(/\.live-view-toggle__icon \{[^}]*display: none/);
    expect(portrait).toMatch(/\.live-view-toggle__icon \{[^}]*display: block/);
    expect(portrait).toMatch(/\.live-view-toggle__label \{[^}]*clip: rect\(0, 0, 0, 0\)/);
  });
});
