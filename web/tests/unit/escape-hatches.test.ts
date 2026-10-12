// Escape hatches: every flag, query parameter, or environment mode that
// relaxes a control, proven inert where it must be.
//
// Two kinds are covered here:
//
//   1. Development conveniences (payment bypasses, the admin ?dev_trial= and
//      lamp overrides). They must do nothing unless NODE_ENV is explicitly
//      "development" or "test": not in production, not when NODE_ENV is unset,
//      not under a custom value such as "staging".
//   2. NODE_ENV=test relaxations (limiters raised or skipped so the suite can
//      hammer one address). Under NODE_ENV=production every one of them must
//      be back at its real value.
//
// Most modules read NODE_ENV once, at import. Each case therefore sets the
// environment, resets the module registry, and imports a fresh copy.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import express from 'express';
import type { Request, RequestHandler } from 'express';
import request from 'supertest';
import bcrypt from 'bcrypt';
import { readFileSync } from 'fs';
import { join } from 'path';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn(() => 'token'),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));

const TOUCHED_ENV = [
  'NODE_ENV', 'SESSION_SECRET', 'ADMIN_EMAIL', 'STRIPE_BYPASS', 'CAS_HARBOR_STRIPE_BYPASS',
  'CAS_HARBOR_ENABLED', 'CAS_ALLOW_RAILWAY_WRITES', 'CAS_HARBOR_TURN_MAX', 'CAS_HARBOR_TURN_IP_MAX',
  'CAS_HARBOR_TURN_WINDOW_MS', 'CAS_HARBOR_BUILD_MAX', 'CAS_HARBOR_TRIAL_MINT_MAX',
  'CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS', 'CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS',
  'PREVIEW_PASSWORD', 'BASE_URL', 'TRUST_PROXY_HOPS', 'RESEND_API_KEY',
] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of TOUCHED_ENV) savedEnv[key] = process.env[key];

// Assembled at runtime so no secret-shaped literal sits in the repository.
const STRONG_SECRET = Array.from({ length: 48 }, (_unused, i) => String.fromCharCode(97 + (i % 26))).join('');

/** Set the environment ('' or undefined means unset) and drop every cached module. */
function enter(env: Partial<Record<(typeof TOUCHED_ENV)[number], string | undefined>>): void {
  for (const key of TOUCHED_ENV) delete process.env[key];
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined && value !== '') process.env[key] = value;
  }
  // dotenv never overrides a variable that is present, even an empty one, so
  // an empty NODE_ENV stands in for "unset" without a developer's web/.env
  // being able to fill it back in.
  if (!('NODE_ENV' in env) || env.NODE_ENV === undefined || env.NODE_ENV === '') process.env.NODE_ENV = '';
  vi.resetModules();
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
}

beforeEach(() => {
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  vi.spyOn(console, 'info').mockImplementation(() => undefined);
});

afterEach(() => {
  for (const key of TOUCHED_ENV) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.restoreAllMocks();
  vi.resetModules();
});

type QueryResult = { rows: unknown[]; rowCount?: number };

/**
 * Outside NODE_ENV=test the app keeps sessions in the user_sessions table
 * (connect-pg-simple). This answers the store's four statements from a Map so
 * a login made in one request is still there in the next; every other
 * statement goes to `answer`.
 */
function withSessionTable(answer: (sql: string, params?: unknown[]) => QueryResult): void {
  const sessions = new Map<string, unknown>();
  mockQuery.mockImplementation((sql: string, params: unknown[] = []) => {
    const text = String(sql);
    if (text.includes('"user_sessions"')) {
      if (text.startsWith('SELECT sess')) {
        const sess = sessions.get(String(params[0]));
        return Promise.resolve({ rows: sess === undefined ? [] : [{ sess }], rowCount: sess === undefined ? 0 : 1 });
      }
      if (text.startsWith('INSERT INTO')) {
        const raw = params[0];
        sessions.set(String(params[2]), typeof raw === 'string' ? JSON.parse(raw) : raw);
        return Promise.resolve({ rows: [{ sid: params[2] }], rowCount: 1 });
      }
      if (text.startsWith('UPDATE')) {
        return Promise.resolve({ rows: sessions.has(String(params[1])) ? [{ sid: params[1] }] : [], rowCount: 1 });
      }
      if (text.startsWith('DELETE') && text.includes('WHERE sid')) sessions.delete(String(params[0]));
      return Promise.resolve({ rows: [], rowCount: 0 });
    }
    return Promise.resolve(answer(text, params));
  });
}

/**
 * Log in with a password and return a function that makes requests as that
 * session over (what the app believes is) HTTPS. The session cookie is Secure
 * outside development and test, and supertest's cookie jar will not replay a
 * Secure cookie to its plain-HTTP test server, so the cookie is carried by hand.
 */
async function loginSession(app: express.Application, csrf: () => string, email: string) {
  const login = await request(app)
    .post('/login')
    .set('X-Forwarded-Proto', 'https')
    .type('form')
    .send({ _csrf: csrf(), email, password: 'password123' });
  const cookie = ([] as string[])
    .concat(login.headers['set-cookie'] ?? [])
    .map((entry) => entry.split(';')[0])
    .join('; ');
  expect(cookie).toContain('cas.sid=');
  return {
    get: (url: string) => request(app).get(url).set('X-Forwarded-Proto', 'https').set('Cookie', cookie),
    post: (url: string) => request(app).post(url).set('X-Forwarded-Proto', 'https').set('Cookie', cookie),
  };
}

/** The limit a limiter enforces, read from the standard header it writes ("<max>;w=<seconds>"). */
async function policyOf(limiter: RequestHandler, opts: { body?: Record<string, unknown>; session?: unknown } = {}) {
  const mini = express();
  mini.set('trust proxy', 1);
  mini.use(express.json());
  mini.use((req, _res, next) => {
    (req as unknown as { session: unknown }).session = opts.session ?? {};
    next();
  });
  mini.post('/x', limiter, (_req, res) => res.json({ ok: true }));
  const res = await request(mini).post('/x').set('X-Forwarded-For', '203.0.113.77').send(opts.body ?? {});
  return { status: res.status, policy: res.headers['ratelimit-policy'] as string | undefined };
}

const NOT_DEV_OR_TEST: Array<[string, string | undefined]> = [
  ['production', 'production'],
  ['staging', 'staging'],
  ['unset', undefined],
];

// ---------------------------------------------------------------------------
// 1. Development conveniences
// ---------------------------------------------------------------------------

describe('isDevOrTestEnv: the one definition of "a development environment"', () => {
  it('is true only for the literal values development and test', async () => {
    const { isDevOrTestEnv } = await import('../../src/middleware/auth');
    expect(isDevOrTestEnv({ NODE_ENV: 'development' })).toBe(true);
    expect(isDevOrTestEnv({ NODE_ENV: 'test' })).toBe(true);
    for (const value of ['production', 'staging', 'Development', 'TEST', 'dev', '', undefined]) {
      expect(isDevOrTestEnv({ NODE_ENV: value })).toBe(false);
    }
  });
});

describe('STRIPE_BYPASS (POST /features/:id/bypass-dev)', () => {
  async function loginAndBypass(env: Record<string, string | undefined>, email: string) {
    enter({ SESSION_SECRET: STRONG_SECRET, ADMIN_EMAIL: 'owner@example.com', ...env });
    const app = (await import('../../src/app')).default;
    const { generateCsrfToken } = await import('../../src/middleware/auth');
    const csrf = (): string => generateCsrfToken({} as Request);
    const passwordHash = await bcrypt.hash('password123', 1);
    withSessionTable((sql) =>
      sql.includes('FROM customers WHERE email')
        ? { rows: [{ id: 'cust-1', email, name: 'C', password: passwordHash, email_verified: true }] }
        : sql.includes('FROM feature_requests fr')
          ? {
              rows: [{
                id: 'feat-1', status: 'payment_pending', build_id: 'build-1', app_name: 'App',
                email, name: 'C',
              }],
            }
          : { rows: [], rowCount: 1 }
    );
    const session = await loginSession(app, csrf, email);
    const res = await session.post('/features/feat-1/bypass-dev').type('form').send({ _csrf: csrf() });
    const queued = mockQuery.mock.calls.some(([sql]) => /UPDATE feature_requests SET status = 'queued'/.test(String(sql)));
    // Proof the session really is logged in: a customer page renders instead
    // of redirecting to /login. Without this a 403 could just mean "anonymous".
    const dashboard = await session.get('/dashboard');
    expect(dashboard.status).toBe(200);
    return { status: res.status, queued };
  }

  it.each(NOT_DEV_OR_TEST)('NODE_ENV %s: a customer cannot skip payment even with the flag set', async (_label, nodeEnv) => {
    const outcome = await loginAndBypass({ NODE_ENV: nodeEnv, STRIPE_BYPASS: 'true' }, 'customer@example.com');
    expect(outcome.status).toBe(403);
    expect(outcome.queued).toBe(false);
  });

  it('production: only the admin account may use it, and only when the flag is set', async () => {
    const admin = await loginAndBypass({ NODE_ENV: 'production', STRIPE_BYPASS: 'true' }, 'owner@example.com');
    expect(admin.status).toBe(302);
    expect(admin.queued).toBe(true);

    const adminNoFlag = await loginAndBypass({ NODE_ENV: 'production' }, 'owner@example.com');
    expect(adminNoFlag.status).toBe(403);
    expect(adminNoFlag.queued).toBe(false);
  });

  it('development: any logged-in customer may use it on their own request when the flag is set', async () => {
    const dev = await loginAndBypass({ NODE_ENV: 'development', STRIPE_BYPASS: 'true' }, 'customer@example.com');
    expect(dev.status).toBe(302);
    expect(dev.queued).toBe(true);
    const devNoFlag = await loginAndBypass({ NODE_ENV: 'development' }, 'customer@example.com');
    expect(devNoFlag.status).toBe(403);
  });
});

describe('CAS_HARBOR_STRIPE_BYPASS, ?dev_trial= and the admin lamp read', () => {
  const TRIAL = {
    trial_id: '00000000-0000-4000-8000-000000000001',
    cookie_token: 'c'.repeat(64),
    companion_name: 'Haven',
    companion_assigned: 'Haven',
    started_at: new Date(),
    expires_at: new Date(Date.now() + 86_400_000),
    last_seen_at: new Date(),
  };
  const OTHER_TRIAL = '99999999-8888-4777-8666-555555555555';
  const BUILD = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';

  async function adminAgent(nodeEnv: string | undefined) {
    enter({
      NODE_ENV: nodeEnv,
      SESSION_SECRET: STRONG_SECRET,
      ADMIN_EMAIL: 'owner@example.com',
      CAS_HARBOR_ENABLED: 'true',
      CAS_HARBOR_STRIPE_BYPASS: 'true',
    });
    const app = (await import('../../src/app')).default;
    const { generateCsrfToken } = await import('../../src/middleware/auth');
    const passwordHash = await bcrypt.hash('password123', 1);
    withSessionTable((text, params) => {
      if (text.includes('FROM customers WHERE email')) {
        return {
          rows: [{ id: 'cust-admin', email: 'owner@example.com', name: 'O', password: passwordHash, email_verified: true }],
        };
      }
      if (text.includes('FROM builds') && Array.isArray(params) && params[0] === BUILD) {
        return {
          rows: [{
            id: BUILD, customer_id: 'cust-someone-else', app_name: 'Not Yours', status: 'preview',
            review_status: 'approved', review_notes: null, preview_url: 'https://x.example', live_url: null,
          }],
        };
      }
      if (text.includes('FROM trial_keepers') && text.includes('cookie_token = $1')) return { rows: [TRIAL] };
      if (text.includes('INSERT INTO trial_keepers')) return { rows: [TRIAL] };
      if (text.includes('FROM trial_keepers') && Array.isArray(params) && params[0] === OTHER_TRIAL) {
        return { rows: [{ ...TRIAL, trial_id: OTHER_TRIAL }] };
      }
      if (text.includes('COUNT(*)')) return { rows: [{ count: '0', total: '0' }] };
      return { rows: [], rowCount: 0 };
    });
    const session = await loginSession(app, () => generateCsrfToken({} as Request), 'owner@example.com');
    // Proof this is a real admin session, in every environment: /admin does
    // not answer 403 or bounce to /login. (The overrides below must be inert
    // for exactly this session.)
    const adminPage = await session.get('/admin/emails');
    expect([302, 403]).not.toContain(adminPage.status);
    return session;
  }

  it.each(NOT_DEV_OR_TEST)('NODE_ENV %s: all three are inert, even for a real admin session', async (_label, nodeEnv) => {
    const session = await adminAgent(nodeEnv);

    const bypass = await session.post('/lighthouse/bypass').send({ email: 'fresh@example.com' });
    expect(bypass.status).toBe(403);
    expect(bypass.body.error).toBe('bypass_blocked_in_production');

    await session.get(`/lighthouse/table/recall?format=json&dev_trial=${OTHER_TRIAL}`);
    const overrideUsed = mockQuery.mock.calls.some(([, params]) => Array.isArray(params) && params.includes(OTHER_TRIAL));
    expect(overrideUsed).toBe(false);

    const lamp = await session.get(`/lighthouse/lamp/${BUILD}`);
    expect(lamp.status).toBe(404);
  });

  it('development: the admin overrides work for a real admin session (the control case)', async () => {
    const session = await adminAgent('development');
    await session.get(`/lighthouse/table/recall?format=json&dev_trial=${OTHER_TRIAL}`);
    const overrideUsed = mockQuery.mock.calls.some(([, params]) => Array.isArray(params) && params.includes(OTHER_TRIAL));
    expect(overrideUsed).toBe(true);
    const lamp = await session.get(`/lighthouse/lamp/${BUILD}`);
    expect(lamp.status).toBe(200);
  });
});

describe('one-time links printed to the console when no mail key is set (logDevEmailLink)', () => {
  const LINK = 'http://localhost:4000/verify-email?token=' + 'a'.repeat(64);

  async function printedBy(env: Parameters<typeof enter>[0]): Promise<string[]> {
    enter(env);
    const info = vi.spyOn(console, 'info').mockImplementation(() => undefined);
    // This file mocks the email service; the real module is what is under test here.
    const { logDevEmailLink } = await vi.importActual<typeof import('../../src/services/email')>('../../src/services/email');
    logDevEmailLink('Verification link', LINK);
    return info.mock.calls.map((call) => call.map(String).join(' '));
  }

  it.each(NOT_DEV_OR_TEST)('NODE_ENV %s: nothing is printed', async (_label, nodeEnv) => {
    expect(await printedBy({ NODE_ENV: nodeEnv })).toEqual([]);
  });

  it.each([['development'], ['test']])('NODE_ENV %s without a mail key: the link is printed on a marked line', async (nodeEnv) => {
    expect(await printedBy({ NODE_ENV: nodeEnv })).toEqual([`[dev] Email is not configured. Verification link: ${LINK}`]);
  });

  it('development with a mail key: nothing is printed', async () => {
    expect(await printedBy({ NODE_ENV: 'development', RESEND_API_KEY: ['re', 'test', 'key'].join('_') })).toEqual([]);
  });
});

describe('CAS_ALLOW_RAILWAY_WRITES', () => {
  it('enables Railway writes only together with NODE_ENV=production', async () => {
    for (const [nodeEnv, flag, expected] of [
      ['production', 'true', true],
      ['production', '', false],
      ['production', 'false', false],
      ['production', '1', false],
      ['development', 'true', false],
      ['test', 'true', false],
      ['staging', 'true', false],
      ['', 'true', false],
    ] as Array<[string, string, boolean]>) {
      enter({ NODE_ENV: nodeEnv, CAS_ALLOW_RAILWAY_WRITES: flag });
      const { isRailwayWriteEnabled } = await import('../../src/services/railway');
      expect(isRailwayWriteEnabled(), `NODE_ENV=${nodeEnv || '(unset)'} flag=${flag || '(unset)'}`).toBe(expected);
    }
  });
});

// ---------------------------------------------------------------------------
// 2. NODE_ENV=test relaxations are gone under NODE_ENV=production
// ---------------------------------------------------------------------------

describe('NODE_ENV=test relaxations do not reach production', () => {
  it('the session secret: test may run on a generated one, production refuses a placeholder', async () => {
    const { resolveSessionSecret, SESSION_SECRET_PLACEHOLDERS } = await import('../../src/middleware/auth');
    expect(resolveSessionSecret({ NODE_ENV: 'test' }).ephemeral).toBe(true);
    for (const nodeEnv of ['production', 'staging', undefined]) {
      expect(() => resolveSessionSecret({ NODE_ENV: nodeEnv })).toThrow();
      for (const placeholder of SESSION_SECRET_PLACEHOLDERS) {
        expect(() => resolveSessionSecret({ NODE_ENV: nodeEnv, SESSION_SECRET: placeholder })).toThrow();
      }
    }
  });

  it('auth limiters (login, signup, reset): skipped under test, enforced under production', async () => {
    enter({ NODE_ENV: 'test' });
    let limits = await import('../../src/middleware/auth-rate-limit');
    expect((await policyOf(limits.authRateLimit, { body: { email: 'a@example.com' } })).policy).toBeUndefined();
    expect((await policyOf(limits.signupRateLimit, { body: { email: 'a@example.com' } })).policy).toBeUndefined();

    enter({ NODE_ENV: 'production' });
    limits = await import('../../src/middleware/auth-rate-limit');
    // The last bucket in the chain (per address) writes the header: 5 x the pair cap.
    expect((await policyOf(limits.authRateLimit, { body: { email: 'a@example.com' } })).policy).toBe(
      `${limits.AUTH_DEFAULT_MAX * limits.IP_BUCKET_MULTIPLIER};w=${limits.AUTH_DEFAULT_WINDOW_MS / 1000}`
    );
    expect((await policyOf(limits.signupRateLimit, { body: { email: 'a@example.com' } })).policy).toBe(
      `${limits.SIGNUP_DEFAULT_MAX * limits.IP_BUCKET_MULTIPLIER};w=${limits.SIGNUP_DEFAULT_WINDOW_MS / 1000}`
    );
  });

  it('anonymous intake limiters: raised under test, real under production', async () => {
    enter({ NODE_ENV: 'test' });
    let anon = await import('../../src/middleware/anon-rate-limit');
    expect((await policyOf(anon.anonDraftCreateRateLimit)).policy).toBe('1000;w=86400');

    enter({ NODE_ENV: 'production' });
    anon = await import('../../src/middleware/anon-rate-limit');
    expect((await policyOf(anon.anonDraftCreateRateLimit)).policy).toBe('5;w=86400');
    expect((await policyOf(anon.anonMessageRateLimit)).policy).toBe('20;w=60');
    expect((await policyOf(anon.anonDailyTurnRateLimit)).policy).toBe(`${anon.ANON_DAILY_TURNS_PER_ADDRESS};w=86400`);
    expect((await policyOf(anon.anonGlobalDailyTurnRateLimit)).policy).toBe(`${anon.ANON_DAILY_TURNS_TOTAL};w=86400`);
  });

  it('Harbor turn and build limiters: 10,000 under test, the documented defaults under production', async () => {
    enter({ NODE_ENV: 'test' });
    let harbor = await import('../../src/middleware/companion-rate-limit');
    expect((await policyOf(harbor.companionTurnRateLimit)).policy).toBe('10000;w=900');

    enter({ NODE_ENV: 'production' });
    harbor = await import('../../src/middleware/companion-rate-limit');
    expect((await policyOf(harbor.companionTurnRateLimit)).policy).toBe('60;w=900');
    expect((await policyOf(harbor.companionTurnIpRateLimit)).policy).toBe('150;w=900');
    expect((await policyOf(harbor.companionBuildRateLimit)).policy).toBe('6;w=3600');
    expect((await policyOf(harbor.companionBuildIpRateLimit)).policy).toBe('24;w=3600');
  });

  it('trial minting: effectively unlimited under test, 20 an hour per address under production', async () => {
    enter({ NODE_ENV: 'production' });
    const { trialCookie } = await import('../../src/middleware/trial-cookie');
    const pool = {
      query: vi.fn().mockImplementation((sql: string) =>
        Promise.resolve(
          String(sql).includes('INSERT INTO trial_keepers')
            ? {
                rows: [{
                  trial_id: 't', cookie_token: 'd'.repeat(64), companion_name: 'Haven', companion_assigned: 'Haven',
                  started_at: new Date(), expires_at: new Date(), last_seen_at: new Date(),
                }],
              }
            : { rows: [] }
        )
      ),
    };
    const mini = express();
    mini.set('trust proxy', 1);
    mini.use(trialCookie(pool as never));
    mini.get('/x', (_req, res) => res.json({ ok: true }));
    const statuses: number[] = [];
    for (let i = 0; i < 22; i += 1) {
      statuses.push((await request(mini).get('/x').set('X-Forwarded-For', '203.0.113.90')).status);
    }
    expect(statuses.slice(0, 20).every((status) => status === 200)).toBe(true);
    expect(statuses.slice(20)).toEqual([429, 429]);
    // Twenty rows were minted, not twenty-two.
    const inserts = pool.query.mock.calls.filter(([sql]) => String(sql).includes('INSERT INTO trial_keepers'));
    expect(inserts).toHaveLength(20);
  });

  it('Harbor cost caps have no test-mode relaxation: $5 per identity and $50 shared, consulted in every environment', async () => {
    for (const nodeEnv of ['test', 'production']) {
      enter({ NODE_ENV: nodeEnv });
      const runtime = await import('../../src/companion/runtime');
      expect(runtime.getCompanionDailyCostCapMicros(), nodeEnv).toBe(5_000_000);
      expect(runtime.getCompanionGlobalDailyCostCapMicros(), nodeEnv).toBe(50_000_000);

      // With no override variable set, a turn reads the spend and is refused
      // when it is over the cap. (The runtime used to skip this read under
      // NODE_ENV=test; it no longer looks at NODE_ENV at all.)
      const query = vi.fn();
      query.mockResolvedValueOnce({
        rows: [{ trial_id: 't-1', companion_name: 'Haven', companion_assigned: 'Haven', started_at: new Date() }],
      });
      query.mockResolvedValueOnce({ rows: [{ total: '6000000', global_total: '6000000' }] });
      const failure = await runtime
        .turn({ pool: { query } as never, trialId: 't-1', keeperText: 'Tell me about pricing.' })
        .catch((err: unknown) => err);
      expect(failure, nodeEnv).toBeInstanceOf(runtime.CompanionDailyCostCapError);
      expect(query.mock.calls.some(([sql]) => String(sql).includes('SUM(ccl.total_cost_micros)')), nodeEnv).toBe(true);
    }
  });

  it('the NODE_ENV=test boot warning in server.ts describes what test mode relaxes, and not the cost caps', () => {
    // The warning is a string in server.ts's start(), which is not exported.
    // It used to say the Haven cost caps are "raised or skipped"; the runtime
    // has no test-mode branch (previous test), so the text must not say that.
    const read = (file: string): string => readFileSync(join(__dirname, '../../src', file), 'utf8');
    const source = read('server.ts');
    const start = source.indexOf("console.warn(\n      'WARNING: NODE_ENV=test.");
    expect(start).toBeGreaterThan(-1);
    const warning = source
      .slice(start, source.indexOf(');', start))
      .replace(/'\s*\+\s*'/g, '')
      .replace(/\s+/g, ' ');
    expect(warning).toContain('trial-mint cap');
    expect(warning).toMatch(/cost caps are not relaxed/);
    expect(warning).not.toMatch(/cost caps are raised/);
    // The claim rests on this: the cost-cap module does not read NODE_ENV.
    expect(read('companion/runtime.ts')).not.toContain('NODE_ENV');
  });

  it('public form limiters and the feedback limiter: skipped under test, enforced under production', async () => {
    enter({ NODE_ENV: 'production', SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' });
    const app = (await import('../../src/app')).default;
    const { generateCsrfToken } = await import('../../src/middleware/auth');
    const guide = await request(app)
      .post('/guide')
      .type('form')
      .send({ _csrf: generateCsrfToken({} as Request), email: 'reader@example.com' });
    expect(guide.headers['ratelimit-policy']).toBe('20;w=3600');
    const contact = await request(app)
      .post('/contact')
      .type('form')
      .send({ _csrf: generateCsrfToken({} as Request), name: 'A', email: 'a@example.com', message: 'hello' });
    expect(contact.headers['ratelimit-policy']).toBe('10;w=3600');

    enter({ NODE_ENV: 'test', SESSION_SECRET: STRONG_SECRET });
    const testApp = (await import('../../src/app')).default;
    const testAuth = await import('../../src/middleware/auth');
    const relaxed = await request(testApp)
      .post('/guide')
      .type('form')
      .send({ _csrf: testAuth.generateCsrfToken({} as Request), email: 'reader@example.com' });
    expect(relaxed.headers['ratelimit-policy']).toBeUndefined();
  });

  it('sessions: an in-memory store under test, the PostgreSQL store under production', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    enter({ NODE_ENV: 'production', SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' });
    await import('../../src/app');
    expect(log.mock.calls.some((call) => String(call[0]).includes('Session store: PostgreSQL'))).toBe(true);

    log.mockClear();
    enter({ NODE_ENV: 'test', SESSION_SECRET: STRONG_SECRET });
    await import('../../src/app');
    expect(log.mock.calls.some((call) => String(call[0]).includes('Session store: PostgreSQL'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 3. The inventory itself
// ---------------------------------------------------------------------------

describe('inventory: no development gate is written as "not production"', () => {
  it('src/ has no NODE_ENV !== \'production\' test left (it treats unset and staging as development)', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const srcRoot = path.join(__dirname, '..', '..', 'src');
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (entry.name.endsWith('.ts')) {
          fs.readFileSync(full, 'utf-8')
            .split('\n')
            .forEach((line, index) => {
              const code = line.replace(/\/\/.*$/, '').replace(/^\s*\*.*$/, '');
              if (/NODE_ENV\s*!==?\s*['"]production['"]/.test(code)) {
                offenders.push(`${path.relative(srcRoot, full)}:${index + 1}`);
              }
            });
        }
      }
    };
    walk(srcRoot);
    expect(offenders).toEqual([]);
  });
});

describe('inventory: .env.example', () => {
  async function load() {
    const fs = await import('fs');
    const path = await import('path');
    const webRoot = path.join(__dirname, '..', '..');
    const template = fs.readFileSync(path.join(webRoot, '.env.example'), 'utf-8');
    const documented = new Map<string, string>();
    for (const line of template.split('\n')) {
      const match = line.match(/^([A-Z0-9_]+)=(.*)$/);
      if (match) documented.set(match[1], match[2]);
    }
    const read = new Set<string>();
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (entry.name.endsWith('.ts')) {
          const source = fs.readFileSync(full, 'utf-8');
          for (const pattern of [
            /process\.env\.([A-Z0-9_]+)/g,
            /process\.env\[['"]([A-Z0-9_]+)['"]\]/g,
            /envInt\(\s*['"]([A-Z0-9_]+)['"]/g,
            /\benv\.([A-Z][A-Z0-9_]+)/g,
          ]) {
            for (const match of source.matchAll(pattern)) read.add(match[1]);
          }
        }
      }
    };
    walk(path.join(webRoot, 'src'));
    return { documented, read };
  }

  it('documents every variable the server reads, and nothing it does not', async () => {
    const { documented, read } = await load();
    expect([...read].filter((name) => !documented.has(name)).sort()).toEqual([]);
    expect([...documented.keys()].filter((name) => !read.has(name)).sort()).toEqual([]);
  });

  it('turns no escape hatch on by default and ships no usable secret', async () => {
    const { documented } = await load();
    for (const flag of [
      'STRIPE_BYPASS', 'CAS_HARBOR_STRIPE_BYPASS', 'CAS_ALLOW_RAILWAY_WRITES',
      'PREVIEW_PASSWORD',
      'CAS_LLM_DAILY_CALL_CAP', 'CAS_HARBOR_TURN_MAX', 'CAS_HARBOR_TURN_IP_MAX', 'CAS_HARBOR_BUILD_MAX',
      'CAS_HARBOR_TRIAL_MINT_MAX', 'CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS', 'CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS',
    ]) {
      expect(documented.get(flag), flag).toBe('');
    }
    // TRUST_PROXY_HOPS is the one setting whose empty value is NOT the tight
    // one: unset means one trusted proxy, and a local run has none, so any
    // client could forge X-Forwarded-For past the per-address limits. The
    // template therefore ships the explicit value that trusts no header.
    expect(documented.get('TRUST_PROXY_HOPS')).toBe('0');
    for (const secret of [
      'ANTHROPIC_API_KEY', 'ANTHROPIC_ADMIN_KEY', 'OPENAI_API_KEY', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET',
      'RESEND_API_KEY', 'RESEND_WEBHOOK_SECRET', 'OPS_API_KEY', 'WORKER_WEBHOOK_SECRET', 'RAILWAY_API_TOKEN',
      'RAILWAY_ADMIN_TOKEN', 'SENTRY_DSN',
    ]) {
      expect(documented.get(secret), secret).toBe('');
    }
    // CAS_HARBOR_ENABLED is the product surface, not a relaxation: a fresh
    // checkout is meant to have the Haven conversation (the only path to a
    // queued build) switched on. The legacy /try and /intake pages collect a
    // spec and start no build.
    expect(documented.get('CAS_HARBOR_ENABLED')).toBe('true');
    // The one non-empty secret is a placeholder the server refuses to sign with.
    const { SESSION_SECRET_PLACEHOLDERS } = await import('../../src/middleware/auth');
    expect(SESSION_SECRET_PLACEHOLDERS).toContain(documented.get('SESSION_SECRET'));
    // And the template does not start a server in test mode.
    expect(documented.get('NODE_ENV')).toBe('development');
  });
});
