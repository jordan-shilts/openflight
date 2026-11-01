import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { Profile } from '../../types/profile';
import { MAX_PROFILES } from '../../types/profile';
import type { Shot } from '../../types/shot';
import { PlayerChip, PlayerStrip } from './PlayerStrip';

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function profile(id: string, name: string): Profile {
  return { id, name, created_at: '2026-11-01T10:00:00Z', settings: {} };
}

function shot(profileId: string): Shot {
  return { profile_id: profileId, ball_speed_mph: 140 } as Shot;
}

const roster = [profile('jor', 'Jordan'), profile('alx', 'Alex'), profile('gst', 'Guest')];

function render(overrides: Partial<Parameters<typeof PlayerStrip>[0]> = {}) {
  return text(
    renderToString(
      <PlayerStrip
        profiles={roster}
        activeProfileId="jor"
        shots={[shot('jor'), shot('jor'), shot('alx')]}
        onSelectProfile={() => {}}
        onAddProfile={() => {}}
        {...overrides}
      />
    )
  );
}

function chip(html: string, name: string): string {
  const chips = [...html.matchAll(/<button[^>]*player-strip__chip[^>]*>[\s\S]*?<\/button>/g)].map((match) => match[0]);
  return chips.find((markup) => markup.includes(`>${name}<`)) ?? '';
}

describe('PlayerStrip', () => {
  it('renders nothing for a single profile, so solo users see no change', () => {
    expect(render({ profiles: [profile('jor', 'Jordan')] })).toBe('');
  });

  it('renders nothing before the roster arrives', () => {
    expect(render({ profiles: [], activeProfileId: '' })).toBe('');
  });

  it('appears as soon as a second profile exists', () => {
    expect(render({ profiles: roster.slice(0, 2) })).toContain('player-strip__chip');
  });

  it('renders one chip per profile in roster order with its session shot count', () => {
    const html = render();
    const names = [...html.matchAll(/player-strip__name">([^<]+)</g)].map((match) => match[1]);
    const counts = [...html.matchAll(/player-strip__count">([^<]+)</g)].map((match) => match[1]);

    expect(names).toEqual(['Jordan', 'Alex', 'Guest']);
    expect(counts).toEqual(['2', '1', '0']);
    // The space gives the button an accessible name of "Alex 1", not "Alex1".
    expect(chip(html, 'Alex')).toContain('Alex</span> <span class="player-strip__count">1<');
  });

  it('highlights only the active profile', () => {
    const html = render({ activeProfileId: 'alx' });

    expect(chip(html, 'Alex')).toContain('panel-chip--active');
    expect(chip(html, 'Alex')).toContain('aria-pressed="true"');
    expect(chip(html, 'Jordan')).not.toContain('panel-chip--active');
    expect(chip(html, 'Jordan')).toContain('aria-pressed="false"');
    expect(html.match(/panel-chip--active/g)).toHaveLength(1);
  });

  it('labels the row as a profile switcher', () => {
    expect(render()).toMatch(/class="player-strip" role="group" aria-label="Switch profile"/);
  });

  it('pins the add chip outside the scrolling names', () => {
    const html = render();
    const scroller = html.match(/player-strip__scroll[\s\S]*?<\/div>/)?.[0] ?? '';

    expect(html).toContain('aria-label="Add profile"');
    expect(scroller).not.toContain('player-strip__add');
    expect(html.indexOf('player-strip__add')).toBeGreaterThan(html.indexOf('player-strip__scroll'));
  });

  it('drops the add chip once the roster reaches the server cap', () => {
    const full = Array.from({ length: MAX_PROFILES }, (_, index) => profile(`p${index}`, `P${index}`));

    expect(MAX_PROFILES).toBe(12);
    expect(render({ profiles: full, activeProfileId: 'p0' })).not.toContain('player-strip__add');
    expect(render({ profiles: full.slice(1), activeProfileId: 'p1' })).toContain('player-strip__add');
  });

  it('owns drag gestures and truncates long names', () => {
    expect(css).toMatch(/\.player-strip__scroll \{[^}]*touch-action: none/);
    expect(css).toMatch(/\.player-strip \.panel-chip \{[^}]*touch-action: none/);
    expect(css).toMatch(/\.player-strip__name \{[^}]*text-overflow: ellipsis/);
  });

  it('wraps under the header title, but sits beside it on 800x400 so the grid keeps its height', () => {
    expect(css).toMatch(/\.panel-header:has\(> \.player-strip\) \{[^}]*flex-wrap: wrap/);
    expect(css).toMatch(/\.player-strip \{[^}]*flex: 1 0 100%/);
    const short = css.match(/@media \(max-height: 440px\) \{([\s\S]*?)\n\}/)?.[1] ?? '';
    expect(short).toMatch(/\.panel-header:has\(> \.player-strip\) \{[^}]*flex-wrap: nowrap/);
    expect(short).toMatch(/\.player-strip \{[^}]*flex: 1 1 0/);
  });
});

describe('PlayerChip', () => {
  const alex = profile('alx', 'Alex');

  it('asks to switch to its profile when tapped', () => {
    const onSelect = vi.fn();
    const element = PlayerChip({ profile: alex, shotCount: 3, active: false, onSelect });

    element.props.onClick();

    expect(onSelect).toHaveBeenCalledExactlyOnceWith('alx');
  });

  it('does nothing when the active profile is tapped again', () => {
    const onSelect = vi.fn();
    const element = PlayerChip({ profile: alex, shotCount: 3, active: true, onSelect });

    element.props.onClick();

    expect(onSelect).not.toHaveBeenCalled();
  });
});
