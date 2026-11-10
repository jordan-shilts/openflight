import { useRigSetupStore, type RigSetupId, type RigSetupSnapshot } from '../stores/useRigSetupStore';
import { getServerOrigin } from '../utils/serverOrigin';

export interface RigSetupChange {
  active?: RigSetupId;
  setup?: RigSetupId;
  ball_distance_m?: number;
  auto_distance?: boolean;
  net_enabled?: boolean;
  net_distance_m?: number;
  radar_height_m?: number;
}

const rigSetupUrl = () => `${getServerOrigin()}/api/rig-setup`;

async function requestSnapshot(init?: RequestInit): Promise<boolean> {
  const store = useRigSetupStore.getState();
  try {
    const response = await fetch(rigSetupUrl(), { cache: 'no-store', ...init });
    if (!response.ok) {
      store.setSaveFailed(true);
      return false;
    }
    store.setSnapshot((await response.json()) as RigSetupSnapshot);
    return true;
  } catch {
    store.setSaveFailed(true);
    return false;
  }
}

/** Load the saved setups when the panel opens. */
export function refreshRigSetup(): Promise<boolean> {
  return requestSnapshot();
}

/** Send one change; the store updates from the server's answer. */
export function changeRigSetup(change: RigSetupChange): Promise<boolean> {
  return requestSnapshot({
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(change),
  });
}
