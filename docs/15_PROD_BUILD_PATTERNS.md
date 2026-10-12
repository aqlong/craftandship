# Prod Build Bugs, Issues, and Patterns

**Started:** 2026-04-14
**Owner:** Ocean (founder) + Claude (co-pilot)
**Status:** Historical pattern library (written April 2026).
**Purpose:** Single source of truth for "what actually goes wrong in prod builds, and what patterns repeat." Feeds the prod build plan going forward.

This document is NOT a bug tracker (use `coordinator/roadmap.json` for that) and NOT a full postmortem index. It is the **pattern library**; "the same failure mode keeps appearing in different builds, here's what we know about it."

Scope: the CAS customer-build pipeline (`cli/*`, `queue/worker_pg.py`, `monitor/*`), as exercised on Railway prod. Out of scope: CAS web app bugs (`web/src/*`) unless they block builds.

---

## How to use this document

- **Reading it:** Start with §2 (Pattern Catalog). Each pattern has a name, a definition, an observation log, and a status. Open patterns are tracked as roadmap items.
- **Adding to it:** When a failed-build audit surfaces a new pattern seen in ≥2 builds, add it to §2 with the Observation Log table filled in. If you only have one observation, log it in §4 (Watch List) until a second instance confirms it.
- **Retiring an entry:** When a pattern is fixed AND has stayed fixed across ≥5 builds without recurrence, move it to §5 (Fixed Patterns) with the commit SHA and verification evidence.

Everything else goes in a dated Appendix section; raw audit output, per-build snapshots, one-off weirdness.

---

## 1. Executive snapshot

As of 2026-04-14 (post 7-day audit, see §7), the dominant prod build failure modes are:

| Pattern | Severity | Category | Count observed | Status |
|---|---|---|---|---|
| ZOMBIE_THREAD_STATUS_OVERWRITE1 | **High**, causes DB/reality drift; every "failed" build may be live | Supervisor / watchdog | 4 confirmed (MW v7, MW v8, build-3 MW, recipe-planner build-4) | Open, reconciled per-build; root cause fix TBD |
| DESIGN_REVIEW_QUALITY_GATE1 | Medium: terminal build fail when design_fix can't clear critical issues within MAX_DESIGN_FIXES | design_review loop | 2× (build-5 MW v7, build-4 recipe-planner DLQ) | **New in this audit** |
| AUTH_BOUNDARY_BUG1 | Medium, generated app exposes admin routes to non-admins, smoke test fails | Build prompt / auth middleware | 2× (build-6 review-gate-test, build-7 task-tracker first attempt) | **New in this audit, promoted from watchlist** |
| QA_FIX_HANG1 | High: 45-min wall-time burn before watchdog fires | qa_fix loop | 2× on MW v8 | Open |
| DEPLOY_CRASH1 | Medium: generated app crashes post-deploy, deploy_fix can't fix | deploy_fix loop | 2× (MW v8, build-7 task-tracker final state) | Open |
| SEED_PADDING_TYPE1 | Low: 3-5 rows rejected of ~60, spams deploy logs | Post-processor | 1× (MW v8) | Open |

This doc focuses on **the pattern view** (cross-build, how often, why).

**Critical operational realization (2026-04-14):** after a failed/hung build, the **app is often already live**. Before proposing any recovery work, hit `/health` on any Railway project matching `app_name` near the build time window. See §3 below.

---

## 2. Pattern Catalog

### 2.1 ZOMBIE_THREAD_STATUS_OVERWRITE1: Force-failed thread deploys successfully, DB stays `failed`

**Definition.** When WATCHDOG1 Layer 2 fires a terminal force-fail on a hung build, the build thread is not actually terminated, cooperative cancel works only at poll boundaries, not inside C-level blocks (e.g. stuck socket read in an Anthropic API call, Railway GraphQL call, subprocess wait). The zombie thread eventually unblocks and completes the rest of the pipeline: projectCreate, postgres deploy, web deploy, health check. All succeed. The only write-back it can't finish is the DB UPDATE of Railway IDs back to the `builds` row, either because the supervisor's `status='failed'` UPDATE already committed and the zombie's write collides with a WHERE-guard, or because the zombie's DB connection is stale.

**Net effect.** Railway has a fully working deployed app. The DB says `status=failed`, `error_category=timeout`, DLQ entry present, `railway_*_id` null, `preview_url` null. The two views of reality disagree completely.

**Why it matters.** Any session that starts a recovery flow from "build says failed" will propose artifact extraction + redeploy, which is total wasted motion. The correct path is a 6-row DB UPDATE. Until this is root-cause-fixed, EVERY failed-build investigation MUST start with a Railway probe.

**Observation log.**

| date | build | tier | app_name | when the zombie completed | app still live? | reconciled? |
|---|---|---|---|---|---|---|
| 2026-04-14 | `build-1` (MW v8) | build | mw-event-site | 19 min after the kill | ✅ 200 on /health + 6 spec routes | ✅ yes, prior session |
| 2026-04-13 | `build-2` (MW v7) | build | mw-event-site | unknown | ✅ 200 | ⏳ pending decision |
| 2026-04-13 | `build-3` (MW) | build | mw-event-site | 34 min after completion | ✅ 200 on /, /events, /videos, /gallery, /contact, /login | ⚠️ candidate; audit 2026-04-14 |
| 2026-04-11 | `build-4` | build | recipe-planner-sample | the Railway project was created inside the build's window; /health still 200 | ✅ /health 200 (root + /login intermittent 000) | ⚠️ candidate; audit 2026-04-14 |

**Proposed fixes (ranked).**
1. **Reality-check supervisor write.** Before the supervisor writes `status='failed'`, probe Railway for a project matching `app_name` created in `(started_at - 5min, NOW() + 30min)`. If found and `/health` returns 200, write `status='preview'` + IDs instead of `failed`. Cost: one Railway GraphQL list call per force-fail. Benefit: ends the pattern entirely.
2. **Pre-register projectId.** Make `deploy.py` INSERT the `railway_project_id` into `builds` BEFORE projectCreate completes its last network round-trip. Then even a dead zombie leaves a trail the DB can use to reconcile.
3. **Background reconciliation sweep.** A periodic coordinator agent scans 72h of failed builds whose error_message contains supervisor/timeout/force-fail keywords, probes Railway, and flips confirmed-live rows to `preview` when a matching project is healthy.

Option 1 is cheapest and most surgical. Option 3 is a safety net even if Option 1 lands.

**Shipped fix (Option 3, `ReconciliationAgent`, coordinator/agents/reconciliation.py).** Background sweep disabled by default (`AGENT_RECONCILIATION_ENABLED=false`); set to `true` on the coordinator worker to activate. Runs every 2h (configurable via `COORDINATOR_RECONCILIATION_INTERVAL_H`). Queries builds with `status='failed'`, `completed_at > NOW() - 72h`, error_message matching zombie indicators; calls `probe_for_zombie_deploy` from `queue/watchdog_probe.py` for each candidate (leaving out Railway projects that other build rows with the same `app_name` already own); for each build confirmed live it files a `reconciliation_flip_to_preview` approval request and sends one alert email. It writes no build row: moving the build to `preview` is left to the person who reads the request, and approval requests are advisory (nothing in this repository executes one). Dry-run respected (nothing is filed or sent). Probe reuses the same shared implementation as Option 1, no duplication.

**Shipped fix (Option 1).** `_supervisor_reality_probe()` runs before every supervisor force-fail write (`queue/worker_pg.py`). Lists Railway projects, filters to `name == app_name` created in `(started_at - 5min, completed_at + 30min)`, resolves the first service domain via `get_service_domain` in `cli/deploy.py`, and curls `https://{domain}/health` with a 20s hard deadline + 5s per-URL timeout. The probe runs from `_supervisor_force_fail` after it has read the build row and with no row lock held; projects that other build rows with the same `app_name` already own are excluded. On 200 the build goes to `status='preview'` through `_set_preview_status` (with the initial `review_status` and `preview_url` set to the probed URL, and any earlier review verdict cleared) instead of `failed`; the probed Railway project and service ids are recorded by that same statement (only a note is written afterwards, in a second one), and a Sentry warning `"supervisor reality-check recovered zombie deploy"` is emitted. `live_url` is not set: only the web app publishes a build. Fail-safe: any probe error / timeout / unreachable domain falls through to the original failed-path write.

**Runbook: zombie recovery in build_logs.** When the supervisor reality-check fires, the customer-facing build_logs entry is:

> *"We detected that your website actually deployed successfully. Your preview is ready for review."*

**If this message appears in the build_logs UI, verify the live app before auto-promoting; the supervisor force-failed mid-deploy but the zombie thread completed it.** Concretely: hit `/health` and `/login` on the probed `live_url`, confirm 200 + DOM looks sane, then approve via `/admin/reviews/:buildId`. The reality-check is conservative (checks only `/health`); the rest of the app may still have partial state (missing seed data, half-applied migrations) that only shows up on deeper routes. Do not rely on the probe alone for auto-promotion until we have a broader multi-route reality-check.

**Sentry alert rule (manual setup: no management API token wired).** Create this rule in the Sentry UI (worker project):

- **Name:** `Zombie deploy recovered by supervisor reality-check`
- **When:** An event is captured
- **If:** `message` contains `supervisor reality-check recovered zombie deploy`
- **And:** `level` equals `warning`
- **And:** `environment` equals `production`
- **Then:** Send notification to `ADMIN_EMAIL` (+ Slack if/when wired)
- **Frequency:** Every occurrence (low volume, only fires on actual zombie hits)

Rationale: every fire of this alert represents a supervisor-write that caught a live zombie deploy. We want direct visibility to (a) validate the probe logic in production, (b) catch false positives before auto-promotion, (c) track frequency as input to the upstream QA_FIX_HANG1 fix priority. Low false-positive risk; the message is scoped to a specific log line emitted only inside `_supervisor_reality_probe`.

---

### 2.2 QA_FIX_HANG1: qa_fix enters C-level blocked state no watchdog under the 45-min tier limit can unblock

**Definition.** The qa_fix loop applies a fix, then calls `run_qa()` with `build_id` threaded through. The subprocess (Playwright), the DB setup, or the Anthropic API call stalls at a C-level blocking primitive. qa_fix's 20-min budget fires `FixLoopBudgetExceeded`, but the Python-level raise never happens because the interpreter is waiting on a syscall that hasn't returned. Cooperative cancel is polled at Python-level boundaries only. Watchdog Layer 2 (45m tier deadline + 30s grace) eventually force-fails, which → ZOMBIE_THREAD_STATUS_OVERWRITE1.

**Observation log.**

| date | build | attempt | hang location (suspected) | resolution |
|---|---|---|---|---|
| 2026-04-14 | MW v8 `build-1` | attempt 1, qa_fix iteration 4 | Playwright subprocess or Anthropic API | supervisor force-fail, one free retry |
| 2026-04-14 | MW v8 `build-1` | attempt 3, after "reverting adjustment" | unknown | supervisor force-fail, terminal |
| 2026-04-13 | `build-7` task-tracker | retry 2 | C-level block (per DLQ msg) | supervisor timeout, 1 free retry, later final fail DEPLOY_CRASH1 |
| 2026-04-11 | `build-4` recipe-planner-sample | attempt with deploy_fix | **deploy_fix** Claude call hung 59 min (WATCHDOG1 Layer 2 gap; sibling pattern to qa_fix hang) | force-fail; Railway project still live (zombie) |

**Proposed fixes.**
1. **Process-tree SIGKILL on qa subprocess timeout.** Wrap `run_qa()` subprocess launches with `subprocess.run(..., timeout=X)` that SIGKILLs the whole process tree, not just the parent. Already partially enforced by static test in `test_subprocess_timeouts.py`; audit the actual qa.py call sites.
2. **httpx timeout audit.** `make_client()` factory sets `httpx.Timeout(connect=10, read=300, write=60, pool=10)`. Verify qa_fix.py's Anthropic client uses `make_client()` and not a bare `anthropic.Anthropic(...)`; the static test in `test_api_client.py` enforces this, but it may not cover every call path.
3. **Hard-kill qa_fix iteration on budget.** When the 20-min budget fires, if the current iteration isn't done, SIGKILL qa subprocesses instead of waiting for cooperative exit.

**Shipped fix (Option 3: `kill_proc_group`, cli/qa_fix.py + queue/worker_pg.py).** `_run_node_syntax_check()` in qa_fix.py launches `node --check server.js` with `start_new_session=True` (new Unix session/process group). Module-level `_last_qa_proc` stores the handle. When `FixLoopBudgetExceeded` fires in worker_pg.py's qa_fix budget handler, `kill_proc_group(_last_qa_proc)` is called: `os.getpgid(proc.pid)` then `os.killpg(pgid, SIGKILL)`. `ProcessLookupError` (process already gone) is silently swallowed; `PermissionError` is logged as a warning and not re-raised. The node syntax-check is a bounded substitute for full Playwright; it acts as the process-group anchor so any surviving Chromium children in the same session are also killed on budget.

---

### 2.3 DEPLOY_CRASH1: Generated app crashes on startup after successful tests; deploy_fix exhausts 3 attempts

**Definition.** Build passes all local smoke tests (20/20), passes design review, uploads to Railway, Railway reports successful deploy. Then `/health` fails; the app is up briefly and crashes on startup. deploy_fix.py reads Railway logs, asks Claude to patch, redeploys. Repeat × 3 (MAX_DEPLOY_FIXES=3). Still crashing. Worker gives up and triggers a full fresh code-gen (retry_count increment).

**Observation log.**

| date | build | attempt | deploy_fix attempts | crash cause (diagnosed?) |
|---|---|---|---|---|
| 2026-04-14 | MW v8 `build-1` | attempt 2 | 3 (all failed) | ❌ Railway logs not captured to build_logs table |
| 2026-04-13 | `build-7` task-tracker | terminal retry | 3 (all failed) | ❌ Railway project not found (auto-cleanup or never created after crash); logs unrecoverable |

**Root cause hypothesis.** Possibilities: (a) race between app startup and DB migration completion; (b) env var set locally but not on Railway; (c) a post-processor fix that works in test environment but breaks under Railway's Node version / memory limit; (d) schema mismatch between local-tested DB and Railway fresh-provisioned DB.

**Proposed fixes.**
1. **Capture Railway deploy logs to `build_logs`.** Call `deploymentLogs` GraphQL during the post-deploy health-check window, grab last 100 lines, append to `build_logs` with stage='deploy-check'. Makes crashes debuggable from DB alone.
2. **Fresh-service retry differentiation.** deploy_fix currently retries against the same Railway service. Add a path: if 3 deploy_fix attempts fail, destroy the Railway project and redeploy fresh. Distinguishes "code broken" from "this particular Railway service is in a bad state."
3. **Pre-deploy schema drift check.** Before deploying to Railway, compare schema.sql against what `db/migrate.js` will actually produce given an empty DB. Most "crashes on startup" are schema migration edge cases.

---

### 2.4 SEED_PADDING_TYPE1: `_ensure_minimum_seed_rows()` writes text suffixes into non-text columns

**Definition.** Post-processor `_ensure_minimum_seed_rows()` pads data tables below 15 rows by duplicating existing seed rows with modified values; it appends strings like `(evening session)`, `(morning session)`, `(weekend edition)` to a column to make the new row "distinct." This works for text columns but fails on `interval`, `timestamp`, numeric, and probably UUID columns. Failed rows are rejected by postgres, leaving the seed row count below the 15 minimum target and spamming the deploy log.

**Observation log.**

| date | build | errors in log | estimated padding rows lost |
|---|---|---|---|
| 2026-04-14 | MW v8 `build-1` | 3× "invalid input syntax for type interval" + 2× "column id integer but expression uuid" | 5 of ~60 |

**Proposed fix.**
1. Per-column type awareness in the padder; look up the column type via schema.sql, only suffix-mutate text/varchar/json, NULL out or offset numeric/interval/timestamp columns.
2. Extend `_fix_seed_uuid_id()` coverage: MW v8 had 2 tables still leaking UUID→integer mismatches in padded rows, so the UUID fix doesn't chain through the padder.

Low severity but easy wins.

---

### 2.5 DESIGN_REVIEW_QUALITY_GATE1: design_review blocks deploy; design_fix exhausts MAX_DESIGN_FIXES

**Definition.** `design_review.py` returns critical issues. `design_fix.py` runs up to `MAX_DESIGN_FIXES` times (default 3). After the final pass, `design_review.py` still reports critical issues. Worker marks the build `failed` with error `"Design review: N critical issues after fix attempts"`, DLQ category `code_error`. Never reaches deploy, so no Railway project exists.

**Key distinction from ZOMBIE_THREAD_STATUS_OVERWRITE1.** This is a legitimate failure: no zombie, no orphan project, DB reflects reality. Customer's build genuinely does not meet visual-quality gate.

**Observation log.**

| date | build | tier | app_name | critical issues unresolved | design_review status |
|---|---|---|---|---|---|
| 2026-04-14 | `build-5` (MW v7) | build | mw-event-site | 3 | warned |
| 2026-04-11 | `build-4` | build | recipe-planner-sample | 1 | warned (also zombie-completed separately) |

**Why it matters.** Currently terminal: the customer sees "failed" with no recovery path. Before a wider launch, need either (a) auto-rebuild trigger, (b) customer-facing "close but needs human review" status, or (c) SE queue entry with the partial build for manual triage.

**Proposed fixes (ranked).**
1. **Route DESIGN_REVIEW_QUALITY_GATE1 failures to SE review queue, not terminal fail.** Mark `review_status='changes_requested'`, attach design review critical issues as `review_notes`, let SE decide. Converts a terminal fail into a soft-fail with human recovery path; aligns with "Craft promise" policy.
2. **Auto-rebuild after N failures with a stronger design prompt.** Feed the unresolved critical issues as `rebuild_request` into a fresh build. Has cost risk; add a cap.
3. **Capture the failing render to screenshots + build_logs.** Today the failure is text-only; adding screenshots would let SE decide in 30s.

---

### 2.6 AUTH_BOUNDARY_BUG1: generated app exposes admin routes to non-admin users (test 8 fails)

**Definition.** Smoke test 8 (`user cannot access admin-only pages`) expects `401/403/redirect` at `/dashboard` or admin routes for non-admin sessions; generated app returns `200`. Build's `requireAdmin` middleware is missing, mounted on the wrong router, or the spec's auth_required + roles was not translated into real middleware on those routes. `fix.py` gets one attempt, can't fix. Build moves to DLQ with `error_category='infra'` (Railway infra category, inaccurate; this is a code bug in the generated app).

**Observation log.**

| date | build | tier | app_name | failing path | fix attempts |
|---|---|---|---|---|---|
| 2026-04-12 | `build-6` review-gate-test | build | review-gate-test | /dashboard (200 instead of 401/403) | 1 |
| 2026-04-12 | `build-7` task-tracker | build | task-tracker | /dashboard (same error, 200 instead of 401/403) | 1 (this was retry 1; later retries hit timeout + deploy crash) |

Both builds produce the IDENTICAL error message verbatim, suggesting a systemic flaw in how the build prompt translates admin-only page specs into middleware.

**Proposed fixes (ranked).**
1. **Post-processor: verify every admin-only route mounts `requireAdmin`.** Scan `routes/*.js` for routes that match spec.pages entries with `roles: ['admin']` or `auth_required: admin`, assert the handler chain includes `requireAdmin`. If missing, inject it. Template-style fix, low risk.
2. **DLQ miscategorization.** Reclassify this pattern under `code_error`, not `infra`. `infra` means Railway; a missing middleware is a generated-code bug. Wrong category hides the pattern from failure_kb.
3. **Add the failing pattern to `failure_kb` seed.** Record error signature `"user cannot access admin-only pages.*expected 401/403.*got 200"` with the fix rule "ensure `requireAdmin` on all admin.ejs pages and admin-scoped routers."

**Miscategorization note.** Both instances of this bug have `error_category='infra'` in DLQ, which is incorrect. `infra` should be reserved for Railway / network / platform issues. This is a generated-code bug → `code_error`. Fixing the category would let `failure_kb.record_fix()` learn from it.

---

## 3. Diagnostic playbook

### 3.1 For any failed/hung/timeout build: check reality first

Before any recovery proposal, run this sequence. 

```bash
TOKEN="<your Railway API token>"
APP_NAME="<build.app_name>"
# 1. List Railway projects matching app_name
curl -s -X POST https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"query { projects { edges { node { id name createdAt } } } }"}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); [print(n['node']['id'], n['node']['createdAt']) for n in d['data']['projects']['edges'] if n['node']['name']==\"$APP_NAME\"]"

# 2. Filter to projects created in (build.started_at - 5min, build.completed_at + 30min)
# 3. For each candidate project: list services -> deployments -> staticUrl
# 4. curl /health and /login on each staticUrl
# 5. If 200 on /health, the app is live; do DB reconciliation, not redeploy
```

### 3.2 Build reconciliation template

Only after Railway probe confirms the app is live. Uses `update_builds_updated_at` trigger so don't set `updated_at` manually. Preview convention: `preview_url` set, `live_url=NULL` until SE approves.

```sql
BEGIN;
UPDATE builds SET
  railway_project_id     = '<uuid>',
  railway_service_id     = '<uuid>',
  railway_environment_id = '<uuid>',
  preview_url            = '<staticUrl>',
  status                 = 'preview',
  review_status          = 'pending',
  error_message          = NULL
WHERE id = '<build_id>';
DELETE FROM dead_letter_builds WHERE build_id = '<build_id>';
COMMIT;
```

Verify across 5 surfaces: (a) DB row persists; (b) DLQ empty; (c) ops API reflects state; (d) admin /admin/reviews queue shows it; (e) live Railway app still healthy.

### 3.3 When to close a review with `changes_requested` (not approve, not delete)

Use when an older build is superseded by a newer one that's also in pending, OR when the build has a known visual regression that a newer pipeline revision will fix. Direct SQL UPDATE bypasses the customer email:

```sql
UPDATE builds SET
  review_status = 'changes_requested',
  reviewed_at   = NOW(),
  reviewed_by   = '<admin email>',
  review_notes  = 'Superseded by <newer_build_id>. Close old review.'
WHERE id = '<old_build_id>'
  AND status = 'preview'
  AND review_status = 'pending';
```

WHERE clause prevents racing against an SE who just approved/rejected.

---

## 4. Watch list (one-off observations; promote to §2 if seen twice)

| date | build | observation |
|---|---|---|
| 2026-04-14 | MW v8 | "Reverting adjustment..." log line appeared mid-qa_fix before the hang; might be a signal something in qa_fix's snapshot/restore triggers the hang. |
| 2026-04-12 | `build-6` review-gate-test | `preview_url` and `live_url` both set to the canonical pattern `https://<app>.up.railway.app` (without Railway's generated suffix) although the Railway project no longer existed; suggests the DB was populated with a placeholder URL, not the real staticUrl. URL format regression or SE review gate self-test scaffolding. |
| 2026-04-13 | `build-7` task-tracker | One DLQ entry per retry, each a DIFFERENT failure mode (auth-boundary → supervisor-timeout → deploy-crash). A single build exercising 3 patterns in one lifetime is unusual and suggests the retry loop is not learning from prior DLQ entries. |
| 2026-04-13 | `build-3` MW | Marked `failed` with message `"Build has prior supervisor timeout failure; not re-queueing to avoid infinite retry loop across worker restarts."`; no DLQ entry for THIS row, only pre-existing ones. This is the "retry-ceiling" terminal path. An orphan MW project serving the full spec at 200 suggests one of the earlier retries did successfully deploy. |

---

## 5. Fixed patterns

| Pattern | Fixed | Regression tests |
|---|---|---|
| DARK_TEXT_GRADIENT1 (dark text on primary-gradient sections) | 2026-04-13 | 8 tests in `TestFixDarkSectionText` |
| EJS_FAVICON1 (`">` rendered at top of page) | 2026-04-14 | 2 tests in `TestEnsureLogoPlacement` |
| HERO_SETTINGS1 (hero h1/p not bound to site_settings) | 2026-04-13 | Regression tests in `TestEnsureHeroSettingsBinding` |
| USER_SEED1 (UUID vs integer id mismatch empties tables; fixed by `_fix_seed_uuid_id()`) | 2026-04-12 | 12 tests in `TestFixSeedUuidId` |
| VIDEO_THUMB_PROXY1 (YouTube thumbnails broken; fixed in `_ensure_video_embeds()` Phase 3) | 2026-04-12 | 4 tests |
| WATCHDOG1 Layer 1 (no outer supervisor) | 2026-04-10 | 3 tests in `TestWatchdogSupervisor` |
| WATCHDOG1 Layer 2 (cooperative cancel) | 2026-04-12 | +109 tests |

---

## 6. Open audit (tracking)

### 6.1 7-day failed build audit: COMPLETED 2026-04-14 (see §7)

The audit queried all builds with `status='failed'` (and `dead_letter_builds`) in the last 7 days, ran the §3.1 diagnostic playbook against each to check whether the app was actually live, classified the failure pattern, and produced a per-build table (see §7).

### 6.2 Longer-term

- Fix ZOMBIE_THREAD_STATUS_OVERWRITE1 at root (see §2.1 ranked fixes).
- Fix QA_FIX_HANG1 subprocess tree kill (see §2.2 ranked fixes).
- Capture Railway deploy logs → build_logs (see §2.3 ranked fixes).
- Clean up unlinked Railway orphans via `cli/railway_cleanup.py --dry-run`.

---

## 7. Audit appendix (raw per-build data)

### 7.1 Failed-build audit of one week in April 2026

Window: builds created in the last 7 days with `status='failed'`. Read-only audit; nothing reconciled, nothing redeployed, nothing deleted. All DB and Railway state unchanged by this session.

Already-reconciled builds (e.g. MW v8 `build-1`, which is now `status='preview'`) are intentionally excluded by the WHERE clause; see §2.1 observation log for their record.

#### Per-build findings

| build_id (short) | app_name | tier | failed on | classification | actually live? | preview URL |
|---|---|---|---|---|---|---|
| `build-5` | mw-event-site | build | 2026-04-14 | DESIGN_REVIEW_QUALITY_GATE1 (new §2.5) | ❌ no (never deployed, design gate stopped it) | - |
| `build-3` | mw-event-site | build | 2026-04-13 | ZOMBIE_THREAD_STATUS_OVERWRITE1 (strong) | ✅ yes, `https://<app>.up.railway.app` serves all 6 probed spec routes | `https://<app>.up.railway.app` |
| `build-7` | task-tracker | build | 2026-04-13 | DEPLOY_CRASH1 (terminal) + AUTH_BOUNDARY_BUG1 (retry 1) + QA_FIX_HANG1 (retry 2) | ❌ no (Railway project not found under that name; auto-cleanup or never persisted) | - |
| `build-6` | review-gate-test | build | 2026-04-12 | AUTH_BOUNDARY_BUG1 (new §2.6) | ❌ no (Railway project gone; `.up.railway.app` → 404) | `https://<app>.up.railway.app` (dead placeholder in DB) |
| `build-4` | recipe-planner-sample | build | 2026-04-11 | ZOMBIE_THREAD_STATUS_OVERWRITE1 (confirmed, deploy_fix Claude hang) + DESIGN_REVIEW_QUALITY_GATE1 (DLQ) | ✅ partial, `/health` returns 200, `/` and `/login` intermittent 000 (DB connect delay or cold start). The Railway project was created inside the build's window. | `https://<app>.up.railway.app` |

#### What the week showed

- **ZOMBIE_THREAD_STATUS_OVERWRITE1** appeared in more than one build; each had a live Railway project that predated the DB-failed write.
- **DESIGN_REVIEW_QUALITY_GATE1** (new §2.5) appeared on a build that never deployed and, as a secondary classification through the DLQ message ("Design review: 1 critical issues after fix attempts"), on a second.
- **AUTH_BOUNDARY_BUG1** (new §2.6) appeared on two builds with an identical error string, so the root cause is in the build prompt.
- **QA_FIX_HANG1** and **DEPLOY_CRASH1** each appeared once in the window, both on `build-7`.
- **SEED_PADDING_TYPE1** had no new occurrence (MW v8 `build-1` is already in §2.4), and no failure was transient.

(Patterns overlap: e.g. `build-7` hits AUTH_BOUNDARY_BUG1 → QA_FIX_HANG1 → DEPLOY_CRASH1 across its retries. Each pattern is counted once.)

#### Reconciliation candidates (nothing was written by this audit)

| build_id | app_name | candidate preview URL | proposed action | confidence |
|---|---|---|---|---|
| `build-4` | recipe-planner-sample | `https://<app>.up.railway.app` | Reconcile `status='preview'`, `review_status='pending'`, populate Railway IDs, but first re-probe `/` and `/login` (intermittent 000s could mean the DB ran out of credits or cold-start); do NOT reconcile if the app is truly flaky | Medium, /health 200 but root routes timing out |
| `build-3` | mw-event-site | `https://<app>.up.railway.app` | Reconcile `status='preview'`, `review_status='pending'`. Strong candidate: 6/7 spec routes return 200, Railway project created 34 min after DB-failed write (classic zombie timing) | High; confirmed spec routes live |

#### New patterns promoted in this audit

- **DESIGN_REVIEW_QUALITY_GATE1** (§2.5): observed 2×, now a first-class pattern.
- **AUTH_BOUNDARY_BUG1** (§2.6): observed 2×, now a first-class pattern. Identical error string across 2 builds suggests a systemic prompt-template gap.

#### Categorization bugs surfaced

Both AUTH_BOUNDARY_BUG1 observations have DLQ `error_category='infra'`, but they are generated-code bugs (missing `requireAdmin` middleware). `infra` should be Railway/platform only. Until this is fixed, `failure_kb.record_fix()` won't learn the rule, and the same bug will keep recurring.

#### Top 3 recommendations for the prod build plan

1. **Ship the §2.1 Option 1 fix (reality-check supervisor write) this week.** Several of the failed builds in the window are the ZOMBIE pattern, and each required manual Railway probing to detect. One Railway GraphQL list call per force-fail is cheap; the operational win is huge (ends an entire failure class and every customer-visible "failed" email for it).
2. **Add a post-processor that verifies `requireAdmin` middleware on admin-only routes (§2.6 Option 1).** AUTH_BOUNDARY_BUG1 hit more than one build. Pattern is simple enough to template-fix deterministically; does not require a Claude round-trip.
3. **Route DESIGN_REVIEW_QUALITY_GATE1 terminal failures into the SE review queue instead of `status='failed'`.** See §2.5 Option 1. Aligns with the "Craft promise" policy (human review at every tier) and converts today's dead-end into soft-fail with recovery.

Honorable mention (small but easy): **fix DLQ `error_category` for auth-boundary / code-bug failures**, reclassify as `code_error`, not `infra`. Lets `failure_kb` learn.


---

## References

- WATCHDOG1 Layer 2 (cooperative cancellation and the supervisor retry) as it shipped: `docs/internals/worker.md`.
