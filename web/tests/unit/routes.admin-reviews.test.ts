import { describe, it, expect, vi, beforeEach } from 'vitest';
import request from 'supertest';
import supertest from 'supertest';
import app from '../../src/app';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

// Set ADMIN_EMAIL before app loads routes
process.env.ADMIN_EMAIL = 'admin@example.com';

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

// Mock services that admin.ts and other mounted routes depend on.
// Mirror the shape used by routes.admin.test.ts so shared routes don't blow up.
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendAppLiveEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
  sendReviewReadyEmail: vi.fn().mockResolvedValue(undefined),
  sendReviewChangesRequestedEmail: vi.fn().mockResolvedValue(undefined),
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
}));

import pool from '../../src/db/index';
import { readFileSync } from 'fs';
import { resolve } from 'path';
import {
  BUILD_LINT_RUN_MARKER,
  BUILD_WARNING_LOG_ENTRY_LIMIT,
  BUILD_WARNING_LOG_SQL,
  readBuildWarningLog,
} from '../../src/routes/admin';
import {
  sendReviewReadyEmail,
  sendReviewChangesRequestedEmail,
} from '../../src/services/email';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const mockSendReviewReady = sendReviewReadyEmail as ReturnType<typeof vi.fn>;
const mockSendReviewChangesRequested = sendReviewChangesRequestedEmail as ReturnType<typeof vi.fn>;

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

// Helpers for building mock rows. Keeps the tests readable.
function makeBuildRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    build_id: 'b-1',
    app_name: 'my-app',
    tier: 'build',
    status: 'preview',
    review_status: 'pending',
    reviewed_at: null,
    reviewed_by: null,
    review_notes: null,
    preview_url: 'https://preview.example.com',
    created_at: new Date('2026-04-10T10:00:00Z'),
    updated_at: new Date('2026-04-10T12:00:00Z'),
    screenshots: [],
    customer_email: 'customer@example.com',
    ...overrides,
  };
}

function makeStatsRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    pending_count: '2',
    reviewed_today: '1',
    total_reviewed: '5',
    ...overrides,
  };
}

describe('Admin Review Routes (section 04)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockSendReviewReady.mockClear();
    mockSendReviewChangesRequested.mockClear();
  });

  // ----------------------------------------------------------------
  // GET /admin/reviews
  // ----------------------------------------------------------------
  describe('GET /admin/reviews', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/admin/reviews');
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.get('/admin/reviews');
      expect(res.status).toBe(403);
    });

    it('renders the queue with stats for admin', async () => {
      const agent = await loginAgent('admin@example.com');
      // Both queries fire via Promise.all; supply in call order: builds, stats.
      mockQuery.mockResolvedValueOnce({
        rows: [
          makeBuildRow({ build_id: 'b-pending', review_status: 'pending' }),
          makeBuildRow({ build_id: 'b-approved', review_status: 'approved' }),
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [makeStatsRow()] });
      const res = await agent.get('/admin/reviews');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Senior Web Craftsman Reviews');
      // Stats surface in the page
      expect(res.text).toContain('data-stat="pending_count"');
      expect(res.text).toContain('data-stat="reviewed_today"');
      expect(res.text).toContain('data-stat="total_reviewed"');
      // At least one row renders
      expect(res.text).toContain('my-app');
    });

    it('shows empty state when there are no builds in the queue', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({
        rows: [makeStatsRow({ pending_count: '0', reviewed_today: '0', total_reviewed: '0' })],
      });
      const res = await agent.get('/admin/reviews');
      expect(res.status).toBe(200);
      expect(res.text).toContain('No builds awaiting review');
    });

    it('uses ORDER BY that puts pending before changes_requested before approved', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [makeStatsRow()] });
      await agent.get('/admin/reviews');
      // The first query after login is the builds SELECT.
      const buildsSql = mockQuery.mock.calls[1][0] as string;
      expect(buildsSql).toContain("WHEN 'pending' THEN 0");
      expect(buildsSql).toContain("WHEN 'changes_requested' THEN 1");
      expect(buildsSql).toContain("WHEN 'approved' THEN 2");
    });

    it('mobile: review-queue tables carry cas-mobile-cards class + per-cell data-label', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [makeBuildRow({ build_id: 'b-pending', review_status: 'pending' })],
      });
      mockQuery.mockResolvedValueOnce({ rows: [makeStatsRow()] });
      const res = await agent.get('/admin/reviews');
      expect(res.status).toBe(200);
      // All four review-queue tables get the mobile-card hook
      const cardClassMatches = res.text.match(/cas-mobile-cards/g) || [];
      expect(cardClassMatches.length).toBeGreaterThanOrEqual(4);
      // Each populated row carries data-label hints so card-mode shows column headers
      expect(res.text).toContain('data-label="App"');
      expect(res.text).toContain('data-label="Customer"');
      expect(res.text).toContain('data-label="Action"');
      // Preview links carry noreferrer alongside noopener
      expect(res.text).toMatch(/rel="noopener noreferrer"/);
    });
  });

  // ----------------------------------------------------------------
  // GET /admin/reviews/:buildId
  // ----------------------------------------------------------------
  describe('GET /admin/reviews/:buildId', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/admin/reviews/b-1');
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.get('/admin/reviews/b-1');
      expect(res.status).toBe(403);
    });

    it('renders the detail page with customer + logs', async () => {
      const agent = await loginAgent('admin@example.com');
      // build SELECT
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: 'b-1',
            app_name: 'my-app',
            tier: 'build',
            status: 'preview',
            review_status: 'pending',
            reviewed_at: null,
            reviewed_by: null,
            review_notes: null,
            preview_url: 'https://preview.example.com',
            live_url: null,
            spec: {},
            screenshots: [],
            customer_id: 'c-1',
            created_at: new Date(),
            updated_at: new Date(),
          },
        ],
      });
      // customer SELECT (Promise.all, first)
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'c-1', email: 'customer@example.com', name: 'Customer' }],
      });
      // build_logs SELECT (Promise.all, second)
      mockQuery.mockResolvedValueOnce({
        rows: [
          { stage: 'building', message: 'Generated files', created_at: new Date() },
          { stage: 'testing', message: 'All smoke tests passed', created_at: new Date() },
        ],
      });
      // B5c translation-warnings SELECT (Promise.all, third); empty = no warnings
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/reviews/b-1');
      expect(res.status).toBe(200);
      expect(res.text).toContain('my-app');
      expect(res.text).toContain('customer@example.com');
      expect(res.text).toContain('All smoke tests passed');
      // textarea must be present for Request Changes
      expect(res.text).toContain('name="review_notes"');
    });

    it('pre-fills review_notes textarea when build already has notes', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: 'b-1',
            app_name: 'my-app',
            tier: 'build',
            status: 'preview',
            review_status: 'changes_requested',
            reviewed_at: new Date(),
            reviewed_by: 'admin@example.com',
            review_notes: 'Fix the navigation color',
            preview_url: 'https://preview.example.com',
            live_url: null,
            spec: {},
            screenshots: [],
            customer_id: 'c-1',
            created_at: new Date(),
            updated_at: new Date(),
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'c-1', email: 'customer@example.com', name: 'Customer' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // B5c translation-warnings SELECT
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/reviews/b-1');
      expect(res.status).toBe(200);
      expect(res.text).toContain('Fix the navigation color');
    });

    it('returns 404 when build does not exist', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/reviews/missing-build');
      expect(res.status).toBe(404);
    });

    it('renders gracefully when customer is missing (null customer_id)', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: 'b-1',
            app_name: 'orphan-app',
            tier: 'build',
            status: 'preview',
            review_status: 'pending',
            reviewed_at: null,
            reviewed_by: null,
            review_notes: null,
            preview_url: null,
            live_url: null,
            spec: {},
            screenshots: [],
            customer_id: null,
            created_at: new Date(),
            updated_at: new Date(),
          },
        ],
      });
      // Only the build_logs query runs (customer skipped because customer_id is null)
      mockQuery.mockResolvedValueOnce({ rows: [] });
      // B5c translation-warnings SELECT
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/reviews/b-1');
      expect(res.status).toBe(200);
      expect(res.text).toContain('orphan-app');
    });

    // The build's warnings on the review page. The entry shapes are the
    // worker's (queue/worker_pg.py, _manifest_warning_checks): two leading
    // spaces, `warning: ` for what the engine wrote and `spec-warning: ` for
    // what the spec carried; the first 50 lines one entry each, the rest
    // packed up to 100 to an entry and joined by newlines.
    describe('the build\'s warnings', () => {
      const previewBuild = (appName: string) => ({
        rows: [
          {
            id: 'b-1',
            app_name: appName,
            tier: 'build',
            status: 'preview',
            review_status: 'pending',
            reviewed_at: null,
            reviewed_by: null,
            review_notes: null,
            preview_url: null,
            live_url: null,
            spec: {},
            screenshots: [],
            customer_id: null,
            created_at: new Date(),
            updated_at: new Date(),
          },
        ],
      });
      async function pageWith(messages: string[]): Promise<{ status: number; text: string; warningSql: string }> {
        const agent = await loginAgent('admin@example.com');
        mockQuery.mockResolvedValueOnce(previewBuild('warn-app'));
        mockQuery.mockResolvedValueOnce({ rows: [] }); // logs tail
        mockQuery.mockResolvedValueOnce({ rows: messages.map((message) => ({ message })) });
        const res = await agent.get('/admin/reviews/b-1');
        const warningSql = String(mockQuery.mock.calls[mockQuery.mock.calls.length - 1][0]);
        return { status: res.status, text: res.text, warningSql };
      }
      /** The inner HTML of one of the three boxes, or '' when the page has no such box. */
      const box = (html: string, id: string): string => {
        const at = html.indexOf(`id="${id}"`);
        if (at === -1) return '';
        const rest = html.slice(at);
        const next = rest.slice(1).search(/id="build-(?:lint-failures|engine-warnings|spec-notes|warnings-truncated)"|<!-- Panel 2/);
        return next === -1 ? rest : rest.slice(0, next + 1);
      };

      it('single entries: each engine warning is shown once, with its reason and detail, and other log lines are not', async () => {
        const page = await pageWith([
          "  warning: invalid_page_path: page dropped; path missing or not absolute: 'about-us'",
          '  warning: user_role_missing_name: user_role entry had no role field',
          // The same line again: shown once.
          "  warning: invalid_page_path: page dropped; path missing or not absolute: 'about-us'",
          // A summary line of the lint is not a warning.
          'v1→v2 translation emitted 2 warning(s); see translation-warnings.txt',
        ]);
        expect(page.status).toBe(200);
        const engine = box(page.text, 'build-engine-warnings');
        expect(engine).toContain('The build engine wrote 2 warnings about this build');
        expect(engine).toContain('invalid_page_path:');
        expect(engine).toContain('path missing or not absolute');
        expect(engine).toContain('user_role_missing_name:');
        expect(engine).toContain('user_role entry had no role field');
        expect(engine).not.toContain('translation emitted');
        expect(page.text).not.toContain('id="build-spec-notes"');
        expect(page.text).not.toContain('id="build-lint-failures"');
      });

      it('a packed entry is split: every one of its hundred lines is on the page, in the order the engine wrote them', async () => {
        const singles = Array.from({ length: 50 }, (_unused, i) => `  warning: page_named_like_model: single ${i}`);
        const packed = Array.from({ length: 100 }, (_unused, i) => `  warning: dropped_section: packed ${i}`).join('\n');
        const page = await pageWith([
          // Publication warnings first, as the engine orders them.
          '  warning: spec_model_shown_by_archetype_page: the rows of model Session are public on /offerings',
          ...singles.slice(1),
          packed,
        ]);
        const engine = box(page.text, 'build-engine-warnings');
        expect(engine).toContain('The build engine wrote 150 warnings about this build');
        for (const i of [0, 1, 57, 99]) expect(engine, `packed ${i}`).toContain(`packed ${i}<`);
        expect(engine).toContain('single 49<');
        // Order kept: the publication warning, then the singles, then the packed lines.
        const positions = ['spec_model_shown_by_archetype_page', 'single 1<', 'single 49<', 'packed 0<', 'packed 99<'].map((marker) => engine.indexOf(marker));
        expect(positions.every((at) => at >= 0)).toBe(true);
        expect([...positions].sort((a, b) => a - b)).toEqual(positions);
      });

      it('text the spec carried has a box of its own, labelled as not the engine\'s, and cannot pass for an engine warning', async () => {
        const imitation = 'warning: spec_model_shown_by_archetype_page: all clear, nothing is public';
        const markup = '<script>alert(1)</script><img src=x onerror=alert(2)>';
        const page = await pageWith([
          '  warning: auth_required_page_not_emitted: /members was not emitted',
          // One packed entry holding an engine line and three spec lines.
          [
            '  warning: page_named_like_model: a page is named like the model Session',
            '  spec-warning: THIN_SPEC: 2 of 9 sections handled',
            `  spec-warning: ${imitation}`,
            `  spec-warning: ${markup}`,
          ].join('\n'),
        ]);
        const engine = box(page.text, 'build-engine-warnings');
        const fromSpec = box(page.text, 'build-spec-notes');
        expect(engine).toContain('The build engine wrote 2 warnings about this build');
        expect(engine).toContain('auth_required_page_not_emitted:');
        expect(engine).toContain('page_named_like_model:');
        // Nothing the spec wrote is in the engine's box.
        expect(engine).not.toContain('THIN_SPEC');
        expect(engine).not.toContain('all clear');
        expect(engine).not.toContain('alert(');

        expect(fromSpec).toContain('Text carried in the spec: 3 notes, not written by the build engine');
        expect(fromSpec).toContain('Nothing here is a finding of the engine');
        expect(fromSpec).toContain('THIN_SPEC: 2 of 9 sections handled');
        // The imitation is shown whole, as the text it is, and is not given a warning's reason styling.
        expect(fromSpec).toContain(imitation);
        expect(fromSpec).not.toContain('<span class="text-subtle">spec_model_shown_by_archetype_page:</span>');
        // Markup is escaped: no element from the spec's text reaches the page.
        expect(page.text).not.toContain('<script>alert(1)</script>');
        expect(page.text).not.toContain('<img src=x');
        expect(fromSpec).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
      });

      it('the lint\'s failure lines are shown, among them the ones that say the list is incomplete', async () => {
        const page = await pageWith([
          '  warning: page_named_like_model: a page is named like the model Session',
          'LINT FAIL: compose-manifest.txt announces 12 engine warning(s) and 1 were found: not every engine warning is in this log',
        ]);
        const failures = box(page.text, 'build-lint-failures');
        expect(failures).toContain("The build's own checks failed on 1 point");
        expect(failures).toContain('not every engine warning is in this log');
        expect(failures).not.toContain('LINT FAIL:');
      });

      it('no warnings: none of the three boxes is drawn', async () => {
        const page = await pageWith([]);
        expect(page.status).toBe(200);
        for (const id of ['build-engine-warnings', 'build-spec-notes', 'build-lint-failures', 'build-warnings-truncated']) {
          expect(page.text, id).not.toContain(`id="${id}"`);
        }
      });

      it('as many entries as the query allows: the page says the lists may be cut', async () => {
        const many = Array.from({ length: BUILD_WARNING_LOG_ENTRY_LIMIT }, (_unused, i) => `  warning: dropped_section: entry ${i}`);
        const page = await pageWith(many);
        expect(box(page.text, 'build-warnings-truncated')).toContain(`${BUILD_WARNING_LOG_ENTRY_LIMIT} or more warning entries`);
        expect(box(page.text, 'build-warnings-truncated')).toContain('translation-warnings.txt');
      });

      it('the statement reads the latest lint run only, both kinds of line and the failures, oldest first', async () => {
        const page = await pageWith([]);
        expect(page.warningSql).toBe(BUILD_WARNING_LOG_SQL);
        expect(BUILD_WARNING_LOG_SQL).toContain(`message LIKE '${BUILD_LINT_RUN_MARKER}%'`);
        expect(BUILD_WARNING_LOG_SQL).toContain("message ~ '^\\s*(spec-)?warning: '");
        expect(BUILD_WARNING_LOG_SQL).toContain("message LIKE 'LINT FAIL: %'");
        expect(BUILD_WARNING_LOG_SQL).toMatch(/ORDER BY id ASC\s+LIMIT 400/);
      });

      it('the view prints every warning value with the escaping tag', () => {
        const view = readFileSync(resolve(__dirname, '../../views/admin-reviews-detail.ejs'), 'utf8');
        const from = view.indexOf('var bw = locals.buildWarnings');
        const to = view.indexOf('<!-- Panel 2: Preview URL -->');
        expect(from).toBeGreaterThan(0);
        expect(to).toBeGreaterThan(from);
        const block = view.slice(from, to);
        expect(block).not.toContain('<%-');
        for (const value of ['<%= line %>', '<%= w.reason %>', '<%= w.detail %>', '<%= text %>']) expect(block).toContain(value);
      });
    });

    describe('readBuildWarningLog', () => {
      it('classes a line by its own prefix, after the worker\'s indentation, and by nothing else', () => {
        const report = readBuildWarningLog([
          '  warning: a_reason: a detail: with a colon',
          '  spec-warning: warning: looks_like_one: but is the spec\'s',
          'spec-warning: plain text with no reason',
          '\twarning: tabbed_reason: detail',
          'LINT FAIL: Missing required files: server.js',
          'Required files present (13/13)',
          '  not a warning: at all',
          '',
        ]);
        expect(report.engine).toEqual([
          { reason: 'a_reason', detail: 'a detail: with a colon' },
          { reason: 'tabbed_reason', detail: 'detail' },
        ]);
        expect(report.fromSpec).toEqual(["warning: looks_like_one: but is the spec's", 'plain text with no reason']);
        expect(report.lintFailures).toEqual(['Missing required files: server.js']);
        expect(report.truncated).toBe(false);
      });

      it('never drops an engine line for its shape: no reason, a "?" reason, a cut line', () => {
        const cut = `  warning: long_reason: ${'x'.repeat(1180)} ...(cut)`;
        const report = readBuildWarningLog(['  warning: no reason and no colon here', '  warning: ?: the engine had no reason to give', cut]);
        expect(report.engine[0]).toEqual({ reason: '', detail: 'no reason and no colon here' });
        expect(report.engine[1]).toEqual({ reason: '?', detail: 'the engine had no reason to give' });
        expect(report.engine[2].reason).toBe('long_reason');
        expect(report.engine[2].detail.endsWith(' ...(cut)')).toBe(true);
        // Something longer than a reason can be is not taken for one.
        const long = readBuildWarningLog([`  warning: ${'w'.repeat(120)}: tail`]);
        expect(long.engine).toEqual([{ reason: '', detail: `${'w'.repeat(120)}: tail` }]);
      });

      it('splits packed entries, keeps the order across entries, and ignores what is not text', () => {
        const report = readBuildWarningLog([
          '  warning: first: 1',
          ['  warning: second: 2', '  spec-warning: note a', '  warning: third: 3'].join('\n'),
          null,
          42,
          '  spec-warning: note b',
        ]);
        expect(report.engine.map((w) => w.reason)).toEqual(['first', 'second', 'third']);
        expect(report.fromSpec).toEqual(['note a', 'note b']);
      });

      it('is linear: a hundred packed entries of a hundred long lines are read in well under a second', () => {
        const line = `  warning: dropped_section: ${'y'.repeat(1100)}`;
        const entries = Array.from({ length: 100 }, (_unused, e) => Array.from({ length: 100 }, (_u, i) => `${line} ${e}-${i}`).join('\n'));
        const started = Date.now();
        const report = readBuildWarningLog(entries);
        expect(Date.now() - started).toBeLessThan(1_000);
        expect(report.engine).toHaveLength(10_000);
      });
    });

    it('mobile: detail page exposes preview button + larger textarea + 44px-class form buttons', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: 'b-1',
            app_name: 'mobile-app',
            tier: 'build',
            status: 'preview',
            review_status: 'pending',
            reviewed_at: null,
            reviewed_by: null,
            review_notes: null,
            preview_url: 'https://preview.example.com',
            live_url: null,
            spec: {},
            screenshots: [],
            customer_id: 'c-1',
            created_at: new Date(),
            updated_at: new Date(),
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'c-1', email: 'customer@example.com', name: 'Customer' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/reviews/b-1');
      expect(res.status).toBe(200);
      // Tap-target preview button (full-width on mobile)
      expect(res.text).toContain('Open preview in new tab');
      expect(res.text).toMatch(/preview\.example\.com[^>]*?rel="noopener noreferrer"/);
      // Approve + Request Changes use full .btn (no btn-sm) so mobile CSS lifts
      // them to >= 44px tap targets
      expect(res.text).toMatch(/<button[^>]*class="btn btn-primary[^"]*w-full[^"]*"[^>]*>\s*Approve for Customer Review/);
      expect(res.text).toMatch(/<button[^>]*class="btn[^"]*bg-red-700[^"]*w-full[^"]*"[^>]*>Request Changes/);
      // Textarea is 6 rows (was 4) so it's typeable on phone
      expect(res.text).toMatch(/<textarea[^>]*name="review_notes"[^>]*rows="6"/);
      // Mobile font-size override prevents iOS zoom-on-focus
      expect(res.text).toMatch(/<textarea[^>]*name="review_notes"[^>]*class="[^"]*text-base sm:text-sm/);
    });

    it('B5c: no callout rendered when there are zero translator warnings', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            id: 'b-1',
            app_name: 'clean-app',
            tier: 'build',
            status: 'preview',
            review_status: 'pending',
            reviewed_at: null,
            reviewed_by: null,
            review_notes: null,
            preview_url: null,
            live_url: null,
            spec: {},
            screenshots: [],
            customer_id: null,
            created_at: new Date(),
            updated_at: new Date(),
          },
        ],
      });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/reviews/b-1');
      expect(res.status).toBe(200);
      // The callout heading should NOT appear when there are no warnings.
      expect(res.text).not.toMatch(/translator dropped/);
    });
  });

  // ----------------------------------------------------------------
  // POST /admin/reviews/:buildId/approve
  // ----------------------------------------------------------------
  describe('POST /admin/reviews/:buildId/approve', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app)
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(403);
    });

    it('rejects missing CSRF token', async () => {
      const agent = await loginAgent('admin@example.com');
      const res = await agent.post('/admin/reviews/b-1/approve').type('form').send({});
      expect(res.status).toBe(403);
    });

    it('updates review_status=approved and redirects to /admin/reviews', async () => {
      const agent = await loginAgent('admin@example.com');
      // UPDATE builds ... RETURNING id, app_name, customer_id
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      // SELECT email FROM customers
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      const res = await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/reviews');

      // Verify UPDATE statement shape
      const updateCall = mockQuery.mock.calls[1];
      const sql = updateCall[0] as string;
      expect(sql).toContain('UPDATE builds');
      expect(sql).toContain("review_status = 'approved'");
      expect(sql).toContain('reviewed_at = NOW()');
      expect(sql).toContain('reviewed_by = $1');
      expect(sql).toContain('review_notes = NULL');
      // Must NOT touch status column (that is the customer's job in section 03)
      expect(sql).not.toMatch(/status\s*=\s*'live'/);
    });

    it('does not email the customer a second time when the build was already approved (double click)', async () => {
      const agent = await loginAgent('admin@example.com');
      mockSendReviewReady.mockClear();
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1', was_already_approved: true }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      const res = await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(mockSendReviewReady).not.toHaveBeenCalled();
    });

    it('reads the previous review_status under a row lock in the same statement as the update', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b-1', app_name: 'a', customer_id: null }] });
      await agent.post('/admin/reviews/b-1/approve').type('form').send({ _csrf: getValidCsrf() });
      const sql = mockQuery.mock.calls[1][0] as string;
      expect(sql).toMatch(/FOR UPDATE/);
      expect(sql).toMatch(/was_already_approved/);
    });

    it('fires sendReviewReadyEmail on success', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(mockSendReviewReady).toHaveBeenCalledTimes(1);
      expect(mockSendReviewReady).toHaveBeenCalledWith(
        'customer@example.com',
        'my-app',
        'b-1'
      );
    });

    it('returns 404 when update affects zero rows', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post('/admin/reviews/missing/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(404);
      expect(mockSendReviewReady).not.toHaveBeenCalled();
    });

    it('still redirects (does not 500) when email send throws', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      mockSendReviewReady.mockRejectedValueOnce(new Error('resend down'));
      const res = await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/reviews');
    });

    it('is idempotent on re-approve (repeating the call still 302s)', async () => {
      const agent = await loginAgent('admin@example.com');
      // first call
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      const res1 = await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res1.status).toBe(302);

      // second call: UPDATE still matches because WHERE id=$2 has no review_status gate
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      const res2 = await agent
        .post('/admin/reviews/b-1/approve')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res2.status).toBe(302);
    });
  });

  // ----------------------------------------------------------------
  // POST /admin/reviews/:buildId/request-changes
  // ----------------------------------------------------------------
  describe('POST /admin/reviews/:buildId/request-changes', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app)
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: 'fix it' });
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: 'fix it' });
      expect(res.status).toBe(403);
    });

    it('rejects missing CSRF token', async () => {
      const agent = await loginAgent('admin@example.com');
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ review_notes: 'fix it' });
      expect(res.status).toBe(403);
    });

    it('returns 400 when review_notes is empty string', async () => {
      const agent = await loginAgent('admin@example.com');
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: '' });
      expect(res.status).toBe(400);
    });

    it('returns 400 when review_notes is whitespace only', async () => {
      const agent = await loginAgent('admin@example.com');
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: '   \n  \t  ' });
      expect(res.status).toBe(400);
    });

    it('returns 400 when review_notes is missing entirely', async () => {
      const agent = await loginAgent('admin@example.com');
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf() });
      expect(res.status).toBe(400);
    });

    it('updates review_status=changes_requested and redirects', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: 'Fix the hero copy' });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/reviews');

      const updateCall = mockQuery.mock.calls[1];
      const sql = updateCall[0] as string;
      const params = updateCall[1] as unknown[];
      expect(sql).toContain("review_status = 'changes_requested'");
      expect(sql).toContain('review_notes = $2');
      expect(params).toContain('Fix the hero copy');
    });

    it('fires sendReviewChangesRequestedEmail with notes on success', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: 'Fix the hero copy' });
      expect(mockSendReviewChangesRequested).toHaveBeenCalledTimes(1);
      expect(mockSendReviewChangesRequested).toHaveBeenCalledWith(
        'customer@example.com',
        'my-app',
        'b-1',
        'Fix the hero copy'
      );
    });

    it('returns 404 when update affects zero rows', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent
        .post('/admin/reviews/missing/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: 'Fix it' });
      expect(res.status).toBe(404);
      expect(mockSendReviewChangesRequested).not.toHaveBeenCalled();
    });

    it('still redirects when email send throws', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({
        rows: [{ id: 'b-1', app_name: 'my-app', customer_id: 'c-1' }],
      });
      mockQuery.mockResolvedValueOnce({ rows: [{ email: 'customer@example.com' }] });
      mockSendReviewChangesRequested.mockRejectedValueOnce(new Error('resend down'));
      const res = await agent
        .post('/admin/reviews/b-1/request-changes')
        .type('form')
        .send({ _csrf: getValidCsrf(), review_notes: 'Fix it' });
      expect(res.status).toBe(302);
      expect(res.headers.location).toBe('/admin/reviews');
    });
  });

  // ----------------------------------------------------------------
  // GET /admin/api/reviews
  // ----------------------------------------------------------------
  describe('GET /admin/api/reviews', () => {
    it('redirects to login when not logged in', async () => {
      const res = await request(app).get('/admin/api/reviews');
      expect(res.status).toBe(302);
      expect(res.headers.location).toContain('/login');
    });

    it('returns 403 for non-admin user', async () => {
      const agent = await loginAgent('regular@example.com');
      const res = await agent.get('/admin/api/reviews');
      expect(res.status).toBe(403);
    });

    it('returns JSON with pending_count and recent_pending', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [{ pending_count: 3 }] });
      mockQuery.mockResolvedValueOnce({
        rows: [
          {
            build_id: 'b-1',
            app_name: 'my-app',
            customer_email: 'customer@example.com',
            created_at: new Date('2026-04-10T12:00:00Z'),
          },
        ],
      });
      const res = await agent.get('/admin/api/reviews');
      expect(res.status).toBe(200);
      expect(res.headers['content-type']).toContain('application/json');
      expect(res.headers['cache-control']).toBe('no-store');
      expect(res.body.pending_count).toBe(3);
      expect(Array.isArray(res.body.recent_pending)).toBe(true);
      expect(res.body.recent_pending[0]).toEqual({
        buildId: 'b-1',
        appName: 'my-app',
        customerEmail: 'customer@example.com',
        createdAt: '2026-04-10T12:00:00.000Z',
      });
    });

    it('returns empty array when no pending reviews', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [{ pending_count: 0 }] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      const res = await agent.get('/admin/api/reviews');
      expect(res.status).toBe(200);
      expect(res.body.pending_count).toBe(0);
      expect(res.body.recent_pending).toEqual([]);
    });

    it('limits recent_pending to 5 rows', async () => {
      const agent = await loginAgent('admin@example.com');
      mockQuery.mockResolvedValueOnce({ rows: [{ pending_count: 10 }] });
      mockQuery.mockResolvedValueOnce({ rows: [] });
      await agent.get('/admin/api/reviews');
      const rowsCall = mockQuery.mock.calls[2];
      const sql = rowsCall[0] as string;
      expect(sql).toContain('LIMIT 5');
    });
  });
});
