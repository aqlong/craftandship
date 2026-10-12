import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import bcrypt from 'bcrypt';

// Outside NODE_ENV=test the app keeps sessions in PostgreSQL through
// connect-pg-simple. These tests drive a real login against the real app in
// production mode and look at the SQL the store sends, rather than grepping the
// source: the table is user_sessions (created by schema.sql, which must not be
// raced by the library creating it too) and the library neither probes for it
// nor creates it.

const mockQuery = vi.hoisted(() => vi.fn());
vi.mock('../../src/db/index', () => ({
  default: { query: mockQuery, connect: vi.fn(), on: vi.fn() },
}));
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn(() => 'token'),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));

const ENV_KEYS = ['NODE_ENV', 'SESSION_SECRET', 'BASE_URL', 'ADMIN_EMAIL'] as const;
const savedEnv: Record<string, string | undefined> = {};
for (const key of ENV_KEYS) savedEnv[key] = process.env[key];

// Assembled at runtime so no secret-shaped literal sits in the repository.
const STRONG_SECRET = Array.from({ length: 48 }, (_unused, i) => String.fromCharCode(97 + (i % 26))).join('');

beforeEach(() => {
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
  vi.spyOn(console, 'warn').mockImplementation(() => undefined);
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
  vi.restoreAllMocks();
  vi.resetModules();
});

describe('Session store configuration (production mode, observed through the SQL it sends)', () => {
  it('stores a login in the user_sessions table and never creates a table itself', async () => {
    process.env.NODE_ENV = 'production';
    process.env.SESSION_SECRET = STRONG_SECRET;
    process.env.BASE_URL = 'https://app.example.test';
    vi.resetModules();

    const app = (await import('../../src/app')).default;
    const { generateCsrfToken } = await import('../../src/middleware/auth');
    const passwordHash = await bcrypt.hash('password123', 1);
    const statements: string[] = [];
    mockQuery.mockReset().mockImplementation((sql: string) => {
      statements.push(String(sql));
      if (String(sql).includes('FROM customers WHERE email')) {
        return Promise.resolve({
          rows: [{ id: 'cust-1', email: 'customer@example.com', name: 'C', password: passwordHash, email_verified: true }],
        });
      }
      return Promise.resolve({ rows: [], rowCount: 1 });
    });

    const login = await request(app)
      .post('/login')
      .set('X-Forwarded-Proto', 'https')
      .type('form')
      .send({ _csrf: generateCsrfToken({} as never), email: 'customer@example.com', password: 'password123' });

    // The login really happened: the session cookie was issued.
    expect(([] as string[]).concat(login.headers['set-cookie'] ?? []).join(';')).toContain('cas.sid=');
    // ... and the store wrote it to the table schema.sql creates.
    expect(statements.some((sql) => /INSERT INTO\s+"user_sessions"/i.test(sql))).toBe(true);
    // With createTableIfMissing the library first probes for the table
    // (to_regclass) and then creates it; neither may happen.
    expect(statements.filter((sql) => /to_regclass|CREATE\s+TABLE/i.test(sql))).toEqual([]);
  });
});
