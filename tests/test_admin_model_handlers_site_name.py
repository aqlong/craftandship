"""Tests for _ensure_admin_model_handlers site-name derivation.

Before this fix, cli/build.py hardcoded "Manage {Name} | Medicine Waves"
into every admin dashboard handler, leaking the Medicine Waves brand into
non-MW customer apps. The fix derives the title suffix from spec.settings.
site_name / business_name / pretty-cased app_name via a shared helper.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


# ---------------------------------------------------------------------------
# _derive_site_name_from_spec: pure helper, exhaustive unit cases
# ---------------------------------------------------------------------------


def test_derive_site_name_prefers_settings_site_name():
    spec = {
        "app_name": "sample-practitioner-v1-123456",
        "settings": {"site_name": "Sample Practitioner"},
    }
    assert build._derive_site_name_from_spec(spec) == "Sample Practitioner"


def test_derive_site_name_falls_back_to_business_name():
    spec = {
        "app_name": "sample-practitioner-v1-123456",
        "settings": {"business_name": "Sample Practitioner Studio"},
    }
    assert build._derive_site_name_from_spec(spec) == "Sample Practitioner Studio"


def test_derive_site_name_falls_back_to_app_name_pretty():
    """No settings; strip -vN-NNNN version suffix; capitalize each word."""
    spec = {"app_name": "sample-practitioner-v1-123456"}
    assert build._derive_site_name_from_spec(spec) == "Sample Practitioner"


def test_derive_site_name_handles_snake_case_app_name():
    spec = {"app_name": "small_business_invoicer"}
    assert build._derive_site_name_from_spec(spec) == "Small Business Invoicer"


def test_derive_site_name_fallback_site():
    """Empty spec → 'Site' (non-empty fallback)."""
    assert build._derive_site_name_from_spec({}) == "Site"
    assert build._derive_site_name_from_spec({"app_name": ""}) == "Site"


def test_derive_site_name_ignores_non_dict_settings():
    """Tolerate None or junk settings without crashing."""
    spec = {"app_name": "foo-bar", "settings": None}
    assert build._derive_site_name_from_spec(spec) == "Foo Bar"


# ---------------------------------------------------------------------------
# End-to-end: _ensure_admin_model_handlers injects a non-MW title
# ---------------------------------------------------------------------------


def _minimal_files_with_dash_and_models() -> list:
    """Claude-like dashboard.js that is MISSING the /admin/events handler.
    Forces _ensure_admin_model_handlers to inject one we can inspect."""
    return [
        (
            "routes/dashboard.js",
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "module.exports = router;\n",
        ),
    ]


def test_injected_handler_uses_spec_site_name_not_medicine_waves():
    """Regression guard: a non-MW spec must NOT get 'Medicine Waves' in
    its injected admin handler titles."""
    spec = {
        "app_name": "sample-practitioner-v1-123456",
        "settings": {"site_name": "Sample Practitioner"},
        "data_models": [
            {"name": "Testimonial", "fields": []},
            {"name": "BlogPost", "fields": []},
            {"name": "Event", "fields": []},
        ],
    }
    files = _minimal_files_with_dash_and_models()
    out = build._ensure_admin_model_handlers(files, spec)
    dash = dict(out)["routes/dashboard.js"]
    assert "Medicine Waves" not in dash, (
        "Medicine Waves brand leaked into injected admin handlers for a "
        "non-MW spec"
    )
    # The spec's site_name appears in at least one injected title.
    assert "Sample Practitioner" in dash


def test_injected_handler_site_name_appears_in_every_model_title():
    """All injected handlers carry the derived site name, not just the first."""
    spec = {
        "app_name": "small-business-invoicer-v2-444",
        "data_models": [
            {"name": "Invoice", "fields": []},
            {"name": "Client", "fields": []},
            {"name": "Payment", "fields": []},
        ],
    }
    files = _minimal_files_with_dash_and_models()
    out = build._ensure_admin_model_handlers(files, spec)
    dash = dict(out)["routes/dashboard.js"]
    # Site name derived from app_name (no settings).
    assert "Small Business Invoicer" in dash
    # Every injected handler carries the derivation; count one per model.
    title_count = dash.count("| Small Business Invoicer'")
    assert title_count == 3, (
        f"Expected 3 handler titles with derived site name; found "
        f"{title_count}"
    )


def test_injected_handler_for_mw_spec_still_uses_medicine_waves():
    """Sanity check: real MW spec still renders 'Medicine Waves' because
    its spec.settings.site_name IS 'Medicine Waves', no hardcode, just
    correct derivation."""
    spec = {
        "app_name": "medicine-waves-v12-123456",
        "settings": {"site_name": "Medicine Waves"},
        "data_models": [
            {"name": "Event", "fields": []},
        ],
    }
    files = _minimal_files_with_dash_and_models()
    out = build._ensure_admin_model_handlers(files, spec)
    dash = dict(out)["routes/dashboard.js"]
    assert "Medicine Waves" in dash


def test_injected_handler_produces_valid_js_for_hyphenated_slugs():
    """Regression: models like BlogPost →
    slug 'blog-posts' previously produced `blog-posts: result.rows`
    inside an object literal, that's a JS SYNTAX ERROR (`-` is not a
    valid bare-identifier char). The injected locals key must use the
    underscore table name (`blog_posts`) so dashboard.js is parseable.

    Verifies the fix by piping the output through a Node syntax check
    when node is available, plus string-based assertions as a fallback."""
    import shutil
    import subprocess

    spec = {
        "app_name": "sample-practitioner-v1-123456",
        "settings": {"site_name": "Sample Practitioner"},
        "data_models": [
            {"name": "BlogPost", "fields": []},
            {"name": "ContactInquiry", "fields": []},
        ],
    }
    # Start with a minimal valid dashboard.js so our injected handlers
    # compose with existing boilerplate.
    base_dash = (
        "const express = require('express');\n"
        "const router = express.Router();\n"
        "const { requireAuth, requireRole } = require('../middleware/auth');\n"
        "const pool = require('../db');\n"
        "module.exports = router;\n"
    )
    files = [("routes/dashboard.js", base_dash)]
    out = build._ensure_admin_model_handlers(files, spec)
    dash = dict(out)["routes/dashboard.js"]

    # Sanity: hyphenated slug does NOT appear as an unquoted object key.
    assert "blog-posts: result.rows" not in dash, (
        "hyphenated slug leaked into object key; would produce JS "
        "SyntaxError at load time"
    )
    assert "contact-inquirys: result.rows" not in dash, (
        "ContactInquiry → contact-inquirys leaked into object key"
    )
    # The underscore table name IS used as the key (valid JS identifier).
    assert "blog_posts: result.rows" in dash

    # If node is available, assert the emitted file actually parses.
    if shutil.which("node"):
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False
        ) as tf:
            tf.write(dash)
            tf.flush()
            result = subprocess.run(
                ["node", "-c", tf.name],
                capture_output=True, text=True, timeout=10,
            )
        assert result.returncode == 0, (
            f"node syntax check FAILED: stderr={result.stderr[:500]}"
        )


def test_injected_handler_escapes_apostrophe_in_site_name():
    """Site names with apostrophes must not break the emitted JS string
    literal. 'Avery\\'s Studio' stays valid and round-trips intact."""
    spec = {
        "app_name": "averys-studio",
        "settings": {"site_name": "Avery's Studio"},
        "data_models": [
            {"name": "Project", "fields": []},
        ],
    }
    files = _minimal_files_with_dash_and_models()
    out = build._ensure_admin_model_handlers(files, spec)
    dash = dict(out)["routes/dashboard.js"]
    assert "Avery\\'s Studio" in dash
    # No stray unescaped apostrophes breaking the JS string.
    assert "'Manage Project | Avery's Studio'" not in dash
