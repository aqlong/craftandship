# tests/test_ops_audit.py - Tests for ops agent audit log (PostgreSQL)

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ops.audit as audit_module

_CREATE_TABLE = """
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
)
"""


@pytest.fixture(autouse=True)
def setup_audit_db(scratch_pg_url):
    """Create the table in the session's scratch database (tests/conftest.py)
    and start every test with it empty. The module skips when no local
    PostgreSQL is reachable."""
    import psycopg2

    def _test_conn():
        return psycopg2.connect(scratch_pg_url)

    conn = _test_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            cur.execute("DELETE FROM ops_actions")
        conn.commit()
    finally:
        conn.close()

    with patch.object(audit_module, "_get_conn", _test_conn):
        yield _test_conn


def _logged_rows(connect) -> list:
    """Every ops_actions row, newest first, read straight from the table."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action_type, description, build_ids, metadata FROM ops_actions "
                        "ORDER BY created_at DESC")
            return [dict(zip(("action_type", "description", "build_ids", "metadata"), row))
                    for row in cur.fetchall()]
    finally:
        conn.close()


class TestAuditLog:
    """Test audit log CRUD operations."""

    def test_log_action_returns_id(self):
        action_id = audit_module.log_action(
            "fix_applied",
            "Added postgresql-client to Dockerfile",
            category="missing_dep",
        )
        assert action_id is not None
        assert len(action_id) == 36  # UUID format

    def test_a_logged_action_is_a_row_in_the_table(self, setup_audit_db):
        audit_module.log_action("fix_applied", "Test fix", category="test")
        actions = _logged_rows(setup_audit_db)
        assert len(actions) == 1
        assert actions[0]["action_type"] == "fix_applied"
        assert actions[0]["description"] == "Test fix"

    def test_count_actions(self):
        audit_module.log_action("fix_applied", "Fix 1")
        audit_module.log_action("fix_applied", "Fix 2")
        audit_module.log_action("scan", "Scan 1")

        count = audit_module.count_actions(hours=1, action_type="fix_applied")
        assert count == 2

    def test_count_excludes_dry_run(self):
        audit_module.log_action("fix_applied", "Real fix", dry_run=False)
        audit_module.log_action("fix_applied", "Dry run fix", dry_run=True)

        count = audit_module.count_actions(hours=1, action_type="fix_applied")
        assert count == 1  # Only the real fix

    def test_log_with_build_ids(self, setup_audit_db):
        audit_module.log_action(
            "fix_applied",
            "Multi-build fix",
            build_ids=["id1", "id2", "id3"],
        )
        actions = _logged_rows(setup_audit_db)
        assert len(actions) == 1
        # PostgreSQL JSONB returns as Python list (psycopg2 auto-decodes)
        build_ids = actions[0]["build_ids"]
        if isinstance(build_ids, str):
            import json
            build_ids = json.loads(build_ids)
        assert build_ids == ["id1", "id2", "id3"]

    def test_log_with_metadata(self, setup_audit_db):
        audit_module.log_action(
            "fix_applied",
            "Fix with metadata",
            metadata={"files_changed": ["Dockerfile.worker"]},
        )
        actions = _logged_rows(setup_audit_db)
        assert len(actions) == 1
        metadata = actions[0]["metadata"]
        if isinstance(metadata, str):
            import json
            metadata = json.loads(metadata)
        assert "Dockerfile.worker" in metadata["files_changed"]

    def test_count_claude_calls(self):
        audit_module.log_action("claude_call", "Analysis call 1")
        audit_module.log_action("claude_call", "Analysis call 2")
        count = audit_module.count_claude_calls(hours=24)
        assert count == 2
