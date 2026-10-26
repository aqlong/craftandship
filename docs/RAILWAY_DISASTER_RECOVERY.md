# Railway Disaster Recovery Playbook

> Historical document from April 2026; the system is no longer developed and the site was taken offline in October 2026.
>
> Written 2026-04-09 after production was deleted twice in one day.
> It was the single source of truth for recovering CraftAndShip on Railway.

---

## Table of Contents

1. [What Happened (2026-04-09)](#what-happened)
2. [Recovery Checklist](#recovery-checklist)
3. [Step-by-Step Recovery](#step-by-step-recovery)
4. [Where to Find Secrets](#where-to-find-secrets)
5. [Env Var Reference](#env-var-reference)
6. [DNS and Custom Domain Setup](#dns-and-custom-domain-setup)
7. [Post-Recovery Verification](#post-recovery-verification)
8. [Safety Guardrails (prevent recurrence)](#safety-guardrails)
9. [Failover and Multi-Provider Options (planning notes)](#failover-and-multi-provider-options-planning-notes)

---

## What Happened

**First deletion:** A background Claude Code agent was spawned with Railway API write access. It ran a `projectDelete` mutation on the production project.

**Second deletion:** After rebuilding, a code push triggered the staging worker to redeploy. The worker's `_sweep_orphan_railway_projects()` function classified production as an "orphan" and deleted it again via `destroy_project()`.

**Root cause:** No guardrails on Railway project deletion. The orphan sweep was not gated to production-only, and `destroy_project()` had no name-based protection.

**Data saved:** a database backup taken earlier that night and kept on the operator's machine (`<BACKUP_DIR>/prod_db_<timestamp>.sql`). All source code in git. All secrets except SENTRY_DSN recoverable from local files or CLI tools.

---

## Recovery Checklist

Quick reference: each item links to the detailed section below.

| # | Step | Time | Needs |
|---|------|------|-------|
| 1 | [Verify backups exist](#1-verify-backups) | 2 min | Local filesystem |
| 2 | [Create Railway project](#2-create-railway-project) | 2 min | Railway API token |
| 3 | [Create and configure Postgres](#3-create-postgres) | 5 min | Railway API |
| 4 | [Restore database](#4-restore-database) | 10 min | Backup file, temp restore service |
| 5 | [Deploy web service](#5-deploy-web-service) | 5 min | GitHub repo access |
| 6 | [Deploy worker service](#6-deploy-worker-service) | 5 min | GitHub repo access |
| 7 | [Set all env vars](#7-set-env-vars) | 10 min | Secrets (see table below) |
| 8 | [Add custom domains](#8-add-custom-domains) | 2 min | Railway API |
| 9 | [Update DNS in Cloudflare](#9-update-dns) | 5 min | Cloudflare dashboard |
| 10 | [Verify everything works](#10-verify) | 5 min | curl |
| 11 | [Update codebase IDs](#11-update-codebase-ids) | 15 min | git |
| 12 | [Cleanup temp resources](#12-cleanup) | 2 min | Railway API |

**Total estimated time: ~70 minutes** (mostly waiting for deploys)

---

## Step-by-Step Recovery

### Prerequisites

```bash
# Railway API token (workspace-scoped, kept in your secret store)
TOKEN="<your-railway-api-token>"

# Helper function for Railway GraphQL
railway() {
  curl -s -X POST https://backboard.railway.app/graphql/v2 \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"query\": \"$1\"}" 2>&1
}
```

### 1. Verify Backups

```bash
# Check for DB backups (newest first)
ls -lt <BACKUP_DIR>/prod_db_*.sql 2>/dev/null

# Verify backup is valid SQL
head -5 <BACKUP_DIR>/prod_db_*.sql
wc -l <BACKUP_DIR>/prod_db_*.sql
```

Backups are created by:
- `monitor/backup.py` daily at 03:00 UTC, which dumps the databases of the apps in the monitor registry (the customer apps the worker registers after a deploy, and anything added with `monitor.cli register`) into `monitor/backups/<app name>__<registry id>/`, or the same directory name under `CAS_BACKUP_DIR` when set (that default location is lost on every redeploy, so production needs `CAS_BACKUP_DIR` on a mounted Railway volume). No code in this repository registers the platform's own database, so these dumps are not a backup of it
- Manual `pg_dump` runs of the platform database, kept outside Railway

If no local backup exists, check the Railway volume (if the Postgres service survived) or ask Railway support about their 48-hour soft-delete window.

### 2. Create Railway Project

```bash
railway 'mutation { projectCreate(input: { name: "<platform-project-name>" }) { id environments { edges { node { id name } } } } }'
```

Save the returned `project_id` and `environment_id`. Choose a name that the worker's delete guard protects (a name starting with `craftandship`, or one listed in `PROTECTED_RAILWAY_PROJECT_NAMES`; see Safety Guardrails), so that the recovered platform project is never a candidate for deletion by name.

### 3. Create Postgres

```bash
PROJECT_ID="<from step 2>"
ENV_ID="<from step 2>"

# Create Postgres service with Docker image
railway "mutation { serviceCreate(input: { projectId: \"$PROJECT_ID\", name: \"postgres\", source: { image: \"postgres:16-alpine\" } }) { id } }"

PG_SERVICE_ID="<returned id>"
PGPW=$(openssl rand -hex 16)

# Set Postgres env vars
for VAR in \
  "POSTGRES_USER=railway" \
  "POSTGRES_PASSWORD=$PGPW" \
  "POSTGRES_DB=railway" \
  "PGDATA=/var/lib/postgresql/data/pgdata"; do
  NAME="${VAR%%=*}"
  VALUE="${VAR#*=}"
  railway "mutation { variableUpsert(input: { projectId: \"$PROJECT_ID\", environmentId: \"$ENV_ID\", serviceId: \"$PG_SERVICE_ID\", name: \"$NAME\", value: \"$VALUE\" }) }"
done

# Create persistent volume (CRITICAL: PGDATA must be a subdirectory, not the mount root)
railway "mutation { volumeCreate(input: { projectId: \"$PROJECT_ID\", serviceId: \"$PG_SERVICE_ID\", mountPath: \"/var/lib/postgresql/data\" }) { id } }"
```

Wait ~60s for Postgres to start.

### 4. Restore Database

Railway has **no `tcpProxyCreate` API mutation**, so a TCP proxy can only be enabled via the dashboard. To restore a backup without dashboard access, create a temporary HTTP-to-psql bridge service:

```bash
# Create a temp branch with restore service code
mkdir -p scripts/db-restore
cat > scripts/db-restore/Dockerfile << 'DOCKERFILE'
FROM postgres:16
RUN apt-get update && apt-get install -y python3 && rm -rf /var/lib/apt/lists/*
COPY server.py /server.py
CMD ["python3", "/server.py"]
DOCKERFILE

cat > scripts/db-restore/server.py << 'PYTHON'
import http.server, subprocess, os, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            # Quick DB connectivity check
            r = subprocess.run(['psql', os.environ['DATABASE_URL'], '-c', 'SELECT count(*) FROM information_schema.tables'], capture_output=True, text=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(r.stdout.encode())

    def do_POST(self):
        if self.path == '/restore':
            length = int(self.headers['Content-Length'])
            data = self.rfile.read(length)
            r = subprocess.run(['psql', os.environ['DATABASE_URL']], input=data, capture_output=True, text=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"stdout: {r.stdout[-500:]}\nstderr: {r.stderr[-500:]}".encode())

http.server.HTTPServer(('', int(os.environ.get('PORT', 8080))), Handler).serve_forever()
PYTHON

git checkout -b temp/db-restore
git add scripts/db-restore/
git commit -m "temp: DB restore service"
git push origin temp/db-restore
```

```bash
# Create restore service on Railway connected to the temp branch
railway "mutation { serviceCreate(input: { projectId: \"$PROJECT_ID\", name: \"db-restore\", source: { repo: \"<GITHUB_ORG>/<REPO>\" } }) { id } }"

RESTORE_ID="<returned id>"

# Configure it
railway "mutation { serviceInstanceUpdate(serviceId: \"$RESTORE_ID\", input: { rootDirectory: \"scripts/db-restore\", source: { repo: \"<GITHUB_ORG>/<REPO>\", branch: \"temp/db-restore\" } }) }"

# Set DATABASE_URL pointing to internal Postgres
railway "mutation { variableUpsert(input: { projectId: \"$PROJECT_ID\", environmentId: \"$ENV_ID\", serviceId: \"$RESTORE_ID\", name: \"DATABASE_URL\", value: \"postgresql://railway:$PGPW@postgres.railway.internal:5432/railway\" }) }"

# Add a public domain
railway "mutation { serviceDomainCreate(input: { environmentId: \"$ENV_ID\", serviceId: \"$RESTORE_ID\" }) { domain } }"

RESTORE_URL="<returned domain>"
```

Wait for deploy, then restore:

```bash
# Upload backup
curl -X POST "https://$RESTORE_URL/restore" \
  --data-binary @<BACKUP_DIR>/prod_db_<timestamp>.sql

# Verify table counts
curl "https://$RESTORE_URL/health"

# Also run schema.sql for any additive migrations
curl -X POST "https://$RESTORE_URL/restore" \
  --data-binary @web/src/db/schema.sql
```

### 5. Deploy Web Service

```bash
# Create web service connected to GitHub master branch
railway "mutation { serviceCreate(input: { projectId: \"$PROJECT_ID\", name: \"craftandship-web\", source: { repo: \"<GITHUB_ORG>/<REPO>\" } }) { id } }"

WEB_ID="<returned id>"

# Set root directory and branch
railway "mutation { serviceInstanceUpdate(serviceId: \"$WEB_ID\", input: { rootDirectory: \"web/\", source: { repo: \"<GITHUB_ORG>/<REPO>\", branch: \"master\" } }) }"

# Create Railway domain
railway "mutation { serviceDomainCreate(input: { environmentId: \"$ENV_ID\", serviceId: \"$WEB_ID\" }) { domain } }"
```

Then set all web env vars (see [Env Var Reference](#env-var-reference)).

### 6. Deploy Worker Service

```bash
# Create worker service connected to GitHub
railway "mutation { serviceCreate(input: { projectId: \"$PROJECT_ID\", name: \"craftandship-worker\", source: { repo: \"<GITHUB_ORG>/<REPO>\" } }) { id } }"

WORKER_ID="<returned id>"

# Set Dockerfile path and watch patterns
railway "mutation { serviceInstanceUpdate(serviceId: \"$WORKER_ID\", input: { rootDirectory: \"/\", builder: \"DOCKERFILE\", dockerfilePath: \"Dockerfile.worker\", watchPatterns: [\"cli/**\", \"compose/**\", \"queue/**\", \"coordinator/**\", \"monitor/**\", \"ops/**\", \"Dockerfile.worker\", \"requirements.worker.txt\", \"railway.toml\"] }) }"
```

Then set all worker env vars (see [Env Var Reference](#env-var-reference)).

### 7. Set Env Vars

See the full [Env Var Reference](#env-var-reference) section below. Use `variableUpsert` for each.

**Tip:** For values containing special characters, use Python to safely JSON-encode:

```bash
python3 -c "
import json, subprocess
token = 'YOUR_RAILWAY_TOKEN'
vars_to_set = {
    'VAR_NAME': 'value_with_special_chars'
}
for name, value in vars_to_set.items():
    query = json.dumps({'query': f'mutation {{ variableUpsert(input: {{projectId: \"PROJECT_ID\", environmentId: \"ENV_ID\", serviceId: \"SERVICE_ID\", name: \"{name}\", value: {json.dumps(value)}}}) }}'})
    subprocess.run(['curl', '-s', '-X', 'POST', 'https://backboard.railway.app/graphql/v2',
        '-H', f'Authorization: Bearer {token}', '-H', 'Content-Type: application/json',
        '-d', query], capture_output=True)
    print(f'Set {name}')
"
```

### 8. Add Custom Domains

```bash
# Root domain
railway "mutation { customDomainCreate(input: { domain: \"craftandship.io\", projectId: \"$PROJECT_ID\", environmentId: \"$ENV_ID\", serviceId: \"$WEB_ID\" }) { id domain status { dnsRecords { requiredValue hostlabel } } } }"

# WWW subdomain
railway "mutation { customDomainCreate(input: { domain: \"www.craftandship.io\", projectId: \"$PROJECT_ID\", environmentId: \"$ENV_ID\", serviceId: \"$WEB_ID\" }) { id domain status { dnsRecords { requiredValue hostlabel } } } }"
```

Note the returned CNAME targets; you'll need them for Cloudflare.

### 9. Update DNS

In Cloudflare (dash.cloudflare.com):

| Type | Name | Target | Proxy |
|------|------|--------|-------|
| CNAME | `@` | `<target from step 8>` | Proxied (orange cloud) |
| CNAME | `www` | `<target from step 8>` | Proxied (orange cloud) |

### 10. Verify

```bash
BASE="https://cas-web-example.up.railway.app"
PW="$PREVIEW_PASSWORD_IF_SET"
OPS_KEY="$OPS_API_KEY"

# Health check
curl -sf -u ":$PW" "$BASE/health" | python3 -m json.tool

# Ops API
curl -sf -H "Authorization: Bearer $OPS_KEY" "$BASE/ops/api/v1/stats"

# Build count
curl -sf -H "Authorization: Bearer $OPS_KEY" "$BASE/ops/api/v1/builds" | python3 -c "import json,sys; builds=json.load(sys.stdin); print(f'{len(builds)} builds')"

# Custom domain (after DNS propagation)
curl -sf -u ":$PW" "https://craftandship.io/health"
```

### 11. Update Codebase IDs

After recovery, these files need the new project/service/environment IDs:

| File | What to update |
|------|---------------|
| Environment of whoever runs `scripts/wait_for_deploy.py` | `CAS_PROD_URL`, `RAILWAY_WEB_SERVICE_ID`, `RAILWAY_WORKER_SERVICE_ID`, `RAILWAY_ENVIRONMENT_ID` (the script reads them from the environment; nothing is hardcoded in it) |
| Any operator script or configuration kept outside the repository | Project, environment and service IDs, remote host and port |
| Any command or doc file | Hardcoded URLs |

Search for old URLs/IDs:

```bash
# Find all references to old project ID
grep -r "OLD_PROJECT_ID" --include="*.py" --include="*.ts" --include="*.md" --include="*.sh" --include="*.json" --include="*.yml" .

# Find all references to old Railway URL
grep -r "old-subdomain.up.railway.app" --include="*.py" --include="*.ts" --include="*.md" --include="*.sh" --include="*.json" --include="*.yml" .
```

Also update wherever you keep the project and service IDs for operators.

### 12. Cleanup

```bash
# Delete restore service
railway "mutation { serviceDelete(id: \"$RESTORE_ID\") }"

# Delete temp git branch
git push origin --delete temp/db-restore
git branch -D temp/db-restore
```

Also:
- Enable TCP proxy on Postgres (Railway dashboard only)
- Enable worker volume for build artifacts (Railway dashboard)

---

## Where to Find Secrets

If a secret is lost, here's where to recover each one:

| Secret | Primary Location | Backup Location | If Truly Lost |
|--------|-----------------|-----------------|---------------|
| **Railway API Token** | Secret store | Railway dashboard: railway.app/account/tokens | Create new token at railway.app/account/tokens |
| **RAILWAY_API_TOKEN** (customer deploys) | Secret store | Railway dashboard (token list) | Create new project-scoped token |
| **ANTHROPIC_API_KEY** | `web/.env` (local) | Anthropic Console: console.anthropic.com/settings/keys | Regenerate at console.anthropic.com |
| **ANTHROPIC_ADMIN_KEY** | `web/.env` (local) | Anthropic Console: console.anthropic.com/settings/keys | Regenerate (admin scope required) |
| **RESEND_API_KEY** | `web/.env` (local) | Resend dashboard: resend.com/api-keys | Regenerate at resend.com |
| **OPS_API_KEY** | `web/.env` (local), secret store | none | Generate: `echo "ops_$(openssl rand -hex 32)"` |
| **SESSION_SECRET** | Railway env var (web service) | none | Generate: `openssl rand -hex 32` (invalidates all sessions) |
| **PREVIEW_PASSWORD** | **NOT USED**: site is public | none | Do NOT set without maintainer approval |
| **Postgres password** | Generated during recovery | Railway env vars on Postgres service | Generate: `openssl rand -hex 16` |
| **SENTRY_DSN** | sentry.io dashboard: Settings > Client Keys | none | Create new Sentry project if org lost |
| **GITHUB_TOKEN** | Secret store | github.com/settings/tokens | No code in this repository reads it now (the ops agent's GitHub commits were removed); create one only for your own use |
| **STRIPE_SECRET_KEY** | Stripe dashboard: dashboard.stripe.com/apikeys | none | Roll the key in the Stripe dashboard |
| **Admin password** | none | Trigger forgot-password at /forgot-password | Requires working Resend + email |
| **DB backup** | `<BACKUP_DIR>/` | none | Railway support (48h window for deleted projects) |

---

## Env Var Reference

### Web Service

| Var | Required | Source | Notes |
|-----|----------|--------|-------|
| DATABASE_URL | Yes | `${{Postgres.DATABASE_PRIVATE_URL}}` | Railway reference var; auto-populates |
| SESSION_SECRET | Yes | `openssl rand -hex 32` | Changing invalidates all user sessions. Whenever `NODE_ENV` is anything but `development` or `test` (unset counts as production) the server refuses to start (`resolveSessionSecret`, `sessionSecretWeakness` in `web/src/middleware/auth.ts`) unless the value has at least 32 characters and at least 8 different ones, is not one short block repeated to fill the value, and contains none of the placeholder words (changeme, replaceme, placeholder, yoursecret, devsecret, sessionsecret, example, password, insecure) once lower-cased and stripped of everything but letters and digits; `openssl rand -hex 32` passes. It is a check for a stand-in value, not an entropy estimate |
| ANTHROPIC_API_KEY | Yes | `web/.env` | Claude API for intake chat |
| ANTHROPIC_ADMIN_KEY | No | `web/.env` | Admin API for cost dashboard |
| RESEND_API_KEY | Yes | `web/.env` | Email delivery |
| ADMIN_EMAIL | Yes | the admin account's email, lower case | Admin access gate (`isAdminSession`); no default, and when unset nobody is admin |
| OPS_API_KEY | Yes | `web/.env` or secret store | Machine-to-machine API auth |
| PREVIEW_PASSWORD | **NO** | Do NOT set | Site is public; setting this locks out all visitors |
| PORT | Yes | `4000` | Express listen port |
| NODE_ENV | Yes | `production` | Only the exact values `development` and `test` count as development; anything else, including unset, gets the hardened behavior |
| BASE_URL | Yes | public origin, no trailing slash | Links in verification, reset and unsubscribe emails and the Stripe return URL; the server warns at boot when it is unset in production; a value that is not a scheme, a plain host and an optional port is ignored for the verification and reset links, which then use the request's `Host` header |
| TRUST_PROXY_HOPS | No | `1` | Reverse proxies in front of the app: `1` on Railway, `0` when the app is reached directly. Unset means 1 and logs a warning at boot, so set it explicitly; `web/.env.example` ships `0` |
| RESEND_WEBHOOK_SECRET | No | Resend dashboard | Bounce and complaint webhook; unset makes `POST /webhooks/resend` reject every call |
| WORKER_WEBHOOK_SECRET | No | `openssl rand -hex 32` | Bearer secret for the optional `POST /webhooks/build-status` hook. The bundled worker does not call that hook (it writes the `builds` table directly), so this matters only for an external worker; unset rejects every call |
| STRIPE_SECRET_KEY | No | Stripe dashboard | Payments; optional |
| STRIPE_PUBLISHABLE_KEY | No | Stripe dashboard | Payments; optional |
| STRIPE_WEBHOOK_SECRET | No | Stripe dashboard | Payments; optional |
| STRIPE_BYPASS | No | unset | Only `true`, `1`, `yes`, `on` or `enabled` enable the feature-request payment bypass; `false`, empty and anything else leave it off. With `NODE_ENV` development or test any logged-in customer can use it; in production only the `ADMIN_EMAIL` admin session can. Leave unset anywhere real money moves |
| SENTRY_DSN | No | sentry.io | Error tracking; no-op if unset |
| CAS_ALLOW_RAILWAY_WRITES | No | unset | Railway writes from the web app (project delete, `/admin/settings` save) are refused unless `NODE_ENV=production` AND this is `true` |
| PROTECTED_RAILWAY_PROJECT_IDS | Yes if writes are on | Comma-separated project UUIDs | Projects the admin delete route must never touch; the project in `RAILWAY_PROJECT_ID` is always protected as well |
| RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_WORKER_SERVICE_ID | No | Railway injects the project and environment ids | Used by `/admin/settings` and the protected-project check |

### Worker Service

All web vars above (except PORT, NODE_ENV, SESSION_SECRET, PREVIEW_PASSWORD, STRIPE_PUBLISHABLE_KEY) plus:

| Var | Required | Source | Notes |
|-----|----------|--------|-------|
| DATABASE_URL | Yes | Internal URL | `postgresql://railway:<pw>@postgres.railway.internal:5432/railway` |
| RAILWAY_API_TOKEN | Yes | secret store / dashboard | Token for customer app Railway projects |
| RAILWAY_PROJECT_ID | Yes | From project creation | Self-deletion guard |
| RAILWAY_ENVIRONMENT_ID | Yes | From project creation | |
| RAILWAY_ENVIRONMENT | Yes | `production` | Selects the failure-KB volume path and the Sentry environment name (it is not a gate on the orphan sweep) |
| PROTECTED_RAILWAY_PROJECT_NAMES | Yes | Comma-separated | Names `destroy_project()` refuses to delete, in addition to any name starting with `craftandship` |
| CAS_BACKUP_DIR | Yes for durable backups | A mounted volume path | Where `monitor/backup.py` writes dumps |
| MONITOR_MAX_LLM_CALLS_PER_DAY | No | `60` (default) | Rolling 24-hour cap on `monitor/diagnose.py` model calls |
| MAX_CONCURRENT_BUILDS | Yes | `1` | |
| DAILY_API_SPEND_CAP | Yes | `50` | USD; 0 to disable |
| DISABLE_BATCH_API | No | unset | `true` forces realtime API calls |
| BLOCKING_DESIGN_REVIEW | No | unset (off) | `true` makes design review a hard gate (v1 engine only) |
| BLOCKING_QA | No | unset (off) | `true` makes QA a hard gate (v1 engine only) |
| MAX_DESIGN_FIXES | No | unset (3) | Design fix loop limit (v1 engine only) |
| COORDINATOR_ENABLED | No | `false` | Start disabled; enable when stable |
| COORDINATOR_DRY_RUN | No | `true` | Start in dry-run |
| COORDINATOR_DAILY_BUDGET | No | unset (20) | USD per day |
| ENABLE_ORPHAN_SWEEP | **CRITICAL** | `false` | **NEVER set true without explicit maintainer approval** |
| OPS_DRY_RUN | No | `true` | Start disabled |
| SENTRY_DSN | No | sentry.io | Error tracking |

---

## DNS and Custom Domain Setup

### Cloudflare DNS Records (dash.cloudflare.com)

After running `customDomainCreate` mutations, Railway returns CNAME targets.

Example layout:

| Type | Name | Target | Proxy Status |
|------|------|--------|-------------|
| CNAME | `@` (root) | `<target returned for the root domain>` | Proxied |
| CNAME | `www` | `<target returned for www>` | Proxied |

**Important:** These CNAME targets change every time you create a new custom domain on Railway. Always use the values returned by the `customDomainCreate` mutation.

### Verification

```bash
# Check DNS propagation
dig +short craftandship.io CNAME
dig +short www.craftandship.io CNAME

# Test through custom domain
curl -sf -u ":$PREVIEW_PASSWORD_IF_SET" "https://craftandship.io/health"
```

---

## Post-Recovery Verification

Run through this checklist after recovery:

```
[ ] /health returns 200 with db_connected: true
[ ] /ops/api/v1/stats returns build counts
[ ] /ops/api/v1/builds returns all builds from backup
[ ] Admin login works (the account named by ADMIN_EMAIL)
[ ] Basic auth gate works (PREVIEW_PASSWORD)
[ ] Custom domain resolves (craftandship.io)
[ ] Worker is processing builds (check /health for worker_heartbeat)
[ ] Email delivery works (trigger forgot-password)
[ ] All codebase IDs updated (grep for old IDs)
[ ] Tests pass (npm test + pytest)
[ ] Pushed and deployed on latest commit
```

---

## Safety Guardrails

These were implemented after the 2026-04-09 incident to prevent recurrence:

### 4-Layer Orphan Sweep Protection

1. **Code gate:** `_sweep_orphan_railway_projects()` returns without doing anything unless `ENABLE_ORPHAN_SWEEP` is the string `true` (case-insensitive string match, not truthiness)
2. **Env var:** `ENABLE_ORPHAN_SWEEP=false` explicitly set on all services
3. **Name guard:** `destroy_project()` blocks any project with name starting with `craftandship` or in `PROTECTED_RAILWAY_PROJECT_NAMES`
4. **ID guard:** `destroy_project()` blocks deletion of `RAILWAY_PROJECT_ID` (self-deletion prevention)

The web admin delete route (`POST /admin/builds/:buildId/delete`) has its own two layers: it calls Railway only when `NODE_ENV=production` AND `CAS_ALLOW_RAILWAY_WRITES=true`, and it refuses (403) any project listed in `PROTECTED_RAILWAY_PROJECT_IDS` or equal to `RAILWAY_PROJECT_ID`. Before it deletes anything it reads the project's name back; a name that cannot be read, or that does not match the build's app name, returns 502 and keeps the project and the row, and only the `NOT_FOUND` error code counts as already deleted.

### Behavioral Rules

- **NEVER** spawn background agents with Railway API write access
- **NEVER** run `projectDelete` without explicit human confirmation
- **NEVER** set `ENABLE_ORPHAN_SWEEP=true` without maintainer approval
- All Railway mutations must run in **foreground** where the user can see each call
- Background agents may only **read** Railway state, never mutate
- Always verify project IDs before any Railway mutation

### Code Locations

- Orphan sweep gate: `queue/worker_pg.py` `_sweep_orphan_railway_projects()`
- The worker's only other project delete: `queue/worker_pg.py` `_discard_unannounced_project()`, which removes the project a failing attempt itself created and never announced, and refuses any project that a build row records. (An earlier `_cleanup_old_railway_projects()`, which deleted the projects of old failed builds, was removed; the janitor `_cleanup_old_builds()` now deletes database rows only.)
- Destroy guard: `cli/deploy.py` `destroy_project()`, which returns only once Railway has confirmed the delete and raises otherwise
- Protected names: `PROTECTED_RAILWAY_PROJECT_NAMES` env var (comma-separated)

---

## Failover and Multi-Provider Options (planning notes)

> Written 2026-05-19 after a Railway platform-wide edge-network outage. All
> Railway services returned Railway's global fallback 404, the DB TCP proxy was
> unreachable, and the `craftandship.io` cert binding fell back to Railway's
> wildcard. Nothing on our side was broken or actionable. These are notes for
> future hardening, not a commitment to build now.

### What today's outage exposed

- Railway edge stayed up but couldn't route to any backing service (`HTTP 404` +
  `x-railway-fallback: true` on every CAS surface).
- DB TCP proxy (`example.proxy.rlwy.net`) timed out: even with the DB itself
  presumably fine, we couldn't reach it from outside Railway.
- Custom domain `craftandship.io` lost its cert binding and served Railway's
  wildcard `*.up.railway.app` cert instead.
- Every customer app hit the same fallback
  404. The build pipeline halted because the worker couldn't reach DB or deploy.
- Resend (email) and Sentry: unaffected because they're off Railway.

### What's coupled to Railway today

| Surface | Provider-coupled? | Failover difficulty |
|---|---|---|
| `web/` (Express) | No code coupling; stateless | Easy; redeploy on Fly / Render / Cloud Run |
| `queue/` worker | Code is portable, but the worker *deploys customer apps to Railway* via API | Medium without multi-provider deploy |
| CAS Postgres | Yes (Railway-managed) | Hard |
| Each customer app's Postgres | Yes (one per app, Railway-managed) | Hardest |
| `craftandship.io` cert + domain | Yes (Railway-managed cert) | Easy; front with Cloudflare |
| Resend / Sentry | No | Already off Railway |

### Phased options (cheap to expensive)

**Phase 0: cheap, an hour or two of work.**

- **Off-Railway DB backups.** Daily `pg_dump` from `run_daily_backups()` in `monitor/backup.py`
  lands on `CAS_BACKUP_DIR`, which in production is a Railway volume; if Railway is gone, so is the backup. Push
  the nightly dump to Backblaze B2 / Cloudflare R2 / S3.
- **Cloudflare in front of `craftandship.io`.** Move DNS to orange-cloud-proxied,
  let Cloudflare hold the cert (not Railway). Visitors hit a Cloudflare-served
  maintenance page during a Railway outage instead of Railway's
  `"Application not found"` JSON. Sets up the rails for DNS-level failover later.
- **Static "Railway-wide outage" maintenance page** parked in Cloudflare, ready
  to flip via a page rule.

**Phase 1: a monthly cost. Trigger: an SLA to defend.**

- **Hot-standby Postgres on Neon or Supabase.** Logical replication from Railway
  Postgres. RPO near zero, RTO is just the connection-string flip.
- **Warm web standby on Fly.io (or Render).** Same Dockerfile, same env vars,
  scaled to zero. Cloudflare LB health-checks both origins and flips automatically.
  Worker rides the same pattern.
- This stack would have kept CAS itself (web + worker + DB) reachable through
  today's outage. It does NOT save customer apps, see Phase 2.

**Phase 2: Multi-provider customer-app deploys. Trigger: the cost is justified by customer demand.**

- Today, every customer app is one Railway project. If Railway is down, every
  customer site is down. Options:
  - **Pick-per-build:** worker chooses Railway or Fly.io at deploy time (sticky
    per customer). Doesn't rescue apps already deployed and down, only the next
    build.
  - **Active-active per customer:** dual Railway + Fly deploys with shared
    Postgres on Neon, Cloudflare LB in front of each customer subdomain.
    Roughly double the hosting cost per customer.
  - **Static-export for brochure-style builds:** for brochure builds
    without admin CRUD, generate a static archetype variant and serve
    from Cloudflare Pages / Netlify. Immune to Railway entirely. Doesn't
    work for admin-bearing apps but covers a real chunk of the catalog.

### What's NOT worth doing yet

- **Re-platforming off Railway.** Cost-of-incident is still
  small at this scale; Railway is the right default. Multi-provider hardens the edges without
  forcing a re-platform.
- **Multi-region inside Railway.** Doesn't help: today's outage was global to
  their edge.
- **Active-active CAS web.** Phase 1 warm-standby covers the same threat for
  much less cost and operational complexity.

### Triggers to revisit this section

- Any Railway outage longer than ~2 hrs.
- An SLA-bearing contract.
- A customer churn or refund directly caused by a Railway outage.
- Any incident where the **DB backup itself** was on Railway and unreachable.
