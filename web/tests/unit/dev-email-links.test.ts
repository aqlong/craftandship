// Development convenience: with no mail provider configured, the one-time
// verification and password-reset links are printed to the server console so a
// developer can finish signing up locally.
//
// The rules under test:
//   - only when NODE_ENV is exactly "development" or "test" (isDevOrTestEnv);
//   - only while RESEND_API_KEY is unset;
//   - never in an HTTP response, a rendered page, a redirect, or the email_log
//     row (a rendered reset link was once a critical account-takeover bug).
//
// The real email service runs here (only the database is mocked). Each case
// sets the environment, resets the module registry and imports a fresh app.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import type { Request } from 'express';

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));

const TOUCHED_ENV = ['NODE_ENV', 'SESSION_SECRET', 'BASE_URL', 'RESEND_API_KEY', 'ADMIN_EMAIL'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of TOUCHED_ENV) savedEnv[key] = process.env[key];

// Assembled at runtime so no secret-shaped literal sits in the repository.
const STRONG_SECRET = Array.from({ length: 48 }, (_unused, i) => String.fromCharCode(97 + (i % 26))).join('');
const TOKEN_IN_LINK = /(verify-email|reset-password)\?token=([0-9a-f]{64})/;

let infoSpy: ReturnType<typeof vi.spyOn>;
let logSpy: ReturnType<typeof vi.spyOn>;
let warnSpy: ReturnType<typeof vi.spyOn>;

function enter(env: Partial<Record<(typeof TOUCHED_ENV)[number], string>>): void {
  for (const key of TOUCHED_ENV) delete process.env[key];
  for (const [key, value] of Object.entries(env)) process.env[key] = value;
  // dotenv never overrides a variable that is present, even an empty one.
  if (!('NODE_ENV' in env)) process.env.NODE_ENV = '';
  if (!('RESEND_API_KEY' in env)) process.env.RESEND_API_KEY = '';
  vi.resetModules();
}

/** Answers the statements signup, resend-verification and forgot-password send; records every call. */
function answerAuthQueries(): void {
  mockQuery.mockReset().mockImplementation((sql: string) => {
    const text = String(sql);
    if (text.includes('INSERT INTO customers')) {
      return Promise.resolve({ rows: [{ id: 'cust-1', email: 'new@example.com', name: 'New Person' }], rowCount: 1 });
    }
    if (text.includes('SELECT id FROM customers WHERE email')) return Promise.resolve({ rows: [], rowCount: 0 });
    if (text.includes('SELECT id, name, email_verified FROM customers')) {
      return Promise.resolve({ rows: [{ id: 'cust-1', name: 'New Person', email_verified: false }], rowCount: 1 });
    }
    if (text.includes('SELECT id, name FROM customers WHERE email')) {
      return Promise.resolve({ rows: [{ id: 'cust-1', name: 'New Person' }], rowCount: 1 });
    }
    return Promise.resolve({ rows: [], rowCount: 0 });
  });
}

async function loadApp() {
  const app = (await import('../../src/app')).default;
  const { generateCsrfToken } = await import('../../src/middleware/auth');
  return { app, csrf: (): string => generateCsrfToken({} as Request) };
}

/** Every line the server wrote through the console, whichever level. */
function consoleLines(): string[] {
  return [infoSpy, logSpy, warnSpy].flatMap((spy) => spy.mock.calls.map((call) => call.map(String).join(' ')));
}

function devLines(): string[] {
  return consoleLines().filter((line) => line.startsWith('[dev] Email is not configured.'));
}

/** Everything a client could read from a response. */
function whatTheClientSaw(res: request.Response): string {
  return `${res.text}\n${JSON.stringify(res.headers)}\n${JSON.stringify(res.body)}`;
}

/** The parameters of every email_log INSERT, joined. The stored HTML must not carry a live token. */
function loggedEmailRows(): string {
  return mockQuery.mock.calls
    .filter(([sql]) => String(sql).includes('INSERT INTO email_log'))
    .map(([, params]) => JSON.stringify(params))
    .join('\n');
}

beforeEach(() => {
  infoSpy = vi.spyOn(console, 'info').mockImplementation(() => undefined);
  logSpy = vi.spyOn(console, 'log').mockImplementation(() => undefined);
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  for (const key of TOUCHED_ENV) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe('development, no mail key: the link goes to the server console only', () => {
  it('POST /signup prints the verification link; the response and email_log never carry the token', async () => {
    enter({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    answerAuthQueries();
    const { app, csrf } = await loadApp();

    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf(), name: 'New Person', email: 'new@example.com', password: 'a-long-enough-password' });

    expect(res.status).toBe(302);
    // The address is not in the redirect URL; the next page reads it from the session.
    expect(res.headers.location).toBe('/verify-email-sent');
    const lines = devLines();
    expect(lines).toHaveLength(1);
    const found = lines[0].match(new RegExp(`^\\[dev\\] Email is not configured\\. Verification link: http://\\S+/${TOKEN_IN_LINK.source}$`));
    expect(found, lines[0]).not.toBeNull();
    const token = lines[0].match(TOKEN_IN_LINK)![2];
    expect(whatTheClientSaw(res)).not.toContain(token);
    expect(loggedEmailRows()).not.toContain(token);
    // The mail was still recorded as skipped, as before.
    expect(loggedEmailRows()).toContain('RESEND_API_KEY not set');
  });

  it('POST /resend-verification prints a fresh verification link', async () => {
    enter({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    answerAuthQueries();
    const { app, csrf } = await loadApp();

    const res = await request(app).post('/resend-verification').type('form').send({ _csrf: csrf(), email: 'new@example.com' });

    expect(res.status).toBe(302);
    const lines = devLines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(/Verification link: http:\/\/\S+\/verify-email\?token=[0-9a-f]{64}$/);
    expect(whatTheClientSaw(res)).not.toContain(lines[0].match(TOKEN_IN_LINK)![2]);
  });

  it('POST /forgot-password prints the reset link; the page never contains it', async () => {
    enter({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    answerAuthQueries();
    const { app, csrf } = await loadApp();

    const res = await request(app).post('/forgot-password').type('form').send({ _csrf: csrf(), email: 'new@example.com' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('check your inbox');
    const lines = devLines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(/Password reset link: http:\/\/\S+\/reset-password\?token=[0-9a-f]{64}$/);
    const token = lines[0].match(TOKEN_IN_LINK)![2];
    expect(whatTheClientSaw(res)).not.toContain(token);
    expect(whatTheClientSaw(res)).not.toContain('/reset-password?token=');
    expect(loggedEmailRows()).not.toContain(token);
  });

  it('prints nothing for an unknown address (no link exists, and nothing distinguishes it for the client)', async () => {
    enter({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET });
    mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
    const { app, csrf } = await loadApp();

    const res = await request(app).post('/forgot-password').type('form').send({ _csrf: csrf(), email: 'nobody@example.com' });

    expect(res.status).toBe(200);
    expect(devLines()).toEqual([]);
  });
});

describe('NODE_ENV=test counts as development', () => {
  it('prints exactly one line for a signup', async () => {
    enter({ NODE_ENV: 'test', SESSION_SECRET: STRONG_SECRET });
    answerAuthQueries();
    const { app, csrf } = await loadApp();

    await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf(), name: 'New Person', email: 'new@example.com', password: 'a-long-enough-password' });

    expect(devLines()).toHaveLength(1);
  });
});

describe('never outside development and test', () => {
  const NOT_DEV_OR_TEST: Array<[string, Record<string, string>]> = [
    ['production', { NODE_ENV: 'production', SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' }],
    ['staging', { NODE_ENV: 'staging', SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' }],
    ['unset', { SESSION_SECRET: STRONG_SECRET, BASE_URL: 'https://app.example.test' }],
  ];

  it.each(NOT_DEV_OR_TEST)('NODE_ENV %s: no console line carries a token, for signup, resend and forgot-password', async (_label, env) => {
    enter(env);
    answerAuthQueries();
    const { app, csrf } = await loadApp();

    const signup = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf(), name: 'New Person', email: 'new@example.com', password: 'a-long-enough-password' });
    const resend = await request(app).post('/resend-verification').type('form').send({ _csrf: csrf(), email: 'new@example.com' });
    const forgot = await request(app).post('/forgot-password').type('form').send({ _csrf: csrf(), email: 'new@example.com' });

    // Positive controls: the three handlers ran and reached the mail layer
    // (which skipped the send for lack of a key), so the silence below means something.
    expect([signup.status, resend.status]).toEqual([302, 302]);
    expect(forgot.status).toBe(200);
    expect(loggedEmailRows()).toContain('email_verification');
    expect(loggedEmailRows()).toContain('password_reset');
    expect(devLines()).toEqual([]);
    expect(consoleLines().filter((line) => /token=[0-9a-f]{64}/.test(line))).toEqual([]);
  });
});

describe('never when a mail key is configured', () => {
  it('development with RESEND_API_KEY set: the mail is sent, nothing is printed', async () => {
    enter({ NODE_ENV: 'development', SESSION_SECRET: STRONG_SECRET, RESEND_API_KEY: ['re', 'test', 'key'].join('_') });
    answerAuthQueries();
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, text: async () => '', json: async () => ({ id: 'msg_1' }) });
    vi.stubGlobal('fetch', fetchMock);
    const { app, csrf } = await loadApp();

    const res = await request(app)
      .post('/signup')
      .type('form')
      .send({ _csrf: csrf(), name: 'New Person', email: 'new@example.com', password: 'a-long-enough-password' });
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(res.status).toBe(302);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(devLines()).toEqual([]);
    expect(consoleLines().filter((line) => /token=[0-9a-f]{64}/.test(line))).toEqual([]);
  });
});
