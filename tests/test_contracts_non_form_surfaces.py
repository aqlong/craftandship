"""Section-05: home, about, login contract tests (§4.6)."""

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
from cli.contracts import about as _about  # noqa: E402
from cli.contracts import home as _home  # noqa: E402
from cli.contracts import login as _login  # noqa: E402


def _art(files: dict[str, str]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        spec={},
        files=list(files.items()),
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/test",
    )


# ---------------------------------------------------------------------------
# home
# ---------------------------------------------------------------------------


def test_home_fails_on_live_youtube_iframe():
    """home.check() fails on live <iframe src="*youtube.com*"> (must be click-to-play)."""
    files = {
        "views/home.ejs": '<iframe src="https://youtube.com/embed/abc"></iframe>\n',
        "server.js": "",
    }
    with pytest.raises(ContractViolation) as exc_info:
        _home.check(_art(files), collect_all=False)
    assert "youtube.com" in str(exc_info.value)


def test_home_fails_on_register_link_without_mounted_route():
    """home.check() fails on <a href="/register"> when /register route not mounted."""
    files = {
        "views/home.ejs": '<a href="/register">Sign up</a>\n',
        "server.js": "app.use('/login', loginRouter);\n",  # no /register
    }
    with pytest.raises(ContractViolation) as exc_info:
        _home.check(_art(files), collect_all=False)
    assert "/register" in str(exc_info.value)


def test_home_passes_when_register_route_is_mounted():
    """home.check() passes when /register route IS mounted (signup flow active)."""
    files = {
        "views/home.ejs": '<a href="/register">Sign up</a>\n',
        "server.js": "app.get('/register', registerController);\n",
    }
    _home.check(_art(files), collect_all=False)


def test_home_fails_on_inline_admin_settings_form():
    """home.check() fails on inline <form action="/admin/settings"> on home."""
    files = {
        "views/home.ejs": (
            '<form action="/admin/settings" method="POST">'
            '<input name="_csrf" value="x"></form>\n'
        ),
        "server.js": "",
    }
    with pytest.raises(ContractViolation):
        _home.check(_art(files), collect_all=False)


# ---------------------------------------------------------------------------
# about
# ---------------------------------------------------------------------------


def test_about_fails_on_svg_placeholder_for_about_photo():
    """about.check() fails on 1x1 SVG placeholder as about photo src."""
    placeholder = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxIiBoZWlnaHQ9IjEiLz4="
    files = {
        "views/about.ejs": (
            f'<section class="about-photo"><img src="{placeholder}"></section>\n'
        ),
    }
    with pytest.raises(ContractViolation):
        _about.check(_art(files), collect_all=False)


def test_about_passes_on_real_image_src():
    """about.check() passes on real image src for about photo."""
    files = {
        "views/about.ejs": (
            '<section class="about-photo"><img src="/images/stock/dome.jpg"></section>\n'
        ),
    }
    _about.check(_art(files), collect_all=False)


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_fails_on_hardcoded_demo_credentials():
    """login.check() fails on hardcoded demo credentials visible in DOM."""
    tainted = (
        "<form method=\"POST\" action=\"/login\">\n"
        '<input type="hidden" name="_csrf" value="x">\n'
        '<input type="email" name="email" value="admin@example.com">\n'
        '<input type="password" name="password">\n'
        "</form>\n"
    )
    files = {"views/login.ejs": tainted}
    with pytest.raises(ContractViolation) as exc_info:
        _login.check(_art(files), collect_all=False)
    assert "admin@example.com" in str(exc_info.value)


def test_login_passes_on_bare_email_password_form():
    """login.check() passes on bare email + password form."""
    good = (
        "<form method=\"POST\" action=\"/login\">\n"
        '<input type="hidden" name="_csrf" value="x">\n'
        '<input type="email" name="email">\n'
        '<input type="password" name="password">\n'
        "</form>\n"
    )
    files = {"views/login.ejs": good}
    _login.check(_art(files), collect_all=False)


def test_login_fails_when_csrf_missing():
    """login.check() fails when CSRF token missing."""
    broken = (
        "<form method=\"POST\" action=\"/login\">\n"
        '<input type="email" name="email">\n'
        '<input type="password" name="password">\n'
        "</form>\n"
    )
    files = {"views/login.ejs": broken}
    with pytest.raises(ContractViolation) as exc_info:
        _login.check(_art(files), collect_all=False)
    assert "_csrf" in str(exc_info.value)
