// Output-escaping contract for the non-Harbor views.
//
// `<%- ... %>` writes its value into the page unescaped. These tests pin the
// places where a raw tag carries something a customer, the model, a crawler or
// a query string can influence, and prove the value cannot leave the context
// it is written into (an HTML attribute, an inline <script>, an iframe srcdoc).

import { describe, it, expect, vi } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join } from 'path';

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }),
    connect: vi.fn(),
    on: vi.fn(),
  },
}));

import app from '../../src/app';

const VIEWS_DIR = join(__dirname, '..', '..', 'views');

/** Render a view (no layout wrapping) with the app's own locals and helpers. */
function renderView(view: string, locals: Record<string, unknown>): Promise<string> {
  return new Promise((resolve, reject) => {
    app.render(view, locals, (err: Error | null, html?: string) => {
      if (err || html === undefined) reject(err ?? new Error('no output'));
      else resolve(html);
    });
  });
}

const LAYOUT_LOCALS = {
  body: '<main>page body</main>',
  csrfToken: 'csrf-token-value',
  cspNonce: 'test-nonce',
  isLoggedIn: false,
  isAdmin: false,
  customerEmail: null,
  customerName: null,
  harborEnabled: false,
  analyticsId: '',
  canonicalPath: '/x',
  pageOgImage: '/og-default.png',
  noIndex: false,
  pageMesh: false,
  harborSubscriptionPrice: '$39',
  harborSlidingScalePrice: '$19',
};

const HOSTILE = 'Acme"><img src=x onerror=alert(1)><meta http-equiv="refresh" content="0;url=https://evil.example.test';

function ogContent(html: string, property: string): string {
  const match = new RegExp(`<meta property="${property}" content="([^"]*)"`).exec(html);
  if (!match) throw new Error(`no ${property} tag`);
  return match[1];
}

describe.each([['layout-harbor'], ['layout']])('%s.ejs Open Graph tags', (layout) => {
  it('keeps a hostile title inside the content attribute', async () => {
    const html = await renderView(layout, { ...LAYOUT_LOCALS, title: HOSTILE });

    // The attribute value is the whole title, with the quote escaped...
    const content = ogContent(html, 'og:title');
    expect(content).toContain('Acme&quot;&gt;&lt;img src=x onerror=alert(1)&gt;');
    // ...so no element was created from it anywhere in the page.
    expect(html).not.toContain('<img src=x onerror=alert(1)>');
    expect(html).not.toContain('<meta http-equiv="refresh"');
  });

  it('keeps a hostile description inside the content attribute', async () => {
    const html = await renderView(layout, { ...LAYOUT_LOCALS, title: 'T', metaDescription: HOSTILE });

    expect(ogContent(html, 'og:description')).toContain('Acme&quot;&gt;&lt;img');
    expect(html).not.toContain('<img src=x onerror=alert(1)>');
    expect(html).not.toContain('<meta http-equiv="refresh"');
  });

  it('still emits a raw ampersand and apostrophe (link-preview scrapers print entities literally)', async () => {
    const html = await renderView(layout, {
      ...LAYOUT_LOCALS,
      title: "Craft & Ship: Ocean's studio",
      metaDescription: "Fish & chips, the keeper's way",
    });

    expect(ogContent(html, 'og:title')).toBe("Craft & Ship: Ocean's studio");
    expect(ogContent(html, 'og:description')).toBe("Fish & chips, the keeper's way");
  });
});

describe('ogAttr helper', () => {
  const ogAttr = app.locals.ogAttr as (value: unknown) => string;

  it('escapes the characters that end an attribute or start a tag, and nothing else', () => {
    expect(ogAttr('a"b<c>d&e\'f')).toBe('a&quot;b&lt;c&gt;d&e\'f');
  });

  it('renders null and undefined as an empty string', () => {
    expect(ogAttr(undefined)).toBe('');
    expect(ogAttr(null)).toBe('');
  });
});

describe('safeJSON helper (values inlined into <script>)', () => {
  const safeJSON = app.locals.safeJSON as (value: unknown) => string;

  it('cannot close the script element or open a comment', () => {
    const out = safeJSON('</script><script>alert(1)</script><!--');
    expect(out).not.toContain('<');
    expect(out).not.toContain('>');
    // Still the same value once the browser parses it as JavaScript.
    expect(JSON.parse(out)).toBe('</script><script>alert(1)</script><!--');
  });

  it('emits null for undefined instead of throwing', () => {
    expect(safeJSON(undefined)).toBe('null');
  });
});

describe('admin-builds.ejs: the status filter comes from the query string', () => {
  it('cannot break out of the inline script', async () => {
    const hostile = '</script><script>alert(document.domain)</script>';
    const html = await renderView('admin-builds', {
      title: 'Admin',
      csrfToken: 'csrf-token-value',
      cspNonce: 'test-nonce',
      harborEnabled: false,
      activeTab: 'builds',
      builds: [],
      statusFilter: hostile,
      stats: { total: 0, queued: 0, building: 0, testing: 0, deploying: 0, preview: 0, live: 0, failed: 0 },
      pendingApprovalCount: 0,
      pendingReviewCount: 0,
    });

    expect(html).not.toContain(hostile);
    expect(html).not.toContain('<script>alert(document.domain)');
    // The value is still delivered to the page script, escaped.
    expect(html).toContain('var statusFilter = "\\u003c/script\\u003e\\u003cscript\\u003ealert(document.domain)\\u003c/script\\u003e";');
  });
});

describe('admin-email-detail.ejs: stored email HTML inside iframe srcdoc', () => {
  const baseLog = {
    id: 'log-1',
    to_email: 'user@example.com',
    template: 'welcome',
    subject: 'Hello',
    status: 'sent',
    resend_id: null,
    created_at: new Date('2026-05-01T12:00:00Z'),
    error_message: null,
  };

  it('does not un-escape text that the email template had escaped', async () => {
    // What an email template stores after escaping a customer name of
    // <script>alert(1)</script>: inert text.
    const stored = '<p>Hi &lt;script&gt;alert(1)&lt;/script&gt;,</p>';
    const html = await renderView('admin-email-detail', {
      title: 'Email',
      csrfToken: 'csrf-token-value',
      cspNonce: 'test-nonce',
      log: { ...baseLog, html_body: stored },
    });

    const srcdoc = /srcdoc="([^"]*)"/.exec(html)?.[1];
    expect(srcdoc).toBeDefined();
    // The browser decodes character references in an attribute value once.
    // After that one decode the frame must receive exactly what was stored,
    // so the escaped name is still escaped inside the frame.
    const decodeOnce = (value: string): string =>
      value.replace(/&#(\d+);/g, (_m, code: string) => String.fromCharCode(Number(code)))
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&amp;/g, '&');
    expect(decodeOnce(srcdoc as string)).toBe(stored);
    expect(decodeOnce(srcdoc as string)).not.toContain('<script>');
  });

  it('cannot break out of the srcdoc attribute, and the frame stays fully sandboxed', async () => {
    const html = await renderView('admin-email-detail', {
      title: 'Email',
      csrfToken: 'csrf-token-value',
      cspNonce: 'test-nonce',
      log: { ...baseLog, html_body: '"></iframe><script>alert(1)</script>' },
    });

    expect(html).not.toContain('"></iframe><script>alert(1)</script>');
    expect(html).toMatch(/<iframe[^>]*\ssandbox=""/);
  });
});

describe('no view inlines unescaped JSON into a script', () => {
  function ejsFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return ejsFiles(full);
      return name.endsWith('.ejs') ? [full] : [];
    });
  }

  it('uses safeJSON, never a raw JSON.stringify, in <%- %> tags', () => {
    const offenders = ejsFiles(VIEWS_DIR).filter((file) =>
      /<%-\s*JSON\.stringify\(/.test(readFileSync(file, 'utf-8'))
    );
    expect(offenders.map((file) => file.slice(VIEWS_DIR.length + 1))).toEqual([]);
  });
});

describe('non-Harbor views under the nonce-based Content-Security-Policy', () => {
  // script-src is 'self' plus a per-request nonce, with no 'unsafe-inline'.
  // An inline <script> without the nonce is silently dropped by the browser,
  // and so is a javascript: URL. (Inline on<event>= attributes are covered by
  // views.csp-and-copy.test.ts; views/lighthouse/** by views.lighthouse-csp.test.ts.)
  function ejsFiles(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) return name === 'lighthouse' ? [] : ejsFiles(full);
      return name.endsWith('.ejs') ? [full] : [];
    });
  }
  const files = ejsFiles(VIEWS_DIR);
  /** Drop EJS comments, and reduce any output tag that prints cspNonce to a token so `>` inside it cannot end the tag early. */
  const prepared = (file: string): string =>
    readFileSync(file, 'utf-8')
      .replace(/<%#[\s\S]*?%>/g, '')
      .replace(/<%=(?:(?!%>)[\s\S])*?\bcspNonce\b(?:(?!%>)[\s\S])*?%>/g, 'NONCE');

  it('finds the views', () => {
    expect(files.length).toBeGreaterThan(30);
  });

  it('every inline <script> carries the request nonce', () => {
    const missing: string[] = [];
    for (const file of files) {
      const inlineScripts = prepared(file).match(/<script\b(?![^>]*\bsrc=)[^>]*>/gi) ?? [];
      for (const tag of inlineScripts) {
        if (!tag.includes('nonce="NONCE"')) missing.push(`${file.slice(VIEWS_DIR.length + 1)}: ${tag}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it('no link or form targets a javascript: URL', () => {
    const offenders = files.filter((file) => /(?:href|action|formaction|src)\s*=\s*["']\s*javascript:/i.test(prepared(file)));
    expect(offenders.map((file) => file.slice(VIEWS_DIR.length + 1))).toEqual([]);
  });

  it('the layout prints the JSON-LD block it is given untouched, nonce included (the nonce itself is added in app.ts; see security-headers-csp.test.ts)', async () => {
    const html = await renderView('layout-harbor', {
      ...LAYOUT_LOCALS,
      title: 'T',
      defaultJsonLd: '<script type="application/ld+json" nonce="test-nonce">{}</script>',
    });
    const scripts = html.match(/<script\b[^>]*>/gi) ?? [];
    expect(scripts.length).toBeGreaterThan(0);
    for (const tag of scripts) expect(tag).toContain('nonce="test-nonce"');
  });
});

describe('admin views: rows and panels built in the browser from API data', () => {
  // These two views assemble HTML strings in page script and assign them to
  // innerHTML. The data is text other people wrote (a customer's name at
  // signup, an app name, a build error, a model's analysis), and an admin is
  // the one looking at it. The tests run the real page script against a
  // stand-in DOM and inspect the HTML it produces.
  const vm = require('vm') as typeof import('vm');

  const HOSTILE_TEXT = '<img src=x onerror=alert(1)><form action="/admin/builds/1/delete" method="POST">';
  const HOSTILE_ATTR = '" onmouseover="alert(1)" x="';

  interface FakeElement {
    innerHTML: string;
    className: string;
    style: Record<string, string>;
    attrs: Record<string, string>;
    setAttribute(name: string, value: string): void;
    getAttribute(name: string): string | null;
  }
  const fakeElement = (): FakeElement => ({
    innerHTML: '',
    className: '',
    style: {},
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return this.attrs[name] ?? null; },
  });

  /**
   * No markup may come out of a hostile value: no new tag (the "<" is escaped)
   * and no new attribute (the quote that would end the current one is escaped).
   */
  function expectInert(html: string): void {
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<form action="/admin/builds/1/delete"');
    expect(html).not.toMatch(/\sonmouseover="/);
  }

  it('admin-builds.ejs: a hot-swapped row escapes every field', async () => {
    const page = await renderView('admin-builds', {
      title: 'Admin',
      csrfToken: 'csrf-token-value',
      cspNonce: 'test-nonce',
      harborEnabled: false,
      activeTab: 'builds',
      builds: [],
      statusFilter: '',
      stats: { total: 0, queued: 0, building: 0, testing: 0, deploying: 0, preview: 0, live: 0, failed: 0 },
      pendingApprovalCount: 0,
      pendingReviewCount: 0,
    });
    const scripts = [...page.matchAll(/<script nonce="test-nonce">([\s\S]*?)<\/script>/g)].map((m) => m[1]);
    const source = scripts.find((s) => s.includes('function buildRow'));
    expect(source).toBeDefined();
    // The script is one IIFE; hand its two renderers out instead of letting it run to the end.
    const body = (source as string).trim().replace(/^\(function\(\) \{/, '').replace(/\}\)\(\);$/, '');
    const sandbox = {
      document: {
        createElement: fakeElement,
        querySelectorAll: () => [],
        querySelector: () => null,
        getElementById: () => null,
        addEventListener: () => undefined,
      },
      window: {},
      setTimeout: () => 0,
      setInterval: () => 0,
      fetch: () => new Promise(() => undefined),
    };
    const api = vm.runInNewContext(
      `(function() {${body}; return { buildRow: buildRow, actionButtons: actionButtons }; })()`,
      sandbox
    ) as { buildRow: (b: Record<string, unknown>) => FakeElement; actionButtons: (b: Record<string, unknown>) => string };

    const row = api.buildRow({
      id: 'b1' + HOSTILE_ATTR,
      app_name: HOSTILE_TEXT,
      customer_name: HOSTILE_TEXT,
      customer_email: 'x@example.com' + HOSTILE_TEXT,
      tier: 'build' + HOSTILE_ATTR,
      status: 'failed',
      build_engine: 'v2' + HOSTILE_ATTR,
      error_message: HOSTILE_ATTR + HOSTILE_TEXT,
      retry_count: '3<b>',
      created_at: '2026-05-01T12:00:00Z',
      live_url: 'javascript:alert(document.domain)',
    });

    expectInert(row.innerHTML);
    // The text is still shown, as text.
    expect(row.innerHTML).toContain('&lt;img src=x onerror=alert(1)&gt;');
    // A javascript: URL from the API never becomes a link target.
    expect(row.innerHTML).not.toContain('javascript:');
    expect(row.innerHTML).toContain('<a href="#" target="_blank"');
    // A non-numeric retry count cannot carry markup.
    expect(row.innerHTML).not.toContain('<b>');

    const preview = api.actionButtons({ id: 'b2', status: 'preview', preview_url: 'https://preview.example.test/x' + HOSTILE_ATTR });
    expectInert(preview);
    expect(preview).toContain('href="https://preview.example.test/x&quot; onmouseover=&quot;alert(1)&quot; x=&quot;"');
  });

  it('admin-build-detail.ejs: the analysis panel escapes model output', () => {
    const source = readFileSync(join(VIEWS_DIR, 'admin-build-detail.ejs'), 'utf-8');
    const start = source.indexOf('function renderAnalysis(analysis) {');
    expect(start).toBeGreaterThan(-1);
    const end = source.indexOf("resultEl.style.display = '';", start);
    const fnSource = source.slice(start, end) + "resultEl.style.display = ''; }";
    expect(fnSource).not.toContain('<%');

    const resultEl = fakeElement();
    const sandbox = { document: { getElementById: () => resultEl } };
    vm.runInNewContext(`${fnSource}; renderAnalysis(input);`, {
      ...sandbox,
      input: {
        root_cause: HOSTILE_TEXT,
        confidence: 'high' + HOSTILE_TEXT,
        implicated_files: ['routes/a.js', HOSTILE_TEXT],
        fix_summary: HOSTILE_TEXT + ' & more',
        recommended_action: HOSTILE_TEXT,
      },
    });

    expectInert(resultEl.innerHTML);
    expect(resultEl.innerHTML).toContain('Recommended: &lt;img src=x onerror=alert(1)&gt;');
    expect(resultEl.innerHTML).toContain('&amp; more');
  });
});
