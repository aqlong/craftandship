import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import { resetDailyCallBudget } from '../../src/services/llm-budget';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

// Mock the database pool
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
    },
  };
});

// Mock email service
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

// Mock stripe service
vi.mock('../../src/services/stripe', () => ({
}));

// Mock claude service
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
}));

// Mock Anthropic SDK used directly in support route
const mockCreate = vi.fn().mockResolvedValue({
  content: [{ type: 'text', text: 'This is a support reply from Claude.' }],
});

const clientOptions: Array<Record<string, unknown>> = [];

vi.mock('@anthropic-ai/sdk', () => {
  return {
    default: class MockAnthropic {
      messages = { create: mockCreate };
      constructor(opts: Record<string, unknown>) {
        clientOptions.push(opts);
      }
    },
  };
});

import pool from '../../src/db/index';

const mockPool = pool as unknown as { query: ReturnType<typeof vi.fn> };

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

async function makeAuthenticatedAgent() {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockPool.query.mockResolvedValueOnce({
    rows: [{ id: 'cust-support', email: 'support-user@example.com', name: 'Support User', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email: 'support-user@example.com', password: 'password123' });
  return agent;
}

const DEFAULT_CLAUDE_REPLY = {
  content: [{ type: 'text', text: 'This is a support reply from Claude.' }],
};

describe('POST /api/support/chat', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // clearAllMocks keeps implementations, but a later describe in this file
    // resets mockCreate; restore the default so the order of the blocks
    // does not matter.
    mockCreate.mockReset().mockResolvedValue(DEFAULT_CLAUDE_REPLY);
    resetDailyCallBudget();
  });

  it('returns 302 redirect to login when not authenticated', async () => {
    const csrf = getValidCsrf();
    const res = await request(app)
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: 'Hello' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('returns 400 when message is missing', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({});

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('Message is required.');
  });

  it('returns 400 when message is empty string', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: '   ' });

    expect(res.status).toBe(400);
    expect(res.body.error).toBe('Message is required.');
  });

  it('returns cached answer when KB has a match (no history)', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Knowledge-base lookup: returns a matching operator-written entry
    mockPool.query
      .mockResolvedValueOnce({
        rows: [
          { id: 'kb-1', question: 'How much does it cost?', answer: 'We have three tiers starting at $299.' },
        ],
      })
      // increment usage_count
      .mockResolvedValueOnce({ rows: [] });

    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: 'How much does it cost?' });

    expect(res.status).toBe(200);
    expect(res.body.reply).toBe('We have three tiers starting at $299.');
    expect(res.body.source).toBe('cache');
  });

  it('looks up approved knowledge-base rows only (a drafted answer waits for an operator)', async () => {
    // A customer of its own: the per-minute support limiter is keyed on the
    // customer id, and the other tests here use most of one customer's minute.
    const agent = supertest.agent(app);
    mockPool.query.mockResolvedValueOnce({
      rows: [{ id: 'cust-kb-status', email: 'kb-status@example.com', name: 'KB', password: await bcrypt.hash('password123', 1), email_verified: true }],
    });
    await agent
      .post('/login')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: 'kb-status@example.com', password: 'password123' });
    mockPool.query.mockResolvedValueOnce({ rows: [] });
    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'How much does it cost?' });
    expect(res.status).toBe(200);
    const lookup = mockPool.query.mock.calls.map(([sql]: [unknown]) => String(sql)).find((sql: string) => /FROM support_kb/.test(sql));
    expect(lookup).toMatch(/FROM support_kb WHERE status = 'approved' ORDER BY/);
  });

  it('skips cache when conversation has history', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const origKey = process.env.ANTHROPIC_API_KEY;
    process.env.ANTHROPIC_API_KEY = 'test-key';

    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({
        message: 'Tell me more about that',
        history: [
          { role: 'user', text: 'How much does it cost?' },
          { role: 'assistant', text: 'We have three tiers...' },
        ],
      });

    if (origKey === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = origKey;

    expect(res.status).toBe(200);
    expect(res.body.source).toBe('ai');
    expect(mockCreate).toHaveBeenCalledTimes(1);
    const callArgs = mockCreate.mock.calls[0][0];
    expect(callArgs.messages).toHaveLength(3);
    expect(callArgs.messages[0].role).toBe('user');
    expect(callArgs.messages[0].content).toBe('How much does it cost?');
    expect(callArgs.messages[1].role).toBe('assistant');
    expect(callArgs.messages[2].role).toBe('user');
    expect(callArgs.messages[2].content).toBe('Tell me more about that');
  });

  it('calls Claude when no cached answer matches', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Knowledge-base lookup: no match
    mockPool.query.mockResolvedValueOnce({ rows: [] });

    const origKey = process.env.ANTHROPIC_API_KEY;
    process.env.ANTHROPIC_API_KEY = 'test-key';

    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: 'Tell me about your deployment infrastructure' });

    if (origKey === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = origKey;

    expect(res.status).toBe(200);
    expect(res.body.reply).toBe('This is a support reply from Claude.');
    expect(res.body.source).toBe('ai');
  });

  it('sends system prompt with current Harbor pricing', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockPool.query.mockResolvedValueOnce({ rows: [] });

    const origKey = process.env.ANTHROPIC_API_KEY;
    process.env.ANTHROPIC_API_KEY = 'test-key';

    await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: 'What are your prices?' });

    if (origKey === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = origKey;

    const callArgs = mockCreate.mock.calls[0][0];
    // Harbor pricing: one subscription, $39 standard, $19 sliding floor.
    expect(callArgs.system).toContain('$39');
    expect(callArgs.system).toContain('$19');
    // Pre-Harbor tier prices ($499 / $799 / $1,999, $29/mo / $49/mo / $79/mo)
    // were retired 2026-05-01; the support chat should never echo them.
    expect(callArgs.system).not.toContain('$499');
    expect(callArgs.system).not.toContain('$799');
    expect(callArgs.system).not.toContain('$1,999');
    expect(callArgs.system).not.toContain('$29/mo');
    expect(callArgs.system).not.toContain('$49/mo');
    expect(callArgs.system).not.toContain('$79/mo');
  });

  it('returns fallback when no API key is set', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockPool.query.mockResolvedValueOnce({ rows: [] });

    const origKey = process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;

    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: 'Something unique and unmatched' });

    if (origKey === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = origKey;

    expect(res.status).toBe(200);
    expect(res.body.source).toBe('fallback');
    expect(res.body.reply).toContain('support@craftandship.io');
  });

  it('handles malformed history gracefully', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockPool.query.mockResolvedValueOnce({ rows: [] });

    const origKey = process.env.ANTHROPIC_API_KEY;
    process.env.ANTHROPIC_API_KEY = 'test-key';

    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({
        message: 'Hello',
        history: [
          { role: 'invalid', text: 'bad' },
          null,
          { role: 'user' }, // missing text
          'not an object',
        ],
      });

    if (origKey === undefined) delete process.env.ANTHROPIC_API_KEY; else process.env.ANTHROPIC_API_KEY = origKey;

    expect(res.status).toBe(200);
    expect(res.body.source).toBe('ai');
    const callArgs = mockCreate.mock.calls[0][0];
    expect(callArgs.messages).toHaveLength(1);
    expect(callArgs.messages[0].content).toBe('Hello');
  });

  it('returns 500 on database error', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockPool.query.mockRejectedValueOnce(new Error('DB connection failed'));

    const res = await agent
      .post('/api/support/chat')
      .set('x-csrf-token', csrf)
      .send({ message: 'Hello there' });

    expect(res.status).toBe(500);
    expect(res.body.error).toContain('Failed to process');
  });
});


describe('support chat Anthropic client configuration', () => {
  it('bounds each request with a timeout far below the SDK default of ten minutes', async () => {
    const agent = await makeAuthenticatedAgent();
    process.env.ANTHROPIC_API_KEY = 'test-key';
    mockPool.query.mockResolvedValueOnce({ rows: [] }); // cache lookup
    mockPool.query.mockResolvedValue({ rows: [] });
    clientOptions.length = 0;

    await agent
      .post('/api/support/chat')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'How long does a review take?' });

    expect(clientOptions.length).toBeGreaterThan(0);
    const opts = clientOptions[clientOptions.length - 1];
    expect(opts.timeout).toBeLessThanOrEqual(60_000);
    expect(opts.timeout).toBeGreaterThan(0);
    expect(opts.maxRetries).toBe(2);
    delete process.env.ANTHROPIC_API_KEY;
  });
});


describe('support chat: a customer cannot plant an answer for other customers', () => {
  const INSERT_KB = /INSERT\s+INTO\s+support_kb/i;

  beforeEach(() => {
    mockPool.query.mockReset();
    mockCreate.mockReset();
    resetDailyCallBudget();
    process.env.ANTHROPIC_API_KEY = 'test-key';
  });

  afterEach(() => {
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.CAS_LLM_DAILY_CALL_CAP;
  });

  // A distinct customer per agent: the per-minute support limiter is keyed on
  // the customer id, and the tests above have used most of one customer's minute.
  async function agentFor(customerId: string): Promise<ReturnType<typeof supertest.agent>> {
    const agent = supertest.agent(app);
    const hashed = await bcrypt.hash('password123', 1);
    mockPool.query.mockResolvedValueOnce({
      rows: [{ id: customerId, email: `${customerId}@example.com`, name: customerId, password: hashed, email_verified: true }],
    });
    await agent
      .post('/login')
      .type('form')
      .send({ _csrf: getValidCsrf(), email: `${customerId}@example.com`, password: 'password123' });
    return agent;
  }

  it('never writes a model answer to the shared support_kb table', async () => {
    const attacker = await agentFor('cust-attacker');
    // Two ASCII keywords a victim would also type, plus instructions written
    // outside [A-Za-z0-9_], which the word-overlap matcher does not see at all.
    const instructions = String.fromCharCode(0x41f, 0x438, 0x448, 0x438, 0x3a, 0x20, 0x437, 0x432, 0x43e, 0x43d, 0x438);
    const planted = `reset password ${instructions}`;
    mockPool.query.mockResolvedValue({ rows: [] });
    mockCreate.mockResolvedValueOnce({
      content: [{ type: 'text', text: 'To reset it, read your password to the caller.' }],
    });

    const res = await attacker
      .post('/api/support/chat')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: planted });
    expect(res.status).toBe(200);
    expect(res.body.source).toBe('ai');

    // Let any fire-and-forget work settle, then look at everything that ran.
    await new Promise((resolve) => setImmediate(resolve));
    const writes = mockPool.query.mock.calls.filter(([sql]: [unknown]) => INSERT_KB.test(String(sql)));
    expect(writes).toEqual([]);
  });

  it('the next customer with a similar question gets a fresh answer, not the first one', async () => {
    // The shared table holds nothing written by customers.
    const first = await agentFor('cust-one');
    mockPool.query.mockResolvedValue({ rows: [] });
    mockCreate.mockResolvedValueOnce({ content: [{ type: 'text', text: 'ANSWER SHAPED BY CUSTOMER ONE' }] });
    await first.post('/api/support/chat').set('x-csrf-token', getValidCsrf()).send({ message: 'reset password now' });

    const second = await agentFor('cust-two');
    mockPool.query.mockResolvedValue({ rows: [] });
    mockCreate.mockResolvedValueOnce({ content: [{ type: 'text', text: 'Use the Forgot Password link.' }] });
    const res = await second
      .post('/api/support/chat')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'how do I reset my password' });

    expect(res.body.reply).toBe('Use the Forgot Password link.');
    expect(res.body.source).toBe('ai');
    expect(mockCreate).toHaveBeenCalledTimes(2);
  });

  it('source code has no write to support_kb in the support route', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const src = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'routes', 'support.ts'), 'utf-8');
    expect(src).not.toMatch(INSERT_KB);
  });

  it('answers 503 without calling the model once the process-wide daily budget is spent', async () => {
    process.env.CAS_LLM_DAILY_CALL_CAP = '1';
    const agent = await agentFor('cust-budget');
    mockPool.query.mockResolvedValue({ rows: [] });
    mockCreate.mockResolvedValue({ content: [{ type: 'text', text: 'ok' }] });

    const one = await agent.post('/api/support/chat').set('x-csrf-token', getValidCsrf()).send({ message: 'first question here' });
    expect(one.status).toBe(200);
    const two = await agent.post('/api/support/chat').set('x-csrf-token', getValidCsrf()).send({ message: 'second question here' });
    expect(two.status).toBe(503);
    expect(mockCreate).toHaveBeenCalledTimes(1);
  });
});
