import { describe, expect, it } from 'vitest';
import { choosePhoneLink, MAX_PHONE_LINK_ALTERNATES, type PhoneLink, type PhoneLinkUrl } from './phoneLink';

const ipv4 = (host: string): PhoneLinkUrl => ({ kind: 'ipv4', host, url: `http://${host}:8080` });
const mdns = (host: string): PhoneLinkUrl => ({ kind: 'mdns', host, url: `http://${host}:8080` });
const link = (urls: PhoneLinkUrl[], available = urls.length > 0): PhoneLink => ({ available, port: 8080, urls });

describe('choosePhoneLink', () => {
  it('puts the IPv4 address in the QR code and offers the .local name as text', () => {
    const choice = choosePhoneLink(link([ipv4('192.168.1.23'), mdns('openflight.local')]));

    expect(choice?.primary.url).toBe('http://192.168.1.23:8080');
    expect(choice?.alternates.map((entry) => entry.url)).toEqual(['http://openflight.local:8080']);
  });

  it('prefers the IPv4 address even when the server lists the .local name first', () => {
    const choice = choosePhoneLink(link([mdns('openflight.local'), ipv4('192.168.1.23')]));

    expect(choice?.primary.host).toBe('192.168.1.23');
    expect(choice?.alternates.map((entry) => entry.host)).toEqual(['openflight.local']);
  });

  it('uses the first address the server lists when there are several', () => {
    const choice = choosePhoneLink(link([ipv4('192.168.1.23'), ipv4('10.0.0.5'), mdns('openflight.local')]));

    expect(choice?.primary.host).toBe('192.168.1.23');
    expect(choice?.alternates.map((entry) => entry.host)).toEqual(['openflight.local', '10.0.0.5']);
  });

  it('keeps the list of alternates short', () => {
    const choice = choosePhoneLink(
      link([ipv4('192.168.1.23'), ipv4('10.0.0.5'), ipv4('10.0.0.6'), ipv4('10.0.0.7'), mdns('openflight.local')])
    );

    expect(choice?.alternates).toHaveLength(MAX_PHONE_LINK_ALTERNATES);
    expect(choice?.alternates[0].host).toBe('openflight.local');
  });

  it('falls back to the .local name when it is all there is', () => {
    const choice = choosePhoneLink(link([mdns('openflight.local')]));

    expect(choice?.primary.host).toBe('openflight.local');
    expect(choice?.alternates).toEqual([]);
  });

  it('has nothing to offer with no network', () => {
    expect(choosePhoneLink(link([]))).toBeNull();
  });

  it('trusts the server when it says the addresses cannot be reached', () => {
    expect(choosePhoneLink(link([ipv4('192.168.1.23')], false))).toBeNull();
  });

  it('has nothing to offer when the server did not answer', () => {
    expect(choosePhoneLink(null)).toBeNull();
  });

  it('has nothing to offer when the server says available but lists no address', () => {
    expect(choosePhoneLink(link([], true))).toBeNull();
  });
});
