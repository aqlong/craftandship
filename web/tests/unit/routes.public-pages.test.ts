import { describe, it, expect, vi, beforeEach } from 'vitest';
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

// ─── Landing Page ────────────────────────────────────────────────

describe('GET / (landing page)', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 200 with no auth required', async () => {
    const res = await request(app).get('/');
    expect(res.status).toBe(200);
  });

  it('renders the hero overlay headline + the warm-paper sub-headline', async () => {
    const res = await request(app).get('/');
    // Harbor v2 (2026-04-27): the painting carries the moment + a three-beat
    // overlay headline + an italic-clay "sounds like you" sub-headline on
    // warm paper below.
    expect(res.text).toContain('Your website.');
    expect(res.text).toContain('Your companion.');
    expect(res.text).toContain('Made with you.');
    expect(res.text).toContain('A website that');
    expect(res.text).toContain('sounds like you.');
  });

  it('renders the hero CTA buttons + audience eyebrow', async () => {
    const res = await request(app).get('/');
    // 2026-04-28: secondary CTA repointed to /about/ocean ("Who is Ocean?")
    // because (a) /the-companion is a dead link, (b) early feedback asked
    // the home page to explain who Ocean is. Primary CTA still goes to /try.
    expect(res.text).toContain('Begin a conversation');
    expect(res.text).toContain('Who is Ocean?');
    expect(res.text).toContain('href="/try"');
    expect(res.text).toContain('href="/about/ocean"');
    expect(res.text).toContain('For healers, therapists, and event producers');
  });

  it('renders the How it works three-step section', async () => {
    const res = await request(app).get('/');
    // Harbor v2: three steps replace the v1 four-stage framing. No mention of
    // tiers, no mention of "1 to 2 business days" tier-language; the review
    // SLA stays in the proof strip.
    expect(res.text).toContain('How it works');
    expect(res.text).toContain('You begin a conversation');
    expect(res.text).toContain('We make it together');
    expect(res.text).toContain("It's yours, kept tidy");
  });

  it('renders the FAQ section with affirmative questions only', async () => {
    const res = await request(app).get('/');
    // Harbor v2 (rev 2026-04-27): FAQ phrased affirmatively. The original
    // "Is this a template?" framed by what we are NOT was retired with the
    // "minimal is confidence" / no-listing-what-we're-not direction.
    // EJS-escapes apostrophes to `&#39;`, so we match the encoded form.
    expect(res.text).toContain('Frequently asked');
    expect(res.text).toContain('A few');
    expect(res.text).toMatch(/What(?:&#39;|')s included\?/);
    expect(res.text).toContain('How is each website made?');
    expect(res.text).toContain('What if I want to leave?');
    // Negative-framed questions retired.
    expect(res.text).not.toContain('Is this a template?');
  });

  it('renders the unified subscription pricing card', async () => {
    const res = await request(app).get('/');
    // 2026-04-28: pricing copy unified to "Choose any amount from $39
    // down to $19, whatever fits your practice." Drop "sliding to fit"
    // jargon per founder direction.
    expect(res.text).toContain('$39');
    expect(res.text).toContain('$19');
    expect(res.text).toContain('whatever fits your practice');
    expect(res.text).toContain('You pick what fits');
    expect(res.text).toContain("What's included");
  });

  it('does not render fabricated recent-websites cards', async () => {
    const res = await request(app).get('/');
    // Fabricated "recently launched" section removed 2026-05-05 for content integrity.
    expect(res.text).not.toContain('recently launched');
    expect(res.text).not.toContain('A somatic experiencing practice');
    expect(res.text).not.toContain('A breathwork facilitator');
    expect(res.text).not.toContain('Three websites launched this week');
  });

  it('does not render dead card-feature variant classes', async () => {
    const res = await request(app).get('/');
    expect(res.text).not.toContain('card-feature-violet');
    expect(res.text).not.toContain('card-feature-cyan');
  });

  it('reads nothing from the database: it renders the same with the database down, and issues no query', async () => {
    // The page is static copy. (It used to run two queries on every request,
    // one of them selecting whole screenshot blobs, for data its template
    // never printed.)
    mockQuery.mockRejectedValue(new Error('DB down'));
    const res = await request(app).get('/');
    expect(res.status).toBe(200);
    expect(res.text).toContain('$39');
    expect(res.text).toContain("What's included");
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('does not name competitors or use anti-subscription idioms (Harbor v2 voice)', async () => {
    const res = await request(app).get('/');
    // Brand-voice rule 2 (Harbor v2): no competitor framing on customer
    // surfaces. This is a regression guard against future drift.
    expect(res.text).not.toMatch(/\bMindbody\b/);
    expect(res.text).not.toMatch(/\bAcuity\b/);
    expect(res.text).not.toMatch(/\bSquarespace\b/);
    expect(res.text).not.toMatch(/\bcancel anytime\b/i);
    expect(res.text).not.toMatch(/\bswitching from\b/i);
  });

  it('does not lead with technical-ownership marketing copy (Harbor v2 voice)', async () => {
    const res = await request(app).get('/');
    // Brand-voice rule 3 (Harbor v2): healers don't shop for source code.
    // Soft ownership reassurance ("If you ever leave, we hand the whole site
    // over intact") is fine in FAQ; the source-code/GitHub/self-host phrases
    // do not lead.
    expect(res.text).not.toMatch(/\bsource code\b/i);
    expect(res.text).not.toMatch(/\bGitHub\b/i);
    expect(res.text).not.toMatch(/\bself-host\b/i);
    expect(res.text).not.toMatch(/\bDockerfile\b/i);
  });
});

// ─── Pricing Page ────────────────────────────────────────────────

describe('GET /pricing', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('returns 200 with no auth required', async () => {
    const res = await request(app).get('/pricing');
    expect(res.status).toBe(200);
  });

  it('renders the unified subscription headline', async () => {
    const res = await request(app).get('/pricing');
    // 2026-04-28: headline reframed to "Choose any amount from $39 down
    // to $19, whatever fits your practice." Drop "Sliding to" jargon.
    expect(res.text).toContain('$39');
    expect(res.text).toContain('$19');
    expect(res.text).toContain('a month');
    expect(res.text).toContain('whatever fits your practice');
  });

  it('renders the what-is-included card', async () => {
    const res = await request(app).get('/pricing');
    expect(res.text).toContain('Begin a conversation');
    expect(res.text).toMatch(/Hosting and care/);
    expect(res.text).toMatch(/AI companion/i);
    expect(res.text).toContain('Unlimited revisions and rebuilds');
  });

  it('renders the pricing FAQ', async () => {
    const res = await request(app).get('/pricing');
    expect(res.text).toContain('Frequently asked');
    // 2026-04-28: question reframed from "How does the sliding scale work?"
    // to "How does the price work?". Plus a new "Who is Ocean?" entry.
    expect(res.text).toContain('How does the price work?');
    expect(res.text).toContain('Who is Ocean?');
    expect(res.text).toContain('What if I want to leave?');
  });

  it('does not list the multi-tier pricing structure (Harbor v2 retired)', async () => {
    const res = await request(app).get('/pricing');
    // The legacy Build / Grow / Scale tier cards + $29/$49/$79 hosting tiers
    // are gone. Single subscription only.
    expect(res.text).not.toContain('$29/mo');
    expect(res.text).not.toContain('$49/mo');
    expect(res.text).not.toContain('$79/mo');
    expect(res.text).not.toContain('Try it free');
    expect(res.text).not.toContain('card-feature-violet');
  });
});

// ─── CSS integrity ───────────────────────────────────────────────

describe('CSS design system integrity', () => {
  it('serves style.css', async () => {
    const res = await request(app).get('/style.css');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toContain('text/css');
  });

  it('style.css contains card-feature base class', async () => {
    const res = await request(app).get('/style.css');
    expect(res.text).toContain('card-feature');
  });

  it('style.css does not contain card-feature::before pseudo-element', async () => {
    const res = await request(app).get('/style.css');
    // The minified CSS should not have ::before content for card-feature
    // After removing, there should be no gradient line pseudo-element
    expect(res.text).not.toContain('card-feature-violet');
    expect(res.text).not.toContain('card-feature-cyan');
  });

  it('style.css contains text-gradient-vivid utility', async () => {
    const res = await request(app).get('/style.css');
    expect(res.text).toContain('text-gradient-vivid');
  });

  it('style.css contains reveal-item animation class', async () => {
    const res = await request(app).get('/style.css');
    expect(res.text).toContain('reveal-item');
    expect(res.text).toContain('revealed');
  });

  it('body has overflow-x hidden to prevent horizontal scroll', async () => {
    const res = await request(app).get('/style.css');
    expect(res.text).toContain('overflow-x:hidden');
  });
});

// ─── Other public pages ──────────────────────────────────────────

describe('other public pages', () => {
  it('GET /login returns 200', async () => {
    const res = await request(app).get('/login');
    expect(res.status).toBe(200);
    // Harbor v2 (2026-04-27): "Sign in." replaces "Log In."
    expect(res.text).toContain('Sign in.');
  });

  it('GET /signup returns 200', async () => {
    const res = await request(app).get('/signup');
    expect(res.status).toBe(200);
    // Harbor v2: "Begin together." replaces "Create your account."
    expect(res.text).toContain('Begin together.');
  });

  it('GET /terms returns 200', async () => {
    const res = await request(app).get('/terms');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Terms');
  });

  it('GET /privacy returns 200', async () => {
    const res = await request(app).get('/privacy');
    expect(res.status).toBe(200);
    expect(res.text).toContain('Privacy');
  });

  it('all public pages have the brand wordmark + a footer', async () => {
    // Harbor v2 (2026-04-27): the home page uses layout-harbor.ejs (no
    // id="main-nav"; uses <header class="nav">). Other public pages still
    // use the legacy layout. Both layouts keep the brand wordmark + footer.
    const pages = ['/', '/pricing', '/login', '/signup', '/terms', '/privacy', '/how-it-works'];
    for (const page of pages) {
      const res = await request(app).get(page);
      expect(res.status).toBe(200);
      expect(res.text).toContain('Craft &amp; Ship');
      expect(res.text).toContain('footer');
    }
  });

  it('the Harbor v2 customer pages return 200', async () => {
    // Harbor v2 (2026-04-27): /the-companion + /about ship in this branch.
    // /begin was retired in favor of the dynamic /try anonymous Haven intake.
    const pages = ['/the-companion', '/about'];
    for (const page of pages) {
      const res = await request(app).get(page);
      expect(res.status).toBe(200);
      expect(res.text).toContain('Craft &amp; Ship');
    }
  });

  it('GET /begin redirects to /try', async () => {
    const res = await request(app).get('/begin');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/try');
  });

  it('all public pages include CSRF meta tag', async () => {
    mockQuery.mockResolvedValueOnce({ rows: [] });
    const pages = ['/', '/pricing', '/login', '/signup'];
    for (const page of pages) {
      const res = await request(app).get(page);
      expect(res.text).toContain('csrf-token');
    }
  });

  it('login page renders the email input', async () => {
    // Redesigned login is an editorial split-panel without the old card-feature
    // wrapper. Assert the semantic email field instead.
    const res = await request(app).get('/login');
    expect(res.text).toContain('name="email"');
  });

  it('signup page renders the email input', async () => {
    // Redesigned signup dropped the card-feature wrapper in favor of an
    // editorial split-panel. Assert the semantic email field instead.
    const res = await request(app).get('/signup');
    expect(res.text).toContain('name="email"');
  });
});

// ─── How It Works Page ──────────────────────────────────────────

describe('GET /how-it-works', () => {
  it('returns 200 with no auth required', async () => {
    const res = await request(app).get('/how-it-works');
    expect(res.status).toBe(200);
  });

  it('renders the page title', async () => {
    const res = await request(app).get('/how-it-works');
    // Redesigned page uses sentence case: "How it works".
    expect(res.text).toContain('How it works');
  });

  it('renders the three Harbor steps', async () => {
    const res = await request(app).get('/how-it-works');
    // Harbor v2 (2026-04-27): three-step explanation replaces the old
    // tech-stack / build-pipeline / security sections (those drifted to the
    // engineering register; Harbor copy stays plain for healers).
    expect(res.text).toContain('You begin a conversation');
    expect(res.text).toContain('We make it together');
    expect(res.text).toContain("It's yours, kept tidy");
  });

  it('introduces Haven on the page', async () => {
    const res = await request(app).get('/how-it-works');
    expect(res.text).toContain('Meet Haven');
  });

  it('renders the begin-a-conversation CTA', async () => {
    const res = await request(app).get('/how-it-works');
    expect(res.text).toContain('Begin a conversation');
    expect(res.text).toContain('href="/try"');
  });

  it('appears in sitemap', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.text).toContain('/how-it-works');
  });
});

// ─── Landing Page: Harbor v2 (2026-04-27) ────────────────────────
//
// The pre-Harbor describe blocks below ("technical transparency features"
// + "TCO calculator section") were retired in the Harbor v2 redesign. The
// retired surfaces:
//   - Live-build-activity widget (no longer rendered on the home page)
//   - Craft Promise pull-quote section (folded into /the-companion + /about)
//   - Anti-subscription competitor reframe ("How does this compare to
//     Bubble or Webflow?"): Harbor v2 rule 2 forbids competitor framing
//   - TCO calculator + ownership-first callout: Harbor v2 retires the
//     comparison table entirely; one $39/mo number does the work
//   - Reveal-item animation script: replaced by static painting
//
// What replaces them lives in the "GET / (landing page)" describe block
// above and in /the-companion, /about, /begin route tests below.

describe('landing page technical transparency features', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('renders the Harbor v2 proof strip with the founder and no review window', async () => {
    const res = await request(app).get('/');
    expect(res.text).not.toMatch(/business day/i);
    expect(res.text).toContain('Ocean Longnion');
    expect(res.text).toContain('Senior Web Craftsman');
    // The strip names who founded the service. It does not claim a named
    // person reviews every site: review is a step the code requires, and who
    // performs it is not something the page can promise.
    expect(res.text).toContain('Founded by:');
    expect(res.text).not.toContain('Reviewed by:');
  });

  it('does not render a fabricated customer testimonial', async () => {
    const res = await request(app).get('/');
    // Fabricated "Dr. M.R." testimonial removed 2026-05-05 for content integrity.
    // Real customer quotes will be added when keepers opt in.
    expect(res.text).not.toContain('Dr. M.R.');
    expect(res.text).not.toContain('From a customer,');
  });
});
