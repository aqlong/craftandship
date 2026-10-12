# Senior Engineer Review Gate for Preview → Live Flow

> Historical planning brief. The shipped design is described under "SE Review Gate" in `CLAUDE.md` and in the "Human review gate" section of `ARCHITECTURE.md`. Two statements below no longer describe the code: `COORDINATOR_AUTO_APPROVE_PREVIEW` does not exist any more (the coordinator never promotes a build and has no method or permission that approves one), and the scope line "no automated review tooling" belongs to this brief only, since an automated review mode (`CAS_HARBOR_AUTO_REVIEW`) was added in June 2026 (see the note under "Craft Quality Policy" in `CLAUDE.md`). A third statement no longer holds: the "Reviewed by Ocean ✓ (date)" badge asked for in the Goal section does not exist. The status page now shows a badge that says "Reviewed", with no reviewer named, because the code records a verdict (`review_status`, `reviewed_by`, `reviewed_at`) and not which person or process gave it; the customer copy says "Every website is reviewed before it goes live" for the same reason.

## Problem

CLAUDE.md stated the Craft & Ship brand promise (the wording there has since been revised):

> Every app receives a senior engineer review before delivery. This commitment is non-negotiable. If demand exceeds review capacity, raise prices or hire more senior engineers. Never skip, shorten, or automate away the human review. The Craft promise is the brand.

Today this is **aspirational, not structural**. `POST /builds/:buildId/approve` in `web/src/routes/builds.ts` lets the customer self-promote a preview build to `live` with only `requireAuth` + CSRF + ownership checks. There is no admin/SE checkpoint anywhere in the flow. A customer can click "Approve and Go Live" at any time and the worker will happily flip the status.

This is a brand-integrity bug: the human review layer is the core of the product promise, not a feature to trim.

## Goal

Structurally enforce the senior engineer review gate so the brand promise is true by construction, not by good intentions.

**Required behaviors:**

1. **Customer still sees preview and can request revisions.** The revision flow (`POST /builds/:buildId/revision`) must stay intact, customers loop with Ocean until the app is right.
2. **Final promotion to `live` requires Ocean's signoff.** The existing customer-facing "Approve and Go Live" button on the status page must be blocked until an admin review has been recorded. Whether "blocked" means (a) hidden/disabled, (b) visible but rejected server-side with a clear message, or (c) replaced with a "Waiting for senior review" label is an open question (see Design Questions below).
3. **"Reviewed by Ocean ✓ (date)" badge** appears on the status page once reviewed, so the customer can *see* the brand promise being honored.
4. **Admin review UI** lives at `/admin/builds` (extending existing) or a new `/admin/reviews` page. Admin (Ocean) can: view preview apps awaiting review, record a review (approve/request-changes), optionally attach notes, and either promote to live directly OR unlock the customer's Approve button.

## Constraints

### Must coexist with existing infrastructure

- **`COORDINATOR_AUTO_APPROVE_PREVIEW` env var** (default false). Already referenced in `coordinator/actuators.py` `approve_build()`, when false, coordinator preview→live promotions route through the `approval_requests` table and `/admin/approvals` page rather than auto-promoting. The new SE review gate must interact cleanly with this: does a coordinator auto-approval satisfy the review gate, or does it still require Ocean's signoff? The answer likely depends on how strict the brand promise is.
- **`approval_requests` table + `/admin/approvals` page** already exist for coordinator agent approvals. Should the new SE review reuse this table with a new `type`, or is a separate `build_reviews` table cleaner?
- **`is_revision` + `parent_build_id` lineage** on the builds table. The existing revision loop uses a recursive CTE to count revisions against `TIER_REVISION_LIMITS`. A review on build N may or may not carry over to a revision (build N+1 descended from N).

### Backwards compatibility

- Existing `live` builds in production must not break. Schema additions are nullable; old live rows have no review record. The status page for legacy live builds must degrade gracefully (either hide the badge or show "Reviewed (grandfathered)"; TBD).
- Schema migration must be additive only. `scripts/check_destructive_ddl.py` runs via `.githooks/pre-commit` and will reject any `DROP`/`ALTER COLUMN TYPE`/`TRUNCATE` in `web/src/db/schema.sql` without a `-- SAFE:` acknowledgment. New columns must be `NULL`able with sensible defaults.

### Test coverage (non-negotiable)

Tests in `web/tests/unit/routes.builds.test.ts` must cover **both** paths:

1. **Blocked customer-approve path**: Customer tries `POST /builds/:buildId/approve` on an unreviewed preview → rejected with clear error (HTTP status and message TBD during plan).
2. **Admin-approve path**: Admin records a review via the new admin route → either auto-promotes OR unlocks customer approval, depending on the two-step vs one-step decision. Test must exercise the full happy path end-to-end.

Existing tests covering the current approve behavior must be updated (not just appended to) so the green test suite reflects the *new* contract, not a vestige of the old one.

## Key Files to Investigate

- **`web/src/routes/builds.ts`**: existing `/builds/:buildId/approve` handler, `/builds/:buildId/revision` handler, `/builds/:buildId/rebuild` handler. Need to read the full approve handler to understand the current contract before modifying it.
- **`web/src/routes/admin.ts`**: admin UI routes, `requireAdmin` middleware, existing `/admin/builds`, `/admin/approvals` patterns, JSON polling API conventions (e.g. `/admin/api/builds`).
- **`web/src/middleware/auth.ts`**: `requireAdmin()` implementation (checks `req.session.customerEmail === process.env.ADMIN_EMAIL`), CSRF token helpers.
- **`web/src/db/schema.sql`**: `builds` table (to add review columns or new table), `approval_requests` table (to understand existing review-like pattern), any existing partial indexes or CHECK constraints to follow conventions.
- **`web/views/status.ejs`**: where the "Reviewed by Ocean ✓" badge needs to appear. Must degrade when review fields are null for legacy builds.
- **The status page poller** (client-side polling on the build status page, no full page reload; `handlePreview()` and `handleLive()` inject UI into the DOM). The badge and the blocked-approve state must be injected by the poller, not require a page reload.
- **`web/views/admin-builds.ejs`** + possibly a new **`web/views/admin-reviews.ejs`**; review queue UI.
- **`coordinator/actuators.py`**: `approve_build()` method. Need to understand exactly how it handles the `COORDINATOR_AUTO_APPROVE_PREVIEW` toggle today, and decide whether SE review gate short-circuits coordinator auto-approval entirely or layers on top.
- **`web/tests/unit/routes.builds.test.ts`**: existing approve/revision test shapes (`vi.mock('../db')`, supertest import from `src/app.ts`), including session setup for customer vs admin.

## Open Design Questions (the plan must resolve these)

1. **Schema shape: new table or new columns on `builds`?**
   - **Option A (columns on builds):** `reviewed_at TIMESTAMP`, `reviewed_by VARCHAR(255)`, `review_notes TEXT`, `review_status VARCHAR(20)` (e.g. `pending | approved | changes_requested`). Simplest; one query to render status page. Awkward if we later want multiple reviews per build (revision loop).
   - **Option B (new `build_reviews` table):** `id UUID PK, build_id FK, reviewer_email, status, notes, created_at`. Cleaner lineage; natural fit if revisions get re-reviewed. Extra join on hot path.
   - **Option C (reuse `approval_requests`):** Add a `type='senior_engineer_review'` row. Unifies with coordinator approvals. Semantic mismatch; approval_requests is a coordinator queue, not a build-level fact.
   - Plan must recommend one with reasoning.

2. **One-step or two-step promotion?**
   - **One-step:** Admin clicks "Approve" in admin UI → build immediately goes to `live`, customer is notified. Customer never sees an "Approve" button for reviewed builds.
   - **Two-step:** Admin clicks "Mark Reviewed" in admin UI → build stays in `preview` but a `reviewed_at` is recorded → customer's "Approve and Go Live" button unlocks → customer clicks to promote. This preserves customer agency (they decide when to flip the switch for their business) and matches the existing dashboard flow more closely.
   - Two-step is probably stronger for the brand promise; Ocean reviews, customer approves. But it's more moving parts.

3. **Interaction with `COORDINATOR_AUTO_APPROVE_PREVIEW`:**
   - Does a coordinator auto-approval satisfy the SE review gate, or not? If yes, the env var becomes "coordinator can auto-review on Ocean's behalf when it's 3am"; dangerous. If no, `COORDINATOR_AUTO_APPROVE_PREVIEW` is effectively dead code (the coordinator can queue approval_requests but can never actually promote). The cleanest answer is probably: **coordinator auto-approve no longer promotes to live; it can only mark the build as "ready for Ocean's review" and queue the approval request.** Need to confirm this is consistent with the current coordinator's behavior and document it clearly.

4. **Retroactive backfill:**
   - Existing `live` builds predate the review gate. Options: (a) NULL review fields, badge hides, no "Reviewed" label shown; (b) backfill all pre-existing live builds with `reviewed_by='Ocean (grandfathered)'` and `reviewed_at=created_at`; (c) show "Delivered before review tracking" label.
   - (a) is the safest default but creates visible inconsistency on the dashboard (some live apps show the badge, some don't). (b) is cleanest for UX but requires a data migration that's hard to reverse.

5. **Revision invalidation:**
   - If a customer requests a revision *after* Ocean has reviewed build N, build N+1 is a fresh child build. Does Ocean need to re-review? Probably yes; that's the whole point of review. But the plan should specify: revisions inherit nothing about review state; each build (including revisions) gets its own review record.

6. **Notifications:**
   - Does the admin UI need to notify Ocean when a new preview is awaiting review? Email? Status page ping? Coordinator alert? For the initial cut, an admin dashboard badge is probably enough; email can follow.

7. **Failure modes:**
   - What if Ocean reviews and then the customer's Approve-to-go-live click hits a DB error? What if the admin accidentally approves the wrong build (can they un-approve before the customer promotes)? What's the customer-facing message when they try to click Approve on an unreviewed build? These UX details belong in the plan.

## Scope Boundaries (explicitly out of scope for this plan)

- **No change to the build pipeline**; this is purely a preview→live gate layered on top of existing pipeline output.
- **No new review-tier/pricing changes**: pricing is set; this plan enforces what pricing already promises.
- **No automated review tooling**: the gate is manual by design. We're not building Claude-as-reviewer; we're protecting the space for a human to do the review.
- **No audit log for reviews**: nice to have but not required for v1. The `reviewed_by + reviewed_at` fields on the build/review record are enough for v1.
- **No review SLA enforcement** (e.g. auto-alert if a build sits in preview for 48h without review). Could come later; out of scope here.
- **No changes to `/admin/approvals`** (the coordinator agent approval queue), unless the plan decides Option C (reuse approval_requests), in which case this becomes the review UI.

## Success Criteria

1. A customer *cannot* promote their build from preview to live without an admin review being recorded in the database. Verified by unit test.
2. After Ocean records a review, the customer's status page immediately (via reactive polling) shows either the unlocked Approve button (two-step) or the live banner (one-step), plus the "Reviewed by Ocean ✓ (date)" badge.
3. Existing live builds load without errors; backward-compat migration confirmed by local test against a DB containing pre-migration rows.
4. `COORDINATOR_AUTO_APPROVE_PREVIEW` behavior is documented in the new plan and in the CLAUDE.md update, with no silent change in meaning.
5. All existing web tests still pass; at least two new tests added (blocked customer path, admin review path) in `web/tests/unit/routes.builds.test.ts`. A new `web/tests/unit/routes.admin-reviews.test.ts` may also be warranted if a new route file is introduced.

## References

- CLAUDE.md "Craft Quality Policy" section: the brand promise text itself
- CLAUDE.md "Admin access" section; existing patterns this plan must match
