/**
 * Weekly Haven rail-flag aggregation sweep.
 *
 * Closes the loop between Haven's live output (rows in `companion_turns`
 * carrying `rail_flags TEXT[]`) and the persona prompt
 * (`web/src/companion/persona.ts`).
 *
 * Counts flag fires across the most recent N days (default 7) at two
 * scopes: per-(trial-or-keeper)-and-flag, and population-wide-per-flag.
 * Findings whose flag rate exceeds 15% across at least 5 turns get
 * recorded into `agent_learnings` with suggested persona guardrail
 * language so the next persona-tuning pass has signal to act on.
 *
 * Read-only against `companion_turns`; idempotent against
 * `agent_learnings` thanks to the table's
 * UNIQUE(source_agent, category, learning_hash) constraint and ON
 * CONFLICT DO UPDATE upsert.
 *
 * `harbor-cron-scheduler.ts` runs this on a timer. This module exposes a
 * single pure-pg sweep function so it stays test-friendly.
 */

import type { Pool } from 'pg';
import * as crypto from 'crypto';

export interface RailFlagSweepOpts {
  pool: Pool;
  /** Window in days. Default 7. */
  windowDays?: number;
  /** Minimum turn count for a (scope, flag) bucket to be evaluated. Default 5. */
  minTurns?: number;
  /** Flag rate threshold (fires / turns) for a finding. Default 0.15. */
  flagRateThreshold?: number;
  /** Per-run cap on agent_learnings inserts. Default 100. */
  maxFindings?: number;
  /** Now() override for tests so windows are deterministic. */
  now?: Date;
}

export type FindingScope = 'keeper' | 'trial' | 'population';

export interface RailFlagFinding {
  scope: FindingScope;
  keeperId: string | null;
  trialId: string | null;
  flag: string;
  totalTurns: number;
  flaggedTurns: number;
  flagRate: number;
  guardrail: string;
  learning: string;
}

export interface RailFlagSweepResult {
  windowDays: number;
  totalTurnsScanned: number;
  populationFindings: number;
  perScopeFindings: number;
  findingsRecorded: number;
  findings: RailFlagFinding[];
}

const DEFAULT_WINDOW_DAYS = 7;
const DEFAULT_MIN_TURNS = 5;
const DEFAULT_FLAG_RATE_THRESHOLD = 0.15;
const DEFAULT_MAX_FINDINGS = 100;

const SOURCE_AGENT = 'haven_rail_sweep';
const LEARNING_CATEGORY = 'persona_guardrail';

/**
 * Persona guardrail language indexed by rail-flag type. Each entry is
 * the suggested copy a persona-tuning pass should fold into the
 * companion prompt to lower the rate of that flag.
 *
 * Flags not present in this map fall back to a generic guardrail; the
 * sweep records the finding either way so the operator can decide
 * whether to add a tailored entry.
 */
const PERSONA_GUARDRAIL_LANGUAGE: Record<string, string> = {
  'ai-impersonation-attempt':
    'Reinforce honest-AI rail. When the keeper asks "are you human?" Haven names that she is an AI built by Ocean and offers to sit with what is behind the question.',
  'roleplay-request':
    'Decline roleplay or pretending-to-be-someone-else asks. Name that the trust between keeper and companion is real and the trick would not be.',
  'therapy-request':
    'When therapy or counseling is asked for, Haven names she is not enough for that work and points to 988 / a real human. Do not let conversation drift into ongoing therapy.',
  'medical-advice-request':
    'Decline medical advice. Point to a real clinician who knows the keeper.',
  'legal-advice-request':
    'Decline legal advice. Point to a real lawyer in the keeper\'s jurisdiction.',
  'fake-content-request':
    'Refuse to write fake testimonials, reviews, credentials, or quotes on the keeper\'s behalf. Offer a graceful placeholder line for now.',
  'impersonation-of-keeper':
    'Refuse to write as the keeper to their clients. Offer a clearly-labeled auto-reply or a template the keeper sends themselves.',
  'output-claims-personhood':
    'Persona regression: Haven slipped into claiming personhood. Tighten the AI-honesty section so she never writes "as a human" / "I am a real person" / "I am not an AI" / "speaking as someone who lived through".',
  'output-uses-em-dash':
    'Brand-voice regression: Haven keeps emitting em dashes. Reinforce the "no em dashes; use commas, colons, semicolons" rule near the top of the persona prompt.',
  crisis:
    'Crisis events are firing in the conversation surface. Review the crisis script and verify Haven is short-circuiting before any therapy-shaped reply.',
  suicide:
    'Suicide language is reaching Haven directly. Verify crisis_detection.ts is firing the deterministic 988 script before any persona generation.',
  'self-harm':
    'Self-harm language is reaching Haven directly. Verify crisis_detection.ts is firing the deterministic crisis script.',
  abuse:
    'Abuse disclosures are reaching Haven directly. Verify crisis_detection.ts is firing the 911 / findahelpline.com script and ADMIN_EMAIL is being notified.',
  'acute-medical':
    'Acute-medical phrasing is reaching Haven directly. Verify the deterministic crisis script fires before any persona reply.',
  'harm-to-others':
    'Statements about hurting another person are reaching Haven directly. Verify crisis_detection.ts is firing the deterministic crisis script, with the 911 line, and ADMIN_EMAIL is being notified.',
};

const GENERIC_GUARDRAIL =
  'Unrecognized rail flag firing at high rate. Add a tailored persona guardrail entry for this flag and audit recent companion_turns.';

interface AggRow {
  trial_id: string | null;
  keeper_id: string | null;
  flag: string;
  turns_with_flag: string; // pg returns BIGINT as string
  total_turns: string;
}

interface PopulationRow {
  flag: string;
  turns_with_flag: string;
  total_turns: string;
}

/**
 * Run a single weekly sweep pass. Safe to invoke on demand; idempotent
 * against the `agent_learnings` table via ON CONFLICT DO UPDATE.
 */
export async function runRailFlagSweep(
  opts: RailFlagSweepOpts
): Promise<RailFlagSweepResult> {
  const windowDays = sanitizePositiveInt(opts.windowDays, DEFAULT_WINDOW_DAYS);
  const minTurns = sanitizePositiveInt(opts.minTurns, DEFAULT_MIN_TURNS);
  const flagRateThreshold = sanitizeFlagRate(
    opts.flagRateThreshold,
    DEFAULT_FLAG_RATE_THRESHOLD
  );
  const maxFindings = sanitizePositiveInt(
    opts.maxFindings,
    DEFAULT_MAX_FINDINGS
  );
  const nowExpr = opts.now
    ? `TIMESTAMP '${opts.now.toISOString()}'`
    : 'NOW()';

  // Per-scope aggregation: each (trial_id|keeper_id, flag) bucket gets a
  // turns-with-flag count plus the total turns the keeper / trial took
  // in the window so we can compute a rate. UNNEST flattens rail_flags
  // so each fired flag becomes one row.
  const perScopeSql = `
    WITH window_turns AS (
      SELECT turn_id, keeper_id, trial_id, rail_flags
      FROM companion_turns
      WHERE created_at >= ${nowExpr} - INTERVAL '${windowDays} days'
        AND created_at <= ${nowExpr}
    ),
    scope_totals AS (
      SELECT keeper_id, trial_id, COUNT(*) AS total_turns
      FROM window_turns
      GROUP BY keeper_id, trial_id
    ),
    flagged_turns AS (
      SELECT DISTINCT keeper_id, trial_id, turn_id, flag
      FROM window_turns,
           LATERAL UNNEST(rail_flags) AS flag
    ),
    flag_counts AS (
      SELECT keeper_id, trial_id, flag, COUNT(*) AS turns_with_flag
      FROM flagged_turns
      GROUP BY keeper_id, trial_id, flag
    )
    SELECT
      fc.keeper_id,
      fc.trial_id,
      fc.flag,
      fc.turns_with_flag,
      st.total_turns
    FROM flag_counts fc
    JOIN scope_totals st
      ON COALESCE(fc.keeper_id::text, '') = COALESCE(st.keeper_id::text, '')
     AND COALESCE(fc.trial_id::text, '') = COALESCE(st.trial_id::text, '')
    WHERE st.total_turns >= $1
  `;
  const perScope = await opts.pool.query<AggRow>(perScopeSql, [minTurns]);

  const populationSql = `
    WITH window_turns AS (
      SELECT turn_id, rail_flags
      FROM companion_turns
      WHERE created_at >= ${nowExpr} - INTERVAL '${windowDays} days'
        AND created_at <= ${nowExpr}
    ),
    pop_totals AS (
      SELECT COUNT(*) AS total_turns FROM window_turns
    ),
    flagged_turns AS (
      SELECT DISTINCT turn_id, flag
      FROM window_turns,
           LATERAL UNNEST(rail_flags) AS flag
    ),
    flag_counts AS (
      SELECT flag, COUNT(*) AS turns_with_flag
      FROM flagged_turns
      GROUP BY flag
    )
    SELECT
      fc.flag,
      fc.turns_with_flag,
      pt.total_turns
    FROM flag_counts fc
    CROSS JOIN pop_totals pt
    WHERE pt.total_turns >= $1
  `;
  const population = await opts.pool.query<PopulationRow>(populationSql, [
    minTurns,
  ]);

  const findings: RailFlagFinding[] = [];
  let totalTurnsScanned = 0;

  for (const row of perScope.rows) {
    const total = Number(row.total_turns);
    const flagged = Number(row.turns_with_flag);
    if (total < minTurns) continue;
    const rate = flagged / total;
    if (rate <= flagRateThreshold) continue;

    const scope: FindingScope = row.keeper_id ? 'keeper' : 'trial';
    const guardrail = guardrailFor(row.flag);
    const learning = formatLearning({
      scope,
      keeperId: row.keeper_id,
      trialId: row.trial_id,
      flag: row.flag,
      flaggedTurns: flagged,
      totalTurns: total,
      rate,
      windowDays,
      guardrail,
    });
    findings.push({
      scope,
      keeperId: row.keeper_id,
      trialId: row.trial_id,
      flag: row.flag,
      totalTurns: total,
      flaggedTurns: flagged,
      flagRate: rate,
      guardrail,
      learning,
    });
  }

  for (const row of population.rows) {
    const total = Number(row.total_turns);
    const flagged = Number(row.turns_with_flag);
    if (total < minTurns) continue;
    if (totalTurnsScanned === 0) totalTurnsScanned = total;
    const rate = flagged / total;
    if (rate <= flagRateThreshold) continue;

    const guardrail = guardrailFor(row.flag);
    const learning = formatLearning({
      scope: 'population',
      keeperId: null,
      trialId: null,
      flag: row.flag,
      flaggedTurns: flagged,
      totalTurns: total,
      rate,
      windowDays,
      guardrail,
    });
    findings.push({
      scope: 'population',
      keeperId: null,
      trialId: null,
      flag: row.flag,
      totalTurns: total,
      flaggedTurns: flagged,
      flagRate: rate,
      guardrail,
      learning,
    });
  }

  // Stable order: population findings first (broadest signal), then
  // per-scope sorted by descending rate so the top hot spots lead.
  findings.sort((a, b) => {
    if (a.scope === 'population' && b.scope !== 'population') return -1;
    if (b.scope === 'population' && a.scope !== 'population') return 1;
    return b.flagRate - a.flagRate;
  });

  const capped = findings.slice(0, maxFindings);

  let recorded = 0;
  for (const finding of capped) {
    const ok = await recordLearning(opts.pool, finding);
    if (ok) recorded += 1;
  }

  const populationFindings = capped.filter((f) => f.scope === 'population')
    .length;
  const perScopeFindings = capped.length - populationFindings;

  return {
    windowDays,
    totalTurnsScanned,
    populationFindings,
    perScopeFindings,
    findingsRecorded: recorded,
    findings: capped,
  };
}

function guardrailFor(flag: string): string {
  return PERSONA_GUARDRAIL_LANGUAGE[flag] ?? GENERIC_GUARDRAIL;
}

interface FormatOpts {
  scope: FindingScope;
  keeperId: string | null;
  trialId: string | null;
  flag: string;
  flaggedTurns: number;
  totalTurns: number;
  rate: number;
  windowDays: number;
  guardrail: string;
}

function formatLearning(opts: FormatOpts): string {
  const pct = (opts.rate * 100).toFixed(1);
  const scopeLabel =
    opts.scope === 'population'
      ? 'population'
      : opts.scope === 'keeper'
        ? `keeper=${opts.keeperId ?? '?'}`
        : `trial=${opts.trialId ?? '?'}`;
  return (
    `Rail flag '${opts.flag}' fired in ${pct}% of turns ` +
    `(${opts.flaggedTurns}/${opts.totalTurns}) over the last ${opts.windowDays} ` +
    `days at scope ${scopeLabel}. Persona guardrail: ${opts.guardrail}`
  );
}

/**
 * UPSERT into agent_learnings using the table's
 * UNIQUE(source_agent, category, learning_hash) constraint. The hash
 * uses scope + flag + (keeper|trial id when present) so the same hot
 * spot updates last_applied_at on re-detection rather than ballooning
 * into duplicate rows.
 *
 * Returns true when the statement affected a row. pg reports rowCount=1
 * for both the insert branch and the DO UPDATE branch, so this cannot tell a
 * brand-new finding from a re-seen one; `findingsRecorded` therefore counts
 * upserts. An operator tells new from repeat by comparing
 * agent_learnings.created_at with last_applied_at.
 */
async function recordLearning(
  pool: Pool,
  finding: RailFlagFinding
): Promise<boolean> {
  const idPart =
    finding.scope === 'population'
      ? 'population'
      : finding.scope === 'keeper'
        ? `keeper:${finding.keeperId ?? ''}`
        : `trial:${finding.trialId ?? ''}`;
  const hash = crypto
    .createHash('sha256')
    .update(`${SOURCE_AGENT}:${LEARNING_CATEGORY}:${finding.flag}:${idPart}`)
    .digest('hex')
    .slice(0, 16);

  const res = await pool.query(
    `INSERT INTO agent_learnings (source_agent, category, learning, learning_hash)
     VALUES ($1, $2, $3, $4)
     ON CONFLICT (source_agent, category, learning_hash)
     DO UPDATE SET learning = EXCLUDED.learning, last_applied_at = NOW()`,
    [SOURCE_AGENT, LEARNING_CATEGORY, finding.learning, hash]
  );
  // pg's UPSERT reports rowCount=1 for both the insert and the update branch,
  // so rowCount cannot distinguish a brand-new finding from a re-seen one. We
  // treat any successful UPSERT as "recorded"; an operator tells new from
  // repeat by reading agent_learnings.created_at vs last_applied_at.
  return (res.rowCount ?? 0) > 0;
}

function sanitizePositiveInt(value: number | undefined, fallback: number): number {
  if (typeof value !== 'number') return fallback;
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.floor(value);
}

function sanitizeFlagRate(value: number | undefined, fallback: number): number {
  if (typeof value !== 'number') return fallback;
  if (!Number.isFinite(value) || value < 0 || value >= 1) return fallback;
  return value;
}
