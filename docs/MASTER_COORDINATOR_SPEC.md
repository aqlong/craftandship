# Master Coordinator: Technical Specification

> Design specification written before implementation; `docs/internals/coordinator.md` describes what was built. The system is no longer developed and the site is still online.
>
> Note added for the public release: the `ux_ui` agent and the `intake_quality` task named below (workflow item 10, the scoring weight, the model table and the permission table) were never implemented; `coordinator/scorer.py` and `coordinator/actuators.py` have no such entries, and `AGENT_MODELS` and `AGENT_PERMISSIONS` there list a different set of agents (including `cost_analyst` and `reconciliation`). The escalation design was not built either: there are no one-tap SMS decisions or emailed decision links (`escalation_decisions`, `fallback_at` and `FOUNDER_PHONE` appear nowhere in the code), and a `fallback_option` never executes by itself. Escalations are `approval_requests` rows decided at `/admin/approvals`, and an unanswered request is rejected after 4 hours, never executed (`docs/internals/coordinator.md`). The actuator layer below is also simpler than the one built: in `coordinator/actuators.py` every gated method applies a permission check, then `COORDINATOR_ANALYSIS_ONLY`, then dry run; model calls (`authorize_model_call`) and ops playbooks (`run_playbook`) go through it too; the permission table has `model_call` and `build_annotate` and a non-agent principal, `failure_triage`; and the daily budget is zero when today's spend cannot be read from its one configured source. The `commit_files`, `redeploy_service`, `set_env_var` and `roadmap_add` methods below were built and later deleted (a commit, a redeploy and a variable write are reachable only through `run_playbook`, with their limits in `ops/fixer.py`), and so were the GitHub and Stripe fields of the world state. The grant table below (`AGENT_PERMISSIONS`, about line 468) is the design's, not the built one: the built table has eleven principals and nine permissions, 29 pairs (see `coordinator/actuators.py` and `docs/internals/coordinator.md`), with no `github_write` at all (nothing in `ops/` or `coordinator/` commits to a repository any more, so the permission and its GitHub client were deleted), no `email_customer`, an `approval_request` permission that gates the filing of an approval request, and no method or permission that approves or promotes a build (`approve_build` and `build_approve` were built and later deleted). The learning loop in the "Self-Learning System" section below was built and later deleted too. The text below is left as the design was written.

## Overview

The Master Coordinator is a persistent daemon that runs every 30 minutes, reads system state, decides which AI agent tasks to dispatch, enforces API rate/cost limits, and logs every decision. It replaces the current single-purpose ops agent (`ops/agent.py`) with a general-purpose orchestrator.

**Why 30 minutes?** Longer ticks mean more budget per tick ($0.42 vs $0.28 at 20-min), so the coordinator can dispatch 3-5 meaningful agent tasks per cycle instead of 1-2 shallow ones. P0 response time is unaffected because the existing health monitor thread (15-min interval) detects app-down events independently and triggers immediate escalation. The coordinator handles the fix; the monitor handles the alarm.

**Design constraints:**
- Shares a single Anthropic API key with the customer build pipeline (same RPM/TPM limits)
- Runs inside `worker_pg.py` as a daemon thread (same as current ops agent)
- Must never starve customer builds of API capacity
- Must be cost-effective: target <$20/day for all coordinator agent work combined
- Starts in dry-run mode; each agent independently toggleable

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    worker_pg.py                           │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Build Loop   │  │ Health Mon.  │  │ Backup Thread │  │
│  │ (polling)    │  │ (15m)        │  │ (daily)       │  │
│  └─────────────┘  └──────────────┘  └───────────────┘  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Master Coordinator (30m)             │   │
│  │                                                    │   │
│  │  WorldState ──► Scorer ──► Budget Gate ──► Dispatch│   │
│  │       │                                     │      │   │
│  │       │              ┌──────────────────────┘      │   │
│  │       │              ▼                              │   │
│  │       │     ┌─────────────────┐                    │   │
│  │       │     │  Agent Executor  │                    │   │
│  │       │     │  (sequential)    │                    │   │
│  │       │     └────────┬────────┘                    │   │
│  │       │              │                              │   │
│  │       │     ┌────────▼────────┐                    │   │
│  │       │     │  Actuator Layer  │◄── GitHub API     │   │
│  │       │     │  (side effects)  │◄── Railway API    │   │
│  │       │     │                  │◄── Resend API     │   │
│  │       │     │                  │◄── PostgreSQL     │   │
│  │       │     └────────┬────────┘                    │   │
│  │       │              │                              │   │
│  │       │     ┌────────▼────────┐                    │   │
│  │       │     │  Decision Log    │                    │   │
│  │       └────►│  + Learning      │                    │   │
│  │             └─────────────────┘                    │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### Key Design Decisions

**Sequential, not parallel dispatch.** Agents run one at a time within a tick. This eliminates RPM spike risk (Anthropic rate limits are requests-per-minute), simplifies error handling, and makes the decision log a clean linear sequence. A 20-minute tick has plenty of wall-clock time for 3-5 sequential agent calls.

**No `plan()` step.** Cost estimation is deterministic based on task category and model, not AI-generated. Calling Claude to estimate cost before calling Claude to do work is double-spend.

**Tiered review, not universal review.** Only code changes and security actions require peer review. Marketing drafts, docs updates, and monitoring scans ship without review. This cuts review API cost by ~70%.

**Customer builds always have priority.** The coordinator checks active build count before dispatching and yields its API slot if builds are running. Customer-facing latency is never degraded by background agent work.

---

## State the Coordinator Reads

### Tiered Refresh Strategy

Not all state needs to be fresh every tick. Expensive queries run on a schedule:

| State Domain | Refresh Interval | Source | Cost |
|---|---|---|---|
| Build pipeline | Every tick | PostgreSQL (3 queries) | ~5ms |
| Customer signals | Every tick | PostgreSQL (2 queries) | ~5ms |
| Infrastructure health | Every tick | PostgreSQL (1 query) + memory | ~2ms |
| Financial state | Every tick | PostgreSQL (1 query) | ~2ms |
| Agent performance | Every 3 ticks (1h) | PostgreSQL (1 query) | ~5ms |
| Code/deploy state | Every 6 ticks (2h) | GitHub API (1 call) | ~200ms |
| Stripe revenue | Daily (first tick after midnight) | Stripe API (1 call) | ~300ms |

### WorldState

```python
@dataclass
class WorldState:
    tick_id: str                          # UUID
    tick_number: int                      # monotonic counter since startup
    tick_start: datetime

    # Refreshed every tick (cheap DB queries)
    queued_builds: int
    active_builds: int                    # building/testing/deploying
    failed_builds_24h: list[dict]         # [{id, app_name, error_message, tier, retry_count}]
    preview_awaiting: int
    abandoned_intakes_24h: int
    pending_feedback: int                 # beta_feedback since last tick
    monitored_apps_down: list[dict]       # [{app_name, live_url, consecutive_failures}]
    monitored_apps_degraded: list[dict]
    api_cost_today_usd: float             # SUM(total_api_cost_usd) from builds + agent_tasks
    daily_budget_remaining_usd: float

    # Refreshed hourly (moderate queries)
    agent_success_rates: dict[str, float]     # 7-day rolling
    agent_avg_cost: dict[str, float]
    failure_kb_high_confidence_count: int
    disk_free_mb: float

    # Refreshed infrequently (external APIs)
    last_deploy_commit: str               # from worker_heartbeat
    open_github_issues: int               # 2h cache
    revenue_mtd_usd: float                # daily cache

    # Carried forward between ticks
    pending_work: list[WorkItem]          # persisted in agent_tasks table
    previous_tick_summary: dict
```

### Work Detection (What Creates WorkItems)

WorkItems are NOT created by scanning on every tick. They enter the queue through **event-driven triggers**:

```python
# Trigger sources (checked each tick with cheap DB queries):

1. NEW_FAILED_BUILD
   Query: builds WHERE status='failed' AND updated_at > last_tick
   Creates: WorkItem(category="build_failure", target="infra_engineer")

2. APP_DOWN
   Query: monitor_apps WHERE status='down' AND last_checked > last_tick
   Creates: WorkItem(category="app_down", target="infra_engineer")

3. NEW_FEEDBACK
   Query: beta_feedback WHERE created_at > last_tick
   Creates: WorkItem(category="customer_feedback", target="customer_service")

4. STALE_BACKUP
   Check: last_backup_age_s > 48h
   Creates: WorkItem(category="backup_overdue", target="infra_engineer")

5. BUDGET_ALERT
   Check: daily_budget_remaining_usd < 5.0
   Creates: WorkItem(category="budget_alert", target="financial")

6. NEW_LIVE_APP
   Query: builds WHERE status='live' AND updated_at > last_tick
   Creates: WorkItem(category="post_deploy_audit", target="security")

# Periodic triggers (not every tick):

7. DAILY_SECURITY_SCAN (first tick after 06:00 UTC)
   Creates: WorkItem(category="security_scan", target="security")

8. DAILY_COST_REPORT (first tick after 00:00 UTC)
   Creates: WorkItem(category="cost_report", target="financial")

9. WEEKLY_DOCS_SYNC (Monday, first tick after 08:00 UTC)
   Creates: WorkItem(category="docs_sync", target="documentation")

10. WEEKLY_INTAKE_REVIEW (Wednesday, first tick after 08:00 UTC)
    Creates: WorkItem(category="intake_quality", target="ux_ui")   # never implemented
```

### Deduplication

Work items are deduplicated by `(category, context_hash)`. If a failed build already has an open WorkItem, no duplicate is created. The context_hash is SHA-256 of the serialized context dict (sorted keys).

```python
def _should_create_work_item(category: str, context: dict, conn) -> bool:
    context_hash = hashlib.sha256(
        json.dumps(context, sort_keys=True).encode()
    ).hexdigest()[:16]

    existing = conn.execute("""
        SELECT id FROM agent_tasks
        WHERE task_category = %s AND context_hash = %s
          AND status NOT IN ('completed', 'failed', 'approved', 'rejected')
    """, (category, context_hash)).fetchone()

    return existing is None
```

---

## Priority Scoring

```python
def score_task(task: WorkItem, state: WorldState) -> float:
    """Returns 0.0 - 1.0. Deterministic, no API calls."""

    base = CATEGORY_WEIGHTS[task.category]
    urgency = _urgency_multiplier(task, state)
    cost_penalty = _cost_penalty(state)

    return min(1.0, base * urgency * cost_penalty)

CATEGORY_WEIGHTS = {
    # P0: Keep the lights on
    "app_down":           0.95,
    "build_stuck":        0.90,
    "security_critical":  0.90,
    "backup_overdue":     0.85,

    # P1: Customer impact
    "build_failure":      0.80,
    "deploy_failure":     0.75,
    "customer_feedback":  0.65,   # actionable signal

    # P2: Quality
    "post_deploy_audit":  0.55,
    "security_scan":      0.50,
    "cost_report":        0.40,

    # P3: Improvement (only when idle)
    "docs_sync":          0.30,
    "intake_quality":     0.25,   # never implemented
    "cost_optimization":  0.20,
}

def _urgency_multiplier(task, state) -> float:
    """1.0-2.0. Based on age and severity."""
    age_hours = (datetime.utcnow() - task.created_at).total_seconds() / 3600

    # Stale tasks get more urgent (prevents permanent deferral)
    age_boost = min(1.5, 1.0 + (age_hours / 24) * 0.5)

    # Down apps escalate with consecutive failures
    if task.category == "app_down":
        failures = task.context.get("consecutive_failures", 0)
        if failures > 10: return 2.0
        if failures > 5: return 1.5

    return age_boost

def _cost_penalty(state) -> float:
    """0.1-1.0. Throttle when budget is low."""
    remaining = state.daily_budget_remaining_usd
    daily = float(os.getenv("COORDINATOR_DAILY_BUDGET", "20.0"))
    pct = remaining / daily if daily > 0 else 0

    if pct > 0.5: return 1.0
    if pct > 0.2: return 0.6
    if pct > 0.05: return 0.3
    return 0.1  # emergency only
```

### Back-Pressure

Work items expire after 72 hours if never dispatched. The coordinator logs expiration as a learning event ("task category X is chronically under-resourced").

```python
def _expire_stale_work(conn):
    expired = conn.execute("""
        UPDATE agent_tasks SET status = 'expired'
        WHERE status = 'pending'
          AND created_at < NOW() - INTERVAL '72 hours'
        RETURNING task_category, COUNT(*)
    """).fetchall()
    for category, count in expired:
        _record_learning("coordinator", "capacity",
            f"Expired {count} '{category}' tasks; need more budget or fewer triggers")
```

---

## Rate Limit Pacing

### The Core Problem

CAS has ONE Anthropic API key shared between:
1. Customer builds (build.py, fix.py, review.py, qa.py); unpredictable, bursty
2. Support chat (services/claude.ts): low volume, latency-sensitive
3. Monitor diagnostics (diagnose.py): rare, high-priority
4. Coordinator agents: continuous background work

Anthropic rate limits (approximate, varies by tier):
- **RPM:** ~60 requests/minute (Opus), ~120 RPM (Sonnet), ~200 RPM (Haiku)
- **TPM:** ~400K tokens/minute (Opus), ~800K TPM (Sonnet)

### Pacing Strategy

**Rule 1: Customer builds always win.** If `active_builds > 0`, coordinator limits itself to 1 Haiku call per tick (monitoring/alerting only). No Opus or Sonnet.

**Rule 2: Per-tick budget is dynamic.**

```python
@dataclass
class TickBudget:
    max_api_calls: int
    max_cost_usd: float
    max_opus_calls: int
    allowed_models: list[str]   # what models this tick can use

def compute_tick_budget(state: WorldState) -> TickBudget:
    remaining = state.daily_budget_remaining_usd
    ticks_left = max(1, ((24 - datetime.utcnow().hour) * 2))  # ~2 ticks/hour
    per_tick = remaining / ticks_left

    # Rule 1: yield to customer builds
    if state.active_builds > 0:
        return TickBudget(
            max_api_calls=1,
            max_cost_usd=min(per_tick, 0.10),
            max_opus_calls=0,
            allowed_models=["claude-haiku-4-5"],
        )

    # Normal operation
    return TickBudget(
        max_api_calls=max(1, int(per_tick / 0.05)),  # ~$0.05 avg Haiku call
        max_cost_usd=per_tick,
        max_opus_calls=max(0, int(per_tick / 0.80)),  # ~$0.80 avg Opus call
        allowed_models=["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6"],
    )
```

**Rule 3: RPM spacing.** Between sequential agent calls, the coordinator sleeps for `max(2, 60 / rpm_limit)` seconds. This ensures even back-to-back calls stay under RPM.

```python
RPM_SPACING = {
    "claude-opus-4-6":   2.0,   # 60/60 = 1s, pad to 2s
    "claude-sonnet-4-6": 1.0,   # 60/120 = 0.5s, pad to 1s
    "claude-haiku-4-5":  0.5,   # 60/200 = 0.3s, pad to 0.5s
}
```

### Model Selection

Agents don't choose their model. The coordinator assigns based on agent type and budget:

```python
AGENT_MODELS = {
    # agent_type -> (default_model, upgrade_model, upgrade_condition)
    "infra_engineer":   ("claude-sonnet-4-6", "claude-opus-4-6",   "diagnostics"),
    "security":         ("claude-sonnet-4-6", "claude-opus-4-6",   "vuln_analysis"),
    "backend_engineer": ("claude-opus-4-6",   None,                 None),
    "qa_test":          ("claude-haiku-4-5",  "claude-sonnet-4-6", "analysis"),
    "financial":        ("claude-haiku-4-5",  None,                 None),
    "customer_service": ("claude-haiku-4-5",  None,                 None),
    "documentation":    ("claude-haiku-4-5",  None,                 None),
    "ux_ui":            ("claude-sonnet-4-6", None,                 None),   # never implemented
    "architect":        ("claude-sonnet-4-6", "claude-opus-4-6",   "deep_review"),
}

def select_model(agent_type: str, task: WorkItem, budget: TickBudget) -> str:
    default, upgrade, condition = AGENT_MODELS[agent_type]

    if default not in budget.allowed_models:
        # Downgrade to cheapest allowed
        for fallback in ["claude-haiku-4-5", "claude-sonnet-4-6"]:
            if fallback in budget.allowed_models:
                return fallback
        return None  # cannot run this agent this tick

    # Check upgrade
    if upgrade and condition and task.context.get("requires") == condition:
        if upgrade in budget.allowed_models and budget.max_opus_calls > 0:
            return upgrade

    return default
```

---

## Agent Interface

```python
class Agent(ABC):
    name: str
    description: str
    requires_review: bool = False   # only code/security changes

    @abstractmethod
    def execute(self, task: WorkItem, state: WorldState, model: str,
                api_client: ApiClient, actuators: Actuators) -> AgentResult:
        """Do the work. Single method, no plan() step."""
        ...

@dataclass
class AgentResult:
    success: bool
    output: dict                    # agent-specific structured output
    actions_taken: list[Action]     # what actuators were called
    api_calls: int
    cost_usd: float
    duration_s: float
    learnings: list[str]            # fed to agent_learnings table
    follow_up_work: list[WorkItem]  # new items for the queue
    escalate: bool = False          # needs human attention
    escalation_reason: str = ""
```

### Actuator Layer

Agents don't have direct access to external systems. They request actions through a controlled actuator layer that enforces permissions and logging:

```python
class Actuators:
    """Controlled side-effect layer. Every action is logged."""

    def __init__(self, agent_name: str, task_id: str, dry_run: bool):
        self.agent = agent_name
        self.task_id = task_id
        self.dry_run = dry_run
        self.actions: list[Action] = []

    # --- GitHub ---
    def commit_files(self, changes: dict[str, str], message: str) -> str | None:
        """Commit file changes via GitHub Git Data API. Returns commit SHA."""
        self._check_permission("github_write")
        if self.dry_run:
            self._log("DRY_RUN: would commit", changes=list(changes.keys()))
            return None
        sha = ops_fixer.commit_file_changes(changes, message, dry_run=False)
        self._record(Action("github_commit", {"files": list(changes.keys()), "sha": sha}))
        return sha

    # --- Railway ---
    def redeploy_service(self, service_id: str, env_id: str) -> bool:
        self._check_permission("railway_deploy")
        ...

    def set_env_var(self, project_id: str, name: str, value: str) -> bool:
        self._check_permission("railway_env")
        ...

    # --- Database ---
    def query_readonly(self, sql: str, params: tuple) -> list[dict]:
        """Read-only DB access. No mutations allowed through this interface."""
        self._record(Action("db_query", {"sql": sql[:200]}))
        ...

    # --- Email ---
    def send_alert(self, subject: str, body: str) -> bool:
        self._check_permission("email_send")
        ...

    def send_customer_email(self, to: str, template: str, **kwargs) -> bool:
        """Customer-facing email. Always requires review approval first."""
        self._check_permission("email_customer")
        ...

    # --- Failure KB ---
    def record_fix(self, error_msg: str, fix_desc: str, files: list, category: str, success: bool):
        failure_kb.record_fix(error_msg, fix_desc, files, category, success)
        self._record(Action("kb_record", {"category": category, "success": success}))

    def _check_permission(self, action_type: str):
        allowed = AGENT_PERMISSIONS.get(self.agent, set())
        if action_type not in allowed:
            raise PermissionError(f"Agent '{self.agent}' not permitted: {action_type}")

    def _record(self, action: Action):
        self.actions.append(action)

    def _log(self, msg, **kwargs):
        logger.info(f"[{self.agent}:{self.task_id[:8]}] {msg}", extra=kwargs)


AGENT_PERMISSIONS = {
    "infra_engineer":   {"github_write", "railway_deploy", "railway_env", "email_send", "kb_record"},
    "security":         {"github_write", "email_send"},               # can commit fixes
    "backend_engineer": {"github_write"},                             # code only
    "qa_test":          set(),                                        # read-only + API calls
    "financial":        {"email_send"},                               # alert only
    "customer_service": {"email_customer", "kb_record"},              # KB + customer comms
    "documentation":    {"github_write"},                             # docs commits
    "ux_ui":            set(),                                        # analysis only (agent never implemented)
    "architect":        set(),                                        # analysis only
}
```

### Tiered Review

Only certain actions require peer review before the actuator executes:

```python
ACTIONS_REQUIRING_REVIEW = {
    "github_commit",     # any code/docs commit
    "railway_deploy",    # any redeploy
    "email_customer",    # customer-facing email
}

# Actions that ship immediately (no review):
# - email_send (internal alerts)
# - railway_env (env var changes)
# - db_query (read-only)
# - kb_record (learning)
```

When an agent's result includes a reviewable action, the coordinator:
1. Stores the result with `status='reviewing'`
2. On the SAME tick (if budget allows) or NEXT tick, dispatches the reviewer agent
3. Reviewer sees the proposed actions + context, returns approve/reject
4. If approved, coordinator executes the actuator calls
5. If rejected, task goes back to author with feedback (max 1 revision)

**Reviewer assignment:**

| Author | Reviewer | Rationale |
|---|---|---|
| backend_engineer | qa_test | Code changes need test perspective |
| infra_engineer | security | Infra changes need security check |
| security | architect | Security fixes need architecture check |
| documentation | (none) | Docs ship without review |
| customer_service | (none) | KB updates ship without review |

---

## Agent Roster

### Phase 1 Agents (implement first)

**Infra Engineer**: Wraps existing `ops/agent.py`
- Triggers: build failures, app down, stuck builds, backup overdue
- Actuators: github_write, railway_deploy, railway_env, email_send, kb_record
- Model: Sonnet (Opus for diagnostics)
- Implementation: Adapter around existing `scanner.scan_recent_failures()` + `playbook.match_playbook()` + fixer modules

**Financial Manager**: Cost tracking and alerts
- Triggers: budget thresholds, daily reconciliation, cost anomalies
- Actuators: email_send
- Model: Haiku
- Implementation: Queries `builds.total_api_cost_usd` + `agent_tasks.actual_cost_usd`, computes margins, sends alerts when daily spend exceeds thresholds. No Claude API call needed for basic reporting; uses Claude only for anomaly analysis.
- **Key insight:** Most financial work is pure SQL aggregation. Claude is only called when the numbers look anomalous and need interpretation.

**Customer Service**: Support KB and feedback triage
- Triggers: new beta feedback, support KB gaps
- Actuators: email_customer, kb_record
- Model: Haiku
- Implementation: Reads `beta_feedback` and `support_kb` tables. For feedback: classifies severity, drafts response. For KB gaps: generates new Q&A entries from recurring questions.

**Documentation Engineer**: Keeps CLAUDE.md current
- Triggers: weekly sync, post-deploy
- Actuators: github_write
- Model: Haiku
- Implementation: Diffs current CLAUDE.md against actual code (file structure, test counts, schema). Generates targeted patches. Cheap because it's mostly string comparison with Haiku for prose generation.

### Phase 2 Agents

**Security Engineer**: Vulnerability scanning
- Triggers: new app deploy, daily scan
- Actuators: github_write, email_send
- Model: Sonnet
- Implementation: Runs `npm audit --json` on generated apps, parses output, classifies severity. For critical CVEs, drafts dependency update PR. Extends existing `npm audit` in test.py.

**QA/Test Engineer**: Regression detection
- Triggers: post-deploy, CI failures
- Actuators: (read-only)
- Model: Haiku (Sonnet for analysis)
- Implementation: Wraps existing `qa.py`. Adds regression detection: compares current test results against historical baselines per app template. Flags when pass rate drops.

### Phase 3 Agents

**Backend Engineer**: Code fixes and features
- Triggers: approved specs from other agents, bug reports
- Actuators: github_write
- Model: Opus
- Implementation: Most expensive agent. Only dispatched when specific, reviewed work items exist. Uses same patterns as `fix.py` and `deploy_fix.py`.

**Architect**: Design review
- Triggers: on-demand from other agents
- Actuators: (read-only)
- Model: Sonnet (Opus for deep review)
- Implementation: Reviews proposed schema changes, evaluates scalability implications, audits generated app architecture.

### Phase 4 Agents (when budget allows)

**UX/UI Expert, Product Manager, Marketing, CEO, API Expert**

These agents are lower priority because their outputs typically require human judgment before shipping. They're useful for generating drafts and analyses but don't close the autonomy loop. Implement only after Phases 1-3 prove the coordinator framework works.

---

## Tick Lifecycle

```
TICK START
│
├─ 1. GUARD CHECK (instant)
│    If _shutdown_event.is_set(): return
│    If last_tick completed < 5 min ago: return (overlap protection)
│
├─ 2. SNAPSHOT (5-50ms)
│    Build WorldState (tiered refresh)
│    Expire stale work items (> 72h)
│
├─ 3. DETECT WORK (5-30ms)
│    Run event triggers against DB
│    Deduplicate against existing queue
│    Score all pending items
│
├─ 4. BUDGET GATE (instant)
│    Compute TickBudget
│    If max_cost_usd < $0.01 and no P0 tasks: LOG "budget exhausted" → END
│    If active_builds > 0: restrict to Haiku-only
│
├─ 5. DISPATCH LOOP (bulk of tick time)
│    Sort tasks by score descending
│    For each task (sequential, not parallel):
│      a. Select model (respect budget.allowed_models)
│      b. If model is None: skip (log "model unavailable")
│      c. If estimated_cost > budget remaining: skip
│      d. Create Actuators instance (permissions, dry_run)
│      e. Inject learnings from agent_learnings + failure_kb
│      f. Execute agent
│      g. Sleep RPM_SPACING[model] seconds
│      h. If result has reviewable actions: queue review
│      i. If result has non-reviewable actions: execute immediately
│      j. Deduct cost from budget
│      k. If budget exhausted: break
│
├─ 6. PROCESS REVIEWS (if budget remains)
│    Fetch pending reviews from previous ticks
│    For each (sequential):
│      a. Dispatch reviewer agent
│      b. If approved: execute deferred actuator actions
│      c. If rejected: return to author queue with feedback
│
├─ 7. LEARN + LOG (5-10ms)
│    Record learnings from all completed tasks
│    Update agent_performance metrics
│    Write tick summary to coordinator_ticks
│    Detect regressions: any build that went live then got
│      customer complaint / rebuild request within 48h
│
TICK END
```

### Regression Detection (Concrete Mechanism)

A regression is detected when:
```sql
-- A build that went live, then within 48h had a rebuild_request or failed feature request
SELECT b.id, b.app_name, rb.rebuild_request
FROM builds b
JOIN builds rb ON rb.parent_build_id = b.id
WHERE b.status = 'live'
  AND rb.created_at < b.completed_at + INTERVAL '48 hours'
  AND rb.rebuild_request IS NOT NULL
```

When detected, the coordinator:
1. Creates a learning: "App {name} required rebuild within 48h; original review by {reviewer} missed: {rebuild_request}"
2. Updates pair regression stats
3. If pair regression rate > 30% over 10+ samples, logs a recommendation to swap reviewers

---

## Decision Log

### Database Schema

```sql
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

CREATE INDEX idx_coordinator_ticks_started ON coordinator_ticks(started_at DESC);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tick_id UUID REFERENCES coordinator_ticks(id),
    agent VARCHAR(50) NOT NULL,
    model VARCHAR(50),
    task_category VARCHAR(50) NOT NULL,
    task_description TEXT,
    context JSONB DEFAULT '{}',
    context_hash VARCHAR(16),              -- for deduplication
    priority_score FLOAT,
    estimated_cost_usd NUMERIC(10,4),
    actual_cost_usd NUMERIC(10,4),
    status VARCHAR(20) DEFAULT 'pending',
    result JSONB,
    actions_taken JSONB DEFAULT '[]',
    review_agent VARCHAR(50),
    review_status VARCHAR(20),             -- pending, approved, rejected
    review_result JSONB,
    learnings JSONB DEFAULT '[]',
    escalated BOOLEAN DEFAULT FALSE,
    escalation_reason TEXT,
    source_agent VARCHAR(50),              -- who created this work item
    parent_task_id UUID REFERENCES agent_tasks(id),  -- for review/revision chains
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ,
    duration_s FLOAT
);

CREATE INDEX idx_agent_tasks_status ON agent_tasks(status) WHERE status IN ('pending', 'running', 'reviewing');
CREATE INDEX idx_agent_tasks_category_hash ON agent_tasks(task_category, context_hash)
    WHERE status NOT IN ('completed', 'failed', 'approved', 'rejected', 'expired');
CREATE INDEX idx_agent_tasks_tick ON agent_tasks(tick_id);

CREATE TABLE IF NOT EXISTS agent_learnings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_agent VARCHAR(50) NOT NULL,
    category VARCHAR(50) NOT NULL,
    learning TEXT NOT NULL,
    learning_hash VARCHAR(16) NOT NULL,    -- prevents duplicates
    confidence FLOAT DEFAULT 0.5,
    times_applied INTEGER DEFAULT 0,
    times_successful INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_applied_at TIMESTAMPTZ,
    UNIQUE(source_agent, category, learning_hash)
);

CREATE TABLE IF NOT EXISTS agent_performance (
    agent VARCHAR(50) NOT NULL,
    metric VARCHAR(50) NOT NULL,
    window VARCHAR(20) NOT NULL,
    value FLOAT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent, metric, window)
);
```

### Decision Record

Each entry in `coordinator_ticks.decisions`:

```json
{
  "task_id": "uuid",
  "decision": "dispatch|skip|defer|expire|escalate",
  "reason": "Budget allows; highest-priority pending item",
  "agent": "infra_engineer",
  "model": "claude-sonnet-4-6",
  "score": 0.85,
  "estimated_cost_usd": 0.12,
  "budget_after": {"remaining_usd": 1.45, "remaining_calls": 6}
}
```

Skipped tasks always include a reason:
```json
{
  "task_id": "uuid",
  "decision": "skip",
  "reason": "Model claude-opus-4-6 not in allowed_models (active builds running)",
  "agent": "backend_engineer",
  "score": 0.75
}
```

---

## Self-Learning System

### How Learnings Are Created

Agents return `learnings: list[str]` in their result. The coordinator:

1. Hashes each learning (SHA-256 of `agent + category + text`)
2. Inserts with `ON CONFLICT (source_agent, category, learning_hash) DO UPDATE SET last_applied_at = NOW()`
3. No duplicates ever created

### How Learnings Are Used

Before dispatching an agent, the coordinator queries relevant learnings:

```python
def _build_learning_context(agent: str, task: WorkItem) -> str:
    """Returns a prompt section with high-confidence learnings. No API call."""
    rows = conn.execute("""
        SELECT learning, confidence FROM agent_learnings
        WHERE (source_agent = %s OR confidence >= 0.8)
          AND category = %s
          AND confidence >= 0.6
        ORDER BY confidence DESC, last_applied_at DESC
        LIMIT 5
    """, (agent, task.category)).fetchall()

    if not rows:
        return ""

    lines = [f"- [{r['confidence']:.0%}] {r['learning']}" for r in rows]
    return "## Lessons Learned\n" + "\n".join(lines)
```

### Confidence Updates

After a task completes, the coordinator checks if injected learnings were relevant:

```python
def _update_learning_confidence(task_result: AgentResult, injected_learning_ids: list[str]):
    for lid in injected_learning_ids:
        was_useful = task_result.success  # simple heuristic: success = useful
        conn.execute("""
            UPDATE agent_learnings
            SET times_applied = times_applied + 1,
                times_successful = times_successful + CASE WHEN %s THEN 1 ELSE 0 END,
                confidence = (times_successful + 1.0) / (times_applied + 2.0),
                last_applied_at = NOW()
            WHERE id = %s
        """, (was_useful, lid))
```

Learnings with `times_applied >= 10 AND confidence < 0.3` are pruned weekly.

---

## Escalation to Human

Escalation triggers:
1. **P0 task + budget exhausted**: can't fix a down app
2. **Task failed 3 times**: agent can't solve it
3. **Agent explicitly requests**: `escalate=True` in result
4. **Security critical**: any CVE classified as critical severity
5. **Novel failure**, not in failure_kb, no similar learnings, agent confidence < 0.5

### One-Tap Decision Interface (Phase 2 Feature)

**Goal:** Founder responds to escalations from their phone without opening a laptop. Every escalation is a yes/no or multiple-choice question with a single tap to resolve.

**Channel: SMS (primary) + Email (backup).** SMS wins for urgency because it doesn't get buried in an inbox, works without internet, and has near-100% open rates. Email carries the full context as backup. Both link to the same decision endpoint.

**Flow:**

```
Agent escalates
    ↓
Coordinator formats decision as structured choices:
    Option A: "Restart the service (recommended)"
    Option B: "Wait 2 hours and retry"
    Option C: "Escalate to manual fix"
    ↓
SMS sent via Twilio:
    "[CAS P1] pet-grooming-scheduler is down (DB timeout).
     Tried: restart, env check.
     Reply 1: Restart service (recommended)
     Reply 2: Wait 2h and retry
     Reply 3: I'll handle it manually"
    ↓
Email sent via Resend (same choices as clickable links):
    Each link → GET /ops/api/v1/decisions/{token}/choose?option=1
    ↓
Founder replies "1" via SMS or clicks link in email
    ↓
Webhook receives response → coordinator executes chosen action
    ↓
Confirmation SMS: "✓ Restarting pet-grooming-scheduler. Will update in 5 min."
    ↓
Follow-up SMS: "✓ pet-grooming-scheduler is back up. Latency 340ms."
```

**Decision Token Schema:**

```sql
CREATE TABLE IF NOT EXISTS escalation_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token VARCHAR(64) UNIQUE NOT NULL,        -- SHA-256 hashed, URL-safe
    task_id UUID REFERENCES agent_tasks(id),
    priority VARCHAR(4) NOT NULL,             -- P0, P1, P2
    summary TEXT NOT NULL,
    options JSONB NOT NULL,                   -- [{label, action_type, action_params, recommended}]
    chosen_option INTEGER,                    -- 1-indexed, NULL until responded
    responded_at TIMESTAMPTZ,
    response_channel VARCHAR(10),             -- 'sms' or 'email'
    fallback_option INTEGER NOT NULL,         -- auto-executes after fallback_at
    fallback_at TIMESTAMPTZ NOT NULL,         -- default: created_at + 4h
    executed_at TIMESTAMPTZ,                  -- when the chosen action ran
    execution_result JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expired BOOLEAN DEFAULT FALSE
);
```

**Security:**
- Decision tokens are single-use, 64-char random hex, hashed before storage (same pattern as password reset tokens)
- Token expires after `fallback_at` (default 4 hours)
- Email links use HTTPS: `https://<web-host>/ops/api/v1/decisions/{raw_token}/choose?option=1`
- Link lands on a confirmation page ("You chose: Restart service. Confirm?") before executing, prevents accidental taps and email link prefetchers from triggering actions
- SMS responses validated against expected option numbers; anything else gets "Reply 1, 2, or 3"

**SMS via Twilio:**
- Twilio Programmable SMS ($0.0079/msg outbound, $0.0075/msg inbound)
- Webhook endpoint: `POST /webhooks/twilio` (validates Twilio signature)
- Founder phone number stored in `FOUNDER_PHONE` env var
- Fallback: if Twilio unavailable, email-only (degraded but functional)

**Implementation (add after Phase 1 coordinator is stable):**
1. `escalation_decisions` table migration
2. coordinator/decisions.py (planned, not implemented): create decision, format choices, process responses
3. `GET /ops/api/v1/decisions/:token/choose`, email click handler with confirmation page
4. `views/decision-confirm.ejs`: simple confirmation page (mobile-friendly, one big button)
5. Ship email-only decision links. Run for 1-2 weeks, verify the flow works.
6. **Gate: get explicit operator approval before the Twilio integration** (cost estimate, phone number options, account setup steps, demo of the SMS flow).
7. Twilio account setup + phone number purchase
8. `POST /webhooks/twilio`: receive SMS replies (signature validation)
9. Wire SMS sending into `decisions.py`, set `ESCALATION_CHANNEL=both`

**Env vars:**
```python
TWILIO_ACCOUNT_SID    = env("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = env("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER   = env("TWILIO_PHONE_NUMBER", "")    # CAS sender number
FOUNDER_PHONE         = env("FOUNDER_PHONE", "")           # operator's mobile
ESCALATION_CHANNEL    = env("ESCALATION_CHANNEL", "email") # "sms", "email", or "both"
```

### Escalation Format (Current: Email Only)

Escalation email:
```
Subject: [CAS] {priority} - {one-line summary}
Body:
  What happened: {context}
  What was tried: {actions taken, including failures}
  Recommended: {agent's suggestion}
  Fallback (4h): {what happens if no response}

  [Option 1: {recommended action}] ← clickable link
  [Option 2: {alternative}]        ← clickable link
  [Option 3: I'll handle it]       ← clickable link
```

Even before SMS is implemented, the email version uses clickable decision links instead of requiring a Claude Code session to respond.

Cooldown: 4 hours per (category, context_hash). No duplicate escalations.

---

## Configuration

```python
COORDINATOR_ENABLED       = env_bool("COORDINATOR_ENABLED", False)
COORDINATOR_DRY_RUN       = env_bool("COORDINATOR_DRY_RUN", True)
COORDINATOR_INTERVAL_S    = env_int("COORDINATOR_INTERVAL_S", 1800)      # 30 min
COORDINATOR_DAILY_BUDGET  = env_float("COORDINATOR_DAILY_BUDGET", 20.0)  # USD
COORDINATOR_MONTHLY_BUDGET = env_float("COORDINATOR_MONTHLY_BUDGET", 300.0)

# Per-agent toggles (all default to dry_run's value)
AGENT_INFRA_ENABLED       = env_bool("AGENT_INFRA_ENABLED", None)  # None = inherit
AGENT_FINANCIAL_ENABLED   = env_bool("AGENT_FINANCIAL_ENABLED", None)
AGENT_CUSTOMER_ENABLED    = env_bool("AGENT_CUSTOMER_ENABLED", None)
AGENT_DOCS_ENABLED        = env_bool("AGENT_DOCS_ENABLED", None)
AGENT_SECURITY_ENABLED    = env_bool("AGENT_SECURITY_ENABLED", None)
AGENT_QA_ENABLED          = env_bool("AGENT_QA_ENABLED", None)
AGENT_BACKEND_ENABLED     = env_bool("AGENT_BACKEND_ENABLED", None)
AGENT_ARCHITECT_ENABLED   = env_bool("AGENT_ARCHITECT_ENABLED", None)
```

### Startup

```
worker_pg.py starts
  → IF COORDINATOR_ENABLED:
      Spawn _run_coordinator_daemon() thread
      (replaces _run_ops_agent_daemon)
    ELSE:
      Spawn legacy ops agent (backward compatible)
```

---

## Cost Model

Per-agent cost is bounded by the budget gate (`coordinator/budget.py`); the cheap agents run on Haiku or Sonnet and the Backend Engineer (Opus) is added only in Phase 3. Customer builds are tracked separately against the build-specific cost column, not the coordinator budget.

---

## Migration Path

### Phase 1: Foundation + Infra Agent (Week 1)
- Schema migrations (4 new tables)
- Coordinator daemon with tick lifecycle
- WorldState snapshot (DB queries only)
- TickBudget computation
- Actuator layer with permissions
- Decision logging
- Infra Engineer agent (adapter around existing ops agent)
- All dry-run by default

### Phase 2: Low-Cost Agents (Week 2)
- Financial Manager (mostly SQL, minimal Claude)
- Customer Service (Haiku, feedback triage)
- Documentation Engineer (Haiku, weekly sync)

### Phase 3: High-Value Agents (Week 3-4)
- Security Engineer (Sonnet, extends npm audit)
- QA/Test Engineer (Haiku, wraps qa.py)
- Review system goes live (reviewable actions enforced)

### Phase 4: Code Generation (When Proven)
- Backend Engineer (Opus, expensive)
- Architect (Sonnet, review-only)
- Only after coordinator has proven stable for 2+ weeks

### Phase 5: Growth Agents (Future)
- CEO, Product Manager, Marketing, UX/UI, API Expert
- Only if the additional agent cost is justified
