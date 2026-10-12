# ops/scanner.py - Failure pattern detection from the builds table
#
# Queries the builds table for recent failures, groups them by
# normalized error signature, and classifies each pattern into
# a category the ops agent can act on.

import logging
import re
from collections import defaultdict

logger = logging.getLogger("ops.scanner")

# Error classification patterns. Order matters: first match wins.
# Each key is a category, each value is a list of regex patterns
# (case-insensitive) that match error messages in that category.
_CLASSIFICATION_PATTERNS = {
    "version_mismatch": [
        r"node[.\s].*version",
        r"engine.*incompatible",
        r"unsupported.*node",
        r"python.*version",
        r"requires.*node",
        r"playwright.*updated",
        r"executable.*doesn't exist.*playwright",
        r"please update docker image",
    ],
    "missing_dep": [
        r"cannot find module",
        r"module not found",
        r"no module named",
        r"no such package",
        r"enoent.*node_modules",
        r"peer dep",
        r"missing.*dependency",
    ],
    "dockerfile": [
        r"dockerfile",
        r"docker build",
        r"base image",
        r"apt-get.*failed",
        r"pip install.*failed",
    ],
    "env_config": [
        r"database_url.*not set",
        r"missing.*env",
        r"env.*undefined",
        r"connection.*refused.*5432",
        r"railway.*env",
    ],
    # The playbook for this category redeploys the worker service
    # (ops/playbooks/railway_redeploy.py), so these patterns are whole words
    # and specific phrases. A bare "oom" would also match "room" and "zoom",
    # and a bare "killed" would match "skilled" and the admin kill route's own
    # "Force-killed by admin".
    "railway_crash": [
        r"service.*crashed",
        r"\boom\b",
        r"\boomkilled\b",
        r"signal 9",
        r"\bkilled by signal\b",
        r"\bprocess (?:was )?killed\b",
        r"(?m)^\s*killed\s*$",
        r"enospc",
        r"enomem",
    ],
    "missing_tool": [
        r"errno 2.*no such file",
        r"no such file or directory.*dropdb",
        r"no such file or directory.*createdb",
        r"no such file or directory.*psql",
        r"command not found",
    ],
    # app_logic must come last: it is a catch-all for code errors
    "app_logic": [
        r"syntaxerror",
        r"typeerror",
        r"referenceerror",
        r"assertion.*failed",
        r"test.*suite.*failed",
        r"spec.*validation.*failed",
    ],
}


# Failures that repeat exactly when the same spec is built again: the v2
# engine's own tag, and a spec that validation refuses (the compose gate and
# the v1 validator both raise "Spec validation failed ..."). Building the same
# input again cannot succeed and, after a deploy, leaves a project behind each
# time. Two things ask this question and must get one answer: the worker's
# retry decision (queue/worker_pg.py) and the coordinator's automatic rebuilds
# (coordinator/actuators.py queue_rebuild, coordinator/work_detection.py).
DETERMINISTIC_FAILURE_MARKERS = ("[v2-deterministic]", "Spec validation failed")


def is_deterministic_failure(error_message) -> bool:
    """True when the error says the same input will fail the same way."""
    text = error_message or ""
    return any(marker in text for marker in DETERMINISTIC_FAILURE_MARKERS)


def classify_error(error_message: str) -> str:
    """Classify an error message into a category.

    Returns one of the category keys from _CLASSIFICATION_PATTERNS,
    or 'unknown' if no pattern matches.
    """
    if not error_message:
        return "unknown"
    lower = error_message.lower()
    for category, patterns in _CLASSIFICATION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return category
    return "unknown"


def _normalize_error(error_message: str) -> str:
    """Strip variable parts (UUIDs, timestamps, paths) to get a stable signature."""
    if not error_message:
        return ""
    normalized = error_message
    # Strip UUIDs
    normalized = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "<UUID>",
        normalized,
    )
    # Strip timestamps (ISO format)
    normalized = re.sub(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}",
        "<TIMESTAMP>",
        normalized,
    )
    # Strip absolute paths
    normalized = re.sub(r"/[^\s:]+/", "/<PATH>/", normalized)
    # Strip port numbers after localhost/127.0.0.1
    normalized = re.sub(r"(localhost|127\.0\.0\.1):\d+", r"\1:<PORT>", normalized)
    return normalized.strip()


def scan_recent_failures(
    conn,
    window_hours: int = 6,
    min_occurrences: int = 2,
) -> list:
    """Find error patterns that appear across multiple builds.

    Args:
        conn: psycopg2 connection to the builds database.
        window_hours: How far back to look for failures.
        min_occurrences: Minimum times a pattern must appear to be reported.

    Returns:
        List of dicts: {pattern, category, count, build_ids, sample_error,
                        first_seen, last_seen}
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, error_message, app_name, created_at
            FROM builds
            WHERE status = 'failed'
              AND completed_at > NOW() - interval '%s hours'
              AND retry_count >= 2
            ORDER BY created_at DESC
            """,
            (window_hours,),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    # Group by normalized error signature
    groups = defaultdict(list)
    for row in rows:
        build_id, error_message, app_name, created_at = row
        if not error_message:
            continue
        signature = _normalize_error(error_message)
        groups[signature].append({
            "build_id": str(build_id),
            "error_message": error_message,
            "app_name": app_name,
            "created_at": created_at,
        })

    # Filter by min_occurrences and build result list
    results = []
    for signature, entries in groups.items():
        if len(entries) < min_occurrences:
            continue
        sample = entries[0]["error_message"]
        category = classify_error(sample)
        results.append({
            "pattern": signature,
            "category": category,
            "count": len(entries),
            "build_ids": [e["build_id"] for e in entries],
            "sample_error": sample,
            "first_seen": str(entries[-1]["created_at"]),
            "last_seen": str(entries[0]["created_at"]),
        })

    logger.info(
        "Scanned %d failures, found %d patterns (min_occurrences=%d)",
        len(rows), len(results), min_occurrences,
    )
    return results
