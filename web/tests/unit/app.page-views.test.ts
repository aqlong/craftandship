// Page-view logging is bounded (src/app.ts).
//
// Every anonymous page GET used to write a page_views row with no limit. The
// rows are now capped per client address and for everyone together, per
// minute, in memory. The middleware is not mounted under NODE_ENV=test, so
// each test imports a fresh copy of the app in development, as
// app.boot-security.test.ts does.

import { describe, it, expect, vi, afterEach } from 'vitest';
import request from 'supertest';
import type { Application } from 'express';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));

const ENV_KEYS = ['NODE_ENV', 'SESSION_SECRET', 'TRUST_PROXY_HOPS', 'CAS_HARBOR_ENABLED', 'PREVIEW_PASSWORD', 'ADMIN_EMAIL'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];

// Assembled at runtime so no secret-shaped literal sits in the repo.
const STRONG_SECRET = Array.from({ length: 48 }, (_unused, i) => String.fromCharCode(97 + (i % 26))).join('');

interface Loaded {
  app: Application;
  perAddress: number;
  overall: number;
  inserts: () => Array<unknown[]>;
}

/** A fresh development app whose page-view inserts are counted. One proxy hop is trusted, so a test can choose its address. */
async function loadApp(): Promise<Loaded> {
  for (const key of ENV_KEYS) process.env[key] = '';
  process.env.NODE_ENV = 'development';
  process.env.SESSION_SECRET = STRONG_SECRET;
  process.env.TRUST_PROXY_HOPS = '1';
  vi.resetModules();
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  const appModule = await import('../../src/app');
  return {
    app: appModule.default,
    perAddress: appModule.PAGE_VIEWS_PER_ADDRESS_PER_MINUTE,
    overall: appModule.PAGE_VIEWS_PER_MINUTE,
    inserts: () =>
      mockQuery.mock.calls
        .filter(([sql]) => typeof sql === 'string' && sql.includes('INSERT INTO page_views'))
        .map(([, params]) => params as unknown[]),
  };
}

const from = (app: Application, address: string, path = '/terms') => request(app).get(path).set('X-Forwarded-For', address);

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('page views: what one address, and all of them together, can write', () => {
  it('the limits are 30 a minute per address and 600 a minute overall', async () => {
    const { perAddress, overall } = await loadApp();
    expect(perAddress).toBe(30);
    expect(overall).toBe(600);
  });

  it('one address is recorded up to its cap, then only served; another address is unaffected', async () => {
    const { app, perAddress, inserts } = await loadApp();

    for (let i = 0; i < perAddress + 12; i += 1) {
      expect((await from(app, '203.0.113.7')).status).toBe(200);
    }
    expect(inserts()).toHaveLength(perAddress);

    expect((await from(app, '203.0.113.8')).status).toBe(200);
    expect(inserts()).toHaveLength(perAddress + 1);
    expect(inserts()[perAddress][0]).toBe('/terms');
  });

  it('the cap is a window: a minute later the same address is recorded again', async () => {
    // Only the clock is faked (timers and I/O are real), and before the app
    // is loaded, because the counters take their clock when they are made.
    const start = Date.now();
    vi.useFakeTimers({ toFake: ['Date'], now: start });
    const { app, perAddress, inserts } = await loadApp();
    for (let i = 0; i < perAddress + 3; i += 1) await from(app, '203.0.113.9');
    expect(inserts()).toHaveLength(perAddress);

    vi.setSystemTime(start + 59_000);
    await from(app, '203.0.113.9');
    expect(inserts()).toHaveLength(perAddress);

    vi.setSystemTime(start + 61_000);
    await from(app, '203.0.113.9');
    expect(inserts()).toHaveLength(perAddress + 1);
  });

  it('many addresses at once stop at the overall cap, and every page is still served', async () => {
    const { app, overall, inserts } = await loadApp();
    const statuses = new Set<number>();
    for (let i = 0; i < overall + 25; i += 1) {
      // A different address every time: none of them is near its own cap.
      const res = await from(app, `198.51.${Math.floor(i / 250)}.${(i % 250) + 1}`);
      statuses.add(res.status);
    }
    expect([...statuses]).toEqual([200]);
    expect(inserts()).toHaveLength(overall);
  }, 60_000);

  it('a client over its own cap does not use up the overall one', async () => {
    const { app, perAddress, overall, inserts } = await loadApp();
    // One address hammers far past its cap...
    for (let i = 0; i < perAddress * 3; i += 1) await from(app, '203.0.113.50');
    expect(inserts()).toHaveLength(perAddress);
    // ...and the overall budget has lost only what was actually written.
    for (let i = 0; i < overall - perAddress; i += 1) {
      await from(app, `198.51.${100 + Math.floor(i / 250)}.${(i % 250) + 1}`);
    }
    expect(inserts()).toHaveLength(overall);
  }, 60_000);

  it('the page the visitor came from is stored as a path: no query string, no fragment, bounded', async () => {
    const { app, inserts } = await loadApp();
    // A mailed link carries the address and a token in its query; following a link out of it sends it as the Referer.
    const token = ['t', 'o', 'k', 'e', 'n'].join('') + '=' + 'a'.repeat(40);
    await from(app, '203.0.113.70').set('Referer', `https://example.com/unsubscribe?email=someone%40example.test&${token}#done`);
    await from(app, '203.0.113.71').set('Referer', `https://example.com/${'p'.repeat(5000)}?x=1`);
    await from(app, '203.0.113.72');

    const [withQuery, tooLong, none] = inserts();
    // INSERT INTO page_views (path, utm_source, utm_medium, utm_campaign, referrer, ...): referrer is the fifth value.
    expect(withQuery[4]).toBe('https://example.com/unsubscribe');
    expect(String(tooLong[4]).length).toBeLessThanOrEqual(203);
    expect(String(tooLong[4]).startsWith('https://example.com/ppp')).toBe(true);
    expect(String(tooLong[4])).not.toContain('?');
    expect(none[4]).toBeNull();
  });

  it('what was never a page view is still not counted or recorded: assets, the health check, API paths, POSTs', async () => {
    const { app, inserts } = await loadApp();
    await from(app, '203.0.113.60', '/style.css');
    await from(app, '203.0.113.60', '/health');
    await from(app, '203.0.113.60', '/api/support/anything');
    await request(app).post('/contact').set('X-Forwarded-For', '203.0.113.60').send({});
    expect(inserts()).toEqual([]);
  });
});
