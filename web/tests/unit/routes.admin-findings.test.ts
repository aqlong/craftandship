import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import supertest from 'supertest';
import app from '../../src/app';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return {
    default: { query: mockQuery, on: vi.fn() },
  };
});

vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendPreviewReadyEmail: vi.fn().mockResolvedValue(undefined),
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
vi.mock('../../src/services/anthropic-costs', () => ({
  getAnthropicCosts: vi.fn().mockResolvedValue(null),
  getAnthropicCostsAndStatus: vi.fn().mockResolvedValue({ rows: null, partial: false, partialReason: null }),
  rebuildAnthropicCostsCache: vi.fn().mockResolvedValue({ rowsFetched: 0, success: true }),
}));

import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

async function makeAdminAgent() {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'admin-id', email: 'founder@example.com', name: 'Admin', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email: 'founder@example.com', password: 'password123' });
  return agent;
}

async function makeNonAdminAgent() {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'user-id', email: 'user@example.com', name: 'User', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email: 'user@example.com', password: 'password123' });
  return agent;
}

const origAdminEmail = process.env.ADMIN_EMAIL;

describe('GET /admin/findings', () => {
  beforeEach(() => { mockQuery.mockReset(); process.env.ADMIN_EMAIL = 'founder@example.com'; });
  afterEach(() => { process.env.ADMIN_EMAIL = origAdminEmail; });

  it('returns 403 for non-admin', async () => {
    const agent = await makeNonAdminAgent();
    const res = await agent.get('/admin/findings');
    expect(res.status).toBe(403);
  });

  it('returns 302 redirect when unauthenticated', async () => {
    const res = await supertest(app).get('/admin/findings');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('renders findings list for admin with default new filter', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'f1', agent: 'backend_engineer', category: 'build_failure',
        finding_key: 'build-fail-abc', title: 'Investigate failed build', description: 'desc',
        priority: 'high', status: 'new', roadmap_item_id: null, review_notes: null,
        created_at: new Date().toISOString(), reviewed_at: null, reviewed_by: null,
      }],
    });
    mockQuery.mockResolvedValueOnce({
      rows: [{ total: '1', new_count: '1', reviewed: '0', promoted: '0', dismissed: '0' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [{ category: 'build_failure' }] });

    const res = await agent.get('/admin/findings');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Investigate failed build');
    expect(res.text).toContain('build_failure');
  });

  it('accepts status filter query param', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ total: '0', new_count: '0', reviewed: '0', promoted: '0', dismissed: '0' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/admin/findings?status=promoted');
    expect(res.status).toBe(200);
    const firstCall = mockQuery.mock.calls.find(c => (c[0] as string).includes('FROM coordinator_findings'));
    expect(firstCall?.[1]).toContain('promoted');
  });
});

describe('POST /admin/findings/:id/review', () => {
  beforeEach(() => { mockQuery.mockReset(); process.env.ADMIN_EMAIL = 'founder@example.com'; });
  afterEach(() => { process.env.ADMIN_EMAIL = origAdminEmail; });

  it('rejects without CSRF', async () => {
    const agent = await makeAdminAgent();
    const res = await agent.post('/admin/findings/f1/review').type('form').send({});
    expect(res.status).toBe(403);
  });

  it('rejects for non-admin', async () => {
    const agent = await makeNonAdminAgent();
    const res = await agent
      .post('/admin/findings/f1/review')
      .type('form')
      .send({ _csrf: getValidCsrf() });
    expect(res.status).toBe(403);
  });

  it('marks finding reviewed for admin', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [{ id: 'f1' }] });

    const res = await agent
      .post('/admin/findings/f1/review')
      .type('form')
      .send({ _csrf: getValidCsrf(), notes: 'looks right' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/admin/findings');
    const updateCall = mockQuery.mock.calls.find(c => (c[0] as string).includes("status = 'reviewed'"));
    expect(updateCall).toBeDefined();
    expect(updateCall?.[1]).toContain('founder@example.com');
  });

  it('returns 404 when finding not in new status', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rowCount: 0, rows: [] });

    const res = await agent
      .post('/admin/findings/f1/review')
      .type('form')
      .send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(404);
  });
});

describe('POST /admin/findings/:id/dismiss', () => {
  beforeEach(() => { mockQuery.mockReset(); process.env.ADMIN_EMAIL = 'founder@example.com'; });
  afterEach(() => { process.env.ADMIN_EMAIL = origAdminEmail; });

  it('marks finding dismissed', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [{ id: 'f1' }] });

    const res = await agent
      .post('/admin/findings/f1/dismiss')
      .type('form')
      .send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    const updateCall = mockQuery.mock.calls.find(c => (c[0] as string).includes("status = 'dismissed'"));
    expect(updateCall).toBeDefined();
  });
});

describe('POST /admin/findings/:id/promote', () => {
  beforeEach(() => { mockQuery.mockReset(); process.env.ADMIN_EMAIL = 'founder@example.com'; });
  afterEach(() => { process.env.ADMIN_EMAIL = origAdminEmail; });

  it('requires roadmap_item_id', async () => {
    const agent = await makeAdminAgent();
    const res = await agent
      .post('/admin/findings/f1/promote')
      .type('form')
      .send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(400);
  });

  it('promotes finding with roadmap_item_id', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [{ id: 'f1' }] });

    const res = await agent
      .post('/admin/findings/f1/promote')
      .type('form')
      .send({ _csrf: getValidCsrf(), roadmap_item_id: 'fix-thing-xyz', notes: 'took this on' });

    expect(res.status).toBe(302);
    const updateCall = mockQuery.mock.calls.find(c => (c[0] as string).includes("status = 'promoted'"));
    expect(updateCall).toBeDefined();
    expect(updateCall?.[1]).toContain('fix-thing-xyz');
  });
});
