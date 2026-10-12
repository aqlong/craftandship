"""MW v17/v18 follow-up: FIXes L / M / N / O, image UX + logo archetype.

Surfaced by founder review of v18 admin/dashboard.ejs inline settings form:

  - L: admin dashboard shipped its own inline /admin/settings form which
       bypasses the canonical settings view (and its drag-drop upload zones).
       Strip the inline form; replace with a link to /admin/settings.

  - M: inline iteration over `settings.forEach` used `setting.key` as the
       <label> (e.g. 'nav_logo_url', 'hero_image_url'). Labels must be
       plain English.

  - N: image fields elsewhere in the app render as
       <input type="text" ...><input type="file"> pairs (the URL-plus-Choose-
       File pattern) instead of the drag-drop image-upload-zone. Broaden
       `_ensure_upload_form_ui` to cover every admin view, not just
       settings.ejs.

  - O: MW-archetype builds (spec description matches wellness + mentions
       'medicine waves') auto-bundle cli/assets/mw_logo.png into
       public/images/mw_logo.png. Pairs with FIX K which seeds nav_logo_url
       when a bundled logo exists.
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


# ---------------------------------------------------------------------------
# FIX L + M: inline settings block on admin dashboard
# ---------------------------------------------------------------------------

DASHBOARD_WITH_INLINE_SETTINGS = """<section><h1>Admin Dashboard</h1></section>

<!-- Site Settings -->
<div class="bg-secondary/50 rounded-xl border border-white/10 overflow-hidden">
  <div class="px-5 py-4 border-b border-white/10">
    <h2 class="font-display text-xl font-bold text-white">Site Settings</h2>
  </div>
  <div class="p-5">
    <form action="/admin/settings" method="POST" enctype="multipart/form-data">
      <input type="hidden" name="_csrf" value="<%= csrfToken %>">
      <% if (settings && settings.length > 0) { %>
        <div class="grid grid-cols-1 gap-4 mb-4">
          <% settings.forEach(function(setting) { %>
            <% if (setting.key === 'nav_logo_url' || setting.key === 'hero_image_url' || setting.key === 'about_photo_url') { %>
              <div>
                <label class="block text-sm font-medium text-slate-300 mb-1"><%= setting.key %></label>
                <input type="text" name="<%= setting.key %>" value="<%= setting.value || '' %>" class="w-full px-3 py-2 bg-primary/50 border border-white/10 rounded-lg text-white text-sm mb-1">
                <input type="file" name="<%= setting.key.replace('_url', '_file') %>" accept="image/*" class="text-sm text-slate-400">
              </div>
            <% } else { %>
              <div>
                <label class="block text-sm font-medium text-slate-300 mb-1"><%= setting.key %></label>
                <input type="text" name="<%= setting.key %>" value="<%= setting.value || '' %>" class="w-full">
              </div>
            <% } %>
          <% }); %>
        </div>
      <% } %>
      <button type="submit" class="bg-accent text-white">Save Settings</button>
    </form>
  </div>
</div>
"""


class TestFixLInlineDashboardSettingsStripped:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_strip_inline_dashboard_settings_form"), (
            "FIX L: add _strip_inline_dashboard_settings_form(files, spec)"
        )

    def test_inline_settings_form_replaced_with_link(self):
        """The duplicate inline <form action='/admin/settings'> in the dashboard
        must be replaced with a link to the canonical /admin/settings page."""
        import build
        files = [("views/admin/dashboard.ejs", DASHBOARD_WITH_INLINE_SETTINGS)]
        files = build._strip_inline_dashboard_settings_form(files, {})
        dash = dict(files)["views/admin/dashboard.ejs"]
        # The inline form must be gone
        assert 'action="/admin/settings"' not in dash or '<form action="/admin/settings"' not in dash, (
            "Inline <form action='/admin/settings'> must be replaced"
        )
        # And the snake_case `<%= setting.key %>` pattern with a file input
        # next to a url text input must be gone
        assert "setting.key.replace('_url', '_file')" not in dash, (
            "The URL+Choose File hybrid must be removed"
        )
        # A link to the canonical settings page replaces it
        assert 'href="/admin/settings"' in dash, "Dashboard should link to /admin/settings"
        # And a readable heading
        assert "Site Settings" in dash, "The 'Site Settings' section can still appear, but as a CTA card"

    def test_non_dashboard_views_untouched(self):
        """A non-dashboard view with a settings form is left alone (the canonical
        settings.ejs template is what we want to keep)."""
        import build
        canonical = '<form action="/admin/settings"><input type="hidden"></form>'
        files = [("views/admin/settings.ejs", canonical)]
        out = build._strip_inline_dashboard_settings_form(files, {})
        assert dict(out)["views/admin/settings.ejs"] == canonical

    def test_idempotent(self):
        import build
        files = [("views/admin/dashboard.ejs", DASHBOARD_WITH_INLINE_SETTINGS)]
        first = dict(build._strip_inline_dashboard_settings_form(files, {}))
        files2 = list(first.items())
        second = dict(build._strip_inline_dashboard_settings_form(files2, {}))
        assert first["views/admin/dashboard.ejs"] == second["views/admin/dashboard.ejs"]


# ---------------------------------------------------------------------------
# FIX N: broaden _ensure_upload_form_ui scope
# ---------------------------------------------------------------------------

URL_PLUS_FILE_FORM = """<form action="/admin/something" method="POST" enctype="multipart/form-data">
  <div>
    <label>hero_image_url</label>
    <input type="text" name="hero_image_url" value="/uploads/old.jpg">
    <input type="file" name="hero_image_file" accept="image/*">
  </div>
</form>
"""


class TestFixNUploadFormUiBroadened:
    def test_url_plus_file_pair_converted_to_upload_zone(self):
        """Any <input type='text'> ending in _url paired with an <input type='file'>
        must be collapsed into the canonical drag-drop upload zone shape."""
        import build
        files = [
            ("views/admin/dashboard.ejs", URL_PLUS_FILE_FORM),
            # _ensure_upload_form_ui requires the upload route to be present.
            ("routes/upload.js", "module.exports = require('express').Router();"),
        ]
        files = build._ensure_upload_form_ui(files, {"_seed_settings": [{"key": "hero_image_url", "value": ""}]})
        content = dict(files)["views/admin/dashboard.ejs"]
        assert "image-upload-zone" in content, (
            "URL+file pair must be converted to drag-drop upload zone"
        )
        # The visible text input for the URL must be gone (replaced with hidden)
        assert '<input type="text" name="hero_image_url"' not in content, (
            "Visible URL text input must be gone (upload zone uses hidden input)"
        )


# ---------------------------------------------------------------------------
# FIX M: English labels
# ---------------------------------------------------------------------------

class TestFixMEnglishLabels:
    def test_snake_case_label_detected_and_humanized(self):
        """In any admin view, a <label>...snake_case_key...</label> that matches
        a known setting key should be replaced with a plain-English label."""
        import build
        v = (
            '<form action="/admin/settings">'
            '<label>hero_tagline</label>'
            '<input type="text" name="hero_tagline">'
            '<label>nav_logo_url</label>'
            '<input type="hidden" name="nav_logo_url">'
            '</form>'
        )
        files = [("views/admin/dashboard.ejs", v)]
        files = build._humanize_settings_labels(files, {})
        content = dict(files)["views/admin/dashboard.ejs"]
        assert ">hero_tagline<" not in content, "raw snake_case label must be gone"
        assert "Hero Tagline" in content, "should have 'Hero Tagline' label"
        assert ">nav_logo_url<" not in content, "raw snake_case label must be gone"
        assert "Navigation Logo" in content, "should have 'Navigation Logo' label"

    def test_ejs_expression_label_humanized(self):
        """Labels of the form <%= setting.key %> should be rewritten to a
        mapping helper call (_label(setting.key)) that returns the English
        name, OR the block should be replaced with a hardcoded English label
        per-key."""
        import build
        v = (
            '<% settings.forEach(function(s) { %>'
            '<label><%= s.key %></label>'
            '<% }); %>'
        )
        files = [("views/admin/dashboard.ejs", v)]
        files = build._humanize_settings_labels(files, {})
        content = dict(files)["views/admin/dashboard.ejs"]
        # Either a helper call, or we've injected a humanizer function
        assert "s.key" not in content or "_humanLabel" in content or "settingsLabel" in content, (
            "<%= s.key %> label pattern must be converted to a humanizer"
        )


# ---------------------------------------------------------------------------
# FIX O: MW-archetype logo bundling
# ---------------------------------------------------------------------------

class TestFixOMwLogoBundled:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_mw_archetype_logo_bundled"), (
            "FIX O: add _ensure_mw_archetype_logo_bundled(files, spec)"
        )

    def test_mw_spec_bundles_logo(self):
        """A spec whose description says 'medicine waves' + somatic-wellness
        keywords should get cli/assets/mw_logo.png copied into the build as
        public/images/mw_logo.png."""
        import build
        spec = {
            "app_name": "medicine-waves-test",
            "description": "Medicine Waves is a somatic sound journey held inside a dome.",
        }
        files = [("server.js", "app.listen(3000);")]
        files = build._ensure_mw_archetype_logo_bundled(files, spec)
        paths = [p for p, _ in files]
        assert "public/images/mw_logo.png" in paths, (
            "MW-archetype build must bundle mw_logo.png"
        )
        # Contents must be bytes (the real PNG)
        mw_entry = next((c for p, c in files if p == "public/images/mw_logo.png"), None)
        assert isinstance(mw_entry, (bytes, bytearray)), "logo must be bytes"
        assert mw_entry[:4] == b"\x89PNG", "logo must be a valid PNG"

    def test_non_mw_spec_no_op(self):
        """A spec that doesn't mention 'medicine waves' should not get the
        MW logo (bundling should be archetype-specific)."""
        import build
        spec = {
            "app_name": "yoga-studio",
            "description": "A yoga studio booking platform.",
        }
        files = [("server.js", "app.listen(3000);")]
        files = build._ensure_mw_archetype_logo_bundled(files, spec)
        paths = [p for p, _ in files]
        assert "public/images/mw_logo.png" not in paths

    def test_idempotent(self):
        import build
        spec = {"app_name": "mw", "description": "Medicine Waves somatic sound journey"}
        files = [("server.js", "")]
        first = dict(build._ensure_mw_archetype_logo_bundled(files, spec))
        files2 = list(first.items())
        second = dict(build._ensure_mw_archetype_logo_bundled(files2, spec))
        assert first == second


# ---------------------------------------------------------------------------
# Pipeline wiring for all 4 fixes
# ---------------------------------------------------------------------------

class TestPipelineWiring:
    """FIXes L / M / N / O must be wired into the main pipeline + reapply
    path so they actually fire during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_fix_l_wired(self):
        """FIX L: _strip_inline_dashboard_settings_form must be registered
        with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_strip_inline_dashboard_settings_form" in pipeline.REGISTRY, (
            "FIX L: _strip_inline_dashboard_settings_form must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_strip_inline_dashboard_settings_form"]
        assert "build" in proc.phase, (
            "FIX L: _strip_inline_dashboard_settings_form must run in the build phase"
        )

    def test_fix_m_wired(self):
        """FIX M: _humanize_settings_labels must be registered with phase
        including 'build'."""
        import pipeline
        import build  # noqa: F401

        assert "_humanize_settings_labels" in pipeline.REGISTRY, (
            "FIX M: _humanize_settings_labels must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_humanize_settings_labels"]
        assert "build" in proc.phase, (
            "FIX M: _humanize_settings_labels must run in the build phase"
        )

    def test_fix_o_wired(self):
        """FIX O: _ensure_mw_archetype_logo_bundled must be registered with
        phase including 'build'."""
        import pipeline
        import build  # noqa: F401

        assert "_ensure_mw_archetype_logo_bundled" in pipeline.REGISTRY, (
            "FIX O: _ensure_mw_archetype_logo_bundled must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_mw_archetype_logo_bundled"]
        assert "build" in proc.phase, (
            "FIX O: _ensure_mw_archetype_logo_bundled must run in the build phase"
        )

    def test_all_fixes_in_reapply(self):
        """Every FIX L/M/O must have 'reapply' in proc.phase so the QA fix
        loop re-runs them when it regenerates affected files."""
        import pipeline
        import build  # noqa: F401

        for fn_name in (
            "_strip_inline_dashboard_settings_form",
            "_humanize_settings_labels",
            "_ensure_mw_archetype_logo_bundled",
        ):
            assert fn_name in pipeline.REGISTRY, (
                f"{fn_name} missing from REGISTRY; must be decorated with @processor"
            )
            proc = pipeline.REGISTRY[fn_name]
            assert "reapply" in proc.phase, (
                f"{fn_name} must also run in the reapply phase"
            )

    def test_fix_o_runs_before_fix_k_in_reapply(self):
        """Ordering dependency: FIX O (MW logo bundle) MUST run before FIX K
        (nav logo seed) in reapply. FIX K discovers the bundled logo; if FIX O
        hasn't run yet, FIX K no-ops. Bug observed on v19 2026-04-17."""
        import pipeline
        import build  # noqa: F401

        order = pipeline.pipeline_order("reapply")
        o_pos = order.index("_ensure_mw_archetype_logo_bundled")
        k_pos = order.index("_ensure_nav_logo_default_seed")
        assert o_pos < k_pos, (
            f"FIX O (pos {o_pos}) must run before FIX K (pos {k_pos}) in reapply"
        )

    def test_fix_o_runs_before_fix_k_in_build_app(self):
        """Same ordering rule in build_app."""
        import pipeline
        import build  # noqa: F401

        order = pipeline.pipeline_order("build")
        o_pos = order.index("_ensure_mw_archetype_logo_bundled")
        k_pos = order.index("_ensure_nav_logo_default_seed")
        assert o_pos < k_pos, (
            f"FIX O (pos {o_pos}) must run before FIX K (pos {k_pos}) in build_app"
        )


# ---------------------------------------------------------------------------
# Section-04: FIX L/M/N are byte-identical no-ops on widget-emitted output
# ---------------------------------------------------------------------------

class TestSection04ByteIdenticalOnWidgetOutput:
    """After section-04 lands, widget emission runs BEFORE these fixes and
    produces markup that already looks like the canonical output. The fixes
    must therefore be byte-identical no-ops when the input IS widget
    output. FIX O is unchanged (still bundles the MW logo).

    See docs/plans/b-plus-c/claude-plan.md (widget emission).
    """

    def _mw_spec(self):
        """Minimal MW-archetype spec with one settings key per section."""
        return {
            "app_name": "medicine-waves-byte-identical-test",
            "description": "Medicine Waves somatic sound journey app.",
            "settings": {"auth": True},
            "_seed_settings": [
                {"key": "site_name", "value": "Medicine Waves"},
                {"key": "hero_tagline", "value": "A somatic sound journey"},
                {"key": "hero_image_url", "value": ""},
                {"key": "about_title", "value": "About"},
                {"key": "contact_email", "value": "a@b.c"},
                {"key": "instagram_url", "value": ""},
            ],
            "data_models": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": "title", "type": "string", "required": True},
                        {"name": "image_url", "type": "image_url"},
                    ],
                },
            ],
        }

    def test_fix_l_byte_identical_on_widget_dashboard_snippet(self):
        """FIX L strips inline settings forms out of dashboards. Widget
        dashboard snippet is a CTA (no <form>), so FIX L no-ops."""
        import build
        from cli.widgets.forms import emit_dashboard_settings_section
        snippet = emit_dashboard_settings_section(self._mw_spec())
        files_before = [("views/admin/dashboard.ejs", snippet)]
        files_after = dict(build._strip_inline_dashboard_settings_form(files_before, {}))
        assert files_after["views/admin/dashboard.ejs"] == snippet, (
            "FIX L must be byte-identical no-op on widget dashboard snippet"
        )

    def test_fix_m_byte_identical_on_widget_settings_ejs(self):
        """FIX M humanizes snake_case labels. Widgets emit humanized labels
        up front, so FIX M's regexes find nothing to rewrite."""
        import build
        from cli.widgets.forms import emit_admin_settings
        settings = emit_admin_settings(self._mw_spec())
        files_before = [("views/admin/settings.ejs", settings)]
        files_after = dict(build._humanize_settings_labels(files_before, {}))
        assert files_after["views/admin/settings.ejs"] == settings, (
            "FIX M must be byte-identical no-op on widget settings.ejs"
        )

    def test_fix_n_byte_identical_on_widget_settings_ejs(self):
        """FIX N converts text/url image inputs into drag-drop zones.
        Widgets emit drag-drop zones directly, so FIX N finds no matching
        inputs and early-returns without mutating content."""
        import build
        from cli.widgets.forms import emit_admin_settings
        settings = emit_admin_settings(self._mw_spec())
        files_before = [
            ("views/admin/settings.ejs", settings),
            # FIX N requires routes/upload.js to exist for any conversion
            # to even be attempted.
            ("routes/upload.js", "module.exports = require('express').Router();"),
        ]
        files_after = dict(build._ensure_upload_form_ui(files_before, self._mw_spec()))
        assert files_after["views/admin/settings.ejs"] == settings, (
            "FIX N must be byte-identical no-op on widget settings.ejs"
        )

    def test_fix_n_byte_identical_on_widget_crud_new_ejs(self):
        """FIX N also no-ops on widget-emitted CRUD new.ejs, image_url
        fields are already drag-drop zones with hidden inputs."""
        import build
        from cli.widgets.forms import emit_crud_new_edit
        spec = self._mw_spec()
        model = spec["data_models"][0]
        new_ejs, _ = emit_crud_new_edit(spec, model)
        files_before = [
            ("views/admin/events/new.ejs", new_ejs),
            ("routes/upload.js", "module.exports = require('express').Router();"),
        ]
        files_after = dict(build._ensure_upload_form_ui(files_before, spec))
        assert files_after["views/admin/events/new.ejs"] == new_ejs, (
            "FIX N must be byte-identical no-op on widget-emitted CRUD new.ejs"
        )

    def test_fix_o_unchanged_still_bundles_mw_logo(self):
        """FIX O continues to bundle the MW logo for MW-archetype specs.
        The widget rewrite does NOT change FIX O behavior."""
        import build
        spec = self._mw_spec()
        files = [("server.js", "app.listen(3000);")]
        files = build._ensure_mw_archetype_logo_bundled(files, spec)
        paths = [p for p, _ in files]
        assert "public/images/mw_logo.png" in paths, (
            "FIX O regression: MW logo must still be bundled for MW specs"
        )
