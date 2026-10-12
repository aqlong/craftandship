#!/bin/sh
# Runs Python security audits against the worker + cli + ops surface so
# vulnerable patterns and vulnerable deps are caught BEFORE a push reaches
# GitHub. Mirrors scripts/run_dep_audit.sh (which audits web/ npm deps) and
# is wired into both .githooks/pre-push and .github/workflows/test.yml so
# the gate exists in both places.
#
# Two scanners:
#   1. bandit: static-analysis on Python source (worker, coordinator,
#                ops, scripts, cli, compose). Threshold: medium+ severity
#                AND medium+ confidence. There is no baseline file: the tree
#                is clean at this threshold, so any finding fails the gate and
#                must be fixed or carry a `# nosec BXXX  # <reason>` comment
#                on the offending line.
#   2. pip-audit: checks requirements.worker.txt + requirements-dev.txt
#                  against PyPI's advisory DB. No baseline; any vuln fails.
#
# Tools-missing policy:
#   - In CI (env CI=true / GITHUB_ACTIONS=true): tools are MANDATORY. Missing
#     tools mean requirements-dev.txt didn't get installed, which is a
#     workflow regression: fail loudly.
#   - Local pre-push: tools are OPTIONAL. We warn + exit 0 so a fresh
#     checkout without bandit installed doesn't block a push. CI is the
#     enforcer; local is a courtesy.
#
# Opt-out: set CAS_SKIP_AUDIT=1 (same env var as run_dep_audit.sh). Use
# sparingly and document the reason in the commit message.

set -e

ROOT="$(git rev-parse --show-toplevel)"

if [ "$CAS_SKIP_AUDIT" = "1" ]; then
  echo "run_python_security_audit.sh: CAS_SKIP_AUDIT=1 set, skipping."
  exit 0
fi

# Detect CI environment. GitHub Actions sets both CI=true and GITHUB_ACTIONS=true.
IS_CI="false"
if [ "$CI" = "true" ] || [ "$GITHUB_ACTIONS" = "true" ]; then
  IS_CI="true"
fi

bandit_missing="false"
pip_audit_missing="false"
command -v bandit >/dev/null 2>&1 || bandit_missing="true"
command -v pip-audit >/dev/null 2>&1 || pip_audit_missing="true"

if [ "$bandit_missing" = "true" ] || [ "$pip_audit_missing" = "true" ]; then
  if [ "$IS_CI" = "true" ]; then
    echo "run_python_security_audit.sh: tools missing in CI."
    [ "$bandit_missing" = "true" ] && echo "  bandit not on PATH"
    [ "$pip_audit_missing" = "true" ] && echo "  pip-audit not on PATH"
    echo "  Both should be installed via requirements-dev.txt."
    exit 1
  fi
  echo "run_python_security_audit.sh: tools missing locally, skipping (CI will enforce)."
  [ "$bandit_missing" = "true" ] && echo "  Install: pip3 install --user bandit       (or: pipx install bandit)"
  [ "$pip_audit_missing" = "true" ] && echo "  Install: pip3 install --user pip-audit   (or: pipx install pip-audit)"
  exit 0
fi

cd "$ROOT"

# bandit: Python static analysis.
# Scope: worker + cli + ops surface. Excludes tests (asserts allowed),
# node_modules, .venv, builds/ (generated customer apps). web/ is JS/TS,
# audited by run_dep_audit.sh (npm) instead.
SCAN_DIRS="cli/ queue/ coordinator/ monitor/ ops/ compose/ scripts/"
EXCLUDES='**/tests/**,**/test_*.py,**/.venv/**,**/venv/**,**/node_modules/**,**/builds/**'

bandit_args="-r $SCAN_DIRS -ll -ii --exclude $EXCLUDES -q"

echo "run_python_security_audit.sh: bandit ($SCAN_DIRS, medium+sev / medium+conf)..."
# shellcheck disable=SC2086
if ! bandit $bandit_args; then
  echo ""
  echo "BLOCKED: bandit found security issues (medium+ severity, medium+ confidence)."
  echo ""
  echo "Fix:"
  echo "  bandit -r $SCAN_DIRS -ll -ii --exclude '$EXCLUDES'"
  echo "                                                 # see the full report"
  echo "  Then either fix the code OR add a '# nosec BXXX  # <reason>' comment"
  echo "  on the offending line."
  echo ""
  echo "Bypass (only with documented reason): CAS_SKIP_AUDIT=1 git push ..."
  exit 1
fi

# pip-audit: Python dependency CVE scan.
# Scope: worker requirements + dev/test requirements. Generated customer
# apps have their own deps; the worker container is what's exposed to the
# internet, plus dev/test deps run in CI.
echo "run_python_security_audit.sh: pip-audit (requirements.worker.txt + requirements-dev.txt)..."
if ! pip-audit -r requirements.worker.txt -r requirements-dev.txt --strict; then
  echo ""
  echo "BLOCKED: pip-audit found vulnerable Python dependencies."
  echo ""
  echo "Fix:"
  echo "  pip-audit -r requirements.worker.txt -r requirements-dev.txt"
  echo "                                                 # see the full report"
  echo "  Then bump the affected pin in the matching requirements*.txt"
  echo "  and re-run pip-audit until clean."
  echo ""
  echo "Bypass (only with documented reason): CAS_SKIP_AUDIT=1 git push ..."
  exit 1
fi

echo "run_python_security_audit.sh: clean."
