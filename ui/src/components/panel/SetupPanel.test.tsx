import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { RigSetupSnapshot } from '../../stores/useRigSetupStore';
import { formatRigDistance, rigDistanceStepMeters, type UnitSystem } from '../../utils/units';
import { SetupPanelView } from './SetupPanel';

function snapshot(overrides: Partial<RigSetupSnapshot> = {}): RigSetupSnapshot {
  return {
    active: 'simulator',
    setups: {
      simulator: {
        ball_distance_m: 1.4,
        auto_distance: false,
        net_enabled: true,
        net_distance_m: 4.6,
        ball_height_m: 0.04,
      },
      course: {
        ball_distance_m: null,
        auto_distance: true,
        net_enabled: false,
        net_distance_m: 4.6,
        ball_height_m: 0.021,
      },
    },
    radar_height_m: 0.1524,
    camera_ready: true,
    scale_samples_needed: 0,
    effective: { ball_distance_m: 1.4, distance_source: 'manual', net_distance_m: 4.6 },
    ...overrides,
  };
}

function render(state: RigSetupSnapshot | null, saveFailed = false, unitSystem: UnitSystem = 'imperial') {
  return renderToString(<SetupPanelView snapshot={state} saveFailed={saveFailed} unitSystem={unitSystem} />);
}

describe('SetupPanelView', () => {
  it('shows the simulator with its taped distance, in feet and inches', () => {
    const html = render(snapshot());

    expect(html).toContain('Simulator');
    expect(html).toContain('Ball distance');
    expect(html).toContain('Set by you');
    expect(html).toContain('4 ft 7 in');
    expect(html).toContain('Radar height');
    expect(html).toContain('6 in');
  });

  it('shows metres when that is the chosen unit', () => {
    expect(render(snapshot(), false, 'metric')).toContain('1.40 m');
  });

  it('offers Auto only on the course', () => {
    expect(render(snapshot())).not.toContain('setup-panel__auto');

    const html = render(snapshot({ active: 'course' }));
    expect(html).toContain('setup-panel__auto setup-panel__auto--on');
    expect(html).toContain('>Auto<');
  });

  it('shows a camera-measured distance as coming from the camera', () => {
    const html = render(
      snapshot({
        active: 'course',
        effective: { ball_distance_m: 2.1, distance_source: 'camera', net_distance_m: null },
      })
    );

    expect(html).toContain('From camera');
    expect(html).toContain('6 ft 11 in');
  });

  it('says when Auto is on and waiting for a swing', () => {
    const html = render(
      snapshot({
        active: 'course',
        effective: { ball_distance_m: null, distance_source: 'none', net_distance_m: null },
      })
    );

    expect(html).toContain('Waiting for a swing');
    expect(html).not.toContain('Not set');
  });

  it('shows a dash when no distance is known yet', () => {
    const html = render(
      snapshot({
        active: 'course',
        effective: { ball_distance_m: null, distance_source: 'none', net_distance_m: null },
      })
    );

    expect(html).toContain('Waiting for a swing');
    expect(html).toContain('—');
  });

  it('hides the net distance when the net is off', () => {
    const on = render(snapshot());
    const off = render(snapshot({ active: 'course' }));

    expect(on.match(/role="group" aria-label="Net"/g)).toHaveLength(2);
    expect(off.match(/role="group" aria-label="Net"/g)).toHaveLength(1);
  });

  it('asks for the tape measure only before the simulator is measured', () => {
    const base = snapshot();
    const unmeasured = snapshot({
      setups: { ...base.setups, simulator: { ...base.setups.simulator, ball_distance_m: null } },
      camera_ready: false,
      scale_samples_needed: 3,
    });

    expect(render(unmeasured)).toContain('Measure this once with a tape');
    expect(render(base)).not.toContain('Measure this once');
  });

  it('says how many swings the camera still needs', () => {
    expect(render(snapshot({ camera_ready: false, scale_samples_needed: 2 }))).toContain('to teach the camera: 2');
    expect(render(snapshot({ active: 'course', camera_ready: false, scale_samples_needed: 3 }))).toContain(
      'to teach the camera: 3'
    );
  });

  it('does not nag about the camera on a course setup adjusted by hand', () => {
    const base = snapshot();
    const html = render(
      snapshot({
        active: 'course',
        camera_ready: false,
        scale_samples_needed: 3,
        setups: { ...base.setups, course: { ...base.setups.course, auto_distance: false } },
      })
    );

    expect(html).not.toContain('teach the camera');
  });

  it('says so when a change did not save', () => {
    expect(render(snapshot(), true)).toContain('That change didn&#x27;t save');
  });

  it('shows a loading state, then an unavailable state', () => {
    expect(render(null)).toContain('Loading setup');
    expect(render(null, true)).toContain('available right now');
  });
});

describe('rig setup units', () => {
  it('formats feet and inches', () => {
    expect(formatRigDistance(1.4, 'imperial')).toBe('4 ft 7 in');
    expect(formatRigDistance(0.1524, 'imperial')).toBe('6 in');
    expect(formatRigDistance(0.3048, 'imperial')).toBe('1 ft 0 in');
  });

  it('rolls 12 inches over to the next foot', () => {
    expect(formatRigDistance(0.6096, 'imperial')).toBe('2 ft 0 in');
  });

  it('formats metres to the centimetre', () => {
    expect(formatRigDistance(1.4, 'metric')).toBe('1.40 m');
  });

  it('steps by an inch or a centimetre', () => {
    expect(rigDistanceStepMeters('imperial')).toBeCloseTo(0.0254);
    expect(rigDistanceStepMeters('metric')).toBeCloseTo(0.01);
  });
});
