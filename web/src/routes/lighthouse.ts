/**
 * CAS Harbor /lighthouse routes.
 *
 * Hidden URL surface; not linked from the public site. Behind the
 * CAS_HARBOR_ENABLED feature flag (gated in app.ts).
 *
 *   GET  /lighthouse                       -> redirects to /lighthouse/table
 *   GET  /lighthouse/table                 -> chat UI (turn history; opening message on first visit)
 *   POST /lighthouse/table/turn            -> JSON { keeperText } -> { companionText, turnId, railFlags }
 *   GET  /lighthouse/table/recall          -> memory view (HTML, or ?format=json)
 *   POST /lighthouse/bypass                -> promotion without Stripe (NODE_ENV development or test only, behind a flag)
 *   POST /lighthouse/build                 -> spec extraction + build dispatch (keeper session or self-serve trial)
 *   GET  /lighthouse/preview               -> template family picker
 *   POST /lighthouse/preview/choose        -> persist the chosen template family
 *   GET  /lighthouse/lamp/:buildId         -> build status for the owner
 *   GET/POST /lighthouse/lamp/:buildId/make-public  -> Stripe Checkout for the subscription
 *   POST /lighthouse/lamp/:buildId/cancel-subscription -> cancel at period end (keeper only)
 *
 * Identity: every request passes through the trial-cookie middleware, which
 * attaches a trial identity (minting one if needed). A logged-in keeper is
 * `req.session.customerId`. Build routes (`/lamp/:buildId/*`) authorize by
 * ownership: the session keeper owns builds with `builds.customer_id`, a trial
 * owns the build stamped on its `handoff_build_id`, and the browser that paid
 * for a trial's build keeps owning that one build through the same cookie
 * after the payment released the trial (`req.harborPaidKeeper`; see
 * middleware/trial-cookie.ts, "After payment").
 *
 * The conversation itself (turns, notes, design choices) has one owner per
 * request, worked out in one place: `resolveCompanionIdentity` below. Every
 * handler that reads or writes `companion_turns` or `keeper_memory` asks it,
 * so the page that shows a conversation and the route that adds to it cannot
 * disagree about whose it is.
 *
 * One handler runs before all of that: `crisisFirst` answers a crisis message
 * on POST /lighthouse/table/turn before the cookie lookup, the rate limiters,
 * the email gate and the cost caps.
 */

import { Router, Request, Response, NextFunction } from 'express';
import type { Pool } from 'pg';
import * as fs from 'fs';
import * as path from 'path';
import { trialCookie, identifyTrialCookie } from '../middleware/trial-cookie';
import {
  turn,
  openingMessage,
  crisisReply,
  recordCrisisTurn,
  type CrisisReply,
  type TurnResult,
  CompanionDailyCostCapError,
} from '../companion/runtime';
import { scanCrisis, MAX_CRISIS_SCAN_CHARS } from '../companion/crisis_detection';
import { listMemoryNotes, memoryOwnerColumn, type MemoryOwner } from '../companion/memory_retrieval';
import {
  companionTurnRateLimit,
  companionTurnIpRateLimit,
  companionBuildRateLimit,
  companionBuildIpRateLimit,
  companionIpKeyGen,
  makeCompanionTurnRateLimit,
  COMPANION_TURN_TEST_MODE_MAX,
} from '../middleware/companion-rate-limit';
import {
  renderCompanionMarkdown,
  RENDER_COMPANION_MARKDOWN_CLIENT_JS,
} from '../companion/markdown';
import {
  promoteTrialToKeeper,
  unreleasedTrialForBuild,
  PUBLISHING_SUBSCRIPTION_STATUSES,
} from '../services/keeper_promotion';
import { AssistantNotConfiguredError, respondAssistantNotConfigured } from '../utils/assistant';
import {
  createHarborSubscriptionCheckoutSession,
  cancelHarborSubscriptionAtPeriodEnd,
  HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS,
  HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS,
  HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS,
} from '../services/stripe';
import {
  generateCsrfToken,
  validateCsrfToken,
  isAdminSession,
  isDevOrTestEnv,
  isCrossOriginWrite,
} from '../middleware/auth';
import { forwardAsyncErrors } from '../utils/async-routes';
import { renderErrorPage } from '../utils/error-page';
import { hashUnusablePassword } from '../utils/password-hash';
import { track } from '../services/funnel-events';
import {
  dispatchHarborTrialBuild,
  dispatchHarborKeeperBuild,
  dispatchFailureResponse,
  HarborDispatchError,
  isBuildableOwnerEmail,
  OWNER_EMAIL_NOT_BUILDABLE_MESSAGE,
} from '../services/harbor_dispatch';
import {
  buildReadinessOf,
  conversationBuildReadiness,
  extractableTurnCountSql,
} from '../services/spec_extraction';

interface TurnRowSlim {
  turn_id: string;
  keeper_text: string | null;
  companion_text: string;
  rail_flags: string[];
  created_at: Date;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** How many notes the conversation page's side panel renders, and the recall page lists. */
const MEMORY_PANEL_LIMIT = 25;
const MEMORY_RECALL_LIMIT = 200;

/**
 * The `?dev_trial=<uuid>` suffix that views append to their links, or ''.
 *
 * Only a well-formed UUID is ever echoed back. The value is attacker
 * controlled (any visitor can put anything in the query string, in any
 * environment), and table.ejs places it inside an inline script, so it must
 * not be reflected unvalidated. Whether the override is actually honoured is
 * decided separately by `applyDevTrialOverride`.
 */
function devTrialQueryFor(req: Request): string {
  const raw = req.query.dev_trial;
  return typeof raw === 'string' && UUID_RE.test(raw)
    ? `?dev_trial=${encodeURIComponent(raw)}`
    : '';
}

/**
 * Admin-gated dev override: when NODE_ENV is "development" or "test", a
 * `dev_trial` query param is present, AND the requester holds an admin
 * session (`isAdminSession`: a password login as ADMIN_EMAIL), look up that
 * trial directly and replace `req.harborTrial`. Lets the admin revisit any
 * past keeper's conversation or recall page by link instead of juggling
 * cookies.
 *
 * Hard-gated off everywhere else: with NODE_ENV=production, unset, or any
 * other value the function returns before the query string is even read.
 * **Hard-gated off for non-admin sessions even in dev** so a third party who
 * tunnels into a local dev server (or shares a screen) can never load another
 * keeper's memory or chat by pasting a UUID. The admin test is the shared
 * `isAdminSession`, not a comparison of the session's email: the payment
 * bypass below writes a caller-supplied email into the session, and that must
 * not be enough. Without admin auth, the override is silently skipped and the
 * cookie identity stays: the unauthorized requester sees their own
 * cookie-bound trial, never someone else's.
 *
 * Read-only by construction: only the GET handlers /table, /preview and
 * /table/recall call this function. No POST handler does, so a POST always
 * acts on the caller's own identity. (The /table GET still emits a
 * `table_view` funnel event attributed to the overridden trial.)
 *
 * Returns true when the override was applied. The caller then shows that
 * trial's rows whoever the admin is otherwise (see `viewedIdentity`). A trial
 * that has since been promoted shows as empty: its turns and notes moved to
 * the keeper, and this tool looks at trials.
 *
 * Every successful override is logged as `[lighthouse-dev-trial]`, and so is
 * an attempt by a session that is not the admin's, so misuse during local
 * development is auditable. A malformed UUID or an unknown trial is silent.
 */
async function applyDevTrialOverride(
  pool: Pool,
  req: Request
): Promise<boolean> {
  if (!isDevOrTestEnv()) return false;
  const raw = req.query.dev_trial;
  if (typeof raw !== 'string') return false;
  if (!UUID_RE.test(raw)) {
    return false;
  }
  const sessionEmail = (req.session?.customerEmail || '').trim().toLowerCase();
  if (!isAdminSession(req)) {
    console.log(
      '[lighthouse-dev-trial] override REFUSED (non-admin) for trial=%s on path=%s session=%s',
      raw,
      req.path,
      sessionEmail || '(none)'
    );
    return false;
  }
  const { rows } = await pool.query(
    `SELECT trial_id, cookie_token, companion_name, companion_assigned,
            started_at, expires_at, last_seen_at
     FROM trial_keepers
     WHERE trial_id = $1`,
    [raw]
  );
  if (rows.length === 0) return false;
  req.harborTrial = rows[0];
  console.log(
    '[lighthouse-dev-trial] override applied for trial=%s on path=%s admin=%s',
    raw,
    req.path,
    sessionEmail
  );
  return true;
}

/**
 * Whose conversation a request speaks in. There are three ways to talk to
 * Haven, and each keeps its turns and notes under a different key:
 *
 *   - a keeper with an account session (`req.session.customerId` that has a
 *     `keepers` row): rows keyed by `keeper_id`;
 *   - the browser that paid, holding only the trial cookie the payment
 *     released (`req.harborPaidKeeper`): the same keeper, the same rows;
 *   - anyone else: the cookie's trial, rows keyed by `trial_id`. That includes
 *     a logged-in customer who has no `keepers` row yet.
 *
 * Payment moves a trial's rows to the keeper (services/keeper_promotion.ts),
 * so after it the trial id finds nothing. Reading or writing by the wrong key
 * is silent: the page is simply empty, or the turn is stored where the next
 * page load does not look. Hence one function, used by the handlers that show
 * or add to a conversation: the conversation page and its notes panel, the
 * turn route, the recall page, and the template picker with its choice.
 *
 * Two places do not use it, on purpose. The build route counts and extracts
 * the trial's turns only on its trial path, where the trial is the owner by
 * construction. And the crisis handler files its turn from the trial cookie
 * alone, because it answers before the session is loaded: see fileCrisisTurn
 * for what that means for a keeper who is logged in.
 *
 * The account session wins over the paid cookie whenever the browser holds
 * both, keeper or not: logging in is the stronger statement. A logged-in
 * account with no keeper row is answered with its own trial, never with the
 * paid conversation (identifyTrialCookie has already given the request a fresh
 * trial in place of a paid cookie promoted into another account).
 *
 * Costs one query, and only for a request that carries an account session.
 */
type CompanionIdentity =
  | {
      kind: 'keeper';
      keeperId: string;
      via: 'session' | 'paid_cookie';
      /** The build this keeper identity leads to: the keeper row's for a session, the one paid for through a cookie. */
      handoffBuildId: string | null;
    }
  | { kind: 'trial'; trialId: string };

async function resolveCompanionIdentity(pool: Pool, req: Request): Promise<CompanionIdentity | null> {
  const sessionCustomerId = req.session?.customerId || null;
  if (sessionCustomerId) {
    const k = await pool.query<{ handoff_build_id: string | null }>(
      `SELECT handoff_build_id FROM keepers WHERE customer_id = $1`,
      [sessionCustomerId]
    );
    if (k.rows.length > 0) {
      return {
        kind: 'keeper',
        keeperId: sessionCustomerId,
        via: 'session',
        handoffBuildId: k.rows[0].handoff_build_id,
      };
    }
    // Logged in, but not a keeper: this account's conversation is its own
    // trial. The trial middleware has already replaced a paid cookie that
    // belongs to another account, so the trial here is an unreleased one.
    const sessionTrial = req.harborTrial;
    return sessionTrial && sessionTrial.released_at == null
      ? { kind: 'trial', trialId: sessionTrial.trial_id }
      : null;
  }
  const paid = req.harborPaidKeeper;
  if (paid) {
    return { kind: 'keeper', keeperId: paid.customerId, via: 'paid_cookie', handoffBuildId: paid.buildId };
  }
  const trial = req.harborTrial;
  return trial ? { kind: 'trial', trialId: trial.trial_id } : null;
}

/**
 * The identity a read-only page shows: the admin's `?dev_trial=` choice when
 * that override applies (development only), otherwise the requester's own.
 */
async function viewedIdentity(pool: Pool, req: Request): Promise<CompanionIdentity | null> {
  const overridden = await applyDevTrialOverride(pool, req);
  if (overridden && req.harborTrial) {
    return { kind: 'trial', trialId: req.harborTrial.trial_id };
  }
  return resolveCompanionIdentity(pool, req);
}

/** The same identity in the shape the memory helpers take. */
function memoryOwnerOf(who: CompanionIdentity): MemoryOwner {
  return who.kind === 'keeper' ? { keeperId: who.keeperId } : { trialId: who.trialId };
}

/**
 * Store a template choice as a `design_pref` note, for a trial and for a
 * keeper. Each inserts through a SELECT on the owner's row, so a missing owner
 * writes nothing instead of failing a foreign key.
 *
 * The trial statement also requires the trial to be unreleased and takes a
 * share lock on its row. Promotion locks that row first (FOR UPDATE) and
 * moves the trial's notes later in the same transaction, so the two cannot
 * interleave: either this insert commits first and promotion moves the note
 * with the rest, or promotion commits first and this statement, re-reading the
 * row it waited for, finds it released and inserts nothing.
 */
const INSERT_TRIAL_DESIGN_CHOICE_SQL = `
  INSERT INTO keeper_memory (trial_id, partition, category, content, keeper_phrase)
  SELECT t.trial_id, 'working', 'design_pref', $2, NULL
  FROM trial_keepers t
  WHERE t.trial_id = $1 AND t.released_at IS NULL
  FOR SHARE OF t
  RETURNING memory_id`;
const INSERT_KEEPER_DESIGN_CHOICE_SQL = `
  INSERT INTO keeper_memory (keeper_id, partition, category, content, keeper_phrase)
  SELECT k.customer_id, 'working', 'design_pref', $2, NULL
  FROM keepers k
  WHERE k.customer_id = $1
  RETURNING memory_id`;

/**
 * How much of a message the crisis scan reads. Five times the longest message
 * the turn endpoint accepts (4000 characters), so a crisis phrase in an
 * over-length paste is still caught; the rest of a larger body is not scanned.
 * The figure is the scan's own (MAX_CRISIS_SCAN_CHARS in crisis_detection.ts,
 * which cuts its input to it whoever calls); this is that constant under the
 * name the route has always exported.
 */
export const CRISIS_SCAN_MAX_CHARS = MAX_CRISIS_SCAN_CHARS;

/**
 * Crisis first, at the HTTP boundary of POST /lighthouse/table/turn.
 *
 * Mounted ahead of the trial-cookie middleware, both rate limiters, the
 * length check, the email gate and turn() with its cost caps. A message that
 * matches the crisis scan is answered here with the fixed script (HTTP 200,
 * the same JSON shape as an ordinary turn) and none of those run. What the
 * answer depends on: the request body and a regex scan. The handler does not
 * read the database, the session, the cookie or the model, so a refusal or a
 * failure in any of them cannot replace its answer. Every other request
 * passes through untouched.
 *
 * Where it runs: app.ts mounts it on POST /lighthouse/table/turn (when the
 * Harbor surface is enabled) directly after the body parsers, ahead of the
 * session middleware, the static files and the cross-origin guard. Only the
 * security headers, the optional preview password gate and the body parsers
 * run before it, so a request that carries a session cookie while the session
 * store's database is down is answered here like any other. The handler needs
 * nothing from the session, which is what makes that placement possible.
 * createLighthouseRouter registers it a second time as the first handler of
 * POST /table/turn, ahead of the trial-cookie middleware, for defense in
 * depth. That copy runs after the app's session middleware, so on its own it
 * would not cover the session-store case; a request the app.ts mount already
 * answered never reaches it.
 *
 * The response is written first. Only then, in the background, is the sender
 * identified from the trial cookie (read only; no trial is minted for this)
 * and the turn stored and the operator alerted. Both of those are bounded
 * (`crisisSideEffects` in companion/runtime.ts) because this path is not rate
 * limited. A sender with no usable cookie gets the same script; there is no
 * conversation to store the turn in, so it is logged and alerted only.
 *
 * The script names the default companion: the name is the one thing the
 * stored identity could have supplied, and waiting for a database to learn it
 * is exactly what this handler exists to avoid.
 *
 * The global cross-origin guard in app.ts runs before this router. The check
 * is repeated here so the handler is safe wherever it is mounted, including
 * ahead of that guard: a cross-site request is passed along to be refused,
 * never answered or recorded.
 */
export function crisisFirst(pool: Pool) {
  return function crisisFirstHandler(req: Request, res: Response, next: NextFunction): void {
    if (req.method !== 'POST' || isCrossOriginWrite(req)) {
      next();
      return;
    }
    const raw = (req.body as { keeperText?: unknown } | undefined)?.keeperText;
    const keeperText = typeof raw === 'string' ? raw.trim() : '';
    if (!keeperText) {
      next();
      return;
    }
    const scan = scanCrisis(keeperText.slice(0, CRISIS_SCAN_MAX_CHARS));
    if (!scan.flagged) {
      next();
      return;
    }

    const reply = crisisReply(scan.categories);
    res.json({
      companionText: reply.companionText,
      turnId: null,
      railFlags: reply.railFlags,
    });

    fileCrisisTurn(pool, req, keeperText, reply).catch((err) => {
      console.error('[companion-crisis] recording the crisis turn failed:', (err as Error).message);
    });
  };
}

/**
 * After the crisis reply has gone out: find out who sent it, store and alert.
 *
 * The sender is read from the trial cookie only: an active trial, or the
 * browser that paid (filed under its keeper). The account session is not
 * consulted, because the handler runs ahead of the session middleware and
 * must not come to depend on the session store. The cost: a keeper who is
 * logged in on a browser that did not make the payment gets the same reply,
 * but the stored copy is filed under that browser's own trial (or not stored,
 * if it has no cookie yet), so it is not in the keeper's conversation when
 * the page is reloaded, and the operator's alert names the trial.
 */
async function fileCrisisTurn(
  pool: Pool,
  req: Request,
  keeperText: string,
  reply: CrisisReply
): Promise<void> {
  let trialId: string | undefined;
  let keeperId: string | undefined;
  try {
    const who = await identifyTrialCookie(pool, req);
    if (who && who.paidKeeper) keeperId = who.paidKeeper.customerId;
    else if (who) trialId = who.trial.trial_id;
  } catch (err) {
    console.error(
      '[companion-crisis] could not identify the sender; the turn is alerted but not stored:',
      (err as Error).message
    );
  }
  await recordCrisisTurn({ pool, trialId, keeperId, keeperText, reply });
}

/**
 * How many times one trial cookie may submit the make-public form per hour.
 * Each accepted submission creates a Stripe Checkout session, so the route is
 * bounded per cookie and, at four times this figure, per client address (a
 * client that drops its cookie gets a new trial, and a new bucket, on every
 * request). Raised under NODE_ENV=test like the other Harbor limiters; a test
 * that exercises the limit passes its own figure to createLighthouseRouter.
 */
export const MAKE_PUBLIC_MAX_PER_HOUR = 10;
const MAKE_PUBLIC_WINDOW_MS = 60 * 60 * 1000;

export function createLighthouseRouter(pool: Pool, opts: { makePublicMaxPerHour?: number } = {}): Router {
  const router = forwardAsyncErrors(Router());
  const cookieMw = trialCookie(pool);
  const makePublicMax =
    opts.makePublicMaxPerHour ??
    (process.env.NODE_ENV === 'test' ? COMPANION_TURN_TEST_MODE_MAX : MAKE_PUBLIC_MAX_PER_HOUR);
  const makePublicMessage = 'That was a lot of attempts in a short time. Please try again in a little while.';
  const makePublicRateLimit = makeCompanionTurnRateLimit({
    windowMs: MAKE_PUBLIC_WINDOW_MS,
    max: makePublicMax,
    message: makePublicMessage,
  });
  const makePublicIpRateLimit = makeCompanionTurnRateLimit({
    windowMs: MAKE_PUBLIC_WINDOW_MS,
    max: makePublicMax * 4,
    keyGenerator: companionIpKeyGen,
    message: makePublicMessage,
  });

  // Before anything that can refuse or fail: see crisisFirst.
  router.post('/table/turn', crisisFirst(pool));

  // Apply trial cookie middleware to all /lighthouse paths.
  router.use(cookieMw);

  // GET /lighthouse: redirect into the chat surface.
  router.get('/', (_req: Request, res: Response) => {
    res.redirect('/lighthouse/table');
  });

  // GET /lighthouse/table: render chat UI.
  router.get('/table', async (req: Request, res: Response, next: NextFunction) => {
    try {
      // Whose conversation this page shows decides which rows are loaded:
      // payment re-keys a trial's turns and notes to the keeper, so a keeper
      // (an account session, or the browser that paid) must be read by
      // keeper id or the page comes up empty. See resolveCompanionIdentity.
      const who = await viewedIdentity(pool, req);
      const trial = req.harborTrial;
      if (!trial || !who) throw new Error('lighthouse: trial context missing on /table');

      const keeperId = who.kind === 'keeper' ? who.keeperId : null;
      const keeperHandoffBuildId = who.kind === 'keeper' ? who.handoffBuildId : null;
      let hasLiveSite = false;
      let liveUrl: string | null = null;
      if (keeperId && keeperHandoffBuildId) {
        // When the keeper has a build, look up its status so the
        // "make changes to my site" affordance can render only after
        // the site has actually gone live (status='live').
        const b = await pool.query<{ status: string; live_url: string | null }>(
          `SELECT status, live_url FROM builds WHERE id = $1`,
          [keeperHandoffBuildId]
        );
        if (b.rows.length > 0 && b.rows[0].status === 'live') {
          hasLiveSite = true;
          liveUrl = b.rows[0].live_url;
        }
      }
      const isKeeper = keeperId !== null;

      const turns = await loadTurnHistory(pool, memoryOwnerOf(who));

      // First-ever visit (no rows yet): emit a deterministic opening
      // message. We do NOT persist it (the runtime persists keeper-paired
      // turns; the opening is special-cased to keep the table clean).
      const openingTurn =
        turns.length === 0
          ? {
              turn_id: 'opening',
              keeper_text: null,
              companion_text: openingMessage(),
              rail_flags: [],
              created_at: new Date(),
            }
          : null;

      // Preserve the dev_trial param on the recall link so the founder
      // can navigate keeper -> recall -> keeper without losing the override.
      const devTrialQuery = devTrialQueryFor(req);

      // Side-panel: server-render the most recent notes so the page is
      // meaningful without JS. Client refreshes via the /recall JSON endpoint
      // after each turn. Same owner as the turns above.
      const initialMemory = await listMemoryNotes(pool, memoryOwnerOf(who), MEMORY_PANEL_LIMIT);

      // Self-serve build eligibility (non-keeper trials). Drives the
      // "Build my free preview" affordance. state: building (a build is already
      // attached) / ready (enough real conversation + a real human) / not_ready
      // (too few turns, or a synthetic/bot trial -> hide the CTA). The turns
      // counted are the ones the extraction may read (extractableTurnCountSql,
      // services/spec_extraction.ts), and the comparison is buildReadinessOf,
      // the same one POST /lighthouse/build makes: the button is offered
      // exactly when that route's readiness floor would let the request
      // through. Computed last
      // (after the render-data queries) + guarded so it appends to the query
      // sequence cleanly. The `?.` guard keeps an unmocked test query safe.
      let selfServeBuild:
        | { state: 'building' | 'ready' | 'not_ready'; buildId: string | null; hasEmail: boolean }
        | null = null;
      if (who.kind === 'trial') {
        const sbRes = await pool.query<{
          email_captured: string | null;
          handoff_build_id: string | null;
          handoff_at: Date | null;
          is_synthetic: boolean | null;
          turn_count: string;
        }>(
          `SELECT t.email_captured, t.handoff_build_id, t.handoff_at, t.is_synthetic,
                  ${extractableTurnCountSql('trial_id', 't.trial_id')} AS turn_count
           FROM trial_keepers t WHERE t.trial_id = $1`,
          [who.trialId]
        );
        if (sbRes?.rows && sbRes.rows.length > 0) {
          const r = sbRes.rows[0];
          const tc = parseInt(r.turn_count, 10) || 0;
          const hasEmail = !!(r.email_captured && r.email_captured.trim());
          if (r.handoff_build_id) {
            selfServeBuild = { state: 'building', buildId: r.handoff_build_id, hasEmail };
          } else if (r.handoff_at) {
            // This trial's build was dispatched and has since been deleted.
            // POST /lighthouse/build would refuse (build_already_used), so the
            // button is not offered.
            selfServeBuild = { state: 'not_ready', buildId: null, hasEmail };
          } else if (!r.is_synthetic && buildReadinessOf(tc).ready) {
            selfServeBuild = { state: 'ready', buildId: null, hasEmail };
          } else {
            selfServeBuild = { state: 'not_ready', buildId: null, hasEmail };
          }
        }
      }

      // A logged-in customer's build is owned by their account, and a build
      // owned by an account whose address the build engine refuses is not
      // dispatched (services/harbor_dispatch.ts, "The address a build is owned
      // by"). So where this page would offer such an account a build, it says
      // why it cannot instead of showing a button that would be refused. Read
      // only when a build would be offered to an account session; the `?.`
      // guards keep an unmocked test query safe, as above.
      let buildBlockedMessage: string | null = null;
      const sessionAccountId = req.session?.customerId || null;
      const offersBuild = isKeeper ? !keeperHandoffBuildId : selfServeBuild?.state === 'ready';
      if (sessionAccountId && offersBuild) {
        const accountRes = await pool.query<{ email: string | null }>(
          'SELECT email FROM customers WHERE id = $1',
          [sessionAccountId]
        );
        const storedEmail = accountRes?.rows?.[0]?.email;
        if (typeof storedEmail === 'string' && !isBuildableOwnerEmail(storedEmail)) {
          buildBlockedMessage = OWNER_EMAIL_NOT_BUILDABLE_MESSAGE;
        }
      }

      // Funnel: someone loaded Haven's chat page. The is_bot flag on the
      // event (from UA) lets us separate real prospects from preview fetchers.
      track(pool, { name: 'table_view', req });

      res.render('lighthouse/table', {
        title: `${trial.companion_name}, your companion`,
        layout: false,        // lighthouse uses its own layout, not the default cas layout
        companionName: trial.companion_name,
        turns: openingTurn ? [openingTurn] : turns,
        trialStartedAt: trial.started_at,
        devTrialQuery,
        // Local-only Stripe bypass affordance. Off unless the flag is set
        // AND NODE_ENV is development or test.
        bypassEnabled: harborStripeBypassEnabled(),
        isKeeper,
        keeperHandoffBuildId,
        hasLiveSite,
        liveUrl,
        selfServeBuild,
        buildBlockedMessage,
        // Markdown renderer for companion turns (parity with client-side
        // appended turns; see web/src/companion/markdown.ts).
        companionMarkdown: renderCompanionMarkdown,
        renderCompanionMarkdownClientJs: RENDER_COMPANION_MARKDOWN_CLIENT_JS,
        initialMemory,
      });
    } catch (err) {
      next(err);
    }
  });

  // POST /lighthouse/table/turn: process a keeper utterance.
  //
  // CSRF posture: this endpoint carries no token. It is called by same-origin
  // fetch from table.ejs with a JSON body, but the body parsers in app.ts
  // would accept a form-encoded body too, so "JSON only" is NOT what protects
  // it. What does: the global rejectCrossOriginWrites guard (app.ts refuses
  // any POST the browser marks as cross-site or whose Origin is not ours)
  // and SameSite=Lax on both the trial cookie and the session cookie, which
  // keeps them off cross-site POSTs in the first place. The same holds for
  // POST /build and POST /bypass below.
  //
  // Email gate: at `CAS_HARBOR_EMAIL_GATE_TURN_COUNT` turns (default 22),
  // the keeper must provide an email to continue. The route returns 402
  // with `requiresEmail: true` so the UI can render an inline capture.
  // The keeper may include `email` in this request body to provide it
  // and then the turn proceeds in the same call. No timing involved;
  // some keepers may hit 22 turns in an hour.
  router.post(
    '/table/turn',
    // Rate limits first (for everything except a crisis message, which
    // crisisFirst answered above). Two buckets, both needed: the per-address one
    // bounds a client that discards its cookie (each cookie-less request
    // gets a brand-new trial id and so a brand-new per-trial bucket and
    // daily cost cap); the per-trial one stops one cookie fanning out
    // across addresses. 60 turns / 15min per trial and 150 per address by
    // default; see middleware/companion-rate-limit.ts for the tunables and
    // the keying caveats. Test-mode raises both to 10_000. See
    // harbor-companion-turn-rate-limit-and-cost-cap (2026-05-09).
    companionTurnIpRateLimit,
    companionTurnRateLimit,
    async (req: Request, res: Response, next: NextFunction) => {
      try {
        const trial = req.harborTrial;
        if (!trial) {
          res.status(400).json({ error: 'no trial context' });
          return;
        }
        const keeperText =
          typeof req.body?.keeperText === 'string' ? req.body.keeperText.trim() : '';
        if (!keeperText) {
          res.status(400).json({ error: 'keeperText is required' });
          return;
        }
        if (keeperText.length > 4000) {
          res.status(413).json({ error: 'keeperText is too long (max 4000 chars)' });
          return;
        }

        // A keeper talks as the keeper, however they got here: an account
        // session, or the browser that paid (whose conversation was moved to
        // the keeper, and whose released trial can no longer hold turns). The
        // turn is stored under the same key the conversation page reads, so
        // it is still there after a reload. A keeper has an account and an
        // email already, so the email gate does not apply.
        const who = await resolveCompanionIdentity(pool, req);
        const keeperId = who && who.kind === 'keeper' ? who.keeperId : null;

        if (!keeperId) {
          // ---- Email gate ----
          const gateOutcome = await checkEmailGate(pool, trial.trial_id, req.body?.email);
          if (gateOutcome.status === 'gated') {
            res.status(402).json({
              requiresEmail: true,
              turnCount: gateOutcome.turnCount,
              threshold: gateOutcome.threshold,
              message:
                "I'd love to keep talking with you. Could you leave me an email so I can find you again next time you come back? No password yet, no card.",
            });
            return;
          }
          if (gateOutcome.status === 'invalid_email') {
            res.status(400).json({
              requiresEmail: true,
              error: 'invalid email format',
            });
            return;
          }
        }

        // Funnel: is this the trial's FIRST message? (the activation metric
        // that matters most: 191 reached the table, ~6 ever typed). Cheap
        // existence check BEFORE turn() inserts this row.
        let isFirstMessage = false;
        if (!keeperId) {
          try {
            const prior = await pool.query(
              'SELECT 1 FROM companion_turns WHERE trial_id = $1 LIMIT 1',
              [trial.trial_id]
            );
            isFirstMessage = prior.rows.length === 0;
          } catch {
            /* best-effort; never block the turn on instrumentation */
          }
        }

        const result: TurnResult = await turn(
          keeperId
            ? { pool, keeperId, keeperText }
            : { pool, trialId: trial.trial_id, keeperText }
        );

        if (isFirstMessage) {
          track(pool, {
            name: 'first_message',
            req,
            props: { chars: keeperText.length },
          });
        }

        // What the turn cost is recorded in companion_cost_log for the admin
        // dashboard; it is not the visitor's business and is not sent.
        res.json({
          companionText: result.companionText,
          turnId: result.turnId,
          railFlags: result.railFlags,
        });
      } catch (err) {
        // Daily cost cap exceeded: 429 with a friendly message. The figures
        // go to the server log so monitoring can correlate; they are not sent
        // to the client (the shared cap in particular is platform-wide
        // spend). Don't pass to next(err) because that's a 500 path; this is
        // a deliberate policy refusal, not an internal error.
        if (err instanceof CompanionDailyCostCapError) {
          console.warn(
            '[harbor-cost-cap] trial=%s refused by the %s daily cost cap: $%s of $%s',
            req.harborTrial?.trial_id,
            err.scope,
            (err.currentMicros / 1_000_000).toFixed(2),
            (err.capMicros / 1_000_000).toFixed(2)
          );
          res.status(429).json({
            error: 'daily_cost_cap_exceeded',
            message:
              "We have spent enough talking time today. Come back tomorrow and we will continue.",
          });
          return;
        }
        // No model key on this server: say so plainly instead of a 500.
        if (err instanceof AssistantNotConfiguredError) {
          respondAssistantNotConfigured(res);
          return;
        }
        next(err);
      }
    }
  );

  // POST /lighthouse/bypass: dev/local-only path to promote a trial to
  // a paid keeper without touching Stripe. Gated on
  // `CAS_HARBOR_STRIPE_BYPASS=true` AND NODE_ENV being "development" or
  // "test", so neither a production deployment nor one that forgot to set
  // NODE_ENV can auto-promote.
  //
  // Body: { email: string }. Creates a customers row for the email (or
  // reuses the caller's own), mints synthetic Stripe ids, calls
  // promoteTrialToKeeper(), sets session.customerId for keeper identity.
  // Returns { promoted: true, customerId, ... }.
  //
  // This route logs the caller in without a password, so it must never be a
  // way INTO an account that already exists:
  //   - an email that already has a customers row is refused unless the
  //     session is already logged in as that customer;
  //   - ADMIN_EMAIL is refused unless the session is already an admin
  //     session (a password login), whether or not the row exists yet.
  // What remains is "create a brand-new keeper account for an address nobody
  // has registered", which is the test fixture the route exists for.
  //
  // No real money flows. Use case: end-to-end testing of the keeper
  // promotion + handoff path before Stripe price IDs are wired.
  router.post('/bypass', async (req: Request, res: Response, next: NextFunction) => {
    try {
      if (process.env.CAS_HARBOR_STRIPE_BYPASS !== 'true') {
        res.status(403).json({ error: 'bypass_disabled' });
        return;
      }
      if (!isDevOrTestEnv()) {
        res.status(403).json({ error: 'bypass_blocked_in_production' });
        return;
      }
      const trial = req.harborTrial;
      if (!trial) {
        res.status(400).json({ error: 'no_trial_context' });
        return;
      }
      const rawEmail =
        typeof req.body?.email === 'string' ? req.body.email.trim().toLowerCase() : '';
      if (!isValidEmailShape(rawEmail)) {
        res.status(400).json({ error: 'valid_email_required' });
        return;
      }

      const adminEmail = (process.env.ADMIN_EMAIL || '').trim().toLowerCase();
      if (adminEmail && rawEmail === adminEmail && !isAdminSession(req)) {
        res.status(403).json({ error: 'admin_email_not_allowed' });
        return;
      }
      const existing = await pool.query<{ id: string }>(
        `SELECT id FROM customers WHERE email = $1 LIMIT 1`,
        [rawEmail]
      );
      const sessionCustomerId = req.session?.customerId || null;
      if (existing.rows.length > 0 && existing.rows[0].id !== sessionCustomerId) {
        res.status(409).json({
          error: 'account_exists',
          message:
            'An account with this email already exists. Log in as that account first, or use an email that has no account.',
        });
        return;
      }

      const customerId =
        existing.rows.length > 0
          ? existing.rows[0].id
          : await createBypassCustomer(pool, rawEmail);
      const subSuffix = `${trial.trial_id.slice(0, 8)}_${Date.now()}`;
      const result = await promoteTrialToKeeper({
        pool,
        trialId: trial.trial_id,
        customerId,
        email: rawEmail,
        stripeSubscriptionId: `sub_bypass_${subSuffix}`,
        stripeCustomerId: `cus_bypass_${trial.trial_id.slice(0, 8)}`,
        monthlyAmountCents: 0,
        slidingScaleAttestation: false,
        initialStatus: 'active',
      });

      // The same rule as the payment webhook: a trial is not promoted into an
      // account whose keeper already has a subscription. Nothing was written.
      if (result.refusal) {
        res.status(409).json({
          error: 'account_already_subscribed',
          message: 'This account already has a subscription, so a trial cannot be promoted into it.',
        });
        return;
      }

      // Authenticate the keeper for subsequent requests in this session.
      // `authenticatedAt` is deliberately NOT set: no password was checked,
      // and isAdminSession() requires it.
      if (req.session) {
        req.session.customerId = customerId;
        req.session.customerEmail = rawEmail;
      }

      res.json({
        bypass: true,
        promoted: result.promoted,
        customerId: result.customerId,
        conversationTurnsTransferred: result.conversationTurnsTransferred,
        memoryRowsTransferred: result.memoryRowsTransferred,
      });
    } catch (err) {
      next(err);
    }
  });

  // POST /lighthouse/build: spec extraction + build dispatch from a Harbor
  // conversation. Two paths, one service (services/harbor_dispatch.ts):
  //   - a keeper with an account session (session.customerId and a keepers
  //     row): dispatchHarborKeeperBuild, from the keeper's conversation;
  //   - everyone else builds the free preview of the conversation their
  //     trial cookie holds: dispatchHarborTrialBuild. A visitor with no
  //     account gives an address; a logged-in customer who is not a keeper
  //     yet gets the preview in the account they are logged in to.
  // Both are idempotent on the row's handoff_build_id (a second click returns
  // the build that is already attached without extracting again), both
  // refuse a conversation that has already had its build even when that
  // build no longer exists (`build_already_used`), and both count against the
  // customer's build-start limits (`build_limit`, answered 429).
  //
  // Note: this route does NOT call sendBuildQueued or any customer email.
  // The worker picks up the queued build and sends its own mail.
  router.post(
    '/build',
    // Cap attempts per trial and per address. An attempt that gets as far as
    // the extraction pays for a model call whether or not a build follows.
    companionBuildIpRateLimit,
    companionBuildRateLimit,
    async (req: Request, res: Response, next: NextFunction) => {
    try {
      const sessionCustomerId = req.session?.customerId || null;

      // ===== KEEPER PATH: a promoted/paid keeper with a session =====
      if (sessionCustomerId) {
        try {
          const result = await dispatchHarborKeeperBuild(pool, {
            customerId: sessionCustomerId,
            refuseBarrenSpec: true,
          });
          // The extraction's cost and context size stay server-side (the admin
          // dispatch route reports them to the operator).
          res.json({
            buildId: result.buildId,
            alreadyDispatched: result.alreadyDispatched,
            warnings: result.warnings,
          });
          return;
        } catch (dispErr) {
          // Logged in, but not a keeper: this account has no keeper
          // conversation. Its conversation is the one under the trial cookie
          // (resolveCompanionIdentity), so it builds a preview like any
          // other trial, below, into the account the session proves. Every
          // other failure goes to the outer catch, which answers it.
          if (!(dispErr instanceof HarborDispatchError) || dispErr.code !== 'keeper_not_found') throw dispErr;
        }
      } // ===== end keeper path =====

      // ===== TRIAL PATH: the conversation under the trial cookie builds a free preview =====
      // The whole server-side build pipeline works from a trialId (the admin
      // dispatch route proves it); here it is exposed self-serve with
      // guardrails, so a build is never made for a bot or an empty
      // conversation, and there is always a delivery email.
      const trial = req.harborTrial;
      if (!trial) {
        res.status(400).json({ error: 'no_trial_context' });
        return;
      }
      const guardRes = await pool.query<{
        is_synthetic: boolean | null;
        email_captured: string | null;
        handoff_build_id: string | null;
        handoff_at: Date | null;
      }>(
        `SELECT is_synthetic, email_captured, handoff_build_id, handoff_at
         FROM trial_keepers WHERE trial_id = $1`,
        [trial.trial_id]
      );
      const g = guardRes.rows[0];
      if (!g) {
        res.status(404).json({ error: 'trial_not_found' });
        return;
      }
      // Idempotent: a build is already attached to this trial.
      if (g.handoff_build_id) {
        res.json({
          buildId: g.handoff_build_id,
          alreadyDispatched: true,
          warnings: [],
          redirect: `/lighthouse/lamp/${g.handoff_build_id}`,
        });
        return;
      }
      // One free preview per trial. handoff_at records that this trial's build
      // was dispatched; it stays set when that build is deleted (the worker
      // prunes old failed builds, and handoff_build_id is ON DELETE SET NULL).
      // Refused here, before the email and readiness steps and before the
      // paid extraction; the dispatch service checks it again under a row lock.
      if (g.handoff_at) {
        res.status(409).json({
          error: 'build_already_used',
          message:
            'This conversation has already had its website built. Log in to your account to see it, or write to support@craftandship.io.',
        });
        return;
      }
      // Bot guard: never build for a synthetic (preview-fetcher / crawler) trial.
      if (g.is_synthetic) {
        res.status(403).json({ error: 'bot_blocked' });
        return;
      }

      // Whose account the preview goes to. A logged-in customer has proved
      // theirs, so the build is attached to it (a verified account included)
      // and nothing typed is consulted. Anyone else gives an address, and an
      // address is only a claim: see allowVerifiedAccount in the service.
      const sessionAccountEmail = sessionCustomerId
        ? await customerEmailById(pool, sessionCustomerId)
        : '';
      const anonymous = !sessionAccountEmail;
      if (sessionAccountEmail && !(g.email_captured || '').trim()) {
        // Record it as the address this conversation gave, the way a typed
        // one is recorded. The make-public page later lets the trial cookie
        // pay only into the account for that address.
        await pool.query(
          `UPDATE trial_keepers SET email_captured = $1
           WHERE trial_id = $2 AND email_captured IS NULL`,
          [sessionAccountEmail, trial.trial_id]
        );
      }

      // Email: the delivery channel and a seriousness filter. Capture inline
      // if provided; otherwise ask for it.
      let resolvedEmail = sessionAccountEmail || (g.email_captured || '').trim().toLowerCase();
      const bodyEmail =
        typeof req.body?.email === 'string' ? req.body.email.trim().toLowerCase() : '';
      if (!resolvedEmail && bodyEmail) {
        if (!isValidEmailShape(bodyEmail)) {
          res.status(400).json({ requiresEmail: true, error: 'invalid email format' });
          return;
        }
        await pool.query(
          `UPDATE trial_keepers SET email_captured = $1
           WHERE trial_id = $2 AND email_captured IS NULL`,
          [bodyEmail, trial.trial_id]
        );
        resolvedEmail = bodyEmail;
      }
      if (!resolvedEmail) {
        res.status(402).json({
          requiresEmail: true,
          message:
            "Leave your email and I'll build your free preview and send you the link when it's ready.",
        });
        return;
      }
      // Readiness floor: enough real conversation to build something true.
      // Counted as the extraction will read it (conversationBuildReadiness,
      // services/spec_extraction.ts): turns that carry the crisis flag or
      // produced a sacred note are never sent to the model, so they do not
      // make a conversation ready, and `turnCount` below is the count without
      // them.
      const readiness = await conversationBuildReadiness(pool, { trialId: trial.trial_id });
      const turnCount = readiness.extractableTurns;
      if (!readiness.ready) {
        res.status(409).json({
          error: 'not_ready',
          message: NOT_READY_MESSAGE,
          turnCount,
          minTurns: readiness.minTurns,
        });
        return;
      }
      // Dispatch the free preview build via the shared hardened logic.
      try {
        const result = await dispatchHarborTrialBuild(pool, {
          trialId: trial.trial_id,
          resolvedEmail,
          source: 'self_serve',
          refuseBarrenSpec: true,
          allowVerifiedAccount: !anonymous,
          anonymous,
        });
        track(pool, {
          name: 'build_dispatched',
          req,
          trialId: trial.trial_id,
          props: { buildId: result.buildId, turnCount },
        });
        res.json({
          buildId: result.buildId,
          alreadyDispatched: result.alreadyDispatched,
          warnings: result.warnings,
          redirect: `/lighthouse/lamp/${result.buildId}`,
        });
      } catch (dispErr) {
        if (dispErr instanceof HarborDispatchError) {
          if (
            dispErr.code === 'account_exists' ||
            dispErr.code === 'address_in_use' ||
            (dispErr.code === 'email_not_buildable' && anonymous)
          ) {
            // The typed address belongs to a verified account, to an account
            // that already has a conversation or a build (address_in_use), or
            // it is one the build engine would refuse as the site's admin
            // address. Nothing was attached to the account. account_exists and
            // email_not_buildable are decided before the extraction, so nothing
            // was spent on them. address_in_use is decided before the extraction
            // too, except when a build lands on the account while the extraction
            // runs: then the insert's lock decides it, after a model call that
            // was paid for, and no build or start row comes of that call. Forget
            // the address on this trial (it is only ever set once otherwise) so
            // the visitor can give another one. The cases get one answer, word
            // for word, so the answer adds nothing a visitor could tell them
            // apart by. (A logged-in customer whose own account has such an
            // address is told so plainly, below: nothing typed is consulted for
            // them, and asking for another address would lead nowhere.)
            //
            // The answer does not say an account exists. It cannot hide it
            // either: an address that would have been accepted gets a build,
            // and this one does not, so a visitor with a build-ready
            // conversation can still tell the two apart, at the rate the
            // limiters above allow. Removing that would take proof of the
            // mailbox before any build starts.
            await pool.query(
              `UPDATE trial_keepers SET email_captured = NULL
               WHERE trial_id = $1 AND handoff_build_id IS NULL`,
              [trial.trial_id]
            );
            res.status(409).json({
              error: 'email_not_usable',
              requiresEmail: true,
              message:
                'I could not use that email address for your preview. Please give me a different one, or log in first if you have an account here.',
            });
            return;
          }
        }
        throw dispErr;
      }
    } catch (err) {
      // Both paths above end here. Every failure that is not a server fault
      // has one answer, shared with the operator's dispatch
      // (dispatchFailureResponse in services/harbor_dispatch.ts): a refusal of
      // the dispatch by its code, nothing the extraction may read (409), a
      // failed model call or a reply that is not a spec (502), the daily
      // model-call budget spent (503 busy) and no model key on this server
      // (503, the answer a conversation turn gets).
      const answer = dispatchFailureResponse(err);
      if (answer) {
        // Nothing the extraction may read: the code says so, and the sentence
        // is the readiness floor's, which asks for more conversation and does
        // not tell a visitor that what they wrote was set aside. (A trial
        // reaches this only when a turn was set aside between the floor's
        // count and the extraction; a keeper's dispatch has no floor.)
        if (answer.body.error === 'nothing_to_extract') {
          res.status(answer.status).json({ ...answer.body, message: NOT_READY_MESSAGE });
          return;
        }
        res.status(answer.status).json(answer.body);
        return;
      }
      // Anything else is a server fault. The app's error handler answers this
      // route in JSON (utils/json-request.ts).
      next(err);
    }
    }
  );

  // GET /lighthouse/preview: show the keeper a picker of compatible
  // template families with a thumbnail-ish summary (shell description,
  // h1 size, accent density). Behind the same trial-cookie identity as
  // /table; admin-gated dev_trial override applies.
  //
  // Query params:
  //   ?archetype=<id>   override the auto-detected archetype
  //   ?hint=<family-id> pre-pin a family to position 0
  //
  // The picker reads the keeper's existing design_pref memory rows
  // (if any) to surface previously-extracted hints; otherwise scoring
  // is empty and the deterministic default tie-break (classic-business)
  // wins position 0.
  router.get('/preview', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const who = await viewedIdentity(pool, req);
      const trial = req.harborTrial;
      if (!trial || !who) {
        res.status(400).render('error-500', {
          title: 'Preview unavailable',
          message: 'No trial context. Open /lighthouse to start a session first.',
          layout: false,
        });
        return;
      }
      // The design choices are notes like any other: read them from whoever
      // owns the conversation now (a keeper's moved with it at payment).
      const owner = memoryOwnerColumn(memoryOwnerOf(who));
      const { listFamilies, listCompatibleFamilies, pickTopK } = await import(
        '../services/template-families'
      );
      const all = listFamilies();
      if (all.length === 0) {
        res.status(503).render('error-500', {
          title: 'Templates unavailable',
          message: 'No template families registered. compose/data/templates.json may be missing.',
          layout: false,
        });
        return;
      }

      // Archetype: query param wins, else design_pref memory row, else
      // null (-> show all families regardless of compat).
      let archetype: string | null = null;
      if (typeof req.query.archetype === 'string' && /^[a-z][a-z-]+$/.test(req.query.archetype)) {
        archetype = req.query.archetype;
      } else {
        const archRes = await pool.query<{ content: string }>(
          `SELECT content FROM keeper_memory
           WHERE ${owner.column} = $1 AND deleted_at IS NULL
                 AND category = 'design_pref'
           ORDER BY updated_at DESC
           LIMIT 1`,
          [owner.value]
        );
        if (archRes.rows.length > 0) {
          try {
            const parsed = JSON.parse(archRes.rows[0].content);
            if (typeof parsed.archetype === 'string') archetype = parsed.archetype;
          } catch {
            // ignore parse error; archetype stays null
          }
        }
      }

      // Build a HarborMetaSubset from existing design_pref rows.
      const meta: import('../services/template-families').HarborMetaSubset = {};
      if (typeof req.query.hint === 'string') meta.template_family_hint = req.query.hint;
      const prefRes = await pool.query<{ content: string }>(
        `SELECT content FROM keeper_memory
         WHERE ${owner.column} = $1 AND deleted_at IS NULL
               AND category = 'design_pref'
         ORDER BY updated_at DESC
         LIMIT 5`,
        [owner.value]
      );
      for (const row of prefRes.rows) {
        try {
          const parsed = JSON.parse(row.content);
          if (Array.isArray(parsed.mood_keywords) && !meta.mood_keywords) {
            meta.mood_keywords = parsed.mood_keywords;
          }
          if (typeof parsed.density_hint === 'string' && !meta.density_hint) {
            meta.density_hint = parsed.density_hint;
          }
          if (typeof parsed.hero_style_hint === 'string' && !meta.hero_style_hint) {
            meta.hero_style_hint = parsed.hero_style_hint;
          }
          if (typeof parsed.template_family_id === 'string' && !meta.template_family_hint) {
            meta.template_family_hint = parsed.template_family_id;
          }
        } catch {
          // skip malformed
        }
      }

      const families = archetype
        ? pickTopK(archetype, meta, 5, owner.value)
        : all.map((f) => ({ family: f, score: 0 }));

      // Detect real Playwright thumbnails on disk (regenerated via
      // `scripts/regen-template-thumbnails.py`). When a family has a
      // `home.png` for the picker hero, we render that instead of the
      // CSS-only mock; if missing, the EJS falls back to the mock.
      // The check is per-family-per-route so partial coverage works.
      // Test override: CAS_TEMPLATE_THUMBNAILS_DIR points the lookup
      // at a fixture directory.
      const thumbsRoot =
        process.env.CAS_TEMPLATE_THUMBNAILS_DIR ||
        path.resolve(__dirname, '..', '..', 'public', 'template-thumbnails');
      const thumbnails: Record<string, { home?: string; about?: string; services?: string }> = {};
      for (const scored of families) {
        const fid = scored.family.id;
        const entry: { home?: string; about?: string; services?: string } = {};
        for (const route of ['home', 'about', 'services'] as const) {
          const file = path.join(thumbsRoot, fid, `${route}.png`);
          if (fs.existsSync(file)) {
            entry[route] = `/template-thumbnails/${fid}/${route}.png`;
          }
        }
        thumbnails[fid] = entry;
      }

      const csrfToken = generateCsrfToken(req);
      res.render('lighthouse/preview', {
        title: 'Choose how your site looks',
        layout: false,
        companionName: trial.companion_name,
        archetype: archetype || '',
        families,
        thumbnails,
        compatibleArchetypes: archetype ? listCompatibleFamilies(archetype).length : all.length,
        csrfToken,
        devTrialQuery: devTrialQueryFor(req),
      });
    } catch (err) {
      next(err);
    }
  });

  // POST /lighthouse/preview/choose: persist the keeper's template
  // family choice. Writes a keeper_memory row tagged
  // category='design_pref' with content={template_family_id}. The
  // build-trigger flow (POST /lighthouse/build -> extraction) reads
  // these rows to populate _harbor_meta.template_family_hint at build
  // time, so the customer's choice flows through to the v2 compose
  // engine deterministically.
  //
  //
  // The row is written under whoever owns the conversation now, because the
  // extraction reads design choices by that same key: a keeper's by
  // keeper_id, a trial's by trial_id. (Written under the cookie's trial id
  // for a keeper, the choice would sit on a released or unrelated trial and
  // never reach a build.)
  //
  // Body: { template_family_id: string, _csrf: string } (the token may also
  // arrive in the x-csrf-token header; see validateCsrfToken).
  // Response: { ok, redirect: '/lighthouse/table' }
  router.post('/preview/choose', validateCsrfToken, async (req: Request, res: Response, next: NextFunction) => {
    try {
      const trial = req.harborTrial;
      const who = trial ? await resolveCompanionIdentity(pool, req) : null;
      if (!trial || !who) {
        res.status(400).json({ error: 'no_trial_context' });
        return;
      }
      const familyId = req.body?.template_family_id;
      if (typeof familyId !== 'string' || !/^[a-z][a-z-]{2,40}$/.test(familyId)) {
        res.status(400).json({ error: 'invalid_template_family_id' });
        return;
      }
      const { listFamilies } = await import('../services/template-families');
      const known = listFamilies();
      if (!known.some((f) => f.id === familyId)) {
        res.status(400).json({ error: 'unknown_template_family' });
        return;
      }
      const content = JSON.stringify({ template_family_id: familyId });
      const saved = await pool.query<{ memory_id: string }>(
        who.kind === 'keeper' ? INSERT_KEEPER_DESIGN_CHOICE_SQL : INSERT_TRIAL_DESIGN_CHOICE_SQL,
        [who.kind === 'keeper' ? who.keeperId : who.trialId, content]
      );
      if (saved.rows.length === 0) {
        // The owner changed under the request (the trial was just released by
        // a payment, or the account is gone). Nothing was written; a reload
        // picks up the current identity.
        res.status(409).json({
          error: 'conversation_moved',
          message: 'Please reload the page and choose again.',
        });
        return;
      }
      res.json({
        ok: true,
        template_family_id: familyId,
        redirect: '/lighthouse/table',
      });
    } catch (err) {
      next(err);
    }
  });

  // GET /lighthouse/table/recall: human-readable view of what the companion
  // remembers about whoever is asking (a trial, or a keeper by session or by
  // the cookie that paid). Rendered as HTML by default; ?format=json returns
  // the raw rows, which is what the conversation page's notes panel polls
  // after each turn.
  router.get('/table/recall', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const who = await viewedIdentity(pool, req);
      const trial = req.harborTrial;
      if (!trial || !who) {
        res.status(400).json({ error: 'no trial context' });
        return;
      }
      const rows = await listMemoryNotes(pool, memoryOwnerOf(who), MEMORY_RECALL_LIMIT);

      if (req.query.format === 'json') {
        res.json({ items: rows });
        return;
      }

      // Lite mode (no embeddings) shows a small note explaining that
      // what's recorded is real but the ranking is recency-only until
      // an embedding provider is wired up.
      const liteMode = !process.env.OPENAI_API_KEY;
      const devTrialQuery = devTrialQueryFor(req);
      res.render('lighthouse/recall', {
        title: `What ${trial.companion_name} remembers about you`,
        layout: false,
        companionName: trial.companion_name,
        items: rows,
        modeNote: liteMode ? 'Recency-ordered.' : 'Most-relevant first, then recent.',
        modeMessage: liteMode
          ? "Memory is in 'lite mode' right now: I'm noting what you share, but I'm not ranking notes by topic the way I will once an embedding provider is wired up. The notes below are real. The order is recency."
          : '',
        devTrialQuery,
      });
    } catch (err) {
      next(err);
    }
  });

  // ===== Make-it-public payment moment =====
  // The lamp surface is the keeper's view of their build. State machine:
  //   queued/building -> "Shaping the lamp"
  //   preview + review pending -> "Your draft is in review"
  //   preview + changes requested -> "A few notes from the review"
  //   preview + approved      -> "Your preview is ready" + Make-it-public CTA
  //   live                    -> "Your website is public" + public URL
  //   failed                  -> "Something went wrong with the shaping"
  // (the headings are the eyebrows in views/lighthouse/lamp.ejs, which also
  // has a notice for each subscription state on a live build)
  //
  // Auth resolves either path (trial owns build via trial_keepers.handoff_build_id,
  // or keeper owns it via builds.customer_id). Read-only routes; the
  // POST /lamp/:buildId/make-public is the only state mutation, gated by CSRF.

  router.get('/lamp/:buildId', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const ctx = await resolveLampOwnership(pool, req, { allowAdminRead: true });
      if (!ctx) {
        renderErrorPage(res, 404, { title: 'Not found', error: 'Build not found.' });
        return;
      }
      // Load subscription state when build is live so the lamp view can
      // show a cancel link or the past_due affordance. Only relevant for
      // keeper-owned builds (trial owners pre-payment never have a
      // keepers row); a missing row is fine, just means no extra surface.
      let subscriptionState: {
        status: string;
        cancelAtPeriodEnd: boolean;
        currentPeriodEnd: Date | null;
        slidingScaleAmountCents: number | null;
      } | null = null;
      if (ctx.ownerKind === 'keeper' && ctx.customerId && ctx.build.status === 'live') {
        const subRes = await pool.query<{
          subscription_status: string;
          stripe_subscription_id: string | null;
          sliding_scale_amount_cents: number | null;
        }>(
          `SELECT subscription_status, stripe_subscription_id, sliding_scale_amount_cents
           FROM keepers WHERE customer_id = $1`,
          [ctx.customerId]
        );
        if (subRes.rows.length > 0) {
          const k = subRes.rows[0];
          // Try the audit row for cancel_at_period_end + period end.
          let cancelAtPeriodEnd = false;
          let currentPeriodEnd: Date | null = null;
          if (k.stripe_subscription_id) {
            const audit = await pool.query<{
              cancel_at_period_end: boolean;
              current_period_end: Date | null;
            }>(
              `SELECT cancel_at_period_end, current_period_end
               FROM harbor_subscriptions
               WHERE stripe_subscription_id = $1`,
              [k.stripe_subscription_id]
            );
            if (audit.rows.length > 0) {
              cancelAtPeriodEnd = audit.rows[0].cancel_at_period_end || false;
              currentPeriodEnd = audit.rows[0].current_period_end;
            }
          }
          subscriptionState = {
            status: k.subscription_status,
            cancelAtPeriodEnd,
            currentPeriodEnd,
            slidingScaleAmountCents: k.sliding_scale_amount_cents,
          };
        }
      }
      const csrfToken = generateCsrfToken(req);
      res.render('lighthouse/lamp', {
        title: 'Your lamp',
        layout: false,
        build: ctx.build,
        ownerKind: ctx.ownerKind,
        canMakePublic:
          ctx.build.status === 'preview' && ctx.build.review_status === 'approved',
        showRevisionsNote:
          ctx.build.status === 'preview' && ctx.build.review_status === 'changes_requested',
        subscriptionState,
        // False for the browser that paid and has only its trial cookie: the
        // page then says how to reach the website's account from elsewhere.
        signedInToAccount: Boolean(req.session?.customerId),
        csrfToken,
      });
    } catch (err) {
      next(err);
    }
  });

  // POST /lighthouse/lamp/:buildId/cancel-subscription
  // Cancel-at-period-end the keeper's harbor subscription. Site stays up.
  // Keeper-only (no trial path; pre-payment trials have no subscription).
  router.post(
    '/lamp/:buildId/cancel-subscription',
    validateCsrfToken,
    async (req: Request, res: Response, next: NextFunction) => {
      try {
        const ctx = await resolveLampOwnership(pool, req);
        if (!ctx) {
          res.status(404).json({ error: 'build_not_found' });
          return;
        }
        if (ctx.ownerKind !== 'keeper' || !ctx.customerId) {
          res.status(403).json({ error: 'keeper_required' });
          return;
        }
        const subRes = await pool.query<{ stripe_subscription_id: string | null }>(
          'SELECT stripe_subscription_id FROM keepers WHERE customer_id = $1',
          [ctx.customerId]
        );
        if (subRes.rows.length === 0 || !subRes.rows[0].stripe_subscription_id) {
          res.status(409).json({ error: 'no_active_subscription' });
          return;
        }
        await cancelHarborSubscriptionAtPeriodEnd(subRes.rows[0].stripe_subscription_id);
        // Mirror immediately for fast read; webhook will reconfirm.
        await pool.query(
          `UPDATE harbor_subscriptions
             SET cancel_at_period_end = TRUE, updated_at = NOW()
           WHERE stripe_subscription_id = $1`,
          [subRes.rows[0].stripe_subscription_id]
        );
        res.redirect(303, `/lighthouse/lamp/${ctx.build.id}`);
      } catch (err) {
        next(err);
      }
    }
  );

  router.get('/lamp/:buildId/make-public', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const ctx = await resolveLampOwnership(pool, req, { allowAdminRead: true });
      if (!ctx) {
        renderErrorPage(res, 404, { title: 'Not found', error: 'Build not found.' });
        return;
      }
      // Block if SE flagged changes; force keeper through revision first.
      if (ctx.build.review_status === 'changes_requested') {
        res.status(409).render('lighthouse/changes-first', {
          title: 'A few notes first',
          layout: false,
          build: ctx.build,
          notes: ctx.build.review_notes || '',
        });
        return;
      }
      // Block if not yet at preview/approved; nothing to make public.
      if (ctx.build.status !== 'preview' || ctx.build.review_status !== 'approved') {
        res.redirect(`/lighthouse/lamp/${ctx.build.id}`);
        return;
      }
      const csrfToken = generateCsrfToken(req);
      // The address shown is the one the payment will be tied to. When the
      // account is already decided (see paymentAccount) it is that account's
      // and cannot be changed here; otherwise it is whatever the visitor
      // gave Haven, editable.
      const account = await paymentAccount(pool, ctx);
      const captured = await capturedTrialEmail(pool, ctx);
      if (account && ctx.ownerKind === 'trial' && !sameAddress(account.email, captured)) {
        // The build's account is not the one for the address this visitor
        // gave (the POST below says when that happens and refuses it too),
        // so that account's address is not shown to them either.
        renderErrorPage(res, 409, { title: 'Log in to continue', error: ACCOUNT_ELSEWHERE_MESSAGE });
        return;
      }
      const prefilledEmail = account ? account.email : captured;
      res.render('lighthouse/make-public', {
        title: 'Make this public',
        layout: false,
        build: ctx.build,
        csrfToken,
        prefilledEmail,
        emailFixed: account !== null,
        // A trial's account rests on an address somebody typed, so the
        // visitor is asked to confirm it before paying (the POST requires it).
        confirmEmail: ctx.ownerKind === 'trial',
        amountMinCents: HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS,
        amountMaxCents: HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS,
        amountDefaultCents: HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS,
      });
    } catch (err) {
      next(err);
    }
  });

  router.post(
    '/lamp/:buildId/make-public',
    makePublicIpRateLimit,
    makePublicRateLimit,
    validateCsrfToken,
    async (req: Request, res: Response, next: NextFunction) => {
      try {
        const ctx = await resolveLampOwnership(pool, req);
        if (!ctx) {
          res.status(404).json({ error: 'build_not_found' });
          return;
        }
        if (ctx.build.status !== 'preview' || ctx.build.review_status !== 'approved') {
          res.status(409).json({ error: 'build_not_ready_to_publish' });
          return;
        }
        // A keeper whose subscription already carries the right to publish
        // does not need a second one: their status page publishes this build
        // (POST /builds/:buildId/approve). Opening Checkout here would start a
        // second subscription billed alongside the first.
        if (ctx.ownerKind === 'keeper' && ctx.customerId) {
          const subscribed = await pool.query<{ customer_id: string }>(
            `SELECT customer_id FROM keepers
             WHERE customer_id = $1
               AND stripe_subscription_id IS NOT NULL
               AND subscription_status = ANY($2::text[])`,
            [ctx.customerId, PUBLISHING_SUBSCRIPTION_STATUSES]
          );
          if (subscribed.rows.length > 0) {
            res.redirect(303, `/builds/${ctx.build.id}`);
            return;
          }
        }
        // Validate amount: whole dollars, inside the pricing band. The client
        // chooses the price within that band (and self-attests below the
        // default); the server trusts nothing else from the form for the
        // charge: the build, the owner and the Stripe customer all come from
        // the session or trial cookie, never from the request body.
        const rawAmount = req.body?.monthly_amount_cents;
        const amount = typeof rawAmount === 'string' ? Number(rawAmount.trim()) : Number(rawAmount);
        if (
          !Number.isInteger(amount) ||
          amount % 100 !== 0 ||
          amount < HARBOR_SUBSCRIPTION_AMOUNT_MIN_CENTS ||
          amount > HARBOR_SUBSCRIPTION_AMOUNT_MAX_CENTS
        ) {
          res.status(400).json({ error: 'invalid_amount' });
          return;
        }
        const slidingScaleAttestation = req.body?.sliding_scale_attestation === 'on'
          || req.body?.sliding_scale_attestation === 'true'
          || req.body?.sliding_scale_attestation === true;
        if (amount < HARBOR_SUBSCRIPTION_DEFAULT_AMOUNT_CENTS && !slidingScaleAttestation) {
          res.status(400).json({ error: 'attestation_required_for_sliding_scale' });
          return;
        }
        // Which account does the payment land in, and which address does
        // Checkout carry? They must be the same address, because three things
        // follow from them and must not come apart: Stripe's receipt (sent to
        // the Checkout address, which Stripe shows and does not let the payer
        // edit), the "your website is public" mail, and the account that
        // receives the conversation (the webhook sends the mail to, and
        // promotes the trial into, the build's customer).
        //
        //   - The account is already decided (a keeper's own, or the customer
        //     a trial's build was attached to at dispatch): Checkout gets that
        //     account's address and nothing typed here can change it. A form
        //     that names a different address is refused rather than silently
        //     overridden, so what the visitor confirmed is what is used.
        //   - A trial's build with no customer yet: the webhook will create or
        //     find the account by the Checkout address, so an address that
        //     already has an account is refused unless the visitor is logged
        //     in as that account. Typing an address is not proof of owning it.
        //     (This does tell the owner of an approved preview whether an
        //     address is registered; that is the smaller harm.)
        //
        // A trial's payment is also refused when the build's customer already
        // has a subscription: promoting into it would overwrite that
        // subscription's record and hand the trial that account's
        // conversation. This is the early answer, given while the form is
        // open; the rule itself is applied again where the payment is
        // applied (the webhook's `claimKeeperSubscription`), because a
        // session opened before the account was subscribed completes after
        // this check.
        const rawEmail =
          typeof req.body?.email === 'string' ? req.body.email.trim().toLowerCase() : '';
        const account = await paymentAccount(pool, ctx);
        let email: string;
        if (account) {
          // A trial pays only into the account for the address it gave. The
          // build's account can be another one: an operator can dispatch a
          // trial's build for an address of the operator's choosing, and a
          // logged-in customer's preview goes to their own account. A
          // cookie is no claim on such an account, so paying into it takes a
          // login (the owner path above), and its address is not disclosed.
          if (ctx.ownerKind === 'trial' && !sameAddress(account.email, await capturedTrialEmail(pool, ctx))) {
            res.status(409).json({ error: 'account_login_required', message: ACCOUNT_ELSEWHERE_MESSAGE });
            return;
          }
          if (ctx.ownerKind === 'trial' && account.subscribed) {
            res.status(409).json({
              error: 'account_already_subscribed',
              message:
                'This account already has a subscription. Log in to it to manage your website.',
            });
            return;
          }
          email = account.email.trim().toLowerCase();
          if (!isValidEmailShape(email)) {
            res.status(400).json({ error: 'valid_email_required' });
            return;
          }
          if (rawEmail && rawEmail !== email) {
            res.status(409).json({
              error: 'email_mismatch',
              message:
                'This website belongs to the email address shown on this page, and that cannot be changed here. If that address is not yours, please write to support@craftandship.io before you pay.',
            });
            return;
          }
        } else {
          email = rawEmail || (await capturedTrialEmail(pool, ctx));
          if (!isValidEmailShape(email)) {
            res.status(400).json({ error: 'valid_email_required' });
            return;
          }
          if (ctx.ownerKind === 'trial') {
            const sessionCustomerId = req.session?.customerId || null;
            const emailOwner = await pool.query<{ id: string }>(
              'SELECT id FROM customers WHERE email = $1 LIMIT 1',
              [email]
            );
            if (emailOwner.rows.length > 0 && emailOwner.rows[0].id !== sessionCustomerId) {
              res.status(409).json({
                error: 'account_exists_login_required',
                message:
                  'An account with this email already exists. Log in to it first and come back, or use a different email.',
              });
              return;
            }
          }
        }
        // A trial's account rests on an address somebody typed and nobody has
        // shown they can read. Whoever reads that mailbox can enter the
        // account, so the visitor must say, on this page, that it is theirs.
        if (ctx.ownerKind === 'trial' && !isChecked(req.body?.email_confirmed)) {
          res.status(400).json({
            error: 'email_confirmation_required',
            message: 'Please confirm that the email address shown is yours before paying.',
          });
          return;
        }

        // The conversation that goes with this website. For a trial paying
        // with its cookie that is the trial itself. For an owner with an
        // account session it is the unreleased trial stamped with this build,
        // if there is one: someone who set a password and logged in before
        // paying has the same conversation as someone who did not, and it must
        // move into their account at payment all the same. (The webhook looks
        // the same trial up for a session that names none; naming it here
        // records in the session which conversation was paid for.)
        const trialId =
          ctx.ownerKind === 'trial' ? (ctx.trialId as string) : await unreleasedTrialForBuild(pool, ctx.build.id);

        // A keeper who has paid before keeps their Stripe customer. Anyone
        // else gets one from Checkout itself when they pay; nothing is
        // created at Stripe for a form that is merely submitted.
        const stripeCustomerId = await storedStripeCustomerId(pool, ctx);

        // Build the absolute origin Stripe will redirect back to. Prefer the
        // configured BASE_URL so the redirect target never depends on a
        // request header; fall back to the request origin for local use.
        const configuredBase = (process.env.BASE_URL || '').trim().replace(/\/+$/, '');
        const proto = (req.headers['x-forwarded-proto'] as string) || (req.protocol || 'http');
        const host = req.headers.host;
        const baseUrl = /^https?:\/\/[^\s]+$/i.test(configuredBase)
          ? configuredBase
          : `${proto}://${host}`;

        const url = await createHarborSubscriptionCheckoutSession({
          buildId: ctx.build.id,
          trialId,
          keeperCustomerId: ctx.ownerKind === 'keeper' ? (ctx.customerId as string) : null,
          stripeCustomerId,
          customerEmail: email,
          monthlyAmountCents: amount,
          slidingScaleAttestation,
          baseUrl,
        });
        res.redirect(303, url);
      } catch (err) {
        next(err);
      }
    }
  );

  router.get('/lamp/:buildId/published', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const ctx = await resolveLampOwnership(pool, req, { allowAdminRead: true });
      if (!ctx) {
        renderErrorPage(res, 404, { title: 'Not found', error: 'Build not found.' });
        return;
      }
      // The actual state flip happens in the webhook. This view just shows
      // a calm landing; if the keeper got here before the webhook fired,
      // they see "almost there" and a refresh affordance.
      res.render('lighthouse/published', {
        title: 'Your website is public',
        layout: false,
        build: ctx.build,
        webhookSettled: ctx.build.status === 'live',
      });
    } catch (err) {
      next(err);
    }
  });

  router.get('/lamp/:buildId/cancelled', async (req: Request, res: Response, next: NextFunction) => {
    try {
      const ctx = await resolveLampOwnership(pool, req, { allowAdminRead: true });
      if (!ctx) {
        renderErrorPage(res, 404, { title: 'Not found', error: 'Build not found.' });
        return;
      }
      res.render('lighthouse/cancelled', {
        title: 'Whenever you are ready',
        layout: false,
        build: ctx.build,
      });
    } catch (err) {
      next(err);
    }
  });

  return router;
}

/**
 * Resolve who owns a build and load it. Either:
 *   - keeper path: req.session.customerId === builds.customer_id
 *   - paid-cookie path: req.harborPaidKeeper names this build and its customer
 *     (the browser that paid for it; acts as the keeper)
 *   - trial path: the active trial's trial_keepers.handoff_build_id is this build
 *   - admin override (dev only): NODE_ENV is "development" or "test" AND
 *     the session is an admin session (isAdminSession). Adopts the build's
 *     existing keeper customer_id if one is set so all the keeper-path
 *     surfaces (subscription state, email prefill, Stripe customer)
 *     work the same way they would for that keeper. READ ONLY: it is
 *     honoured only when the caller passes `allowAdminRead: true`, which
 *     the GET handlers do and the POST handlers (make-public, cancel
 *     subscription) do not. Because the override adopts the keeper's
 *     customer_id, honouring it on a POST would let an admin session in a
 *     non-production environment cancel that keeper's subscription or open
 *     a Checkout session as them.
 *
 * Returns the build row + ownership kind, or null if the build is missing
 * or the requester does not own it.
 */
type LampOwnership = {
  build: {
    id: string;
    customer_id: string | null;
    app_name: string | null;
    status: string;
    review_status: string | null;
    review_notes: string | null;
    preview_url: string | null;
    live_url: string | null;
  };
  ownerKind: 'keeper' | 'trial' | 'admin';
  customerId: string | null;
  trialId: string | null;
};

function isAdminDevSession(req: Request): boolean {
  return isDevOrTestEnv() && isAdminSession(req);
}

/** The Harbor payment bypass is on: the flag is set AND this is development or test. */
function harborStripeBypassEnabled(): boolean {
  return process.env.CAS_HARBOR_STRIPE_BYPASS === 'true' && isDevOrTestEnv();
}

async function resolveLampOwnership(
  pool: Pool,
  req: Request,
  opts: { allowAdminRead?: boolean } = {}
): Promise<LampOwnership | null> {
  const buildId = req.params.buildId;
  if (!buildId || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(buildId)) return null;

  const buildRes = await pool.query<{
    id: string;
    customer_id: string | null;
    app_name: string | null;
    status: string;
    review_status: string | null;
    review_notes: string | null;
    preview_url: string | null;
    live_url: string | null;
  }>(
    `SELECT id, customer_id, app_name, status, review_status, review_notes,
            preview_url, live_url
     FROM builds
     WHERE id = $1`,
    [buildId]
  );
  if (buildRes.rows.length === 0) return null;
  const build = buildRes.rows[0];

  // Path 1: authed keeper owns the build.
  const sessionCustomerId = req.session?.customerId;
  if (sessionCustomerId && build.customer_id === sessionCustomerId) {
    return { build, ownerKind: 'keeper', customerId: sessionCustomerId, trialId: null };
  }

  // Path 2: the browser that paid for this build. Its trial was released by
  // the payment; the cookie middleware vouches for the link (trial ->
  // handoff build -> that build's keeper) and this checks it is this build
  // and still that customer's. The payer then acts as the keeper: they see
  // the subscription and can cancel it.
  const paid = req.harborPaidKeeper;
  if (paid && paid.buildId === build.id && build.customer_id === paid.customerId) {
    return { build, ownerKind: 'keeper', customerId: paid.customerId, trialId: null };
  }

  // Path 3: active trial cookie owns the build via handoff_build_id.
  const trial = req.harborTrial;
  if (trial) {
    const trialOwnerRes = await pool.query<{ trial_id: string }>(
      `SELECT trial_id FROM trial_keepers
       WHERE trial_id = $1 AND handoff_build_id = $2 AND released_at IS NULL`,
      [trial.trial_id, buildId]
    );
    if (trialOwnerRes.rows.length > 0) {
      return { build, ownerKind: 'trial', customerId: null, trialId: trial.trial_id };
    }
  }

  // Path 4 (dev only): admin can read any keeper's lamp without
  // bypassing as them. Logged so misuse during local dev is auditable.
  if (opts.allowAdminRead === true && isAdminDevSession(req)) {
    console.log(
      '[lighthouse-lamp-admin] admin read of build=%s (customer=%s)',
      buildId,
      build.customer_id || 'none'
    );
    return {
      build,
      // Adopt the build's keeper id when set so subscription_state,
      // email prefill, and the cancel form all show the keeper's data.
      ownerKind: build.customer_id ? 'keeper' : 'admin',
      customerId: build.customer_id,
      trialId: null,
    };
  }

  return null;
}

/**
 * The account a payment for this build lands in, when that is already
 * decided: the keeper's own, or the customer a trial's build was attached to
 * when it was dispatched (the webhook promotes the trial into
 * `builds.customer_id`). Null when it is not decided yet, which is a trial's
 * build that has no customer: the webhook then finds or creates the account
 * from the Checkout address.
 *
 * `subscribed` says whether that account already has a keeper row pointing at
 * a Stripe subscription.
 */
async function paymentAccount(
  pool: Pool,
  ctx: LampOwnership
): Promise<{ customerId: string; email: string; subscribed: boolean } | null> {
  const customerId = ctx.ownerKind === 'keeper' ? ctx.customerId : ctx.build.customer_id;
  if (!customerId) return null;
  const r = await pool.query<{ email: string | null; subscribed: boolean | null }>(
    `SELECT c.email,
            EXISTS (
              SELECT 1 FROM keepers k
              WHERE k.customer_id = c.id AND k.stripe_subscription_id IS NOT NULL
            ) AS subscribed
     FROM customers c
     WHERE c.id = $1`,
    [customerId]
  );
  if (r.rows.length === 0) return null;
  return { customerId, email: r.rows[0].email || '', subscribed: r.rows[0].subscribed === true };
}

/** Two addresses are the same account address: compared trimmed and lower-cased, and an empty one equals nothing. */
function sameAddress(a: string | null | undefined, b: string | null | undefined): boolean {
  const left = (a || '').trim().toLowerCase();
  return left !== '' && left === (b || '').trim().toLowerCase();
}

/** What a trial cookie is told when its build belongs to an account it gave no address for. */
const ACCOUNT_ELSEWHERE_MESSAGE =
  'This website belongs to an account. Please log in to that account to make it public, or write to support@craftandship.io.';

/** The address a trial gave Haven: what its payment may be tied to, and the prefill when no account is decided yet. */
async function capturedTrialEmail(pool: Pool, ctx: LampOwnership): Promise<string> {
  if (ctx.ownerKind !== 'trial' || !ctx.trialId) return '';
  const r = await pool.query<{ email_captured: string | null }>(
    'SELECT email_captured FROM trial_keepers WHERE trial_id = $1',
    [ctx.trialId]
  );
  return r.rows[0]?.email_captured || '';
}

/** The address of an account, lower-cased, or '' when the account does not exist. */
async function customerEmailById(pool: Pool, customerId: string): Promise<string> {
  const r = await pool.query<{ email: string | null }>('SELECT email FROM customers WHERE id = $1', [customerId]);
  return (r.rows[0]?.email || '').trim().toLowerCase();
}

/** A checkbox as a form or a JSON body sends it. */
function isChecked(value: unknown): boolean {
  return value === 'on' || value === 'true' || value === true;
}

/**
 * The Stripe customer already on file for the make-public Checkout session,
 * or null. Only a keeper has one (keepers.stripe_customer_id, written by the
 * webhook when they first paid). A trial has none, and none is created here:
 * Checkout creates the customer when the session is paid. A Stripe customer
 * is never looked up by the typed email, because Checkout offers a returning
 * customer's saved card.
 */
async function storedStripeCustomerId(pool: Pool, ctx: LampOwnership): Promise<string | null> {
  if (ctx.ownerKind !== 'keeper' || !ctx.customerId) return null;
  const r = await pool.query<{ stripe_customer_id: string | null }>(
    `SELECT stripe_customer_id FROM keepers WHERE customer_id = $1`,
    [ctx.customerId]
  );
  const stored = r.rows[0]?.stripe_customer_id || '';
  // Ids minted by the development payment bypass ("cus_bypass_...") do not
  // exist at Stripe.
  return stored.startsWith('cus_') && !stored.startsWith('cus_bypass_') ? stored : null;
}

// ---- Email gate helpers ----

const EMAIL_GATE_DEFAULT_THRESHOLD = 22;

function getEmailGateThreshold(): number {
  const raw = process.env.CAS_HARBOR_EMAIL_GATE_TURN_COUNT;
  if (!raw) return EMAIL_GATE_DEFAULT_THRESHOLD;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 1) return EMAIL_GATE_DEFAULT_THRESHOLD;
  return n;
}

/**
 * What a conversation below the readiness floor is told (the floor itself,
 * and which turns count toward it, are in services/spec_extraction.ts).
 */
const NOT_READY_MESSAGE =
  "Let's talk a little more first, so I have enough about your work to build something that really sounds like you.";

/** Lightweight RFC-ish email shape check. Real validation is on send. */
export function isValidEmailShape(s: unknown): s is string {
  if (typeof s !== 'string') return false;
  const trimmed = s.trim();
  if (trimmed.length < 5 || trimmed.length > 254) return false;
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed);
}

interface EmailGateOutcome {
  status: 'pass' | 'gated' | 'invalid_email';
  turnCount: number;
  threshold: number;
}

/**
 * Decide whether the trial's next turn is allowed.
 *
 * Pass path: trial under threshold, OR email already on file, OR email
 * provided + valid in this request.
 *
 * Gate path: trial at/above threshold AND no email captured AND no
 * (or empty) email provided in this request.
 *
 * Invalid: email field provided but malformed shape.
 */
async function checkEmailGate(
  pool: Pool,
  trialId: string,
  emailFromBody: unknown
): Promise<EmailGateOutcome> {
  const threshold = getEmailGateThreshold();
  const countRes = await pool.query<{ c: string }>(
    `SELECT COUNT(*)::text AS c FROM companion_turns WHERE trial_id = $1`,
    [trialId]
  );
  const turnCount = parseInt(countRes.rows[0]?.c ?? '0', 10);
  const trialRes = await pool.query<{ email_captured: string | null }>(
    `SELECT email_captured FROM trial_keepers WHERE trial_id = $1`,
    [trialId]
  );
  const captured = trialRes.rows[0]?.email_captured ?? null;

  // Below threshold: always pass.
  if (turnCount < threshold) return { status: 'pass', turnCount, threshold };
  // At/above threshold + email already on file: pass.
  if (captured) return { status: 'pass', turnCount, threshold };

  // At/above threshold + no email yet. Caller may have included one.
  const incoming =
    typeof emailFromBody === 'string' && emailFromBody.trim().length > 0
      ? emailFromBody.trim().toLowerCase()
      : null;
  if (!incoming) return { status: 'gated', turnCount, threshold };
  if (!isValidEmailShape(incoming)) {
    return { status: 'invalid_email', turnCount, threshold };
  }
  // Persist + pass.
  await pool.query(
    `UPDATE trial_keepers SET email_captured = $1 WHERE trial_id = $2 AND email_captured IS NULL`,
    [incoming, trialId]
  );
  return { status: 'pass', turnCount, threshold };
}

// ---- Stripe bypass helpers ----

/**
 * Create a customers row for an email that has none. Used by the
 * `/lighthouse/bypass` route (development and test only) to mint a keeper
 * without touching Stripe; the route has already refused any email that
 * belongs to an existing account.
 *
 * Sets `email_verified=TRUE` (this is a test fixture account; nothing has
 * shown the address is reachable) and seeds a random password, so the
 * account can only be entered later through /forgot-password.
 */
async function createBypassCustomer(pool: Pool, email: string): Promise<string> {
  // Synthetic password the keeper can reset via /forgot-password if they
  // ever want to log in to the keeper dashboard.
  const hashed = await hashUnusablePassword();
  const created = await pool.query<{ id: string }>(
    `INSERT INTO customers (email, password, name, email_verified, terms_accepted_at)
     VALUES ($1, $2, $3, TRUE, NOW())
     RETURNING id`,
    [email, hashed, email.split('@')[0]]
  );
  return created.rows[0].id;
}

/**
 * The owner's most recent 200 turns, oldest first. (Ordering ASC with a LIMIT
 * would return the FIRST 200 turns, so a long conversation would stop showing
 * anything new after reload.) For a keeper this includes the turns their
 * trial held before payment, which promotion re-keyed, and the blessing turn
 * the make-public webhook appended.
 */
async function loadTurnHistory(pool: Pool, owner: MemoryOwner): Promise<TurnRowSlim[]> {
  const { column, value } = memoryOwnerColumn(owner);
  const { rows } = await pool.query<TurnRowSlim>(
    `SELECT turn_id, keeper_text, companion_text, rail_flags, created_at
     FROM (
       SELECT turn_id, keeper_text, companion_text, rail_flags, created_at
       FROM companion_turns
       WHERE ${column} = $1
       ORDER BY created_at DESC
       LIMIT 200
     ) recent
     ORDER BY created_at ASC`,
    [value]
  );
  return rows;
}
