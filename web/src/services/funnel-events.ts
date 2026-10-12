// funnel-events.ts - first-party funnel instrumentation. Records the key
// acquisition/activation steps server-side into the funnel_events table so
// the site is no longer flying blind (it had ZERO web analytics).
//
// Contract (mirrors services/email.ts sendEmail):
//   - FIRE-AND-FORGET: callers do `emitEvent(...).catch(() => {})` or just
//     `void emitEvent(...)`. It NEVER throws and NEVER blocks the response.
//   - A measurement insert must never break a real request. Every failure is
//     swallowed (logged at debug only) so a DB hiccup can't 500 the funnel.
//   - Privacy: the raw User-Agent is never stored, only isBot + a short hash,
//     and the path and the referrer are stored without their query strings
//     (a /lighthouse URL can carry a Stripe Checkout `session_id` or an
//     admin's `dev_trial` id). Nothing reads either query: event-specific
//     values go in `props`.

import type { Pool } from 'pg';
import type { Request } from 'express';
import { isBotUserAgent, hashUserAgent } from '../utils/bot-ua';
import { loggablePath } from '../utils/recent-errors';

export type FunnelEventName =
  | 'landing_view' // a marketing page (/, /pricing, /how-it-works, /gallery)
  | 'cta_click_begin' // clicked "Begin a conversation" (GET /try)
  | 'table_view' // loaded Haven's chat page (GET /lighthouse/table)
  | 'first_message' // sent their first message to Haven
  | 'build_dispatched' // a free preview build was queued
  | 'made_public'; // paid to make the site public

interface EmitOptions {
  name: FunnelEventName;
  req?: Request;
  trialId?: string | null;
  props?: Record<string, unknown>;
}

/**
 * Record one funnel event. Fire-and-forget; resolves to true on insert,
 * false on any (swallowed) failure. Safe to call without awaiting.
 */
export async function emitEvent(pool: Pool, opts: EmitOptions): Promise<boolean> {
  try {
    const ua = opts.req?.headers['user-agent'];
    const referrer = loggablePath(
      (opts.req?.headers['referer'] || opts.req?.headers['referrer']) as string | undefined
    );
    const path = loggablePath(opts.req?.originalUrl || opts.req?.path);
    const trialId =
      opts.trialId ?? opts.req?.harborTrial?.trial_id ?? null;

    await pool.query(
      `INSERT INTO funnel_events (name, trial_id, is_bot, ua_hash, path, referrer, props)
       VALUES ($1, $2, $3, $4, $5, $6, $7)`,
      [
        opts.name,
        trialId,
        isBotUserAgent(ua),
        hashUserAgent(ua),
        path || null,
        referrer || null,
        opts.props ? JSON.stringify(opts.props) : null,
      ]
    );
    return true;
  } catch (err) {
    // Never let instrumentation break a request. Debug-log only.
    if (process.env.NODE_ENV !== 'test') {
      console.debug(
        '[funnel-events] emit failed (non-blocking):',
        (err as Error).message
      );
    }
    return false;
  }
}

/**
 * Express helper: fire an event without awaiting, swallowing the promise so
 * no unhandled-rejection warning is produced. Use in route handlers where you
 * don't want to `void` + `.catch` inline.
 */
export function track(pool: Pool, opts: EmitOptions): void {
  void emitEvent(pool, opts).catch(() => {});
}
