import { respondAssistantNotConfigured } from '../utils/assistant';
import { Router, Request, Response } from 'express';
import { boundedRateLimit, customerOrAddressKey } from '../middleware/companion-rate-limit';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { requireAuth, generateCsrfToken, validateCsrfToken, verifyHmacToken, isAdminSession } from '../middleware/auth';
import { Build, BuildStatus, BuildApiResponse, BuildTier, IntakeMessage, AppSpec, DEFAULT_BUILD_ESTIMATE_MINUTES } from '../types';
import { sendAppLiveEmail } from '../services/email';
import { schedulePostBuildSequence, cancelWelcomeSequence } from '../services/email-scheduler';
import { processRevisionMessage, generateRevisionGreeting } from '../services/claude';
import { REVISION_HARD_TURN_CAP } from '../services/llm-budget';
import {
  MAX_ACTIVE_BUILDS_PER_CUSTOMER,
  MAX_BUILD_STARTS_PER_DAY,
  MAX_MANUAL_RETRIES_PER_BUILD,
  buildStartRefusal,
  readBuildStartBudget,
  recordBuildStart,
  withCustomerBuildLock,
} from '../services/build-start-limits';
import { PUBLISHING_SUBSCRIPTION_STATUS_LIST_SQL } from '../services/keeper_promotion';
import { customerLlmDailyLimit } from '../middleware/llm-rate-limit';
import { renderErrorPage } from '../utils/error-page';

// `TIER_BUILD_ESTIMATES`, `getEffectiveRevisionLimits`, and `validateSpecForTier`
// were removed 2026-05-01 with the tier system kill. Harbor promises "unlimited
// revisions and rebuilds, kept across the years"; the practical cap below
// prevents pathological abuse but is intentionally generous.
const UNLIMITED_REVISION_LIMIT = 999;

const router = forwardAsyncErrors(Router());

// ---------------------------------------------------------------------------
// Customer-initiated build starts: retry, rebuild, revision
// ---------------------------------------------------------------------------
//
// Each of these puts a build on the worker queue, under the per-customer
// limits in services/build-start-limits.ts (how many builds at once, how many
// starts a day, how many manual retries of one build). That module holds the
// numbers, the advisory lock, the counts and the ledger write, and the Harbor
// dispatch uses the same ones, so a customer's first build and their later
// rebuilds draw on one budget. The names below are re-exported for the code
// and tests that have always imported them from this file.
export { MAX_ACTIVE_BUILDS_PER_CUSTOMER, MAX_BUILD_STARTS_PER_DAY, MAX_MANUAL_RETRIES_PER_BUILD, buildStartRefusal };

function renderBuildStartRefused(res: Response, message: string): void {
  renderErrorPage(res, 429, { title: 'Please wait', error: message });
}

// ---------------------------------------------------------------------------
// What a customer is shown of worker output
// ---------------------------------------------------------------------------
//
// builds.error_message and build_logs.message are written by the build worker
// and shown to the build's owner on the status page. They are free text from
// subprocesses and exceptions, so before they leave for a non-admin, anything
// shaped like a connection string or a URL with embedded credentials is
// removed. This is a backstop, not a guarantee: the worker is responsible for
// not logging secrets in the first place.
//
// Both patterns must stay linear in the length of the text: the text is
// worker output of any length, it is redacted on every poll of the status
// page, and this process has one event loop. The credentials pattern
// therefore starts at "://" and reads forward only (userinfo cannot contain
// "/", "?" or "#", so one attempt never reads past the end of its authority
// and the attempts of different starts do not overlap). An earlier form began
// at the scheme, `\b[a-z][a-z0-9+.-]*://`, and was quadratic: in dotted text
// such as a stack trace every word after a "." is a place a scheme could
// start, and each start read to the end of the run. It needs no scheme to
// decide: "://user:secret@" is removed whatever precedes it.
//
// What counts as credentials is everything between "://" and the last "@" of
// the authority, not only "user:password": a token is often the whole
// userinfo ("https://<token>@host/repo.git"), a password may hold "@" or ")",
// and a user name that is not a secret is no loss to a customer who is
// reading an error message. A database URL goes to the end of its word, so a
// closing parenthesis or a ")" in the password cannot end it early and leave
// the rest of the password in view; the price is that a ")" which closed a
// parenthesis around the URL goes with it.
const URL_WITH_CREDENTIALS_RE = /(:\/\/)[^\s\/?#]+@/g;
const DATABASE_URL_RE = /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqps?):\/\/[^\s'"<>]+/gi;

export function redactForCustomer(text: string): string {
  return text
    .replace(DATABASE_URL_RE, '[connection string removed]')
    .replace(URL_WITH_CREDENTIALS_RE, '$1[credentials removed]@');
}

const STATUS_MESSAGES: Record<BuildStatus, string> = {
  intake: 'Describing your project',
  payment_pending: 'Awaiting payment',
  queued: 'In queue',
  building: 'Generating code',
  testing: 'Running tests',
  deploying: 'Deploying',
  preview: 'Ready for review',
  live: 'Live',
  failed: 'Build failed',
  abandoned: 'Conversation paused',
};

const STATUS_PAGE_COLUMNS = [
  'id', 'customer_id', 'tier', 'status', 'app_name', 'spec', 'live_url', 'preview_url',
  'error_message', 'created_at', 'updated_at', 'started_at', 'completed_at',
  'qa_status', 'qa_results', 'screenshots', 'review_status', 'reviewed_at', 'review_notes',
  'gallery_opted_in', 'gallery_featured', 'gallery_description', 'gallery_category',
  'parent_build_id', 'is_revision', 'retry_count',
].join(', ');

router.get('/dashboard', requireAuth, async (req: Request, res: Response) => {
  try {
    const result = await pool.query(
      `SELECT b.id, b.tier, b.status, b.app_name, b.live_url, b.error_message, b.created_at, b.updated_at, b.gallery_opted_in,
              m.status AS health_status, m.last_checked AS health_last_checked,
              m.last_latency_ms AS health_latency_ms, m.consecutive_failures AS health_consecutive_failures
       FROM builds b
       LEFT JOIN monitor_apps m ON m.railway_project_id = b.railway_project_id
       WHERE b.customer_id = $1
       ORDER BY b.created_at DESC`,
      [req.session.customerId]
    );

    res.render('dashboard', {
      title: 'My Websites',
      builds: result.rows as Build[],
      customerName: req.session.customerName,
      statusMessages: STATUS_MESSAGES,
    });
  } catch (err) {
    console.error('Error loading dashboard:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load dashboard.' });
  }
});

router.get('/builds/:buildId', requireAuth, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    // Admins can view any build; regular users can only see their own.
    // Columns are listed on purpose: SELECT * would also pull build_artifact
    // (the multi-megabyte source tarball) and railway_database_url (the
    // customer database's address) into every status-page render. status.ejs
    // needs neither.
    const result = isAdmin
      ? await pool.query(`SELECT ${STATUS_PAGE_COLUMNS} FROM builds WHERE id = $1`, [buildId])
      : await pool.query(`SELECT ${STATUS_PAGE_COLUMNS} FROM builds WHERE id = $1 AND customer_id = $2`, [buildId, req.session.customerId]);

    if (result.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = result.rows[0] as Build;
    if (!isAdmin && typeof build.error_message === 'string') {
      build.error_message = redactForCustomer(build.error_message);
    }

    const csrfToken = generateCsrfToken(req);

    // Resolve admin email: spec.admin_email (set by worker) > session email
    const adminEmail = (build.spec as Record<string, unknown>)?.admin_email || req.session.customerEmail || '';

    // Compute revision info for preview builds
    let revisionInfo: { used: number; limit: number; remaining: number } | null = null;
    if (build.status === 'preview') {
      const { count } = await countRevisionsInLineage(buildId);
      const limit = UNLIMITED_REVISION_LIMIT;
      revisionInfo = { used: count, limit, remaining: limit - count };
    }

    // The page links to the source download only when the route would serve
    // it: always for the admin, and for the owner under the same rule the
    // download route applies (asked of the database only for a preview; a
    // live build always qualifies and no other status ever does).
    let canDownloadSource = isAdmin ? ['preview', 'live'].includes(build.status) : build.status === 'live';
    if (!isAdmin && build.status === 'preview') {
      const allowed = await pool.query<{ allowed: boolean }>(OWNER_SOURCE_DOWNLOAD_ALLOWED_SQL, [
        buildId,
        req.session.customerId,
      ]);
      canDownloadSource = allowed.rows[0]?.allowed === true;
    }

    res.render('status', {
      title: build.app_name || 'Build Status',
      build,
      statusMessages: STATUS_MESSAGES,
      csrfToken,
      adminEmail,
      revisionInfo,
      canDownloadSource,
      estimateMinutes: DEFAULT_BUILD_ESTIMATE_MINUTES,
    });
  } catch (err) {
    console.error('Error loading build status:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load build status.' });
  }
});

router.get('/api/builds/:buildId', requireAuth, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    const result = isAdmin
      ? await pool.query(
          `SELECT b.status, b.live_url, b.preview_url, b.error_message, b.queue_job_id, b.tier,
                  b.spec->>'admin_email' AS admin_email,
                  b.review_status, b.reviewed_at, b.review_notes,
                  b.retry_count, b.total_api_cost_usd,
                  (SELECT COUNT(*) FROM builds b2
                   WHERE b2.status = 'queued'
                   AND b2.created_at < b.created_at) as queue_position,
                  m.status AS health_status, m.last_checked AS health_last_checked,
                  m.last_latency_ms AS health_latency_ms, m.consecutive_failures AS health_consecutive_failures
           FROM builds b
           LEFT JOIN monitor_apps m ON m.railway_project_id = b.railway_project_id
           WHERE b.id = $1`,
          [buildId]
        )
      : // The customer query does not select total_api_cost_usd at all: what
        // a build cost the platform is internal, and a column that is never
        // read cannot leak through a later change to the response mapping.
        await pool.query(
          `SELECT b.status, b.live_url, b.preview_url, b.error_message, b.tier,
                  b.spec->>'admin_email' AS admin_email,
                  b.review_status, b.reviewed_at, b.review_notes,
                  b.retry_count,
                  (SELECT COUNT(*) FROM builds b2
                   WHERE b2.status = 'queued'
                   AND b2.created_at < b.created_at) as queue_position,
                  m.status AS health_status, m.last_checked AS health_last_checked,
                  m.last_latency_ms AS health_latency_ms, m.consecutive_failures AS health_consecutive_failures
           FROM builds b
           LEFT JOIN monitor_apps m ON m.railway_project_id = b.railway_project_id
           WHERE b.id = $1 AND b.customer_id = $2`,
          [buildId, req.session.customerId]
        );

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const row = result.rows[0] as {
      status: BuildStatus;
      live_url: string | null;
      preview_url: string | null;
      error_message: string | null;
      admin_email: string | null;
      queue_position: string;
      health_status: string | null;
      health_last_checked: string | null;
      health_latency_ms: number | null;
      health_consecutive_failures: number | null;
      review_status: 'pending' | 'approved' | 'changes_requested' | null;
      reviewed_at: Date | string | null;
      review_notes: string | null;
      retry_count: number | string | null;
      total_api_cost_usd?: string | number | null;
    };

    const queuePosition =
      row.status === 'queued' ? parseInt(row.queue_position, 10) + 1 : null;

    const estimateMinutes = DEFAULT_BUILD_ESTIMATE_MINUTES;

    const response: BuildApiResponse = {
      status: row.status,
      live_url: row.live_url,
      preview_url: row.preview_url,
      error:
        row.error_message == null || isAdmin ? row.error_message : redactForCustomer(row.error_message),
      admin_email: row.admin_email || null,
      queue_position: queuePosition,
      progress_message: STATUS_MESSAGES[row.status] || row.status,
      health: row.health_status ? {
        status: row.health_status,
        last_checked: row.health_last_checked,
        latency_ms: row.health_latency_ms,
        consecutive_failures: row.health_consecutive_failures ?? 0,
      } : null,
      estimate_minutes: estimateMinutes,
      review_status: row.review_status ?? null,
      reviewed_at: row.reviewed_at ? new Date(row.reviewed_at).toISOString() : null,
      review_notes: row.review_notes ?? null,
      retry_count: row.retry_count == null ? 0 : Number(row.retry_count),
    };
    if (isAdmin) {
      response.total_api_cost_usd =
        row.total_api_cost_usd == null ? null : String(row.total_api_cost_usd);
    }

    // Prevent browser/proxy caching so status polling always gets fresh data
    res.setHeader('Cache-Control', 'no-store');
    res.json(response);
  } catch (err) {
    console.error('Error fetching build API:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch build status.' });
  }
});

/**
 * SET assignments that wipe a build's review verdict. A verdict (pending,
 * approved, changes requested, and who gave it when with what notes) is about
 * the deployment that was reviewed. Every statement in the web app that sends
 * an existing build row back to `queued` includes this fragment, so the next
 * deployment of that row can never arrive carrying the last one's
 * `review_status = 'approved'`. (The worker also resets these when a build
 * lands at preview; this is the same rule at the other end of the cycle.)
 */
export const REVIEW_RESET_ASSIGNMENTS = `review_status = NULL,
             reviewed_at = NULL,
             reviewed_by = NULL,
             review_notes = NULL,
             review_report = NULL,
             ai_review_iterations = 0,
             ai_reviewed_at = NULL`;

// Retry a failed build: reset to queued so the worker picks it up again.
// Customers are bounded by the build-start budget above. Every manual retry
// also leaves a row in build_logs (stage 'manual_retry' for the owner,
// 'admin_retry' for the admin): that is the audit trail the owner and the
// admin see in the build log, not what the budget counts.
const RETRY_SQL_SET = `SET status = 'queued',
                 error_message = NULL,
                 started_at = NULL,
                 completed_at = NULL,
                 retry_count = 0,
                 ${REVIEW_RESET_ASSIGNMENTS}`;

router.post('/builds/:buildId/retry', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    if (isAdmin) {
      // Admins can retry any build.
      const result = await pool.query(
        `UPDATE builds ${RETRY_SQL_SET} WHERE id = $1 AND status = 'failed' RETURNING id`,
        [buildId]
      );
      if (result.rows.length === 0) {
        res.status(404).render('404', { title: 'Not Found' });
        return;
      }
      // Audit row. The build is already requeued, so a failure to write the
      // row is logged rather than reported as a failed retry.
      try {
        await pool.query(
          `INSERT INTO build_logs (build_id, stage, message) VALUES ($1, 'admin_retry', $2)`,
          [buildId, 'Retry requested by the admin.']
        );
      } catch (auditErr) {
        console.error('Admin retry of build %s was not recorded in build_logs:', buildId, (auditErr as Error).message);
      }
      res.redirect(`/builds/${buildId}`);
      return;
    }

    const customerId = req.session.customerId as string;
    const outcome = await withCustomerBuildLock(pool, customerId, async (client) => {
      const owned = await client.query(
        `SELECT id FROM builds WHERE id = $1 AND customer_id = $2 AND status = 'failed'`,
        [buildId, customerId]
      );
      if (owned.rows.length === 0) return { kind: 'not_found' as const };

      const budget = await readBuildStartBudget(client, customerId, buildId);
      const refusal = buildStartRefusal(budget, 'retry');
      if (refusal) return { kind: 'refused' as const, refusal };

      const updated = await client.query(
        `UPDATE builds ${RETRY_SQL_SET} WHERE id = $1 AND customer_id = $2 AND status = 'failed' RETURNING id`,
        [buildId, customerId]
      );
      if (updated.rows.length === 0) return { kind: 'not_found' as const };
      await recordBuildStart(client, customerId, buildId, 'retry');
      await client.query(
        `INSERT INTO build_logs (build_id, stage, message) VALUES ($1, 'manual_retry', $2)`,
        [buildId, `Retry requested (${budget.retriesOfThisBuild + 1} of ${MAX_MANUAL_RETRIES_PER_BUILD}).`]
      );
      return { kind: 'ok' as const };
    });

    if (outcome.kind === 'not_found') {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }
    if (outcome.kind === 'refused') {
      renderBuildStartRefused(res, outcome.refusal);
      return;
    }
    res.redirect(`/builds/${buildId}`);
  } catch (err) {
    console.error('Error retrying build:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to retry build.' });
  }
});

/**
 * The owner of build `b` holds a Harbor subscription that carries the right
 * to publish (PUBLISHING_SUBSCRIPTION_STATUSES in services/keeper_promotion.ts:
 * `active`, `trialing`, `past_due`). The DEFAULT of
 * keepers.subscription_status is 'active', so a keeper row counts only when
 * it also names a Stripe subscription.
 */
const OWNER_SUBSCRIBED_SQL = `EXISTS (
               SELECT 1 FROM keepers k
                WHERE k.customer_id = b.customer_id
                  AND k.stripe_subscription_id IS NOT NULL
                  AND k.subscription_status IN (${PUBLISHING_SUBSCRIPTION_STATUS_LIST_SQL})
             )`;

/**
 * Build `b` is one its owner may take live themselves:
 *   - it is at `preview`;
 *   - a reviewer approved it (`review_status = 'approved'`);
 *   - if it is a Harbor build, its owner has a subscription. A Harbor website
 *     is built free and goes public through the "make it public" subscription
 *     (/lighthouse/lamp/:id/make-public); the owner's own routes must not be
 *     a way around that. A subscriber's later revisions and rebuilds pass
 *     without paying again.
 * Whether a build is a Harbor build is the row's own `is_harbor` column, set
 * when the build is inserted (see "Harbor origin" in db/schema.sql). It is
 * not derived from parent_build_id or from the handoff_build_id stamps on
 * purpose: the admin and ops delete routes remove builds, detach their
 * children and clear those stamps, the worker's cleanup removes old failed
 * builds, and a payment rule must not read rows that something else deletes.
 * A build that is not a Harbor build (operator-queued, or from before
 * Harbor) needs no subscription: nothing else in this application charges
 * for it.
 */
const OWNER_MAY_PUBLISH_SQL = `b.status = 'preview' AND b.review_status = 'approved'
              AND (NOT b.is_harbor OR ${OWNER_SUBSCRIBED_SQL})`;

/**
 * The customer's own promotion of a preview build to live. One statement;
 * every condition is a predicate of the UPDATE, so none can be raced past:
 * the build is the caller's ($2), and OWNER_MAY_PUBLISH_SQL holds.
 */
export const CUSTOMER_APPROVE_SQL = `UPDATE builds b
              SET status = 'live',
                  live_url = preview_url,
                  completed_at = NOW(),
                  preview_approved_at = NOW()
            WHERE b.id = $1 AND b.customer_id = $2
              AND ${OWNER_MAY_PUBLISH_SQL}
           RETURNING b.id, b.app_name, b.preview_url, b.customer_id`;

/**
 * The owner's source download: the stored artifact of a build that has been
 * delivered (`live`) or that the owner could take live right now
 * (OWNER_MAY_PUBLISH_SQL). The terms assign the source to the customer "upon
 * successful delivery", so it is not handed out for a preview that is still
 * waiting for review, has changes requested, or is a Harbor website nobody
 * has subscribed for. The artifact column is selected only when the rule
 * holds, so a refused request never loads the tarball.
 */
const OWNER_MAY_DOWNLOAD_SOURCE_SQL = `COALESCE(b.status = 'live' OR (${OWNER_MAY_PUBLISH_SQL}), FALSE)`;

export const OWNER_SOURCE_DOWNLOAD_SQL = `SELECT b.app_name,
                  src.allowed,
                  (b.build_artifact IS NOT NULL) AS has_artifact,
                  CASE WHEN src.allowed THEN b.build_artifact END AS build_artifact
             FROM builds b
            CROSS JOIN LATERAL (SELECT ${OWNER_MAY_DOWNLOAD_SOURCE_SQL} AS allowed) src
            WHERE b.id = $1 AND b.customer_id = $2`;

/** Whether the owner's source download would be served; the status page shows the link only then. */
const OWNER_SOURCE_DOWNLOAD_ALLOWED_SQL = `SELECT ${OWNER_MAY_DOWNLOAD_SOURCE_SQL} AS allowed
             FROM builds b
            WHERE b.id = $1 AND b.customer_id = $2`;

/**
 * The admin's promotion of any preview build. Deliberately without the review
 * and subscription predicates: the admin is the reviewer and the operator.
 */
export const ADMIN_APPROVE_SQL = `UPDATE builds
             SET status = 'live',
                 live_url = preview_url,
                 completed_at = NOW(),
                 preview_approved_at = NOW()
           WHERE id = $1 AND status = 'preview'
           RETURNING id, app_name, preview_url, customer_id`;

/**
 * Promotes a preview build to live.
 *
 * Preconditions (enforced atomically in the UPDATE WHERE clause):
 * 1. requireAuth passes
 * 2. validateCsrfToken passes
 * 3. Build exists, is owned by caller (or caller is admin)
 * 4. Build status is 'preview'
 * 5. For non-admin callers: review_status = 'approved'
 * 6. For non-admin callers: a Harbor build's owner has a subscription
 *    (see OWNER_MAY_PUBLISH_SQL)
 *
 * On zero-row UPDATE, a follow-up read-only SELECT disambiguates the
 * response (404 missing / 404 not-owned / 409 not-preview / 403-waiting /
 * 403-changes-requested / redirect to the make-public page).
 *
 * On success: fires sendAppLiveEmail, cancels welcome sequence,
 * schedules post-build sequence, redirects to /builds/:buildId.
 *
 * Invariant: the review_status check lives in the UPDATE WHERE clause,
 * NOT in a separate SELECT-then-check, to prevent TOCTOU races where an
 * admin "Request Changes" action between the read and the write could be
 * silently clobbered. The disambiguation SELECT runs only AFTER the
 * UPDATE returns 0 rows, and it is read-only; it can only affect the
 * response the customer sees, never the state of the build.
 */
router.post('/builds/:buildId/approve', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    const result = isAdmin
      ? await pool.query(ADMIN_APPROVE_SQL, [buildId])
      : await pool.query(CUSTOMER_APPROVE_SQL, [buildId, req.session.customerId]);

    if (result.rows.length === 0) {
      // Disambiguation SELECT: read-only. The atomic UPDATE above already
      // rejected the request; this query only shapes the customer-facing
      // error message. A concurrent write between UPDATE and SELECT can
      // only change which error message is shown, never the build state.
      const diag = await pool.query(
        `SELECT id, customer_id, status, review_status
           FROM builds
          WHERE id = $1`,
        [buildId]
      );

      if (diag.rows.length === 0) {
        // Build does not exist.
        res.status(404).render('404', { title: 'Not Found' });
        return;
      }

      const row = diag.rows[0] as {
        id: string;
        customer_id: string;
        status: BuildStatus;
        review_status: 'pending' | 'approved' | 'changes_requested' | null;
      };

      // Not owned by caller (and caller is not admin); do NOT leak
      // existence. Return 404 to match the original not-found behavior.
      if (!isAdmin && row.customer_id !== req.session.customerId) {
        res.status(404).render('404', { title: 'Not Found' });
        return;
      }

      // Build is not in preview status; 409 conflict.
      if (row.status !== 'preview') {
        renderErrorPage(res, 409, {
          title: 'Not Ready',
          error: 'This build is not in preview status and cannot be approved.',
        });
        return;
      }

      // At this point the build exists, is owned (or admin), and is in
      // preview, so what stopped the UPDATE is the review gate or, for a
      // reviewed Harbor build, the subscription gate. (The admin UPDATE has
      // neither predicate, so admin should never reach these branches.)
      if (row.review_status === 'changes_requested') {
        renderErrorPage(res, 403, {
          title: 'Changes Requested',
          error: 'The review of this build left notes. Please open the status page to see them and request a revision.',
        });
        return;
      }

      if (row.review_status === 'approved') {
        // Reviewed and approved, and still not promoted: a Harbor website
        // whose owner has no subscription. Send them to where it starts.
        if (process.env.CAS_HARBOR_ENABLED === 'true') {
          res.redirect(303, `/lighthouse/lamp/${encodeURIComponent(buildId)}/make-public`);
          return;
        }
        renderErrorPage(res, 409, {
          title: 'Subscription Needed',
          error: 'This website goes public when its subscription starts. Please write to support@craftandship.io and we will help you finish.',
        });
        return;
      }

      // review_status is null, 'pending' or an automated-review state: still
      // waiting for review.
      renderErrorPage(res, 403, {
        title: 'Awaiting Review',
        error: "This build is waiting for its review. We'll email you as soon as the review is complete.",
      });
      return;
    }

    const build = result.rows[0] as { id: string; app_name: string | null; preview_url: string; customer_id: string };

    // Send the "website is live" email to the build's OWNER. When the owner
    // approves it is the session user; when the admin approves on their behalf
    // the session user is the admin, who must not receive the customer's email.
    let recipientEmail = req.session.customerEmail || '';
    let recipientName = req.session.customerName || '';
    if (build.customer_id && build.customer_id !== req.session.customerId) {
      try {
        const owner = await pool.query<{ email: string; name: string }>(
          'SELECT email, name FROM customers WHERE id = $1',
          [build.customer_id]
        );
        recipientEmail = owner.rows[0]?.email ?? '';
        recipientName = owner.rows[0]?.name ?? '';
      } catch (lookupErr) {
        recipientEmail = '';
        console.error('[approve] Owner lookup failed:', (lookupErr as Error).message);
      }
    }
    if (recipientEmail) {
      sendAppLiveEmail(
        recipientEmail,
        recipientName,
        build.app_name || buildId,
        build.preview_url,
        buildId
      ).catch(err => console.error('[approve] App live email failed:', (err as Error).message));
    }

    // Cancel any pending welcome nurture emails (they already built)
    cancelWelcomeSequence(build.customer_id).catch(err =>
      console.error('[approve] Cancel welcome sequence failed:', (err as Error).message)
    );

    // Schedule post-build follow-up sequence (day 3 check-in, day 7 testimonial)
    schedulePostBuildSequence(
      build.customer_id,
      build.app_name || buildId,
      build.preview_url
    ).catch(err =>
      console.error('[approve] Post-build sequence scheduling failed:', (err as Error).message)
    );

    req.session.save(() => res.redirect(`/builds/${buildId}`));
  } catch (err) {
    console.error('Error approving build:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to approve build.' });
  }
});

// Build logs: real-time pipeline output for the status page
router.get('/api/builds/:buildId/logs', requireAuth, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const after = parseInt(req.query.after as string, 10) || 0;
  const isAdmin = isAdminSession(req);

  try {
    const ownership = isAdmin
      ? await pool.query('SELECT id FROM builds WHERE id = $1', [buildId])
      : await pool.query('SELECT id FROM builds WHERE id = $1 AND customer_id = $2',
          [buildId, req.session.customerId]);

    if (ownership.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    // Never show api_costs on the status page (raw JSON, not customer-friendly).
    // Cost data is available at /admin/costs instead.
    const result = await pool.query(
      `SELECT id, stage, message, created_at
       FROM build_logs
       WHERE build_id = $1 AND id > $2 AND stage != 'api_costs'
       ORDER BY id ASC
       LIMIT 100`,
      [buildId, after]
    );

    const logs = isAdmin
      ? result.rows
      : (result.rows as Array<{ id: string; stage: string; message: string; created_at: Date }>).map((row) => ({
          ...row,
          message: typeof row.message === 'string' ? redactForCustomer(row.message) : row.message,
        }));

    res.setHeader('Cache-Control', 'no-store');
    res.json({ logs });
  } catch (err) {
    console.error('Error fetching build logs:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch build logs.' });
  }
});

// Serve build screenshots from DB (base64-encoded PNGs stored in screenshots JSONB)
router.get('/api/builds/:buildId/screenshots/:index', requireAuth, async (req: Request, res: Response) => {
  const { buildId, index } = req.params;
  const idx = parseInt(index, 10);
  const isAdmin = isAdminSession(req);

  try {
    const query = isAdmin
      ? 'SELECT screenshots FROM builds WHERE id = $1'
      : 'SELECT screenshots FROM builds WHERE id = $1 AND customer_id = $2';
    const params = isAdmin ? [buildId] : [buildId, req.session.customerId];
    const result = await pool.query(query, params);

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const screenshots = result.rows[0].screenshots || [];
    if (isNaN(idx) || idx < 0 || idx >= screenshots.length) {
      res.status(404).json({ error: 'Screenshot not found.' });
      return;
    }

    const entry = screenshots[idx];
    if (!entry || typeof entry !== 'object' || !entry.data) {
      res.status(404).json({ error: 'Screenshot data missing.' });
      return;
    }

    const buffer = Buffer.from(entry.data, 'base64');
    res.setHeader('Content-Type', 'image/png');
    // Behind requireAuth and the ownership check, so shared caches (CDN, proxy)
    // must not store it: `public` would let one customer's screenshot be served
    // from a cache to someone who is not logged in.
    res.setHeader('Cache-Control', 'private, max-age=86400');
    res.setHeader('Content-Length', buffer.length.toString());
    res.end(buffer);
  } catch (err) {
    console.error('Error serving screenshot:', (err as Error).message);
    res.status(500).json({ error: 'Failed to load screenshot.' });
  }
});

/**
 * The admin's source download: any build at preview or live. The admin is the
 * reviewer and reads the source before a verdict exists.
 */
const ADMIN_SOURCE_DOWNLOAD_SQL = `SELECT b.app_name,
                  src.allowed,
                  (b.build_artifact IS NOT NULL) AS has_artifact,
                  CASE WHEN src.allowed THEN b.build_artifact END AS build_artifact
             FROM builds b
            CROSS JOIN LATERAL (SELECT b.status IN ('preview', 'live') AS allowed) src
            WHERE b.id = $1`;

// Download source code artifact (tar.gz). Who may have it is decided in the
// query (OWNER_SOURCE_DOWNLOAD_SQL for the owner).
router.get('/builds/:buildId/download-source', requireAuth, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    const result = isAdmin
      ? await pool.query(ADMIN_SOURCE_DOWNLOAD_SQL, [buildId])
      : await pool.query(OWNER_SOURCE_DOWNLOAD_SQL, [buildId, req.session.customerId]);

    if (result.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const { app_name, build_artifact, allowed, has_artifact } = result.rows[0] as {
      app_name: string | null;
      build_artifact: Buffer | null;
      allowed: boolean;
      has_artifact: boolean;
    };

    if (!allowed) {
      if (isAdmin) {
        res.status(400).send('This download is only available once the website is in preview or live.');
      } else {
        res.status(403).send('This download becomes available once your website is live.');
      }
      return;
    }

    if (!has_artifact || !build_artifact) {
      res.status(404).send('No download is available for this build yet.');
      return;
    }

    // app_name came from a model-written spec; keep it to filename-safe
    // characters so it cannot break out of the quoted Content-Disposition value.
    const safeName = String(app_name || 'website').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'website';
    const filename = `${safeName}-source.tar.gz`;
    const buffer = Buffer.from(build_artifact);

    res.setHeader('Content-Type', 'application/gzip');
    res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    res.setHeader('Content-Length', buffer.length.toString());
    res.end(buffer);
  } catch (err) {
    console.error('Error downloading source:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

// Count revision builds in a build's lineage (walks parent_build_id chain)
async function countRevisionsInLineage(buildId: string): Promise<{ count: number }> {
  const result = await pool.query(`
    WITH RECURSIVE lineage AS (
      SELECT id, parent_build_id, is_revision
      FROM builds WHERE id = $1
      UNION ALL
      SELECT b.id, b.parent_build_id, b.is_revision
      FROM builds b JOIN lineage l ON b.id = l.parent_build_id
    )
    SELECT
      (SELECT COUNT(*) FROM lineage WHERE is_revision = true) as revision_count
  `, [buildId]);

  const row = result.rows[0];
  return {
    count: parseInt(row?.revision_count || '0', 10),
  };
}

/**
 * Insert a rebuild or revision build. For a customer the insert runs under
 * the build-start lock and only if the budget allows it; the admin's insert
 * is unconditional. Returns the new build id, or the reason it was refused.
 *
 * Every statement passed in sets parent_build_id, and that is what carries
 * the Harbor flag over: the builds_inherit_is_harbor trigger (db/schema.sql)
 * copies is_harbor from the parent row on insert, so the child of a Harbor
 * build needs a subscription to publish just as its parent did.
 */
async function queueChildBuild(input: {
  isAdmin: boolean;
  customerId: string;
  parentBuildId: string;
  kind: 'rebuild' | 'revision';
  sql: string;
  params: unknown[];
}): Promise<{ id: string } | { refusal: string }> {
  if (input.isAdmin) {
    const result = await pool.query(input.sql, input.params);
    return { id: (result.rows[0] as { id: string }).id };
  }
  return withCustomerBuildLock(pool, input.customerId, async (client) => {
    const budget = await readBuildStartBudget(client, input.customerId, input.parentBuildId);
    const refusal = buildStartRefusal(budget, 'new');
    if (refusal) return { refusal };
    const result = await client.query(input.sql, input.params);
    const id = (result.rows[0] as { id: string }).id;
    await recordBuildStart(client, input.customerId, id, input.kind);
    return { id };
  });
}

const revisionRateLimit = boundedRateLimit({
  windowMs: 60 * 1000,
  max: 30,
  message: { error: 'Too many messages. Please wait a moment before sending again.' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: customerOrAddressKey,
});

// Revision chat page: multi-turn AI conversation for revision scoping
router.get('/builds/:buildId/revision', requireAuth, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    const query = isAdmin
      ? `SELECT id, tier, spec, app_name, review_notes, customer_id FROM builds WHERE id = $1 AND status = 'preview'`
      : `SELECT id, tier, spec, app_name, review_notes, customer_id FROM builds WHERE id = $1 AND customer_id = $2 AND status = 'preview'`;
    const params = isAdmin ? [buildId] : [buildId, req.session.customerId];
    const buildResult = await pool.query(query, params);

    if (buildResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = buildResult.rows[0] as { id: string; tier: BuildTier; spec: AppSpec; app_name: string; review_notes: string | null; customer_id: string };

    const { count } = await countRevisionsInLineage(buildId);
    const limit = UNLIMITED_REVISION_LIMIT;

    if (count >= limit) {
      res.redirect(`/projects/${buildId}/features`);
      return;
    }

    const messagesResult = await pool.query(
      'SELECT * FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );

    let messages = messagesResult.rows as IntakeMessage[];

    if (messages.length === 0) {
      const seNotes = typeof build.review_notes === 'string' ? build.review_notes.trim() : null;
      const greeting = await generateRevisionGreeting(build.spec, build.tier as BuildTier, seNotes || null);
      await pool.query(
        'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
        [buildId, 'assistant', greeting]
      );
      messages = [{ id: '', build_id: buildId, role: 'assistant', content: greeting, created_at: new Date() }];
    }

    const csrfToken = generateCsrfToken(req);
    const turnCount = messages.filter((m) => m.role === 'user').length;
    const revisionReady = messages.length > 0 && messages[messages.length - 1].content.includes('REVISION SUMMARY:');

    res.render('revision', {
      title: 'Request Revision',
      build,
      messages,
      csrfToken,
      turnCount,
      revisionReady,
      revisionInfo: { used: count, limit, remaining: limit - count },
    });
  } catch (err) {
    console.error('Error loading revision chat:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load revision chat.' });
  }
});

// Revision chat message: AI conversation turn
router.post('/builds/:buildId/revision/message', requireAuth, revisionRateLimit, customerLlmDailyLimit, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const { message } = req.body as { message?: unknown };
  const csrfToken = req.headers['x-csrf-token'] as string;

  if (!csrfToken || !verifyHmacToken(csrfToken, req)) {
    res.status(403).json({ error: 'Invalid CSRF token.' });
    return;
  }

  // A JSON number or array has no .trim(); that used to throw outside the try.
  if (typeof message !== 'string' || message.trim().length === 0) {
    res.status(400).json({ error: 'Message is required.' });
    return;
  }

  const sanitizedMessage = message.trim().slice(0, 2000);
  const isAdmin = isAdminSession(req);

  try {
    const query = isAdmin
      ? `SELECT id, tier, spec, review_notes FROM builds WHERE id = $1 AND status = 'preview'`
      : `SELECT id, tier, spec, review_notes FROM builds WHERE id = $1 AND customer_id = $2 AND status = 'preview'`;
    const params = isAdmin ? [buildId] : [buildId, req.session.customerId];
    const buildResult = await pool.query(query, params);

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in preview status.' });
      return;
    }

    const build = buildResult.rows[0] as { id: string; tier: BuildTier; spec: AppSpec; review_notes: string | null };

    const historyResult = await pool.query(
      'SELECT role, content, created_at FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );
    const history = historyResult.rows as IntakeMessage[];

    while (history.length > 0 && history[history.length - 1].role === 'user') {
      history.pop();
    }

    const turnCount = history.filter((m) => m.role === 'user').length;
    if (turnCount >= REVISION_HARD_TURN_CAP) {
      res.status(409).json({
        error:
          'This conversation has reached its maximum length. Use "Confirm and Queue Revision" with what we have, or write to support@craftandship.io.',
      });
      return;
    }
    const seNotes = typeof build.review_notes === 'string' ? build.review_notes.trim() : null;

    const response = await processRevisionMessage(
      sanitizedMessage,
      history,
      turnCount,
      build.spec,
      build.tier as BuildTier,
      seNotes || null
    );

    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'user', sanitizedMessage]
    );
    await pool.query(
      'INSERT INTO intake_messages (build_id, role, content) VALUES ($1, $2, $3)',
      [buildId, 'assistant', response.reply]
    );

    res.json(response);
  } catch (err) {
    console.error('Error processing revision message:', (err as Error).message);
    if ((err as Error).name === 'AssistantNotConfiguredError') {
      respondAssistantNotConfigured(res);
    } else if ((err as Error).name === 'CircuitBreakerOpenError') {
      res.status(503).json({ error: (err as Error).message });
    } else {
      res.status(500).json({ error: 'Failed to process message. Please try again.' });
    }
  }
});

// Revision confirm: queue the revision build after chat conversation
router.post('/builds/:buildId/revision/confirm', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const isAdmin = isAdminSession(req);

  try {
    const query = isAdmin
      ? `SELECT id, tier, spec, app_name, review_notes, customer_id FROM builds WHERE id = $1 AND status = 'preview'`
      : `SELECT id, tier, spec, app_name, review_notes, customer_id FROM builds WHERE id = $1 AND customer_id = $2 AND status = 'preview'`;
    const params = isAdmin ? [buildId] : [buildId, req.session.customerId];
    const original = await pool.query(query, params);

    if (original.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = original.rows[0];
    const { count } = await countRevisionsInLineage(buildId);
    const limit = UNLIMITED_REVISION_LIMIT;

    if (count >= limit) {
      res.redirect(`/projects/${buildId}/features`);
      return;
    }

    // Gather the conversation as the revision feedback
    const messagesResult = await pool.query(
      'SELECT role, content FROM intake_messages WHERE build_id = $1 ORDER BY created_at ASC',
      [buildId]
    );
    const chatMessages = messagesResult.rows as { role: string; content: string }[];

    // Build feedback from the full conversation
    const conversationFeedback = chatMessages
      .filter((m) => m.role === 'user')
      .map((m) => m.content)
      .join('\n');

    // Extract the revision summary from the last assistant message if present
    const lastAssistant = [...chatMessages].reverse().find((m) => m.role === 'assistant');
    const summaryMatch = lastAssistant?.content.match(/REVISION SUMMARY:\s*([\s\S]*?)$/);
    const revisionSummary = summaryMatch ? summaryMatch[1].trim() : conversationFeedback;

    const seNotes = typeof build.review_notes === 'string' ? build.review_notes.trim() : '';
    const rebuildRequest = seNotes
      ? `REVISION REQUEST (round ${count + 1} of ${limit}):\n\nSENIOR ENGINEER REVIEW NOTES:\n${seNotes}\n\nCUSTOMER REVISION (from conversation):\n${revisionSummary}`
      : `REVISION REQUEST (round ${count + 1} of ${limit}):\n${revisionSummary}`;

    const specWithHistory = { ...build.spec };
    if (!Array.isArray(specWithHistory.revision_history)) {
      specWithHistory.revision_history = [];
    }
    specWithHistory.revision_history.push({
      round: count + 1,
      feedback: revisionSummary,
      se_notes: seNotes || null,
      created_at: new Date().toISOString()
    });

    // The revision belongs to the build's owner. Using the session user here
    // handed an admin-initiated revision to the admin's own account.
    const queued = await queueChildBuild({
      isAdmin,
      customerId: build.customer_id,
      parentBuildId: buildId,
      kind: 'revision',
      sql: `INSERT INTO builds (customer_id, tier, status, spec, app_name, parent_build_id, rebuild_request, is_revision)
            VALUES ($1, $2, 'queued', $3, $4, $5, $6, true)
            RETURNING id`,
      params: [build.customer_id, build.tier, JSON.stringify(specWithHistory), build.app_name, buildId, rebuildRequest],
    });
    if ('refusal' in queued) {
      renderBuildStartRefused(res, queued.refusal);
      return;
    }

    res.redirect(`/builds/${queued.id}`);
  } catch (err) {
    console.error('Error confirming revision:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to create revision build.' });
  }
});

// Request Revision (legacy form fallback): single-step revision from modal
router.post('/builds/:buildId/revision', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const rawFeedback = req.body.revision_feedback;
  const feedback = (typeof rawFeedback === 'string' ? rawFeedback : '').trim().substring(0, 4000);
  const isAdmin = isAdminSession(req);

  if (!feedback) {
    renderErrorPage(res, 400, { title: 'Error', error: 'Please describe what needs to be fixed.' });
    return;
  }

  try {
    const query = isAdmin
      ? `SELECT id, tier, spec, app_name, review_notes, customer_id FROM builds WHERE id = $1 AND status = 'preview'`
      : `SELECT id, tier, spec, app_name, review_notes, customer_id FROM builds WHERE id = $1 AND customer_id = $2 AND status = 'preview'`;
    const params = isAdmin ? [buildId] : [buildId, req.session.customerId];
    const original = await pool.query(query, params);

    if (original.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = original.rows[0];

    // Check revision limit for this build lineage
    const { count } = await countRevisionsInLineage(buildId);
    const limit = UNLIMITED_REVISION_LIMIT;

    if (count >= limit) {
      res.redirect(`/projects/${buildId}/features`);
      return;
    }

    // Forward Senior Engineer review notes (if any) from the parent build into the
    // new build's rebuild_request so build.py sees both the SE concerns and the
    // customer's additional feedback. Without this, a customer clicking
    // "Request Revision" after Ocean has left notes in the admin review panel
    // would only ship their own words to the pipeline, leaving the SE concerns
    // invisible to Claude. This bug was discovered in local smoke testing,
    // when a test revision request said only "apply everything from the
    // review" and the resulting rebuild_request contained zero information
    // about what the reviewer had actually flagged.
    const seNotes = typeof build.review_notes === 'string' ? build.review_notes.trim() : '';
    const rebuildRequest = seNotes
      ? `REVISION REQUEST (round ${count + 1} of ${limit}):\n\nSENIOR ENGINEER REVIEW NOTES:\n${seNotes}\n\nCUSTOMER ADDITIONAL FEEDBACK:\n${feedback}`
      : `REVISION REQUEST (round ${count + 1} of ${limit}):\n${feedback}`;

    // Persist revision history in spec so it survives across all future builds
    const specWithHistory = { ...build.spec };
    if (!Array.isArray(specWithHistory.revision_history)) {
      specWithHistory.revision_history = [];
    }
    specWithHistory.revision_history.push({
      round: count + 1,
      feedback,
      se_notes: seNotes || null,
      created_at: new Date().toISOString()
    });

    const queued = await queueChildBuild({
      isAdmin,
      customerId: build.customer_id,
      parentBuildId: buildId,
      kind: 'revision',
      sql: `INSERT INTO builds (customer_id, tier, status, spec, app_name, parent_build_id, rebuild_request, is_revision)
            VALUES ($1, $2, 'queued', $3, $4, $5, $6, true)
            RETURNING id`,
      params: [build.customer_id, build.tier, JSON.stringify(specWithHistory), build.app_name, buildId, rebuildRequest],
    });
    if ('refusal' in queued) {
      renderBuildStartRefused(res, queued.refusal);
      return;
    }

    res.redirect(`/builds/${queued.id}`);
  } catch (err) {
    console.error('Error creating revision:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to create revision build.' });
  }
});

// One-Click Rebuild: create a new build from an existing live or failed build's spec
router.post('/builds/:buildId/rebuild', requireAuth, validateCsrfToken, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const rawChange = req.body.rebuild_request;
  const changeRequest = (typeof rawChange === 'string' ? rawChange : '').trim().substring(0, 2000) || null;

  try {
    // Fetch original build (must be owned by customer, status live, failed, or preview)
    const original = await pool.query(
      `SELECT id, tier, spec, app_name
       FROM builds
       WHERE id = $1 AND customer_id = $2 AND status IN ('live', 'failed', 'preview')`,
      [buildId, req.session.customerId]
    );

    if (original.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const { tier, spec, app_name } = original.rows[0];

    if (!spec) {
      renderErrorPage(res, 400, { title: 'Error', error: 'Original build has no spec to rebuild from.' });
      return;
    }

    // Preserve revision_history in spec so past customer feedback carries forward
    const specForRebuild = { ...spec };
    if (changeRequest && Array.isArray(specForRebuild.revision_history)) {
      // Append rebuild request to history so it persists even if this build fails
      specForRebuild.revision_history.push({
        round: specForRebuild.revision_history.length + 1,
        feedback: changeRequest,
        created_at: new Date().toISOString()
      });
    }

    // Create new build record linked to the original. This route only ever
    // acts on the caller's own builds, so the budget always applies.
    const queued = await queueChildBuild({
      isAdmin: false,
      customerId: req.session.customerId as string,
      parentBuildId: buildId,
      kind: 'rebuild',
      sql: `INSERT INTO builds (customer_id, tier, status, spec, app_name, parent_build_id, rebuild_request)
            VALUES ($1, $2, 'queued', $3, $4, $5, $6)
            RETURNING id`,
      params: [req.session.customerId, tier, JSON.stringify(specForRebuild), app_name, buildId, changeRequest],
    });
    if ('refusal' in queued) {
      renderBuildStartRefused(res, queued.refusal);
      return;
    }

    res.redirect(`/builds/${queued.id}`);
  } catch (err) {
    console.error('Error creating rebuild:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to create rebuild.' });
  }
});

// Customer data export (GDPR): download all personal data as JSON
router.get('/export-data', requireAuth, async (req: Request, res: Response) => {
  try {
    const customerId = req.session.customerId;

    const [customerResult, buildsResult, messagesResult, featuresResult] = await Promise.all([
      pool.query(
        'SELECT id, email, name, created_at FROM customers WHERE id = $1',
        [customerId]
      ),
      pool.query(
        `SELECT id, tier, status, app_name, live_url, created_at, updated_at, started_at, completed_at
         FROM builds WHERE customer_id = $1 ORDER BY created_at DESC`,
        [customerId]
      ),
      pool.query(
        `SELECT im.role, im.content, im.created_at, b.app_name
         FROM intake_messages im
         JOIN builds b ON b.id = im.build_id
         WHERE b.customer_id = $1
         ORDER BY im.created_at ASC`,
        [customerId]
      ),
      pool.query(
        `SELECT fr.id, fr.status, fr.description, fr.scope_summary, fr.created_at
         FROM feature_requests fr
         WHERE fr.customer_id = $1
         ORDER BY fr.created_at DESC`,
        [customerId]
      ),
    ]);

    const exportData = {
      exported_at: new Date().toISOString(),
      customer: customerResult.rows[0] || null,
      builds: buildsResult.rows,
      intake_conversations: messagesResult.rows,
      feature_requests: featuresResult.rows,
    };

    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Content-Disposition', 'attachment; filename="craftandship-data-export.json"');
    res.json(exportData);
  } catch (err) {
    console.error('Error exporting data:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to export data.' });
  }
});

export default router;
