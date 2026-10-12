// services/build-start-limits.ts: what one customer may put on the build queue.
//
// A build is not free: it provisions a Railway project and a database, and on
// the v1 engine it also spends model tokens. Without a bound, one account
// could queue builds in a loop. Three limits apply to customers (the admin is
// exempt: it acts on a customer's behalf from a session that is already
// trusted with far more):
//
//   1. MAX_ACTIVE_BUILDS_PER_CUSTOMER builds queued or in progress at once.
//   2. MAX_BUILD_STARTS_PER_DAY starts per rolling 24 hours, counting a
//      conversation's first build, rebuild and revision builds and manual
//      retries together.
//   3. MAX_MANUAL_RETRIES_PER_BUILD manual retries of one build, ever. (The
//      worker's own automatic retries are separate and tracked in
//      builds.retry_count.)
//
// The counts come from the database, so they survive a restart and hold
// across instances, unlike the in-memory request limiters used elsewhere:
// limit 1 from the builds table, limits 2 and 3 from build_start_events, a
// table that records each accepted start and that nothing prunes (the worker
// deletes a customer's older failed builds and their logs, so counting those
// would give the budget back). The check and the writes that consume the
// budget run in one transaction under a per-customer advisory lock; without
// it, N parallel requests would all read "0 active" and all queue a build.
// The marketing promise of unlimited revisions is about not charging for
// them; these limits only stop a runaway loop.
//
// This module is the one statement of the numbers, the lock, the counts and
// the ledger write on the web side. Its callers are every customer-initiated
// start: the retry, rebuild and revision routes (routes/builds.ts) and the
// Harbor dispatch (services/harbor_dispatch.ts). The coordinator's automatic
// rebuild (coordinator/actuators.py, queue_rebuild) repeats the lock key and
// the two counts in Python and is tested against this file's text.

import type { Pool, PoolClient } from 'pg';

export const MAX_ACTIVE_BUILDS_PER_CUSTOMER = 2;
export const MAX_BUILD_STARTS_PER_DAY = 10;
export const MAX_MANUAL_RETRIES_PER_BUILD = 3;

/**
 * What a ledger row records. The values are the ones the kind column's CHECK
 * constraint admits (db/schema.sql, which also says what build id each kind
 * is filed under and which readers count a kind per build).
 */
export type BuildStartKind = 'dispatch' | 'retry' | 'rebuild' | 'revision';

export interface BuildStartBudget {
  active: number;
  startsToday: number;
  retriesOfThisBuild: number;
}

/** Why a customer may not start another build right now, or null when they may. */
export function buildStartRefusal(budget: BuildStartBudget, kind: 'retry' | 'new'): string | null {
  if (kind === 'retry' && budget.retriesOfThisBuild >= MAX_MANUAL_RETRIES_PER_BUILD) {
    return 'This build has already been retried several times. Please write to support@craftandship.io so we can look at what is going wrong.';
  }
  if (budget.active >= MAX_ACTIVE_BUILDS_PER_CUSTOMER) {
    return 'You already have builds in progress. Please wait for one to finish before starting another.';
  }
  if (budget.startsToday >= MAX_BUILD_STARTS_PER_DAY) {
    return 'You have reached the limit for new builds, rebuilds, revisions and retries in one day. Please try again tomorrow, or write to support@craftandship.io.';
  }
  return null;
}

/**
 * The per-customer build-start lock, held to the end of the transaction that
 * takes it. One statement of its own on purpose: under READ COMMITTED each
 * statement sees a fresh snapshot, so the counts that follow observe whatever
 * the previous holder of the lock just committed.
 */
export const BUILD_START_LOCK_SQL = `SELECT pg_advisory_xact_lock(hashtext('cas-build-start'), hashtext($1))`;

type Queryable = Pick<PoolClient, 'query'>;

/**
 * The three counts the limits are stated in. `buildId` is the build whose
 * manual retries are counted; a start that is not a retry of anything (a
 * dispatch) passes null and gets 0 for that count.
 */
export async function readBuildStartBudget(
  db: Queryable,
  customerId: string,
  buildId: string | null
): Promise<BuildStartBudget> {
  const result = await db.query<{ active: number; starts_today: number; build_retries: number }>(
    `SELECT
       (SELECT COUNT(*) FROM builds
         WHERE customer_id = $1
           AND status IN ('queued', 'building', 'testing', 'deploying'))::int AS active,
       (SELECT COUNT(*) FROM build_start_events
         WHERE customer_id = $1
           AND created_at > NOW() - INTERVAL '24 hours')::int AS starts_today,
       (SELECT COUNT(*) FROM build_start_events
         WHERE build_id = $2 AND kind = 'retry')::int AS build_retries`,
    [customerId, buildId]
  );
  const row = result.rows[0];
  return {
    active: Number(row?.active ?? 0),
    startsToday: Number(row?.starts_today ?? 0),
    retriesOfThisBuild: Number(row?.build_retries ?? 0),
  };
}

/** Record an accepted start. Called inside the locked transaction that performs it. */
export async function recordBuildStart(
  db: Queryable,
  customerId: string,
  buildId: string,
  kind: BuildStartKind
): Promise<void> {
  await db.query(
    `INSERT INTO build_start_events (customer_id, build_id, kind) VALUES ($1, $2, $3)`,
    [customerId, buildId, kind]
  );
}

/**
 * Run `fn` in a transaction that holds this customer's build-start lock.
 * pg_advisory_xact_lock is released automatically at COMMIT or ROLLBACK, so a
 * crashed request cannot leave it held.
 */
export async function withCustomerBuildLock<T>(
  pool: Pool,
  customerId: string,
  fn: (client: PoolClient) => Promise<T>
): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(BUILD_START_LOCK_SQL, [customerId]);
    const result = await fn(client);
    await client.query('COMMIT');
    return result;
  } catch (err) {
    try {
      await client.query('ROLLBACK');
    } catch (rollbackErr) {
      console.error('Build-start rollback failed:', (rollbackErr as Error).message);
    }
    throw err;
  } finally {
    client.release();
  }
}
