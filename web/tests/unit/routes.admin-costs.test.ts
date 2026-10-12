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
  getAnthropicCosts: vi.fn().mockResolvedValue(null), // default: no API data, use DB fallback
  getAnthropicCostsAndStatus: vi.fn().mockResolvedValue({
    rows: null,
    partial: false,
    partialReason: null,
  }),
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

// Mock cost query results (5 queries: daily, per-build, per-tier, per-agent, platform)
function mockCostQueries(platformRows: Array<{ date: string; total_usd: number }> = []) {
  const todayStr = new Date().toISOString().slice(0, 10);
  // Query 1: daily breakdown
  mockQuery.mockResolvedValueOnce({
    rows: [
      { date: todayStr, build_cost: 5.25, agent_cost: 0.50, total: 5.75, build_count: 2 },
    ],
  });
  // Query 2: per-build
  mockQuery.mockResolvedValueOnce({
    rows: [
      { id: 'b1', app_name: 'test-app', tier: 'build', cost: 3.00, status: 'live', completed_at: new Date().toISOString() },
    ],
  });
  // Query 3: per-tier
  mockQuery.mockResolvedValueOnce({
    rows: [{ tier: 'build', count: 2, total_cost: 5.25, avg_cost: 2.63 }],
  });
  // Query 4: per-agent
  mockQuery.mockResolvedValueOnce({
    rows: [{ agent: 'infra_engineer', count: 3, total_cost: 0.50, avg_cost: 0.17 }],
  });
  // Query 5: platform_costs
  mockQuery.mockResolvedValueOnce({
    rows: platformRows,
  });
}

describe('Admin Cost Dashboard', () => {
  const origEnv = process.env.ADMIN_EMAIL;

  beforeEach(() => {
    mockQuery.mockReset();
    process.env.ADMIN_EMAIL = 'founder@example.com';
  });

  afterEach(() => {
    process.env.ADMIN_EMAIL = origEnv;
  });

  describe('GET /admin/costs', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/admin/costs');
      expect(res.status).toBe(302);
      expect(res.headers.location).toMatch(/\/login/);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await makeNonAdminAgent();
      const res = await agent.get('/admin/costs');
      expect(res.status).toBe(403);
    });

    it('returns 200 for admin with cost data', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/costs');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Period Total');
      expect(res.text).toContain('$5.25');
      // Today's Spend was removed (always $0 due to 24h billing lag)
      expect(res.text).not.toContain("Today's Spend");
    });

    it('accepts range parameter', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/costs?range=7');
      expect(res.status).toBe(200);
      expect(res.text).toContain('value="7" selected');
    });

    it('defaults range to 30 for invalid input', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/costs?range=invalid');
      expect(res.status).toBe(200);
      expect(res.text).toContain('value="30" selected');
    });

    it('clamps range to max 90', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/costs?range=999');
      expect(res.status).toBe(200);
      expect(res.text).toContain('value="90" selected');
    });
  });

  describe('GET /admin/api/costs', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app).get('/admin/api/costs');
      expect(res.status).toBe(302);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await makeNonAdminAgent();
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(403);
    });

    it('returns JSON with correct shape for admin', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('summary');
      expect(res.body).toHaveProperty('daily');
      expect(res.body).toHaveProperty('builds');
      expect(res.body).toHaveProperty('per_tier');
      expect(res.body).toHaveProperty('per_agent');
      expect(res.body.summary).toHaveProperty('today_build');
      expect(res.body.summary).toHaveProperty('today_agent');
      expect(res.body.summary).toHaveProperty('today_total');
      expect(res.body.summary).toHaveProperty('today_platform');
      expect(res.body.summary).toHaveProperty('today_untracked');
      expect(res.body.summary).toHaveProperty('period_total');
      expect(res.body.summary).toHaveProperty('period_platform');
      expect(res.body.summary).toHaveProperty('period_untracked');
      expect(res.body.summary).toHaveProperty('avg_per_build');
      expect(res.body.summary).toHaveProperty('avg_platform_per_day');
      expect(res.body.summary).toHaveProperty('period_build_total');
      expect(res.body.summary).toHaveProperty('daily_budget');
    });

    it('computes avg_per_build using only build costs', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      // build_cost = 5.25, build_count = 2, so avg = 5.25 / 2 = 2.625
      expect(res.body.summary.avg_per_build).toBeCloseTo(2.625, 2);
      // period_build_total should be just build costs
      expect(res.body.summary.period_build_total).toBeCloseTo(5.25, 2);
    });

    it('computes avg_platform_per_day from days with platform data', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const agent = await makeAdminAgent();
      mockCostQueries([{ date: todayStr, total_usd: 40.00 }]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      // Only 1 day has platform data ($40), so avg = $40
      expect(res.body.summary.avg_platform_per_day).toBeCloseTo(40, 1);
    });

    it('defaults budget to 20 when env var is invalid', async () => {
      process.env.COORDINATOR_DAILY_BUDGET = 'not_a_number';
      const agent = await makeAdminAgent();
      mockCostQueries();
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.summary.daily_budget).toBe(20);
      delete process.env.COORDINATOR_DAILY_BUDGET;
    });

    it('computes untracked costs from platform data', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const agent = await makeAdminAgent();
      // Platform total = $10, tracked = $5.75 (build 5.25 + agent 0.50), untracked = $4.25
      mockCostQueries([{ date: todayStr, total_usd: 10.00 }]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.summary.today_platform).toBe(10);
      expect(res.body.summary.today_untracked).toBeCloseTo(4.25, 1);
      expect(res.body.daily[0].platform_usd).toBe(10);
      expect(res.body.daily[0].untracked).toBeCloseTo(4.25, 1);
    });

    it('returns zero untracked when no platform data', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries([]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.summary.today_platform).toBe(0);
      expect(res.body.summary.today_untracked).toBe(0);
    });

    it('includes cost_breakdown when builds have advisor metrics', async () => {
      const agent = await makeAdminAgent();
      const todayStr = new Date().toISOString().slice(0, 10);

      // Query 1: daily breakdown
      mockQuery.mockResolvedValueOnce({
        rows: [{ date: todayStr, build_cost: 3.00, agent_cost: 0.00, total: 3.00, build_count: 1 }],
      });
      // Query 2: per-build with cost_breakdown JSONB
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'b1',
          app_name: 'advisor-test-app',
          tier: 'build',
          cost: 3.00,
          status: 'live',
          completed_at: new Date().toISOString(),
          cost_breakdown: {
            fix_attempt_1: {
              advisor_model: 'claude-opus-4-6',
              advisor_input_tokens: 10000,
              advisor_output_tokens: 1000,
              advisor_cost_usd: 0.075,
              executor_model: 'claude-sonnet-4-6',
              executor_cost_usd: 0.150,
              total_cost_usd: 0.225,
            },
          },
        }],
      });
      // Query 3: per-tier
      mockQuery.mockResolvedValueOnce({ rows: [{ tier: 'build', count: 1, total_cost: 3.00, avg_cost: 3.00 }] });
      // Query 4: per-agent
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // Query 5: platform_costs
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.builds[0]).toHaveProperty('cost_breakdown');
      expect(res.body.builds[0].cost_breakdown).toHaveProperty('fix_attempt_1');
      expect(res.body.builds[0].cost_breakdown.fix_attempt_1.advisor_model).toBe('claude-opus-4-6');
    });

    it('handles null cost_breakdown gracefully', async () => {
      const agent = await makeAdminAgent();
      const todayStr = new Date().toISOString().slice(0, 10);

      // Query 1: daily
      mockQuery.mockResolvedValueOnce({
        rows: [{ date: todayStr, build_cost: 2.00, agent_cost: 0.00, total: 2.00, build_count: 1 }],
      });
      // Query 2: per-build with null cost_breakdown
      mockQuery.mockResolvedValueOnce({
        rows: [{
          id: 'b2',
          app_name: 'legacy-app',
          tier: 'build',
          cost: 2.00,
          status: 'live',
          completed_at: new Date().toISOString(),
          cost_breakdown: null,
        }],
      });
      // Query 3: per-tier
      mockQuery.mockResolvedValueOnce({ rows: [{ tier: 'build', count: 1, total_cost: 2.00, avg_cost: 2.00 }] });
      // Query 4: per-agent
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // Query 5: platform_costs
      mockQuery.mockResolvedValueOnce({ rows: [] });

      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      const breakdown = res.body.builds[0].cost_breakdown;
      expect(typeof breakdown).toBe('object');
      expect(Object.keys(breakdown).length).toBe(0);
    });

    it('clamps untracked to zero when platform < tracked', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const agent = await makeAdminAgent();
      // Platform = $2 but tracked = $5.75, untracked should be 0 not negative
      mockCostQueries([{ date: todayStr, total_usd: 2.00 }]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.summary.today_untracked).toBe(0);
    });

    // ─── Day Total math regression tests ───────────────────
    // Day Total = max(tracked, platform_usd), NOT their sum. The platform
    // total already includes the tracked build/agent calls (Anthropic
    // console reports gross org spend), so summing them double-counts the
    // tracked portion. These tests pin the corrected math against re-
    // introduction of the double-count bug.

    it('day total uses max(tracked, platform), not their sum (no double-count)', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const agent = await makeAdminAgent();
      // tracked = build 5.25 + agent 0.50 = 5.75, platform = 10
      // Old buggy total: 5.75 + 10 = 15.75 (double-counted)
      // Corrected total: max(5.75, 10) = 10
      mockCostQueries([{ date: todayStr, total_usd: 10.00 }]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.daily[0].total).toBeCloseTo(10.00, 2);
      expect(res.body.daily[0].total).not.toBeCloseTo(15.75, 2);
    });

    it('day total falls back to tracked when platform > tracked is false', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const agent = await makeAdminAgent();
      // tracked = 5.75, platform = 2 (CAS overestimates relative to billing
      // reconciliation lag). max(5.75, 2) = 5.75.
      mockCostQueries([{ date: todayStr, total_usd: 2.00 }]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.daily[0].total).toBeCloseTo(5.75, 2);
    });

    it('day total equals tracked when platform_usd is 0 (not yet posted)', async () => {
      const agent = await makeAdminAgent();
      mockCostQueries([]); // no platform row -> platform_usd = 0
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.daily[0].total).toBeCloseTo(5.75, 2);
    });

    it('today_total summary stat mirrors the daily row total, not build+agent+platform', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const agent = await makeAdminAgent();
      // Same shape as the day-total test above; 5.75 tracked, 10 platform.
      // Corrected today_total = max(5.75, 10) = 10. Old buggy: 15.75.
      mockCostQueries([{ date: todayStr, total_usd: 10.00 }]);
      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.summary.today_total).toBeCloseTo(10.00, 2);
      expect(res.body.summary.today_total).not.toBeCloseTo(15.75, 2);
      // And it should match the daily row's total exactly
      expect(res.body.summary.today_total).toBeCloseTo(res.body.daily[0].total, 4);
    });

    it('period total sums daily totals using corrected max math', async () => {
      // Mock 2 days. Day 1: tracked 5.75, platform 10 -> total 10.
      // Day 2: tracked 5.75, platform 2 -> total 5.75.
      // Period total: 15.75 (NOT 5.75+5.75+10+2 = 23.5 buggy).
      const today = new Date();
      const yesterday = new Date(today.getTime() - 86400000);
      const todayStr = today.toISOString().slice(0, 10);
      const yesterdayStr = yesterday.toISOString().slice(0, 10);
      const agent = await makeAdminAgent();

      // Daily query returns 2 rows
      mockQuery.mockResolvedValueOnce({
        rows: [
          { date: todayStr, build_cost: 5.25, agent_cost: 0.50, total: 5.75, build_count: 2 },
          { date: yesterdayStr, build_cost: 5.25, agent_cost: 0.50, total: 5.75, build_count: 2 },
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // builds
      mockQuery.mockResolvedValueOnce({ rows: [] }); // per-tier
      mockQuery.mockResolvedValueOnce({ rows: [] }); // per-agent
      mockQuery.mockResolvedValueOnce({
        rows: [
          { date: todayStr, total_usd: 10.00 },
          { date: yesterdayStr, total_usd: 2.00 },
        ],
      });

      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.summary.period_total).toBeCloseTo(15.75, 2);
    });
  });

  describe('Anthropic Admin API integration', () => {
    it('uses API data when available instead of platform_costs table', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      const { getAnthropicCostsAndStatus } = await import('../../src/services/anthropic-costs');
      const mockGetCostsAndStatus = getAnthropicCostsAndStatus as ReturnType<typeof vi.fn>;

      // API returns $25 for today
      mockGetCostsAndStatus.mockResolvedValueOnce({
        rows: [
          { date: todayStr, total_usd: 25.00, by_model: { 'claude-opus-4-6': 20, 'claude-sonnet-4-6': 5 } },
        ],
        partial: false,
        partialReason: null,
      });

      const agent = await makeAdminAgent();
      // platform_costs table returns $10 (should be ignored when API works)
      mockCostQueries([{ date: todayStr, total_usd: 10.00 }]);

      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      // API value ($25) should win over DB value ($10)
      expect(res.body.summary.today_platform).toBe(25);
      // untracked = 25 - 5.75 = 19.25
      expect(res.body.summary.today_untracked).toBeCloseTo(19.25, 1);
      // API succeeded -> response surface flags non-partial.
      expect(res.body.platform_costs_partial).toBe(false);
      expect(res.body.platform_costs_partial_reason).toBeNull();

      // Reset mock to default
      mockGetCostsAndStatus.mockResolvedValue({ rows: null, partial: false, partialReason: null });
    });

    it('surfaces platform_costs_partial=true with reason when Admin API returns partial data', async () => {
      // Regression guard for bug-ops-api-costs-endpoint-underreports
      // (2026-05-18): when the Admin API errors mid-pagination, the
      // dashboard JSON response must carry partial=true + a reason so the
      // UI / ops API can warn that the period total is a lower bound and
      // not silently render a "false-low" headline.
      const todayStr = new Date().toISOString().slice(0, 10);
      const { getAnthropicCostsAndStatus } = await import('../../src/services/anthropic-costs');
      const mockGetCostsAndStatus = getAnthropicCostsAndStatus as ReturnType<typeof vi.fn>;

      mockGetCostsAndStatus.mockResolvedValueOnce({
        rows: [{ date: todayStr, total_usd: 12.5, by_model: { 'claude-opus-4-6': 12.5 } }],
        partial: true,
        partialReason: 'mid_pagination_error_status_500_on_page_3_of_at_least_4',
      });

      const agent = await makeAdminAgent();
      mockCostQueries([]);

      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.platform_costs_partial).toBe(true);
      expect(res.body.platform_costs_partial_reason).toContain('mid_pagination_error');
      expect(res.body.platform_costs_partial_reason).toContain('500');

      // Reset mock to default
      mockGetCostsAndStatus.mockResolvedValue({ rows: null, partial: false, partialReason: null });
    });

    it('flags partial when Admin API unavailable and falls back to scraped platform_costs', async () => {
      // When apiResult.rows is null, the code falls back to the scraped
      // platform_costs rows. That fallback is by definition not the
      // authoritative Admin API total, so the response must still flag
      // platform_costs_partial=true with a descriptive reason.
      const todayStr = new Date().toISOString().slice(0, 10);
      const { getAnthropicCostsAndStatus } = await import('../../src/services/anthropic-costs');
      const mockGetCostsAndStatus = getAnthropicCostsAndStatus as ReturnType<typeof vi.fn>;

      mockGetCostsAndStatus.mockResolvedValueOnce({
        rows: null,
        partial: false,
        partialReason: null,
      });

      const agent = await makeAdminAgent();
      mockCostQueries([{ date: todayStr, total_usd: 10.0 }]);

      const res = await agent.get('/admin/api/costs');
      expect(res.status).toBe(200);
      expect(res.body.platform_costs_partial).toBe(true);
      expect(res.body.platform_costs_partial_reason).toMatch(/admin_api|scraped/);

      // Reset mock to default
      mockGetCostsAndStatus.mockResolvedValue({ rows: null, partial: false, partialReason: null });
    });
  });

  describe('POST /admin/api/costs/platform', () => {
    it('redirects to login when not authenticated', async () => {
      const res = await request(app)
        .post('/admin/api/costs/platform')
        .send({ date: '2026-04-01', total_usd: 10.00 });
      expect(res.status).toBe(302);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await makeNonAdminAgent();
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/api/costs/platform')
        .type('form')
        .send({ _csrf: csrf, date: '2026-04-01', total_usd: 10.00 });
      expect(res.status).toBe(403);
    });

    it('records platform cost for admin', async () => {
      const agent = await makeAdminAgent();
      const csrf = getValidCsrf();
      // Mock the INSERT query
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      const res = await agent
        .post('/admin/api/costs/platform')
        .type('form')
        .send({ _csrf: csrf, date: '2026-04-01', total_usd: '12.50' });
      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(res.body.total_usd).toBe(12.5);
    });

    it('rejects invalid date format', async () => {
      const agent = await makeAdminAgent();
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/api/costs/platform')
        .type('form')
        .send({ _csrf: csrf, date: 'not-a-date', total_usd: '10' });
      expect(res.status).toBe(400);
      expect(res.body.error).toContain('date');
    });

    it('rejects negative cost', async () => {
      const agent = await makeAdminAgent();
      const csrf = getValidCsrf();
      const res = await agent
        .post('/admin/api/costs/platform')
        .type('form')
        .send({ _csrf: csrf, date: '2026-04-01', total_usd: '-5' });
      expect(res.status).toBe(400);
      expect(res.body.error).toContain('non-negative');
    });
  });
});
