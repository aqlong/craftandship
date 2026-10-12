import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import bcrypt from 'bcrypt';
import app from '../../src/app';
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

vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
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
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
  processFeatureMessage: vi.fn().mockResolvedValue({ reply: 'Got it!', scopeComplete: false }),
  generateFeatureGreeting: vi.fn().mockResolvedValue('What feature would you like?'),
}));
vi.mock('@anthropic-ai/sdk', () => ({
  default: class MockAnthropic { messages = { create: vi.fn() }; },
}));

import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import { processFeatureMessage } from '../../src/services/claude';
import type { Request } from 'express';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const mockProcessFeatureMessage = processFeatureMessage as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

// Several tests set STRIPE_BYPASS or ADMIN_EMAIL; put both back after each one
// so no test depends on what an earlier one left behind.
const INITIAL_STRIPE_BYPASS = process.env.STRIPE_BYPASS;
const INITIAL_ADMIN_EMAIL = process.env.ADMIN_EMAIL;
afterEach(() => {
  if (INITIAL_STRIPE_BYPASS === undefined) delete process.env.STRIPE_BYPASS;
  else process.env.STRIPE_BYPASS = INITIAL_STRIPE_BYPASS;
  if (INITIAL_ADMIN_EMAIL === undefined) delete process.env.ADMIN_EMAIL;
  else process.env.ADMIN_EMAIL = INITIAL_ADMIN_EMAIL;
});

/** The first statement whose SQL matches `pattern`, as [sql, params]. The status and ownership guards are SQL predicates, so the tests read them there. */
function statementMatching(pattern: RegExp): [string, unknown[]] {
  const call = mockQuery.mock.calls.find(([sql]) => pattern.test(String(sql)));
  expect(call, `a statement matching ${pattern}`).toBeDefined();
  return [String(call![0]), (call![1] ?? []) as unknown[]];
}

async function makeAuthenticatedAgent(email = 'user@example.test', isAdmin = false) {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  const adminEmail = isAdmin ? 'founder@example.com' : email;
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'cust-1', email: adminEmail, name: 'Test User', password: hashed, email_verified: true }],
  });
  await agent.post('/login').type('form').send({
    _csrf: getValidCsrf(),
    email: adminEmail,
    password: 'password123',
  });
  return agent;
}

describe('Feature Request Routes', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  describe('GET /projects/:buildId/features', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/projects/build-1/features');
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/login/);
    });

    it('returns 404 for build not owned by customer', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({ rows: [] }); // build not found
      const res = await agent.get('/projects/build-1/features');
      expect(res.status).toBe(404);
      const [sql, params] = statementMatching(/FROM builds/);
      expect(sql).toContain('customer_id = $2');
      expect(params).toEqual(['build-1', 'cust-1']);
    });

    it('returns 200 for owned preview build', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery
        .mockResolvedValueOnce({
          rows: [{ id: 'build-1', app_name: 'my-app', status: 'preview', live_url: null, preview_url: 'https://app.example.com' }],
        })
        .mockResolvedValueOnce({ rows: [] }); // no features yet
      const res = await agent.get('/projects/build-1/features');
      expect(res.status).toBe(200);
      expect(res.text).toContain('my-app');
    });

    it('admin can view any build features', async () => {
      process.env.ADMIN_EMAIL = 'founder@example.com';
      const agent = await makeAuthenticatedAgent('admin@example.test', true);
      mockQuery
        .mockResolvedValueOnce({
          rows: [{ id: 'build-1', app_name: 'other-app', status: 'live', live_url: 'https://example.test', preview_url: null }],
        })
        .mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/projects/build-1/features');
      expect(res.status).toBe(200);
      // The admin lookup carries no owner predicate (contrast with the 404 test above).
      const [sql, params] = statementMatching(/FROM builds/);
      expect(sql).not.toContain('customer_id');
      expect(params).toEqual(['build-1']);
    });
  });

  describe('POST /projects/:buildId/features/new', () => {
    it('requires CSRF token', async () => {
      const agent = await makeAuthenticatedAgent();
      const res = await agent.post('/projects/build-1/features/new').type('form').send({});
      expect(res.status).toBe(403);
    });

    it('creates feature request for preview build', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery
        .mockResolvedValueOnce({
          rows: [{ id: 'build-1', spec: { app_name: 'my-app', description: 'test' } }],
        })
        .mockResolvedValueOnce({ rows: [{ id: 'feat-1' }] }) // INSERT feature_request
        .mockResolvedValueOnce({ rows: [] }); // INSERT greeting message

      const res = await agent
        .post('/projects/build-1/features/new')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/features\/feat-1/);
    });

    it('returns 404 for non-existent build', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post('/projects/build-1/features/new')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(404);
    });
  });

  describe('GET /features/:featureId', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/features/feat-1');
      expect(res.status).toBe(302);
    });

    it('returns 404 for feature not owned by customer', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/features/feat-1');
      expect(res.status).toBe(404);
      const [sql, params] = statementMatching(/FROM feature_requests/);
      expect(sql).toContain('customer_id = $2');
      expect(params).toEqual(['feat-1', 'cust-1']);
    });

    it('renders feature chat for owned feature', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery
        .mockResolvedValueOnce({
          rows: [{ id: 'feat-1', build_id: 'build-1', status: 'drafting', customer_id: 'cust-1' }],
        })
        .mockResolvedValueOnce({
          rows: [{ id: 'build-1', app_name: 'my-app', live_url: 'https://app.example' }],
        })
        .mockResolvedValueOnce({ rows: [] }); // messages
      const res = await agent.get('/features/feat-1');
      expect(res.status).toBe(200);
      expect(res.text).toContain('my-app');
    });

    it('admin can view any feature', async () => {
      process.env.ADMIN_EMAIL = 'founder@example.com';
      const agent = await makeAuthenticatedAgent('admin@example.test', true);
      mockQuery
        .mockResolvedValueOnce({
          rows: [{ id: 'feat-1', build_id: 'build-1', status: 'drafting', customer_id: 'other' }],
        })
        .mockResolvedValueOnce({
          rows: [{ id: 'build-1', app_name: 'my-app', live_url: 'https://app.example' }],
        })
        .mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/features/feat-1');
      expect(res.status).toBe(200);
      const [sql, params] = statementMatching(/FROM feature_requests/);
      expect(sql).not.toContain('customer_id');
      expect(params).toEqual(['feat-1']);
    });
  });

  describe('POST /features/:featureId/approve', () => {
    it('moves scoped feature to payment_pending', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery
        .mockResolvedValueOnce({
          rows: [{ id: 'feat-1', status: 'scoped', customer_id: 'cust-1' }],
        })
        .mockResolvedValueOnce({ rows: [] }); // UPDATE status

      const res = await agent
        .post('/features/feat-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/features\/feat-1\/pay/);
      const [sql, params] = statementMatching(/UPDATE feature_requests/);
      expect(sql).toContain("status = 'payment_pending'");
      expect(params).toEqual(['feat-1']);
    });

    it('returns 404 for non-scoped feature', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post('/features/feat-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(404);
      const [sql, params] = statementMatching(/FROM feature_requests/);
      expect(sql).toContain("status = 'scoped'");
      expect(sql).toContain('customer_id = $2');
      expect(params).toEqual(['feat-1', 'cust-1']);
      expect(mockQuery.mock.calls.some(([q]) => /UPDATE feature_requests/.test(String(q)))).toBe(false);
    });
  });

  describe('GET /features/:featureId/pay', () => {
    it('renders payment page for payment_pending feature', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-1', status: 'payment_pending', customer_id: 'cust-1',
          estimated_cost: 4900, scope_summary: 'Add dark mode', app_name: 'my-app',
        }],
      });
      const res = await agent.get('/features/feat-1/pay');
      expect(res.status).toBe(200);
      expect(res.text).toContain('$49');
    });

    it('returns 404 for non-payment_pending feature', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/features/feat-1/pay');
      expect(res.status).toBe(404);
      const [sql, params] = statementMatching(/FROM feature_requests/);
      expect(sql).toContain("fr.status = 'payment_pending'");
      expect(sql).toContain('fr.customer_id = $2');
      expect(params).toEqual(['feat-1', 'cust-1']);
    });
  });

  describe('POST /features/:featureId/bypass-dev', () => {
    it('queues feature when STRIPE_BYPASS is set', async () => {
      const original = process.env.STRIPE_BYPASS;
      process.env.STRIPE_BYPASS = 'true';

      const agent = await makeAuthenticatedAgent();
      mockQuery
        .mockResolvedValueOnce({
          rows: [{
            id: 'feat-1', status: 'payment_pending', build_id: 'build-1',
            app_name: 'my-app', email: 'user@example.test', name: 'Test',
          }],
        })
        .mockResolvedValueOnce({ rows: [] }); // UPDATE status

      const res = await agent
        .post('/features/feat-1/bypass-dev')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      // After bypass, redirects to build status page (not back to chat)
      expect(res.headers.location).toMatch(/\/builds\/build-1/);
      const [sql, params] = statementMatching(/UPDATE feature_requests/);
      expect(sql).toContain("status = 'queued'");
      expect(params).toEqual(['feat-1']);

      if (original === undefined) delete process.env.STRIPE_BYPASS;
      else process.env.STRIPE_BYPASS = original;
    });

    // STRIPE_BYPASS is an arming switch (utils/env-switch.ts): the spellings
    // that say "yes" turn the bypass on, and every other text, a "no" above all,
    // leaves it off. The route is driven for each, so the answer is the one a
    // customer would get, not the helper's.
    it.each(['true', '1', 'yes', 'on', 'enabled', ' TRUE ', 'On'])('STRIPE_BYPASS=%j turns the bypass on', async (spelling) => {
      process.env.STRIPE_BYPASS = spelling;
      const agent = await makeAuthenticatedAgent();
      mockQuery
        .mockResolvedValueOnce({
          rows: [{
            id: 'feat-1', status: 'payment_pending', build_id: 'build-1',
            app_name: 'my-app', email: 'user@example.test', name: 'Test',
          }],
        })
        .mockResolvedValueOnce({ rows: [] }); // UPDATE status

      const res = await agent.post('/features/feat-1/bypass-dev').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(statementMatching(/UPDATE feature_requests/)[0]).toContain("status = 'queued'");
    });

    it.each(['false', 'FALSE', '0', 'no', 'off', 'disabled', '', '   ', 'nope', 'ture'])(
      'STRIPE_BYPASS=%j leaves the bypass off: 403 before any lookup, in development and test too',
      async (spelling) => {
        process.env.STRIPE_BYPASS = spelling;
        const agent = await makeAuthenticatedAgent();
        const callsBefore = mockQuery.mock.calls.length;
        const res = await agent.post('/features/feat-1/bypass-dev').type('form').send({ _csrf: getValidCsrf() });
        expect(res.status).toBe(403);
        expect(mockQuery.mock.calls.length).toBe(callsBefore);
        expect(mockQuery.mock.calls.some(([sql]) => /UPDATE feature_requests/.test(String(sql)))).toBe(false);
      }
    );

    it('the payment page offers the bypass only when it is on: the form is there for "true" and not for "false"', async () => {
      const page = async (spelling: string): Promise<string> => {
        process.env.STRIPE_BYPASS = spelling;
        const agent = await makeAuthenticatedAgent();
        mockQuery
          .mockResolvedValueOnce({
            rows: [{
              id: 'feat-1', status: 'payment_pending', build_id: 'build-1', description: 'A gallery',
              scope_summary: 'A gallery page', estimated_cost: 4900, app_name: 'my-app',
            }],
          })
          .mockResolvedValue({ rows: [] });
        const res = await agent.get('/features/feat-1/pay');
        expect(res.status).toBe(200);
        return res.text;
      };
      expect(await page('true')).toContain('/features/feat-1/bypass-dev');
      expect(await page('false')).not.toContain('/features/feat-1/bypass-dev');
    });

    it('refuses a non-admin customer in production even when STRIPE_BYPASS is set', async () => {
      const originalBypass = process.env.STRIPE_BYPASS;
      const originalEnv = process.env.NODE_ENV;
      const originalAdmin = process.env.ADMIN_EMAIL;
      process.env.STRIPE_BYPASS = 'true';
      process.env.ADMIN_EMAIL = 'someone-else@example.com';

      const agent = await makeAuthenticatedAgent();
      const callsBefore = mockQuery.mock.calls.length;
      process.env.NODE_ENV = 'production';
      try {
        const res = await agent
          .post('/features/feat-1/bypass-dev')
          .type('form')
          .send({ _csrf: getValidCsrf() });
        expect(res.status).toBe(403);
        // Refused before any lookup or status change.
        expect(mockQuery.mock.calls.length).toBe(callsBefore);
      } finally {
        process.env.NODE_ENV = originalEnv;
        if (originalBypass === undefined) delete process.env.STRIPE_BYPASS;
        else process.env.STRIPE_BYPASS = originalBypass;
        if (originalAdmin === undefined) delete process.env.ADMIN_EMAIL;
        else process.env.ADMIN_EMAIL = originalAdmin;
      }
    });

    it('returns 403 when STRIPE_BYPASS not set', async () => {
      const original = process.env.STRIPE_BYPASS;
      delete process.env.STRIPE_BYPASS;

      const agent = await makeAuthenticatedAgent();
      const res = await agent
        .post('/features/feat-1/bypass-dev')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);

      process.env.STRIPE_BYPASS = original;
    });

    it('redirects when feature is not in payment_pending or scoped', async () => {
      const original = process.env.STRIPE_BYPASS;
      process.env.STRIPE_BYPASS = 'true';

      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-live', status: 'live', build_id: 'build-1',
          app_name: 'my-app', email: 'user@example.test', name: 'Test',
        }],
      });

      const res = await agent
        .post('/features/feat-live/bypass-dev')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/features\/feat-live/);

      process.env.STRIPE_BYPASS = original;
    });
  });

  describe('POST /features/:featureId/message', () => {
    beforeEach(() => {
      mockProcessFeatureMessage.mockReset().mockResolvedValue({ reply: 'Got it!', scopeComplete: false });
    });

    it('returns 403 when CSRF token is missing', async () => {
      const agent = await makeAuthenticatedAgent();
      const res = await agent
        .post('/features/feat-1/message')
        .send({ message: 'I want a dashboard' });
      expect(res.status).toBe(403);
      expect(res.body.error).toMatch(/CSRF/i);
    });

    it('returns 400 when message is empty', async () => {
      const agent = await makeAuthenticatedAgent();
      const res = await agent
        .post('/features/feat-1/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: '' });
      expect(res.status).toBe(400);
      expect(res.body.error).toMatch(/Message is required/i);
    });

    it('returns 404 for non-existent or non-drafting feature', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const res = await agent
        .post('/features/feat-nonexist/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: 'Hello' });
      expect(res.status).toBe(404);
      const [sql, params] = statementMatching(/FROM feature_requests/);
      expect(sql).toContain("fr.status = 'drafting'");
      expect(sql).toContain('fr.customer_id = $2');
      expect(params).toEqual(['feat-nonexist', 'cust-1']);
    });

    it('processes message and returns AI response', async () => {
      const agent = await makeAuthenticatedAgent();
      // Feature lookup
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-1', status: 'drafting', customer_id: 'cust-1',
          build_id: 'build-1', spec: { app_name: 'my-app', description: 'test' },
        }],
      });
      // History messages
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // INSERT user message
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // INSERT assistant message
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const res = await agent
        .post('/features/feat-1/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: 'I want a dark mode feature' });
      expect(res.status).toBe(200);
      expect(res.body.reply).toBe('Got it!');
      expect(res.body.scopeComplete).toBe(false);
    });

    it('updates feature to scoped when scopeComplete is true', async () => {
      const agent = await makeAuthenticatedAgent();
      mockProcessFeatureMessage.mockResolvedValueOnce({
        reply: 'Scope finalized!',
        scopeComplete: true,
        scope: {
          description: 'Dark mode toggle',
          summary: 'Add dark mode with system preference detection',
          estimated_cost: 4900,
        },
      });

      // Feature lookup
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-scope', status: 'drafting', customer_id: 'cust-1',
          build_id: 'build-1', spec: { app_name: 'my-app', description: 'test' },
        }],
      });
      // History
      mockQuery.mockResolvedValueOnce({ rows: [{ role: 'assistant', content: 'Hello', created_at: new Date() }] });
      // INSERT user message
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // INSERT assistant message
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // UPDATE feature_requests SET status='scoped'
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const res = await agent
        .post('/features/feat-scope/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: 'Yes, that looks good' });
      expect(res.status).toBe(200);
      expect(res.body.scopeComplete).toBe(true);

      // Verify the UPDATE was called (find it among all calls)
      const scopedCalls = mockQuery.mock.calls.filter(
        (call: unknown[]) => typeof call[0] === 'string' && (call[0] as string).includes("SET status = 'scoped'")
      );
      expect(scopedCalls.length).toBe(1);
      expect(scopedCalls[0][1]).toContain(4900);
    });

    it('returns 500 when Claude API fails', async () => {
      const agent = await makeAuthenticatedAgent();
      mockProcessFeatureMessage.mockRejectedValueOnce(new Error('Claude overloaded'));

      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-err', status: 'drafting', customer_id: 'cust-1',
          build_id: 'build-1', spec: { app_name: 'my-app', description: 'test' },
        }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // history
      mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT user message

      const res = await agent
        .post('/features/feat-err/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: 'Some request' });
      expect(res.status).toBe(500);
      expect(res.body.error).toMatch(/Failed to process/i);
    });
  });

  describe('GET /features/:featureId/pay: cost tiers', () => {
    it('shows $99 for medium features', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-med', status: 'payment_pending', customer_id: 'cust-1',
          estimated_cost: 9900, scope_summary: 'Add integrations', app_name: 'my-app',
        }],
      });
      const res = await agent.get('/features/feat-med/pay');
      expect(res.status).toBe(200);
      expect(res.text).toContain('$99');
      expect(res.text).toContain('Medium');
    });

    it('shows $199 for large features', async () => {
      const agent = await makeAuthenticatedAgent();
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'feat-lg', status: 'payment_pending', customer_id: 'cust-1',
          estimated_cost: 19900, scope_summary: 'Full redesign', app_name: 'my-app',
        }],
      });
      const res = await agent.get('/features/feat-lg/pay');
      expect(res.status).toBe(200);
      expect(res.text).toContain('$199');
    });
  });
});


describe('Feature Request Routes: review round fixes', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockProcessFeatureMessage.mockReset().mockResolvedValue({ reply: 'Got it!', scopeComplete: false });
  });

  function featureLookupRow() {
    return {
      id: 'feat-1', status: 'drafting', customer_id: 'cust-1',
      build_id: 'build-1', spec: { app_name: 'my-app', description: 'test' },
    };
  }

  it('saves nothing when the model call fails, so the transcript stays retryable', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [featureLookupRow()] });
    mockQuery.mockResolvedValueOnce({ rows: [] }); // history
    mockProcessFeatureMessage.mockRejectedValueOnce(new Error('upstream down'));

    const res = await agent
      .post('/features/feat-1/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'add a gallery' });

    expect(res.status).toBe(500);
    const inserts = mockQuery.mock.calls.filter((c: unknown[]) => String(c[0]).includes('INSERT INTO feature_messages'));
    expect(inserts).toHaveLength(0);
  });

  it('saves the user turn and the reply together, in that order, after the model call succeeds', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [featureLookupRow()] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValue({ rows: [] });

    const res = await agent
      .post('/features/feat-1/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'add a gallery' });

    expect(res.status).toBe(200);
    const inserts = mockQuery.mock.calls.filter((c: unknown[]) => String(c[0]).includes('INSERT INTO feature_messages'));
    expect(inserts.map((c: unknown[]) => (c[1] as string[])[1])).toEqual(['user', 'assistant']);
  });

  it('drops an orphaned trailing user message from the history it sends to the model', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [featureLookupRow()] });
    mockQuery.mockResolvedValueOnce({
      rows: [
        { role: 'assistant', content: 'What would you like?' },
        { role: 'user', content: 'first try that failed' },
      ],
    });
    mockQuery.mockResolvedValue({ rows: [] });

    await agent
      .post('/features/feat-1/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'retry' });

    const history = mockProcessFeatureMessage.mock.calls[0][1] as Array<{ role: string }>;
    expect(history.map((m) => m.role)).toEqual(['assistant']);
    expect(mockProcessFeatureMessage.mock.calls[0][2]).toBe(0); // turnCount
  });

  it('answers 400 when message is not a string', async () => {
    const agent = await makeAuthenticatedAgent();
    for (const bad of [7, { a: 1 }, ['x']]) {
      const res = await agent
        .post('/features/feat-1/message')
        .set('x-csrf-token', getValidCsrf())
        .send({ message: bad });
      expect(res.status).toBe(400);
    }
    expect(mockProcessFeatureMessage).not.toHaveBeenCalled();
  });

  it('answers 503 when the circuit breaker is open', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [featureLookupRow()] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const open = new Error('We are experiencing high demand right now.');
    open.name = 'CircuitBreakerOpenError';
    mockProcessFeatureMessage.mockRejectedValueOnce(open);

    const res = await agent
      .post('/features/feat-1/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'hello' });
    expect(res.status).toBe(503);
  });

  it('answers 503 with the explanation when no model key is configured', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [featureLookupRow()] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockProcessFeatureMessage.mockRejectedValueOnce(new AssistantNotConfiguredError());

    const res = await agent
      .post('/features/feat-1/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 'hello' });
    expect(res.status).toBe(503);
    expect(res.body.code).toBe('assistant_not_configured');
    expect(res.body.error).toMatch(/assistant is not configured on this server/i);
  });

  it('a new feature request is owned by the build owner even when the admin creates it', async () => {
    process.env.ADMIN_EMAIL = 'founder@example.com';
    const agent = await makeAuthenticatedAgent('user@example.test', true);
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-1', spec: { app_name: 'x' }, customer_id: 'customer-owner' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'feat-new' }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.post('/projects/build-1/features/new').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    const insert = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('INSERT INTO feature_requests'));
    expect(insert).toBeDefined();
    expect((insert![1] as unknown[])[1]).toBe('customer-owner');
  });
});
