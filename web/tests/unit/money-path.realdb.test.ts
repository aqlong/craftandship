// The money, account and safety paths against a real Postgres.
//
// Every other web test replaces the database with a double, so no WHERE
// clause, lock or constraint in them is ever executed. This file is the
// counterweight: it loads the real schema.sql into a throwaway database,
// boots the real Express app on a real connection pool, and drives the real
// HTTP routes. No application module is mocked. Stripe is not contacted:
// webhook events are built here and signed with a signing secret generated
// for the run, which is all the signature check needs, and any outbound
// request to a host other than this machine is made to throw (see
// blockOutboundRequests). No model is called and no email is sent (neither
// key is set; the mailer records "skipped" and prints its links to the
// console, where the tests read them). One test also runs the build worker's
// own cleanup function, in a Python child process, on the same database.
//
// Three single functions are replaced, each for the length of one test and
// named where it happens: the call that would create a Checkout session at
// Stripe, the model call inside spec extraction, and the model client's
// `create` method for the one test that runs memory capture. Everything
// around them (the routes, the dispatch service, the webhook, the SQL) is the
// application's own.
//
// It runs only when CAS_SCHEMA_TEST_DATABASE_URL points at a THROWAWAY
// database (the same switch as schema.idempotency.test.ts). Rows are created
// under a per-run suffix and are not cleaned up.

import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';
import http from 'http';
import https from 'https';
import { spawnSync } from 'child_process';
import bcrypt from 'bcrypt';
import request from 'supertest';
import { Client } from 'pg';
import type { Application } from 'express';
import type { Pool } from 'pg';

const integrationUrl = process.env.CAS_SCHEMA_TEST_DATABASE_URL;
const schema = fs.readFileSync(path.join(__dirname, '../../src/db/schema.sql'), 'utf-8');

// ---------------------------------------------------------------------------
// The build worker, run for real
// ---------------------------------------------------------------------------
//
// One test below needs the worker's cleanup of old failed builds to run
// between the web app's statements, because the bug it guards against lived
// exactly there: the web app's payment rule read rows that the worker deletes.
// It calls the worker's own function, queue/worker_pg.py::_cleanup_old_builds,
// in a Python child process on the same database. The alternative, lifting the
// function's SQL text out of the Python file and replaying it from here, was
// rejected: the replay would be this file's copy of the worker's control flow,
// and it would keep passing after the worker changed.
//
// The price is that the test needs a Python that can import the worker
// (psycopg2 and the worker's other imports). CAS_WORKER_PYTHON names the
// interpreter; when it is set the test must run, and fails if the import
// does. When it is unset, `python3` is tried and the test is skipped, with
// the reason printed, if that interpreter cannot import the worker. The admin
// and ops deletion tests next to it need no Python and always run.
const REPO_ROOT = path.join(__dirname, '../../..');
const WORKER_PYTHON = process.env.CAS_WORKER_PYTHON || 'python3';
const WORKER_IMPORT = [
  'import sys',
  'for p in (sys.argv[1] + "/queue", sys.argv[1] + "/cli", sys.argv[1]):',
  '    sys.path.insert(0, p)',
  'import psycopg2',
  'import worker_pg',
].join('\n');
const WORKER_PREVIEW = [
  WORKER_IMPORT,
  'conn = psycopg2.connect(sys.argv[2])',
  'cur = conn.cursor()',
  'cur.execute("SELECT started_at FROM builds WHERE id = %s", (sys.argv[3],))',
  'token = cur.fetchone()[0]',
  'cur.close()',
  'landed = worker_pg._set_preview_status(conn, sys.argv[3], preview_url="https://worker-landed.example.test", attempt=token)',
  'conn.close()',
  'print("worker-preview-landed" if landed else "worker-preview-refused")',
].join('\n');
const WORKER_CLEANUP = [
  WORKER_IMPORT,
  'conn = psycopg2.connect(sys.argv[2])',
  'worker_pg._cleanup_old_builds(conn, sys.argv[3], sys.argv[4])',
  'conn.close()',
  'print("worker-cleanup-ran")',
].join('\n');
function runWorkerPython(
  script: string,
  args: string[],
  extraEnv: Record<string, string | undefined> = {}
): { ok: boolean; output: string } {
  // A value of undefined removes the variable for the child.
  const env: Record<string, string | undefined> = { ...process.env, ...extraEnv, PYTHONDONTWRITEBYTECODE: '1' };
  for (const key of Object.keys(env)) if (env[key] === undefined) delete env[key];
  const result = spawnSync(WORKER_PYTHON, ['-c', script, REPO_ROOT, ...args], {
    encoding: 'utf-8',
    timeout: 60_000,
    // Keep Python from writing bytecode files into the repository.
    env: env as NodeJS.ProcessEnv,
  });
  const output = `${result.stdout || ''}${result.stderr || ''}${result.error ? result.error.message : ''}`;
  return { ok: result.status === 0, output };
}
const workerProbe: { ok: boolean; reason: string } = (() => {
  if (!integrationUrl) return { ok: false, reason: 'no database configured' };
  const probe = runWorkerPython(WORKER_IMPORT, []);
  return { ok: probe.ok, reason: probe.ok ? '' : probe.output.trim().split('\n').slice(-1)[0] };
})();
const workerRequired = Boolean(process.env.CAS_WORKER_PYTHON);
if (integrationUrl && !workerProbe.ok && !workerRequired) {
  process.stderr.write(
    `money-path.realdb.test.ts: SKIPPING the worker-cleanup test: ${WORKER_PYTHON} cannot import queue/worker_pg.py (${workerProbe.reason}). ` +
      'Set CAS_WORKER_PYTHON to an interpreter with the worker requirements installed to run it.\n'
  );
}
const workerIt = workerProbe.ok || workerRequired ? it : it.skip;
/** Land one build at preview with the worker's own function, as a worker whose environment is `extraEnv` would. */
function runWorkerPreview(buildId: string, extraEnv: Record<string, string | undefined>): void {
  const result = runWorkerPython(WORKER_PREVIEW, [integrationUrl as string, buildId], extraEnv);
  expect(result.ok, result.output).toBe(true);
  expect(result.output).toContain('worker-preview-landed');
}
/** Run the worker's real cleanup for one build of one app, on the test database. */
function runWorkerCleanup(buildId: string, appName: string): void {
  const result = runWorkerPython(WORKER_CLEANUP, [integrationUrl as string, buildId, appName]);
  expect(result.ok, result.output).toBe(true);
  expect(result.output).toContain('worker-cleanup-ran');
}

const run = crypto.randomBytes(4).toString('hex');
const uuid = (): string => crypto.randomUUID();
const email = (label: string): string => `${label}-${run}@example.test`;
// Generated per run; nothing here is a real credential.
const PASSWORD = `pw-${crypto.randomBytes(9).toString('hex')}`;
const WEBHOOK_SECRET = ['whsec', crypto.randomBytes(16).toString('hex')].join('_');
const WORKER_SECRET = crypto.randomBytes(16).toString('hex');
const OPS_KEY = crypto.randomBytes(16).toString('hex');
const ADMIN_EMAIL = email('admin');
const CRISIS_TEXT = 'I want to kill myself';

let app: Application;
let appPool: { end: () => Promise<void> };
/** The application's own connection pool, for calling its services directly. */
let appDbPool: Pool;
/** The application's dispatch service (the module instance the app itself uses). */
let harborDispatch: typeof import('../../src/services/harbor_dispatch');
/** Other modules of the same application instance that the later tests call or stand in for. */
let stripeService: typeof import('../../src/services/stripe');
let specExtraction: typeof import('../../src/services/spec_extraction');
let memoryCapture: typeof import('../../src/companion/memory_capture');
let cronScheduler: typeof import('../../src/services/harbor-cron-scheduler');
let buildStartLimits: typeof import('../../src/services/build-start-limits');
let companionClaude: typeof import('../../src/companion/claude');
let llmBudget: typeof import('../../src/services/llm-budget');
let authRoutes: typeof import('../../src/routes/auth');
let adminRoutes: typeof import('../../src/routes/admin');
let db: Client;
let passwordHash: string;
let orderingColumn = false;
let resetOrderingCache: () => void = () => undefined;
let infoSpy: ReturnType<typeof vi.spyOn>;

async function q<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T[]> {
  const res = await db.query(sql, params);
  return res.rows as T[];
}
async function one<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T> {
  const rows = await q<T>(sql, params);
  expect(rows, sql).toHaveLength(1);
  return rows[0];
}

/** Apply schema.sql the way runMigration() does. Retried: another test file may be applying it to the same database. */
async function applySchema(client: Client): Promise<void> {
  for (let attempt = 1; ; attempt += 1) {
    try {
      await client.query('BEGIN');
      await client.query(schema);
      await client.query('COMMIT');
      return;
    } catch (err) {
      await client.query('ROLLBACK').catch(() => undefined);
      if (attempt >= 5) throw err;
      await new Promise((resolve) => setTimeout(resolve, 300 * attempt));
    }
  }
}

async function createCustomer(label: string, opts: { verified?: boolean } = {}): Promise<{ id: string; email: string }> {
  const address = email(label);
  const row = await one<{ id: string }>(
    `INSERT INTO customers (email, password, name, email_verified) VALUES ($1, $2, $3, $4) RETURNING id`,
    [address, passwordHash, label, opts.verified ?? true]
  );
  return { id: row.id, email: address };
}

interface BuildOpts {
  status?: string;
  review?: string | null;
  parent?: string | null;
  reviewedBy?: string | null;
  /** Insert the row the way the Harbor dispatch does: is_harbor = TRUE. */
  harbor?: boolean;
  appName?: string;
  /** Minutes in the past for created_at (the worker's cleanup ranks by it). */
  ageMinutes?: number;
}
/**
 * Insert a build row. Like every statement in the application that creates a
 * child, it names parent_build_id and says nothing about is_harbor for a
 * child: whether a child is a Harbor build is the schema's business (the
 * builds_inherit_is_harbor trigger). Only a root can be marked, as the
 * dispatch marks it.
 */
async function createBuild(customerId: string | null, opts: BuildOpts = {}): Promise<string> {
  const id = uuid();
  const columns = `id, customer_id, tier, status, review_status, app_name, preview_url, parent_build_id,
                         reviewed_by, reviewed_at, review_notes, spec, created_at`;
  const values = `$1, $2, 'build', $3, $4, $5, $6, $7, $8, $9, $10, '{}'::jsonb, NOW() - make_interval(mins => $11::int)`;
  await q(
    opts.harbor
      ? `INSERT INTO builds (${columns}, is_harbor) VALUES (${values}, TRUE)`
      : `INSERT INTO builds (${columns}) VALUES (${values})`,
    [
      id,
      customerId,
      opts.status ?? 'preview',
      opts.review === undefined ? 'approved' : opts.review,
      opts.appName ?? `Site ${id.slice(0, 6)}`,
      `https://preview-${id.slice(0, 8)}.example.test`,
      opts.parent ?? null,
      opts.reviewedBy ?? null,
      opts.reviewedBy ? new Date() : null,
      opts.reviewedBy ? 'looked fine' : null,
      opts.ageMinutes ?? 0,
    ]
  );
  return id;
}
const isHarbor = async (id: string): Promise<boolean> =>
  (await one<{ is_harbor: boolean }>('SELECT is_harbor FROM builds WHERE id = $1', [id])).is_harbor;
const buildRow = (id: string) =>
  one<{
    status: string;
    live_url: string | null;
    preview_url: string | null;
    review_status: string | null;
    reviewed_at: Date | null;
    reviewed_by: string | null;
    review_notes: string | null;
    stripe_session_id: string | null;
    customer_id: string | null;
  }>(
    `SELECT status, live_url, preview_url, review_status, reviewed_at, reviewed_by, review_notes,
            stripe_session_id, customer_id FROM builds WHERE id = $1`,
    [id]
  );

type Browser = ReturnType<typeof request.agent>;
const browser = (): Browser => request.agent(app);

function csrfFrom(html: string): string {
  const match = html.match(/name="_csrf" value="([^"]+)"/);
  return match ? match[1] : '';
}
async function freshCsrf(agent: Browser): Promise<string> {
  return csrfFrom((await agent.get('/login')).text);
}
async function loggedIn(address: string): Promise<Browser> {
  const agent = browser();
  const res = await agent.post('/login').type('form').send({ _csrf: await freshCsrf(agent), email: address, password: PASSWORD });
  expect(res.status, `login as ${address}`).toBe(302);
  return agent;
}

/** A browser holding a fresh Harbor trial cookie, and that trial's id. */
async function trialBrowser(): Promise<{ agent: Browser; trialId: string; token: string }> {
  const agent = browser();
  const res = await agent.get('/lighthouse/table');
  expect(res.status).toBe(200);
  const cookie = ([] as string[]).concat(res.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
  const token = String(cookie).split(';')[0].split('=')[1];
  const row = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [token]);
  return { agent, trialId: row.trial_id, token };
}

/**
 * Nothing in this file may leave the machine. The routes run unmocked, so a
 * regression in one of them (a make-public request that reaches Stripe, say)
 * would otherwise send a real request with test data in it. Outbound HTTP and
 * HTTPS to anything but loopback throws here instead, and the test that
 * caused it fails. (The database connection is a plain socket and is not
 * affected; supertest talks to 127.0.0.1.)
 */
function blockOutboundRequests(): void {
  const isLoopback = (host: string): boolean => /^(localhost|127\.\d+\.\d+\.\d+|::1|\[::1\])$/i.test(host);
  const hostOf = (target: unknown): string => {
    if (typeof target === 'string') return new URL(target).hostname;
    if (target instanceof URL) return target.hostname;
    const options = (target || {}) as { hostname?: string; host?: string };
    return String(options.hostname || options.host || 'localhost').replace(/:\d+$/, '');
  };
  for (const transport of [http, https]) {
    const original = transport.request.bind(transport) as (...args: unknown[]) => unknown;
    vi.spyOn(transport, 'request').mockImplementation(((...args: unknown[]) => {
      const host = hostOf(args[0]);
      if (!isLoopback(host)) throw new Error(`money-path.realdb.test.ts: outbound request to ${host} blocked`);
      return original(...args);
    }) as never);
  }
  const originalFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation(((input: unknown, init?: unknown) => {
    const url = typeof input === 'string' || input instanceof URL ? input : (input as { url: string }).url;
    const host = new URL(String(url)).hostname;
    if (!isLoopback(host)) return Promise.reject(new Error(`money-path.realdb.test.ts: outbound fetch to ${host} blocked`));
    return originalFetch(input as never, init as never);
  }) as never);
}

let eventSeq = 0;
function stripeEvent(type: string, object: Record<string, unknown>, created?: number) {
  eventSeq += 1;
  return {
    id: `evt_${run}_${eventSeq}`,
    object: 'event',
    type,
    created: created ?? Math.floor(Date.now() / 1000),
    data: { object },
  };
}
/** POST a locally signed event to the real webhook route. */
async function deliver(event: Record<string, unknown>) {
  const payload = JSON.stringify(event);
  const timestamp = Math.floor(Date.now() / 1000);
  const signature = crypto.createHmac('sha256', WEBHOOK_SECRET).update(`${timestamp}.${payload}`).digest('hex');
  return request(app)
    .post('/webhooks/stripe')
    .set('Content-Type', 'application/json')
    .set('Stripe-Signature', `t=${timestamp},v1=${signature}`)
    .send(payload);
}
const claimed = async (eventId: unknown): Promise<number> =>
  Number((await one<{ n: string }>('SELECT COUNT(*) AS n FROM processed_webhooks WHERE event_id = $1', [eventId])).n);

function checkoutSession(opts: {
  buildId: string;
  trialId?: string | null;
  /** What the route records when the account's own login or paying cookie opened the session. */
  keeperCustomerId?: string | null;
  email: string;
  tag: string;
  amount?: string;
  paymentStatus?: string;
}) {
  const metadata: Record<string, string> = {
    kind: 'harbor_make_public',
    build_id: opts.buildId,
    monthly_amount_cents: opts.amount ?? '2900',
    sliding_scale_attestation: 'true',
  };
  if (opts.trialId) metadata.trial_id = opts.trialId;
  if (opts.keeperCustomerId) metadata.keeper_customer_id = opts.keeperCustomerId;
  return {
    id: `cs_${run}_${opts.tag}`,
    object: 'checkout.session',
    payment_status: opts.paymentStatus ?? 'paid',
    customer: `cus_${run}_${opts.tag}`,
    subscription: `sub_${run}_${opts.tag}`,
    customer_email: opts.email,
    metadata,
  };
}
const subscriptionObject = (tag: string, overrides: Record<string, unknown> = {}) => ({
  id: `sub_${run}_${tag}`,
  object: 'subscription',
  status: 'active',
  cancel_at_period_end: false,
  current_period_start: 1_760_000_000,
  current_period_end: 1_762_592_000,
  metadata: { kind: 'harbor_make_public' },
  ...overrides,
});
const subscriptionRow = (tag: string) =>
  one<{ status: string; cancel_at_period_end: boolean; keeper_customer_id: string; monthly_amount_cents: number }>(
    'SELECT status, cancel_at_period_end, keeper_customer_id, monthly_amount_cents FROM harbor_subscriptions WHERE stripe_subscription_id = $1',
    [`sub_${run}_${tag}`]
  );
const keeperRow = (customerId: string) =>
  one<{ subscription_status: string; stripe_subscription_id: string | null; handoff_build_id: string | null }>(
    'SELECT subscription_status, stripe_subscription_id, handoff_build_id FROM keepers WHERE customer_id = $1',
    [customerId]
  );

/** A trial with an approved preview build attached, as the dispatch route leaves it: a Harbor build, stamped on the trial. */
async function trialWithPreview(tag: string, build: BuildOpts = {}) {
  const visitor = await trialBrowser();
  const payer = await createCustomer(`payer-${tag}`, { verified: false });
  const buildId = await createBuild(payer.id, { harbor: true, ...build });
  await q('UPDATE trial_keepers SET handoff_build_id = $1, handoff_at = NOW(), email_captured = $2 WHERE trial_id = $3', [
    buildId,
    payer.email,
    visitor.trialId,
  ]);
  await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, 'I teach reiki', 'Tell me more.')`, [
    visitor.trialId,
  ]);
  return { ...visitor, payer, buildId };
}

/** The newest link of a kind that the mailer printed to the console. */
function latestDevLink(kind: 'Verification link' | 'Password reset link', address?: string): string {
  const lines = infoSpy.mock.calls.map((call) => call.map(String).join(' ')).filter((line) => line.includes(`[dev] Email is not configured. ${kind}:`));
  expect(lines.length, `${kind} printed${address ? ` for ${address}` : ''}`).toBeGreaterThan(0);
  const url = lines[lines.length - 1].split(`${kind}:`)[1].trim();
  return url.slice(url.indexOf('/', url.indexOf('//') + 2));
}

describe.skipIf(!integrationUrl)('money, account and safety paths against a real Postgres (integration)', () => {
  beforeAll(async () => {
    db = new Client({ connectionString: integrationUrl });
    await db.connect();
    await applySchema(db);
    orderingColumn =
      (
        await q(
          `SELECT 1 FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = 'harbor_subscriptions' AND column_name = 'last_event_created'`
        )
      ).length > 0;

    process.env.DATABASE_URL = integrationUrl;
    process.env.CAS_HARBOR_ENABLED = 'true';
    process.env.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET;
    process.env.WORKER_WEBHOOK_SECRET = WORKER_SECRET;
    process.env.OPS_API_KEY = OPS_KEY;
    process.env.ADMIN_EMAIL = ADMIN_EMAIL;
    delete process.env.RESEND_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
    vi.resetModules();
    blockOutboundRequests();
    infoSpy = vi.spyOn(console, 'info').mockImplementation(() => undefined);
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    app = (await import('../../src/app')).default;
    appPool = (await import('../../src/db/index')).default as unknown as { end: () => Promise<void> };
    appDbPool = appPool as unknown as Pool;
    harborDispatch = await import('../../src/services/harbor_dispatch');
    stripeService = await import('../../src/services/stripe');
    specExtraction = await import('../../src/services/spec_extraction');
    memoryCapture = await import('../../src/companion/memory_capture');
    cronScheduler = await import('../../src/services/harbor-cron-scheduler');
    buildStartLimits = await import('../../src/services/build-start-limits');
    companionClaude = await import('../../src/companion/claude');
    llmBudget = await import('../../src/services/llm-budget');
    authRoutes = await import('../../src/routes/auth');
    adminRoutes = await import('../../src/routes/admin');
    resetOrderingCache = (await import('../../src/routes/webhooks')).resetEventOrderingCache;
    passwordHash = await bcrypt.hash(PASSWORD, 4);
    await createCustomer('admin');
  }, 60_000);

  afterAll(async () => {
    vi.restoreAllMocks();
    if (appPool) await appPool.end();
    if (db) await db.end();
  });

  // -------------------------------------------------------------------------
  describe('the review gate: POST /builds/:buildId/approve, one row per state', () => {
    let owner: { id: string; email: string };
    let stranger: { id: string; email: string };
    let ownerBrowser: Browser;

    beforeAll(async () => {
      owner = await createCustomer('owner');
      stranger = await createCustomer('stranger');
      ownerBrowser = await loggedIn(owner.email);
    });

    const approve = async (agent: Browser, buildId: string) =>
      agent.post(`/builds/${buildId}/approve`).type('form').send({ _csrf: await freshCsrf(agent) });

    it('the owner of a reviewed, approved preview takes it live', async () => {
      const id = await createBuild(owner.id);
      const res = await approve(ownerBrowser, id);
      expect(res.status).toBe(302);
      const row = await buildRow(id);
      expect(row.status).toBe('live');
      expect(row.live_url).toBe(row.preview_url);
    });

    it('not the owner: 404, and the build stays in preview', async () => {
      const id = await createBuild(stranger.id);
      const res = await approve(ownerBrowser, id);
      expect(res.status).toBe(404);
      expect((await buildRow(id)).status).toBe('preview');
    });

    it.each([
      ['pending', 403],
      ['changes_requested', 403],
      [null, 403],
      ['ai_review_queued', 403],
      ['ai_review_held', 403],
    ] as Array<[string | null, number]>)('not reviewed (review_status = %s): %i, and the build stays in preview', async (review, status) => {
      const id = await createBuild(owner.id, { review });
      const res = await approve(ownerBrowser, id);
      expect(res.status).toBe(status);
      const row = await buildRow(id);
      expect(row.status).toBe('preview');
      expect(row.live_url).toBeNull();
    });

    it.each(['queued', 'building', 'deploying', 'failed', 'abandoned'])(
      'not in preview (status = %s), even with an approved verdict on the row: 409, and nothing changes',
      async (status) => {
        const id = await createBuild(owner.id, { status, review: 'approved' });
        const res = await approve(ownerBrowser, id);
        expect(res.status).toBe(409);
        const row = await buildRow(id);
        expect(row.status).toBe(status);
        expect(row.live_url).toBeNull();
      }
    );

    it('an unknown build id: 404', async () => {
      expect((await approve(ownerBrowser, uuid())).status).toBe(404);
    });

    it('a logged-out visitor is sent to log in and changes nothing', async () => {
      const id = await createBuild(owner.id);
      const res = await request(app).post(`/builds/${id}/approve`).type('form').send({});
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/^\/login/);
      expect((await buildRow(id)).status).toBe('preview');
    });

    it('the admin may take any preview build live, reviewed or not, but still only from preview', async () => {
      const admin = await loggedIn(ADMIN_EMAIL);
      const unreviewed = await createBuild(stranger.id, { review: 'pending' });
      expect((await approve(admin, unreviewed)).status).toBe(302);
      expect((await buildRow(unreviewed)).status).toBe('live');

      const queued = await createBuild(stranger.id, { status: 'queued', review: 'approved' });
      expect((await approve(admin, queued)).status).toBe(409);
      expect((await buildRow(queued)).status).toBe('queued');
    });

    // These fixtures used to make a build "Harbor" by stamping it on a trial
    // (UPDATE trial_keepers SET handoff_build_id) after inserting it, because
    // the gate derived Harbor-ness from those stamps and from parent_build_id.
    // The gate now reads builds.is_harbor, so the fixtures insert the root the
    // way the dispatch does (is_harbor = TRUE) and let children inherit.
    describe('a Harbor website does not go public here without its subscription', () => {
      it('a Harbor build, owner has no subscription: sent to the make-public page, still in preview', async () => {
        const id = await createBuild(owner.id, { harbor: true });

        const res = await approve(ownerBrowser, id);
        expect(res.status).toBe(303);
        expect(res.headers.location).toBe(`/lighthouse/lamp/${id}/make-public`);
        expect((await buildRow(id)).status).toBe('preview');
        // And that page is there for the logged-in owner.
        expect((await ownerBrowser.get(String(res.headers.location))).status).toBe(200);
      });

      it('a revision of a Harbor build (a child row whose INSERT says nothing about Harbor) is a Harbor build too', async () => {
        const root = await createBuild(owner.id, { status: 'live', harbor: true });
        const child = await createBuild(owner.id, { parent: root });
        const grandchild = await createBuild(owner.id, { parent: child });

        for (const id of [child, grandchild]) {
          expect(await isHarbor(id)).toBe(true);
          const res = await approve(ownerBrowser, id);
          expect(res.status).toBe(303);
          expect((await buildRow(id)).status).toBe('preview');
        }
      });

      it.each([
        ['active', 302, 'live'],
        ['trialing', 302, 'live'],
        ['past_due', 302, 'live'],
        ['unpaid', 303, 'preview'],
        ['paused', 303, 'preview'],
        ['incomplete', 303, 'preview'],
        ['incomplete_expired', 303, 'preview'],
        ['canceled', 303, 'preview'],
      ])('owner is a keeper whose subscription is %s: %i, build ends %s', async (subscriptionStatus, status, endsAs) => {
        const keeper = await createCustomer(`keeper-${subscriptionStatus}`);
        const root = await createBuild(keeper.id, { status: 'live', harbor: true });
        await q(
          `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status,
                                stripe_subscription_id, handoff_build_id)
           VALUES ($1, 'Haven', 'Haven', $2, $3, $4)`,
          [keeper.id, subscriptionStatus, `sub_${run}_gate_${subscriptionStatus}`, root]
        );
        const revision = await createBuild(keeper.id, { parent: root });

        const res = await approve(await loggedIn(keeper.email), revision);
        expect(res.status).toBe(status);
        expect((await buildRow(revision)).status).toBe(endsAs);
      });

      it('a keeper row without a subscription id does not count as subscribed', async () => {
        const keeper = await createCustomer('keeper-nosub');
        const id = await createBuild(keeper.id, { harbor: true });
        await q(
          `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status, handoff_build_id)
           VALUES ($1, 'Haven', 'Haven', 'active', $2)`,
          [keeper.id, id]
        );
        expect((await approve(await loggedIn(keeper.email), id)).status).toBe(303);
        expect((await buildRow(id)).status).toBe('preview');
      });

      it('someone else\'s subscription does not publish my Harbor build', async () => {
        const subscriber = await createCustomer('subscriber');
        await q(
          `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status, stripe_subscription_id)
           VALUES ($1, 'Haven', 'Haven', 'active', $2)`,
          [subscriber.id, `sub_${run}_someone_else`]
        );
        const mine = await createBuild(owner.id, { harbor: true });
        expect((await approve(ownerBrowser, mine)).status).toBe(303);
        expect((await buildRow(mine)).status).toBe('preview');
      });

      it('a build that is not a Harbor build, and its rebuilds, still publish without a subscription', async () => {
        const root = await createBuild(owner.id, { status: 'live' });
        const child = await createBuild(owner.id, { parent: root });
        expect(await isHarbor(child)).toBe(false);
        expect((await approve(ownerBrowser, child)).status).toBe(302);
        expect((await buildRow(child)).status).toBe('live');
      });
    });
  });

  // -------------------------------------------------------------------------
  // The payment rule reads builds.is_harbor, a column of the build itself.
  // Before, it was derived at publish time from parent_build_id and from the
  // handoff_build_id stamps, which are rows and columns that other code
  // deletes: the worker's cleanup of old failed builds, the admin and ops
  // delete and cleanup routes. Each test here builds a Harbor lineage through
  // the application's own statements, lets one of those deleters run for
  // real, and then asks for the publish.
  describe('the Harbor rule survives everything that deletes builds', () => {
    let admin: Browser;
    let lineageSeq = 0;

    beforeAll(async () => {
      admin = await loggedIn(ADMIN_EMAIL);
    });

    interface Lineage {
      owner: { id: string; email: string };
      ownerBrowser: Browser;
      /** The trial that dispatched the root, and its browser. */
      agent: Browser;
      trialId: string;
      root: string;
      appName: string;
    }

    const approve = async (agent: Browser, buildId: string) =>
      agent.post(`/builds/${buildId}/approve`).type('form').send({ _csrf: await freshCsrf(agent) });
    const ops = (method: 'post' | 'delete', pathname: string, body: Record<string, unknown> = {}) =>
      request(app)[method](`/ops/api/v1${pathname}`).set('Authorization', `Bearer ${OPS_KEY}`).send(body);
    const newBuildIdFrom = (location: unknown): string => String(location).split('/').pop() as string;
    const setState = (id: string, status: string, review: string | null, ageMinutes: number) =>
      q(
        `UPDATE builds SET status = $2, review_status = $3, preview_url = COALESCE(preview_url, 'https://preview.example.test'),
                created_at = NOW() - make_interval(mins => $4::int) WHERE id = $1`,
        [id, status, review, ageMinutes]
      );

    /**
     * What a dispatch leaves behind, for an owner of its own: a build
     * inserted by the application's own Harbor insert (insertHarborBuild, on
     * the application's pool), stamped on a trial, and here already failed.
     */
    async function dispatchedAndFailed(): Promise<Lineage> {
      lineageSeq += 1;
      const owner = await createCustomer(`lineage-owner-${lineageSeq}`);
      const ownerBrowser = await loggedIn(owner.email);
      const appName = `harbor-lineage-${run}-${lineageSeq}`;
      const visitor = await trialBrowser();
      const root = await harborDispatch.insertHarborBuild(appDbPool, {
        customerId: owner.id,
        tier: 'build',
        spec: { app_name: appName },
        appName,
      });
      await q('UPDATE trial_keepers SET handoff_build_id = $1, handoff_at = NOW(), email_captured = $2 WHERE trial_id = $3', [
        root,
        owner.email,
        visitor.trialId,
      ]);
      // Enough conversation that the "Build my free preview" button would be
      // offered again if nothing else stopped it.
      await q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'turn ' || n, 'ok' FROM generate_series(1, 6) AS n`,
        [visitor.trialId]
      );
      await setState(root, 'failed', null, 180);
      return { owner, ownerBrowser, agent: visitor.agent, trialId: visitor.trialId, root, appName };
    }
    /** The owner's "Rebuild" button, through the real route. Returns the new build's id. */
    async function rebuild(t: Lineage, parentId: string): Promise<string> {
      const res = await t.ownerBrowser
        .post(`/builds/${parentId}/rebuild`)
        .type('form')
        .send({ _csrf: await freshCsrf(t.ownerBrowser) });
      expect(res.status, `rebuild of ${parentId}`).toBe(302);
      return newBuildIdFrom(res.headers.location);
    }
    const trialStamp = (trialId: string) =>
      one<{ handoff_build_id: string | null; dispatched: boolean }>(
        'SELECT handoff_build_id, handoff_at IS NOT NULL AS dispatched FROM trial_keepers WHERE trial_id = $1',
        [trialId]
      );
    const buildCount = async (customerId: string): Promise<number> =>
      Number((await one<{ n: string }>('SELECT COUNT(*) AS n FROM builds WHERE customer_id = $1', [customerId])).n);
    /** The owner asks to publish; the answer must be "subscribe first", and nothing is written. */
    async function expectPublishRefused(t: Lineage, buildId: string): Promise<void> {
      const res = await approve(t.ownerBrowser, buildId);
      expect(res.status, 'approve').toBe(303);
      expect(res.headers.location).toBe(`/lighthouse/lamp/${buildId}/make-public`);
      const row = await buildRow(buildId);
      expect(row.status).toBe('preview');
      expect(row.live_url).toBeNull();
    }
    /** The trial asks for another free build after its first one was deleted. */
    async function expectNoSecondFreeBuild(t: Lineage): Promise<void> {
      const before = await buildCount(t.owner.id);
      const res = await t.agent.post('/lighthouse/build').send({});
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('build_already_used');
      expect(await buildCount(t.owner.id)).toBe(before);
      const page = await t.agent.get('/lighthouse/table');
      expect(page.status).toBe(200);
      expect(page.text).not.toContain('Build my free preview');
    }

    it('the application\'s Harbor insert marks the row, and each route that makes a child carries the mark over', async () => {
      const t = await dispatchedAndFailed();
      expect(await isHarbor(t.root)).toBe(true);
      await setState(t.root, 'preview', 'approved', 60);

      const children: Array<[string, string]> = [];
      // The owner's rebuild.
      children.push(['rebuild', await rebuild(t, t.root)]);
      await setState(children[0][1], 'failed', null, 50);
      // The owner's revision, legacy form and chat confirmation.
      const legacy = await t.ownerBrowser
        .post(`/builds/${t.root}/revision`)
        .type('form')
        .send({ _csrf: await freshCsrf(t.ownerBrowser), revision_feedback: 'make the header green' });
      expect(legacy.status).toBe(302);
      children.push(['revision form', newBuildIdFrom(legacy.headers.location)]);
      await setState(children[1][1], 'failed', null, 40);
      const confirmed = await t.ownerBrowser
        .post(`/builds/${t.root}/revision/confirm`)
        .type('form')
        .send({ _csrf: await freshCsrf(t.ownerBrowser) });
      expect(confirmed.status).toBe(302);
      children.push(['revision confirm', newBuildIdFrom(confirmed.headers.location)]);
      await setState(children[2][1], 'failed', null, 30);
      // The admin's "fix as a new build".
      const fixed = await admin
        .post(`/admin/builds/${t.root}/ai-fix`)
        .type('form')
        .send({ _csrf: await freshCsrf(admin), rebuild_request: 'tidy the footer', force_new: '1' });
      expect(fixed.status).toBe(302);
      children.push(['admin ai-fix as new', newBuildIdFrom(fixed.headers.location)]);
      // The ops API rebuild.
      const viaOps = await ops('post', `/builds/${t.root}/rebuild`);
      expect(viaOps.status).toBe(200);
      children.push(['ops rebuild', viaOps.body.new_build.id]);
      // Any other writer that names a parent and says nothing about Harbor:
      // the shape of the coordinator's queue_rebuild INSERT (coordinator/actuators.py).
      children.push([
        'an INSERT with only parent_build_id',
        await createBuild(t.owner.id, { parent: t.root, status: 'queued', review: null }),
      ]);

      for (const [how, id] of children) {
        const row = await one<{ is_harbor: boolean; parent_build_id: string | null }>(
          'SELECT is_harbor, parent_build_id FROM builds WHERE id = $1',
          [id]
        );
        expect(row.parent_build_id, how).toBe(t.root);
        expect(row.is_harbor, how).toBe(true);
        await setState(id, 'preview', 'approved', 5);
        await expectPublishRefused(t, id);
      }
    });

    it('a child keeps the mark when it is detached, gains it when moved under a Harbor build, and nothing un-marks a row', async () => {
      const owner = await createCustomer('lineage-trigger-owner');
      const harborRoot = await createBuild(owner.id, { harbor: true, status: 'live' });
      const child = await createBuild(owner.id, { parent: harborRoot });
      await q('UPDATE builds SET parent_build_id = NULL WHERE id = $1', [child]);
      expect(await isHarbor(child)).toBe(true);

      const plainRoot = await createBuild(owner.id, { status: 'live' });
      const plainChild = await createBuild(owner.id, { parent: plainRoot });
      expect(await isHarbor(plainChild)).toBe(false);
      await q('UPDATE builds SET parent_build_id = $1 WHERE id = $2', [harborRoot, plainChild]);
      expect(await isHarbor(plainChild)).toBe(true);
      // Moving it back under a build that is not Harbor does not clear it.
      await q('UPDATE builds SET parent_build_id = $1 WHERE id = $2', [plainRoot, plainChild]);
      expect(await isHarbor(plainChild)).toBe(true);
    });

    workerIt(
      'the worker\'s cleanup of old failed builds (queue/worker_pg.py, run for real) does not open the gate',
      async () => {
        expect(workerProbe.ok, `CAS_WORKER_PYTHON is set but the worker cannot be imported: ${workerProbe.reason}`).toBe(true);
        // The reviewer's sequence: the dispatched build fails, the owner
        // rebuilds, that fails, they rebuild again and it reaches preview and
        // is approved by a reviewer.
        const t = await dispatchedAndFailed();
        const second = await rebuild(t, t.root);
        await setState(second, 'failed', null, 120);
        const third = await rebuild(t, second);
        await setState(third, 'preview', 'approved', 60);
        expect(await isHarbor(third)).toBe(true);
        await expectPublishRefused(t, third);

        runWorkerCleanup(third, t.appName);

        // What the worker removes is its own business and has changed over
        // time (it used to delete the dispatched build here and detach its
        // child). Whatever it left, the owner still cannot publish for free.
        const left = await q<{ id: string }>('SELECT id FROM builds WHERE app_name = $1', [t.appName]);
        expect(left.map((row) => row.id)).toContain(third);
        expect(await isHarbor(third)).toBe(true);
        await expectPublishRefused(t, third);
        // And the trial does not get a second free build out of it.
        const stamp = await trialStamp(t.trialId);
        expect(stamp.dispatched).toBe(true);
        if (stamp.handoff_build_id === null) {
          await expectNoSecondFreeBuild(t);
        } else {
          const before = await buildCount(t.owner.id);
          const again = await t.agent.post('/lighthouse/build').send({});
          expect(again.body).toMatchObject({ alreadyDispatched: true, buildId: t.root });
          expect(await buildCount(t.owner.id)).toBe(before);
        }
      },
      60_000
    );

    it('an admin deletes the dispatched build: its rebuild still needs the subscription, and the trial gets no second free build', async () => {
      const t = await dispatchedAndFailed();
      const child = await rebuild(t, t.root);
      await setState(child, 'preview', 'approved', 60);

      const res = await admin.post(`/admin/builds/${t.root}/delete`).type('form').send({ _csrf: await freshCsrf(admin) });
      expect(res.status).toBe(302);
      // The ancestry is gone in every place the old rule read it from.
      expect(await q('SELECT 1 FROM builds WHERE id = $1', [t.root])).toHaveLength(0);
      expect((await one<{ parent_build_id: string | null }>('SELECT parent_build_id FROM builds WHERE id = $1', [child])).parent_build_id).toBeNull();
      expect(await trialStamp(t.trialId)).toEqual({ handoff_build_id: null, dispatched: true });

      expect(await isHarbor(child)).toBe(true);
      await expectPublishRefused(t, child);
      await expectNoSecondFreeBuild(t);
    });

    it('the ops API deletes the dispatched build: same answer', async () => {
      const t = await dispatchedAndFailed();
      const child = await rebuild(t, t.root);
      await setState(child, 'preview', 'approved', 60);

      const res = await ops('delete', `/builds/${t.root}`);
      expect(res.status).toBe(200);
      expect(await q('SELECT 1 FROM builds WHERE id = $1', [t.root])).toHaveLength(0);
      expect(await trialStamp(t.trialId)).toEqual({ handoff_build_id: null, dispatched: true });

      await expectPublishRefused(t, child);
      await expectNoSecondFreeBuild(t);
    });

    it.each(['admin', 'ops'] as const)(
      'the %s "cleanup duplicates" route deletes the older builds of the lineage: same answer',
      async (cleanup) => {
        const t = await dispatchedAndFailed();
        const second = await rebuild(t, t.root);
        await setState(second, 'failed', null, 120);
        const third = await rebuild(t, second);
        await setState(third, 'preview', 'approved', 60);

        if (cleanup === 'admin') {
          const res = await admin.post('/admin/builds/cleanup').type('form').send({ _csrf: await freshCsrf(admin) });
          expect(res.status).toBe(302);
        } else {
          const res = await ops('post', '/builds/cleanup', { app_name: t.appName });
          expect(res.status).toBe(200);
        }
        expect(await q('SELECT 1 FROM builds WHERE id = $1', [t.root])).toHaveLength(0);
        expect((await trialStamp(t.trialId)).handoff_build_id).toBeNull();

        await expectPublishRefused(t, third);
        await expectNoSecondFreeBuild(t);
      }
    );

    it('once the owner subscribes, the same orphaned rebuild publishes', async () => {
      const t = await dispatchedAndFailed();
      const child = await rebuild(t, t.root);
      await setState(child, 'preview', 'approved', 60);
      await ops('delete', `/builds/${t.root}`);
      await expectPublishRefused(t, child);

      await q(
        `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status, stripe_subscription_id)
         VALUES ($1, 'Haven', 'Haven', 'active', $2)`,
        [t.owner.id, `sub_${run}_lineage`]
      );
      const res = await approve(t.ownerBrowser, child);
      expect(res.status).toBe(302);
      expect((await buildRow(child)).status).toBe('live');
    });

    it('schema.sql marks rows written before the column existed, from the stamps and lineage still there, and then leaves rows alone', async () => {
      const owner = await createCustomer('backfill-owner');
      const trialRoot = await createBuild(owner.id, { harbor: true, status: 'live' });
      const child = await createBuild(owner.id, { parent: trialRoot, status: 'failed' });
      const grandchild = await createBuild(owner.id, { parent: child });
      const keeperRoot = await createBuild(owner.id, { harbor: true, status: 'live' });
      const bystander = await createBuild(owner.id);
      // Stamps without handoff_at, as anything but the application writes them.
      const visitor = await trialBrowser();
      await q('UPDATE trial_keepers SET handoff_build_id = $1 WHERE trial_id = $2', [trialRoot, visitor.trialId]);
      const keeper = await createCustomer('backfill-keeper');
      await q(
        `INSERT INTO keepers (customer_id, companion_name, companion_assigned, handoff_build_id) VALUES ($1, 'Haven', 'Haven', $2)`,
        [keeper.id, keeperRoot]
      );
      // As the code before this column left such rows: Harbor only by a stamp or by descent.
      const legacy = [trialRoot, child, grandchild, keeperRoot];
      await q('UPDATE builds SET is_harbor = FALSE WHERE id = ANY($1::uuid[])', [legacy]);
      const updatedAt = async (id: string) => (await one<{ updated_at: Date }>('SELECT updated_at FROM builds WHERE id = $1', [id])).updated_at;
      const bystanderBefore = await updatedAt(bystander);

      await applySchema(db);

      for (const id of legacy) expect(await isHarbor(id), id).toBe(true);
      expect(await isHarbor(bystander)).toBe(false);
      expect(await updatedAt(bystander)).toEqual(bystanderBefore);
      expect((await trialStamp(visitor.trialId)).dispatched).toBe(true);
      expect((await one<{ at: Date | null }>('SELECT handoff_at AS at FROM keepers WHERE customer_id = $1', [keeper.id])).at).not.toBeNull();

      // Applied again with nothing to do, it writes nothing.
      const before = await updatedAt(trialRoot);
      await applySchema(db);
      expect(await updatedAt(trialRoot)).toEqual(before);
    });
  });

  // -------------------------------------------------------------------------
  describe('the source download follows the publishing rule: GET /builds/:buildId/download-source', () => {
    let owner: { id: string; email: string };
    let ownerBrowser: Browser;
    const ARTIFACT = Buffer.from(`fake-tarball-${run}`);

    beforeAll(async () => {
      owner = await createCustomer('source-owner');
      ownerBrowser = await loggedIn(owner.email);
    });

    async function buildWithArtifact(customerId: string, opts: BuildOpts): Promise<string> {
      const id = await createBuild(customerId, opts);
      await q('UPDATE builds SET build_artifact = $2, live_url = CASE WHEN status = \'live\' THEN preview_url END WHERE id = $1', [id, ARTIFACT]);
      return id;
    }
    async function expectServed(agent: Browser, id: string): Promise<void> {
      const res = await agent.get(`/builds/${id}/download-source`).buffer(true).parse((response, callback) => {
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => chunks.push(chunk));
        response.on('end', () => callback(null, Buffer.concat(chunks)));
      });
      expect(res.status).toBe(200);
      expect(res.headers['content-type']).toContain('application/gzip');
      expect(Buffer.compare(res.body as Buffer, ARTIFACT)).toBe(0);
      // And the status page links to it.
      expect((await agent.get(`/builds/${id}`)).text).toContain(`/builds/${id}/download-source`);
    }
    async function expectRefused(agent: Browser, id: string): Promise<void> {
      const res = await agent.get(`/builds/${id}/download-source`);
      expect(res.status).toBe(403);
      expect(res.headers['content-type']).not.toContain('application/gzip');
      expect(res.text).not.toContain(ARTIFACT.toString());
      // And the status page does not offer it.
      const page = await agent.get(`/builds/${id}`);
      expect(page.status).toBe(200);
      expect(page.text).not.toContain('/download-source');
    }

    it.each([
      ['waiting for review', { review: 'pending' }],
      ['never reviewed', { review: null }],
      ['changes requested', { review: 'changes_requested' }],
      ['queued for automated review', { review: 'ai_review_queued' }],
    ] as Array<[string, BuildOpts]>)('a preview that is %s: refused, whether or not it is a Harbor build', async (_label, state) => {
      await expectRefused(ownerBrowser, await buildWithArtifact(owner.id, state));
      await expectRefused(ownerBrowser, await buildWithArtifact(owner.id, { ...state, harbor: true }));
    });

    it('an approved preview that is not a Harbor build: served (its owner could publish it)', async () => {
      await expectServed(ownerBrowser, await buildWithArtifact(owner.id, {}));
    });

    it('an approved Harbor preview with no subscription: refused, and so is its rebuild', async () => {
      const root = await buildWithArtifact(owner.id, { harbor: true });
      await expectRefused(ownerBrowser, root);
      await expectRefused(ownerBrowser, await buildWithArtifact(owner.id, { parent: root }));
    });

    it.each([
      ['active', true],
      ['past_due', true],
      ['unpaid', false],
      ['canceled', false],
    ] as Array<[string, boolean]>)('an approved Harbor preview whose owner\'s subscription is %s: served = %s', async (subscriptionStatus, served) => {
      const keeper = await createCustomer(`source-keeper-${subscriptionStatus}`);
      await q(
        `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status, stripe_subscription_id)
         VALUES ($1, 'Haven', 'Haven', $2, $3)`,
        [keeper.id, subscriptionStatus, `sub_${run}_source_${subscriptionStatus}`]
      );
      const id = await buildWithArtifact(keeper.id, { harbor: true });
      const agent = await loggedIn(keeper.email);
      if (served) await expectServed(agent, id);
      else await expectRefused(agent, id);
    });

    it('a live build is served, Harbor or not, subscription or not: it was delivered', async () => {
      await expectServed(ownerBrowser, await buildWithArtifact(owner.id, { status: 'live' }));
      await expectServed(ownerBrowser, await buildWithArtifact(owner.id, { status: 'live', harbor: true }));
    });

    it.each(['queued', 'building', 'failed', 'abandoned'])('a %s build is refused even with an approved verdict left on the row', async (status) => {
      const id = await buildWithArtifact(owner.id, { status, review: 'approved' });
      const res = await ownerBrowser.get(`/builds/${id}/download-source`);
      expect(res.status).toBe(403);
      expect(res.headers['content-type']).not.toContain('application/gzip');
    });

    it('someone else\'s build is 404, and a logged-out visitor is sent to log in', async () => {
      const stranger = await createCustomer('source-stranger');
      const theirs = await buildWithArtifact(stranger.id, { status: 'live' });
      expect((await ownerBrowser.get(`/builds/${theirs}/download-source`)).status).toBe(404);
      const anonymous = await request(app).get(`/builds/${theirs}/download-source`);
      expect(anonymous.status).toBe(302);
      expect(anonymous.headers.location).toMatch(/^\/login/);
    });

    it('the admin, who reviews the source, gets a preview before any verdict', async () => {
      const id = await buildWithArtifact(owner.id, { review: 'pending', harbor: true });
      const admin = await loggedIn(ADMIN_EMAIL);
      const res = await admin.get(`/builds/${id}/download-source`);
      expect(res.status).toBe(200);
      expect(res.headers['content-type']).toContain('application/gzip');
    });
  });
  // -------------------------------------------------------------------------
  describe('a review verdict does not outlive the deployment it was given for', () => {
    const ops = (pathname: string, body: Record<string, unknown> = {}) =>
      request(app).post(`/ops/api/v1${pathname}`).set('Authorization', `Bearer ${OPS_KEY}`).send(body);
    const report = (buildId: string, status: string) =>
      request(app).post('/webhooks/build-status').set('Authorization', `Bearer ${WORKER_SECRET}`).send({ build_id: buildId, status });

    it('ops requeue of an approved preview clears the verdict; the rebuilt preview must be reviewed again before the owner can publish', async () => {
      const owner = await createCustomer('requeue-owner');
      const id = await createBuild(owner.id, { reviewedBy: ADMIN_EMAIL });
      expect((await buildRow(id)).review_status).toBe('approved');

      const requeued = await ops(`/builds/${id}/requeue`);
      expect(requeued.status).toBe(200);
      let row = await buildRow(id);
      expect(row).toMatchObject({ status: 'queued', review_status: null, reviewed_at: null, reviewed_by: null, review_notes: null });

      // The pipeline runs again and reports the new deployment.
      expect((await report(id, 'building')).status).toBe(200);
      expect((await report(id, 'preview')).status).toBe(200);
      row = await buildRow(id);
      expect(row).toMatchObject({ status: 'preview', review_status: 'pending', reviewed_by: null });

      const agent = await loggedIn(owner.email);
      const res = await agent.post(`/builds/${id}/approve`).type('form').send({ _csrf: await freshCsrf(agent) });
      expect(res.status).toBe(403);
      expect((await buildRow(id)).status).toBe('preview');
    });

    it('ops requeue-from-stage and the owner\'s retry of a failed build clear a stale verdict too', async () => {
      const owner = await createCustomer('retry-owner');
      const viaOps = await createBuild(owner.id, { status: 'failed', review: 'approved', reviewedBy: ADMIN_EMAIL });
      await q(`UPDATE builds SET build_artifact = 'x'::bytea WHERE id = $1`, [viaOps]);
      expect((await ops(`/builds/${viaOps}/requeue-from-stage`, { stage: 'deploying' })).status).toBe(200);
      expect(await buildRow(viaOps)).toMatchObject({ status: 'queued', review_status: null, reviewed_by: null, review_notes: null });

      const viaRetry = await createBuild(owner.id, { status: 'failed', review: 'approved', reviewedBy: ADMIN_EMAIL });
      const agent = await loggedIn(owner.email);
      const res = await agent.post(`/builds/${viaRetry}/retry`).type('form').send({ _csrf: await freshCsrf(agent) });
      expect(res.status).toBe(302);
      expect(await buildRow(viaRetry)).toMatchObject({ status: 'queued', review_status: null, reviewed_by: null, review_notes: null });
    });

    describe('an admin puts a failed build back in the queue', () => {
      const reviewColumns = (id: string) =>
        one<{
          status: string;
          error_message: string | null;
          rebuild_request: string | null;
          review_status: string | null;
          reviewed_at: Date | null;
          reviewed_by: string | null;
          review_notes: string | null;
          review_report: unknown;
          ai_review_iterations: number | null;
          ai_reviewed_at: Date | null;
        }>(
          `SELECT status, error_message, rebuild_request, review_status, reviewed_at, reviewed_by, review_notes,
                  review_report, ai_review_iterations, ai_reviewed_at FROM builds WHERE id = $1`,
          [id]
        );
      /** A failed build whose row still carries a complete verdict: the verdict fields and the review report columns. */
      async function failedWithVerdict(label: string): Promise<string> {
        const owner = await createCustomer(label);
        const id = await createBuild(owner.id, { status: 'failed', review: 'approved', reviewedBy: ADMIN_EMAIL });
        await q(
          `UPDATE builds SET review_report = '{"decision":"approve"}'::jsonb, ai_review_iterations = 2, ai_reviewed_at = NOW(),
                  error_message = 'deploy crashed' WHERE id = $1`,
          [id]
        );
        const before = await reviewColumns(id);
        expect(before).toMatchObject({ status: 'failed', review_status: 'approved', reviewed_by: ADMIN_EMAIL, review_notes: 'looked fine', ai_review_iterations: 2 });
        expect(before.reviewed_at).not.toBeNull();
        expect(before.review_report).not.toBeNull();
        expect(before.ai_reviewed_at).not.toBeNull();
        return id;
      }
      const verdictCleared = {
        status: 'queued',
        error_message: null,
        review_status: null,
        reviewed_at: null,
        reviewed_by: null,
        review_notes: null,
        review_report: null,
        ai_review_iterations: 0,
        ai_reviewed_at: null,
      };

      it('POST /admin/builds/:id/reset leaves the row queued with the verdict and the review report cleared', async () => {
        const id = await failedWithVerdict('admin-reset-owner');
        const admin = await loggedIn(ADMIN_EMAIL);
        const res = await admin.post(`/admin/builds/${id}/reset`).type('form').send({ _csrf: await freshCsrf(admin) });
        expect(res.status).toBe(302);
        expect(await reviewColumns(id)).toMatchObject(verdictCleared);
      });

      it('POST /admin/builds/:id/ai-fix on a failed build resets it in place and clears the verdict too', async () => {
        const id = await failedWithVerdict('admin-aifix-owner');
        const admin = await loggedIn(ADMIN_EMAIL);
        const res = await admin
          .post(`/admin/builds/${id}/ai-fix`)
          .type('form')
          .send({ _csrf: await freshCsrf(admin), rebuild_request: 'make the footer match the header', force_new: '' });
        expect(res.status).toBe(302);
        expect(res.headers.location).toBe(`/admin/builds/${id}`);
        expect(await reviewColumns(id)).toMatchObject({ ...verdictCleared, rebuild_request: 'make the footer match the header' });
      });

      it('a reset does not touch a build that is not failed: it keeps its verdict and answers 404', async () => {
        const owner = await createCustomer('admin-reset-preview-owner');
        const id = await createBuild(owner.id, { reviewedBy: ADMIN_EMAIL });
        const admin = await loggedIn(ADMIN_EMAIL);
        const res = await admin.post(`/admin/builds/${id}/reset`).type('form').send({ _csrf: await freshCsrf(admin) });
        expect(res.status).toBe(404);
        expect(await reviewColumns(id)).toMatchObject({ status: 'preview', review_status: 'approved', reviewed_by: ADMIN_EMAIL });
      });
    });

    it('requeue-from-stage does not pull back a build that is no longer failed', async () => {
      const owner = await createCustomer('stage-owner');
      const id = await createBuild(owner.id, { status: 'building', review: null });
      const res = await ops(`/builds/${id}/requeue-from-stage`, { stage: 'deploying' });
      expect(res.status).toBe(400);
      expect((await buildRow(id)).status).toBe('building');
    });
  });

  // -------------------------------------------------------------------------
  describe('Stripe: a trial visitor pays, and lands as the owner', () => {
    it('one signed checkout.session.completed takes the build live, releases the trial and records everything, together', async () => {
      const t = await trialWithPreview('land');
      const before = await t.agent.get(`/lighthouse/lamp/${t.buildId}/published`);
      expect(before.status).toBe(200);
      expect(before.text).toContain('Hold on');

      const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'land' }));
      const res = await deliver(event);
      expect(res.status).toBe(200);
      expect(res.body).toEqual({ received: true });

      const build = await buildRow(t.buildId);
      expect(build).toMatchObject({ status: 'live', live_url: build.preview_url, stripe_session_id: `cs_${run}_land`, customer_id: t.payer.id });
      const trial = await one<{ released: boolean; days_left: string }>(
        `SELECT released_at IS NOT NULL AS released, EXTRACT(EPOCH FROM (expires_at - NOW())) / 86400 AS days_left
         FROM trial_keepers WHERE trial_id = $1`,
        [t.trialId]
      );
      expect(trial.released).toBe(true);
      expect(Number(trial.days_left)).toBeGreaterThan(29);
      expect(await keeperRow(t.payer.id)).toMatchObject({
        subscription_status: 'active',
        stripe_subscription_id: `sub_${run}_land`,
        handoff_build_id: t.buildId,
      });
      expect(await subscriptionRow('land')).toMatchObject({ status: 'active', keeper_customer_id: t.payer.id, monthly_amount_cents: 2900 });
      const turns = await q<{ surface: string; keeper_text: string | null }>(
        'SELECT surface, keeper_text FROM companion_turns WHERE keeper_id = $1 ORDER BY created_at',
        [t.payer.id]
      );
      expect(turns.map((turn) => turn.surface).sort()).toEqual(['harbor-blessing', 'table']);
      expect((await q('SELECT 1 FROM companion_turns WHERE trial_id = $1', [t.trialId])).length).toBe(0);
      expect(await claimed(event.id)).toBe(1);

      // The payer, with the cookie they had all along:
      const published = await t.agent.get(`/lighthouse/lamp/${t.buildId}/published`);
      expect(published.status).toBe(200);
      expect(published.text).toContain('Your website is public.');
      const lamp = await t.agent.get(`/lighthouse/lamp/${t.buildId}`);
      expect(lamp.status).toBe(200);
      expect(lamp.text).toContain('Cancel subscription');
      const table = await t.agent.get('/lighthouse/table');
      expect(table.text).toContain('Tell me more.');
      expect(table.text).toContain('Your site is live');
      // The cookie was not swapped for a new trial.
      const stillTheirs = await one<{ n: string }>('SELECT COUNT(*) AS n FROM trial_keepers WHERE cookie_token = $1', [t.token]);
      expect(Number(stillTheirs.n)).toBe(1);

      // Someone who only knows the address:
      const stranger = await trialBrowser();
      for (const url of [`/lighthouse/lamp/${t.buildId}/published?session_id=cs_${run}_land`, `/lighthouse/lamp/${t.buildId}`]) {
        expect((await stranger.agent.get(url)).status).toBe(404);
        expect((await request(app).get(url)).status).toBe(404);
      }
      const cancelAttempt = await stranger.agent
        .post(`/lighthouse/lamp/${t.buildId}/cancel-subscription`)
        .type('form')
        .send({ _csrf: await freshCsrf(stranger.agent) });
      expect(cancelAttempt.status).toBe(404);

      // A second delivery of the same event changes nothing.
      const replay = await deliver(event);
      expect(replay.body).toEqual({ received: true, duplicate: true });
      expect((await q('SELECT 1 FROM harbor_subscriptions WHERE keeper_customer_id = $1', [t.payer.id])).length).toBe(1);
      expect((await q(`SELECT 1 FROM companion_turns WHERE keeper_id = $1 AND surface = 'harbor-blessing'`, [t.payer.id])).length).toBe(1);
    });

    it('the payer\'s cancel request is authorized (checked up to the point where Stripe would be called)', async () => {
      const t = await trialWithPreview('cancel');
      await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'cancel' })));
      // With no subscription id on the keeper row the route stops at 409 after
      // it has accepted the caller as the keeper, and before any call to Stripe.
      await q('UPDATE keepers SET stripe_subscription_id = NULL WHERE customer_id = $1', [t.payer.id]);
      const res = await t.agent
        .post(`/lighthouse/lamp/${t.buildId}/cancel-subscription`)
        .type('form')
        .send({ _csrf: await freshCsrf(t.agent) });
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('no_active_subscription');
    });

    it('an event that fails part-way leaves nothing behind, not even its claim, and is applied in full when it is delivered again', async () => {
      const t = await trialWithPreview('atomic');
      const session = checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'atomic', amount: '99999999999' });
      const event = stripeEvent('checkout.session.completed', session);

      // The amount overflows an INTEGER column in the middle of the promotion,
      // after the keeper row has been written in the same transaction.
      const failed = await deliver(event);
      expect(failed.status).toBe(500);
      expect(await claimed(event.id)).toBe(0);
      expect((await buildRow(t.buildId)).status).toBe('preview');
      expect((await q('SELECT 1 FROM trial_keepers WHERE trial_id = $1 AND released_at IS NOT NULL', [t.trialId])).length).toBe(0);
      expect((await q('SELECT 1 FROM keepers WHERE customer_id = $1', [t.payer.id])).length).toBe(0);
      expect((await q('SELECT 1 FROM companion_turns WHERE trial_id = $1', [t.trialId])).length).toBe(1);
      // The payer still owns the preview through the trial.
      expect((await t.agent.get(`/lighthouse/lamp/${t.buildId}`)).status).toBe(200);

      // Same event id, deliverable this time: processed, not skipped as a duplicate.
      const retry = await deliver({ ...event, data: { object: { ...session, metadata: { ...session.metadata, monthly_amount_cents: '2900' } } } });
      expect(retry.status).toBe(200);
      expect(retry.body).toEqual({ received: true });
      expect((await buildRow(t.buildId)).status).toBe('live');
      expect(await claimed(event.id)).toBe(1);
    });

    it.each([
      ['changes requested', { review: 'changes_requested' }],
      ['waiting for review', { review: 'pending' }],
      ['requeued', { status: 'queued', review: null }],
    ] as Array<[string, BuildOpts]>)('a paid session for a build that is %s: not published, trial not released, the charge recorded', async (label, state) => {
      const tag = `np${label.replace(/\W/g, '').slice(0, 6)}`;
      const t = await trialWithPreview(tag, state);
      const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag }));

      expect((await deliver(event)).status).toBe(200);
      const build = await buildRow(t.buildId);
      expect(build.status).toBe(state.status ?? 'preview');
      expect(build.live_url).toBeNull();
      expect((await q('SELECT 1 FROM trial_keepers WHERE trial_id = $1 AND released_at IS NULL', [t.trialId])).length).toBe(1);
      expect((await q('SELECT 1 FROM keepers WHERE customer_id = $1', [t.payer.id])).length).toBe(0);
      expect(await subscriptionRow(tag)).toMatchObject({ status: 'active', keeper_customer_id: t.payer.id });
      expect(await claimed(event.id)).toBe(1);
    });

    it('a session that is not paid changes nothing', async () => {
      const t = await trialWithPreview('unpaid');
      const res = await deliver(
        stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'unpaid', paymentStatus: 'unpaid' }))
      );
      expect(res.status).toBe(200);
      expect((await buildRow(t.buildId)).status).toBe('preview');
      expect((await q('SELECT 1 FROM harbor_subscriptions WHERE stripe_subscription_id = $1', [`sub_${run}_unpaid`])).length).toBe(0);
    });

    it('an event with a bad signature is refused before anything is read', async () => {
      const t = await trialWithPreview('badsig');
      const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'badsig' }));
      const res = await request(app)
        .post('/webhooks/stripe')
        .set('Content-Type', 'application/json')
        .set('Stripe-Signature', `t=${Math.floor(Date.now() / 1000)},v1=${'0'.repeat(64)}`)
        .send(JSON.stringify(event));
      expect(res.status).toBe(400);
      expect((await buildRow(t.buildId)).status).toBe('preview');
      expect(await claimed(event.id)).toBe(0);
    });

    it('the account a payer ends up in: a password reset both sets the password and verifies it, so they can log in and manage', async () => {
      const t = await trialWithPreview('durable');
      await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'durable' })));
      expect((await one<{ email_verified: boolean }>('SELECT email_verified FROM customers WHERE id = $1', [t.payer.id])).email_verified).toBe(false);

      const agent = browser();
      await agent.post('/forgot-password').type('form').send({ _csrf: await freshCsrf(agent), email: t.payer.email });
      await vi.waitFor(() => expect(latestDevLink('Password reset link')).toContain('/reset-password?token='));
      const link = latestDevLink('Password reset link');
      const token = new URL(link, 'http://localhost').searchParams.get('token') as string;
      const newPassword = `new-${crypto.randomBytes(9).toString('hex')}`;
      const set = await agent.post('/reset-password').type('form').send({ _csrf: await freshCsrf(agent), token, password: newPassword });
      expect(set.text).toContain('Your password is set');
      expect((await one<{ email_verified: boolean }>('SELECT email_verified FROM customers WHERE id = $1', [t.payer.id])).email_verified).toBe(true);

      const elsewhere = browser();
      const login = await elsewhere.post('/login').type('form').send({ _csrf: await freshCsrf(elsewhere), email: t.payer.email, password: newPassword });
      expect(login.status).toBe(302);
      const lamp = await elsewhere.get(`/lighthouse/lamp/${t.buildId}`);
      expect(lamp.status).toBe(200);
      expect(lamp.text).toContain('Cancel subscription');
    });
  });

  // -------------------------------------------------------------------------
  describe('Stripe: subscription events', () => {
    /** A paid, live keeper to send subscription events about. */
    async function paidKeeper(tag: string) {
      const t = await trialWithPreview(tag);
      const res = await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag })));
      expect(res.status).toBe(200);
      return t;
    }

    it('updated: status, cancel flag and period land in the audit row and the keeper row', async () => {
      const t = await paidKeeper('upd');
      const res = await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('upd', { status: 'past_due', cancel_at_period_end: true })));
      expect(res.status).toBe(200);
      expect(await subscriptionRow('upd')).toMatchObject({ status: 'past_due', cancel_at_period_end: true });
      expect((await keeperRow(t.payer.id)).subscription_status).toBe('past_due');
    });

    it('deleted is final: a late or redelivered "active" does not bring the subscription back', async () => {
      const t = await paidKeeper('term');
      const now = Math.floor(Date.now() / 1000);
      expect((await deliver(stripeEvent('customer.subscription.deleted', subscriptionObject('term', { status: 'canceled' }), now))).status).toBe(200);
      expect((await subscriptionRow('term')).status).toBe('canceled');
      expect((await keeperRow(t.payer.id)).subscription_status).toBe('canceled');

      // Older than the delete, and newer than it: neither may reactivate.
      for (const created of [now - 600, now + 600]) {
        const res = await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('term', { status: 'active' }), created));
        expect(res.status).toBe(200);
        expect((await subscriptionRow('term')).status).toBe('canceled');
        expect((await keeperRow(t.payer.id)).subscription_status).toBe('canceled');
      }
      // Nor does a failed invoice move it to past_due.
      await deliver(stripeEvent('invoice.payment_failed', { id: `in_${run}_term`, subscription: `sub_${run}_term` }, now + 700));
      expect((await subscriptionRow('term')).status).toBe('canceled');
      // The website stays up.
      expect((await buildRow(t.buildId)).status).toBe('live');
    });

    it('invoice.payment_failed moves both rows to past_due in one step', async () => {
      const t = await paidKeeper('inv');
      const res = await deliver(stripeEvent('invoice.payment_failed', { id: `in_${run}_inv`, subscription: `sub_${run}_inv` }));
      expect(res.status).toBe(200);
      expect((await subscriptionRow('inv')).status).toBe('past_due');
      expect((await keeperRow(t.payer.id)).subscription_status).toBe('past_due');
    });

    it('an event for a subscription the keeper has since replaced updates its audit row but not the keeper', async () => {
      const t = await paidKeeper('old');
      await q(`UPDATE keepers SET stripe_subscription_id = $1, subscription_status = 'active' WHERE customer_id = $2`, [`sub_${run}_newer`, t.payer.id]);
      const res = await deliver(stripeEvent('customer.subscription.deleted', subscriptionObject('old', { status: 'canceled' })));
      expect(res.status).toBe(200);
      expect((await subscriptionRow('old')).status).toBe('canceled');
      expect(await keeperRow(t.payer.id)).toMatchObject({ subscription_status: 'active', stripe_subscription_id: `sub_${run}_newer` });
    });

    it('an event that overtakes its checkout is refused (500, no claim) and applies once the checkout has been recorded', async () => {
      const t = await trialWithPreview('early');
      const early = stripeEvent('customer.subscription.updated', subscriptionObject('early', { status: 'past_due' }), Math.floor(Date.now() / 1000) + 5);

      const refused = await deliver(early);
      expect(refused.status).toBe(500);
      expect(await claimed(early.id)).toBe(0);

      await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'early' })));
      const redelivered = await deliver(early);
      expect(redelivered.status).toBe(200);
      expect(redelivered.body).toEqual({ received: true });
      expect((await subscriptionRow('early')).status).toBe('past_due');
      expect((await keeperRow(t.payer.id)).subscription_status).toBe('past_due');
    });

    it('an event for an unknown subscription is refused, so Stripe retries it, while it is under 72 hours old', async () => {
      // A checkout that Stripe retries for about three days may still create the row.
      const young = stripeEvent('customer.subscription.updated', subscriptionObject('never-recorded-30h'), Math.floor(Date.now() / 1000) - 30 * 3600);
      const refused = await deliver(young);
      expect(refused.status).toBe(500);
      expect(await claimed(young.id)).toBe(0);
    });

    it('an event for an unknown subscription that is more than 72 hours old is acknowledged and dropped', async () => {
      const stale = stripeEvent('customer.subscription.updated', subscriptionObject('never-recorded'), Math.floor(Date.now() / 1000) - 80 * 3600);
      const res = await deliver(stale);
      expect(res.status).toBe(200);
      expect(await claimed(stale.id)).toBe(1);
    });

    it('a subscription that is not Harbor\'s is acknowledged untouched', async () => {
      const res = await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('foreign', { metadata: {} })));
      expect(res.status).toBe(200);
    });

    it('order: an event older than the state already applied is skipped', async () => {
      // The handlers detect the column at runtime and, without it, apply the
      // terminal-state rule only. schema.sql must create it, so its absence is
      // a failure here, not a skipped test.
      expect(orderingColumn, 'schema.sql adds harbor_subscriptions.last_event_created').toBe(true);
      resetOrderingCache();
      const t = await paidKeeper('order');
      const now = Math.floor(Date.now() / 1000);

      // The newer event (recovered: active) arrives first.
      await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('order', { status: 'active' }), now + 100));
      // The older one (past_due) is a retried delivery that arrives late.
      const late = await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('order', { status: 'past_due' }), now + 50));
      expect(late.status).toBe(200);
      expect((await subscriptionRow('order')).status).toBe('active');
      expect((await keeperRow(t.payer.id)).subscription_status).toBe('active');
      // So is a late failed-invoice event.
      await deliver(stripeEvent('invoice.payment_failed', { id: `in_${run}_order`, subscription: `sub_${run}_order` }, now + 60));
      expect((await subscriptionRow('order')).status).toBe('active');
      // An event newer than both does apply.
      await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('order', { status: 'past_due' }), now + 200));
      expect((await subscriptionRow('order')).status).toBe('past_due');
      const marker = await one<{ last_event_created: string }>(
        'SELECT last_event_created FROM harbor_subscriptions WHERE stripe_subscription_id = $1',
        [`sub_${run}_order`]
      );
      expect(Number(marker.last_event_created)).toBe(now + 200);
    });

    // What each Stripe status is worth, end to end: the event arrives at the
    // real webhook, lands on the keeper row, and the keeper then tries to
    // publish a revision of the website they paid for.
    describe('each Stripe subscription status, from the webhook to the publish gate', () => {
      /** The paid keeper, logged in to the account, with a reviewed revision of their Harbor website waiting. */
      async function keeperWithRevision(tag: string) {
        const t = await paidKeeper(tag);
        await q('UPDATE customers SET email_verified = TRUE WHERE id = $1', [t.payer.id]);
        const revision = await createBuild(t.payer.id, { parent: t.buildId });
        expect(await isHarbor(revision)).toBe(true);
        return { ...t, revision, keeperBrowser: await loggedIn(t.payer.email) };
      }
      const publish = async (agent: Browser, buildId: string) =>
        agent.post(`/builds/${buildId}/approve`).type('form').send({ _csrf: await freshCsrf(agent) });

      it.each([
        ['active', true],
        ['trialing', true],
        ['past_due', true],
        ['unpaid', false],
        ['paused', false],
        ['incomplete', false],
        ['incomplete_expired', false],
        ['canceled', false],
      ] as Array<[string, boolean]>)('%s: the keeper row says so, and the revision publishes = %s', async (status, publishes) => {
        const tag = `st${status.replace(/_/g, '')}`;
        const t = await keeperWithRevision(tag);
        const res = await deliver(stripeEvent('customer.subscription.updated', subscriptionObject(tag, { status })));
        expect(res.status).toBe(200);
        expect((await subscriptionRow(tag)).status).toBe(status);
        expect((await keeperRow(t.payer.id)).subscription_status).toBe(status);

        const attempt = await publish(t.keeperBrowser, t.revision);
        if (publishes) {
          expect(attempt.status).toBe(302);
          expect((await buildRow(t.revision)).status).toBe('live');
        } else {
          expect(attempt.status).toBe(303);
          expect(attempt.headers.location).toBe(`/lighthouse/lamp/${t.revision}/make-public`);
          expect((await buildRow(t.revision)).status).toBe('preview');
        }
      });

      it('a status this code has never heard of is stored as incomplete on the keeper row and does not publish', async () => {
        const t = await keeperWithRevision('stfuture');
        await deliver(stripeEvent('customer.subscription.updated', subscriptionObject('stfuture', { status: 'some_future_state' })));
        expect((await subscriptionRow('stfuture')).status).toBe('some_future_state');
        expect((await keeperRow(t.payer.id)).subscription_status).toBe('incomplete');
        expect((await publish(t.keeperBrowser, t.revision)).status).toBe(303);
        expect((await buildRow(t.revision)).status).toBe('preview');
      });

      it.each(['unpaid', 'incomplete', 'paused'])(
        'a failed invoice does not lift a subscription that is %s to past_due, which would publish',
        async (status) => {
          const tag = `inv${status}`;
          const t = await keeperWithRevision(tag);
          const now = Math.floor(Date.now() / 1000);
          await deliver(stripeEvent('customer.subscription.updated', subscriptionObject(tag, { status }), now + 10));
          // Newer than the status event, so the ordering rule alone would let it apply.
          const res = await deliver(stripeEvent('invoice.payment_failed', { id: `in_${run}_${tag}`, subscription: `sub_${run}_${tag}` }, now + 20));
          expect(res.status).toBe(200);
          expect((await subscriptionRow(tag)).status).toBe(status);
          expect((await keeperRow(t.payer.id)).subscription_status).toBe(status);
          expect((await publish(t.keeperBrowser, t.revision)).status).toBe(303);
          expect((await buildRow(t.revision)).status).toBe('preview');
        }
      );

      it('a keeper who already has a subscription that publishes is not sent to a second Checkout', async () => {
        const t = await keeperWithRevision('nodouble');
        const res = await t.keeperBrowser
          .post(`/lighthouse/lamp/${t.revision}/make-public`)
          .type('form')
          .send({ _csrf: await freshCsrf(t.keeperBrowser), monthly_amount_cents: '3900', email: t.payer.email });
        // Back to the status page, where "Approve and go live" publishes on the subscription they have.
        expect(res.status).toBe(303);
        expect(res.headers.location).toBe(`/builds/${t.revision}`);
        expect((await q('SELECT 1 FROM harbor_subscriptions WHERE keeper_customer_id = $1', [t.payer.id])).length).toBe(1);
      });
    });
  });

  // -------------------------------------------------------------------------
  describe('account deletion: POST /account/delete', () => {
    // Subscription ids here carry the "sub_bypass_" prefix of the development
    // payment bypass, which the route never sends to Stripe, or are already
    // canceled, so no request leaves the process.
    /** Every table of the schema this test database holds. */
    const everyTable = async (): Promise<string[]> =>
      (
        await q<{ name: string }>(
          `SELECT table_name AS name FROM information_schema.tables
           WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY 1`
        )
      ).map((row) => row.name);
    /** For each table that mentions `needle` in any column of any row (case ignored), how many rows do. */
    async function tablesMentioning(needle: string): Promise<Record<string, number>> {
      const found: Record<string, number> = {};
      for (const name of await everyTable()) {
        const hit = await one<{ n: string }>(
          `SELECT COUNT(*) AS n FROM "${name}" AS r WHERE POSITION(LOWER($1) IN LOWER(r::text)) > 0`,
          [needle]
        );
        if (Number(hit.n) > 0) found[name] = Number(hit.n);
      }
      return found;
    }
    /** A Haven trial row, and (with `says`) one turn of its conversation and a note taken from it. */
    async function havenTrial(fields: { address?: string; build?: string; released?: boolean; says?: string }): Promise<string> {
      const row = await one<{ trial_id: string }>(
        `INSERT INTO trial_keepers (cookie_token, companion_name, companion_assigned, expires_at, email_captured,
                                    handoff_build_id, handoff_at, released_at)
         VALUES ($1, 'Haven', 'Haven', NOW() + INTERVAL '7 days', $2, $3::uuid,
                 CASE WHEN $3::uuid IS NULL THEN NULL ELSE NOW() END, CASE WHEN $4::boolean THEN NOW() ELSE NULL END)
         RETURNING trial_id`,
        [crypto.randomBytes(16).toString('hex'), fields.address ?? null, fields.build ?? null, fields.released ?? false]
      );
      if (fields.says) {
        const turn = await one<{ turn_id: string }>(
          `INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, $2, 'Tell me more.') RETURNING turn_id`,
          [row.trial_id, fields.says]
        );
        await q(
          `INSERT INTO keeper_memory (trial_id, partition, category, content, source_turn_id) VALUES ($1, 'working', 'fact', $2, $3)`,
          [row.trial_id, fields.says, turn.turn_id]
        );
      }
      return row.trial_id;
    }

    it('removes the account and its Haven trials from every table, and leaves a tombstone for each subscription so the events Stripe still sends for them are acknowledged', async () => {
      const account = await createCustomer('leaver');
      const live = `sub_bypass_${run}_leaver`;
      const ended = `sub_${run}_leaver_ended`;
      await q(
        `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status, stripe_subscription_id)
         VALUES ($1, 'Haven', 'Haven', 'active', $2)`,
        [account.id, live]
      );
      await q(
        `INSERT INTO harbor_subscriptions (keeper_customer_id, stripe_subscription_id, stripe_customer_id, status)
         VALUES ($1, $2, 'cus_bypass_leaver', 'active'), ($1, $3, 'cus_bypass_leaver', 'canceled')`,
        [account.id, live, ended]
      );
      const waiting = await createBuild(account.id, { status: 'queued', review: null });
      const published = await createBuild(account.id, { status: 'live', harbor: true });

      // The conversations around this account. `said` marks what must be gone
      // afterwards and `kept` what belongs to someone else.
      const said = `What the leaver said ${run}`;
      const kept = `What someone else said ${run}`;
      const neighbor = await createCustomer('leaver-neighbor');
      const neighborBuild = await createBuild(neighbor.id, { status: 'live', harbor: true });
      const neighborPreview = await createBuild(neighbor.id, { status: 'preview', harbor: true });
      await q(
        `INSERT INTO keepers (customer_id, companion_name, companion_assigned, subscription_status, handoff_build_id)
         VALUES ($1, 'Haven', 'Haven', 'active', $2)`,
        [neighbor.id, neighborBuild]
      );
      // The keeper's own conversation, which cascades from the customer row.
      await q(`INSERT INTO companion_turns (keeper_id, keeper_text, companion_text) VALUES ($1, $2, 'Tell me more.')`, [account.id, said]);
      await q(`INSERT INTO keeper_memory (keeper_id, partition, category, content) VALUES ($1, 'working', 'fact', $2)`, [account.id, said]);
      const typedDifferently = `  ${account.email.toUpperCase()} `;
      const otherAddress = email('leaver-other-address');
      const theirs = {
        // The address only, typed in another case and padded, and never dispatched.
        typed: await havenTrial({ address: typedDifferently, says: said }),
        // Stamped with one of the account's builds, under an address typed earlier.
        stamped: await havenTrial({ address: otherAddress, build: waiting, says: said }),
        // The trial that paid: released, its conversation already the keeper's.
        paid: await havenTrial({ address: account.email, build: published, released: true }),
        // Released with no build (its week ran out).
        lapsed: await havenTrial({ address: account.email, released: true }),
      };
      const others = {
        // The address was typed here once, but the build is another account's: unpaid, and paid.
        neighborPreview: await havenTrial({ address: account.email, build: neighborPreview, says: kept }),
        neighborPaid: await havenTrial({ address: typedDifferently, build: neighborBuild, released: true }),
        unrelated: await havenTrial({ address: email('leaver-unrelated'), says: kept }),
        anonymous: await havenTrial({ says: kept }),
      };
      // The scan below reads every table the schema creates (39 of them when this was written).
      const scanned = await everyTable();
      const declared = [...schema.matchAll(/CREATE TABLE IF NOT EXISTS "?(\w+)"?/g)].map((m) => m[1]);
      expect(declared.length).toBeGreaterThanOrEqual(39);
      expect(scanned).toEqual(expect.arrayContaining(declared));
      expect(await tablesMentioning(account.email)).toEqual({ customers: 1, trial_keepers: 5 });
      expect(await tablesMentioning(said)).toEqual({ companion_turns: 3, keeper_memory: 3 });

      const agent = await loggedIn(account.email);
      const res = await agent.post('/account/delete').type('form').send({ _csrf: await freshCsrf(agent), password: PASSWORD });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/?deleted=1');

      // Nothing in any table still carries the address or what was said.
      expect(await tablesMentioning(account.email)).toEqual({});
      expect(await tablesMentioning(said)).toEqual({});
      // The account's trials are gone. The others are all still there: the two
      // that belong to another account's build have lost the address, and the
      // paying one still leads to its keeper.
      const left = await q<{ trial_id: string; email_captured: string | null; handoff_build_id: string | null }>(
        'SELECT trial_id, email_captured, handoff_build_id FROM trial_keepers WHERE trial_id = ANY($1)',
        [[...Object.values(theirs), ...Object.values(others)]]
      );
      expect(Object.fromEntries(left.map((row) => [row.trial_id, [row.email_captured, row.handoff_build_id]]))).toEqual({
        [others.neighborPreview]: [null, neighborPreview],
        [others.neighborPaid]: [null, neighborBuild],
        [others.unrelated]: [email('leaver-unrelated'), null],
        [others.anonymous]: [null, null],
      });
      expect(await tablesMentioning(kept)).toEqual({ companion_turns: 3, keeper_memory: 3 });
      expect(await q('SELECT 1 FROM keepers WHERE customer_id = $1', [neighbor.id])).toHaveLength(1);

      expect(await q('SELECT 1 FROM customers WHERE id = $1', [account.id])).toHaveLength(0);
      expect(await q('SELECT 1 FROM harbor_subscriptions WHERE stripe_subscription_id = ANY($1)', [[live, ended]])).toHaveLength(0);
      const tombstones = await q<{ stripe_subscription_id: string }>(
        'SELECT stripe_subscription_id FROM harbor_subscription_tombstones WHERE stripe_subscription_id = ANY($1) ORDER BY 1',
        [[live, ended]]
      );
      expect(tombstones.map((row) => row.stripe_subscription_id)).toEqual([live, ended].sort());

      // The build nobody could ever claim is closed out; the published one is only unlinked.
      expect(await one('SELECT status, customer_id, error_message FROM builds WHERE id = $1', [waiting])).toMatchObject({
        status: 'failed',
        customer_id: null,
        error_message: 'The account was deleted before this build started.',
      });
      expect(await one('SELECT status, customer_id, is_harbor FROM builds WHERE id = $1', [published])).toMatchObject({
        status: 'live',
        customer_id: null,
        is_harbor: true,
      });

      // Stripe reports the cancellation and, later, the end of the subscription.
      for (const type of ['customer.subscription.updated', 'customer.subscription.deleted']) {
        const event = stripeEvent(type, { ...subscriptionObject('unused'), id: live, status: 'canceled' });
        const delivered = await deliver(event);
        expect(delivered.status, type).toBe(200);
        expect(delivered.body).toEqual({ received: true });
        expect(await claimed(event.id)).toBe(1);
      }
      // An event for a subscription nobody ever recorded is still refused, so that Stripe retries it.
      const stranger = await deliver(stripeEvent('customer.subscription.deleted', { ...subscriptionObject('unused'), id: `sub_${run}_never_seen` }));
      expect(stranger.status).toBe(500);
    });

    it('what the privacy page says deletion leaves is what it leaves: the build with its specification and logs, the email log, a newsletter address, the page views', async () => {
      const account = await createCustomer('leaves');
      const worked = `teaches breathwork in Leeds ${run}`;
      // A build whose stored specification carries what the customer said and
      // their address, as a dispatched build's does (admin_email).
      const buildId = await createBuild(account.id, { status: 'live', review: 'approved' });
      await q(`UPDATE builds SET spec = $2::jsonb WHERE id = $1`, [buildId, JSON.stringify({ admin_email: account.email, about: worked })]);
      await q(`INSERT INTO build_logs (build_id, stage, message) VALUES ($1, 'deploy', $2)`, [buildId, `deployed the site that ${worked}`]);
      // What deletion removes: one row in each kind the page names.
      await q(`INSERT INTO intake_messages (build_id, role, content) VALUES ($1, 'user', $2)`, [buildId, `chat: ${worked}`]);
      const feature = await one<{ id: string }>(
        `INSERT INTO feature_requests (build_id, customer_id, description) VALUES ($1, $2, $3) RETURNING id`,
        [buildId, account.id, `feature: ${worked}`]
      );
      await q(`INSERT INTO feature_messages (feature_request_id, role, content) VALUES ($1, 'user', $2)`, [feature.id, `feature chat: ${worked}`]);
      await q(`INSERT INTO beta_feedback (customer_id, category, message) VALUES ($1, 'other', $2)`, [account.id, `feedback: ${worked}`]);
      await q(`INSERT INTO email_schedule (customer_id, template, send_at, context) VALUES ($1, 'welcome_how_it_works', NOW() + INTERVAL '1 day', $2::jsonb)`, [
        account.id,
        JSON.stringify({ note: worked }),
      ]);
      await q(`INSERT INTO keepers (customer_id, companion_name, companion_assigned) VALUES ($1, 'Haven', 'Haven')`, [account.id]);
      await q(`INSERT INTO companion_turns (keeper_id, keeper_text, companion_text) VALUES ($1, $2, 'Tell me more.')`, [account.id, `said: ${worked}`]);
      await q(`INSERT INTO keeper_memory (keeper_id, partition, category, content) VALUES ($1, 'working', 'fact', $2)`, [account.id, `note: ${worked}`]);
      // What it leaves, keyed by address or by a link the foreign key clears.
      await q(`INSERT INTO email_log (template, to_email, subject, status) VALUES ('preview_ready', $1, 'Your website is ready for review', 'skipped')`, [account.email]);
      await q(`INSERT INTO newsletter_subscribers (email, source) VALUES ($1, 'guide')`, [account.email]);
      const view = await one<{ id: string }>(`INSERT INTO page_views (path, customer_id) VALUES ($1, $2) RETURNING id`, [`/builds/${run}`, account.id]);

      const agent = await loggedIn(account.email);
      const res = await agent.post('/account/delete').type('form').send({ _csrf: await freshCsrf(agent), password: PASSWORD });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/?deleted=1');

      // The address and the customer's words survive only in the tables the
      // route says it leaves, and in no table it says it removes.
      const withAddress = await tablesMentioning(account.email);
      const withWords = await tablesMentioning(worked);
      expect(withAddress).toEqual({ builds: 1, email_log: 1, newsletter_subscribers: 1 });
      expect(withWords).toEqual({ builds: 1, build_logs: 1 });
      const leaves: readonly string[] = authRoutes.ACCOUNT_DELETION_LEAVES;
      const removes: readonly string[] = authRoutes.ACCOUNT_DELETION_REMOVES;
      for (const table of [...Object.keys(withAddress), ...Object.keys(withWords)]) {
        expect(leaves, table).toContain(table);
        expect(removes, table).not.toContain(table);
      }
      // The build is still there, whole, and belongs to nobody; the page view lost its link.
      expect(await one('SELECT status, customer_id, spec->>\'admin_email\' AS admin_email FROM builds WHERE id = $1', [buildId])).toEqual({
        status: 'live',
        customer_id: null,
        admin_email: account.email,
      });
      expect(await one('SELECT customer_id FROM page_views WHERE id = $1', [view.id])).toEqual({ customer_id: null });
      expect(leaves).toContain('page_views');

      // And that is what the page tells a customer, sentence for sentence.
      const page = (await request(app).get('/privacy')).text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
      expect(page).toContain('the records of your website builds stay in our database with the link to your account removed');
      expect(page).toContain('which holds what you told us about your work and can include your email address');
      expect(page).toContain("and the build's logs");
      expect(page).toContain('The log of emails we sent you, with your address, also stays');
      expect(page).toContain('and so does an address you gave the newsletter list');
      expect(page).toContain('Page-view records stay without your account identifier');
      expect(page).toContain("deleting your account does not take your website down, and it does not delete the website's own database");
    });

    it('a wrong password deletes nothing', async () => {
      const account = await createCustomer('stayer');
      const agent = await loggedIn(account.email);
      const res = await agent.post('/account/delete').type('form').send({ _csrf: await freshCsrf(agent), password: `${PASSWORD}-wrong` });
      expect(res.status).toBe(200);
      expect(res.text).toContain('Incorrect password');
      expect(await q('SELECT 1 FROM customers WHERE id = $1', [account.id])).toHaveLength(1);
    });

    it('the password check is rate limited like a login: the sixth attempt in the window is refused, even with the right password, and another account is not held back', async () => {
      const account = await createCustomer('guessed');
      const bystander = await createCustomer('bystander');
      const agent = await loggedIn(account.email);
      const other = await loggedIn(bystander.email);
      const attempt = async (who: Browser, password: string) =>
        who.post('/account/delete').type('form').send({ _csrf: await freshCsrf(who), password });
      // The limiter is skipped under NODE_ENV=test, like the login's; the
      // setting is read when a request arrives, so this runs the attempts as a
      // development server would.
      const savedEnv = process.env.NODE_ENV;
      process.env.NODE_ENV = 'development';
      try {
        for (let i = 1; i <= 5; i += 1) {
          const res = await attempt(agent, `${PASSWORD}-wrong-${i}`);
          expect(res.status).toBe(200);
          expect(res.text).toContain('Incorrect password');
        }
        const refused = await attempt(agent, PASSWORD);
        expect(refused.status).toBe(429);
        expect(refused.text).toContain('Too many attempts');
        expect(await q('SELECT 1 FROM customers WHERE id = $1', [account.id])).toHaveLength(1);
        // Another account, from the same address, still gets its own attempts.
        const fine = await attempt(other, `${PASSWORD}-wrong`);
        expect(fine.status).toBe(200);
        expect(fine.text).toContain('Incorrect password');
      } finally {
        process.env.NODE_ENV = savedEnv;
      }
    });

    it('the ops queue-bypass does not queue a draft that has no customer (the worker could never claim it)', async () => {
      const bypass = (id: string) =>
        request(app).post(`/ops/api/v1/builds/${id}/queue-bypass`).set('Authorization', `Bearer ${OPS_KEY}`).send({});
      const orphan = await createBuild(null, { status: 'intake', review: null });
      expect((await bypass(orphan)).status).toBe(404);
      expect((await buildRow(orphan)).status).toBe('intake');

      const owned = await createBuild((await createCustomer('bypass-owner')).id, { status: 'intake', review: null });
      expect((await bypass(owned)).status).toBe(200);
      expect((await buildRow(owned)).status).toBe('queued');
    });
  });

  // -------------------------------------------------------------------------
  describe('signup against an address someone else already claimed', () => {
    const signUp = async (agent: Browser, address: string, password: string, name: string) =>
      agent.post('/signup').type('form').send({ _csrf: await freshCsrf(agent), email: address, password, name });
    const tryLogin = async (address: string, password: string) => {
      const agent = browser();
      return agent.post('/login').type('form').send({ _csrf: await freshCsrf(agent), email: address, password });
    };
    const stored = (address: string) =>
      one<{ password: string; name: string; email_verified: boolean }>('SELECT password, name, email_verified FROM customers WHERE email = $1', [address]);
    const attackerPassword = `attacker-${crypto.randomBytes(8).toString('hex')}`;
    const ownerPassword = `owner-${crypto.randomBytes(8).toString('hex')}`;

    it('the owner signs up later and verifies: their password and name, not the first claimant\'s', async () => {
      const address = email('victim-a');
      const attacker = browser();
      const first = await signUp(attacker, address, attackerPassword, 'Mallory');
      expect(first.headers.location).toBe('/verify-email-sent');
      const attackerHash = (await stored(address)).password;

      const owner = browser();
      const second = await signUp(owner, address, ownerPassword, 'Vera Owner');
      expect(second.status).toBe(first.status);
      expect(second.headers.location).toBe(first.headers.location);
      const afterClaim = await stored(address);
      expect(afterClaim.password).not.toBe(attackerHash);
      expect(afterClaim.name).toBe('Vera Owner');

      await vi.waitFor(() => expect(latestDevLink('Verification link')).toContain('/verify-email?token='));
      const verified = await owner.get(latestDevLink('Verification link'));
      expect(verified.status).toBe(302);
      expect(verified.headers.location).toBe('/intake');
      expect(await stored(address)).toMatchObject({ email_verified: true, name: 'Vera Owner' });

      expect((await tryLogin(address, attackerPassword)).status).toBe(200);
      const ownerLogin = await tryLogin(address, ownerPassword);
      expect(ownerLogin.status).toBe(302);
      expect(ownerLogin.headers.location).toBe('/dashboard');
    });

    it('the first claimant claims again after the owner; the owner opens the newest link and still gets their own password', async () => {
      const address = email('victim-b');
      const owner = browser();
      await signUp(owner, address, ownerPassword, 'Vera Owner');
      const before = infoSpy.mock.calls.length;
      await signUp(browser(), address, attackerPassword, 'Mallory');
      await vi.waitFor(() => expect(infoSpy.mock.calls.length).toBeGreaterThan(before));
      expect(await bcrypt.compare(attackerPassword, (await stored(address)).password)).toBe(true);

      const verified = await owner.get(latestDevLink('Verification link'));
      expect(verified.headers.location).toBe('/intake');
      const row = await stored(address);
      expect(row).toMatchObject({ email_verified: true, name: 'Vera Owner' });
      expect(await bcrypt.compare(ownerPassword, row.password)).toBe(true);
      expect((await tryLogin(address, attackerPassword)).status).toBe(200);
      expect((await tryLogin(address, ownerPassword)).status).toBe(302);
    });

    it('the owner never signed up and opens a link the claimant triggered: nothing the claimant typed survives', async () => {
      const address = email('victim-c');
      const before = infoSpy.mock.calls.length;
      await signUp(browser(), address, attackerPassword, 'Mallory');
      await vi.waitFor(() => expect(infoSpy.mock.calls.length).toBeGreaterThan(before));

      const owner = browser();
      const verified = await owner.get(latestDevLink('Verification link'));
      expect(verified.status).toBe(302);
      expect(verified.headers.location).toMatch(/^\/reset-password\?token=[0-9a-f]{64}&verified=1$/);
      const row = await stored(address);
      expect(row.email_verified).toBe(true);
      expect(row.name).toBe(address.split('@')[0]);
      expect(await bcrypt.compare(attackerPassword, row.password)).toBe(false);
      expect((await tryLogin(address, attackerPassword)).status).toBe(200);

      const token = new URL(String(verified.headers.location), 'http://localhost').searchParams.get('token') as string;
      const set = await owner.post('/reset-password').type('form').send({ _csrf: await freshCsrf(owner), token, password: ownerPassword });
      expect(set.text).toContain('Your password is set');
      expect((await tryLogin(address, ownerPassword)).status).toBe(302);
    });

    it('a verified account is not touched by a signup against it, and an old link cannot change it', async () => {
      const account = await createCustomer('verified-victim');
      const before = await stored(account.email);
      const leftover = crypto.randomBytes(32).toString('hex');
      await q('INSERT INTO email_verification_tokens (customer_id, token) VALUES ($1, $2)', [
        account.id,
        crypto.createHash('sha256').update(leftover).digest('hex'),
      ]);

      const attacker = browser();
      const res = await signUp(attacker, account.email, attackerPassword, 'Mallory');
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/verify-email-sent');
      expect(await stored(account.email)).toEqual(before);

      const viaLink = await attacker.get(`/verify-email?token=${leftover}`);
      expect(viaLink.text).toContain('expired or already been used');
      expect(await stored(account.email)).toEqual(before);
      expect((await tryLogin(account.email, attackerPassword)).status).toBe(200);
      expect((await tryLogin(account.email, PASSWORD)).status).toBe(302);
    });
  });

  // -------------------------------------------------------------------------
  describe('crisis first, on a real conversation', () => {
    it('at the email gate an ordinary message is asked for an email and a crisis message gets the script, stored for review', async () => {
      const visitor = await trialBrowser();
      await q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'turn ' || n, 'ok' FROM generate_series(1, 22) AS n`,
        [visitor.trialId]
      );

      const ordinary = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: 'What would my site look like?' });
      expect(ordinary.status).toBe(402);
      expect(ordinary.body.requiresEmail).toBe(true);

      const crisis = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT });
      expect(crisis.status).toBe(200);
      expect(crisis.body.companionText).toMatch(/988/);
      expect(crisis.body.railFlags).toEqual(['crisis', 'suicide']);

      await vi.waitFor(async () => {
        const storedTurns = await q<{ rail_flags: string[]; companion_text: string }>(
          'SELECT rail_flags, companion_text FROM companion_turns WHERE trial_id = $1 AND keeper_text = $2',
          [visitor.trialId, CRISIS_TEXT]
        );
        expect(storedTurns).toHaveLength(1);
        expect(storedTurns[0].rail_flags).toEqual(['crisis', 'suicide']);
        expect(storedTurns[0].companion_text).toBe(crisis.body.companionText);
      });
    });

    it('the payer\'s crisis message is filed under the keeper, and the released trial takes no new turns', async () => {
      const t = await trialWithPreview('crisis');
      await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag: 'crisis' })));

      const crisis = await t.agent.post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT });
      expect(crisis.status).toBe(200);
      expect(crisis.body.companionText).toMatch(/988/);
      await vi.waitFor(async () => {
        const rows = await q('SELECT 1 FROM companion_turns WHERE keeper_id = $1 AND keeper_text = $2', [t.payer.id, CRISIS_TEXT]);
        expect(rows).toHaveLength(1);
      });
      expect((await q('SELECT 1 FROM companion_turns WHERE trial_id = $1', [t.trialId])).length).toBe(0);
    });
  });

  // -------------------------------------------------------------------------
  // The tests below this line need to get past two calls that would leave the
  // machine. Each is replaced only where a test needs it, for that test, and
  // put back afterwards:
  //   - Stripe's Checkout creation (services/stripe.ts): replaced by a stand-in
  //     that records what the route asked for. The signed webhook event that
  //     follows is built from exactly those arguments, so the test carries the
  //     route's own choice of trial and address across to the webhook.
  //   - the model call inside spec extraction (services/spec_extraction.ts):
  //     replaced by a fixed spec, so the dispatch service around it runs for
  //     real. One test instead replaces the model client's `create` method, to
  //     run memory capture for real around a canned classification.
  interface CheckoutAsk {
    buildId: string;
    trialId: string | null;
    keeperCustomerId: string | null;
    stripeCustomerId: string | null;
    customerEmail: string;
    monthlyAmountCents: number;
  }
  function standInForCheckout(): { asks: CheckoutAsk[]; restore: () => void } {
    const asks: CheckoutAsk[] = [];
    const spy = vi.spyOn(stripeService, 'createHarborSubscriptionCheckoutSession').mockImplementation(async (opts) => {
      asks.push(opts as CheckoutAsk);
      return 'http://127.0.0.1/checkout-stand-in';
    });
    return { asks, restore: () => spy.mockRestore() };
  }
  /** The checkout.session.completed event Stripe would send for what the route asked for. */
  const paidEventFor = (ask: CheckoutAsk, tag: string) =>
    stripeEvent(
      'checkout.session.completed',
      checkoutSession({ buildId: ask.buildId, trialId: ask.trialId, keeperCustomerId: ask.keeperCustomerId, email: ask.customerEmail, tag })
    );

  function standInForExtraction(): { calls: () => number; restore: () => void } {
    const spy = vi.spyOn(specExtraction, 'extractSpecFromConversation').mockImplementation(async () => ({
      spec: {
        spec_version: '1.0',
        app_name: `harbor-site-${run}`,
        tier: 'build',
        copy: { about_body: 'I teach reiki in Portland.' },
        pages: [],
      },
      warnings: [],
      costMicros: 0,
      contextSize: { memoryRows: 0, turns: 0 },
    }) as never);
    return { calls: () => spy.mock.calls.length, restore: () => spy.mockRestore() };
  }

  const turnsOf = (column: 'keeper_id' | 'trial_id', id: string) =>
    q<{ keeper_text: string | null; surface: string }>(
      `SELECT keeper_text, surface FROM companion_turns WHERE ${column} = $1 ORDER BY created_at`,
      [id]
    );
  const notesOf = (column: 'keeper_id' | 'trial_id', id: string) =>
    q<{ content: string; category: string }>(
      `SELECT content, category FROM keeper_memory WHERE ${column} = $1 ORDER BY created_at`,
      [id]
    );
  async function recallOf(agent: Browser): Promise<string[]> {
    const res = await agent.get('/lighthouse/table/recall?format=json');
    expect(res.status).toBe(200);
    return (res.body.items as Array<{ content: string }>).map((item) => item.content);
  }
  /** Give the trial a note the way memory capture stores one: tied to a turn. */
  async function seedNote(trialId: string, content: string): Promise<void> {
    const turn = await one<{ turn_id: string }>('SELECT turn_id FROM companion_turns WHERE trial_id = $1 ORDER BY created_at LIMIT 1', [trialId]);
    await q(
      `INSERT INTO keeper_memory (trial_id, partition, category, content, source_turn_id) VALUES ($1, 'working', 'fact', $2, $3)`,
      [trialId, content, turn.turn_id]
    );
  }
  async function pay(t: { buildId: string; trialId: string; payer: { email: string } }, tag: string): Promise<void> {
    const res = await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: t.trialId, email: t.payer.email, tag })));
    expect(res.body).toEqual({ received: true });
  }
  /** The account a payment landed in, entered from a browser that never held the trial cookie. */
  async function loggedInElsewhere(payer: { id: string; email: string }): Promise<Browser> {
    await q('UPDATE customers SET email_verified = TRUE WHERE id = $1', [payer.id]);
    return loggedIn(payer.email);
  }
  /** A message the input rail answers itself: the turn is stored without any model call. */
  const RAIL_TEXT = 'please write me some testimonials';

  describe('what Haven remembers follows the customer through payment', () => {
    it('the notes panel and the recall page: the trial cookie before payment; the paying cookie and an account login after it', async () => {
      const t = await trialWithPreview('hvmem');
      const note = `Teaches reiki in Leeds ${run}`;
      await seedNote(t.trialId, note);

      // Before payment, by the trial cookie.
      expect((await t.agent.get('/lighthouse/table')).text).toContain(note);
      expect(await recallOf(t.agent)).toEqual([note]);

      await pay(t, 'hvmem');
      // Promotion moved the rows: nothing is left under the trial id.
      expect(await notesOf('trial_id', t.trialId)).toEqual([]);
      expect((await notesOf('keeper_id', t.payer.id)).map((row) => row.content)).toEqual([note]);

      // After payment, by the cookie the payer had all along.
      const table = await t.agent.get('/lighthouse/table');
      expect(table.text).toContain('Your site is live');
      expect(table.text).toContain(note);
      expect(table.text).toMatch(/<span id="memory-panel-count">1<\/span> notes/);
      expect(await recallOf(t.agent)).toEqual([note]);
      expect((await t.agent.get('/lighthouse/table/recall')).text).toContain(note);

      // And by the account, logged in on a browser that never had that cookie.
      const elsewhere = await loggedInElsewhere(t.payer);
      const theirTable = await elsewhere.get('/lighthouse/table');
      expect(theirTable.text).toContain('Tell me more.');
      expect(theirTable.text).toContain(note);
      expect(await recallOf(elsewhere)).toEqual([note]);

      // Nobody else sees it.
      const stranger = await trialBrowser();
      expect((await stranger.agent.get('/lighthouse/table')).text).not.toContain(note);
      expect(await recallOf(stranger.agent)).toEqual([]);
    });

    it('a keeper logged in on another browser: what they say is stored under the keeper and is there after a reload', async () => {
      const t = await trialWithPreview('hvsess');
      await pay(t, 'hvsess');
      const elsewhere = await loggedInElsewhere(t.payer);
      // That browser has a trial cookie of its own, as every /lighthouse visitor does.
      const page = await elsewhere.get('/lighthouse/table');
      const cookie = ([] as string[]).concat(page.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
      const theirTrial = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [
        String(cookie).split(';')[0].split('=')[1],
      ]);

      const said = await elsewhere.post('/lighthouse/table/turn').send({ keeperText: RAIL_TEXT });
      expect(said.status).toBe(200);
      expect(said.body.turnId).toBeTruthy();

      expect((await turnsOf('keeper_id', t.payer.id)).map((turn) => turn.keeper_text)).toContain(RAIL_TEXT);
      expect(await turnsOf('trial_id', theirTrial.trial_id)).toEqual([]);
      const reloaded = await elsewhere.get('/lighthouse/table');
      expect(reloaded.text).toContain(RAIL_TEXT);
      // The payer's own browser, by cookie, shows the same conversation.
      expect((await t.agent.get('/lighthouse/table')).text).toContain(RAIL_TEXT);
    });

    it('a template chosen before payment moves with the conversation; one chosen after it is stored under the keeper (by the paying cookie, and by an account login)', async () => {
      const t = await trialWithPreview('hvpick');
      const families = (await import('../../src/services/template-families')).listFamilies();
      expect(families.length).toBeGreaterThan(2);
      const early = families[families.length - 1].id;

      // Before payment the trial is the owner, and the choice is its note.
      const byTrial = await t.agent
        .post('/lighthouse/preview/choose')
        .set('x-csrf-token', await freshCsrf(t.agent))
        .send({ template_family_id: early });
      expect(byTrial.status).toBe(200);
      expect((await notesOf('trial_id', t.trialId)).map((row) => JSON.parse(row.content).template_family_id)).toEqual([early]);

      await pay(t, 'hvpick');

      const byCookie = await t.agent
        .post('/lighthouse/preview/choose')
        .set('x-csrf-token', await freshCsrf(t.agent))
        .send({ template_family_id: families[0].id });
      expect(byCookie.status).toBe(200);
      const elsewhere = await loggedInElsewhere(t.payer);
      const bySession = await elsewhere
        .post('/lighthouse/preview/choose')
        .set('x-csrf-token', await freshCsrf(elsewhere))
        .send({ template_family_id: families[1].id });
      expect(bySession.status).toBe(200);

      const choices = (await notesOf('keeper_id', t.payer.id)).filter((row) => row.category === 'design_pref');
      expect(choices.map((row) => JSON.parse(row.content).template_family_id)).toEqual([early, families[0].id, families[1].id]);
      // Nothing was written under a trial: not the released one, and not the
      // one the second browser was given a cookie for on that request.
      expect(await notesOf('trial_id', t.trialId)).toEqual([]);
      const theirCookie = ([] as string[]).concat(bySession.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
      const theirTrial = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [
        String(theirCookie).split(';')[0].split('=')[1],
      ]);
      expect(await notesOf('trial_id', theirTrial.trial_id)).toEqual([]);
    });

    it('a note captured from a turn that was in flight when the payment landed is filed under the keeper', async () => {
      // Memory capture runs in the background after the reply. Here the turn
      // was stored under the trial, the payment arrived, and only then does
      // the capture for that turn write its note, still holding the trial id
      // it started with.
      const t = await trialWithPreview('hvrace');
      const turn = await one<{ turn_id: string }>('SELECT turn_id FROM companion_turns WHERE trial_id = $1', [t.trialId]);
      await pay(t, 'hvrace');

      const note = `Sees clients on Thursdays ${run}`;
      const Anthropic = (await import('@anthropic-ai/sdk')).default;
      const create = vi.spyOn(Anthropic.Messages.prototype, 'create').mockResolvedValue({
        content: [{ type: 'text', text: JSON.stringify([{ category: 'fact', content: note }]) }],
        usage: { input_tokens: 1, output_tokens: 1 },
      } as never);
      process.env.ANTHROPIC_API_KEY = ['not', 'a', 'key', run].join('-');
      memoryCapture._resetClientForTests();
      try {
        const result = await memoryCapture.captureMemory({
          pool: appDbPool,
          trialId: t.trialId,
          keeperText: 'I see clients on Thursdays',
          recentContext: '',
          sourceTurnId: turn.turn_id,
        });
        expect(result.insertedMemoryIds).toHaveLength(1);
      } finally {
        create.mockRestore();
        delete process.env.ANTHROPIC_API_KEY;
        memoryCapture._resetClientForTests();
      }

      expect(await notesOf('trial_id', t.trialId)).toEqual([]);
      expect((await notesOf('keeper_id', t.payer.id)).map((row) => row.content)).toEqual([note]);
      expect((await t.agent.get('/lighthouse/table')).text).toContain(note);
    });

    it('a turn whose answer straddles the payment is stored under the keeper, not under the released trial', async () => {
      // The visitor sends a message; while the model is still answering, the
      // payment lands and the trial is released; then the answer arrives and
      // the turn is stored. The model client is held open here to make that
      // order certain. (Everything else is the application's own: the turn
      // route, the runtime, the webhook, promotion.)
      const t = await trialWithPreview('hvmid');
      const reply = 'A workshops page sounds right. Tell me what happens in one.';
      const said = `Could we add a page for workshops ${run}`;
      const Anthropic = (await import('@anthropic-ai/sdk')).default;
      let modelEntered!: () => void;
      const inTheModel = new Promise<void>((resolve) => {
        modelEntered = resolve;
      });
      let letModelAnswer!: () => void;
      const answerAllowed = new Promise<void>((resolve) => {
        letModelAnswer = resolve;
      });
      let classifierCalls = 0;
      const create = vi.spyOn(Anthropic.Messages.prototype, 'create').mockImplementation((async (body: { system?: unknown }) => {
        if (Array.isArray(body.system)) {
          // Haven's reply (the companion call sends its system prompt as blocks).
          modelEntered();
          await answerAllowed;
          return { content: [{ type: 'text', text: reply }], usage: { input_tokens: 1, output_tokens: 1 } };
        }
        // The memory classifier that runs after the turn: nothing to note.
        classifierCalls += 1;
        return { content: [{ type: 'text', text: '[]' }], usage: { input_tokens: 1, output_tokens: 1 } };
      }) as never);
      process.env.ANTHROPIC_API_KEY = ['not', 'a', 'key', run].join('-');
      companionClaude._resetClientForTests();
      memoryCapture._resetClientForTests();
      try {
        const answered = t.agent
          .post('/lighthouse/table/turn')
          .send({ keeperText: said })
          .then((res) => res);
        await inTheModel;
        await pay(t, 'hvmid');
        expect((await buildRow(t.buildId)).status).toBe('live');
        letModelAnswer();

        const res = await answered;
        expect(res.status).toBe(200);
        expect(res.body.companionText).toBe(reply);
        await vi.waitFor(() => expect(classifierCalls).toBe(1));
      } finally {
        letModelAnswer();
        create.mockRestore();
        delete process.env.ANTHROPIC_API_KEY;
        companionClaude._resetClientForTests();
        memoryCapture._resetClientForTests();
      }

      expect(await turnsOf('trial_id', t.trialId)).toEqual([]);
      expect((await turnsOf('keeper_id', t.payer.id)).map((turn) => turn.keeper_text)).toContain(said);
      const table = await t.agent.get('/lighthouse/table');
      expect(table.text).toContain(said);
      expect(table.text).toContain(reply);
    });

    it('an owner who set a password and logged in before paying keeps the conversation: Checkout carries the trial and the webhook moves it', async () => {
      const t = await trialWithPreview('hvprelog');
      const note = `Works from a studio in Bath ${run}`;
      await seedNote(t.trialId, note);
      // The same browser, now also logged in to the account the preview was attached to.
      await q('UPDATE customers SET email_verified = TRUE WHERE id = $1', [t.payer.id]);
      const login = await t.agent.post('/login').type('form').send({ _csrf: await freshCsrf(t.agent), email: t.payer.email, password: PASSWORD });
      expect(login.status).toBe(302);

      const form = await t.agent.get(`/lighthouse/lamp/${t.buildId}/make-public`);
      expect(form.status).toBe(200);
      expect(form.text).toContain(t.payer.email);
      expect(form.text).not.toContain('name="email_confirmed"');

      const checkout = standInForCheckout();
      try {
        const submitted = await t.agent
          .post(`/lighthouse/lamp/${t.buildId}/make-public`)
          .type('form')
          .send({ _csrf: csrfFrom(form.text), monthly_amount_cents: '3900', email: t.payer.email });
        expect(submitted.status).toBe(303);
        expect(checkout.asks).toHaveLength(1);
        expect(checkout.asks[0]).toMatchObject({
          buildId: t.buildId,
          trialId: t.trialId,
          keeperCustomerId: t.payer.id,
          customerEmail: t.payer.email,
        });
      } finally {
        checkout.restore();
      }
      expect((await deliver(paidEventFor(checkout.asks[0], 'hvprelog'))).body).toEqual({ received: true });

      expect((await buildRow(t.buildId)).status).toBe('live');
      expect(await turnsOf('trial_id', t.trialId)).toEqual([]);
      const table = await t.agent.get('/lighthouse/table');
      expect(table.text).toContain('Tell me more.');
      expect(table.text).toContain(note);
      expect(await recallOf(t.agent)).toEqual([note]);
    });

    it('the same owner paying from a browser that never held the conversation: the trial stamped with the build is the one promoted', async () => {
      const t = await trialWithPreview('hvother');
      const elsewhere = await loggedInElsewhere(t.payer);
      const form = await elsewhere.get(`/lighthouse/lamp/${t.buildId}/make-public`);
      expect(form.status).toBe(200);

      const checkout = standInForCheckout();
      try {
        const submitted = await elsewhere
          .post(`/lighthouse/lamp/${t.buildId}/make-public`)
          .type('form')
          .send({ _csrf: csrfFrom(form.text), monthly_amount_cents: '3900' });
        expect(submitted.status).toBe(303);
        expect(checkout.asks[0]).toMatchObject({ trialId: t.trialId, customerEmail: t.payer.email });
      } finally {
        checkout.restore();
      }
      await deliver(paidEventFor(checkout.asks[0], 'hvother'));
      expect((await elsewhere.get('/lighthouse/table')).text).toContain('Tell me more.');
      // And the browser that had the conversation is recognized as the payer's.
      expect((await t.agent.get('/lighthouse/table')).text).toContain('Your site is live');
    });

    it('a paid session that names no trial still moves the conversation: the webhook promotes the trial stamped with the build', async () => {
      const t = await trialWithPreview('hvnone');
      const note = `Runs a clinic in Hull ${run}`;
      await seedNote(t.trialId, note);

      // The event as Stripe sends it for a Checkout session whose metadata
      // carries the build and no trial.
      const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: null, email: t.payer.email, tag: 'hvnone' }));
      expect((event.data.object as { metadata: Record<string, string> }).metadata.trial_id).toBeUndefined();
      expect((await deliver(event)).body).toEqual({ received: true });

      expect((await buildRow(t.buildId)).status).toBe('live');
      const trial = await one<{ released: boolean }>('SELECT released_at IS NOT NULL AS released FROM trial_keepers WHERE trial_id = $1', [t.trialId]);
      expect(trial.released).toBe(true);
      expect(await turnsOf('trial_id', t.trialId)).toEqual([]);
      expect((await turnsOf('keeper_id', t.payer.id)).map((turn) => turn.keeper_text)).toContain('I teach reiki');
      expect((await notesOf('keeper_id', t.payer.id)).map((row) => row.content)).toEqual([note]);
      // The browser that held the conversation is recognized as the payer's.
      const table = await t.agent.get('/lighthouse/table');
      expect(table.text).toContain('Tell me more.');
      expect(table.text).toContain(note);
    });

    it('the operator still finds a conversation after its owner pays: it leaves the trial list and is listed under Paid keepers, counted by keeper id', async () => {
      const t = await trialWithPreview('hvinbox');
      await seedNote(t.trialId, `Teaches on Tuesdays ${run}`);
      const admin = await loggedIn(ADMIN_EMAIL);
      /** The inbox row carrying this attribute, up to the end of the row. */
      const rowOf = (html: string, attribute: string): string | null => {
        const at = html.indexOf(attribute);
        return at === -1 ? null : html.slice(at, html.indexOf('</tr>', at));
      };
      const counts = (row: string): number[] => [...row.matchAll(/tabular-nums">(\d+)</g)].map((m) => Number(m[1]));
      const trialRow = `data-trial-id="${t.trialId}"`;
      const keeperRow = `data-keeper-id="${t.payer.id}"`;

      // Before payment: a trial row with one turn and one note, and no keeper.
      const before = rowOf((await admin.get('/admin/harbor')).text, trialRow);
      expect(before).not.toBeNull();
      expect(await turnsOf('trial_id', t.trialId)).toHaveLength(1);
      expect(counts(before as string)).toEqual([1, 1]);
      expect(rowOf((await admin.get('/admin/harbor?status=keepers')).text, keeperRow)).toBeNull();

      await pay(t, 'hvinbox');
      // The keeper says one more thing, from a browser logged in to the account.
      const elsewhere = await loggedInElsewhere(t.payer);
      expect((await elsewhere.post('/lighthouse/table/turn').send({ keeperText: RAIL_TEXT })).status).toBe(200);
      const stored = await turnsOf('keeper_id', t.payer.id);
      expect(stored.length).toBeGreaterThanOrEqual(2);

      // The released trial is gone from the trial list in every trial view.
      for (const view of ['', '?status=active', '?status=ready', '?status=handed_off']) {
        expect(rowOf((await admin.get(`/admin/harbor${view}`)).text, trialRow), view).toBeNull();
      }
      const page = await admin.get('/admin/harbor?status=keepers');
      expect(page.status).toBe(200);
      const row = rowOf(page.text, keeperRow);
      expect(row).not.toBeNull();
      // Every turn the keeper now owns (the one from before payment, what the
      // webhook appended, and the one said after), and the note that moved.
      expect(counts(row as string)).toEqual([stored.length, 1]);
      expect(row).toContain(t.payer.email);
      expect(row).toContain('I teach reiki');
      expect(row).toContain(`href="/admin/builds/${t.buildId}"`);
      expect(row).toContain('>active<');

      // The owner of the conversation cannot open the operator's list.
      const asKeeper = await elsewhere.get('/admin/harbor?status=keepers');
      expect(asKeeper.status).toBe(403);
      expect(asKeeper.text).not.toContain('data-keeper-list');
      expect(asKeeper.text).not.toContain('I teach reiki');
    });

    it('deleting the account removes the conversation and the notes, what was said by login included; the paying cookie then starts an empty conversation', async () => {
      const t = await trialWithPreview('hvgone');
      const note = `Keeps a studio in York ${run}`;
      await seedNote(t.trialId, note);
      await pay(t, 'hvgone');
      // An ended subscription, so the deletion has nothing to cancel at Stripe.
      await q(`UPDATE harbor_subscriptions SET status = 'canceled' WHERE keeper_customer_id = $1`, [t.payer.id]);

      const elsewhere = await loggedInElsewhere(t.payer);
      const said = `${RAIL_TEXT} for York ${run}`;
      expect((await elsewhere.post('/lighthouse/table/turn').send({ keeperText: said })).status).toBe(200);
      expect((await t.agent.get('/lighthouse/table')).text).toContain(note);

      const gone = await elsewhere.post('/account/delete').type('form').send({ _csrf: await freshCsrf(elsewhere), password: PASSWORD });
      expect(gone.status).toBe(302);
      expect(gone.headers.location).toBe('/?deleted=1');

      // Nothing the keeper said or Haven noted is left under any key.
      expect(await q('SELECT 1 FROM companion_turns WHERE keeper_text = $1', [said])).toEqual([]);
      expect(await q('SELECT 1 FROM keeper_memory WHERE content = $1', [note])).toEqual([]);
      expect(await turnsOf('keeper_id', t.payer.id)).toEqual([]);
      expect(await turnsOf('trial_id', t.trialId)).toEqual([]);

      // The cookie that paid no longer stands for anyone: a new conversation.
      const after = await t.agent.get('/lighthouse/table');
      expect(after.status).toBe(200);
      expect(after.text).not.toContain(note);
      expect(after.text).not.toContain('Tell me more.');
      expect(after.text).not.toContain('Your site is live');
      expect(await recallOf(t.agent)).toEqual([]);
    });
  });

  // -------------------------------------------------------------------------
  describe('the address a payment is tied to: confirmed on the page, and the same one everywhere', () => {
    it('a trial is shown the account address, must confirm it, and cannot pay under a different one; Checkout then carries that address', async () => {
      const t = await trialWithPreview('hvaddr');
      const form = await t.agent.get(`/lighthouse/lamp/${t.buildId}/make-public`);
      expect(form.status).toBe(200);
      expect(form.text).toContain(`<input type="hidden" name="email" value="${t.payer.email}">`);
      expect(form.text).toContain('name="email_confirmed"');
      expect(form.text).not.toMatch(/<input\b[^>]*type="email"/);

      const checkout = standInForCheckout();
      try {
        const post = (fields: Record<string, string>) =>
          t.agent
            .post(`/lighthouse/lamp/${t.buildId}/make-public`)
            .type('form')
            .send({ _csrf: csrfFrom(form.text), monthly_amount_cents: '3900', ...fields });

        const unconfirmed = await post({ email: t.payer.email });
        expect(unconfirmed.status).toBe(400);
        expect(unconfirmed.body.error).toBe('email_confirmation_required');

        const other = await createCustomer('addr-other');
        for (const typed of [email('addr-nobody'), other.email]) {
          const changed = await post({ email: typed, email_confirmed: 'on' });
          expect(changed.status).toBe(409);
          // The same answer whether or not the typed address has an account.
          expect(changed.body.error).toBe('email_mismatch');
        }
        expect(checkout.asks).toEqual([]);
        expect((await buildRow(t.buildId)).customer_id).toBe(t.payer.id);

        const confirmed = await post({ email: t.payer.email.toUpperCase(), email_confirmed: 'on' });
        expect(confirmed.status).toBe(303);
        expect(checkout.asks).toHaveLength(1);
        expect(checkout.asks[0]).toMatchObject({ trialId: t.trialId, customerEmail: t.payer.email, keeperCustomerId: null });
      } finally {
        checkout.restore();
      }

      // What the webhook then does with exactly that: the account, the mail
      // and the receipt address are one address.
      await deliver(paidEventFor(checkout.asks[0], 'hvaddr'));
      expect((await buildRow(t.buildId)).customer_id).toBe(t.payer.id);
      await vi.waitFor(async () => {
        const mail = await q<{ to_email: string }>(
          `SELECT to_email FROM email_log WHERE to_email = $1 AND template = 'harbor_made_public'`,
          [t.payer.email]
        );
        expect(mail).toHaveLength(1);
      });
    });

    it('a build attached to an account the trial gave no address for: the cookie cannot pay into it or learn its address; the account, logged in, can', async () => {
      const t = await trialWithPreview('hvelse');
      // As an operator's dispatch for an address of the operator's choosing
      // leaves it: the build's customer is not the account for the address
      // this conversation gave.
      const chosen = await createCustomer('hvelse-chosen');
      await q('UPDATE builds SET customer_id = $1 WHERE id = $2', [chosen.id, t.buildId]);

      const page = await t.agent.get(`/lighthouse/lamp/${t.buildId}/make-public`);
      expect(page.status).toBe(409);
      expect(page.text).toContain('Please log in to that account');
      expect(page.text).not.toContain(chosen.email);

      const checkout = standInForCheckout();
      try {
        const byCookie = await t.agent
          .post(`/lighthouse/lamp/${t.buildId}/make-public`)
          .type('form')
          .send({ _csrf: await freshCsrf(t.agent), monthly_amount_cents: '3900', email: chosen.email, email_confirmed: 'on' });
        expect(byCookie.status).toBe(409);
        expect(byCookie.body.error).toBe('account_login_required');
        expect(JSON.stringify(byCookie.body)).not.toContain(chosen.email);
        expect(checkout.asks).toEqual([]);

        const owner = await loggedIn(chosen.email);
        const form = await owner.get(`/lighthouse/lamp/${t.buildId}/make-public`);
        expect(form.status).toBe(200);
        const byOwner = await owner
          .post(`/lighthouse/lamp/${t.buildId}/make-public`)
          .type('form')
          .send({ _csrf: csrfFrom(form.text), monthly_amount_cents: '3900' });
        expect(byOwner.status).toBe(303);
        expect(checkout.asks).toHaveLength(1);
        expect(checkout.asks[0]).toMatchObject({ trialId: t.trialId, keeperCustomerId: chosen.id, customerEmail: chosen.email });
      } finally {
        checkout.restore();
      }
    });
  });

  // -------------------------------------------------------------------------
  // The rule "a trial cannot pay into an account that already has a
  // subscription" used to be enforced only by the route that opens Checkout.
  // A Checkout session opened earlier completes later, through the webhook,
  // which had no such rule, and the second trial's cookie then read the first
  // payer's conversation. These tests drive all three sides of that seam:
  // the make-public route (which opens the sessions), the signed webhook
  // (which applies them) and the cookie read (which says whose conversation a
  // browser may see and write), on rows the real routes and the real webhook
  // wrote. They also seed, where they say so, the rows the earlier code left
  // behind, because the cookie rule has to hold for those too.
  describe('a second trial on an account that already has a keeper: the route, the webhook and the cookie agree', () => {
    /** Two trials, each with an approved Harbor preview stamped on it, on the one unverified account whose address both gave. */
    async function twoTrialsOnOneAccount(tag: string) {
      const first = await trialWithPreview(`${tag}a`);
      const secret = `first-trial-says-${tag}-${run}`;
      await q('UPDATE companion_turns SET keeper_text = $1 WHERE trial_id = $2', [secret, first.trialId]);
      const note = `Teaches reiki on Sundays ${tag} ${run}`;
      await seedNote(first.trialId, note);
      const visitor = await trialBrowser();
      const buildId = await createBuild(first.payer.id, { harbor: true });
      await q('UPDATE trial_keepers SET handoff_build_id = $1, handoff_at = NOW(), email_captured = $2 WHERE trial_id = $3', [
        buildId,
        first.payer.email,
        visitor.trialId,
      ]);
      const spoken = `second-trial-says-${tag}-${run}`;
      await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, $2, 'Go on.')`, [visitor.trialId, spoken]);
      return { first: { ...first, secret, note }, second: { ...visitor, buildId, spoken } };
    }

    /** Press "make public" as a trial cookie: the response, and what the route asked Stripe for (null when it asked for nothing). */
    async function openCheckout(who: { agent: Browser; buildId: string }, address: string) {
      const checkout = standInForCheckout();
      try {
        const res = await who.agent
          .post(`/lighthouse/lamp/${who.buildId}/make-public`)
          .type('form')
          .send({ _csrf: await freshCsrf(who.agent), monthly_amount_cents: '3900', email: address, email_confirmed: 'on' });
        return { res, ask: checkout.asks[0] ?? null };
      } finally {
        checkout.restore();
      }
    }

    /** The rows the earlier code left when it promoted a trial into whatever keeper its build's account had: released, cookie kept for 30 days, nothing recording where it went. */
    const releasedAsTheEarlierCodeDid = (trialId: string) =>
      q(`UPDATE trial_keepers SET released_at = NOW(), expires_at = GREATEST(expires_at, NOW() + INTERVAL '30 days') WHERE trial_id = $1`, [trialId]);

    const paymentAlerts = (tag: string) =>
      q<{ to_email: string; subject: string }>(
        `SELECT to_email, subject FROM email_log WHERE template = 'harbor_payment_not_applied' AND metadata->>'stripe_session_id' = $1`,
        [`cs_${run}_${tag}`]
      );
    const trialRow = (trialId: string) =>
      one<{ released: boolean; promoted_customer_id: string | null }>(
        'SELECT released_at IS NOT NULL AS released, promoted_customer_id FROM trial_keepers WHERE trial_id = $1',
        [trialId]
      );

    it('a Checkout opened by the second trial before the first one paid completes afterwards: the keeper stays the first payer\'s, the second trial and its cookie stay the second\'s, and the money is recorded and flagged', async () => {
      const { first, second } = await twoTrialsOnOneAccount('h1');
      const account = first.payer;

      // The route lets the second trial open Checkout while the account has no subscription.
      const secondOpened = await openCheckout(second, account.email);
      expect(secondOpened.res.status).toBe(303);
      expect(secondOpened.ask).toMatchObject({ trialId: second.trialId, buildId: second.buildId, keeperCustomerId: null });

      // The first trial opens its own and pays.
      const firstOpened = await openCheckout(first, account.email);
      expect(firstOpened.res.status).toBe(303);
      expect((await deliver(paidEventFor(firstOpened.ask as CheckoutAsk, 'h1a'))).body).toEqual({ received: true });
      expect(await keeperRow(account.id)).toMatchObject({ stripe_subscription_id: `sub_${run}_h1a`, subscription_status: 'active', handoff_build_id: first.buildId });

      // The rule the documents name, now that the account has a subscription: the route refuses.
      const again = await openCheckout(second, account.email);
      expect(again.res.status).toBe(409);
      expect(again.res.body.error).toBe('account_already_subscribed');
      expect(again.ask).toBeNull();

      // The session opened earlier completes: the webhook applies the same rule.
      const event = paidEventFor(secondOpened.ask as CheckoutAsk, 'h1b');
      const applied = await deliver(event);
      expect(applied.status).toBe(200);
      expect(applied.body).toEqual({ received: true });
      expect(await claimed(event.id)).toBe(1);

      // The keeper row is still the first payer's, and so is the conversation.
      expect(await keeperRow(account.id)).toMatchObject({ stripe_subscription_id: `sub_${run}_h1a`, subscription_status: 'active', handoff_build_id: first.buildId });
      expect((await turnsOf('keeper_id', account.id)).map((turn) => turn.keeper_text)).toContain(first.secret);
      expect((await turnsOf('keeper_id', account.id)).map((turn) => turn.keeper_text)).not.toContain(second.spoken);
      // The second trial was not promoted: still a trial, with its own turns.
      expect(await trialRow(second.trialId)).toEqual({ released: false, promoted_customer_id: null });
      expect((await turnsOf('trial_id', second.trialId)).map((turn) => turn.keeper_text)).toEqual([second.spoken]);
      // The first trial was promoted into this account, and the row says so.
      expect(await trialRow(first.trialId)).toEqual({ released: true, promoted_customer_id: account.id });
      // The second build was not taken live by that payment; the first is live.
      expect(await buildRow(second.buildId)).toMatchObject({ status: 'preview', live_url: null, stripe_session_id: null });
      expect((await buildRow(first.buildId)).status).toBe('live');
      // The payment is recorded against the account, so that the events Stripe sends for it find a row,
      // and the operator is told (the alert goes to ADMIN_EMAIL, with the ids needed to deal with it).
      expect(await subscriptionRow('h1b')).toMatchObject({ status: 'active', keeper_customer_id: account.id, monthly_amount_cents: 2900 });
      await vi.waitFor(async () => {
        const alerts = await paymentAlerts('h1b');
        expect(alerts).toHaveLength(1);
        expect(alerts[0].to_email).toBe(ADMIN_EMAIL);
      });
      // Nothing was alerted for the payment that was applied.
      expect(await paymentAlerts('h1a')).toEqual([]);

      // What each browser can now do and see.
      const secondTable = await second.agent.get('/lighthouse/table');
      expect(secondTable.status).toBe(200);
      expect(secondTable.text).toContain(second.spoken);
      expect(secondTable.text).not.toContain(first.secret);
      expect(secondTable.text).not.toContain(first.note);
      expect(await recallOf(second.agent)).toEqual([]);
      // It is the owner of its own preview as a trial, and has no lamp of the first payer's.
      expect((await second.agent.get(`/lighthouse/lamp/${second.buildId}`)).status).toBe(200);
      expect((await second.agent.get(`/lighthouse/lamp/${first.buildId}`)).status).toBe(404);
      // It cannot cancel the first payer's subscription (a keeper's action; that would call Stripe).
      const cancel = await second.agent
        .post(`/lighthouse/lamp/${second.buildId}/cancel-subscription`)
        .type('form')
        .send({ _csrf: await freshCsrf(second.agent) });
      expect(cancel.status).toBe(403);
      // What it says is stored under the trial, not under the keeper; a crisis message too.
      expect((await second.agent.post('/lighthouse/table/turn').send({ keeperText: RAIL_TEXT })).status).toBe(200);
      expect((await second.agent.post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT })).status).toBe(200);
      await vi.waitFor(async () => {
        expect((await turnsOf('trial_id', second.trialId)).map((turn) => turn.keeper_text)).toEqual(
          expect.arrayContaining([second.spoken, RAIL_TEXT, CRISIS_TEXT])
        );
      });
      expect((await turnsOf('keeper_id', account.id)).map((turn) => turn.keeper_text)).not.toEqual(
        expect.arrayContaining([RAIL_TEXT])
      );
      expect((await turnsOf('keeper_id', account.id)).map((turn) => turn.keeper_text)).not.toEqual(
        expect.arrayContaining([CRISIS_TEXT])
      );
      // The first payer is unharmed: same cookie, same keeper, same conversation, same lamp.
      const firstTable = await first.agent.get('/lighthouse/table');
      expect(firstTable.text).toContain(first.secret);
      expect(firstTable.text).toContain(first.note);
      expect(firstTable.text).not.toContain(second.spoken);
      expect((await first.agent.get(`/lighthouse/lamp/${first.buildId}`)).text).toContain('Cancel subscription');

      // Delivered again, the event is a duplicate and nothing is recorded twice.
      expect((await deliver(event)).body).toEqual({ received: true, duplicate: true });
      expect(await q('SELECT 1 FROM harbor_subscriptions WHERE keeper_customer_id = $1', [account.id])).toHaveLength(2);
      expect(await paymentAlerts('h1b')).toHaveLength(1);
    });

    it('the subscription events of the payment that was not applied do not touch the keeper: its status, cancellation and failures stay on its own row', async () => {
      const { first, second } = await twoTrialsOnOneAccount('h1ev');
      const secondOpened = await openCheckout(second, first.payer.email);
      await pay(first, 'h1ev1');
      await deliver(paidEventFor(secondOpened.ask as CheckoutAsk, 'h1ev2'));
      const now = Math.floor(Date.now() / 1000);
      expect((await deliver(stripeEvent('invoice.payment_failed', { id: 'in_x', subscription: `sub_${run}_h1ev2` }, now))).status).toBe(200);
      expect((await deliver(stripeEvent('customer.subscription.deleted', subscriptionObject('h1ev2', { status: 'canceled' }), now + 1))).status).toBe(200);
      expect(await subscriptionRow('h1ev2')).toMatchObject({ status: 'canceled', keeper_customer_id: first.payer.id });
      expect(await keeperRow(first.payer.id)).toMatchObject({ stripe_subscription_id: `sub_${run}_h1ev1`, subscription_status: 'active' });
      expect(await subscriptionRow('h1ev1')).toMatchObject({ status: 'active' });
    });

    it('two sessions for the same account completing at the same moment: exactly one is applied, the other is recorded and flagged', async () => {
      const { first, second } = await twoTrialsOnOneAccount('h1race');
      // Both sessions were opened while the account had no subscription.
      const firstOpened = await openCheckout(first, first.payer.email);
      const secondOpened = await openCheckout(second, first.payer.email);
      expect(firstOpened.res.status).toBe(303);
      expect(secondOpened.res.status).toBe(303);

      const results = await Promise.all([
        deliver(paidEventFor(firstOpened.ask as CheckoutAsk, 'h1race1')),
        deliver(paidEventFor(secondOpened.ask as CheckoutAsk, 'h1race2')),
      ]);
      expect(results.map((res) => res.status)).toEqual([200, 200]);

      const keeper = await keeperRow(first.payer.id);
      expect([`sub_${run}_h1race1`, `sub_${run}_h1race2`]).toContain(keeper.stripe_subscription_id);
      const winnerIsFirst = keeper.stripe_subscription_id === `sub_${run}_h1race1`;
      const [winner, loser] = winnerIsFirst ? [first, second] : [second, first];
      const [winnerTag, loserTag] = winnerIsFirst ? ['h1race1', 'h1race2'] : ['h1race2', 'h1race1'];
      // The winner's trial was promoted and its build is live; the loser's trial and build are untouched.
      expect(await trialRow(winner.trialId)).toEqual({ released: true, promoted_customer_id: first.payer.id });
      expect(await trialRow(loser.trialId)).toEqual({ released: false, promoted_customer_id: null });
      expect((await buildRow(winner.buildId)).status).toBe('live');
      expect((await buildRow(loser.buildId)).status).toBe('preview');
      // The loser's payment is on record and flagged; the winner's is not flagged.
      expect(await q('SELECT 1 FROM harbor_subscriptions WHERE keeper_customer_id = $1', [first.payer.id])).toHaveLength(2);
      await vi.waitFor(async () => expect(await paymentAlerts(loserTag)).toHaveLength(1));
      expect(await paymentAlerts(winnerTag)).toEqual([]);
    });

    it('rows the earlier code left, a trial released with nothing recording where it went: its cookie is nobody, reads no keeper\'s conversation and writes nothing into it', async () => {
      const { first, second } = await twoTrialsOnOneAccount('h1old');
      await pay(first, 'h1old');
      await releasedAsTheEarlierCodeDid(second.trialId);

      const table = await second.agent.get('/lighthouse/table');
      expect(table.status).toBe(200);
      expect(table.text).not.toContain(first.secret);
      expect(table.text).not.toContain(first.note);
      expect(await recallOf(second.agent)).toEqual([]);
      // It is given a fresh trial like any stale cookie; it owns no lamp.
      expect((await second.agent.get(`/lighthouse/lamp/${second.buildId}`)).status).toBe(404);
      expect((await second.agent.get(`/lighthouse/lamp/${first.buildId}`)).status).toBe(404);
      expect((await second.agent.post('/lighthouse/table/turn').send({ keeperText: RAIL_TEXT })).status).toBe(200);
      expect((await second.agent.post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT })).status).toBe(200);
      await new Promise((resolve) => setTimeout(resolve, 300));
      const stored = (await turnsOf('keeper_id', first.payer.id)).map((turn) => turn.keeper_text);
      expect(stored).not.toContain(RAIL_TEXT);
      expect(stored).not.toContain(CRISIS_TEXT);
      // The browser that did pay is still the payer.
      const firstTable = await first.agent.get('/lighthouse/table');
      expect(firstTable.text).toContain(first.secret);
      expect((await first.agent.get(`/lighthouse/lamp/${first.buildId}`)).text).toContain('Cancel subscription');
    });

    it('rows that name another account: a trial recorded as promoted into one account whose build belongs to another is the keeper of neither', async () => {
      const { first, second } = await twoTrialsOnOneAccount('h1other');
      await pay(first, 'h1other');
      // Another account that is a keeper too, with a conversation of its own.
      const elsewhere = await createCustomer('h1other-elsewhere');
      const elsewhereSecret = `elsewhere-says-${run}`;
      await q(`INSERT INTO keepers (customer_id, companion_name, companion_assigned) VALUES ($1, 'Haven', 'Haven')`, [elsewhere.id]);
      await q(`INSERT INTO companion_turns (keeper_id, keeper_text, companion_text) VALUES ($1, $2, 'ok')`, [elsewhere.id, elsewhereSecret]);
      await releasedAsTheEarlierCodeDid(second.trialId);
      await q('UPDATE trial_keepers SET promoted_customer_id = $1 WHERE trial_id = $2', [elsewhere.id, second.trialId]);

      const table = await second.agent.get('/lighthouse/table');
      expect(table.text).not.toContain(first.secret);
      expect(table.text).not.toContain(elsewhereSecret);
      expect((await second.agent.get(`/lighthouse/lamp/${second.buildId}`)).status).toBe(404);
    });

    it.each(['active', 'canceled'])(
      'a trial\'s payment replaces nothing, not even a subscription that has ended: the keeper\'s subscription is %s, the second trial is not promoted',
      async (status) => {
        const tag = `rn${status}`;
        const { first, second } = await twoTrialsOnOneAccount(tag);
        const secondOpened = await openCheckout(second, first.payer.email);
        expect(secondOpened.res.status).toBe(303);
        await pay(first, `${tag}1`);
        await q('UPDATE keepers SET subscription_status = $1 WHERE customer_id = $2', [status, first.payer.id]);

        expect((await deliver(paidEventFor(secondOpened.ask as CheckoutAsk, `${tag}2`))).status).toBe(200);
        expect(await keeperRow(first.payer.id)).toMatchObject({ stripe_subscription_id: `sub_${run}_${tag}1`, subscription_status: status });
        expect(await trialRow(second.trialId)).toEqual({ released: false, promoted_customer_id: null });
        expect((await buildRow(second.buildId)).status).toBe('preview');
        await vi.waitFor(async () => expect(await paymentAlerts(`${tag}2`)).toHaveLength(1));
      }
    );

    it('a turn whose answer straddles a release that was no promotion into the account\'s keeper is not stored under that keeper', async () => {
      const { first, second } = await twoTrialsOnOneAccount('h1mid');
      await pay(first, 'h1mid');
      const said = `Second trial straddling a release, h1mid ${run}`;
      const Anthropic = (await import('@anthropic-ai/sdk')).default;
      let modelEntered!: () => void;
      const inTheModel = new Promise<void>((resolve) => {
        modelEntered = resolve;
      });
      let letModelAnswer!: () => void;
      const answerAllowed = new Promise<void>((resolve) => {
        letModelAnswer = resolve;
      });
      const create = vi.spyOn(Anthropic.Messages.prototype, 'create').mockImplementation((async (body: { system?: unknown }) => {
        if (Array.isArray(body.system)) {
          modelEntered();
          await answerAllowed;
          return { content: [{ type: 'text', text: 'A workshops page sounds right.' }], usage: { input_tokens: 1, output_tokens: 1 } };
        }
        return { content: [{ type: 'text', text: '[]' }], usage: { input_tokens: 1, output_tokens: 1 } };
      }) as never);
      process.env.ANTHROPIC_API_KEY = ['not', 'a', 'key', run].join('-');
      companionClaude._resetClientForTests();
      memoryCapture._resetClientForTests();
      try {
        const answered = second.agent.post('/lighthouse/table/turn').send({ keeperText: said }).then((res) => res);
        await inTheModel;
        await releasedAsTheEarlierCodeDid(second.trialId);
        letModelAnswer();
        const res = await answered;
        // The turn has nowhere to go (the trial is released and is nobody's keeper): it is an error, not a write into someone else's conversation.
        expect(res.status).not.toBe(200);
      } finally {
        letModelAnswer();
        create.mockRestore();
        delete process.env.ANTHROPIC_API_KEY;
        companionClaude._resetClientForTests();
        memoryCapture._resetClientForTests();
      }
      expect((await turnsOf('keeper_id', first.payer.id)).map((turn) => turn.keeper_text)).not.toContain(said);
      expect((await q('SELECT 1 FROM companion_turns WHERE keeper_text = $1', [said]))).toEqual([]);
    });

    it('a session that names a trial which is not the one stamped on its build is not applied: nobody is promoted, the money is recorded and flagged', async () => {
      const t = await trialWithPreview('h1wrong');
      const stranger = await trialBrowser();
      await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, 'stranger turn', 'ok')`, [stranger.trialId]);
      const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: t.buildId, trialId: stranger.trialId, email: t.payer.email, tag: 'h1wrong' }));
      expect((await deliver(event)).status).toBe(200);

      expect(await trialRow(stranger.trialId)).toEqual({ released: false, promoted_customer_id: null });
      expect(await trialRow(t.trialId)).toEqual({ released: false, promoted_customer_id: null });
      expect(await q('SELECT 1 FROM keepers WHERE customer_id = $1', [t.payer.id])).toEqual([]);
      expect((await buildRow(t.buildId)).status).toBe('preview');
      expect(await turnsOf('trial_id', stranger.trialId)).toHaveLength(1);
      expect(await subscriptionRow('h1wrong')).toMatchObject({ status: 'active', keeper_customer_id: t.payer.id });
      await vi.waitFor(async () => expect(await paymentAlerts('h1wrong')).toHaveLength(1));
    });

    it('the account\'s own keeper, whose subscription has ended, pays again for a website that has a trial stamped on it: the keeper takes the new subscription and the website goes live, and the trial is not merged into the keeper\'s conversation', async () => {
      const { first, second } = await twoTrialsOnOneAccount('rsown');
      await pay(first, 'rsown1');
      await q(`UPDATE keepers SET subscription_status = 'canceled' WHERE customer_id = $1`, [first.payer.id]);
      const owner = await loggedInElsewhere(first.payer);

      // Through the route, as the account's owner: Checkout carries the stamped trial and the keeper's id.
      const checkout = standInForCheckout();
      let ask: CheckoutAsk;
      try {
        const res = await owner
          .post(`/lighthouse/lamp/${second.buildId}/make-public`)
          .type('form')
          .send({ _csrf: await freshCsrf(owner), monthly_amount_cents: '3900' });
        expect(res.status).toBe(303);
        ask = checkout.asks[0];
        expect(ask).toMatchObject({ trialId: second.trialId, keeperCustomerId: first.payer.id, buildId: second.buildId });
      } finally {
        checkout.restore();
      }
      expect((await deliver(paidEventFor(ask, 'rsown2'))).status).toBe(200);

      expect(await keeperRow(first.payer.id)).toMatchObject({ stripe_subscription_id: `sub_${run}_rsown2`, subscription_status: 'active' });
      expect((await buildRow(second.buildId)).status).toBe('live');
      // The trial's conversation was not merged into the keeper's; the trial is still a trial.
      expect(await trialRow(second.trialId)).toEqual({ released: false, promoted_customer_id: null });
      expect((await turnsOf('trial_id', second.trialId)).map((turn) => turn.keeper_text)).toEqual([second.spoken]);
      expect((await turnsOf('keeper_id', first.payer.id)).map((turn) => turn.keeper_text)).not.toContain(second.spoken);
      expect(await paymentAlerts('rsown2')).toEqual([]);
      // And the trial's browser is no keeper.
      expect((await second.agent.get('/lighthouse/table')).text).not.toContain(first.secret);
    });

    describe('a payment with no trial to promote (a keeper paying for another website)', () => {
      /** A keeper with a subscription in this status, and a new approved Harbor preview on the account. */
      async function keeperWithSubscription(tag: string, status: string) {
        const t = await trialWithPreview(`${tag}k`);
        await pay(t, `${tag}k`);
        await q('UPDATE keepers SET subscription_status = $1 WHERE customer_id = $2', [status, t.payer.id]);
        const buildId = await createBuild(t.payer.id, { harbor: true });
        return { ...t, nextBuildId: buildId, firstSubscription: `sub_${run}_${tag}k` };
      }

      it.each(['active', 'trialing', 'past_due'])(
        'the keeper\'s subscription is %s: a second payment is recorded and flagged, not made the keeper\'s subscription, and the website stays in preview',
        async (status) => {
          const k = await keeperWithSubscription(`np${status.replace('_', '')}`, status);
          const tag = `np${status.replace('_', '')}2`;
          const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: k.nextBuildId, email: k.payer.email, tag }));
          expect((await deliver(event)).status).toBe(200);
          expect(await keeperRow(k.payer.id)).toMatchObject({ stripe_subscription_id: k.firstSubscription, subscription_status: status });
          expect((await buildRow(k.nextBuildId)).status).toBe('preview');
          expect(await subscriptionRow(tag)).toMatchObject({ status: 'active', keeper_customer_id: k.payer.id });
          await vi.waitFor(async () => expect(await paymentAlerts(tag)).toHaveLength(1));
        }
      );

      it.each(['canceled', 'unpaid', 'paused', 'incomplete', 'incomplete_expired'])(
        'the keeper\'s subscription is %s: the new subscription replaces it and the website goes live, as the route\'s own rule allows',
        async (status) => {
          const k = await keeperWithSubscription(`rs${status.replace('_', '')}`, status);
          const tag = `rs${status.replace('_', '')}2`;
          const event = stripeEvent('checkout.session.completed', checkoutSession({ buildId: k.nextBuildId, email: k.payer.email, tag }));
          expect((await deliver(event)).status).toBe(200);
          expect(await keeperRow(k.payer.id)).toMatchObject({ stripe_subscription_id: `sub_${run}_${tag}`, subscription_status: 'active' });
          expect((await buildRow(k.nextBuildId)).status).toBe('live');
          expect(await paymentAlerts(tag)).toEqual([]);
        }
      );
    });

    it('the development bypass will not promote a trial into an account that already has a subscription, and leaves its keeper row alone', async () => {
      const t = await trialWithPreview('h1byp');
      await pay(t, 'h1byp');
      const owner = await loggedInElsewhere(t.payer);
      const before = await keeperRow(t.payer.id);
      process.env.CAS_HARBOR_STRIPE_BYPASS = 'true';
      try {
        // The logged-in browser has a trial cookie of its own (every /lighthouse visitor does).
        await owner.get('/lighthouse/table');
        const res = await owner.post('/lighthouse/bypass').send({ email: t.payer.email });
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('account_already_subscribed');
      } finally {
        delete process.env.CAS_HARBOR_STRIPE_BYPASS;
      }
      expect(await keeperRow(t.payer.id)).toEqual(before);
    });
  });

  // -------------------------------------------------------------------------
  describe('arriving at preview: the worker and the status hook put a build into review the same way', () => {
    // [CAS_HARBOR_AUTO_REVIEW as written, the review_status both writers give]
    const SETTINGS: Array<[string | undefined, string]> = [
      [undefined, 'pending'],
      ['', 'pending'],
      ['false', 'pending'],
      ['tru', 'pending'],
      ['true', 'ai_review_queued'],
      ['1', 'ai_review_queued'],
      ['yes', 'ai_review_queued'],
      [' TRUE ', 'ai_review_queued'],
    ];

    workerIt.each(SETTINGS)('CAS_HARBOR_AUTO_REVIEW=%j: the worker\'s preview write and POST /webhooks/build-status leave the same review_status, %s', async (setting, expected) => {
      const owner = await createCustomer(`arrive-${crypto.randomBytes(3).toString('hex')}`);
      const landing = async (): Promise<string> => {
        const id = await createBuild(owner.id, { status: 'deploying', review: null });
        await q('UPDATE builds SET started_at = NOW() WHERE id = $1', [id]);
        return id;
      };
      const viaWorker = await landing();
      runWorkerPreview(viaWorker, { CAS_HARBOR_AUTO_REVIEW: setting });

      const viaHook = await landing();
      const saved = process.env.CAS_HARBOR_AUTO_REVIEW;
      if (setting === undefined) delete process.env.CAS_HARBOR_AUTO_REVIEW;
      else process.env.CAS_HARBOR_AUTO_REVIEW = setting;
      try {
        const res = await request(app)
          .post('/webhooks/build-status')
          .set('Authorization', `Bearer ${WORKER_SECRET}`)
          .send({ build_id: viaHook, status: 'preview', preview_url: 'https://hook-landed.example.test' });
        expect(res.status).toBe(200);
      } finally {
        if (saved === undefined) delete process.env.CAS_HARBOR_AUTO_REVIEW;
        else process.env.CAS_HARBOR_AUTO_REVIEW = saved;
      }

      const fromWorker = await buildRow(viaWorker);
      const fromHook = await buildRow(viaHook);
      expect(fromWorker).toMatchObject({ status: 'preview', review_status: expected, preview_url: 'https://worker-landed.example.test' });
      expect(fromHook).toMatchObject({ status: 'preview', review_status: expected, preview_url: 'https://hook-landed.example.test' });
      // Neither writer leaves a verdict, and neither publishes.
      for (const row of [fromWorker, fromHook]) {
        expect(row).toMatchObject({ reviewed_at: null, reviewed_by: null, review_notes: null, live_url: null, stripe_session_id: null });
      }
    });
  });

  // -------------------------------------------------------------------------
  describe('every password hash the application stores has one cost, the cost of the throwaway hash that login compares an unknown address against', () => {
    it('signup, the verification link opened elsewhere, a reset, an account made at checkout, by the two dispatches and by the bypass, and the throwaway hash itself', async () => {
      const passwordHashes = await import('../../src/utils/password-hash');
      const costOf = (hash: string): number => bcrypt.getRounds(hash);
      const hashOf = async (address: string): Promise<string> =>
        (await one<{ password: string }>('SELECT password FROM customers WHERE email = $1', [address])).password;
      const found: Record<string, number> = {};

      // Signup: the password the visitor typed.
      const signupAddress = email('cost-signup');
      const typist = browser();
      await typist.post('/signup').type('form').send({ _csrf: await freshCsrf(typist), email: signupAddress, password: PASSWORD, name: 'Cost Signup' });
      found.signup = costOf(await hashOf(signupAddress));

      // The verification link opened in a browser that made no claim: the password becomes one nobody knows.
      await vi.waitFor(() => expect(latestDevLink('Verification link')).toContain('/verify-email?token='));
      const stranger = browser();
      expect((await stranger.get(latestDevLink('Verification link'))).status).toBe(302);
      found.verificationOpenedElsewhere = costOf(await hashOf(signupAddress));

      // A reset: the password the owner chose.
      const resetter = browser();
      await resetter.post('/forgot-password').type('form').send({ _csrf: await freshCsrf(resetter), email: signupAddress });
      await vi.waitFor(() => expect(latestDevLink('Password reset link')).toContain('/reset-password?token='));
      const resetToken = new URL(latestDevLink('Password reset link'), 'http://localhost').searchParams.get('token') as string;
      const chosen = `chosen-${crypto.randomBytes(9).toString('hex')}`;
      await resetter.post('/reset-password').type('form').send({ _csrf: await freshCsrf(resetter), token: resetToken, password: chosen });
      found.reset = costOf(await hashOf(signupAddress));

      // An account made for a payer whose build had none.
      const payerAddress = email('cost-payer');
      const buildId = await createBuild(null, { harbor: true });
      expect((await deliver(stripeEvent('checkout.session.completed', checkoutSession({ buildId, email: payerAddress, tag: 'cost' })))).status).toBe(200);
      found.checkout = costOf(await hashOf(payerAddress));

      // The two dispatches, which make an account for the address a conversation gave.
      const extraction = standInForExtraction();
      try {
        const turnsFor = async (address: string) => {
          const visitor = await trialBrowser();
          await q('UPDATE trial_keepers SET email_captured = $1, is_synthetic = FALSE WHERE trial_id = $2', [address, visitor.trialId]);
          await q(
            `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
             SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
            [visitor.trialId]
          );
          return visitor;
        };
        const selfServeAddress = email('cost-dispatch');
        const visitor = await turnsFor(selfServeAddress);
        expect((await visitor.agent.post('/lighthouse/build').send({})).status).toBe(200);
        found.visitorDispatch = costOf(await hashOf(selfServeAddress));

        const operatorAddress = email('cost-operator');
        const forOperator = await turnsFor(operatorAddress);
        const admin = await loggedIn(ADMIN_EMAIL);
        expect((await admin.post(`/admin/harbor/${forOperator.trialId}/dispatch`).set('x-csrf-token', await freshCsrf(admin))).status).toBe(200);
        found.operatorDispatch = costOf(await hashOf(operatorAddress));
      } finally {
        extraction.restore();
      }

      // The development bypass, which makes a verified account without a password anyone knows.
      const bypassAddress = email('cost-bypass');
      const bypasser = await trialBrowser();
      process.env.CAS_HARBOR_STRIPE_BYPASS = 'true';
      try {
        expect((await bypasser.agent.post('/lighthouse/bypass').send({ email: bypassAddress })).status).toBe(200);
      } finally {
        delete process.env.CAS_HARBOR_STRIPE_BYPASS;
      }
      found.bypass = costOf(await hashOf(bypassAddress));

      // The hash login compares an unknown address against.
      found.throwawayForUnknownAddresses = costOf(await passwordHashes.getDummyPasswordHash());

      // One cost everywhere, and it is the one the real passwords have always had.
      expect(passwordHashes.BCRYPT_ROUNDS).toBe(12);
      expect(Object.keys(found)).toHaveLength(8);
      expect(found).toEqual(Object.fromEntries(Object.keys(found).map((path) => [path, passwordHashes.BCRYPT_ROUNDS])));
    }, 60_000);
  });

  // -------------------------------------------------------------------------
  describe('the legacy intake routes read the build columns they use, and not the source tarball or the database credential', () => {
    it('GET /intake/:buildId and POST /intake/message: the rows Postgres sends back to the web process carry neither', async () => {
      const owner = await createCustomer('intake-cols');
      const agent = await loggedIn(owner.email);
      const buildId = await createBuild(owner.id, { status: 'intake', review: null });
      await q('UPDATE builds SET build_artifact = $1, railway_database_url = $2 WHERE id = $3', [
        Buffer.from(`tarball-${run}`),
        `postgres://intake-cols-${run}.invalid/db`,
        buildId,
      ]);

      // The columns of every result the application's pool returns for a statement on builds.
      const original = appDbPool.query.bind(appDbPool) as (...args: unknown[]) => Promise<{ fields?: Array<{ name: string }> }>;
      const columnSets: string[][] = [];
      const spy = vi.spyOn(appDbPool, 'query').mockImplementation(((...args: unknown[]) =>
        original(...args).then((result) => {
          const text = typeof args[0] === 'string' ? args[0] : String((args[0] as { text?: string }).text);
          if (/FROM builds\b/.test(text) && !/customers|session/i.test(text)) columnSets.push((result.fields || []).map((field) => field.name));
          return result;
        })) as never);
      const flag = process.env.CAS_HARBOR_ENABLED;
      process.env.CAS_HARBOR_ENABLED = 'false';
      try {
        const page = await agent.get(`/intake/${buildId}`);
        expect(page.status).toBe(200);
        expect(page.text).toContain('Describe Your Website');
        const pageColumns = columnSets.splice(0);
        expect(pageColumns.length).toBeGreaterThan(0);

        const said = await agent
          .post('/intake/message')
          .set('x-csrf-token', await freshCsrf(agent))
          .send({ buildId, message: 'I need a booking page' });
        // No model key on this server: the route got as far as the model, so the build was read.
        expect(said.status).toBe(503);
        const messageColumns = columnSets.splice(0);
        expect(messageColumns.length).toBeGreaterThan(0);

        for (const columns of [...pageColumns, ...messageColumns]) {
          expect(columns).not.toContain('build_artifact');
          expect(columns).not.toContain('railway_database_url');
        }
        // What the page needs is there.
        expect(pageColumns.some((columns) => ['id', 'customer_id', 'status', 'tier', 'spec'].every((name) => columns.includes(name)))).toBe(true);
      } finally {
        if (flag === undefined) delete process.env.CAS_HARBOR_ENABLED;
        else process.env.CAS_HARBOR_ENABLED = flag;
        spy.mockRestore();
      }
    });
  });

  // -------------------------------------------------------------------------
  describe('a conversation\'s first build is a build start: the same lock, the same two limits, the same ledger', () => {
    /** A trial ready to dispatch for this address: enough turns, the address captured. */
    async function readyTrial(address: string) {
      const visitor = await trialBrowser();
      // The test client sends no browser User-Agent, so the trial was minted
      // as a bot's; a person's is what these tests are about.
      await q('UPDATE trial_keepers SET email_captured = $1, is_synthetic = FALSE WHERE trial_id = $2', [address, visitor.trialId]);
      await q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
        [visitor.trialId]
      );
      return visitor;
    }
    /** A logged-in account's browser, holding its own trial with enough turns to build from. */
    async function loggedInTrialBrowser(address: string): Promise<{ agent: Browser; trialId: string }> {
      const agent = await loggedIn(address);
      const page = await agent.get('/lighthouse/table');
      expect(page.status).toBe(200);
      const cookie = ([] as string[]).concat(page.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
      const token = String(cookie).split(';')[0].split('=')[1];
      const row = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [token]);
      await q('UPDATE trial_keepers SET is_synthetic = FALSE WHERE trial_id = $1', [row.trial_id]);
      await q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
        [row.trial_id]
      );
      return { agent, trialId: row.trial_id };
    }
    const dispatch = (trialId: string, address: string) =>
      harborDispatch.dispatchHarborTrialBuild(appDbPool, { trialId, resolvedEmail: address, source: 'self_serve', refuseBarrenSpec: true });
    const ledgerOf = (customerId: string) =>
      q<{ build_id: string; kind: string }>('SELECT build_id, kind FROM build_start_events WHERE customer_id = $1 ORDER BY id', [customerId]);
    const activeOf = async (customerId: string): Promise<number> =>
      Number(
        (await one<{ n: string }>(`SELECT COUNT(*) AS n FROM builds WHERE customer_id = $1 AND status IN ('queued', 'building', 'testing', 'deploying')`, [customerId])).n
      );

    it('through the route: the build is queued for the address, the conversation is stamped, and the start is in the ledger', async () => {
      const address = email('first-build');
      const visitor = await readyTrial(address);
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(200);
        const customer = await one<{ id: string; email_verified: boolean }>('SELECT id, email_verified FROM customers WHERE email = $1', [address]);
        expect(customer.email_verified).toBe(false);
        expect(await buildRow(res.body.buildId)).toMatchObject({ status: 'queued', customer_id: customer.id });
        // A kind of its own, which the schema's CHECK admits.
        expect(await ledgerOf(customer.id)).toEqual([{ build_id: res.body.buildId, kind: 'dispatch' }]);
        expect(harborDispatch.HARBOR_DISPATCH_START_KIND).toBe('dispatch');
        expect(extraction.calls()).toBe(1);
        // The row counts as a start and as nothing else. The web app's own
        // reader, asked about this build: one start today, one build active,
        // and no manual retry of it (retries are 'retry' rows of a build).
        expect(await buildStartLimits.readBuildStartBudget(appDbPool, customer.id, res.body.buildId)).toEqual({
          active: 1,
          startsToday: 1,
          retriesOfThisBuild: 0,
        });
        // The coordinator's lineage bound counts 'rebuild' rows filed under a
        // lineage's root (coordinator/actuators.py, queue_rebuild), and this
        // build is a root: the dispatch must not be one of those either.
        const asAutomaticRebuild = await one<{ n: string }>(
          `SELECT COUNT(*) AS n FROM build_start_events WHERE build_id = $1 AND kind = 'rebuild'`,
          [res.body.buildId]
        );
        expect(Number(asAutomaticRebuild.n)).toBe(0);
      } finally {
        extraction.restore();
      }
    });

    it('an address the build engine would refuse as the site admin address: no build, no ledger row, no account and no model call, for a visitor, an account owner and the operator', async () => {
      // The worker writes the owning account's email into the spec, and the
      // engine refuses an apostrophe, an underscore in the host or a letter
      // outside ASCII there. Signup and the conversation's email field accept
      // all three.
      const refused = [`o'brien-${run}@example.test`, `maya-${run}@my_studio.example.test`, `jos${String.fromCharCode(233)}-${run}@example.test`];
      const totals = async () =>
        one<{ builds: string; ledger: string; customers: string }>(
          `SELECT (SELECT COUNT(*) FROM builds) AS builds, (SELECT COUNT(*) FROM build_start_events) AS ledger,
                  (SELECT COUNT(*) FROM customers) AS customers`
        );
      const trialState = (trialId: string) =>
        one<{ email_captured: string | null; handoff_build_id: string | null; handoff_at: Date | null }>(
          'SELECT email_captured, handoff_build_id, handoff_at FROM trial_keepers WHERE trial_id = $1',
          [trialId]
        );
      const admin = await loggedIn(ADMIN_EMAIL);
      const extraction = standInForExtraction();
      try {
        const before = await totals();

        // What a visitor is told when the address has a verified account: the yardstick.
        const taken = await createCustomer('engine-taken');
        const takenBody = (await (await readyTrial(taken.email)).agent.post('/lighthouse/build').send({})).body;
        expect(takenBody.error).toBe('email_not_usable');

        const visitors: Array<Awaited<ReturnType<typeof readyTrial>>> = [];
        for (const address of refused) {
          // A visitor, by the trial cookie alone, with the address captured earlier.
          const visitor = await readyTrial(address);
          const res = await visitor.agent.post('/lighthouse/build').send({});
          expect(res.status, address).toBe(409);
          expect(res.body, address).toEqual(takenBody);
          // The refused address is forgotten and the conversation is not used up.
          expect(await trialState(visitor.trialId)).toEqual({ email_captured: null, handoff_build_id: null, handoff_at: null });
          expect(await q('SELECT 1 FROM customers WHERE email = $1', [address])).toEqual([]);

          // The operator's manual dispatch of a trial that captured it.
          const forOperator = await readyTrial(address);
          const manual = await admin.post(`/admin/harbor/${forOperator.trialId}/dispatch`).set('x-csrf-token', await freshCsrf(admin));
          expect(manual.status, address).toBe(409);
          expect(manual.body.error).toBe('email_not_buildable');
          expect(manual.body.message).toContain(address);
          expect(await trialState(forOperator.trialId)).toMatchObject({ handoff_build_id: null, handoff_at: null });
          visitors.push(visitor);
        }

        // An account that exists under such an address (signup accepts it):
        // its owner, logged in, with a build-ready conversation of their own.
        const ownerAddress = `owner.o'neil-${run}@example.test`;
        const owner = await one<{ id: string }>(
          `INSERT INTO customers (email, password, name, email_verified) VALUES ($1, $2, 'owner', TRUE) RETURNING id`,
          [ownerAddress, passwordHash]
        );
        const ownerBrowser = await loggedIn(ownerAddress);
        const firstVisit = await ownerBrowser.get('/lighthouse/table');
        const cookie = ([] as string[]).concat(firstVisit.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
        const ownerTrial = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [
          String(cookie).split(';')[0].split('=')[1],
        ]);
        await q('UPDATE trial_keepers SET is_synthetic = FALSE WHERE trial_id = $1', [ownerTrial.trial_id]);
        await q(
          `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
           SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
          [ownerTrial.trial_id]
        );
        // The page says why instead of offering the button...
        const page = await ownerBrowser.get('/lighthouse/table');
        expect(page.status).toBe(200);
        expect(page.text).toContain('id="build-blocked"');
        expect(page.text).toContain('the builder does not accept its email address');
        expect(page.text).not.toContain('Build my free preview');
        // ...and the route refuses all the same, in plain words, whatever is typed.
        const asOwner = await ownerBrowser.post('/lighthouse/build').send({ email: email('engine-typed-instead') });
        expect(asOwner.status).toBe(409);
        expect(asOwner.body.error).toBe('email_not_buildable');
        expect(asOwner.body.message).toMatch(/A website cannot be built for this account/);
        expect(asOwner.body.requiresEmail).toBeUndefined();
        expect(await q('SELECT 1 FROM builds WHERE customer_id = $1', [owner.id])).toEqual([]);

        // Nothing was spent and nothing was written by any of it: the model
        // stand-in was never called, and the only new rows are the two
        // accounts this test made itself.
        expect(extraction.calls()).toBe(0);
        const after = await totals();
        expect(after).toEqual({ ...before, customers: String(Number(before.customers) + 2) });

        // The visitor gives an address the engine takes, and gets the build.
        const second = email('engine-second-try');
        const again = await visitors[0].agent.post('/lighthouse/build').send({ email: second });
        expect(again.status).toBe(200);
        expect(extraction.calls()).toBe(1);
        const customer = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [second]);
        expect(await buildRow(again.body.buildId)).toMatchObject({ status: 'queued', customer_id: customer.id });
        expect(await ledgerOf(customer.id)).toEqual([{ build_id: again.body.buildId, kind: 'dispatch' }]);
      } finally {
        extraction.restore();
      }
    });

    it('a customer with two builds in progress gets no third from a new conversation of its own, and no model call is paid to find that out', async () => {
      // The account is logged in, so the dispatch is its own (not an anonymous
      // one): an anonymous visitor's request for an account in use is refused
      // as address_in_use before any limit is read (paid-trial-identity.realdb.test.ts).
      const busy = await createCustomer('busy');
      await createBuild(busy.id, { status: 'building', review: null });
      await createBuild(busy.id, { status: 'queued', review: null });
      const visitor = await loggedInTrialBrowser(busy.email);
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(429);
        expect(res.body.error).toBe('build_limit');
        expect(res.body.message).toMatch(/builds in progress/);
        expect(extraction.calls()).toBe(0);
      } finally {
        extraction.restore();
      }
      expect(await activeOf(busy.id)).toBe(2);
      expect(await ledgerOf(busy.id)).toEqual([]);
      // The conversation was not used up by the refusal.
      const trial = await one<{ handoff_build_id: string | null; handoff_at: Date | null }>(
        'SELECT handoff_build_id, handoff_at FROM trial_keepers WHERE trial_id = $1',
        [visitor.trialId]
      );
      expect(trial).toEqual({ handoff_build_id: null, handoff_at: null });
    });

    it('two conversations dispatching at once for a customer with one build in progress: exactly one gets a build', async () => {
      const racing = await createCustomer('racing', { verified: false });
      await createBuild(racing.id, { status: 'building', review: null });
      const [a, b] = [await readyTrial(racing.email), await readyTrial(racing.email)];
      const extraction = standInForExtraction();
      try {
        const results = await Promise.allSettled([dispatch(a.trialId, racing.email), dispatch(b.trialId, racing.email)]);
        expect(results.map((r) => r.status).sort()).toEqual(['fulfilled', 'rejected']);
        const refused = results.find((r) => r.status === 'rejected') as PromiseRejectedResult;
        expect(refused.reason).toMatchObject({ code: 'build_limit' });
      } finally {
        extraction.restore();
      }
      expect(await activeOf(racing.id)).toBe(2);
      expect(await ledgerOf(racing.id)).toHaveLength(1);
    });

    it('the tenth start of the day is the last, whichever route makes it: a dispatch counts against the owner\'s retry, and nine retries against a dispatch', async () => {
      const owner = await createCustomer('tenth');
      const failed = await createBuild(owner.id, { status: 'failed', review: null });
      await q(
        `INSERT INTO build_start_events (customer_id, build_id, kind) SELECT $1, $2, 'rebuild' FROM generate_series(1, 9)`,
        [owner.id, uuid()]
      );
      const ownerBrowser = await loggedIn(owner.email);

      // Start number ten is a dispatch. (The owner's address is verified, so
      // the operator's flag stands in for the proof a session would give.)
      const visitor = await readyTrial(owner.email);
      const extraction = standInForExtraction();
      let dispatched: string;
      try {
        dispatched = (
          await harborDispatch.dispatchHarborTrialBuild(appDbPool, {
            trialId: visitor.trialId,
            resolvedEmail: owner.email,
            source: 'self_serve',
            allowVerifiedAccount: true,
          })
        ).buildId;
        expect(await ledgerOf(owner.id)).toHaveLength(10);

        // routes/builds.ts reads the same ledger: the retry is now refused.
        const retry = await ownerBrowser.post(`/builds/${failed}/retry`).type('form').send({ _csrf: await freshCsrf(ownerBrowser) });
        expect(retry.status).toBe(429);
        expect((await buildRow(failed)).status).toBe('failed');

        // And an eleventh start by dispatch is refused too, before the extraction.
        const second = await readyTrial(owner.email);
        const before = extraction.calls();
        await expect(
          harborDispatch.dispatchHarborTrialBuild(appDbPool, { trialId: second.trialId, resolvedEmail: owner.email, source: 'self_serve', allowVerifiedAccount: true })
        ).rejects.toMatchObject({ code: 'build_limit' });
        expect(extraction.calls()).toBe(before);
      } finally {
        extraction.restore();
      }
      expect((await buildRow(dispatched)).status).toBe('queued');
    });

    it('one lock for both: while the shared lock statement is held for a customer, the owner\'s retry (routes/builds.ts) waits, and a dispatch waits', async () => {
      const owner = await createCustomer('samelock');
      const failed = await createBuild(owner.id, { status: 'failed', review: null });
      const ownerBrowser = await loggedIn(owner.email);
      const csrf = await freshCsrf(ownerBrowser);
      const visitor = await readyTrial(owner.email);

      const holder = new Client({ connectionString: integrationUrl });
      await holder.connect();
      const extraction = standInForExtraction();
      try {
        await holder.query('BEGIN');
        await holder.query(buildStartLimits.BUILD_START_LOCK_SQL, [owner.id]);

        let retryDone = false;
        const retry = ownerBrowser
          .post(`/builds/${failed}/retry`)
          .type('form')
          .send({ _csrf: csrf })
          .then((res) => {
            retryDone = true;
            return res;
          });
        let dispatchDone = false;
        const dispatching = harborDispatch
          .dispatchHarborTrialBuild(appDbPool, { trialId: visitor.trialId, resolvedEmail: owner.email, source: 'self_serve', allowVerifiedAccount: true })
          .then((res) => {
            dispatchDone = true;
            return res;
          });

        await new Promise((resolve) => setTimeout(resolve, 600));
        expect(retryDone).toBe(false);
        expect(dispatchDone).toBe(false);
        expect((await buildRow(failed)).status).toBe('failed');

        await holder.query('COMMIT');
        expect((await retry).status).toBe(302);
        expect((await dispatching).alreadyDispatched).toBe(false);
      } finally {
        await holder.query('ROLLBACK').catch(() => undefined);
        await holder.end();
        extraction.restore();
      }
      expect(await ledgerOf(owner.id)).toHaveLength(2);
    });
  });

  // -------------------------------------------------------------------------
  describe('the address typed for a preview: what the build route says, and what it costs to ask', () => {
    async function readyVisitor() {
      const visitor = await trialBrowser();
      // As in readyTrial above: a person's trial, not a bot's.
      await q('UPDATE trial_keepers SET is_synthetic = FALSE WHERE trial_id = $1', [visitor.trialId]);
      await q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
        [visitor.trialId]
      );
      return visitor;
    }

    it('an address that has a verified account: refused before any model call, in words that do not say so, and the address is forgotten', async () => {
      const victim = await createCustomer('typed-victim');
      const visitor = await readyVisitor();
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({ email: victim.email });
        expect(res.status).toBe(409);
        // Nothing was spent to give this answer.
        expect(extraction.calls()).toBe(0);
        expect(res.body.error).toBe('email_not_usable');
        expect(res.body.requiresEmail).toBe(true);
        expect(JSON.stringify(res.body)).not.toMatch(/account_exists|already exists|verified/i);
      } finally {
        extraction.restore();
      }
      expect((await q('SELECT 1 FROM builds WHERE customer_id = $1', [victim.id])).length).toBe(0);
      const trial = await one<{ email_captured: string | null; handoff_at: Date | null }>(
        'SELECT email_captured, handoff_at FROM trial_keepers WHERE trial_id = $1',
        [visitor.trialId]
      );
      expect(trial).toEqual({ email_captured: null, handoff_at: null });
    });

    it('a logged-in customer who is not a keeper builds the preview into the account they are logged in to, whatever is typed', async () => {
      const member = await createCustomer('member');
      const someoneElse = await createCustomer('member-other');
      const visitor = await readyVisitor();
      const login = await visitor.agent.post('/login').type('form').send({ _csrf: await freshCsrf(visitor.agent), email: member.email, password: PASSWORD });
      expect(login.status).toBe(302);
      // Not a keeper: the conversation page still shows the trial's conversation and offers the preview.
      expect((await visitor.agent.get('/lighthouse/table')).text).toContain('Build my free preview');

      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({ email: someoneElse.email });
        expect(res.status).toBe(200);
        expect(await buildRow(res.body.buildId)).toMatchObject({ status: 'queued', customer_id: member.id });
        expect((await q('SELECT 1 FROM builds WHERE customer_id = $1', [someoneElse.id])).length).toBe(0);
        const stamped = await one<{ handoff_build_id: string | null; email_captured: string | null }>(
          'SELECT handoff_build_id, email_captured FROM trial_keepers WHERE trial_id = $1',
          [visitor.trialId]
        );
        expect(stamped.handoff_build_id).toBe(res.body.buildId);
        // The conversation's address is the account's, not the one typed.
        expect(stamped.email_captured).toBe(member.email);
        // They own it through the account, on the lamp page and on their dashboard.
        expect((await visitor.agent.get(`/lighthouse/lamp/${res.body.buildId}`)).status).toBe(200);
      } finally {
        extraction.restore();
      }
    });
  });

  // -------------------------------------------------------------------------
  describe('a build request that cannot produce a build is answered as what it is, in JSON, on all three dispatch paths', () => {
    const totals = async () =>
      one<{ builds: string; ledger: string; customers: string }>(
        `SELECT (SELECT COUNT(*) FROM builds) AS builds, (SELECT COUNT(*) FROM build_start_events) AS ledger,
                (SELECT COUNT(*) FROM customers) AS customers`
      );
    const handoffOf = (trialId: string) =>
      one<{ handoff_build_id: string | null; handoff_at: Date | null }>(
        'SELECT handoff_build_id, handoff_at FROM trial_keepers WHERE trial_id = $1',
        [trialId]
      );
    const isJson = (res: { headers: Record<string, string> }): boolean => /^application\/json/.test(String(res.headers['content-type']));

    /** A person's trial (the test client has no browser User-Agent, so it was minted as a bot's). */
    async function personTrial(address?: string) {
      const visitor = await trialBrowser();
      await q('UPDATE trial_keepers SET is_synthetic = FALSE, email_captured = $2 WHERE trial_id = $1', [visitor.trialId, address ?? null]);
      return visitor;
    }
    const ordinaryTurns = (trialId: string, count: number) =>
      q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, $2::int) AS n`,
        [trialId, count]
      );
    /** Say the crisis sentence through the real turn route until `count` turns are stored with the crisis flag. */
    async function crisisTurns(visitor: { agent: Browser; trialId: string }, count: number): Promise<void> {
      for (let i = 0; i < count; i += 1) {
        const res = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT });
        expect(res.status).toBe(200);
        expect(res.body.railFlags).toContain('crisis');
      }
      await vi.waitFor(async () => {
        const stored = await one<{ n: string }>(
          `SELECT COUNT(*) AS n FROM companion_turns WHERE trial_id = $1 AND 'crisis' = ANY(rail_flags)`,
          [visitor.trialId]
        );
        expect(Number(stored.n)).toBe(count);
      });
    }
    /**
     * The model client's `create` method stood in for, for the length of one
     * test: it answers `reply`, or rejects when `reply` is an Error. The
     * extraction, the dispatch service and the routes around it are the
     * application's own.
     */
    async function withModelReply<T>(reply: string | Error, fn: (calls: () => number) => Promise<T>): Promise<T> {
      const Anthropic = (await import('@anthropic-ai/sdk')).default;
      const create = vi.spyOn(Anthropic.Messages.prototype, 'create').mockImplementation((async () => {
        if (reply instanceof Error) throw reply;
        return { content: [{ type: 'text', text: reply }], usage: { input_tokens: 1, output_tokens: 1 } };
      }) as never);
      process.env.ANTHROPIC_API_KEY = ['not', 'a', 'key', run].join('-');
      specExtraction._resetClientForTests();
      try {
        return await fn(() => create.mock.calls.length);
      } finally {
        create.mockRestore();
        delete process.env.ANTHROPIC_API_KEY;
        specExtraction._resetClientForTests();
      }
    }
    async function keeperBrowser(label: string) {
      const account = await createCustomer(label);
      await q(`INSERT INTO keepers (customer_id, companion_name, companion_assigned) VALUES ($1, 'Haven', 'Haven')`, [account.id]);
      return { account, agent: await loggedIn(account.email) };
    }

    it('a conversation of crisis messages only is not build-ready: the page offers no build, and the route says "not ready" before any model call or reservation', async () => {
      const visitor = await personTrial();
      await crisisTurns(visitor, 4);
      // Four turns are stored, which is the floor; none is one the extraction may read.
      expect(Number((await one<{ n: string }>('SELECT COUNT(*) AS n FROM companion_turns WHERE trial_id = $1', [visitor.trialId])).n)).toBe(4);

      const page = await visitor.agent.get('/lighthouse/table');
      expect(page.status).toBe(200);
      expect(page.text).not.toContain('Build my free preview');

      const before = await totals();
      const admittedBefore = llmBudget.anonymousCallsAdmitted();
      await withModelReply('{}', async (calls) => {
        const res = await visitor.agent.post('/lighthouse/build').send({ email: email('crisis-only') });
        expect(res.status).toBe(409);
        expect(isJson(res)).toBe(true);
        expect(res.body.error).toBe('not_ready');
        expect(res.body.message).toMatch(/talk a little more/);
        expect(res.body).toMatchObject({ turnCount: 0, minTurns: 4 });
        expect(calls()).toBe(0);
      });
      expect(await totals()).toEqual(before);
      expect(llmBudget.anonymousCallsAdmitted()).toBe(admittedBefore);
      expect(await handoffOf(visitor.trialId)).toEqual({ handoff_build_id: null, handoff_at: null });
      // The count the page and the route used, and what the extraction's own loader reads.
      expect(await specExtraction.countExtractableTurns(appDbPool, { trialId: visitor.trialId })).toBe(0);
      expect((await specExtraction.loadTrialContext(appDbPool, visitor.trialId)).turns).toEqual([]);
    });

    it('the floor counts exactly the turns the extraction reads: crisis turns and turns a sacred note came from do not count toward it', async () => {
      const visitor = await personTrial(email('floor-mixed'));
      await ordinaryTurns(visitor.trialId, 4);
      await crisisTurns(visitor, 2);
      // One of the four ordinary turns produced a sacred note; the extraction leaves that turn out too.
      const sacredSource = await one<{ turn_id: string }>(
        `SELECT turn_id FROM companion_turns WHERE trial_id = $1 AND keeper_text = 'I teach reiki, part 1'`,
        [visitor.trialId]
      );
      await q(
        `INSERT INTO keeper_memory (trial_id, partition, category, content, source_turn_id) VALUES ($1, 'sacred', 'fear', 'a private thing', $2)`,
        [visitor.trialId, sacredSource.turn_id]
      );

      // Six stored turns, three the extraction may read: below the floor of four.
      expect((await visitor.agent.get('/lighthouse/table')).text).not.toContain('Build my free preview');
      const extraction = standInForExtraction();
      try {
        const early = await visitor.agent.post('/lighthouse/build').send({});
        expect(early.status).toBe(409);
        expect(early.body.error).toBe('not_ready');
        expect(extraction.calls()).toBe(0);
        // The same three turns, by the extraction's own loader and by the count.
        const loaded = await specExtraction.loadTrialContext(appDbPool, visitor.trialId);
        expect(loaded.turns.map((t) => t.keeper_text).sort()).toEqual(['I teach reiki, part 2', 'I teach reiki, part 3', 'I teach reiki, part 4']);
        expect(await specExtraction.countExtractableTurns(appDbPool, { trialId: visitor.trialId })).toBe(loaded.turns.length);
        expect(await specExtraction.conversationBuildReadiness(appDbPool, { trialId: visitor.trialId })).toEqual({
          ready: false,
          extractableTurns: 3,
          minTurns: 4,
        });

        // One more ordinary turn reaches the floor: the page offers the build and the route dispatches it.
        await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, 'I also teach sound baths', 'Lovely.')`, [visitor.trialId]);
        expect((await visitor.agent.get('/lighthouse/table')).text).toContain('Build my free preview');
        const ready = await visitor.agent.post('/lighthouse/build').send({});
        expect(ready.status).toBe(200);
        expect(extraction.calls()).toBe(1);
        expect(await buildRow(ready.body.buildId)).toMatchObject({ status: 'queued' });
      } finally {
        extraction.restore();
      }
    });

    it('the operator dispatching a crisis-only conversation, and a keeper with nothing said yet: 409 with a plain sentence, no model call, nothing written', async () => {
      const captured = email('crisis-operator');
      const visitor = await personTrial(captured);
      await crisisTurns(visitor, 4);
      const admin = await loggedIn(ADMIN_EMAIL);
      const keeper = await keeperBrowser('keeper-empty');
      const before = await totals();

      await withModelReply('{}', async (calls) => {
        const manual = await admin.post(`/admin/harbor/${visitor.trialId}/dispatch`).set('x-csrf-token', await freshCsrf(admin));
        expect(manual.status).toBe(409);
        expect(isJson(manual)).toBe(true);
        expect(manual.body.error).toBe('nothing_to_extract');
        expect(manual.body.message).toMatch(/nothing in this conversation yet that a website can be built from/);
        expect(manual.body.message).toMatch(/crisis flag/);

        const own = await keeper.agent.post('/lighthouse/build').send({});
        expect(own.status).toBe(409);
        expect(isJson(own)).toBe(true);
        expect(own.body.error).toBe('nothing_to_extract');
        expect(own.body.message).toMatch(/talk a little more/);
        expect(calls()).toBe(0);
      });

      // No build, no ledger row, and no account for the address the trial captured.
      expect(await totals()).toEqual(before);
      expect(await q('SELECT 1 FROM customers WHERE email = $1', [captured])).toEqual([]);
      expect(await handoffOf(visitor.trialId)).toEqual({ handoff_build_id: null, handoff_at: null });
      expect(await q('SELECT handoff_build_id, handoff_at FROM keepers WHERE customer_id = $1', [keeper.account.id])).toEqual([
        { handoff_build_id: null, handoff_at: null },
      ]);
    });

    it('a model reply that is not a spec: 502 with a plain sentence for the visitor, the keeper and the operator; no build, no ledger row, no account, and the conversation can try again', async () => {
      const address = email('bad-reply');
      const visitor = await personTrial(address);
      await ordinaryTurns(visitor.trialId, 5);
      const forOperator = await personTrial(email('bad-reply-operator'));
      await ordinaryTurns(forOperator.trialId, 5);
      const keeper = await keeperBrowser('keeper-bad-reply');
      await q(
        `INSERT INTO companion_turns (keeper_id, keeper_text, companion_text)
         SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
        [keeper.account.id]
      );
      const admin = await loggedIn(ADMIN_EMAIL);
      const before = await totals();
      const admittedBefore = llmBudget.anonymousCallsAdmitted();

      await withModelReply('I am sorry, I cannot produce that. Here is a poem instead.', async (calls) => {
        const asVisitor = await visitor.agent.post('/lighthouse/build').send({});
        expect(asVisitor.status).toBe(502);
        expect(isJson(asVisitor)).toBe(true);
        expect(asVisitor.body.error).toBe('unusable_reply');
        expect(asVisitor.body.message).toMatch(/Nothing was built\. Please try again/);
        expect(calls()).toBe(1);
        // The call was made and paid for, so the anonymous reservation it was
        // admitted under stays taken (see releaseAnonymousCalls).
        expect(llmBudget.anonymousCallsAdmitted()).toBe(admittedBefore + 1);

        const asKeeper = await keeper.agent.post('/lighthouse/build').send({});
        expect(asKeeper.status).toBe(502);
        expect(isJson(asKeeper)).toBe(true);
        expect(asKeeper.body.error).toBe('unusable_reply');

        const asOperator = await admin.post(`/admin/harbor/${forOperator.trialId}/dispatch`).set('x-csrf-token', await freshCsrf(admin));
        expect(asOperator.status).toBe(502);
        expect(isJson(asOperator)).toBe(true);
        expect(asOperator.body.error).toBe('unusable_reply');
        expect(calls()).toBe(3);
      });

      expect(await totals()).toEqual(before);
      expect(await q('SELECT 1 FROM customers WHERE email = ANY($1)', [[address, email('bad-reply-operator')]])).toEqual([]);
      for (const trialId of [visitor.trialId, forOperator.trialId]) {
        expect(await handoffOf(trialId)).toEqual({ handoff_build_id: null, handoff_at: null });
      }

      // Nothing was used up: with a usable reply the same conversation gets its build.
      const usable = JSON.stringify({
        spec_version: '1.0',
        app_name: `second-try-${run}`,
        tier: 'build',
        copy: { about_body: 'I teach reiki in Portland.' },
        pages: [{ name: 'Home', slug: '/', sections: [] }],
      });
      await withModelReply(usable, async () => {
        const again = await visitor.agent.post('/lighthouse/build').send({});
        expect(again.status).toBe(200);
        const customer = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [address]);
        expect(await buildRow(again.body.buildId)).toMatchObject({ status: 'queued', customer_id: customer.id });
      });
    });

    it('a model call that fails is a 502 too, and the provider\'s own error text stays in the server log', async () => {
      const visitor = await personTrial(email('call-failed'));
      await ordinaryTurns(visitor.trialId, 5);
      const before = await totals();
      await withModelReply(new Error(`upstream said no ${run}`), async (calls) => {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(502);
        expect(isJson(res)).toBe(true);
        expect(res.body.error).toBe('model_call_failed');
        expect(JSON.stringify(res.body)).not.toContain('upstream said no');
        expect(calls()).toBe(1);
      });
      expect(await totals()).toEqual(before);
    });

    it('a reservation for a call that was never made is given back: an anonymous request that ends before the model does not use up the anonymous share', async () => {
      // No model key on this server (the state this file runs in): the
      // extraction ends at "not configured", after the reservation was taken.
      const visitor = await personTrial(email('no-key'));
      await ordinaryTurns(visitor.trialId, 5);
      const admittedBefore = llmBudget.anonymousCallsAdmitted();
      const res = await visitor.agent.post('/lighthouse/build').send({});
      expect(res.status).toBe(503);
      expect(res.body.code).toBe('assistant_not_configured');
      expect(llmBudget.anonymousCallsAdmitted()).toBe(admittedBefore);
    });

    it('a fault nobody mapped still answers this route in JSON: the keeper\'s button, which posts no body, and the visitor\'s', async () => {
      const visitor = await personTrial(email('server-fault'));
      await ordinaryTurns(visitor.trialId, 5);
      const keeper = await keeperBrowser('keeper-fault');
      const spy = vi.spyOn(specExtraction, 'extractSpecFromConversation').mockRejectedValue(new Error(`an unmapped fault ${run}`));
      try {
        // The visitor's button sends a JSON body; the keeper's sends none, so
        // only the route itself says this is a JSON request.
        for (const res of [await visitor.agent.post('/lighthouse/build').send({}), await keeper.agent.post('/lighthouse/build')]) {
          expect(res.status).toBe(500);
          expect(isJson(res)).toBe(true);
          expect(res.body.code).toBe('server_error');
          expect(typeof res.body.message).toBe('string');
          expect(res.text).not.toMatch(/<html|<!DOCTYPE/i);
        }
      } finally {
        spy.mockRestore();
      }
    });
  });

  // -------------------------------------------------------------------------
  describe('the build\'s warnings reach the reviewer: the worker\'s log entries, read by the review page', () => {
    // The shapes below are the worker's, from queue/worker_pg.py:
    //   _manifest_warning_checks: every `warning: ` line of the manifest,
    //     then every `spec-warning: ` line; a line over 1,200 characters is
    //     cut and ends " ...(cut)"; each line is written with two leading
    //     spaces; the first 50 lines are one build_logs entry each and the
    //     rest are packed, up to 100 lines to an entry, joined by "\n";
    //   _run_pipeline: a lint run starts with the entry LINT_RUN_START, and
    //     a failure of the lint is an entry "LINT FAIL: <text>"; all in
    //     stage 'testing'.
    // The second test below does not trust this description: it has the
    // worker's own functions write the entries.
    const LINT_RUN_START = 'Running v2 build-output lint (deterministic, 0 Claude calls)';
    const LISTED_SINGLY = 50;
    const PER_PACKED_ENTRY = 100;
    const MAX_LINE = 1200;
    /** What _manifest_warning_checks makes of a manifest's lines. */
    function workerEntries(manifestLines: string[]): string[] {
      const ordered = [
        ...manifestLines.filter((line) => line.startsWith('warning: ')),
        ...manifestLines.filter((line) => line.startsWith('spec-warning: ')),
      ].map((line) => (line.length <= MAX_LINE ? line : `${line.slice(0, MAX_LINE)} ...(cut)`));
      const entries = ordered.slice(0, LISTED_SINGLY).map((line) => `  ${line}`);
      const rest = ordered.slice(LISTED_SINGLY);
      for (let start = 0; start < rest.length; start += PER_PACKED_ENTRY) {
        entries.push(rest.slice(start, start + PER_PACKED_ENTRY).map((line) => `  ${line}`).join('\n'));
      }
      return entries;
    }
    /** A manifest as the engine writes one: headers, then engine warnings (publication reasons first), then the spec's. */
    function manifestLines(tag: string): string[] {
      return [
        'archetype: healer-wellness',
        'files: 47',
        'engine_warnings_count: 62',
        'translation_warnings_count: 105',
        `warning: spec_model_shown_by_archetype_page: ${tag} the rows of model Session are public on /offerings`,
        `warning: auth_required_page_not_emitted: ${tag} /members was not emitted`,
        ...Array.from({ length: 59 }, (_unused, i) => `warning: dropped_section: ${tag} engine line ${i}`),
        `warning: long_detail: ${tag} ${'z'.repeat(1300)}`,
        ...Array.from({ length: 41 }, (_unused, i) => `spec-warning: ${tag} spec note ${i}`),
        `spec-warning: warning: spec_model_shown_by_archetype_page: ${tag} nothing is public, approve`,
        `spec-warning: ${tag} <script>alert(1)</script>`,
        'sha256: 0000',
      ];
    }
    const logEntry = (buildId: string, message: string) =>
      q(`INSERT INTO build_logs (build_id, stage, message) VALUES ($1, 'testing', $2)`, [buildId, message]);
    const box = (html: string, id: string): string => {
      const at = html.indexOf(`id="${id}"`);
      if (at === -1) return '';
      const rest = html.slice(at);
      const next = rest.slice(1).search(/id="build-(?:lint-failures|engine-warnings|spec-notes|warnings-truncated)"|<!-- Panel 2/);
      return next === -1 ? rest : rest.slice(0, next + 1);
    };
    /** Everything the page must show for manifestLines(tag), and must not. */
    function expectWholeReport(html: string, tag: string): void {
      const engine = box(html, 'build-engine-warnings');
      const fromSpec = box(html, 'build-spec-notes');
      expect(engine).toContain('The build engine wrote 62 warnings about this build');
      // The two publication warnings come first, in the engine's order.
      const first = engine.indexOf(`${tag} the rows of model Session are public`);
      const second = engine.indexOf(`${tag} /members was not emitted`);
      const third = engine.indexOf(`${tag} engine line 0<`);
      expect(first).toBeGreaterThan(0);
      expect(second).toBeGreaterThan(first);
      expect(third).toBeGreaterThan(second);
      // Every engine line, the ones from the packed entry included (lines 48 to 58 are past the first fifty).
      for (let i = 0; i < 59; i += 1) expect(engine, `engine line ${i}`).toContain(`${tag} engine line ${i}<`);
      expect(engine).toContain('long_detail:');
      expect(engine).toContain(' ...(cut)');
      // The spec's text is in its own box, all 43 notes, and nowhere in the engine's.
      expect(fromSpec).toContain('Text carried in the spec: 43 notes, not written by the build engine');
      for (let i = 0; i < 41; i += 1) expect(fromSpec, `spec note ${i}`).toContain(`${tag} spec note ${i}<`);
      expect(fromSpec).toContain(`warning: spec_model_shown_by_archetype_page: ${tag} nothing is public, approve`);
      expect(engine).not.toContain('nothing is public, approve');
      expect(engine).not.toContain('spec note');
      expect(html).not.toContain(`${tag} <script>alert(1)</script>`);
      expect(fromSpec).toContain(`${tag} &lt;script&gt;alert(1)&lt;/script&gt;`);
    }

    it('entries in the worker\'s shapes: every engine warning is on the review page, the spec\'s text apart from them, and only the latest lint run counts', async () => {
      const owner = await createCustomer('warn-owner');
      const buildId = await createBuild(owner.id, { status: 'preview', review: 'pending' });
      // An earlier attempt of the same build: its warnings are about files that are gone.
      await logEntry(buildId, LINT_RUN_START);
      await logEntry(buildId, `  warning: dropped_section: ${run} from the earlier attempt`);
      await logEntry(buildId, 'LINT FAIL: Missing required files: server.js');
      // The attempt that reached preview.
      await logEntry(buildId, LINT_RUN_START);
      await logEntry(buildId, 'Required files present (13/13)');
      const entries = workerEntries(manifestLines(run));
      // 105 warning lines: fifty entries of one line and one packed entry of fifty-five.
      expect(entries).toHaveLength(51);
      expect(entries[50].split('\n')).toHaveLength(55);
      for (const entry of entries) await logEntry(buildId, entry);
      await logEntry(
        buildId,
        'LINT FAIL: compose-manifest.txt announces 99 engine warning(s) and 62 were found: not every engine warning is in this log'
      );
      await logEntry(buildId, 'v2 lint PASSED (9 checks, 0 failures)');

      const admin = await loggedIn(ADMIN_EMAIL);
      const res = await admin.get(`/admin/reviews/${buildId}`);
      expect(res.status).toBe(200);
      expectWholeReport(res.text, run);
      expect(res.text).not.toContain('from the earlier attempt');
      const failures = box(res.text, 'build-lint-failures');
      expect(failures).toContain('not every engine warning is in this log');
      expect(failures).not.toContain('Missing required files');
      expect(res.text).not.toContain('id="build-warnings-truncated"');

      // A build with few warnings: each line is an entry of its own, so the
      // spec's note is an entry that starts with `spec-warning: `, and the
      // statement has to fetch that kind too.
      const small = await createBuild(owner.id, { status: 'preview', review: 'pending' });
      await logEntry(small, LINT_RUN_START);
      for (const entry of workerEntries([`warning: page_named_like_model: ${run} small build`, `spec-warning: ${run} small build note`])) {
        await logEntry(small, entry);
      }
      const smallPage = (await admin.get(`/admin/reviews/${small}`)).text;
      expect(box(smallPage, 'build-engine-warnings')).toContain(`${run} small build<`);
      expect(box(smallPage, 'build-spec-notes')).toContain('Text carried in the spec: 1 note, not written by the build engine');
      expect(box(smallPage, 'build-spec-notes')).toContain(`${run} small build note<`);

      // What the owner's status page is sent: the same lines (they are about
      // the owner's own site), with nothing redacted from them; the activity
      // card's filter is what keeps them out of sight (public/status.js).
      const asOwner = await (await loggedIn(owner.email)).get(`/api/builds/${buildId}/logs?after=0`);
      expect(asOwner.status).toBe(200);
      const sent = (asOwner.body.logs as Array<{ message: string }>).map((log) => log.message);
      expect(sent).toContain(entries[0]);
      // And a stranger is sent none of it.
      const stranger = await createCustomer('warn-stranger');
      expect((await (await loggedIn(stranger.email)).get(`/api/builds/${buildId}/logs?after=0`)).status).toBe(404);
    });

    workerIt(
      'entries written by the worker\'s own functions are read whole: _manifest_warning_checks and _append_log, then the review page',
      async () => {
        const owner = await createCustomer('warn-worker-owner');
        const buildId = await createBuild(owner.id, { status: 'preview', review: 'pending' });
        const tag = `w${run}`;
        const lines = manifestLines(tag);
        const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'manifest-'));
        const manifestPath = path.join(dir, 'compose-manifest.txt');
        fs.writeFileSync(manifestPath, `${lines.join('\n')}\n`, 'utf-8');
        try {
          const result = runWorkerPython(
            [
              WORKER_IMPORT,
              'conn = psycopg2.connect(sys.argv[2])',
              'manifest, was_cut = worker_pg._read_manifest(sys.argv[4])',
              'entries, engine_found = worker_pg._manifest_warning_checks(manifest)',
              'worker_pg._append_log(conn, sys.argv[3], "testing", sys.argv[5])',
              'for entry in entries:',
              '    worker_pg._append_log(conn, sys.argv[3], "testing", entry)',
              'conn.close()',
              'print("worker-wrote", len(entries), engine_found, was_cut)',
            ].join('\n'),
            [integrationUrl as string, buildId, manifestPath, LINT_RUN_START]
          );
          expect(result.ok, result.output).toBe(true);
          expect(result.output).toContain('worker-wrote 51 62 False');
        } finally {
          fs.rmSync(dir, { recursive: true, force: true });
        }

        // The worker wrote exactly the shapes the first test builds by hand.
        const stored = (await q<{ message: string }>('SELECT message FROM build_logs WHERE build_id = $1 ORDER BY id', [buildId])).map((row) => row.message);
        expect(stored).toEqual([LINT_RUN_START, ...workerEntries(lines)]);
        // And the start-of-run entry this file uses is the worker's, and the one the page looks for.
        const workerSource = fs.readFileSync(path.join(REPO_ROOT, 'queue/worker_pg.py'), 'utf-8');
        expect(workerSource).toContain(`_append_log(conn, build_id, "testing", "${LINT_RUN_START}")`);
        expect(LINT_RUN_START.startsWith(adminRoutes.BUILD_LINT_RUN_MARKER)).toBe(true);

        const admin = await loggedIn(ADMIN_EMAIL);
        const res = await admin.get(`/admin/reviews/${buildId}`);
        expect(res.status).toBe(200);
        expectWholeReport(res.text, tag);
      },
      60_000
    );
  });

  // -------------------------------------------------------------------------
  describe('the weekly sweeps: a run is claimed in the database', () => {
    const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

    it('the first claim is granted, the next is refused for a week (a restart changes nothing), and it is granted again when the week has passed', async () => {
      const name = `test-sweep-${run}`;
      expect(await cronScheduler.claimSweepRun(appDbPool, name, WEEK_MS)).toBe(true);
      // A second process, or this one after a restart, asking the same question.
      expect(await cronScheduler.claimSweepRun(appDbPool, name, WEEK_MS)).toBe(false);
      await q(`UPDATE platform_metadata SET updated_at = NOW() - INTERVAL '6 days 23 hours' WHERE key = $1`, [cronScheduler.sweepRunKey(name)]);
      expect(await cronScheduler.claimSweepRun(appDbPool, name, WEEK_MS)).toBe(false);
      await q(`UPDATE platform_metadata SET updated_at = NOW() - INTERVAL '7 days 1 minute' WHERE key = $1`, [cronScheduler.sweepRunKey(name)]);
      expect(await cronScheduler.claimSweepRun(appDbPool, name, WEEK_MS)).toBe(true);
      expect(await cronScheduler.claimSweepRun(appDbPool, name, WEEK_MS)).toBe(false);
    });

    it('several replicas asking at the same moment: exactly one gets the run, on a first run and on a due one', async () => {
      const name = `test-sweep-race-${run}`;
      const race = () => Promise.all(Array.from({ length: 6 }, () => cronScheduler.claimSweepRun(appDbPool, name, WEEK_MS)));
      expect((await race()).filter(Boolean)).toHaveLength(1);
      await q(`UPDATE platform_metadata SET updated_at = NOW() - INTERVAL '8 days' WHERE key = $1`, [cronScheduler.sweepRunKey(name)]);
      expect((await race()).filter(Boolean)).toHaveLength(1);
    });

    it('one sweep\'s claim does not hold another sweep back', async () => {
      expect(await cronScheduler.claimSweepRun(appDbPool, `test-sweep-a-${run}`, WEEK_MS)).toBe(true);
      expect(await cronScheduler.claimSweepRun(appDbPool, `test-sweep-b-${run}`, WEEK_MS)).toBe(true);
    });
  });
});
