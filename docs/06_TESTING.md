# Testing Strategy

## Overview

This document covers the web app's tests, which live in `web/tests/`. The Python suites (`tests/`, `compose/tests/`) are described in the Testing section of `CLAUDE.md` and in `tests/README.md`. Unit tests run locally and in GitHub Actions. UI (Playwright) tests run in CI after unit tests pass.

---

## Running Tests

```bash
cd web

npm test               # unit tests (run before every push; no database needed)
npm run test:watch     # unit tests in watch mode (dev)
npm run test:coverage  # unit tests + coverage report -> web/coverage/
npm run test:ui        # Playwright UI/E2E tests (needs running server + DB)
```

---

## Unit Tests (Vitest)

Location: `web/tests/unit/`. The table lists the original core files, not the full set; run `ls web/tests/unit/` for everything.

| File | What it covers |
|---|---|
| `auth.middleware.test.ts` | HMAC CSRF token generation, verification, tamper detection, `validateCsrfToken`, the cross-origin write guard, `resolveSessionSecret`, `isAdminSession` |
| `claude.service.test.ts` | `processIntakeMessage`, `generateInitialGreeting`, spec parsing, clean reply on spec complete |
| `email.service.test.ts` | `generateUnsubscribeToken` determinism, `emailShell` unsubscribe link, `sendEmail` skip/send/fail paths, token redaction in `email_log` |
| `routes.auth.test.ts` | Signup, login, logout: valid/invalid inputs, duplicate email, wrong password |
| `routes.builds.test.ts` | Dashboard auth guard, build status page, `/api/builds/:id` JSON endpoint |
| `routes.intake.test.ts` | Start intake, load intake page, send message, spec saved on complete, confirm tier |
| `routes.password-reset.test.ts` | `GET/POST /forgot-password`, `GET/POST /reset-password`, `GET /unsubscribe`; token validation, expiry, used_at |
| `routes.webhooks.test.ts` | `POST /webhooks/build-status` (auth, the guarded status transitions, the refusal of `live`, the review verdict cleared when a build lands at preview, the preview-ready and build-failed emails), the Stripe webhook (the claim and the event's work in one transaction, the make-public checkout that takes a reviewed preview live, subscription lifecycle events, ordering and unknown subscriptions) and the Resend webhook signature check |
| `routes.builds.test.ts` ("the gate is in the statement") | Asserts the text and parameters of `CUSTOMER_APPROVE_SQL` and `ADMIN_APPROVE_SQL`, the statements that take a build live, so removing the review or subscription condition fails a test |
| `routes.lighthouse.paid-keeper.test.ts` | The browser that paid for a build (its trial cookie) as the build's keeper: lamp, published and conversation pages, cancel; and what a stranger holding the same URL cannot do |
| `routes.lighthouse.crisis-first.test.ts`, `app.crisis-first.test.ts` | A crisis message on `POST /lighthouse/table/turn` is answered before the cookie lookup, the limiters, the email gate and the cost caps, and with the database down or hanging (the second file runs the real `app.ts` with the session store down, to show that `crisisFirst` is mounted ahead of the session middleware) |
| `routes.auth.unverified-claim.test.ts` | The rule for unverified accounts in `routes/auth.ts`: signup against an address someone else already claimed, verification from a browser that did or did not type the password, reset verifying the account |
| `app.boot-security.test.ts`, `db.tls.test.ts`, `utils.listen.test.ts`, `playwright.config.test.ts` | The preview-password gate (`isPublicStaticAsset`), the database TLS decision (`poolSslOption`), the listen/`EADDRINUSE` handling, and the Playwright port and server-reuse rules |
| `money-path.realdb.test.ts`, `crisis-phrasing.realdb.test.ts`, `schema.idempotency.test.ts` | The only unit-directory files that run SQL: see "What runs real SQL" below |
| `services.site-copy-crawl.test.ts`, `.reference.test.ts`, `.linear-time.test.ts`, `.time-limits.test.ts`, `.parse-worker.test.ts` | The faithful-copy crawler: behavior of the extractors, the old regular expressions kept as an oracle for the single-pass scanners (`site_copy_parse.ts`), linear running time through 2 MiB, the per-request and whole-crawl deadlines, and the parse worker's hard kill |
| `views.marketing-accuracy.test.ts`, `dead-code.guard.test.ts` | The seven marketing views make no claim that a person builds or reviews every site, and style no class they do not use; no file under `src/cron/` is unreferenced and no exported `send*` helper in `email.ts` lacks a caller |
| `services.harbor-dispatch.test.ts` | The shared Harbor dispatch service (`insertHarborBuild`, `dispatchHarborKeeperBuild`): `is_harbor` set at insert, one build per conversation (`handoff_at`), the keeper path's guards, and the customer's build-start limits (`build_limit`, refused before any model call) |
| `app.page-views.test.ts`, `llm-budget.anonymous-share.test.ts`, `harbor-cron-scheduler.test.ts`, `routes.anon-intake.test.ts` | The page-view caps (30 a minute per address, 600 overall), the anonymous share of the model-call budget (80%), the sweeps' claim in `platform_metadata`, and the retired `POST /try/...` routes answering 404 under Harbor |
| `companion/memory_owner.test.ts` | Whose notes a request reads (`listMemoryNotes`, `memoryOwnerColumn`) |

Framework: **Vitest** with `@vitest/coverage-v8`. Config: `web/vitest.config.ts`. The unit suite never reads `web/.env`: the config's setup file `web/tests/unit/setup-no-dotenv.ts` replaces `dotenv` with a no-op, because the quickstart tells a reader to create `web/.env` (with `NODE_ENV=development` in it) and `src/app.ts` and `src/db/index.ts` load it at import time, which used to make tests that mean "`NODE_ENV` is unset" run as development. Each test sets the environment it needs.

### What runs real SQL

The web unit tests replace the database with query mocks, so a mocked test proves which statement a route sends and what it does with the rows it is handed, not what Postgres does with the statement. SQL is executed only by (1) the Playwright suite, (2) `schema.idempotency.test.ts`, whose static part checks `schema.sql` in every run and whose integration part applies the file twice to a real database, and (3) `money-path.realdb.test.ts`, which loads `schema.sql` into a database, boots the real app on a real pool and drives the real publish, payment, signup, requeue and crisis routes against rows in each state, and also the conversation's notes through payment, the build-start limits on a dispatch, the address a payment is tied to and the weekly sweeps' claim (the Python counterpart for the worker is `tests/test_worker_pg_realdb.py`), and (4) `crisis-phrasing.realdb.test.ts`, which boots the real app the same way, puts a trial at the email gate, sends it the plain crisis disclosures a narrower scan let through (one request each) and reads back the row stored for the operator, and checks that a first visit stores the path it arrived on and not the query. The three Vitest files run only when `CAS_SCHEMA_TEST_DATABASE_URL` points at a throwaway, empty database and are skipped otherwise. Run them serially, because each applies `schema.sql`:

```bash
createdb cas_web_integration
cd web
CAS_SCHEMA_TEST_DATABASE_URL=postgresql://localhost:5432/cas_web_integration \
  npx vitest run --no-file-parallelism tests/unit/schema.idempotency.test.ts tests/unit/money-path.realdb.test.ts tests/unit/crisis-phrasing.realdb.test.ts
```

`money-path.realdb.test.ts` leaves its rows behind under a per-run suffix, `crisis-phrasing.realdb.test.ts` leaves its trials and turns behind, and the idempotency test inserts a fixed email, so use a fresh database for each run. The `ui` job in CI does the same on its own database.

One test in `money-path.realdb.test.ts` crosses from TypeScript into Python: it runs the worker's real janitor, `_cleanup_old_builds` in `queue/worker_pg.py`, in a child process on the same database, between the web app's statements ("the Harbor rule survives everything that deletes builds"). The janitor deletes failed builds that a payment rule once read the ancestry of, and a test that replayed its SQL text from TypeScript would keep passing after the worker changed. The price is a Python that can import the worker: set `CAS_WORKER_PYTHON` to an interpreter with `requirements.worker.txt` installed (CI does, after `actions/setup-python`), and the test must run and fails if the import does; leave it unset and the test tries `python3` and, if that cannot import the worker, skips and prints why. The admin and ops deletion variants beside it need no Python and always run. The file also refuses any non-loopback HTTP, HTTPS or `fetch` while it runs, so a missing mock cannot reach a payment provider. Three single functions are stood in for, each in one test: the creation of the Checkout session, the model call inside spec extraction and the model client's `create` method.

Mocking approach:
- DB pool mocked via `vi.mock('../db')`: default returns `{ rows: [] }`, overridden per test with `mockResolvedValueOnce`
- Anthropic SDK mocked via `vi.hoisted` + `vi.mock('@anthropic-ai/sdk')`
- Express app imported from `src/app.ts` (no port binding, no migration) for supertest

---

## UI Tests (Playwright)

Location: `web/tests/ui/`. The table lists the original core specs; run `ls web/tests/ui/` for everything.

| File | What it covers |
|---|---|
| `auth.spec.ts` | Full signup flow, login, wrong-password error, logout, admin email log unsubscribe link in iframe |
| `intake.spec.ts` | Greeting visible in viewport, send message, AI response, focus retention, Shift+Enter newline |
| `password-reset.spec.ts` | Forgot-password link navigation, empty form, unknown email neutral response, bad token error |
| `browser-behavior.spec.ts` | Behavior only a browser can show after the CSP change: the delegated click handlers on the status page and dashboard, the Harbor price slider and the amount it submits, the cancel-subscription confirmation, no console or CSP errors on the public pages, and login, logout and forgot-password (no reset link on a page); the Stripe-bound POSTs are stubbed in the browser; needs the seeded fixtures |

Requires:
- A built server (`npm run build`); `web/playwright.config.ts` starts `node dist/server.js` itself on `PORT` (default 4000, the same value the specs use through `web/tests/ui/port.ts`) and stops with a message if something already answers there, because the suite would otherwise test that process. Pick another port with `PORT=4412 npm run test:ui`, or set `CAS_UI_REUSE_SERVER=1` to use a server you started yourself (ignored when `CI` is set)
- Chromium for Playwright: `npx playwright install chromium` (CI uses `--with-deps`); the package does not download a browser itself
- A real PostgreSQL DB (local; CI uses a service container seeded by `node tests/ui/seed-test-db.js`)
- Env vars:
  - `DATABASE_URL`: the seeded database (`browser-behavior.spec.ts` re-applies its fixture rows through it)
  - `DEV_TEST_EMAIL`, `DEV_TEST_PASSWORD`: pre-seeded account for login/intake tests
  - `ADMIN_TEST_EMAIL`, `ADMIN_TEST_PASSWORD`: admin account for email log tests
  - `CAS_HARBOR_ENABLED=true`, and `CAS_A11Y_DEV_TRIAL` (any truthy value) with `CAS_A11Y_LAMP_BUILD_ID` (the id of the lamp fixture build, a preview and approved one, that `seed-test-db.js` prints), as set in the `ui` job of `.github/workflows/test.yml`, which is the reference recipe. Without those two the accessibility sweep specs skip: a run with only the first group of variables reports fewer passes and more skips than the CI run (38 passed and 15 skipped, against 45 and 8, when this was measured)
  - Further variables switch on the keyboard-tab-order and screen-reader specs (`CAS_KB_DEV_TRIAL`, `CAS_KB_LAMP_BUILD_ID`, `CAS_SR_DEV_TRIAL`) and the password-reset token spec (`VALID_RESET_TOKEN`); CI sets none of them (nor `ADMIN_TEST_EMAIL` and `ADMIN_TEST_PASSWORD`), so those specs skip there

Specs whose prerequisites are missing skip with a reason naming what to set. The suite runs one project, desktop Chromium (`web/playwright.config.ts`); no mobile viewport and no other browser is configured.

Config: `web/playwright.config.ts`.

---

## Automation

### Pre-push hook (local)
Tests run automatically before every `git push`. Push is aborted if any test fails.

Installed via: `git config core.hooksPath .githooks` (run once per clone, or `make hooks`; `make verify-hooks` checks the wiring).
Hook file: `.githooks/pre-push`. It runs the web unit tests once through `scripts/run_web_tests.sh` (which tells a fresh clone to run `cd web && npm ci` first), after the staging check, the golden-file bless gate and the dependency audits.

Do not bypass the hook with `--no-verify`: `CLAUDE.md` forbids it, and the `Hook Integrity` workflow runs `scripts/verify_hooks.sh` server-side, which runs both hooks in a throwaway repository. The one documented bypass is `CAS_SKIP_AUDIT=1`, which skips only the dependency audits, not the tests.

### GitHub Actions CI
Workflow: `.github/workflows/test.yml`

Triggers on pushes to the default branch, on every pull request, and on manual dispatch. It needs no repository secrets. The seven jobs:
1. **unit**: installs deps, builds TypeScript, runs `npm test`
2. **python**: installs the Python requirements and, because the v2 engine's widget tests render EJS from `web/node_modules`, Node and `cd web && npm ci`; composes the sample generated site and runs `npm install` in `builds/sample-healer` (the five generated-site modules in `compose/tests` skip without it); then runs three steps against a Postgres service container: `pytest tests/` (default mode), `pytest compose/tests/`, and `pytest tests/` again with `CAS_BUILD_ENGINE=v1` for the legacy engine's tests
3. **quickstart**: starts the repo's `docker-compose.yml` Postgres, copies `web/.env.example` to `web/.env`, builds and starts the server, and checks `/health` and the home page
4. **worker-image**: builds `Dockerfile.worker` (it downloads the Playwright base image and the PostgreSQL apt repository, so it needs Docker and the network and takes a few minutes), checks that the image's `pg_dump` major version is at least `POSTGRES_SERVER_MAJOR` in `cli/deploy.py`, then starts a `postgres:<that major>` container with a password generated for the run and dumps its schema with the image's `pg_dump`, printing the last three lines. It had not run on GitHub when the release was prepared
5. **secrets**: downloads a gitleaks release pinned by version, verifies its SHA-256, and scans the full git history with `.gitleaks.toml`
6. **audit** (advisory, `continue-on-error`): the npm and Python dependency audits that the pre-push hook runs as blocking checks
7. **ui**: spins up a Postgres service container, installs Playwright, builds, installs Python 3.12 and `requirements.worker.txt` (so the one test that runs the worker's cleanup can import the worker; `CAS_WORKER_PYTHON: python`), runs the three real-Postgres Vitest files (`schema.idempotency.test.ts`, `money-path.realdb.test.ts`, `crisis-phrasing.realdb.test.ts`) against a separate database, seeds the test database, runs `npm run test:ui`, uploads the Playwright report artifact

The UI job only runs after the unit job passes. `.github/workflows/hook-integrity.yml` separately verifies the git hooks and the destructive-DDL guard.

---

## Python Tests

`python3 -m pytest tests/ compose/tests -q` from the repo root, on Python 3.12 (what CI uses; see `tests/README.md` for why 3.14 cannot install the requirements, how each kind of skip is lifted, and which tests are source-text tripwires rather than behavior tests). Rules enforced by the root `conftest.py`:

- **It takes a while.** `pytest tests/ compose/tests` takes about 15 minutes on a developer machine (measured on one Mac), because `test_executor_loop_behavior.py` and `test_executor_loop_recovery.py` run the real executor loop in bash against throwaway repositories; CI runs them once, in the default pass. The files added in the last fix round are listed in `tests/README.md`: bounded probes (`test_bounded_fetch.py`), unknown spend (`test_coordinator_unknown_is_not_zero.py`), the destructive-DDL corpus (`test_destructive_ddl_corpus.py`), the staged-content pre-commit hook (`test_pre_commit_staged.py`), executor recovery and configuration (`test_executor_loop_recovery.py`, `test_executor_config.py`), and the manifest and numeric-setting fixes of the worker.
- **No network.** Only loopback is reachable during a test run; any other DNS lookup or `connect` raises `NetworkBlockedError`, and the blocked attempts are listed at the end of the run. Mock the network in a test; `CAS_TESTS_ALLOW_NETWORK=1` turns the guard off for a deliberate live run. The guard is installed for the whole life of the process and is never lifted at the end of the session, because a test can leave a thread running past the last test (a build thread blocked in a model call, for example) and a guard removed at that point would let the thread reach the network. It does not cover child processes (curl, git, npm) or libpq, so tests that spawn those must stay local.
- **v1-only tests are skipped by default.** Files that exercise the legacy v1 pipeline (`pytestmark = pytest.mark.v1_only`, listed in `_V1_ONLY_FILE_PREFIXES`) run only with `CAS_BUILD_ENGINE=v1`. A plain `pytest tests/` therefore runs the v2-relevant set.
- `tests/test_v2_local_e2e.py` is opt-in (`-m v2_integration`) and needs local Postgres, node and the npm registry (for its one `npm install`). It composes every sample spec, boots it and follows the real login flow: a GET of `/magic?token=` shows the confirmation page and spends nothing, and the POST to `/magic/consume` logs in.
- `SENTRY_DSN` is removed from the environment before the run, and `CAS_LINT_EXTERNAL_URLS` defaults to `skip`.
- **Scratch databases.** Tests that need Postgres use the `scratch_pg_url` fixture in `tests/conftest.py`: an empty `cas_test_scratch_<random>` database is created on a local server (`DATABASE_URL` is honored only when it points at this machine) and dropped when the session ends. They skip when no local server answers. `tests/test_worker_pg_realdb.py` runs the worker's SQL against one, and so do `tests/test_worker_lifecycle_realdb.py` (the claim, the janitor, the Railway-project rules, cancellation and the resume marker, driving the real `run_worker` and `_process_job`) and `tests/test_coordinator_tick_realdb.py` (a real coordinator tick on the real schema).
- **Missing tools skip, they do not fail.** A test that shells out to `git` or `node` carries the `requires_tools` marker and skips, naming the program, when it is not on `PATH`. The widget and layout tests use `web/node_modules/ejs` and skip until `cd web && npm ci` has run. The five modules in `compose/tests` that boot a generated site (`test_compose_end_to_end.py`, `test_smoke_suite.py`, `test_generated_app_guards_e2e.py`, `test_generated_site_security_e2e.py`, `test_generated_site_publication_e2e.py`) also need `builds/sample-healer/node_modules`, a local Postgres and `node`, and skip with the exact commands to run when they are missing.
- A session fixture sends `tempfile` leftovers to a directory under pytest's own base temp.

---

## Adding New Tests

When you add a new route or service:

1. Add a corresponding unit test file in `web/tests/unit/`
2. Mock the DB at the top: `vi.mock('../db', () => ({ pool: { query: vi.fn() } }))`
3. Use `supertest(app)` for route tests
4. Keep each test isolated: reset mocks with `beforeEach(() => vi.clearAllMocks())`

When you add a new user-facing page or flow:
1. Add a Playwright spec in `web/tests/ui/`
2. Use `page.goto('/path')` and assert with `expect(page).toHaveURL(...)` or `expect(page.locator(...)).toBeVisible()`

---

## Coverage Targets

These are targets, not measured results; the README reports the measured web coverage.

| Layer | Target |
|---|---|
| Middleware | 100% |
| Route handlers | 90%+ |
| Services (claude, email, queue) | 80%+ |
| UI flows | All happy paths + key error states |
