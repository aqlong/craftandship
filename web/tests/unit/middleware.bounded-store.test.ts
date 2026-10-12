import { describe, it, expect, vi, afterEach } from 'vitest';
import express from 'express';
import request from 'supertest';
import v8 from 'v8';
import vm from 'vm';
import rateLimit, { MemoryStore, type Options } from 'express-rate-limit';
import {
  BoundedRateLimitStore,
  MAX_RATE_LIMIT_KEY_CHARS,
  RATE_LIMIT_MAX_KEYS,
  boundedRateLimit,
  customerOrAddressKey,
  makeCompanionTurnRateLimit,
} from '../../src/middleware/companion-rate-limit';
import { emailKeyPart, MAX_EMAIL_KEY_CHARS, makeAuthRateLimit } from '../../src/middleware/auth-rate-limit';
import { makeAnonDailyTurnLimit } from '../../src/middleware/anon-rate-limit';
import { makeCustomerLlmDailyLimit } from '../../src/middleware/llm-rate-limit';

// The store every limiter in src/middleware counts in: a fixed-window counter
// with a ceiling on the number of keys.

const WINDOW_MS = 60_000;
const storeWith = (maxKeys?: number): BoundedRateLimitStore => {
  const store = new BoundedRateLimitStore(maxKeys);
  store.init({ windowMs: WINDOW_MS } as Options);
  return store;
};

afterEach(() => {
  vi.useRealTimers();
});

describe('BoundedRateLimitStore: counting', () => {
  it('counts hits per key inside one window and reports when the window ends', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const store = storeWith();
    expect((await store.increment('a')).totalHits).toBe(1);
    expect((await store.increment('a')).totalHits).toBe(2);
    const third = await store.increment('a');
    expect(third.totalHits).toBe(3);
    expect(third.resetTime?.getTime()).toBe(1_000_000 + WINDOW_MS);
    expect((await store.increment('b')).totalHits).toBe(1);
    expect((await store.get('a'))?.totalHits).toBe(3);
  });

  it('starts a new window once the old one has ended', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const store = storeWith();
    await store.increment('a');
    await store.increment('a');
    vi.setSystemTime(1_000_000 + WINDOW_MS);
    expect(await store.get('a')).toBeUndefined();
    const fresh = await store.increment('a');
    expect(fresh.totalHits).toBe(1);
    expect(fresh.resetTime?.getTime()).toBe(1_000_000 + 2 * WINDOW_MS);
  });

  it('decrement, resetKey and resetAll do what express-rate-limit expects of a store', async () => {
    const store = storeWith();
    await store.increment('a');
    await store.increment('a');
    await store.decrement('a');
    expect((await store.get('a'))?.totalHits).toBe(1);
    await store.decrement('a');
    await store.decrement('a');
    expect((await store.get('a'))?.totalHits).toBe(0);
    await store.resetKey('a');
    expect(await store.get('a')).toBeUndefined();
    await store.increment('b');
    await store.resetAll();
    expect(store.size).toBe(0);
  });
});

describe('BoundedRateLimitStore: the ceiling', () => {
  it('never holds more keys than its ceiling, and drops the oldest window first', async () => {
    const store = storeWith(100);
    for (let i = 0; i < 1000; i++) {
      await store.increment(`key-${i}`);
      expect(store.size).toBeLessThanOrEqual(100);
    }
    expect(store.size).toBe(100);
    expect(await store.get('key-0')).toBeUndefined();
    expect(await store.get('key-899')).toBeUndefined();
    expect((await store.get('key-900'))?.totalHits).toBe(1);
    expect((await store.get('key-999'))?.totalHits).toBe(1);
  });

  it('a key that is still under the ceiling keeps its count while others come and go', async () => {
    const store = storeWith(100);
    await store.increment('regular');
    await store.increment('regular');
    for (let i = 0; i < 50; i++) await store.increment(`passer-by-${i}`);
    expect((await store.increment('regular')).totalHits).toBe(3);
  });

  it('forgetting a key only ever loosens the limit for that key', async () => {
    const store = storeWith(2);
    await store.increment('a');
    await store.increment('a');
    await store.increment('b');
    await store.increment('c'); // 'a' had the oldest window and is dropped
    expect(store.size).toBe(2);
    expect((await store.increment('a')).totalHits).toBe(1);
  });

  it('sweeps expired keys as new ones arrive, without waiting for the ceiling', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const store = storeWith(10_000);
    for (let i = 0; i < 500; i++) await store.increment(`old-${i}`);
    expect(store.size).toBe(500);
    vi.setSystemTime(1_000_000 + WINDOW_MS + 1);
    for (let i = 0; i < 20; i++) await store.increment(`new-${i}`);
    // 20 new keys, and every expired one swept (a bounded number per insert).
    expect(store.size).toBe(20);
    expect(await store.get('old-499')).toBeUndefined();
  });

  it('ten times its ceiling in distinct keys leaves the default store at its ceiling', async () => {
    const store = storeWith();
    for (let i = 0; i < RATE_LIMIT_MAX_KEYS * 10; i++) await store.increment(`ip:${i.toString(36)}`);
    expect(store.size).toBe(RATE_LIMIT_MAX_KEYS);
  });

  it("the library's own memory store, for comparison, keeps every key", async () => {
    const store = new MemoryStore();
    store.init({ windowMs: WINDOW_MS } as Options);
    for (let i = 0; i < 100_000; i++) await store.increment(`ip:${i.toString(36)}`);
    const held = (store as unknown as { current: Map<string, unknown> }).current.size;
    store.shutdown();
    expect(held).toBe(100_000);
  });
});

describe('BoundedRateLimitStore behind express-rate-limit', () => {
  it('refuses the request after the cap and admits again in the next window', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(5_000_000);
    const app = express();
    app.get(
      '/x',
      rateLimit({ windowMs: WINDOW_MS, max: 2, keyGenerator: () => 'one-client', store: new BoundedRateLimitStore() }),
      (_req, res) => {
        res.json({ ok: true });
      }
    );
    expect((await request(app).get('/x')).status).toBe(200);
    expect((await request(app).get('/x')).status).toBe(200);
    const refused = await request(app).get('/x');
    expect(refused.status).toBe(429);
    expect(Number(refused.headers['retry-after'])).toBeGreaterThan(0);
    vi.setSystemTime(5_000_000 + WINDOW_MS + 1);
    expect((await request(app).get('/x')).status).toBe(200);
  });
});

describe('every limiter built in src/middleware counts in a bounded store', () => {
  // express-rate-limit keeps the store it was given private. What can be seen
  // from outside is memory: the library's own store keeps every key (about
  // 250 bytes each), so these limiters are fed five times their ceiling in
  // distinct clients and must not grow with them.
  const CLIENTS = RATE_LIMIT_MAX_KEYS * 5;

  it(`${CLIENTS} distinct clients against each kind of limiter do not accumulate`, async () => {
    v8.setFlagsFromString('--expose-gc');
    const collect = vm.runInNewContext('gc') as () => void;
    const heapMb = (): number => {
      collect();
      collect();
      return process.memoryUsage().heapUsed / 1024 / 1024;
    };
    // [name, limiter, most it may retain in MB]. A full store is about 6 MB;
    // the auth limiter has three. Unbounded, 100,000 keys are about 23 MB a
    // bucket (75 MB for the auth limiter), which is what these limiters
    // retained when they counted in the library's store.
    const limiters: Array<[string, express.RequestHandler, number]> = [
      ['companion', makeCompanionTurnRateLimit({ max: 5, windowMs: WINDOW_MS }), 12],
      ['anon', makeAnonDailyTurnLimit({ max: 5, shared: false, windowMs: WINDOW_MS }), 12],
      ['customer llm', makeCustomerLlmDailyLimit({ max: 5, windowMs: WINDOW_MS }), 12],
      ['auth', makeAuthRateLimit({ max: 5, windowMs: WINDOW_MS, skipInTest: false }), 30],
    ];
    for (const [name, limiter, allowedMb] of limiters) {
      let passed = 0;
      const next = (): void => {
        passed += 1;
      };
      const before = heapMb();
      for (let i = 0; i < CLIENTS; i++) {
        const a = (i >> 16) & 255;
        const b = (i >> 8) & 255;
        const c = i & 255;
        const req = {
          ip: `10.${a}.${b}.${c}`,
          headers: {},
          body: { email: `user${i}@example.com` },
          session: {},
          app: { get: () => false },
        } as unknown as express.Request;
        const res = {
          headersSent: false,
          setHeader: () => undefined,
          getHeader: () => undefined,
        } as unknown as express.Response;
        await new Promise<void>((resolve) => {
          limiter(req, res, () => {
            next();
            resolve();
          });
        });
      }
      const grownMb = heapMb() - before;
      expect(passed, name).toBe(CLIENTS);
      expect(grownMb, `${name}: heap grew by ${grownMb.toFixed(1)} MB`).toBeLessThan(allowedMb);
    }
  }, 120_000);
});

describe('boundedRateLimit: the one factory every limiter is built by', () => {
  // A mini app whose limiter key is whatever the test puts in a header, with
  // `trust proxy` on so a request can also claim an address.
  function appWith(limiter: express.RequestHandler): express.Express {
    const app = express();
    app.set('trust proxy', true);
    app.get('/x', limiter, (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }
  const headerKey = (req: express.Request): string => String(req.headers['x-key'] ?? '');

  it('ignores a store the caller passes and counts in its own bounded one', async () => {
    const callers = new MemoryStore();
    const limiter = boundedRateLimit({
      windowMs: WINDOW_MS,
      max: 1,
      keyGenerator: headerKey,
      store: callers,
    } as Parameters<typeof boundedRateLimit>[0]);
    const app = appWith(limiter);
    expect((await request(app).get('/x').set('X-Key', 'a')).status).toBe(200);
    expect((await request(app).get('/x').set('X-Key', 'a')).status).toBe(429);
    const held = (callers as unknown as { current: Map<string, unknown> }).current.size;
    callers.shutdown();
    expect(held).toBe(0);
  });

  it('a key of any length still tells clients apart, and is not what gets stored', async () => {
    const app = appWith(boundedRateLimit({ windowMs: WINDOW_MS, max: 1, keyGenerator: headerKey }));
    const long = 'k'.repeat(6000);
    expect(long.length).toBeGreaterThan(MAX_RATE_LIMIT_KEY_CHARS);
    expect((await request(app).get('/x').set('X-Key', `${long}1`)).status).toBe(200);
    expect((await request(app).get('/x').set('X-Key', `${long}1`)).status).toBe(429);
    expect((await request(app).get('/x').set('X-Key', `${long}2`)).status).toBe(200);
  });

  it('2,000 distinct 100 KB keys leave next to nothing behind', async () => {
    v8.setFlagsFromString('--expose-gc');
    const collect = vm.runInNewContext('gc') as () => void;
    const heapMb = (): number => {
      collect();
      collect();
      return process.memoryUsage().heapUsed / 1024 / 1024;
    };
    const limiter = boundedRateLimit({
      windowMs: WINDOW_MS,
      max: 5,
      keyGenerator: (req) => (req as unknown as { hugeKey: string }).hugeKey,
    });
    const filler = 'x'.repeat(100_000);
    const before = heapMb();
    for (let i = 0; i < 2000; i++) {
      const req = { ip: '10.0.0.1', headers: {}, app: { get: () => false }, hugeKey: `${filler}${i}` } as unknown as express.Request;
      const res = { headersSent: false, setHeader: () => undefined, getHeader: () => undefined } as unknown as express.Response;
      await new Promise<void>((resolve) => limiter(req, res, () => resolve()));
    }
    const grownMb = heapMb() - before;
    // Stored as given, these keys alone are 200 MB.
    expect(grownMb, `heap grew by ${grownMb.toFixed(1)} MB`).toBeLessThan(20);
  });

  it('a limiter given no key generator keys on the validated address, not on raw req.ip', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const app = appWith(boundedRateLimit({ windowMs: WINDOW_MS, max: 2 }));
    // Three different strings that are not addresses share one bucket.
    expect((await request(app).get('/x').set('X-Forwarded-For', 'not-an-address-1')).status).toBe(200);
    expect((await request(app).get('/x').set('X-Forwarded-For', 'not-an-address-2')).status).toBe(200);
    expect((await request(app).get('/x').set('X-Forwarded-For', 'not-an-address-3')).status).toBe(429);
    // A real address has its own.
    expect((await request(app).get('/x').set('X-Forwarded-For', '203.0.113.7')).status).toBe(200);
    vi.restoreAllMocks();
  });
});

describe('customerOrAddressKey: the key of the limiters on logged-in routes', () => {
  const asRequest = (fields: Record<string, unknown>): express.Request => fields as unknown as express.Request;

  it('is the customer when there is a session, whatever the address', () => {
    expect(customerOrAddressKey(asRequest({ ip: '203.0.113.9', session: { customerId: 'cust-1' } }))).toBe('customer:cust-1');
    expect(customerOrAddressKey(asRequest({ ip: 'x'.repeat(9000), session: { customerId: 'cust-1' } }))).toBe('customer:cust-1');
    const huge = customerOrAddressKey(asRequest({ ip: '203.0.113.9', session: { customerId: 'c'.repeat(9000) } }));
    expect(huge.length).toBeLessThanOrEqual(MAX_RATE_LIMIT_KEY_CHARS);
  });

  it('is the validated address when there is none', () => {
    expect(customerOrAddressKey(asRequest({ ip: '203.0.113.9', session: {} }))).toBe('ip:203.0.113.9');
    expect(customerOrAddressKey(asRequest({ ip: '2001:db8:1:2:aaaa::1' }))).toBe('ip6:2001:db8:1:2');
    expect(customerOrAddressKey(asRequest({ ip: 'x'.repeat(9000), session: {} }))).toBe('ip:invalid');
    expect(customerOrAddressKey(asRequest({}))).toBe('ip:unknown');
  });

  // The same limiter shape the chat routes use, behind a stand-in for the
  // session: a header says who is logged in.
  function chatApp(): express.Express {
    const app = express();
    app.set('trust proxy', true);
    app.use((req, _res, next) => {
      const who = req.headers['x-customer'];
      (req as unknown as { session: { customerId?: string } }).session = typeof who === 'string' ? { customerId: who } : {};
      next();
    });
    app.post('/message', boundedRateLimit({ windowMs: WINDOW_MS, max: 2, keyGenerator: customerOrAddressKey }), (_req, res) => {
      res.json({ ok: true });
    });
    return app;
  }
  const send = (app: express.Express, ip: string, customer?: string) => {
    const req = request(app).post('/message').set('X-Forwarded-For', ip);
    return customer ? req.set('X-Customer', customer) : req;
  };

  it('logged in: one customer has one budget on every address, and two customers on one address have two', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const app = chatApp();
    expect((await send(app, '198.51.100.1', 'alice')).status).toBe(200);
    expect((await send(app, '198.51.100.2', 'alice')).status).toBe(200);
    expect((await send(app, '198.51.100.3', 'alice')).status).toBe(429);
    expect((await send(app, '198.51.100.1', 'bob')).status).toBe(200);
    vi.restoreAllMocks();
  });

  it('anonymous: the budget is per address, and text that is not an address is one shared budget', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const app = chatApp();
    expect((await send(app, '198.51.100.9')).status).toBe(200);
    expect((await send(app, '198.51.100.9')).status).toBe(200);
    expect((await send(app, '198.51.100.9')).status).toBe(429);
    expect((await send(app, '198.51.100.10')).status).toBe(200);
    expect((await send(app, 'forged-a')).status).toBe(200);
    expect((await send(app, 'forged-b')).status).toBe(200);
    expect((await send(app, 'forged-c')).status).toBe(429);
    vi.restoreAllMocks();
  });
});

describe('emailKeyPart', () => {
  it('keeps a short email as typed (trimmed, lower-cased) and hashes a long one', () => {
    expect(emailKeyPart('  Alice@Example.COM ')).toBe('alice@example.com');
    const atLimit = `${'a'.repeat(MAX_EMAIL_KEY_CHARS - 12)}@example.com`;
    expect(atLimit.length).toBe(MAX_EMAIL_KEY_CHARS);
    expect(emailKeyPart(atLimit)).toBe(atLimit);
    const over = `a${atLimit}`;
    expect(emailKeyPart(over)).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(emailKeyPart(over.toUpperCase())).toBe(emailKeyPart(over));
    expect(emailKeyPart(`b${atLimit}`)).not.toBe(emailKeyPart(over));
    expect(emailKeyPart('   ')).toBe('');
  });
});
