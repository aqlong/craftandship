# Craft & Ship Living Platform: Technical Architecture

> Historical document from April 2026; the system is no longer developed and the site was taken offline in October 2026.

> **Built differently from this draft:** the Harbor price literals ended up as `harborSubscriptionPrice` / `harborSlidingScalePrice` in `web/src/app.ts` and the `HARBOR_SUBSCRIPTION_*` constants in `web/src/services/stripe.ts`; the proposed web/src/types/subscription.ts module was never created, and the companion tests are in `web/tests/unit/companion/`. Memory does not use pgvector: `keeper_memory.embedding` is a `DOUBLE PRECISION[]` column and cosine similarity runs in JavaScript (see the comment above `keeper_memory` in `web/src/db/schema.sql`). Nothing in the schema or code implements the Harbor pipeline tables of 2.4 and section 5 (`harbor_contributions`, `harbor_patterns`, `harbor_charts`), the longitudinal markers of 2.5 and 8.4 (`keeper_markers`), the trust phases of 3.6 (`trust_phase`), voice intake (7.4 and section 11) or the soft-cap counters of 10.3.

> **2026-04-28 PIVOT NOTE (read before everything else; supersedes content below where in conflict):** the "7-day trial" / "day-7 conversion letter" / any time-based mechanic is **retired**. The keeper journey is now: open conversation with Haven (no clock) → Haven and keeper agree the spec is clear → CAS Harbor builds the site privately (preview-only URL) → keeper walks around in it at their own pace, can iterate with Haven → **the payment moment is "make it public,"** at which point the card is collected, the trial cookie identity is promoted to a `keepers` row, the build flips from `preview` to `live`, and the site is indexed. Cookie TTL becomes 90 days for hygiene only, refreshed on every visit, and is never named in customer copy. See `docs/CAS_HARBOR_PHASE_2_TECH_PLAN.md` top-of-file pivot for the canonical statement; sections 6.1–6.3 of this doc still describe useful schema and promotion mechanics but should be read with the new trigger in mind.

**Status:** Working draft, 2026-04-26 (pivot 2026-04-28). Design reference for the Harbor / Living Platform work.
**Audience:** Ocean + Claude (and any future engineer or contractor working on this).
**Posture:** Ground truth for the build. Decisions are inked unless flagged "open." Concrete enough to start work.

---

## 0. Read this first

The pivot is large but clean: the schema was pre-launch, so schema, brand, pricing, and intake could be rewritten without migration debt. This document is the build map. It does not re-argue product decisions; it answers "how do we actually build it."

**The four most load-bearing technical decisions in this doc:**

1. **Memory is PostgreSQL + pgvector, not a separate vector DB.** The keeper-companion memory schema lives in the same database as everything else, with embeddings stored in `vector` columns and similarity search via pgvector indexes. This keeps ops simple, survives Railway-style restarts, and means the harbor master review queue can be a normal admin view.
2. **Companion runtime is Claude (Sonnet for daily, Opus for wedge moments) with aggressive prompt caching.** Per-keeper system prompt is large and stable; conversational tail is small. Caching the first ~5-10K tokens of each call brings per-turn cost into the ~$0.005 range.
3. **The companion is a stateful character, not a tool.** Every turn the runtime: loads the keeper's persona context, retrieves relevant memory (RAG over their own corpus, scoped by sacred-flag), runs the trust-phase gating, calls Claude with the AI-honesty rail in the system prompt, persists the new turn back to memory with classification flags. The AI-honesty rail and the crisis-detection layer are deterministic (not LLM-judged) where possible.
4. **The "just-talking" conversation happens on a session cookie before any account exists.** No email, no card, no clock. The cookie is the identity. When Haven and the keeper agree the spec is clear, CAS Harbor builds the site to a private preview URL the keeper can visit without paying. **The conversion moment is the keeper choosing to make the site public** (= pay). At that moment, an account is created (email captured then), the cookie identity is atomically promoted to a `keepers` row, and the build flips from `preview` to `live`. The keeper's accumulated memory and their walkable preview are what they're asked to "make permanent" by paying. There is no day-counting, no trial expiry message.

**Build sequencing summary** (Section 12 has the detail): Phase 0 wires the companion runtime + memory + trial cookie surface (the heart). Phase 1 brand-softens the marketing site so the door matches the room. Phase 2 integrates v2 compose as a moment inside the companion relationship. Phase 3 ships the harbor pipeline. Phase 4 ships the longitudinal letters + voice intake + dedicated lantern foundations. Each phase is shippable independently.

---

## 1. System overview

### 1.1 What's reused from CAS today

- **PostgreSQL** (Railway-managed) as primary store
- **Express + EJS web app skeleton** (`web/`) as the customer-facing site
- **Tailwind v3.4** + the existing Vitest + Playwright test infra
- **Stripe** integration (currently one-time Build payments; we extend to subscription)
- **Resend** for transactional email + brand-voiced letters
- **Magic-link auth** + session-store contract (`connect-pg-simple`)
- **v2 static-compose engine** (`compose/`) for the actual website artifact, unchanged in shape
- **Senior engineer review gate** (now pre-set-the-lamp ritual rather than pre-deploy gate)
- **Worker queue** (`queue/`) for async jobs (harbor synthesis, longitudinal letters, retention purges)
- **Sentry, BetterStack, ops API** for observability and uptime

### 1.2 What's new

- **Companion runtime** module (`companion/`) with prompt assembly, Claude invocation, AI-honesty rail, crisis detection, trust-phase gating
- **Memory subsystem** (`memory/`) with three-tier partitioning, pgvector RAG, retention worker, export, sacred firewall
- **Companion chat UI** (`/lighthouse`, `/lighthouse/table`, `/lighthouse/shore`) replacing the current intake-only chat
- **Just-talking flow** with anon session cookie before account creation; **no day-7 letter and no time-based trigger** (retired 2026-04-28); the keeper journey converts at the "make it public" moment, not on a clock
- **Subscription billing** ($39/month flat with sliding-scale-to-$19 attestation, optional credit packs)
- **Harbor pipeline** (`harbor/`) with consent, anonymization, contribution log, harbor master review queue, approved-pattern surface
- **Voice intake** (Deepgram Nova-3 STT) on the companion chat surface; no TTS
- **Longitudinal letter cron** (pre-launch ritual, anniversary markers, end-of-year letter)
- **Brand-softened marketing surface** (landing, pricing, how-it-works, contact, feature pages)
- **New BRAND_VOICE.md v2** with the lighthouse-and-safe-harbor lexicon

### 1.3 What's retired or repositioned

- **The transactional intake flow** (`/try` → spec → build) is repositioned. The intake chat becomes one moment inside a longer relationship: when the companion says "would you like to begin shaping the lamp now?" the keeper says yes and the existing intake/spec/compose path runs, but it's bracketed by the relationship rather than being the front door.
- **The Build / Grow / Scale tier pricing** is retired. Pricing is $39/month flat, sliding to $19 on attestation. Existing constants in `web/src/types/index.ts` are deprecated; new constants live in a new `subscription.ts` module.
- **The "AI builds your website" framing** is retired across all customer-facing copy. New framing: "your cocreation companion (Haven) helps you shape and tend your lighthouse."
- **The hero CTA "Start your build"** is retired in favor of "Meet your companion" (or similar; final copy in brand-softening pass).
- **Per-build paywalls** are retired entirely; v2 compose makes builds near-zero marginal cost; rebuilds are unlimited under the subscription.

---

## 2. Data model

All new tables live in `web/src/db/schema.sql`. We follow the existing parameterized-SQL-no-ORM convention. Migrations are additive; no destructive DDL on existing tables.

### 2.1 Keepers (extends `customers`)

We keep the existing `customers` table for the auth/billing facts and add a `keepers` table 1:1 with it for the relationship facts. This separation lets us evolve the keeper layer without touching auth invariants.

```sql
CREATE TABLE IF NOT EXISTS keepers (
  customer_id        BIGINT PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  companion_name     TEXT NOT NULL,           -- 'Haven' or 'Marin' on creation; user-renameable
  companion_assigned TEXT NOT NULL,           -- the original assignment ('Haven' or 'Marin'); never changes
  trust_phase        SMALLINT NOT NULL DEFAULT 0,  -- 0=listen, 1=light suggestions, 2=co-create, 3=full presence
  trust_phase_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  consent_collective BOOLEAN NOT NULL DEFAULT FALSE,  -- opt-in to harbor contribution
  consent_introductions BOOLEAN NOT NULL DEFAULT FALSE,  -- opt-in to site-sibling intros
  retention_window_days INT NOT NULL DEFAULT 90,  -- working memory; sacred has its own field
  long_silence_at    TIMESTAMPTZ,              -- last meaningful turn; used by silence-acknowledgment
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

For the just-talking phase, where there's no `customers` row yet, we use a `trial_keepers` table keyed on session cookie. **The conversion event is the "make it public" payment**, not a calendar day; at that moment the row atomically promotes into a `customers` + `keepers` pair and re-keys all memory rows. The cookie carries an internal 90-day TTL refreshed on every visit purely for hygiene; this is never surfaced as a "trial expiring."

```sql
CREATE TABLE IF NOT EXISTS trial_keepers (
  trial_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cookie_token       TEXT NOT NULL UNIQUE,    -- HMAC-signed cookie value
  companion_name     TEXT NOT NULL,
  companion_assigned TEXT NOT NULL,
  started_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at         TIMESTAMPTZ NOT NULL,    -- internal hygiene TTL (90 days, refreshed on every visit). Never surfaced to keeper as a "trial expiry."
  converted_to_customer_id BIGINT REFERENCES customers(id),  -- NULL until conversion
  released_at        TIMESTAMPTZ              -- set when trial expires + memory purged
);
CREATE INDEX trial_keepers_cookie_idx ON trial_keepers(cookie_token);
CREATE INDEX trial_keepers_expires_idx ON trial_keepers(expires_at) WHERE converted_to_customer_id IS NULL AND released_at IS NULL;
```

### 2.2 Companion conversations + memory

The unit of capture is a **turn**: one keeper utterance + one companion utterance. Each turn is stored verbatim and also classified into structured memory. The verbatim store is for replay/debug/export; the classified store is what the runtime retrieves.

```sql
-- Verbatim turns (the conversation itself)
CREATE TABLE IF NOT EXISTS companion_turns (
  turn_id            BIGSERIAL PRIMARY KEY,
  keeper_id          BIGINT REFERENCES keepers(customer_id) ON DELETE CASCADE,
  trial_id           UUID REFERENCES trial_keepers(trial_id) ON DELETE CASCADE,
  CHECK ((keeper_id IS NOT NULL) <> (trial_id IS NOT NULL)),  -- exactly one
  surface            TEXT NOT NULL,           -- 'table' | 'shore' | 'note' (async leave-a-note) | 'voice-intake'
  keeper_text        TEXT,                    -- nullable for opening companion message
  companion_text     TEXT,
  keeper_audio_url   TEXT,                    -- voice intake; managed S3-style storage
  trust_phase_at_turn SMALLINT NOT NULL,      -- snapshot for replay correctness
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Classified memory items (what the runtime retrieves)
CREATE TABLE IF NOT EXISTS keeper_memory (
  memory_id          BIGSERIAL PRIMARY KEY,
  keeper_id          BIGINT REFERENCES keepers(customer_id) ON DELETE CASCADE,
  trial_id           UUID REFERENCES trial_keepers(trial_id) ON DELETE CASCADE,
  CHECK ((keeper_id IS NOT NULL) <> (trial_id IS NOT NULL)),
  partition          TEXT NOT NULL,           -- 'working' | 'voice' | 'sacred'
  category           TEXT NOT NULL,           -- 'phrase' | 'fact' | 'preference' | 'fear' | 'milestone' | 'ritual' | 'audience' | 'practice'
  content            TEXT NOT NULL,           -- the canonical statement, in third person ("The keeper's audience is somatic practitioners")
  keeper_phrase      TEXT,                    -- the original keeper words (preserved for quote-back)
  source_turn_id     BIGINT REFERENCES companion_turns(turn_id) ON DELETE SET NULL,
  embedding          vector(1536),            -- pgvector; for semantic retrieval
  retention_until    TIMESTAMPTZ,             -- NULL = no auto-purge; set for working memory based on retention_window_days
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX keeper_memory_keeper_partition_idx ON keeper_memory(keeper_id, partition);
CREATE INDEX keeper_memory_embedding_idx ON keeper_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX keeper_memory_retention_idx ON keeper_memory(retention_until) WHERE retention_until IS NOT NULL;
```

**Why partition is a column, not separate tables.** Three reasons. (1) Sacred-mode firewall is enforced at *query time* (the harbor pipeline excludes WHERE partition <> 'sacred', deterministically) rather than by separate-table indirection that's easy to forget. (2) A keeper-initiated reclassification (working → sacred) is a one-row UPDATE, not a cross-table migration. (3) Export-everything is one query. The downside (developer carelessness) is mitigated by integration tests that assert no harbor query, no synthesis prompt, and no cross-keeper retrieval ever touches partition='sacred'.

### 2.3 Subscription + billing

The existing `customers.tier` column is repurposed; new columns layer in subscription state.

```sql
ALTER TABLE customers ADD COLUMN IF NOT EXISTS subscription_status TEXT;
-- values: 'trial' | 'active' | 'past_due' | 'canceled' | 'paused'
ALTER TABLE customers ADD COLUMN IF NOT EXISTS subscription_tier TEXT NOT NULL DEFAULT 'standard';
-- values: 'standard' (39) | 'sliding' (19-38) | 'lantern' (future)
ALTER TABLE customers ADD COLUMN IF NOT EXISTS sliding_scale_amount_cents INT;  -- 1900 to 3900; NULL means standard 3900
ALTER TABLE customers ADD COLUMN IF NOT EXISTS sliding_scale_attested_at TIMESTAMPTZ;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS subscription_current_period_end TIMESTAMPTZ;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS conversation_credits INT NOT NULL DEFAULT 0;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS conversation_turns_this_period INT NOT NULL DEFAULT 0;
```

Pricing constants move from `web/src/types/index.ts` (deprecated tiers section) to web/src/types/subscription.ts:

```typescript
export const SUBSCRIPTION_STANDARD_CENTS = 3900;
export const SUBSCRIPTION_SLIDING_MIN_CENTS = 1900;
export const SUBSCRIPTION_SLIDING_MAX_CENTS = 3800;  // any whole-dollar amount in this range
export const SOFT_CAP_TURNS_PER_PERIOD = 200;
export const CREDIT_PACK_SMALL_CENTS = 900;
export const CREDIT_PACK_MEDIUM_CENTS = 1900;
export const CREDIT_PACK_LARGE_CENTS = 3900;
```

### 2.4 Harbor pipeline tables

```sql
-- Anonymized contribution log: every keeper turn that consent allows contributing
CREATE TABLE IF NOT EXISTS harbor_contributions (
  contribution_id    BIGSERIAL PRIMARY KEY,
  source_turn_id     BIGINT NOT NULL REFERENCES companion_turns(turn_id) ON DELETE CASCADE,
  keeper_id_hash     TEXT NOT NULL,           -- HMAC of keeper_id for de-dupe in synthesis without re-identifying
  topic_tags         TEXT[] NOT NULL,         -- ['sliding-scale', 'about-page', 'pricing-anxiety']
  redacted_text      TEXT NOT NULL,           -- text with names/places/identifiers stripped
  classified_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  partition_check    TEXT NOT NULL CHECK (partition_check = 'working')  -- belt-and-suspenders: never sacred
);

-- Patterns synthesized across contributions, awaiting harbor master review
CREATE TABLE IF NOT EXISTS harbor_patterns (
  pattern_id         BIGSERIAL PRIMARY KEY,
  pattern_summary    TEXT NOT NULL,           -- "Three keepers building sliding-scale pages put it on the homepage rather than the rates page"
  proposed_offer     TEXT,                    -- the soft suggestion that would surface back, if approved
  contribution_ids   BIGINT[] NOT NULL,       -- the anonymized contributions that informed this pattern
  status             TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'declined' | 'edited'
  harbor_master_notes TEXT,
  reviewed_at        TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Approved patterns surface back via this table; companion runtime queries it
CREATE TABLE IF NOT EXISTS harbor_charts (
  chart_id           BIGSERIAL PRIMARY KEY,
  pattern_id         BIGINT NOT NULL REFERENCES harbor_patterns(pattern_id),
  trigger_topic_tags TEXT[] NOT NULL,         -- when the keeper's working context matches these tags, the chart is offerable
  offer_text         TEXT NOT NULL,           -- the actual phrasing the companion uses
  approved_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retired_at         TIMESTAMPTZ              -- set when the pattern stops being useful or true
);
CREATE INDEX harbor_charts_active_idx ON harbor_charts(retired_at) WHERE retired_at IS NULL;
```

### 2.5 Longitudinal markers

```sql
CREATE TABLE IF NOT EXISTS keeper_markers (
  marker_id          BIGSERIAL PRIMARY KEY,
  keeper_id          BIGINT NOT NULL REFERENCES keepers(customer_id) ON DELETE CASCADE,
  marker_type        TEXT NOT NULL,           -- 'pre_launch_reflection' | 'one_month' | 'six_month' | 'one_year' | 'eoy_letter'
  scheduled_at       TIMESTAMPTZ NOT NULL,
  delivered_at       TIMESTAMPTZ,
  marker_payload     JSONB NOT NULL,          -- the keeper's pre-launch answers, the letter draft, etc.
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX keeper_markers_due_idx ON keeper_markers(scheduled_at) WHERE delivered_at IS NULL;
```

### 2.6 Schema migration order

The schema is pre-launch, so the migrations can run in any order. Recommended:

1. Create `keepers`, `trial_keepers`, `companion_turns`, `keeper_memory` (with `pgvector` extension enabled first).
2. Add subscription columns to `customers`.
3. Create `harbor_*` tables.
4. Create `keeper_markers`.
5. Backfill: nothing to backfill. Drop or NULL out the deprecated tier-pricing columns later, after the marketing-site cutover.

---

## 3. Companion runtime

### 3.1 Module layout

```
companion/
├── runtime.ts            # the public turn() function: input -> output
├── prompt.ts             # system-prompt assembly (persona + memory + trust phase + harbor charts)
├── memory_retrieval.ts   # query keeper_memory by semantic + recency + category
├── memory_capture.ts     # post-turn classification of keeper utterances into memory items
├── trust_phase.ts        # phase progression rules + gating
├── ai_honesty_rail.ts    # deterministic checks for AI-impersonation, role-play asks, therapy asks
├── crisis_detection.ts   # keyword + classifier-based detection; routes to crisis script
├── refusal.ts            # categorical refusals (impersonation, fake testimonials, predatory MLM)
└── claude.ts             # Anthropic SDK wrapper with prompt caching
```

The runtime is invoked by the chat HTTP endpoint (`POST /lighthouse/turn`), the async note endpoint, and the longitudinal-marker worker. All three paths converge on `runtime.turn(keeperOrTrialId, surface, keeper_text | keeper_audio_url)`.

### 3.2 The turn lifecycle

```
1. Identity:           load keeper or trial; load trust_phase.
2. Audio:              if voice intake, transcribe via Deepgram; keeper_text = transcript.
3. Pre-flight rails:   crisis_detection.scan(keeper_text); ai_honesty_rail.scan(keeper_text);
                       refusal.scan(keeper_text). Any hit → run that branch and return early.
4. Memory retrieve:    memory_retrieval.fetch(keeper_id, keeper_text):
                          - semantic top-K from keeper_memory (working partition only by default)
                          - recency-weighted recent turns
                          - sacred touched only if keeper_text directly references a sacred memory
5. Harbor offer:       harbor_charts WHERE topic_tags intersects current context AND retired_at IS NULL.
                       At most one offer per N turns; never in a sacred context.
6. Prompt assembly:    prompt.build({persona, memory, trust_phase, harbor_offer, recent_turns, keeper_text}).
7. Claude call:        sonnet (daily) with prompt caching on the persona+memory prefix. Opus on wedge moments
                       (final About-page draft, EOY letter, crisis follow-through).
8. Post-process:       strip any AI-honesty violation that slipped through; log token cost.
9. Memory capture:     classify keeper_text into 0-N memory items; embed; insert with retention_until
                       computed from keeper.retention_window_days (or NULL for sacred).
10. Persist turn:      insert into companion_turns.
11. Trust phase tick:  trust_phase.maybe_advance(keeper_id) based on turn count, tenure, depth signals.
12. Long-silence:      update keeper.long_silence_at if this is the first turn after >2 weeks.
13. Return:            companion_text to the caller.
```

### 3.3 Prompt structure (cacheable prefix + dynamic tail)

The system prompt has three layers, structured for prompt caching:

**Layer 1 (always cached, ~3-5K tokens):** the companion persona, the AI-honesty rail rules, the refusal categories, the brand-voice rules. This is shared across every keeper and rarely changes.

**Layer 2 (cached per keeper, ~2-5K tokens):** the keeper's working-memory retrieval (top-K), their persona summary (synthesized periodically and cached), their trust phase, their consent flags, their assigned and current companion name. Rebuilt when memory materially changes.

**Layer 3 (uncached tail, ~500-2K tokens):** the most recent N turns of conversation, the harbor offer if any, the keeper's current message.

With Claude's prompt caching (90% discount on cached tokens), a typical 8K-token call costs ~$0.005 instead of ~$0.024.

### 3.4 The AI-honesty rail

This is the load-bearing piece. The rail is a deterministic check (regex + lightweight classifier) that runs *before* every Claude call and *again* on every Claude response. It enforces:

- The companion never claims to be human ("I am Haven, a cocreation companion. I'm an AI, and I'll tell you so any time you ask.")
- The companion never roleplays as another person, real or fictional, on request.
- The companion never therapizes (when the keeper asks for therapy, the script routes to the "you deserve a real human" template + a referral to crisis resources if signals warrant).
- The companion never gives medical or legal advice (same routing).
- The companion never pretends to remember something not in `keeper_memory`. If asked about an event it has no record of, it says so.

Implementation: a Python-style classifier on the rail (small, fast, runs <50ms) flags potential violations; the prompt is augmented with explicit rejection scaffolding before Claude is called; the response is post-checked against the same flags. If a Claude response slips through, we strip it and re-emit the deterministic refusal template.

### 3.5 Crisis detection

A separate, narrow scan: keyword + lightweight classifier for self-harm, abuse-by-other, acute medical, suicidal ideation. Hits route to a crisis script that:

1. Acknowledges the keeper specifically and warmly
2. Names that the companion is AI and not enough for this moment
3. Offers concrete external resources (988, RAINN, country-specific lines based on locale if known)
4. Logs the turn with a `crisis_flagged` boolean to a separate small table for harbor master triage
5. Does not contribute to harbor (sacred-by-default for any flagged turn)
6. Does not therapize, does not offer coping techniques, does not pretend to be enough

The crisis script is one of the few places we use Opus, not Sonnet. The cost overhead is trivial; the quality stakes are not.

### 3.6 Trust phase progression

Trust phases are gates on what the companion offers, not what the keeper can ask for. The keeper can always ask anything; the companion's outputs are scoped.

| Phase | Tenure / signal | What the companion does |
|-------|-----------------|-------------------------|
| 0 (listen) | First session through ~5 turns | Mostly asks gentle questions; reflects keeper words back; never proactively suggests; never offers harbor charts |
| 1 (light suggestions) | ~5 turns through end of session 2 | Offers small reflections; asks one shaping question per turn at most; still no harbor charts |
| 2 (co-create) | Session 3 through ~30 days | Quote-back active; one-question-per-week active; harbor charts can be offered (with consent flag); About-page interview cycle starts when the keeper invites it |
| 3 (full presence) | After ~30 days of substantive engagement | Site blessings; site siblings introductions; longitudinal markers active; pre-launch ritual active; voice ledger active |

Progression is mostly automatic but bounded: tenure alone advances Phase 0 → 1 → 2; depth signals (length of turns, emotional content, invitations to suggest) advance 2 → 3. The keeper can also invite a slower pace ("can we just talk a little while longer before you start helping me build?") and the runtime respects it.

---

## 4. Memory subsystem

### 4.1 Three partitions

| Partition | Purpose | RAG-able by companion | Contributes to harbor | Auto-purge |
|-----------|---------|------------------------|----------------------|------------|
| `working` | Default. Phrases, facts, preferences, audience details, practice rituals | Yes | Yes (if consent) | After `retention_window_days` post-deactivation |
| `voice`   | Voice intake transcripts + voice-derived classifications | Yes | No (voice content is more sensitive) | After `retention_window_days` |
| `sacred`  | Trauma, grief, family, faith, identity disclosures, anything keeper-flagged | Only when keeper directly references | Never | After 12 months post-deactivation regardless of working window |

### 4.2 Memory capture (how working becomes structured)

After every keeper turn the companion runtime calls `memory_capture.classify(keeper_text, recent_context)`. The classifier (Claude Haiku, cheap) returns 0-N candidate memory items with proposed `category`, `content` (third-person canonical), and `keeper_phrase` (verbatim). Items are filtered:

- **Sacred-mode auto-flag:** if the classifier's confidence on grief/trauma/family/faith/identity content exceeds threshold, partition is set to `sacred`. The keeper can also explicitly say "this is sacred" or use a UI affordance.
- **De-dupe:** semantic search against existing memory; if cosine similarity >0.95 to an existing item, skip insertion (or update timestamp on the existing).
- **Retention timestamp:** for working/voice, `retention_until = NOW() + (keeper.retention_window_days || ' days')::interval` *if* the keeper deactivates; otherwise NULL while the relationship is active.

### 4.3 Retrieval (RAG)

Retrieval at turn-time is a hybrid query:

1. **Semantic top-K** (cosine similarity) over `keeper_memory.embedding` against an embedding of the current keeper_text. K=8.
2. **Recency boost:** the most recent 5 memory items are always included regardless of similarity.
3. **Category-aware:** if the current turn is about audience, boost `category='audience'` items; if about pricing, boost preferences-around-money items.
4. **Sacred firewall:** by default `WHERE partition <> 'sacred'`. Only included if the keeper's text explicitly references a sacred memory (e.g. mentions a name, a date, a phrase that matches a sacred item with similarity >0.8).

### 4.4 Sacred firewall (defense in depth)

Three layers:

1. **Query-level:** every harbor-pipeline query, every cross-keeper retrieval, every synthesis prompt asserts `WHERE partition <> 'sacred'`. Linted by an integration test that scans `harbor/` and `companion/` for raw `keeper_memory` queries lacking the predicate.
2. **Application-level:** the synthesis worker (Section 5.3) loads candidate contributions through a single function that filters by partition; bypassing it requires editing that function.
3. **Audit:** every read of a sacred memory is logged to a small audit table with caller-identifying metadata. Periodic Ocean review.

### 4.5 Retention enforcement

A scheduled worker (`memory/retention_worker.py`, runs daily) does:

```sql
DELETE FROM keeper_memory WHERE retention_until IS NOT NULL AND retention_until < NOW();
DELETE FROM companion_turns WHERE created_at < NOW() - INTERVAL '12 months'
  AND keeper_id IN (SELECT customer_id FROM keepers WHERE deactivated_at IS NOT NULL AND deactivated_at < NOW() - INTERVAL '12 months');
```

Trial keeper memory is purged when `trial_keepers.expires_at < NOW() AND converted_to_customer_id IS NULL` (set `released_at` and cascade-delete their memory and turns).

### 4.6 Export + amend + delete

- **Export:** `GET /lighthouse/memory/export` produces a Markdown file (the "book of their voice") plus a JSON dump for completeness. Sacred memory is included by default, with an explicit checkbox to exclude.
- **Amend:** any memory item shows up in a "what I remember about you" view; the keeper can edit `content` (we preserve the original `keeper_phrase` separately) or flag-as-sacred or delete.
- **Delete:** soft-delete (set `deleted_at`) for 30 days, then hard-delete. The companion treats soft-deleted items as gone.

---

## 5. Harbor pipeline

### 5.1 Consent model

Default is opt-out for collective contribution. The keeper opts in via `keepers.consent_collective = TRUE` after reading a clear plain-English explanation. They can also opt in granularly per topic (we model this as a separate `keeper_consent_topics` table if the granular UI ships; otherwise it's all-or-nothing for v1).

### 5.2 Anonymization pipeline

For each turn where the keeper has consent, a worker (`harbor/anonymize_worker.py`):

1. Loads the turn + the keeper's recent persona summary
2. Calls Claude to redact: names of people, business names, locations, dates, identifying numbers; replace with `[name]`, `[place]`, `[date]`, etc.
3. Tags topics (`sliding-scale`, `about-page`, `audience`, `pricing-anxiety`, etc.) using a fixed taxonomy
4. Inserts into `harbor_contributions` with a HMAC-of-keeper-id rather than the keeper id itself

The taxonomy starts narrow (~20 tags) and grows from harbor master review.

### 5.3 Synthesis (Claude in batch)

Once weekly, a worker:

1. Groups recent `harbor_contributions` by topic tag
2. For each topic where N>=3 contributions, asks Claude (Sonnet) to find a meaningful pattern
3. Inserts proposed `harbor_patterns` rows with status='pending'

This is where we resist the temptation to surface anything automatically.

### 5.4 Harbor master review queue

Ocean (the harbor master) sees `/admin/harbor/queue`. Each pending pattern shows:

- The proposed summary
- The proposed offer text (what would surface back to keepers)
- Up to 5 redacted source contributions
- Topic tags
- Buttons: Approve, Decline, Edit-then-approve

Approve creates a `harbor_charts` row. Decline marks the pattern but keeps it for audit. Edit lets Ocean tune the offer text before approving.

### 5.5 Surfacing back to keepers

At turn-time, after memory retrieval and before prompt assembly, the runtime queries:

```sql
SELECT * FROM harbor_charts
WHERE retired_at IS NULL
  AND trigger_topic_tags && current_context_tags
LIMIT 1;
```

If a chart is found and the trust phase is >=2 and the keeper has consent_collective and the surface is not crisis, the chart's `offer_text` is included in the prompt as "if relevant, you may gently offer this." Claude decides whether to actually offer it; if it does, we log the offer to a small table to throttle (don't repeat the same chart to the same keeper within N days).

---

## 6. Onboarding + the no-clock keeper journey

### 6.1 The "just talk" surface (no account, no card, no clock)

`GET /` lands on the brand-softened marketing site. The primary CTA is "Meet your companion." Clicking goes to `/lighthouse` (no auth gate).

`GET /lighthouse` mints a trial cookie (`HMAC(SESSION_SECRET, uuid())`), creates a `trial_keepers` row, assigns Haven, and redirects to `/lighthouse/table` where the chat UI loads with an opening message from the companion.

The conversation is genuinely no-friction: no email, no phone, no card, no countdown. The cookie is the identity. We accept that some conversations will be lost to cleared cookies; the cost of those lost relationships is much lower than the cost of front-loaded friction.

### 6.2 Spec clarity → private build (the new automatic trigger)

There is **no time-based outreach** (the day-7 letter mechanic is retired, 2026-04-28). Instead, when the conversation reaches a point where Haven and the keeper agree the shape of the website is clear (Haven asks plainly; keeper confirms in their own words), the runtime kicks `extractSpecFromConversation()` and POSTs `/lighthouse/build` automatically. The build runs through the normal v2 compose pipeline + senior-engineer review and lands at a **private preview URL** the keeper can revisit any time, scoped to their cookie identity. No card asked for at any step.

The keeper can come back to Haven, refine, request changes, trigger another build iteration. There is no penalty for taking weeks. There is no "trial expired" message; ever.

### 6.3 The payment moment: "make it public"

When the keeper decides they want their site live (a deliberate choice, not a clock), they click **Make this public**. That click is the conversion moment:

1. They enter email + (optional) custom domain. If no domain, the harbor-subdomain default is used.
2. `POST /lighthouse/make-public` creates a `customers` row, a `keepers` row (copying companion_name and companion_assigned from the trial), runs Stripe Checkout for the $39 subscription with the sliding-scale attestation step.
3. On Stripe success webhook: update the keeper's subscription_status to 'active', set converted_to_customer_id on the trial row, re-key all `companion_turns` and `keeper_memory` rows to point at the new keeper_id (within a transaction), and **flip the build's `status` from `preview` to `live`** + index it + serve it on its public domain.
4. Send the welcome email; the companion opens the next session with a referenceable acknowledgment ("we made it. Glad we're here.")

### 6.4 Sliding scale attestation

On the Begin page, a small disclosure: "$39/month is the standard. If that's not comfortable for you right now, you can choose any amount from $19 to $38. Your word is enough." A slider or a free-text box. The chosen amount is stored in `customers.sliding_scale_amount_cents`. Stripe is told to bill that amount.

---

## 7. Companion chat UI

### 7.1 Surfaces

| Path | Purpose |
|------|---------|
| `/lighthouse` | Entry; mints trial cookie or redirects authed keeper to /lighthouse/table |
| `/lighthouse/table` | The keeper's table: the primary chat surface, structured for tending the lamp |
| `/lighthouse/shore` | The casual chat surface: looser, more exploratory; same chat engine, different framing/styling |
| `/lighthouse/note` | Async leave-a-note: text-only, no expectation of immediate reply |
| `/lighthouse/lamp` | The keeper's preview of their built lamp (the live website; reuses v2 admin) |
| `/lighthouse/memory` | The "what I remember about you" view: working memory list with edit/sacred/delete |
| `/lighthouse/memory/export` | Download the book |
| `/lighthouse/voice` | Voice intake (records, transcribes via Deepgram, posts as a turn) |
| `/lighthouse/settings` | Consent flags, retention window, sliding-scale amount, billing |

### 7.2 Visual direction (lantern light)

- Soft serif for display (Cooper / Tiempos Text)
- Humanist sans for body (Söhne / GT Walsheim)
- Sage / clay / dusk-rose / oat / ink-brown palette with coastal accents
- Chat bubbles styled as letters on warm paper, lit from one side (subtle gradient)
- No live typing indicator that pretends the companion is "typing" right now (it's a letter; it arrives when it arrives, but we do show a soft "she's writing" pulse during the actual API call so the keeper knows the system is working)
- No notification badges or unread counts. Letters land in the table; the keeper sees them when they look.

### 7.3 The async leave-a-note model

`POST /lighthouse/note` takes only `text`. The companion responds with a brief acknowledgment ("got it, I'll sit with this") rather than a full reply. The full reply lands the next time the keeper opens the table (computed on-demand at table load, so it always feels fresh).

### 7.4 Voice intake

`/lighthouse/voice` records audio in-browser (MediaRecorder API), uploads to a signed URL, the runtime calls Deepgram Nova-3 (~$0.0043/minute) for transcription, and the result becomes a `companion_turns` row with `surface='voice-intake'` and `keeper_audio_url` pointing at the stored audio. The companion replies in writing only; no TTS.

---

## 8. v2 compose integration (the lamp moment)

### 8.1 When does the build run

The build is no longer the front door. It runs when the companion senses the keeper is ready and asks: "Would you like to start shaping the lamp now?" That happens when:

- The keeper has expressed a clear practice description in working memory
- The keeper has answered enough about audience, tone, and offerings
- Trust phase is 2+
- The keeper says yes

On yes, the runtime translates the relevant working-memory items into an v2 spec (`spec_version: '2.0'`, archetype derived from practice category) and dispatches to the existing v2 compose path.

### 8.2 About-page interview cycle (the wedge)

After the first build is up, the companion proposes the About-page interview cycle: ~6 short interviews over multiple sittings. Each interview is a guided conversation logged as a sequence of `companion_turns`. Key phrases are captured into `keeper_memory` with `category='phrase'`. After all interviews, an Opus call drafts the page using the keeper's actual phrases as raw material; the keeper reviews and edits; v2 compose re-emits with the new About content.

### 8.3 Lamp-set ritual

When the keeper says they're ready to set the lamp (go live), the companion runs the pre-launch reflection (3 questions: who is this for, what would feel like success, what are you afraid of). Answers are stored in `keeper_markers.marker_payload` with `marker_type='pre_launch_reflection'`. The Senior Web Craftsman review still runs (we don't skip Craft Quality), but reframed: it's a final reading at the keeper's table before the lamp is set, not a gate.

After approval, the companion writes a small blessing and the public site goes live. The blessing is a special turn the keeper can save or share.

### 8.4 Longitudinal markers (cron)

A worker runs daily and looks for `keeper_markers WHERE scheduled_at < NOW() AND delivered_at IS NULL`. For each, it generates the appropriate letter (Opus for EOY, Sonnet for monthly markers) referencing `marker_payload` from the pre-launch reflection. Letters are inserted as `companion_turns` and the keeper sees them next time they open the table.

---

## 9. Brand softening (marketing site migration)

### 9.1 Order of operations

1. Extend `docs/BRAND_VOICE.md` with the full lexicon (use list / retire list), tone examples, and the "no AI builds your website" rule replaced with "your companion helps you shape and tend your lighthouse, with a Senior Web Craftsman's hand on the build itself."
2. Rewrite the landing page (`web/views/index.ejs`) hero, sections, footer copy.
3. Rewrite pricing (`web/views/pricing.ejs`) for $39/month flat with sliding scale; remove tier matrices.
4. Rewrite how-it-works (`web/views/how-it-works.ejs`) around the relationship arc, not the build pipeline.
5. Soft-rewrite gallery, contact, intake-as-companion-entry.
6. Rewrite all email templates in `services/email.ts` and `queue/mailer.py`.
7. Soft-retire `/try` (redirect to `/lighthouse`).

Each step is its own branch and PR. None of them depend on the companion runtime being shipped; they ship in parallel with Phase 0.

### 9.2 Visual direction

- Rebuild the public CSS layer with the new palette and type
- Replace photography with hand-drawn illustration where licensable; pencil in lighthouse-and-harbor hero
- Single-image hero per page; generous whitespace
- Soft serifs, never geometric sans for headlines

### 9.3 BRAND_VOICE.md v2 rule changes

| Old rule | New rule |
|----------|----------|
| "The build is done by a Senior Web Craftsman" | "Your cocreation companion helps you shape and tend your lighthouse; a Senior Web Craftsman reviews and approves the build itself." |
| "No AI claims" | "AI honesty: we're explicit that the companion is AI; we never claim AI builds the website" |
| "Use 'website,' not 'app/software/platform'" | Unchanged |
| "1 to 2 business days review" | Unchanged for now; revisit when companion-driven flow stabilizes review pacing |
| "No em dashes" | Unchanged |
| (new) | Lighthouse-and-safe-harbor lexicon: lighthouse, lamp, keeper, keeper's table, shore, safe harbor, dock rail, headlands, harbor master, voyage, setting the lamp |
| (new) | Affirmative voice; describe what we do, not what we are not (existing), and never market the companion as a "friend" |

---

## 10. Subscription billing

### 10.1 Stripe wiring

- Create a Stripe Product "Living Platform Subscription" with two Prices:
  - `price_living_standard`: $39.00 USD recurring monthly
  - `price_living_sliding`: recurring monthly with `unit_amount` overridden at Checkout creation per attestation (use `price_data` to set the amount inline)
- For credit packs (later, Phase 4): create one-time prices at $9, $19, $39
- Webhook endpoint `POST /webhooks/stripe-living` updates `customers.subscription_status` based on event type

### 10.2 Sliding scale

The Begin Together flow includes a single screen with a slider 19-39 USD, a checkbox "this is honest," a one-line *"Choose any amount from $39 down to $19. Your word is enough,"* and a Continue button. Whatever amount they pick is the price of the subscription (they can re-attest up or down any time from `/lighthouse/settings/billing`).

### 10.3 Conversation soft-cap

`customers.conversation_turns_this_period` increments per turn. A nightly job resets at the start of each Stripe period. If the keeper exceeds `SOFT_CAP_TURNS_PER_PERIOD` (200), a small banner offers the credit packs. No paywall; it's a mention, not a wall. Most keepers never see it.

---

## 11. Voice intake (Deepgram Nova-3)

### 11.1 Wiring

- Deepgram account; API key in `DEEPGRAM_API_KEY` env var
- Backend route `POST /lighthouse/voice/upload` accepts audio chunk, returns transcript via Deepgram REST
- We recommend Nova-3 for accuracy + speed + cost (~$0.0043/min)
- Whisper API as fallback if Deepgram has an outage

### 11.2 What's stored

- The audio file in S3-compatible storage (Backblaze B2 likely; cheapest, S3 API compatible)
- The transcript as `companion_turns.keeper_text` with `surface='voice-intake'`
- Memory items derived from the transcript marked with `partition='voice'` (more conservative retention/contribution defaults)

### 11.3 What we never do

- No TTS. The companion writes; the keeper speaks if they want. The companion never speaks.
- No streaming voice display ("you said: ...") that would feel like surveillance. The transcript is shown after submission, edit-able if Deepgram got it wrong.

---

## 12. Phased build sequence (deliverables, not timelines)

**Phase 0: companion runtime + memory (the heart).** Schema migrations 2.1-2.2; companion module skeleton; AI-honesty rail; crisis detection; trust phase 0-1; chat UI for the keeper's table; trial cookie surface; one happy-path end-to-end conversation. No payments, no harbor, no build. Just talk.

**Phase 1: brand softening + entry door.** the brand-voice update; rewrite landing, pricing, how-it-works, contact; rewrite email templates; redirect `/try` → `/lighthouse`. Concurrent with Phase 0; either ships independently.

**Phase 2: pricing + "make it public" payment moment.** Subscription columns; Stripe wiring; sliding-scale attestation; trial-to-keeper promotion path triggered by the "make it public" event (no day-7 letter, no clock).

**Phase 3: v2 compose integration + private preview.** When-build-runs logic (auto-fires once Haven and the keeper agree the spec is clear); private preview URL scoped to cookie identity; About-page interview cycle; pre-launch ritual; Senior Web Craftsman review reframed; site blessing. The preview is reachable without payment; only "make it public" requires the card.

**Phase 4: harbor pipeline.** Consent UI; anonymization worker; synthesis worker; harbor master review queue; harbor charts surfacing; site siblings introductions (manual to start).

**Phase 5: longitudinal letters + voice intake + dedicated lantern foundations.** EOY letters; one-month / six-month / one-year markers; Deepgram Nova-3 voice intake; LoRA-adapter-per-keeper infrastructure work that the dedicated lantern depends on (without yet exposing the tier).

Each phase is independently shippable.

---

## 13. Risks + open technical questions

### 13.1 Open technical questions (we should answer these before Phase 2)

- **Token economics validation.** Run 100 simulated companion turns end-to-end with Sonnet + prompt caching; measure actual per-turn cost. Compare to the $0.005/turn estimate.
- **Embedding model + dimension.** OpenAI text-embedding-3-small (1536d) is cheap and fast; Anthropic doesn't have a hosted embedding model. Stick with OpenAI for embeddings or evaluate Voyage AI? Default: OpenAI text-embedding-3-small unless cost or quality argues otherwise.
- **Sacred classification accuracy.** We can't test this without real conversation data. Plan: Phase 0 ships with a conservative sacred-flag classifier (high recall, may over-flag); we tune over time as we see misclassifications.
- **The no-clock trial vs abuse.** With no time-based gate at all, some visitors will rack up many turns without ever paying. Mitigation: per-cookie turn-rate caps (e.g. <50 meaningful turns/day) bound worst-case spend; the spec-clarity → private-build trigger naturally caps how long a single trial can chew through Claude tokens before the keeper either commits or drifts away. Revisit if abuse becomes a real cost vector.
- **Memory growth per keeper.** A keeper at 3 turns/day for a year = ~1100 turns + ~3000 memory items. With 1536d float32 vectors that's ~18MB of vector data per keeper per year, plus ~1MB of text. Negligible per keeper; need to watch ivfflat index lifetime as the table grows past ~10K keepers (consider HNSW migration then).

### 13.2 Risks to manage

- **The companion gets dumber after a major Claude model swap.** When Anthropic releases a new model, prompt-caching invalidates and the persona/memory cache rebuild is one-time but the keeper might feel a "Haven changed somehow" moment. Mitigation: hold a stable Sonnet pin per keeper; migrate keepers in cohorts with an explicit "your companion has new tools" note when we move them.
- **Sacred firewall regression.** A junior engineer adds a new harbor query without the partition filter. Mitigation: integration tests as described in Section 4.4 layer 1; CI fails the merge.
- **The harbor master becomes a bottleneck.** Ocean reviews every promoted pattern. Mitigation: tier the queue (auto-approve patterns from very narrow categories where the synthesis is mechanical) only if and when needed; resist auto-promote temptation.
- **Deactivation is grief.** Keepers who delete will likely return. Mitigation: pause-don't-delete is the default in the UI; "freeze for a season" is an explicit option.

### 13.3 Open decisions (small)

- Retention window post-deactivation in days for working memory: 30 / 60 / **90**. Lock in this number.
- Right-to-be-forgotten public copy: the asymmetry that we can withdraw from active memory and future synthesis but cannot un-influence already-trained patterns needs honest plain-English language. Draft below as a starting point.
- Embedding provider choice (OpenAI vs Voyage AI): default to OpenAI text-embedding-3-small, revisit if cost or quality argues otherwise. Unblock the build.
- Storage provider for voice audio: Backblaze B2 (recommended; S3-compatible; ~$0.005/GB/month) vs AWS S3. Default to Backblaze.

**Right-to-be-forgotten draft language:**

> When you ask us to forget something, we delete it from your active memory and from anything we've published into the safe harbor. The harder honest part: if a pattern from your past conversation was approved into a chart that other keepers have already seen, we cannot un-show it to them. We can stop the chart from surfacing again, and we will. We can't undo the influence it already had. We're telling you this because we think you deserve to know.

---

## 14. What's reused vs new (one-table summary)

| Component | Status | Notes |
|-----------|--------|-------|
| PostgreSQL | Reused | Add pgvector extension; add new tables in §2 |
| Express + EJS | Reused | New `/lighthouse/*` routes layered on existing app skeleton |
| Tailwind v3.4 + PostCSS | Reused | New palette + type tokens; same build pipeline |
| Vitest + Playwright | Reused | New web/tests/unit/companion.test.ts etc. |
| Stripe | Reused | Add subscription product + sliding-scale price overrides |
| Resend | Reused | New brand-voiced templates |
| Magic-link auth | Reused | Becomes one of two auth options (alongside email+password) |
| `connect-pg-simple` sessions | Reused | Trial cookie is a different mechanism (HMAC), not session-based |
| v2 compose engine | Reused | Invoked from companion runtime instead of intake form |
| Senior engineer review | Reused | Reframed as pre-set-the-lamp ritual |
| Worker queue | Reused | New jobs: harbor synthesis, marker delivery, retention purge |
| Companion runtime | New | `companion/` module |
| Memory subsystem | New | `memory/` module + `keeper_memory` table |
| Harbor pipeline | New | `harbor/` module + `harbor_*` tables |
| Voice intake | New | Deepgram Nova-3 + audio storage |
| Subscription billing | New | Stripe subscription + sliding-scale flow |
| Brand-softened marketing site | Mostly rewrite | Same templates, new copy + visuals |
| Trial cookie surface | New | `trial_keepers` table + cookie middleware |
| Longitudinal markers | New | `keeper_markers` + cron worker |
| Trust phase gating | New | `companion/trust_phase.ts` + `keepers.trust_phase` |
| AI-honesty rail | New | `companion/ai_honesty_rail.ts` + integration tests |
| Crisis detection | New | `companion/crisis_detection.ts` + crisis script template |

---

## 15. What this doc is not

- A detailed prompt library (companion persona text, AI-honesty rail rules, crisis script copy, refusal templates). That's a separate working doc to write in Phase 0.
- A finalized UI design (layouts, components, visual treatments). That's the brand-softening + frontend-design pass.
- A test plan. That's per-phase and lives alongside each phase's PR.

The vessel takes shape in conversation, not in documents. This doc is the build map; the real architecture is what we ship.

---

## Appendix A: Revision log

- **2026-04-26 (initial):** First draft. All sections. Open questions are listed at §13.3.
