#!/usr/bin/env bash
# scripts/verify_hooks.sh: guardrail integrity checker.
#
# Checks that the committed .githooks/ directory:
#   1. is the active hooks path (core.hooksPath points at it locally)
#   2. contains the two guardrail hooks (pre-commit, pre-push)
#   3. has them executable
#   4. holds hooks that still refuse what they exist to refuse. This is
#      established by running them, not by reading them: the two hooks, as
#      they are in this checkout, are installed in a throwaway repository (a
#      temporary directory, removed afterwards) and each gate is handed an
#      input it must refuse and one it must accept:
#        pre-commit, with this checkout's scripts/check_destructive_ddl.py and
#        scripts/check_brand_voice.py:
#          - a staged schema.sql that drops a table is refused; one that only
#            adds a column is accepted
#          - a staged cli/failure_kb.json that is not JSON is refused
#          - a staged view with an em dash is refused; one without is accepted
#          - a staged web/src/app.ts is refused when the SEO lint fails, and
#            accepted when it passes
#        pre-push:
#          - a commit that changes tests/golden/ is accepted with a "bless:"
#            message and refused without one
#          - a push to master is refused while "use_staging_workflow" is true
#            in scripts/cas-executor-config.json
#          - the push is refused when scripts/run_dep_audit.sh,
#            scripts/run_python_security_audit.sh or scripts/run_web_tests.sh
#            fails, and goes through when all three succeed
#          - a push does not run scripts/wait_for_worker.py (the hook once
#            waited for customer builds to finish; that wait was taken out of
#            it on purpose and must not come back unnoticed)
#      For the SEO lint and for those three pre-push scripts the throwaway
#      repository holds stand-ins that only succeed or fail. What is verified
#      there is that the hook runs the script and stops on its failure; what
#      the audits, the web tests and the SEO lint themselves find is not
#      (CI runs the audits and the web tests in jobs of their own, and the
#      Python suite runs the SEO lint on the tree). A hook replaced by
#      `exit 0`, with or without the script names left behind in a comment,
#      fails this step.
#   5. contains nothing else: with core.hooksPath set, every file in it named
#      after a git hook runs on that git operation for whoever enabled the
#      hooks, so a hook nobody reviewed as a gate does not belong there
#
# This runs two ways:
#   - Locally (dev): fails loud if core.hooksPath drifted
#   - In CI: skips the core.hooksPath check (CI has no local git config)
#
# Needs git and python3. Step 4 takes a few seconds.
#
# Exit codes:
#   0: all checks passed
#   1: one or more checks failed
#
# This script is itself called by .github/workflows/hook-integrity.yml so
# that a client-side bypass (e.g. --no-verify, chmod -x) is still caught on
# the server before the push can land.

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || exit 1

FAIL=0
CI_MODE="${CI:-}"

pass() { echo "  ok   $1"; }
fail() { echo "  FAIL $1"; FAIL=1; }

echo "verify_hooks: repo=$REPO_ROOT ci=${CI_MODE:-false}"
echo ""

# 1. core.hooksPath points at .githooks (skip in CI; CI has no local config)
if [ -z "$CI_MODE" ]; then
  echo "[1/5] core.hooksPath points at .githooks"
  CURRENT_PATH="$(git config --get core.hooksPath || echo '')"
  CURRENT_PATH_ABS=""
  if [ -n "$CURRENT_PATH" ]; then
    case "$CURRENT_PATH" in
      /*) CURRENT_PATH_ABS="$CURRENT_PATH" ;;
      *)  CURRENT_PATH_ABS="$REPO_ROOT/$CURRENT_PATH" ;;
    esac
  fi
  EXPECTED_PATH_ABS="$REPO_ROOT/.githooks"
  if [ "$CURRENT_PATH_ABS" = "$EXPECTED_PATH_ABS" ]; then
    pass "core.hooksPath = $CURRENT_PATH"
  else
    fail "core.hooksPath is '$CURRENT_PATH' (abs: '$CURRENT_PATH_ABS'), expected '$EXPECTED_PATH_ABS'"
    echo "       fix: git config core.hooksPath .githooks"
  fi
  echo ""
else
  echo "[1/5] core.hooksPath: skipped in CI"
  echo ""
fi

# 2. Required hook files exist
echo "[2/5] required hook files exist"
REQUIRED_HOOKS=".githooks/pre-commit .githooks/pre-push"
HOOKS_PRESENT=1
for hook in $REQUIRED_HOOKS; do
  if [ -f "$hook" ]; then
    pass "$hook exists"
  else
    fail "$hook is missing"
    HOOKS_PRESENT=0
  fi
done
echo ""

# 3. Required hooks are executable
echo "[3/5] required hooks are executable"
for hook in $REQUIRED_HOOKS; do
  if [ -x "$hook" ]; then
    pass "$hook is executable"
  else
    fail "$hook is not executable (chmod +x $hook)"
    HOOKS_PRESENT=0
  fi
done
echo ""

# 4. The hooks refuse what they exist to refuse
echo "[4/5] hooks refuse what they exist to refuse (run in a throwaway repository)"

# The scripts the throwaway repository gets as stand-ins: each exits with the
# status in .stub-status/<its name>, or 0 when there is none. (The hooks run
# the first with python3 and the others directly.)
STAND_IN_PY="check_seo_consistency.py"
STAND_INS_SH="run_dep_audit.sh run_python_security_audit.sh run_web_tests.sh"
# The scripts it gets as they are in this checkout.
REAL_SCRIPTS="check_destructive_ddl.py check_brand_voice.py"

WORK=""
cleanup_work() { [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup_work EXIT

# git, in the throwaway repository, away from the caller's configuration,
# identity and bypass variables.
tgit() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u CAS_PROMOTE -u CAS_EXECUTOR -u CAS_SKIP_AUDIT \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_AUTHOR_NAME=verify-hooks GIT_AUTHOR_EMAIL=verify-hooks@example.invalid \
    GIT_COMMITTER_NAME=verify-hooks GIT_COMMITTER_EMAIL=verify-hooks@example.invalid \
    git -C "$WORK/repo" "$@"
}

stub_status() { printf '%s\n' "$2" > "$WORK/repo/.stub-status/$1"; }

# refused LABEL COMMAND...: the command must fail. accepted: it must succeed.
refused() {
  local label="$1"; shift
  if "$@" >"$WORK/last-output" 2>&1; then
    fail "$label: accepted, and it must be refused"
  else
    pass "$label: refused"
  fi
}
accepted() {
  local label="$1"; shift
  if "$@" >"$WORK/last-output" 2>&1; then
    pass "$label: accepted"
  else
    fail "$label: refused, and it must be accepted"
    sed 's/^/         | /' "$WORK/last-output" | tail -n 8
  fi
}

# Stage the working tree of the throwaway repository and try to commit it.
try_commit() { tgit add -A && tgit commit -q -m "$1"; }
# Throw away what a refused commit left staged.
discard() { tgit reset -q --hard; }

run_hook_behaviour() {
  local name
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/cas-verify-hooks.XXXXXX")" || { fail "cannot create a temporary directory"; return; }
  mkdir -p "$WORK/repo/.githooks" "$WORK/repo/scripts" "$WORK/repo/.stub-status" \
           "$WORK/repo/web/src/db" "$WORK/repo/web/views" "$WORK/repo/cli" "$WORK/repo/tests/golden"
  cp .githooks/pre-commit .githooks/pre-push "$WORK/repo/.githooks/" || { fail "cannot copy the hooks"; return; }
  chmod +x "$WORK/repo/.githooks/pre-commit" "$WORK/repo/.githooks/pre-push"
  for name in $REAL_SCRIPTS; do
    if ! cp "scripts/$name" "$WORK/repo/scripts/$name" 2>/dev/null; then
      fail "scripts/$name is missing: the pre-commit hook cannot do its check without it"
      return
    fi
  done
  for name in $STAND_INS_SH; do
    printf '#!/bin/sh\n# stand-in written by verify_hooks.sh\nexit "$(cat "$(dirname "$0")/../.stub-status/%s" 2>/dev/null || echo 0)"\n' "$name" \
      > "$WORK/repo/scripts/$name"
    chmod +x "$WORK/repo/scripts/$name"
  done
  {
    echo '# stand-in written by verify_hooks.sh'
    echo 'import pathlib, sys'
    echo "status = pathlib.Path(__file__).resolve().parent.parent / '.stub-status' / '$STAND_IN_PY'"
    echo 'sys.exit(int(status.read_text().strip()) if status.exists() else 0)'
  } > "$WORK/repo/scripts/$STAND_IN_PY"
  # A stand-in that records being run: the hook must not run it at all.
  printf '#!/bin/sh\n: > "$(dirname "$0")/../.stub-status/wait-for-worker-ran"\nexit 0\n' \
    > "$WORK/repo/scripts/wait_for_worker.py"
  chmod +x "$WORK/repo/scripts/wait_for_worker.py"
  printf '{"use_staging_workflow": false}\n' > "$WORK/repo/scripts/cas-executor-config.json"
  printf '.stub-status/\n' > "$WORK/repo/.gitignore"
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    git init -q -b master "$WORK/repo" || { fail "cannot create the throwaway repository"; return; }
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    git init -q --bare -b master "$WORK/origin.git" || { fail "cannot create the throwaway remote"; return; }
  tgit remote add origin "$WORK/origin.git"
  # what `make hooks` does
  tgit config core.hooksPath .githooks

  # ---- pre-commit ----
  printf 'CREATE TABLE IF NOT EXISTS widgets (id SERIAL PRIMARY KEY);\n' > "$WORK/repo/web/src/db/schema.sql"
  printf '{}\n' > "$WORK/repo/cli/failure_kb.json"
  printf '<p>Plain copy.</p>\n' > "$WORK/repo/web/views/page.ejs"
  printf 'export const app = 1;\n' > "$WORK/repo/web/src/app.ts"
  printf '{"pinned": 1}\n' > "$WORK/repo/tests/golden/fixture.json"
  accepted "pre-commit: a first commit with a harmless schema.sql, valid failure_kb.json and plain copy" \
    try_commit "start"

  printf 'ALTER TABLE widgets ADD COLUMN IF NOT EXISTS name TEXT;\nDROP TABLE widgets;\n' >> "$WORK/repo/web/src/db/schema.sql"
  refused "pre-commit: a staged schema.sql that drops a table (scripts/check_destructive_ddl.py)" \
    try_commit "drop a table"
  discard
  printf 'ALTER TABLE widgets ADD COLUMN IF NOT EXISTS name TEXT;\n' >> "$WORK/repo/web/src/db/schema.sql"
  accepted "pre-commit: a staged schema.sql that only adds a column (scripts/check_destructive_ddl.py)" \
    try_commit "add a column"

  # A statement that changes rows needs a reason of its own; one that sets a
  # build live is refused whatever reason is given. The second is written as a
  # DO block with a tag, which a reader that toggled on $$ alone did not see into.
  printf '%s\n' 'DO $$' 'BEGIN' "  UPDATE widgets SET name = 'x' WHERE name IS NULL;" 'END $$;' \
    >> "$WORK/repo/web/src/db/schema.sql"
  refused "pre-commit: a staged schema.sql with a row change that has no reason (scripts/check_destructive_ddl.py)" \
    try_commit "update rows"
  discard
  printf '%s\n' 'DO $$' 'BEGIN' '  -- SAFE: a one-time backfill' "  UPDATE widgets SET name = 'x' WHERE name IS NULL;" 'END $$;' \
    >> "$WORK/repo/web/src/db/schema.sql"
  accepted "pre-commit: a staged schema.sql with a row change that has a reason of its own (scripts/check_destructive_ddl.py)" \
    try_commit "backfill"
  printf '%s\n' 'DO $m$' 'BEGIN' '  -- SAFE: a reason' "  UPDATE builds SET status = 'live' WHERE id IS NOT NULL;" 'END' '$m$;' \
    >> "$WORK/repo/web/src/db/schema.sql"
  refused "pre-commit: a staged schema.sql with a tagged DO block that sets a build live (scripts/check_destructive_ddl.py)" \
    try_commit "set a build live"
  discard

  printf '{"not": json\n' > "$WORK/repo/cli/failure_kb.json"
  refused "pre-commit: a staged cli/failure_kb.json that is not JSON" try_commit "break the failure KB"
  discard

  # the em dash is written by its bytes, so that this file holds none
  printf '<p>Before \342\200\224 after.</p>\n' > "$WORK/repo/web/views/page.ejs"
  refused "pre-commit: a staged view with an em dash (scripts/check_brand_voice.py)" try_commit "an em dash"
  discard
  printf '<p>Before, and after.</p>\n' > "$WORK/repo/web/views/page.ejs"
  accepted "pre-commit: a staged view without one (scripts/check_brand_voice.py)" try_commit "copy edit"

  printf 'export const app = 2;\n' > "$WORK/repo/web/src/app.ts"
  stub_status check_seo_consistency.py 1
  refused "pre-commit: a staged web/src/app.ts while scripts/check_seo_consistency.py fails" try_commit "app"
  discard
  stub_status check_seo_consistency.py 0
  printf 'export const app = 3;\n' > "$WORK/repo/web/src/app.ts"
  accepted "pre-commit: a staged web/src/app.ts while scripts/check_seo_consistency.py passes" try_commit "app"

  # ---- pre-push ----
  accepted "pre-push: a push while run_dep_audit.sh, run_python_security_audit.sh and run_web_tests.sh all succeed" \
    tgit push -q origin master
  if [ -e "$WORK/repo/.stub-status/wait-for-worker-ran" ]; then
    fail "pre-push: the push ran scripts/wait_for_worker.py, and the hook must not wait for the worker"
  else
    pass "pre-push: the push did not run scripts/wait_for_worker.py"
  fi
  printf 'more\n' > "$WORK/repo/notes.txt"
  try_commit "notes" >/dev/null 2>&1
  for name in run_dep_audit.sh run_python_security_audit.sh run_web_tests.sh; do
    stub_status "$name" 1
    refused "pre-push: a push while scripts/$name fails" tgit push -q origin master
    stub_status "$name" 0
  done
  accepted "pre-push: the same push once all three succeed again" tgit push -q origin master

  printf '{"pinned": 2}\n' > "$WORK/repo/tests/golden/fixture.json"
  try_commit "bless: fixture - verify_hooks" >/dev/null 2>&1
  accepted "pre-push: a commit that changes tests/golden/ with a bless: message" tgit push -q origin master
  printf '{"pinned": 3}\n' > "$WORK/repo/tests/golden/fixture.json"
  try_commit "change a golden fixture" >/dev/null 2>&1
  refused "pre-push: a commit that changes tests/golden/ without a bless: message" tgit push -q origin master
  # the unblessed commit must not be what refuses the next push
  tgit reset -q --hard HEAD~1

  printf '{"use_staging_workflow": true}\n' > "$WORK/repo/scripts/cas-executor-config.json"
  try_commit "staging workflow on" >/dev/null 2>&1
  refused "pre-push: a push to master while use_staging_workflow is true" tgit push -q origin master
}

if [ "$HOOKS_PRESENT" -eq 1 ]; then
  run_hook_behaviour
else
  fail "the hooks were not run: one is missing or not executable (see above)"
fi
cleanup_work
WORK=""
echo ""

# 5. Nothing but the two guardrail hooks lives in .githooks/
echo "[5/5] .githooks/ holds only the guardrail hooks"
EXTRA_HOOKS=""
for entry in .githooks/* .githooks/.[!.]*; do
  [ -e "$entry" ] || continue
  case "$entry" in
    .githooks/pre-commit|.githooks/pre-push) ;;
    *) EXTRA_HOOKS="$EXTRA_HOOKS $entry" ;;
  esac
done
if [ -z "$EXTRA_HOOKS" ]; then
  pass ".githooks/ contains pre-commit and pre-push only"
else
  fail ".githooks/ contains other files:$EXTRA_HOOKS"
  echo "       a file there named after a git hook runs for everyone who ran 'make hooks'."
fi
echo ""

if [ "$FAIL" -eq 0 ]; then
  echo "verify_hooks: PASSED"
  exit 0
else
  echo "verify_hooks: FAILED"
  exit 1
fi
