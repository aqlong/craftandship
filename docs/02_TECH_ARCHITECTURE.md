# Craft & Ship: Technical Architecture

> Historical document from May 2026; the system is no longer developed and the site is still online.

**Version:** 0.3 (updated to reflect actual implementation as of March 29, 2026)
**Last updated:** March 29, 2026

> **2026-05-01 update: tier kill.** This document describes the pre-Harbor multi-tier architecture (Build/Grow/Scale; `/payment/:buildId`; tier-varying review SLAs; `validateSpecForTier`; `BETA_MODE`). All of that was retired 2026-05-01: Harbor sells a single subscription product, the `/payment` route was deleted, the beta config module is gone, and `TIER_PRICES` / `TIER_MONTHLY_PRICES` / `TIER_LIMITS` / `TIER_REVISION_LIMITS` were stripped from `web/src/types/index.ts`. The `BuildTier` type alias is kept for `builds.tier` DB-column type-safety only; the column is no longer rendered on customer surfaces. References below to "Build tier" / "Grow tier" / "Scale tier" / "tier limits" / "tier-based timeouts" describe the historical architecture, not the current shipping behavior (the worker still keys its timeout on `builds.tier`, `_TIER_TIMEOUTS` in `queue/worker_pg.py`, and every row is `build`, so every build gets the 60-minute budget). The pipeline stages below (Opus code generation, Claude code review, 19 smoke tests, up to 3 concurrent builds) are the March 2026 v1 design; the default engine is now v2, a deterministic compose with no model call in the build path, and the worker's concurrency is `MAX_CONCURRENT_BUILDS` (default 1). Current behavior is described in `CLAUDE.md` and `docs/internals/`. Canonical pricing surface: `web/views/pricing.ejs`.

---

## System Overview

Craft & Ship is a pipeline with seven stages: Intake > Validate > Build > Review > Test > Deploy > Monitor. Each stage is a separate module. A PostgreSQL-backed worker queue processes builds concurrently (up to 3).

```
Customer
  |
  v
[Intake Conversation] -- Claude API (Opus 4.6) --> Structured JSON Spec
  |
  v
[Spec Validator] -- Pure Python (no API call) --> Validated Spec or Rejection
  |
  v
[Build Queue] -- PostgreSQL SKIP LOCKED, concurrent (max 3)
  |
  v
[Builder] -- Claude API (Opus 4.6, single or multi-pass) --> Generated App Code
  |
  v
[Code Review] -- Claude API (Sonnet) --> Non-blocking quality check
  |
  v
[Test Runner] -- 19 smoke tests (Playwright + HTTP) --> Pass/Fail
  |                                                        |
  | (if fail: up to 4 self-correction attempts via fix.py) |
  v
[Deployer] -- Railway GraphQL API --> Live URL + health check
  |                                     |
  | (if fail: up to 3 deploy-fix attempts via deploy_fix.py)
  v
[QA Agent] -- Claude Sonnet generates + runs targeted Playwright tests (non-blocking)
  |
  v
[Health Monitor] -- Every 15 min, tiered diagnostics --> Alerts/Auto-fixes
  |
  v
[Ops Agent] -- Every 30 min, playbook-based self-healing --> Auto-fixes/Escalation
```

---

## Stage 1: Intake Conversation

**Model:** Claude Opus 4.6 via API
**Purpose:** Transform a vague app idea into a precise, buildable JSON spec

### Conversational Design Principles

1. Open with "what does your app need to do?" not "what do you want to build?" The first question grounds the conversation in outcomes, not features.

2. Ask about users next: "Who will use this, and what do they need to accomplish?" This prevents building for an imaginary audience.

3. Push back on scope: The intake must detect when a description exceeds what the platform can build and either simplify or decline. Explicit boundaries (page count, user roles, tables, integrations) are enforced by the spec validator.

4. Confirm understanding by restating: "So you need a booking tool where customers can pick a date, enter their info, and you get an email notification. The customer sees a confirmation page. Is that right?"

5. Output a structured JSON spec, not prose.

### Intake Logging (Implemented)

Full conversation transcripts stored in `intake_messages` table (build_id FK, role, content, created_at). Tier, build outcome, and spec available via JOIN to builds table. Analytics endpoint at GET /ops/api/v1/intake-analytics provides per-conversation metrics and aggregates. GDPR deletion implemented via cascade in account deletion route. Customer data export includes all intake conversations.

### Human Review Gate (planned here; shipped after the build instead)

This section records a plan that was not built as written: a Senior Web Craftsman review of the spec **before** the build queue, through an `awaiting_human_review` status. No such status exists (`builds.status` in `web/src/db/schema.sql` allows `intake`, `payment_pending`, `queued`, `building`, `testing`, `deploying`, `preview`, `live`, `failed` and `abandoned`). The review that shipped happens after the build: a finished build parks in `preview` with `review_status = 'pending'` until a reviewer decides at `/admin/reviews` (see "SE review gate" in `CLAUDE.md` and the "Human review gate" section of `ARCHITECTURE.md`). The plan was that the reviewer either:

1. Approves the spec (build proceeds)
2. Requests clarification (reaches out to the customer on their preferred text channel, e.g. iMessage, WhatsApp, Instagram DM, Slack, email, or CAS pipeline chat)
3. Rejects and rewrites (spec returns to intake with reviewer notes)

No review time is given to customers (`docs/BRAND_VOICE.md`, rule 4). The review is async and text-first.

See the "Craft Quality Policy" section of `CLAUDE.md`.

### AI Customer Service (Implemented)

Each deployed app gets a floating AI support chat widget powered by Claude Haiku. System prompt includes the app's spec, feature set, and dynamic pricing info. Auto-growing knowledge base (support_kb table) caches answers with keyword similarity matching. Rate limited to 10 msgs/min per user. Max 2048 tokens per response. Escalation path: AI chat > email support@craftandship.io.

### Spec Format (v0.1)

This is the actual schema produced by `cli/intake.py` and validated by `cli/validate.py`.

```json
{
  "spec_version": "0.1",
  "app_name": "landscaping-task-tracker",
  "description": "Task tracker for a landscaping company",
  "user_roles": [
    {
      "role": "crew_member",
      "description": "Field worker who completes jobs",
      "can": ["view_assigned_jobs", "mark_job_complete", "add_job_notes"]
    },
    {
      "role": "foreman",
      "description": "Supervises crew, updates job status",
      "can": ["view_all_jobs", "edit_job", "manage_crew"]
    }
  ],
  "pages": [
    {
      "path": "/login",
      "title": "Login",
      "purpose": "Authenticate user and redirect by role",
      "auth_required": false,
      "allowed_roles": []
    },
    {
      "path": "/my-jobs",
      "title": "My Jobs",
      "purpose": "List jobs assigned to the current crew member",
      "auth_required": true,
      "allowed_roles": ["crew_member"]
    }
  ],
  "data_models": [
    {
      "name": "User",
      "fields": [
        {"name": "email", "type": "email", "required": true},
        {"name": "password", "type": "password", "required": true},
        {"name": "role", "type": "text", "required": true}
      ],
      "belongs_to": null,
      "has_many": ["Job"]
    },
    {
      "name": "Job",
      "fields": [
        {"name": "title", "type": "text", "required": true},
        {"name": "status", "type": "text", "required": true},
        {"name": "completed", "type": "boolean", "required": true},
        {"name": "scheduled_at", "type": "timestamp", "required": false}
      ],
      "belongs_to": "User",
      "has_many": []
    }
  ],
  "actions": [
    {
      "name": "mark_job_complete",
      "description": "Crew member marks a job as done",
      "trigger": "button_click",
      "role": "crew_member",
      "steps": ["validate ownership", "set completed=true", "redirect to /my-jobs"]
    }
  ],
  "settings": {
    "auth": true,
    "seed_data": true
  }
}
```

**Field type constraints** (enforced by `cli/validate.py`):
- Valid types: `text`, `integer`, `boolean`, `timestamp`, `email`, `password`
- Valid triggers: `form_submit`, `button_click`, `scheduled`
- `app_name` must be kebab-case
- `allowed_roles` must reference roles defined in `user_roles`
- `belongs_to` / `has_many` must reference model names defined in `data_models`

---

## Stage 2: Spec Validation

**Implementation:** Pure Python (`cli/validate.py`) + TypeScript (`web/src/services/spec-validation.ts`) for web-side tier enforcement
**Purpose:** Catch schema errors and enforce tier limits before the expensive build starts

### Validation Rules (enforced by `cli/validate.py`)

**Top-level required fields:** `spec_version`, `app_name`, `description`, `user_roles`, `pages`, `data_models`, `actions`, `settings`

**`spec_version`:** Must equal `"0.1"`

**`app_name`:** Must be kebab-case (letters, numbers, hyphens only)

**`user_roles`:** Non-empty list; each entry requires `role`, `description`, `can` (non-empty list); no duplicate role names

**`pages`:** Non-empty list; each entry requires `path`, `title`, `purpose`, `auth_required`, `allowed_roles`; paths must start with `/` and be unique; if `auth_required: true` then `allowed_roles` must be non-empty; all values in `allowed_roles` must reference defined roles

**`data_models`:** Non-empty list; each entry requires `name`, `fields` (non-empty); each field requires `name`, `type`, `required`; no duplicate model or field names; `type` must be one of: `text`, `integer`, `boolean`, `timestamp`, `email`, `password`; `belongs_to` and `has_many` references must point to defined model names

**`actions`:** Each entry requires `name`, `description`, `trigger`, `role`, `steps` (non-empty); `trigger` must be `form_submit`, `button_click`, or `scheduled`; `role` must reference a defined user role

**`settings`:** Must contain both `auth` and `seed_data` fields

### Usage

```bash
python cli/validate.py specs/my-app-1234567890.json
# VALID - my-app (spec v0.1)
#   2 roles, 5 pages, 3 models, 4 actions

# or on error:
# INVALID - 2 error(s):
#   - pages[2]: allowed_role 'admin' not defined in user_roles
#   - data_models[1]: belongs_to references unknown model 'Organization'
```

---

## Stage 3: Build Queue (Implemented)

**Implementation:** `queue/worker_pg.py`, PostgreSQL-backed with SKIP LOCKED for atomic job claim

### Queue Design

Worker polls the `builds` table every 10 seconds. Jobs claimed atomically via `SELECT ... FOR UPDATE SKIP LOCKED`. A ThreadPoolExecutor runs builds concurrently, one thread each: `MAX_CONCURRENT_BUILDS` (default 1) sets how many, and the claim counts active builds under an advisory lock so two workers cannot exceed it. The oldest queued build is claimed first (`ORDER BY created_at`); the tier only selects the timeout.

**Status flow:** `intake` > `payment_pending` > `queued` > `building` > `testing` > `deploying` > `preview` > `live` (or `failed` at any point; `payment_pending` and `abandoned` belong to the earlier chat-intake flow)

The `preview` status is the enforcement point for the Craft Quality Policy: the worker never writes `live`, and the web app's approve route and the Stripe subscription handler write it only for a build whose `review_status` is `approved`, and the owner's variant of the approve route (`CUSTOMER_APPROVE_SQL` in `web/src/routes/builds.ts`) also requires, for a build descended from a Harbor trial or keeper, that its owner has an active, trialing or past-due subscription; the admin variant (`ADMIN_APPROVE_SQL`) checks neither (see "Build lifecycle" in `ARCHITECTURE.md`). See Stage 1 "Human Review Gate" above.

**Timeouts (historically tier-based):** 60m / 90m / 120m by tier (raised from 45/60/90 on 2026-04-20 after builds hit the 45m ceiling during Anthropic throttle + deploy_fix loops). The only timeout mechanism is a supervisor in `_process_job` that waits on the build thread's future for the tier timeout plus a 30 s grace period; cooperative cancel via `cli.cancellation` stops fix loops and API retries at their next checkpoint, and a thread blocked in a C-level call is abandoned while the supervisor decides the build's fate (the Layer 1 async-exception interrupt was removed; see `docs/internals/worker.md`).

**Retry:** Both transient and code errors auto-retry up to 6x (`_MAX_RETRIES`; `builds.retry_count` is the one counter, shared with the supervisor's free retry and orphan restarts). Code errors clear build artifacts and rebuild fresh. A 2-hour retry budget ends a series of retries early; it is measured from the first attempt of the series on a clock the worker process keeps (`_RETRY_TIME_BUDGET_S`), not from the age of the build row, which can be days old when the build is first queued (see `docs/internals/worker.md`). Test-phase self-correction: up to 4 fix attempts. Deploy-phase: up to 3 fix attempts.

**Build isolation:** Each build gets its own directory (`builds/{app_name}-{build_id[:8]}/`) preventing path collisions during concurrent builds.

**Status writes:** every status write the worker makes is one `UPDATE` guarded by the build's current status (forward through `building`, `testing`, `deploying` only) and by an attempt token (`builds.started_at`), so a stale thread cannot move a build that was requeued, previewed, failed or published; `failed` is written only by `_fail_build`, which also writes the dead-letter row. See "Status contract" in `docs/internals/worker.md`.

**Graceful shutdown:** SIGTERM handler via threading.Event; finishes current jobs then exits. Orphan recovery runs at startup and every two minutes after: it requeues, or permanently fails, builds in a worker-owned status whose lease no live worker holds (the lease is best effort; see `docs/internals/worker.md`).

**Background threads:** Health monitor (every 15 min), daily backups (03:00 UTC), ops agent (every 30 min).

---

## Stage 4: Builder (Implemented)

**Model:** Claude Opus 4.6 for code generation; Sonnet for Phase 1 boilerplate (cost optimization)
**Implementation:** `compose/build.py` (v2, the default since 2026-04-27) or `cli/build.py` (v1, legacy, opt-in with `CAS_BUILD_ENGINE=v1`)
**Purpose:** Generate a complete, deployable web application from a spec

### Engine dispatch (CAS_BUILD_ENGINE)

As of 2026-04-21, the worker reads `CAS_BUILD_ENGINE` to decide which engine to run. The default flipped from `v1` to `v2` on 2026-04-27 (`queue/worker_pg.py` now falls back to `v2`; the test suite's `v1_only` gate flipped the same day):

- **`v1` (legacy, opt-in):** Claude-per-build pipeline (`cli/build.py`) with 117 registered post-processors. Slow (12-15 min) and variable.
- **`v2` (default):** Static-compose pipeline (`compose/build.py`), widgets + decision files + templates, zero Claude calls in the build path. Fast (minutes) and deterministic. Shadow-rollout gate: flip per-worker to cut over one customer at a time. Unknown values raise `ValueError`. See [cas_v2_architecture.html](internals/cas_v2_architecture.html).

### v2 validate-stage: v1→v2 auto-translation

When `CAS_BUILD_ENGINE=v2` and the inbound spec came from the **v1 intake flow** (has `spec_version` / `user_roles` / `data_models` but no `archetype` / `palette` / `font_pair`), the worker transparently calls `compose.intake.translate_v1_spec(spec, admin_email, app_name)` before validation. The translator classifies the archetype via `detect_archetype(spec)`, which first checks `spec._harbor_meta.archetype_hint` (set by `services/harbor_to_compose.ts` for harbor-extracted specs) and returns it when valid. When the hint is missing or unknown the function falls through to keyword text matching against the description + data model names + action names + page titles + user-role descriptions. The translator then derives palette + font from v1 design hints (with sensible fallbacks), maps description → site_description + hero_description, and consumes `spec._harbor_meta.services` + `spec._harbor_meta.testimonials` (also harbor-populated): each is mapped from the harbor data shape (`{title, description?, price?}` / `{quote, author?}`) to the compose offerings-grid + testimonials-card shape via `_normalize_harbor_services` + `_normalize_harbor_testimonials`, then passed into `answers["services"]` + `answers["testimonials"]` so `scaffold_spec` honors the keeper's words instead of falling back to the archetype's executive-coaching defaults. Tier + admin_email + app_name are preserved on the translated spec. The translated dict is written back to the spec file on disk so downstream compose sees the v2 shape. This is the bridge that lets production customer intake (regular `/try` flow + harbor `/lighthouse/build` flow) → v2 engine work without changes to the customer-facing routes.

### v2 build-stage: tier handling

`compose.build.build_app(spec_path_or_dict, output_dir, tier=None)` accepts `tier` for API-compatibility with v1's `cli.build.build_app`. **v2 does NOT vary output by tier today**, all tiers get the same archetype + widget composition. The tier value is recorded in `compose-manifest.txt` (alongside archetype/palette/font_pair/files/sha256) for audit + customer dashboard surfacing. Tier-gated features (revision limits, review SLA, Stripe pricing) read from `builds.tier` in the DB and are handled by worker / web / CMS layers downstream, unchanged by engine choice. If future v2 gating is added (grow/scale-specific widgets, per-tier archetype variants), extend `compose.build.compose_files()` to vary by tier.

### v2 testing-stage: `_lint_v2_build_output`

When `CAS_BUILD_ENGINE=v2`, the testing stage does NOT run the v1 Claude-driven test/design-review/QA-fix loops (they expect v1 LLM-emitted structure and their fix agents would re-invoke Claude, defeating the v2 zero-AI-in-build-path contract). Instead the worker calls `_lint_v2_build_output(build_dir)`; a fast, deterministic set of static checks against the emitted artifact. Each check surfaces as a per-line entry in the customer-visible build log:

- All required files present (compose-manifest.txt, server.js, package.json, db_migrate.js, db/schema.sql, db/seed.sql, layouts, style.css, logo.svg, JS runtimes).
- `compose-manifest.txt` has archetype/palette/font_pair/files/sha256 lines, the warning counts and one line per warning (`warning: ` for the engine's, `spec-warning: ` for the spec's).
- Seed `INSERT` counts per table match `spec.seed_content.<model>` array lengths.
- No widget renders `<%= heading %>` raw without the `_accent` or `_rendered` transform (regression guard for the `{Word}` italic-accent leak bug, 2026-04-21).
- `style.css` carries required CAS utility classes (`.cas-btn`, `.cas-card-lift`, `.cas-eyebrow`, `.cas-reveal`, `.cas-container`) and has no banned v1-era tokens (Inter/Roboto fonts, excessive `100vh` use).
- Every page view in `views/*.ejs` is composed from widget `<%- include(...) %>` chains (no static-HTML fallback).
- Footer carries the `craftandship.io` Powered-By attribution.

Failures are warning-level today: they surface prominently in the build log but don't abort the build. Individual checks can be promoted to fatal as the v2 pipeline matures. Contract tests: `tests/test_worker_pg.py::TestV2BuildOutputLint` (5 tests) assert the helper is importable, the shape is stable, missing files trigger errors, fresh builds pass clean, and raw-heading regressions are caught.

### Build Strategy

Single-pass or multi-pass depending on complexity (historically tier-based; multi-pass triggers at 7+ models or 15+ pages):

**Single-pass:** `_build_explicit_prompt(spec)` generates the full app in one Claude call. Derives explicit route requirements per page and full CRUD per data model (not a raw JSON dump).

**Multi-pass:** 3 phases (foundation + features_a + features_b). Phase 1 (Sonnet) generates boilerplate: package.json, server.js, db/schema.sql, db/seed.sql, middleware, auth, public files. Phase 2+ (Opus) generates feature routes and views for subsets of data models. `_split_models_by_dependency()` does FK analysis to put parent tables in Phase A, child/join tables in Phase B.

### Template Library

`cli/templates/` contains 7 static files (db_index.js, db_migrate.js, middleware_auth.js, routes_admin.js, sw.js, support-chat.js, 500.ejs) pre-loaded before Phase 1, reducing API token usage by ~20-30%.

### Post-Processing

Thirteen deterministic fixes run after all phases complete: shared DB pool, Tailwind config, CSRF pattern fix, validateCsrfToken export, circular import fix, route view scaffolding, demo credential stripping, admin mount, error handling, EJS validation, package deps, compiled Tailwind CSS, .railwayignore. Plus plural slug path fixes for EJS view links.

### Knowledge Base Injection

`failure_kb.py` and review KB both inject learned rules into ALL prompt builders so every build benefits from past failures. High-confidence patterns (rate >= 70%, 3+ occurrences) are injected automatically. Vector similarity (TF-IDF cosine over character trigrams) provides fuzzy matching for novel errors.

### Tech Stack for Generated Apps

Standardized on one stack across all tiers for maximum build reliability:

- **Runtime:** Node.js 20+ / Express.js
- **Database:** PostgreSQL via `pg.Pool`, raw parameterized SQL, no ORM
- **Frontend:** Server-rendered EJS templates + Tailwind CSS v3.4 (compiled, not CDN)
- **Auth:** express-session + bcrypt + HMAC-based CSRF tokens
- **Security:** Helmet.js (CSP, HSTS, X-Frame-Options), express-rate-limit on login
- **Email verification:** Required on registration, 24-hour token, Resend integration
- **Support chat:** Floating AI widget powered by Claude Haiku, spec-aware system prompt
- **Admin tools:** Data reset, CSV import, password change (routes/admin.js template)
- **PWA:** manifest.json, service worker (sw.js), SVG favicon
- **DB resilience:** Server keeps listening even if DB is unavailable on startup; auto-recovers

Generated apps always include 14 required files: db/index.js, db/schema.sql, db/seed.sql, db/migrate.js, middleware/auth.js, routes/auth.js, routes/admin.js, server.js, views/layout.ejs, views/login.ejs, views/register.ejs, public/sw.js, public/manifest.json, package.json.

### Revision and Rebuild Support

`spec.rebuild_request` (revision/rebuild feedback) is prepended as a HIGHEST PRIORITY section in the prompt. Page-to-model route reconciliation detects when spec page paths don't match CRUD slugs and generates explicit alias instructions. Plural slug rules ensure EJS view links match Express router mount paths.

---

## Stage 4.5: Code Review (Implemented)

**Model:** Claude Sonnet
**Implementation:** `cli/review.py`
**Purpose:** Non-blocking quality check after build, before design review

Checks generated code against spec: every page has a route+view, every model has full CRUD, auth-required pages call requireAuth, CSRF tokens in POST forms, no XSS in EJS, plural slug consistency. Returns `{passed, critical_count, warning_count, summary, issues}`. Critical issues feed into review_kb.json for future build prompt injection. Always non-blocking; skips gracefully on any error.

---

## Stage 4.75: Design Review (Implemented)

**Implementation:** `cli/design_review.py`, `cli/design_fix.py`
**Purpose:** Visual quality check after code review, before tests. Non-blocking by default; blocking when `BLOCKING_DESIGN_REVIEW=true` (prod).

Checks generated app for visual quality: color contrast, typography, spacing, responsive layout. Returns `{score, critical_count, issues}`. When critical issues are found, triggers `design_fix.py` up to `MAX_DESIGN_FIXES` times (default 3, currently 6 on prod) then re-reviews; early-exits if no progress after 2+ attempts. Results stored in `builds.design_review_status` / `builds.design_review_results` after each iteration. Build artifact is stored AFTER design review/fix so fixes are captured. Skipped on resume.

### Blocking gate + one-retry-with-guidance (DESIGN_QG_RETRY1)

When `BLOCKING_DESIGN_REVIEW=true` and the design_fix loop can't clear critical issues, the worker raises `DesignQualityGateError` (subclass of `QualityGateError`). The exception handler allows **one retry** of the entire pipeline, but not blindly. Before requeueing, `_inject_design_issues_as_rebuild_request(conn, build_id)` reads `builds.design_review_results`, formats up to 10 critical issues as `- location: issue → fix` bullets, and writes a `DESIGN REVIEW CRITICAL ISSUES` block to `builds.rebuild_request`. The next job claim reads that column into the spec, and `build.py` prepends the block as the REVISION REQUEST (HIGHEST PRIORITY) section of the Claude prompt.

Rationale: design generation is stochastic, the same spec passed on a fresh attempt in production (validated via the admin AI Fix flow). One guided retry replaces what was previously a manual click. Customer-written revision text is preserved above the injected block; a stale prior design block is replaced, not stacked. Injection is fail-safe (returns False on missing critical issues or DB errors); the requeue proceeds in either case. Existing guardrails still apply: repeated-error early-exit (identical error across 2 attempts → permanent fail) and 2-hour build-age cap.

`QAQualityGateError` (from `BLOCKING_QA=true`) stays non-retryable: QA failures are structural (missing CSRF, auth bypass) and don't self-heal via stochastic re-gen.

---

## Stage 5: Test Runner (Implemented)

**Implementation:** `cli/test.py`
**Tool:** Playwright (headless browser) + HTTP requests for speed

### 19 Smoke Tests

Tests 1-14 (Playwright): health check, root page, login form, bad credentials, role-based login, auth redirect, XSS protection, button styling, CSS loads, layout structure, CSRF tokens, form methods, navigation links, admin access.

Tests 15-18 (HTTP requests for speed): all spec routes return 200, CRUD list pages show seed data, create form submission works, no 500 on any crawled page.

Test 19 (HTTP): no broken links on CRUD list pages (plural slug validation).

### Self-Correction

When tests fail, `fix.py` reads test failures + source files, asks Claude to fix the code, writes corrected files. Route manifest (`_build_route_manifest(spec)`) gives Claude a clear target. Max 4 fix attempts per build. Consults failure_kb for hints; records outcomes for future learning.

### Visual Verification

Pipeline captures screenshots during test phase (login + up to 5 auth pages) and after deploy. Stored in `{build_dir}/screenshots/`, paths saved to `builds.screenshots` JSONB. Served on status page for preview/live builds.

### QA Agent (Stage 3.5)

After smoke tests pass, `qa.py` asks Claude Sonnet to generate targeted Playwright tests from the spec JSON, runs them. QA failures are non-blocking warnings. One fix attempt via `qa_fix.py` with hardened guardrails (never modifies auth, server.js, or layout structure).

### UI Tests (Stage 3.75)

After QA stage, `ui_test.py` runs visual quality checks (Playwright-based). Non-blocking: returns `{score, results}`. Score stored in `builds.ui_test_score` / `builds.ui_test_results`. Errors/skips never block the build.

---

## Stage 6: Deployer (Implemented)

**Implementation:** `cli/deploy.py`
**Provider:** Railway (GraphQL API + CLI source upload)

### Railway Operations

- `projectCreate` + `serviceCreate` (app + PostgreSQL)
- `variableCollectionUpsert` (DATABASE_URL, SESSION_SECRET, NODE_ENV, PORT, the site's ADMIN_EMAIL and SITE_NAME from the build's spec, and RESEND_API_KEY and RESEND_FROM_DOMAIN only when the operator sets `GENERATED_APP_RESEND_API_KEY` and `GENERATED_APP_RESEND_FROM_DOMAIN`; the platform's own `RESEND_API_KEY` is never copied to an app)
- `serviceDomainCreate` (generates `.up.railway.app` domain)
- `railway up --detach` (upload source, trigger build)
- `deployment` query (poll status, 12 retries for eventual consistency)
- `projectDelete` (cleanup on failure)

### Post-Deploy Verification

- Health check: polls /health for up to 120s
- Route verification: `verify_routes(url, spec)` checks ALL spec-defined routes return 200 (not 500) after deploy, using requests.Session with login
- Auth verification: `verify_auth(url, spec)` tests POST /login with seeded admin credentials
- DB crash detection: scans Railway deploy logs for DB connectivity patterns before entering Claude fix loop; waits 2 min for DB to recover

### Deploy Self-Correction

`deploy_fix.py` handles post-deploy crashes. Reads Railway deploy logs + source files, asks Claude to fix, writes corrected files for redeployment. Max 3 attempts. Consults and updates failure_kb.

### Staging Preview

After successful deploy, builds go to `preview` status (not `live`). Customer reviews and clicks "Approve and Go Live" to promote. Enables revision requests before going live.

---

## Stage 7: Health Monitor (Implemented)

**Implementation:** `monitor/` directory
**Schedule:** Every 15 minutes (configurable via MONITOR_INTERVAL)

### Tiered Architecture

```
Every 15 min: Tier 1 HTTP Ping (monitor/ping.py)
    |
    [200 OK?] -- Yes --> Log success, update baseline latency (EMA alpha=0.2)
    |
    No
    |
    v
Tier 2: Retry with backoff (3 attempts, 120s)
    |
    [Any retry 200?] -- Yes --> Log transient failure, move on
    |
    No
    |
    v
Tier 3: Lightweight Diagnostic (monitor/diagnose.py)
    - Check Railway service status via GraphQL
    - Check DATABASE_URL reachability
    - Check SSL certificate expiry
    - SNAPSHOT DATABASE BEFORE ANY FIX
    |
    [Issue identified and fixable?] -- Yes --> Apply fix, log, notify via email
    |
    No
    |
    v
Tier 4: Full Diagnostic (Claude Sonnet)
    - Fetch Railway deployment logs
    - Analyze and recommend fix
    - SNAPSHOT DATABASE BEFORE ANY FIX
    |
    [Fixed?] -- Yes --> Log, notify via email
    |
    No
    |
    v
Escalate to admin (email via Resend, 2-hour per-app cooldown)
```

### Supporting Infrastructure

- **Registry:** `monitor/registry.py`, PostgreSQL-backed (migrated from SQLite). Tracks baseline_latency_ms with EMA. Upserts on rebuild to prevent duplicate rows.
- **Backups:** `monitor/backup.py`, daily pg_dump of each registered app's database (the customer apps the worker registers after a deploy, not the platform database) with gzip compression into `<app name>__<registry id>/`, the 7 most recent kept per app, up to 3 attempts for a rejected password, a starting database or connection saturation. Dumps are plain SQL taken with `--clean --if-exists` and are not encrypted; `restore()` replays one in a single transaction with `ON_ERROR_STOP`. Whether the worker can reach each customer database is not established by this repository (see `docs/internals/monitor-ops-scripts.md`). When `RAILWAY_API_TOKEN` is unset (e.g. during an incident-response safety pause), `run_daily_backups()` returns a skip marker instead of raising per-app; the worker logs once at info level and does NOT fire Sentry. Prevents noise from what is a config state, not a code bug.
- **Pre-action snapshots:** Enforced before all Tier 3/4 DB-touching fixes.
- **CLI:** `monitor/cli.py` for register, list, check, diagnose, backup, restore, run.

---

## Stage 8: Ops Agent (Implemented)

**Implementation:** `ops/` directory
**Schedule:** Every 30 minutes (configurable via OPS_SCAN_INTERVAL)

Autonomous operations agent that scans failed builds, matches known patterns to playbooks, and applies fixes within guardrails.

- **Scanner:** `ops/scanner.py` queries failed builds, groups by normalized error signature, classifies category
- **Playbooks:** `ops/playbook.py` registry of known fixes; the two that run are env_config and railway_redeploy (both on Railway; there is no GitHub client), and failures classified `version_mismatch`, `missing_dep`, `missing_tool` or `dockerfile` are reported and not fixed
- **Guardrails:** `ops/guardrails.py` circuit breaker (3 fixes/hour), cost guard (20 fixer runs/day; the playbooks make no model calls), severity filter
- **Audit:** `ops/audit.py` PostgreSQL audit log of all actions
- **Alerter:** `ops/alerter.py` escalation emails via Resend with 4-hour per-category cooldown

Safety: starts in dry-run mode (OPS_DRY_RUN). Never touches app logic. Currently running live (OPS_DRY_RUN=false).

---

## API Cost Budget Per App Type

| App type | Build loops | Est. input tokens | Est. output tokens | Cache hit rate | Est. cost |
|----------|------------|------------------|-------------------|---------------|-----------|
| Booking system (Build) | 8 to 10 | 2M (mostly cached) | 40K | 85% | $3 to $8 |
| Service portal (Build) | 10 to 15 | 4M (mostly cached) | 80K | 80% | $8 to $15 |
| Basic CRM (Grow) | 15 to 25 | 8M (mostly cached) | 150K | 80% | $20 to $40 |
| Member portal with payments (Grow) | 20 to 30 | 10M (mostly cached) | 200K | 75% | $30 to $60 |

These are estimates.

---

## Security Considerations

- Customer apps are isolated (separate deployments, separate database instances, not shared schemas)
- No customer app has access to Craft & Ship infrastructure or other customer apps
- API keys for hosting providers are stored in platform infrastructure, never in customer app code
- Customer database dumps written by `monitor/backup.py` are owner-only files (mode 0600) but are not encrypted
- Stripe handles all payment data (the platform never touches credit card numbers)
- Stripe client-side tokenization must always be used so card numbers never hit the customer app server
- Customer app admin passwords are hashed (bcrypt) and never stored in plain text
- Generated apps must never log request bodies on payment or authentication routes
- All database queries must be parameterized (no string concatenation in SQL)
- All user inputs must be sanitized and outputs encoded
- CSRF protection on all forms
- Content Security Policy (CSP) headers on all generated apps
- Rate limiting on all API endpoints
- Auth tokens stored in httpOnly cookies, never localStorage
- npm audit runs during every build to catch known dependency vulnerabilities
- Builder system prompt must include explicit security instructions (parameterized queries, input sanitization, output encoding, secure auth patterns)

---

## File and Repo Structure

```
craftandship/
  web/                      # Customer-facing website (TypeScript, Express, port 4000)
    src/
      app.ts                # Express app factory (no port binding, used by tests)
      server.ts             # Entry point, binds port, runs migrations
      middleware/auth.ts     # requireAuth, CSRF (HMAC-SHA256)
      routes/               # auth, intake, builds, admin, support, gallery, features, ops-api
      services/             # claude, email, stripe, spec-validation, logo, abandon-check
      db/                   # schema.sql, migrate.ts, pool export
      types/                # TypeScript interfaces
    views/                  # EJS templates (30+ views)
    tests/unit/             # Vitest
    tests/ui/               # Playwright E2E
  cli/                      # Build pipeline (Python)
    intake.py               # Intake conversation (Claude Opus 4.6)
    validate.py             # Spec validation (pure Python)
    build.py                # Code generation (Opus 4.6, single/multi-pass)
    review.py               # Code review agent (Claude Sonnet)
    test.py                 # 19 smoke tests (Playwright + HTTP)
    fix.py                  # Test failure self-correction
    deploy.py               # Railway deployment (GraphQL API)
    deploy_fix.py           # Post-deploy self-correction
    qa.py                   # QA agent (Claude Sonnet, targeted Playwright tests)
    qa_fix.py               # QA self-correction
    failure_kb.py           # Auto-learning knowledge base (vector similarity)
    railway_cleanup.py      # Orphan Railway project cleanup
    templates/              # 7 static template files for generated apps
  queue/
    worker_pg.py            # PostgreSQL-backed build queue worker (concurrent, self-healing)
  monitor/                  # Health monitoring
    ping.py                 # Tier 1+2 health checks
    diagnose.py             # Tier 3+4 diagnostics, self-healing
    backup.py               # Database snapshots, restore, daily backups
    registry.py             # PostgreSQL app registry
    cli.py                  # CLI interface
  ops/                      # Autonomous ops agent
    agent.py                # Main scan loop
    scanner.py              # Error classification
    playbook.py             # Fix registry
    guardrails.py           # Circuit breaker, cost guard
    audit.py                # PostgreSQL audit log
    alerter.py              # Escalation emails
    playbooks/              # Fix implementations
  scripts/                  # Deployment, backup, maintenance scripts
  tests/                    # Python tests (pytest)
  docs/                     # Architecture and planning documents
```

---

## Implementation Status

| Module | Status | Notes |
|--------|--------|-------|
| `cli/intake.py` | Complete | Opus 4.6, spec validation before save, design/color preferences |
| `cli/validate.py` | Complete | Pure Python, no API cost, design field validation |
| `cli/build.py` | Complete | Opus 4.6 (Sonnet for Phase 1), single/multi-pass, 13 post-processing fixes, template library, KB injection, route reconciliation, plural slug rules |
| `cli/review.py` | Complete | Sonnet code review, 9 checks, non-blocking, feeds review_kb |
| `cli/test.py` | Complete | 19 smoke tests (Playwright + HTTP), screenshot capture |
| `cli/fix.py` | Complete | Self-correction (4 attempts), route manifest, failure_kb |
| `cli/deploy.py` | Complete | Railway GraphQL, health check, route/auth verification, redeploy |
| `cli/deploy_fix.py` | Complete | Post-deploy fix (3 attempts), DB crash detection |
| `cli/qa.py` | Complete | Spec-aware QA agent (Sonnet), targeted Playwright tests |
| `cli/qa_fix.py` | Complete | QA self-correction with guardrails |
| `cli/failure_kb.py` | Complete | Auto-learning KB, vector similarity (TF-IDF cosine), fuzzy lookup |
| `monitor/ping.py` | Complete | Tier 1/2 health checks, latency tracking (EMA) |
| `monitor/diagnose.py` | Complete | Tier 3/4 diagnostics, self-healing, escalation (2hr cooldown) |
| `monitor/backup.py` | Complete | pg_dump snapshots, gzip, 7 kept per app, retry on password, startup and saturation errors, single-transaction restore |
| `monitor/registry.py` | Complete | PostgreSQL app registry, upsert on rebuild |
| `queue/worker_pg.py` | Complete | PostgreSQL SKIP LOCKED, concurrent (`MAX_CONCURRENT_BUILDS`, default 1), tier-based timeouts, supervisor with cooperative cancellation, guarded status writes, orphan recovery, stage-based resumption, auto-cleanup |
| `ops/` | Complete | Autonomous ops agent, playbook-based fixes, circuit breaker, cost guard, audit log, escalation emails |
| `web/` | Complete | TypeScript/Express, PostgreSQL, Stripe Checkout, Claude Opus intake, email verification, transactional email (Resend), admin panel, support chat, feature requests, gallery, ops API, Sentry |

### Test Counts

Test counts drift; see CI or run `pytest tests/ -q --co | tail -1` and `cd web && npm test`.
