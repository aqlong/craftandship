// A refused request (HTTP 4xx) gets a page whose numeral and wording match its
// status. views/500.ejs prints "500 Something went sideways." whatever status it
// is sent with, and about two dozen call sites used to render it with a 403,
// 404, 409, 429 or 400. They now go through renderErrorPage (utils/error-page.ts)
// and views/error.ejs; 500.ejs is left to real server faults.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import express from 'express';
import request from 'supertest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';
import type { Request } from 'express';

process.env.ADMIN_EMAIL = 'admin@example.com';

const mockState = vi.hoisted(() => ({ query: null as ReturnType<typeof vi.fn> | null }));
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  mockState.query = mockQuery;
  const mockConnect = vi.fn(async () => ({
    query: (...args: unknown[]) => (mockState.query as unknown as (...a: unknown[]) => unknown)(...args),
    release: vi.fn(),
  }));
  return { default: { query: mockQuery, on: vi.fn(), connect: mockConnect } };
});
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));
vi.mock('../../src/services/stripe', () => ({}));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
  analyzeBuildFailure: vi.fn(),
}));
vi.mock('../../src/services/spec_extraction', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/services/spec_extraction')>()),
  extractSpecFromConversation: vi.fn(),
}));
vi.mock('../../src/services/harbor_to_compose', () => ({ translateExtractedSpecToV1: vi.fn() }));
vi.mock('../../src/services/site_copy_crawl', () => ({
  crawlSite: vi.fn(),
  isPrivateOrLoopbackHost: vi.fn().mockReturnValue(false),
}));
vi.mock('../../src/services/railway', () => ({
  deleteRailwayProject: vi.fn(),
  RailwayApiError: class RailwayApiError extends Error {},
  isRailwayWriteEnabled: vi.fn().mockReturnValue(false),
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import { makeCustomerLlmDailyLimit } from '../../src/middleware/llm-rate-limit';
import { renderErrorPage } from '../../src/utils/error-page';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const webRoot = path.join(__dirname, '../..');

/** The visible numeral of the error page: the first element whose text is a bare status. */
function numeral(html: string): string | null {
  const match = html.match(/>\s*(\d{3})\s*<\/div>/);
  return match ? match[1] : null;
}

async function loginAgent(email: string): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'customer-1', email, name: 'Test User', password: hashed, email_verified: true }],
  });
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: generateCsrfToken({} as Request), email, password: 'password123' });
  return agent;
}

function expectHonestPage(html: string, status: number, text: RegExp): void {
  expect(numeral(html), 'the numeral on the page').toBe(String(status));
  expect(html).toMatch(text);
  expect(html).not.toContain('Something went sideways');
  expect(html).not.toContain('Something on our end');
  if (status !== 500) expect(html).not.toMatch(/>\s*500\s*</);
}

describe('a refused request is not shown the page for a server fault', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
  });

  it('a forbidden admin request: 403, the 403 numeral, and the reason', async () => {
    const agent = await loginAgent('regular@example.com');
    const res = await agent.get('/admin/builds');
    expect(res.status).toBe(403);
    expectHonestPage(res.text, 403, /That request was not allowed\./);
    expect(res.text).toContain('Admin access required.');
  });

  it('a missing build on an admin page: 404, the 404 numeral, and the reason', async () => {
    const agent = await loginAgent('admin@example.com');
    const res = await agent.get('/admin/builds/00000000-0000-4000-8000-000000000000');
    expect(res.status).toBe(404);
    expectHonestPage(res.text, 404, /That page is missing\./);
    expect(res.text).toContain('Build not found.');
  });

  it('a rate-limited request: 429, the 429 numeral, and a request to wait', async () => {
    const mini = express();
    mini.set('view engine', 'ejs');
    mini.set('views', path.join(webRoot, 'views'));
    mini.use((req, _res, next) => {
      (req as unknown as { session: { customerId?: string } }).session = { customerId: 'customer-1' };
      next();
    });
    mini.get('/intake', makeCustomerLlmDailyLimit({ max: 1 }), (_req, res) => res.send('the intake page'));

    expect((await request(mini).get('/intake')).status).toBe(200);
    const limited = await request(mini).get('/intake');
    expect(limited.status).toBe(429);
    expectHonestPage(limited.text, 429, /Please wait a moment\./);
    expect(limited.text).toContain('daily limit for conversations');
  });

  // These two are a form and a page address, so they get the page. The same
  // refusals of a JSON request are answered in JSON: utils.json-request.test.ts.
  it('an oversized form: 413, the 413 numeral, and a request to send less', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await request(app)
        .post('/guide')
        .type('form')
        .send(`email=${'x'.repeat(1.2 * 1024 * 1024)}`);
      expect(res.status).toBe(413);
      expect(res.headers['content-type']).toMatch(/^text\/html/);
      expectHonestPage(res.text, 413, /That was too large to accept\./);
      expect(res.text).toContain('send less');
      // A client's mistake is not a server fault and is not logged as one.
      expect(errorSpy.mock.calls.some((call) => String(call[0]).includes('Unhandled error'))).toBe(false);
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('a page address that cannot be decoded: 400, the 400 numeral', async () => {
    const res = await request(app).get('/builds/%E0%A4%A');
    expect(res.status).toBe(400);
    expect(res.headers['content-type']).toMatch(/^text\/html/);
    expectHonestPage(res.text, 400, /That request could not be processed\./);
  });

  it('a request that is not allowed in this state: 409', async () => {
    // POST /builds/:id/approve on a build that is not in preview.
    const agent = await loginAgent('owner@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [] }); // the atomic UPDATE promotes nothing
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'b-live', customer_id: 'customer-1', status: 'live', review_status: 'approved' }],
    });
    const res = await agent
      .post('/builds/b-live/approve')
      .type('form')
      .send({ _csrf: generateCsrfToken({} as Request) });
    expect(res.status).toBe(409);
    expectHonestPage(res.text, 409, /That is not possible right now\./);
    expect(res.text).toContain('not in preview status');
  });

  it('a real server fault still gets the 500 page', async () => {
    const mini = express();
    mini.set('view engine', 'ejs');
    mini.set('views', path.join(webRoot, 'views'));
    mini.get('/boom', (_req, res) => res.status(500).render('500', { title: 'Error' }));
    const res = await request(mini).get('/boom');
    expect(res.status).toBe(500);
    expect(res.text).toContain('Something went sideways.');
  });
});

describe('renderErrorPage', () => {
  function renderedWith(status: number, options?: Parameters<typeof renderErrorPage>[2]) {
    const calls: Array<{ view: string; locals: Record<string, unknown> }> = [];
    let sent = 0;
    const res = {
      status(code: number) {
        sent = code;
        return res;
      },
      render(view: string, locals: Record<string, unknown>) {
        calls.push({ view, locals });
      },
    };
    renderErrorPage(res as never, status, options);
    return { sent, ...calls[0] };
  }

  it('sends the status it shows: one value, two uses', () => {
    for (const status of [400, 403, 404, 409, 413, 429, 418]) {
      const out = renderedWith(status);
      expect(out.sent).toBe(status);
      expect(out.view).toBe('error');
      expect(out.locals.errorStatus).toBe(status);
      expect(String(out.locals.errorHeading).length).toBeGreaterThan(5);
    }
  });

  it('takes the caller\'s message and heading over the defaults', () => {
    const out = renderedWith(404, { error: 'Finding not found or already resolved.', heading: 'That finding is gone.' });
    expect(out.locals.errorMessage).toBe('Finding not found or already resolved.');
    expect(out.locals.errorHeading).toBe('That finding is gone.');
  });

  it('refuses a status that is not a client error: a server fault belongs on 500.ejs', () => {
    for (const status of [200, 302, 399, 500, 502, 404.5]) {
      expect(() => renderedWith(status), String(status)).toThrow(RangeError);
    }
  });
});

describe('no refusal is rendered with the page for a server fault', () => {
  function sourceFiles(dir: string): string[] {
    const out: string[] = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) out.push(...sourceFiles(full));
      else if (entry.name.endsWith('.ts')) out.push(full);
    }
    return out;
  }

  // res.status(4xx).render('500', ...) in any spelling, including a status
  // held in a variable that the same statement bounds to 4xx.
  const refusalOn500 = /\.status\(\s*4\d\d\s*\)\s*\.render\(\s*['"]500['"]/;
  const clientStatusOn500 = /status\(\s*clientStatus\s*\)\s*\.render\(\s*['"]500['"]/;

  it('the pattern catches the old call sites (control)', () => {
    expect(refusalOn500.test("res.status(403).render('500', { title: 'Forbidden' });")).toBe(true);
    expect(refusalOn500.test("return res.status(404)\n  .render('500', {")).toBe(true);
    expect(clientStatusOn500.test("res.status(clientStatus).render('500', {")).toBe(true);
    expect(refusalOn500.test("res.status(500).render('500', { title: 'Error' });")).toBe(false);
  });

  it('src/ has none', () => {
    const offenders = sourceFiles(path.join(webRoot, 'src'))
      .filter((file) => {
        const text = fs.readFileSync(file, 'utf-8');
        return refusalOn500.test(text) || clientStatusOn500.test(text);
      })
      .map((file) => path.relative(webRoot, file));
    expect(offenders).toEqual([]);
  });

  it('views/error.ejs prints the status it is given, not a fixed number', () => {
    const view = fs.readFileSync(path.join(webRoot, 'views', 'error.ejs'), 'utf-8');
    expect(view).toMatch(/<%= errorStatus %>/);
    expect(view.replace(/<%#[\s\S]*?%>/g, '')).not.toMatch(/>\s*\d{3}\s*</);
  });
});
