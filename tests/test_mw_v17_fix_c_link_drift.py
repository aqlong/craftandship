"""MW v17 FIX C, _fix_route_link_drift post-processor.

Regression: v16 admin dashboard linked `<a href="/messages">` but the actual
route is mounted at `/contact-messages`. Clicking the nav tile returned 404.

Goal: crawl `<a href>` values in view templates; for each href that doesn't
resolve against a mounted route in server.js, search for a close-match route
(same slug suffix or single-word rename) and rewrite the href. If no match
is found, leave it alone and log a warning (another post-processor may fill
the gap, or it's a dynamic runtime href).
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


class TestFixRouteLinkDrift:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_fix_route_link_drift"), (
            "FIX C: add _fix_route_link_drift(files, spec) to cli/build.py"
        )
        assert callable(build._fix_route_link_drift)

    def test_messages_rewritten_to_contact_messages(self):
        """/messages href should rewrite to /contact-messages when only the
        latter is mounted."""
        import build
        files = [
            ("views/admin/dashboard.ejs",
             '<a href="/messages" class="tile">Messages</a>'),
            ("server.js",
             "const express=require('express'); const app=express();\n"
             "app.use('/contact-messages', require('./routes/contact-messages'));\n"
             "app.use('/events', require('./routes/events'));\n"),
        ]
        files = build._fix_route_link_drift(files, {})
        dash = dict(files)["views/admin/dashboard.ejs"]
        assert 'href="/contact-messages"' in dash, (
            f"Dashboard /messages link must be rewritten; got: {dash!r}"
        )
        assert 'href="/messages"' not in dash, (
            "Old /messages href must be gone (no 404 drift)"
        )

    def test_messages_any_depth_path(self):
        """/messages/new should rewrite to /contact-messages/new."""
        import build
        files = [
            ("views/admin/events.ejs",
             '<a href="/messages/new">New Message</a>'),
            ("server.js",
             "app.use('/contact-messages', require('./routes/contact-messages'));\n"),
        ]
        files = build._fix_route_link_drift(files, {})
        view = dict(files)["views/admin/events.ejs"]
        assert 'href="/contact-messages/new"' in view, (
            "Sub-path after /messages must also rewrite to /contact-messages/new"
        )

    def test_mounted_href_is_left_alone(self):
        """An href that already resolves against a mounted route must NOT be touched."""
        import build
        files = [
            ("views/admin/dashboard.ejs",
             '<a href="/events">Events</a><a href="/events/new">New</a>'),
            ("server.js",
             "app.use('/events', require('./routes/events'));\n"),
        ]
        files = build._fix_route_link_drift(files, {})
        dash = dict(files)["views/admin/dashboard.ejs"]
        assert 'href="/events"' in dash
        assert 'href="/events/new"' in dash

    def test_external_and_anchor_links_ignored(self):
        """http(s) and # links must be left alone."""
        import build
        files = [
            ("views/footer.ejs",
             '<a href="https://instagram.com/sample-handle">IG</a>'
             '<a href="#main">Skip</a>'
             '<a href="mailto:a@b.c">mail</a>'),
            ("server.js", ""),
        ]
        before = dict(files)["views/footer.ejs"]
        files = build._fix_route_link_drift(files, {})
        after = dict(files)["views/footer.ejs"]
        assert before == after, "External and anchor links must not be rewritten"

    def test_dynamic_ejs_hrefs_ignored(self):
        """hrefs containing <%= ... %> expressions must not be rewritten."""
        import build
        files = [
            ("views/events/index.ejs",
             '<a href="/events/<%= event.id %>/edit">Edit</a>'),
            ("server.js",
             "app.use('/events', require('./routes/events'));\n"),
        ]
        before = dict(files)["views/events/index.ejs"]
        files = build._fix_route_link_drift(files, {})
        after = dict(files)["views/events/index.ejs"]
        assert before == after, "Dynamic EJS hrefs must not be rewritten"

    def test_unresolvable_href_left_alone(self):
        """If no close-match route exists, leave the href as-is (don't break the tag)."""
        import build
        files = [
            ("views/admin/weird.ejs",
             '<a href="/xyznonexistent">Weird</a>'),
            ("server.js",
             "app.use('/events', require('./routes/events'));\n"),
        ]
        before = dict(files)["views/admin/weird.ejs"]
        files = build._fix_route_link_drift(files, {})
        after = dict(files)["views/admin/weird.ejs"]
        assert before == after, "Unresolvable href left alone"

    def test_idempotent(self):
        """Running twice produces the same output."""
        import build
        files = [
            ("views/admin/dashboard.ejs", '<a href="/messages">Messages</a>'),
            ("server.js", "app.use('/contact-messages', require('./routes/contact-messages'));"),
        ]
        first = dict(build._fix_route_link_drift(files, {}))
        files2 = list(first.items())
        second = dict(build._fix_route_link_drift(files2, {}))
        assert first["views/admin/dashboard.ejs"] == second["views/admin/dashboard.ejs"]


class TestPipelineWiring:
    """FIX C must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_fix_route_link_drift must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_fix_route_link_drift" in pipeline.REGISTRY, (
            "FIX C: _fix_route_link_drift must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_fix_route_link_drift"]
        assert "build" in proc.phase, (
            "FIX C: _fix_route_link_drift must run in the build phase"
        )

    def test_wired_into_reapply(self):
        """_fix_route_link_drift must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_fix_route_link_drift"]
        assert "reapply" in proc.phase, (
            "FIX C: _fix_route_link_drift must also run in the reapply phase"
        )
