# Build queue worker: queue/worker_pg.py

> Historical document from April 2026; the system is no longer developed and the site was taken offline in October 2026.

> **Scope:** detailed behavior of the PostgreSQL-backed build queue worker. Extracted from CLAUDE.md to keep the root context file focused on load-bearing decisions. Consult this file when investigating retry logic, concurrency, timeouts, orphan recovery, or any worker-internal behavior.
>
> **Companion files:**
> - `docs/internals/pipeline.md` - `cli/*.py` module details
> - `docs/internals/post-processors.md`: the deterministic fixes (117 `@processor` functions registered in `cli/build.py`)
> - `docs/internals/coordinator.md`: autonomous agent framework
> - `queue/mailer.py`, `queue/watchdog_probe.py`: covered at the end of this file

## Core loop

- Polls `builds` table every 10s (`_POLL_INTERVAL`), `SELECT FOR UPDATE SKIP LOCKED` for atomic job claim, and claims only when one of its own build threads is free (`_claim_and_dispatch`; see Concurrency).
- Runs full pipeline on each job, sends emails on success/fail.

## Status contract

A build belongs to a worker thread only while its status is `building`, `testing` or `deploying`. Every status write the worker makes is one `UPDATE` with two guards (see the comment block above `BuildNoLongerOwnedError` in `queue/worker_pg.py`).

- **Guard by status.** `_STAGE_ORDER` is `("building", "testing", "deploying")`. `_set_status` writes only those three values (anything else raises `ValueError`) and matches only a build that is in the same stage or an earlier one, so a write can neither move a build backwards nor touch one that has left the worker's hands. `failed` is not accepted by `_set_status` at all. `_set_preview_status`, `_requeue_for_retry`, `_fail_build` and the supervisor act only on a build that is still in one of the three stages; none of them acts on `queued`. The worker never writes `live`: promotion belongs to the web app, after review.
- **Guard by attempt.** The claim sets `builds.started_at = NOW()` and returns it as `job["attempt"]`; every requeue (the worker's own, orphan recovery, and the web app's retry, reset and requeue routes) clears `started_at`, so the next claim stamps a new value.
  - Each guarded write also requires `started_at` to equal the writer's attempt (`started_at IS NOT DISTINCT FROM %s::timestamp`), so a thread left over from an earlier attempt, such as one the supervisor gave up on, cannot advance, preview, requeue or fail a later attempt.
  - The token is not optional: `_attempt_param` raises `ValueError` for `None`, so a caller that lost its token cannot fall back to the status guard alone, and a claim that returned no token returns no job.
  - The one writer that can meet a row with no `started_at`, orphan recovery, says so with `ROW_WITHOUT_STARTED_AT`, and its write then matches only a row whose `started_at` is still NULL; otherwise it guards its writes with the `started_at` it read.
- **What the attempt guard does not cover.** It covers the status writes and the Railway columns, which only the preview write sets (see Railway projects below). `build_logs` rows, screenshots, the stored artifact and cost are keyed by build id alone, so a leftover thread can still write those until it reaches its next stage write.
- **Stage writes inside the pipeline** go through `_advance_stage`, which raises `BuildNoLongerOwnedError` when the guarded write matches no row (an admin killed the build, or another writer failed, requeued or previewed it). `_run_pipeline` stops there, before it deploys anything, and `_process_job` drops the job with no retry, no dead-letter row, no email and no Sentry event.
- **Arrival at preview** is `_set_preview_status`, one guarded `UPDATE` that sets `status = 'preview'`, `preview_url` and the initial `review_status` (`pending`, or `ai_review_queued` when `CAS_HARBOR_AUTO_REVIEW=true`; `_initial_review_status`), clears what an earlier review recorded (`reviewed_at`, `reviewed_by`, `review_notes`, `review_report`, `ai_review_iterations`, `ai_reviewed_at`) and, in the same statement, records the Railway ids of the deployment being announced.
  - A review verdict belongs to the deployment that was reviewed, so a rebuild goes back into review instead of keeping an old `approved`.
  - It returns True only when it moved the row; the caller then runs `_announce_preview`, which registers the app with the monitor, logs "ready for review", sends the admin's `send_review_pending` email (once per arrival at preview, so again after every rebuild, and never when auto-review is on), runs the janitor and sends the customer's preview email.
  - The normal path and the supervisor's zombie recovery both end in `_announce_preview`, so a deploy that a stuck thread finished is announced like any other.
  - If the write matched no row (a duplicate call, or a build killed while deploying), nothing is announced: no log line, no monitor registration, no email.
- **Terminal failure** is `_fail_build`, the only way the worker sets `failed` (`_MARK_FAILED_SQL`).
  - One transaction inserts a `dead_letter_builds` row and runs the guarded flip; if the flip matches no row, the transaction is rolled back and nothing else happens (no customer email).
  - If the transaction itself errors, the flip is retried alone; if that fails too, `_fail_build` returns False and tells nobody, because the build is still in an active status and orphan recovery will find it.
  - Pipeline errors with no retry left, the supervisor's terminal timeout, the permanent failures of orphan recovery and a crash of the build thread (`_run_build_in_thread`) all end here, so each leaves a dead-letter row and the customer's "build failed" email.
  - `_fail_build` never touches Railway: the project recorded on the row is the deployment the customer was last told about, and after an in-place requeue of a build that was in preview that is the previous, working site. The project the failing attempt itself created, if any, is removed by the attempt (see Railway projects).
- **Requeue** is `_requeue_for_retry` (pipeline errors), the supervisor's free retry, and orphan recovery; each is a guarded `UPDATE ... SET status = 'queued'` that clears `started_at` and `completed_at`.

## Timeouts

Tier-based: **build=60m, grow=90m, scale=120m** (`_TIER_TIMEOUTS`; raised from 45/60/90 on 2026-04-20 after customer apps hit the 45m ceiling during Anthropic throttle + deploy_fix loops). The tier selects only this timeout; it does not affect which queued build is claimed first.

- The sole timeout mechanism is the supervisor in `_process_job`: the pipeline runs in a one-thread `ThreadPoolExecutor` and the supervisor waits on `future.result(timeout=<tier timeout> + _SUPERVISOR_GRACE_SECONDS)` (30s).
- Cooperative cancellation (`cli/cancellation.py`, polled by `_check_cancelled()` in `cli/api_client.py`) lets fix loops and API retries stop when they next reach a checkpoint.
- Each attempt registers its own event under a key made by `_new_cancel_key` (the build id plus a random suffix), keeps it in `job["cancel_key"]`, and passes it as `cancel_key=` to the five consumers (`cli/fix.py`, `qa_fix.py`, `design_fix.py`, `deploy_fix.py`, `ui_test.py`) and through them to `call_api`; the pipeline future's done-callback drops the event when the pipeline thread ends, not when `_process_job` returns, because the thread the supervisor gave up on is the one that still has to see it.
- `BuildCancelled` is re-raised past the "non-fatal" handlers around the fix loops.
- Only the v1 fix loops and model calls have checkpoints: the v2 pipeline has none, and a thread blocked in a C-level call, the Railway CLI, a socket read or Chromium cannot be interrupted, so it is abandoned and `_supervisor_force_fail` decides the build's fate on a fresh DB connection.
  - It fires the attempt's cancel event, reads the row in a short transaction that it commits at once, runs the zombie reality probe (`queue/watchdog_probe.py`, below) with no row lock and no open transaction, and then writes one outcome as a guarded `UPDATE` (status in a worker-owned stage, and still the same attempt).
  - An admin kill or a requeue that lands during the probe fails a guard, the supervisor writes nothing, and it reports `committed=False`.
  - The outcomes are
    - `preview` (the probe found a healthy deployment; the build goes through `_set_preview_status` into review),
    - a first-hang free retry (requeued once with `supervisor_retry_used = TRUE`, `retry_count + 1` and no dead-letter row; it needs `retry_count < _MAX_RETRIES`)
    - and `failed` (a second hang, or no retries left: `_fail_build` with category `timeout`, which writes the dead-letter row and emails the customer; it deletes no Railway project, because the abandoned thread may still be inside a Railway call and removes its own unannounced project when it ends).

**Per-loop wall-clock budgets** (`_FIX_LOOP_BUDGETS_S`): fix=30m, qa_fix=20m, design_fix=20m, deploy_fix=15m (tightened 2026-04-20 after a build was stalled by Anthropic API throttling; build-tier supervisor budget is 60m so this leaves 45m headroom for the supervised retry). When a loop exceeds its budget, `FixLoopBudgetExceeded` is raised. For `fix` / `design_fix` / `deploy_fix` this re-raises as a code_error (retry). `qa_fix` budget exceedance ALSO fails the build by default (see gate below).

**`deploy_fix` per-call Anthropic cap** (`CAS_DEPLOY_FIX_CALL_BUDGET_S` in `cli/deploy_fix.py`, default 7m, clamp 5s..900s): the single Anthropic call inside each `deploy_fix` iteration is budgeted below the loop ceiling so one retry storm cannot exhaust the whole 15m window. On cap the call raises `CallApiWallClockExceeded` (subclass of `anthropic.APITimeoutError`); the worker's `except Exception: break` handler exits the loop and subsequent iterations do NOT run, preserving existing content.

## Deploy-blocking gates (added 2026-04-20)

Three env-gated gates turn previously-silent failure modes into clean build failures so the normal code-error retry path can regenerate instead of shipping broken code.

- **`BLOCK_ON_CODE_REVIEW_CRITICAL`** (default `true`): Stage 2.5 code review now fails the build when any critical finding matches the narrow `_BLOCKING_REVIEW_PATTERNS` regex list (column mismatch / schema defines the columns as / column does not exist / missing route / schema-query drift / skipped seed / seed failure). Raises `ReviewCriticalBlockError`. Nitpicks (style, comments, naming) still pass through so fix loops have a shot at them.
- **`BLOCK_ON_QA_FIX_BUDGET`** (default `true`): `qa_fix` `FixLoopBudgetExceeded` AND `CallApiWallClockExceeded` / `anthropic.APITimeoutError` now re-raise instead of swallowing as warnings. Rationale: a hung API call means the fix did NOT land, so proceeding to deploy is guaranteed wrong. Non-timeout QA errors still degrade gracefully.
- **`BLOCK_ON_SCHEMA_DRIFT`** (default `true`): `_verify_schema_column_references` post-processor parses `db/schema.sql` CREATE TABLE blocks, scans JS/EJS for `SELECT cols FROM t` / `INSERT INTO t (cols)` / `UPDATE t SET col` references, and raises `SchemaColumnDriftError` if any `(table, column)` pair isn't in the schema. Build phase only; reapply never fails an already-built app (would block self-healing). Skips SELECT *, aggregates, subqueries, and tables not in the schema (CTE / alias / temp table).

Rollback any one of them to `false` for emergency pass-through; each logs a clearly-marked "continuing anyway" warning.

## Admin surface contrast + route guards (added 2026-04-20)

Three narrow post-processors ensure the generated admin surface renders legibly regardless of Claude's exact output:

- **`_fix_nav_brand_contrast_and_dedupe`** (cli/build.py), when the first `<nav>` is primary-colored, rewrites inline `color: var(--color-primary | --color-foreground | --color-text)` on descendant elements to `color: var(--color-on-primary, #fff)`. Also dedupes the brand anchor when `_ensure_nav_brand_element` injects an orphan next to Claude's unmarked wordmark.
- **`OUTLINE_BUTTON_CONTRAST` block** (appended to `public/style.css` by `_fix_delete_button_styling`); undoes the blanket `button[type="submit"]` safety-net bg fill when a button carries an inline `style="border-color: ...; color: ..."` (Claude's ghost/outline pattern). Without this, Publish/Unpublish toggles render as solid maroon blocks with invisible text.
- **`cli/templates/views_admin_settings.ejs` back-link**: points at `/dashboard` (not `/admin`, which isn't a real route).

## Retry / Self-healing

- Transient and code errors auto-retry up to 6x (`_MAX_RETRIES=6`).
  - `builds.retry_count` is the one attempt counter: a pipeline-error requeue (`_requeue_for_retry`, which writes `Retry n/6: <error>` to `error_message`), the supervisor's free retry, and an orphan restart that has nothing to resume from each use one retry, against the same cap.
  - Exceptions to the cap in `_process_job`: QA and generic quality-gate failures are not retried, design quality-gate failures and infrastructure errors (npm install, ENOENT, ENOSPC, OOM kill; `_INFRA_ERROR_KEYWORDS`, matched as substrings) get one retry, a failure tagged `[v2-deterministic]` or carrying `Spec validation failed` is not retried (`_is_deterministic_failure`), a deployed app that never answers `/health` healthy counts as a transient failure (`_DEPLOY_NEVER_HEALTHY`, whose text contains "timeout"), and an error identical to the previous attempt's is not retried unless it is transient.
- Code errors clear the build artifact and the on-disk build directory and rebuild fresh; transient errors requeue as is, keeping the stored artifact and the files on disk, so the next claim resumes from the test stage (see Stage resume below).
- **2-hour retry budget** ends a series of retries early (`_RETRY_TIME_BUDGET_S`; `_MAX_RETRIES` is the hard bound). The clock starts at the first attempt of the series (a claim with `retry_count` 0, so a new build or one a person requeued) and is kept in the worker process (`_note_attempt_start`, `_retry_series_age_s`, `_end_retry_series`), measured with `time.monotonic()`; a worker restart, or another worker claiming the retry, starts it again. The age of the build row is no measure of it: a row is created when the intake conversation starts, often days before it is first queued.
- Test-phase self-correction: up to 4 fix attempts (`max_fix_attempts=4`).
- Deploy-phase: up to 3 fix attempts (`max_deploy_fixes=3`).
- **Transient error patterns:** network (connection refused/reset, timeout, DNS), API (429, 502, 503, rate limit, overloaded), infrastructure (missing executables, errno 2, command not found, npm errors), resource exhaustion (ENOSPC, ENOMEM, OOM kill/signal 9), DB startup races. `_TRANSIENT_PATTERNS` and `_INFRA_ERROR_KEYWORDS` are matched as case-insensitive substrings of the error text, so a message that merely contains `killed` or `timeout` is classified accordingly; this decides only the retry policy.
  - 429, 502, 503 and `errno 2` are matched as whole tokens (`_TRANSIENT_STATUS_RE`), so a build id such as `3f429a1c` or a version such as `2.429.1` is not a status code. "timed out", "deadline exceeded" and "ran out of time" are transient patterns. A bare number elsewhere in a message ("line 429") still reads as a status code.
- Playwright is pinned in `requirements.worker.txt` to match the Docker image.
- After each fix loop (`fix.py`, `qa_fix.py`, `deploy_fix.py`), `reapply_post_processors(build_dir, spec)` in `cli/build.py` runs `run_pipeline(phase="reapply")`, which re-runs every processor registered for the `reapply` phase (60 of the 117; the other 57 are `build`-only) so fix loops cannot overwrite post-processed files.

## Quality gate retry policy (DESIGN_QG_RETRY1)

`QualityGateError` is split into two subclasses raised by the blocking gates:
- `DesignQualityGateError`: from `BLOCKING_DESIGN_REVIEW=true` when design_fix can't clear critical issues
- `QAQualityGateError`: from `BLOCKING_QA=true` when qa_fix can't clear failures

`DesignQualityGateError` gets **1 retry** because design regeneration is stochastic and an earlier-attempted spec can pass on a fresh generation (empirically validated via the admin AI Fix flow). Before requeue, `_inject_design_issues_as_rebuild_request(conn, build_id)` reads `builds.design_review_results`, extracts up to 10 critical issues with `location: issue → fix` bullets, and writes a `DESIGN REVIEW CRITICAL ISSUES` block to `builds.rebuild_request`; the next job claim picks it up via `job["rebuild_request"]` and `build.py` prepends it as the REVISION REQUEST (HIGHEST PRIORITY) section. Customer-written revision text is preserved above; a stale prior design block on the row is replaced, not stacked.

`QAQualityGateError` stays non-retryable (structural: missing CSRF, auth bypass, won't self-heal via stochastic re-gen). Base `QualityGateError` stays non-retryable as a safe default. Repeated-error early-exit and the 2-hour retry budget still apply to design retries. Injection is fail-safe (returns False, never raises); the requeue proceeds either way. Existing `except QualityGateError` clauses still catch both subclasses unchanged.

## Concurrency

ThreadPoolExecutor with `MAX_CONCURRENT_BUILDS` env var (default 1). Each build runs in its own thread with a dedicated DB connection and a supervisor deadline (see Timeouts). Two limits apply, and they are different things.

- `_claim_next_job` takes a transaction-scoped advisory lock (`_CLAIM_LOCK_KEY`) before counting builds in a pipeline status, so two workers cannot over-claim past `MAX_CONCURRENT_BUILDS`.
- `_claim_and_dispatch` counts this process's busy pool threads and claims nothing unless one is free: a build thread stays busy after its row left the pipeline statuses (it still sends email, runs the janitor and deletes the attempt's Railway project), and a status count that is already back to zero would otherwise let a second build be claimed and wait in the executor's queue as `building`, unleased and unowned, where a recovery pass requeues it and burns a retry for an attempt that never ran.
  - The claiming thread then adds the attempt to `_OWN_ATTEMPTS` (a set of build id and `started_at` pairs) and takes its lease before submitting it; `_run_build_in_thread(job, lease_conn)` releases both when it has finished everything. The lease key is made from the build id and its `started_at`, computed by the database on both sides (`_LEASE_TAKE_SQL`, `_ORPHAN_SELECT_SQL`), so the end of an earlier attempt's tail neither releases nor hides the lease of a later attempt of the same build.
- The claim takes the oldest queued build (`ORDER BY b.created_at`); `builds.tier` does not affect the order.
- The claim returns the row's `error_message` as well, because a stage-resume marker travels in it (see `_resume_stage`).
- Graceful shutdown via `threading.Event` (`_shutdown_event`); the SIGTERM handler calls `.set()` (registered in `run_worker`, not at import), and the main loop uses `.wait(timeout)` for instant response.

**Zombie build protection:** module-level `_run_build_in_thread(job, lease_conn)` catches `BaseException` (not `Exception`) so a `SystemExit` still ends the build through `_fail_build` (dead-letter row, guarded flip to `failed`, customer email) instead of leaving it stuck at `"deploying"` forever. `deploy.get_token()` and `check_railway_cli()` no longer exit the process: they raise `DeployConfigError` when `RAILWAY_API_TOKEN` is absent or the Railway CLI is missing, but a cli module that exits (`test.test_app`) can still raise `SystemExit` in a build thread.

`SystemExit` is logged at WARNING (expected in local testing mode); all other `BaseException` subtypes log at ERROR with traceback. `KeyboardInterrupt` is also caught. Function is module-level so it is directly testable without instantiating the full poll loop.

**Numeric settings.** `MAX_CONCURRENT_BUILDS`, `DAILY_API_SPEND_CAP`, `MONITOR_INTERVAL`, `OPS_SCAN_INTERVAL`, `MAX_DESIGN_FIXES` and `MAX_QA_FIXES` are read with `env_int` / `env_float` (`coordinator/config.py`) and fall back to their defaults on a mistyped value, where a bare `int()` used to stop the worker at import; a spend cap that is not a finite number means 50, never off.

**Manifest warnings.** The v2 lint reads at most 256 KiB of `compose-manifest.txt` and copies every line that starts with `warning: ` (the engine's own, in particular the one that says a model's rows are public) and then every `spec-warning: ` line (spec text) into the build log, the first 50 lines as one entry each and the rest packed 100 to an entry. A line longer than 1,200 characters is cut. When `engine_warnings_count` is present it must equal the number of `warning: ` lines, and a mismatch or an oversize manifest is a lint error. Before, only warnings in the manifest's first 50 lines were copied, so a spec could bury the engine's warnings with its own.

The manifest's `external_url_lint:` header (`skipped`, `ran` or `failed`) is not read by the worker and is not copied into the build log; only the warning lines and the two counts are.

**Watchdog probe.** The probe in `queue/watchdog_probe.py` reads at most 4,096 bytes per `/health`, waits 5 seconds per URL and spends at most 20 seconds in all, through `guarded_session`.

## Daily spend cap

`DAILY_API_SPEND_CAP` env var (default $50). Pauses claiming new builds when the sum of `total_api_cost_usd` over builds with `COALESCE(started_at, updated_at) >= CURRENT_DATE` reaches the cap. The `COALESCE` keeps a retrying build in the sum: every requeue clears `started_at`, and a build waiting for its next attempt would otherwise drop out of the figure together with what it already spent. In-flight builds finish normally (their cost is written when they finish, so they are not counted until then). Only the v1 engine records a cost, so under v2 the figure is 0 and the cap never triggers; a failed build's cost also leaves the sum when the janitor deletes its row. Set to 0 to disable. Checked at job claim time via `_get_daily_api_spend()`, which fails closed: if the query cannot run, new builds pause.

## Infra-aware deploy

`verify_health()` returns `"infra"` when the app never answers within its window or its health body points at the database (connection timeout, ECONNREFUSED, etc). The worker then polls up to six times (`verify_health(url, timeout=90)`, then a 30 s pause between polls, so about nine minutes at most) instead of entering the Claude fix loop.

- If it is still `"infra"` afterwards, the app never answered healthy, and a site that never answered is not a preview: `_run_pipeline` raises `_DEPLOY_NEVER_HEALTHY`, whose text contains "timeout", so the attempt ends as a transient failure (requeued, using a retry, artifact kept, not tagged `[v2-deterministic]`). No fix loop runs for it on either engine, and a final gate refuses any non-ok health, so the pipeline reaches `_set_preview_status` only after a healthy `/health` response was seen. Saves API costs and prevents false failures during Railway maintenance.
- A healthy answer is HTTP 200 with a JSON body whose `status` is `ok` (`health_response_ok` in `cli/deploy.py`); a 200 that merely contains the letters "ok" is not. The app's own `503 {"status": "db_error"}` is `"infra"`, not `"failed"`, and a generated v2 site's `503 {"status": "home_page_error"}` (its home page did not render when it started, `services/health.js`) is `"failed"`.
- For a v2 build, a non-ok result from `deploy()` first gets one recovery probe (`verify_health(timeout=180)`); only an explicit healthy answer promotes it to ok.
- `deploy()` returns Railway's end state for the deployment as `deployment_status` (it raises `DeploymentEnded` after two consecutive CRASHED or FAILED polls), and `_v2_deploy_failure` decides from it:
  - FAILED (Railway never got a container running) is retried once, with stable text so the repeated-error check stops a second one.
  - CRASHED with no readable log line (the read failed, or the log has no line with text) is retried once with stable text (`_DEPLOY_CRASH_CAUSE_UNKNOWN`), and the repeated-error check ends the series: nothing shows whose fault it is.
  - CRASHED with a log showing the database was unreachable is a transient failure.
  - CRASHED with a log that shows neither, or a running deployment whose `/health` never said ok, stays `[v2-deterministic]`.
- After a healthy `/health`, the worker signs in once with its own verification token (`verify_magic_link_auth`): `GET /login`, then `POST /magic/consume` with the token in the request body. That uses the token up and puts it in no URL; the customer's separately seeded token is not touched.
- A v1 app that only implements `GET /magic?token=` is reported as "login link verification failed", a warning, not a build failure.
- `deploy()` itself is called with `password_login_probe=(_engine != "v2")`, because a v2 app seeds its admin without a password and the v1 password-login probe would report a healthy app as failed.

## Misc infrastructure

- **Timing:** `started_at`/`completed_at` tracked on builds table. `started_at` is also the attempt token (see Status contract): a requeue clears it and the next claim stamps a new value, so it is NULL while a build waits in the queue.
- **Deploy integration:** the Railway project, service and environment IDs and the generated `DATABASE_URL` that `deploy()` returns are held in the attempt's own state (see Railway projects) and written to the build row only by the preview write, in the same statement that moves the build to preview; the app is registered with the monitor once that write has landed (`_announce_preview`). With no `RAILWAY_API_TOKEN` the pipeline stops before deploying and `_run_pipeline` moves the build to preview through `_set_preview_status` with no URL (a local build); `_process_job` then sends no emails for it.

## Railway projects: who creates, records and deletes one

`builds.railway_project_id` is written only by the preview write (`_set_preview_status(..., railway=...)`; `_record_environment_id` fills in a missing environment id on a row that already names the project), so it always names a deployment that reached preview, which the customer was told about. After an in-place requeue of a build that was in preview, that is the previous, working site with its database.

- **The attempt's own record.** The project that `deploy()` creates in an attempt is held in the job's `attempt_state` (`_attempt_state`, filled by `_note_attempt_project`): the project, whether it was announced, whether it was discarded, whether the supervisor abandoned the thread. Nothing there is persisted.
  - It also holds the ids of every project a preview write announced for the attempt (`announced_projects`, which includes one the supervisor's probe found while the thread was still inside `deploy()`) and whether the thread has ended (`thread_ended`).
  - Two locks: `lock` decides the project's fate (the thread's look-up and delete, and the supervisor's preview write for a project its probe found; the supervisor waits at most `_ATTEMPT_LOCK_WAIT_S`, 240 s, for it) and `handoff` guards `abandoned` and `thread_ended` only, so exactly one side, the thread or the supervisor, owns the clean-up.
- **The one lifecycle delete.** `_discard_unannounced_project` is the only place where the worker deletes a project outside `deploy()`'s clean-up after its own failed call and the opt-in orphan sweep. It deletes only the project this attempt created, and only if it was not announced, no build row records it, the lookup that checks that can run, and `RAILWAY_API_TOKEN` is set. Not knowing is not permission. It is called when the preview write missed, when the build was killed while deploying, after a requeue or a terminal failure, when the build thread crashes, when the pipeline thread ends after the supervisor gave up on it, and by the supervisor's follow-up. `_fail_build`, orphan recovery and the janitor delete no project.
  - The clean-up of an abandoned thread is `_discard_after_abandonment`, on a connection of its own: the thread's connection was closed when `_process_job` returned. It connects in about 48 seconds at most (three tries of 15 seconds, with 1 and 2 seconds between); without a connection it deletes nothing and logs the project's id. A project in `announced_projects` is not deleted by it.
- **A delete that fails** leaves the project running; it is logged with its id and "NOT deleted", and nothing retries it. `destroy_project` in `cli/deploy.py` returns only when Railway confirmed the delete and raises otherwise.
- **Worker killed mid-attempt.** If the process dies after `deploy()` created a project and before the preview write, no build row names it; it keeps running until an operator or the opt-in orphan sweep removes it.
- **Superseded preview.** When a build in preview is requeued in place and deploys again, the earlier project is no longer referenced by any row. The worker does not delete it (it was announced); it is named once in a warning in the worker log and stays in the monitor registry until an operator removes it.
- **Removed.** `_store_railway_metadata` (it wrote the ids to the row as soon as `deploy()` returned) and `_cleanup_old_railway_projects` (the janitor's project deletion) no longer exist.
- **DB connection reuse:** `_ensure_conn()` health check before deploy, post-deploy fix, and cleanup stages. Reconnects automatically if the connection is stale after long-running pipeline stages.
- **Background threads** (started in `run_worker`): health monitor (`MONITOR_INTERVAL`, default 900s; `run_checks` in `monitor/ping.py` logs and retries a failed registry read after a backoff that starts at 30 s and doubles up to the interval, and a failed check of one app moves the cycle on to the next, so a transient database error no longer ends the loop; should the loop return anyway, the worker's wrapper restarts it after a pause), daily backups (03:00 UTC, `monitor/backup.py`), and either the coordinator or, when it is off, the legacy ops agent (`OPS_SCAN_INTERVAL`, dry run unless `OPS_DRY_RUN=false`).
  - The coordinator (`COORDINATOR_ENABLED`) ticks every `COORDINATOR_INTERVAL_S` seconds, default 1800, and runs dry unless `COORDINATOR_DRY_RUN=false`. `coordinator.config.tick_interval_s()` reads the interval, and a value that is not a whole number of at least 1 means 1800; a bare `int()` used to stop the worker at boot on `1800.5` and make the loop spin on `0`.
- **`USE_MAGIC_LINK_AUTH`** is parsed by `safety_switch` and the worker no longer rewrites it in its environment: `cli/build.py`, `cli/test.py` and `cli/deploy.py` call `magic_link_auth_enabled()`, the same parser, so a spelling such as `1` gives one answer to the generator, the seed and the probe.
- **Graceful shutdown:** SIGTERM stops claiming new builds, waits for in-progress builds to finish (`_executor.shutdown(wait=True)`), then exits. Railway's `drainingSeconds` (60) bounds how long it waits before SIGKILL.

## Failure KB / Review KB persistence

Both `cli/failure_kb.json` (fix-pattern learnings) and `cli/review_kb.json` (design-review rules) are committed seed files; the **live** prod state lives on the Railway worker volume mounted at `/builds/`. `cli.failure_kb._get_kb_path()` and `_get_review_kb_path()` both resolve to `/builds/failure_kb.json` and `/builds/review_kb.json` when `RAILWAY_ENVIRONMENT` is set, and seed from the committed file on first access if the volume file doesn't yet exist.

**Why this matters:** before the 2026-04-19 fix, these helpers used `Path(__file__).parent.parent / "builds"` which resolved to `/app/builds/`; ephemeral container storage, not the mounted volume. Every deploy wiped the running worker's accumulated KB state and replaced it with whatever was in git. Now the volume preserves learnings across deploys; the committed file is only used as a bootstrap seed for fresh projects.

**Module-level constants** you can monkeypatch in tests: `KB_PATH`, `REVIEW_KB_PATH`, `_KB_SEED_PATH`, `_REVIEW_KB_SEED_PATH`, `_RAILWAY_VOLUME_DIR`.

**Env overrides** (optional, useful for tests/ops): `FAILURE_KB_PATH`, `REVIEW_KB_PATH` point the resolver at an explicit path regardless of Railway status.

**Regression coverage:** `tests/test_failure_kb.py` - `TestGetKbPath`, `TestGetReviewKbPath`, `TestLoadSaveUsesResolvedPath`.

## Orphan recovery

`_recover_orphaned_builds` requeues, or permanently fails, builds stuck in `building`/`testing`/`deploying` (from a previous crash or Railway redeploy). It runs at worker startup and again every `_ORPHAN_RECOVERY_INTERVAL_S` (120s) from the poll loop, so a build whose owner was still draining at startup is picked up once the owner is gone.

**What makes a build an orphan.** One statement, `_ORPHAN_SELECT_SQL`, reads the candidates, oldest first. A build qualifies when all of these hold:

- (1) it is in a worker-owned status;
- (2) it started more than `_ORPHAN_MIN_AGE_S` (30s) ago, or has no `started_at`;
- (3) no connection holds its lease: every claimed attempt takes a session-level Postgres advisory lock (`_acquire_build_lease`, class id `_LEASE_CLASS_ID`) on a dedicated connection, which the database drops when that connection or process dies, and the statement checks `pg_locks`;
- (4) its current attempt is not run by this process (`_OWN_ATTEMPTS`).

The claiming thread takes both the lease and the ownership at the claim (`_claim_and_dispatch`), before the build is handed to the pool, so there is no window in which a claimed build is neither leased nor owned; the build thread releases them when it has finished everything, including what it does after the row left the pipeline statuses.

Every recovery `UPDATE` re-checks the status and that `started_at` is still the value the `SELECT` read, so a build that finished, or was requeued and claimed again, in between is not pulled backwards.

**The lease is best effort.** If `_acquire_build_lease` cannot take the lock (database unreachable, or another connection holds it) it returns None and the build runs anyway; nothing re-checks the lease later, so if the lease connection drops mid-build the lock is gone while the build continues. In both cases only `_OWN_ATTEMPTS` still protects the attempt, and that covers this process only: a second worker process would treat the build as an orphan once it is older than 30 seconds.

**What happens to each orphan,** in this order:

1. The supervisor already spent the build's one free retry (`supervisor_retry_used`, read by `_has_prior_timeout_failure`): permanent failure.
2. The build's files are on disk (`_can_resume_build`: `package.json` and `server.js` present, interrupted in `testing` or `deploying`; an exception is treated as "cannot resume"): requeue with the marker `Recovered: resuming from <stage> stage` in `error_message`. `retry_count` is not incremented (no work lost), but resumes are bounded: each writes a `recovery-resume` row to `build_logs`, and once a build has `_MAX_ARTIFACT_RESUMES` (5) of them it fails permanently. The cap exists because a build that kills the worker on every resume would otherwise loop forever without touching `retry_count`.
3. Otherwise requeue with no marker and `retry_count + 1`: the same counter and the same cap (`_MAX_RETRIES`, 6) as the retry after a pipeline error. With `retry_count >= _MAX_RETRIES`: permanent failure.

Every permanent failure goes through `_fail_build` (category `infra`): a dead-letter row, the guarded flip to `failed`, and the customer's "build failed" email.

## Stage resume

The claim returns the row's `error_message`, and `_resume_stage` decides where the pipeline starts. It returns `testing`, `deploying` or None (a full run: validate, build, test, deploy), and returns a stage only when the build's files are on disk or were just restored from `builds.build_artifact`.

Two things ask for a resume: a marker in `error_message`, or no marker but a stored artifact, which is what a build requeued after a transient error or a manual retry has. A stored artifact that fails to restore does not count as resumable, and the build starts over.

The marker is a contract between two code bases that share the column, defined once on the worker side (`_RESUMABLE_STAGES`, `_resume_marker`, `_parse_resume_marker`): the whole column value must be exactly `Recovered: resuming from <testing|deploying> stage` (orphan recovery) or `Requeued: resuming from <testing|deploying> stage` (the web app's `POST /ops/api/v1/builds/:id/requeue-from-stage`), matched with `fullmatch`, so a retry note that merely quotes the words is not a marker. `TestResumeMarkerContract` reads the template and the stage list from `web/src/routes/ops-api.ts`.

A resume skips validate and build; a resume from `deploying` also skips the test stage of the v1 engine. For v2 the build-output lint still runs on every resume, because it is the v2 test stage.

## Orphan Railway sweep

On startup, `_sweep_orphan_railway_projects()` lists up to 50 Railway projects via GraphQL, cross-references with active builds, and deletes orphans.

- **Gated by `ENABLE_ORPHAN_SWEEP=true`; it does nothing unless that variable is set, and it was switched off on all services after the 2026-04-09 project-deletion incident (see `docs/RAILWAY_DISASTER_RECOVERY.md`).** Only the worker whose database is the source of truth for the Railway account may enable it: any other database would classify every real project as an orphan.
- Keeps the CAS platform project (`RAILWAY_PROJECT_ID` env var), every project that any build row records (whatever the row's status) and any project younger than four hours (`_ORPHAN_SWEEP_MIN_PROJECT_AGE_S`; an attempt in flight in another worker process has not recorded its project yet).
- What it can delete: a project an attempt created and could not delete when it ended, a preview that a later deployment of the same build superseded, and any project in the account that this database never knew. Logs project name + ID before every deletion.

## Build dir isolation

Each build gets its own directory: `builds/{app_name}-{build_id[:8]}/`. Prevents path collision when concurrent builds run for the same app. `build_app()` receives the dir via `output_dir` parameter. Disk cleanup queries active `build_ids` to construct expected dir names.

## Disk hygiene

- **Startup disk cleanup:** `_startup_disk_cleanup()` runs on worker startup (after orphan sweep, before background threads). Cleans `/tmp/npm_test_*` dirs and `/tmp/spec_*.json` files leaked by crashed builds, then calls `_free_disk_space()` to remove stale build dirs and snapshots from the Railway volume. Non-blocking.
- **Fresh build dir on retry:** when a code error triggers retry, the worker removes the entire build directory (not just `node_modules`) via `shutil.rmtree()` before rebuilding fresh, preventing ENOENT from stale leftover files.
- **Stage-based resumption:** see Stage resume above. The files come from the `builds/` directory when it persists (a Railway volume), otherwise from the tarball in `builds.build_artifact` (`_store_build_artifact`, written after a fresh build and again after the tests pass; each file is stored once, `node_modules` at any depth and `.git` are left out and the artifact is skipped above 50 MB). Orphan recovery's own on-disk check (`_can_resume_build`) looks at the directory only, so without a volume an orphan restarts with a retry, and the stored artifact is then used at the next claim.

## Design review stage (2.75)

After code review (Stage 2.5), runs `design_review.py` (non-blocking unless `BLOCKING_DESIGN_REVIEW=true`). When critical issues found, runs `design_fix.py` up to `MAX_DESIGN_FIXES` times (default 3) then re-reviews. v1 engine only: the v2 path skips code review, design review and the QA/UI stages (`_engine == "v1"` guards in `_run_pipeline`) and runs a deterministic output lint instead. Early-exits if no progress after 2+ attempts. Results stored in `builds.design_review_status`/`builds.design_review_results` after each iteration (not just the final one) for observability. Artifact storage (`_store_build_artifact`) moved to AFTER design review/fix so fixes are captured. Skipped on resume.

**KB recording:** after each `design_fix.py` iteration, `failure_kb.record_fix()` is called with `fix_category="design"` and `succeeded = (new_critical_count < prev_critical_count)` so the KB learns which design fix patterns work.

## QA stage (3.5)

After smoke tests pass, runs `qa.py`. Failures log to `build_logs` and store in `builds.qa_status`/`builds.qa_results`. On failures it runs `qa_fix.py` up to `MAX_QA_FIXES` times (default 4, 20-minute loop budget), re-verifying smoke tests after each fix. QA errors and skips do not block the build, with three exceptions: `BLOCKING_QA=true` raises `QAQualityGateError` when failures remain, and (unless `BLOCK_ON_QA_FIX_BUDGET` is set to a rollback token) a `qa_fix` loop-budget or API wall-clock timeout fails the build.

**Resume skip (RESUME_SUBSTAGE1):** on orphan resume (`resume_from` is set), reads `builds.qa_status` from DB before entering the QA block. If `qa_status != 'pending'` (i.e. passed/warned/skipped), skips QA entirely to avoid re-running it on a resumed build.

## UI test stage (3.75)

After QA stage, runs `ui_test.py` visual quality checks (non-blocking, v1 path). Score stored in `builds.ui_test_score`/`builds.ui_test_results`. Errors/skips never block the build.

## Auto-cleanup

After a build ends (a successful deploy and a permanent failure both call it), `_cleanup_old_builds()` (the janitor) deletes older duplicate builds of the same app, keeping the 2 most recent rows. It deletes rows and nothing else: no Railway project. The scope is deliberately narrow: only builds of the SAME customer (`customer_id`, since `app_name` is not unique across customers) and only those in `failed` or `abandoned` status; a live, preview, queued or in-flight build is never deleted however old it is.

- Other rules read build rows, so the janitor deletes a candidate only if, at the moment of the delete, all of this still holds:
  - it is still failed or abandoned (the status is part of the `DELETE`, and the candidates are locked first with `FOR UPDATE SKIP LOCKED`, so a build retried between the ranking and the delete is skipped);
  - no surviving build descends from it (a build is deleted only together with all of its descendants, and `parent_build_id` is never rewritten on a row that stays);
  - no trial or keeper is stamped with it (`handoff_build_id` is `ON DELETE SET NULL`, and clearing it would reopen the one free build per trial);
  - it records no Railway project and no `live_url` (such a row stays until an operator deletes it together with its project).
- If a condition changed under the lock, the whole cleanup is rolled back and the next run decides again.
- Uses PostgreSQL advisory locks (`pg_try_advisory_lock(hashtext(app_name))`) to prevent concurrent cleanup races.
- Deletes dependent rows in `intake_messages`, `feature_messages` and `feature_requests` explicitly (`build_logs` and `dead_letter_builds` go by `ON DELETE CASCADE`).
- Also removes the stale per-build dirs from disk (`builds/<app>-<id8>`, names validated by `_safe_build_path`).
- Not tied to a deleted build on purpose: the `build_start_events` ledger (the web app's start budget must not be refunded by a delete).
- The daily spend figure loses the deleted build's cost.
- Whether a payment rule may read the rows the janitor deletes is settled on the web side: `builds.is_harbor` is a column on the row, not derived from `parent_build_id` (see `docs/THREAT_MODEL.md`, "Review gate and publication").

## Sentry

Error tracking via `@sentry/node` (web) and `sentry-sdk` (worker). Initialized in `web/src/instrument.ts` (must be first import in `server.ts`) and at top of `worker_pg.py`. Express error handler via `Sentry.setupExpressErrorHandler(app)`. Worker captures exceptions with build context tags. Guarded by `SENTRY_DSN` env var; no-op when not set. The worker's options come from `_sentry_init_options`: `include_local_variables=False` (stack-frame locals are never sent, because they held project tokens and database URLs), `send_default_pii=False`, and a `before_send` hook, `_sentry_scrub_event`, that redacts secrets and masks email addresses in exception values, log messages and breadcrumbs (covered by `tests/test_worker_sentry_scrub.py`).

---

## `queue/mailer.py`: Transactional email module

Sends build lifecycle emails via Resend REST API. The module defines `send_preview_ready()`, `send_build_failed()`, `send_build_progress()` (only when `SEND_BUILD_PROGRESS_EMAILS` is switched on) and `send_review_pending()`, and the worker calls all four. The worker does not report progress to the web app over HTTP: it writes the `builds` table directly, and the web app's optional `POST /webhooks/build-status` hook is not used by it. Every public `send_*` is wrapped in `_never_raises`, so an email problem cannot change a build's outcome.

- `send_review_pending()`: notifies admin when a build reaches preview and needs SE review; includes deep link to `/admin/reviews/:buildId`. `_process_job` sends it after `_set_preview_status` moved the build, so once per arrival at preview (again after every rebuild), never for a duplicate call that matched no row, and not at all when `CAS_HARBOR_AUTO_REVIEW=true`.
- The build-queued, review-ready and review-changes-requested emails are sent by the web tier (`sendBuildQueuedEmail`, `sendReviewReadyEmail`, `sendReviewChangesRequestedEmail` in `web/src/services/email.ts`), not by this module.

`_send` checks unsubscribe status (skipped for internal mail to the admin) and logs every attempt to the `email_log` table; with no `RESEND_API_KEY` the attempt is logged as `skipped`. Before it stores the HTML, `_log_email` redacts token values with `_redact_secret_params`, a port of the web tier's `redactSecretParams` (`test_the_redaction_is_the_web_apps` in `tests/test_mailer.py` compares the two on one corpus), so the stored body of the preview-ready email holds `/magic?token=[redacted]`. The login link itself is not written to `build_logs`: `_announce_preview` writes `LOGIN_LINK_EMAILED_LOG_LINE`, which says the link is in the email.

---

## `queue/watchdog_probe.py`: Zombie deploy detection module

`probe_for_zombie_deploy(app_name, started_at, completed_at, hard_timeout_s)`: fail-safe (never raises). Lists the account's Railway projects, keeps those whose NAME equals `app_name` and whose creation time falls inside the build's window, and GETs `/health` on each. Returns `{live_url, project_id, service_id}` when a zombie deploy is confirmed live, else None. 20s total deadline (`PROBE_DEADLINE_S`), 5s per URL.

- **Caveat:** the match is by project name and `app_name` is not unique across customers.
  - The caller passes `exclude_project_ids`: every Railway project id recorded on any build row with the same `app_name` (`_recorded_railway_projects`, which includes the build's own earlier, announced deployment), plus the fresh projects that other attempts in the same worker process created and have not recorded yet (`_UNRECORDED_ATTEMPT_PROJECT_IDS`); those are never returned, and if the lookup fails the supervisor skips the probe.
  - What remains confusable is a same-named build in another worker process whose project is not on a row yet.
  - The consequence is bounded because a match only produces a preview that a reviewer must approve.

`_supervisor_reality_probe()` in `worker_pg.py` calls this from `_supervisor_force_fail` after the build row has been read and before any outcome is written, with no row lock held. On a healthy answer (`health_response_ok`: HTTP 200 and a JSON body whose `status` is `ok`; an HTML placeholder page or a 204 does not count) the build goes through `_set_preview_status` (preview, the initial `review_status`, the reviewer columns cleared, and the probed Railway ids recorded in that same statement; only a note is written afterwards, in a separate statement) instead of `failed`.

`_process_job` then announces it with `_announce_preview` like any other preview (monitor registration, the admin's review-pending email unless auto-review is on, the customer's preview email). The worker never writes `live`. The probe is also used by `coordinator/agents/reconciliation.py`, which only files an approval request (see `docs/internals/coordinator.md`). Covered by `tests/test_watchdog_probe.py`.
