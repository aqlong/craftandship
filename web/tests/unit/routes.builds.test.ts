import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';
import fs from 'fs';
import path from 'path';
import app from '../../src/app';

// State shared with the pool double below (vi.mock factories are hoisted, so
// anything they close over has to be hoisted too).
const dbDouble = vi.hoisted(() => ({
  // What the build-start budget query answers. Tests overwrite the fields.
  budget: { active: 0, starts_today: 0, build_retries: 0 },
  // Every statement sent through a transaction client, in order.
  clientCalls: [] as Array<{ sql: string; params?: unknown[] }>,
}));

// Mock DB pool
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  // The client pool.connect() hands out. Transaction control and the advisory
  // lock are answered here, the budget query from dbDouble.budget, and every
  // other statement goes to the same queue as pool.query, so a test describes
  // the data statements in order without caring which connection ran them.
  const client = {
    query: (sql: string, params?: unknown[]) => {
      dbDouble.clientCalls.push({ sql, params });
      if (/^(BEGIN|COMMIT|ROLLBACK)$/.test(sql) || sql.includes('pg_advisory_xact_lock')) {
        return Promise.resolve({ rows: [], rowCount: 0 });
      }
      if (sql.includes('AS build_retries')) {
        return Promise.resolve({ rows: [{ ...dbDouble.budget }], rowCount: 1 });
      }
      // The ledger of accepted starts: recorded in clientCalls, always succeeds.
      if (sql.includes('INSERT INTO build_start_events')) {
        return Promise.resolve({ rows: [], rowCount: 1 });
      }
      return mockQuery(sql, params);
    },
    release: () => undefined,
  };
  return {
    default: {
      query: mockQuery,
      connect: async () => client,
      on: vi.fn(),
    },
  };
});

function resetBuildBudget(overrides: Partial<typeof dbDouble.budget> = {}): void {
  dbDouble.budget = { active: 0, starts_today: 0, build_retries: 0, ...overrides };
  dbDouble.clientCalls.length = 0;
}

// The budget and the transaction log are module state; a test that fills the
// budget (to prove the 429) must not leave it full for whichever test runs next.
beforeEach(() => resetBuildBudget());

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
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello! Describe your app.'),
}));

import pool from '../../src/db/index';
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

// Helper: create an authenticated agent (sets session via login)
// Because supertest doesn't share cookies between requests automatically,
// we use supertest agent which persists cookies.
import supertest from 'supertest';
import { sendAppLiveEmail } from '../../src/services/email';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

function getValidCsrf(): string {
  return generateCsrfToken({} as Request);
}

async function makeAuthenticatedAgent() {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  // Prime mock: login query returns customer
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'customer-abc', email: 'me@example.com', name: 'Me', password: hashed, email_verified: true }],
  });
  const csrf = getValidCsrf();
  await agent
    .post('/login')
    .type('form')
    .send({ _csrf: csrf, email: 'me@example.com', password: 'password123' });
  return agent;
}

describe('GET /dashboard', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).get('/dashboard');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
    expect(res.headers.location).toContain('%2Fdashboard');
  });

  it('returns 200 and renders builds list when authenticated', async () => {
    const agent = await makeAuthenticatedAgent();
    // Dashboard query: return builds list (now includes health columns from LEFT JOIN)
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-1',
          tier: 'build',
          status: 'live',
          app_name: 'My App',
          live_url: 'https://app.example.com',
          error_message: null,
          created_at: new Date(),
          updated_at: new Date(),
          health_status: 'healthy',
          health_last_checked: new Date().toISOString(),
          health_latency_ms: 120,
          health_consecutive_failures: 0,
        },
      ],
    });

    const res = await agent.get('/dashboard');
    expect(res.status).toBe(200);
    expect(res.text).toContain('My Websites');
  });

  it('shows health badge for live apps with monitoring data', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-1',
          tier: 'build',
          status: 'live',
          app_name: 'Healthy App',
          live_url: 'https://app.example.com',
          error_message: null,
          created_at: new Date(),
          updated_at: new Date(),
          gallery_opted_in: false,
          health_status: 'healthy',
          health_last_checked: new Date().toISOString(),
          health_latency_ms: 250,
          health_consecutive_failures: 0,
        },
      ],
    });

    const res = await agent.get('/dashboard');
    expect(res.status).toBe(200);
    // Harbor v2 (2026-04-27): the lit lamp dot replaces the cyan brand badge.
    // "Lit and healthy" copy + the .lamp-dot class are the visible signals.
    expect(res.text).toContain('Lit and healthy');
    expect(res.text).toContain('lamp-dot');
  });

  it('shows red health badge when app has consecutive failures', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-1',
          tier: 'build',
          status: 'live',
          app_name: 'Down App',
          live_url: 'https://app.example.com',
          error_message: null,
          created_at: new Date(),
          updated_at: new Date(),
          gallery_opted_in: false,
          health_status: 'down',
          health_last_checked: new Date().toISOString(),
          health_latency_ms: null,
          health_consecutive_failures: 3,
        },
      ],
    });

    const res = await agent.get('/dashboard');
    expect(res.status).toBe(200);
    // Harbor v2: "Needs attention" copy + the build-row__health--alert class
    // replace the old #C47A7A red dot.
    expect(res.text).toContain('Needs attention');
    expect(res.text).toContain('build-row__health--alert');
  });

  it('shows gray health indicator for live apps without monitoring data', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-1',
          tier: 'build',
          status: 'live',
          app_name: 'Unmonitored App',
          live_url: 'https://app.example.com',
          error_message: null,
          created_at: new Date(),
          updated_at: new Date(),
          gallery_opted_in: false,
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
        },
      ],
    });

    const res = await agent.get('/dashboard');
    expect(res.status).toBe(200);
    // Harbor v2: when there's no monitoring data, the dashboard does not
    // render a health indicator at all (instead of a gray dot). The build
    // still appears in the list.
    expect(res.text).toContain('Unmonitored App');
    expect(res.text).not.toContain('Lit and healthy');
    expect(res.text).not.toContain('Needs attention');
  });
});

describe('GET /builds/:buildId', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 200 for a valid session and owned build', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-99',
          customer_id: 'customer-abc',
          tier: 'grow',
          status: 'building',
          app_name: 'Owned App',
          live_url: null,
          error_message: null,
          spec: null,
          spec_path: null,
          stripe_session_id: null,
          stripe_payment_intent: null,
          queue_job_id: null,
          created_at: new Date(),
          updated_at: new Date(),
        },
      ],
    });

    const res = await agent.get('/builds/build-99');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Owned App');
  });

  it('returns 404 when build belongs to a different customer', async () => {
    const agent = await makeAuthenticatedAgent();
    // Simulate DB returning empty rows (wrong customer_id filtered out)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/builds/build-someone-elses');
    expect(res.status).toBe(404);
  });
});

describe('GET /builds/:buildId - review states', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  // Helper: build a canonical preview build row. Callers can override fields
  // (review_status, reviewed_at, review_notes) to exercise the three branches.
  function previewBuildRow(overrides: Partial<Record<string, unknown>> = {}) {
    return {
      id: 'build-review-1',
      customer_id: 'customer-abc',
      tier: 'build',
      status: 'preview',
      app_name: 'Review Gate App',
      live_url: null,
      preview_url: 'https://preview.example.com',
      error_message: null,
      spec: { admin_email: 'admin@example.com' },
      spec_path: null,
      stripe_session_id: null,
      stripe_payment_intent: null,
      queue_job_id: null,
      parent_build_id: null,
      is_revision: false,
      review_status: null,
      reviewed_at: null,
      review_notes: null,
      created_at: new Date(),
      updated_at: new Date(),
      ...overrides,
    };
  }

  // Helper: prime the two queries the status page makes for a preview build
  // after loading it. First the lineage CTE, so countRevisionsInLineage
  // returns "no revisions used yet on a Build-tier app" (plenty of remaining
  // revisions); then the question "would the source download be served to
  // this owner", which decides whether the page links to it.
  function primeLineageMock(sourceDownloadAllowed = false) {
    mockQuery.mockResolvedValueOnce({
      rows: [{ root_tier: 'build', revision_count: '0' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [{ allowed: sourceDownloadAllowed }] });
  }

  it('renders the in-review state when review_status is null', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [previewBuildRow({ review_status: null })] });
    primeLineageMock();

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Your website is in review.');
    // The page names no reviewer: who reviews is not something the code decides.
    // (The site footer names the founder, so match the claim, not the title.)
    expect(res.text).not.toMatch(/review(?:ed)? (?:with|by) (?:Ocean|a Senior Web Craftsman)/i);
    expect(res.text).not.toMatch(/human review/i);
    // The preview address is offered while the review runs (it was emailed on arrival).
    expect(res.text).toContain('https://preview.example.com');
    // Approve form must NOT render until the review is approved.
    expect(res.text).not.toContain('Approve and go live');
    // Legacy "Ready for Review" banner must not leak through.
    expect(res.text).not.toContain('Your App is Ready for Review');
    // Nor is the source offered before the review.
    expect(res.text).not.toContain('/download-source');
  });

  it('renders the in-review state when review_status is pending', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [previewBuildRow({ review_status: 'pending' })] });
    primeLineageMock();

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Your website is in review.');
    expect(res.text).not.toContain('Approve and go live');
    expect(res.text).not.toContain('The review left notes for your website');
  });

  it('renders the review-notes panel with the notes when changes_requested', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        previewBuildRow({
          review_status: 'changes_requested',
          review_notes: 'The contact form is missing a subject field.',
        }),
      ],
    });
    primeLineageMock();

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('The review left notes for your website');
    expect(res.text).toContain('The contact form is missing a subject field.');
    expect(res.text).not.toContain('Notes from Ocean');
    // Approve button must still be hidden in changes_requested state.
    expect(res.text).not.toContain('Approve and go live');
    expect(res.text).not.toContain('Your website is in review.');
    expect(res.text).not.toContain('/download-source');
  });

  it('renders Approve button and the reviewed line when review_status is approved', async () => {
    const agent = await makeAuthenticatedAgent();
    const reviewedAt = new Date('2026-04-10T15:30:00Z');
    mockQuery.mockResolvedValueOnce({
      rows: [
        previewBuildRow({
          review_status: 'approved',
          reviewed_at: reviewedAt,
          review_notes: null,
        }),
      ],
    });
    primeLineageMock(true);

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    // Approve form must now be visible.
    expect(res.text).toContain('Approve and go live');
    // The review is stated without naming a reviewer (reviewed_by is not sent to
    // the page, and the code does not require that a particular person approved).
    expect(res.text).toContain('Your website has been reviewed and approved.');
    expect(res.text).not.toContain('Reviewed by Ocean</span>');
    expect(res.text).not.toMatch(/Senior Web Craftsman reviewed|Reviewed by (?:me|Ocean|a Senior Web Craftsman)/i);
    // Should not render the waiting or changes_requested panels.
    expect(res.text).not.toContain('Your website is in review.');
    expect(res.text).not.toContain('The review left notes for your website');
    // The owner may publish this build, so the source is offered too.
    expect(res.text).toContain('/builds/build-review-1/download-source');
    // That was asked of the database with the build id and the session's customer.
    const asked = mockQuery.mock.calls[mockQuery.mock.calls.length - 1];
    expect(String(asked[0])).toContain("COALESCE(b.status = 'live' OR (b.status = 'preview'");
    expect(asked[1]).toEqual(['build-review-1', 'customer-abc']);
  });

  it('an approved Harbor preview without a subscription: Approve is offered (it leads to payment), the source is not', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [previewBuildRow({ review_status: 'approved', reviewed_at: new Date('2026-04-10T15:30:00Z') })],
    });
    primeLineageMock(false);

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Approve and go live');
    expect(res.text).not.toContain('/download-source');
  });

  it('renders the reviewed badge on live builds when reviewed_at is set', async () => {
    const agent = await makeAuthenticatedAgent();
    const reviewedAt = new Date('2026-04-10T15:30:00Z');
    mockQuery.mockResolvedValueOnce({
      rows: [
        previewBuildRow({
          status: 'live',
          live_url: 'https://live.example.com',
          preview_url: 'https://preview.example.com',
          review_status: 'approved',
          reviewed_at: reviewedAt,
        }),
      ],
    });
    // Live builds skip the lineage CTE.

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Your website is live.');
    expect(res.text).toMatch(/class="status-pill[^"]*"\s*>\s*Reviewed</);
    expect(res.text).toContain('Reviewed and approved on');
    expect(res.text).not.toContain('Reviewed and approved by Ocean');
    expect(res.text).toContain('https://live.example.com');
  });

  it('does not render the reviewed badge on legacy live builds with null reviewed_at', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        previewBuildRow({
          status: 'live',
          live_url: 'https://legacy-live.example.com',
          preview_url: 'https://legacy-preview.example.com',
          review_status: null,
          reviewed_at: null,
        }),
      ],
    });

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Your website is live.');
    expect(res.text).toContain('https://legacy-live.example.com');
    // Legacy builds that never went through the review gate must not show
    // the badge (no backfill, no misleading signal). Match the badge
    // structure specifically, not the word, which the page uses elsewhere.
    expect(res.text).not.toMatch(/class="status-pill[^"]*"\s*>\s*Reviewed</);
  });

  it('HTML-escapes review_notes to prevent XSS', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        previewBuildRow({
          review_status: 'changes_requested',
          review_notes: "<script>alert('xss')</script>",
        }),
      ],
    });
    primeLineageMock();

    const res = await agent.get('/builds/build-review-1');
    expect(res.status).toBe(200);
    // The raw <script> tag must NOT appear in the rendered HTML.
    expect(res.text).not.toContain("<script>alert('xss')</script>");
    // EJS <%= %> escapes to &lt;script&gt; / &#39; (apostrophe).
    expect(res.text).toContain('&lt;script&gt;');
  });
});

describe('GET /api/builds/:buildId', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns JSON with status fields', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'building',
          live_url: null,
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-99');
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({
      status: 'building',
      live_url: null,
      error: null,
      queue_position: null,
      progress_message: 'Generating code',
    });
  });

  it('includes queue_position for a queued build', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'queued',
          live_url: null,
          error_message: null,
          queue_job_id: 'job-xyz',
          tier: 'scale',
          queue_position: '2', // 2 builds ahead, so position = 2+1 = 3
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-queued');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('queued');
    expect(res.body.queue_position).toBe(3);
  });

  it('returns 404 for a build not found', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/api/builds/no-such-build');
    expect(res.status).toBe(404);
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).get('/api/builds/build-99');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('includes health data for live builds with monitoring', async () => {
    const agent = await makeAuthenticatedAgent();
    const lastChecked = new Date().toISOString();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'live',
          live_url: 'https://app.example.com',
          preview_url: null,
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: 'healthy',
          health_last_checked: lastChecked,
          health_latency_ms: 150,
          health_consecutive_failures: 0,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-live');
    expect(res.status).toBe(200);
    expect(res.body.health).toEqual({
      status: 'healthy',
      last_checked: lastChecked,
      latency_ms: 150,
      consecutive_failures: 0,
    });
  });

  it('returns null health when no monitoring data exists', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'building',
          live_url: null,
          preview_url: null,
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-no-monitor');
    expect(res.status).toBe(200);
    expect(res.body.health).toBeNull();
  });

  it('includes preview_url in response for preview builds', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'preview',
          live_url: null,
          preview_url: 'https://preview.up.railway.app',
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-preview');
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({
      status: 'preview',
      preview_url: 'https://preview.up.railway.app',
      progress_message: 'Ready for review',
    });
  });
});

describe('POST /builds/:buildId/approve', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('promotes a preview build to live and redirects', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-approve', app_name: 'TestApp', preview_url: 'https://preview.up.railway.app', customer_id: 'customer-abc' }],
    });

    const res = await agent
      .post('/builds/build-approve/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-approve');
  });

  it('returns 404 when build is not in preview status', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Mock 1: UPDATE returns 0 rows (atomic gate failed)
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // Mock 2: disambiguation SELECT returns 0 rows (build missing → 404)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent
      .post('/builds/build-not-preview/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(404);
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app)
      .post('/builds/build-1/approve')
      .type('form')
      .send({ _csrf: 'fake' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('returns 404 when caller does not own the build (disambiguation does not leak existence)', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // UPDATE returns 0 rows
    mockQuery.mockResolvedValueOnce({ rows: [] });
    // Disambiguation SELECT: build exists but belongs to someone else
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-stranger',
          customer_id: 'customer-someone-else',
          status: 'preview',
          review_status: 'approved',
        },
      ],
    });

    const res = await agent
      .post('/builds/build-stranger/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(404);
  });

  it('returns 409 when build exists, is owned, but status is not preview', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-live-already',
          customer_id: 'customer-abc',
          status: 'live',
          review_status: 'approved',
        },
      ],
    });

    const res = await agent
      .post('/builds/build-live-already/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(409);
  });

  it('blocks with 403 and distinct message when review_status is changes_requested', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-changes',
          customer_id: 'customer-abc',
          status: 'preview',
          review_status: 'changes_requested',
        },
      ],
    });

    const res = await agent
      .post('/builds/build-changes/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(403);
    expect(res.text).toContain('left notes');
    expect(res.text).toContain('revision');
    expect(res.text).not.toMatch(/Our Senior Web Craftsman/);
  });

  it('blocks with 403 when review_status is pending', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-pending',
          customer_id: 'customer-abc',
          status: 'preview',
          review_status: 'pending',
        },
      ],
    });

    const res = await agent
      .post('/builds/build-pending/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(403);
    expect(res.text).toContain('waiting for its review');
    expect(res.text).not.toMatch(/waiting for Senior Web Craftsman/);
  });

  it('blocks with 403 when review_status is null (legacy / not yet reviewed)', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-null-review',
          customer_id: 'customer-abc',
          status: 'preview',
          review_status: null,
        },
      ],
    });

    const res = await agent
      .post('/builds/build-null-review/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(403);
    expect(res.text).toContain('waiting for its review');
    expect(res.text).not.toMatch(/waiting for Senior Web Craftsman/);
  });

  it('admin override bypasses review_status and ownership gates', async () => {
    // Build an admin-authenticated agent: the login query must return the
    // ADMIN_EMAIL so that isAdmin === true inside the handler. ADMIN_EMAIL is
    // set explicitly: with it unset the fallback address below is not the
    // admin and this test would silently exercise the non-admin path.
    process.env.ADMIN_EMAIL = process.env.ADMIN_EMAIL || 'founder@example.com';
    const agent = supertest.agent(app);
    const hashed = await bcrypt.hash('password123', 1);
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'admin-1',
          email: process.env.ADMIN_EMAIL || 'founder@example.com',
          name: 'Admin',
          password: hashed,
          email_verified: true,
        },
      ],
    });
    const loginCsrf = getValidCsrf();
    await agent
      .post('/login')
      .type('form')
      .send({
        _csrf: loginCsrf,
        email: process.env.ADMIN_EMAIL || 'founder@example.com',
        password: 'password123',
      });

    // Admin UPDATE succeeds in one query (no review_status predicate), so
    // only a single UPDATE mock is queued; no disambiguation SELECT.
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-admin-override',
          app_name: 'AdminForced',
          preview_url: 'https://preview.up.railway.app',
          customer_id: 'customer-other',
        },
      ],
    });

    const csrf = getValidCsrf();
    const res = await agent
      .post('/builds/build-admin-override/approve')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-admin-override');
  });
});

describe('GET /api/builds/:buildId: review fields', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('response includes review_status field when pending', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'preview',
          live_url: null,
          preview_url: 'https://preview.up.railway.app',
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
          review_status: 'pending',
          reviewed_at: null,
          review_notes: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-pending');
    expect(res.status).toBe(200);
    expect(res.body.review_status).toBe('pending');
    expect(res.body.reviewed_at).toBeNull();
    expect(res.body.review_notes).toBeNull();
  });

  it('response serializes reviewed_at as ISO string when set', async () => {
    const agent = await makeAuthenticatedAgent();
    const reviewedAt = new Date('2026-04-10T12:34:56.000Z');
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'live',
          live_url: 'https://app.example.com',
          preview_url: 'https://preview.up.railway.app',
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
          review_status: 'approved',
          reviewed_at: reviewedAt,
          review_notes: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-approved');
    expect(res.status).toBe(200);
    expect(typeof res.body.reviewed_at).toBe('string');
    expect(res.body.reviewed_at).toBe('2026-04-10T12:34:56.000Z');
    expect(res.body.review_status).toBe('approved');
    expect(res.body.review_notes).toBeNull();
  });

  it('legacy live build with NULL reviewed_at serializes as null (no throw)', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'live',
          live_url: 'https://legacy.example.com',
          preview_url: null,
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
          review_status: null,
          reviewed_at: null,
          review_notes: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-legacy');
    expect(res.status).toBe(200);
    expect(res.body.reviewed_at).toBeNull();
    expect(res.body.review_status).toBeNull();
  });

  it('response includes review_notes when changes_requested', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'preview',
          live_url: null,
          preview_url: 'https://preview.up.railway.app',
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
          review_status: 'changes_requested',
          reviewed_at: new Date('2026-04-10T00:00:00.000Z'),
          review_notes: 'Fix navigation',
        },
      ],
    });

    const res = await agent.get('/api/builds/build-changes');
    expect(res.status).toBe(200);
    expect(res.body.review_status).toBe('changes_requested');
    expect(res.body.review_notes).toBe('Fix navigation');
  });

  it('response does NOT include reviewed_by field (PII protection)', async () => {
    const agent = await makeAuthenticatedAgent();
    // Even if the DB row somehow carried reviewed_by, the API must not
    // echo it. The handler's SELECT does not request reviewed_by, so the
    // row key would not exist: this assertion guards against a future
    // regression that adds it to the SELECT.
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          status: 'live',
          live_url: 'https://app.example.com',
          preview_url: 'https://preview.up.railway.app',
          error_message: null,
          queue_job_id: null,
          tier: 'build',
          queue_position: '0',
          health_status: null,
          health_last_checked: null,
          health_latency_ms: null,
          health_consecutive_failures: null,
          review_status: 'approved',
          reviewed_at: new Date('2026-04-10T00:00:00.000Z'),
          review_notes: null,
        },
      ],
    });

    const res = await agent.get('/api/builds/build-privacy');
    expect(res.status).toBe(200);
    expect(res.body.reviewed_by).toBeUndefined();
    expect(Object.keys(res.body)).not.toContain('reviewed_by');
  });
});

describe('GET /api/builds/:buildId/logs', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns logs for an authenticated user\'s build', async () => {
    const agent = await makeAuthenticatedAgent();
    // Ownership check
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-99' }] });
    // Logs query
    mockQuery.mockResolvedValueOnce({
      rows: [
        { id: 1, stage: 'building', message: 'Generating code with Claude', created_at: new Date() },
        { id: 2, stage: 'building', message: 'Code generation complete', created_at: new Date() },
      ],
    });

    const res = await agent.get('/api/builds/build-99/logs');
    expect(res.status).toBe(200);
    expect(res.body.logs).toHaveLength(2);
    expect(res.body.logs[0].stage).toBe('building');
    expect(res.body.logs[1].message).toBe('Code generation complete');
  });

  it('returns only logs after the given cursor', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-99' }] });
    mockQuery.mockResolvedValueOnce({
      rows: [
        { id: 5, stage: 'testing', message: 'All tests passed', created_at: new Date() },
      ],
    });

    const res = await agent.get('/api/builds/build-99/logs?after=3');
    expect(res.status).toBe(200);
    expect(res.body.logs).toHaveLength(1);
    expect(res.body.logs[0].id).toBe(5);
  });

  it('returns 404 for another user\'s build', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/api/builds/other-user-build/logs');
    expect(res.status).toBe(404);
  });

  it('returns empty array when no logs exist', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-99' }] });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.get('/api/builds/build-99/logs');
    expect(res.status).toBe(200);
    expect(res.body.logs).toHaveLength(0);
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).get('/api/builds/build-99/logs');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('excludes api_costs stage from non-admin logs query', async () => {
    const agent = await makeAuthenticatedAgent();
    // Ownership check
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-99' }] });
    // Logs query (should exclude api_costs)
    mockQuery.mockResolvedValueOnce({
      rows: [
        { id: 1, stage: 'building', message: 'Generating code', created_at: new Date() },
      ],
    });

    const res = await agent.get('/api/builds/build-99/logs');
    expect(res.status).toBe(200);

    // Verify the SQL query includes the api_costs filter
    const logsCall = mockQuery.mock.calls[mockQuery.mock.calls.length - 1];
    expect(logsCall[0]).toContain("stage != 'api_costs'");
  });
});

describe('POST /builds/:buildId/rebuild', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('creates a new build from an existing live build and redirects', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Original build query
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-live', tier: 'grow', spec: { app_name: 'My App', description: 'A cool app' }, app_name: 'My App' }],
    });
    // Insert new build
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-new-123' }],
    });

    const res = await agent
      .post('/builds/build-live/rebuild')
      .type('form')
      .send({ _csrf: csrf, rebuild_request: 'Add dark mode' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-new-123');
  });

  it('creates rebuild without change request', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-failed', tier: 'build', spec: { app_name: 'App' }, app_name: 'App' }],
    });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-new-456' }],
    });

    const res = await agent
      .post('/builds/build-failed/rebuild')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-new-456');
  });

  it('returns 404 for a build not in live or failed status', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent
      .post('/builds/build-building/rebuild')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(404);
  });

  it('returns 400 when original build has no spec', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-nospec', tier: 'build', spec: null, app_name: 'NoSpec' }],
    });

    const res = await agent
      .post('/builds/build-nospec/rebuild')
      .type('form')
      .send({ _csrf: csrf });

    expect(res.status).toBe(400);
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app)
      .post('/builds/build-1/rebuild')
      .type('form')
      .send({ _csrf: 'fake' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });
});

describe('POST /builds/:buildId/revision', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('creates a revision build with is_revision=true and redirects', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Original build query (preview status, no SE review notes)
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-preview', tier: 'grow', spec: { app_name: 'Test App' }, app_name: 'Test App', customer_id: 'customer-abc', review_notes: null }],
    });
    // Lineage CTE query (0 revisions so far, root tier = grow)
    mockQuery.mockResolvedValueOnce({
      rows: [{ root_tier: 'grow', revision_count: '0' }],
    });
    // Insert new revision build
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'revision-new-1' }],
    });

    const res = await agent
      .post('/builds/build-preview/revision')
      .type('form')
      .send({ _csrf: csrf, revision_feedback: 'The login page colors are wrong' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/revision-new-1');
  });

  it('forwards Senior Engineer review notes from the parent build into rebuild_request', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    const seNotes =
      '1. The hero headline runs past the right edge on mobile.\n' +
      '2. The Contact page gallery is showing placeholder images.\n' +
      '3. The admin dashboard stat cards should use the primary color.';
    // Original build query: parent has SE review notes attached
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-with-notes',
        tier: 'build',
        spec: { app_name: 'MW' },
        app_name: 'MW',
        customer_id: 'customer-abc',
        review_notes: seNotes,
      }],
    });
    // Lineage CTE query
    mockQuery.mockResolvedValueOnce({
      rows: [{ root_tier: 'build', revision_count: '0' }],
    });
    // INSERT stub
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'revision-with-notes-1' }],
    });

    const res = await agent
      .post('/builds/build-with-notes/revision')
      .type('form')
      .send({ _csrf: csrf, revision_feedback: 'apply everything from ocean and make it look good everywhere' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/revision-with-notes-1');

    // Inspect the INSERT call to verify rebuild_request has both
    // the Senior Engineer section AND the customer section.
    // Call order: [0] login SELECT customer (from makeAuthenticatedAgent),
    // [1] SELECT original build, [2] lineage CTE, [3] INSERT new build.
    expect(mockQuery).toHaveBeenCalledTimes(4);
    const insertCall = mockQuery.mock.calls[3];
    const insertParams = insertCall[1] as unknown[];
    const rebuildRequest = insertParams[5] as string;
    expect(rebuildRequest).toContain('SENIOR ENGINEER REVIEW NOTES:');
    expect(rebuildRequest).toContain('The hero headline runs past the right edge');
    expect(rebuildRequest).toContain('Contact page gallery');
    expect(rebuildRequest).toContain('admin dashboard stat cards');
    expect(rebuildRequest).toContain('CUSTOMER ADDITIONAL FEEDBACK:');
    expect(rebuildRequest).toContain('apply everything from ocean');
    // The new spec should also stamp se_notes into revision_history for audit
    const specJson = insertParams[2] as string;
    const spec = JSON.parse(specJson);
    expect(spec.revision_history).toHaveLength(1);
    expect(spec.revision_history[0].se_notes).toBe(seNotes);
    expect(spec.revision_history[0].feedback).toContain('apply everything from ocean');
  });

  it('falls back to single-section rebuild_request when parent has no SE review notes (regression guard)', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Parent has review_notes = null (customer requested revision without SE review)
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-no-notes',
        tier: 'build',
        spec: { app_name: 'App' },
        app_name: 'App',
        customer_id: 'customer-abc',
        review_notes: null,
      }],
    });
    mockQuery.mockResolvedValueOnce({
      rows: [{ root_tier: 'build', revision_count: '0' }],
    });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'revision-no-notes-1' }],
    });

    const res = await agent
      .post('/builds/build-no-notes/revision')
      .type('form')
      .send({ _csrf: csrf, revision_feedback: 'Fix the colors please' });

    expect(res.status).toBe(302);
    // Call order: [0] login SELECT customer, [1] SELECT original build,
    // [2] lineage CTE, [3] INSERT new build.
    const insertParams = mockQuery.mock.calls[3][1] as unknown[];
    const rebuildRequest = insertParams[5] as string;
    // No SE header should appear when the parent had no notes
    expect(rebuildRequest).not.toContain('SENIOR ENGINEER REVIEW NOTES');
    expect(rebuildRequest).not.toContain('CUSTOMER ADDITIONAL FEEDBACK');
    expect(rebuildRequest).toContain('REVISION REQUEST (round 1 of');
    expect(rebuildRequest).toContain('Fix the colors please');
    // And se_notes should be null in the audit trail
    const spec = JSON.parse(insertParams[2] as string);
    expect(spec.revision_history[0].se_notes).toBeNull();
  });

  it('returns 400 when feedback is empty', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();

    const res = await agent
      .post('/builds/build-preview/revision')
      .type('form')
      .send({ _csrf: csrf, revision_feedback: '' });

    expect(res.status).toBe(400);
  });

  it('returns 404 for a non-preview build', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Build query returns empty (not in preview status or not owned)
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent
      .post('/builds/build-live/revision')
      .type('form')
      .send({ _csrf: csrf, revision_feedback: 'Fix colors' });

    expect(res.status).toBe(404);
  });

  it('redirects to features when revision limit is exhausted', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    // Original build (preview). After the 2026-05-01 tier kill, the revision
    // limit is the universal UNLIMITED_REVISION_LIMIT (999); only a count
    // beyond that triggers the redirect-to-features fallback.
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-preview', tier: 'build', spec: { app_name: 'App' }, app_name: 'App', customer_id: 'customer-abc' }],
    });
    // Lineage CTE: revision_count exceeds the unlimited cap (1000 > 999).
    mockQuery.mockResolvedValueOnce({
      rows: [{ root_tier: 'build', revision_count: '1000' }],
    });

    const res = await agent
      .post('/builds/build-preview/revision')
      .type('form')
      .send({ _csrf: csrf, revision_feedback: 'More fixes needed' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/projects/build-preview/features');
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app)
      .post('/builds/build-1/revision')
      .type('form')
      .send({ _csrf: 'fake', revision_feedback: 'Fix something' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });
});

describe('GET /builds/:buildId/download-source', () => {
  // The rule (who may have the source) is in the statement, so the database
  // is what applies it; money-path.realdb.test.ts runs it on a real Postgres.
  // Here the database is a double: these tests pin which statement each
  // caller gets and how the route answers what the statement returns.
  beforeEach(() => {
    mockQuery.mockReset();
  });

  /** A row as OWNER_SOURCE_DOWNLOAD_SQL returns it. */
  const sourceRow = (over: Record<string, unknown> = {}) => ({
    app_name: 'test-app',
    allowed: true,
    has_artifact: true,
    build_artifact: Buffer.from('data'),
    ...over,
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).get('/builds/build-123/download-source');
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('returns 404 when build not found', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await agent.get('/builds/build-123/download-source');
    expect(res.status).toBe(404);
  });

  it('the owner is asked through OWNER_SOURCE_DOWNLOAD_SQL, with the build id and the SESSION customer id', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [sourceRow()] });
    await agent.get('/builds/build-123/download-source');
    const [sql, params] = mockQuery.mock.calls[mockQuery.mock.calls.length - 1];
    expect(sql).toBe(OWNER_SOURCE_DOWNLOAD_SQL);
    expect(params).toEqual(['build-123', 'customer-abc']);
  });

  it('the owner statement serves a live build, or a preview the owner could publish, and nothing else', () => {
    const sql = OWNER_SOURCE_DOWNLOAD_SQL.replace(/\s+/g, ' ');
    expect(sql).toContain('WHERE b.id = $1 AND b.customer_id = $2');
    // Allowed: live, or exactly the publishing rule (preview, approved, and a
    // subscription for a Harbor build).
    expect(sql).toContain(
      "COALESCE(b.status = 'live' OR (b.status = 'preview' AND b.review_status = 'approved' AND (NOT b.is_harbor OR EXISTS ("
    );
    expect(sql).toMatch(/k\.stripe_subscription_id IS NOT NULL AND k\.subscription_status IN \('active', 'trialing', 'past_due'\)/);
    // The tarball is read only when the rule holds.
    expect(sql).toContain('CASE WHEN src.allowed THEN b.build_artifact END AS build_artifact');
    // The publishing rule appears here word for word as it does in the approve statement.
    const rule = CUSTOMER_APPROVE_SQL.slice(CUSTOMER_APPROVE_SQL.indexOf("b.status = 'preview'"), CUSTOMER_APPROVE_SQL.indexOf('RETURNING')).replace(/\s+/g, ' ').trim();
    expect(sql).toContain(rule);
  });

  it('refuses with 403 when the statement says the owner may not have it yet, and sends no file', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [sourceRow({ allowed: false, build_artifact: null })] });
    const res = await agent.get('/builds/build-123/download-source');
    expect(res.status).toBe(403);
    expect(res.text).toMatch(/once your website is live/);
    expect(res.headers['content-type']).not.toContain('application/gzip');
  });

  it('returns 404 when the build is allowed but has no stored artifact', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [sourceRow({ has_artifact: false, build_artifact: null })] });
    const res = await agent.get('/builds/build-123/download-source');
    expect(res.status).toBe(404);
  });

  it('returns the tar.gz when the statement allows it', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [sourceRow({ app_name: 'my-app', build_artifact: Buffer.from('fake-tar-gz-data') })],
    });
    const res = await agent.get('/builds/my-build-id/download-source');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toContain('application/gzip');
    expect(res.headers['content-disposition']).toContain('my-app-source.tar.gz');
  });

  it('the admin gets any preview or live build, through a statement with no review or subscription condition', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rows: [sourceRow({ app_name: 'preview-app' })] });
    const res = await agent.get('/builds/build-456/download-source');
    expect(res.status).toBe(200);
    const [sql, params] = mockQuery.mock.calls[mockQuery.mock.calls.length - 1];
    expect(params).toEqual(['build-456']);
    expect(String(sql)).toContain("b.status IN ('preview', 'live') AS allowed");
    expect(String(sql)).not.toMatch(/customer_id|is_harbor|review_status/);
  });

  it('the admin is told 400 for a build that is neither preview nor live', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rows: [sourceRow({ allowed: false, build_artifact: null })] });
    const res = await agent.get('/builds/build-123/download-source');
    expect(res.status).toBe(400);
  });
});


// ---------------------------------------------------------------------------
// Review round additions
// ---------------------------------------------------------------------------

const ADMIN_TEST_EMAIL = 'review-admin@example.test';

async function makeAdminAgent(): Promise<supertest.Agent> {
  process.env.ADMIN_EMAIL = ADMIN_TEST_EMAIL;
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
  mockQuery.mockResolvedValueOnce({
    rows: [{ id: 'admin-1', email: ADMIN_TEST_EMAIL, name: 'Admin', password: hashed, email_verified: true }],
  });
  await agent.post('/login').type('form').send({ _csrf: getValidCsrf(), email: ADMIN_TEST_EMAIL, password: 'password123' });
  return agent;
}

describe('approve: who receives the "your website is live" email', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    (sendAppLiveEmail as ReturnType<typeof vi.fn>).mockClear();
  });

  it('sends it to the build owner when the admin approves on their behalf', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'b1', app_name: 'OwnerSite', preview_url: 'https://owner.example.test', customer_id: 'customer-owner' }],
    });
    // owner lookup
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'owner@example.test', name: 'Olive Owner' }] });

    const res = await agent.post('/builds/b1/approve').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    expect(sendAppLiveEmail).toHaveBeenCalledTimes(1);
    const args = (sendAppLiveEmail as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(args[0]).toBe('owner@example.test');
    expect(args[1]).toBe('Olive Owner');
    expect(args[0]).not.toBe(ADMIN_TEST_EMAIL);
  });

  it('sends nothing (rather than mailing the admin) when the owner cannot be found', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'b2', app_name: 'Orphan', preview_url: 'https://o.example.test', customer_id: 'customer-gone' }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent.post('/builds/b2/approve').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    expect(sendAppLiveEmail).not.toHaveBeenCalled();
  });

  it('still emails the session user when the owner approves their own build (no extra lookup)', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'b3', app_name: 'Mine', preview_url: 'https://m.example.test', customer_id: 'customer-abc' }],
    });
    const callsBefore = mockQuery.mock.calls.length;

    await agent.post('/builds/b3/approve').type('form').send({ _csrf: getValidCsrf() });

    expect((sendAppLiveEmail as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe('me@example.com');
    // Only the approve UPDATE ran: no owner lookup query was issued.
    expect(mockQuery.mock.calls.length - callsBefore).toBe(1);
  });
});

describe('revisions created by the admin belong to the customer', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('legacy revision form: INSERT uses the build owner, not the admin session', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'bp', tier: 'build', spec: { app_name: 'x' }, app_name: 'x', customer_id: 'customer-owner', review_notes: null }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [{ revision_count: '0' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'rev-1' }] });

    const res = await agent
      .post('/builds/bp/revision')
      .type('form')
      .send({ _csrf: getValidCsrf(), revision_feedback: 'Please change the header colour' });

    expect(res.status).toBe(302);
    const insert = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('INSERT INTO builds'));
    expect(insert).toBeDefined();
    expect((insert![1] as unknown[])[0]).toBe('customer-owner');
    // The select now includes customer_id so there is an owner to use.
    const select = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes("status = 'preview'"));
    expect(String(select![0])).toContain('customer_id');
  });

  it('revision confirm: INSERT uses the build owner, not the admin session', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'bp', tier: 'build', spec: { app_name: 'x' }, app_name: 'x', customer_id: 'customer-owner', review_notes: null }],
    });
    mockQuery.mockResolvedValueOnce({ rows: [{ revision_count: '0' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ role: 'user', content: 'change the colours' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'rev-2' }] });

    const res = await agent.post('/builds/bp/revision/confirm').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    const insert = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('INSERT INTO builds'));
    expect((insert![1] as unknown[])[0]).toBe('customer-owner');
  });
});

describe('non-string request bodies answer 4xx instead of an unhandled rejection', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('POST /builds/:id/revision with revision_feedback[]=x', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/builds/bp/revision')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&revision_feedback[]=a&revision_feedback[]=b');
    expect(res.status).toBe(400);
  });

  it('POST /builds/:id/rebuild with rebuild_request[a]=x treats it as no change request', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'b', tier: 'build', spec: { app_name: 'x' }, app_name: 'x' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'new-b' }] });
    const res = await agent
      .post('/builds/b/rebuild')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&rebuild_request[a]=x');
    expect(res.status).toBe(302);
    const insert = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('INSERT INTO builds'));
    expect((insert![1] as unknown[])[5]).toBeNull();
  });

  it('POST /builds/:id/revision/message with a numeric message', async () => {
    const agent = await makeAuthenticatedAgent();
    const res = await agent
      .post('/builds/bp/revision/message')
      .set('x-csrf-token', getValidCsrf())
      .send({ message: 12345 });
    expect(res.status).toBe(400);
  });
});

describe('status page and downloads: what leaves the server', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('GET /builds/:id selects named columns, never the source tarball or the database URL', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    await agent.get('/builds/b-cols');
    const sql = String(mockQuery.mock.calls[mockQuery.mock.calls.length - 1][0]);
    expect(sql).toMatch(/FROM builds/);
    expect(sql).not.toMatch(/SELECT\s+\*/);
    expect(sql).not.toContain('build_artifact');
    expect(sql).not.toContain('railway_database_url');
    // Everything status.ejs reads is still selected.
    for (const col of ['status', 'preview_url', 'live_url', 'review_status', 'qa_results', 'screenshots', 'app_name']) {
      expect(sql).toContain(col);
    }
  });

  it('screenshots behind login are not cacheable by shared caches', async () => {
    const agent = await makeAuthenticatedAgent();
    const png = Buffer.from('iVBORw0KGgo=', 'base64').toString('base64');
    mockQuery.mockResolvedValueOnce({ rows: [{ screenshots: [{ page: '/', data: png }] }] });
    const res = await agent.get('/api/builds/b-shot/screenshots/0');
    expect(res.status).toBe(200);
    expect(res.headers['cache-control']).toMatch(/\bprivate\b/);
    expect(res.headers['cache-control']).not.toMatch(/\bpublic\b/);
  });

  it('download filename cannot carry quotes or header-breaking characters from app_name', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{ app_name: 'evil"name\r\nSet-Cookie: x=1', build_artifact: Buffer.from('x'), allowed: true, has_artifact: true }],
    });
    const res = await agent.get('/builds/b-dl/download-source');
    expect(res.status).toBe(200);
    const disposition = String(res.headers['content-disposition']);
    expect(disposition).toMatch(/^attachment; filename="[A-Za-z0-9._-]+-source\.tar\.gz"$/);
    expect(res.headers['set-cookie']?.some((c: string) => c.startsWith('x='))).toBeFalsy();
  });
});


// ----------------------------------------------------------------------
// Build-start budget: retry, rebuild and revision cannot queue without bound
// ----------------------------------------------------------------------

import {
  buildStartRefusal,
  redactForCustomer,
  MAX_ACTIVE_BUILDS_PER_CUSTOMER,
  MAX_BUILD_STARTS_PER_DAY,
  MAX_MANUAL_RETRIES_PER_BUILD,
  CUSTOMER_APPROVE_SQL,
  ADMIN_APPROVE_SQL,
  OWNER_SOURCE_DOWNLOAD_SQL,
  REVIEW_RESET_ASSIGNMENTS,
} from '../../src/routes/builds';

function statementsMatching(pattern: RegExp): Array<{ sql: string; params?: unknown[] }> {
  return dbDouble.clientCalls.filter((call) => pattern.test(call.sql));
}

describe('buildStartRefusal (the rule)', () => {
  const none = { active: 0, startsToday: 0, retriesOfThisBuild: 0 };

  it('allows a start inside every limit', () => {
    expect(buildStartRefusal(none, 'new')).toBeNull();
    expect(buildStartRefusal(none, 'retry')).toBeNull();
    expect(
      buildStartRefusal(
        {
          active: MAX_ACTIVE_BUILDS_PER_CUSTOMER - 1,
          startsToday: MAX_BUILD_STARTS_PER_DAY - 1,
          retriesOfThisBuild: MAX_MANUAL_RETRIES_PER_BUILD - 1,
        },
        'retry'
      )
    ).toBeNull();
  });

  it('refuses at each limit', () => {
    expect(buildStartRefusal({ ...none, active: MAX_ACTIVE_BUILDS_PER_CUSTOMER }, 'new')).toMatch(/in progress/);
    expect(buildStartRefusal({ ...none, startsToday: MAX_BUILD_STARTS_PER_DAY }, 'new')).toMatch(/one day/);
    expect(
      buildStartRefusal({ ...none, retriesOfThisBuild: MAX_MANUAL_RETRIES_PER_BUILD }, 'retry')
    ).toMatch(/retried several times/);
  });

  it('the per-build retry cap does not apply to a new rebuild or revision', () => {
    expect(buildStartRefusal({ ...none, retriesOfThisBuild: 99 }, 'new')).toBeNull();
  });
});

describe('POST /builds/:buildId/retry', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    resetBuildBudget();
  });

  it('requeues the caller\'s failed build under the per-customer lock and records the retry', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] }); // ownership + failed
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] }); // UPDATE ... RETURNING
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 }); // INSERT build_logs

    const res = await agent.post('/builds/build-failed/retry').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-failed');

    const order = dbDouble.clientCalls.map((call) => call.sql.trim().split(/\s+/).slice(0, 3).join(' '));
    expect(order[0]).toBe('BEGIN');
    expect(dbDouble.clientCalls[1].sql).toContain('pg_advisory_xact_lock');
    expect(dbDouble.clientCalls[1].params).toEqual(['customer-abc']);
    expect(order[order.length - 1]).toBe('COMMIT');
    // The lock is taken before the budget is read, and the budget before the write.
    const lockAt = dbDouble.clientCalls.findIndex((c) => c.sql.includes('pg_advisory_xact_lock'));
    const budgetAt = dbDouble.clientCalls.findIndex((c) => c.sql.includes('AS build_retries'));
    const updateAt = dbDouble.clientCalls.findIndex((c) => /UPDATE builds/.test(c.sql));
    expect(lockAt).toBeLessThan(budgetAt);
    expect(budgetAt).toBeLessThan(updateAt);

    const update = statementsMatching(/UPDATE builds/)[0];
    expect(update.sql).toMatch(/customer_id = \$2 AND status = 'failed'/);
    expect(update.params).toEqual(['build-failed', 'customer-abc']);
    const audit = statementsMatching(/INSERT INTO build_logs/)[0];
    expect(audit.sql).toContain("'manual_retry'");
    expect(audit.params).toEqual(['build-failed', `Retry requested (1 of ${MAX_MANUAL_RETRIES_PER_BUILD}).`]);
    // The start is entered in the ledger the budget counts, in the same transaction.
    const ledger = statementsMatching(/INSERT INTO build_start_events/);
    expect(ledger).toHaveLength(1);
    expect(ledger[0].params).toEqual(['customer-abc', 'build-failed', 'retry']);
    const ledgerAt = dbDouble.clientCalls.findIndex((c) => /INSERT INTO build_start_events/.test(c.sql));
    expect(ledgerAt).toBeGreaterThan(updateAt);
    expect(ledgerAt).toBeLessThan(dbDouble.clientCalls.length - 1); // before COMMIT
  });

  it('counts starts from the ledger table, not from rows the worker may prune', () => {
    // The worker deletes a customer's older failed builds and their
    // build_logs rows. A budget counted from either would be handed back
    // every time that happened.
    // The reader lives in the module the retry, rebuild and revision routes
    // and the Harbor dispatch all take it from.
    const source = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'services', 'build-start-limits.ts'), 'utf-8');
    const reader = source.slice(source.indexOf('async function readBuildStartBudget'), source.indexOf('async function recordBuildStart'));
    expect(reader).toContain('FROM build_start_events');
    expect(reader).not.toContain('build_logs');
    expect(reader).not.toContain('parent_build_id');
  });

  it('answers 404 for a build that is not the caller\'s or not failed, without consuming budget', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // ownership check finds nothing

    const res = await agent.post('/builds/someone-elses/retry').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(404);
    expect(statementsMatching(/UPDATE builds/)).toHaveLength(0);
    expect(statementsMatching(/INSERT INTO build_logs/)).toHaveLength(0);
  });

  it.each([
    ['the build has used its manual retries', { build_retries: MAX_MANUAL_RETRIES_PER_BUILD }, /retried several times/],
    ['the customer already has builds in progress', { active: MAX_ACTIVE_BUILDS_PER_CUSTOMER }, /in progress/],
    ['the daily budget is spent', { starts_today: MAX_BUILD_STARTS_PER_DAY }, /one day/],
  ])('refuses with 429 and queues nothing when %s', async (_label, budget, message) => {
    const agent = await makeAuthenticatedAgent();
    resetBuildBudget(budget);
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] }); // ownership + failed

    const res = await agent.post('/builds/build-failed/retry').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(429);
    expect(res.text).toMatch(message);
    expect(statementsMatching(/UPDATE builds/)).toHaveLength(0);
    expect(statementsMatching(/INSERT INTO build_logs/)).toHaveLength(0);
    expect(statementsMatching(/INSERT INTO build_start_events/)).toHaveLength(0);
  });

  it('rejects a missing CSRF token before touching the database', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockReset();
    const res = await agent.post('/builds/build-failed/retry').type('form').send({});
    expect(res.status).toBe(403);
    expect(mockQuery).not.toHaveBeenCalled();
    expect(dbDouble.clientCalls).toHaveLength(0);
  });

  it('redirects to /login when not authenticated', async () => {
    const res = await request(app).post('/builds/b/retry').type('form').send({ _csrf: 'x' });
    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });
});

describe('POST /builds/:buildId/rebuild and revision: build-start budget', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    resetBuildBudget();
  });

  const liveBuild = { id: 'build-live', tier: 'build', spec: { app_name: 'App' }, app_name: 'App' };
  const previewBuild = {
    id: 'build-preview', tier: 'build', spec: { app_name: 'App' }, app_name: 'App',
    customer_id: 'customer-abc', review_notes: null,
  };

  it('a rebuild inserts under the lock when the budget allows it', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [liveBuild] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-new' }] });

    const res = await agent.post('/builds/build-live/rebuild').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/build-new');
    expect(dbDouble.clientCalls.some((c) => c.sql.includes('pg_advisory_xact_lock'))).toBe(true);
    expect(statementsMatching(/INSERT INTO builds/)).toHaveLength(1);
    const ledger = statementsMatching(/INSERT INTO build_start_events/);
    expect(ledger).toHaveLength(1);
    expect(ledger[0].params).toEqual(['customer-abc', 'build-new', 'rebuild']);
  });

  it.each([
    ['too many builds in progress', { active: MAX_ACTIVE_BUILDS_PER_CUSTOMER }],
    ['the daily budget is spent', { starts_today: MAX_BUILD_STARTS_PER_DAY }],
  ])('a rebuild is refused with 429 and inserts nothing when %s', async (_label, budget) => {
    const agent = await makeAuthenticatedAgent();
    resetBuildBudget(budget);
    mockQuery.mockResolvedValueOnce({ rows: [liveBuild] });

    const res = await agent.post('/builds/build-live/rebuild').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(429);
    expect(statementsMatching(/INSERT INTO builds/)).toHaveLength(0);
    expect(statementsMatching(/INSERT INTO build_start_events/)).toHaveLength(0);
    expect(mockQuery.mock.calls.some(([sql]) => /INSERT INTO builds/.test(String(sql)))).toBe(false);
  });

  it('an accepted revision is entered in the ledger as a revision', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [previewBuild] });
    mockQuery.mockResolvedValueOnce({ rows: [{ revision_count: '0' }] }); // lineage count
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'revision-new' }] }); // INSERT builds

    const res = await agent
      .post('/builds/build-preview/revision')
      .type('form')
      .send({ _csrf: getValidCsrf(), revision_feedback: 'Change the heading' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/builds/revision-new');
    const ledger = statementsMatching(/INSERT INTO build_start_events/);
    expect(ledger).toHaveLength(1);
    expect(ledger[0].params).toEqual(['customer-abc', 'revision-new', 'revision']);
  });

  it('the legacy revision form is refused with 429 once the daily budget is spent', async () => {
    const agent = await makeAuthenticatedAgent();
    resetBuildBudget({ starts_today: MAX_BUILD_STARTS_PER_DAY });
    mockQuery.mockResolvedValueOnce({ rows: [previewBuild] });
    mockQuery.mockResolvedValueOnce({ rows: [{ revision_count: '0' }] }); // lineage count

    const res = await agent
      .post('/builds/build-preview/revision')
      .type('form')
      .send({ _csrf: getValidCsrf(), revision_feedback: 'Change the heading' });

    expect(res.status).toBe(429);
    expect(statementsMatching(/INSERT INTO builds/)).toHaveLength(0);
  });

  it('revision confirm is refused with 429 while builds are already in progress', async () => {
    const agent = await makeAuthenticatedAgent();
    resetBuildBudget({ active: MAX_ACTIVE_BUILDS_PER_CUSTOMER });
    mockQuery.mockResolvedValueOnce({ rows: [previewBuild] });
    mockQuery.mockResolvedValueOnce({ rows: [{ revision_count: '0' }] }); // lineage count
    mockQuery.mockResolvedValueOnce({ rows: [{ role: 'user', content: 'Change the heading' }] }); // chat

    const res = await agent
      .post('/builds/build-preview/revision/confirm')
      .type('form')
      .send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(429);
    expect(statementsMatching(/INSERT INTO builds/)).toHaveLength(0);
  });

  it('the admin is exempt: no lock, no budget query, and the retry is logged as admin_retry', async () => {
    const agent = await makeAdminAgent();
    resetBuildBudget({ active: 99, starts_today: 99, build_retries: 99 });
    mockQuery.mockReset();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] }); // UPDATE ... RETURNING
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 }); // INSERT build_logs

    const res = await agent.post('/builds/build-failed/retry').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    expect(dbDouble.clientCalls).toHaveLength(0);
    const audit = mockQuery.mock.calls.find(([sql]) => /INSERT INTO build_logs/.test(String(sql)));
    expect(String(audit?.[0])).toContain("'admin_retry'");
  });
});

// ----------------------------------------------------------------------
// Internal fields stay internal
// ----------------------------------------------------------------------

describe('GET /api/builds/:buildId: platform cost is for the admin only', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  const row = {
    status: 'live', live_url: 'https://app.example', preview_url: null, error_message: null, tier: 'build',
    queue_position: '0', health_status: null, health_last_checked: null, health_latency_ms: null,
    health_consecutive_failures: null, retry_count: 0, total_api_cost_usd: '4.2500',
  };

  it('a customer response has no cost field, and the customer query never selects the column', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [row] }); // even if the driver handed the column back
    const res = await agent.get('/api/builds/build-1');
    expect(res.status).toBe(200);
    expect(res.body).not.toHaveProperty('total_api_cost_usd');
    expect(JSON.stringify(res.body)).not.toContain('4.25');
    const sql = String(mockQuery.mock.calls[mockQuery.mock.calls.length - 1][0]);
    expect(sql).toContain('customer_id = $2');
    expect(sql).not.toContain('total_api_cost_usd');
    expect(sql).not.toContain('queue_job_id');
  });

  it('an admin response carries the cost', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockReset();
    mockQuery.mockResolvedValueOnce({ rows: [row] });
    const res = await agent.get('/api/builds/build-1');
    expect(res.status).toBe(200);
    expect(res.body.total_api_cost_usd).toBe('4.2500');
  });
});

describe('worker text shown to a customer is scrubbed of connection strings', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  // Built at runtime so no secret-shaped literal sits in the repository.
  const dbUrl = ['postgresql', '://', 'app_user', ':', 'pw-' + 'x'.repeat(12), '@', 'db.internal.example:5432/app'].join('');
  const tokenUrl = ['https', '://', 'deploy', ':', 'tok-' + 'y'.repeat(12), '@', 'git.example/repo.git'].join('');

  it('redactForCustomer removes database URLs and credentials embedded in URLs, and nothing else', () => {
    const out = redactForCustomer(`psql failed for ${dbUrl} while cloning ${tokenUrl} (see https://docs.example/help)`);
    expect(out).not.toContain('pw-');
    expect(out).not.toContain('tok-');
    expect(out).not.toContain('db.internal.example');
    expect(out).toContain('[connection string removed]');
    expect(out).toContain('https://[credentials removed]@git.example/repo.git');
    expect(out).toContain('https://docs.example/help');
    expect(redactForCustomer('Build failed: timeout after 120s')).toBe('Build failed: timeout after 120s');
  });

  it('credentials are removed whatever stands in front of the scheme, and text without them is returned unchanged', () => {
    const secret = 'tok-' + 'z'.repeat(10);
    const cases: Array<[string, string]> = [
      [`git+https://ci:${secret}@git.example/x`, 'git+https://[credentials removed]@git.example/x'],
      [`1.https://ci:${secret}@git.example/x`, '1.https://[credentials removed]@git.example/x'],
      [`(HTTPS://ci:${secret}@git.example/x)`, '(HTTPS://[credentials removed]@git.example/x)'],
      [`a.b.c.ssh://deploy:${secret}@10.0.0.5`, 'a.b.c.ssh://[credentials removed]@10.0.0.5'],
      [`two: http://a:${secret}@h1 and ftp://b:${secret}@h2`, 'two: http://[credentials removed]@h1 and ftp://[credentials removed]@h2'],
    ];
    for (const [input, expected] of cases) {
      expect(redactForCustomer(input), input).toBe(expected);
      expect(redactForCustomer(input)).not.toContain(secret);
    }
    // A port, an email address, a time and an "@" after the path are not credentials.
    for (const untouched of [
      'http://localhost:4000/health',
      'https://example.com?mail=me@example.test',
      'mail admin@example.com at 12:30:45',
      'at Object.<anonymous> (/app/node_modules/pkg/lib/index.js:12:5)',
      'see https://docs.example/a:b@c',
    ]) {
      expect(redactForCustomer(untouched), untouched).toBe(untouched);
    }
  });

  it('the whole userinfo goes, whatever shape it has: a token alone, a password with ")" or "@" in it, a user name alone', () => {
    const secret = 'tok-' + 'w'.repeat(10);
    const cases: Array<[string, string]> = [
      // A token as the only userinfo: there is no ":" to tell it from a user name.
      [`cloning https://${secret}@github.example/o/r.git failed`, 'cloning https://[credentials removed]@github.example/o/r.git failed'],
      [`https://x-access-token:${secret}@github.example/o/r.git`, 'https://[credentials removed]@github.example/o/r.git'],
      // A password holding ")" or "@".
      [`https://ci:pa)${secret}@git.example/x`, 'https://[credentials removed]@git.example/x'],
      [`(https://ci:pa@${secret}@git.example/x)`, '(https://[credentials removed]@git.example/x)'],
      // A user name alone is removed too: it may be the token.
      ['https://user@git.example/repo', 'https://[credentials removed]@git.example/repo'],
    ];
    for (const [input, expected] of cases) {
      expect(redactForCustomer(input), input).toBe(expected);
      expect(redactForCustomer(input)).not.toContain(secret);
    }
  });

  it('a database URL goes whole, even when its password holds ")" or the URL stands in parentheses', () => {
    const tail = 'rest-' + 'v'.repeat(10);
    // Built from parts, as the other secret-shaped values in these tests are.
    const url = (scheme: string, userinfo: string, rest: string): string => [scheme, '://', userinfo, '@', rest].join('');
    for (const input of [
      url('postgres', `app:pa)${tail}`, 'db.internal.example:5432/app'),
      `(see ${url('postgresql', `app:pa)${tail}`, 'db.internal.example/app')})`,
      url('mongodb+srv', `app:p)(a)${tail}`, 'cluster.example/app?retryWrites=true'),
      url('redis', `:pa)${tail}`, 'cache.internal.example:6379/0'),
      `error: could not connect to "${url('mysql', `app:pa)${tail}`, 'db.internal.example/app')}"`,
    ]) {
      const out = redactForCustomer(input);
      expect(out, input).not.toContain(tail);
      expect(out, input).not.toContain('db.internal.example');
      expect(out, input).not.toContain('cluster.example');
      expect(out, input).not.toContain('cache.internal.example');
      expect(out, input).toContain('[connection string removed]');
    }
  });

  it('takes time in proportion to the text: a long dotted log (a stack trace) is redacted in milliseconds', () => {
    // Every word after a "." or "-" used to be tried as the start of a URL
    // scheme and read to the end of the run: 80,000 characters took over a
    // second of this process's only event loop, on every status poll.
    const secret = 'tok-' + 'q'.repeat(10);
    const shapes = [
      'a.'.repeat(200_000),
      'a-'.repeat(200_000) + '://',
      'a+'.repeat(200_000) + `://u:${secret}@host`,
      'at mod.fn (/app/node_modules/a/b.c.js:1:2)\n'.repeat(10_000),
      '://'.repeat(100_000),
      '://a:'.repeat(80_000),
      // Long authorities with no "@", with many, and a long run of them each starting a new URL.
      '://' + 'a'.repeat(400_000),
      '://' + 'a@'.repeat(200_000),
      ('x://' + 'a'.repeat(50) + '@').repeat(8_000),
      '://' + 'a'.repeat(40) + ' '.repeat(1) + ('://' + 'b'.repeat(40) + ' ').repeat(8_000),
      'postgres://' + 'a)'.repeat(200_000),
      ('postgres://' + 'a'.repeat(30) + ' ').repeat(10_000),
    ];
    for (const text of shapes) {
      const started = process.hrtime.bigint();
      const out = redactForCustomer(text);
      const ms = Number(process.hrtime.bigint() - started) / 1e6;
      expect(ms, `${text.slice(0, 12)}... (${text.length} characters)`).toBeLessThan(250);
      expect(out).not.toContain(secret);
    }
  });

  it('the build API redacts error text for the owner and leaves it intact for the admin', async () => {
    const row = {
      status: 'failed', live_url: null, preview_url: null, error_message: `migrate failed: ${dbUrl}`, tier: 'build',
      queue_position: '0', health_status: null, retry_count: 1,
    };
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [row] });
    const customerRes = await agent.get('/api/builds/build-1');
    expect(customerRes.body.error).toBe('migrate failed: [connection string removed]');

    const admin = await makeAdminAgent();
    mockQuery.mockReset();
    mockQuery.mockResolvedValueOnce({ rows: [row] });
    const adminRes = await admin.get('/api/builds/build-1');
    expect(adminRes.body.error).toBe(`migrate failed: ${dbUrl}`);
  });

  it('the build log stream redacts messages for the owner', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-1' }] }); // ownership
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 7, stage: 'deploying', message: `DATABASE_URL=${dbUrl}`, created_at: new Date() }],
    });
    const res = await agent.get('/api/builds/build-1/logs');
    expect(res.status).toBe(200);
    expect(res.body.logs[0].message).toBe('DATABASE_URL=[connection string removed]');
  });

  it('the status page redacts the error text for the owner', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({
      rows: [{
        id: 'build-1', customer_id: 'customer-abc', tier: 'build', status: 'failed', app_name: 'App',
        spec: { app_name: 'App' }, error_message: `deploy failed: ${dbUrl}`, created_at: new Date(), updated_at: new Date(),
        qa_results: [], screenshots: [],
      }],
    });
    const res = await agent.get('/builds/build-1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('deploy failed: [connection string removed]');
    expect(res.text).not.toContain('db.internal.example');
  });
});


// ---------------------------------------------------------------------------
// The review gate is in the statement
// ---------------------------------------------------------------------------
//
// The approve route is one of the two places a build becomes `live`. What
// keeps an unreviewed, unowned or unpaid build from going live is the WHERE
// clause of the UPDATE it runs, so these tests read that statement: its text
// and its parameters, for the customer path and for the admin path. Removing
// a predicate from either statement fails a test here. (Whether Postgres
// then behaves as the text says is checked with real rows in
// money-path.realdb.test.ts.)

describe('POST /builds/:buildId/approve: the gate is in the statement', () => {
  const promoted = {
    rows: [{ id: 'build-gate', app_name: 'Gate', preview_url: 'https://preview.example.test', customer_id: 'customer-abc' }],
  };
  const savedHarborFlag = process.env.CAS_HARBOR_ENABLED;

  beforeEach(() => {
    mockQuery.mockReset();
    (sendAppLiveEmail as ReturnType<typeof vi.fn>).mockClear();
  });
  afterEach(() => {
    if (savedHarborFlag === undefined) delete process.env.CAS_HARBOR_ENABLED;
    else process.env.CAS_HARBOR_ENABLED = savedHarborFlag;
  });

  it('customer path: runs CUSTOMER_APPROVE_SQL with the build id and the SESSION customer id', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce(promoted);

    const res = await agent.post('/builds/build-gate/approve').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    const [sql, params] = mockQuery.mock.calls[1] as [string, unknown[]];
    expect(sql).toBe(CUSTOMER_APPROVE_SQL);
    // The owner is the logged-in customer, never anything from the request.
    expect(params).toEqual(['build-gate', 'customer-abc']);
  });

  it('customer path: the statement requires ownership, preview, an approved review, and a subscription for a Harbor build', () => {
    const where = CUSTOMER_APPROVE_SQL.slice(CUSTOMER_APPROVE_SQL.indexOf('UPDATE builds b'));
    expect(where).toMatch(/SET status = 'live'/);
    // One WHERE, every condition ANDed onto the row being updated.
    expect(where).toMatch(
      /WHERE b\.id = \$1 AND b\.customer_id = \$2\s+AND b\.status = 'preview' AND b\.review_status = 'approved'\s+AND \(NOT b\.is_harbor OR EXISTS \(/
    );
    expect(where).not.toMatch(/\bOR b\./);
    // The subscription that lets a Harbor build through belongs to the build's own customer and has not ended.
    expect(where).toMatch(/FROM keepers k\s+WHERE k\.customer_id = b\.customer_id\s+AND k\.stripe_subscription_id IS NOT NULL\s+AND k\.subscription_status IN \('active', 'trialing', 'past_due'\)/);
    // "Harbor" is the build row's own column. The statement reads no other
    // build row and no handoff stamp: those are rows that the worker's cleanup
    // and the delete routes remove, and the payment rule must not depend on them.
    expect(CUSTOMER_APPROVE_SQL).not.toMatch(/parent_build_id|handoff_build_id|trial_keepers|RECURSIVE/);
    expect(CUSTOMER_APPROVE_SQL.match(/\bbuilds\b/g)).toHaveLength(1);
    // Exactly two parameters: nothing else from the request reaches the statement.
    expect(new Set(CUSTOMER_APPROVE_SQL.match(/\$\d+/g))).toEqual(new Set(['$1', '$2']));
  });

  it('admin path: runs ADMIN_APPROVE_SQL with the build id only', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ ...promoted.rows[0], customer_id: 'customer-other' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ email: 'owner@example.test', name: 'Owner' }] }); // owner lookup for the email

    const res = await agent.post('/builds/build-gate/approve').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    const [sql, params] = mockQuery.mock.calls[1] as [string, unknown[]];
    expect(sql).toBe(ADMIN_APPROVE_SQL);
    expect(params).toEqual(['build-gate']);
  });

  it('admin path: the statement still requires preview, and only the admin session reaches it', async () => {
    expect(ADMIN_APPROVE_SQL).toMatch(/WHERE id = \$1 AND status = 'preview'\s+RETURNING/);
    expect(ADMIN_APPROVE_SQL).not.toMatch(/review_status|customer_id = /);

    // The same request from an ordinary customer never runs the admin statement.
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce(promoted);
    await agent.post('/builds/build-gate/approve').type('form').send({ _csrf: getValidCsrf() });
    expect(mockQuery.mock.calls.some(([sql]) => sql === ADMIN_APPROVE_SQL)).toBe(false);
  });

  it('the two statements are the only ones in the route file that set a build live', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'routes', 'builds.ts'), 'utf-8');
    // Writes only: the source-download rule also reads `b.status = 'live'`.
    expect(source.match(/SET\s+status = 'live'/g)).toHaveLength(2);
    expect(source.match(/status = 'live'/g)).toHaveLength(3);
  });

  it('a reviewed Harbor build whose owner has no subscription is sent to the make-public page, not published', async () => {
    process.env.CAS_HARBOR_ENABLED = 'true';
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] }); // the UPDATE matched nothing
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-harbor', customer_id: 'customer-abc', status: 'preview', review_status: 'approved' }],
    });

    const res = await agent.post('/builds/build-harbor/approve').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(303);
    expect(res.headers.location).toBe('/lighthouse/lamp/build-harbor/make-public');
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(sendAppLiveEmail).not.toHaveBeenCalled();
    // The diagnostic read is the last statement: nothing was written after the refused UPDATE.
    expect(mockQuery).toHaveBeenCalledTimes(3);
    expect(String(mockQuery.mock.calls[2][0])).toMatch(/^SELECT id, customer_id, status, review_status/);
  });

  it('with the Harbor routes not mounted the same case is refused with an explanation instead of a dead link', async () => {
    delete process.env.CAS_HARBOR_ENABLED;
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'build-harbor', customer_id: 'customer-abc', status: 'preview', review_status: 'approved' }],
    });

    const res = await agent.post('/builds/build-harbor/approve').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(409);
    expect(res.text).toContain('goes public when its subscription starts');
  });
});

describe('POST /builds/:buildId/retry: the review verdict does not survive the requeue', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    resetBuildBudget();
  });

  const expectReviewReset = (sql: string): void => {
    expect(sql).toContain(REVIEW_RESET_ASSIGNMENTS);
    for (const assignment of [
      'review_status = NULL',
      'reviewed_at = NULL',
      'reviewed_by = NULL',
      'review_notes = NULL',
      'review_report = NULL',
      'ai_review_iterations = 0',
      'ai_reviewed_at = NULL',
    ]) {
      expect(sql).toContain(assignment);
    }
  };

  it('customer retry: the UPDATE that sets queued also clears every review column', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] });
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] });
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 });

    await agent.post('/builds/build-failed/retry').type('form').send({ _csrf: getValidCsrf() });

    const update = statementsMatching(/UPDATE builds/)[0];
    expect(update.sql).toMatch(/SET status = 'queued'/);
    expectReviewReset(update.sql);
  });

  it('admin retry: the same', async () => {
    const agent = await makeAdminAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-failed' }] }); // UPDATE
    mockQuery.mockResolvedValueOnce({ rows: [], rowCount: 1 }); // audit row

    const res = await agent.post('/builds/build-failed/retry').type('form').send({ _csrf: getValidCsrf() });

    expect(res.status).toBe(302);
    const [sql, params] = mockQuery.mock.calls[1] as [string, unknown[]];
    expect(sql).toMatch(/UPDATE builds SET status = 'queued'/);
    expect(sql).toMatch(/WHERE id = \$1 AND status = 'failed'/);
    expect(params).toEqual(['build-failed']);
    expectReviewReset(sql);
  });

  it('every statement in routes/builds.ts and routes/ops-api.ts that sends an existing build back to queued clears the verdict', () => {
    for (const file of ['builds.ts', 'ops-api.ts']) {
      const source = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'routes', file), 'utf-8');
      // Each `SET status = 'queued'` assignment list, up to its WHERE (or the end of the shared fragment).
      const assignments = source.split(/SET status = 'queued'/).slice(1).map((rest) => rest.split(/\bWHERE\b|`;/)[0]);
      expect(assignments.length, file).toBeGreaterThan(0);
      for (const assignment of assignments) {
        expect(assignment, file).toContain('${REVIEW_RESET_ASSIGNMENTS}');
      }
    }
  });
});
