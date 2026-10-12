"""Tests for cli/templates/db_migrate.js seed-splitter logic.

Regression guards for the seed-splitter bug:
the legacy `.split(/;\\s*\\n/).filter(!startsWith('--'))` pipeline
silently drops any seed statement that has a leading `-- comment` in
its split chunk. On a fresh DB, that skips ~40% of INSERTs, leaving
`users` empty and magic-link login impossible.

Tests exercise the splitter behavior directly via a Node subprocess
(the ACTUAL runtime-shipping logic), not a Python port.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "cli" / "templates" / "db_migrate.js"


def _run_splitter(seed_sql: str) -> list[str]:
    """Spawn node, apply the same split+filter logic the template uses on
    `seed_sql`, and return the resulting statements.

    The test reads the TEMPLATE file and extracts the splitter snippet by
    string-anchors, then runs it in a Node `-e` harness. If the template
    changes shape, the test anchors catch it.
    """
    tmpl = TEMPLATE.read_text()

    # Find the split snippet: from `const statements = seed` through the
    # closing `.filter(...)`.
    start = tmpl.index("const statements = seed")
    end = tmpl.index(";\n", start) + 1
    splitter = tmpl[start:end]

    harness = f"""
const seed = {json.dumps(seed_sql)};
{splitter}
console.log(JSON.stringify(statements));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node splitter failed: {result.stderr}")
    return json.loads(result.stdout.strip())


def _node_available() -> bool:
    """Smoke-test that node is available (CI may lack it)."""
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# A list, not a second `pytestmark =` assignment: that would silently drop the
# v1_only mark set at the top of this file.
pytestmark = [
    pytest.mark.v1_only,
    pytest.mark.skipif(
        not _node_available(),
        reason="node binary required to exercise the template's JS splitter",
    ),
]


def test_splitter_preserves_comment_led_insert():
    """Regression: `-- comment\\nINSERT ... ;` was silently dropped by the
    legacy `.filter(s => !s.startsWith('--'))` pattern because the split
    keeps the `-- comment` attached to the NEXT chunk. Fix must preserve
    the INSERT."""
    seed = (
        "-- Admin user (magic link auth, no password)\n"
        "INSERT INTO users (name, email) VALUES ('Ocean', 'ocean@example.com');\n"
        "-- Visitor user\n"
        "INSERT INTO users (name, email) VALUES ('Sarah', 'sarah@example.com');\n"
    )
    statements = _run_splitter(seed)
    inserts = [s for s in statements if "INSERT INTO users" in s]
    assert len(inserts) == 2, (
        f"expected 2 user INSERTs to survive split+filter, got {len(inserts)}: "
        f"{[s[:80] for s in statements]}"
    )


def test_splitter_preserves_both_ocean_and_sarah_inserts():
    """Positive: admin AND visitor survive the splitter even though both
    are preceded by `--` comment lines."""
    seed = (
        "-- Admin user\n"
        "INSERT INTO users (name, role) VALUES ('Ocean', 'admin');\n"
        "-- Visitor user\n"
        "INSERT INTO users (name, role) VALUES ('Sarah', 'visitor');\n"
    )
    statements = _run_splitter(seed)
    has_ocean = any("'Ocean'" in s for s in statements)
    has_sarah = any("'Sarah'" in s for s in statements)
    assert has_ocean, f"Ocean INSERT dropped: {statements}"
    assert has_sarah, f"Sarah INSERT dropped: {statements}"


def test_splitter_drops_pure_comment_only_chunks():
    """Negative: a chunk that is ONLY comments (no SQL) should still be
    dropped, the filter must distinguish comment-only from comment+SQL."""
    seed = (
        "-- A standalone comment with no following SQL;\n"
        "-- Another standalone comment;\n"
        "INSERT INTO users (name) VALUES ('Ocean');\n"
    )
    statements = _run_splitter(seed)
    inserts = [s for s in statements if "INSERT" in s]
    assert len(inserts) == 1, f"expected exactly 1 INSERT, got {inserts}"
    # No pure-comment statements smuggled through
    for s in statements:
        non_comment = "\n".join(
            l for l in s.split("\n") if not l.strip().startswith("--")
        ).strip()
        assert non_comment, (
            f"smuggled a comment-only chunk through splitter: {s!r}"
        )


def test_splitter_preserves_statements_with_inline_apostrophes():
    """Negative-negative: seed lines with escaped apostrophes (e.g.
    `Ocean''s live vocal channeling`) must survive the regex split
    unharmed."""
    seed = (
        "-- Site settings\n"
        "INSERT INTO site_settings (key, value) VALUES ('about_body', "
        "E'Ocean''s live vocal channeling brings you home.\\nMultiple paragraphs.') "
        "ON CONFLICT DO NOTHING;\n"
    )
    statements = _run_splitter(seed)
    inserts = [s for s in statements if "INSERT INTO site_settings" in s]
    assert len(inserts) == 1, f"multi-line apostrophe INSERT split or dropped: {statements}"


def test_splitter_preserves_mw_canonical_seed_shape():
    """Integration-ish: the shape of a canonical MW seed.sql, including
    the admin+visitor+site_settings header pattern, survives end-to-end.
    Counts every INSERT in and out; no drops."""
    seed = (
        "-- Admin user (magic link auth, no password)\n"
        "INSERT INTO users (name, email, role) VALUES ('Ocean', 'a@b.c', 'admin') ON CONFLICT DO NOTHING;\n"
        "-- Visitor user\n"
        "INSERT INTO users (name, email, role) VALUES ('Sarah', 'v@b.c', 'visitor') ON CONFLICT DO NOTHING;\n"
        "-- Site settings\n"
        "INSERT INTO site_settings (key, value) VALUES ('site_name', 'MW') ON CONFLICT DO NOTHING;\n"
        "INSERT INTO site_settings (key, value) VALUES ('hero_tagline', 'Somatic sound') ON CONFLICT DO NOTHING;\n"
        "-- Events\n"
        "INSERT INTO events (title, date_time, created_by) VALUES ('Sacred Oceans', NOW(), 1) ON CONFLICT DO NOTHING;\n"
        "-- Photos\n"
        "INSERT INTO photos (image_url, created_by) VALUES ('/a.jpg', 1) ON CONFLICT DO NOTHING;\n"
    )
    expected_inserts = seed.count("INSERT INTO")
    statements = _run_splitter(seed)
    actual_inserts = sum(1 for s in statements if "INSERT INTO" in s)
    assert actual_inserts == expected_inserts, (
        f"splitter lost {expected_inserts - actual_inserts} INSERTs out of "
        f"{expected_inserts}; surviving statements: "
        f"{[s.split(chr(10))[0][:80] for s in statements]}"
    )
