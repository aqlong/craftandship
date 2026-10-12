# Failover & Disaster Recovery Runbook

> Historical document; the system was wound down in July 2026. The procedures below describe the production setup as it was. The Render and Fly.io failover options in section 4 were written as contingency procedures; this repository holds no record of either being used.

This document provides step-by-step procedures for handling infrastructure outages affecting Craft & Ship production services on Railway.

---

## Table of Contents

1. [Detection](#1-detection)
2. [Customer Communication](#2-customer-communication)
3. [DNS Rerouting via Cloudflare](#3-dns-rerouting-via-cloudflare)
4. [Manual Failover to Backup Provider](#4-manual-failover-to-backup-provider)
5. [Database Restore from pg_dump Backups](#5-database-restore-from-pg_dump-backups)
6. [Post-Incident Review Checklist](#6-post-incident-review-checklist)
7. [Cloudflare Proxy Configuration](#7-cloudflare-proxy-configuration)
8. [Contact Info & References](#8-contact-info--references)

---

## 1. Detection

### Automated Detection (BetterStack)

An external BetterStack account checked the production endpoints every 60 seconds and alerted after 3 confirmations. That configuration lives in the BetterStack dashboard, not in this repository.

Alerts fired when:
- `craftandship-web` /health returns non-200 or times out
- `craftandship-worker` /ops/api/v1/stats returns non-200

Alert channels: email and SMS.

A heartbeat monitor for the worker needs the worker to send a periodic ping to a BetterStack heartbeat URL; no code in this repository does that, so none of the alerts above depended on one.

### Manual Checks

If you suspect an outage before alerts fire:

```bash
# Check web health
curl -s -o /dev/null -w "%{http_code}" https://cas-web-example.up.railway.app/health

# Check worker stats (requires OPS_API_KEY)
curl -s -H "Authorization: Bearer $OPS_API_KEY" \
  https://cas-web-example.up.railway.app/ops/api/v1/stats

# Check from the existing monitor system (app ids come from `list`)
cd <repo>
python3 -m monitor.cli list
python3 -m monitor.cli check <app-id>

# Check Railway service status directly
railway status
```

### Confirm It Is a Railway Outage (Not App Bug)

1. Check Railway status page: https://status.railway.com
2. Check if other Railway-hosted services are also down
3. Review Railway deploy logs for recent failed deployments
4. If /health returns 503 with `"status": "degraded"`, the app is running but DB is unreachable; this may be a Railway Postgres issue, not a full outage

---

## 2. Customer Communication

### Incident Acknowledgment (send within 15 minutes of detection)

**Subject:** Craft & Ship: Service Interruption

```
Hi [Customer Name],

We are aware of a service interruption affecting Craft & Ship and are actively
working to restore full functionality. Your data is safe and no builds have
been lost.

What is affected:
- New builds may be delayed
- [Live app URLs / dashboard access] may be temporarily unavailable

We will provide updates every 30 minutes until the issue is resolved.

Thank you for your patience,
The Craft & Ship team
support@craftandship.io
```

### Resolution Notification

**Subject:** Craft & Ship: Service Restored

```
Hi [Customer Name],

The service interruption has been resolved. All systems are operating normally.

Duration: [start time] to [end time] ([X] minutes)
Impact: [brief description of what was affected]

Any builds that were queued during the outage have been automatically resumed.
If you notice any issues with your app, please reply to this email.

Thank you for your patience,
The Craft & Ship team
support@craftandship.io
```

### Sending via Resend API (if web service is down)

```bash
curl -X POST https://api.resend.com/emails \
  -H "Authorization: Bearer $RESEND_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "Craft & Ship <noreply@craftandship.io>",
    "reply_to": "support@craftandship.io",
    "to": ["customer@example.com"],
    "subject": "Craft & Ship: Service Interruption",
    "text": "We are aware of a service interruption..."
  }'
```

---

## 3. DNS Rerouting via Cloudflare

Use this when Railway is down and you need to point traffic to a backup provider.

### Prerequisites

- Cloudflare dashboard access: https://dash.cloudflare.com
- Domain: craftandship.io
- Current DNS: CNAME record pointing to Railway (`*.up.railway.app`)

### Steps to Reroute

1. **Log in to Cloudflare dashboard** at https://dash.cloudflare.com
2. Select the **craftandship.io** zone
3. Go to **DNS > Records**
4. Find the existing CNAME record for the web service
5. **Change the record:**
   - Type: Change from `CNAME` to `A`
   - Name: `@` (or the relevant subdomain)
   - Content: IP address of the backup provider (see Section 4)
   - Proxy status: **Proxied** (orange cloud ON)
   - TTL: Auto (Cloudflare manages this when proxied)
6. Click **Save**
7. DNS propagation is near-instant when Cloudflare proxy is enabled

### For Customer App Subdomains

If customer apps use custom domains routed through Cloudflare:

1. Update each customer app's DNS record similarly
2. Point A records to the backup provider's IP
3. Verify SSL is handled by Cloudflare (orange cloud must be ON)

### Rollback After Recovery

1. Change A record back to CNAME pointing to Railway
2. Verify Railway health check passes
3. Confirm DNS resolution with: `dig craftandship.io +short`

---

## 4. Manual Failover to Backup Provider

### Option A: Render

1. **Create Render account** at https://render.com (if not already set up)
2. **Deploy web service:**
   ```bash
   # From local machine with current repo
   cd <repo>

   # Create a render.yaml or use the dashboard
   # Web service: Node.js, root directory = web/, build command = npm install && npm run build
   # Start command: npx ts-node src/server.ts
   # Environment: set all vars from web/.env (production values)
   ```
3. **Deploy worker:** Use Render Background Worker with Dockerfile.worker
4. **Create PostgreSQL database** on Render and restore from backup (see Section 5)
5. **Update DATABASE_URL** in both services to point to the new Render Postgres instance
6. **Update Cloudflare DNS** to point to the Render service URL/IP (see Section 3)

### Option B: Fly.io

1. **Install flyctl:** `brew install flyctl`
2. **Deploy web service:**
   ```bash
   cd <repo>/web
   fly launch --name craftandship-web --region sjc
   # Set environment variables
   fly secrets set DATABASE_URL="..." SESSION_SECRET="..." ANTHROPIC_API_KEY="..." \
     STRIPE_SECRET_KEY="..." RESEND_API_KEY="..." ADMIN_EMAIL="<admin account email>" \
     BASE_URL="https://<your public origin>" NODE_ENV=production
   fly deploy
   ```
3. **Deploy worker:**
   ```bash
   cd <repo>
   fly launch --name craftandship-worker --dockerfile Dockerfile.worker --region sjc
   fly secrets set DATABASE_URL="..." RAILWAY_API_TOKEN="..." ANTHROPIC_API_KEY="..." \
     RESEND_API_KEY="..." ADMIN_EMAIL="<admin account email>"
   fly deploy
   ```
4. **Create Postgres:** `fly postgres create --name craftandship-db --region sjc`
5. **Restore database** from backup (see Section 5)
6. **Update Cloudflare DNS** to point to Fly.io IP

### Environment Variables Checklist

Both web and worker need these vars set on the backup provider:

| Variable | Source |
|---|---|
| DATABASE_URL | New provider's Postgres connection string |
| SESSION_SECRET | Same as production |
| ANTHROPIC_API_KEY | Same as production |
| STRIPE_SECRET_KEY | Same as production |
| STRIPE_WEBHOOK_SECRET | Regenerate in Stripe dashboard for new URL |
| RESEND_API_KEY | Same as production |
| ADMIN_EMAIL | The admin account's email, lower case (no default; unset means nobody is admin) |
| RAILWAY_API_TOKEN | Same (still needed for customer app deploys) |
| SENTRY_DSN | Same as production |
| OPS_API_KEY | Same as production |
| NODE_ENV | production (anything other than `development` or `test`, including unset, counts as production) |
| BASE_URL | The public origin of the new deployment; used for links in emails and the Stripe return URL |
| TRUST_PROXY_HOPS | Number of reverse proxies in front of the web service on the new provider (unset means 1, which fits Railway, with a warning at boot; `web/.env.example` ships 0, so a copied template runs with 0); a wrong value breaks rate limiting and Secure cookies |
| WORKER_WEBHOOK_SECRET | Same as production, only if an external worker uses the optional `POST /webhooks/build-status` hook; the bundled worker does not call it |
| RESEND_WEBHOOK_SECRET | Same as production, if bounce and complaint webhooks are used |

---

## 5. Database Restore from pg_dump Backups

### Backup Sources

1. **Automatic daily backups** by the worker (03:00 UTC) via `monitor/backup.py`: gzip-compressed plain SQL (`<timestamp>.sql.gz`, taken with `--clean --if-exists`) under `<app name>__<registry id>/` in a `backups` folder beside `monitor/backup.py`, or in `CAS_BACKUP_DIR` when it is set (the default location is lost on every redeploy, so production needs `CAS_BACKUP_DIR` on a mounted volume); the 7 most recent per app are kept (`prune_backups`). These cover the databases of the apps in the monitor registry, which are the customer apps the worker registers after a deploy; no code in this repository registers or dumps the platform database (the one holding `customers` and `builds`), so a platform restore needs a dump taken some other way, such as the manual `pg_dump` in the next item
2. **Manual backups** via monitor CLI: `python3 -m monitor.cli backup <app-id>` for a registered app, or a plain `pg_dump` of the platform database against its connection string

### Restore Procedure

```bash
# 1. List available backups
ls -la <BACKUP_DIR>/<app name>__<registry id>/

# 2. Create fresh database on the backup provider
createdb craftandship_web_restored

# 3. Restore from the most recent dump of THIS database. A dump the monitor took
# of a registered customer app is gzip-compressed plain SQL, so it is piped into
# psql (the platform database has no monitor dump; use the pg_dump you took of it):
gunzip -c <BACKUP_DIR>/<app name>__<registry id>/<timestamp>.sql.gz | psql -v ON_ERROR_STOP=1 --single-transaction "$NEW_DATABASE_URL"

# A custom-format dump from a manual `pg_dump -Fc` needs pg_restore instead:
pg_restore --verbose --clean --if-exists \
  --dbname="$NEW_DATABASE_URL" \
  <BACKUP_DIR>/latest.dump

# 4. Verify restore
psql "$NEW_DATABASE_URL" -c "SELECT COUNT(*) FROM customers;"
psql "$NEW_DATABASE_URL" -c "SELECT COUNT(*) FROM builds;"
psql "$NEW_DATABASE_URL" -c "SELECT status, COUNT(*) FROM builds GROUP BY status;"
```

### Using monitor CLI to Restore

```bash
cd <repo>

# List registered apps and their ids
python3 -m monitor.cli list

# Restore a registered app's database from a .sql.gz snapshot (one transaction;
# the first failing statement aborts it and the command exits non-zero)
python3 -m monitor.cli restore <app-id> <backup_path>
```

### Post-Restore Verification

- [ ] Customer accounts exist and can log in
- [ ] Active builds are in correct states
- [ ] Intake messages are preserved
- [ ] Email log entries are present
- [ ] Session store table exists (user_sessions)
- [ ] Run schema migration to apply any additive changes: the web service does this on startup automatically

---

## 6. Post-Incident Review Checklist

Complete within 48 hours of resolution:

### Immediate (within 2 hours)

- [ ] All services confirmed healthy (web /health, worker /ops/api/v1/stats)
- [ ] BetterStack monitors showing green
- [ ] Customer communication sent (resolution notification)
- [ ] DNS records restored to Railway (if rerouted)
- [ ] Any queued builds resumed or requeued
- [ ] Orphan Railway projects reviewed by hand (the automatic sweep is disabled by default; see `docs/RAILWAY_DISASTER_RECOVERY.md`)

### Within 24 Hours

- [ ] Write incident timeline (detection time, response time, resolution time)
- [ ] Calculate total downtime and customer impact
- [ ] Identify root cause (Railway outage, deploy failure, DB issue, etc.)
- [ ] Review alert effectiveness: did BetterStack fire before manual detection?
- [ ] Review monitoring gaps: were there signals we missed?
- [ ] Check for data loss: compare pre/post-incident build counts
- [ ] Verify no builds were corrupted during the outage

### Within 48 Hours

- [ ] Document lessons learned
- [ ] Create action items for prevention (e.g., add redundancy, improve alerting)
- [ ] Update this runbook with any new procedures discovered
- [ ] Test backup restore procedure if it was not exercised during the incident
- [ ] Review and update backup frequency if needed
- [ ] Consider adding the failure pattern to ops agent playbooks

---

## 7. Cloudflare Proxy Configuration

Enabling Cloudflare proxy (orange cloud) on DNS records provides DDoS protection, SSL termination, and the ability to reroute traffic during outages without DNS propagation delays.

### Enabling Proxy on CAS Platform Domain

1. **Log in to Cloudflare dashboard** at https://dash.cloudflare.com
2. Select the **craftandship.io** zone
3. Go to **DNS > Records**
4. For each relevant DNS record (A or CNAME):
   - Click **Edit**
   - Toggle **Proxy status** to **Proxied** (orange cloud icon)
   - Click **Save**

### Enabling Proxy on Customer App Domains

When customers point custom domains to their Railway-hosted apps:

1. Customer adds a CNAME record pointing to their Railway URL
2. In Cloudflare, ensure the proxy toggle is **ON** (orange cloud)
3. Under **SSL/TLS**, set mode to **Full (strict)**; Railway provides valid certificates
4. Under **SSL/TLS > Edge Certificates**, enable **Always Use HTTPS**

### Benefits

- **DDoS protection:** Cloudflare absorbs volumetric attacks before they reach Railway
- **SSL termination:** Cloudflare handles TLS at the edge, reducing load on origin
- **Traffic rerouting:** When proxied, changing the origin IP/CNAME takes effect instantly (no DNS TTL wait), which is critical for failover scenarios in Section 3
- **Caching:** Static assets (CSS, JS, images) can be cached at Cloudflare edge, reducing origin load
- **WAF rules:** Optional Web Application Firewall rules for additional protection

### Verification

After enabling proxy, verify Cloudflare is in the request path:

```bash
# Check for cf-ray header (proves traffic flows through Cloudflare)
curl -sI https://craftandship.io | grep -i cf-ray

# Expected output:
# cf-ray: 8a1b2c3d4e5f6g7h-SJC

# Check that the origin IP is hidden
dig craftandship.io +short
# Should return Cloudflare IPs (104.x.x.x or 172.x.x.x), NOT the Railway IP

# Full header check
curl -sI https://craftandship.io | grep -iE "(cf-ray|server|cf-cache)"
```

### Important Notes

- **Do NOT enable proxy on MX records** (email routing); only A/AAAA/CNAME records
- **Websockets:** Cloudflare proxies WebSocket connections by default on all plans
- **SSE (Server-Sent Events):** Used by the intake chat; it works through Cloudflare proxy, but set **Response Buffering** to OFF under Speed > Optimization if SSE events are delayed
- **Railway health checks:** Railway's internal health checks are not affected by Cloudflare proxy since they hit the service directly

---

## 8. Contact Info & References

| Resource | URL |
|---|---|
| Railway Status Page | https://status.railway.com |
| Railway Dashboard | https://railway.com/dashboard |
| Railway Project (CAS) | https://railway.com/project/<RAILWAY_PROJECT_ID> |
| Cloudflare Dashboard | https://dash.cloudflare.com |
| BetterStack Dashboard | https://uptime.betterstack.com |
| Resend Dashboard | https://resend.com/emails |
| Stripe Dashboard | https://dashboard.stripe.com |
| Sentry Dashboard | https://sentry.io |
| Render Dashboard | https://dashboard.render.com |
| Fly.io Dashboard | https://fly.io/dashboard |

### Key Contacts

| Role | Contact |
|---|---|
| Operator | the address in `ADMIN_EMAIL` |
| Support Inbox | support@craftandship.io |
| Railway Support | support@railway.com or Discord |
| Cloudflare Support | Cloudflare dashboard ticket system |
