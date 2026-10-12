import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import request from 'supertest';
import type { Request } from 'express';
import app, { privacyCookieFacts } from '../../src/app';
import { listFamilies, _resetTemplateCacheForTests } from '../../src/services/template-families';
import { pricingFaqs } from '../../src/services/pricing-faq';
import {
  generateCsrfToken,
  SESSION_ABSOLUTE_MAX_MS,
  SESSION_COOKIE_NAME,
  SESSION_IDLE_TIMEOUT_MS,
} from '../../src/middleware/auth';
import { createMintLimiter, resolveTrial, TRIAL_COOKIE_NAME, TRIAL_TTL_DAYS } from '../../src/middleware/trial-cookie';
import { PAID_TRIAL_ACCESS_DAYS } from '../../src/services/keeper_promotion';
import { ACCOUNT_DELETION_LEAVES, ACCOUNT_DELETION_REMOVES } from '../../src/routes/auth';
import {
  MAX_ACTIVE_BUILDS_PER_CUSTOMER,
  MAX_BUILD_STARTS_PER_DAY,
  MAX_MANUAL_RETRIES_PER_BUILD,
} from '../../src/services/build-start-limits';

vi.mock('../../src/db/index', () => {
  const mockQuery = vi.fn();
  return { default: { query: mockQuery, on: vi.fn() } };
});
vi.mock('../../src/services/email', () => ({
  sendWelcomeEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeHowItWorksEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeUseCasesEmail: vi.fn().mockResolvedValue(undefined),
  sendWelcomeBetaUrgencyEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildCheckInEmail: vi.fn().mockResolvedValue(undefined),
  sendPostBuildTestimonialEmail: vi.fn().mockResolvedValue(undefined),
  generateUnsubscribeToken: vi.fn((email: string) => `token-for-${email}`),
}));
vi.mock('../../src/services/email-scheduler', () => ({
  scheduleWelcomeSequence: vi.fn().mockResolvedValue(undefined),
  schedulePostBuildSequence: vi.fn().mockResolvedValue(undefined),
  cancelWelcomeSequence: vi.fn().mockResolvedValue(0),
}));

import pool from '../../src/db/index';
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

const webRoot = path.join(__dirname, '../..');
const read = (rel: string): string => fs.readFileSync(path.join(webRoot, rel), 'utf-8');
// Compare on text, not markup: collapse whitespace and strip tags and EJS tags.
const plain = (rel: string): string =>
  read(rel).replace(/<%[\s\S]*?%>/g, ' ').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

// How a site is made, and what is true of it before it goes live. The site is
// built by software from a library of tested building blocks, matched to what
// the customer told the companion, and it passes a review step before it can be
// marked live. Nothing in the code has a person design, write or hand-code each
// site, and the review was not always a person (an automated review mode
// existed), so the pages must not say either.
const MARKETING_VIEWS = [
  'views/index.ejs',
  'views/about.ejs',
  'views/about-ocean.ejs',
  'views/how-it-works.ejs',
  'views/pricing.ejs',
  'views/guide.ejs',
  'views/guide-read.ejs',
];

const PROCESS_CLAIMS: Array<[RegExp, string]> = [
  [/\bby hand\b/i, '"by hand"'],
  [/hand-?crafted|hand-?built|handmade/i, '"handcrafted"'],
  [/honestly (?:human|his hand)/i, '"honestly human"'],
  [
    /\b(?:Ocean|I)\b[^.<>]{0,60}\b(?:designs?|builds?)\b[^.<>]{0,30}\b(?:every|each|your)\b[^.<>]{0,10}\b(?:site|website)s?\b/i,
    'a person designs and builds each site',
  ],
  [
    /(?:Senior Web Craftsman|\bOcean\b|\bme\b|\bI\b)[^.<>]{0,40}\breviews?\b[^.<>]{0,30}\b(?:build|page|site|website)/i,
    'a named person reviews every site',
  ],
  [/reviewed by (?:me|Ocean|a Senior Web Craftsman)/i, '"reviewed by <a person>"'],
  [/refined (?:over|for)|for a long time|sixty times|\bbuilding websites for\b/i, 'experience the repository cannot support'],
];

describe('marketing pages describe how a site is made as the code does it', () => {
  it('the patterns catch the wording they replaced (control)', () => {
    const samples = [
      'There is no agency behind the curtain; the build is honestly his hand.',
      'By hand, with you. Ocean designs and builds your site from a library of proven building blocks',
      'I design and build every site myself, by hand',
      'A Senior Web Craftsman reviews every build before it goes live; that is me, today.',
      'Every site I build gets reviewed by me personally before it goes live.',
      'Ocean, a Senior Web Craftsman, designs and builds your site from a library of proven building blocks.',
      'proven pieces I\'ve refined over years',
      'I\'ve been building websites for a long time.',
      'I\'ve had some version of the same conversation probably sixty times now.',
      'Why Handcrafted Matters',
    ];
    for (const sample of samples) {
      expect(PROCESS_CLAIMS.some(([pattern]) => pattern.test(sample)), sample).toBe(true);
    }
  });

  it('the patterns leave the replacement wording alone (control)', () => {
    const samples = [
      'Ocean Longnion is a Senior Web Craftsman and the founder of Craft & Ship.',
      'Your site is built from a library of tested building blocks, matched to what you told Haven.',
      'Every website needs a recorded review approval before it goes live.',
      'Write me directly. I read every message.',
    ];
    for (const sample of samples) {
      expect(PROCESS_CLAIMS.some(([pattern]) => pattern.test(sample)), sample).toBe(false);
    }
  });

  for (const file of MARKETING_VIEWS) {
    it(`${file} makes none of those claims`, () => {
      const text = plain(file);
      for (const [pattern, label] of PROCESS_CLAIMS) {
        expect(pattern.test(text), `${file}: ${label}`).toBe(false);
      }
    });
  }

  for (const file of ['views/index.ejs', 'views/about.ejs', 'views/about-ocean.ejs', 'views/how-it-works.ejs']) {
    it(`${file} says the site is built from tested building blocks and needs a recorded review approval before it goes live`, () => {
      const text = plain(file) + read(file); // index keeps its FAQ answers inside a script block
      expect(text).toMatch(/library of tested building blocks/);
      expect(text).toMatch(/needs a recorded review approval before it goes live/);
    });
  }
});

// The same two facts are stated in the places that are not marketing pages:
// the layouts' meta tags and footer, the status and terms pages, the lamp page,
// the JSON-LD, the FAQ, the comparison pages, the gallery description, the
// support corpus, the companion's persona, the intake prompt, the emails and
// the page descriptions in app.ts. What a customer can read there must not
// say that a person builds each site, that a named person personally reviews
// it, or that a review comes before the preview address (the worker emails the
// preview address when the build arrives at preview, and the review follows).
const PERSON_CLAIMS: Array<[RegExp, string]> = [
  ...PROCESS_CLAIMS,
  [/personally (?:reviews?|reviewed|builds?|built|designs?)/i, '"personally reviews"'],
  [/\breal person walks\b|\ba human review\b|\breviewed by a human\b|\bhuman who cares\b|\bread by a human\b/i, 'a human reviews every page'],
  [/Senior Web Craftsman (?:reviews?|reviewed|designs?|builds?|and tester|will read|is reading)/i, 'a Senior Web Craftsman reviews or builds'],
  [/\bco-?created with (?:you and )?a Senior Web Craftsman/i, 'co-created with a Senior Web Craftsman'],
  [/in review with a Senior Web Craftsman|Ocean (?:has|left) (?:notes|suggestions)|Notes from Ocean/i, 'a named reviewer on the status page'],
  [/Ocean[^.<>]{0,20}(?:is giving|gives) it a human review/i, 'Ocean gives it a human review'],
  [/(?:the )?Craft promise/i, '"the Craft promise"'],
];

const ORDER_CLAIMS: Array<[RegExp, string]> = [
  [/before (?:you|the customer) (?:ever )?sees? (?:a|the|your|an?) (?:next )?preview|before the next preview/i, 'review before the preview address'],
  [/read it before you ever see/i, 'review before the preview address'],
];

// Text a customer can read in a file: EJS scriptlets removed, markup kept so
// that attribute values (meta descriptions, alt text) are included.
function copyOf(rel: string, from?: string, to?: string): string {
  let text = read(rel);
  if (from) {
    const start = text.indexOf(from);
    expect(start, `${rel}: marker ${from}`).toBeGreaterThanOrEqual(0);
    text = text.slice(start);
  }
  if (to) {
    const end = text.indexOf(to);
    expect(end, `${rel}: marker ${to}`).toBeGreaterThanOrEqual(0);
    text = text.slice(0, end);
  }
  return text.replace(/<%[\s\S]*?%>/g, ' ').replace(/\\n/g, ' ').replace(/\s+/g, ' ');
}

// [file, first marker, last marker]: the span of the file that is customer copy.
const COPY_SPANS: Array<[string, string?, string?]> = [
  ['views/layout-harbor.ejs'],
  ['views/layout.ejs'],
  ['views/status.ejs'],
  ['views/terms.ejs'],
  ['views/vs-index.ejs'],
  ['views/lighthouse/lamp.ejs'],
  ['views/lighthouse/preview.ejs'],
  ['views/error.ejs'],
  ['src/services/structured-data.ts'],
  ['src/services/pricing-faq.ts'],
  ['src/data/comparison-pages.ts'],
  ['src/services/support-knowledge.ts'],
  ['../coordinator/support_knowledge.py'],
  ['src/companion/persona-sections.json'],
  // The intake prompt only: the rest of claude.ts is code and other prompts.
  ['src/services/claude.ts', 'const SYSTEM_PROMPT', 'export async function processIntakeMessage'],
  // The senders above the two review-gate emails, which are sent when an admin
  // records a verdict and say so (sendReviewReadyEmail, sendReviewChangesRequestedEmail).
  ['src/services/email.ts', undefined, '// Review gate emails'],
  // The llms.txt text and the descriptions the pages hand to the layout.
  ['src/app.ts', "app.get('/llms.txt'", '// Render helper for the landing page'],
  ['src/app.ts', 'function renderIndexPage', '// Legal pages'],
  ['src/routes/gallery.ts', "res.render('gallery'", 'categories,'],
];

describe('the rest of the customer-facing copy makes none of those claims', () => {
  it('the patterns catch the wording they replaced (control)', () => {
    const samples = [
      'Reviewed by Ocean Longnion · Senior Web Craftsman',
      'A Senior Web Craftsman personally reviews every page before the customer sees a preview.',
      'Ocean is giving it a human review before delivery: every route, every page, every edge case.',
      'The review is hands-on: a real person walks every page, every form, every link.',
      'Made by hand. Reviewed by a Senior Web Craftsman.',
      'The build is honestly human.',
      'Every website we deliver is co-created with you and a Senior Web Craftsman who personally reviews every page',
      'In review with a Senior Web Craftsman',
      'The Craft promise is the brand. We will not skip it.',
      'Ocean personally reviews every page before you ever see a preview URL.',
      'When it is ready, a Senior Web Craftsman will read it before you ever see a preview URL.',
      'A Senior Web Craftsman and tester reads every page before it goes live',
    ];
    for (const sample of samples) {
      const hit = PERSON_CLAIMS.some(([pattern]) => pattern.test(sample)) || ORDER_CLAIMS.some(([pattern]) => pattern.test(sample));
      expect(hit, sample).toBe(true);
    }
  });

  it('the patterns leave the replacement wording alone (control)', () => {
    const samples = [
      'Every website needs a recorded review approval before it goes live.',
      'built from a library of tested building blocks',
      'Founded by Ocean Longnion · Senior Web Craftsman',
      'Ocean Longnion is a Senior Web Craftsman and the founder of Craft & Ship.',
      'When it is ready, we will email you its preview address.',
      'We will rebuild it, email you the new preview address, and it will need a recorded review approval again before it goes live.',
    ];
    for (const sample of samples) {
      const hit = PERSON_CLAIMS.some(([pattern]) => pattern.test(sample)) || ORDER_CLAIMS.some(([pattern]) => pattern.test(sample));
      expect(hit, sample).toBe(false);
    }
  });

  for (const [file, from, to] of COPY_SPANS) {
    it(`${file}${from ? ` (${from.slice(0, 28)}...)` : ''} states no person-built, person-reviewed or review-first claim`, () => {
      const text = copyOf(file, from, to);
      expect(text.length).toBeGreaterThan(40);
      for (const [pattern, label] of [...PERSON_CLAIMS, ...ORDER_CLAIMS]) {
        expect(pattern.test(text), `${file}: ${label}`).toBe(false);
      }
    });
  }

  it('says the same thing in every default description (layouts, home page, pricing, about)', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    for (const route of ['/', '/pricing', '/about', '/about/ocean', '/vs', '/vs/wix', '/gallery', '/terms']) {
      const res = await request(app).get(route);
      expect(res.status, route).toBe(200);
      const description = res.text.match(/<meta name="description" content="([^"]*)"/)?.[1] ?? '';
      expect(description.length, route).toBeGreaterThan(40);
      for (const [pattern, label] of [...PERSON_CLAIMS, ...ORDER_CLAIMS]) {
        expect(pattern.test(description), `${route} description: ${label}`).toBe(false);
      }
      for (const tag of ['og:description', 'twitter:description']) {
        const content = res.text.match(new RegExp(`<meta (?:property|name)="${tag}" content="([^"]*)"`))?.[1] ?? '';
        for (const [pattern, label] of [...PERSON_CLAIMS, ...ORDER_CLAIMS]) {
          expect(pattern.test(content), `${route} ${tag}: ${label}`).toBe(false);
        }
      }
    }
  });

  it('the footer names the founder, and does not say the founder reviewed anything', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await request(app).get('/terms');
    expect(res.text).toContain('Founded by Ocean Longnion');
    expect(res.text).not.toMatch(/Reviewed by Ocean Longnion/);
  });

  it('the JSON-LD on every page that ships it parses and carries none of the claims', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    for (const route of ['/', '/pricing', '/about', '/about/ocean', '/vs', '/vs/squarespace']) {
      const res = await request(app).get(route);
      const blocks = [...res.text.matchAll(/<script type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
      expect(blocks.length, route).toBeGreaterThan(0);
      for (const block of blocks) {
        const parsed = JSON.parse(block); // throws if the escaping broke the JSON
        const flat = JSON.stringify(parsed);
        for (const [pattern, label] of [...PERSON_CLAIMS, ...ORDER_CLAIMS]) {
          expect(pattern.test(flat), `${route} JSON-LD: ${label}`).toBe(false);
        }
        expect(flat, route).not.toMatch(/human-reviewed/i);
      }
    }
  });

  it('llms.txt, the JSON-LD and the pricing FAQ state the two settled sentences', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    const llms = (await request(app).get('/llms.txt')).text;
    expect(llms).toContain('Every website needs a recorded review approval before it goes live');
    expect(llms).toContain('library of tested building blocks');
    const pricing = (await request(app).get('/pricing')).text;
    const ld = pricing.match(/<script type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/)?.[1] ?? '';
    const parsed = JSON.parse(ld);
    const flat = JSON.stringify(parsed);
    expect(flat).toContain('Every website needs a recorded review approval before it goes live');
    expect(flat).toContain('library of tested building blocks');
    expect(flat).not.toMatch(/by hand|real person|co-created with a Senior Web Craftsman|reviews every page/i);
  });

  // A turnaround time or a personal reply is a promise the archived service
  // cannot keep. These are the surfaces whose wording this round changed.
  it('the llms.txt, the contact description, the pricing page and the pricing FAQ promise no turnaround and no personal reply', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    const PROMISE = /business day|within 1 to 2|reviewed within|writes back personally|will write back|write back to|answers when you write|reply personally|say so plainly|same day|within a few hours/i;
    expect((await request(app).get('/llms.txt')).text).not.toMatch(PROMISE);
    const contact = (await request(app).get('/contact')).text;
    const contactDescription = contact.match(/<meta name="description" content="([^"]*)"/)?.[1] ?? '';
    expect(contactDescription.length).toBeGreaterThan(40);
    expect(contactDescription).not.toMatch(PROMISE);
    const pricing = (await request(app).get('/pricing')).text;
    const pricingDescription = pricing.match(/<meta name="description" content="([^"]*)"/)?.[1] ?? '';
    expect(pricingDescription).not.toMatch(PROMISE);
    const pricingLd = pricing.match(/<script type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/)?.[1] ?? '';
    expect(JSON.stringify(JSON.parse(pricingLd))).not.toMatch(PROMISE);
    for (const item of pricingFaqs({ subscription: '$39', sliding: '$19' })) {
      expect(`${item.question} ${item.answer}`).not.toMatch(PROMISE);
    }
  });
});

describe('internal vocabulary stays out of what a customer reads', () => {
  // "keeper" is the internal name for a customer (docs/BRAND_VOICE.md, CLAUDE.md rule 7).
  const FILES = ['views/index.ejs', 'views/lighthouse/lamp.ejs', 'views/lighthouse/preview.ejs', 'views/error.ejs'];
  for (const file of FILES) {
    it(`${file} never says "keeper" outside EJS code`, () => {
      expect(copyOf(file)).not.toMatch(/keeper/i);
    });
  }

  it('the companion page title and description do not say it either', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await request(app).get('/the-companion');
    expect(res.status).toBe(200);
    const title = res.text.match(/<title>([^<]*)<\/title>/)?.[1] ?? '';
    const description = res.text.match(/<meta name="description" content="([^"]*)"/)?.[1] ?? '';
    expect(title).toContain('Meet Haven');
    expect(`${title} ${description}`).not.toMatch(/keeper/i);
  });

  it('the home page does not say it in its markup or its image text', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await request(app).get('/');
    expect(res.text).toContain('The Lamp at Dusk');
    expect(res.text).not.toMatch(/Keeper's Lamp|keeper's lighthouse/i);
  });
});

describe('pages do not promise content or messages that nothing produces', () => {
  const promises: Array<[string, RegExp, string]> = [
    ['views/guide.ejs', /quiet stories|letters from friends/i, 'practitioner stories that are not in the guide'],
    ['views/guide.ejs', /I will send it to you|You will hear from me|Unsubscribe at any time/i, 'a message or an unsubscribe that nothing implements'],
    ['views/docs-research.ejs', /our earlier notes|on our landing page|with a footnote/i, 'notes, quotes or footnotes that do not exist'],
  ];
  for (const [file, pattern, label] of promises) {
    it(`${file} has no promise of ${label}`, () => {
      expect(pattern.test(plain(file)), file).toBe(false);
    });
  }

  it('guide.ejs describes Part Five as the guide actually has it', () => {
    // Part Five of /guide/read is prose about what changes, with no stories.
    expect(plain('views/guide-read.ejs')).toMatch(/What Freedom Feels Like/);
    expect(plain('views/guide-read.ejs')).not.toMatch(/\bgr-story\b/);
    expect(plain('views/guide.ejs')).toMatch(/What tends to change once a website feels like yours/);
  });
});

describe('page styles have markup', () => {
  // A style rule for a class that no element carries is the leftover of a
  // section that was removed (the guide's story blocks, the landing page's
  // letter and work-card sections). Classes added to <body> by the layout are
  // the only legitimate exception.
  const LAYOUT_CLASSES = new Set(['harbor']);

  function orphanClasses(file: string): string[] {
    const source = read(file);
    const match = source.match(/<style>([\s\S]*?)<\/style>/);
    if (!match) return [];
    const css = match[1].replace(/\/\*[\s\S]*?\*\//g, '').replace(/\{[^}]*\}/g, '{}');
    const markup = source.replace(match[0], '');
    const classes = new Set([...css.matchAll(/\.([a-zA-Z_][\w-]*)/g)].map((m) => m[1]));
    return [...classes].filter(
      (name) => !LAYOUT_CLASSES.has(name) && !new RegExp(`(?<![\\w-])${name}(?![\\w-])`).test(markup)
    );
  }

  it('the check finds an unused class (control)', () => {
    const css = '.gr-story-name { color: red; }'.replace(/\{[^}]*\}/g, '{}');
    expect([...css.matchAll(/\.([a-zA-Z_][\w-]*)/g)].map((m) => m[1])).toEqual(['gr-story-name']);
  });

  for (const file of ['views/index.ejs', 'views/guide.ejs', 'views/guide-read.ejs', 'views/about.ejs', 'views/about-ocean.ejs', 'views/how-it-works.ejs', 'views/pricing.ejs']) {
    it(`${file} styles only classes it uses`, () => {
      expect(orphanClasses(file), file).toEqual([]);
    });
  }
});

describe('a refused request gets a page that says so', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
  });

  it('a form post without a valid token is a 403 page, not the 500 page', async () => {
    const res = await request(app).post('/guide').type('form').send({ email: 'person@example.com' });
    expect(res.status).toBe(403);
    expect(res.text).toMatch(/>\s*403\s*</);
    expect(res.text).toContain('That request was not allowed.');
    expect(res.text).toContain('could not be verified');
    expect(res.text).not.toContain('Something went sideways');
    expect(res.text).not.toMatch(/>\s*500\s*</);
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('a missing page is a 404 page', async () => {
    const res = await request(app).get('/this-page-does-not-exist');
    expect(res.status).toBe(404);
    expect(res.text).toContain('That page is missing.');
    expect(res.text).not.toContain('Something went sideways');
  });
});

// The privacy policy and the terms state facts about what this application
// does. Each fact below is read from the constant the code itself uses (or,
// for the cookies, from the cookie the application really sets), and the page
// has to say the same.
describe('the privacy page states the cookies as the code sets them', () => {
  const HOUR_MS = 60 * 60 * 1000;
  const DAY_MS = 24 * HOUR_MS;
  const rendered = async (route: string): Promise<string> => {
    mockQuery.mockResolvedValue({ rows: [] });
    const res = await request(app).get(route);
    expect(res.status).toBe(200);
    return res.text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
  };

  it('the facts handed to the page are the constants, in the units the page prints', () => {
    expect(privacyCookieFacts()).toEqual({
      sessionCookieName: SESSION_COOKIE_NAME,
      sessionIdleHours: SESSION_IDLE_TIMEOUT_MS / HOUR_MS,
      sessionMaxDays: SESSION_ABSOLUTE_MAX_MS / DAY_MS,
      conversationCookieName: TRIAL_COOKIE_NAME,
      conversationDays: TRIAL_TTL_DAYS,
      conversationPaidDays: PAID_TRIAL_ACCESS_DAYS,
    });
    // Whole numbers: the page prints them as they are.
    for (const value of Object.values(privacyCookieFacts())) {
      if (typeof value === 'number') expect(Number.isInteger(value) && value > 0).toBe(true);
    }
  });

  it('the page names both cookies and gives each lifetime the code gives it', async () => {
    const text = await rendered('/privacy');
    expect(text).toContain(`( ${SESSION_COOKIE_NAME} )`);
    expect(text).toContain(`It expires after ${SESSION_IDLE_TIMEOUT_MS / HOUR_MS} hours without a visit`);
    expect(text).toContain(`a login ends after ${SESSION_ABSOLUTE_MAX_MS / DAY_MS} days however often you visit`);
    expect(text).toContain(`( ${TRIAL_COOKIE_NAME} )`);
    expect(text).toContain(`It lasts ${TRIAL_TTL_DAYS} days from your first visit`);
    expect(text).toContain(`for ${PAID_TRIAL_ACCESS_DAYS} days after the payment`);
    // The sentence this replaced, and the claim that there is one kind of cookie.
    expect(text).not.toMatch(/Expire after 30 days of inactivity/i);
    expect(text).not.toMatch(/only essential session cookies to maintain your login state/i);
  });

  it('the session cookie the app really sets lasts the idle timeout, with the flags the page lists', async () => {
    // A login is what first stores something in the session, so it is what sets the cookie.
    const { default: bcrypt } = await import('bcrypt');
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    mockQuery.mockResolvedValueOnce({
      rows: [{ id: 'customer-1', email: 'person@example.com', name: 'Person', password: await bcrypt.hash('password123', 1), email_verified: true }],
    });
    const before = Date.now();
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ _csrf: generateCsrfToken({} as Request), email: 'person@example.com', password: 'password123' });
    const cookie = ([] as string[]).concat(res.headers['set-cookie'] || []).find((c) => c.startsWith(`${SESSION_COOKIE_NAME}=`)) ?? '';
    expect(cookie, 'the login sets the session cookie').not.toBe('');
    const expires = Date.parse(cookie.match(/Expires=([^;]+)/i)?.[1] ?? '');
    expect(Math.abs(expires - (before + SESSION_IDLE_TIMEOUT_MS))).toBeLessThan(60_000);
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/SameSite=Lax/i);
  });

  it('the conversation cookie is set for its seven days, and a paying browser\'s for what is left of its window', async () => {
    const cookies: Array<{ name: string; options: { maxAge?: number; httpOnly?: boolean; sameSite?: string } }> = [];
    const res = { cookie: (name: string, _value: string, options: Record<string, unknown>) => cookies.push({ name, options }) };
    const fresh = { query: vi.fn().mockResolvedValue({ rows: [{ trial_id: 't-1', cookie_token: 'a'.repeat(64) }] }) };
    await resolveTrial(fresh as never, { headers: {}, ip: '203.0.113.5' } as never, res as never, {
      mintLimiter: createMintLimiter({ max: 5, windowMs: 60_000 }),
    });
    expect(cookies).toHaveLength(1);
    expect(cookies[0].name).toBe(TRIAL_COOKIE_NAME);
    expect(cookies[0].options.maxAge).toBe(TRIAL_TTL_DAYS * DAY_MS);
    expect(cookies[0].options).toMatchObject({ httpOnly: true, sameSite: 'lax' });
    // The row itself is given the same lifetime, by the statement that mints it.
    expect(String(fresh.query.mock.calls[0][0])).toContain(`NOW() + INTERVAL '${TRIAL_TTL_DAYS} days'`);
  });
});

describe('the privacy page says what deleting an account removes and what it leaves', () => {
  const pageText = (): string => plain('views/privacy.ejs');

  // One phrase of the page per table the deletion leaves rows in.
  const LEFT: Record<(typeof ACCOUNT_DELETION_LEAVES)[number], RegExp> = {
    builds: /the records of your website builds stay in our database with the link to your account removed\. They include the specification each website was built from/,
    build_logs: /and the build's logs/,
    email_log: /The log of emails we sent you, with your address, also stays/,
    newsletter_subscribers: /and so does an address you gave the newsletter list/,
    page_views: /Page-view records stay without your account identifier/,
  };
  // And one per kind of thing it removes; every removed table is of one kind.
  const REMOVED: Array<[RegExp, ReadonlyArray<(typeof ACCOUNT_DELETION_REMOVES)[number]>]> = [
    [/your account record/, ['customers', 'password_reset_tokens', 'email_verification_tokens', 'user_sessions', 'build_start_events']],
    [/your conversations with Haven and the notes kept from them/, ['keepers', 'trial_keepers', 'companion_turns', 'keeper_memory']],
    [/your messages in the other chats/, ['intake_messages', 'feature_requests', 'feature_messages']],
    [/your feedback/, ['beta_feedback']],
    [/the emails scheduled for you/, ['email_schedule']],
    [/tells Stripe to end any subscription at the close of the period you have paid for/, ['harbor_subscriptions']],
  ];

  it('every table the deletion leaves rows in has its sentence on the page', () => {
    expect(Object.keys(LEFT).sort()).toEqual([...ACCOUNT_DELETION_LEAVES].sort());
    for (const table of ACCOUNT_DELETION_LEAVES) expect(pageText(), table).toMatch(LEFT[table]);
  });

  it('every table the deletion empties is covered by a sentence on the page', () => {
    const covered = REMOVED.flatMap(([, tables]) => tables);
    expect([...covered].sort()).toEqual([...ACCOUNT_DELETION_REMOVES].sort());
    for (const [phrase] of REMOVED) expect(pageText()).toMatch(phrase);
  });

  it('the page does not promise what the deletion does not do', () => {
    const text = pageText();
    // The specification and the hosted website: both stay, and the page says so.
    expect(text).not.toMatch(/This includes your account information, website specifications, and chat history/);
    expect(text).not.toMatch(/we will take down any hosted websites within 30 days and delete associated deployment data/);
    expect(text).toMatch(/deleting your account does not take your website down, and it does not delete the website's own database/);
    // An account's own deletion page exists and is where the page sends the reader.
    expect(read('views/privacy.ejs')).toContain('href="/account/delete"');
  });

  it('the route that deletes an account names no hosting provider and deletes no build', () => {
    // The statements themselves are run against a real database in
    // money-path.realdb.test.ts; this is the cheap check that runs everywhere.
    const source = read('src/routes/auth.ts');
    const route = source.slice(source.indexOf("router.post('/account/delete'"));
    expect(route).toContain('UPDATE builds SET customer_id = NULL WHERE customer_id = $1');
    expect(route).not.toMatch(/DELETE FROM builds\b/);
    expect(route).not.toMatch(/railway/i);
    for (const table of ['email_log', 'newsletter_subscribers', 'build_logs', 'page_views']) expect(route, table).not.toContain(table);
  });
});

describe('the terms state what the code does', () => {
  it('the build limits are the code\'s, and a preview is described as what it is', async () => {
    mockQuery.mockResolvedValue({ rows: [] });
    const text = (await request(app).get('/terms')).text.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
    expect(text).toContain(
      `at most ${MAX_ACTIVE_BUILDS_PER_CUSTOMER} in progress at a time, ${MAX_BUILD_STARTS_PER_DAY} started in any 24 hours, and ${MAX_MANUAL_RETRIES_PER_BUILD} retries of one failed build`
    );
    expect(text).toContain('A preview is a real website at a public address. Anyone who has the address can open it, and it is not hidden from search engines.');
    // No step where the customer approves a specification exists, and nothing pauses a subscription.
    expect(text).not.toMatch(/you review and approve the Spec/);
    expect(text).not.toMatch(/You may pause or cancel the subscription at any time from your conversation/);
    expect(text).toMatch(/There is no button for pausing/);
  });
});

// A subscription has one control in this application: a cancel button on the
// website's own page, which stops billing at the end of the period. Nothing
// pauses a subscription, and nothing in the conversation cancels one. The
// copy has to say that wherever it mentions either.
describe('copy about cancelling and pausing matches the one control that exists', () => {
  it('the only subscription route is cancel, it lives on the website\'s page, and it is cancel-at-period-end', () => {
    const routes = read('src/routes/lighthouse.ts');
    expect([...routes.matchAll(/'\/lamp\/:buildId\/([a-z-]*subscription[a-z-]*)'/g)].map((m) => m[1])).toEqual(['cancel-subscription']);
    expect(routes).not.toMatch(/pause[-_]?subscription|pauseHarborSubscription|pause_collection/i);
    expect(read('src/services/stripe.ts')).toContain('cancel_at_period_end: true');
    expect(read('views/lighthouse/lamp.ejs')).toContain('/cancel-subscription');
    expect(read('views/lighthouse/table.ejs')).not.toContain('cancel-subscription');
  });

  const SURFACES = ['views/terms.ejs', 'views/lighthouse/make-public.ejs', 'src/services/email.ts', 'src/services/pricing-faq.ts'];
  for (const file of SURFACES) {
    it(`${file} does not say a subscription is paused or cancelled from the conversation, or paused by the customer`, () => {
      const text = copyOf(file);
      expect(text).not.toMatch(/pause or cancel/i);
      expect(text).not.toMatch(/cancel(?:led|ling)? (?:the subscription )?(?:at any time )?from your conversation/i);
      expect(text).not.toMatch(/You pause it any time/i);
    });
  }

  it('each of them says where cancelling is done, and that pausing is by writing to us', () => {
    expect(copyOf('views/lighthouse/make-public.ejs')).toContain(
      "You can cancel at any time from your website's page, or write to us to pause; your website stays up either way."
    );
    expect(copyOf('src/services/email.ts')).toContain(
      "You can cancel from your website's page, linked from your conversation, whenever it feels right, or write to us to pause; your website stays up either way."
    );
    expect(copyOf('src/services/pricing-faq.ts')).toContain('You can cancel it any time, and write to us if you would rather pause.');
    expect(plain('views/terms.ejs')).toMatch(/You may cancel the subscription at any time from your website's page on this site/);
    expect(plain('views/terms.ejs')).toMatch(/There is no button for pausing; write to us and we will arrange it\./);
  });
});

// The Stripe product's name and description are held to the same rules, and
// to the terms page, in stripe.service.test.ts, where the request sent to
// Stripe can be read.

describe('customer-visible template summaries carry no internal labels', () => {
  it('no family name, summary or alt text names an internal project', () => {
    _resetTemplateCacheForTests();
    const families = listFamilies();
    expect(families.length).toBeGreaterThan(0);
    for (const family of families) {
      for (const text of [family.name, family.summary, family.thumbnail_alt_summary ?? '']) {
        expect(text, family.id).not.toMatch(/\bMW\b|\bv\d+\b|\bported\b/i);
      }
    }
  });
});
