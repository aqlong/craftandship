import { describe, it, expect, vi, beforeEach } from 'vitest';
import request from 'supertest';
import app from '../../src/app';

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

// Mock anthropic-costs service
vi.mock('../../src/services/anthropic-costs', () => ({
  getAnthropicCosts: vi.fn().mockResolvedValue(null),
  getAnthropicCostsAndStatus: vi.fn().mockResolvedValue({ rows: null, partial: false, partialReason: null }),
  rebuildAnthropicCostsCache: vi.fn().mockResolvedValue({ rowsFetched: 0, success: true }),
}));

// Mock Anthropic SDK
vi.mock('@anthropic-ai/sdk', () => {
  return {
    default: class MockAnthropic {
      messages = { create: vi.fn() };
    },
  };
});

import pool from '../../src/db/index';

const mockPool = pool as unknown as { query: ReturnType<typeof vi.fn> };
// Synthetic fixture, built at runtime so no key-shaped literal appears in source.
const TEST_API_KEY = 'ops_test_' + 'key_abc123';

describe('Ops API', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    process.env.OPS_API_KEY = TEST_API_KEY;
  });

  describe('Authentication', () => {
    it('returns 401 without Authorization header', async () => {
      const res = await request(app).get('/ops/api/v1/stats');
      expect(res.status).toBe(401);
      expect(res.body.error).toBe('Missing or invalid API key.');
    });

    it('returns 401 with wrong token', async () => {
      const res = await request(app)
        .get('/ops/api/v1/stats')
        .set('Authorization', 'Bearer wrong_key');
      expect(res.status).toBe(401);
      expect(res.body.error).toBe('Missing or invalid API key.');
    });

    it('returns 401 with non-Bearer auth', async () => {
      const res = await request(app)
        .get('/ops/api/v1/stats')
        .set('Authorization', 'Basic abc123');
      expect(res.status).toBe(401);
    });

    it('returns 503 when OPS_API_KEY is not configured', async () => {
      delete process.env.OPS_API_KEY;
      const res = await request(app)
        .get('/ops/api/v1/stats')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);
      expect(res.status).toBe(503);
      expect(res.body.error).toBe('Ops API not configured.');
    });
  });

  describe('GET /ops/api/v1/stats', () => {
    it('returns stats with valid API key', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ total: '5', queued: '1', building: '0', live: '3', failed: '1', failed_last_24h: '0' }] })
        .mockResolvedValueOnce({ rows: [{ total: '2', with_live_apps: '1' }] })
        .mockResolvedValueOnce({ rows: [{ sent_today: '3', failed_today: '0' }] })
        .mockResolvedValueOnce({ rows: [{ daily_total: '2.5000', weekly_total: '15.0000', avg_cost_build: '1.2000', avg_cost_grow: '3.0000', avg_cost_scale: '5.0000', top_cost_build_id: 'b1', top_cost_amount: '8.0000' }] });

      const res = await request(app)
        .get('/ops/api/v1/stats')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.builds.total).toBe('5');
      expect(res.body.customers.total).toBe('2');
      expect(res.body.emails.sent_today).toBe('3');
      expect(res.body.api_costs).toBeDefined();
      expect(res.body.api_costs.daily_total).toBe('2.5000');
      expect(res.body.api_costs.weekly_total).toBe('15.0000');
    });

    it('handles api_costs when no builds have cost data', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ total: '5', queued: '0', building: '0', live: '5', failed: '0', failed_last_24h: '0' }] })
        .mockResolvedValueOnce({ rows: [{ total: '3', with_live_apps: '3' }] })
        .mockResolvedValueOnce({ rows: [{ sent_today: '0', failed_today: '0' }] })
        .mockResolvedValueOnce({ rows: [{ daily_total: null, weekly_total: null, avg_cost_build: null, avg_cost_grow: null, avg_cost_scale: null, top_cost_build_id: null, top_cost_amount: null }] });

      const res = await request(app)
        .get('/ops/api/v1/stats')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.api_costs).toBeDefined();
      expect(res.body.api_costs.daily_total).toBeNull();
      expect(res.body.api_costs.weekly_total).toBeNull();
    });
  });

  describe('GET /ops/api/v1/builds', () => {
    it('returns builds list', async () => {
      mockPool.query
        .mockResolvedValueOnce({
          rows: [
            { id: 'b1', app_name: 'test-app', status: 'live', customer_email: 'user@example.test' },
          ],
        })
        .mockResolvedValueOnce({ rows: [{ total: '1', queued: '0', building: '0', testing: '0', deploying: '0', live: '1', failed: '0' }] });

      const res = await request(app)
        .get('/ops/api/v1/builds')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.builds).toHaveLength(1);
      expect(res.body.builds[0].app_name).toBe('test-app');
      expect(res.body.stats).toBeDefined();
    });

    it('filters by status', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ id: 'b1', status: 'failed', error_message: 'test error' }] })
        .mockResolvedValueOnce({ rows: [{ total: '3', failed: '1' }] });

      const res = await request(app)
        .get('/ops/api/v1/builds?status=failed')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.builds[0].status).toBe('failed');
    });

    it('includes review_status in list rows', async () => {
      mockPool.query
        .mockResolvedValueOnce({
          rows: [
            { id: 'b1', app_name: 'a', status: 'preview', review_status: 'pending', customer_email: 'u@example.test' },
            { id: 'b2', app_name: 'b', status: 'live', review_status: 'approved', customer_email: 'u@example.test' },
          ],
        })
        .mockResolvedValueOnce({ rows: [{ total: '2' }] });

      const res = await request(app)
        .get('/ops/api/v1/builds')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.builds[0].review_status).toBe('pending');
      expect(res.body.builds[1].review_status).toBe('approved');

      const sql = mockPool.query.mock.calls[0][0] as string;
      expect(sql).toMatch(/b\.review_status/);
    });
  });

  describe('GET /ops/api/v1/builds/:buildId', () => {
    it('returns build detail', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'bookmark-manager', status: 'failed', error_message: 'Test suite failed', customer_email: 'user@example.test' }],
      });

      const res = await request(app)
        .get('/ops/api/v1/builds/b1')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.build.app_name).toBe('bookmark-manager');
      expect(res.body.build.error_message).toBe('Test suite failed');
    });

    it('includes total_api_cost_usd when cost data exists', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'cost-tracker', status: 'live', total_api_cost_usd: '3.7500', customer_email: 'user@example.test' }],
      });

      const res = await request(app)
        .get('/ops/api/v1/builds/b1')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.build.total_api_cost_usd).toBe('3.7500');
    });

    it('returns null total_api_cost_usd for historical builds', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b-old', app_name: 'legacy-app', status: 'live', total_api_cost_usd: null, customer_email: 'old@example.test' }],
      });

      const res = await request(app)
        .get('/ops/api/v1/builds/b-old')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.build.total_api_cost_usd).toBeNull();
    });

    it('returns 404 for unknown build', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .get('/ops/api/v1/builds/nonexistent')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(404);
    });

    it('returns review_status, reviewed_at, reviewed_by, review_notes', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{
          id: 'b1',
          app_name: 'reviewed-app',
          status: 'preview',
          review_status: 'approved',
          reviewed_at: '2026-04-12T10:00:00Z',
          reviewed_by: 'founder@example.com',
          review_notes: 'LGTM',
          customer_email: 'user@example.test',
        }],
      });

      const res = await request(app)
        .get('/ops/api/v1/builds/b1')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.build.review_status).toBe('approved');
      expect(res.body.build.reviewed_at).toBe('2026-04-12T10:00:00Z');
      expect(res.body.build.reviewed_by).toBe('founder@example.com');
      expect(res.body.build.review_notes).toBe('LGTM');

      const sql = mockPool.query.mock.calls[0][0] as string;
      expect(sql).toMatch(/b\.review_status/);
      expect(sql).toMatch(/b\.reviewed_at/);
      expect(sql).toMatch(/b\.reviewed_by/);
      expect(sql).toMatch(/b\.review_notes/);
    });
  });

  describe('POST /ops/api/v1/builds/:buildId/requeue', () => {
    it('requeues a failed build', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'bookmark-manager', status: 'queued' }],
      });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.requeued).toBe(true);
      expect(res.body.build.app_name).toBe('bookmark-manager');
    });

    it('returns 404 for non-failed build', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(404);
      expect(res.body.error).toContain('not in failed/preview state');
    });
  });

  // A review verdict belongs to the deployment that was reviewed. Each of the
  // three ops statements that put an existing build row back in the queue
  // must clear it, or the rebuilt site would come back already "approved".
  describe('requeue paths clear the review verdict', () => {
    const REVIEW_RESET = [
      'review_status = NULL',
      'reviewed_at = NULL',
      'reviewed_by = NULL',
      'review_notes = NULL',
      'review_report = NULL',
      'ai_review_iterations = 0',
      'ai_reviewed_at = NULL',
    ];
    const lastStatement = (): [string, unknown[]] => {
      const calls = mockPool.query.mock.calls;
      const call = calls[calls.length - 1];
      return [String(call[0]), call[1] as unknown[]];
    };
    const expectQueuedWithReviewReset = (sql: string): void => {
      expect(sql).toMatch(/UPDATE builds\s+SET status = 'queued'/);
      for (const assignment of REVIEW_RESET) expect(sql).toContain(assignment);
    };

    it('POST /requeue (failed or preview)', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [{ id: 'b1', app_name: 'site', status: 'queued' }] });
      const res = await request(app).post('/ops/api/v1/builds/b1/requeue').set('Authorization', `Bearer ${TEST_API_KEY}`);
      expect(res.status).toBe(200);
      const [sql, params] = lastStatement();
      expectQueuedWithReviewReset(sql);
      expect(sql).toMatch(/WHERE id = \$1 AND status IN \('failed', 'preview'\)/);
      expect(params).toEqual(['b1']);
    });

    it('POST /requeue-from-stage', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ id: 'b1', app_name: 'site', status: 'failed', has_artifact: true }] })
        .mockResolvedValueOnce({ rows: [{ id: 'b1', app_name: 'site', status: 'queued' }] });
      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'deploying' });
      expect(res.status).toBe(200);
      const [sql, params] = lastStatement();
      expectQueuedWithReviewReset(sql);
      // Guarded on the state that was read, so it cannot pull back a build the worker has since claimed.
      expect(sql).toMatch(/WHERE id = \$1 AND status = 'failed'/);
      expect(params).toEqual(['b1', 'Requeued: resuming from deploying stage']);
    });

    it('POST /requeue-from-stage answers 409 when the build left the failed state between the read and the write', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ id: 'b1', app_name: 'site', status: 'failed', has_artifact: true }] })
        .mockResolvedValueOnce({ rows: [] });
      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'testing' });
      expect(res.status).toBe(409);
      expect(res.body.requeued).toBeUndefined();
    });

    it('POST /queue-bypass', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'site', status: 'queued', tier: 'build', customer_id: 'c1' }],
      });
      const res = await request(app).post('/ops/api/v1/builds/b1/queue-bypass').set('Authorization', `Bearer ${TEST_API_KEY}`);
      expect(res.status).toBe(200);
      const [sql] = lastStatement();
      expectQueuedWithReviewReset(sql);
      // Only a build that has a customer: the worker claims through a join to
      // customers, so a queued build without one would never be built or failed.
      expect(sql).toMatch(/WHERE id = \$1 AND status IN \('payment_pending', 'intake'\) AND customer_id IS NOT NULL/);
    });
  });

  describe('POST /ops/api/v1/builds/:buildId/requeue-from-stage', () => {
    it('requeues a failed build from testing stage', async () => {
      mockPool.query
        .mockResolvedValueOnce({
          rows: [{ id: 'b1', app_name: 'bookmark-manager', status: 'failed', has_artifact: true }],
        })
        .mockResolvedValueOnce({
          rows: [{ id: 'b1', app_name: 'bookmark-manager', status: 'queued' }],
        });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'testing' });

      expect(res.status).toBe(200);
      expect(res.body.requeued).toBe(true);
      expect(res.body.stage).toBe('testing');
    });

    it('requeues from deploying stage', async () => {
      mockPool.query
        .mockResolvedValueOnce({
          rows: [{ id: 'b1', app_name: 'green-grow', status: 'failed', has_artifact: true }],
        })
        .mockResolvedValueOnce({
          rows: [{ id: 'b1', app_name: 'green-grow', status: 'queued' }],
        });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'deploying' });

      expect(res.status).toBe(200);
      expect(res.body.stage).toBe('deploying');
    });

    it('rejects invalid stage', async () => {
      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'building' });

      expect(res.status).toBe(400);
      expect(res.body.error).toContain('Invalid stage');
    });

    it('rejects non-failed build', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'test', status: 'live', has_artifact: true }],
      });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'testing' });

      expect(res.status).toBe(400);
      expect(res.body.error).toContain("not 'failed'");
    });

    it('rejects build without artifact', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'test', status: 'failed', has_artifact: false }],
      });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'testing' });

      expect(res.status).toBe(400);
      expect(res.body.error).toContain('no stored artifact');
    });

    it('returns 404 for nonexistent build', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/builds/nonexistent/requeue-from-stage')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ stage: 'testing' });

      expect(res.status).toBe(404);
    });
  });

  describe('POST /ops/api/v1/builds/:buildId/fail', () => {
    it('force-fails an active build', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'green-grow', status: 'failed' }],
      });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/fail')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ reason: 'Stuck in testing' });

      expect(res.status).toBe(200);
      expect(res.body.failed).toBe(true);
      expect(res.body.build.app_name).toBe('green-grow');
    });

    it('returns 404 for non-active build', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/fail')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(404);
      expect(res.body.error).toContain('not in an active state');
    });
  });

  describe('GET /ops/api/v1/costs', () => {
    it('returns 401 without auth', async () => {
      const res = await request(app).get('/ops/api/v1/costs');
      expect(res.status).toBe(401);
    });

    it('returns cost data with valid auth', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ date: todayStr, build_cost: 3.50, agent_cost: 0.25, total: 3.75, build_count: 1 }] })
        .mockResolvedValueOnce({ rows: [{ id: 'b1', app_name: 'test', tier: 'build', cost: 3.50, status: 'live', completed_at: new Date().toISOString() }] })
        .mockResolvedValueOnce({ rows: [{ tier: 'build', count: 1, total_cost: 3.50, avg_cost: 3.50 }] })
        .mockResolvedValueOnce({ rows: [{ agent: 'infra_engineer', count: 2, total_cost: 0.25, avg_cost: 0.13 }] })
        .mockResolvedValueOnce({ rows: [] }); // platform_costs

      const res = await request(app)
        .get('/ops/api/v1/costs')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body).toHaveProperty('summary');
      expect(res.body).toHaveProperty('daily');
      expect(res.body).toHaveProperty('builds');
      expect(res.body).toHaveProperty('per_tier');
      expect(res.body).toHaveProperty('per_agent');
      expect(res.body.summary.today_total).toBeCloseTo(3.75, 1);
      expect(res.body.summary).toHaveProperty('today_platform');
      expect(res.body.summary).toHaveProperty('today_untracked');
    });

    it('accepts days parameter', async () => {
      const todayStr = new Date().toISOString().slice(0, 10);
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ date: todayStr, build_cost: 0, agent_cost: 0, total: 0, build_count: 0 }] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] }); // platform_costs

      const res = await request(app)
        .get('/ops/api/v1/costs?days=7')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.summary).toBeDefined();
    });
  });

  describe('POST /ops/api/v1/costs/platform', () => {
    it('returns 401 without auth', async () => {
      const res = await request(app)
        .post('/ops/api/v1/costs/platform')
        .send({ date: '2026-04-01', total_usd: 10 });
      expect(res.status).toBe(401);
    });

    it('records platform cost with valid auth', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      const res = await request(app)
        .post('/ops/api/v1/costs/platform')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ date: '2026-04-01', total_usd: 15.50 });
      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(res.body.total_usd).toBe(15.5);
    });

    it('rejects invalid date', async () => {
      const res = await request(app)
        .post('/ops/api/v1/costs/platform')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ date: 'bad', total_usd: 10 });
      expect(res.status).toBe(400);
    });

    it('rejects negative amount', async () => {
      const res = await request(app)
        .post('/ops/api/v1/costs/platform')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ date: '2026-04-01', total_usd: -5 });
      expect(res.status).toBe(400);
    });
  });

  describe('GET /ops/api/v1/errors', () => {
    it('returns empty array initially', async () => {
      const res = await request(app)
        .get('/ops/api/v1/errors')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.errors).toBeInstanceOf(Array);
    });
  });

  describe('GET /ops/api/v1/analytics', () => {
    it('returns analytics data with funnel, UTM, and page views', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ signups: '10', started_intake: '7', reached_payment: '4', reached_live: '2' }] })
        .mockResolvedValueOnce({ rows: [{ source: 'twitter', medium: 'social', campaign: 'launch', signups: '5' }] })
        .mockResolvedValueOnce({ rows: [{ path: '/', views: '100', unique_sessions: '80' }] })
        .mockResolvedValueOnce({ rows: [{ day: '2026-04-01', views: '50' }] })
        .mockResolvedValueOnce({ rows: [{ source: 'twitter', views: '30' }] });

      const res = await request(app)
        .get('/ops/api/v1/analytics')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.days).toBe(30);
      expect(res.body.funnel.signups).toBe('10');
      expect(res.body.funnel.reached_live).toBe('2');
      expect(res.body.utm_attribution).toHaveLength(1);
      expect(res.body.utm_attribution[0].source).toBe('twitter');
      expect(res.body.top_pages).toHaveLength(1);
      expect(res.body.daily_views).toHaveLength(1);
      expect(res.body.utm_traffic).toHaveLength(1);
    });

    it('accepts days parameter', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ signups: '0', started_intake: '0', reached_payment: '0', reached_live: '0' }] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .get('/ops/api/v1/analytics?days=7')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.days).toBe(7);
    });

    it('clamps days to max 90', async () => {
      mockPool.query
        .mockResolvedValueOnce({ rows: [{ signups: '0', started_intake: '0', reached_payment: '0', reached_live: '0' }] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] })
        .mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .get('/ops/api/v1/analytics?days=200')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.days).toBe(90);
    });

    it('returns 401 without auth', async () => {
      const res = await request(app).get('/ops/api/v1/analytics');
      expect(res.status).toBe(401);
    });

    it('handles DB errors gracefully', async () => {
      mockPool.query.mockRejectedValueOnce(new Error('DB connection lost'));

      const res = await request(app)
        .get('/ops/api/v1/analytics')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(500);
      expect(res.body.error).toBe('Failed to fetch analytics.');
    });
  });

  describe('POST /ops/api/v1/builds/:buildId/queue-bypass', () => {
    it('moves a payment_pending build to queued', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'my-app', status: 'queued', tier: 'build', customer_id: 'c1' }],
      });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/queue-bypass')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.queued).toBe(true);
      expect(res.body.build.status).toBe('queued');
      expect(res.body.build.app_name).toBe('my-app');
    });

    it('moves an intake build to queued', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b2', app_name: 'intake-app', status: 'queued', tier: 'grow', customer_id: 'c2' }],
      });

      const res = await request(app)
        .post('/ops/api/v1/builds/b2/queue-bypass')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.queued).toBe(true);
    });

    it('returns 404 when build is not in payment_pending or intake state', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/queue-bypass')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(404);
      expect(res.body.error).toContain('payment_pending/intake');
    });

    it('returns 401 without API key', async () => {
      const res = await request(app)
        .post('/ops/api/v1/builds/b1/queue-bypass');

      expect(res.status).toBe(401);
    });

    it('handles DB errors gracefully', async () => {
      mockPool.query.mockRejectedValueOnce(new Error('DB connection lost'));

      const res = await request(app)
        .post('/ops/api/v1/builds/b1/queue-bypass')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(500);
      expect(res.body.error).toBe('Failed to queue bypass build.');
    });
  });

  describe('GET /ops/api/v1/builds/:buildId/errors', () => {
    it('returns error detail with recent messages', async () => {
      mockPool.query
        .mockResolvedValueOnce({
          rows: [{ id: 'b1', status: 'failed', error_message: 'Test suite failed; deployment aborted', app_name: 'bookmark-manager' }],
        })
        .mockResolvedValueOnce({
          rows: [
            { role: 'assistant', content: 'Here is the spec...', created_at: '2026-03-20T10:00:00Z' },
          ],
        });

      const res = await request(app)
        .get('/ops/api/v1/builds/b1/errors')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(200);
      expect(res.body.error_message).toContain('Test suite failed');
      expect(res.body.recent_messages).toHaveLength(1);
    });
  });

  describe('POST /ops/api/v1/gallery/:buildId/optin', () => {
    it('toggles gallery opt-in for a live build', async () => {
      mockPool.query.mockResolvedValueOnce({
        rows: [{ id: 'b1', app_name: 'test-app', gallery_opted_in: true }],
      });

      const res = await request(app)
        .post('/ops/api/v1/gallery/b1/optin')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ gallery_description: 'A test app', gallery_category: 'SaaS' });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(res.body.build.gallery_opted_in).toBe(true);
    });

    it('returns 404 for non-live build', async () => {
      mockPool.query.mockResolvedValueOnce({ rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/gallery/b1/optin')
        .set('Authorization', `Bearer ${TEST_API_KEY}`);

      expect(res.status).toBe(404);
    });

    it('requires auth', async () => {
      const res = await request(app)
        .post('/ops/api/v1/gallery/b1/optin');

      expect(res.status).toBe(401);
    });
  });

  describe('POST /ops/api/v1/executor/sweep-zombies', () => {
    it('marks stuck rows failed and returns swept count', async () => {
      mockPool.query.mockResolvedValueOnce({
        rowCount: 3,
        rows: [
          { id: 'r1', roadmap_item_id: 'foo', status: 'running' },
          { id: 'r2', roadmap_item_id: 'bar', status: 'working' },
          { id: 'r3', roadmap_item_id: 'baz', status: 'pr_created' },
        ],
      });

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ max_age_minutes: 60 });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(res.body.swept).toBe(3);
      expect(res.body.max_age_minutes).toBe(60);

      const call = mockPool.query.mock.calls[0];
      const sql = call[0] as string;
      const params = call[1] as unknown[];
      expect(sql).toMatch(/UPDATE executor_runs/);
      expect(sql).toMatch(/status IN \('running', 'working', 'pr_created'\)/);
      expect(sql).toMatch(/COALESCE\(completed_at, started_at, created_at\)/);
      expect(params).toEqual([60]);
    });

    it('returns ok:true with swept:0 when nothing is stuck', async () => {
      mockPool.query.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ max_age_minutes: 60 });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(res.body.swept).toBe(0);
    });

    it('defaults max_age_minutes to 60 when missing', async () => {
      mockPool.query.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({});

      expect(res.status).toBe(200);
      expect(res.body.max_age_minutes).toBe(60);
      expect(mockPool.query.mock.calls[0][1]).toEqual([60]);
    });

    it('clamps absurd max_age_minutes to 24*60 ceiling', async () => {
      mockPool.query.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ max_age_minutes: 999999 });

      expect(res.status).toBe(200);
      expect(res.body.max_age_minutes).toBe(24 * 60);
    });

    it('falls back to 60 on non-numeric input (does not crash)', async () => {
      mockPool.query.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ max_age_minutes: 'banana' });

      expect(res.status).toBe(200);
      expect(res.body.max_age_minutes).toBe(60);
    });

    it('falls back to 60 on zero or negative (treated as invalid)', async () => {
      mockPool.query.mockResolvedValueOnce({ rowCount: 0, rows: [] });

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ max_age_minutes: 0 });

      expect(res.status).toBe(200);
      expect(res.body.max_age_minutes).toBe(60);
    });

    it('returns 500 on DB error', async () => {
      mockPool.query.mockRejectedValueOnce(new Error('connection refused'));

      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send({ max_age_minutes: 60 });

      expect(res.status).toBe(500);
      expect(res.body.error).toBe('Failed to sweep zombies');
    });

    it('requires auth', async () => {
      const res = await request(app)
        .post('/ops/api/v1/executor/sweep-zombies')
        .send({ max_age_minutes: 60 });

      expect(res.status).toBe(401);
    });
  });
  describe('POST /ops/api/v1/builds/cleanup', () => {
    function mockClient(rows: Array<{ id: string; owner_rank: string }>) {
      const client = {
        query: vi.fn((sql: string) => {
          if (sql.includes('FROM builds WHERE app_name')) return Promise.resolve({ rows });
          if (sql.startsWith('DELETE FROM builds')) return Promise.resolve({ rowCount: rows.filter((r) => Number(r.owner_rank) > 1).length });
          return Promise.resolve({ rows: [], rowCount: 0 });
        }),
        release: vi.fn(),
      };
      (pool as unknown as { connect: unknown }).connect = vi.fn().mockResolvedValue(client);
      return client;
    }
    // The ops limiter allows 60 requests a minute per client address and this
    // file is close to that from the default address, so these tests present
    // their own (the app trusts one proxy hop for X-Forwarded-For).
    const post = (body: Record<string, unknown>) =>
      request(app)
        .post('/ops/api/v1/builds/cleanup')
        .set('X-Forwarded-For', '198.51.100.77')
        .set('Authorization', `Bearer ${TEST_API_KEY}`)
        .send(body);

    it('ranks builds per customer, so a same-named build of another customer is never a "duplicate"', async () => {
      // Two customers both named their site "studio". Customer A has three
      // builds, customer B has one. Only A's two older builds may go.
      const client = mockClient([
        { id: 'a-newest', owner_rank: '1' },
        { id: 'b-only', owner_rank: '1' },
        { id: 'a-older', owner_rank: '2' },
        { id: 'a-oldest', owner_rank: '3' },
      ]);

      const res = await post({ app_name: 'studio' });

      expect(res.status).toBe(200);
      expect(res.body.deleted).toBe(2);
      expect(res.body.kept_ids).toEqual(['a-newest', 'b-only']);

      const select = client.query.mock.calls.find((c) => String(c[0]).includes('FROM builds WHERE app_name'));
      expect(String(select?.[0])).toContain('PARTITION BY customer_id');
      const deleteBuilds = client.query.mock.calls.find((c) => String(c[0]).startsWith('DELETE FROM builds'));
      expect(deleteBuilds?.[1]).toEqual([['a-older', 'a-oldest']]);
      // Every statement that deletes or rewrites rows is limited to those ids.
      for (const call of client.query.mock.calls) {
        if (/^(DELETE|UPDATE)/.test(String(call[0]))) {
          expect(call[1]).toEqual([['a-older', 'a-oldest']]);
        }
      }
      expect(client.release).toHaveBeenCalled();
    });

    it('deletes nothing when each customer has a single build of that name', async () => {
      const client = mockClient([
        { id: 'a-only', owner_rank: '1' },
        { id: 'b-only', owner_rank: '1' },
      ]);

      const res = await post({ app_name: 'studio' });

      expect(res.status).toBe(200);
      expect(res.body.deleted).toBe(0);
      expect(client.query.mock.calls.some((c) => String(c[0]).startsWith('DELETE'))).toBe(false);
    });

    it('requires app_name to be a string', async () => {
      expect((await post({ app_name: ['studio'] })).status).toBe(400);
      expect((await post({})).status).toBe(400);
    });

    it('requires the API key', async () => {
      const res = await request(app)
        .post('/ops/api/v1/builds/cleanup')
        .set('X-Forwarded-For', '198.51.100.77')
        .send({ app_name: 'studio' });
      expect(res.status).toBe(401);
    });
  });

  describe('API key comparison', () => {
    it('answers 401, without throwing, for a key of the same length with non-ASCII bytes', async () => {
      const sameLength = String.fromCharCode(0xe9).repeat(TEST_API_KEY.length);
      const res = await request(app)
        .get('/ops/api/v1/stats')
        .set('X-Forwarded-For', '198.51.100.78')
        .set('Authorization', `Bearer ${sameLength}`);
      expect(res.status).toBe(401);
      expect(mockPool.query).not.toHaveBeenCalled();
    });

    it('answers 401 for a key that is a prefix or an extension of the real one', async () => {
      for (const candidate of [TEST_API_KEY.slice(0, -1), TEST_API_KEY + 'x', '']) {
        const res = await request(app)
          .get('/ops/api/v1/stats')
          .set('X-Forwarded-For', '198.51.100.78')
          .set('Authorization', `Bearer ${candidate}`);
        expect(res.status).toBe(401);
      }
    });

    it('fails closed with 503 on every route when the key is not configured, even with a matching empty bearer', async () => {
      delete process.env.OPS_API_KEY;
      for (const [method, path] of [['get', '/ops/api/v1/errors'], ['post', '/ops/api/v1/builds/x/queue-bypass'], ['delete', '/ops/api/v1/builds/x']] as const) {
        const res = await request(app)[method](path)
          .set('X-Forwarded-For', '198.51.100.78')
          .set('Authorization', 'Bearer ');
        expect(res.status).toBe(503);
      }
      expect(mockPool.query).not.toHaveBeenCalled();
    });
  });
});
