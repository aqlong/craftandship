// POST /lighthouse/table/turn: a crisis message gets the fixed script whatever
// else the route would have answered.
//
// Each test first shows what an ORDINARY message gets in the situation (the
// email gate's 402, a limiter's 429, the cost cap's 429, a 500 from a dead
// database), then sends a crisis message into the same situation. The router
// is the real one, mounted the way app.ts mounts it; the rate limiter is the
// real express-rate-limit instance with a small budget; turn() and the crisis
// scan are real. Only the database pool and the email sender are fakes.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import express, { type NextFunction, type Request, type Response } from 'express';
import request from 'supertest';
import type { Pool } from 'pg';

const sendCrisisAlertEmail = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock('../../src/services/email', () => ({ sendCrisisAlertEmail }));

// Funnel analytics write to the pool on their own schedule; keep them out of the statement log.
vi.mock('../../src/services/funnel-events', () => ({ track: vi.fn() }));

// The per-address limiter with a budget of 3 requests, so a test can exhaust
// it. Everything else in the module (key generators, the factory) is real.
const TURN_BUDGET = vi.hoisted(() => 3);
vi.mock('../../src/middleware/companion-rate-limit', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/middleware/companion-rate-limit')>();
  return {
    ...actual,
    companionTurnIpRateLimit: actual.makeCompanionTurnRateLimit({
      max: TURN_BUDGET,
      keyGenerator: actual.companionIpKeyGen,
    }),
  };
});

import { createLighthouseRouter, crisisFirst, CRISIS_SCAN_MAX_CHARS } from '../../src/routes/lighthouse';
import { crisisSideEffects } from '../../src/companion/runtime';

const TRIAL_ID = '11111111-2222-3333-4444-555555555555';
const TOKEN = 'a'.repeat(64);
const COOKIE = `cas_harbor_trial=${TOKEN}`;
const CRISIS_TEXT = 'I want to kill myself';
const ORDINARY_TEXT = 'Tell me what a website for my practice could look like';

interface Statement {
  sql: string;
  params: unknown[];
}
type Answer = { rows: unknown[]; rowCount?: number } | Error;

/** A pool whose answers are chosen by matching the SQL text. */
function makeFakePool(rules: Array<[RegExp, Answer | ((params: unknown[]) => Answer)]>) {
  const statements: Statement[] = [];
  const query = vi.fn((sql: unknown, params?: unknown) => {
    const text = String(sql);
    const values = Array.isArray(params) ? params : [];
    statements.push({ sql: text, params: values });
    const rule = rules.find(([pattern]) => pattern.test(text));
    const answer = rule ? (typeof rule[1] === 'function' ? rule[1](values) : rule[1]) : { rows: [], rowCount: 0 };
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  });
  return { pool: { query } as unknown as Pool, statements, query };
}

const ACTIVE_TRIAL = {
  trial_id: TRIAL_ID,
  cookie_token: TOKEN,
  companion_name: 'Haven',
  companion_assigned: 'Haven',
  started_at: new Date('2026-04-26T10:00:00Z'),
  expires_at: new Date('2099-01-01T00:00:00Z'),
  last_seen_at: new Date('2026-04-26T11:00:00Z'),
  released_at: null,
  handoff_build_id: null,
};

const TRIAL_BY_COOKIE: [RegExp, Answer] = [/FROM trial_keepers\s+WHERE cookie_token = \$1/, { rows: [ACTIVE_TRIAL] }];
const STORE_TURN: [RegExp, Answer] = [/INSERT INTO companion_turns/, { rows: [{ turn_id: 'turn-crisis' }] }];

/** The router mounted as app.ts mounts it: JSON body parser, then /lighthouse. */
function makeApp(pool: Pool, clientIp: string) {
  const app = express();
  app.use((req: Request, _res: Response, next: NextFunction) => {
    // One address per test, so each test starts with a fresh limiter budget.
    Object.defineProperty(req, 'ip', { value: clientIp, configurable: true });
    next();
  });
  app.use(express.json({ limit: '1mb' }));
  app.use('/lighthouse', createLighthouseRouter(pool));
  app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
    res.status(500).json({ error: 'internal', detail: err.message });
  });
  return app;
}

function post(app: express.Express, keeperText: unknown, cookie: string | null = COOKIE) {
  const req = request(app).post('/lighthouse/table/turn');
  if (cookie) req.set('Cookie', cookie);
  return req.send({ keeperText });
}

function expectCrisisScript(res: request.Response): void {
  expect(res.status).toBe(200);
  expect(res.body.companionText).toMatch(/988/);
  expect(res.body.companionText).toMatch(/I'm an AI/);
  expect(res.body.railFlags).toEqual(['crisis', 'suicide']);
  expect(res.body).not.toHaveProperty('requiresEmail');
  expect(res.body).not.toHaveProperty('error');
}

let ipCounter = 0;
function freshIp(): string {
  ipCounter += 1;
  return `198.51.100.${ipCounter}`;
}

const savedEnv = { ...process.env };

beforeEach(() => {
  crisisSideEffects.reset();
  sendCrisisAlertEmail.mockClear();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  for (const key of ['CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS', 'ANTHROPIC_API_KEY']) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

describe('crisis + email gate', () => {
  const atTheGate: Array<[RegExp, Answer]> = [
    TRIAL_BY_COOKIE,
    [/SELECT COUNT\(\*\)::text AS c FROM companion_turns WHERE trial_id = \$1/, { rows: [{ c: '22' }] }],
    [/SELECT email_captured FROM trial_keepers WHERE trial_id = \$1/, { rows: [{ email_captured: null }] }],
    STORE_TURN,
  ];

  it('an ordinary 23rd message with no email on file is asked for an email (402)', async () => {
    const { pool } = makeFakePool(atTheGate);
    const res = await post(makeApp(pool, freshIp()), ORDINARY_TEXT);
    expect(res.status).toBe(402);
    expect(res.body.requiresEmail).toBe(true);
  });

  it('a crisis message in the same state gets the script, and the gate is never consulted', async () => {
    const { pool, statements } = makeFakePool(atTheGate);
    const res = await post(makeApp(pool, freshIp()), CRISIS_TEXT);

    expectCrisisScript(res);
    await vi.waitFor(() => expect(statements.some((s) => /INSERT INTO companion_turns/.test(s.sql))).toBe(true));
    expect(statements.some((s) => /COUNT\(\*\)::text AS c FROM companion_turns/.test(s.sql))).toBe(false);
    expect(statements.some((s) => /SELECT email_captured/.test(s.sql))).toBe(false);
  });

  it('the turn is stored for review under the sender\'s trial, flagged, and the operator is alerted once', async () => {
    const { pool, statements } = makeFakePool(atTheGate);
    await post(makeApp(pool, freshIp()), CRISIS_TEXT);

    await vi.waitFor(() => expect(sendCrisisAlertEmail).toHaveBeenCalledTimes(1));
    const insert = statements.find((s) => /INSERT INTO companion_turns/.test(s.sql));
    expect(insert!.params[0]).toBeNull(); // keeper_id
    expect(insert!.params[1]).toBe(TRIAL_ID);
    expect(insert!.params[2]).toBe(CRISIS_TEXT);
    expect(insert!.params[4]).toEqual(['crisis', 'suicide']);
    expect(sendCrisisAlertEmail.mock.calls[0][0]).toMatchObject({ trialId: TRIAL_ID, categories: ['suicide'] });
  });
});

describe('crisis + exhausted rate limiter', () => {
  it('once the per-address budget is spent an ordinary message gets 429 and a crisis message still gets the script', async () => {
    process.env.ANTHROPIC_API_KEY = '';
    const { pool } = makeFakePool([
      TRIAL_BY_COOKIE,
      [/COUNT\(\*\)::text AS c FROM companion_turns/, { rows: [{ c: '0' }] }],
      STORE_TURN,
    ]);
    const app = makeApp(pool, freshIp());

    const statuses: number[] = [];
    for (let i = 0; i < TURN_BUDGET + 1; i += 1) {
      statuses.push((await post(app, ORDINARY_TEXT)).status);
    }
    // The last ordinary message is refused by the limiter.
    expect(statuses[statuses.length - 1]).toBe(429);
    expect(statuses.slice(0, TURN_BUDGET)).not.toContain(429);

    const refused = await post(app, ORDINARY_TEXT);
    expect(refused.status).toBe(429);
    expect(refused.body.error).toBe('rate_limited');

    const crisis = await post(app, CRISIS_TEXT);
    expectCrisisScript(crisis);
  });

  it('crisis messages do not spend the limiter budget of ordinary ones', async () => {
    const { pool } = makeFakePool([
      TRIAL_BY_COOKIE,
      [/COUNT\(\*\)::text AS c FROM companion_turns/, { rows: [{ c: '0' }] }],
      STORE_TURN,
    ]);
    const app = makeApp(pool, freshIp());
    for (let i = 0; i < TURN_BUDGET * 2; i += 1) {
      expectCrisisScript(await post(app, CRISIS_TEXT));
    }
    // The ordinary budget is still whole.
    const ordinary = await post(app, ORDINARY_TEXT);
    expect(ordinary.status).not.toBe(429);
  });
});

describe('crisis + failing database', () => {
  it('with every query failing, an ordinary message is a 500 and a crisis message still gets the script', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { pool } = makeFakePool([[/./, new Error('connection refused')]]);
    const app = makeApp(pool, freshIp());

    const ordinary = await post(app, ORDINARY_TEXT);
    expect(ordinary.status).toBe(500);

    const crisis = await post(app, CRISIS_TEXT);
    expectCrisisScript(crisis);
    expect(crisis.body.turnId).toBeNull();
    // The operator still hears about it: logged, and alerted without an identity.
    await vi.waitFor(() => expect(sendCrisisAlertEmail).toHaveBeenCalledTimes(1));
    expect(errSpy.mock.calls.some(([line]) => /could not identify the sender/.test(String(line)))).toBe(true);
  });

  it('the script does not wait for the database: it is answered while the lookup is still hanging', async () => {
    let releaseLookup: (() => void) | undefined;
    const hanging = new Promise<{ rows: unknown[] }>((resolve) => {
      releaseLookup = () => resolve({ rows: [ACTIVE_TRIAL] });
    });
    const query = vi.fn(() => hanging);
    const app = makeApp({ query } as unknown as Pool, freshIp());

    const crisis = await post(app, CRISIS_TEXT);
    expectCrisisScript(crisis);
    // The response arrived although no query has resolved yet.
    expect(query).toHaveBeenCalledTimes(1);
    releaseLookup?.();
  });
});

describe('crisis + cost caps', () => {
  it('an identity that has spent its daily budget gets 429 for an ordinary message and the script for a crisis one', async () => {
    process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS = '1000';
    const rules: Array<[RegExp, Answer]> = [
      TRIAL_BY_COOKIE,
      [/COUNT\(\*\)::text AS c FROM companion_turns/, { rows: [{ c: '0' }] }],
      [/SELECT companion_name, started_at FROM trial_keepers/, { rows: [{ companion_name: 'Haven', started_at: new Date() }] }],
      [/FROM companion_cost_log ccl/, { rows: [{ total: '5000', global_total: '5000' }] }],
      STORE_TURN,
    ];
    const { pool } = makeFakePool(rules);
    const app = makeApp(pool, freshIp());

    const ordinary = await post(app, ORDINARY_TEXT);
    expect(ordinary.status).toBe(429);
    expect(ordinary.body.error).toBe('daily_cost_cap_exceeded');

    expectCrisisScript(await post(app, CRISIS_TEXT));
  });
});

describe('crisis first: what it does not do', () => {
  it('a sender with no cookie gets the script; no trial is minted and nothing is stored', async () => {
    const { pool, statements } = makeFakePool([STORE_TURN]);
    const res = await post(makeApp(pool, freshIp()), CRISIS_TEXT, null);

    expectCrisisScript(res);
    expect(res.headers['set-cookie']).toBeUndefined();
    await vi.waitFor(() => expect(sendCrisisAlertEmail).toHaveBeenCalledTimes(1));
    expect(statements.some((s) => /INSERT INTO trial_keepers/.test(s.sql))).toBe(false);
    expect(statements.some((s) => /INSERT INTO companion_turns/.test(s.sql))).toBe(false);
  });

  it('a message over the 4000 character limit that contains a crisis phrase gets the script, not 413; the stored copy is cut to 4000', async () => {
    const { pool, statements } = makeFakePool([TRIAL_BY_COOKIE, STORE_TURN]);
    const app = makeApp(pool, freshIp());
    const long = `${'a'.repeat(4100)} ${CRISIS_TEXT}`;

    expect((await post(app, 'a'.repeat(4100))).status).toBe(413);
    expectCrisisScript(await post(app, long));
    await vi.waitFor(() => expect(statements.some((s) => /INSERT INTO companion_turns/.test(s.sql))).toBe(true));
    const insert = statements.find((s) => /INSERT INTO companion_turns/.test(s.sql));
    expect(String(insert!.params[2])).toHaveLength(4000);
  });

  it('the scan reads the first CRISIS_SCAN_MAX_CHARS characters of a message and no further', async () => {
    expect(CRISIS_SCAN_MAX_CHARS).toBe(20_000);
    const { pool } = makeFakePool([TRIAL_BY_COOKIE]);
    const app = makeApp(pool, freshIp());
    const beyond = `${'a'.repeat(CRISIS_SCAN_MAX_CHARS)} ${CRISIS_TEXT}`;
    expect((await post(app, beyond)).status).toBe(413);
  });

  it('a flood of crisis messages from one cookie: every one gets the script, one alert email is sent, stored turns are capped', async () => {
    const { pool, statements } = makeFakePool([TRIAL_BY_COOKIE, STORE_TURN]);
    const app = makeApp(pool, freshIp());
    for (let i = 0; i < 30; i += 1) {
      expectCrisisScript(await post(app, CRISIS_TEXT));
    }
    await vi.waitFor(() => expect(statements.filter((s) => /INSERT INTO companion_turns/.test(s.sql))).toHaveLength(20));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(statements.filter((s) => /INSERT INTO companion_turns/.test(s.sql))).toHaveLength(20);
    expect(sendCrisisAlertEmail).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['an ordinary message', ORDINARY_TEXT],
    ['a screening sentence that only mentions the topic', 'I refer people who are suicidal to a licensed therapist'],
  ])('%s is passed on untouched', async (_label, text) => {
    const next = vi.fn();
    const json = vi.fn();
    const { pool, query } = makeFakePool([]);
    crisisFirst(pool)(
      { method: 'POST', headers: {}, body: { keeperText: text } } as unknown as Request,
      { json } as unknown as Response,
      next
    );
    expect(next).toHaveBeenCalledOnce();
    expect(json).not.toHaveBeenCalled();
    expect(query).not.toHaveBeenCalled();
  });

  it.each([
    ['no body', undefined],
    ['a non-string keeperText', { keeperText: ['I want to kill myself'] }],
    ['an empty keeperText', { keeperText: '   ' }],
  ])('%s is passed on to the ordinary validation', async (_label, body) => {
    const next = vi.fn();
    const json = vi.fn();
    crisisFirst(makeFakePool([]).pool)(
      { method: 'POST', headers: {}, body } as unknown as Request,
      { json } as unknown as Response,
      next
    );
    expect(next).toHaveBeenCalledOnce();
    expect(json).not.toHaveBeenCalled();
  });

  it('a request the browser marks cross-site is passed on (to be refused), never answered or recorded', async () => {
    const next = vi.fn();
    const json = vi.fn();
    const { pool, query } = makeFakePool([]);
    crisisFirst(pool)(
      { method: 'POST', headers: { 'sec-fetch-site': 'cross-site' }, body: { keeperText: CRISIS_TEXT } } as unknown as Request,
      { json } as unknown as Response,
      next
    );
    expect(next).toHaveBeenCalledOnce();
    expect(json).not.toHaveBeenCalled();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(query).not.toHaveBeenCalled();
    expect(sendCrisisAlertEmail).not.toHaveBeenCalled();
  });
});

describe('the browser that paid: a crisis turn is filed under the keeper', () => {
  it('stores the turn with keeper_id when the cookie belongs to a trial released by a payment', async () => {
    const BUILD_ID = 'bbbbbbbb-1111-2222-3333-444444444444';
    const KEEPER_ID = 'cccccccc-1111-2222-3333-444444444444';
    const { pool, statements } = makeFakePool([
      [/FROM trial_keepers\s+WHERE cookie_token = \$1/, {
        rows: [{ ...ACTIVE_TRIAL, released_at: new Date(), handoff_build_id: BUILD_ID }],
      }],
      [/FROM trial_keepers t\s+JOIN builds b ON b\.id = t\.handoff_build_id\s+JOIN keepers k ON k\.customer_id = t\.promoted_customer_id/, { rows: [{ build_id: BUILD_ID, customer_id: KEEPER_ID }] }],
      STORE_TURN,
    ]);
    expectCrisisScript(await post(makeApp(pool, freshIp()), CRISIS_TEXT));
    await vi.waitFor(() => expect(statements.some((s) => /INSERT INTO companion_turns/.test(s.sql))).toBe(true));
    const insert = statements.find((s) => /INSERT INTO companion_turns/.test(s.sql));
    expect(insert!.params[0]).toBe(KEEPER_ID);
    expect(insert!.params[1]).toBeNull();
  });
});
