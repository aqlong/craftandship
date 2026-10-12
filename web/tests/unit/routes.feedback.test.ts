import { describe, it, expect, vi, beforeEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

// Set ADMIN_EMAIL for admin routes
process.env.ADMIN_EMAIL = 'siteadmin@example.com';

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
}));

import pool from '../../src/db/index';
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

async function makeAuthenticatedAgent(email = 'user@example.test') {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'cust-1', email, name: 'Test User', password: hashed, email_verified: true }],
  });
  await agent.post('/login').type('form').send({
    _csrf: getValidCsrf(),
    email,
    password: 'password123',
  });
  return agent;
}

describe('POST /feedback', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('redirects to login when not authenticated', async () => {
    const res = await request(app)
      .post('/feedback')
      .send({ category: 'bug', message: 'Something broke' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('returns 403 when CSRF token is missing', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/feedback')
      .send({ category: 'bug', message: 'Something broke' });
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/CSRF/i);
  });

  it('returns 403 when CSRF token is invalid', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', 'invalid.token')
      .send({ category: 'bug', message: 'Something broke' });
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/CSRF/i);
  });

  it('returns 400 when category is missing', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ message: 'Something broke' });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/category/i);
  });

  it('returns 400 when category is invalid', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ category: 'rant', message: 'Something broke' });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/category/i);
  });

  it('returns 400 when message is empty', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ category: 'bug', message: '' });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Message is required/i);
  });

  it('returns 400 when message is only whitespace', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ category: 'bug', message: '   ' });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Message is required/i);
  });

  it('returns 400 when message exceeds max length', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const longMessage = 'x'.repeat(2001);
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ category: 'bug', message: longMessage });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/under.*2000/i);
  });

  it('inserts feedback and returns success for valid submission', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // INSERT succeeds
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ category: 'suggestion', message: 'Add dark mode please' });
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);

    // Verify INSERT was called with correct params (skip the login query)
    const insertCalls = mockQuery.mock.calls.filter(
      (call: unknown[]) => typeof call[0] === 'string' && (call[0] as string).includes('INSERT INTO beta_feedback')
    );
    expect(insertCalls.length).toBe(1);
    expect(insertCalls[0][1][1]).toBe('suggestion');
    expect(insertCalls[0][1][2]).toBe('Add dark mode please');
  });

  it('accepts all valid categories', async () => {
    for (const category of ['bug', 'suggestion', 'question', 'other']) {
      const agent = await makeAuthenticatedAgent();
      const csrf = getValidCsrf();
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post('/feedback')
        .set('x-csrf-token', csrf)
        .send({ category, message: 'Test feedback' });
      expect(res.status).toBe(200);
    }
  });

  it('returns 500 when DB insert fails', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockRejectedValueOnce(new Error('DB connection lost'));
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', csrf)
      .send({ category: 'bug', message: 'Something broke' });
    expect(res.status).toBe(500);
    expect(res.body.error).toMatch(/Failed to save/i);
  });
});

describe('GET /admin/feedback', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('redirects to login when not authenticated', async () => {
    const res = await request(app).get('/admin/feedback');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('returns 403 for non-admin user', async () => {
    const agent = await makeAuthenticatedAgent('regular@example.com');
    const res = await agent.get('/admin/feedback');
    expect(res.status).toBe(403);
    // The page says what the response says: a 403, not the server-fault page.
    expect(res.text).toContain('That request was not allowed.');
    expect(res.text).toContain('Admin access required.');
    expect(res.text).not.toContain('Something went sideways');
  });

  it('renders feedback list for admin user', async () => {
    const agent = await makeAuthenticatedAgent('siteadmin@example.com');
    // COUNT query
    mockQuery.mockResolvedValueOnce({ rows: [{ count: '2' }] });
    // Data query
    mockQuery.mockResolvedValueOnce({
      rows: [
        { id: 'fb-1', category: 'bug', message: 'Page broken', customer_email: 'u@example.test', customer_name: 'User', created_at: new Date() },
        { id: 'fb-2', category: 'suggestion', message: 'Add dark mode', customer_email: 'u2@example.test', customer_name: 'User2', created_at: new Date() },
      ],
    });
    // Insights query (category counts)
    mockQuery.mockResolvedValueOnce({ rows: [{ category: 'bug', count: 1 }, { category: 'suggestion', count: 1 }] });
    const res = await agent.get('/admin/feedback');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Beta Feedback');
    expect(res.text).toContain('Feedback Insights');
  });

  it('supports category filter', async () => {
    const agent = await makeAuthenticatedAgent('siteadmin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ count: '1' }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({ rows: [{ category: 'bug', count: 1 }] }); // insights
    const res = await agent.get('/admin/feedback?category=bug');
    expect(res.status).toBe(200);

    // Find the COUNT query with category filter (skip the login query)
    const feedbackCalls = mockQuery.mock.calls.filter(
      (call: unknown[]) => typeof call[0] === 'string' && (call[0] as string).includes('beta_feedback')
    );
    expect(feedbackCalls.length).toBeGreaterThan(0);
    expect(feedbackCalls[0][0]).toContain('bf.category = $1');
    expect(feedbackCalls[0][1]).toEqual(['bug']);
  });

  it('returns 500 when DB query fails', async () => {
    const agent = await makeAuthenticatedAgent('siteadmin@example.com');
    mockQuery.mockRejectedValueOnce(new Error('DB error'));
    const res = await agent.get('/admin/feedback');
    expect(res.status).toBe(500);
  });
});

describe('POST /feedback: non-string fields (review round)', () => {
  it('answers 400 for a non-string message or category instead of throwing', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    for (const body of [
      { category: 'bug', message: ['a'] },
      { category: 'bug', message: { x: 1 } },
      { category: 'bug', message: 9 },
      { category: ['bug'], message: 'ok' },
    ]) {
      const res = await agent.post('/feedback').set('x-csrf-token', csrf).send(body);
      expect(res.status, JSON.stringify(body)).toBe(400);
    }
  });

  it('ignores a non-string page_url instead of throwing', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await agent
      .post('/feedback')
      .set('x-csrf-token', getValidCsrf())
      .send({ category: 'bug', message: 'broken', page_url: { evil: true } });
    expect(res.status).toBe(200);
    const insert = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('INSERT INTO beta_feedback'));
    expect((insert![1] as unknown[])[3]).toBe('');
  });
});
