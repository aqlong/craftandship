import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';
import { AssistantNotConfiguredError } from '../../src/utils/assistant';

// Mock DB pool
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

// Mock claude service
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello! Describe your app.'),
}));

// Mock services other routes depend on
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
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
vi.mock('../../src/services/stripe', () => ({
}));

import pool from '../../src/db/index';
import { processIntakeMessage, generateInitialGreeting } from '../../src/services/claude';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const mockProcessIntakeMessage = processIntakeMessage as ReturnType<typeof vi.fn>;
const mockGenerateInitialGreeting = generateInitialGreeting as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

// Call counts are asserted ("called once", "not called"); without this they
// would include calls made by whichever tests happened to run before.
beforeEach(() => {
  mockGenerateInitialGreeting.mockClear();
  mockProcessIntakeMessage.mockClear();
});

async function makeAuthenticatedAgent(): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'customer-xyz', email: 'intake@example.com', name: 'Intake User', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email: 'intake@example.com', password: 'password123' });
  return agent;
}

describe('GET /intake (Haven redirect when CAS_HARBOR_ENABLED=true)', () => {
  const originalFlag = process.env.CAS_HARBOR_ENABLED;
  beforeEach(() => { mockQuery.mockReset(); });
  afterEach(() => {
    if (originalFlag === undefined) delete process.env.CAS_HARBOR_ENABLED;
    else process.env.CAS_HARBOR_ENABLED = originalFlag;
  });

  it('redirects /intake to /lighthouse/table when CAS_HARBOR_ENABLED=true', async () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const agent = await makeAuthenticatedAgent();
    const res = await agent.get('/intake');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/lighthouse/table');
    // Build is NOT created on redirect (no fingerprints in intake_messages
    // or builds tables; legacy code path is short-circuited).
    expect(mockGenerateInitialGreeting).not.toHaveBeenCalled();
  });

  it('redirects /intake/:buildId to /lighthouse/table when flag is on', async () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const agent = await makeAuthenticatedAgent();
    const res = await agent.get('/intake/some-build-id');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/lighthouse/table');
  });
});

describe('GET /intake', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockGenerateInitialGreeting.mockResolvedValue('Hello! Describe your app.');
    delete process.env.CAS_HARBOR_ENABLED;
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).get('/intake');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('creates a build, calls generateInitialGreeting, and redirects to /intake/:buildId', async () => {
    const agent = await makeAuthenticatedAgent();

    // INSERT into builds -> return new build id
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'new-build-123' }] });
    // INSERT greeting message -> no return needed
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/intake');

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/intake/new-build-123');
    expect(mockGenerateInitialGreeting).toHaveBeenCalledOnce();
  });
});

describe('POST /intake/start', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockProcessIntakeMessage.mockReset();
    mockGenerateInitialGreeting.mockResolvedValue('Hello! Describe your app.');
  });

  it('returns 403 without CSRF', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent.post('/intake/start').send({ message: 'I want a booking app' });
    expect(res.status).toBe(403);
  });

  it('returns 400 when message is empty', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/intake/start')
      .set('x-csrf-token', csrf)
      .send({ message: '   ' });
    expect(res.status).toBe(400);
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app)
      .post('/intake/start')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'hello' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('seeds the conversation and returns buildId on success', async () => {
    const agent = await makeAuthenticatedAgent();

    mockProcessIntakeMessage.mockResolvedValueOnce({
      reply: 'Tell me more about the booking flow.',
      specComplete: false,
      spec: null,
    });

    // INSERT builds -> returning id
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'seeded-build-1' }] });
    // INSERT assistant greeting
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT user seed message
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT assistant reply
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const csrf = getValidCsrf();
    const res = await agent
      .post('/intake/start')
      .set('x-csrf-token', csrf)
      .send({ message: 'I want a booking app for my dog groomer business' });

    expect(res.status).toBe(200);
    expect(res.body.buildId).toBe('seeded-build-1');
    expect(mockGenerateInitialGreeting).toHaveBeenCalled();
    expect(mockProcessIntakeMessage).toHaveBeenCalledWith(
      'I want a booking app for my dog groomer business',
      [],
      0
    );
  });
});

describe('GET /intake/:buildId', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 200 with messages rendered for a valid intake build', async () => {
    const agent = await makeAuthenticatedAgent();

    // SELECT build
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-abc', status: 'intake', tier: 'build', spec: null }],
    });
    // SELECT messages
    mockQuery.mockResolvedValueOnce({
      rows: [
        { id: 'msg-1', build_id: 'build-abc', role: 'assistant', content: 'Hello! Describe your app.', created_at: new Date() },
      ],
    });

    const res = await agent.get('/intake/build-abc');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Describe Your Website');
  });

  it('redirects to /builds/:buildId when build is not in intake status', async () => {
    const agent = await makeAuthenticatedAgent();

    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-done', status: 'queued', tier: 'build', spec: { app_name: 'myapp' } }],
    });

    const res = await agent.get('/intake/build-done');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-done');
  });

  it('returns 404 when build does not belong to user', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/intake/not-my-build');
    expect(res.status).toBe(404);
  });
});

describe('POST /intake/message', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockProcessIntakeMessage.mockReset();
  });

  it('returns JSON with reply when given valid CSRF and message', async () => {
    const agent = await makeAuthenticatedAgent();

    // SELECT build (owned, intake status)
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-abc', customer_id: 'customer-xyz', status: 'intake' }],
    });
    // SELECT message history
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT user message
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT assistant reply
    mockQuery.mockResolvedValueOnce({ rows: [] });

    mockProcessIntakeMessage.mockResolvedValueOnce({
      reply: 'What kind of app do you need?',
      specComplete: false,
      spec: null,
    });

    const csrf = getValidCsrf();
    const res = await agent
      .post('/intake/message')
      .set('x-csrf-token', csrf)
      .send({ buildId: 'build-abc', message: 'I want a booking app' });

    expect(res.status).toBe(200);
    expect(res.body.reply).toBe('What kind of app do you need?');
    expect(res.body.specComplete).toBe(false);
  });

  it('answers 503 with the explanation when no model key is configured', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-abc', customer_id: 'customer-xyz', status: 'intake' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // history
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT user message
    mockProcessIntakeMessage.mockRejectedValueOnce(new AssistantNotConfiguredError());
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await agent
        .post('/intake/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ buildId: 'build-abc', message: 'I want a booking app' });

      expect(res.status).toBe(503);
      expect(res.body.code).toBe('assistant_not_configured');
      expect(res.body.error).toMatch(/assistant is not configured on this server/i);
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('returns 403 when CSRF is missing', async () => {
    const agent = await makeAuthenticatedAgent();

    const res = await agent
      .post('/intake/message')
      .send({ buildId: 'build-abc', message: 'hello' });

    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/CSRF/i);
  });

  it('returns 403 when CSRF is tampered', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const tampered = csrf.slice(0, -4) + 'dead';

    const res = await agent
      .post('/intake/message')
      .set('x-csrf-token', tampered)
      .send({ buildId: 'build-abc', message: 'hello' });

    expect(res.status).toBe(403);
  });

  it('updates build with spec and app_name when Claude returns specComplete', async () => {
    const agent = await makeAuthenticatedAgent();

    const spec = {
      spec_version: '0.1',
      app_name: 'booking-app',
      description: 'A booking app',
      user_roles: [],
      pages: [],
      data_models: [],
      actions: [],
      settings: { auth: true, seed_data: false },
    };

    // SELECT build
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-spec', customer_id: 'customer-xyz', status: 'intake' }],
    });
    // SELECT history
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT user message
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT assistant reply
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // UPDATE builds SET spec=... (only called when specComplete=true)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    mockProcessIntakeMessage.mockResolvedValueOnce({
      reply: 'Your app spec is ready.',
      specComplete: true,
      spec,
    });

    const csrf = getValidCsrf();
    const res = await agent
      .post('/intake/message')
      .set('x-csrf-token', csrf)
      .send({ buildId: 'build-spec', message: 'looks good' });

    expect(res.status).toBe(200);
    expect(res.body.specComplete).toBe(true);

    // The 5th query call should be the UPDATE builds SET spec = ...
    const updateCall = mockQuery.mock.calls.find(
      (call: unknown[]) => typeof call[0] === 'string' && (call[0] as string).includes('UPDATE builds SET spec')
    );
    expect(updateCall).toBeDefined();
    expect(updateCall?.[1]).toContain('booking-app'); // app_name in params
  });

  it('returns 400 when message is empty', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();

    const res = await agent
      .post('/intake/message')
      .set('x-csrf-token', csrf)
      .send({ buildId: 'build-abc', message: '   ' });

    expect(res.status).toBe(400);
  });
});

// POST /intake/:buildId/confirm and the 3 cases that exercised it (tier-confirm
// + redirect to /payment/:buildId) were removed 2026-05-01 with the tier kill.
// Harbor handles spec confirmation via /lighthouse/table; the legacy intake
// flow has no destination after confirm.

// ---------------------------------------------------------------------------
// Review round additions
// ---------------------------------------------------------------------------

describe('POST /intake/message body validation', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockProcessIntakeMessage.mockReset();
  });

  it('answers 400, not an unhandled rejection, when message is not a string', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();

    // A JSON number and an object both lack .trim(); they used to throw outside the try block.
    for (const bad of [42, { a: 1 }, ['x'], true]) {
      const res = await agent
        .post('/intake/message')
        .set('x-csrf-token', csrf)
        .send({ buildId: 'build-abc', message: bad });
      expect(res.status).toBe(400);
    }
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
  });

  it('answers 400 when buildId is not a string', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/intake/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ buildId: { $ne: null }, message: 'hello' });
    expect(res.status).toBe(400);
  });
});

describe('POST /intake/:buildId/logo', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  async function pngBytes(): Promise<Buffer> {
    const sharp = (await import('sharp')).default;
    return sharp({ create: { width: 40, height: 40, channels: 3, background: '#2563eb' } }).png().toBuffer();
  }

  it('rejects a file over 2 MB with a JSON 413, not the HTML error page', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/intake/build-logo/logo')
      .set('x-csrf-token', getValidCsrf())
      .attach('logo', Buffer.alloc(2 * 1024 * 1024 + 1024, 1), { filename: 'big.png', contentType: 'image/png' });
    expect(res.status).toBe(413);
    expect(res.headers['content-type']).toMatch(/json/);
    expect(res.body.error).toMatch(/2 MB/);
  });

  it('rejects a disallowed declared type with a JSON 400', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/intake/build-logo/logo')
      .set('x-csrf-token', getValidCsrf())
      .attach('logo', Buffer.from('<html></html>'), { filename: 'x.html', contentType: 'text/html' });
    expect(res.status).toBe(400);
    expect(res.headers['content-type']).toMatch(/json/);
  });

  it('rejects HTML bytes that claim to be a PNG (type is sniffed from the bytes)', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-logo', spec: null, status: 'intake' }] });
    const res = await agent
      .post('/intake/build-logo/logo')
      .set('x-csrf-token', getValidCsrf())
      .attach('logo', Buffer.from('<html><script>alert(1)</script></html>'), { filename: 'x.png', contentType: 'image/png' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
    // Nothing was written.
    expect(mockQuery.mock.calls.some((c: unknown[]) => String(c[0]).startsWith('UPDATE builds SET spec'))).toBe(false);
  });

  it('stores a sanitised filename (last path segment, control characters removed, max 255)', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-logo', spec: null, status: 'intake' }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const hostile = '../../etc/' + 'a'.repeat(400) + '.png';
    const res = await agent
      .post('/intake/build-logo/logo')
      .set('x-csrf-token', getValidCsrf())
      .attach('logo', await pngBytes(), { filename: hostile, contentType: 'image/png' });
    expect(res.status).toBe(200);
    const update = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).startsWith('UPDATE builds SET spec'));
    expect(update).toBeDefined();
    const storedName = (update![1] as string[])[1];
    expect(storedName).not.toContain('/');
    expect(storedName).not.toContain('..');
    expect(storedName.length).toBeLessThanOrEqual(255);
    expect((update![1] as string[])[2]).toBe('image/png');
  });
});
