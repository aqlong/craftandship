"""Regression test for _ensure_admin_model_handlers requireRole import.

`_ensure_admin_model_handlers` injects handlers like
`router.get('/slug', requireAuth, requireRole('admin'), ...)`, but the
dashboard.js the model wrote typically imports only `requireAuth` and
`requirePasswordSet` from middleware/auth.js, not `requireRole`. The result
was a dashboard.js that crashed at require time with
`ReferenceError: requireRole is not defined`: the server failed to boot and
every admin page answered 500.

Fix: when _ensure_admin_model_handlers injects handlers, also add
`requireRole` to the middleware/auth destructured import line (or
prepend a fresh import if none exists).
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


MAROON_SPEC = {
    "app_name": "test-app-v1",
    "settings": {"site_name": "Test App"},
    "data_models": [{"name": "BlogPost", "fields": []}],
}


def test_injects_require_role_into_existing_auth_import():
    dash = (
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "const { requireAuth, requirePasswordSet } = require('../middleware/auth');\n"
        "const pool = require('../db');\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    got = out["routes/dashboard.js"]
    assert "requireRole" in got
    # The existing import line should now include requireRole alongside
    # the originals (tolerant of extra whitespace inside the braces).
    import re as _re
    assert _re.search(
        r"\{\s*requireAuth,\s*requirePasswordSet,\s*requireRole\s*\}",
        got,
    ), f"requireRole not cleanly appended; got:\n{got[:300]}"
    assert "require('../middleware/auth')" in got


def test_requireRole_already_present_is_no_op():
    """If requireRole is ALREADY in the destructured import, don't
    duplicate it."""
    dash = (
        "const { requireAuth, requireRole } = require('../middleware/auth');\n"
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "const pool = require('../db');\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    got = out["routes/dashboard.js"]
    # Only one requireRole in the import line.
    import_lines = [l for l in got.splitlines() if "middleware/auth" in l]
    assert len(import_lines) == 1
    assert import_lines[0].count("requireRole") == 1


def test_double_quoted_existing_auth_import_not_duplicated():
    """Claude may use double-quoted require paths. The safety-net
    `require('../middleware/auth')` presence check must match both
    single and double quotes so we don't fabricate a duplicate
    require alongside a non-destructured one."""
    dash = (
        'const auth = require("../middleware/auth");\n'  # double-quoted, non-destructured
        "const router = require('express').Router();\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    got = out["routes/dashboard.js"]
    # Only ONE auth require: no duplicate prepended.
    assert got.count("require('../middleware/auth')") + got.count('require("../middleware/auth")') == 1


def test_injects_into_multiline_destructured_import():
    """Claude sometimes emits the destructured import spread across
    multiple lines for readability. The regex tolerates newlines
    inside the `{...}` braces via `[^}]`."""
    dash = (
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "const {\n"
        "  requireAuth,\n"
        "  requirePasswordSet\n"
        "} = require('../middleware/auth');\n"
        "const pool = require('../db');\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    got = out["routes/dashboard.js"]
    assert "requireRole" in got
    # Import still resolves syntactically (the flattened result must
    # parse: `{requireAuth, requirePasswordSet, requireRole}`).
    import re as _re
    assert _re.search(
        r"\{\s*requireAuth\s*,\s*requirePasswordSet\s*,\s*requireRole\s*\}",
        got, _re.DOTALL,
    ), f"multi-line import not flattened cleanly; got:\n{got[:400]}"


def test_injects_into_destructured_import_with_trailing_comma():
    """JS destructuring allows trailing commas: `const { x, }`. The
    rstrip(',') call should clean that up before appending the new
    name, so we don't end up with `{ x, , requireRole }`."""
    dash = (
        "const { requireAuth, } = require('../middleware/auth');\n"
        "const router = require('express').Router();\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    got = out["routes/dashboard.js"]
    # No double commas.
    assert ", ," not in got
    # requireRole is present.
    assert "requireRole" in got


def test_injects_into_single_binding_destructured_import():
    """Edge case: Claude imports only `requireAuth` as a single
    binding. The regex should still catch it + append requireRole."""
    dash = (
        "const { requireAuth } = require('../middleware/auth');\n"
        "const router = require('express').Router();\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    got = out["routes/dashboard.js"]
    import re as _re
    assert _re.search(
        r"\{\s*requireAuth\s*,\s*requireRole\s*\}",
        got,
    ), f"single-binding import not extended; got:\n{got[:300]}"


def test_injected_dashboard_parses_as_js():
    """End-to-end: the full injected file loads without ReferenceError.
    Runs `node -c` when available, real proof the fix works."""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node is not on PATH")
    dash = (
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "const { requireAuth, requirePasswordSet } = require('../middleware/auth');\n"
        "const pool = require('../db');\n"
        "module.exports = router;\n"
    )
    out = dict(build._ensure_admin_model_handlers(
        [("routes/dashboard.js", dash)], MAROON_SPEC,
    ))
    # Write to a temp file and run `node -c` for a real syntax check.
    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False
    ) as tf:
        tf.write(out["routes/dashboard.js"])
        tf.flush()
        result = subprocess.run(
            ["node", "-c", tf.name],
            capture_output=True, text=True, timeout=10,
        )
    assert result.returncode == 0, (
        f"node -c FAILED: {result.stderr[:500]}"
    )
    # Confirm the injected handler uses requireRole as intended.
    assert "requireRole('admin')" in out["routes/dashboard.js"]
