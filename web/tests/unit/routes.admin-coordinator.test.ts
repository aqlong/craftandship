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

// Mock the 5 parallel queries: count, data, stats, agents, categories
function mockCoordinatorQueries(opts?: { total?: number; tasks?: object[] }) {
  const total = opts?.total ?? 1;
  const tasks = opts?.tasks ?? [
    {
      id: 'task-1',
      tick_id: 'tick-1',
      agent: 'infra_engineer',
      task_category: 'build_failure',
      task_description: 'Fix failed build',
      status: 'completed',
      actual_cost_usd: 0.05,
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
      duration_s: 12.5,
    },
  ];
  // count
  mockQuery.mockResolvedValueOnce({ rows: [{ total: String(total) }] });
  // data
  mockQuery.mockResolvedValueOnce({ rows: tasks });
  // stats
  mockQuery.mockResolvedValueOnce({
    rows: [{ total: String(total), pending: '0', running: '0', completed: String(total), failed: '0', skipped: '0' }],
  });
  // agents
  mockQuery.mockResolvedValueOnce({ rows: [{ agent: 'infra_engineer' }, { agent: 'backend_engineer' }] });
  // categories
  mockQuery.mockResolvedValueOnce({ rows: [{ task_category: 'build_failure' }, { task_category: 'cost_report' }] });
}

describe('Admin Coordinator', () => {
  const origEnv = process.env.ADMIN_EMAIL;

  beforeEach(() => {
    mockQuery.mockReset();
    process.env.ADMIN_EMAIL = 'founder@example.com';
  });

  afterEach(() => {
    process.env.ADMIN_EMAIL = origEnv;
  });

  describe('GET /admin/coordinator', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/admin/coordinator');
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/login/);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await makeNonAdminAgent();
      const res = await agent.get('/admin/coordinator');
      expect(res.status).toBe(403);
    });

    it('renders coordinator page for admin', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries();
      const res = await agent.get('/admin/coordinator');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Coordinator');
      expect(res.text).toContain('infra_engineer');
      expect(res.text).toContain('build_failure');
    });

    it('applies agent filter', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries();
      const res = await agent.get('/admin/coordinator?agent=infra_engineer');
      expect(res.status).toBe(200);
      // Verify agent param was passed to query
      const calls = mockQuery.mock.calls;
      const countCall = calls.find((c: string[]) => typeof c[0] === 'string' && c[0].includes('COUNT(*)'));
      expect(countCall).toBeTruthy();
      if (countCall) {
        expect(countCall[0]).toContain('agent = $');
        expect(countCall[1]).toContain('infra_engineer');
      }
    });

    it('applies date range filters', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries();
      const res = await agent.get('/admin/coordinator?date_from=2026-03-01&date_to=2026-03-31');
      expect(res.status).toBe(200);
      const calls = mockQuery.mock.calls;
      const countCall = calls.find((c: string[]) => typeof c[0] === 'string' && c[0].includes('COUNT(*)'));
      expect(countCall).toBeTruthy();
      if (countCall) {
        expect(countCall[0]).toContain('created_at >=');
        expect(countCall[0]).toContain('created_at <');
      }
    });

    it('ignores invalid date formats', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries();
      const res = await agent.get('/admin/coordinator?date_from=not-a-date');
      expect(res.status).toBe(200);
      const calls = mockQuery.mock.calls;
      const countCall = calls.find((c: string[]) => typeof c[0] === 'string' && c[0].includes('COUNT(*)'));
      expect(countCall).toBeDefined();
      expect(countCall![0]).not.toContain('created_at >=');
    });

    it('renders pagination when more than 20 tasks', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries({ total: 50 });
      const res = await agent.get('/admin/coordinator');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Page 1 of 3');
      expect(res.text).toContain('Next');
    });

    it('renders empty state', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries({ total: 0, tasks: [] });
      const res = await agent.get('/admin/coordinator');
      expect(res.status).toBe(200);
      expect(res.text).toContain('No tasks found.');
    });
  });

  describe('GET /admin/api/coordinator', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/admin/api/coordinator');
      expect(res.status).toBe(302);
    });

    it('returns JSON for admin', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries();
      const res = await agent.get('/admin/api/coordinator');
      expect(res.status).toBe(200);
      expect(res.body.tasks).toHaveLength(1);
      expect(res.body.stats).toBeDefined();
      expect(res.body.stats.completed).toBe(1);
      expect(res.body.agents).toContain('infra_engineer');
      expect(res.body.categories).toContain('build_failure');
    });

    it('passes filters through to JSON API', async () => {
      const agent = await makeAdminAgent();
      mockCoordinatorQueries();
      const res = await agent.get('/admin/api/coordinator?status=failed&agent=backend_engineer');
      expect(res.status).toBe(200);
      const calls = mockQuery.mock.calls;
      const countCall = calls.find((c: string[]) => typeof c[0] === 'string' && c[0].includes('COUNT(*)'));
      expect(countCall).toBeTruthy();
      if (countCall) {
        expect(countCall[0]).toContain('agent = $');
        expect(countCall[0]).toContain('status = $');
      }
    });
  });
});

describe('Ops API Coordinator', () => {
  // Synthetic fixture, built at runtime so no key-shaped literal appears in source.
  const TEST_API_KEY = 'ops_test_' + 'key_abc123';

  beforeEach(() => {
    mockQuery.mockReset();
    process.env.OPS_API_KEY = TEST_API_KEY;
  });

  describe('GET /ops/api/v1/coordinator', () => {
    it('returns 401 without auth', async () => {
      const res = await request(app).get('/ops/api/v1/coordinator');
      expect(res.status).toBe(401);
    });

    it('returns coordinator data with valid API key', async () => {
      // Mock the 5 queries
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '2' }] });
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: 't1', tick_id: 'tk1', agent: 'infra_engineer', task_category: 'build_failure',
            task_description: 'Fix build', status: 'completed', actual_cost_usd: 0.03,
            created_at: new Date().toISOString(), completed_at: new Date().toISOString(), duration_s: 5,
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({
        rows: [{ total: '2', pending: '0', running: '0', completed: '2', failed: '0', skipped: '0' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ agent: 'infra_engineer' }] });
      mockQuery.mockResolvedValueOnce({ rows: [{ task_category: 'build_failure' }] });

      const res = await request(app)
        .get('/ops/api/v1/coordinator')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);
      expect(res.status).toBe(200);
      expect(res.body.tasks).toHaveLength(1);
      expect(res.body.stats.completed).toBe(2);
    });

    it('supports filter params', async () => {
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '0' }] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({
        rows: [{ total: '0', pending: '0', running: '0', completed: '0', failed: '0', skipped: '0' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .get('/ops/api/v1/coordinator?agent=backend_engineer&status=failed')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);
      expect(res.status).toBe(200);
      expect(res.body.tasks).toHaveLength(0);
    });
  });
});
