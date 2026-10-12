# cli/: the v1 build pipeline (legacy, frozen)

The first build engine. It turns a validated JSON app spec into a deployed
Express + EJS + PostgreSQL app in two broad steps:

1. **LLM generation.** `build.py` prompts Claude to write the app files from
   the spec (multi-pass for larger specs), seeded with static `templates/`.
2. **Deterministic post-processors.** 117 `@processor` functions in
   `build.py`, ordered as a DAG by `pipeline.py`, repair and normalize the
   model output (routes, views, auth and CSRF wiring, seed data, design).
   Most exist because a real build failed in a specific way.

Around that core: `intake.py` (chat to spec), `validate.py` (spec schema),
`review.py` and `design_review.py` (model-based code and design review),
`qa.py` and `qa_runner.py` (model-written Playwright checks, run in a child
process), the `fix.py`, `qa_fix.py`, `design_fix.py` and `deploy_fix.py` repair
loops, `test.py` and `ui_test.py` (smoke and UI tests against a locally started
copy of the app), `deploy.py` (Railway), `failure_kb.py` with
`failure_kb.json` and `review_kb.json` (learned fixes fed back into prompts),
`widgets/` and `contracts/` (schema-driven admin forms and declarative output
checks), `api_client.py` (Anthropic calls: retry, batch mode, cost metrics),
`cancellation.py`, `build_trace.py`, `design_data.py` (palettes, fonts, UX
rules), `visual_diff.py` and `railway_cleanup.py`. `stock_photos/` holds generated placeholder images
(see its README).

## Status

Legacy and frozen: do not extend it. The v2 engine in `../compose/` replaced
it as the default; the worker runs v1 only when `CAS_BUILD_ENGINE=v1` is set.
It is kept because the move from v1 (the model writes the app, code repairs
it) to v2 (deterministic composition, no model in the build path) is the main
engineering story here, and because its tests document real failure modes.
Those tests are skipped by default; run them with
`CAS_BUILD_ENGINE=v1 pytest tests/` (see `../conftest.py`).

Not everything in this directory is v1-only. These modules are imported by the
worker, coordinator or scripts and are not frozen with the engine:
`deploy.py` (Railway helpers used for both engines), `api_client.py`,
`cancellation.py`, `failure_kb.py`, `build_trace.py`, `pipeline.py` and
`visual_diff.py` (used by dev scripts), and the `stock_photos/` data (read by
`compose/stock_photos.py`). The header of `__init__.py` lists them with their
importers.

More: `../docs/internals/pipeline.md` (per-module detail) and
`../docs/internals/post-processors.md` (post-processor catalog).

## Measured facts

Each number can be reproduced with the command beside it, run from the repo
root. They describe the repository as published.

| What | Value | Command |
|---|---|---|
| Registered post-processors | 117. All 117 `@processor` decorations are in `build.py` (no other file in `cli/` has one). All 117 run in the `build` phase; 60 of them also run in the `reapply` phase. | `grep -c '^@processor' cli/build.py`; at runtime `python3 -c "import sys; sys.path.insert(0,'cli'); import build, pipeline; print(len(pipeline.REGISTRY), len(pipeline.pipeline_order('build')), len(pipeline.pipeline_order('reapply')))"` prints `117 117 60` |
| v1-only test functions | 1,522 test functions (1,562 test items once parametrized cases are counted), in 63 files under `tests/` | `CAS_BUILD_ENGINE=v1 python3 -m pytest tests/ --collect-only -q -m v1_only \| tail -1` prints `1562/<total> tests collected`; distinct functions: add `\| grep :: \| sed 's/\[.*//' \| sort -u \| wc -l` before the `tail` |
| Python source in `cli/` | 30,136 physical lines in 39 files (23,029 once blank lines and comment-only lines are dropped) | `find cli -name '*.py' -not -path '*/__pycache__/*' \| xargs cat \| wc -l` |
| Template source in `cli/templates/` and `cli/widgets/` | 2,015 lines in 22 `.js`, `.css`, `.ejs` and `.tmpl` files | `find cli \( -name '*.js' -o -name '*.css' -o -name '*.ejs' -o -name '*.tmpl' \) \| xargs cat \| wc -l` |
| `build.py` | 15,673 lines, about 682 KiB, in one file | `wc -l cli/build.py; wc -c cli/build.py` |

The line counts cover source files only: the 5 `.json` files
(`failure_kb.json`, `review_kb.json`, `cas_qa_kb.json`, `video_pools.json`,
`stock_photos/manifest.json`), the images (`stock_photos/`, `assets/`) and the
`.md` files are not counted. A test is "v1-only" when its file has
`pytestmark = pytest.mark.v1_only`; the root `conftest.py` lists the same files
in `_V1_ONLY_FILE_PREFIXES` and skips collecting them unless
`CAS_BUILD_ENGINE=v1` is set.

## Running the v1 tests

```
CAS_BUILD_ENGINE=v1 python3 -m pytest tests/ -q
```

All of it runs offline. Every skip and expected failure carries a reason
string: tests that need a past build directory (`builds/...`) or the private
gold-standard reference are skipped as "not part of this repository", and
`test_v2_local_e2e.py` is opt-in (`-m v2_integration`, needs local Postgres,
node and an npm registry). A guard in the root `conftest.py` makes any
Python-level attempt to reach a non-loopback host fail with
`NetworkBlockedError`; set `CAS_TESTS_ALLOW_NETWORK=1` for a deliberate live run.

## Security notes

- **Login shortcut for build-time tests.** The test runner (`test.py`) logs in
  over HTTP with a fixed test password, so the pipeline puts a shortcut in every
  magic-link app's `POST /login`: with `TEST_MODE` set, the password
  `test_password_for_ci` logs in as any existing user. The shortcut ships in the
  app's source, so it is switched off by construction in a deployed app: its
  condition also requires `NODE_ENV !== 'production'`, and `server.js` gets a
  startup guard that exits when `TEST_MODE` is set in production. Both are
  enforced by the post-processor `_ensure_magic_link_auth` (not left to the
  prompt), and `deploy.py` sets `NODE_ENV=production` and never sets `TEST_MODE`.
  Before this was enforced, the production guard existed only as an instruction
  to the model. Tests: `tests/test_security_sec1_test_mode_gate.py`. The v2
  engine emits no such shortcut.
- **Seeded credentials.** Apps built before 2026-04-19 shipped a seed
  password hash that was identical across customers; since then
  `_scrub_bcrypt_from_seed` always strips bcrypt hashes from `db/seed.sql` and
  seeded users have a NULL password (magic-link login only).
  `tests/test_security_sec1_seed_password_hash.py` pins this (parts of it read a
  private reference build and are skipped here).
- **Build-time child processes.** `npm install` and `node server.js` for the
  app under test no longer inherit the worker's environment: `test.py` uses
  `deploy.env_without_secrets()`, which drops API keys, tokens, passwords,
  `RAILWAY_*`, `ANTHROPIC_*`, `RESEND_*` and the worker's `DATABASE_URL` (the
  app is then given a `DATABASE_URL` for its own throwaway test database). The
  model-written QA tests run the same way, in a child process (`qa_runner.py`,
  started by `qa._run_tests_in_child`) with that scrubbed environment and a
  wall-clock timeout. `psql` on the generated `db/schema.sql` and `db/seed.sql`
  gets the scrubbed environment plus the `PG*` connection variables
  (`test._pg_env`), and the Railway CLI gets it plus the project token
  (`deploy.deploy_source`).
- **Failure knowledge base.** `failure_kb.json` and `review_kb.json` are only
  written through `failure_kb._atomic_write_json`, and every call site passes
  redacted data (checked by an AST test). The committed seeds are redaction
  fixed points. Redaction covers API-key shapes, bearer tokens, JWTs, private
  key blocks, passwords inside URLs and secret-bearing query parameters. It is
  pattern based: a secret with no recognizable shape would not be caught.

## Known limits

- **Model-written code is not sandboxed.** The Python that the model writes
  for `qa.py` no longer runs inside the worker process: `qa._run_tests_in_child`
  starts `qa_runner.py` as a child with the scrubbed environment, so the worker's
  secrets are not in its environment or address space. The child still runs as
  the same user in the same container with the same filesystem and network, so
  code that goes looking (for example `/proc/<worker pid>/environ`) can still
  find them. The model is steered by the customer's spec, so this remains a code
  execution path from customer text to the worker's host. The generated app
  under test also receives credentials for the worker's Postgres server (it
  needs them for its throwaway test database). A dedicated container or a
  dedicated database role would close both; v1 was frozen first.
- **Mail from generated apps needs its own key.** `deploy.py` no longer copies
  the platform's `RESEND_API_KEY` into customer apps
  (`deploy._generated_app_mail_env`). An app gets a mail key only when the
  operator sets `GENERATED_APP_RESEND_API_KEY`, and then every generated app
  shares that one key, so it should be a separate send-only key. With the
  variable unset an app sends no mail: it writes a requested login link to its
  own server log and skips form notifications.
- **The `db/index.js` template disables TLS certificate verification** for the
  database connection in production (`rejectUnauthorized: false`).
- **Post-processors edit source text with regular expressions.** Ordering
  between them is hand-declared (`after=` edges) and checked for drift at import,
  but correctness rests on golden hashes and example-based tests, not on parsing
  the generated JavaScript or EJS.
- **Leftover password probes.** `deploy.verify_auth`, `verify_data` and
  `verify_routes` post `password123` to a live app's `/login` (failures are
  downgraded to warnings in magic-link mode), and `capture_deploy_screenshots`
  tries the build-time test password when magic-link mode is off. They cannot
  succeed against apps built since 2026-04-19, whose seeded users have no
  password; they are a leftover from the seeded-password era and misleading to
  read.
- **Data quality.** `video_pools.json` categorizations are approximate and at
  least one id is dead; the placeholder stock photos are gradients, not photos.
- **Test-only gaps.** A few v1 tests need a past build directory or a private
  reference build that is not published, so those checks do not run here.

### The worst code in `cli/`

The post-processor layer in `build.py`: 117 passes that rewrite model-written
JavaScript and EJS with regular expressions and string splicing, six of them
250 to 440 lines long (`_ensure_video_embeds` is 436 lines in one function),
some of which repair the output of earlier passes. It works because each one was
added after a real failure and has a test, and it is hard to reason about for
the same reason: no single place says what a finished app must look like except
the golden hashes and contract checks bolted on at the end. Second worst is
`test.py::run_tests`, a 782-line function holding about twenty inline checks
with hand-maintained counters.

## Plan section labels

Comments and test docstrings in v1 code refer to the B+C rewrite by labels
such as `section-02-foundation`, `Section-04` or `Section 06`. Those labels
come from the original split-file layout of the plan; in the surviving
`../docs/plans/b-plus-c/claude-plan.md` the same material is numbered one lower.

| Label | Content | Section in `claude-plan.md` |
|---|---|---|
| section-01 | fixtures | file layout |
| section-02 | foundation (`pipeline.py` DAG, validator extension) | 1 |
| section-03 | widget registry (`widgets/`) | 2 |
| section-04 | widget emission (`widgets/forms.py`) | 3 |
| section-05 | contract framework (`contracts/`) | 4 |
| section-06 | prompt pruning and safety net | 5 |
| section-07 | golden-file blessing | 6 |

Section labels elsewhere (`api_client.py`, `Section-06 of WATCHDOG1 Layer 2`,
the coordinator tests) belong to other plans, not to this table.

## Third-party material

- `design_data.py`: palettes, font pairings, product recommendations and UX
  rules extracted from the UI UX Pro Max skill ("UUPM"),
  https://github.com/nextlevelbuilder/ui-ux-pro-max-skill, MIT license.
- `templates/icon_helper.js`: icon artwork from Lucide, https://lucide.dev,
  ISC license (Feather-derived icons: MIT).
- `templates/lite-youtube.js` and `.css`: an independent, much smaller
  reimplementation of the pattern of
  https://github.com/paulirish/lite-youtube-embed (Apache-2.0); the play-button
  icon path follows upstream's.
- `video_pools.json`: ids of public YouTube videos; the videos are not ours.
