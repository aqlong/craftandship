"""MW v15 fix coverage tests, written BEFORE fixing to lock in behavior.

Each test captures a specific regression found during MW v14 QA. Tests are
designed to fail before fix + pass after fix, and to cover edge cases the
initial implementation might miss.
"""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)


# ============================================================================
# FIX 1: Admin settings upload-zone ordering in reapply_post_processors
# ============================================================================

class TestReapplyUploadFormUiOrdering:
    """reapply_post_processors must run _ensure_site_settings BEFORE
    _ensure_upload_form_ui so the admin/settings.ejs template (injected by the
    former) gets its image inputs converted to upload zones by the latter.

    v14 had wrong order: upload_form_ui ran first, then site_settings injected
    the unconverted template, overwriting any prior conversion."""

    def test_reapply_injects_settings_template_before_upload_conversion(self):
        """Direct check: in the reapply topological order, _ensure_site_settings
        must appear BEFORE _ensure_upload_form_ui.

        Updated in section-02-foundation: uses pipeline.pipeline_order("reapply")
        + .index() instead of inspecting reapply_post_processors source (which
        is now a single run_pipeline call). Behavioral guard discipline: assert on behavior,
        not on literal source text.
        """
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        order = pipeline.pipeline_order("reapply")
        assert "_ensure_site_settings" in order, "reapply missing _ensure_site_settings"
        assert "_ensure_upload_form_ui" in order, "reapply missing _ensure_upload_form_ui"

        ss_pos = order.index("_ensure_site_settings")
        uf_pos = order.index("_ensure_upload_form_ui")
        assert ss_pos < uf_pos, (
            "reapply phase must run _ensure_site_settings BEFORE "
            "_ensure_upload_form_ui; otherwise the admin/settings.ejs template "
            "gets injected AFTER conversion, overwriting the upload zones with "
            "raw <input type=url> inputs. See MW v14 regression."
        )

    def test_site_settings_then_upload_form_ui_produces_upload_zones(self):
        """Integration: site_settings → upload_form_ui should produce a
        template with .image-upload-zone divs, not raw <input type="url">."""
        from build import _ensure_site_settings, _ensure_upload_form_ui

        spec = {
            "app_name": "test-app", "admin_email": "a@b.c",
            "description": "x", "tagline": "y",
            "_seed_settings": [
                {"key": "nav_logo_url", "value": ""},
                {"key": "hero_image_url", "value": ""},
                {"key": "about_photo_url", "value": ""},
            ],
        }
        files = [
            ("db/schema.sql", "CREATE TABLE users ();"),
            ("db/seed.sql", "-- seed"),
            ("routes/upload.js", "// upload route"),
        ]
        files = _ensure_site_settings(files, spec)
        files = _ensure_upload_form_ui(files, spec)
        fm = dict(files)
        content = fm.get("views/admin/settings.ejs", "")
        assert "image-upload-zone" in content, (
            "Expected upload-zone divs after upload_form_ui runs on settings template"
        )
        assert 'type="hidden" id="nav_logo_url"' in content, (
            "Expected hidden input wired to nav_logo_url after conversion"
        )


# ============================================================================
# FIX 2: Timezone helper regex, broaden to catch .toLocaleDateString(...) UTC
# ============================================================================

class TestTimezoneHelperRegex:
    """_ensure_event_time_helper must rewrite all common date_time display
    patterns, including new Date(x.date_time).toLocaleDateString('en-US', {...})
    followed by a literal ' UTC' text."""

    def test_rewrites_toLocaleDateString_pattern(self):
        """Most common v14 pattern: <%= new Date(event.date_time).toLocaleDateString('en-US', {...}) %> UTC"""
        from build import _ensure_event_time_helper
        view = (
            '<div>'
            '<%= new Date(event.date_time).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" }) %> UTC'
            '</div>'
        )
        files = [
            ("server.js", "const app = express();\napp.use(express.json());"),
            ("views/home.ejs", view),
        ]
        result = _ensure_event_time_helper(files, {})
        fm = dict(result)
        out = fm["views/home.ejs"]
        # After fix: should use formatEventDate helper, no bare toLocaleDateString, no " UTC" suffix
        assert "formatEventDate(event.date_time)" in out, (
            "Expected rewrite to formatEventDate(event.date_time)"
        )
        assert "toLocaleDateString" not in out, (
            "toLocaleDateString call should be replaced"
        )
        assert " UTC</div>" not in out, (
            "Literal ' UTC' suffix must be stripped (helper now includes timeZoneName)"
        )

    def test_rewrites_toLocaleString_pattern(self):
        """<%= new Date(event.date_time).toLocaleString("en-US", {...}) %>"""
        from build import _ensure_event_time_helper
        view = '<%= new Date(event.date_time).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", timeZoneName: "short" }) %>'
        files = [
            ("server.js", "const app = express();"),
            ("views/events/detail.ejs", view),
        ]
        result = _ensure_event_time_helper(files, {})
        out = dict(result)["views/events/detail.ejs"]
        assert "formatEventTime(event.date_time)" in out

    def test_rewrites_forEach_iterator_patterns(self):
        """Card rendering inside forEach loop: `e.date_time`, `evt.date_time`, etc."""
        from build import _ensure_event_time_helper
        view = (
            '<% events.forEach(function(e) { %>'
            '<%= new Date(e.date_time).toLocaleDateString("en-US", { month: "long", day: "numeric" }) %>'
            '<% }); %>'
        )
        files = [
            ("server.js", "const app = express();"),
            ("views/events.ejs", view),
        ]
        result = _ensure_event_time_helper(files, {})
        out = dict(result)["views/events.ejs"]
        assert "formatEventDate(e.date_time)" in out, (
            "Must handle iterator var names other than 'event'"
        )

    def test_preserves_already_converted_patterns(self):
        """Idempotency: if a view already uses formatEventTime/Date, don't double-convert."""
        from build import _ensure_event_time_helper
        view = '<%= formatEventTime(event.date_time) %>'
        files = [
            ("server.js", "const app = express();\napp.locals.formatEventTime = function() {};"),
            ("views/x.ejs", view),
        ]
        result = _ensure_event_time_helper(files, {})
        out = dict(result)["views/x.ejs"]
        assert out.count("formatEventTime(event.date_time)") == 1


# ============================================================================
# FIX 3: Dead /register link strip, handle multi-line <p> wrapping
# ============================================================================

class TestStripDeadSignupLinks:
    """_strip_dead_signup_links must remove dead signup/register links even
    when the <a> is wrapped in plain text inside a <p> element that spans
    content lines. v14 still had one at login.ejs:52."""

    def test_strips_register_link_in_paragraph_wrapper(self):
        """v14 actual pattern: <p>...Don't have an account? <a href="/register">Register here</a></p>"""
        from build import _strip_dead_signup_links
        view = (
            '<div class="form-footer">'
            "<p class=\"text-sm text-muted\">Don't have an account? "
            '<a href="/register" class="text-accent">Register here</a></p>'
            '</div>'
        )
        files = [
            ("routes/auth.js", "router.post('/login', async () => {});"),
            ("views/login.ejs", view),
        ]
        result = _strip_dead_signup_links(files, {})
        out = dict(result)["views/login.ejs"]
        assert '/register' not in out, (
            "Expected dead /register link to be stripped"
        )
        assert 'Register here' not in out, (
            "Expected 'Register here' text to be removed along with the link"
        )

    def test_strips_signup_link_single_a_no_wrapper(self):
        """Plain <a> without paragraph wrapper."""
        from build import _strip_dead_signup_links
        view = '<div>Welcome back. <a href="/signup">Sign up</a> if new.</div>'
        files = [
            ("routes/auth.js", "router.get('/login', () => {});"),
            ("views/login.ejs", view),
        ]
        result = _strip_dead_signup_links(files, {})
        out = dict(result)["views/login.ejs"]
        assert '/signup' not in out

    def test_preserves_link_when_signup_route_exists(self):
        """If a signup/register route exists, the link is legit, don't strip."""
        from build import _strip_dead_signup_links
        view = '<p>New? <a href="/register">Register</a></p>'
        files = [
            ("routes/auth.js", "router.post('/register', async () => {});"),
            ("views/login.ejs", view),
        ]
        result = _strip_dead_signup_links(files, {})
        out = dict(result)["views/login.ejs"]
        assert '/register' in out, (
            "Link should remain when matching route exists"
        )


# ============================================================================
# FIX 4: Hero image URL wired into home hero background
# ============================================================================

class TestHeroImageBinding:
    """_ensure_hero_settings_binding should wire hero_image_url into the home
    hero section as a background image when site_settings.hero_image_url is set."""

    def test_hero_section_has_conditional_background_image(self):
        """After post-processor, the <section class="hero-gradient"> should
        have a conditional inline style with settings.hero_image_url."""
        from build import _ensure_hero_settings_binding
        view = (
            '<section class="hero-gradient relative overflow-hidden">'
            '  <div class="max-w-6xl mx-auto px-4 py-20">'
            '    <h1>A somatic sound journey</h1>'
            '    <p>Description here</p>'
            '  </div>'
            '</section>'
        )
        files = [("views/home.ejs", view)]
        result = _ensure_hero_settings_binding(files)
        out = dict(result)["views/home.ejs"]
        # Fix target: the <section> tag (or an inner wrapper) should have an
        # inline style with a conditional background-image URL.
        has_conditional_bg = (
            "settings.hero_image_url" in out and
            "background" in out.lower()
        )
        assert has_conditional_bg, (
            "Expected hero section to conditionally set background-image "
            "from settings.hero_image_url when the value is present"
        )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
