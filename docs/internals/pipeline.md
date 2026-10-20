# Build pipeline: cli/ modules

> Historical document from April 2026; the system is no longer developed and the site is still online.

> **Scope:** per-module detail for every `cli/*.py` module in the build pipeline. Extracted from CLAUDE.md to keep the root context file focused on load-bearing decisions. Consult this file when you need to understand a specific pipeline module's responsibilities, public functions, or invariants.
>
> **Companion files:**
> - `docs/internals/post-processors.md`: the deterministic fixes applied after code generation (117 `@processor` functions registered in `cli/build.py`)
> - `docs/internals/worker.md` - `queue/worker_pg.py` scheduler + retry + concurrency
> - `docs/internals/coordinator.md`: autonomous agent framework

## `cli/build.py`

**Core:** Completeness validation (`REQUIRED_FILES` in `cli/build.py`, including `db/index.js` + `routes/` + `views/`), token continuation fix (concatenate-then-parse), deduplication. Design-aware CSS generation: reads `spec.design` (primary_color, secondary_color, accent_color, style) and generates themed, responsive apps with CSS custom properties. PWA scaffolding (`manifest.json`, `sw.js`), SVG favicon, strengthened auth/registration, realistic seed data. Support chat widget removed from generated apps (2026-04-15; reduces complexity; CAS platform `support.ts` handles customer support separately).

**Prompt derivation:** `_build_explicit_prompt(spec)` derives explicit route requirements (from `spec.pages` with auth/roles annotation) and full CRUD requirements (from `spec.data_models` with list/create/read/update/delete per model) replacing bare JSON dump.

**Page-to-model route reconciliation:** `_build_page_model_mapping()` detects when spec page paths don't match CRUD slugs (e.g., `/time` page vs `TimeEntry` model's `/time-entry` slug) and `_build_route_reconciliation_section()` generates explicit instructions for Claude to mount routers on both paths. `_camel_to_parts()` splits CamelCase model names for matching. `_model_slug()` and `_model_table()` are canonical slug helpers used by all prompt builders. Expanded POST method keywords include "import", "export", "upload", "sync", "approve", "reject", "delete", "settings", "profile", "account", "config".

**Routing/UX rules:** Public-page-aware routing: if spec has pages with `auth_required=false`, instructs Claude to create a public landing page at `/` instead of a login form. **PUBLIC FORM PAGES rule** (added 2026-04-15): any page with `auth_required=false` that collects user input (e.g. `/contact`, `/book`, `/request`) MUST have GET + POST handlers NOT inside admin-only CRUD routers; mount at the spec page path, keep admin CRUD at a separate path (e.g. `/admin/contact-messages`). `session.save()` before redirects (prevents race with async session stores). Button visibility rules (solid `bg-primary` on all submit buttons). Boolean/status toggle rules (POST-based toggle for done/active fields). Delete graceful handling (check result before redirect). Destructive action confirmation (all delete/remove/reset buttons require `data-confirm` dialog). Register error handling (re-render form, never 500).

**Email verification (required):** Every generated app with `/register` implements email verification. POST `/register` creates unverified user, generates 24-hour token, attempts to send via Resend (if `RESEND_API_KEY` set) and always logs the link to console, redirects to `/verify-email-sent`. GET `/verify-email?token=` marks verified and logs in. Login gate blocks unverified users with resend link. Seed users have `email_verified=TRUE` so smoke tests pass. `views/verify-email-sent.ejs` included.

**DB startup resilience:** `server.js` must NOT call `process.exit()` on migration failure. Railway's PostgreSQL takes 10-30s to provision after app start so generated apps keep the HTTP server listening even if DB is unavailable; health checks pass and the app auto-recovers when DB is ready. Injects learned rules from `failure_kb` when high-confidence patterns exist. Reads `spec.rebuild_request` (revision/rebuild feedback) and prepends it as a HIGHEST PRIORITY section in the prompt.

**Multi-pass generation:** `_should_use_multi_pass(spec)` decides single vs multi-pass based on tier and complexity. Build tier is single-pass up to 6 models and 14 pages. Grow triggers multi-pass at 7+ models or 15+ pages. Scale always multi-pass. 7+ models always triggers 3 phases (foundation + features_a + features_b). Phase 1 uses Sonnet (`MODEL_FOUNDATION`, cost optimization for boilerplate); feature phases use Opus (`MODEL`). Phase 1 generates foundation (`package.json`, `server.js` with all router mounts, complete `db/schema.sql` + `seed.sql`, middleware, layout, auth, public files). Phase 2+ generates feature routes and views for subsets of data models, receiving Phase 1's `schema.sql` as context. Phase N prompt includes explicit file checklist (routes/slug.js + 4 view files per model) to prevent Claude from skipping models. `_split_models_by_dependency()` does FK analysis to put parent tables in Phase A and child/join tables in Phase B.

**Template library:** `cli/templates/` contains static files (`db_index.js`, `db_migrate.js`, `middleware_auth.js`, `routes_admin.js`, `routes_settings.js`, `views_admin_settings.ejs`, `sw.js`, `500.ejs`, `image-upload.js`) pre-loaded by `_load_templates()` in **both single-pass and multi-pass** builds (wired into `build_app()` single-pass branch 2026-04-10), reducing API token usage by ~20-30%. Single-pass merge semantics match multi-pass: templates seed the file dict, Claude's generated version overrides on collision, then post-processors normalize. `_build_explicit_prompt()` includes the same "files are already provided" NOTE block as `_build_phase1_prompt()` so Claude can skip regenerating them.

**Go-live features (template):** `routes/admin.js` is a static template providing admin data reset (POST `/admin/reset-data` truncates all non-user tables), CSV import (POST `/admin/csv-import` with `csv-parser` for bulk data), admin password change (POST `/admin/change-password` with bcrypt validation). `server.js` must mount it: `app.use(require('./routes/admin'))`. `package.json` must include `csv-parser` and `multer`.

**Seed data:** Minimum 15 realistic records per data table, no placeholder text (Lorem ipsum, Test item, etc.).

**Post-processing:** the registered deterministic fixes (117 `@processor` functions; `cli/pipeline.py` orders them as a DAG, see `docs/internals/POST_PROCESSOR_ORDERING.md`) run after all phases complete. **Full catalogue: `docs/internals/post-processors.md`.**

**KB injection:** `get_build_prompt_rules()` and `get_review_rules()` are injected into ALL prompt builders (single-pass `_build_explicit_prompt`, multi-pass `_build_phase1_prompt`, and `_build_phase_n_prompt`) so every tier benefits from learned rules.

**UUPM design context:** `_build_design_context(spec, abbreviated=False)` is a shared function called by all 3 prompt builders. Calls `design_data.get_design_context()` for industry-specific palettes, curated font pairings, and UX rules. Full mode includes `UX_RULES_COMPACT` + `VISUAL_DESIGN_CHECKLIST` (15 mandatory visual rules: split-panel login, gradient backgrounds, solid nav, card animations, dashboard stat cards, hero sections, color harmony, font/spacing/responsive/icon rules; plus 4 Impeccable anti-pattern rules added 2026-04-16, no gradient text on headings, no 3+ identical icon orb bullet lists, no equal-weight 3-col feature grids, section eyebrows must be plain text, see `docs/IMPECCABLE_DESIGN_SYSTEM.md`) + `STYLE_VISUAL_HOOKS` (per-style Tailwind directives). Falls back to legacy `_get_style_fonts()` if `design_data` is unavailable. Phase N uses `abbreviated=True` (colors+fonts only, no UX rules or checklist) to save tokens. Pre-flight checklist in `_build_explicit_prompt` includes 3 DESIGN items for recency reinforcement; final instruction also mentions design rules.

**Build log messages** are customer-friendly throughout (no file names, function names, or technical details exposed to customers; all diagnostic detail goes to server logger only).

## `cli/api_client.py`

Shared API client for all pipeline modules. `ApiCallMetrics` dataclass for per-call cost tracking with split executor/advisor billing (`executor_cost_usd`, `advisor_cost_usd`, `advisor_input_tokens`, `advisor_output_tokens`, `advisor_model`). `PRICING` table with rates for Opus/Sonnet/Haiku (realtime + batch). `estimate_cost()` for USD calculation including cache token pricing. `call_api()` centralizes retry (exponential backoff, 5 retries), token continuation (auto-handles `max_tokens`), cost metrics (returned per call, thread-safe), and batch mode (single-request batches with 5-min polling, cancel, fallback-to-realtime). `should_use_batch(tier)` checks `TIER_API_CONFIG` (build=realtime, grow/scale=batch) and `DISABLE_BATCH_API` env var kill switch. `CombinedResponse` merges continuation calls. All pipeline modules (`build`, `review`, `qa`, `fix`, `qa_fix`) delegate to `call_api()` via thin wrappers. Fix loops always use realtime regardless of tier.

**Anthropic Advisor Tool integration (`advisor_20260301`):** when `advisor_model` param is set, calls `client.beta.messages.create(betas=["advisor-tool-2026-03-01"])` with Sonnet executor + Opus advisor in a single request. `_extract_advisor_usage()` parses `response.usage.iterations` (handles both Pydantic `BetaMessageIterationUsage` shape and dict mocks via `_field()` helper). Per-module env var toggles in fix loops: `USE_ADVISOR_FIX`, `USE_ADVISOR_DEPLOY_FIX`, `USE_ADVISOR_QA_FIX`, `USE_ADVISOR_DESIGN_FIX` (all default to disabled). SDK pin `>=0.45.0,<1` (`requirements.worker.txt`).

## `cli/review.py`

Stage 2.5 code review agent (Claude Sonnet). After build writes files, checks generated code against spec: every page has a route+view, every model has full CRUD, auth-required pages call `requireAuth`, CSRF tokens in POST forms, no XSS in EJS. Non-blocking: always returns `{passed, critical_count, warning_count, summary, issues}`. Skips gracefully on any error. Called by worker before design review and artifact storage.

## `cli/design_review.py`

Stage 2.75 design review agent (Claude Opus). After code review, checks generated app for visual quality: color contrast, typography, spacing, responsive layout. Non-blocking by default; becomes blocking when `BLOCKING_DESIGN_REVIEW=true`. Returns `{score, critical_count, issues}`. When critical issues found, triggers `design_fix.py` up to `MAX_DESIGN_FIXES` times (env var, default 3) then re-reviews. Early-exits if no progress after 2+ attempts. `_KEY_FILES` = `["server.js", "views/layout.ejs", "views/login.ejs", "views/register.ejs"]`; these load first before the 80k char budget can be exhausted by alphabetical `admin/*` views. Results stored in `builds.design_review_status`/`builds.design_review_results`.

## `cli/design_fix.py`

Design self-correction (Claude Opus). When design_review finds critical visual issues, applies targeted CSS/EJS fixes. Non-blocking.

## `cli/ui_test.py`

Stage 3.75 UI test agent. After QA stage, runs visual quality checks (Playwright-based). Non-blocking: returns `{score, results}`. Score stored in `builds.ui_test_score`/`builds.ui_test_results`.

## `cli/deploy.py`

Post-deploy health check (`verify_health` polls `/health` for 120s, non-blocking; it returns `"infra"` when no response ever arrives or the app reports a DB problem, `"failed"` otherwise), regex-based deployment ID parsing, aggressive retry for Railway eventual consistency (12 retries for "Deployment not found" in `wait_for_deployment`). `wait_for_deployment` raises only after two consecutive CRASHED/FAILED polls, because Railway often reports CRASHED briefly before SUCCESS. `redeploy()` for re-uploading source to existing Railway service.

- `verify_routes(url, spec)`: checks ALL spec-defined routes return 200 (not 500) after deploy. Uses `requests.Session` with login, retries once per route for transient DB race.
- `verify_auth(url, spec)`: tests POST `/login` with seeded admin credentials. No longer skips when `USE_MAGIC_LINK_AUTH` is true (downgrades failures to warnings instead).
- `verify_data(url, spec, timeout=15)`: checks CRUD list page content size (>2000 bytes threshold) to detect empty seed data. Logs in as admin, checks up to 5 pages.
- `verify_magic_link_auth(url, raw_token)`: signs in with the worker's own verification token the way the confirmation page does. It sends `GET /login` to get a session cookie and the form's `_csrf` value, then `POST /magic/consume` with `token` and `_csrf` in the request body. A redirect to anywhere but `/login` means the seeded hash matched, the app set `used_at` and a session was created; `/login?error=...` or a missing route is a failure. The token is in no URL, so it cannot land in the app's access log, a proxy log or a `Referer` header, and it is single use, so it is dead once the probe passed. The worker seeds two tokens; the customer's emailed one is not touched. A v1 app that only implements `GET /magic?token=` has no `POST /magic/consume` and is reported as "failed", which the worker logs as a warning, never as a build failure. The contract with the generated app is pinned by `tests/test_login_probe_generated_router.py`, which runs the probe against the router `compose.build.build_app` really emits. It is skipped when `USE_MAGIC_LINK_AUTH` is off or no token was generated, and refuses a base URL that fails `probe_url_problem`.
- **Probe transport.** Every HTTP probe in this module (`verify_health`, `verify_routes`, `verify_auth`, `verify_data`, the login probe) sends through `monitor.ping.guarded_session` (`_same_origin_session` wraps it). Each connection resolves the host once, refuses unless every address is public, and connects to the address it checked; every redirect hop is URL-checked, and proxy environment variables are ignored. The review browser is not covered by this (Chromium resolves a name itself).
- **`deploy()` return shape:** dict `{live_url, railway_project_id, railway_service_id, railway_environment_id, database_url, health_check, deployment_status}` (`deployment_status` is Railway's own end state for the deployment). The variables it sets on the generated app are `NODE_ENV=production`, a fresh `SESSION_SECRET`, `PORT=3000`, the app's own `DATABASE_URL`, the site owner's `ADMIN_EMAIL` and the `SITE_NAME` from the build's `spec.json` (each left out when the spec has no plain-text value), and, only when the operator sets `GENERATED_APP_RESEND_API_KEY`, that value as the app's `RESEND_API_KEY` and `GENERATED_APP_RESEND_FROM_DOMAIN` as its `RESEND_FROM_DOMAIN` (`_generated_app_mail_env`). The platform's own `RESEND_API_KEY` is never copied to an app; without the dedicated key an app has no mail transport and writes a requested login link to its own server log.
- **`destroy_project()` guard:** Single choke point for all Railway project deletions. Hard-stops: (1) ID match against `RAILWAY_PROJECT_ID` env var, (2) name lookup via GraphQL, blocks any project whose name starts with `"craftandship"`, is one of the built-in platform names, or is in `PROTECTED_RAILWAY_PROJECT_NAMES` env var (comma-separated). Raises RuntimeError on block. It fails closed: if the project name cannot be read (API error, unexpected response shape, a project that no longer exists) it raises and deletes nothing, and it returns only when Railway confirmed the delete (`projectDelete` true); any other outcome raises `destroy_project FAILED`, so a caller that gets no exception can rely on the project being gone. Defense-in-depth on top of the worker's `ENABLE_ORPHAN_SWEEP` gate. (The web app's admin delete has its own `projectDelete` call in `web/src/services/railway.ts`, so this function is the choke point for the Python side only.)

## `cli/intake.py`

Validates extracted spec with `validate_spec()` before saving. Feeds errors back to Claude for self-correction. Asks for design/color preferences (step 4 of conversation) with industry-specific color hints (pet=orange, finance=blue, healthcare=teal, etc.). After validation, UUPM enrichment hook auto-populates `spec.design` fields (primary_color, secondary_color, accent_color, font_display, font_body, style) with industry defaults when customer didn't specify. Uses existing field names so `_ensure_tailwind_compiled()` works unchanged.

## `cli/design_data.py`

Curated UUPM design data for the build pipeline. WCAG AA verified color palettes (`COLOR_PALETTES`, on_primary pre-fixed), curated font pairings (`FONT_PAIRINGS`; no banned fonts: Inter/Roboto/Open Sans/Lato/Arial/Helvetica substituted with DM Sans/Source Sans 3), product recommendations mapped to the 6-value style enum, keyword matching (`PRODUCT_KEYWORDS`). Counts live in the module.

- `match_product_type(description)`: returns product category.
- `get_design_context(spec)`: merges UUPM defaults with customer overrides, enforces WCAG AA via `_ensure_wcag()`. `_hex_luminance()` and `_are_generic_dark_colors()` detect Claude's near-black placeholder colors and override them with the UUPM palette.
- `VISUAL_DESIGN_CHECKLIST`, reinforces 15 visual rules Claude must follow (was 11; 4 Impeccable anti-pattern rules added 2026-04-16, no gradient text headings, no icon orb bullet lists, no equal-weight card grids, plain-text eyebrows only; see `docs/IMPECCABLE_DESIGN_SYSTEM.md`).
- `STYLE_VISUAL_HOOKS`: per-style concrete Tailwind directives (one entry per `VALID_STYLES` value). Both injected into full-mode `_build_design_context()` alongside `UX_RULES_COMPACT`. Static data, no I/O, no external dependencies.

## `cli/build_trace.py`

Fail-safe JSONL trace writer for build-pipeline observability. `emit_build_trace(build_dir, event_type, payload)` appends one JSON line per event to `{build_dir}/.build-trace.jsonl`. Never raises (silently drops on missing/invalid dir, non-serialisable payload coerced via `repr`). Kill switch via `BUILD_TRACE_DISABLED=1` env var. Used by `build.py` (API calls, file writes, phase transitions, post-processor block start/end) and `design_review.py` (review start/end, per-issue, skipped events). `_CURRENT_BUILD_DIR` ContextVar lets post-processors pull the build dir without signature changes.

## `cli/test.py`

App stdout/stderr captured to log files for debugging. Last 50 lines printed after test run. Returns result dict (success, results, app_stderr) for self-correction loop. Exports `setup_app(build_path)` → `(process, db_name, spec, test_dir)` (4-tuple) and `teardown_app(process, db_name, app_dir=None, test_dir=None)` for reuse by `qa.py`.

**Railway npm install:** `install_deps()` returns `(success: bool, error: str, test_dir: Path|None)`. On Railway, copies source files to `/tmp`, runs `npm install` there, returns the tmp dir as `test_dir`; app runs from `test_dir`, volume never receives `node_modules`. Locally, returns `(success, error, None)` (unchanged).

**20 smoke tests:**
- Tests 1-14: health, login, auth, XSS, buttons, CSS, layout
- Test 15: all spec routes return 200
- Test 16: CRUD list pages show seed data
- Test 17: create form submission works
- Test 18: no 500 on any crawled page
- Test 19: no broken links on CRUD list pages
- Test 20: magic link login works (generates test token, inserts via psql, verifies GET `/magic?token=` redirects correctly)

Tests 15-20 use HTTP requests for speed.

## `cli/fix.py`

Self-correction module. When tests fail, reads test failures + source files, asks Claude to fix the code, writes corrected files. Path traversal protection, 80KB source context limit. The attempt cap lives in the worker (`max_fix_attempts = 4` in `_run_pipeline`, under a 30-minute loop budget). Consults `failure_kb` for hints from past fixes. Records fix outcomes (both successes and failures) for future learning.

**Route manifest:** `_build_route_manifest(spec)` generates an explicit list of every expected route (from spec pages + CRUD models) and includes page-to-model alias detection, giving Claude a clear target when fixing missing/broken routes.

## `cli/deploy_fix.py`

Post-deploy self-correction. When deployed app crashes on startup (health check fails), reads Railway deploy logs + source files, asks Claude to fix, writes corrected files for redeployment. The worker allows up to 3 attempts (`max_deploy_fixes`), under a 15-minute loop budget. Consults and updates `failure_kb`. v2 builds never enter this loop. Records both successful and failed fix attempts for accurate success rate tracking.

**DB-crash detection:** Before invoking Claude, worker scans Railway deploy logs for DB connectivity patterns ("could not connect to database", "connection timeout", "connection terminated", etc.). If detected, waits 2 min for DB to recover and rechecks health before entering the Claude fix loop; prevents wasting API calls on infrastructure delays.

## `cli/failure_kb.py`

Auto-learning knowledge base for build failures. Caches fix outcomes by normalized error signature (strips UUIDs, ports, paths; rejects signatures under 10 chars to prevent catch-all pollution). `record_fix()` is called by the worker AFTER tests/health-checks confirm the outcome; never prematurely. Each entry tracks a `recent_outcomes` rolling window (last 20 booleans); `_effective_rate()` uses this window (when 5+ entries) instead of aggregate counts to prevent inflated historical success rates from persisting.

`get_build_prompt_rules()` injects high-confidence patterns (rate >= 70%, total >= 3) into ALL build prompt builders.

**Seeded entries** (`seeded: True` flag) bypass the threshold entirely, human-curated rules always inject regardless of outcome counts (10 seeded entries as of 2026-04-13).

`get_prompt_hints()` injects known-fix hints into fix/deploy_fix prompts. JSON storage at `cli/failure_kb.json` (committed to repo). Prunes to 200 entries.

**Vector similarity matching:** When exact normalized-signature lookup fails, `fuzzy_lookup()` uses TF-IDF cosine similarity over character 3-grams + word tokens to find semantically similar errors (threshold=0.45). Pure Python, zero external dependencies. `lookup()` tries exact match first, then fuzzy fallback (`use_fuzzy=True` default). `get_prompt_hints()` annotates fuzzy matches with similarity score so Claude can weigh confidence. `find_similar(error_msg, top_k)` returns top-k ranked results for diagnostics.
- Tokenizer: `_tokenize()` produces `w:word` tokens (2+ char words) + `n:xyz` character trigrams.
- `_tf_vector()` uses sub-linear TF (1+log).
- `_cosine_similarity()` over sparse dicts.

**Review KB:** `record_review_issues()` feeds code review critical issues into `review_kb.json` stored on Railway volume (`builds/review_kb.json` via `RAILWAY_ENVIRONMENT` env var detection, falling back to `cli/` locally). `get_review_rules()` returns recurring patterns (2+ occurrences) as build prompt rules. Worker calls `record_review_issues` after `review_build()` finds critical issues. Build prompt injects both failure_kb rules AND review_kb rules.

## `cli/qa.py`

Spec-aware QA agent (Stage 3.5). After smoke tests pass, asks Claude Haiku to generate targeted Playwright tests from the spec JSON, runs them in a child process with a scrubbed environment and a timeout (`cli/qa_runner.py`, started by `qa._run_tests_in_child`; the worker never `exec`s model-written code in its own process), and returns structured results. QA failures are non-blocking warnings (build deploys regardless). Uses port 4568 for its own app instance. Default timeouts: 20s page/navigation. Timeout errors displayed as "Slow response" on status page (not raw Playwright messages). Exports `run_qa(build_dir, spec, port=None, tier="build")`. Downgraded from Sonnet to Haiku for ~67% cost savings (non-blocking QA does not need high-capability model).

**Locator prompt (2026-04-15):** `QA_SYSTEM_PROMPT` has 4 STRICT MODE rules:
1. Every CAS app has desktop nav + `#mobileMenu` + heading + footer containing the same text 3-5×; never use `locator("text=X")` for nav/page-title checks. Use `get_by_role("heading")` or scope to `get_by_role("navigation")`.
2. Row-level buttons like "Done"/"Delete" appear in every row; scope to `tr.nth(0)` or `[data-id].first`.
3. Duplicate form labels: use `.nth(0)`.
4. Do NOT assume `/admin/{model}` sub-routes exist unless they appear in `spec.pages`. Navigate to `/admin` first and discover actual sub-paths from links/buttons on that page.

## `cli/qa_fix.py`

QA self-correction. When `qa.py` finds failures, asks Claude Opus to fix the app code and applies the fixes. The worker allows up to `MAX_QA_FIXES` attempts (default 4, 20-minute loop budget); smoke tests re-run after each to prevent regression. Hardened guardrails: NEVER modifies `server.js`, `middleware/auth.js`, `routes/auth.js`, `db/migrate.js`, login/logout/session logic, bcrypt, CSRF tokens, or layout nav structure.

## `cli/railway_cleanup.py`

CLI tool to delete duplicate Railway projects. Keeps newest per name, never deletes projects referenced in the DB.

```bash
python3 cli/railway_cleanup.py [--dry-run]
```
