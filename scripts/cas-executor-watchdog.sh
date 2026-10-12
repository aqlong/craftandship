#!/bin/bash
# cas-executor-watchdog.sh: deterministic self-healing sweep for the CAS
# roadmap executor. Runs every 30 min via the launchd agent rendered from
# scripts/com.cas.executor-watchdog.plist.template (2x/hour). Auto-fixes the
# MECHANICAL blocks that stall the executor and that its own preflight cannot
# self-recover from; ALERTS (does not auto-fix) on the ambiguous ones that could
# be active human/parallel-session work.
#
# WHY DETERMINISTIC (not an AI guard): every block this catches is mechanical
# - a stale lock file, a dead PID, an orphan worktree, a crashed-run leftover.
# No judgment needed. A shell script is instant, free, has no API dependency
# (the AI guard itself dies when the Claude CLI auth is down, which is one of
# the very blocks we need to detect), and is trivially testable. Design
# principle: minimize AI decisions in recovery paths.
#
# AUTO-FIX (unambiguously safe):
#   1. Stale .git/index.lock        (>120s old + no live git process) → rm
#   2. Stale cas-executor-merge.lock in the state dir (PID dead) → rm
#   3. Stale cas-executor.lock in the state dir (PID dead + loop not running) → rm
#   4. Orphan .worktrees/auto-* (a session's worktree) and .worktrees/gate-*
#      (the test gate's checkout) (>2h old + no process mentions its path) → prune
#
# ALERT-ONLY (ambiguous: could be active session work, never auto-touch):
#   5. PROJECT_DIR on non-master branch
#   6. A dirty tree the executor yields on, persisting (snapshot-compared across runs)
#   6b. Unmerged paths (a stash pop that conflicted, or an unfinished merge)
#   7. Claude CLI auth failure (401)
#   8. Executor idle >90 min WHILE eligible items exist in the queue
#
# RECOVERY (safe, targeted):
#   9. Executor process not running at all → launchctl kickstart the agent
#
# Usage:
#   scripts/cas-executor-watchdog.sh            # live: auto-fix + alert
#   scripts/cas-executor-watchdog.sh --dry-run  # report only, change nothing
#
# A run is live only when it is started with no argument at all (and without a
# DRY_RUN in its environment other than 0). Any other argument, a mistyped
# --dry-run included, is a dry run that says so.
#
# Checks 7 to 9 (Claude auth probe, stall alert, kickstart) only run while
# scripts/cas-executor-config.json has "enabled": true, so a disabled executor
# costs nothing and raises no "executor is down" alerts. The lock, worktree and
# branch checks (1 to 6) always run.
#
# Built for macOS (launchctl kickstart, launchd label); the lock, worktree and
# branch checks also work on Linux. Needs python3; curl and claude are optional.
# Environment: CAS_EXECUTOR_STATE_DIR, CAS_OPS_BASE_URL, OPS_API_KEY and
# CAS_PROD_URL as described below, CLAUDE_BIN (the CLI to probe, as for the
# loop) and CAS_WATCHDOG_PROBE_TIMEOUT_SEC (whole seconds the probe may take,
# default 30).
#
# Exit code is always 0 (a watchdog must never fail-loop). Findings go to
# stdout (captured by the LaunchAgent log) and, best-effort, to the ops API
# heartbeat so the admin /executor dashboard shows the watchdog ran.

set -uo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
# State files live in CAS_EXECUTOR_STATE_DIR (default $TMPDIR, else /tmp), like the loop's.
STATE_FILES_DIR="${CAS_EXECUTOR_STATE_DIR:-${TMPDIR:-/tmp}}"
STATE_FILES_DIR="${STATE_FILES_DIR%/}"
MERGE_LOCK="$STATE_FILES_DIR/cas-executor-merge.lock"
MAIN_LOCK="$STATE_FILES_DIR/cas-executor.lock"
INDEX_LOCK="$PROJECT_DIR/.git/index.lock"
WORKTREE_DIR="$PROJECT_DIR/.worktrees"
STATE_DIR="$PROJECT_DIR/logs/executor"
DIRTY_SNAPSHOT="$STATE_FILES_DIR/cas-watchdog-dirty-snapshot"
# CAS web app serving /ops/api/v1/* (heartbeat sink) and /health. Both are
# best-effort. CAS_PROD_URL (the deployed site) enables the staleness check (8b).
OPS_BASE="${CAS_OPS_BASE_URL:-http://localhost:4000}"
PROD_URL="${CAS_PROD_URL:-}"
EXECUTOR_LABEL="com.cas.roadmap-executor"

# Live or dry is decided once, here, and read in one place (_live). The run is
# live only for exactly this: no argument, and no inherited DRY_RUN other than
# 0. Everything else is a dry run: --dry-run, an argument this script does not
# know (so a typo of --dry-run cannot be a live run), or a DRY_RUN handed in by
# the caller with any other value.
DRY_REASON=""
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 1 ] || [ "$1" != "--dry-run" ]; then
        DRY_REASON="unrecognised argument(s): $*"
    else
        DRY_REASON="--dry-run"
    fi
elif [ -n "${DRY_RUN:-}" ] && [ "${DRY_RUN}" != "0" ]; then
    DRY_REASON="DRY_RUN=${DRY_RUN} in the environment"
fi
DRY_RUN=1
[ -z "$DRY_REASON" ] && DRY_RUN=0

# The only reader of DRY_RUN: true for a live run, false for anything else.
_live() { [ "$DRY_RUN" = "0" ]; }

if ! _live && [ "$DRY_REASON" != "--dry-run" ]; then
    echo "[watchdog] DRY-RUN ($DRY_REASON): nothing will be changed; a live run takes no argument"
fi

mkdir -p "$STATE_DIR"
cd "$PROJECT_DIR" 2>/dev/null || { echo "[watchdog] cannot cd to $PROJECT_DIR"; exit 0; }

TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
FIXES=()
ALERTS=()

_now() { date +%s; }
# Modification time in epoch seconds: GNU stat first (BSD stat rejects -c), then BSD.
_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }
_file_age() { echo $(( $(_now) - $(_mtime "$1") )); }

# Is any live `git` process running inside PROJECT_DIR right now? Used to
# decide whether .git/index.lock is genuinely held vs orphaned by a crash.
_git_process_active() {
    pgrep -fl "git" 2>/dev/null | grep -qF "$(basename "$PROJECT_DIR")" && return 0
    # Fallback: any git process at all (conservative, avoids racing a real op)
    pgrep -x git >/dev/null 2>&1 && return 0
    return 1
}

# Is the executor loop running?
_executor_running() {
    pgrep -f "cas-roadmap-loop.sh" >/dev/null 2>&1
}

# Run a mutation in a live run; print it in a dry run. The command and its
# arguments are passed as separate words and executed directly (no eval), so a
# path containing a quote or other shell syntax cannot inject commands.
_do() {
    if ! _live; then
        echo "[watchdog] DRY-RUN would: $*"
        return 0
    fi
    "$@"
}

# Remove one executor worktree: ask git first, fall back to rm -rf. Refuses any
# path that is not directly under .worktrees/auto-* or .worktrees/gate-*.
_remove_worktree() {
    case "$1" in
        "$WORKTREE_DIR"/auto-*|"$WORKTREE_DIR"/gate-*) ;;
        *) return 1 ;;
    esac
    git worktree remove --force "$1" 2>/dev/null || rm -rf "$1"
}

# Is the executor enabled in its config? Asked of the picker, the same reader
# the loop uses (executor_enabled in scripts/cas-pick-item.py): anything but a
# JSON true counts as disabled, an unreadable config included, so the paid /
# noisy checks stay off exactly when the loop is off.
_executor_enabled() {
    python3 "$SCRIPT_DIR/cas-pick-item.py" --enabled 2>/dev/null
}

# Remove a lock file whose owner (the process id in it) is not running. The
# loop removes its own stale locks the same way (break_lock in
# scripts/cas-roadmap-loop.sh): only while holding FILE.breaking, a directory,
# and only after reading the owner again there, so that a lock a live loop has
# just taken is never the one removed. $2 "or-empty" also removes a lock with
# nothing in it. Returns 0 when the lock was removed (or would be, in a dry run).
_remove_stale_lock() {
    local file="$1" or_empty="${2:-}" guard="$1.breaking" holder removed=1
    if ! _live; then
        echo "[watchdog] DRY-RUN would: rm -f $file"
        return 0
    fi
    mkdir "$guard" 2>/dev/null || return 1
    holder="$(cat "$file" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
        rm -f "$file"; removed=0
    elif [ -z "$holder" ] && [ "$or_empty" = "or-empty" ]; then
        rm -f "$file"; removed=0
    fi
    rmdir "$guard" 2>/dev/null || true
    return "$removed"
}

# ---------------------------------------------------------------------------
# 1. Stale .git/index.lock
# ---------------------------------------------------------------------------
if [ -f "$INDEX_LOCK" ]; then
    age=$(_file_age "$INDEX_LOCK")
    if [ "$age" -gt 120 ] && ! _git_process_active; then
        _do rm -f "$INDEX_LOCK"
        FIXES+=("removed stale .git/index.lock (${age}s old, no live git process)")
    elif [ "$age" -gt 120 ]; then
        ALERTS+=(".git/index.lock is ${age}s old but a git process IS active; left in place")
    fi
fi

# ---------------------------------------------------------------------------
# 2. Stale cas-executor-merge.lock in the state dir (dead PID)
# ---------------------------------------------------------------------------
if [ -f "$MERGE_LOCK" ]; then
    lock_pid="$(cat "$MERGE_LOCK" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
        if _remove_stale_lock "$MERGE_LOCK"; then
            FIXES+=("removed stale merge_lock (dead PID $lock_pid)")
        fi
    elif [ -z "$lock_pid" ]; then
        # Empty lock file, also stale.
        age=$(_file_age "$MERGE_LOCK")
        if [ "$age" -gt 120 ] && _remove_stale_lock "$MERGE_LOCK" or-empty; then
            FIXES+=("removed empty/stale merge_lock (${age}s old)")
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. Stale cas-executor.lock in the state dir (dead PID + loop not running)
# ---------------------------------------------------------------------------
if [ -f "$MAIN_LOCK" ]; then
    main_pid="$(cat "$MAIN_LOCK" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$main_pid" ] && ! kill -0 "$main_pid" 2>/dev/null && ! _executor_running; then
        if _remove_stale_lock "$MAIN_LOCK"; then
            FIXES+=("removed stale main lock (dead PID $main_pid, loop not running); next launchd tick can acquire")
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 4. Orphan worktrees (>2h old, no running executor + no live child)
# ---------------------------------------------------------------------------
if [ -d "$WORKTREE_DIR" ]; then
    for wt in "$WORKTREE_DIR"/auto-* "$WORKTREE_DIR"/gate-*; do
        [ -d "$wt" ] || continue
        age=$(_file_age "$wt")
        # 2h = 7200s. An item run is ~5-20 min; >2h means the run died. The
        # directory mtime only changes when a top-level entry is added or
        # removed, so also require that no live process (the running Claude
        # session's prompt names its worktree path, and the gate's pytest
        # command names its checkout) mentions this worktree.
        if [ "$age" -gt 7200 ] && ! pgrep -f -- "$wt" >/dev/null 2>&1; then
            wt_name="$(basename "$wt")"
            _do _remove_worktree "$wt"
            FIXES+=("pruned orphan worktree $wt_name (${age}s old, run long dead)")
        fi
    done
    # Reconcile worktree metadata after any removals.
    _live && git worktree prune 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 5. PROJECT_DIR on non-master branch (ALERT: never auto-checkout)
# ---------------------------------------------------------------------------
current_branch="$(git -C "$PROJECT_DIR" branch --show-current 2>/dev/null || echo '?')"
if [ "$current_branch" != "master" ] && [ -n "$current_branch" ] && [ "$current_branch" != "?" ]; then
    ALERTS+=("PROJECT_DIR on branch '$current_branch' (not master); executor preflight yields here; checkout master when safe")
fi

# ---------------------------------------------------------------------------
# 6. Persistent dirty tree that makes the executor yield (ALERT:
#    snapshot-compared, no auto-clean)
# ---------------------------------------------------------------------------
# The tracked files the executor's preflight tolerates as dirty: the same
# expression as DIRTY_OK_RE in scripts/cas-roadmap-loop.sh
# (tests/test_executor_ops_contract.py compares the two). Anything else that
# differs from HEAD, coordinator/roadmap.json included, makes every cycle yield.
DIRTY_OK_RE='(cli/failure_kb\.json|scripts/cas-executor-config\.json|CLAUDE\.md)$'
other_dirty="$(git -C "$PROJECT_DIR" status --porcelain 2>/dev/null | grep -vE "$DIRTY_OK_RE" | grep -v '^??' || true)"
if [ -n "$other_dirty" ]; then
    dirty_sig="$(echo "$other_dirty" | sort | md5 2>/dev/null || echo "$other_dirty" | sort | md5sum | cut -d' ' -f1)"
    prev_sig="$(cat "$DIRTY_SNAPSHOT" 2>/dev/null || echo '')"
    dirty_count="$(echo "$other_dirty" | grep -c . | tr -d ' ')"
    if [ "$dirty_sig" = "$prev_sig" ]; then
        # Same dirty files as last run (~30 min ago); likely a crashed-run
        # leftover, not active typing. Alert; still don't auto-clean (could be
        # a human's paused work). Human resolves.
        ALERTS+=("dirty tree UNCHANGED across 2 watchdog runs (~30+ min): $dirty_count file(s), likely crashed-run leftover, executor is yielding every tick. Resolve manually: $(echo "$other_dirty" | head -3 | tr '\n' ';')")
    fi
    _live && echo "$dirty_sig" > "$DIRTY_SNAPSHOT"
else
    _live && rm -f "$DIRTY_SNAPSHOT"
fi

# ---------------------------------------------------------------------------
# 6b. Unmerged paths (ALERT: never auto-resolved). A stash pop that conflicted
#     leaves conflict markers and an unmerged index entry, and git then refuses
#     both the next stash and the next merge. The loop's preflight yields on the
#     same condition (preflight_check in cas-roadmap-loop.sh). The stash that the
#     pop could not apply stays on the stack for a person to resolve.
# ---------------------------------------------------------------------------
unmerged_list="$(git -C "$PROJECT_DIR" ls-files -u 2>/dev/null | cut -f2 | sort -u | tr '\n' ' ')"
if [ -n "$unmerged_list" ]; then
    ALERTS+=("unmerged paths in PROJECT_DIR (${unmerged_list% }); the executor yields until they are resolved. A stash pop that conflicted keeps its stash: see git stash list")
fi

# ---------------------------------------------------------------------------
# 7. Claude CLI auth failure (ALERT: needs `claude /login`, can't auto-fix)
# ---------------------------------------------------------------------------
# Cheap probe: a 1-token Haiku ping. 401 → auth dead → every executor pick
# fails with "Claude exited 1" in <10s (an outage was exactly this).
# The CLI is the one the loop uses: CLAUDE_BIN, else `claude` on PATH.

# _claude_auth_probe SECONDS BINARY MODEL: send "ping" to BINARY --print and
# print the first 4 KiB of what it answers. The call runs in a session of its
# own and is killed, with everything it started, when it has not finished
# after SECONDS: launchd does not start the next watchdog run while this one
# waits, so a CLI that hangs must not be able to hold it. (macOS ships no
# `timeout`; python3, which this script needs anyway, does the waiting.)
_claude_auth_probe() {
    python3 -c '
import os, signal, subprocess, sys, tempfile

limit, binary, model = float(sys.argv[1]), sys.argv[2], sys.argv[3]
with tempfile.TemporaryFile() as answer:
    probe = subprocess.Popen([binary, "--print", "--model", model], stdin=subprocess.PIPE,
                             stdout=answer, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        probe.communicate(b"ping\n", timeout=limit)
    except subprocess.TimeoutExpired:
        os.killpg(probe.pid, signal.SIGKILL)
        probe.wait()
    answer.seek(0)
    sys.stdout.buffer.write(answer.read(4096))
' "$1" "$2" "$3" 2>/dev/null || true
}

PROBE_TIMEOUT_SEC="${CAS_WATCHDOG_PROBE_TIMEOUT_SEC:-30}"
case "$PROBE_TIMEOUT_SEC" in ''|*[!0-9]*|0) PROBE_TIMEOUT_SEC=30 ;; esac
CLAUDE_CLI="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
if _executor_enabled && [ -n "$CLAUDE_CLI" ] && [ -x "$CLAUDE_CLI" ]; then
    auth_probe="$(_claude_auth_probe "$PROBE_TIMEOUT_SEC" "$CLAUDE_CLI" claude-haiku-4-5-20251001 | head -3 || true)"
    if echo "$auth_probe" | grep -qiE '401|invalid authentication|unauthorized'; then
        ALERTS+=("Claude CLI AUTH FAILURE (401): executor picks will all fail in <10s. Run 'claude /login' interactively. This cannot be auto-fixed.")
    fi
fi

# ---------------------------------------------------------------------------
# 8. Executor idle >90 min WHILE eligible items exist (ALERT; generic block)
# ---------------------------------------------------------------------------
# This is the catch-all for UNKNOWN blocks: items are pickable but nothing's
# happening. Combined with the mechanical checks above, catches both known
# and novel stall causes.
stdout_log="$STATE_DIR/launchd-stdout.log"
if _executor_enabled && [ -f "$stdout_log" ]; then
    log_age=$(_file_age "$stdout_log")
    if [ "$log_age" -gt 5400 ]; then  # 90 min since last log write
        eligible="$(python3 "$SCRIPT_DIR/cas-pick-item.py" --next-n 1 2>/dev/null || echo '[]')"
        if [ -n "$eligible" ] && [ "$eligible" != "[]" ]; then
            ALERTS+=("executor log silent for ${log_age}s (>90min) but eligible items EXIST in queue; executor may be stalled/dead. Check 'pgrep -fl cas-roadmap-loop' + launchd-stderr.log.")
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 8b. Prod deploy staleness (ALERT: executor's pushes not reaching prod)
# ---------------------------------------------------------------------------
# The executor pushes to master; web auto-deploys from master. If prod's
# live commit is many commits behind origin/master, the executor's output
# isn't landing: a Railway deploy stall or a crash-looping deploy. Folded
# in from the retired com.cas.exec-guard-watch AI guard (check 4). Read-only
# + best-effort; never blocks. Skips silently if prod is unreachable or its
# commit isn't in local history (avoids false alerts on an un-fetched SHA).
if [ -n "$PROD_URL" ] && command -v curl >/dev/null 2>&1; then
    prod_commit="$(curl -sf --connect-timeout 5 --max-time 10 "${PROD_URL%/}/health" 2>/dev/null | head -c 65536 | python3 -c "import sys,json; print(json.load(sys.stdin).get('git_commit','')[:40])" 2>/dev/null || echo '')"
    if [ -n "$prod_commit" ] && git -C "$PROJECT_DIR" cat-file -e "${prod_commit}^{commit}" 2>/dev/null; then
        # The remote-tracking branch by its full name: "origin/master" would
        # resolve to a tag of that name first.
        behind="$(git -C "$PROJECT_DIR" rev-list --count "${prod_commit}..refs/remotes/origin/master" 2>/dev/null || echo 0)"
        if [ "${behind:-0}" -gt 15 ]; then
            ALERTS+=("prod web is ${behind} commits behind origin/master (live=${prod_commit:0:8}), executor pushes may not be deploying. Check Railway web service + scripts/wait_for_deploy.py.")
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 9. Executor not running at all (RECOVERY; kickstart the LaunchAgent)
# ---------------------------------------------------------------------------
if _executor_enabled && ! _executor_running; then
    if ! _live; then
        echo "[watchdog] DRY-RUN would: launchctl kickstart -k gui/$(id -u)/$EXECUTOR_LABEL"
    else
        if launchctl kickstart -k "gui/$(id -u)/$EXECUTOR_LABEL" 2>/dev/null; then
            FIXES+=("executor was DOWN: launchctl kickstarted $EXECUTOR_LABEL")
        else
            ALERTS+=("executor is DOWN and launchctl kickstart failed; manual 'launchctl kickstart -k gui/$(id -u)/$EXECUTOR_LABEL' needed")
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
status="OK"
[ "${#FIXES[@]}" -gt 0 ] && status="FIXED"
[ "${#ALERTS[@]}" -gt 0 ] && status="ALERT"

echo "[watchdog $status] $TS (dry_run=$DRY_RUN)"
for f in "${FIXES[@]:-}"; do [ -n "$f" ] && echo "  FIX: $f"; done
for a in "${ALERTS[@]:-}"; do [ -n "$a" ] && echo "  ALERT: $a"; done
[ "$status" = "OK" ] && echo "  all clear; no executor blocks detected"

# Best-effort heartbeat to ops API so the dashboard shows the watchdog ran.
# Bounded: launchd does not start the next run while this one is still waiting
# on a server that never answers. The summary goes in skipped_reason, the text
# field the heartbeat route stores.
if _live && command -v curl >/dev/null 2>&1; then
    OPS_KEY="${OPS_API_KEY:-$(grep '^OPS_API_KEY=' "$PROJECT_DIR/web/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d ' ' || true)}"
    if [ -n "$OPS_KEY" ]; then
        detail="watchdog $status fixes=${#FIXES[@]} alerts=${#ALERTS[@]}"
        # The key goes to curl on its standard input (-H @-), not on its
        # command line, which the process list shows to every user.
        printf 'Authorization: Bearer %s\n' "$OPS_KEY" \
            | curl -sf --connect-timeout 5 --max-time 10 -X POST "$OPS_BASE/ops/api/v1/executor/heartbeat" \
                -H @- -H "Content-Type: application/json" \
                -d "{\"status\": \"watchdog\", \"skipped_reason\": \"$detail\"}" \
                >/dev/null 2>&1 || true
    fi
fi

exit 0
