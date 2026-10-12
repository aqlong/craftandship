# Running the Craft & Ship Test Suite

Two Python test directories sit beside the web app's tests:

| Directory | What it tests |
|---|---|
| `tests/` | The worker, the v1 pipeline, the coordinator, the monitor, the ops agent, the scripts and hooks, deploy, mail |
| `../compose/tests/` | The v2 engine: it composes sites, renders their EJS and, for some tests, boots a generated site |
| `../web/tests/` | The web app (Vitest unit tests, Playwright browser tests); see `docs/06_TESTING.md` |

All commands below run from the repository root.

## Python version and install

Python 3.12 is the supported version for the test requirements; it is what CI runs. On Python 3.14,
`pip install -r requirements.worker.txt` fails while building `greenlet` from source: the
`playwright==1.42.0` pin in `requirements.worker.txt` (it must equal the Playwright tag of the base
image in `Dockerfile.worker`) requires `greenlet==3.0.3`, and that release has no wheel for 3.14.
Use a 3.12 interpreter:

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.worker.txt -r requirements-dev.txt
cd web && npm ci        # optional, but see "Why a test is skipped"
```

`requirements-dev.txt` adds pytest, Pillow and the audit tools (bandit, pip-audit).

## Run

```
python3 -m pytest tests/ compose/tests -q -rs            # everything that can run here; -rs prints each skip reason
python3 -m pytest tests/test_security.py -q               # one file
CAS_BUILD_ENGINE=v1 python3 -m pytest tests/ -q           # also the legacy v1 pipeline tests
```

`make test` runs `tests/` and `compose/tests/`. `tests/` and `compose/tests/` take about 15 minutes together on a developer machine (measured on one Mac), because `test_executor_loop_behavior.py` and `test_executor_loop_recovery.py` run the real executor loop in bash against throwaway repositories (CI runs them once, in the default pass). `compose/tests` takes about three and a half minutes (216 seconds in the last full run, with the generated-site modules skipped). The suite makes no real network calls: the root `conftest.py` blocks
every connection except loopback (a blocked attempt raises `NetworkBlockedError` and is listed at the
end of the run), so a test that would reach a network must mock it. `CAS_TESTS_ALLOW_NETWORK=1` keeps
the guard from being installed, for a deliberate live run. Once installed, the guard is never lifted,
not even at the end of the session: a test can leave a thread running past the last test (a build thread
blocked in a model call, for example), and a guard removed at that point would let the thread reach the
network. The guard does not cover child processes (`curl`, `git push`, `npm install`) or libpq.

Many of the files under `tests/` are named after the tickets that produced them (`test_mw_v17_*`,
`test_pipeline_bug_*`, `test_security_sec1_*`), not after the behavior they pin; the docstring at the top
of each says what it covers. Three files are worth reading before any of the others, because they run
real things rather than text: `compose/tests/test_spec_hardening.py` (hostile specs through the gate and
the writer, with the output compiled), `tests/test_worker_pg_realdb.py` (the worker's guarded SQL against a
real database) and `web/tests/unit/money-path.realdb.test.ts` (the publish, payment, signup and crisis
routes, the conversation's notes through payment, the build-start limits on a dispatch, the address a
payment is tied to and the weekly sweeps' claim, against a real database).

`compose/tests/test_spec_shape_matrix.py` is the one test file whose size you choose. It puts each of 16 values at every position of ten sample specs: the 14 wrong ones (a number, a fraction, true, false, null, an empty list, a list, an empty object, an object, an empty string, another string, a 20,000-character string, a 300,000-character string, and no key at all) and two valid column types, a text type and a number type, at the positions that hold one. It requires `validate_spec` to refuse the spec or the built site to parse every script, compile every view and render every GET route as a visitor and as the admin. The whole product is 30,039 cases for a v2 spec and 2,870 for a v1 spec. The default run takes a fixed sample of them (243 and 152 cases; 44 and 19 seconds in the last run here); two more tests run built sites' SQL on a real PostgreSQL (the columns the engine reads with a fixed meaning, 108 cases, and 38 cases of the whole product; 13 and 11 seconds), and the whole file took 93 seconds. `CAS_SPEC_MATRIX=full` runs every case: it took about 48 minutes when the product was 25,163 v2 cases and was not re-timed since it grew; `CAS_SPEC_MATRIX=<n>` runs one case in n. It needs `node` and `web/node_modules`, and the two PostgreSQL tests skip without a local server. The main mode changes one position at a time, renders with one stand-in row per table, runs no SQL and calls no POST handler.

## How the Postgres-backed tests find a server

Most tests that need a database use the `scratch_pg_url` fixture in `tests/conftest.py`:

1. It picks the server. `DATABASE_URL` if it is set, and only if it names this machine (`localhost`,
   `127.0.0.1`, `::1` or a Unix-socket directory); any other host skips the test rather than create a
   database somewhere else. With `DATABASE_URL` unset it connects to the PostgreSQL on localhost that
   the current OS user can reach, using the `postgres` maintenance database to issue `CREATE DATABASE`.
2. It creates one empty database, `cas_test_scratch_<12 hex digits>`, for the whole session.
3. Tests create their own tables inside it (the worker's real schema in `test_worker_pg_realdb.py`, the
   monitor registry in `test_monitor.py`, the ops audit table in `test_ops_audit.py`, and so on).
4. At the end of the session it runs `DROP DATABASE ... WITH (FORCE)` on that one database. It never
   connects to, alters or drops any other database.

If psycopg2 is missing, `DATABASE_URL` is not local, no server answers within 3 seconds, or the role
cannot create a database, the test skips with that reason. Five other places create a database of their
own, each with a unique name, and drop it afterwards:

- `tests/test_coordinator_tick_realdb.py` creates `cas_test_coordinator_<10 hex digits>` through the
  scratch database, to hold the web app's `schema.sql` for a real coordinator tick;
- the generated-site tests in `compose/tests` (`boot_generated_app` in `test_compose_end_to_end.py`, also
  used by the other four modules that boot a site, named below) choose the server the same way (TCP
  only) and create `cas_test_e2e_<10 hex digits>`;
- `tests/test_worker_pg_realdb.py` creates `cas_test_worker_<10 hex digits>` on the same server, to hold
  the web app's `schema.sql` for the worker's status SQL (`tests/test_worker_lifecycle_realdb.py` uses
  the same fixture);
- the tests that run a built site's SQL without booting it (`compose/tests/test_engine_columns.py`, `test_stored_values.py` and two tests of the wrong-type matrix, through `compose/tests/_scratch_pg.py`) create and drop `cas_test_cols_<10 hex digits>` on the same server, or use the database `CAS_COMPOSE_TEST_DATABASE` names (its `public` schema is replaced by each of them, so name one that holds nothing else; the booted-site modules give each boot a schema in it instead);
- `tests/test_v2_local_e2e.py` (opt-in) runs `createdb`/`dropdb` too (`cas_v2_e2e_<8 hex digits>`).

A leftover `cas_test_*` database after a crashed run is safe to drop by hand.

## Why a test is skipped

A skip names its reason in the `-rs` summary. These are the real categories:

| Why it skips | How to make it run |
|---|---|
| **v1-only test.** Files that exercise the legacy Claude-per-build pipeline carry `pytest.mark.v1_only` and are listed in `_V1_ONLY_FILE_PREFIXES` in the root `conftest.py`, which also keeps them from being collected | `CAS_BUILD_ENGINE=v1 python3 -m pytest tests/ -q` |
| **`v2_integration` is opt-in.** `tests/test_v2_local_e2e.py` composes every sample spec, boots it on a local Postgres and follows the real login flow (the link's confirmation page, then the POST to `/magic/consume`), which takes minutes and runs `npm install` | `python3 -m pytest -m v2_integration tests/test_v2_local_e2e.py`; needs `psql`, `createdb`, `dropdb`, `node` and `npm` on `PATH` and the npm registry |
| **EJS render tests need the web dependencies.** The widget, layout, faithful-copy, widget-parameter, wrong-type matrix and spec-hardening tests render EJS with the `ejs` package from `web/node_modules`, and `compose/tests/test_generated_app_runtime.py` also needs `express`, `express-rate-limit` and `bcrypt` there | `cd web && npm ci`; `node` must be on `PATH` too |
| **Generated-site tests.** 103 tests in five modules in `compose/tests` (all skipped without this install) boot a generated site: `test_compose_end_to_end.py`, `test_smoke_suite.py`, `test_generated_app_guards_e2e.py`, `test_generated_site_security_e2e.py` and `test_generated_site_publication_e2e.py`. They need `builds/sample-healer/node_modules` as well as `node` and a local Postgres | From the repository root: `CAS_LINT_EXTERNAL_URLS=skip python3 -m compose.build compose/fixtures/sample-healer-spec.json`, then `cd builds/sample-healer && npm install --no-audit --no-fund`. `builds/` is gitignored; the generated app ships no lockfile, hence `npm install`. `CAS_LINT_EXTERNAL_URLS=skip` stops the build from sending HEAD requests to the image hosts named in the sample spec |
| **Real-database column tests.** `test_engine_columns.py`, `test_stored_values.py` and two tests of `test_spec_shape_matrix.py` (the columns the engine reads with a fixed meaning, and a sample of the whole matrix) run a built site's schema, seed and statements on a local PostgreSQL | Start a local PostgreSQL that your OS user can create databases on (psycopg2 and a server on this machine; `DATABASE_URL` is honored only when it is local), or name a database in `CAS_COMPOSE_TEST_DATABASE` |
| **No Postgres.** Everything that uses `scratch_pg_url` (see above), including `test_worker_pg_realdb.py`, `test_worker_lifecycle_realdb.py`, `test_coordinator_tick_realdb.py`, `test_coordinator_agent_effects.py`, `test_coordinator_readonly_query.py`, `test_postgres_client_version.py`, the real-Postgres classes in `test_monitor.py`, `test_backup_infra.py`, `test_ops_audit.py` and `test_build_failure_categorizer.py` | Start a local PostgreSQL that your OS user can create databases on, or set `DATABASE_URL` to one on this machine |
| **`requires_tools`.** A test that shells out to `git`, `node`, `pg_dump`, `psql` and the like carries `@pytest.mark.requires_tools(...)` and skips, naming the missing program. A few executor and hook tests also need `bash`, `npm` and `python3` | Install the named program |
| **Pillow missing.** The release-workflow image checks | `pip install -r requirements-dev.txt` |
| **Network guard off.** `tests/test_network_guard.py` tests the guard itself | Unset `CAS_TESTS_ALLOW_NETWORK` |
| **Browser specs without their fixtures** (Playwright, `web/tests/ui`). The admin spec wants an admin login, one password-reset case a token seeded in the database, and the keyboard and screen-reader specs a trial and a preview build to walk | `ADMIN_TEST_EMAIL` and `ADMIN_TEST_PASSWORD`; `VALID_RESET_TOKEN`; the `CAS_KB_*` and `CAS_SR_*` variables named at the top of `keyboard-tab-order.spec.ts` and `screen-reader-conversation.spec.ts` |
| **Past build outputs that are not in this repository** (v1 mode only). `test_b_plus_c_integration.py` wants a `builds/medicine-waves-v19-*` snapshot, `test_pipeline_byte_identical.py` the hash maps of the pre-DAG pipeline output, `test_security_sec1_seed_password_hash.py` a gold-standard build snapshot | Cannot be made to run here |

## What these tests can and cannot show

**The web unit tests do not run SQL.** `web/tests/unit` replaces the database with query mocks, so a
mocked test proves which statement a route sends and what the route does with the rows it is handed,
not what PostgreSQL does with that statement. SQL is executed by the browser suite (Playwright, against a
seeded database), by the web integration files `schema.idempotency.test.ts`,
`money-path.realdb.test.ts` and `crisis-phrasing.realdb.test.ts` (the crisis scan at the email gate of a
real conversation) when `CAS_SCHEMA_TEST_DATABASE_URL` names an empty throwaway database (they
are skipped otherwise; run them serially, because each applies `schema.sql`:
`cd web && CAS_SCHEMA_TEST_DATABASE_URL=postgresql://localhost:5432/<empty database> npx vitest run --no-file-parallelism tests/unit/schema.idempotency.test.ts tests/unit/money-path.realdb.test.ts tests/unit/crisis-phrasing.realdb.test.ts`),
and on the Python side by the tests that use `scratch_pg_url`: `tests/test_worker_pg_realdb.py` (the
worker's status writes, claim, orphan recovery and supervisor SQL), `tests/test_worker_lifecycle_realdb.py`
(one build followed across the worker's seams: Railway projects, the health gate, the claim, the janitor,
cancellation, the resume marker) and `tests/test_coordinator_tick_realdb.py` (a real coordinator tick on
the real schema). The same goes for the Python side generally: `tests/test_worker_pg.py` mocks the
connection, so a passing mocked test says nothing about the `WHERE` clause.

One test in `money-path.realdb.test.ts` crosses from TypeScript into Python: it runs the worker's real
cleanup of old failed builds (`_cleanup_old_builds`) in a child process, between the web app's own
statements, because the payment rule it guards once read rows that cleanup deletes. That test needs a
Python that can import `queue/worker_pg.py`: set `CAS_WORKER_PYTHON` to an interpreter with
`requirements.worker.txt` installed (CI does), and it must run and fails if the import fails. With
`CAS_WORKER_PYTHON` unset it tries `python3` and, if that cannot import the worker, skips and prints the
reason to stderr; the other tests in the file need no Python and always run.

**The web unit suite does not read `web/.env`.** `src/app.ts` and `src/db/index.ts` call `dotenv` at import
time, and the quickstart tells a reader to create `web/.env` (with `NODE_ENV=development` in it), which
used to turn a test that means "`NODE_ENV` is unset" into a test of development mode. The Vitest setup
file `web/tests/unit/setup-no-dotenv.ts` (named in `web/vitest.config.ts`) replaces `dotenv` with a
no-op for the whole unit suite, so `cd web && npm test` gives the same result with or without that file.

**Some tests are tripwires, not proofs of behavior.** They read source text, or the text a generator
emits, and fail when someone edits it by accident. A pass means the text is still there, not that the
code behaves. Examples you can open:

- `tests/test_worker_pg.py::TestV2DeployFixSkip` (`test_source_v2_raises_instead_of_running_deploy_fix`
  and its neighbours) read `queue/worker_pg.py` as a string and assert that the v2 gate contains
  `if _engine == "v2":`, `raise RuntimeError` and the `[v2-deterministic]` tag.
- `tests/test_security.py` scans `cli/`, `monitor/` and `queue/` source for secret-shaped strings and for
  `shell=True`, and checks that `.env` is in `.gitignore`: structural checks over text.
- `tests/test_release_workflow.py` says of itself that most of its tests are shape checks (a file exists,
  a script or the CI workflow contains a step), not runs of the workflow.
- `compose/tests/test_spec_hardening.py::test_every_route_of_a_protected_form_page_carries_its_guard`
  asserts on the text of an emitted router file. Its behavioral counterparts boot the emitted code:
  `compose/tests/test_generated_app_runtime.py::test_protected_form_refuses_a_post_without_the_page_guard`
  (real Express) and `test_generated_app_guards_e2e.py::test_protected_form_takes_a_post_from_the_admin_and_nobody_else`
  (a real site on Postgres).
- On the web side, `routes.builds.test.ts` ("the gate is in the statement") asserts the text and
  parameters of `CUSTOMER_APPROVE_SQL`; `money-path.realdb.test.ts` is the test that runs it.

When a change touches behavior, prefer a test that runs the code, and treat a tripwire failure as "read
what changed", not as proof of a bug.

## The main test files

The table lists files that are good places to start; run `ls tests/ compose/tests/` for the rest. A test
file's docstring says what it covers.

| File | What it tests |
|------|---------------|
| `test_worker_pg.py` | The worker with the database mocked: claim, retry and supervisor logic, error classification, mail hooks, plus source-text checks (see above) |
| `test_worker_pg_realdb.py` | The worker's status writes, claim order, preview transition, orphan recovery and supervisor against a real PostgreSQL scratch database |
| `test_worker_lifecycle_realdb.py` | The worker across its seams on a real PostgreSQL: a failed redeploy never touches the announced Railway project, a site that never answers is not previewed, a claim needs a free thread, the janitor keeps what other rules read, the supervisor stops a running fix loop, the resume marker's contract with the web app, an abandoned attempt's thread cleaning up on a connection of its own, a crash whose log cannot be read not being called the app's fault |
| `test_coordinator*.py`, `test_spend_tracker.py`, `test_reconciliation_agent.py` | The coordinator: scoring, budget, tick, work detection, the actuator permission table (`test_coordinator_actuator_gates.py` holds the permission and dry-run matrices, a call-pattern scan and an import scan for capabilities outside `actuators.py`) |
| `test_coordinator_tick_realdb.py`, `test_coordinator_routing.py` | A real tick on the real schema (both spend sources, a statement made to fail on the tick's own connection, the model-call meter, what an agent returns surviving the tick), and the tie between what work detection emits and what the agents accept |
| `test_probe_guard.py`, `test_login_probe_generated_router.py` | The URL guard and `guarded_session` against real loopback listeners (including the backslash bypass), the worker's login probe leaving no usable token in any URL, the monitor loop surviving a database error, and the probe run against the login routes `compose/build.py` really emits |
| `test_executor_loop_behavior.py`, `test_executor_hardening.py` | The roadmap executor `scripts/cas-roadmap-loop.sh`, driving the real loop against a throwaway git repository, a fake agent and a stand-in for the web app's ops API (`OpsApi`, which answers with the shapes the real routes have); includes the merge guard, tiered governance both ways with a stand-in `gh` (`GH_BIN`) and the configured target branch, the pinned test gate, the check that what is merged is what was tested, no push while a build is queued, building, testing or deploying (or when the build counts cannot be read), the time limits on its network calls and the rule that its only commit besides the merge is an item's status change |
| `test_executor_loop_recovery.py`, `test_executor_config.py` | The executor loop killed with SIGTERM and SIGKILL at each step of the merge section and what the next cycle does (keep the merge, take it back, or start nothing while the remote cannot be asked), and the loop giving up on a merge mid-run (a failed status mark or journal update: taken back only on the target branch, only the loop's own merge, never over a person's edit or onto a branch of a person's, and a second item refused while one is unsettled), and the loop crashing; one reader of `enabled` for the loop, the watchdog and `cas-exec`, and a bad config value stopping the cycle with the key named |
| `test_destructive_ddl_corpus.py`, `test_pre_commit_staged.py` | The destructive-DDL and row-change check against 77 evasions (alone, appended to the real schema, spliced into its middle), 5 unreadable inputs, all 212 real statements one at a time, 27 look-alikes and 12 row changes with a restriction, and the 161-case corpus (`web/tests/unit/fixtures/sql-scan-corpus.json`) that the web test's own reader is held to as well, plus the reviewer's mutation (a tagged `DO` block that sets a build live); and the pre-commit hook judging the staged content rather than the working tree, in both directions, through `git commit` |
| `test_bounded_fetch.py`, `test_deploy_browser_bounds.py` | Every outbound probe and provider call is one exchange with a deadline and a cap on the decoded body (a dripping body, a 40 MiB body, a gzip bomb, a redirect chain, a slow TLS handshake, a keep-alive connection not inheriting the last exchange's time), a scan that fails on any other outbound client, and the review browser's page timeouts and 16,000 px screenshot cut |
| `test_coordinator_unknown_is_not_zero.py`, `test_diagnose_untrusted.py` | A cost report or a refresh that cannot be read is unknown spend (`None`), never zero, and every reader is restrictive; the diagnosis prompt fence cannot be re-formed by the log text, and a model reply cannot suppress the notification |
| `test_coordinator_readonly_query.py` | `Actuators.query_readonly` runs exactly one statement, in a read-only transaction: the reviewer's `COMMIT; DELETE ...` probe and variants, semicolons in comments, strings, dollar quotes and E strings that must not be refused, and 400 random texts compared with the real server |
| `test_coordinator_agent_effects.py`, `test_coordinator_model_free_tasks.py`, `test_ops_alert_copy.py` | What became of each alert, finding, KB entry, rebuild and approval request (the five outcomes, the real agents with a real `Actuators`, the tick writing the row on a real Postgres, a tripwire for silent handlers); which task categories can reach a model call, proven against the source, so a tick with no budget still runs the others; and the legacy ops agent's mail claiming no commit |
| `test_postgres_client_version.py`, `test_url_credential_redaction.py` | The worker image's `pg_dump` against the customer databases' major version (a text check of `Dockerfile.worker` and `cli/deploy.py`, and a real run with a stand-in `pg_dump` against a real server; the image itself is built by the `worker-image` CI job, not by a test), and one rule for the credentials of a URL through `redact_url`, `env_without_secrets` and the worker's `_redact_secrets`, with hostile 2 MB inputs and the web app's two expressions |
| `test_worker_manifest_warnings.py`, `test_worker_numeric_settings.py` | The engine's warnings reach the build log whatever the spec put in the manifest; a mistyped worker number falls back to its default instead of stopping the process |
| `test_executor_ops_contract.py` | The loop's assumptions about other components, read as text: the build stages in `queue/worker_pg.py` against the loop's list of active statuses, the per-status counts and the heartbeat fields in `web/src/routes/ops-api.ts`, the `coordinator/**` watch pattern in `railway.toml`, and the dirty-file expression the watchdog shares with the loop. It pins names and shapes, not behavior |
| `test_honest_completion_audit.py` | The Stop hook `scripts/honest_completion_audit.py` (a phrase tripwire), including running it as a process |
| `test_backup_infra.py`, `test_monitor.py`, `test_ops_scanner.py` | Database backup and restore (with real `pg_dump` and `psql`), the health registry, the ops scanner's crash classification |
| `test_deploy_security.py`, `test_deploy_sanitize.py` | `cli/deploy.py`: what reaches a customer service's environment, `sanitize_app_name()`, `redact_url()` |
| `test_security.py` | Structural security properties of the source (see above) |
| `test_release_workflow.py` | Shape checks over the release tooling: scripts, hooks and the CI workflow (this file was `test_enterprise_workflow.py`) |
| `test_validate.py` (v1-only) | The validation rules in `cli/validate.py` |
| `test_keyboard_tab_order_static_scan.py` | The focusable-element counts of the Harbor views against `docs/audits/keyboard-tab-order-2026-05-27.md` |
| `test_web_committed_css.py` | `web/public/style.css` is committed build output (`npm run dev` never builds it): the test runs the same Tailwind build into a scratch file and compares the bytes; it skips without `web/node_modules` |
| `test_no_customer_markers.py` | A structural guard over every published text file (a real-customer tag) and over `compose/fixtures/*.json` (reserved hosts, 555-01xx phones, the state `ST`, postal codes counted up from 00001). It does not look for names |
| `test_network_guard.py` | The test-run network guard itself |
| `../web/tests/unit/utils.json-request.test.ts` | Whether a refusal is JSON or a page (a JSON content type, the `x-csrf-token` header, an API path, `?format=json`), through the real app: malformed JSON and an oversize body on a JSON route, the 404 and error handlers, the token check and the model limiter |
| `../compose/tests/test_build_pipeline.py`, `test_spec_hardening.py` | The v2 spec-to-files pipeline, and hostile spec shapes (reserved names, route guards, navigation) |
| `../compose/tests/test_gate_checks.py`, `test_widget_params.py` | One test for each condition of the spec gate that nothing else pinned, the ASCII-only allowlists, the size bounds (the whole document included) and the types of the values the emitters read; and page data reaching a template only as a parameter it declares, in the shape it declares (real EJS) |
| `../compose/tests/test_build_report.py`, `test_site_health.py`, `_health_check_probe.js` | The build report (the engine's `warning: ` lines first, the spec's notes as at most 40 `spec-warning: ` lines, no spec text able to start a line of either kind), including the worker's real lint on a real build; and the emitted `services/health.js` run under node against a stand-in home page |
| `../compose/tests/test_spec_shape_matrix.py`, `_spec_matrix.py`, `_site_check.py`, `_site_harness.js` | The wrong-type matrix described under "Run": every wrong JSON value at every position of ten sample specs is refused or builds a site whose every route renders; a second matrix does the same to v1 specs through `translate_v1_spec` |
| `../compose/tests/test_engine_columns.py`, `test_stored_values.py`, `_scratch_pg.py` | The columns the engine reads with a fixed meaning (`id`, `created_at`, `published_at`, the link to a parent, the newsletter email: a gate grid per column and kind, and proofs on a real PostgreSQL such as a text `created_at` failing the dashboard), the order tables are created in (a child declared before its parent applies; a ring is refused), and every seed value and default put to Postgres to see whether it is stored |
| `../compose/tests/test_generated_app_runtime.py`, `test_generated_app_guards_e2e.py` | Emitted route and auth code run under real Express, and a generated site booted on Postgres |
| `../compose/tests/test_generated_site_publication_e2e.py`, `test_publication.py` | What a visitor to a generated site can read (no model's rows are public because a page has its name; unpublished visitor rows stay hidden) and what its public forms store and reject; the first boots a site on Postgres, the second reads the emitted source |
| `../compose/tests/test_translator_output_builds.py`, `_translate_extracted_spec.js` | The web translator's real output built by the engine: `harbor_to_compose.ts` is compiled in node and its output goes through `translate_v1_spec`, `validate_spec`, `resolve_spec` and `compose_files`, so a contradiction the engine refuses and the translator emits fails here; skips without node or `web/node_modules/typescript` |
| `../compose/tests/test_emitted_css_js_syntax.py` | Every stylesheet and script a build emits parses: braces and PostCSS for the CSS, `node --check` for the JavaScript, compiled inline `<script>` blocks, over every fixture, archetype and template family and layout variant |
| `../compose/tests/test_generated_site_security_e2e.py`, `test_model_derived_emission.py` | Two generated sites booted on Postgres (no login token in the log, role guards, CSRF on public and admin writes, cookie flags, session fixation, drafts, uploads, the CSP nonce), and the emitted contact form, listing order and protected-page refusals that come from the model |
