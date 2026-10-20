/**
 * Weekly Haiku sweep of companion_turns to feed support_kb.
 *
 * Closes the loop between Haven (the keeper-facing AI companion) and the
 * support chat: questions keepers ask Haven during the conversation
 * surface but that the support_kb cache never sees. Once a week, pull the
 * last 7 days of keeper utterances, ask Haiku to extract 3-5 recurring
 * topic clusters with a draft Q&A pair for each, and INSERT each pair
 * into support_kb WHERE NOT EXISTS (deduped by normalized question).
 *
 * Cost target: ~$0.01/week at Haiku pricing. The single Haiku call
 * receives a concatenated keeper-text corpus capped at 30K chars so the
 * sweep stays bounded in token spend even on a busy week.
 *
 * Read-only against `companion_turns`; deduplicated against `support_kb`
 * via the WHERE NOT EXISTS guard on `question_normalized`. The guard matches
 * the exact normalized question, and the model words each question a little
 * differently on every run, so re-running yields new near-duplicate rows
 * rather than a no-op.
 *
 * Inputs and outputs are filtered because the corpus is other people's chat
 * text and the output is meant for the support chat:
 *  - crisis-flagged turns and turns from bot-classified trials are not read;
 *  - at most MAX_TURN_ROWS newest turns are loaded;
 *  - a generated pair is rejected (`isValidCluster`) when it carries a URL, a
 *    `www.` host, any other dotted host name but the product's own domain, an
 *    IPv4 address, a tag or entity, an email address, or a run of seven or
 *    more digits shaped like a phone number, so a visitor cannot get their own
 *    link or number cached as a support answer by repeating it in chat. The
 *    check reads shapes: an address spelled out in words passes it.
 *
 * Review: a pair that passes the filters is inserted with status 'pending',
 * and the support chat (routes/support.ts) serves only 'approved' rows, so
 * nothing this sweep writes reaches a customer until an operator approves it.
 * There is no admin page for that; an operator reads the pending rows and
 * approves one by hand (the statement is next to the column in
 * db/schema.sql). The filters do not stop a false statement that several
 * visitors (or one visitor with several trial cookies) repeated until the
 * model treated it as a recurring topic; the approval is what does.
 *
 * `harbor-cron-scheduler.ts` runs this on a timer. This module exposes a
 * single pure-pg sweep function so it stays test-friendly. The Haiku call is
 * injected via `extractFn` so tests can pass a fake; production wires through
 * to `extractWithHaiku()`.
 */

import Anthropic from '@anthropic-ai/sdk';
import type { Pool } from 'pg';

const HAIKU_MODEL = 'claude-haiku-4-5-20251001';
const MAX_OUTPUT_TOKENS = 1500;
const HAIKU_PRICING_PER_MILLION = { input: 1, output: 5 };

const DEFAULT_WINDOW_DAYS = 7;
const DEFAULT_MIN_TURNS = 10;
const DEFAULT_MAX_CLUSTERS = 5;
const DEFAULT_MIN_CLUSTERS = 3;
const DEFAULT_CORPUS_CHAR_CAP = 30_000;
const PER_TURN_CHAR_CAP = 1_500;

const MAX_QUESTION_CHARS = 200;
const MAX_ANSWER_CHARS = 1_500;

/** Hard cap on turn rows loaded per run (the newest are kept). */
const MAX_TURN_ROWS = 5_000;

let anthropicClient: Anthropic | null = null;

function getClient(): Anthropic {
  if (!anthropicClient) {
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      throw new Error('topic_kb_sweep: ANTHROPIC_API_KEY is not set');
    }
    anthropicClient = new Anthropic({ apiKey });
  }
  return anthropicClient;
}

export function _resetClientForTests(): void {
  anthropicClient = null;
}

export interface TopicKbSweepOpts {
  pool: Pool;
  /** Window in days. Default 7. */
  windowDays?: number;
  /** Minimum keeper-text rows in the window before we call Haiku. Default 10. */
  minTurns?: number;
  /** Hard ceiling on Q&A pairs the model may emit. Default 5. */
  maxClusters?: number;
  /** Per-keeper-text truncation. Default 1500 chars. */
  perTurnCharCap?: number;
  /** Total corpus cap fed to Haiku. Default 30000 chars. */
  corpusCharCap?: number;
  /** Now() override for tests so windows are deterministic. */
  now?: Date;
  /** Extract function (test seam). Defaults to extractWithHaiku. */
  extractFn?: ExtractFn;
}

export type ExtractFn = (input: {
  corpus: string;
  maxClusters: number;
  windowDays: number;
}) => Promise<ExtractResult>;

export interface ExtractResult {
  clusters: TopicCluster[];
  costMicros: number;
}

export interface TopicCluster {
  question: string;
  answer: string;
}

export interface TopicKbSweepResult {
  windowDays: number;
  turnsScanned: number;
  clustersExtracted: number;
  insertedCount: number;
  skippedDuplicateCount: number;
  skippedInvalidCount: number;
  totalCostMicros: number;
  inserted: TopicCluster[];
}

interface TurnRow {
  keeper_text: string;
}

/**
 * Run a single weekly sweep pass. Idempotent: clusters already cached
 * (matched by normalized question) are skipped.
 */
export async function runTopicKbSweep(
  opts: TopicKbSweepOpts
): Promise<TopicKbSweepResult> {
  const windowDays = sanitizePositiveInt(opts.windowDays, DEFAULT_WINDOW_DAYS);
  const minTurns = sanitizePositiveInt(opts.minTurns, DEFAULT_MIN_TURNS);
  const maxClusters = clampClusters(opts.maxClusters, DEFAULT_MAX_CLUSTERS);
  const perTurnCap = sanitizePositiveInt(opts.perTurnCharCap, PER_TURN_CHAR_CAP);
  const corpusCap = sanitizePositiveInt(opts.corpusCharCap, DEFAULT_CORPUS_CHAR_CAP);
  const extractFn = opts.extractFn ?? extractWithHaiku;
  const nowExpr = opts.now
    ? `TIMESTAMP '${opts.now.toISOString()}'`
    : 'NOW()';

  const result: TopicKbSweepResult = {
    windowDays,
    turnsScanned: 0,
    clustersExtracted: 0,
    insertedCount: 0,
    skippedDuplicateCount: 0,
    skippedInvalidCount: 0,
    totalCostMicros: 0,
    inserted: [],
  };

  // Newest MAX_TURN_ROWS first (inner query), then oldest-first for the corpus.
  const turnsRes = await opts.pool.query<TurnRow>(
    `SELECT keeper_text
     FROM (
       SELECT ct.keeper_text, ct.created_at
       FROM companion_turns ct
       LEFT JOIN trial_keepers tk ON tk.trial_id = ct.trial_id
       WHERE ct.keeper_text IS NOT NULL
         AND ct.keeper_text <> ''
         AND NOT ('crisis' = ANY(ct.rail_flags))
         AND COALESCE(tk.is_synthetic, FALSE) = FALSE
         AND ct.created_at >= ${nowExpr} - INTERVAL '${windowDays} days'
         AND ct.created_at <= ${nowExpr}
       ORDER BY ct.created_at DESC
       LIMIT $1
     ) recent
     ORDER BY created_at ASC`,
    [MAX_TURN_ROWS]
  );

  result.turnsScanned = turnsRes.rows.length;
  if (turnsRes.rows.length < minTurns) {
    return result;
  }

  const corpus = buildCorpus(
    turnsRes.rows.map((r) => r.keeper_text),
    perTurnCap,
    corpusCap
  );

  const extracted = await extractFn({
    corpus,
    maxClusters,
    windowDays,
  });
  result.totalCostMicros += extracted.costMicros;
  result.clustersExtracted = extracted.clusters.length;

  for (const cluster of extracted.clusters.slice(0, maxClusters)) {
    if (!isValidCluster(cluster)) {
      result.skippedInvalidCount += 1;
      continue;
    }
    const normalized = normalizeQuestion(cluster.question);
    // Stored as 'pending': the support chat does not serve it until an
    // operator approves it (see "Review" at the top of this file).
    const insert = await opts.pool.query(
      `INSERT INTO support_kb (question, question_normalized, answer, status)
       SELECT $1, $2, $3, 'pending'
       WHERE NOT EXISTS (
         SELECT 1 FROM support_kb WHERE question_normalized = $2
       )`,
      [cluster.question, normalized, cluster.answer]
    );
    if ((insert.rowCount ?? 0) > 0) {
      result.insertedCount += 1;
      result.inserted.push(cluster);
    } else {
      result.skippedDuplicateCount += 1;
    }
  }

  return result;
}

/**
 * Concatenate keeper utterances into a single Haiku-bound corpus.
 * Each turn is truncated to perTurnCap chars (defends against a
 * pathological mega-message) and the total is capped at corpusCap.
 */
export function buildCorpus(
  keeperTexts: string[],
  perTurnCap: number,
  corpusCap: number
): string {
  const parts: string[] = [];
  let total = 0;
  for (const raw of keeperTexts) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const chunk = trimmed.length > perTurnCap
      ? trimmed.slice(0, perTurnCap)
      : trimmed;
    const block = `- ${chunk}`;
    if (total + block.length + 1 > corpusCap) break;
    parts.push(block);
    total += block.length + 1;
  }
  return parts.join('\n');
}

export function buildExtractPrompt(opts: {
  corpus: string;
  maxClusters: number;
  windowDays: number;
}): string {
  return [
    `You are reviewing the last ${opts.windowDays} days of keeper messages`,
    `to Craft & Ship's AI companion (Haven). Each line below is one keeper`,
    `utterance, prefixed with "- ".`,
    ``,
    `Identify ${DEFAULT_MIN_CLUSTERS} to ${opts.maxClusters} RECURRING`,
    `topics where multiple keepers asked similar questions about Craft &`,
    `Ship as a product (pricing, hosting, what's included, how it works,`,
    `how to revise, how to cancel, how the build works). For each, write`,
    `one canonical question (≤ ${MAX_QUESTION_CHARS} chars) and one helpful`,
    `support-chat-style answer (≤ ${MAX_ANSWER_CHARS} chars).`,
    ``,
    `Skip topics that are personal to one keeper, are emotional support,`,
    `are not about the Craft & Ship product, or only appear in one`,
    `utterance. We want repeated patterns, not one-offs.`,
    ``,
    `Output a JSON object with one field, "clusters", whose value is an`,
    `array of objects with "question" and "answer" string fields. No`,
    `markdown, no fences, no preamble.`,
    ``,
    `Hard rules for question and answer text:`,
    `  - NO em dashes. Use colons, semicolons, or commas.`,
    `  - "website" not "app" / "platform" / "software".`,
    `  - Be plain and friendly; under 3 paragraphs in the answer.`,
    `  - Do not invent pricing or features that may not exist; describe`,
    `    only what is generally true for Craft & Ship.`,
    ``,
    `If fewer than ${DEFAULT_MIN_CLUSTERS} recurring topics exist, output`,
    `an empty clusters array: {"clusters": []}.`,
    ``,
    `--- Keeper utterances (last ${opts.windowDays} days) ---`,
    opts.corpus,
  ].join('\n');
}

/**
 * Production extract path. Calls Haiku with the corpus and parses /
 * lints the JSON output. Used as the default extractFn when callers
 * don't inject one.
 */
export async function extractWithHaiku(input: {
  corpus: string;
  maxClusters: number;
  windowDays: number;
}): Promise<ExtractResult> {
  const client = getClient();
  const prompt = buildExtractPrompt(input);
  const response = await client.messages.create(
    {
      model: HAIKU_MODEL,
      max_tokens: MAX_OUTPUT_TOKENS,
      messages: [{ role: 'user', content: prompt }],
    },
    { timeout: 90_000, maxRetries: 1 }
  );

  const textBlock = response.content.find((b) => b.type === 'text');
  if (!textBlock || textBlock.type !== 'text') {
    return { clusters: [], costMicros: computeHaikuCost(response.usage) };
  }
  const parsed = parseClustersJson(textBlock.text);
  return {
    clusters: parsed,
    costMicros: computeHaikuCost(response.usage),
  };
}

export function parseClustersJson(raw: string): TopicCluster[] {
  let text = raw.trim();
  const fence = text.match(/^```(?:json)?\n?([\s\S]*?)\n?```$/);
  if (fence) text = fence[1].trim();
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return [];
  const obj = parsed as Record<string, unknown>;
  const arr = obj.clusters;
  if (!Array.isArray(arr)) return [];
  const clusters: TopicCluster[] = [];
  for (const item of arr) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    if (typeof rec.question !== 'string' || rec.question.trim().length === 0) continue;
    if (typeof rec.answer !== 'string' || rec.answer.trim().length === 0) continue;
    clusters.push({
      question: rec.question.trim(),
      answer: rec.answer.trim(),
    });
  }
  return clusters;
}

/**
 * Reject clusters that violate brand voice or shape constraints.
 * Em dash detection uses Unicode escape so this file itself never
 * contains a literal em dash (would trip the brand-voice lint).
 */
export function isValidCluster(cluster: TopicCluster): boolean {
  if (!cluster.question || !cluster.answer) return false;
  if (cluster.question.length > MAX_QUESTION_CHARS) return false;
  if (cluster.answer.length > MAX_ANSWER_CHARS) return false;
  if (containsEmDash(cluster.question) || containsEmDash(cluster.answer)) {
    return false;
  }
  // Once approved, the support chat serves these answers to strangers.
  // Visitor-typed text reaches the model through the corpus, so refuse
  // anything that could carry a link, markup or a number to call.
  if (containsLinkOrMarkup(cluster.question) || containsLinkOrMarkup(cluster.answer)) {
    return false;
  }
  return true;
}

// The email alternative looks at one character before the "@" (a longer local
// part matches whenever its last character does), which keeps the test linear
// on a long run of text with no "@" in it.
const LINK_OR_MARKUP_RE = /https?:\/\/|www\.|<[a-z/!]|&lt;|[^\s@]@[^\s@]+\.[a-z]{2,}/i;

/** The product's own domain, the one host name an answer may mention (with its subdomains). */
const PRODUCT_DOMAIN = 'craftandship.io';

/**
 * True when `text` names a host: a dotted name whose last label starts with a
 * letter and has two characters or more (paypal.me, t.me, bit.ly,
 * example.shop), or a dotted-quad address. "e.g.", "3.5" and "$19.99" are not
 * host names. Unicode letters count as letters and compatibility forms (a
 * full-width dot) are folded first. Linear in the text.
 */
function containsHostName(text: string): boolean {
  for (const token of text.normalize('NFKC').toLowerCase().split(/[^\p{L}\p{N}.-]+/u)) {
    // Sentence punctuation around a name ("craftandship.io." at the end of a
    // sentence) is not part of it; trimmed with a loop, which stays linear.
    let start = 0;
    let end = token.length;
    while (start < end && (token[start] === '.' || token[start] === '-')) start += 1;
    while (end > start && (token[end - 1] === '.' || token[end - 1] === '-')) end -= 1;
    const host = token.slice(start, end);
    if (!host.includes('.')) continue;
    const labels = host.split('.');
    const last = labels[labels.length - 1];
    const isName = /^\p{L}[\p{L}\p{N}-]+$/u.test(last);
    const isAddress = labels.length === 4 && labels.every((label) => /^\d{1,3}$/.test(label));
    if (!isName && !isAddress) continue;
    if (host === PRODUCT_DOMAIN || host.endsWith(`.${PRODUCT_DOMAIN}`)) continue;
    return true;
  }
  return false;
}

/** True when `text` holds a run of seven or more digits written as one number (512-555-0100, +1 (512) 555 0100). */
function containsPhoneNumber(text: string): boolean {
  for (const run of text.normalize('NFKC').match(/[\d\s().+-]+/g) ?? []) {
    if ((run.match(/\d/g) ?? []).length >= 7) return true;
  }
  return false;
}

export function containsLinkOrMarkup(text: string): boolean {
  return LINK_OR_MARKUP_RE.test(text) || containsHostName(text) || containsPhoneNumber(text);
}

function containsEmDash(text: string): boolean {
  if (text.includes('\u2014')) return true;
  return new RegExp('&' + 'mdash;', 'i').test(text);
}

/**
 * Normalize a question to a dedupe key. Mirrors the `normalizeText`
 * used in routes/support.ts so cron-inserted rows collide with
 * support-chat-cached rows on the same canonical phrasing.
 */
export function normalizeQuestion(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\s]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function computeHaikuCost(usage: {
  input_tokens: number;
  output_tokens: number;
}): number {
  return Math.round(
    usage.input_tokens * HAIKU_PRICING_PER_MILLION.input +
      usage.output_tokens * HAIKU_PRICING_PER_MILLION.output
  );
}

function sanitizePositiveInt(value: number | undefined, fallback: number): number {
  if (typeof value !== 'number') return fallback;
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.floor(value);
}

function clampClusters(value: number | undefined, fallback: number): number {
  const n = sanitizePositiveInt(value, fallback);
  if (n > 20) return 20; // hard ceiling regardless of caller
  return n;
}
