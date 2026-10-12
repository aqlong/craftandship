# tests/test_documentation_agent.py: Documentation Engineer agent tests

from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents import documentation
from coordinator.agents.documentation import DocumentationEngineer
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators


def _make_state(**overrides):
    defaults = dict(
        tick_id="test-tick", tick_number=1,
        tick_start=datetime.now(timezone.utc),
        queued_builds=0, active_builds=0,
        failed_builds_24h=[], preview_awaiting=0,
        abandoned_intakes_24h=0, pending_feedback=0,
        monitored_apps_down=[], monitored_apps_degraded=[],
        api_cost_today_usd=0, daily_budget_remaining_usd=20.0,
        agent_success_rates={}, agent_avg_cost={},
        pending_work=[], previous_tick_summary={},
    )
    defaults.update(overrides)
    return WorldState(**defaults)


def _make_task(category, context=None):
    return WorkItem(
        category=category,
        target_agent="documentation",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


def _checkout(tmp_path, claude_md: str, tables=("builds", "widgets")):
    """A directory laid out like the worker's checkout, with the two files the
    agent compares."""
    schema_dir = tmp_path / "web" / "src" / "db"
    schema_dir.mkdir(parents=True)
    (schema_dir / "schema.sql").write_text(
        "".join(f"CREATE TABLE IF NOT EXISTS {table} (\n  id UUID PRIMARY KEY\n);\n" for table in tables))
    (tmp_path / "CLAUDE.md").write_text(claude_md)
    return tmp_path


class TestDocsSync:
    """Through execute(), against files on disk (nothing in the agent is mocked)."""

    def _run(self, monkeypatch, root):
        monkeypatch.setenv("PROJECT_ROOT", str(root))
        actuators = Actuators("documentation", "test-task", dry_run=True)
        # finding_add writes to a database even under dry run; there is none
        # here, so watch the call (the real method still runs and declines).
        actuators.finding_add = MagicMock(wraps=actuators.finding_add)
        result = DocumentationEngineer().execute(
            _make_task("agent_initiative"), _make_state(), "claude-haiku-4-5", actuators)
        return result, actuators

    def test_no_issues(self, tmp_path, monkeypatch):
        result, actuators = self._run(monkeypatch, _checkout(tmp_path, "Tables: builds, widgets.\n"))
        assert result.success
        assert result.output == {"status": "docs_current", "issues_found": 0}
        assert actuators.actions == []
        actuators.finding_add.assert_not_called()

    def test_finds_issues(self, tmp_path, monkeypatch):
        result, actuators = self._run(monkeypatch, _checkout(tmp_path, "Tables: builds.\n"))
        assert result.success
        assert result.output["issues"] == ["Table 'widgets' in schema.sql but not in CLAUDE.md"]
        assert [a.action_type for a in actuators.actions] == ["email_send"]
        assert actuators.finding_add.call_args.kwargs["finding_key"] == "docs-drift-detected"
        assert "widgets" in actuators.finding_add.call_args.kwargs["description"]

    def test_a_missing_file_is_nothing_to_compare(self, tmp_path, monkeypatch):
        result, _actuators = self._run(monkeypatch, tmp_path)
        assert result.success and result.output["issues_found"] == 0

    def test_an_unreadable_file_fails_the_task_and_says_why(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path, "Tables: builds.\n")
        (root / "CLAUDE.md").unlink()
        (root / "CLAUDE.md").mkdir()          # exists, cannot be read as a file
        result, _actuators = self._run(monkeypatch, root)
        assert not result.success
        assert "Could not read the files to compare" in result.output["error"]

    def test_unknown_category(self):
        agent = DocumentationEngineer()
        state = _make_state()
        task = _make_task("unknown")
        actuators = Actuators("documentation", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success


class TestAgainstTheRepositorysOwnFiles:
    """The real input: this repository's CLAUDE.md and web/src/db/schema.sql,
    which is what the agent finds under /app in the worker image."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_the_published_claude_md_delegates_the_schema_so_nothing_is_reported(self, monkeypatch):
        """CLAUDE.md lists no tables: it names web/src/db/schema.sql as the
        database schema's source. The earlier pointer pattern did not match
        that wording, and the check reported every table as drift."""
        claude_md = (self.ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        schema = (self.ROOT / "web" / "src" / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "web/src/db/schema.sql" in claude_md
        tables = documentation._CREATE_TABLE_RE.findall(schema)
        assert len(tables) > 30 and [t for t in tables if t not in claude_md], (
            "the control: without the pointer, tables WOULD be reported")

        monkeypatch.setenv("PROJECT_ROOT", str(self.ROOT))
        assert DocumentationEngineer()._check_schema_freshness() == []

    def test_the_agent_starts_no_process_and_opens_no_connection(self):
        """The two checks that shelled out (pytest --collect-only, git log)
        could not produce a result in the worker image and are gone."""
        import ast
        source = (self.ROOT / "coordinator" / "agents" / "documentation.py").read_text()
        imported = {alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Import)
                    for alias in node.names}
        assert imported == {"logging", "os", "re"}
        assert not hasattr(DocumentationEngineer, "_check_test_counts")
        assert not hasattr(DocumentationEngineer, "_check_new_files")


class TestSchemaCheck:
    def test_detects_missing_table(self, tmp_path):
        agent = DocumentationEngineer()

        schema = tmp_path / "web" / "src" / "db"
        schema.mkdir(parents=True)
        (schema / "schema.sql").write_text(
            "CREATE TABLE IF NOT EXISTS new_shiny_table (\n  id UUID PRIMARY KEY\n);\n"
            "CREATE TABLE IF NOT EXISTS builds (\n  id UUID PRIMARY KEY\n);\n"
        )
        (tmp_path / "CLAUDE.md").write_text("## Schema\nbuilds table is documented here.\n")

        with patch.dict("os.environ", {"PROJECT_ROOT": str(tmp_path)}):
            issues = agent._check_schema_freshness()

        assert len(issues) == 1
        assert "new_shiny_table" in issues[0]

    def test_no_drift(self, tmp_path):
        agent = DocumentationEngineer()

        schema = tmp_path / "web" / "src" / "db"
        schema.mkdir(parents=True)
        (schema / "schema.sql").write_text(
            "CREATE TABLE IF NOT EXISTS builds (\n  id UUID PRIMARY KEY\n);\n"
        )
        (tmp_path / "CLAUDE.md").write_text("builds table documented\n")

        with patch.dict("os.environ", {"PROJECT_ROOT": str(tmp_path)}):
            issues = agent._check_schema_freshness()

        assert len(issues) == 0

    def test_skips_when_claude_md_points_at_schema_sql(self, tmp_path):
        """After the 2026-04-16 CLAUDE.md Tier 3 slim, the root context file
        delegates schema to web/src/db/schema.sql via a pointer instead of
        listing tables inline. The drift check must detect that pointer
        pattern and skip, otherwise it fires a false-positive for every
        table in schema.sql, every week."""
        agent = DocumentationEngineer()

        schema = tmp_path / "web" / "src" / "db"
        schema.mkdir(parents=True)
        (schema / "schema.sql").write_text(
            "CREATE TABLE IF NOT EXISTS builds (\n  id UUID PRIMARY KEY\n);\n"
            "CREATE TABLE IF NOT EXISTS customers (\n  id UUID PRIMARY KEY\n);\n"
            "CREATE TABLE IF NOT EXISTS undocumented_table (\n  id UUID PRIMARY KEY\n);\n"
        )
        # CLAUDE.md mentions schema.sql as source of truth; the exact
        # wording that ships in the Tier 3 rewrite.
        (tmp_path / "CLAUDE.md").write_text(
            "## Architecture\n"
            "DB schema → `web/src/db/schema.sql`\n"
        )

        with patch.dict("os.environ", {"PROJECT_ROOT": str(tmp_path)}):
            issues = agent._check_schema_freshness()

        # Zero issues: the pointer triggered a full skip, even though
        # none of the three tables are spelled out in CLAUDE.md.
        assert issues == [], (
            f"Expected zero issues (pointer-to-schema.sql should skip check), "
            f"got: {issues}"
        )

    def test_skips_with_alternate_pointer_phrasing(self, tmp_path):
        """Pointer detection should also match `source of truth: schema.sql`
        phrasing (regex covers both directions)."""
        agent = DocumentationEngineer()

        schema = tmp_path / "web" / "src" / "db"
        schema.mkdir(parents=True)
        (schema / "schema.sql").write_text(
            "CREATE TABLE IF NOT EXISTS undocumented (\n  id UUID PRIMARY KEY\n);\n"
        )
        (tmp_path / "CLAUDE.md").write_text(
            "Schema source of truth: `web/src/db/schema.sql` (see for full definitions).\n"
        )

        with patch.dict("os.environ", {"PROJECT_ROOT": str(tmp_path)}):
            issues = agent._check_schema_freshness()

        assert issues == []


    @pytest.mark.parametrize("line", [
        "- Database schema: `web/src/db/schema.sql`",
        "Source-of-truth files: web/src/db/schema.sql",
        "DB schema -> web/src/db/schema.sql",
    ])
    def test_the_wordings_that_delegate(self, tmp_path, line):
        root = _checkout(tmp_path, f"## Files\n\n{line}\n", tables=("undocumented",))
        with patch.dict("os.environ", {"PROJECT_ROOT": str(root)}):
            assert DocumentationEngineer()._check_schema_freshness() == []

    @pytest.mark.parametrize("line", [
        "Run psql -f web/src/db/schema.sql to load it.",
        "The schema lives somewhere under web/.",
    ])
    def test_a_passing_mention_of_the_path_does_not_delegate(self, tmp_path, line):
        root = _checkout(tmp_path, f"## Files\n\n{line}\n", tables=("undocumented",))
        with patch.dict("os.environ", {"PROJECT_ROOT": str(root)}):
            assert DocumentationEngineer()._check_schema_freshness() == [
                "Table 'undocumented' in schema.sql but not in CLAUDE.md"]


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.documentation import register
            register()
            mock_reg.assert_called_once_with("documentation", DocumentationEngineer)
