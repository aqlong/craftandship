"""Tests for _fix_admin_layout_nav_prefix.

The processor adds /admin/ prefix to <a href="/<slug>"> links inside
<nav>...</nav> of views/layout.ejs (admin layout only) when:
- <slug> matches a writable-resource plural from spec data_models
- server.js has a matching app.use('/admin/<slug>', ...) mount.

Scope carve-outs:
- views/layout-public.ejs is never touched.
- /dashboard, /admin/*, /logout, / preserved.
- Hrefs outside <nav>...</nav> (e.g. in <footer>) untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import build  # noqa: E402


SAMPLE_SPEC = {
    "data_models": [
        {"name": "BlogPost"},
        {"name": "Event"},
        {"name": "Testimonial"},
        {"name": "ContactInquiry"},
        {"name": "NewsletterSubscriber"},
    ],
}


def _server_with_admin_mounts(*slugs: str) -> str:
    """Minimal server.js body with admin dual-mounts for given slugs."""
    lines = ["const express = require('express');", "const app = express();"]
    for slug in slugs:
        lines.append(f"app.use('/{slug}', require('./routes/{slug}'));")
        lines.append(
            f"app.use('/admin/{slug}', requireAdmin, require('./routes/{slug}'));"
        )
    lines.append("app.listen(process.env.PORT || 4000);")
    return "\n".join(lines)


def _layout_with_nav(nav_inner: str) -> str:
    """Minimal admin layout with one <nav> wrapping the given inner HTML."""
    return (
        "<!DOCTYPE html><html><body>\n"
        '<nav class="bg-primary sticky top-0">\n'
        f"{nav_inner}\n"
        "</nav>\n"
        "<main><%- body %></main>\n"
        "</body></html>"
    )


_LAYOUT_PUBLIC_FIXTURE = (
    "<!DOCTYPE html><html><body>\n"
    '<nav class="bg-white"><a href="/">Sample Practitioner</a></nav>\n'
    "<main><%- body %></main>\n"
    "</body></html>"
)


def _two_layout_files(admin_layout: str, server_js: str) -> list:
    """Build the files list for a two-layout architecture (layout.ejs
    + layout-public.ejs). F1 gate requires layout-public.ejs to exist
    for the processor to fire."""
    return [
        ("views/layout.ejs", admin_layout),
        ("views/layout-public.ejs", _LAYOUT_PUBLIC_FIXTURE),
        ("server.js", server_js),
    ]


# ---- test 1: bare slug nav link gets admin prefix -----------------

def test_bare_slug_nav_link_gets_admin_prefix():
    nav = '<a href="/blog-posts" class="text-sm">Blog Posts</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/admin/blog-posts"' in got
    assert 'href="/blog-posts"' not in got


# ---- test 2: already admin-prefixed unchanged ---------------------

def test_already_admin_prefixed_unchanged():
    nav = '<a href="/admin/blog-posts" class="text-sm">Blog Posts</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    # Must NOT become /admin/admin/blog-posts.
    assert "/admin/admin/" not in got
    assert 'href="/admin/blog-posts"' in got


# ---- test 3: /dashboard unchanged ---------------------------------

def test_dashboard_link_unchanged():
    nav = '<a href="/dashboard" class="text-sm">Dashboard</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/dashboard"' in got
    assert "/admin/dashboard" not in got


# ---- test 4: /admin/settings unchanged ----------------------------

def test_admin_settings_link_unchanged():
    nav = '<a href="/admin/settings" class="text-sm">Settings</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/admin/settings"' in got
    assert "/admin/admin/" not in got


# ---- test 5: home link unchanged ----------------------------------

def test_home_link_unchanged():
    nav = '<a href="/" class="brand">Sample Practitioner</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/"' in got
    # The home anchor is not rewritten to /admin/.
    assert 'href="/admin/"' not in got


# ---- test 6: logout form action unchanged -------------------------

def test_logout_form_action_unchanged():
    nav = (
        '<form action="/logout" method="POST">\n'
        '  <button type="submit">Logout</button>\n'
        "</form>"
    )
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'action="/logout"' in got
    assert 'action="/admin/logout"' not in got


# ---- test 7: views/layout-public.ejs not touched ------------------

def test_layout_public_not_touched():
    layout_public = _layout_with_nav('<a href="/blog-posts">Blog Posts</a>')
    # No views/layout.ejs at all in this build (or it's the public
    # one named conventionally). The processor should still no-op
    # gracefully because views/layout.ejs isn't present.
    files = [
        ("views/layout-public.ejs", layout_public),
        ("server.js", _server_with_admin_mounts("blog-posts")),
    ]
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout-public.ejs"]
    assert 'href="/blog-posts"' in got  # unchanged
    assert "/admin/blog-posts" not in got


# ---- test 8: non-writable slug not touched ------------------------

def test_non_writable_slug_not_touched():
    # /contact is NOT a data-model slug in SAMPLE_SPEC; left alone.
    nav = '<a href="/contact" class="text-sm">Contact</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/contact"' in got
    assert 'href="/admin/contact"' not in got


# ---- test 9: no admin mount means no rewrite ----------------------

def test_no_admin_mount_no_rewrite():
    # server.js has ONLY the bare mount, no /admin/blog-posts mount.
    # Under this shape the rewrite would produce a 404, so we skip it.
    nav = '<a href="/blog-posts" class="text-sm">Blog Posts</a>'
    layout = _layout_with_nav(nav)
    server_no_admin_mounts = (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use('/blog-posts', require('./routes/blog-posts'));\n"
        "app.listen(4000);"
    )
    files = _two_layout_files(layout, server_no_admin_mounts)
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/blog-posts"' in got  # unchanged
    assert "/admin/blog-posts" not in got


# ---- test 10: idempotent reapply ---------------------------------

def test_idempotent_reapply():
    nav = '<a href="/blog-posts" class="text-sm">Blog Posts</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    once = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    twice = dict(build._fix_admin_layout_nav_prefix(
        list(once.items()), SAMPLE_SPEC,
    ))
    assert once["views/layout.ejs"] == twice["views/layout.ejs"]
    # Double check: no /admin/admin/ after second pass.
    assert "/admin/admin/" not in twice["views/layout.ejs"]


# ---- bonus: nested slug path preserved ----------------------------

def test_nested_slug_path_preserved():
    """Ensure /events/new → /admin/events/new (tail preserved)."""
    nav = '<a href="/events/new">New Event</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("events"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/admin/events/new"' in got


# ---- bonus: footer href in same layout untouched -----------------

def test_footer_href_outside_nav_not_touched():
    """Href inside <footer>, outside <nav>, must not be rewritten."""
    layout = (
        "<!DOCTYPE html><html><body>\n"
        '<nav><a href="/blog-posts">Blog Posts</a></nav>\n'
        '<footer><a href="/blog-posts">Blog RSS</a></footer>\n'
        "</body></html>"
    )
    files = _two_layout_files(layout, _server_with_admin_mounts("blog-posts"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    # Nav link rewritten, footer link left alone.
    assert '<nav><a href="/admin/blog-posts">Blog Posts</a></nav>' in got
    assert '<footer><a href="/blog-posts">Blog RSS</a></footer>' in got


# ---- F1 regression guard: single-layout architecture must NOT be rewritten
# When Claude emits only
# views/layout.ejs (shared public+admin) with NO views/layout-public.ejs,
# rewriting nav to /admin/<slug> bounces anonymous
# visitors to /login on every Events click. Gate on layout-public.ejs
# existence. Test ensures the processor no-ops when that file is absent.


def test_f1_single_layout_no_public_file_no_rewrite():
    """When views/layout-public.ejs is absent (single-layout architecture),
    the processor must leave bare /<slug> hrefs alone. Only two-layout
    builds (layout.ejs for admin + layout-public.ejs for public) should
    get the /admin/ prefix treatment."""
    nav = '<a href="/events" class="text-sm">Events</a>'
    layout = _layout_with_nav(nav)
    files = [
        ("views/layout.ejs", layout),
        # NO views/layout-public.ejs: single-layout architecture.
        ("server.js", _server_with_admin_mounts("events")),
    ]
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    # Bare href preserved; NOT rewritten to /admin/events.
    assert 'href="/events"' in got
    assert "/admin/events" not in got


def test_f1_two_layout_architecture_still_gets_rewritten():
    """Complement to test_f1_single_layout_no_public_file_no_rewrite;
    when views/layout-public.ejs IS present, the rewrite fires as usual."""
    nav = '<a href="/events" class="text-sm">Events</a>'
    layout = _layout_with_nav(nav)
    files = _two_layout_files(layout, _server_with_admin_mounts("events"))
    out = dict(build._fix_admin_layout_nav_prefix(files, SAMPLE_SPEC))
    got = out["views/layout.ejs"]
    assert 'href="/admin/events"' in got
