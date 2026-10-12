import { describe, it, expect, vi, beforeEach } from 'vitest';
import crypto from 'crypto';
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
  sendBuildQueuedEmail: vi.fn().mockResolvedValue(undefined),
  sendBuildFailedEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn((email: string) => `token-for-${email}`),
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
  constructWebhookEvent: vi.fn(),
}));
vi.mock('../../src/services/claude', () => ({
  processIntakeMessage: vi.fn(),
  generateInitialGreeting: vi.fn().mockResolvedValue('Hello!'),
  processFeatureMessage: vi.fn(),
  generateFeatureGreeting: vi.fn().mockResolvedValue('Hello!'),
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

async function makeAuthenticatedAgent() {
  const agent = supertest.agent(app);
  const hashed = await bcrypt.hash('password123', 1);
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

describe('GET /gallery', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 200 with no auth required', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Website Gallery');
  });

  it('renders gallery apps with display names', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-1',
          app_name: 'neighborhood-cms',
          tier: 'build',
          live_url: 'https://cool.up.railway.app',
          gallery_description: 'A cool productivity tool',
          gallery_category: 'SaaS',
          gallery_featured: false,
          created_at: new Date(),
          spec: null,
          screenshots: null,
        },
      ],
    });

    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Neighborhood CMS');
    expect(res.text).toContain('A cool productivity tool');
    expect(res.text).toContain('SaaS');
  });

  // gallery_display_name lets the operator override the
  // slug-derived title with a brand-accurate label (e.g. "Sample Studio
  // Website, with CMS") without mutating the canonical app_name.
  it('gallery_display_name overrides displayName(app_name) when set', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-x',
          app_name: 'sample-studio-premium',
          tier: 'build',
          live_url: 'https://sample-studio-example.up.railway.app',
          gallery_description: 'A sample studio website',
          gallery_category: 'Wellness',
          gallery_featured: true,
          gallery_display_name: 'Sample Studio Website, with CMS',
          created_at: new Date(),
          spec: null,
          screenshots: null,
        },
      ],
    });
    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    // Override wins
    expect(res.text).toContain('Sample Studio Website, with CMS');
    // Slug-derived title is NOT rendered
    expect(res.text).not.toContain('Sample Studio Premium');
  });

  it('falls back to displayName(app_name) when gallery_display_name is null', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-y',
          app_name: 'sample-studio',
          tier: 'build',
          live_url: 'https://c.up.railway.app',
          gallery_description: 'x',
          gallery_category: 'Healer',
          gallery_featured: false,
          gallery_display_name: null,
          created_at: new Date(),
          spec: null,
          screenshots: null,
        },
      ],
    });
    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Sample Studio');
  });

  it('SELECT pulls gallery_display_name column', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    await request(app).get('/gallery');
    const sql = mockQuery.mock.calls[0][0] as string;
    expect(sql).toMatch(/gallery_display_name/);
  });

  it('extracts description from spec when gallery_description is null', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-2',
          app_name: 'Spec App',
          tier: 'grow',
          live_url: 'https://spec.up.railway.app',
          gallery_description: null,
          gallery_category: null,
          gallery_featured: false,
          created_at: new Date(),
          // The query now truncates spec.description in SQL (LEFT(..., 200)).
          spec_description: 'A website described in the spec',
          screenshot_meta: [],
        },
      ],
    });

    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    expect(res.text).toContain('A website described in the spec');
  });

  it('never selects the raw spec or screenshots columns (base64 image data) for the public page', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    await request(app).get('/gallery');
    const sql = mockQuery.mock.calls[0][0] as string;
    // Reduced inside Postgres: no bare b.spec / b.screenshots in the select list.
    expect(sql).not.toMatch(/\bb\.spec\s*,/);
    expect(sql).not.toMatch(/\bb\.screenshots\s*,/);
    expect(sql).toMatch(/LEFT\(b\.spec->>'description', 200\)/);
    expect(sql).toMatch(/screenshot_meta/);
    expect(sql).toMatch(/LIMIT 50/);
  });

  it('does not render a preview button (removed) even when app has screenshots', async () => {
    const fakeB64 = Buffer.from('fakepng').toString('base64');
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-ss',
          app_name: 'screenshot-app',
          tier: 'build',
          live_url: 'https://ss.up.railway.app',
          gallery_description: 'Has screenshots',
          gallery_category: 'SaaS',
          gallery_featured: false,
          created_at: new Date(),
          screenshot_meta: [
            { page: '/login', idx: 0, v: 'aaaaaaaa' },
            { page: '/dashboard', idx: 1, v: 'bbbbbbbb' },
          ],
        },
      ],
    });
    void fakeB64;

    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    // Preview button + modal removed from gallery page (2026-04-05).
    expect(res.text).not.toContain('openGalleryPreview');
    expect(res.text).not.toContain('gallery-preview-modal');
    // Harbor v2 (2026-04-27): visit copy is "Visit the website".
    expect(res.text).toContain('Visit the website');
  });

  // 2026-05-17: cache-bust the /gallery/screenshots/:id/:i URL via a
   // bytes-derived ?v=<hash> token so admin re-uploads of the same index
   // invalidate the 24h browser cache.
  it('appends ?v=<hash> token derived from screenshot bytes', async () => {
    const dataA = Buffer.from('bytes-A').toString('base64');
    const dataB = Buffer.from('bytes-B').toString('base64');
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-cb',
          app_name: 'cache-bust-app',
          tier: 'build',
          live_url: 'https://cb.up.railway.app',
          gallery_description: 'cb',
          gallery_category: null,
          gallery_featured: false,
          created_at: new Date(),
          // Version tokens are md5(data) prefixes computed by the query.
          screenshot_meta: [
            { page: '/', idx: 0, v: crypto.createHash('md5').update(dataA).digest('hex').slice(0, 8) },
            { page: '/admin', idx: 1, v: crypto.createHash('md5').update(dataB).digest('hex').slice(0, 8) },
          ],
        },
      ],
    });

    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    // Both URLs carry a ?v= token (8 hex chars)
    const matches = res.text.match(
      /\/gallery\/screenshots\/build-cb\/[01]\?v=[0-9a-f]{8}/g
    );
    expect(matches).not.toBeNull();
    expect(matches!.length).toBeGreaterThanOrEqual(2);
    // Distinct bytes → distinct version tokens
    const v0 = res.text.match(/\/gallery\/screenshots\/build-cb\/0\?v=([0-9a-f]{8})/);
    const v1 = res.text.match(/\/gallery\/screenshots\/build-cb\/1\?v=([0-9a-f]{8})/);
    expect(v0).not.toBeNull();
    expect(v1).not.toBeNull();
    expect(v0![1]).not.toBe(v1![1]);
  });

  it('version token is stable for the same bytes (cache-friendly)', async () => {
    const data = Buffer.from('stable-bytes').toString('base64');
    const row = {
      id: 'build-stable',
      app_name: 'stable-app',
      tier: 'build',
      live_url: 'https://stable.up.railway.app',
      gallery_description: 'x',
      gallery_category: null,
      gallery_featured: false,
      created_at: new Date(),
      screenshot_meta: [{ page: '/', idx: 0, v: crypto.createHash('md5').update(data).digest('hex').slice(0, 8) }],
    };
    mockQuery.mockResolvedValueOnce({ rows: [row] });
    const res1 = await request(app).get('/gallery');
    mockQuery.mockResolvedValueOnce({ rows: [row] });
    const res2 = await request(app).get('/gallery');
    const m1 = res1.text.match(/\/gallery\/screenshots\/build-stable\/0\?v=([0-9a-f]{8})/);
    const m2 = res2.text.match(/\/gallery\/screenshots\/build-stable\/0\?v=([0-9a-f]{8})/);
    expect(m1).not.toBeNull();
    expect(m2).not.toBeNull();
    expect(m1![1]).toBe(m2![1]);
  });

  it('renders a card for a build that has no screenshots', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        {
          id: 'build-no-ss',
          app_name: 'no-screenshots-app',
          tier: 'build',
          live_url: 'https://noss.up.railway.app',
          gallery_description: 'No screenshots',
          gallery_category: null,
          gallery_featured: false,
          created_at: new Date(),
          spec: null,
          screenshots: null,
        },
      ],
    });

    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Visit the website');
  });

  it('filters by category when query param provided', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await request(app).get('/gallery?category=Restaurant');
    expect(res.status).toBe(200);

    // Verify the query was called with the category parameter
    const queryCall = mockQuery.mock.calls[0];
    expect(queryCall[0]).toContain('gallery_category = $1');
    expect(queryCall[1]).toEqual(['Restaurant']);
  });
});

describe('GET /gallery: the link to a customer site', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  const row = (live_url: string | null) => ({
    id: 'build-1',
    app_name: 'sample-studio',
    tier: 'build',
    live_url,
    gallery_description: 'A studio site',
    gallery_category: 'Wellness',
    gallery_featured: false,
    gallery_display_name: null,
    created_at: new Date(),
    spec_description: null,
    screenshot_meta: [],
  });

  it('opens in a new tab with real target and rel attributes (not HTML-escaped quotes)', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [row('https://sample-studio.example/')] });
    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    const link = res.text.match(/<a class="harbor-card__link"[^>]*>/)?.[0] ?? '';
    expect(link).toContain('href="https://sample-studio.example/"');
    expect(link).toContain('target="_blank"');
    expect(link).toContain('rel="noopener noreferrer"');
    expect(link).not.toContain('&quot;');
    // aria-label uses the display name, which is how humans read "sample-studio".
    expect(link).toContain('aria-label="Visit Sample Studio"');
  });

  it('renders "#" instead of a live_url that is not http(s)', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [row('javascript:alert(document.domain)')] });
    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    const link = res.text.match(/<a class="harbor-card__link"[^>]*>/)?.[0] ?? '';
    expect(link).toContain('href="#"');
    expect(res.text).not.toContain('javascript:alert');
  });
});

describe('displayName utility', () => {
  // Import directly to test
  let displayName: (slug: string) => string;
  beforeEach(async () => {
    const mod = await import('../../src/utils/format');
    displayName = mod.displayName;
  });

  it('converts hyphenated slugs to title case', () => {
    expect(displayName('neighborhood-cms')).toBe('Neighborhood CMS');
    expect(displayName('equipment-rental-tracker')).toBe('Equipment Rental Tracker');
    expect(displayName('craft-brewery-manager')).toBe('Craft Brewery Manager');
  });

  it('handles common acronyms', () => {
    expect(displayName('my-crm')).toBe('My CRM');
    expect(displayName('api-gateway')).toBe('API Gateway');
    expect(displayName('hr-portal')).toBe('HR Portal');
  });

  it('handles single word slugs', () => {
    expect(displayName('taskmaster')).toBe('Taskmaster');
  });

  it('handles empty strings', () => {
    expect(displayName('')).toBe('');
  });

  it('handles leading/trailing hyphens', () => {
    expect(displayName('-foo-bar-')).toBe('Foo Bar');
  });
});

describe('GET /gallery/screenshots/:buildId/:index', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 404 for non-gallery builds', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const res = await request(app).get('/gallery/screenshots/non-existent/0');
    expect(res.status).toBe(404);
    // "Non-gallery" is decided by the SQL: only live, opted-in builds match.
    const [sql, params] = mockQuery.mock.calls[0];
    expect(sql).toContain("status = 'live'");
    expect(sql).toContain('gallery_opted_in = TRUE');
    expect(params).toEqual(['non-existent']);
  });

  it('returns 404 for out-of-range index', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [{ screenshots: [] }] });
    const res = await request(app).get('/gallery/screenshots/build-1/5');
    expect(res.status).toBe(404);
  });

  it('serves screenshot as PNG for gallery builds', async () => {
    const fakeB64 = Buffer.from('fakepng').toString('base64');
    mockQuery.mockResolvedValueOnce({
      rows: [{ screenshots: [{ data: fakeB64, filename: 'test.png' }] }],
    });
    const res = await request(app).get('/gallery/screenshots/build-1/0');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toBe('image/png');
  });
});

describe('POST /builds/:buildId/gallery-optin', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('requires auth (redirects to login)', async () => {
    const res = await request(app)
      .post('/builds/build-1/gallery-optin')
      .type('form')
      .send({ _csrf: 'fake', gallery_opted_in: 'true' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toMatch(/\/login/);
  });

  it('toggles opt-in for authenticated user and redirects to dashboard', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-1' }] });

    const res = await agent
      .post('/builds/build-1/gallery-optin')
      .type('form')
      .send({ _csrf: csrf, gallery_opted_in: 'true' });

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/dashboard');
    // The flag written is the one submitted, for this build and this customer only.
    const update = mockQuery.mock.calls.find(([sql]) => /UPDATE builds/.test(String(sql)));
    expect(update).toBeDefined();
    expect(String(update![0])).toContain('gallery_opted_in = $1');
    expect(update![1][0]).toBe(true);
    expect(update![1][3]).toBe('build-1');
  });

  it('returns 404 for non-owned build', async () => {
    const agent = await makeAuthenticatedAgent();
    const csrf = getValidCsrf();
    mockQuery.mockResolvedValueOnce({ rows: [] });

    const res = await agent
      .post('/builds/other-build/gallery-optin')
      .type('form')
      .send({ _csrf: csrf, gallery_opted_in: 'true' });

    expect(res.status).toBe(404);
    // Ownership is the SQL predicate: the session's customer id must be bound to customer_id.
    const update = mockQuery.mock.calls.find(([sql]) => /UPDATE builds/.test(String(sql)));
    expect(update).toBeDefined();
    expect(String(update![0])).toMatch(/WHERE id = \$4 AND customer_id = \$5/);
    expect(update![1][3]).toBe('other-build');
    expect(update![1][4]).toBe('customer-abc');
  });
});


describe('GET /gallery: thumbnail and carousel selection', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  const base = {
    id: 'build-t',
    app_name: 'thumb-site',
    tier: 'build',
    live_url: 'https://t.example.test',
    gallery_description: 'd',
    gallery_category: null,
    gallery_featured: false,
    created_at: new Date(),
    spec_description: null,
  };

  it('keeps each screenshot at its stored index even when earlier entries have no image data', async () => {
    // Index 0 had no data and is absent from the query's output; /gallery/screenshots/:id/:idx
    // reads the stored array, so the URL must keep index 1 and 2.
    mockQuery.mockResolvedValueOnce({
      rows: [{
        ...base,
        screenshot_meta: [
          { page: '/login', idx: 1, v: '11111111' },
          { page: '/services', idx: 2, v: '22222222' },
        ],
      }],
    });
    const res = await request(app).get('/gallery');
    expect(res.text).toContain('/gallery/screenshots/build-t/1?v=11111111');
    expect(res.text).toContain('/gallery/screenshots/build-t/2?v=22222222');
    expect(res.text).not.toContain('/gallery/screenshots/build-t/0?');
  });

  it('survives a spec whose description is not text, and a row with no screenshot_meta at all', async () => {
    mockQuery.mockResolvedValueOnce({
      rows: [
        { ...base, id: 'a', spec_description: null, screenshot_meta: null },
        { ...base, id: 'b', app_name: 'second-site', screenshot_meta: undefined },
      ],
    });
    const res = await request(app).get('/gallery');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Thumb Site');
    expect(res.text).toContain('Second Site');
  });
});

describe('POST /builds/:buildId/gallery-optin: bounded text', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('truncates an oversized description and category to their column sizes', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-1' }] });
    await agent
      .post('/builds/build-1/gallery-optin')
      .type('form')
      .send({ _csrf: getValidCsrf(), gallery_opted_in: 'true', gallery_description: 'd'.repeat(900), gallery_category: 'c'.repeat(300) });
    const update = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('UPDATE builds'));
    const params = update![1] as unknown[];
    expect((params[1] as string).length).toBe(500);
    expect((params[2] as string).length).toBe(100);
  });

  it('ignores array-valued fields instead of passing them to Postgres', async () => {
    const agent = await makeAuthenticatedAgent();
    mockQuery.mockResolvedValueOnce({ rows: [{ id: 'build-1' }] });
    const res = await agent
      .post('/builds/build-1/gallery-optin')
      .type('form')
      .send('_csrf=' + encodeURIComponent(getValidCsrf()) + '&gallery_opted_in=true&gallery_description[]=a&gallery_category[x]=b');
    expect(res.status).toBe(302);
    const update = mockQuery.mock.calls.find((c: unknown[]) => String(c[0]).includes('UPDATE builds'));
    const params = update![1] as unknown[];
    expect(params[1]).toBeNull();
    expect(params[2]).toBeNull();
  });
});
