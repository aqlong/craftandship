// Phase 0 SEO surface tests.
//
// Covers:
//  - GET /robots.txt: explicit AI crawler allow-list, admin/api/ops/webhooks disallow,
//    sitemap reference.
//  - GET /sitemap.xml: canonical URL list, lastmod present, no auth pages.
//  - GET /llms.txt: brand description, no defensive language, no shutdown talk,
//    no calendar timetable.
//  - GET /pricing: FAQPage + Organization JSON-LD in head, schema FAQs match
//    visible FAQs (drift guard), canonical link.
//  - GET /about: Organization + Person JSON-LD.
//  - GET /about/ocean: Person schema with sameAs, canonical, page renders.
//
// These tests instantiate the real Express app via supertest. No mocks of
// the JSON-LD layer: we want to exercise the same render path the browser
// hits.

import { describe, it, expect, vi } from 'vitest';
import request from 'supertest';

// Mock the DB pool: these route tests don't touch the database, but the
// shared landing-page render path queries `builds` for the gallery feed,
// so we stub it with empty results to keep the test surface fast and
// hermetic. Same pattern as routes.public-pages.test.ts.
vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn().mockResolvedValue({ rows: [], rowCount: 0 });
  return {
    default: { query: mockQuery, on: vi.fn() },
  };
});

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

import app from '../../src/app';
import { pricingFaqs } from '../../src/services/pricing-faq';

describe('GET /robots.txt', () => {
  it('returns text/plain with 200', async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/plain/);
  });

  it('explicitly allows the major AI crawlers (defensive over wildcard)', async () => {
    const res = await request(app).get('/robots.txt');
    const aiBots = ['GPTBot', 'ChatGPT-User', 'OAI-SearchBot', 'ClaudeBot', 'anthropic-ai', 'PerplexityBot', 'Google-Extended', 'CCBot'];
    for (const bot of aiBots) {
      expect(res.text).toContain(`User-agent: ${bot}`);
    }
  });

  it('disallows admin / api / ops / webhooks under every agent block', async () => {
    const res = await request(app).get('/robots.txt');
    // Each disallow path appears multiple times (once per AI agent block + once for *).
    expect(res.text.match(/Disallow: \/admin\//g)?.length ?? 0).toBeGreaterThanOrEqual(2);
    expect(res.text.match(/Disallow: \/api\//g)?.length ?? 0).toBeGreaterThanOrEqual(2);
    expect(res.text.match(/Disallow: \/ops\//g)?.length ?? 0).toBeGreaterThanOrEqual(2);
    expect(res.text.match(/Disallow: \/webhooks\//g)?.length ?? 0).toBeGreaterThanOrEqual(2);
  });

  it('keeps the wildcard allow for non-AI crawlers (Google, Bing, etc.)', async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.text).toContain('User-agent: *');
  });

  it('references the sitemap at the canonical URL', async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.text).toContain('Sitemap: https://craftandship.io/sitemap.xml');
  });
});

describe('GET /sitemap.xml', () => {
  it('returns application/xml with 200', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/xml/);
  });

  it('lists the canonical Harbor public pages', async () => {
    const res = await request(app).get('/sitemap.xml');
    for (const path of ['/', '/pricing', '/the-companion', '/how-it-works', '/gallery', '/about', '/about/ocean', '/contact', '/terms', '/privacy']) {
      expect(res.text).toContain(`<loc>https://craftandship.io${path}</loc>`);
    }
  });

  it('does NOT include auth pages (low SEO value, clutters the sitemap)', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).not.toContain('/login');
    expect(res.text).not.toContain('/signup');
  });

  it('every <url> entry has a lastmod in YYYY-MM-DD form', async () => {
    const res = await request(app).get('/sitemap.xml');
    // Match every <lastmod>...</lastmod> and assert each is ISO-8601 date-only.
    const lastmods = [...res.text.matchAll(/<lastmod>([^<]+)<\/lastmod>/g)].map(m => m[1]);
    expect(lastmods.length).toBeGreaterThan(0);
    for (const ts of lastmods) {
      expect(ts).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });

  it('is well-formed XML with the sitemap urlset namespace', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).toContain('<?xml version="1.0" encoding="UTF-8"?>');
    expect(res.text).toContain('xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"');
    expect(res.text.trim()).toMatch(/<\/urlset>$/);
  });
});

describe('GET /llms.txt', () => {
  it('returns text/plain with 200', async () => {
    const res = await request(app).get('/llms.txt');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/plain/);
  });

  it('includes the brand description and the canonical price line', async () => {
    const res = await request(app).get('/llms.txt');
    expect(res.text).toContain('Craft & Ship');
    // 2026-04-28: pricing copy unified to "Choose any amount from $39
    // down to $19 a month, whatever fits your practice." Drop sliding /
    // "your word is enough" jargon.
    expect(res.text).toMatch(/\$39 down to \$19 a month/);
    expect(res.text).toContain('whatever fits your practice');
  });

  it('does not seed shutdown / hostage / lock-in language', async () => {
    const res = await request(app).get('/llms.txt');
    expect(res.text).not.toMatch(/\bif we ever (?:close|shut)\b/i);
    expect(res.text).not.toMatch(/\bhostage\b/i);
    expect(res.text).not.toMatch(/\block[- ]?in\b/i);
  });

  it('does not put a calendar timetable on the conversation', async () => {
    const res = await request(app).get('/llms.txt');
    expect(res.text).not.toMatch(/\b7[- ]?day\b|\btrial period\b/i);
  });

  it('does not name competitors (AI engines paraphrase llms.txt into citations)', async () => {
    const res = await request(app).get('/llms.txt');
    expect(res.text).not.toMatch(/\b(Mindbody|Squarespace|Wix|Practice\.do|Lovable|Bolt|Webflow)\b/);
  });

  it('points at the sitemap so AI crawlers can discover the URL list', async () => {
    const res = await request(app).get('/llms.txt');
    expect(res.text).toContain('Sitemap: https://craftandship.io/sitemap.xml');
  });

  it('describes the build accurately: assembled from building blocks, reviewed before it goes live', async () => {
    // The website is put together by ordinary software from a library of
    // tested building blocks, and every website is reviewed before it goes
    // live. Nobody hand-codes each site and the code does not record who
    // reviewed, so the file must not name a builder or a reviewer.
    const res = await request(app).get('/llms.txt');
    expect(res.text).not.toMatch(/designed and built (?:with you )?by Ocean/i);
    expect(res.text).not.toMatch(/Ocean designs and builds/i);
    expect(res.text).not.toMatch(/by hand|handcrafted|Senior Web Craftsman|Ocean,? .{0,40}reviews/i);
    expect(res.text).toContain('library of tested building blocks');
    expect(res.text).toContain('Every website needs a recorded review approval before it goes live');
    // Plain English: the internal engine vocabulary stays out of it.
    expect(res.text).not.toMatch(/\bcompos(?:e|es|ed)\b|\bdeterministic\b/i);
  });
});

describe('GET /pricing: JSON-LD schema parity with visible FAQ', () => {
  it('embeds an application/ld+json script tag in the head', async () => {
    const res = await request(app).get('/pricing');
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>/);
  });

  it('JSON-LD includes Organization + FAQPage in a @graph', async () => {
    const res = await request(app).get('/pricing');
    // Pull the first ld+json block and parse it.
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    expect(m).toBeTruthy();
    const parsed = JSON.parse(m![1]);
    expect(parsed['@graph']).toBeDefined();
    const types = (parsed['@graph'] as Array<{ '@type': string }>).map(n => n['@type']);
    expect(types).toContain('Organization');
    expect(types).toContain('FAQPage');
  });

  it('FAQPage JSON-LD questions exactly match the source pricing FAQ list (no drift)', async () => {
    const res = await request(app).get('/pricing');
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    const parsed = JSON.parse(m![1]);
    const faqNode = (parsed['@graph'] as Array<{ '@type': string; mainEntity?: Array<{ name: string; acceptedAnswer: { text: string } }> }>)
      .find(n => n['@type'] === 'FAQPage');
    expect(faqNode).toBeDefined();
    const schemaQuestions = faqNode!.mainEntity!.map(q => q.name);
    const expected = pricingFaqs({ subscription: '$39', sliding: '$19' }).map(f => f.question);
    expect(schemaQuestions).toEqual(expected);
  });

  it('FAQPage answers exactly match the source pricing FAQ list (no drift)', async () => {
    const res = await request(app).get('/pricing');
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    const parsed = JSON.parse(m![1]);
    const faqNode = (parsed['@graph'] as Array<{ '@type': string; mainEntity?: Array<{ name: string; acceptedAnswer: { text: string } }> }>)
      .find(n => n['@type'] === 'FAQPage');
    const schemaAnswers = faqNode!.mainEntity!.map(q => q.acceptedAnswer.text);
    const expected = pricingFaqs({ subscription: '$39', sliding: '$19' }).map(f => f.answer);
    expect(schemaAnswers).toEqual(expected);
  });

  it('emits canonical link to /pricing', async () => {
    const res = await request(app).get('/pricing');
    expect(res.text).toMatch(/<link rel="canonical" href="https:\/\/craftandship\.io\/pricing"/);
  });

  it('visible FAQ accordion renders every FAQ from the source list (HTML side of the parity guard)', async () => {
    const res = await request(app).get('/pricing');
    const expected = pricingFaqs({ subscription: '$39', sliding: '$19' });
    for (const faq of expected) {
      // The visible HTML must contain each Q+A. We use `toContain` rather
      // than DOM parsing for speed: it's enough to know the text is
      // present in the response body.
      expect(res.text).toContain(faq.question);
      // Trim the answer at 60 chars to dodge any HTML-entity differences
      // for apostrophes / curly quotes.
      expect(res.text).toContain(faq.answer.slice(0, 60));
    }
  });
});

describe('GET /about: JSON-LD ships Organization + Person', () => {
  it('returns 200 and embeds JSON-LD', async () => {
    const res = await request(app).get('/about');
    expect(res.status).toBe(200);
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    expect(m).toBeTruthy();
    const parsed = JSON.parse(m![1]);
    const types = (parsed['@graph'] as Array<{ '@type': string }>).map(n => n['@type']);
    expect(types).toContain('Organization');
    expect(types).toContain('Person');
  });
});

describe('GET /about/ocean', () => {
  it('returns 200 and renders Ocean\'s name + role', async () => {
    const res = await request(app).get('/about/ocean');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Ocean Longnion');
    // The founder page carries name and role only, no work history.
    expect(res.text).toContain('Senior Web Craftsman');
  });

  it('embeds Person JSON-LD with the canonical @id', async () => {
    const res = await request(app).get('/about/ocean');
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    expect(m).toBeTruthy();
    const parsed = JSON.parse(m![1]);
    const personNode = (parsed['@graph'] as Array<{ '@type': string; '@id'?: string; name?: string }>)
      .find(n => n['@type'] === 'Person');
    expect(personNode).toBeDefined();
    expect(personNode!['@id']).toBe('https://craftandship.io/about/ocean#person');
    expect(personNode!.name).toBe('Ocean Longnion');
  });

  it('Person JSON-LD carries no work history (no alumniOf)', async () => {
    const res = await request(app).get('/about/ocean');
    const m = res.text.match(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>([\s\S]*?)<\/script>/);
    const parsed = JSON.parse(m![1]);
    const personNode = (parsed['@graph'] as Array<{ '@type': string; alumniOf?: unknown }>)
      .find(n => n['@type'] === 'Person');
    expect(personNode).toBeDefined();
    expect(personNode!.alumniOf).toBeUndefined();
  });

  it('emits canonical link to /about/ocean', async () => {
    const res = await request(app).get('/about/ocean');
    expect(res.text).toMatch(/<link rel="canonical" href="https:\/\/craftandship\.io\/about\/ocean"/);
  });

  it('appears in the sitemap', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).toContain('<loc>https://craftandship.io/about/ocean</loc>');
  });

  it('body copy stays affirmative: no shutdown talk, no defensive framing', async () => {
    const res = await request(app).get('/about/ocean');
    // Strip the JSON-LD script tag so we only check visible body. The
    // schema description is also affirmative (asserted in unit tests),
    // but we want to guard against a future drift in the EJS view.
    const body = res.text.replace(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>[\s\S]*?<\/script>/g, '');
    expect(body).not.toMatch(/\bif we ever (?:close|shut)\b/i);
    expect(body).not.toMatch(/\bhostage\b/i);
    expect(body).not.toMatch(/\block[- ]?in\b/i);
    expect(body).not.toMatch(/\b7[- ]day\b|\btrial period\b/i);
  });
});

describe('GET /: baseline Organization JSON-LD on every Harbor surface', () => {
  it('home page ships at least the default Organization schema', async () => {
    const res = await request(app).get('/');
    expect(res.text).toMatch(/<script type="application\/ld\+json"(?: nonce="[A-Za-z0-9+/=]+")?>/);
    expect(res.text).toMatch(/"@type":"Organization"/);
  });
});

describe('Page <title> tag ships the brand suffix exactly once (no double-suffix)', () => {
  // The two layouts (layout.ejs + layout-harbor.ejs) append " | Craft & Ship"
  // to <%= title %>. Any per-route title that bakes in " - Craft & Ship"
  // produces a doubly-suffixed <title> like "Pricing - Craft & Ship | Craft &
  // Ship", which truncates in SERPs and dilutes brand. Guard each public
  // surface's rendered <title>.
  const surfaces: Array<[string, string]> = [
    ['/', 'home'],
    ['/about', 'about'],
    ['/about/ocean', 'about/ocean'],
    ['/pricing', 'pricing'],
    ['/how-it-works', 'how-it-works'],
    ['/contact', 'contact'],
    ['/docs/research', 'docs/research'],
  ];
  for (const [path, label] of surfaces) {
    it(`${label} renders " | Craft & Ship" exactly once and never " - Craft & Ship"`, async () => {
      const res = await request(app).get(path);
      expect(res.status).toBe(200);
      const m = res.text.match(/<title>([\s\S]*?)<\/title>/);
      expect(m).toBeTruthy();
      const titleHtml = m![1];
      const titleText = titleHtml.replace(/&amp;/g, '&');
      const pipeMatches = titleText.match(/ \| Craft & Ship/g) || [];
      expect(pipeMatches.length).toBe(1);
      expect(titleText).not.toMatch(/ - Craft & Ship/);
      // The brand appears once: a title that names it ("Craft & Ship - ...",
      // "About Craft & Ship") would say it twice with the suffix.
      expect((titleText.match(/Craft & Ship/g) || []).length).toBe(1);
    });
  }
});

describe('OG meta tags emit unencoded ampersands (Facebook/iMessage/Slack/LinkedIn parsers)', () => {
  // Several OG-card scrapers (Facebook, iMessage, Slack, LinkedIn) display
  // entity-encoded ampersands literally instead of decoding them, so social
  // shares would render "Craft &amp; Ship" instead of "Craft & Ship". The
  // layouts must emit a raw "&" in og:title / og:description / og:site_name
  // content attributes. Same for twitter equivalents that share the layout.
  const surfaces: Array<[string, string]> = [
    ['/', 'home'],
    ['/about', 'about'],
    ['/about/ocean', 'about/ocean'],
    ['/pricing', 'pricing'],
  ];
  for (const [path, label] of surfaces) {
    it(`${label} og:site_name renders "Craft & Ship" (raw ampersand)`, async () => {
      const res = await request(app).get(path);
      expect(res.status).toBe(200);
      const m = res.text.match(/<meta property="og:site_name" content="([^"]*)"/);
      expect(m).toBeTruthy();
      expect(m![1]).toBe('Craft & Ship');
      expect(m![1]).not.toContain('&amp;');
    });

    it(`${label} og:title content attr does not contain "&amp;" entity`, async () => {
      const res = await request(app).get(path);
      expect(res.status).toBe(200);
      const m = res.text.match(/<meta property="og:title" content="([^"]*)"/);
      expect(m).toBeTruthy();
      expect(m![1]).not.toContain('&amp;');
    });

    it(`${label} og:description content attr does not contain "&amp;" entity`, async () => {
      const res = await request(app).get(path);
      expect(res.status).toBe(200);
      const m = res.text.match(/<meta property="og:description" content="([^"]*)"/);
      expect(m).toBeTruthy();
      expect(m![1]).not.toContain('&amp;');
    });
  }
});

describe('Favicon static surface (Search Console reports 404 if any of these miss)', () => {
  // Browsers that don't honor <link rel="icon" type="image/svg+xml"> still
  // request /favicon.ico unconditionally. Search Console flags the 404 as a
  // technical SEO issue. These four URLs MUST all 200 from express.static.
  it.each([
    ['/favicon.ico', /image\/(x-icon|vnd\.microsoft\.icon)/],
    ['/favicon.svg', /image\/svg\+xml/],
    ['/favicon-16x16.png', /image\/png/],
    ['/favicon-32x32.png', /image\/png/],
  ])('serves %s with the right content-type', async (path, contentTypeRe) => {
    const res = await request(app).get(path);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(contentTypeRe);
  });

  it('home page <head> references favicon.ico for legacy / non-SVG browsers', async () => {
    const res = await request(app).get('/');
    expect(res.text).toMatch(/<link rel="icon"[^>]*href="\/favicon\.ico"/);
  });
});

describe('Per-page OG images (link shares look distinct in social/iMessage/Slack)', () => {
  // Roadmap item seo-per-page-og-images (2026-05-04). Every page used to
  // share /og-default.png; the curated map in services/og-images.ts now
  // assigns a per-page asset to each canonical Harbor public URL. These
  // tests assert the rendered <meta property="og:image"> on each page.
  const pages: Array<[string, string]> = [
    ['/', '/og-default.png'],
    ['/pricing', '/og-pricing.png'],
    ['/the-companion', '/og-companion.png'],
    ['/how-it-works', '/og-how-it-works.png'],
    ['/about', '/og-about.png'],
    ['/about/ocean', '/og-about-ocean.png'],
    ['/contact', '/og-contact.png'],
    ['/terms', '/og-terms.png'],
    ['/privacy', '/og-privacy.png'],
  ];
  for (const [routePath, ogPath] of pages) {
    it(`${routePath} ships og:image=${ogPath}`, async () => {
      const res = await request(app).get(routePath);
      expect(res.status).toBe(200);
      const ogMatch = res.text.match(/<meta property="og:image" content="([^"]+)"/);
      expect(ogMatch).toBeTruthy();
      expect(ogMatch![1]).toBe(`https://craftandship.io${ogPath}`);
    });

    it(`${routePath} ships twitter:image=${ogPath}`, async () => {
      const res = await request(app).get(routePath);
      const twitterMatch = res.text.match(/<meta name="twitter:image" content="([^"]+)"/);
      expect(twitterMatch).toBeTruthy();
      expect(twitterMatch![1]).toBe(`https://craftandship.io${ogPath}`);
    });
  }

  it('an unmapped path falls back to /og-default.png (regression guard)', async () => {
    // /robots.txt etc. don't render HTML, but the 404 handler does. Any
    // public route not in PER_PAGE_OG_IMAGES should still render an
    // og:image, not crash, and not silently emit an empty href.
    const res = await request(app).get('/login');
    expect(res.status).toBe(200);
    const m = res.text.match(/<meta property="og:image" content="([^"]+)"/);
    expect(m).toBeTruthy();
    expect(m![1]).toBe('https://craftandship.io/og-default.png');
  });

  it('every per-page OG asset serves with image/png content-type', async () => {
    const ogPaths = ['/og-default.png', '/og-pricing.png', '/og-companion.png',
      '/og-how-it-works.png', '/og-gallery.png', '/og-about.png',
      '/og-about-ocean.png', '/og-contact.png', '/og-terms.png', '/og-privacy.png'];
    for (const p of ogPaths) {
      const res = await request(app).get(p);
      expect(res.status, `${p} should 200`).toBe(200);
      expect(res.headers['content-type']).toMatch(/image\/png/);
    }
  });
});
