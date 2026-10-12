#!/bin/sh
# Runs `npm audit` against web/ so vulnerable dependencies are caught
# BEFORE a push reaches GitHub (and therefore Dependabot). Wired into
# .githooks/pre-push (blocking) and the advisory `audit` job in
# .github/workflows/test.yml, so the same check runs locally and in CI.
#
# Needs: npm on PATH and network access to the npm registry (npm audit reads
# web/package-lock.json and queries the registry; no `npm ci` is required).
# An unreachable registry makes `npm audit` fail, which this script reports as
# a block; use CAS_SKIP_AUDIT=1 when working offline.
#
# Scope: only web/ (the one Node package in this repo that has a lockfile).
# Generated customer apps live under builds/ (gitignored) and are not audited
# here.
#
# Threshold: --audit-level=high --omit=dev. Dev-only vulns don't ship;
# high+ is the severity Dependabot surfaces by default on the repo
# security tab.
#
# Opt-out: set CAS_SKIP_AUDIT=1. Use sparingly and say why in the commit
# message; the CI `audit` job re-runs the check (advisory, not a gate).

set -e

ROOT="$(git rev-parse --show-toplevel)"

if [ "$CAS_SKIP_AUDIT" = "1" ]; then
  echo "run_dep_audit.sh: CAS_SKIP_AUDIT=1 set, skipping."
  exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "run_dep_audit.sh: npm not found on PATH, cannot audit."
  exit 1
fi

cd "$ROOT/web"

echo "run_dep_audit.sh: auditing web/ (high+, prod deps only)..."
if ! npm audit --audit-level=high --omit=dev; then
  echo ""
  echo "BLOCKED: npm audit found high+ severity vulnerabilities in web/."
  echo ""
  echo "Fix:"
  echo "  cd web && npm audit           # see the full report"
  echo "  cd web && npm audit fix       # auto-upgrade safe cases"
  echo "  cd web && npm i <pkg>@<ver>   # manual pin for breaking upgrades"
  echo ""
  echo "Bypass (only with documented reason): CAS_SKIP_AUDIT=1 git push ..."
  exit 1
fi

echo "run_dep_audit.sh: clean."
