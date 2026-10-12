// Security behavior that lives in src/app.ts itself: the cross-origin write
// guard, the contact and guide forms, async error forwarding, the error
// handler, the preview password gate, and the response headers.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readdirSync, readFileSync } from 'fs';
import { join } from 'path';
import request from 'supertest';

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }),
    connect: vi.fn(),
    on: vi.fn(),
  },
}));

import app, { buildContactEmail, escapeHtml, CONTACT_SEND_TIMEOUT_MS } from '../../src/app';
import pool from '../../src/db/index';
import { generateCsrfToken } from '../../src/middleware/auth';
import type { Request } from 'express';
import bcrypt from 'bcrypt';

const mockQuery = pool.query as ReturnType<typeof vi.fn>;
const csrf = (): string => generateCsrfToken({} as Request);

beforeEach(() => {
  mockQuery.mockReset().mockResolvedValue({ rows: [], rowCount: 0 });
});

describe('cross-origin write guard (CSRF layer 1)', () => {
  const validContact = () => ({ _csrf: csrf(), name: 'Ada', email: 'ada@example.com', subject: 'general', message: 'Hello' });

  it('refuses a form post another site submits, even with a valid CSRF token', async () => {
    // The token is not bound to a session, so an attacker can fetch one and
    // embed it. The browser-supplied origin is what stops the forged request.
    const res = await request(app)
      .post('/contact')
      .set('Origin', 'https://evil.example.test')
      .type('form')
      .send(validContact());

    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/cross-origin/i);
  });

  it('refuses when the browser labels the request cross-site or same-site', async () => {
    for (const site of ['cross-site', 'same-site']) {
      const res = await request(app)
        .post('/login')
        .set('Sec-Fetch-Site', site)
        .type('form')
        .send({ _csrf: csrf(), email: 'a@example.com', password: 'password123' });
      expect(res.status).toBe(403);
    }
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('runs before the routers, so it also covers the JSON routes that check no token', async () => {
    // POST /try/:id/message reads its token inside the handler and the Harbor
    // turn endpoint reads none; neither is reached by a cross-origin request.
    for (const path of ['/try/00000000-0000-4000-8000-000000000001/message', '/lighthouse/table/turn', '/lighthouse/build']) {
      const res = await request(app)
        .post(path)
        .set('Origin', 'https://evil.example.test')
        .send({ message: 'hello' });
      expect(res.status, path).toBe(403);
      expect(res.body.error, path).toMatch(/cross-origin/i);
    }
    expect(mockQuery).not.toHaveBeenCalled();
  });

  it('still sees a write aimed at a static file path (express.static passes non-GET methods on)', async () => {
    const res = await request(app)
      .post('/style.css')
      .set('Origin', 'https://evil.example.test')
      .send({ message: 'hello' });
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/cross-origin/i);
  });

  it('allows the same request from our own origin', async () => {
    const agent = request(app);
    const sameOrigin = await agent
      .post('/contact')
      .set('Host', 'app.example.test')
      .set('Origin', 'http://app.example.test')
      .type('form')
      .send(validContact());
    // Past the guard and into the handler (which, with no mail configured in
    // this block, answers 503).
    expect(sameOrigin.status).not.toBe(403);

    const fetchMetadata = await agent
      .post('/contact')
      .set('Sec-Fetch-Site', 'same-origin')
      .type('form')
      .send(validContact());
    expect(fetchMetadata.status).not.toBe(403);
  });

  it('does not get in the way of server-to-server webhooks (no Origin, no Sec-Fetch-Site)', async () => {
    const res = await request(app)
      .post('/webhooks/stripe')
      .set('Content-Type', 'application/json')
      .send('{}');
    // Reaches the webhook handler, which rejects the missing signature itself.
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/signature/i);
  });

  it('leaves cross-origin GET navigation alone (links from other sites must work)', async () => {
    const res = await request(app).get('/pricing').set('Origin', 'https://other.example.test').set('Sec-Fetch-Site', 'cross-site');
    expect(res.status).toBe(200);
  });
});

describe('POST /contact', () => {
  const originalFetch = globalThis.fetch;
  const originalKey = process.env.RESEND_API_KEY;
  const originalAdmin = process.env.ADMIN_EMAIL;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    process.env.RESEND_API_KEY = ['re', 'test', 'placeholder'].join('_');
    process.env.ADMIN_EMAIL = 'owner@example.com';
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    if (originalKey === undefined) delete process.env.RESEND_API_KEY;
    else process.env.RESEND_API_KEY = originalKey;
    if (originalAdmin === undefined) delete process.env.ADMIN_EMAIL;
    else process.env.ADMIN_EMAIL = originalAdmin;
  });

  const sentPayload = (): { subject: string; html: string; reply_to: string; to: string[]; from: string } =>
    JSON.parse(fetchMock.mock.calls[0][1].body as string);

  it('escapes the sender name and email in the HTML sent to the admin inbox', async () => {
    const res = await request(app)
      .post('/contact')
      .type('form')
      .send({
        _csrf: csrf(),
        name: '<img src=x onerror=alert(1)>Mallory',
        email: 'mallory@example.com',
        subject: 'general',
        message: 'Hi <b>there</b> & "friends"',
      });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const mail = sentPayload();
    expect(mail.html).not.toContain('<img');
    expect(mail.html).toContain('&lt;img src=x onerror=alert(1)&gt;Mallory');
    expect(mail.html).not.toContain('<b>there</b>');
    expect(mail.html).toContain('Hi &lt;b&gt;there&lt;/b&gt; &amp; &quot;friends&quot;');
    expect(mail.to).toEqual(['owner@example.com']);
    expect(mail.reply_to).toBe('mallory@example.com');
  });

  it('builds the subject from an allow-listed label, never from the submitted value', async () => {
    await request(app)
      .post('/contact')
      .type('form')
      .send({
        _csrf: csrf(),
        name: 'Ada',
        email: 'ada@example.com',
        subject: '<script>alert(1)</script>\r\nBcc: victim@example.com',
        message: 'Hello',
      });

    const mail = sentPayload();
    expect(mail.subject).toBe('[Contact] General question: Ada');
    expect(mail.html).not.toContain('<script>');
    expect(mail.html).not.toContain('Bcc:');
  });

  it('maps every option the form offers to its label', async () => {
    const expected: Record<string, string> = {
      general: 'General question',
      project: 'Project outside the standard offer',
      partnership: 'Partnership or collaboration',
      press: 'Press or interview',
      grant: 'Grant program',
      other: 'Something else',
    };
    for (const [value, label] of Object.entries(expected)) {
      expect(buildContactEmail({ name: 'Ada', email: 'ada@example.com', subjectKey: value, message: 'm' }).subject)
        .toBe(`[Contact] ${label}: Ada`);
    }
    // Inherited object keys are not labels.
    expect(buildContactEmail({ name: 'Ada', email: 'ada@example.com', subjectKey: 'general', message: 'm' }).subject)
      .not.toContain('function');
  });

  it('keeps line breaks out of the subject line (header injection)', () => {
    const mail = buildContactEmail({
      name: 'Ada\r\nBcc: victim@example.com\u0000',
      email: 'ada@example.com',
      subjectKey: 'general',
      message: 'm',
    });
    expect(mail.subject).not.toMatch(/[\u0000-\u001f]/);
    expect(mail.subject).toBe('[Contact] General question: Ada Bcc: victim@example.com');
  });

  it('rejects an email address that could smuggle a second header into Reply-To', async () => {
    const res = await request(app)
      .post('/contact')
      .type('form')
      .send({ _csrf: csrf(), name: 'Ada', email: 'ada@example.com\r\nBcc: victim@example.com', subject: 'general', message: 'Hello' });

    expect(res.status).toBe(400);
    expect(res.text).toContain('valid email address');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('requires the CSRF token the form already carries', async () => {
    const res = await request(app)
      .post('/contact')
      .type('form')
      .send({ name: 'Ada', email: 'ada@example.com', subject: 'general', message: 'Hello' });

    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('answers array or object fields with the validation error instead of crashing', async () => {
    const res = await request(app)
      .post('/contact')
      .type('form')
      .send('_csrf=' + encodeURIComponent(csrf()) + '&name[]=x&email[k]=v&message[]=m&subject[]=s');

    expect(res.status).toBe(400);
    expect(res.text).toContain('Please fill in all fields');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ['no name', { name: '' }],
    ['no message', { message: '   ' }],
    ['no address', { email: '' }],
    ['an address that is not one', { email: 'not-an-address' }],
  ])('%s: the same page with what was typed kept, answered 400, and nothing is sent', async (_label, overrides) => {
    const res = await request(app)
      .post('/contact')
      .type('form')
      .send({ _csrf: csrf(), name: 'Ada', email: 'ada@example.com', subject: 'general', message: 'Hello there', ...overrides });

    expect(res.status).toBe(400);
    expect(res.text).toContain('Please fill in all fields with a valid email address.');
    expect(res.text).toContain('class="contact-error"');
    // What was typed is still in the form.
    if (!('name' in overrides)) expect(res.text).toContain('value="Ada"');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  const submit = (overrides: Record<string, string> = {}) =>
    request(app)
      .post('/contact')
      .type('form')
      .send({ _csrf: csrf(), name: 'Ada', email: 'ada@example.com', subject: 'general', message: 'Hello there', ...overrides });

  it('shows the confirmation, and no error, once the mail provider accepted the note', async () => {
    const res = await submit();

    expect(res.status).toBe(200);
    expect(res.text).toContain('Note received.');
    expect(res.text).toContain('Thank you. Your note has been sent.');
    // The confirmation promises no reply, from anyone, in any time.
    expect(res.text).not.toMatch(/will write back|write back to|reply personally|within one business day/i);
    expect(res.text).not.toContain('class="contact-error"');
  });

  it('does not echo the sender address into the confirmation, so markup in it never reaches the page', async () => {
    // The address passes the (deliberately loose) format check with markup in it.
    const res = await submit({ email: '<b>x</b>@example.com' });

    expect(res.status).toBe(200);
    expect(res.text).toContain('Note received.');
    expect(res.text).not.toContain('<b>x</b>');
    expect(res.text).not.toContain('&lt;b&gt;x&lt;/b&gt;');
  });

  it('does not claim the note was received when the mail provider rejects the send', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 422 });
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await submit();

      expect(res.status).toBe(502);
      expect(res.text).not.toContain('Note received.');
      expect(res.text).toContain('class="contact-error" role="alert"');
      expect(res.text).toContain('Your note could not be sent. Please try again in a little while.');
      // What the visitor typed is still in the form.
      expect(res.text).toContain('Hello there');
      expect(res.text).toContain('value="ada@example.com"');
      expect(errorSpy.mock.calls.some((call) => String(call[0]).includes('HTTP 422'))).toBe(true);
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('a mail provider that never answers is given up on at the deadline: the visitor gets the "not sent" page, not an open request', async () => {
    // The stand-in provider accepts the request and never answers; only the
    // request's own signal ends it. The deadline is shortened by handing
    // AbortSignal.timeout a signal that fires after 40 ms, and what the route
    // asked for is recorded.
    const askedFor: number[] = [];
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout').mockImplementation((ms: number) => {
      askedFor.push(ms);
      const controller = new AbortController();
      setTimeout(() => controller.abort(new DOMException('The operation timed out.', 'TimeoutError')), 40);
      return controller.signal;
    });
    fetchMock.mockImplementationOnce(
      (_url: string, init: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          // Without a signal this promise never settles, and neither does the request.
          init.signal?.addEventListener('abort', () => reject(init.signal?.reason));
        })
    );
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const started = Date.now();
      const res = await submit();
      expect(Date.now() - started).toBeLessThan(3_000);
      expect(res.status).toBe(502);
      expect(res.text).not.toContain('Note received.');
      expect(res.text).toContain('Your note could not be sent.');
      expect(res.text).toContain('Hello there');
      // The deadline the route set is the shipped one, and it is seconds.
      expect(askedFor).toEqual([CONTACT_SEND_TIMEOUT_MS]);
      expect(CONTACT_SEND_TIMEOUT_MS).toBeGreaterThanOrEqual(5_000);
      expect(CONTACT_SEND_TIMEOUT_MS).toBeLessThanOrEqual(30_000);
    } finally {
      timeoutSpy.mockRestore();
      errorSpy.mockRestore();
    }
  }, 10_000);

  it('does not claim the note was received when the mail provider cannot be reached', async () => {
    fetchMock.mockRejectedValueOnce(new Error('connect ECONNREFUSED'));
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await submit();

      expect(res.status).toBe(502);
      expect(res.text).not.toContain('Note received.');
      expect(res.text).toContain('Your note could not be sent.');
      expect(errorSpy.mock.calls.some((call) => String(call[1]).includes('ECONNREFUSED'))).toBe(true);
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('tells a developer that no mail is configured, and does not pretend the note was sent', async () => {
    delete process.env.RESEND_API_KEY;
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    try {
      const res = await submit();

      expect(res.status).toBe(503);
      expect(fetchMock).not.toHaveBeenCalled();
      expect(res.text).not.toContain('Note received.');
      expect(res.text).toContain('mail is not configured on this server');
      expect(res.text).toContain('Hello there');
    } finally {
      logSpy.mockRestore();
    }
  });

  it('keeps the missing configuration to itself when the same thing happens in production', async () => {
    delete process.env.RESEND_API_KEY;
    const savedEnv = process.env.NODE_ENV;
    process.env.NODE_ENV = 'production';
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    try {
      const res = await submit();

      expect(res.status).toBe(503);
      expect(res.text).toContain('Your note could not be sent. Please try again in a little while.');
      expect(res.text).not.toContain('RESEND_API_KEY');
      expect(res.text).not.toContain('Note received.');
    } finally {
      process.env.NODE_ENV = savedEnv;
      logSpy.mockRestore();
    }
  });

  it('does not write the message body to the log when no mail transport is configured', async () => {
    delete process.env.RESEND_API_KEY;
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => undefined);
    try {
      await request(app)
        .post('/contact')
        .type('form')
        .send({ _csrf: csrf(), name: 'Ada', email: 'ada@example.com', subject: 'general', message: 'a private note' });

      const logged = logSpy.mock.calls.map((call) => call.join(' ')).join('\n');
      expect(logged).not.toContain('a private note');
      expect(logged).not.toContain('ada@example.com');
    } finally {
      logSpy.mockRestore();
    }
  });
});

describe('escapeHtml', () => {
  it('escapes the five characters that matter in element content and quoted attributes', () => {
    expect(escapeHtml(`<a href="x" title='y'>&</a>`)).toBe('&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;');
  });
});

describe('POST /guide', () => {
  it('answers a non-string email with the validation error instead of crashing', async () => {
    const res = await request(app).post('/guide').send({ _csrf: csrf(), email: ['a@example.com'] });

    expect(res.status).toBe(200);
    expect(res.text).toContain('valid email address');
    expect(mockQuery).not.toHaveBeenCalled();
  });
});

describe('async handlers: a rejected promise reaches the error middleware', () => {
  // Express 4 ignores the promise an async handler returns. Without
  // forwardAsyncErrors (utils/async-routes.ts) a rejection outside the
  // handler's own try/catch leaves the request hanging and, as an unhandled
  // rejection, ends the process on current Node versions.
  const opsKey = ['ops', 'test', 'key'].join('-');

  it('ops API: a failure before the handler enters its try block becomes a 500, not a hang', async () => {
    const original = process.env.OPS_API_KEY;
    process.env.OPS_API_KEY = opsKey;
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    // POST /v1/builds/cleanup-abandoned awaits pool.connect() before its try.
    (pool as unknown as { connect: ReturnType<typeof vi.fn> }).connect.mockRejectedValueOnce(new Error('pool exhausted'));
    try {
      const res = await request(app)
        .post('/ops/api/v1/builds/cleanup-abandoned')
        .set('Authorization', `Bearer ${opsKey}`)
        .timeout({ response: 3000 });

      expect(res.status).toBe(500);
    } finally {
      errorSpy.mockRestore();
      if (original === undefined) delete process.env.OPS_API_KEY;
      else process.env.OPS_API_KEY = original;
    }
  });

  it('every router module creates its router through forwardAsyncErrors', () => {
    const routesDir = join(__dirname, '..', '..', 'src', 'routes');
    const unwrapped: string[] = [];
    for (const file of readdirSync(routesDir).filter((name) => name.endsWith('.ts'))) {
      const source = readFileSync(join(routesDir, file), 'utf-8');
      const created = source.match(/=\s*(?:forwardAsyncErrors\()?\s*Router\(\)/g) ?? [];
      expect(created.length, `${file} creates a router`).toBeGreaterThan(0);
      if (created.some((expr) => !expr.includes('forwardAsyncErrors('))) unwrapped.push(file);
    }
    expect(unwrapped).toEqual([]);
  });

  it('handlers registered on the app itself are wrapped too', () => {
    interface Layer { route?: { path: string; methods: Record<string, boolean>; stack: Array<{ name: string }> } }
    const stack = (app as unknown as { _router: { stack: Layer[] } })._router.stack;
    const routeHandlers = (method: string, path: string): string[] => {
      const layer = stack.find((l) => l.route && l.route.path === path && l.route.methods[method]);
      if (!layer || !layer.route) throw new Error(`no ${method} ${path} route on the app`);
      return layer.route.stack.map((entry) => entry.name);
    };

    for (const [method, path] of [['post', '/contact'], ['post', '/guide'], ['get', '/health'], ['get', '/']] as const) {
      const names = routeHandlers(method, path);
      expect(names.length).toBeGreaterThan(0);
      for (const name of names) expect(name).toBe('forwardingHandler');
    }
  });

  it('settings lookups through app.get(name) still work after the wrap', () => {
    expect(app.get('view engine')).toBe('ejs');
    expect(app.get('trust proxy')).toBe(1);
  });
});

describe('error handler', () => {
  it('answers a malformed JSON body with 400, not 500', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const res = await request(app)
        .post('/guide')
        .set('Content-Type', 'application/json')
        .send('{"email": ');

      expect(res.status).toBe(400);
      expect(res.text).toContain('That request could not be processed.');
      // A client mistake is not logged as a server fault.
      expect(errorSpy.mock.calls.some((call) => String(call[0]).includes('Unhandled error'))).toBe(false);
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('answers an undecodable URL with 400', async () => {
    const res = await request(app).get('/builds/%E0%A4%A');
    expect(res.status).toBe(400);
  });
});

describe('response headers', () => {
  it('locks down the directives that do not inherit from default-src', async () => {
    const res = await request(app).get('/');
    const csp = res.headers['content-security-policy'] as string;

    expect(csp).toContain("base-uri 'self'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
    // Forms may post here, and follow our redirect to Stripe Checkout.
    expect(csp).toContain("form-action 'self' https://checkout.stripe.com");
    expect(csp).not.toMatch(/script-src[^;]*'unsafe-inline'/);
    expect(csp).not.toMatch(/script-src[^;]*'unsafe-eval'/);
  });

  it('switches the legacy XSS auditor off rather than on', async () => {
    const res = await request(app).get('/');
    expect(res.headers['x-xss-protection']).toBe('0');
    expect(res.headers['x-powered-by']).toBeUndefined();
  });

  // The headers every response carries. HSTS is the one that depends on the
  // environment; app.boot-security.test.ts covers it in production mode.
  const expectSecurityHeaders = (res: request.Response, label: string): void => {
    expect(res.headers['x-content-type-options'], label).toBe('nosniff');
    expect(res.headers['x-frame-options'], label).toBe('DENY');
    expect(res.headers['referrer-policy'], label).toBe('strict-origin-when-cross-origin');
    expect(res.headers['permissions-policy'], label).toBeTruthy();
    expect(res.headers['x-xss-protection'], label).toBe('0');
    const csp = res.headers['content-security-policy'] as string;
    expect(csp, label).toContain("frame-ancestors 'none'");
    expect(csp, label).toMatch(/script-src [^;]*'nonce-[A-Za-z0-9+/=]+'/);
  };

  it.each([['/style.css'], ['/favicon.svg'], ['/nav.js'], ['/favicon-32x32.png']])(
    'files served from public/ carry the same security headers as pages (%s)',
    async (path) => {
      const res = await request(app).get(path);

      // A real file, served by express.static, not an HTML page or a 404.
      expect(res.status, path).toBe(200);
      expect(res.headers['content-type'], path).not.toContain('text/html');
      expectSecurityHeaders(res, path);
    },
  );

  it('a missing file under public/ and a malformed request carry them too', async () => {
    const missing = await request(app).get('/not-a-file-in-public.css');
    expect(missing.status).toBe(404);
    expectSecurityHeaders(missing, 'missing file');

    // Rejected by the body parser, before any router runs.
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    try {
      const malformed = await request(app)
        .post('/guide')
        .set('Content-Type', 'application/json')
        .send('{"email": ');
      expect(malformed.status).toBe(400);
      expectSecurityHeaders(malformed, 'malformed body');
    } finally {
      errorSpy.mockRestore();
    }
  });

  it('pages still carry all of them, with a nonce that matches the inline scripts', async () => {
    const res = await request(app).get('/terms');

    expect(res.status).toBe(200);
    expectSecurityHeaders(res, '/terms');
    const nonce = (res.headers['content-security-policy'] as string).match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1];
    expect(nonce).toBeTruthy();
    expect(res.text).toContain(`nonce="${nonce}"`);
  });

  it('static files get a fresh nonce per request, like pages', async () => {
    const nonceOf = async (): Promise<string | undefined> =>
      ((await request(app).get('/style.css')).headers['content-security-policy'] as string).match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1];
    const [first, second] = [await nonceOf(), await nonceOf()];
    expect(first).toBeTruthy();
    expect(first).not.toBe(second);
  });

  it('shows the Admin link to the configured admin and to nobody else', async () => {
    const original = process.env.ADMIN_EMAIL;
    process.env.ADMIN_EMAIL = 'owner@example.com';
    try {
      const passwordHash = await bcrypt.hash('password123', 1);
      const login = async (email: string) => {
        const agent = request.agent(app);
        mockQuery.mockResolvedValueOnce({
          rows: [{ id: 'cust-' + email, email, name: 'N', password: passwordHash, email_verified: true }],
        });
        await agent.post('/login').type('form').send({ _csrf: csrf(), email, password: 'password123' });
        return agent;
      };

      const admin = await login('owner@example.com');
      expect((await admin.get('/terms')).text).toContain('href="/admin/builds"');

      const customer = await login('someone@example.com');
      const page = await customer.get('/terms');
      expect(page.text).toContain('href="/dashboard"');
      expect(page.text).not.toContain('href="/admin/builds"');

      // With ADMIN_EMAIL removed, the former admin is an ordinary customer.
      delete process.env.ADMIN_EMAIL;
      expect((await admin.get('/terms')).text).not.toContain('href="/admin/builds"');
    } finally {
      if (original === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = original;
    }
  });
});
