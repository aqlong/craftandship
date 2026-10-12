"""MW v17 FIX H, strip /register link when spec has no signup page.

Regression: MW v16 login page links to `/register` even though the spec
never lists a register page and the product has no customer signup (founder
is the only admin). Link resolves to a real route + template (Claude always
emits a register scaffold) but it shouldn't appear.

Existing `_strip_dead_signup_links` only strips when NO route exists. This
change adds a second trigger: if the spec.pages list contains no /register
or /signup path, strip regardless of route existence, spec intent wins
over Claude's boilerplate.
"""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)


LOGIN_WITH_REGISTER_LINK = """<section>
  <form action="/login" method="POST">
    <input type="email" name="email">
    <input type="password" name="password">
    <button>Sign In</button>
  </form>
  <p>Don't have an account? <a href="/register">Register here</a></p>
</section>
"""

SPEC_WITH_SIGNUP = {
    "pages": [
        {"path": "/"}, {"path": "/register"}, {"path": "/login"},
    ]
}
SPEC_WITHOUT_SIGNUP = {
    "pages": [
        {"path": "/"}, {"path": "/events"}, {"path": "/admin"},
    ]
}


class TestStripRegisterLinkViaSpec:
    def test_strips_link_when_spec_pages_has_no_register(self):
        """Even if a /register route exists, strip the link when spec.pages
        doesn't list /register or /signup, spec intent is authoritative."""
        import build
        files = [
            ("views/login.ejs", LOGIN_WITH_REGISTER_LINK),
            ("routes/auth.js", "router.get('/register', () => {}); router.post('/register', () => {});"),
        ]
        files = build._strip_dead_signup_links(files, SPEC_WITHOUT_SIGNUP)
        content = dict(files)["views/login.ejs"]
        assert "/register" not in content, (
            "When spec.pages has no /register, the link must be stripped"
        )
        assert "Register here" not in content

    def test_preserves_link_when_spec_includes_register_page(self):
        """If spec.pages explicitly lists /register, leave the link alone."""
        import build
        files = [
            ("views/login.ejs", LOGIN_WITH_REGISTER_LINK),
            ("routes/auth.js", "router.get('/register', () => {});"),
        ]
        before = dict(files)["views/login.ejs"]
        files = build._strip_dead_signup_links(files, SPEC_WITH_SIGNUP)
        after = dict(files)["views/login.ejs"]
        assert before == after, (
            "When spec explicitly declares /register, link must NOT be stripped"
        )

    def test_existing_behavior_preserved_when_no_route(self):
        """Legacy behavior: still strips when spec has no pages AND no route."""
        import build
        files = [
            ("views/login.ejs", LOGIN_WITH_REGISTER_LINK),
            ("routes/auth.js", "// no signup routes"),
        ]
        files = build._strip_dead_signup_links(files, {"pages": []})
        content = dict(files)["views/login.ejs"]
        assert "/register" not in content

    def test_missing_spec_falls_back_to_route_check(self):
        """If spec is empty/missing, fall back to the existing route-existence check."""
        import build
        files = [
            ("views/login.ejs", LOGIN_WITH_REGISTER_LINK),
            ("routes/auth.js", "router.get('/register', () => {});"),
        ]
        before = dict(files)["views/login.ejs"]
        files = build._strip_dead_signup_links(files, {})  # no pages key
        after = dict(files)["views/login.ejs"]
        assert before == after, (
            "Missing spec → fall back to old behavior (route exists, link preserved)"
        )
