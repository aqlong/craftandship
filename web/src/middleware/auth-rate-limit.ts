/**
 * Rate limiters for /login, /signup, /forgot-password, /reset-password and
 * /resend-verification, and (makeAccountDeleteRateLimit, at the bottom) for
 * POST /account/delete, the one other route that checks a password.
 *
 * Each exported limiter is three buckets. A request must fit in all of them:
 *
 *   1. IP alone, across every email (`ipMax`, default 5 x max). Stops one
 *      host from trying a single password against thousands of accounts, or
 *      from creating accounts in bulk.
 *
 *   2. IP + email pair (`max` per window). A shared egress IP (CGNAT, an
 *      office proxy) gets a separate budget for each email tried from it, so
 *      one user fat-fingering a password does not lock out their colleagues.
 *
 *   3. Email alone, across every IP (`emailMax`, default 3 x max). The pair
 *      bucket by itself is no defense against a botnet: every new IP would
 *      get a fresh budget against the same account. This bucket caps guesses
 *      against one account, and caps the verification and reset mail one
 *      address can be sent, no matter how many IPs are used. The price is that
 *      someone who keeps this bucket full can keep the real owner from
 *      logging in for the length of the window. That trade is deliberate.
 *
 * They are checked in that order, and the order is about memory. The limiter
 * runs before the CSRF check and before the route looks at the form, so it
 * meets every request anyone cares to send. The address bucket goes first
 * because its key cannot be chosen by the sender (one validated address, see
 * rateLimitIpKey); the two buckets whose keys contain the typed email are
 * reached only by a request the address bucket let through. A refused client
 * therefore adds nothing to memory, and one address can add at most `ipMax`
 * emails per window to each of the other two.
 *
 * What an email may put in a key: it is trimmed and lower-cased so casing or
 * whitespace cannot spread one account across buckets; an email longer than
 * MAX_EMAIL_CHARS (the longest address SMTP can carry) is refused with a 400
 * after the address bucket and before anything else is stored; and a key
 * keeps the email itself only up to MAX_EMAIL_KEY_CHARS, above which it keeps
 * a SHA-256 of it instead. All three buckets are built by boundedRateLimit
 * (companion-rate-limit.ts), so the number of keys is capped as well. The
 * request body itself is capped for these routes in app.ts (AUTH_BODY_LIMIT).
 *
 * Addresses go through rateLimitIpKey (companion-rate-limit.ts), which keys
 * an IPv6 client on its /64: a single subscriber usually controls a whole /64
 * and could otherwise pick a new address for every request (express-rate-limit
 * 7.x keys on the raw `req.ip`). When the request carries no email, bucket 2
 * degrades to an address-only key and bucket 3 is skipped.
 *
 * Counters live in process memory: they reset on restart and are not shared
 * between replicas. That is acceptable for the single-instance deployment
 * this app ran as; a multi-instance deployment needs a shared store.
 */

import crypto from 'crypto';
import type { Request, RequestHandler } from 'express';
import { boundedRateLimit, customerOrAddressKey, rateLimitIpKey } from './companion-rate-limit';

export const AUTH_DEFAULT_WINDOW_MS = 15 * 60 * 1000;
export const AUTH_DEFAULT_MAX = 10;
export const SIGNUP_DEFAULT_WINDOW_MS = 60 * 60 * 1000;
export const SIGNUP_DEFAULT_MAX = 5;
export const ACCOUNT_DELETE_DEFAULT_MAX = 5;
export const EMAIL_BUCKET_MULTIPLIER = 3;
export const IP_BUCKET_MULTIPLIER = 5;
/** RFC 5321: a forward-path is at most 256 octets, brackets included. */
export const MAX_EMAIL_CHARS = 254;
/** An email longer than this is kept in a key as a hash. */
export const MAX_EMAIL_KEY_CHARS = 64;
export const EMAIL_TOO_LONG_MESSAGE = 'That email address is too long.';

const RATE_LIMIT_HEADERS = ['RateLimit-Policy', 'RateLimit-Limit', 'RateLimit-Remaining', 'RateLimit-Reset'];

function rawEmail(req: Request): string {
  return req.body && typeof req.body.email === 'string' ? req.body.email : '';
}

/** The email as a key holds it: trimmed, lower-cased, and never longer than MAX_EMAIL_KEY_CHARS + 7. */
export function emailKeyPart(email: string): string {
  const normalized = email.trim().toLowerCase();
  if (normalized.length <= MAX_EMAIL_KEY_CHARS) return normalized;
  return `sha256:${crypto.createHash('sha256').update(normalized).digest('hex')}`;
}

/**
 * Key for bucket 2 (IP + email). Exported so unit tests can exercise the
 * keying contract without standing up the full middleware.
 */
export function authHybridKeyGen(req: Request): string {
  const address = rateLimitIpKey(req.ip);
  const email = emailKeyPart(rawEmail(req));
  return email ? `${address}|${email}` : address;
}

interface AuthRateLimitOpts {
  windowMs?: number;
  /** Cap for the IP + email pair bucket. */
  max?: number;
  /** Cap for one email across all IPs. Default: EMAIL_BUCKET_MULTIPLIER x max. */
  emailMax?: number;
  /** Cap for one IP across all emails. Default: IP_BUCKET_MULTIPLIER x max. */
  ipMax?: number;
  message?: string;
  /** When true (default), skip the limiter under NODE_ENV=test so the
   *  broader auth route tests don't get throttled. Pass false from
   *  middleware-focused tests that need to exercise the cap. */
  skipInTest?: boolean;
}

function buildLimiter(opts: AuthRateLimitOpts & { defaultMessage: string; defaultMax: number; defaultWindowMs: number }): RequestHandler {
  const skipInTest = opts.skipInTest ?? true;
  const skipForTests = (): boolean => skipInTest && process.env.NODE_ENV === 'test';
  const windowMs = opts.windowMs ?? opts.defaultWindowMs;
  const max = opts.max ?? opts.defaultMax;
  const shared = {
    windowMs,
    message: opts.message ?? opts.defaultMessage,
    standardHeaders: true,
    legacyHeaders: false,
  };

  const ipLimiter = boundedRateLimit({
    ...shared,
    max: opts.ipMax ?? max * IP_BUCKET_MULTIPLIER,
    skip: skipForTests,
    keyGenerator: (req) => `all:${rateLimitIpKey(req.ip)}`,
  });
  const pairLimiter = boundedRateLimit({
    ...shared,
    max,
    skip: skipForTests,
    keyGenerator: authHybridKeyGen,
  });
  const emailLimiter = boundedRateLimit({
    ...shared,
    max: opts.emailMax ?? max * EMAIL_BUCKET_MULTIPLIER,
    skip: (req) => skipForTests() || rawEmail(req).trim() === '',
    keyGenerator: (req) => `email:${emailKeyPart(rawEmail(req))}`,
  });

  // Each bucket either answers 429 itself or calls its callback, so the chain
  // stops at the first one that is full. A request that passes all three
  // carries the address bucket's RateLimit-* headers (the other two overwrite
  // them on the way through, so they are put back at the end).
  return function authRateLimitChain(req, res, next) {
    ipLimiter(req, res, (ipErr?: unknown) => {
      if (ipErr) return next(ipErr);
      if (!skipForTests() && rawEmail(req).trim().length > MAX_EMAIL_CHARS) {
        res.status(400).type('text/plain').send(EMAIL_TOO_LONG_MESSAGE);
        return;
      }
      const addressHeaders = RATE_LIMIT_HEADERS.map((name) => [name, res.getHeader(name)] as const);
      pairLimiter(req, res, (pairErr?: unknown) => {
        if (pairErr) return next(pairErr);
        emailLimiter(req, res, (emailErr?: unknown) => {
          if (emailErr) return next(emailErr);
          for (const [name, value] of addressHeaders) {
            if (value !== undefined) res.setHeader(name, value);
          }
          next();
        });
      });
    });
  };
}

export function makeAuthRateLimit(opts: AuthRateLimitOpts = {}): RequestHandler {
  return buildLimiter({
    ...opts,
    defaultMax: AUTH_DEFAULT_MAX,
    defaultWindowMs: AUTH_DEFAULT_WINDOW_MS,
    defaultMessage: 'Too many attempts. Please try again in 15 minutes.',
  });
}

export function makeSignupRateLimit(opts: AuthRateLimitOpts = {}): RequestHandler {
  return buildLimiter({
    ...opts,
    defaultMax: SIGNUP_DEFAULT_MAX,
    defaultWindowMs: SIGNUP_DEFAULT_WINDOW_MS,
    defaultMessage: 'Too many signup attempts. Please try again later.',
  });
}

/**
 * The limiter for POST /account/delete. The route checks the account's
 * password, so it is a place to guess one: a stolen session cookie is enough
 * to reach it, and without a limit it would answer every guess. Two buckets,
 * the second reached only by a request the first let through:
 *
 *   1. The address (`ipMax`, default 5 x max), keyed like every other
 *      per-address limiter (rateLimitIpKey).
 *   2. The account (`max`): the customer id from the session, so the guesses
 *      against one account are capped however many addresses they come from.
 *
 * The route sits behind requireAuth, so the session carries a customer id
 * whenever this runs; customerOrAddressKey falls back to the address only for
 * a request that has none. Like the others, counters are in process memory
 * and the limiter is skipped under NODE_ENV=test unless `skipInTest` is false.
 */
export function makeAccountDeleteRateLimit(opts: Pick<AuthRateLimitOpts, 'windowMs' | 'max' | 'ipMax' | 'message' | 'skipInTest'> = {}): RequestHandler {
  const skipInTest = opts.skipInTest ?? true;
  const skipForTests = (): boolean => skipInTest && process.env.NODE_ENV === 'test';
  const max = opts.max ?? ACCOUNT_DELETE_DEFAULT_MAX;
  const shared = {
    windowMs: opts.windowMs ?? AUTH_DEFAULT_WINDOW_MS,
    message: opts.message ?? 'Too many attempts. Please try again in 15 minutes.',
    standardHeaders: true,
    legacyHeaders: false,
    skip: skipForTests,
  };
  const ipLimiter = boundedRateLimit({
    ...shared,
    max: opts.ipMax ?? max * IP_BUCKET_MULTIPLIER,
    keyGenerator: (req) => `all:${rateLimitIpKey(req.ip)}`,
  });
  const accountLimiter = boundedRateLimit({
    ...shared,
    max,
    keyGenerator: customerOrAddressKey,
  });
  return function accountDeleteRateLimitChain(req, res, next) {
    ipLimiter(req, res, (ipErr?: unknown) => {
      if (ipErr) return next(ipErr);
      accountLimiter(req, res, next);
    });
  };
}

export const authRateLimit = makeAuthRateLimit();
export const signupRateLimit = makeSignupRateLimit();
export const accountDeleteRateLimit = makeAccountDeleteRateLimit();
