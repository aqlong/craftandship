// Whose conversation a browser speaks for, and whose build an anonymous
// dispatch may attach to, against a real Postgres.
//
// A trial cookie that a payment released (the "paid cookie", see
// middleware/trial-cookie.ts "After payment") identifies the browser that paid
// for one build. It must not outlive the account a browser logs in to, and it
// must not outlive a password reset or an email verification of the account it
// was promoted into. An anonymous dispatch must not attach a build to an
// account that already exists (one with a keeper row or a build, or any other),
// because that account, and the builds and start limits it has, belong to
// whoever owns the address.
//
// The routes, the webhook and the dispatch service run unmodified, against the
// schema in src/db/schema.sql. Stripe is not contacted: webhook events are
// built here and signed with a secret generated for the run. Outbound requests
// to any host but this machine throw. The model call inside spec extraction is
// the one stand-in, as in money-path.realdb.test.ts.
//
// It runs only when CAS_SCHEMA_TEST_DATABASE_URL names a THROWAWAY database
// (the same switch as money-path.realdb.test.ts). Run it with
// `npx vitest run --no-file-parallelism`, because each file applies schema.sql.

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
      if (!isLoopback(host)) throw new Error(`paid-trial-identity.realdb.test.ts: outbound request to ${host} blocked`);
      return original(...args);
    }) as never);
  }
  const originalFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation(((input: unknown, init?: unknown) => {
    const url = typeof input === 'string' || input instanceof URL ? input : (input as { url: string }).url;
    const host = new URL(String(url)).hostname;
    if (!isLoopback(host)) return Promise.reject(new Error(`paid-trial-identity.realdb.test.ts: outbound fetch to ${host} blocked`));
    return originalFetch(input as never, init as never);
  }) as never);
}

const sha256 = (value: string): string => crypto.createHash('sha256').update(value).digest('hex');

async function createCustomer(label: string, opts: { verified?: boolean } = {}): Promise<{ id: string; email: string }> {
  const address = email(label);
  const row = await one<{ id: string }>(
    `INSERT INTO customers (email, password, name, email_verified) VALUES ($1, $2, $3, $4) RETURNING id`,
    [address, passwordHash, label, opts.verified ?? true]
  );
  return { id: row.id, email: address };
}

/**
 * A build row as the dispatch leaves it: queued or at preview, approved, owned
 * by `customerId`. `ownerCreatedByDispatch` is what an anonymous dispatch
 * records when it created that account (builds.owner_created_by_dispatch).
 */
async function createBuild(
  customerId: string,
  opts: { harbor?: boolean; status?: string; ownerCreatedByDispatch?: boolean } = {}
): Promise<string> {
  const id = crypto.randomUUID();
  const columns = `id, customer_id, tier, status, review_status, app_name, preview_url, spec, owner_created_by_dispatch`;
  const values = `$1, $2, 'build', $3, 'approved', $4, $5, '{}'::jsonb, $6`;
  await q(
    opts.harbor
      ? `INSERT INTO builds (${columns}, is_harbor) VALUES (${values}, TRUE)`
      : `INSERT INTO builds (${columns}) VALUES (${values})`,
    [id, customerId, opts.status ?? 'preview', `Site ${id.slice(0, 6)}`, `https://preview-${id.slice(0, 8)}.example.test`, opts.ownerCreatedByDispatch === true]
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
/** Log the browser in as `address` (verified accounts only: an unverified one is sent to verify first). */
async function logIn(agent: Browser, address: string): Promise<void> {
  const csrf = await freshCsrf(agent);
  const res = await agent.post('/login').type('form').send({ _csrf: csrf, email: address, password: PASSWORD });
  expect(res.status, `login as ${address}`).toBe(302);
}
const setCookies = (res: { headers: Record<string, unknown> }): string[] =>
  ([] as string[]).concat((res.headers['set-cookie'] as string[] | undefined) || []);

/** A browser holding a fresh Harbor trial cookie, and that trial's id and cookie value. */
async function trialBrowser(): Promise<{ agent: Browser; trialId: string; token: string }> {
  const agent = browser();
  const res = await agent.get('/lighthouse/table');
  expect(res.status).toBe(200);
  const cookie = setCookies(res).find((c) => c.startsWith(`${TRIAL_COOKIE}=`));
  const token = String(cookie).split(';')[0].split('=')[1];
  const row = await one<{ trial_id: string }>('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [token]);
  return { agent, trialId: row.trial_id, token };
}

// --- Stripe events, signed locally -------------------------------------------

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
function checkoutSession(opts: { buildId: string; trialId: string; email: string; tag: string }) {
  return {
    id: `cs_${run}_${opts.tag}`,
    object: 'checkout.session',
    payment_status: 'paid',
    customer: `cus_${run}_${opts.tag}`,
    subscription: `sub_${run}_${opts.tag}`,
    customer_email: opts.email,
    metadata: {
      kind: 'harbor_make_public',
      build_id: opts.buildId,
      trial_id: opts.trialId,
      monthly_amount_cents: '2900',
      sliding_scale_attestation: 'true',
    },
  };
}

interface PaidTrial {
  /** The browser that paid. It holds the trial cookie, which the payment released. */
  agent: Browser;
  trialId: string;
  /** The cookie value the payment released: the paid cookie. */
  token: string;
  /** The account the payment landed in (unverified, with a random password, as a payer's account is). */
  payer: { id: string; email: string };
  buildId: string;
  /** A line of the payer's conversation, which the paying browser's page must show and no other browser's may. */
  phrase: string;
}

/** A visitor's trial with a Harbor build stamped on it and a conversation, then paid for through the signed webhook. */
async function paidTrial(tag: string): Promise<PaidTrial> {
  const visitor = await trialBrowser();
  // The account the trial's dispatch created for the address it gave.
  const payer = await createCustomer(`payer-${tag}`, { verified: false });
  const buildId = await createBuild(payer.id, { harbor: true, ownerCreatedByDispatch: true });
  const phrase = `conversation-${tag}-${run}`;
  await q('UPDATE trial_keepers SET handoff_build_id = $1, handoff_at = NOW(), email_captured = $2 WHERE trial_id = $3', [
    buildId,
    payer.email,
    visitor.trialId,
  ]);
  await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, $2, 'Tell me more.')`, [
    visitor.trialId,
    `I teach reiki. ${phrase}`,
  ]);
  const paid = await deliver(
    stripeEvent('checkout.session.completed', checkoutSession({ buildId, trialId: visitor.trialId, email: payer.email, tag }))
  );
  expect(paid.status).toBe(200);
  const released = await one<{ released: boolean }>('SELECT released_at IS NOT NULL AS released FROM trial_keepers WHERE trial_id = $1', [
    visitor.trialId,
  ]);
  expect(released.released).toBe(true);
  return { agent: visitor.agent, trialId: visitor.trialId, token: visitor.token, payer, buildId, phrase };
}

/** The companion page of a browser: its status and body. */
const tablePage = (agent: Browser) => agent.get('/lighthouse/table');

/** The "ready to build" conversation, for the dispatch tests: an address captured and enough turns. */
async function readyTrial(address: string) {
  const visitor = await trialBrowser();
  // The test client sends no browser User-Agent, so the trial is minted as a
  // bot's. These tests are about people's conversations.
  await q('UPDATE trial_keepers SET email_captured = $1, is_synthetic = FALSE WHERE trial_id = $2', [address, visitor.trialId]);
  await q(
    `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
     SELECT $1, 'I teach reiki, part ' || n, 'Tell me more.' FROM generate_series(1, 5) AS n`,
    [visitor.trialId]
  );
  return visitor;
}

/** The extraction's model call, stood in for: the spec it returns is a real one with copy in it. */
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

const ledgerOf = (customerId: string) =>
  q<{ build_id: string; kind: string }>('SELECT build_id, kind FROM build_start_events WHERE customer_id = $1 ORDER BY id', [customerId]);
const buildsOf = (customerId: string) => q<{ id: string }>('SELECT id FROM builds WHERE customer_id = $1', [customerId]);

describe.skipIf(!integrationUrl)('the paid trial cookie and the account session, against a real Postgres (integration)', () => {
  beforeAll(async () => {
    db = new Client({ connectionString: integrationUrl });
    await db.connect();
    await applySchema(db);

    process.env.DATABASE_URL = integrationUrl;
    process.env.CAS_HARBOR_ENABLED = 'true';
    process.env.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET;
    process.env.ADMIN_EMAIL = ADMIN_EMAIL;
    delete process.env.RESEND_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
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
    passwordHash = await bcrypt.hash(PASSWORD, 4);
    await createCustomer('admin');
  }, 60_000);

  afterAll(async () => {
    vi.restoreAllMocks();
    if (appPool) await appPool.end();
    if (db) await db.end();
  });

  // -------------------------------------------------------------------------
  describe('the paid cookie yields to the browser\'s account session', () => {
    it('an account that is not a keeper does not read the conversation of the browser that paid for a trial', async () => {
      const paid = await paidTrial('nonkeeper');
      const other = await createCustomer('nonkeeper-account');
      expect((await tablePage(paid.agent)).text).toContain(paid.phrase);

      await logIn(paid.agent, other.email);
      const page = await tablePage(paid.agent);
      expect(page.status).toBe(200);
      expect(page.text).not.toContain(paid.phrase);
      // The browser now holds a trial of its own, not the paid cookie.
      const replaced = setCookies(page).find((c) => c.startsWith(`${TRIAL_COOKIE}=`));
      expect(replaced, 'a new trial cookie is set').toBeDefined();
      expect(String(replaced).split(';')[0]).not.toBe(`${TRIAL_COOKIE}=${paid.token}`);
    });

    it('an account that is not a keeper cannot open the lamp page of the build the paying browser paid for', async () => {
      const paid = await paidTrial('lampcheck');
      const other = await createCustomer('lampcheck-account');
      expect((await paid.agent.get(`/lighthouse/lamp/${paid.buildId}`)).status).toBe(200);

      await logIn(paid.agent, other.email);
      const lamp = await paid.agent.get(`/lighthouse/lamp/${paid.buildId}`);
      expect(lamp.status).toBe(404);
      expect(lamp.text).not.toContain('Cancel subscription');
    });

    it('a keeper logged in on the browser that paid for another keeper reads their own conversation', async () => {
      const own = await paidTrial('ownkeeper');
      // The payer's account is unverified until its mailbox is proven; login needs a verified account.
      await q('UPDATE customers SET email_verified = TRUE WHERE id = $1', [own.payer.id]);
      const other = await paidTrial('otherkeeper');

      await logIn(other.agent, own.payer.email);
      const page = await tablePage(other.agent);
      expect(page.text).toContain(own.phrase);
      expect(page.text).not.toContain(other.phrase);
    });

    it('logout clears the paid cookie, so the next visitor of that browser starts a new trial', async () => {
      const paid = await paidTrial('logout');
      const other = await createCustomer('logout-account');
      await logIn(paid.agent, other.email);
      // A logged-in page that renders a form token: the account deletion page (requireAuth).
      const csrf = csrfFrom((await paid.agent.get('/account/delete')).text);
      expect(csrf).not.toBe('');

      const out = await paid.agent.post('/logout').type('form').send({ _csrf: csrf });
      expect(out.status).toBe(302);
      const cleared = setCookies(out).find((c) => c.startsWith(`${TRIAL_COOKIE}=`));
      expect(cleared, 'the trial cookie is cleared by logout').toMatch(/^cas_harbor_trial=;.*Expires=Thu, 01 Jan 1970/);

      const next = await tablePage(paid.agent);
      expect(next.text).not.toContain(paid.phrase);
    });

    it('logout does not clear an unpaid trial cookie, so a visitor who logs in and out keeps their own conversation', async () => {
      const visitor = await trialBrowser();
      await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, $2, 'Tell me more.')`, [
        visitor.trialId,
        `unpaid-conversation-${run}`,
      ]);
      const other = await createCustomer('unpaid-logout-account');
      await logIn(visitor.agent, other.email);
      const csrf = csrfFrom((await visitor.agent.get('/account/delete')).text);

      const out = await visitor.agent.post('/logout').type('form').send({ _csrf: csrf });
      expect(out.status).toBe(302);
      expect(setCookies(out).some((c) => c.startsWith(`${TRIAL_COOKIE}=;`))).toBe(false);
      expect((await tablePage(visitor.agent)).text).toContain(`unpaid-conversation-${run}`);
    });

    it('a completed password reset expires the paid cookie of the account it was promoted into', async () => {
      const paid = await paidTrial('reset');
      expect((await tablePage(paid.agent)).text).toContain(paid.phrase);

      const raw = crypto.randomBytes(32).toString('hex');
      await q('INSERT INTO password_reset_tokens (customer_id, token) VALUES ($1, $2)', [paid.payer.id, sha256(raw)]);
      const elsewhere = browser();
      // The token first: it is good only in the session its page started, and an
      // agent attaches its cookies when a request is created, not when it is sent.
      const csrf = await freshCsrf(elsewhere);
      const reset = await elsewhere
        .post('/reset-password')
        .type('form')
        .send({ _csrf: csrf, token: raw, password: `new-${run}-${PASSWORD}` });
      expect(reset.text).toContain('Your password is set');

      expect((await tablePage(paid.agent)).text).not.toContain(paid.phrase);
      expect((await paid.agent.get(`/lighthouse/lamp/${paid.buildId}`)).status).toBe(404);
      const row = await one<{ live: boolean }>('SELECT expires_at > NOW() AS live FROM trial_keepers WHERE trial_id = $1', [paid.trialId]);
      expect(row.live).toBe(false);
    });

    it('a completed email verification expires the paid cookie of the account it was promoted into', async () => {
      const paid = await paidTrial('verify');
      expect((await tablePage(paid.agent)).text).toContain(paid.phrase);

      const raw = crypto.randomBytes(32).toString('hex');
      await q('INSERT INTO email_verification_tokens (customer_id, token) VALUES ($1, $2)', [paid.payer.id, sha256(raw)]);
      // Opening the link shows a page; its button posts the token back.
      const page = await paid.agent.get(`/verify-email?token=${raw}`);
      const opened = await paid.agent.post('/verify-email').type('form').send({ _csrf: csrfFrom(page.text), token: raw });
      expect(opened.status).toBe(302);
      expect((await one<{ email_verified: boolean }>('SELECT email_verified FROM customers WHERE id = $1', [paid.payer.id])).email_verified).toBe(true);

      expect((await tablePage(paid.agent)).text).not.toContain(paid.phrase);
    });
  });

  // -------------------------------------------------------------------------
  describe('an anonymous dispatch does not attach a build to an account that already exists', () => {
    it('an unverified account that already has a build: an anonymous request for its address is refused, and no start is counted against it', async () => {
      const victim = await createCustomer('victim-with-build', { verified: false });
      const existingBuild = await createBuild(victim.id);
      const before = (await buildsOf(victim.id)).length;
      const visitor = await readyTrial(victim.email);
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('email_not_usable');
        // Refused before the model call, so nothing was paid for either.
        expect(extraction.calls()).toBe(0);
        expect((await buildsOf(victim.id)).map((b) => b.id)).toEqual([existingBuild]);
        expect(before).toBe(1);
        expect(await ledgerOf(victim.id)).toEqual([]);
        expect(await one<{ n: string }>('SELECT COUNT(*) AS n FROM builds WHERE customer_id = $1 AND status IN (\'queued\', \'building\', \'testing\', \'deploying\')', [victim.id]))
          .toEqual({ n: '0' });
        // The conversation is stamped with nothing, and the address is forgotten, so the visitor can give another.
        const trial = await one<{ handoff_build_id: string | null; handoff_at: Date | null; email_captured: string | null }>(
          'SELECT handoff_build_id, handoff_at, email_captured FROM trial_keepers WHERE trial_id = $1',
          [visitor.trialId]
        );
        expect(trial).toEqual({ handoff_build_id: null, handoff_at: null, email_captured: null });
      } finally {
        extraction.restore();
      }
    });

    it('an unverified account that has a keeper row (a payer\'s account): refused the same way', async () => {
      const paid = await paidTrial('payer-address');
      const visitor = await readyTrial(paid.payer.email);
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('email_not_usable');
        expect(extraction.calls()).toBe(0);
        expect(await ledgerOf(paid.payer.id)).toEqual([]);
      } finally {
        extraction.restore();
      }
    });

    it('the typed error is address_in_use, answered 409 by the dispatch failure table, and nothing is written for the account', async () => {
      const { HarborDispatchError, dispatchFailureResponse } = harborDispatch;
      const victim = await createCustomer('typed-error', { verified: false });
      await createBuild(victim.id);
      const visitor = await trialBrowser();
      await q('UPDATE trial_keepers SET is_synthetic = FALSE WHERE trial_id = $1', [visitor.trialId]);
      const extraction = standInForExtraction();
      try {
        const err = await harborDispatch
          .dispatchHarborTrialBuild(appDbPool, {
            trialId: visitor.trialId,
            resolvedEmail: victim.email,
            source: 'self_serve',
            refuseBarrenSpec: true,
            anonymous: true,
          })
          .then(
            () => null,
            (caught: unknown) => caught
          );
        expect(err).toBeInstanceOf(HarborDispatchError);
        expect((err as { code: string }).code).toBe('address_in_use');
        expect(dispatchFailureResponse(err)).toMatchObject({ status: 409, body: { error: 'address_in_use' } });
        expect(extraction.calls()).toBe(0);
        expect(await ledgerOf(victim.id)).toEqual([]);
      } finally {
        extraction.restore();
      }
    });

    it('an account that comes into being while the extraction runs is refused at the insert, in the insert\'s transaction', async () => {
      // The early check finds no account for the address. While the model
      // call runs, a signup (or another conversation's dispatch) creates one
      // and a build lands on it. The insert, which creates the dispatch's own
      // account row in the same transaction as the build, must find the
      // address taken and refuse.
      const address = email('raced-account');
      const visitor = await readyTrial(address);
      let racer: { id: string; email: string } | null = null;
      const extraction = standInForExtraction(async () => {
        racer = await createCustomer('raced-account', { verified: false });
        await createBuild(racer.id);
      });
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('email_not_usable');
        expect(extraction.calls()).toBe(1);
        const raced = racer as unknown as { id: string };
        // Only the build that landed during the extraction exists, and no start is counted for this dispatch.
        expect(await buildsOf(raced.id)).toHaveLength(1);
        expect(await ledgerOf(raced.id)).toEqual([]);
      } finally {
        extraction.restore();
      }
    });

    it('a new address, with no account, still gets its build from an anonymous dispatch, and the start is in the ledger', async () => {
      const fresh = email('fresh-anonymous');
      const visitor = await readyTrial(fresh);
      const extraction = standInForExtraction();
      try {
        const res = await visitor.agent.post('/lighthouse/build').send({});
        expect(res.status).toBe(200);
        const account = await one<{ id: string }>('SELECT id FROM customers WHERE email = $1', [fresh]);
        expect(await ledgerOf(account.id)).toEqual([{ build_id: res.body.buildId, kind: 'dispatch' }]);
      } finally {
        extraction.restore();
      }
    });
  });
});
