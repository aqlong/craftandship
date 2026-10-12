/**
 * Weekly Haven persona learning digest.
 *
 * Closes the human-in-the-loop on Haven persona tuning. The static
 * persona prompt (`web/src/companion/persona.ts`, text in
 * `persona-sections.json`) cannot be auto-patched: it ships with the app and
 * a change requires a code commit. The
 * upstream learning agents (rail-flag sweep, conversion-funnel analysis,
 * harbor extraction warnings, archetype-correction signal) all write
 * suggestions into `agent_learnings` under persona-tuning categories.
 *
 * Without a surfacing mechanism those rows accumulate silently until
 * someone runs a manual SQL query. This sweep reads any unapplied rows
 * in the persona-tuning categories and emails Ocean a plain-text bullet
 * summary so he can review and apply them as a single persona commit.
 *
 * Filter: `last_applied_at IS NULL` AND `category IN (harbor_persona,
 * archetype_correction, harbor_extraction)`.
 *
 * Read-only against `agent_learnings`. Idempotent: calling the digest
 * does NOT mark rows as applied. Ocean's commit landing the suggested
 * persona changes is the side effect that flips `last_applied_at` (or
 * the operator can update the rows manually after acting on the digest).
 */

import type { Pool } from 'pg';
import { sendPersonaLearningDigestEmail } from '../services/email';

export const PERSONA_DIGEST_CATEGORIES = [
  'harbor_persona',
  'archetype_correction',
  'harbor_extraction',
] as const;

export interface PersonaDigestRow {
  id: string;
  source_agent: string;
  category: string;
  learning: string;
  confidence: number;
  created_at: Date | string;
}

export interface PersonaLearningDigestOpts {
  pool: Pool;
  /** Window in days for `created_at` lower bound. Default 30. */
  windowDays?: number;
  /** Per-category cap of rows surfaced into the digest. Default 25. */
  maxPerCategory?: number;
  /** Total cap across all categories. Default 75. */
  maxTotal?: number;
  /** Test seam: override the email sender. Defaults to the real Resend send. */
  sendFn?: (opts: { subject: string; body: string; to?: string }) => Promise<void>;
}

export interface PersonaLearningDigestResult {
  windowDays: number;
  totalCandidates: number;
  surfacedCount: number;
  byCategory: Record<string, number>;
  emailed: boolean;
  skippedReason?: 'no_candidates' | 'send_failed';
}

const DEFAULT_WINDOW_DAYS = 30;
const DEFAULT_MAX_PER_CATEGORY = 25;
const DEFAULT_MAX_TOTAL = 75;

export async function runPersonaLearningDigest(
  opts: PersonaLearningDigestOpts
): Promise<PersonaLearningDigestResult> {
  const windowDays = sanitizePositiveInt(opts.windowDays, DEFAULT_WINDOW_DAYS);
  const maxPerCategory = sanitizePositiveInt(
    opts.maxPerCategory,
    DEFAULT_MAX_PER_CATEGORY
  );
  const maxTotal = sanitizePositiveInt(opts.maxTotal, DEFAULT_MAX_TOTAL);

  // Pull at most maxPerCategory rows per category (newest first), then
  // cap the total so a single noisy category cannot drown out the rest.
  const sql = `
    SELECT id, source_agent, category, learning, confidence, created_at
    FROM (
      SELECT id, source_agent, category, learning, confidence, created_at,
             ROW_NUMBER() OVER (
               PARTITION BY category
               ORDER BY created_at DESC, id DESC
             ) AS rn
      FROM agent_learnings
      WHERE category = ANY($1::text[])
        AND last_applied_at IS NULL
        AND created_at >= NOW() - ($2 || ' days')::INTERVAL
    ) ranked
    WHERE rn <= $3
    ORDER BY category ASC, created_at DESC, id DESC
  `;
  const result = await opts.pool.query<PersonaDigestRow>(sql, [
    [...PERSONA_DIGEST_CATEGORIES],
    String(windowDays),
    maxPerCategory,
  ]);

  const rows = result.rows.slice(0, maxTotal);
  const byCategory: Record<string, number> = {};
  for (const row of rows) {
    byCategory[row.category] = (byCategory[row.category] ?? 0) + 1;
  }

  if (rows.length === 0) {
    return {
      windowDays,
      totalCandidates: 0,
      surfacedCount: 0,
      byCategory,
      emailed: false,
      skippedReason: 'no_candidates',
    };
  }

  const subject = `[CAS Harbor] Haven persona digest: ${rows.length} unapplied learning${rows.length === 1 ? '' : 's'}`;
  const body = formatDigestBody({
    rows,
    windowDays,
    byCategory,
  });

  const send = opts.sendFn ?? defaultSend;
  try {
    await send({ subject, body });
  } catch (err) {
    console.error(
      '[persona-learning-digest] email send failed:',
      (err as Error).message
    );
    return {
      windowDays,
      totalCandidates: rows.length,
      surfacedCount: rows.length,
      byCategory,
      emailed: false,
      skippedReason: 'send_failed',
    };
  }

  return {
    windowDays,
    totalCandidates: rows.length,
    surfacedCount: rows.length,
    byCategory,
    emailed: true,
  };
}

async function defaultSend(opts: {
  subject: string;
  body: string;
}): Promise<void> {
  await sendPersonaLearningDigestEmail({
    subject: opts.subject,
    body: opts.body,
  });
}

interface FormatBodyOpts {
  rows: PersonaDigestRow[];
  windowDays: number;
  byCategory: Record<string, number>;
}

/**
 * Format a plain-text digest body. The rendering is deliberately spartan
 * (no markdown, minimal punctuation) so it reads cleanly inside the
 * existing HTML email shell when wrapped in a `<pre>` block, and also
 * survives copy-paste into a commit message or planning doc.
 */
export function formatDigestBody(opts: FormatBodyOpts): string {
  const lines: string[] = [];
  lines.push(
    `Unapplied Haven persona learnings over the last ${opts.windowDays} days.`
  );
  lines.push('');
  lines.push('Counts by category:');
  for (const cat of PERSONA_DIGEST_CATEGORIES) {
    const n = opts.byCategory[cat] ?? 0;
    lines.push(`  - ${cat}: ${n}`);
  }
  lines.push('');

  const grouped = new Map<string, PersonaDigestRow[]>();
  for (const row of opts.rows) {
    const arr = grouped.get(row.category) ?? [];
    arr.push(row);
    grouped.set(row.category, arr);
  }

  for (const cat of PERSONA_DIGEST_CATEGORIES) {
    const catRows = grouped.get(cat);
    if (!catRows || catRows.length === 0) continue;
    lines.push(`== ${cat} (${catRows.length}) ==`);
    for (const row of catRows) {
      const created =
        row.created_at instanceof Date
          ? row.created_at.toISOString().slice(0, 10)
          : String(row.created_at).slice(0, 10);
      const conf = Number(row.confidence ?? 0).toFixed(2);
      const learning = collapseWhitespace(row.learning ?? '');
      lines.push(
        `  * [${created}] (${row.source_agent}, conf=${conf}) ${learning}`
      );
    }
    lines.push('');
  }

  lines.push(
    'Review these in the order listed. Apply by editing ' +
      'web/src/companion/persona-sections.json (the persona text lives ' +
      'there), then mark the relevant rows applied:'
  );
  lines.push(
    "  UPDATE agent_learnings SET last_applied_at = NOW() WHERE id IN (..);"
  );
  lines.push('');
  lines.push('This is an operational alert; not subject to unsubscribe.');

  return lines.join('\n');
}

function collapseWhitespace(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

function sanitizePositiveInt(value: number | undefined, fallback: number): number {
  if (typeof value !== 'number') return fallback;
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.floor(value);
}
