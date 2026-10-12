// Ops API: machine-to-machine endpoints for the ops agent and Claude Code sessions.
// Auth: Bearer token via OPS_API_KEY env var. No session/cookie auth required.
//
// The key is admin-equivalent: it can requeue, rebuild, fail, and delete builds,
// move a build past payment (queue-bypass), and read customer emails, specs and
// the recent-error buffer. Treat it like the admin password. With OPS_API_KEY
// unset every route answers 503.

import { Router, Request, Response, NextFunction } from 'express';
import pool from '../db';
import { getRecentErrors } from '../utils/recent-errors';
import { timingSafeEqualStrings } from '../middleware/auth';
import { boundedRateLimit, rateLimitIpKey } from '../middleware/companion-rate-limit';
import { forwardAsyncErrors } from '../utils/async-routes';
import { REVIEW_RESET_ASSIGNMENTS } from './builds';

const router = forwardAsyncErrors(Router());

// Rate limit: 60 req/min per client address (generous for M2M but prevents
// runaway loops). It runs before the key check, so it also bounds key guessing.
const opsRateLimit = boundedRateLimit({
  windowMs: 60 * 1000,
  max: 60,
  message: { error: 'Rate limit exceeded.' },
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: (req) => rateLimitIpKey(req.ip),
});

// Bearer token auth middleware
function requireOpsApiKey(req: Request, res: Response, next: NextFunction): void {
  const apiKey = process.env.OPS_API_KEY;
  if (!apiKey) {
    console.error('ops-api: OPS_API_KEY not configured');
    res.status(503).json({ error: 'Ops API not configured.' });
    return;
  }

  const auth = req.headers.authorization;
  if (!auth || !auth.startsWith('Bearer ')) {
    res.status(401).json({ error: 'Missing or invalid API key.' });
    return;
  }

  if (!timingSafeEqualStrings(auth.slice(7), apiKey)) {
    res.status(401).json({ error: 'Missing or invalid API key.' });
    return;
  }

  console.log('ops-api: %s %s from %s', req.method, req.path, req.ip);
  next();
}

router.use(opsRateLimit);
router.use(requireOpsApiKey);

// GET /ops/api/v1/builds: list builds with optional filters
router.get('/v1/builds', async (req: Request, res: Response) => {
  try {
    const statusFilter = req.query.status as string || '';
    const limit = Math.min(parseInt(req.query.limit as string, 10) || 50, 100);
    const since = req.query.since as string || '';

    const conditions: string[] = [];
    const params: (string | number)[] = [];

    if (statusFilter) {
      params.push(statusFilter);
      conditions.push(`b.status = $${params.length}`);
    }
    if (since) {
      params.push(since);
      conditions.push(`b.updated_at >= $${params.length}::timestamp`);
    }

    const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

    params.push(limit);
    const buildsResult = await pool.query(
      `SELECT b.id, b.app_name, b.tier, b.status, b.error_message,
              b.created_at, b.started_at, b.completed_at, b.retry_count,
              b.live_url, b.preview_url, b.railway_project_id, b.railway_service_id,
              b.qa_status, jsonb_array_length(COALESCE(b.screenshots, '[]'::jsonb)) AS screenshot_count,
              b.review_status,
              c.email AS customer_email
       FROM builds b
       JOIN customers c ON b.customer_id = c.id
       ${whereClause}
       ORDER BY b.created_at DESC
       LIMIT $${params.length}`,
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
        COUNT(*) FILTER (WHERE status = 'failed') AS failed
      FROM builds
    `);

    res.json({
      builds: buildsResult.rows,
      stats: statsResult.rows[0],
    });
  } catch (err) {
    console.error('ops-api: Error listing builds:', (err as Error).message);
    res.status(500).json({ error: 'Failed to list builds.' });
  }
});

// GET /ops/api/v1/builds/:buildId, single build detail
router.get('/v1/builds/:buildId', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const result = await pool.query(
      `SELECT b.id, b.app_name, b.tier, b.status, b.spec, b.error_message,
              b.live_url, b.preview_url, b.retry_count, b.railway_project_id, b.railway_service_id,
              b.railway_environment_id, b.created_at, b.started_at, b.completed_at,
              b.qa_status, b.qa_results, b.screenshots, b.total_api_cost_usd,
              b.review_status, b.reviewed_at, b.reviewed_by, b.review_notes,
              (b.build_artifact IS NOT NULL) AS has_build_artifact,
              c.email AS customer_email
       FROM builds b
       JOIN customers c ON b.customer_id = c.id
       WHERE b.id = $1`,
      [buildId]
    );

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    res.json({ build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: Error fetching build:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch build.' });
  }
});

// GET /ops/api/v1/builds/:buildId/errors, error detail + recent messages
router.get('/v1/builds/:buildId/errors', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const buildResult = await pool.query(
      'SELECT id, status, error_message, app_name FROM builds WHERE id = $1',
      [buildId]
    );

    if (buildResult.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const build = buildResult.rows[0] as { id: string; status: string; error_message: string | null; app_name: string | null };

    const messagesResult = await pool.query(
      `SELECT role, content, created_at
       FROM intake_messages
       WHERE build_id = $1
       ORDER BY created_at DESC
       LIMIT 10`,
      [buildId]
    );

    res.json({
      build_id: build.id,
      app_name: build.app_name,
      status: build.status,
      error_message: build.error_message,
      recent_messages: messagesResult.rows,
    });
  } catch (err) {
    console.error('ops-api: Error fetching build errors:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch build errors.' });
  }
});

// POST /ops/api/v1/builds/:buildId/requeue, reset a failed or preview build back to queued.
// The row is about to be built and deployed again, so its review verdict goes
// with the old deployment (REVIEW_RESET_ASSIGNMENTS in routes/builds.ts): a
// preview that was approved does not come back approved.
router.post('/v1/builds/:buildId/requeue', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const result = await pool.query(
      `UPDATE builds
         SET status = 'queued',
             error_message = NULL,
             started_at = NULL,
             completed_at = NULL,
             retry_count = 0,
             ${REVIEW_RESET_ASSIGNMENTS}
       WHERE id = $1 AND status IN ('failed', 'preview')
       RETURNING id, app_name, status`,
      [buildId]
    );

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in failed/preview state.' });
      return;
    }

    console.log('ops-api: Requeued build %s (%s)', buildId, result.rows[0].app_name);
    res.json({ requeued: true, build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: Error requeuing build:', (err as Error).message);
    res.status(500).json({ error: 'Failed to requeue build.' });
  }
});

// POST /ops/api/v1/builds/:buildId/requeue-from-stage, retry a failed build from a specific stage
// Reuses the worker's existing stage-resumption mechanism (error_message marker + build_artifact).
// Saves $3-5 per retry by skipping code generation when only late stages failed.
router.post('/v1/builds/:buildId/requeue-from-stage', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const VALID_STAGES = ['testing', 'deploying'];
    const stage = typeof req.body.stage === 'string' ? req.body.stage.trim().toLowerCase() : '';

    if (!VALID_STAGES.includes(stage)) {
      res.status(400).json({ error: `Invalid stage. Must be one of: ${VALID_STAGES.join(', ')}` });
      return;
    }

    const build = await pool.query(
      `SELECT id, app_name, status, build_artifact IS NOT NULL AS has_artifact
       FROM builds
       WHERE id = $1`,
      [buildId]
    );

    if (build.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const row = build.rows[0];
    if (row.status !== 'failed') {
      res.status(400).json({ error: `Build is in '${row.status}' state, not 'failed'.` });
      return;
    }

    if (!row.has_artifact) {
      res.status(400).json({ error: 'Build has no stored artifact; cannot resume from stage. Use full requeue instead.' });
      return;
    }

    // Guarded on the status that was just read: a build that left `failed`
    // in the meantime (someone else requeued it and the worker claimed it)
    // must not be pulled back to `queued` underneath the worker.
    const result = await pool.query(
      `UPDATE builds
         SET status = 'queued',
             error_message = $2,
             started_at = NULL,
             completed_at = NULL,
             ${REVIEW_RESET_ASSIGNMENTS}
       WHERE id = $1 AND status = 'failed'
       RETURNING id, app_name, status`,
      [buildId, `Requeued: resuming from ${stage} stage`]
    );

    if (result.rows.length === 0) {
      res.status(409).json({ error: 'Build is no longer in the failed state; nothing was requeued.' });
      return;
    }

    console.log('ops-api: Requeued build %s (%s) from %s stage', buildId, result.rows[0].app_name, stage);
    res.json({ requeued: true, stage, build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: Error requeuing build from stage:', (err as Error).message);
    res.status(500).json({ error: 'Failed to requeue build from stage.' });
  }
});

// POST /ops/api/v1/builds/:buildId/rebuild, create a new build from an existing build's spec
router.post('/v1/builds/:buildId/rebuild', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const changeRequest = (typeof req.body.rebuild_request === 'string')
      ? req.body.rebuild_request.trim().substring(0, 2000) || null
      : null;

    const original = await pool.query(
      `SELECT id, customer_id, tier, spec, app_name
       FROM builds
       WHERE id = $1 AND status IN ('live', 'failed', 'preview')`,
      [buildId]
    );

    if (original.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in live/failed/preview state.' });
      return;
    }

    const { customer_id, tier, spec, app_name } = original.rows[0];

    if (!spec) {
      res.status(400).json({ error: 'Original build has no spec to rebuild from.' });
      return;
    }

    const result = await pool.query(
      `INSERT INTO builds (customer_id, tier, status, spec, app_name, parent_build_id, rebuild_request)
       VALUES ($1, $2, 'queued', $3, $4, $5, $6)
       RETURNING id, app_name, status`,
      [customer_id, tier, spec, app_name, buildId, changeRequest]
    );

    console.log('ops-api: Rebuilt build %s -> %s (%s)', buildId, result.rows[0].id, app_name);
    res.json({ rebuilt: true, original_build_id: buildId, new_build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: Error rebuilding build:', (err as Error).message);
    res.status(500).json({ error: 'Failed to rebuild.' });
  }
});

// POST /ops/api/v1/builds/:buildId/queue-bypass, skip payment and move build to queued (admin-only).
// Only a build that belongs to a customer: the worker claims a queued build
// through a join to its customer, so a draft with no customer (an anonymous
// /try draft) would sit at `queued` forever, never built and never failed.
router.post('/v1/builds/:buildId/queue-bypass', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const result = await pool.query(
      `UPDATE builds
         SET status = 'queued',
             updated_at = NOW(),
             ${REVIEW_RESET_ASSIGNMENTS}
       WHERE id = $1 AND status IN ('payment_pending', 'intake') AND customer_id IS NOT NULL
       RETURNING id, app_name, status, tier, customer_id`,
      [buildId]
    );

    if (result.rows.length === 0) {
      res.status(404).json({
        error: 'Build not found, not in payment_pending/intake state, or it has no customer (the worker could never claim it).',
      });
      return;
    }

    console.log('ops-api: Queue-bypassed build %s (%s)', buildId, result.rows[0].app_name);
    res.json({ queued: true, build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: Error queue-bypassing build:', (err as Error).message);
    res.status(500).json({ error: 'Failed to queue bypass build.' });
  }
});

// POST /ops/api/v1/builds/:buildId/fail, force a stuck build to failed status
router.post('/v1/builds/:buildId/fail', async (req: Request, res: Response) => {
  try {
    const { buildId } = req.params;
    const reason = (typeof req.body.reason === 'string')
      ? req.body.reason.trim().substring(0, 2000) || 'Force-failed via ops API'
      : 'Force-failed via ops API';

    const result = await pool.query(
      `UPDATE builds
         SET status = 'failed',
             error_message = $2,
             completed_at = NOW()
       WHERE id = $1 AND status IN ('building', 'testing', 'deploying', 'queued')
       RETURNING id, app_name, status`,
      [buildId, reason]
    );

    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not in an active state.' });
      return;
    }

    console.log('ops-api: Force-failed build %s (%s): %s', buildId, result.rows[0].app_name, reason);
    res.json({ failed: true, build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: Error force-failing build:', (err as Error).message);
    res.status(500).json({ error: 'Failed to update build.' });
  }
});

// GET /ops/api/v1/stats: dashboard summary
router.get('/v1/stats', async (_req: Request, res: Response) => {
  try {
    const buildStats = await pool.query(`
      SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE status = 'queued') AS queued,
        COUNT(*) FILTER (WHERE status = 'building') AS building,
        COUNT(*) FILTER (WHERE status = 'live') AS live,
        COUNT(*) FILTER (WHERE status = 'failed') AS failed,
        COUNT(*) FILTER (WHERE status = 'failed' AND updated_at >= NOW() - INTERVAL '24 hours') AS failed_last_24h
      FROM builds
    `);

    const customerStats = await pool.query(`
      SELECT
        COUNT(*) AS total,
        COUNT(DISTINCT b.customer_id) FILTER (WHERE b.status = 'live') AS with_live_apps
      FROM customers c
      LEFT JOIN builds b ON b.customer_id = c.id
    `);

    const emailStats = await pool.query(`
      SELECT
        COUNT(*) FILTER (WHERE status = 'sent') AS sent_today,
        COUNT(*) FILTER (WHERE status = 'failed') AS failed_today
      FROM email_log
      WHERE created_at >= CURRENT_DATE
    `);

    const costStats = await pool.query(`
      SELECT
        SUM(total_api_cost_usd) FILTER (WHERE completed_at >= CURRENT_DATE) AS daily_total,
        SUM(total_api_cost_usd) FILTER (WHERE completed_at >= NOW() - INTERVAL '7 days') AS weekly_total,
        AVG(total_api_cost_usd) FILTER (WHERE tier = 'build' AND total_api_cost_usd IS NOT NULL) AS avg_cost_build,
        AVG(total_api_cost_usd) FILTER (WHERE tier = 'grow' AND total_api_cost_usd IS NOT NULL) AS avg_cost_grow,
        AVG(total_api_cost_usd) FILTER (WHERE tier = 'scale' AND total_api_cost_usd IS NOT NULL) AS avg_cost_scale,
        (SELECT id FROM builds WHERE total_api_cost_usd IS NOT NULL AND completed_at >= NOW() - INTERVAL '7 days' ORDER BY total_api_cost_usd DESC LIMIT 1) AS top_cost_build_id,
        (SELECT total_api_cost_usd FROM builds WHERE total_api_cost_usd IS NOT NULL AND completed_at >= NOW() - INTERVAL '7 days' ORDER BY total_api_cost_usd DESC LIMIT 1) AS top_cost_amount
      FROM builds
    `);

    res.json({
      builds: buildStats.rows[0],
      customers: customerStats.rows[0],
      emails: emailStats.rows[0],
      api_costs: costStats.rows[0],
    });
  } catch (err) {
    console.error('ops-api: Error fetching stats:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch stats.' });
  }
});

// GET /ops/api/v1/intake-analytics: conversation metrics for the data flywheel
router.get('/v1/intake-analytics', async (_req: Request, res: Response) => {
  try {
    const conversationStats = await pool.query(`
      SELECT
        b.id AS build_id,
        b.app_name,
        b.tier,
        b.status AS build_outcome,
        b.created_at,
        COUNT(im.id) AS message_count,
        COUNT(im.id) FILTER (WHERE im.role = 'user') AS user_messages,
        COUNT(im.id) FILTER (WHERE im.role = 'assistant') AS assistant_messages,
        EXTRACT(EPOCH FROM (MAX(im.created_at) - MIN(im.created_at))) AS conversation_duration_seconds,
        CASE WHEN b.spec IS NOT NULL THEN true ELSE false END AS spec_extracted
      FROM builds b
      LEFT JOIN intake_messages im ON im.build_id = b.id
      GROUP BY b.id
      ORDER BY b.created_at DESC
      LIMIT 50
    `);

    const aggregates = await pool.query(`
      SELECT
        COUNT(DISTINCT b.id) AS total_conversations,
        AVG(msg_counts.msg_count)::numeric(10,1) AS avg_messages_per_conversation,
        COUNT(DISTINCT b.id) FILTER (WHERE b.status = 'live') AS successful_builds,
        COUNT(DISTINCT b.id) FILTER (WHERE b.status = 'failed') AS failed_builds,
        COUNT(DISTINCT b.id) FILTER (WHERE b.status = 'intake') AS abandoned_intakes
      FROM builds b
      LEFT JOIN (
        SELECT build_id, COUNT(*) AS msg_count
        FROM intake_messages
        GROUP BY build_id
      ) msg_counts ON msg_counts.build_id = b.id
    `);

    res.json({
      conversations: conversationStats.rows,
      aggregates: aggregates.rows[0],
    });
  } catch (err) {
    console.error('ops-api: Error fetching intake analytics:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch intake analytics.' });
  }
});

// GET /ops/api/v1/costs: detailed cost breakdown for coordinator agents
router.get('/v1/costs', async (req: Request, res: Response) => {
  try {
    const { getCostData } = await import('./admin');
    const days = Math.min(90, Math.max(1, parseInt(req.query.days as string, 10) || 30));
    const data = await getCostData(days);
    res.json(data);
  } catch (err) {
    console.error('ops-api: Error fetching costs:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch costs.' });
  }
});

// POST /ops/api/v1/costs/platform: record platform cost (from console scrape or manual entry)
router.post('/v1/costs/platform', async (req: Request, res: Response) => {
  try {
    const { date, total_usd } = req.body as { date: string; total_usd: number };

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

    console.log('ops-api: Recorded platform cost: %s = $%s', date, cost.toFixed(4));
    res.json({ ok: true, date, total_usd: cost });
  } catch (err) {
    console.error('ops-api: Error recording platform cost:', (err as Error).message);
    res.status(500).json({ error: 'Failed to record platform cost.' });
  }
});

// GET /ops/api/v1/errors: recent server errors (in-memory buffer)
router.get('/v1/errors', (_req: Request, res: Response) => {
  res.json({ errors: getRecentErrors() });
});

// POST /ops/api/v1/builds/cleanup: remove duplicate builds for an app_name
//
// app_name is not unique across customers (two people can name their site the
// same thing), so "duplicate" means an older build of the same name that
// belongs to the SAME customer. The newest build of each customer is kept;
// another customer's build that happens to share the name is never touched.
router.post('/v1/builds/cleanup', async (req: Request, res: Response) => {
  const { app_name } = req.body as { app_name: string };
  if (!app_name || typeof app_name !== 'string') {
    res.status(400).json({ error: 'app_name is required.' });
    return;
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const builds = await client.query(
      `SELECT id, status, created_at,
              ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) AS owner_rank
       FROM builds WHERE app_name = $1 ORDER BY created_at DESC`,
      [app_name.trim()]
    );

    const rows = builds.rows as Array<{ id: string; owner_rank: string | number }>;
    const deleteIds = rows.filter((r) => Number(r.owner_rank) > 1).map((r) => r.id);
    const keptIds = rows.filter((r) => Number(r.owner_rank) === 1).map((r) => r.id);

    if (deleteIds.length === 0) {
      await client.query('ROLLBACK');
      res.json({ deleted: 0, kept: rows.length, message: 'Nothing to clean up.' });
      return;
    }

    await client.query('DELETE FROM intake_messages WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('DELETE FROM build_logs WHERE build_id = ANY($1::uuid[])', [deleteIds]);
    await client.query('DELETE FROM feature_messages WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE build_id = ANY($1))', [deleteIds]);
    await client.query('DELETE FROM feature_requests WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('DELETE FROM dead_letter_builds WHERE build_id = ANY($1)', [deleteIds]);
    // Clear parent_build_id references from other builds pointing to ones we're deleting
    await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = ANY($1)', [deleteIds]);
    const result = await client.query('DELETE FROM builds WHERE id = ANY($1)', [deleteIds]);

    await client.query('COMMIT');
    res.json({
      deleted: result.rowCount,
      kept: keptIds[0],
      kept_ids: keptIds,
      message: `Cleaned up ${result.rowCount} duplicate(s) for "${app_name}".`,
    });
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('Ops cleanup error:', (err as Error).message);
    res.status(500).json({ error: 'Cleanup failed.' });
  } finally {
    client.release();
  }
});

// POST /ops/api/v1/builds/cleanup-abandoned: remove nameless intake builds older than 24h
router.post('/v1/builds/cleanup-abandoned', async (_req: Request, res: Response) => {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const abandoned = await client.query(
      `SELECT id FROM builds WHERE app_name IS NULL AND status = 'intake' AND created_at < NOW() - INTERVAL '24 hours'`
    );

    if (abandoned.rows.length === 0) {
      await client.query('ROLLBACK');
      res.json({ deleted: 0, message: 'No abandoned intakes.' });
      return;
    }

    const deleteIds = abandoned.rows.map((r: { id: string }) => r.id);
    await client.query('DELETE FROM intake_messages WHERE build_id = ANY($1)', [deleteIds]);
    await client.query('DELETE FROM build_logs WHERE build_id = ANY($1::uuid[])', [deleteIds]);
    await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = ANY($1)', [deleteIds]);
    const result = await client.query('DELETE FROM builds WHERE id = ANY($1)', [deleteIds]);

    await client.query('COMMIT');
    res.json({ deleted: result.rowCount, message: `Removed ${result.rowCount} abandoned intake(s).` });
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('Ops abandoned cleanup error:', (err as Error).message);
    res.status(500).json({ error: 'Cleanup failed.' });
  } finally {
    client.release();
  }
});

// DELETE /ops/api/v1/builds/:buildId, permanently delete a build and cascaded data
router.delete('/v1/builds/:buildId', async (req: Request, res: Response) => {
  const client = await pool.connect();
  try {
    const { buildId } = req.params;

    // Verify build exists
    const check = await client.query('SELECT id, app_name, status FROM builds WHERE id = $1', [buildId]);
    if (check.rows.length === 0) {
      res.status(404).json({ error: 'Build not found.' });
      return;
    }

    const build = check.rows[0] as { id: string; app_name: string; status: string };

    await client.query('BEGIN');
    // Cascade delete related records
    await client.query('UPDATE builds SET parent_build_id = NULL WHERE parent_build_id = $1', [buildId]);
    await client.query('DELETE FROM feature_messages WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE build_id = $1)', [buildId]);
    await client.query('DELETE FROM feature_requests WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM intake_messages WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM build_logs WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM dead_letter_builds WHERE build_id = $1', [buildId]);
    await client.query('DELETE FROM builds WHERE id = $1', [buildId]);
    await client.query('COMMIT');

    console.log('ops-api: Deleted build %s (%s, %s)', buildId, build.app_name, build.status);
    res.json({ deleted: true, build: { id: buildId, app_name: build.app_name, status: build.status } });
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    console.error('ops-api: Error deleting build:', (err as Error).message);
    res.status(500).json({ error: 'Delete failed.' });
  } finally {
    client.release();
  }
});

// GET /ops/api/v1/coordinator: coordinator task history for machine access
router.get('/v1/coordinator', async (req: Request, res: Response) => {
  try {
    const { getCoordinatorData } = await import('./admin');
    const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
    const filters = {
      agent: (req.query.agent as string) || '',
      task_category: (req.query.task_category as string) || '',
      status: (req.query.status as string) || '',
      date_from: (req.query.date_from as string) || '',
      date_to: (req.query.date_to as string) || '',
    };
    const data = await getCoordinatorData(page, filters);
    res.json(data);
  } catch (err) {
    console.error('ops-api: Error fetching coordinator:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch coordinator tasks.' });
  }
});

// GET /ops/api/v1/coordinator/health: lightweight coordinator liveness check
// Returns coordinator_enabled from worker_heartbeat + last tick recency from coordinator_ticks.
// Lets dashboards show accurate per-env coordinator status without
// inferring from agent_tasks (which can be stale for days when no work is detected).
router.get('/v1/coordinator/health', async (req: Request, res: Response) => {
  try {
    const [heartbeatResult, tickResult, runningResult] = await Promise.all([
      pool.query(
        `SELECT coordinator_enabled, started_at FROM worker_heartbeat WHERE id = 1`
      ),
      pool.query(
        `SELECT started_at, tasks_dispatched FROM coordinator_ticks
         ORDER BY started_at DESC LIMIT 1`
      ),
      pool.query(
        `SELECT COUNT(*)::int AS running FROM agent_tasks WHERE status = 'running'`
      ),
    ]);

    const hb = heartbeatResult.rows[0] || {};
    const tick = tickResult.rows[0] || null;
    const running = runningResult.rows[0]?.running ?? 0;

    const coordinatorEnabled: boolean = hb.coordinator_enabled ?? false;
    const lastTickAt: string | null = tick ? (tick.started_at as Date).toISOString() : null;
    const tickAgeMinutes: number | null = lastTickAt
      ? Math.round((Date.now() - new Date(lastTickAt).getTime()) / 60000)
      : null;
    // Active = enabled AND last tick within 2x the 30-min interval (60 min window)
    const isActive = coordinatorEnabled && tickAgeMinutes !== null && tickAgeMinutes < 60;

    res.json({
      enabled: coordinatorEnabled,
      last_tick_at: lastTickAt,
      tick_age_minutes: tickAgeMinutes,
      tasks_dispatched_last_tick: tick?.tasks_dispatched ?? 0,
      running_tasks: running,
      is_active: isActive,
    });
  } catch (err) {
    console.error('ops-api: Error fetching coordinator health:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch coordinator health.' });
  }
});

// GET /ops/api/v1/analytics: marketing funnel and page view analytics
router.get('/v1/analytics', async (req: Request, res: Response) => {
  try {
    const days = Math.min(90, Math.max(1, parseInt(req.query.days as string, 10) || 30));

    // Conversion funnel: signup -> intake -> payment -> live
    const funnel = await pool.query(`
      WITH since AS (SELECT NOW() - ($1 || ' days')::interval AS cutoff)
      SELECT
        (SELECT COUNT(*) FROM customers WHERE created_at >= (SELECT cutoff FROM since)) AS signups,
        (SELECT COUNT(DISTINCT customer_id) FROM builds
         WHERE created_at >= (SELECT cutoff FROM since) AND status != 'intake') AS started_intake,
        (SELECT COUNT(DISTINCT customer_id) FROM builds
         WHERE created_at >= (SELECT cutoff FROM since)
           AND status IN ('queued','building','testing','deploying','preview','live','failed')) AS reached_payment,
        (SELECT COUNT(DISTINCT customer_id) FROM builds
         WHERE created_at >= (SELECT cutoff FROM since) AND status = 'live') AS reached_live
    `, [days]);

    // UTM attribution breakdown
    const utmBreakdown = await pool.query(`
      SELECT
        COALESCE(utm_source, 'direct') AS source,
        COALESCE(utm_medium, 'none') AS medium,
        COALESCE(utm_campaign, 'none') AS campaign,
        COUNT(*) AS signups
      FROM customers
      WHERE created_at >= NOW() - ($1 || ' days')::interval
      GROUP BY utm_source, utm_medium, utm_campaign
      ORDER BY signups DESC
      LIMIT 50
    `, [days]);

    // Page view summary: top pages by views
    const topPages = await pool.query(`
      SELECT path, COUNT(*) AS views, COUNT(DISTINCT session_id) AS unique_sessions
      FROM page_views
      WHERE created_at >= NOW() - ($1 || ' days')::interval
      GROUP BY path
      ORDER BY views DESC
      LIMIT 20
    `, [days]);

    // Daily page views trend
    const dailyViews = await pool.query(`
      SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS views
      FROM page_views
      WHERE created_at >= NOW() - ($1 || ' days')::interval
      GROUP BY day
      ORDER BY day
    `, [days]);

    // UTM source page views (which channels drive traffic)
    const utmTraffic = await pool.query(`
      SELECT
        COALESCE(utm_source, 'direct') AS source,
        COUNT(*) AS views
      FROM page_views
      WHERE created_at >= NOW() - ($1 || ' days')::interval
      GROUP BY utm_source
      ORDER BY views DESC
      LIMIT 20
    `, [days]);

    res.json({
      days,
      funnel: funnel.rows[0],
      utm_attribution: utmBreakdown.rows,
      top_pages: topPages.rows,
      daily_views: dailyViews.rows,
      utm_traffic: utmTraffic.rows,
    });
  } catch (err) {
    console.error('ops-api: Error fetching analytics:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch analytics.' });
  }
});

// === Executor API ===

// POST /ops/api/v1/executor/heartbeat: status update from local executor
router.post('/v1/executor/heartbeat', async (req: Request, res: Response) => {
  const { timestamp, status, current_item, skipped_reason } = req.body;
  try {
    await pool.query(
      `INSERT INTO executor_runs (status, roadmap_item_id, skipped_reason, started_at)
       VALUES ($1, $2, $3, $4)`,
      [status || 'idle', current_item || null, skipped_reason || null, timestamp || new Date().toISOString()]
    );
    res.json({ ok: true });
  } catch (err) {
    console.error('Executor heartbeat error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to record heartbeat' });
  }
});

// POST /ops/api/v1/executor/result: completed item result
router.post('/v1/executor/result', async (req: Request, res: Response) => {
  const { roadmap_item_id, roadmap_item_title, status, result, cost_usd, commit_sha, duration_s, error_message } = req.body;
  try {
    const dur = parseInt(duration_s, 10) || 0;
    await pool.query(
      `INSERT INTO executor_runs (status, roadmap_item_id, roadmap_item_title, result, cost_usd, commit_sha, duration_s, error_message, started_at, completed_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() - ($7::int * interval '1 second'), NOW())`,
      [status, roadmap_item_id, roadmap_item_title, result, cost_usd || 0, commit_sha || null, dur, error_message || null]
    );
    res.json({ ok: true });
  } catch (err) {
    console.error('Executor result error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to record result' });
  }
});

// POST /ops/api/v1/executor/question: ask a non-blocking question
router.post('/v1/executor/question', async (req: Request, res: Response) => {
  const { roadmap_item_id, question, context } = req.body;
  if (!question) {
    res.status(400).json({ error: 'question is required' });
    return;
  }
  try {
    const result = await pool.query(
      `INSERT INTO executor_questions (roadmap_item_id, question, context)
       VALUES ($1, $2, $3) RETURNING id`,
      [roadmap_item_id || null, question, JSON.stringify(context || {})]
    );
    res.json({ ok: true, id: result.rows[0].id });
  } catch (err) {
    console.error('Executor question error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to record question' });
  }
});

// GET /ops/api/v1/executor/questions: get questions (optionally filtered by status)
router.get('/v1/executor/questions', async (req: Request, res: Response) => {
  const status = req.query.status as string || null;
  try {
    const query = status
      ? 'SELECT * FROM executor_questions WHERE status = $1 ORDER BY created_at DESC LIMIT 50'
      : 'SELECT * FROM executor_questions ORDER BY created_at DESC LIMIT 50';
    const params = status ? [status] : [];
    const result = await pool.query(query, params);
    res.json(result.rows);
  } catch (err) {
    console.error('Executor questions error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch questions' });
  }
});

// POST /ops/api/v1/executor/sweep-zombies: mark stuck running/working/pr_created rows as failed.
// Called by cas-roadmap-loop.sh on startup so the dashboard reflects reality
// after a crash. Idempotent: filters on age so repeated calls in the same
// window are no-ops. Uses COALESCE(completed_at, started_at, created_at) as
// the age anchor so newly-inserted rows are never swept.
router.post('/v1/executor/sweep-zombies', async (req: Request, res: Response) => {
  const raw = (req.body && (req.body.max_age_minutes as unknown)) ?? 60;
  const parsed = typeof raw === 'number' ? raw : parseInt(String(raw), 10);
  const maxAgeMin = Number.isFinite(parsed) && parsed > 0 ? Math.min(parsed, 24 * 60) : 60;
  try {
    const result = await pool.query(
      `UPDATE executor_runs
       SET status = 'failed',
           error_message = CASE
             WHEN error_message IS NULL OR error_message = ''
               THEN 'zombie-swept: row stuck in ' || status || ' > ' || $1::int || 'min (previous executor crash)'
             ELSE error_message || '; zombie-swept: was ' || status
           END,
           completed_at = NOW()
       WHERE status IN ('running', 'working', 'pr_created')
         AND COALESCE(completed_at, started_at, created_at) < NOW() - ($1::int * interval '1 minute')
       RETURNING id, roadmap_item_id, status`,
      [maxAgeMin]
    );
    const swept = result.rowCount ?? 0;
    if (swept > 0) {
      console.log(
        'ops-api: sweep-zombies marked %d row(s) failed (max_age_minutes=%d)',
        swept,
        maxAgeMin
      );
    }
    res.json({ ok: true, swept, max_age_minutes: maxAgeMin });
  } catch (err) {
    console.error('Executor sweep-zombies error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to sweep zombies' });
  }
});

// GET /ops/api/v1/executor/status: current executor state (latest heartbeat + recent runs)
router.get('/v1/executor/status', async (_req: Request, res: Response) => {
  try {
    const latest = await pool.query(
      'SELECT * FROM executor_runs ORDER BY created_at DESC LIMIT 1'
    );
    const recent = await pool.query(
      `SELECT * FROM executor_runs WHERE status IN ('completed', 'failed') ORDER BY created_at DESC LIMIT 10`
    );
    const pending_q = await pool.query(
      "SELECT COUNT(*) FROM executor_questions WHERE status = 'pending'"
    );
    res.json({
      latest: latest.rows[0] || null,
      recent_runs: recent.rows,
      pending_questions: parseInt(pending_q.rows[0].count, 10),
    });
  } catch (err) {
    console.error('Executor status error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to fetch executor status' });
  }
});

// POST /v1/gallery/:buildId/optin, toggle gallery opt-in for a build (admin/ops use)
router.post('/v1/gallery/:buildId/optin', async (req: Request, res: Response) => {
  const { buildId } = req.params;
  const description = req.body.gallery_description || null;
  const category = req.body.gallery_category || null;
  try {
    const result = await pool.query(
      `UPDATE builds
       SET gallery_opted_in = NOT COALESCE(gallery_opted_in, FALSE),
           gallery_description = COALESCE($1, gallery_description),
           gallery_category = COALESCE($2, gallery_category)
       WHERE id = $3 AND status IN ('live', 'preview')
       RETURNING id, app_name, gallery_opted_in`,
      [description, category, buildId]
    );
    if (result.rows.length === 0) {
      res.status(404).json({ error: 'Build not found or not live/preview.' });
      return;
    }
    res.json({ ok: true, build: result.rows[0] });
  } catch (err) {
    console.error('ops-api: gallery optin error:', (err as Error).message);
    res.status(500).json({ error: 'Failed to toggle gallery opt-in.' });
  }
});

export default router;
