// Boot-time and environment-dependent security behavior of src/app.ts.
//
// app.ts reads NODE_ENV and SESSION_SECRET once, when it is imported. Each
// test here sets the environment, imports a fresh copy of the module, and
// restores the environment afterwards.

import { describe, it, expect, vi, afterEach } from 'vitest';
import request from 'supertest';
import bcrypt from 'bcrypt';
import type { Application, Request } from 'express';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));
// Only the Harbor bypass test reaches this; it stands in for the transactional
// trial-to-keeper promotion so that test can stay about the session. The rest
// of the module (the subscription-status vocabulary the routes import) is real.
vi.mock('../../src/services/keeper_promotion', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/services/keeper_promotion')>()),
  promoteTrialToKeeper: vi.fn().mockResolvedValue({
    customerId: 'cust-admin',
    promoted: true,
    conversationTurnsTransferred: 0,
    memoryRowsTransferred: 0,
  }),
}));

const ENV_KEYS = [
  'NODE_ENV', 'SESSION_SECRET', 'BASE_URL', 'PREVIEW_PASSWORD', 'TRUST_PROXY_HOPS',
  'CAS_HARBOR_ENABLED', 'CAS_HARBOR_STRIPE_BYPASS', 'ADMIN_EMAIL',
] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];

// Assembled at runtime so no secret-shaped literal sits in the repo.
const STRONG_SECRET = Array.from({ length: 48 }, (_unused, i) => String.fromCharCode(97 + (i % 26))).join('');

class ExitCalled extends Error {
  constructor(readonly code: number | undefined) {
    super(`process.exit(${code})`);
  }
}

interface Loaded {
  app: Application;
  csrf: () => string;
}

/**
 * Import a fresh src/app.ts under the given environment. An empty string
 * stands for "unset": dotenv never overrides a variable that is present, so
 * the result does not depend on whether a developer has a web/.env file.
 */
async function loadApp(env: Partial<Record<(typeof ENV_KEYS)[number], string>>): Promise<Loaded> {
  for (const key of ENV_KEYS) process.env[key] = env[key] ?? '';
  vi.resetModules();
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
  const appModule = await import('../../src/app');
  const authModule = await import('../../src/middleware/auth');
  return {
    app: appModule.default,
    csrf: () => authModule.generateCsrfToken({} as Request),
  };
}

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.restoreAllMocks();
});

function silenceConsole(): { errors: () => string; warnings: () => string } {
  const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
  return {
    errors: () => error.mock.calls.map((call) => call.join(' ')).join('\n'),
    warnings: () => warn.mock.calls.map((call) => call.join(' ')).join('\n'),
  };
}

function trapExit(): void {
  vi.spyOn(process, 'exit').mockImplementation(((code?: number) => {
    throw new ExitCalled(code);
  }) as never);
}

describe('boot: SESSION_SECRET', () => {
  it.each([
    ['production', 'dev_secret_change_me'],
    ['production', 'change-me-in-production'],
    ['production', ''],
    ['', 'change-me-in-production'],
    ['', ''],
    ['staging', 'change-me-in-production'],
    ['production', 'too-short-to-be-a-secret'],
  ])('refuses to start with NODE_ENV=%j and SESSION_SECRET=%j', async (nodeEnv, secret) => {
    const logs = silenceConsole();
    trapExit();

    await expect(loadApp({ NODE_ENV: nodeEnv, SESSION_SECRET: secret })).rejects.toMatchObject({ code: 1 });
    expect(logs.errors()).toContain('FATAL: SESSION_SECRET');
  });

  it('starts in development on the .env.example placeholder, with a loud warning and a random secret', async () => {
    const logs = silenceConsole();
    trapExit();

    const { app, csrf } = await loadApp({ NODE_ENV: 'development', SESSION_SECRET: 'change-me-in-production' });

    expect(logs.warnings()).toContain('SESSION_SECRET is unset or still a placeholder');
    // The placeholder is not what signs things: the published secret is random.
    expect(process.env.SESSION_SECRET).toMatch(/^[0-9a-f]{64}$/);
    // And the app works: a token it issues is accepted.
    const res = await request(app).post('/logout').type('form').send({ _csrf: csrf() });
    expect(res.status).toBe(302);
  });

  it('starts in production with a real secret', async () => {
    silenceConsole();
    trapExit();

    const { app } = await loadApp({ NODE_ENV: 'production', SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' });

    const res = await request(app).get('/terms');
    expect(res.status).toBe(200);
  });
});

describe('production mode', () => {
  const production = { NODE_ENV: 'production', SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' };

  it('sends HSTS and warns at boot when BASE_URL is missing', async () => {
    const logs = silenceConsole();
    const { app } = await loadApp({ ...production, BASE_URL: '' });

    const res = await request(app).get('/terms');
    expect(res.headers['strict-transport-security']).toBe('max-age=31536000; includeSubDomains; preload');
    expect(logs.warnings()).toContain('BASE_URL is not set');
  });

  it('sends HSTS and the other security headers on files from public/, not only on pages', async () => {
    silenceConsole();
    const { app } = await loadApp(production);

    for (const path of ['/style.css', '/favicon.svg']) {
      const res = await request(app).get(path);
      expect(res.status, path).toBe(200);
      expect(res.headers['strict-transport-security'], path).toBe('max-age=31536000; includeSubDomains; preload');
      expect(res.headers['x-content-type-options'], path).toBe('nosniff');
      expect(res.headers['x-frame-options'], path).toBe('DENY');
      expect(res.headers['referrer-policy'], path).toBe('strict-origin-when-cross-origin');
    }
  });

  it('issues the session cookie HttpOnly, Secure and SameSite=Lax over HTTPS', async () => {
    silenceConsole();
    const { app, csrf } = await loadApp(production);
    const passwordHash = await bcrypt.hash('password123', 1);
    mockQuery.mockImplementation((sql: string) =>
      Promise.resolve(
        typeof sql === 'string' && sql.includes('FROM customers WHERE email')
          ? { rows: [{ id: 'cust-1', email: 'user@example.com', name: 'U', password: passwordHash, email_verified: true }] }
          : { rows: [], rowCount: 0 }
      )
    );

    const res = await request(app)
      .post('/login')
      .set('X-Forwarded-Proto', 'https')
      .type('form')
      .send({ _csrf: csrf(), email: 'user@example.com', password: 'password123' });

    expect(res.status).toBe(302);
    const cookie = String(res.headers['set-cookie']);
    expect(cookie).toContain('cas.sid=');
    expect(cookie).toMatch(/;\s*HttpOnly/i);
    expect(cookie).toMatch(/;\s*Secure/i);
    expect(cookie).toMatch(/;\s*SameSite=Lax/i);
  });

  it('ignores X-Forwarded-* headers when TRUST_PROXY_HOPS=0 (no proxy in front)', async () => {
    silenceConsole();
    const { app, csrf } = await loadApp({ ...production, TRUST_PROXY_HOPS: '0' });
    const passwordHash = await bcrypt.hash('password123', 1);
    mockQuery.mockImplementation((sql: string) =>
      Promise.resolve(
        typeof sql === 'string' && sql.includes('FROM customers WHERE email')
          ? { rows: [{ id: 'cust-1', email: 'user@example.com', name: 'U', password: passwordHash, email_verified: true }] }
          : { rows: [], rowCount: 0 }
      )
    );

    // A client claiming HTTPS through a header it wrote itself is not believed,
    // so the Secure session cookie is not sent over what is really plain HTTP.
    const res = await request(app)
      .post('/login')
      .set('X-Forwarded-Proto', 'https')
      .set('X-Forwarded-For', '203.0.113.50')
      .type('form')
      .send({ _csrf: csrf(), email: 'user@example.com', password: 'password123' });

    expect(res.status).toBe(302);
    expect(String(res.headers['set-cookie'] ?? '')).not.toContain('cas.sid=');
  });

  it('does not show error text to the client', async () => {
    silenceConsole();
    const { app } = await loadApp(production);
    // The page-view logger calls pool.query on every page GET outside test.
    mockQuery.mockImplementation(() => {
      throw new Error('relation "customers" does not exist at character 15');
    });

    const res = await request(app).get('/terms');

    expect(res.status).toBe(500);
    expect(res.text).toContain('An unexpected error occurred.');
    expect(res.text).not.toContain('relation');
    expect(res.text).not.toContain('customers');
  });

  it('shows error text in development, where the reader is the developer', async () => {
    silenceConsole();
    const { app } = await loadApp({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    mockQuery.mockImplementation(() => {
      throw new Error('relation "customers" does not exist');
    });

    const res = await request(app).get('/terms');

    expect(res.status).toBe(500);
    expect(res.text).toContain('relation');
  });
});

describe('PREVIEW_PASSWORD gate', () => {
  // Built at runtime; contains a colon on purpose.
  const password = ['open', 'sesame', '2026'].join(':');
  const basic = (pass: string): string => 'Basic ' + Buffer.from(`anyone:${pass}`).toString('base64');
  const gated = { NODE_ENV: 'test', SESSION_SECRET: STRONG_SECRET, PREVIEW_PASSWORD: password };

  it('lets public marketing paths through and challenges everything else', async () => {
    const { app } = await loadApp(gated);

    expect((await request(app).get('/pricing')).status).toBe(200);
    const blocked = await request(app).get('/login');
    expect(blocked.status).toBe(401);
    expect(blocked.headers['www-authenticate']).toContain('Basic');
  });

  it('answers the challenge, and serves exempt files, with the security headers', async () => {
    const { app } = await loadApp(gated);

    const challenge = await request(app).get('/login');
    expect(challenge.status).toBe(401);
    const asset = await request(app).get('/style.css');
    expect(asset.status).toBe(200);
    for (const [label, res] of [['challenge', challenge], ['asset', asset]] as const) {
      expect(res.headers['x-content-type-options'], label).toBe('nosniff');
      expect(res.headers['x-frame-options'], label).toBe('DENY');
      expect(res.headers['content-security-policy'], label).toContain("frame-ancestors 'none'");
    }
  });

  it('accepts the right password, including one that contains a colon', async () => {
    const { app } = await loadApp(gated);

    expect((await request(app).get('/login').set('Authorization', basic(password))).status).toBe(200);
  });

  it('rejects wrong passwords without throwing, whatever their byte length', async () => {
    const { app } = await loadApp(gated);
    const sameLengthMoreBytes = String.fromCharCode(0xe9).repeat(password.length);

    for (const wrong of ['', 'wrong', password + 'x', password.slice(0, -1), sameLengthMoreBytes]) {
      const res = await request(app).get('/login').set('Authorization', basic(wrong));
      expect(res.status).toBe(401);
    }
    // No colon at all in the decoded value.
    const noColon = 'Basic ' + Buffer.from(password.replace(/:/g, '')).toString('base64');
    expect((await request(app).get('/login').set('Authorization', noColon)).status).toBe(401);
  });

  it('is off when PREVIEW_PASSWORD is unset', async () => {
    const { app } = await loadApp({ NODE_ENV: 'test', SESSION_SECRET: STRONG_SECRET });

    expect((await request(app).get('/login')).status).toBe(200);
  });

  describe('static asset exemption', () => {
    it.each([['/style.css'], ['/nav.js'], ['/favicon-32x32.png'], ['/favicon.svg'], ['/brand-harbor/']])(
      'serves a file that exists under public/ (%s) without the password',
      async (path) => {
        const { app } = await loadApp(gated);
        const res = await request(app).get(path);

        // The directory has no index file, so the last case only proves that
        // the exemption does not apply to it; every file must be served.
        if (path.endsWith('/')) expect(res.status).toBe(401);
        else expect(res.status).toBe(200);
      },
    );

    // Each of these ends in a static-asset extension but is not a file in
    // public/. They must meet the password challenge like any other path, not
    // reach a route handler (a redirect to /login, a 404 page, a lookup) first.
    it.each([
      ['/admin/anything.css'],
      ['/admin/builds/anything.js'],
      ['/builds/anything.png'],
      ['/api/builds/anything.svg'],
      ['/lighthouse/lamp/anything.webp'],
      ['/not-a-real-file.css'],
      ['/style.css/extra.css'],
      ['/..%2fsrc%2fstyles%2finput.css'],
      ['/%2e%2e/src/styles/input.css'],
      ['/style.css%00.png'],
      ['/STYLE.CSS.map'],
    ])('does not let %s skip the password because of its extension', async (path) => {
      const { app } = await loadApp(gated);
      const res = await request(app).get(path);

      expect(res.status, path).toBe(401);
      expect(res.headers['www-authenticate']).toContain('Basic');
      expect(mockQuery).not.toHaveBeenCalled();
    });

    it('does not let an asset-looking Harbor path mint a trial (a database write) without the password', async () => {
      const { app } = await loadApp({ ...gated, CAS_HARBOR_ENABLED: 'true' });

      for (const path of ['/lighthouse/table.png', '/lighthouse/lamp/anything.webp', '/lighthouse/table/turn.js']) {
        const res = await request(app).get(path);
        expect(res.status, path).toBe(401);
      }
      expect(mockQuery).not.toHaveBeenCalled();
    });

    it('gives the right password the same reach on those paths as on any other', async () => {
      const { app } = await loadApp(gated);
      const res = await request(app).get('/admin/builds/anything.js').set('Authorization', basic(password));

      // Past the gate: the admin router's own login redirect, not a 401.
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });
  });
});

describe('a deployment that is not explicitly development or test gets the hardened settings', () => {
  // NODE_ENV unset (written '' here, see loadApp) and a custom value such as
  // "staging" must behave like production for every control that has a
  // development-mode relaxation.
  it.each([['unset', ''], ['staging', 'staging']])(
    'NODE_ENV %s: HSTS, a Secure session cookie, a Secure trial cookie, no error text',
    async (_label, nodeEnv) => {
      silenceConsole();
      const { app, csrf } = await loadApp({
        NODE_ENV: nodeEnv,
        SESSION_SECRET: STRONG_SECRET,
        CAS_HARBOR_ENABLED: 'true',
        CAS_HARBOR_STRIPE_BYPASS: 'true',
        ADMIN_EMAIL: 'owner@example.com',
      });
      const passwordHash = await bcrypt.hash('password123', 1);
      mockQuery.mockImplementation((sql: string) =>
        Promise.resolve(
          typeof sql === 'string' && sql.includes('FROM customers WHERE email')
            ? { rows: [{ id: 'cust-1', email: 'user@example.com', name: 'U', password: passwordHash, email_verified: true }] }
            : typeof sql === 'string' && sql.includes('INSERT INTO trial_keepers')
              ? {
                  rows: [{
                    trial_id: '00000000-0000-4000-8000-000000000001', cookie_token: 'b'.repeat(64),
                    companion_name: 'Haven', companion_assigned: 'Haven',
                    started_at: new Date(), expires_at: new Date(Date.now() + 86_400_000), last_seen_at: new Date(),
                  }],
                }
              : { rows: [], rowCount: 0 }
        )
      );

      const page = await request(app).get('/terms');
      expect(page.headers['strict-transport-security']).toBe('max-age=31536000; includeSubDomains; preload');

      const login = await request(app)
        .post('/login')
        .set('X-Forwarded-Proto', 'https')
        .type('form')
        .send({ _csrf: csrf(), email: 'user@example.com', password: 'password123' });
      expect(login.status).toBe(302);
      expect(String(login.headers['set-cookie'])).toMatch(/cas\.sid=[^;]+;.*;\s*Secure/i);

      const trial = await request(app).get('/lighthouse/table').set('X-Forwarded-Proto', 'https');
      const trialCookie = ([] as string[]).concat(trial.headers['set-cookie'] ?? []).find((c) => c.startsWith('cas_harbor_trial='));
      expect(trialCookie).toMatch(/;\s*Secure/i);
      expect(trialCookie).toMatch(/;\s*HttpOnly/i);

      // The development payment bypass stays shut although its flag is set.
      const bypass = await request(app).post('/lighthouse/bypass').send({ email: 'fresh@example.com' });
      expect(bypass.status).toBe(403);
      expect(bypass.body.error).toBe('bypass_blocked_in_production');

      // Error text is not shown. (The page-view logger's query throws, which
      // reaches the global error handler; POST /login would not do, since the
      // login route catches database errors and renders its own message.)
      mockQuery.mockImplementation(() => {
        throw new Error('relation "customers" does not exist at character 15');
      });
      const failing = await request(app).get('/terms');
      expect(failing.status).toBe(500);
      expect(failing.text).toContain('An unexpected error occurred.');
      expect(failing.text).not.toContain('relation');
      expect(failing.text).not.toContain('does not exist');
    }
  );

  it('development keeps the local-friendly settings (no HSTS, cookie usable over plain HTTP)', async () => {
    silenceConsole();
    const { app, csrf } = await loadApp({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    const passwordHash = await bcrypt.hash('password123', 1);
    mockQuery.mockImplementation((sql: string) =>
      Promise.resolve(
        typeof sql === 'string' && sql.includes('FROM customers WHERE email')
          ? { rows: [{ id: 'cust-1', email: 'user@example.com', name: 'U', password: passwordHash, email_verified: true }] }
          : { rows: [], rowCount: 0 }
      )
    );
    const page = await request(app).get('/terms');
    expect(page.headers['strict-transport-security']).toBeUndefined();
    const asset = await request(app).get('/style.css');
    expect(asset.headers['strict-transport-security']).toBeUndefined();
    // The other headers do not depend on the environment.
    expect(asset.headers['x-content-type-options']).toBe('nosniff');
    const login = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: csrf(), email: 'user@example.com', password: 'password123' });
    const cookie = String(login.headers['set-cookie']);
    expect(cookie).toContain('cas.sid=');
    expect(cookie).not.toMatch(/;\s*Secure/i);
  });
});

describe('boot warning about an unset BASE_URL', () => {
  // Follows the same rule as the rest of the app: only an explicit
  // "development" or "test" is exempt. A deployment that forgot NODE_ENV, or
  // runs as "staging", is told that its emailed links are built from the
  // request's Host header.
  it.each([['production'], ['staging'], ['unset', '']])(
    'warns when NODE_ENV is %s and BASE_URL is missing',
    async (label, nodeEnv = label) => {
      const logs = silenceConsole();
      await loadApp({ NODE_ENV: nodeEnv, SESSION_SECRET: STRONG_SECRET, BASE_URL: '' });

      expect(logs.warnings()).toContain('BASE_URL is not set');
    }
  );

  it.each([['development'], ['test']])('stays quiet when NODE_ENV is %s', async (nodeEnv) => {
    const logs = silenceConsole();
    await loadApp({ NODE_ENV: nodeEnv, SESSION_SECRET: STRONG_SECRET, BASE_URL: '' });

    expect(logs.warnings()).not.toContain('BASE_URL is not set');
  });

  it.each([['production'], ['staging'], ['']])('stays quiet when BASE_URL is set (NODE_ENV %j)', async (nodeEnv) => {
    const logs = silenceConsole();
    await loadApp({ NODE_ENV: nodeEnv, SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' });

    expect(logs.warnings()).not.toContain('BASE_URL is not set');
  });
});

describe('Harbor payment bypass cannot mint an admin session', () => {
  it('POST /lighthouse/bypass refuses the admin email, and the session it leaves behind is not admin', async () => {
    silenceConsole();
    const { app, csrf } = await loadApp({
      NODE_ENV: 'test',
      SESSION_SECRET: STRONG_SECRET,
      CAS_HARBOR_ENABLED: 'true',
      CAS_HARBOR_STRIPE_BYPASS: 'true',
      ADMIN_EMAIL: 'owner@example.com',
    });
    // Any lookup finds a row: a trial for the cookie middleware, a customer for the bypass.
    mockQuery.mockImplementation(() =>
      Promise.resolve({
        rowCount: 1,
        rows: [{
          id: 'cust-admin',
          trial_id: '00000000-0000-4000-8000-000000000001',
          cookie_token: 'a'.repeat(64),
          companion_name: 'Haven',
          companion_assigned: 'Haven',
          started_at: new Date(),
          expires_at: new Date(Date.now() + 86_400_000),
          last_seen_at: new Date(),
        }],
      })
    );

    const agent = request.agent(app);
    const bypass = await agent.post('/lighthouse/bypass').send({ email: 'owner@example.com' });
    // First line of defense: the bypass will not hand out the admin address
    // (or any address that already has an account) without a password.
    expect(bypass.status).toBe(403);
    expect(bypass.body.error).toBe('admin_email_not_allowed');

    // The session is still anonymous, so /admin sends it to the login page.
    const admin = await agent.get('/admin/builds');
    expect(admin.status).toBe(302);
    expect(admin.headers.location).toMatch(/^\/login/);

    // Second line of defense, independent of the first: even a session that
    // somehow carried the admin's email is not admin unless a password login
    // stamped it (isAdminSession requires authenticatedAt).
    const { isAdminSession } = await import('../../src/middleware/auth');
    const forged = { session: { customerId: 'cust-admin', customerEmail: 'owner@example.com' } };
    expect(isAdminSession(forged as never)).toBe(false);
    expect(isAdminSession({ session: { ...forged.session, authenticatedAt: Date.now() } } as never)).toBe(true);

    // A real password login as the same account is admin.
    const passwordHash = await bcrypt.hash('password123', 1);
    mockQuery.mockImplementation((sql: string) =>
      Promise.resolve(
        typeof sql === 'string' && sql.includes('FROM customers WHERE email')
          ? { rows: [{ id: 'cust-admin', email: 'owner@example.com', name: 'O', password: passwordHash, email_verified: true }] }
          : typeof sql === 'string' && sql.includes('COUNT(*)')
            ? { rows: [{ total: '0', count: '0' }], rowCount: 1 }
            : { rows: [], rowCount: 0 }
      )
    );
    const real = request.agent(app);
    const realLogin = await real.post('/login').type('form').send({ _csrf: csrf(), email: 'owner@example.com', password: 'password123' });
    expect(realLogin.status).toBe(302);
    expect(realLogin.headers.location).not.toMatch(/^\/login/);
    const adminPage = await real.get('/admin/emails');
    expect(adminPage.status).toBe(200);
  });
});

describe('page view logging: a failed insert is reported, once, and never fails the page', () => {
  // The middleware is not mounted under NODE_ENV=test, so this runs the app in
  // development. The page-view insert is made to fail the way a missing table
  // would; every other statement answers normally.
  it('logs the first failure with its cause and stays quiet for the ones that follow', async () => {
    const consoleOutput = silenceConsole();
    const { app } = await loadApp({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    let pageViewInserts = 0;
    mockQuery.mockImplementation((sql: unknown) => {
      if (typeof sql === 'string' && sql.includes('INSERT INTO page_views')) {
        pageViewInserts += 1;
        return Promise.reject(new Error('relation "page_views" does not exist'));
      }
      return Promise.resolve({ rows: [], rowCount: 0 });
    });

    for (let i = 0; i < 4; i += 1) {
      expect((await request(app).get('/terms')).status).toBe(200);
    }
    // The inserts are fire-and-forget; give their rejections a turn to land.
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(pageViewInserts).toBe(4);
    const reports = consoleOutput
      .errors()
      .split('\n')
      .filter((line) => line.includes('Page view was not recorded'));
    expect(reports).toHaveLength(1);
    expect(reports[0]).toContain('page_views');
  });
});
