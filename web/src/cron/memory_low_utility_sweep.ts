/**
 * Weekly low-utility flag sweep for keeper_memory.
 *
 * Closes the loop on memory bloat: as a keeper accumulates rows, the
 * ones that never end up in retrieval (irrelevant facts, classifier
 * over-capture, drifted topics) hang around and dilute ranking quality
 * over time. This sweep flags rows that have never been retrieved
 * (`last_retrieved_at IS NULL`) AND are older than `staleDays` (default
 * 14) AS `low_utility = TRUE`, but ONLY for keepers/trials that have
 * accrued at least `minMemoryRows` rows (default 20). Sparse keepers
 * keep every row.
 *
 * `memory_retrieval.fetchRelevantMemory` then excludes `low_utility`
 * rows from ranking when the keeper has at least
 * COMPANION_MEMORY_LOW_UTILITY_FLOOR (default 10) non-low-utility rows.
 *
 * Read + write against `keeper_memory` only. Idempotent: a re-run on
 * the same day is a no-op for already-flagged rows. Pure SQL, no AI
 * calls; cheap to run as often as we like.
 *
 * `harbor-cron-scheduler.ts` runs this on a timer. This module exposes a
 * single pure-pg sweep function so it stays test-friendly.
 */

import type { Pool } from 'pg';

export interface MemoryLowUtilitySweepOpts {
  pool: Pool;
  /** Minimum non-deleted memory rows for a keeper / trial to be eligible. Default 20. */
  minMemoryRows?: number;
  /** Rows older than this many days with last_retrieved_at IS NULL get flagged. Default 14. */
  staleDays?: number;
  /** Per-run cap on rows flagged. Default 5000. */
  maxFlagged?: number;
  /** Now() override for tests so windows are deterministic. */
  now?: Date;
}

export interface MemoryLowUtilitySweepResult {
  staleDays: number;
  minMemoryRows: number;
  eligibleScopes: number;
  rowsFlagged: number;
}

const DEFAULT_MIN_MEMORY_ROWS = 20;
const DEFAULT_STALE_DAYS = 14;
const DEFAULT_MAX_FLAGGED = 5000;

/**
 * Run a single weekly sweep pass. Safe to invoke on demand; idempotent
 * against the `keeper_memory.low_utility` flag.
 */
export async function runMemoryLowUtilitySweep(
  opts: MemoryLowUtilitySweepOpts
): Promise<MemoryLowUtilitySweepResult> {
  const minMemoryRows = sanitizePositiveInt(
    opts.minMemoryRows,
    DEFAULT_MIN_MEMORY_ROWS
  );
  const staleDays = sanitizePositiveInt(opts.staleDays, DEFAULT_STALE_DAYS);
  const maxFlagged = sanitizePositiveInt(opts.maxFlagged, DEFAULT_MAX_FLAGGED);
  const nowExpr = opts.now
    ? `TIMESTAMP '${opts.now.toISOString()}'`
    : 'NOW()';

  // Eligible-scope filter: bucket non-deleted rows by (keeper_id, trial_id)
  // and keep only buckets >= minMemoryRows. UNION the two id columns into
  // a single eligibility predicate so the UPDATE stays one statement.
  const sql = `
    WITH scope_counts AS (
      SELECT keeper_id, trial_id, COUNT(*) AS n
      FROM keeper_memory
      WHERE deleted_at IS NULL
      GROUP BY keeper_id, trial_id
    ),
    eligible AS (
      SELECT keeper_id, trial_id
      FROM scope_counts
      WHERE n >= $1
    ),
    candidates AS (
      SELECT km.memory_id
      FROM keeper_memory km
      JOIN eligible e
        ON COALESCE(km.keeper_id::text, '') = COALESCE(e.keeper_id::text, '')
       AND COALESCE(km.trial_id::text, '') = COALESCE(e.trial_id::text, '')
      WHERE km.deleted_at IS NULL
        AND km.low_utility = FALSE
        AND km.last_retrieved_at IS NULL
        AND km.created_at < ${nowExpr} - INTERVAL '${staleDays} days'
      LIMIT $2
    )
    UPDATE keeper_memory
       SET low_utility = TRUE
     WHERE memory_id IN (SELECT memory_id FROM candidates)
    RETURNING memory_id
  `;
  const updateRes = await opts.pool.query<{ memory_id: string }>(sql, [
    minMemoryRows,
    maxFlagged,
  ]);

  // Count eligible scopes for the operator-visible result. Cheap +
  // accurate even when no rows ended up flagged this run.
  const eligibleRes = await opts.pool.query<{ scopes: string }>(
    `SELECT COUNT(*) AS scopes FROM (
       SELECT keeper_id, trial_id
       FROM keeper_memory
       WHERE deleted_at IS NULL
       GROUP BY keeper_id, trial_id
       HAVING COUNT(*) >= $1
     ) t`,
    [minMemoryRows]
  );
  const eligibleScopes = Number(eligibleRes.rows[0]?.scopes ?? 0);

  return {
    staleDays,
    minMemoryRows,
    eligibleScopes,
    rowsFlagged: updateRes.rowCount ?? 0,
  };
}

function sanitizePositiveInt(value: number | undefined, fallback: number): number {
  if (typeof value !== 'number') return fallback;
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.floor(value);
}
