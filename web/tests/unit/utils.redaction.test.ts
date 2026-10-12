import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import request from 'supertest';

// Secrets that ride in URLs (the password reset, email verification, magic
// login and unsubscribe tokens) and the three places a URL is written down:
// the recent-error buffer behind GET /ops/api/v1/errors, the stored copy of
// every email (email_log.html_body), and the events sent to Sentry.

vi.mock('../../src/db/index', () => ({
  default: {
    query: vi.fn().mockResolvedValue({ rows: [], rowCount: 0 }),
    connect: vi.fn(),
    on: vi.fn(),
  },
}));

import app from '../../src/app';
import pool from '../../src/db/index';
import {
  getRecentErrors,
  loggablePath,
  recordRecentError,
  redactSecretParams,
  MAX_LOGGED_MESSAGE_CHARS,
  MAX_LOGGED_PATH_CHARS,
} from '../../src/utils/recent-errors';
import { sendPasswordResetEmail } from '../../src/services/email';
import { scrubSentryEvent } from '../../src/instrument';

// Assembled at run time so no secret-shaped literal sits in the source.
const SECRET = ['tok', 'en', 'value', '0123456789abcdef'].join('');
const mockQuery = pool.query as ReturnType<typeof vi.fn>;

describe('loggablePath', () => {
  it('keeps the path and drops the query string and fragment', () => {
    expect(loggablePath(`/reset-password?token=${SECRET}`)).toBe('/reset-password');
    expect(loggablePath(`/verify-email?token=${SECRET}&verified=1#top`)).toBe('/verify-email');
    expect(loggablePath(`https://example.com/unsubscribe?email=a%40example.com&token=${SECRET}`)).toBe(
      'https://example.com/unsubscribe'
    );
    expect(loggablePath('/dashboard')).toBe('/dashboard');
    expect(loggablePath(undefined)).toBe('');
  });

  it('cuts a very long path', () => {
    const long = `/${'a'.repeat(5000)}`;
    expect(loggablePath(long).length).toBeLessThanOrEqual(MAX_LOGGED_PATH_CHARS + 3);
  });
});

describe('redactSecretParams', () => {
  it('redacts a token wherever it sits in the query string and keeps the rest', () => {
    expect(redactSecretParams(`/reset-password?token=${SECRET}`)).toBe('/reset-password?token=[redacted]');
    expect(redactSecretParams(`/unsubscribe?email=a%40example.com&token=${SECRET}`)).toBe(
      '/unsubscribe?email=a%40example.com&token=[redacted]'
    );
    expect(redactSecretParams(`/x?token=${SECRET}&keep=1`)).toBe('/x?token=[redacted]&keep=1');
  });

  it('matches the ampersand as HTML writes it', () => {
    for (const amp of ['&amp;', '&#38;', '&#x26;']) {
      const html = `<a href="https://example.com/unsubscribe?email=a%40example.com${amp}token=${SECRET}">link</a>`;
      const out = redactSecretParams(html);
      expect(out, amp).not.toContain(SECRET);
      expect(out, amp).toContain(`${amp}token=[redacted]"`);
    }
  });

  it('matches a link nested inside another URL, encoded once or twice', () => {
    const once = `https://example.com/go?next=%2Freset-password%3Ftoken%3D${SECRET}%26verified%3D1`;
    expect(redactSecretParams(once)).toBe('https://example.com/go?next=%2Freset-password%3Ftoken%3D[redacted]%26verified%3D1');
    const lower = `https://example.com/go?next=%2Fa%3fx%3d1%26token%3d${SECRET}`;
    expect(redactSecretParams(lower)).not.toContain(SECRET);
    const twice = `https://example.com/go?u=%252Freset-password%253Ftoken%253D${SECRET}`;
    expect(redactSecretParams(twice)).not.toContain(SECRET);
  });

  it('covers the other names a credential travels under, and leaves ordinary parameters alone', () => {
    for (const name of ['reset_token', 'access_token', 'api_key', 'apikey', 'key', 'secret', 'signature', 'sig', 'code', 'password', 'session_id']) {
      expect(redactSecretParams(`/x?${name}=${SECRET}`), name).toBe(`/x?${name}=[redacted]`);
    }
    for (const url of ['/gallery?page=2&sort=new', '/try?utm_source=mail&utm_campaign=spring', '/x?monkey=1&zipcode=78701']) {
      expect(redactSecretParams(url)).toBe(url);
    }
  });

  it('redacts every occurrence, in upper or lower case', () => {
    const text = `first /a?TOKEN=${SECRET} then /b?x=1&Token=${SECRET}.`;
    const out = redactSecretParams(text);
    expect(out).not.toContain(SECRET);
    expect(out.match(/\[redacted\]/g)).toHaveLength(2);
  });
});

describe('recordRecentError', () => {
  beforeEach(() => {
    getRecentErrors().length = 0;
  });

  it('stores the path without its query string whatever the caller passes', () => {
    recordRecentError({
      timestamp: new Date(0).toISOString(),
      method: 'GET',
      path: `/reset-password?token=${SECRET}`,
      message: `could not load /reset-password?token=${SECRET}`,
      stack: `Error: could not load /reset-password?token=${SECRET}\n    at handler`,
    });
    const [entry] = getRecentErrors();
    expect(entry.path).toBe('/reset-password');
    expect(JSON.stringify(entry)).not.toContain(SECRET);
  });

  it('cuts a huge message', () => {
    recordRecentError({ timestamp: '', method: 'GET', path: '/x', message: 'm'.repeat(100_000) });
    expect(getRecentErrors()[0].message.length).toBeLessThanOrEqual(MAX_LOGGED_MESSAGE_CHARS + 3);
  });
});

describe('a 5xx on a token-carrying URL, read back through the ops API', () => {
  const opsKey = ['ops', 'redaction', 'key'].join('-');
  const original = process.env.OPS_API_KEY;

  beforeEach(() => {
    getRecentErrors().length = 0;
    process.env.OPS_API_KEY = opsKey;
  });
  afterEach(() => {
    if (original === undefined) delete process.env.OPS_API_KEY;
    else process.env.OPS_API_KEY = original;
  });

  it('GET /ops/api/v1/errors shows the path and not the token', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    // POST /v1/builds/cleanup-abandoned awaits pool.connect() before its try
    // block, so a failing pool reaches the app's error handler: the same
    // route the app takes when a store fails under a reset or verify link.
    (pool as unknown as { connect: ReturnType<typeof vi.fn> }).connect.mockRejectedValueOnce(new Error('pool exhausted'));
    try {
      const failed = await request(app)
        .post(`/ops/api/v1/builds/cleanup-abandoned?token=${SECRET}&reset_token=${SECRET}`)
        .set('Authorization', `Bearer ${opsKey}`);
      expect(failed.status).toBe(500);

      const res = await request(app).get('/ops/api/v1/errors').set('Authorization', `Bearer ${opsKey}`);
      expect(res.status).toBe(200);
      expect(res.body.errors).toHaveLength(1);
      expect(res.body.errors[0].path).toBe('/ops/api/v1/builds/cleanup-abandoned');
      expect(res.text).not.toContain(SECRET);
    } finally {
      errorSpy.mockRestore();
    }
  });
});

describe('the stored copy of an email', () => {
  beforeEach(() => {
    mockQuery.mockReset();
    mockQuery.mockResolvedValue({ rows: [] });
    process.env.RESEND_API_KEY = 'k';
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.RESEND_API_KEY;
  });

  const storedHtml = (): string => {
    const insert = mockQuery.mock.calls.find((call) => /INSERT INTO email_log/i.test(call[0] as string));
    expect(insert).toBeDefined();
    return (insert![1] as string[])[9];
  };

  it('has the token redacted when it is not the first parameter (the template writes "&amp;token=")', async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    await sendPasswordResetEmail('user@example.com', 'Alice', `https://example.com/reset-password?verified=1&token=${SECRET}`);

    const sent = JSON.parse(mockFetch.mock.calls[0][1].body as string) as { html: string };
    expect(sent.html).toContain(`&amp;token=${SECRET}`);

    const stored = storedHtml();
    expect(stored).toContain('reset-password?verified=1&amp;token=[redacted]');
    expect(stored).not.toContain(SECRET);
  });

  it('has the token redacted when the link is nested inside another URL', async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: 'r1' }) });
    vi.stubGlobal('fetch', mockFetch);
    const nested = `https://example.com/go?next=${encodeURIComponent(`/reset-password?token=${SECRET}`)}`;
    await sendPasswordResetEmail('user@example.com', 'Alice', nested);

    expect(JSON.parse(mockFetch.mock.calls[0][1].body as string).html).toContain(SECRET);
    expect(storedHtml()).not.toContain(SECRET);
  });
});

describe('scrubSentryEvent', () => {
  it('sends the path and nothing of the query string or the Referer', () => {
    const event = scrubSentryEvent({
      request: {
        url: `https://example.com/reset-password?token=${SECRET}`,
        query_string: `token=${SECRET}`,
        headers: {
          referer: `https://example.com/verify-email?token=${SECRET}`,
          'user-agent': 'test',
        },
      },
      contexts: { trace: { trace_id: 't', span_id: 's', data: { 'http.query': `?token=${SECRET}`, 'url.full': `https://example.com/reset-password?token=${SECRET}` } } },
      spans: [{ data: { 'http.target': `/reset-password?token=${SECRET}`, 'http.status_code': 500 } }] as never,
      breadcrumbs: [{ category: 'http', data: { url: `https://example.com/magic?token=${SECRET}` } }],
      message: `failed to load /reset-password?token=${SECRET}`,
      exception: { values: [{ type: 'Error', value: `GET /verify-email?token=${SECRET} failed` }] },
    });
    expect(event.message).toBe('failed to load /reset-password?token=[redacted]');
    expect(event.exception?.values?.[0].value).toBe('GET /verify-email?token=[redacted] failed');
    expect(JSON.stringify(event)).not.toContain(SECRET);
    expect(event.request?.url).toBe('https://example.com/reset-password');
    expect(event.request?.query_string).toBe('[redacted]');
    expect(event.request?.headers?.referer).toBe('https://example.com/verify-email');
    expect(event.request?.headers?.['user-agent']).toBe('test');
    expect((event.spans?.[0].data as Record<string, unknown>)['http.status_code']).toBe(500);
  });

  it('leaves an event with no request alone', () => {
    expect(scrubSentryEvent({})).toEqual({});
  });
});
