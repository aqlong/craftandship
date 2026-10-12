#!/bin/bash
# Run the CAS build worker (queue/worker_pg.py) locally for development.
#
# Usage:
#   ./scripts/run_worker_local.sh
#   MAX_CONCURRENT_BUILDS=1 ./scripts/run_worker_local.sh
#
# Needs:
#   - python3 with the worker dependencies (pip install -r requirements.worker.txt)
#   - the PostgreSQL database the web app uses, already migrated (start the web
#     app once, or `docker compose up -d --wait` and boot web/; the schema is
#     applied by the web server at boot)
#   - for real builds: ANTHROPIC_API_KEY and RAILWAY_API_TOKEN in the
#     environment or web/.env. Without them the worker starts but builds fail.
#
# Configuration: variables already in your environment win; the rest are read
# from web/.env (KEY=VALUE lines only, never evaluated as shell). DATABASE_URL
# defaults to the docker-compose database from the README quickstart.
#
# Safety: the worker may write to the database it is pointed at, so this script
# refuses a DATABASE_URL whose host is not localhost unless
# CAS_ALLOW_REMOTE_DB=1 is set. It also defaults OPS_DRY_RUN=true so the
# self-healing agent only logs what it would do.

set -euo pipefail
cd "$(dirname "$0")/.."

# Load web/.env without evaluating it. Existing environment variables win.
if [ -f web/.env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      *[!A-Za-z0-9_]*|'') continue ;;
    esac
    # Strip one pair of matching surrounding quotes.
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    if ! printenv "$key" >/dev/null 2>&1; then
      export "$key=$val"
    fi
  done < web/.env
else
  echo "warning: web/.env not found; using the current environment only" >&2
fi

# Default to the docker-compose database from the README quickstart.
export DATABASE_URL="${DATABASE_URL:-postgresql://cas:cas@127.0.0.1:5432/craftandship_web}"

DB_HOST="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "")' "$DATABASE_URL")"
case "$DB_HOST" in
  localhost|127.0.0.1|::1|'') ;;
  *)
    if [ "${CAS_ALLOW_REMOTE_DB:-}" != "1" ]; then
      echo "error: DATABASE_URL points at '$DB_HOST', not localhost." >&2
      echo "       This script is for local development; set CAS_ALLOW_REMOTE_DB=1 to override." >&2
      exit 1
    fi
    ;;
esac

# Safety defaults for local dev
export MAX_CONCURRENT_BUILDS="${MAX_CONCURRENT_BUILDS:-1}"
export OPS_DRY_RUN="${OPS_DRY_RUN:-true}"
export MONITOR_INTERVAL="${MONITOR_INTERVAL:-3600}"

echo "Starting CAS worker locally..."
echo "  DB host: ${DB_HOST:-unix socket}"
echo "  MAX_CONCURRENT_BUILDS: $MAX_CONCURRENT_BUILDS"
echo "  OPS_DRY_RUN: $OPS_DRY_RUN"
echo ""

exec python3 queue/worker_pg.py
