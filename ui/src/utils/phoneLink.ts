export type PhoneLinkKind = 'ipv4' | 'mdns';

export interface PhoneLinkUrl {
  kind: PhoneLinkKind;
  host: string;
  url: string;
}

/** What `GET /api/phone-link` answers. */
export interface PhoneLink {
  available: boolean;
  port: number | null;
  urls: PhoneLinkUrl[];
}

export interface PhoneLinkChoice {
  /** The URL the QR code encodes. */
  primary: PhoneLinkUrl;
  /** Other ways in, shown as text in case the first does not load. */
  alternates: PhoneLinkUrl[];
}

export const MAX_PHONE_LINK_ALTERNATES = 2;

/**
 * Pick the URL for the QR code. An IP address works on every network; a
 * `.local` name needs mDNS, which some routers and phones do not pass, so it
 * is only the first of the alternates.
 */
export function choosePhoneLink(link: PhoneLink | null): PhoneLinkChoice | null {
  if (!link || !link.available) {
    return null;
  }
  const primary = link.urls.find((entry) => entry.kind === 'ipv4') ?? link.urls[0];
  if (!primary) {
    return null;
  }
  const others = link.urls.filter((entry) => entry !== primary);
  const alternates = [
    ...others.filter((entry) => entry.kind === 'mdns'),
    ...others.filter((entry) => entry.kind !== 'mdns'),
  ].slice(0, MAX_PHONE_LINK_ALTERNATES);
  return { primary, alternates };
}
