// POST /lighthouse/table/turn through the real app, with the session store down.
//
// routes.lighthouse.crisis-first.test.ts proves the handler at router level.
// What it cannot show is where src/app.ts puts it: the session middleware runs
// before the lighthouse router, so a request that carries a session cookie
// while the session store's database is unreachable used to fail there with a
// 500 and never reached the handler. app.ts therefore mounts crisisFirst
// directly after the body parsers, ahead of the session middleware.
//
// Everything here is the real src/app.ts in production mode, which uses the
// PostgreSQL session store. Only the database pool is fake, and every query on
// it rejects. The cookie is signed with the app's own secret so that
// express-session accepts it and really asks the store about it (the control
// test proves that by observing the store's query and the 500 it causes).

import { describe, it, expect, vi, afterEach } from 'vitest';
import crypto from 'crypto';
import request from 'supertest';
import type { Application } from 'express';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: mockQuery, on: vi.fn() },
}));

// The alert email is the one outbound side effect of a crisis message; observe it, send nothing.
const sendCrisisAlertEmail = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock('../../src/services/email', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/services/email')>()),
  sendCrisisAlertEmail,
}));

const ENV_KEYS = ['NODE_ENV', 'SESSION_SECRET', 'BASE_URL', 'CAS_HARBOR_ENABLED', 'ADMIN_EMAIL', 'PREVIEW_PASSWORD'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];

// Assembled at runtime so no secret-shaped literal sits in the repo.
const SECRET = crypto.randomBytes(24).toString('hex');
const CRISIS_TEXT = 'I want to kill myself';
const ORDINARY_TEXT = 'Tell me what a website for my practice could look like';
const STORE_DOWN = 'session store database unreachable';

async function loadApp(harborEnabled: boolean, previewPassword = ''): Promise<Application> {
  const env: Record<(typeof ENV_KEYS)[number], string> = {
    NODE_ENV: 'production',
    SESSION_SECRET: SECRET,
    BASE_URL: 'https://app.example.test',
    CAS_HARBOR_ENABLED: harborEnabled ? 'true' : '',
    ADMIN_EMAIL: '',
    PREVIEW_PASSWORD: previewPassword,
  };
  for (const key of ENV_KEYS) process.env[key] = env[key];
  vi.resetModules();
  mockQuery.mockReset().mockRejectedValue(new Error(STORE_DOWN));
  sendCrisisAlertEmail.mockClear();
  return (await import('../../src/app')).default;
}

/**
 * The session cookie express-session accepts: "s:<id>.<base64 HMAC-SHA256 of the id, unpadded>", URL-encoded,
 * under the name it has outside development and test (these tests load the app with NODE_ENV=production).
 */
function signedSessionCookie(sessionId: string): string {
  const mac = crypto.createHmac('sha256', SECRET).update(sessionId).digest('base64').replace(/=+$/, '');
  return `__Host-cas.sid=${encodeURIComponent(`s:${sessionId}.${mac}`)}`;
}
const SESSION_COOKIE = (): string => signedSessionCookie(crypto.randomBytes(16).toString('hex'));

const sessionStoreQueries = (): string[] =>
  mockQuery.mock.calls.map((call) => String(call[0])).filter((sql) => /FROM "?user_sessions"?/.test(sql));
const wroteCompanionTurn = (): boolean =>
  mockQuery.mock.calls.some((call) => /INSERT INTO companion_turns/.test(String(call[0])));

function silenceConsole(): void {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
}

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.restoreAllMocks();
});

describe('POST /lighthouse/table/turn with a session cookie while the session store is down', () => {
  it('control: an ordinary message is failed by the session middleware (500), after the store was asked about the cookie', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app).post('/lighthouse/table/turn').set('Cookie', SESSION_COOKIE()).send({ keeperText: ORDINARY_TEXT });

    expect(res.status).toBe(500);
    expect(res.status).not.toBe(200);
    // The 500 is the session store's failure, not something later: the store was queried for this session.
    expect(sessionStoreQueries().length).toBeGreaterThan(0);
    expect(res.text).not.toMatch(/reach 988/);
  });

  it('a crisis message in the same situation gets the fixed crisis script (200, 988), without the session store being asked', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app).post('/lighthouse/table/turn').set('Cookie', SESSION_COOKIE()).send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(200);
    expect(res.body.companionText).toMatch(/988/);
    expect(res.body.companionText).toMatch(/I'm an AI/);
    expect(res.body.railFlags).toEqual(['crisis', 'suicide']);
    expect(res.body.turnId).toBeNull();
    expect(res.body).not.toHaveProperty('error');
    expect(res.body).not.toHaveProperty('requiresEmail');
    // It was answered ahead of the session middleware, so the dead store was never consulted for it.
    expect(sessionStoreQueries()).toEqual([]);
  });

  it('the operator is still alerted, although nothing can be stored and no database call succeeds', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app).post('/lighthouse/table/turn').set('Cookie', SESSION_COOKIE()).send({ keeperText: CRISIS_TEXT });
    expect(res.status).toBe(200);

    await vi.waitFor(() => expect(sendCrisisAlertEmail).toHaveBeenCalledTimes(1));
    expect(sendCrisisAlertEmail.mock.calls[0][0]).toMatchObject({ categories: ['suicide'] });
  });

  it('the same crisis message sent as a form post (the other body parser) gets the script too', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Cookie', SESSION_COOKIE())
      .type('form')
      .send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(200);
    expect(res.body.companionText).toMatch(/988/);
  });
});

describe('the early mount does not weaken the cross-origin write guard (app.ts mounts the guard later)', () => {
  it.each([['cross-site'], ['same-site']])(
    'Sec-Fetch-Site: %s, no cookie: refused with the guard\'s 403; no script, no alert, nothing stored',
    async (site) => {
      silenceConsole();
      const app = await loadApp(true);

      const res = await request(app)
        .post('/lighthouse/table/turn')
        .set('Sec-Fetch-Site', site)
        .send({ keeperText: CRISIS_TEXT });

      expect(res.status).toBe(403);
      expect(res.body.error).toMatch(/cross-origin/i);
      expect(JSON.stringify(res.body)).not.toMatch(/reach 988/);
      // Give a (wrongly) started background job time to run before asserting that it did not.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(sendCrisisAlertEmail).not.toHaveBeenCalled();
      expect(wroteCompanionTurn()).toBe(false);
    }
  );

  it('an Origin that is not the request\'s own host is refused the same way', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Origin', 'https://evil.example.test')
      .send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/cross-origin/i);
    expect(JSON.stringify(res.body)).not.toMatch(/reach 988/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sendCrisisAlertEmail).not.toHaveBeenCalled();
  });

  it('a cross-site crisis message that carries a session cookie is not answered either', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Cookie', SESSION_COOKIE())
      .set('Sec-Fetch-Site', 'cross-site')
      .send({ keeperText: CRISIS_TEXT });

    expect(res.status).not.toBe(200);
    expect(res.text).not.toMatch(/reach 988/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sendCrisisAlertEmail).not.toHaveBeenCalled();
    expect(wroteCompanionTurn()).toBe(false);
  });

  it('the same-origin request is answered (Sec-Fetch-Site: same-origin)', async () => {
    silenceConsole();
    const app = await loadApp(true);

    const res = await request(app)
      .post('/lighthouse/table/turn')
      .set('Sec-Fetch-Site', 'same-origin')
      .send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(200);
    expect(res.body.companionText).toMatch(/988/);
  });
});

describe('the early mount stays behind the preview password gate, which app.ts mounts first', () => {
  // Generated per run; nothing here is a real credential.
  const previewPassword = crypto.randomBytes(9).toString('hex');

  it('without credentials a crisis message gets the gate\'s 401: no script, no alert', async () => {
    silenceConsole();
    const app = await loadApp(true, previewPassword);

    const res = await request(app).post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(401);
    expect(res.text).not.toMatch(/reach 988/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sendCrisisAlertEmail).not.toHaveBeenCalled();
  });

  it('with the password the same message gets the script', async () => {
    silenceConsole();
    const app = await loadApp(true, previewPassword);

    const res = await request(app)
      .post('/lighthouse/table/turn')
      .auth('anyone', previewPassword)
      .send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(200);
    expect(res.body.companionText).toMatch(/988/);
  });
});

describe('the mount follows CAS_HARBOR_ENABLED', () => {
  it('with Harbor off the endpoint does not exist: a crisis message is not answered by it', async () => {
    silenceConsole();
    const app = await loadApp(false);

    const res = await request(app).post('/lighthouse/table/turn').send({ keeperText: CRISIS_TEXT });

    expect(res.status).toBe(404);
    expect(res.text).not.toMatch(/reach 988/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sendCrisisAlertEmail).not.toHaveBeenCalled();
  });
});
