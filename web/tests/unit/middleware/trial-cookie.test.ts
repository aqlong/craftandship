import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Pool } from 'pg';
import type { Request, Response, NextFunction } from 'express';
import {
  trialCookie,
  identifyTrialCookie,
  rotateTrialCookie,
  createMintLimiter,
  _internals,
} from '../../../src/middleware/trial-cookie';

const { readCookie, isValidTokenShape, generateToken, TRIAL_COOKIE_NAME, TRIAL_TTL_DAYS } =
  _internals;

describe('trial-cookie middleware: helpers', () => {
  it('TRIAL_COOKIE_NAME is the bare name in development and test (plain HTTP; see hostOnlyCookieName)', () => {
    expect(TRIAL_COOKIE_NAME).toBe('cas_harbor_trial');
  });

  it('TRIAL_TTL_DAYS matches the 7-day "just talk" trial', () => {
    expect(TRIAL_TTL_DAYS).toBe(7);
  });

  it('readCookie returns the named value from a single-cookie header', () => {
    const req = { headers: { cookie: 'cas_harbor_trial=abc123' } };
    expect(readCookie(req as never, 'cas_harbor_trial')).toBe('abc123');
  });

  it('readCookie returns the named value from a multi-cookie header', () => {
    const req = {
      headers: { cookie: 'cas.sid=xyz; cas_harbor_trial=tok456; other=foo' },
    };
    expect(readCookie(req as never, 'cas_harbor_trial')).toBe('tok456');
  });

  it('readCookie returns null when the cookie is missing', () => {
    const req = { headers: { cookie: 'cas.sid=xyz' } };
    expect(readCookie(req as never, 'cas_harbor_trial')).toBeNull();
  });

  it('readCookie returns null when there is no Cookie header', () => {
    expect(readCookie({ headers: {} } as never, 'cas_harbor_trial')).toBeNull();
  });

  it('readCookie URL-decodes the value', () => {
    const req = { headers: { cookie: 'cas_harbor_trial=hello%20world' } };
    expect(readCookie(req as never, 'cas_harbor_trial')).toBe('hello world');
  });

  it('isValidTokenShape accepts a 64-char hex string (32 random bytes, hex encoded)', () => {
    expect(isValidTokenShape('a'.repeat(64))).toBe(true);
    expect(isValidTokenShape('0123456789abcdef'.repeat(4))).toBe(true);
    expect(isValidTokenShape('0123456789ABCDEF'.repeat(4))).toBe(true);
  });

  it('isValidTokenShape rejects malformed tokens', () => {
    expect(isValidTokenShape('')).toBe(false);
    expect(isValidTokenShape('too-short')).toBe(false);
    expect(isValidTokenShape('a'.repeat(63))).toBe(false);
    expect(isValidTokenShape('a'.repeat(65))).toBe(false);
    expect(isValidTokenShape('z'.repeat(64))).toBe(false); // non-hex char
    expect(isValidTokenShape('not hex at all '.repeat(5))).toBe(false);
  });

  it('readCookie treats malformed percent-encoding as "no cookie" instead of throwing', () => {
    expect(readCookie({ headers: { cookie: 'cas_harbor_trial=%' } } as never, 'cas_harbor_trial')).toBeNull();
    expect(
      readCookie({ headers: { cookie: 'cas_harbor_trial=%E0%A4%A' } } as never, 'cas_harbor_trial')
    ).toBeNull();
  });

  it('generateToken produces a 64-char hex string', () => {
    const t = generateToken();
    expect(t).toMatch(/^[0-9a-f]{64}$/);
    expect(isValidTokenShape(t)).toBe(true);
  });

  it('generateToken returns different tokens on consecutive calls', () => {
    const a = generateToken();
    const b = generateToken();
    expect(a).not.toBe(b);
  });
});

// ----------------------------------------------------------------------
// Middleware-handler coverage. Mocks the pg Pool. Verifies the full
// request lifecycle: read cookie -> load active row -> mint fresh trial
// when the cookie is missing / invalid / unmatched / expired.
// ----------------------------------------------------------------------

const VALID_TOKEN = 'a'.repeat(64);
const ACTIVE_TRIAL_ROW = {
  trial_id: '11111111-2222-3333-4444-555555555555',
  cookie_token: VALID_TOKEN,
  companion_name: 'Haven',
  companion_assigned: 'Haven',
  started_at: new Date('2026-04-26T10:00:00Z'),
  expires_at: new Date('2099-04-26T10:00:00Z'),
  last_seen_at: new Date('2026-04-26T11:00:00Z'),
};

function makePool(queryFn: ReturnType<typeof vi.fn>): Pool {
  return { query: queryFn } as unknown as Pool;
}

function makeReq(cookieHeader?: string): Request {
  return {
    headers: cookieHeader ? { cookie: cookieHeader } : {},
  } as unknown as Request;
}

function makeRes(): { res: Response; cookieFn: ReturnType<typeof vi.fn> } {
  const cookieFn = vi.fn();
  return {
    res: { cookie: cookieFn } as unknown as Response,
    cookieFn,
  };
}

describe('trialCookie middleware-handler', () => {
  let next: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    next = vi.fn();
  });

  it('attaches an active trial when the cookie token matches a row', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    await handler(req, res, next as unknown as NextFunction);

    expect(req.harborTrial).toEqual(ACTIVE_TRIAL_ROW);
    expect(next).toHaveBeenCalledWith();
    expect(cookieFn).not.toHaveBeenCalled();
  });

  it('mints a fresh trial when no cookie is present + sets the cookie', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq();
    const { res, cookieFn } = makeRes();
    await handler(req, res, next as unknown as NextFunction);

    expect(req.harborTrial).toEqual(ACTIVE_TRIAL_ROW);
    expect(cookieFn).toHaveBeenCalledOnce();
    const [cookieName, cookieValue, opts] = cookieFn.mock.calls[0];
    expect(cookieName).toBe('cas_harbor_trial');
    expect(cookieValue).toBe(VALID_TOKEN);
    expect(opts).toMatchObject({
      httpOnly: true,
      sameSite: 'lax',
      path: '/',
    });
    expect(opts.maxAge).toBe(7 * 24 * 60 * 60 * 1000);
  });

  it('mints a new trial when the cookie token has invalid shape (skips SELECT)', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq('cas_harbor_trial=garbage');
    const { res, cookieFn } = makeRes();
    await handler(req, res, next as unknown as NextFunction);
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(queryFn.mock.calls[0][0]).toMatch(/INSERT INTO trial_keepers/);
    expect(cookieFn).toHaveBeenCalledOnce();
  });

  it('mints a fresh trial when the cookie token does not match any active row', async () => {
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [] })                    // SELECT existing: nothing
      .mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });   // INSERT new
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    await handler(req, res, next as unknown as NextFunction);

    expect(queryFn).toHaveBeenCalledTimes(2);
    const selectSql = queryFn.mock.calls[0][0] as string;
    expect(selectSql).toMatch(/SELECT trial_id, cookie_token/);
    expect(selectSql).toMatch(/expires_at > NOW\(\)/);
    expect(cookieFn).toHaveBeenCalledOnce();
    expect(req.harborTrial).toEqual(ACTIVE_TRIAL_ROW);
  });

  it('SELECT excludes expired rows in SQL and reads released_at so the code can tell a released row', async () => {
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [] })
      .mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res } = makeRes();
    await handler(req, res, next as unknown as NextFunction);
    const [selectSql, selectParams] = queryFn.mock.calls[0] as [string, unknown[]];
    expect(selectSql).toMatch(/WHERE cookie_token = \$1\s+AND expires_at > NOW\(\)/);
    expect(selectSql).toMatch(/released_at, handoff_build_id,[\s\S]*\s+FROM trial_keepers/);
    // Whether the payment released the trial after its token was last replaced (see "After payment").
    expect(selectSql).toMatch(
      /\(released_at IS NOT NULL\s+AND \(cookie_rotated_at IS NULL OR cookie_rotated_at < released_at\)\) AS rotate_cookie/
    );
    expect(selectParams).toEqual([VALID_TOKEN]);
  });

  it('mint INSERT seeds companion_name=Haven for MVP', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq();
    const { res } = makeRes();
    await handler(req, res, next as unknown as NextFunction);
    const insertParams = queryFn.mock.calls[0][1] as unknown[];
    expect(insertParams[1]).toBe('Haven');
    expect(insertParams[2]).toBe('Haven');
  });

  it('cookie maxAge matches 7-day TTL in milliseconds', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq();
    const { res, cookieFn } = makeRes();
    await handler(req, res, next as unknown as NextFunction);
    const [, , opts] = cookieFn.mock.calls[0];
    expect(opts.maxAge).toBe(7 * 24 * 60 * 60 * 1000);
  });

  // Only an explicit development or test run gets a cookie that works over
  // plain HTTP. An unset NODE_ENV or a custom value such as "staging" is
  // treated like production (same rule as the session cookie).
  it.each([
    ['development', false],
    ['test', false],
    ['staging', true],
    [undefined, true],
  ] as Array<[string | undefined, boolean]>)('cookie secure flag when NODE_ENV is %s: %s', async (nodeEnv, expected) => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    if (nodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = nodeEnv;
    try {
      const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      const pool = makePool(queryFn);
      const handler = trialCookie(pool);
      const req = makeReq();
      const { res, cookieFn } = makeRes();
      await handler(req, res, next as unknown as NextFunction);
      expect(cookieFn.mock.calls[0][2].secure).toBe(expected);
    } finally {
      if (ORIGINAL_NODE_ENV === undefined) delete process.env.NODE_ENV;
      else process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('cookie secure flag is TRUE in production', async () => {
    const ORIGINAL_NODE_ENV = process.env.NODE_ENV;
    process.env.NODE_ENV = 'production';
    try {
      const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
      const pool = makePool(queryFn);
      const handler = trialCookie(pool);
      const req = makeReq();
      const { res, cookieFn } = makeRes();
      await handler(req, res, next as unknown as NextFunction);
      expect(cookieFn.mock.calls[0][2].secure).toBe(true);
    } finally {
      if (ORIGINAL_NODE_ENV === undefined) delete process.env.NODE_ENV;
      else process.env.NODE_ENV = ORIGINAL_NODE_ENV;
    }
  });

  it('forwards DB errors via next(err) without setting harborTrial', async () => {
    const queryFn = vi.fn().mockRejectedValueOnce(new Error('connection lost'));
    const pool = makePool(queryFn);
    const handler = trialCookie(pool);
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res } = makeRes();
    await handler(req, res, next as unknown as NextFunction);
    expect(next).toHaveBeenCalledOnce();
    const err = next.mock.calls[0][0];
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toBe('connection lost');
    expect(req.harborTrial).toBeUndefined();
  });
});

describe('trialCookie: malformed cookie value (R1-W2 review)', () => {
  it('a cookie with broken percent-encoding gets a fresh trial, not a 500', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const handler = trialCookie(makePool(queryFn));
    const req = makeReq('cas_harbor_trial=%E0%A4%A');
    const { res, cookieFn } = makeRes();
    const next = vi.fn();
    await handler(req, res, next as unknown as NextFunction);
    expect(next).toHaveBeenCalledWith();
    expect(queryFn.mock.calls[0][0]).toMatch(/INSERT INTO trial_keepers/);
    expect(cookieFn).toHaveBeenCalledOnce();
  });
});

describe('createMintLimiter', () => {
  it('allows up to max mints per key inside the window, then refuses', () => {
    let t = 1_000;
    const limiter = createMintLimiter({ max: 3, windowMs: 60_000, now: () => t });
    expect(limiter.allow('ip:1.1.1.1')).toBe(true);
    expect(limiter.allow('ip:1.1.1.1')).toBe(true);
    expect(limiter.allow('ip:1.1.1.1')).toBe(true);
    expect(limiter.allow('ip:1.1.1.1')).toBe(false);
    // A different address has its own budget.
    expect(limiter.allow('ip:2.2.2.2')).toBe(true);
  });

  it('opens a new window after windowMs and reports Retry-After seconds', () => {
    let t = 0;
    const limiter = createMintLimiter({ max: 1, windowMs: 60_000, now: () => t });
    expect(limiter.allow('k')).toBe(true);
    t = 10_000;
    expect(limiter.allow('k')).toBe(false);
    expect(limiter.retryAfterSeconds('k')).toBe(50);
    t = 60_001;
    expect(limiter.allow('k')).toBe(true);
  });

  it('keeps memory bounded when many distinct keys arrive', () => {
    let t = 0;
    const limiter = createMintLimiter({ max: 1, windowMs: 1_000_000, maxKeys: 100, now: () => t });
    for (let i = 0; i < 1000; i++) limiter.allow(`ip:${i}`);
    // Not directly observable, but the earliest keys must have been evicted
    // (and so are allowed again) while the call path stays fast.
    expect(limiter.allow('ip:0')).toBe(true);
  });
});

describe('trialCookie: bots that never keep cookies cannot mint unbounded trials (R1-W2 review)', () => {
  it('stops inserting trial_keepers rows once the per-address mint budget is spent', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [ACTIVE_TRIAL_ROW] });
    const limiter = createMintLimiter({ max: 2, windowMs: 3_600_000 });
    const handler = trialCookie(makePool(queryFn), { mintLimiter: limiter });
    const next = vi.fn();
    const statusFn = vi.fn();
    const setFn = vi.fn();
    const jsonFn = vi.fn();
    function res(): Response {
      const r = { cookie: vi.fn(), status: statusFn, set: setFn, json: jsonFn } as unknown as Response;
      statusFn.mockReturnValue(r);
      setFn.mockReturnValue(r);
      return r;
    }
    const reqFor = (ip: string) => ({ headers: {}, ip }) as unknown as Request;

    await handler(reqFor('198.51.100.7'), res(), next as unknown as NextFunction);
    await handler(reqFor('198.51.100.7'), res(), next as unknown as NextFunction);
    expect(next).toHaveBeenCalledTimes(2);
    expect(queryFn).toHaveBeenCalledTimes(2);

    // Third cookie-less request from the same address: refused, no INSERT.
    await handler(reqFor('198.51.100.7'), res(), next as unknown as NextFunction);
    expect(next).toHaveBeenCalledTimes(2);
    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(statusFn).toHaveBeenCalledWith(429);
    expect(setFn).toHaveBeenCalledWith('Retry-After', expect.any(String));
    expect(jsonFn).toHaveBeenCalledWith(expect.objectContaining({ error: 'rate_limited' }));

    // A visitor with a valid cookie is not affected by the exhausted budget.
    queryFn.mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const withCookie = { headers: { cookie: `cas_harbor_trial=${VALID_TOKEN}` }, ip: '198.51.100.7' } as unknown as Request;
    await handler(withCookie, res(), next as unknown as NextFunction);
    expect(next).toHaveBeenCalledTimes(3);
    expect(queryFn.mock.calls[2][0]).toMatch(/SELECT trial_id, cookie_token/);
  });

  it('another address is unaffected', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [ACTIVE_TRIAL_ROW] });
    const limiter = createMintLimiter({ max: 1, windowMs: 3_600_000 });
    const handler = trialCookie(makePool(queryFn), { mintLimiter: limiter });
    const next = vi.fn();
    const mk = (ip: string) => ({ headers: {}, ip }) as unknown as Request;
    const okRes = () => ({ cookie: vi.fn() }) as unknown as Response;
    await handler(mk('198.51.100.1'), okRes(), next as unknown as NextFunction);
    await handler(mk('198.51.100.2'), okRes(), next as unknown as NextFunction);
    expect(next).toHaveBeenCalledTimes(2);
  });
});


// ----------------------------------------------------------------------
// After payment. The Stripe webhook releases the trial; the browser on its
// way back from Stripe holds nothing but this cookie. It must keep meaning
// "the browser that paid for this trial's build" and must not be swapped for
// a fresh trial (which is what locked the payer out before). Its value is
// replaced once, on the first request after the release (rotate_cookie, read
// with the row); releasedRow() is a trial whose value was already replaced.
// ----------------------------------------------------------------------

const BUILD_ID = 'bbbbbbbb-1111-2222-3333-444444444444';
const KEEPER_ID = 'cccccccc-1111-2222-3333-444444444444';

function releasedRow(overrides: Record<string, unknown> = {}) {
  return {
    ...ACTIVE_TRIAL_ROW,
    released_at: new Date('2026-04-27T10:00:00Z'),
    handoff_build_id: BUILD_ID,
    expires_at: new Date(Date.now() + 20 * 24 * 60 * 60 * 1000),
    rotate_cookie: false,
    ...overrides,
  };
}

describe('trialCookie: the first request after the payment replaces the cookie value', () => {
  it('writes a new token in place of the old one, only while the row still holds the old one, and sets it once', async () => {
    const row = releasedRow({ rotate_cookie: true });
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [row] }) // trial by token
      .mockResolvedValueOnce({ rows: [{ build_id: BUILD_ID, customer_id: KEEPER_ID }] }) // paid build
      .mockResolvedValueOnce({ rows: [{ trial_id: row.trial_id }] }); // the replacement
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    await trialCookie(makePool(queryFn))(req, res, vi.fn() as unknown as NextFunction);

    expect(queryFn).toHaveBeenCalledTimes(3);
    const [sql, params] = queryFn.mock.calls[2] as [string, unknown[]];
    expect(sql).toMatch(/UPDATE trial_keepers SET cookie_token = \$3, cookie_rotated_at = NOW\(\)\s+WHERE trial_id = \$1 AND cookie_token = \$2/);
    expect(params[0]).toBe(row.trial_id);
    expect(params[1]).toBe(VALID_TOKEN);
    expect(params[2]).toMatch(/^[0-9a-f]{64}$/);
    expect(params[2]).not.toBe(VALID_TOKEN);
    // The browser gets the new value, once, with the time left on the row.
    expect(cookieFn).toHaveBeenCalledOnce();
    const [name, value, opts] = cookieFn.mock.calls[0];
    expect(name).toBe('cas_harbor_trial');
    expect(value).toBe(params[2]);
    expect(opts.maxAge).toBeGreaterThan(19 * 24 * 60 * 60 * 1000);
    // Still the payer: the replacement changes the value, not who it is.
    expect(req.harborPaidKeeper).toEqual({ trialId: row.trial_id, buildId: BUILD_ID, customerId: KEEPER_ID });
    expect(req.harborTrial?.cookie_token).toBe(params[2]);
  });

  it('a request that lost the race to replace it sets no cookie (the winner sets the new one) and is still the payer', async () => {
    const row = releasedRow({ rotate_cookie: true });
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [row] })
      .mockResolvedValueOnce({ rows: [{ build_id: BUILD_ID, customer_id: KEEPER_ID }] })
      .mockResolvedValueOnce({ rows: [] }); // another request replaced the token first
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    await trialCookie(makePool(queryFn))(req, res, vi.fn() as unknown as NextFunction);

    expect(cookieFn).not.toHaveBeenCalled();
    expect(req.harborPaidKeeper).toEqual({ trialId: row.trial_id, buildId: BUILD_ID, customerId: KEEPER_ID });
  });
});

describe('rotateTrialCookie: a new value after a step that trusts the cookie with more', () => {
  it('replaces the token of the request\'s trial and sets the new value', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [{ trial_id: ACTIVE_TRIAL_ROW.trial_id }] });
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    req.harborTrial = { ...ACTIVE_TRIAL_ROW };
    const { res, cookieFn } = makeRes();
    expect(await rotateTrialCookie(makePool(queryFn), req, res)).toBe(true);
    const [sql, params] = queryFn.mock.calls[0] as [string, unknown[]];
    expect(sql).toMatch(/WHERE trial_id = \$1 AND cookie_token = \$2/);
    expect(params.slice(0, 2)).toEqual([ACTIVE_TRIAL_ROW.trial_id, VALID_TOKEN]);
    expect(cookieFn).toHaveBeenCalledOnce();
    expect(cookieFn.mock.calls[0][1]).toBe(params[2]);
    expect(req.harborTrial.cookie_token).toBe(params[2]);
  });

  it('sets nothing when the token was already replaced, or when the request has no trial', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [] });
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    req.harborTrial = { ...ACTIVE_TRIAL_ROW };
    const { res, cookieFn } = makeRes();
    expect(await rotateTrialCookie(makePool(queryFn), req, res)).toBe(false);
    expect(await rotateTrialCookie(makePool(queryFn), makeReq(), res)).toBe(false);
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(cookieFn).not.toHaveBeenCalled();
    expect(req.harborTrial.cookie_token).toBe(VALID_TOKEN);
  });
});

describe('the cookie names outside development and test', () => {
  it('carry the __Host- prefix (Secure, Path=/, no Domain) in production, and stay bare in development and test', async () => {
    const { hostOnlyCookieName } = await import('../../../src/middleware/auth');
    expect(hostOnlyCookieName('cas_harbor_trial', { NODE_ENV: 'production' })).toBe('__Host-cas_harbor_trial');
    expect(hostOnlyCookieName('cas.sid', { NODE_ENV: 'staging' })).toBe('__Host-cas.sid');
    expect(hostOnlyCookieName('cas.sid', {})).toBe('__Host-cas.sid');
    expect(hostOnlyCookieName('cas.sid', { NODE_ENV: 'development' })).toBe('cas.sid');
    expect(hostOnlyCookieName('cas_harbor_trial', { NODE_ENV: 'test' })).toBe('cas_harbor_trial');
  });

  it('a production trial cookie is set as __Host-cas_harbor_trial with Secure, Path=/ and no Domain, and only that name is read', async () => {
    const original = process.env.NODE_ENV;
    process.env.NODE_ENV = 'production';
    vi.resetModules();
    try {
      const production = await import('../../../src/middleware/trial-cookie');
      expect(production.TRIAL_COOKIE_NAME).toBe('__Host-cas_harbor_trial');
      const queryFn = vi.fn().mockResolvedValue({ rows: [ACTIVE_TRIAL_ROW] });
      const { res, cookieFn } = makeRes();
      // The bare name, which a sibling host could set with Domain=, is not read: a fresh trial is minted.
      await production.trialCookie(makePool(queryFn))(makeReq(`cas_harbor_trial=${VALID_TOKEN}`), res, vi.fn() as unknown as NextFunction);
      expect(queryFn).toHaveBeenCalledTimes(1);
      expect(String(queryFn.mock.calls[0][0])).toMatch(/INSERT INTO trial_keepers/);
      const [name, , opts] = cookieFn.mock.calls[0];
      expect(name).toBe('__Host-cas_harbor_trial');
      expect(opts).toMatchObject({ secure: true, path: '/' });
      expect(opts).not.toHaveProperty('domain');
    } finally {
      if (original === undefined) delete process.env.NODE_ENV;
      else process.env.NODE_ENV = original;
      vi.resetModules();
    }
  });
});

describe('trialCookie: a trial released by a payment', () => {
  it('keeps the cookie, attaches the released trial and names the build and keeper it paid for', async () => {
    const row = releasedRow();
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [row] }) // trial by token
      .mockResolvedValueOnce({ rows: [{ build_id: BUILD_ID, customer_id: KEEPER_ID }] }); // paid build
    const handler = trialCookie(makePool(queryFn));
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    const next = vi.fn();
    await handler(req, res, next as unknown as NextFunction);

    expect(next).toHaveBeenCalledWith();
    expect(req.harborTrial).toEqual(row);
    expect(req.harborPaidKeeper).toEqual({ trialId: row.trial_id, buildId: BUILD_ID, customerId: KEEPER_ID });
    // No new trial was minted: two reads, no INSERT.
    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(queryFn.mock.calls.some((c) => /INSERT INTO trial_keepers/.test(String(c[0])))).toBe(false);
    // The same token is re-sent with the time left on the row, not replaced.
    expect(cookieFn).toHaveBeenCalledOnce();
    const [name, value, opts] = cookieFn.mock.calls[0];
    expect(name).toBe('cas_harbor_trial');
    expect(value).toBe(VALID_TOKEN);
    expect(opts).toMatchObject({ httpOnly: true, sameSite: 'lax', path: '/' });
    expect(opts.maxAge).toBeGreaterThan(19 * 24 * 60 * 60 * 1000);
    expect(opts.maxAge).toBeLessThanOrEqual(20 * 24 * 60 * 60 * 1000);
  });

  it('proves the payment by what promotion recorded on this trial\'s own row, and nothing else', async () => {
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [releasedRow()] })
      .mockResolvedValueOnce({ rows: [{ build_id: BUILD_ID, customer_id: KEEPER_ID }] });
    const handler = trialCookie(makePool(queryFn));
    const { res } = makeRes();
    await handler(makeReq(`cas_harbor_trial=${VALID_TOKEN}`), res, vi.fn() as unknown as NextFunction);

    const [paidSql, paidParams] = queryFn.mock.calls[1] as [string, unknown[]];
    // The trial, released, promoted into the account of its own build, which is a keeper.
    expect(paidSql).toMatch(
      /FROM trial_keepers t\s+JOIN builds b ON b\.id = t\.handoff_build_id\s+JOIN keepers k ON k\.customer_id = t\.promoted_customer_id\s+AND k\.customer_id = b\.customer_id\s+WHERE t\.trial_id = \$1 AND t\.released_at IS NOT NULL/
    );
    // The only input is this trial's own id; nothing from the request.
    expect(paidParams).toEqual([releasedRow().trial_id]);
  });

  it('a released trial whose build has no keeper (nothing was paid) gets a fresh trial', async () => {
    const fresh = { ...ACTIVE_TRIAL_ROW, trial_id: '99999999-2222-3333-4444-555555555555', cookie_token: 'b'.repeat(64) };
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [releasedRow()] })
      .mockResolvedValueOnce({ rows: [] }) // no builds-keepers match
      .mockResolvedValueOnce({ rows: [fresh] }); // INSERT new trial
    const handler = trialCookie(makePool(queryFn));
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    await handler(req, res, vi.fn() as unknown as NextFunction);

    expect(req.harborTrial).toEqual(fresh);
    expect(req.harborPaidKeeper).toBeUndefined();
    expect(queryFn.mock.calls[2][0]).toMatch(/INSERT INTO trial_keepers/);
    expect(cookieFn.mock.calls[0][1]).toBe(fresh.cookie_token);
  });

  it('a released trial with no build at all gets a fresh trial without asking about keepers', async () => {
    const fresh = { ...ACTIVE_TRIAL_ROW, cookie_token: 'c'.repeat(64) };
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [releasedRow({ handoff_build_id: null })] })
      .mockResolvedValueOnce({ rows: [fresh] });
    const handler = trialCookie(makePool(queryFn));
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res } = makeRes();
    await handler(req, res, vi.fn() as unknown as NextFunction);

    expect(queryFn).toHaveBeenCalledTimes(2);
    expect(queryFn.mock.calls[1][0]).toMatch(/INSERT INTO trial_keepers/);
    expect(req.harborPaidKeeper).toBeUndefined();
  });

  it('an active trial is never given a paid-keeper identity, even with a build attached', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({
      rows: [{ ...ACTIVE_TRIAL_ROW, released_at: null, handoff_build_id: BUILD_ID }],
    });
    const handler = trialCookie(makePool(queryFn));
    const req = makeReq(`cas_harbor_trial=${VALID_TOKEN}`);
    const { res, cookieFn } = makeRes();
    await handler(req, res, vi.fn() as unknown as NextFunction);

    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(req.harborPaidKeeper).toBeUndefined();
    expect(cookieFn).not.toHaveBeenCalled();
  });
});

describe('identifyTrialCookie: read only', () => {
  it('returns the active trial for a good cookie', async () => {
    const queryFn = vi.fn().mockResolvedValueOnce({ rows: [ACTIVE_TRIAL_ROW] });
    const who = await identifyTrialCookie(makePool(queryFn), makeReq(`cas_harbor_trial=${VALID_TOKEN}`));
    expect(who).toEqual({ trial: ACTIVE_TRIAL_ROW, paidKeeper: null });
  });

  it('returns the paid keeper for a released trial that paid', async () => {
    const row = releasedRow();
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce({ rows: [row] })
      .mockResolvedValueOnce({ rows: [{ build_id: BUILD_ID, customer_id: KEEPER_ID }] });
    const who = await identifyTrialCookie(makePool(queryFn), makeReq(`cas_harbor_trial=${VALID_TOKEN}`));
    expect(who?.paidKeeper).toEqual({ trialId: row.trial_id, buildId: BUILD_ID, customerId: KEEPER_ID });
  });

  it.each([
    ['no cookie', undefined],
    ['a malformed cookie', 'cas_harbor_trial=garbage'],
  ])('returns null for %s and never queries', async (_label, header) => {
    const queryFn = vi.fn();
    expect(await identifyTrialCookie(makePool(queryFn), makeReq(header))).toBeNull();
    expect(queryFn).not.toHaveBeenCalled();
  });

  it('returns null for an unknown token and never inserts a trial', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [] });
    expect(await identifyTrialCookie(makePool(queryFn), makeReq(`cas_harbor_trial=${VALID_TOKEN}`))).toBeNull();
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(String(queryFn.mock.calls[0][0])).not.toMatch(/INSERT/);
  });
});
