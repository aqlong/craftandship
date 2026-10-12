import type { Request } from 'express';
import { boundedRateLimit, rateLimitIpKey } from './companion-rate-limit';

// The per-address limiters key on the client address through rateLimitIpKey
// (companion-rate-limit.ts), which collapses an IPv6 address to its /64 and
// gives anything that is not an address one shared key. express-rate-limit
// 7.x keys on the raw `req.ip` by default, so an IPv6 client could otherwise
// take a fresh address from its own /64 for every request and never hit the
// cap. Each limiter is built by boundedRateLimit, which caps how many keys it
// holds.
const clientKey = (req: Request): string => rateLimitIpKey(req.ip);

// IP-based rate limit for anonymous intake draft creation. Prevents
// cost bombs from bots hammering POST /try/start. 5 drafts per IP
// per 24 hours is generous for real humans (who rarely need more than
// 1-2 drafts) while capping abuse. Tightened from 10 after observing
// bot/crawler traffic generating greeting-only sessions.
// The in-memory store resets on restart and is per process, which is
// acceptable for a limiter this loose on a single-instance deployment.
export const anonDraftCreateRateLimit = boundedRateLimit({
  windowMs: 24 * 60 * 60 * 1000, // 24 hours
  max: process.env.NODE_ENV === 'test' ? 1000 : 5,
  message: {
    error:
      'Too many anonymous intake attempts from this address. Sign up to continue.',
  },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: clientKey,
});

// Separate (looser) limiter for anonymous message turns within an
// existing draft. The per-draft turn cap is the primary guardrail; this
// just catches edge cases where a bot tries to create many drafts and
// exchange messages across them.
export const anonMessageRateLimit = boundedRateLimit({
  windowMs: 60 * 1000, // 1 minute
  max: 20,
  message: {
    error: 'Too many messages. Please slow down.',
  },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: clientKey,
});

// Daily ceilings on anonymous turns. Each anonymous turn makes two model
// calls (the reply and the spec preview) and resends the whole draft, and the
// per-minute limiter above still allows 28,800 turns a day from one address.
//
//  - per address: ANON_DAILY_TURNS_PER_ADDRESS turns per 24 hours, enough for
//    several full previews;
//  - everyone together: ANON_DAILY_TURNS_TOTAL turns per 24 hours, so many
//    addresses at once cannot multiply the per-address figure. This one is
//    deliberately a shared bucket: when it is spent, anonymous previews stop
//    for everybody until the window rolls over, and visitors are told to sign
//    up (logged-in conversations have their own budget).
export const ANON_DAILY_TURNS_PER_ADDRESS = 150;
export const ANON_DAILY_TURNS_TOTAL = 2000;
const DAY_MS = 24 * 60 * 60 * 1000;
const ANON_DAILY_MESSAGE = {
  error: 'The free preview has reached its limit for today. Sign up to continue the conversation.',
};

/** Factory for tests; production uses the two singletons below. */
export function makeAnonDailyTurnLimit(opts: { max: number; shared: boolean; windowMs?: number }) {
  return boundedRateLimit({
    windowMs: opts.windowMs ?? DAY_MS,
    max: opts.max,
    message: ANON_DAILY_MESSAGE,
    standardHeaders: true,
    legacyHeaders: false,
    keyGenerator: opts.shared ? () => 'anon-intake:all' : clientKey,
  });
}

export const anonDailyTurnRateLimit = makeAnonDailyTurnLimit({
  max: process.env.NODE_ENV === 'test' ? 100_000 : ANON_DAILY_TURNS_PER_ADDRESS,
  shared: false,
});

export const anonGlobalDailyTurnRateLimit = makeAnonDailyTurnLimit({
  max: process.env.NODE_ENV === 'test' ? 100_000 : ANON_DAILY_TURNS_TOTAL,
  shared: true,
});
