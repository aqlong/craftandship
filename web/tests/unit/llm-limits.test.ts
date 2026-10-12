// Bounds on model spend: what stops any one route, account, address, or all
// of them together from calling a paid model without limit.
//
//   services/llm-budget.ts        process-wide daily call budget, hard turn caps
//   middleware/llm-rate-limit.ts  per-customer daily request ceiling
//   middleware/anon-rate-limit.ts per-address and shared daily ceilings for /try
//
// The route tests at the bottom check the wiring: each authenticated route
// that can cause a model call answers with the daily policy, and each chat
// refuses a conversation that has reached its hard length before the model
// is called.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import express from 'express';
import type { Request } from 'express';
import request from 'supertest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return { default: { query: mockQuery, on: vi.fn() } };
});

const claudeMock = vi.hoisted(() => ({
  processIntakeMessage: vi.fn(),
  processRevisionMessage: vi.fn(),
  processFeatureMessage: vi.fn(),
  generateInitialGreeting: vi.fn(),
  generateRevisionGreeting: vi.fn(),
  generateFeatureGreeting: vi.fn(),
  generateAnonSpecPreview: vi.fn(),
}));

vi.mock('../../src/services/claude', () => ({
  ...claudeMock,
  analyzeBuildFailure: vi.fn(),
  ANON_STATIC_GREETING: 'hello',
  CircuitBreakerOpenError: class CircuitBreakerOpenError extends Error {},
}));

vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn(() => 'token'),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));
vi.mock('../../src/services/stripe', () => ({
  cancelHarborSubscriptionAtPeriodEnd: vi.fn(),
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import {
  DAILY_CALL_BUDGET_DEFAULT,
  dailyCallBudget,
  dailyCallAvailable,
  dailyCallsUsed,
  tryConsumeDailyCall,
  resetDailyCallBudget,
  INTAKE_HARD_TURN_CAP,
  REVISION_HARD_TURN_CAP,
  FEATURE_HARD_TURN_CAP,
} from '../../src/services/llm-budget';
import {
  CUSTOMER_LLM_DAILY_MAX,
  customerLlmKey,
  makeCustomerLlmDailyLimit,
} from '../../src/middleware/llm-rate-limit';
import {
  ANON_DAILY_TURNS_PER_ADDRESS,
  ANON_DAILY_TURNS_TOTAL,
  makeAnonDailyTurnLimit,
} from '../../src/middleware/anon-rate-limit';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const csrf = (): string => generateCsrfToken({} as Request);
const DAY_SECONDS = 24 * 60 * 60;
const DAILY_POLICY = `${CUSTOMER_LLM_DAILY_MAX};w=${DAY_SECONDS}`;

let customerSeq = 0;
/** Log in as a fresh customer (fresh limiter buckets for every test). */
async function loginAsNewCustomer(): Promise<{ agent: ReturnType<typeof supertest.agent>; customerId: string }> {
  customerSeq += 1;
  const customerId = `limit-customer-${customerSeq}`;
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: customerId, email: `${customerId}@example.com`, name: 'L', password: hashed, email_verified: true }],
  });
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf(), email: `${customerId}@example.com`, password: 'password123' });
  return { agent, customerId };
}

/** A conversation with `turns` user messages, each answered. */
function conversation(turns: number): Array<{ role: string; content: string; created_at: Date }> {
  const rows: Array<{ role: string; content: string; created_at: Date }> = [];
  for (let i = 0; i < turns; i += 1) {
    rows.push({ role: 'user', content: `message ${i}`, created_at: new Date() });
    rows.push({ role: 'assistant', content: `reply ${i}`, created_at: new Date() });
  }
  return rows;
}

describe('process-wide daily call budget (services/llm-budget.ts)', () => {
  beforeEach(() => {
    delete process.env.CAS_LLM_DAILY_CALL_CAP;
    resetDailyCallBudget();
  });
  afterEach(() => {
    delete process.env.CAS_LLM_DAILY_CALL_CAP;
    resetDailyCallBudget();
  });

  it('defaults to a finite budget and honours CAS_LLM_DAILY_CALL_CAP, including 0', () => {
    expect(dailyCallBudget()).toBe(DAILY_CALL_BUDGET_DEFAULT);
    expect(Number.isFinite(DAILY_CALL_BUDGET_DEFAULT)).toBe(true);
    process.env.CAS_LLM_DAILY_CALL_CAP = '7';
    expect(dailyCallBudget()).toBe(7);
    process.env.CAS_LLM_DAILY_CALL_CAP = '0';
    expect(dailyCallBudget()).toBe(0);
    expect(tryConsumeDailyCall()).toBe(false);
    process.env.CAS_LLM_DAILY_CALL_CAP = 'not-a-number';
    expect(dailyCallBudget()).toBe(DAILY_CALL_BUDGET_DEFAULT);
    process.env.CAS_LLM_DAILY_CALL_CAP = '-5';
    expect(dailyCallBudget()).toBe(DAILY_CALL_BUDGET_DEFAULT);
  });

  it('hands out exactly the budget, then refuses without counting the refusals', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '3';
    expect([tryConsumeDailyCall(), tryConsumeDailyCall(), tryConsumeDailyCall()]).toEqual([true, true, true]);
    expect(dailyCallAvailable()).toBe(false);
    expect(tryConsumeDailyCall()).toBe(false);
    expect(tryConsumeDailyCall()).toBe(false);
    expect(dailyCallsUsed()).toBe(3);
  });

  it('starts a new window after 24 hours', () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '1';
    const start = Date.now();
    expect(tryConsumeDailyCall(start)).toBe(true);
    expect(tryConsumeDailyCall(start + 23 * 60 * 60 * 1000)).toBe(false);
    expect(dailyCallAvailable(start + 24 * 60 * 60 * 1000)).toBe(true);
    expect(tryConsumeDailyCall(start + 24 * 60 * 60 * 1000)).toBe(true);
    expect(dailyCallsUsed()).toBe(1);
  });
});

describe('the budget gates every call in services/claude.ts', () => {
  beforeEach(() => {
    vi.resetModules();
    delete process.env.CAS_LLM_DAILY_CALL_CAP;
  });
  afterEach(() => {
    vi.doUnmock('@anthropic-ai/sdk');
    vi.doUnmock('../../src/services/claude');
    delete process.env.CAS_LLM_DAILY_CALL_CAP;
    delete process.env.ANTHROPIC_API_KEY;
  });

  it('refuses the call after the last one in the budget, and never reaches the SDK', async () => {
    const create = vi.fn().mockResolvedValue({ content: [{ type: 'text', text: 'hello there' }] });
    vi.doMock('@anthropic-ai/sdk', () => ({
      default: function AnthropicMock() {
        return { messages: { create } };
      },
    }));
    vi.doUnmock('../../src/services/claude');
    process.env.CAS_LLM_DAILY_CALL_CAP = '2';
    process.env.ANTHROPIC_API_KEY = 'test-key';
    const budget = await import('../../src/services/llm-budget');
    budget.resetDailyCallBudget();
    const claude = await import('../../src/services/claude');

    await claude.processIntakeMessage('one', [], 0);
    await claude.processIntakeMessage('two', [], 1);
    await expect(claude.processIntakeMessage('three', [], 2)).rejects.toBeInstanceOf(
      claude.CircuitBreakerOpenError
    );
    expect(create).toHaveBeenCalledTimes(2);
    // Helpers that degrade instead of throwing fall back without a call.
    expect(await claude.generateAnonSpecPreview([{ role: 'user', content: 'x', created_at: new Date() } as never])).toBeNull();
    expect(typeof (await claude.generateInitialGreeting())).toBe('string');
    expect(create).toHaveBeenCalledTimes(2);
    // An exhausted budget is not an unhealthy API: the breaker stays closed.
    expect(claude.getCircuitState()).toBe('closed');
  });
});

describe('customer daily ceiling (middleware/llm-rate-limit.ts)', () => {
  function appWithLimit(max: number) {
    const mini = express();
    mini.use((req, _res, next) => {
      const who = req.headers['x-customer'];
      (req as unknown as { session: { customerId?: string } }).session = {
        customerId: typeof who === 'string' ? who : undefined,
      };
      next();
    });
    mini.post('/chat', makeCustomerLlmDailyLimit({ max }), (_req, res) => res.json({ ok: true }));
    return mini;
  }

  it('counts per customer, across requests, and answers a script 429 JSON when spent', async () => {
    // The chat endpoints are called with a JSON body; that is what makes the
    // refusal JSON (isJsonRequest). A form or a page gets the 429 page:
    // views.error-pages.test.ts and utils.json-request.test.ts.
    const mini = appWithLimit(2);
    const chat = (who: string) => request(mini).post('/chat').set('x-customer', who).send({ message: 'hello' });
    expect((await chat('a')).status).toBe(200);
    expect((await chat('a')).status).toBe(200);
    const third = await chat('a');
    expect(third.status).toBe(429);
    expect(third.headers['content-type']).toMatch(/^application\/json/);
    expect(third.body.error).toMatch(/daily limit/);
    // Another customer has their own budget.
    expect((await chat('b')).status).toBe(200);
  });

  it('keys on the customer id, falling back to the client address', () => {
    expect(customerLlmKey({ session: { customerId: 'c-1' }, ip: '203.0.113.9' } as unknown as Request)).toBe('customer:c-1');
    expect(customerLlmKey({ session: {}, ip: '203.0.113.9' } as unknown as Request)).toBe('ip:203.0.113.9');
  });

  it('is far below what the per-minute limiters alone would allow', () => {
    // 30 requests a minute is 43,200 a day; the daily ceiling must bite well before that.
    expect(CUSTOMER_LLM_DAILY_MAX).toBeLessThanOrEqual(500);
    expect(CUSTOMER_LLM_DAILY_MAX).toBeGreaterThanOrEqual(60);
  });
});

describe('anonymous daily ceilings (middleware/anon-rate-limit.ts)', () => {
  function appWith(limit: ReturnType<typeof makeAnonDailyTurnLimit>) {
    const mini = express();
    mini.set('trust proxy', 1);
    mini.post('/turn', limit, (_req, res) => res.json({ ok: true }));
    return mini;
  }

  it('the per-address bucket is separate for each address', async () => {
    const mini = appWith(makeAnonDailyTurnLimit({ max: 1, shared: false }));
    expect((await request(mini).post('/turn').set('X-Forwarded-For', '198.51.100.1')).status).toBe(200);
    expect((await request(mini).post('/turn').set('X-Forwarded-For', '198.51.100.1')).status).toBe(429);
    expect((await request(mini).post('/turn').set('X-Forwarded-For', '198.51.100.2')).status).toBe(200);
  });

  it('the shared bucket is one budget for every address', async () => {
    const mini = appWith(makeAnonDailyTurnLimit({ max: 2, shared: true }));
    expect((await request(mini).post('/turn').set('X-Forwarded-For', '198.51.100.1')).status).toBe(200);
    expect((await request(mini).post('/turn').set('X-Forwarded-For', '198.51.100.2')).status).toBe(200);
    const third = await request(mini).post('/turn').set('X-Forwarded-For', '198.51.100.3');
    expect(third.status).toBe(429);
    expect(third.body.error).toMatch(/Sign up/);
  });

  it('the production numbers are finite and the shared one is the larger', () => {
    // > 0 alone would accept Infinity, i.e. no cap at all.
    expect(Number.isFinite(ANON_DAILY_TURNS_PER_ADDRESS)).toBe(true);
    expect(Number.isFinite(ANON_DAILY_TURNS_TOTAL)).toBe(true);
    expect(ANON_DAILY_TURNS_PER_ADDRESS).toBeGreaterThan(0);
    expect(ANON_DAILY_TURNS_TOTAL).toBeGreaterThan(ANON_DAILY_TURNS_PER_ADDRESS);
  });
});

describe('wiring: every authenticated model-backed route carries the daily policy', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    Object.values(claudeMock).forEach((fn) => fn.mockReset());
    claudeMock.generateInitialGreeting.mockResolvedValue('Hello.');
    claudeMock.generateFeatureGreeting.mockResolvedValue('Hello.');
    claudeMock.processIntakeMessage.mockResolvedValue({ reply: 'ok', specComplete: false, spec: null });
    claudeMock.processRevisionMessage.mockResolvedValue({ reply: 'ok', revisionReady: false, revisionSummary: null });
    claudeMock.processFeatureMessage.mockResolvedValue({ reply: 'ok', scopeComplete: false, scope: null });
    delete process.env.CAS_HARBOR_ENABLED;
  });

  it('GET /intake', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValue({ rows: [{ id: 'b-1' }] });
    const res = await agent.get('/intake');
    expect(res.status).toBe(302);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('POST /intake/start', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValue({ rows: [{ id: 'b-1' }] });
    const res = await agent.post('/intake/start').set('x-csrf-token', csrf()).send({ message: 'a site for my studio' });
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('POST /intake/message', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', status: 'intake' }] });
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await agent.post('/intake/message').set('x-csrf-token', csrf()).send({ buildId: 'b-1', message: 'hello' });
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('POST /builds/:id/revision/message', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', tier: 'build', spec: {}, review_notes: null }] });
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await agent.post('/builds/b-1/revision/message').set('x-csrf-token', csrf()).send({ message: 'hello' });
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('POST /projects/:id/features/new', async () => {
    const { agent, customerId } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', spec: {}, customer_id: customerId }] });
    mockQuery.mockResolvedValue({ rows: [{ id: 'f-1' }] });
    const res = await agent.post('/projects/b-1/features/new').type('form').send({ _csrf: csrf() });
    expect(res.status).toBe(302);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('POST /features/:id/message', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'f-1', status: 'drafting', spec: {} }] });
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await agent.post('/features/f-1/message').set('x-csrf-token', csrf()).send({ message: 'hello' });
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('POST /api/support/chat', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValue({ rows: [] });
    // No ANTHROPIC_API_KEY in the test environment: the route answers with its
    // fallback text, after passing through the limiter.
    const res = await agent.post('/api/support/chat').set('x-csrf-token', csrf()).send({ message: 'hello there' });
    expect(res.status).toBe(200);
    expect(res.headers['ratelimit-policy']).toBe(DAILY_POLICY);
  });

  it('the /intake redirect to Haven is not counted against the budget', async () => {
    const { agent } = await loginAsNewCustomer();
    process.env.CAS_HARBOR_ENABLED = 'true';
    try {
      const res = await agent.get('/intake');
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/lighthouse/table');
      expect(res.headers['ratelimit-policy']).toBeUndefined();
      expect(claudeMock.generateInitialGreeting).not.toHaveBeenCalled();
    } finally {
      delete process.env.CAS_HARBOR_ENABLED;
    }
  });
});

describe('hard conversation length: refused before the model is called', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    Object.values(claudeMock).forEach((fn) => fn.mockReset());
  });

  it('intake: the turn after INTAKE_HARD_TURN_CAP answers 409', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', status: 'intake' }] });
    mockQuery.mockResolvedValueOnce({ rows: conversation(INTAKE_HARD_TURN_CAP) });
    const res = await agent.post('/intake/message').set('x-csrf-token', csrf()).send({ buildId: 'b-1', message: 'more' });
    expect(res.status).toBe(409);
    expect(res.body.error).toMatch(/maximum length/);
    expect(claudeMock.processIntakeMessage).not.toHaveBeenCalled();
    // Nothing was appended to the conversation either.
    expect(mockQuery.mock.calls.some(([sql]) => /INSERT INTO intake_messages/.test(String(sql)))).toBe(false);
  });

  it('intake: one turn below the cap still goes through', async () => {
    const { agent } = await loginAsNewCustomer();
    claudeMock.processIntakeMessage.mockResolvedValue({ reply: 'ok', specComplete: false, spec: null });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', status: 'intake' }] });
    mockQuery.mockResolvedValueOnce({ rows: conversation(INTAKE_HARD_TURN_CAP - 1) });
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await agent.post('/intake/message').set('x-csrf-token', csrf()).send({ buildId: 'b-1', message: 'more' });
    expect(res.status).toBe(200);
    expect(claudeMock.processIntakeMessage).toHaveBeenCalledTimes(1);
  });

  it('revision: the turn after REVISION_HARD_TURN_CAP answers 409', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', tier: 'build', spec: {}, review_notes: null }] });
    mockQuery.mockResolvedValueOnce({ rows: conversation(REVISION_HARD_TURN_CAP) });
    const res = await agent.post('/builds/b-1/revision/message').set('x-csrf-token', csrf()).send({ message: 'more' });
    expect(res.status).toBe(409);
    expect(claudeMock.processRevisionMessage).not.toHaveBeenCalled();
  });

  it('feature chat: the turn after FEATURE_HARD_TURN_CAP answers 409', async () => {
    const { agent } = await loginAsNewCustomer();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'f-1', status: 'drafting', spec: {} }] });
    mockQuery.mockResolvedValueOnce({ rows: conversation(FEATURE_HARD_TURN_CAP) });
    const res = await agent.post('/features/f-1/message').set('x-csrf-token', csrf()).send({ message: 'more' });
    expect(res.status).toBe(409);
    expect(claudeMock.processFeatureMessage).not.toHaveBeenCalled();
  });
});
