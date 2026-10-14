# Contributing

This is an archived repository. The system it describes was wound down in July 2026 and nothing here is under active development. An issue or a pull request may not be reviewed quickly, or at all, and a repository set to archived on GitHub accepts neither. For anything larger than a fix, forking is the faster route.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first; it says where each piece lives and which files are the hard ones.

## Prerequisites

- Node 24 and Python 3.12, the versions CI runs ([test.yml](.github/workflows/test.yml)). Use Python 3.12 for the test requirements: the pinned `playwright` package does not install on Python 3.14. The worker image installs Node 20 ([Dockerfile.worker](Dockerfile.worker)).
- PostgreSQL. [docker-compose.yml](docker-compose.yml) starts Postgres 16 on `127.0.0.1:5432` with the credentials that [web/.env.example](web/.env.example) expects; CI uses Postgres 15 for the Python and UI jobs.
- Git and a POSIX shell; the hooks are `sh` and `bash` scripts.

## Setup

```bash
docker compose up -d --wait
cp web/.env.example web/.env
cd web && npm ci
npm run dev        # http://localhost:4000, schema applied at boot
```

Without Docker, start your own Postgres and set `DATABASE_URL` in the copied env file; an empty database is enough. With no mail key set, the development server prints each verification and reset link to its console (`[dev] Email is not configured. ...`) and logs the email as `skipped`; open the printed verification link, in the browser you signed up in, to confirm a new account and be logged in (see "Running the web app locally" in [CLAUDE.md](CLAUDE.md)). Chat needs your own `ANTHROPIC_API_KEY`; without one the chat routes answer 503 with a message saying so. `npm run build && npm start` runs the compiled server, as the CI quickstart job does.

## Running tests

```bash
# web unit tests (Vitest; the database is mocked, and the suite never reads web/.env)
cd web && npm test

# web tests that run real SQL: a throwaway empty database, run serially
createdb craftandship_integration
cd web && CAS_SCHEMA_TEST_DATABASE_URL=postgresql://localhost:5432/craftandship_integration \
  CAS_WORKER_PYTHON=<python with requirements.worker.txt installed> \
  npx vitest run --no-file-parallelism tests/unit/schema.idempotency.test.ts tests/unit/money-path.realdb.test.ts tests/unit/crisis-phrasing.realdb.test.ts

# Python: worker, coordinator, monitor, v2 engine
# (the v2 widget tests render EJS from web/node_modules: run `cd web && npm ci` first or they skip)
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.worker.txt -r requirements-dev.txt
python3 -m pytest tests/ compose/tests -q

# legacy v1 engine tests, skipped unless this variable is set
CAS_BUILD_ENGINE=v1 python3 -m pytest tests/ -q
```

The Python suite blocks non-loopback network access ([conftest.py](conftest.py)) from start to exit and never lifts the block; set `CAS_TESTS_ALLOW_NETWORK=1` only for a deliberate live run. The Python tests that need Postgres create and drop their own scratch database on a local server (the `scratch_pg_url` fixture in [tests/conftest.py](tests/conftest.py)) and skip when none answers; `npm test` needs no database. The three web files that run real SQL need `CAS_SCHEMA_TEST_DATABASE_URL` (they skip without it), and one test in `money-path.realdb.test.ts` additionally runs the Python worker's cleanup, so it needs `CAS_WORKER_PYTHON` or a `python3` that can import `queue/worker_pg.py`, and otherwise skips and prints why. Five modules in `compose/tests` boot a generated site and skip until `builds/sample-healer/node_modules` exists (the commands are in [tests/README.md](tests/README.md)). The deploy tests mock the Railway API and the post-deploy probes. The Playwright UI tests (`npm run test:ui` in `web/`) need Chromium (`cd web && npx playwright install chromium`), a built server (`npm run build`), Postgres, a seeded test user (`node tests/ui/seed-test-db.js`) and several environment variables, including the two fixture variables `CAS_A11Y_DEV_TRIAL` and `CAS_A11Y_LAMP_BUILD_ID` without which some specs skip; [docs/06_TESTING.md](docs/06_TESTING.md) lists them and the `ui` job in [test.yml](.github/workflows/test.yml) is the reference recipe. The suite starts its own server on `PORT` (default 4000) and refuses to run if something already answers there, unless `CAS_UI_REUSE_SERVER=1` is set. That workflow has seven jobs (`unit`, `python`, `quickstart`, `worker-image`, `secrets`, `audit`, `ui`); `audit` is advisory, `secrets` runs gitleaks over the full history and `worker-image` builds `Dockerfile.worker` and checks its `pg_dump` against a `postgres:15` server (it needs Docker and the network). Run a single file while iterating, for example `python3 -m pytest tests/test_security.py -q` or `cd web && npx vitest run tests/unit/auth.middleware.test.ts`.

The [Makefile](Makefile) targets are `help`, `hooks`, `verify-hooks`, `test` (runs `pytest tests/` only, not `compose/tests`), `test-web`, and `bless-golden SPEC=<name>`, which regenerates a golden fixture; the resulting commit message must start with `bless:`.

## Git hooks

Run `make hooks` once per clone (it sets `core.hooksPath` to `.githooks`) and `make verify-hooks` to check the wiring. `.githooks/` holds two hooks, `pre-commit` and `pre-push`, and `verify-hooks` fails if it holds anything else; it also runs both hooks in a throwaway repository, so a hook that is an empty stub, or has a gate switched off, fails.

- **pre-commit** ([.githooks/pre-commit](.githooks/pre-commit)): judges the staged content, not the working tree, and rejects destructive DDL and row changes in `schema.sql` (read with a tokenizer, so a statement inside a `DO` block counts: `DROP TABLE`, `DROP COLUMN`, `ALTER COLUMN ... TYPE`, `TRUNCATE`, `DELETE` with no `WHERE` or with one that names no column) without a `-- SAFE: <reason>` comment above it (or above the `DO`); because the file runs at every boot, any `UPDATE`, `INSERT`, `DELETE`, `MERGE`, `COPY ... FROM` or dynamic `EXECUTE` anywhere in it needs a reason of its own, on its line or directly above it, not one above the `DO`, and a statement that names the status `live`, or a default of `live`, is refused whatever the comment (the comment satisfies this hook only: the web unit tests that the pre-push hook runs still refuse a top-level `DROP TABLE`, `DROP COLUMN`, `DROP CONSTRAINT`, `TRUNCATE`, `DELETE`, `UPDATE` or `INSERT` in `schema.sql` and keep the inventory of the nine row changes it may hold), rejects invalid JSON in the failure knowledge base, lints customer-facing files for the brand-voice rules (em dashes, competitor names, technical-ownership copy), and runs the SEO consistency lint when SEO files are staged.
- **pre-push** ([.githooks/pre-push](.githooks/pre-push)): checks the staging workflow (a no-op unless `use_staging_workflow` is true in `scripts/cas-executor-config.json`, which ships false), blocks a push that contains commits touching `tests/golden/` unless their messages start with `bless:`, runs the npm and Python dependency audits (`CAS_SKIP_AUDIT=1` skips these two), and runs the web unit tests. The audits need network access; bandit and pip-audit are skipped with a warning if not installed.

Do not bypass the hooks with `--no-verify`; [hook-integrity.yml](.github/workflows/hook-integrity.yml) runs `verify_hooks.sh` on the server. `pytest tests/ compose/tests` takes about 15 minutes, because it runs the real executor loop in bash against throwaway repositories.

## Code conventions

From "Code style" in [CLAUDE.md](CLAUDE.md):

- No formatter is configured. The static checks are `cd web && npx tsc --noEmit` (strict mode), `ruff check .` (Python correctness rules only; [ruff.toml](ruff.toml) lists them, and CI runs it) and `python3 -m compileall`; `bandit` runs in the pre-push hook and the CI audit job.
- TypeScript in strict mode; `async` and `await` only; explicit types rather than `any`.
- Parameterized SQL only. Never build a query by string concatenation.
- Catch and log errors; do not swallow them.
- Calls to external APIs retry with exponential backoff.
- Descriptive names; functions under 50 lines. The largest existing functions do not meet this (see [ARCHITECTURE.md](ARCHITECTURE.md#where-the-bodies-are-buried)), so new code should not copy them.
- No em dashes in code, comments, logs or copy; use a comma, colon, semicolon or a new sentence.
- Customer-facing copy follows [docs/BRAND_VOICE.md](docs/BRAND_VOICE.md).
- Schema changes are idempotent additions to `web/src/db/schema.sql`.
- Add or update a test with every behavior change.

## Security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
