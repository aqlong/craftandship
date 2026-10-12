/**
 * Daily ceiling on model-backed requests from one logged-in customer.
 *
 * The chat endpoints each have a per-minute limiter (30 a minute for intake,
 * revision and feature chats, 10 for support). Those stop bursts, but 30 a
 * minute is still 43,200 calls a day from one account, each of which resends
 * a growing conversation to a paid model. This limiter is shared by every
 * authenticated endpoint that can cause a model call (one counter per
 * customer across all of them):
 *
 *   GET  /intake                          (greeting for a new conversation)
 *   POST /intake/start, /intake/message
 *   POST /builds/:id/revision/message
 *   POST /projects/:id/features/new       (greeting for a new feature request)
 *   POST /features/:id/message
 *   POST /api/support/chat
 *
 * 300 a day is far above what building a website takes (an intake runs 5 to
 * 20 turns) and far below what a loop would do. Counters are in process
 * memory: they reset on restart and are per instance, like the other request
 * limiters here. The process-wide budget in services/llm-budget.ts is the
 * bound that holds across accounts.
 */

import type { Request, Response } from 'express';
import { boundedRateLimit, customerOrAddressKey } from './companion-rate-limit';
import { renderErrorPage } from '../utils/error-page';
import { isJsonRequest } from '../utils/json-request';

export const CUSTOMER_LLM_DAILY_MAX = 300;
const DAY_MS = 24 * 60 * 60 * 1000;

const LIMIT_MESSAGE =
  'You have reached the daily limit for conversations with our assistant. Please come back tomorrow, or write to support@craftandship.io.';

/**
 * Key: the customer when logged in (every route above requires it), else the
 * address. The same key the per-minute chat limiters use
 * (customerOrAddressKey in companion-rate-limit.ts).
 */
export function customerLlmKey(req: Request): string {
  return customerOrAddressKey(req);
}

/** Factory, so tests can exercise a small cap without waiting a day. */
export function makeCustomerLlmDailyLimit(opts: { max?: number; windowMs?: number } = {}) {
  return boundedRateLimit({
    windowMs: opts.windowMs ?? DAY_MS,
    max: opts.max ?? CUSTOMER_LLM_DAILY_MAX,
    standardHeaders: true,
    legacyHeaders: false,
    keyGenerator: customerLlmKey,
    // Two of the routes are pages (GET /intake, and the form that starts a
    // feature request); the rest are called by scripts. isJsonRequest tells
    // them apart the way the rest of the app does.
    handler: (req: Request, res: Response) => {
      if (!isJsonRequest(req)) {
        renderErrorPage(res, 429, { title: 'Please wait', error: LIMIT_MESSAGE });
        return;
      }
      res.status(429).json({ error: LIMIT_MESSAGE });
    },
  });
}

export const customerLlmDailyLimit = makeCustomerLlmDailyLimit();
