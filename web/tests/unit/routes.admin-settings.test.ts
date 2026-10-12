import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import app from '../../src/app';

// The admin settings page and its save, through the real routes with an admin
// session. Railway is stood in for by a small stateful fake behind `fetch`:
// it answers the variables query from `railway.vars` and records every
// variableUpsert in `railway.writes`, so each test can say exactly what a
// save would have written to the worker service.

// Mock DB pool
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendPreviewReadyEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));
vi.mock('../../src/services/stripe', () => ({
}));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
}));
// Saving settings is a Railway write, gated by isRailwayWriteEnabled()
// (NODE_ENV=production AND CAS_ALLOW_RAILWAY_WRITES=true; the rule itself is
// covered in services.railway.test.ts). These tests switch the gate directly
// instead of faking a production environment for the whole app.
const railwayWrites = vi.hoisted(() => ({ enabled: true }));
vi.mock('../../src/services/railway', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/services/railway')>();
  return { ...actual, isRailwayWriteEnabled: () => railwayWrites.enabled };
});
// The page's input is observed (the real function still runs): the test reads
// what the route handed to the view, not only what the page drew.
vi.mock('../../src/services/coordinator-settings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/services/coordinator-settings')>();
  return { ...actual, describeCoordinatorSettings: vi.fn(actual.describeCoordinatorSettings) };
});
vi.mock('../../src/services/anthropic-costs', () => ({
  getAnthropicCosts: vi.fn().mockResolvedValue(null),
  getAnthropicCostsAndStatus: vi.fn().mockResolvedValue({ rows: null, partial: false, partialReason: null }),
  rebuildAnthropicCostsCache: vi.fn().mockResolvedValue({ rowsFetched: 0, success: true }),
}));

import pool from '../../src/db/index';
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

import supertest from 'supertest';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';
import { armingSwitch, safetySwitch } from '../../src/utils/env-switch';
import { describeCoordinatorSettings } from '../../src/services/coordinator-settings';

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

async function makeAdminAgent() {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'admin-id', email: 'founder@example.com', name: 'Admin', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email: 'founder@example.com', password: 'password123' });
  // Every later query on these pages is the pending-approvals count.
  mockQuery.mockResolvedValue({ rows: [{ count: '0' }] });
  return agent;
}

// ── The Railway stand-in ────────────────────────────────────────────────────

interface Write { name: string; value: string }

const railway = {
  /** The worker service's variables; null makes the read fail. */
  vars: {} as Record<string, string> | null,
  writes: [] as Write[],
  /** Name of a variable whose upsert Railway refuses. */
  refuses: null as string | null,
  unreachable: false,
};

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

async function fakeRailway(_url: unknown, init: RequestInit): Promise<unknown> {
  if (railway.unreachable) throw new Error('Network error');
  const query = (JSON.parse(String(init.body)) as { query: string }).query;
  if (query.startsWith('query')) {
    if (railway.vars === null) return { ok: false, json: async () => ({ errors: [{ message: 'Not Authorized' }] }) };
    // A real service holds other variables too; the page must ignore them.
    return { ok: true, json: async () => ({ data: { variables: { ...railway.vars, PORT: '8080' } } }) };
  }
  const match = /name: "([^"]*)", value: "([^"]*)"/.exec(query);
  if (!match) throw new Error('unexpected Railway call: ' + query);
  const write = { name: match[1], value: match[2] };
  railway.writes.push(write);
  if (railway.refuses === write.name) return { ok: true, json: async () => ({ data: { variableUpsert: false } }) };
  if (railway.vars !== null) railway.vars[write.name] = write.value;
  return { ok: true, json: async () => ({ data: { variableUpsert: true } }) };
}

// ── The page as a browser sees it ───────────────────────────────────────────

interface Control { type: string; name: string; value: string; checked: boolean; disabled: boolean }

function attribute(tag: string, name: string): string | null {
  const match = new RegExp(`\\s${name}="([^"]*)"`).exec(tag);
  return match ? match[1] : null;
}

function hasFlag(tag: string, name: string): boolean {
  return new RegExp(`\\s${name}(?=[\\s>/=])`).test(tag);
}

/** Every input of the settings form, in document order. */
function settingsForm(html: string): Control[] {
  const form = /<form action="\/admin\/settings\/coordinator"[\s\S]*?<\/form>/.exec(html);
  if (!form) throw new Error('the settings form is not on the page');
  return [...form[0].matchAll(/<input\b[^>]*>/g)].map((m) => ({
    type: attribute(m[0], 'type') ?? 'text',
    name: attribute(m[0], 'name') ?? '',
    value: attribute(m[0], 'value') ?? '',
    checked: hasFlag(m[0], 'checked'),
    disabled: hasFlag(m[0], 'disabled'),
  }));
}

function checkbox(controls: Control[], name: string): Control {
  const found = controls.find((c) => c.type === 'checkbox' && c.name === name);
  if (!found) throw new Error(`no checkbox named ${name}`);
  return found;
}

/**
 * What a browser submits for these controls: a disabled control is left out,
 * a checkbox is sent only when checked, everything else is sent as it stands.
 */
function browserSubmission(controls: Control[]): Record<string, string> {
  const body: Record<string, string> = {};
  for (const control of controls) {
    if (!control.name || control.disabled) continue;
    if (control.type === 'checkbox' && !control.checked) continue;
    body[control.name] = control.value;
  }
  return body;
}

type Agent = Awaited<ReturnType<typeof makeAdminAgent>>;

async function openSettings(agent: Agent): Promise<Control[]> {
  const res = await agent.get('/admin/settings');
  expect(res.status).toBe(200);
  return settingsForm(res.text);
}

/** Open the page, apply `change` to its controls the way a person would, and save. */
async function saveSettings(agent: Agent, change: (controls: Control[]) => void = () => undefined) {
  const controls = await openSettings(agent);
  change(controls);
  railway.writes.length = 0;
  return agent.post('/admin/settings/coordinator').type('form').send(browserSubmission(controls));
}

function setText(controls: Control[], name: string, value: string): void {
  const found = controls.find((c) => c.name === name && c.type !== 'checkbox' && c.type !== 'hidden');
  if (!found) throw new Error(`no field named ${name}`);
  found.value = value;
}

const SWITCHES = [
  'COORDINATOR_ENABLED', 'COORDINATOR_DRY_RUN',
  'AGENT_INFRA_ENABLED', 'AGENT_BACKEND_ENABLED', 'AGENT_FINANCIAL_ENABLED',
  'AGENT_CUSTOMER_ENABLED', 'AGENT_DOCS_ENABLED', 'AGENT_SECURITY_ENABLED',
  'AGENT_QA_ENABLED', 'AGENT_ARCHITECT_ENABLED',
];

/** The state the worker reads for a switch on this page from its stored text. */
function workerReads(name: string, raw: string | undefined): boolean {
  if (name === 'COORDINATOR_DRY_RUN') return safetySwitch(raw, true);
  if (name === 'COORDINATOR_ENABLED') return armingSwitch(raw, false);
  return armingSwitch(raw, true);
}

function effectiveStates(vars: Record<string, string>): Record<string, boolean> {
  return Object.fromEntries(SWITCHES.map((name) => [name, workerReads(name, vars[name])]));
}

describe('Admin Settings', () => {
  const origEnv = process.env.ADMIN_EMAIL;
  const origToken = process.env.RAILWAY_ADMIN_TOKEN;

  beforeEach(() => {
    mockQuery.mockReset();
    mockFetch.mockReset();
    mockFetch.mockImplementation(fakeRailway);
    railway.vars = {};
    railway.writes = [];
    railway.refuses = null;
    railway.unreachable = false;
    railwayWrites.enabled = true;
    process.env.ADMIN_EMAIL = 'founder@example.com';
    process.env.RAILWAY_ADMIN_TOKEN = 'test-token';
    vi.stubEnv('RAILWAY_PROJECT_ID', '00000000-0000-4000-8000-0000000000a1');
    vi.stubEnv('RAILWAY_ENVIRONMENT_ID', '00000000-0000-4000-8000-0000000000a2');
    vi.stubEnv('RAILWAY_WORKER_SERVICE_ID', '00000000-0000-4000-8000-0000000000a3');
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    // Assigning undefined would store the string "undefined"; restore by deleting.
    if (origEnv === undefined) delete process.env.ADMIN_EMAIL;
    else process.env.ADMIN_EMAIL = origEnv;
    if (origToken === undefined) delete process.env.RAILWAY_ADMIN_TOKEN;
    else process.env.RAILWAY_ADMIN_TOKEN = origToken;
  });

  describe('GET /admin/settings', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/admin/settings');
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/login/);
    });

    it('returns 403 for non-admin', async () => {
      const agent = supertest.agent(app);
      const hashed = await bcrypt.hash('password123', 1);
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'user-id', email: 'user@example.com', name: 'User', password: hashed, email_verified: true }],
      });
      const csrf = getValidCsrf();
      await agent.post('/login').type('form').send({ _csrf: csrf, email: 'user@example.com', password: 'password123' });
      const res = await agent.get('/admin/settings');
      expect(res.status).toBe(403);
    });

    it('renders settings page for admin', async () => {
      railway.vars = { COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'false', COORDINATOR_DAILY_BUDGET: '100' };
      const agent = await makeAdminAgent();
      const res = await agent.get('/admin/settings');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Coordinator Settings');
      expect(res.text).toContain('Dry Run');
      expect(res.text).toContain('Daily Budget');
    });

    it('gives the page only its own variables: the worker secrets Railway returns with them stay in the fetch', async () => {
      // Values are built at runtime; the names are what matters here.
      const secret = ['worker', 'secret', 'value'].join('-');
      railway.vars = {
        COORDINATOR_ENABLED: 'true',
        COORDINATOR_DRY_RUN: 'false',
        COORDINATOR_DAILY_BUDGET: '100',
        ANTHROPIC_API_KEY: secret,
        RAILWAY_API_TOKEN: secret,
        RAILWAY_ADMIN_TOKEN: secret,
        DATABASE_URL: secret,
        RESEND_API_KEY: secret,
        ADMIN_EMAIL: secret,
      };
      const agent = await makeAdminAgent();
      const res = await agent.get('/admin/settings');
      expect(res.status).toBe(200);
      expect(res.text).not.toContain(secret);

      const handed = vi.mocked(describeCoordinatorSettings).mock.calls.at(-1)?.[0];
      expect(handed).toEqual({ COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'false', COORDINATOR_DAILY_BUDGET: '100' });
      expect(JSON.stringify(handed)).not.toContain(secret);
    });

    it('shows the defaults the worker uses when nothing is set: dry run on, coordinator off, every agent on', async () => {
      railway.vars = {};
      const agent = await makeAdminAgent();
      const controls = await openSettings(agent);

      expect(checkbox(controls, 'COORDINATOR_DRY_RUN').checked, 'dry run').toBe(true);
      expect(checkbox(controls, 'COORDINATOR_ENABLED').checked, 'coordinator').toBe(false);
      for (const name of SWITCHES.filter((n) => n.startsWith('AGENT_'))) {
        expect(checkbox(controls, name).checked, name).toBe(true);
      }
    });

    // Literal expectations, one row per stored text: [text, dry run, coordinator, an agent].
    // `undefined` is "not set". These are the worker's rules (coordinator/envflags.py):
    // dry run falls ON for anything but an OFF word; the other two fall OFF for
    // anything but an ON word, and an agent that is not set at all runs.
    const STORED: Array<[string | undefined, boolean, boolean, boolean]> = [
      [undefined, true, false, true],
      ['true', true, true, true],
      ['false', false, false, false],
      ['1', true, true, true],
      ['yes', true, true, true],
      ['on', true, true, true],
      ['TRUE ', true, true, true],
      ['enabled', true, true, true],
      ['0', false, false, false],
      ['no', false, false, false],
      [' Off', false, false, false],
      ['disabled', false, false, false],
      ['', true, false, false],
      ['garbage', true, false, false],
      ['ture', true, false, false],
    ];

    it.each(STORED)('shows a stored %j as the worker reads it', async (raw, dryRun, coordinator, agentOn) => {
      const names = ['COORDINATOR_DRY_RUN', 'COORDINATOR_ENABLED', 'AGENT_QA_ENABLED'];
      railway.vars = raw === undefined ? {} : Object.fromEntries(names.map((n) => [n, raw]));
      const agent = await makeAdminAgent();
      const controls = await openSettings(agent);

      expect(checkbox(controls, 'COORDINATOR_DRY_RUN').checked, 'dry run').toBe(dryRun);
      expect(checkbox(controls, 'COORDINATOR_ENABLED').checked, 'coordinator').toBe(coordinator);
      expect(checkbox(controls, 'AGENT_QA_ENABLED').checked, 'agent').toBe(agentOn);
      // And the table itself is what the shared parser says.
      expect([dryRun, coordinator, agentOn]).toEqual(names.map((n) => workerReads(n, raw)));
    });

    it('says what is stored when the text is not a plain true or false', async () => {
      railway.vars = { COORDINATOR_DRY_RUN: 'ture', COORDINATOR_ENABLED: '<b>yes</b>' };
      const agent = await makeAdminAgent();
      const res = await agent.get('/admin/settings');

      expect(res.text).toContain('Stored as &#34;ture&#34;, which the worker reads as on.');
      // Stored text is printed escaped.
      expect(res.text).not.toContain('<b>yes</b>');
      expect(res.text).toContain('&lt;b&gt;yes&lt;/b&gt;');
    });

    it('offers nothing to save when the worker variables cannot be read', async () => {
      railway.vars = null;
      const agent = await makeAdminAgent();
      const res = await agent.get('/admin/settings');

      expect(res.status).toBe(200);
      expect(res.text).toContain('could not be read');
      const controls = settingsForm(res.text);
      // No switch is drawn in a state the page does not know, and a save of
      // this page carries no control at all.
      expect(Object.keys(browserSubmission(controls))).toEqual(['_csrf']);
    });

    it('handles missing RAILWAY_ADMIN_TOKEN gracefully', async () => {
      delete process.env.RAILWAY_ADMIN_TOKEN;

      const agent = await makeAdminAgent();
      const res = await agent.get('/admin/settings');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Coordinator Settings');
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('still lets the settings page read the current values when writes are disabled', async () => {
      railwayWrites.enabled = false;
      railway.vars = { COORDINATOR_ENABLED: 'true' };

      const agent = await makeAdminAgent();
      const res = await agent.get('/admin/settings');

      expect(res.status).toBe(200);
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(String((mockFetch.mock.calls[0][1] as RequestInit).body)).toContain('query { variables(');
      expect(checkbox(settingsForm(res.text), 'COORDINATOR_ENABLED').checked).toBe(true);
    });
  });

  describe('POST /admin/settings/coordinator', () => {
    it('requires CSRF token', async () => {
      const agent = await makeAdminAgent();
      const res = await agent.post('/admin/settings/coordinator').type('form').send({});
      expect(res.status).toBe(403);
    });

    // The page as rendered, saved without touching anything, for several
    // states of the worker's variables.
    const UNTOUCHED: Array<[string, Record<string, string>]> = [
      ['nothing set', {}],
      ['canonical values, coordinator live', { COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'false', AGENT_QA_ENABLED: 'false' }],
      ['other spellings', { COORDINATOR_ENABLED: 'yes', COORDINATOR_DRY_RUN: '1', AGENT_INFRA_ENABLED: '0', AGENT_DOCS_ENABLED: 'off' }],
      ['typos and blanks', { COORDINATOR_ENABLED: 'ture', COORDINATOR_DRY_RUN: 'flase', AGENT_SECURITY_ENABLED: '', AGENT_BACKEND_ENABLED: 'TRUE ' }],
      ['numbers set', { COORDINATOR_DAILY_BUDGET: '12.5', COORDINATOR_INTERVAL_S: '600' }],
      ['numbers this page would not write', { COORDINATOR_DAILY_BUDGET: '100000', COORDINATOR_INTERVAL_S: 'soon' }],
    ];

    it.each(UNTOUCHED)('saving the page untouched changes no switch and writes nothing (%s)', async (_label, vars) => {
      railway.vars = { ...vars };
      const before = effectiveStates(vars);
      const agent = await makeAdminAgent();

      const res = await saveSettings(agent);

      expect(res.status).toBe(302);
      // What the worker would read after the save is what it read before it.
      expect(effectiveStates(railway.vars)).toEqual(before);
      expect(railway.writes).toEqual([]);
      expect(railway.vars).toEqual(vars);
    });

    it('writes nothing for a form that carries no controls', async () => {
      const agent = await makeAdminAgent();
      const res = await agent.post('/admin/settings/coordinator').type('form').send({ _csrf: getValidCsrf() });

      expect(res.status).toBe(302);
      expect(railway.writes).toEqual([]);
    });

    it('does not take a value without its marker as a control', async () => {
      const agent = await makeAdminAgent();
      await agent.post('/admin/settings/coordinator').type('form').send({
        _csrf: getValidCsrf(),
        COORDINATOR_ENABLED: 'true',
        COORDINATOR_DRY_RUN: 'false',
        AGENT_QA_ENABLED: 'false',
        COORDINATOR_DAILY_BUDGET: '400',
        confirm_live: 'go live',
      });

      expect(railway.writes).toEqual([]);
    });

    it('refuses to turn dry run off without the typed confirmation', async () => {
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_DRY_RUN').checked = false;
      });

      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/admin/settings?error=');
      expect(decodeURIComponent(res.headers.location)).toContain('go live');
      expect(railway.writes).toEqual([]);
    });

    it('refuses to enable the coordinator without the typed confirmation', async () => {
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_ENABLED').checked = true;
      });

      expect(res.headers.location).toContain('/admin/settings?error=');
      expect(railway.writes).toEqual([]);
    });

    it.each(['', 'yes', 'true', 'GO LIVE', 'go  live', 'golive'])('does not take %j as the confirmation', async (typed) => {
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_DRY_RUN').checked = false;
        setText(controls, 'confirm_live', typed);
      });

      expect(res.headers.location).toContain('/admin/settings?error=');
      expect(railway.writes).toEqual([]);
    });

    it('writes none of the other changes when the save is refused', async () => {
      const agent = await makeAdminAgent();
      await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_ENABLED').checked = true;
        checkbox(controls, 'AGENT_QA_ENABLED').checked = false;
        setText(controls, 'COORDINATOR_DAILY_BUDGET', '5');
      });

      expect(railway.writes).toEqual([]);
    });

    it('goes live with the confirmation, writing exactly the two changes as true and false', async () => {
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_ENABLED').checked = true;
        checkbox(controls, 'COORDINATOR_DRY_RUN').checked = false;
        setText(controls, 'confirm_live', 'go live');
      });

      expect(res.headers.location).toContain('/admin/settings?success=');
      expect([...railway.writes].sort((a, b) => a.name.localeCompare(b.name))).toEqual([
        { name: 'COORDINATOR_DRY_RUN', value: 'false' },
        { name: 'COORDINATOR_ENABLED', value: 'true' },
      ]);

      // The page now shows the state it produced, and saving it again writes nothing.
      const controls = await openSettings(agent);
      expect(checkbox(controls, 'COORDINATOR_ENABLED').checked).toBe(true);
      expect(checkbox(controls, 'COORDINATOR_DRY_RUN').checked).toBe(false);
      await saveSettings(agent);
      expect(railway.writes).toEqual([]);
    });

    it('needs no confirmation to go back to the safe state', async () => {
      railway.vars = { COORDINATOR_ENABLED: 'true', COORDINATOR_DRY_RUN: 'false' };
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_ENABLED').checked = false;
        checkbox(controls, 'COORDINATOR_DRY_RUN').checked = true;
      });

      expect(res.headers.location).toContain('/admin/settings?success=');
      expect(railway.vars).toMatchObject({ COORDINATOR_ENABLED: 'false', COORDINATOR_DRY_RUN: 'true' });
      expect(railway.writes).toHaveLength(2);
    });

    it('writes only the control that changed', async () => {
      railway.vars = { AGENT_DOCS_ENABLED: 'no' };
      const agent = await makeAdminAgent();
      await saveSettings(agent, (controls) => {
        checkbox(controls, 'AGENT_QA_ENABLED').checked = false;
      });

      expect(railway.writes).toEqual([{ name: 'AGENT_QA_ENABLED', value: 'false' }]);
      // The agent that was off under another spelling was not rewritten or enabled.
      expect(railway.vars.AGENT_DOCS_ENABLED).toBe('no');
    });

    it('sends changes toward the safe state first and stops at the first one Railway refuses', async () => {
      railway.vars = { COORDINATOR_DRY_RUN: 'false', AGENT_QA_ENABLED: 'false' };
      railway.refuses = 'COORDINATOR_DRY_RUN';
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        checkbox(controls, 'COORDINATOR_ENABLED').checked = true;
        checkbox(controls, 'AGENT_QA_ENABLED').checked = true;
        checkbox(controls, 'COORDINATOR_DRY_RUN').checked = true;
        setText(controls, 'confirm_live', 'go live');
      });

      // Dry run on was attempted first and failed, so nothing that arms
      // anything was sent after it.
      expect(railway.writes).toEqual([{ name: 'COORDINATOR_DRY_RUN', value: 'true' }]);
      expect(res.headers.location).toContain('/admin/settings?error=');
      expect(decodeURIComponent(res.headers.location)).toContain('COORDINATOR_DRY_RUN');
    });

    it('never sends Railway a name that is not one of the page settings', async () => {
      const agent = await makeAdminAgent();
      await agent.post('/admin/settings/coordinator').type('form').send({
        _csrf: getValidCsrf(),
        shown_DATABASE_URL: 'off',
        DATABASE_URL: 'true',
        shown_OPS_DRY_RUN: 'on',
        shown_COORDINATOR_ANALYSIS_ONLY: 'on',
        'shown_AGENT_QA_ENABLED", value: "x': 'on',
      });

      expect(railway.writes).toEqual([]);
    });

    it('refuses non-finite and absurd numbers (Infinity would switch the daily spend cap off)', async () => {
      const agent = await makeAdminAgent();
      for (const bad of ['Infinity', '1e999', '-5', 'abc', '99999999', '1e2', '0x10']) {
        const res = await saveSettings(agent, (controls) => {
          setText(controls, 'COORDINATOR_DAILY_BUDGET', bad);
          setText(controls, 'COORDINATOR_INTERVAL_S', bad);
        });
        expect(res.headers.location, bad).toContain('/admin/settings?error=');
        expect(railway.writes, bad).toEqual([]);
      }
    });

    it('refuses an interval the worker could not read as whole seconds, or outside the range the form states', async () => {
      const agent = await makeAdminAgent();
      for (const bad of ['1800.5', '0', '299', '7201']) {
        const res = await saveSettings(agent, (controls) => {
          setText(controls, 'COORDINATOR_INTERVAL_S', bad);
        });
        expect(res.headers.location, bad).toContain('/admin/settings?error=');
        expect(railway.writes, bad).toEqual([]);
      }
    });

    it('accepts an ordinary budget and gives every Railway call a timeout signal', async () => {
      const agent = await makeAdminAgent();
      const res = await saveSettings(agent, (controls) => {
        setText(controls, 'COORDINATOR_DAILY_BUDGET', '12.5');
      });

      expect(res.headers.location).toContain('/admin/settings?success=');
      expect(railway.writes).toEqual([{ name: 'COORDINATOR_DAILY_BUDGET', value: '12.5' }]);
      for (const call of mockFetch.mock.calls) {
        expect((call[1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
      }
    });

    it('writes nothing to Railway when Railway writes are disabled in this environment', async () => {
      const agent = await makeAdminAgent();
      const controls = await openSettings(agent);
      checkbox(controls, 'AGENT_QA_ENABLED').checked = false;
      setText(controls, 'COORDINATOR_DAILY_BUDGET', '500');
      railwayWrites.enabled = false;
      mockFetch.mockClear();

      const res = await agent.post('/admin/settings/coordinator').type('form').send(browserSubmission(controls));

      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/admin/settings?error=');
      expect(decodeURIComponent(res.headers.location)).toContain('Railway writes are disabled');
      // Not one call left the process.
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('reports failures when Railway API is unreachable', async () => {
      const agent = await makeAdminAgent();
      const controls = await openSettings(agent);
      checkbox(controls, 'AGENT_QA_ENABLED').checked = false;
      railway.unreachable = true;

      const res = await agent.post('/admin/settings/coordinator').type('form').send(browserSubmission(controls));

      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/admin/settings?error=');
    });

    it('reports failure without calling Railway when RAILWAY_WORKER_SERVICE_ID is unset', async () => {
      const agent = await makeAdminAgent();
      const controls = await openSettings(agent);
      checkbox(controls, 'AGENT_QA_ENABLED').checked = false;
      vi.stubEnv('RAILWAY_WORKER_SERVICE_ID', '');
      mockFetch.mockClear();

      const res = await agent.post('/admin/settings/coordinator').type('form').send(browserSubmission(controls));

      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/admin/settings?error=');
      expect(mockFetch).not.toHaveBeenCalled();
    });
  });
});
