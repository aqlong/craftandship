/**
 * Memory retrieval for the companion runtime.
 *
 * At turn-time we want the keeper's most-relevant memory items, ranked
 * by semantic similarity to the current keeper text plus a recency boost.
 * No pgvector at MVP scale: we pull the keeper's full memory set (max
 * a few thousand rows) and rank in JS via cosineSimilarity().
 */

import type { Pool } from 'pg';
import { embedOne } from './embeddings';
import { cosineSimilarity } from './cosine';
import { renderMemoryLine } from './prompt_text';

export type MemoryPartition = 'working' | 'sacred' | 'voice';

export interface KeeperMemoryRow {
  memory_id: string;
  keeper_id: string | null;
  trial_id: string | null;
  partition: MemoryPartition;
  category: string;
  content: string;
  keeper_phrase: string | null;
  source_turn_id: string | null;
  embedding: number[] | null;
  created_at: Date;
  updated_at: Date;
  last_retrieved_at?: Date | null;
  low_utility?: boolean;
}

export interface FetchOpts {
  pool: Pool;
  trialId?: string;
  keeperId?: string;
  queryText: string;
  topK?: number;
  recentN?: number;
  /**
   * Include the sacred partition in retrieval. Defaults to false. The
   * Phase-4 firewall will replace this knob with a dedicated entry
   * point + audit log; until then the runtime never sets this true.
   */
  includeSacred?: boolean;
}

export interface FetchResult {
  items: KeeperMemoryRow[];
  embeddingTokens: number;
}

/**
 * Whose memory: a keeper (customers.id) or a trial (trial_keepers.trial_id).
 * Every keeper_memory and companion_turns row is keyed by exactly one of the
 * two (a CHECK constraint in db/schema.sql), and payment moves a trial's rows
 * to the keeper it was promoted into (services/keeper_promotion.ts). A reader
 * that asks for a paying customer's rows by their old trial id therefore
 * finds nothing, so callers name the owner and this module picks the column.
 */
export type MemoryOwner = { keeperId: string } | { trialId: string };

/** The column that names the owner, on both keeper_memory and companion_turns. */
export type MemoryOwnerColumn = 'keeper_id' | 'trial_id';

/** The identity column, and the value to compare it with, for one owner. */
export function memoryOwnerColumn(owner: MemoryOwner): { column: MemoryOwnerColumn; value: string } {
  return 'keeperId' in owner
    ? { column: 'keeper_id', value: owner.keeperId }
    : { column: 'trial_id', value: owner.trialId };
}

/** One note as the owner is shown it (the memory panel and the recall page). */
export interface MemoryNoteRow {
  memory_id: string;
  partition: string;
  category: string;
  content: string;
  keeper_phrase: string | null;
  created_at: Date;
  updated_at: Date;
}

/**
 * The owner's own notes, newest first, for display to that owner. All three
 * partitions are returned, sacred included: this is the owner reading their
 * own notes, not the model reading them (turn-time retrieval below leaves
 * sacred rows out).
 */
export async function listMemoryNotes(
  pool: Pool,
  owner: MemoryOwner,
  limit: number
): Promise<MemoryNoteRow[]> {
  const { column, value } = memoryOwnerColumn(owner);
  const { rows } = await pool.query<MemoryNoteRow>(
    `SELECT memory_id, partition, category, content, keeper_phrase,
            created_at, updated_at
     FROM keeper_memory
     WHERE ${column} = $1 AND deleted_at IS NULL
     ORDER BY updated_at DESC
     LIMIT $2`,
    [value, limit]
  );
  return rows;
}

/**
 * Defaults for the relevance + recency knobs. Override per call via
 * `topK` / `recentN` opts. The env-var path lets ops tune in production
 * without a code change; absent or non-numeric env values fall back to
 * the documented defaults.
 *
 *   COMPANION_MEMORY_TOP_K   default 8
 *   COMPANION_MEMORY_RECENT_N default 5
 *
 * Hard floor 0; no upper cap (caller responsibility) but `MAX_FETCH_ROWS`
 * caps the underlying SELECT to avoid pulling unbounded rows on a
 * pathologically large keeper.
 */
function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return n;
}
const DEFAULT_TOP_K = envInt('COMPANION_MEMORY_TOP_K', 8);
const DEFAULT_RECENT_N = envInt('COMPANION_MEMORY_RECENT_N', 5);
/**
 * Low-utility floor: when a keeper has at least this many non-low-utility
 * memory rows, retrieval excludes low_utility=TRUE rows from ranking.
 * Below the floor, every row is fair game so a keeper whose memory is
 * mostly stale (or whose memory hasn't accrued yet) never goes blank.
 */
const LOW_UTILITY_NON_LOW_FLOOR = envInt('COMPANION_MEMORY_LOW_UTILITY_FLOOR', 10);
/**
 * Safety cap on the underlying memory SELECT. At MVP scale a keeper has
 * tens to a few thousand rows; this guards a runaway 100K-row fetch from
 * OOM-ing the worker. JS-side cosine ranking is still fine inside this
 * window. When a real keeper bumps the ceiling we should migrate to
 * pgvector + DB-side ranking.
 */
const MAX_FETCH_ROWS = envInt('COMPANION_MEMORY_MAX_FETCH_ROWS', 5000);

/**
 * Fetch the most-relevant memory items for the current turn.
 *
 * Returns up to topK semantically-similar items, then up to recentN
 * recently-updated items, deduped by memory_id. Order: relevance first,
 * then recency for items not already returned by relevance.
 *
 * Throws if neither trialId nor keeperId is provided. Throws if both
 * are provided. (Mirrors the schema CHECK constraint on companion_turns
 * and keeper_memory: exactly one identity must be set.)
 */
export async function fetchRelevantMemory(opts: FetchOpts): Promise<FetchResult> {
  if (!opts.trialId && !opts.keeperId) {
    throw new Error('fetchRelevantMemory: trialId or keeperId required');
  }
  if (opts.trialId && opts.keeperId) {
    throw new Error('fetchRelevantMemory: only one of trialId or keeperId may be set');
  }

  const topK = opts.topK ?? DEFAULT_TOP_K;
  const recentN = opts.recentN ?? DEFAULT_RECENT_N;
  const includeSacred = opts.includeSacred ?? false;

  // Pull the keeper's memory set. Order by updated_at DESC so the
  // recency tail is correct without a second query.
  const partitionFilter = includeSacred
    ? `AND partition IN ('working', 'sacred', 'voice')`
    : `AND partition IN ('working', 'voice')`;

  const idColumn = opts.trialId ? 'trial_id' : 'keeper_id';
  const idValue = opts.trialId ?? opts.keeperId!;

  const sql = `
    SELECT memory_id, keeper_id, trial_id, partition, category,
           content, keeper_phrase, source_turn_id, embedding,
           created_at, updated_at, last_retrieved_at, low_utility
    FROM keeper_memory
    WHERE ${idColumn} = $1
      AND deleted_at IS NULL
      ${partitionFilter}
    ORDER BY updated_at DESC
    LIMIT $2
  `;
  const allRows = await opts.pool.query<KeeperMemoryRow>(sql, [idValue, MAX_FETCH_ROWS]);

  // Low-utility filter: when the keeper has >= LOW_UTILITY_NON_LOW_FLOOR
  // non-low-utility rows, drop low_utility=TRUE rows from ranking. Below
  // the floor we keep everything so a sparse keeper never goes blank.
  // Defensive on `low_utility=undefined` (older rows that predate the
  // column or test fixtures that omit it): treat as not low-utility.
  const nonLowCount = allRows.rows.filter((r) => !r.low_utility).length;
  const rows =
    nonLowCount >= LOW_UTILITY_NON_LOW_FLOOR
      ? allRows.rows.filter((r) => !r.low_utility)
      : allRows.rows;

  if (rows.length === 0) {
    return { items: [], embeddingTokens: 0 };
  }

  // Graceful degrade: when OPENAI_API_KEY is unset, OR when embedding
  // fails (quota, network, bad key), skip semantic ranking and return
  // the recency tail. Lets the founder dogfood Haven's memory layer
  // even when the embedding side is sick.
  if (!process.env.OPENAI_API_KEY) {
    const items = rows.slice(0, recentN);
    await stampLastRetrievedAt(opts.pool, items);
    return { items, embeddingTokens: 0 };
  }

  let queryVec: number[];
  let totalTokens: number;
  try {
    const r = await embedOne(opts.queryText);
    queryVec = r.embedding;
    totalTokens = r.totalTokens;
  } catch (err) {
    console.warn(
      '[memory-retrieval] embed failed; falling back to recency-only:',
      err instanceof Error ? err.message.slice(0, 200) : String(err)
    );
    const items = rows.slice(0, recentN);
    await stampLastRetrievedAt(opts.pool, items);
    return { items, embeddingTokens: 0 };
  }
  const ranked = rows
    .map((r) => ({
      row: r,
      // A row embedded with a different model/dimension (or a corrupt row)
      // would make cosineSimilarity throw and fail the whole turn; rank it
      // last instead.
      similarity:
        r.embedding && r.embedding.length === queryVec.length
          ? cosineSimilarity(queryVec, r.embedding)
          : 0,
    }))
    .sort((a, b) => b.similarity - a.similarity);

  const seen = new Set<string>();
  const result: KeeperMemoryRow[] = [];

  // Top-K by relevance.
  for (const { row } of ranked.slice(0, topK)) {
    if (seen.has(row.memory_id)) continue;
    seen.add(row.memory_id);
    result.push(row);
  }

  // Recency tail: rows is already sorted by updated_at DESC.
  for (const row of rows.slice(0, recentN)) {
    if (seen.has(row.memory_id)) continue;
    seen.add(row.memory_id);
    result.push(row);
  }

  await stampLastRetrievedAt(opts.pool, result);
  return { items: result, embeddingTokens: totalTokens };
}

/**
 * Stamp last_retrieved_at = NOW() on the rows that were actually returned
 * to the runtime. Drives the weekly low-utility sweep: rows that go
 * months without a stamp become candidates for pruning. Errors are
 * caught + logged so a stamp failure never breaks a turn.
 */
async function stampLastRetrievedAt(
  pool: Pool,
  items: KeeperMemoryRow[]
): Promise<void> {
  if (items.length === 0) return;
  const ids = items.map((i) => i.memory_id);
  try {
    await pool.query(
      `UPDATE keeper_memory
         SET last_retrieved_at = NOW()
       WHERE memory_id = ANY($1::uuid[])`,
      [ids]
    );
  } catch (err) {
    console.warn(
      '[memory-retrieval] stampLastRetrievedAt failed; continuing:',
      err instanceof Error ? err.message.slice(0, 200) : String(err)
    );
  }
}

/**
 * Render a list of memory items as Markdown for the per-keeper system
 * block. The companion sees this as "what you remember about them."
 */
export function renderMemorySnapshot(items: KeeperMemoryRow[]): string {
  if (items.length === 0) return '';
  // renderMemoryLine flattens each stored value to one capped line so a
  // stored keeper phrase cannot forge prompt structure (see prompt_text.ts).
  return items.map((item) => renderMemoryLine(item)).join('\n');
}
