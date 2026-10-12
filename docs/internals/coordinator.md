# Master Coordinator: coordinator/

> Historical document from April 2026; the system was wound down in July 2026.

> **Scope:** autonomous operations framework. Most role agents are rule-based Python; only a few call a model (see the model-usage note under Agents). Extracted from CLAUDE.md to keep the root context file focused on load-bearing decisions. Consult this file for agent architecture, scoring, budget, work detection, and per-agent responsibilities.
>
> **Spec:** `docs/MASTER_COORDINATOR_SPEC.md`

## Module overview

- `__init__.py`: package marker.
- `spend_tracker.py`: today's spend from two sources.
  - The worker's own ledger (`get_daily_spend()`, the one definition of it: `builds.total_api_cost_usd` over builds whose `updated_at` is today, a column a trigger stamps on every update, so a requeued build's cost stays in the day although every requeue clears `started_at`; plus `agent_tasks.actual_cost_usd` over tasks finished today; the budget and the world state both read it) omits everything the web tier spends on intake, support and companion chat, so it under-reports the account.
  - The Anthropic Admin API cost report (`get_anthropic_spend()`, read only when `ANTHROPIC_ADMIN_KEY` is set) covers the whole account; it is paged (`_fetch_cost_buckets`, 31 daily buckets a page, refusing a partial total), `amount` is in cents and `_bucket_usd` divides by 100 exactly once, and the result is cached for 10 minutes.
  - `get_comprehensive_spend()` returns the report's figure when it can be read, else the ledger's, with `provider_failed=True` when the key is set but the report could not be read; `provider_spend_configured()` says whether the key is set.
- `sample_build_specs.py`: pool of 8 realistic app specs for automated pipeline testing (it is data, not a test module; it was named `test_build_specs.py` earlier). `get_next_spec()` picks an available spec not recently built. `_generate_actions()` auto-derives CRUD ops from models. Its only consumer is `backend_engineer._handle_test_build`, reached when `ENABLE_TEST_BUILDS=true` (off by default) makes work detection file test builds.
- `config.py`: centralized thresholds. Reads env vars with bounds-checked `_int()`/`_float()` helpers. Replaces scattered hardcoded constants across `tick`/`work_detection`. Its helpers `env_int` and `env_float` are also used by the worker, the monitor and the ops agent, so a mistyped number falls back to its default instead of stopping the process at import. Exports:
  - `FAILURE_RATE_WINDOW` (10), `FAILURE_RATE_THRESHOLD` (0.8), `STALLED_MINUTES` (30), `WORK_EXPIRY_HOURS` (2), `STUCK_TASK_HOURS` (4).
  - `APPROVAL_REMINDER_HOURS` (3, `COORDINATOR_APPROVAL_REMINDER_HOURS`), `APPROVAL_TIMEOUT_HOURS` (4, `COORDINATOR_APPROVAL_TIMEOUT_HOURS`, never below the reminder delay).
  - `FAILURE_RETRY_HOURS` (6, `COORDINATOR_FAILURE_RETRY_HOURS`, at least 0.25), `INITIATIVE_INTERVAL_DAYS` (7, `COORDINATOR_INITIATIVE_INTERVAL_DAYS`).
  - `MODEL_CALL_TIMEOUT_S` (60) and `MODEL_CALL_MAX_RETRIES` (1), plus `daily_budget_usd()` and `tick_interval_s()` (`COORDINATOR_INTERVAL_S`; a value that is not a whole number of at least 1 means 1800).
- `state.py` - `WorldState` dataclass + snapshot builder.
  - Tiered refresh: everything in the snapshot comes from the platform database. The build, customer, monitor and spend figures are read every tick; agent performance is refreshed at most once an hour (`_PERF_CACHE_TTL`, 3600s). Nothing in `state.py` calls GitHub or Stripe.
  - The cache holds at most 100 entries (the oldest is evicted) and entries expire by TTL only.
  - The per-agent success rates are counted from `agent_tasks` rows (`_refresh_agent_performance`); the `agent_performance` table is not used. The financial figure is `spend_tracker.get_daily_spend`, the same ledger the budget reads.
  - `failed_builds` query LEFT JOINs `dead_letter_builds` to surface `error_category` for intelligent routing.
  - Each per-tick refresh is isolated (a failed query is rolled back and does not skip the others); until the financial refresh succeeds, `WorldState.spend_known` is False and the remaining daily budget is 0, so the budget fails closed.
- `scorer.py` - `score_task()`, `CATEGORY_WEIGHTS` (P0-P3), `_urgency_multiplier()` with tiered escalation (0-4h cap 1.5, 4-8h cap 1.8, 8h+ cap 2.0, prevents starvation while avoiding spikes), `_cost_penalty()`, `_performance_multiplier_cached()` (0.7 floor prevents death spiral). `build_performance_cache(conn)` runs one query per tick so scoring can factor in recent agent success rates without N+1 overhead. `AGENT_MODELS` dict with default/upgrade/condition per agent type. `select_model()` respects budget constraints. Pure functions, no API calls.
- `budget.py` - `TickBudget` dataclass, `ModelCallMeter`, `compute_tick_budget()`.
  - The budget is `COORDINATOR_DAILY_BUDGET` minus today's spend from ONE configured source, spread over the ticks left in the day (`_remaining_today`).
  - With `ANTHROPIC_ADMIN_KEY` set the source is the cost report, and if it cannot be read the spend is unknown and the tick gets a budget of zero; the worker's ledger is never substituted for it, because it would report less than was spent.
  - Without the key the source is the ledger (`spend_tracker.get_daily_spend`), and if that query fails the spend is unknown and the budget is zero too: the figure `build_world_state` read earlier in the tick is not used in its place, because a ledger that cannot be read now is not one to spend against (`WorldState.spend_known` is False when even the snapshot could not read it).
  - No dollars means no calls: `max_api_calls` is the number of whole calls the dollars pay for at the per-call estimates (counted in millionths of a dollar, so $0.20 buys four $0.05 calls and not three), and it is 0 when the figure is below the cheapest call's estimate.
  - `ModelCallMeter` is one tick's allowance for model calls, counted where each call is granted: `run_tick` builds one from the tick's `TickBudget` and hands it to every `Actuators` instance that may authorize a call in that tick (failure triage in work detection and each dispatched agent), and `Actuators.authorize_model_call` grants a call only while the tick has a call left, the model is one the tick allows and the dollars left cover the call's estimated cost (`scorer.ESTIMATED_COST`; no caller knows its token count before it calls).
    - An `Actuators` built without a meter grants none. The number granted is stored in the tick record (`model_calls_granted`).
  - Yields to active customer builds (Haiku-only, one call, at most $0.10, when `active_builds > 0`).
  - Per-tick cap of 25% daily budget prevents early exhaustion; tick count derived from `COORDINATOR_INTERVAL_S` instead of hardcoded "30 min" assumption. `RPM_SPACING` dict for rate limit pacing.
- `tick.py` - `run_tick()` implementing full tick lifecycle: guard check, snapshot, budget (computed before detection, because failure triage draws from the same allowance; the dispatch allowance is the budget less what triage was granted), detect work, budget gate, parallel dispatch bounded by one round deadline (the longest per-agent timeout in the batch, from `AGENT_TIMEOUTS` 900-7200s and `CATEGORY_TIMEOUTS` 300-1800s; a worker still running at the deadline is abandoned, not joined, so one hung agent cannot hang the tick; see "Tick watermark and dispatch"), process reviews, record the outcome and log. Rows written under dry run are marked and left out of the failure circuit (see "Failure circuit and initiative turns").
  - Each dispatched task is charged against the per-tick allowance by `_task_charge`: no agent in this repository reports token usage (`AgentResult.cost_usd` and `api_calls` stay 0), so a task keeps the reservation made for it, its estimated cost and one call, which is also what `_dispatch_agent` writes to `agent_tasks.actual_cost_usd`.
  - The allowance is therefore a cap on how many tasks run per tick at their estimated cost, not a measurement of spend; model calls are counted separately, one at a time, by the tick's `ModelCallMeter`, which counts calls at fixed per-model estimates and not tokens, so it bounds how many calls a tick makes and not the bill.
  - Each dispatched task's result (learnings, follow-up work, review flag) is committed before the next task runs, so a later task's failure cannot roll it back.
  - `detect_work` is called with the tick's `dry_run` flag and the meter.
  - When the budget is under one cent and no task scores 0.85 or higher (P0), the tick records a `skip_all` decision with reason `budget_exhausted` and dispatches nothing; a P0 task that does enter the loop is still refused by the per-task cost gate unless the remaining allowance covers its estimate.
  - Decision logging to `coordinator_ticks` table. Agent registry.
  - Per-agent DB connection isolation (`conn.rollback()` on failure prevents `InFailedSqlTransaction` poisoning; `_safe_record_on_error` checks `conn.closed` before acting). All cursor usage wrapped in `with conn.cursor() as cur:` context managers for deterministic release under errors.
  - `_expire_stale_approval_requests` sends one reminder email at 3h and REJECTS an unanswered `approval_requests` row at 4h (`resolved_by = 'timeout_auto_deny'`); it never auto-approves, because silence is not consent. Approval requests are advisory in this repository: nothing executes an action when one is marked approved.
  - `_persist_work_item` uses `INSERT...ON CONFLICT DO NOTHING RETURNING id` for atomic dedup (no read-then-write races). `_expire_stale_work` uses `config.WORK_EXPIRY_HOURS` / `config.STUCK_TASK_HOURS` with parameterized intervals.
- `work_detection.py`, event-driven triggers (`NEW_FAILED_BUILD`, `APP_DOWN`, `NEW_FEEDBACK`, `STALE_BACKUP`, `BUDGET_ALERT`, `NEW_LIVE_APP`) + periodic triggers (daily security scan, daily cost report via cost_analyst, weekly cost projection on Fridays 10:00 UTC, the reconciliation sweep) + proactive triggers (`RECURRING_QA_WARNING`, a stalled-coordinator check, spec, post-build and architecture reviews, test builds when `ENABLE_TEST_BUILDS=true`, agent initiative).
  - It defines 17 `_check_*` functions and `detect_work` calls every one of them.
  - The triggers that used to be defined but not called (preview-ready, pipeline-idle, platform-improvement, roadmap, weekly docs sync) and the four that were called but produced work no agent accepted (`KB_PATTERN_DEGRADING`, `LEARNING_VALIDATION`, `SELF_IMPROVEMENT`, `PERSONA_UPDATE_PROPOSAL`: the target agent answered "Unknown category" and recorded a failed task) were deleted with their tests; `tests/test_coordinator_routing.py` now fails if a trigger is defined and not called, if any (category, agent) pair a trigger can emit is not accepted by that agent's real `execute()`, or, the other way round, if an agent has a dispatch branch or a `_handle_*` method that no emitted work item reaches.
  - The triggers only read, with one exception: failure triage in `_check_failed_builds`.
    - For a failed build the regex classifier cannot categorize, it asks a cheap-tier model (`build_failure_categorizer.enrich_uncategorized`) and records the answer.
    - It acts only through an `Actuators("failure_triage", ...)` built in `detect_work` (so the permission table, analysis-only mode and dry run apply; under the default dry run it does nothing), every call is granted individually by the tick's `ModelCallMeter` (a tick with no budget grants none), it is skipped when the world-state snapshot says today's budget is spent or its spend unknown, it makes at most `MAX_TRIAGE_CALLS_PER_TICK` (5) calls per tick, and it needs `ANTHROPIC_API_KEY`.
    - With the ledger as the spend source, triage calls are counted in the tick they happen in and recorded on that tick's row but are not written to the ledger, so later ticks do not see them.
  - All thresholds read from `coordinator.config` instead of hardcoded constants.
  - Deduplication via `context_hash` (SHA-256 truncated to 32 hex chars, 2x collision resistance vs the prior 16) enforced atomically at persist time by the unique partial index `idx_agent_tasks_dedup`, not by a pre-check.
  - Build-failure routing: `_route_build_failure()` routes `error_category IN ('infra','transient')` to `infra_engineer` and everything else (code_error/unknown/app_logic/…) to `backend_engineer`, using DLQ data supplied by `state.py`.
  - All cursor usage migrated to `with conn.cursor() as cur:` context managers. `_ROADMAP_PATH` module constant for clean test patching.
  - AGENT_INITIATIVE: when no reactive work pending, agents take turns doing autonomous creative work.
  - `ROADMAP_NEXT` dispatch was disabled on 2026-04-04 and its trigger has since been deleted: the local executor has exclusive ownership of `coordinator/roadmap.json`; coordinator + executor both reading the same queue caused dupe-dispatches and merge-conflict races.
  - Every dispatch branch in an agent is one a trigger routes to that agent, with no exception list: the branches no trigger reached (a `roadmap_item` branch in nine agents, and handlers for `auto_approve_preview`, `pipeline_idle`, `platform_improvement`, `rebuild_app`, `deploy_failure`, `cost_optimization`, `docs_sync`, `regression_check`, the financial `cost_report` and the QA agent's `post_deploy_audit`) were deleted with their tests, because only `tick._persist_work_item` writes task rows and no trigger emitted those categories.
- `untrusted.py`: `wrap_untrusted(tag, text, max_chars)` truncates attacker-influenced text (customer messages, build error text, deployment logs, failure-KB error signatures) and wraps it as `<tag>...</tag>` for a prompt. Every `<` in the text (and the two characters that Unicode NFKC folds into it, the fullwidth and small less-than signs) becomes `&lt;`, so no tag of any name can start inside the block and the only closing tag is the one the function appends; stripping a tag with a regex does not give that property, because deleting an inner closing tag can join the text on either side into a new one.
  - `UNTRUSTED_NOTICE` and `NO_LINKS_NOTICE` are the system-prompt sentences that go with it.
  - This is a mitigation, not a guarantee: the hard guarantees are the allowlisted model output, the validated answers (`support_knowledge.kb_rejection_reason` rejects a draft with a link, markup or an address other than the support address) and the actuator gates.
- `build_failure_categorizer.py`: classifies a failed build's error into one of five categories with a cheap-tier model. It reads the log excerpt and calls the model, and persists nothing itself: the call is made only after `Actuators.authorize_model_call` returned True, and the result is written through `set_build_error_category` (`builds.error_category`) and `record_failure_category` (the failure KB), each gated.
- `failure_kb_sync.py`: the weekly pass that drafts support answers from the top failure-KB entries; each draft is gated by `authorize_model_call` and each insert by `support_kb_insert`; under dry run, or when no model call is granted, nothing is drafted (the summary counts it in `skipped_not_authorized`). `support_knowledge.py` holds the knowledge corpus those drafts are grounded in and the draft validator.
- `roadmap.json`: structured roadmap of pending platform improvements with priority, agent assignment, and dependency tracking. The coordinator no longer dispatches from it (see "Roadmap dispatch disabled 2026-04-04" below); the local executor reads it.
- `actuators.py` - `Actuators`, the one place a coordinator agent (or the failure triage step in work detection) causes a side effect: ops playbooks (the only path to a redeploy or a Railway variable), build rows, knowledge-base writes, emails, approval requests. A model call is made by the caller, but only after `authorize_model_call` has allowed it.
  - Every gated method applies three checks in order (`_check_permission`):
    - (1) the caller's entry in `AGENT_PERMISSIONS` must hold the method's permission, else `PermissionError`;
    - (2) with `COORDINATOR_ANALYSIS_ONLY=true` the permission must also be in `_ANALYSIS_ONLY_PERMISSIONS`;
    - (3) under dry run (the default) the method records what it would have done and writes, sends and spends nothing.
  - `run_playbook` is the one method that answers a missing permission by running dry instead of raising.
  - One method is exempt from dry run because its only effect is an entry in a queue a person reviews: `finding_add` (the `coordinator_findings` table).
  - Not behind the layer: reads (`query_readonly`, the world-state snapshot, the Railway zombie probe, the cost report, health probes of deployed apps) and the tick loop's own bookkeeping in `tick.py` (task rows, the tick record, expiring approval requests and the reminder email about one).
  - `tests/test_coordinator_actuator_gates.py` holds the permission matrix, the dry-run matrix and an import scan: outside `actuators.py` a coordinator module may import only a pinned allowlist of standard-library modules and other coordinator modules (a library borrowed through another coordinator module counts), so a network, subprocess, database or model library, or first-party code outside `coordinator/`, fails the test unless it is listed there with a reason (stale entries fail too); `os`, dynamic code and file opens are checked by use.
    - The scan is tested against a list of evasions (`test_the_import_scan_sees_each_evasion`).
    - An older call-pattern scan (a name matcher for `.commit`, `psycopg2.connect`, `requests.<verb>`, `urlopen`, `Anthropic`, `subprocess.*`) stays as a tripwire.
    - What the scan does not show is that a coordinator module makes no database write through a connection it was handed (the tick loop, work detection, the world state and the spend tracker hold one).

  The permission table (`AGENT_PERMISSIONS`) has eleven principals (the ten agents and the failure triage step) and nine permissions, 29 (principal, permission) pairs in all. By permission:

  - `railway_deploy` and `railway_env` (read only by `run_playbook`, below, which needs the one that fits the playbook's category; they gate nothing else, and only `infra_engineer` holds them),
  - `approval_request` (`create_approval_request`; held by infra, security, financial, customer service and reconciliation, the principals whose real handlers escalate or file),
  - `email_send` (`send_alert`; held by infra, security, backend, financial, customer service, documentation, cost analyst and reconciliation),
  - `build_queue` (`queue_rebuild`, `queue_new_build`; held by `backend_engineer` only),
  - `build_annotate` (`set_build_error_category`, which writes `builds.error_category` on a row that is still `failed`; held by failure triage only),
  - `model_call` (`authorize_model_call`, which returns False under dry run; infra, customer service and failure triage),
  - `kb_record` (`support_kb_insert`, `record_fix`, `record_failure_category`, all skipped under dry run, analysis-only mode or not, because the v1 pipeline and the support chat read what they write; infra, customer service and failure triage),
  - `finding_write` (`finding_add`; held by the six agents that file findings: infra, security, backend, customer service, documentation and architect).

  `qa_test` holds no permission and is read-only. `tests/test_coordinator_permission_use.py` runs the principal's real handler for each of the 29 pairs and fails on a grant nothing asks for or a call an agent makes without the grant. An agent cannot build its own `Actuators`: the constructor raises `PermissionError` for a caller in the `coordinator` package other than `coordinator.tick` and `coordinator.work_detection` (an AST scan in `tests/test_coordinator_actuator_gates.py` covers `coordinator/`; `queue/`, `ops/` and `monitor/` could still construct one). That is a check on first-party code, not a sandbox.

  `run_playbook(category, fixer_fn, context)` needs the playbook's permission (`PLAYBOOK_PERMISSION`: `env_config` needs `railway_env`, `railway_crash` needs `railway_deploy`, and a category with no entry raises `ValueError`) and the ops circuit breaker (fail closed: if its audit table cannot be read the run stays dry), and a live run is written to the ops audit log. `create_approval_request` needs `approval_request` (it is refused in analysis-only mode and, under dry run, recorded and not filed); `query_readonly` needs no permission.

  `failure_triage` is an entry in the table that is not a registered agent (`NON_AGENT_PRINCIPALS`); it holds `model_call`, `build_annotate` and `kb_record` and nothing else. The analysis-only set is `kb_record`, `model_call`, `finding_write`, `email_send`.

  Other limits:

  - A redeploy and a Railway variable write are reachable only through `run_playbook`; nothing in `ops/` or `coordinator/` commits to a repository. Their limits are enforced in `ops/fixer.py`, in the functions that make the call, so they hold for the legacy ops agent too: `trigger_redeploy` and `upsert_env_var` accept only the platform's own web and worker services in its own environment (`RAILWAY_WEB_SERVICE_ID`, `RAILWAY_WORKER_SERVICE_ID`, `RAILWAY_ENVIRONMENT_ID`, and `RAILWAY_PROJECT_ID` for a variable); `upsert_env_var` sets only `DATABASE_URL`, `RESEND_API_KEY` and `ADMIN_EMAIL` (`SETTABLE_ENV_VARS`).
  - `queue_rebuild()` refuses, and records why, when:
    - the parent has no customer;
    - the parent failed with an error the same input reproduces (`ops.scanner.is_deterministic_failure`);
    - the customer is at the web app's build-start limits (2 builds queued or in progress, 10 starts in 24 hours; `web/src/services/build-start-limits.ts`, repeated in Python and read from the same tables under the same advisory lock);
    - a child of that parent is in flight, or `COORDINATOR_MAX_REBUILDS_PER_BUILD` (default 2) were queued for it in 24 hours;
    - `COORDINATOR_MAX_REBUILDS_PER_LINEAGE` (default 3, 0 switches automatic rebuilds off) were already recorded for the lineage's root in 7 days.
  - An accepted rebuild inserts the child build and one `build_start_events` row (kind `rebuild`, filed under the root's id; the lineage count reads only that kind, so a conversation's first build, written by the web app as `dispatch` under the same root id, is never counted as an automatic rebuild) in one transaction.
  - The coordinator has no method or permission that approves or promotes a build (the `approve_build` method and the `build_approve` permission were deleted; `tests/test_coordinator_actuator_gates.py` asserts that `Actuators` has neither).
  - An approval request (`create_approval_request`, which needs no permission) is filed only by an agent's escalation (`tick._check_escalation`, when a handler returns `escalate=True`: infra, financial, customer service and security do) and by the reconciliation agent. It is advisory: `/admin/approvals` only changes its status, nothing executes it, and an unanswered request is rejected after `COORDINATOR_APPROVAL_TIMEOUT_HOURS` (4 by default).
  - `query_readonly` runs exactly one statement, counted by a lexer for PostgreSQL text (`coordinator/sql_statements.py`; more than one is a `PermissionError`, checked before the connection is opened and again on the text psycopg2 sends), in `BEGIN READ ONLY` on a session whose `default_transaction_read_only` is on, so PostgreSQL refuses the writes a single statement can make. The refusal of the plainest writes by first keyword is a courtesy, not the guarantee. Connecting and the statement each time out after 15 s.
  - `create_approval_request` files at most one pending request per (type, description).

## Agents

**Model usage.** The role agents are rule-based Python and make no model calls: `infra_engineer`, `financial_manager`, `documentation`, `security_engineer`, `qa_engineer`, `backend_engineer`, `cost_analyst`, `architect` and `reconciliation`.

The model named for each role in `scorer.AGENT_MODELS` only sets the cost the scheduler reserves for the task and the cap on upgrades.

The only coordinator code that calls a model (Haiku) is `agents/customer_service.py` (drafting a support answer for a detected KB gap, capped at 5 per run), `failure_kb_sync.py` (drafting support answers from failure-KB entries; it uses its own cheap-tier default whichever tier the infra task that runs it was dispatched on) and `build_failure_categorizer.py` (classifying a failed build's error).

Each of the three calls the model only after `Actuators.authorize_model_call` returned True for that one call, so none of them calls it under dry run, without the `model_call` permission, or without a call left in the tick's `ModelCallMeter`, and each wraps customer or log text with `untrusted.wrap_untrusted`.

Outside the coordinator, `monitor/diagnose.py` uses Haiku and then Opus.

- `agents/base.py`: Agent ABC with `execute()` method, `AgentResult` dataclass (success, output, actions_taken, api_calls, cost_usd, learnings, follow_up_work, escalate, `effects`), the `Effects` helper and `Agent.model_categories`.
  - `AgentResult.effects` is what became of each alert, finding, KB entry, rebuild and approval request an agent asked for: sent, recorded, queued, dry_run, refused or failed. `Effects` never raises `PermissionError`; the tick stores the list in the task's `result` under `effects` and logs a refused or failed one. An alert-only task's `success` and `output["action"]` say what happened (`alert_sent`, `alert_dry_run`, `alert_refused`, `alert_failed`).
  - `Agent.model_categories` is the set of categories that can reach a model call. Anything else (`None`, a class that does not say, an unregistered agent) counts as "may call" (`_task_may_call_model` in `tick.py`); `tests/test_coordinator_model_free_tasks.py` checks each declaration against the source.
- `agents/infra_engineer.py`: Phase 1 agent. Wraps `ops/scanner.scan_recent_failures()` + `ops/playbook.match_playbook()` + fixer dispatch. Handles `build_failure`, `app_down`, `backup_overdue`, `coordinator_stalled` and `agent_initiative` categories. Translates ops results into `AgentResult` format. The ops playbooks are deterministic.
  - The agent runs a playbook through `Actuators.run_playbook`, which decides whether the run is live (not in dry run, with the playbook's permission, inside the circuit breaker) and logs the action under the name the review step reads; the fixers in `ops/playbooks/` reach GitHub and Railway through `ops/fixer.py` on their own, so `run_playbook` is the only gate in front of them. The weekly failure-KB sync (`failure_kb_sync.py`) runs from this agent's initiative handler.
- `agents/reconciliation.py` - `ReconciliationAgent`. Background sweep (every `COORDINATOR_RECONCILIATION_INTERVAL_H`) of `builds` rows with status `failed`, completed in the last 72 hours, whose `error_message` mentions a supervisor, timeout, force-fail or watchdog; the rows are read through `Actuators.query_readonly`.
  - It calls `queue/watchdog_probe.probe_for_zombie_deploy()` for each, leaving out every Railway project that any build row with the same `app_name` records, the failed row's own earlier, announced deployment included (`_recorded_projects`; the worker writes `builds.railway_project_id` only at preview, so a recorded project is never a failed attempt's leftover).
  - Its candidate query could not run against a real database before this release (a literal percent sign in the SQL text met a parameter tuple, so `query_readonly` failed on every sweep); the patterns and the window are parameters now, and the sweep is covered by real-database tests.
  - It is off by default. For a build confirmed live it files a `reconciliation_flip_to_preview` approval request (`create_approval_request`) and sends one alert email (`send_alert`).
  - It writes no build row and holds no permission that could: the agent's only grant is `email_send`. Moving the build to `preview` is left to the person who reads the request, and approval requests are advisory (nothing in this repository executes one when approved), so the supervisor's own reality check in `queue/worker_pg.py` is the code that actually writes `preview`.
  - Under dry run it files and sends nothing. It never sets `live`. Gated by `AGENT_RECONCILIATION_ENABLED`.
  - `probe_for_zombie_deploy()` is fail-safe (never raises), returns `{live_url, project_id, service_id}` or None.
  - Tests: `tests/test_reconciliation_agent.py`.
- `agents/financial_manager.py`: handles budget alerts (sends email when daily budget low) and, on its turn of initiative work, emails a cost summary (pure SQL aggregation of build + agent costs, no model call; the daily cost report is the cost analyst's). Triggers: `budget_alert`, `agent_initiative`.
- `agents/customer_service.py`: triages beta feedback: classifies severity (high/medium/low based on keywords, no model call), checks the support KB for gaps and drafts answers for them with Haiku, alerts on bugs, escalates high-severity. Triggers: `customer_feedback`, `agent_initiative`.
- `agents/documentation.py`: one check, on its turn of initiative work: every table created in `schema.sql` is named in CLAUDE.md, unless CLAUDE.md hands the schema over to that file (then there is nothing to compare). Pure string comparison of two files in the worker's checkout; no model call, no subprocess, no edit. Drift is filed as one finding and one alert email. It does not check test counts (the worker image has no pytest) or files added since a commit (the image has no `.git`). Triggers: `agent_initiative`.
- `agents/security_engineer.py`: post-deploy security audits (HTTPS, sensitive fields in spec, QA security failures) and daily health scans of all live apps. Escalates on 3+ high-severity findings. Triggers: `post_deploy_audit`, `security_scan`, `agent_initiative` (the daily scan).
- `agents/qa_engineer.py`: on its turn of initiative work, lists the live builds that got a rebuild request within 48 hours of going live. Read-only and rule-based: it runs no tests and holds no permission. Triggers: `agent_initiative`.
- `agents/backend_engineer.py`: handles recurring QA warnings, failed builds that are not an infrastructure problem, the periodic test build and, on its turn of initiative work, a read-only look at the last seven days of builds, with rule-based handlers (no model call). Can queue rebuilds (`actuators.queue_rebuild`, bounded per build, per lineage and per customer) and a new sample build (`queue_new_build`). It has no way to approve or promote a build. Triggers: `recurring_qa_warning`, `build_failure`, `test_build`, `agent_initiative`.
- `agents/cost_analyst.py`: analyzes API costs, generates scaling projections (10/50/100/500 customers), produces daily and weekly cost reports. Pure SQL + math, no model call; the daily report reads the budget's own ledger statements through `query_readonly` and says so when the cost report could not be read. Triggers: `cost_projection`, `cost_report`, `agent_initiative`.
- `agents/architect.py`: reviews build specs and finished builds, and looks at recurring QA patterns for systemic issues. Triggers: `spec_review`, `post_build_review`, `architecture_qa_review`, `agent_initiative`. It sends no email: what it finds is the task's result, and the findings of its initiative review are filed by the tick under its name. Rule-based (spec shape, counts, ratios) and makes no model calls; `AGENT_MODELS` lists Sonnet with an Opus upgrade for it, but that selection is not used by the agent and affects only the reserved budget.

## Env vars

- `COORDINATOR_ENABLED` (default false)
- `COORDINATOR_DRY_RUN` (default true)
- `COORDINATOR_INTERVAL_S` (default 1800; a value that is not a whole number of at least 1 means 1800)
- `COORDINATOR_DAILY_BUDGET` (default 20.0 USD; read on every tick)
- Per-agent toggles: `AGENT_INFRA_ENABLED`, `AGENT_FINANCIAL_ENABLED`, `AGENT_CUSTOMER_ENABLED`, `AGENT_DOCS_ENABLED`, `AGENT_SECURITY_ENABLED`, `AGENT_QA_ENABLED`, `AGENT_BACKEND_ENABLED`, `AGENT_COST_ENABLED`, `AGENT_ARCHITECT_ENABLED` (unset means enabled; the tick still respects dry run)
- Approval timing: `COORDINATOR_APPROVAL_REMINDER_HOURS` (default 3) and `COORDINATOR_APPROVAL_TIMEOUT_HOURS` (default 4); unanswered requests are rejected, never approved
- Actuator limits: `COORDINATOR_MAX_REBUILDS_PER_BUILD` (default 2 per 24h), `COORDINATOR_MAX_REBUILDS_PER_LINEAGE` (default 3 per root ancestor per 7 days; 0 switches automatic rebuilds off), `COORDINATOR_ANALYSIS_ONLY` (narrows every agent: an action must be in the analysis-only permission set and in the agent's own `AGENT_PERMISSIONS` entry)
- Platform targets: `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`, `RAILWAY_WEB_SERVICE_ID`, `RAILWAY_WORKER_SERVICE_ID`
- `AGENT_RECONCILIATION_ENABLED` (default false: gates `coordinator/agents/reconciliation.py` `ReconciliationAgent`; when enabled, sweeps recently failed timeout builds every `COORDINATOR_RECONCILIATION_INTERVAL_H` hours and files approval requests for confirmed zombie deploys)
- `COORDINATOR_RECONCILIATION_INTERVAL_H` (default 2: hours between reconciliation sweeps)

## Safety

- Starts in dry-run mode.
- Every write, send and model call goes through `Actuators`, which checks `AGENT_PERMISSIONS`, honors `COORDINATOR_ANALYSIS_ONLY` and does nothing under dry run, except `finding_add`, which only files an entry for a person to review. Reads and the tick loop's own bookkeeping are outside it (see the `actuators.py` entry above); a test scans `coordinator/` for any effect made elsewhere.
- A tick gets no budget when today's spend cannot be read from its one configured source (see `budget.py`): zero dollars and zero model calls, with no earlier figure substituted. Within a tick the allowance caps how many tasks that can reach a model call run, at their estimated cost; it does not measure spend. A task whose agent declares its category in `model_categories` as model-free draws on an unmetered allowance, so a tick with no budget still runs it and still makes no model call. Every model call, failure triage included (up to five cheap-tier calls per tick), is granted one at a time by the tick's `ModelCallMeter` inside `Actuators.authorize_model_call`, and none is made under dry run. The meter counts calls at fixed per-model estimates, not tokens.
- Without `ANTHROPIC_ADMIN_KEY` the budget runs on the worker's own ledger, which omits web-tier spend and records an estimate, not a measurement, for each agent task. The ledger's day is the database session's `CURRENT_DATE` while the per-tick split uses UTC; they differ when the database time zone is not UTC.
- Customer builds always have priority.

## Integration

Runs as daemon thread in `worker_pg.py` (`_start_coordinator_thread`). When `COORDINATOR_ENABLED` is true, replaces the legacy ops agent thread. Dry run unless `COORDINATOR_DRY_RUN=false`. Respects `_shutdown_event` for graceful shutdown.

## DB tables

`coordinator_ticks`, `agent_tasks` and `agent_learnings` (all `CREATE TABLE IF NOT EXISTS` in `schema.sql`). The coordinator no longer reads or writes `agent_learnings`: its rows went into an argument no agent used, and the table belongs to the web tier (admin routes, the weekly sweeps and the persona digest). `schema.sql` also still creates `agent_performance`, but nothing in this repository reads or writes it: the code that wrote to it used columns the table does not have, so every write failed (and its rollback discarded the tick's learnings), and it was deleted. Success rates are counted from `agent_tasks`.

## Tick watermark and dispatch

- Each tick takes a watermark from the database clock at its start, stores it as `coordinator_ticks.started_at` and returns it in its summary (`run_tick`). The triggers that look for new rows, and the daily and weekly schedules, work on the half-open interval (previous watermark, this watermark] (`_schedule_due` runs the most recent slot once when the previous watermark lies before it), so a row written while a tick runs belongs to the next tick and none is skipped or seen twice.
- The loop advances its watermark only on a summary that carries one. A skipped tick, or one that failed before its work items were committed, does not.
- A process with no watermark of its own (the first tick after a restart) resumes from the start of the last completed tick in the database (`_previous_watermark`), at most `WATERMARK_MAX_LOOKBACK_HOURS` (24) back. A tick is skipped when the previous one was less than half the interval ago, at most 5 minutes (overlap protection).
- Every dispatch with `DATABASE_URL` set runs on a pool thread with its own connection and a deadline, so one hung agent cannot hang the tick. An agent that raises has its task marked failed at once (`_fail_task_after_exception`), so the event can be filed again.
- A task whose agent runs past the round's deadline is marked failed then (status `failed`, with `timed_out: true` in its result); its actuators refuse from that moment (every gated call and `create_approval_request`), and its late result is discarded (`TaskClosedElsewhere`). The thread is not stopped: its reads continue, a model call authorized before the deadline can still complete, and the task's budget reservation is kept for that reason.
- A row whose transaction began before a tick's watermark and committed after that tick's queries ran is seen by no tick.

## Failure circuit and initiative turns

Before dispatch, `tick._failure_circuit` looks at each (agent, category) pair's recent outcomes. For work below P0 the pair is skipped when it has three or more live outcomes in the window (`FAILURE_RATE_WINDOW`, 10), the newest failed, and at least 80% failed (`FAILURE_RATE_THRESHOLD`). Once the newest outcome is `FAILURE_RETRY_HOURS` (6) old, one task per pair per tick is let through as a probe, and a success closes the circuit (a skipped task writes no outcome, so without the probe an open circuit could never see the success that closes it).

A history that cannot be read holds the circuit. Outcomes written under dry run are marked in `agent_tasks.result` (`state.DRY_RUN_RESULT_KEY`) and are left out of the circuit and of the scorer's performance counts, so the default dry run does not trip it. Success and failure counts for scoring come from the `agent_tasks` rows the tick writes (`scorer.build_performance_cache`, read once per tick). An agent's initiative turn comes at most once in 7 days (`INITIATIVE_INTERVAL_DAYS`). Stalled-coordinator auto-check fires after `STALLED_MINUTES` (30) of zero dispatches.

## Roadmap dispatch disabled 2026-04-04

The coordinator's roadmap trigger (`_check_roadmap_next`) was disabled on that date and has since been deleted. The local executor (`scripts/cas-roadmap-loop.sh`) has exclusive ownership of `coordinator/roadmap.json`. Rationale: both systems previously read the same queue with no cross-coordination, causing every item to be dispatched 2x (coordinator + executor race window). See the `TestDetectWorkRoadmapDisabled` regression guard.

## Tests

Counts drift; each file's docstring says what it covers.

- Coordinator core:
  - `tests/test_coordinator.py` (proactive triggers, actuator queue/approve, platform improvement, cost projection, agent initiative, roadmap dispatch disabled regression guard, config module, dedup hardening, failure routing, agent timeouts, adaptive scoring, approval gates, state cache, budget cap, cursor context managers), `tests/test_coordinator_actuators.py`,
  - `tests/test_coordinator_actuator_gates.py` (the permission and dry-run matrices, the call-pattern scan and the import scan for capabilities outside `actuators.py`),
  - `tests/test_coordinator_tick_realdb.py` (a real tick, `run_tick(dry_run=False)`, on a real schema: both spend sources, a statement made to fail on the tick's own connection, the model-call meter, and what an agent returns surviving the tick),
  - `tests/test_coordinator_routing.py` (every trigger defined is called; every pair a trigger emits is accepted by the target agent's real `execute()`; every dispatch branch and every `_handle_*` method is reached by an emitted work item, with no exception list), `tests/test_coordinator_permission_use.py` (each of the 25 (principal, permission) pairs asked for and granted through the real handler), `tests/test_spend_tracker.py`,
  - `tests/test_build_failure_categorizer.py` (the gated triage path),
  - `tests/test_coordinator_guardrails.py` (includes `TestUntrustedText`, the wrapper cases, and `TestTickAllowanceDepletes`),
  - `tests/test_failure_kb_sync.py`, `tests/test_coordinator_budget.py`, `tests/test_coordinator_unknown_is_not_zero.py` (a cost report that cannot be read, or lacks today, is unknown spend; unknown is `None` and is read restrictively) and the failure-circuit cases in `tests/test_coordinator_tick_realdb.py`, `tests/test_coordinator_scorer.py`, `tests/test_coordinator_state.py`, `tests/test_coordinator_tick.py`, `tests/test_coordinator_work_detection.py`.
- Per agent:
  - `tests/test_infra_agent.py`, `tests/test_financial_agent.py`, `tests/test_customer_service_agent.py`, `tests/test_documentation_agent.py`, `tests/test_security_agent.py`, `tests/test_qa_agent.py`, `tests/test_backend_engineer_agent.py`, `tests/test_cost_analyst_agent.py`, `tests/test_architect_agent.py`, `tests/test_reconciliation_agent.py`.

## Analysis-only mode & findings

An agent whose primary action is blocked (analysis mode, missing permission, etc.) has one output channel, **`finding_add()`**: it inserts into the `coordinator_findings` Postgres table, the shared coordination surface across all environments and worktrees (no merge conflicts, no path juggling). Agents in analysis-mode fallback paths call it directly. A second channel, `roadmap_add()`, which appended to `coordinator/roadmap.json`, was removed: no coordinator code writes that file now.

`coordinator_findings` columns: `id`, `source_task_id` (FK to `agent_tasks`), `agent`, `category`, `finding_key` (unique, the dedup key), `title`, `description`, `priority` (low/medium/high), `status` (new/reviewed/promoted/dismissed), `roadmap_item_id` (set on promote), `review_notes`, `created_at`, `reviewed_at`, `reviewed_by`. Insert uses `ON CONFLICT (finding_key) DO NOTHING`, callers can retry safely.

Admin review surface: `/admin/findings` lists findings by status, priority, and category. Actions:
- `POST /admin/findings/:id/review`, mark reviewed (no promotion)
- `POST /admin/findings/:id/promote`, mark promoted; requires a `roadmap_item_id` form field so the intent is tracked even if the caller still has to add the item to `roadmap.json` manually
- `POST /admin/findings/:id/dismiss`, mark dismissed; rejects will not re-fire for the same `finding_key`

Permissions: `finding_write` is held by the six agents that file findings (infra, security, backend, customer service, documentation and architect); `qa_test` holds no permission, and the `failure_triage` principal does not hold this one. `COORDINATOR_ANALYSIS_ONLY=true` only narrows: `Actuators._check_permission` first rejects any action outside `_ANALYSIS_ONLY_PERMISSIONS` (`kb_record`, `model_call`, `finding_write`, `email_send`), then applies the agent's own grants as usual, so the mode never gives an agent a permission it lacks (the `qa_test` agent, for instance, still cannot send email in this mode).

The set includes `finding_write` so analysis-mode always has a working output channel. Dry run still wins over analysis-only: `support_kb_insert`, `record_fix` and `record_failure_category` write nothing under dry run even though `kb_record` is in the analysis-only set.

### Auto-surfacing initiative findings

Agents that run in the `agent_initiative` rotation (backend_engineer, architect, security, cost_analyst, financial, etc.) often return rich analytical output; e.g., `"QA warning rate elevated: 3/5 (60%)"`, `"Retry rate elevated: 0.8 avg"`, cost projections. Before 2026-04-18 this output lived only in `agent_tasks.result` JSONB and was invisible to the admin.

`_surface_initiative_findings(task, result, dry_run)` (in `coordinator/tick.py`) is called after every successful dispatch (both parallel and serial paths). If `result.output["findings"]` is a non-empty list of non-empty strings, each entry becomes a row in `coordinator_findings` via `Actuators.finding_add()`:

- **finding_key:** `init-{agent}-{sha256(text)[:12]}`, stable, so repeat discovery dedups via the unique index
- **category:** `initiative_{agent}`, filterable on `/admin/findings`
- **priority:** `medium` (agents don't currently set per-finding priority)
- **source_task_id:** the originating `agent_tasks.id` for traceability
- **title:** `[{agent}] {text[:100]}`; full text stored in `description`

The helper never raises: `PermissionError`, `RuntimeError`, and DB failures are all logged and swallowed so they can't break the tick.

To make agent output surface-able: ensure the agent's `execute()` puts insights in `result.output["findings"]` as `list[str]`. Empty/whitespace/non-string entries are filtered. Failed agent runs (`result.success == False`) are skipped; failures belong in the alert/escalation path, not the findings queue.

Regression coverage: `tests/test_coordinator.py::TestSurfaceInitiativeFindings` (empty/missing/wrong-type findings, stable dedup keys, failed-agent skip, permission + DB error resilience, title truncation, per-agent category, dry_run passthrough).

### Promoted findings → local roadmap.json sync

There is no sync any more. What happens to a promoted finding:

- Promoting a finding (`POST /admin/findings/:id/promote` in `web/src/routes/admin.ts`) sets `status='promoted'` and stores the `roadmap_item_id` the admin typed on the database row. It writes nothing to `coordinator/roadmap.json`; the route's comment says the person promoting is expected to add the roadmap item by hand.
- Nothing else writes the roadmap from a finding. A promoted finding reaches `coordinator/roadmap.json` only if a person adds an item with that id. No coordinator code writes that file (`Actuators.roadmap_add` is gone), and the only other writer is the executor loop, which changes the status, and at most one note field, of an item that is already there (`docs/LOCAL_EXECUTOR.md`).
- The scripts that used to copy promoted findings into the roadmap and push the result (a sync script, its commit-and-push wrapper, an installer and a LaunchAgent plist) were removed in this release, with their tests.
- The schema still has `coordinator_findings.synced_at` and its partial index `idx_coordinator_findings_unsynced`. Nothing in this repository reads or writes either; the comment above them in `web/src/db/schema.sql` says so.

**Operator flow:**
1. On `/admin/findings`, click Promote on a finding and enter a roadmap item id. That records the intent on the finding.
2. Add an item with that id to `coordinator/roadmap.json` and commit it. The executor yields while that file differs from HEAD.
3. When the executor is enabled, its next cycle picks the item up unless the item is marked `executor_eligible: false`, is blocked, or is judged complex (`cas-pick-item.py`).

## Transaction poisoning: always rollback on caught SQL exceptions

Every helper that takes a psycopg2 `conn` and catches an exception MUST call `conn.rollback()` in its except block. If it doesn't, psycopg2 leaves the connection in `InFailedSqlTransaction` state and every subsequent operation on that conn errors with `"current transaction is aborted, commands ignored until end of transaction block"`; cascading through the rest of the tick and preventing `_write_tick_record` from writing `completed_at`.

Observed 2026-04-18: 82 consecutive ticks with 0 completions. Root cause was `_update_agent_performance` and `build_performance_cache` both catching exceptions without rolling back. When only a single task was queued (common for `agent_initiative`), the serial dispatch path used the main conn, a silent SQL failure in one of those helpers poisoned the main conn, and the final tick record UPDATE failed.

Fix applied at the time: rollback added to both helpers, the learning-decay call site wrapped with rollback-on-error (that code has since been deleted), and `_write_tick_record` defensively rolls back at start so the tick always gets its final state persisted.

That fix hid a second defect rather than removing it: `_update_agent_performance` wrote columns the `agent_performance` table does not have, so it failed on every call, and its rollback then discarded the learnings, follow-up work and review flag of every task dispatched before it. The function is gone, success counts are read from `agent_tasks`, and each task's result is committed before the next task runs (`test_what_an_agent_returns_is_still_there_after_the_tick` in `tests/test_coordinator_tick_realdb.py`, a real tick on a real schema).

Regression guard: `tests/test_coordinator.py::TestTransactionPoisoningGuards` (the remaining helper plus source-level check that the decay call site rolls back).

**Rule for new helpers:** any new function that takes a `conn` and swallows an exception MUST include `try: conn.rollback() except: pass` inside the except block. Preferred pattern:

```python
try:
    with conn.cursor() as cur:
        cur.execute(...)
except Exception as exc:
    logger.debug("... failed: %s", exc)
    try:
        conn.rollback()
    except Exception:
        pass
```
