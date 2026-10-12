import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  COORDINATOR_SWITCHES,
  COORDINATOR_NUMBERS,
  COORDINATOR_SETTING_NAMES,
  GO_LIVE_FIELD,
  GO_LIVE_PHRASE,
  SHOWN_PREFIX,
  describeCoordinatorSettings,
  planCoordinatorSettingsSave,
  settingNumber,
} from '../../src/services/coordinator-settings';

// services/coordinator-settings.ts: what the admin settings page shows for the
// worker's variables and what a submitted form may write. The route tests
// (routes.admin-settings.test.ts) drive the same rules through the page; here
// the rules are checked directly, and the page's list of switches is checked
// against the Python code that reads each variable.

const REPO = path.resolve(__dirname, '../../..');
const read = (relative: string): string => fs.readFileSync(path.join(REPO, relative), 'utf8');

const shown = (name: string, state: 'on' | 'off' | string): Record<string, string> => ({ [SHOWN_PREFIX + name]: state });

describe('the page reads each switch the way the worker code does', () => {
  const worker = read('queue/worker_pg.py');
  const tick = read('coordinator/tick.py');
  const config = read('coordinator/config.py');

  it('COORDINATOR_ENABLED is an arming switch that is off when not set', () => {
    const sw = COORDINATOR_SWITCHES.find((s) => s.name === 'COORDINATOR_ENABLED');
    expect(sw).toMatchObject({ kind: 'arming', unset: false, confirmWhen: true });
    // The worker starts the coordinator on arming_switch with its default (False).
    expect(worker).toContain('if arming_switch("COORDINATOR_ENABLED"):');
  });

  it('COORDINATOR_DRY_RUN is a safety switch that is on when not set', () => {
    const sw = COORDINATOR_SWITCHES.find((s) => s.name === 'COORDINATOR_DRY_RUN');
    expect(sw).toMatchObject({ kind: 'safety', unset: true, confirmWhen: false });
    // safety_switch with its default (True).
    expect(worker).toContain('dry_run = safety_switch("COORDINATOR_DRY_RUN")');
  });

  it('every agent toggle on the page is one the tick reads, as an arming switch that is on when not set', () => {
    const agents = COORDINATOR_SWITCHES.filter((s) => s.name.startsWith('AGENT_'));
    expect(agents).toHaveLength(8);
    const envMap = /env_map = \{([\s\S]*?)\n {4}\}/.exec(tick);
    expect(envMap).not.toBeNull();
    for (const sw of agents) {
      expect(envMap![1], sw.name).toContain(`"${sw.name}"`);
      expect(sw).toMatchObject({ kind: 'arming', unset: true, confirmWhen: null });
    }
    expect(tick).toContain('return arming_switch(env_key, default=True)');
  });

  it('the number fields default to what the coordinator uses when the variable is not set', () => {
    const byName = Object.fromEntries(COORDINATOR_NUMBERS.map((n) => [n.name, n]));
    expect(config).toContain(`_float("COORDINATOR_DAILY_BUDGET", ${byName.COORDINATOR_DAILY_BUDGET.unset.toFixed(1)}, min_val=0.0)`);
    expect(config).toContain(`_int("COORDINATOR_INTERVAL_S", ${byName.COORDINATOR_INTERVAL_S.unset}, min_val=1)`);
    // The page writes whole seconds only, inside what the coordinator accepts.
    expect(byName.COORDINATOR_INTERVAL_S).toMatchObject({ integer: true });
    expect(byName.COORDINATOR_INTERVAL_S.min).toBeGreaterThanOrEqual(1);
    expect(byName.COORDINATOR_DAILY_BUDGET.min).toBe(0);
  });

  it('these twelve names are everything the page can write', () => {
    expect([...COORDINATOR_SETTING_NAMES].sort()).toEqual([
      'AGENT_ARCHITECT_ENABLED', 'AGENT_BACKEND_ENABLED', 'AGENT_CUSTOMER_ENABLED', 'AGENT_DOCS_ENABLED',
      'AGENT_FINANCIAL_ENABLED', 'AGENT_INFRA_ENABLED', 'AGENT_QA_ENABLED', 'AGENT_SECURITY_ENABLED',
      'COORDINATOR_DAILY_BUDGET', 'COORDINATOR_DRY_RUN', 'COORDINATOR_ENABLED', 'COORDINATOR_INTERVAL_S',
    ]);
  });
});

describe('describeCoordinatorSettings', () => {
  it('shows the defaults for variables that are not set', () => {
    const view = describeCoordinatorSettings({});
    expect(view.readable).toBe(true);
    expect(view.switches.COORDINATOR_DRY_RUN.on).toBe(true);
    expect(view.switches.COORDINATOR_ENABLED.on).toBe(false);
    expect(view.switches.AGENT_INFRA_ENABLED.on).toBe(true);
    expect(view.numbers.COORDINATOR_DAILY_BUDGET.value).toBe('20');
    expect(view.numbers.COORDINATOR_INTERVAL_S.value).toBe('1800');
    expect(view.summary).toContain('coordinator is off');
  });

  it('says LIVE only when the coordinator is on and dry run is off', () => {
    const live = (vars: Record<string, string>): boolean => describeCoordinatorSettings(vars).summary.includes('LIVE');
    expect(live({ COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'false' })).toBe(true);
    expect(live({ COORDINATOR_ENABLED: '1', COORDINATOR_DRY_RUN: 'off' })).toBe(true);
    expect(live({ COORDINATOR_ENABLED: 'true' })).toBe(false);
    expect(live({ COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'flase' })).toBe(false);
    expect(live({ COORDINATOR_DRY_RUN: 'false' })).toBe(false);
    expect(live({ COORDINATOR_ENABLED: 'ture', COORDINATOR_DRY_RUN: 'false' })).toBe(false);
  });

  it('draws nothing when the variables could not be read', () => {
    expect(describeCoordinatorSettings(null)).toEqual({ readable: false, switches: {}, numbers: {}, summary: '' });
  });

  it('is not fooled by inherited property names', () => {
    const view = describeCoordinatorSettings(Object.create({ COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'false' }));
    expect(view.switches.COORDINATOR_ENABLED.on).toBe(false);
    expect(view.switches.COORDINATOR_DRY_RUN.on).toBe(true);
  });

  it('shows the default in a number field whose stored text it would not write, and says so', () => {
    const view = describeCoordinatorSettings({ COORDINATOR_DAILY_BUDGET: 'Infinity', COORDINATOR_INTERVAL_S: '5' });
    expect(view.numbers.COORDINATOR_DAILY_BUDGET.value).toBe('20');
    expect(view.numbers.COORDINATOR_DAILY_BUDGET.note).toContain('Infinity');
    expect(view.numbers.COORDINATOR_INTERVAL_S.value).toBe('1800');
    expect(view.numbers.COORDINATOR_INTERVAL_S.note).toContain('outside what this page writes');
  });
});

describe('settingNumber', () => {
  const [budget, interval] = COORDINATOR_NUMBERS;

  it.each(['0', '20', '12.5', '500', ' 42 '])('takes %j as a budget', (text) => {
    expect(settingNumber(budget, text)).toBe(Number(text));
  });

  it.each(['', 'abc', '-5', '+5', '1e2', '1e999', 'Infinity', 'NaN', '0x10', '500.01', '501', '99999999', '1_000', '12.', '.5', '1,5'])(
    'refuses %j as a budget',
    (text) => {
      expect(settingNumber(budget, text)).toBeNull();
    }
  );

  it.each(['1800.5', '299', '7201', '0', '60.0001'])('refuses %j as an interval', (text) => {
    expect(settingNumber(interval, text)).toBeNull();
  });

  it.each(['300', '1800', '7200'])('takes %j as an interval', (text) => {
    expect(settingNumber(interval, text)).toBe(Number(text));
  });
});

describe('planCoordinatorSettingsSave', () => {
  it('writes nothing for an empty form', () => {
    expect(planCoordinatorSettingsSave({})).toEqual({ writes: [], refusal: null });
  });

  it('never reads a missing control as off: no marker, no write', () => {
    // Every value a form could send, with no shown_ field for any of them.
    const body: Record<string, string> = { [GO_LIVE_FIELD]: GO_LIVE_PHRASE };
    for (const sw of COORDINATOR_SWITCHES) body[sw.name] = 'false';
    for (const num of COORDINATOR_NUMBERS) body[num.name] = '300';
    expect(planCoordinatorSettingsSave(body)).toEqual({ writes: [], refusal: null });
  });

  it('writes nothing when every control comes back as it was shown', () => {
    const body: Record<string, string> = {
      ...shown('COORDINATOR_DRY_RUN', 'on'), COORDINATOR_DRY_RUN: 'true',
      ...shown('COORDINATOR_ENABLED', 'off'),
      ...shown('AGENT_QA_ENABLED', 'on'), AGENT_QA_ENABLED: 'true',
      ...shown('AGENT_DOCS_ENABLED', 'off'),
      ...shown('COORDINATOR_DAILY_BUDGET', '20'), COORDINATOR_DAILY_BUDGET: '20.0',
      ...shown('COORDINATOR_INTERVAL_S', '1800'), COORDINATOR_INTERVAL_S: '1800',
    };
    expect(planCoordinatorSettingsSave(body)).toEqual({ writes: [], refusal: null });
  });

  it.each([
    ['dry run turned off', { ...shown('COORDINATOR_DRY_RUN', 'on') }],
    ['the coordinator enabled', { ...shown('COORDINATOR_ENABLED', 'off'), COORDINATOR_ENABLED: 'true' }],
  ])('refuses %s without the typed phrase, and plans no write at all', (_label, change) => {
    for (const typed of [undefined, '', 'yes', 'GO LIVE', ['go live'], { phrase: 'go live' }, 1]) {
      const body: Record<string, unknown> = { ...change, ...shown('AGENT_QA_ENABLED', 'on'), [GO_LIVE_FIELD]: typed };
      const plan = planCoordinatorSettingsSave(body);
      expect(plan.writes, JSON.stringify(typed)).toEqual([]);
      expect(plan.refusal, JSON.stringify(typed)).toContain(GO_LIVE_PHRASE);
    }
  });

  it('plans the live change once the phrase is typed, as canonical text', () => {
    const plan = planCoordinatorSettingsSave({
      ...shown('COORDINATOR_DRY_RUN', 'on'),
      ...shown('COORDINATOR_ENABLED', 'off'), COORDINATOR_ENABLED: 'true',
      [GO_LIVE_FIELD]: ` ${GO_LIVE_PHRASE} `,
    });
    expect(plan.refusal).toBeNull();
    expect(plan.writes).toEqual([
      { name: 'COORDINATOR_ENABLED', value: 'true' },
      { name: 'COORDINATOR_DRY_RUN', value: 'false' },
    ]);
  });

  it('needs no phrase for a change toward the safe state or for an agent toggle', () => {
    const plan = planCoordinatorSettingsSave({
      ...shown('COORDINATOR_DRY_RUN', 'off'), COORDINATOR_DRY_RUN: 'true',
      ...shown('COORDINATOR_ENABLED', 'on'),
      ...shown('AGENT_QA_ENABLED', 'off'), AGENT_QA_ENABLED: 'true',
      ...shown('AGENT_DOCS_ENABLED', 'on'),
    });
    expect(plan.refusal).toBeNull();
    // Toward the safe state first; the one that arms something last.
    expect(plan.writes.map((w) => `${w.name}=${w.value}`)).toEqual([
      'COORDINATOR_ENABLED=false', 'COORDINATOR_DRY_RUN=true', 'AGENT_DOCS_ENABLED=false', 'AGENT_QA_ENABLED=true',
    ]);
  });

  it('only ever plans true or false for a switch, whatever the form sent', () => {
    for (const sent of ['TRUE', '1', 'yes', 'on', 'false', '', ['true', 'true'], { a: 1 }, 0]) {
      const plan = planCoordinatorSettingsSave({ ...shown('AGENT_QA_ENABLED', 'on'), AGENT_QA_ENABLED: sent });
      // Anything but the checkbox's own value "true" is an unchecked box.
      expect(plan.writes, JSON.stringify(sent)).toEqual([{ name: 'AGENT_QA_ENABLED', value: 'false' }]);
    }
  });

  it('does not take a marker that is not the page\'s own on or off', () => {
    for (const marker of ['', 'true', 'ON', '1', 'checked']) {
      expect(planCoordinatorSettingsSave({ ...shown('COORDINATOR_DRY_RUN', marker) }).writes).toEqual([]);
    }
    const arrayMarker = { [SHOWN_PREFIX + 'COORDINATOR_DRY_RUN']: ['on', 'off'] };
    expect(planCoordinatorSettingsSave(arrayMarker)).toEqual({ writes: [], refusal: null });
  });

  it('writes a number only when it changed, and refuses the whole save for one it would not write', () => {
    const changed = planCoordinatorSettingsSave({
      ...shown('COORDINATOR_DAILY_BUDGET', '20'), COORDINATOR_DAILY_BUDGET: '35',
      ...shown('COORDINATOR_INTERVAL_S', '1800'), COORDINATOR_INTERVAL_S: '1800',
    });
    expect(changed).toEqual({ writes: [{ name: 'COORDINATOR_DAILY_BUDGET', value: '35' }], refusal: null });

    const bad = planCoordinatorSettingsSave({
      ...shown('COORDINATOR_INTERVAL_S', '1800'), COORDINATOR_INTERVAL_S: '1800.5',
      ...shown('AGENT_QA_ENABLED', 'on'),
    });
    expect(bad.writes).toEqual([]);
    expect(bad.refusal).toContain('Tick Interval must be a whole number from 300 to 7200.');
  });

  it('leaves a number alone when its field came back empty', () => {
    const plan = planCoordinatorSettingsSave({ ...shown('COORDINATOR_DAILY_BUDGET', '20'), COORDINATOR_DAILY_BUDGET: '  ' });
    expect(plan).toEqual({ writes: [], refusal: null });
  });
});
