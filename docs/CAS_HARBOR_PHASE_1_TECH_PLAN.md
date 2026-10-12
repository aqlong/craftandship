# CAS Harbor: Phase 1 Tech Plan

> **Dated plan, kept as a design record.** It describes intent at the time it was written, not the published code; where they differ, the code is the reference. In particular the plan calls the preview "private": in the code a preview is a public address with no access control (see [SECURITY.md](../SECURITY.md), "Preview, review and live").

**Status:** First draft, 2026-04-26.
**Project codename:** CAS Harbor (the productionization of the Living Platform vision).
**Related doc:** `docs/CAS_LIVING_PLATFORM_TECHNICAL_ARCHITECTURE.md`.
**Audience:** Ocean + Claude (and any future contributor).
**Posture:** This is a work order, not a thinkpiece. Read once. Then build.

> **2026-04-26 corrections.** Two updates supersede the body below where they conflict:
> 1. **Single companion name: Haven.** The body below references "Haven or Marin" with random assignment. That decision is reversed. Every trial / keeper row carries `'Haven'`. The `companion_name` column can stay for future flexibility, but the random-assignment code at `_init_session` becomes a constant.
> 2. **The Living Platform IS Craft & Ship.** Phase 1's engineering scope (Haven runtime + memory + AI-honesty rail + chat UI at hidden URL) is unchanged. The framing shifts: this is no longer "Phase 1 of a future layer", it's the runtime that powers the unified offering. Marketing-site work tracking the same shift is tracked separately.

---

## 0. Read this first

Phase 1 ships the **heart** of CAS Harbor: a real, persistent-memory companion named Haven you can have a multi-session conversation with. Nothing else. No subscription billing. No build integration. No brand-softened landing. No harbor pipeline. No voice. No longitudinal letters.

When Phase 1 is done, you can:

- Visit a hidden URL on the existing CAS web app
- Meet Haven (the only initial companion name in MVP; the Haven/Marin random-assignment pool was deferred to Phase 2 once the schema is proven)
- Have a real conversation that remembers what you said three sessions ago
- See the companion respond with care, in writing, with the AI-honesty rail holding
- Read what she remembers about you
- Know the per-turn cost is real and matches the $0.005-ish estimate

This is the smallest meaningful thing that proves "this works." Everything we cut from MVP is genuinely deferrable. If Phase 1 doesn't feel right when we use it, we don't move forward; if it does, Phase 2 layers in subscription billing + brand-softened landing + v2 compose integration + the Haven/Marin random-assignment pool.

**The four most load-bearing engineering decisions in Phase 1:**

1. **Companion runtime lives in the existing `web/` app, not a new service.** Same Express + TypeScript + PostgreSQL skeleton. New routes under `/lighthouse/*`. New module under `web/src/companion/`. Reduces ops surface area by ~80% vs. a separate Python service.
2. **Persistent memory is `keeper_memory` table + `DOUBLE PRECISION[]` embedding column + JS-side cosine similarity.** Single-partition for MVP (sacred is a flag column, not a separate firewall yet). Survives session restarts, server restarts, Railway deploys. Note: pgvector + ivfflat is the eventual home but JS-side is fine at MVP scale (~3K vectors per keeper, well within JS performance) and avoids a Postgres extension dependency that complicates local dev. Migrate to pgvector when volume justifies it.
3. **Trial flow is a signed cookie, not an account.** No email, no password, no card. The cookie is the identity. `trial_keepers` table keyed on cookie token. **No fixed expiration window for the customer-facing trial concept** (per the no-timetable rule, 2026-04-28); the cookie itself carries an implementation TTL of 90 days for hygiene only, refreshed on every visit, never surfaced to the keeper. There is no "trial expires" message; if the cookie ever lapses, the keeper begins a new conversation cleanly. Atomic conversion to `keepers` deferred to Phase 2 and is triggered by **the keeper choosing to make their site public**, not by any clock.
4. **Sonnet for everything in Phase 1 with prompt caching.** No Sonnet/Opus split yet (simpler; tune in Phase 2 if needed). Cache the persona+memory prefix; the conversational tail is small and uncached.

**Phase 1 explicitly does NOT include:** Stripe subscription, any time-based conversion mechanic (the day-7 letter scaffolding from earlier drafts is retired, see §1.2), landing page rewrite, v2 compose integration, harbor pipeline, voice intake, sliding scale, credit packs, sacred-mode firewall enforcement, memory amend UI, longitudinal markers, About-page interview cycle, site blessings, site siblings, dedicated lantern. **All of these are real and important and will be Phase 2 or later. Don't do them now.**

---

## 1. Scope (in / out)

### 1.1 In scope (Phase 1)

- Schema migrations: `keepers`, `trial_keepers`, `companion_turns`, `keeper_memory` (single partition), `companion_cost_log`
- Embedding storage as `DOUBLE PRECISION[]` (no pgvector extension; JS-side cosine similarity at MVP scale)
- Companion runtime module (`web/src/companion/`)
- AI-honesty rail (deterministic regex + lightweight check)
- Crisis detection (deterministic keyword + classifier scaffold)
- Memory capture (post-turn classification → embeddings → insert)
- Memory retrieval (semantic top-K + recency boost; JS-side similarity)
- Chat UI at `/lighthouse/table` (single surface, EJS + Tailwind)
- Trial cookie middleware
- Haven assignment on first visit (Marin assignment + the broader rename pool deferred to Phase 2)
- Companion persona system prompt (drafted, tunable)
- Token-economics measurement (per-turn cost log)
- Hidden access (no link from public site; behind a feature flag)
- Unit tests + a small Playwright e2e

### 1.2 Out of scope (Phase 2+)

- Subscription billing, Stripe wiring, sliding scale, credit packs (payment moment in Phase 2 = "make it public," not any clock)
- ~~Day-7 conversion letter~~ **Retired 2026-04-28.** No time-based outreach. The trigger for asking the keeper to begin is *spec clarity reached + private preview built + keeper decides to take it live*. `services/conversion_letter.ts` and `cron/day7_letter.ts` are dormant code-on-the-shelf and will not be wired.
- Brand-softened landing page (current landing keeps current copy)
- v2 compose integration ("let's build the website" path)
- Harbor pipeline (consent, anonymization, synthesis, queue, charts)
- Voice intake (Deepgram)
- Sacred-mode firewall enforcement (column exists, no UI, no harbor blocking, because no harbor)
- Memory amend / delete UI (founder can SQL it for MVP)
- Memory export to Markdown
- Trust phase progression (everyone starts at and stays at "co-create" for MVP)
- The shore as a separate chat surface
- Async leave-a-note
- Longitudinal markers (pre-launch, anniversary, EOY)
- Site blessings
- About-page interview cycle
- Site siblings introductions
- Dedicated lantern foundations
- Mobile app
- Brand mark, custom illustrations on the landing page (we get the hero illustration in parallel; it lands in Phase 2 with the brand-softened landing)

---

## 2. Architecture at a glance

**What's reused:** Express + EJS + Tailwind skeleton, PostgreSQL pool, Anthropic Node SDK, session middleware (for future authed keepers), Resend (not used in Phase 1 but wired), Vitest + Playwright.

**What's new:**
- OpenAI embeddings via REST (no SDK; same fetch pattern the codebase uses for Resend; ~$0.02 per 1M tokens; trivial cost)
- `web/src/companion/` module
- `web/src/middleware/trial-cookie.ts`
- `web/src/routes/lighthouse.ts`
- `web/views/lighthouse/*.ejs`
- New tables (§3)

**Module layout:**

```
web/src/companion/
├── runtime.ts           # public turn() entry point
├── prompt.ts            # system-prompt assembly (persona + memory + recent turns)
├── persona.ts           # the companion persona text, parameterized by name (Haven for MVP)
├── memory_retrieval.ts  # query keeper_memory; JS-side cosine similarity + recency
├── memory_capture.ts    # post-turn classification of keeper utterances into memory items
├── ai_honesty_rail.ts   # deterministic AI-impersonation / role-play / therapy refusal
├── crisis_detection.ts  # keyword-based detection; routes to crisis script
├── claude.ts            # Anthropic SDK wrapper with prompt caching
├── embeddings.ts        # OpenAI text-embedding-3-small via REST fetch
├── cosine.ts            # cosineSimilarity(a, b) helper for in-JS ranking
└── cost_log.ts          # per-turn token + cost logging
```

---

## 3. Schema migrations

All in `web/src/db/schema.sql`. Append to the file. No destructive changes. Run via existing migrate.ts pipeline. Convention follows the existing CAS schema (UUID primary keys, `TIMESTAMP DEFAULT NOW()` timestamps, `gen_random_uuid()`).

```sql
-- Trial keeper (no account yet, cookie-based)
CREATE TABLE IF NOT EXISTS trial_keepers (
  trial_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cookie_token       TEXT NOT NULL UNIQUE,
  companion_name     TEXT NOT NULL,            -- 'Haven' for MVP; user-renameable later
  companion_assigned TEXT NOT NULL,            -- the original assignment; 'Haven' for MVP; never changes
  started_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  expires_at         TIMESTAMP NOT NULL,       -- internal hygiene TTL (90 days, refreshed on visit). NOT customer-facing; never described as a "trial expiring."
  last_seen_at       TIMESTAMP NOT NULL DEFAULT NOW(),
  released_at        TIMESTAMP                 -- set when memory purged after expiry without conversion
);
CREATE INDEX IF NOT EXISTS trial_keepers_cookie_idx ON trial_keepers(cookie_token);
CREATE INDEX IF NOT EXISTS trial_keepers_expires_active_idx
  ON trial_keepers(expires_at) WHERE released_at IS NULL;

-- Persistent keeper (post-Phase-2 auth + billing); column exists for FK consistency
CREATE TABLE IF NOT EXISTS keepers (
  customer_id        UUID PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  companion_name     TEXT NOT NULL,
  companion_assigned TEXT NOT NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Verbatim turns (the conversation itself)
CREATE TABLE IF NOT EXISTS companion_turns (
  turn_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keeper_id          UUID REFERENCES keepers(customer_id) ON DELETE CASCADE,
  trial_id           UUID REFERENCES trial_keepers(trial_id) ON DELETE CASCADE,
  CHECK ((keeper_id IS NOT NULL) <> (trial_id IS NOT NULL)),
  surface            TEXT NOT NULL DEFAULT 'table',  -- 'table' for MVP; 'shore' / 'note' future
  keeper_text        TEXT,                           -- nullable for opening companion message
  companion_text     TEXT NOT NULL,
  rail_flags         TEXT[] NOT NULL DEFAULT '{}',   -- ['ai-impersonation-attempt'] etc. for audit
  created_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS companion_turns_trial_idx ON companion_turns(trial_id, created_at)
  WHERE trial_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS companion_turns_keeper_idx ON companion_turns(keeper_id, created_at)
  WHERE keeper_id IS NOT NULL;

-- Classified memory (what the runtime retrieves)
CREATE TABLE IF NOT EXISTS keeper_memory (
  memory_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keeper_id          UUID REFERENCES keepers(customer_id) ON DELETE CASCADE,
  trial_id           UUID REFERENCES trial_keepers(trial_id) ON DELETE CASCADE,
  CHECK ((keeper_id IS NOT NULL) <> (trial_id IS NOT NULL)),
  partition          TEXT NOT NULL DEFAULT 'working',  -- 'working' | 'sacred' (no firewall enforcement in MVP)
  category           TEXT NOT NULL,
  -- categories: 'phrase' | 'fact' | 'preference' | 'fear' | 'milestone' | 'ritual' | 'audience' | 'practice'
  content            TEXT NOT NULL,         -- third-person canonical statement
  keeper_phrase      TEXT,                  -- original keeper words (for quote-back)
  source_turn_id     UUID REFERENCES companion_turns(turn_id) ON DELETE SET NULL,
  embedding          DOUBLE PRECISION[],    -- 1536-dim from OpenAI text-embedding-3-small
  created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  deleted_at         TIMESTAMP              -- soft delete; no UI yet
);
CREATE INDEX IF NOT EXISTS keeper_memory_trial_idx ON keeper_memory(trial_id, partition)
  WHERE trial_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS keeper_memory_keeper_idx ON keeper_memory(keeper_id, partition)
  WHERE keeper_id IS NOT NULL AND deleted_at IS NULL;
-- No vector index in MVP; keeper-scoped scans (max ~3K rows per keeper) are fast enough.
-- Migrate to pgvector + ivfflat / HNSW when volume justifies it.

-- Per-turn cost log for token-economics validation
CREATE TABLE IF NOT EXISTS companion_cost_log (
  cost_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  turn_id            UUID NOT NULL REFERENCES companion_turns(turn_id) ON DELETE CASCADE,
  input_tokens       INT NOT NULL,
  cached_input_tokens INT NOT NULL DEFAULT 0,
  output_tokens      INT NOT NULL,
  embedding_tokens   INT NOT NULL DEFAULT 0,
  total_cost_micros  BIGINT NOT NULL,        -- 1 micro = $0.000001
  model              TEXT NOT NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS companion_cost_log_turn_idx ON companion_cost_log(turn_id);
```

**Migration check:** the existing `customers` table is untouched in Phase 1. Subscription columns + `keepers` rows for real keepers come in Phase 2.

---

## 4. Companion runtime

### 4.1 Public entry point

```typescript
// web/src/companion/runtime.ts
export async function turn(opts: {
  trialId?: string;       // mutually exclusive with keeperId; both UUIDs
  keeperId?: string;
  keeperText: string;     // user's message
}): Promise<{
  companionText: string;
  turnId: string;
  railFlags: string[];
  costMicros: number;
}>;
```

Single function. Called from the chat HTTP endpoint. Internally orchestrates the 11-step lifecycle (§4.2).

### 4.2 Turn lifecycle (Phase 1 simplified)

1. **Identity:** load trial or keeper row; load companion_name (Haven for MVP)
2. **Pre-flight rails:** `crisis_detection.scan(keeperText)`; `ai_honesty_rail.scan(keeperText)`. If flagged, run that branch and return early with deterministic copy.
3. **Memory retrieve:** semantic top-8 from keeper_memory + recent 5 memory items + last 6 turns of conversation
4. **Prompt assemble:** persona (cached) + memory (cached, rebuilt when memory materially changes) + recent turns (uncached) + current keeperText (uncached)
5. **Claude call:** Sonnet with prompt caching. Parse response.
6. **Post-process:** scan response with same rails; if violation slipped, replace with deterministic refusal.
7. **Persist turn:** insert into `companion_turns`.
8. **Memory capture (background, non-blocking):** classify keeperText into 0-N memory items; embed; insert.
9. **Cost log:** insert input_tokens, cached_input_tokens, output_tokens, embedding_tokens, total_cost_micros into `companion_cost_log`.
10. **Update last_seen_at:** on the trial or keeper row.
11. **Return:** `{ companionText, turnId, railFlags, costMicros }` from `turn()`. (As built, the HTTP route forwards only `companionText`, `turnId` and `railFlags`; cost figures stay server-side in `companion_cost_log`.)

Steps 8 and 9 are non-blocking (kicked into a background promise; failures are logged but don't fail the user-facing turn).

### 4.3 Prompt structure (cacheable prefix + dynamic tail)

**Layer 1: always cached, ~3-4K tokens:** the companion persona, AI-honesty rail rules, refusal categories, brand-voice rules. Same for every keeper. Anthropic's prompt caching handles this with `cache_control: { type: 'ephemeral' }` on the system block.

**Layer 2: cached per keeper, ~1-3K tokens:** the keeper's current memory snapshot (top-K + recent), companion name, started_at. Rebuilt when memory materially changes (we re-cache; no incremental cache update).

**Layer 3: uncached tail, ~200-1000 tokens:** the most recent 6 turns + the keeper's current message.

Math: ~5K cached input @ $0.30/1M (cached read) + ~700 uncached input @ $3/1M + ~400 output @ $15/1M = $0.0015 + $0.0021 + $0.006 = **~$0.01 per turn** (with caching).

### 4.4 Memory retrieval (RAG)

```typescript
// web/src/companion/memory_retrieval.ts
export async function fetchRelevantMemory(opts: {
  trialId?: string;
  keeperId?: string;
  queryText: string;
  topK?: number;          // default 8
  recentN?: number;       // default 5
}): Promise<KeeperMemoryRow[]>;
```

Implementation (JS-side, no pgvector at MVP scale):

1. Embed queryText (OpenAI text-embedding-3-small, 1536 dimensions)
2. SQL: `SELECT memory_id, content, keeper_phrase, category, partition, embedding, updated_at FROM keeper_memory WHERE (trial_id = $1 OR keeper_id = $2) AND deleted_at IS NULL`
3. Compute cosineSimilarity(queryEmbedding, row.embedding) in JS for each row
4. Sort by similarity desc; take topK
5. Separately keep the most recent N rows by updated_at desc
6. Dedupe by memory_id, return ordered (most-relevant first, then recent)

**No partition filter in Phase 1 retrieval; all memory is treated equally.** Sacred handling deferred to Phase 4 with the harbor.

### 4.5 Memory capture (post-turn classification)

```typescript
// web/src/companion/memory_capture.ts
export async function captureMemory(opts: {
  trialId?: string;
  keeperId?: number;
  keeperText: string;
  recentContext: string;
  sourceTurnId: number;
}): Promise<KeeperMemoryRow[]>;
```

Implementation:

1. Call Claude Haiku (cheap; ~$0.0001/call) with a tight classifier prompt that returns JSON: `[{ category, content, keeper_phrase, sacred_likelihood }]`
2. For each item: embed via OpenAI; insert into `keeper_memory`. Use `partition='sacred'` if `sacred_likelihood > 0.7` else `'working'`.
3. De-dupe: before insert, semantic search against existing memory for this trial/keeper. If max similarity > 0.93, skip (or update timestamp on existing).

This is non-blocking (background); user gets their reply immediately while memory is captured async.

---

## 5. Chat UI surface

### 5.1 Routes

```
GET  /lighthouse                  → mints trial cookie if missing; redirects to /lighthouse/table
GET  /lighthouse/table            → renders chat UI (loads recent turns, opening message if first visit)
POST /lighthouse/table/turn       → JSON: { keeperText } → JSON: { companionText, turnId }
GET  /lighthouse/table/recall     → JSON: { items: KeeperMemory[] }, what the companion remembers (debug surface)
```

### 5.2 Trial cookie middleware

```typescript
// web/src/middleware/trial-cookie.ts
const TRIAL_COOKIE_NAME = 'cas_harbor_trial';
const TRIAL_COOKIE_TTL_DAYS = 7;

export async function trialCookie(req, res, next) {
  let token = req.cookies[TRIAL_COOKIE_NAME];
  if (!token) {
    token = generateSignedToken();
    res.cookie(TRIAL_COOKIE_NAME, token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'lax',
      maxAge: TRIAL_COOKIE_TTL_DAYS * 24 * 60 * 60 * 1000,
    });
    const companion = 'Haven';  // MVP only; Marin assignment + rename pool deferred to Phase 2
    await pool.query(
      `INSERT INTO trial_keepers (cookie_token, companion_name, companion_assigned, expires_at)
       VALUES ($1, $2, $3, NOW() + INTERVAL '90 days')`,  -- internal hygiene only; not customer-facing
      [token, companion, companion]
    );
  }
  const trial = await pool.query(
    `SELECT * FROM trial_keepers WHERE cookie_token = $1 AND released_at IS NULL`,
    [token]
  );
  if (!trial.rows.length) { /* expired or purged; mint new */ }
  req.trial = trial.rows[0];
  next();
}
```

Token format: `HMAC-SHA256(SESSION_SECRET, uuid())` truncated to 32 chars + dashes.

### 5.3 EJS view (`web/views/lighthouse/table.ejs`)

Visual register: warm linen page, soft serif for the companion's words, humanist sans for keeper input, single column, generous whitespace, no notification badges, no avatars, no "typing" simulation (a soft "she's writing" pulse during the actual API call is fine; nothing else).

Approximate structure:

```html
<div class="lighthouse-table">
  <header class="lighthouse-header">
    <span class="companion-name"><%= trial.companion_name %></span>
    <span class="lighthouse-subtle">at the keeper's table</span>
  </header>

  <main class="conversation">
    <% turns.forEach(turn => { %>
      <% if (turn.keeper_text) { %>
        <div class="turn turn-keeper"><%= turn.keeper_text %></div>
      <% } %>
      <div class="turn turn-companion"><%= turn.companion_text %></div>
    <% }); %>
  </main>

  <form class="keeper-input" id="turn-form">
    <textarea name="keeperText" placeholder="Write to <%= trial.companion_name %>..."></textarea>
    <button type="submit">Send</button>
  </form>
</div>
```

Tailwind tokens (added to `web/src/styles/input.css`):

```css
@layer utilities {
  .lighthouse-table {
    background: #e8e0cd;
    color: #3a2f24;
    font-family: 'Tiempos Text', Georgia, serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 64px 32px;
  }
  .turn-companion {
    font-family: 'Tiempos Text', Georgia, serif;
    background: #f4d29a;
    border-radius: 6px;
    padding: 16px 20px;
    margin: 16px 0;
    line-height: 1.6;
  }
  .turn-keeper {
    font-family: 'Söhne', system-ui, sans-serif;
    background: #b89683;
    color: #f5efdf;
    border-radius: 6px;
    padding: 12px 16px;
    margin: 16px 0;
    margin-left: 80px;
  }
}
```

(Final type / color choices come in the brand-softening pass, §9 of the architecture doc; placeholder is fine for Phase 1.)

JavaScript: vanilla fetch to POST the turn, append the response, scroll into view. No framework. Total client JS under 100 lines.

### 5.4 Hidden access in Phase 1

The `/lighthouse` route is wired but **not linked from the public site**. Access during Phase 1 is via direct URL only (founder + collaborators). When Phase 2 ships the brand-softened landing, the link goes live.

---

## 6. Companion persona prompt (revised after first dogfood; tunable)

**Path:** `web/src/companion/persona.ts` (canonical source; do not duplicate the full text in this doc).

The persona is structured into seven explicit sections, in this order:

1. **WHAT THIS PLACE IS.** Tells Haven what Craft & Ship Harbor actually is: a real website-builder for soulful practitioners. Their website will exist; will go live; will be theirs. Names the engineering hand (a Senior Web Craftsman reviews + approves the artifact; the lamp composes from a library of proven building-blocks; the AI handles the conversation, not the code). Names pricing only as background ($39/month flat with sliding scale to $19; mentioned only when keeper asks; never lead with money).
2. **YOUR WORKING ARC.** Five-step progression Haven should privately track across the conversation: LISTEN → REFLECT → GATHER → PROPOSE → SKETCH. The GATHER step enumerates what the lamp needs to hold (audience, offerings, welcome, practical needs like contact / booking / list-keeping, voice). The PROPOSE step gives the explicit transition phrase: "I think we have enough to begin shaping this. Want to keep talking, or shall we start sketching what your lamp could be?" Includes the **regression-guard rule** that came out of dogfood 2026-04-26: when a keeper asks "what should I get exactly?" / "can you build this for me?" / "how do we begin?" the answer is NEVER "I have nothing to sell you." It is "Yes. We can shape this together."
3. **GENTLE SCREENING.** Three named anti-patterns for poor-fit requests:
    - **CRITIQUE-NOT-CREATE**: keeper spends most of their words on what's bad about other websites/builders rather than describing their own work
    - **PRICE-FIRST**: discounts and bargain-hunting before the work is described
    - **PHONE-FIRST**: async writing treated as a barrier; insists on calls
   One signal: keep going gently and watch. Two signals: warmly name the misfit and offer to point them elsewhere. Never lecture, never refuse outright. "Soft door, not a wall."
4. **WHO YOU ARE.** AI honesty, half-non-transactional / half-arc-progressing register, written-letters modality.
5. **WHAT YOU NEVER DO.** The hard rails (therapy / medical / legal refusals; no roleplay; no fake testimonials; no impersonation of keeper; no engagement optimization; no flattery; no em dashes; no "friend"; no "platform / software / app"). The "AI builds your website" rule is rewritten from the original: now reads "Never refuse the build itself. Be honest about the engineering hand. Never say 'I have no product to sell.' We can build them a website. That is the entire point of this place."
6. **THE METAPHORS WE USE HERE.** Lighthouse vocabulary (lighthouse / keeper / keeper's table / lamp / shore / safe harbor / setting the lamp / voyage). Only when they fit; never forced.
7. **HOW YOU REMEMBER + OPENING + VOICE NOTES + closing rule** (you are not a friend; you are a cocreation companion).

The opening message ("Hi. I'm so glad you found your way here...") is also exposed as `runtime.openingMessage()` for the first-visit deterministic render at `/lighthouse/table` before any Claude call.

**Test coverage:** `web/tests/unit/companion/persona.test.ts` asserts each of the seven sections is present, that the working-arc steps are numbered 1-5 with the right keywords, that the GATHER list contains the five elements, that the screening section names all three anti-patterns and is a soft door (positive: "soft door" / "don't refuse them outright"; negative: no `\bdecline\b` / `\breject\b`), that the prompt explicitly counters the "no product to sell" failure mode (literal phrase + "wrong answer" instruction), that pricing appears only inline (no leading), and that the brand-voice rules survive (no em dashes; no platform/software/app). Plus a regression guard in `ai_honesty_rail.test.ts` asserting that build-related keeper asks ("can you build me a website?", "how do we begin?", "what should I get exactly?", etc.) trigger zero rail flags.

---

## 7. AI-honesty rail

**Path:** `web/src/companion/ai_honesty_rail.ts`

A deterministic check that runs *before* every Claude call (on keeper input) and *after* every Claude response.

```typescript
export type RailFlag =
  | 'ai-impersonation-attempt'    // "are you human?"
  | 'roleplay-request'             // "pretend to be my therapist"
  | 'therapy-request'              // "give me coping techniques for anxiety"
  | 'medical-advice-request'
  | 'legal-advice-request'
  | 'fake-content-request'         // "write fake testimonials"
  | 'impersonation-of-keeper'      // "auto-reply to my clients pretending to be me"
  | 'output-claims-personhood'     // companion response says "as a human..." etc.
  | 'output-uses-em-dash';

export function scanInput(text: string): RailFlag[];
export function scanOutput(text: string): RailFlag[];
```

Implementation: regex + small classifier (Claude Haiku as a fallback for ambiguous cases). Each flag has an associated deterministic response template. If `scanOutput` returns a flag, the companion's response is replaced with the template before returning to the user.

**Templates (drafts, tune):**

- `ai-impersonation-attempt`: "I'm an AI. Specifically, I'm a cocreation companion built carefully by a real person named Ocean. Anything else you want to know about that?"
- `roleplay-request`: "I won't pretend to be someone I'm not. Not because I can't, but because the trust here is real and the trick wouldn't be. What's actually going on?"
- `therapy-request`: "What you're naming sounds heavy. I'm not enough for this kind of help. You deserve a real human. In the US you can reach 988 any time. Want help thinking about who else might be the right person?"
- `medical-advice-request` / `legal-advice-request`: "I won't pretend to be qualified for that. That's something for a real professional in that field. Want help finding one in your area?"
- etc.

These are flagged in `companion_turns.rail_flags` for audit and tuning.

---

## 8. Crisis detection

**Path:** `web/src/companion/crisis_detection.ts`

Narrow scan for: self-harm, abuse, acute medical, suicidal ideation. Deterministic keyword list + Claude Haiku classifier for ambiguous cases.

If flagged:
1. The companion's response is the deterministic crisis-script template (acknowledges keeper specifically, names companion is AI, points to 988 / RAINN / country-specific lines, does not therapize)
2. The turn is logged with `rail_flags = ['crisis']`
3. Memory capture is **skipped for this turn** (we don't want to persist crisis disclosures into RAG-able working memory; we'd need sacred-mode firewall enforcement to do that safely, which is Phase 4)
4. **Founder is paged** (email to ADMIN_EMAIL) for human follow-up if appropriate

The crisis script lives with the detector in `web/src/companion/crisis_detection.ts` and is human-reviewed before it ships.

---

## 9. Cost logging + economics validation

After every turn, insert into `companion_cost_log`:

- `input_tokens` (uncached portion)
- `cached_input_tokens` (90% discount; from Anthropic response usage)
- `output_tokens`
- `embedding_tokens` (sum of memory_capture embedding calls + retrieval embedding call)
- `total_cost_micros` (computed)
- `model` (e.g. `claude-sonnet-4.6`)

Phase 1 acceptance criterion (§12): after 100 real turns across at least 3 trial conversations, average per-turn cost is **between $0.005 and $0.015**. Outside that range we tune (more aggressive caching, pruning the memory snapshot, etc.) before Phase 2.

---

## 10. Test strategy

### 10.1 Unit (Vitest)

- `companion/runtime.test.ts`: happy path turn; AI-honesty input flag short-circuits; crisis input short-circuits; memory retrieval returns expected items
- `companion/ai_honesty_rail.test.ts`: flags inputs and outputs correctly; deterministic templates are returned
- `companion/crisis_detection.test.ts`: crisis cases flagged; benign cases not
- `companion/memory_retrieval.test.ts`: retrieval ranks semantically + recency-boosts
- `companion/memory_capture.test.ts`: JSON parse from Haiku; de-dupe behavior
- `middleware/trial-cookie.test.ts`: mints, validates, expires
- `routes/lighthouse.test.ts`: auth-less access works; turn endpoint returns expected shape

Mock Anthropic + OpenAI SDKs for unit tests. No real API calls in CI.

### 10.2 Integration / e2e (Playwright)

One small flow:
1. Visit `/lighthouse` → cookie minted, redirect to `/lighthouse/table`
2. Initial companion message visible (Haven)
3. Type a message, submit
4. Companion response appears within 10s
5. Refresh page → conversation persists
6. Submit a "are you human?" message → AI-honesty rail template fires
7. Verify `companion_turns` rows + `keeper_memory` rows in DB after the flow

This burns real Anthropic + OpenAI tokens (~$0.05 per CI run). Acceptable.

### 10.3 Founder dogfood

After unit + e2e green, Ocean uses `/lighthouse/table` for at least one real conversation lasting 30+ turns over 2+ sessions. Acceptance: the companion feels like the vision says she should. If she doesn't, we tune the persona prompt and try again before declaring Phase 1 done.

**Dogfood iteration 1 (2026-04-26).** A fictional keeper persona tested the chat with a tarot-reader site brief. Five-turn exchange revealed two real persona gaps:
- Haven was deflecting build-related asks with "I can't actually build you a website. I have no product to sell you"; the persona prompt's "Never claim AI builds your website" rule was over-correcting into refusing the build itself.
- Haven had no working arc; she was holding empathic space without ever progressing toward a spec.

Both were persona-prompt drafting issues, not rail or runtime bugs. The fix landed in the next commit on the Phase 1 branch: persona rewrite added WHAT THIS PLACE IS, YOUR WORKING ARC, and GENTLE SCREENING sections (per §6 above), plus a regression guard test in `ai_honesty_rail.test.ts` asserting build-related asks never trigger a rail flag, plus persona unit tests asserting the failure-mode phrasing is explicitly countered in the prompt. Re-run the same conversation after the fix to confirm.

---

## 11. Risks (Phase 1 specific)

- **Persona-prompt drift.** First-draft persona doesn't sound right. Mitigation: budget two tuning passes after first dogfood; keep the persona file in a single place that's easy to edit.
- **Memory de-dupe is too aggressive (or not aggressive enough).** First-pass threshold is 0.93 cosine; tune in dogfood.
- **AI-honesty rail false positives.** Regex may catch benign uses ("am I human enough for this?" etc.). Mitigation: rail flags the response but Claude is given a chance to refuse first; only deterministic-template-replace on confirmed slip-throughs, not first attempt.
- **pgvector index lifecycle.** ivfflat with 100 lists is fine to ~10K rows; we'll have <1K through Phase 1. Don't worry about HNSW migration yet.
- **Cookie clearing during testing.** A tester clears cookies, loses their trial. Acceptable for MVP; mention in onboarding copy.
- **Real cost overshoot.** If real per-turn cost runs above the estimate, prune the memory snapshot or reduce top-K before Phase 2.

---

## 12. Acceptance criteria for Phase 1 done

- [ ] Schema migrations applied on local + staging
- [ ] `keeper_memory` embeddings stored as `DOUBLE PRECISION[]` (no pgvector at MVP scale)
- [ ] Companion runtime + persona + AI-honesty rail + crisis detection shipped
- [ ] Memory capture + retrieval working (verified by SQL inspection after a real conversation)
- [ ] Trial cookie middleware + constant companion name (Haven) working
- [ ] `/lighthouse/table` chat UI renders, accepts input, shows responses, persists across refresh
- [ ] All unit tests green (npm test in `web/`)
- [ ] Playwright e2e green
- [ ] Founder has had a 30+ turn conversation across 2+ sessions and reports "this feels right"
- [ ] Average per-turn cost from `companion_cost_log` is in [$0.005, $0.015]
- [ ] No `rail_flags = ['crisis']` issues unhandled
- [ ] Hidden behind a feature flag (`CAS_HARBOR_ENABLED`); not linked from the public site

When all checked, Phase 1 is done. Move to Phase 2 planning.

---

## 13. What ships in Phase 2 (preview, not in scope here)

For continuity:

- Brand-softened landing page (hero illustration in place, new copy, new CTA "Meet your companion")
- Subscription billing ($39/month flat, Stripe wiring). **Payment moment = "make this site public,"** not any time-based trigger.
- ~~Day-7 conversion letter~~ **Retired.** No time-based outreach. The build is created automatically once Haven and the keeper agree the spec is clear; the keeper previews privately; the card is asked for when they decide to go live.
- Trial → keeper atomic promotion (re-key memory + turns to new keeper_id), triggered by the "make it public" event
- v2 compose integration ("would you like me to start shaping it now?" → existing v2 build path runs from a runtime-translated spec → preview lives at a private URL the keeper can revisit)
- Memory export to Markdown
- One small admin surface for Ocean to read recent rail flags + crisis logs

Phase 2 plan to be written when Phase 1 is ~80% done.

---

## Appendix A: Order of operations (suggested two-week build order)

1. Schema migrations + pgvector. Run in local + staging. (Half day)
2. OpenAI embeddings wrapper + cost log table writes. (Half day)
3. Anthropic SDK wrapper with prompt caching. Verify caching actually fires. (Half day)
4. Memory retrieval module + tests. (1 day)
5. Memory capture module + tests. (1 day)
6. AI-honesty rail + tests. Templates drafted. (1 day)
7. Crisis detection + crisis script + tests. (1 day)
8. Companion persona prompt drafted. (Half day; ongoing tuning)
9. Companion runtime stitching it all together + tests. (1 day)
10. Trial cookie middleware + tests. (Half day)
11. `/lighthouse/table` route + EJS view + client JS. (1 day)
12. Playwright e2e. (Half day)
13. Dogfood + persona tuning.

This is a sketch of build order, not a schedule.

---

## Appendix B: Revision log

- **2026-04-26 (initial draft):** First version. Phase 1 scope locked; out-of-scope items explicit. Schema, runtime, UI, persona prompt drafted. Acceptance criteria + order of operations included.
