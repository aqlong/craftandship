// Admin routes, gated to ADMIN_EMAIL only

import { Router, Request, Response, NextFunction } from 'express';
import crypto from 'crypto';
import pool from '../db';
import { forwardAsyncErrors } from '../utils/async-routes';
import { validateCsrfToken, generateCsrfToken, isAdminSession, isDevOrTestEnv } from '../middleware/auth';
import { REVIEW_RESET_ASSIGNMENTS } from './builds';
import { analyzeBuildFailure, CircuitBreakerOpenError } from '../services/claude';
import { getAnthropicCosts, getAnthropicCostsAndStatus, rebuildAnthropicCostsCache } from '../services/anthropic-costs';
import {
  sendReviewReadyEmail,
  sendReviewChangesRequestedEmail,
} from '../services/email';
import { deleteRailwayProject, readRailwayProjectName, RailwayApiError, isRailwayWriteEnabled } from '../services/railway';
import { extractSpecFromConversation } from '../services/spec_extraction';
import { translateExtractedSpecToV1 } from '../services/harbor_to_compose';
import { dispatchFailureResponse, insertHarborBuild, isBuildableOwnerEmail } from '../services/harbor_dispatch';
import type { MemoryOwnerColumn } from '../companion/memory_retrieval';
import { crawlSite } from '../services/site_copy_crawl';
import { describeCoordinatorSettings, planCoordinatorSettingsSave, COORDINATOR_SETTING_NAMES, GO_LIVE_FIELD, GO_LIVE_PHRASE, SHOWN_PREFIX } from '../services/coordinator-settings';
import { renderErrorPage } from '../utils/error-page';
import { hashUnusablePassword } from '../utils/password-hash';

const router = forwardAsyncErrors(Router());
const PAGE_SIZE = 50;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Form and JSON bodies are attacker-shaped: extended urlencoding turns `a[]=1`
// into an array and JSON can send numbers or objects. Free-text admin fields go
// through this so a non-string is dropped and a long one cannot reach the
// database or a downstream prompt unbounded.
function optionalText(value: unknown, maxLength: number): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim().slice(0, maxLength);
  return trimmed || null;
}

const RAILWAY_FETCH_TIMEOUT_MS = 15_000;


type EmailStatus = 'sent' | 'failed' | 'skipped';
type EmailTemplate = 'welcome' | 'app_live' | 'build_failed';

interface EmailLogFilters {
  status: string;
  template: string;
  q: string;
}

interface TodayStats {
  sent: number;
  failed: number;
  skipped: number;
}

// Statuses an admin may reset back to 'queued' in place. Anything else (live,
// preview, abandoned...) gets a NEW build instead.
const RESET_STATUSES = ['failed', 'building', 'testing', 'deploying', 'queued'];

function requireAdmin(req: Request, res: Response, next: NextFunction): void {
  if (!req.session.customerId) {
    res.redirect(`/login?returnTo=${encodeURIComponent(req.originalUrl)}`);
    return;
  }
  if (!isAdminSession(req)) {
    renderErrorPage(res, 403, { title: 'Forbidden', error: 'Admin access required.' });
    return;
  }
  next();
}

const VALID_STATUSES: EmailStatus[] = ['sent', 'failed', 'skipped'];
const VALID_TEMPLATES: EmailTemplate[] = ['welcome', 'app_live', 'build_failed'];

router.get('/emails', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
  const offset = (page - 1) * PAGE_SIZE;

  const rawStatus = req.query.status as string;
  const rawTemplate = req.query.template as string;
  const rawQ = (req.query.q as string) ?? '';

  const statusFilter = VALID_STATUSES.includes(rawStatus as EmailStatus) ? rawStatus : '';
  const templateFilter = VALID_TEMPLATES.includes(rawTemplate as EmailTemplate) ? rawTemplate : '';
  const qFilter = rawQ.trim();

  const filters: EmailLogFilters = {
    status: statusFilter,
    template: templateFilter,
    q: qFilter,
  };

  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (statusFilter) {
    params.push(statusFilter);
    conditions.push(`status = $${params.length}`);
  }
  if (templateFilter) {
    params.push(templateFilter);
    conditions.push(`template = $${params.length}`);
  }
  if (qFilter) {
    params.push(`%${qFilter}%`);
    conditions.push(
      `(to_email ILIKE $${params.length} OR subject ILIKE $${params.length} OR template ILIKE $${params.length})`
    );
  }

  const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

  const countParams = [...params];
  const countResult = await pool.query(
    `SELECT COUNT(*) AS total FROM email_log ${whereClause}`,
    countParams
  );
  const total = parseInt(countResult.rows[0].total as string, 10);

  const dataParams = [...params, PAGE_SIZE, offset];
  const dataResult = await pool.query(
    `SELECT id, created_at, template, to_email, subject, status, status_code,
            error_message, resend_id, duration_ms
     FROM email_log
     ${whereClause}
     ORDER BY created_at DESC
     LIMIT $${dataParams.length - 1} OFFSET $${dataParams.length}`,
    dataParams
  );

  // Today stats query
  const todayResult = await pool.query(`
    SELECT
      COUNT(*) FILTER (WHERE status = 'sent') AS sent,
      COUNT(*) FILTER (WHERE status = 'failed') AS failed,
      COUNT(*) FILTER (WHERE status = 'skipped') AS skipped
    FROM email_log
    WHERE created_at >= CURRENT_DATE
  `);

  const todayStats: TodayStats = {
    sent: parseInt(todayResult.rows[0].sent as string, 10) || 0,
    failed: parseInt(todayResult.rows[0].failed as string, 10) || 0,
    skipped: parseInt(todayResult.rows[0].skipped as string, 10) || 0,
  };

  res.render('admin-emails', { activeTab: 'emails',
    title: 'Email Log',
    logs: dataResult.rows,
    total,
    page,
    filters,
    todayStats,
  });
});

router.get('/emails/:id', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  const { id } = req.params;
  if (!UUID_RE.test(id)) {
    res.status(404).render('404', { title: 'Not Found' });
    return;
  }
  const result = await pool.query('SELECT * FROM email_log WHERE id = $1', [id]);
  if (result.rows.length === 0) {
    res.status(404).render('404', { title: 'Not Found' });
    return;
  }
  res.render('admin-email-detail', { activeTab: 'emails',
    title: 'Email Preview',
    log: result.rows[0],
  });
});

// Admin builds list: all builds across all customers
router.get('/builds', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const statusFilter = req.query.status as string || '';
    const conditions: string[] = [];
    const params: (string | number)[] = [];

    if (statusFilter) {
      params.push(statusFilter);
      conditions.push(`b.status = $${params.length}`);
    }

    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    // build_engine is written authoritatively by the worker on dispatch (reads
    // CAS_BUILD_ENGINE at build time). The COALESCE fallback handles column
    // NULLs (abandoned intakes that never reached dispatch, historical rows
    // that predate the column). Default is 'v2' because CAS_BUILD_ENGINE has
    // been 'v2' in code since 2026-04-27 and on prod worker since 2026-04-22; 
    // any build that DID run, ran on v2; any unnamed/queued row that WOULD run
    // would also run on v2. Earlier behavior used a `CASE WHEN spec->>'archetype'
    // IS NOT NULL THEN v2 ELSE v1 END` heuristic that mis-tagged abandoned
    // intakes (NULL or thin spec) as v1 even though the engine that would
    // process them is v2.
    const buildsResult = await pool.query(
      `SELECT b.id, b.app_name, b.tier, b.status, b.error_message,
              b.created_at, b.started_at, b.completed_at, b.retry_count,
              b.live_url, b.preview_url, c.email AS customer_email, c.name AS customer_name,
              COALESCE(b.build_engine, 'v2') AS build_engine
       FROM builds b
       JOIN customers c ON b.customer_id = c.id
       ${whereClause}
       ORDER BY b.created_at DESC
       LIMIT 100`,
      params
    );

    const statsResult = await pool.query(`
      SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE status = 'queued') AS queued,
        COUNT(*) FILTER (WHERE status = 'building') AS building,
        COUNT(*) FILTER (WHERE status = 'testing') AS testing,
        COUNT(*) FILTER (WHERE status = 'deploying') AS deploying,
        COUNT(*) FILTER (WHERE status = 'live') AS live,
        COUNT(*) FILTER (WHERE status = 'failed') AS failed,
        COUNT(*) FILTER (WHERE status = 'abandoned') AS abandoned
      FROM builds
    `);

    const stats = statsResult.rows[0];

    res.render('admin-builds', { activeTab: 'builds',
      title: 'Admin - Builds',
      builds: buildsResult.rows,
      stats,
      statusFilter,
      railwayWritesEnabled: isRailwayWriteEnabled(),
    });
  } catch (err) {
    console.error('Error loading admin builds:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load builds.' });
  }
});

// JSON API for admin builds (used by client-side polling)
router.get('/api/builds', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const statusFilter = (req.query.status as string) || '';
    const validStatuses = ['intake','payment_pending','queued','building','testing','deploying','preview','live','failed','abandoned'];
    const params: (string | number)[] = [];
    const conditions: string[] = [];
    if (statusFilter && validStatuses.includes(statusFilter)) {
      params.push(statusFilter);
      conditions.push(`b.status = $${params.length}`);
    }
    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [buildsResult, statsResult] = await Promise.all([
      pool.query(
        `SELECT b.id, b.app_name, b.tier, b.status, b.error_message,
                b.created_at, b.started_at, b.completed_at, b.retry_count,
                b.live_url, c.email AS customer_email, c.name AS customer_name,
                COALESCE(b.build_engine, 'v2') AS build_engine
         FROM builds b
         JOIN customers c ON b.customer_id = c.id
         ${whereClause}
         ORDER BY b.created_at DESC
         LIMIT 100`,
        params
      ),
      pool.query(`
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE status = 'queued') AS queued,
          COUNT(*) FILTER (WHERE status = 'building') AS building,
          COUNT(*) FILTER (WHERE status = 'testing') AS testing,
          COUNT(*) FILTER (WHERE status = 'deploying') AS deploying,
          COUNT(*) FILTER (WHERE status = 'live') AS live,
          COUNT(*) FILTER (WHERE status = 'failed') AS failed
        FROM builds
      `)
    ]);

    res.json({ builds: buildsResult.rows, stats: statsResult.rows[0] });
  } catch (err) {
    console.error('Error fetching admin builds API:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch builds.' });
  }
});

// Admin customers list
router.get('/customers', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
    const offset = (page - 1) * PAGE_SIZE;
    const qFilter = ((req.query.q as string) ?? '').trim();

    const conditions: string[] = [];
    const params: (string | number)[] = [];
    let paramIdx = 1;

    if (qFilter) {
      conditions.push(`(c.email ILIKE $${paramIdx} OR c.name ILIKE $${paramIdx})`);
      params.push(`%${qFilter}%`);
      paramIdx++;
    }

    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [customersResult, countResult] = await Promise.all([
      pool.query(`
        SELECT c.id, c.email, c.name, c.created_at,
               COUNT(b.id) AS build_count,
               COUNT(b.id) FILTER (WHERE b.status = 'live') AS live_count,
               COUNT(b.id) FILTER (WHERE b.status = 'failed') AS failed_count
        FROM customers c
        LEFT JOIN builds b ON b.customer_id = c.id
        ${whereClause}
        GROUP BY c.id
        ORDER BY c.created_at DESC
        LIMIT $${paramIdx} OFFSET $${paramIdx + 1}
      `, [...params, PAGE_SIZE, offset]),
      pool.query(`SELECT COUNT(*) FROM customers c ${whereClause}`, params),
    ]);

    const totalCount = parseInt(countResult.rows[0].count, 10);
    const totalPages = Math.ceil(totalCount / PAGE_SIZE);

    res.render('admin-customers', { activeTab: 'customers',
      title: 'Admin - Customers',
      customers: customersResult.rows,
      page,
      totalPages,
      totalCount,
      qFilter,
    });
  } catch (err) {
    console.error('Error loading admin customers:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load customers.' });
  }
});

// ---------------------------------------------------------------------------
// Admin AI Build Tools (section-03)
// ---------------------------------------------------------------------------

// Build detail page: status strip, diagnostic tabs, AI analysis card, queue-fix panel
router.get('/builds/:buildId', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  const { buildId } = req.params;
  try {
    const [buildResult, logsResult] = await Promise.all([
      pool.query(
        `SELECT id, app_name, tier, status, error_message, spec,
                qa_results, design_review_results, ai_analysis,
                stage_timings, retry_count, total_api_cost_usd,
                created_at, started_at, completed_at, customer_id,
                live_url, preview_url, railway_project_id,
                COALESCE(build_engine, 'v2') AS build_engine,
                gallery_opted_in, gallery_featured,
                gallery_description, gallery_category, gallery_display_name
         FROM builds WHERE id = $1`,
        [buildId]
      ),
      pool.query(
        `SELECT id, stage, message, created_at FROM build_logs
         WHERE build_id = $1 ORDER BY id ASC LIMIT 100`,
        [buildId]
      ),
    ]);

    if (buildResult.rows.length === 0) {
      renderErrorPage(res, 404, { title: 'Not Found', error: 'Build not found.' });
      return;
    }

    const build = buildResult.rows[0];
    const logs = logsResult.rows;

    const qaResults = (build.qa_results as Array<{ name: string; passed: boolean; error: string | null }>) || [];
    const designResults = (build.design_review_results as Array<{ severity: string; issue?: string; message?: string; file?: string; category?: string; suggestion?: string }>) || [];

    const qaFailures = qaResults.filter(r => !r.passed);
    const designIssues = designResults.filter(d => d.severity === 'critical' || d.severity === 'warning');

    const defaultAction = RESET_STATUSES.includes(build.status) ? 'reset' : 'new';

    const csrfToken = generateCsrfToken(req);

    res.render('admin-build-detail', {
      activeTab: 'builds',
      title: `Admin - Build ${build.app_name || buildId}`,
      build,
      logs,
      qaFailures,
      designIssues,
      csrfToken,
      defaultAction,
      railwayWritesEnabled: isRailwayWriteEnabled(),
    });
  } catch (err) {
    console.error('Error loading build detail:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load build.' });
  }
});

// Returns AI analysis for a build, caching in builds.ai_analysis.
// Query: ?force=1 bypasses cache. Response: { analysis, cached }.
router.post('/builds/:buildId/analyze', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { buildId } = req.params;
  try {
    const buildResult = await pool.query(
      `SELECT id, tier, status, error_message, spec,
              qa_results, design_review_results, ai_analysis
       FROM builds WHERE id = $1`,
      [buildId]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const build = buildResult.rows[0];

    // Return cached analysis unless ?force=1
    if (build.ai_analysis !== null && req.query.force !== '1') {
      res.json({ analysis: build.ai_analysis, cached: true });
      return;
    }

    // Fetch logs then call Claude
    const logsResult = await pool.query(
      // The newest 50 entries, oldest first: a failure is at the end of a log.
      `SELECT stage, message, created_at FROM (
         SELECT id, stage, message, created_at FROM build_logs
          WHERE build_id = $1 ORDER BY id DESC LIMIT 50
       ) newest ORDER BY id ASC`,
      [buildId]
    );

    const qaResults = (build.qa_results as Array<{ name: string; passed: boolean; error: string | null }>) || [];
    const designResults = (build.design_review_results as Array<{ severity: string; issue?: string; message?: string; file?: string; category?: string; suggestion?: string }>) || [];

    const analysisResult = await analyzeBuildFailure(
      {
        id: build.id as string,
        tier: build.tier as string,
        status: build.status as string,
        error_message: build.error_message as string | null,
        spec: build.spec as object | null,
      },
      logsResult.rows,
      qaResults,
      designResults
    );

    const storedValue = { ...analysisResult, analyzed_at: new Date().toISOString() };
    await pool.query(
      'UPDATE builds SET ai_analysis = $2 WHERE id = $1',
      [buildId, storedValue]
    );

    res.json({ analysis: storedValue, cached: false });
  } catch (err) {
    console.error('Error analyzing build:', (err as Error).message);
    res.status(500).json({ error: (err as Error).message || 'Analysis failed.' });
  }
});

// Re-queues a build for the worker. Resets existing build (failed/building/testing/deploying)
// or creates a new build (live/preview). Body: { rebuild_request, force_new }.
router.post('/builds/:buildId/ai-fix', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { buildId } = req.params;

  const rebuildRequest = optionalText(req.body.rebuild_request, 8000) ?? '';
  if (!rebuildRequest) {
    res.status(400).json({ error: 'rebuild_request is required' });
    return;
  }

  try {
    const buildResult = await pool.query(
      `SELECT id, customer_id, tier, app_name, spec, status FROM builds WHERE id = $1`,
      [buildId]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const build = buildResult.rows[0];
    const forceNew = (req.body.force_new as string) === '1';
    const useNewBuild = forceNew || !RESET_STATUSES.includes(build.status as string);

    if (useNewBuild) {
      const insertResult = await pool.query(
        `INSERT INTO builds
           (customer_id, tier, app_name, spec, status, parent_build_id, rebuild_request)
         VALUES ($1, $2, $3, $4, 'queued', $5, $6)
         RETURNING id`,
        [build.customer_id, build.tier, build.app_name, build.spec, build.id, rebuildRequest]
      );
      const newBuildId = insertResult.rows[0].id as string;
      res.redirect(`/admin/builds/${newBuildId}`);
    } else {
      // The status predicate makes the reset atomic: the SELECT above is only
      // a hint, and a build that finished (or was killed) in the meantime must
      // not be dragged back to 'queued' underneath the worker.
      const reset = await pool.query(
        `UPDATE builds
         SET status = 'queued',
             error_message = NULL,
             started_at = NULL,
             completed_at = NULL,
             retry_count = 0,
             rebuild_request = $2,
             ${REVIEW_RESET_ASSIGNMENTS}
         WHERE id = $1 AND status = ANY($3::text[])`,
        [buildId, rebuildRequest, RESET_STATUSES]
      );
      if ((reset.rowCount ?? 0) === 0) {
        res.status(409).json({ error: 'Build changed state while resetting; reload and try again.' });
        return;
      }
      res.redirect(`/admin/builds/${buildId}`);
    }
  } catch (err) {
    console.error('Error queuing AI fix:', (err as Error).message);
    res.status(500).json({ error: 'Failed to queue fix.' });
  }
});

// Put a failed build back in the queue so the worker retries it
router.post('/builds/:buildId/reset', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { buildId } = req.params;

  try {
    const result = await pool.query(
      `UPDATE builds
         SET status = 'queued',
             error_message = NULL,
             started_at = NULL,
             completed_at = NULL,
             retry_count = 0,
             ${REVIEW_RESET_ASSIGNMENTS}
       WHERE id = $1 AND status = 'failed'
       RETURNING id`,
      [buildId]
    );

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in failed state.' });
      return;
    }

    const referer = req.headers.referer;
    let safeRedirect = '/admin/emails';
    if (referer) {
      try {
        const refUrl = new URL(referer);
        if (refUrl.pathname.startsWith('/admin/')) safeRedirect = refUrl.pathname + refUrl.search;
      } catch { /* ignore malformed referer */ }
    }
    res.redirect(safeRedirect);
  } catch (err) {
    console.error('Error resetting build:', (err as Error).message);
    res.status(500).json({ error: 'Failed to reset build.' });
  }
});

// Clear the support_kb cache (truncate table)
router.post('/support-kb/clear', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  try {
    const result = await pool.query('DELETE FROM support_kb');
    const deleted = result.rowCount || 0;
    console.log(`Admin cleared support_kb cache: ${deleted} entries removed`);
    const referer = req.headers.referer;
    let safeRedirect = '/admin/builds';
    if (referer) {
      try {
        const refUrl = new URL(referer);
        if (refUrl.pathname.startsWith('/admin/')) safeRedirect = refUrl.pathname + refUrl.search;
      } catch { /* ignore malformed referer */ }
    }
    res.redirect(safeRedirect);
  } catch (err) {
    console.error('Error clearing support_kb:', (err as Error).message);
    res.status(500).json({ error: 'Failed to clear support KB cache.' });
  }
});

// Toggle gallery opt-in for a build (admin can opt any build in, bypassing customer ownership)
router.post('/gallery/:buildId/optin', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { buildId } = req.params;
  const description = optionalText(req.body.gallery_description, 500);
  const category = optionalText(req.body.gallery_category, 100);
  // Optional brand-name override. Empty/missing leaves the existing value; an
  // empty string explicitly clears (so admin can revert to slug-derived).
  const rawDisplay = typeof req.body.gallery_display_name === 'string'
    ? req.body.gallery_display_name.trim().slice(0, 120)
    : null;
  // Distinguish "field omitted" (null -> COALESCE preserves) from "field
  // submitted empty" (empty string -> set NULL, clears the override).
  const displayName: string | null | undefined = rawDisplay === null
    ? null
    : (rawDisplay === '' ? null : rawDisplay);
  const displaySubmitted = rawDisplay !== null;
  try {
    const result = await pool.query(
      `UPDATE builds
       SET gallery_opted_in = NOT COALESCE(gallery_opted_in, FALSE),
           gallery_description = COALESCE($1, gallery_description),
           gallery_category = COALESCE($2, gallery_category),
           gallery_display_name = CASE WHEN $4::boolean THEN $3 ELSE gallery_display_name END
       WHERE id = $5 AND status IN ('live', 'preview')
       RETURNING id, gallery_opted_in`,
      [description, category, displayName, displaySubmitted, buildId]
    );
    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not live/preview.' });
      return;
    }
    const referer = req.headers.referer;
    let safeRedirect = '/admin/builds';
    if (referer) {
      try {
        const refUrl = new URL(referer);
        if (refUrl.pathname.startsWith('/admin/')) safeRedirect = refUrl.pathname + refUrl.search;
      } catch { /* ignore malformed referer */ }
    }
    res.redirect(safeRedirect);
  } catch (err) {
    console.error('Error toggling gallery opt-in:', (err as Error).message);
    res.status(500).json({ error: 'Failed to toggle gallery opt-in.' });
  }
});

// Toggle gallery featured status for a build
router.post('/gallery/:buildId/feature', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { buildId } = req.params;
  try {
    const result = await pool.query(
      'UPDATE builds SET gallery_featured = NOT gallery_featured WHERE id = $1 RETURNING id, gallery_featured',
      [buildId]
    );
    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }
    const referer = req.headers.referer;
    let safeRedirect = '/admin/builds';
    if (referer) {
      try {
        const refUrl = new URL(referer);
        if (refUrl.pathname.startsWith('/admin/')) safeRedirect = refUrl.pathname + refUrl.search;
      } catch { /* ignore malformed referer */ }
    }
    res.redirect(safeRedirect);
  } catch (err) {
    console.error('Error toggling gallery feature:', (err as Error).message);
    res.status(500).json({ error: 'Failed to toggle gallery feature.' });
  }
});

// Admin cleanup: remove old duplicate builds of the same website.
// Keeps the 2 most recent builds per (customer, app_name), deletes older ones.
// app_name is only unique within a customer: two customers can both own
// "portfolio", and ranking across customers deleted one customer's builds
// because of another's. Builds still moving through the pipeline are never
// deleted.
router.post('/builds/cleanup', requireAdmin, validateCsrfToken, async (req: Request, res: Response) => {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Find builds to delete: for each app_name, keep newest 2 and mark the rest.
    // Also include failed builds older than 7 days regardless of position.
    const staleBuilds = await client.query(
      `SELECT id, app_name, railway_project_id FROM (
         SELECT id, app_name, railway_project_id,
                ROW_NUMBER() OVER (PARTITION BY customer_id, app_name ORDER BY created_at DESC) AS rn,
                status, created_at
         FROM builds
         WHERE app_name IS NOT NULL
       ) ranked
       WHERE (rn > 2 OR (status = 'failed' AND created_at < NOW() - INTERVAL '7 days'))
         AND status NOT IN ('queued', 'building', 'testing', 'deploying', 'preview')`
    );

    if (staleBuilds.rows.length === 0) {
      await client.query('ROLLBACK');
      res.redirect('/admin/builds');
      return;
    }

    const deleteIds = staleBuilds.rows.map((r: { id: string }) => r.id);
    const railwayProjectIds = staleBuilds.rows
      .map((r: { railway_project_id: string | null }) => r.railway_project_id)
      .filter(Boolean);

    // Delete related data first (foreign key constraints)
    await client.query('DELETE FROM intake_messages WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('DELETE FROM build_logs WHERE build_id = ANY($1::uuid[])', [deleteIds]);
    await client.query('DELETE FROM feature_messages WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE build_id = ANY($1))', [deleteIds]);
    await client.query('DELETE FROM feature_requests WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('DELETE FROM dead_letter_builds WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = ANY($1)', [deleteIds]);

    const result = await client.query('DELETE FROM builds WHERE id = ANY($1)', [deleteIds]);

    await client.query('COMMIT');

    if (railwayProjectIds.length > 0) {
      // This route only deletes database rows. The Railway projects are left
      // running; list them so the operator can remove them deliberately.
      console.log(`Admin cleanup: ${railwayProjectIds.length} Railway project(s) no longer referenced by any build row (NOT deleted): ${railwayProjectIds.join(', ')}`);
    }
    console.log(`Admin cleanup: deleted ${result.rowCount} old/failed build(s)`);
    res.redirect('/admin/builds');
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('Error cleaning up builds:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to clean up builds.' });
  } finally {
    client.release();
  }
});

// Admin cleanup: remove abandoned intake builds (no app_name, stuck in intake status)
router.post('/builds/cleanup-abandoned', requireAdmin, validateCsrfToken, async (req: Request, res: Response) => {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Find abandoned intakes: no app_name, status=intake, older than 24 hours
    const abandoned = await client.query(
      `SELECT id FROM builds
       WHERE app_name IS NULL AND status = 'intake'
       AND created_at < NOW() - INTERVAL '24 hours'`
    );

    if (abandoned.rows.length === 0) {
      await client.query('ROLLBACK');
      res.redirect('/admin/builds');
      return;
    }

    const deleteIds = abandoned.rows.map((r: { id: string }) => r.id);

    await client.query('DELETE FROM intake_messages WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('DELETE FROM build_logs WHERE build_id = ANY($1::uuid[])', [deleteIds]);
    await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = ANY($1)', [deleteIds]);
    const result = await client.query('DELETE FROM builds WHERE id = ANY($1)', [deleteIds]);

    await client.query('COMMIT');

    console.log(`Admin cleanup: removed ${result.rowCount} abandoned intake(s)`);
    res.redirect('/admin/builds');
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('Error cleaning up abandoned intakes:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to clean up abandoned intakes.' });
  } finally {
    client.release();
  }
});

// Force-fail a stuck build (kill)
router.post('/builds/:buildId/kill', requireAdmin, validateCsrfToken, async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const result = await pool.query(
      `UPDATE builds
         SET status = 'failed',
             error_message = 'Force-killed by admin',
             completed_at = NOW()
       WHERE id = $1 AND status IN ('building', 'testing', 'deploying', 'queued')
       RETURNING id, app_name`,
      [buildId]
    );
    if (result.rows.length === 0) {
      res.redirect('/admin/builds');
      return;
    }
    console.log('Admin killed build %s (%s)', buildId, result.rows[0].app_name);
    res.redirect('/admin/builds');
  } catch (err) {
    console.error('Error killing build:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to kill build.' });
  }
});

// ---- Cost Dashboard ----

interface CostSummary {
  today_build: number;
  today_agent: number;
  today_total: number;
  today_platform: number;
  today_untracked: number;
  period_total: number;
  period_build_total: number;
  period_platform: number;
  period_untracked: number;
  avg_per_build: number;
  avg_platform_per_day: number;
  daily_budget: number;
}

interface CostDailyRow {
  date: string;
  build_cost: number;
  agent_cost: number;
  total: number;
  build_count: number;
  platform_usd: number;
  untracked: number;
}

interface CostBuildRow {
  id: string;
  app_name: string | null;
  tier: string;
  cost: number;
  status: string;
  completed_at: string;
  cost_breakdown: Record<string, {
    advisor_model: string;
    advisor_input_tokens: number;
    advisor_output_tokens: number;
    advisor_cost_usd: number;
    executor_model: string;
    executor_cost_usd: number;
    total_cost_usd: number;
  }> | null;
}

interface CostTierRow {
  tier: string;
  count: number;
  total_cost: number;
  avg_cost: number;
}

interface CostAgentRow {
  agent: string;
  count: number;
  total_cost: number;
  avg_cost: number;
}

interface CostDashboardData {
  summary: CostSummary;
  daily: CostDailyRow[];
  builds: CostBuildRow[];
  per_tier: CostTierRow[];
  per_agent: CostAgentRow[];
  // True when the platform-cost data sourced from the Anthropic Admin API
  // is incomplete (mid-pagination error, pagination cap reached, or stale
  // DB cache served during an API outage). Surface this so the UI can warn
  // operators that period_platform is a lower bound, not the authoritative
  // total. Without this flag, the dashboard silently rendered a false-low
  // number: exactly the 2026-05-16 reconciliation gap that prompted this
  // wiring.
  platform_costs_partial: boolean;
  platform_costs_partial_reason: string | null;
}

export async function getCostData(rangeDays: number): Promise<CostDashboardData> {
  const days = Math.min(90, Math.max(1, rangeDays));
  const budgetRaw = parseFloat(process.env.COORDINATOR_DAILY_BUDGET || '20');
  const dailyBudget = isFinite(budgetRaw) ? budgetRaw : 20;
  // Earliest date Anthropic has any data for the org. Don't paginate before
  // this on the dashboard; it just produces empty rows. Override via env if
  // the floor moves. Mirrors EARLIEST_DATA_DATE in services/anthropic-costs.
  const earliestDate = process.env.CAS_ANTHROPIC_EARLIEST_DATE || '2026-03-15';

  const [dailyResult, buildsResult, tierResult, agentResult, platformResult] = await Promise.all([
    // 1. Daily breakdown (CTE with generate_series for gap-filling)
    pool.query(
      `WITH date_range AS (
        SELECT generate_series(
          GREATEST(
            (CURRENT_DATE - ($1::int - 1) * INTERVAL '1 day')::date,
            $2::date
          ),
          CURRENT_DATE::date,
          '1 day'::interval
        )::date AS day
      ),
      build_daily AS (
        SELECT date_trunc('day', completed_at)::date AS day,
          COALESCE(SUM(total_api_cost_usd), 0) AS cost,
          COUNT(*) AS cnt
        FROM builds
        WHERE completed_at >= CURRENT_DATE - $1::int * INTERVAL '1 day'
          AND total_api_cost_usd IS NOT NULL
        GROUP BY 1
      ),
      agent_daily AS (
        SELECT date_trunc('day', completed_at)::date AS day,
          COALESCE(SUM(actual_cost_usd), 0) AS cost,
          COUNT(*) AS cnt
        FROM agent_tasks
        WHERE completed_at >= CURRENT_DATE - $1::int * INTERVAL '1 day'
          AND actual_cost_usd IS NOT NULL
        GROUP BY 1
      )
      SELECT dr.day::text AS date,
        COALESCE(bd.cost, 0)::float AS build_cost,
        COALESCE(ad.cost, 0)::float AS agent_cost,
        (COALESCE(bd.cost, 0) + COALESCE(ad.cost, 0))::float AS total,
        COALESCE(bd.cnt, 0)::int AS build_count
      FROM date_range dr
      LEFT JOIN build_daily bd ON bd.day = dr.day
      LEFT JOIN agent_daily ad ON ad.day = dr.day
      ORDER BY dr.day DESC`,
      [days, earliestDate]
    ),

    // 2. Per-build (recent 100 with cost)
    pool.query(
      `SELECT id, app_name, tier, total_api_cost_usd::float AS cost,
              status, completed_at, cost_breakdown
       FROM builds
       WHERE total_api_cost_usd IS NOT NULL
       ORDER BY completed_at DESC LIMIT 100`
    ),

    // 3. Per-tier
    pool.query(
      `SELECT tier, COUNT(*)::int AS count,
        COALESCE(SUM(total_api_cost_usd), 0)::float AS total_cost,
        COALESCE(AVG(total_api_cost_usd), 0)::float AS avg_cost
      FROM builds WHERE total_api_cost_usd IS NOT NULL
        AND completed_at >= CURRENT_DATE - $1::int * INTERVAL '1 day'
      GROUP BY tier ORDER BY total_cost DESC`,
      [days]
    ),

    // 4. Per-agent
    pool.query(
      `SELECT agent, COUNT(*)::int AS count,
        COALESCE(SUM(actual_cost_usd), 0)::float AS total_cost,
        COALESCE(AVG(actual_cost_usd), 0)::float AS avg_cost
      FROM agent_tasks WHERE actual_cost_usd IS NOT NULL
        AND completed_at >= CURRENT_DATE - $1::int * INTERVAL '1 day'
      GROUP BY agent ORDER BY total_cost DESC`,
      [days]
    ),

    // 5. Platform costs (scraped from Anthropic console, includes Claude Code).
    //    Floor at earliestDate so pre-history rows from any old import don't
    //    leak into the dashboard.
    pool.query(
      `SELECT date::text, total_usd::float
      FROM platform_costs
      WHERE date >= GREATEST(
              CURRENT_DATE - $1::int * INTERVAL '1 day',
              $2::date
            )
      ORDER BY date DESC`,
      [days, earliestDate]
    ),
  ]);

  // Try real Anthropic Admin API first (with DB-backed persistent cache so
  // historical days survive restart). Fall back to scraped platform_costs
  // (source != 'anthropic_api') if the API has never succeeded.
  const apiResult = await getAnthropicCostsAndStatus(days, pool);
  const apiCosts = apiResult.rows;
  const platformByDate = new Map<string, number>();
  let platformCostsPartial = apiResult.partial;
  let platformCostsPartialReason = apiResult.partialReason;

  if (apiCosts) {
    for (const entry of apiCosts) {
      platformByDate.set(entry.date, entry.total_usd);
    }
  } else {
    // Fallback: use scraped platform_costs table
    for (const row of platformResult.rows) {
      platformByDate.set(row.date, Number(row.total_usd));
    }
    // No live API data; the scraped fallback is by definition stale relative
    // to the Admin API total, so flag the response as partial so callers
    // don't treat the scraped period as authoritative.
    platformCostsPartial = true;
    platformCostsPartialReason =
      platformCostsPartialReason || 'admin_api_unavailable_using_scraped_platform_costs';
  }

  // Enrich daily rows with platform + untracked costs.
  // Anthropic console total (`platform_usd`) is the authoritative day spend
  // and already includes every CAS-tracked build + agent call. So the
  // daily total is NOT build + agent + platform (that double-counts the
  // tracked portion); it's the larger of "what we tracked" vs "what
  // Anthropic billed" so we capture both untracked spend AND any case
  // where CAS over-estimates relative to Anthropic billing reconciliation.
  // Untracked = max(0, platform - tracked) is the orphan spend that
  // platform shows but we couldn't attribute to a build or agent task.
  const daily: CostDailyRow[] = dailyResult.rows.map((r: { date: string; build_cost: number; agent_cost: number; total: number; build_count: number }) => {
    const platformUsd = platformByDate.get(r.date) || 0;
    const tracked = Number(r.build_cost) + Number(r.agent_cost);
    return {
      ...r,
      platform_usd: platformUsd,
      untracked: Math.max(0, platformUsd - tracked),
    };
  });

  // Day total: max of tracked vs platform, never their sum (avoids
  // double-counting since platform_usd already includes the tracked
  // build/agent calls).
  for (const row of daily) {
    const tracked = Number(row.build_cost) + Number(row.agent_cost);
    row.total = Math.max(tracked, row.platform_usd);
  }

  const todayStr = new Date().toISOString().slice(0, 10);
  const todayRow = daily.find(r => r.date === todayStr);
  const todayBuild = todayRow ? Number(todayRow.build_cost) : 0;
  const todayAgent = todayRow ? Number(todayRow.agent_cost) : 0;
  const todayPlatform = todayRow ? todayRow.platform_usd : 0;
  const todayUntracked = todayRow ? todayRow.untracked : 0;
  // todayTotal mirrors the per-row total formula: max of tracked vs platform,
  // never their sum. Platform spend already includes the tracked build+agent
  // calls so summing them double-counts.
  const todayTotal = todayRow ? Number(todayRow.total) : Math.max(todayBuild + todayAgent, todayPlatform);
  const periodTotal = daily.reduce((s, r) => s + r.total, 0);
  const periodBuildTotal = daily.reduce((s, r) => s + Number(r.build_cost), 0);
  const periodPlatform = daily.reduce((s, r) => s + r.platform_usd, 0);
  const periodUntracked = daily.reduce((s, r) => s + r.untracked, 0);
  const totalBuilds = daily.reduce((s, r) => s + Number(r.build_count), 0);
  const avgPerBuild = totalBuilds > 0 ? periodBuildTotal / totalBuilds : 0;
  const daysWithPlatform = daily.filter(r => r.platform_usd > 0).length;
  const avgPlatformPerDay = daysWithPlatform > 0 ? periodPlatform / daysWithPlatform : 0;

  return {
    summary: {
      today_build: todayBuild,
      today_agent: todayAgent,
      today_total: todayTotal,
      today_platform: todayPlatform,
      today_untracked: todayUntracked,
      period_total: periodTotal,
      period_build_total: periodBuildTotal,
      period_platform: periodPlatform,
      period_untracked: periodUntracked,
      avg_per_build: avgPerBuild,
      avg_platform_per_day: avgPlatformPerDay,
      daily_budget: dailyBudget,
    },
    daily,
    builds: buildsResult.rows.map((b: any) => ({
      ...b,
      cost_breakdown: b.cost_breakdown || {},
    })),
    per_tier: tierResult.rows,
    per_agent: agentResult.rows,
    platform_costs_partial: platformCostsPartial,
    platform_costs_partial_reason: platformCostsPartialReason,
  };
}

// Admin cost dashboard
router.get('/costs', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const range = Math.min(90, Math.max(1, parseInt(req.query.range as string, 10) || 30));
    const data = await getCostData(range);
    res.render('admin-costs', { activeTab: 'costs',
      title: 'Admin - Costs',
      ...data,
      range,
    });
  } catch (err) {
    console.error('Error loading admin costs:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load costs.' });
  }
});

// JSON API for cost dashboard polling
router.get('/api/costs', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const range = Math.min(90, Math.max(1, parseInt(req.query.range as string, 10) || 30));
    const data = await getCostData(range);
    res.json(data);
  } catch (err) {
    console.error('Error fetching admin costs API:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch costs.' });
  }
});

// Record platform cost (scraped from Anthropic console or entered manually)
router.post('/api/costs/platform', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  try {
    const { date, total_usd } = req.body as { date: string; total_usd: number };

    // Validate date format YYYY-MM-DD
    if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      res.status(400).json({ error: 'Invalid date format. Use YYYY-MM-DD.' });
      return;
    }

    const cost = parseFloat(String(total_usd));
    if (!isFinite(cost) || cost < 0) {
      res.status(400).json({ error: 'total_usd must be a non-negative number.' });
      return;
    }

    await pool.query(
      `INSERT INTO platform_costs (date, total_usd)
       VALUES ($1, $2)
       ON CONFLICT (date, source) DO UPDATE SET total_usd = $2, created_at = NOW()`,
      [date, cost]
    );

    res.json({ ok: true, date, total_usd: cost });
  } catch (err) {
    console.error('Error recording platform cost:', (err as Error).message);
    res.status(500).json({ error: 'Failed to record platform cost.' });
  }
});

// Force-rebuild the Anthropic API cost cache. Wipes platform_costs rows
// where source='anthropic_api' and the in-memory cache, then refetches
// the full 90d window from the Anthropic Admin API and persists.
// Use when historical cached data is suspected wrong, or after a schema
// change to the by_model/fetched_at columns.
router.post('/api/costs/anthropic-rebuild', requireAdmin, validateCsrfToken, async (_req: Request, res: Response): Promise<void> => {
  try {
    const result = await rebuildAnthropicCostsCache(pool);
    if (!result.success) {
      res.status(502).json({ ok: false, error: result.error || 'Rebuild failed' });
      return;
    }
    res.json({ ok: true, rowsFetched: result.rowsFetched });
  } catch (err) {
    console.error('Error rebuilding Anthropic cost cache:', (err as Error).message);
    res.status(500).json({ ok: false, error: 'Rebuild failed' });
  }
});

// Delete a build entirely.
// If the build has a Railway project attached, delete it FIRST. Never leave CAS rows
// pointing at an orphaned Railway project; never leave a Railway project unreferenced.
//
// Projects the delete route must never touch (infra and live-customer
// projects) come from PROTECTED_RAILWAY_PROJECT_IDS, a comma-separated list of
// Railway project UUIDs. Read per request so the list can change without a
// rebuild. Unset means no project is protected, so set it wherever
// CAS_ALLOW_RAILWAY_WRITES=true.
//
// The project this server itself runs in (RAILWAY_PROJECT_ID, injected by
// Railway into every service) is always protected, whatever the env list says:
// a build row that points at the platform's own project (a bad import, a
// production database copied into staging) must never turn this route into a
// way to delete the platform.
function getProtectedRailwayProjectIds(): Set<string> {
  const ids = (process.env.PROTECTED_RAILWAY_PROJECT_IDS || '')
    .split(',')
    .map((id) => id.trim())
    .filter((id) => id.length > 0);
  const ownProject = (process.env.RAILWAY_PROJECT_ID || '').trim();
  if (ownProject) ids.push(ownProject);
  return new Set(ids);
}

// The key a project name is compared on. The worker names a project from the
// build's app name, with each run of characters other than letters, digits and
// hyphens turned into one hyphen and the result cut to 50 characters
// (cli/deploy.py sanitize_app_name). The build row keeps the name as it was
// typed, so both sides are reduced to that form, with letters lowercased (the
// compose spec writes the project name in lowercase), before they are compared.
// An empty key matches nothing.
function railwayProjectNameKey(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 50);
}

function railwayProjectNameMatches(appName: string | null, railwayName: string): boolean {
  if (!appName) return false;
  const key = railwayProjectNameKey(appName);
  return key.length > 0 && key === railwayProjectNameKey(railwayName);
}

router.post('/builds/:buildId/delete', requireAdmin, validateCsrfToken, async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const client = await pool.connect();
  try {
    const lookup = await client.query<{ railway_project_id: string | null; app_name: string | null }>(
      'SELECT railway_project_id, app_name FROM builds WHERE id = $1',
      [buildId],
    );
    if (lookup.rows.length === 0) {
      return renderErrorPage(res, 404, { title: 'Error', error: 'Build not found.' });
    }
    const { railway_project_id: rwProjectId, app_name: appName } = lookup.rows[0];

    if (rwProjectId && isRailwayWriteEnabled()) {
      const protectedIds = getProtectedRailwayProjectIds();
      if (protectedIds.size === 0) {
        console.warn('Admin delete: Railway writes are enabled but PROTECTED_RAILWAY_PROJECT_IDS and RAILWAY_PROJECT_ID are both unset; no project is protected');
      }
      if (protectedIds.has(rwProjectId)) {
        console.error('Admin delete blocked: protected Railway project %s / %s (build %s)', appName, rwProjectId, buildId);
        const rwLabel = appName ? `${appName} (${rwProjectId})` : rwProjectId;
        return renderErrorPage(res, 403, {
          title: 'Protected project',
          error: `Refusing to delete build ${buildId}: Railway project ${rwLabel} is protected. Remove the protection entry if this is intentional.`,
        });
      }
      // The project is named back before anything is deleted: a stored id that
      // now points at another project, or a token that lost access, must not
      // lead to a delete. A name that cannot be read, or that does not match the
      // build's app name, keeps both the project and the row.
      const rwLabel = appName ? `${appName} (${rwProjectId})` : rwProjectId;
      const railwayName = await readRailwayProjectName(rwProjectId).catch((err: unknown) => {
        console.error('Railway project name could not be read for build %s (project %s): %s', buildId, rwProjectId, err instanceof Error ? err.message : String(err));
        return null;
      });
      if (railwayName === null || !railwayProjectNameMatches(appName, railwayName)) {
        console.error('Admin delete blocked: Railway project %s does not match build %s (app %s)', rwProjectId, buildId, appName ?? '(unnamed)');
        return res.status(502).render('500', {
          title: 'Railway delete failed',
          error: `Could not confirm that Railway project ${rwLabel} belongs to this build, so nothing was deleted. The CAS build row was NOT deleted. Check the project in Railway, then retry.`,
        });
      }

      try {
        await deleteRailwayProject(rwProjectId);
        console.log('Admin deleted Railway project %s / %s for build %s', appName, rwProjectId, buildId);
      } catch (err) {
        if (err instanceof RailwayApiError && err.alreadyGone) {
          console.log('Railway project %s / %s already gone; proceeding with CAS delete for build %s', appName, rwProjectId, buildId);
        } else {
          const message = err instanceof Error ? err.message : String(err);
          console.error('Railway delete failed for build %s (project %s / %s): %s', buildId, appName, rwProjectId, message);
          return res.status(502).render('500', {
            title: 'Railway delete failed',
            error: `Could not delete Railway project ${rwLabel}. The CAS build row was NOT deleted so you can retry. Details: ${message}`,
          });
        }
      }
    } else if (rwProjectId) {
      console.log('Admin delete: Railway writes disabled in this environment; skipping Railway project delete for %s / %s (build %s) and proceeding with CAS-only cascade', appName, rwProjectId, buildId);
    } else {
      console.log('Admin delete: build %s (%s) has no Railway project attached; CAS-only cascade', buildId, appName ?? '(unnamed)');
    }

    await client.query('BEGIN');
    await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = $1', [buildId]);
    await client.query('DELETE FROM feature_messages WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE build_id = $1)', [buildId]);
    await client.query('DELETE FROM feature_requests WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM intake_messages WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM build_logs WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM dead_letter_builds WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM builds WHERE id = $1', [buildId]);
    await client.query('COMMIT');
    console.log('Admin deleted build %s (%s, rwProjectId=%s)', buildId, appName ?? '(unnamed)', rwProjectId ?? 'none');
    res.redirect('/admin/builds');
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('Error deleting build:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to delete build.' });
  } finally {
    client.release();
  }
});

// ---- Coordinator Task History ----

const COORDINATOR_PAGE_SIZE = 20;

type CoordinatorStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';

interface CoordinatorFilters {
  agent: string;
  task_category: string;
  status: string;
  date_from: string;
  date_to: string;
}

interface CoordinatorTaskRow {
  id: string;
  tick_id: string | null;
  agent: string;
  task_category: string;
  task_description: string;
  status: string;
  actual_cost_usd: number | null;
  created_at: string;
  completed_at: string | null;
  duration_s: number | null;
}

interface CoordinatorStats {
  total: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  skipped: number;
}

interface CoordinatorData {
  tasks: CoordinatorTaskRow[];
  stats: CoordinatorStats;
  total: number;
  agents: string[];
  categories: string[];
}

export async function getCoordinatorData(
  page: number,
  filters: CoordinatorFilters
): Promise<CoordinatorData> {
  const offset = (page - 1) * COORDINATOR_PAGE_SIZE;
  const conditions: string[] = [];
  const params: (string | number)[] = [];

  if (filters.agent) {
    params.push(filters.agent);
    conditions.push(`agent = $${params.length}`);
  }
  if (filters.task_category) {
    params.push(filters.task_category);
    conditions.push(`task_category = $${params.length}`);
  }
  if (filters.status) {
    params.push(filters.status);
    conditions.push(`status = $${params.length}`);
  }
  if (filters.date_from) {
    params.push(filters.date_from);
    conditions.push(`created_at >= $${params.length}::date`);
  }
  if (filters.date_to) {
    params.push(filters.date_to);
    conditions.push(`created_at < ($${params.length}::date + INTERVAL '1 day')`);
  }

  const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

  const [countResult, dataResult, statsResult, agentsResult, categoriesResult] = await Promise.all([
    pool.query(`SELECT COUNT(*) AS total FROM agent_tasks ${whereClause}`, [...params]),
    pool.query(
      `SELECT id, tick_id, agent, task_category, task_description, status,
              actual_cost_usd::float, created_at, completed_at,
              CASE WHEN completed_at IS NOT NULL AND created_at IS NOT NULL
                THEN EXTRACT(EPOCH FROM (completed_at - created_at))::float
                ELSE NULL
              END AS duration_s
       FROM agent_tasks
       ${whereClause}
       ORDER BY created_at DESC
       LIMIT $${params.length + 1} OFFSET $${params.length + 2}`,
      [...params, COORDINATOR_PAGE_SIZE, offset]
    ),
    pool.query(`
      SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE status = 'pending') AS pending,
        COUNT(*) FILTER (WHERE status = 'running') AS running,
        COUNT(*) FILTER (WHERE status = 'completed') AS completed,
        COUNT(*) FILTER (WHERE status = 'failed') AS failed,
        COUNT(*) FILTER (WHERE status = 'skipped') AS skipped
      FROM agent_tasks
    `),
    pool.query(`SELECT DISTINCT agent FROM agent_tasks ORDER BY agent`),
    pool.query(`SELECT DISTINCT task_category FROM agent_tasks ORDER BY task_category`),
  ]);

  const total = parseInt(countResult.rows[0].total as string, 10);
  const stats: CoordinatorStats = {
    total: parseInt(statsResult.rows[0].total as string, 10) || 0,
    pending: parseInt(statsResult.rows[0].pending as string, 10) || 0,
    running: parseInt(statsResult.rows[0].running as string, 10) || 0,
    completed: parseInt(statsResult.rows[0].completed as string, 10) || 0,
    failed: parseInt(statsResult.rows[0].failed as string, 10) || 0,
    skipped: parseInt(statsResult.rows[0].skipped as string, 10) || 0,
  };

  return {
    tasks: dataResult.rows,
    stats,
    total,
    agents: agentsResult.rows.map((r: { agent: string }) => r.agent),
    categories: categoriesResult.rows.map((r: { task_category: string }) => r.task_category),
  };
}

// Admin coordinator task history page
router.get('/coordinator', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
    const filters: CoordinatorFilters = {
      agent: (req.query.agent as string) || '',
      task_category: (req.query.task_category as string) || '',
      status: (req.query.status as string) || '',
      date_from: (req.query.date_from as string) || '',
      date_to: (req.query.date_to as string) || '',
    };

    // Validate date formats
    const dateRe = /^\d{4}-\d{2}-\d{2}$/;
    if (filters.date_from && !dateRe.test(filters.date_from)) filters.date_from = '';
    if (filters.date_to && !dateRe.test(filters.date_to)) filters.date_to = '';

    const data = await getCoordinatorData(page, filters);
    res.render('admin-coordinator', { activeTab: 'coordinator',
      title: 'Admin - Coordinator',
      ...data,
      page,
      filters,
      pageSize: COORDINATOR_PAGE_SIZE,
    });
  } catch (err) {
    console.error('Error loading coordinator tasks:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load coordinator tasks.' });
  }
});

// JSON API for coordinator polling
router.get('/api/coordinator', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
    const filters: CoordinatorFilters = {
      agent: (req.query.agent as string) || '',
      task_category: (req.query.task_category as string) || '',
      status: (req.query.status as string) || '',
      date_from: (req.query.date_from as string) || '',
      date_to: (req.query.date_to as string) || '',
    };

    const dateRe = /^\d{4}-\d{2}-\d{2}$/;
    if (filters.date_from && !dateRe.test(filters.date_from)) filters.date_from = '';
    if (filters.date_to && !dateRe.test(filters.date_to)) filters.date_to = '';

    const data = await getCoordinatorData(page, filters);
    res.json(data);
  } catch (err) {
    console.error('Error fetching coordinator API:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch coordinator tasks.' });
  }
});

// ---- Founder Approval Requests ----

interface ApprovalRequest {
  id: string;
  type: string;
  description: string;
  context: Record<string, unknown>;
  status: string;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
}

// Helper: count pending approvals (used to add badge to nav)
export async function getPendingApprovalCount(): Promise<number> {
  const result = await pool.query(
    `SELECT COUNT(*) AS count FROM approval_requests WHERE status = 'pending'`
  );
  return parseInt(result.rows[0].count as string, 10) || 0;
}

// Admin approvals page
router.get('/approvals', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const statusFilter = (req.query.status as string) || 'pending';
    const validStatuses = ['pending', 'approved', 'rejected', 'all'];
    const filter = validStatuses.includes(statusFilter) ? statusFilter : 'pending';

    const conditions: string[] = [];
    const params: (string | number)[] = [];
    if (filter !== 'all') {
      params.push(filter);
      conditions.push(`status = $${params.length}`);
    }
    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [itemsResult, statsResult] = await Promise.all([
      pool.query(
        `SELECT id, type, description, context, status, created_at, resolved_at, resolved_by
         FROM approval_requests
         ${whereClause}
         ORDER BY created_at DESC
         LIMIT 100`,
        params
      ),
      pool.query(`
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE status = 'pending') AS pending,
          COUNT(*) FILTER (WHERE status = 'approved') AS approved,
          COUNT(*) FILTER (WHERE status = 'rejected') AS rejected
        FROM approval_requests
      `),
    ]);

    const pendingCount = parseInt(statsResult.rows[0].pending as string, 10) || 0;

    res.render('admin-approvals', { activeTab: 'approvals',
      title: 'Admin - Approvals',
      items: itemsResult.rows,
      stats: {
        total: parseInt(statsResult.rows[0].total as string, 10) || 0,
        pending: pendingCount,
        approved: parseInt(statsResult.rows[0].approved as string, 10) || 0,
        rejected: parseInt(statsResult.rows[0].rejected as string, 10) || 0,
      },
      statusFilter: filter,
      pendingApprovalCount: pendingCount,
    });
  } catch (err) {
    console.error('Error loading approvals:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load approvals.' });
  }
});

// Approve a single request
router.post('/approvals/:id/approve', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  try {
    const { id } = req.params;
    const result = await pool.query(
      `UPDATE approval_requests
         SET status = 'approved', resolved_at = NOW(), resolved_by = $2
       WHERE id = $1 AND status = 'pending'
       RETURNING id`,
      [id, req.session.customerEmail]
    );
    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Approval request not found or already resolved.' });
      return;
    }
    console.log('Admin approved request %s', id);
    res.redirect('/admin/approvals');
  } catch (err) {
    console.error('Error approving request:', (err as Error).message);
    res.status(500).json({ error: 'Failed to approve request.' });
  }
});

// Reject a single request
router.post('/approvals/:id/reject', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  try {
    const { id } = req.params;
    const result = await pool.query(
      `UPDATE approval_requests
         SET status = 'rejected', resolved_at = NOW(), resolved_by = $2
       WHERE id = $1 AND status = 'pending'
       RETURNING id`,
      [id, req.session.customerEmail]
    );
    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Approval request not found or already resolved.' });
      return;
    }
    console.log('Admin rejected request %s', id);
    res.redirect('/admin/approvals');
  } catch (err) {
    console.error('Error rejecting request:', (err as Error).message);
    res.status(500).json({ error: 'Failed to reject request.' });
  }
});

// Batch approve/reject
router.post('/approvals/batch', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  try {
    const { ids, action } = req.body as { ids: string | string[]; action: string };

    if (!action || !['approve', 'reject'].includes(action)) {
      res.status(400).json({ error: 'Invalid action. Must be approve or reject.' });
      return;
    }

    // Support both array and comma-separated string
    let idList: string[];
    if (Array.isArray(ids)) {
      idList = ids;
    } else if (typeof ids === 'string') {
      idList = ids.split(',').map(s => s.trim()).filter(Boolean);
    } else {
      res.status(400).json({ error: 'ids must be an array or comma-separated string.' });
      return;
    }

    if (idList.length === 0 || idList.length > 10) {
      res.status(400).json({ error: 'Provide between 1 and 10 IDs.' });
      return;
    }

    const newStatus = action === 'approve' ? 'approved' : 'rejected';
    const result = await pool.query(
      `UPDATE approval_requests
         SET status = $1, resolved_at = NOW(), resolved_by = $2
       WHERE id = ANY($3::uuid[]) AND status = 'pending'
       RETURNING id`,
      [newStatus, req.session.customerEmail, idList]
    );

    console.log('Admin batch %s %d request(s)', action, result.rowCount);
    res.redirect('/admin/approvals');
  } catch (err) {
    console.error('Error batch processing approvals:', (err as Error).message);
    res.status(500).json({ error: 'Failed to process batch.' });
  }
});

// ---- Senior Web Craftsman Review Queue (section 04) ----
//
// These five routes power Ocean's review queue. They mutate
// builds.review_status / reviewed_at / reviewed_by / review_notes but do NOT
// touch builds.status; the customer is still the one who flips preview to
// live via POST /builds/:buildId/approve (section 03). A customer approve
// only succeeds when review_status = 'approved'.

interface ReviewQueueRow {
  build_id: string;
  app_name: string | null;
  tier: string | null;
  status: string | null;
  review_status: 'pending' | 'approved' | 'changes_requested' | null;
  reviewed_at: Date | null;
  reviewed_by: string | null;
  review_notes: string | null;
  preview_url: string | null;
  created_at: Date | null;
  updated_at: Date | null;
  screenshots: unknown;
  customer_email: string | null;
}

interface ReviewStats {
  pending_count: number;
  reviewed_today: number;
  total_reviewed: number;
}

/**
 * Senior Web Craftsman review queue list page.
 *
 * Query strategy: single SELECT with ORDER BY priority so pending rows come
 * first, then changes_requested, then approved-preview, then the rest. Stats
 * header (pending_count, reviewed_today, total_reviewed) is computed in JS
 * from a lightweight COUNT query.
 *
 * View locals: builds, pending_count, reviewed_today, total_reviewed.
 */
router.get('/reviews', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const [buildsResult, statsResult] = await Promise.all([
      pool.query<ReviewQueueRow>(
        `SELECT b.id AS build_id, b.app_name, b.tier, b.status, b.review_status,
                b.reviewed_at, b.reviewed_by, b.review_notes, b.preview_url,
                b.created_at, b.updated_at, b.screenshots,
                c.email AS customer_email
           FROM builds b
           LEFT JOIN customers c ON c.id = b.customer_id
          WHERE b.status IN ('preview', 'live')
          ORDER BY
            CASE b.review_status
              WHEN 'pending' THEN 0
              WHEN 'changes_requested' THEN 1
              WHEN 'approved' THEN 2
              ELSE 3
            END,
            b.updated_at DESC NULLS LAST
          LIMIT 200`
      ),
      pool.query(
        `SELECT
           COUNT(*) FILTER (WHERE review_status = 'pending') AS pending_count,
           COUNT(*) FILTER (WHERE reviewed_at >= CURRENT_DATE) AS reviewed_today,
           COUNT(*) FILTER (WHERE reviewed_at IS NOT NULL) AS total_reviewed
         FROM builds`
      ),
    ]);

    const statsRow = (statsResult.rows[0] ?? {}) as Record<string, string | number | null>;
    const stats: ReviewStats = {
      pending_count: parseInt(String(statsRow.pending_count ?? '0'), 10) || 0,
      reviewed_today: parseInt(String(statsRow.reviewed_today ?? '0'), 10) || 0,
      total_reviewed: parseInt(String(statsRow.total_reviewed ?? '0'), 10) || 0,
    };

    res.render('admin-reviews', {
      activeTab: 'reviews',
      title: 'Admin - Senior Web Craftsman Reviews',
      builds: buildsResult.rows,
      pending_count: stats.pending_count,
      reviewed_today: stats.reviewed_today,
      total_reviewed: stats.total_reviewed,
      pendingReviewCount: stats.pending_count,
    });
  } catch (err) {
    console.error('Error loading review queue:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load review queue.' });
  }
});

// ---------------------------------------------------------------------------
// The build's warnings, as the review page shows them
// ---------------------------------------------------------------------------
//
// The worker's lint copies two kinds of line from the build's manifest into
// build_logs, stage 'testing' (queue/worker_pg.py, _manifest_warning_checks):
//
//   "  warning: <reason>: <detail>"        written by the build engine about
//                                          its own decisions: a model whose
//                                          rows are public, a protected page
//                                          it did not emit, what it dropped;
//   "  spec-warning: <reason>: <detail>"   text the spec carried, copied
//   "  spec-warning: <text>"               through. Whoever shaped the spec
//                                          wrote it; it is not a finding.
//
// The first 50 lines are one log entry each. The rest are packed, up to 100
// lines to an entry, joined by newlines, so one entry can hold a hundred
// warnings. Engine lines come before spec lines, the engine's publication
// warnings first. A line longer than 1,200 characters ends in " ...(cut)".
// The lint also writes "LINT FAIL: ..." entries, among them the two that say
// the list in the log is incomplete.
//
// The reviewer is the reader these lines exist for, so the page shows every
// one of them: entries are split on newlines, a line is classed by its own
// prefix and by nothing else, and no line is dropped for having a shape the
// parser did not expect. The two kinds are kept apart all the way to the
// page, because a spec line is attacker-influenced text: under its own
// heading it cannot pass for something the engine said.
//
// Which entries: those of the latest lint run only. A build requeued in place
// is linted again under the same build id, and the earlier run's lines are
// about an attempt that is gone. A run starts with the entry
// BUILD_LINT_RUN_MARKER.
//
// Bounds: the worker reads at most 256 KB of manifest per run, which is at
// most about 310 entries; BUILD_WARNING_LOG_ENTRY_LIMIT is above that, and
// when it is reached anyway the page says the list is cut.

/** The start of the entry the worker writes when a lint run begins (queue/worker_pg.py, _run_pipeline). */
export const BUILD_LINT_RUN_MARKER = 'Running v2 build-output lint';
export const BUILD_WARNING_LOG_ENTRY_LIMIT = 400;
const ENGINE_WARNING_PREFIX = 'warning: ';
const SPEC_WARNING_PREFIX = 'spec-warning: ';
const LINT_FAIL_PREFIX = 'LINT FAIL: ';
/** The engine caps a reason at 80 characters; a longer "reason" is not one. */
const ENGINE_WARNING_REASON_MAX = 80;

/** The log entries the review page reads: this build's warning and lint-failure entries since its latest lint run began, oldest first. */
export const BUILD_WARNING_LOG_SQL = `SELECT message
           FROM build_logs
          WHERE build_id = $1
            AND stage = 'testing'
            AND id > COALESCE((SELECT MAX(id) FROM build_logs
                                WHERE build_id = $1 AND stage = 'testing'
                                  AND message LIKE '${BUILD_LINT_RUN_MARKER}%'), 0)
            AND (message ~ '^\\s*(spec-)?warning: ' OR message LIKE '${LINT_FAIL_PREFIX}%')
          ORDER BY id ASC
          LIMIT ${BUILD_WARNING_LOG_ENTRY_LIMIT}`;

export interface BuildWarningReport {
  /** What the engine wrote, in the order it wrote it. `reason` is empty when the line had none. */
  engine: Array<{ reason: string; detail: string }>;
  /** What the spec carried, whole and unparsed. */
  fromSpec: string[];
  /** The lint's failure lines, without their prefix. */
  lintFailures: string[];
  /** True when as many entries came back as the query's limit allows: there may be more. */
  truncated: boolean;
}

/**
 * Sort the entries BUILD_WARNING_LOG_SQL returns into what the engine said,
 * what the spec carried and what the lint failed on. Exact repeats are shown
 * once. Linear in the text.
 */
export function readBuildWarningLog(messages: readonly unknown[]): BuildWarningReport {
  const report: BuildWarningReport = {
    engine: [],
    fromSpec: [],
    lintFailures: [],
    truncated: messages.length >= BUILD_WARNING_LOG_ENTRY_LIMIT,
  };
  const seen = new Set<string>();
  for (const message of messages) {
    if (typeof message !== 'string') continue;
    for (const rawLine of message.split('\n')) {
      const line = rawLine.trim();
      if (line === '' || seen.has(line)) continue;
      if (line.startsWith(ENGINE_WARNING_PREFIX)) {
        seen.add(line);
        const body = line.slice(ENGINE_WARNING_PREFIX.length);
        const split = body.indexOf(': ');
        report.engine.push(
          split > 0 && split <= ENGINE_WARNING_REASON_MAX
            ? { reason: body.slice(0, split), detail: body.slice(split + 2) }
            : { reason: '', detail: body }
        );
      } else if (line.startsWith(SPEC_WARNING_PREFIX)) {
        seen.add(line);
        report.fromSpec.push(line.slice(SPEC_WARNING_PREFIX.length));
      } else if (line.startsWith(LINT_FAIL_PREFIX)) {
        seen.add(line);
        report.lintFailures.push(line.slice(LINT_FAIL_PREFIX.length));
      }
    }
  }
  return report;
}

/**
 * Review detail page. Information-dense single screen per plan section 6.2:
 * no tabs, no drill-downs. Ocean decides Approve or Request Changes from
 * this one view.
 *
 * View locals include: build row (full columns), customer row (email, name),
 * build_logs tail (last 20 entries), screenshots array from builds.screenshots
 * JSONB, spec JSONB, preview_url, csrfToken.
 *
 * Responds 404 if the buildId does not exist.
 */
router.get('/reviews/:buildId', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const { buildId } = req.params;
    const buildResult = await pool.query(
      `SELECT id, app_name, tier, status, review_status, reviewed_at, reviewed_by,
              review_notes, preview_url, live_url, spec, screenshots, customer_id,
              created_at, updated_at
         FROM builds
        WHERE id = $1`,
      [buildId]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).render('404', { title: 'Not Found' });
      return;
    }

    const build = buildResult.rows[0];

    const [customerResult, logsResult, warningsResult] = await Promise.all([
      build.customer_id
        ? pool.query(
            'SELECT id, email, name FROM customers WHERE id = $1',
            [build.customer_id]
          )
        : Promise.resolve({ rows: [] } as { rows: unknown[] }),
      pool.query(
        `SELECT stage, message, created_at
           FROM build_logs
          WHERE build_id = $1
          ORDER BY id DESC
          LIMIT 20`,
        [buildId]
      ),
      // The build's warnings, read apart from the log tail above so that
      // none falls off its end: see "The build's warnings, as the review
      // page shows them" above.
      pool.query(BUILD_WARNING_LOG_SQL, [buildId]),
    ]);

    const customer = customerResult.rows[0] ?? null;
    const buildWarnings = readBuildWarningLog(
      (warningsResult.rows as Array<{ message?: unknown }>).map((row) => row.message)
    );

    res.render('admin-reviews-detail', {
      activeTab: 'reviews',
      title: `Review: ${build.app_name ?? buildId}`,
      build,
      customer,
      logs: logsResult.rows,
      buildWarnings,
      buildWarningEntryLimit: BUILD_WARNING_LOG_ENTRY_LIMIT,
      error: null,
    });
  } catch (err) {
    console.error('Error loading review detail:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load review detail.' });
  }
});

/**
 * Records a Senior Web Craftsman approval on a build.
 *
 * Updates: review_status='approved', reviewed_at=NOW(),
 *          reviewed_by=<session.customerEmail>, review_notes=NULL
 * (clears any prior changes_requested notes).
 *
 * Does NOT flip status to 'live'; that is the customer's step. Section 03's
 * POST /builds/:buildId/approve reads review_status='approved' as its gate.
 *
 * Side effect: fires sendReviewReadyEmail to the customer. Fire-and-forget;
 * email failures must not block or 500 the response.
 *
 * On success: 302 redirect to /admin/reviews.
 * On DB error: 500; email NOT fired.
 * On unknown build_id: 404.
 */
router.post(
  '/reviews/:buildId/approve',
  requireAdmin,
  validateCsrfToken,
  async (req: Request, res: Response): Promise<void> => {
    try {
      const { buildId } = req.params;
      // The CTE reads the previous review_status under a row lock so a
      // double-click (or two admin tabs) approves once and mails the customer
      // once: the second request sees 'approved' already and sends nothing.
      const result = await pool.query(
        `WITH prev AS (
           SELECT id, review_status FROM builds WHERE id = $2 FOR UPDATE
         )
         UPDATE builds b
            SET review_status = 'approved',
                reviewed_at = NOW(),
                reviewed_by = $1,
                review_notes = NULL,
                updated_at = NOW()
           FROM prev
          WHERE b.id = prev.id
          RETURNING b.id, b.app_name, b.customer_id,
                    (prev.review_status = 'approved') AS was_already_approved`,
        [req.session.customerEmail, buildId]
      );

      if (result.rows.length === 0) {
        res.status(404).render('404', { title: 'Not Found' });
        return;
      }

      const updated = result.rows[0] as {
        id: string;
        app_name: string | null;
        customer_id: string | null;
        was_already_approved?: boolean;
      };

      // Look up the customer email for the notification. Missing customer is
      // handled gracefully: the approval still succeeds; we just skip the
      // email because there is no one to notify.
      let customerEmail: string | null = null;
      if (updated.customer_id) {
        try {
          const customerResult = await pool.query<{ email: string }>(
            'SELECT email FROM customers WHERE id = $1',
            [updated.customer_id]
          );
          customerEmail = customerResult.rows[0]?.email ?? null;
        } catch (lookupErr) {
          console.error('Review approve: customer lookup failed', (lookupErr as Error).message);
        }
      }

      if (customerEmail && !updated.was_already_approved) {
        // Fire-and-forget; email failures must never block the DB write.
        sendReviewReadyEmail(customerEmail, updated.app_name ?? '', updated.id).catch(
          (err: unknown) =>
            console.error('sendReviewReadyEmail failed', (err as Error).message)
        );
      }

      console.log('Admin approved review for build %s', updated.id);
      res.redirect('/admin/reviews');
    } catch (err) {
      console.error('Error approving review:', (err as Error).message);
      res.status(500).render('500', { title: 'Error', error: 'Failed to record approval.' });
    }
  }
);

/**
 * Records a "changes requested" decision with notes.
 *
 * Requires: non-empty review_notes in the form body (min 1 char after trim).
 *
 * Updates: review_status='changes_requested', reviewed_at=NOW(),
 *          reviewed_by=<session.customerEmail>, review_notes=<body>
 *
 * Side effect: fires sendReviewChangesRequestedEmail with notes.
 * Fire-and-forget; email failures must not block or 500 the response.
 *
 * On success: 302 redirect to /admin/reviews.
 * On empty/whitespace notes: 400.
 * On unknown build_id: 404.
 */
router.post(
  '/reviews/:buildId/request-changes',
  requireAdmin,
  validateCsrfToken,
  async (req: Request, res: Response): Promise<void> => {
    try {
      const { buildId } = req.params;
      const rawNotes =
        typeof req.body.review_notes === 'string' ? req.body.review_notes : '';
      const notes = rawNotes.trim();
      if (notes.length === 0) {
        renderErrorPage(res, 400, {
          title: 'Notes required',
          error: 'Notes are required when requesting changes.',
        });
        return;
      }

      const result = await pool.query(
        `UPDATE builds
            SET review_status = 'changes_requested',
                reviewed_at = NOW(),
                reviewed_by = $1,
                review_notes = $2,
                updated_at = NOW()
          WHERE id = $3
          RETURNING id, app_name, customer_id`,
        [req.session.customerEmail, notes, buildId]
      );

      if (result.rows.length === 0) {
        res.status(404).render('404', { title: 'Not Found' });
        return;
      }

      const updated = result.rows[0] as {
        id: string;
        app_name: string | null;
        customer_id: string | null;
      };

      let customerEmail: string | null = null;
      if (updated.customer_id) {
        try {
          const customerResult = await pool.query<{ email: string }>(
            'SELECT email FROM customers WHERE id = $1',
            [updated.customer_id]
          );
          customerEmail = customerResult.rows[0]?.email ?? null;
        } catch (lookupErr) {
          console.error(
            'Review request-changes: customer lookup failed',
            (lookupErr as Error).message
          );
        }
      }

      if (customerEmail) {
        sendReviewChangesRequestedEmail(
          customerEmail,
          updated.app_name ?? '',
          updated.id,
          notes
        ).catch((err: unknown) =>
          console.error('sendReviewChangesRequestedEmail failed', (err as Error).message)
        );
      }

      console.log('Admin requested changes on build %s', updated.id);
      res.redirect('/admin/reviews');
    } catch (err) {
      console.error('Error recording changes-requested review:', (err as Error).message);
      res.status(500).render('500', { title: 'Error', error: 'Failed to record review.' });
    }
  }
);

/**
 * JSON polling endpoint for the /admin/reviews auto-refresh loop AND the
 * /admin/builds "Pending Reviews" stat card (single endpoint, two consumers).
 *
 * Powered by the partial index from section 01:
 *   idx_builds_review_pending ON builds(updated_at DESC)
 *     WHERE review_status = 'pending'
 *
 * Polling interval: 5s when any pending, 20s when none (client-side decision).
 */
router.get('/api/reviews', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const [countResult, rowsResult] = await Promise.all([
      pool.query<{ pending_count: number }>(
        `SELECT COUNT(*)::int AS pending_count
           FROM builds
          WHERE review_status = 'pending'`
      ),
      pool.query<{
        build_id: string;
        app_name: string | null;
        customer_email: string | null;
        created_at: Date | null;
      }>(
        `SELECT b.id AS build_id, b.app_name, c.email AS customer_email, b.created_at
           FROM builds b
           LEFT JOIN customers c ON c.id = b.customer_id
          WHERE b.review_status = 'pending'
          ORDER BY b.updated_at DESC NULLS LAST
          LIMIT 5`
      ),
    ]);

    const pendingCount = countResult.rows[0]?.pending_count ?? 0;
    const recentPending = rowsResult.rows.map(row => ({
      buildId: row.build_id,
      appName: row.app_name ?? '',
      customerEmail: row.customer_email ?? '',
      createdAt: row.created_at ? new Date(row.created_at).toISOString() : null,
    }));

    res.set('Cache-Control', 'no-store');
    res.json({
      pending_count: pendingCount,
      recent_pending: recentPending,
    });
  } catch (err) {
    console.error('Error fetching review API data:', (err as Error).message);
    res.status(500).json({ error: 'Failed to load review data.' });
  }
});

// ---- Executor Run History & Questions ----

// GET /admin/executor: executor run history and questions.
//
// Default view requires `roadmap_item_id IS NOT NULL` so loop heartbeats
// (paused / idle / running-with-no-item) never surface unless the operator
// explicitly picks the "Heartbeats" chip. Without the item-attached gate
// most of the table was heartbeat rows with no roadmap item.
//
// Per-status drill-ins (Completed / Failed / Crashed / Review / Working)
// also gate on item-attached: "failed" should mean "item-attempt failed,"
// not "loop fired but couldn't pick an item." The Heartbeats + All chips
// are the only escape hatches that show no-item rows.
//
// Pagination: 25 per page. URL params:
//   ?status=active (default) | all | completed | failed | crashed | review | working | heartbeats
//   ?page=N (1-indexed)
router.get('/executor', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    // Statuses that historically mark a real item-attempt (vs. loop
    // heartbeats). Used by both `active` and per-status filters.
    const ITEM_STATUSES = ['completed', 'failed', 'crashed', 'review', 'working', 'pr_created'];

    const ALLOWED_FILTERS = new Set([
      'active', 'all', 'heartbeats',
      'completed', 'failed', 'crashed', 'review', 'working',
    ]);
    const rawStatus = typeof req.query.status === 'string' ? req.query.status : 'active';
    const statusFilter = ALLOWED_FILTERS.has(rawStatus) ? rawStatus : 'active';

    const PER_PAGE = 25;
    const rawPage = parseInt(String(req.query.page || '1'), 10);
    const page = Number.isFinite(rawPage) && rawPage > 0 ? Math.min(rawPage, 100_000) : 1;
    const offset = (page - 1) * PER_PAGE;

    // Build the WHERE clause for the runs query based on the filter.
    let where = '';
    const params: (string | number)[] = [];
    if (statusFilter === 'active') {
      // Real item activity: any row where the executor actually picked an
      // item, regardless of outcome. Excludes loop heartbeats by definition.
      where = 'WHERE roadmap_item_id IS NOT NULL';
    } else if (statusFilter === 'heartbeats') {
      // Loop diagnostics: rows where the executor never picked an item
      // (paused, idle, running-mid-cycle, etc.).
      where = 'WHERE roadmap_item_id IS NULL';
    } else if (statusFilter === 'all') {
      where = '';
    } else {
      // Per-status drill-in. Item-attached gate prevents loop heartbeats
      // from sneaking into Failed / Crashed counts (which historically
      // include "loop couldn't pick anything" rows that don't represent
      // a real item attempt).
      where = 'WHERE status = $1 AND roadmap_item_id IS NOT NULL';
      params.push(statusFilter);
    }

    // Fetch one page + total count for this filter.
    const countSql = `SELECT COUNT(*) AS n FROM executor_runs ${where}`;
    const countResult = await pool.query(countSql, params);
    const totalRuns = parseInt(countResult.rows[0].n as string, 10) || 0;
    const totalPages = Math.max(1, Math.ceil(totalRuns / PER_PAGE));

    const runsSql = `
      SELECT * FROM executor_runs
      ${where}
      ORDER BY created_at DESC
      LIMIT $${params.length + 1} OFFSET $${params.length + 2}
    `;
    const runs = await pool.query(runsSql, [...params, PER_PAGE, offset]);

    // Per-status counts for chip badges. Group by status, then split each
    // bucket into item-attached vs heartbeat so the chip counts reflect
    // the same item-gate that the WHERE clause applies.
    const statusCountsResult = await pool.query(`
      SELECT status,
             COUNT(*) FILTER (WHERE roadmap_item_id IS NOT NULL) AS item_n,
             COUNT(*) FILTER (WHERE roadmap_item_id IS NULL) AS noitem_n
      FROM executor_runs
      GROUP BY status
    `);
    const statusCounts: Record<string, number> = {
      active: 0, all: 0, heartbeats: 0,
      completed: 0, failed: 0, crashed: 0, review: 0, working: 0,
    };
    for (const row of statusCountsResult.rows) {
      const k = row.status as string;
      const itemN = parseInt(row.item_n as string, 10) || 0;
      const noitemN = parseInt(row.noitem_n as string, 10) || 0;
      statusCounts.all += itemN + noitemN;
      statusCounts.heartbeats += noitemN;
      statusCounts.active += itemN;
      // Per-status chip count reflects ONLY item-attached rows so chip
      // numbers match the drill-in result count.
      if (k in statusCounts && k !== 'all' && k !== 'active' && k !== 'heartbeats') {
        statusCounts[k] = itemN;
      }
    }

    const questions = await pool.query(
      'SELECT * FROM executor_questions ORDER BY created_at DESC LIMIT 20'
    );
    const stats = await pool.query(`
      SELECT
        COUNT(*) FILTER (WHERE status = 'completed' AND created_at::date = CURRENT_DATE) as today_completed,
        COUNT(*) FILTER (WHERE status = 'completed') as total_completed,
        COUNT(*) FILTER (WHERE status = 'failed' AND created_at::date = CURRENT_DATE) as today_failed,
        COALESCE(SUM(cost_usd) FILTER (WHERE created_at::date = CURRENT_DATE), 0) as today_cost
      FROM executor_runs
    `);
    const pendingQuestions = await pool.query(
      "SELECT COUNT(*) FROM executor_questions WHERE status = 'pending'"
    );

    // 2026-05-18: Status banner must reflect TRUE current loop state, not
    // the latest item-attached run (which can be hours stale). Pull both:
    // - heartbeatRun: the absolute newest row (any status, any item). Tells
    //   us if the loop is alive + when it last ran.
    // - latestItemRun: the newest item-attached run. Tells us what the loop
    //   most recently worked on, but is NOT proxy for "what's it doing now."
    const latestRunResult = await pool.query(
      'SELECT * FROM executor_runs ORDER BY created_at DESC LIMIT 1'
    );
    const latestItemRunResult = await pool.query(
      'SELECT * FROM executor_runs WHERE roadmap_item_id IS NOT NULL ORDER BY created_at DESC LIMIT 1'
    );
    const heartbeatRun = latestRunResult.rows[0] || null;
    const latestItemRun = latestItemRunResult.rows[0] || null;

    // Queue diagnostics: count pending roadmap items by skip reason. Lets
    // the operator see WHY the loop is idling (every cycle says "no eligible
    // items" without any hint that the queue is fully consumed by skip
    // flags). Read roadmap.json directly; this is a synchronous read of a
    // tracked file, no DB call needed.
    let queueDiag = {
      pending: 0,
      eligible: 0,
      skipped_complex: 0,
      skipped_executor_eligible_false: 0,
      skipped_blocked: 0,
      skipped_failed_today: 0,
      sample_eligible_id: null as string | null,
    };
    try {
      const fs = require('fs');
      const path = require('path');
      // The web service runs from web/, roadmap is at ../coordinator/roadmap.json
      const roadmapPath = path.resolve(__dirname, '..', '..', '..', 'coordinator', 'roadmap.json');
      const raw = fs.readFileSync(roadmapPath, 'utf-8');
      const items = (JSON.parse(raw).items || []) as Array<Record<string, unknown>>;
      const completedIds = new Set(items.filter(i => i.status === 'completed').map(i => i.id as string));
      const COMPLEX_KEYWORDS = ['audit', 'full', 'complete', 'comprehensive', 'top-to-bottom', 'end-to-end', 'across all'];
      function isComplex(it: Record<string, unknown>): boolean {
        const explicit = String((it.complexity || '')).toLowerCase().trim();
        if (explicit === 'complex') return true;
        if (explicit === 'easy' || explicit === 'medium' || explicit === 'standard') return false;
        const title = String(it.title || '').toLowerCase();
        return COMPLEX_KEYWORDS.some(k => title.includes(k));
      }
      for (const it of items) {
        if (it.status !== 'pending') continue;
        queueDiag.pending += 1;
        const blocked = ((it.blocked_by as string[]) || []).filter(b => !completedIds.has(b));
        if (blocked.length > 0) { queueDiag.skipped_blocked += 1; continue; }
        if ((it as { executor_eligible?: boolean }).executor_eligible === false) {
          queueDiag.skipped_executor_eligible_false += 1; continue;
        }
        if (isComplex(it)) { queueDiag.skipped_complex += 1; continue; }
        queueDiag.eligible += 1;
        if (!queueDiag.sample_eligible_id) queueDiag.sample_eligible_id = it.id as string;
      }
    } catch (e) {
      // Roadmap read failed (path miss, parse error, etc.); render banner
      // without diagnostics rather than 500 the page.
      console.warn('admin-executor: queue diag read failed:', (e as Error).message);
    }

    res.render('admin-executor', { activeTab: 'executor',
      title: 'Executor',
      runs: runs.rows,
      questions: questions.rows,
      stats: stats.rows[0],
      pendingQuestions: parseInt(pendingQuestions.rows[0].count as string, 10),
      statusFilter,
      statusCounts,
      page,
      totalPages,
      totalRuns,
      perPage: PER_PAGE,
      heartbeatRun,
      latestItemRun,
      queueDiag,
    });
  } catch (err) {
    console.error('Admin executor error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

// ─── Coordinator Settings ─────────────────────────────────────────────────────

// The worker service whose variables this page edits is identified by env:
// RAILWAY_PROJECT_ID and RAILWAY_ENVIRONMENT_ID are injected by Railway into
// every service; RAILWAY_WORKER_SERVICE_ID is set by the operator. When any is
// missing the settings page says the variables could not be read, draws no
// control, and a save reports failure. Which variables the page shows and may
// write, and the rules for a save, are in services/coordinator-settings.ts.
interface WorkerTarget { projectId: string; environmentId: string; serviceId: string }

function getWorkerTarget(): WorkerTarget | null {
  const projectId = process.env.RAILWAY_PROJECT_ID || '';
  const environmentId = process.env.RAILWAY_ENVIRONMENT_ID || '';
  const serviceId = process.env.RAILWAY_WORKER_SERVICE_ID || '';
  // IDs are interpolated into GraphQL text below; accept UUID-shaped values only.
  const ok = (v: string): boolean => /^[A-Za-z0-9-]{8,64}$/.test(v);
  if (!ok(projectId) || !ok(environmentId) || !ok(serviceId)) return null;
  return { projectId, environmentId, serviceId };
}

// The worker service's variables, or null when they could not be read. The
// two are kept apart because "not set" has a meaning on the settings page (a
// switch shows as its default) and "unknown" must not be drawn as one.
//
// Railway returns every variable of the service, the worker's API keys and
// database URL included. Only the names the settings page reads or writes are
// kept, and every other value is dropped here, before anything is returned.
async function fetchWorkerVars(): Promise<Record<string, string> | null> {
  const token = process.env.RAILWAY_ADMIN_TOKEN;
  const target = getWorkerTarget();
  if (!token || !target) return null;
  try {
    const resp = await fetch('https://backboard.railway.app/graphql/v2', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        query: `query { variables(projectId: "${target.projectId}", environmentId: "${target.environmentId}", serviceId: "${target.serviceId}") }`,
      }),
      signal: AbortSignal.timeout(RAILWAY_FETCH_TIMEOUT_MS),
    });
    if (!resp.ok) return null;
    const data = await resp.json() as { data?: { variables?: unknown } };
    const variables = data?.data?.variables;
    if (typeof variables !== 'object' || variables === null || Array.isArray(variables)) return null;
    const kept: Record<string, string> = {};
    const shown = new Set<string>(COORDINATOR_SETTING_NAMES);
    for (const [name, value] of Object.entries(variables as Record<string, unknown>)) {
      if (shown.has(name) && typeof value === 'string') kept[name] = value;
    }
    return kept;
  } catch (err) {
    console.error('Failed to fetch worker vars:', (err as Error).message);
    return null;
  }
}

async function upsertWorkerVar(name: string, value: string): Promise<boolean> {
  // Same double gate as project deletion (services/railway.ts): a local or
  // staging server that happens to hold a Railway token and the production
  // worker's ids must not be able to flip the autonomous agents on, or raise
  // their budget, in production.
  if (!isRailwayWriteEnabled()) return false;
  const token = process.env.RAILWAY_ADMIN_TOKEN;
  const target = getWorkerTarget();
  if (!token || !target) return false;
  try {
    const resp = await fetch('https://backboard.railway.app/graphql/v2', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        query: `mutation { variableUpsert(input: { projectId: "${target.projectId}", environmentId: "${target.environmentId}", serviceId: "${target.serviceId}", name: "${name}", value: "${value}" }) }`,
      }),
      signal: AbortSignal.timeout(RAILWAY_FETCH_TIMEOUT_MS),
    });
    const data = await resp.json() as { data?: { variableUpsert?: boolean } };
    return data?.data?.variableUpsert === true;
  } catch (err) {
    console.error(`Failed to upsert ${name}:`, (err as Error).message);
    return false;
  }
}

router.get('/settings', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  const pendingApprovalCount = await pool.query("SELECT COUNT(*) FROM approval_requests WHERE status = 'pending'")
    .then(r => parseInt(r.rows[0].count, 10)).catch(() => 0);

  // The page draws each switch in the state the worker reads from the stored
  // text, and nothing at all when the variables could not be read.
  const settings = describeCoordinatorSettings(await fetchWorkerVars());

  res.render('admin-settings', {
    title: 'Settings',
    activeTab: 'settings',
    settings,
    canWrite: isRailwayWriteEnabled(),
    shownPrefix: SHOWN_PREFIX,
    goLiveField: GO_LIVE_FIELD,
    goLivePhrase: GO_LIVE_PHRASE,
    pendingApprovalCount,
    error: req.query.error || '',
    success: req.query.success || '',
  });
});

router.post('/settings/coordinator', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  if (!isRailwayWriteEnabled()) {
    res.redirect('/admin/settings?error=' + encodeURIComponent(
      'Railway writes are disabled in this environment (they require NODE_ENV=production and CAS_ALLOW_RAILWAY_WRITES=true). Nothing was saved.'
    ));
    return;
  }
  try {
    // Only controls the form carried, and only where the submitted value
    // differs from what the page showed; a save that would let the
    // coordinator act needs the typed confirmation.
    const plan = planCoordinatorSettingsSave((req.body ?? {}) as Record<string, unknown>);
    if (plan.refusal) {
      res.redirect('/admin/settings?error=' + encodeURIComponent(plan.refusal));
      return;
    }
    if (plan.writes.length === 0) {
      res.redirect('/admin/settings?success=' + encodeURIComponent('Nothing was changed, so nothing was written.'));
      return;
    }

    // One at a time (Railway throttles parallel mutations), in the plan's
    // order, and no further once one fails: a change that arms something is
    // never sent after a change toward the safe state did not go through.
    const saved: string[] = [];
    for (const write of plan.writes) {
      const ok = await upsertWorkerVar(write.name, write.value);
      if (!ok) {
        res.redirect('/admin/settings?error=' + encodeURIComponent(
          `Could not save ${write.name}; stopped there with ${saved.length} of ${plan.writes.length} change(s) saved. Check RAILWAY_ADMIN_TOKEN and RAILWAY_WORKER_SERVICE_ID.`
        ));
        return;
      }
      saved.push(write.name);
    }
    res.redirect('/admin/settings?success=' + encodeURIComponent(
      `Saved ${saved.join(', ')}. The worker reads these when it starts.`
    ));
  } catch (err) {
    console.error('Settings save error:', (err as Error).message);
    res.redirect('/admin/settings?error=' + encodeURIComponent('Failed to save settings.'));
  }
});

// POST /admin/executor/answer/:id : answer a question
router.post('/executor/answer/:id', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { id } = req.params;
  const answer = optionalText(req.body.answer, 8000);
  if (!answer) {
    renderErrorPage(res, 400, { title: 'Answer required', error: 'An answer is required.' });
    return;
  }
  try {
    await pool.query(
      "UPDATE executor_questions SET answer = $1, status = 'answered', answered_at = NOW() WHERE id = $2",
      [answer, id]
    );
    res.redirect('/admin/executor');
  } catch (err) {
    console.error('Answer question error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

// ---------------------------------------------------------------------------
// Coordinator findings: admin review of coordinator-surfaced issues
// ---------------------------------------------------------------------------

interface Finding {
  id: string;
  agent: string;
  category: string;
  finding_key: string;
  title: string;
  description: string;
  priority: string;
  status: string;
  roadmap_item_id: string | null;
  review_notes: string | null;
  created_at: string;
  reviewed_at: string | null;
  reviewed_by: string | null;
}

const FINDING_VALID_STATUSES = ['new', 'reviewed', 'promoted', 'dismissed', 'all'];

export async function getNewFindingCount(): Promise<number> {
  const result = await pool.query(
    `SELECT COUNT(*) AS count FROM coordinator_findings WHERE status = 'new'`
  );
  return parseInt(result.rows[0].count as string, 10) || 0;
}

// GET /admin/findings : list all coordinator findings with filters
router.get('/findings', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    const statusQuery = (req.query.status as string) || 'new';
    const statusFilter = FINDING_VALID_STATUSES.includes(statusQuery) ? statusQuery : 'new';
    const categoryFilter = (req.query.category as string) || '';
    const priorityFilter = (req.query.priority as string) || '';

    const conditions: string[] = [];
    const params: (string | number)[] = [];
    if (statusFilter !== 'all') {
      params.push(statusFilter);
      conditions.push(`status = $${params.length}`);
    }
    if (categoryFilter) {
      params.push(categoryFilter);
      conditions.push(`category = $${params.length}`);
    }
    if (priorityFilter && ['low', 'medium', 'high'].includes(priorityFilter)) {
      params.push(priorityFilter);
      conditions.push(`priority = $${params.length}`);
    }
    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    const [itemsResult, statsResult, categoriesResult] = await Promise.all([
      pool.query(
        `SELECT id, agent, category, finding_key, title, description, priority,
                status, roadmap_item_id, review_notes, created_at, reviewed_at, reviewed_by
         FROM coordinator_findings
         ${whereClause}
         ORDER BY
           CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
           created_at DESC
         LIMIT 200`,
        params
      ),
      pool.query(
        `SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE status = 'new') AS new_count,
          COUNT(*) FILTER (WHERE status = 'reviewed') AS reviewed,
          COUNT(*) FILTER (WHERE status = 'promoted') AS promoted,
          COUNT(*) FILTER (WHERE status = 'dismissed') AS dismissed
        FROM coordinator_findings`
      ),
      pool.query(
        `SELECT DISTINCT category FROM coordinator_findings ORDER BY category`
      ),
    ]);

    const stats = {
      total: parseInt(statsResult.rows[0].total as string, 10) || 0,
      new: parseInt(statsResult.rows[0].new_count as string, 10) || 0,
      reviewed: parseInt(statsResult.rows[0].reviewed as string, 10) || 0,
      promoted: parseInt(statsResult.rows[0].promoted as string, 10) || 0,
      dismissed: parseInt(statsResult.rows[0].dismissed as string, 10) || 0,
    };

    res.render('admin-findings', {
      activeTab: 'findings',
      title: 'Admin - Findings',
      items: itemsResult.rows as Finding[],
      stats,
      statusFilter,
      categoryFilter,
      priorityFilter,
      availableCategories: categoriesResult.rows.map(r => r.category),
    });
  } catch (err) {
    console.error('Error loading findings:', (err as Error).message);
    res.status(500).render('500', { title: 'Error', error: 'Failed to load findings.' });
  }
});

// POST /admin/findings/:id/review : mark reviewed (no promotion)
router.post('/findings/:id/review', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { id } = req.params;
  const notes = optionalText(req.body.notes, 4000);
  const reviewer = req.session.customerEmail || 'admin';
  try {
    const result = await pool.query(
      `UPDATE coordinator_findings
       SET status = 'reviewed', reviewed_at = NOW(), reviewed_by = $1, review_notes = $2
       WHERE id = $3 AND status = 'new'
       RETURNING id`,
      [reviewer, notes, id]
    );
    if (result.rowCount === 0) {
      renderErrorPage(res, 404, { title: 'Not found', error: 'Finding not found or not in new status.' });
      return;
    }
    res.redirect('/admin/findings');
  } catch (err) {
    console.error('Review finding error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

// POST /admin/findings/:id/dismiss : mark dismissed
router.post('/findings/:id/dismiss', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { id } = req.params;
  const notes = optionalText(req.body.notes, 4000);
  const reviewer = req.session.customerEmail || 'admin';
  try {
    const result = await pool.query(
      `UPDATE coordinator_findings
       SET status = 'dismissed', reviewed_at = NOW(), reviewed_by = $1, review_notes = $2
       WHERE id = $3 AND status IN ('new', 'reviewed')
       RETURNING id`,
      [reviewer, notes, id]
    );
    if (result.rowCount === 0) {
      renderErrorPage(res, 404, { title: 'Not found', error: 'Finding not found or already resolved.' });
      return;
    }
    res.redirect('/admin/findings');
  } catch (err) {
    console.error('Dismiss finding error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

// POST /admin/findings/:id/promote : mark promoted (caller is expected to add the
// corresponding roadmap item manually or via a separate executor flow). This just
// records the intent + the roadmap_item_id the admin provides.
router.post('/findings/:id/promote', requireAdmin, validateCsrfToken, async (req: Request, res: Response): Promise<void> => {
  const { id } = req.params;
  const roadmapItemId = optionalText(req.body.roadmap_item_id, 200) ?? '';
  const notes = optionalText(req.body.notes, 4000);
  const reviewer = req.session.customerEmail || 'admin';
  if (!roadmapItemId) {
    renderErrorPage(res, 400, { title: 'Missing field', error: 'roadmap_item_id is required when promoting a finding.' });
    return;
  }
  try {
    const result = await pool.query(
      `UPDATE coordinator_findings
       SET status = 'promoted', roadmap_item_id = $1, reviewed_at = NOW(),
           reviewed_by = $2, review_notes = $3
       WHERE id = $4 AND status IN ('new', 'reviewed')
       RETURNING id`,
      [roadmapItemId, reviewer, notes, id]
    );
    if (result.rowCount === 0) {
      renderErrorPage(res, 404, { title: 'Not found', error: 'Finding not found or already resolved.' });
      return;
    }
    res.redirect('/admin/findings');
  } catch (err) {
    console.error('Promote finding error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

/* ============================================================================
   /admin/harbor: master inbox of active CAS Harbor trials.
   ----------------------------------------------------------------------------
   Read-only listing of trial_keepers rows + per-trial turn count, memory
   count, last seen, opening keeper words, and a "ready to hand off" signal.
   Closes the loop on Haven's "Ocean will be in touch" promise: when a keeper
   reaches the SKETCH stage of the working arc, this page surfaces them so
   Ocean can pick up the relationship by email and dispatch a v2 build by
   hand. Phase 2 will replace the manual hand-off with an automated promotion.

   A trial that pays is released and leaves that listing; its conversation
   then belongs to the keeper. `?status=keepers` lists paid keepers with the
   same conversation columns, read by keeper id.
   ========================================================================= */

interface HarborTrialRow {
  trial_id: string;
  companion_name: string;
  started_at: Date;
  expires_at: Date;
  last_seen_at: Date;
  turn_count: string;            // pg COUNT() returns string
  memory_count: string;
  first_keeper_text: string | null;
  last_companion_text: string | null;
  email_captured: string | null;
  handoff_build_id: string | null;
  handoff_at: Date | null;
  // Faithful-copy state surfaced to the inbox so the operator can see
  // at a glance which trials already have a captured source-site crawl.
  // Boolean (not the full JSONB) to keep the row payload small.
  harbor_meta_has_copy_source: boolean;
}

/** One paid keeper in the inbox's "Paid keepers" view. */
interface HarborKeeperRow {
  customer_id: string;
  email: string;
  companion_name: string;
  subscription_status: string;
  handoff_build_id: string | null;
  turn_count: string;
  memory_count: string;
  first_keeper_text: string | null;
  last_turn_at: Date | null;
}

/**
 * The conversation columns of one inbox row. A conversation is keyed by the
 * trial until payment and by the keeper afterwards (promotion re-keys the
 * turns and the notes, see services/keeper_promotion.ts), so the trial list
 * and the keeper list count through this one definition and differ only in
 * the owner column. Both arguments are fixed strings in this file, never
 * request input.
 */
function harborConversationSql(column: MemoryOwnerColumn, ownerId: 'tk.trial_id' | 'k.customer_id'): string {
  return `(SELECT COUNT(*) FROM companion_turns ct
            WHERE ct.${column} = ${ownerId}) AS turn_count,
         (SELECT COUNT(*) FROM keeper_memory km
            WHERE km.${column} = ${ownerId} AND km.deleted_at IS NULL) AS memory_count,
         (SELECT keeper_text FROM companion_turns ct
            WHERE ct.${column} = ${ownerId} AND ct.keeper_text IS NOT NULL
            ORDER BY ct.created_at ASC LIMIT 1) AS first_keeper_text`;
}

const HARBOR_HANDOFF_PHRASE = /ocean[^.\n]{0,40}(will be in touch|will reach out|takes it from here)/i;
const HARBOR_HANDOFF_MIN_TURNS = 8;
const HARBOR_LIST_LIMIT = 200;

function detectHarborReadyToHandoff(turnCount: number, lastCompanionText: string | null): boolean {
  if (turnCount >= HARBOR_HANDOFF_MIN_TURNS) return true;
  if (lastCompanionText && HARBOR_HANDOFF_PHRASE.test(lastCompanionText)) return true;
  return false;
}

router.get('/harbor', requireAdmin, async (req: Request, res: Response): Promise<void> => {
  try {
    if (process.env.CAS_HARBOR_ENABLED !== 'true') {
      res.render('admin-harbor', {
        title: 'Harbor Inbox',
        activeTab: 'harbor',
        harborEnabled: false,
        trials: [],
        stats: { total: 0, active: 0, ready: 0 },
      });
      return;
    }
    const { rows } = await pool.query<HarborTrialRow>(
      `SELECT
         tk.trial_id,
         tk.companion_name,
         tk.started_at,
         tk.expires_at,
         tk.last_seen_at,
         tk.email_captured,
         tk.handoff_build_id,
         tk.handoff_at,
         (tk.harbor_meta_overrides IS NOT NULL
          AND tk.harbor_meta_overrides ? 'copy_source_url')
           AS harbor_meta_has_copy_source,
         ${harborConversationSql('trial_id', 'tk.trial_id')},
         (SELECT companion_text FROM companion_turns ct
            WHERE ct.trial_id = tk.trial_id
            ORDER BY ct.created_at DESC LIMIT 1) AS last_companion_text
       FROM trial_keepers tk
       WHERE tk.released_at IS NULL
       ORDER BY tk.last_seen_at DESC NULLS LAST
       LIMIT $1`,
      [HARBOR_LIST_LIMIT]
    );

    const now = Date.now();
    const trials = rows.map((row) => {
      const turnCount = parseInt(row.turn_count, 10) || 0;
      const memoryCount = parseInt(row.memory_count, 10) || 0;
      const expiresMs = new Date(row.expires_at).getTime();
      const isExpired = expiresMs < now;
      const readyToHandoff = detectHarborReadyToHandoff(turnCount, row.last_companion_text);
      return {
        trial_id: row.trial_id,
        companion_name: row.companion_name,
        started_at: row.started_at,
        expires_at: row.expires_at,
        last_seen_at: row.last_seen_at,
        turn_count: turnCount,
        memory_count: memoryCount,
        first_keeper_text: row.first_keeper_text,
        last_companion_text: row.last_companion_text,
        is_expired: isExpired,
        ready_to_handoff: readyToHandoff,
        email_captured: row.email_captured,
        handoff_build_id: row.handoff_build_id,
        handoff_at: row.handoff_at,
        // True when the operator already ran a faithful-copy crawl for
        // this trial. The "Source site" button in admin-harbor.ejs flips
        // to "Source site ✓" so a re-click prompts re-crawl-or-clear
        // rather than silently overwriting.
        harbor_meta_has_copy_source: row.harbor_meta_has_copy_source === true,
        // Dispatchable when: trial has any conversation, hasn't been
        // dispatched yet, and isn't expired.
        is_dispatchable: turnCount > 0 && !row.handoff_build_id && !isExpired,
      };
    });

    // Stats are over the FULL row set (so the chip counts don't change
    // as you flip filters; the UI shows "showing N of M" once filtered).
    const stats = {
      total: trials.length,
      active: trials.filter((t) => !t.is_expired && t.turn_count > 0 && !t.handoff_build_id).length,
      ready: trials.filter((t) => t.ready_to_handoff && !t.handoff_build_id).length,
      handed_off: trials.filter((t) => !!t.handoff_build_id).length,
    };

    // Optional filter: ?status=active|ready|handed_off|keepers|all (default all).
    // Mirrors the chip set in admin-harbor.ejs. Anything else falls through to all.
    const rawStatus = String(req.query.status || 'all').toLowerCase();
    const allowedStatus = new Set(['all', 'active', 'ready', 'handed_off', 'keepers']);
    const statusFilter = allowedStatus.has(rawStatus) ? rawStatus : 'all';
    const filteredTrials = (() => {
      switch (statusFilter) {
        case 'active':
          return trials.filter((t) => !t.is_expired && t.turn_count > 0 && !t.handoff_build_id);
        case 'ready':
          return trials.filter((t) => t.ready_to_handoff && !t.handoff_build_id);
        case 'handed_off':
          return trials.filter((t) => !!t.handoff_build_id);
        case 'keepers':
          return [];
        case 'all':
        default:
          return trials;
      }
    })();

    // Payment releases the trial, which drops it from the list above, and
    // moves its turns and notes to the keeper. The "Paid keepers" view lists
    // those conversations by keeper id, most recent turn first. It is read
    // only when that view is asked for.
    let keepers: Array<Omit<HarborKeeperRow, 'turn_count' | 'memory_count'> & { turn_count: number; memory_count: number }> = [];
    if (statusFilter === 'keepers') {
      const keeperRows = await pool.query<HarborKeeperRow>(
        `SELECT
           k.customer_id,
           c.email,
           k.companion_name,
           k.subscription_status,
           k.handoff_build_id,
           ${harborConversationSql('keeper_id', 'k.customer_id')},
           (SELECT MAX(ct.created_at) FROM companion_turns ct
              WHERE ct.keeper_id = k.customer_id) AS last_turn_at
         FROM keepers k
         JOIN customers c ON c.id = k.customer_id
         ORDER BY last_turn_at DESC NULLS LAST, k.created_at DESC
         LIMIT $1`,
        [HARBOR_LIST_LIMIT]
      );
      keepers = keeperRows.rows.map((row) => ({
        ...row,
        turn_count: parseInt(row.turn_count, 10) || 0,
        memory_count: parseInt(row.memory_count, 10) || 0,
      }));
    }

    res.render('admin-harbor', {
      title: 'Harbor Inbox',
      activeTab: 'harbor',
      harborEnabled: true,
      // dev_trial deep-links work only when isDevOrTestEnv() is true, that is
      // when NODE_ENV is exactly "development" or "test" (the lighthouse
      // override is hard-gated off in every other environment, an unset
      // NODE_ENV included). Hide the links elsewhere so the admin doesn't
      // click into a fresh-trial loop.
      devTrialEnabled: isDevOrTestEnv(),
      trials: filteredTrials,
      keepers,
      totalUnfilteredTrials: trials.length,
      stats,
      statusFilter,
      csrfToken: generateCsrfToken(req),
    });
  } catch (err) {
    console.error('Admin harbor list error:', (err as Error).message);
    res.status(500).render('500', { title: 'Error' });
  }
});

// Internal: feed an archetype-correction signal from a Harbor dispatch
// into the `agent_learnings` table when the model's raw archetype guess
// didn't match the coerced archetype hint that flows downstream. The rows
// are for a person: the weekly learning digest email lists them, as material for
// extending the keywords in compose/intake.py detect_archetype(). No
// coordinator agent reads this table.
//
// Comparison rules:
//   - Both empty           -> nothing to learn from; skip.
//   - Same value           -> no correction needed; skip.
//   - Opus guess different -> record (Opus invented an archetype id
//                             that isn't in KNOWN_ARCHETYPES, or named
//                             a different known archetype than what
//                             the translator emitted).
//
// Best-effort: errors swallowed (logged only) so a learning-write
// failure cannot fail the dispatch itself.
export async function recordHarborArchetypeCorrectionLearning(
  trialId: string,
  opusGuess: string | undefined,
  coercedArchetype: string | undefined
): Promise<void> {
  const SOURCE_AGENT = 'architect';
  const CATEGORY = 'archetype_correction';
  const guess = (opusGuess || '').trim();
  const coerced = (coercedArchetype || '').trim();
  if (!guess) return;
  if (guess === coerced) return;
  const learning =
    `Harbor dispatch trial=${trialId}: Opus archetype guess "${guess}" ` +
    `differs from coerced archetype "${coerced || '(none)'}". ` +
    `Architect: propose keyword expansion in compose/intake.py ` +
    `detect_archetype() to map "${guess}"-style language to the correct ` +
    `archetype id.`;
  const learningHash = crypto
    .createHash('sha256')
    .update(`${SOURCE_AGENT}:${CATEGORY}:${learning}`)
    .digest('hex')
    .slice(0, 16);
  try {
    await pool.query(
      `INSERT INTO agent_learnings
         (source_agent, category, learning, learning_hash)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (source_agent, category, learning_hash)
       DO UPDATE SET last_applied_at = NOW()`,
      [SOURCE_AGENT, CATEGORY, learning, learningHash]
    );
  } catch (err) {
    console.error(
      '[admin/harbor/dispatch] failed to record archetype_correction learning for trial=%s: %s',
      trialId,
      (err as Error).message
    );
  }
}

// Internal: feed BARREN_SPEC / THIN_SPEC warnings from a Harbor dispatch
// into the `agent_learnings` table so the operator can see which keeper
// conversations produced thin specs (the weekly learning digest email lists them;
// no coordinator agent reads this table). Best-effort: errors are swallowed (logged
// only) so a learning-write failure cannot fail the dispatch itself.
//
// The hash is computed over (source_agent | category | learning) per the
// existing `_record_learning` convention shared with
// scripts/analyze_harbor_conversion_funnel.py and coordinator/tick.py;
// embedding the trial_id in the learning text gives every (trial, warning)
// pair a unique row, while still deduping if the exact same dispatch is
// somehow replayed.
export async function recordHarborExtractionLearning(
  trialId: string,
  warnings: readonly string[]
): Promise<void> {
  const SOURCE_AGENT = 'backend_engineer';
  const CATEGORY = 'harbor_extraction';
  const flagged = warnings.filter((w) => {
    const s = String(w);
    return s.startsWith('BARREN_SPEC') || s.startsWith('THIN_SPEC');
  });
  if (flagged.length === 0) return;
  const summary = flagged
    .map((w) => `- ${w}`)
    .join('\n');
  const learning = `Harbor dispatch trial=${trialId} produced ${flagged.length} thin-spec warning(s):\n${summary}`;
  const learningHash = crypto
    .createHash('sha256')
    .update(`${SOURCE_AGENT}:${CATEGORY}:${learning}`)
    .digest('hex')
    .slice(0, 16);
  try {
    await pool.query(
      `INSERT INTO agent_learnings
         (source_agent, category, learning, learning_hash)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (source_agent, category, learning_hash)
       DO UPDATE SET last_applied_at = NOW()`,
      [SOURCE_AGENT, CATEGORY, learning, learningHash]
    );
  } catch (err) {
    console.error(
      '[admin/harbor/dispatch] failed to record agent_learning for trial=%s: %s',
      trialId,
      (err as Error).message
    );
  }
}

// POST /admin/harbor/:trial_id/crawl-source-site, operator-triggered
// faithful-copy crawl. When a keeper has an existing live website that
// the rebuild should mirror exactly, the operator pastes the URL here;
// the crawler captures the site's content into _harbor_meta.copy_pages
// + copy_source_url, persisted on `trial_keepers.harbor_meta_overrides`.
// At dispatch time, those overrides merge into the Opus-extracted spec's
// _harbor_meta and flow through to compose's faithful-copy layout.
//
// Auth + safety:
//   - requireAdmin (admin session required; no anonymous trigger).
//   - validateCsrfToken (POST against same-origin form; CSRF-gated).
//   - Body: { source_url: string }. Defaults to JSON parsing.
//   - Crawler enforces SSRF guard (http(s) only, rejects private/loopback/
//     cloud-metadata hosts on start URL + every redirect).
//   - 503 when CAS_HARBOR_ENABLED !== 'true'.
//   - 400 on malformed trial_id, missing source_url, or non-http(s) URL.
//   - 404 on trial not found.
//   - 409 when trial.released_at IS NOT NULL (already promoted to keeper;
//     overrides on a released trial would be silently lost on dispatch).
//   - 502 on crawler failure with no captured pages (network down,
//     content-type wrong, body cap blew, etc.).
//
// Idempotent: a re-run with the same source_url overwrites the prior
// overrides for `copy_source_url` + `copy_pages` keys (the crawler is
// deterministic for the same input). Other override keys preserved.
router.post(
  '/harbor/:trial_id/crawl-source-site',
  requireAdmin,
  validateCsrfToken,
  async (req: Request, res: Response): Promise<void> => {
    if (process.env.CAS_HARBOR_ENABLED !== 'true') {
      res.status(503).json({ error: 'harbor_disabled' });
      return;
    }
    const trialId = String(req.params.trial_id || '').trim();
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (!UUID_RE.test(trialId)) {
      res.status(400).json({ error: 'bad_trial_id' });
      return;
    }
    const sourceUrl = typeof req.body?.source_url === 'string'
      ? req.body.source_url.trim()
      : '';
    if (!sourceUrl) {
      res.status(400).json({ error: 'source_url_required' });
      return;
    }
    if (!/^https?:\/\//i.test(sourceUrl)) {
      res.status(400).json({ error: 'source_url_must_be_http' });
      return;
    }
    try {
      // Confirm the trial exists + is dispatchable before paying for the
      // crawl. The dispatch handler also checks released_at; we duplicate
      // here so the operator gets feedback BEFORE the crawler runs.
      const trialRes = await pool.query<{
        trial_id: string;
        released_at: Date | null;
      }>(
        `SELECT trial_id, released_at FROM trial_keepers WHERE trial_id = $1`,
        [trialId]
      );
      if (trialRes.rows.length === 0) {
        res.status(404).json({ error: 'trial_not_found' });
        return;
      }
      if (trialRes.rows[0].released_at) {
        res.status(409).json({
          error: 'trial_already_promoted',
          message:
            'This trial has already been promoted to a paid keeper. Overrides on a released trial are silently dropped.',
        });
        return;
      }

      // Run the crawl. Defaults: 6 pages max, 60s wall-clock, 2 MiB cap,
      // SSRF guard active. The crawler returns warnings (e.g. partial
      // fetches) inline; we surface them so the operator can decide
      // whether to dispatch with thin content.
      const result = await crawlSite(sourceUrl);
      if (result.pages.length === 0) {
        res.status(502).json({
          error: 'crawl_yielded_no_pages',
          source_url: result.source_url,
          warnings: result.warnings,
        });
        return;
      }

      // Persist into trial_keepers.harbor_meta_overrides via JSONB merge:
      // copy_source_url + copy_pages overwrite the prior values; other
      // override keys (future archetype_hint nudges, palette overrides,
      // etc.) are preserved by the COALESCE + jsonb_set chain.
      const overrideUpdate: Record<string, unknown> = {
        copy_source_url: result.source_url,
        copy_pages: result.pages,
      };
      // Deep-media candidates: mined from script blobs,
      // operator-approvable; persisted only when the crawl found any.
      if (Array.isArray(result.media_candidates) && result.media_candidates.length > 0) {
        overrideUpdate.copy_media_candidates = result.media_candidates;
      }
      await pool.query(
        `UPDATE trial_keepers
         SET harbor_meta_overrides = COALESCE(harbor_meta_overrides, '{}'::jsonb) || $1::jsonb
         WHERE trial_id = $2`,
        [JSON.stringify(overrideUpdate), trialId]
      );
      console.log(
        '[admin/harbor/crawl-source-site] captured trial=%s source=%s pages=%d warnings=%d by=%s',
        trialId,
        result.source_url,
        result.pages.length,
        result.warnings.length,
        req.session.customerEmail
      );
      res.json({
        ok: true,
        sourceUrl: result.source_url,
        pageCount: result.pages.length,
        pagePaths: result.pages.map((p) => p.path),
        mediaCandidateCount: Array.isArray(result.media_candidates)
          ? result.media_candidates.length
          : 0,
        warnings: result.warnings,
      });
    } catch (err) {
      console.error(
        '[admin/harbor/crawl-source-site] failed: trial=%s err=%s',
        trialId,
        (err as Error).message
      );
      res.status(500).json({ error: 'crawl_failed', message: (err as Error).message });
    }
  }
);

// GET /admin/harbor/:trial_id/source-preview, read-only inspection of a
// trial's harbor_meta_overrides + a compact preview of any captured
// copy_pages. Lets the operator confirm the crawl result before clicking
// dispatch. JSON only; admin UI consumes via fetch().
//
// Auth: requireAdmin. No CSRF (read-only GET). 503 when flag off.
router.get(
  '/harbor/:trial_id/source-preview',
  requireAdmin,
  async (req: Request, res: Response): Promise<void> => {
    if (process.env.CAS_HARBOR_ENABLED !== 'true') {
      res.status(503).json({ error: 'harbor_disabled' });
      return;
    }
    const trialId = String(req.params.trial_id || '').trim();
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (!UUID_RE.test(trialId)) {
      res.status(400).json({ error: 'bad_trial_id' });
      return;
    }
    try {
      const r = await pool.query<{
        trial_id: string;
        harbor_meta_overrides: Record<string, unknown> | null;
      }>(
        `SELECT trial_id, harbor_meta_overrides
         FROM trial_keepers WHERE trial_id = $1`,
        [trialId]
      );
      if (r.rows.length === 0) {
        res.status(404).json({ error: 'trial_not_found' });
        return;
      }
      const overrides = r.rows[0].harbor_meta_overrides ?? {};
      const copyPages = Array.isArray(
        (overrides as Record<string, unknown>).copy_pages
      )
        ? ((overrides as Record<string, unknown>).copy_pages as Array<
            Record<string, unknown>
          >)
        : [];
      // Trim the preview: paths + first 200 chars of first markdown
      // block per page. Keeps the response under a few KB even for
      // verbose 6-page crawls.
      const preview = copyPages.map((p) => {
        const blocks = Array.isArray(p.markdown_blocks)
          ? (p.markdown_blocks as unknown[])
          : [];
        const firstBlock = blocks.find((b) => typeof b === 'string') as
          | string
          | undefined;
        return {
          path: typeof p.path === 'string' ? p.path : '',
          title: typeof p.title === 'string' ? p.title : '',
          blockCount: blocks.length,
          firstBlockSnippet: firstBlock
            ? firstBlock.slice(0, 200)
            : '',
        };
      });
      console.log(
        '[admin/harbor/source-preview] viewed trial=%s by=%s',
        trialId,
        req.session.customerEmail
      );
      const mediaCandidates = Array.isArray(
        (overrides as Record<string, unknown>).copy_media_candidates
      )
        ? ((overrides as Record<string, unknown>)
            .copy_media_candidates as unknown[]).filter(
            (u): u is string => typeof u === 'string'
          )
        : [];
      res.json({
        trialId,
        copySourceUrl:
          typeof (overrides as Record<string, unknown>).copy_source_url ===
          'string'
            ? (overrides as Record<string, unknown>).copy_source_url
            : null,
        pageCount: copyPages.length,
        preview,
        mediaCandidates,
      });
    } catch (err) {
      console.error(
        '[admin/harbor/source-preview] failed: trial=%s err=%s',
        trialId,
        (err as Error).message
      );
      res.status(500).json({ error: 'preview_failed' });
    }
  }
);

// POST /admin/harbor/:trial_id/clear-source, operator-triggered reset of
// harbor_meta_overrides (typically after a bad crawl or to pivot to a
// different source URL). Resets the JSONB column to NULL so dispatch
// runs against archetype defaults again.
//
// Auth: requireAdmin + validateCsrfToken.
router.post(
  '/harbor/:trial_id/clear-source',
  requireAdmin,
  validateCsrfToken,
  async (req: Request, res: Response): Promise<void> => {
    if (process.env.CAS_HARBOR_ENABLED !== 'true') {
      res.status(503).json({ error: 'harbor_disabled' });
      return;
    }
    const trialId = String(req.params.trial_id || '').trim();
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (!UUID_RE.test(trialId)) {
      res.status(400).json({ error: 'bad_trial_id' });
      return;
    }
    try {
      const r = await pool.query(
        `UPDATE trial_keepers
         SET harbor_meta_overrides = NULL
         WHERE trial_id = $1`,
        [trialId]
      );
      if (r.rowCount === 0) {
        res.status(404).json({ error: 'trial_not_found' });
        return;
      }
      res.json({ ok: true, trialId });
    } catch (err) {
      console.error(
        '[admin/harbor/clear-source] failed: trial=%s err=%s',
        trialId,
        (err as Error).message
      );
      res.status(500).json({ error: 'clear_failed' });
    }
  }
);

// POST /admin/harbor/:trial_id/dispatch, Ocean's manual handoff trigger.
// Pre-Stripe path: extract a v2 spec from the trial keeper's conversation
// + memory and queue a build, without first promoting the trial to a paid
// keeper. Stamps `trial_keepers.handoff_build_id` so the same trial cannot
// be double-dispatched. Mirrors `/lighthouse/build` but keyed on trial_id
// + admin-authenticated.
//
// Pipeline:
//   1. requireAdmin + validateCsrfToken.
//   2. Validate :trial_id is a UUID + the trial exists + has memory/turns.
//   3. Idempotent: if trial.handoff_build_id is already set, return it.
//   4. Resolve customer_id: prefer existing customers row by
//      trial.email_captured; create one (no password) when missing; fall
//      back to admin's own customer row if no email captured (so the
//      build at least belongs somewhere review-able). An address the build
//      engine would refuse as the site's admin address is refused here
//      (409 email_not_buildable), before the customer row and the call below.
//   5. extractSpecFromConversation({ trialId }) -> Opus call (~$0.18).
//   6. translateExtractedSpecToV1 -> v1-shape spec.
//   7. insertHarborBuild (services/harbor_dispatch.ts): the one INSERT that
//      creates a Harbor build, status='queued' and is_harbor=TRUE.
//   8. UPDATE trial_keepers SET handoff_build_id, handoff_at.
//   9. Return { buildId, warnings, costMicros, customerId }.
//
// Unlike the self-serve dispatch, the operator may dispatch again for a trial
// whose build has been deleted (handoff_build_id is NULL again while
// handoff_at is still set): deleting a bad build and re-dispatching is an
// operator's decision.
router.post(
  '/harbor/:trial_id/dispatch',
  requireAdmin,
  validateCsrfToken,
  async (req: Request, res: Response): Promise<void> => {
    if (process.env.CAS_HARBOR_ENABLED !== 'true') {
      res.status(503).json({ error: 'harbor_disabled' });
      return;
    }
    const trialId = String(req.params.trial_id || '').trim();
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (!UUID_RE.test(trialId)) {
      res.status(400).json({ error: 'bad_trial_id' });
      return;
    }

    try {
      // Load the trial, asserting it exists + grabbing email + handoff state
      // + any operator-set harbor_meta_overrides (faithful-copy crawl result,
      // archetype hint nudges, etc.) that the dispatch handler should merge
      // into the Opus-extracted spec's _harbor_meta.
      const trialRes = await pool.query<{
        trial_id: string;
        email_captured: string | null;
        handoff_build_id: string | null;
        released_at: Date | null;
        harbor_meta_overrides: Record<string, unknown> | null;
      }>(
        `SELECT trial_id, email_captured, handoff_build_id, released_at,
                harbor_meta_overrides
         FROM trial_keepers
         WHERE trial_id = $1`,
        [trialId]
      );
      if (trialRes.rows.length === 0) {
        res.status(404).json({ error: 'trial_not_found' });
        return;
      }
      const trial = trialRes.rows[0];
      if (trial.handoff_build_id) {
        res.json({
          buildId: trial.handoff_build_id,
          alreadyDispatched: true,
          warnings: [],
        });
        return;
      }
      // A trial that's already been promoted to a paid keeper (released_at
      // set by the Stripe webhook) had its memory + turns migrated to
      // keeper-keyed rows; the trial-keyed SELECTs would return empty and
      // the extraction would fail. Reject up front so the operator gets a
      // clear error instead of "no conversation to extract from".
      if (trial.released_at) {
        res.status(409).json({
          error: 'trial_already_promoted',
          message:
            'This trial has already been promoted to a paid keeper. Use the keeper build path instead.',
        });
        return;
      }

      // Resolve customer_id. Prefer the email captured on the trial; fall
      // back to the admin's own customer record so the build is at least
      // review-able even when no email was captured.
      const adminEmail = req.session.customerEmail || process.env.ADMIN_EMAIL || '';
      let resolvedEmail = (trial.email_captured || '').trim().toLowerCase();
      if (!resolvedEmail) {
        // Honest about the fallback: the build will live under the admin's
        // own account and Ocean must reach the keeper out-of-band.
        resolvedEmail = adminEmail.toLowerCase();
      }
      if (!resolvedEmail) {
        res.status(400).json({
          error: 'no_email_for_dispatch',
          message:
            'Trial has no email_captured and ADMIN_EMAIL is unset; cannot resolve a customer record to attach the build to.',
        });
        return;
      }
      // The worker writes the owning account's email into the spec as
      // admin_email, and the build engine refuses anything but a plain
      // address there (services/harbor_dispatch.ts, "The address a build is
      // owned by"). Such a build would only fail at validation, so it is
      // refused here, before a customers row is created for the address and
      // before the extraction is paid for.
      if (!isBuildableOwnerEmail(resolvedEmail)) {
        res.status(409).json({
          error: 'email_not_buildable',
          message:
            `The build engine does not accept ${resolvedEmail} as a site's admin address, so a build owned by that account would fail at validation. ` +
            'It takes letters, digits and . _ % + - before the @, and letters, digits and hyphens in each part after it. ' +
            (trial.email_captured && trial.email_captured.trim()
              ? 'That is the address captured on this trial; nothing was dispatched.'
              : 'No address was captured on this trial, so that is your own account; nothing was dispatched.'),
        });
        return;
      }

      // Extract the spec via Opus (trial path) and translate to v1-shape.
      // This is the expensive call (~$0.18); do it before the transaction
      // so we don't hold a DB connection through the network round-trip.
      // It is also the step most likely to end the dispatch (nothing to
      // extract, no model key, a failed call, a reply that is not a spec), so
      // it runs before the customers row below is written: a dispatch that
      // ends here leaves no account behind for the captured address.
      const extraction = await extractSpecFromConversation({
        pool,
        trialId,
      });
      const translation = translateExtractedSpecToV1(
        extraction.spec as Parameters<typeof translateExtractedSpecToV1>[0]
      );
      const v1Spec = translation.spec;
      const warnings = [...extraction.warnings, ...translation.warnings];

      // Find or create the customer row. Race-safe: ON CONFLICT DO NOTHING
      // returns no row when a concurrent INSERT (e.g. another dispatch or
      // a /signup with the same email) already created the row, so we
      // re-SELECT to pick it up. Without this guard the UNIQUE(email)
      // constraint on customers throws and the dispatch 500s.
      //
      // The customers schema enforces `password NOT NULL` + `name NOT NULL`,
      // so even when ON CONFLICT DO NOTHING is going to skip the insert,
      // Postgres validates the input row first and rejects NULL values.
      // We provide:
      //   - password: bcrypt-hashed random 64-byte secret. Unguessable;
      //     the keeper can never log in via password (we never share the
      //     plaintext). They reach their account via the magic link the
      //     worker emails after the build is reviewed, or via the
      //     standard password-reset flow on the login page.
      //   - name: derived from the email local part (everything before @),
      //     truncated to 60 chars. Falls back to "Trial Keeper" for the
      //     pathological empty-local-part case.
      const ephemeralPassword = await hashUnusablePassword();
      const localPart = resolvedEmail.split('@')[0] || '';
      const ephemeralName = (localPart || 'Trial Keeper').slice(0, 60);
      let customerId: string;
      const upsertRes = await pool.query<{ id: string }>(
        `INSERT INTO customers (email, password, email_verified, name)
         VALUES ($1, $2, FALSE, $3)
         ON CONFLICT (email) DO NOTHING
         RETURNING id`,
        [resolvedEmail, ephemeralPassword, ephemeralName]
      );
      if (upsertRes.rows.length > 0) {
        customerId = upsertRes.rows[0].id;
      } else {
        const existingRes = await pool.query<{ id: string }>(
          `SELECT id FROM customers WHERE email = $1 LIMIT 1`,
          [resolvedEmail]
        );
        if (existingRes.rows.length === 0) {
          throw new Error(
            'customer row vanished between INSERT ON CONFLICT and re-SELECT'
          );
        }
        customerId = existingRes.rows[0].id;
      }

      // Merge any operator-set harbor_meta_overrides (e.g. faithful-copy
      // crawl result captured via POST /admin/harbor/:trial_id/crawl-source-
      // site) into the Opus-extracted spec's _harbor_meta. Operator overrides
      // win over Opus inferences for the same key; that's the entire point
      // of the override channel. JSONB column on trial_keepers; null when
      // operator hasn't run any pre-dispatch tooling. Push the audit
      // line into BOTH `warnings` (response payload) and
      // `translation.warnings` (persisted on builds.spec._translation_warnings
      // by the existing serialization step below) so SE reviewers see it.
      if (
        trial.harbor_meta_overrides &&
        typeof trial.harbor_meta_overrides === 'object' &&
        !Array.isArray(trial.harbor_meta_overrides)
      ) {
        const v1WithMeta = v1Spec as unknown as Record<string, unknown> & {
          _harbor_meta?: Record<string, unknown>;
        };
        const existingMeta =
          v1WithMeta._harbor_meta && typeof v1WithMeta._harbor_meta === 'object'
            ? v1WithMeta._harbor_meta
            : {};
        v1WithMeta._harbor_meta = {
          ...existingMeta,
          ...trial.harbor_meta_overrides,
        };
        const overrideKeys = Object.keys(trial.harbor_meta_overrides).join(', ');
        if (overrideKeys) {
          const mergeNote =
            `dispatch: applied harbor_meta_overrides keys=[${overrideKeys}] from operator pre-dispatch tooling`;
          warnings.push(mergeNote);
          translation.warnings.push(mergeNote);
        }
      }

      const appName = v1Spec.app_name || `harbor-${trialId.slice(0, 8)}`;
      // Validate tier: the worker validates downstream, but a clear error
      // here saves an Opus call's worth of debugging on a malformed Opus
      // response.
      const ALLOWED_TIERS = new Set(['build', 'grow', 'scale']);
      const rawTier = (v1Spec.tier ?? 'build').toString();
      const tier = ALLOWED_TIERS.has(rawTier) ? rawTier : 'build';
      if (rawTier !== tier) {
        warnings.push(
          `dispatch: Opus returned tier='${rawTier}'; coerced to 'build'`
        );
      }

      // Persist the dispatch-time warnings (extraction + translation +
      // tier coercion) into builds.spec so they survive into
      // /admin/reviews and aren't lost when the dispatch handler returns.
      // Closes an earlier end-to-end finding: 13 warnings flowed
      // into the dispatch JSON response but only the 2 produced by
      // compose.intake.translate_v1_spec at build time were persisted.
      // We merge into the existing arrays (Opus may have populated
      // _extraction_warnings already; harbor_to_compose populates none
      // by default but this is defensive). Distinct sub-arrays preserve
      // the source of each warning.
      const v1WithWarnings = v1Spec as unknown as Record<string, unknown> & {
        _extraction_warnings?: unknown;
        _translation_warnings?: unknown;
      };
      const existingExtraction = Array.isArray(v1WithWarnings._extraction_warnings)
        ? v1WithWarnings._extraction_warnings as unknown[]
        : [];
      const existingTranslation = Array.isArray(v1WithWarnings._translation_warnings)
        ? v1WithWarnings._translation_warnings as unknown[]
        : [];
      v1WithWarnings._extraction_warnings = [
        ...existingExtraction,
        ...extraction.warnings,
      ];
      v1WithWarnings._translation_warnings = [
        ...existingTranslation,
        ...translation.warnings,
        ...(rawTier !== tier
          ? [`dispatch: Opus returned tier='${rawTier}'; coerced to 'build'`]
          : []),
      ];

      // Wrap build INSERT + trial UPDATE in a transaction so a partial
      // failure (process kill, DB blip) can't leave a queued build
      // without the corresponding handoff_build_id stamp on the trial,
      // which would let a second click create a second orphan build.
      const client = await pool.connect();
      let buildId: string;
      let lostRace = false;
      try {
        await client.query('BEGIN');
        // The shared insert: queued, and marked as a Harbor build, which is
        // what makes the customer's own publish require a subscription.
        buildId = await insertHarborBuild(client, { customerId, tier, spec: v1Spec, appName });
        // `handoff_build_id IS NULL` makes the stamp a compare-and-set. The
        // check at the top of this handler happens before a slow model call,
        // so two clicks can both get here; the second UPDATE waits for the
        // first to commit, then matches no row and rolls its build back.
        const stamped = await client.query(
          `UPDATE trial_keepers
           SET handoff_build_id = $1, handoff_at = NOW()
           WHERE trial_id = $2 AND handoff_build_id IS NULL`,
          [buildId, trialId]
        );
        if (stamped.rowCount === 0) {
          lostRace = true;
          await client.query('ROLLBACK');
        } else {
          await client.query('COMMIT');
        }
      } catch (txErr) {
        await client.query('ROLLBACK').catch(() => undefined);
        throw txErr;
      } finally {
        client.release();
      }

      if (lostRace) {
        const winner = await pool.query<{ handoff_build_id: string | null }>(
          'SELECT handoff_build_id FROM trial_keepers WHERE trial_id = $1',
          [trialId]
        );
        console.warn(
          '[admin/harbor/dispatch] lost a double-dispatch race for trial=%s; build already dispatched',
          trialId
        );
        res.json({
          buildId: winner.rows[0]?.handoff_build_id ?? null,
          alreadyDispatched: true,
          warnings: [],
        });
        return;
      }

      console.log(
        '[admin/harbor/dispatch] dispatched build=%s for trial=%s by=%s tier=%s warnings=%d',
        buildId,
        trialId,
        adminEmail,
        tier,
        warnings.length
      );

      // Self-learn loop: feed BARREN_SPEC / THIN_SPEC translation warnings
      // into agent_learnings so the operator can see which keeper
      // conversations produced thin specs. Awaited so unit tests can deterministically assert
      // the row was written; the helper itself swallows errors so a
      // learning-write failure cannot fail the dispatch response.
      await recordHarborExtractionLearning(trialId, warnings);

      // Self-learn loop: when Opus's raw archetype guess didn't survive
      // the translator's KNOWN_ARCHETYPES coercion (e.g. Opus invented
      // "music-instructor" instead of selecting "coach-consultant"),
      // record the delta so the architect agent can propose
      // detect_archetype() keyword expansions. The translator drops
      // unknown archetype values, so a mismatch means Opus's vocabulary
      // is broader than the worker's classifier.
      const opusArchetype =
        typeof extraction.spec.archetype === 'string'
          ? extraction.spec.archetype
          : undefined;
      const harborMeta = (v1Spec as { _harbor_meta?: unknown })._harbor_meta;
      const coercedArchetype =
        harborMeta &&
        typeof harborMeta === 'object' &&
        typeof (harborMeta as Record<string, unknown>).archetype_hint === 'string'
          ? ((harborMeta as Record<string, unknown>).archetype_hint as string)
          : undefined;
      await recordHarborArchetypeCorrectionLearning(
        trialId,
        opusArchetype,
        coercedArchetype
      );

      res.json({
        buildId,
        customerId,
        warnings,
        costMicros: extraction.costMicros,
        contextSize: extraction.contextSize,
      });
    } catch (err) {
      // The failures that are not server faults get the answer the self-serve
      // route gives them (dispatchFailureResponse, services/harbor_dispatch.ts):
      // nothing the extraction may read is 409, a failed model call or a reply
      // that is not a spec is 502, a spent daily model-call budget is 503, and
      // no model key on this server is the 503 the chat routes give. None of
      // them has written a customers row, a build or a stamp.
      const answer = dispatchFailureResponse(err);
      if (answer) {
        const body =
          answer.body.error === 'nothing_to_extract'
            ? { ...answer.body, message: `${answer.body.message} ${NOTHING_TO_EXTRACT_OPERATOR_NOTE}` }
            : answer.body;
        res.status(answer.status).json(body);
        return;
      }
      console.error(
        '[admin/harbor/dispatch] failed for trial=%s: %s',
        trialId,
        (err as Error).message
      );
      res.status(500).json({ error: (err as Error).message });
    }
  }
);

/**
 * Added for the operator to the "nothing to extract" answer: why a trial that
 * shows turns in the inbox can still have nothing to build from.
 */
const NOTHING_TO_EXTRACT_OPERATOR_NOTE =
  'Turns that carry the crisis flag, or from which a sacred note was filed, are never sent to the model; this trial has no other turns and no working or voice notes.';

export { detectHarborReadyToHandoff, HARBOR_HANDOFF_PHRASE, HARBOR_HANDOFF_MIN_TURNS };
export default router;
