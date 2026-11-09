import { create } from 'zustand';

export type RigSetupId = 'simulator' | 'course';
export type RigDistanceSource = 'camera' | 'manual' | 'none';

export interface RigSetupValues {
  ball_distance_m: number | null;
  auto_distance: boolean;
  net_enabled: boolean;
  net_distance_m: number;
  ball_height_m: number;
}

export interface RigSetupSnapshot {
  active: RigSetupId;
  setups: Record<RigSetupId, RigSetupValues>;
  radar_height_m: number;
  camera_ready: boolean;
  scale_samples_needed: number;
  effective: {
    ball_distance_m: number | null;
    distance_source: RigDistanceSource;
    net_distance_m: number | null;
  };
}

interface RigSetupState {
  snapshot: RigSetupSnapshot | null;
  saveFailed: boolean;
  setSnapshot: (snapshot: RigSetupSnapshot) => void;
  setSaveFailed: (saveFailed: boolean) => void;
}

export const useRigSetupStore = create<RigSetupState>((set) => ({
  snapshot: null,
  saveFailed: false,
  setSnapshot: (snapshot) => set({ snapshot, saveFailed: false }),
  setSaveFailed: (saveFailed) => set({ saveFailed }),
}));
