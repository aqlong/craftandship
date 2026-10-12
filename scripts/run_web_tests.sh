#!/bin/bash
# scripts/run_web_tests.sh: run the web unit tests (Vitest) from anywhere in
# the repo, with a clear message on a fresh clone.
#
# Needs: Node 20+ and the web dependencies installed (cd web && npm ci). The
# unit tests need no database, no network and no environment variables.
#
# Called by .githooks/pre-push. Equivalent to `cd web && npm test`, plus the
# check that dependencies are installed (without it a fresh clone fails with a
# cryptic "vitest: command not found").
#
# Exit status: 0 when every test passes, 1 on a failure or missing dependencies.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT/web" || { echo "[web-tests] Cannot cd to web/"; exit 1; }

if [ ! -d node_modules ]; then
    echo "[web-tests] web/node_modules is missing, so the web tests cannot run."
    echo "[web-tests] Install dependencies first:  cd web && npm ci"
    exit 1
fi

echo "[web-tests] npm test (web/)"
if npm test; then
    echo "[web-tests] PASS"
else
    echo "[web-tests] FAIL"
    exit 1
fi
