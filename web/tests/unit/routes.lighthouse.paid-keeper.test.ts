// After payment: the browser that paid lands as the owner of what it paid for.
//
// The Stripe webhook releases the trial. These tests drive the real app (the
// real trial-cookie middleware and the real lamp, table and turn routes, with
// their real views) against a fake pool that answers by SQL text AND by the
// parameters it is given, so an ownership lookup only returns a row when it
// was asked about the right trial and the right build. Three browsers appear:
//   - the payer: holds the cookie of the trial that was released by payment;
//   - a stranger: holds a fresh, active trial of their own, and knows the URL;
//   - another trial visitor: active trial with a build of their own.

import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import request from 'supertest';
import type { Application } from 'express';

vi.mock('../../src/db/index', () => ({
  default: { query: vi.fn(), connect: vi.fn(), on: vi.fn() },
}));

vi.mock('../../src/companion/runtime', async () => {
  const actual = await vi.importActual<typeof import('../../src/companion/runtime')>(
    '../../src/companion/runtime'
  );
  return { ...actual, turn: vi.fn() };
});

vi.mock('../../src/services/stripe', () => ({
  createHarborSubscriptionCheckoutSession: vi.fn(),
  cancelHarborSubscriptionAtPeriodEnd: vi.fn().mockResolvedValue({ id: 'sub_paid', cancel_at_period_end: true }),
  constructWebhookEvent: vi.fn(),
  HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS: 1900,
  HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS: 3900,
  HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS: 3900,
}));

vi.mock('../../src/services/funnel-events', () => ({ track: vi.fn() }));

const PAYER_TOKEN = 'a'.repeat(64);
const STRANGER_TOKEN = 'b'.repeat(64);
const OTHER_TOKEN = 'c'.repeat(64);
const PAYER_TRIAL = '11111111-1111-4111-8111-111111111111';
const STRANGER_TRIAL = '22222222-2222-4222-8222-222222222222';
const OTHER_TRIAL = '33333333-3333-4333-8333-333333333333';
const PAID_BUILD = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const OTHER_BUILD = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const SECOND_BUILD_OF_KEEPER = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const KEEPER = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const OTHER_CUSTOMER = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const PAID_SUB = ['sub', 'paid', 'one'].join('_');

interface TrialRow {
  trial_id: string;
  cookie_token: string;
  released_at: Date | null;
  handoff_build_id: string | null;
  /** The account a payment promoted this trial into (trial_keepers.promoted_customer_id). */
  promoted_customer_id: string | null;
}
interface BuildRow {
  id: string;
  customer_id: string | null;
  app_name: string;
  status: string;
  review_status: string | null;
  review_notes: null;
  preview_url: string;
  live_url: string | null;
}

/** The little world the fake pool answers from. Tests may change it. */
let world: {
  trials: TrialRow[];
  builds: BuildRow[];
  keepers: Array<{ customer_id: string; stripe_subscription_id: string | null; subscription_status: string; handoff_build_id: string | null }>;
  /** What harbor_subscriptions.cancel_at_period_end holds; false unless a test sets it. */
  cancelAtPeriodEnd?: boolean;
};
let statements: Array<{ sql: string; params: unknown[] }>;

function freshWorld(): typeof world {
  return {
    trials: [
      { trial_id: PAYER_TRIAL, cookie_token: PAYER_TOKEN, released_at: new Date('2026-05-02T10:00:00Z'), handoff_build_id: PAID_BUILD, promoted_customer_id: KEEPER },
      { trial_id: STRANGER_TRIAL, cookie_token: STRANGER_TOKEN, released_at: null, handoff_build_id: null, promoted_customer_id: null },
      { trial_id: OTHER_TRIAL, cookie_token: OTHER_TOKEN, released_at: null, handoff_build_id: OTHER_BUILD, promoted_customer_id: null },
    ],
    builds: [
      { id: PAID_BUILD, customer_id: KEEPER, app_name: 'Lantern Studio', status: 'live', review_status: 'approved', review_notes: null, preview_url: 'https://preview.example.test', live_url: 'https://preview.example.test' },
      { id: OTHER_BUILD, customer_id: OTHER_CUSTOMER, app_name: 'Other Site', status: 'preview', review_status: 'approved', review_notes: null, preview_url: 'https://other.example.test', live_url: null },
      { id: SECOND_BUILD_OF_KEEPER, customer_id: KEEPER, app_name: 'Second Site', status: 'live', review_status: 'approved', review_notes: null, preview_url: 'https://second.example.test', live_url: 'https://second.example.test' },
    ],
    keepers: [
      { customer_id: KEEPER, stripe_subscription_id: PAID_SUB, subscription_status: 'active', handoff_build_id: PAID_BUILD },
    ],
  };
}

function trialContext(trial: TrialRow) {
  return {
    ...trial,
    companion_name: 'Haven',
    companion_assigned: 'Haven',
    started_at: new Date('2026-04-28T10:00:00Z'),
    expires_at: new Date(Date.now() + 25 * 24 * 60 * 60 * 1000),
    last_seen_at: new Date('2026-05-02T09:00:00Z'),
  };
}

function answer(sql: string, params: unknown[]): { rows: unknown[]; rowCount?: number } {
  if (/FROM trial_keepers\s+WHERE cookie_token = \$1\s+AND expires_at > NOW\(\)/.test(sql)) {
    const trial = world.trials.find((t) => t.cookie_token === params[0]);
    return { rows: trial ? [trialContext(trial)] : [] };
  }
  if (/FROM trial_keepers t\s+JOIN builds b ON b\.id = t\.handoff_build_id\s+JOIN keepers k ON k\.customer_id = t\.promoted_customer_id\s+AND k\.customer_id = b\.customer_id\s+WHERE t\.trial_id = \$1 AND t\.released_at IS NOT NULL/.test(sql)) {
    // Answers only for what the statement asks: the trial was released, was
    // promoted into the account of its own build, and that account is a keeper.
    const trial = world.trials.find((t) => t.trial_id === params[0]);
    const build = trial && world.builds.find((b) => b.id === trial.handoff_build_id);
    const keeper = build && trial && world.keepers.find((k) => k.customer_id === trial.promoted_customer_id && k.customer_id === build.customer_id);
    return { rows: trial && trial.released_at && build && keeper ? [{ build_id: build.id, customer_id: keeper.customer_id }] : [] };
  }
  if (/SELECT id, customer_id, app_name, status, review_status, review_notes,\s+preview_url, live_url\s+FROM builds\s+WHERE id = \$1/.test(sql)) {
    return { rows: world.builds.filter((b) => b.id === params[0]) };
  }
  if (/SELECT trial_id FROM trial_keepers\s+WHERE trial_id = \$1 AND handoff_build_id = \$2 AND released_at IS NULL/.test(sql)) {
    return {
      rows: world.trials
        .filter((t) => t.trial_id === params[0] && t.handoff_build_id === params[1] && t.released_at === null)
        .map((t) => ({ trial_id: t.trial_id })),
    };
  }
  if (/SELECT subscription_status, stripe_subscription_id, sliding_scale_amount_cents\s+FROM keepers WHERE customer_id = \$1/.test(sql)) {
    return {
      rows: world.keepers
        .filter((k) => k.customer_id === params[0])
        .map((k) => ({ ...k, sliding_scale_amount_cents: 2900 })),
    };
  }
  if (/SELECT stripe_subscription_id FROM keepers WHERE customer_id = \$1/.test(sql)) {
    return { rows: world.keepers.filter((k) => k.customer_id === params[0]) };
  }
  if (/SELECT cancel_at_period_end, current_period_end\s+FROM harbor_subscriptions/.test(sql)) {
    return { rows: [{ cancel_at_period_end: world.cancelAtPeriodEnd ?? false, current_period_end: null }] };
  }
  if (/SELECT status, live_url FROM builds WHERE id = \$1/.test(sql)) {
    return { rows: world.builds.filter((b) => b.id === params[0]).map((b) => ({ status: b.status, live_url: b.live_url })) };
  }
  if (/FROM companion_turns\s+WHERE keeper_id = \$1/.test(sql)) {
    return {
      rows: params[0] === KEEPER
        ? [{ turn_id: 't1', keeper_text: 'I teach reiki', companion_text: 'Tell me about your practice.', rail_flags: [], created_at: new Date() }]
        : [],
    };
  }
  // What Haven remembers. Promotion moved the payer's notes to the keeper;
  // nothing is left under any trial id.
  if (/FROM keeper_memory\s+WHERE keeper_id = \$1 AND deleted_at IS NULL/.test(sql)) {
    return {
      rows: params[0] === KEEPER
        ? [{ memory_id: 'm1', partition: 'working', category: 'fact', content: 'Teaches reiki in Leeds.', keeper_phrase: null, created_at: new Date(), updated_at: new Date() }]
        : [],
    };
  }
  if (/INSERT INTO trial_keepers/.test(sql)) {
    const minted: TrialRow = { trial_id: '99999999-9999-4999-8999-999999999999', cookie_token: String(params[0]), released_at: null, handoff_build_id: null, promoted_customer_id: null };
    world.trials.push(minted);
    return { rows: [trialContext(minted)] };
  }
  return { rows: [], rowCount: 0 };
}

let app: Application;
let mockQuery: ReturnType<typeof vi.fn>;
let mockTurn: ReturnType<typeof vi.fn>;
let mockCancel: ReturnType<typeof vi.fn>;
let csrf: string;

beforeAll(async () => {
  process.env.CAS_HARBOR_ENABLED = 'true';
  process.env.NODE_ENV = process.env.NODE_ENV || 'test';
  vi.resetModules();
  app = (await import('../../src/app')).default;
  mockQuery = (await import('../../src/db/index')).default.query as unknown as ReturnType<typeof vi.fn>;
  mockTurn = (await import('../../src/companion/runtime')).turn as unknown as ReturnType<typeof vi.fn>;
  mockCancel = (await import('../../src/services/stripe')).cancelHarborSubscriptionAtPeriodEnd as unknown as ReturnType<typeof vi.fn>;
  csrf = (await import('../../src/middleware/auth')).generateCsrfToken({} as never);
});

beforeEach(() => {
  world = freshWorld();
  statements = [];
  mockQuery.mockReset().mockImplementation((sql: unknown, params?: unknown) => {
    const text = String(sql);
    const values = Array.isArray(params) ? params : [];
    statements.push({ sql: text, params: values });
    return Promise.resolve(answer(text, values));
  });
  mockTurn.mockReset().mockResolvedValue({ companionText: 'Of course.', turnId: 'turn-9', railFlags: [], costMicros: 0 });
  mockCancel.mockClear();
});

const as = (token: string) => `cas_harbor_trial=${token}`;

describe('the payer, back from Stripe with only the trial cookie', () => {
  it('lands on the published page as the owner, and keeps the same cookie', async () => {
    const res = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}/published`).set('Cookie', as(PAYER_TOKEN));

    expect(res.status).toBe(200);
    expect(res.text).toContain('Your website is public.');
    expect(res.text).toContain(`href="/lighthouse/lamp/${PAID_BUILD}"`);
    // Not swapped for a fresh trial: the cookie that comes back is the one that paid.
    const setCookie = String(res.headers['set-cookie'] || '');
    expect(setCookie).toContain(`cas_harbor_trial=${PAYER_TOKEN}`);
    expect(statements.some((s) => /INSERT INTO trial_keepers/.test(s.sql))).toBe(false);
  });

  it('before the webhook has landed the same page says "hold on", and the poll it runs keeps answering 200', async () => {
    // Before payment is recorded: the trial is still active and owns the preview.
    world.trials[0].released_at = null;
    world.builds[0].status = 'preview';
    world.builds[0].live_url = null;
    world.keepers = [];
    const before = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}/published`).set('Cookie', as(PAYER_TOKEN));
    expect(before.status).toBe(200);
    expect(before.text).toContain('Hold on');

    // The webhook lands: trial released, keeper created, build live.
    world = freshWorld();
    const after = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}/published`).set('Cookie', as(PAYER_TOKEN));
    expect(after.status).toBe(200);
    expect(after.text).toContain('It is real now');
  });

  it('sees the lamp page with the subscription and a way to cancel it, and how to reach the account from elsewhere', async () => {
    const res = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}`).set('Cookie', as(PAYER_TOKEN));

    expect(res.status).toBe(200);
    expect(res.text).toContain('Your website is public');
    expect(res.text).toContain('$29 a month');
    expect(res.text).toContain(`action="/lighthouse/lamp/${PAID_BUILD}/cancel-subscription"`);
    expect(res.text).toContain('Cancel subscription');
    expect(res.text).toMatch(/href="\/forgot-password">choose a password<\/a>/);
  });

  it('cannot cancel with the cookie alone: a page says to log in (or choose a password first), and Stripe is not asked', async () => {
    const res = await request(app)
      .post(`/lighthouse/lamp/${PAID_BUILD}/cancel-subscription`)
      .set('Cookie', as(PAYER_TOKEN))
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(403);
    expect(res.text).toContain('Log in to cancel the subscription.');
    expect(res.text).toContain('Forgot your password?');
    expect(mockCancel).not.toHaveBeenCalled();
    expect(statements.some((s) => /SELECT stripe_subscription_id FROM keepers/.test(s.sql))).toBe(false);
  });

  it('opens the conversation promotion moved to the keeper, with the live-site banner and a link to manage', async () => {
    const res = await request(app).get('/lighthouse/table').set('Cookie', as(PAYER_TOKEN));

    expect(res.status).toBe(200);
    expect(res.text).toContain('Tell me about your practice.');
    expect(res.text).toContain('Your site is live');
    expect(res.text).toContain(`href="/lighthouse/lamp/${PAID_BUILD}"`);
    // Not offered a second build.
    expect(res.text).not.toContain('Build my website');
    const history = statements.find((s) => /FROM companion_turns\s+WHERE keeper_id = \$1/.test(s.sql));
    expect(history!.params).toEqual([KEEPER]);
  });

  it('still sees what Haven remembers: the notes panel and the recall page ask by keeper id, where promotion put the notes', async () => {
    const table = await request(app).get('/lighthouse/table').set('Cookie', as(PAYER_TOKEN));
    expect(table.status).toBe(200);
    expect(table.text).toContain('Teaches reiki in Leeds.');
    expect(table.text).toMatch(/<span id="memory-panel-count">1<\/span> notes/);

    const recall = await request(app).get('/lighthouse/table/recall?format=json').set('Cookie', as(PAYER_TOKEN));
    expect(recall.status).toBe(200);
    expect(recall.body.items.map((item: { content: string }) => item.content)).toEqual(['Teaches reiki in Leeds.']);
    const page = await request(app).get('/lighthouse/table/recall').set('Cookie', as(PAYER_TOKEN));
    expect(page.text).toContain('Teaches reiki in Leeds.');

    const asked = statements.filter((s) => /FROM keeper_memory/.test(s.sql));
    expect(asked.length).toBe(3);
    for (const statement of asked) {
      expect(statement.sql).toMatch(/WHERE keeper_id = \$1/);
      expect(statement.params[0]).toBe(KEEPER);
    }
    // The released trial's id finds nothing, and is not what was asked.
    expect(statements.some((s) => /FROM keeper_memory/.test(s.sql) && s.params.includes(PAYER_TRIAL))).toBe(false);
  });

  it('a template the payer chooses is written under the keeper, not under the released trial', async () => {
    mockQuery.mockImplementation((sql: unknown, params?: unknown) => {
      const text = String(sql);
      const values = Array.isArray(params) ? params : [];
      statements.push({ sql: text, params: values });
      if (/INSERT INTO keeper_memory/.test(text)) return Promise.resolve({ rows: [{ memory_id: 'm-new' }] });
      return Promise.resolve(answer(text, values));
    });
    const res = await request(app)
      .post('/lighthouse/preview/choose')
      .set('Cookie', as(PAYER_TOKEN))
      .set('x-csrf-token', csrf)
      .send({ template_family_id: 'editorial-statement' });
    expect(res.status).toBe(200);
    const insert = statements.find((s) => /INSERT INTO keeper_memory/.test(s.sql));
    expect(insert!.sql).toMatch(/\(keeper_id, partition/);
    expect(insert!.params[0]).toBe(KEEPER);
  });

  it('keeps talking as the keeper: the turn is filed under the keeper, and the email gate is not asked', async () => {
    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Cookie', as(PAYER_TOKEN))
      .send({ keeperText: 'Could we change the headline?' });

    expect(res.status).toBe(200);
    expect(res.body.companionText).toBe('Of course.');
    expect(mockTurn).toHaveBeenCalledOnce();
    const opts = mockTurn.mock.calls[0][0] as { keeperId?: string; trialId?: string };
    expect(opts.keeperId).toBe(KEEPER);
    expect(opts.trialId).toBeUndefined();
    expect(statements.some((s) => /SELECT email_captured FROM trial_keepers/.test(s.sql))).toBe(false);
  });

  it('gets no account session out of it: the dashboard still asks for a login', async () => {
    const agent = request.agent(app);
    await agent.get(`/lighthouse/lamp/${PAID_BUILD}`).set('Cookie', as(PAYER_TOKEN));
    const dashboard = await agent.get('/dashboard').set('Cookie', as(PAYER_TOKEN));
    expect(dashboard.status).toBe(302);
    expect(dashboard.headers.location).toMatch(/^\/login/);
  });

  it('owns that one build only: another build on the same account is not found', async () => {
    const res = await request(app).get(`/lighthouse/lamp/${SECOND_BUILD_OF_KEEPER}`).set('Cookie', as(PAYER_TOKEN));
    expect(res.status).toBe(404);
    const cancel = await request(app)
      .post(`/lighthouse/lamp/${SECOND_BUILD_OF_KEEPER}/cancel-subscription`)
      .set('Cookie', as(PAYER_TOKEN))
      .type('form')
      .send({ _csrf: csrf });
    expect(cancel.status).toBe(404);
    expect(mockCancel).not.toHaveBeenCalled();
  });

  it('stops owning the build if the build no longer belongs to the keeper the payment created', async () => {
    world.builds[0].customer_id = OTHER_CUSTOMER;
    const res = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}`).set('Cookie', as(PAYER_TOKEN));
    expect(res.status).toBe(404);
  });

  it('a released trial with no keeper behind its build (nothing was paid) is just a stale cookie: fresh trial, not found', async () => {
    world.keepers = [];
    const res = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}`).set('Cookie', as(PAYER_TOKEN));
    expect(res.status).toBe(404);
    expect(statements.some((s) => /INSERT INTO trial_keepers/.test(s.sql))).toBe(true);
    expect(String(res.headers['set-cookie'])).not.toContain(PAYER_TOKEN);
  });
});

describe('a keeper logged in on another browser (an account session, and a trial cookie that has nothing to do with them)', () => {
  /** A browser that holds the stranger's fresh trial cookie and logs in as the keeper. */
  async function keeperSession(): Promise<ReturnType<typeof request.agent>> {
    const bcrypt = (await import('bcrypt')).default;
    const hashed = await bcrypt.hash('keeper-password', 1);
    mockQuery.mockImplementation((sql: unknown, params?: unknown) => {
      const text = String(sql);
      const values = Array.isArray(params) ? params : [];
      statements.push({ sql: text, params: values });
      if (/FROM customers WHERE email = \$1/.test(text)) {
        return Promise.resolve({ rows: [{ id: KEEPER, email: 'keeper@example.com', name: 'K', password: hashed, email_verified: true }] });
      }
      if (/SELECT handoff_build_id FROM keepers WHERE customer_id = \$1/.test(text)) {
        return Promise.resolve({ rows: world.keepers.filter((k) => k.customer_id === values[0]).map((k) => ({ handoff_build_id: k.handoff_build_id })) });
      }
      return Promise.resolve(answer(text, values));
    });
    const agent = request.agent(app);
    const login = await agent.post('/login').type('form').send({ _csrf: csrf, email: 'keeper@example.com', password: 'keeper-password' });
    expect(login.status).toBe(302);
    statements = [];
    return agent;
  }

  it('sees the keeper\'s conversation and notes, not the cookie\'s', async () => {
    const agent = await keeperSession();
    const res = await agent.get('/lighthouse/table').set('Cookie', as(STRANGER_TOKEN));
    expect(res.status).toBe(200);
    expect(res.text).toContain('Tell me about your practice.');
    expect(res.text).toContain('Teaches reiki in Leeds.');
    expect(statements.some((s) => /FROM keeper_memory/.test(s.sql) && s.params.includes(STRANGER_TRIAL))).toBe(false);
  });

  it('talks as the keeper: the turn goes to the runtime under the keeper id, so it is in the history the page reloads', async () => {
    const agent = await keeperSession();
    const res = await agent
      .post('/lighthouse/table/turn')
      .set('Cookie', as(STRANGER_TOKEN))
      .send({ keeperText: 'Could we add a page for workshops?' });
    expect(res.status).toBe(200);
    expect(mockTurn).toHaveBeenCalledOnce();
    const opts = mockTurn.mock.calls[0][0] as { keeperId?: string; trialId?: string };
    expect(opts.keeperId).toBe(KEEPER);
    expect(opts.trialId).toBeUndefined();
    // A keeper has an account: no email gate, no first-message probe on the cookie's trial.
    expect(statements.some((s) => /SELECT email_captured FROM trial_keepers/.test(s.sql))).toBe(false);
    expect(statements.some((s) => /FROM companion_turns WHERE trial_id = \$1/.test(s.sql))).toBe(false);
  });

  it('can cancel: the keeper\'s subscription, and no one else\'s, is cancelled at period end', async () => {
    const agent = await keeperSession();
    const res = await agent
      .post(`/lighthouse/lamp/${PAID_BUILD}/cancel-subscription`)
      .set('Cookie', as(STRANGER_TOKEN))
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(303);
    expect(res.headers.location).toBe(`/lighthouse/lamp/${PAID_BUILD}`);
    expect(mockCancel).toHaveBeenCalledOnce();
    expect(mockCancel).toHaveBeenCalledWith(PAID_SUB);
    const lookup = statements.find((s) => /SELECT stripe_subscription_id FROM keepers/.test(s.sql));
    expect(lookup!.params).toEqual([KEEPER]);
  });

  it('a logged-in customer who is not a keeper still talks as the trial their cookie holds', async () => {
    world.keepers = [];
    const agent = await keeperSession();
    await agent.post('/lighthouse/table/turn').set('Cookie', as(STRANGER_TOKEN)).send({ keeperText: 'Hello there' });
    const opts = mockTurn.mock.calls[0][0] as { keeperId?: string; trialId?: string };
    expect(opts.trialId).toBe(STRANGER_TRIAL);
    expect(opts.keeperId).toBeUndefined();
  });
});

describe('someone who only knows the URL', () => {
  const urls = [
    `/lighthouse/lamp/${PAID_BUILD}/published`,
    `/lighthouse/lamp/${PAID_BUILD}/published?session_id=cs_whatever_the_payer_had`,
    `/lighthouse/lamp/${PAID_BUILD}`,
    `/lighthouse/lamp/${PAID_BUILD}/make-public`,
    `/lighthouse/lamp/${PAID_BUILD}/cancelled`,
  ];

  it.each(urls)('a stranger with their own trial cookie gets 404 on %s', async (url) => {
    const res = await request(app).get(url).set('Cookie', as(STRANGER_TOKEN));
    expect(res.status).toBe(404);
    expect(res.text).not.toContain('Lantern Studio');
  });

  it.each(urls)('a visitor with no cookie at all gets 404 on %s', async (url) => {
    const res = await request(app).get(url);
    expect(res.status).toBe(404);
  });

  it('cannot cancel the payer\'s subscription', async () => {
    for (const cookie of [as(STRANGER_TOKEN), as(OTHER_TOKEN), null]) {
      const req = request(app).post(`/lighthouse/lamp/${PAID_BUILD}/cancel-subscription`).type('form');
      if (cookie) req.set('Cookie', cookie);
      const res = await req.send({ _csrf: csrf });
      expect(res.status).toBe(404);
    }
    expect(mockCancel).not.toHaveBeenCalled();
  });

  it('does not see the payer\'s conversation', async () => {
    const res = await request(app).get('/lighthouse/table').set('Cookie', as(STRANGER_TOKEN));
    expect(res.status).toBe(200);
    expect(res.text).not.toContain('Tell me about your practice.');
    expect(statements.some((s) => /FROM companion_turns\s+WHERE keeper_id/.test(s.sql))).toBe(false);
  });
});

describe('an active trial owns exactly the build stamped on it', () => {
  it('opens its own build', async () => {
    const res = await request(app).get(`/lighthouse/lamp/${OTHER_BUILD}`).set('Cookie', as(OTHER_TOKEN));
    expect(res.status).toBe(200);
    expect(res.text).toContain('Other Site');
  });

  it('is asked about ownership with BOTH its trial id and the build id, and only while unreleased', async () => {
    await request(app).get(`/lighthouse/lamp/${OTHER_BUILD}`).set('Cookie', as(OTHER_TOKEN));
    const owner = statements.find((s) => /SELECT trial_id FROM trial_keepers\s+WHERE trial_id = \$1/.test(s.sql));
    expect(owner).toBeDefined();
    expect(owner!.sql).toMatch(/WHERE trial_id = \$1 AND handoff_build_id = \$2 AND released_at IS NULL/);
    expect(owner!.params).toEqual([OTHER_TRIAL, OTHER_BUILD]);
  });

  it('does not open someone else\'s build, paid for or not', async () => {
    for (const buildId of [PAID_BUILD, SECOND_BUILD_OF_KEEPER]) {
      const res = await request(app).get(`/lighthouse/lamp/${buildId}`).set('Cookie', as(OTHER_TOKEN));
      expect(res.status).toBe(404);
    }
    const stranger = await request(app).get(`/lighthouse/lamp/${OTHER_BUILD}`).set('Cookie', as(STRANGER_TOKEN));
    expect(stranger.status).toBe(404);
  });

  it('is not treated as a keeper: no subscription panel, no cancel form, and cancel is refused', async () => {
    world.builds[1].status = 'live';
    world.builds[1].live_url = 'https://other.example.test';
    const page = await request(app).get(`/lighthouse/lamp/${OTHER_BUILD}`).set('Cookie', as(OTHER_TOKEN));
    expect(page.status).toBe(200);
    expect(page.text).not.toContain('Cancel subscription');
    const cancel = await request(app)
      .post(`/lighthouse/lamp/${OTHER_BUILD}/cancel-subscription`)
      .set('Cookie', as(OTHER_TOKEN))
      .type('form')
      .send({ _csrf: csrf });
    expect(cancel.status).toBe(403);
    expect(cancel.body.error).toBe('keeper_required');
  });
});

describe('the lamp page for each state Stripe can report for the subscription', () => {
  // keepers.subscription_status holds Stripe's own word (services/keeper_promotion.ts).
  // Each state the page does not handle with a notice is a person left staring at a
  // page that says nothing about why their card was refused or what to do.
  const SUPPORT_MAILTO = 'href="mailto:support@craftandship.io"';

  async function lampWith(status: string, cancelAtPeriodEnd = false) {
    world.keepers[0].subscription_status = status;
    world.cancelAtPeriodEnd = cancelAtPeriodEnd;
    const res = await request(app).get(`/lighthouse/lamp/${PAID_BUILD}`).set('Cookie', as(PAYER_TOKEN));
    expect(res.status).toBe(200);
    return res.text;
  }

  it('active: the price, a way to cancel, and no notice', async () => {
    const page = await lampWith('active');
    expect(page).toContain('$29 a month.');
    expect(page).toContain('Cancel subscription');
    expect(page).not.toContain(SUPPORT_MAILTO);
  });

  it('trialing: the price and no notice', async () => {
    const page = await lampWith('trialing');
    expect(page).toContain('$29 a month.');
    expect(page).not.toContain(SUPPORT_MAILTO);
  });

  it('active and ending at the close of the period: says so, and offers no second cancel', async () => {
    const page = await lampWith('active', true);
    expect(page).toContain('Subscription will end at the close of the current period; your website will stay up.');
    expect(page).not.toContain('Cancel subscription');
  });

  it('past_due: the card was refused, the website is up, the price is still shown', async () => {
    const page = await lampWith('past_due');
    expect(page).toContain('Stripe was not able to charge the card on your last invoice.');
    expect(page).toContain('Your website is still up.');
    expect(page).toContain('$29 a month.');
    expect(page).toContain(SUPPORT_MAILTO);
  });

  const noLongerInForce: Array<[string, RegExp]> = [
    ['unpaid', /marked your subscription as unpaid because the card could not be charged/],
    ['paused', /Your subscription is paused, so nothing is being billed\./],
    ['incomplete', /Your first payment has not been confirmed yet, so the subscription has not started\./],
    ['incomplete_expired', /Your first payment was not confirmed in time, so Stripe closed that subscription before it started\./],
    ['canceled', /Your subscription has ended\./],
  ];
  for (const [status, sentence] of noLongerInForce) {
    it(`${status}: a notice that says what happened, that the website is up, and who to write to`, async () => {
      const page = await lampWith(status);
      expect(page).toMatch(sentence);
      expect(page).toContain('Your website is still up.');
      expect(page).toContain(SUPPORT_MAILTO);
      // A subscription that is not in force shows no price and no cancel link.
      expect(page).not.toContain('$29 a month.');
      expect(page).not.toContain('Cancel subscription');
      expect(page).not.toContain('Subscription will end');
    });
  }

  it('canceled is not described as paused', async () => {
    const page = await lampWith('canceled');
    expect(page).not.toContain('Subscription is paused');
  });

  it('a subscription that is not in force is not shown as ending at the close of a period', async () => {
    // cancel_at_period_end can still be true on the audit row when a later event ends the subscription.
    const page = await lampWith('unpaid', true);
    expect(page).toContain('marked your subscription as unpaid');
    expect(page).not.toContain('Subscription will end');
  });
});
