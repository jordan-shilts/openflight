import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale, t } from '../../i18n';
import type { CloudStatus } from '../../types/socket';
import { cloudStatusText, cloudStatusTone } from './cloudStatus';

function status(overrides: Partial<CloudStatus>): CloudStatus {
  return { state: 'online', pending: 0, message: '', reason: '', ...overrides };
}

describe('cloudStatusText', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('says all shots are saved when online with nothing waiting', () => {
    expect(cloudStatusText(status({ state: 'online', pending: 0 }), t)).toBe('Online · all shots saved');
  });

  it('counts shots still going up while online', () => {
    expect(cloudStatusText(status({ state: 'online', pending: 1 }), t)).toBe('Online · sending 1 shot');
    expect(cloudStatusText(status({ state: 'online', pending: 4 }), t)).toBe('Online · sending 4 shots');
  });

  it('counts the shots waiting while offline', () => {
    expect(cloudStatusText(status({ state: 'offline', pending: 12 }), t)).toBe(
      'Offline · 12 shots waiting, will send when back online'
    );
    expect(cloudStatusText(status({ state: 'offline', pending: 1 }), t)).toBe(
      'Offline · 1 shot waiting, will send when back online'
    );
  });

  it('still promises to send when the count is unknown or zero', () => {
    expect(cloudStatusText(status({ state: 'offline', pending: null }), t)).toBe(
      'Offline · shots will send when back online'
    );
    expect(cloudStatusText(status({ state: 'offline', pending: 0 }), t)).toBe(
      'Offline · shots will send when back online'
    );
  });

  it('never calls a refused device offline', () => {
    expect(cloudStatusText(status({ state: 'needs_attention', reason: 'unauthorized', pending: 3 }), t)).toBe(
      'Device not registered or wrong key'
    );
    expect(cloudStatusText(status({ state: 'needs_attention', reason: 'schema_missing' }), t)).toBe(
      'Supabase tables missing: run schema.sql'
    );
  });

  it('says when it is not set up', () => {
    expect(cloudStatusText(status({ state: 'not_set_up', pending: null }), t)).toBe('Not set up');
  });

  it('follows the active language', () => {
    setActiveLocale('es');
    expect(cloudStatusText(status({ state: 'offline', pending: 12 }), t)).toBe(
      'Sin conexión · 12 golpes en espera, se enviarán al volver la conexión'
    );
    setActiveLocale('fr');
    expect(cloudStatusText(status({ state: 'online', pending: 0 }), t)).toBe('En ligne · tous les coups enregistrés');
    setActiveLocale('pt');
    expect(cloudStatusText(status({ state: 'not_set_up', pending: null }), t)).toBe('Não configurado');
  });
});

describe('cloudStatusTone', () => {
  it('maps each state to a severity', () => {
    expect(cloudStatusTone(status({ state: 'online' }))).toBe('ok');
    expect(cloudStatusTone(status({ state: 'offline' }))).toBe('warn');
    expect(cloudStatusTone(status({ state: 'needs_attention' }))).toBe('error');
    expect(cloudStatusTone(status({ state: 'not_set_up' }))).toBe('off');
  });
});
