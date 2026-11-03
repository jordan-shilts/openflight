import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { SimStatus as SimStatusData } from '../types/socket';
import { SimStatus } from './SimStatus';

describe('SimStatus', () => {
  it('renders nothing when no connectors are configured', () => {
    expect(renderToString(<SimStatus statuses={{}} />)).toBe('');
  });

  it('renders a pill per connector with display names and state', () => {
    const statuses: Record<string, SimStatusData> = {
      gspro: { target: 'gspro', state: 'connected', host: '127.0.0.1', port: 921 },
      opengolfsim: { target: 'opengolfsim', state: 'reconnecting', attempt: 2, next_retry_in_s: 4 },
      partee: { target: 'partee', state: 'connecting', host: '192.168.1.70', port: 921 },
    };
    const html = renderToString(<SimStatus statuses={statuses} />);
    expect(html).toContain('GSPro');
    expect(html).toContain('OpenGolfSim');
    expect(html).toContain('PAR-TEE');
    expect(html).toContain('sim-status__pill--ok'); // connected
    expect(html).toContain('sim-status__pill--warn'); // reconnecting
  });

  it('names the simulator that answered, not the protocol it speaks', () => {
    const statuses: Record<string, SimStatusData> = {
      gspro: { target: 'gspro', state: 'connected', host: '192.168.1.20', port: 921, peer: 'Open-Birdie' },
    };
    const html = renderToString(<SimStatus statuses={statuses} />);
    expect(html).toContain('>Open-Birdie<');
    expect(html).not.toContain('>GSPro<');
    expect(html).toContain('Open-Birdie via GSPro protocol'); // the tooltip keeps the protocol
  });

  it('marks error state with the error severity class', () => {
    const statuses: Record<string, SimStatusData> = {
      gspro: { target: 'gspro', state: 'error', message: 'Connection refused' },
    };
    const html = renderToString(<SimStatus statuses={statuses} />);
    expect(html).toContain('sim-status__pill--error');
  });
});
