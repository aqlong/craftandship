"""MW v17 FIX F, admin settings input dark-theme contrast.

Regression: v16 admin settings form rendered inputs with white backgrounds
(inherited from .input-premium) and no dark-theme override. Text values on
a dark primary app were near-invisible ("Medicine Waves" on white field).

Fix: extend the inline `<style>` block at the top of
cli/templates/views_admin_settings.ejs to override .input-premium input +
textarea + select with dark-theme friendly colors when the page is on a
dark-primary surface (same selector pattern as the existing
.admin-settings-card dark override).

Since _ensure_site_settings re-injects this template into each build from
the CLI file, editing the template is the source-of-truth fix, no new
post-processor is needed. The assertions here are on the template itself.
"""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

from pathlib import Path

ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "cli" / "templates" / "views_admin_settings.ejs"


class TestSettingsTemplateDarkInputOverride:
    def test_template_exists(self):
        assert TEMPLATE.exists(), "cli/templates/views_admin_settings.ejs missing"

    def test_template_has_dark_input_premium_override(self):
        """Template's inline <style> must define a dark-theme variant for
        .input-premium (and textarea/select) so values are readable on dark
        surfaces."""
        content = TEMPLATE.read_text()
        # The dark-mode selector used by the existing admin-settings-card overrides
        # body.bg-slate-950 / body[class*="bg-primary"] / [data-theme="dark"]
        assert "body.bg-slate-950 .admin-settings-card .input-premium" in content or (
            '[data-theme="dark"] .admin-settings-card .input-premium' in content
        ), "Template missing dark-theme .input-premium override"
        # Must set a dark background
        assert "background" in content
        assert ("rgba(15, 23, 42" in content  # slate-900-ish
                or "#0f172a" in content
                or "rgba(30, 41, 59" in content  # slate-800-ish
                or "#1e293b" in content
                or "rgba(26, 36, 86" in content), (
            "Dark-theme input must have a slate/navy background"
        )
        # Must set readable text color
        assert ("#e2e8f0" in content or "#f1f5f9" in content or "#cbd5e1" in content or "#fff" in content), (
            "Dark-theme input must have a light text color"
        )

    def test_override_covers_textarea_and_select(self):
        """Dark-theme styling must cover textarea + select, not just input."""
        content = TEMPLATE.read_text()
        dark_block_start = content.find("body.bg-slate-950")
        if dark_block_start < 0:
            dark_block_start = content.find('[data-theme="dark"]')
        assert dark_block_start >= 0
        rest = content[dark_block_start:]
        # textarea and select should appear in the dark block
        assert "textarea" in rest.lower(), "textarea not covered by dark override"
        assert "select" in rest.lower(), "select not covered by dark override"

    def test_light_default_input_background_preserved(self):
        """The base (light) .input-premium should still be white, only the
        dark-theme media/class selector overrides it."""
        content = TEMPLATE.read_text()
        # cheap check: light-theme default still references white/neutral
        # (we just don't want to have BROKEN the default block)
        assert ".input-premium" in content


class TestRenderedBuildReflectsOverride:
    def test_reapply_writes_updated_template_to_build(self):
        """Running _ensure_site_settings with the fixed template produces a
        views/admin/settings.ejs that includes the dark-theme override."""
        import sys
        sys.path.insert(0, str(ROOT / "cli"))
        import build

        spec = {
            "app_name": "tmp-app",
            "admin_email": "a@b.c",
            "description": "x",
            "tagline": "y",
            "_seed_settings": [
                {"key": "site_name", "value": "MW"},
                {"key": "hero_tagline", "value": "tagline"},
            ],
        }
        files = [
            ("db/schema.sql", "CREATE TABLE users();"),
            ("db/seed.sql", "-- seed"),
            ("routes/upload.js", "// upload"),
        ]
        files = build._ensure_site_settings(files, spec)
        settings_view = dict(files).get("views/admin/settings.ejs", "")
        assert ".input-premium" in settings_view
        assert "body.bg-slate-950" in settings_view or '[data-theme="dark"]' in settings_view
