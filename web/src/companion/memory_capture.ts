/**
 * Memory capture: classify a keeper utterance into 0-N memory items,
 * embed each, dedupe against existing memory, persist.
 *
 * Runs as a non-blocking background task after the user-facing turn
 * has already been returned. Failures are logged but never propagate.
 *
 * The classifier is Claude Haiku with a tight JSON-output prompt. Cost
 * is negligible (~$0.0001/call). On parse failure or empty result, no
 * memory is captured and the turn proceeds normally.
 *
 * Sacred-flag handling: if the classifier marks an item with
 * sacred_likelihood > 0.7, we set partition='sacred'. The classification is
 * a model judgment. What follows from it today: turn-time retrieval never
 * reads sacred rows (the runtime does not pass includeSacred), spec
 * extraction and the conversion letter read only 'working' and 'voice' rows
 * and skip the turn a sacred row came from, and the keeper can still see
 * their own sacred notes on the recall page. Nothing else enforces the
 * partition; a dedicated firewall and audit log are not built.
 *
 * Not metered: the classifier call's tokens are returned in CaptureResult
 * but not written to companion_cost_log, so the daily cost cap does not
 * count them.
 */

import Anthropic from '@anthropic-ai/sdk';
import type { Pool } from 'pg';
import { embed } from './embeddings';
import { cosineSimilarity } from './cosine';

const HAIKU_MODEL = 'claude-haiku-4-5';
const SACRED_THRESHOLD = 0.7;
const DEDUPE_SIMILARITY_THRESHOLD = 0.93;

// Cap the dedupe fetch the same way retrieval caps its scan
// (COMPANION_MEMORY_MAX_FETCH_ROWS, default 5000). Without it, a large
// keeper does an unbounded SELECT + O(existing * new) cosine on the
// background capture path. Recent rows are the best dedupe candidates,
// so we order by recency and take the most recent N.
const DEDUPE_MAX_FETCH_ROWS = (() => {
  const raw = parseInt(process.env.COMPANION_MEMORY_MAX_FETCH_ROWS || '', 10);
  return Number.isFinite(raw) && raw > 0 ? raw : 5000;
})();

const VALID_CATEGORIES = new Set([
  'phrase',
  'fact',
  'preference',
  'fear',
  'milestone',
  'ritual',
  'audience',
  'practice',
  'other',
]);

export interface CapturedItem {
  category: string;
  content: string;
  keeper_phrase?: string;
  sacred_likelihood?: number;
}

export interface CaptureOpts {
  pool: Pool;
  trialId?: string;
  keeperId?: string;
  keeperText: string;
  recentContext: string;
  sourceTurnId: string;
}

export interface CaptureResult {
  insertedMemoryIds: string[];
  skippedDuplicateCount: number;
  embeddingTokens: number;
  classifierInputTokens: number;
  classifierOutputTokens: number;
}

let anthropicClient: Anthropic | null = null;

function getClient(): Anthropic {
  if (!anthropicClient) {
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      throw new Error('memory_capture: ANTHROPIC_API_KEY is not set');
    }
    anthropicClient = new Anthropic({ apiKey });
  }
  return anthropicClient;
}

/**
 * Test-only reset.
 */
export function _resetClientForTests(): void {
  anthropicClient = null;
}

const CLASSIFIER_SYSTEM = `You extract structured memory items from one
keeper utterance for a website cocreation companion. Output a JSON array
(only the array, no prose). Each item:

  {
    "category": one of [phrase, fact, preference, fear, milestone, ritual, audience, practice, other],
    "content": a third-person canonical statement, e.g. "The keeper's audience is somatic practitioners working with grief.",
    "keeper_phrase": optional verbatim quote from the keeper if it captures voice,
    "sacred_likelihood": float 0-1 estimating whether this is sensitive (grief, trauma, family, faith, identity)
  }

Capture only items that would be USEFUL to remember for future conversation.
Skip pleasantries, throwaway phrases, and meta-comments about the chat itself.
If nothing is worth capturing, output [].`;

/**
 * Run the classifier against a keeper utterance + recent context. Returns
 * 0-N candidate items. On parse failure returns [] and logs a warning.
 */
export async function classify(
  keeperText: string,
  recentContext: string
): Promise<{
  items: CapturedItem[];
  inputTokens: number;
  outputTokens: number;
}> {
  const client = getClient();
  const userMessage = recentContext
    ? `Recent context for grounding:\n${recentContext}\n\nKeeper just said:\n${keeperText}`
    : `Keeper just said:\n${keeperText}`;

  const response = await client.messages.create(
    {
      model: HAIKU_MODEL,
      max_tokens: 800,
      system: CLASSIFIER_SYSTEM,
      messages: [{ role: 'user', content: userMessage }],
    },
    { timeout: 30_000, maxRetries: 1 }
  );

  const textBlock = response.content.find((b) => b.type === 'text');
  if (!textBlock || textBlock.type !== 'text') {
    console.warn('memory_capture.classify: no text block in classifier response');
    return {
      items: [],
      inputTokens: response.usage.input_tokens,
      outputTokens: response.usage.output_tokens,
    };
  }

  const items = parseClassifierJson(textBlock.text);
  return {
    items,
    inputTokens: response.usage.input_tokens,
    outputTokens: response.usage.output_tokens,
  };
}

/**
 * Parse the classifier's JSON output. Tolerant of code-fenced output
 * and leading/trailing whitespace. Returns [] on any parse failure.
 */
export function parseClassifierJson(raw: string): CapturedItem[] {
  let text = raw.trim();
  // Strip markdown code fences if present.
  const fenceMatch = text.match(/^```(?:json)?\s*([\s\S]*?)```\s*$/);
  if (fenceMatch) {
    text = fenceMatch[1].trim();
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    console.warn('memory_capture.parse: classifier output was not valid JSON');
    return [];
  }
  if (!Array.isArray(parsed)) {
    console.warn('memory_capture.parse: classifier output was not an array');
    return [];
  }

  const items: CapturedItem[] = [];
  for (const raw of parsed) {
    if (!raw || typeof raw !== 'object') continue;
    const candidate = raw as Record<string, unknown>;
    const category = typeof candidate.category === 'string' ? candidate.category : null;
    const content = typeof candidate.content === 'string' ? candidate.content : null;
    if (!category || !content) continue;
    if (!VALID_CATEGORIES.has(category)) continue;
    items.push({
      category,
      content,
      keeper_phrase:
        typeof candidate.keeper_phrase === 'string' ? candidate.keeper_phrase : undefined,
      sacred_likelihood:
        typeof candidate.sacred_likelihood === 'number'
          ? candidate.sacred_likelihood
          : undefined,
    });
  }
  return items;
}

/**
 * Top-level capture: classify, embed, dedupe, insert.
 */
export async function captureMemory(opts: CaptureOpts): Promise<CaptureResult> {
  if (!opts.trialId && !opts.keeperId) {
    throw new Error('captureMemory: trialId or keeperId required');
  }
  if (opts.trialId && opts.keeperId) {
    throw new Error('captureMemory: only one of trialId or keeperId may be set');
  }

  // Capture works in two modes:
  //  - Full: OpenAI embeddings present -> semantic dedupe (cosine > 0.93)
  //          + JS-side similarity ranking at retrieval time.
  //  - Lite: no embeddings -> normalized-content dedupe + recency-only
  //          retrieval. Less smart but lets the founder dogfood Haven's
  //          memory layer without committing to an embedding provider.
  const hasEmbeddings = !!process.env.OPENAI_API_KEY;

  const { items, inputTokens, outputTokens } = await classify(
    opts.keeperText,
    opts.recentContext
  );

  if (items.length === 0) {
    return {
      insertedMemoryIds: [],
      skippedDuplicateCount: 0,
      embeddingTokens: 0,
      classifierInputTokens: inputTokens,
      classifierOutputTokens: outputTokens,
    };
  }

  // Embed only when OpenAI is wired up. Embed failures (quota, network,
  // bad key) drop us into lite mode for THIS capture: items still get
  // classified + persisted with NULL embeddings, so memory accrues even
  // when the embedding side is sick. Logged once; not propagated.
  let itemEmbeddings: Array<number[] | null> = items.map(() => null);
  let embeddingTokens = 0;
  if (hasEmbeddings) {
    try {
      const embedResult = await embed(items.map((i) => i.content));
      itemEmbeddings = embedResult.embeddings;
      embeddingTokens = embedResult.totalTokens;
    } catch (err) {
      console.warn(
        '[memory-capture] embed failed; falling back to lite-mode insert (no semantic ranking):',
        err instanceof Error ? err.message.slice(0, 200) : String(err)
      );
      // itemEmbeddings stays as all-nulls; embeddingTokens stays 0.
    }
  }

  const stored = await storeCapturedItems({
    pool: opts.pool,
    trialId: opts.trialId,
    keeperId: opts.keeperId,
    sourceTurnId: opts.sourceTurnId,
    items,
    embeddings: itemEmbeddings,
  });

  return {
    insertedMemoryIds: stored.insertedMemoryIds,
    skippedDuplicateCount: stored.skippedDuplicateCount,
    embeddingTokens,
    classifierInputTokens: inputTokens,
    classifierOutputTokens: outputTokens,
  };
}

/**
 * Insert one note for the conversation its source turn belongs to NOW.
 *
 * The owner columns are copied from the source turn's row in the same
 * statement, not taken from the caller. Capture runs in the background after
 * the reply has gone out, and a payment can land in between: promotion
 * (services/keeper_promotion.ts) moves the trial's turns to the keeper and
 * then moves the trial's notes. A note inserted under the trial id after that
 * second step would belong to a released trial that nothing reads any more.
 *
 * FOR SHARE on the turn row closes the gap in both orders. If promotion has
 * already moved the turn, this statement waits for it to commit and then reads
 * the keeper id. If this statement gets there first, promotion's update of the
 * turn waits for it, and its update of the notes, which runs afterwards, sees
 * the new note and moves it. That depends on promotion moving turns before
 * notes; the comment there says so.
 *
 * Inserts nothing when the turn no longer exists (its account was deleted).
 */
const INSERT_NOTE_FOR_TURN_SQL = `
  INSERT INTO keeper_memory
    (keeper_id, trial_id, partition, category, content,
     keeper_phrase, source_turn_id, embedding)
  SELECT ct.keeper_id, ct.trial_id, $2, $3, $4, $5, ct.turn_id, $6
  FROM companion_turns ct
  WHERE ct.turn_id = $1
  FOR SHARE OF ct
  RETURNING memory_id
`;

export interface StoreCapturedOpts {
  pool: Pool;
  /** Who the caller believes the turn belongs to; used only to find notes to dedupe against. */
  trialId?: string;
  keeperId?: string;
  sourceTurnId: string;
  items: CapturedItem[];
  /** One entry per item, null where there is no embedding. */
  embeddings: Array<number[] | null>;
}

/**
 * Dedupe classified items against the notes already held and insert the rest.
 * Split from `captureMemory` so the storage step can be run without a model.
 */
export async function storeCapturedItems(
  opts: StoreCapturedOpts
): Promise<{ insertedMemoryIds: string[]; skippedDuplicateCount: number }> {
  // Pull existing memory for dedupe. Always select content; embedding is
  // only used in full mode but we fetch it once. If the trial was promoted
  // since the turn, this finds nothing under the old id and dedupe is skipped
  // for this one capture, which costs at most a repeated note.
  const idColumn = opts.trialId ? 'trial_id' : 'keeper_id';
  const idValue = opts.trialId ?? opts.keeperId!;
  const { rows: existing } = await opts.pool.query<{
    memory_id: string;
    content: string;
    embedding: number[] | null;
  }>(
    `SELECT memory_id, content, embedding FROM keeper_memory
     WHERE ${idColumn} = $1 AND deleted_at IS NULL
     ORDER BY updated_at DESC
     LIMIT $2`,
    [idValue, DEDUPE_MAX_FETCH_ROWS]
  );

  const insertedMemoryIds: string[] = [];
  let skippedDuplicateCount = 0;

  for (let i = 0; i < opts.items.length; i++) {
    const item = opts.items[i];
    const embedding = opts.embeddings[i] ?? null;

    // Two-layer dedupe: semantic (when embeddings present) THEN content
    // (normalized lowercase) as a backstop. Either layer hitting marks
    // the item as duplicate.
    let isDuplicate = false;
    if (embedding) {
      isDuplicate = existing.some(
        (e) =>
          e.embedding &&
          cosineSimilarity(e.embedding, embedding) > DEDUPE_SIMILARITY_THRESHOLD
      );
    }
    if (!isDuplicate) {
      const norm = normalizeContent(item.content);
      isDuplicate = existing.some((e) => normalizeContent(e.content) === norm);
    }
    if (isDuplicate) {
      skippedDuplicateCount += 1;
      continue;
    }

    const partition =
      (item.sacred_likelihood ?? 0) > SACRED_THRESHOLD ? 'sacred' : 'working';
    const insertResult = await opts.pool.query<{ memory_id: string }>(
      INSERT_NOTE_FOR_TURN_SQL,
      [
        opts.sourceTurnId,
        partition,
        item.category,
        item.content,
        item.keeper_phrase ?? null,
        embedding,
      ]
    );
    if (insertResult.rows.length === 0) {
      console.warn(
        '[memory-capture] source turn %s no longer exists; its notes were not stored',
        opts.sourceTurnId
      );
      break;
    }
    insertedMemoryIds.push(insertResult.rows[0].memory_id);
  }

  return { insertedMemoryIds, skippedDuplicateCount };
}

/**
 * Normalize content for the lite-mode dedupe path: lowercase, trim,
 * collapse whitespace. Only used when embeddings are unavailable; in
 * full mode the cosine layer catches near-duplicates that don't string-
 * match.
 */
export function normalizeContent(s: string): string {
  return s.trim().toLowerCase().replace(/\s+/g, ' ');
}
