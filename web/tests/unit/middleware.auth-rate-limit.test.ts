import { describe, it, expect } from 'vitest';
import express from 'express';
import request from 'supertest';
import {
  ACCOUNT_DELETE_DEFAULT_MAX,
  accountDeleteRateLimit,
  authHybridKeyGen,
  makeAccountDeleteRateLimit,
  makeAuthRateLimit,
  makeSignupRateLimit,
} from '../../src/middleware/auth-rate-limit';

/**
 * Tests for the hybrid (IP + email) auth rate limiter
 * (auth-rate-limit-hybrid-keyer, 2026-05-09).
 *
 * The production singletons skip under NODE_ENV=test so the broader
 * auth route tests aren't throttled. To exercise the cap we use the
 * factory with `skipInTest: false`.
 */

describe('authHybridKeyGen', () => {
  it('combines IP and lowercased email when both present', () => {
    const req = {
      ip: '1.2.3.4',
      body: { email: 'Alice@Example.com' },
    } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:1.2.3.4|alice@example.com');
  });

  it('trims surrounding whitespace from email before keying', () => {
    const req = {
      ip: '1.2.3.4',
      body: { email: '   alice@example.com  ' },
    } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:1.2.3.4|alice@example.com');
  });

  it('falls back to IP-only key when email is missing', () => {
    const req = {
      ip: '1.2.3.4',
      body: {},
    } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:1.2.3.4');
  });

  it('falls back to IP-only key when body is missing entirely', () => {
    const req = { ip: '1.2.3.4' } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:1.2.3.4');
  });

  it('falls back to IP-only when email field is a non-string', () => {
    const req = {
      ip: '1.2.3.4',
      body: { email: 12345 },
    } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:1.2.3.4');
  });

  it('treats empty-string email as missing and keys on IP', () => {
    const req = {
      ip: '1.2.3.4',
      body: { email: '   ' },
    } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:1.2.3.4');
  });

  it('uses "unknown" placeholder when neither IP nor email is present', () => {
    const req = { body: {} } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip:unknown');
  });
});

describe('auth rate limit middleware (factory)', () => {
  function appWithLimiter(
    limiter: express.RequestHandler
  ): express.Express {
    const app = express();
    app.use(express.json());
    app.use(express.urlencoded({ extended: true }));
    app.post('/login', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }

  it('allows requests under the cap and emits rate-limit headers', async () => {
    const limiter = makeAuthRateLimit({
      max: 5,
      windowMs: 60_000,
      skipInTest: false,
    });
    const app = appWithLimiter(limiter);
    const res = await request(app)
      .post('/login')
      .send({ email: 'alice@example.com' });
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-limit']).toBeTruthy();
    expect(res.headers['ratelimit-remaining']).toBeTruthy();
  });

  it('returns 429 after exceeding the cap for the same IP+email pair', async () => {
    const limiter = makeAuthRateLimit({
      max: 2,
      windowMs: 60_000,
      skipInTest: false,
    });
    const app = appWithLimiter(limiter);
    const body = { email: 'victim@example.com' };
    expect((await request(app).post('/login').send(body)).status).toBe(200);
    expect((await request(app).post('/login').send(body)).status).toBe(200);
    const r3 = await request(app).post('/login').send(body);
    expect(r3.status).toBe(429);
  });

  it('keys per-email so a shared-IP user with a different email is NOT locked out', async () => {
    const limiter = makeAuthRateLimit({
      max: 1,
      windowMs: 60_000,
      skipInTest: false,
    });
    const app = appWithLimiter(limiter);
    // Attacker exhausts budget against victim's email from a shared IP.
    expect(
      (
        await request(app).post('/login').send({ email: 'victim@example.com' })
      ).status
    ).toBe(200);
    expect(
      (
        await request(app).post('/login').send({ email: 'victim@example.com' })
      ).status
    ).toBe(429);
    // Legit user behind the same egress IP, different email, still allowed.
    expect(
      (
        await request(app).post('/login').send({ email: 'legit@example.com' })
      ).status
    ).toBe(200);
  });

  it('caps per-email regardless of email casing / surrounding whitespace', async () => {
    const limiter = makeAuthRateLimit({
      max: 1,
      windowMs: 60_000,
      skipInTest: false,
    });
    const app = appWithLimiter(limiter);
    expect(
      (
        await request(app).post('/login').send({ email: 'victim@example.com' })
      ).status
    ).toBe(200);
    // Same email, attacker permutes casing + whitespace; should land in
    // the same bucket and 429.
    const r2 = await request(app)
      .post('/login')
      .send({ email: '  Victim@Example.COM  ' });
    expect(r2.status).toBe(429);
  });

  it('signup factory uses a longer window and lower cap by default', async () => {
    const limiter = makeSignupRateLimit({ skipInTest: false, max: 1 });
    const app = appWithLimiter(limiter);
    expect(
      (
        await request(app).post('/login').send({ email: 'new@example.com' })
      ).status
    ).toBe(200);
    expect(
      (
        await request(app).post('/login').send({ email: 'new@example.com' })
      ).status
    ).toBe(429);
  });

  it('production singletons skip rate-limiting under NODE_ENV=test', async () => {
    // The exported `authRateLimit` should not throttle test-suite calls.
    const { authRateLimit } = await import('../../src/middleware/auth-rate-limit');
    const app = appWithLimiter(authRateLimit);
    for (let i = 0; i < 25; i++) {
      const r = await request(app)
        .post('/login')
        .send({ email: 'flood@example.com' });
      expect(r.status).toBe(200);
    }
  });
});

// ---------------------------------------------------------------------------
// Three-bucket limiter and IPv6 keying (public-release security review)
// ---------------------------------------------------------------------------

describe('authHybridKeyGen with IPv6 clients', () => {
  it('uses the /64 in the pair key', () => {
    const req = {
      ip: '2001:db8:1234:5678:aaaa:bbbb:cccc:dddd',
      body: { email: 'alice@example.com' },
    } as unknown as express.Request;
    expect(authHybridKeyGen(req)).toBe('ip6:2001:db8:1234:5678|alice@example.com');
    // A different interface id in the same /64 lands in the same bucket.
    const sibling = {
      ip: '2001:db8:1234:5678::1',
      body: { email: 'alice@example.com' },
    } as unknown as express.Request;
    expect(authHybridKeyGen(sibling)).toBe(authHybridKeyGen(req));
  });
});

describe('auth rate limit: buckets beyond the IP + email pair', () => {
  // `trust proxy` lets each request claim a client address, which is how the
  // tests stand in for many different hosts.
  function appWithLimiter(limiter: express.RequestHandler): express.Express {
    const app = express();
    app.set('trust proxy', true);
    app.use(express.json());
    app.post('/login', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }
  const post = (app: express.Express, ip: string, email?: string) =>
    request(app).post('/login').set('X-Forwarded-For', ip).send(email ? { email } : {});

  it('caps guesses against ONE email across MANY addresses (rotating botnet)', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 2, emailMax: 3, ipMax: 100, windowMs: 60_000, skipInTest: false })
    );
    // Every request comes from a fresh address, so the pair bucket never fills.
    expect((await post(app, '198.51.100.1', 'victim@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.2', 'victim@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.3', 'victim@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.4', 'victim@example.com')).status).toBe(429);
    // Another account is unaffected.
    expect((await post(app, '198.51.100.4', 'other@example.com')).status).toBe(200);
  });

  it('caps ONE address spraying MANY emails (password spraying, bulk signup)', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 2, emailMax: 100, ipMax: 3, windowMs: 60_000, skipInTest: false })
    );
    expect((await post(app, '198.51.100.9', 'a@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.9', 'b@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.9', 'c@example.com')).status).toBe(200);
    expect((await post(app, '198.51.100.9', 'd@example.com')).status).toBe(429);
    // A different address still gets through.
    expect((await post(app, '198.51.100.10', 'd@example.com')).status).toBe(200);
  });

  it('an IPv6 client cannot dodge the address bucket by rotating inside its /64', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 1, emailMax: 100, ipMax: 2, windowMs: 60_000, skipInTest: false })
    );
    expect((await post(app, '2001:db8:1:2::1', 'a@example.com')).status).toBe(200);
    expect((await post(app, '2001:db8:1:2::2', 'b@example.com')).status).toBe(200);
    expect((await post(app, '2001:db8:1:2:ffff::3', 'c@example.com')).status).toBe(429);
  });

  it('requests without an email are still capped per address', async () => {
    const app = appWithLimiter(
      makeAuthRateLimit({ max: 2, windowMs: 60_000, skipInTest: false })
    );
    expect((await post(app, '198.51.100.20')).status).toBe(200);
    expect((await post(app, '198.51.100.20')).status).toBe(200);
    expect((await post(app, '198.51.100.20')).status).toBe(429);
  });

  it('defaults the wider buckets to multiples of the pair cap', async () => {
    const app = appWithLimiter(makeAuthRateLimit({ max: 1, windowMs: 60_000, skipInTest: false }));
    // emailMax defaults to 3: the fourth distinct address is refused.
    for (const ip of ['198.51.100.31', '198.51.100.32', '198.51.100.33']) {
      expect((await post(app, ip, 'victim@example.com')).status).toBe(200);
    }
    expect((await post(app, '198.51.100.34', 'victim@example.com')).status).toBe(429);
  });
});

describe('the account-deletion limiter (POST /account/delete checks a password)', () => {
  // The route sits behind requireAuth, so the session names the customer. The
  // tests set it from a header; `trust proxy` lets each request claim an address.
  function appWithLimiter(limiter: express.RequestHandler): express.Express {
    const app = express();
    app.set('trust proxy', true);
    app.use(express.json());
    app.use((req, _res, next) => {
      (req as unknown as { session: { customerId?: string } }).session = { customerId: String(req.headers['x-customer'] || '') || undefined };
      next();
    });
    app.post('/account/delete', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }
  const post = (app: express.Express, ip: string, customer: string) =>
    request(app).post('/account/delete').set('X-Forwarded-For', ip).set('x-customer', customer).send({ password: 'x' });

  it('caps the guesses against one account across many addresses, and leaves another account alone', async () => {
    const app = appWithLimiter(makeAccountDeleteRateLimit({ max: 3, ipMax: 100, windowMs: 60_000, skipInTest: false }));
    // Every request comes from a fresh address, so the address bucket never fills.
    for (const ip of ['198.51.100.1', '198.51.100.2', '198.51.100.3']) {
      expect((await post(app, ip, 'customer-a')).status).toBe(200);
    }
    const refused = await post(app, '198.51.100.4', 'customer-a');
    expect(refused.status).toBe(429);
    expect(refused.text).toMatch(/Too many attempts/);
    expect((await post(app, '198.51.100.4', 'customer-b')).status).toBe(200);
  });

  it('caps one address across many accounts', async () => {
    const app = appWithLimiter(makeAccountDeleteRateLimit({ max: 1, ipMax: 2, windowMs: 60_000, skipInTest: false }));
    expect((await post(app, '198.51.100.10', 'customer-a')).status).toBe(200);
    expect((await post(app, '198.51.100.10', 'customer-b')).status).toBe(200);
    expect((await post(app, '198.51.100.10', 'customer-c')).status).toBe(429);
  });

  it('an IPv6 client cannot dodge the address bucket by rotating inside its /64', async () => {
    const app = appWithLimiter(makeAccountDeleteRateLimit({ max: 1, ipMax: 2, windowMs: 60_000, skipInTest: false }));
    expect((await post(app, '2001:db8:1:2::1', 'customer-a')).status).toBe(200);
    expect((await post(app, '2001:db8:1:2::2', 'customer-b')).status).toBe(200);
    expect((await post(app, '2001:db8:1:2:ffff::3', 'customer-c')).status).toBe(429);
  });

  it('allows five attempts per account by default, like a login', async () => {
    expect(ACCOUNT_DELETE_DEFAULT_MAX).toBe(5);
    const app = appWithLimiter(makeAccountDeleteRateLimit({ windowMs: 60_000, skipInTest: false }));
    for (let i = 1; i <= 5; i += 1) expect((await post(app, `198.51.100.${40 + i}`, 'customer-a')).status).toBe(200);
    expect((await post(app, '198.51.100.50', 'customer-a')).status).toBe(429);
  });

  it('the production singleton skips under NODE_ENV=test, like the other auth limiters', async () => {
    const app = appWithLimiter(accountDeleteRateLimit);
    for (let i = 1; i <= ACCOUNT_DELETE_DEFAULT_MAX + 3; i += 1) expect((await post(app, '198.51.100.60', 'customer-a')).status).toBe(200);
  });
});
