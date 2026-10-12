#!/bin/bash
# cas-roadmap-loop.sh: the autonomous roadmap executor. Every interval it picks
# the next eligible item from coordinator/roadmap.json (scripts/cas-pick-item.py),
# runs a headless Claude Code session on it in a git worktree on branch
# auto/<item-id>, runs the web and Python test suites on a fresh checkout of the
# commit the session left on that branch, and, if they pass, merges that commit
# into the target branch of THIS checkout and pushes.
# It ships DISABLED ("enabled": false in scripts/cas-executor-config.json).
#
# Gates, in order: an interrupted merge of an earlier run settled (see
# MERGE_JOURNAL); config enabled (only a JSON true enables) and not paused (a
# pause_until that cannot be read as a time is a pause), with every value of
# the kind it should be; project dir on the target
# branch with no uncommitted change to roadmap.json and no unrelated uncommitted
# changes; max_items_per_day Claude sessions started today; the ops API answers
# the build check, and today's spend, read from the same API, is under the
# daily ceiling (both FAIL CLOSED: no session is started when either cannot be
# read, when a count is not a whole number, when the cost report is partial or
# its total is not a plausible amount); per-item caps (one --max-budget-usd
# session, a 20 minute kill timer, at most 3 failures a day and
# max_total_failures_before_defer overall); the session left a committed change
# and a clean worktree, on a branch that still descends from the commit its
# worktree was created from; the merge guard (scripts/cas-merge-guard.py:
# nothing under scripts/, .githooks/, .github/ or .claude/, no CLAUDE.md,
# roadmap.json, package manifest, npm configuration, test-runner configuration
# or setup file, Tailwind or Playwright configuration, top-level Python module
# or anything under a node_modules, no Dockerfile, .dockerignore, Railway or
# nixpacks configuration, requirements file, Makefile or compose file, not
# web/src/db/schema.sql or migrate.ts, no deleted file under a directory named
# tests, no path that is not plain ASCII, and no symlink or submodule link in
# the branch's diff), applied before the
# tests are run and again just before the merge; the tests pass (run_test_gate:
# npm test, and pytest on tests/ and compose/tests);
# the ops API reports no build queued or in progress (wait_for_builds_clear,
# FAILS CLOSED); the branch is still the commit that was tested; a
# non-conflicting merge of that commit; the item's status change committed as
# exactly that change (record_roadmap_status); a push that the pre-push hook
# accepts.
# With "tiered_governance" on (anything but a JSON false, or no key), changes
# classified high-risk by
# scripts/classify_risk.py open a draft PR instead of merging. The guard has
# already run by then, so a branch that touches a protected path is not pushed
# as a PR branch either.
#
# One commit goes through all of it. The branch tip is read once, when the
# session has exited (tested_sha). The guard diffs that commit against the
# commit the worktree was created from (base_sha, recorded before the session
# ran, not a ref the session can move); the tests run on a fresh checkout of
# it, not in the session's worktree; and the merge, or the draft PR push, is of
# that object by its hash. If the branch has moved by merge time the run is
# refused. Wherever a branch is meant it is named in full (refs/heads/...,
# refs/remotes/origin/...): a short name finds a tag of that name first.
#
# Being stopped. SIGTERM and SIGINT end the loop on purpose: it ends its
# jobs (each is a process group), settles an interrupted merge section when
# that needs no network, releases its lock and removes its worktrees. A kill
# that cannot be caught, or a power cut, leaves all of that to the next start,
# which ends the jobs the dead loop left running, removes its worktrees and
# stale locks, and settles the merge section from the journal before anything
# else: the stash is popped, and an unpushed merge is taken back unless the
# remote turns out to have it. Every take-back, here and where a merge is given
# up on in the middle of a run, goes through take_back_merge: only on the branch
# the merge was made on, only a merge the loop can show it made, and with
# `git reset --keep`, so an edit of a person's is kept or stops the take-back and
# is never discarded (the script has no `reset --hard`). What the next start
# cannot settle (a person's
# commits on top of the interrupted merge, a remote that does not answer) it
# leaves in place and says so in every heartbeat, and starts no session.
#
# Limits worth knowing: the Claude session runs with whatever permissions your
# Claude Code settings grant (no --permission-mode or tool list is passed here),
# and it is not sandboxed: it can write anywhere your user can, including this
# checkout, its web/node_modules (which the gate's checkout links to), the
# state directory and the remote. The guard and the gate constrain what a branch can
# get merged through this loop; they do not contain the session. The gate runs
# the tested commit's own tests, and a branch may add or edit tests and the
# code they import, so nothing here stops a session from weakening a test;
# what it stops is a change to the test commands, to what the runners load
# before the first test (see run_test_gate), to the hooks, and the deletion of
# a test file. Tests run on the branch's commit before the merge; only the
# pre-push hook's web tests run on the merged result. Python tests are not
# re-run after the merge. The Playwright tests under web/tests/ui are not part
# of the gate.
# The push sends everything on the target branch of this checkout that the
# remote does not have yet: the merge, the status commits this loop made, and
# any commit a person (or a session, which is not sandboxed) made here directly.
# The configuration is read from this checkout's working tree each cycle
# (scripts/cas-exec edits it in place), committed or not: the loop never
# commits it, but a session that rewrites it changes the limits the next cycle
# runs under.
# The build check is a snapshot: a build the worker claims after the last zero
# count and before its redeploy has finished is still cut off (the next worker
# process's orphan recovery resumes or requeues it; see railway.toml).
# The spend figure is the platform's spend today as the web app's cost report
# gives it, not this loop's own; the report is complete only when the web app
# has the Anthropic cost API configured, so without that no session starts.
#
# `cas-roadmap-loop.sh --guard-check` runs only the merge guard's path rules:
# it reads `git diff -z --name-status --no-renames <target>...<branch>` on
# stdin, prints the paths the guard refuses, and exits without starting a loop
# (status 0; status 2 when the listing cannot be read, which is a refusal).
#
# Needs: macOS or Linux with bash, git, python3 (3.11 or newer, with pytest 8.4
# or newer: the gate uses `python3 -P` and `--disable-plugin-autoload`, and
# fails closed without them), curl (7.55 or newer, for -H @-), node/npm, and the
# `claude` CLI (CLAUDE_BIN overrides its path); `gh` for the tiered-governance
# PR path (GH_BIN overrides its path); an origin remote. launchd installs it via
# scripts/com.cas.roadmap-executor.plist.template; the watchdog is
# scripts/cas-executor-watchdog.sh.
# The web app's ops API (CAS_OPS_BASE_URL, default http://localhost:4000, with
# OPS_API_KEY from the environment or web/.env) is needed for the build check
# and the spend ceiling: without its answers no session is started and nothing
# is pushed. Its other uses (heartbeats, run results, the zombie sweep) are
# best-effort. The key is never put on a command line.
# Everything the loop waits for has a time limit: CAS_OPS_TIMEOUT_SEC per ops
# API request (default 10; an answer is also cut at 8 MiB),
# CAS_EXECUTOR_NET_TIMEOUT_SEC for git fetch and pull, gh and the gate's npm
# install (default 300), CAS_EXECUTOR_GATE_TIMEOUT_SEC for each of the gate's
# two test commands (default 1800), CAS_EXECUTOR_PUSH_TIMEOUT_SEC for a push,
# which also runs the pre-push hook (default 1800), and 20 minutes for the
# session. What runs over is ended as a process group.
# Other environment: CAS_EXECUTOR_STATE_DIR (locks/counters, default $TMPDIR or
# /tmp), RUN_ONCE=1 (one cycle, then exit), CLAUDE_BIN, GH_BIN, CRASH_RATE_WINDOW_SEC,
# CRASH_RATE_LIMIT, ZOMBIE_SWEEP_MAX_AGE_MIN.
set -euo pipefail

# Ensure Homebrew paths are available (launchd has minimal PATH)
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$PROJECT_DIR/scripts/cas-executor-config.json"
# Lock files, the daily counter, the failure tracker and the per-run PID/timeout
# markers live in STATE_DIR: CAS_EXECUTOR_STATE_DIR, else the user's temp dir
# ($TMPDIR, which on macOS is private to the user; /tmp when unset). The picker,
# the watchdog and `cas-exec failures` resolve it the same way, and the picker
# is handed the resolved value below.
STATE_DIR="${CAS_EXECUTOR_STATE_DIR:-${TMPDIR:-/tmp}}"
STATE_DIR="${STATE_DIR%/}"
export CAS_EXECUTOR_STATE_DIR="$STATE_DIR"
LOCKFILE="$STATE_DIR/cas-executor.lock"
LOG_DIR="$PROJECT_DIR/logs/executor"
WORKTREE_DIR="$PROJECT_DIR/.worktrees"
DAILY_COUNTER_FILE="$STATE_DIR/cas-executor-daily-count"
FAIL_TRACKER="$STATE_DIR/cas-executor-failures"
# Base URL of the CAS web app that serves /ops/api/v1/*. Defaults to a local
# dev server. Heartbeats, run results and the zombie sweep are best-effort; the
# build check (active_build_count) and the cost report are not: when either
# goes unanswered preflight starts no session, and wait_for_builds_clear
# refuses the push when the build check does.
OPS_BASE="${CAS_OPS_BASE_URL:-http://localhost:4000}"
# Time limits, in seconds, for everything that talks to the network (curl's own
# limits for the ops API; with_timeout for git, gh and npm install). A value
# that is not a positive number (digits, optionally a decimal part) falls back
# to the default; zero would mean "no limit" to curl.
seconds_or() {
    if [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]] && [[ ! "$1" =~ ^[0.]+$ ]]; then
        printf '%s' "$1"
    else
        printf '%s' "$2"
    fi
}
OPS_TIMEOUT_SEC=$(seconds_or "${CAS_OPS_TIMEOUT_SEC:-}" 10)
NET_TIMEOUT_SEC=$(seconds_or "${CAS_EXECUTOR_NET_TIMEOUT_SEC:-}" 300)
PUSH_TIMEOUT_SEC=$(seconds_or "${CAS_EXECUTOR_PUSH_TIMEOUT_SEC:-}" 1800)
# Each of the gate's two test commands (npm test, pytest) gets this long. A
# test that never returns would otherwise hold the loop for good.
GATE_TIMEOUT_SEC=$(seconds_or "${CAS_EXECUTOR_GATE_TIMEOUT_SEC:-}" 1800)
# The Claude session's hard limit (20 minutes).
SESSION_TIMEOUT_SEC=1200
# The most the loop reads of one ops API response. The answers it needs are a
# few kilobytes; a longer body is cut here, which makes curl fail, which every
# caller treats as no answer.
OPS_MAX_BYTES=8388608
# Where the gate's Python tests live, relative to the repository root. Every
# directory that holds pytest files belongs here: the merge guard keeps a
# branch from deleting a test anywhere, and a test the gate never runs
# protects nothing (tests/test_executor_loop_behavior.py walks the tree and
# fails a directory this list leaves out).
GATE_PYTEST_DIRS="tests compose/tests"
# Build statuses that mean the worker has a customer build in hand or is about
# to: queued (the next claim takes it) and the three pipeline stages
# (_STAGE_ORDER in queue/worker_pg.py). A merge pushed by this loop redeploys
# the worker whatever the branch changed: the push also carries the item's
# roadmap.json commit, and coordinator/ is in the worker's watchPatterns in
# railway.toml. A redeploy cuts off a build in any of these statuses.
# tests/test_executor_ops_contract.py compares this list with the worker's
# stages and with the counts the ops API returns.
ACTIVE_BUILD_STATUSES="queued building testing deploying"
# Tracked files that may differ from HEAD without the cycle yielding: local
# operator state (scripts/cas-exec rewrites the config; the failure KB is
# updated by local builds) and CLAUDE.md. The loop never commits them; the
# pre-merge stash sets them aside for the merge. roadmap.json is deliberately
# not here (see preflight_check). An unmerged path is never tolerated, whatever
# its name: preflight_check yields on it before this filter runs, since a
# conflicted stash pop leaves one of these very files unmerged (status UU).
# scripts/cas-executor-watchdog.sh carries the same expression.
DIRTY_OK_RE='(cli/failure_kb\.json|scripts/cas-executor-config\.json|CLAUDE\.md)$'
MERGE_LOCK="$STATE_DIR/cas-executor-merge.lock"
GIT_SETUP_LOCK="$STATE_DIR/cas-executor-git-setup.lock"
CRASH_LOG="$LOG_DIR/crash.log"
# Crash-loop detector thresholds (see check_crash_loop): CRASH_RATE_LIMIT crashes
# within CRASH_RATE_WINDOW_SEC make the next start sleep one full window.
CRASH_RATE_WINDOW_SEC="${CRASH_RATE_WINDOW_SEC:-900}"
CRASH_RATE_LIMIT="${CRASH_RATE_LIMIT:-3}"
# The merge guard's rules live in scripts/cas-merge-guard.py: PROTECTED_PATHS
# (what a branch may never add, modify, delete or rename), TEST_DIRS (where a
# branch may add and edit but not delete), the refusal of any path that is not
# plain ASCII, and the refusal of symlinks and submodule links. This script
# only hands it a diff.
GUARD_HELPER="$SCRIPT_DIR/cas-merge-guard.py"
# The error text posted with a guard rejection.
GUARD_REJECT_REASON="Branch changed a protected path (CLAUDE.md or .claude/, scripts/, hooks, workflows, a package manifest or npm configuration, a test-runner configuration or setup file, a top-level Python module, roadmap.json), deleted a test file, used a path that is not plain ASCII, or added a symlink or submodule; nothing was merged."
# The error text posted when the branch is no longer the commit that was tested.
PIN_REJECT_REASON="The branch moved after its tests ran, so it is no longer the commit that was tested; nothing was merged."

# The merge guard's path rules. Reads `git diff -z --name-status --no-renames`
# records (STATUS NUL PATH NUL) on stdin, or `git diff -z --raw --no-renames`
# records when called with --raw, and prints one line per refused path.
# Returns non-zero when the listing could not be read and judged (a record cut
# short, python3 or the helper missing): the caller refuses the branch then,
# exactly as it does for a printed line.
#   --no-renames matters: with rename detection `git mv .githooks/pre-push
#     docs/x` is listed under the new name only, and the hook's removal would
#     never be compared against the protected paths.
#   -z matters: without it git quotes a path that has a non-ASCII byte or a
#     double quote in it, and the guard would be judging git's quoting.
# The comparison is done on bytes by the helper, not by grep: grep folds case
# by the locale's rules, which is how a path spelled with U+212A (KELVIN SIGN)
# for the k of .githooks passes a case-insensitive match and still lands in
# .githooks/ on a filesystem that folds Unicode.
guard_violations() {
    LC_ALL=C python3 -I "$GUARD_HELPER" "$@"
}

# Everything the merge guard refuses in commit $2 relative to the point where
# it forked from commit or ref $1 (three-dot diff: only what the branch
# changed, not what the base gained meanwhile), with git run in directory $3.
# Prints one line per finding: a refused path, or a path the branch leaves as
# a symlink or a submodule link (--raw carries the file modes). Returns
# non-zero when the diff cannot be computed or read; the caller refuses the
# branch in that case too. A caller that means a branch passes its full name
# (refs/heads/...): a short name is resolved with tags first, so a tag named
# like the branch would be diffed in its place.
# --ignore-submodules=none is spelled out because the repository's
# configuration (diff.ignoreSubmodules) could otherwise leave a submodule link
# out of the listing.
guard_report() {
    local base="$1" branch="$2" dir="$3"
    ( set -o pipefail
      LC_ALL=C git -C "$dir" diff -z --raw --no-renames --ignore-submodules=none "$base...$branch" 2>/dev/null \
          | guard_violations --raw )
}

if [ "${1:-}" = "--guard-check" ]; then
    guard_violations || exit 2
    exit 0
fi
# Zombie-sweep window: rows stuck in running/working/pr_created older than this
# are marked failed on startup (covers previous-crash leftovers).
ZOMBIE_SWEEP_MAX_AGE_MIN="${ZOMBIE_SWEEP_MAX_AGE_MIN:-60}"

mkdir -p "$LOG_DIR" "$WORKTREE_DIR" "$STATE_DIR"

# Strip whitespace/newlines from a value so it's safe for shell arithmetic
sanitize_int() { echo "${1:-0}" | tr -d '[:space:]'; }

# curl with the loop's limits: quiet, fails on an HTTP error status, bounded in
# time (a server that accepts the connection and then says nothing would
# otherwise hold the loop and, around a merge, the merge lock) and in size
# (OPS_MAX_BYTES: a longer body is cut, curl then fails on the closed pipe,
# and the caller sees no answer).
bounded_curl() {
    ( set -o pipefail
      curl -sf --connect-timeout 5 --max-time "$OPS_TIMEOUT_SEC" "$@" | head -c "$OPS_MAX_BYTES" )
}

# bounded_curl for the ops API. The bearer key is read fresh for each call (so
# a rotated key needs no restart) and handed to curl on its standard input
# (-H @-), never on its command line, which every user of the machine can read
# in the process list.
ops_curl() {
    local key
    key=$(get_ops_key)
    key="${key//[$'\r\n']/}"
    printf 'Authorization: Bearer %s\n' "$key" | bounded_curl -H @- "$@"
}

# ---------------------------------------------------------------------------
# Background jobs: each one is a process group, with a record
# ---------------------------------------------------------------------------
# Everything the loop runs with a time limit (the Claude session, the gate's
# npm and pytest, git fetch, pull and push, gh) is started by start_job as the
# leader of a process group of its own, so that it can be ended as a group
# however deep its process tree is. Each job leaves a record in the state
# directory, named after this loop's process id and the job's, holding the
# job's start time: a later start of the loop ends the jobs of a loop that
# died without cleaning up (stop_orphan_jobs), and the start time is what
# tells such a job from an unrelated process that was given its number since.

# A wait of a fraction of a second between two looks at something. Where
# sleep takes whole seconds only, it waits one second.
short_sleep() { sleep "$1" 2>/dev/null || sleep 1; }

# start_job COMMAND [ARG...]: run the command in the background as a process
# group leader and leave its process id in JOB_PID. Job control (set -m) is
# what makes bash give a background job a group of its own; it is switched on
# only for the fork. Standard input is /dev/null, as for any background job of
# a script (a job in a background group that read a terminal would be
# stopped). The command may be a shell function: it then runs in a subshell,
# so a `cd` inside it does not leak.
start_job() {
    set -m
    "$@" < /dev/null &
    JOB_PID=$!
    set +m
    ps -o lstart= -p "$JOB_PID" > "$STATE_DIR/cas-exec-job-$$-$JOB_PID" 2>/dev/null || true
}

job_forget() { rm -f "$STATE_DIR/cas-exec-job-$$-$1"; }

# The process ids below $1, every generation, one per line. Read before
# anything is signalled: once a parent has exited its children are re-parented
# and can no longer be found through it.
descendants_of() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null || true); do
        echo "$child"
        descendants_of "$child"
    done
}

# jobs_running "LEADERS" "PIDS": true while a process in one of those groups,
# or one of those processes, is still running (a child that has exited and has
# not been waited for yet does not count).
jobs_running() {
    ps -A -o pid=,pgid=,stat= 2>/dev/null | awk -v leaders=" $1 " -v pids=" $1 $2 " '
        $3 !~ /^Z/ && (index(leaders, " " $2 " ") || index(pids, " " $1 " ")) { found = 1 }
        END { exit found ? 0 : 1 }'
}

# stop_jobs PID...: end jobs started by start_job, and everything they started.
# SIGTERM goes to each job's process group and to every process still below it
# in the process tree (the second list is what reaches a child that moved to a
# group of its own, as the tool subprocesses of a Claude session may). What is
# still running two seconds later gets SIGKILL. Not reached: a process that
# has both left the group and been re-parented (a double fork with setsid).
stop_jobs() {
    local pid leaders="" kin="" waited=0
    for pid in "$@"; do
        case "$pid" in ''|*[!0-9]*|0|1) continue ;; esac
        leaders="$leaders $pid"
        kin="$kin $(descendants_of "$pid" | tr '\n' ' ')"
    done
    [ -n "$leaders" ] || return 0
    signal_jobs TERM "$leaders" "$kin"
    while jobs_running "$leaders" "$kin"; do
        if [ "$waited" -ge 10 ]; then
            signal_jobs KILL "$leaders" "$kin"
            break
        fi
        short_sleep 0.2
        waited=$((waited + 1))
    done
    return 0
}

signal_jobs() {
    local sig="$1" pid
    for pid in $2; do
        kill -s "$sig" -- "-$pid" 2>/dev/null || true
    done
    # shellcheck disable=SC2086  # one argument per process id
    kill -s "$sig" $2 $3 2>/dev/null || true
}

# End the jobs a previous loop left running (it was killed outright, or the
# machine went down). Called once the main lock is held, when every record in
# the state directory belongs to a loop that is gone. A record is acted on
# only when the process with that number still has the recorded start time.
stop_orphan_jobs() {
    local record pid started orphans=""
    for record in "$STATE_DIR"/cas-exec-job-*; do
        [ -f "$record" ] || continue
        pid="${record##*-}"
        started=$(cat "$record" 2>/dev/null || true)
        rm -f "$record"
        case "$pid" in ''|*[!0-9]*) continue ;; esac
        if [ -n "$started" ] && [ "$(ps -o lstart= -p "$pid" 2>/dev/null || true)" = "$started" ]; then
            echo "Stopping a job a previous executor left running (PID $pid)"
            orphans="$orphans $pid"
        fi
    done
    # shellcheck disable=SC2086  # one argument per process id
    stop_jobs $orphans
}

# The timer half of with_timeout, run as a job of its own: after $1 seconds,
# if process $2 is still there, drop the marker file $3 and end that job.
timeout_watch() {
    sleep "$1"
    if kill -0 "$2" 2>/dev/null; then
        : > "$3" 2>/dev/null || true
        stop_jobs "$2"
    fi
}

# with_timeout SECONDS COMMAND [ARG...]: run the command as a job and end the
# job (stop_jobs) if it has not finished after SECONDS. Returns the command's
# exit status, or 124 when it was ended by the timer; TIMED_OUT is 1 in that
# case and 0 otherwise. macOS ships no `timeout`, so the timer is a second
# job; the marker file tells an end by the timer from the command's own
# failure. The timer is a separate process: it still ends the command when
# this shell has been killed in the meantime.
TIMED_OUT=0
with_timeout() {
    local limit="$1"
    shift
    start_job "$@"
    local cmd_pid="$JOB_PID"
    local marker="$STATE_DIR/cas-exec-timeout-$$-$cmd_pid"
    rm -f "$marker"
    start_job timeout_watch "$limit" "$cmd_pid" "$marker"
    local timer_pid="$JOB_PID" rc=0
    wait "$cmd_pid" 2>/dev/null || rc=$?
    TIMED_OUT=0
    if [ -f "$marker" ]; then
        # The timer is ending the command's process group; let it finish.
        wait "$timer_pid" 2>/dev/null || true
        TIMED_OUT=1
    else
        kill -s TERM -- "-$timer_pid" 2>/dev/null || true
        wait "$timer_pid" 2>/dev/null || true
    fi
    job_forget "$cmd_pid"
    job_forget "$timer_pid"
    rm -f "$marker"
    if [ "$TIMED_OUT" -eq 1 ]; then
        local what="$*"
        echo "[timeout] gave up after ${limit}s: ${what:0:160}" >&2
        return 124
    fi
    return "$rc"
}

# The test gate's commands run under with_timeout and have to change
# directory first, so they are functions.
gate_npm_install() { cd "$1/web" && npm install --silent; }
gate_npm_test() { cd "$1/web" && npm test; }
# $1 the gate checkout, $2 the pytest configuration file to use. See
# run_test_gate for what each option keeps out.
gate_pytest() {
    cd "$1" || return 1
    # shellcheck disable=SC2086  # GATE_PYTEST_DIRS is a list of directories
    env -u PYTHONPATH -u PYTEST_ADDOPTS -u PYTEST_PLUGINS \
        python3 -P -m pytest -c "$2" --rootdir "$1" --confcutdir "$1" \
        --disable-plugin-autoload -p no:cacheprovider $GATE_PYTEST_DIRS -q
}

# The paths of the main checkout that have an unmerged index entry: a stash pop
# that conflicted, or a merge that is not finished. One path per line, nothing
# when there are none. Returns 1 when the index cannot be read, and a caller
# that needs to know whether the checkout is clean treats that as unmerged.
unmerged_paths() {
    local listing
    listing=$(git -C "$PROJECT_DIR" ls-files -u 2>/dev/null) || return 1
    printf '%s\n' "$listing" | cut -f2 | sort -u | sed '/^$/d'
}

# Pop the pre-merge stash that carries $1 in its message, wherever it is on
# the stack. Safe to call with an empty tag (no-op). Logs to $2. See the
# pre-merge dirty safety block in execute_item() for why the stash exists.
# A pop that applies cleanly drops the stash. A pop that conflicts leaves the
# stash on the stack (git keeps it) and leaves conflict markers and an unmerged
# path in the checkout; this says so, with the stash ref, and preflight_check
# then yields until no path is unmerged. Never fails the caller: it is called
# from statements that run under set -e.
pop_premerge_stash() {
    local tag="$1" log_file="$2" ref pop_rc=0 unmerged
    if [ -z "$tag" ]; then return 0; fi
    ref=$(git -C "$PROJECT_DIR" stash list --format='%gd %gs' 2>/dev/null \
        | awk -v t="$tag" 'index($0, t) { print $1; exit }' || true)
    if [ -z "$ref" ]; then return 0; fi
    git -C "$PROJECT_DIR" stash pop "$ref" >> "$log_file" 2>&1 || pop_rc=$?
    # A pop that succeeded dropped the stash, whatever else the index holds, so
    # only a pop that did not succeed can keep it.
    if [ "$pop_rc" -eq 0 ]; then
        echo "Pre-merge: popped stash $tag" | tee -a "$log_file"
        return 0
    fi
    if ! unmerged=$(unmerged_paths); then
        unmerged="(the index cannot be read)"
    fi
    if [ -n "$unmerged" ]; then
        echo "Pre-merge: stash pop conflicted; stash kept as $ref ($tag). Unmerged: $(echo "$unmerged" | tr '\n' ' '). Resolve those paths and the stash by hand (git status, git stash list); the executor yields until no path is unmerged" | tee -a "$log_file"
    else
        echo "Pre-merge: stash $tag could not be applied; it is left on the stack for manual resolution (git stash list)" | tee -a "$log_file"
    fi
    return 0
}

# Read OPS_KEY fresh each cycle (survives key rotation without restart).
# Precedence: OPS_API_KEY in the environment, then web/.env. Always returns 0
# (an empty key just means the ops calls are rejected), because
# under `set -e` a failing grep on a missing web/.env would kill the loop.
get_ops_key() {
    if [ -n "${OPS_API_KEY:-}" ]; then
        printf '%s\n' "$OPS_API_KEY"
        return 0
    fi
    grep '^OPS_API_KEY=' "$PROJECT_DIR/web/.env" 2>/dev/null | head -n1 | cut -d= -f2- || true
}

# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------
# A lock is a file that holds its owner's process id (the main lock, the merge
# lock and the git-setup lock are all this). It is created in one step, with
# the id already in it: the id is written to a private file first and the lock
# name is then hard-linked to it, and link(2) fails when the name exists. Two
# processes can therefore not both create it, and no reader ever sees a lock
# without an owner.
# A lock whose owner is not running is stale. Removing a stale lock has to be
# exclusive as well: of two processes that both saw the dead owner, the second
# must not remove the lock the first has just taken. So a stale lock is
# removed only by the process that holds FILE.breaking (a directory; mkdir is
# atomic), and only after it has read the owner again.
# The owner is identified by its process id alone: an unrelated process that
# was given the id of a dead owner makes the lock look held until it exits.

# Modification time in epoch seconds: GNU stat first (BSD stat rejects -c), then BSD.
file_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }

# lock_owner_running PID [not-me]: is a lock that names PID held? With
# "not-me", this process's own id does not count (the main lock at start:
# nothing of this process can hold it yet, so its id there is a leftover).
# 0 and 1 are never an owner: `kill -0 0` asks about this process's own group
# and always succeeds, and process 1 is always there.
lock_owner_running() {
    case "$1" in ''|*[!0-9]*|0|1) return 1 ;; esac
    if [ "${2:-}" = "not-me" ] && [ "$1" = "$$" ]; then return 1; fi
    kill -0 "$1" 2>/dev/null
}

# break_lock FILE MODE: remove FILE while holding FILE.breaking. MODE "stale"
# removes it only if its owner is not running, "stale-not-me" likewise with
# this process's id counted as not running, "force" removes it regardless.
break_lock() {
    local file="$1" mode="$2" guard="$1.breaking" holder
    if ! mkdir "$guard" 2>/dev/null; then
        # Another process is in here, or one died in here: a guard older than
        # 30 seconds is a leftover (the section below takes milliseconds).
        if [ $(( $(date +%s) - $(file_mtime "$guard") )) -gt 30 ]; then
            rmdir "$guard" 2>/dev/null || true
        fi
        short_sleep 0.1
        return 0
    fi
    holder=$(cat "$file" 2>/dev/null || true)
    case "$mode" in
        force) rm -f "$file" ;;
        stale-not-me) lock_owner_running "$holder" not-me || rm -f "$file" ;;
        *) lock_owner_running "$holder" || rm -f "$file" ;;
    esac
    if [ "$mode" != "force" ] && [ ! -e "$file" ] && [ -n "$holder" ]; then
        echo "Removed the stale lock $(basename "$file") (its owner, PID $holder, is not running)" >&2
    fi
    rmdir "$guard" 2>/dev/null || true
}

# try_lock FILE [not-me]: take the lock, or return 1 with the running owner's
# id in LOCK_OWNER. A stale lock is removed (break_lock) and the attempt is
# repeated.
LOCK_OWNER=""
try_lock() {
    local file="$1" not_me="${2:-}" mine holder tries=0
    LOCK_OWNER=""
    mine=$(mktemp "$file.XXXXXX" 2>/dev/null) || return 1
    echo "$$" > "$mine"
    while [ "$tries" -lt 100 ]; do
        if ln "$mine" "$file" 2>/dev/null; then
            rm -f "$mine"
            return 0
        fi
        holder=$(cat "$file" 2>/dev/null || true)
        if lock_owner_running "$holder" "$not_me"; then
            LOCK_OWNER="$holder"
            rm -f "$mine"
            return 1
        fi
        break_lock "$file" "stale${not_me:+-$not_me}"
        tries=$((tries + 1))
    done
    rm -f "$mine"
    return 1
}

# Remove a lock this process took. A lock that names another process (ours was
# forced after its time limit) is left alone.
unlock() {
    if [ "$(cat "$1" 2>/dev/null || true)" = "$$" ]; then rm -f "$1"; fi
}

# The main lock: one executor per state directory. LOCK_HELD records that THIS
# process took it: on_exit must only release the lock and delete worktrees
# when it did, otherwise a second launch (a stray `cas-exec run-once`, a
# launchd kickstart) would exit here and, on its way out, delete the live
# executor's lock and worktrees.
LOCK_HELD=0
acquire_lock() {
    if try_lock "$LOCKFILE" not-me; then
        LOCK_HELD=1
        return 0
    fi
    if [ -n "$LOCK_OWNER" ]; then
        echo "Another executor is running (PID $LOCK_OWNER). Exiting."
    else
        echo "The executor lock $LOCKFILE could not be taken. Exiting."
    fi
    CLEAN_EXIT=1   # sanctioned exit, not a crash
    exit 1
}

release_lock() { unlock "$LOCKFILE"; }

# CLEAN_EXIT discipline (crash-surfacing, see on_exit + check_crash_loop below).
# Default = 0 (anything is a crash until proven otherwise). Set to 1 only on
# sanctioned exits: SIGTERM/SIGINT from launchctl unload, or RUN_ONCE break.
# ITERATION_PHASE is set at cycle checkpoints so on_exit can attribute the
# crash to a specific phase (preflight / picking / executing / sleeping).
CLEAN_EXIT=0
ITERATION_PHASE="startup"
# What SIGTERM and SIGINT do to this shell. (An item running as a background
# subshell has its own: see the dispatch in the main loop.)
SIGNAL_HANDLER='CLEAN_EXIT=1; exit 0'
trap "$SIGNAL_HANDLER" TERM INT

# hold_signals ... release_signals: between the two, SIGTERM and SIGINT are
# noted and not acted on; release_signals then does what the signal asked for.
# For a few commands that must not be cut apart (see record_roadmap_status).
# A signal that cannot be caught is not held by this, of course.
HELD_SIGNAL=""
hold_signals() {
    HELD_SIGNAL=""
    trap 'HELD_SIGNAL=1' TERM INT
}
release_signals() {
    trap "$SIGNAL_HANDLER" TERM INT
    if [ -n "$HELD_SIGNAL" ]; then
        HELD_SIGNAL=""
        eval "$SIGNAL_HANDLER"
    fi
}

# The EXIT trap is installed further down, AFTER on_exit is defined.
# Until then, if the script crashes during the constant-assignment block
# above, launchd stderr log is the only signal.

# The configuration, one value per line, read and checked by the picker
# (--loop-config in scripts/cas-pick-item.py, which lists the lines). It is the
# one reader of "enabled", for this loop and for the watchdog alike: only a
# JSON true enables. A config that is not a JSON object, or holds a value of
# the wrong kind, makes this fail, and the cycle yields.
read_all_config() {
    python3 "$SCRIPT_DIR/cas-pick-item.py" --loop-config
}

# Build JSON payload safely using python3 json.dumps (prevents SyntaxError from special chars)
_json_payload() {
    python3 -c "
import json, sys, os
pairs = []
i = 1
while i < len(sys.argv):
    key = sys.argv[i]
    val = sys.argv[i+1]
    typ = sys.argv[i+2]  # s=string, i=int, f=float
    if typ == 'i':
        pairs.append((key, int(val) if val else 0))
    elif typ == 'f':
        pairs.append((key, float(val) if val else 0.0))
    else:
        pairs.append((key, val))
    i += 3
print(json.dumps(dict(pairs)))
" "$@"
}

# ---------------------------------------------------------------------------
# The merge journal: what an interrupted merge section left, and settling it
# ---------------------------------------------------------------------------
# Between the pre-merge stash and the end of the push this checkout passes
# through states that are wrong to stay in: tolerated local edits (the
# operator's config, the failure KB) sit in a stash, the target branch holds a
# merge that is not pushed, perhaps with the item's "completed" commit on top.
# A signal, a kill or a power cut can stop the loop in any of them. Before it
# enters the first one the loop writes this journal (in logs/executor/, which
# survives a reboot; the state directory may not), updates it before the merge
# and before the push, and removes it when the section is closed. Whoever
# finds the journal knows what was in flight:
#   item           the roadmap item
#   target         the branch being merged into
#   stash_tag      the message of the pre-merge stash ("" when none was made)
#   tested_sha     the commit being merged
#   pre_merge_sha  the target branch before the merge ("" until just before it)
#   phase          stash (nothing merged yet), merge (the merge and the status
#                  commit are being made), push (the push has been started)
# settle_interrupted_merge brings the checkout back to a state a cycle can
# start from. It runs on the way out of a signal (offline: no network), at the
# top of every cycle, and after a push that did not report success (online):
#   - nothing merged: the stash is popped (and a merge that git itself was
#     stopped in the middle of is aborted).
#   - merged, and the remote has it (the push got through before the loop was
#     stopped): the commits stay, the stash is popped.
#   - merged, and the remote does not have it: the target branch is put back
#     to pre_merge_sha (take_back_merge: git reset --keep, which refuses to
#     overwrite a local edit rather than discard it, and only on the target
#     branch) and the stash is popped. The item is still pending and is worked
#     again. execute_item takes a merge back through the same function when it
#     cannot record the item's status or update this journal; if take_back_merge
#     refuses there, the merge, the journal and the stash stay for this routine.
#   - merged during phase push, and the remote cannot be asked: nothing is
#     changed and the journal stays. The next cycle asks again, and no cycle
#     starts a session before there is an answer.
# It takes back only commits it can show are the loop's own: the first-parent
# chain from pre_merge_sha to HEAD must be the merge of tested_sha and, at
# most, one commit on top that changes coordinator/roadmap.json alone.
# Anything else on the branch is somebody's work: the journal then stays,
# every cycle yields with that reason, and a person decides (push or reset,
# then delete the journal file).
MERGE_JOURNAL="$LOG_DIR/merge-in-progress"
SETTLE_OUTCOME=""
SETTLE_REASON=""
# Process ids of the items running as background jobs (max_parallel > 1).
ITEM_PIDS=""

# journal_write KEY VALUE [KEY VALUE...]: replace the journal in one step.
journal_write() {
    local tmp="$MERGE_JOURNAL.tmp.$$"
    : > "$tmp" || return 1
    while [ "$#" -ge 2 ]; do
        printf '%s=%s\n' "$1" "$2" >> "$tmp" || return 1
        shift 2
    done
    mv -f "$tmp" "$MERGE_JOURNAL"
}

journal_get() {
    awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' \
        "$MERGE_JOURNAL" 2>/dev/null || true
}

# Is the first-parent chain from commit $1 (exclusive) to commit $3 exactly the
# merge of $2 into $1, with at most one commit on top of it that changes
# coordinator/roadmap.json alone?
merge_is_the_loops_own() {
    local pre="$1" tested="$2" head="$3" chain count merge mark=""
    chain=$(git rev-list --first-parent "$pre..$head" 2>/dev/null) || return 1
    count=$(printf '%s\n' "$chain" | grep -c . || true)
    case "$count" in
        1) merge="$chain" ;;
        2) mark=$(printf '%s\n' "$chain" | sed -n '1p')
           merge=$(printf '%s\n' "$chain" | sed -n '2p') ;;
        *) return 1 ;;
    esac
    [ "$(git rev-list --parents -n 1 "$merge" 2>/dev/null || true)" = "$merge $pre $tested" ] || return 1
    if [ -n "$mark" ]; then
        [ "$(git rev-list --parents -n 1 "$mark" 2>/dev/null || true)" = "$mark $merge" ] || return 1
        [ "$(git diff-tree --no-commit-id --name-only -r "$mark" 2>/dev/null || true)" = "coordinator/roadmap.json" ] || return 1
    fi
    return 0
}

# coordinator/roadmap.json as an interrupted status write may have left it:
# when it differs from HEAD by exactly the "completed" mark of item $1, HEAD's
# copy is put back. Returns 1 when it differs in any other way: that is
# somebody else's edit, and it stays.
undo_interrupted_status_write() {
    local item="$1" log_file="$2" roadmap="coordinator/roadmap.json" before rc=0
    if git diff --quiet HEAD -- "$roadmap" 2>/dev/null; then return 0; fi
    before="$STATE_DIR/cas-exec-roadmap-settle-$$.json"
    if ! git show "HEAD:$roadmap" > "$before" 2>>"$log_file"; then
        rm -f "$before"
        return 1
    fi
    python3 "$SCRIPT_DIR/cas-pick-item.py" --verify-transition "$before" "$item" completed >> "$log_file" 2>&1 || rc=1
    if [ "$rc" -eq 0 ]; then
        git reset -q -- "$roadmap" >> "$log_file" 2>&1 || true
        cp "$before" "$roadmap" 2>>"$log_file" || rc=1
    fi
    rm -f "$before"
    return "$rc"
}

# take_back_merge ITEM TARGET PRE TESTED LOG_FILE: put branch TARGET back at
# PRE, the commit it was at before this loop merged TESTED into it. This is the
# only place the loop moves a branch backwards: the settle routine below, the
# failed status mark and the failed journal update in execute_item all come
# through here, so they all make the same checks:
#   - the checkout is on TARGET, the branch the merge was made on. Another
#     branch is somebody's, and `git reset` moves whatever branch is checked
#     out;
#   - the first-parent chain from PRE to HEAD is the loop's own (see
#     merge_is_the_loops_own);
#   - coordinator/roadmap.json is HEAD's copy, or differs from it by exactly
#     the item's "completed" mark, which is put back (see
#     undo_interrupted_status_write);
#   - the reset is `git reset --keep`, which leaves an edit to a file the merge
#     did not touch where it is and refuses (rather than overwrites) an edit to
#     a file the merge changed. Nothing is ever discarded: there is no
#     `reset --hard` anywhere in this script.
# Returns 0 when TARGET is back at PRE. Returns 1, with TAKE_BACK_REASON set,
# when it is not safe: no branch has moved and no edit of a person's has been
# touched (the loop's own half-written status mark, if there was one, may have
# been put back). The caller leaves the merge, the stash and the journal where
# they are, and the next cycle's settle routine asks again (and, until it can,
# starts no session).
TAKE_BACK_REASON=""
take_back_merge() {
    local item="$1" target="$2" pre="$3" tested="$4" log_file="$5" head
    TAKE_BACK_REASON=""
    if [ -z "$item" ] || [ -z "$target" ] || [ -z "$pre" ] || [ -z "$tested" ]; then
        TAKE_BACK_REASON="the item, the branch or a commit to go back to is not known"
        return 1
    fi
    if ! cd "$PROJECT_DIR"; then
        TAKE_BACK_REASON="cannot enter $PROJECT_DIR"
        return 1
    fi
    if [ "$(git symbolic-ref -q HEAD 2>/dev/null || true)" != "refs/heads/$target" ]; then
        TAKE_BACK_REASON="the project directory is not on $target, where the merge was made"
        return 1
    fi
    head=$(git rev-parse --verify -q "HEAD^{commit}" 2>/dev/null || true)
    if [ -z "$head" ]; then
        TAKE_BACK_REASON="HEAD cannot be read"
        return 1
    fi
    if [ "$head" = "$pre" ]; then
        return 0
    fi
    if ! merge_is_the_loops_own "$pre" "$tested" "$head"; then
        TAKE_BACK_REASON="$target holds commits this loop did not make"
        return 1
    fi
    if ! undo_interrupted_status_write "$item" "$log_file"; then
        TAKE_BACK_REASON="coordinator/roadmap.json holds changes this loop did not make"
        return 1
    fi
    if ! git reset -q --keep "$pre" >> "$log_file" 2>&1; then
        TAKE_BACK_REASON="git reset --keep refused: a local edit would be overwritten (see $log_file)"
        return 1
    fi
    return 0
}

# settle_interrupted_merge MODE LOG_FILE. MODE is "offline" or "online" (see
# above). Returns 0 when nothing is left to settle: the journal is gone and
# SETTLE_OUTCOME says what was found ("" when there was no journal,
# "nothing-merged", "pushed" or "taken-back"). Returns 1 with the journal in
# place and SETTLE_REASON set when it could not be settled.
settle_interrupted_merge() {
    local mode="$1" log_file="$2"
    SETTLE_OUTCOME=""
    SETTLE_REASON=""
    [ -f "$MERGE_JOURNAL" ] || return 0
    local item target pre tested stash_tag phase head merge_head landed="unknown"
    item=$(journal_get item)
    target=$(journal_get target)
    pre=$(journal_get pre_merge_sha)
    tested=$(journal_get tested_sha)
    stash_tag=$(journal_get stash_tag)
    phase=$(journal_get phase)
    case "$phase" in
        stash|merge|push) ;;
        *) phase="" ;;
    esac
    if [ -z "$phase" ] || [ -z "$item" ] || [ -z "$target" ] || [ -z "$tested" ]; then
        SETTLE_REASON="the merge journal $MERGE_JOURNAL cannot be read; look at the checkout, then delete it"
        return 1
    fi
    if ! cd "$PROJECT_DIR"; then
        SETTLE_REASON="cannot enter $PROJECT_DIR"
        return 1
    fi
    if [ "$(git symbolic-ref -q HEAD 2>/dev/null || true)" != "refs/heads/$target" ]; then
        SETTLE_REASON="the merge of $item was interrupted and the project directory is not on $target"
        return 1
    fi
    head=$(git rev-parse --verify -q "HEAD^{commit}" 2>/dev/null || true)
    if [ -z "$pre" ] || [ "$head" = "$pre" ]; then
        merge_head=$(cat "$(git rev-parse --git-path MERGE_HEAD 2>/dev/null || echo /nonexistent)" 2>/dev/null || true)
        if [ -n "$merge_head" ] && [ "$merge_head" = "$tested" ]; then
            git merge --abort >> "$log_file" 2>&1 || true
        fi
        SETTLE_OUTCOME="nothing-merged"
    else
        if ! merge_is_the_loops_own "$pre" "$tested" "$head"; then
            SETTLE_REASON="the merge of $item was interrupted and $target holds commits this loop did not make; push or reset by hand, then delete $MERGE_JOURNAL"
            return 1
        fi
        if git merge-base --is-ancestor "$head" "refs/remotes/origin/$target" 2>/dev/null; then
            landed="yes"
        elif [ "$phase" != "push" ]; then
            landed="no"
        elif [ "$mode" = "online" ]; then
            if with_timeout "$NET_TIMEOUT_SEC" git fetch origin "+refs/heads/$target:refs/remotes/origin/$target" >> "$log_file" 2>&1; then
                landed="no"
                if git merge-base --is-ancestor "$head" "refs/remotes/origin/$target" 2>/dev/null; then
                    landed="yes"
                fi
            fi
        fi
        case "$landed" in
            yes)
                SETTLE_OUTCOME="pushed" ;;
            no)
                if ! take_back_merge "$item" "$target" "$pre" "$tested" "$log_file"; then
                    SETTLE_REASON="the unpushed merge of $item could not be taken back: $TAKE_BACK_REASON"
                    return 1
                fi
                SETTLE_OUTCOME="taken-back" ;;
            *)
                SETTLE_REASON="the push of $item was interrupted and the remote has not said whether it has it"
                return 1 ;;
        esac
    fi
    echo "Interrupted merge of $item settled: $SETTLE_OUTCOME" | tee -a "$log_file"
    pop_premerge_stash "$stash_tag" "$log_file"
    rm -f "$MERGE_JOURNAL"
    return 0
}

# ---------------------------------------------------------------------------
# Crash surfacing + zombie sweep
# ---------------------------------------------------------------------------
# Problem: `set -euo pipefail` + launchd `KeepAlive=true` + nothing catching
# unbound-var crashes = silent respawn forever. A single unset variable in the
# draft-PR path (`duration=$(( t_end - t_start ))`) once caused 35 respawns in
# 46 min, left 72 zombie executor_runs rows, and masked the configured
# interval_minutes entirely.
#
# CRITICAL bash quirk: `set -u` unbound-variable errors DO NOT fire ERR trap.
# Bash errors-out before command dispatch, the script exits with rc=0, and
# the EXIT trap sees $? = 0. That's why this implementation uses a
# breadcrumb flag (`CLEAN_EXIT`) rather than ERR or $? to classify exits.
#
# Three-layer defense:
#   1. `on_exit` (trap EXIT): always fires. If CLEAN_EXIT != 1, it was a
#      crash (unbound var, `set -e` non-zero, pipefail, SIGKILL w/o SIGTERM).
#      Writes timestamp line to $CRASH_LOG + best-effort "crashed" heartbeat.
#      Also does the existing release_lock + cleanup_worktrees.
#   2. `check_crash_loop`: on startup, if $CRASH_LOG has >=CRASH_RATE_LIMIT
#      entries in the last CRASH_RATE_WINDOW_SEC, it writes a "CRASH-LOOP"
#      marker to $CRASH_LOG, then sleeps for the whole window before the
#      script continues, so a broken code path is retried at most once per
#      window instead of in a tight loop. It does not exit: the launchd plist
#      uses KeepAlive = true, which respawns regardless, so sleeping inside
#      the process is the only brake available from within the script.
#   3. `sweep_zombie_runs`: POST /ops/api/v1/executor/sweep-zombies right
#      after acquire_lock so the DB reflects reality after each crash.
#
# CLEAN_EXIT is set to 1 at three sanctioned points: the TERM/INT trap
# (launchctl unload), just before `break` in RUN_ONCE mode, and when
# acquire_lock finds another live executor. Anything else that exits the
# script, including normal `exit 1` branches we've already coded: will log a
# crash, which is the conservative default.

on_exit() {
    local rc=$?
    if [ "${CLEAN_EXIT:-0}" != "1" ]; then
        local ts epoch
        ts=$(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date)
        epoch=$(date +%s)
        {
            echo "$epoch ts=$ts pid=$$ exit=$rc phase=${ITERATION_PHASE:-unknown} last_cmd=$(printf '%q' "${BASH_COMMAND:-?}")"
        } >> "$CRASH_LOG" 2>/dev/null || true
        if command -v curl >/dev/null 2>&1 && [ -n "$(get_ops_key)" ]; then
            OPS_TIMEOUT_SEC=3 ops_curl -X POST \
                -H "Content-Type: application/json" \
                "$OPS_BASE/ops/api/v1/executor/heartbeat" \
                -d "{\"status\":\"crashed\",\"skipped_reason\":\"exit=$rc phase=${ITERATION_PHASE:-unknown}\",\"timestamp\":\"$(date -Iseconds 2>/dev/null || date)\"}" \
                >/dev/null 2>&1 || true
        fi
    fi
    if [ "${LOCK_HELD:-0}" = "1" ]; then
        # Stop what is still running (SIGTERM from launchctl, or a crash). A
        # Claude session would otherwise keep editing a worktree that is about
        # to be deleted and keep spending against its budget; a push would
        # keep going after its merge has been taken back. Jobs are ended as
        # process groups. Items running as background subshells get SIGTERM
        # alone: each one exits when the command it is in the middle of
        # returns, so a git command in the merge section is never cut off.
        local record job_pids="" child waited=0
        for record in "$STATE_DIR"/cas-exec-job-$$-*; do
            [ -f "$record" ] || continue
            job_pids="$job_pids ${record##*-}"
            rm -f "$record"
        done
        # shellcheck disable=SC2086  # one argument per process id
        stop_jobs $job_pids
        for child in $ITEM_PIDS $(pgrep -P $$ 2>/dev/null || true); do
            kill -s TERM "$child" 2>/dev/null || true
        done
        while [ -n "$ITEM_PIDS" ] && [ "$waited" -lt 50 ] && jobs_running "" "$ITEM_PIDS"; do
            short_sleep 0.2
            waited=$((waited + 1))
        done
        # An interrupted merge section is settled here when that needs no
        # network and no item is still at work in the checkout; otherwise the
        # journal stays and the next start settles it.
        if [ -n "$ITEM_PIDS" ] && jobs_running "" "$ITEM_PIDS"; then
            echo "[exit] an item is still running; an interrupted merge, if any, is left for the next start" >> "$LOG_DIR/dispatcher.log"
        elif ! settle_interrupted_merge offline "$LOG_DIR/dispatcher.log"; then
            echo "[exit] $SETTLE_REASON; the next start settles it" | tee -a "$LOG_DIR/dispatcher.log"
        fi
        release_lock
        cleanup_worktrees
    fi
}

# Always returns 0. If the crash rate is at the cap, writes a prominent marker
# to $CRASH_LOG and sleeps for one full window (see the layer-2 note above).
# CLEAN_EXIT is set to 1 around the sleep so that a SIGTERM during the back-off
# is not logged as another crash.
check_crash_loop() {
    [ -f "$CRASH_LOG" ] || return 0
    local cutoff now count
    now=$(date +%s)
    cutoff=$((now - CRASH_RATE_WINDOW_SEC))
    count=$(awk -v c="$cutoff" '$1 >= c' "$CRASH_LOG" 2>/dev/null | wc -l | tr -d ' ')
    count=${count:-0}
    if [ "$count" -ge "$CRASH_RATE_LIMIT" ]; then
        local ts epoch
        ts=$(date '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || date)
        epoch=$(date +%s)
        # Loud marker that dashboards + `grep CRASH-LOOP crash.log` can catch.
        echo "$epoch CRASH-LOOP DETECTED: count=$count window_sec=$CRASH_RATE_WINDOW_SEC limit=$CRASH_RATE_LIMIT ts=$ts" >> "$CRASH_LOG"
        echo "[$ts] CRASH-LOOP DETECTED: $count crashes in last ${CRASH_RATE_WINDOW_SEC}s (limit=$CRASH_RATE_LIMIT)" >&2
        echo "[$ts] Tail of $CRASH_LOG:" >&2
        tail -n "$CRASH_RATE_LIMIT" "$CRASH_LOG" >&2 2>/dev/null || true
        echo "[$ts] Backing off: sleeping ${CRASH_RATE_WINDOW_SEC}s before starting." >&2
        echo "[$ts] Fix the cause and truncate crash.log, or \`scripts/cas-exec stop\`." >&2
        # Back off for a full window so we don't tight-loop over a broken code path.
        CLEAN_EXIT=1
        sleep "$CRASH_RATE_WINDOW_SEC" || true
        CLEAN_EXIT=0
    fi
    return 0
}

# POST to /ops/api/v1/executor/sweep-zombies to mark stuck running/working/
# pr_created rows as failed. Idempotent (backend filters on age). Best-effort.
sweep_zombie_runs() {
    [ -z "$(get_ops_key)" ] && return 0
    local resp
    resp=$(ops_curl -X POST \
        -H "Content-Type: application/json" \
        "$OPS_BASE/ops/api/v1/executor/sweep-zombies" \
        -d "{\"max_age_minutes\": $ZOMBIE_SWEEP_MAX_AGE_MIN}" 2>/dev/null || echo "")
    if [ -n "$resp" ]; then
        echo "Zombie sweep response: $resp"
    fi
}

# Install the unified EXIT trap (replaces the earlier `release_lock;
# cleanup_worktrees` trap: on_exit does both plus crash logging).
trap on_exit EXIT

# POST heartbeat to ops API. The payload holds the four fields the heartbeat
# route stores (tests/test_executor_ops_contract.py compares the two).
post_heartbeat() {
    local status="$1" item="${2:-}" reason="${3:-}"
    local payload
    payload=$(_json_payload \
        timestamp "$(date -Iseconds)" s \
        status "$status" s \
        current_item "$item" s \
        skipped_reason "$reason" s \
    ) || { echo "[warn] heartbeat JSON build failed" >&2; return; }
    ops_curl -X POST -H "Content-Type: application/json" \
        "$OPS_BASE/ops/api/v1/executor/heartbeat" \
        -d "$payload" \
        2>/dev/null || echo "[warn] heartbeat post failed" >&2
}

# POST result to ops API
post_result() {
    local item_id="$1" title="$2" status="$3" result="$4" cost="${5:-0}" commit="${6:-}" duration="${7:-0}" error="${8:-}"
    local payload
    payload=$(_json_payload \
        roadmap_item_id "$item_id" s \
        roadmap_item_title "$title" s \
        status "$status" s \
        result "$result" s \
        cost_usd "$cost" f \
        commit_sha "$commit" s \
        duration_s "$duration" i \
        error_message "$error" s \
    ) || { echo "[warn] result JSON build failed for $item_id" >&2; return; }
    ops_curl -X POST -H "Content-Type: application/json" \
        "$OPS_BASE/ops/api/v1/executor/result" \
        -d "$payload" \
        2>/dev/null || echo "[warn] result post failed for $item_id" >&2
}

# Track item failures (skip items that fail too many times)
record_failure() {
    local item_id="$1"
    local today
    today=$(date +%Y-%m-%d)
    echo "$today $item_id" >> "$FAIL_TRACKER"
}

get_failure_count() {
    local item_id="$1"
    local today
    today=$(date +%Y-%m-%d)
    local count
    count=$(grep -c "$today $item_id" "$FAIL_TRACKER" 2>/dev/null) || true
    sanitize_int "${count:-0}"
}

# Count total failures across ALL days (for auto-defer decisions)
get_total_failure_count() {
    local item_id="$1"
    local count
    count=$(grep -c " $item_id$" "$FAIL_TRACKER" 2>/dev/null) || true
    sanitize_int "${count:-0}"
}

# Set one item's status in coordinator/roadmap.json and commit exactly that
# change on $TARGET_BRANCH.
#   $1 item id, $2 status, $3 log file, $4 commit subject,
#   $5 and $6: an optional note key and value (SETTABLE_NOTE_KEYS in the picker).
# This is the only commit the loop makes in this checkout besides the merge.
# roadmap.json, the executor config and the failure KB are files the merge
# guard keeps a branch from changing, and a session can still write them here
# directly (it is not sandboxed), so the loop never commits "whatever is in the
# file":
#   - it writes nothing when this checkout is not on $TARGET_BRANCH, or when
#     roadmap.json already differs from HEAD (somebody else's edit, which is
#     not the loop's to commit);
#   - after the picker has written the status, the picker compares the file
#     with HEAD's copy (--verify-transition): that one item, its status and the
#     one note field, nothing else;
#   - the commit names the one path and goes through the pre-commit hook.
# Returns 0 when the change is committed; 1 when the roadmap is as it was (it
# was not written, or the commit was refused and the change was taken back);
# 2 when the file was written and is not exactly the one change: it is left
# for a person to look at, a copy goes to the log directory, and preflight
# yields while roadmap.json differs from HEAD.
# What this does not exclude: a process that rewrites the file between the
# comparison and the commit.
# The caller holds the merge lock (see record_roadmap_status_locked).
#
# SIGTERM and SIGINT are held from the first step to the last. The write, the
# comparison and the commit are separate commands, and a loop that stopped
# between two of them would leave roadmap.json changed and uncommitted: every
# later cycle would then yield on it until a person stepped in. A kill that
# cannot be caught in that fraction of a second still leaves exactly that
# (inside a merge section the journal lets the next cycle put the file back;
# outside one, a person has to commit or discard the change).
record_roadmap_status() {
    local rc=0
    hold_signals
    write_roadmap_status "$@" || rc=$?
    release_signals
    return "$rc"
}

write_roadmap_status() {
    local item_id="$1" status="$2" log_file="$3" subject="$4" note_key="${5:-}" note_value="${6:-}"
    local roadmap="coordinator/roadmap.json" dirty before set_rc=0 verify_rc=0
    cd "$PROJECT_DIR" || return 1
    if [ "$(git branch --show-current 2>/dev/null || true)" != "$TARGET_BRANCH" ]; then
        echo "Roadmap: $item_id not marked $status: this checkout is not on $TARGET_BRANCH" | tee -a "$log_file"
        return 1
    fi
    if ! dirty=$(git status --porcelain -- "$roadmap" 2>>"$log_file") || [ -n "$dirty" ]; then
        echo "Roadmap: $item_id not marked $status: $roadmap has uncommitted changes that this loop did not make" | tee -a "$log_file"
        return 1
    fi
    before="$STATE_DIR/cas-exec-roadmap-head-$$-$item_id.json"
    if ! git show "HEAD:$roadmap" > "$before" 2>>"$log_file"; then
        rm -f "$before"
        echo "Roadmap: $item_id not marked $status: could not read HEAD's $roadmap" | tee -a "$log_file"
        return 1
    fi
    if [ -n "$note_key" ]; then
        python3 "$SCRIPT_DIR/cas-pick-item.py" --set-status "$item_id" "$status" \
            --note "$note_key" "$note_value" >> "$log_file" 2>&1 || set_rc=$?
    else
        python3 "$SCRIPT_DIR/cas-pick-item.py" --set-status "$item_id" "$status" >> "$log_file" 2>&1 || set_rc=$?
    fi
    if [ "$set_rc" -ne 0 ]; then
        rm -f "$before"
        echo "Roadmap: $item_id not marked $status: the picker could not write it (exit $set_rc)" | tee -a "$log_file"
        return 1
    fi
    if [ -n "$note_key" ]; then
        python3 "$SCRIPT_DIR/cas-pick-item.py" --verify-transition "$before" "$item_id" "$status" \
            --note "$note_key" "$note_value" >> "$log_file" 2>&1 || verify_rc=$?
    else
        python3 "$SCRIPT_DIR/cas-pick-item.py" --verify-transition "$before" "$item_id" "$status" >> "$log_file" 2>&1 || verify_rc=$?
    fi
    if [ "$verify_rc" -ne 0 ]; then
        cp "$roadmap" "$LOG_DIR/roadmap-not-committed-$item_id-$(date +%s).json" 2>/dev/null || true
        rm -f "$before"
        echo "Roadmap: $roadmap is not HEAD plus the status change for $item_id; NOT committing it (a copy is in $LOG_DIR)" | tee -a "$log_file"
        return 2
    fi
    if git diff --quiet -- "$roadmap" 2>>"$log_file"; then
        # The item already had this status and note in HEAD: nothing to commit.
        rm -f "$before"
        return 0
    fi
    if git commit -q -m "$subject" -- "$roadmap" >> "$log_file" 2>&1; then
        rm -f "$before"
        return 0
    fi
    # The commit was refused (the pre-commit hook, an index lock, a merge in
    # progress in this checkout). The file was just shown to be HEAD plus this
    # one change, so putting HEAD's copy back loses nothing else, and the
    # checkout is not left with a change that would make every later cycle yield.
    git reset -q -- "$roadmap" >> "$log_file" 2>&1 || true
    cp "$before" "$roadmap" 2>>"$log_file" || true
    rm -f "$before"
    echo "Roadmap: the commit marking $item_id $status was refused; the change was taken back" | tee -a "$log_file"
    return 1
}

# record_roadmap_status for callers outside the merge critical section: takes
# the merge lock around it, so the commit cannot land in the middle of another
# item's merge (whose failed push resets the branch) and two parallel items
# cannot write roadmap.json at once.
record_roadmap_status_locked() {
    local rc=0
    acquire_merge_lock
    record_roadmap_status "$@" || rc=$?
    release_merge_lock
    return "$rc"
}

# Auto-defer an item in roadmap.json after too many total failures. When the
# status cannot be recorded the item is still not picked again while those
# failures are in the tracker (entries are kept seven days): the picker skips
# an item whose tracked failures have reached the same threshold.
auto_defer_item() {
    local item_id="$1" reason="$2"
    if record_roadmap_status_locked "$item_id" deferred "$LOG_DIR/dispatcher.log" \
            "[auto] Mark $item_id deferred in roadmap" deferred_reason "$reason"; then
        echo "[auto-defer] $item_id: $reason" | tee -a "$LOG_DIR/dispatcher.log"
    else
        echo "[auto-defer] $item_id: $reason (not recorded in roadmap.json, see above)" | tee -a "$LOG_DIR/dispatcher.log"
    fi
}

# Clean up any leftover session (auto-*) and test-gate (gate-*) worktrees and
# orphaned auto/* branches
cleanup_worktrees() {
    cd "$PROJECT_DIR" 2>/dev/null || return
    for wt in "$WORKTREE_DIR"/auto-* "$WORKTREE_DIR"/gate-*; do
        [ -d "$wt" ] || continue
        git worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
    done
    git worktree prune 2>/dev/null || true

    # Delete orphaned auto/* branches not checked out in any worktree
    local active_branches
    active_branches=$(git worktree list --porcelain 2>/dev/null | grep '^branch ' | sed 's|^branch refs/heads/||' || true)
    local b
    for b in $(git branch --list 'auto/*' 2>/dev/null | sed 's/^[* ]*//' || true); do
        if ! echo "$active_branches" | grep -qx "$b"; then
            git branch -D "$b" 2>/dev/null || true
        fi
    done
}

# Pre-flight gates (shared checks, run once per cycle)
preflight_check() {
    # 0. An interrupted merge section comes first, before the config is even
    # read: the operator's edit of the config may be sitting in the pre-merge
    # stash, and with it the "enabled" this cycle is about to look at.
    if ! settle_interrupted_merge online "$LOG_DIR/dispatcher.log"; then
        echo "[preflight] $SETTLE_REASON; no session is started this cycle"
        post_heartbeat "idle" "" "interrupted merge not settled: $SETTLE_REASON"
        return 1
    fi

    # Batch-read config (one python process instead of six). The picker checks
    # each value; what it says about a config it refuses goes to the heartbeat.
    local config_output
    if ! config_output=$(read_all_config 2>&1); then
        config_output=$(printf '%s' "$config_output" | tail -n 1 | cut -c1-200)
        echo "[preflight] the config cannot be used: $config_output"
        post_heartbeat "idle" "" "config read failed: $config_output"
        return 1
    fi
    local enabled pause_state max_items ceiling interval max_par max_budget max_total_failures skip_complex target_branch tiered_gov
    enabled=$(echo "$config_output" | sed -n '1p')
    pause_state=$(echo "$config_output" | sed -n '2p')
    max_items=$(echo "$config_output" | sed -n '3p')
    ceiling=$(echo "$config_output" | sed -n '4p')
    interval=$(echo "$config_output" | sed -n '5p')
    max_par=$(echo "$config_output" | sed -n '6p')
    max_budget=$(echo "$config_output" | sed -n '7p')
    max_total_failures=$(echo "$config_output" | sed -n '8p')
    skip_complex=$(echo "$config_output" | sed -n '9p')
    target_branch=$(echo "$config_output" | sed -n '10p')
    tiered_gov=$(echo "$config_output" | sed -n '11p')

    # Export for use in main loop and merge function
    EXEC_INTERVAL="$interval"
    EXEC_MAX_PARALLEL="$max_par"
    EXEC_MAX_BUDGET="$max_budget"
    EXEC_MAX_TOTAL_FAILURES="$max_total_failures"
    TARGET_BRANCH="$target_branch"
    TIERED_GOVERNANCE="$tiered_gov"

    # 1. Config enabled? The picker prints True for a JSON true and for
    # nothing else.
    [ "$enabled" = "True" ] || { post_heartbeat "paused"; return 1; }

    # 2. Pause until? The picker prints "active" when no pause is in force;
    # a pause_until it cannot read as a time counts as a pause.
    if [ "$pause_state" != "active" ]; then
        post_heartbeat "paused" "" "$pause_state"
        return 1
    fi

    # 3. Git repo: yield to active human session on feature branches; do NOT force-checkout master.
    # Rationale: a force-checkout hijacks any active Claude Code or human terminal session working
    # on a feature branch in PROJECT_DIR. Instead, if not on master, skip this tick entirely.
    # The executor will resume automatically once the human commits and returns to master.
    cd "$PROJECT_DIR"
    local current_branch
    current_branch=$(git branch --show-current 2>/dev/null)
    if [ "$current_branch" != "$TARGET_BRANCH" ]; then
        post_heartbeat "idle" "" "project dir on branch '$current_branch', yielding (want $TARGET_BRANCH)"
        return 1
    fi

    # roadmap.json must equal HEAD's copy. The loop commits its own status
    # changes when it makes them (record_roadmap_status) and commits nothing
    # else here, so a difference is somebody else's edit: items or statuses that
    # no commit records. The picker would act on them, and a session can write
    # this file (it is not sandboxed), so the cycle yields until a person has
    # committed or discarded the change.
    local roadmap_dirty
    if ! roadmap_dirty=$(git status --porcelain -- coordinator/roadmap.json 2>/dev/null) || [ -n "$roadmap_dirty" ]; then
        post_heartbeat "idle" "" "coordinator/roadmap.json has uncommitted changes, yielding until a person commits or discards them"
        return 1
    fi

    # Unmerged paths, whatever their name. A stash pop that conflicted leaves one
    # (status UU, in a file DIRTY_OK_RE tolerates), and git refuses the next
    # stash and the next merge until a person resolves it. The check comes
    # before the DIRTY_OK_RE filter below, which would otherwise pass the line.
    # An index that cannot be read yields too.
    local unmerged
    if ! unmerged=$(unmerged_paths); then
        post_heartbeat "idle" "" "the index cannot be read; yielding"
        return 1
    fi
    if [ -n "$unmerged" ]; then
        post_heartbeat "idle" "" "unmerged paths ($(echo "$unmerged" | tr '\n' ' ' | cut -c1-200)): a stash pop conflicted or a merge is unfinished; yielding until they are resolved"
        return 1
    fi

    # Other uncommitted changes to tracked files: yield the tick instead of auto-stashing.
    # Rationale: auto-stash silently reverts edits from active Claude Code or human terminal
    # sessions working on master. Better to skip the tick and let the human commit when ready.
    # The executor will resume automatically on the next cycle once the working tree is clean.
    # The files in DIRTY_OK_RE are tolerated, and never committed by the loop.
    local other_dirty
    other_dirty=$(git status --porcelain 2>/dev/null | grep -vE "$DIRTY_OK_RE" | grep -v '^??' || true)
    if [ -n "$other_dirty" ]; then
        local dirty_count
        dirty_count=$(echo "$other_dirty" | wc -l | tr -d ' ')
        post_heartbeat "idle" "" "dirty tree ($dirty_count files), yielding to active session"
        return 1
    fi

    # 4. Daily item limit?
    # The counter counts Claude sessions STARTED today (see increment_daily_count
    # in execute_item), not merges, so failing attempts cannot run past the cap.
    local today stored_date count
    today=$(date +%Y-%m-%d)
    stored_date=$(head -1 "$DAILY_COUNTER_FILE" 2>/dev/null || echo "")
    count=0
    if [ "$stored_date" = "$today" ]; then
        count=$(tail -1 "$DAILY_COUNTER_FILE" 2>/dev/null | tr -d '[:space:]' || true)
        count="${count:-0}"
    else
        printf '%s\n0\n' "$today" > "$DAILY_COUNTER_FILE"
    fi
    case "$count" in ''|*[!0-9]*) count=0 ;; esac
    case "$max_items" in ''|*[!0-9]*) post_heartbeat "idle" "" "bad max_items_per_day in config"; return 1 ;; esac
    if [ "$count" -ge "$max_items" ]; then
        post_heartbeat "idle" "" "daily limit reached ($count/$max_items)"
        return 1
    fi
    EXEC_DAILY_REMAINING=$((max_items - count))

    # 5. Can the build check be answered? FAILS CLOSED. execute_item pushes
    # only after the ops API has reported zero builds queued or in progress
    # (wait_for_builds_clear), so a cycle that cannot read those counts would
    # spend a session whose result could not be merged.
    local active_now
    active_now=$(active_build_count)
    case "$active_now" in
        ''|*[!0-9]*)
            echo "[preflight] the ops API at $OPS_BASE did not report build counts; no session is started this cycle"
            post_heartbeat "idle" "" "build counts unavailable from the ops API"
            return 1 ;;
    esac

    # 6. Daily spend ceiling? FAILS CLOSED: only a complete cost report with a
    # plausible total under the ceiling lets a session start (spend_verdict).
    # The gates that do not depend on the report are max_items_per_day (Claude
    # sessions started), --max-budget-usd per session, the 20 minute timeout
    # and the per-item failure caps.
    local spend spend_verdict daily_total
    spend=$(spend_verdict "$ceiling")
    spend_verdict="${spend%% *}"
    daily_total="${spend#* }"
    if [ "$spend_verdict" = "over" ]; then
        post_heartbeat "idle" "" "daily spend ceiling (\$$daily_total >= \$$ceiling)"
        return 1
    fi
    if [ "$spend_verdict" != "under" ]; then
        echo "[preflight] today's spend could not be read from the ops API ($daily_total); no session is started this cycle"
        post_heartbeat "idle" "" "spend unreadable from the ops API: $daily_total"
        return 1
    fi

    return 0
}

# Print "under FIGURE", "over FIGURE" or "unknown REASON" for today's spend
# against the ceiling $1, from GET /ops/api/v1/costs. Always returns 0; the
# caller reads the first word. "under" is printed only when all of this holds:
#   - the API answered within the time and size limits, with a JSON object
#     (NaN and Infinity are not JSON and are refused as such);
#   - platform_costs_partial is false. The route sets it to true when its
#     platform figures are a lower bound: the Anthropic cost API was not
#     configured, failed, or was read in part. A lower bound under the ceiling
#     says nothing about the spend;
#   - summary.today_total is a number (not a string, not a boolean), finite
#     and not negative;
#   - the ceiling is a finite number that is not negative.
# Anything else is "unknown", and no session is started. The figure is the
# platform's spend today as the web app reports it, which includes more than
# this loop's own sessions.
spend_verdict() {
    local report
    if ! report=$(ops_curl "$OPS_BASE/ops/api/v1/costs" 2>/dev/null); then
        echo "unknown no answer from the ops API"
        return 0
    fi
    printf '%s' "$report" | python3 -c '
import json, math, sys

class Refused(Exception):
    pass

def not_json(name):
    raise Refused("the cost report is not JSON")

def dollars(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)

try:
    try:
        ceiling = float(sys.argv[1])
    except ValueError:
        ceiling = None
    if not dollars(ceiling):
        raise Refused("the ceiling is not a usable number")
    try:
        report = json.loads(sys.stdin.buffer.read(), parse_constant=not_json)
    except ValueError:
        raise Refused("the cost report is not JSON")
    if not isinstance(report, dict):
        raise Refused("the cost report is not an object")
    if report.get("platform_costs_partial") is not False:
        raise Refused("the cost report is partial")
    summary = report.get("summary")
    total = summary.get("today_total") if isinstance(summary, dict) else None
    if not dollars(total):
        raise Refused("today_total is not a plausible amount")
    print("over" if total >= ceiling else "under", total)
except Refused as problem:
    print("unknown", problem)
' "$1" 2>/dev/null || echo "unknown the cost report could not be read"
}

# Print how many builds are queued or in progress (ACTIVE_BUILD_STATUSES), or
# ERR when that cannot be established: no answer within the time and size
# limits, an HTTP error, or JSON without every one of the counts as a whole
# number that is not negative (a JSON integer, or a string of digits, which is
# how node-postgres returns COUNT(*)). Reads the per-status counts that
# GET /ops/api/v1/builds returns under "stats" (the dashboard summary at
# /ops/api/v1/stats reports queued and building only). Always returns 0; the
# caller tells a number from ERR.
active_build_count() {
    local body
    if ! body=$(ops_curl "$OPS_BASE/ops/api/v1/builds?limit=1" 2>/dev/null); then
        echo "ERR"
        return 0
    fi
    # shellcheck disable=SC2086  # the status names are separate arguments on purpose
    printf '%s' "$body" | python3 -c '
import json, sys

def count(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("not a count")

try:
    stats = json.load(sys.stdin)["stats"]
    counts = [count(stats[name]) for name in sys.argv[1:]]
    if not counts:
        raise ValueError("no counts asked for")
    print(sum(counts))
except Exception:
    print("ERR")
' $ACTIVE_BUILD_STATUSES 2>/dev/null || echo "ERR"
}

# Increment the daily counter: called once per Claude session started.
increment_daily_count() {
    local today count
    today=$(date +%Y-%m-%d)
    count=0
    if [ "$(head -1 "$DAILY_COUNTER_FILE" 2>/dev/null)" = "$today" ]; then
        count=$(tail -1 "$DAILY_COUNTER_FILE" 2>/dev/null | tr -d '[:space:]' || true)
        count="${count:-0}"
    fi
    count=$((count + 1))
    printf '%s\n%s\n' "$today" "$count" > "$DAILY_COUNTER_FILE"
}

# Wait until no customer build is queued or in progress, then return 0: the
# caller may push. FAILS CLOSED. Returns 1 when builds were still active after
# 45 one-minute waits, and 2 when the counts could not be read three times in a
# row (see active_build_count); the caller pushes in neither case. The push
# this guards redeploys the worker (see ACTIVE_BUILD_STATUSES), so "cannot
# tell" is not a reason to go ahead.
wait_for_builds_clear() {
    local log_file="$1"
    local push_wait=0 unreadable=0 building
    while true; do
        building=$(active_build_count | tr -d '[:space:]')
        case "$building" in
            ''|*[!0-9]*)
                unreadable=$((unreadable + 1))
                if [ "$unreadable" -ge 3 ]; then
                    echo "The ops API did not report build counts in 3 attempts: not pushing" | tee -a "$log_file"
                    return 2
                fi
                echo "Could not read build counts from the ops API (attempt $unreadable/3), retrying in 10s..." | tee -a "$log_file"
                sleep 10
                continue ;;
        esac
        unreadable=0
        if [ "$building" -eq 0 ]; then
            return 0
        fi
        push_wait=$((push_wait + 1))
        if [ "$push_wait" -ge 45 ]; then
            echo "Active builds ($building) still queued or running after 45 min: aborting item (will retry next cycle)" | tee -a "$log_file"
            return 1
        fi
        echo "Active builds ($building): waiting to push (${push_wait}/45 min)..." | tee -a "$log_file"
        sleep 60
    done
}

# Acquire the merge lock (only one item merges at a time). Waits for a running
# owner. The owner may legitimately wait up to 45 min for active builds to
# clear (wait_for_builds_clear) before merging, so the lock is forced only
# after 70 min; a lock whose owner has died is taken at once (try_lock).
acquire_merge_lock() {
    local attempts=0
    until try_lock "$MERGE_LOCK"; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 420 ]; then
            echo "Merge lock held for 70 min, forcing"
            break_lock "$MERGE_LOCK" force
            attempts=0
            continue
        fi
        sleep 10
    done
}

release_merge_lock() { unlock "$MERGE_LOCK"; }

# Leave the merge section (see MERGE_JOURNAL): pop the pre-merge stash $1,
# remove the journal and release the merge lock. $2 is the log file. With a
# commit in $3, HEAD is first compared with it: the caller has just put the
# target branch back there, and if that did not take, the journal and the
# stash stay as they are for the next cycle to settle.
close_merge_section() {
    local stash_tag="$1" log_file="$2" expected_head="${3:-}"
    if [ -n "$expected_head" ] \
        && [ "$(git -C "$PROJECT_DIR" rev-parse --verify -q "HEAD^{commit}" 2>/dev/null || true)" != "$expected_head" ]; then
        echo "$TARGET_BRANCH is not back at $expected_head; the next cycle settles it" | tee -a "$log_file"
        release_merge_lock
        return 0
    fi
    pop_premerge_stash "$stash_tag" "$log_file"
    rm -f "$MERGE_JOURNAL"
    release_merge_lock
}

# The merge of item $1 cannot be taken back (take_back_merge said why in
# TAKE_BACK_REASON): say so in the log, drop the item's worktree and branch (the
# merge on $TARGET_BRANCH holds its commit) and release the merge lock. The
# merge, the journal and the pre-merge stash stay where they are. The next
# cycle's settle routine takes the merge back once that is safe, and starts no
# session before it has. $2 is the log file, $3 the worktree and $4 the branch.
leave_merge_unsettled() {
    local item_id="$1" log_file="$2" worktree_path="$3" branch="$4"
    echo "NOT TAKEN BACK: the unpushed merge of $item_id stays on $TARGET_BRANCH ($TAKE_BACK_REASON). Nothing was pushed. The journal $MERGE_JOURNAL and the pre-merge stash stay; the next cycle settles them and starts no session until it can" | tee -a "$log_file"
    git -C "$PROJECT_DIR" worktree remove --force "$worktree_path" 2>/dev/null || true
    git -C "$PROJECT_DIR" branch -D "$branch" 2>/dev/null || true
    release_merge_lock
}

# Serialize git worktree creation (prevents .git/config lock contention in
# parallel runs). Forced after 60 seconds.
acquire_git_setup_lock() {
    local attempts=0
    until try_lock "$GIT_SETUP_LOCK"; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 30 ]; then
            echo "Git setup lock held for 60s, forcing"
            break_lock "$GIT_SETUP_LOCK" force
            attempts=0
            continue
        fi
        sleep 2
    done
}

release_git_setup_lock() { unlock "$GIT_SETUP_LOCK"; }

# Retry a command with exponential backoff (for transient network failures)
retry() {
    local max_attempts="$1" delay="$2"
    shift 2
    local attempt=1
    while [ "$attempt" -le "$max_attempts" ]; do
        if "$@"; then return 0; fi
        echo "[retry] Attempt $attempt/$max_attempts failed: $*" >&2
        [ "$attempt" -lt "$max_attempts" ] && sleep "$delay"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
    return 1
}

# The test gate: run the web and Python suites on commit $1 and return 0 only
# when both pass. $2 is the commit the branch forked from, $3 the directory to
# check the commit out into (created and removed here), $4 the log file.
# The Python suites are every directory in GATE_PYTEST_DIRS (tests/ and
# compose/tests); the v2 widget tests among them render EJS from
# web/node_modules, which is in the checkout by then (linked, or installed by
# the npm step). Each of the two test commands is ended, with everything it
# started, when it runs longer than GATE_TIMEOUT_SEC, and the gate then fails.
#
# What each command would otherwise read or run from the session's worktree
# before the first test, and how it is kept out:
#   - Anything the session left in its worktree that is not in the commit
#     (ignored files such as a stale cache or a node_modules directory, a file
#     a .gitignore edit hid from `git status`, an edit to a tracked file hidden
#     with `git update-index --skip-worktree`): the tests do not run there.
#     They run on a fresh detached checkout of the commit, which holds the
#     commit's files and the node_modules link made below, nothing else.
#   - npm: the test command, lifecycle scripts, the dependency set, .npmrc and
#     node_modules contents come from the commit, where the merge guard has
#     already refused any change to them (PROTECTED_PATHS in the guard). npm is given no
#     flags to override them: with those files pinned to the target branch,
#     `npm install` and `npm test` run the target branch's commands.
#   - python3 -m pytest: -m puts the working directory first on sys.path, so a
#     top-level pytest.py (or pluggy.py, or a *.dist-info with a plugin entry
#     point) would be imported in place of the installed one. -P keeps the
#     working directory off sys.path until pytest itself is running; PYTHONPATH
#     is unset for the same reason.
#   - pytest configuration: pytest searches upward from a test directory for
#     a configuration file and would find one planted above the worktree. -c
#     names the file to use (the target branch's pytest.ini when it has one,
#     otherwise an empty one written here; a configuration kept in
#     pyproject.toml, tox.ini or setup.cfg is not read by the gate), --rootdir
#     and --confcutdir pin the root, and PYTEST_ADDOPTS is unset.
#   - pytest plugins: --disable-plugin-autoload loads no installed plugin (the
#     suite needs none), PYTEST_PLUGINS is unset, and no conftest.py names one.
#     -p no:cacheprovider keeps pytest from reading or writing a cache.
# What is left is what the guard covers by path (conftest.py, top-level Python
# modules and package inits, which pytest imports once it has put the root on
# sys.path) and the tests themselves.
run_test_gate() {
    local sha="$1" base="$2" gate_path="$3" log_file="$4"
    local ok=1 gate_ini

    acquire_git_setup_lock
    git -C "$PROJECT_DIR" worktree remove --force "$gate_path" >/dev/null 2>&1 || true
    if ! git -C "$PROJECT_DIR" worktree add --detach "$gate_path" "$sha" >> "$log_file" 2>&1; then
        release_git_setup_lock
        echo "Could not check out $sha for the test gate" | tee -a "$log_file"
        return 1
    fi
    release_git_setup_lock
    if [ "$(git -C "$gate_path" rev-parse HEAD 2>/dev/null || true)" != "$sha" ]; then
        echo "The test gate checkout is not at $sha" | tee -a "$log_file"
        ok=0
    fi
    if [ -d "$PROJECT_DIR/web/node_modules" ]; then
        ln -s "$PROJECT_DIR/web/node_modules" "$gate_path/web/node_modules" 2>/dev/null || true
    fi

    # npm install and npm test are SEPARATE steps
    if [ "$ok" -eq 1 ] && ! with_timeout "$NET_TIMEOUT_SEC" gate_npm_install "$gate_path" >> "$log_file" 2>&1; then
        echo "npm install failed" | tee -a "$log_file"
        ok=0
    fi
    if [ "$ok" -eq 1 ] && ! with_timeout "$GATE_TIMEOUT_SEC" gate_npm_test "$gate_path" >> "$log_file" 2>&1; then
        echo "Web tests failed" | tee -a "$log_file"
        ok=0
    fi

    if [ "$ok" -eq 1 ]; then
        gate_ini="$STATE_DIR/cas-exec-pytest-$$-$(basename "$gate_path").ini"
        if ! git -C "$gate_path" show "$base:pytest.ini" > "$gate_ini" 2>/dev/null; then
            printf '[pytest]\n' > "$gate_ini"
        fi
        if ! python3 -P -c 'import sys' >/dev/null 2>&1; then
            echo "python3 does not accept -P (Python 3.11 or newer is needed), so the Python tests cannot be run safely" | tee -a "$log_file"
            echo "Python tests failed" | tee -a "$log_file"
            ok=0
        elif ! with_timeout "$GATE_TIMEOUT_SEC" gate_pytest "$gate_path" "$gate_ini" >> "$log_file" 2>&1; then
            echo "Python tests failed" | tee -a "$log_file"
            ok=0
        fi
        rm -f "$gate_ini"
    fi

    acquire_git_setup_lock
    git -C "$PROJECT_DIR" worktree remove --force "$gate_path" >/dev/null 2>&1 || rm -rf "$gate_path"
    release_git_setup_lock
    [ "$ok" -eq 1 ]
}

# Check if item should be auto-deferred and do it
maybe_auto_defer() {
    local item_id="$1"
    local max_total
    max_total=$(sanitize_int "${EXEC_MAX_TOTAL_FAILURES:-4}")
    local total_fails
    total_fails=$(get_total_failure_count "$item_id")
    if [ "$total_fails" -ge "$max_total" ]; then
        auto_defer_item "$item_id" "Auto-deferred after $total_fails total failures across multiple days"
        post_heartbeat "idle" "$item_id" "auto-deferred after $total_fails failures"
        return 0
    fi
    return 1
}

# Execute one roadmap item in a worktree (can run in parallel)
execute_item() {
    local item_json="$1"
    local item_id item_title branch worktree_path start_time log_file
    item_id=$(echo "$item_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
    item_title=$(echo "$item_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])")
    branch="auto/$item_id"
    worktree_path="$WORKTREE_DIR/auto-$item_id"
    start_time=$(date +%s)
    log_file="$LOG_DIR/$(date +%Y-%m-%d-%H%M%S)-$item_id.log"

    # Stage timing variables (epoch seconds; 0 = not reached)
    local t_wt_start=0 t_wt_end=0 t_cl_start=0 t_cl_end=0 t_test_start=0 t_test_end=0 t_merge_start=0 t_merge_end=0
    local cost_usd="0"

    # Skip items that have failed 3+ times today (don't waste resources)
    local fail_count
    fail_count=$(get_failure_count "$item_id")
    if [ "$fail_count" -ge 3 ]; then
        echo "Skipping $item_id: failed $fail_count times today" | tee -a "$LOG_DIR/dispatcher.log"
        post_heartbeat "idle" "$item_id" "skipped: $fail_count failures today"
        return 1
    fi

    echo "=== Executing: $item_title ===" | tee "$log_file"
    echo "Branch: $branch | Worktree: $worktree_path" | tee -a "$log_file"
    echo "Started: $(date '+%Y-%m-%d %H:%M %Z')" | tee -a "$log_file"

    ln -sf "$log_file" "$LOG_DIR/latest-$item_id.log"
    ln -sf "$log_file" "$LOG_DIR/latest.log"

    post_heartbeat "working" "$item_id"

    # Create worktree from latest master (serialized to prevent .git/config lock races)
    t_wt_start=$(date +%s)
    acquire_git_setup_lock

    cd "$PROJECT_DIR"
    # The branch by its full name on both sides: `git fetch origin master`
    # fetches a tag named master when the remote has one, and leaves the
    # remote-tracking branch where it was.
    if ! retry 3 5 with_timeout "$NET_TIMEOUT_SEC" git fetch origin \
            "+refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH" >> "$log_file" 2>&1; then
        release_git_setup_lock
        echo "git fetch failed after retries; skipping item" | tee -a "$log_file"
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "git fetch failed" "0" "" "0" "Network error fetching $TARGET_BRANCH"
        return 1
    fi

    # Clean up stale worktree/branch if leftover from previous failed run
    git worktree remove --force "$worktree_path" 2>/dev/null || true
    git branch -D "$branch" 2>/dev/null || true

    # The commit the branch starts from, by hash. Every later comparison (did
    # the session change anything, what did it change) is against this value,
    # read before the session runs: the remote-tracking branch is a ref in the
    # repository the session shares, and a session can move it. It is read by
    # its full name: "origin/$TARGET_BRANCH" would resolve to a tag of that
    # name before the remote-tracking branch.
    local base_sha
    base_sha=$(git rev-parse --verify -q "refs/remotes/origin/$TARGET_BRANCH^{commit}" 2>/dev/null || true)

    if [ -z "$base_sha" ] || ! git worktree add -b "$branch" "$worktree_path" "$base_sha" >> "$log_file" 2>&1; then
        release_git_setup_lock
        echo "worktree creation failed: skipping item" | tee -a "$log_file"
        git branch -D "$branch" 2>/dev/null || true
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "worktree failed" "0" "" "0" "Could not create worktree"
        return 1
    fi

    release_git_setup_lock
    t_wt_end=$(date +%s)

    # Symlink node_modules into worktree so Claude Code can run web tests
    if [ -d "$PROJECT_DIR/web/node_modules" ]; then
        ln -sf "$PROJECT_DIR/web/node_modules" "$worktree_path/web/node_modules" 2>/dev/null || true
    fi

    # Build the prompt from template
    local prompt
    prompt=$(python3 "$SCRIPT_DIR/cas-pick-item.py" --build-prompt "$item_id" 2>/dev/null) || {
        echo "Prompt build failed: skipping item" | tee -a "$log_file"
        cd "$PROJECT_DIR"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "prompt failed" "0" "" "0" "Could not build prompt"
        return 1
    }

    # Substitute worktree path into prompt so Claude Code edits the right directory
    prompt="${prompt//\{\{WORKTREE_PATH\}\}/$worktree_path}"

    # Prepend a hard scope guardrail to keep Claude's session focused on the
    # files named in the item description. Without this, the executor's Claude
    # session has historically wandered into adjacent improvements (brand-voice
    # rewrites in docs/, refactors in unrelated files, CLAUDE.md edits).
    prompt="SCOPE GUARDRAIL: modify ONLY the files explicitly named in the item description. Do NOT touch brand voice, customer copy, CLAUDE.md, docs/, or unrelated files. If the description says 'modify file X', that is the WHOLE scope. If you see other improvements you would make, leave them for the operator.

$prompt"

    # Run Claude Code in the worktree, under the hard limit (20 minutes). The
    # session is a job with a process group of its own (with_timeout): when
    # the limit is reached the timer ends the whole group, however deep the
    # session's process tree is: a kill that reaches only the session and its
    # children leaves their children running. (History: a handler that only
    # set a flag once left `claude -p` running for over five hours.) TIMED_OUT
    # tells a timeout from an ordinary failure; with max_parallel > 1 this
    # function runs as a background job and has its own copy of it.
    # Count the session as started BEFORE it runs, so failed attempts consume
    # the daily budget of sessions too (max_items_per_day).
    increment_daily_count
    t_cl_start=$(date +%s)
    local claude_exit=0 was_timeout=0
    local claude_bin="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || echo /opt/homebrew/bin/claude)}"
    cd "$worktree_path"
    with_timeout "$SESSION_TIMEOUT_SEC" "$claude_bin" -p "$prompt" \
        --max-budget-usd "${EXEC_MAX_BUDGET:-3}" \
        >> "$log_file" 2>&1 || claude_exit=$?
    was_timeout="$TIMED_OUT"
    t_cl_end=$(date +%s)
    # From here on the loop's own helpers (python3 -c one-liners, the picker)
    # do not run with the session's worktree as their working directory, where
    # a file the session left could be imported in place of a standard module.
    # git is pointed at the worktree with -C; the draft-PR push further down
    # is the one command that changes into it.
    cd "$PROJECT_DIR"
    if [ "$was_timeout" -eq 1 ]; then
        echo "Timeout: the Claude Code session ran past ${SESSION_TIMEOUT_SEC}s and was ended with everything it started" >> "$log_file"
    fi

    # Extract cost from Claude Code output (best effort)
    local parsed_cost
    parsed_cost=$(grep -oE 'Total cost: \$[0-9.]+' "$log_file" | grep -oE '[0-9.]+' | tail -1 2>/dev/null || echo "")
    if [ -n "$parsed_cost" ]; then
        cost_usd="$parsed_cost"
    else
        # Fallback: estimate from Claude session duration (~$0.10/min for Opus)
        local cl_dur=$(( t_cl_end - t_cl_start ))
        cost_usd=$(python3 -c "print(round($cl_dur / 60 * 0.10, 2))" 2>/dev/null || echo "0")
    fi

    if [ "$was_timeout" -eq 1 ] || [ "$claude_exit" -ne 0 ]; then
        echo "Claude exited with code $claude_exit (timeout=$was_timeout)" | tee -a "$log_file"
        cd "$PROJECT_DIR"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        local duration=$(( $(date +%s) - start_time ))
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        local reason="Claude exited $claude_exit"
        [ "$was_timeout" -eq 1 ] && reason="Timed out after 20 min"
        post_result "$item_id" "$item_title" "failed" "$reason" "$cost_usd" "" "$duration" "$reason"
        return 1
    fi

    # What did the session produce? Exactly one commit is carried through the
    # rest of this function: the tip of $branch as it is now (tested_sha). It
    # must be a real, finished change: the worktree is still on $branch,
    # nothing in it is uncommitted or untracked (a session that left an edit
    # or a new file behind did not commit what it meant to), the commit still
    # descends from the one the worktree was created from (otherwise the tests
    # would run on an older tree than the one being merged into), and its tree
    # differs from that starting point (a session that did nothing would
    # otherwise be merged as "Already up to date" and its item marked
    # completed). Checked once, here, before the tests are spent.
    local change_problem="" wt_branch wt_status="" committed_files="" tested_sha=""
    wt_branch=$(git -C "$worktree_path" branch --show-current 2>/dev/null || true)
    if [ "$wt_branch" != "$branch" ]; then
        change_problem="the worktree is on '${wt_branch:-a detached HEAD}', not $branch"
    elif ! wt_status=$(git -C "$worktree_path" status --porcelain 2>>"$log_file"); then
        change_problem="could not read the worktree status"
    else
        # The node_modules link this function created is the one untracked
        # entry that is expected (and it is not listed at all where .gitignore
        # ignores it).
        wt_status=$(printf '%s\n' "$wt_status" | grep -v '^?? web/node_modules$' || true)
        tested_sha=$(git -C "$worktree_path" rev-parse --verify -q "HEAD^{commit}" 2>>"$log_file" || true)
        if [ -n "$wt_status" ]; then
            change_problem="the session left uncommitted or untracked files in the worktree"
        elif [ -z "$tested_sha" ]; then
            change_problem="could not read the commit $branch points at"
        elif ! git -C "$worktree_path" merge-base --is-ancestor "$base_sha" "$tested_sha" 2>>"$log_file"; then
            change_problem="$branch no longer descends from the commit its worktree was created from"
        elif ! committed_files=$(git -C "$worktree_path" diff --name-only --no-renames "$base_sha" "$tested_sha" 2>>"$log_file"); then
            change_problem="could not diff $branch against the commit it started from"
        elif [ -z "$committed_files" ]; then
            change_problem="the session committed no change on $branch"
        fi
    fi
    if [ -n "$change_problem" ]; then
        echo "REJECT: $change_problem; nothing is tested or merged" | tee -a "$log_file"
        if [ -n "$wt_status" ]; then echo "$wt_status" | tee -a "$log_file"; fi
        cd "$PROJECT_DIR"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        local duration=$(( $(date +%s) - start_time ))
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "No committed change" "$cost_usd" "" "$duration" "$change_problem"
        return 1
    fi

    # Merge guard, first pass: refuse branches that touch the executor's own
    # control surface. A branch written by an unattended agent must not be able
    # to loosen the limits, hooks, CI or test gate that constrain the next run
    # (everything under scripts/, the git hooks, the workflows, the test command
    # and the test-runner configuration; see scripts/cas-merge-guard.py), to remove a
    # test, to queue more work for itself (roadmap.json; the loop updates item
    # status itself), or to rewrite what every Claude Code session reads or
    # obeys (CLAUDE.md, .claude/, .mcp.json). It runs here, before the test
    # gate, so that the commands the gate runs (npm install, npm test, pytest)
    # and what they load before the first test are the target branch's and not
    # ones the session rewrote, and before the tiered-governance path can push
    # the branch anywhere. It compares two commits by hash: where the worktree
    # started and what is about to be tested.
    local guard_hits="" guard_ok=1
    guard_hits=$(guard_report "$base_sha" "$tested_sha" "$worktree_path") || guard_ok=0
    if [ "$guard_ok" -ne 1 ] || [ -n "$guard_hits" ]; then
        echo "REJECT: branch touches protected paths, deletes a test or adds a symlink" | tee -a "$log_file"
        if [ "$guard_ok" -ne 1 ]; then echo "(the branch diff could not be computed)" | tee -a "$log_file"; fi
        echo "$guard_hits" | tee -a "$log_file"
        cd "$PROJECT_DIR"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        local duration=$(( $(date +%s) - start_time ))
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Protected file" "$cost_usd" "" "$duration" "$GUARD_REJECT_REASON"
        return 1
    fi

    # Run the tests on a fresh checkout of the commit (see run_test_gate).
    t_test_start=$(date +%s)
    echo "Running tests on $tested_sha..." | tee -a "$log_file"
    local tests_ok=1
    if ! run_test_gate "$tested_sha" "$base_sha" "$WORKTREE_DIR/gate-$item_id" "$log_file"; then
        tests_ok=0
    fi
    cd "$PROJECT_DIR"

    t_test_end=$(date +%s)
    local duration=$(( $(date +%s) - start_time ))

    if [ "$tests_ok" -eq 0 ]; then
        echo "Tests FAILED: discarding" | tee -a "$log_file"
        cd "$PROJECT_DIR"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Tests failed" "$cost_usd" "" "$duration" "Test suite failed after code changes"
        return 1
    fi

    echo "Tests passed: ready to merge" | tee -a "$log_file"

    # Tiered governance: classify risk based on changed files
    if [ "$TIERED_GOVERNANCE" = "True" ]; then
        local risk_level
        # committed_files is the --no-renames diff of the tested commit against
        # its starting point (read above): a renamed file is listed under its
        # old name as well, so moving a high-risk file does not hide it from
        # the classifier.
        risk_level=$(printf '%s\n' "$committed_files" | python3 "$SCRIPT_DIR/classify_risk.py" 2>/dev/null || echo "high")
        if [ "$risk_level" = "high" ]; then
            echo "HIGH RISK: creating draft PR instead of auto-merge" | tee -a "$log_file"
            # The item is marked in_review only when BOTH the push and the PR
            # creation succeed, so their exit codes are captured, never
            # swallowed with `|| true`: a version that did swallow them once
            # marked seven items in_review that had no branch on GitHub and no
            # PR. CAS_EXECUTOR=1 bypasses the pre-push staging-workflow block
            # on auto/* branches (mirrors the fast-path pattern below).
            # What is pushed for review is the tested commit by its hash, not
            # whatever $branch points at by now. The push runs from the
            # session's worktree, as git runs the pre-push hook where the push
            # starts.
            local push_ok=0 pr_ok=0
            cd "$worktree_path" 2>/dev/null || true
            if CAS_EXECUTOR=1 retry 3 10 with_timeout "$PUSH_TIMEOUT_SEC" git push origin "$tested_sha:refs/heads/$branch" >> "$log_file" 2>&1; then
                push_ok=1
            else
                echo "git push origin $branch FAILED after retries (HIGH-risk PR path)" | tee -a "$log_file"
            fi
            if [ "$push_ok" -eq 1 ]; then
                if with_timeout "$NET_TIMEOUT_SEC" "${GH_BIN:-gh}" pr create --base "$TARGET_BRANCH" --head "$branch" \
                        --title "[auto] $item_title" --body "Risk: HIGH. Auto-generated by executor." \
                        --draft >> "$log_file" 2>&1; then
                    pr_ok=1
                else
                    echo "gh pr create FAILED (HIGH-risk PR path, branch=$branch)" | tee -a "$log_file"
                fi
            fi
            cd "$PROJECT_DIR"
            if [ "$push_ok" -ne 1 ] || [ "$pr_ok" -ne 1 ]; then
                # The item stays pending; a status_drift_note records the
                # attempt so the operator can audit it. Emit a failed heartbeat
                # and clean up the local branch + worktree.
                local drift_ts
                drift_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
                record_roadmap_status_locked "$item_id" pending "$log_file" \
                    "[auto] Note a failed push or PR for $item_id in roadmap" \
                    status_drift_note "push_or_pr_failed at $drift_ts" \
                    || echo "Warning: could not record the status_drift_note for $item_id" | tee -a "$log_file"
                cd "$PROJECT_DIR"
                git worktree remove --force "$worktree_path" 2>/dev/null || true
                git branch -D "$branch" 2>/dev/null || true
                # Failed heartbeat so the operator sees the event in the
                # executor dashboard. post_heartbeat sends the fields the
                # heartbeat route stores (current_item, skipped_reason).
                post_heartbeat "failed" "$item_id" "Push or PR creation failed" >> "$log_file" 2>&1 || true
                record_failure "$item_id"
                maybe_auto_defer "$item_id" || true
                local t_end; t_end=$(date +%s); duration=$(( t_end - start_time ))
                post_result "$item_id" "$item_title" "failed" "Push or PR creation failed" "$cost_usd" "" "$duration" "git push or gh pr create failed; item left pending"
                return 1
            fi
            # Both push + PR creation succeeded.
            # Mark the item in_review in roadmap.json, in a commit, so it is
            # not picked again (an item left "pending" here was once re-picked
            # every cycle) and the status survives an executor restart. If the
            # status cannot be recorded, the failure is counted against the
            # item, so the per-day and total failure caps stop the re-picks.
            if ! record_roadmap_status_locked "$item_id" in_review "$log_file" \
                    "[auto] Mark $item_id as in_review (PR created)"; then
                echo "Warning: could not update roadmap status to in_review" | tee -a "$log_file"
                record_failure "$item_id"
            fi
            cd "$PROJECT_DIR"
            # Notify the operator via ops API
            post_heartbeat "pr_created" "$item_id" >> "$log_file" 2>&1 || true
            git worktree remove --force "$worktree_path" 2>/dev/null || true
            # start_time is this function's start. A reference here to a
            # variable that was never set once crashed the loop under `set -u`
            # after every HIGH-risk item, and launchd respawned it each time.
            local t_end; t_end=$(date +%s); duration=$(( t_end - start_time ))
            post_result "$item_id" "$item_title" "review" "PR created for manual review" "$cost_usd" "" "$duration" ""
            return 0
        fi
    fi

    # === SERIALIZE: merge lock + build wait + push ===
    t_merge_start=$(date +%s)
    acquire_merge_lock

    # A journal at this point is an earlier merge of this run that could not be
    # settled (leave_merge_unsettled, or a push the remote has not answered
    # for), or a stray file. It says what is in flight in this checkout, and a
    # second merge would stack on it and replace the record of it. The next
    # cycle settles the journal before it starts anything.
    if [ -f "$MERGE_JOURNAL" ]; then
        cd "$PROJECT_DIR"
        echo "Not merging $item_id: an earlier merge is unsettled ($MERGE_JOURNAL)" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        release_merge_lock
        post_result "$item_id" "$item_title" "failed" "Merge unsettled" "$cost_usd" "" "$duration" "An earlier merge is unsettled; nothing was merged, and the item will retry once the next cycle has settled it"
        return 1
    fi

    local builds_rc=0
    wait_for_builds_clear "$log_file" || builds_rc=$?
    if [ "$builds_rc" -ne 0 ]; then
        cd "$PROJECT_DIR"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        release_merge_lock
        if [ "$builds_rc" -eq 2 ]; then
            # The ops API stopped answering after preflight. That says nothing
            # about the item, so it is not counted against its failure caps.
            post_result "$item_id" "$item_title" "failed" "Build check unavailable" "$cost_usd" "" "$duration" "The ops API did not report build counts, so nothing was pushed; item will retry next cycle"
            return 1
        fi
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Builds active" "$cost_usd" "" "$duration" "Active builds did not clear in time; item will retry next cycle"
        return 1
    fi

    cd "$PROJECT_DIR"

    # The preflight yields unless the project dir is on $TARGET_BRANCH, but a
    # human may have switched branches during the 15 to 65 minutes since. Never
    # check out $TARGET_BRANCH underneath them: discard this run instead (the
    # item is re-picked once the tree is back on $TARGET_BRANCH).
    local merge_branch
    merge_branch=$(git branch --show-current 2>/dev/null || true)
    if [ "$merge_branch" != "$TARGET_BRANCH" ]; then
        echo "Project dir is on '$merge_branch', not $TARGET_BRANCH: discarding this run instead of switching branches" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        release_merge_lock
        post_result "$item_id" "$item_title" "failed" "Branch moved" "$cost_usd" "" "$duration" "Project dir left $TARGET_BRANCH during the run; nothing was merged"
        return 1
    fi

    # From here to close_merge_section the checkout goes through states a
    # stopped loop must not leave behind (edits in a stash, an unpushed
    # merge). The journal says which one; see MERGE_JOURNAL.
    local stash_tag=""
    if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        stash_tag="cas-exec-premerge-${item_id}-$(date +%s)"
    fi
    if ! journal_write item "$item_id" target "$TARGET_BRANCH" stash_tag "$stash_tag" \
            tested_sha "$tested_sha" pre_merge_sha "" phase stash; then
        echo "Could not write $MERGE_JOURNAL: not merging" | tee -a "$log_file"
        rm -f "$MERGE_JOURNAL"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        release_merge_lock
        post_result "$item_id" "$item_title" "failed" "Journal failed" "$cost_usd" "" "$duration" "The merge journal could not be written; nothing was merged"
        return 1
    fi

    # Pre-merge dirty safety: parallel Claude Code sessions can dirty files in
    # PROJECT_DIR during the 15-20 min worktree run, and the files in
    # DIRTY_OK_RE may be dirty from the start. Without stashing first, git
    # checkout / git merge fails with "local changes would be overwritten" and
    # the session's work is wasted. Untracked files are left in place; only
    # tracked dirty files are stashed, since untracked files don't block
    # checkout/merge.
    if [ -n "$stash_tag" ]; then
        echo "Pre-merge: dirty tree detected, stashing with tag $stash_tag" | tee -a "$log_file"
        # The merge_lock serializes executor-vs-executor stash attempts, but
        # `.git/index.lock` can still be held by other git writers in this
        # checkout (an interactive session, an editor's git integration, a
        # scheduled job), and a single failed stash used to cost the whole
        # run. Retry up to 5x with 3s backoff; detect a stale lock (>60s old,
        # owner crashed) and force-rm.
        local stash_ok=0 stash_attempt
        for stash_attempt in 1 2 3 4 5; do
            if git stash push -m "$stash_tag" >> "$log_file" 2>&1; then
                stash_ok=1
                break
            fi
            if [ -f "$PROJECT_DIR/.git/index.lock" ]; then
                local lock_mtime now lock_age
                lock_mtime=$(stat -c %Y "$PROJECT_DIR/.git/index.lock" 2>/dev/null || stat -f %m "$PROJECT_DIR/.git/index.lock" 2>/dev/null || echo 0)
                now=$(date +%s)
                lock_age=$(( now - lock_mtime ))
                if [ "$lock_age" -gt 60 ]; then
                    echo "Pre-merge: stale .git/index.lock (${lock_age}s old), force-removing" | tee -a "$log_file"
                    rm -f "$PROJECT_DIR/.git/index.lock"
                else
                    echo "Pre-merge: .git/index.lock held (${lock_age}s old, attempt $stash_attempt/5), retrying in 3s" | tee -a "$log_file"
                fi
            else
                echo "Pre-merge: stash failed (no index.lock visible, attempt $stash_attempt/5), retrying in 3s" | tee -a "$log_file"
            fi
            sleep 3
        done
        if [ "$stash_ok" -ne 1 ]; then
            # git refuses a stash while a path is unmerged (a stash pop that
            # conflicted, a merge a person left unfinished). That is the state of
            # the checkout, not of the item, so nothing is charged to the item:
            # no failure is recorded and the item cannot be deferred for it. The
            # next cycle's preflight yields until the paths are resolved.
            local stash_unmerged
            if ! stash_unmerged=$(unmerged_paths) || [ -n "$stash_unmerged" ]; then
                echo "Pre-merge: git refused the stash because the checkout has unmerged paths (${stash_unmerged:-the index cannot be read}); nothing was merged, and this run is not charged to $item_id" | tee -a "$log_file"
                git worktree remove --force "$worktree_path" 2>/dev/null || true
                git branch -D "$branch" 2>/dev/null || true
                close_merge_section "$stash_tag" "$log_file"
                post_result "$item_id" "$item_title" "failed" "Checkout has unmerged paths" "$cost_usd" "" "$duration" "The checkout has unmerged paths (a stash pop that conflicted or an unfinished merge); nothing was merged and the run is not counted against the item"
                return 1
            fi
            echo "Pre-merge: stash FAILED after 5 retries; aborting merge to preserve parallel work" | tee -a "$log_file"
            git worktree remove --force "$worktree_path" 2>/dev/null || true
            git branch -D "$branch" 2>/dev/null || true
            close_merge_section "$stash_tag" "$log_file"
            record_failure "$item_id"
            maybe_auto_defer "$item_id" || true
            post_result "$item_id" "$item_title" "failed" "Stash failed" "$cost_usd" "" "$duration" "Could not stash dirty tree before merge (parallel session work at risk)"
            return 1
        fi
    fi

    retry 2 3 with_timeout "$NET_TIMEOUT_SEC" git pull --ff-only >> "$log_file" 2>&1 || true

    # Where $TARGET_BRANCH is now, by hash, read from the branch by its full
    # name (a tag named like the branch would win a lookup by short name).
    # The second guard pass diffs against this commit, the merge goes on top
    # of it, and a failed push restores exactly it. HEAD must be that branch
    # and that commit: the preflight and the check above saw the checkout on
    # $TARGET_BRANCH, and nothing here ever checks a branch out underneath a
    # person who has switched since.
    local pre_merge_sha
    pre_merge_sha=$(git rev-parse --verify -q "refs/heads/$TARGET_BRANCH^{commit}" 2>/dev/null || true)
    if [ -z "$pre_merge_sha" ] \
        || [ "$(git symbolic-ref -q HEAD 2>/dev/null || true)" != "refs/heads/$TARGET_BRANCH" ] \
        || [ "$(git rev-parse --verify -q "HEAD^{commit}" 2>/dev/null || true)" != "$pre_merge_sha" ]; then
        echo "Project dir is no longer on $TARGET_BRANCH: discarding this run instead of switching branches" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file"
        post_result "$item_id" "$item_title" "failed" "Branch moved" "$cost_usd" "" "$duration" "Project dir left $TARGET_BRANCH during the run; nothing was merged"
        return 1
    fi

    # Merge guard, second pass: the same rules applied to what the merge is
    # about to bring into $TARGET_BRANCH as it stands in this checkout (the
    # first pass compared the tested commit with the commit its worktree
    # started from; this one compares it with the commit it is merged into,
    # which differs from the first whenever the branch has moved on since:
    # a pull, or commits made in this checkout).
    guard_hits=""
    guard_ok=1
    guard_hits=$(guard_report "$pre_merge_sha" "$tested_sha" "$PROJECT_DIR") || guard_ok=0
    if [ "$guard_ok" -ne 1 ] || [ -n "$guard_hits" ]; then
        echo "REJECT: branch touches protected paths, deletes a test or adds a symlink" | tee -a "$log_file"
        if [ "$guard_ok" -ne 1 ]; then echo "(the branch diff could not be computed)" | tee -a "$log_file"; fi
        echo "$guard_hits" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file"
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Protected file" "$cost_usd" "" "$duration" "$GUARD_REJECT_REASON"
        return 1
    fi

    # The pin. The tests ran on $tested_sha and both guard passes read it, so
    # that object is the only thing this function merges. If $branch points
    # anywhere else by now, something (a process the session left running, or
    # another writer) committed to it after the tests: that work is untested
    # and the session's result is not what was examined, so the whole run is
    # refused rather than merged in part.
    local branch_now
    branch_now=$(git rev-parse --verify -q "refs/heads/$branch^{commit}" 2>/dev/null || true)
    if [ "$branch_now" != "$tested_sha" ]; then
        echo "REJECT: $branch is at ${branch_now:-no commit} but the tests ran on $tested_sha" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file"
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Branch moved after tests" "$cost_usd" "" "$duration" "$PIN_REJECT_REASON"
        return 1
    fi

    # git refuses a merge while a path is unmerged. As at the stash, that is the
    # checkout's state and not the item's: nothing is merged and no failure is
    # recorded for the item.
    local merge_unmerged
    if ! merge_unmerged=$(unmerged_paths) || [ -n "$merge_unmerged" ]; then
        echo "Merge: the checkout has unmerged paths (${merge_unmerged:-the index cannot be read}); nothing was merged, and this run is not charged to $item_id" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file" "$pre_merge_sha"
        post_result "$item_id" "$item_title" "failed" "Checkout has unmerged paths" "$cost_usd" "" "$duration" "The checkout has unmerged paths (a stash pop that conflicted or an unfinished merge); nothing was merged and the run is not counted against the item"
        return 1
    fi

    # The journal learns where $TARGET_BRANCH was before anything is merged.
    if ! journal_write item "$item_id" target "$TARGET_BRANCH" stash_tag "$stash_tag" \
            tested_sha "$tested_sha" pre_merge_sha "$pre_merge_sha" phase merge; then
        echo "Could not update $MERGE_JOURNAL: not merging" | tee -a "$log_file"
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file"
        post_result "$item_id" "$item_title" "failed" "Journal failed" "$cost_usd" "" "$duration" "The merge journal could not be written; nothing was merged"
        return 1
    fi

    echo "Merging $branch ($tested_sha) to $TARGET_BRANCH..." | tee -a "$log_file"
    if ! git merge --no-ff "$tested_sha" -m "Merge $branch: $item_title

Co-Authored-By: Claude <noreply@anthropic.com>" >> "$log_file" 2>&1; then
        echo "Merge conflict: discarding" | tee -a "$log_file"
        git merge --abort 2>/dev/null || true
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file" "$pre_merge_sha"
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Merge conflict" "$cost_usd" "" "$duration" "Could not merge to $TARGET_BRANCH (conflict with parallel item)"
        return 1
    fi

    # Mark completed in roadmap.json BEFORE push (batches into single push to
    # reduce Railway deploy events). The mark is committed as exactly that one
    # change and through the pre-commit hook (record_roadmap_status; the merge
    # lock is held here). If it cannot be, the merge is taken back and nothing
    # is pushed: a merge pushed without its mark leaves the item pending, to be
    # worked a second time.
    if ! record_roadmap_status "$item_id" completed "$log_file" "[auto] Mark $item_id completed in roadmap

Co-Authored-By: Claude <noreply@anthropic.com>"; then
        echo "Could not record $item_id as completed; taking $TARGET_BRANCH back to $pre_merge_sha (before the merge)" | tee -a "$log_file"
        if ! take_back_merge "$item_id" "$TARGET_BRANCH" "$pre_merge_sha" "$tested_sha" "$log_file"; then
            leave_merge_unsettled "$item_id" "$log_file" "$worktree_path" "$branch"
            post_result "$item_id" "$item_title" "failed" "Merge not taken back" "$cost_usd" "" "$duration" "The completed status could not be committed and the merge could not be taken back ($TAKE_BACK_REASON); nothing pushed, the next cycle settles it"
            return 1
        fi
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file" "$pre_merge_sha"
        record_failure "$item_id"
        maybe_auto_defer "$item_id" || true
        post_result "$item_id" "$item_title" "failed" "Roadmap mark failed" "$cost_usd" "" "$duration" "The completed status could not be committed as a single change; merge reverted, nothing pushed"
        return 1
    fi

    # Push. The journal is told first: from here on the remote may have the
    # merge without this loop knowing it.
    if ! journal_write item "$item_id" target "$TARGET_BRANCH" stash_tag "$stash_tag" \
            tested_sha "$tested_sha" pre_merge_sha "$pre_merge_sha" phase push; then
        echo "Could not update $MERGE_JOURNAL; taking $TARGET_BRANCH back to $pre_merge_sha (nothing was pushed)" | tee -a "$log_file"
        if ! take_back_merge "$item_id" "$TARGET_BRANCH" "$pre_merge_sha" "$tested_sha" "$log_file"; then
            leave_merge_unsettled "$item_id" "$log_file" "$worktree_path" "$branch"
            post_result "$item_id" "$item_title" "failed" "Merge not taken back" "$cost_usd" "" "$duration" "The merge journal could not be written and the merge could not be taken back ($TAKE_BACK_REASON); nothing pushed, the next cycle settles it"
            return 1
        fi
        git worktree remove --force "$worktree_path" 2>/dev/null || true
        git branch -D "$branch" 2>/dev/null || true
        close_merge_section "$stash_tag" "$log_file" "$pre_merge_sha"
        post_result "$item_id" "$item_title" "failed" "Journal failed" "$cost_usd" "" "$duration" "The merge journal could not be written; merge reverted, nothing pushed"
        return 1
    fi
    if ! CAS_EXECUTOR=1 retry 3 10 with_timeout "$PUSH_TIMEOUT_SEC" git push >> "$log_file" 2>&1; then
        # A push that did not report success is not proof that the remote
        # lacks the merge (a push ended by its time limit may have got
        # through). The remote is asked before anything is taken back
        # (settle_interrupted_merge): when it has the merge the run continues
        # as a success; when it does not, $TARGET_BRANCH goes back to
        # $pre_merge_sha; when it cannot be asked the journal stays and the
        # next cycle settles it.
        echo "git push FAILED after retries; asking the remote whether it has the merge" | tee -a "$log_file"
        local settled=1
        settle_interrupted_merge online "$log_file" || settled=0
        if [ "$settled" -ne 1 ] || [ "$SETTLE_OUTCOME" != "pushed" ]; then
            git worktree remove --force "$worktree_path" 2>/dev/null || true
            git branch -D "$branch" 2>/dev/null || true
            release_merge_lock
            if [ "$settled" -ne 1 ]; then
                echo "Not settled: $SETTLE_REASON. The merge stays in place until the remote answers" | tee -a "$log_file"
                post_result "$item_id" "$item_title" "failed" "Push unsettled" "$cost_usd" "" "$duration" "$SETTLE_REASON"
                return 1
            fi
            echo "$TARGET_BRANCH is back at $pre_merge_sha (before the merge and the roadmap mark)" | tee -a "$log_file"
            record_failure "$item_id"
            maybe_auto_defer "$item_id" || true
            post_result "$item_id" "$item_title" "failed" "Push failed" "$cost_usd" "" "$duration" "git push rejected; merge reverted"
            return 1
        fi
        echo "The remote has the merge after all; continuing" | tee -a "$log_file"
        stash_tag=""   # settle_interrupted_merge has popped it and removed the journal
    fi

    local commit_sha
    commit_sha=$(git rev-parse --short HEAD)

    # Clean up worktree and branch
    git worktree remove --force "$worktree_path" 2>/dev/null || true
    git branch -d "$branch" 2>/dev/null || true

    close_merge_section "$stash_tag" "$log_file"

    # Wait for deploy (non-blocking, 2 min max, just a courtesy check)
    echo "Waiting for web deploy..." | tee -a "$log_file"
    local deploy_ok=0
    for _dcheck in $(seq 1 8); do
        sleep 15
        local web_commit
        web_commit=$(bounded_curl "$OPS_BASE/health" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('git_commit','')[:7])" 2>/dev/null || echo "")
        if [ "$web_commit" = "${commit_sha:0:7}" ]; then
            deploy_ok=1
            echo "Web deployed: $commit_sha" | tee -a "$log_file"
            break
        fi
    done
    if [ "$deploy_ok" -eq 0 ]; then
        echo "Web deploy not confirmed after 2 min (non-blocking)" | tee -a "$log_file"
    fi

    t_merge_end=$(date +%s)

    # Log stage timings
    local wt_dur=$((t_wt_end - t_wt_start)) cl_dur=$((t_cl_end - t_cl_start)) te_dur=$((t_test_end - t_test_start)) mg_dur=$((t_merge_end - t_merge_start))
    echo "=== Stage Timings ===" | tee -a "$log_file"
    echo "  Worktree: ${wt_dur}s | Claude: ${cl_dur}s | Tests: ${te_dur}s | Merge+Push: ${mg_dur}s" | tee -a "$log_file"
    echo "  Cost: \$${cost_usd}" | tee -a "$log_file"

    echo "=== Completed: $item_title ($commit_sha, ${duration}s, \$${cost_usd}) ===" | tee -a "$log_file"
    local deploy_note="web deploy not confirmed"
    if [ "$deploy_ok" -eq 1 ]; then deploy_note="web deploy confirmed"; fi
    post_result "$item_id" "$item_title" "completed" \
        "Merged and pushed, $deploy_note (wt=${wt_dur}s claude=${cl_dur}s test=${te_dur}s merge=${mg_dur}s cost=\$${cost_usd})" \
        "$cost_usd" "$commit_sha" "$duration" ""

    return 0
}

# ========== MAIN LOOP ==========
# Crash-loop detector BEFORE acquire_lock so a stale lockfile can't wedge us
# into a crash-then-respawn state. Sleeps one full window if tripped (see
# check_crash_loop).
check_crash_loop

acquire_lock

# This is now the only executor on this state directory. End the jobs a
# previous one left running (it was killed outright), then clean up its
# worktrees and its locks. What it left of a merge section is settled by the
# first cycle (preflight_check).
stop_orphan_jobs
cleanup_worktrees
rm -f "$MERGE_LOCK" "$GIT_SETUP_LOCK"
rmdir "$MERGE_LOCK.breaking" "$GIT_SETUP_LOCK.breaking" 2>/dev/null || true

# Mark any executor_runs rows left stuck in running/working/pr_created by a
# previous crash. Idempotent, best-effort; no-op if web is down.
sweep_zombie_runs

# Prune old failure tracker entries (keep last 7 days for cross-day auto-defer)
if [ -f "$FAIL_TRACKER" ]; then
    prune_cutoff=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d "7 days ago" +%Y-%m-%d 2>/dev/null || echo "")
    if [ -n "$prune_cutoff" ]; then
        awk -v cutoff="$prune_cutoff" '$1 >= cutoff' "$FAIL_TRACKER" > "${FAIL_TRACKER}.tmp" 2>/dev/null || true
        mv "${FAIL_TRACKER}.tmp" "$FAIL_TRACKER" 2>/dev/null || true
    fi
    unset prune_cutoff
fi

echo "CAS Roadmap Executor started at $(date '+%Y-%m-%d %H:%M %Z')"
echo "Config: $CONFIG"
echo "Logs: $LOG_DIR"
echo "PID: $$"

# Initial variables (overwritten each cycle by preflight)
# (Only used if the first config read fails: retry in 10 minutes, one item, $3.)
EXEC_INTERVAL=10
EXEC_MAX_PARALLEL=1
EXEC_MAX_BUDGET=3
EXEC_MAX_TOTAL_FAILURES=4
EXEC_DAILY_REMAINING=0
TARGET_BRANCH=master
TIERED_GOVERNANCE=False

while true; do
    ITERATION_PHASE="preflight"
    if preflight_check; then
        post_heartbeat "running"
        ITERATION_PHASE="picking"

        # Pick up to max_parallel items, but never more than the sessions left
        # in today's max_items_per_day budget. Picker errors go to the
        # dispatcher log instead of being mistaken for an empty queue.
        pick_n="$EXEC_MAX_PARALLEL"
        case "$pick_n" in ''|*[!0-9]*) pick_n=1 ;; esac
        if [ "$pick_n" -gt "$EXEC_DAILY_REMAINING" ]; then pick_n="$EXEC_DAILY_REMAINING"; fi
        all_items=$(python3 "$SCRIPT_DIR/cas-pick-item.py" --next-n "$pick_n" 2>>"$LOG_DIR/dispatcher.log" || echo "")

        if [ -n "$all_items" ] && [ "$all_items" != "[]" ]; then
            item_count=$(sanitize_int "$(echo "$all_items" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")")
            ITERATION_PHASE="executing"

            if [ "$item_count" -eq 1 ]; then
                # Single item: run inline
                item_json=$(echo "$all_items" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)[0]))")
                item_id=$(echo "$item_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
                ln -sf "$LOG_DIR/latest-$item_id.log" "$LOG_DIR/latest.log" 2>/dev/null || true
                execute_item "$item_json" || true
            elif [ "$item_count" -gt 1 ]; then
                # Multiple items: run in parallel, wait for all
                echo "Dispatching $item_count items in parallel..." | tee -a "$LOG_DIR/dispatcher.log"
                pids=""
                for i in $(seq 0 $((item_count - 1))); do
                    item_json=$(echo "$all_items" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)[$i]))")
                    item_id=$(echo "$item_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
                    echo "  Starting: $item_id" | tee -a "$LOG_DIR/dispatcher.log"

                    # The subshell traps SIGTERM so that it exits between
                    # commands: on the way out the main loop signals it
                    # (on_exit), and a git command it is in the middle of is
                    # left to finish.
                    ( SIGNAL_HANDLER='exit 143'
                      trap "$SIGNAL_HANDLER" TERM INT
                      execute_item "$item_json" || true ) &
                    pids="$pids $!"
                    ITEM_PIDS="$pids"
                done

                # Wait for all parallel items to complete
                for pid in $pids; do
                    wait "$pid" 2>/dev/null || true
                done
                ITEM_PIDS=""
                echo "All parallel items finished" | tee -a "$LOG_DIR/dispatcher.log"
            fi
        else
            post_heartbeat "idle" "" "no eligible items"
        fi
    fi

    if [ "${RUN_ONCE:-}" = "1" ]; then
        echo "RUN_ONCE mode: exiting after single iteration"
        CLEAN_EXIT=1
        break
    fi

    ITERATION_PHASE="sleeping"
    echo "Sleeping ${EXEC_INTERVAL}m until next run..."
    # sleep in the background + wait, so a TERM/INT trap fires immediately
    # instead of after the (up to 2 hour) foreground sleep finishes.
    case "$EXEC_INTERVAL" in ''|*[!0-9]*) EXEC_INTERVAL=10 ;; esac
    sleep $((EXEC_INTERVAL * 60)) &
    wait $! || true
done
CLEAN_EXIT=1
