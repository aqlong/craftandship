/**
 * CAS Harbor trial cookie middleware.
 *
 * On every /lighthouse/* request:
 *  - Read the `cas_harbor_trial` cookie if present.
 *  - Look up the trial_keepers row by cookie token. If active and
 *    not expired, attach to req.harborTrial.
 *  - If the row was released by a payment (see "After payment" below), keep
 *    the cookie and attach req.harborPaidKeeper as well.
 *  - Otherwise (no cookie, no row, expired, or released with nothing paid
 *    for): mint a fresh trial row (Haven assignment, 7-day TTL), set the
 *    cookie, attach.
 *
 * Identity model: the cookie token is the only thing tying the visitor to
 * their conversation. No email, no password, no card.
 *
 * The token is an opaque random bearer value: 32 bytes from
 * crypto.randomBytes, hex encoded (256 bits). It is NOT a signed value and
 * nothing is verified cryptographically; the database lookup
 * (`cookie_token = $1 AND expires_at > NOW()`, then `released_at`) is the
 * whole validation. A tampered, guessed or expired token simply fails the
 * lookup and the visitor is given a fresh trial. The unguessable token is the
 * secret, so it is stored as-is in `trial_keepers.cookie_token` (a database
 * read exposes live trial sessions; hashing it at rest is a known follow-up).
 * The TTL is enforced twice: by `expires_at` in the lookup and by the cookie
 * `Max-Age`.
 *
 * After payment
 * =============
 * The Stripe webhook that takes a trial's website live also releases the
 * trial (services/keeper_promotion.ts). The visitor's browser is, at that
 * moment, on its way back from Stripe holding nothing but this cookie: it has
 * no account session, and the account the payment landed in has a random
 * password. If a released trial were simply replaced by a fresh one, the
 * person who just paid would get "not found" on the page Stripe sends them to
 * and on the page where the subscription can be cancelled.
 *
 * So a released trial's cookie keeps one narrow meaning until `expires_at`
 * (which promotion moves out to PAID_TRIAL_ACCESS_DAYS): "this browser paid
 * for the build stamped on this trial". `req.harborPaidKeeper` carries that
 * build and its customer, and the lighthouse routes use it in two places.
 * One is the lamp pages of that one build (status, published, cancel the
 * subscription). The other is the conversation, where this browser is the
 * keeper (`resolveCompanionIdentity` in routes/lighthouse.ts): it reads the
 * turns and the notes that promotion moved to the keeper (the conversation
 * page and its notes panel, the recall page, the template picker), and it
 * writes there too (new turns, the notes captured from them, and template
 * choices). It is not an account session: `req.session.customerId`
 * is not set, so the dashboard, other builds on the account, data export and
 * account deletion stay out of reach. That matters because the account can
 * predate the trial (a build is attached to an existing unverified account
 * with the same address), and its other contents are not the payer's to see
 * on the strength of a cookie.
 *
 * The paid cookie does not outlive the account's sessions. A browser that is
 * logged in to an account (`req.session.customerId`) is that account's: a
 * paid cookie promoted into a different account is not recognized
 * (`identifyTrialCookie`), and the request gets a fresh trial instead, whose
 * cookie replaces the paid one in that browser. Logout clears a paid cookie
 * (`clearPaidTrialCookie`), and a completed password reset or email
 * verification expires every trial promoted into the account
 * (`expirePromotedTrialCookies`), so a paid cookie stops speaking for the
 * account once its owner has set a password or proven the mailbox.
 *
 * The cookie speaks as a keeper only if its own trial was promoted into that
 * keeper. Promotion writes the account into `trial_keepers.promoted_customer_id`
 * in the statement that releases the trial, and the link is read back from
 * the rows it leaves (`PROMOTED_KEEPER_OF_TRIAL_SQL`): the trial's
 * `handoff_build_id`, that build's `customer_id`, a `keepers` row for that
 * customer, and `promoted_customer_id` equal to it. A released trial without
 * all four is given a fresh trial like any other stale cookie. Two trials can
 * hold builds on one account, so the first three alone would say that the
 * second trial's cookie belongs to the first trial's keeper; the fourth is
 * what says it does not, and promotion refuses to promote a second trial into
 * an account whose keeper already has a subscription
 * (`claimKeeperSubscription`, services/keeper_promotion.ts). Nothing in a URL
 * takes part: knowing a build id, or the Checkout session id on Stripe's
 * return URL, identifies nobody.
 *
 * Cookie flags: HttpOnly, SameSite=Lax, and Secure unless NODE_ENV is
 * "development" or "test" (same rule as the session cookie in app.ts).
 *
 * Minting: every request that arrives without a valid cookie inserts a
 * `trial_keepers` row, so a client that never stores cookies could create
 * rows without bound. `mintLimiter` caps new trials per client address per
 * hour (default 20, `CAS_HARBOR_TRIAL_MINT_MAX`). Over the cap the request
 * is answered with 429 and no row is written. Behind the Railway proxy the
 * address is `req.ip` (`trust proxy` is set to 1 in app.ts). A shared NAT
 * shares the budget; the counter is in process memory, so it resets on
 * restart and is per replica.
 */

import { Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import type { Pool } from 'pg';
import { assignCompanionName } from '../companion/persona';
import { isBotUserAgent, hashUserAgent } from '../utils/bot-ua';
import { loggablePath } from '../utils/recent-errors';
import { rateLimitIpKey } from './companion-rate-limit';
import { isDevOrTestEnv } from './auth';
import { PROMOTED_KEEPER_OF_TRIAL_SQL } from '../services/keeper_promotion';

/** The conversation cookie's name and its lifetime from the first visit. The privacy page prints both. */
export const TRIAL_COOKIE_NAME = 'cas_harbor_trial';
export const TRIAL_TTL_DAYS = 7;
const TRIAL_TTL_MS = TRIAL_TTL_DAYS * 24 * 60 * 60 * 1000;

export interface TrialKeeperContext {
  trial_id: string;
  cookie_token: string;
  companion_name: string;
  companion_assigned: string;
  started_at: Date;
  expires_at: Date;
  last_seen_at: Date;
  /** Set once the trial has been promoted; absent or null on an active trial. */
  released_at?: Date | null;
  /** The build dispatched from this trial's conversation, if any. */
  handoff_build_id?: string | null;
}

/**
 * Attached when the cookie belongs to a trial that was released by a payment
 * for `buildId`, which belongs to keeper `customerId` (see "After payment").
 */
export interface PaidKeeperContext {
  trialId: string;
  buildId: string;
  customerId: string;
}

declare global {
  namespace Express {
    interface Request {
      harborTrial?: TrialKeeperContext;
      harborPaidKeeper?: PaidKeeperContext;
    }
  }
}

/**
 * Fixed-window counter keyed by client address. `allow()` consumes one unit
 * and returns false once the key has used `max` units inside the window.
 * Memory is bounded: expired entries are swept when the map grows past
 * `maxKeys`, and if it is still over, the oldest half is dropped.
 */
export interface MintLimiter {
  allow(key: string): boolean;
  /** Seconds until the oldest window for `key` resets (for Retry-After). */
  retryAfterSeconds(key: string): number;
}

export function createMintLimiter(opts: {
  max: number;
  windowMs: number;
  maxKeys?: number;
  now?: () => number;
}): MintLimiter {
  const now = opts.now ?? Date.now;
  const maxKeys = opts.maxKeys ?? 50_000;
  const hits = new Map<string, { count: number; resetAt: number }>();

  function sweep(): void {
    if (hits.size <= maxKeys) return;
    const t = now();
    for (const [k, v] of hits) {
      if (v.resetAt <= t) hits.delete(k);
    }
    if (hits.size > maxKeys) {
      // Still over: drop the oldest-inserted half (Map keeps insertion order).
      let toDrop = Math.floor(hits.size / 2);
      for (const k of hits.keys()) {
        if (toDrop-- <= 0) break;
        hits.delete(k);
      }
    }
  }

  return {
    allow(key: string): boolean {
      const t = now();
      const entry = hits.get(key);
      if (!entry || entry.resetAt <= t) {
        hits.set(key, { count: 1, resetAt: t + opts.windowMs });
        sweep();
        return true;
      }
      if (entry.count >= opts.max) return false;
      entry.count += 1;
      return true;
    },
    retryAfterSeconds(key: string): number {
      const entry = hits.get(key);
      if (!entry) return 0;
      return Math.max(1, Math.ceil((entry.resetAt - now()) / 1000));
    },
  };
}

const MINT_WINDOW_MS = 60 * 60 * 1000;
const MINT_MAX_DEFAULT = 20;

function resolveMintMax(): number {
  // The unit-test suite creates many unauthenticated requests from one
  // address; raise the cap there, as the other Harbor limiters do.
  if (process.env.NODE_ENV === 'test') return 100_000;
  const raw = parseInt(process.env.CAS_HARBOR_TRIAL_MINT_MAX || '', 10);
  return Number.isFinite(raw) && raw > 0 ? raw : MINT_MAX_DEFAULT;
}

const defaultMintLimiter = createMintLimiter({
  max: resolveMintMax(),
  windowMs: MINT_WINDOW_MS,
});

export interface TrialIdentity {
  trial: TrialKeeperContext;
  /** Set when the trial was released by a payment; see "After payment". */
  paidKeeper: PaidKeeperContext | null;
}

export type TrialResolution =
  | ({ kind: 'trial' } & TrialIdentity)
  | { kind: 'mint_refused'; retryAfterSeconds: number };

/**
 * Who the request's cookie stands for, if anyone: an active trial, or the
 * browser that paid for a released trial's build. Read only: it neither mints
 * a trial nor touches the response, so it is safe to call after the response
 * has been sent. Null for a missing, malformed, unknown or expired cookie, for
 * a released trial with nothing paid for, and for a paid cookie whose account
 * is not the one this browser is logged in to (see "After payment"). Database
 * errors propagate.
 */
export async function identifyTrialCookie(pool: Pool, req: Request): Promise<TrialIdentity | null> {
  const incomingToken = readCookie(req, TRIAL_COOKIE_NAME);
  if (!incomingToken || !isValidTokenShape(incomingToken)) return null;
  const found = await loadUnexpiredTrial(pool, incomingToken);
  if (!found) return null;
  if (found.released_at == null) return { trial: found, paidKeeper: null };
  const paidKeeper = await loadPaidKeeper(pool, found);
  if (!paidKeeper) return null;
  // The account session is the stronger statement: a paid cookie for another
  // account says nothing about a request made by a logged-in browser.
  const sessionCustomerId = req.session?.customerId;
  if (sessionCustomerId && sessionCustomerId !== paidKeeper.customerId) return null;
  return { trial: found, paidKeeper };
}

/**
 * Logout: clear the trial cookie when a payment released it. An unpaid trial's
 * cookie is kept, so a visitor who logs in and out keeps their own
 * conversation. When the database cannot say which kind it is, the cookie is
 * cleared: that is the answer that keeps a paid cookie from outliving logout.
 */
export async function clearPaidTrialCookie(pool: Pool, req: Request, res: Response): Promise<void> {
  const token = readCookie(req, TRIAL_COOKIE_NAME);
  if (!token || !isValidTokenShape(token)) return;
  let paid = true;
  try {
    const { rows } = await pool.query(
      `SELECT 1 FROM trial_keepers WHERE cookie_token = $1 AND released_at IS NOT NULL LIMIT 1`,
      [token]
    );
    paid = rows.length > 0;
  } catch (err) {
    console.error('[trial-cookie] could not read the trial cookie at logout; clearing it:', (err as Error).message);
  }
  if (paid) res.clearCookie(TRIAL_COOKIE_NAME, trialCookieOptions());
}

/**
 * A completed password reset or email verification of an account expires every
 * trial promoted into it (`promoted_customer_id`), so each paid cookie stops
 * being recognized (`loadUnexpiredTrial` reads `expires_at`). The paying
 * browser then starts a fresh trial, and the account is reached by logging in.
 */
export async function expirePromotedTrialCookies(pool: Pool, customerId: string): Promise<void> {
  await pool.query(`UPDATE trial_keepers SET expires_at = NOW() WHERE promoted_customer_id = $1`, [customerId]);
}

/**
 * Work out who a /lighthouse request is and make sure it has an identity:
 * the one its cookie stands for (see `identifyTrialCookie`), or else a brand
 * new trial, minted here with its cookie set on `res`. Returns `mint_refused`
 * instead of minting when this address has used up its budget of new trials.
 * Database errors propagate.
 */
export async function resolveTrial(
  pool: Pool,
  req: Request,
  res: Response,
  opts: { mintLimiter?: MintLimiter } = {}
): Promise<TrialResolution> {
  const mintLimiter = opts.mintLimiter ?? defaultMintLimiter;

  const known = await identifyTrialCookie(pool, req);
  if (known) {
    if (known.paidKeeper) {
      // Keep the browser's copy alive for as long as the row is: the cookie
      // was set with the trial's 7-day Max-Age, and promotion moved
      // expires_at past that.
      const remainingMs = new Date(known.trial.expires_at).getTime() - Date.now();
      if (remainingMs > 0) {
        res.cookie(TRIAL_COOKIE_NAME, known.trial.cookie_token, {
          ...trialCookieOptions(),
          maxAge: remainingMs,
        });
      }
    }
    return { kind: 'trial', ...known };
  }

  const mintKey = rateLimitIpKey(req.ip);
  if (!mintLimiter.allow(mintKey)) {
    return { kind: 'mint_refused', retryAfterSeconds: mintLimiter.retryAfterSeconds(mintKey) };
  }
  const trial = await mintTrial(pool, req);
  res.cookie(TRIAL_COOKIE_NAME, trial.cookie_token, {
    ...trialCookieOptions(),
    maxAge: TRIAL_TTL_MS,
  });
  return { kind: 'trial', trial, paidKeeper: null };
}

function trialCookieOptions(): { httpOnly: true; secure: boolean; sameSite: 'lax'; path: string } {
  return { httpOnly: true, secure: !isDevOrTestEnv(), sameSite: 'lax', path: '/' };
}

/**
 * Build the trial-cookie middleware bound to a pg Pool.
 */
export function trialCookie(
  pool: Pool,
  opts: { mintLimiter?: MintLimiter } = {}
) {
  return async function trialCookieHandler(
    req: Request,
    res: Response,
    next: NextFunction
  ): Promise<void> {
    try {
      const resolved = await resolveTrial(pool, req, res, { mintLimiter: opts.mintLimiter });
      if (resolved.kind === 'mint_refused') {
        res
          .status(429)
          .set('Retry-After', String(resolved.retryAfterSeconds))
          .json({
            error: 'rate_limited',
            message: 'Too many new sessions from this address. Please try again later.',
          });
        return;
      }
      req.harborTrial = resolved.trial;
      if (resolved.paidKeeper) req.harborPaidKeeper = resolved.paidKeeper;
      next();
    } catch (err) {
      next(err);
    }
  };
}

// ---------- helpers ----------

/**
 * Read a single cookie value out of the raw Cookie header. Avoids the
 * cookie-parser dependency for one tiny lookup.
 */
function readCookie(req: Request, name: string): string | null {
  const header = req.headers.cookie;
  if (!header) return null;
  const parts = header.split(';');
  for (const part of parts) {
    const idx = part.indexOf('=');
    if (idx < 0) continue;
    const k = part.slice(0, idx).trim();
    if (k !== name) continue;
    const v = part.slice(idx + 1).trim();
    try {
      return decodeURIComponent(v);
    } catch {
      // Malformed percent-encoding ("%", "%E0%A4%A"): treat as no cookie so
      // the visitor gets a fresh trial instead of a 500.
      return null;
    }
  }
  return null;
}

/**
 * Token shape: 64 hex chars (32 random bytes, hex encoded). Reject anything
 * else before hitting the DB.
 */
function isValidTokenShape(token: string): boolean {
  return /^[0-9a-f]{64}$/i.test(token);
}

/**
 * Generate a fresh opaque bearer token: 32 bytes of CSPRNG output, hex
 * encoded. (Earlier versions HMAC'd a random nonce with SESSION_SECRET and
 * discarded the nonce, which gave a 128-bit token and no verifiable
 * signature; plain random bytes are simpler, stronger and do not read like
 * a signing scheme that is not there.)
 */
function generateToken(): string {
  return crypto.randomBytes(32).toString('hex');
}

/**
 * The row for this token if it has not expired, released or not. The caller
 * decides what a released row is worth (see "After payment" at the top).
 */
async function loadUnexpiredTrial(
  pool: Pool,
  token: string
): Promise<TrialKeeperContext | null> {
  const { rows } = await pool.query<TrialKeeperContext>(
    `SELECT trial_id, cookie_token, companion_name, companion_assigned,
            started_at, expires_at, last_seen_at, released_at, handoff_build_id
     FROM trial_keepers
     WHERE cookie_token = $1
       AND expires_at > NOW()
     LIMIT 1`,
    [token]
  );
  return rows.length > 0 ? rows[0] : null;
}

/**
 * For a released trial: the build it paid for and the keeper it was promoted
 * into, or null. The statement is shared with the runtime
 * (`PROMOTED_KEEPER_OF_TRIAL_SQL`), which asks the same question when a turn
 * outlives the release; the answer is a fact recorded by the promotion, not
 * an inference from a build and an account that may belong to someone else's
 * payment.
 */
async function loadPaidKeeper(
  pool: Pool,
  trial: TrialKeeperContext
): Promise<PaidKeeperContext | null> {
  if (!trial.handoff_build_id) return null;
  const { rows } = await pool.query<{ build_id: string; customer_id: string }>(
    PROMOTED_KEEPER_OF_TRIAL_SQL,
    [trial.trial_id]
  );
  if (rows.length === 0) return null;
  return { trialId: trial.trial_id, buildId: rows[0].build_id, customerId: rows[0].customer_id };
}

async function mintTrial(pool: Pool, req?: Request): Promise<TrialKeeperContext> {
  const token = generateToken();
  const companion = assignCompanionName(); // 'Haven' for MVP

  // Stamp bot classification + entry attribution at first touch so funnel
  // queries can separate real humans from bots/preview-fetchers and attribute
  // the entry channel. Without this, the table is ~97% ghost rows that lie.
  const ua = req?.headers['user-agent'];
  const isSynthetic = isBotUserAgent(ua);
  const uaHash = hashUserAgent(ua);
  // Both URLs are stored without their query string. A /lighthouse URL can
  // carry a Stripe Checkout `session_id` or an admin's `dev_trial` id, and a
  // Referer can carry whatever the page before had in its address; neither
  // belongs in an attribution column. The campaign parameters, the one thing
  // attribution reads from a query, are kept separately in entry_utm.
  const referrer = loggablePath(
    (req?.headers['referer'] || req?.headers['referrer']) as string | undefined
  );
  const entryPath = loggablePath(req?.originalUrl || req?.path);
  const entryUtm = extractUtm(req);

  const { rows } = await pool.query<TrialKeeperContext>(
    `INSERT INTO trial_keepers
       (cookie_token, companion_name, companion_assigned, expires_at,
        is_synthetic, ua_hash, entry_referrer, entry_path, entry_utm)
     VALUES ($1, $2, $3, NOW() + INTERVAL '${TRIAL_TTL_DAYS} days',
             $4, $5, $6, $7, $8)
     RETURNING trial_id, cookie_token, companion_name, companion_assigned,
               started_at, expires_at, last_seen_at`,
    [
      token,
      companion,
      companion,
      isSynthetic,
      uaHash,
      referrer || null,
      entryPath || null,
      entryUtm,
    ]
  );
  return rows[0];
}

/**
 * Pull a compact utm string from the request query for channel attribution.
 * e.g. "?utm_source=client-site&utm_medium=footer" -> "client-site/footer".
 * Returns null when no utm params are present.
 */
function extractUtm(req?: Request): string | null {
  const q = req?.query as Record<string, unknown> | undefined;
  if (!q) return null;
  const src = typeof q.utm_source === 'string' ? q.utm_source : '';
  const med = typeof q.utm_medium === 'string' ? q.utm_medium : '';
  const cmp = typeof q.utm_campaign === 'string' ? q.utm_campaign : '';
  if (!src && !med && !cmp) return null;
  return [src, med, cmp].filter(Boolean).join('/').slice(0, 255);
}

export const _internals = {
  TRIAL_COOKIE_NAME,
  TRIAL_TTL_DAYS,
  readCookie,
  isValidTokenShape,
  generateToken,
};
