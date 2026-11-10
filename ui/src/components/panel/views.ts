export type PanelView = 'live' | 'club' | 'profiles' | 'stats' | 'shots' | 'camera' | 'setup' | 'debug';

/**
 * Footer tabs, in order. Design doc 6a uses text-only tabs, so no icons here.
 */
export const PANEL_VIEWS: ReadonlyArray<{ id: PanelView; label: string }> = [
  { id: 'live', label: 'Live' },
  { id: 'club', label: 'Club' },
  { id: 'stats', label: 'Stats' },
  { id: 'shots', label: 'Shots' },
  { id: 'camera', label: 'Camera' },
  { id: 'setup', label: 'Setup' },
  { id: 'profiles', label: 'Profiles' },
  { id: 'debug', label: 'Debug' },
];
