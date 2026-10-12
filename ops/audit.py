# ops/audit.py - PostgreSQL audit log for ops agent actions
#
# Records every action the ops agent takes (fixes applied, skipped,
# circuit breaker triggers, escalations, fixer runs) for
# accountability and guardrail enforcement.
#
# Migrated from SQLite (ephemeral on Railway) to PostgreSQL for
# persistence across redeploys.

import json
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def _get_conn():
    """Get a psycopg2 connection using DATABASE_URL."""
    import psycopg2
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db_url)


def log_action(
    action_type: str,
    description: str,
    *,
    category: Optional[str] = None,
    build_ids: Optional[list] = None,
    commit_sha: Optional[str] = None,
    dry_run: bool = False,
    success: bool = True,
    metadata: Optional[dict] = None,
) -> str:
    """Record an ops agent action. Returns the action ID."""
    action_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops_actions
                    (id, created_at, action_type, category, description,
                     build_ids, commit_sha, dry_run, success, metadata)
                VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    action_id,
                    action_type,
                    category,
                    description,
                    json.dumps(build_ids) if build_ids else None,
                    commit_sha,
                    dry_run,
                    success,
                    json.dumps(metadata) if metadata else None,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return action_id


def count_actions(hours: int = 1, action_type: str = "fix_applied") -> int:
    """Count actions of a given type within the time window."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM ops_actions
                WHERE action_type = %s
                  AND created_at > NOW() - INTERVAL '%s hours'
                  AND dry_run = FALSE
                """,
                (action_type, hours),
            )
            row = cur.fetchone()
            return row[0] if row else 0
    finally:
        conn.close()


def count_claude_calls(hours: int = 24) -> int:
    """Count fixer runs ("claude_call" rows, a historical name) in the window."""
    return count_actions(hours=hours, action_type="claude_call")
