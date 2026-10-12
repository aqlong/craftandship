# coordinator/agents/documentation.py: Documentation Engineer agent
#
# One check, on its turn of initiative work: every table created in
# web/src/db/schema.sql is either named in CLAUDE.md, or CLAUDE.md hands the
# schema over to that file (a line that names web/src/db/schema.sql as the
# database schema or as a source of truth), in which case there is nothing to
# compare. Pure string comparison of two files in the worker's checkout: no
# model call, no subprocess, no edit. Drift is filed as one finding and one
# alert email.
#
# What it does not check, and why: test counts (the worker image has no
# pytest, and CLAUDE.md states no count to compare with) and files added since
# a commit (the image has no .git directory).

import logging
import os
import re

from coordinator.agents.base import Agent, AgentResult, Effects
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators

logger = logging.getLogger("coordinator.agents.documentation")

# How much of either file is read: both are a few hundred kilobytes at most.
_MAX_FILE_BYTES = 2 * 1024 * 1024

# CLAUDE.md delegates the schema when one line both names schema.sql and says
# what it is: "Database schema: `web/src/db/schema.sql`", "DB schema ->
# web/src/db/schema.sql", "Schema source of truth: web/src/db/schema.sql".
# A bare mention of the path elsewhere does not count.
_ARROW = chr(0x2192)   # the rightwards arrow, also used as "see"
_SCHEMA_POINTER_RE = re.compile(
    r"(?:database\s+schema|db\s+schema|source[\s-]+of[\s-]+truth|" + _ARROW
    + r"|->)[^\n]*web/src/db/schema\.sql"
    r"|web/src/db/schema\.sql[^\n]*source[\s-]+of[\s-]+truth",
    re.IGNORECASE,
)
_CREATE_TABLE_RE = re.compile(r'CREATE TABLE IF NOT EXISTS\s+["\']?(\w+)')


class DocumentationEngineer(Agent):
    name = "documentation"
    description = "Reports schema tables that CLAUDE.md neither names nor delegates to schema.sql"
    requires_review = False  # Reads two files; files a finding and an alert
    # Model-free in every category (checked against the source by
    # tests/test_coordinator_model_free_tasks.py).
    model_categories = frozenset()

    def execute(self, task: WorkItem, state: WorldState, model: str,
                actuators: Actuators) -> AgentResult:
        category = task.category

        if category == "agent_initiative":
            return self._handle_docs_sync(actuators)
        else:
            return AgentResult(
                success=False,
                output={"error": f"Unknown category: {category}"},
                learnings=[f"DocumentationEngineer received unhandled category: {category}"],
            )

    def _handle_docs_sync(self, actuators: Actuators) -> AgentResult:
        """Compare schema.sql with CLAUDE.md and report what is missing."""
        try:
            issues = self._check_schema_freshness()
        except OSError as exc:
            logger.warning("Schema check failed: %s", exc)
            return AgentResult(
                success=False,
                output={"error": f"Could not read the files to compare: {exc}"},
            )

        if not issues:
            return AgentResult(
                success=True,
                output={"status": "docs_current", "issues_found": 0},
                learnings=["Docs sync: CLAUDE.md names or delegates every schema table"],
            )

        # Send an alert with the findings. No agent edits CLAUDE.md: the
        # edits are left to a person.
        report = "Documentation drift detected:\n\n" + "\n".join(f"- {i}" for i in issues)

        effects = Effects()
        effects.alert(actuators, "[CAS] Docs drift detected", report)

        # Record doc drift as a DB finding for admin review
        effects.finding(
            actuators,
            finding_key="docs-drift-detected",
            title="Update CLAUDE.md for documentation drift",
            description=(
                "Documentation drift detected during the docs sync. Issues:\n"
                + "\n".join(f"- {i}" for i in issues)
            ),
            priority="low",
            category="docs_drift",
        )

        return AgentResult(
            success=True,
            output={"status": "drift_detected", "issues_found": len(issues), "issues": issues},
            actions_taken=actuators.actions,
            effects=effects.entries,
            learnings=[f"Docs sync found {len(issues)} issues: {'; '.join(issues[:3])}"] + effects.learnings(),
        )

    def _check_schema_freshness(self) -> list[str]:
        """Tables in schema.sql that CLAUDE.md does not mention.

        Returns [] when either file is missing, and when CLAUDE.md delegates
        the schema to schema.sql (_SCHEMA_POINTER_RE): the published CLAUDE.md
        lists no tables and points at the file instead.

        The files are looked for under PROJECT_ROOT (default /app, where the
        worker image has the repository). Raises OSError if one exists and
        cannot be read.
        """
        project_root = os.environ.get("PROJECT_ROOT", "/app")
        schema_path = os.path.join(project_root, "web/src/db/schema.sql")
        claude_md_path = os.path.join(project_root, "CLAUDE.md")

        if not os.path.exists(schema_path) or not os.path.exists(claude_md_path):
            return []

        with open(schema_path, encoding="utf-8", errors="replace") as f:
            schema = f.read(_MAX_FILE_BYTES)
        with open(claude_md_path, encoding="utf-8", errors="replace") as f:
            claude_md = f.read(_MAX_FILE_BYTES)

        if _SCHEMA_POINTER_RE.search(claude_md):
            return []

        return [
            f"Table '{table}' in schema.sql but not in CLAUDE.md"
            for table in _CREATE_TABLE_RE.findall(schema)
            if table not in claude_md
        ]


def register():
    from coordinator.tick import register_agent
    register_agent("documentation", DocumentationEngineer)
