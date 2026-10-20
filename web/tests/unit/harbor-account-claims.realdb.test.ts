// Which account a Harbor build and its payment may land in, against a real
// Postgres.
//
// An address someone typed is not an account they own. These tests drive the
// real routes (the visitor's and the operator's dispatch, the make-public
// page, the lamp's cancel form, the admin review verdicts and the Harbor
// inbox) and the real signed Stripe webhook against the schema in
// src/db/schema.sql, and check what lands where:
//   - an anonymous dispatch, the visitor's or the operator's, attaches a build
//     only to an account it creates; any account that already exists for the
//     address, verified or not, is refused before the model call, and one
//     that appears while the model runs is refused at the insert;
//   - a dispatch that refuses verified accounts reads the account again under
//     a share lock, so a verification committed while it ran is seen;
//   - the payment webhook writes no keeper row on a verified account for a
//     session no login of that account opened, and promotes a trial only into
//     the account its own dispatch created; a refused payment is recorded,
//     mailed to the operator and listed on /admin/harbor;
//   - recording a trial's first address (the email gate, the build request)
//     replaces the trial's cookie, and the old value gets a fresh trial;
//   - the Stripe return address is built by publicBaseUrl, not from raw
//     forwarded headers; the cancel form takes an account login; a review
//     verdict is recorded only on a build at preview.
//
// Stripe is not contacted: events are built here and signed with a secret
// generated for the run, and the two calls that would reach Stripe (opening
// a Checkout session, cancelling a subscription) are stood in for, as is the
// model call inside spec extraction. Outbound requests to any host but this
// machine throw.
//
// It runs only when CAS_SCHEMA_TEST_DATABASE_URL names a THROWAWAY database.
// Run it with `npx vitest run --no-file-parallelism`, because each realdb file
// applies schema.sql.

import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import http from 'http';
import https from 'https';
import bcrypt from 'bcrypt';
import request from 'supertest';
import { Client } from 'pg';
import type { Application } from 'express';
import type { Pool } from 'pg';

const integrationUrl = process.env.CAS_SCHEMA_TEST_DATABASE_URL;
const schema = fs.readFileSync(path.join(__dirname, '../../src/db/schema.sql'), 'utf-8');

const run = crypto.randomBytes(4).toString('hex');
const email = (label: string): string => `${label}-${run}@example.test`;
// Generated per run; nothing here is a real credential.
const PASSWORD = `pw-${crypto.randomBytes(9).toString('hex')}`;
const WEBHOOK_SECRET = ['whsec', crypto.randomBytes(16).toString('hex')].join('_');
const ADMIN_EMAIL = email('admin');
const TRIAL_COOKIE = 'cas_harbor_trial';

let app: Application;
let appPool: { end: () => Promise<void> };
let appDbPool: Pool;
let harborDispatch: typeof import('../../src/services/harbor_dispatch');
let specExtraction: typeof import('../../src/services/spec_extraction');
let stripeService: typeof import('../../src/services/stripe');
let buildStartLimits: typeof import('../../src/services/build-start-limits');
let db: Client;
let passwordHash: string;

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

/** Nothing here may leave the machine: outbound HTTP(S) to anything but loopback throws. */
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
      if (!isLoopback(host)) throw new Error(`harbor-account-claims.realdb.test.ts: outbound request to ${host} blocked`);
      return original(...args);
    }) as never);
  }
  const originalFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation(((input: unknown, init?: unknown) => {
    const url = typeof input === 'string' || input instanceof URL ? input : (input as { url: string }).url;
    const host = new URL(String(url)).hostname;
    if (!isLoopback(host)) return Promise.reject(new Error(`harbor-account-claims.realdb.test.ts: outbound fetch to ${host} blocked`));
    return originalFetch(input as never, init as never);
  }) as never);
}

async function createCustomer(label: string, opts: { verified?: boolean } = {}): Promise<{ id: string; email: string }> {
  const address = email(label);
  const row = await one<{ id: string }>(
    `INSERT INTO customers (email, password, name, email_verified) VALUES ($1, $2, $3, $4) RETURNING id`,
    [address, passwordHash, label, opts.verified ?? true]
  );
  return { id: row.id, email: address };
}

/** A Harbor build at preview, approved, on `customerId`, as a dispatch leaves it once reviewed. */
async function createHarborBuild(customerId: string, opts: { ownerCreatedByDispatch: boolean; status?: string; review?: string | null }): Promise<string> {
  const id = crypto.randomUUID();
  await q(
    `INSERT INTO builds (id, customer_id, tier, status, review_status, app_name, preview_url, spec, is_harbor, owner_created_by_dispatch)
     VALUES ($1, $2, 'build', $3, $4, $5, $6, '{}'::jsonb, TRUE, $7)`,
    [
      id,
      customerId,
      opts.status ?? 'preview',
      opts.review === undefined ? 'approved' : opts.review,
      `Site ${id.slice(0, 6)}`,
      `https://preview-${id.slice(0, 8)}.example.test`,
      opts.ownerCreatedByDispatch,
    ]
  );
  return id;
}

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
  // The token is bound to the session the page was rendered in, so it is read
  // before the request that carries it (and that session's cookie) is built.
  const csrf = await freshCsrf(agent);
  const res = await agent.post('/login').type('form').send({ _csrf: csrf, email: address, password: PASSWORD });
  expect(res.status, `login as ${address}`).toBe(302);
  return agent;
}
const setCookies = (res: { headers: Record<string, unknown> }): string[] =>
  ([] as string[]).concat((res.headers['set-cookie'] as string[] | undefined) || []);

/** A browser holding a fresh Harbor trial cookie, and that trial's id and cookie value. */
async function trialBrowser(agent: Browser = browser()): Promise<{ agent: Browser; trialId: string; token: string }> {
  const res = await agent.get('/lighthouse/table');
  expect(res.status).toBe(200);
  const cookie = setCookies(res).find((c) => c.startsWith(`${TRIAL_COOKIE}=`));
  const token = String(cookie).split(';')[0].split('=')[1];
  const row = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [token]);
  return { agent, trialId: row.trial_id, token };
}

/** A person's conversation, ready to build from, that gave `address` (or none). */
async function readyTrial(address: string | null, agent?: Browser) {
  const visitor = await trialBrowser(agent);
  // The test client sends no browser User-Agent, so the trial was minted as a bot's.
  await q('UPDATE trial_keepers SET email_captured = $1, is_synthetic = FALSE WHERE trial_id = $2', [address, visitor.trialId]);
  await q(
    `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
     SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
    [visitor.trialId]
  );
  return visitor;
}

/** The extraction's model call, stood in for. `onCall` runs inside it, while the dispatch waits for the model. */
function standInForExtraction(onCall?: () => Promise<unknown>): { calls: () => number; restore: () => void } {
  const spy = vi.spyOn(specExtraction, 'extractSpecFromConversation').mockImplementation(async () => {
    await onCall?.();
    return {
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
    } as never;
  });
  return { calls: () => spy.mock.calls.length, restore: () => spy.mockRestore() };
}

interface CheckoutAsk {
  buildId: string;
  trialId: string | null;
  keeperCustomerId: string | null;
  customerEmail: string;
  baseUrl: string;
}
/** The call that would open a Checkout session at Stripe, stood in for: what the route asked for. */
function standInForCheckout(): { asks: CheckoutAsk[]; restore: () => void } {
  const asks: CheckoutAsk[] = [];
  const spy = vi.spyOn(stripeService, 'createHarborSubscriptionCheckoutSession').mockImplementation(async (opts) => {
    asks.push(opts as unknown as CheckoutAsk);
    return 'https://checkout.stripe.com/c/pay/stand-in';
  });
  return { asks, restore: () => spy.mockRestore() };
}

let eventSeq = 0;
function stripeEvent(type: string, object: Record<string, unknown>, created?: number) {
  eventSeq += 1;
  return { id: `evt_${run}_${eventSeq}`, object: 'event', type, created: created ?? Math.floor(Date.now() / 1000), data: { object } };
}
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
/** The checkout.session.completed object Stripe sends for a session the route opened (or for these fields, built by hand). */
function paidSession(fields: { buildId: string; trialId: string | null; keeperCustomerId: string | null; email: string }, tag: string) {
  const metadata: Record<string, string> = {
    kind: 'harbor_make_public',
    build_id: fields.buildId,
    monthly_amount_cents: '2900',
    sliding_scale_attestation: 'true',
  };
  if (fields.trialId) metadata.trial_id = fields.trialId;
  if (fields.keeperCustomerId) metadata.keeper_customer_id = fields.keeperCustomerId;
  return stripeEvent('checkout.session.completed', {
    id: `cs_${run}_${tag}`,
    object: 'checkout.session',
    payment_status: 'paid',
    customer: `cus_${run}_${tag}`,
    subscription: `sub_${run}_${tag}`,
    customer_email: fields.email,
    metadata,
  });
}
/** The session Stripe completes for what the make-public route asked it to open. */
const sessionFor = (ask: CheckoutAsk) => ({
  buildId: ask.buildId,
  trialId: ask.trialId,
  keeperCustomerId: ask.keeperCustomerId,
  email: ask.customerEmail,
});
const paymentAlerts = (tag: string) =>
  q<{ to_email: string }>(
    `SELECT to_email FROM email_log WHERE template = 'harbor_payment_not_applied' AND metadata->>'stripe_session_id' = $1`,
    [`cs_${run}_${tag}`]
  );
const keeperOf = (customerId: string) =>
  q<{ stripe_subscription_id: string | null; handoff_build_id: string | null }>(
    'SELECT stripe_subscription_id, handoff_build_id FROM keepers WHERE customer_id = $1',
    [customerId]
  );
const trialState = (trialId: string) =>
  one<{ released: boolean; promoted_customer_id: string | null; email_captured: string | null; handoff_build_id: string | null }>(
    'SELECT released_at IS NOT NULL AS released, promoted_customer_id, email_captured, handoff_build_id FROM trial_keepers WHERE trial_id = $1',
    [trialId]
  );
const buildOf = (buildId: string) =>
  one<{ status: string; review_status: string | null; customer_id: string | null; owner_created_by_dispatch: boolean; reviewed_by: string | null }>(
    'SELECT status, review_status, customer_id, owner_created_by_dispatch, reviewed_by FROM builds WHERE id = $1',
    [buildId]
  );
const buildsOf = (customerId: string) => q<{ id: string }>('SELECT id FROM builds WHERE customer_id = $1', [customerId]);
const ledgerOf = (customerId: string) => q<{ build_id: string; kind: string }>('SELECT build_id, kind FROM build_start_events WHERE customer_id = $1', [customerId]);
const subscriptionRow = (tag: string) =>
  q<{ status: string; keeper_customer_id: string }>('SELECT status, keeper_customer_id FROM harbor_subscriptions WHERE stripe_subscription_id = $1', [
    `sub_${run}_${tag}`,
  ]);

async function operatorDispatch(admin: Browser, trialId: string) {
  const csrf = await freshCsrf(admin);
  return admin.post(`/admin/harbor/${trialId}/dispatch`).set('Content-Type', 'application/json').set('x-csrf-token', csrf).send({});
}

/** The section of /admin/harbor that lists payments needing a person, or '' when the page draws none. */
async function paymentsCard(admin: Browser): Promise<string> {
  const page = await admin.get('/admin/harbor');
  expect(page.status).toBe(200);
  const at = page.text.indexOf('data-payments-needing-a-person');
  return at === -1 ? '' : page.text.slice(at, page.text.indexOf('</table>', at));
}

describe.skipIf(!integrationUrl)('which account a Harbor build and its payment land in, against a real Postgres (integration)', () => {
  beforeAll(async () => {
    db = new Client({ connectionString: integrationUrl });
    await db.connect();
    await applySchema(db);

    process.env.DATABASE_URL = integrationUrl;
    process.env.CAS_HARBOR_ENABLED = 'true';
    process.env.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET;
    process.env.ADMIN_EMAIL = ADMIN_EMAIL;
    // Nothing proxies this app: X-Forwarded-* comes from the client.
    process.env.TRUST_PROXY_HOPS = '0';
    delete process.env.BASE_URL;
    delete process.env.RESEND_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
    vi.resetModules();
    blockOutboundRequests();
    vi.spyOn(console, 'info').mockImplementation(() => undefined);
    vi.spyOn(console, 'log').mockImplementation(() => undefined);
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    app = (await import('../../src/app')).default;
    appPool = (await import('../../src/db/index')).default as unknown as { end: () => Promise<void> };
    appDbPool = appPool as unknown as Pool;
    harborDispatch = await import('../../src/services/harbor_dispatch');
    specExtraction = await import('../../src/services/spec_extraction');
    stripeService = await import('../../src/services/stripe');
    buildStartLimits = await import('../../src/services/build-start-limits');
    passwordHash = await bcrypt.hash(PASSWORD, 4);
    await createCustomer('admin');
  }, 60_000);

  afterAll(async () => {
    vi.restoreAllMocks();
    if (appPool) await appPool.end();
    if (db) await db.end();
  });

  // -------------------------------------------------------------------------
  describe('the operator dispatch of a captured address follows the visitor\'s rule', () => {
    it('a VERIFIED account (the owner proved the mailbox): 409 account_exists with a plain sentence, no build, no model call', async () => {
      const owner = await createCustomer('op-verified', { verified: true });
      const stranger = await readyTrial(owner.email);
      const admin = await loggedIn(ADMIN_EMAIL);
      const extraction = standInForExtraction();
      try {
        const res = await operatorDispatch(admin, stranger.trialId);
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('account_exists');
        expect(res.body.message).toBe('That address has a verified account; the owner must log in and build from their account.');
        expect(extraction.calls()).toBe(0);
      } finally {
        extraction.restore();
      }
      expect(await buildsOf(owner.id)).toEqual([]);
      expect(await trialState(stranger.trialId)).toMatchObject({ handoff_build_id: null, released: false });
    });

    it('the configured ADMIN_EMAIL typed into a conversation is refused the same way', async () => {
      const admin = await loggedIn(ADMIN_EMAIL);
      const adminRow = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [ADMIN_EMAIL]);
      const before = (await buildsOf(adminRow.id)).length;
      const stranger = await readyTrial(ADMIN_EMAIL);
      const extraction = standInForExtraction();
      try {
        const res = await operatorDispatch(admin, stranger.trialId);
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('account_exists');
      } finally {
        extraction.restore();
      }
      expect(await buildsOf(adminRow.id)).toHaveLength(before);
    });

    it('an existing UNVERIFIED account: 409 address_in_use, no build, no start, no model call', async () => {
      const someone = await createCustomer('op-unverified', { verified: false });
      const stranger = await readyTrial(someone.email);
      const admin = await loggedIn(ADMIN_EMAIL);
      const extraction = standInForExtraction();
      try {
        const res = await operatorDispatch(admin, stranger.trialId);
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('address_in_use');
        expect(res.body.message).toMatch(/already has an account/);
        expect(extraction.calls()).toBe(0);
      } finally {
        extraction.restore();
      }
      expect(await buildsOf(someone.id)).toEqual([]);
      expect(await ledgerOf(someone.id)).toEqual([]);
    });

    it('a new address: the dispatch creates the account, and the build records that it did', async () => {
      const address = email('op-fresh');
      const visitor = await readyTrial(address);
      const admin = await loggedIn(ADMIN_EMAIL);
      const extraction = standInForExtraction();
      let buildId: string;
      try {
        const res = await operatorDispatch(admin, visitor.trialId);
        expect(res.status).toBe(200);
        buildId = res.body.buildId;
        expect(res.body).toMatchObject({ costMicros: 0, contextSize: { memoryRows: 0, turns: 0 } });
      } finally {
        extraction.restore();
      }
      const account = await one<{ id: string; email_verified: boolean }>('SELECT id, email_verified FROM customers WHERE email = $1', [address]);
      expect(account.email_verified).toBe(false);
      expect(await buildOf(buildId)).toMatchObject({ status: 'queued', customer_id: account.id, owner_created_by_dispatch: true });
      expect((await trialState(visitor.trialId)).handoff_build_id).toBe(buildId);
      // The operator is exempt from the build-start limits and writes no ledger row.
      expect(await ledgerOf(account.id)).toEqual([]);
    });

    it('a trial that captured no address: the build goes to the operator\'s own account, the one existing account this route attaches to', async () => {
      const visitor = await readyTrial(null);
      const admin = await loggedIn(ADMIN_EMAIL);
      const adminRow = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [ADMIN_EMAIL]);
      const extraction = standInForExtraction();
      try {
        const res = await operatorDispatch(admin, visitor.trialId);
        expect(res.status).toBe(200);
        expect(res.body.customerId).toBe(adminRow.id);
        expect(await buildOf(res.body.buildId)).toMatchObject({ customer_id: adminRow.id, owner_created_by_dispatch: false });
      } finally {
        extraction.restore();
      }
    });
  });

  // -------------------------------------------------------------------------
  describe('the visitor\'s anonymous dispatch attaches only to an account it creates', () => {
    async function expectRefusedBeforeTheModel(account: { id: string; email: string }) {
      const visitor = await readyTrial(account.email);
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('email_not_usable');
        expect(extraction.calls()).toBe(0);
      } finally {
        extraction.restore();
      }
      expect(await buildsOf(account.id)).toEqual([]);
      expect(await ledgerOf(account.id)).toEqual([]);
      // The address is forgotten so the visitor can give another; the conversation is not used up.
      expect(await trialState(visitor.trialId)).toMatchObject({ email_captured: null, handoff_build_id: null });
    }

    it('an unverified account with no build and no keeper row (someone\'s signup waiting for its link): refused', async () => {
      await expectRefusedBeforeTheModel(await createCustomer('anon-bare', { verified: false }));
    });

    it('an account made by POST /signup from another browser a moment earlier: refused', async () => {
      const address = email('anon-signup');
      const owner = browser();
      const signupCsrf = await freshCsrf(owner);
      const signup = await owner.post('/signup').type('form').send({ _csrf: signupCsrf, email: address, password: PASSWORD, name: 'Owner' });
      expect(signup.status).toBe(302);
      const account = await one<{ id: string; email_verified: boolean }>('SELECT id, email_verified FROM customers WHERE email = $1', [address]);
      expect(account.email_verified).toBe(false);
      await expectRefusedBeforeTheModel({ id: account.id, email: address });
    });

    it('an account that appears while the model runs is refused at the insert: no build, no start, and the account is left as it was', async () => {
      const address = email('anon-raced');
      const visitor = await readyTrial(address);
      let appeared: { id: string; email: string } | null = null;
      const extraction = standInForExtraction(async () => {
        appeared = await one<{ id: string; email: string }>(
          `INSERT INTO customers (email, password, name, email_verified) VALUES ($1, $2, 'raced', FALSE) RETURNING id, email`,
          [address, passwordHash]
        );
      });
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('email_not_usable');
        expect(extraction.calls()).toBe(1);
      } finally {
        extraction.restore();
      }
      const raced = appeared as unknown as { id: string };
      expect(await buildsOf(raced.id)).toEqual([]);
      expect(await ledgerOf(raced.id)).toEqual([]);
      expect(await q('SELECT name FROM customers WHERE email = $1', [address])).toEqual([{ name: 'raced' }]);
    });

    it('a new address: the build is on the account the dispatch made, recorded as such, with its start in the ledger', async () => {
      const address = email('anon-fresh');
      const visitor = await readyTrial(address);
      const extraction = standInForExtraction();
      let buildId: string;
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(200);
        buildId = res.body.buildId;
      } finally {
        extraction.restore();
      }
      const account = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [address]);
      expect(await buildOf(buildId)).toMatchObject({ customer_id: account.id, owner_created_by_dispatch: true });
      expect(await ledgerOf(account.id)).toEqual([{ build_id: buildId, kind: 'dispatch' }]);
    });

    it('a dispatch that may not attach to a verified account reads it again under a share lock: a verification that lands after its last unlocked read is seen, and no build is written', async () => {
      // The service's default options (no session, not anonymous). The
      // account exists, unverified, at every read the dispatch makes before
      // its insert transaction. The test holds the customer's build-start
      // lock, which that transaction takes first, so the dispatch stops right
      // there; then the owner's verification (an UPDATE of the row) starts,
      // the lock is let go, and the verification commits only once the
      // dispatch has gone on as far as it can.
      const account = await createCustomer('share-lock', { verified: false });
      const visitor = await readyTrial(account.email);
      const holder = new Client({ connectionString: integrationUrl });
      const verifier = new Client({ connectionString: integrationUrl });
      await holder.connect();
      await verifier.connect();
      const waitingOn = async (kind: 'advisory' | 'row'): Promise<boolean> => {
        const rows = await q<{ n: string }>(
          `SELECT COUNT(*) AS n FROM pg_stat_activity
            WHERE datname = current_database() AND wait_event_type = 'Lock'
              AND (wait_event = 'advisory') = $1`,
          [kind === 'advisory']
        );
        return Number(rows[0].n) > 0;
      };
      const extraction = standInForExtraction();
      try {
        await holder.query('BEGIN');
        await holder.query(buildStartLimits.BUILD_START_LOCK_SQL, [account.id]);
        let settled = false;
        const dispatching = harborDispatch
          .dispatchHarborTrialBuild(appDbPool, { trialId: visitor.trialId, resolvedEmail: account.email, source: 'self_serve' })
          .then(
            (result) => ({ result, error: null as unknown }),
            (error: unknown) => ({ result: null, error })
          )
          .finally(() => {
            settled = true;
          });
        // The dispatch has read the account (unverified) and waits for the build-start lock.
        for (let i = 0; i < 200 && !(await waitingOn('advisory')); i += 1) {
          expect(settled, 'the dispatch should wait for the build-start lock').toBe(false);
          await new Promise((resolve) => setTimeout(resolve, 25));
        }
        expect(await waitingOn('advisory')).toBe(true);
        // The owner verifies; the UPDATE holds the row until it commits.
        await verifier.query('BEGIN');
        await verifier.query('UPDATE customers SET email_verified = TRUE WHERE id = $1', [account.id]);
        await holder.query('COMMIT');
        // The dispatch goes on until it finishes or waits on the row.
        for (let i = 0; i < 200 && !settled && !(await waitingOn('row')); i += 1) {
          await new Promise((resolve) => setTimeout(resolve, 25));
        }
        await verifier.query('COMMIT');
        const outcome = await dispatching;
        expect(outcome.result).toBeNull();
        expect(outcome.error).toMatchObject({ code: 'account_exists' });
      } finally {
        await holder.query('ROLLBACK').catch(() => undefined);
        await verifier.query('ROLLBACK').catch(() => undefined);
        await holder.end();
        await verifier.end();
        extraction.restore();
      }
      expect(await buildsOf(account.id)).toEqual([]);
      expect(await ledgerOf(account.id)).toEqual([]);
      expect((await trialState(visitor.trialId)).handoff_build_id).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  describe('the payment webhook: whose account a paid session may write a keeper row on', () => {
    it('an account its dispatch created, then verified by the mailbox owner: the trial cookie\'s session opened before the verification is refused, recorded, mailed and listed; the cookie can no longer open one', async () => {
      // A visitor builds a preview for an address; the dispatch creates the account.
      const address = email('verified-later');
      const visitor = await readyTrial(address);
      const extraction = standInForExtraction();
      let buildId: string;
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(200);
        buildId = res.body.buildId;
      } finally {
        extraction.restore();
      }
      const account = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [address]);
      await q(`UPDATE builds SET status = 'preview', review_status = 'approved', preview_url = 'https://verified-later.example.test' WHERE id = $1`, [buildId]);

      // The visitor's cookie opens Checkout while nobody has verified the account.
      const checkout = standInForCheckout();
      let ask: CheckoutAsk;
      try {
        const page = await visitor.agent.get(`/lighthouse/lamp/${buildId}/make-public`);
        expect(page.status).toBe(200);
        const opened = await visitor.agent
          .post(`/lighthouse/lamp/${buildId}/make-public`)
          .type('form')
          .send({ _csrf: csrfFrom(page.text), email: address, email_confirmed: 'on', monthly_amount_cents: '2900', sliding_scale_attestation: 'on' });
        expect(opened.status).toBe(303);
        ask = checkout.asks[0];
        expect(ask).toMatchObject({ buildId, trialId: visitor.trialId, keeperCustomerId: null, customerEmail: address });
      } finally {
        checkout.restore();
      }

      // The mailbox's owner proves it before the payment completes (the row
      // as a completed verification or password reset leaves it).
      await q('UPDATE customers SET email_verified = TRUE WHERE id = $1', [account.id]);

      // The session completes: nothing is written on the verified account.
      const paid = await deliver(paidSession(sessionFor(ask), 'vlater'));
      expect(paid.status).toBe(200);
      expect(paid.body).toEqual({ received: true });
      expect(await keeperOf(account.id)).toEqual([]);
      expect(await trialState(visitor.trialId)).toMatchObject({ released: false, promoted_customer_id: null });
      expect((await buildOf(buildId)).status).toBe('preview');
      expect(await subscriptionRow('vlater')).toEqual([{ status: 'active', keeper_customer_id: account.id }]);
      await vi.waitFor(async () => expect(await paymentAlerts('vlater')).toEqual([{ to_email: ADMIN_EMAIL }]));

      // The operator sees it on the Harbor inbox.
      const admin = await loggedIn(ADMIN_EMAIL);
      const card = await paymentsCard(admin);
      expect(card).toContain('Payments needing a person');
      expect(card).toContain(`sub_${run}_vlater`);
      expect(card).toContain(address);

      // The cookie cannot open another session for the verified account; the page does not show its address.
      const page = await visitor.agent.get(`/lighthouse/lamp/${buildId}/make-public`);
      expect(page.status).toBe(409);
      expect(page.text).toContain('Please log in to that account');
      const againCsrf = await freshCsrf(visitor.agent);
      const again = await visitor.agent
        .post(`/lighthouse/lamp/${buildId}/make-public`)
        .type('form')
        .send({ _csrf: againCsrf, email: address, email_confirmed: 'on', monthly_amount_cents: '2900', sliding_scale_attestation: 'on' });
      expect(again.status).toBe(409);
      expect(again.body.error).toBe('account_login_required');

      // Cancelled in Stripe, it leaves the list.
      const ended = await deliver(
        stripeEvent('customer.subscription.deleted', {
          id: `sub_${run}_vlater`,
          object: 'subscription',
          status: 'canceled',
          cancel_at_period_end: false,
          metadata: { kind: 'harbor_make_public' },
        })
      );
      expect(ended.status).toBe(200);
      expect(await paymentsCard(admin)).not.toContain(`sub_${run}_vlater`);
    });

    it('a verified account\'s own login opens the session (keeper_customer_id): the keeper row is written and the website goes live', async () => {
      const owner = await createCustomer('own-login', { verified: true });
      const buildId = await createHarborBuild(owner.id, { ownerCreatedByDispatch: false });
      const paid = await deliver(paidSession({ buildId, trialId: null, keeperCustomerId: owner.id, email: owner.email }, 'ownlogin'));
      expect(paid.status).toBe(200);
      expect(await keeperOf(owner.id)).toEqual([{ stripe_subscription_id: `sub_${run}_ownlogin`, handoff_build_id: null }]);
      expect((await buildOf(buildId)).status).toBe('live');
      expect(await paymentAlerts('ownlogin')).toEqual([]);
    });

    it('the same verified account and a session no login of it opened, with no trial: no keeper row, refused and recorded', async () => {
      const owner = await createCustomer('no-login', { verified: true });
      const buildId = await createHarborBuild(owner.id, { ownerCreatedByDispatch: false });
      const paid = await deliver(paidSession({ buildId, trialId: null, keeperCustomerId: null, email: owner.email }, 'nologin'));
      expect(paid.status).toBe(200);
      expect(await keeperOf(owner.id)).toEqual([]);
      expect((await buildOf(buildId)).status).toBe('preview');
      expect(await subscriptionRow('nologin')).toEqual([{ status: 'active', keeper_customer_id: owner.id }]);
      await vi.waitFor(async () => expect(await paymentAlerts('nologin')).toHaveLength(1));
    });

    it('an unverified account that existed before the trial\'s dispatch (a build the earlier code attached to it): the trial cookie\'s session promotes nothing', async () => {
      const victim = await createCustomer('pre-existing', { verified: false });
      const stranger = await readyTrial(victim.email);
      const buildId = await createHarborBuild(victim.id, { ownerCreatedByDispatch: false });
      await q('UPDATE trial_keepers SET handoff_build_id = $1, handoff_at = NOW() WHERE trial_id = $2', [buildId, stranger.trialId]);

      const paid = await deliver(paidSession({ buildId, trialId: stranger.trialId, keeperCustomerId: null, email: victim.email }, 'preexist'));
      expect(paid.status).toBe(200);
      expect(await keeperOf(victim.id)).toEqual([]);
      expect(await trialState(stranger.trialId)).toMatchObject({ released: false, promoted_customer_id: null });
      expect(await q('SELECT 1 FROM companion_turns WHERE keeper_id = $1', [victim.id])).toEqual([]);
      expect((await buildOf(buildId)).status).toBe('preview');
      expect(await subscriptionRow('preexist')).toEqual([{ status: 'active', keeper_customer_id: victim.id }]);
      await vi.waitFor(async () => expect(await paymentAlerts('preexist')).toHaveLength(1));
    });

    it('the account the trial\'s own dispatch created, paid from the trial cookie through the route: promoted, live, and the cookie is the payer\'s', async () => {
      const address = email('own-dispatch');
      const visitor = await readyTrial(address);
      const extraction = standInForExtraction();
      let buildId: string;
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(200);
        buildId = res.body.buildId;
      } finally {
        extraction.restore();
      }
      const account = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [address]);
      await q(`UPDATE builds SET status = 'preview', review_status = 'approved', preview_url = 'https://own-dispatch.example.test' WHERE id = $1`, [buildId]);
      const checkout = standInForCheckout();
      let ask: CheckoutAsk;
      try {
        const page = await visitor.agent.get(`/lighthouse/lamp/${buildId}/make-public`);
        const opened = await visitor.agent
          .post(`/lighthouse/lamp/${buildId}/make-public`)
          .type('form')
          .send({ _csrf: csrfFrom(page.text), email: address, email_confirmed: 'on', monthly_amount_cents: '2900', sliding_scale_attestation: 'on' });
        expect(opened.status).toBe(303);
        ask = checkout.asks[0];
      } finally {
        checkout.restore();
      }
      expect((await deliver(paidSession(sessionFor(ask), 'owndisp'))).status).toBe(200);
      expect(await keeperOf(account.id)).toEqual([{ stripe_subscription_id: `sub_${run}_owndisp`, handoff_build_id: buildId }]);
      expect(await trialState(visitor.trialId)).toMatchObject({ released: true, promoted_customer_id: account.id });
      expect((await buildOf(buildId)).status).toBe('live');
      expect((await visitor.agent.get(`/lighthouse/lamp/${buildId}`)).status).toBe(200);
      expect(await paymentAlerts('owndisp')).toEqual([]);
      // An applied payment is not one that needs a person.
      expect(await paymentsCard(await loggedIn(ADMIN_EMAIL))).not.toContain(`sub_${run}_owndisp`);
    });
  });

  // -------------------------------------------------------------------------
  describe('recording a trial\'s first address replaces its cookie', () => {
    /** The trial cookie a response sets, or null. */
    const cookieSetBy = (res: { headers: Record<string, unknown> }): string | null => {
      const cookie = setCookies(res).find((c) => c.startsWith(`${TRIAL_COOKIE}=`));
      return cookie ? cookie.split(';')[0].split('=')[1] : null;
    };
    /** What a browser holding only `token` is: the trial it is, after the request. */
    async function trialHeldBy(token: string): Promise<{ trialId: string | null; replaced: boolean }> {
      const res = await request(app).get('/lighthouse/table').set('Cookie', `${TRIAL_COOKIE}=${token}`);
      expect(res.status).toBe(200);
      const given = cookieSetBy(res);
      const now = given && given !== token ? given : token;
      const row = await q<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [now]);
      return { trialId: row[0]?.trial_id ?? null, replaced: Boolean(given && given !== token) };
    }
    async function expectRotated(before: { trialId: string; token: string }, res: { headers: Record<string, unknown> }) {
      const after = cookieSetBy(res);
      expect(after, 'the response sets a new trial cookie').not.toBeNull();
      expect(after).not.toBe(before.token);
      expect((await one<{ cookie_token: string }>('SELECT cookie_token FROM trial_keepers WHERE trial_id = $1', [before.trialId])).cookie_token).toBe(after);
      // The new value is the same trial; the old one is a stale cookie and gets a fresh trial.
      expect((await trialHeldBy(after as string)).trialId).toBe(before.trialId);
      const old = await trialHeldBy(before.token);
      expect(old.replaced).toBe(true);
      expect(old.trialId).not.toBe(before.trialId);
    }

    it('at the conversation\'s email gate', async () => {
      const visitor = await trialBrowser();
      await q(
        `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
         SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 3) AS n`,
        [visitor.trialId]
      );
      const saved = process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
      process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT = '3';
      try {
        // A message the input rail answers itself, so no model is called.
        const res = await visitor.agent
          .post('/lighthouse/table/turn')
          .send({ keeperText: 'please write me some testimonials', email: email('gate-capture') });
        expect(res.status).toBe(200);
        expect((await trialState(visitor.trialId)).email_captured).toBe(email('gate-capture'));
        await expectRotated(visitor, res);
      } finally {
        if (saved === undefined) delete process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
        else process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT = saved;
      }
    });

    it('when the build request gives the address', async () => {
      const visitor = await trialBrowser();
      await q('UPDATE trial_keepers SET is_synthetic = FALSE WHERE trial_id = $1', [visitor.trialId]);
      // Not ready to build (no turns): the address is recorded all the same, before the readiness answer.
      const res = await visitor.agent.post('/lighthouse/build').send({ email: email('build-capture') });
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('not_ready');
      expect((await trialState(visitor.trialId)).email_captured).toBe(email('build-capture'));
      await expectRotated(visitor, res);
    });

    it('when a logged-in customer\'s own conversation takes the account\'s address', async () => {
      const member = await createCustomer('capture-member');
      const agent = await loggedIn(member.email);
      const visitor = await trialBrowser(agent);
      await q('UPDATE trial_keepers SET is_synthetic = FALSE WHERE trial_id = $1', [visitor.trialId]);
      const res = await agent.post('/lighthouse/build').send({});
      expect(res.status).toBe(409);
      expect((await trialState(visitor.trialId)).email_captured).toBe(member.email);
      await expectRotated(visitor, res);
    });

    it('an address already on the trial is not recorded again, and the cookie stays', async () => {
      const visitor = await readyTrial(email('already-captured'));
      const res = await visitor.agent.post('/lighthouse/build').send({ email: email('another-address') });
      expect(cookieSetBy(res) ?? visitor.token).toBe(visitor.token);
      expect((await trialState(visitor.trialId)).email_captured).toBe(email('already-captured'));
    });
  });

  // -------------------------------------------------------------------------
  describe('the Stripe return address comes from publicBaseUrl, not from raw forwarded headers', () => {
    it('X-Forwarded-Proto and a Host that is not a plain host do not reach the Checkout return address', async () => {
      const address = email('base-url');
      const visitor = await readyTrial(address);
      const extraction = standInForExtraction();
      let buildId: string;
      try {
        buildId = (await visitor.agent.post('/lighthouse/build').send({})).body.buildId;
      } finally {
        extraction.restore();
      }
      await q(`UPDATE builds SET status = 'preview', review_status = 'approved', preview_url = 'https://base-url.example.test' WHERE id = $1`, [buildId]);
      const page = await visitor.agent.get(`/lighthouse/lamp/${buildId}/make-public`);
      const checkout = standInForCheckout();
      try {
        const opened = await visitor.agent
          .post(`/lighthouse/lamp/${buildId}/make-public`)
          .set('X-Forwarded-Proto', 'javascript')
          .set('Host', 'evil.example/phish')
          .type('form')
          .send({ _csrf: csrfFrom(page.text), email: address, email_confirmed: 'on', monthly_amount_cents: '2900', sliding_scale_attestation: 'on' });
        expect(opened.status).toBe(303);
        expect(checkout.asks[0].baseUrl).toBe('http://localhost');
      } finally {
        checkout.restore();
      }
    });
  });

  // -------------------------------------------------------------------------
  describe('cancelling the subscription takes an account login', () => {
    async function paidThroughTheCookie(tag: string) {
      const address = email(`cancel-${tag}`);
      const visitor = await readyTrial(address);
      const extraction = standInForExtraction();
      let buildId: string;
      try {
        buildId = (await visitor.agent.post('/lighthouse/build').send({})).body.buildId;
      } finally {
        extraction.restore();
      }
      await q(`UPDATE builds SET status = 'preview', review_status = 'approved', preview_url = 'https://cancel.example.test' WHERE id = $1`, [buildId]);
      const account = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [address]);
      expect((await deliver(paidSession({ buildId, trialId: visitor.trialId, keeperCustomerId: null, email: address }, tag))).status).toBe(200);
      expect((await keeperOf(account.id))[0]?.stripe_subscription_id).toBe(`sub_${run}_${tag}`);
      return { visitor, buildId, account, address };
    }

    it('the paying cookie alone gets a page that says to log in, and Stripe is not asked', async () => {
      const t = await paidThroughTheCookie('cnlcookie');
      const cancel = vi.spyOn(stripeService, 'cancelHarborSubscriptionAtPeriodEnd').mockResolvedValue({} as never);
      try {
        const lamp = await t.visitor.agent.get(`/lighthouse/lamp/${t.buildId}`);
        expect(lamp.status).toBe(200);
        const res = await t.visitor.agent
          .post(`/lighthouse/lamp/${t.buildId}/cancel-subscription`)
          .type('form')
          .send({ _csrf: csrfFrom(lamp.text) });
        expect(res.status).toBe(403);
        expect(res.text).toContain('Log in to cancel the subscription.');
        expect(res.text).toContain('Forgot your password?');
        expect(cancel).not.toHaveBeenCalled();
      } finally {
        cancel.mockRestore();
      }
      expect(await q('SELECT cancel_at_period_end FROM harbor_subscriptions WHERE stripe_subscription_id = $1', [`sub_${run}_cnlcookie`])).toEqual([
        { cancel_at_period_end: false },
      ]);
    });

    it('the account, logged in, cancels at period end', async () => {
      const t = await paidThroughTheCookie('cnllogin');
      // The payer chose a password by reset, which also verifies the account.
      await q('UPDATE customers SET email_verified = TRUE, password = $2 WHERE id = $1', [t.account.id, passwordHash]);
      const owner = await loggedIn(t.address);
      const cancel = vi.spyOn(stripeService, 'cancelHarborSubscriptionAtPeriodEnd').mockResolvedValue({} as never);
      try {
        const lamp = await owner.get(`/lighthouse/lamp/${t.buildId}`);
        expect(lamp.status).toBe(200);
        const res = await owner.post(`/lighthouse/lamp/${t.buildId}/cancel-subscription`).type('form').send({ _csrf: csrfFrom(lamp.text) });
        expect(res.status).toBe(303);
        expect(cancel).toHaveBeenCalledWith(`sub_${run}_cnllogin`);
      } finally {
        cancel.mockRestore();
      }
      expect(await q('SELECT cancel_at_period_end FROM harbor_subscriptions WHERE stripe_subscription_id = $1', [`sub_${run}_cnllogin`])).toEqual([
        { cancel_at_period_end: true },
      ]);
    });
  });

  // -------------------------------------------------------------------------
  describe('an admin review verdict is recorded only on a build at preview', () => {
    const verdict = async (admin: Browser, buildId: string, action: 'approve' | 'request-changes') => {
      const csrf = await freshCsrf(admin);
      return admin.post(`/admin/reviews/${buildId}/${action}`).type('form').send({ _csrf: csrf, review_notes: 'Please change the header.' });
    };

    it.each([
      ['approve', 'queued', null],
      ['approve', 'building', null],
      ['approve', 'failed', 'pending'],
      ['request-changes', 'live', 'approved'],
      ['request-changes', 'queued', null],
    ] as const)('%s on a build that is %s: 409, nothing recorded, no email', async (action, status, review) => {
      const owner = await createCustomer(`verdict-${action}-${status}`);
      const buildId = await createHarborBuild(owner.id, { ownerCreatedByDispatch: false, status, review });
      const admin = await loggedIn(ADMIN_EMAIL);
      const res = await verdict(admin, buildId, action);
      expect(res.status).toBe(409);
      expect(res.text).toContain('This build is not at preview.');
      expect(await buildOf(buildId)).toMatchObject({ status, review_status: review, reviewed_by: null });
      expect(await q(`SELECT 1 FROM email_log WHERE to_email = $1`, [owner.email])).toEqual([]);
    });

    it('at preview both verdicts are recorded, and an unknown build is still 404', async () => {
      const owner = await createCustomer('verdict-preview');
      const buildId = await createHarborBuild(owner.id, { ownerCreatedByDispatch: false, review: 'pending' });
      const admin = await loggedIn(ADMIN_EMAIL);
      expect((await verdict(admin, buildId, 'request-changes')).status).toBe(302);
      expect(await buildOf(buildId)).toMatchObject({ status: 'preview', review_status: 'changes_requested', reviewed_by: ADMIN_EMAIL });
      expect((await verdict(admin, buildId, 'approve')).status).toBe(302);
      expect(await buildOf(buildId)).toMatchObject({ status: 'preview', review_status: 'approved' });
      expect((await verdict(admin, crypto.randomUUID(), 'approve')).status).toBe(404);
    });
  });
});
