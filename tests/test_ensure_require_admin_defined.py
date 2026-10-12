"""Regression tests for _ensure_require_admin_defined post-processor.

Recurring issue across v20-v24: `_ensure_admin_slug_mount` emits
`app.use('/admin/<slug>', requireAdmin, ...)` lines in server.js but
Claude's server.js template does NOT always define the `requireAdmin`
middleware. Result: ReferenceError on first request, test phase catches
it, self-correction loop spends a Claude call to synthesize the fix.

Pipeline-level fix (standard widget): detect the reference-without-
definition pattern and inject a canonical middleware body. Eliminates
the recurring class of bug at build time instead of at test time.
"""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

import pipeline  # noqa: E402
import build  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_processor_is_registered():
    """@processor registers _ensure_require_admin_defined in REGISTRY."""
    assert "_ensure_require_admin_defined" in pipeline.REGISTRY


def test_processor_in_expected_processors():
    """EXPECTED_PROCESSORS guardrail enumerates the new processor."""
    assert "_ensure_require_admin_defined" in pipeline.EXPECTED_PROCESSORS


def test_processor_runs_after_ensure_admin_slug_mount():
    """Must run AFTER _ensure_admin_slug_mount because that processor is
    the one that emits `app.use('/admin/<slug>', requireAdmin, ...)`
    references we're injecting the definition to back."""
    proc = pipeline.REGISTRY["_ensure_require_admin_defined"]
    assert "_ensure_admin_slug_mount" in proc.after


def test_processor_runs_in_both_phases():
    """Build phase: new v22+ app gets the injection. Reapply phase:
    older v19 snapshot reaped via reapply also gets the injection."""
    proc = pipeline.REGISTRY["_ensure_require_admin_defined"]
    assert "build" in proc.phase
    assert "reapply" in proc.phase


# ---------------------------------------------------------------------------
# Rewrite behavior
# ---------------------------------------------------------------------------


def _mk_artifact(files: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        files=list(files.items()),
        spec={"app_name": "test", "data_models": []},
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )


def test_injects_requireadmin_when_referenced_but_undefined():
    """The v20-v24 recurring bug: server.js has
    `app.use('/admin/events', requireAdmin, eventsRouter);` but no
    `function requireAdmin` or import. Fix must inject the definition."""
    server_js = (
        "const express = require('express');\n"
        "const app = express();\n"
        "const eventsRouter = require('./routes/events');\n"
        "app.use('/events', eventsRouter);\n"
        "app.use('/admin/events', requireAdmin, eventsRouter);\n"
        "app.listen(3000);\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]

    assert "function requireAdmin(req, res, next)" in result, (
        "requireAdmin definition not injected"
    )
    # Injection lands AFTER the require block, so it's in scope when
    # app.use references it.
    def_pos = result.find("function requireAdmin")
    use_pos = result.find("app.use('/admin/events'")
    assert 0 <= def_pos < use_pos, (
        f"definition at {def_pos} must precede use at {use_pos}"
    )


def test_skips_when_already_defined_as_function():
    """Idempotent: server.js that already has `function requireAdmin`
    is untouched (no duplicate injection)."""
    server_js = (
        "const express = require('express');\n"
        "function requireAdmin(req, res, next) { next(); }\n"
        "const app = express();\n"
        "app.use('/admin/events', requireAdmin, require('./routes/events'));\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]
    assert result.count("function requireAdmin") == 1, (
        "duplicate requireAdmin definition injected"
    )


def test_skips_when_defined_as_const_arrow():
    """Const arrow form (`const requireAdmin = (req, res, next) => {...}`)
    also counts as defined."""
    server_js = (
        "const express = require('express');\n"
        "const requireAdmin = (req, res, next) => { next(); };\n"
        "const app = express();\n"
        "app.use('/admin/x', requireAdmin, (req, res) => res.send('ok'));\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]
    assert "function requireAdmin" not in result, (
        "injected function def over existing const arrow"
    )


def test_skips_when_imported_from_middleware_module():
    """Some apps destructure requireAdmin from a middleware module:
    `const { requireAdmin } = require('./middleware/auth')`. Must not
    inject a duplicate."""
    server_js = (
        "const express = require('express');\n"
        "const { requireAuth, requireAdmin } = require('./middleware/auth');\n"
        "const app = express();\n"
        "app.use('/admin/x', requireAdmin, (req, res) => res.send('ok'));\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]
    assert result.count("function requireAdmin") == 0
    # The original import is preserved
    assert "require('./middleware/auth')" in result


def test_noop_when_no_references():
    """If server.js never mentions requireAdmin, no injection.
    Apps without admin surfaces stay lean."""
    server_js = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/', (req, res) => res.send('hi'));\n"
        "app.listen(3000);\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]
    assert "requireAdmin" not in result


def test_noop_when_server_js_absent():
    """No crash if server.js isn't in the artifact."""
    artifact = _mk_artifact({"package.json": "{}"})
    # Should not raise
    build._ensure_require_admin_defined(artifact)


def test_idempotent_on_second_pass():
    """Running the processor twice on the same artifact produces the
    same output (i.e. after injection, second call is a no-op)."""
    server_js = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use('/admin/events', requireAdmin, require('./routes/events'));\n"
    )
    artifact1 = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact1)
    first = dict(artifact1.files)["server.js"]

    artifact2 = _mk_artifact({"server.js": first})
    build._ensure_require_admin_defined(artifact2)
    second = dict(artifact2.files)["server.js"]

    assert first == second, "second pass produced different output"


def test_injected_middleware_redirects_unauth_to_login():
    """Sanity: the injected middleware body redirects to /login when
    the session is missing, returns 403 for non-admin, and calls
    next() for admin. These behaviors must be present verbatim (the
    test phase relies on them)."""
    server_js = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use('/admin/events', requireAdmin, require('./routes/events'));\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]
    assert "res.redirect('/login')" in result
    assert "res.status(403)" in result
    assert "next()" in result


def test_injection_lands_after_require_block():
    """Injection site: after the last `const X = require(...)` line in
    the initial require block. Keeps requireAdmin in scope by the time
    app.use references it."""
    server_js = (
        "const express = require('express');\n"
        "const session = require('express-session');\n"
        "const bodyParser = require('body-parser');\n"
        "\n"
        "const app = express();\n"
        "app.use('/admin/events', requireAdmin, require('./routes/events'));\n"
    )
    artifact = _mk_artifact({"server.js": server_js})
    build._ensure_require_admin_defined(artifact)
    result = dict(artifact.files)["server.js"]
    # Definition lies BETWEEN the last require and the const app = line
    def_pos = result.find("function requireAdmin")
    last_require_pos = result.rfind("body-parser")
    app_const_pos = result.find("const app = express()")
    assert last_require_pos < def_pos < app_const_pos, (
        f"require={last_require_pos} def={def_pos} app={app_const_pos}"
    )
