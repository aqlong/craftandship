# coordinator/agents/base.py: Agent abstract base class
#
# All coordinator agents must subclass Agent and implement execute().

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.base")

# What became of one side effect an agent asked its Actuators for. An agent
# says which of these happened instead of reporting the effect as done.
SENT = "sent"            # an alert email was handed to the mailer
QUEUED = "queued"        # a rebuild was queued
RECORDED = "recorded"    # a finding or a knowledge-base entry was written
DRY_RUN = "dry_run"      # the actuator layer recorded it and did nothing (dry run)
REFUSED = "refused"      # the actuator layer refused it (permission, analysis-only mode, closed task)
FAILED = "failed"        # it was allowed and did not happen (the mailer or the database said no)


@dataclass
class AgentResult:
    """Structured result from an agent execution."""
    success: bool = False
    output: dict = field(default_factory=dict)
    actions_taken: list = field(default_factory=list)
    # Model usage, for an agent that measures it. None of the agents in this
    # package does, so both stay 0; coordinator.tick._task_charge then charges
    # the task its estimated cost and one call against the per-tick allowance.
    api_calls: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    # Short notes about what the task found. The tick stores them on the
    # task's row (agent_tasks.learnings); nothing reads them back.
    learnings: list = field(default_factory=list)
    follow_up_work: list = field(default_factory=list)
    escalate: bool = False
    escalation_reason: str = ""
    # What became of each side effect the agent asked for: a list of
    # {"effect": "alert", "what": <subject or key>, "outcome": SENT | RECORDED |
    # DRY_RUN | REFUSED | FAILED}, built with Effects. The tick stores it in the
    # task's result row under "effects".
    effects: list = field(default_factory=list)


class Effects:
    """The side effects one task asked its Actuators for, and what became of each.

    An agent used to call `actuators.send_alert(...)` inside a `try` that
    swallowed PermissionError, and then report "alert_sent" whatever had
    happened: refused by the permission table, switched off by analysis-only
    mode, a dry run that sent nothing, a mailer that failed. Each method here
    makes the call, never raises PermissionError, and returns what happened;
    `entries` is the list the agent hands back in AgentResult.effects.
    """

    def __init__(self):
        self.entries: list = []

    def _note(self, effect: str, what: str, outcome: str, detail: str = "") -> str:
        entry = {"effect": effect, "what": str(what)[:200], "outcome": outcome}
        if detail:
            entry["detail"] = detail[:300]
        self.entries.append(entry)
        if outcome in (REFUSED, FAILED):
            logger.info("%s '%s' %s%s", effect, str(what)[:80], outcome, f": {detail}" if detail else "")
        return outcome

    def alert(self, actuators: Actuators, subject: str, body: str) -> str:
        """Send an alert email through the actuator layer; return its outcome."""
        try:
            sent = actuators.send_alert(subject=subject, body=body)
        except PermissionError as refused:
            return self._note("alert", subject, REFUSED, str(refused))
        except Exception as error:
            # The actuator answers False for a mailer that failed; this is for
            # anything else it raises. An alert must not end its task.
            return self._note("alert", subject, FAILED, f"{type(error).__name__}: {error}")
        if actuators.dry_run:
            return self._note("alert", subject, DRY_RUN)   # send_alert answers True without sending
        return self._note("alert", subject, SENT if sent else FAILED)

    def finding(self, actuators: Actuators, **fields) -> str:
        """File a coordinator finding (never held back by dry run); return its outcome."""
        try:
            filed = actuators.finding_add(**fields)
        except PermissionError as refused:
            return self._note("finding", fields.get("finding_key", ""), REFUSED, str(refused))
        return self._note("finding", fields.get("finding_key", ""), RECORDED if filed else FAILED)

    def approval_request(self, actuators: Actuators, request_type: str, description: str, context: dict) -> str:
        """File an approval request for a person to read; return its outcome.

        RECORDED when a request was filed, or when an identical one is already
        pending (the actuator files at most one per type and description);
        FAILED when it could not be filed.
        """
        try:
            request_id = actuators.create_approval_request(
                request_type=request_type, description=description, context=context)
        except PermissionError as refused:
            return self._note("approval_request", request_type, REFUSED, str(refused))
        if request_id:
            return self._note("approval_request", request_type, RECORDED)
        if actuators.dry_run:
            return self._note("approval_request", request_type, DRY_RUN)
        last = actuators.actions[-1] if actuators.actions else None
        if last is not None and last.action_type == "approval_request" and last.details.get("duplicate"):
            return self._note("approval_request", request_type, RECORDED, "an identical request is already pending")
        return self._note("approval_request", request_type, FAILED, "the request could not be filed")

    def rebuild(self, actuators: Actuators, parent_build_id: str, rebuild_request: str = ""):
        """Ask for a rebuild of `parent_build_id`; return (new build id or None, outcome).

        None with outcome FAILED means queue_rebuild declined or could not queue
        (its own limits, a missing customer, a database error); what it
        recorded says which.
        """
        try:
            new_id = actuators.queue_rebuild(parent_build_id, rebuild_request=rebuild_request)
        except PermissionError as refused:
            return None, self._note("rebuild", parent_build_id, REFUSED, str(refused))
        if new_id:
            return new_id, self._note("rebuild", parent_build_id, QUEUED)
        if actuators.dry_run:
            return None, self._note("rebuild", parent_build_id, DRY_RUN)
        return None, self._note("rebuild", parent_build_id, FAILED, "queue_rebuild declined or could not queue it")

    def kb_record(self, actuators: Actuators, category: str, **fields) -> str:
        """Record a fix in the failure KB; return its outcome."""
        try:
            recorded = actuators.record_fix(category=category, **fields)
        except PermissionError as refused:
            return self._note("kb_record", category, REFUSED, str(refused))
        if actuators.dry_run:
            return self._note("kb_record", category, DRY_RUN)
        return self._note("kb_record", category, RECORDED if recorded else FAILED)

    def outcomes(self, effect: str) -> list:
        """The outcomes of every entry of this kind of effect, in order."""
        return [entry["outcome"] for entry in self.entries if entry["effect"] == effect]

    def delivered(self, effect: str = "alert") -> bool:
        """True when every effect of this kind took place (sent or recorded) or was a dry run."""
        return all(outcome in (SENT, RECORDED, QUEUED, DRY_RUN) for outcome in self.outcomes(effect))

    def action(self, effect: str, noun: str) -> str:
        """A one-word summary for a result's "action": `alert_sent`, `alert_dry_run`,
        `alert_refused`, `alert_failed`, or `alert_mixed` when the outcomes differ.
        `noun` is the effect's name in the action word ("alert", "budget_alert")."""
        found = set(self.outcomes(effect))
        if not found:
            return f"{noun}_not_attempted"
        if len(found) > 1:
            return f"{noun}_mixed"
        return f"{noun}_{found.pop()}"

    def learnings(self) -> list:
        """One line for each effect that did not take place, for AgentResult.learnings."""
        return [
            f"{entry['effect']} '{entry['what'][:80]}' was {entry['outcome']}"
            for entry in self.entries if entry["outcome"] in (REFUSED, FAILED)
        ]


class Agent(ABC):
    """Abstract base class for coordinator agents."""
    name: str = ""
    description: str = ""
    requires_review: bool = False
    # The task categories whose handler can reach a model call (a call to
    # Actuators.authorize_model_call). None means the class does not say, and
    # the tick then treats every category as one that may call a model. A class
    # that declares a frozenset says that the categories outside it make none;
    # such a task is not held back by an empty model budget
    # (coordinator/tick.py: _task_may_call_model).
    model_categories = None

    @abstractmethod
    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        """Execute the agent task. Single method, no plan() step.

        Args:
            task: The work item to process
            state: Current world state snapshot. A figure the tick could not
                read is None (see coordinator.state.WorldState).
            model: Claude model to use
            actuators: Permission-gated side-effect layer. The tick builds it
                and hands it over; an agent never constructs its own.

        Returns:
            AgentResult with structured output
        """
        ...
