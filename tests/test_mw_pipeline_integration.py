"""Integration tests: run the pipeline on a realistic spec and verify the
final file-dict has all the properties the MW builds have been missing.

These tests simulate what happens AFTER Claude generates the app, passing
through all post-processors in their actual pipeline order. If one of these
fails, the pipeline has a systemic gap, fix it before building v16+.

Unlike per-function unit tests (tests/test_mw_v15_fixes.py), these exercise
the interaction between post-processors and catch ordering/contract bugs
that only surface at the full-pipeline level.

RCA reference: docs/internals/MW_BUG_RECURRENCE_RCA.md
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


# ---------------------------------------------------------------------------
# Fixture: a minimal-but-representative MW-style spec
# ---------------------------------------------------------------------------

def _mw_like_spec():
    return {
        "spec_version": "0.1",
        "app_name": "integration-test-app",
        "admin_email": "a@b.c",
        "description": "A test app that simulates the MW shape for integration testing.",
        "tagline": "Test tagline",
        "user_roles": [
            {"role": "visitor", "description": "Public"},
            {"role": "admin", "description": "Admin"},
        ],
        "pages": [
            {"path": "/", "title": "Home", "purpose": "Hero", "auth_required": False, "allowed_roles": ["visitor", "admin"]},
            {"path": "/events", "title": "Events", "purpose": "List", "auth_required": False, "allowed_roles": ["visitor", "admin"]},
            {"path": "/admin", "title": "Admin", "purpose": "Dashboard", "auth_required": True, "allowed_roles": ["admin"]},
        ],
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "text"},
                {"name": "date_time", "type": "timestamp"},
                {"name": "image_url", "type": "text"},
            ]},
            {"name": "Photo", "fields": [
                {"name": "image_url", "type": "text"},
                {"name": "caption", "type": "text"},
            ]},
            {"name": "Video", "fields": [
                {"name": "title", "type": "text"},
                {"name": "embed_url", "type": "text"},
            ]},
        ],
        "actions": [],
        "settings": {"auth": True, "seed_data": True},
        "design": {
            "style": "modern dark",
            "primary_color": "#070924",
            "secondary_color": "#1a2456",
            "accent_color": "#5a80c0",
            "font_display": "Cormorant Garamond",
            "font_body": "DM Sans",
        },
        "_seed_settings": [
            {"key": "site_name", "value": "Test App"},
            {"key": "hero_tagline", "value": "Test tagline"},
            {"key": "hero_description", "value": "Test description"},
            {"key": "hero_image_url", "value": ""},
            {"key": "nav_logo_url", "value": ""},
            {"key": "about_title", "value": "About"},
            {"key": "about_body", "value": "Story"},
            {"key": "about_photo_url", "value": ""},
            {"key": "contact_email", "value": "a@b.c"},
            {"key": "contact_phone", "value": "555"},
            {"key": "contact_address", "value": "Nowhere"},
            {"key": "instagram_url", "value": ""},
        ],
    }


def _simulate_claude_generated_files():
    """Represents what Claude's Opus phase would emit BEFORE post-processors run.

    Deliberately includes the drift variants we've seen across v12-v15:
    admin-events.ejs (hyphen), events-index.ejs (-index suffix), raw file inputs,
    /register links, naive TIMESTAMP seed literals.
    """
    return [
        # Schema + seed
        ("db/schema.sql",
         "CREATE TABLE users (id SERIAL PRIMARY KEY, email TEXT);\n"
         "CREATE TABLE events (id SERIAL PRIMARY KEY, title TEXT, date_time TIMESTAMP, image_url TEXT, created_by INTEGER);\n"
         "CREATE TABLE photos (id SERIAL PRIMARY KEY, image_url TEXT, caption TEXT, created_by INTEGER);\n"
         "CREATE TABLE videos (id SERIAL PRIMARY KEY, title TEXT, embed_url TEXT, created_by INTEGER);\n"
         "CREATE TABLE contact_messages (id SERIAL PRIMARY KEY, name TEXT, email TEXT, message TEXT, created_at TIMESTAMP);\n"),
        ("db/seed.sql",
         "INSERT INTO events (title, date_time) VALUES ('Test', TIMESTAMP '2026-05-13 00:00:00');\n"),
        # Layout + views
        ("views/layout.ejs",
         '<!DOCTYPE html><html><head><title><%= title %></title></head>'
         '<body class="bg-slate-950"><%- body %></body></html>'),
        ("views/home.ejs",
         '<section class="hero py-20">'
         '<h1>Welcome</h1>'
         '<p>A tagline here.</p>'
         '</section>'),
        ("views/login.ejs",
         '<form action="/login" method="POST">'
         '<input type="email" name="email">'
         '<input type="password" name="password">'
         '<button type="submit">Sign In</button>'
         '</form>'),
        # Admin views: DELIBERATELY USE DRIFT VARIANT
        ("views/admin/events-index.ejs",  # v15 pattern
         '<h1>Manage Events</h1><a href="/admin/events/new">New</a>'),
        ("views/admin-photos.ejs",  # v13 pattern
         '<h1>Manage Photos</h1><a href="/admin/photos/new">New</a>'),
        ("views/admin/settings.ejs",
         '<form action="/admin/settings" method="POST">'
         '<input type="url" id="nav_logo_url" name="nav_logo_url" value="<%= _v(\'nav_logo_url\') %>">'
         '<input type="url" id="hero_image_url" name="hero_image_url" value="<%= _v(\'hero_image_url\') %>">'
         '</form>'),
        # Routes
        ("routes/auth.js",
         "const express = require('express');\n"
         "const router = express.Router();\n"
         "router.post('/login', () => {});\n"
         "module.exports = router;\n"),
        ("routes/home.js",
         "const express = require('express');\n"
         "const router = express.Router();\n"
         "router.get('/', (req, res) => { res.render('home'); });\n"
         "module.exports = router;\n"),
        ("routes/events.js",
         "const express = require('express');\n"
         "const router = express.Router();\n"
         "router.get('/', (req, res) => { res.render('events/index'); });\n"
         "router.get('/:id/edit', (req, res) => { res.render('events/edit'); });\n"
         "module.exports = router;\n"),
        ("routes/photos.js",
         "const express = require('express');\n"
         "const router = express.Router();\n"
         "module.exports = router;\n"),
        ("routes/videos.js",
         "const express = require('express');\n"
         "const router = express.Router();\n"
         "module.exports = router;\n"),
        # Dashboard with only events/photos (missing videos; that's the v15 bug)
        ("routes/dashboard.js",
         "const express = require('express');\n"
         "const router = express.Router();\n"
         "const pool = require('../db');\n"
         "const { requireAuth, requireRole } = require('../middleware/auth');\n"
         "router.get('/events', requireAuth, requireRole('admin'), async (req, res) => {\n"
         "  const result = await pool.query('SELECT * FROM events');\n"
         "  res.render('admin/events', { events: result.rows });\n"
         "});\n"
         "router.get('/photos', requireAuth, requireRole('admin'), async (req, res) => {\n"
         "  const result = await pool.query('SELECT * FROM photos');\n"
         "  res.render('admin/photos', { photos: result.rows });\n"
         "});\n"
         "// NOTE: /videos handler deliberately MISSING; v15 regression\n"
         "module.exports = router;\n"),
        ("routes/upload.js",
         "const express = require('express');\nconst router = express.Router();\nmodule.exports = router;\n"),
        # server.js
        ("server.js",
         "const express = require('express');\n"
         "const app = express();\n"
         "app.set('trust proxy', 1);\n"
         "app.set('view engine', 'ejs');\n"
         "app.use('/', require('./routes/auth'));\n"
         "app.use('/', require('./routes/home'));\n"
         "app.use('/events', require('./routes/events'));\n"
         "app.use('/photos', require('./routes/photos'));\n"
         "app.use('/videos', require('./routes/videos'));\n"
         "app.use('/admin', require('./routes/dashboard'));\n"
         "app.listen(3000);\n"),
    ]


def _run_key_post_processors(files, spec):
    """Run the v16 prevention post-processors in their main-pipeline order."""
    import build
    # Per main pipeline order (post-v16 fixes)
    files = build._ensure_site_settings(files, spec)
    files = build._ensure_upload_route(files, spec)
    files = build._ensure_upload_form_ui(files, spec)
    files = build._fix_admin_crud_link_prefix(files, spec)
    files = build._strip_dead_signup_links(files, spec)
    files = build._normalize_admin_view_paths(files, spec)
    files = build._enforce_admin_view_path_convention(files, spec)
    files = build._ensure_admin_model_handlers(files, spec)
    files = build._ensure_settings_middleware_contract(files, spec)
    files = build._convert_raw_file_inputs_to_upload_zones(files, spec)
    files = build._ensure_event_time_helper(files, spec)
    files = build._ensure_tz_aware_datetime_schema(files, spec)
    files = build._verify_template_middleware_contract(files, spec)
    return files


class TestIntegrationPipeline:
    """End-to-end contract tests, the build must produce a working app without
    hotpatches. Each test captures a real v12-v15 regression."""

    def setup_method(self):
        """Fresh pipeline run per test."""
        self.spec = _mw_like_spec()
        self.files = _simulate_claude_generated_files()
        self.result = dict(_run_key_post_processors(self.files, self.spec))

    # ========== Regression coverage ==========

    def test_admin_view_path_convention(self):
        """Views rendered by routes must exist at their exact paths. No drift."""
        # routes/dashboard.js renders 'admin/events': file must exist
        assert "views/admin/events.ejs" in self.result, (
            "routes/dashboard.js renders 'admin/events' but views/admin/events.ejs "
            "is missing. _enforce_admin_view_path_convention should rename "
            "admin/events-index.ejs → admin/events.ejs"
        )
        # And the drift variant should be gone
        assert "views/admin/events-index.ejs" not in self.result, (
            "Drift variant should have been renamed by _enforce_admin_view_path_convention"
        )

    def test_admin_dashboard_has_all_model_handlers(self):
        """dashboard.js must have GET /admin/{slug} for every data_model."""
        dash = self.result.get("routes/dashboard.js", "")
        for slug in ("events", "photos", "videos"):
            assert f"router.get('/{slug}'," in dash, (
                f"/admin/{slug} handler missing from dashboard.js; "
                "_ensure_admin_model_handlers should inject it"
            )

    def test_settings_middleware_populates_res_locals_settings(self):
        """Templates use `settings.X`, middleware must populate res.locals.settings."""
        server = self.result.get("server.js", "")
        assert "res.locals.settings" in server, (
            "server.js missing res.locals.settings = ... middleware. "
            "Templates will silently fall back to defaults. "
            "_ensure_settings_middleware_contract should inject this."
        )

    def test_admin_settings_upload_fields_are_zones(self):
        """Settings form image fields must be upload zones, not <input type='url'>."""
        settings_view = self.result.get("views/admin/settings.ejs", "")
        assert "image-upload-zone" in settings_view, (
            "Settings form must have drag-and-drop upload zones for image fields. "
            "_ensure_upload_form_ui should convert the <input type='url'> fields."
        )
        # Verify the specific fields are hidden inputs
        for field in ("nav_logo_url", "hero_image_url"):
            assert f'type="hidden" id="{field}"' in settings_view, (
                f"{field} should be wired as a hidden input + upload zone"
            )

    def test_schema_uses_timestamptz_for_date_time(self):
        """date_time columns must be TIMESTAMPTZ so pg returns UTC-anchored Dates."""
        schema = self.result.get("db/schema.sql", "")
        assert "date_time TIMESTAMPTZ" in schema or "date_time TIMESTAMP " not in schema, (
            "date_time column must use TIMESTAMPTZ (not naive TIMESTAMP). "
            "_ensure_tz_aware_datetime_schema should convert."
        )

    def test_seed_timestamps_anchored_to_utc(self):
        """Seed TIMESTAMP literals must be UTC-anchored."""
        seed = self.result.get("db/seed.sql", "")
        # Either TIMESTAMPTZ or AT TIME ZONE 'UTC' modifier
        has_naive = "TIMESTAMP '2026-05-13 00:00:00')" in seed
        has_anchored = "AT TIME ZONE 'UTC'" in seed
        assert has_anchored or not has_naive, (
            "Seed must anchor naive timestamps to UTC. "
            "_ensure_tz_aware_datetime_schema should append AT TIME ZONE 'UTC'."
        )

    def test_event_time_helper_injected(self):
        """server.js must have the formatEventTime/Date helpers."""
        server = self.result.get("server.js", "")
        assert "formatEventTime" in server and "formatEventDate" in server, (
            "server.js missing app.locals.formatEventTime/formatEventDate helpers"
        )

    def test_pipeline_is_idempotent(self):
        """Running the pipeline twice produces identical output (no double-conversion)."""
        first = dict(_run_key_post_processors(list(self.files), self.spec))
        second = dict(_run_key_post_processors(list(first.items()), self.spec))
        # Compare a few key files
        for key in ("views/admin/settings.ejs", "server.js", "db/schema.sql"):
            if key in first and key in second:
                assert first[key] == second[key], (
                    f"Pipeline not idempotent on {key}: output differs on 2nd run"
                )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
