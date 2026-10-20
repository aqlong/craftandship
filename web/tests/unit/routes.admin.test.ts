import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

// Set ADMIN_EMAIL before app loads routes
process.env.ADMIN_EMAIL = 'admin@example.com';

// Mock DB pool. The `client.query` returned by `pool.connect()` is a
// thin wrapper that forwards to the current `pool.query` mock at call
// time. This is required because:
//   1. The `POST /admin/builds/:id/delete` describe at line ~387
//      mutates `(pool as any).connect = vi.fn().mockResolvedValue(...)`
//      to install a delete-specific mock client and never restores it,
//      so by the time the dispatch describe runs `pool.connect` is the
//      delete-test's stale mock returning empty rows for everything.
//   2. Across `vi.clearAllMocks` + per-test `mockReset` boundaries the
//      inner-closure spy reference can desync from the exported
//      `default.query`.
// The shared state is hoisted via `vi.hoisted` so the vi.mock factory
// (also hoisted) can read it. Dispatch tests use `restoreMockConnect()`
// in beforeEach to defeat the delete-test pollution.
const mockState = vi.hoisted(() => ({
  query: null as ReturnType<typeof vi.fn> | null,
  connect: null as ReturnType<typeof vi.fn> | null,
}));
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  mockState.query = mockQuery;
  const mockConnect = vi.fn(async () => ({
    query: (...args: unknown[]) =>
      (mockState.query as unknown as (...a: unknown[]) => unknown)(...args),
    release: vi.fn(),
  }));
  mockState.connect = mockConnect;
  return {
    default: {
      query: mockQuery,
      on: vi.fn(),
      connect: mockConnect,
    },
  };
});

// Mock services that other routes depend on
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
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
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello! Describe your app.'),
  analyzeBuildFailure: vi.fn(),
}));
// The model call is stood in for; the module's error class is the real one.
vi.mock('../../src/services/spec_extraction', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/services/spec_extraction')>()),
  extractSpecFromConversation: vi.fn(),
}));
// The translation is stood in for; the address rule the dispatch asks
// (isEngineSafeAdminEmail) is the real one.
vi.mock('../../src/services/harbor_to_compose', async (importOriginal) => ({
  isEngineSafeAdminEmail: (await importOriginal<typeof import('../../src/services/harbor_to_compose')>()).isEngineSafeAdminEmail,
  translateExtractedSpecToV1: vi.fn(),
}));
vi.mock('../../src/services/site_copy_crawl', () => ({
  crawlSite: vi.fn(),
  isPrivateOrLoopbackHost: vi.fn().mockReturnValue(false),
}));
vi.mock('../../src/services/railway', () => {
  class RailwayApiError extends Error {
    readonly status?: number;
    readonly alreadyGone: boolean;
    readonly disabled: boolean;
    constructor(message: string, status?: number, alreadyGone = false, disabled = false) {
      super(message);
      this.name = 'RailwayApiError';
      this.status = status;
      this.alreadyGone = alreadyGone;
      this.disabled = disabled;
    }
  }
  return {
    deleteRailwayProject: vi.fn().mockResolvedValue(undefined),
    readRailwayProjectName: vi.fn().mockResolvedValue('some-app'),
    RailwayApiError,
    isRailwayWriteEnabled: vi.fn().mockReturnValue(true),
  };
});

import pool from '../../src/db/index';
import { analyzeBuildFailure, CircuitBreakerOpenError } from '../../src/services/claude';
import { deleteRailwayProject, readRailwayProjectName, RailwayApiError, isRailwayWriteEnabled } from '../../src/services/railway';
import { extractSpecFromConversation } from '../../src/services/spec_extraction';
import { translateExtractedSpecToV1 } from '../../src/services/harbor_to_compose';
import { crawlSite } from '../../src/services/site_copy_crawl';
import { REVIEW_RESET_ASSIGNMENTS } from '../../src/routes/builds';
const mockAnalyzeBuildFailure = analyzeBuildFailure as ReturnType<typeof vi.fn>;
const mockDeleteRailwayProject = deleteRailwayProject as ReturnType<typeof vi.fn>;
const mockReadRailwayProjectName = readRailwayProjectName as ReturnType<typeof vi.fn>;
const mockIsRailwayWriteEnabled = isRailwayWriteEnabled as ReturnType<typeof vi.fn>;
const mockExtractSpecFromConversation = extractSpecFromConversation as ReturnType<typeof vi.fn>;
const mockTranslateExtractedSpecToV1 = translateExtractedSpecToV1 as ReturnType<typeof vi.fn>;
const mockCrawlSite = crawlSite as ReturnType<typeof vi.fn>;

const mockQuery = pool.query as ReturnType<typeof vi.fn>;

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

async function loginAgent(email: string): Promise<supertest.Agent> {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'customer-1', email, name: 'Test User', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email, password: 'password123' });
  return agent;
}

const emptyLogsResponse = {
  rows: [{ total: '0' }],
};
const emptyDataResponse = { rows: [] };
const emptyTodayResponse = {
  rows: [{ sent: '0', failed: '0', skipped: '0' }],
};

describe('Admin Routes', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockExtractSpecFromConversation.mockReset();
    mockTranslateExtractedSpecToV1.mockReset();
  });

  describe('GET /admin/emails', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/admin/emails');
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 when logged in as non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      mockQuery.mockResolvedValueOnce(emptyLogsResponse);
      mockQuery.mockResolvedValueOnce(emptyDataResponse);
      mockQuery.mockResolvedValueOnce(emptyTodayResponse);
      const res = await agent.get('/admin/emails');
      expect(res.status).toBe(403);
    });

    it('returns 200 when logged in as admin user', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce(emptyLogsResponse);
      mockQuery.mockResolvedValueOnce(emptyDataResponse);
      mockQuery.mockResolvedValueOnce(emptyTodayResponse);
      const res = await agent.get('/admin/emails');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Email Log');
    });
  });

  describe('GET /admin/builds', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/admin/builds');
      expect(res.status).toBe(302);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.get('/admin/builds');
      expect(res.status).toBe(403);
    });

    it('returns 200 for admin with builds', async () => {
      const agent = await loginAgent('admin@example.com');
      // Builds list query (runs first)
      mockQuery.mockResolvedValueOnce({
        rows: [
          { id: 'b1', app_name: 'app-1', status: 'live', tier: 'build', created_at: new Date(),
            started_at: null, completed_at: null, retry_count: 0, live_url: 'https://example.test',
            error_message: null, customer_email: 'a@example.test', customer_name: 'User' },
        ],
      });
      // Stats query (runs second)
      mockQuery.mockResolvedValueOnce({
        rows: [{ total: '3', queued: '0', building: '0', testing: '0', deploying: '0', preview: '1', live: '1', failed: '1' }],
      });
      const res = await agent.get('/admin/builds');
      expect(res.status).toBe(200);
    });

    // 2026-04-22: SELECT must surface build_engine so SE reviewer can
    // distinguish v1 vs v2 at a glance. 2026-04-28 update: the COALESCE
    // fallback no longer uses the spec.archetype heuristic (which mis-
    // tagged abandoned/unnamed intakes as v1); instead it defaults to
    // 'v2' since CAS_BUILD_ENGINE has been v2 since 2026-04-22.
    it('SELECT query surfaces build_engine column with v2 default', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '0', queued: '0', building: '0', testing: '0', deploying: '0', preview: '0', live: '0', failed: '0' }] });
      await agent.get('/admin/builds');
      const listCall = mockQuery.mock.calls.find(call => {
        const sql = call[0] as string;
        return typeof sql === 'string' && sql.includes('FROM builds b');
      });
      expect(listCall, 'expected a builds-list query with FROM builds b').toBeDefined();
      const sql = listCall![0] as string;
      expect(sql).toMatch(/build_engine/);
      expect(sql).toContain("'v2'");
      // Old archetype heuristic must be GONE.
      expect(sql).not.toContain("spec->>'archetype'");
    });

    it('JSON API /admin/api/builds also includes build_engine', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '0', queued: '0', building: '0', testing: '0', deploying: '0', live: '0', failed: '0' }] });
      await agent.get('/admin/api/builds');
      const listCall = mockQuery.mock.calls.find(call => {
        const sql = call[0] as string;
        return typeof sql === 'string' && sql.includes('FROM builds b');
      });
      expect(listCall, 'expected an admin/api/builds list query').toBeDefined();
      const sql = listCall![0] as string;
      expect(sql).toMatch(/build_engine/);
      expect(sql).toMatch(/COALESCE\(b\.build_engine,\s*'v2'\)/);
    });

    // 2026-04-23 regression guard: build_engine resolution must prefer the
    // authoritative column written by the worker on dispatch, falling back to
    // the archetype heuristic only when the column is NULL (historical rows
    // backfilled best-effort by schema.sql migration). Inference-only would
    // misclassify v2-built rows whose v1-shape input specs were translated
    // in-memory by compose.intake.translate_v1_spec without persisting the
    // translated archetype back to builds.spec (bug id:
    // bug-admin-engine-badge-misreports-v2-as-v1).
    it('SELECT prefers builds.build_engine column with archetype heuristic as fallback', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '0', queued: '0', building: '0', testing: '0', deploying: '0', preview: '0', live: '0', failed: '0' }] });
      await agent.get('/admin/builds');
      const listCall = mockQuery.mock.calls.find(call => {
        const sql = call[0] as string;
        return typeof sql === 'string' && sql.includes('FROM builds b');
      });
      const sql = listCall![0] as string;
      expect(sql).toMatch(/COALESCE\(\s*b\.build_engine\s*,/);
    });

    // 2026-04-28: founder reported unnamed/abandoned builds tagging as v1
    // even though the active engine has been v2 since 2026-04-22. The
    // archetype heuristic that defaulted NULL build_engine to v1 when
    // spec.archetype was missing is gone: the COALESCE fallback now
    // simply defaults to 'v2' across all 3 SELECT sites.
    it('build_engine COALESCE defaults to v2 when build_engine is NULL (no archetype heuristic)', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '0', queued: '0', building: '0', testing: '0', deploying: '0', preview: '0', live: '0', failed: '0' }] });
      await agent.get('/admin/builds');
      const listCall = mockQuery.mock.calls.find(call => {
        const sql = call[0] as string;
        return typeof sql === 'string' && sql.includes('FROM builds b');
      });
      const sql = listCall![0] as string;
      // Plain default to 'v2' (no CASE-WHEN heuristic).
      expect(sql).toMatch(/COALESCE\(b\.build_engine,\s*'v2'\)/);
      // Old heuristic must be GONE: abandoned intakes with no archetype
      // were mis-tagged v1.
      expect(sql).not.toMatch(/CASE WHEN b\.spec->>'archetype' IS NOT NULL/);
    });

    it('build detail SELECT also defaults build_engine to v2 (no archetype heuristic)', async () => {
      const agent = await loginAgent('admin@example.com');
      // Build detail route fetches the build row + logs.
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-test', app_name: 'x', tier: 'build', status: 'queued', build_engine: 'v2', spec: {}, qa_results: null, design_review_results: null, ai_analysis: null, stage_timings: null, retry_count: 0, total_api_cost_usd: null, created_at: new Date(), started_at: null, completed_at: null, customer_id: 'c1', live_url: null, preview_url: null, railway_project_id: null }] });
      mockQuery.mockResolvedValueOnce({ rows: [] }); // logs
      await agent.get('/admin/builds/b-test');
      const detailCall = mockQuery.mock.calls.find(call => {
        const sql = call[0] as string;
        return typeof sql === 'string' && sql.includes('FROM builds WHERE id = $1');
      });
      const sql = detailCall![0] as string;
      expect(sql).toMatch(/COALESCE\(build_engine,\s*'v2'\)/);
      expect(sql).not.toMatch(/CASE WHEN spec->>'archetype' IS NOT NULL/);
    });

    // 2026-05-10 gallery curation: admin needs a UI surface on the build
    // detail page to mark a build as gallery_opted_in (+ description +
    // category) and to feature it. POST routes existed since 2026-04-05
    // (`/admin/gallery/:id/optin`, `/admin/gallery/:id/feature`) but had no
    // calling UI, so opting in required a curl or psql. Unblocks Step-1
    // gallery seeding for free-build customers.
    describe('Gallery curation on build detail', () => {
      const buildLiveRow = (overrides: Record<string, unknown> = {}) => ({
        id: 'b-live', app_name: 'sample-studio', tier: 'build', status: 'live',
        build_engine: 'v2', spec: {}, qa_results: null,
        design_review_results: null, ai_analysis: null, stage_timings: null,
        retry_count: 0, total_api_cost_usd: null, created_at: new Date(),
        started_at: null, completed_at: null, customer_id: 'c1',
        live_url: 'https://sample-studio-example.up.railway.app', preview_url: null,
        railway_project_id: null,
        gallery_opted_in: false, gallery_featured: false,
        gallery_description: null, gallery_category: null,
        gallery_display_name: null,
        ...overrides,
      });

      it('build detail SELECT pulls gallery_opted_in, gallery_featured, gallery_description, gallery_category', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [buildLiveRow()] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        await agent.get('/admin/builds/b-live');
        const detailCall = mockQuery.mock.calls.find(call => {
          const sql = call[0] as string;
          return typeof sql === 'string' && sql.includes('FROM builds WHERE id = $1');
        });
        const sql = detailCall![0] as string;
        expect(sql).toMatch(/gallery_opted_in/);
        expect(sql).toMatch(/gallery_featured/);
        expect(sql).toMatch(/gallery_description/);
        expect(sql).toMatch(/gallery_category/);
        // 2026-05-13: optional brand-name override column.
        expect(sql).toMatch(/gallery_display_name/);
      });

      it('renders gallery curation panel for live build with opt-in form + description + category + display name inputs', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [buildLiveRow()] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent.get('/admin/builds/b-live');
        expect(res.status).toBe(200);
        // Panel header
        expect(res.text).toMatch(/Gallery curation/i);
        // Opt-in form points at admin route, not customer-facing route
        expect(res.text).toMatch(/action="\/admin\/gallery\/b-live\/optin"/);
        // Description + category + display_name fields
        expect(res.text).toMatch(/name="gallery_description"/);
        expect(res.text).toMatch(/name="gallery_category"/);
        expect(res.text).toMatch(/name="gallery_display_name"/);
        // Feature toggle form (separate POST)
        expect(res.text).toMatch(/action="\/admin\/gallery\/b-live\/feature"/);
      });

      it('shows OPTED IN badge + description/category/display_name prefilled when build is already in gallery', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [buildLiveRow({
          gallery_opted_in: true,
          gallery_featured: true,
          gallery_description: 'A quiet healing practice',
          gallery_category: 'Healer',
          gallery_display_name: 'Sample & Studio Website, with CMS',
        })] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent.get('/admin/builds/b-live');
        expect(res.status).toBe(200);
        // Status badges
        expect(res.text).toMatch(/data-gallery-status="opted-in"/);
        expect(res.text).toMatch(/data-gallery-featured="true"/);
        // Existing values prefilled (HTML-escaped)
        expect(res.text).toMatch(/A quiet healing practice/);
        expect(res.text).toMatch(/Healer/);
        // gallery_display_name prefills with HTML-entity-encoded ampersand
        expect(res.text).toMatch(/Sample &amp; Studio Website, with CMS/);
      });

      it('does NOT render gallery curation panel for queued/failed builds (only live or preview)', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [buildLiveRow({ status: 'queued', live_url: null })] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent.get('/admin/builds/b-live');
        expect(res.status).toBe(200);
        expect(res.text).not.toMatch(/action="\/admin\/gallery\/b-live\/optin"/);
      });

      it('renders gallery panel for preview builds (admin can opt-in before /live promotion)', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [buildLiveRow({ status: 'preview' })] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent.get('/admin/builds/b-live');
        expect(res.status).toBe(200);
        expect(res.text).toMatch(/action="\/admin\/gallery\/b-live\/optin"/);
      });
    });

    describe('POST /admin/gallery/:buildId/optin', () => {
      it('requires admin (redirects when not logged in)', async () => {
        const res = await request(app)
          .post('/admin/gallery/b-1/optin')
          .type('form')
          .send({ _csrf: getValidCsrf(), gallery_description: 'x', gallery_category: 'Healer' });
        expect(res.status).toBe(302);
        expect(res.headers.location).toMatch(/\/login/);
      });

      it('toggles gallery_opted_in + persists description + category + display name', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', gallery_opted_in: true }] });
        const res = await agent
          .post('/admin/gallery/b-1/optin')
          .type('form')
          .send({
            _csrf: getValidCsrf(),
            gallery_description: 'Quiet practice',
            gallery_category: 'Healer',
            gallery_display_name: 'Sample & Studio Website, with CMS',
          });
        expect(res.status).toBe(302);
        const updateCall = mockQuery.mock.calls.find(call => {
          const sql = call[0] as string;
          return typeof sql === 'string' && sql.includes('UPDATE builds') && sql.includes('gallery_opted_in');
        });
        expect(updateCall, 'expected gallery optin UPDATE').toBeDefined();
        const sql = updateCall![0] as string;
        expect(sql).toMatch(/gallery_display_name/);
        expect(updateCall![1]).toContain('Quiet practice');
        expect(updateCall![1]).toContain('Healer');
        expect(updateCall![1]).toContain('Sample & Studio Website, with CMS');
      });

      // 2026-05-13: submitting the form WITHOUT the gallery_display_name field
      // (e.g. an older form, or programmatic POST) must NOT overwrite an
      // existing override. The route uses a CASE WHEN $submitted clause so
      // null overrides preserve.
      it('omitting gallery_display_name preserves existing override (no clobber)', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', gallery_opted_in: true }] });
        const res = await agent
          .post('/admin/gallery/b-1/optin')
          .type('form')
          .send({ _csrf: getValidCsrf() });
        expect(res.status).toBe(302);
        const updateCall = mockQuery.mock.calls.find(call => {
          const sql = call[0] as string;
          return typeof sql === 'string' && sql.includes('UPDATE builds') && sql.includes('gallery_opted_in');
        });
        // displaySubmitted parameter should be false → CASE WHEN preserves
        expect(updateCall![1]).toContain(false);
      });

      // 2026-05-13: submitting an EMPTY display name explicitly clears the
      // override (revert to slug-derived). Submitted=true + value=NULL.
      it('submitting empty gallery_display_name clears the override', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', gallery_opted_in: true }] });
        const res = await agent
          .post('/admin/gallery/b-1/optin')
          .type('form')
          .send({ _csrf: getValidCsrf(), gallery_display_name: '' });
        expect(res.status).toBe(302);
        const updateCall = mockQuery.mock.calls.find(call => {
          const sql = call[0] as string;
          return typeof sql === 'string' && sql.includes('UPDATE builds') && sql.includes('gallery_opted_in');
        });
        // Submitted=true so CASE WHEN takes the new value, which is NULL
        expect(updateCall![1]).toContain(true);
        expect(updateCall![1]).toContain(null);
      });

      it('returns 404 when build does not exist or is not live/preview', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent
          .post('/admin/gallery/b-nope/optin')
          .type('form')
          .send({ _csrf: getValidCsrf() });
        expect(res.status).toBe(404);
      });
    });

    describe('POST /admin/gallery/:buildId/feature', () => {
      it('requires admin', async () => {
        const res = await request(app)
          .post('/admin/gallery/b-1/feature')
          .type('form')
          .send({ _csrf: getValidCsrf() });
        expect(res.status).toBe(302);
        expect(res.headers.location).toMatch(/\/login/);
      });

      it('toggles gallery_featured', async () => {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', gallery_featured: true }] });
        const res = await agent
          .post('/admin/gallery/b-1/feature')
          .type('form')
          .send({ _csrf: getValidCsrf() });
        expect(res.status).toBe(302);
        const updateCall = mockQuery.mock.calls.find(call => {
          const sql = call[0] as string;
          return typeof sql === 'string' && sql.includes('UPDATE builds') && sql.includes('gallery_featured');
        });
        expect(updateCall, 'expected gallery feature toggle UPDATE').toBeDefined();
      });
    });

    // 2026-05-17: /admin/executor gained status-filter chips + pagination.
    // 2026-05-18 update: default + per-status filters now gate on
    // `roadmap_item_id IS NOT NULL` so heartbeat rows (no-item paused /
    // idle / running) never show in real-activity views. The Heartbeats
    // chip is the only place they surface (besides All).
    describe('GET /admin/executor', () => {
      // Helper: load the 8 queries the route runs in order:
      // 1. count, 2. runs page, 3. status counts, 4. questions,
      // 5. stats, 6. pendingQuestions, 7. heartbeatRun (newest any),
      // 8. latestItemRun (newest item-attached)
      function loadDefaultMocks(opts: {
        runs?: unknown[];
        total?: number;
        statusRows?: Array<{ status: string; item_n: string; noitem_n: string }>;
        heartbeatRun?: unknown;
        latestItemRun?: unknown;
      } = {}) {
        const runs = opts.runs || [];
        const total = opts.total != null ? opts.total : runs.length;
        const statusRows = opts.statusRows || [];
        const hbRows = opts.heartbeatRun ? [opts.heartbeatRun] : [];
        const lirRows = opts.latestItemRun ? [opts.latestItemRun] : [];
        mockQuery
          .mockResolvedValueOnce({ rows: [{ n: String(total) }] })   // count
          .mockResolvedValueOnce({ rows: runs })                     // runs page
          .mockResolvedValueOnce({ rows: statusRows })               // status counts
          .mockResolvedValueOnce({ rows: [] })                       // questions
          .mockResolvedValueOnce({ rows: [{ today_completed: '0', total_completed: '0', today_failed: '0', today_cost: '0' }] })
          .mockResolvedValueOnce({ rows: [{ count: '0' }] })         // pendingQuestions
          .mockResolvedValueOnce({ rows: hbRows })                   // heartbeatRun
          .mockResolvedValueOnce({ rows: lirRows });                 // latestItemRun
      }

      it('requires admin', async () => {
        const res = await request(app).get('/admin/executor');
        expect(res.status).toBe(302);
        expect(res.headers.location).toMatch(/\/login/);
      });

      it('default filter requires roadmap_item_id IS NOT NULL (real activity)', async () => {
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks();
        const res = await agent.get('/admin/executor');
        expect(res.status).toBe(200);
        const countCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('COUNT(*)') && sql.includes('FROM executor_runs');
        });
        expect(countCall, 'expected count query').toBeDefined();
        // Default = active = roadmap_item_id IS NOT NULL
        expect(countCall![0]).toMatch(/roadmap_item_id IS NOT NULL/);
        // Old paused/idle status filter is GONE; the item gate subsumes it
        expect(countCall![0]).not.toMatch(/status NOT IN \(/);
      });

      it('?status=heartbeats surfaces no-item rows for loop debugging', async () => {
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks();
        const res = await agent.get('/admin/executor?status=heartbeats');
        expect(res.status).toBe(200);
        const runsCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('SELECT * FROM executor_runs');
        });
        expect(runsCall![0]).toMatch(/roadmap_item_id IS NULL/);
      });

      it('?status=all returns all rows (no WHERE clause)', async () => {
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks();
        const res = await agent.get('/admin/executor?status=all');
        expect(res.status).toBe(200);
        const runsCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('SELECT * FROM executor_runs');
        });
        expect(runsCall![0]).not.toMatch(/WHERE/);
      });

      it('?status=failed gates on item-attached (drops loop-failed-no-item)', async () => {
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks();
        const res = await agent.get('/admin/executor?status=failed');
        expect(res.status).toBe(200);
        const runsCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('SELECT * FROM executor_runs') && sql.includes('WHERE status = $1');
        });
        expect(runsCall, 'expected status-filtered query').toBeDefined();
        // Per-status filters carry the item-attached gate too
        expect(runsCall![0]).toMatch(/status = \$1 AND roadmap_item_id IS NOT NULL/);
        expect(runsCall![1]).toContain('failed');
      });

      it('?status=garbage falls back to default (active)', async () => {
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks();
        const res = await agent.get('/admin/executor?status=javascript:alert(1)');
        expect(res.status).toBe(200);
        const countCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('COUNT(*)') && sql.includes('FROM executor_runs');
        });
        // Defaults back to "active" filter, not the unsanitized string
        expect(countCall![0]).toMatch(/roadmap_item_id IS NOT NULL/);
      });

      it('?status=paused (legacy) falls back to default (active)', async () => {
        // Paused / idle were standalone chips in v1; folded into Heartbeats
        // in v2. Old bookmarks should land somewhere sensible (active).
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks();
        const res = await agent.get('/admin/executor?status=paused');
        expect(res.status).toBe(200);
        const countCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('COUNT(*)') && sql.includes('FROM executor_runs');
        });
        expect(countCall![0]).toMatch(/roadmap_item_id IS NOT NULL/);
      });

      it('?page=N applies OFFSET; defaults to page 1 when missing/invalid', async () => {
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks({ total: 100 });
        await agent.get('/admin/executor?page=3');
        const runsCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('SELECT * FROM executor_runs');
        });
        // page 3 with PER_PAGE=25 → OFFSET 50, passed as a bind parameter
        // rather than interpolated into the SQL text.
        expect(runsCall![0]).toMatch(/LIMIT \$\d+ OFFSET \$\d+/);
        expect((runsCall![1] as number[]).slice(-2)).toEqual([25, 50]);

        mockQuery.mockReset();
        loadDefaultMocks({ total: 100 });
        await agent.get('/admin/executor?page=not-a-number');
        const fallbackCall = mockQuery.mock.calls.find(c => {
          const sql = c[0] as string;
          return typeof sql === 'string' && sql.includes('SELECT * FROM executor_runs');
        });
        expect((fallbackCall![1] as number[]).slice(-2)).toEqual([25, 0]);
      });

      it('chip counts use item-attached counts for per-status chips, no-item count for heartbeats', async () => {
        // Sample numbers: completed=98 all-with-item;
        // failed=421 (209 with item + 212 no-item); paused=395 all-no-item;
        // idle=169 (2 with item + 167 no-item); running=46 all-no-item;
        // crashed=3 all-no-item; review=29 + working=17 all-with-item;
        // pr_created=4 all-no-item.
        // After the item gate: failed chip = 209, heartbeats = 826,
        // active = 209+98+29+17+0+0 = 353, all = 1182.
        const agent = await loginAgent('admin@example.com');
        loadDefaultMocks({
          statusRows: [
            { status: 'completed', item_n: '98', noitem_n: '0' },
            { status: 'failed', item_n: '209', noitem_n: '212' },
            { status: 'paused', item_n: '0', noitem_n: '395' },
            { status: 'idle', item_n: '2', noitem_n: '167' },
            { status: 'running', item_n: '0', noitem_n: '46' },
            { status: 'crashed', item_n: '0', noitem_n: '3' },
            { status: 'review', item_n: '29', noitem_n: '0' },
            { status: 'working', item_n: '17', noitem_n: '0' },
            { status: 'pr_created', item_n: '0', noitem_n: '4' },
          ],
        });
        const res = await agent.get('/admin/executor');
        expect(res.status).toBe(200);
        // Active = item-attached count across all statuses = 98+209+2+29+17 = 355
        expect(res.text).toMatch(/Active runs[\s\S]*?>\s*355\s*</);
        // Heartbeats = total no-item count = 212+395+167+46+3+4 = 827
        expect(res.text).toMatch(/Heartbeats[\s\S]*?>\s*827\s*</);
        // Failed chip = item-attached count only (209), NOT the raw 421
        expect(res.text).toMatch(/Failed[\s\S]*?>\s*209\s*</);
        // Completed = 98 (all already have items)
        expect(res.text).toMatch(/Completed[\s\S]*?>\s*98\s*</);
        // Crashed chip = 0 (all 3 prod crashed rows are no-item heartbeats)
        expect(res.text).toMatch(/Crashed[\s\S]*?>\s*0\s*</);
        // All = grand total 1182
        expect(res.text).toMatch(/All[\s\S]*?>\s*1182\s*</);
      });

      it('renders pagination controls when totalPages > 1', async () => {
        const agent = await loginAgent('admin@example.com');
        // 100 rows + PER_PAGE=25 → 4 pages
        loadDefaultMocks({ total: 100 });
        const res = await agent.get('/admin/executor?page=2');
        expect(res.status).toBe(200);
        expect(res.text).toMatch(/page 2 of 4/);
        // Newer (prev) link points at page 1
        expect(res.text).toMatch(/href="\/admin\/executor\?status=[^"]*page=1"[^>]*>[^<]*&larr; Newer/);
        // Older (next) link points at page 3
        expect(res.text).toMatch(/href="\/admin\/executor\?status=[^"]*page=3"[^>]*>[^<]*Older &rarr;/);
      });

      // 2026-05-18: status banner must reflect TRUE loop state from
      // heartbeatRun (newest any-status row), not the stale latest
      // item-attached run. Drove the "things seem stuck" report.
      describe('status banner reflects current heartbeat state', () => {
        const recentTs = new Date().toISOString();
        const oldTs = new Date(Date.now() - 90 * 60 * 1000).toISOString(); // 90min ago

        it('shows STALE when last heartbeat is >35min old', async () => {
          const agent = await loginAgent('admin@example.com');
          loadDefaultMocks({
            heartbeatRun: { id: 'h1', created_at: oldTs, status: 'idle', roadmap_item_id: null, roadmap_item_title: null, skipped_reason: null, result: null },
          });
          const res = await agent.get('/admin/executor');
          expect(res.status).toBe(200);
          expect(res.text).toMatch(/data-executor-state="STALE"/);
          expect(res.text).toMatch(/>STALE</);
        });

        it('shows PAUSED when last heartbeat status is paused', async () => {
          const agent = await loginAgent('admin@example.com');
          loadDefaultMocks({
            heartbeatRun: { id: 'h1', created_at: recentTs, status: 'paused', roadmap_item_id: null, roadmap_item_title: null, skipped_reason: 'paused (enabled=false)', result: null },
          });
          const res = await agent.get('/admin/executor');
          expect(res.status).toBe(200);
          expect(res.text).toMatch(/data-executor-state="PAUSED"/);
          expect(res.text).toMatch(/>PAUSED</);
        });

        it('shows ACTIVE / WORKING when latest heartbeat is working an item', async () => {
          const agent = await loginAgent('admin@example.com');
          loadDefaultMocks({
            heartbeatRun: { id: 'h1', created_at: recentTs, status: 'working', roadmap_item_id: 'foo-bar', roadmap_item_title: 'Foo Bar Item', skipped_reason: null, result: null },
          });
          const res = await agent.get('/admin/executor');
          expect(res.status).toBe(200);
          expect(res.text).toMatch(/data-executor-state="ACTIVE"/);
          expect(res.text).toMatch(/>WORKING</);
          expect(res.text).toMatch(/Foo Bar Item/);
        });

        it('shows NO_DATA when there are zero heartbeats', async () => {
          const agent = await loginAgent('admin@example.com');
          loadDefaultMocks();  // no heartbeatRun
          const res = await agent.get('/admin/executor');
          expect(res.status).toBe(200);
          expect(res.text).toMatch(/data-executor-state="NO_DATA"/);
        });

        it('latestItemRun is shown separately when older than heartbeat (no false-active banner)', async () => {
          const agent = await loginAgent('admin@example.com');
          loadDefaultMocks({
            heartbeatRun: { id: 'h2', created_at: recentTs, status: 'idle', roadmap_item_id: null, roadmap_item_title: null, skipped_reason: 'no eligible items', result: null },
            latestItemRun: { id: 'h1', created_at: oldTs, status: 'review', roadmap_item_id: 'foo-bar', roadmap_item_title: 'Foo Bar Item', skipped_reason: null, result: 'PR created' },
          });
          const res = await agent.get('/admin/executor');
          expect(res.status).toBe(200);
          // Banner does NOT say WORKING (state is IDLE-flavor)
          expect(res.text).not.toMatch(/data-executor-state="ACTIVE"/);
          // "Last real activity" footnote surfaces the stale item run
          expect(res.text).toMatch(/Last real activity/);
          expect(res.text).toMatch(/review/);
        });

        it('queries heartbeatRun + latestItemRun as separate ORDER BY DESC LIMIT 1 selects', async () => {
          const agent = await loginAgent('admin@example.com');
          loadDefaultMocks({
            heartbeatRun: { id: 'h1', created_at: recentTs, status: 'idle', roadmap_item_id: null, roadmap_item_title: null, skipped_reason: null, result: null },
          });
          await agent.get('/admin/executor');
          const hbCall = mockQuery.mock.calls.find(c => {
            const sql = c[0] as string;
            return typeof sql === 'string' && sql.includes('FROM executor_runs ORDER BY created_at DESC LIMIT 1') && !sql.includes('WHERE');
          });
          expect(hbCall, 'expected heartbeatRun query').toBeDefined();
          const itemCall = mockQuery.mock.calls.find(c => {
            const sql = c[0] as string;
            return typeof sql === 'string' && sql.includes('FROM executor_runs WHERE roadmap_item_id IS NOT NULL ORDER BY created_at DESC LIMIT 1');
          });
          expect(itemCall, 'expected latestItemRun query').toBeDefined();
        });
      });
    });

    it('admin-builds JS buildRow() wraps app_name in detail-link anchor + emits engine cell', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [{ total: '0', queued: '0', building: '0', testing: '0', deploying: '0', preview: '0', live: '0', failed: '0' }] });
      const res = await agent.get('/admin/builds');
      expect(res.status).toBe(200);
      // 2026-04-28 regression: when the auto-refresh polling hot-swapped a
      // row, the JS buildRow() rendered app_name as plain text (lost the
      // detail link) AND skipped the engine column entirely (column shifted
      // at xl+ width). buildRow() now mirrors the server-render structure.
      // Anchor wrapper present (the id is escaped like every other value the
      // row builder concatenates into HTML):
      expect(res.text).toMatch(/<a href="\/admin\/builds\/'\s*\+\s*esc\(b\.id\)\s*\+\s*'"/);
      // Engine <td> with badge and data-cell attribute:
      expect(res.text).toMatch(/data-cell="engine"/);
      expect(res.text).toMatch(/admin-badge admin-badge-engine-/);
      // data-engine attr on the <tr> for visual styling:
      expect(res.text).toMatch(/tr\.setAttribute\(\s*'data-engine'/);
    });
  });

  describe('GET /admin/customers', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/admin/customers');
      expect(res.status).toBe(302);
    });

    it('returns 403 for non-admin', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.get('/admin/customers');
      expect(res.status).toBe(403);
    });

    it('returns 200 for admin', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'c1', email: 'user@example.test', name: 'User', created_at: new Date(), build_count: '2', live_count: '1', failed_count: '0' }],
      });
      mockQuery.mockResolvedValueOnce({
        rows: [{ count: '1' }],
      });
      const res = await agent.get('/admin/customers');
      expect(res.status).toBe(200);
    });
  });

  describe('GET /export-data', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/export-data');
      expect(res.status).toBe(302);
    });

    it('returns JSON export for authenticated user', async () => {
      const agent = await loginAgent('admin@example.com');
      // Customer query
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'c1', email: 'admin@example.com', name: 'Admin', created_at: new Date() }],
      });
      // Builds query
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // Intake messages query
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // Feature requests query
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/export-data');
      expect(res.status).toBe(200);
      expect(res.headers['content-type']).toContain('application/json');
    });
  });

  describe('POST /admin/builds/cleanup', () => {
    it('requires admin + CSRF', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post('/admin/builds/cleanup').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);
    });
  });

  describe('POST /admin/support-kb/clear', () => {
    it('requires admin + CSRF', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post('/admin/support-kb/clear').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);
    });

    it('clears KB for admin', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 5 });
      const res = await agent.post('/admin/support-kb/clear').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
    });
  });

  describe('POST /admin/builds/:id/kill', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).post('/admin/builds/build-1/kill').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post('/admin/builds/build-1/kill').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);
    });

    it('redirects to /admin/builds on success for active build', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'build-1', app_name: 'test-app' }],
      });
      const res = await agent.post('/admin/builds/build-1/kill').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');
    });

    it('only kills builds in active states', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'build-1', app_name: 'test-app' }],
      });
      await agent.post('/admin/builds/build-1/kill').type('form').send({ _csrf: getValidCsrf() });

      // Verify the SQL constrains to active states
      const lastCall = mockQuery.mock.calls[mockQuery.mock.calls.length - 1];
      const sql = lastCall[0] as string;
      expect(sql).toContain('building');
      expect(sql).toContain('testing');
      expect(sql).toContain('deploying');
      expect(sql).toContain('queued');
    });

    it('redirects even when no matching build found', async () => {
      const agent = await loginAgent('admin@example.com');
      // No rows returned means build was not in an active state
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.post('/admin/builds/nonexistent/kill').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');
    });
  });

  describe('POST /admin/builds/:id/delete', () => {
    function mockDeleteClient(lookupRow: { railway_project_id: string | null; app_name: string | null } | null) {
      const mockClient = {
        query: vi.fn().mockImplementation((sql: string) => {
          if (typeof sql === 'string' && sql.includes('SELECT railway_project_id')) {
            return Promise.resolve({ rows: lookupRow ? [lookupRow] : [] });
          }
          return Promise.resolve({ rows: [] });
        }),
        release: vi.fn(),
      };
      (pool as any).connect = vi.fn().mockResolvedValue(mockClient);
      return mockClient;
    }

    beforeEach(() => {
      mockDeleteRailwayProject.mockReset();
      mockDeleteRailwayProject.mockResolvedValue(undefined);
      mockReadRailwayProjectName.mockReset();
      mockReadRailwayProjectName.mockResolvedValue('some-app');
      mockIsRailwayWriteEnabled.mockReset();
      mockIsRailwayWriteEnabled.mockReturnValue(true);
    });

    afterEach(() => {
      vi.unstubAllEnvs();
    });

    it('redirects to login when not logged in', async () => {
      const res = await request(app).post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);
    });

    it('returns 404 when the build does not exist', async () => {
      const agent = await loginAgent('admin@example.com');
      mockDeleteClient(null);
      const res = await agent.post('/admin/builds/nope/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(404);
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();
    });

    it('cascades CAS deletes and skips Railway when no project id', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: null, app_name: 'orphan-app' });
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries).toContain('BEGIN');
      expect(queries).toContain('COMMIT');
      expect(queries.some((q: string) => q.includes('DELETE FROM intake_messages'))).toBe(true);
      expect(queries.some((q: string) => q.includes('DELETE FROM build_logs'))).toBe(true);
      expect(queries.some((q: string) => q.includes('DELETE FROM dead_letter_builds'))).toBe(true);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(true);
      expect(queries.some((q: string) => q.includes('DELETE FROM feature_requests'))).toBe(true);
      expect(queries.some((q: string) => q.includes('DELETE FROM feature_messages'))).toBe(true);
      expect(mockClient.release).toHaveBeenCalled();

      const nullPathLogs = logSpy.mock.calls.filter(
        (c) => typeof c[0] === 'string' && c[0].includes('has no Railway project attached'),
      );
      expect(nullPathLogs).toHaveLength(1);
      expect(nullPathLogs[0]).toEqual([
        'Admin delete: build %s (%s) has no Railway project attached; CAS-only cascade',
        'build-1',
        'orphan-app',
      ]);
      logSpy.mockRestore();
    });

    it('deletes Railway project first, then cascades CAS on happy path', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'some-app' });

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');
      expect(mockDeleteRailwayProject).toHaveBeenCalledWith('rw-proj-xyz');

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(true);
    });

    it('returns 502 and does NOT delete CAS when Railway delete fails', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'some-app' });
      mockDeleteRailwayProject.mockRejectedValueOnce(new RailwayApiError('Railway API 500: upstream', 500, false));

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(502);
      expect(res.text).toContain('some-app');
      expect(res.text).toContain('rw-proj-xyz');

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries).not.toContain('BEGIN');
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(false);
      expect(mockClient.release).toHaveBeenCalled();
    });

    it('proceeds with CAS cascade when Railway reports project already gone', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'some-app' });
      mockDeleteRailwayProject.mockRejectedValueOnce(
        new RailwayApiError('Railway project not found (already deleted)', 404, true),
      );

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(true);
    });

    // The project is named back before any delete. A stored id that points at
    // another project, or a token that lost access, must not lead to the row
    // being removed while the project keeps running.
    it('reads the project name back before deleting, and deletes only when it matches the app name', async () => {
      const agent = await loginAgent('admin@example.com');
      mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'Sample Studio' });
      mockReadRailwayProjectName.mockResolvedValueOnce('sample-studio');

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(mockReadRailwayProjectName).toHaveBeenCalledWith('rw-proj-xyz');
      expect(mockReadRailwayProjectName.mock.invocationCallOrder[0]).toBeLessThan(mockDeleteRailwayProject.mock.invocationCallOrder[0]);
      expect(mockDeleteRailwayProject).toHaveBeenCalledWith('rw-proj-xyz');
    });

    it('keeps the project and the row when the name read back is a different project', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'some-app' });
      mockReadRailwayProjectName.mockResolvedValueOnce('another-customer-site');

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(502);
      expect(res.text).toContain('nothing was deleted');
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();
      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries).not.toContain('BEGIN');
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(false);
    });

    it('keeps the project and the row when the name cannot be read', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'some-app' });
      mockReadRailwayProjectName.mockRejectedValueOnce(new RailwayApiError('Railway API 401: Not Authorized', 401, false));

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(502);
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();
      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(false);
    });

    it('keeps the row for a build with no app name, since no name can match', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: null });

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(502);
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();
      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(false);
    });

    it('returns 502 and keeps the row when Railway says "not found" without the NOT_FOUND code', async () => {
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({ railway_project_id: 'rw-proj-xyz', app_name: 'some-app' });
      mockDeleteRailwayProject.mockRejectedValueOnce(
        new RailwayApiError('Railway GraphQL error: Project not found or you do not have access', 200, false),
      );

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(502);
      expect(res.text).toContain('Project not found or you do not have access');
      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(false);
    });

    it('refuses to delete a protected Railway project id', async () => {
      vi.stubEnv('PROTECTED_RAILWAY_PROJECT_IDS', '00000000-0000-4000-8000-000000000001, 00000000-0000-4000-8000-000000000009');
      const agent = await loginAgent('admin@example.com');
      const mockClient = mockDeleteClient({
        railway_project_id: '00000000-0000-4000-8000-000000000001',
        app_name: 'craftandship-recovery',
      });

      const res = await agent.post('/admin/builds/prod-build/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();
      expect(res.text).toContain('craftandship-recovery');
      expect(res.text).toContain('00000000-0000-4000-8000-000000000001');

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries).not.toContain('BEGIN');
    });

    it('silently skips Railway and still cascades CAS when writes disabled (local/staging)', async () => {
      const agent = await loginAgent('admin@example.com');
      mockIsRailwayWriteEnabled.mockReturnValue(false);
      const mockClient = mockDeleteClient({
        railway_project_id: 'rw-proj-some',
        app_name: 'some-app',
      });

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries).toContain('BEGIN');
      expect(queries).toContain('COMMIT');
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(true);
      expect(mockClient.release).toHaveBeenCalled();
    });

    it('does not protect any project when PROTECTED_RAILWAY_PROJECT_IDS is unset', async () => {
      vi.stubEnv('PROTECTED_RAILWAY_PROJECT_IDS', '');
      const agent = await loginAgent('admin@example.com');
      mockDeleteClient({
        railway_project_id: '00000000-0000-4000-8000-000000000001',
        app_name: 'craftandship-recovery',
      });
      mockReadRailwayProjectName.mockResolvedValueOnce('craftandship-recovery');

      const res = await agent.post('/admin/builds/prod-build/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(mockDeleteRailwayProject).toHaveBeenCalledWith('00000000-0000-4000-8000-000000000001');
    });

    it('skips protected-id check when writes are disabled (protected guard only fires when Railway call would actually happen)', async () => {
      vi.stubEnv('PROTECTED_RAILWAY_PROJECT_IDS', '00000000-0000-4000-8000-000000000001');
      const agent = await loginAgent('admin@example.com');
      mockIsRailwayWriteEnabled.mockReturnValue(false);
      const mockClient = mockDeleteClient({
        railway_project_id: '00000000-0000-4000-8000-000000000001',
        app_name: 'craftandship-recovery',
      });

      const res = await agent.post('/admin/builds/prod-mirror/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(true);
    });

    it('still cascades CAS when writes disabled but build has no Railway project', async () => {
      const agent = await loginAgent('admin@example.com');
      mockIsRailwayWriteEnabled.mockReturnValue(false);
      const mockClient = mockDeleteClient({
        railway_project_id: null,
        app_name: 'local-orphan',
      });
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});

      const res = await agent.post('/admin/builds/build-1/delete').type('form').send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/builds');
      expect(mockDeleteRailwayProject).not.toHaveBeenCalled();

      const queries = mockClient.query.mock.calls.map((c: any) => c[0] as string);
      expect(queries).toContain('BEGIN');
      expect(queries).toContain('COMMIT');
      expect(queries.some((q: string) => q.includes('DELETE FROM builds'))).toBe(true);

      const nullPathLogs = logSpy.mock.calls.filter(
        (c) => typeof c[0] === 'string' && c[0].includes('has no Railway project attached'),
      );
      expect(nullPathLogs).toHaveLength(1);
      expect(nullPathLogs[0]).toEqual([
        'Admin delete: build %s (%s) has no Railway project attached; CAS-only cascade',
        'build-1',
        'local-orphan',
      ]);
      const skipLogs = logSpy.mock.calls.filter(
        (c) => typeof c[0] === 'string' && c[0].includes('Railway writes disabled'),
      );
      expect(skipLogs).toHaveLength(0);
      logSpy.mockRestore();
    });
  });
});

// ---------------------------------------------------------------------------
// Admin AI Build Tools route tests (section-05)
// ---------------------------------------------------------------------------

const buildRow = {
  id: 'build-123',
  app_name: 'test-app',
  status: 'failed',
  tier: 'build',
  error_message: 'npm install failed',
  spec: { app_name: 'test-app' },
  qa_results: [],
  design_review_results: [],
  ai_analysis: null,
  stage_timings: {},
  retry_count: 2,
  total_api_cost_usd: '0.05',
  created_at: new Date(),
  started_at: new Date(),
  completed_at: null,
  customer_id: 'cust-1',
};

const logRows = [
  { id: 42, stage: 'build', message: 'Error: missing package', created_at: new Date() },
];

const analysisResult = {
  root_cause: 'Missing npm package',
  confidence: 'high',
  implicated_files: ['package.json'],
  fix_summary: 'Add the missing package to package.json',
  recommended_action: 'rebuild',
};

describe('GET /admin/builds/:id', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('redirects to /login when not logged in', async () => {
    const res = await request(app).get('/admin/builds/build-123');
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/login');
  });

  it('returns 403 when logged in as non-admin', async () => {
    const agent = await loginAgent('regular@example.com');
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(403);
  });

  it('returns 404 when build not found', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [] }); // build lookup
    mockQuery.mockResolvedValueOnce({ rows: [] }); // logs lookup
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(404);
  });

  it('returns 200 with Analyze with Claude button when build exists and ai_analysis is null', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Analyze with Claude');
    expect(res.text).toContain(buildRow.app_name);
  });

  it('returns 200 with cached analysis and Re-analyze button when ai_analysis is populated', async () => {
    const agent = await loginAgent('admin@example.com');
    const rowWithAnalysis = { ...buildRow, ai_analysis: { ...analysisResult, analyzed_at: new Date().toISOString() } };
    mockQuery.mockResolvedValueOnce({ rows: [rowWithAnalysis] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    expect(res.text).toContain(analysisResult.root_cause);
    expect(res.text).toContain('Re-analyze');
  });

  it('rendered page contains CSRF token', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    expect(res.text).toContain('_csrf');
  });

  it('pool.query called for build row and build logs', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    await agent.get('/admin/builds/build-123');
    const calls = mockQuery.mock.calls;
    const allSql = calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('FROM builds');
    expect(allSql).toContain('build_logs');
  });

  it('build_logs SELECT includes id column for live polling cursor', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    await agent.get('/admin/builds/build-123');
    const calls = mockQuery.mock.calls;
    const logSql = calls.map((c: unknown[]) => String(c[0])).find((s: string) => s.includes('build_logs')) || '';
    expect(logSql).toMatch(/SELECT\s+id\s*,\s*stage/);
  });

  it('renders log entries with data-log-id attributes for live polling', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    expect(res.text).toContain('data-log-id="42"');
    expect(res.text).toContain('id="admin-build-logs-list"');
    expect(res.text).toContain('id="admin-build-logs-scroll"');
  });

  it('renders stable ids on status, retries, and api cost cells for live updates', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    expect(res.text).toContain('id="admin-build-status-badge"');
    expect(res.text).toContain('id="admin-build-retries"');
    expect(res.text).toContain('id="admin-build-api-cost"');
  });

  it('emits live-polling script that stops on terminal status', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    expect(res.text).toContain("'/api/builds/' + BUILD_ID + '/logs?after=' + lastLogId");
    expect(res.text).toMatch(/TERMINAL_STATES\s*=\s*\[[^\]]*'preview'[^\]]*'live'[^\]]*'failed'[^\]]*'abandoned'/);
  });
});

describe('POST /admin/builds/:id/analyze', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('redirects to /login when not logged in', async () => {
    const res = await request(app)
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/login');
  });

  it('returns 403 for non-admin', async () => {
    const agent = await loginAgent('regular@example.com');
    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());
    expect(res.status).toBe(403);
  });

  it('returns 403 when CSRF token missing', async () => {
    const agent = await loginAgent('admin@example.com');
    const res = await agent.post('/admin/builds/build-123/analyze');
    expect(res.status).toBe(403);
  });

  it('returns 404 JSON when build not found', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());
    expect(res.status).toBe(404);
    expect(res.body).toHaveProperty('error');
  });

  it('returns cached analysis without calling analyzeBuildFailure when ai_analysis present and no force', async () => {
    const agent = await loginAgent('admin@example.com');
    const rowWithAnalysis = { ...buildRow, ai_analysis: { ...analysisResult, analyzed_at: new Date().toISOString() } };
    mockQuery.mockResolvedValueOnce({ rows: [rowWithAnalysis] });
    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());
    expect(res.status).toBe(200);
    expect(res.body.cached).toBe(true);
    expect(mockAnalyzeBuildFailure).not.toHaveBeenCalled();
  });

  it('calls analyzeBuildFailure, stores result, returns { analysis, cached: false } when ai_analysis is null', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] }); // build fetch
    mockQuery.mockResolvedValueOnce({ rows: logRows });    // logs fetch
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });      // UPDATE
    mockAnalyzeBuildFailure.mockResolvedValueOnce(analysisResult);

    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());

    expect(res.status).toBe(200);
    expect(res.body.cached).toBe(false);
    expect(mockAnalyzeBuildFailure).toHaveBeenCalledOnce();
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('UPDATE builds SET ai_analysis');
    // The log window is the newest 50 entries, handed over oldest first.
    const logSql = String(mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).find((sql: string) => sql.includes('build_logs')) || '').replace(/\s+/g, ' ');
    expect(logSql).toMatch(/FROM build_logs WHERE build_id = \$1 ORDER BY id DESC LIMIT 50 \) newest ORDER BY id ASC/);
  });

  it('calls analyzeBuildFailure when ?force=1 even if ai_analysis is present', async () => {
    const agent = await loginAgent('admin@example.com');
    const rowWithAnalysis = { ...buildRow, ai_analysis: { ...analysisResult, analyzed_at: new Date().toISOString() } };
    mockQuery.mockResolvedValueOnce({ rows: [rowWithAnalysis] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    mockAnalyzeBuildFailure.mockResolvedValueOnce(analysisResult);

    const res = await agent
      .post('/admin/builds/build-123/analyze?force=1')
      .set('x-csrf-token', getValidCsrf());

    expect(res.status).toBe(200);
    expect(res.body.cached).toBe(false);
    expect(mockAnalyzeBuildFailure).toHaveBeenCalledOnce();
  });

  it('returns 500 JSON with error field when analyzeBuildFailure throws', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    mockAnalyzeBuildFailure.mockRejectedValueOnce(new Error('Claude API timeout'));

    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());

    expect(res.status).toBe(500);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 500 JSON when CircuitBreakerOpenError thrown', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    mockAnalyzeBuildFailure.mockRejectedValueOnce(new Error('Circuit breaker open'));

    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());

    expect(res.status).toBe(500);
    expect(res.body).toHaveProperty('error');
  });
});

describe('POST /admin/builds/:id/ai-fix', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('redirects to /login when not logged in', async () => {
    const res = await request(app)
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix it' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/login');
  });

  it('returns 403 for non-admin', async () => {
    const agent = await loginAgent('regular@example.com');
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix it' });
    expect(res.status).toBe(403);
  });

  it('returns 403 when CSRF token missing', async () => {
    const agent = await loginAgent('admin@example.com');
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ rebuild_request: 'fix it' });
    expect(res.status).toBe(403);
  });

  it('returns 400 when rebuild_request is empty', async () => {
    const agent = await loginAgent('admin@example.com');
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: '' });
    expect(res.status).toBe(400);
  });

  it('returns 404 when build not found', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix the route' });
    expect(res.status).toBe(404);
  });

  it('calls UPDATE to reset build when status is failed', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix the thing', force_new: '' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/admin/builds/build-123');
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('UPDATE builds');
    expect(allSql).toContain('queued');
    expect(allSql).toContain('retry_count = 0');
  });

  it('calls UPDATE when status is building', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ ...buildRow, status: 'building' }] });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix it', force_new: '' });
    expect(res.status).toBe(302);
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('UPDATE builds');
  });

  it('calls UPDATE when status is testing', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ ...buildRow, status: 'testing' }] });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix it', force_new: '' });
    expect(res.status).toBe(302);
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('UPDATE builds');
  });

  it('calls UPDATE when status is deploying', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ ...buildRow, status: 'deploying' }] });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix it', force_new: '' });
    expect(res.status).toBe(302);
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('UPDATE builds');
  });

  it('calls INSERT to create new build when status is live', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ ...buildRow, status: 'live' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'new-build-456' }] });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'add feature', force_new: '' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain('/admin/builds/new-build-456');
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('INSERT INTO builds');
  });

  it('calls INSERT when status is preview', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ ...buildRow, status: 'preview' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'new-build-789' }] });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix design', force_new: '' });
    expect(res.status).toBe(302);
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('INSERT INTO builds');
  });

  it('calls INSERT when force_new=1 even for failed build', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] }); // status: failed
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'new-build-999' }] });
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fresh start', force_new: '1' });
    expect(res.status).toBe(302);
    const allSql = mockQuery.mock.calls.map((c: unknown[]) => String(c[0])).join('\n');
    expect(allSql).toContain('INSERT INTO builds');
  });

  it('UPDATE sets status=queued, retry_count=0, rebuild_request', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'my fix description', force_new: '' });
    const callsJson = JSON.stringify(mockQuery.mock.calls);
    expect(callsJson).toContain('queued');
    expect(callsJson).toContain('retry_count = 0');
    expect(callsJson).toContain('my fix description');
  });

  it('INSERT includes parent_build_id and rebuild_request', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ ...buildRow, status: 'live' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'new-build-abc' }] });
    await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'parent link test', force_new: '' });
    const callsJson = JSON.stringify(mockQuery.mock.calls);
    expect(callsJson).toContain('parent_build_id');
    expect(callsJson).toContain('parent link test');
  });

  it('returns 500 JSON when pool.query throws', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockRejectedValueOnce(new Error('DB connection lost'));
    const res = await agent
      .post('/admin/builds/build-123/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'fix it' });
    expect(res.status).toBe(500);
    expect(res.body).toHaveProperty('error');
  });
});

// ---------------------------------------------------------------------------
// GET /admin/builds/:id, additional coverage
// ---------------------------------------------------------------------------

describe('GET /admin/builds/:id: error and rendering edge cases', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns 500 when pool.query throws', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockRejectedValueOnce(new Error('DB unavailable'));
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(500);
  });

  it('renders QA failure badge count when qa_results has failing entries', async () => {
    const agent = await loginAgent('admin@example.com');
    const rowWithQa = {
      ...buildRow,
      qa_results: [
        { name: 'login page', passed: false, error: 'Element not found' },
        { name: 'dashboard', passed: true, error: null },
        { name: 'create form', passed: false, error: 'Timeout' },
      ],
    };
    mockQuery.mockResolvedValueOnce({ rows: [rowWithQa] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    // Badge shows count of failing entries (2)
    expect(res.text).toContain('>2<');
    expect(res.text).toContain('QA Failures');
  });

  it('renders Design Issues badge count for critical and warning severity', async () => {
    const agent = await loginAgent('admin@example.com');
    const rowWithDesign = {
      ...buildRow,
      design_review_results: [
        { type: 'contrast', severity: 'critical', message: 'Text too dark' },
        { type: 'layout', severity: 'warning', message: 'Nav overflow' },
        { type: 'font', severity: 'info', message: 'Font size fine' }, // excluded
      ],
    };
    mockQuery.mockResolvedValueOnce({ rows: [rowWithDesign] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    // Badge shows count of critical+warning entries (2); info excluded
    expect(res.text).toContain('>2<');
    expect(res.text).toContain('Design Issues');
  });

  it('renders queue-fix panel as hidden when ai_analysis is null', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] }); // ai_analysis: null
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    // Panel should have display:none when no analysis
    expect(res.text).toMatch(/queue-fix-panel[^>]*style="display:none"/);
  });

  it('renders queue-fix panel as visible when ai_analysis is populated', async () => {
    const agent = await loginAgent('admin@example.com');
    const rowWithAnalysis = { ...buildRow, ai_analysis: { ...analysisResult, analyzed_at: new Date().toISOString() } };
    mockQuery.mockResolvedValueOnce({ rows: [rowWithAnalysis] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    const res = await agent.get('/admin/builds/build-123');
    expect(res.status).toBe(200);
    // Panel should NOT have display:none when analysis present
    expect(res.text).not.toMatch(/queue-fix-panel[^>]*style="display:none"/);
    expect(res.text).toContain('Queue Fix');
  });
});

// ---------------------------------------------------------------------------
// POST /admin/builds/:id/analyze, additional coverage
// ---------------------------------------------------------------------------

describe('POST /admin/builds/:id/analyze: additional coverage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('response body contains analyzed_at timestamp after analysis', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [buildRow] });
    mockQuery.mockResolvedValueOnce({ rows: logRows });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    mockAnalyzeBuildFailure.mockResolvedValueOnce(analysisResult);

    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());

    expect(res.status).toBe(200);
    expect(res.body.analysis).toHaveProperty('analyzed_at');
    expect(typeof res.body.analysis.analyzed_at).toBe('string');
  });

  it('returns 500 JSON when initial pool.query throws', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockRejectedValueOnce(new Error('Connection reset'));
    const res = await agent
      .post('/admin/builds/build-123/analyze')
      .set('x-csrf-token', getValidCsrf());
    expect(res.status).toBe(500);
    expect(res.body).toHaveProperty('error');
  });

  describe('GET /admin/harbor (CAS Harbor inbox)', () => {
    const ORIGINAL_HARBOR_FLAG = process.env.CAS_HARBOR_ENABLED;

    afterEach(() => {
      if (ORIGINAL_HARBOR_FLAG === undefined) {
        delete process.env.CAS_HARBOR_ENABLED;
      } else {
        process.env.CAS_HARBOR_ENABLED = ORIGINAL_HARBOR_FLAG;
      }
    });

    function makeHarborRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
      return {
        trial_id: '11111111-2222-3333-4444-555555555555',
        companion_name: 'Haven',
        started_at: new Date('2026-04-26T10:00:00Z'),
        expires_at: new Date('2099-04-26T10:00:00Z'),
        last_seen_at: new Date('2026-04-26T11:00:00Z'),
        turn_count: '3',
        memory_count: '8',
        first_keeper_text: 'I run a yoga studio and want a simple website.',
        last_companion_text: "Glad you stopped in. Tell me a bit about who you'd like to invite.",
        ...overrides,
      };
    }

    it('redirects to /login when not logged in', async () => {
      const res = await request(app).get('/admin/harbor');
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(403);
    });

    it('renders dormant message when CAS_HARBOR_ENABLED is unset (no DB query fired)', async () => {
      delete process.env.CAS_HARBOR_ENABLED;
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockReset();
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Harbor surface is dormant');
      expect(res.text).toContain('CAS_HARBOR_ENABLED=true');
      expect(mockQuery).not.toHaveBeenCalled();
    });

    it('renders the inbox with stats + a row when flag is on and trials exist', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [makeHarborRow({ turn_count: '3', memory_count: '8' })],
      });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Harbor Inbox');
      expect(res.text).toContain('data-stat="total"');
      expect(res.text).toContain('data-stat="active"');
      expect(res.text).toContain('data-stat="ready"');
      expect(res.text).toContain('Haven');
      expect(res.text).toContain('11111111');
      expect(res.text).toContain('I run a yoga studio and want');
      expect(res.text).toContain('/lighthouse/table?dev_trial=11111111-2222-3333-4444-555555555555');
      expect(res.text).toContain('/lighthouse/table/recall?dev_trial=11111111-2222-3333-4444-555555555555');
    });

    it('admin nav shows Harbor entry (anchor entity + label) when CAS_HARBOR_ENABLED=true', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      // U+2693 (anchor) HTML entity &#9875; renders only in the mobile
      // nav variant of _admin-nav.ejs (mobile shows icon+label, desktop
      // shows label-only). Exactly 1 occurrence proves the Harbor entry
      // is in the mobile array; >0 is the contract.
      const anchorMatches = res.text.match(/&#9875;/g) || [];
      expect(anchorMatches.length).toBe(1);
      // Harbor link href appears in BOTH nav variants (mobile + desktop)
      // so we expect >=2 hrefs to /admin/harbor in addition to the
      // canonical link tag in the page header.
      const harborHrefMatches = res.text.match(/href="\/admin\/harbor"/g) || [];
      expect(harborHrefMatches.length).toBeGreaterThanOrEqual(2);
    });

    it('hides dev_trial deep-links in NODE_ENV=production', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
      process.env.NODE_ENV = 'production';
      try {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({
          rows: [
            {
              trial_id: '11111111-2222-3333-4444-555555555555',
              companion_name: 'Haven',
              started_at: new Date(),
              expires_at: new Date(Date.now() + 86400000),
              last_seen_at: new Date(),
              turn_count: '8',
              memory_count: '0',
              first_keeper_text: 'hi',
              last_companion_text: 'hi',
            },
          ],
        });
        const res = await agent.get('/admin/harbor');
        expect(res.status).toBe(200);
        // Dev-trial deep-links should NOT render in prod.
        expect(res.text).not.toContain('?dev_trial=');
        // Replacement affordance: id prefix shown instead.
        expect(res.text).toContain('dev_trial override is dev-only');
      } finally {
        if (ORIGINAL_NODE_ENV === undefined) delete process.env.NODE_ENV;
        else process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      }
    });

    it('shows dev_trial deep-links in NODE_ENV=development', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
      process.env.NODE_ENV = 'development';
      try {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({
          rows: [
            {
              trial_id: '11111111-2222-3333-4444-555555555555',
              companion_name: 'Haven',
              started_at: new Date(),
              expires_at: new Date(Date.now() + 86400000),
              last_seen_at: new Date(),
              turn_count: '8',
              memory_count: '0',
              first_keeper_text: 'hi',
              last_companion_text: 'hi',
            },
          ],
        });
        const res = await agent.get('/admin/harbor');
        expect(res.status).toBe(200);
        expect(res.text).toContain('/lighthouse/table?dev_trial=11111111-2222-3333-4444-555555555555');
        expect(res.text).toContain('/lighthouse/table/recall?dev_trial=11111111-2222-3333-4444-555555555555');
      } finally {
        if (ORIGINAL_NODE_ENV === undefined) delete process.env.NODE_ENV;
        else process.env.NODE_ENV = ORIGINAL_NODE_ENV;
      }
    });

    it('admin nav HIDES Harbor entry when CAS_HARBOR_ENABLED is unset', async () => {
      delete process.env.CAS_HARBOR_ENABLED;
      const agent = await loginAgent('admin@example.com');
      // Dormant /admin/harbor still renders _admin-nav. No DB query fires
      // when the flag is unset (verified by an earlier test); so no mock needed.
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      // Anchor entity is unique to the Harbor nav entry; absence proves the filter.
      expect(res.text).not.toContain('&#9875;');
    });

    it('marks a trial READY when turn_count >= 8 (low-effort handoff signal)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [makeHarborRow({ turn_count: '8', last_companion_text: 'Tell me more about your audience.' })],
      });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).toContain('data-ready-badge');
      expect(res.text).toContain('READY');
    });

    it('marks a trial READY when last companion message contains the handoff phrase', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [makeHarborRow({
          turn_count: '4',
          last_companion_text: 'Ocean will be in touch within a few days, probably by email.',
        })],
      });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).toContain('data-ready-badge');
    });

    it('does NOT mark a low-turn trial without the handoff phrase as ready', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [makeHarborRow({
          turn_count: '3',
          last_companion_text: 'Tell me more about who you serve.',
        })],
      });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).not.toContain('data-ready-badge');
      expect(res.text).toContain('in progress');
    });

    it('shows empty state when there are no trial rows', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).toContain('No trials yet');
    });

    it('SQL excludes released trials and orders by last_seen_at DESC', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      await agent.get('/admin/harbor');
      const sql = mockQuery.mock.calls[1][0] as string;
      expect(sql).toMatch(/released_at IS NULL/);
      expect(sql).toMatch(/ORDER BY tk\.last_seen_at DESC/);
      expect(sql).toMatch(/keeper_memory/);
      expect(sql).toMatch(/companion_turns/);
      // A trial's conversation is counted by trial id, never by keeper id.
      expect(sql).toMatch(/ct\.trial_id = tk\.trial_id/);
      expect(sql).toMatch(/km\.trial_id = tk\.trial_id AND km\.deleted_at IS NULL/);
      expect(sql).not.toMatch(/keeper_id/);
    });

    // Payment releases the trial, so the list above stops showing it, and
    // moves the conversation to the keeper. The "Paid keepers" view is where
    // the operator finds it again.
    describe('?status=keepers (paid keepers)', () => {
      const KEEPER_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
      const BUILD_ID = '99999999-8888-4777-8666-555555555555';

      // Each test here scripts two results (the trial list, then the keeper
      // list). Drop whatever a failing test left unread, so it cannot be
      // handed to the next test's first query.
      afterEach(() => {
        mockQuery.mockReset();
      });

      function makeKeeperRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
        return {
          customer_id: KEEPER_ID,
          email: 'keeper@example.com',
          companion_name: 'Haven',
          subscription_status: 'active',
          handoff_build_id: BUILD_ID,
          turn_count: '12',
          memory_count: '5',
          first_keeper_text: 'I teach pottery and want a class sign-up page.',
          last_turn_at: new Date('2026-04-26T11:00:00Z'),
          ...overrides,
        };
      }

      it('lists each paid keeper with the conversation read by keeper id', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [makeHarborRow()] });
        mockQuery.mockResolvedValueOnce({ rows: [makeKeeperRow()] });
        const res = await agent.get('/admin/harbor?status=keepers');
        expect(res.status).toBe(200);

        const sql = mockQuery.mock.calls[2][0] as string;
        expect(sql).toMatch(/FROM keepers k\s+JOIN customers c ON c\.id = k\.customer_id/);
        expect(sql).toMatch(/ct\.keeper_id = k\.customer_id/);
        expect(sql).toMatch(/km\.keeper_id = k\.customer_id AND km\.deleted_at IS NULL/);
        expect(sql).not.toMatch(/trial_id/);
        expect(sql).toMatch(/ORDER BY last_turn_at DESC NULLS LAST/);

        expect(res.text).toContain(`data-keeper-id="${KEEPER_ID}"`);
        expect(res.text).toContain('keeper@example.com');
        expect(res.text).toContain('I teach pottery and want a class sign-up page.');
        expect(res.text).toContain(`href="/admin/builds/${BUILD_ID}"`);
        const row = res.text.slice(res.text.indexOf(`data-keeper-id="${KEEPER_ID}"`));
        expect(row.slice(0, row.indexOf('</tr>'))).toMatch(/>12<[\s\S]*>5<[\s\S]*>active</);
        // The trial table is not drawn in this view, though the stats above
        // it still count the trials.
        expect(res.text).not.toContain('Trials by recency');
        expect(res.text).not.toContain('data-trial-id=');
        expect(res.text).toMatch(/data-stat="total">1</);
      });

      it('says so when nobody has paid yet', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent.get('/admin/harbor?status=keepers');
        expect(res.status).toBe(200);
        expect(res.text).toContain('No paid keepers yet.');
      });

      it('escapes what a keeper typed and the address they gave', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [] });
        mockQuery.mockResolvedValueOnce({
          rows: [makeKeeperRow({ first_keeper_text: '<script>alert(1)</script>', email: '"><b>x@example.com' })],
        });
        const res = await agent.get('/admin/harbor?status=keepers');
        expect(res.status).toBe(200);
        expect(res.text).not.toContain('<script>alert(1)</script>');
        expect(res.text).not.toContain('"><b>x@example.com');
        expect(res.text).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
      });

      it('the default view offers the filter and does not read the keepers', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [makeHarborRow()] });
        mockQuery.mockResolvedValueOnce({ rows: [] }); // payments needing a person
        const before = mockQuery.mock.calls.length;
        const res = await agent.get('/admin/harbor');
        expect(res.status).toBe(200);
        // Two statements: the trial list and the payments needing a person. The keeper list is not one of them.
        const statements = mockQuery.mock.calls.slice(before).map((c) => String(c[0]));
        expect(statements).toHaveLength(2);
        expect(statements.some((sql) => /FROM keepers k\s+JOIN customers c ON c\.id = k\.customer_id/.test(sql))).toBe(false);
        expect(res.text).toContain('href="/admin/harbor?status=keepers"');
        expect(res.text).toContain('data-filter="keepers"');
        expect(res.text).toContain('Trials by recency');
        expect(res.text).not.toContain('data-keeper-list');
      });

      it('is admin-only: a logged-in customer gets 403 and nothing is read', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('regular@example.com');
        const before = mockQuery.mock.calls.length;
        const res = await agent.get('/admin/harbor?status=keepers');
        expect(res.status).toBe(403);
        expect(mockQuery.mock.calls.length - before).toBe(0);
        expect(res.text).not.toContain('data-keeper-list');
      });

      it('is admin-only: a visitor with no session is sent to the login page', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const res = await request(app).get('/admin/harbor?status=keepers');
        expect(res.status).toBe(302);
        expect(res.headers.location).toContain('/login');
      });

      it('renders 500 when the keeper query throws', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [] });
        mockQuery.mockRejectedValueOnce(new Error('boom'));
        const res = await agent.get('/admin/harbor?status=keepers');
        expect(res.status).toBe(500);
      });
    });

    it('renders 500 when the DB query throws', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockRejectedValueOnce(new Error('boom'));
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(500);
    });

    // A payment the webhook recorded and did not apply is mailed once; the
    // inbox keeps it in front of the operator until Stripe says it ended.
    describe('payments needing a person', () => {
      afterEach(() => {
        mockQuery.mockReset();
      });
      const SUBSCRIPTION = ['sub', 'refused', 'one'].join('_');

      it('lists each subscription still open at Stripe that no keeper row uses, on every view, escaped', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        for (const view of ['', '?status=keepers']) {
          mockQuery.mockResolvedValueOnce({ rows: [] }); // trials
          if (view) mockQuery.mockResolvedValueOnce({ rows: [] }); // keepers
          mockQuery.mockResolvedValueOnce({
            rows: [
              {
                stripe_subscription_id: SUBSCRIPTION,
                stripe_customer_id: ['cus', 'refused', 'one'].join('_'),
                status: 'active',
                monthly_amount_cents: 2900,
                created_at: new Date('2026-05-01T10:00:00Z'),
                keeper_customer_id: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
                email: '"><b>owner@example.com',
              },
            ],
          });
          const before = mockQuery.mock.calls.length;
          const res = await agent.get(`/admin/harbor${view}`);
          expect(res.status).toBe(200);
          expect(res.text).toContain('Payments needing a person');
          expect(res.text).toContain(`data-payment-subscription="${SUBSCRIPTION}"`);
          expect(res.text).toContain('$29.00 a month');
          expect(res.text).not.toContain('"><b>owner@example.com');
          const [sql, params] = mockQuery.mock.calls[mockQuery.mock.calls.length - 1] as [string, unknown[]];
          expect(mockQuery.mock.calls.length - before).toBe(view ? 3 : 2);
          expect(sql).toMatch(/FROM harbor_subscriptions hs/);
          expect(sql).toMatch(/NOT EXISTS \(SELECT 1 FROM keepers k WHERE k\.stripe_subscription_id = hs\.stripe_subscription_id\)/);
          expect(params[0]).toEqual(['canceled', 'incomplete_expired']);
        }
      });

      it('draws nothing when there is none', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [] });
        mockQuery.mockResolvedValueOnce({ rows: [] });
        const res = await agent.get('/admin/harbor');
        expect(res.status).toBe(200);
        expect(res.text).not.toContain('data-payments-needing-a-person');
      });

      it('a read that fails is said on the page, and the trial list is still drawn', async () => {
        process.env.CAS_HARBOR_ENABLED = 'true';
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce({ rows: [makeHarborRow()] });
        mockQuery.mockRejectedValueOnce(new Error('boom'));
        const res = await agent.get('/admin/harbor');
        expect(res.status).toBe(200);
        expect(res.text).toContain('Payments needing a person could not be read.');
        expect(res.text).toContain('Trials by recency');
      });
    });

    // 2026-04-28 Phase 2 polish: add filter chips by status
    // (active/ready/handed_off/all) + a 4th stats card for handed_off +
    // handoff_at relative-time display under the DISPATCHED badge.
    it('renders 4-card stats grid including handed_off + filter chips', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/harbor');
      expect(res.status).toBe(200);
      expect(res.text).toContain('data-stat="handed_off"');
      // Filter chip set (4 chips: all + 3 status filters).
      expect(res.text).toContain('data-filter="all"');
      expect(res.text).toContain('data-filter="active"');
      expect(res.text).toContain('data-filter="ready"');
      expect(res.text).toContain('data-filter="handed_off"');
    });

    it('?status=ready filters to ready-to-handoff trials only (turn_count >= 8 OR phrase match)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [
          // Ready: 9 turns, no handoff yet
          makeHarborRow({ trial_id: '11111111-1111-1111-1111-111111111111', turn_count: '9', memory_count: '12' }),
          // Active but not ready: 3 turns, no phrase match
          makeHarborRow({ trial_id: '22222222-2222-2222-2222-222222222222', turn_count: '3', memory_count: '5' }),
          // Already handed off (should be filtered out from `ready`)
          makeHarborRow({
            trial_id: '33333333-3333-3333-3333-333333333333',
            turn_count: '10',
            memory_count: '20',
            handoff_build_id: 'b-already-dispatched',
            handoff_at: new Date('2026-04-28T14:00:00Z'),
          }),
        ],
      });
      const res = await agent.get('/admin/harbor?status=ready');
      expect(res.status).toBe(200);
      // Only the 9-turn unhandled trial qualifies.
      expect(res.text).toContain('11111111-1111-1111-1111-111111111111');
      expect(res.text).not.toContain('22222222-2222-2222-2222-222222222222');
      expect(res.text).not.toContain('33333333-3333-3333-3333-333333333333');
      // "ready" chip is the active one.
      expect(res.text).toMatch(/border-brand[^"]*"[^>]*data-filter="ready"/);
    });

    it('?status=handed_off filters to dispatched trials and shows handoff relative time', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [
          makeHarborRow({
            trial_id: '44444444-4444-4444-4444-444444444444',
            turn_count: '8',
            handoff_build_id: '00000000-0000-4000-8000-0000000000b1',
            handoff_at: new Date(Date.now() - 30 * 60_000), // 30 min ago
          }),
          // Active row should be filtered out
          makeHarborRow({ trial_id: '55555555-5555-5555-5555-555555555555', turn_count: '3' }),
        ],
      });
      const res = await agent.get('/admin/harbor?status=handed_off');
      expect(res.status).toBe(200);
      expect(res.text).toContain('44444444-4444-4444-4444-444444444444');
      expect(res.text).not.toContain('55555555-5555-5555-5555-555555555555');
      // DISPATCHED badge present + relative time appears under it.
      expect(res.text).toContain('DISPATCHED');
      expect(res.text).toMatch(/30m ago|29m ago/); // window for race
    });

    it('unknown ?status= value falls through to "all" (defensive default)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [makeHarborRow({ trial_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' })],
      });
      const res = await agent.get('/admin/harbor?status=hax%20xss');
      expect(res.status).toBe(200);
      // Row still rendered (not filtered out) + "all" chip is active.
      expect(res.text).toContain('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
      expect(res.text).toMatch(/border-brand[^"]*"[^>]*data-filter="all"/);
    });
  });

  describe('POST /admin/harbor/:trial_id/dispatch (Ocean manual handoff)', () => {
    const ORIGINAL_HARBOR_FLAG = process.env.CAS_HARBOR_ENABLED;
    const TRIAL_ID = '22222222-3333-4444-5555-666666666666';

    // Hard-reset all mocks AND restore pool.connect at the start of
    // each dispatch test. Earlier describes mutate (pool as any).connect
    // (e.g. POST /admin/builds/:id/delete around line 387) and never
    // restore it, so without this guard pool.connect() returns a stale
    // delete-test client whose query stub returns {rows: []} for
    // everything except railway lookup.
    beforeEach(() => {
      mockQuery.mockReset();
      mockExtractSpecFromConversation.mockReset();
      mockTranslateExtractedSpecToV1.mockReset();
      if (mockState.connect) {
        (pool as { connect?: unknown }).connect = mockState.connect;
      }
    });

    afterEach(() => {
      if (ORIGINAL_HARBOR_FLAG === undefined) delete process.env.CAS_HARBOR_ENABLED;
      else process.env.CAS_HARBOR_ENABLED = ORIGINAL_HARBOR_FLAG;
    });

    async function getCsrf(agent: ReturnType<typeof supertest.agent>): Promise<string> {
      process.env.CAS_HARBOR_ENABLED = 'true';
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/harbor');
      const m = res.text.match(/var csrfToken = "([^"]+)"/);
      return m ? m[1] : '';
    }

    it('redirects to /login when not logged in', async () => {
      const res = await request(app).post(`/admin/harbor/${TRIAL_ID}/dispatch`);
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user (after CSRF strip)', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`);
      expect(res.status).toBe(403);
    });

    it('returns 503 when CAS_HARBOR_ENABLED is unset', async () => {
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      delete process.env.CAS_HARBOR_ENABLED;
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(503);
      expect(res.body.error).toBe('harbor_disabled');
    });

    it('returns 400 on a non-UUID trial_id', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      const res = await agent
        .post('/admin/harbor/not-a-uuid/dispatch')
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(400);
      expect(res.body.error).toBe('bad_trial_id');
    });

    it('returns 404 when the trial does not exist', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      // SELECT trial_keepers WHERE ... -> empty
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(404);
      expect(res.body.error).toBe('trial_not_found');
    });

    it('returns the existing buildId on idempotent re-dispatch (no Opus call)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      // Trial already has handoff_build_id set.
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            trial_id: TRIAL_ID,
            email_captured: 'theo@example.com',
            handoff_build_id: '99999999-aaaa-bbbb-cccc-dddddddddddd',
            released_at: null,
          },
        ],
      });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.buildId).toBe('99999999-aaaa-bbbb-cccc-dddddddddddd');
      expect(res.body.alreadyDispatched).toBe(true);
    });

    it('rejects without CSRF token', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`);
      expect(res.status).toBe(403);
    });

    it('returns 409 when the trial has already been promoted to a paid keeper', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            trial_id: TRIAL_ID,
            email_captured: 'theo@example.com',
            handoff_build_id: null,
            released_at: new Date('2026-04-27T18:00:00Z'),
          },
        ],
      });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('trial_already_promoted');
    });

    // The worker writes the owning account's email into the spec as
    // admin_email, and the build engine refuses anything but a plain address
    // there. The operator is told so before anything is spent or written.
    it.each([
      ["o'brien@example.com", 'an apostrophe'],
      ['maya@my_studio.example', 'an underscore in the host'],
      [`jos${String.fromCharCode(233)}@example.com`, 'a letter outside ASCII'],
    ])('refuses to dispatch for a captured address the build engine would not accept (%s: %s)', async (address) => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      const statements: string[] = [];
      mockQuery.mockImplementation(async (sql: string) => {
        statements.push(String(sql));
        if (/SELECT trial_id, email_captured, handoff_build_id,/.test(String(sql))) {
          return { rows: [{ trial_id: TRIAL_ID, email_captured: address, handoff_build_id: null, released_at: null, harbor_meta_overrides: null }] };
        }
        return { rows: [] };
      });
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('email_not_buildable');
      // A plain sentence naming the address and the reason.
      expect(res.body.message).toContain(address);
      expect(res.body.message).toMatch(/build engine does not accept/);
      expect(res.body.message).toMatch(/address captured on this trial; nothing was dispatched/);
      // Nothing was spent and nothing was written: no extraction, no
      // customers row for the address, no build.
      expect(mockExtractSpecFromConversation).not.toHaveBeenCalled();
      expect(mockTranslateExtractedSpecToV1).not.toHaveBeenCalled();
      expect(statements.filter((sql) => /INSERT INTO|UPDATE /.test(sql))).toEqual([]);
      expect(statements.filter((sql) => /FROM trial_keepers\s+WHERE trial_id = \$1/.test(sql))).toHaveLength(1);
    });

    it('says whose address it is when the trial captured none and the build would fall to the admin account', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const original = process.env.ADMIN_EMAIL;
      // The admin logs in under an address the engine would refuse.
      process.env.ADMIN_EMAIL = "admin.o'neil@example.com";
      try {
        const agent = await loginAgent("admin.o'neil@example.com");
        const csrf = await getCsrf(agent);
        mockQuery.mockImplementation(async (sql: string) =>
          /SELECT trial_id, email_captured, handoff_build_id,/.test(String(sql))
            ? { rows: [{ trial_id: TRIAL_ID, email_captured: null, handoff_build_id: null, released_at: null, harbor_meta_overrides: null }] }
            : { rows: [] }
        );
        const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
        expect(res.status).toBe(409);
        expect(res.body.error).toBe('email_not_buildable');
        expect(res.body.message).toContain("admin.o'neil@example.com");
        expect(res.body.message).toMatch(/No address was captured on this trial, so that is your own account/);
        expect(mockExtractSpecFromConversation).not.toHaveBeenCalled();
      } finally {
        if (original === undefined) delete process.env.ADMIN_EMAIL;
        else process.env.ADMIN_EMAIL = original;
      }
    });

    // Helper: install a SQL-pattern-based mockQuery dispatcher that
    // routes each call to the right response by its leading keywords,
    // instead of relying on a strict FIFO `mockResolvedValueOnce` queue.
    // FIFO queues are fragile across nested describes whose
    // `vi.clearAllMocks()` doesn't reset implementation queues, leaving
    // the dispatch tests vulnerable to mock-leak from earlier test
    // bodies. The pattern dispatcher is order-agnostic + auth-route
    // queries (login customer SELECT, harbor list trials SELECT) are
    // matched by their text and don't pollute the dispatch flow.
    //
    // The route dispatches through dispatchHarborTrialBuild, whose own reads
    // match the same patterns: its trial read is the route's columns and more,
    // its insert transaction runs on a client that forwards here, and the
    // trial's row lock is answered as unstamped unless a test says otherwise.
    // A value that is an Error is thrown, as a failing statement would.
    function installDispatcher(byPattern: Array<[RegExp, unknown]>): void {
      mockQuery.mockImplementation(async (sql: string) => {
        const text = String(sql);
        for (const [re, value] of byPattern) {
          if (re.test(text)) {
            if (value instanceof Error) throw value;
            return value;
          }
        }
        // Default for transaction control + login + harbor list reads.
        if (/^\s*BEGIN/i.test(text)) return { rows: [] };
        if (/^\s*COMMIT/i.test(text)) return { rows: [] };
        if (/^\s*ROLLBACK/i.test(text)) return { rows: [] };
        if (/SELECT handoff_build_id, handoff_at FROM trial_keepers WHERE trial_id = \$1 FOR UPDATE/.test(text)) {
          return { rows: [{ handoff_build_id: null, handoff_at: null }] };
        }
        if (/SELECT id, email, name, password, email_verified FROM customers/i.test(text)) {
          // loginAgent's customer SELECT; loginAgent passes the customer
          // row via mockResolvedValueOnce earlier than this dispatcher
          // can be called, so this fallthrough should not normally fire.
          return { rows: [] };
        }
        if (/FROM trial_keepers tk\s+LEFT JOIN/i.test(text)) {
          // /admin/harbor list query.
          return { rows: [] };
        }
        return { rows: [] };
      });
    }

    it('happy path: extracts spec, creates customer, INSERTs build, UPDATEs trial', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'theo@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-uuid-aaa' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-uuid-zzz' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'theo-piano-studio',
          tier: 'build',
          archetype: 'coach-consultant',
          description: 'Piano lessons',
          pages: [{ name: 'home', sections: [] }],
        },
        warnings: [],
        costMicros: 180_000,
        contextSize: { memoryRows: 12, turns: 7 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'theo-piano-studio',
          tier: 'build',
          description: 'Piano lessons',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: { archetype_hint: 'coach-consultant' },
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.buildId).toBe('build-uuid-zzz');
      expect(res.body.customerId).toBe('cust-uuid-aaa');
      expect(res.body.warnings).toEqual([]);
      expect(res.body.costMicros).toBe(180_000);
      expect(mockExtractSpecFromConversation).toHaveBeenCalledWith(
        expect.objectContaining({ pool: expect.anything(), trialId: TRIAL_ID })
      );
      const calls = mockQuery.mock.calls.map((c) => String(c[0]).trim().split(/\s+/)[0]);
      expect(calls).toContain('BEGIN');
      expect(calls).toContain('COMMIT');
      // The captured address is a typed one: the account is created inside the
      // transaction that inserts the build (after BEGIN), not before it.
      const verbs = mockQuery.mock.calls.map((c) => String(c[0]));
      const begin = verbs.findIndex((sql) => /^\s*BEGIN/.test(sql));
      const createAccount = verbs.findIndex((sql) => /INSERT INTO customers/.test(sql));
      expect(createAccount).toBeGreaterThan(begin);
      // The build is inserted through the shared Harbor insert, which marks it
      // as a Harbor build (what the customer's publish gate reads) and records
      // that its dispatch created the owning account.
      const inserts = mockQuery.mock.calls.filter((c) => /INSERT INTO builds/.test(String(c[0])));
      expect(inserts).toHaveLength(1);
      expect(String(inserts[0][0])).toMatch(
        /\(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)\s+VALUES \(\$1, \$2, 'queued', \$3, \$4, TRUE, \$5\)/
      );
      expect((inserts[0][1] as unknown[])[4]).toBe(true);
    });

    it('double dispatch: the loser finds the winner\'s stamp under the trial\'s row lock and returns it, writing nothing', async () => {
      // The route dispatches through the service, whose insert transaction
      // locks the trial row (FOR UPDATE) before it writes anything: a second
      // click that ran its extraction alongside the first waits there, sees
      // the first one's stamp, and commits nothing of its own.
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'twice@example.com', handoff_build_id: null, released_at: null }] },
        ],
        // Another request stamped the trial first: the locked read sees its build.
        [
          /SELECT handoff_build_id, handoff_at FROM trial_keepers WHERE trial_id = \$1 FOR UPDATE/,
          { rows: [{ handoff_build_id: 'build-winner', handoff_at: new Date() }] },
        ],
        [/INSERT INTO customers \(email, password, email_verified, name\)/, { rows: [{ id: 'cust-race' }] }],
        [/INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/, { rows: [{ id: 'build-loser' }] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'race-site', tier: 'build', archetype: 'coach-consultant', description: 'x', pages: [] },
        warnings: [],
        costMicros: 1,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: { spec_version: '1.0', app_name: 'race-site', tier: 'build', description: 'x', data_models: [], user_roles: [], pages: [], _seed_settings: {}, _harbor_meta: {} },
        warnings: [],
      });

      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);

      expect(res.status).toBe(200);
      expect(res.body.alreadyDispatched).toBe(true);
      expect(res.body.buildId).toBe('build-winner');
      const statements = mockQuery.mock.calls.map((c) => String(c[0]));
      expect(statements.some((sql) => /INSERT INTO (builds|customers)|UPDATE trial_keepers/.test(sql))).toBe(false);
      expect(statements.map((sql) => sql.trim().split(/\s+/)[0])).not.toContain('ROLLBACK');
    });

    // A captured address is one a visitor typed. The operator's dispatch of it
    // follows the visitor's rule: the build goes only to an account that
    // dispatch creates. An address that already has an account is refused, in
    // the dispatch's own sentence for the operator.
    it.each([
      ['an unverified account', false, 'address_in_use', /already has an account/],
      ['a verified account', true, 'account_exists', /^That address has a verified account; the owner must log in and build from their account\.$/],
    ] as const)('a captured address that already has %s: 409 %s, before the model call, and nothing is written', async (_label, verified, code, message) => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'someone@example.com', handoff_build_id: null, released_at: null }] },
        ],
        [/SELECT id, email_verified FROM customers WHERE email = \$1/, { rows: [{ id: 'cust-existing', email_verified: verified }] }],
      ]);
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
      expect(res.status).toBe(409);
      expect(res.body.error).toBe(code);
      expect(res.body.message).toMatch(message);
      expect(mockExtractSpecFromConversation).not.toHaveBeenCalled();
      const statements = mockQuery.mock.calls.map((c) => String(c[0]));
      expect(statements.filter((sql) => /INSERT INTO|UPDATE /.test(sql))).toEqual([]);
    });

    it('a captured address that gains an account while the model runs: the insert finds it taken, 409 address_in_use, rolled back, no build', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'concurrent@example.com', handoff_build_id: null, released_at: null }] },
        ],
        // INSERT ... ON CONFLICT DO NOTHING returns no row: a signup got there first.
        [/INSERT INTO customers \(email, password, email_verified, name\)/, { rows: [] }],
        [/INSERT INTO builds/, { rows: [{ id: 'build-never' }] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'x', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: { spec_version: '1.0', app_name: 'x', tier: 'build', description: '', data_models: [], user_roles: [], pages: [], _seed_settings: {}, _harbor_meta: {} },
        warnings: [],
      });

      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('address_in_use');
      const statements = mockQuery.mock.calls.map((c) => String(c[0]));
      expect(statements.some((sql) => /INSERT INTO builds/.test(sql))).toBe(false);
      expect(statements.map((sql) => sql.trim().split(/\s+/)[0])).toContain('ROLLBACK');
    });

    it('a trial that captured no address: the build is attached to the operator\'s own, existing account', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: null, handoff_build_id: null, released_at: null }] },
        ],
        // The admin's account exists and is verified: the upsert finds it.
        [/SELECT id, email_verified FROM customers WHERE email = \$1/, { rows: [{ id: 'cust-admin', email_verified: true }] }],
        [/INSERT INTO customers \(email, password, email_verified, name\)/, { rows: [] }],
        [/INSERT INTO builds/, { rows: [{ id: 'build-admin' }] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'x', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: { spec_version: '1.0', app_name: 'x', tier: 'build', description: '', data_models: [], user_roles: [], pages: [], _seed_settings: {}, _harbor_meta: {} },
        warnings: [],
      });

      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.customerId).toBe('cust-admin');
      const lookup = mockQuery.mock.calls.find((c) => /SELECT id, email_verified FROM customers WHERE email = \$1/.test(String(c[0])));
      expect((lookup![1] as unknown[])[0]).toBe('admin@example.com');
      const insert = mockQuery.mock.calls.find((c) => /INSERT INTO builds/.test(String(c[0])));
      expect((insert![1] as unknown[])[0]).toBe('cust-admin');
      expect((insert![1] as unknown[])[4]).toBe(false);
    });

    it('coerces invalid Opus tier to build + records a warning', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'tier@example.com', handoff_build_id: null, released_at: null }] },
        ],
        [/INSERT INTO customers \(email, password, email_verified, name\)/, { rows: [{ id: 'cust-3' }] }],
        [/INSERT INTO builds/, { rows: [{ id: 'build-3' }] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'y', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'y',
          // Opus hallucinated a non-canon tier.
          tier: 'enterprise' as unknown as 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.warnings).toContain(
        "dispatch: Opus returned tier='enterprise'; coerced to 'build'"
      );
      // Confirm the coerced tier was passed to the INSERT.
      const insertCall = mockQuery.mock.calls.find(
        (c) => typeof c[0] === 'string' && c[0].includes('INSERT INTO builds')
      );
      expect(insertCall?.[1]?.[1]).toBe('build');
    });

    it('answers 503 with the plain explanation when the server has no model key', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'nokey@example.com', handoff_build_id: null, released_at: null }] },
        ],
        [/INSERT INTO customers \(email, password, email_verified, name\)/, { rows: [{ id: 'cust-nokey' }] }],
      ]);
      const { AssistantNotConfiguredError } = await import('../../src/utils/assistant');
      mockExtractSpecFromConversation.mockRejectedValueOnce(new AssistantNotConfiguredError());

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(503);
      expect(res.body.code).toBe('assistant_not_configured');
      const statements = mockQuery.mock.calls.map((c) => String(c[0]));
      expect(statements.some((sql) => sql.includes('INSERT INTO builds'))).toBe(false);
      // The extraction runs before the account for the captured address is
      // written, so a dispatch that ends there leaves no customers row.
      expect(statements.some((sql) => sql.includes('INSERT INTO customers'))).toBe(false);
    });

    // The extraction's other failures get the answers the self-serve route
    // gives (dispatchFailureResponse): none is a 500, and none has written
    // anything.
    it.each([
      ['nothing_to_extract', 409],
      ['model_call_failed', 502],
      ['unusable_reply', 502],
    ] as const)('answers %s from the extraction with %i and its sentence, and writes nothing', async (code, status) => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'theo@example.com', handoff_build_id: null, released_at: null }] },
        ],
      ]);
      const { SpecExtractionError } = await import('../../src/services/spec_extraction');
      mockExtractSpecFromConversation.mockRejectedValueOnce(new SpecExtractionError(code, 'Nothing was built.'));

      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
      expect(res.status).toBe(status);
      expect(res.body.error).toBe(code);
      expect(res.body.message).toMatch(/^Nothing was built\./);
      // The operator is also told why a trial with turns can have nothing to build from.
      expect(/crisis flag/.test(res.body.message)).toBe(code === 'nothing_to_extract');
      const statements = mockQuery.mock.calls.map((c) => String(c[0]));
      expect(statements.filter((sql) => /INSERT INTO|UPDATE /.test(sql))).toEqual([]);
      expect(mockTranslateExtractedSpecToV1).not.toHaveBeenCalled();
    });

    it('answers 503 "busy" when the daily model-call budget is spent', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'theo@example.com', handoff_build_id: null, released_at: null }] },
        ],
      ]);
      const { LlmDailyBudgetError } = await import('../../src/services/llm-budget');
      mockExtractSpecFromConversation.mockRejectedValueOnce(new LlmDailyBudgetError());
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/dispatch`).set('x-csrf-token', csrf);
      expect(res.status).toBe(503);
      expect(res.body.error).toBe('busy');
    });

    it('rolls back the transaction when build INSERT fails', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          { rows: [{ trial_id: TRIAL_ID, email_captured: 'rollback@example.com', handoff_build_id: null, released_at: null }] },
        ],
        [/INSERT INTO customers \(email, password, email_verified, name\)/, { rows: [{ id: 'cust-roll' }] }],
        // Build INSERT fails.
        [/INSERT INTO builds/, new Error('disk full')],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'z', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'z',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(500);
      const calls = mockQuery.mock.calls.map((c) => String(c[0]).trim().split(/\s+/)[0]);
      expect(calls).toContain('BEGIN');
      expect(calls).toContain('ROLLBACK');
      expect(calls).not.toContain('COMMIT');
    });

    it('persists extraction + translation warnings into builds.spec', async () => {
      // Closes an earlier end-to-end finding: previously, only the 2
      // warnings produced by compose.intake.translate_v1_spec at build time
      // were persisted to builds.spec; the dispatch-time extraction +
      // translation warnings were returned in the JSON response and lost
      // when the dispatch handler returned. They must now flow into
      // builds.spec._extraction_warnings + ._translation_warnings.
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'sample@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-sample-1' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-sample-1' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'sample-and-studio',
          tier: 'build',
          archetype: 'healer-wellness',
          description: 'Wellness',
          pages: [{ name: 'sessions', sections: [] }],
          _extraction_warnings: ['hero_tagline is inferred', 'palette inferred'],
        },
        warnings: ['extraction-side: model emitted no seed_rows for Session'],
        costMicros: 180_000,
        contextSize: { memoryRows: 30, turns: 8 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'sample-and-studio',
          tier: 'build',
          description: 'Wellness',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: { archetype_hint: 'healer-wellness' },
          _extraction_warnings: ['hero_tagline is inferred', 'palette inferred'],
        },
        warnings: [
          'translator: page "home" unknown section type "testimonial" dropped',
          'translator: harbor_meta.services empty; falling back to archetype defaults',
        ],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.warnings).toHaveLength(3); // 1 extraction + 2 translation

      // The build INSERT must carry the merged warnings in spec.
      const insertCall = mockQuery.mock.calls.find((c) =>
        /INSERT INTO builds/.test(String(c[0]))
      );
      expect(insertCall).toBeDefined();
      const specJson = (insertCall as unknown as [string, unknown[]])[1][2] as string;
      const persistedSpec = JSON.parse(specJson);
      expect(persistedSpec._extraction_warnings).toEqual(
        expect.arrayContaining([
          'hero_tagline is inferred',
          'palette inferred',
          'extraction-side: model emitted no seed_rows for Session',
        ])
      );
      expect(persistedSpec._translation_warnings).toEqual(
        expect.arrayContaining([
          'translator: page "home" unknown section type "testimonial" dropped',
          'translator: harbor_meta.services empty; falling back to archetype defaults',
        ])
      );
    });

    it('records an agent_learnings row when BARREN_SPEC / THIN_SPEC warning present', async () => {
      // Self-learn loop: after a Harbor dispatch, any
      // BARREN_SPEC/THIN_SPEC warnings emitted by the translator must be
      // INSERTed into agent_learnings(source_agent='backend_engineer',
      // category='harbor_extraction') so the QA engineer agent can spot
      // recurring thin-spec keeper conversations and flag persona-prompt
      // improvements.
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'thinspec@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-thin' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-thin' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'thin', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'thin',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: [
          'translator: page "home" unknown section type "wibble" dropped',
          'BARREN_SPEC: 8 sections seen but 0 handled; build will have empty pages backed only by archetype defaults and may crash post-deploy. Investigate the dropped section types above.',
          'THIN_SPEC: 1/8 sections handled (< 25%); build may be missing significant content.',
        ],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);

      const learningCalls = mockQuery.mock.calls.filter((c) =>
        /INSERT INTO agent_learnings/.test(String(c[0]))
      );
      expect(learningCalls).toHaveLength(1);
      const params = learningCalls[0][1] as unknown[];
      expect(params[0]).toBe('backend_engineer');
      expect(params[1]).toBe('harbor_extraction');
      const learningText = String(params[2]);
      expect(learningText).toContain(`trial=${TRIAL_ID}`);
      expect(learningText).toContain('BARREN_SPEC');
      expect(learningText).toContain('THIN_SPEC');
      // Non-flagged warnings should NOT be in the learning body.
      expect(learningText).not.toContain('wibble');
      // Hash is a 16-char hex string.
      expect(String(params[3])).toMatch(/^[0-9a-f]{16}$/);
    });

    it('does NOT record an agent_learnings row when no BARREN_SPEC / THIN_SPEC warning present', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'fat@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-fat' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-fat' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'fat', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 12, turns: 9 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'fat',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: ['translator: harbor_meta.services empty; falling back to archetype defaults'],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      const learningCalls = mockQuery.mock.calls.filter((c) =>
        /INSERT INTO agent_learnings/.test(String(c[0]))
      );
      expect(learningCalls).toHaveLength(0);
    });

    it('records an archetype_correction learning when Opus guess differs from coerced hint', async () => {
      // Self-learn loop (architect): when Opus emits an archetype id
      // that the translator's KNOWN_ARCHETYPES coercion drops (e.g.
      // "music-instructor" -> undefined), record a row in
      // agent_learnings(source_agent='architect',
      // category='archetype_correction') so the architect agent can
      // propose detect_archetype() keyword expansions.
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'piano@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-piano' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-piano' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'piano-studio',
          tier: 'build',
          // Opus invented an archetype id outside KNOWN_ARCHETYPES.
          archetype: 'music-instructor',
          pages: [],
        },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'piano-studio',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          // Translator dropped the unknown archetype id; no archetype_hint.
          _harbor_meta: {},
        },
        warnings: ['unknown archetype "music-instructor"; worker will re-classify from description'],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);

      const learningCalls = mockQuery.mock.calls.filter((c) =>
        /INSERT INTO agent_learnings/.test(String(c[0]))
      );
      const archetypeLearning = learningCalls.find((c) => {
        const params = c[1] as unknown[];
        return params[1] === 'archetype_correction';
      });
      expect(archetypeLearning, 'expected an archetype_correction learning row').toBeDefined();
      const params = archetypeLearning![1] as unknown[];
      expect(params[0]).toBe('architect');
      expect(params[1]).toBe('archetype_correction');
      const learningText = String(params[2]);
      expect(learningText).toContain(`trial=${TRIAL_ID}`);
      expect(learningText).toContain('"music-instructor"');
      // Coerced value renders as "(none)" when archetype_hint is unset.
      expect(learningText).toContain('"(none)"');
      expect(learningText).toContain('detect_archetype()');
      expect(String(params[3])).toMatch(/^[0-9a-f]{16}$/);
    });

    it('records an archetype_correction learning when Opus picks a different known archetype than the coerced hint', async () => {
      // Edge case: in principle the translator passes through any value
      // that's in KNOWN_ARCHETYPES unchanged, but a future translator
      // could remap (e.g. alias collapse). We still want the signal
      // recorded whenever the post-translation hint differs from Opus's
      // raw guess.
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'remap@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-remap' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-remap' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'remap',
          tier: 'build',
          archetype: 'local-service',
          pages: [],
        },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'remap',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: { archetype_hint: 'tradesperson-contractor' },
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      const learningCalls = mockQuery.mock.calls.filter((c) =>
        /INSERT INTO agent_learnings/.test(String(c[0]))
      );
      const archetypeLearning = learningCalls.find((c) => {
        const params = c[1] as unknown[];
        return params[1] === 'archetype_correction';
      });
      expect(archetypeLearning).toBeDefined();
      const learningText = String((archetypeLearning![1] as unknown[])[2]);
      expect(learningText).toContain('"local-service"');
      expect(learningText).toContain('"tradesperson-contractor"');
    });

    it('does NOT record an archetype_correction learning when Opus guess equals coerced hint', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'match@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-match' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-match' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'match',
          tier: 'build',
          archetype: 'coach-consultant',
          pages: [],
        },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'match',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: { archetype_hint: 'coach-consultant' },
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      const archetypeLearningCalls = mockQuery.mock.calls.filter((c) => {
        if (!/INSERT INTO agent_learnings/.test(String(c[0]))) return false;
        const params = c[1] as unknown[];
        return params[1] === 'archetype_correction';
      });
      expect(archetypeLearningCalls).toHaveLength(0);
    });

    it('does NOT record an archetype_correction learning when Opus emits no archetype', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'silent@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-silent' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-silent' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, { rows: [] }],
      ]);
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'silent',
          tier: 'build',
          // No `archetype` key at all.
          pages: [],
        },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'silent',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      const archetypeLearningCalls = mockQuery.mock.calls.filter((c) => {
        if (!/INSERT INTO agent_learnings/.test(String(c[0]))) return false;
        const params = c[1] as unknown[];
        return params[1] === 'archetype_correction';
      });
      expect(archetypeLearningCalls).toHaveLength(0);
    });

    it('still returns 200 when the archetype_correction learning INSERT fails (best-effort)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      mockQuery.mockImplementation(async (sql: string, params?: unknown[]) => {
        const text = String(sql);
        if (/SELECT trial_id, email_captured, handoff_build_id,/.test(text)) {
          return {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'arch-broken@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          };
        }
        if (/INSERT INTO customers/.test(text)) return { rows: [{ id: 'cust-ab' }] };
        if (/INSERT INTO builds/.test(text)) return { rows: [{ id: 'build-ab' }] };
        if (/FOR UPDATE/.test(text)) return { rows: [{ handoff_build_id: null, handoff_at: null }] };
        if (/UPDATE trial_keepers/.test(text)) return { rows: [] };
        if (/INSERT INTO agent_learnings/.test(text)) {
          // Throw only on the archetype_correction row; allow other
          // categories (BARREN_SPEC/THIN_SPEC) to succeed silently.
          if (Array.isArray(params) && params[1] === 'archetype_correction') {
            throw new Error('agent_learnings table dropped');
          }
          return { rows: [] };
        }
        if (/^\s*BEGIN/i.test(text)) return { rows: [] };
        if (/^\s*COMMIT/i.test(text)) return { rows: [] };
        if (/^\s*ROLLBACK/i.test(text)) return { rows: [] };
        return { rows: [] };
      });
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'ab',
          tier: 'build',
          archetype: 'astro-cartographer',
          pages: [],
        },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'ab',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: [],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.buildId).toBe('build-ab');
    });

    it('still returns 200 when the agent_learnings INSERT fails (best-effort)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      installDispatcher([
        [
          /SELECT trial_id, email_captured, handoff_build_id,/,
          {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'broken-learn@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          },
        ],
        [
          /INSERT INTO customers \(email, password, email_verified, name\)/,
          { rows: [{ id: 'cust-bl' }] },
        ],
        [
          /INSERT INTO builds \(customer_id, tier, status, spec, app_name, is_harbor, owner_created_by_dispatch\)/,
          { rows: [{ id: 'build-bl' }] },
        ],
        [/UPDATE trial_keepers\s+SET handoff_build_id/, { rows: [] }],
        [/INSERT INTO agent_learnings/, new Error('agent_learnings table does not exist')],
      ]);
      // Tweak the dispatcher: rejecting from a regex-keyed value array is
      // not native; install a one-shot rejection by re-implementing.
      mockQuery.mockImplementation(async (sql: string) => {
        const text = String(sql);
        if (/SELECT trial_id, email_captured, handoff_build_id,/.test(text)) {
          return {
            rows: [
              {
                trial_id: TRIAL_ID,
                email_captured: 'broken-learn@example.com',
                handoff_build_id: null,
                released_at: null,
              },
            ],
          };
        }
        if (/INSERT INTO customers/.test(text)) return { rows: [{ id: 'cust-bl' }] };
        if (/INSERT INTO builds/.test(text)) return { rows: [{ id: 'build-bl' }] };
        if (/FOR UPDATE/.test(text)) return { rows: [{ handoff_build_id: null, handoff_at: null }] };
        if (/UPDATE trial_keepers/.test(text)) return { rows: [] };
        if (/INSERT INTO agent_learnings/.test(text)) {
          throw new Error('agent_learnings table does not exist');
        }
        if (/^\s*BEGIN/i.test(text)) return { rows: [] };
        if (/^\s*COMMIT/i.test(text)) return { rows: [] };
        if (/^\s*ROLLBACK/i.test(text)) return { rows: [] };
        return { rows: [] };
      });
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: { spec_version: '1.0', app_name: 'bl', tier: 'build', pages: [] },
        warnings: [],
        costMicros: 100,
        contextSize: { memoryRows: 1, turns: 1 },
      });
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'bl',
          tier: 'build',
          description: '',
          data_models: [],
          user_roles: [],
          pages: [],
          _seed_settings: {},
          _harbor_meta: {},
        },
        warnings: [
          'BARREN_SPEC: 4 sections seen but 0 handled; build will have empty pages backed only by archetype defaults and may crash post-deploy. Investigate the dropped section types above.',
        ],
      });

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      // Dispatch must still succeed even though the learning INSERT threw.
      expect(res.status).toBe(200);
      expect(res.body.buildId).toBe('build-bl');
    });
  });

  // ─────────────────────────────────────────────────────────────────
  // POST /admin/harbor/:trial_id/crawl-source-site
  // GET  /admin/harbor/:trial_id/source-preview
  // POST /admin/harbor/:trial_id/clear-source
  // (harbor-faithful-copy-admin-trigger-route, 2026-05-09)
  // ─────────────────────────────────────────────────────────────────

  describe('Harbor faithful-copy crawl trigger routes', () => {
    const ORIGINAL_HARBOR_FLAG = process.env.CAS_HARBOR_ENABLED;
    const TRIAL_ID = '88888888-9999-aaaa-bbbb-cccccccccccc';

    beforeEach(() => {
      mockQuery.mockReset();
      mockCrawlSite.mockReset();
      if (mockState.connect) {
        (pool as { connect?: unknown }).connect = mockState.connect;
      }
    });

    afterEach(() => {
      if (ORIGINAL_HARBOR_FLAG === undefined) delete process.env.CAS_HARBOR_ENABLED;
      else process.env.CAS_HARBOR_ENABLED = ORIGINAL_HARBOR_FLAG;
    });

    async function getCsrf(agent: ReturnType<typeof supertest.agent>): Promise<string> {
      process.env.CAS_HARBOR_ENABLED = 'true';
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/harbor');
      const m = res.text.match(/var csrfToken = "([^"]+)"/);
      return m ? m[1] : '';
    }

    // POST /crawl-source-site: auth + CSRF + flag

    it('crawl-source-site redirects to /login when not logged in', async () => {
      const res = await request(app).post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`);
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('crawl-source-site returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`);
      expect(res.status).toBe(403);
    });

    it('crawl-source-site returns 503 when CAS_HARBOR_ENABLED is unset', async () => {
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      delete process.env.CAS_HARBOR_ENABLED;
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(503);
    });

    it('crawl-source-site rejects non-UUID trial_id', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      const res = await agent
        .post('/admin/harbor/not-a-uuid/crawl-source-site')
        .set('x-csrf-token', csrf)
        .send({ source_url: 'https://example.com/' });
      expect(res.status).toBe(400);
      expect(res.body.error).toBe('bad_trial_id');
    });

    it('crawl-source-site rejects missing source_url', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf)
        .send({});
      expect(res.status).toBe(400);
      expect(res.body.error).toBe('source_url_required');
    });

    it('crawl-source-site rejects non-http(s) source_url', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf)
        .send({ source_url: 'javascript:alert(1)' });
      expect(res.status).toBe(400);
      expect(res.body.error).toBe('source_url_must_be_http');
    });

    it('crawl-source-site returns 404 when trial does not exist', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      // SELECT returns no rows
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf)
        .send({ source_url: 'https://example.com/' });
      expect(res.status).toBe(404);
      expect(res.body.error).toBe('trial_not_found');
    });

    it('crawl-source-site returns 409 when trial already promoted (released_at set)', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      mockQuery.mockResolvedValueOnce({
        rows: [{ trial_id: TRIAL_ID, released_at: new Date('2026-05-08') }],
      });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf)
        .send({ source_url: 'https://example.com/' });
      expect(res.status).toBe(409);
      expect(res.body.error).toBe('trial_already_promoted');
    });

    it('crawl-source-site returns 502 when crawler captures zero pages', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      mockQuery.mockResolvedValueOnce({
        rows: [{ trial_id: TRIAL_ID, released_at: null }],
      });
      mockCrawlSite.mockResolvedValueOnce({
        source_url: 'https://broken.example/',
        pages: [],
        warnings: ['fetch failed: ECONNREFUSED'],
      });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf)
        .send({ source_url: 'https://broken.example/' });
      expect(res.status).toBe(502);
      expect(res.body.error).toBe('crawl_yielded_no_pages');
      expect(res.body.warnings).toContain('fetch failed: ECONNREFUSED');
    });

    it('crawl-source-site happy path persists copy_source_url + copy_pages', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      // SELECT trial_keepers (exists, not released)
      mockQuery.mockResolvedValueOnce({
        rows: [{ trial_id: TRIAL_ID, released_at: null }],
      });
      mockCrawlSite.mockResolvedValueOnce({
        source_url: 'https://sample-irrigation.example/',
        pages: [
          { path: '/', markdown_blocks: ['# Sample Irrigation Co', 'A sample paragraph of copy.'] },
          { path: '/about', markdown_blocks: ['# About', 'Twelve years.'] },
        ],
        warnings: [],
      });
      // UPDATE harbor_meta_overrides
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/crawl-source-site`)
        .set('x-csrf-token', csrf)
        .send({ source_url: 'https://sample-irrigation.example/' });
      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(res.body.pageCount).toBe(2);
      expect(res.body.pagePaths).toEqual(['/', '/about']);
      // Verify the UPDATE SQL ran with the JSONB merge.
      const updateCall = mockQuery.mock.calls.find((c: unknown[]) =>
        typeof c[0] === 'string' && (c[0] as string).includes('harbor_meta_overrides = COALESCE')
      );
      expect(updateCall).toBeDefined();
    });

    // GET /source-preview: read-only, requireAdmin only

    it('source-preview redirects to /login when not logged in', async () => {
      const res = await request(app).get(`/admin/harbor/${TRIAL_ID}/source-preview`);
      expect(res.status).toBe(302);
    });

    it('source-preview returns 503 when flag off', async () => {
      const agent = await loginAgent('admin@example.com');
      delete process.env.CAS_HARBOR_ENABLED;
      const res = await agent.get(`/admin/harbor/${TRIAL_ID}/source-preview`);
      expect(res.status).toBe(503);
    });

    it('source-preview returns 404 for unknown trial', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get(`/admin/harbor/${TRIAL_ID}/source-preview`);
      expect(res.status).toBe(404);
    });

    it('source-preview returns trimmed copy-pages preview', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{
          trial_id: TRIAL_ID,
          harbor_meta_overrides: {
            copy_source_url: 'https://sample-irrigation.example/',
            copy_pages: [
              {
                path: '/',
                title: 'Sample Irrigation Co',
                markdown_blocks: ['A sample paragraph of copy. ' + 'x'.repeat(500)],
              },
              { path: '/about', markdown_blocks: ['About copy.'] },
            ],
          },
        }],
      });
      const res = await agent.get(`/admin/harbor/${TRIAL_ID}/source-preview`);
      expect(res.status).toBe(200);
      expect(res.body.copySourceUrl).toBe('https://sample-irrigation.example/');
      expect(res.body.pageCount).toBe(2);
      expect(res.body.preview[0].path).toBe('/');
      expect(res.body.preview[0].title).toBe('Sample Irrigation Co');
      // First-block snippet capped at 200 chars.
      expect(res.body.preview[0].firstBlockSnippet.length).toBeLessThanOrEqual(200);
    });

    it('source-preview returns empty preview when no overrides set', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ trial_id: TRIAL_ID, harbor_meta_overrides: null }],
      });
      const res = await agent.get(`/admin/harbor/${TRIAL_ID}/source-preview`);
      expect(res.status).toBe(200);
      expect(res.body.pageCount).toBe(0);
      expect(res.body.copySourceUrl).toBeNull();
    });

    it('source-preview logs an audit line on successful view', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ trial_id: TRIAL_ID, harbor_meta_overrides: null }],
      });
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
      try {
        const res = await agent.get(`/admin/harbor/${TRIAL_ID}/source-preview`);
        expect(res.status).toBe(200);
        const logged = logSpy.mock.calls.some((call) => {
          const fmt = String(call[0] ?? '');
          if (!fmt.includes('[admin/harbor/source-preview] viewed')) return false;
          return call.some((arg) => arg === TRIAL_ID) &&
            call.some((arg) => arg === 'admin@example.com');
        });
        expect(logged).toBe(true);
      } finally {
        logSpy.mockRestore();
      }
    });

    // POST /clear-source: auth + CSRF

    it('clear-source returns 403 for non-admin', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.post(`/admin/harbor/${TRIAL_ID}/clear-source`);
      expect(res.status).toBe(403);
    });

    it('clear-source happy path resets harbor_meta_overrides to NULL', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/clear-source`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      const updateCall = mockQuery.mock.calls.find((c: unknown[]) =>
        typeof c[0] === 'string' && (c[0] as string).includes('SET harbor_meta_overrides = NULL')
      );
      expect(updateCall).toBeDefined();
    });

    it('clear-source returns 404 when trial does not exist', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);
      mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 0 });
      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/clear-source`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(404);
    });
  });

  // ─────────────────────────────────────────────────────────────────
  // dispatch handler MERGES harbor_meta_overrides into the spec's
  // _harbor_meta when the trial has overrides set.
  // ─────────────────────────────────────────────────────────────────

  describe('POST /admin/harbor/:trial_id/dispatch: harbor_meta_overrides merge', () => {
    const ORIGINAL_HARBOR_FLAG = process.env.CAS_HARBOR_ENABLED;
    const TRIAL_ID = '99999999-aaaa-bbbb-cccc-dddddddddddd';

    beforeEach(() => {
      mockQuery.mockReset();
      mockExtractSpecFromConversation.mockReset();
      mockTranslateExtractedSpecToV1.mockReset();
      if (mockState.connect) {
        (pool as { connect?: unknown }).connect = mockState.connect;
      }
    });

    afterEach(() => {
      if (ORIGINAL_HARBOR_FLAG === undefined) delete process.env.CAS_HARBOR_ENABLED;
      else process.env.CAS_HARBOR_ENABLED = ORIGINAL_HARBOR_FLAG;
    });

    async function getCsrf(agent: ReturnType<typeof supertest.agent>): Promise<string> {
      process.env.CAS_HARBOR_ENABLED = 'true';
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/harbor');
      const m = res.text.match(/var csrfToken = "([^"]+)"/);
      return m ? m[1] : '';
    }

    it('merges harbor_meta_overrides into spec._harbor_meta on dispatch', async () => {
      process.env.CAS_HARBOR_ENABLED = 'true';
      const agent = await loginAgent('admin@example.com');
      const csrf = await getCsrf(agent);

      // SELECT trial_keepers (the route's read and the dispatch service's): has overrides.
      const trialWithOverrides = {
        trial_id: TRIAL_ID,
        email_captured: 'sample-irrigation@example.com',
        handoff_build_id: null,
        released_at: null,
        harbor_meta_overrides: {
          copy_source_url: 'https://sample-irrigation.example/',
          copy_pages: [{ path: '/', markdown_blocks: ['# Sample Irrigation Co'] }],
        },
      };
      mockQuery.mockImplementation(async (sql: string) =>
        /SELECT trial_id, email_captured, handoff_build_id,/.test(String(sql))
          ? { rows: [trialWithOverrides] }
          : { rows: [] }
      );

      // Opus extraction returns spec with no _harbor_meta initially.
      mockExtractSpecFromConversation.mockResolvedValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'sample-irrigation-test',
          tier: 'build',
          description: 'Irrigation specialist.',
        },
        warnings: [],
      });
      // Translator passes through.
      mockTranslateExtractedSpecToV1.mockReturnValueOnce({
        spec: {
          spec_version: '1.0',
          app_name: 'sample-irrigation-test',
          tier: 'build',
          description: 'Irrigation specialist.',
          _harbor_meta: { archetype_hint: 'tradesperson-contractor' },
        },
        warnings: [],
      });

      // Capture the INSERT to verify the spec carries the merged _harbor_meta.
      let insertedSpec: Record<string, unknown> = {};
      // The dispatch service's insert transaction: BEGIN, the trial's row
      // lock, the account it creates for the captured address, INSERT builds
      // (RETURNING id), UPDATE trial_keepers, COMMIT.
      const txClient = {
        query: vi.fn().mockImplementation(async (sql: string, params?: unknown[]) => {
          if (typeof sql === 'string' && sql.startsWith('BEGIN')) return { rows: [] };
          if (typeof sql === 'string' && /FOR UPDATE/.test(sql)) return { rows: [{ handoff_build_id: null, handoff_at: null }] };
          if (typeof sql === 'string' && sql.includes('INSERT INTO customers')) return { rows: [{ id: 'cust-1' }] };
          if (typeof sql === 'string' && sql.includes('INSERT INTO builds')) {
            insertedSpec = JSON.parse(params?.[2] as string);
            return { rows: [{ id: 'build-merge-1' }] };
          }
          if (typeof sql === 'string' && sql.startsWith('UPDATE trial_keepers')) {
            return { rows: [], rowCount: 1 };
          }
          if (typeof sql === 'string' && sql.startsWith('COMMIT')) return { rows: [] };
          return { rows: [] };
        }),
        release: vi.fn(),
      };
      (pool as { connect?: unknown }).connect = vi.fn().mockResolvedValue(txClient);

      const res = await agent
        .post(`/admin/harbor/${TRIAL_ID}/dispatch`)
        .set('x-csrf-token', csrf);
      expect(res.status).toBe(200);
      expect(res.body.buildId).toBe('build-merge-1');

      // Spec INSERT'd into builds carries the merged _harbor_meta with
      // BOTH the translator's archetype_hint AND the operator's
      // copy_source_url + copy_pages.
      const meta = (insertedSpec._harbor_meta || {}) as Record<string, unknown>;
      expect(meta.archetype_hint).toBe('tradesperson-contractor');
      expect(meta.copy_source_url).toBe('https://sample-irrigation.example/');
      expect(Array.isArray(meta.copy_pages)).toBe(true);
      // Warnings include the merge note so the SE reviewer can see it.
      const warnings = (insertedSpec._translation_warnings || []) as string[];
      expect(warnings.some((w) => /harbor_meta_overrides/.test(w))).toBe(true);
    });
  });
});


// ---------------------------------------------------------------------------
// Review round additions
// ---------------------------------------------------------------------------
describe('Admin review-round fixes', () => {
  function installClient(handler: (sql: string) => unknown) {
    const client = {
      query: vi.fn().mockImplementation((sql: string) => Promise.resolve(handler(String(sql)))),
      release: vi.fn(),
    };
    (pool as any).connect = vi.fn().mockResolvedValue(client);
    return client;
  }

  beforeEach(() => {
    mockQuery.mockReset();
    mockIsRailwayWriteEnabled.mockReset();
    mockIsRailwayWriteEnabled.mockReturnValue(true);
    mockDeleteRailwayProject.mockReset();
    mockDeleteRailwayProject.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('cleanup ranks per customer and never deletes builds still in the pipeline', async () => {
    const agent = await loginAgent('admin@example.com');
    const client = installClient(() => ({ rows: [] }));
    const res = await agent.post('/admin/builds/cleanup').type('form').send({ _csrf: getValidCsrf() });
    expect(res.status).toBe(302);
    const select = client.query.mock.calls.map((c: any) => String(c[0])).find((q: string) => q.includes('ROW_NUMBER'));
    expect(select).toBeDefined();
    // app_name is only unique within a customer.
    expect(select).toMatch(/PARTITION BY customer_id, app_name/);
    for (const status of ['queued', 'building', 'testing', 'deploying', 'preview']) {
      expect(select).toContain(`'${status}'`);
    }
    expect(select).toMatch(/status NOT IN \(/);
  });

  it('ai-fix resets only if the build is still in a resettable status (atomic), else 409', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'b-9', customer_id: 'c', tier: 'build', app_name: 'x', spec: {}, status: 'building' }],
    });
    mockQuery.mockResolvedValueOnce({ rowCount: 0 }); // finished in the meantime
    const res = await agent
      .post('/admin/builds/b-9/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'redo it' });
    expect(res.status).toBe(409);
    const update = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('UPDATE builds'));
    expect(String(update![0])).toMatch(/status = ANY\(\$3::text\[\]\)/);
    expect((update![1] as unknown[])[2]).toEqual(['failed', 'building', 'testing', 'deploying', 'queued']);
  });

  it('ai-fix with an array-valued rebuild_request answers 400 instead of throwing', async () => {
    const agent = await loginAgent('admin@example.com');
    const res = await agent
      .post('/admin/builds/b-9/ai-fix')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&rebuild_request[]=a&rebuild_request[]=b');
    expect(res.status).toBe(400);
  });

  it('delete never touches the project this server itself runs in, even when PROTECTED_RAILWAY_PROJECT_IDS is empty', async () => {
    vi.stubEnv('PROTECTED_RAILWAY_PROJECT_IDS', '');
    vi.stubEnv('RAILWAY_PROJECT_ID', '00000000-0000-4000-8000-0000000000aa');
    const agent = await loginAgent('admin@example.com');
    const client = installClient((sql) =>
      sql.includes('SELECT railway_project_id')
        ? { rows: [{ railway_project_id: '00000000-0000-4000-8000-0000000000aa', app_name: 'platform' }] }
        : { rows: [] }
    );
    const res = await agent.post('/admin/builds/b-own/delete').type('form').send({ _csrf: getValidCsrf() });
    expect(res.status).toBe(403);
    expect(mockDeleteRailwayProject).not.toHaveBeenCalled();
    expect(client.query.mock.calls.map((c: any) => c[0])).not.toContain('BEGIN');
  });

  it('GET /admin/emails/:id answers 404 (not a 500) for an id that is not a UUID', async () => {
    const agent = await loginAgent('admin@example.com');
    const res = await agent.get('/admin/emails/not-a-uuid');
    expect(res.status).toBe(404);
    expect(mockQuery.mock.calls.some((c: unknown[]) => String(c[0]).includes('FROM email_log WHERE id'))).toBe(false);
  });

  it('admin gallery opt-in bounds free text to the column sizes and ignores non-strings', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b', gallery_opted_in: true }] });
    await agent
      .post('/admin/gallery/b-1/optin')
      .type('form')
      .send({
        _csrf: getValidCsrf(),
        gallery_description: 'd'.repeat(900),
        gallery_category: 'c'.repeat(300),
        gallery_display_name: 'n'.repeat(400),
      });
    const update = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('gallery_opted_in = NOT'));
    const params = update![1] as Array<string | boolean | null>;
    expect((params[0] as string).length).toBe(500);
    expect((params[1] as string).length).toBe(100);
    expect((params[2] as string).length).toBe(120);
  });

  it('findings: array-valued notes are dropped rather than sent to Postgres as an array', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rowCount: 1, rows: [{ id: 'f' }] });
    const res = await agent
      .post('/admin/findings/f-1/review')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&notes[]=x&notes[]=y');
    expect(res.status).toBe(302);
    const update = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('UPDATE coordinator_findings'));
    expect((update![1] as unknown[])[1]).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// A review verdict belongs to one deployment. The two admin statements that put
// an existing build back to 'queued' must clear it, exactly like the retry and
// requeue statements in routes/builds.ts and routes/ops-api.ts. The real-database
// proof is in money-path.realdb.test.ts; these tests pin the SQL text.
// ---------------------------------------------------------------------------
describe('Admin requeue statements clear the review verdict', () => {
  /** "review_status = NULL,\n   reviewed_at = NULL, ..." as a list of "column = value" pieces. */
  const assignments = REVIEW_RESET_ASSIGNMENTS.split(',').map((piece) => piece.replace(/\s+/g, ' ').trim());
  const squash = (sql: unknown): string => String(sql).replace(/\s+/g, ' ');

  const failedBuild = { id: 'b-7', customer_id: 'c-1', tier: 'build', app_name: 'Site', spec: {}, status: 'failed' };

  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('the shared assignment list covers the verdict and every review report column', () => {
    // A guard on the constant itself: emptying it would make the checks below vacuous.
    expect(assignments).toEqual([
      'review_status = NULL',
      'reviewed_at = NULL',
      'reviewed_by = NULL',
      'review_notes = NULL',
      'review_report = NULL',
      'ai_review_iterations = 0',
      'ai_reviewed_at = NULL',
    ]);
  });

  it('POST /admin/builds/:id/ai-fix: the in-place reset to queued includes every assignment', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [failedBuild] });
    mockQuery.mockResolvedValueOnce({ rowCount: 1 });
    const res = await agent
      .post('/admin/builds/b-7/ai-fix')
      .type('form')
      .send({ _csrf: getValidCsrf(), rebuild_request: 'try again', force_new: '' });
    expect(res.status).toBe(302);

    const update = mockQuery.mock.calls.find((c: unknown[]) => /UPDATE builds/.test(String(c[0])));
    expect(update, 'an UPDATE builds statement was issued').toBeDefined();
    const sql = squash(update![0]);
    expect(sql).toContain("SET status = 'queued'");
    expect(sql).toContain('rebuild_request = $2');
    for (const assignment of assignments) {
      expect(sql, assignment).toContain(assignment);
    }
    // The assignments sit in the SET list, before the WHERE clause.
    expect(sql.indexOf('ai_reviewed_at = NULL')).toBeLessThan(sql.indexOf('WHERE'));
  });

  it('POST /admin/builds/:id/reset: puts a failed build back with every assignment, and only a failed one', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-7' }] });
    const res = await agent
      .post('/admin/builds/b-7/reset')
      .type('form')
      .send({ _csrf: getValidCsrf() });
    expect(res.status).toBe(302);

    const update = mockQuery.mock.calls.find((c: unknown[]) => /UPDATE builds/.test(String(c[0])));
    expect(update, 'an UPDATE builds statement was issued').toBeDefined();
    const sql = squash(update![0]);
    expect(sql).toContain("SET status = 'queued'");
    expect(sql).toContain('retry_count = 0');
    for (const assignment of assignments) {
      expect(sql, assignment).toContain(assignment);
    }
    expect(sql.indexOf('ai_reviewed_at = NULL')).toBeLessThan(sql.indexOf('WHERE'));
    expect(sql).toContain("WHERE id = $1 AND status = 'failed'");
    expect(update![1]).toEqual(['b-7']);
  });

  it('POST /admin/builds/:id/reset: a build that is not failed answers 404', async () => {
    const agent = await loginAgent('admin@example.com');
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await agent
      .post('/admin/builds/b-8/reset')
      .type('form')
      .send({ _csrf: getValidCsrf() });
    expect(res.status).toBe(404);
  });
});
