"""Section-05: admin_dashboard contract tests (§4.4)."""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

from cli.contracts import ContractViolation  # noqa: E402
from cli.contracts import admin_dashboard as _admin_dashboard  # noqa: E402


def _artifact_with(dashboard_ejs: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        spec={},
        files=[("views/admin/dashboard.ejs", dashboard_ejs)],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/test",
    )


def test_admin_dashboard_fails_on_inline_settings_form():
    """admin_dashboard.check() fails if views/admin/dashboard.ejs contains <form action="/admin/settings">."""
    bad = (
        "<h1>Admin Dashboard</h1>\n"
        '<form action="/admin/settings" method="POST">\n'
        '<input name="_csrf" value="x">\n'
        "</form>\n"
    )
    art = _artifact_with(bad)
    with pytest.raises(ContractViolation) as exc_info:
        _admin_dashboard.check(art, collect_all=False)
    assert "/admin/settings" in str(exc_info.value)


def test_admin_dashboard_passes_with_cta_link_only():
    """admin_dashboard.check() passes when dashboard has <a href="/admin/settings"> CTA only."""
    good = (
        "<h1>Admin Dashboard</h1>\n"
        '<section class="dashboard-settings-cta">\n'
        '<a href="/admin/settings">Manage Site Settings</a>\n'
        "</section>\n"
    )
    art = _artifact_with(good)
    _admin_dashboard.check(art, collect_all=False)


def test_admin_dashboard_fails_when_cta_link_missing():
    """admin_dashboard.check() fails when CTA link to /admin/settings is missing."""
    bad = "<h1>Admin Dashboard</h1>\n<p>Nothing here.</p>\n"
    art = _artifact_with(bad)
    with pytest.raises(ContractViolation):
        _admin_dashboard.check(art, collect_all=False)
