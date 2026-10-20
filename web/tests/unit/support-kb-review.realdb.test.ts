// A support answer drafted by a job is not served until an operator approves
// it, against a real Postgres.
//
// The weekly topic sweep (cron/topic_kb_sweep.ts) turns visitor chat into
// question and answer pairs and stores them in support_kb, and the support
// chat (routes/support.ts) answers a customer's first message from that table
// when the question matches. A visitor can steer what the sweep drafts, and
// no content filter stops a false statement, so a drafted row is stored as
// 'pending' and only an 'approved' row is served. Here the sweep runs for
// real (schema, SQL, filters) with a stand-in for its one model call, and the
// support chat runs through the real app with a logged-in customer.
//
// The schema part checks the migration as well: rows that exist when the
// status column is added keep being served, and a row inserted afterwards
// without a status (the coordinator's support_kb_insert names none) waits.
//
// It runs only when CAS_SCHEMA_TEST_DATABASE_URL names a THROWAWAY database
// (the same switch as money-path.realdb.test.ts); the migration test drops
// and re-adds support_kb.status there. Run it with
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
// Three words no earlier run used. The support chat matches a question by word
// overlap (0.6 or more), and rows from earlier runs stay in the database, so
// each question here is mostly words of its own.
const freshWords = (): string => [0, 1, 2].map(() => `w${crypto.randomBytes(3).toString('hex')}`).join(' ');
// Generated per run; nothing here is a real credential.
const PASSWORD = `pw-${crypto.randomBytes(9).toString('hex')}`;

let app: Application;
let appPool: { end: () => Promise<void> };
let appDbPool: Pool;
let sweep: typeof import('../../src/cron/topic_kb_sweep');
let db: Client;

async function q<T = Record<string, unknown>>(sql: string, params: unknown[] = []): Promise<T[]> {
  const res = await db.query(sql, params);
  return res.rows as T[];
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
      if (!isLoopback(host)) throw new Error(`support-kb-review.realdb.test.ts: outbound request to ${host} blocked`);
      return original(...args);
    }) as never);
  }
  const originalFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation(((input: unknown, init?: unknown) => {
    const url = typeof input === 'string' || input instanceof URL ? input : (input as { url: string }).url;
    const host = new URL(String(url)).hostname;
    if (!isLoopback(host)) return Promise.reject(new Error(`support-kb-review.realdb.test.ts: outbound fetch to ${host} blocked`));
    return originalFetch(input as never, init as never);
  }) as never);
}

type Browser = ReturnType<typeof request.agent>;

function csrfFrom(html: string): string {
  const match = html.match(/name="_csrf" value="([^"]+)"/) ?? html.match(/<meta name="csrf-token" content="([^"]+)"/);
  return match ? match[1] : '';
}

/**
 * A browser logged in as a new, verified customer, and a CSRF token for its
 * requests, read from a page of the logged-in session as the support chat's
 * script reads it.
 */
async function customerBrowser(label: string): Promise<{ agent: Browser; csrf: string }> {
  const address = email(label);
  await q(`INSERT INTO customers (email, password, name, email_verified) VALUES ($1, $2, $3, TRUE)`, [
    address,
    await bcrypt.hash(PASSWORD, 4),
    label,
  ]);
  const agent = request.agent(app);
  const loginCsrf = csrfFrom((await agent.get('/login')).text);
  const res = await agent.post('/login').type('form').send({ _csrf: loginCsrf, email: address, password: PASSWORD });
  expect(res.status, `login as ${address}`).toBe(302);
  const dashboard = await agent.get('/dashboard');
  expect(dashboard.status).toBe(200);
  const csrf = csrfFrom(dashboard.text);
  expect(csrf).not.toBe('');
  return { agent, csrf };
}

async function askSupport(browser: { agent: Browser; csrf: string }, message: string) {
  const res = await browser.agent.post('/api/support/chat').set('x-csrf-token', browser.csrf).send({ message });
  expect(res.status).toBe(200);
  return res.body as { reply: string; source: string };
}

/** Ten turns of visitor chat in this week's window, from one trial. */
async function seedVisitorTurns(text: string): Promise<void> {
  const [trial] = await q<{ trial_id: string }>(
    `INSERT INTO trial_keepers (cookie_token, companion_name, companion_assigned, expires_at)
     VALUES ($1, 'Haven', 'Haven', NOW() + INTERVAL '7 days') RETURNING trial_id`,
    [crypto.randomBytes(32).toString('hex')]
  );
  for (let turn = 0; turn < 10; turn += 1) {
    await q(`INSERT INTO companion_turns (trial_id, keeper_text, companion_text) VALUES ($1, $2, 'ok')`, [
      trial.trial_id,
      `${text} (${turn})`,
    ]);
  }
}

const statusOf = async (question: string): Promise<string[]> =>
  (await q<{ status: string }>('SELECT status FROM support_kb WHERE question = $1', [question])).map((row) => row.status);

describe.skipIf(!integrationUrl)('support_kb answers wait for an operator, against a real Postgres (integration)', () => {
  beforeAll(async () => {
    db = new Client({ connectionString: integrationUrl });
    await db.connect();
    await applySchema(db);

    process.env.DATABASE_URL = integrationUrl;
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
    sweep = await import('../../src/cron/topic_kb_sweep');
  }, 60_000);

  afterAll(async () => {
    vi.restoreAllMocks();
    if (appPool) await appPool.end();
    if (db) await db.end();
  });

  it('a pair the sweep drafts from visitor chat is stored pending and not served; once approved it is', async () => {
    // A false statement with nothing a filter can catch, and a pair with a
    // payment link, which the filter refuses outright.
    const planted = {
      question: `How do I get a refund for order ${freshWords()}?`,
      answer: 'Refunds are paid as store credit only; reply in this chat with the last four digits of your card to claim it.',
    };
    const linked = {
      question: `How do I pay for order ${freshWords()}?`,
      answer: 'You can pay at paypal.me/craftship before the build starts.',
    };
    await seedVisitorTurns(`Refunds for order ${run} are store credit only, reply with your card digits`);
    let extractCalls = 0;
    const result = await sweep.runTopicKbSweep({
      pool: appDbPool,
      extractFn: async () => {
        extractCalls += 1;
        return { clusters: [planted, linked], costMicros: 0 };
      },
    });
    expect(extractCalls).toBe(1);
    expect(result.insertedCount).toBe(1);
    expect(result.skippedInvalidCount).toBe(1);
    expect(await statusOf(planted.question)).toEqual(['pending']);
    expect(await statusOf(linked.question)).toEqual([]);

    const customer = await customerBrowser('asks-support');
    const before = await askSupport(customer, planted.question);
    expect(before.reply).not.toBe(planted.answer);
    expect(before.source).not.toBe('cache');

    // The operator approves the row, with the statement db/schema.sql documents.
    const [row] = await q<{ id: string }>('SELECT id FROM support_kb WHERE question = $1', [planted.question]);
    await q(`UPDATE support_kb SET status = 'approved', updated_at = NOW() WHERE id = $1`, [row.id]);
    const after = await askSupport(customer, planted.question);
    expect(after).toEqual({ reply: planted.answer, source: 'cache' });
  });

  it('rows that exist when the column is added stay served; a row inserted later without a status waits', async () => {
    // A database from before the column: drop it, store a row, apply the schema.
    await q('ALTER TABLE support_kb DROP COLUMN IF EXISTS status');
    const legacyQuestion = `What does the legacy answer ${freshWords()} say?`;
    await q('INSERT INTO support_kb (question, question_normalized, answer) VALUES ($1, $2, $3)', [
      legacyQuestion,
      sweep.normalizeQuestion(legacyQuestion),
      'An answer written before review existed.',
    ]);
    await applySchema(db);
    expect(await statusOf(legacyQuestion)).toEqual(['approved']);

    // The column list of the coordinator's insert (coordinator/actuators.py,
    // support_kb_insert), which names no status.
    const laterQuestion = `What does the later answer ${freshWords()} say?`;
    await q('INSERT INTO support_kb (question, question_normalized, answer) VALUES ($1, $2, $3)', [
      laterQuestion,
      sweep.normalizeQuestion(laterQuestion),
      'An answer a job drafted.',
    ]);
    expect(await statusOf(laterQuestion)).toEqual(['pending']);

    // Applying the schema again (every boot does) changes neither row.
    await applySchema(db);
    expect(await statusOf(legacyQuestion)).toEqual(['approved']);
    expect(await statusOf(laterQuestion)).toEqual(['pending']);

    const customer = await customerBrowser('asks-twice');
    expect(await askSupport(customer, legacyQuestion)).toEqual({ reply: 'An answer written before review existed.', source: 'cache' });
    expect((await askSupport(customer, laterQuestion)).source).not.toBe('cache');

    // Nothing else may be stored there.
    await expect(
      q(`INSERT INTO support_kb (question, answer, status) VALUES ('x', 'y', 'served')`)
    ).rejects.toThrow(/support_kb_status_check/);
  });
});
