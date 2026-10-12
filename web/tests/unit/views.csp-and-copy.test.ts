import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import request from 'supertest';
import app from '../../src/app';

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

function listFiles(dir: string, ext: string, skip: (rel: string) => boolean = () => false): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(path.join(webRoot, dir), { withFileTypes: true })) {
    const rel = path.join(dir, entry.name);
    if (skip(rel)) continue;
    if (entry.isDirectory()) out.push(...listFiles(rel, ext, skip));
    else if (entry.name.endsWith(ext)) out.push(rel);
  }
  return out;
}

// views/lighthouse/** belongs to the Harbor surface and is reviewed separately.
const customerAndAdminViews = listFiles('views', '.ejs', (rel) => rel.startsWith(path.join('views', 'lighthouse')));
const clientScripts = listFiles('public', '.js');

describe('no inline event-handler attributes (the CSP forbids them)', () => {
  // script-src is 'self' + a per-request nonce. A nonce authorises <script>
  // elements only; onclick="...", onchange="..." and friends are blocked, so a
  // button wired that way silently does nothing. Wire handlers with
  // addEventListener or the data-* hooks in views/layout.ejs instead.
  const inlineHandler = /<[a-zA-Z][^<>]*?\son[a-z]+\s*=\s*["']/;

  it('finds a plausible number of files to scan', () => {
    expect(customerAndAdminViews.length).toBeGreaterThan(30);
    expect(clientScripts.length).toBeGreaterThan(4);
  });

  it('the pattern catches a handler that follows an EJS tag inside the same HTML tag (control)', () => {
    const sample = '<form action="/x/<%= build.id %>/cancel" onsubmit="return confirm(1)">'.replace(/<%[\s\S]*?%>/g, 'EJS');
    expect(sample).toMatch(inlineHandler);
    expect('<form action="/x/<%= build.id %>/cancel" onsubmit="return confirm(1)">').not.toMatch(inlineHandler);
  });

  for (const file of [...customerAndAdminViews, ...clientScripts]) {
    it(`${file} has none`, () => {
      // EJS tags are replaced first: their own "<" and ">" would otherwise end
      // the HTML tag early and hide a handler written after one, as in
      // <form action="/x/<%= id %>/y" onsubmit="...">.
      const text = fs.readFileSync(path.join(webRoot, file), 'utf-8').replace(/<%[\s\S]*?%>/g, 'EJS');
      const match = text.match(inlineHandler);
      expect(match ? match[0].slice(0, 120) : null).toBeNull();
    });
  }
});

describe('retired pricing, tier and payment copy is gone from customer-facing files', () => {
  const customerFacing = [
    'views/status.ejs',
    'views/dashboard.ejs',
    'views/revision.ejs',
    'views/intake.ejs',
    'views/intake-anon.ejs',
    'views/terms.ejs',
    'views/guide.ejs',
    'public/status.js',
    'public/intake.js',
    'public/intake-anon.js',
    'public/revision.js',
  ];
  const stale: Array<[RegExp, string]> = [
    [/\/payment\//, 'link to the removed /payment route'],
    [/\$299|\$499|\$799|\$1,999/, 'retired one-time tier price'],
    [/Choose (a|your) tier|intake__tier|tiersAllowed/, 'tier picker'],
    [/full refund/i, 'refund promise tied to the retired build fee'],
    [/Pay now|Complete payment/, 'dead pay-now button'],
    [/free revisions? (remaining|used)|of \d+ free revision/i, 'capped free revisions (the offer is unlimited)'],
    [/\/projects\/<%= build\.id %>\/features|\/projects\/' \+ buildId \+ '\/features/, 'entry point to the feature-request flow (no payment path)'],
    [/TODO|FIXME/, 'unresolved TODO'],
  ];

  for (const file of customerFacing) {
    it(`${file} has none of it`, () => {
      const text = fs.readFileSync(path.join(webRoot, file), 'utf-8');
      for (const [pattern, why] of stale) {
        expect(pattern.test(text), `${file}: ${why}`).toBe(false);
      }
    });
  }

  it('customer-facing titles and labels say "website", not "app"', () => {
    for (const [file, forbidden] of [
      ['src/routes/builds.ts', "title: 'My Apps'"],
      ['src/routes/intake.ts', "title: 'Describe Your App'"],
      ['src/routes/gallery.ts', "title: 'App Gallery'"],
      ['public/status.js', 'Your App is Ready for Review'],
      ['public/status.js', 'Preview Your App'],
      ['public/status.js', 'Your Project is Live'],
    ]) {
      expect(fs.readFileSync(path.join(webRoot, file), 'utf-8'), `${file}: ${forbidden}`).not.toContain(forbidden);
    }
  });
});

describe('GET /terms describes the single subscription', () => {
  beforeEach(() => {
    mockQuery.mockReset();
  });

  it('quotes the price band from the shared locals and no longer describes a one-time build fee', async () => {
    const res = await request(app).get('/terms');
    expect(res.status).toBe(200);
    expect(res.text).toContain('$39');
    expect(res.text).toContain('$19');
    expect(res.text).toMatch(/Monthly subscription/);
    expect(res.text).toMatch(/Free preview/);
    expect(res.text).not.toMatch(/build fee|Non-refundable after build starts|first 3 months|tier rate/i);
    // The terms state a recorded review approval, not a review time.
    expect(res.text).not.toMatch(/business day/);
    expect(res.text).toContain('needs a recorded review approval before it goes live');
    expect(res.text).not.toMatch(/from payment to review-ready preview/);
  });

  it('calls the free build a preview, not a private one', async () => {
    const res = await request(app).get('/terms');
    expect(res.text).toMatch(/walk around\s+it in a preview\./);
    expect(res.text).not.toMatch(/private\s+preview/i);
  });
});

describe('the preview is never described as private', () => {
  // A preview is a public address on the hosting provider with no access
  // control: the generated site allows crawling from its first deploy and asks
  // for no credentials. Customer-facing copy must not say otherwise. The
  // persona prompt may contain the word only to forbid it (see persona.test.ts).
  const repoRoot = path.join(webRoot, '..');
  const claims: Array<[RegExp, string]> = [
    [/private\s+preview/i, '"private preview"'],
    [/preview(\s+(url|address|link|site))?\s+(is|stays|remains|will\s+stay)\s+private/i, '"the preview is private"'],
    [/(url|address|link)\s+(above\s+)?is\s+private/i, '"the address is private"'],
    [/no\s+one\s+(else\s+)?(finds|can\s+find|can\s+see)\s+it/i, '"no one finds it"'],
    [/only\s+you\s+(and\s+people\s+you\s+share|can\s+see)/i, '"only you can see it"'],
  ];
  const files = [
    ...listFiles('views', '.ejs'),
    'src/services/email.ts',
    'src/services/support-knowledge.ts',
    'src/routes/lighthouse.ts',
    'src/companion/persona-sections.json',
    path.join('..', 'coordinator', 'support_knowledge.py'),
    path.join('..', 'queue', 'mailer.py'),
  ];

  it('scans a plausible number of files', () => {
    expect(files.length).toBeGreaterThan(40);
    for (const file of files) expect(fs.existsSync(path.join(webRoot, file)), file).toBe(true);
    expect(fs.existsSync(path.join(repoRoot, 'coordinator'))).toBe(true);
  });

  it('the patterns catch the old wording (control)', () => {
    const samples = [
      'Your private preview is ready',
      'Walk around your private preview',
      'The URL above is private. No one finds it; only you and people you share the link with see it.',
      'the preview is private',
      'only you can see it',
    ];
    for (const sample of samples) {
      expect(claims.some(([pattern]) => pattern.test(sample)), sample).toBe(true);
    }
  });

  for (const file of files) {
    it(`${file} makes none of those claims`, () => {
      const text = fs.readFileSync(path.join(webRoot, file), 'utf-8').replace(/\\n/g, ' ').replace(/\s+/g, ' ');
      for (const [pattern, label] of claims) {
        expect(pattern.test(text), `${file}: ${label}`).toBe(false);
      }
    });
  }
});


describe('default descriptions match the subscription product', () => {
  const root = path.join(__dirname, '..', '..');

  function textFiles(dir: string): string[] {
    const out: string[] = [];
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) out.push(...textFiles(full));
      else if (/\.(ejs|js|json|css)$/.test(entry.name)) out.push(full);
    }
    return out;
  }

  it('nothing in views/ or public/ still says "pay once" or "host anywhere"', () => {
    const offenders: string[] = [];
    for (const file of [...textFiles(path.join(root, 'views')), ...textFiles(path.join(root, 'public'))]) {
      if (/pay once|host anywhere|cancel never/i.test(fs.readFileSync(file, 'utf-8'))) {
        offenders.push(path.relative(root, file));
      }
    }
    expect(offenders).toEqual([]);
  });

  it('a Harbor page with no metaDescription falls back to the same Open Graph copy as the classic layout', async () => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    // /terms passes no metaDescription and renders through layout-harbor.
    const res = await request(app).get('/terms');
    expect(res.status).toBe(200);
    const og = res.text.match(/<meta property="og:description" content="([^"]*)"/)?.[1] ?? '';
    const twitter = res.text.match(/<meta name="twitter:description" content="([^"]*)"/)?.[1] ?? '';
    expect(og).toBe(
      'Custom websites and a thoughtful companion, made with healers, therapists, and event producers. Every website needs a recorded review approval before it goes live.'
    );
    expect(twitter).toBe('Custom websites and a thoughtful companion. Every website needs a recorded review approval before it goes live.');
    const classic = fs.readFileSync(path.join(root, 'views', 'layout.ejs'), 'utf-8');
    expect(classic).toContain(og);
    expect(classic).toContain(twitter);
    // The page's own description still states the subscription price band.
    const description = res.text.match(/<meta name="description" content="([^"]*)"/)?.[1] ?? '';
    expect(description).toMatch(/\$39 down to \$19 a month/);
  });
});
