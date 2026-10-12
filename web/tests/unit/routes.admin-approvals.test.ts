import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import app from '../../src/app';

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

// Mock services that other mounted routes depend on
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
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

import supertest from 'supertest';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

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

// Mock the approval list page queries (items + stats)
function mockApprovalPageQueries(items: Array<Record<string, unknown>> = [], stats = { total: '0', pending: '0', approved: '0', rejected: '0' }) {
  // Items query
  mockQuery.mockResolvedValueOnce({ rows: items });
  // Stats query
  mockQuery.mockResolvedValueOnce({ rows: [stats] });
}

/** The UPDATE approval_requests statement a handler issued, as [sql, params]. */
function approvalUpdate(): [string, unknown[]] {
  const call = mockQuery.mock.calls.find(([sql]) => /UPDATE approval_requests/.test(String(sql)));
  expect(call, 'the handler should have issued UPDATE approval_requests').toBeDefined();
  return [String(call![0]), call![1] as unknown[]];
}

describe('Admin Approvals', () => {
  const origEnv = process.env.ADMIN_EMAIL;

  beforeEach(() => {
    mockQuery.mockReset();
    process.env.ADMIN_EMAIL = 'founder@example.com';
  });

  afterEach(() => {
    // Assigning undefined would store the string "undefined"; restore by deleting.
    if (origEnv === undefined) delete process.env.ADMIN_EMAIL;
    else process.env.ADMIN_EMAIL = origEnv;
  });

  describe('GET /admin/approvals', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/admin/approvals');
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/login/);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await makeNonAdminAgent();
      const res = await agent.get('/admin/approvals');
      expect(res.status).toBe(403);
    });

    it('returns 200 for admin with empty list', async () => {
      const agent = await makeAdminAgent();
      mockApprovalPageQueries();
      const res = await agent.get('/admin/approvals');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Approvals');
      expect(res.text).toContain('No approval requests found');
    });

    it('shows pending items with approve/reject buttons', async () => {
      const agent = await makeAdminAgent();
      mockApprovalPageQueries(
        [{
          id: 'req-1',
          type: 'high_cost_deploy',
          description: 'Deploy costing $50+',
          context: { cost: 55 },
          status: 'pending',
          created_at: new Date().toISOString(),
          resolved_at: null,
          resolved_by: null,
        }],
        { total: '1', pending: '1', approved: '0', rejected: '0' }
      );
      const res = await agent.get('/admin/approvals');
      expect(res.status).toBe(200);
      expect(res.text).toContain('high_cost_deploy');
      expect(res.text).toContain('Deploy costing $50+');
      // The forms themselves; the words alone also appear in the stats and filter labels.
      expect(res.text).toContain('action="/admin/approvals/req-1/approve"');
      expect(res.text).toContain('action="/admin/approvals/req-1/reject"');
    });

    it('filters by status', async () => {
      const agent = await makeAdminAgent();
      mockApprovalPageQueries(
        [{
          id: 'req-2',
          type: 'risky_change',
          description: 'Schema migration',
          context: {},
          status: 'approved',
          created_at: new Date().toISOString(),
          resolved_at: new Date().toISOString(),
          resolved_by: 'founder@example.com',
        }],
        { total: '1', pending: '0', approved: '1', rejected: '0' }
      );
      const res = await agent.get('/admin/approvals?status=approved');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Schema migration');
      expect(res.text).toContain('value="approved" selected');
      // The filter reaches the query, not just the dropdown.
      const itemsCall = mockQuery.mock.calls.find(([sql]) => /FROM approval_requests/.test(String(sql)) && /status = \$1/.test(String(sql)));
      expect(itemsCall, 'a status-filtered items query').toBeDefined();
      expect(itemsCall![1]).toContain('approved');
    });

    it('shows pending count badge', async () => {
      const agent = await makeAdminAgent();
      mockApprovalPageQueries([], { total: '3', pending: '3', approved: '0', rejected: '0' });
      const res = await agent.get('/admin/approvals');
      expect(res.status).toBe(200);
      // The badge should show the count 3
      expect(res.text).toMatch(/>3<\/span>/);
    });

    it('shows context details toggle when context is present', async () => {
      const agent = await makeAdminAgent();
      mockApprovalPageQueries(
        [{
          id: 'req-3',
          type: 'test_type',
          description: 'Test desc',
          context: { key: 'value' },
          status: 'pending',
          created_at: new Date().toISOString(),
          resolved_at: null,
          resolved_by: null,
        }],
        { total: '1', pending: '1', approved: '0', rejected: '0' }
      );
      const res = await agent.get('/admin/approvals');
      expect(res.status).toBe(200);
      expect(res.text).toContain('details');
      expect(res.text).toContain('ctx-req-3');
    });
  });

  describe('POST /admin/approvals/:id/approve', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).post('/admin/approvals/some-id/approve');
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/login/);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await makeNonAdminAgent();
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/some-id/approve')
        .type('form')
        .send({ _csrf: csrf });
      expect(res.status).toBe(403);
    });

    it('approves a pending request and redirects', async () => {
      const agent = await makeAdminAgent();
      // UPDATE returning
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'req-1' }], rowCount: 1 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/req-1/approve')
        .type('form')
        .send({ _csrf: csrf });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/approvals');
      const [sql, params] = approvalUpdate();
      expect(sql).toContain("SET status = 'approved'");
      expect(params).toEqual(['req-1', 'founder@example.com']);
    });

    it('returns 404 when request not found or already resolved', async () => {
      const agent = await makeAdminAgent();
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 0 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/nonexistent/approve')
        .type('form')
        .send({ _csrf: csrf });
      expect(res.status).toBe(404);
      expect(res.body.error).toContain('not found');
      // "Already resolved" is the predicate: only a pending row can be approved.
      expect(approvalUpdate()[0]).toContain("status = 'pending'");
    });
  });

  describe('POST /admin/approvals/:id/reject', () => {
    it('rejects a pending request and redirects', async () => {
      const agent = await makeAdminAgent();
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'req-1' }], rowCount: 1 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/req-1/reject')
        .type('form')
        .send({ _csrf: csrf });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/approvals');
      const [sql, params] = approvalUpdate();
      expect(sql).toContain("SET status = 'rejected'");
      expect(params).toEqual(['req-1', 'founder@example.com']);
    });

    it('returns 404 when request not found', async () => {
      const agent = await makeAdminAgent();
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 0 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/nonexistent/reject')
        .type('form')
        .send({ _csrf: csrf });
      expect(res.status).toBe(404);
      expect(approvalUpdate()[0]).toContain("status = 'pending'");
    });
  });

  describe('POST /admin/approvals/batch', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).post('/admin/approvals/batch');
      expect(res.status).toBe(302);
    });

    it('batch approves multiple requests', async () => {
      const agent = await makeAdminAgent();
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'a' }, { id: 'b' }], rowCount: 2 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/batch')
        .type('form')
        .send({ _csrf: csrf, action: 'approve', ids: 'a,b' });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/approvals');
      const [sql, params] = approvalUpdate();
      expect(sql).toContain("status = 'pending'");
      expect(params).toEqual(['approved', 'founder@example.com', ['a', 'b']]);
    });

    it('batch rejects multiple requests', async () => {
      const agent = await makeAdminAgent();
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'a' }], rowCount: 1 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/batch')
        .type('form')
        .send({ _csrf: csrf, action: 'reject', ids: 'a' });
      expect(res.status).toBe(302);
      expect(approvalUpdate()[1]).toEqual(['rejected', 'founder@example.com', ['a']]);
    });

    it('rejects invalid action', async () => {
      const agent = await makeAdminAgent();
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/batch')
        .type('form')
        .send({ _csrf: csrf, action: 'invalid', ids: 'a' });
      expect(res.status).toBe(400);
      expect(res.body.error).toContain('Invalid action');
    });

    it('rejects empty ids', async () => {
      const agent = await makeAdminAgent();
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/batch')
        .type('form')
        .send({ _csrf: csrf, action: 'approve', ids: '' });
      expect(res.status).toBe(400);
      expect(res.body.error).toContain('1 and 10');
    });

    it('supports array of ids', async () => {
      const agent = await makeAdminAgent();
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'x' }, { id: 'y' }, { id: 'z' }], rowCount: 3 });
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/approvals/batch')
        .send({ _csrf: csrf, action: 'approve', ids: ['x', 'y', 'z'] });
      expect(res.status).toBe(302);
      expect(approvalUpdate()[1]).toEqual(['approved', 'founder@example.com', ['x', 'y', 'z']]);
    });
  });
});
