import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import { isJsonRequest } from '../utils/json-request';

declare module 'express-session' {
  interface SessionData {
    /** Epoch milliseconds at which this session was authenticated (login or email verification). */
    authenticatedAt: number;
  }
}

export function requireAuth(req: Request, res: Response, next: NextFunction): void {
  if (!req.session.customerId) {
    const returnTo = req.originalUrl;
    res.redirect(`/login?returnTo=${encodeURIComponent(returnTo)}`);
    return;
  }
  next();
}

// ---------------------------------------------------------------------------
// Session lifetime
// ---------------------------------------------------------------------------
//
// The session cookie is a 24-hour rolling idle timeout (app.ts). On its own
// that never expires a session that is touched once a day, so a stolen cookie
// would stay good for as long as the thief kept using it. Authenticated
// sessions therefore also carry the time they were created and are destroyed
// once they are older than SESSION_ABSOLUTE_MAX_MS, however active they are.

export const SESSION_ABSOLUTE_MAX_MS = 30 * 24 * 60 * 60 * 1000;

/** The session cookie's name and its rolling idle timeout (the cookie's Max-Age). app.ts builds the session from these, and the privacy page prints them. */
export const SESSION_COOKIE_NAME = 'cas.sid';
export const SESSION_IDLE_TIMEOUT_MS = 24 * 60 * 60 * 1000;

export function isSessionPastAbsoluteLifetime(
  session: { customerId?: string; authenticatedAt?: number } | undefined,
  now: number = Date.now()
): boolean {
  if (!session || !session.customerId) return false;
  if (typeof session.authenticatedAt !== 'number') return false;
  return now - session.authenticatedAt > SESSION_ABSOLUTE_MAX_MS;
}

/** Mounted right after the session middleware in app.ts. */
export function enforceSessionLifetime(req: Request, _res: Response, next: NextFunction): void {
  if (!isSessionPastAbsoluteLifetime(req.session)) {
    next();
    return;
  }
  // regenerate() deletes the old session from the store and attaches a fresh,
  // empty one, so the rest of the request runs unauthenticated: page routes
  // redirect to /login through requireAuth, and API routes answer as they do
  // for any visitor.
  req.session.regenerate((err) => {
    if (err) {
      next(err);
      return;
    }
    next();
  });
}

// ---------------------------------------------------------------------------
// Session secret
// ---------------------------------------------------------------------------
//
// One secret signs the session cookie, the CSRF tokens below, and the
// unsubscribe tokens (services/email.ts). A known value makes every one of
// those forgeable, so the placeholder literals that ship in the repo are never
// accepted as a real secret:
//
//   - NODE_ENV=development or test, secret missing or a placeholder: use a
//     random secret generated for this process. Nothing is forgeable, and the
//     only cost is that sessions and tokens do not survive a restart.
//   - Any other NODE_ENV (production, staging, unset): refuse to start unless
//     a secret is set that passes `sessionSecretWeakness` below (long enough,
//     and not an obvious stand-in). "Unset" is treated like production on
//     purpose: a deployment that forgot NODE_ENV must not silently run on a
//     public literal.

/** Literals that appear in this repo (old code default, .env.example). */
export const SESSION_SECRET_PLACEHOLDERS: readonly string[] = [
  'dev_secret_change_me',
  'change-me-in-production',
];

export const MIN_SESSION_SECRET_LENGTH = 32;

/** A production secret must use at least this many different characters. */
export const MIN_SESSION_SECRET_DISTINCT_CHARS = 8;

/** A production secret must not be one block of at most this many characters, repeated. */
export const MAX_SESSION_SECRET_REPEATED_BLOCK = 16;

/**
 * Words that mark a value as a stand-in. Matched after lower-casing the
 * secret and dropping everything that is not a letter or a digit, so
 * "change-me", "Change_Me" and "CHANGEME" are the same word. Every entry is
 * at least seven letters long, which keeps the chance of one turning up
 * inside a random base64 value negligible (and none can occur in hex).
 */
export const SESSION_SECRET_PLACEHOLDER_WORDS: readonly string[] = [
  'changeme',
  'replaceme',
  'placeholder',
  'yoursecret',
  'devsecret',
  'sessionsecret',
  'example',
  'password',
  'insecure',
];

/**
 * Why a value cannot be the session secret outside development and test, as
 * the end of a sentence that starts "SESSION_SECRET ...", or null when it is
 * acceptable. The checks, in order:
 *
 *   1. at least MIN_SESSION_SECRET_LENGTH characters;
 *   2. at least MIN_SESSION_SECRET_DISTINCT_CHARS different characters;
 *   3. not a single block of MAX_SESSION_SECRET_REPEATED_BLOCK or fewer
 *      characters repeated to fill the value (the last repetition may be cut
 *      short);
 *   4. no SESSION_SECRET_PLACEHOLDER_WORDS entry inside it (see there for how
 *      it is compared).
 *
 * This catches a value typed or pasted as a stand-in. It is not an entropy
 * estimate: a long sentence, or a keyboard walk with enough different keys,
 * passes. `openssl rand -hex 32` passes with a failure chance far below one
 * in a billion.
 */
export function sessionSecretWeakness(secret: string): string | null {
  if (secret.length < MIN_SESSION_SECRET_LENGTH) {
    return `is too short (${secret.length} characters; need at least ${MIN_SESSION_SECRET_LENGTH})`;
  }
  if (new Set(secret).size < MIN_SESSION_SECRET_DISTINCT_CHARS) {
    return `uses fewer than ${MIN_SESSION_SECRET_DISTINCT_CHARS} different characters`;
  }
  for (let block = 1; block <= MAX_SESSION_SECRET_REPEATED_BLOCK; block++) {
    const repeated = secret.slice(0, block).repeat(Math.ceil(secret.length / block)).slice(0, secret.length);
    if (repeated === secret) {
      return `is a block of ${block} character${block === 1 ? '' : 's'} repeated`;
    }
  }
  const normalized = secret.toLowerCase().replace(/[^a-z0-9]/g, '');
  const word = SESSION_SECRET_PLACEHOLDER_WORDS.find((candidate) => normalized.includes(candidate));
  if (word) {
    return `contains the placeholder word "${word}"`;
  }
  return null;
}

export class SessionSecretError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SessionSecretError';
  }
}

export interface SessionSecretResolution {
  secret: string;
  /** True when the secret was generated for this process only. */
  ephemeral: boolean;
  /** Human-readable note for the boot log, or null when there is nothing to say. */
  warning: string | null;
}

/**
 * Decide which session secret to use for the given environment. Pure apart
 * from the random bytes: it reads nothing but its argument, so tests can
 * exercise every branch without touching process.env.
 */
export function resolveSessionSecret(
  env: Record<string, string | undefined> = process.env
): SessionSecretResolution {
  const configured = (env.SESSION_SECRET || '').trim();
  const nodeEnv = env.NODE_ENV || '';
  const isDevOrTest = isDevOrTestEnv(env);
  const isPlaceholder = configured === '' || SESSION_SECRET_PLACEHOLDERS.includes(configured);

  if (!isPlaceholder) {
    // Development and test take any explicitly configured value: nothing
    // there is worth forging, and a short fixed secret keeps local logins
    // across restarts.
    const weakness = isDevOrTest ? null : sessionSecretWeakness(configured);
    if (weakness) {
      throw new SessionSecretError(
        `SESSION_SECRET ${weakness}. Generate one with: openssl rand -hex 32`
      );
    }
    return { secret: configured, ephemeral: false, warning: null };
  }

  if (isDevOrTest) {
    return {
      secret: crypto.randomBytes(32).toString('hex'),
      ephemeral: true,
      warning:
        nodeEnv === 'test'
          ? null
          : 'WARNING: SESSION_SECRET is unset or still a placeholder. Using a random secret for ' +
            'this process only: logins, CSRF tokens, and unsubscribe links will stop working on ' +
            'every restart. Set SESSION_SECRET in web/.env (openssl rand -hex 32) to keep them.',
    };
  }

  throw new SessionSecretError(
    `SESSION_SECRET must be set to a strong random value when NODE_ENV is "${nodeEnv || '(unset)'}". ` +
      'The placeholder values from the repo are refused outside development and test. ' +
      'Generate one with: openssl rand -hex 32'
  );
}

/**
 * True only when NODE_ENV is explicitly "development" or "test".
 *
 * Every development convenience that weakens a control (the two payment
 * bypasses, the admin `?dev_trial=` and lamp overrides) keys on this, not on
 * `NODE_ENV !== 'production'`. The difference is a deployment that forgot to
 * set NODE_ENV, or that runs as "staging": with the negative test those would
 * count as development and the conveniences would be live on a public host.
 * Same rule, for the same reason, as the session secret above.
 */
export function isDevOrTestEnv(env: Record<string, string | undefined> = process.env): boolean {
  return env.NODE_ENV === 'development' || env.NODE_ENV === 'test';
}

let cachedSessionSecret: string | null = null;

/**
 * Resolve the secret once for this process and publish it. Called by app.ts at
 * boot (which turns a SessionSecretError into a fatal exit). Every signer in
 * the app (CSRF tokens here, unsubscribe tokens in services/email.ts) reads it
 * through getSessionSecret(). The resolved value is also written back to
 * process.env.SESSION_SECRET, so that anything reading the variable directly
 * sees the same value and never a placeholder.
 */
export function initSessionSecret(
  env: Record<string, string | undefined> = process.env
): SessionSecretResolution {
  const resolution = resolveSessionSecret(env);
  cachedSessionSecret = resolution.secret;
  env.SESSION_SECRET = resolution.secret;
  return resolution;
}

/** The process-wide session secret. Resolves lazily if app.ts has not run. */
export function getSessionSecret(): string {
  if (cachedSessionSecret === null) {
    initSessionSecret();
  }
  return cachedSessionSecret as string;
}

// ---------------------------------------------------------------------------
// Small shared helpers
// ---------------------------------------------------------------------------

/**
 * Constant-time string comparison that never throws. Both sides are hashed
 * first, so the comparison does not leak the length of either input and
 * crypto.timingSafeEqual always receives equal-length buffers (it throws a
 * RangeError otherwise, which is easy to trigger with multi-byte characters
 * when only string lengths were compared).
 */
export function timingSafeEqualStrings(a: unknown, b: unknown): boolean {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const digestA = crypto.createHash('sha256').update(a, 'utf8').digest();
  const digestB = crypto.createHash('sha256').update(b, 'utf8').digest();
  return crypto.timingSafeEqual(digestA, digestB);
}

/**
 * True when the session belongs to the configured admin account. This is the
 * one definition of "admin" for the app (requireAdmin in routes/admin.ts, the
 * admin branches in routes/builds.ts and routes/features.ts, and the nav flag
 * in app.ts all call it). It fails closed on every input:
 *
 *   - ADMIN_EMAIL unset or empty: nobody is admin. (A bare
 *     `session.customerEmail === process.env.ADMIN_EMAIL` is true when both
 *     sides are undefined.)
 *   - The session must be a logged-in customer session that was established
 *     by a password login or an email-verification link, which is what stamps
 *     `authenticatedAt`. A session that merely carries the admin's email is
 *     not enough: the Harbor payment bypass (POST /lighthouse/bypass, a
 *     development-only flag) writes a caller-supplied email into the session
 *     without checking a password, and must never be a way to become admin.
 *     (That route also refuses the admin address and any address that
 *     already has an account; this check does not rely on it.)
 *   - The comparison is exact. Customer emails are stored lower-case, so
 *     ADMIN_EMAIL must be written in lower case to match.
 */
export function isAdminSession(req: Request): boolean {
  const adminEmail = process.env.ADMIN_EMAIL;
  if (!adminEmail) return false;
  const session = req.session as (Request['session'] | undefined);
  if (!session || !session.customerId) return false;
  if (typeof session.authenticatedAt !== 'number') return false;
  const sessionEmail = session.customerEmail;
  if (typeof sessionEmail !== 'string' || sessionEmail === '') return false;
  return sessionEmail === adminEmail;
}

/**
 * Validate a post-login redirect target. Only same-site absolute paths are
 * allowed. Returns the path, or null when it must not be followed.
 *
 * Browsers treat a backslash like a forward slash in the authority position,
 * so "/\evil.example" navigates to another host just like "//evil.example";
 * both are rejected, along with control characters (header splitting, and
 * tab/newline stripping that can turn "/\t/evil.example" into "//evil.example").
 */
export function safeReturnTo(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  if (raw.length === 0 || raw.length > 2000) return null;
  if (raw[0] !== '/') return null;
  if (raw.startsWith('//')) return null;
  if (raw.includes('\\')) return null;
  if (/[\u0000-\u001f\u007f]/.test(raw)) return null;
  return raw;
}

// ---------------------------------------------------------------------------
// CSRF
// ---------------------------------------------------------------------------
//
// Two independent layers, both required on browser form and fetch posts:
//
// 1. rejectCrossOriginWrites (mounted globally in app.ts) refuses any
//    state-changing request that the browser itself labels as coming from
//    another origin (Sec-Fetch-Site, or Origin vs Host on older browsers).
//    This is the layer that actually stops a forged cross-site request, and it
//    covers every route, including the JSON ones.
//
// 2. A stateless HMAC token: `nonce.hmac` where hmac = HMAC-SHA256(nonce,
//    SESSION_SECRET). It needs no session storage and stays valid until the
//    secret changes. It is NOT bound to a session or a user: anyone who can
//    load a page can obtain a valid token, so on its own it only proves "this
//    request came from something that read one of our pages". Treat it as a
//    second factor behind layer 1 and the SameSite=Lax session cookie, not as
//    the primary defense.

export function generateCsrfToken(_req: Request): string {
  const nonce = crypto.randomBytes(16).toString('hex');
  const mac = crypto.createHmac('sha256', getSessionSecret()).update(nonce).digest('hex');
  return `${nonce}.${mac}`;
}

export function validateCsrfToken(req: Request, res: Response, next: NextFunction): void {
  const bodyToken = req.body?._csrf as unknown;
  const headerToken = req.headers['x-csrf-token'] as unknown;
  const submittedToken = bodyToken || headerToken;

  if (typeof submittedToken !== 'string' || !verifyToken(submittedToken)) {
    // A script (the x-csrf-token header, a JSON body, a JSON route) expects
    // JSON, not the HTML page: the one rule is isJsonRequest.
    if (isJsonRequest(req)) {
      res.status(403).json({ error: 'Invalid CSRF token. Please refresh and try again.' });
    } else {
      res.status(403).render('403', {
        title: 'Forbidden',
        error: 'This form could not be verified. The page may have been open for a while. Reload it and try again.',
      });
    }
    return;
  }
  next();
}

export function verifyHmacToken(token: unknown): boolean {
  return typeof token === 'string' && verifyToken(token);
}

function verifyToken(token: string): boolean {
  const dot = token.lastIndexOf('.');
  if (dot === -1) return false;
  const nonce = token.slice(0, dot);
  const mac = token.slice(dot + 1);
  if (!nonce || !mac) return false;
  const expected = crypto.createHmac('sha256', getSessionSecret()).update(nonce).digest('hex');
  // Constant-time comparison to prevent timing attacks. Buffer.from(hex)
  // silently truncates at the first non-hex character, so a malformed mac
  // yields a shorter buffer and timingSafeEqual throws: treat that as invalid.
  try {
    return crypto.timingSafeEqual(Buffer.from(mac, 'hex'), Buffer.from(expected, 'hex'));
  } catch {
    return false;
  }
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/**
 * Decide whether a state-changing request is cross-origin, using only headers
 * the browser sets and page script cannot forge. Exported for tests.
 *
 *   - Sec-Fetch-Site present: "same-origin" (our own pages) and "none" (typed
 *     URL, bookmark) pass; "cross-site" and "same-site" (a sibling subdomain)
 *     are cross-origin.
 *   - No Sec-Fetch-Site but an Origin header (older browsers): the Origin host
 *     must equal the Host this request was sent to.
 *   - Neither header: not a browser (curl, the Stripe and Resend webhook
 *     senders, the build worker, server-side tests). Those carry no ambient
 *     cookies from a victim, so there is nothing to forge and they pass.
 */
export function isCrossOriginWrite(req: Request): boolean {
  if (SAFE_METHODS.has(req.method)) return false;

  const fetchSite = req.headers['sec-fetch-site'];
  if (typeof fetchSite === 'string' && fetchSite !== '') {
    return fetchSite !== 'same-origin' && fetchSite !== 'none';
  }

  const origin = req.headers.origin;
  if (typeof origin !== 'string' || origin === '') return false;

  let originHost: string;
  try {
    originHost = new URL(origin).host.toLowerCase();
  } catch {
    // "null" (sandboxed frame, some redirects) or garbage: not our origin.
    return true;
  }
  const requestHost = (req.headers.host || '').toLowerCase();
  return originHost !== requestHost;
}

/**
 * Global guard: refuse cross-origin POST/PUT/PATCH/DELETE before any route
 * runs. Server-to-server callers (webhooks, the ops API) send neither header
 * and are unaffected.
 */
export function rejectCrossOriginWrites(req: Request, res: Response, next: NextFunction): void {
  if (!isCrossOriginWrite(req)) {
    next();
    return;
  }
  res.status(403).json({ error: 'Cross-origin request refused.' });
}
