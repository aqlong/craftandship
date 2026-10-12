#!/usr/bin/env bash
# scripts/sync_staging_from_master.sh: Keep the `staging` branch current with `master`.
#
# WHY THIS EXISTS
# ---------------
# The staging workflow routes real
# code changes through a `staging` branch → Railway staging env → promote to
# master. It is on when `use_staging_workflow` is true in
# `cas-executor-config.json` (the file ships with false): the pre-push hook then
# rejects direct `git push origin master` for anything that isn't a sanctioned
# fast-path escape (CAS_PROMOTE=1) or the executor.
#
# Fast-path pushes (docs, scripts, CI, hooks, tests) go straight to master.
# Each fast-path push silently advances master past staging, and staging does
# not auto-merge. Left unmanaged, `staging` drifts behind `master`, and the
# next real staging-path push either conflicts at merge time or ships against
# a stale base. This script closes that drift in one idempotent command.
#
# WHEN THIS RUNS
# --------------
#   By hand, after a fast-path push: `scripts/sync_staging_from_master.sh`
#   (scripts/classify_push.py says which path a pending push is). Nothing in
#   this repository calls it.
#
# SAFETY CONTRACT
# ---------------
# - Refuses to run if the working tree has uncommitted changes, except three
#   local state files, which it stashes for the branch switch and restores.
# - Saves and restores the caller's current branch on both success and error.
# - On merge conflict: aborts the merge, checks out the original branch, and
#   exits non-zero. Never leaves the repo in a conflicted state.
# - Only ever merges `origin/master` into `staging`. Never touches master,
#   never force-pushes, never rewrites history.
# - Fast-forward preferred; falls back to a regular merge commit with a
#   descriptive message when FF isn't possible (which will eventually happen
#   if someone makes staging-only commits).
# - Respects the pre-push hook: `git push origin staging` still runs the
#   audits and the web unit tests. If that hook blocks, the script exits
#   non-zero and the caller decides what to do.
#
# USAGE
# -----
#   scripts/sync_staging_from_master.sh              # sync + push
#   scripts/sync_staging_from_master.sh --dry-run    # show what would happen
#   scripts/sync_staging_from_master.sh --no-push    # merge locally, don't push
#   scripts/sync_staging_from_master.sh --prefer-staging-on-docs-superset
#       Opt-in: on add/add merge conflicts STRICTLY under docs/plans/**, if
#       staging's version contains every line of master's version (line-wise
#       subset; tolerant of reordering / trailing notes), auto-resolve with
#       staging's version and complete the merge. Bails as today if any
#       conflict is on a non-docs path or fails the subset check. Every
#       auto-resolved path is logged so the human can audit.

set -u

DRY_RUN=0
NO_PUSH=0
PREFER_STAGING_DOCS_SUPERSET=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-push) NO_PUSH=1 ;;
    --prefer-staging-on-docs-superset) PREFER_STAGING_DOCS_SUPERSET=1 ;;
    -h|--help)
      sed -n '1,45p' "$0"
      exit 0
      ;;
    *)
      echo "sync_staging: unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
  echo "sync_staging: not inside a git repo" >&2
  exit 1
fi
cd "$REPO_ROOT" || exit 1

# 1. Bail if working tree is dirty, BUT allow a whitelist of churn files
#    (roadmap.json, failure_kb.json, cas-executor-config.json) to be dirty.
#    Rationale: these files are auto-modified by the worker/executor between
#    commits. If we refused to run while they're dirty, sync would almost
#    never run in practice. Since they're identical on master and staging at
#    the same commit, git checkout will carry them across the branch switch
#    without conflict.
_CHURN_WHITELIST_REGEX='^(cli/failure_kb\.json|coordinator/roadmap\.json|scripts/cas-executor-config\.json)$'
_DIRTY=$(git status --porcelain | awk '$1 ~ /^[MARCDU]/ || $1 ~ /^.[MDU]/ { print $NF }')
_BLOCKING=""
if [ -n "$_DIRTY" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if ! echo "$f" | grep -qE "$_CHURN_WHITELIST_REGEX"; then
      _BLOCKING="$_BLOCKING $f"
    fi
  done <<< "$_DIRTY"
fi
if [ -n "$_BLOCKING" ]; then
  echo "sync_staging: working tree has uncommitted non-churn changes, commit or stash first" >&2
  echo "  blocking files:" >&2
  for f in $_BLOCKING; do echo "    $f" >&2; done
  exit 1
fi

# 2. Save the branch we were on so we can restore it no matter what happens.
ORIGINAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$ORIGINAL_BRANCH" = "HEAD" ]; then
  echo "sync_staging: detached HEAD, refusing to run" >&2
  exit 1
fi

# 2a. Stash any dirty churn files BEFORE switching branches. The whitelist
# above lets them through the bail check, but `git checkout staging` will
# still refuse if the same paths would be overwritten, which happens any
# time master and staging are at different commits (the whole reason sync
# is needed). We stash only the specific churn files, not the entire tree,
# so unrelated untracked/ignored files are untouched.
_STASHED=0
_DIRTY_CHURN_FILES=""
for _f in cli/failure_kb.json coordinator/roadmap.json scripts/cas-executor-config.json; do
  if [ -f "$_f" ] && { ! git diff --quiet -- "$_f" 2>/dev/null || ! git diff --cached --quiet -- "$_f" 2>/dev/null; }; then
    _DIRTY_CHURN_FILES="$_DIRTY_CHURN_FILES $_f"
  fi
done
if [ -n "$_DIRTY_CHURN_FILES" ]; then
  _STASH_COUNT_BEFORE=$(git stash list 2>/dev/null | wc -l | tr -d ' ')
  # shellcheck disable=SC2086  # intentional word-splitting on file list
  git stash push --quiet -m "sync_staging: auto-stash dirty churn files" -- $_DIRTY_CHURN_FILES 2>/dev/null || true
  _STASH_COUNT_AFTER=$(git stash list 2>/dev/null | wc -l | tr -d ' ')
  if [ "$_STASH_COUNT_AFTER" -gt "$_STASH_COUNT_BEFORE" ]; then
    _STASHED=1
    echo "→ Stashed dirty churn files for safe branch switch"
  fi
fi

restore_branch() {
  local rc=$?
  # Abort any in-progress merge before switching branches.
  if [ -f "$REPO_ROOT/.git/MERGE_HEAD" ]; then
    echo "sync_staging: aborting in-progress merge..." >&2
    git merge --abort 2>/dev/null || true
  fi
  if [ "$(git rev-parse --abbrev-ref HEAD)" != "$ORIGINAL_BRANCH" ]; then
    git checkout -q "$ORIGINAL_BRANCH" 2>/dev/null || {
      echo "sync_staging: WARNING, could not restore branch to $ORIGINAL_BRANCH" >&2
    }
  fi
  # Restore stashed churn files AFTER we're back on the original branch.
  # Pop is safe here because the stash was taken from this branch's worktree.
  if [ "${_STASHED:-0}" = "1" ]; then
    git stash pop --quiet 2>/dev/null || echo "sync_staging: WARNING, could not pop churn-file stash (check 'git stash list')" >&2
  fi
  return $rc
}
trap restore_branch EXIT

# 3. Fetch the latest refs. Branches are named in full here and below: a
# short name ("master", "origin/master") is looked up among tags before
# branches, on the remote as well as locally, so a tag of that name would be
# fetched, compared or merged in the branch's place.
echo "→ Fetching origin..."
if ! git fetch origin "+refs/heads/master:refs/remotes/origin/master" \
        "+refs/heads/staging:refs/remotes/origin/staging" >/dev/null 2>&1; then
  echo "sync_staging: git fetch failed" >&2
  exit 1
fi

# 4. Check if staging is already up to date with origin/master.
MASTER_SHA="$(git rev-parse refs/remotes/origin/master)"
STAGING_SHA="$(git rev-parse refs/remotes/origin/staging 2>/dev/null || echo "")"

if [ -z "$STAGING_SHA" ]; then
  echo "sync_staging: origin/staging not found, nothing to sync" >&2
  exit 1
fi

# If origin/master is already reachable from origin/staging, staging is
# already ahead of or equal to master. Nothing to do.
if git merge-base --is-ancestor "$MASTER_SHA" "$STAGING_SHA" 2>/dev/null; then
  echo "✓ staging is already up to date with master (or ahead of it)"
  exit 0
fi

# Count how many commits staging is behind master and how many it's ahead of.
BEHIND="$(git rev-list --count "$STAGING_SHA..$MASTER_SHA")"
AHEAD="$(git rev-list --count "$MASTER_SHA..$STAGING_SHA")"
echo "staging: $BEHIND behind master, $AHEAD ahead of master"

if [ "$DRY_RUN" = "1" ]; then
  echo "→ [dry-run] would checkout staging, merge origin/master, push"
  echo "→ commits that would be merged in:"
  git log --oneline "$STAGING_SHA..$MASTER_SHA" | head -20
  exit 0
fi

# 5. Untracked-file collision check with origin/staging.
#
# `git checkout staging` refuses (and can silently overwrite with -f) when
# untracked files on the current branch exist as tracked files on the target.
# We hit this with docs/plans/se-review-gate work that is untracked on master
# but committed to staging. Bail cleanly BEFORE attempting checkout so we never
# touch the caller's in-progress work.
_UNTRACKED=$(git ls-files --others --exclude-standard)
if [ -n "$_UNTRACKED" ]; then
  _COLLISIONS=""
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if git cat-file -e "$STAGING_SHA:$f" 2>/dev/null; then
      _COLLISIONS="$_COLLISIONS $f"
    fi
  done <<< "$_UNTRACKED"
  if [ -n "$_COLLISIONS" ]; then
    echo "sync_staging: untracked files on $ORIGINAL_BRANCH collide with tracked files on origin/staging:" >&2
    for f in $_COLLISIONS; do echo "    $f" >&2; done
    echo "  These files would be overwritten by 'git checkout staging'." >&2
    echo "  Commit them, move them aside, or delete them before running sync." >&2
    exit 1
  fi
fi

# 6. Check out staging and fast-forward or merge.
echo "→ Checking out staging..."
if ! git checkout -q staging; then
  echo "sync_staging: could not checkout staging" >&2
  exit 1
fi

# Make sure local staging matches origin/staging before merging. If local
# staging has diverged (someone committed directly to it locally), bail.
LOCAL_STAGING="$(git rev-parse refs/heads/staging)"
if [ "$LOCAL_STAGING" != "$STAGING_SHA" ]; then
  echo "sync_staging: local staging ($LOCAL_STAGING) != origin/staging ($STAGING_SHA), fast-forwarding..." >&2
  if ! git merge --ff-only "$STAGING_SHA" >/dev/null 2>&1; then
    echo "sync_staging: local staging has diverged from origin/staging, manual intervention needed" >&2
    exit 1
  fi
fi

# Auto-resolver for add/add conflicts confined to docs/plans/** where staging's
# version is a strict line-wise superset of master's. Opt-in via
# --prefer-staging-on-docs-superset. Returns 0 if it resolved every conflict and
# completed the merge commit; returns 1 if any check failed (caller aborts).
_try_auto_resolve_docs_superset() {
  local conflicted f missing
  conflicted=$(git diff --name-only --diff-filter=U)
  if [ -z "$conflicted" ]; then
    return 1
  fi

  # Gate 1: every conflicted path must be under docs/plans/.
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      docs/plans/*) ;;
      *)
        echo "sync_staging: auto-resolve refused, conflict on non-docs path: $f" >&2
        return 1
        ;;
    esac
  done <<< "$conflicted"

  # Gate 2: every conflicted file must pass the strict line-wise subset check.
  # ":2:$f" = ours (staging), ":3:$f" = theirs (origin/master).
  # We require: every line in master's version appears (somewhere) as a line in
  # staging's version. Tolerant of reordering / appended notes; NOT a plain
  # diff (so reformatting like blank-line shuffling won't false-trip).
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    missing=$(grep -F -x -v -f <(git show ":2:$f" 2>/dev/null) <(git show ":3:$f" 2>/dev/null) 2>/dev/null | wc -l | tr -d ' ')
    if [ "$missing" != "0" ]; then
      echo "sync_staging: auto-resolve refused, staging is NOT a strict superset of master for $f ($missing master line(s) missing)" >&2
      return 1
    fi
  done <<< "$conflicted"

  # Both gates passed. Resolve each with staging's version and stage.
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if ! git checkout --ours -- "$f" >/dev/null 2>&1; then
      echo "sync_staging: auto-resolve failed, could not checkout ours for $f" >&2
      return 1
    fi
    if ! git add -- "$f" >/dev/null 2>&1; then
      echo "sync_staging: auto-resolve failed, could not stage $f" >&2
      return 1
    fi
    echo "    auto-resolved (kept staging): $f"
  done <<< "$conflicted"

  # Complete the merge using the message git already wrote to .git/MERGE_MSG.
  if ! git commit --no-edit >/dev/null 2>&1; then
    echo "sync_staging: auto-resolve failed, could not commit merge" >&2
    return 1
  fi
  return 0
}

# 6. Merge master into staging.
echo "→ Merging origin/master into staging..."
if git merge-base --is-ancestor "$STAGING_SHA" "$MASTER_SHA" 2>/dev/null; then
  # Fast-forward is possible.
  if ! git merge --ff-only "$MASTER_SHA" >/dev/null 2>&1; then
    echo "sync_staging: fast-forward failed unexpectedly" >&2
    exit 1
  fi
  echo "✓ staging fast-forwarded to master ($BEHIND commits)"
else
  # Non-ff merge (staging has its own commits). Create a merge commit.
  if ! git merge --no-ff "$MASTER_SHA" -m "merge(staging): sync with master ($BEHIND commits behind, $AHEAD ahead)" >/dev/null 2>&1; then
    if [ "$PREFER_STAGING_DOCS_SUPERSET" = "1" ] && _try_auto_resolve_docs_superset; then
      echo "✓ merged master into staging with auto-resolved docs/plans/ conflicts ($BEHIND behind / $AHEAD ahead)"
    else
      echo "sync_staging: merge conflict, aborting and restoring $ORIGINAL_BRANCH" >&2
      git merge --abort 2>/dev/null || true
      exit 1
    fi
  else
    echo "✓ merged master into staging ($BEHIND behind / $AHEAD ahead → merge commit)"
  fi
fi

# 7. Push staging (unless --no-push).
if [ "$NO_PUSH" = "1" ]; then
  echo "→ --no-push set; staging updated locally but not pushed"
  exit 0
fi

echo "→ Pushing staging to origin..."
if ! git push origin staging; then
  echo "sync_staging: push failed, staging is synced locally but not on origin" >&2
  exit 1
fi

echo "✓ staging synced with master and pushed to origin"
exit 0
