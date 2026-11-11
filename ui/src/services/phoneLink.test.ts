import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PhoneLink } from '../utils/phoneLink';
import { fetchPhoneLink } from './phoneLink';

const link: PhoneLink = {
  available: true,
  port: 8080,
  urls: [{ kind: 'ipv4', host: '192.168.1.23', url: 'http://192.168.1.23:8080' }],
};

function respondWith(ok: boolean, body: unknown = link) {
  const fetchMock = vi.fn(async () => ({ ok, json: async () => body }) as Response);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('phone link service', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('asks the server and returns its answer', async () => {
    const fetchMock = respondWith(true);

    await expect(fetchPhoneLink()).resolves.toEqual(link);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/phone-link$/);
    expect(init.cache).toBe('no-store');
  });

  it('gives nothing when the server refuses', async () => {
    respondWith(false);

    await expect(fetchPhoneLink()).resolves.toBeNull();
  });

  it('gives nothing when the server cannot be reached', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new Error('down')))
    );

    await expect(fetchPhoneLink()).resolves.toBeNull();
  });

  it('gives nothing when the answer is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => Promise.reject(new SyntaxError('bad json')) }) as Response)
    );

    await expect(fetchPhoneLink()).resolves.toBeNull();
  });

  it.each([null, {}, { urls: 'http://192.168.1.23:8080' }])(
    'gives nothing for an answer without a URL list: %j',
    async (body) => {
      respondWith(true, body);

      await expect(fetchPhoneLink()).resolves.toBeNull();
    }
  );
});
