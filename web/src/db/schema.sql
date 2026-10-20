-- Customers
CREATE TABLE IF NOT EXISTS customers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email VARCHAR(255) UNIQUE NOT NULL,
  password VARCHAR(255) NOT NULL,
  name VARCHAR(255) NOT NULL,
  stripe_customer_id VARCHAR(255),
  created_at TIMESTAMP DEFAULT NOW()
);

-- Builds
CREATE TABLE IF NOT EXISTS builds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID REFERENCES customers(id),
  tier VARCHAR(50) NOT NULL CHECK (tier IN ('build', 'grow', 'scale')),
  status VARCHAR(50) NOT NULL DEFAULT 'intake'
    CHECK (status IN ('intake', 'payment_pending', 'queued', 'building', 'testing', 'deploying', 'preview', 'live', 'failed', 'abandoned')),
  app_name VARCHAR(255),
  spec JSONB,
  spec_path VARCHAR(500),
  live_url VARCHAR(500),
  error_message TEXT,
  stripe_session_id VARCHAR(255),
  stripe_payment_intent VARCHAR(255),
  queue_job_id VARCHAR(255),
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Intake messages (conversation history per build)
CREATE TABLE IF NOT EXISTS intake_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  build_id UUID REFERENCES builds(id),
  role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Every intake, revision and anonymous-draft turn reads the whole conversation
-- for one build in order.
CREATE INDEX IF NOT EXISTS idx_intake_messages_build_id
  ON intake_messages (build_id, created_at);

-- Auto-update updated_at on builds
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_builds_updated_at ON builds;
CREATE TRIGGER update_builds_updated_at
  BEFORE UPDATE ON builds
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- Email log: every send attempt regardless of outcome
CREATE TABLE IF NOT EXISTS email_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMP DEFAULT NOW(),
  template VARCHAR(100) NOT NULL,
  to_email VARCHAR(255) NOT NULL,
  subject VARCHAR(500) NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')),
  status_code INTEGER,
  error_message TEXT,
  resend_id VARCHAR(255),
  duration_ms INTEGER,
  html_body TEXT,
  metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_email_log_created_at ON email_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_log_status ON email_log (status);
CREATE INDEX IF NOT EXISTS idx_email_log_to_email ON email_log (to_email);

-- Additive column migrations (safe to re-run)
ALTER TABLE email_log ADD COLUMN IF NOT EXISTS html_body TEXT;

-- Unsubscribe support
ALTER TABLE customers ADD COLUMN IF NOT EXISTS email_unsubscribed BOOLEAN DEFAULT FALSE;

-- GDPR consent timestamp: records when the user accepted Terms of Service
ALTER TABLE customers ADD COLUMN IF NOT EXISTS terms_accepted_at TIMESTAMP;

-- Build timing, retry tracking, and Railway metadata
ALTER TABLE builds ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS retry_count INTEGER DEFAULT 0;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS railway_project_id VARCHAR(255);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS railway_service_id VARCHAR(255);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS railway_environment_id VARCHAR(255);
-- Generated Postgres DATABASE_URL with its credentials masked (the worker's
-- _set_preview_status); the password stays with Railway.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS railway_database_url TEXT;

-- Anonymous intake preview: drafts start with customer_id=NULL and a random
-- session ID tying the draft to a browser cookie. Claimed (migrated) on signup.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS anonymous_session_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_builds_anonymous_session_id
  ON builds (anonymous_session_id)
  WHERE customer_id IS NULL AND anonymous_session_id IS NOT NULL;

-- Dashboard, ownership, and admin query indexes
CREATE INDEX IF NOT EXISTS idx_builds_customer_id ON builds (customer_id);
CREATE INDEX IF NOT EXISTS idx_builds_status ON builds (status);
CREATE INDEX IF NOT EXISTS idx_builds_app_name ON builds (app_name);

-- Session store for connect-pg-simple (must match expected schema exactly)
CREATE TABLE IF NOT EXISTS "user_sessions" (
  "sid" varchar NOT NULL COLLATE "default",
  "sess" json NOT NULL,
  "expire" timestamp(6) NOT NULL,
  CONSTRAINT "user_sessions_pkey" PRIMARY KEY ("sid")
) WITH (OIDS=FALSE);
CREATE INDEX IF NOT EXISTS "IDX_user_sessions_expire" ON "user_sessions" ("expire");

-- Support knowledge base for AI customer service chat
CREATE TABLE IF NOT EXISTS support_kb (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question TEXT NOT NULL,
    question_normalized TEXT,
    answer TEXT NOT NULL,
    usage_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_support_kb_usage ON support_kb (usage_count DESC);

-- Whether a support_kb answer may be served: the support chat serves only
-- 'approved' rows (findKnowledgeBaseAnswer in routes/support.ts). The column
-- arrives with DEFAULT 'approved', so the rows that exist when it is added are
-- filled with 'approved' once, by the ADD COLUMN itself, and keep being served;
-- on every later boot the ADD COLUMN does nothing. The default is then set to
-- 'pending', so a row inserted afterwards without a status (the weekly topic
-- sweep and the coordinator's support_kb_insert are the writers) is not served
-- until an operator approves it, by hand:
--   UPDATE support_kb SET status = 'approved', updated_at = NOW() WHERE id = '<id>'
ALTER TABLE support_kb ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved'
  CHECK (status IN ('pending', 'approved'));
ALTER TABLE support_kb ALTER COLUMN status SET DEFAULT 'pending';

-- Platform-level singleton key/value metadata (e.g. support_kb knowledge hash
-- so we can detect when the dynamic support-knowledge content changed and
-- invalidate the cached support_kb rows on the next boot).
CREATE TABLE IF NOT EXISTS platform_metadata (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Password reset tokens
CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token VARCHAR(64) NOT NULL UNIQUE,
  expires_at TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '1 hour',
  used_at TIMESTAMP
);

-- Feature requests for existing live projects
CREATE TABLE IF NOT EXISTS feature_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  build_id UUID NOT NULL REFERENCES builds(id),
  customer_id UUID NOT NULL REFERENCES customers(id),
  status VARCHAR(50) DEFAULT 'drafting'
    CHECK (status IN ('drafting', 'scoped', 'payment_pending', 'queued', 'building', 'live', 'failed')),
  description TEXT,
  scope_summary TEXT,
  estimated_cost INTEGER,
  tier VARCHAR(20) DEFAULT 'feature',
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feature_requests_build_id ON feature_requests (build_id);
CREATE INDEX IF NOT EXISTS idx_feature_requests_customer_id ON feature_requests (customer_id);

-- Feature request conversation messages
CREATE TABLE IF NOT EXISTS feature_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  feature_request_id UUID NOT NULL REFERENCES feature_requests(id),
  role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_feature_messages_request_id ON feature_messages (feature_request_id);

-- Logo storage (cleaned SVG or optimized PNG stored as base64 in spec JSONB)
-- Tracking columns for cleanup and analytics
ALTER TABLE builds ADD COLUMN IF NOT EXISTS logo_filename VARCHAR(255);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS logo_mimetype VARCHAR(50);

-- Staging preview: preview_url and approval timestamp. (The 'preview' and
-- 'abandoned' statuses are part of builds_status_check, which is created with
-- the table above and upgraded for older databases further down.)
ALTER TABLE builds ADD COLUMN IF NOT EXISTS preview_url VARCHAR(500);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS preview_approved_at TIMESTAMP;

-- Auto-update updated_at on feature_requests
DROP TRIGGER IF EXISTS update_feature_requests_updated_at ON feature_requests;
CREATE TRIGGER update_feature_requests_updated_at
  BEFORE UPDATE ON feature_requests
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- Build logs: real-time pipeline output streamed to customer dashboard
CREATE TABLE IF NOT EXISTS build_logs (
  id BIGSERIAL PRIMARY KEY,
  build_id UUID NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
  stage VARCHAR(50) NOT NULL,
  message TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_build_logs_build_id ON build_logs (build_id, id);

-- Build starts a customer caused: one row per start that was accepted. Four
-- kinds, and what each row's build_id names:
--   'dispatch'  a Harbor conversation's first build (services/harbor_dispatch.ts);
--               build_id is that new build;
--   'retry'     a manual retry of a failed build (routes/builds.ts); build_id
--               is the build retried;
--   'rebuild'   a rebuild: the owner's (routes/builds.ts; build_id is the new
--               child build) or the coordinator's automatic one
--               (coordinator/actuators.py; build_id is the lineage's root);
--   'revision'  a revision build (routes/builds.ts); build_id is the new
--               child build.
-- Every row, whatever its kind, counts toward the customer's daily budget
-- (services/build-start-limits.ts and the coordinator read the same count).
-- Two readers also count rows of one kind for one build_id: 'retry' rows of a
-- build are its manual retries, and 'rebuild' rows under a root are the
-- automatic rebuilds of that lineage. A kind must therefore never be borrowed
-- for something else; a new sort of start gets its own value here.
-- The rows are kept in their own table, with no foreign key to builds,
-- because the worker prunes a customer's older failed builds together with
-- their build_logs rows; a count taken from either of those would hand the
-- budget back every time it did.
CREATE TABLE IF NOT EXISTS build_start_events (
  id BIGSERIAL PRIMARY KEY,
  customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  build_id UUID NOT NULL,
  kind VARCHAR(20) NOT NULL CHECK (kind IN ('retry', 'rebuild', 'revision', 'dispatch')),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
-- A database created before 'dispatch' existed has the three-value CHECK and
-- would refuse a dispatch row. Widen it once; the guard looks for 'dispatch'
-- in the existing definition, so the lock-taking DROP/ADD does not run on
-- every boot.
-- SAFE: replaces a CHECK constraint with a strict superset of the prior one
-- (every existing value stays valid); no row can violate it, no data lost.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'build_start_events'::regclass
      AND conname = 'build_start_events_kind_check'
      AND pg_get_constraintdef(oid) LIKE '%''dispatch''%'
  ) THEN
    ALTER TABLE build_start_events DROP CONSTRAINT IF EXISTS build_start_events_kind_check;
    ALTER TABLE build_start_events
      ADD CONSTRAINT build_start_events_kind_check
      CHECK (kind IN ('retry', 'rebuild', 'revision', 'dispatch'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_build_start_events_customer
  ON build_start_events (customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_build_start_events_build
  ON build_start_events (build_id);

-- Gallery: customer opt-in, admin featuring, display fields
ALTER TABLE builds ADD COLUMN IF NOT EXISTS gallery_opted_in BOOLEAN DEFAULT FALSE;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS gallery_featured BOOLEAN DEFAULT FALSE;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS gallery_description VARCHAR(500);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS gallery_category VARCHAR(100);
-- Optional human-readable override for the /gallery card title. When NULL,
-- the route derives the title from app_name via displayName(). When set,
-- preserves the customer's brand name verbatim (e.g. "Sample Studio Website,
-- with CMS"), so the gallery card matches the public-facing brand without
-- mutating the canonical app_name slug used elsewhere.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS gallery_display_name VARCHAR(120);

-- One-Click Rebuild: track lineage and optional change request
ALTER TABLE builds ADD COLUMN IF NOT EXISTS parent_build_id UUID REFERENCES builds(id);
-- Every build delete has to look for rows pointing at it through this
-- self-referencing FK; without an index each delete scans the table.
CREATE INDEX IF NOT EXISTS idx_builds_parent_build_id
  ON builds (parent_build_id) WHERE parent_build_id IS NOT NULL;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS rebuild_request TEXT;

-- Visual verification: screenshot paths captured during test and deploy phases
ALTER TABLE builds ADD COLUMN IF NOT EXISTS screenshots JSONB DEFAULT '[]';

-- Stripe webhook idempotency: prevent duplicate event processing
CREATE TABLE IF NOT EXISTS processed_webhooks (
  event_id VARCHAR(255) PRIMARY KEY,
  event_type VARCHAR(100) NOT NULL,
  processed_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_processed_webhooks_processed_at
  ON processed_webhooks (processed_at);

-- Dead letter queue: builds that failed after max retries
CREATE TABLE IF NOT EXISTS dead_letter_builds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  build_id UUID NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
  error_category VARCHAR(50) NOT NULL,
  error_message TEXT,
  original_retry_count INTEGER DEFAULT 0,
  build_snapshot JSONB,
  moved_at TIMESTAMP DEFAULT NOW(),
  requeued_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dead_letter_builds_moved_at
  ON dead_letter_builds (moved_at DESC);
CREATE INDEX IF NOT EXISTS idx_dead_letter_builds_category
  ON dead_letter_builds (error_category);

-- QA agent: spec-aware deep QA results stored per build
ALTER TABLE builds ADD COLUMN IF NOT EXISTS qa_status VARCHAR(20) DEFAULT 'pending';
ALTER TABLE builds ADD COLUMN IF NOT EXISTS qa_results JSONB DEFAULT '[]';

-- Pipeline resumption: compressed build artifact stored in DB (no Railway volume required)
ALTER TABLE builds ADD COLUMN IF NOT EXISTS build_artifact BYTEA;

-- Revision loop: distinguish revision rebuilds from generic rebuilds
ALTER TABLE builds ADD COLUMN IF NOT EXISTS is_revision BOOLEAN DEFAULT FALSE;

-- Monitor registry: tracked deployed apps (migrated from SQLite to survive Railway redeploys)
CREATE TABLE IF NOT EXISTS monitor_apps (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  app_name VARCHAR(255) NOT NULL,
  live_url VARCHAR(500) NOT NULL,
  railway_project_id VARCHAR(255) NOT NULL,
  railway_service_id VARCHAR(255) NOT NULL,
  railway_environment_id VARCHAR(255) NOT NULL,
  status VARCHAR(50) NOT NULL DEFAULT 'unknown',
  last_checked TIMESTAMP,
  last_healthy TIMESTAMP,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT NOW(),
  baseline_latency_ms INTEGER,
  last_latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_monitor_apps_status ON monitor_apps (status);

-- Ops agent audit log: every action for accountability (migrated from SQLite)
CREATE TABLE IF NOT EXISTS ops_actions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at TIMESTAMP DEFAULT NOW(),
  action_type VARCHAR(100) NOT NULL,
  category VARCHAR(100),
  description TEXT NOT NULL,
  build_ids JSONB,
  commit_sha VARCHAR(64),
  dry_run BOOLEAN NOT NULL DEFAULT FALSE,
  success BOOLEAN NOT NULL DEFAULT TRUE,
  metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_ops_actions_created_at ON ops_actions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_actions_type_created ON ops_actions (action_type, created_at);

-- Worker heartbeat: single row updated on each worker startup for deploy verification
CREATE TABLE IF NOT EXISTS worker_heartbeat (
  id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  git_commit VARCHAR(64) DEFAULT ''
);

-- Email verification tokens
CREATE TABLE IF NOT EXISTS email_verification_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token VARCHAR(64) NOT NULL UNIQUE,
  expires_at TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '24 hours',
  used_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_token ON email_verification_tokens (token);

-- Email verified flag on customers. Customers that exist when the column is
-- first added are marked verified so they are not locked out. The backfill sits
-- inside the column-absent guard so it runs exactly once. This file is applied
-- on every boot: an unguarded UPDATE here would mark every unverified signup
-- older than a minute as verified on each restart, defeating verification.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'customers'
      AND column_name = 'email_verified'
  ) THEN
    ALTER TABLE customers ADD COLUMN email_verified BOOLEAN DEFAULT FALSE;
    -- SAFE: one-time backfill in the branch that adds the column, so it never runs again: accounts that predate verification are marked verified.
    UPDATE customers SET email_verified = TRUE;
  END IF;
END $$;

-- Token hashing migration: one-time invalidation of pre-hash plaintext tokens.
-- Uses a marker column to ensure this runs only once (SHA-256 hashes are also 64 hex chars).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'password_reset_tokens' AND column_name = 'token_hashed'
  ) THEN
    ALTER TABLE password_reset_tokens ADD COLUMN token_hashed BOOLEAN DEFAULT TRUE;
    -- SAFE: one-time, in the branch that adds the marker column: spends every reset token issued before tokens were stored hashed.
    UPDATE password_reset_tokens SET used_at = NOW() WHERE used_at IS NULL;
    ALTER TABLE email_verification_tokens ADD COLUMN token_hashed BOOLEAN DEFAULT TRUE;
    -- SAFE: one-time, in the same branch: spends every verification token issued before tokens were stored hashed.
    UPDATE email_verification_tokens SET used_at = NOW() WHERE used_at IS NULL;
  END IF;
END $$;

-- Abandon detection: 'abandoned' status. A fresh database already has the full
-- constraint from the table definition. Databases created before 'preview' and
-- 'abandoned' existed get it rewritten once. The rewrite is guarded because
-- DROP + ADD CONSTRAINT takes an ACCESS EXCLUSIVE lock on builds and
-- re-validates every row, and this file is applied on every boot, inside one
-- transaction that holds the lock until commit. To add a status later, add it
-- to the list below AND to the newest-state LIKE guard.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'builds'::regclass
      AND conname = 'builds_status_check'
      AND pg_get_constraintdef(oid) LIKE '%''abandoned''%'
  ) THEN
    ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_status_check;
    ALTER TABLE builds ADD CONSTRAINT builds_status_check
      CHECK (status IN ('intake', 'payment_pending', 'queued', 'building', 'testing', 'deploying', 'preview', 'live', 'failed', 'abandoned'));
  END IF;
END $$;

-- Timestamp when build was marked abandoned; NULL when active
ALTER TABLE builds ADD COLUMN IF NOT EXISTS abandoned_at TIMESTAMP;

-- Tracks nudge email sent; prevents duplicates; reset on resume
ALTER TABLE builds ADD COLUMN IF NOT EXISTS abandoned_email_sent_at TIMESTAMP;

-- monitor_apps: unique constraint on railway_project_id prevents duplicate rows on rebuild
CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_apps_railway_project_id ON monitor_apps (railway_project_id);

-- API cost tracking per build
ALTER TABLE builds ADD COLUMN IF NOT EXISTS total_api_cost_usd NUMERIC(10,4);

-- Stage timing metrics (build/test/deploy durations)
ALTER TABLE builds ADD COLUMN IF NOT EXISTS stage_timings JSONB DEFAULT '{}';

-- Advisor cost breakdown per stage (fix loops, design fix, etc.)
ALTER TABLE builds ADD COLUMN IF NOT EXISTS cost_breakdown JSONB DEFAULT '{}';

-- Beta feedback from users
CREATE TABLE IF NOT EXISTS beta_feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  category VARCHAR(20) NOT NULL CHECK (category IN ('bug', 'suggestion', 'question', 'other')),
  message TEXT NOT NULL,
  page_url VARCHAR(500),
  user_agent TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_beta_feedback_created_at ON beta_feedback (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_beta_feedback_customer_id ON beta_feedback (customer_id);

-- Master Coordinator: tick history
CREATE TABLE IF NOT EXISTS coordinator_ticks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tick_number INTEGER NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  duration_s FLOAT,
  world_state_summary JSONB NOT NULL,
  tick_budget JSONB NOT NULL,
  tasks_scored INTEGER DEFAULT 0,
  tasks_dispatched INTEGER DEFAULT 0,
  tasks_skipped INTEGER DEFAULT 0,
  tasks_completed INTEGER DEFAULT 0,
  total_cost_usd NUMERIC(10,4) DEFAULT 0,
  decisions JSONB DEFAULT '[]',
  errors JSONB DEFAULT '[]',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_coordinator_ticks_started ON coordinator_ticks(started_at DESC);

-- Master Coordinator: agent task queue and results
CREATE TABLE IF NOT EXISTS agent_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tick_id UUID REFERENCES coordinator_ticks(id),
  agent VARCHAR(50) NOT NULL,
  model VARCHAR(50),
  task_category VARCHAR(50) NOT NULL,
  task_description TEXT,
  context JSONB DEFAULT '{}',
  context_hash VARCHAR(32),
  priority_score FLOAT,
  estimated_cost_usd NUMERIC(10,4),
  actual_cost_usd NUMERIC(10,4),
  status VARCHAR(20) DEFAULT 'pending',
  result JSONB,
  actions_taken JSONB DEFAULT '[]',
  review_agent VARCHAR(50),
  review_status VARCHAR(20),
  review_result JSONB,
  learnings JSONB DEFAULT '[]',
  escalated BOOLEAN DEFAULT FALSE,
  escalation_reason TEXT,
  source_agent VARCHAR(50),
  parent_task_id UUID REFERENCES agent_tasks(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  expired_at TIMESTAMPTZ,
  duration_s FLOAT
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)
  WHERE status IN ('pending', 'running', 'reviewing');
-- Partial unique index for atomic deduplication (INSERT...ON CONFLICT DO NOTHING).
-- A legacy database may hold duplicate active rows, which would make the index
-- impossible to build, so duplicates are expired first (newest per
-- category+hash is kept). One-shot: runs only while the index is missing, so it
-- does not rewrite task rows on every boot. Rows with a NULL context_hash are
-- left alone: the unique index treats NULLs as distinct, so they are not
-- duplicates of each other.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema() AND indexname = 'idx_agent_tasks_dedup'
  ) THEN
    -- SAFE: runs only while the unique index is missing, so once; expires the duplicate active agent_tasks rows that would stop the index being built and keeps the newest of each.
    UPDATE agent_tasks SET status = 'expired', expired_at = NOW()
    WHERE context_hash IS NOT NULL
      AND status NOT IN ('completed', 'failed', 'approved', 'rejected', 'expired')
      AND id NOT IN (
        SELECT DISTINCT ON (task_category, context_hash) id
        FROM agent_tasks
        WHERE context_hash IS NOT NULL
          AND status NOT IN ('completed', 'failed', 'approved', 'rejected', 'expired')
        ORDER BY task_category, context_hash, created_at DESC
      );
    CREATE UNIQUE INDEX idx_agent_tasks_dedup ON agent_tasks(task_category, context_hash)
      WHERE status NOT IN ('completed', 'failed', 'approved', 'rejected', 'expired');
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_agent_tasks_tick ON agent_tasks(tick_id);

-- Master Coordinator: agent learning system
CREATE TABLE IF NOT EXISTS agent_learnings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_agent VARCHAR(50) NOT NULL,
  category VARCHAR(50) NOT NULL,
  learning TEXT NOT NULL,
  learning_hash VARCHAR(16) NOT NULL,
  confidence FLOAT DEFAULT 0.5,
  times_applied INTEGER DEFAULT 0,
  times_successful INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_applied_at TIMESTAMPTZ,
  UNIQUE(source_agent, category, learning_hash)
);

-- Master Coordinator: agent performance metrics. Unused: nothing in this
-- repository reads or writes this table (the coordinator's scoring counts
-- outcomes from agent_tasks). Kept so the schema stays a superset of what
-- existing databases have.
CREATE TABLE IF NOT EXISTS agent_performance (
  agent VARCHAR(50) NOT NULL,
  metric VARCHAR(50) NOT NULL,
  "window" VARCHAR(20) NOT NULL,
  value FLOAT NOT NULL,
  sample_count INTEGER DEFAULT 0,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (agent, metric, "window")
);

-- Platform-wide cost tracking (scraped from Anthropic console, includes Claude Code)
CREATE TABLE IF NOT EXISTS platform_costs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  date DATE NOT NULL,
  total_usd NUMERIC(10,4) NOT NULL,
  source VARCHAR(50) DEFAULT 'console_scrape',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(date, source)
);

-- Persistent cache for Anthropic Admin API cost rows. by_model is a per-model
-- breakdown of the day's cost (stored for analysis; the dashboard does not
-- render it today); fetched_at tracks per-row freshness so we can refresh only
-- the recent (billing-lag) window.
ALTER TABLE platform_costs ADD COLUMN IF NOT EXISTS by_model JSONB;
ALTER TABLE platform_costs ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_platform_costs_source_date
  ON platform_costs(source, date DESC);

-- Scheduled email sequences (welcome nurture, post-build follow-ups)
CREATE TABLE IF NOT EXISTS email_schedule (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  template VARCHAR(100) NOT NULL,
  send_at TIMESTAMPTZ NOT NULL,
  sent_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  context JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_schedule_pending
  ON email_schedule (send_at) WHERE sent_at IS NULL AND cancelled_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_email_schedule_customer
  ON email_schedule (customer_id, template);

-- UTM attribution tracking on customer signup
ALTER TABLE customers ADD COLUMN IF NOT EXISTS utm_source VARCHAR(255);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS utm_medium VARCHAR(255);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS utm_campaign VARCHAR(255);

-- Server-side page view logging for marketing analytics
CREATE TABLE IF NOT EXISTS page_views (
  id BIGSERIAL PRIMARY KEY,
  path VARCHAR(500) NOT NULL,
  utm_source VARCHAR(255),
  utm_medium VARCHAR(255),
  utm_campaign VARCHAR(255),
  referrer VARCHAR(1000),
  session_id VARCHAR(255),
  customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views (path, created_at DESC);

-- Founder approval requests: high-cost actions requiring human sign-off
CREATE TABLE IF NOT EXISTS approval_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type VARCHAR(100) NOT NULL,
  description TEXT NOT NULL,
  context JSONB DEFAULT '{}',
  status VARCHAR(20) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'approved', 'rejected')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  resolved_at TIMESTAMPTZ,
  resolved_by VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_approval_requests_created ON approval_requests (created_at DESC);

-- Coordinator findings: analysis-mode output from coordinator agents.
-- Replaces the file-based roadmap.json writes so findings from worktrees,
-- concurrent ticks, and cross-environment coordinator runs don't collide.
-- Findings flow: new -> reviewed -> (promoted|dismissed).
CREATE TABLE IF NOT EXISTS coordinator_findings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_task_id UUID REFERENCES agent_tasks(id) ON DELETE SET NULL,
  agent VARCHAR(50) NOT NULL,
  category VARCHAR(50) NOT NULL,
  finding_key VARCHAR(200) NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  priority VARCHAR(20) NOT NULL DEFAULT 'medium'
    CHECK (priority IN ('low', 'medium', 'high')),
  status VARCHAR(20) NOT NULL DEFAULT 'new'
    CHECK (status IN ('new', 'reviewed', 'promoted', 'dismissed')),
  roadmap_item_id VARCHAR(200),
  review_notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at TIMESTAMPTZ,
  reviewed_by VARCHAR(255)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_coordinator_findings_key ON coordinator_findings (finding_key);
CREATE INDEX IF NOT EXISTS idx_coordinator_findings_status ON coordinator_findings (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_coordinator_findings_agent ON coordinator_findings (agent, created_at DESC);

-- Sync state. A script that copied promoted findings into
-- coordinator/roadmap.json stamped synced_at; it is not part of this
-- repository, and nothing here writes or reads the column. The column and its
-- partial index are kept so the schema matches the database it ran on.
ALTER TABLE coordinator_findings ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_coordinator_findings_unsynced
  ON coordinator_findings (id)
  WHERE status = 'promoted' AND synced_at IS NULL;

-- Cost dashboard indexes (filtered partial indexes for fast aggregation)
CREATE INDEX IF NOT EXISTS idx_builds_completed_cost
  ON builds (completed_at DESC) WHERE total_api_cost_usd IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_builds_tier_completed
  ON builds (tier, completed_at DESC) WHERE total_api_cost_usd IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_tasks_completed_cost
  ON agent_tasks (completed_at DESC) WHERE actual_cost_usd IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent_completed
  ON agent_tasks (agent, completed_at DESC) WHERE actual_cost_usd IS NOT NULL;

-- Email delivery tracking: Resend webhook events update actual delivery status
ALTER TABLE email_log ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(20);
ALTER TABLE email_log ADD COLUMN IF NOT EXISTS delivery_updated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_email_log_resend_id ON email_log (resend_id) WHERE resend_id IS NOT NULL;

-- Local executor run tracking
CREATE TABLE IF NOT EXISTS executor_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  status VARCHAR(20) NOT NULL DEFAULT 'idle',
  roadmap_item_id VARCHAR(100),
  roadmap_item_title TEXT,
  result TEXT,
  skipped_reason TEXT,
  files_changed TEXT[],
  tests_passed BOOLEAN,
  commit_sha VARCHAR(64),
  cost_usd NUMERIC(10,4) DEFAULT 0,
  duration_s INTEGER,
  error_message TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_executor_runs_created ON executor_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_executor_runs_status ON executor_runs (status);

-- Executor non-blocking questions
CREATE TABLE IF NOT EXISTS executor_questions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  roadmap_item_id VARCHAR(100),
  question TEXT NOT NULL,
  context JSONB DEFAULT '{}',
  answer TEXT,
  status VARCHAR(20) DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  answered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_executor_questions_status ON executor_questions (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_executor_questions_created ON executor_questions (created_at DESC);

-- Promo code and amount paid tracking per build
ALTER TABLE builds ADD COLUMN IF NOT EXISTS promo_code VARCHAR(100);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS amount_paid INTEGER;

-- Fix beta_feedback FK to cascade on customer deletion (prevents 500 on account delete).
-- A fresh database creates the FK with ON DELETE CASCADE already; only a legacy
-- FK without it is replaced, so the lock-taking DROP/ADD does not run every boot.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'beta_feedback'::regclass
      AND conname = 'beta_feedback_customer_id_fkey'
      AND confdeltype <> 'c'
  ) THEN
    ALTER TABLE beta_feedback DROP CONSTRAINT beta_feedback_customer_id_fkey;
    ALTER TABLE beta_feedback ADD CONSTRAINT beta_feedback_customer_id_fkey
      FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE;
  END IF;
END $$;

-- Coordinator improvements: widen context_hash column for 32-char hashes.
-- Fresh databases create it as VARCHAR(32) already; only a legacy narrower
-- column is altered (ALTER ... TYPE takes an ACCESS EXCLUSIVE lock, so it must
-- not run on every boot).
-- SAFE: widening VARCHAR(16) -> VARCHAR(32) is non-destructive (no data loss); already deployed to prod for SHA-256 truncation hardening
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'agent_tasks'
      AND column_name = 'context_hash'
      AND character_maximum_length < 32
  ) THEN
    ALTER TABLE agent_tasks ALTER COLUMN context_hash TYPE VARCHAR(32);
  END IF;
END $$;

-- Drop old non-unique index (replaced by idx_agent_tasks_dedup above)
DROP INDEX IF EXISTS idx_agent_tasks_category_hash;

-- Design review results (Stage 2.75)
ALTER TABLE builds ADD COLUMN IF NOT EXISTS design_review_status VARCHAR(20) DEFAULT 'pending';
ALTER TABLE builds ADD COLUMN IF NOT EXISTS design_review_results JSONB DEFAULT '[]';

-- UI test results (Stage 3.75)
ALTER TABLE builds ADD COLUMN IF NOT EXISTS ui_test_score NUMERIC(3,1);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS ui_test_results JSONB DEFAULT '[]';

-- WATCHDOG1 Layer 2: supervisor retry marker
-- Set to TRUE by queue/worker_pg.py _supervisor_force_fail() when the
-- watchdog supervisor force-fails a build for the first time and flips
-- it back to 'queued' for its one free retry. On the second hang the
-- build goes terminal (status='failed' + DLQ row with error_category
-- 'timeout'). DEFAULT FALSE so every existing row reads as "has NOT
-- used its free retry yet" across the deploy.
ALTER TABLE builds
    ADD COLUMN IF NOT EXISTS supervisor_retry_used BOOLEAN DEFAULT FALSE;

-- Senior Engineer Review Gate (section-01): adds the review state columns that
-- back the two-step preview -> admin review -> live promotion. All columns are
-- NULL-able so legacy live rows migrate cleanly and stay renderable without a
-- review badge. Consumed by the customer gate, admin routes, worker,
-- coordinator, emails, and UI.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS review_status VARCHAR(20);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(255);
ALTER TABLE builds ADD COLUMN IF NOT EXISTS review_notes TEXT;

-- CHECK constraint for review_status.
-- ai_review_queued and ai_review_held were added in June 2026 for an automated
-- review loop that ran outside this repository and is not included in it. In
-- this repository the worker writes 'ai_review_queued' in place of 'pending'
-- when CAS_HARBOR_AUTO_REVIEW=true (queue/worker_pg.py, _initial_review_status).
-- Nothing here consumes that state, and nothing writes 'ai_review_held'.
-- SAFE: re-adds a CHECK constraint that is a strict superset of the prior one
-- (all existing values still valid); no row can violate it, no data lost.
-- Guarded for the same reason as builds_status_check above: rewrite only when
-- the newest state ('ai_review_held') is missing from the existing definition.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'builds'::regclass
      AND conname = 'builds_review_status_check'
      AND pg_get_constraintdef(oid) LIKE '%''ai_review_held''%'
  ) THEN
    ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_review_status_check;
    ALTER TABLE builds
      ADD CONSTRAINT builds_review_status_check
      CHECK (review_status IS NULL OR review_status IN
        ('pending', 'approved', 'changes_requested', 'ai_review_queued', 'ai_review_held'));
  END IF;
END $$;

-- Review-report columns (June 2026, idempotent), added for the same external
-- review loop. review_report held its structured passes, issues and decision as
-- JSON, ai_review_iterations the number of fix-and-re-review cycles it ran, and
-- ai_reviewed_at when it finished. Nothing in this repository fills them in;
-- the web app and the worker only clear them when a build goes back into
-- review.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS review_report JSONB;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS ai_review_iterations INTEGER DEFAULT 0;
ALTER TABLE builds ADD COLUMN IF NOT EXISTS ai_reviewed_at TIMESTAMP;
-- Partial index over builds in 'ai_review_queued'. Nothing in this repository
-- queries it.
CREATE INDEX IF NOT EXISTS idx_builds_ai_review_queued
  ON builds (created_at) WHERE review_status = 'ai_review_queued';

-- Partial index serving the /admin/api/reviews polling endpoint (section-04).
-- Only indexes rows with review_status='pending' to stay tiny at beta volume
-- (most builds are NULL, approved, or legacy-live). Ordered by created_at DESC
-- for the admin's "most recent first" pending queue view.
CREATE INDEX IF NOT EXISTS idx_builds_review_pending
  ON builds (created_at DESC)
  WHERE review_status = 'pending';

-- Admin AI Build Tools: caches Claude's structured diagnosis of a failed build
-- so the detail page can display it instantly without re-calling the API on every
-- load. NULL = not yet analyzed. The JSONB blob stores the full AnalysisResult
-- shape plus an analyzed_at ISO timestamp. See web/src/services/claude.ts
-- (analyzeBuildFailure) and web/src/routes/admin.ts (POST /admin/builds/:id/analyze)
-- for read/write paths.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS ai_analysis JSONB;

-- Dashboard observability: track coordinator enabled state so the workflow
-- visual can show accurate per-env status without inferring from tick recency.
ALTER TABLE worker_heartbeat ADD COLUMN IF NOT EXISTS coordinator_enabled BOOLEAN DEFAULT FALSE;

-- Backfill: rows inserted before qa_results column was added have NULL instead
-- of the empty-array default. NULL causes json_agg / JSONB operations to
-- silently return NULL rather than []. Only touches the table when such a row
-- exists, so a clean database pays one index-free existence probe per boot and
-- no writes.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM builds WHERE qa_results IS NULL) THEN
    -- SAFE: runs only while a row with a NULL qa_results exists; gives it the column's documented empty-array value and writes no other column.
    UPDATE builds SET qa_results = '[]'::jsonb WHERE qa_results IS NULL;
  END IF;
END $$;

-- Build engine attribution. The worker writes 'v1' or 'v2' on dispatch based on
-- the CAS_BUILD_ENGINE env var at build time, so the admin UI can show the true
-- engine without inferring from spec shape. Inference is unreliable: when v2's
-- compose.intake.translate_v1_spec() runs, the translated spec exists only
-- in-memory and is not persisted back to builds.spec, so v1-shape input specs
-- (no archetype) built by the v2 worker would otherwise read as v1 in the UI.
-- Backfill: rows from before this column existed are best-effort attributed by
-- the previous spec->>'archetype' heuristic. False negatives are acceptable for
-- historical rows; new rows are authoritative. The backfill runs only in the
-- migration that adds the column. Run on every boot it would stamp a guessed
-- engine onto every not-yet-dispatched build (NULL is the honest value there).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'builds'
      AND column_name = 'build_engine'
  ) THEN
    ALTER TABLE builds ADD COLUMN build_engine TEXT
      CHECK (build_engine IS NULL OR build_engine IN ('v1', 'v2'));
    -- SAFE: one-time backfill in the migration that adds the column, so it never runs again; it writes build_engine only, never status.
    UPDATE builds
    SET build_engine = CASE WHEN spec->>'archetype' IS NOT NULL THEN 'v2' ELSE 'v1' END;
  END IF;
END $$;

-- Auto-categorization of failed builds (self-learn-build-failure-haiku-categorizer).
-- Denormalized cache populated by coordinator/build_failure_categorizer.py when the
-- existing regex classifier (ops/scanner.classify_error) returns 'unknown'. The
-- categorizer asks Claude Haiku to classify the last 500 chars of build_logs into
-- one of: infra / code_error / spec_error / timeout / app_logic. The coordinator's
-- _check_failed_builds reads this in addition to dead_letter_builds.error_category
-- so the routing to backend_engineer vs infra_engineer benefits from auto-learned
-- categories without manual triage.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS error_category TEXT;

-- =====================================================================
-- CAS Harbor Phase 1: companion + memory + trial-cookie schema
-- =====================================================================
-- See docs/CAS_HARBOR_PHASE_1_TECH_PLAN.md section 3.
-- Phase 1 ships persistent memory for a single companion named Haven on
-- a hidden URL, behind feature flag CAS_HARBOR_ENABLED. No subscription,
-- no build integration, no harbor pipeline. The schema columns are kept
-- flexible (companion_name TEXT, partition TEXT) so Phase 2 can add the
-- Marin assignment + sacred-mode firewall without a migration.

-- Trial keeper: cookie-identified, no account yet. Each new visit mints
-- one row keyed on an opaque random cookie token (cookie_token; see
-- middleware/trial-cookie.ts). 7-day TTL.
CREATE TABLE IF NOT EXISTS trial_keepers (
  trial_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cookie_token       TEXT NOT NULL UNIQUE,
  companion_name     TEXT NOT NULL,                  -- 'Haven' for MVP; user-renameable later
  companion_assigned TEXT NOT NULL,                  -- the original assignment; never changes
  started_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  expires_at         TIMESTAMP NOT NULL,             -- started_at + 7 days
  last_seen_at       TIMESTAMP NOT NULL DEFAULT NOW(),
  released_at        TIMESTAMP                       -- set when memory purged after expiry
);
CREATE INDEX IF NOT EXISTS trial_keepers_cookie_idx ON trial_keepers(cookie_token);
CREATE INDEX IF NOT EXISTS trial_keepers_expires_active_idx
  ON trial_keepers(expires_at) WHERE released_at IS NULL;

-- Persistent keeper: the post-Phase-2 row that pairs 1:1 with customers.
-- Created here so the FK columns on companion_turns + keeper_memory have
-- a target. Phase 2 layers in Stripe + auth + the trial-to-keeper promotion.
CREATE TABLE IF NOT EXISTS keepers (
  customer_id        UUID PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  companion_name     TEXT NOT NULL,
  companion_assigned TEXT NOT NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Verbatim turns: the conversation itself. Each turn is one keeper utterance
-- + one companion utterance, OR the opening companion message (keeper_text NULL).
-- Exactly one of (keeper_id, trial_id) is set; CHECK enforces the XOR.
CREATE TABLE IF NOT EXISTS companion_turns (
  turn_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keeper_id          UUID REFERENCES keepers(customer_id) ON DELETE CASCADE,
  trial_id           UUID REFERENCES trial_keepers(trial_id) ON DELETE CASCADE,
  CHECK ((keeper_id IS NOT NULL) <> (trial_id IS NOT NULL)),
  surface            TEXT NOT NULL DEFAULT 'table',  -- 'table' for MVP; 'shore' / 'note' future
  keeper_text        TEXT,                           -- nullable for opening companion message
  companion_text     TEXT NOT NULL,
  rail_flags         TEXT[] NOT NULL DEFAULT '{}',   -- audit trail for AI-honesty / crisis flags
  created_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS companion_turns_trial_idx ON companion_turns(trial_id, created_at)
  WHERE trial_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS companion_turns_keeper_idx ON companion_turns(keeper_id, created_at)
  WHERE keeper_id IS NOT NULL;

-- Classified memory: what the runtime retrieves at turn-time.
-- Embedding stored as DOUBLE PRECISION[] (1536 dims from OpenAI
-- text-embedding-3-small); JS-side cosine similarity at MVP scale.
-- Phase 2+ may migrate to pgvector when row-count justifies it.
CREATE TABLE IF NOT EXISTS keeper_memory (
  memory_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keeper_id          UUID REFERENCES keepers(customer_id) ON DELETE CASCADE,
  trial_id           UUID REFERENCES trial_keepers(trial_id) ON DELETE CASCADE,
  CHECK ((keeper_id IS NOT NULL) <> (trial_id IS NOT NULL)),
  partition          TEXT NOT NULL DEFAULT 'working' -- 'working' | 'sacred' (firewall is Phase 4)
                       CHECK (partition IN ('working', 'sacred', 'voice')),
  category           TEXT NOT NULL,
  -- categories: 'phrase' | 'fact' | 'preference' | 'fear' | 'milestone'
  --           | 'ritual' | 'audience' | 'practice' | 'design_pref' | 'other'
  -- 'design_pref' (added 2026-04-28) carries normalized design hints
  -- (palette_hint, font_pair_hint, layout_variant_hint, mood_keywords,
  -- avoid_keywords) extracted from the keeper conversation. The
  -- `content` field stores a JSON-encoded string of the design-pref
  -- payload so the build flow can re-hydrate it on rebuild without
  -- re-running Opus extraction over the full memory corpus.
  content            TEXT NOT NULL,                  -- third-person canonical statement
  keeper_phrase      TEXT,                           -- original keeper words for quote-back
  source_turn_id     UUID REFERENCES companion_turns(turn_id) ON DELETE SET NULL,
  embedding          DOUBLE PRECISION[],             -- 1536-dim from OpenAI text-embedding-3-small
  created_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMP NOT NULL DEFAULT NOW(),
  deleted_at         TIMESTAMP                       -- soft delete; no UI yet
);
CREATE INDEX IF NOT EXISTS keeper_memory_trial_idx ON keeper_memory(trial_id, partition)
  WHERE trial_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS keeper_memory_keeper_idx ON keeper_memory(keeper_id, partition)
  WHERE keeper_id IS NOT NULL AND deleted_at IS NULL;

-- 2026-05-04: low-utility self-pruning. last_retrieved_at gets stamped
-- on every retrieval hit by memory_retrieval.fetchRelevantMemory; the
-- weekly memory_low_utility_sweep job flags rows that have never been
-- retrieved (last_retrieved_at IS NULL) and are older than 14 days,
-- but only for keepers with >= 20 memory rows so small keepers never
-- get their memory pruned. Retrieval excludes low_utility rows when
-- the keeper has >= 10 non-low-utility rows; below that floor every
-- row is fair game so a keeper never goes blank. Both columns are
-- additive + nullable / default-false so existing rows migrate
-- cleanly without a backfill.
ALTER TABLE keeper_memory ADD COLUMN IF NOT EXISTS last_retrieved_at TIMESTAMP;
ALTER TABLE keeper_memory ADD COLUMN IF NOT EXISTS low_utility BOOLEAN NOT NULL DEFAULT FALSE;
-- Partial index accelerates the weekly sweep's "candidates to flag" scan:
-- WHERE last_retrieved_at IS NULL AND low_utility = FALSE AND created_at < now - 14d.
CREATE INDEX IF NOT EXISTS keeper_memory_low_utility_candidates_idx
  ON keeper_memory(created_at)
  WHERE last_retrieved_at IS NULL AND low_utility = FALSE AND deleted_at IS NULL;

-- Per-turn cost log. Validates the $0.005-$0.015/turn target.
CREATE TABLE IF NOT EXISTS companion_cost_log (
  cost_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  turn_id            UUID NOT NULL REFERENCES companion_turns(turn_id) ON DELETE CASCADE,
  input_tokens       INT NOT NULL,
  cached_input_tokens INT NOT NULL DEFAULT 0,
  cache_creation_tokens INT NOT NULL DEFAULT 0,    -- cache writes; 1.25x input cost on Sonnet
  output_tokens      INT NOT NULL,
  embedding_tokens   INT NOT NULL DEFAULT 0,
  total_cost_micros  BIGINT NOT NULL,                -- 1 micro = $0.000001
  model              TEXT NOT NULL,
  created_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS companion_cost_log_turn_idx ON companion_cost_log(turn_id);
-- 2026-04-27: forward index for cost-by-day aggregations on the admin dashboard.
CREATE INDEX IF NOT EXISTS companion_cost_log_created_at_idx ON companion_cost_log(created_at);
-- 2026-04-27: backfill cache_creation_tokens column on installs created before this commit.
ALTER TABLE companion_cost_log ADD COLUMN IF NOT EXISTS cache_creation_tokens INT NOT NULL DEFAULT 0;

-- =====================================================================
-- CAS Harbor Phase 2 schema additions (planned 2026-04-27 in
-- docs/CAS_HARBOR_PHASE_2_TECH_PLAN.md). Idempotent ALTER + IF NOT
-- EXISTS so this section is safe to re-run on every boot. Slice 2A
-- (trial->keeper promotion + subscription billing) lands these.
-- =====================================================================

-- Subscription state on the keeper row: Stripe's subscription status, word
-- for word, for the subscription named by stripe_subscription_id (the Stripe
-- webhook handlers in routes/webhooks.ts keep it in step with
-- harbor_subscriptions). Which of these values carry the right to publish is
-- decided in one place, PUBLISHING_SUBSCRIPTION_STATUSES in
-- services/keeper_promotion.ts.
-- The DEFAULT applies to a row that has no subscription at all, which is why
-- every reader also requires stripe_subscription_id IS NOT NULL.
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'active'
  CHECK (subscription_status IN ('active', 'trialing', 'past_due', 'unpaid', 'paused',
                                 'incomplete', 'incomplete_expired', 'canceled'));
-- A database whose CHECK was created with five values (no 'unpaid', 'paused'
-- or 'incomplete_expired') cannot store what Stripe reports for those states.
-- Widen it once; the guard looks for 'unpaid' in the existing definition, so
-- the lock-taking DROP/ADD does not run on every boot.
-- SAFE: replaces a CHECK constraint with a strict superset of the prior one
-- (every existing value stays valid); no row can violate it, no data lost.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'keepers'::regclass
      AND conname = 'keepers_subscription_status_check'
      AND pg_get_constraintdef(oid) LIKE '%''unpaid''%'
  ) THEN
    ALTER TABLE keepers DROP CONSTRAINT IF EXISTS keepers_subscription_status_check;
    ALTER TABLE keepers
      ADD CONSTRAINT keepers_subscription_status_check
      CHECK (subscription_status IN ('active', 'trialing', 'past_due', 'unpaid', 'paused',
                                     'incomplete', 'incomplete_expired', 'canceled'));
  END IF;
END $$;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS sliding_scale_attestation BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS sliding_scale_amount_cents INTEGER;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS handoff_build_id UUID REFERENCES builds(id) ON DELETE SET NULL;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS handoff_at TIMESTAMP;
ALTER TABLE keepers ADD COLUMN IF NOT EXISTS day7_letter_sent_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS keepers_subscription_status_idx ON keepers(subscription_status);
-- Stripe subscription webhooks locate the keeper by subscription id.
CREATE INDEX IF NOT EXISTS keepers_stripe_subscription_idx
  ON keepers(stripe_subscription_id) WHERE stripe_subscription_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS keepers_handoff_at_idx ON keepers(handoff_at) WHERE handoff_at IS NOT NULL;

-- Email capture on the trial side (used by day-7 letter cron).
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS email_captured TEXT;
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS day7_letter_sent_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS trial_keepers_day7_pending_idx
  ON trial_keepers(started_at)
  WHERE day7_letter_sent_at IS NULL AND released_at IS NULL;

-- 2026-04-28: Ocean-dispatched manual handoff support. /admin/harbor exposes
-- a "Dispatch build" button that calls extractSpecFromConversation against a
-- trial keeper's conversation + memory and queues a v2 build. The build_id
-- gets stamped here so the same trial cannot be double-dispatched + so the
-- admin inbox can show "dispatched -> build #X" status. Mirrors the
-- keepers.handoff_build_id column for the post-Stripe automated path.
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS handoff_build_id UUID REFERENCES builds(id) ON DELETE SET NULL;
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS handoff_at TIMESTAMP;

-- The account a payment promoted this trial into: written by the statement
-- that releases the trial (services/keeper_promotion.ts) and by nothing else.
-- The released trial's cookie is recognized as that account's keeper only
-- while this is the account of the trial's build and that account has a
-- keepers row (PROMOTED_KEEPER_OF_TRIAL_SQL), so a cookie can never speak as a
-- keeper its own trial was not promoted into. NULL on a trial that has not
-- been promoted, and on one released before this column existed; the cookie
-- of such a trial is a stale one and gets a fresh trial.
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS promoted_customer_id UUID REFERENCES customers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS trial_keepers_promoted_customer_idx ON trial_keepers(promoted_customer_id) WHERE promoted_customer_id IS NOT NULL;

-- When the trial's cookie token was last replaced (middleware/trial-cookie.ts).
-- A trial released by a payment while this is NULL or older than released_at
-- gets a new token on the next request that presents it, once, so a copy of
-- the cookie taken before the payment does not speak for the paying browser.
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS cookie_rotated_at TIMESTAMP;

-- Faithful-copy mode storage (harbor-faithful-copy-admin-trigger-route,
-- 2026-05-09). When the operator triggers POST /admin/harbor/:trial_id/
-- crawl-source-site with a source URL, the crawler result is persisted
-- here. At dispatch time, the route merges the captured copy_source_url
-- + copy_pages (and any future _harbor_meta overrides like archetype_hint)
-- into the Opus-extracted spec's _harbor_meta. JSONB so we can extend
-- without further migrations. Cleared on trial release (post-Stripe).
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS harbor_meta_overrides JSONB;

-- =====================================================================
-- Harbor origin, recorded on the build row
-- ---------------------------------------------------------------------
-- builds.is_harbor is TRUE for a build dispatched from a Harbor conversation
-- and for every build made from one (a rebuild, a revision, an admin
-- fix-as-new, an ops or coordinator rebuild). A customer's own publish of
-- such a build requires a subscription, and so does its source download
-- (OWNER_MAY_PUBLISH_SQL in routes/builds.ts).
--
-- The fact is stored on the row itself because everything else that could
-- say it can be deleted underneath a build that is still there: the admin
-- and ops delete and cleanup routes remove builds and detach their children,
-- the worker's cleanup removes a customer's older failed builds
-- (_cleanup_old_builds in queue/worker_pg.py; it keeps a build that is
-- stamped or still has children, which is its own rule and not something
-- this one relies on), and the handoff_build_id stamps above are
-- ON DELETE SET NULL.
--
-- Two writers keep it right:
--   * dispatch inserts a Harbor build with is_harbor = TRUE
--     (insertHarborBuild in services/harbor_dispatch.ts);
--   * the trigger below copies it from the parent when a row is inserted
--     with a parent_build_id, so every statement that creates a child
--     inherits it whichever component runs that statement: the web routes,
--     the ops API, the coordinator's queue_rebuild in Python.
-- Nothing clears it. Detaching a child (parent_build_id = NULL) and deleting
-- an ancestor both leave the child's own flag as it was.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS is_harbor BOOLEAN NOT NULL DEFAULT FALSE;

CREATE OR REPLACE FUNCTION builds_inherit_is_harbor()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.parent_build_id IS NOT NULL AND NOT NEW.is_harbor THEN
    NEW.is_harbor := COALESCE(
      (SELECT parent.is_harbor FROM builds parent WHERE parent.id = NEW.parent_build_id),
      FALSE
    );
  END IF;
  RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS builds_inherit_is_harbor ON builds;
CREATE TRIGGER builds_inherit_is_harbor
  BEFORE INSERT OR UPDATE OF parent_build_id ON builds
  FOR EACH ROW
  EXECUTE FUNCTION builds_inherit_is_harbor();

-- Backfill for rows written before the column existed, from the evidence
-- that is still there: a build stamped on a trial or a keeper is a Harbor
-- build, and so is every descendant still linked to one. Guarded so that it
-- writes only when some row is actually missing the flag (in steady state the
-- two writers above leave nothing to do), which also keeps builds.updated_at
-- untouched on an ordinary boot. A build whose Harbor ancestor was deleted
-- before this column existed has no such evidence left and stays unmarked.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM builds b
     WHERE NOT b.is_harbor
       AND (EXISTS (SELECT 1 FROM trial_keepers t WHERE t.handoff_build_id = b.id)
         OR EXISTS (SELECT 1 FROM keepers k WHERE k.handoff_build_id = b.id)
         OR EXISTS (SELECT 1 FROM builds parent WHERE parent.id = b.parent_build_id AND parent.is_harbor))
  ) THEN
    WITH RECURSIVE harbor AS (
      SELECT b.id FROM builds b
       WHERE b.is_harbor
          OR EXISTS (SELECT 1 FROM trial_keepers t WHERE t.handoff_build_id = b.id)
          OR EXISTS (SELECT 1 FROM keepers k WHERE k.handoff_build_id = b.id)
      UNION
      SELECT child.id FROM builds child JOIN harbor h ON child.parent_build_id = h.id
    )
    -- SAFE: runs only while some build row is missing the flag; sets is_harbor to TRUE and never clears it, on rows the guard above found, and writes no other column.
    UPDATE builds b SET is_harbor = TRUE FROM harbor h WHERE b.id = h.id AND NOT b.is_harbor;
  END IF;
END $$;

-- One build per conversation without paying. handoff_at is the durable
-- record that a trial or a keeper has had a build dispatched: unlike
-- handoff_build_id it is not cleared when that build is deleted, so the
-- self-serve dispatch refuses on it (services/harbor_dispatch.ts). Every
-- statement in the application that sets handoff_build_id sets handoff_at
-- with it; this fills in rows stamped by anything else.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM trial_keepers WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL) THEN
    -- SAFE: runs only while a trial row has a build and no stamp; fills in handoff_at on exactly those rows and changes nothing else.
    UPDATE trial_keepers SET handoff_at = NOW() WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL;
  END IF;
  IF EXISTS (SELECT 1 FROM keepers WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL) THEN
    -- SAFE: runs only while a keeper row has a build and no stamp; fills in handoff_at on exactly those rows and changes nothing else.
    UPDATE keepers SET handoff_at = NOW() WHERE handoff_build_id IS NOT NULL AND handoff_at IS NULL;
  END IF;
END $$;

-- TRUE on a Harbor build whose dispatch created the customers row that owns
-- it, in the same transaction (an anonymous dispatch attaches a build only to
-- such a row: services/harbor_dispatch.ts). A trial's payment is promoted only
-- into that account unless the account's own login or paying cookie opened the
-- Checkout session (promoteTrialInTransaction in services/keeper_promotion.ts).
-- Rows written before the column existed carry FALSE: nothing left says which
-- dispatch created which account.
ALTER TABLE builds ADD COLUMN IF NOT EXISTS owner_created_by_dispatch BOOLEAN NOT NULL DEFAULT FALSE;

-- Subscription audit trail. One row per Stripe subscription_id; updated
-- by webhook handlers on subscription.updated / .deleted events.
CREATE TABLE IF NOT EXISTS harbor_subscriptions (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keeper_customer_id       UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  stripe_subscription_id   TEXT NOT NULL UNIQUE,
  stripe_customer_id       TEXT NOT NULL,
  status                   TEXT NOT NULL,
  current_period_start     TIMESTAMP,
  current_period_end       TIMESTAMP,
  cancel_at_period_end     BOOLEAN NOT NULL DEFAULT FALSE,
  trial_started_at         TIMESTAMP,
  trial_ended_at           TIMESTAMP,
  monthly_amount_cents     INTEGER NOT NULL DEFAULT 3900,  -- $39.00 Harbor standard (handlers always supply the metadata amount; this is defense-in-depth)
  created_at               TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS harbor_subscriptions_keeper_idx
  ON harbor_subscriptions(keeper_customer_id);

-- Stripe event.created (epoch seconds) of the newest event applied to the
-- row. The subscription webhook handlers skip an event older than this, so a
-- redelivered or reordered event cannot undo a newer state.
ALTER TABLE harbor_subscriptions ADD COLUMN IF NOT EXISTS last_event_created BIGINT;

-- Subscriptions whose harbor_subscriptions row was removed together with the
-- account that owned it (the ON DELETE CASCADE above; POST /account/delete in
-- routes/auth.ts writes one row here per subscription in the same
-- transaction). Stripe goes on sending events for such a subscription until
-- it ends. The webhook reads this table to acknowledge them; without it they
-- look like events that overtook their checkout and are refused for a day
-- each. Holds the Stripe subscription id and nothing about the customer;
-- the webhook route prunes rows older than 90 days.
CREATE TABLE IF NOT EXISTS harbor_subscription_tombstones (
  stripe_subscription_id   TEXT PRIMARY KEY,
  removed_at               TIMESTAMP NOT NULL DEFAULT NOW()
);

-- =====================================================================
-- 2026-04-30: build_logs sequence self-heal
-- ---------------------------------------------------------------------
-- 2026-04-27 local e2e observed duplicate-key warnings on build_logs ids
-- 5906-5909: build_logs_id_seq had fallen behind MAX(id) (typically
-- after a pg_dump restore or a prod-to-local DB sync that
-- inserted rows with explicit ids without bumping the sequence). The
-- worker's _append_log retries on a fresh connection so writes succeed,
-- but every drifted insert is a wasted round trip and the gap widens
-- over time. Realign on every migrate boot. Idempotent + cheap (one
-- MAX scan; the table has an index on (build_id, id) so MAX(id) is
-- fast). is_called=false makes nextval return MAX+1 unchanged, so a
-- concurrent insert during migration cannot collide.
-- SAFE: read-only sequence reset; no row mutation.
-- =====================================================================
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'build_logs_id_seq') THEN
    PERFORM setval(
      pg_get_serial_sequence('build_logs', 'id'),
      GREATEST(COALESCE((SELECT MAX(id) FROM build_logs), 0) + 1, 1),
      false
    );
  END IF;
END $$;

-- Newsletter / guide-landing signups. Captures emails left on /guide and any
-- future footer/landing newsletter form. UNIQUE(email) lets the route INSERT
-- with ON CONFLICT to be idempotent on duplicate submissions.
CREATE TABLE IF NOT EXISTS newsletter_subscribers (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email             TEXT NOT NULL,
  source            VARCHAR(40) DEFAULT 'footer',
  subscribed_at     TIMESTAMPTZ DEFAULT NOW(),
  unsubscribed_at   TIMESTAMPTZ,
  UNIQUE(email)
);

-- First-party funnel event log (2026-06-02). The site had ZERO web analytics
-- and most trial_keepers rows were bots, so every funnel number lied.
-- This is the source-of-truth event stream: server-side, no third-party
-- dependency, privacy-respecting (UA is hashed, never stored raw). Written
-- fire-and-forget by web/src/services/funnel-events.ts:emitEvent (never blocks
-- a request, never throws). Nothing in this repository reads it back; it was
-- meant for directional (NOT statistically-significant, at this traffic)
-- funnel deltas.
CREATE TABLE IF NOT EXISTS funnel_events (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name          VARCHAR(60) NOT NULL,        -- landing_view | cta_click_begin | table_view | first_message | build_dispatched | made_public
  trial_id      UUID,                        -- nullable: marketing-page events have no trial
  is_bot        BOOLEAN DEFAULT FALSE,       -- classified from UA at emit time
  ua_hash       VARCHAR(32),                 -- sha256(ua)[:16], never the raw UA
  path          VARCHAR(255),
  referrer      VARCHAR(500),
  props         JSONB,                       -- event-specific extras (e.g. {turn_count, build_id})
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_funnel_events_name_created ON funnel_events (name, created_at);
CREATE INDEX IF NOT EXISTS idx_funnel_events_human_created
  ON funnel_events (created_at) WHERE is_bot = FALSE;

-- Trial attribution + bot classification (2026-06-02). Stamped once at mint by
-- trial-cookie.ts so funnel queries can separate real humans from bots/preview
-- fetchers and attribute the entry channel. is_synthetic=TRUE means the mint
-- UA matched a bot/crawler/previewer signature (web/src/utils/bot-ua.ts).
-- Idempotent ALTERs: safe to re-run on every boot.
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN DEFAULT FALSE;
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS ua_hash VARCHAR(32);
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS entry_referrer VARCHAR(500);
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS entry_path VARCHAR(255);
ALTER TABLE trial_keepers ADD COLUMN IF NOT EXISTS entry_utm VARCHAR(255);
CREATE INDEX IF NOT EXISTS idx_trial_keepers_real_engaged
  ON trial_keepers (started_at) WHERE is_synthetic = FALSE;
