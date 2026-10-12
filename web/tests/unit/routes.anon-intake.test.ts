import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import { AssistantNotConfiguredError } from '../../src/utils/assistant';
import type { Request } from 'express';

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

// Mock claude service.
vi.mock('../../src/services/claude', () => {
  class CircuitBreakerOpenError extends Error {
    constructor(message = 'Claude API circuit breaker is open.') {
      super(message);
      this.name = 'CircuitBreakerOpenError';
    }
  }
  return {
    processIntakeMessage: vi.fn(),
    generateInitialGreeting: vi.fn().mockResolvedValue('Hello! Describe your app.'),
    generateAnonSpecPreview: vi.fn().mockResolvedValue(null),
    ANON_STATIC_GREETING: 'Static greeting for tests.',
    processFeatureMessage: vi.fn(),
    generateFeatureGreeting: vi.fn(),
    CircuitBreakerOpenError,
  };
});

// Mock the anon-intake service layer. The route file imports
// generateAnonSessionId, createAnonDraft, getAnonDraftByBuildId.
vi.mock('../../src/services/anon-intake', () => ({
  generateAnonSessionId: vi.fn(() => 'a'.repeat(64)),
  createAnonDraft: vi.fn(),
  getAnonDraftByBuildId: vi.fn(),
  claimAnonDraft: vi.fn(),
  claimAnonDraftBySessionId: vi.fn(),
}));

// Mock services other routes depend on (loaded at app-import time).
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
import { anonymousCallsAdmitted, resetDailyCallBudget, tryAdmitAnonymousCalls } from '../../src/services/llm-budget';
import {
  processIntakeMessage,
  generateInitialGreeting,
} from '../../src/services/claude';
import {
  generateAnonSessionId,
  createAnonDraft,
  getAnonDraftByBuildId,
} from '../../src/services/anon-intake';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const mockProcessIntakeMessage = processIntakeMessage as ReturnType<typeof vi.fn>;
const mockGenerateInitialGreeting = generateInitialGreeting as ReturnType<typeof vi.fn>;
const mockGenerateAnonSessionId = generateAnonSessionId as ReturnType<typeof vi.fn>;
const mockCreateAnonDraft = createAnonDraft as ReturnType<typeof vi.fn>;
const mockGetAnonDraftByBuildId = getAnonDraftByBuildId as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

// Call counts are asserted ("called once", "not called"); clear them so calls
// made by whichever tests ran before are not counted.
beforeEach(() => {
  mockProcessIntakeMessage.mockClear();
  mockGenerateInitialGreeting.mockClear();
  mockCreateAnonDraft.mockClear();
});

async function startSession(): Promise<{ agent: supertest.Agent; buildId: string }> {
  const agent = supertest.agent(app);

  mockGenerateAnonSessionId.mockReturnValueOnce('s'.repeat(64));
  mockCreateAnonDraft.mockResolvedValueOnce({ buildId: 'build-anon-1' });
  // INSERT greeting message
  mockQuery.mockResolvedValueOnce({ rows: [] });

  const res = await agent
    .post('/try/start')
    .set('x-csrf-token', getValidCsrf())
    .send({});

  expect(res.status).toBe(200);
  return { agent, buildId: res.body.buildId };
}

describe('GET /try (Haven redirect when CAS_HARBOR_ENABLED=true)', () => {
  const originalFlag = process.env.CAS_HARBOR_ENABLED;
  beforeEach(() => { mockQuery.mockReset(); });
  afterEach(() => {
    if (originalFlag === undefined) delete process.env.CAS_HARBOR_ENABLED;
    else process.env.CAS_HARBOR_ENABLED = originalFlag;
  });

  it('redirects to /lighthouse/table when CAS_HARBOR_ENABLED=true', async () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const agent = supertest.agent(app);
    const res = await agent.get('/try');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/lighthouse/table');
  });

  it('redirects /try/:buildId to /lighthouse/table when flag is on', async () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const agent = supertest.agent(app);
    const res = await agent.get('/try/some-build-id');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/lighthouse/table');
  });

  it('serves the legacy intake page when CAS_HARBOR_ENABLED is unset', async () => {
    delete process.env.CAS_HARBOR_ENABLED;
    const agent = supertest.agent(app);
    const res = await agent.get('/try');
    // Legacy path renders the static-greeting intake-anon view (200, no redirect).
    expect(res.status).toBe(200);
    expect(res.text).toContain('Static greeting for tests');
  });
});

describe('POST /try/... when CAS_HARBOR_ENABLED=true: the intake is retired, so its POST routes are gone too', () => {
  // With Harbor on, GET /try redirects, so no page posts here. A hand-made
  // POST must not reach the model (two calls a turn) or the database.
  const originalFlag = process.env.CAS_HARBOR_ENABLED;
  beforeEach(() => {
    mockQuery.mockReset();
    resetDailyCallBudget();
  });
  afterEach(() => {
    if (originalFlag === undefined) delete process.env.CAS_HARBOR_ENABLED;
    else process.env.CAS_HARBOR_ENABLED = originalFlag;
    resetDailyCallBudget();
  });

  it('POST /try/start is answered 404 with where the conversation is now: no draft, no model call, no budget used', async () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const res = await supertest
      .agent(app)
      .post('/try/start')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'I need a site for my yoga studio' });
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: 'Not found.', redirect: '/lighthouse/table' });
    expect(mockCreateAnonDraft).not.toHaveBeenCalled();
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
    expect(mockGenerateInitialGreeting).not.toHaveBeenCalled();
    expect(mockQuery).not.toHaveBeenCalled();
    expect(anonymousCallsAdmitted()).toBe(0);
  });

  it('POST /try/:buildId/message is answered 404 even for a session that owns a live draft from before the switch', async () => {
    // The draft was started while the flag was off.
    delete process.env.CAS_HARBOR_ENABLED;
    const { agent, buildId } = await startSession();
    mockProcessIntakeMessage.mockClear();
    mockQuery.mockReset();
    mockGetAnonDraftByBuildId.mockClear();
    resetDailyCallBudget();

    process.env.CAS_HARBOR_ENABLED = 'true';
    const res = await agent
      .post(`/try/${buildId}/message`)
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'and a booking page' });
    expect(res.status).toBe(404);
    expect(res.body.redirect).toBe('/lighthouse/table');
    expect(mockGetAnonDraftByBuildId).not.toHaveBeenCalled();
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
    expect(mockQuery).not.toHaveBeenCalled();
    expect(anonymousCallsAdmitted()).toBe(0);
  });

  it('the refusal comes before the CSRF check and the limiters: it does not depend on a token', async () => {
    // With the flag off the same request passes the limiters (which stamp
    // their headers on it) and is then refused for its missing token.
    delete process.env.CAS_HARBOR_ENABLED;
    const counted = await supertest.agent(app).post('/try/start').send({ message: 'x' });
    expect(counted.status).toBe(403);
    expect(counted.headers['ratelimit-limit']).toBeDefined();

    process.env.CAS_HARBOR_ENABLED = 'true';
    const res = await supertest.agent(app).post('/try/start').send({ message: 'x' });
    expect(res.status).toBe(404);
    expect(res.headers['ratelimit-limit']).toBeUndefined();
  });
});

describe('anonymous turns are held to their share of the daily model-call budget', () => {
  const savedCap = process.env.CAS_LLM_DAILY_CALL_CAP;
  beforeEach(() => {
    mockQuery.mockReset();
    resetDailyCallBudget();
  });
  afterEach(() => {
    if (savedCap === undefined) delete process.env.CAS_LLM_DAILY_CALL_CAP;
    else process.env.CAS_LLM_DAILY_CALL_CAP = savedCap;
    resetDailyCallBudget();
  });

  it('POST /try/start with a first message asks for two calls, and is refused (503, nothing created) once the share is spent', async () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '5'; // anonymous share: 4 calls
    mockGenerateAnonSessionId.mockReturnValue('s'.repeat(64));
    mockCreateAnonDraft.mockResolvedValue({ buildId: 'build-anon-share' });
    mockProcessIntakeMessage.mockResolvedValue({ reply: 'Tell me more.', specComplete: false });
    mockQuery.mockResolvedValue({ rows: [] });

    for (let i = 0; i < 2; i += 1) {
      const ok = await supertest.agent(app).post('/try/start').set('x-csrf-token', getValidCsrf()).send({ message: 'a site for my studio' });
      expect(ok.status).toBe(200);
    }
    expect(anonymousCallsAdmitted()).toBe(4);

    mockCreateAnonDraft.mockClear();
    mockProcessIntakeMessage.mockClear();
    const refused = await supertest.agent(app).post('/try/start').set('x-csrf-token', getValidCsrf()).send({ message: 'a site for my studio' });
    expect(refused.status).toBe(503);
    expect(refused.body.error).toMatch(/high demand/i);
    expect(mockCreateAnonDraft).not.toHaveBeenCalled();
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
    // One call of the five is left in the pool, and only a caller with an account can take it.
    expect(tryAdmitAnonymousCalls(1)).toBe(false);
    mockGenerateAnonSessionId.mockReset();
    mockGenerateAnonSessionId.mockImplementation(() => 'a'.repeat(64));
    mockCreateAnonDraft.mockReset();
    mockProcessIntakeMessage.mockReset();
  });

  it('POST /try/:buildId/message is refused the same way, before the model is asked and before anything is stored', async () => {
    const { agent, buildId } = await startSession(); // one admission (a greeting)
    process.env.CAS_LLM_DAILY_CALL_CAP = '2'; // anonymous share: 1 call, already admitted above
    mockProcessIntakeMessage.mockClear();
    mockQuery.mockReset();
    mockGetAnonDraftByBuildId.mockResolvedValueOnce({ id: buildId, status: 'intake' });
    mockQuery.mockResolvedValueOnce({ rows: [{ role: 'assistant', content: 'Hello!', created_at: new Date() }] }); // history

    const res = await agent.post(`/try/${buildId}/message`).set('x-csrf-token', getValidCsrf()).send({ message: 'and a booking page' });
    expect(res.status).toBe(503);
    expect(res.body.error).toMatch(/high demand/i);
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
    expect(mockQuery.mock.calls.some(([sql]) => /INSERT INTO intake_messages/.test(String(sql)))).toBe(false);
  });
});

describe('POST /try/start', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockGenerateInitialGreeting.mockReset();
    mockGenerateInitialGreeting.mockResolvedValue('Hello! Describe your app.');
    mockGenerateAnonSessionId.mockReset();
    mockCreateAnonDraft.mockReset();
    mockGetAnonDraftByBuildId.mockReset();
  });

  it('creates a fresh draft and returns { buildId, greeting } on first call', async () => {
    const agent = supertest.agent(app);

    mockGenerateAnonSessionId.mockReturnValueOnce('s'.repeat(64));
    mockCreateAnonDraft.mockResolvedValueOnce({ buildId: 'build-new-1' });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT greeting

    const res = await agent
      .post('/try/start')
      .set('x-csrf-token', getValidCsrf())
      .send({});

    expect(res.status).toBe(200);
    expect(res.body.buildId).toBe('build-new-1');
    expect(res.body.greeting).toBe('Hello! Describe your app.');
    expect(mockCreateAnonDraft).toHaveBeenCalledOnce();
    expect(mockGenerateInitialGreeting).toHaveBeenCalledOnce();
  });

  it('returns the same draft idempotently when session already has one', async () => {
    const { agent, buildId } = await startSession();

    // Second call: route looks up the existing draft, then queries the
    // earliest assistant greeting row.
    mockGetAnonDraftByBuildId.mockResolvedValueOnce({
      id: buildId,
      anonymous_session_id: 's'.repeat(64),
      customer_id: null,
      status: 'intake',
      tier: 'build',
      created_at: new Date(),
    });
    mockQuery.mockResolvedValueOnce({
      rows: [{ content: 'Hello! Describe your app.' }],
    });

    const res = await agent
      .post('/try/start')
      .set('x-csrf-token', getValidCsrf())
      .send({});

    expect(res.status).toBe(200);
    expect(res.body.buildId).toBe(buildId);
    expect(res.body.greeting).toBe('Hello! Describe your app.');
    // No second createAnonDraft call: idempotent.
    expect(mockCreateAnonDraft).toHaveBeenCalledOnce();
  });

  it('uses static greeting (no API call) when a seed message is provided', async () => {
    const agent = supertest.agent(app);

    mockGenerateAnonSessionId.mockReturnValueOnce('s'.repeat(64));
    mockCreateAnonDraft.mockResolvedValueOnce({ buildId: 'build-seed-1' });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT greeting
    // Seed processing: processIntakeMessage + generateAnonSpecPreview
    mockProcessIntakeMessage.mockResolvedValueOnce({
      reply: 'Great idea!',
      specComplete: false,
      spec: null,
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT user msg
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT assistant reply

    const res = await agent
      .post('/try/start')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'I want a booking app' });

    expect(res.status).toBe(200);
    expect(res.body.buildId).toBe('build-seed-1');
    expect(res.body.seeded).toBe(true);
    // Static greeting used: no API call to generateInitialGreeting
    expect(mockGenerateInitialGreeting).not.toHaveBeenCalled();
    expect(mockProcessIntakeMessage).toHaveBeenCalledOnce();
  });

  it('returns 403 when CSRF token is missing', async () => {
    const res = await request(app).post('/try/start').send({});
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/CSRF/i);
    expect(mockCreateAnonDraft).not.toHaveBeenCalled();
  });

  it('returns 403 when CSRF token is tampered', async () => {
    const csrf = getValidCsrf();
    const tampered = csrf.slice(0, -4) + 'dead';
    const res = await request(app)
      .post('/try/start')
      .set('x-csrf-token', tampered)
      .send({});
    expect(res.status).toBe(403);
    expect(mockCreateAnonDraft).not.toHaveBeenCalled();
  });
});

describe('POST /try/:buildId/message', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockGenerateInitialGreeting.mockReset();
    mockGenerateInitialGreeting.mockResolvedValue('Hello! Describe your app.');
    mockGenerateAnonSessionId.mockReset();
    mockCreateAnonDraft.mockReset();
    mockGetAnonDraftByBuildId.mockReset();
    mockProcessIntakeMessage.mockReset();
  });

  it('returns a reply with turn metadata on a valid turn', async () => {
    const { agent, buildId } = await startSession();

    // getAnonDraftByBuildId -> valid intake draft
    mockGetAnonDraftByBuildId.mockResolvedValueOnce({
      id: buildId,
      anonymous_session_id: 's'.repeat(64),
      customer_id: null,
      status: 'intake',
      tier: 'build',
      created_at: new Date(),
    });
    // SELECT history (just the greeting)
    mockQuery.mockResolvedValueOnce({
      rows: [
        { role: 'assistant', content: 'Hello!', created_at: new Date() },
      ],
    });
    // INSERT user message
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // INSERT assistant reply
    mockQuery.mockResolvedValueOnce({ rows: [] });

    mockProcessIntakeMessage.mockResolvedValueOnce({
      reply: 'What problem does your app solve?',
      specComplete: false,
      spec: null,
    });

    const res = await agent
      .post(`/try/${buildId}/message`)
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'I want a booking app' });

    expect(res.status).toBe(200);
    expect(res.body.reply).toBe('What problem does your app solve?');
    expect(res.body.turnCount).toBe(1);
    // Cap raised 2026-04-28 from 3 → 100 since Haven at /lighthouse/table is
    // the canonical surface (no cap until email gate at turn 22). Anything
    // > 0 is acceptable; a 1-turn conversation is not near the cap.
    expect(res.body.turnsRemaining).toBeGreaterThan(0);
    expect(res.body.signupRequiredNext).toBe(false);
    expect(mockProcessIntakeMessage).toHaveBeenCalledOnce();
  });

  it('returns signupRequired:true at the turn cap and does NOT call Claude', async () => {
    const { agent, buildId } = await startSession();

    mockGetAnonDraftByBuildId.mockResolvedValueOnce({
      id: buildId,
      anonymous_session_id: 's'.repeat(64),
      customer_id: null,
      status: 'intake',
      tier: 'build',
      created_at: new Date(),
    });
    // History at the legacy cap (100 prior user turns). The cap is huge by
    // design now: Haven is the canonical surface; the legacy /try cap exists
    // only as a backstop. Build the seeded turns programmatically so the cap
    // value can change without re-keying every row.
    const ANON_CAP_TEST_VALUE = 100;
    const seededRows: { role: string; content: string; created_at: Date }[] = [
      { role: 'assistant', content: 'Hello!', created_at: new Date(0) },
    ];
    for (let i = 1; i <= ANON_CAP_TEST_VALUE; i++) {
      seededRows.push({ role: 'user', content: `turn ${i}`, created_at: new Date(i * 2) });
      seededRows.push({ role: 'assistant', content: `a${i}`, created_at: new Date(i * 2 + 1) });
    }
    mockQuery.mockResolvedValueOnce({ rows: seededRows });

    const res = await agent
      .post(`/try/${buildId}/message`)
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'one more please' });

    expect(res.status).toBe(200);
    expect(res.body.signupRequired).toBe(true);
    expect(res.body.buildId).toBe(buildId);
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
  });

  it('returns 404 when the draft is not found for this session', async () => {
    const { agent, buildId } = await startSession();
    mockGetAnonDraftByBuildId.mockResolvedValueOnce(null);

    const res = await agent
      .post(`/try/${buildId}/message`)
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'hello' });

    expect(res.status).toBe(404);
    // The lookup is scoped by the id this session was given at /try/start, not by anything the request supplies.
    expect(mockGetAnonDraftByBuildId).toHaveBeenCalledWith(buildId, 's'.repeat(64));
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
  });

  it('returns 401 when no anon session cookie is present', async () => {
    const res = await request(app)
      .post('/try/build-unknown/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'hello' });

    expect(res.status).toBe(401);
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
  });

  it('returns 403 when CSRF token is missing on a message', async () => {
    const { agent, buildId } = await startSession();

    const res = await agent
      .post(`/try/${buildId}/message`)
      .send({ message: 'hello' });

    expect(res.status).toBe(403);
    expect(mockProcessIntakeMessage).not.toHaveBeenCalled();
  });

  it('truncates message payload to 1000 chars before sending to Claude', async () => {
    const { agent, buildId } = await startSession();

    mockGetAnonDraftByBuildId.mockResolvedValueOnce({
      id: buildId,
      anonymous_session_id: 's'.repeat(64),
      customer_id: null,
      status: 'intake',
      tier: 'build',
      created_at: new Date(),
    });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // history
    mockQuery.mockResolvedValueOnce({ rows: [] }); // insert user
    mockQuery.mockResolvedValueOnce({ rows: [] }); // insert assistant

    mockProcessIntakeMessage.mockResolvedValueOnce({
      reply: 'ok',
      specComplete: false,
      spec: null,
    });

    const longMessage = 'x'.repeat(5000);
    const res = await agent
      .post(`/try/${buildId}/message`)
      .set('x-csrf-token', getValidCsrf())
      .send({ message: longMessage });

    expect(res.status).toBe(200);
    expect(mockProcessIntakeMessage).toHaveBeenCalledOnce();
    const firstArg = mockProcessIntakeMessage.mock.calls[0][0] as string;
    expect(firstArg.length).toBe(1000);
  });
});

describe('no model key on the server: the visitor is told so, and nothing is silently dropped', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockGenerateInitialGreeting.mockReset();
    mockGenerateInitialGreeting.mockResolvedValue('Hello! Describe your app.');
    mockGenerateAnonSessionId.mockReset();
    mockCreateAnonDraft.mockReset();
    mockGetAnonDraftByBuildId.mockReset();
    mockProcessIntakeMessage.mockReset();
  });

  it('POST /try/start with a first message answers 503 with the explanation (the page shows it instead of navigating)', async () => {
    const agent = supertest.agent(app);
    mockGenerateAnonSessionId.mockReturnValueOnce('s'.repeat(64));
    mockCreateAnonDraft.mockResolvedValueOnce({ buildId: 'build-nokey-1' });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT greeting
    mockProcessIntakeMessage.mockRejectedValueOnce(new AssistantNotConfiguredError());
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await agent
        .post('/try/start')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: 'I run a small yoga studio and need a website' });

      expect(res.status).toBe(503);
      expect(res.body.code).toBe('assistant_not_configured');
      expect(res.body.error).toMatch(/assistant is not configured on this server/i);
      expect(res.body.buildId).toBeUndefined();
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('POST /try/:buildId/message answers 503 with the explanation, not a generic 500', async () => {
    const { agent, buildId } = await startSession();
    mockGetAnonDraftByBuildId.mockResolvedValueOnce({
      id: buildId,
      anonymous_session_id: 's'.repeat(64),
      customer_id: null,
      status: 'intake',
      tier: 'build',
      created_at: new Date(),
    });
    mockQuery.mockResolvedValueOnce({ rows: [{ role: 'assistant', content: 'Hello!', created_at: new Date() }] });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT user message
    mockProcessIntakeMessage.mockRejectedValueOnce(new AssistantNotConfiguredError());
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await agent
        .post(`/try/${buildId}/message`)
        .set('x-csrf-token', getValidCsrf())
        .send({ message: 'I want a booking app' });

      expect(res.status).toBe(503);
      expect(res.body.code).toBe('assistant_not_configured');
      expect(res.body.error).toMatch(/ANTHROPIC_API_KEY/);
    } finally {
      errorSpy.mockRestore();
    }
  });
});
