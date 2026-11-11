import type { PhoneLink } from '../utils/phoneLink';
import { getServerOrigin } from '../utils/serverOrigin';

/** Ask the server where a phone can reach it. Null when it gave no usable answer. */
export async function fetchPhoneLink(): Promise<PhoneLink | null> {
  try {
    const response = await fetch(`${getServerOrigin()}/api/phone-link`, { cache: 'no-store' });
    if (!response.ok) {
      return null;
    }
    const link = (await response.json()) as PhoneLink;
    return Array.isArray(link?.urls) ? link : null;
  } catch {
    return null;
  }
}
