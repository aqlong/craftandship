import { describe, it, expect, vi, beforeAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import v8 from 'v8';
import vm from 'vm';

// What an unauthenticated client can make the rate limiters remember.
//
// The auth limiters run before the CSRF check and key on the `email` field of
// the body, so their keys used to be as large as the body parser allowed
// (1 MB) and were kept for the whole window. These tests pin the three things
// that close that: the shape of every key, the order of the buckets, and how
// much memory a flood of oversized requests leaves behind. They import only
// names the limiter modules have always exported, so the same file can be run
// against an older copy of those modules.

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }),
    connect: vi.fn(),
    on: vi.fn(),
  },
}));

import { authHybridKeyGen, makeAuthRateLimit, makeSignupRateLimit } from '../../src/middleware/auth-rate-limit';
import { companionTurnKeyGen, companionIpKeyGen, rateLimitIpKey } from '../../src/middleware/companion-rate-limit';
import { customerLlmKey } from '../../src/middleware/llm-rate-limit';

/** The longest key any limiter may build: an address, a separator and a hashed email. */
const MAX_KEY_CHARS = 128;
const HUGE = 'a'.repeat(900_000);
const asRequest = (fields: Record<string, unknown>): express.Request => fields as unknown as express.Request;

describe('rate-limit keys: no key holds text of a length the client chooses', () => {
  it('an address that is not an IP address gets one fixed key', () => {
    expect(rateLimitIpKey('not an address')).toBe('ip:invalid');
    expect(rateLimitIpKey(HUGE)).toBe('ip:invalid');
    expect(rateLimitIpKey(`${'1'.repeat(5000)}.2.3.4`)).toBe('ip:invalid');
    expect(rateLimitIpKey(`2001:db8:${'f'.repeat(5000)}::1`)).toBe('ip:invalid');
    expect(rateLimitIpKey('203.0.113.9, 198.51.100.1')).toBe('ip:invalid');
    expect(companionIpKeyGen(asRequest({ ip: HUGE }))).toBe('ip:invalid');
  });

  it('real addresses keep the keys they had', () => {
    expect(rateLimitIpKey('203.0.113.9')).toBe('ip:203.0.113.9');
    expect(rateLimitIpKey('::ffff:203.0.113.9')).toBe('ip:203.0.113.9');
    expect(rateLimitIpKey('2001:db8:abcd:12:aaaa:bbbb:cccc:dddd')).toBe('ip6:2001:db8:abcd:12');
    expect(rateLimitIpKey('fe80::1%lo0')).toBe('ip6:fe80:0:0:0');
  });

  it('the auth pair key is bounded whatever the email field holds', () => {
    const key = authHybridKeyGen(asRequest({ ip: '203.0.113.9', body: { email: `${HUGE}@example.com` } }));
    expect(key.length).toBeLessThanOrEqual(MAX_KEY_CHARS);
    expect(key.startsWith('ip:203.0.113.9|')).toBe(true);
    // Two different long emails still get two different keys.
    const other = authHybridKeyGen(asRequest({ ip: '203.0.113.9', body: { email: `${HUGE}b@example.com` } }));
    expect(other).not.toBe(key);
    expect(other.length).toBeLessThanOrEqual(MAX_KEY_CHARS);
  });

  it('casing and padding of a long email do not make a second key', () => {
    const long = `${'x'.repeat(200)}@example.com`;
    const a = authHybridKeyGen(asRequest({ ip: '203.0.113.9', body: { email: long } }));
    const b = authHybridKeyGen(asRequest({ ip: '203.0.113.9', body: { email: `  ${long.toUpperCase()}  ` } }));
    expect(b).toBe(a);
    expect(a.length).toBeLessThanOrEqual(MAX_KEY_CHARS);
  });

  it('an ordinary email is still readable in its key', () => {
    expect(authHybridKeyGen(asRequest({ ip: '203.0.113.9', body: { email: ' Alice@Example.com ' } }))).toBe(
      'ip:203.0.113.9|alice@example.com'
    );
  });

  it('the trial and customer keys are bounded too', () => {
    expect(companionTurnKeyGen(asRequest({ ip: '203.0.113.9', harborTrial: { trial_id: HUGE } })).length).toBeLessThanOrEqual(
      MAX_KEY_CHARS
    );
    expect(customerLlmKey(asRequest({ ip: '203.0.113.9', session: { customerId: HUGE } })).length).toBeLessThanOrEqual(
      MAX_KEY_CHARS
    );
    expect(customerLlmKey(asRequest({ ip: HUGE, session: {} }))).toBe('ip:invalid');
  });
});

describe('auth rate limit: the address bucket is checked first', () => {
  // `trust proxy` lets each request claim a client address, which is how the
  // tests stand in for different hosts.
  function appWithLimiter(limiter: express.RequestHandler): express.Express {
    const app = express();
    app.set('trust proxy', true);
    app.use(express.json({ limit: '1mb' }));
    app.post('/login', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }
  const post = (app: express.Express, ip: string, email?: string) =>
    request(app).post('/login').set('X-Forwarded-For', ip).send(email === undefined ? {} : { email });

  it('a request the address bucket refuses is not counted against the email it names', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 5, emailMax: 1, ipMax: 2, windowMs: 60_000, skipInTest: false })
    );
    expect((await post(app, '198.51.100.1', 'first@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.1', 'second@example.com')).status).toBe(200);
    // The address has used its two requests. This one names a third account
    // and is refused by the address bucket.
    expect((await post(app, '198.51.100.1', 'third@example.com')).status).toBe(429);
    // Had the refused request been counted in the email bucket (cap 1), the
    // account's real owner, on another address, would now be locked out.
    expect((await post(app, '198.51.100.2', 'third@example.com')).status).toBe(200);
  });

  it('a request that passes carries the address bucket figures in its headers', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 5, emailMax: 7, ipMax: 9, windowMs: 60_000, skipInTest: false })
    );
    const res = await post(app, '198.51.100.3', 'someone@example.com');
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-limit']).toBe('9');
    expect(res.headers['ratelimit-remaining']).toBe('8');
  });

  it('refuses an email longer than 254 characters with a 400, and still counts the request per address', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 5, emailMax: 5, ipMax: 2, windowMs: 60_000, skipInTest: false })
    );
    const tooLong = `${'a'.repeat(243)}@example.com`;
    expect(tooLong.length).toBe(255);
    const first = await post(app, '198.51.100.4', tooLong);
    expect(first.status).toBe(400);
    expect(first.text).toMatch(/too long/i);
    expect((await post(app, '198.51.100.4', `${tooLong}x`)).status).toBe(400);
    expect((await post(app, '198.51.100.4', tooLong)).status).toBe(429);
  });

  it('accepts an email of exactly 254 characters', async () => {
    const app = appWithLimiter(makeSignupRateLimit({ max: 5, windowMs: 60_000, skipInTest: false }));
    const longest = `${'a'.repeat(242)}@example.com`;
    expect(longest.length).toBe(254);
    expect((await post(app, '198.51.100.5', longest)).status).toBe(200);
  });
});

describe('auth rate limit: what a flood of oversized logins leaves in memory', () => {
  let collect: () => void;
  beforeAll(() => {
    // Node exposes its collector to code that asks for it at run time.
    v8.setFlagsFromString('--expose-gc');
    collect = vm.runInNewContext('gc') as () => void;
  });
  const heapMb = (): number => {
    collect();
    collect();
    return process.memoryUsage().heapUsed / 1024 / 1024;
  };

  it('30 logins with a unique 900 KB email each, from 30 addresses, retain next to nothing', async () => {
    // The parser here allows 1 MB on purpose: this is the limiter on its own,
    // as it stood behind the app-wide limit.
    const app = express();
    app.set('trust proxy', true);
    app.use(express.urlencoded({ extended: true, limit: '1mb' }));
    app.post('/login', makeAuthRateLimit({ skipInTest: false }), (_req, res) => {
      res.json({ ok: true });
    });

    const before = heapMb();
    const statuses: number[] = [];
    for (let i = 0; i < 30; i++) {
      const res = await request(app)
        .post('/login')
        .set('X-Forwarded-For', `198.51.100.${i + 1}`)
        .type('form')
        .send(`email=${HUGE}${i}%40example.com&password=x`);
      statuses.push(res.status);
    }
    const grown = heapMb() - before;

    // Each of these used to pass the limiter (status 200) and leave its two
    // email-bearing keys behind, megabytes for the thirty. Now every one is
    // refused before a key holding the email exists.
    expect(grown, `heap grew by ${grown.toFixed(1)} MB`).toBeLessThan(15);
    expect(statuses.every((status) => status === 400)).toBe(true);
  });
});

describe('the account forms have a small body limit of their own', () => {
  let app: express.Express;
  beforeAll(async () => {
    app = (await import('../../src/app')).default as unknown as express.Express;
  });

  it('answers 413 to a 900 KB login, signup, reset or resend body before any route sees it', async () => {
    for (const path of ['/login', '/signup', '/forgot-password', '/reset-password', '/resend-verification', '/logout', '/account/delete']) {
      const form = await request(app).post(path).type('form').send(`email=${HUGE}%40example.com&password=x`);
      expect(form.status, `${path} form`).toBe(413);
      const json = await request(app).post(path).send({ email: `${HUGE}@example.com` });
      expect(json.status, `${path} json`).toBe(413);
    }
  });

  it('still reads an ordinary login form (the refusal is then the missing CSRF token)', async () => {
    const res = await request(app).post('/login').type('form').send('email=user%40example.com&password=correct-horse');
    expect(res.status).toBe(403);
  });

  it('leaves the general 1 MB limit in place for the other routes', async () => {
    // The contact form is public and JSON-capable; a 900 KB body is parsed
    // and the route answers for itself (not 413).
    const res = await request(app).post('/contact').send({ message: HUGE });
    expect(res.status).not.toBe(413);
    const tooBig = await request(app).post('/contact').send({ message: 'a'.repeat(1_200_000) });
    expect(tooBig.status).toBe(413);
  });
});
