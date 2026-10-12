// POST /intake/:buildId/logo: the two refusals that protect the server.
//
// A logo upload costs an image decode or an SVG rebuild, so the route has its
// own per-customer budget, checked before the multipart body is read, and it
// answers 503 with Retry-After when the SVG cleaner already has as many
// rebuilds in flight as it allows. The route and the limiter are real; the
// database and the logo service are stand-ins.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';
import type { Request } from 'express';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return { default: { query: mockQuery, on: vi.fn() } };
});

const processLogo = vi.hoisted(() => vi.fn());
vi.mock('../../src/services/logo', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/services/logo')>();
  return { ...actual, processLogo };
});

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import { LogoBusyError } from '../../src/services/logo';
import { LOGO_UPLOAD_MAX } from '../../src/routes/intake';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const BUILD_ROW = { rows: [{ id: 'build-logo', spec: null, status: 'intake' }] };
const CLEAN_RESULT = {
  cleanedBase64: 'AAAA',
  mimetype: 'image/png',
  width: 40,
  height: 40,
  colors: { primary_color: '#2563eb', secondary_color: '#1e293b', accent_color: '#f59e0b' },
};

function csrf(): string {
  return generateCsrfToken({} as Request);
}

// The budget is per customer, so each test logs in as its own.
async function loggedInAgent(customerId: string): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('a-test-password', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: customerId, email: 'logo@example.com', name: 'Logo User', password: hashed, email_verified: true }],
  });
  await agent.post('/login').type('form').send({ _csrf: csrf(), email: 'logo@example.com', password: 'a-test-password' });
  return agent;
}

function upload(agent: supertest.Agent) {
  return agent
    .post('/intake/build-logo/logo')
    .set('x-csrf-token', csrf())
    .attach('logo', Buffer.from('not really an image'), { filename: 'logo.png', contentType: 'image/png' });
}

describe('POST /intake/:buildId/logo limits', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    processLogo.mockReset();
  });

  it('answers 503 with Retry-After when the cleaner is at its limit, and stores nothing', async () => {
    const agent = await loggedInAgent('customer-logo-busy');
    mockQuery.mockResolvedValue(BUILD_ROW);
    processLogo.mockRejectedValueOnce(new LogoBusyError());

    const res = await upload(agent);

    expect(res.status).toBe(503);
    expect(res.headers['retry-after']).toBe('5');
    expect(res.body.error).toMatch(/busy/i);
    const writes = mockQuery.mock.calls.filter(([sql]) => /UPDATE builds/i.test(String(sql)));
    expect(writes).toHaveLength(0);
  });

  it('refuses the upload after the per-customer budget, before the file is processed', async () => {
    const agent = await loggedInAgent('customer-logo-budget');
    mockQuery.mockResolvedValue(BUILD_ROW);
    processLogo.mockResolvedValue(CLEAN_RESULT);

    for (let i = 0; i < LOGO_UPLOAD_MAX; i += 1) {
      const ok = await upload(agent);
      expect(ok.status).toBe(200);
    }
    const processedBefore = processLogo.mock.calls.length;
    const refused = await upload(agent);

    expect(refused.status).toBe(429);
    expect(refused.body.error).toMatch(/Too many logo uploads/);
    expect(processLogo.mock.calls.length).toBe(processedBefore);
  });
});
