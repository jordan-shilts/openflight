import { useEffect } from 'react';
import { useI18n } from '../../i18n/useI18n';
import type { MessageKey } from '../../i18n/en';
import { changeRigSetup, refreshRigSetup } from '../../services/rigSetup';
import { useUnitPreference } from '../../state/useUnitPreference';
import {
  useRigSetupStore,
  type RigDistanceSource,
  type RigSetupId,
  type RigSetupSnapshot,
} from '../../stores/useRigSetupStore';
import { formatRigDistance, rigDistanceStepMeters, type UnitSystem } from '../../utils/units';
import { SegmentedControl } from '../ui/SegmentedControl';
import { Stepper } from './Stepper';
import './SetupPanel.css';

// Friendly limits. The server has the final say on what it accepts.
const BALL_DISTANCE_RANGE = { min: 0.6, max: 6 };
const NET_DISTANCE_RANGE = { min: 1, max: 15 };
const RADAR_HEIGHT_RANGE = { min: 0.02, max: 1.5 };
/** Where minus and plus begin before a distance is set: OpenFlight's default. */
const DEFAULT_BALL_DISTANCE_M = 1.575;

const SOURCE_LABELS: Record<RigDistanceSource, MessageKey> = {
  camera: 'setup.sourceCamera',
  manual: 'setup.sourceManual',
  none: 'setup.sourceNone',
};

export function SetupPanel() {
  const { unitSystem } = useUnitPreference();
  const snapshot = useRigSetupStore((state) => state.snapshot);
  const saveFailed = useRigSetupStore((state) => state.saveFailed);

  useEffect(() => {
    void refreshRigSetup();
  }, []);

  return <SetupPanelView snapshot={snapshot} saveFailed={saveFailed} unitSystem={unitSystem} />;
}

interface SetupPanelViewProps {
  snapshot: RigSetupSnapshot | null;
  saveFailed: boolean;
  unitSystem: UnitSystem;
}

/** The two saved locations, and everything that can be adjusted at each. */
export function SetupPanelView({ snapshot, saveFailed, unitSystem }: SetupPanelViewProps) {
  const { t } = useI18n();

  if (snapshot === null) {
    return (
      <div className="panel setup-panel">
        <div className="panel__body panel__body--empty">{saveFailed ? t('setup.unavailable') : t('setup.loading')}</div>
      </div>
    );
  }

  const active = snapshot.active;
  const setup = snapshot.setups[active];
  const isCourse = active === 'course';
  const distance = snapshot.effective.ball_distance_m;
  const step = rigDistanceStepMeters(unitSystem);
  const format = (value: number) => formatRigDistance(value, unitSystem);
  const labels = (name: string) => ({
    name,
    decreaseLabel: t('setup.decrease', { name }),
    increaseLabel: t('setup.increase', { name }),
  });

  const source = snapshot.effective.distance_source;
  const waitingForSwing = isCourse && setup.auto_distance && snapshot.camera_ready && source !== 'camera';

  let hint: string | null = null;
  if (!isCourse && setup.ball_distance_m === null) {
    hint = t('setup.hintTapeOnce');
  } else if (!snapshot.camera_ready && (!isCourse || setup.auto_distance)) {
    hint = t('setup.hintTeachCamera', { count: snapshot.scale_samples_needed });
  }

  return (
    <div className="panel setup-panel">
      <div className="panel__body setup-panel__body">
        <SegmentedControl<RigSetupId>
          ariaLabel={t('setup.location')}
          value={active}
          options={[
            { id: 'simulator', label: t('setup.simulator') },
            { id: 'course', label: t('setup.course') },
          ]}
          onChange={(id) => void changeRigSetup({ active: id })}
        />

        <div className="setup-panel__row">
          <div className="setup-panel__label">
            <span>{t('setup.ballDistance')}</span>
            <small>{waitingForSwing ? t('setup.sourceWaiting') : t(SOURCE_LABELS[source])}</small>
          </div>
          <Stepper
            {...labels(t('setup.ballDistance'))}
            {...BALL_DISTANCE_RANGE}
            step={step}
            start={DEFAULT_BALL_DISTANCE_M}
            value={distance}
            format={format}
            onCommit={(value) => changeRigSetup({ ball_distance_m: value })}
          />
          <div className="setup-panel__trailing">
            {isCourse ? (
              <button
                type="button"
                className={`setup-panel__auto${setup.auto_distance ? ' setup-panel__auto--on' : ''}`}
                aria-pressed={setup.auto_distance}
                onClick={() => void changeRigSetup({ auto_distance: true })}
              >
                {t('setup.auto')}
              </button>
            ) : null}
          </div>
        </div>

        <div className="setup-panel__row">
          <div className="setup-panel__label">
            <span>{t('setup.net')}</span>
          </div>
          {setup.net_enabled ? (
            <Stepper
              {...labels(t('setup.net'))}
              {...NET_DISTANCE_RANGE}
              step={step}
              start={setup.net_distance_m}
              value={setup.net_distance_m}
              format={format}
              onCommit={(value) => changeRigSetup({ net_distance_m: value })}
            />
          ) : (
            <div />
          )}
          <div className="setup-panel__trailing">
            <SegmentedControl<'off' | 'on'>
              ariaLabel={t('setup.net')}
              value={setup.net_enabled ? 'on' : 'off'}
              options={[
                { id: 'off', label: t('setup.off') },
                { id: 'on', label: t('setup.on') },
              ]}
              onChange={(id) => void changeRigSetup({ net_enabled: id === 'on' })}
            />
          </div>
        </div>

        <div className="setup-panel__row">
          <div className="setup-panel__label">
            <span>{t('setup.radarHeight')}</span>
          </div>
          <Stepper
            {...labels(t('setup.radarHeight'))}
            {...RADAR_HEIGHT_RANGE}
            step={step}
            start={snapshot.radar_height_m}
            value={snapshot.radar_height_m}
            format={format}
            onCommit={(value) => changeRigSetup({ radar_height_m: value })}
          />
          <div className="setup-panel__trailing" />
        </div>

        {saveFailed ? (
          <p className="setup-panel__hint setup-panel__hint--error">{t('setup.saveFailed')}</p>
        ) : hint ? (
          <p className="setup-panel__hint">{hint}</p>
        ) : null}
      </div>
    </div>
  );
}
