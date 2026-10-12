"""tests/test_db_safety.py, Tests for database safety infrastructure.

Covers:
- Migration idempotency (hash-based version tracking)
- Destructive DDL detection
- Drift detection (compare_schemas)
"""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import targets
# ---------------------------------------------------------------------------

from scripts.check_schema_drift import compare_schemas, introspect_schema
from scripts.check_destructive_ddl import check_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "web" / "src" / "db" / "schema.sql"


# ===========================================================================
# Migration idempotency (hash-based)
# ===========================================================================

class TestMigrationIdempotency:
    """Verify the hash-based migration tracking logic."""

    def test_schema_file_exists(self):
        """schema.sql must exist at the expected path."""
        assert SCHEMA_PATH.exists(), f"schema.sql not found at {SCHEMA_PATH}"

    def test_schema_hash_is_deterministic(self):
        """Same content always produces the same hash."""
        content = SCHEMA_PATH.read_text()
        h1 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        h2 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert h1 == h2

    def test_schema_hash_changes_on_edit(self):
        """Modifying schema content changes the hash."""
        content = SCHEMA_PATH.read_text()
        h_original = hashlib.sha256(content.encode("utf-8")).hexdigest()
        h_modified = hashlib.sha256(
            (content + "\n-- new migration").encode("utf-8")
        ).hexdigest()
        assert h_original != h_modified

    def test_schema_is_additive_only(self):
        """Current schema.sql uses only additive DDL (IF NOT EXISTS, ADD COLUMN IF NOT EXISTS)."""
        content = SCHEMA_PATH.read_text()
        # Every CREATE TABLE should have IF NOT EXISTS
        import re

        create_tables = re.findall(
            r"CREATE TABLE(?!\s+IF\s+NOT\s+EXISTS)", content, re.IGNORECASE
        )
        # Filter out the ones inside DO $$ blocks (conditional)
        assert len(create_tables) == 0, (
            f"Found CREATE TABLE without IF NOT EXISTS: {create_tables}"
        )

    # The 4 tests below assert features of the idempotent-migration system
    # that was reverted after causing a production outage. They are
    # preserved as a spec for when migrate.ts idempotency is re-implemented.
    # xfail(strict=True) means CI stays green today AND loudly fails the day
    # someone re-adds the feature without removing these markers, acting as
    # a nudge to re-enable the suite.
    _MIGRATE_TS_REVERTED = pytest.mark.xfail(
        strict=True,
        reason=(
            "migrate.ts does not track applied schema versions (no schema_migrations "
            "table, no content hash, no skip-if-already-applied path); an earlier "
            "implementation was reverted. Remove this marker if it is re-implemented."
        ),
    )

    @_MIGRATE_TS_REVERTED
    def test_schema_migrations_table_in_migrate(self):
        """migrate.ts should reference schema_migrations table."""
        migrate_path = PROJECT_ROOT / "web" / "src" / "db" / "migrate.ts"
        content = migrate_path.read_text()
        assert "schema_migrations" in content

    @_MIGRATE_TS_REVERTED
    def test_hash_function_in_migrate(self):
        """migrate.ts should use SHA-256 hashing."""
        migrate_path = PROJECT_ROOT / "web" / "src" / "db" / "migrate.ts"
        content = migrate_path.read_text()
        assert "sha256" in content

    @_MIGRATE_TS_REVERTED
    def test_skip_when_already_applied(self):
        """migrate.ts should have fast-path skip logic."""
        migrate_path = PROJECT_ROOT / "web" / "src" / "db" / "migrate.ts"
        content = migrate_path.read_text()
        assert "isAlreadyApplied" in content
        assert "up to date" in content.lower() or "up-to-date" in content.lower() or "up to date" in content

    @_MIGRATE_TS_REVERTED
    def test_pre_migration_snapshot_exists(self):
        """migrate.ts should have pre-migration snapshot logic for production."""
        migrate_path = PROJECT_ROOT / "web" / "src" / "db" / "migrate.ts"
        content = migrate_path.read_text()
        assert "takePreMigrationSnapshot" in content
        assert "production" in content


# ===========================================================================
# Destructive DDL detection
# ===========================================================================

class TestDestructiveDDL:
    """Verify the DDL safety checker catches dangerous patterns."""

    def test_catches_drop_table(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("DROP TABLE customers;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 1
        assert violations[0][2] == "DROP TABLE"

    def test_catches_drop_column(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("ALTER TABLE builds DROP COLUMN app_name;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 1
        assert violations[0][2] == "DROP COLUMN"

    def test_catches_alter_column_type(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("ALTER TABLE builds ALTER COLUMN tier TYPE TEXT;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 1
        assert violations[0][2] == "ALTER COLUMN TYPE"

    def test_catches_truncate(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("TRUNCATE builds;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 1
        assert violations[0][2] == "TRUNCATE"

    def test_allows_safe_comment(self):
        """A -- SAFE: comment on the preceding line suppresses the violation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("-- SAFE: Replacing constraint with updated status list\n")
            f.write("ALTER TABLE builds DROP COLUMN old_field;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 0

    def test_allows_inline_safe_comment(self):
        """A trailing -- SAFE: comment on the same line suppresses the violation."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("DROP TABLE old_cache; -- SAFE: Cache table, no user data\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 0

    def test_ignores_drop_constraint(self):
        """DROP CONSTRAINT is not destructive (constraint replacement pattern)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("ALTER TABLE builds DROP CONSTRAINT IF EXISTS builds_status_check;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 0

    def test_do_block_content_is_checked(self):
        """A DO block is no place to hide: the statements in its body are read
        like any other, and what is found there is refused unless a SAFE
        comment stands above the statement or above the DO."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("DO $$\n")
            f.write("BEGIN\n")
            f.write("  ALTER TABLE foo DROP COLUMN bar;\n")
            f.write("END\n")
            f.write("$$;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert [(line, kind) for line, _, kind in violations] == [(3, "DROP COLUMN")]

    def test_do_block_content_is_acknowledged_above_the_block_or_above_the_statement(self):
        """The real schema.sql pattern: `-- SAFE:` above the DO. A comment
        directly above the statement inside the body does the same."""
        body = ("DO $$\nBEGIN\n  IF EXISTS (SELECT 1) THEN\n{inner}"
                "    ALTER TABLE beta_feedback DROP COLUMN old_col;\n  END IF;\nEND $$;\n")
        cases = {
            "-- SAFE: the column was never read\n" + body.format(inner=""): [],
            body.format(inner="    -- SAFE: the column was never read\n"): [],
            body.format(inner=""): [(4, "DROP COLUMN")],
            # a blank line between the comment and the DO: not "directly above"
            "-- SAFE: about something else\n\n" + body.format(inner=""): [(6, "DROP COLUMN")],
        }
        for text, expected in cases.items():
            with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
                f.write(text)
            violations = check_file(f.name)
            os.unlink(f.name)
            assert [(line, kind) for line, _, kind in violations] == expected, text

    def test_detects_ddl_after_do_block_ends(self):
        """DDL after a DO $$ block is NOT inside the block and should be caught."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("DO $$\n")
            f.write("BEGIN\n")
            f.write("  -- safe stuff\n")
            f.write("END $$;\n")
            f.write("DROP TABLE exposed;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 1
        assert violations[0][2] == "DROP TABLE"

    def test_current_schema_passes(self):
        """The actual schema.sql should pass (all DDL is safe/acknowledged)."""
        violations = check_file(str(SCHEMA_PATH))
        assert len(violations) == 0, f"schema.sql has unacknowledged destructive DDL: {violations}"

    def test_multiple_violations(self):
        """Multiple destructive statements are all reported."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("DROP TABLE customers;\n")
            f.write("DROP TABLE builds;\n")
            f.write("TRUNCATE email_log;\n")
            f.flush()
            violations = check_file(f.name)
        os.unlink(f.name)
        assert len(violations) == 3

    def test_a_file_that_cannot_be_read_is_an_error_not_a_pass(self, tmp_path):
        """A file that was not read is not a file without destructive DDL: an
        empty result for a missing file is what a gate would report as
        "nothing found"."""
        missing = tmp_path / "no-such-schema.sql"
        with pytest.raises(OSError):
            check_file(str(missing))
        result = subprocess.run(["python3", "scripts/check_destructive_ddl.py", str(missing)],
                                capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        assert result.returncode == 2
        assert "cannot check" in result.stderr and "No unacknowledged" not in result.stdout

    def test_cli_exit_code_on_violation(self):
        """CLI exits 1 when violations found."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("DROP TABLE x;\n")
            f.flush()
            result = subprocess.run(
                ["python3", "scripts/check_destructive_ddl.py", f.name],
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
        os.unlink(f.name)
        assert result.returncode == 1
        assert "DESTRUCTIVE DDL" in result.stdout

    def test_the_example_in_the_refusal_message_is_one_the_comment_really_settles(self, tmp_path):
        """The refusal prints an example of an acknowledged statement. It must
        be a statement this check flags and the comment settles, and not one of
        the statements the web suite's schema test refuses at top level with or
        without the comment (an acknowledged top-level DROP passes this hook
        and then fails the pre-push hook's web tests)."""
        import re
        from scripts.check_destructive_ddl import ACKNOWLEDGED_EXAMPLE
        comment, statement = ACKNOWLEDGED_EXAMPLE
        bare = tmp_path / "bare.sql"
        bare.write_text(statement + "\n")
        assert check_file(str(bare)), "the example is not a statement this check flags"
        acknowledged = tmp_path / "acknowledged.sql"
        acknowledged.write_text(f"{comment}\n{statement}\n")
        assert check_file(str(acknowledged)) == []
        refused_by_the_web_test = r"\bDROP\s+(TABLE|COLUMN|CONSTRAINT)\b|^\s*(UPDATE|DELETE|INSERT|TRUNCATE)\b"
        assert not re.search(refused_by_the_web_test, statement, re.IGNORECASE)
        result = subprocess.run(["python3", "scripts/check_destructive_ddl.py", str(bare)],
                                capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        assert result.returncode == 1
        assert statement in result.stdout and comment in result.stdout
        # the message points at the other gate, which must exist and hold that rule
        named = "web/tests/unit/schema.idempotency.test.ts"
        assert named in result.stdout
        web_test = (PROJECT_ROOT / named).read_text()
        assert "has no top-level DROP of a table, column or constraint" in web_test
        assert "has no top-level statement that changes or removes rows" in web_test

    def test_cli_exit_code_on_clean(self):
        """CLI exits 0 when no violations."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
            f.write("CREATE TABLE IF NOT EXISTS foo (id SERIAL PRIMARY KEY);\n")
            f.flush()
            result = subprocess.run(
                ["python3", "scripts/check_destructive_ddl.py", f.name],
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
            )
        os.unlink(f.name)
        assert result.returncode == 0


# ===========================================================================
# Drift detection
# ===========================================================================

class TestDriftDetection:
    """Verify schema comparison logic finds real divergences."""

    def test_identical_schemas_no_drift(self):
        schema = {
            "customers": {
                "id": {"type": "uuid", "nullable": "NO", "default": None, "max_length": None, "precision": None},
                "email": {"type": "character varying", "nullable": "NO", "default": None, "max_length": 255, "precision": None},
            }
        }
        drifts = compare_schemas(schema, schema, "env_a", "env_b")
        assert len(drifts) == 0

    def test_detects_missing_table(self):
        schema_a = {
            "customers": {"id": {"type": "uuid", "nullable": "NO", "default": None, "max_length": None, "precision": None}},
        }
        schema_b = {
            "customers": {"id": {"type": "uuid", "nullable": "NO", "default": None, "max_length": None, "precision": None}},
            "new_table": {"id": {"type": "uuid", "nullable": "NO", "default": None, "max_length": None, "precision": None}},
        }
        drifts = compare_schemas(schema_a, schema_b, "local", "prod")
        assert len(drifts) == 1
        assert drifts[0]["type"] == "table_missing"
        assert "new_table" in drifts[0]["detail"]

    def test_detects_missing_column(self):
        schema_a = {
            "builds": {
                "id": {"type": "uuid", "nullable": "NO", "default": None, "max_length": None, "precision": None},
            }
        }
        schema_b = {
            "builds": {
                "id": {"type": "uuid", "nullable": "NO", "default": None, "max_length": None, "precision": None},
                "new_col": {"type": "text", "nullable": "YES", "default": None, "max_length": None, "precision": None},
            }
        }
        drifts = compare_schemas(schema_a, schema_b, "local", "prod")
        assert len(drifts) == 1
        assert drifts[0]["type"] == "column_missing"
        assert "new_col" in drifts[0]["detail"]

    def test_detects_type_mismatch(self):
        col_a = {"type": "integer", "nullable": "NO", "default": None, "max_length": None, "precision": None}
        col_b = {"type": "text", "nullable": "NO", "default": None, "max_length": None, "precision": None}
        schema_a = {"t": {"col": col_a}}
        schema_b = {"t": {"col": col_b}}
        drifts = compare_schemas(schema_a, schema_b, "local", "prod")
        assert len(drifts) == 1
        assert drifts[0]["type"] == "type_mismatch"

    def test_detects_nullable_mismatch(self):
        col_a = {"type": "text", "nullable": "YES", "default": None, "max_length": None, "precision": None}
        col_b = {"type": "text", "nullable": "NO", "default": None, "max_length": None, "precision": None}
        schema_a = {"t": {"col": col_a}}
        schema_b = {"t": {"col": col_b}}
        drifts = compare_schemas(schema_a, schema_b, "local", "prod")
        assert len(drifts) == 1
        assert drifts[0]["type"] == "nullable_mismatch"

    def test_multiple_drifts_all_reported(self):
        schema_a = {
            "t1": {"a": {"type": "int", "nullable": "NO", "default": None, "max_length": None, "precision": None}},
        }
        schema_b = {
            "t1": {"a": {"type": "text", "nullable": "YES", "default": None, "max_length": None, "precision": None}},
            "t2": {"b": {"type": "int", "nullable": "NO", "default": None, "max_length": None, "precision": None}},
        }
        drifts = compare_schemas(schema_a, schema_b, "local", "prod")
        # type mismatch + nullable mismatch on t1.a + table_missing for t2
        assert len(drifts) == 3

    def test_empty_schemas_no_drift(self):
        drifts = compare_schemas({}, {}, "a", "b")
        assert len(drifts) == 0

    def test_symmetric_table_missing(self):
        """Missing table is reported from both directions."""
        schema_a = {"only_in_a": {"col": {"type": "int", "nullable": "NO", "default": None, "max_length": None, "precision": None}}}
        schema_b = {"only_in_b": {"col": {"type": "int", "nullable": "NO", "default": None, "max_length": None, "precision": None}}}
        drifts = compare_schemas(schema_a, schema_b, "local", "prod")
        assert len(drifts) == 2
        details = [d["detail"] for d in drifts]
        assert any("only_in_a" in d and "prod" in d for d in details)
        assert any("only_in_b" in d and "local" in d for d in details)


# ===========================================================================
# Pre-commit hook
# ===========================================================================

class TestPreCommitHook:
    """Verify the pre-commit hook exists and is executable."""

    def test_hook_exists(self):
        hook = PROJECT_ROOT / ".githooks" / "pre-commit"
        assert hook.exists()

    def test_hook_is_executable(self):
        hook = PROJECT_ROOT / ".githooks" / "pre-commit"
        assert os.access(hook, os.X_OK)

    def test_hook_references_ddl_checker(self):
        hook = PROJECT_ROOT / ".githooks" / "pre-commit"
        content = hook.read_text()
        assert "check_destructive_ddl" in content
        assert "schema.sql" in content


# ===========================================================================
# WATCHDOG1 Layer 2: supervisor_retry_used column (section-03)
# ===========================================================================

class TestSupervisorRetryUsedColumn:
    """WATCHDOG1 Layer 2 schema migration: supervisor_retry_used column.

    The supervisor_retry_used column is the authoritative marker for
    "the watchdog supervisor has already force-failed this build once
    and given it its one free retry." It is read by queue/worker_pg.py
    _supervisor_force_fail() and _has_prior_timeout_failure() (both
    described in docs/internals/worker.md).
    """

    def test_schema_has_supervisor_retry_used_column(self):
        """schema.sql must declare the supervisor_retry_used column on
        the builds table with BOOLEAN DEFAULT FALSE."""
        text = SCHEMA_PATH.read_text()
        normalized = " ".join(text.split())
        assert "ALTER TABLE builds" in normalized, (
            "schema.sql must use ALTER TABLE builds for additive migrations"
        )
        assert "supervisor_retry_used BOOLEAN DEFAULT FALSE" in normalized, (
            "supervisor_retry_used must be declared as BOOLEAN DEFAULT FALSE "
            "so existing rows read as 'has NOT used its free retry yet'"
        )

    def test_supervisor_retry_used_uses_if_not_exists(self):
        """The ALTER must use ADD COLUMN IF NOT EXISTS so the runtime
        migrator can re-run schema.sql on every boot without error."""
        text = SCHEMA_PATH.read_text()
        # Find the supervisor_retry_used column declaration
        idx = text.index("supervisor_retry_used")
        # Walk backward to the start of its enclosing ALTER TABLE statement
        alter_start = text.rfind("ALTER TABLE", 0, idx)
        assert alter_start != -1, (
            "supervisor_retry_used must live inside an ALTER TABLE statement"
        )
        block = text[alter_start : idx + len("supervisor_retry_used")]
        assert "ADD COLUMN IF NOT EXISTS" in block, (
            "ALTER TABLE block for supervisor_retry_used must use "
            "ADD COLUMN IF NOT EXISTS for idempotent re-runs"
        )
