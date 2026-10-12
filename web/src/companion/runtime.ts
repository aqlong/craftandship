/**
 * Companion runtime: the public turn() entry point.
 *
 * One function. Called from POST /lighthouse/table/turn. Orchestrates
 * the full lifecycle (see docs/CAS_HARBOR_PHASE_1_TECH_PLAN.md §4.2):
 *
 *   1. Crisis rail (pure regex; no model or database dependency). A crisis
 *      turn is answered with the deterministic script even if the identity
 *      lookup, the insert or the alert email fails. (The HTTP route answers a
 *      crisis message before it ever calls turn(), ahead of its rate limiters
 *      and gates; see `crisisFirst` in routes/lighthouse.ts. This step keeps
 *      turn() itself crisis-first for any other caller.)
 *   2. Identity (load trial or keeper, get companion name)
 *   3. Daily cost cap (throws CompanionDailyCostCapError when exceeded)
 *   4. AI-honesty input rail
 *   5. Memory retrieval
 *   6. Prompt assembly
 *   7. Claude call
 *   8. Output rail post-scan
 *   9. Persist turn
 *  10. Memory capture (background, non-blocking)
 *  11. Cost log (best effort once the turn is persisted)
 *  12. Update last_seen_at (best effort)
 *  13. Return
 *
 * There is no surrounding transaction: each step is its own autocommit
 * statement. Steps 9 to 12 are ordered so a failure after the reply was
 * persisted never turns into an error for the keeper (which would invite a
 * retry and a second model call plus a duplicate turn row).
 */

import type { Pool } from 'pg';
import { callCompanion, DEFAULT_MODEL } from './claude';
import {
  scanInput,
  scanOutput,
  railTemplate,
  sanitizeOutput,
  OUTPUT_FLAG_SEVERITY,
  type RailFlag,
} from './ai_honesty_rail';
import {
  scanCrisis,
  crisisTemplate,
  type CrisisCategory,
} from './crisis_detection';
import {
  buildPersonaPrompt,
  buildKeeperContextPrompt,
  assignCompanionName,
  type CompanionName,
} from './persona';
import { fetchRelevantMemory, renderMemorySnapshot } from './memory_retrieval';
import { captureMemory } from './memory_capture';
import { sendCrisisAlertEmail } from '../services/email';
import { PROMOTED_KEEPER_OF_TRIAL_SQL } from '../services/keeper_promotion';

export interface TurnOpts {
  pool: Pool;
  trialId?: string;
  keeperId?: string;
  keeperText: string;
  /**
   * If true, skip the Claude call entirely and return the deterministic
   * opening message (first-message-ever path). Used by GET handlers
   * that render a fresh table for a never-seen trial. The rest of the
   * pipeline (memory, persistence) is also skipped: there's nothing to
   * remember, no turn to log, no cost to record.
   */
  isOpeningMessage?: boolean;
}

export interface TurnResult {
  companionText: string;
  turnId: string | null;        // null when isOpeningMessage
  railFlags: string[];
  costMicros: number;
}

/**
 * Process one keeper-to-companion turn. Returns the companion's reply
 * along with audit metadata. Throws on hard infrastructure failures
 * (DB unreachable, no API keys, etc.); soft failures inside background
 * tasks are logged and swallowed.
 */
export async function turn(opts: TurnOpts): Promise<TurnResult> {
  validateOpts(opts);

  // Opening-message path: deterministic, no Claude, no DB. Honors the
  // documented "the rest of the pipeline (memory, persistence) is also
  // skipped" contract. Identity load runs lazily after this check so a
  // first-visit GET handler can call turn() without paying for a
  // SELECT just to render the opening line.
  if (opts.isOpeningMessage) {
    return {
      companionText: openingMessage(),
      turnId: null,
      railFlags: [],
      costMicros: 0,
    };
  }

  // 1. Crisis rail FIRST. It is a pure local regex scan, so it runs before
  // anything that can fail or be refused (identity lookup, cost cap, model).
  // A keeper who has used up the daily budget, or hits a database blip,
  // still gets the 988 script instead of a 429 or a 500.
  const crisis = scanCrisis(opts.keeperText);
  if (crisis.flagged) {
    return handleCrisisTurn(opts, crisis.categories);
  }

  // 2. Identity
  const identity = await loadIdentity(opts);
  const companionName = identity.companion_name as CompanionName;

  // 3. Per-trial / per-keeper daily cost cap. Rejects with a typed error
  // when this identity has already burned its daily Anthropic budget.
  // Throws BEFORE the input rail / memory / Claude so an over-cap identity
  // cannot spend more. Fail-open on SUM-query failure so a transient DB
  // blip can't 429 a real keeper.
  //
  // The cap is keyed on the trial row, so it bounds one cookie, not one
  // person: a client that discards its cookie gets a fresh trial and a
  // fresh budget. The per-IP limits in middleware/companion-rate-limit.ts
  // and the trial-mint limit in middleware/trial-cookie.ts are what bound
  // that case.
  const capMicros = getCompanionDailyCostCapMicros();
  const used = await getCompanionDailyCosts(opts.pool, {
    trialId: opts.trialId,
    keeperId: opts.keeperId,
  });
  if (used.identityMicros >= capMicros) {
    throw new CompanionDailyCostCapError(used.identityMicros, capMicros);
  }
  // The shared ceiling: every identity's turns in the last 24 hours added
  // together. The per-identity cap above and the per-address limiters each
  // bound one cookie or one address; this is what bounds many of them at
  // once. It reads from the same query, so it costs no extra round trip.
  const globalCapMicros = getCompanionGlobalDailyCostCapMicros();
  if (used.globalMicros >= globalCapMicros) {
    throw new CompanionDailyCostCapError(used.globalMicros, globalCapMicros, 'shared');
  }

  // 4. AI-honesty input rail (deterministic refusal templates).
  const inputFlags = scanInput(opts.keeperText);
  if (inputFlags.length > 0) {
    const text = railTemplate(inputFlags[0], companionName);
    const turnId = await persistTurn(opts, text, inputFlags);
    return {
      companionText: text,
      turnId,
      railFlags: inputFlags,
      costMicros: 0,
    };
  }

  // 5. Memory retrieval
  const memory = await fetchRelevantMemory({
    pool: opts.pool,
    trialId: opts.trialId,
    keeperId: opts.keeperId,
    queryText: opts.keeperText,
  });

  // 6. Prompt assembly
  const recentTurns = await loadRecentTurns(opts);
  const isFirstTurn = recentTurns.length === 0;
  const startedAt =
    identity.started_at instanceof Date
      ? identity.started_at
      : new Date(identity.started_at as string);

  const systemCacheable = buildPersonaPrompt({ companionName });
  const systemPerKeeper = buildKeeperContextPrompt({
    companionName,
    startedAt,
    memorySnapshot: renderMemorySnapshot(memory.items),
    isFirstTurn,
  });

  // 7. Claude call
  const result = await callCompanion({
    systemCacheable,
    systemPerKeeper,
    recentTurns,
    keeperText: opts.keeperText,
  });

  // 8. Output rail post-scan. Two severity tracks:
  //    - 'dangerous' (e.g. claims personhood): replace whole response
  //      with the deterministic template. Trust violation.
  //    - 'hygiene' (e.g. em-dash slip): sanitize in place. Punctuation
  //      hygiene shouldn't nuke 700 valid characters.
  const outputFlags = scanOutput(result.companionText);
  let finalText = result.companionText;
  if (outputFlags.length > 0) {
    const dangerous = outputFlags.find(
      (f) =>
        f.startsWith('output-') &&
        OUTPUT_FLAG_SEVERITY[f as keyof typeof OUTPUT_FLAG_SEVERITY] ===
          'dangerous'
    );
    if (dangerous) {
      finalText = railTemplate(dangerous, companionName);
    } else {
      // All flags are hygiene-level: sanitize in place.
      finalText = sanitizeOutput(result.companionText);
    }
  }

  // 9. Persist turn
  const turnId = await persistTurn(opts, finalText, outputFlags);

  // 10. Memory capture (background; failures must not break the user-facing turn).
  void captureMemory({
    pool: opts.pool,
    trialId: opts.trialId,
    keeperId: opts.keeperId,
    keeperText: opts.keeperText,
    recentContext: recentTurns
      .slice(-4)
      .map((t) => `${t.role}: ${t.content}`)
      .join('\n'),
    sourceTurnId: turnId,
  }).catch((err) => {
    console.error('[memory-capture] failed:', err);
  });

  // 11. Cost log. The reply is already persisted, so a failure here must
  // not surface as an error: the keeper would retry, paying for a second
  // model call and writing a duplicate turn. Log the figures so the cost
  // can be reconstructed from the process log if the insert is lost.
  try {
    await logCost(opts.pool, turnId, {
      inputTokens: result.usage.inputTokens,
      cachedInputTokens: result.usage.cachedInputTokens,
      cacheCreationTokens: result.usage.cacheCreationTokens,
      outputTokens: result.usage.outputTokens,
      embeddingTokens: memory.embeddingTokens,
      totalCostMicros: result.costMicros,
      model: result.model,
    });
  } catch (err) {
    console.error(
      '[companion-cost-log] insert failed; turn already persisted:',
      JSON.stringify({
        turnId,
        model: result.model,
        totalCostMicros: result.costMicros,
      }),
      (err as Error).message
    );
  }

  // 12. Update last_seen_at (best effort for the same reason).
  try {
    await touchLastSeen(opts);
  } catch (err) {
    console.error('[companion-last-seen] update failed:', (err as Error).message);
  }

  return {
    companionText: finalText,
    turnId,
    railFlags: outputFlags,
    costMicros: result.costMicros,
  };
}

/**
 * Crisis turn: deterministic script, never the model. Everything around the
 * script is best effort so the keeper always receives it:
 *  - companion name falls back to the default when the identity lookup fails
 *  - a failed insert is logged and the reply is returned with turnId null
 *  - the operator alert is fire-and-forget (it logs, then tries the email)
 */
async function handleCrisisTurn(
  opts: TurnOpts,
  categories: CrisisCategory[]
): Promise<TurnResult> {
  let companionName: CompanionName = assignCompanionName();
  try {
    const identity = await loadIdentity(opts);
    companionName = identity.companion_name as CompanionName;
  } catch (err) {
    console.error(
      '[companion-crisis] identity lookup failed; using default companion name:',
      (err as Error).message
    );
  }

  const reply = crisisReply(categories, companionName);
  const turnId = await recordCrisisTurn({
    pool: opts.pool,
    trialId: opts.trialId,
    keeperId: opts.keeperId,
    keeperText: opts.keeperText,
    reply,
  });
  return { companionText: reply.companionText, turnId, railFlags: reply.railFlags, costMicros: 0 };
}

export interface CrisisReply {
  companionText: string;
  railFlags: string[];
  categories: CrisisCategory[];
}

/**
 * The fixed crisis script for these categories. Pure: no database, no model,
 * no clock. With no name given it uses the default companion name, which is
 * what lets a caller answer before it knows (or without ever learning) who is
 * asking.
 */
export function crisisReply(
  categories: CrisisCategory[],
  companionName: CompanionName = assignCompanionName()
): CrisisReply {
  return {
    companionText: crisisTemplate(companionName, categories),
    railFlags: ['crisis', ...categories],
    categories,
  };
}

/** A stored crisis turn keeps at most this much of the message. */
const CRISIS_STORED_TEXT_MAX_CHARS = 4000;

/**
 * What follows a crisis reply: store the turn (flagged, for the operator to
 * review) and alert the operator. Never throws and never decides whether the
 * person gets the script; the caller has that already. Both steps are bounded
 * by `crisisSideEffects`, because the crisis reply itself is deliberately not
 * rate limited and each of these writes a row or sends an email.
 *
 * Returns the stored turn's id, or null when nothing was stored (no identity
 * to file it under, over budget, or the insert failed).
 */
export async function recordCrisisTurn(input: {
  pool: Pool;
  trialId?: string;
  keeperId?: string;
  keeperText: string;
  reply: CrisisReply;
}): Promise<string | null> {
  const identityKey = input.trialId
    ? `trial:${input.trialId}`
    : input.keeperId
      ? `keeper:${input.keeperId}`
      : 'anonymous';

  let turnId: string | null = null;
  if (identityKey === 'anonymous') {
    // companion_turns needs a trial or a keeper; there is nowhere to file it.
  } else if (!crisisSideEffects.allowPersist(identityKey)) {
    console.warn(
      '[companion-crisis] crisis turn not stored: the stored-turn budget is spent (this identity, or the per-minute cap for the process); the reply was still sent',
      JSON.stringify({ identity: identityKey })
    );
  } else {
    try {
      turnId = await persistTurn(
        {
          pool: input.pool,
          trialId: input.trialId,
          keeperId: input.keeperId,
          keeperText: input.keeperText.slice(0, CRISIS_STORED_TEXT_MAX_CHARS),
        },
        input.reply.companionText,
        input.reply.railFlags
      );
    } catch (err) {
      console.error(
        '[companion-crisis] persisting the crisis turn failed; reply still returned:',
        (err as Error).message
      );
    }
  }

  notifyHarborMaster(input.reply.categories, identityKey, {
    trialId: input.trialId,
    keeperId: input.keeperId,
  }).catch((err) => {
    console.error('[harbor-master-notify] failed:', err);
  });

  return turnId;
}

// ---------- crisis side-effect budget ----------

export const CRISIS_PERSIST_WINDOW_MS = 15 * 60 * 1000;
export const CRISIS_PERSIST_MAX_PER_WINDOW = 20;
/** Stored crisis turns the whole process may write in one one-minute window. */
export const CRISIS_PERSIST_MAX_PER_MINUTE = 60;
export const CRISIS_ALERT_COOLDOWN_MS = 30 * 60 * 1000;
export const CRISIS_ALERT_MAX_PER_HOUR = 10;
/** Most identities either per-identity map holds. */
export const CRISIS_BUDGET_MAX_KEYS = 10_000;

export type CrisisAlertDecision = 'send' | 'cooldown' | 'hourly_cap';

export interface CrisisSideEffectBudget {
  /** May one more crisis turn be stored for this identity now? */
  allowPersist(identityKey: string): boolean;
  /** May an alert email go out for this identity now? Consumes the budget when it answers 'send'. */
  allowAlert(identityKey: string): CrisisAlertDecision;
  /** How many identities the stored-turn budget is holding a count for (at most CRISIS_BUDGET_MAX_KEYS). */
  trackedIdentities(): number;
  /** Forget everything counted so far. */
  reset(): void;
}

/**
 * Bounds on what a crisis reply leaves behind. The reply is answered ahead of
 * every rate limiter on purpose, so without these a client could use it to
 * fill companion_turns or to make the server mail the operator in a loop:
 *
 *  - stored turns: CRISIS_PERSIST_MAX_PER_WINDOW per identity per
 *    CRISIS_PERSIST_WINDOW_MS;
 *  - alert emails: one per identity per CRISIS_ALERT_COOLDOWN_MS, and
 *    CRISIS_ALERT_MAX_PER_HOUR for the whole process. (A request with no
 *    trial or keeper shares the single identity "anonymous".)
 *
 * Stored turns have a process-wide bound as well: at most
 * CRISIS_PERSIST_MAX_PER_MINUTE in each one-minute window, whatever the identity.
 * Each per-identity map is kept at or below CRISIS_BUDGET_MAX_KEYS (see
 * evictToLimit), so an identity that has used its quota is not handed a fresh
 * one by other identities filling the map.
 *
 * Past a bound the side effect is skipped and a log line says so; the log
 * line for the crisis itself is always written. Counters live in process
 * memory: they reset on restart and are per replica. The cost of the email
 * bound is real and accepted: in an hour that has already produced
 * CRISIS_ALERT_MAX_PER_HOUR alerts, a further crisis reaches the operator
 * through the log and the stored turn only.
 */
export function createCrisisSideEffectBudget(
  opts: { now?: () => number; maxPersistPerMinute?: number } = {}
): CrisisSideEffectBudget {
  const now = opts.now ?? (() => Date.now());
  const maxPersistPerMinute = opts.maxPersistPerMinute ?? CRISIS_PERSIST_MAX_PER_MINUTE;
  // Map order is the order each entry was last started, so the first entries
  // are the oldest.
  const persisted = new Map<string, { count: number; resetAt: number }>();
  const lastAlertAt = new Map<string, number>();
  let alertTimes: number[] = [];
  let minuteStart = 0;
  let minuteWrites = 0;

  return {
    allowPersist(identityKey: string): boolean {
      const t = now();
      if (t < minuteStart || t - minuteStart >= 60 * 1000) {
        minuteStart = t;
        minuteWrites = 0;
      }
      const entry = persisted.get(identityKey);
      const counting = entry !== undefined && entry.resetAt > t;
      if (counting && entry.count >= CRISIS_PERSIST_MAX_PER_WINDOW) return false;
      if (minuteWrites >= maxPersistPerMinute) return false;
      minuteWrites += 1;
      if (counting) {
        entry.count += 1;
        return true;
      }
      persisted.delete(identityKey);
      persisted.set(identityKey, { count: 1, resetAt: t + CRISIS_PERSIST_WINDOW_MS });
      evictToLimit(persisted, (value) => value.resetAt > t && value.count >= CRISIS_PERSIST_MAX_PER_WINDOW);
      return true;
    },
    allowAlert(identityKey: string): CrisisAlertDecision {
      const t = now();
      const last = lastAlertAt.get(identityKey);
      if (last !== undefined && t - last < CRISIS_ALERT_COOLDOWN_MS) return 'cooldown';
      alertTimes = alertTimes.filter((at) => t - at < 60 * 60 * 1000);
      if (alertTimes.length >= CRISIS_ALERT_MAX_PER_HOUR) return 'hourly_cap';
      alertTimes.push(t);
      lastAlertAt.delete(identityKey);
      lastAlertAt.set(identityKey, t);
      evictToLimit(lastAlertAt, (at) => t - at < CRISIS_ALERT_COOLDOWN_MS);
      return 'send';
    },
    trackedIdentities(): number {
      return persisted.size;
    },
    reset(): void {
      persisted.clear();
      lastAlertAt.clear();
      alertTimes = [];
      minuteStart = 0;
      minuteWrites = 0;
    },
  };
}

/**
 * Brings a per-identity map down to CRISIS_BUDGET_MAX_KEYS. Entries that hold no
 * quota (a window that has ended, or one not yet used up) go first, oldest
 * first, so an identity that has used its quota keeps it while the map has any
 * other entry to drop. Only when every entry holds a quota is the oldest one
 * dropped. For the stored-turn map that last case cannot arise: a quota is held
 * only by a live entry, a live entry was created by an allowed write in the last
 * 15 minutes, and the one-minute windows start at least a minute apart, so a
 * 15-minute span overlaps at most 16 of them and there are at most
 * 16 * CRISIS_PERSIST_MAX_PER_MINUTE live entries.
 */
function evictToLimit<V>(map: Map<string, V>, holdsQuota: (value: V) => boolean): void {
  while (map.size > CRISIS_BUDGET_MAX_KEYS) {
    let victim: string | undefined;
    for (const [key, value] of map) {
      if (!holdsQuota(value)) {
        victim = key;
        break;
      }
    }
    victim ??= map.keys().next().value;
    if (victim === undefined) return;
    map.delete(victim);
  }
}

/** The budget every crisis reply in this process draws on. */
export const crisisSideEffects = createCrisisSideEffectBudget();

// ---------- helpers ----------

function validateOpts(opts: TurnOpts): void {
  if (!opts.trialId && !opts.keeperId) {
    throw new Error('runtime.turn: trialId or keeperId required');
  }
  if (opts.trialId && opts.keeperId) {
    throw new Error('runtime.turn: only one of trialId or keeperId may be set');
  }
  if (!opts.keeperText && !opts.isOpeningMessage) {
    throw new Error('runtime.turn: keeperText is required unless isOpeningMessage');
  }
}

interface IdentityRow {
  companion_name: string;
  started_at: Date | string;
}

async function loadIdentity(opts: TurnOpts): Promise<IdentityRow> {
  if (opts.trialId) {
    const { rows } = await opts.pool.query<IdentityRow>(
      `SELECT companion_name, started_at FROM trial_keepers
       WHERE trial_id = $1 AND released_at IS NULL`,
      [opts.trialId]
    );
    if (rows.length === 0) {
      throw new Error(`runtime: no active trial_keeper for ${opts.trialId}`);
    }
    return rows[0];
  }
  const { rows } = await opts.pool.query<IdentityRow>(
    `SELECT companion_name, created_at AS started_at FROM keepers
     WHERE customer_id = $1`,
    [opts.keeperId!]
  );
  if (rows.length === 0) {
    throw new Error(`runtime: no keeper for ${opts.keeperId}`);
  }
  return rows[0];
}

async function loadRecentTurns(opts: TurnOpts): Promise<Array<{
  role: 'user' | 'assistant';
  content: string;
}>> {
  // Last 6 turns of conversation. Each row may contain a keeper text +
  // companion text; expand into 1-2 messages depending on which fields
  // are set.
  const idColumn = opts.trialId ? 'trial_id' : 'keeper_id';
  const idValue = opts.trialId ?? opts.keeperId!;
  const { rows } = await opts.pool.query<{
    keeper_text: string | null;
    companion_text: string;
  }>(
    `SELECT keeper_text, companion_text FROM companion_turns
     WHERE ${idColumn} = $1
     ORDER BY created_at DESC
     LIMIT 6`,
    [idValue]
  );
  // Reverse so chronological. Each row is one keeper utterance + one
  // companion reply, except first-ever turn where keeper_text may be NULL.
  const ordered = rows.reverse();
  const messages: Array<{ role: 'user' | 'assistant'; content: string }> = [];
  for (const row of ordered) {
    if (row.keeper_text) {
      messages.push({ role: 'user', content: row.keeper_text });
    }
    messages.push({ role: 'assistant', content: row.companion_text });
  }
  return messages;
}

/**
 * A keeper's turn: a plain insert. Parameters: keeper_id, trial_id (null),
 * keeper_text, companion_text, rail_flags.
 */
const INSERT_KEEPER_TURN_SQL = `
  INSERT INTO companion_turns
    (keeper_id, trial_id, surface, keeper_text, companion_text, rail_flags)
  VALUES ($1, $2, 'table', $3, $4, $5)
  RETURNING turn_id`;

/**
 * A trial's turn, with the same five parameters (keeper_id is null). It is
 * inserted through the trial's row, only while the trial is unreleased, and
 * under a share lock on that row.
 *
 * Between the moment a turn starts and the moment it is stored lies a model
 * call of several seconds, and a payment can land in it. Promotion
 * (services/keeper_promotion.ts) locks the trial row first, moves the trial's
 * turns to the keeper, and releases the trial. A turn inserted under the
 * trial id after that would sit on a released trial, where neither the
 * keeper's conversation page nor anything else reads it. With the lock the
 * two cannot interleave: either this insert commits first and promotion,
 * which was waiting for the row, moves the turn with the others; or promotion
 * commits first and this statement, re-reading the row it waited for, finds
 * it released and inserts nothing.
 */
const INSERT_TRIAL_TURN_SQL = `
  INSERT INTO companion_turns
    (keeper_id, trial_id, surface, keeper_text, companion_text, rail_flags)
  SELECT $1::uuid, t.trial_id, 'table', $3, $4, $5
  FROM trial_keepers t
  WHERE t.trial_id = $2 AND t.released_at IS NULL
  FOR SHARE OF t
  RETURNING turn_id`;

/**
 * Store one turn under whoever owns the conversation when it is stored.
 *
 * For a trial that is the trial itself, unless it was released while the turn
 * was being answered (see INSERT_TRIAL_TURN_SQL); then the turn goes to the
 * keeper the trial was promoted into, so that it is in the conversation the
 * payer sees next. A released trial with no keeper behind it has no
 * conversation to add to, and the turn fails rather than being stored where
 * nothing reads it.
 */
async function persistTurn(
  opts: Pick<TurnOpts, 'pool' | 'trialId' | 'keeperId' | 'keeperText'>,
  companionText: string,
  railFlags: string[]
): Promise<string> {
  const turnFields = [opts.keeperText, companionText, railFlags];
  if (!opts.trialId) {
    const { rows } = await opts.pool.query<{ turn_id: string }>(INSERT_KEEPER_TURN_SQL, [
      opts.keeperId ?? null,
      null,
      ...turnFields,
    ]);
    return rows[0].turn_id;
  }

  const asTrial = await opts.pool.query<{ turn_id: string }>(INSERT_TRIAL_TURN_SQL, [
    null,
    opts.trialId,
    ...turnFields,
  ]);
  if (asTrial.rows.length > 0) return asTrial.rows[0].turn_id;

  const promoted = await opts.pool.query<{ customer_id: string }>(PROMOTED_KEEPER_OF_TRIAL_SQL, [
    opts.trialId,
  ]);
  const keeperId = promoted.rows[0]?.customer_id;
  if (!keeperId) {
    throw new Error(`runtime: trial ${opts.trialId} is no longer active and no keeper holds its conversation`);
  }
  const { rows } = await opts.pool.query<{ turn_id: string }>(INSERT_KEEPER_TURN_SQL, [
    keeperId,
    null,
    ...turnFields,
  ]);
  return rows[0].turn_id;
}

interface CostLogParams {
  inputTokens: number;
  cachedInputTokens: number;
  cacheCreationTokens: number;
  outputTokens: number;
  embeddingTokens: number;
  totalCostMicros: number;
  model: string;
}

async function logCost(
  pool: Pool,
  turnId: string,
  cost: CostLogParams
): Promise<void> {
  await pool.query(
    `INSERT INTO companion_cost_log
       (turn_id, input_tokens, cached_input_tokens, cache_creation_tokens,
        output_tokens, embedding_tokens, total_cost_micros, model)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
    [
      turnId,
      cost.inputTokens,
      cost.cachedInputTokens,
      cost.cacheCreationTokens,
      cost.outputTokens,
      cost.embeddingTokens,
      cost.totalCostMicros,
      cost.model,
    ]
  );
}

/**
 * Per-trial / per-keeper daily cost cap on companion turns. Reads
 * SUM(total_cost_micros) from companion_cost_log for the trailing 24 hours
 * (a rolling window, not a calendar day) scoped to this identity (via the
 * turn_id -> companion_turns -> trial_id/keeper_id join). Cap default is
 * $5/day in micros (5_000_000); override via
 * CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS. All amounts are integer micro
 * dollars (1 micro = $0.000001); the SUM is cast to bigint in SQL.
 *
 * `turn()` compares the current spend to the cap and throws
 * CompanionDailyCostCapError when it is reached; the route turns that into
 * a deterministic 429 with a friendly message. The model is not called when
 * over cap, so the budget actually clamps.
 *
 * Limits of this cap, stated plainly:
 *  - It is per identity row. A client that drops its trial cookie gets a new
 *    trial and a new budget; the per-IP limiters bound that.
 *  - It is checked before the call and the call's cost is written after, so
 *    concurrent requests can overshoot by (concurrency x one turn).
 *  - It counts only turns whose cost row was written; the memory-capture
 *    classifier and spec extraction calls are not part of this sum.
 *
 * Why a cap instead of just a rate limit: the rate limit caps message
 * COUNT (60 per 15min). A poorly-tuned prompt could blow the cost
 * budget at a normal message rate (e.g. 10x token usage per turn from
 * a manipulated prompt). The cost cap is the load-bearing budget
 * defense; the rate limit is the burst defense.
 *
 * Derived from a security review of the platform
 * (`harbor-companion-turn-rate-limit-and-cost-cap`).
 */
export const COMPANION_DAILY_COST_CAP_DEFAULT_MICROS = 5_000_000; // $5 a day per identity

export function getCompanionDailyCostCapMicros(): number {
  // The env is read on every call. There is one resolution path for every
  // environment: a test that wants a different cap sets the variable.
  const raw = parseInt(process.env.CAS_HARBOR_TURN_DAILY_COST_CAP_MICROS || '', 10);
  if (Number.isFinite(raw) && raw > 0) return raw;
  return COMPANION_DAILY_COST_CAP_DEFAULT_MICROS;
}

/**
 * Shared daily ceiling on Haven conversation spend, across every trial and
 * keeper together. Default $50 a day; override with
 * CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS. When it is reached, no one can take
 * another model-backed turn until older turns age out of the 24-hour window
 * (the crisis script still answers: it runs before any cap). That is a
 * deliberate trade: a flood from many addresses can stop the chat for
 * everybody for a day, but it cannot run up an open-ended bill.
 */
export const COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS = 50_000_000;

export function getCompanionGlobalDailyCostCapMicros(): number {
  const raw = parseInt(process.env.CAS_HARBOR_GLOBAL_DAILY_COST_CAP_MICROS || '', 10);
  if (Number.isFinite(raw) && raw > 0) return raw;
  return COMPANION_GLOBAL_DAILY_COST_CAP_DEFAULT_MICROS;
}

function toMicros(raw: string | number | null | undefined): number {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : 0;
  if (typeof raw === 'string') return parseInt(raw, 10) || 0;
  return 0;
}

/**
 * Spend in the last 24 hours: by this identity, and by everyone. One query.
 * Fails open (both figures 0) on a database error, like the per-identity sum
 * always has: a blip on this read must not turn a real keeper away.
 */
export async function getCompanionDailyCosts(
  pool: Pool,
  identity: { trialId?: string; keeperId?: string }
): Promise<{ identityMicros: number; globalMicros: number }> {
  const params: unknown[] = [];
  let scope: string;
  if (identity.trialId) {
    scope = 'ct.trial_id = $1';
    params.push(identity.trialId);
  } else if (identity.keeperId) {
    scope = 'ct.keeper_id = $1';
    params.push(identity.keeperId);
  } else {
    scope = 'FALSE';
  }
  const sql = `
    SELECT COALESCE(SUM(ccl.total_cost_micros) FILTER (WHERE ${scope}), 0)::bigint AS total,
           COALESCE(SUM(ccl.total_cost_micros), 0)::bigint AS global_total
    FROM companion_cost_log ccl
    JOIN companion_turns ct ON ct.turn_id = ccl.turn_id
    WHERE ccl.created_at >= NOW() - INTERVAL '24 hours'
  `;
  try {
    const r = await pool.query<{ total: string | number; global_total?: string | number }>(sql, params);
    return {
      identityMicros: toMicros(r.rows[0]?.total),
      globalMicros: toMicros(r.rows[0]?.global_total),
    };
  } catch (err) {
    console.error('[companion-cost-cap] SUM query failed:', (err as Error).message);
    return { identityMicros: 0, globalMicros: 0 };
  }
}

/** Spend by one identity in the last 24 hours. Returns 0 without a query when no identity is given. */
export async function getCompanionDailyCostMicros(
  pool: Pool,
  identity: { trialId?: string; keeperId?: string }
): Promise<number> {
  if (!identity.trialId && !identity.keeperId) return 0;
  return (await getCompanionDailyCosts(pool, identity)).identityMicros;
}

export class CompanionDailyCostCapError extends Error {
  readonly currentMicros: number;
  readonly capMicros: number;
  /** Which cap refused the turn: this identity's own, or the one shared by everyone. */
  readonly scope: 'identity' | 'shared';
  constructor(currentMicros: number, capMicros: number, scope: 'identity' | 'shared' = 'identity') {
    super(
      `Companion daily cost cap exceeded: $${(currentMicros / 1_000_000).toFixed(2)} of $${(capMicros / 1_000_000).toFixed(2)} budget consumed in the last 24h.`
    );
    this.name = 'CompanionDailyCostCapError';
    this.currentMicros = currentMicros;
    this.capMicros = capMicros;
    this.scope = scope;
  }
}

async function touchLastSeen(opts: TurnOpts): Promise<void> {
  if (opts.trialId) {
    await opts.pool.query(
      `UPDATE trial_keepers SET last_seen_at = NOW() WHERE trial_id = $1`,
      [opts.trialId]
    );
  } else if (opts.keeperId) {
    await opts.pool.query(
      `UPDATE keepers SET updated_at = NOW() WHERE customer_id = $1`,
      [opts.keeperId]
    );
  }
}

/**
 * Fire-and-forget hook for crisis turns. Two layers, in order:
 *  1. A stderr line tagged `[harbor-master-notify]` with the crisis
 *     categories and the trial / keeper id. Always written; this is the
 *     source of truth even when no email goes out.
 *  2. An alert email to ADMIN_EMAIL via services/email.ts
 *     (`sendCrisisAlertEmail`), if `crisisSideEffects` allows one for this
 *     identity right now. The email carries categories and the id only,
 *     never the keeper's text. A send failure is caught and logged; it
 *     cannot reach the turn handler.
 */
async function notifyHarborMaster(
  categories: string[],
  identityKey: string,
  identity: { trialId?: string; keeperId?: string }
): Promise<void> {
  const decision = crisisSideEffects.allowAlert(identityKey);
  console.warn(
    '[harbor-master-notify] crisis flagged',
    JSON.stringify({
      categories,
      trialId: identity.trialId,
      keeperId: identity.keeperId,
      alertEmail: decision === 'send' ? 'sending' : `suppressed (${decision})`,
    })
  );
  if (decision !== 'send') return;
  try {
    await sendCrisisAlertEmail({
      categories,
      trialId: identity.trialId,
      keeperId: identity.keeperId,
    });
  } catch (err) {
    // An email failure must not turn into a failed turn. The log line above
    // already recorded the crisis; record this layer too.
    console.error(
      '[harbor-master-notify] email send failed (non-blocking):',
      (err as Error).message
    );
  }
}

/**
 * Deterministic opening message for the very-first encounter. Mirrors
 * the persona prompt's OPENING block.
 */
export function openingMessage(): string {
  return (
    `Hi. I'm so glad you found your way here. Before we get into anything ` +
    `about your work or a website, I want to ask: what brings you here ` +
    `today? You can answer in any way that feels true. There are no wrong ` +
    `answers and there's no wrong order to do this in. I'll go at whatever ` +
    `pace feels right.`
  );
}

/**
 * Constants the route layer can import for self-documenting handlers.
 */
export const RUNTIME_DEFAULT_MODEL = DEFAULT_MODEL;
export type { RailFlag };
