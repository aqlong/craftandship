/**
 * Rate limiters for the CAS Harbor companion endpoints
 * (POST /lighthouse/table/turn and POST /lighthouse/build).
 *
 * Why
 * ===
 * `/table/turn` invokes Anthropic Sonnet (~$0.005-$0.015 per turn after
 * caching) for every keeper utterance, and `/build` runs an Opus spec
 * extraction (~$0.18). Without limits, one client can hammer the endpoints
 * and drive Anthropic API spend to the org daily cap.
 *
 * Keying (read this before trusting the numbers)
 * ==============================================
 * Two limiters guard the turn endpoint, because neither is enough alone:
 *
 *  1. Per trial (`companionTurnRateLimit`): keyed on `req.harborTrial.trial_id`,
 *     so a single cookie cannot multiply itself across IPs. A client that
 *     drops its cookie is handed a NEW trial id by the trial-cookie
 *     middleware on every request, and therefore a new bucket, so this
 *     limiter does not bound a cookie-less client at all.
 *  2. Per client address (`companionTurnIpRateLimit`): keyed on `req.ip`
 *     (IPv6 collapsed to its /64). This is what bounds the cookie-rotation
 *     case, at the price of sharing a budget between people behind one NAT.
 *
 * The per-identity daily cost cap in companion/runtime.ts is keyed on the
 * trial row as well, so it has the same blind spot as limiter 1; limiter 2
 * and the trial-mint limit in trial-cookie.ts are the real ceiling for a
 * client that rotates cookies. `req.ip` is only as good as the `trust proxy`
 * setting (TRUST_PROXY_HOPS, app.ts): behind one proxy and with the value 1
 * it is the client address the proxy saw; with the value 1 and nothing in
 * front, it is whatever the client wrote in X-Forwarded-For, and every
 * per-address limit in this app can be walked past.
 *
 * What a limiter may hold in memory
 * =================================
 * Counters live in process memory, and a limiter runs before whatever would
 * refuse the request for other reasons, so both the size of one key and the
 * number of keys have to be bounded here:
 *
 *  - every key is built from a validated address (`rateLimitIpKey`: a string
 *    that is not an IP address becomes the one key "ip:invalid"), from an id
 *    this server issued, cut to MAX_ID_KEY_CHARS, or (the auth limiters only)
 *    from the typed email, which auth-rate-limit.ts cuts or hashes; no key
 *    holds text of a length the client chooses;
 *  - every limiter in the app is built by boundedRateLimit below, which
 *    keeps its counters in a BoundedRateLimitStore (at most
 *    RATE_LIMIT_MAX_KEYS keys) and cuts any key to MAX_RATE_LIMIT_KEY_CHARS.
 *
 * Tunables (env)
 * ==============
 *   CAS_HARBOR_TURN_WINDOW_MS    default 15min (900_000)
 *   CAS_HARBOR_TURN_MAX          default 60 turns / window / trial
 *   CAS_HARBOR_TURN_IP_MAX       default 150 turns / window / address
 *   CAS_HARBOR_BUILD_MAX         default 6 build attempts / hour
 *
 * Test-mode short-circuit: when NODE_ENV=test the limits are raised to
 * 10_000 so unit tests aren't accidentally rate-limited by repeated
 * supertest calls.
 */

import { createHash } from 'crypto';
import { isIP } from 'net';
import rateLimit, {
  type ClientRateLimitInfo,
  type Options,
  type RateLimitRequestHandler,
  type Store,
} from 'express-rate-limit';
import type { Request } from 'express';

/**
 * How many keys one limiter holds. express-rate-limit's own memory store has
 * no ceiling: a million distinct keys cost it about 240 MB (measured, 250
 * bytes a key), kept for up to two windows. At this ceiling a full store is
 * about 6 MB (measured), however many distinct keys are sent at it. A flood
 * of distinct keys fills the limiter it is aimed at, not all of them.
 */
export const RATE_LIMIT_MAX_KEYS = 20_000;
/** Expired keys removed per new key, so no single request pays for a big sweep. */
const SWEEP_PER_INSERT = 64;

interface Counter {
  totalHits: number;
  resetTime: Date;
}

/**
 * A fixed-window counter store for express-rate-limit with a ceiling on the
 * number of keys.
 *
 * Every key's window is the same length and starts when the key is inserted,
 * so the Map's insertion order is also the order in which windows end:
 * expired keys are always at the front. A new key sweeps a few expired ones
 * from the front, and if the store is still over its ceiling the key whose
 * window started longest ago is dropped.
 *
 * Dropping a live key forgets its count, which loosens the limit for that
 * one client and never refuses anybody. It only happens while more than
 * `maxKeys` distinct keys are live at once, and distinct keys are not free to
 * make: each is an address, an id this server issued, or (the auth limiters)
 * an email that got past the per-address bucket first. Memory, which a flood
 * could otherwise take from every request the process serves, is what this
 * trades that for.
 */
export class BoundedRateLimitStore implements Store {
  /** Counters are per instance; tells express-rate-limit not to expect sharing. */
  readonly localKeys = true;
  private windowMs = 60_000;
  private readonly counters = new Map<string, Counter>();

  constructor(private readonly maxKeys: number = RATE_LIMIT_MAX_KEYS) {}

  init(options: Options): void {
    this.windowMs = options.windowMs;
  }

  get size(): number {
    return this.counters.size;
  }

  async get(key: string): Promise<ClientRateLimitInfo | undefined> {
    const counter = this.counters.get(key);
    return counter && counter.resetTime.getTime() > Date.now() ? counter : undefined;
  }

  async increment(key: string): Promise<ClientRateLimitInfo> {
    const now = Date.now();
    let counter = this.counters.get(key);
    if (!counter || counter.resetTime.getTime() <= now) {
      // Delete first so the key moves to the back: newest window last.
      this.counters.delete(key);
      counter = { totalHits: 0, resetTime: new Date(now + this.windowMs) };
      this.counters.set(key, counter);
      this.makeRoom(now);
    }
    counter.totalHits += 1;
    return counter;
  }

  async decrement(key: string): Promise<void> {
    const counter = this.counters.get(key);
    if (counter && counter.totalHits > 0) counter.totalHits -= 1;
  }

  async resetKey(key: string): Promise<void> {
    this.counters.delete(key);
  }

  async resetAll(): Promise<void> {
    this.counters.clear();
  }

  private makeRoom(now: number): void {
    let swept = 0;
    for (const [key, counter] of this.counters) {
      const overCeiling = this.counters.size > this.maxKeys;
      const expired = counter.resetTime.getTime() <= now;
      if (!overCeiling && (!expired || swept >= SWEEP_PER_INSERT)) break;
      this.counters.delete(key);
      swept += 1;
    }
  }
}

/** Ids this server issued (a trial id, a customer id) are short; a key keeps this much. */
export const MAX_ID_KEY_CHARS = 64;
/** The longest key a limiter stores. A longer one is stored as its SHA-256. */
export const MAX_RATE_LIMIT_KEY_CHARS = 128;

function boundedKey(key: string): string {
  const text = String(key);
  if (text.length <= MAX_RATE_LIMIT_KEY_CHARS) return text;
  return `sha256:${createHash('sha256').update(text).digest('hex')}`;
}

/**
 * The one place in web/src that builds an express-rate-limit limiter. Every
 * limiter in the app is made here, and a unit test
 * (middleware.rate-limit-callsites.test.ts) fails if another file imports the
 * library to build its own. So the two memory bounds hold for all of them by
 * construction, whatever the call site passes:
 *
 *  - counters live in a BoundedRateLimitStore, never in the library's memory
 *    store, which keeps every key it is sent;
 *  - whatever the key generator returns is cut to MAX_RATE_LIMIT_KEY_CHARS
 *    (a longer key is replaced by its hash), and a limiter given no key
 *    generator keys on rateLimitIpKey, not on the library's raw `req.ip`.
 */
export function boundedRateLimit(options: Omit<Partial<Options>, 'store'>): RateLimitRequestHandler {
  const keyGenerator = options.keyGenerator ?? ((req: Request): string => rateLimitIpKey(req.ip));
  return rateLimit({
    ...options,
    keyGenerator: (req, res) => {
      const key = keyGenerator(req, res);
      return typeof key === 'string' ? boundedKey(key) : key.then(boundedKey);
    },
    store: new BoundedRateLimitStore(),
  });
}

export const COMPANION_TURN_DEFAULT_WINDOW_MS = 15 * 60 * 1000;
export const COMPANION_TURN_DEFAULT_MAX_PER_WINDOW = 60;
export const COMPANION_TURN_TEST_MODE_MAX = 10_000;
export const COMPANION_TURN_DEFAULT_MAX_PER_IP = 150;
export const COMPANION_BUILD_DEFAULT_MAX_PER_WINDOW = 6;
export const COMPANION_BUILD_WINDOW_MS = 60 * 60 * 1000;

/**
 * Normalize a client address into a rate-limit key. IPv4 (and IPv4-mapped
 * IPv6) addresses are used as-is; any other IPv6 address is collapsed to its
 * /64 prefix, because a single subscriber typically controls a whole /64 and
 * could otherwise rotate through addresses to dodge a per-address bucket.
 * (express-rate-limit 7.x keys on the bare `req.ip`.)
 *
 * Anything that is not an IP address gets the single key "ip:invalid". With
 * `trust proxy` set higher than the number of proxies really in front,
 * `req.ip` is the text of a header the client wrote, up to the 16 KB Node
 * accepts for headers; that text must not become a key.
 */
export function rateLimitIpKey(ip: string | undefined | null): string {
  if (!ip) return 'ip:unknown';
  if (isIP(ip) === 0) return 'ip:invalid';
  const mapped = ip.match(/^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i);
  if (mapped) return `ip:${mapped[1]}`;
  if (!ip.includes(':')) return `ip:${ip}`;
  const [head, tail = ''] = ip.split('::');
  const headGroups = head ? head.split(':') : [];
  const tailGroups = ip.includes('::') && tail ? tail.split(':') : [];
  const missing = ip.includes('::') ? 8 - headGroups.length - tailGroups.length : 0;
  const groups = [
    ...headGroups,
    ...Array(Math.max(missing, 0)).fill('0'),
    ...tailGroups,
  ].slice(0, 4);
  while (groups.length < 4) groups.push('0');
  return `ip6:${groups.map((g) => g.toLowerCase().replace(/^0+(?=.)/, '')).join(':')}`;
}

/** Key generator for the per-address limiters. */
export function companionIpKeyGen(req: Request): string {
  return rateLimitIpKey(req.ip);
}

/**
 * Key generator for the limiters on routes a customer uses while logged in:
 * the customer id from the session, else the client address. The id is one
 * this server wrote into the session; it is cut to MAX_ID_KEY_CHARS all the
 * same. The address goes through rateLimitIpKey, so a request with no session
 * is keyed like every other per-address limiter (validated, IPv6 by /64).
 */
export function customerOrAddressKey(req: Request): string {
  const customerId = req.session?.customerId;
  return customerId ? `customer:${String(customerId).slice(0, MAX_ID_KEY_CHARS)}` : rateLimitIpKey(req.ip);
}

/**
 * Trial-cookie-keyed key generator. Falls back to IP when no trial is
 * attached. Exported so unit tests can exercise the keying contract
 * without standing up the full middleware.
 */
export function companionTurnKeyGen(req: Request): string {
  const harborTrial = (req as Request & { harborTrial?: { trial_id?: string } })
    .harborTrial;
  if (harborTrial && typeof harborTrial.trial_id === 'string') {
    return `trial:${harborTrial.trial_id.slice(0, MAX_ID_KEY_CHARS)}`;
  }
  return rateLimitIpKey(req.ip);
}

/**
 * Factory for unit tests. Production code uses the default singleton
 * `companionTurnRateLimit` below; tests override max/window via the
 * factory rather than re-importing the module with cache-busting query
 * strings (which fail under vitest's transformer).
 */
export function makeCompanionTurnRateLimit(opts: {
  windowMs?: number;
  max?: number;
  keyGenerator?: (req: Request) => string;
  message?: string;
} = {}) {
  return boundedRateLimit({
    windowMs: opts.windowMs ?? COMPANION_TURN_DEFAULT_WINDOW_MS,
    max: opts.max ?? COMPANION_TURN_DEFAULT_MAX_PER_WINDOW,
    message: {
      error: 'rate_limited',
      message:
        opts.message ??
        'You are sending messages quickly; pause for a moment, then continue.',
    },
    standardHeaders: true,
    legacyHeaders: false,
    keyGenerator: opts.keyGenerator ?? companionTurnKeyGen,
  });
}

// Parse an env int, honoring an explicit 0. `parseInt(env) || DEFAULT`
// silently swallows 0 (the natural value to lock the endpoint with max=0),
// so use a finite-check fallback instead.
function envInt(raw: string | undefined, fallback: number): number {
  if (raw === undefined || raw.trim() === '') return fallback;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? n : fallback;
}

const WINDOW_MS = envInt(
  process.env.CAS_HARBOR_TURN_WINDOW_MS,
  COMPANION_TURN_DEFAULT_WINDOW_MS
);
const MAX_PER_WINDOW =
  process.env.NODE_ENV === 'test'
    ? COMPANION_TURN_TEST_MODE_MAX
    : envInt(
        process.env.CAS_HARBOR_TURN_MAX,
        COMPANION_TURN_DEFAULT_MAX_PER_WINDOW
      );

export const companionTurnRateLimit = makeCompanionTurnRateLimit({
  windowMs: WINDOW_MS,
  max: MAX_PER_WINDOW,
});

/**
 * Per-address turn limiter: the bound that still holds when a client throws
 * its trial cookie away on every request (see "Keying" above).
 */
export const companionTurnIpRateLimit = makeCompanionTurnRateLimit({
  windowMs: WINDOW_MS,
  max:
    process.env.NODE_ENV === 'test'
      ? COMPANION_TURN_TEST_MODE_MAX
      : envInt(
          process.env.CAS_HARBOR_TURN_IP_MAX,
          COMPANION_TURN_DEFAULT_MAX_PER_IP
        ),
  keyGenerator: companionIpKeyGen,
});

/**
 * POST /lighthouse/build can run a paid Opus extraction without ending in a
 * dispatched build: a 409 barren_spec refusal, a reply that is not a spec
 * and a failed call (both 502) each cost the call. (The refusals made before
 * the extraction, not_ready among them, cost nothing.) Keyed on the trial
 * with an address fallback, plus the same address bucket via
 * `companionBuildIpRateLimit`.
 */
export const companionBuildRateLimit = makeCompanionTurnRateLimit({
  windowMs: COMPANION_BUILD_WINDOW_MS,
  max:
    process.env.NODE_ENV === 'test'
      ? COMPANION_TURN_TEST_MODE_MAX
      : envInt(
          process.env.CAS_HARBOR_BUILD_MAX,
          COMPANION_BUILD_DEFAULT_MAX_PER_WINDOW
        ),
  message: 'That was a lot of build attempts in a short time. Please try again in a little while.',
});

export const companionBuildIpRateLimit = makeCompanionTurnRateLimit({
  windowMs: COMPANION_BUILD_WINDOW_MS,
  max:
    process.env.NODE_ENV === 'test'
      ? COMPANION_TURN_TEST_MODE_MAX
      : envInt(
          process.env.CAS_HARBOR_BUILD_MAX,
          COMPANION_BUILD_DEFAULT_MAX_PER_WINDOW
        ) * 4,
  keyGenerator: companionIpKeyGen,
  message: 'That was a lot of build attempts in a short time. Please try again in a little while.',
});
