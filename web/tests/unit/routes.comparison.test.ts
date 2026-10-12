// Routes for the SEO comparison surface (/vs hub + /vs/<slug> pages).
//
// Roadmap: seo-content-comparison-pages (2026-05-04). Targets the
// high-intent "alternative to" keyword cluster. The test surface verifies:
//  - hub at /vs renders + 200s
//  - each /vs/<slug> renders + 200s
//  - sitemap includes the new urls
//  - JSON-LD ships Article schema on the page route + CollectionPage on hub
//  - canonical link is present
//  - no banned brand-voice phrases leak into the visible HTML body

import { describe, it, expect, vi } from 'vitest';
import request from 'supertest';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn().mockResolvedValue({ rows: [], rowCount: 0 });
  return { default: { query: mockQuery, on: vi.fn() } };
});
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn(), sendAppLiveEmail: vi.fn(), sendPreviewReadyEmail: vi.fn(),
  sendBuildQueuedEmail: vi.fn(), sendBuildFailedEmail: vi.fn(),
  generateUnsubscribeToken: vi.fn((email: string) => `t-${email}`),
  sendWelcomeHowItWorksEmail: vi.fn(), sendWelcomeUseCasesEmail: vi.fn(),
  sendWelcomeBetaUrgencyEmail: vi.fn(), sendPostBuildCheckInEmail: vi.fn(),
  sendPostBuildTestimonialEmail: vi.fn(),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn(), schedulePostBuildSequence: vi.fn(),
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

import app from '../../src/app';
import { listComparisonPages } from '../../src/data/comparison-pages';

const ALL_SLUGS = listComparisonPages().map((p) => p.slug);

describe('GET /vs (comparison hub)', () => {
  it('returns 200 and renders the hub view', async () => {
    const res = await request(app).get('/vs');
    expect(res.status).toBe(200);
    // Hub headline.
    expect(res.text).toContain('A real alternative to template website builders');
  });

  it('lists every comparison page as a link', async () => {
    const res = await request(app).get('/vs');
    for (const slug of ALL_SLUGS) {
      expect(res.text).toContain(`/vs/${slug}`);
    }
  });

  it('emits a canonical link to /vs', async () => {
    const res = await request(app).get('/vs');
    expect(res.text).toMatch(/<link rel="canonical" href="https:\/\/craftandship\.io\/vs"/);
  });

  it('embeds CollectionPage JSON-LD in a graph with Organization', async () => {
    const res = await request(app).get('/vs');
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    expect(m).toBeTruthy();
    const parsed = JSON.parse(m![1]);
    const types = (parsed['@graph'] as Array<{ '@type': string }>).map((n) => n['@type']);
    expect(types).toContain('Organization');
    expect(types).toContain('CollectionPage');
  });
});

describe('GET /vs/<slug> per-competitor pages', () => {
  it.each(ALL_SLUGS)('serves /vs/%s with 200', async (slug) => {
    const res = await request(app).get(`/vs/${slug}`);
    expect(res.status).toBe(200);
  });

  it.each(ALL_SLUGS)('/vs/%s emits a canonical link to its own URL', async (slug) => {
    const res = await request(app).get(`/vs/${slug}`);
    const re = new RegExp(`<link rel="canonical" href="https://craftandship\\.io/vs/${slug}"`);
    expect(res.text).toMatch(re);
  });

  it.each(ALL_SLUGS)('/vs/%s embeds Article JSON-LD bound to Ocean as author', async (slug) => {
    const res = await request(app).get(`/vs/${slug}`);
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    expect(m).toBeTruthy();
    const parsed = JSON.parse(m![1]);
    const graph = parsed['@graph'] as Array<{ '@type': string; author?: { '@id': string }; '@id'?: string }>;
    const article = graph.find((n) => n['@type'] === 'Article');
    expect(article).toBeDefined();
    expect(article!['@id']).toBe(`https://craftandship.io/vs/${slug}#article`);
    expect(article!.author).toEqual({ '@id': 'https://craftandship.io/about/ocean#person' });
  });

  it.each(ALL_SLUGS)('/vs/%s body stays affirmative (no banned phrases in visible HTML)', async (slug) => {
    const res = await request(app).get(`/vs/${slug}`);
    // Strip JSON-LD script blocks; competitor names legitimately appear there.
    const body = res.text.replace(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>[\s\S]*?<\/script>/g, '');
    expect(body).not.toMatch(/\bif we ever (?:close|shut)\b/i);
    expect(body).not.toMatch(/\bhostage\b/i);
    expect(body).not.toMatch(/\block[- ]?in\b/i);
    expect(body).not.toMatch(/\b7[- ]?days?\b/i);
    expect(body).not.toMatch(/\btrial period\b/i);
  });

  it('returns 404 for an unknown slug', async () => {
    const res = await request(app).get('/vs/not-a-real-competitor');
    expect(res.status).toBe(404);
  });
});

describe('sitemap.xml includes the comparison surface', () => {
  it('lists the /vs hub', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).toContain('<loc>https://craftandship.io/vs</loc>');
  });

  it.each(ALL_SLUGS)('lists /vs/%s', async (slug) => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).toContain(`<loc>https://craftandship.io/vs/${slug}</loc>`);
  });
});
