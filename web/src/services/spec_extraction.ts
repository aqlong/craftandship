/**
 * v2 spec extraction from a keeper's conversation + memory.
 *
 * Phase 2 Slice 2C, see `docs/CAS_HARBOR_PHASE_2_TECH_PLAN.md` § 5.3.
 *
 * When the keeper reaches the SKETCH stage of the working arc and says
 * "yes, build it," this service walks the keeper's `companion_turns`
 * + `keeper_memory` and produces a v2 build spec JSON suitable for the
 * existing `compose/build.py` pipeline.
 *
 * Sacred-content firewall, as implemented (all in SQL, see
 * SAFE_TURNS_FILTER and the memory queries below):
 *  - memory rows: only partitions 'working' and 'voice' are read;
 *  - conversation turns: a turn is left out if it carries the 'crisis'
 *    rail flag, or if the memory classifier filed a 'sacred' row from it
 *    (`keeper_memory.source_turn_id`).
 * Limits: classification is a model judgment and runs in the background, so
 * a turn that is sacred in substance but was not (yet) classified as such is
 * still included, and the prompt asks the model not to publish such
 * material. Treat the firewall as defence in depth, not a guarantee.
 *
 * The transformation is one Opus call with a structured-output prompt.
 * Cost is ~$0.10-$0.20 per extraction (vs ~$3 for a full v1 build), so
 * even a few re-extractions per keeper stay well within unit-economics.
 *
 * The output is validated for shape (required keys present, archetype
 * is a known string, pages array non-empty, etc.) BEFORE the caller
 * persists it to the builds table or dispatches to the queue. Schema
 * shape mirrors the v1-shape spec the v2 worker `compose.intake.translate_v1_spec()`
 * already accepts; we deliberately stay in v1 shape so the existing
 * worker translator handles archetype mapping.
 *
 * NO Stripe / Resend / Railway side effects fire here. This is pure
 * memory + conversation -> JSON. Caller dispatches.
 */

import Anthropic from '@anthropic-ai/sdk';
import type { Pool } from 'pg';
import { renderMemoryLine } from '../companion/prompt_text';
import { tryConsumeDailyCall, LlmDailyBudgetError } from './llm-budget';
import { AssistantNotConfiguredError } from '../utils/assistant';

const EXTRACTION_MODEL = 'claude-opus-4-6';
const MAX_OUTPUT_TOKENS = 4096;
const RECENT_TURNS_LIMIT = 30;
const MEMORY_LIMIT = 200;

/**
 * SQL predicate (alias `ct` for companion_turns) that leaves out turns that
 * must not feed a public website: crisis-flagged turns, and turns from which
 * the memory classifier derived a sacred memory row. Requires the identity
 * column to be filtered by the caller.
 */
const SAFE_TURNS_FILTER = `
       AND NOT ('crisis' = ANY(ct.rail_flags))
       AND NOT EXISTS (
         SELECT 1 FROM keeper_memory km
         WHERE km.source_turn_id = ct.turn_id AND km.partition = 'sacred'
       )`;

// ---------------------------------------------------------------------------
// Build readiness: which turns count
// ---------------------------------------------------------------------------
//
// "Is there enough in this conversation to build from" is asked in three
// places: the conversation page (whether to offer the build button), POST
// /lighthouse/build (the readiness floor, before any model call) and the
// extraction itself (which has nothing to send when every turn was left out).
// They must count the same turns. A count of every stored turn says "ready"
// for a conversation the extraction then reads as empty: four crisis messages
// are four turns and no material. So the count below carries the filter the
// extraction's own statements carry (SAFE_TURNS_FILTER), from this one place,
// and buildReadinessOf is the one comparison against the floor.

/** Whose conversation: a trial's, or a keeper's (customers.id). */
export type ConversationIdentity = { trialId: string } | { keeperCustomerId: string };

/**
 * A scalar subquery: how many of a conversation's turns the extraction may
 * read. `ownerExpr` is SQL the caller wrote (a parameter such as `$1`, or a
 * column of an outer row), never a value from a request.
 */
export function extractableTurnCountSql(column: 'trial_id' | 'keeper_id', ownerExpr: string): string {
  return `(SELECT COUNT(*) FROM companion_turns ct
       WHERE ct.${column} = ${ownerExpr}${SAFE_TURNS_FILTER})`;
}

/** How many of this conversation's turns the extraction may read. */
export async function countExtractableTurns(
  pool: Pick<Pool, 'query'>,
  identity: ConversationIdentity
): Promise<number> {
  const [column, ownerId] =
    'trialId' in identity
      ? (['trial_id', identity.trialId] as const)
      : (['keeper_id', identity.keeperCustomerId] as const);
  const res = await pool.query<{ c: string }>(
    `SELECT ${extractableTurnCountSql(column, '$1')}::text AS c`,
    [ownerId]
  );
  return parseInt(res.rows[0]?.c ?? '0', 10) || 0;
}

/**
 * The readiness floor: how many turns the extraction can read before a build
 * may be started without an operator. Default 4; CAS_HARBOR_BUILD_MIN_TURNS
 * overrides it (a value that is not a whole number of at least 1 means 4).
 */
export const BUILD_MIN_TURNS_DEFAULT = 4;
export function getBuildMinTurns(): number {
  const raw = process.env.CAS_HARBOR_BUILD_MIN_TURNS;
  if (!raw) return BUILD_MIN_TURNS_DEFAULT;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 1) return BUILD_MIN_TURNS_DEFAULT;
  return n;
}

export interface BuildReadiness {
  ready: boolean;
  /** Turns the extraction may read; crisis-flagged and sacred-sourced turns are not among them. */
  extractableTurns: number;
  minTurns: number;
}

/** The one comparison: a conversation is build-ready when the turns the extraction may read reach the floor. */
export function buildReadinessOf(extractableTurns: number, minTurns: number = getBuildMinTurns()): BuildReadiness {
  return { ready: extractableTurns >= minTurns, extractableTurns, minTurns };
}

/** Count this conversation's extractable turns and compare them with the floor. */
export async function conversationBuildReadiness(
  pool: Pick<Pool, 'query'>,
  identity: ConversationIdentity
): Promise<BuildReadiness> {
  return buildReadinessOf(await countExtractableTurns(pool, identity));
}

// ---------------------------------------------------------------------------
// The ways an extraction fails
// ---------------------------------------------------------------------------
//
// Each has a code, so a caller can answer it as what it is and not as a
// server fault:
//   - nothing_to_extract: no turn the extraction may read and no working or
//     voice note. Decided before any model call.
//   - model_call_failed: the provider did not answer (a timeout, a refused
//     connection, an error status). The call was attempted.
//   - unusable_reply: the provider answered, and the answer is not a spec (no
//     text, not JSON, not an object with an app name and a list of pages).
//     The call was made and paid for.
// The two other deliberate refusals keep their own classes, which the chat
// routes share: AssistantNotConfiguredError (no key on this server) and
// LlmDailyBudgetError (the daily call budget is spent). Both are raised
// before any call. services/harbor_dispatch.ts (dispatchFailureResponse) maps
// all five to HTTP answers for the three routes that dispatch.
export type SpecExtractionFailure = 'nothing_to_extract' | 'model_call_failed' | 'unusable_reply';

export class SpecExtractionError extends Error {
  constructor(
    public readonly code: SpecExtractionFailure,
    message: string
  ) {
    super(message);
    this.name = 'SpecExtractionError';
  }
}

/**
 * Pricing for cost log (per million tokens, USD). Mirrors
 * companion/claude.ts but for Opus only since we only call Opus from
 * this service.
 */
const OPUS_PRICING = { input: 15, output: 75 };

let anthropicClient: Anthropic | null = null;

function getClient(): Anthropic {
  if (!anthropicClient) {
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      // The routes answer this as the chat routes do: 503 with a plain explanation.
      throw new AssistantNotConfiguredError();
    }
    anthropicClient = new Anthropic({ apiKey });
  }
  return anthropicClient;
}

/** Test-only client reset. Not for production. */
export function _resetClientForTests(): void {
  anthropicClient = null;
}

export interface ExtractSpecOpts {
  pool: Pool;
  /**
   * Identity of the conversation to extract from. Exactly one of these
   * must be set:
   * - `keeperCustomerId`: a paid keeper. Loads from `keepers`/`companion_turns`/`keeper_memory`
   *   keyed on `keeper_id = customer_id`. This is the post-Stripe path
   *   triggered by /lighthouse/build.
   * - `trialId`: an unpromoted trial. Loads from `trial_keepers`/`companion_turns`/`keeper_memory`
   *   keyed on `trial_id`. This is the Ocean-dispatched manual path
   *   triggered by /admin/harbor/:trial_id/dispatch (2026-04-28).
   */
  keeperCustomerId?: string;
  trialId?: string;
  /** Fallback CAS tier if extraction can't decide. Default 'build'. */
  fallbackTier?: 'build' | 'grow' | 'scale';
  /** Override the default model for tests. */
  model?: string;
  /** Override the Anthropic client for tests. */
  client?: Anthropic;
  /**
   * Called once, immediately before the request to the model is made. A
   * caller that reserved something for this call (the anonymous share of the
   * daily budget, in services/harbor_dispatch.ts) uses it to tell a failure
   * that came before the call, where the reservation was not used, from one
   * that came after it.
   */
  onModelCall?: () => void;
}

/**
 * v2-shape spec returned by extraction. Deliberately structural-typed
 * (Record-of-string-keys) so we don't have to mirror the full compose
 * schema in TypeScript; v2 worker's `compose.intake.translate_v1_spec()`
 * is the source-of-truth validator on the build side.
 */
export interface ExtractedSpec {
  spec_version: string;
  app_name: string;
  business_type?: string;
  archetype?: string;
  tier: 'build' | 'grow' | 'scale';
  admin_email?: string;
  pages: Array<Record<string, unknown>>;
  data_models?: Array<Record<string, unknown>>;
  user_roles?: Array<Record<string, unknown>>;
  design?: Record<string, unknown>;
  copy?: Record<string, unknown>;
  /** Free-form audit info: what extraction noticed but couldn't decide. */
  _extraction_warnings?: string[];
  // Other keys allowed; v2 translator handles the full surface.
  [key: string]: unknown;
}

export interface ExtractSpecResult {
  spec: ExtractedSpec;
  warnings: string[];
  costMicros: number;
  /** Number of memory rows + turns considered. */
  contextSize: { memoryRows: number; turns: number };
}

interface KeeperContext {
  email: string;
  memoryRows: Array<{ category: string; content: string; keeper_phrase: string | null }>;
  turns: Array<{ keeper_text: string | null; companion_text: string }>;
}

/**
 * Pull the keeper's working-partition memory + last N turns. Caller
 * passes a customers(id); we look the keeper up by that.
 *
 * Returns {} with empty arrays if the keeper has no rows yet (caller
 * decides whether to refuse or extract a minimal scaffold; see
 * `docs/CAS_HARBOR_PHASE_2_TECH_PLAN.md` § 5.3, "Empty conversation"
 * open decision).
 */
export async function loadKeeperContext(
  pool: Pool,
  keeperCustomerId: string
): Promise<KeeperContext> {
  // The keeper's email lives on customers.
  const emailRes = await pool.query<{ email: string }>(
    `SELECT email FROM customers WHERE id = $1`,
    [keeperCustomerId]
  );
  const email = emailRes.rows[0]?.email ?? '';

  // Working + voice partitions only (sacred memory is firewalled out).
  const memoryRes = await pool.query<{
    category: string;
    content: string;
    keeper_phrase: string | null;
  }>(
    `SELECT category, content, keeper_phrase
     FROM keeper_memory
     WHERE keeper_id = $1
       AND deleted_at IS NULL
       AND partition IN ('working', 'voice')
     ORDER BY updated_at DESC
     LIMIT $2`,
    [keeperCustomerId, MEMORY_LIMIT]
  );

  const turnsRes = await pool.query<{
    keeper_text: string | null;
    companion_text: string;
  }>(
    `SELECT ct.keeper_text, ct.companion_text
     FROM companion_turns ct
     WHERE ct.keeper_id = $1${SAFE_TURNS_FILTER}
     ORDER BY ct.created_at DESC
     LIMIT $2`,
    [keeperCustomerId, RECENT_TURNS_LIMIT]
  );

  return {
    email,
    memoryRows: memoryRes.rows,
    turns: turnsRes.rows.reverse(), // chronological for the prompt
  };
}

/**
 * Mirror of loadKeeperContext for the unpromoted-trial path. Used by
 * /admin/harbor/:trial_id/dispatch, Ocean's manual handoff before the
 * keeper has paid through Stripe. Reads `trial_keepers.email_captured`
 * for the email field and the same memory/turns tables keyed on
 * `trial_id` instead of `keeper_id`.
 *
 * Returns empty arrays if the trial has no rows; caller decides whether
 * to refuse or extract a minimal scaffold.
 */
export async function loadTrialContext(
  pool: Pool,
  trialId: string
): Promise<KeeperContext> {
  const emailRes = await pool.query<{ email_captured: string | null }>(
    `SELECT email_captured FROM trial_keepers WHERE trial_id = $1`,
    [trialId]
  );
  const email = emailRes.rows[0]?.email_captured ?? '';

  const memoryRes = await pool.query<{
    category: string;
    content: string;
    keeper_phrase: string | null;
  }>(
    `SELECT category, content, keeper_phrase
     FROM keeper_memory
     WHERE trial_id = $1
       AND deleted_at IS NULL
       AND partition IN ('working', 'voice')
     ORDER BY updated_at DESC
     LIMIT $2`,
    [trialId, MEMORY_LIMIT]
  );

  const turnsRes = await pool.query<{
    keeper_text: string | null;
    companion_text: string;
  }>(
    `SELECT ct.keeper_text, ct.companion_text
     FROM companion_turns ct
     WHERE ct.trial_id = $1${SAFE_TURNS_FILTER}
     ORDER BY ct.created_at DESC
     LIMIT $2`,
    [trialId, RECENT_TURNS_LIMIT]
  );

  return {
    email,
    memoryRows: memoryRes.rows,
    turns: turnsRes.rows.reverse(),
  };
}

/**
 * Build the Opus prompt that takes a keeper context and returns a
 * v2-shape spec JSON. Exposed for tests so we can verify the prompt
 * shape without a real Anthropic call.
 */
export function buildExtractionPrompt(
  context: KeeperContext,
  fallbackTier: 'build' | 'grow' | 'scale'
): string {
  const memoryBlock = context.memoryRows.length
    ? context.memoryRows.map((m) => renderMemoryLine(m)).join('\n')
    : '(no memory rows yet)';

  const turnsBlock = context.turns.length
    ? context.turns
        .map(
          (t, i) =>
            `Turn ${i + 1}:\n` +
            (t.keeper_text ? `  Keeper: ${t.keeper_text}\n` : '') +
            `  Haven: ${t.companion_text}`
        )
        .join('\n\n')
    : '(no turns yet)';

  return [
    `You are extracting a v2 build spec from a keeper's conversation`,
    `with Haven (their cocreation companion). Output JSON ONLY: a`,
    `single object matching the v2 spec shape below. Use the keeper's`,
    `exact words for hero copy where natural. If you have to guess on a`,
    `field, ADD it to "_extraction_warnings" so the harbor master can`,
    `review.`,
    ``,
    `Required top-level keys:`,
    `  spec_version: "1.0"`,
    `  app_name: a short slug (kebab-case) derived from the keeper's work`,
    `  business_type: one short noun phrase from their work`,
    `  tier: "${fallbackTier}" unless the keeper explicitly named one`,
    `  pages: array of page objects with name + slug + sections`,
    ``,
    `CRITICAL: services / offerings / sessions are LOAD-BEARING content.`,
    `For ANY service-oriented archetype (healer-wellness, coach-consultant,`,
    `therapist-counselor, salon-spa, fitness-studio), the home page MUST`,
    `include an "offerings" section enumerating EACH thing the keeper`,
    `sells. Use the section type "offerings" with content.items, where`,
    `each item is { title, description, price }. Examples:`,
    `  - Wellness practitioner mentions "1:1 Sessions $100, Group Sessions,`,
    `    Workshops": include all three as separate items.`,
    `  - Coach mentions "Intro Call free, 1:1 Coaching $150/hr,`,
    `    Group Program $300/mo": include all three.`,
    `  - Music teacher mentions "Weekly Lesson $80, Trial Lesson $40,`,
    `    Recital Prep": include all three.`,
    `Use the keeper's EXACT names + EXACT prices. Empty offerings means`,
    `the build falls back to generic archetype defaults that have NO`,
    `relationship to the keeper's actual work; that is a build-quality`,
    `failure. If the keeper named no offerings yet, emit a single`,
    `placeholder offering with title="Get in touch" and price omitted,`,
    `and add a warning to "_extraction_warnings" saying so.`,
    ``,
    `Same load-bearing rule for testimonials: if the keeper shared any`,
    `client praise / quotes / reviews, include them as a "testimonials"`,
    `section with content.items = [{ quote, author }]. NEVER invent`,
    `testimonials the keeper did not say.`,
    ``,
    `Optional but encouraged:`,
    `  archetype: one of "healer-wellness", "coach-consultant",`,
    `    "therapist-counselor", "fitness-studio", "salon-spa",`,
    `    "restaurant-cafe", "tradesperson-contractor", "event-producer",`,
    `    "author" (novelists / writers with a backlist),`,
    `    "local-service" (best fit; add a warning if uncertain)`,
    `  data_models: per-archetype fields the keeper named. CRITICAL:`,
    `    every public listing the keeper described needs a data_model so`,
    `    they can edit it from admin after launch. Common shapes:`,
    `      - Session (or Service / Offering): one row per offering the`,
    `        keeper sells (e.g. "60-minute Reiki," "Voice lesson"). Fields:`,
    `        name, duration_minutes, price_cents, description.`,
    `      - SoundBath / Workshop / GroupEvent: one row per scheduled`,
    `        date the keeper hosts a group thing. Fields: title,`,
    `        starts_at, ends_at, capacity, seats_remaining, price_cents,`,
    `        description.`,
    `      - Class: one row per class type (yoga, fitness). Fields: name,`,
    `        instructor, schedule, description, price_cents.`,
    `      - Service: tradesperson / salon services. Fields: name,`,
    `        price_cents, duration_minutes, description.`,
    `      - MenuItem: restaurant-cafe items. Fields: name, price_cents,`,
    `        section_name, description, available.`,
    `      - Treatment: spa / healer modalities offered. Fields: name,`,
    `        price_cents, duration_minutes, description.`,
    `    Default models that are always emitted (DO NOT add): BlogPost,`,
    `    Event, Photo, Testimonial, ContactInquiry, NewsletterSubscriber.`,
    `    BookingIntent is auto-emitted whenever any page has a "booking"`,
    `    section.`,
    `    For each custom data_model you emit, also seed 2-3 sample rows`,
    `    in the model.seed_rows array using the keeper's actual offerings`,
    `    (their words, their prices). Empty admin = lost keeper.`,
    `  copy: { site_name, hero_tagline, hero_subtext, about_body }`,
    `    site_name: the keeper's brand name with ORIGINAL punctuation`,
    `      preserved. If they said "Fern & Stone", emit "Fern & Stone"`,
    `      (NOT "Fern And Stone"). If they used a slash, an apostrophe,`,
    `      a period, keep them. If no brand was mentioned, OMIT site_name.`,
    `      The kebab-case slug for app_name is separate from this.`,
    `  design: {`,
    `    primary_color, mood (warm, calm, alive, etc.),`,
    `    palette_hint: one of "warm-earth", "soft-rose", "forest-emerald",`,
    `      "deep-plum", "ocean-blue", "slate-gold", "navy-clean",`,
    `      "terracotta-sage", "cream-espresso", "sage-clay", "blush-bronze"`,
    `      (only if the keeper expressed a clear color preference; OMIT otherwise),`,
    `    font_pair_hint: one of "rubik-nunito", "lora-raleway",`,
    `      "playfair-source-sans", "inter-inter", "cormorant-lato",`,
    `      "karla-karla", "lora-inter", "archivo-dm-sans",`,
    `      "instrument-serif-inter" (OMIT unless clearly stated),`,
    `    density_hint: "airy" | "standard" | "dense" (OMIT unless stated),`,
    `    hero_style_hint: "centered" | "split-image" | "editorial" | "image-led"`,
    `      (OMIT unless stated),`,
    `    mood_keywords: up to 5 lowercase one-word adjectives the keeper`,
    `      actually used about visual feel (e.g. "warm","minimal","earthy"),`,
    `    avoid_keywords: up to 5 lowercase one-word adjectives the keeper`,
    `      explicitly rejected (e.g. "corporate","clinical","tech")`,
    `  }`,
    ``,
    `Hard rules:`,
    `  - NO em dashes anywhere in the output (use colons / commas).`,
    `  - "website" not "app" / "platform" / "software" in copy fields.`,
    `  - Hero tagline ≤ 60 chars; subtext ≤ 160 chars.`,
    `  - PRIVACY: this output becomes a public website. Do not put the`,
    `    keeper's grief, trauma, health, family, faith, identity or any`,
    `    crisis disclosure into any field unless the keeper clearly asked for`,
    `    that exact wording to be public. When unsure, leave it out.`,
    `  - Output VALID JSON only. No markdown fences. No prose preamble.`,
    ``,
    `--- Keeper memory (working + voice partitions only) ---`,
    memoryBlock,
    ``,
    `--- Conversation (chronological) ---`,
    turnsBlock,
    ``,
    `Now produce the JSON spec.`,
  ].join('\n');
}

/**
 * Parse the Opus output as JSON. Tolerates a single set of triple-
 * backtick fences in case the model slips. Returns null on any parse
 * failure; caller must handle.
 */
export function parseExtractedSpec(raw: string): ExtractedSpec | null {
  let text = raw.trim();
  // Strip ```json ... ``` or ``` ... ``` fences if present.
  const fenceMatch = text.match(/^```(?:json)?\n?([\s\S]*?)\n?```$/);
  if (fenceMatch) {
    text = fenceMatch[1].trim();
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  // Light shape checks; full validation happens worker-side via
  // compose.intake.translate_v1_spec.
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.app_name !== 'string' || obj.app_name.length === 0) return null;
  if (!Array.isArray(obj.pages)) return null;
  return obj as ExtractedSpec;
}

/**
 * Light shape validation that runs after parsing. Returns the list of
 * shape warnings (non-empty when extraction is unsafe to dispatch).
 */
export function validateExtractedSpec(spec: ExtractedSpec): string[] {
  const warnings: string[] = [];
  if (!spec.spec_version) warnings.push('missing spec_version');
  if (typeof spec.app_name !== 'string' || spec.app_name.length === 0) {
    warnings.push('missing or empty app_name');
  }
  if (!Array.isArray(spec.pages) || spec.pages.length === 0) {
    warnings.push('pages must be a non-empty array');
  }
  if (
    spec.tier !== undefined &&
    !['build', 'grow', 'scale'].includes(spec.tier as string)
  ) {
    warnings.push(`unknown tier: ${spec.tier}`);
  }
  // Brand voice: em dashes in any string field are an error.
  if (containsEmDash(JSON.stringify(spec))) {
    warnings.push('output contains em dash; brand-voice violation');
  }
  return warnings;
}

function containsEmDash(text: string): boolean {
  // Split + Unicode escape so the detector does not itself contain
  // literals that would trip the brand-voice lint.
  return text.includes('\u2014') || new RegExp('&' + 'mdash;', 'i').test(text);
}

/**
 * Archetypes whose builds collapse into nonsense when offerings/services
 * are empty: the home page falls back to generic coaching defaults
 * (a stock list of sessions, mentorship and group events) that have
 * nothing to do with the keeper's actual work.
 *
 * Source: an end-to-end test dispatch (2026-05-01) where Opus left
 * _harbor_meta.services = None despite the conversation enumerating
 * three named offerings.
 */
const SERVICE_REQUIRED_ARCHETYPES = new Set([
  'healer-wellness',
  'coach-consultant',
  'therapist-counselor',
  'salon-spa',
  'fitness-studio',
]);

/**
 * Section type strings (post-normalize) that the harbor->compose
 * translator routes to the offerings/services bucket. Mirrors
 * SECTION_TYPE_ALIASES in harbor_to_compose.ts; if you add aliases there,
 * add them here too. Defensive lowercase + dash-to-underscore normalize
 * in case Opus emits "service-list" or "Sessions Overview".
 */
const OFFERINGS_SECTION_TYPES = new Set([
  'offerings',
  'services',
  'service_list',
  'classes',
  'menu',
  'treatments',
  'sessions',
  'sessions_list',
  'sessions_overview',
  'session_list',
  'session_overview',
  'pricing',
  'prices',
]);

/**
 * Data model names that count as "the keeper's offerings exist as a CRUD
 * surface." When seed_rows are populated on one of these, the build can
 * fall back on `_services_from_offering_seed_rows` in compose/intake.py
 * even if Opus didn't emit an offerings section. So we treat
 * service-shaped data_models with seed_rows as evidence the keeper named
 * their offerings.
 */
const SERVICE_DATA_MODEL_NAMES = new Set([
  'Session',
  'Service',
  'Treatment',
  'Offering',
  'Package',
  'Program',
  'Lesson',
  'Course',
  'Class',
]);

function normalizeSectionTypeForCheck(raw: unknown): string {
  if (typeof raw !== 'string') return '';
  return raw.toLowerCase().trim().replace(/-/g, '_').replace(/_(preview|teaser)$/, '');
}

/**
 * Checks the parsed spec for evidence that the keeper's actual
 * offerings are encoded somewhere the harbor->compose translator can
 * pick up. Returns true when:
 *   - any page has a section whose type maps to offerings/services AND
 *     that section's content.items is a non-empty array; OR
 *   - any data_model name is in SERVICE_DATA_MODEL_NAMES AND has a
 *     non-empty seed_rows array.
 *
 * Pure / no side effects. Exposed for tests.
 */
export function specHasOfferingsContent(spec: ExtractedSpec): boolean {
  if (Array.isArray(spec.pages)) {
    for (const page of spec.pages) {
      if (!page || typeof page !== 'object') continue;
      const sections = (page as Record<string, unknown>).sections;
      if (!Array.isArray(sections)) continue;
      for (const section of sections) {
        if (!section || typeof section !== 'object') continue;
        const sec = section as Record<string, unknown>;
        const type = normalizeSectionTypeForCheck(sec.type);
        if (!OFFERINGS_SECTION_TYPES.has(type)) continue;
        const content = sec.content;
        if (!content || typeof content !== 'object') continue;
        const items = (content as Record<string, unknown>).items
          ?? (content as Record<string, unknown>).list
          ?? (content as Record<string, unknown>).entries;
        if (Array.isArray(items) && items.length > 0) return true;
      }
    }
  }
  if (Array.isArray(spec.data_models)) {
    for (const model of spec.data_models) {
      if (!model || typeof model !== 'object') continue;
      const m = model as Record<string, unknown>;
      const name = typeof m.name === 'string' ? m.name : '';
      if (!SERVICE_DATA_MODEL_NAMES.has(name)) continue;
      const seedRows = m.seed_rows;
      if (Array.isArray(seedRows) && seedRows.length > 0) return true;
    }
  }
  return false;
}

/**
 * Resolves the spec's archetype either from top-level `archetype` or
 * `_harbor_meta.archetype_hint`. Returns the empty string when neither
 * is set. Exposed for tests.
 */
export function resolveArchetype(spec: ExtractedSpec): string {
  if (typeof spec.archetype === 'string' && spec.archetype.length > 0) {
    return spec.archetype;
  }
  const meta = spec._harbor_meta;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    const hint = (meta as Record<string, unknown>).archetype_hint;
    if (typeof hint === 'string' && hint.length > 0) return hint;
  }
  return '';
}

/**
 * Returns warnings when the spec's archetype is one that requires
 * offerings/services content but the spec emitted none. Empty array
 * otherwise. Pure / no side effects. Exposed for tests.
 */
export function validateServiceArchetypeContent(spec: ExtractedSpec): string[] {
  const archetype = resolveArchetype(spec);
  if (!SERVICE_REQUIRED_ARCHETYPES.has(archetype)) return [];
  if (specHasOfferingsContent(spec)) return [];
  return [
    `archetype "${archetype}" requires offerings/services content but spec emitted none; ` +
      `build will fall back to generic archetype defaults that do not match the keeper's actual work`,
  ];
}

/**
 * Persisted design preferences loaded from `keeper_memory` rows tagged
 * `category='design_pref'`. Populated by `POST /lighthouse/preview/choose`
 * (and any future widget that captures design hints) so the keeper's
 * explicit choice flows through to the v2 compose engine via
 * `_harbor_meta.template_family_hint` at extraction time.
 *
 * Most-recent row wins per field; older rows can fill in fields the
 * newest row didn't set (mood_keywords carried from a prior round, for
 * example, while a fresh preview pick supplies template_family_id).
 *
 * Returns null when no design_pref rows exist.
 */
export interface PersistedDesignPrefs {
  template_family_id?: string;
  mood_keywords?: string[];
  density_hint?: string;
  hero_style_hint?: string;
  palette_hint?: string;
  font_pair_hint?: string;
}

export async function loadPersistedDesignPrefs(
  pool: Pool,
  identity: { keeperCustomerId?: string; trialId?: string }
): Promise<PersistedDesignPrefs | null> {
  const { keeperCustomerId, trialId } = identity;
  if (!keeperCustomerId && !trialId) return null;
  const sql = keeperCustomerId
    ? `SELECT content FROM keeper_memory
       WHERE keeper_id = $1 AND deleted_at IS NULL
             AND category = 'design_pref'
       ORDER BY updated_at DESC
       LIMIT 10`
    : `SELECT content FROM keeper_memory
       WHERE trial_id = $1 AND deleted_at IS NULL
             AND category = 'design_pref'
       ORDER BY updated_at DESC
       LIMIT 10`;
  const id = keeperCustomerId || trialId;
  const { rows } = await pool.query<{ content: string }>(sql, [id]);
  if (rows.length === 0) return null;

  const merged: PersistedDesignPrefs = {};
  // Newest row wins per field (we iterate in DESC order; only set
  // a field when it's still undefined).
  for (const row of rows) {
    let parsed: Record<string, unknown> | null = null;
    try {
      const v = JSON.parse(row.content);
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        parsed = v as Record<string, unknown>;
      }
    } catch {
      continue;
    }
    if (!parsed) continue;
    if (!merged.template_family_id && typeof parsed.template_family_id === 'string') {
      merged.template_family_id = parsed.template_family_id;
    }
    if (!merged.mood_keywords && Array.isArray(parsed.mood_keywords)) {
      merged.mood_keywords = parsed.mood_keywords.filter(
        (x): x is string => typeof x === 'string'
      );
    }
    if (!merged.density_hint && typeof parsed.density_hint === 'string') {
      merged.density_hint = parsed.density_hint;
    }
    if (!merged.hero_style_hint && typeof parsed.hero_style_hint === 'string') {
      merged.hero_style_hint = parsed.hero_style_hint;
    }
    if (!merged.palette_hint && typeof parsed.palette_hint === 'string') {
      merged.palette_hint = parsed.palette_hint;
    }
    if (!merged.font_pair_hint && typeof parsed.font_pair_hint === 'string') {
      merged.font_pair_hint = parsed.font_pair_hint;
    }
  }
  if (Object.keys(merged).length === 0) return null;
  return merged;
}

/**
 * Entry point. Caller (lighthouse-build route) is responsible for
 * persisting + dispatching the spec via the existing build queue.
 */
export async function extractSpecFromConversation(
  opts: ExtractSpecOpts
): Promise<ExtractSpecResult> {
  // Exactly one of keeperCustomerId / trialId must be set. Reject ambiguous
  // calls explicitly so the trial-vs-keeper path is never accidentally
  // mixed (the two tables key the same conversation/memory rows on
  // different XOR foreign keys; mixing leaks the wrong context into the
  // Opus prompt).
  const hasKeeper = !!opts.keeperCustomerId;
  const hasTrial = !!opts.trialId;
  if (hasKeeper && hasTrial) {
    throw new Error(
      'extractSpecFromConversation: pass exactly one of keeperCustomerId or trialId, not both'
    );
  }
  if (!hasKeeper && !hasTrial) {
    throw new Error(
      'extractSpecFromConversation: keeperCustomerId or trialId is required'
    );
  }
  const fallbackTier = opts.fallbackTier ?? 'build';
  const context = hasKeeper
    ? await loadKeeperContext(opts.pool, opts.keeperCustomerId as string)
    : await loadTrialContext(opts.pool, opts.trialId as string);

  // The context was loaded with SAFE_TURNS_FILTER, so "no turns" here means
  // no turn the extraction may read: a conversation of crisis messages only
  // has stored turns and still ends here. The readiness floor in front of the
  // self-serve route counts with the same filter (countExtractableTurns), so
  // a visitor is told "not ready" there; an operator's dispatch and a
  // keeper's have no floor and are answered from this.
  if (context.turns.length === 0 && context.memoryRows.length === 0) {
    throw new SpecExtractionError(
      'nothing_to_extract',
      'There is nothing in this conversation yet that a website can be built from.'
    );
  }

  const prompt = buildExtractionPrompt(context, fallbackTier);
  const client = opts.client ?? getClient();
  const model = opts.model ?? EXTRACTION_MODEL;

  // Read before the model call, not after it: this is the last statement
  // that can fail, and after the call a failure here would throw away a reply
  // that was already paid for.
  const persistedDesign = await loadPersistedDesignPrefs(opts.pool, {
    keeperCustomerId: opts.keeperCustomerId,
    trialId: opts.trialId,
  });

  // One extraction is the most expensive single call a visitor can cause, and
  // the per-trial and per-address limits on POST /lighthouse/build do not add
  // up to a ceiling across many addresses. It draws on the process-wide daily
  // budget like the other request-driven calls.
  if (!tryConsumeDailyCall()) {
    throw new LlmDailyBudgetError();
  }

  // Bounded in time: the caller is an HTTP request. The SDK default (10
  // minutes, 2 retries) would hold it open for half an hour on a stalled
  // upstream; this is at most two attempts of 150 seconds. Bounded in size by
  // max_tokens. A call that fails is reported as such, with the provider's
  // own message kept for the server log only.
  opts.onModelCall?.();
  let response: Awaited<ReturnType<typeof client.messages.create>>;
  try {
    response = await client.messages.create(
      {
        model,
        max_tokens: MAX_OUTPUT_TOKENS,
        messages: [{ role: 'user', content: prompt }],
      },
      { timeout: 150_000, maxRetries: 1 }
    );
  } catch (callErr) {
    console.error(
      '[spec_extraction] the model call failed: %s',
      callErr instanceof Error ? callErr.message : String(callErr)
    );
    throw new SpecExtractionError(
      'model_call_failed',
      'The assistant did not answer, so the website could not be drafted. Nothing was built. Please try again in a few minutes.'
    );
  }

  const UNUSABLE_REPLY =
    'The assistant answered with something a website could not be built from. Nothing was built. Please try again in a few minutes.';
  const blocks = Array.isArray(response?.content) ? response.content : [];
  const textBlock = blocks.find((b) => b.type === 'text');
  if (!textBlock || textBlock.type !== 'text') {
    console.error('[spec_extraction] the model reply had no text block');
    throw new SpecExtractionError('unusable_reply', UNUSABLE_REPLY);
  }

  const parsed = parseExtractedSpec(textBlock.text);
  if (!parsed) {
    // The reply itself is conversation-derived text and is not logged.
    console.error(
      '[spec_extraction] the model reply was not a usable spec (%d characters)',
      textBlock.text.length
    );
    throw new SpecExtractionError('unusable_reply', UNUSABLE_REPLY);
  }

  // Apply fallback tier when missing; surface as warning.
  const warnings = validateExtractedSpec(parsed);
  if (!parsed.tier) {
    parsed.tier = fallbackTier;
    warnings.push(`tier defaulted to ${fallbackTier}`);
  }
  if (!parsed.admin_email && context.email) {
    parsed.admin_email = context.email;
  }

  // Service-archetype offerings check: when Opus emits a
  // service archetype but no offerings content, surface a warning so
  // admin/reviews can flag the build before keeper sees the empty
  // archetype-default home page.
  warnings.push(...validateServiceArchetypeContent(parsed));

  // Cost: input + output tokens at Opus pricing. SDK usage is total
  // input across all blocks (no caching used in this single-shot call). A
  // reply without usage figures is costed at zero, not thrown away.
  const inputTokens = Number(response.usage?.input_tokens) || 0;
  const outputTokens = Number(response.usage?.output_tokens) || 0;
  const costMicros = Math.round(
    inputTokens * OPUS_PRICING.input + outputTokens * OPUS_PRICING.output
  );

  // Merge the keeper's persisted design preferences
  // (`category='design_pref'` keeper_memory rows, read above) into
  // `_harbor_meta`. The customer's explicit choice from the
  // /lighthouse/preview picker UI MUST win over Opus's free-form
  // guess (Opus reads conversation tone, the picker is an explicit
  // click). We overwrite `template_family_hint`, `mood_keywords`,
  // etc. when persisted values are present; otherwise we leave
  // Opus's output alone. Helper: loadPersistedDesignPrefs.
  if (persistedDesign) {
    if (!parsed._harbor_meta || typeof parsed._harbor_meta !== 'object'
        || Array.isArray(parsed._harbor_meta)) {
      parsed._harbor_meta = {};
    }
    const meta = parsed._harbor_meta as Record<string, unknown>;
    if (persistedDesign.template_family_id) {
      meta.template_family_hint = persistedDesign.template_family_id;
    }
    if (persistedDesign.mood_keywords && persistedDesign.mood_keywords.length > 0) {
      meta.mood_keywords = persistedDesign.mood_keywords;
    }
    if (persistedDesign.density_hint) meta.density_hint = persistedDesign.density_hint;
    if (persistedDesign.hero_style_hint) meta.hero_style_hint = persistedDesign.hero_style_hint;
    if (persistedDesign.palette_hint) meta.palette_hint = persistedDesign.palette_hint;
    if (persistedDesign.font_pair_hint) meta.font_pair_hint = persistedDesign.font_pair_hint;
  }

  // Merge warnings from the model's own audit field, if present.
  const modelWarnings = Array.isArray(parsed._extraction_warnings)
    ? (parsed._extraction_warnings as string[])
    : [];

  return {
    spec: parsed,
    warnings: [...warnings, ...modelWarnings],
    costMicros,
    contextSize: {
      memoryRows: context.memoryRows.length,
      turns: context.turns.length,
    },
  };
}
