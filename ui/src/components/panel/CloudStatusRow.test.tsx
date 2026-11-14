import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { CloudStatusRow } from './CloudStatusRow';
import type { CloudStatus } from '../../types/socket';

function render(status: CloudStatus | null) {
  return renderToString(<CloudStatusRow status={status} />);
}

describe('CloudStatusRow', () => {
  const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

  it('renders nothing before the server reports', () => {
    expect(render(null)).toBe('');
  });

  it('shows the shots waiting while offline', () => {
    const html = render({ state: 'offline', pending: 12, message: '', reason: '' });

    expect(html).toMatch(
      /menu-sheet__status-label">Cloud<.*menu-sheet__cloud menu-sheet__cloud--warn" role="status".*Offline · 12 shots waiting, will send when back online/s
    );
  });

  it('shows online and not set up in plain words', () => {
    const online = render({ state: 'online', pending: 0, message: '', reason: '' });

    expect(online).toContain('menu-sheet__cloud--ok');
    expect(online).toContain('Online · all shots saved');
    expect(render({ state: 'not_set_up', pending: null, message: '', reason: '' })).toContain('Not set up');
  });

  it('flags a refused device as an error, not as offline', () => {
    const html = render({ state: 'needs_attention', pending: 2, message: '401', reason: 'unauthorized' });

    expect(html).toContain('menu-sheet__cloud--error');
    expect(html).toContain('Device not registered or wrong key');
    expect(html).not.toContain('Offline');
  });

  it('sizes its text in rem so it follows the kiosk type scale', () => {
    const rule = css.split('.menu-sheet__cloud {')[1].split('}')[0];

    expect(rule).toMatch(/font-size: [\d.]+rem;/);
    expect(rule).not.toMatch(/font-size: [\d.]+px/);
    expect(rule).toContain('overflow-wrap: anywhere');
  });
});
