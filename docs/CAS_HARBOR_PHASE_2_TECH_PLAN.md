# CAS Harbor Phase 2 Tech Plan

> **Dated plan, kept as a design record.** It describes intent at the time it was written, not the published code; where they differ, the code is the reference. In particular the plan calls the preview "private": in the code a preview is a public address with no access control (see [SECURITY.md](../SECURITY.md), "Preview, review and live"). The two day-7 modules the plan names, `conversion_letter.ts` and `day7_letter.ts`, were kept as dormant code at the time and have since been deleted, so the paths that name them below no longer exist; the `day7_letter_sent_at` columns they would have written remain in the schema, unused.

> **2026-04-28 PIVOT (supersedes everything below where in conflict):**
>
> The whole keeper journey is reshaped around a quality trigger and a value-aligned payment moment, not a clock. The new arc:
>
> 1. **Open conversation with Haven.** No card, no clock, no countdown. Conversations last as long as they take.
> 2. **Spec clarity.** Haven and the keeper agree (in conversation) that the shape of the website is clear.
> 3. **Private build.** CAS Harbor builds the site automatically off Haven's extracted spec. The site is **previewable on a private URL only**, not indexed, not public, not served on a custom domain.
> 4. **Walk around.** The keeper visits the preview at their own pace. They can come back to Haven and refine. Multiple build iterations are fine.
> 5. **Make it public (= the payment moment).** When the keeper decides they want the site live, they pay. That single moment is the trigger for: collecting the card, asking for a custom domain (or the harbor-subdomain default), promoting `trial_keepers` → `keepers`, flipping the build's `status` from `preview` to `live`, indexing it.
>
> **Retired entirely:**
> - The day-7 letter and any time-based cron. `services/conversion_letter.ts` + `cron/day7_letter.ts` stay as dormant code-on-the-shelf, NOT wired anywhere.
> - The "trial expires" / "trial period" concept on customer surfaces. Cookie TTL is 90 days for hygiene only, refreshed every visit, never named in copy.
> - The hard turn-count email gate (turn 22 → 402). The email is asked for at the moment the keeper says they want a build, by Haven, in conversation, not by the server with a payment-required response.
>
> **Still relevant from the original plan below:**
> - **Trial → keeper atomic promotion** is unchanged in mechanism, but the **trigger** is the "make it public" event, not Stripe Checkout completing on a trial-expiry path.
> - **v2 compose handoff** is unchanged; it now fires automatically when Haven and the keeper agree the spec is clear, with no payment required at this point.
> - **Crisis email plumbing**, **brand-softened landing**, **admin harbor inbox v2**; unchanged.
>
> **Open decisions after the pivot:** (a) the exact criterion that means "Haven and the keeper agree the spec is clear" (Haven asks plainly + keeper confirms; or a heuristic on Haven's classifier; or both); (b) Stripe price IDs for the "make it public" charge.

---

**Status:** plan only, not implemented. Written 2026-04-27 by Claude Opus 4.7. **MVP correction at top supersedes specifics below.**
**Predecessor:** `docs/CAS_HARBOR_PHASE_1_TECH_PLAN.md` (Phase 1 = companion runtime + memory + chat surface).

This plan covers everything needed to take CAS Harbor from "Ocean reads admin/harbor and emails keepers by hand" (Phase 1) to "self-serve subscription + automated v2 compose handoff" (Phase 2 GA). Phase 2 is large; it ships in slices with a clear ordering.

---

## 1. Goals (what Phase 2 ships)

1. **Subscription billing.** $39/month flat with sliding-scale-to-$19 attestation. 7-day "just talking" trial. Stripe Checkout for the upgrade. Cancel-anytime. (No tiered prices; no credit packs in MVP.)
2. **Trial → keeper atomic promotion.** When a trial-cookie keeper signs up + pays, their conversation history + memory rows transfer to a permanent `keepers` row paired with `customers(id)`. Single transaction. Zero memory loss.
3. **v2 compose handoff.** When the keeper reaches the SKETCH stage of the working arc and says "yes, build it," extract a structured spec from their conversation + memory and dispatch it to the existing v2 build pipeline (`compose/build.py` via the queue worker). Reuses CAS's existing build infrastructure.
4. **Day-7 conversion letter.** Cron job runs daily; for each trial reaching day 7 with N+ turns and no upgrade, generate an Opus-quality letter that reflects what they shared and gently invites the build. Sends via Resend. Single touch, no follow-ups.
5. **Crisis email plumbing.** Wire `notifyHarborMaster()` from runtime.ts into `services/email.ts` for real ADMIN_EMAIL alerts (currently logs only; Phase 1 contract).
6. **Brand-softened landing.** Coordinate with the parallel homepage workstream to land the unified $39/mo offering on `/`, `/the-companion`, `/recent-harbors`, `/about`, `/begin`. (Customer-facing copy belongs to that workstream; backend wiring belongs to this plan.)
7. **Admin harbor inbox v2.** Add hand-off action button + filters by status (active / ready / handed-off / converted) on top of Phase 1's read-only listing. Records hand-off timestamp + which build_id was dispatched.

## 2. Non-goals (deferred)

- **Voice intake.** Whisper / Deepgram. Deferred to Phase 3.
- **Sacred-mode firewall.** Per-keeper memory partitioning enforcement. Phase 4.
- **pgvector migration.** Move from JS-side cosine to DB-side. Triggered by keeper count, not date. Phase 4+.
- **End-of-year letters.** Deferred to Phase 3.
- **Site siblings (collective contributions).** Phase 4 or later.
- **Dedicated-lantern tier (open-weight per-keeper LoRA).** Phase 5+.
- **Couples / shared accounts.** Open question; deferred.
- **Tiered pricing or credit packs.** Phase 2 ships single $39 flat with sliding scale; tiers were considered and cut.

---

## 3. Architecture overview

```
┌──────────────────────┐
│ trial-cookie keeper  │  Phase 1: cookie-only identity, 7-day TTL
│ (anon, /lighthouse)  │  Phase 1 surface; unchanged in P2.
└──────────┬───────────┘
           │  upgrade flow (P2 NEW)
           │  └─ /lighthouse/upgrade →
           │     POST /lighthouse/upgrade/checkout (Stripe Checkout)
           │     ↓
           │     Stripe Checkout success (webhook) →
           │     trial→keeper atomic promotion (P2 NEW)
           ↓
┌──────────────────────┐
│ paid keeper          │  P2: customers(id) + keepers(customer_id) row
│ (auth, /lighthouse)  │  Inherits all conversation_turns + keeper_memory.
└──────────┬───────────┘
           │
           ├─ continued conversation (companion runtime, unchanged)
           │
           ├─ "yes, build it" trigger (P2 NEW)
           │  └─ POST /lighthouse/build →
           │     extractSpecFromConversation(keeperId) →
           │     queue.addToQueue(spec) [existing v2 path]
           │     ↓
           │     existing v2 compose pipeline produces the website
           │     ↓
           │     existing SE review gate
           │     ↓
           │     existing deploy + emails
           │
           └─ day-7 cron sweep (P2 NEW)
              └─ for each trial-or-keeper at day 7 + 8+ turns + not converted →
                 Opus call → conversion letter → Resend
```

**Reused infrastructure:**
- v2 compose pipeline (`compose/build.py` → Railway deploy); unchanged.
- Stripe Checkout + webhooks: extends existing `/payment` flow with new subscription price.
- Resend mailer (`services/email.ts`): adds 2 new templates (conversion-letter, crisis-alert).
- pg_cron OR a Node interval scheduler for the day-7 sweep; TBD; see § 5.4.

**New infrastructure:**
- `web/src/services/keeper_promotion.ts` (atomic trial→keeper transaction).
- `web/src/services/spec_extraction.ts` (memory → v2 spec JSON).
- `conversion_letter.ts` in `web/src/services/` (Opus call + Resend send; deleted later).
- Stripe Checkout for the harbor subscription and the handoff trigger (both ended up in `web/src/routes/lighthouse.ts`).
- `day7_letter.ts` in `web/src/cron/` (scheduled job; deleted later).

---

## 4. Schema additions

```sql
-- Add columns to existing keepers table from Phase 1.
-- All ALTER ... ADD COLUMN IF NOT EXISTS; non-destructive.

ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'active'
    CHECK (subscription_status IN ('active', 'past_due', 'canceled', 'trialing'));
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS sliding_scale_attestation BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS sliding_scale_amount_cents INTEGER;
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS handoff_build_id UUID REFERENCES builds(id) ON DELETE SET NULL;
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS handoff_at TIMESTAMP;
ALTER TABLE keepers
  ADD COLUMN IF NOT EXISTS day7_letter_sent_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS keepers_subscription_status_idx ON keepers(subscription_status);
CREATE INDEX IF NOT EXISTS keepers_handoff_at_idx ON keepers(handoff_at) WHERE handoff_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS keepers_day7_pending_idx ON keepers(created_at)
  WHERE day7_letter_sent_at IS NULL;

-- New table: harbor_subscriptions for subscription event audit trail.
CREATE TABLE IF NOT EXISTS harbor_subscriptions (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keeper_customer_id       UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  stripe_subscription_id   TEXT NOT NULL UNIQUE,
  stripe_customer_id       TEXT NOT NULL,
  status                   TEXT NOT NULL,
  current_period_start     TIMESTAMP,
  current_period_end       TIMESTAMP,
  cancel_at_period_end     BOOLEAN NOT NULL DEFAULT FALSE,
  trial_started_at         TIMESTAMP,
  trial_ended_at           TIMESTAMP,
  monthly_amount_cents     INTEGER NOT NULL DEFAULT 3900,  -- $39.00 default
  created_at               TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS harbor_subscriptions_keeper_idx
  ON harbor_subscriptions(keeper_customer_id);
CREATE INDEX IF NOT EXISTS harbor_subscriptions_stripe_idx
  ON harbor_subscriptions(stripe_subscription_id);
```

**Why a separate `harbor_subscriptions` table:** existing CAS one-time `builds` table mixes one-time payment + subscription fields. Keeping subscription state in a dedicated table avoids polluting the one-time path; trial-to-keeper promotion can write to both atomically.

**Migration safety:** all changes are `ADD COLUMN IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`. Idempotent; safe to re-run.

---

## 5. Component specs

### 5.1 Trial → keeper atomic promotion

**Purpose:** when a cookie-trial keeper successfully completes Stripe Checkout, transfer their conversation history + memory + cookie identity to a permanent `keepers` row paired with `customers(id)`; in a single transaction so a partial failure leaves no orphans.

**Trigger:** Stripe `checkout.session.completed` webhook for a session with `metadata.cas_harbor_trial_id` set.

**Service:** `web/src/services/keeper_promotion.ts`

```typescript
export interface PromoteTrialOpts {
  pool: Pool;
  trialId: string;          // from session.metadata
  customerId: string;       // existing customers(id) row, or freshly created
  email: string;
  stripeSubscriptionId: string;
  stripeCustomerId: string;
  monthlyAmountCents: number;
  slidingScaleAttestation: boolean;
}

export interface PromoteTrialResult {
  customerId: string;
  keeperCustomerId: string;
  conversationTurnsTransferred: number;
  memoryRowsTransferred: number;
  costLogRowsTransferred: number;
}

export async function promoteTrialToKeeper(
  opts: PromoteTrialOpts
): Promise<PromoteTrialResult>;
```

**Transaction sequence:**

1. `BEGIN;`
2. `SELECT * FROM trial_keepers WHERE trial_id = $1 AND released_at IS NULL FOR UPDATE;`; lock + verify trial active.
3. `INSERT INTO keepers (customer_id, companion_name, companion_assigned, ...) VALUES ($customerId, ...) ON CONFLICT DO NOTHING;`; create keeper if not exists.
4. `INSERT INTO harbor_subscriptions (...);`: record subscription audit.
5. `UPDATE keepers SET subscription_status='active', stripe_subscription_id=$stripeId, ...`; link to Stripe.
6. `UPDATE companion_turns SET keeper_id=$customerId, trial_id=NULL WHERE trial_id=$1;`; transfer conversation. **CHECK constraint requires exactly one of (keeper_id, trial_id) to be set; the SET clause above swaps them in one statement.**
7. `UPDATE keeper_memory SET keeper_id=$customerId, trial_id=NULL WHERE trial_id=$1;`; transfer memory.
8. `UPDATE trial_keepers SET released_at=NOW() WHERE trial_id=$1;`; soft-delete trial.
9. `COMMIT;` (or `ROLLBACK` on any failure → no orphans, can retry).

**Race protection:** the `FOR UPDATE` on step 2 prevents two concurrent webhook deliveries (Stripe sometimes retries) from double-promoting. The subsequent `released_at IS NULL` check is the canonical "already promoted?" signal.

**Cost log:** `companion_cost_log.turn_id` cascades from `companion_turns.turn_id`, so cost rows are automatically retained when turns flip from trial_id to keeper_id.

**Acceptance criteria:**
- 0 orphaned turns/memory after a successful promotion.
- Promotion is idempotent: a duplicate webhook delivery is a no-op (returns the existing keeper).
- A failure mid-promotion leaves the trial intact (no half-done state).
- The keeper's first companion turn after promotion sees the same memory rows as before.

**Tests (target: 12 cases):**
- Happy path: trial with N turns + M memory rows promotes to keeper with N turns + M memory rows.
- Idempotency: re-deliver same Stripe webhook, no duplicate `harbor_subscriptions` row.
- Concurrent webhooks: two parallel promotions for same trial, only one wins (exactly one resolves with success; the other returns the same keeper).
- Empty trial (0 turns): still creates keeper row + subscription audit.
- Sacred memory partition: rows with `partition='sacred'` transfer correctly.
- Already-released trial: returns 404 / explicit error, doesn't create orphan.
- Stripe customer creation failure mid-flow: trial + companion data unchanged.
- Subscription audit insert failure: full transaction rolls back.
- companion_cost_log rows preserved: query before + after, count + sum match.
- Sliding-scale attestation flag: boolean + amount cents both persisted.
- Magic link / first-login flow after promotion: keeper sees their conversation seamlessly.
- Promotion from multiple parallel trials of the same email (edge): each promotes separately or merges (decision: merge by email if customer_id matches).

### 5.2 Subscription billing

**Pricing (canonical, aligned with `web/views/pricing.ejs`):**
- Monthly: **$39 standard, sliding to $19 on the keeper's word** (`STRIPE_HARBOR_PRICE_ID_STANDARD` for $39 and `STRIPE_HARBOR_PRICE_ID_SLIDING_FLOOR` for $19).
- Sliding scale attestation: the keeper sees the standard $39 price and a small affordance to choose any monthly amount from $39 down to $19 ("Choose any amount from $39 down to $19, whatever fits your practice. Your word is enough."). Implementation: Stripe Checkout `mode: 'subscription'` with a custom price tier picked from a small set, OR a custom-price flow if Stripe Billing is preferred. Recommendation: ship with two fixed price IDs ($39 and $19) for simplicity in V1; revisit a continuous slider once attestation behavior is observed.
- The conversation, the build, and the private preview are all part of the offering before the subscription begins. The subscription begins at the "make it public" moment (§ pivot at top of file).

**Stripe products to create (manual step):**
- 1 product: "CAS Harbor Subscription"
- 2 prices on that product: $39 standard and $19 sliding-scale floor, both monthly recurring.
- 1 webhook endpoint pointing at `/webhooks/stripe` (existing); add `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted` event types.

**Routes (added to `web/src/routes/lighthouse.ts`):**
- `GET /lighthouse/upgrade`: form with the 4 price options + sliding-scale attestation copy.
- `POST /lighthouse/upgrade/checkout`: creates Stripe Checkout session with `metadata.cas_harbor_trial_id`, redirects to Stripe.
- `GET /lighthouse/upgrade/success`: placeholder page after Stripe redirect; backend promotion happens via webhook so this page just confirms.
- `GET /lighthouse/account`: keeper's subscription dashboard: view status, change tier, cancel, change card.

**Webhook hooks (in `web/src/routes/webhooks.ts`, existing file):**
- On `checkout.session.completed`: call `promoteTrialToKeeper()`.
- On `customer.subscription.updated`: update `keepers.subscription_status` + `harbor_subscriptions` row.
- On `customer.subscription.deleted`: update both to `canceled`. Keeper retains read-only access to their conversation; new turns return a "subscription required" affordance.

**Acceptance criteria:**
- 7-day trial counter starts at first /lighthouse visit.
- Day 7 + 1 second: cookie still works for read-only; POST /turn returns 402 with upgrade link.
- After Stripe success, keeper can resume turn-taking immediately (no manual login).
- `harbor_subscriptions.status` mirrors Stripe's status field.
- Cancel via /account: subscription_status='canceled' but companion data + memory persist (do not auto-delete on cancel).

**Tests (target: 18 cases):**
- Stripe Checkout session creation: includes correct metadata, correct price ID per tier choice.
- Webhook `checkout.session.completed`: invokes promotion, marks subscription active.
- Webhook `customer.subscription.updated` (status change): updates keeper subscription_status.
- Webhook idempotency: replayed event is no-op.
- Sliding-scale attestation: persisted in keeper row + harbor_subscriptions.
- 7-day trial expired + POST /turn: 402 response with upgrade link.
- Cancel via /account: status flips, conversation read-only, memory retained.
- Resume after cancel + re-checkout: subscription_status flips back to active, no new keeper row created.
- Webhook signature validation (existing infra coverage; verify P2 routes use it).
- Failed Stripe API call (network): rollback, retry-safe.
- Multiple sliding-scale prices: each price ID maps correctly to amount.
- Admin can see /admin/harbor with subscription status column added.
- Account page: shows next billing date, current tier, sliding-scale checkbox.
- Magic-link reuse from existing CAS code (do NOT re-implement).
- Email after promotion: send a `harbor_welcome` template that names Haven + sets expectations.

### 5.3 v2 compose handoff

**Purpose:** when the keeper has reached the SKETCH stage of the working arc and explicitly says "yes, build it" (or Ocean clicks "Build now" in /admin/harbor), extract a structured spec from the keeper's conversation + memory and dispatch it to the existing v2 build pipeline.

**Trigger paths:**
- Keeper-initiated: keeper clicks a "Build my website" button in /lighthouse/table that appears when handoff-readiness criteria are met (turn count ≥ 8 AND last companion turn matches handoff phrase OR keeper explicitly affirms).
- Ocean-initiated: Ocean clicks "Build now" in /admin/harbor row action menu.

**Service:** `web/src/services/spec_extraction.ts`

```typescript
export interface ExtractSpecOpts {
  pool: Pool;
  keeperCustomerId: string;
  fallbackTier?: 'build' | 'grow' | 'scale';  // default 'build' / harbor
}

export interface ExtractSpecResult {
  spec: V2BuildSpec;
  warnings: string[];
}

export async function extractSpecFromConversation(
  opts: ExtractSpecOpts
): Promise<ExtractSpecResult>;
```

**Extraction strategy:**
1. Pull all `keeper_memory` rows for the keeper, partitioned by category (audience, practice, ritual, fear, fact, etc.).
2. Pull last 30 `companion_turns` for the keeper, both directions.
3. Single Opus call with a structured-output prompt: "Given this keeper's conversation + memory, produce a v2 spec JSON for compose/build.py. Use the keeper's exact words for hero copy. Use audience facts for navigation. Include warnings for any uncertain decisions." Returns JSON with `archetype`, `pages`, `data_models`, `seed`, `design`, etc.
4. Validate the spec against the v2 schema (the v2 compose input schema) before persisting.
5. Return `{spec, warnings}`. Caller persists the spec to a `builds` row + dispatches via existing queue.

**Reuses:** `compose.intake.translate_v1_spec()` if the extracted spec lands in v1 shape, verify shape first.

**Trigger handling (route):** `POST /lighthouse/build` (keeper) and `POST /admin/harbor/:trial_id/build` (Ocean).

```typescript
router.post('/build', requireSubscribedKeeper, validateCsrfToken, async (req, res) => {
  const keeperId = req.session.customerId;
  const { spec, warnings } = await extractSpecFromConversation({
    pool, keeperCustomerId: keeperId,
  });
  const buildId = await dispatchBuild(spec, keeperId);
  await pool.query(
    `UPDATE keepers SET handoff_build_id=$1, handoff_at=NOW() WHERE customer_id=$2`,
    [buildId, keeperId]
  );
  // Email keeper: "Your website is being built. You'll hear back in 1 to 2 business days."
  res.json({ buildId, warnings });
});
```

**Acceptance criteria:**
- Spec validates against v2 compose schema before queue dispatch.
- Warnings (uncertain hero copy, missing about page, etc.) surface to admin and to the keeper as "I made some guesses; you'll get to revise."
- Hand-off does not re-trigger if `handoff_build_id` is already set (idempotent button).
- v2 pipeline produces a real Railway-deployed website that matches the keeper's working session.

**Tests (target: 14 cases):**
- Memory + conversation → valid v2 spec for each archetype (9 archetypes × 1 baseline keeper-shape each = 9 cases).
- Empty conversation: throws or returns minimal scaffold (open decision).
- Sacred-mode memory: filtered out before extraction.
- Spec validation failure: extraction returns error, no build dispatched.
- Idempotent retrigger: same handoff_build_id returned, no second build.
- Opus call failure: returns clear error to caller; keeper sees "Try again in a moment" affordance.

### 5.4 Day-7 conversion letter

**Purpose:** for trials that reach day 7 with 8+ turns and haven't upgraded, send one Opus-quality letter that reflects what they shared and gently invites the build. Single touch; no follow-up sequence.

**Cron:** runs daily at 10am UTC (configurable). Uses either:
- **Option A (recommended):** Node `setInterval` in `day7_letter.ts` (under `web/src/cron/`), started in `server.ts` after migrations. Survives restarts via DB-backed claim (`day7_letter_sent_at IS NOT NULL` check).
- **Option B:** pg_cron extension. Requires Postgres extension on Railway managed Postgres (verify supported); cleaner but introduces an infra dependency.

For Phase 2 ship Option A; revisit in Phase 4 when scale demands.

**Selection query:**

```sql
SELECT trial_id, companion_name, started_at, last_seen_at,
       (SELECT COUNT(*) FROM companion_turns WHERE trial_id = tk.trial_id) AS turn_count
FROM trial_keepers tk
WHERE released_at IS NULL
  AND started_at < NOW() - INTERVAL '7 days'
  AND started_at > NOW() - INTERVAL '14 days'  -- catch-up window if cron missed days
  AND day7_letter_sent_at IS NULL              -- column on trial_keepers (add)
HAVING turn_count >= 8
LIMIT 50;  -- per-day cap to keep Opus cost bounded
```

(Schema requires `ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS day7_letter_sent_at TIMESTAMP;`.)

**Letter generation (`services/conversion_letter.ts`):**

```typescript
export interface GenerateLetterOpts {
  pool: Pool;
  trialId: string;
}

export interface GenerateLetterResult {
  to: string;          // empty string if no email captured (skip send)
  subject: string;
  body: string;
  costMicros: number;
}

export async function generateConversionLetter(
  opts: GenerateLetterOpts
): Promise<GenerateLetterResult>;
```

**Prompt structure (Opus):**
- System: brand voice rails (no em dashes, written-letter register, names Haven, points outward).
- User: "Here's what Haven remembers about this keeper: [memory snapshot]. Here's the last 6 turns of conversation: [transcript]. Write a 250-300 word letter from Ocean (the harbor master) inviting them to make their site public. Use their exact words where natural. Mention what they shared specifically. Close with a clear $39 offer (sliding to $19 on their word). No sales pressure. No urgency."

**Email problem:** trial cookies don't capture emails. So the letter has nowhere to send. Two options:
- **Defer until the keeper provides email:** add an "if you'd like a copy of this conversation by email, leave your address" affordance during the conversation. Optional; keeper can decline.
- **In-conversation prompt:** Haven gently asks at day-6 mark if there's an email she should capture so Ocean can follow up. Persona update.

**MVP path:** capture email via an in-conversation Haven prompt at day 5-6 ("Want me to ask Ocean to write you in a few days? If yes, where?"). Persisted to `trial_keepers.email_captured` (new nullable column).

**Acceptance criteria:**
- Cron runs idempotently; multiple invocations on the same day don't double-send.
- Per-day cap prevents Opus cost runaway.
- Letter passes brand-voice lint (no em dashes, "website" not "app", review window 1-2 business days if mentioned).
- Day-7 letter linkable from /admin/harbor row.
- Tests cover the cron query window logic, letter generation, email send pathway with mocked Resend.

**Tests (target: 10 cases):**
- Selection query honors all 5 conditions (released_at NULL, started_at window, turn count, day7 not sent).
- Per-day cap of 50 honored.
- Letter generation: brand-voice lint passes (no em dashes).
- Letter generation: includes keeper's exact words from conversation.
- Email-not-captured: skip send, log `[day7] no email for trial=...`.
- Email captured: send via Resend, mark day7_letter_sent_at = NOW().
- Resend send failure: row NOT marked sent (retryable).
- Multiple cron runs same day: idempotent.
- Catch-up window: trials at day 8-14 still receive (overdue but not lost).
- Trial that converted between cron runs: NOT sent (released_at IS NOT NULL filter).

### 5.5 Crisis email plumbing

**Purpose:** wire `runtime.notifyHarborMaster()` from Phase 1's "log only" stub into a real ADMIN_EMAIL alert via `services/email.ts`.

**Template:** new `crisis_alert` template in `services/email.ts`.

```
Subject: [HARBOR CRISIS] {category} flagged on trial {trialId}
Body:
A keeper at the keeper's table just sent a message that triggered the
crisis rail with category(s): {categories}.

Trial id: {trialId}
Keeper id: {keeperId} (if applicable)
Time: {timestamp}

The companion responded with the crisis script template (988 + findahelpline.com).

You can view the conversation at: {origin}/lighthouse/table?dev_trial={trialId}

This alert does NOT include the keeper's text. Look up via SQL only if there is a real reason.
```

**Acceptance criteria:**
- Real Resend send with `category=crisis_alert` (so it shows up in /admin/emails filter).
- Email body NEVER includes the keeper's text.
- Async fire-and-forget: a Resend failure does not block the user-facing turn.
- `email_log` row created so admin can audit which alerts fired.

**Tests (target: 5 cases):**
- Crisis flagged → mockSendEmail called with correct template + categories.
- Email body does NOT contain `keeperText`.
- Resend failure → turn still completes successfully.
- email_log row created with template='crisis_alert'.
- Multiple categories → all listed in subject + body.

### 5.6 Brand-softened landing coordination

**Status:** the homepage workstream owns customer-facing copy + visual chassis. This Phase 2 plan owns the backend wiring required to make those pages functional.

**Backend wiring required (this lane):**
- `GET /begin` → render the new "begin" page (form variant or button → /lighthouse).
- Public route `GET /the-companion` → static informational page; no backend.
- Public route `GET /recent-harbors` → reuse existing gallery infrastructure or new query against builds with `gallery_opted_in=true` AND `handoff_build_id IS NOT NULL`.
- Public route `GET /about` → static.
- Public route `GET /` → updated index.ejs from the homepage workstream; ensure data dependencies resolved (TCO, gallery, FAQ).
- Updates to `app.ts`: route mounts + `pageMesh` flag adjustments.

**Coordination protocol:**
- The homepage workstream merges to `master` first.
- The Harbor Phase 1 work rebases on master.
- Backend wiring lands as a follow-up commit referencing the new EJS views.

**Acceptance criteria:**
- All 5 customer pages return HTTP 200 with rendered content.
- /lighthouse remains gated by `CAS_HARBOR_ENABLED` (no inadvertent prod exposure).
- Existing one-time-tier flow at `/payment` continues to work for any in-flight customers (verified via Vitest).

---

## 6. Test plan summary

| Component | Existing tests | New tests planned | Notes |
|---|---|---|---|
| Trial-keeper promotion | 0 | 12 | Transaction + concurrency + idempotency |
| Subscription billing | (CAS one-time tested) | 18 | Stripe Checkout + webhooks + sliding scale |
| v2 compose handoff | (compose tested) | 14 | Spec extraction + idempotent dispatch |
| Day-7 conversion letter | 0 | 10 | Cron + letter gen + email capture |
| Crisis email plumbing | (Phase 1 logs only) | 5 | Wire to existing email.ts |
| Admin harbor inbox v2 | 10 (Phase 1 baseline) | 8 | Add filters + handoff button |
| **Total new** | | **67** | |

Plus **brand-voice lint** every Phase 2 customer-facing surface (templates, account page, conversion letter prompt).

---

## 7. Phasing and order

Phase 2 itself is large; ship in 4 slices.

### Slice 2A: Trial-to-keeper promotion + subscription billing (~1-2 weeks)

- Schema additions (§ 4 above).
- `keeper_promotion.ts` service + 12 tests.
- Stripe products created (manual step).
- Upgrade (Stripe Checkout) route + UI.
- Webhook handlers in existing `web/src/routes/webhooks.ts`.
- /lighthouse/account dashboard.
- Acceptance: paid subscription end-to-end on staging Stripe test mode.

### Slice 2B: Crisis email plumbing + day-7 letter (~3-5 days)

- `notifyHarborMaster()` wires to email.ts.
- `services/conversion_letter.ts` + Opus prompt.
- `cron/day7_letter.ts` + start in server.ts.
- Email capture persona update.
- Acceptance: a synthetic 8-turn trial gets a real letter delivered to a test inbox.

### Slice 2C: v2 compose handoff (~1-2 weeks)

- `services/spec_extraction.ts` + 14 tests across 9 archetypes.
- Build handoff route.
- Admin "Build now" action in `/admin/harbor`.
- Hand-off-readiness criteria pickled for keeper-side button visibility.
- Acceptance: a real test keeper conversation produces a real Railway-deployed website with the correct archetype + content.

### Slice 2D: Brand-softened landing wiring (~3-5 days, blocked on the homepage workstream)

- Coordinate with the homepage workstream's merge.
- Public routes + content fetch.
- Acceptance: 5 marketing pages live, /lighthouse + /admin/harbor still functional.

## 8. Acceptance criteria (Phase 2 GA)

Phase 2 is "shipped" when:

1. ✅ A new visitor can land on `/`, click "begin," reach `/lighthouse`, talk for 8 turns, hit a "Build now" button, complete Stripe Checkout, see their conversation seamlessly preserved, watch their website build, and receive a `preview_ready` email; all without manual intervention.
2. ✅ Ocean can see all active trials + keepers + their subscription status + handoff status at `/admin/harbor`.
3. ✅ A keeper at day 7 with 8+ turns receives an Opus-quality conversion letter to their captured email.
4. ✅ A keeper who triggers crisis detection causes Ocean to receive a real ADMIN_EMAIL alert.
5. ✅ Brand-voice lint passes on every Phase 2 customer-facing surface.
6. ✅ All 67 new test cases green; existing 931 tests still green.
7. ✅ Admin /admin/harbor handoff button + dispatch works on staging.
8. ✅ Cancel-anytime works: status flips, conversation persists, no silent data deletion.
9. ✅ A real-keeper end-to-end run is reviewed and approved before enabling `CAS_HARBOR_ENABLED=true` in production.

---

## 9. Risks and mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Stripe webhook delivery failure (network) | High | Use Stripe's built-in retry; idempotent `promoteTrialToKeeper()` with `FOR UPDATE` lock. |
| v2 spec extraction produces low-quality archetype match | Medium | Per-archetype unit tests with golden specs; warnings surface to admin + keeper. |
| Day-7 cron sends duplicate letter | Medium | `day7_letter_sent_at` column + `IS NULL` filter; Resend idempotency keys. |
| Opus cost runaway on letters or extraction | Medium | Per-day cap (50 letters); cost log + dashboard alert. |
| Keeper data lost on Stripe cancel | High | Never auto-delete; keep all turns + memory; surface read-only mode after cancel. |
| Sacred memory leaks into extraction | High | Test asserts sacred rows filtered before Opus call; partition firewall in Phase 4. |

---

## 10. Open follow-ups (out of Phase 2 scope but worth noting)

- pgvector migration, when total memory rows exceed ~50K.
- Sacred-mode firewall: Phase 4.
- End-of-year letters: Phase 3.
- Site siblings collective contributions: Phase 4.
- Voice intake (Whisper / Deepgram): Phase 3.
- Couples / shared accounts: open question.
- Dedicated lantern (per-keeper LoRA): Phase 5+.

---

## 11. References

- `docs/CAS_HARBOR_PHASE_1_TECH_PLAN.md`: predecessor.
- `docs/CAS_LIVING_PLATFORM_TECHNICAL_ARCHITECTURE.md`: system topology.
- `web/src/db/schema.sql`: Phase 1 schema (extend in § 4 above).
- `web/src/companion/runtime.ts`: turn lifecycle (extends in § 5.5 for crisis email).
- `web/src/services/email.ts`: Resend wrapper (extends in § 5.5 for crisis_alert + § 5.4 for conversion_letter templates).
- `web/src/routes/admin.ts` - `/admin/harbor` (extended with filters + handoff button; goal 7 in § 1).
- `compose/build.py`: v2 build pipeline (consumed in § 5.3 spec dispatch).

---

*Plan written 2026-04-27.*
