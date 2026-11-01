import { useMemo, useRef } from 'react';
import type { Profile } from '../../types/profile';
import { MAX_PROFILES } from '../../types/profile';
import type { Shot } from '../../types/shot';
import { countShotsByProfile } from '../../types/shot';
import { useDragScroll } from '../../hooks/useDragScroll';
import { useI18n } from '../../i18n/useI18n';

interface PlayerStripProps {
  profiles: Profile[];
  activeProfileId: string;
  shots: Shot[];
  onSelectProfile: (profileId: string) => void;
  onAddProfile: () => void;
}

interface PlayerChipProps {
  profile: Profile;
  shotCount: number;
  active: boolean;
  onSelect: (profileId: string) => void;
}

export function PlayerChip({ profile, shotCount, active, onSelect }: PlayerChipProps) {
  return (
    <button
      type="button"
      className={`panel-chip player-strip__chip${active ? ' panel-chip--active' : ''}`}
      aria-pressed={active}
      onClick={() => {
        if (!active) onSelect(profile.id);
      }}
    >
      <span className="player-strip__name">{profile.name}</span>{' '}
      <span className="player-strip__count">{shotCount}</span>
    </button>
  );
}

/** Live-screen profile switcher. The add chip sits outside the scroller so it never scrolls away. */
export function PlayerStrip({ profiles, activeProfileId, shots, onSelectProfile, onAddProfile }: PlayerStripProps) {
  const { t } = useI18n();
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(scrollRef, 'x');
  const shotCounts = useMemo(() => countShotsByProfile(shots), [shots]);

  // A lone profile has nobody to switch to, so solo users keep the plain Live screen.
  if (profiles.length < 2) {
    return null;
  }

  return (
    <div className="player-strip" role="group" aria-label={t('live.switchProfile')}>
      <div
        className="panel-chips player-strip__scroll"
        ref={scrollRef}
        onPointerDown={dragScroll.onPointerDown}
        onPointerMove={dragScroll.onPointerMove}
        onPointerUp={dragScroll.onPointerUp}
        onPointerCancel={dragScroll.onPointerCancel}
        onClickCapture={dragScroll.onClickCapture}
      >
        {profiles.map((profile) => (
          <PlayerChip
            key={profile.id}
            profile={profile}
            shotCount={shotCounts[profile.id] ?? 0}
            active={profile.id === activeProfileId}
            onSelect={onSelectProfile}
          />
        ))}
      </div>
      {profiles.length < MAX_PROFILES ? (
        <button
          type="button"
          className="panel-chip player-strip__add"
          aria-label={t('menu.addProfile')}
          onClick={onAddProfile}
        >
          +
        </button>
      ) : null}
    </div>
  );
}
