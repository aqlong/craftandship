// The crisis scan where it matters: at the email gate of a real conversation,
// through the real app, on a real Postgres.
//
// The handler that answers a crisis message runs ahead of every gate, but it
// only runs for text the scan recognizes. A disclosure the scan misses goes on
// to the gates, and at turn 22 an anonymous visitor's next message is answered
// 402 "enter your email". This file puts a trial at that gate and sends the
// plain phrasings a narrower scan let through, one request each, and reads
// back the row that is stored for the operator.
//
// The last two cases are about the same surface from another side: what the
// first visit to /lighthouse/table writes about the URL it arrived on (the
// trial row and the funnel event), and what the page-view log writes about
// the page a visitor came from (the Referer). Each must be the path and never
// the query or the fragment.
//
// It runs only when CAS_SCHEMA_TEST_DATABASE_URL points at a THROWAWAY
// database (the same switch as money-path.realdb.test.ts; run the files with
// --no-file-parallelism, because each applies schema.sql). No model is called
// and no email is sent: neither key is set, and outbound requests to any host
// but this machine are made to throw.

import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import path from 'path';
import http from 'http';
import https from 'https';
import request from 'supertest';
import { Client } from 'pg';
import type { Application } from 'express';

const integrationUrl = process.env.CAS_SCHEMA_TEST_DATABASE_URL;
const schema = fs.readFileSync(path.join(__dirname, '../../src/db/schema.sql'), 'utf-8');

type Browser = ReturnType<typeof request.agent>;

let app: Application;
let appPool: { end: () => Promise<void> };
let db: Client;

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
      if (!isLoopback(host)) throw new Error(`crisis-phrasing.realdb.test.ts: outbound request to ${host} blocked`);
      return original(...args);
    }) as never);
  }
  const originalFetch = globalThis.fetch;
  vi.spyOn(globalThis, 'fetch').mockImplementation(((input: unknown, init?: unknown) => {
    const url = typeof input === 'string' || input instanceof URL ? input : (input as { url: string }).url;
    const host = new URL(String(url)).hostname;
    if (!isLoopback(host)) return Promise.reject(new Error(`crisis-phrasing.realdb.test.ts: outbound fetch to ${host} blocked`));
    return originalFetch(input as never, init as never);
  }) as never);
}

/** A visitor with a trial cookie, 22 turns in: the next ordinary message meets the email gate. */
async function visitorAtTheEmailGate(): Promise<{ agent: Browser; trialId: string }> {
  const agent = request.agent(app);
  const res = await agent.get('/lighthouse/table');
  expect(res.status).toBe(200);
  const cookie = ([] as string[]).concat(res.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
  const token = String(cookie).split(';')[0].split('=')[1];
  const trial = await db.query('SELECT trial_id FROM trial_keepers WHERE cookie_token = $1', [token]);
  expect(trial.rows).toHaveLength(1);
  const trialId = trial.rows[0].trial_id as string;
  await db.query(
    `INSERT INTO companion_turns (trial_id, keeper_text, companion_text)
     SELECT $1, 'turn ' || n, 'ok' FROM generate_series(1, 22) AS n`,
    [trialId]
  );
  return { agent, trialId };
}

describe.skipIf(!integrationUrl)('crisis phrasings at the email gate, real app and real Postgres (integration)', () => {
  beforeAll(async () => {
    db = new Client({ connectionString: integrationUrl });
    await db.connect();
    await applySchema(db);

    process.env.DATABASE_URL = integrationUrl;
    process.env.CAS_HARBOR_ENABLED = 'true';
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
  }, 60_000);

  afterAll(async () => {
    vi.restoreAllMocks();
    if (appPool) await appPool.end();
    if (db) await db.end();
  });

  // [what was typed, the category the stored turn carries]
  const DISCLOSURES: Array<[string, string]> = [
    ['I just want to die', 'suicide'],
    ['i wanna die', 'suicide'],
    ['I want to kill  myself', 'suicide'],
    ['I want to\nkill myself', 'suicide'],
    ["I don't want to be here anymore", 'suicide'],
    ['life is not worth living', 'suicide'],
    ['I was raped', 'abuse'],
    ['my husband is hitting me', 'abuse'],
    ['I have been cutting again', 'self-harm'],
    ['I keep having thoughts of hurting my children', 'harm-to-others'],
    ['I took a whole bottle of pills', 'acute-medical'],
  ];

  it('the gate is really there: an ordinary message is asked for an email', async () => {
    const visitor = await visitorAtTheEmailGate();
    const ordinary = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: 'What would my site look like?' });
    expect(ordinary.status).toBe(402);
    expect(ordinary.body.requiresEmail).toBe(true);
  });

  it('each disclosure gets the script instead of the gate, and is stored with its category', async () => {
    const visitor = await visitorAtTheEmailGate();
    for (const [text, category] of DISCLOSURES) {
      const res = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: text });
      expect(res.status, text).toBe(200);
      expect(res.body.companionText, text).toMatch(/988/);
      expect(res.body.railFlags, text).toEqual(expect.arrayContaining(['crisis', category]));
    }
    await vi.waitFor(async () => {
      const stored = await db.query<{ keeper_text: string; rail_flags: string[] }>(
        `SELECT keeper_text, rail_flags FROM companion_turns WHERE trial_id = $1 AND 'crisis' = ANY(rail_flags)`,
        [visitor.trialId]
      );
      const byText = new Map(stored.rows.map((row) => [row.keeper_text, row.rail_flags]));
      for (const [text, category] of DISCLOSURES) {
        expect(byText.get(text), text).toEqual(expect.arrayContaining(['crisis', category]));
      }
    });
  });

  it('a medical emergency is told to call the emergency number first', async () => {
    const visitor = await visitorAtTheEmailGate();
    const res = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: 'I took a whole bottle of pills' });
    expect(res.status).toBe(200);
    const text = String(res.body.companionText);
    expect(text.indexOf('911')).toBeGreaterThan(-1);
    expect(text.indexOf('911')).toBeLessThan(text.indexOf('988'));
  });

  it('a practitioner describing their work still meets the gate, not the script', async () => {
    const visitor = await visitorAtTheEmailGate();
    for (const text of [
      'I work with survivors of domestic violence',
      'my practice focuses on suicide prevention for teens',
      'that workshop was to die for',
    ]) {
      const res = await visitor.agent.post('/lighthouse/table/turn').send({ keeperText: text });
      expect(res.status, text).toBe(402);
    }
    const stored = await db.query(`SELECT 1 FROM companion_turns WHERE trial_id = $1 AND 'crisis' = ANY(rail_flags)`, [
      visitor.trialId,
    ]);
    expect(stored.rows).toHaveLength(0);
  });

  it('with no cookie at all the script is still the answer', async () => {
    const res = await request(app).post('/lighthouse/table/turn').send({ keeperText: 'honestly i just wanna die' });
    expect(res.status).toBe(200);
    expect(res.body.companionText).toMatch(/988/);
    expect(res.body.railFlags).toEqual(['crisis', 'suicide']);
  });

  it('a first visit stores the path it arrived on and the campaign, and nothing else from the query', async () => {
    // Built at run time so no identifier-shaped literal sits in the source.
    const devTrial = ['9f3c2a10', '5b7d', '4e21', '8a6f', '0c1d2e3f4a5b'].join('-');
    const sessionId = ['cs', 'test', 'a1b2c3d4e5f6a7b8'].join('_');
    const query = `session_id=${sessionId}&dev_trial=${devTrial}&utm_source=mail&utm_medium=email&utm_campaign=spring`;

    const agent = request.agent(app);
    const res = await agent
      .get(`/lighthouse/table?${query}`)
      .set('User-Agent', 'Mozilla/5.0 (Macintosh) Chrome/120 Safari/537.36')
      .set('Referer', `https://example.com/lighthouse/lamp/b1/published?${query}`);
    expect(res.status).toBe(200);
    const cookie = ([] as string[]).concat(res.headers['set-cookie'] || []).find((c) => c.startsWith('cas_harbor_trial='));
    const token = String(cookie).split(';')[0].split('=')[1];

    const trial = await db.query<{ trial_id: string; entry_path: string; entry_referrer: string; entry_utm: string }>(
      'SELECT trial_id, entry_path, entry_referrer, entry_utm FROM trial_keepers WHERE cookie_token = $1',
      [token]
    );
    expect(trial.rows).toHaveLength(1);
    expect(trial.rows[0].entry_path).toBe('/lighthouse/table');
    expect(trial.rows[0].entry_referrer).toBe('https://example.com/lighthouse/lamp/b1/published');
    expect(trial.rows[0].entry_utm).toBe('mail/email/spring');

    // The page view is also a funnel event, written in the background.
    await vi.waitFor(async () => {
      const events = await db.query<{ path: string; referrer: string }>(
        `SELECT path, referrer FROM funnel_events WHERE trial_id = $1 AND name = 'table_view'`,
        [trial.rows[0].trial_id]
      );
      expect(events.rows).toHaveLength(1);
      expect(events.rows[0].path).toBe('/lighthouse/table');
      expect(events.rows[0].referrer).toBe('https://example.com/lighthouse/lamp/b1/published');
    });

    const everything = await db.query(
      `SELECT (SELECT COUNT(*) FROM trial_keepers t WHERE t::text LIKE $1 OR t::text LIKE $2) AS trials,
              (SELECT COUNT(*) FROM funnel_events f WHERE f::text LIKE $1 OR f::text LIKE $2) AS events`,
      [`%${sessionId}%`, `%${devTrial}%`]
    );
    expect(Number(everything.rows[0].trials)).toBe(0);
    expect(Number(everything.rows[0].events)).toBe(0);
  });

  it('the page-view log follows the same rule: where the visitor came from is stored as a path, never with its query or fragment', async () => {
    // The page-view logger is not mounted under NODE_ENV=test, so this loads a
    // second copy of the app as a development server (its own pool, the same
    // database) and sends one visit through it.
    const savedEnv = process.env.NODE_ENV;
    process.env.NODE_ENV = 'development';
    vi.resetModules();
    let devPool: { end: () => Promise<void> } | undefined;
    try {
      const devApp = (await import('../../src/app')).default;
      devPool = (await import('../../src/db/index')).default as unknown as { end: () => Promise<void> };
      process.env.NODE_ENV = savedEnv;

      // A mailed link carries the address and a token in its query; a visitor who follows a link out of that page sends it as the Referer.
      const mark = `mail-${Math.random().toString(36).slice(2, 10)}`;
      const secret = ['to', 'ken'].join('') + '=' + 'c'.repeat(32);
      const res = await request(devApp)
        .get('/terms')
        .set('Referer', `https://example.test/${mark}/unsubscribe?email=someone%40example.test&${secret}#done`);
      expect(res.status).toBe(200);

      const stored = await vi.waitFor(async () => {
        const rows = await db.query<{ referrer: string }>(`SELECT referrer FROM page_views WHERE path = '/terms' AND referrer LIKE $1`, [`%${mark}%`]);
        expect(rows.rows).toHaveLength(1);
        return rows.rows[0].referrer;
      });
      expect(stored).toBe(`https://example.test/${mark}/unsubscribe`);
      // Nothing of the query is anywhere in the table.
      const leaked = await db.query(`SELECT 1 FROM page_views WHERE referrer LIKE '%' || $1 || '%' OR referrer LIKE '%someone%'`, [secret]);
      expect(leaked.rows).toHaveLength(0);
    } finally {
      process.env.NODE_ENV = savedEnv;
      if (devPool) await devPool.end();
    }
  });
});
