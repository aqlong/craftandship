// services/llm-budget.ts: the bounds on model spend that are not tied to one
// user. Kept apart from services/claude.ts so that every caller of a model
// (including the support chat, which has its own client) can share them.

// ---------------------------------------------------------------------------
// Process-wide daily call budget
// ---------------------------------------------------------------------------
//
// Every per-user limit in the routes (requests per minute, messages per day,
// turns per conversation) bounds one account or one address. None of them
// bounds many accounts or many addresses at once, and each call costs money.
// This is the ceiling under all of them: at most `dailyCallBudget()` model
// calls leave this process per 24-hour window through services/claude.ts
// (intake, revision, feature chat, admin analysis), the support chat and the
// Harbor spec extraction, whoever asks. When it is spent, callers get the same
// "high demand" answer as an open circuit until the window rolls over. (Haven
// conversation turns are budgeted separately, in dollars: companion/runtime.ts.
// Scheduled jobs are not counted: their schedule is their bound.) Anonymous
// visitors may use only part of it; see "The part of the budget anonymous
// visitors may use" below.
//
// The counter is in memory: it resets on restart and is per process, like the
// other limiters in this app. It is a spend backstop, not an accounting
// system; the provider-side spend limit on the API key is the outer one.
export const DAILY_CALL_BUDGET_DEFAULT = 3000;
const DAILY_CALL_WINDOW_MS = 24 * 60 * 60 * 1000;

export function dailyCallBudget(): number {
  const raw = parseInt(process.env.CAS_LLM_DAILY_CALL_CAP || '', 10);
  return Number.isFinite(raw) && raw >= 0 ? raw : DAILY_CALL_BUDGET_DEFAULT;
}

const callWindow = { startedAt: Date.now(), calls: 0, anonymousAdmitted: 0 };

/** Start a new window when the current one is over. Both counters share it. */
function rollWindow(now: number): void {
  if (now - callWindow.startedAt >= DAILY_CALL_WINDOW_MS) {
    callWindow.startedAt = now;
    callWindow.calls = 0;
    callWindow.anonymousAdmitted = 0;
  }
}

/** True when today's budget still has a call left. Takes nothing. */
export function dailyCallAvailable(now: number = Date.now()): boolean {
  if (now - callWindow.startedAt >= DAILY_CALL_WINDOW_MS) return dailyCallBudget() > 0;
  return callWindow.calls < dailyCallBudget();
}

/**
 * Take one call out of today's budget. Returns false (and takes nothing) when
 * the budget is spent. Exported for the support chat, which has its own
 * Anthropic client but must draw on the same budget.
 */
export function tryConsumeDailyCall(now: number = Date.now()): boolean {
  rollWindow(now);
  if (callWindow.calls >= dailyCallBudget()) {
    return false;
  }
  callWindow.calls += 1;
  return true;
}

// ---------------------------------------------------------------------------
// The part of the budget anonymous visitors may use
// ---------------------------------------------------------------------------
//
// The budget above is one pool, and two kinds of caller draw on it without an
// account: the legacy anonymous intake (POST /try/..., two calls a turn, live
// only while Harbor is disabled) and a cookie-only trial's spec extraction
// (POST /lighthouse/build). Everyone else has an account and a per-customer
// daily limit (middleware/llm-rate-limit.ts). Left alone, anonymous traffic
// from many addresses could spend the whole pool, and a paying customer's
// extraction or chat would then be answered "high demand" for the rest of the
// window.
//
// So anonymous callers are held to a share of the pool. A route that is about
// to make model calls for an anonymous visitor first asks
// tryAdmitAnonymousCalls(n), n being the most calls the request can make. The
// admissions are counted separately from the calls themselves (which
// tryConsumeDailyCall still counts when they happen), and refused once they
// would exceed anonymousCallBudget(). Anonymous requests can therefore cause
// at most that many calls per window, whatever the pool's state, and the rest
// of the pool, at least dailyCallBudget() - anonymousCallBudget() calls, can
// only be spent by callers with an account. The check and the count are one
// synchronous step, so concurrent requests cannot both pass on the last slot.
//
// An admission is for calls that may be made. The Harbor dispatch gives its
// one back (releaseAnonymousCalls) when the request ends before the call:
// otherwise requests that cost nothing would use up the share. It keeps it
// once the call is made, however the call ends, so the bound above still
// counts every call an anonymous request caused. The legacy anonymous intake
// gives nothing back; its admissions are an upper bound on its calls.
//
// It is a reservation against anonymous traffic only. Callers with accounts
// still share one pool among themselves; what bounds each of them is their
// per-customer limit.
export const ANONYMOUS_CALL_SHARE = 0.8;

/** How many of the daily calls anonymous visitors may cause. */
export function anonymousCallBudget(): number {
  return Math.floor(dailyCallBudget() * ANONYMOUS_CALL_SHARE);
}

/**
 * Admit an anonymous request that may make up to `calls` model calls. Returns
 * false, and counts nothing, when that would take anonymous traffic past its
 * share of the window. Does not take the calls out of the pool: the code that
 * makes them does that.
 */
export function tryAdmitAnonymousCalls(calls: number, now: number = Date.now()): boolean {
  rollWindow(now);
  if (callWindow.anonymousAdmitted + calls > anonymousCallBudget()) {
    return false;
  }
  callWindow.anonymousAdmitted += calls;
  return true;
}

/**
 * Give back admissions for calls that were never made: a request that was
 * admitted for `calls` and ended before reaching the model (nothing to send,
 * no key on this server, the pool itself spent). `admittedAt` is the time the
 * admission was taken; when the window has rolled over since, there is
 * nothing to give back and nothing is. Never call it for a call that was
 * made, whatever came back: the share bounds calls made, and a reply nobody
 * could use cost the same as a good one.
 */
export function releaseAnonymousCalls(calls: number, admittedAt: number, now: number = Date.now()): void {
  rollWindow(now);
  if (admittedAt < callWindow.startedAt) return;
  callWindow.anonymousAdmitted = Math.max(0, callWindow.anonymousAdmitted - calls);
}

/** Calls anonymous requests have been admitted for in the current window (for tests and diagnostics). */
export function anonymousCallsAdmitted(): number {
  return callWindow.anonymousAdmitted;
}

/**
 * Thrown by callers that have no softer way to say "the daily budget is
 * spent" (the spec extraction, which a route turns into a 503).
 */
export class LlmDailyBudgetError extends Error {
  constructor() {
    super('We are experiencing high demand right now. Please try again later.');
    this.name = 'LlmDailyBudgetError';
  }
}

/** Calls made in the current window (for tests and diagnostics). */
export function dailyCallsUsed(): number {
  return callWindow.calls;
}

export function resetDailyCallBudget(): void {
  callWindow.startedAt = Date.now();
  callWindow.calls = 0;
  callWindow.anonymousAdmitted = 0;
}

// ---------------------------------------------------------------------------
// Hard ceilings on user turns per conversation
// ---------------------------------------------------------------------------
//
// The *_MAX_TURNS values in services/claude.ts are soft: at that turn the
// model is told to wrap up, but nothing stops further messages, and every
// message resends the whole history. These stop a conversation from growing
// (and being re-billed) without end. The routes enforce them before calling
// the model.
export const INTAKE_HARD_TURN_CAP = 60;
export const REVISION_HARD_TURN_CAP = 30;
export const FEATURE_HARD_TURN_CAP = 30;
