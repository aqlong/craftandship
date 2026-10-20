import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Request, Response, NextFunction } from 'express';
import { createHmac, randomBytes } from 'crypto';
import {
  generateCsrfToken,
  verifyHmacToken,
  validateCsrfToken,
} from '../../src/middleware/auth';

// Helper: create a minimal mock Request
function mockReq(overrides: Partial<Request> = {}): Request {
  return {
    body: {},
    headers: {},
    session: {} as Request['session'],
    originalUrl: '/',
    ...overrides,
  } as unknown as Request;
}

// Helper: create a minimal mock Response with vi.fn() stubs
function mockRes(): Response & { _status: number; _rendered: string | null; _body: unknown; _json: unknown } {
  const res = {
    _status: 200,
    _rendered: null as string | null,
    _body: null as unknown,
    _json: undefined as unknown,
    status(code: number) {
      this._status = code;
      return this;
    },
    render(view: string) {
      this._rendered = view;
      return this;
    },
    json(body: unknown) {
      this._json = body;
      this._body = body;
      return this;
    },
  };
  return res as unknown as Response & { _status: number; _rendered: string | null; _body: unknown; _json: unknown };
}

describe('generateCsrfToken', () => {
  it('returns nonce.tag.mac: a hex nonce, the session tag, and a hex HMAC over both', () => {
    const token = generateCsrfToken(mockReq({ sessionID: 'session-one' } as Partial<Request>));
    expect(typeof token).toBe('string');
    const parts = token.split('.');
    expect(parts.length).toBe(3);
    expect(parts[0]).toMatch(/^[0-9a-f]{32}$/);
    expect(parts[1]).toMatch(/^[0-9a-f]{32}$/);
    expect(parts[2]).toMatch(/^[0-9a-f]{64}$/);
    // The tag is derived from the session id and does not contain it.
    expect(token).not.toContain('session-one');
  });

  it('a token minted outside a request (no session id) carries the no-session tag', () => {
    expect(generateCsrfToken(mockReq()).split('.')[1]).toBe('nosession');
  });

  it('generates unique tokens on each call', () => {
    const req = mockReq();
    const t1 = generateCsrfToken(req);
    const t2 = generateCsrfToken(req);
    expect(t1).not.toBe(t2);
  });
});

describe('verifyHmacToken', () => {
  it('returns true for a valid token just generated', () => {
    const req = mockReq();
    const token = generateCsrfToken(req);
    expect(verifyHmacToken(token)).toBe(true);
  });

  it('returns false for a tampered token', () => {
    const req = mockReq();
    const token = generateCsrfToken(req);
    const tampered = token.slice(0, -4) + 'dead';
    expect(verifyHmacToken(tampered)).toBe(false);
  });

  it('returns false for an empty string', () => {
    expect(verifyHmacToken('')).toBe(false);
  });

  it('returns false for a token with wrong HMAC', () => {
    const req = mockReq();
    const token = generateCsrfToken(req);
    const parts = token.split('.');
    // Replace HMAC with zeros of same length
    const badToken = `${parts[0]}.${parts[1]}.${'0'.repeat(parts[2].length)}`;
    expect(verifyHmacToken(badToken)).toBe(false);
  });

  it('returns false for a token in the earlier two-part form (nonce.mac), even one this secret signed', () => {
    const nonce = randomBytes(16).toString('hex');
    const mac = createHmac('sha256', getSessionSecret()).update(nonce).digest('hex');
    expect(verifyHmacToken(`${nonce}.${mac}`)).toBe(false);
  });

  it('returns false for a token with no dot separator', () => {
    expect(verifyHmacToken('notadottoken')).toBe(false);
  });

  it('returns false when nonce portion is empty', () => {
    // Token starts with a dot: empty nonce
    expect(verifyHmacToken('.abc123')).toBe(false);
  });
});

describe('validateCsrfToken middleware', () => {
  it('calls next() when body _csrf is a valid HMAC token', () => {
    const req = mockReq();
    const token = generateCsrfToken(req);
    req.body = { _csrf: token };

    const res = mockRes();
    const next: NextFunction = vi.fn() as unknown as NextFunction;

    validateCsrfToken(req, res, next);

    expect(next).toHaveBeenCalledOnce();
    expect(res._status).toBe(200); // unchanged, not set to 403
  });

  it('calls next() when header x-csrf-token is a valid HMAC token', () => {
    const req = mockReq();
    const token = generateCsrfToken(req);
    req.headers = { 'x-csrf-token': token };

    const res = mockRes();
    const next: NextFunction = vi.fn() as unknown as NextFunction;

    validateCsrfToken(req, res, next);

    expect(next).toHaveBeenCalledOnce();
  });

  it('returns 403 when _csrf is missing', () => {
    const req = mockReq();
    req.body = {};

    const res = mockRes();
    const next: NextFunction = vi.fn() as unknown as NextFunction;

    validateCsrfToken(req, res, next);

    expect(next).not.toHaveBeenCalled();
    expect(res._status).toBe(403);
    // The 403 page, not the 500 page: the response must say what happened.
    expect(res._rendered).toBe('403');
  });

  it('returns 403 when _csrf is tampered', () => {
    const req = mockReq();
    const token = generateCsrfToken(req);
    const tampered = token.slice(0, -4) + 'dead';
    req.body = { _csrf: tampered };

    const res = mockRes();
    const next: NextFunction = vi.fn() as unknown as NextFunction;

    validateCsrfToken(req, res, next);

    expect(next).not.toHaveBeenCalled();
    expect(res._status).toBe(403);
  });

  it('returns 403 JSON (not HTML) when x-csrf-token header is invalid', () => {
    const req = mockReq();
    req.headers = { 'x-csrf-token': 'bad-token' };

    const res = mockRes();
    const next: NextFunction = vi.fn() as unknown as NextFunction;

    validateCsrfToken(req, res, next);

    expect(next).not.toHaveBeenCalled();
    expect(res._status).toBe(403);
    expect(res._json).toBeDefined();
    expect(res._rendered).toBeNull(); // json() was called, not render()
  });
});

describe('the CSRF token is bound to the session it was issued in', () => {
  const issuedIn = (sessionID: string): string => generateCsrfToken(mockReq({ sessionID } as Partial<Request>));
  const validate = (sessionID: string | undefined, token: string): { passed: boolean; status: number } => {
    const req = mockReq({ sessionID, body: { _csrf: token } } as Partial<Request>);
    const res = mockRes();
    const next = vi.fn();
    validateCsrfToken(req, res, next as unknown as NextFunction);
    return { passed: next.mock.calls.length === 1, status: res._status };
  };

  it('is accepted from the session it was issued in', () => {
    expect(validate('session-a', issuedIn('session-a'))).toEqual({ passed: true, status: 200 });
  });

  it('is refused from any other session, and from a request with no session', () => {
    const token = issuedIn('session-a');
    expect(validate('session-b', token)).toEqual({ passed: false, status: 403 });
    expect(validate(undefined, token)).toEqual({ passed: false, status: 403 });
  });

  it('a tag copied onto another session\'s token breaks its HMAC', () => {
    const [nonce, , mac] = issuedIn('session-a').split('.');
    const otherTag = issuedIn('session-b').split('.')[1];
    expect(validate('session-b', `${nonce}.${otherTag}.${mac}`)).toEqual({ passed: false, status: 403 });
  });

  it('verifyHmacToken checks the binding when given the request, and only the HMAC without it', () => {
    const token = issuedIn('session-a');
    expect(verifyHmacToken(token, mockReq({ sessionID: 'session-a' } as Partial<Request>))).toBe(true);
    expect(verifyHmacToken(token, mockReq({ sessionID: 'session-b' } as Partial<Request>))).toBe(false);
    expect(verifyHmacToken(token)).toBe(true);
  });

  it('a token minted with no session id (only code outside a request does that) is accepted from any session', () => {
    expect(validate('session-b', generateCsrfToken(mockReq())).passed).toBe(true);
  });

  it('rendering a page writes to the session, so the session (and its cookie) outlives the page', () => {
    const session = {} as Request['session'];
    const render = vi.fn();
    const res = { render } as unknown as Response;
    const req = mockReq({ sessionID: 'session-a', session, res } as Partial<Request>);
    generateCsrfToken(req);
    generateCsrfToken(req); // a second token in the same request wraps render once
    expect(session.csrfBound).toBeUndefined();
    (res.render as (view: string, locals: object) => void)('login', { a: 1 });
    expect(session.csrfBound).toBe(true);
    expect(render).toHaveBeenCalledOnce();
    expect(render).toHaveBeenCalledWith('login', { a: 1 });
  });
});

// ---------------------------------------------------------------------------
// Hardening added in the public-release security review
// ---------------------------------------------------------------------------

import {
  resolveSessionSecret,
  initSessionSecret,
  getSessionSecret,
  SessionSecretError,
  SESSION_SECRET_PLACEHOLDERS,
  SESSION_SECRET_PLACEHOLDER_WORDS,
  MIN_SESSION_SECRET_LENGTH,
  MIN_SESSION_SECRET_DISTINCT_CHARS,
  MAX_SESSION_SECRET_REPEATED_BLOCK,
  sessionSecretWeakness,
  timingSafeEqualStrings,
  isAdminSession,
  safeReturnTo,
  isCrossOriginWrite,
  rejectCrossOriginWrites,
  isSessionPastAbsoluteLifetime,
  SESSION_ABSOLUTE_MAX_MS,
} from '../../src/middleware/auth';

// Built at runtime so no secret-shaped literal sits in the repo: 48 random bytes, hex.
const STRONG_SECRET = randomBytes(48).toString('hex');

describe('resolveSessionSecret', () => {
  it('uses a real secret as configured, in every environment', () => {
    for (const NODE_ENV of ['production', 'staging', 'development', 'test', undefined]) {
      const result = resolveSessionSecret({ NODE_ENV, SESSION_SECRET: STRONG_SECRET });
      expect(result).toEqual({ secret: STRONG_SECRET, ephemeral: false, warning: null });
    }
  });

  it.each([...SESSION_SECRET_PLACEHOLDERS, '', '   ', undefined])(
    'refuses to run on a missing or placeholder secret (%j) in production',
    (value) => {
      expect(() => resolveSessionSecret({ NODE_ENV: 'production', SESSION_SECRET: value })).toThrow(SessionSecretError);
    }
  );

  it('treats an unset NODE_ENV like production: a forgotten variable must not unlock the placeholder', () => {
    for (const value of [...SESSION_SECRET_PLACEHOLDERS, undefined]) {
      expect(() => resolveSessionSecret({ SESSION_SECRET: value })).toThrow(/NODE_ENV is "\(unset\)"/);
    }
    expect(() => resolveSessionSecret({ NODE_ENV: 'staging' })).toThrow(SessionSecretError);
  });

  it('refuses a short secret outside development and test', () => {
    const short = 'x'.repeat(MIN_SESSION_SECRET_LENGTH - 1);
    expect(() => resolveSessionSecret({ NODE_ENV: 'production', SESSION_SECRET: short })).toThrow(/too short/);
    expect(resolveSessionSecret({ NODE_ENV: 'development', SESSION_SECRET: short }).secret).toBe(short);
  });

  // Long enough is not the same as real. Each of these is 32 characters or
  // more and used to be accepted in production.
  it.each([
    ['one character repeated', 'a'.repeat(36), /fewer than 8 different characters/],
    ['two characters alternating', 'ab'.repeat(20), /fewer than 8 different characters/],
    ['seven different characters', 'abcdefg'.repeat(6) + 'gfedcba', /fewer than 8 different characters/],
    ['a short block repeated', 'abcdefgh'.repeat(5), /block of 8 characters repeated/],
    ['a 16-character block repeated, last copy cut short', '0123456789abcdef'.repeat(2) + '0123', /block of 16 characters repeated/],
    ['the placeholder written twice', 'change-me-in-production-change-me-in-production', /placeholder word "changeme"/],
    ['a placeholder word in other casing and punctuation', 'Please_CHANGE.ME_before-you-deploy-this-app!', /placeholder word "changeme"/],
    ['an example value', 'this-is-an-example-session-key-0123456789', /placeholder word "example"/],
    ['a value that says what it is', 'my-super-long-session-secret-value-1234567', /placeholder word "sessionsecret"/],
    ['a password-style value', 'correct-horse-battery-staple-password-1', /placeholder word "password"/],
  ])('refuses %s outside development and test', (_label, value, reason) => {
    expect(value.length).toBeGreaterThanOrEqual(MIN_SESSION_SECRET_LENGTH);
    for (const NODE_ENV of ['production', 'staging', undefined]) {
      expect(() => resolveSessionSecret({ NODE_ENV, SESSION_SECRET: value })).toThrow(SessionSecretError);
      expect(() => resolveSessionSecret({ NODE_ENV, SESSION_SECRET: value })).toThrow(reason);
    }
    // Development and test still take whatever was configured.
    expect(resolveSessionSecret({ NODE_ENV: 'development', SESSION_SECRET: value }).secret).toBe(value);
    expect(resolveSessionSecret({ NODE_ENV: 'test', SESSION_SECRET: value }).secret).toBe(value);
  });

  it('accepts what the documented generators produce, every time', () => {
    for (let i = 0; i < 500; i += 1) {
      expect(sessionSecretWeakness(randomBytes(32).toString('hex'))).toBeNull();
      expect(sessionSecretWeakness(randomBytes(32).toString('base64'))).toBeNull();
      expect(sessionSecretWeakness(randomBytes(24).toString('base64url'))).toBeNull();
    }
  });

  it('accepts a long value that merely looks orderly: this is a stand-in check, not an entropy estimate', () => {
    expect(sessionSecretWeakness('abcdefghijklmnopqrstuvwxyz012345')).toBeNull();
    expect(sessionSecretWeakness('the quick brown fox jumps over the lazy dog')).toBeNull();
  });

  it('the rule is exactly: length, distinct characters, no short repeated block, no placeholder word', () => {
    expect(MIN_SESSION_SECRET_LENGTH).toBe(32);
    expect(MIN_SESSION_SECRET_DISTINCT_CHARS).toBe(8);
    expect(MAX_SESSION_SECRET_REPEATED_BLOCK).toBe(16);
    expect([...SESSION_SECRET_PLACEHOLDER_WORDS].sort()).toEqual(
      ['changeme', 'devsecret', 'example', 'insecure', 'password', 'placeholder', 'replaceme', 'sessionsecret', 'yoursecret']
    );
    // Short words would collide with random values; none is allowed in the list.
    for (const word of SESSION_SECRET_PLACEHOLDER_WORDS) expect(word.length).toBeGreaterThanOrEqual(7);
    // Boundaries.
    expect(sessionSecretWeakness('abcdefgh'.repeat(3) + 'abcdefg')).toMatch(/too short/); // 31 characters
    expect(sessionSecretWeakness('abcdefgh'.repeat(4))).toMatch(/block of 8/); // 32, 8 distinct, but periodic
    const eightDistinctNotPeriodic = 'abcdefgh' + 'hgfedcba' + 'aabbccdd' + 'eeffgghh';
    expect(new Set(eightDistinctNotPeriodic).size).toBe(8);
    expect(sessionSecretWeakness(eightDistinctNotPeriodic)).toBeNull();
    // A block of 17 repeated is past the block limit and has enough variety: accepted.
    expect(sessionSecretWeakness('abcdefghijklmnopq'.repeat(2))).toBeNull();
  });

  it('never signs with a shared literal in development: a placeholder becomes a random per-process secret', () => {
    // .env.example ships NODE_ENV=development with the placeholder, so this is
    // the quickstart path. It must boot, and it must not use the placeholder.
    const first = resolveSessionSecret({ NODE_ENV: 'development', SESSION_SECRET: 'change-me-in-production' });
    const second = resolveSessionSecret({ NODE_ENV: 'development', SESSION_SECRET: 'change-me-in-production' });

    expect(first.ephemeral).toBe(true);
    expect(first.secret).toMatch(/^[0-9a-f]{64}$/);
    expect(SESSION_SECRET_PLACEHOLDERS).not.toContain(first.secret);
    expect(first.secret).not.toBe(second.secret);
    expect(first.warning).toMatch(/SESSION_SECRET is unset or still a placeholder/);
  });

  it('stays quiet in test', () => {
    const result = resolveSessionSecret({ NODE_ENV: 'test' });
    expect(result.ephemeral).toBe(true);
    expect(result.warning).toBeNull();
  });
});

describe('initSessionSecret / getSessionSecret', () => {
  it('publishes the resolved secret to the environment so direct readers never hit a literal fallback', () => {
    const env: Record<string, string | undefined> = { NODE_ENV: 'development' };
    try {
      const resolution = initSessionSecret(env);
      expect(env.SESSION_SECRET).toBe(resolution.secret);
      expect(getSessionSecret()).toBe(resolution.secret);
      expect(SESSION_SECRET_PLACEHOLDERS).not.toContain(env.SESSION_SECRET);
    } finally {
      // Restore the process-wide secret for the other tests in this file.
      initSessionSecret();
    }
  });

  it('CSRF tokens are signed with the resolved secret, not the old literal', () => {
    const token = generateCsrfToken(mockReq());
    const [nonce, tag, mac] = token.split('.');
    for (const literal of SESSION_SECRET_PLACEHOLDERS) {
      const forged = createHmac('sha256', literal).update(`${nonce}|${tag}`).digest('hex');
      expect(forged).not.toBe(mac);
      expect(verifyHmacToken(`${nonce}.${tag}.${forged}`)).toBe(false);
    }
  });
});

describe('timingSafeEqualStrings', () => {
  it('compares by value', () => {
    expect(timingSafeEqualStrings('abc', 'abc')).toBe(true);
    expect(timingSafeEqualStrings('abc', 'abd')).toBe(false);
    expect(timingSafeEqualStrings('abc', 'abcd')).toBe(false);
  });

  it('does not throw when string lengths match but byte lengths differ', () => {
    // 'e' with an acute accent is one UTF-16 unit and two UTF-8 bytes.
    const accented = String.fromCharCode(0xe9).repeat(3);
    expect(accented.length).toBe('abc'.length);
    expect(() => timingSafeEqualStrings(accented, 'abc')).not.toThrow();
    expect(timingSafeEqualStrings(accented, 'abc')).toBe(false);
  });

  it('rejects non-strings', () => {
    expect(timingSafeEqualStrings(undefined, undefined)).toBe(false);
    expect(timingSafeEqualStrings(['a'], 'a')).toBe(false);
    expect(timingSafeEqualStrings('', undefined)).toBe(false);
  });
});

describe('isAdminSession', () => {
  const withAdmin = (value: string | undefined, fn: () => void): void => {
    const original = process.env.ADMIN_EMAIL;
    if (value === undefined) delete process.env.ADMIN_EMAIL;
    else process.env.ADMIN_EMAIL = value;
    try {
      fn();
    } finally {
      if (original === undefined) delete process.env.ADMIN_EMAIL;
      else process.env.ADMIN_EMAIL = original;
    }
  };
  const sessionReq = (session: Record<string, unknown> | undefined): Request =>
    ({ session } as unknown as Request);
  // What POST /login and the email-verification link put in the session.
  const loggedIn = (email: string | undefined): Record<string, unknown> =>
    ({ customerId: 'c1', customerEmail: email, authenticatedAt: Date.now() });

  it('is true only for a password-authenticated session whose email equals ADMIN_EMAIL', () => {
    withAdmin('admin@example.com', () => {
      expect(isAdminSession(sessionReq(loggedIn('admin@example.com')))).toBe(true);
      expect(isAdminSession(sessionReq(loggedIn('user@example.com')))).toBe(false);
    });
  });

  it('fails closed when ADMIN_EMAIL is unset: undefined === undefined is not admin', () => {
    withAdmin(undefined, () => {
      expect(isAdminSession(sessionReq({}))).toBe(false);
      expect(isAdminSession(sessionReq(loggedIn(undefined)))).toBe(false);
      expect(isAdminSession(sessionReq(undefined))).toBe(false);
    });
  });

  it('fails closed when ADMIN_EMAIL is empty, even against an empty session email', () => {
    withAdmin('', () => {
      expect(isAdminSession(sessionReq(loggedIn('')))).toBe(false);
    });
  });

  it('requires a logged-in customer id, not just a matching email value', () => {
    withAdmin('admin@example.com', () => {
      expect(isAdminSession(sessionReq({ customerEmail: 'admin@example.com', authenticatedAt: Date.now() }))).toBe(false);
    });
  });

  it('refuses a session that carries the admin email but was never password-authenticated', () => {
    // The shape POST /lighthouse/bypass leaves behind: a caller-chosen email
    // written into the session with no password check and no login stamp.
    withAdmin('admin@example.com', () => {
      expect(isAdminSession(sessionReq({ customerId: 'c1', customerEmail: 'admin@example.com' }))).toBe(false);
    });
  });

  it('is an exact match: a differently cased value does not match', () => {
    withAdmin('Admin@Example.com', () => {
      expect(isAdminSession(sessionReq(loggedIn('admin@example.com')))).toBe(false);
    });
  });
});

describe('safeReturnTo', () => {
  it('accepts same-site absolute paths', () => {
    expect(safeReturnTo('/dashboard')).toBe('/dashboard');
    expect(safeReturnTo('/builds/abc?tab=logs#top')).toBe('/builds/abc?tab=logs#top');
  });

  it.each([
    ['absolute URL', 'https://evil.example.test/'],
    ['protocol-relative', '//evil.example.test'],
    ['backslash authority', '/\\evil.example.test'],
    ['backslash later in the path', '/a\\b'],
    ['javascript scheme', 'javascript:alert(1)'],
    ['relative path', 'dashboard'],
    ['empty', ''],
    ['tab', '/\t/evil.example.test'],
    ['newline', '/dashboard\r\nSet-Cookie: x=1'],
    ['array', ['/dashboard']],
    ['undefined', undefined],
    ['overlong', '/' + 'a'.repeat(2001)],
  ])('rejects %s', (_label, value) => {
    expect(safeReturnTo(value)).toBeNull();
  });
});

describe('isCrossOriginWrite', () => {
  const req = (method: string, headers: Record<string, string>): Request =>
    ({ method, headers } as unknown as Request);

  it('never blocks safe methods', () => {
    expect(isCrossOriginWrite(req('GET', { 'sec-fetch-site': 'cross-site', origin: 'https://evil.example.test', host: 'app.example.test' }))).toBe(false);
    expect(isCrossOriginWrite(req('HEAD', { 'sec-fetch-site': 'cross-site' }))).toBe(false);
  });

  it('trusts Sec-Fetch-Site when the browser sends it', () => {
    expect(isCrossOriginWrite(req('POST', { 'sec-fetch-site': 'same-origin' }))).toBe(false);
    expect(isCrossOriginWrite(req('POST', { 'sec-fetch-site': 'none' }))).toBe(false);
    expect(isCrossOriginWrite(req('POST', { 'sec-fetch-site': 'cross-site' }))).toBe(true);
    // A sibling subdomain is not us.
    expect(isCrossOriginWrite(req('POST', { 'sec-fetch-site': 'same-site' }))).toBe(true);
    expect(isCrossOriginWrite(req('DELETE', { 'sec-fetch-site': 'cross-site' }))).toBe(true);
  });

  it('falls back to Origin versus Host for browsers without Sec-Fetch-Site', () => {
    expect(isCrossOriginWrite(req('POST', { origin: 'https://app.example.test', host: 'app.example.test' }))).toBe(false);
    expect(isCrossOriginWrite(req('POST', { origin: 'http://localhost:4000', host: 'localhost:4000' }))).toBe(false);
    expect(isCrossOriginWrite(req('POST', { origin: 'https://evil.example.test', host: 'app.example.test' }))).toBe(true);
    expect(isCrossOriginWrite(req('POST', { origin: 'https://app.example.test:8443', host: 'app.example.test' }))).toBe(true);
    // Sandboxed frames and some redirects send the literal string "null".
    expect(isCrossOriginWrite(req('POST', { origin: 'null', host: 'app.example.test' }))).toBe(true);
  });

  it('lets server-to-server callers through: no browser headers, no ambient cookies to abuse', () => {
    expect(isCrossOriginWrite(req('POST', { host: 'app.example.test' }))).toBe(false);
  });

  it('middleware answers 403 for a cross-origin write and calls next otherwise', () => {
    const blocked = mockRes();
    const nextBlocked = vi.fn();
    rejectCrossOriginWrites(req('POST', { 'sec-fetch-site': 'cross-site' }), blocked, nextBlocked as unknown as NextFunction);
    expect(blocked._status).toBe(403);
    expect(nextBlocked).not.toHaveBeenCalled();

    const allowed = mockRes();
    const nextAllowed = vi.fn();
    rejectCrossOriginWrites(req('POST', { 'sec-fetch-site': 'same-origin' }), allowed, nextAllowed as unknown as NextFunction);
    expect(nextAllowed).toHaveBeenCalledOnce();
  });
});

describe('isSessionPastAbsoluteLifetime', () => {
  const now = 1_800_000_000_000;

  it('expires an authenticated session older than the absolute cap, however recently it was used', () => {
    expect(isSessionPastAbsoluteLifetime({ customerId: 'c1', authenticatedAt: now - SESSION_ABSOLUTE_MAX_MS - 1 }, now)).toBe(true);
    expect(isSessionPastAbsoluteLifetime({ customerId: 'c1', authenticatedAt: now - SESSION_ABSOLUTE_MAX_MS + 1000 }, now)).toBe(false);
  });

  it('leaves anonymous sessions and sessions without a login timestamp alone', () => {
    expect(isSessionPastAbsoluteLifetime({ authenticatedAt: 0 }, now)).toBe(false);
    expect(isSessionPastAbsoluteLifetime({ customerId: 'c1' }, now)).toBe(false);
    expect(isSessionPastAbsoluteLifetime(undefined, now)).toBe(false);
  });
});

describe('CSRF token input types', () => {
  it('rejects a non-string token instead of throwing', () => {
    expect(verifyHmacToken(['a.b'] as unknown)).toBe(false);
    expect(verifyHmacToken({ lastIndexOf: () => 1 } as unknown)).toBe(false);
    expect(verifyHmacToken(undefined)).toBe(false);

    const req = mockReq();
    req.body = { _csrf: { nested: 'object' } };
    const res = mockRes();
    const next = vi.fn();
    expect(() => validateCsrfToken(req, res, next as unknown as NextFunction)).not.toThrow();
    expect(next).not.toHaveBeenCalled();
    expect(res._status).toBe(403);
  });
});

describe('enforceSessionLifetime middleware', () => {
  it('swaps an over-age authenticated session for a fresh empty one and continues', async () => {
    const { enforceSessionLifetime } = await import('../../src/middleware/auth');
    const regenerate = vi.fn((done: (err?: Error) => void) => done());
    const req = {
      session: { customerId: 'c1', authenticatedAt: Date.now() - SESSION_ABSOLUTE_MAX_MS - 60_000, regenerate },
    } as unknown as Request;
    const next = vi.fn();

    enforceSessionLifetime(req, mockRes(), next as unknown as NextFunction);

    expect(regenerate).toHaveBeenCalledOnce();
    expect(next).toHaveBeenCalledOnce();
    expect(next.mock.calls[0]).toEqual([]);
  });

  it('does not touch a session inside its lifetime, or an anonymous one', async () => {
    const { enforceSessionLifetime } = await import('../../src/middleware/auth');
    for (const session of [
      { customerId: 'c1', authenticatedAt: Date.now() - 1000 },
      { anonSessionId: 'anon' },
    ]) {
      const regenerate = vi.fn();
      const next = vi.fn();
      enforceSessionLifetime({ session: { ...session, regenerate } } as unknown as Request, mockRes(), next as unknown as NextFunction);
      expect(regenerate).not.toHaveBeenCalled();
      expect(next).toHaveBeenCalledOnce();
    }
  });

  it('passes a store failure to the error handler', async () => {
    const { enforceSessionLifetime } = await import('../../src/middleware/auth');
    const failure = new Error('store down');
    const req = {
      session: {
        customerId: 'c1',
        authenticatedAt: 0,
        regenerate: (done: (err?: Error) => void) => done(failure),
      },
    } as unknown as Request;
    const next = vi.fn();

    enforceSessionLifetime(req, mockRes(), next as unknown as NextFunction);

    expect(next).toHaveBeenCalledWith(failure);
  });
});
