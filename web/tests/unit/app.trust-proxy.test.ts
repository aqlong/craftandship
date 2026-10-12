// TRUST_PROXY_HOPS: how many hops of X-Forwarded-* headers the app believes.
//
// Too high and a client writes its own address: with the value 1 and nothing
// in front of the server, a different X-Forwarded-For on every request walks
// past every per-address rate limit. The local template therefore ships 0,
// and an unset value (which means 1) is called out at boot.

import { describe, it, expect, vi, afterEach } from 'vitest';
import express from 'express';
import request from 'supertest';
import fs from 'fs';
import path from 'path';
import type { Application } from 'express';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));

import { makeAuthRateLimit } from '../../src/middleware/auth-rate-limit';

const ENV_KEYS = ['NODE_ENV', 'SESSION_SECRET', 'BASE_URL', 'TRUST_PROXY_HOPS', 'PREVIEW_PASSWORD', 'CAS_HARBOR_ENABLED'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];

type AppModule = typeof import('../../src/app');

/** Import a fresh src/app.ts under the given environment ('' stands for unset). */
async function loadAppModule(env: Partial<Record<(typeof ENV_KEYS)[number], string>>): Promise<AppModule> {
  for (const key of ENV_KEYS) process.env[key] = env[key] ?? '';
  vi.resetModules();
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
  return import('../../src/app');
}

function captureWarnings(): () => string {
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
  return () => warn.mock.calls.map((call) => call.join(' ')).join('\n');
}

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.restoreAllMocks();
});

/** The value web/.env.example ships, as dotenv would read it. */
function templateValue(): string {
  const template = fs.readFileSync(path.join(__dirname, '..', '..', '.env.example'), 'utf-8');
  const match = template.match(/^TRUST_PROXY_HOPS=(.*)$/m);
  if (!match) throw new Error('TRUST_PROXY_HOPS is missing from .env.example');
  return match[1];
}

describe('resolveTrustProxyHops', () => {
  it('reads a whole number as given, with no warning', async () => {
    const { resolveTrustProxyHops } = await loadAppModule({ NODE_ENV: 'test' });
    expect(resolveTrustProxyHops({ TRUST_PROXY_HOPS: '0', NODE_ENV: 'production' })).toEqual({ hops: 0, warning: null });
    expect(resolveTrustProxyHops({ TRUST_PROXY_HOPS: '1', NODE_ENV: 'production' })).toEqual({ hops: 1, warning: null });
    expect(resolveTrustProxyHops({ TRUST_PROXY_HOPS: ' 2 ', NODE_ENV: 'development' })).toEqual({ hops: 2, warning: null });
  });

  it('treats unset and empty as 1 and says so', async () => {
    const { resolveTrustProxyHops } = await loadAppModule({ NODE_ENV: 'test' });
    for (const env of [{ NODE_ENV: 'production' }, { NODE_ENV: 'development', TRUST_PROXY_HOPS: '' }, { TRUST_PROXY_HOPS: '   ' }]) {
      const resolved = resolveTrustProxyHops(env);
      expect(resolved.hops).toBe(1);
      expect(resolved.warning).toContain('TRUST_PROXY_HOPS is not set');
      expect(resolved.warning).toContain('TRUST_PROXY_HOPS=0');
    }
  });

  it('does not guess at a value that is not a whole number: 1, with a warning that quotes it', async () => {
    const { resolveTrustProxyHops } = await loadAppModule({ NODE_ENV: 'test' });
    for (const raw of ['true', 'yes', '-1', '1.5', '1 proxy', '0x1', 'all']) {
      const resolved = resolveTrustProxyHops({ TRUST_PROXY_HOPS: raw, NODE_ENV: 'production' });
      expect(resolved.hops, raw).toBe(1);
      expect(resolved.warning, raw).toContain(`"${raw}"`);
      expect(resolved.warning, raw).toContain('not a whole number');
    }
  });

  it('is silent under NODE_ENV=test, where the suite imports the app over and over', async () => {
    const { resolveTrustProxyHops } = await loadAppModule({ NODE_ENV: 'test' });
    expect(resolveTrustProxyHops({ NODE_ENV: 'test' })).toEqual({ hops: 1, warning: null });
  });
});

describe('the app applies the setting at boot', () => {
  it('warns when the variable is unset in development and trusts one hop', async () => {
    const warnings = captureWarnings();
    const module = await loadAppModule({ NODE_ENV: 'development' });
    expect((module.default as unknown as Application).get('trust proxy')).toBe(1);
    expect(warnings()).toContain('TRUST_PROXY_HOPS is not set');
  });

  it('with the value the template ships, trusts no hop and has nothing to warn about', async () => {
    expect(templateValue()).toBe('0');
    const warnings = captureWarnings();
    const module = await loadAppModule({ NODE_ENV: 'development', TRUST_PROXY_HOPS: templateValue() });
    expect((module.default as unknown as Application).get('trust proxy')).toBe(0);
    expect(warnings()).not.toContain('TRUST_PROXY_HOPS');
  });
});

describe('what the setting does to a per-address limit', () => {
  // The auth limiter with an address cap of 3, behind `trust proxy` set the
  // way the app sets it. Every request claims a new address and a new email.
  async function statusesWith(hops: number): Promise<number[]> {
    const mini = express();
    mini.set('trust proxy', hops);
    mini.use(express.json());
    mini.post('/login', makeAuthRateLimit({ max: 2, ipMax: 3, windowMs: 60_000, skipInTest: false }), (_req, res) => {
      res.json({ ok: true });
    });
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const statuses: number[] = [];
    for (let i = 1; i <= 8; i++) {
      const res = await request(mini)
        .post('/login')
        .set('X-Forwarded-For', `203.0.113.${i}`)
        .send({ email: `rotating${i}@example.com` });
      statuses.push(res.status);
    }
    return statuses;
  }

  it('with the template value, a rotating X-Forwarded-For changes nothing: the fourth request is refused', async () => {
    const { resolveTrustProxyHops } = await loadAppModule({ NODE_ENV: 'test' });
    const { hops } = resolveTrustProxyHops({ TRUST_PROXY_HOPS: templateValue(), NODE_ENV: 'development' });
    expect(await statusesWith(hops)).toEqual([200, 200, 200, 429, 429, 429, 429, 429]);
  });

  it('with one trusted hop and no proxy, the same client is never refused (why 1 is wrong for a local run)', async () => {
    expect(await statusesWith(1)).toEqual([200, 200, 200, 200, 200, 200, 200, 200]);
  });
});
