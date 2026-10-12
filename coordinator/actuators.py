# coordinator/actuators.py: Permission-gated side-effect layer
#
# Every side effect a coordinator agent (or the failure triage step in work
# detection) can cause is a method on Actuators: ops playbooks (the only path
# to a redeploy or a Railway variable), build rows, knowledge-base records,
# emails and approval requests. A model call is made by the caller, but only
# after authorize_model_call here has allowed it, one call at a time, which
# besides the checks below requires the tick's model-call allowance to cover
# the call (coordinator/budget.py, ModelCallMeter). Each gated method applies
# the same three checks, in order:
#
#   1. Permission: the caller's entry in AGENT_PERMISSIONS must hold the
#      method's permission, else PermissionError. (run_playbook is the one
#      method that downgrades to a dry run instead of raising.)
#   2. Analysis-only: with COORDINATOR_ANALYSIS_ONLY=true the permission must
#      also be in _ANALYSIS_ONLY_PERMISSIONS. The mode narrows, never widens.
#   3. Dry run (the default): the method records what it would have done and
#      writes, sends and spends nothing. One method is exempt because its only
#      effect is an entry in a queue a person reviews: finding_add (the
#      coordinator_findings table).
# create_approval_request is one of them: its permission is approval_request,
# which is not an analysis-only permission, so analysis-only mode refuses it.
#
# Who holds an Actuators. An agent does not build one: the tick builds one
# per dispatched task, with the tick's dry-run flag and model-call meter, and
# hands it to the agent; work detection builds the one for failure triage.
# Actuators.__init__ refuses a call that comes from any other module of the
# coordinator package (_ISSUING_MODULES), so an agent cannot give itself an
# instance with dry_run=False or under another agent's name, and
# tests/test_coordinator_actuator_gates.py scans the package for a
# construction, a subclass or any use of the class as a value outside those
# modules. Both are checks on first-party code that is expected to be honest
# about what it calls; neither is a sandbox for code that sets out to evade
# them (it runs in the same process).
#
# What is NOT behind this layer: reads (query_readonly, the world-state
# snapshot, the Railway zombie probe, the cost report, health probes of
# deployed apps) and the tick loop's own bookkeeping in coordinator/tick.py
# (task rows, the tick record, learnings, expiring approval requests and the
# reminder email about one).
# tests/test_coordinator_actuator_gates.py holds the permission and dry-run
# matrix and an import scan: outside this file a coordinator module may import
# only an allowlist of standard-library modules and other coordinator modules,
# so a network, subprocess, database or model library (or first-party code
# outside coordinator/) fails that test unless it is listed there with a
# reason. The scan does not prove that a module writes nothing through a
# database connection it was handed; for that it only has tripwires.
#
# Railway is written to only through an ops playbook (Actuators.run_playbook),
# and the playbooks call ops/fixer.py. Which services may be redeployed and
# which variables may be set is enforced there, in the functions that make
# the call (SETTABLE_ENV_VARS and the platform-service check in
# ops/fixer.py), so the same limits hold when the legacy ops agent runs a
# playbook without this layer. No coordinator or ops code commits to a
# repository or reads a GitHub token.

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from urllib.error import URLError
from urllib.request import Request

import psycopg2

from coordinator import config
from coordinator.envflags import safety_switch
from coordinator.sql_statements import UnterminatedSql, count_statements
from monitor.bounded_fetch import bounded_urlopen

logger = logging.getLogger("coordinator.actuators")


def _require_one_statement(sql) -> None:
    """Raise PermissionError unless `sql` (str or bytes) holds exactly one statement."""
    try:
        statements = count_statements(sql)
    except UnterminatedSql as exc:
        raise PermissionError(f"Actuator query_readonly cannot tell where the statement ends: {exc}") from None
    if statements != 1:
        raise PermissionError(
            f"Actuator query_readonly runs exactly one statement; the text holds {statements}")

# The coordinator has no way to approve or promote a build: no method here
# writes a review verdict or the live status, and no permission names one.
#
# An approval request (create_approval_request) is advisory: nothing in this
# repository executes an action when a request is marked approved. Two things
# file one: the tick, when an agent's result asks for escalation
# (coordinator/tick.py:_check_escalation, under that agent's name and so
# under its approval_request permission), and the reconciliation agent, for a
# build it thinks should be put back to preview. A person reads the request at
# /admin/approvals and acts on it themselves. A request nobody resolves is
# denied after a timeout (coordinator/tick.py:_expire_stale_approval_requests),
# never approved.

# The modules that may construct an Actuators (see the header and __init__).
_ISSUING_MODULES = frozenset({"coordinator.tick", "coordinator.work_detection"})

# Max rebuilds the coordinator may queue for the same parent build within 24h,
# and a refusal when one is already in flight. Without this, a build that keeps
# failing is retried every tick for as long as it stays in the 24h failed list.
MAX_REBUILDS_PER_BUILD_24H = config.MAX_REBUILDS_PER_BUILD_24H

# The cap above counts the children of one parent, and a rebuild that fails is
# itself a parent on the next tick, so on its own it bounds nothing. The
# lineage bound does: at most this many automatic rebuilds per root ancestor
# (the top of the parent_build_id chain) in a rolling LINEAGE_WINDOW_DAYS. It
# is counted from build_start_events, the ledger the web app keeps for build
# starts, where queue_rebuild records each rebuild under the root's id: that
# table has no foreign key to builds and nothing prunes it, so the count
# survives the worker's janitor deleting failed rows of the lineage.
MAX_REBUILDS_PER_LINEAGE = config.MAX_REBUILDS_PER_LINEAGE
LINEAGE_WINDOW_DAYS = 7
_LINEAGE_MAX_DEPTH = 100

# The web app's limits on build starts per customer
# (web/src/services/build-start-limits.ts: MAX_ACTIVE_BUILDS_PER_CUSTOMER and
# MAX_BUILD_STARTS_PER_DAY). An automatic rebuild is a build start for that
# customer: it is refused at the same limits and written to the same ledger,
# under the same per-customer advisory lock.
# tests/test_coordinator_rebuild_realdb.py checks the numbers and the lock
# against that file and runs its budget statement on rows written here.
CUSTOMER_MAX_ACTIVE_BUILDS = 2
CUSTOMER_MAX_BUILD_STARTS_PER_DAY = 10

# The lock key and the two counts are the web app's (BUILD_START_LOCK_SQL and
# readBuildStartBudget in web/src/services/build-start-limits.ts).
_CUSTOMER_BUILD_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('cas-build-start'), hashtext(%s))"
_CUSTOMER_BUILD_BUDGET_SQL = """
    SELECT
      (SELECT COUNT(*) FROM builds
        WHERE customer_id = %s
          AND status IN ('queued', 'building', 'testing', 'deploying')),
      (SELECT COUNT(*) FROM build_start_events
        WHERE customer_id = %s
          AND created_at > NOW() - INTERVAL '24 hours')
"""

# The topmost ancestor reachable from a build through parent_build_id.
_LINEAGE_ROOT_SQL = """
    WITH RECURSIVE up(id, parent_build_id, depth) AS (
        SELECT id, parent_build_id, 0 FROM builds WHERE id = %s
        UNION ALL
        SELECT b.id, b.parent_build_id, up.depth + 1
          FROM builds b JOIN up ON b.id = up.parent_build_id
         WHERE up.depth < %s
    )
    SELECT id FROM up ORDER BY depth DESC LIMIT 1
"""


def _admin_approvals_url() -> str:
    """Absolute URL of the admin approvals page, from BASE_URL, or "" if unset."""
    base = os.environ.get("BASE_URL", "").strip().rstrip("/")
    return f"{base}/admin/approvals" if base else ""


@dataclass
class Action:
    action_type: str
    details: dict = field(default_factory=dict)


# Each principal holds exactly the permissions its reachable handlers ask for.
# tests/test_coordinator_permission_use.py runs a real handler for every
# (principal, permission) pair in this table, and fails for a grant no handler
# asks for and for an actuator call whose permission the caller does not hold.
AGENT_PERMISSIONS = {
    # The only agent that runs an ops playbook, which is what the two
    # playbook permissions (railway_deploy, railway_env) gate.
    "infra_engineer":   {"railway_deploy", "railway_env", "email_send", "kb_record", "model_call", "finding_write", "approval_request"},
    "security":         {"email_send", "finding_write", "approval_request"},
    "backend_engineer": {"build_queue", "email_send", "finding_write"},
    # Read-only: queries, and a report in its task result.
    "qa_test":          set(),
    "financial":        {"email_send", "approval_request"},
    "customer_service": {"email_send", "kb_record", "model_call", "finding_write", "approval_request"},
    "documentation":    {"email_send", "finding_write"},
    "cost_analyst":     {"email_send"},
    # Sends nothing itself; the findings of its initiative review are filed
    # by the tick under its name (coordinator/tick.py:_surface_initiative_findings).
    "architect":        {"finding_write"},
    # Reconciliation agent: read-only DB query and Railway probe, then an
    # approval request and one alert email. It holds no permission that writes
    # a build row.
    "reconciliation":   {"email_send", "approval_request"},
    # Not an agent: the failure triage step in coordinator/work_detection.py,
    # which asks a cheap-tier model to categorize a failed build that the regex
    # classifier could not, caches the answer on the build row and records it
    # in the failure KB.
    "failure_triage":   {"model_call", "build_annotate", "kb_record"},
}

# Entries of AGENT_PERMISSIONS that are steps of the coordinator itself, not
# registered agents (they are never dispatched and hold no reporting grants).
NON_AGENT_PRINCIPALS = frozenset({"failure_triage"})

# When COORDINATOR_ANALYSIS_ONLY=true, an action must be in this set AND in the
# agent's own AGENT_PERMISSIONS entry (the mode narrows, it never widens). Write
# actions (railway_deploy, build_queue, build_annotate, approval_request, etc.)
# are blocked; agents may only call a model, record findings to the KB and the
# DB findings table, and send alert emails. An escalation in this mode is not
# filed as an approval request: the agent's result still carries it on the
# task row, and its findings and alert email go out as usual.
_ANALYSIS_ONLY_PERMISSIONS: frozenset[str] = frozenset({
    "kb_record",
    "model_call",
    "finding_write",
    "email_send",
})

# The permission an ops playbook needs before Actuators.run_playbook lets it
# run live. The fixers in ops/playbooks/ reach Railway through ops/fixer.py on
# their own, so run_playbook is the only coordinator gate in front of them
# (ops/fixer.py applies its own limits to every caller). One entry per
# playbook in ops.playbook.PLAYBOOKS; a category without an entry is not run
# at all. These two permissions gate nothing else: run_playbook is their only
# reader.
PLAYBOOK_PERMISSION = {
    "env_config": "railway_env",
    "railway_crash": "railway_deploy",
}
# Action-log name per permission; coordinator/tick.py's
# ACTIONS_REQUIRING_REVIEW keys on these names.
_PLAYBOOK_ACTION_TYPE = {
    "railway_deploy": "railway_deploy",
    "railway_env": "railway_env",
}


class Actuators:
    """Controlled side-effect layer. Every action is logged.

    See the module header for the gate every method applies (permission,
    analysis-only, dry run) and the one documented dry-run exemption.
    """

    def __init__(self, agent_name: str, task_id: str, dry_run: bool = True,
                 model_calls=None, abandoned=None):
        # An agent is handed its instance; it does not make one. A call from
        # inside the coordinator package is accepted only from the modules
        # that issue them. (Callers outside the package, which are the tests
        # and nothing else in this repository, are not restricted here; the
        # import scan in tests/test_coordinator_actuator_gates.py covers what
        # a coordinator module may import from outside.)
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if (caller == "coordinator" or caller.startswith("coordinator.")) \
                and caller not in _ISSUING_MODULES:
            raise PermissionError(
                f"{caller} may not construct Actuators; an agent uses the instance the tick hands it")
        self.agent = agent_name
        self.task_id = task_id
        self.dry_run = dry_run
        # A threading.Event the tick sets when it closes this task at its
        # deadline, or None. The agent's thread cannot be stopped, so from
        # then on every gated call it makes is refused (_check_permission),
        # and so is an approval request.
        self.abandoned = abandoned
        # The tick's allowance for model calls (coordinator.budget.ModelCallMeter),
        # or None. authorize_model_call grants nothing without one.
        self.model_calls = model_calls
        self.actions: list[Action] = []

    # --- Ops playbooks ---
    def run_playbook(self, category: str, fixer_fn, context: dict):
        """Run an ops playbook fixer. Returns (fix_result, ran_live).

        fixer_fn is the callable ops.playbook.load_fixer returned for the
        category. A category that has no entry in PLAYBOOK_PERMISSION raises
        ValueError before the fixer is called: there is no permission to
        check it against. Otherwise the fixer is always called, but with
        context["dry_run"] False (a live run that may set a variable or
        redeploy) only when all of these hold:
          - this Actuators is not in dry run;
          - the agent holds the playbook's permission (PLAYBOOK_PERMISSION),
            which COORDINATOR_ANALYSIS_ONLY removes;
          - the ops circuit breaker allows another live fix. It fails closed:
            if its audit table cannot be read, the run stays dry.
        Otherwise the fixer gets dry_run True and only describes what it would
        do. Unlike the other methods, a missing permission downgrades the run
        instead of raising, so the caller still gets the description.

        A live run is written to the ops audit log (what the breaker counts).
        """
        required = PLAYBOOK_PERMISSION.get(category)
        if required is None:
            raise ValueError(f"no playbook permission is defined for category {category!r}")
        live = not self.dry_run
        try:
            self._check_permission(required)
        except PermissionError as exc:
            self._log("Playbook '%s' forced to dry-run: %s", category, exc)
            live = False
        if live and not _playbook_breaker_allows(category):
            live = False

        fix_result = fixer_fn({**context, "dry_run": not live})
        self._record(Action(_PLAYBOOK_ACTION_TYPE[required], {
            "playbook": category,
            "dry_run": not live,
            "success": bool(getattr(fix_result, "success", False)),
        }))
        if live:
            _audit_live_playbook(category, context.get("build_ids") or [], fix_result)
        return fix_result, live

    # --- Model calls ---
    def authorize_model_call(self, purpose: str, model: str = "claude-haiku-4-5") -> bool:
        """Gate ONE call to `model` made on this agent's behalf; call it only on True.

        Raises PermissionError when the agent does not hold model_call. Returns
        False under dry run: a dry run spends nothing, and the write the model
        output would feed is not going to happen either. Returns False when the
        tick's allowance does not cover the call: the meter handed to this
        instance (coordinator.budget.ModelCallMeter) has no call left, does not
        allow the model, or has fewer dollars left than the call's estimate.
        An instance that was given no meter grants no call at all, so a model
        call cannot be made outside a tick's budget by leaving the meter out.
        Each True reserves one call; ask again for the next one.
        """
        self._check_permission("model_call")
        if self.dry_run:
            self._log("DRY_RUN: would call a model for %s", purpose)
            self._record(Action("model_call", {"purpose": purpose, "dry_run": True}))
            return False
        if self.model_calls is None or not self.model_calls.grant(model):
            self._log("No model call allowance left for %s (%s); not calling", purpose, model)
            self._record(Action("model_call_refused", {"purpose": purpose, "model": model}))
            return False
        self._record(Action("model_call", {"purpose": purpose, "model": model}))
        return True

    # --- Database ---
    def query_readonly(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run ONE statement in a read-only transaction and return its rows.

        The guarantee is the database's, and it has two parts that need each
        other:

          * The statement runs inside `BEGIN READ ONLY`, and PostgreSQL then
            refuses every write that statement could make (a data-modifying
            CTE, `setval`, a `SELECT ... FOR UPDATE`): it raises
            psycopg2.errors.ReadOnlySqlTransaction.
          * That transaction holds only while the text is ONE statement. A
            `COMMIT;` in front of a `DELETE` ends it, and the rest runs in
            another. So the text is refused unless it holds exactly one
            statement, counted by a lexer for PostgreSQL text
            (coordinator/sql_statements.py; PermissionError otherwise), not
            by a list of words: a comment, a string or a dollar-quoted body
            that contains a semicolon is still one statement. The text is
            checked as written and again as psycopg2 sends it, after the
            parameters are in.

        As a backstop for a lexer that read the text differently from the
        server, the session's default is read-only too
        (`default_transaction_read_only`), so a second statement that did run
        would start a read-only transaction of its own.

        Not covered: a function that acts outside this connection (dblink and
        the like), which a read-only transaction cannot contain. The platform's
        schema installs no extension. A lock the statement takes (a
        session-level advisory lock, or LOCK TABLE, which a read-only
        transaction allows) is released when the connection closes.

        Bounds: opening the connection gives up after 15 s, the statement
        after 15 s (statement_timeout). The number of rows is whatever the
        statement returns; every caller passes constant SQL with its own limit.
        """
        # A courtesy: the plainest writes are refused before a connection is
        # opened. Nothing relies on it (a comment in front of the keyword gets
        # past it); the two parts above are the guarantee.
        sql_upper = sql.strip().upper()
        if any(sql_upper.startswith(kw) for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE")):
            raise PermissionError("Actuator query_readonly does not allow mutations")
        _require_one_statement(sql)
        self._record(Action("db_query", {"sql": sql[:200]}))

        import psycopg2
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return []
        conn = psycopg2.connect(db_url, connect_timeout=15)
        try:
            conn.set_session(readonly=True, autocommit=False)
            cur = conn.cursor()
            cur.execute("SET statement_timeout = 15000")
            cur.execute("SET default_transaction_read_only = on")
            # The lexer assumes standard string literals (see sql_statements).
            cur.execute("SET standard_conforming_strings = on")
            sent = cur.mogrify(sql, params)
            _require_one_statement(sent)
            cur.execute(sent)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            conn.close()

    # --- Email ---
    def send_alert(self, subject: str, body: str) -> bool:
        """Send an alert email to ADMIN_EMAIL. The recipient is not caller-controlled."""
        self._check_permission("email_send")
        if self.dry_run:
            self._log("DRY_RUN: would send alert: %s", subject)
            self._record(Action("email_send", {"subject": subject, "dry_run": True}))
            return True

        try:
            from ops.alerter import send_admin_alert
            sent = send_admin_alert(subject, body)
        except Exception as exc:
            self._log("Send alert failed: %s", str(exc))
            return False
        self._record(Action("email_send", {"subject": subject, "sent": sent}))
        return sent


    # --- Escalation / Approval ---
    def create_approval_request(
        self,
        request_type: str,
        description: str,
        context: dict | None = None,
    ) -> str | None:
        """Insert an approval request and send an escalation email to the admin.

        Returns the approval request UUID, or None on failure / dry-run.
        Gated like every other method: raises PermissionError when the agent
        does not hold approval_request, in COORDINATOR_ANALYSIS_ONLY mode
        (the permission is not an analysis-only one), and once the task was
        closed at its deadline. Under dry run it records what it would have
        filed and files nothing.
        """
        context = context or {}
        self._check_permission("approval_request")

        if self.dry_run:
            self._log("DRY_RUN: would create approval request: %s", description[:120])
            self._record(Action("approval_request", {
                "type": request_type,
                "description": description[:200],
                "dry_run": True,
            }))
            return None

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            self._log("No DATABASE_URL; cannot create approval request")
            return None

        request_id = str(uuid.uuid4())
        conn = psycopg2.connect(db_url)
        try:
            cur = conn.cursor()
            # One pending request per (type, description): an escalation that
            # recurs every tick must not file, and email, a new request each time.
            cur.execute(
                "INSERT INTO approval_requests (id, type, description, context, status) "
                "SELECT %s, %s, %s, %s, 'pending' "
                "WHERE NOT EXISTS (SELECT 1 FROM approval_requests "
                "WHERE status = 'pending' AND type = %s AND description = %s) "
                "RETURNING id",
                (request_id, request_type[:100], description, json.dumps(context, default=str),
                 request_type[:100], description),
            )
            inserted = cur.fetchone() is not None
            conn.commit()
            cur.close()
        except Exception as exc:
            conn.rollback()
            self._log("Failed to insert approval request: %s", str(exc))
            return None
        finally:
            conn.close()

        if not inserted:
            self._log("Approval request already pending (%s); not filing another", request_type)
            self._record(Action("approval_request", {
                "type": request_type, "description": description[:200], "duplicate": True}))
            return None

        self._record(Action("approval_request", {
            "id": request_id,
            "type": request_type,
            "description": description[:200],
        }))

        # Best-effort email notification
        _send_escalation_email(request_type, description, context, request_id)

        self._log("Created approval request %s (%s)", request_id[:8], request_type)
        return request_id

    # --- Build Queue ---
    def queue_rebuild(self, parent_build_id: str, rebuild_request: str = "") -> str | None:
        """Queue a rebuild of an existing build. Returns the new build id, or None.

        The rebuild is refused (None, with the reason on the action record)
        when any of these holds:
          * deterministic_failure: the parent failed with an error that the
            same input reproduces (ops.scanner.is_deterministic_failure, the
            rule the worker's own retry decision uses);
          * customer_build_start_limit: the customer already has
            CUSTOMER_MAX_ACTIVE_BUILDS builds queued or in progress, or
            CUSTOMER_MAX_BUILD_STARTS_PER_DAY starts in the last 24 hours
            (the web app's limits, web/src/services/build-start-limits.ts,
            read from the same tables under the same lock);
          * rebuild_cap: a child of this parent is still in flight, or
            MAX_REBUILDS_PER_BUILD_24H were queued for it in 24 hours;
          * lineage_cap: MAX_REBUILDS_PER_LINEAGE automatic rebuilds were
            already recorded for the lineage's root in LINEAGE_WINDOW_DAYS.
        An accepted rebuild inserts the child build and one build_start_events
        row (kind 'rebuild', filed under the root's id) in one transaction.
        """
        self._check_permission("build_queue")
        if self.dry_run:
            self._log("DRY_RUN: would queue rebuild of %s", parent_build_id)
            self._record(Action("build_queue", {"parent_build_id": parent_build_id, "dry_run": True}))
            return None

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            self._log("No DATABASE_URL; cannot queue rebuild")
            return None

        from ops.scanner import is_deterministic_failure

        def refuse(reason: str, detail: str):
            conn.rollback()  # also releases the customer's build-start lock
            self._log("Rebuild of %s refused (%s): %s", str(parent_build_id)[:8], reason, detail)
            self._record(Action("build_queue", {
                "parent_build_id": parent_build_id, "refused": reason}))

        conn = psycopg2.connect(db_url)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT customer_id, tier, app_name, spec, status, error_message "
                "FROM builds WHERE id = %s",
                (parent_build_id,),
            )
            row = cur.fetchone()
            if not row:
                self._log("Parent build %s not found", parent_build_id)
                return None
            customer_id, tier, app_name, spec, status, error_message = row

            if customer_id is None:
                refuse("no_customer", "the build belongs to no customer account")
                return None
            if status == "failed" and is_deterministic_failure(error_message):
                refuse("deterministic_failure", "the same input fails the same way")
                return None

            # From here to the commit this customer's build starts are
            # serialized with the web app's (its own statement, so the counts
            # below see whatever the previous holder committed).
            cur.execute(_CUSTOMER_BUILD_LOCK_SQL, (str(customer_id),))
            cur.execute(_CUSTOMER_BUILD_BUDGET_SQL, (customer_id, customer_id))
            active_builds, starts_today = cur.fetchone()
            if active_builds >= CUSTOMER_MAX_ACTIVE_BUILDS \
                    or starts_today >= CUSTOMER_MAX_BUILD_STARTS_PER_DAY:
                refuse("customer_build_start_limit",
                       f"{active_builds} active, {starts_today} starts in 24h")
                return None

            # Never stack rebuilds on one parent: refuse while a child of it is
            # still in flight, and cap how many are queued per 24h.
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE status IN "
                "('queued', 'building', 'testing', 'deploying')), "
                "COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') "
                "FROM builds WHERE parent_build_id = %s",
                (parent_build_id,),
            )
            active_children, recent_children = cur.fetchone()
            if active_children or recent_children >= MAX_REBUILDS_PER_BUILD_24H:
                refuse("rebuild_cap",
                       f"{active_children} in flight, {recent_children} queued in 24h "
                       f"(cap {MAX_REBUILDS_PER_BUILD_24H})")
                return None

            # The lineage bound (see MAX_REBUILDS_PER_LINEAGE).
            cur.execute(_LINEAGE_ROOT_SQL, (parent_build_id, _LINEAGE_MAX_DEPTH))
            root_id = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM build_start_events "
                "WHERE build_id = %s AND kind = 'rebuild' "
                "AND created_at > NOW() - make_interval(days => %s)",
                (root_id, LINEAGE_WINDOW_DAYS),
            )
            lineage_rebuilds = cur.fetchone()[0]
            if lineage_rebuilds >= MAX_REBUILDS_PER_LINEAGE:
                refuse("lineage_cap",
                       f"{lineage_rebuilds} automatic rebuilds of this lineage in "
                       f"{LINEAGE_WINDOW_DAYS} days (cap {MAX_REBUILDS_PER_LINEAGE})")
                return None

            new_id = str(uuid.uuid4())
            # The v1 engine reads the request from the spec (cli/build.py);
            # the v2 engine ignores the key.
            spec_data = spec if isinstance(spec, dict) else json.loads(spec) if spec else {}
            if rebuild_request:
                spec_data["rebuild_request"] = rebuild_request

            cur.execute(
                "INSERT INTO builds (id, customer_id, tier, status, app_name, spec, "
                "parent_build_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'queued', %s, %s, %s, NOW(), NOW()) RETURNING id",
                (new_id, customer_id, tier, app_name, json.dumps(spec_data), parent_build_id),
            )
            cur.execute(
                "INSERT INTO build_start_events (customer_id, build_id, kind) "
                "VALUES (%s, %s, 'rebuild')",
                (customer_id, root_id),
            )
            conn.commit()
            cur.close()

            self._log("Queued rebuild %s (parent: %s)", new_id[:8], str(parent_build_id)[:8])
            self._record(Action("build_queue", {
                "new_build_id": new_id, "parent_build_id": parent_build_id,
                "app_name": app_name,
            }))
            return new_id
        except Exception as exc:
            conn.rollback()
            self._log("Queue rebuild failed: %s", str(exc))
            return None
        finally:
            conn.close()

    def queue_new_build(self, customer_id: str, app_name: str, spec: dict,
                        tier: str = "build") -> str | None:
        """Queue a brand-new build with a spec. Returns build ID or None."""
        self._check_permission("build_queue")
        if self.dry_run:
            self._log("DRY_RUN: would queue new build %s", app_name)
            self._record(Action("build_queue", {"app_name": app_name, "dry_run": True}))
            return None

        import psycopg2
        import uuid
        import json
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            self._log("No DATABASE_URL; cannot queue build")
            return None

        # Normalize spec before insert
        spec.setdefault("spec_version", "0.1")
        if spec.get("spec_version") != "0.1":
            self._log("Fixing spec_version %s -> 0.1 for %s", spec["spec_version"], app_name)
            spec["spec_version"] = "0.1"

        conn = psycopg2.connect(db_url)
        try:
            cur = conn.cursor()
            new_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO builds (id, customer_id, tier, status, app_name, spec, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, 'queued', %s, %s, NOW(), NOW()) RETURNING id",
                (new_id, customer_id, tier, app_name, json.dumps(spec)),
            )
            conn.commit()
            cur.close()

            self._log("Queued new build %s (%s)", new_id[:8], app_name)
            self._record(Action("build_queue", {
                "new_build_id": new_id, "app_name": app_name, "tier": tier,
            }))
            return new_id
        except Exception as exc:
            conn.rollback()
            self._log("Queue new build failed: %s", str(exc))
            return None
        finally:
            conn.close()

    def set_build_error_category(self, build_id: str, category: str) -> bool:
        """Cache a failure category on a failed build (builds.error_category).

        The category must be one of build_failure_categorizer.VALID_CATEGORIES,
        and only a row that is still 'failed' is updated. Returns True when a
        row was written.
        """
        self._check_permission("build_annotate")
        from coordinator.build_failure_categorizer import VALID_CATEGORIES
        if not build_id or category not in VALID_CATEGORIES:
            return False
        if self.dry_run:
            self._log("DRY_RUN: would set error_category=%s on build %s", category, str(build_id)[:8])
            self._record(Action("build_annotate", {
                "build_id": build_id, "error_category": category, "dry_run": True}))
            return False

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            self._log("No DATABASE_URL; cannot set error_category")
            return False
        try:
            conn = psycopg2.connect(db_url)
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE builds SET error_category = %s WHERE id = %s AND status = 'failed'",
                    (category, build_id),
                )
                updated = cur.rowcount == 1
                conn.commit()
                cur.close()
            finally:
                conn.close()
        except Exception as exc:
            self._log("set_build_error_category failed for %s: %s", str(build_id)[:8], str(exc))
            return False
        self._record(Action("build_annotate", {
            "build_id": build_id, "error_category": category, "updated": updated}))
        return updated


    # --- Support KB ---
    def support_kb_insert(self, question: str, answer: str) -> bool:
        """INSERT a Q&A pair into support_kb so the support chat can serve a
        cached answer the next time a similar question is asked.

        This closes the customer-service self-learning loop: the agent
        already detects KB gaps from beta_feedback questions; this writes
        the drafted answer back so the next keeper who asks the same thing
        gets an immediate response without re-paying the LLM cost.

        Permission: kb_record (same gate as the failure-KB record_fix).
        Skipped on dry run, always: a support_kb row is served to customers by
        the support chat, so it is a production write, and analysis-only mode
        does not turn a dry run into a live one.

        Deduplicates on question_normalized via WHERE NOT EXISTS so a
        repeated KB-gap detection for the same question doesn't pile up
        duplicate rows.
        """
        self._check_permission("kb_record")

        if not question or not question.strip() or not answer or not answer.strip():
            self._log("support_kb_insert: empty question/answer; skipping")
            return False

        question = question.strip()
        answer = answer.strip()

        from coordinator.support_knowledge import kb_rejection_reason
        rejection = kb_rejection_reason(question, answer)
        if rejection:
            self._log("support_kb_insert: rejected draft (%s) for %r", rejection, question[:80])
            self._record(Action("support_kb_insert", {
                "question": question[:200], "rejected": rejection}))
            return False

        if self.dry_run:
            self._log(
                "DRY_RUN: would insert support_kb question=%r",
                question[:80],
            )
            self._record(Action("support_kb_insert", {
                "question": question[:200],
                "answer_chars": len(answer),
                "dry_run": True,
            }))
            return True

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            self._log("No DATABASE_URL; cannot insert support_kb row")
            return False

        from coordinator.support_knowledge import normalize_question
        normalized = normalize_question(question)
        if not normalized:
            self._log("support_kb_insert: question normalizes to empty; skipping")
            return False

        try:
            conn = psycopg2.connect(db_url)
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO support_kb (question, question_normalized, answer)
                    SELECT %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM support_kb
                        WHERE question_normalized = %s
                    )
                    RETURNING id
                    """,
                    (question, normalized, answer, normalized),
                )
                row = cur.fetchone()
                conn.commit()
                cur.close()
                inserted = row is not None
            finally:
                conn.close()
        except Exception as exc:
            self._log("support_kb_insert failed: %s", str(exc))
            return False

        self._record(Action("support_kb_insert", {
            "question": question[:200],
            "answer_chars": len(answer),
            "inserted": inserted,
        }))
        if inserted:
            self._log("Inserted support_kb row for question %r", question[:80])
        else:
            self._log(
                "support_kb row already exists for question %r; skipping",
                question[:80],
            )
        return True

    # --- Failure KB ---
    def record_fix(self, error_msg: str, fix_desc: str, files: list,
                   category: str, success: bool) -> bool:
        """Record a fix in the failure knowledge base (cli/failure_kb.json).

        Skipped on dry run: the v1 build pipeline reads this file back into its
        prompts, so a row here is not a private note.

        Returns False when the entry could not be written (that is logged and
        recorded as a `kb_record_failed` action); True otherwise, a dry run
        included, which writes nothing and says so in the action it records.
        """
        self._check_permission("kb_record")

        if self.dry_run:
            self._log("DRY_RUN: would record a %s fix in the failure KB", category)
            self._record(Action("kb_record", {
                "category": category, "success": success, "dry_run": True}))
            return True

        try:
            from cli.failure_kb import record_fix as kb_record_fix
            kb_record_fix(error_msg, fix_desc, files, category, success)
        except Exception as exc:
            self._log("KB record failed: %s", str(exc))
            self._record(Action("kb_record_failed", {"category": category}))
            return False

        self._record(Action("kb_record", {"category": category, "success": success}))
        return True

    def record_failure_category(self, error_message: str, category: str) -> bool:
        """Record an automatic failure categorization in the failure KB.

        The entry is written as an outstanding failure (failure_count and a
        False in recent_outcomes) that carries the assigned fix_category.
        cli.failure_kb.record_fix only stores fix_category for a fix that
        succeeded, which is wrong for a categorization made before any fix, so
        the entry is shaped here. A recurring uncategorized failure therefore
        lowers the entry's recent success rate. Nothing in the coordinator
        reads that rate any more; the entry is kept for the failure KB's own
        consumers (the weekly sync into the support KB ranks entries by count).

        Skipped on dry run. Returns True when the KB was written.
        """
        self._check_permission("kb_record")
        from coordinator.build_failure_categorizer import VALID_CATEGORIES
        if not error_message or category not in VALID_CATEGORIES:
            return False
        if self.dry_run:
            self._log("DRY_RUN: would record failure category %s in the failure KB", category)
            self._record(Action("kb_record", {"category": category, "dry_run": True}))
            return False

        try:
            import time
            from cli.failure_kb import RECENT_WINDOW, _load_kb, _save_kb, normalize_error

            sig = normalize_error(error_message)
            if not sig:
                return False
            kb = _load_kb()
            entry = kb.get(sig, {
                "signature": sig,
                "fix_description": "",
                "files_changed": [],
                "fix_category": "unknown",
                "success_count": 0,
                "failure_count": 0,
                "first_seen": time.time(),
                "last_seen": 0,
            })
            entry["last_seen"] = time.time()
            entry["failure_count"] = entry.get("failure_count", 0) + 1
            entry["fix_category"] = category
            # Note what the categorizer thinks the cause is, but never overwrite
            # the description of a real fix that succeeded.
            if not entry.get("fix_description") or entry["fix_description"].startswith(
                "[auto-categorized]"
            ):
                entry["fix_description"] = f"[auto-categorized] {category}"
            recent = entry.get("recent_outcomes", [])
            recent.append(False)
            entry["recent_outcomes"] = recent[-RECENT_WINDOW:]
            kb[sig] = entry
            _save_kb(kb)
        except Exception as exc:
            self._log("record_failure_category failed: %s", str(exc))
            return False

        self._record(Action("kb_record", {"category": category, "auto_categorized": True}))
        return True

    # --- Coordinator Findings (DB-backed) ---
    def finding_add(
        self,
        finding_key: str,
        title: str,
        description: str,
        priority: str = "medium",
        category: str = "improvement",
        source_task_id: str | None = None,
    ) -> bool:
        """Record a coordinator analysis finding in coordinator_findings.

        Primary output channel for COORDINATOR_ANALYSIS_ONLY mode. Findings
        live in the shared database, where any coordinator instance sees the
        same state.

        Deduplicates by finding_key via the unique index; ON CONFLICT DO
        NOTHING. Safe to call repeatedly.

        Bypasses dry_run because the findings table is a coordination log,
        not a production system side effect (analogous to record_fix on the KB).
        """
        self._check_permission("finding_write")

        import psycopg2
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            self._log("No DATABASE_URL; cannot record finding '%s'", finding_key)
            return False

        try:
            conn = psycopg2.connect(db_url)
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO coordinator_findings
                      (source_task_id, agent, category, finding_key, title,
                       description, priority, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'new')
                    ON CONFLICT (finding_key) DO NOTHING
                    RETURNING id
                    """,
                    (source_task_id, self.agent, category, finding_key,
                     title, description, priority),
                )
                row = cur.fetchone()
                conn.commit()
                cur.close()
                inserted = row is not None
            finally:
                conn.close()
        except Exception as exc:
            self._log("finding_add failed for '%s': %s", finding_key, str(exc))
            return False

        self._record(Action("finding_add", {
            "finding_key": finding_key,
            "title": title,
            "priority": priority,
            "inserted": inserted,
        }))
        if inserted:
            self._log("Recorded finding: %s", finding_key)
        else:
            self._log("Finding '%s' already exists; skipping", finding_key)
        return True

    # --- Permission enforcement ---
    @property
    def analysis_only(self) -> bool:
        """True when COORDINATOR_ANALYSIS_ONLY is switched on.

        A safety switch that is off when unset: an explicit OFF word (or
        nothing) leaves the agents their full permissions, and any other value
        that is set narrows them (coordinator.envflags.safety_switch).
        """
        return safety_switch("COORDINATOR_ANALYSIS_ONLY", default=False)

    def _task_closed(self) -> bool:
        return self.abandoned is not None and self.abandoned.is_set()

    def _check_permission(self, action_type: str):
        # First, because it holds for every action: nothing is done on behalf
        # of a task the tick has already closed.
        if self._task_closed():
            raise PermissionError(
                f"Agent '{self.agent}' not permitted: {action_type} (its task was closed at the deadline)")

        # Analysis-only mode narrows every agent to the analysis set. It is a
        # hard gate, not dry_run, and it never widens: the agent must still
        # hold the permission in AGENT_PERMISSIONS below.
        if self.analysis_only and action_type not in _ANALYSIS_ONLY_PERMISSIONS:
            raise PermissionError(
                f"Action '{action_type}' blocked: COORDINATOR_ANALYSIS_ONLY mode is active"
            )

        allowed = AGENT_PERMISSIONS.get(self.agent, set())
        if action_type not in allowed:
            raise PermissionError(f"Agent '{self.agent}' not permitted: {action_type}")

    def _record(self, action: Action):
        self.actions.append(action)

    def _log(self, msg, *args, **kwargs):
        task_short = self.task_id[:8] if self.task_id else "unknown"
        formatted = msg % args if args else msg
        logger.info("[%s:%s] %s", self.agent, task_short, formatted)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _playbook_breaker_allows(category: str) -> bool:
    """Apply the legacy ops circuit breaker (default 3 live fixes per hour).

    The breaker counts "fix_applied" rows in ops_actions, which
    _audit_live_playbook writes. Fails closed: if the audit table cannot be
    read, the playbook is not allowed to run live.
    """
    try:
        from ops.guardrails import CircuitBreaker
        allowed = CircuitBreaker().can_proceed()
    except Exception as exc:
        logger.warning("Circuit breaker unavailable (%s); playbook '%s' stays dry-run",
                       exc, category)
        return False
    if not allowed:
        logger.warning("Circuit breaker tripped; playbook '%s' stays dry-run", category)
    return allowed


def _audit_live_playbook(category: str, build_ids: list, fix_result) -> None:
    """Best-effort ops_actions row so the circuit breaker sees live fixes."""
    try:
        from ops.audit import log_action
        log_action(
            "fix_applied", fix_result.description, category=category,
            build_ids=list(build_ids) or None,
            dry_run=False,
            success=bool(fix_result.success),
        )
    except Exception as exc:
        logger.warning("Could not audit live fix for '%s': %s", category, exc)


def _send_escalation_email(
    request_type: str,
    description: str,
    context: dict,
    request_id: str,
) -> bool:
    """Send an escalation email to ADMIN_EMAIL via Resend.

    Uses the same pattern as ops/alerter.py (_send_email).
    Returns True on success.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    if not api_key:
        logger.warning("RESEND_API_KEY not set; skipping escalation email")
        return False
    if not admin_email:
        logger.warning("ADMIN_EMAIL not set; skipping escalation email")
        return False

    context_lines = ""
    for key, value in context.items():
        context_lines += (
            f'<tr><td style="padding:4px 12px;color:#94a3b8;font-size:0.85rem;">'
            f"{_esc(str(key))}</td>"
            f'<td style="padding:4px 12px;color:#e2e8f0;font-size:0.85rem;">'
            f"{_esc(str(value)[:200])}</td></tr>"
        )

    subject = f"Coordinator Escalation: {request_type}"
    approvals_url = _admin_approvals_url()
    review_button = (
        f'''<p style="text-align:center;margin:24px 0 8px;">
      <a href="{_esc(approvals_url)}"
         style="display:inline-block;padding:12px 28px;background:#22c55e;color:#fff;
                text-decoration:none;border-radius:6px;font-weight:600;">
        Review in Admin
      </a>
    </p>''' if approvals_url else ""
    )
    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;">
  <div style="background:#18181b;padding:20px 24px;border-radius:8px 8px 0 0;">
    <span style="color:#f87171;font-weight:700;font-size:1rem;">Approval Required</span>
  </div>
  <div style="background:#ffffff;padding:24px;border:1px solid #e4e4e7;border-top:none;">
    <p style="margin:0 0 12px;color:#3f3f46;font-size:0.95rem;"><strong>Type:</strong> {_esc(request_type)}</p>
    <p style="margin:0 0 16px;color:#3f3f46;font-size:0.95rem;"><strong>Summary:</strong> {_esc(description[:500])}</p>
    {('<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">' + context_lines + '</table>') if context_lines else ''}
    {review_button}
    <p style="color:#a1a1aa;font-size:0.75rem;text-align:center;">
      Request ID: {_esc(str(request_id))}
    </p>
  </div>
  <div style="background:#f4f4f5;padding:12px 24px;border-radius:0 0 8px 8px;
              text-align:center;font-size:0.75rem;color:#a1a1aa;">
    Craft &amp; Ship Coordinator: a request nobody resolves is denied automatically, never approved.
  </div>
</div>"""

    payload = json.dumps({
        "from": os.environ.get("RESEND_FROM_EMAIL") or "Craft & Ship Ops <noreply@craftandship.io>",
        "to": [admin_email],
        "subject": subject,
        "html": html,
    }).encode()

    req = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        # One bounded exchange: 10 seconds for the whole request, 64 KiB of answer.
        with bounded_urlopen(req, timeout=10, max_body_bytes=64 * 1024) as resp:
            if resp.status in (200, 201):
                logger.info("Escalation email sent for request %s", request_id[:8])
                return True
            logger.warning("Resend returned %d for escalation email", resp.status)
            return False
    except (URLError, OSError) as exc:
        logger.error("Failed to send escalation email: %s", exc)
        return False


def _esc(text: str) -> str:
    """Minimal HTML escaping for email templates."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
