import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { Shot } from '../../types/shot';
import { ClubPanel } from './ClubPanel';

/** React SSR splits interpolated text with comment markers; drop them. */
function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    shot_number: 4,
    ball_speed_mph: 112,
    club_speed_mph: 84,
    smash_factor: 1.33,
    estimated_carry_yards: 160,
    carry_range: [155, 165],
    club: '7-iron',
    timestamp: '2026-11-07T22:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 16.4,
    launch_angle_horizontal: 1.5,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: -4.0,
    club_path_deg: 2.5,
    spin_axis_deg: -1.0,
    spin_rpm: 6800,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    carry_spin_adjusted: 161,
    profile_id: 'jordan',
    profile_name: 'Jordan',
    ...overrides,
  };
}

function render(shots: Shot[], profileId = 'jordan'): string {
  return text(
    renderToString(<ClubPanel shots={shots} profileId={profileId} profileName="Jordan" clubLabel="7 Iron" />)
  );
}

/** The rendered reading cell for one data-reading id. */
function reading(html: string, id: string): string {
  const start = html.indexOf(`data-reading="${id}"`);
  expect(start, id).toBeGreaterThan(-1);
  return html.slice(
    start,
    html.indexOf('data-reading=', start + 1) === -1 ? undefined : html.indexOf('data-reading=', start + 1)
  );
}

describe('ClubPanel', () => {
  it('shows top view, side view and strike for the latest shot, each value with its source', () => {
    const html = render([makeShot()]);

    expect(html).toContain('Top at impact');
    expect(html).toContain('Side at impact');
    expect(html).toContain('Strike');
    expect(reading(html, 'face_angle')).toContain('+1.2');
    expect(reading(html, 'face_angle')).toContain('CALC');
    expect(reading(html, 'club_path')).toContain('+2.5');
    expect(reading(html, 'club_path')).toContain('MEAS');
    expect(reading(html, 'face_to_path')).toContain('-1.3');
    expect(reading(html, 'attack_angle')).toContain('-4.0');
    expect(reading(html, 'attack_angle')).toContain('MEAS');
    expect(reading(html, 'dynamic_loft')).toContain('22.5');
    expect(reading(html, 'dynamic_loft')).not.toContain('+22.5');
    expect(reading(html, 'spin_loft')).toContain('26.5');
    expect(reading(html, 'club_speed')).toContain('84.0');
    expect(reading(html, 'club_speed')).toContain('mph');
    expect(reading(html, 'smash')).toContain('1.33');
  });

  it('names the shot and the D-plane share behind the calculated values', () => {
    const html = render([makeShot()]);

    expect(html).toContain('Shot 4 · 7 Iron');
    expect(html).toContain('about 77% on the face');
    expect(html).toContain('drawn 3× larger');
  });

  it('says impact location is not tracked rather than drawing a contact mark', () => {
    expect(render([makeShot()])).toContain('Impact location needs camera club tracking');
  });

  it('marks camera-fused delivery experimental', () => {
    const html = render([
      makeShot({
        club_path_deg: null,
        club_angle_deg: null,
        experimental_fused_status: 'fused',
        experimental_fused_club_path_deg: 3.1,
        experimental_fused_attack_angle_deg: -3.5,
        experimental_fused_club_path_confidence: 'medium',
        experimental_fused_attack_angle_confidence: 'medium',
      }),
    ]);

    expect(reading(html, 'club_path')).toContain('EXP');
    expect(reading(html, 'club_path')).toContain('camera fused');
    expect(reading(html, 'attack_angle')).toContain('EXP');
  });

  it('shows a dash and the missing input instead of guessing', () => {
    const html = render([makeShot({ angle_source: 'estimated', club_speed_mph: null, smash_factor: null })]);

    expect(reading(html, 'face_angle')).toContain('—');
    expect(reading(html, 'face_angle')).toContain('needs measured launch');
    expect(reading(html, 'face_angle')).not.toContain('°');
    expect(reading(html, 'club_speed')).toContain('—');
    expect(reading(html, 'club_speed')).not.toContain('mph');
    expect(reading(html, 'club_path')).toContain('+2.5');
  });

  it('follows the active golfer and their latest shot', () => {
    const html = render([
      makeShot({ shot_number: 1, club_path_deg: 1.0 }),
      makeShot({ shot_number: 2, club_path_deg: -2.0, profile_id: 'kid' }),
      makeShot({ shot_number: 3, club_path_deg: 0.5, timestamp: '2026-11-07T22:05:00Z' }),
    ]);

    expect(reading(html, 'club_path')).toContain('+0.5');
    expect(html).toContain('Shot 3');
  });

  it('shows an empty state before the golfer has hit a shot', () => {
    const html = render([makeShot({ profile_id: 'kid' })]);

    expect(html).toContain('No club data yet');
    expect(html).not.toContain('data-reading=');
  });

  it('draws a wood face for a driver and an iron face with grooves for an iron', () => {
    const driver = render([makeShot({ club: 'driver' })]);
    const iron = render([makeShot()]);

    expect(driver).not.toContain('club-panel__groove');
    expect(iron).toContain('club-panel__groove');
    expect(driver).toContain('about 85% on the face');
  });
});
