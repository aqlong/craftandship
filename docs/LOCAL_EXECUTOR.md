# CAS Local Roadmap Executor

> Historical document (April to May 2026); the system is no longer developed and the site is still online. The executor scripts remain in `scripts/` as a reference; nothing here is running.

## Overview

The Local Roadmap Executor is an autonomous agent loop that works through items in `coordinator/roadmap.json`. It runs as a persistent loop on the operator's machine (macOS launchd), starts a headless Claude Code session per item in a git worktree, runs the web and Python tests, and, if they pass, merges the branch into the target branch of the checkout it runs in and pushes.

**It ships disabled.** `scripts/cas-executor-config.json` has `"enabled": false`, and a config that has no `enabled` key at all also means disabled. Only a JSON `true` enables it: the loop, the watchdog and `cas-exec status` all ask one reader, `executor_enabled` in `scripts/cas-pick-item.py`, so the string `"True"` enables none of them, and a half-edited config cannot turn it on. Read the script before enabling it: the Claude session runs with whatever permissions your Claude Code settings grant (the loop passes no `--permission-mode` or tool list), and the worktree is a working directory, not a sandbox: the session can write anywhere your user can, including the checkout the loop runs in, the `web/node_modules` the test gate links to, the state directory and the remote. The test gate needs `python3` 3.11 or newer and pytest 8.4 or newer (it uses `python3 -P` and `--disable-plugin-autoload`) and fails closed without them.

An enabled executor also needs the web app's cost report to be complete: it starts a session only when `GET /ops/api/v1/costs` says `platform_costs_partial` is false, and the report is always flagged partial when the web app has no `ANTHROPIC_ADMIN_KEY`, so without that key on the web app an enabled executor starts nothing.

It complements the production Coordinator (which handles ops: requeue builds, send emails, queue items for senior-engineer review) by handling code-level work (edit files, run tests, git push).

## Quick Start

```bash
# Install the launchd agent first (see the comment block in
# scripts/com.cas.roadmap-executor.plist.template), then:

scripts/cas-exec resume     # sets "enabled": true and clears pause_until
scripts/cas-exec start      # load the launchd agent (stop unloads it)

scripts/cas-exec status     # config, loop running or not, active worktrees
scripts/cas-exec fast       # interval 10 min
scripts/cas-exec normal     # interval 20 min (the shipped config uses 120)
scripts/cas-exec slow       # interval 60 min
scripts/cas-exec parallel 2 # max_parallel
scripts/cas-exec pause      # sets "enabled": false
scripts/cas-exec pause-until 2026-04-02T23:00:00+00:00
scripts/cas-exec run-once   # one cycle now (RUN_ONCE=1), then exit
scripts/cas-exec logs       # tail the latest log (logs [item-id] for one item)
scripts/cas-exec queue      # eligible roadmap items
scripts/cas-exec failures   # failure tracker with counts
scripts/cas-exec deferred   # deferred items with reasons
scripts/cas-exec config     # print the config
scripts/cas-exec stop
```

## How It Works

### Loop Cycle (every `interval_minutes`)

0. **Settle an interrupted merge.** At the top of every cycle, before the config is read, `settle_interrupted_merge` reads the journal `logs/executor/merge-in-progress`, which the merge section writes before the stash, before the merge and before the push. Nothing merged: the stash is popped. A pop that conflicts keeps the stash and prints `stash pop conflicted; stash kept as <ref>`. The remote has the merge: it is kept. The remote lacks it: it is taken back with `git reset --keep`, the stash is popped and the item is worked again. The remote cannot be asked, or a person has committed on top of the merge, or the checkout is on another branch, or taking the merge back would overwrite a person's edit: nothing changes, no session starts, and each heartbeat says why. The same routine runs on the way out of a signal (without the network), and the same take-back (`take_back_merge`) serves the two places where a run gives up on its own merge, a status mark that cannot be committed and a journal that cannot be updated for the push.
1. **Crash-loop check and lock.** `check_crash_loop`, then a PID lockfile (`cas-executor.lock` in the state dir; see Safety Rails), then cleanup of stale worktrees and a zombie sweep of `executor_runs` rows.
2. **Read config** from `scripts/cas-executor-config.json` (re-read every cycle; no restart needed).
3. **Post a heartbeat** to `/ops/api/v1/executor/heartbeat` (best effort; visible on `/admin/executor`).
4. **Pre-flight gates** (`preflight_check`), skip the cycle if any fails:
   - Config not `enabled`, or `pause_until` is in the future.
   - The project directory is not on `target_branch` (the executor yields; it never checks out another branch underneath a human).
   - `coordinator/roadmap.json` differs from HEAD (a status the loop recorded is already committed, so a difference is somebody else's edit, or a session's): the cycle yields until a person commits or discards it.
    - Any path is unmerged (`git ls-files -u`): a stash pop that conflicted, or a merge a person left unfinished. The cycle yields, and nothing is charged to an item.
   - Any other tracked file has uncommitted changes, except `cli/failure_kb.json`, `scripts/cas-executor-config.json` and `CLAUDE.md` (`DIRTY_OK_RE`, which the watchdog shares; it matches the end of each path, so a CLAUDE.md under web/ is tolerated too): the cycle yields. The loop never commits those three. Untracked files do not count.
   - `max_items_per_day` reached. The counter counts Claude sessions started today, not merges.
   - The build check cannot be answered or is not whole numbers (`active_build_count` accepts only whole numbers; `0.9` and `false` are not read as zero). The loop asks `GET /ops/api/v1/builds` for the per-status counts it returns under `stats` (`active_build_count`). This gate FAILS CLOSED: with no answer within the time limit, an HTTP error or JSON without the counts, no session is started.
   - `daily_spend_ceiling_usd` reached, read from `/ops/api/v1/costs`. This gate FAILS CLOSED too (`spend_verdict`): only a JSON object (at most 8 MiB) whose `platform_costs_partial` is exactly false and whose `today_total` is a finite, non-negative number under a usable ceiling starts a session. A partial report, a missing, string, boolean, NaN or negative total, or a NaN ceiling is not taken for zero spend. The figure is platform spend, not the executor's own.
5. **Pick items** (`cas-pick-item.py --next-n`): highest-priority eligible pending items, at most `max_parallel` and at most the sessions left in today's budget.
6. **Worktree isolation.** `git worktree add -b auto/<item-id> .worktrees/auto-<item-id> <commit>`, where `<commit>` is the one `origin/<target_branch>` points at, recorded by hash (`base_sha`) before the session runs. The executor links `web/node_modules` into the worktree so tests can run there.
7. **Execute.** `claude -p "<prompt>" --max-budget-usd <max_budget_per_item_usd>`, with a 20-minute kill timer.
8. **Change check.** Only what is committed on `auto/<item-id>` is merged, so before anything is tested the loop requires the worktree to be on that branch, to hold no uncommitted or untracked files (the `web/node_modules` link it made is the one expected entry), and the branch tip to differ from, and descend from, `base_sha`. A session that did nothing, left a file uncommitted, or rebuilt the branch on an older commit is rejected with `"No committed change"`: the failure is recorded and the item stays pending. The branch tip is read once here (`tested_sha`); that one commit is what the rest of the cycle guards, tests and merges.
9. **Merge guard, first pass** (below), on the diff of `tested_sha` against `base_sha`, before the tests, so that the test commands that run are the target branch's and not ones the session rewrote. A branch that touches a protected path, deletes a test file, uses a path that is not plain ASCII, or adds a symlink or a submodule link is rejected with `"Protected file"`.
10. **Test.** On a fresh detached checkout of `tested_sha` at `.worktrees/gate-<item-id>` (removed afterwards), not in the session's worktree: `npm install` and `npm test` in `web/`, then, after every `__pycache__`, `.pyc` and `.pyo` is removed from the checkout (`gate_remove_bytecode`, which fails closed if any is left), `PYTHONDONTWRITEBYTECODE=1 python3 -P -B --check-hash-based-pycs always -m pytest -c <file written by the loop> --rootdir --confcutdir --disable-plugin-autoload -p no:cacheprovider tests compose/tests -q` with `PYTHONPATH`, `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` unset. Each of the gate's three commands has a time limit (`CAS_EXECUTOR_GATE_TIMEOUT_SEC`, default 1800 seconds). The checkout holds the commit's files and nothing the session left around them, `-P` keeps a top-level `pytest.py` from shadowing the runner, and the configuration file is the target branch's `pytest.ini` (or an empty one). Removing the bytecode and `--check-hash-based-pycs always` keep compiled Python the commit carries from running in place of its source. Playwright UI tests are not part of this gate (`web/tests/ui` is protected from deletion and not run).
11. **If tests fail:** discard the worktree and branch, record the failure, move on.
12. **If tests pass:**
    - With `tiered_governance: true`, a diff that `scripts/classify_risk.py` rates high-risk is pushed (the tested commit, by hash) as `auto/<item-id>` and opened as a draft PR (`gh pr create --draft`, the path of `gh` being `GH_BIN`) instead of merging; the item becomes `in_review`. The guard has already run, so a branch that touches a protected path is not pushed as a PR branch either. The classifier reads the diff with `--no-renames`, so moving a high-risk file does not hide it. The shipped config has tiered governance off.
    - Otherwise: take the merge lock, wait for builds to clear (`wait_for_builds_clear`, see Safety Rails: up to 45 minutes, and nothing is pushed if the ops API will not report the counts), `git pull --ff-only` the target branch, run the merge guard a second time, refuse the run if `auto/<item-id>` no longer points at `tested_sha` (the branch moved after its tests ran), then `git merge --no-ff <tested_sha>`, mark the item `completed` in `coordinator/roadmap.json` as one commit of exactly that change (`record_roadmap_status`, below), and `git push` (with `CAS_EXECUTOR=1` so the pre-push staging block lets it through; the rest of the pre-push hook still runs). The push sends every commit on the target branch that the remote lacks.
    - If the completed mark cannot be committed as that single change, the merge is taken back with `git reset --keep` (`take_back_merge`: only while the checkout is on the target branch, only a merge the loop can show it made, and never over a person's edit; the loop's own half-written status mark is put back first) and nothing is pushed. If it cannot be taken back, the merge, its journal and the stash stay, the dashboard gets the result "Merge not taken back", and the next cycle yields until it can settle them; a second item that reaches the merge step meanwhile is refused ("Merge unsettled"). If the push does not succeed, the loop asks the remote: only after the remote has said it lacks the merge is the target branch taken back to its pre-merge commit the same way; if the remote has it, the run continues; if it cannot be asked, the merge stays and the cycles yield until it can.
13. **Courtesy deploy check:** poll the web `/health` for up to 2 minutes for the new commit. Non-blocking; there is no revert if it never appears.
14. **Report result:** POST to `/ops/api/v1/executor/result` (visible on `/admin/executor`).
15. **Sleep** `interval_minutes`, then repeat.

### Branch Workflow

```
master ───────●──────────────────────●── master (after merge)
               \                    /
                ● auto/<item-id> ──●   (work here, delete after)
```

- The session never edits the target branch directly; work happens on a branch in a worktree. The loop itself commits on the target branch only the merge and an item's status change.
- Merges use `git merge --no-ff`, so every executor change lands as a merge commit.
- **Status commits.** `record_roadmap_status` is the only commit the loop makes besides the merge. It writes nothing when the checkout is off the target branch or `coordinator/roadmap.json` already differs from HEAD. After the picker has written the status, the picker compares the file with HEAD's copy (`cas-pick-item.py --verify-transition`: that one item, its status and at most one note field, nothing else), and the commit names that one path and goes through the pre-commit hook (the loop no longer passes `--no-verify`). If the file is not exactly that change, nothing is committed, a copy goes to `logs/executor/`, and preflight yields until a person looks at it. The loop does not commit a session's edits to the roadmap, the executor config or the failure KB, which a session can still make in this checkout.
- The branch is deleted after the merge or on failure; at every start and exit `cleanup_worktrees` deletes any local `auto/*` branch that no worktree has checked out, whoever made it.
- **Merge guard.** The loop lists the diff with `git diff -z --raw --no-renames --ignore-submodules=none <base>...<tested commit>` (`guard_report` in `scripts/cas-roadmap-loop.sh`) and hands it to `scripts/cas-merge-guard.py`, run as `LC_ALL=C python3 -I`. The rules, and the protected set (`PROTECTED_PATHS`), are in that helper, not in the loop. It refuses: any added, modified, deleted, type-changed or renamed path in the protected set (any `CLAUDE.md` or `CLAUDE.local.md`, `.claude/`, `.mcp.json`; `coordinator/roadmap.json`; everything under `scripts/` (this loop, the guard, the picker, the prompt, the config, the risk classifier, and every script the git hooks, the CI workflows and the Claude Code hooks run); `.githooks/`, `.github/` and `.gitleaks.toml`; what `npm install` and `npm test` read or run before a test (any `package.json`, lockfile or shrinkwrap, any `.npmrc`, anything under a `node_modules`); what Vitest evaluates before a test file (the `web/vite*` and `web/vitest*` config, the PostCSS configuration, the setup file `web/tests/unit/setup-no-dotenv.ts`); the Tailwind and Playwright configuration (`tests/test_web_committed_css.py` runs Tailwind inside the gate, which evaluates `web/tailwind.config.js`, and `npm run build` evaluates it at deploy); what is built into the deployed images and how they are built and started (every Dockerfile, `.dockerignore`, `railway.toml`, `web/railway.toml`, nixpacks configuration, every `requirements*.txt`); what a person runs after following the README (the `Makefile`, any compose file and its overrides); the schema file the web app applies at every boot and the module that applies it (`web/src/db/schema.sql`, `web/src/db/migrate.ts`); every `conftest.py` and pytest configuration file; what pytest imports before it collects a test (top-level Python modules, the `__init__.py` of top-level packages such as `tests/`, top-level `*.dist-info` and `*.egg-info`); compiled Python at any depth (anything under a `__pycache__` directory, any `.pyc`, `.pyo`, `.pyd` or `.so`); and the files a version-manager shim reads to choose the interpreter); the deletion or rename of any file under a directory named `tests`, `test` or `__tests__` (a branch may add and edit tests, not remove one; `tests/test_executor_loop_behavior.py` walks the repository and fails when a test file lives somewhere the rule does not cover); a path that is not valid UTF-8 or has a byte outside printable ASCII (on APFS, which folds case and Unicode normalization, a name spelled with U+212A for the k is the same file as `.githooks/`, so every such path is refused and reported with what it folds to); and a path the branch leaves as a symlink (mode 120000) or a submodule link (160000; `--ignore-submodules=none` is spelled out because `diff.ignoreSubmodules` can hide one). Paths are compared as bytes, ASCII letters without regard to case, and nothing depends on the locale. A listing the helper cannot read (cut short, an unknown status, over 16 MiB, the helper missing or failing) exits 2 and the loop treats that as a refusal. The target branch is resolved with full refs (`refs/heads/T`, `refs/remotes/origin/T`, fetched with an explicit refspec), so a tag named like the branch changes nothing. A rejected run is reported as `"Protected file"` and nothing is merged. The guard runs twice: before the tests, and again just before the merge on what the merge brings into the local target branch (a script another clone pushed since the first pass is seen only by the second). `scripts/cas-roadmap-loop.sh --guard-check` runs only the path rules on a name-status diff read from stdin. The point is that an unattended agent cannot loosen the limits, hooks, CI or test gate that constrain the next run, and that what is merged is what was tested: the guard and the gate read one commit, the tests run on a fresh checkout of it rather than in the session's worktree, and the merge is of that commit by hash. What it cannot do: it reads the commit's diff and sees nothing a session did outside git (the session is not sandboxed and can write outside its worktree), a branch can still weaken a test, or code a test imports, that it is allowed to edit, and a test file or imported module that exits the process with status 0 still passes the gate.

### Items that wait for a human

`cas-pick-item.py` skips items whose status is `completed`, `deferred`, `waiting_for_answer` or `in_review`, and items marked `executor_eligible: false`. A person adds items to the roadmap: no coordinator code writes `coordinator/roadmap.json`, and a finding promoted at `/admin/findings` reaches the roadmap only when a person adds an item for it. An item held back that way can carry an `executor_skip_reason` saying why; an item whose id is not a plain slug is never eligible, because the id becomes a branch name and a worktree path. The loop itself writes only `completed`, `in_review` (draft PR path), `pending` (a failed PR push, with a `status_drift_note`) and `deferred` (auto-defer after `max_total_failures_before_defer` total failures, with a `deferred_reason`), each through `record_roadmap_status`. The web app has endpoints and an admin panel for executor questions (`POST /ops/api/v1/executor/question`, the Questions section of `/admin/executor`), but the loop in this repository never posts a question or checks for an answer, so `waiting_for_answer` is a status a person sets, not a flow the loop drives.

## Configuration

`scripts/cas-executor-config.json` (shipped values; the fallback used when a key is missing from the file is the same value except `enabled`, which falls back to disabled):

| Field | Type | Shipped | Description |
|-------|------|---------|-------------|
| `enabled` | boolean | `false` | Master kill switch; missing means disabled |
| `interval_minutes` | number | `120` | Minutes between cycles |
| `max_parallel` | number | `1` | Items run at once |
| `max_items_per_day` | number | `3` | Claude sessions started per day (failed attempts count) |
| `max_budget_per_item_usd` | number | `3` | Passed to `claude --max-budget-usd` |
| `daily_spend_ceiling_usd` | number | `10` | Stop if the ops API reports more spend today (fails closed: no session is started when the spend cannot be read) |
| `pause_until` | string/null | `null` | ISO timestamp to pause until |
| `max_total_failures_before_defer` | number | `4` | Failures across all days before an item is auto-deferred |
| `skip_complex_items` | boolean | `true` | Skip items `cas-pick-item.py` rates complex |
| `target_branch` | string | `master` | Branch the loop merges into and pushes |
| `use_staging_workflow` | boolean | `false` | Read by `.githooks/pre-push`, not by the loop; when true, direct pushes to master are blocked unless `CAS_PROMOTE=1` or `CAS_EXECUTOR=1` |
| `tiered_governance` | boolean | `false` | Send high-risk diffs to a draft PR instead of merging; on for any value but JSON false |

Modify with `cas-exec` commands or edit directly. Changes take effect on the next loop iteration. A `pause_until` that is not a time counts as a pause, and a value of the wrong kind (a string where a number belongs, NaN) stops the cycle with the key named.

## Safety Rails

| Rail | Implementation |
|------|----------------|
| No concurrent runs | PID lockfile `cas-executor.lock` in the state dir (`CAS_EXECUTOR_STATE_DIR`, else `$TMPDIR`, else `/tmp`). A lock is created by hard link to a file that already holds the process id, and a stale one is removed only under a `mkdir` guard after the owner is read again (the watchdog does the same); an owner is identified by process id alone, so an unrelated process given a dead owner's id makes a lock look held until it exits |
| No git races | A git-setup lock serializes worktree creation; a merge lock serializes merges (forced after 70 minutes) |
| Failure-aware dispatch | Items with 3 failures today, or `max_total_failures_before_defer` in total, are skipped or deferred; the tracker is `cas-executor-failures` in the state dir (7-day retention) |
| Don't conflict with a human | The cycle yields when the project directory is off the target branch, when `coordinator/roadmap.json` differs from HEAD, or when a tracked file outside `cli/failure_kb.json`, `scripts/cas-executor-config.json` and `CLAUDE.md` has uncommitted changes, or when any path is unmerged. There is no process check for a running interactive session. |
| Don't break the target branch | Branch isolation, tests on a fresh checkout of the branch's commit before the merge, a `git pull --ff-only` before merging (a failure of it is still ignored, and the second guard pass then judges against the local branch), a check that the branch still is the tested commit, and a take-back (`git reset --keep`, only on the target branch, only of the loop's own merge) once the remote has said it lacks the merge. An interrupted merge is settled from the journal at the next cycle. Only the pre-push hook's web unit tests run on the merged result; the Python suite is not re-run after the merge. |
| Don't merge control files | The change check and the merge guard above (a committed, clean worktree that descends from the commit it was created from; protected paths, deleted tests, non-ASCII paths, symlinks and submodule links), applied before the tests and again before the merge |
| Merge what was tested | One commit (`tested_sha`) is guarded, tested and merged by hash; a branch that moved after its tests is refused ("Branch moved after tests") |
| Don't push during builds | Before merging it reads `GET /ops/api/v1/builds` (the per-status counts under `stats`; `/ops/api/v1/stats` reports queued and building only) and waits up to 45 minutes for no build to be `queued`, `building`, `testing` or `deploying` (`ACTIVE_BUILD_STATUSES`). It FAILS CLOSED: if the counts cannot be read in 3 attempts, nothing is pushed and the item is not counted as a failure. Every executor push redeploys the worker, because the push carries the item's `coordinator/roadmap.json` commit and `coordinator/**` is in the worker's `watchPatterns` (`railway.toml`). The check is a snapshot: a build claimed after the last zero count is still cut off |
| Cost control | `--max-budget-usd` per item, `max_items_per_day`, and the daily ceiling (which fails closed) |
| Bounded network calls | `CAS_OPS_TIMEOUT_SEC` per ops API request (default 10), `CAS_EXECUTOR_NET_TIMEOUT_SEC` for `git fetch` and `git pull`, `gh pr create` and the gate's `npm install` (300), `CAS_EXECUTOR_PUSH_TIMEOUT_SEC` for a push, which includes the pre-push hook (1800), `CAS_EXECUTOR_GATE_TIMEOUT_SEC` for each of the gate's `npm install`, `npm test` and `pytest` runs (1800). Every timed job is a process group: a job that runs over, or a signal, ends the group and its process tree (SIGKILL after 2 seconds), and the jobs are recorded so the next start ends what a killed loop left. A process that has both left the group and been re-parented is not reached. The per-item log file has no size limit. |
| Its own commits only | Besides the merge, the loop commits one thing: an item's status change (`record_roadmap_status`). It never commits `cli/failure_kb.json`, `scripts/cas-executor-config.json` or `CLAUDE.md`, and not a roadmap file that is anything but HEAD plus that change. |
| Timeout | A 20-minute timer drops a marker file and ends the Claude session's process group and tree; the loop reads the marker afterwards to tell a timeout from other failures (a signal cannot be used when items run as parallel background jobs) |
| Failed deploy | Not handled: the post-push deploy check is a 2-minute courtesy poll with no revert |
| Audit trail | All runs logged locally under `logs/executor/` and posted to the ops API |

## Self-Healing Watchdog (added 2026-05-28)

`scripts/cas-executor-watchdog.sh` runs every 30 min (2x/hour) via
`com.cas.executor-watchdog.plist.template` (rendered into `~/Library/LaunchAgents/`) and auto-fixes the mechanical blocks that
stall the executor and that its own preflight cannot self-recover from. It is
deterministic shell (no AI) because every block it catches is mechanical (a
stale lock, a dead PID, an orphan worktree, a crashed-run leftover), and a
shell script has no API dependency (an AI guard would die when the Claude CLI
auth is down, which is one of the blocks the watchdog must detect). The checks
for Claude auth, stalls and a down executor only run while the config has
`"enabled": true`; the lock, worktree and branch checks always run.

**Auto-fixes (unambiguously safe):**
- Stale .git/index.lock (>120s old + no live git process); this exact bug
  burned 7+ items on 2026-05-28 before the fix
- Stale `cas-executor-merge.lock` in the state dir (dead PID)
- Stale `cas-executor.lock` in the state dir (dead PID + loop not running)
- Orphan `.worktrees/auto-*` and `.worktrees/gate-*` (>2h old and no process mentions its path)
- Executor process down → `launchctl kickstart`

**Alerts only (ambiguous: could be active session work, never auto-touched):**
- PROJECT_DIR on a non-master branch
- A dirty tree that makes the executor yield (a tracked change other than in `cli/failure_kb.json`, `scripts/cas-executor-config.json` and `CLAUDE.md`, `coordinator/roadmap.json` included) persisting across 2 watchdog runs (~30+ min)
- Unmerged paths in the checkout (a conflicted stash pop or an unfinished merge); the watchdog reports them and resolves nothing
- Claude CLI auth failure (401): needs `claude /login`, cannot auto-fix (the probe uses `CLAUDE_BIN` and is cut off after `CAS_WATCHDOG_PROBE_TIMEOUT_SEC`, 30 seconds, and 4 KiB; the production health read is cut off after 10 seconds and 64 KiB)
- Executor log silent >90 min WHILE eligible items exist (catch-all for novel stalls)
- Prod web >15 commits behind origin/master; executor pushes not deploying

```bash
scripts/cas-executor-watchdog.sh            # live: auto-fix + alert
scripts/cas-executor-watchdog.sh --dry-run  # report only, change nothing
tail -f logs/executor/watchdog.log          # watch it run
launchctl list | grep executor-watchdog     # confirm loaded
```

A run is live only when it is started with no argument at all and without a `DRY_RUN` in its environment other than 0. Any other argument, a mistyped `--dry-run` included, makes it a dry run, and it says so ([test_executor_watchdog_modes.py](../tests/test_executor_watchdog_modes.py)).

The launchd template is `scripts/com.cas.executor-watchdog.plist.template`; the live copy is rendered to `~/Library/LaunchAgents/com.cas.executor-watchdog.plist`. Always exits 0
(a watchdog must never fail-loop) and posts a `status: watchdog` heartbeat to
the ops API so the admin /executor dashboard shows it ran.

## Monitoring

### Admin Page (/admin/executor)
- Live status banner with heartbeat
- Run history with status, duration, cost, commit
- Pending questions with answer UI
- Auto-refreshes every 30 seconds

### Local Logs
- Per-run: `logs/executor/YYYY-MM-DD-HHMMSS-<item-id>.log`
- Latest: `logs/executor/latest.log` (symlink)
- Tail live: `cas-exec logs`

## Troubleshooting

### Executor keeps skipping
Check which pre-flight gate is triggering:
```bash
cas-exec run-once  # Watch output for the heartbeat "skipped_reason"
```

Common causes:
- Config disabled or paused (`cas-exec status`)
- The project directory is on a feature branch, or has uncommitted tracked changes (commit or stash them; `coordinator/roadmap.json` counts)
- Daily limit reached (adjust in config)
- Daily spend ceiling reached
- The ops API is not answering (`CAS_OPS_BASE_URL`, `OPS_API_KEY`): the heartbeat reason says the build counts or the spend could not be read
- No eligible items (`cas-exec queue`)

### An item shows `waiting_for_answer`
Nothing in the loop sets or clears this status. Set it back to pending with `python3 scripts/cas-pick-item.py --set-status <item-id> pending`, or edit `coordinator/roadmap.json`.

### Orphaned branches
```bash
git branch | grep "auto/" | xargs git branch -D
```

### Costs too high
Adjust in config:
```bash
cas-exec config                          # Check current limits
scripts/cas-exec slow                    # Longer intervals = fewer items
# Or edit scripts/cas-executor-config.json directly:
# max_budget_per_item_usd, daily_spend_ceiling_usd, max_items_per_day
```

## Cost Model

| Metric | Estimate |
|--------|----------|
| Per item | Bounded by `max_budget_per_item_usd` (the Claude session reads code, makes changes, runs tests) |
| Per day | Bounded by `daily_spend_ceiling_usd` and `max_items_per_day` |

The `--max-budget-usd` flag caps each Claude Code invocation. `max_items_per_day` bounds the number of sessions. The daily ceiling is a third limit; it reads spend from the ops API and, when that cannot be read, no session is started.

## Files

| File | Purpose |
|------|---------|
| `scripts/cas-roadmap-loop.sh` | Main persistent loop |
| `scripts/cas-merge-guard.py` | The merge guard's rules (protected paths, tests that may not be deleted, ASCII paths, symlinks and submodule links) |
| `scripts/cas-pick-item.py` | Item selector + prompt builder; `executor_enabled`, the one reader of `enabled` |
| `scripts/cas-executor-prompt.md` | Claude Code prompt template |
| `scripts/cas-executor-config.json` | Runtime configuration |
| `scripts/cas-exec` | Convenience CLI |
| `scripts/com.cas.roadmap-executor.plist.template` | Canonical launchd plist (copy to `~/Library/LaunchAgents/`) |
| `~/Library/LaunchAgents/com.cas.roadmap-executor.plist` | macOS auto-start (installed from template above) |
| `scripts/cas-executor-watchdog.sh`, `scripts/com.cas.executor-watchdog.plist.template` | Self-healing sweep and its launchd agent |
| `logs/executor/` | Execution logs (gitignored) |
| `logs/executor/crash.log` | Crash-surfacing log: one line per unclean exit (epoch-prefixed for awk filtering) |

## Crash surfacing (added 2026-04-23)

The loop runs under `set -euo pipefail` with launchd `KeepAlive = true`. Historically a single `set -u` unbound-variable bug would crash the iteration AND silently respawn forever; a `t_start` unbound on 2026-04-23 caused 35 respawns in 46 min, 72 zombie `executor_runs` rows, and the configured `interval_minutes` never applied because the loop never reached the sleep line.

The architectural fix (three layers, all in `cas-roadmap-loop.sh`):

1. **`on_exit` EXIT trap + `CLEAN_EXIT` sentinel**; every unclean exit writes a timestamped line to `logs/executor/crash.log` and POSTs a `crashed` heartbeat. Critical quirk: `set -u` errors do NOT fire ERR trap and exit with rc=0, which is why this uses an EXIT trap + boolean sentinel (default 0 = crash-until-proven-otherwise) rather than `$?`-inspection. Only TERM/INT signals (launchctl unload) and the RUN_ONCE break set `CLEAN_EXIT=1`.
2. **`check_crash_loop` on startup**: counts crash.log entries in the last 900s. If ≥3, writes a `CRASH-LOOP DETECTED` marker to crash.log and sleeps 900s in-process so the loop doesn't pile on over a broken code path.
3. **`sweep_zombie_runs` on startup**: POSTs `/ops/api/v1/executor/sweep-zombies` to mark any rows left stuck in `running`/`working`/`pr_created` (older than 60 min) as `failed`. Idempotent and best-effort.

Diagnosis flow when the loop appears wrong:

```bash
tail -20 logs/executor/crash.log              # recent crashes
grep -c "" logs/executor/crash.log            # total entries (all-time)
grep CRASH-LOOP logs/executor/crash.log       # when it's self-dampened
grep -c "Executor started" logs/executor/launchd-stdout.log  # respawn count
```

After fixing the root cause, clear crash.log so `check_crash_loop` starts fresh: `: > logs/executor/crash.log`.
