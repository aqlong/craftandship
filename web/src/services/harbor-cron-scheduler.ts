// web/src/services/harbor-cron-scheduler.ts
// Wires the four weekly Harbor self-learn cron sweeps into the running
// process. Gated by CAS_HARBOR_ENABLED so a careless deploy before the
// Harbor surface goes live never starts the cron loop.
//
// Sweeps:
//  - topic_kb_sweep: weekly Haiku sweep of companion_turns -> support_kb
//  - memory_low_utility_sweep: weekly keeper_memory low-utility flag
//  - rail_flag_sweep: weekly rail-flag aggregation -> agent_learnings
//  - persona_learning_digest: weekly email of unapplied persona learnings
//
// All default to a 7-day interval; each can be overridden via env var for
// ops or testing (values under one minute are ignored, see
// `resolveIntervalMs`).
//
// What "weekly" means here
// ------------------------
// A sweep runs when its interval has passed since its last run, and the last
// run is a row in the database, not a timer in this process. Each scheduler
// asks 60 seconds after boot (so the server has settled) and then once an
// hour (or once per interval, if that is shorter) whether its sweep is due,
// by trying to claim the run in `platform_metadata` (`claimSweepRun`). The
// claim is one statement that succeeds only if no run was claimed within the
// interval, so:
//
//  - a restart or a deploy does not run the sweeps again: the row says they
//    ran, and the next run is still a week after that one;
//  - with more than one web replica, exactly one of them gets the claim and
//    runs the sweep; the others see it taken;
//  - on a database that has never run a sweep there is no row, and the first
//    check after boot runs it.
//
// Limits, stated plainly:
//  - The claim is taken before the sweep runs. A sweep that fails, or a
//    process that is stopped in the middle of one, is not retried until the
//    next interval; the failure is logged.
//  - If the claim cannot be read or written (database down, table missing)
//    the sweep does not run and the error is logged; the next check tries
//    again.
//  - Timing is as coarse as the check: a sweep runs up to an hour after it
//    becomes due. Within one process a check is skipped if the previous one
//    has not finished (`guardedRun`).

import pool from '../db';
import { runTopicKbSweep } from '../cron/topic_kb_sweep';
import { runMemoryLowUtilitySweep } from '../cron/memory_low_utility_sweep';
import { runRailFlagSweep } from '../cron/rail_flag_sweep';
import { runPersonaLearningDigest } from '../cron/persona_learning_digest';

const WEEKLY_MS = 7 * 24 * 60 * 60 * 1000;
const FIRST_RUN_DELAY_MS = 60_000;
const MIN_INTERVAL_MS = 60_000;
/** How often a scheduler asks whether its sweep is due. */
export const SWEEP_CHECK_EVERY_MS = 60 * 60 * 1000;

/**
 * Parse an interval from an env var. Anything that is not a finite number of
 * at least one minute falls back to the default. This matters because
 * `setInterval(fn, NaN)` and `setInterval(fn, 0)` both fire about every
 * millisecond in Node, which for these sweeps means a hot loop of database
 * scans and model calls from a typo such as `RAIL_FLAG_SWEEP_INTERVAL_MS=7d`.
 */
export function resolveIntervalMs(raw: string | undefined, fallback: number): number {
  const n = parseInt(raw ?? '', 10);
  return Number.isFinite(n) && n >= MIN_INTERVAL_MS ? n : fallback;
}

/**
 * Wrap a sweep so overlapping invocations are dropped instead of stacked
 * (a slow run must not be joined by the next tick's run).
 */
export function guardedRun(name: string, fn: () => Promise<void>): () => Promise<void> {
  let running = false;
  return async () => {
    if (running) {
      console.warn(`[${name}] previous run still in progress; skipping this tick`);
      return;
    }
    running = true;
    try {
      await fn();
    } finally {
      running = false;
    }
  };
}

/** The platform_metadata key that records a sweep's last claimed run. */
export function sweepRunKey(name: string): string {
  return `harbor_sweep_last_run:${name}`;
}

/**
 * Claim one run of a sweep, if one is due. True means this caller holds the
 * run and must do it; false means a run was claimed less than `intervalMs`
 * ago (by this process before a restart, or by another replica).
 *
 * One statement, so two callers cannot both get it: the insert creates the
 * row on a database that has never run the sweep, and otherwise the update
 * applies only when the stored time is at least an interval old. A second
 * caller arriving at the same moment waits for the first to commit and then
 * finds the time fresh. Times are the database's clock (NOW()), so replicas
 * with different clocks agree. `value` records when the claim was made, for
 * a person reading the table; the decision uses `updated_at`.
 */
export async function claimSweepRun(
  db: { query: typeof pool.query },
  name: string,
  intervalMs: number
): Promise<boolean> {
  const res = await db.query(
    `INSERT INTO platform_metadata (key, value, updated_at)
     VALUES ($1, 'last run claimed ' || NOW()::text, NOW())
     ON CONFLICT (key) DO UPDATE
       SET value = EXCLUDED.value, updated_at = NOW()
       WHERE platform_metadata.updated_at <= NOW() - ($2::double precision * INTERVAL '1 millisecond')
     RETURNING key`,
    [sweepRunKey(name), intervalMs]
  );
  return res.rows.length > 0;
}

export interface HarborCronIntervals {
  topicKb?: NodeJS.Timeout;
  memoryLowUtility?: NodeJS.Timeout;
  railFlag?: NodeJS.Timeout;
  personaLearningDigest?: NodeJS.Timeout;
}

async function safeRunTopicKbSweep(): Promise<void> {
  try {
    const result = await runTopicKbSweep({ pool });
    if (result.insertedCount > 0 || result.clustersExtracted > 0) {
      console.log(
        `[topic-kb-sweep] inserted=${result.insertedCount} ` +
          `extracted=${result.clustersExtracted} ` +
          `dupes=${result.skippedDuplicateCount} ` +
          `invalid=${result.skippedInvalidCount} ` +
          `turns=${result.turnsScanned} ` +
          `cost=$${(result.totalCostMicros / 1_000_000).toFixed(4)}`
      );
    }
  } catch (err) {
    console.error('[topic-kb-sweep] error:', (err as Error).message);
  }
}

async function safeRunMemoryLowUtilitySweep(): Promise<void> {
  try {
    const result = await runMemoryLowUtilitySweep({ pool });
    if (result.rowsFlagged > 0) {
      console.log(
        `[memory-low-utility-sweep] flagged=${result.rowsFlagged} ` +
          `eligibleScopes=${result.eligibleScopes} ` +
          `staleDays=${result.staleDays} ` +
          `minRows=${result.minMemoryRows}`
      );
    }
  } catch (err) {
    console.error('[memory-low-utility-sweep] error:', (err as Error).message);
  }
}

async function safeRunRailFlagSweep(): Promise<void> {
  try {
    const result = await runRailFlagSweep({ pool });
    if (result.findingsRecorded > 0) {
      console.log(
        `[rail-flag-sweep] recorded=${result.findingsRecorded} ` +
          `population=${result.populationFindings} ` +
          `perScope=${result.perScopeFindings} ` +
          `turns=${result.totalTurnsScanned}`
      );
    }
  } catch (err) {
    console.error('[rail-flag-sweep] error:', (err as Error).message);
  }
}

async function safeRunPersonaLearningDigest(): Promise<void> {
  try {
    const result = await runPersonaLearningDigest({ pool });
    if (result.surfacedCount > 0) {
      const cats = Object.entries(result.byCategory)
        .map(([k, v]) => `${k}=${v}`)
        .join(' ');
      console.log(
        `[persona-learning-digest] surfaced=${result.surfacedCount} ` +
          `emailed=${result.emailed} ${cats}`
      );
    }
  } catch (err) {
    console.error(
      '[persona-learning-digest] error:',
      (err as Error).message
    );
  }
}

/**
 * Start one sweep's scheduler: check 60 seconds from now and then on a timer
 * whether a run is due, and run the sweep when this process gets the claim.
 * Returns the timer so the caller can clear it on shutdown.
 */
function startSweepScheduler(
  name: string,
  intervalMs: number,
  sweep: () => Promise<void>
): NodeJS.Timeout {
  const checkEveryMs = Math.min(intervalMs, SWEEP_CHECK_EVERY_MS);
  console.log(
    `[${name}] Started (interval: ${Math.round(intervalMs / 60000)}m, checked every ${Math.round(checkEveryMs / 60000)}m)`
  );
  const check = guardedRun(name, async () => {
    let due: boolean;
    try {
      due = await claimSweepRun(pool, name, intervalMs);
    } catch (err) {
      console.error(`[${name}] could not check whether a run is due; not running:`, (err as Error).message);
      return;
    }
    if (due) await sweep();
  });
  const tick = (): void => {
    check().catch((err) => console.error(`[${name}] scheduler tick failed:`, (err as Error).message));
  };
  setTimeout(tick, FIRST_RUN_DELAY_MS);
  return setInterval(tick, checkEveryMs);
}

export function startTopicKbSweepScheduler(intervalMs?: number): NodeJS.Timeout {
  const ms =
    intervalMs ??
    resolveIntervalMs(process.env.TOPIC_KB_SWEEP_INTERVAL_MS, WEEKLY_MS);
  return startSweepScheduler('topic-kb-sweep', ms, safeRunTopicKbSweep);
}

export function startMemoryLowUtilitySweepScheduler(
  intervalMs?: number
): NodeJS.Timeout {
  const ms =
    intervalMs ??
    resolveIntervalMs(process.env.MEMORY_LOW_UTILITY_SWEEP_INTERVAL_MS, WEEKLY_MS);
  return startSweepScheduler('memory-low-utility-sweep', ms, safeRunMemoryLowUtilitySweep);
}

export function startRailFlagSweepScheduler(intervalMs?: number): NodeJS.Timeout {
  const ms =
    intervalMs ??
    resolveIntervalMs(process.env.RAIL_FLAG_SWEEP_INTERVAL_MS, WEEKLY_MS);
  return startSweepScheduler('rail-flag-sweep', ms, safeRunRailFlagSweep);
}

export function startPersonaLearningDigestScheduler(
  intervalMs?: number
): NodeJS.Timeout {
  const ms =
    intervalMs ??
    resolveIntervalMs(process.env.PERSONA_LEARNING_DIGEST_INTERVAL_MS, WEEKLY_MS);
  return startSweepScheduler('persona-learning-digest', ms, safeRunPersonaLearningDigest);
}

/**
 * Boot-time wrapper. Starts all four Harbor self-learn schedulers when
 * CAS_HARBOR_ENABLED=true; returns an empty intervals object otherwise so
 * the caller can blindly clear on shutdown.
 */
export function startHarborCronSchedulers(): HarborCronIntervals {
  if (process.env.CAS_HARBOR_ENABLED !== 'true') {
    console.log(
      '[harbor-cron] CAS_HARBOR_ENABLED is not "true"; skipping harbor self-learn schedulers'
    );
    return {};
  }
  return {
    topicKb: startTopicKbSweepScheduler(),
    memoryLowUtility: startMemoryLowUtilitySweepScheduler(),
    railFlag: startRailFlagSweepScheduler(),
    personaLearningDigest: startPersonaLearningDigestScheduler(),
  };
}
