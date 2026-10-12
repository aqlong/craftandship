// What the admin settings page shows and what a save of it may write.
//
// The page edits environment variables of the worker service, which the worker
// reads when it starts. Two rules keep a save from changing something the
// admin did not choose:
//
//   1. The page shows each switch in the state the worker will read from the
//      stored text (utils/env-switch.ts, the same rules as the worker's
//      parser), not in the state of a string comparison. A variable that is
//      not set shows as its default: dry run on, the coordinator off, each
//      agent on.
//   2. A save writes a variable only when the submitted form carried that
//      control (a hidden `shown_<NAME>` field holding what the page displayed)
//      and the submitted value differs from it. A field that is absent is
//      never read as "off".
//
// Turning dry run off and enabling the coordinator are the two changes that
// let the coordinator act, so a save containing either is refused unless the
// form also carries the typed confirmation phrase.

import { armingSwitch, safetySwitch } from '../utils/env-switch';

export type SwitchKind = 'safety' | 'arming';

export interface CoordinatorSwitch {
  name: string;
  kind: SwitchKind;
  /** What the worker reads when the variable is not set at all. */
  unset: boolean;
  /** The state that needs the typed confirmation; null when neither state does. */
  confirmWhen: boolean | null;
}

const agentSwitch = (name: string): CoordinatorSwitch => ({ name, kind: 'arming', unset: true, confirmWhen: null });

export const COORDINATOR_SWITCHES: readonly CoordinatorSwitch[] = [
  { name: 'COORDINATOR_ENABLED', kind: 'arming', unset: false, confirmWhen: true },
  { name: 'COORDINATOR_DRY_RUN', kind: 'safety', unset: true, confirmWhen: false },
  agentSwitch('AGENT_INFRA_ENABLED'),
  agentSwitch('AGENT_BACKEND_ENABLED'),
  agentSwitch('AGENT_FINANCIAL_ENABLED'),
  agentSwitch('AGENT_CUSTOMER_ENABLED'),
  agentSwitch('AGENT_DOCS_ENABLED'),
  agentSwitch('AGENT_SECURITY_ENABLED'),
  agentSwitch('AGENT_QA_ENABLED'),
  agentSwitch('AGENT_ARCHITECT_ENABLED'),
];

export interface CoordinatorNumber {
  name: string;
  label: string;
  /** What the worker uses when the variable is not set. */
  unset: number;
  min: number;
  max: number;
  integer: boolean;
}

// The bounds are the ones the form states, checked again on the server. The
// interval is whole seconds: the worker reads it as an integer.
export const COORDINATOR_NUMBERS: readonly CoordinatorNumber[] = [
  { name: 'COORDINATOR_DAILY_BUDGET', label: 'Daily Budget', unset: 20, min: 0, max: 500, integer: false },
  { name: 'COORDINATOR_INTERVAL_S', label: 'Tick Interval', unset: 1800, min: 300, max: 7200, integer: true },
];

/** Every variable this page can write. Nothing else is ever sent to Railway from it. */
export const COORDINATOR_SETTING_NAMES: readonly string[] = [
  ...COORDINATOR_SWITCHES.map((s) => s.name),
  ...COORDINATOR_NUMBERS.map((n) => n.name),
];

export const SHOWN_PREFIX = 'shown_';
export const GO_LIVE_FIELD = 'confirm_live';
export const GO_LIVE_PHRASE = 'go live';

const MAX_NOTE_TEXT = 40;

function stored(vars: Record<string, string>, name: string): string | undefined {
  if (!Object.prototype.hasOwnProperty.call(vars, name)) return undefined;
  const value: unknown = vars[name];
  return typeof value === 'string' ? value : undefined;
}

/** The state the worker reads for one switch from the stored text. */
export function switchState(sw: CoordinatorSwitch, raw: string | undefined): boolean {
  return sw.kind === 'safety' ? safetySwitch(raw, sw.unset) : armingSwitch(raw, sw.unset);
}

const onOff = (state: boolean): string => (state ? 'on' : 'off');

// Plain decimal text only: no sign, no exponent, no "Infinity". An infinite
// daily budget would switch off the spend cap on the autonomous agents.
const PLAIN_NUMBER = /^\d{1,7}(\.\d{1,4})?$/;

/** The number a field's text stands for, or null when this page does not write it. */
export function settingNumber(spec: CoordinatorNumber, text: string): number | null {
  const trimmed = text.trim();
  if (!PLAIN_NUMBER.test(trimmed)) return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < spec.min || value > spec.max) return null;
  if (spec.integer && !Number.isInteger(value)) return null;
  return value;
}

export interface SwitchView { on: boolean; note: string }
export interface NumberView { value: string; note: string; min: number; max: number; step: string }

export interface CoordinatorSettingsView {
  /** False when the worker's variables could not be read: nothing is shown and nothing can be saved. */
  readable: boolean;
  switches: Record<string, SwitchView>;
  numbers: Record<string, NumberView>;
  summary: string;
}

function switchNote(raw: string | undefined, state: boolean): string {
  if (raw === undefined) return `Not set, which the worker reads as ${onOff(state)}.`;
  if (raw === 'true' || raw === 'false') return `Stored as ${raw}.`;
  return `Stored as "${raw.slice(0, MAX_NOTE_TEXT)}", which the worker reads as ${onOff(state)}.`;
}

function numberView(spec: CoordinatorNumber, raw: string | undefined): NumberView {
  const bounds = { min: spec.min, max: spec.max, step: spec.integer ? '1' : '0.01' };
  if (raw === undefined) {
    return { ...bounds, value: String(spec.unset), note: `Not set: the worker uses ${spec.unset}.` };
  }
  const value = settingNumber(spec, raw);
  if (value === null) {
    return {
      ...bounds,
      value: String(spec.unset),
      note: `Stored as "${raw.slice(0, MAX_NOTE_TEXT)}", which is outside what this page writes (${spec.min} to ${spec.max}). `
        + 'The field shows the default; the stored value stays until you change the field.',
    };
  }
  return { ...bounds, value: String(value), note: `Stored as ${raw.trim()}.` };
}

function summarize(switches: Record<string, SwitchView>): string {
  if (!switches.COORDINATOR_ENABLED.on) {
    return 'With these values the coordinator is off: the worker runs the legacy ops agent in its place.';
  }
  if (switches.COORDINATOR_DRY_RUN.on) {
    return 'With these values the coordinator runs in dry run: it records its gated actions instead of performing them.';
  }
  return 'With these values the coordinator is LIVE: its agents perform their actions.';
}

/** The page's view of the worker's variables; `null` means they could not be read. */
export function describeCoordinatorSettings(vars: Record<string, string> | null): CoordinatorSettingsView {
  if (vars === null) {
    return { readable: false, switches: {}, numbers: {}, summary: '' };
  }
  const switches: Record<string, SwitchView> = {};
  for (const sw of COORDINATOR_SWITCHES) {
    const raw = stored(vars, sw.name);
    const on = switchState(sw, raw);
    switches[sw.name] = { on, note: switchNote(raw, on) };
  }
  const numbers: Record<string, NumberView> = {};
  for (const spec of COORDINATOR_NUMBERS) {
    numbers[spec.name] = numberView(spec, stored(vars, spec.name));
  }
  return { readable: true, switches, numbers, summary: summarize(switches) };
}

export interface SettingsWrite { name: string; value: string }

export interface SettingsPlan {
  /** In the order to send them: changes toward the safe state first, changes that arm something last. */
  writes: SettingsWrite[];
  /** Why nothing may be written, or null. */
  refusal: string | null;
}

interface RankedWrite extends SettingsWrite { rank: number }

const RANK_SAFE = 0;
const RANK_NUMBER = 1;
const RANK_ARMING = 2;

function refuse(reason: string): SettingsPlan {
  return { writes: [], refusal: `${reason} Nothing was saved.` };
}

function plannedSwitches(body: Record<string, unknown>): { writes: RankedWrite[]; needsConfirmation: boolean } {
  const writes: RankedWrite[] = [];
  let needsConfirmation = false;
  for (const sw of COORDINATOR_SWITCHES) {
    const shown = body[SHOWN_PREFIX + sw.name];
    // No marker: the form did not carry this control, so its absence means nothing.
    if (shown !== 'on' && shown !== 'off') continue;
    const wanted = body[sw.name] === 'true';
    if (wanted === (shown === 'on')) continue;
    const towardSafe = sw.kind === 'safety' ? wanted : !wanted;
    writes.push({ name: sw.name, value: wanted ? 'true' : 'false', rank: towardSafe ? RANK_SAFE : RANK_ARMING });
    if (sw.confirmWhen === wanted) needsConfirmation = true;
  }
  return { writes, needsConfirmation };
}

function plannedNumbers(body: Record<string, unknown>): { writes: RankedWrite[]; invalid: CoordinatorNumber | null } {
  const writes: RankedWrite[] = [];
  for (const spec of COORDINATOR_NUMBERS) {
    const shown = body[SHOWN_PREFIX + spec.name];
    const submitted = body[spec.name];
    if (typeof shown !== 'string' || typeof submitted !== 'string' || submitted.trim() === '') continue;
    const value = settingNumber(spec, submitted);
    if (value === null) return { writes: [], invalid: spec };
    if (value === settingNumber(spec, shown)) continue;
    writes.push({ name: spec.name, value: String(value), rank: RANK_NUMBER });
  }
  return { writes, invalid: null };
}

function typedConfirmation(body: Record<string, unknown>): boolean {
  const typed = body[GO_LIVE_FIELD];
  return typeof typed === 'string' && typed.trim() === GO_LIVE_PHRASE;
}

/**
 * Decide what one submitted settings form writes. Pure: it reads only the
 * form body. The values it returns are canonical (`true`, `false`, or a plain
 * number), and the names come from the lists above, never from the body.
 */
export function planCoordinatorSettingsSave(body: Record<string, unknown>): SettingsPlan {
  const switches = plannedSwitches(body);
  const numbers = plannedNumbers(body);
  if (numbers.invalid) {
    const spec = numbers.invalid;
    const kind = spec.integer ? 'a whole number' : 'a number';
    return refuse(`${spec.label} must be ${kind} from ${spec.min} to ${spec.max}.`);
  }
  if (switches.needsConfirmation && !typedConfirmation(body)) {
    return refuse(
      `Turning Dry Run off or enabling the coordinator lets the coordinator act. Type "${GO_LIVE_PHRASE}" in the confirmation field to save that change.`
    );
  }
  const ranked = [...switches.writes, ...numbers.writes].sort((a, b) => a.rank - b.rank);
  return { writes: ranked.map(({ name, value }) => ({ name, value })), refusal: null };
}
