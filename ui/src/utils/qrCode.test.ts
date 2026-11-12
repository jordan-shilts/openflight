import { describe, expect, it } from 'vitest';
import { QR_QUIET_ZONE, qrCodeShape } from './qrCode';

/** Rebuild the module grid from the path so tests can look at single modules. */
function darkModules(path: string): Set<string> {
  return new Set([...path.matchAll(/M(\d+) (\d+)h1v1h-1z/g)].map(([, column, row]) => `${column},${row}`));
}

describe('qrCodeShape', () => {
  it('fits a LAN address in a version 2 code with a four-module quiet zone', () => {
    const { size } = qrCodeShape('http://192.168.1.23:8080');

    expect(size).toBe(25 + QR_QUIET_ZONE * 2);
  });

  it('grows the code when the text needs it', () => {
    const short = qrCodeShape('http://10.0.0.5');
    const long = qrCodeShape('http://a-rather-long-kiosk-hostname.local:8080');

    expect(long.size).toBeGreaterThan(short.size);
  });

  it('draws only whole modules', () => {
    const { path } = qrCodeShape('http://192.168.1.23:8080');

    expect(path.replace(/M\d+ \d+h1v1h-1z/g, '')).toBe('');
  });

  it('leaves the quiet zone empty on every side', () => {
    const { size, path } = qrCodeShape('http://192.168.1.23:8080');
    const inside = (value: number) => value >= QR_QUIET_ZONE && value < size - QR_QUIET_ZONE;

    for (const module of darkModules(path)) {
      const [column, row] = module.split(',').map(Number);
      expect(inside(column) && inside(row), module).toBe(true);
    }
  });

  it('has the three finder patterns a camera locks on to, and none in the fourth corner', () => {
    const { size, path } = qrCodeShape('http://192.168.1.23:8080');
    const dark = darkModules(path);
    const first = QR_QUIET_ZONE;
    const last = size - QR_QUIET_ZONE - 1;
    /** A finder is a 7x7 dark ring around a light ring around a 3x3 dark centre. */
    const isFinder = (left: number, top: number) => {
      for (let row = 0; row < 7; row += 1) {
        for (let column = 0; column < 7; column += 1) {
          const ring = Math.max(Math.abs(row - 3), Math.abs(column - 3));
          if (dark.has(`${left + column},${top + row}`) !== (ring !== 2)) return false;
        }
      }
      return true;
    };

    expect(isFinder(first, first)).toBe(true);
    expect(isFinder(last - 6, first)).toBe(true);
    expect(isFinder(first, last - 6)).toBe(true);
    expect(isFinder(last - 6, last - 6)).toBe(false);
  });

  it('gives a different code for a different address', () => {
    expect(qrCodeShape('http://192.168.1.23:8080').path).not.toBe(qrCodeShape('http://192.168.1.24:8080').path);
  });
});
