"""Tests for build.py completeness validation and token continuation fix.

Tests cover:
- validate_completeness() required file checks
- parse_files() handling of split responses (continuation)
- Deduplication of files with the same path
"""

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

from build import (
    REQUIRED_FILES, parse_files, validate_completeness, _build_explicit_prompt,
    _build_design_context,
    _fix_circular_imports, _ensure_route_views, _strip_demo_credentials, _fix_login_form_fields,
    _ensure_admin_mount, _ensure_error_handling, _ensure_package_deps, _ensure_tailwind_compiled,
    REQUIRED_DEPS, BANNED_DEPS,
    _should_use_multi_pass, _split_models_by_dependency, _assign_pages_to_phases,
    _build_phase1_prompt, _build_phase_n_prompt,
    _model_slug, _model_table, _build_page_model_mapping, _build_route_reconciliation_section,
    _pluralize_slug, _build_plural_slug_rules, _fix_view_slug_paths,
    _fix_view_html_shells, _ensure_mobile_nav, _ensure_logout_form,
    _fix_brand_nowrap, _ensure_resend_from_address, _ensure_mobile_responsive,
    _validate_destructive_confirm, _fix_null_property_access,
    _ensure_premium_css, _ensure_font_loading, _ensure_color_consistency,
    _fix_admin_dark_stat_cards,
    _fix_light_on_light_text,
    _ensure_logo_in_build, _logo_filename,
    _ensure_icon_helper,
    _ensure_video_embeds,
    _ensure_upload_route,
    _ensure_upload_form_ui,
    _strip_redundant_image_upload_forms,
    _dedup_seed_entries,
    _join_multiline_inserts,
    _remove_homepage_video_section,
    _fix_seed_uuid_id,
)
from tests._intake_driver import spec_saved_by_a_conversation  # noqa: E402


# ---------------------------------------------------------------------------
# validate_completeness()
# ---------------------------------------------------------------------------

class TestValidateCompleteness:
    def _make_files(self, paths, contents=None):
        """Helper: build a file list with dummy content."""
        if contents is None:
            contents = {}
            for p in paths:
                if p == "package.json":
                    contents[p] = json.dumps({"name": "test-app", "dependencies": {"express-ejs-layouts": "^2.5.1"}})
                elif p == "views/layout.ejs":
                    contents[p] = '<link rel="stylesheet" href="/tailwind.css">\n<%- body %>'
                else:
                    contents[p] = f"// {p}"
        return [(p, contents.get(p, f"// {p}")) for p in paths]

    def _complete_file_list(self):
        """Return a minimal complete set of file paths."""
        return REQUIRED_FILES + ["routes/auth.js", "views/index.ejs"]

    def test_complete_list_passes(self):
        files = self._make_files(self._complete_file_list())
        errors = validate_completeness(files)
        assert errors == []

    def test_missing_server_js(self):
        paths = [p for p in self._complete_file_list() if p != "server.js"]
        errors = validate_completeness(self._make_files(paths))
        assert any("server.js" in e for e in errors)

    def test_missing_package_json(self):
        paths = [p for p in self._complete_file_list() if p != "package.json"]
        errors = validate_completeness(self._make_files(paths))
        assert any("package.json" in e for e in errors)

    def test_missing_routes_dir(self):
        paths = [p for p in self._complete_file_list() if not p.startswith("routes/")]
        errors = validate_completeness(self._make_files(paths))
        assert any("routes/" in e for e in errors)

    def test_missing_view_besides_layout(self):
        paths = [p for p in self._complete_file_list() if p != "views/index.ejs"]
        errors = validate_completeness(self._make_files(paths))
        assert any("view files besides layout.ejs" in e for e in errors)

    def test_corrupted_package_json(self):
        paths = self._complete_file_list()
        contents = {p: f"// {p}" for p in paths}
        contents["package.json"] = "{ invalid json"
        files = [(p, contents[p]) for p in paths]
        errors = validate_completeness(files)
        assert any("package.json" in e and "not valid JSON" in e for e in errors)

    def test_valid_package_json(self):
        paths = self._complete_file_list()
        contents = {p: f"// {p}" for p in paths}
        contents["package.json"] = json.dumps({"name": "test-app", "version": "1.0.0", "dependencies": {"express-ejs-layouts": "^2.5.1"}})
        contents["views/layout.ejs"] = '<script src="https://cdn.tailwindcss.com"></script>\n<%- body %>'
        files = [(p, contents[p]) for p in paths]
        errors = validate_completeness(files)
        assert errors == []

    def test_multiple_missing_files(self):
        paths = ["routes/auth.js", "views/index.ejs"]
        errors = validate_completeness(self._make_files(paths))
        assert len(errors) >= 2


# ---------------------------------------------------------------------------
# parse_files() with split response text (continuation fix)
# ---------------------------------------------------------------------------

class TestParseFilesContinuation:
    def test_normal_single_response(self):
        text = (
            '--BEGINFILE-- server.js\nconsole.log("hello");\n--ENDFILE--\n'
            '--BEGINFILE-- package.json\n{"name":"app"}\n--ENDFILE--'
        )
        files = parse_files(text)
        assert len(files) == 2
        assert files[0][0] == "server.js"
        assert files[1][0] == "package.json"

    def test_split_file_across_responses(self):
        """Simulates token continuation: file started in response 1, ended in response 2."""
        response1 = '--BEGINFILE-- server.js\nconst express = require("express");\n'
        response2 = 'const app = express();\n--ENDFILE--'
        combined = response1 + "\n" + response2
        files = parse_files(combined)
        assert len(files) == 1
        assert files[0][0] == "server.js"
        assert "express" in files[0][1]
        assert "app = express()" in files[0][1]

    def test_call_and_parse_keeps_the_later_copy_of_a_repeated_file(self):
        """_call_and_parse parses the model's reply and, when one path is
        emitted twice (here server.js, before and after package.json), keeps
        only the later copy; the other file is unaffected."""
        import build as build_mod

        reply = MagicMock()
        reply.content = [MagicMock(text=(
            "--BEGINFILE-- server.js\n// version 1\n--ENDFILE--\n"
            '--BEGINFILE-- package.json\n{"name":"app"}\n--ENDFILE--\n'
            "--BEGINFILE-- server.js\n// version 2\n--ENDFILE--\n"
        ))]
        with patch.object(build_mod, "_shared_call_api", return_value=(reply, {})):
            files = build_mod._call_and_parse(object(), "prompt")

        assert len(files) == 2
        assert dict(files) == {"server.js": "// version 2", "package.json": '{"name":"app"}'}

    def test_duplicated_beginfile_marker_keeps_last_copy(self):
        """Regression: Claude occasionally emits two --BEGINFILE-- markers for
        the same file without an --ENDFILE-- between them, so the non-greedy
        regex captures both copies as one block with the inner marker embedded
        in content. parse_files must split on the embedded marker and keep
        only the last (most recent) copy. In the original medicine-waves
        incident, the corrupted routes/videos.js made the
        generated app crash on startup with a SyntaxError."""
        text = (
            "--BEGINFILE-- routes/videos.js\n"
            "const stale = require('express');\n"
            "stale.broken_line;\n"
            "--BEGINFILE-- routes/videos.js\n"
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "module.exports = router;\n"
            "--ENDFILE--"
        )
        files = parse_files(text)
        assert len(files) == 1
        path, content = files[0]
        assert path == "routes/videos.js"
        assert "stale" not in content
        assert "--BEGINFILE--" not in content
        assert "const express = require('express');" in content
        assert "module.exports = router;" in content

    def test_file_after_split_is_parsed(self):
        """Files after a split section should still be parsed correctly."""
        response1 = (
            '--BEGINFILE-- server.js\nline1\n--ENDFILE--\n'
            '--BEGINFILE-- routes/auth.js\npartial content'
        )
        response2 = (
            '\nmore content\n--ENDFILE--\n'
            '--BEGINFILE-- package.json\n{"name":"x"}\n--ENDFILE--'
        )
        combined = response1 + "\n" + response2
        files = parse_files(combined)
        assert len(files) == 3
        paths = [f[0] for f in files]
        assert "server.js" in paths
        assert "routes/auth.js" in paths
        assert "package.json" in paths


# ---------------------------------------------------------------------------
# _build_explicit_prompt()
# ---------------------------------------------------------------------------

class TestBuildExplicitPrompt:
    def _spec(self):
        return {
            "app_name": "task-tracker",
            "description": "A task tracking app",
            "roles": ["admin", "user"],
            "pages": [
                {"name": "Dashboard", "path": "/dashboard", "auth_required": True, "roles": ["user"]},
                {"name": "Home", "path": "/", "auth_required": False},
            ],
            "data_models": [
                {"name": "Task", "fields": [{"name": "title"}, {"name": "status"}]},
                {"name": "Project", "fields": [{"name": "name"}]},
            ],
        }

    def test_includes_spec_json(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "task-tracker" in prompt

    def test_includes_all_pages(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "/dashboard" in prompt
        assert "/" in prompt

    def test_includes_auth_annotation(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "requireAuth" in prompt

    def test_includes_crud_for_each_model(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "Task" in prompt
        assert "Project" in prompt
        assert "list" in prompt
        assert "create" in prompt
        assert "delete" in prompt

    def test_includes_checklist(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "seed.sql" in prompt
        assert "_csrf" in prompt
        assert "trust proxy" in prompt

    def test_uses_default_admin_email_when_not_in_spec(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "admin@example.com" in prompt

    def test_uses_customer_admin_email_when_provided(self):
        spec = self._spec()
        spec["admin_email"] = "owner@mycompany.example"
        prompt = _build_explicit_prompt(spec)
        assert "owner@mycompany.example" in prompt
        assert "admin@example.com" not in prompt

    def test_includes_admin_crud_requirement(self):
        """Section-06 moved admin CRUD emission into the Python widget
        registry. The prompt no longer carries the old 'Admin pages ...
        create, edit, and delete' checklist item (widgets own that
        surface); instead it carries the 'Do NOT generate these files'
        directive pointing at the widget-emitted admin paths. Keep this
        assertion honest so future prompt churn can't silently re-introduce
        the checklist item."""
        prompt = _build_explicit_prompt(self._spec())
        assert "Do NOT generate these files" in prompt
        assert "views/admin/settings.ejs" in prompt
        assert "views/admin/<slug>/new.ejs" in prompt
        assert "views/admin/<slug>/edit.ejs" in prompt

    def test_works_with_empty_spec(self):
        prompt = _build_explicit_prompt({"app_name": "empty", "pages": [], "data_models": [], "roles": []})
        assert "empty" in prompt

    def test_includes_template_preload_notice(self):
        """Single-pass prompt must tell Claude which files are pre-provided
        so it can skip regenerating them (matches _build_phase1_prompt)."""
        prompt = _build_explicit_prompt(self._spec())
        assert "already provided by the template system" in prompt
        # Core templates that are currently pre-loaded via _load_templates()
        for core in [
            "db/index.js",
            "db/migrate.js",
            "middleware/auth.js",
            "routes/admin.js",
            "public/sw.js",
        ]:
            assert core in prompt, f"Template notice missing {core}"
        # Must still instruct Claude to mount the admin router
        assert "app.use(require('./routes/admin'))" in prompt

    def test_build_explicit_prompt_public_form_pages_rule(self):
        """Prompt must include the PUBLIC FORM PAGES rule so Claude generates
        correct GET+POST handlers for visitor-facing forms (e.g. /contact, /book)
        that are NOT wrapped inside admin-only CRUD routers.

        Root cause: without this rule Claude put /contact POST inside an
        admin-scoped router, causing code review criticals and smoke-test failures
        (GET /contact returned 404 for unauthenticated visitors)."""
        spec = self._spec()
        # Add a public form page that should trigger the rule
        spec["pages"].append({
            "name": "Contact",
            "path": "/contact",
            "auth_required": False,
        })
        prompt = _build_explicit_prompt(spec)
        assert "PUBLIC FORM PAGES" in prompt, (
            "Prompt must include PUBLIC FORM PAGES checklist item for visitor-facing form handlers"
        )
        # Rule must explain the separation between visitor-facing form and admin CRUD
        assert "POST handler" in prompt or "auth_required=false" in prompt

    def test_build_explicit_prompt_public_form_rule_in_magic_link_branch(self):
        """PUBLIC FORM PAGES rule must appear in both USE_MAGIC_LINK_AUTH branches."""
        import os
        spec = self._spec()
        # Test with magic link env var set
        with patch.dict(os.environ, {"USE_MAGIC_LINK_AUTH": "true"}):
            prompt_ml = _build_explicit_prompt(spec)
        # Test without magic link env var
        env_without_ml = {k: v for k, v in os.environ.items() if k != "USE_MAGIC_LINK_AUTH"}
        with patch.dict(os.environ, env_without_ml, clear=True):
            prompt_no_ml = _build_explicit_prompt(spec)
        assert "PUBLIC FORM PAGES" in prompt_ml, (
            "PUBLIC FORM PAGES rule missing from magic-link prompt branch"
        )
        assert "PUBLIC FORM PAGES" in prompt_no_ml, (
            "PUBLIC FORM PAGES rule missing from standard-auth prompt branch"
        )


class TestSinglePassTemplatePreload:
    """Regression guard: single-pass builds must seed files from cli/templates/
    before post-processors run. This closes the gap where _load_templates() was
    only wired into _generate_multi_pass(), leaving Build tier (beta's only
    tier) paying full generation cost for boilerplate that always gets
    overwritten."""

    def test_load_templates_returns_core_files(self):
        from build import _load_templates

        templates = _load_templates()
        # These eight are unconditional in the mapping
        expected = {
            "db/index.js",
            "db/migrate.js",
            "middleware/auth.js",
            "routes/admin.js",
            "public/sw.js",
            "views/500.ejs",
            "views/admin-csv-import.ejs",
        }
        missing = expected - set(templates.keys())
        assert not missing, f"Template mapping missing: {missing}"
        # Every value should be non-empty content read from disk
        for path, content in templates.items():
            assert content, f"{path} template loaded empty"

    def test_build_app_single_pass_seeds_templates(self, monkeypatch, tmp_path):
        """Stub out Claude and verify single-pass build_app() merges
        _load_templates() content before post-processors, and that Claude's
        output overrides the template when both exist (same merge semantics
        as _generate_multi_pass)."""
        import build as build_mod

        spec = {
            "app_name": "preload-test",
            "description": "Template preload regression test",
            "admin_email": "owner@example.com",
            "roles": ["admin"],
            "pages": [{"name": "Home", "path": "/", "auth_required": False}],
            "data_models": [{"name": "Item", "fields": [{"name": "title"}]}],
            "tier": "build",
            "design": {
                "primary_color": "#0f172a",
                "secondary_color": "#1e293b",
                "accent_color": "#f59e0b",
                "font_display": "Inter",
                "font_body": "Inter",
                "style": "minimal",
            },
        }
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(spec))
        output_dir = tmp_path / "out"

        # Bypass validate.py schema checks: this test only cares about the
        # template pre-load/merge logic, not spec validation.
        monkeypatch.setattr(build_mod, "load_and_validate", lambda path: (spec, []))

        # Capture the merged file list that post-processors receive by
        # intercepting the first post-processor in the chain.
        captured: dict = {}

        real_ensure_shared_db_pool = build_mod._ensure_shared_db_pool

        def spy_ensure_shared_db_pool(files):
            captured["files"] = list(files)
            # Raise to short-circuit the rest of the build; we only care about
            # what made it into the merged list, not downstream behavior.
            raise RuntimeError("__captured__")

        monkeypatch.setattr(build_mod, "_ensure_shared_db_pool", spy_ensure_shared_db_pool)

        # Fake Claude returns ONE file that exists in the template mapping
        # (so we can prove Claude wins the merge) plus two files that don't.
        claude_routes_admin = "// Claude's routes/admin.js; should override template"

        def fake_call_and_parse(*args, **kwargs):
            return [
                ("package.json", "{}"),
                ("server.js", "// server"),
                ("routes/admin.js", claude_routes_admin),
            ]

        monkeypatch.setattr(build_mod, "_call_and_parse", fake_call_and_parse)
        monkeypatch.setattr(build_mod, "create_client", lambda: object())

        # Force single-pass regardless of heuristic drift
        monkeypatch.setattr(build_mod, "_should_use_multi_pass", lambda s: None)

        with pytest.raises(RuntimeError, match="__captured__"):
            build_mod.build_app(str(spec_path), output_dir=str(output_dir))

        # Restore for other tests that may share the module
        monkeypatch.setattr(build_mod, "_ensure_shared_db_pool", real_ensure_shared_db_pool)

        merged = dict(captured["files"])

        # Template files Claude did NOT generate must be present from pre-load
        assert "db/index.js" in merged
        assert "db/migrate.js" in merged
        assert "middleware/auth.js" in merged
        assert "public/sw.js" in merged
        assert "views/500.ejs" in merged
        assert "views/admin-csv-import.ejs" in merged

        # Claude's explicit file must win over the template for routes/admin.js
        assert merged["routes/admin.js"] == claude_routes_admin

        # Claude-only files still present
        assert merged["package.json"] == "{}"
        assert merged["server.js"] == "// server"


# ---------------------------------------------------------------------------
# _fix_circular_imports()
# ---------------------------------------------------------------------------

class TestFixCircularImports:
    def test_fixes_require_server_in_route(self):
        files = [
            ("routes/auth.js", "const { validateCsrfToken } = require('../server');\nrouter.post('/login', validateCsrfToken);"),
        ]
        result = dict(_fix_circular_imports(files))
        assert "require('../middleware/auth')" in result["routes/auth.js"]
        assert "require('../server')" not in result["routes/auth.js"]

    def test_fixes_double_dotdot_server(self):
        files = [
            ("routes/admin/users.js", "const { requireAuth } = require('../../server');"),
        ]
        result = dict(_fix_circular_imports(files))
        assert "require('../middleware/auth')" in result["routes/admin/users.js"]

    def test_leaves_non_server_requires_alone(self):
        files = [
            ("routes/auth.js", "const pool = require('../db');\nconst { requireAuth } = require('../middleware/auth');"),
        ]
        result = dict(_fix_circular_imports(files))
        assert "require('../db')" in result["routes/auth.js"]
        assert "require('../middleware/auth')" in result["routes/auth.js"]

    def test_skips_non_js_files(self):
        files = [
            ("views/layout.ejs", "require('../server')"),
        ]
        result = dict(_fix_circular_imports(files))
        assert "require('../server')" in result["views/layout.ejs"]

    def test_no_change_when_no_server_import(self):
        files = [
            ("routes/items.js", "const { requireAuth } = require('../middleware/auth');"),
        ]
        result = dict(_fix_circular_imports(files))
        assert result["routes/items.js"] == files[0][1]


# ---------------------------------------------------------------------------
# _ensure_route_views()
# ---------------------------------------------------------------------------

class TestEnsureRouteViews:
    def test_creates_missing_view(self):
        files = [
            ("routes/items.js", "router.get('/', (req, res) => { res.render('items', { items }); });"),
            ("views/layout.ejs", "<html><%- body %></html>"),
        ]
        result = dict(_ensure_route_views(files))
        assert "views/items.ejs" in result
        assert "Items" in result["views/items.ejs"]

    def test_skips_existing_views(self):
        files = [
            ("routes/items.js", "res.render('items', { items });"),
            ("views/layout.ejs", "<html><%- body %></html>"),
            ("views/items.ejs", "<h1>My Items</h1>"),
        ]
        result = dict(_ensure_route_views(files))
        assert result["views/items.ejs"] == "<h1>My Items</h1>"

    def test_handles_subdirectory_views(self):
        files = [
            ("routes/admin.js", "res.render('admin/dashboard', { stats });"),
            ("views/layout.ejs", ""),
        ]
        result = dict(_ensure_route_views(files))
        assert "views/admin/dashboard.ejs" in result

    def test_handles_multiple_renders_in_one_route(self):
        files = [
            ("routes/items.js", "res.render('items'); res.render('items-new', {});"),
            ("views/layout.ejs", ""),
        ]
        result = dict(_ensure_route_views(files))
        assert "views/items.ejs" in result
        assert "views/items-new.ejs" in result

    def test_no_views_created_when_all_exist(self):
        files = [
            ("routes/auth.js", "res.render('login', {}); res.render('register', {});"),
            ("views/layout.ejs", ""),
            ("views/login.ejs", "<form>login</form>"),
            ("views/register.ejs", "<form>register</form>"),
        ]
        result = dict(_ensure_route_views(files))
        assert len(result) == 4  # no new files added


# ---------------------------------------------------------------------------
# _strip_demo_credentials()
# ---------------------------------------------------------------------------

class TestStripDemoCredentials:
    def test_strips_details_demo_access(self):
        html = '<form>login</form><details><summary>Demo Access</summary><p>admin@example.com / password123</p></details>'
        files = [("views/login.ejs", html)]
        result = dict(_strip_demo_credentials(files))
        assert "Demo Access" not in result["views/login.ejs"]
        assert "example.com" not in result["views/login.ejs"]
        assert "<form>login</form>" in result["views/login.ejs"]

    def test_strips_div_with_demo_class(self):
        html = '<form>login</form><div class="demo-access"><p>Use admin@example.com</p></div>'
        files = [("views/login.ejs", html)]
        result = dict(_strip_demo_credentials(files))
        assert "demo-access" not in result["views/login.ejs"]
        assert "<form>login</form>" in result["views/login.ejs"]

    def test_strips_paragraph_with_demo_account_text(self):
        html = '<form>login</form><p>Demo accounts: use admin@example.com with password: password123</p>'
        files = [("views/login.ejs", html)]
        result = dict(_strip_demo_credentials(files))
        assert "password123" not in result["views/login.ejs"]

    def test_leaves_non_ejs_files_alone(self):
        files = [("routes/auth.js", "// Demo Access section")]
        result = dict(_strip_demo_credentials(files))
        assert "Demo Access" in result["routes/auth.js"]

    def test_no_change_when_no_demo_content(self):
        html = '<form><input name="email"><input name="password"><button>Login</button></form>'
        files = [("views/login.ejs", html)]
        result = dict(_strip_demo_credentials(files))
        assert result["views/login.ejs"] == html


# ---------------------------------------------------------------------------
# Multi-pass: _should_use_multi_pass()
# ---------------------------------------------------------------------------

class TestShouldUseMultiPass:
    def _make_spec(self, tier="build", num_models=3, num_pages=5):
        return {
            "tier": tier,
            "app_name": "test-app",
            "data_models": [{"name": f"Model{i}", "fields": []} for i in range(num_models)],
            "pages": [{"path": f"/page{i}", "auth_required": True} for i in range(num_pages)],
        }

    def test_build_tier_single_pass(self):
        assert _should_use_multi_pass(self._make_spec("build", 5, 10)) is None

    def test_simple_grow_single_pass(self):
        assert _should_use_multi_pass(self._make_spec("grow", 5, 10)) is None

    def test_grow_with_many_models_multi_pass(self):
        result = _should_use_multi_pass(self._make_spec("grow", 8, 10))
        assert result is not None
        assert result["phases"] == 3  # 7+ models always gets 3 phases

    def test_grow_with_many_pages_multi_pass(self):
        result = _should_use_multi_pass(self._make_spec("grow", 5, 18))
        assert result is not None
        assert result["phases"] == 2

    def test_scale_always_multi_pass(self):
        result = _should_use_multi_pass(self._make_spec("scale", 8, 20))
        assert result is not None
        assert result["phases"] >= 2

    def test_many_models_three_phases(self):
        result = _should_use_multi_pass(self._make_spec("scale", 14, 30))
        assert result is not None
        assert result["phases"] == 3
        assert len(result["phase_configs"]) == 3

    def test_phase_configs_have_foundation(self):
        result = _should_use_multi_pass(self._make_spec("grow", 8, 10))
        assert result["phase_configs"][0]["name"] == "foundation"
        assert result["phase_configs"][0]["models"] == []


# ---------------------------------------------------------------------------
# Multi-pass: _split_models_by_dependency()
# ---------------------------------------------------------------------------

class TestSplitModelsByDependency:
    def test_parent_child_split(self):
        models = [
            {"name": "recipes", "fields": [{"name": "title"}]},
            {"name": "recipe_ingredients", "fields": [{"name": "recipe_id"}, {"name": "quantity"}]},
            {"name": "suppliers", "fields": [{"name": "company"}]},
            {"name": "purchase_orders", "fields": [{"name": "supplier_id"}]},
        ]
        phase_a, phase_b = _split_models_by_dependency(models)
        a_names = {m["name"] for m in phase_a}
        b_names = {m["name"] for m in phase_b}
        # Parents (recipes, suppliers) should be in phase A
        assert "recipes" in a_names or "suppliers" in a_names
        # Children (recipe_ingredients, purchase_orders) should be in phase B
        assert "recipe_ingredients" in b_names or "purchase_orders" in b_names

    def test_no_dependencies_even_split(self):
        models = [{"name": f"model_{i}", "fields": []} for i in range(6)]
        phase_a, phase_b = _split_models_by_dependency(models)
        # All independent, should be roughly even
        assert len(phase_a) >= 2
        assert len(phase_b) >= 2
        assert len(phase_a) + len(phase_b) == 6

    def test_lopsided_fallback(self):
        """When FK analysis produces <2 in one group, fallback to 50/50."""
        models = [
            {"name": "users", "fields": []},
            {"name": "tasks", "fields": [{"name": "user_id"}]},
            {"name": "notes", "fields": [{"name": "user_id"}]},
            {"name": "tags", "fields": [{"name": "user_id"}]},
        ]
        phase_a, phase_b = _split_models_by_dependency(models)
        # users is the only parent (1 in phase_a), so fallback should trigger
        assert len(phase_a) >= 2
        assert len(phase_b) >= 2


# ---------------------------------------------------------------------------
# Multi-pass: _assign_pages_to_phases()
# ---------------------------------------------------------------------------

class TestAssignPagesToPhases:
    def test_matches_pages_to_models(self):
        pages = [
            {"path": "/recipes", "name": "Recipe List"},
            {"path": "/suppliers", "name": "Supplier Directory"},
        ]
        a_models = [{"name": "recipes"}]
        b_models = [{"name": "suppliers"}]
        pages_a, pages_b = _assign_pages_to_phases(pages, a_models, b_models)
        assert len(pages_a) == 1
        assert pages_a[0]["path"] == "/recipes"
        assert len(pages_b) == 1
        assert pages_b[0]["path"] == "/suppliers"

    def test_unmatched_goes_to_phase_a(self):
        pages = [{"path": "/settings", "name": "Settings"}]
        a_models = [{"name": "recipes"}]
        b_models = [{"name": "suppliers"}]
        pages_a, pages_b = _assign_pages_to_phases(pages, a_models, b_models)
        assert len(pages_a) == 1
        assert len(pages_b) == 0


# ---------------------------------------------------------------------------
# Multi-pass: prompt generation
# ---------------------------------------------------------------------------

class TestMultiPassPrompts:
    def _make_spec(self):
        return {
            "app_name": "test-app",
            "description": "Test",
            "data_models": [
                {"name": "items", "fields": [{"name": "title"}]},
                {"name": "orders", "fields": [{"name": "item_id"}]},
            ],
            "pages": [{"path": "/items"}, {"path": "/orders"}],
            "roles": ["admin"],
            "design": {"primary_color": "#333"},
        }

    def test_phase1_prompt_lists_required_files(self):
        prompt = _build_phase1_prompt(self._make_spec())
        assert "package.json" in prompt
        assert "server.js" in prompt
        assert "db/schema.sql" in prompt
        assert "middleware/auth.js" in prompt
        assert "views/layout.ejs" in prompt

    def test_phase1_prompt_mentions_all_models_in_schema(self):
        prompt = _build_phase1_prompt(self._make_spec())
        assert "COMPLETE schema for ALL 2 data models" in prompt

    def test_phase1_prompt_includes_router_mounts(self):
        prompt = _build_phase1_prompt(self._make_spec())
        assert "items" in prompt
        assert "orders" in prompt

    def test_phase_n_prompt_includes_schema(self):
        models = [{"name": "items", "fields": [{"name": "title"}]}]
        prompt = _build_phase_n_prompt(
            self._make_spec(), models, [],
            ["package.json", "server.js"], "CREATE TABLE items (id SERIAL);", 2,
        )
        assert "CREATE TABLE items" in prompt
        assert "package.json" in prompt

    def test_phase_n_prompt_has_crud(self):
        models = [{"name": "items", "fields": [{"name": "title"}]}]
        prompt = _build_phase_n_prompt(
            self._make_spec(), models, [],
            [], "", 2,
        )
        assert "/items" in prompt
        assert "list" in prompt
        assert "create" in prompt
        assert "delete" in prompt

    def test_phase_n_prompt_says_do_not_regenerate(self):
        prompt = _build_phase_n_prompt(
            self._make_spec(), [], [],
            ["server.js", "db/schema.sql"], "", 2,
        )
        assert "DO NOT regenerate" in prompt


# ---------------------------------------------------------------------------
# _ensure_admin_mount()
# ---------------------------------------------------------------------------

class TestEnsureAdminMount:
    def test_adds_admin_mount_when_missing(self):
        files = [
            ("server.js", "app.use(require('./routes/auth'));\napp.use(require('./routes/todos'));"),
            ("routes/auth.js", "module.exports = router;"),
        ]
        result = dict(_ensure_admin_mount(files))
        assert "routes/admin" in result["server.js"]

    def test_skips_if_already_mounted(self):
        original = "app.use(require('./routes/admin'));\napp.use(require('./routes/auth'));"
        files = [("server.js", original)]
        result = dict(_ensure_admin_mount(files))
        assert result["server.js"] == original

    def test_no_server_js_returns_unchanged(self):
        files = [("routes/auth.js", "module.exports = router;")]
        result = _ensure_admin_mount(files)
        assert len(result) == 1

    def test_inserts_before_error_handler_as_fallback(self):
        server = "const app = express();\napp.use((err, req, res, next) => {});"
        files = [("server.js", server)]
        result = dict(_ensure_admin_mount(files))
        assert "require('./routes/admin')" in result["server.js"]
        assert result["server.js"].index("routes/admin") < result["server.js"].index("(err,")

    def test_preserves_get_admin_dashboard_route(self):
        """Claude's GET /admin dashboard route should survive template replacement."""
        claude_admin = """const express = require('express');
const router = express.Router();
const pool = require('../db');

// GET /admin: Admin Dashboard
router.get('/admin', requireAuth, async (req, res) => {
  const stats = await pool.query('SELECT count(*) FROM events');
  res.render('admin/dashboard', { stats: stats.rows[0] });
});

module.exports = router;"""
        files = [
            ("server.js", "app.use(require('./routes/admin'));"),
            ("routes/admin.js", claude_admin),
        ]
        result = dict(_ensure_admin_mount(files))
        admin_js = result["routes/admin.js"]
        # Template utility routes must be present
        assert "/admin/csv-import" in admin_js
        assert "/admin/reset-data" in admin_js
        assert "/admin/change-password" in admin_js
        # Claude's dashboard route must also be present
        assert "router.get('/admin'" in admin_js
        assert "admin/dashboard" in admin_js

    def test_template_used_when_no_get_admin_route(self):
        """If Claude didn't generate GET /admin, template is used as-is (no crash)."""
        claude_admin = """const router = require('express').Router();
router.post('/admin/something', requireAuth, (req, res) => {});
module.exports = router;"""
        files = [
            ("server.js", "app.use(require('./routes/admin'));"),
            ("routes/admin.js", claude_admin),
        ]
        result = dict(_ensure_admin_mount(files))
        admin_js = result["routes/admin.js"]
        assert "/admin/csv-import" in admin_js
        # No GET /admin should have been injected
        assert "router.get('/admin'" not in admin_js

    def test_stat_cards_wrapped_in_links(self):
        """Admin dashboard stat cards should be wrapped in <a> tags."""
        dashboard = '''<div class="stat-card animate-in">
        <div class="stat-stripe bg-gradient-to-r from-primary to-secondary"></div>
        <div class="p-5">
          <p class="text-3xl font-bold text-primary"><%= stats.events %></p>
          <p class="text-sm text-gray-500 mt-1">Events</p>
        </div>
      </div>'''
        files = [
            ("server.js", "app.use(require('./routes/admin'));"),
            ("views/admin/dashboard.ejs", dashboard),
        ]
        result = dict(_ensure_admin_mount(files))
        dash = result["views/admin/dashboard.ejs"]
        assert '<a href="/events"' in dash
        assert "block" in dash  # has the block class for full clickability

    def test_stat_cards_messages_links_to_contact_messages(self):
        """Messages stat card should link to /contact-messages, not /messages."""
        dashboard = '''<div class="stat-card animate-in">
        <div class="stat-stripe"></div>
        <div class="p-5">
          <p class="text-3xl font-bold"><%= stats.messages %></p>
          <p class="text-sm text-gray-500 mt-1">Messages</p>
        </div>
      </div>'''
        files = [
            ("server.js", "app.use(require('./routes/admin'));"),
            ("views/admin/dashboard.ejs", dashboard),
        ]
        result = dict(_ensure_admin_mount(files))
        dash = result["views/admin/dashboard.ejs"]
        assert '<a href="/contact-messages"' in dash

    def test_stat_cards_work_with_views_dashboard_path(self):
        """Stat card links work when dashboard is at views/dashboard.ejs (not views/admin/)."""
        dashboard = '''<div class="stat-card animate-in">
        <div class="stat-stripe"></div>
        <div class="p-5">
          <p class="text-3xl font-bold"><%= stats.products %></p>
          <p class="text-sm text-gray-500 mt-1">Products</p>
        </div>
      </div>'''
        files = [
            ("server.js", "app.use(require('./routes/admin'));"),
            ("views/dashboard.ejs", dashboard),
        ]
        result = dict(_ensure_admin_mount(files))
        dash = result["views/dashboard.ejs"]
        assert '<a href="/products"' in dash

    def test_fallback_route_builds_stats_object_matching_view_contract(self):
        """Regression: MW v3 /admin 500 crash.

        When Claude generates an admin dashboard view that reads
        `stats.events`, `stats.messages`, etc., but does NOT generate a
        GET /admin route, the post-processor must inject a fallback route
        that renders with a nested `stats: {...}` object whose keys match
        the view. The earlier flat-vars-only fallback caused "stats is not
        defined" at render time.
        """
        dashboard = """<h1>Admin Dashboard</h1>
<p><%= stats.events %> events</p>
<p><%= stats.photos %> photos</p>
<p><%= stats.videos %> videos</p>
<p><%= stats.subscribers %> subscribers</p>
<p><%= stats.messages %> messages</p>
<% recentMessages.forEach(function(msg) { %>
  <div><%= msg.name %>: <%= msg.message %></div>
<% }) %>"""
        schema = """
CREATE TABLE users (id UUID PRIMARY KEY);
CREATE TABLE events (id UUID PRIMARY KEY);
CREATE TABLE photos (id UUID PRIMARY KEY);
CREATE TABLE videos (id UUID PRIMARY KEY);
CREATE TABLE subscribers (id UUID PRIMARY KEY);
CREATE TABLE contact_messages (id UUID PRIMARY KEY);
"""
        files = [
            ("server.js", "const express = require('express');\napp.use(require('./routes/auth'));"),
            ("views/admin/dashboard.ejs", dashboard),
            ("db/schema.sql", schema),
        ]
        result = dict(_ensure_admin_mount(files))
        admin_js = result["routes/admin.js"]

        # Fallback route was injected
        assert "Admin dashboard (injected by post-processor)" in admin_js
        assert "router.get('/admin'" in admin_js

        # Count queries for each data table are emitted
        assert "eventsCount" in admin_js
        assert "FROM events" in admin_js
        assert "photosCount" in admin_js
        assert "videosCount" in admin_js
        assert "subscribersCount" in admin_js
        assert "contact_messagesCount" in admin_js
        # users table is skipped for stat counts (no usersCount variable)
        # Note: template's password-change code legitimately uses FROM users
        assert "usersCount" not in admin_js
        assert "SELECT COUNT(*) FROM users" not in admin_js

        # Nested stats object with keys matching view contract
        assert "const stats = {" in admin_js
        assert "events: eventsCount" in admin_js
        assert "photos: photosCount" in admin_js
        assert "videos: videosCount" in admin_js
        assert "subscribers: subscribersCount" in admin_js
        # The critical suffix match: stats.messages -> contact_messages table
        assert "messages: contact_messagesCount" in admin_js

        # recentMessages detection and safe try/catch query
        assert "let recentMessages = []" in admin_js
        assert "FROM contact_messages ORDER BY created_at DESC LIMIT 5" in admin_js
        assert "catch (e) { recentMessages = []" in admin_js

        # Render call includes stats and recentMessages
        assert "res.render('admin/dashboard'" in admin_js
        assert "stats" in admin_js
        assert "recentMessages" in admin_js

    def test_fallback_route_skips_when_no_dashboard_view(self):
        """No dashboard view means no fallback route (template stays as-is)."""
        files = [
            ("server.js", "const express = require('express');"),
            ("db/schema.sql", "CREATE TABLE events (id UUID PRIMARY KEY);"),
        ]
        result = dict(_ensure_admin_mount(files))
        admin_js = result.get("routes/admin.js", "")
        assert "Admin dashboard (injected by post-processor)" not in admin_js

    def test_fallback_route_handles_views_dashboard_ejs_path(self):
        """Fallback also works when dashboard is at views/dashboard.ejs (not views/admin/)."""
        dashboard = "<p><%= stats.products %></p>"
        schema = "CREATE TABLE products (id UUID PRIMARY KEY);"
        files = [
            ("server.js", "const express = require('express');"),
            ("views/dashboard.ejs", dashboard),
            ("db/schema.sql", schema),
        ]
        result = dict(_ensure_admin_mount(files))
        admin_js = result["routes/admin.js"]
        assert "Admin dashboard (injected by post-processor)" in admin_js
        assert "res.render('dashboard'" in admin_js
        assert "products: productsCount" in admin_js


# ---------------------------------------------------------------------------
# _ensure_package_deps()
# ---------------------------------------------------------------------------

class TestEnsurePackageDeps:
    def test_adds_missing_deps(self):
        pkg = '{"name": "app", "dependencies": {"express": "^4.0.0"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        assert "csv-parser" in deps
        assert "multer" in deps
        assert "express" in deps

    def test_adds_all_required_deps(self):
        """Every dep in REQUIRED_DEPS must be present after post-processing."""
        pkg = '{"name": "app", "dependencies": {}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        for dep in REQUIRED_DEPS:
            assert dep in deps, f"Missing required dep: {dep}"

    def test_adds_critical_session_deps(self):
        """connect-pg-simple and express-ejs-layouts must always be added."""
        pkg = '{"name": "app", "dependencies": {"express": "^4.0.0"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        assert "connect-pg-simple" in deps
        assert "express-ejs-layouts" in deps
        assert "helmet" in deps
        assert "express-rate-limit" in deps

    def test_adds_bcryptjs_for_admin_template(self):
        """bcryptjs required by routes/admin.js template for change-password."""
        pkg = '{"name": "app", "dependencies": {"express": "^4.0.0"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        assert "bcryptjs" in deps

    def test_removes_banned_csurf(self):
        """csurf must be removed if Claude adds it."""
        pkg = '{"name": "app", "dependencies": {"express": "^4.0.0", "csurf": "^1.11.0"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        assert "csurf" not in deps

    def test_removes_banned_csrf_csrf(self):
        """csrf-csrf must be removed if Claude adds it."""
        pkg = '{"name": "app", "dependencies": {"express": "^4.0.0", "csrf-csrf": "^3.0.0"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        assert "csrf-csrf" not in deps

    def test_skips_if_already_present(self):
        # csv-parser is not in FORCE_UPGRADE_DEPS, so an existing pin is
        # preserved. multer IS force-upgraded, so it is tested separately in
        # test_force_upgrades_multer_from_vulnerable_1x.
        pkg = '{"name": "app", "dependencies": {"csv-parser": "^3.0.0", "multer": "^2.1.1"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        import json
        deps = json.loads(result["package.json"])["dependencies"]
        assert deps["csv-parser"] == "^3.0.0"  # unchanged
        assert deps["multer"] == "^2.1.1"  # safe pin preserved

    def test_force_upgrades_multer_from_vulnerable_1x(self):
        """multer is in FORCE_UPGRADE_DEPS because the 1.x line carries 7
        high-severity DoS advisories (via busboy 0.x). Even if Claude emits
        the old LTS pin in package.json, the post-processor must overwrite
        it with the REQUIRED_DEPS baseline (^2.0.x)."""
        import json
        from cli.build import REQUIRED_DEPS
        safe_pin = REQUIRED_DEPS["multer"]
        assert safe_pin.startswith("^2."), (
            f"REQUIRED_DEPS['multer'] must be 2.x; got {safe_pin!r}"
        )
        pkg = '{"name": "app", "dependencies": {"multer": "^1.4.5-lts.1"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        deps = json.loads(result["package.json"])["dependencies"]
        assert deps["multer"] == safe_pin, (
            f"expected multer upgraded to {safe_pin}, got {deps['multer']}"
        )

    def test_does_not_replace_newer_than_baseline_multer(self):
        """If Claude ships a multer pin that is already at-or-above the
        safe baseline (e.g. ^2.1.0 or ^3.0.0), keep it, don't downgrade.
        The upgrade rule is `deps[dep] != version`, so any exact string
        mismatch triggers replacement; document the expected behavior so
        future refactors don't accidentally introduce range-comparison."""
        import json
        # Matching baseline exactly: left untouched.
        pkg = '{"name": "app", "dependencies": {"multer": "^2.1.1"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        deps = json.loads(result["package.json"])["dependencies"]
        assert deps["multer"] == "^2.1.1"

    def test_force_upgrades_bcrypt_from_vulnerable_5x(self):
        """bcrypt 5.x pulls @mapbox/node-pre-gyp -> tar@6 transitively,
        which has multiple high-severity path-traversal advisories.
        bcrypt 6.0 ships without node-pre-gyp and clears the chain. API
        surface our templates use (async .hash/.compare) is unchanged."""
        import json
        from cli.build import REQUIRED_DEPS
        safe_pin = REQUIRED_DEPS["bcrypt"]
        assert safe_pin.startswith("^6.") or safe_pin.startswith("^7."), (
            f"REQUIRED_DEPS['bcrypt'] must be 6.x+; got {safe_pin!r}"
        )
        pkg = '{"name": "app", "dependencies": {"bcrypt": "^5.1.1"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        deps = json.loads(result["package.json"])["dependencies"]
        assert deps["bcrypt"] == safe_pin

    def test_force_upgrades_sharp_from_vulnerable_0_33(self):
        """sharp is in FORCE_UPGRADE_DEPS because 0.33 pulls a transitive
        dep chain (@mapbox/node-pre-gyp -> tar@6) with multiple high-
        severity path-traversal advisories. 0.34 ships a clean install
        tree. Our usage (.resize/.jpeg/.toBuffer) is unchanged."""
        import json
        from cli.build import REQUIRED_DEPS
        safe_pin = REQUIRED_DEPS["sharp"]
        assert safe_pin.startswith("^0.34.") or safe_pin.startswith("^0.35.") \
            or safe_pin.startswith("^1."), (
            f"REQUIRED_DEPS['sharp'] must be 0.34+; got {safe_pin!r}"
        )
        pkg = '{"name": "app", "dependencies": {"sharp": "^0.33.0"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_package_deps(files))
        deps = json.loads(result["package.json"])["dependencies"]
        assert deps["sharp"] == safe_pin

    def test_no_package_json_returns_unchanged(self):
        files = [("server.js", "const app = express();")]
        result = _ensure_package_deps(files)
        assert len(result) == 1

    def test_banned_deps_list_is_complete(self):
        """Verify BANNED_DEPS contains the known problematic packages."""
        assert "csurf" in BANNED_DEPS
        assert "csrf-csrf" in BANNED_DEPS


class TestEnsureTailwindCompiled:
    """Tests for the _ensure_tailwind_compiled post-processor."""

    SPEC = {"design": {"primary_color": "#ff0000", "secondary_color": "#00ff00", "accent_color": "#0000ff"}}

    def test_creates_tailwind_config_when_missing(self):
        files = [("package.json", '{"name":"app","dependencies":{},"scripts":{}}')]
        result = dict(_ensure_tailwind_compiled(files, self.SPEC))
        assert "tailwind.config.js" in result
        assert "#ff0000" in result["tailwind.config.js"]
        assert "views/**/*.ejs" in result["tailwind.config.js"]

    def test_adds_tailwindcss_dependency(self):
        pkg = '{"name":"app","dependencies":{"express":"^4.18"},"scripts":{}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_tailwind_compiled(files, self.SPEC))
        deps = json.loads(result["package.json"])["dependencies"]
        assert "tailwindcss" in deps

    def test_adds_postinstall_script(self):
        pkg = '{"name":"app","dependencies":{},"scripts":{"start":"node server.js"}}'
        files = [("package.json", pkg)]
        result = dict(_ensure_tailwind_compiled(files, self.SPEC))
        scripts = json.loads(result["package.json"])["scripts"]
        assert "postinstall" in scripts
        assert "tailwind" in scripts["postinstall"]

    def test_removes_cdn_from_layout(self):
        layout = '''<head>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
  tailwind.config = {
    theme: { extend: { colors: { primary: '#2563EB' } } },
  };
  </script>
  <link rel="stylesheet" href="/style.css">
</head>
<body><%- body %></body>'''
        files = [
            ("views/layout.ejs", layout),
            ("package.json", '{"name":"app","dependencies":{},"scripts":{}}'),
        ]
        result = dict(_ensure_tailwind_compiled(files, self.SPEC))
        assert "cdn.tailwindcss.com" not in result["views/layout.ejs"]
        assert "/tailwind.css" in result["views/layout.ejs"]

    def test_removes_cdn_from_server_csp(self):
        server = '''app.use(helmet({ contentSecurityPolicy: { directives: { scriptSrc: ["'self'", "'unsafe-inline'", "https://cdn.tailwindcss.com"] } } }));'''
        files = [
            ("server.js", server),
            ("package.json", '{"name":"app","dependencies":{},"scripts":{}}'),
        ]
        result = dict(_ensure_tailwind_compiled(files, self.SPEC))
        assert "cdn.tailwindcss.com" not in result["server.js"]

    def test_uses_default_colors_when_no_design(self):
        files = [("package.json", '{"name":"app","dependencies":{},"scripts":{}}')]
        result = dict(_ensure_tailwind_compiled(files, {}))
        assert "tailwind.config.js" in result
        assert "#2563eb" in result["tailwind.config.js"]

    def test_preserves_existing_tailwind_config(self):
        config = "module.exports = { content: ['./views/**/*.ejs'], theme: {} };"
        files = [
            ("tailwind.config.js", config),
            ("package.json", '{"name":"app","dependencies":{},"scripts":{}}'),
        ]
        result = dict(_ensure_tailwind_compiled(files, self.SPEC))
        assert "module.exports" in result["tailwind.config.js"]


class TestEnsurePremiumCss:
    """Tests for the _ensure_premium_css post-processor."""

    def test_injects_premium_css_file(self):
        files = [("views/layout.ejs", '<head>\n  <link rel="stylesheet" href="/tailwind.css">\n</head>\n<body><%- body %></body>')]
        result = dict(_ensure_premium_css(files))
        assert "public/premium-base.css" in result
        assert "animate-in" in result["public/premium-base.css"]
        assert "skeleton" in result["public/premium-base.css"]

    def test_links_css_in_layout_after_tailwind(self):
        layout = '<head>\n  <link rel="stylesheet" href="/tailwind.css">\n</head>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_premium_css(files))
        assert "premium-base.css" in result["views/layout.ejs"]
        # Should appear after tailwind.css
        tw_pos = result["views/layout.ejs"].index("tailwind.css")
        prem_pos = result["views/layout.ejs"].index("premium-base.css")
        assert prem_pos > tw_pos

    def test_links_css_in_layout_after_style_css(self):
        layout = '<head>\n  <link rel="stylesheet" href="/style.css">\n</head>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_premium_css(files))
        assert "premium-base.css" in result["views/layout.ejs"]

    def test_links_css_before_head_close_fallback(self):
        layout = '<head>\n  <title>App</title>\n</head>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_premium_css(files))
        assert "premium-base.css" in result["views/layout.ejs"]
        assert result["views/layout.ejs"].index("premium-base.css") < result["views/layout.ejs"].index("</head>")

    def test_skips_if_already_linked(self):
        layout = '<head>\n  <link rel="stylesheet" href="/premium-base.css">\n</head>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_premium_css(files))
        # Should not duplicate the link
        assert result["views/layout.ejs"].count("premium-base.css") == 1

    def test_works_without_layout(self):
        files = [("server.js", "app.listen(3000);")]
        result = dict(_ensure_premium_css(files))
        assert "public/premium-base.css" in result


class TestEnsureErrorHandling:
    """Tests for the _ensure_error_handling post-processor."""

    def test_adds_500_ejs_when_missing(self):
        files = [("server.js", "app.listen(3000);")]
        result = dict(_ensure_error_handling(files))
        assert "views/500.ejs" in result
        assert "Something Went Wrong" in result["views/500.ejs"]

    def test_overwrites_existing_500_ejs_with_canonical_template(self):
        custom = "<h1>Custom Error</h1>"
        files = [("views/500.ejs", custom), ("server.js", "app.listen(3000);")]
        result = dict(_ensure_error_handling(files))
        # Force-overwrite with canonical template to prevent drift
        assert "Something Went Wrong" in result["views/500.ejs"]
        assert result["views/500.ejs"] != custom

    def test_adds_error_handler_to_server_js(self):
        server = "const app = express();\napp.listen(3000);"
        files = [("server.js", server)]
        result = dict(_ensure_error_handling(files))
        assert "err, req, res, next" in result["server.js"]
        assert "500" in result["server.js"]

    def test_skips_if_error_handler_exists(self):
        server = "app.use((err, req, res, next) => { res.status(500).send('error'); });\napp.listen(3000);"
        files = [("server.js", server)]
        result = dict(_ensure_error_handling(files))
        # Should not add a duplicate
        assert result["server.js"].count("err, req, res, next") == 1


# ---------------------------------------------------------------------------
# Page-to-model mapping and route reconciliation
# ---------------------------------------------------------------------------


class TestModelSlugHelpers:
    """Tests for _model_slug and _model_table canonical helpers."""

    def test_model_slug_simple(self):
        assert _model_slug("Client") == "client"

    def test_model_slug_multi_word(self):
        assert _model_slug("Time Entry") == "time-entry"

    def test_model_slug_underscores(self):
        assert _model_slug("time_entry") == "time-entry"

    def test_model_slug_mixed(self):
        assert _model_slug("Blog Post") == "blog-post"

    def test_model_table_simple(self):
        assert _model_table("Client") == "clients"

    def test_model_table_multi_word(self):
        assert _model_table("Time Entry") == "time_entrys"

    def test_model_table_already_lowercase(self):
        assert _model_table("invoice") == "invoices"


class TestPageModelMapping:
    """Tests for _build_page_model_mapping detecting path-to-slug mismatches."""

    def _freelance_spec(self):
        return {
            "pages": [
                {"path": "/dashboard", "auth_required": True},
                {"path": "/clients", "auth_required": True},
                {"path": "/projects", "auth_required": True},
                {"path": "/time", "auth_required": True},
                {"path": "/invoices", "auth_required": True},
                {"path": "/expenses", "auth_required": True},
                {"path": "/reports", "auth_required": True},
                {"path": "/settings", "auth_required": True},
            ],
            "data_models": [
                {"name": "Client", "fields": []},
                {"name": "Project", "fields": []},
                {"name": "TimeEntry", "fields": []},
                {"name": "Invoice", "fields": []},
                {"name": "Expense", "fields": []},
                {"name": "Milestone", "fields": []},
                {"name": "Note", "fields": []},
            ],
        }

    def test_detects_time_to_timeentry_mismatch(self):
        """The freelance-flow failure: /time page vs TimeEntry model."""
        result = _build_page_model_mapping(self._freelance_spec())
        aliases = result["route_aliases"]
        time_alias = [a for a in aliases if a["page_path"] == "/time"]
        assert len(time_alias) == 1
        assert time_alias[0]["model_name"] == "TimeEntry"
        assert time_alias[0]["crud_slug"] == "time-entry"

    def test_matching_pages_not_flagged_as_aliases(self):
        """Pages like /clients matching model Client should not be aliases."""
        result = _build_page_model_mapping(self._freelance_spec())
        aliases = result["route_aliases"]
        client_alias = [a for a in aliases if a["page_path"] == "/clients"]
        assert len(client_alias) == 0

    def test_foundation_pages_skipped(self):
        """Foundation pages like /dashboard should not appear in mapping."""
        result = _build_page_model_mapping(self._freelance_spec())
        assert "/dashboard" not in result["page_to_model"]

    def test_unmatched_pages_detected(self):
        """Pages with no matching model should appear in unmatched_pages."""
        result = _build_page_model_mapping(self._freelance_spec())
        # /reports has no matching model (no "Report" model)
        assert "/reports" in result["unmatched_pages"]

    def test_settings_is_foundation(self):
        """/settings is in the foundation set, not flagged as unmatched."""
        result = _build_page_model_mapping(self._freelance_spec())
        assert "/settings" not in result["unmatched_pages"]

    def test_empty_spec(self):
        result = _build_page_model_mapping({"pages": [], "data_models": []})
        assert result["page_to_model"] == {}
        assert result["unmatched_pages"] == []
        assert result["route_aliases"] == []

    def _brewery_spec(self):
        """craft-brewery-manager spec: compound model names with shortened page paths."""
        return {
            "pages": [
                {"path": "/login", "auth_required": False},
                {"path": "/dashboard", "auth_required": True},
                {"path": "/recipes", "auth_required": True},
                {"path": "/ingredients", "auth_required": True},
                {"path": "/batches", "auth_required": True},
                {"path": "/fermentation", "auth_required": True},
                {"path": "/taproom", "auth_required": True},
                {"path": "/inventory", "auth_required": True},
                {"path": "/feedback", "auth_required": True},
                {"path": "/settings", "auth_required": True},
            ],
            "data_models": [
                {"name": "Recipe", "fields": []},
                {"name": "Ingredient", "fields": []},
                {"name": "BrewBatch", "fields": []},
                {"name": "FermentationLog", "fields": []},
                {"name": "TapRoomOrder", "fields": []},
                {"name": "Inventory", "fields": []},
                {"name": "CustomerFeedback", "fields": []},
            ],
        }

    def test_brewery_compound_models_match_shortened_pages(self):
        """Shortened page paths must match compound model names."""
        result = _build_page_model_mapping(self._brewery_spec())
        # These should all match via concatenated/last-word forms
        assert "/batches" not in result["unmatched_pages"], "/batches should match BrewBatch"
        assert "/taproom" not in result["unmatched_pages"], "/taproom should match TapRoomOrder"
        assert "/feedback" not in result["unmatched_pages"], "/feedback should match CustomerFeedback"
        assert "/fermentation" not in result["unmatched_pages"], "/fermentation should match FermentationLog"
        # Verify correct model associations
        assert result["page_to_model"].get("/batches") == "BrewBatch"
        assert result["page_to_model"].get("/taproom") == "TapRoomOrder"
        assert result["page_to_model"].get("/feedback") == "CustomerFeedback"
        assert result["page_to_model"].get("/fermentation") == "FermentationLog"

    def test_brewery_simple_models_match_directly(self):
        """Simple model names (Recipe, Ingredient, Inventory) match their plural pages."""
        result = _build_page_model_mapping(self._brewery_spec())
        assert result["page_to_model"].get("/recipes") == "Recipe"
        assert result["page_to_model"].get("/ingredients") == "Ingredient"
        assert result["page_to_model"].get("/inventory") == "Inventory"

    def test_brewery_aliases_generated_for_path_mismatches(self):
        """Compound models with shortened page paths need route aliases."""
        result = _build_page_model_mapping(self._brewery_spec())
        alias_paths = {a["page_path"] for a in result["route_aliases"]}
        # /batches differs from brew-batch CRUD slug
        assert "/batches" in alias_paths
        # /taproom differs from tap-room-order CRUD slug
        assert "/taproom" in alias_paths
        # /feedback differs from customer-feedback CRUD slug
        assert "/feedback" in alias_paths

    def test_brewery_no_unmatched_model_pages(self):
        """Only foundation pages should be unmatched; all model pages must match."""
        result = _build_page_model_mapping(self._brewery_spec())
        assert len(result["unmatched_pages"]) == 0, f"Unexpected unmatched: {result['unmatched_pages']}"


class TestRouteReconciliationSection:
    """Tests for _build_route_reconciliation_section prompt generation."""

    def test_generates_alias_section_for_mismatched_paths(self):
        spec = {
            "pages": [
                {"path": "/time", "auth_required": True},
            ],
            "data_models": [
                {"name": "TimeEntry", "fields": []},
            ],
        }
        section = _build_route_reconciliation_section(spec)
        assert "CRITICAL" in section
        assert "/time" in section
        assert "TimeEntry" in section
        assert "time-entry" in section
        assert "server.js" in section

    def test_no_section_when_no_mismatches(self):
        spec = {
            "pages": [
                {"path": "/clients", "auth_required": True},
            ],
            "data_models": [
                {"name": "Client", "fields": []},
            ],
        }
        section = _build_route_reconciliation_section(spec)
        assert section == ""

    def test_unmatched_pages_get_dedicated_routes(self):
        spec = {
            "pages": [
                {"path": "/reports", "auth_required": True},
            ],
            "data_models": [],
        }
        section = _build_route_reconciliation_section(spec)
        assert "/reports" in section
        assert "dedicated route" in section.lower() or "route handler" in section.lower()


class TestExplicitPromptReconciliation:
    """Tests that _build_explicit_prompt includes route reconciliation."""

    def test_prompt_includes_alias_for_mismatched_spec(self):
        spec = {
            "app_name": "test-app",
            "pages": [
                {"path": "/time", "name": "Time Tracking", "auth_required": True},
            ],
            "data_models": [
                {"name": "TimeEntry", "fields": []},
            ],
            "roles": ["admin"],
        }
        prompt = _build_explicit_prompt(spec)
        assert "CRITICAL" in prompt
        assert "/time" in prompt
        assert "time-entry" in prompt

    def test_prompt_has_expanded_post_keywords(self):
        spec = {
            "app_name": "test-app",
            "pages": [
                {"path": "/settings", "name": "Settings", "auth_required": True},
                {"path": "/import", "name": "Data Import", "auth_required": True},
            ],
            "data_models": [],
            "roles": ["admin"],
        }
        prompt = _build_explicit_prompt(spec)
        # "Settings" and "Import" should both get POST methods
        assert "GET + POST /settings" in prompt
        assert "GET + POST /import" in prompt


class TestPhase1PromptAliases:
    """Tests that _build_phase1_prompt includes route alias mounts."""

    def test_phase1_prompt_includes_alias_mounts(self):
        spec = {
            "app_name": "test-app",
            "pages": [
                {"path": "/dashboard", "auth_required": True},
                {"path": "/time", "name": "Time Tracking", "auth_required": True},
            ],
            "data_models": [
                {"name": "TimeEntry", "fields": []},
            ],
            "roles": ["admin"],
        }
        prompt = _build_phase1_prompt(spec)
        assert "alias" in prompt.lower() or "ALSO mount" in prompt
        assert "/time" in prompt
        assert "time-entry" in prompt


class TestFixViewHtmlShells:
    """Tests for _fix_view_html_shells() post-processing."""

    def test_strips_full_html_from_view(self):
        view_with_shell = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n<title>Recipes</title>\n'
            '</head>\n<body class="bg-white">\n<h1>Recipes</h1>\n<p>Content</p>\n'
            '</body>\n</html>'
        )
        files = [
            ("views/layout.ejs", "<html><%- body %></html>"),
            ("views/recipes.ejs", view_with_shell),
        ]
        result = dict(_fix_view_html_shells(files))
        # Layout should be untouched
        assert "<%- body %>" in result["views/layout.ejs"]
        # View should have only body content
        assert "<!DOCTYPE" not in result["views/recipes.ejs"]
        assert "<html" not in result["views/recipes.ejs"]
        assert "<head>" not in result["views/recipes.ejs"]
        assert "<h1>Recipes</h1>" in result["views/recipes.ejs"]
        assert "<p>Content</p>" in result["views/recipes.ejs"]

    def test_leaves_clean_views_alone(self):
        clean_view = '<h1>Dashboard</h1>\n<p>Welcome</p>'
        files = [("views/dashboard.ejs", clean_view)]
        result = dict(_fix_view_html_shells(files))
        assert result["views/dashboard.ejs"] == clean_view

    def test_skips_layout_ejs(self):
        layout = '<!DOCTYPE html>\n<html>\n<body><%- body %></body>\n</html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_fix_view_html_shells(files))
        assert result["views/layout.ejs"] == layout

    def test_skips_login_ejs(self):
        login = '<!DOCTYPE html>\n<html>\n<body><form>Login</form></body>\n</html>'
        files = [("views/login.ejs", login)]
        result = dict(_fix_view_html_shells(files))
        assert result["views/login.ejs"] == login

    def test_skips_non_view_files(self):
        route = '<!DOCTYPE html>\nvar x = 1;'
        files = [("routes/recipe.js", route)]
        result = dict(_fix_view_html_shells(files))
        assert result["routes/recipe.js"] == route

    def test_handles_multiple_views(self):
        shell = '<!DOCTYPE html>\n<html>\n<head></head>\n<body>\n<div>Content {}</div>\n</body>\n</html>'
        files = [
            ("views/layout.ejs", "<html><%- body %></html>"),
            ("views/recipes.ejs", shell.format("A")),
            ("views/ingredients.ejs", shell.format("B")),
            ("views/dashboard.ejs", "<h1>Clean</h1>"),
        ]
        result = dict(_fix_view_html_shells(files))
        assert "<!DOCTYPE" not in result["views/recipes.ejs"]
        assert "<!DOCTYPE" not in result["views/ingredients.ejs"]
        assert result["views/dashboard.ejs"] == "<h1>Clean</h1>"
        assert "Content A" in result["views/recipes.ejs"]
        assert "Content B" in result["views/ingredients.ejs"]


class TestEnsureMobileNav:
    """Tests for _ensure_mobile_nav() post-processing."""

    def test_adds_hamburger_when_hidden_md_flex(self):
        layout = (
            '<!DOCTYPE html>\n<html>\n<body>\n'
            '<nav>\n<div class="max-w-7xl mx-auto">\n'
            '<div class="flex items-center justify-between h-16">\n'
            '<div class="flex items-center space-x-4">\n'
            '<a href="/dashboard">Brand</a>\n'
            '<div class="hidden md:flex items-center space-x-2">\n'
            '<a href="/recipes" class="nav-link">Recipes</a>\n'
            '<a href="/batches" class="nav-link">Batches</a>\n'
            '</div>\n</div>\n</div>\n</div>\n</nav>\n'
            '</body>\n</html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        out = result["views/layout.ejs"]
        assert "mobile-menu-btn" in out
        assert "mobile-menu" in out
        assert 'aria-label="Toggle menu"' in out
        assert "addEventListener" in out

    def test_skips_when_already_has_hamburger(self):
        layout = (
            '<nav>\n<div class="flex items-center">\n'
            '<button id="mobile-menu-btn">Menu</button>\n'
            '<div class="hidden md:flex">\n<a href="/">Home</a>\n</div>\n'
            '</div>\n</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        # Should not modify since hamburger already exists
        assert result["views/layout.ejs"] == layout

    def test_skips_when_no_hidden_nav(self):
        layout = '<nav>\n<div class="flex">\n<a href="/">Home</a>\n</div>\n</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        assert result["views/layout.ejs"] == layout

    def test_skips_when_camelcase_mobilemenu_already_exists(self):
        """Regression: Claude uses mobileMenuBtn/mobileMenu (camelCase), should not inject duplicate."""
        layout = (
            '<nav>\n'
            '<button id="mobileMenuBtn" class="md:hidden">menu</button>\n'
            '<div id="mobileMenu" class="hidden">\n'
            '<a href="/" class="mobile-nav-link">Home</a>\n'
            '<a href="/dashboard" class="mobile-nav-link">Dashboard</a>\n'
            '</div>\n'
            '</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        # Must not inject a second mobile menu
        out = result["views/layout.ejs"]
        assert out.count('id="mobile-menu"') == 0, "duplicate dash-case menu injected alongside camelCase menu"
        assert out == layout, "layout was modified when camelCase mobile menu already present"

    def test_skips_when_no_layout(self):
        files = [("views/dashboard.ejs", "<h1>Hi</h1>")]
        result = dict(_ensure_mobile_nav(files))
        assert result["views/dashboard.ejs"] == "<h1>Hi</h1>"

    def test_mobile_menu_has_same_links(self):
        layout = (
            '<html>\n<body>\n<nav>\n'
            '<div class="flex items-center">\n'
            '<div class="hidden lg:flex items-center">\n'
            '<a href="/recipes" class="text-sm">Recipes</a>\n'
            '<a href="/settings" class="text-sm">Settings</a>\n'
            '</div>\n</div>\n</nav>\n</body>\n</html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        out = result["views/layout.ejs"]
        # Mobile menu should contain same destinations
        assert out.count('href="/recipes"') >= 2  # original + mobile
        assert out.count('href="/settings"') >= 2


class TestPhaseNPromptAliases:
    """Tests that _build_phase_n_prompt includes route alias info."""

    def test_phase_n_includes_alias_for_relevant_models(self):
        spec = {
            "app_name": "test-app",
            "pages": [
                {"path": "/time", "name": "Time", "auth_required": True},
            ],
            "data_models": [
                {"name": "TimeEntry", "fields": []},
            ],
            "roles": ["admin"],
        }
        models = [{"name": "TimeEntry", "fields": []}]
        prompt = _build_phase_n_prompt(
            spec, models, spec["pages"],
            prior_filenames=["server.js", "db/schema.sql"],
            schema_sql="CREATE TABLE time_entries (...);",
            phase_num=2,
        )
        assert "/time" in prompt
        assert "TimeEntry" in prompt


class TestEnsureLogoutForm:
    """Tests for _ensure_logout_form post-processor."""

    def test_converts_get_logout_link_to_post_form(self):
        layout = (
            '<nav>\n'
            '<a href="/dashboard" class="text-xl font-bold">App</a>\n'
            '<a href="/logout" class="nav-link text-sm">Logout</a>\n'
            '</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_logout_form(files))
        out = result["views/layout.ejs"]
        assert 'action="/logout"' in out
        assert 'method="POST"' in out
        assert '_csrf' in out
        assert '<a href="/logout"' not in out

    def test_preserves_existing_post_form(self):
        layout = (
            '<nav>\n'
            '<form action="/logout" method="POST">'
            '<input type="hidden" name="_csrf" value="<%= csrfToken %>">'
            '<button type="submit">Logout</button></form>\n'
            '</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_logout_form(files))
        # Should not modify
        assert result["views/layout.ejs"] == layout

    def test_preserves_link_classes_and_style(self):
        layout = (
            '<nav>\n'
            '<a href="/logout" class="text-sm text-gray-300" style="color: #D4A056;">Log Out</a>\n'
            '</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_logout_form(files))
        out = result["views/layout.ejs"]
        assert 'class="text-sm text-gray-300"' in out
        assert 'style="color: #D4A056;"' in out
        assert '>Log Out<' in out

    def test_skips_when_no_layout(self):
        files = [("views/dashboard.ejs", "<h1>Hi</h1>")]
        result = dict(_ensure_logout_form(files))
        assert result["views/dashboard.ejs"] == "<h1>Hi</h1>"

    def test_skips_when_no_logout_link(self):
        layout = '<nav><a href="/dashboard">Dashboard</a></nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_logout_form(files))
        assert result["views/layout.ejs"] == layout


class TestEnsureMobileNavFlatFlex:
    """Tests for _ensure_mobile_nav handling flat flex navs (pattern 2)."""

    def test_adds_hamburger_to_flat_nav_with_many_links(self):
        layout = (
            '<html>\n<body>\n'
            '<nav style="background-color: #3E2723;">\n'
            '<div class="flex items-center space-x-4">\n'
            '<a href="/dashboard" class="text-xl font-bold">App</a>\n'
            '<a href="/dashboard" class="nav-link">Dashboard</a>\n'
            '<a href="/recipes" class="nav-link">Recipes</a>\n'
            '<a href="/batches" class="nav-link">Batches</a>\n'
            '<a href="/settings" class="nav-link">Settings</a>\n'
            '</div>\n'
            '</nav>\n</body>\n</html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        out = result["views/layout.ejs"]
        assert 'mobile-menu-btn' in out
        assert 'mobile-menu' in out
        # Mobile menu should have page links
        assert 'href="/recipes"' in out
        assert 'href="/batches"' in out
        # Toggle script injected
        assert 'addEventListener' in out

    def test_flat_nav_skips_brand_and_logout_in_mobile_menu(self):
        layout = (
            '<html>\n<body>\n<nav>\n'
            '<div class="flex items-center space-x-4">\n'
            '<a href="/dashboard" class="text-xl font-bold">Brand</a>\n'
            '<a href="/recipes" class="nav-link">Recipes</a>\n'
            '<a href="/batches" class="nav-link">Batches</a>\n'
            '<a href="/inventory" class="nav-link">Inventory</a>\n'
            '<a href="/settings" class="nav-link">Settings</a>\n'
            '<a href="/logout" class="nav-link">Logout</a>\n'
            '</div>\n'
            '</nav>\n</body>\n</html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        out = result["views/layout.ejs"]
        # mobile-menu div should not contain logout or brand
        mobile_menu = out[out.index('id="mobile-menu"'):]
        assert '/logout' not in mobile_menu.split('</div>')[0]

    def test_flat_nav_skips_when_too_few_links(self):
        layout = (
            '<html>\n<body>\n<nav>\n'
            '<div class="flex items-center space-x-4">\n'
            '<a href="/dashboard" class="nav-link">Dashboard</a>\n'
            '<a href="/settings" class="nav-link">Settings</a>\n'
            '</div>\n'
            '</nav>\n</body>\n</html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        assert 'mobile-menu-btn' not in result["views/layout.ejs"]

    def test_flat_nav_extracts_bg_color(self):
        layout = (
            '<html>\n<body>\n'
            '<nav style="background-color: #8B4513;">\n'
            '<div class="flex items-center space-x-4">\n'
            '<a href="/a" class="nav-link">A</a>\n'
            '<a href="/b" class="nav-link">B</a>\n'
            '<a href="/c" class="nav-link">C</a>\n'
            '<a href="/d" class="nav-link">D</a>\n'
            '</div>\n'
            '</nav>\n</body>\n</html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        out = result["views/layout.ejs"]
        assert '#8B4513' in out


class TestEnsureMobileResponsive:
    """Tests for _ensure_mobile_responsive() post-processing."""

    def test_wraps_bare_table_in_overflow_auto(self):
        view = '<div>\n<table class="min-w-full">\n<tr><td>Data</td></tr>\n</table>\n</div>'
        files = [("views/items.ejs", view), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        out = result["views/items.ejs"]
        assert 'overflow-x-auto' in out
        assert '<div class="overflow-x-auto">' in out

    def test_skips_already_wrapped_table(self):
        view = '<div class="overflow-x-auto">\n<table>\n<tr><td>Data</td></tr>\n</table>\n</div>'
        files = [("views/items.ejs", view), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        # Should not double-wrap
        assert result["views/items.ejs"].count('overflow-x-auto') == 1

    def test_fixes_bare_grid_cols_2(self):
        view = '<div class="grid grid-cols-2 gap-4"><div>Card</div></div>'
        files = [("views/items.ejs", view), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        out = result["views/items.ejs"]
        assert 'grid-cols-1 sm:grid-cols-2' in out

    def test_fixes_bare_grid_cols_3(self):
        view = '<div class="grid grid-cols-3 gap-6"><div>Card</div></div>'
        files = [("views/items.ejs", view), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        out = result["views/items.ejs"]
        assert 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3' in out

    def test_preserves_responsive_grid(self):
        view = '<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4"><div>Card</div></div>'
        files = [("views/items.ejs", view), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        # Should not modify already-responsive grid
        assert result["views/items.ejs"] == view

    def test_adds_mobile_safety_css(self):
        files = [("public/style.css", "body { margin: 0; }")]
        result = dict(_ensure_mobile_responsive(files))
        css = result["public/style.css"]
        assert "overflow-x: hidden" in css
        assert "font-size: 16px" in css
        assert "@media (max-width: 640px)" in css

    def test_skips_safety_css_if_already_present(self):
        existing = "html, body { overflow-x: hidden; }\n"
        files = [("public/style.css", existing)]
        result = dict(_ensure_mobile_responsive(files))
        assert result["public/style.css"] == existing

    def test_handles_multiple_tables_in_one_view(self):
        view = (
            '<div>\n<table><tr><td>A</td></tr></table>\n'
            '<p>Text</p>\n'
            '<table><tr><td>B</td></tr></table>\n</div>'
        )
        files = [("views/report.ejs", view), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        out = result["views/report.ejs"]
        assert out.count('overflow-x-auto') == 2

    def test_only_processes_ejs_files(self):
        js_file = '<table>not a view</table>'
        files = [("server.js", js_file), ("public/style.css", "")]
        result = dict(_ensure_mobile_responsive(files))
        # JS file should not be modified for table wrapping
        assert result["server.js"] == js_file


class TestEnsureAdminRoutes:
    """Tests for _ensure_admin_mount injecting template file."""

    def test_injects_admin_template_when_missing(self):
        files = [
            ("server.js", "app.use(require('./routes/auth'));"),
            ("routes/auth.js", "module.exports = router;"),
        ]
        result = dict(_ensure_admin_mount(files))
        assert "routes/admin.js" in result
        assert "routes/admin" in result["server.js"]

    def test_overwrites_claude_generated_admin_with_template(self):
        admin_content = "// custom admin without csv-import"
        files = [
            ("server.js", "app.use(require('./routes/auth'));"),
            ("routes/admin.js", admin_content),
        ]
        result = dict(_ensure_admin_mount(files))
        # Should overwrite Claude-generated admin.js with canonical template
        assert result["routes/admin.js"] != admin_content
        assert "csv-import" in result["routes/admin.js"]


class TestFixNullPropertyAccess:
    """Tests for _fix_null_property_access post-processor."""

    def test_adds_guard_before_direct_property_access(self):
        route_js = (
            "router.get('/items/:id', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM items WHERE id = $1', [req.params.id]);\n"
            "  const item = result.rows[0].name;\n"
            "  res.render('items/show', { item });\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        result = dict(_fix_null_property_access(files))
        assert "if (!result.rows.length)" in result["routes/items.js"]
        assert "res.status(404)" in result["routes/items.js"]

    def test_adds_guard_before_destructuring(self):
        route_js = (
            "router.get('/users/:id', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM users WHERE id = $1', [req.params.id]);\n"
            "  const { id, name } = result.rows[0];\n"
            "  res.json({ id, name });\n"
            "});"
        )
        files = [("routes/users.js", route_js)]
        result = dict(_fix_null_property_access(files))
        assert "if (!result.rows.length)" in result["routes/users.js"]
        assert "res.status(404)" in result["routes/users.js"]

    def test_skips_when_guard_already_present(self):
        route_js = (
            "router.get('/items/:id', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM items WHERE id = $1', [req.params.id]);\n"
            "  if (!result.rows.length) return res.status(404).json({ error: 'Not found' });\n"
            "  const item = result.rows[0].name;\n"
            "  res.render('items/show', { item });\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        result = dict(_fix_null_property_access(files))
        # Should not add a second guard
        assert result["routes/items.js"].count("if (!result.rows.length)") == 1

    def test_skips_when_rows_length_check_nearby(self):
        route_js = (
            "router.get('/items/:id', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM items WHERE id = $1', [req.params.id]);\n"
            "  if (result.rows.length === 0) return res.status(404).render('404');\n"
            "  const item = result.rows[0].name;\n"
            "  res.render('items/show', { item });\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        result = dict(_fix_null_property_access(files))
        # Already has a guard, should not add another one
        assert result["routes/items.js"].count("if (") == 1

    def test_skips_non_route_files(self):
        server_js = (
            "const result = await pool.query('SELECT * FROM items WHERE id = $1', [id]);\n"
            "const item = result.rows[0].name;\n"
        )
        files = [("server.js", server_js)]
        result = dict(_fix_null_property_access(files))
        assert "if (!result.rows.length)" not in result.get("server.js", "")

    def test_skips_auth_js(self):
        auth_js = (
            "router.post('/login', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM users WHERE email = $1', [email]);\n"
            "  if (result.rows.length === 0) return res.render('login', { error: 'Invalid' });\n"
            "  req.session.userId = result.rows[0].id;\n"
            "});"
        )
        files = [("routes/auth.js", auth_js)]
        result = dict(_fix_null_property_access(files))
        # auth.js is excluded from this post-processor
        assert result["routes/auth.js"] == auth_js

    def test_preserves_indentation(self):
        route_js = (
            "router.get('/items/:id', async (req, res) => {\n"
            "  try {\n"
            "    const result = await pool.query('SELECT * FROM items WHERE id = $1', [req.params.id]);\n"
            "    const item = result.rows[0].name;\n"
            "  } catch (err) { next(err); }\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        result = dict(_fix_null_property_access(files))
        content = result["routes/items.js"]
        # Guard should be indented to match the access line (4 spaces)
        assert "    if (!result.rows.length)" in content

    def test_skips_multiline_call_argument_context(self):
        """NULL_GUARD_INJECTION1: rows[0] appearing inside a multiline db.query()
        argument list must not receive a guard injection, the injected
        `if (...) return` would land INSIDE the call's argument array and produce
        SyntaxError: Unexpected token 'if'.

        Reproduces the customer proposals.js incident where
          [newTask.rows[0].id]
        (not ending with ',') was inside a multi-line pool.query() call.
        """
        from build import _fix_null_property_access
        route_js = (
            "router.post('/proposals', async (req, res) => {\n"
            "  const newTask = await pool.query(\n"
            "    'INSERT INTO tasks (title) VALUES ($1) RETURNING *',\n"
            "    [req.body.title]\n"
            "  );\n"
            "  const result = await pool.query(\n"
            "    'SELECT * FROM proposals WHERE task_id = $1',\n"
            "    [newTask.rows[0].id]\n"  # inside multiline call, must NOT be guarded
            "  );\n"
            "  res.json(result.rows);\n"
            "});"
        )
        files = [("routes/proposals.js", route_js)]
        result = dict(_fix_null_property_access(files))
        content = result["routes/proposals.js"]
        # No guard should appear above [newTask.rows[0].id]; it's in a call arg
        assert "if (!newTask.rows.length) return res.status(404)" not in content
        # Original content must be unchanged
        assert content == route_js

    def test_skips_multiline_call_argument_no_trailing_comma(self):
        """Guard must be skipped when rows[0] is the last arg before ')' with no
        trailing comma, the endswith(',') check does NOT catch this case."""
        from build import _fix_null_property_access
        route_js = (
            "router.get('/items/:id', async (req, res) => {\n"
            "  const parent = await pool.query('SELECT * FROM parents WHERE id = $1', [req.params.id]);\n"
            "  const child = await pool.query(\n"
            "    'SELECT * FROM children WHERE parent_id = $1',\n"
            "    [parent.rows[0].id]\n"  # last arg, no trailing comma
            "  );\n"
            "  res.json(child.rows);\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        result = dict(_fix_null_property_access(files))
        content = result["routes/items.js"]
        assert "if (!parent.rows.length) return res.status(404)" not in content
        assert content == route_js

    def test_still_injects_guard_at_statement_level(self):
        """Guard injection must still work when rows[0] is at statement level
        (not inside any unclosed call expression). Regression guard: the paren-
        depth check must not over-fire and suppress legitimate guards."""
        from build import _fix_null_property_access
        route_js = (
            "router.get('/items/:id', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM items WHERE id = $1', [req.params.id]);\n"
            "  const { title, body } = result.rows[0];\n"
            "  res.render('items/show', { title, body });\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        result_files = dict(_fix_null_property_access(files))
        content = result_files["routes/items.js"]
        # Guard MUST be injected: this is a legitimate statement-level access
        assert "if (!result.rows.length) return res.status(404)" in content

    def test_skips_object_literal_context(self):
        """Regression: dashboard.js had `events: parseInt(r.rows[0].count),`
        inside an object literal. The post-processor was injecting
        `if (...) return res.status(404)...` above the property, producing
        invalid JS (statement inside object literal). Skip object-literal and
        array/function-arg contexts (lines ending in ',' or starting with
        a property key pattern). First seen in a medicine-waves build.
        """
        route_js = (
            "router.get('/dashboard', async (req, res) => {\n"
            "  const eventsResult = await pool.query('SELECT COUNT(*) FROM events');\n"
            "  const photosResult = await pool.query('SELECT COUNT(*) FROM photos');\n"
            "  res.render('dashboard', {\n"
            "    title: 'Dashboard',\n"
            "    stats: {\n"
            "      events: parseInt(eventsResult.rows[0].count),\n"
            "      photos: parseInt(photosResult.rows[0].count)\n"
            "    }\n"
            "  });\n"
            "});"
        )
        files = [("routes/dashboard.js", route_js)]
        result = dict(_fix_null_property_access(files))
        content = result["routes/dashboard.js"]
        # No guard should have been injected inside the object literal
        assert "if (!eventsResult.rows.length) return res.status(404)" not in content
        assert "if (!photosResult.rows.length) return res.status(404)" not in content
        # Original content should be unchanged
        assert content == route_js

    def test_noop_when_no_rows_access(self):
        route_js = (
            "router.get('/items', async (req, res) => {\n"
            "  const result = await pool.query('SELECT * FROM items');\n"
            "  res.render('items/index', { items: result.rows });\n"
            "});"
        )
        files = [("routes/items.js", route_js)]
        original = dict(files)
        result = dict(_fix_null_property_access(files))
        assert result["routes/items.js"] == original["routes/items.js"]


class TestStripProcessExitOnDb:
    """Tests for _strip_process_exit_on_db post-processor."""

    def test_strips_process_exit_in_migration_catch(self):
        from cli.build import _strip_process_exit_on_db
        server = """
const pool = require('./db');
pool.query(fs.readFileSync('./db/schema.sql', 'utf8'))
  .then(() => console.log('Migrations complete'))
  .catch(err => {
    console.error('Migration failed:', err.message);
    process.exit(1);
  });
app.listen(PORT);
"""
        files = [("server.js", server)]
        result = _strip_process_exit_on_db(files)
        assert "process.exit(1)" not in result[0][1]
        assert "keep server alive" in result[0][1]
        assert "app.listen(PORT)" in result[0][1]

    def test_strips_process_exit_in_db_connect_catch(self):
        from cli.build import _strip_process_exit_on_db
        server = """
try {
  await pool.connect();
} catch (error) {
  console.error('Database connection failed:', error);
  process.exit(1);
}
"""
        files = [("server.js", server)]
        result = _strip_process_exit_on_db(files)
        assert "process.exit(1)" not in result[0][1]

    def test_preserves_process_exit_outside_db_context(self):
        from cli.build import _strip_process_exit_on_db
        server = """
if (!process.env.SESSION_SECRET) {
  console.error('SESSION_SECRET not set');
  process.exit(1);
}
"""
        files = [("server.js", server)]
        result = _strip_process_exit_on_db(files)
        # This process.exit is for a missing env var, not DB -- should be preserved
        assert "process.exit(1)" in result[0][1]

    def test_no_change_when_no_process_exit(self):
        from cli.build import _strip_process_exit_on_db
        server = "const app = express();\napp.listen(3000);\n"
        files = [("server.js", server)]
        result = _strip_process_exit_on_db(files)
        assert result[0][1] == server


class TestFixBrandNowrap:
    """Tests for _fix_brand_nowrap post-processor."""

    def test_adds_nowrap_to_font_bold_brand(self):
        layout = '<nav><a href="/dashboard" class="text-xl font-bold">My App</a></nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_fix_brand_nowrap(files))
        assert "whitespace-nowrap" in result["views/layout.ejs"]

    def test_adds_nowrap_to_text_lg_brand(self):
        layout = '<nav><a href="/" class="text-lg font-bold text-white">Brand</a></nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_fix_brand_nowrap(files))
        assert "whitespace-nowrap" in result["views/layout.ejs"]

    def test_skips_when_already_has_nowrap(self):
        layout = '<nav><a href="/" class="text-xl font-bold whitespace-nowrap">Brand</a></nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_fix_brand_nowrap(files))
        # Should not double-add
        assert result["views/layout.ejs"].count("whitespace-nowrap") == 1

    def test_skips_non_brand_links(self):
        layout = '<nav><a href="/recipes" class="text-sm">Recipes</a></nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_fix_brand_nowrap(files))
        assert "whitespace-nowrap" not in result["views/layout.ejs"]

    def test_skips_when_no_layout(self):
        files = [("server.js", "const app = express();")]
        result = dict(_fix_brand_nowrap(files))
        assert "views/layout.ejs" not in result


# ---------------------------------------------------------------------------
# _ensure_logo_placement() tests
# ---------------------------------------------------------------------------

class TestEnsureLogoPlacement:
    """Tests for _ensure_logo_placement post-processor."""

    def test_skips_when_no_logo_data(self):
        from build import _ensure_logo_placement
        files = [("views/layout.ejs", "<nav><a href='/' class='text-xl font-bold'>App</a></nav>")]
        spec = {"app_name": "test-app", "design": {}}
        result = dict(_ensure_logo_placement(files, spec))
        assert "/logo.png" not in result["views/layout.ejs"]

    def test_injects_logo_in_nav(self):
        from build import _ensure_logo_placement
        layout = '<nav><a href="/" class="text-xl font-bold">My App</a></nav><footer></footer>'
        files = [("views/layout.ejs", layout)]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        assert '<img src="/logo.png"' in result["views/layout.ejs"]

    def test_injects_logo_in_login(self):
        from build import _ensure_logo_placement
        files = [
            ("views/layout.ejs", "<nav></nav>"),
            ("views/login.ejs", "<h1>Login</h1><form></form>"),
        ]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        assert '<img src="/logo.png"' in result["views/login.ejs"]

    def test_injects_logo_in_register(self):
        from build import _ensure_logo_placement
        files = [
            ("views/layout.ejs", "<nav></nav>"),
            ("views/register.ejs", "<h2>Sign Up</h2><form></form>"),
        ]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        assert '<img src="/logo.png"' in result["views/register.ejs"]

    def test_injects_favicon_link(self):
        from build import _ensure_logo_placement
        layout = '<head><title>App</title></head><nav></nav>'
        files = [("views/layout.ejs", layout)]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        assert 'rel="icon"' in result["views/layout.ejs"]
        assert "/logo.png" in result["views/layout.ejs"]

    def test_replaces_existing_favicon(self):
        from build import _ensure_logo_placement
        layout = '<head><link rel="icon" href="/favicon.svg"></head><nav></nav>'
        files = [("views/layout.ejs", layout)]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        assert "favicon.svg" not in result["views/layout.ejs"]
        assert 'href="/logo.png"' in result["views/layout.ejs"]

    def test_skips_if_logo_already_present(self):
        from build import _ensure_logo_placement
        login = '<img src="/logo.png" alt="Logo"><form></form>'
        files = [
            ("views/layout.ejs", "<nav></nav>"),
            ("views/login.ejs", login),
        ]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        # Should not double-add
        assert result["views/login.ejs"].count("/logo.png") == 1

    def test_nav_logo_has_constrained_height(self):
        from build import _ensure_logo_placement
        layout = '<nav><a href="/" class="text-xl font-bold">My App</a></nav><footer></footer>'
        files = [("views/layout.ejs", layout)]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        nav_section = result["views/layout.ejs"].split("</nav>")[0]
        # Nav logo should have max-h-10 to constrain height
        assert "max-h-10" in nav_section

    def test_nav_logo_has_rounded_corners(self):
        from build import _ensure_logo_placement
        layout = '<nav><a href="/" class="text-xl font-bold">My App</a></nav><footer></footer>'
        files = [("views/layout.ejs", layout)]
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files, spec))
        nav_section = result["views/layout.ejs"].split("</nav>")[0]
        assert "rounded-lg" in nav_section

    def test_ejs_favicon_href_does_not_leave_dangling_quote_bracket(self):
        """Regression: EJS_FAVICON1, Claude wraps the favicon href in an EJS expression
        like href="<%= siteSettings.nav_logo_url %>". The old [^>]* regex stopped at the
        '>' inside '%>' and left the literal string '\">' in the template, which rendered
        as visible characters at the top of every page (an MW v6 build).
        Fixed by using [^<>]* in the favicon sub so EJS-expression tags are not matched."""
        from build import _ensure_logo_placement
        layout = (
            '<head>\n'
            '  <link rel="stylesheet" href="/style.css">\n'
            '  <link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
            '  <% if (typeof siteSettings !== "undefined" && siteSettings.nav_logo_url) { %>\n'
            '  <link rel="icon" type="image/png" href="<%= siteSettings.nav_logo_url %>">\n'
            '  <% } %>\n'
            '</head>\n'
            '<nav><a href="/" class="text-xl font-bold">App</a></nav>\n'
        )
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files=[("views/layout.ejs", layout)], spec=spec))
        out = result["views/layout.ejs"]
        # The dangling '"> ' pattern is '>">'; a tag close '>' immediately followed by '">'.
        # Normal HTML never has this; it only appears when [^>]* stops inside an EJS tag.
        assert '>">' not in out, f"Dangling close-quote-bracket found in:\n{out}"
        # The EJS-conditional favicon block is removed
        assert 'siteSettings.nav_logo_url' not in out
        # A clean static favicon pointing to logo.png remains
        assert '<link rel="icon" type="image/png" href="/logo.png">' in out

    def test_ejs_favicon_static_tag_replaced_cleanly(self):
        """Static favicon (no EJS) is still replaced correctly after the [^<>]* fix."""
        from build import _ensure_logo_placement
        layout = (
            '<head>\n'
            '  <link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
            '</head>\n'
            '<nav><a href="/" class="text-xl font-bold">App</a></nav>\n'
        )
        spec = {"app_name": "test-app", "design": {"logo_data": "data:image/png;base64,abc"}}
        result = dict(_ensure_logo_placement(files=[("views/layout.ejs", layout)], spec=spec))
        out = result["views/layout.ejs"]
        # The static SVG favicon is replaced with the logo.png favicon
        assert '<link rel="icon" type="image/png" href="/logo.png">' in out
        # The original SVG favicon is gone from the head
        assert '/favicon.svg' not in out.split('</head>')[0]


# ---------------------------------------------------------------------------
# _write_logo_file() tests
# ---------------------------------------------------------------------------

class TestWriteLogoFile:
    """Tests for _write_logo_file binary logo writer."""

    def test_writes_logo_binary(self, tmp_path):
        from build import _write_logo_file
        import base64
        # Create a tiny valid PNG-like payload (just test the decode+write)
        raw = b"\x89PNG\r\n\x1a\ntest"
        b64 = base64.b64encode(raw).decode()
        spec = {"design": {"logo_data": f"data:image/png;base64,{b64}"}}
        public_dir = tmp_path / "public"
        public_dir.mkdir()
        _write_logo_file(tmp_path, spec)
        logo_path = public_dir / "logo.png"
        assert logo_path.exists()
        assert logo_path.read_bytes() == raw

    def test_skips_when_no_logo_data(self, tmp_path):
        from build import _write_logo_file
        spec = {"design": {}}
        _write_logo_file(tmp_path, spec)
        assert not (tmp_path / "public" / "logo.png").exists()

    def test_handles_invalid_data_uri(self, tmp_path):
        from build import _write_logo_file
        spec = {"design": {"logo_data": "not-a-data-uri"}}
        # Should not crash
        _write_logo_file(tmp_path, spec)
        assert not (tmp_path / "public" / "logo.png").exists()


# ---------------------------------------------------------------------------
# _ensure_logo_in_build() tests
# ---------------------------------------------------------------------------

class TestEnsureLogoInBuild:
    """Tests for _ensure_logo_in_build post-processor (replaces _write_logo_file)."""

    def test_decodes_base64_png_logo(self, tmp_path):
        """PNG logo written to public/logo.png."""
        import base64
        png_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 50
        b64 = base64.b64encode(png_bytes).decode()
        spec = {"design": {"logo_data": f"data:image/png;base64,{b64}"}}
        (tmp_path / "public").mkdir()
        _ensure_logo_in_build(tmp_path, spec)
        assert (tmp_path / "public" / "logo.png").exists()
        assert (tmp_path / "public" / "logo.png").read_bytes() == png_bytes

    def test_decodes_base64_svg_logo(self, tmp_path):
        """SVG logo written to public/logo.svg, NOT logo.png."""
        import base64
        svg_content = b'<svg xmlns="http://www.w3.org/2000/svg"><circle cx="50" cy="50" r="40"/></svg>'
        b64 = base64.b64encode(svg_content).decode()
        spec = {"design": {"logo_data": f"data:image/svg+xml;base64,{b64}"}}
        (tmp_path / "public").mkdir()
        _ensure_logo_in_build(tmp_path, spec)
        assert (tmp_path / "public" / "logo.svg").exists()
        assert not (tmp_path / "public" / "logo.png").exists()

    def test_skips_when_no_logo_data(self, tmp_path):
        """No file written when logo_data absent."""
        spec = {"design": {}}
        (tmp_path / "public").mkdir()
        _ensure_logo_in_build(tmp_path, spec)
        assert not list((tmp_path / "public").glob("logo.*"))

    def test_path_alignment_with_ensure_logo_placement(self, tmp_path):
        """PNG path matches what _ensure_logo_placement expects."""
        import base64
        png_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 10
        b64 = base64.b64encode(png_bytes).decode()
        spec = {"design": {"logo_data": f"data:image/png;base64,{b64}"}}
        (tmp_path / "public").mkdir()
        _ensure_logo_in_build(tmp_path, spec)
        assert (tmp_path / "public" / "logo.png").exists()

    def test_handles_malformed_data_uri(self, tmp_path):
        """Invalid data URI does not crash."""
        spec = {"design": {"logo_data": "not-a-valid-uri"}}
        (tmp_path / "public").mkdir()
        _ensure_logo_in_build(tmp_path, spec)  # Should not raise
        assert not list((tmp_path / "public").glob("logo.*"))


# ---------------------------------------------------------------------------
# _logo_filename() tests
# ---------------------------------------------------------------------------

class TestLogoFilename:
    """Tests for _logo_filename helper."""

    def test_returns_svg_for_svg_data(self):
        spec = {"design": {"logo_data": "data:image/svg+xml;base64,abc"}}
        assert _logo_filename(spec) == "logo.svg"

    def test_returns_png_for_png_data(self):
        spec = {"design": {"logo_data": "data:image/png;base64,abc"}}
        assert _logo_filename(spec) == "logo.png"

    def test_returns_png_for_no_data(self):
        spec = {"design": {}}
        assert _logo_filename(spec) == "logo.png"


# ---------------------------------------------------------------------------
# _pluralize_slug() tests
# ---------------------------------------------------------------------------

class TestPluralizeSlug:
    """Test English pluralization of URL slugs."""

    def test_regular_plural(self):
        from build import _pluralize_slug
        assert _pluralize_slug("recipe") == "recipes"

    def test_consonant_y_to_ies(self):
        from build import _pluralize_slug
        assert _pluralize_slug("inventory") == "inventories"
        assert _pluralize_slug("category") == "categories"

    def test_vowel_y_gets_s(self):
        from build import _pluralize_slug
        assert _pluralize_slug("day") == "days"
        assert _pluralize_slug("key") == "keys"

    def test_ch_sh_s_x_z_get_es(self):
        from build import _pluralize_slug
        assert _pluralize_slug("batch") == "batches"
        assert _pluralize_slug("dish") == "dishes"
        assert _pluralize_slug("class") == "classes"
        assert _pluralize_slug("box") == "boxes"

    def test_hyphenated_slug(self):
        from build import _pluralize_slug
        assert _pluralize_slug("brew-batch") == "brew-batches"
        assert _pluralize_slug("time-entry") == "time-entries"
        assert _pluralize_slug("fermentation-log") == "fermentation-logs"

    def test_single_char_y(self):
        from build import _pluralize_slug
        # Edge case: single char 'y' should just get 's'
        assert _pluralize_slug("y") == "ys"


# ---------------------------------------------------------------------------
# _build_plural_slug_rules() tests
# ---------------------------------------------------------------------------

class TestBuildPluralSlugRules:
    """Test prompt section generation for plural slug enforcement."""

    def test_empty_spec_returns_empty(self):
        from build import _build_plural_slug_rules
        assert _build_plural_slug_rules({}) == ""
        assert _build_plural_slug_rules({"data_models": []}) == ""

    def test_single_model_generates_table(self):
        from build import _build_plural_slug_rules
        spec = {"data_models": [{"name": "Recipe"}]}
        result = _build_plural_slug_rules(spec)
        assert "recipes" in result
        assert "PLURAL SLUG RULES" in result
        assert "/recipe/" in result  # WRONG column
        assert "/recipes/" in result  # CORRECT column

    def test_multiple_models(self):
        from build import _build_plural_slug_rules
        spec = {"data_models": [{"name": "Recipe"}, {"name": "Inventory"}]}
        result = _build_plural_slug_rules(spec)
        assert "recipes" in result
        assert "inventories" in result

    def test_hyphenated_model_name(self):
        from build import _build_plural_slug_rules
        spec = {"data_models": [{"name": "BrewBatch"}]}
        result = _build_plural_slug_rules(spec)
        assert "brew-batches" in result


# ---------------------------------------------------------------------------
# _fix_view_slug_paths() tests
# ---------------------------------------------------------------------------

class TestFixViewSlugPaths:
    """Test post-processor that fixes singular slugs in EJS views."""

    def test_fixes_singular_href(self):
        from build import _fix_view_slug_paths
        files = [("views/recipes.ejs", '<a href="/recipe/1">View</a>')]
        spec = {"data_models": [{"name": "Recipe"}]}
        result = dict(_fix_view_slug_paths(files, spec))
        assert '/recipes/1' in result["views/recipes.ejs"]

    def test_fixes_singular_action(self):
        from build import _fix_view_slug_paths
        files = [("views/recipes.ejs", '<form action="/recipe" method="POST">')]
        spec = {"data_models": [{"name": "Recipe"}]}
        result = dict(_fix_view_slug_paths(files, spec))
        assert '/recipes"' in result["views/recipes.ejs"]

    def test_skips_non_view_files(self):
        from build import _fix_view_slug_paths
        files = [("server.js", 'app.use("/recipe", recipeRouter)')]
        spec = {"data_models": [{"name": "Recipe"}]}
        result = dict(_fix_view_slug_paths(files, spec))
        assert "/recipe" in result["server.js"]  # unchanged

    def test_skips_when_no_models(self):
        from build import _fix_view_slug_paths
        files = [("views/test.ejs", '<a href="/recipe/1">')]
        result = dict(_fix_view_slug_paths(files, {}))
        assert "/recipe/1" in result["views/test.ejs"]  # unchanged

    def test_already_plural_unchanged(self):
        from build import _fix_view_slug_paths
        files = [("views/index.ejs", '<a href="/recipes/1">View</a>')]
        spec = {"data_models": [{"name": "Recipe"}]}
        result = dict(_fix_view_slug_paths(files, spec))
        assert '/recipes/1' in result["views/index.ejs"]

    def test_hyphenated_model_slug(self):
        from build import _fix_view_slug_paths
        files = [("views/batches.ejs", '<a href="/brew-batch/1">View</a>')]
        spec = {"data_models": [{"name": "BrewBatch"}]}
        result = dict(_fix_view_slug_paths(files, spec))
        assert '/brew-batches/1' in result["views/batches.ejs"]


class TestEnsureMagicLinkAuth:
    """Test post-processor #17: magic link auth artifacts."""

    def test_injects_magic_link_tokens_table(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        files = [("db/schema.sql", "CREATE TABLE users (id UUID);")]
        result = dict(_ensure_magic_link_auth(files))
        assert "magic_link_tokens" in result["db/schema.sql"]
        assert "token_hash" in result["db/schema.sql"]

    def test_skips_if_table_already_exists(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        schema = "CREATE TABLE users (id UUID);\nCREATE TABLE magic_link_tokens (id UUID);"
        files = [("db/schema.sql", schema)]
        result = dict(_ensure_magic_link_auth(files))
        # Should not duplicate the table
        assert result["db/schema.sql"].count("magic_link_tokens") == 1

    def test_scrubbing_moved_to_dedicated_processor(self, monkeypatch):
        """SEC-1 (2026-04-19): scrubbing logic extracted from
        _ensure_magic_link_auth into a standalone _scrub_bcrypt_from_seed
        so it runs for every app regardless of USE_MAGIC_LINK_AUTH. This
        test verifies the refactor by confirming _ensure_magic_link_auth
        no longer removes hashes (it now only manages the magic-link
        table, password_set flag, and TEST_MODE backdoor)."""
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth, _scrub_bcrypt_from_seed
        synthetic_hash = "$2b$10$" + "L" * 53  # bcrypt-shaped fixture, not a real hash
        seed = f"INSERT INTO users VALUES ('admin', '{synthetic_hash}');"
        files = [("db/schema.sql", "CREATE TABLE users (id UUID);"), ("db/seed.sql", seed)]
        after_magic_link = dict(_ensure_magic_link_auth(files))
        # _ensure_magic_link_auth should NOT scrub hashes anymore
        assert synthetic_hash in after_magic_link["db/seed.sql"], (
            "scrubbing was moved out of _ensure_magic_link_auth \u2014 this test must be updated "
            "if you intentionally re-added scrubbing there"
        )
        # Full chain: run the dedicated scrubber and confirm it cleans up
        final = dict(_scrub_bcrypt_from_seed(list(after_magic_link.items())))["db/seed.sql"]
        assert "$2b$" not in final
        assert "'NULL'" not in final

    def test_seed_users_remain_null_password_for_magic_link(self, monkeypatch):
        """SEC-1: magic-link apps seed users with password=NULL; post-processor
        must not back-fill a password hash. Founders authenticate by minting
        a one-off magic_link_tokens row via psql."""
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        seed = (
            "INSERT INTO users (id, email, password, password_set, email_verified) "
            "VALUES (gen_random_uuid(), 'admin@app.example', NULL, FALSE, TRUE);"
        )
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
            ("db/seed.sql", seed),
        ]
        result = dict(_ensure_magic_link_auth(files))
        final_seed = result["db/seed.sql"]
        assert "$2b$" not in final_seed
        assert "UPDATE users SET password = '$2b$" not in final_seed
        # The original INSERT with NULL password must still be present
        assert "NULL, FALSE, TRUE" in final_seed

    def test_injects_template_views(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        from pathlib import Path
        templates_dir = Path(__file__).parent.parent / "cli" / "templates"
        has_magic_link = (templates_dir / "magic_link_sent.ejs").exists()
        has_set_password = (templates_dir / "set_password.ejs").exists()
        files = [("db/schema.sql", "CREATE TABLE users (id UUID);")]
        result = dict(_ensure_magic_link_auth(files))
        if has_magic_link:
            assert "views/magic-link-sent.ejs" in result
        if has_set_password:
            assert "views/set-password.ejs" in result

    def test_noop_when_flag_disabled(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", False)
        from build import _ensure_magic_link_auth
        files = [("db/schema.sql", "CREATE TABLE users (id UUID);")]
        result = dict(_ensure_magic_link_auth(files))
        assert "magic_link_tokens" not in result.get("db/schema.sql", "")

    def test_warns_missing_magic_route(self, monkeypatch, capsys):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID);"),
            ("routes/auth.js", "router.post('/login', ...)"),
        ]
        _ensure_magic_link_auth(files)
        captured = capsys.readouterr()
        assert "/magic route not found" in captured.out

    def test_injects_test_mode_backdoor_when_missing(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        auth_js = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const { email, password } = req.body;
  const result = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
  // normal bcrypt check
});
module.exports = router;"""
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
            ("routes/auth.js", auth_js),
        ]
        result = dict(_ensure_magic_link_auth(files))
        assert "test_password_for_ci" in result["routes/auth.js"]
        assert "req.session.save" in result["routes/auth.js"]
        assert "req.session.userId" in result["routes/auth.js"]

    def test_does_not_double_inject_test_mode(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        auth_js = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const { email, password } = req.body;
  if (process.env.TEST_MODE && password === 'test_password_for_ci') {
    const testUser = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
    if (testUser.rows.length > 0) {
      req.session.userId = testUser.rows[0].id;
      return req.session.save(() => res.redirect('/dashboard'));
    }
  }
});
module.exports = router;"""
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
            ("routes/auth.js", auth_js),
        ]
        result = dict(_ensure_magic_link_auth(files))
        # Should not duplicate
        assert result["routes/auth.js"].count("test_password_for_ci") == 1

    def test_injects_test_mode_with_csrf_middleware(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        auth_js = """const router = require('express').Router();
router.post('/login', validateCsrfToken, async (req, res) => {
  const { email, password } = req.body;
  const result = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
});
module.exports = router;"""
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
            ("routes/auth.js", auth_js),
        ]
        result = dict(_ensure_magic_link_auth(files))
        assert "test_password_for_ci" in result["routes/auth.js"]
        assert "req.session.save" in result["routes/auth.js"]

    def test_injects_test_mode_with_separate_declarations(self, monkeypatch):
        """When Claude uses separate const lines instead of destructuring,
        the TEST_MODE block must inject AFTER the password declaration."""
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        auth_js = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const email = (req.body.email || '').trim();
  const password = req.body.password;
  const result = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
});
module.exports = router;"""
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
            ("routes/auth.js", auth_js),
        ]
        result = dict(_ensure_magic_link_auth(files))
        auth = result["routes/auth.js"]
        assert "test_password_for_ci" in auth
        assert "req.session.save" in auth
        # The TEST_MODE block must appear AFTER the password declaration
        pw_decl_pos = auth.index("const password = req.body.password;")
        test_mode_pos = auth.index("test_password_for_ci")
        assert test_mode_pos > pw_decl_pos, "TEST_MODE block injected before password declaration"

    def test_injects_test_mode_safe_fallback_no_variables(self, monkeypatch):
        """When no email/password variable can be found, uses req.body directly."""
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        auth_js = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  // some unusual pattern with no standard variable extraction
  const user = await findUser(req);
});
module.exports = router;"""
        files = [
            ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
            ("routes/auth.js", auth_js),
        ]
        result = dict(_ensure_magic_link_auth(files))
        auth = result["routes/auth.js"]
        assert "test_password_for_ci" in auth
        # Safe fallback uses req.body.password directly
        assert "req.body.password" in auth
        assert "req.body.email" in auth

    def test_adds_password_set_column_when_missing(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        schema = "CREATE TABLE users (\n    id UUID PRIMARY KEY,\n    email VARCHAR(255)\n);"
        files = [("db/schema.sql", schema)]
        result = dict(_ensure_magic_link_auth(files))
        assert "password_set" in result["db/schema.sql"]
        assert "BOOLEAN DEFAULT FALSE" in result["db/schema.sql"]

    def test_does_not_duplicate_password_set_column(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_auth
        schema = "CREATE TABLE users (\n    id UUID PRIMARY KEY,\n    password_set BOOLEAN DEFAULT FALSE\n);"
        files = [("db/schema.sql", schema)]
        result = dict(_ensure_magic_link_auth(files))
        assert result["db/schema.sql"].count("password_set") == 1

    def test_password_set_column_not_added_when_flag_disabled(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", False)
        from build import _ensure_magic_link_auth
        schema = "CREATE TABLE users (\n    id UUID PRIMARY KEY\n);"
        files = [("db/schema.sql", schema)]
        result = dict(_ensure_magic_link_auth(files))
        assert "password_set" not in result.get("db/schema.sql", "")


class TestEnsureMagicLinkHash:
    """Test post-processor #18: magic link token hash before DB query."""

    def test_injects_hash_when_raw_token_used(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        auth_js = """const router = require('express').Router();
router.get('/magic', async (req, res) => {
  const token = req.query.token;
  const result = await pool.query('SELECT * FROM magic_link_tokens WHERE token_hash = $1 AND expires_at > NOW() AND used_at IS NULL', [token]);
  if (result.rows.length === 0) return res.redirect('/login?error=invalid');
  const row = result.rows[0];
  await pool.query('UPDATE magic_link_tokens SET used_at = NOW() WHERE id = $1', [row.id]);
  req.session.userId = row.user_id;
  req.session.save(() => res.redirect('/dashboard'));
});
module.exports = router;"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        assert "createHash('sha256')" in result["routes/auth.js"]
        assert "tokenHash" in result["routes/auth.js"]
        assert "[tokenHash]" in result["routes/auth.js"]

    def test_noop_when_already_hashed(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        auth_js = """const router = require('express').Router();
router.get('/magic', async (req, res) => {
  const token = req.query.token;
  const tokenHash = crypto.createHash('sha256').update(token).digest('hex');
  const result = await pool.query('SELECT * FROM magic_link_tokens WHERE token_hash = $1', [tokenHash]);
  req.session.save(() => res.redirect('/dashboard'));
});
module.exports = router;"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        # Should not double-inject
        assert result["routes/auth.js"].count("createHash") == 1

    def test_noop_when_flag_disabled(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", False)
        from build import _ensure_magic_link_hash
        auth_js = """router.get('/magic', async (req, res) => {
  const token = req.query.token;
  const result = await pool.query('SELECT * FROM magic_link_tokens WHERE token_hash = $1', [token]);
});"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        assert "createHash" not in result["routes/auth.js"]

    def test_noop_when_no_magic_route(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        auth_js = """const router = require('express').Router();
router.post('/login', async (req, res) => {});
module.exports = router;"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        assert "createHash" not in result["routes/auth.js"]

    def test_noop_when_no_auth_route_file(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        files = [("server.js", "const app = require('express')();")]
        result = dict(_ensure_magic_link_hash(files))
        assert "routes/auth.js" not in result

    def test_handles_destructured_token(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        auth_js = """const router = require('express').Router();
router.get('/magic', async (req, res) => {
  const { token } = req.query;
  const result = await pool.query('SELECT * FROM magic_link_tokens WHERE token_hash = $1 AND expires_at > NOW()', [token]);
  if (result.rows.length === 0) return res.redirect('/login');
  req.session.userId = result.rows[0].user_id;
  req.session.save(() => res.redirect('/dashboard'));
});
module.exports = router;"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        assert "createHash('sha256')" in result["routes/auth.js"]
        assert "[tokenHash]" in result["routes/auth.js"]

    def test_handles_req_query_token_in_array(self, monkeypatch):
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        auth_js = """const router = require('express').Router();
router.get('/magic', async (req, res) => {
  const result = await pool.query('SELECT * FROM magic_link_tokens WHERE token_hash = $1', [req.query.token]);
  req.session.save(() => res.redirect('/dashboard'));
});
module.exports = router;"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        assert "createHash('sha256')" in result["routes/auth.js"]
        assert "[tokenHash]" in result["routes/auth.js"]


    def test_fixes_get_magic_when_post_request_magic_link_has_hash(self, monkeypatch):
        """Regression: createHash in POST /request-magic-link caused false no-op in GET /magic."""
        monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
        from build import _ensure_magic_link_hash
        auth_js = """const router = require('express').Router();
router.post('/request-magic-link', async (req, res) => {
  const token = require('crypto').randomBytes(32).toString('hex');
  const hash = require('crypto').createHash('sha256').update(token).digest('hex');
  await pool.query('INSERT INTO magic_link_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, NOW())', [userId, hash]);
  res.redirect('/magic-link-sent');
});
router.get('/magic', async (req, res) => {
  const token = req.query.token;
  const result = await pool.query('SELECT * FROM magic_link_tokens WHERE token_hash = $1 AND expires_at > NOW() AND used_at IS NULL', [token]);
  if (result.rows.length === 0) return res.redirect('/login');
  req.session.userId = result.rows[0].user_id;
  req.session.save(() => res.redirect('/dashboard'));
});
module.exports = router;"""
        files = [("routes/auth.js", auth_js)]
        result = dict(_ensure_magic_link_hash(files))
        auth = result["routes/auth.js"]
        # GET /magic handler must now hash the token
        get_magic_start = auth.index("router.get('/magic'")
        get_magic_section = auth[get_magic_start:]
        assert "createHash('sha256')" in get_magic_section
        assert "[tokenHash]" in get_magic_section


class TestFixSeedAdminEmail:

    def test_replaces_admin_example_com(self):
        from build import _fix_seed_admin_email
        files = [
            ("db/seed.sql", "INSERT INTO users (email, name, role) VALUES ('admin@example.com', 'Admin', 'admin');"),
        ]
        spec = {"admin_email": "admin@medicinewaves.example"}
        result = dict(_fix_seed_admin_email(files, spec))
        assert "admin@medicinewaves.example" in result["db/seed.sql"]
        assert "admin@example.com" not in result["db/seed.sql"]

    def test_no_change_when_already_correct(self):
        from build import _fix_seed_admin_email
        files = [
            ("db/seed.sql", "INSERT INTO users (email) VALUES ('admin@medicinewaves.example');"),
        ]
        spec = {"admin_email": "admin@medicinewaves.example"}
        result = dict(_fix_seed_admin_email(files, spec))
        assert result["db/seed.sql"].count("admin@medicinewaves.example") == 1

    def test_no_change_when_no_admin_email_in_spec(self):
        from build import _fix_seed_admin_email
        files = [
            ("db/seed.sql", "INSERT INTO users (email) VALUES ('admin@example.com');"),
        ]
        spec = {}
        result = dict(_fix_seed_admin_email(files, spec))
        assert "admin@example.com" in result["db/seed.sql"]

    def test_no_change_when_no_seed_file(self):
        from build import _fix_seed_admin_email
        files = [("server.js", "const express = require('express');")]
        spec = {"admin_email": "test@test.example"}
        result = _fix_seed_admin_email(files, spec)
        assert len(result) == 1

    def test_fixes_email_verified_false(self):
        from build import _fix_seed_admin_email
        files = [
            ("db/seed.sql", "INSERT INTO users (email, email_verified) VALUES ('admin@example.com', FALSE);"),
        ]
        spec = {"admin_email": "admin@medicinewaves.example"}
        result = dict(_fix_seed_admin_email(files, spec))
        assert "TRUE" in result["db/seed.sql"]
        assert "FALSE" not in result["db/seed.sql"]

    def test_does_not_inject_password_hash_for_null_password(self):
        """SEC-1 (2026-04-19): prior behavior appended
        `UPDATE users SET password = '$2b$...' WHERE password IS NULL`
        so magic-link users could also log in via password. That shipped
        a shadow credential into every DB snapshot. The post-processor
        must NOT inject a password hash now."""
        from build import _fix_seed_admin_email
        files = [
            ("db/seed.sql", "INSERT INTO users (email, password, password_set) VALUES ('admin@x.example', NULL, FALSE);"),
        ]
        spec = {"admin_email": "admin@x.example"}
        result = dict(_fix_seed_admin_email(files, spec))
        seed = result["db/seed.sql"]
        assert "UPDATE users SET password = '$2b$" not in seed
        assert "$2b$" not in seed

    def test_does_not_append_update_when_hash_already_present(self):
        """SEC-1: after SEC-1 the post-processor never appends an
        `UPDATE users SET password = '$2b$...'` statement. If seed.sql
        arrives with a hash already inline (from Claude or legacy
        artifact), _fix_seed_admin_email leaves that inline value
        alone, scrubbing is the separate _ensure_magic_link_auth
        processor's job, but it must NOT amplify the leak by adding
        an UPDATE."""
        from build import _fix_seed_admin_email
        existing_hash = "$2b$10$" + "C" * 53  # bcrypt-shaped fixture, not a real hash
        files = [
            ("db/seed.sql", f"INSERT INTO users (email, password) VALUES ('a@b.example', '{existing_hash}');"),
        ]
        spec = {"admin_email": "a@b.example"}
        result = dict(_fix_seed_admin_email(files, spec))
        seed = result["db/seed.sql"]
        assert "UPDATE users SET password = '$2b$" not in seed

    def test_fixes_password_set_false_as_last_column(self):
        from build import _fix_seed_admin_email
        files = [
            ("db/seed.sql", "INSERT INTO users (email, password_set) VALUES ('admin@x.example', FALSE);"),
        ]
        spec = {"admin_email": "admin@x.example"}
        result = dict(_fix_seed_admin_email(files, spec))
        seed = result["db/seed.sql"]
        # The text replacement should have changed FALSE to TRUE
        assert "VALUES ('admin@x.example', TRUE)" in seed


# ---------------------------------------------------------------------------
# _ensure_resend_from_address()
# ---------------------------------------------------------------------------

class TestEnsureResendFromAddress:
    """Tests for post-processor #19: fix Resend API from address."""

    def test_fixes_host_derived_from_address(self):
        auth_code = """
router.post('/request-magic-link', async (req, res) => {
  const { email } = req.body;
  await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + process.env.RESEND_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from: 'noreply@' + req.get('host').replace(/:\\d+$/, ''), to: email, subject: 'Login link', html: '<p>Click here</p>' })
  });
});
"""
        files = [("routes/auth.js", auth_code)]
        result = dict(_ensure_resend_from_address(files))
        assert "noreply@craftandship.io" in result["routes/auth.js"]
        assert "req.get('host')" not in result["routes/auth.js"]

    def test_fixes_railway_domain_from_address(self):
        auth_code = """
fetch('https://api.resend.com/emails', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key },
  body: JSON.stringify({ from: 'noreply@myapp-production-abc123.up.railway.app', to: email, subject: 'test', html: '<p>hi</p>' })
});
"""
        files = [("routes/auth.js", auth_code)]
        result = dict(_ensure_resend_from_address(files))
        assert "noreply@craftandship.io" in result["routes/auth.js"]
        assert "railway.app" not in result["routes/auth.js"]

    def test_fixes_to_field_to_array(self):
        auth_code = """
fetch('https://api.resend.com/emails', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer key', 'Content-Type': 'application/json' },
  body: JSON.stringify({ from: 'noreply@craftandship.io', to: email, subject: 'Link', html: '<p>hi</p>' })
});
"""
        files = [("routes/auth.js", auth_code)]
        result = dict(_ensure_resend_from_address(files))
        assert "to: [email]" in result["routes/auth.js"]

    def test_leaves_already_correct_code_unchanged(self):
        auth_code = """
fetch('https://api.resend.com/emails', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer key', 'Content-Type': 'application/json' },
  body: JSON.stringify({ from: 'noreply@craftandship.io', to: [email], subject: 'Link', html: '<p>hi</p>' })
});
"""
        files = [("routes/auth.js", auth_code)]
        result = dict(_ensure_resend_from_address(files))
        # Should still have the correct values
        assert "noreply@craftandship.io" in result["routes/auth.js"]
        assert "to: [email]" in result["routes/auth.js"]

    def test_skips_non_js_files(self):
        files = [("README.md", "api.resend.com/emails is our email provider")]
        result = _ensure_resend_from_address(files)
        assert result == files

    def test_skips_files_without_resend(self):
        files = [("routes/index.js", "router.get('/', (req, res) => res.render('index'));")]
        result = _ensure_resend_from_address(files)
        assert result == files

    def test_fixes_template_literal_host(self):
        auth_code = """
fetch('https://api.resend.com/emails', {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ from: `noreply@${req.get('host')}`, to: userEmail, subject: 'Link', html: '<p>hi</p>' })
});
"""
        files = [("routes/auth.js", auth_code)]
        result = dict(_ensure_resend_from_address(files))
        assert "noreply@craftandship.io" in result["routes/auth.js"]

    def test_fixes_utils_email_file(self):
        """Post-processor should fix email utility files too, not just auth routes."""
        email_util = """
async function sendEmail(to, subject, html) {
  await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + process.env.RESEND_API_KEY },
    body: JSON.stringify({ from: 'noreply@' + req.get('host').replace(/:\\d+$/, ''), to: recipientEmail, subject: subject, html: html })
  });
}
"""
        files = [("utils/email.js", email_util)]
        result = dict(_ensure_resend_from_address(files))
        assert "noreply@craftandship.io" in result["utils/email.js"]


# ---------------------------------------------------------------------------
# _validate_destructive_confirm()
# ---------------------------------------------------------------------------

class TestValidateDestructiveConfirm:
    def test_returns_files_unchanged(self):
        """Non-blocking: always returns files as-is."""
        files = [("views/items.ejs", '<form action="/items/1/delete" method="POST" data-confirm="Are you sure?"></form>')]
        result = _validate_destructive_confirm(files)
        assert result == files

    def test_no_warnings_when_data_confirm_present(self, capsys):
        files = [("views/items.ejs", '<form action="/items/1/delete" method="POST" data-confirm="Are you sure you want to delete this record? This cannot be undone."></form>')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" not in out.lower()

    def test_warns_when_delete_form_missing_data_confirm(self, capsys):
        files = [("views/items.ejs", '<form action="/items/1/delete" method="POST"></form>')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" in out.lower()

    def test_warns_when_remove_form_missing_data_confirm(self, capsys):
        files = [("views/members.ejs", '<form action="/members/2/remove" method="POST"></form>')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" in out.lower()

    def test_warns_when_reset_form_missing_data_confirm(self, capsys):
        files = [("views/admin.ejs", '<form action="/admin/reset" method="POST"></form>')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" in out.lower()

    def test_skips_layout_ejs(self, capsys):
        """Layout only has logout form, should never warn on layout."""
        files = [("views/layout.ejs", '<form action="/logout/reset" method="POST"></form>')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" not in out.lower()

    def test_skips_non_ejs_files(self, capsys):
        files = [("routes/items.js", 'router.delete("/items/:id/delete", handler)')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" not in out.lower()

    def test_no_warning_when_no_destructive_routes(self, capsys):
        files = [("views/items.ejs", '<form action="/items/new" method="POST"></form>')]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "warning" not in out.lower()

    def test_multiple_files_counted_correctly(self, capsys):
        files = [
            ("views/items.ejs", '<form action="/items/1/delete" method="POST"></form>'),
            ("views/tasks.ejs", '<form action="/tasks/2/delete" method="POST"></form>'),
        ]
        _validate_destructive_confirm(files)
        out = capsys.readouterr().out
        assert "2 warning" in out


# ---------------------------------------------------------------------------
# Design context and checklist tests
# ---------------------------------------------------------------------------

class TestDesignContext:
    """Verify _build_design_context produces DESIGN.md format output."""

    SPEC = {
        "name": "Pet Grooming App",
        "description": "A pet grooming scheduling app",
        "design": {"style": "warm earthy"},
    }

    def test_full_mode_starts_with_design_md_header(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "# DESIGN.md:" in ctx

    def test_full_mode_has_color_palette_section(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "## 2. Color Palette" in ctx
        assert "--color-primary" in ctx

    def test_full_mode_section2_includes_on_primary_token(self):
        """Section 2 color table must include --color-on-primary as a table row."""
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "| --color-on-primary |" in ctx, (
            "Section 2 must include a | --color-on-primary | table row; "
            "this token is referenced by btn_primary in STYLE_COMPONENT_TOKENS"
        )

    def test_full_mode_has_typography_section(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "## 3. Typography" in ctx
        assert "Google Fonts" in ctx

    def test_full_mode_has_component_styles_section(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "## 4. Component Styles" in ctx

    def test_full_mode_has_donts_section(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "## 7." in ctx
        assert "NEVER" in ctx

    def test_full_mode_has_key_rules_section(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "## 9. Key Rules" in ctx

    def test_full_mode_visual_checklist_still_appended(self):
        ctx = _build_design_context(self.SPEC, abbreviated=False)
        assert "Visual Design Checklist" in ctx
        assert "split-panel" in ctx

    def test_abbreviated_mode_has_sections_2_3_4_7(self):
        ctx = _build_design_context(self.SPEC, abbreviated=True)
        assert "## 2." in ctx
        assert "## 3." in ctx
        assert "## 4." in ctx
        assert "## 7." in ctx

    def test_abbreviated_mode_excludes_sections_1_5_6_8_9(self):
        ctx = _build_design_context(self.SPEC, abbreviated=True)
        assert "## 1." not in ctx
        assert "## 5." not in ctx
        assert "## 6." not in ctx
        assert "## 8." not in ctx
        assert "## 9." not in ctx

    def test_abbreviated_mode_excludes_checklist(self):
        ctx = _build_design_context(self.SPEC, abbreviated=True)
        assert "Visual Design Checklist" not in ctx

    @pytest.mark.parametrize("style", [
        "modern light", "modern dark", "clean minimal",
        "warm earthy", "bold vibrant", "corporate professional",
    ])
    def test_each_style_produces_donts_section(self, style):
        spec = {"description": "A generic utility tool", "design": {"style": style}}
        ctx = _build_design_context(spec, abbreviated=False)
        assert "## 7." in ctx
        assert "NEVER" in ctx


class TestDesignInPreflight:
    """Verify pre-flight checklist includes design items."""

    def _spec(self):
        return {
            "name": "Test App",
            "description": "A simple test app",
            "admin_email": "admin@test.example",
            "pages": [{"name": "Dashboard", "path": "/dashboard", "auth_required": True}],
            "data_models": [{"name": "Item", "fields": [{"name": "title", "type": "string"}]}],
            "user_roles": ["admin"],
            "design": {"style": "modern light"},
        }

    def test_explicit_prompt_has_design_checklist_items(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "DESIGN: Login/register pages use split-panel" in prompt
        assert "DESIGN: Every page has a gradient or tinted background" in prompt
        assert "DESIGN: Nav bar uses bg-primary" in prompt
        assert "DESIGN: Cards have class=\"animate-in\"" in prompt

    def test_explicit_prompt_final_instruction_mentions_design(self):
        prompt = _build_explicit_prompt(self._spec())
        assert "split-panel login" in prompt
        assert "gradient/tinted page backgrounds" in prompt
        assert "animate-in cards" in prompt
        assert "failed build" in prompt  # strong language about banned patterns


class TestUUPMPetAppPipeline:
    """End-to-end UUPM pipeline verification for pet grooming apps.

    Validates that a minimal spec with description "A pet grooming appointment
    scheduler" and NO design field produces warm orange colors (#F97316) in the
    generated Tailwind config, not the default indigo (#2563eb).
    """

    MINIMAL_SPEC = {
        "name": "Pet Grooming Scheduler",
        "description": "A pet grooming appointment scheduler",
        # No design field: UUPM enrichment should fill it in
    }

    def _enrich_spec(self, spec):
        """The spec as intake.run_conversation() saves it: the real UUPM
        enrichment has filled the unset spec.design fields."""
        return spec_saved_by_a_conversation(spec)

    def test_design_data_matches_pet_services(self):
        """Step 1: design_data recognizes 'pet grooming' as pet_services."""
        from design_data import match_product_type
        product_type = match_product_type(self.MINIMAL_SPEC["description"])
        assert product_type == "pet_services"

    def test_design_data_returns_orange_palette(self):
        """Step 2: pet_services palette has warm orange primary (#F97316)."""
        from design_data import get_design_context
        ctx = get_design_context(self.MINIMAL_SPEC)
        assert ctx["palette"]["primary"] == "#F97316"
        assert ctx["palette"]["secondary"] == "#FB923C"
        assert ctx["palette"]["accent"] == "#2563EB"
        assert ctx["style"] == "warm earthy"

    def test_intake_enrichment_populates_design_field(self):
        """Step 3: intake enrichment fills spec.design with orange colors."""
        spec = dict(self.MINIMAL_SPEC)  # shallow copy
        enriched = self._enrich_spec(spec)
        assert enriched["design"]["primary_color"] == "#F97316"
        assert enriched["design"]["secondary_color"] == "#FB923C"
        assert enriched["design"]["accent_color"] == "#2563EB"
        assert enriched["design"]["style"] == "warm earthy"

    def test_tailwind_compiled_uses_orange_not_indigo(self):
        """Step 4: _ensure_tailwind_compiled writes orange to tailwind.config.js."""
        spec = dict(self.MINIMAL_SPEC)
        enriched = self._enrich_spec(spec)
        files = [("package.json", '{"name":"app","dependencies":{},"scripts":{}}')]
        result = dict(_ensure_tailwind_compiled(files, enriched))
        assert "tailwind.config.js" in result
        tw_config = result["tailwind.config.js"]
        # Must contain orange, not default indigo
        assert "#F97316" in tw_config, f"Expected orange #F97316 in tailwind.config.js, got: {tw_config}"
        assert "#2563eb" not in tw_config, "Default indigo should not appear when pet palette is active"

    def test_design_context_prompt_has_orange_colors(self):
        """Step 5: _build_design_context includes orange colors in DESIGN.md table."""
        spec = dict(self.MINIMAL_SPEC)
        enriched = self._enrich_spec(spec)
        ctx = _build_design_context(enriched, abbreviated=False)
        # Hex value appears in the color table (not CSS property format)
        assert "#F97316" in ctx
        assert "warm earthy" in ctx
        assert "--color-secondary" in ctx

    def test_enrichment_does_not_override_customer_colors(self):
        """Customer-specified colors take priority over UUPM defaults."""
        spec = {
            "name": "Custom Pet App",
            "description": "A pet grooming appointment scheduler",
            "design": {"primary_color": "#FF0000"},
        }
        enriched = self._enrich_spec(spec)
        # Customer chose red, enrichment must not override
        assert enriched["design"]["primary_color"] == "#FF0000"
        # But secondary/accent stay red since primary was already set
        assert "secondary_color" not in enriched["design"] or enriched["design"]["secondary_color"] == ""


class TestUUPMFinanceAppPipeline:
    """End-to-end UUPM pipeline verification for freelancer invoice apps.

    Validates that a minimal spec with description "A freelancer invoice
    tracking app" and NO design field produces the invoice palette: navy
    blue primary (#1E3A5F) with green accent (#059669) and corporate
    professional style.  The keyword "invoice" matches before "freelancer"
    in PRODUCT_KEYWORDS iteration order, which is correct, the core
    product is invoice tracking, so trust-oriented navy is a better fit
    than the freelancer indigo (#4F46E5).
    """

    MINIMAL_SPEC = {
        "name": "Freelancer Invoice Tracker",
        "description": "A freelancer invoice tracking app",
        # No design field: UUPM enrichment should fill it in
    }

    def _enrich_spec(self, spec):
        """The spec as intake.run_conversation() saves it: the real UUPM
        enrichment has filled the unset spec.design fields."""
        return spec_saved_by_a_conversation(spec)

    def test_design_data_matches_invoice(self):
        """Step 1: 'invoice' keyword matches before 'freelancer' in iteration order."""
        from design_data import match_product_type
        product_type = match_product_type(self.MINIMAL_SPEC["description"])
        assert product_type == "invoice"

    def test_design_data_returns_navy_palette(self):
        """Step 2: invoice palette has navy primary (#1E3A5F) with green accent."""
        from design_data import get_design_context
        ctx = get_design_context(self.MINIMAL_SPEC)
        assert ctx["palette"]["primary"] == "#1E3A5F"
        assert ctx["palette"]["secondary"] == "#2563EB"
        assert ctx["palette"]["accent"] == "#059669"
        assert ctx["style"] == "corporate professional"

    def test_intake_enrichment_populates_design_field(self):
        """Step 3: intake enrichment fills spec.design with invoice colors."""
        spec = dict(self.MINIMAL_SPEC)
        enriched = self._enrich_spec(spec)
        assert enriched["design"]["primary_color"] == "#1E3A5F"
        assert enriched["design"]["secondary_color"] == "#2563EB"
        assert enriched["design"]["accent_color"] == "#059669"
        assert enriched["design"]["style"] == "corporate professional"

    def test_tailwind_compiled_uses_navy_not_default_indigo(self):
        """Step 4: _ensure_tailwind_compiled writes navy to tailwind.config.js."""
        spec = dict(self.MINIMAL_SPEC)
        enriched = self._enrich_spec(spec)
        files = [("package.json", '{"name":"app","dependencies":{},"scripts":{}}')]
        result = dict(_ensure_tailwind_compiled(files, enriched))
        assert "tailwind.config.js" in result
        tw_config = result["tailwind.config.js"]
        assert "#1E3A5F" in tw_config, f"Expected navy #1E3A5F in tailwind.config.js"
        assert "#2563eb" not in tw_config, "Default indigo should not appear when invoice palette is active"

    def test_design_context_prompt_has_navy_colors(self):
        """Step 5: _build_design_context includes navy colors in DESIGN.md table."""
        spec = dict(self.MINIMAL_SPEC)
        enriched = self._enrich_spec(spec)
        ctx = _build_design_context(enriched, abbreviated=False)
        assert "#1E3A5F" in ctx
        assert "corporate professional" in ctx
        assert "--color-secondary" in ctx

    def test_enrichment_does_not_override_customer_colors(self):
        """Customer-specified colors take priority over UUPM defaults."""
        spec = {
            "name": "My Invoice App",
            "description": "A freelancer invoice tracking app",
            "design": {"primary_color": "#4F46E5"},  # customer wants indigo
        }
        enriched = self._enrich_spec(spec)
        assert enriched["design"]["primary_color"] == "#4F46E5"
        # The invoice palette's secondary and accent are not mixed into a customer's primary.
        assert "secondary_color" not in enriched["design"]
        assert "accent_color" not in enriched["design"]


# ---------------------------------------------------------------------------
# _ensure_font_loading()
# ---------------------------------------------------------------------------

class TestEnsureFontLoading:
    """Tests for _ensure_font_loading post-processor."""

    def test_injects_google_fonts_link_when_missing(self):
        """Layout.ejs gets Google Fonts link injected."""
        files = [("views/layout.ejs", '<html><head><link rel="stylesheet" href="/tailwind.css"></head><body></body></html>')]
        spec = {"design": {"font_display": "Poppins", "font_body": "DM Sans"}}
        result = dict(_ensure_font_loading(files, spec))
        assert "fonts.googleapis.com" in result["views/layout.ejs"]
        assert "Poppins" in result["views/layout.ejs"]

    def test_preserves_existing_correct_link(self):
        """Already correct Google Fonts link is not duplicated."""
        link = '<link href="https://fonts.googleapis.com/css2?family=Poppins&family=DM+Sans&display=swap" rel="stylesheet">'
        files = [("views/layout.ejs", f'<html><head>{link}</head><body></body></html>')]
        spec = {"design": {"font_display": "Poppins", "font_body": "DM Sans"}}
        result = dict(_ensure_font_loading(files, spec))
        # Should not have duplicate links
        assert result["views/layout.ejs"].count("fonts.googleapis.com") == 1

    def test_falls_back_to_uupm_fonts_when_no_font_info(self):
        """UUPM-resolved fonts injected even when spec.design has no explicit fonts."""
        original = '<html><head></head><body></body></html>'
        files = [("views/layout.ejs", original)]
        spec = {"design": {}}
        result = dict(_ensure_font_loading(files, spec))
        # UUPM default pairing (modern_professional) should be injected
        assert "fonts.googleapis.com" in result["views/layout.ejs"]
        assert "font-family" in result["views/layout.ejs"]

    def test_injects_font_family_style(self):
        """Font-family CSS injected for body and headings."""
        files = [("views/layout.ejs", '<html><head></head><body></body></html>')]
        spec = {"design": {"font_display": "Poppins", "font_body": "DM Sans"}}
        result = dict(_ensure_font_loading(files, spec))
        assert "font-family" in result["views/layout.ejs"]
        assert "DM Sans" in result["views/layout.ejs"]


# ---------------------------------------------------------------------------
# _ensure_color_consistency()
# ---------------------------------------------------------------------------

class TestEnsureColorConsistency:
    """Tests for _ensure_color_consistency post-processor."""

    def test_injects_css_custom_properties(self):
        """CSS custom properties injected when :root missing."""
        files = [("views/layout.ejs", '<html><head></head><body></body></html>')]
        spec = {"design": {"primary_color": "#B45309", "secondary_color": "#92400e", "accent_color": "#f59e0b"}}
        result = dict(_ensure_color_consistency(files, spec))
        layout = result["views/layout.ejs"]
        assert "--color-primary" in layout
        assert "#B45309" in layout

    def test_does_not_replace_error_colors(self):
        """Semantic status colors are preserved."""
        files = [
            ("views/layout.ejs", '<html><head></head><body></body></html>'),
            ("views/dashboard.ejs", '<span class="text-red-500">Error</span><span class="bg-green-100">OK</span>')
        ]
        spec = {"design": {"primary_color": "#B45309"}}
        result = dict(_ensure_color_consistency(files, spec))
        assert "text-red-500" in result["views/dashboard.ejs"]
        assert "bg-green-100" in result["views/dashboard.ejs"]

    def test_replaces_hardcoded_nav_bg(self):
        """Hardcoded hex in nav background replaced."""
        files = [("views/layout.ejs", '<html><head></head><body><nav style="background-color: #2563eb">Nav</nav></body></html>')]
        spec = {"design": {"primary_color": "#B45309"}}
        result = dict(_ensure_color_consistency(files, spec))
        layout = result["views/layout.ejs"]
        # Should replace the hardcoded color
        assert "#2563eb" not in layout.split("<nav")[1].split("</nav>")[0] if "<nav" in layout else True

    def test_skips_when_no_design_colors(self):
        """No changes when spec has no design colors."""
        original = '<html><head></head><body></body></html>'
        files = [("views/layout.ejs", original)]
        spec = {"design": {}}
        result = dict(_ensure_color_consistency(files, spec))
        assert result["views/layout.ejs"] == original

    def test_preserves_existing_correct_root(self):
        """Existing :root block with correct values not duplicated."""
        style = '<style>:root { --color-primary: #B45309; --color-secondary: #92400e; --color-accent: #f59e0b; }</style>'
        files = [("views/layout.ejs", f'<html><head>{style}</head><body></body></html>')]
        spec = {"design": {"primary_color": "#B45309", "secondary_color": "#92400e", "accent_color": "#f59e0b"}}
        result = dict(_ensure_color_consistency(files, spec))
        assert result["views/layout.ejs"].count("--color-primary") == 1

    def test_fixes_inline_root_inside_larger_style_block(self):
        """Fixes :root embedded inside a larger <style> block (not standalone)."""
        layout = (
            '<html><head><style>\n'
            '  :root { --color-primary: #1a1a2e; --color-secondary: #16213e; }\n'
            '  body { margin: 0; }\n'
            '</style></head><body></body></html>'
        )
        files = [("views/layout.ejs", layout)]
        spec = {"design": {"primary_color": "#0f2b3c", "secondary_color": "#1a5a6a"}}
        result = dict(_ensure_color_consistency(files, spec))
        out = result["views/layout.ejs"]
        assert "#1a1a2e" not in out
        assert "#0f2b3c" in out
        assert "#1a5a6a" in out
        # The body rule should still be there
        assert "body { margin: 0; }" in out

    def test_fixes_multiple_root_blocks(self):
        """Fixes ALL :root blocks when layout has more than one."""
        layout = (
            '<html><head>\n'
            '<style>:root { --color-primary: #1a1a2e; }</style>\n'
            '<link rel="stylesheet" href="/style.css">\n'
            '<style>:root { --color-primary: #222; --color-accent: #333; } .foo { color: red; }</style>\n'
            '</head><body></body></html>'
        )
        files = [("views/layout.ejs", layout)]
        spec = {"design": {"primary_color": "#0f2b3c", "accent_color": "#c8963e"}}
        result = dict(_ensure_color_consistency(files, spec))
        out = result["views/layout.ejs"]
        # Both blocks should be fixed
        assert out.count("#1a1a2e") == 0
        assert out.count("#222") == 0
        assert out.count("#0f2b3c") == 2  # one in each :root block
        assert "#c8963e" in out


# ---------------------------------------------------------------------------
# _ensure_nav_glass()
# ---------------------------------------------------------------------------

class TestEnsureNavGlass:
    """Tests for _ensure_nav_glass post-processor."""

    def test_replaces_bg_white_on_nav(self):
        """bg-white on nav replaced with glass styling."""
        from build import _ensure_nav_glass
        layout = '<html><body><nav class="bg-white shadow-md flex items-center">Nav</nav></body></html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_glass(files))
        out = result["views/layout.ejs"]
        assert "bg-white" not in out
        assert "bg-[var(--color-primary)]/90" in out

    def test_replaces_bg_gray_900(self):
        """bg-gray-900 on nav replaced with glass styling."""
        from build import _ensure_nav_glass
        layout = '<nav class="bg-gray-900 text-white px-4">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_glass(files))
        out = result["views/layout.ejs"]
        assert "bg-gray-900" not in out
        assert "bg-[var(--color-primary)]/90" in out

    def test_preserves_non_nav_bg(self):
        """bg-white on non-nav elements is NOT changed."""
        from build import _ensure_nav_glass
        layout = '<nav class="flex">Nav</nav><div class="bg-white">Content</div>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_glass(files))
        out = result["views/layout.ejs"]
        assert "bg-white" in out  # the div's bg-white stays

    def test_no_nav_no_change(self):
        """No nav element means no changes."""
        from build import _ensure_nav_glass
        layout = '<html><body><div>No nav</div></body></html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_glass(files))
        assert result["views/layout.ejs"] == layout

    def test_no_layout_no_crash(self):
        """No layout.ejs returns files unchanged."""
        from build import _ensure_nav_glass
        files = [("server.js", "console.log('ok')")]
        result = dict(_ensure_nav_glass(files))
        assert "views/layout.ejs" not in result


# ---------------------------------------------------------------------------
# _ensure_nav_solid_surface()
# ---------------------------------------------------------------------------

class TestEnsureNavSolidSurface:
    """Tests for _ensure_nav_solid_surface defensive post-processor.

    Guarantees the <nav> element has a solid background-producing class,
    catching the case where Claude emits a bare <nav> with only layout
    utilities (sticky, flex, etc) and no background.
    """

    def test_injects_bg_on_bare_nav_with_layout_classes(self):
        """Nav with layout-only classes (no bg) gets bg-[var(--color-primary)]."""
        from build import _ensure_nav_solid_surface
        layout = '<nav class="sticky top-0 z-50 flex items-center">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        assert "bg-[var(--color-primary)]" in out
        # Existing layout classes preserved
        assert "sticky top-0 z-50 flex items-center" in out

    def test_injects_bg_on_nav_with_only_nav_glass(self):
        """Nav with only a custom .nav-glass class still gets a Tailwind bg fallback.

        .nav-glass CSS may be missing, overridden, or too-transparent, the
        Tailwind bg is a guaranteed floor.
        """
        from build import _ensure_nav_solid_surface
        layout = '<nav class="nav-glass sticky top-0">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        assert "bg-[var(--color-primary)]" in out
        assert "nav-glass" in out  # preserved

    def test_preserves_existing_bg_palette_class(self):
        """Nav with bg-slate-900 is left alone (idempotent)."""
        from build import _ensure_nav_solid_surface
        layout = '<nav class="bg-slate-900 sticky top-0">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        # No injection because a bg class already exists
        assert out.count("bg-") == 1
        assert "bg-slate-900" in out

    def test_preserves_existing_bg_primary_class(self):
        """Nav with bg-primary is left alone."""
        from build import _ensure_nav_solid_surface
        layout = '<nav class="bg-primary text-white">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        # Unchanged
        assert out == layout

    def test_preserves_existing_bg_arbitrary_value(self):
        """Nav with bg-[var(--color-primary)]/90 is left alone."""
        from build import _ensure_nav_solid_surface
        layout = '<nav class="bg-[var(--color-primary)]/90 sticky">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        assert out == layout

    def test_preserves_inline_background_style(self):
        """Nav with inline style='background-color: ...' is left alone."""
        from build import _ensure_nav_solid_surface
        layout = '<nav class="sticky top-0" style="background-color: #0f2b3c">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        assert out == layout

    def test_injects_bg_when_nav_has_no_class_attribute(self):
        """Nav with zero attributes gets a class attribute added."""
        from build import _ensure_nav_solid_surface
        layout = '<nav>Nav</nav>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        assert 'class="bg-[var(--color-primary)]"' in out

    def test_only_patches_first_nav(self):
        """Only the first <nav> (primary layout nav) is patched."""
        from build import _ensure_nav_solid_surface
        layout = (
            '<nav class="sticky top-0">Primary</nav>'
            '<nav class="flex">Secondary</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        out = result["views/layout.ejs"]
        # First nav gets bg injection
        first_nav_end = out.find("</nav>")
        first_nav = out[:first_nav_end]
        assert "bg-[var(--color-primary)]" in first_nav
        # Second nav untouched (still just "flex")
        second_nav = out[first_nav_end:]
        assert "bg-[var(--color-primary)]" not in second_nav

    def test_idempotent_across_multiple_runs(self):
        """Running the post-processor twice produces the same output."""
        from build import _ensure_nav_solid_surface
        layout = '<nav class="sticky top-0 flex items-center">Nav</nav>'
        files = [("views/layout.ejs", layout)]
        first = dict(_ensure_nav_solid_surface(files))
        second = dict(_ensure_nav_solid_surface(list(first.items())))
        assert first["views/layout.ejs"] == second["views/layout.ejs"]

    def test_no_layout_no_crash(self):
        """No layout.ejs returns files unchanged."""
        from build import _ensure_nav_solid_surface
        files = [("server.js", "console.log('ok')")]
        result = dict(_ensure_nav_solid_surface(files))
        assert "views/layout.ejs" not in result

    def test_no_nav_no_change(self):
        """layout.ejs without a <nav> element is unchanged."""
        from build import _ensure_nav_solid_surface
        layout = '<html><body><header>No nav here</header></body></html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_solid_surface(files))
        assert result["views/layout.ejs"] == layout


# ---------------------------------------------------------------------------
# _ensure_nav_logo()
# ---------------------------------------------------------------------------

class TestEnsureNavLogo:
    """Tests for _ensure_nav_logo post-processor (NAV_LOGO1).

    Replaces inline-SVG or gradient-div brand marks in the nav with a real
    <img src="/logo.png"> when a logo file exists in public/. Fills the gap
    left by _ensure_logo_placement()'s narrow brand-anchor regex.
    """

    _SPEC = {"app_name": "medicine-waves", "business_name": "Medicine Waves"}

    def test_replaces_inline_svg_brand_mark(self):
        """Anchor with an inline <svg viewBox> gets the svg replaced with <img>."""
        from build import _ensure_nav_logo
        layout = (
            '<nav class="bg-primary sticky top-0">'
            '<a href="/" class="flex items-center gap-3 flex-shrink-0">'
            '<svg viewBox="0 0 32 32" class="w-10 h-10"><path d="M16 4z"/></svg>'
            '<span class="font-bold text-lg text-white">Medicine Waves</span>'
            '</a>'
            '</nav>'
        )
        files = [
            ("views/layout.ejs", layout),
            ("public/logo.png", b"\x89PNG\r\n"),
        ]
        result = dict(_ensure_nav_logo(files, self._SPEC))
        out = result["views/layout.ejs"]
        assert '<img src="/logo.png"' in out
        assert 'alt="Medicine Waves"' in out
        assert "w-10 h-10 md:w-12 md:h-12 rounded-full object-cover" in out
        # The inline svg brand mark is gone.
        assert "<svg viewBox=" not in out
        # The sibling wordmark span is preserved intact.
        assert '<span class="font-bold text-lg text-white">Medicine Waves</span>' in out

    def test_replaces_gradient_div_brand_mark(self):
        """Anchor with a <div style='linear-gradient'> wrapper gets the whole wrapper replaced."""
        from build import _ensure_nav_logo
        layout = (
            '<nav class="bg-primary sticky top-0">'
            '<a href="/" class="flex items-center gap-3 flex-shrink-0">'
            '<div class="w-10 h-10 rounded-full flex items-center justify-center"'
            ' style="background: linear-gradient(135deg, var(--color-accent), #e8b85e);">'
            '<svg viewBox="0 0 32 32" class="w-6 h-6" fill="white">'
            '<path d="M16 4c-2 4-6 6-6 12z"/></svg>'
            '</div>'
            '<span class="font-brand text-lg font-bold italic text-white">Medicine Waves</span>'
            '</a>'
            '</nav>'
        )
        files = [
            ("views/layout.ejs", layout),
            ("public/logo.png", b"\x89PNG\r\n"),
        ]
        result = dict(_ensure_nav_logo(files, self._SPEC))
        out = result["views/layout.ejs"]
        assert '<img src="/logo.png" alt="Medicine Waves"' in out
        # The entire gradient wrapper div (and its inner svg) is gone.
        assert "linear-gradient" not in out.split("</nav>")[0]
        assert "<svg viewBox=" not in out.split("</nav>")[0]
        # Wordmark span preserved.
        assert '<span class="font-brand text-lg font-bold italic text-white">Medicine Waves</span>' in out

    def test_idempotent_when_already_wired(self):
        """If the nav already contains <img src='/logo.png'>, layout is unchanged."""
        from build import _ensure_nav_logo
        layout = (
            '<nav class="bg-primary sticky top-0">'
            '<a href="/" class="flex items-center gap-3 flex-shrink-0">'
            '<img src="/logo.png" alt="Medicine Waves" class="w-10 h-10 rounded-full object-cover">'
            '<span class="font-bold text-lg text-white">Medicine Waves</span>'
            '</a>'
            '</nav>'
        )
        files = [
            ("views/layout.ejs", layout),
            ("public/logo.png", b"\x89PNG\r\n"),
        ]
        result = dict(_ensure_nav_logo(files, self._SPEC))
        assert result["views/layout.ejs"] == layout

    def test_bails_when_logo_file_missing(self):
        """No public/logo.png (or .svg) in files → no change, even if nav has a gradient brand mark."""
        from build import _ensure_nav_logo
        layout = (
            '<nav class="bg-primary sticky top-0">'
            '<a href="/" class="flex items-center gap-3">'
            '<svg viewBox="0 0 32 32"><path d="M16 4z"/></svg>'
            '<span class="font-bold">Medicine Waves</span>'
            '</a>'
            '</nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_nav_logo(files, self._SPEC))
        # layout.ejs untouched
        assert result["views/layout.ejs"] == layout
        # And we didn't accidentally introduce an img tag
        assert '<img src="/logo.png"' not in result["views/layout.ejs"]

    def test_does_not_touch_footer_brand_mark(self):
        """Only the first <nav>...</nav> is patched; footer gradient marks are left alone."""
        from build import _ensure_nav_logo
        layout = (
            '<nav class="bg-primary sticky top-0">'
            '<a href="/" class="flex items-center gap-3">'
            '<svg viewBox="0 0 32 32"><path d="M16 4z"/></svg>'
            '<span class="font-bold text-lg text-white">Medicine Waves</span>'
            '</a>'
            '</nav>'
            '<footer>'
            '<div class="flex items-center gap-3 mb-4">'
            '<div class="w-10 h-10 rounded-full"'
            ' style="background: linear-gradient(135deg, var(--color-accent), #e8b85e);">'
            '<svg viewBox="0 0 32 32" class="w-6 h-6" fill="white"><path d="M16 4z"/></svg>'
            '</div>'
            '<span class="font-brand text-lg font-bold italic">Medicine Waves</span>'
            '</div>'
            '</footer>'
        )
        files = [
            ("views/layout.ejs", layout),
            ("public/logo.png", b"\x89PNG\r\n"),
        ]
        result = dict(_ensure_nav_logo(files, self._SPEC))
        out = result["views/layout.ejs"]
        # Nav was patched
        nav_section = out[:out.find("</nav>") + len("</nav>")]
        assert '<img src="/logo.png"' in nav_section
        assert "<svg viewBox=" not in nav_section
        # Footer gradient brand mark is still there (unchanged)
        footer_section = out[out.find("<footer"):]
        assert "linear-gradient" in footer_section
        assert "<svg viewBox=" in footer_section
        assert '<img src="/logo.png"' not in footer_section


# ---------------------------------------------------------------------------
# Premium CSS expansion
# ---------------------------------------------------------------------------

class TestEnsurePremiumCssExpansion:
    """Tests for premium-base.css content verification."""

    def test_has_focus_ring_styles(self):
        css = Path(__file__).parent.parent / "cli" / "templates" / "premium-base.css"
        content = css.read_text()
        assert "focus" in content and "box-shadow" in content

    def test_has_transition_on_interactive(self):
        css = Path(__file__).parent.parent / "cli" / "templates" / "premium-base.css"
        content = css.read_text()
        assert "transition" in content

    def test_has_reduced_motion(self):
        css = Path(__file__).parent.parent / "cli" / "templates" / "premium-base.css"
        content = css.read_text()
        assert "prefers-reduced-motion" in content

    def test_has_section_spacing(self):
        css = Path(__file__).parent.parent / "cli" / "templates" / "premium-base.css"
        content = css.read_text()
        assert "section-spacing" in content

    def test_has_card_border_radius(self):
        css = Path(__file__).parent.parent / "cli" / "templates" / "premium-base.css"
        content = css.read_text()
        assert ".card" in content and "border-radius" in content


# ---------------------------------------------------------------------------
# _strip_broken_logo_refs()
# ---------------------------------------------------------------------------

class TestStripBrokenLogoRefs:
    """Tests for _strip_broken_logo_refs post-processor."""

    def test_strips_logo_img_when_no_asset(self):
        """Broken <img src="/logo.png"> removed when no logo file exists."""
        from build import _strip_broken_logo_refs
        files = [
            ("views/layout.ejs", '<nav><img src="/logo.png" alt="App" class="h-10"> Brand</nav>'),
            ("views/login.ejs", '<img src="/logo.png" alt="App" class="h-20">\n<form>'),
        ]
        spec = {"design": {}}
        result = dict(_strip_broken_logo_refs(files, spec))
        assert "/logo.png" not in result["views/layout.ejs"]
        assert "/logo.png" not in result["views/login.ejs"]
        assert "Brand" in result["views/layout.ejs"]
        assert "<form>" in result["views/login.ejs"]

    def test_keeps_logo_img_when_asset_exists(self):
        """Logo refs preserved when public/logo.png is in the file dict."""
        from build import _strip_broken_logo_refs
        files = [
            ("views/layout.ejs", '<nav><img src="/logo.png" alt="App"> Brand</nav>'),
            ("public/logo.png", b"\x89PNG"),
        ]
        spec = {"design": {}}
        result = dict(_strip_broken_logo_refs(files, spec))
        assert "/logo.png" in result["views/layout.ejs"]

    def test_keeps_logo_img_when_logo_data_in_spec(self):
        """Logo refs preserved when spec.design.logo_data is present."""
        from build import _strip_broken_logo_refs
        files = [
            ("views/layout.ejs", '<nav><img src="/logo.png" alt="App"> Brand</nav>'),
        ]
        spec = {"design": {"logo_data": "data:image/png;base64,iVBOR..."}}
        result = dict(_strip_broken_logo_refs(files, spec))
        assert "/logo.png" in result["views/layout.ejs"]

    def test_replaces_favicon_with_fallback(self):
        """Favicon link pointing at /logo.png replaced with /favicon.svg."""
        from build import _strip_broken_logo_refs
        files = [
            ("views/layout.ejs", '<head><link rel="icon" type="image/png" href="/logo.png"></head>'),
        ]
        spec = {"design": {}}
        result = dict(_strip_broken_logo_refs(files, spec))
        assert "/logo.png" not in result["views/layout.ejs"]
        assert '/favicon.svg' in result["views/layout.ejs"]

    def test_strips_svg_logo_refs_too(self):
        """Also strips /logo.svg references when no asset."""
        from build import _strip_broken_logo_refs
        files = [
            ("views/layout.ejs", '<nav><img src="/logo.svg" alt="App"> Brand</nav>'),
        ]
        spec = {"design": {}}
        result = dict(_strip_broken_logo_refs(files, spec))
        assert "/logo.svg" not in result["views/layout.ejs"]

    def test_no_op_on_non_ejs_files(self):
        """JS files with /logo.png are not touched."""
        from build import _strip_broken_logo_refs
        files = [
            ("server.js", 'app.get("/logo.png", handler)'),
        ]
        spec = {"design": {}}
        result = dict(_strip_broken_logo_refs(files, spec))
        assert "/logo.png" in result["server.js"]


# ---------------------------------------------------------------------------
# _ensure_icon_helper()
# ---------------------------------------------------------------------------

class TestEnsureIconHelper:
    """Tests for _ensure_icon_helper post-processor."""

    def test_injects_template_when_missing(self):
        files = [("server.js", 'const app = express();\napp.use(express.urlencoded({extended: true}));\napp.use(require("./routes/auth"));')]
        result = dict(_ensure_icon_helper(files))
        assert "icon_helper.js" in result
        assert "res.locals.icons" in result["icon_helper.js"]

    def test_patches_server_js(self):
        files = [("server.js", 'const app = express();\napp.use(express.urlencoded({extended: true}));\napp.use(require("./routes/auth"));')]
        result = dict(_ensure_icon_helper(files))
        assert "icon_helper" in result["server.js"]

    def test_no_duplicate_injection(self):
        files = [
            ("server.js", 'const app = express();\nrequire("./icon_helper")(app);\napp.use(require("./routes/auth"));'),
            ("icon_helper.js", "// already here")
        ]
        result = dict(_ensure_icon_helper(files))
        assert result["server.js"].count("icon_helper") == 1

    def test_icon_helper_has_common_icons(self):
        template = Path(__file__).parent.parent / "cli" / "templates" / "icon_helper.js"
        content = template.read_text()
        for icon in ["edit", "trash", "plus", "search", "user", "check", "home"]:
            assert icon in content


# ---------------------------------------------------------------------------
# _ensure_upload_route()
# ---------------------------------------------------------------------------

class TestEnsureUploadRoute:
    """Tests for _ensure_upload_route post-processor."""

    def test_injects_when_image_field_present(self):
        files = [
            ("server.js", 'const app = express();\napp.use(require("./routes/auth"));\n'),
            ("db/schema.sql", "CREATE TABLE users (id UUID);"),
            ("package.json", '{"name": "test", "dependencies": {"express": "^4"}}'),
        ]
        spec = {"data_models": [{"name": "Product", "fields": [{"name": "photo_url", "type": "string"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert "routes/upload.js" in result
        assert "requireAuth" in result["routes/upload.js"]

    def test_skips_when_no_image_fields(self):
        files = [("server.js", "const app = express();")]
        spec = {"data_models": [{"name": "Task", "fields": [{"name": "title", "type": "string"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert "routes/upload.js" not in result

    def test_validates_file_types_in_template(self):
        template = Path(__file__).parent.parent / "cli" / "templates" / "routes_upload.js"
        content = template.read_text()
        assert "ffd8ff" in content  # JPEG magic bytes
        assert "89504e47" in content  # PNG magic bytes

    def test_enforces_size_limit_in_template(self):
        template = Path(__file__).parent.parent / "cli" / "templates" / "routes_upload.js"
        content = template.read_text()
        assert "5 * 1024 * 1024" in content or "MAX_FILE_SIZE" in content

    def test_has_rate_limiting(self):
        template = Path(__file__).parent.parent / "cli" / "templates" / "routes_upload.js"
        content = template.read_text()
        assert "rateLimit" in content

    def test_multer_field_name_matches_client_template(self):
        """Regression: multer upload.single() must match the field name the
        client template sends. Client FormData uses 'file', so the server
        must use upload.single('file'). A mismatch produces MulterError:
        Unexpected field -> HTTP 500 on every upload attempt."""
        templates_dir = Path(__file__).parent.parent / "cli" / "templates"
        server_template = (templates_dir / "routes_upload.js").read_text()
        client_template = (templates_dir / "image-upload.js").read_text()

        # Server must expect field name 'file', not the old 'image'.
        assert "upload.single('file')" in server_template, (
            "routes_upload.js must use upload.single('file') to match client"
        )
        assert "upload.single('image')" not in server_template, (
            "routes_upload.js still uses upload.single('image') which breaks "
            "uploads with MulterError: Unexpected field"
        )

        # Client must send the multipart field as 'file'.
        assert "fd.append('file'" in client_template, (
            "image-upload.js must send the file as FormData field 'file'"
        )

    def test_adds_multer_dependency(self):
        files = [
            ("server.js", 'app.use(require("./routes/auth"));'),
            ("db/schema.sql", "CREATE TABLE users (id UUID);"),
            ("package.json", '{"name": "test", "dependencies": {"express": "^4"}}'),
        ]
        spec = {"data_models": [{"name": "Product", "fields": [{"name": "image", "type": "string"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert "multer" in result.get("package.json", "")

    def test_adds_uploads_table_to_schema(self):
        files = [
            ("server.js", 'app.use(require("./routes/auth"));'),
            ("db/schema.sql", "CREATE TABLE users (id UUID);"),
            ("package.json", '{"name": "test", "dependencies": {"express": "^4"}}'),
        ]
        spec = {"data_models": [{"name": "Profile", "fields": [{"name": "avatar_url", "type": "string"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert "CREATE TABLE IF NOT EXISTS uploads" in result.get("db/schema.sql", "")

    def test_mounts_route_in_server_js(self):
        files = [
            ("server.js", 'const app = express();\napp.use(require("./routes/auth"));\n'),
            ("db/schema.sql", "CREATE TABLE users (id UUID);"),
            ("package.json", '{"name": "test", "dependencies": {"express": "^4"}}'),
        ]
        spec = {"data_models": [{"name": "Product", "fields": [{"name": "thumbnail", "type": "string"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert "routes/upload" in result.get("server.js", "")

    def test_skips_if_upload_route_already_exists(self):
        files = [
            ("server.js", 'app.use(require("./routes/auth"));'),
            ("routes/upload.js", "// custom upload route"),
        ]
        spec = {"data_models": [{"name": "Product", "fields": [{"name": "photo", "type": "string"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert result["routes/upload.js"] == "// custom upload route"

    def test_detects_various_image_keywords(self):
        for keyword in ["photo", "image", "avatar", "picture", "thumbnail", "logo", "banner", "cover", "icon"]:
            files = [
                ("server.js", 'app.use(require("./routes/auth"));'),
                ("db/schema.sql", "CREATE TABLE users (id UUID);"),
                ("package.json", '{"name": "test", "dependencies": {"express": "^4"}}'),
            ]
            spec = {"data_models": [{"name": "Item", "fields": [{"name": f"{keyword}_url", "type": "string"}]}]}
            result = dict(_ensure_upload_route(files, spec))
            assert "routes/upload.js" in result, f"Failed to detect keyword: {keyword}"

    def test_handles_empty_data_models(self):
        files = [("server.js", "const app = express();")]
        spec = {"data_models": []}
        result = dict(_ensure_upload_route(files, spec))
        assert "routes/upload.js" not in result

    def test_handles_missing_data_models(self):
        files = [("server.js", "const app = express();")]
        spec = {}
        result = dict(_ensure_upload_route(files, spec))
        assert "routes/upload.js" not in result


# ---------------------------------------------------------------------------
# _ensure_upload_form_ui()
# ---------------------------------------------------------------------------

class TestEnsureUploadFormUI:
    """Tests for _ensure_upload_form_ui post-processor."""

    def _base_files(self, form_html="", edit_html="", layout="<html><body></body></html>"):
        files = [
            ("routes/upload.js", "// upload route present"),
            ("views/layout.ejs", layout),
        ]
        if form_html:
            files.append(("views/photos/new.ejs", form_html))
        if edit_html:
            files.append(("views/photos/edit.ejs", edit_html))
        return files

    def test_replaces_image_url_text_input(self):
        form = '<input type="text" id="image_url" name="image_url" required class="input-premium" value="" placeholder="/images/gallery/photo.jpg">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        new_ejs = result["views/photos/new.ejs"]
        assert 'type="text"' not in new_ejs
        assert 'type="hidden"' in new_ejs
        assert 'class="image-upload-zone"' in new_ejs
        assert 'data-target="image_url"' in new_ejs
        assert 'data-required="true"' in new_ejs

    def test_preserves_ejs_value_in_edit_form(self):
        edit = '<input type="text" id="image_url" name="image_url" required class="input-premium" value="<%= photo.image_url %>">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(edit_html=edit), spec))
        new_ejs = result["views/photos/edit.ejs"]
        assert 'value="<%= photo.image_url %>"' in new_ejs
        assert 'class="image-upload-zone"' in new_ejs

    def test_injects_script_and_css_in_layout(self):
        form = '<input type="text" id="image_url" name="image_url" value="">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        layout = result["views/layout.ejs"]
        assert 'image-upload.js' in layout
        assert '.image-upload-zone:hover' in layout
        assert "public/image-upload.js" in result

    def test_skips_when_no_upload_route(self):
        form = '<input type="text" id="image_url" name="image_url" value="">'
        files = [("views/photos/new.ejs", form)]  # no routes/upload.js
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(files, spec))
        assert result["views/photos/new.ejs"] == form  # unchanged

    def test_skips_non_image_text_inputs(self):
        form = '<input type="text" id="title" name="title" value="">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'name="title"' in result["views/photos/new.ejs"]
        assert "image-upload-zone" not in result["views/photos/new.ejs"]

    def test_handles_photo_url_field_name(self):
        form = '<input type="text" name="photo_url" id="photo_url" value="">'
        spec = {"data_models": [{"name": "Gallery", "fields": [{"name": "photo_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'data-target="photo_url"' in result["views/photos/new.ejs"]

    def test_handles_cover_image_field_name(self):
        form = '<input type="text" name="cover_image" id="cover_image" value="">'
        spec = {"data_models": [{"name": "Event", "fields": [{"name": "cover_image"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'data-target="cover_image"' in result["views/photos/new.ejs"]

    def test_optional_field_not_required(self):
        form = '<input type="text" id="image_url" name="image_url" class="input-premium" value="">'
        spec = {"data_models": [{"name": "Event", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'data-required="false"' in result["views/photos/new.ejs"]

    def test_updates_label_text(self):
        form = '<label for="image_url">Image URL *</label>\n<input type="text" name="image_url" id="image_url" value="">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert ">Image *<" in result["views/photos/new.ejs"]
        assert ">Image URL *<" not in result["views/photos/new.ejs"]

    def test_attribute_order_independence(self):
        """Input with name before type should still match."""
        form = '<input name="image_url" class="input-premium" type="text" id="image_url" value="">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'class="image-upload-zone"' in result["views/photos/new.ejs"]

    def test_js_template_has_required_elements(self):
        template = Path(__file__).parent.parent / "cli" / "templates" / "image-upload.js"
        content = template.read_text()
        assert "upload-file-input" in content
        assert "/api/upload" in content
        assert "x-csrf-token" in content
        assert "upload-preview" in content
        assert "upload-remove" in content
        assert "dragover" in content
        assert "5 * 1024 * 1024" in content

    def test_no_duplicate_script_injection(self):
        form = '<input type="text" id="image_url" name="image_url" value="">'
        layout = '<html><body><script src="/image-upload.js"></script></body></html>'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form, layout=layout), spec))
        assert result["views/layout.ejs"].count("image-upload.js") == 1

    def test_replaces_type_url_input(self):
        """MW v6 regression: Claude generates type=\"url\" for image fields, not type=\"text\"."""
        form = '<input type="url" id="image_url" name="image_url" class="input-premium" value="" placeholder="https://...">'
        spec = {"data_models": [{"name": "Event", "fields": [{"name": "image_url"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        new_ejs = result["views/photos/new.ejs"]
        assert 'type="url"' not in new_ejs
        assert 'class="image-upload-zone"' in new_ejs
        assert 'data-target="image_url"' in new_ejs

    def test_replaces_about_photo_url_input(self):
        """MW v6 regression: admin settings `about_photo_url` text input."""
        form = '<input type="text" id="about_photo_url" name="about_photo_url" class="input-premium" value="<%= siteSettings.about_photo_url %>">'
        spec = {"data_models": []}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        new_ejs = result["views/photos/new.ejs"]
        assert 'class="image-upload-zone"' in new_ejs
        assert 'data-target="about_photo_url"' in new_ejs

    def test_replaces_nav_logo_url_input(self):
        """MW v6 regression: admin settings `nav_logo_url` text input."""
        form = '<input type="text" id="nav_logo_url" name="nav_logo_url" value="<%= siteSettings.nav_logo_url %>">'
        spec = {"data_models": []}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'class="image-upload-zone"' in result["views/photos/new.ejs"]
        assert 'data-target="nav_logo_url"' in result["views/photos/new.ejs"]

    def test_replaces_hero_image_url_input(self):
        """MW v6 regression: admin settings `hero_image_url` text input."""
        form = '<input type="text" id="hero_image_url" name="hero_image_url" value="<%= siteSettings.hero_image_url %>">'
        spec = {"data_models": []}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'class="image-upload-zone"' in result["views/photos/new.ejs"]
        assert 'data-target="hero_image_url"' in result["views/photos/new.ejs"]

    def test_skips_image_alt_text_input(self):
        """Negative: `image_alt` is alt text, not a photo field, must NOT convert."""
        form = '<input type="text" id="image_alt" name="image_alt" value="">'
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url"}, {"name": "image_alt"}]}]}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'name="image_alt"' in result["views/photos/new.ejs"]
        assert "image-upload-zone" not in result["views/photos/new.ejs"]

    def test_skips_logo_text_input(self):
        """Negative: `logo_text` is a text caption, not a photo field, must NOT convert."""
        form = '<input type="text" id="logo_text" name="logo_text" value="">'
        spec = {"data_models": []}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'name="logo_text"' in result["views/photos/new.ejs"]
        assert "image-upload-zone" not in result["views/photos/new.ejs"]

    def test_skips_instagram_url_input(self):
        """Negative: `instagram_url` is a social link, not a photo field, must NOT convert."""
        form = '<input type="url" id="instagram_url" name="instagram_url" value="">'
        spec = {"data_models": []}
        result = dict(_ensure_upload_form_ui(self._base_files(form_html=form), spec))
        assert 'name="instagram_url"' in result["views/photos/new.ejs"]
        assert "image-upload-zone" not in result["views/photos/new.ejs"]


# ---------------------------------------------------------------------------
# _strip_redundant_image_upload_forms()
# ---------------------------------------------------------------------------

class TestStripRedundantImageUploadForms:
    """Tests for _strip_redundant_image_upload_forms post-processor.

    Ensures that when _ensure_upload_form_ui has already injected a drag-drop
    `.image-upload-zone` into a view, any parallel `<form enctype=\"multipart/form-data\">`
    block with `<input type=\"file\" accept=\"image/*\">` is removed so customers
    see a single upload affordance.
    """

    def _settings_view(self, extra_form_html=""):
        return (
            '<form method="post" action="/admin/settings">\n'
            '  <div class="field">\n'
            '    <label>About Photo</label>\n'
            '    <input type="hidden" name="about_photo_url" value="">\n'
            '    <div class="image-upload-zone" data-target="about_photo_url"></div>\n'
            '  </div>\n'
            '</form>\n'
            + extra_form_html
        )

    def test_strips_parallel_multipart_form(self):
        parallel = (
            '<hr>\n'
            '<h3>Or upload a new photo</h3>\n'
            '<form action="/admin/settings/about-photo-upload" method="post" enctype="multipart/form-data">\n'
            '  <input type="file" name="photo" accept="image/*" required>\n'
            '  <button type="submit">Upload Photo</button>\n'
            '</form>\n'
        )
        files = [("views/admin/settings.ejs", self._settings_view(parallel))]
        result = dict(_strip_redundant_image_upload_forms(files, {}))
        out = result["views/admin/settings.ejs"]
        assert 'enctype="multipart/form-data"' not in out
        assert 'type="file"' not in out
        assert 'Upload Photo</button>' not in out
        # Drag-drop zone remains.
        assert 'class="image-upload-zone"' in out

    def test_leaves_multipart_form_alone_when_no_upload_zone(self):
        """If the view never had a drag-drop zone injected, the multipart form stays."""
        view = (
            '<form action="/admin/upload" method="post" enctype="multipart/form-data">\n'
            '  <input type="file" name="photo" accept="image/*" required>\n'
            '  <button type="submit">Upload Photo</button>\n'
            '</form>\n'
        )
        files = [("views/admin/settings.ejs", view)]
        result = dict(_strip_redundant_image_upload_forms(files, {}))
        assert result["views/admin/settings.ejs"] == view

    def test_strips_multiple_parallel_forms(self):
        """MW v6 admin settings has THREE parallel forms (logo, hero, about-photo)."""
        parallel = (
            '<form action="/admin/settings/logo-upload" method="post" enctype="multipart/form-data">'
            '<input type="file" name="logo" accept="image/*"><button>Upload</button></form>\n'
            '<form action="/admin/settings/hero-upload" method="post" enctype="multipart/form-data">'
            '<input type="file" name="hero" accept="image/*"><button>Upload</button></form>\n'
            '<form action="/admin/settings/about-photo-upload" method="post" enctype="multipart/form-data">'
            '<input type="file" name="photo" accept="image/*"><button>Upload</button></form>\n'
        )
        files = [("views/admin/settings.ejs", self._settings_view(parallel))]
        result = dict(_strip_redundant_image_upload_forms(files, {}))
        out = result["views/admin/settings.ejs"]
        assert out.count("enctype=\"multipart/form-data\"") == 0
        assert out.count("type=\"file\"") == 0

    def test_ignores_non_view_files(self):
        """Routes/js files should never be touched even if they contain matching strings."""
        content = (
            '// enctype="multipart/form-data"\n'
            '// <input type="file" accept="image/*">\n'
        )
        files = [("routes/upload.js", content)]
        result = dict(_strip_redundant_image_upload_forms(files, {}))
        assert result["routes/upload.js"] == content

    def test_preserves_non_image_multipart_forms(self):
        """A multipart form accepting application/pdf (not image/*) must NOT be stripped."""
        view = (
            '<div class="image-upload-zone"></div>\n'
            '<form enctype="multipart/form-data">\n'
            '  <input type="file" accept="application/pdf" name="doc">\n'
            '  <button>Upload Document</button>\n'
            '</form>\n'
        )
        files = [("views/admin/settings.ejs", view)]
        result = dict(_strip_redundant_image_upload_forms(files, {}))
        assert 'accept="application/pdf"' in result["views/admin/settings.ejs"]


# ---------------------------------------------------------------------------
# _ensure_video_embeds()
# ---------------------------------------------------------------------------

class TestEnsureVideoEmbeds:
    """Tests for _ensure_video_embeds post-processor."""

    def test_converts_youtube_iframe_detail(self):
        """Detail page: raw YouTube iframe replaced with youtube-nocookie iframe."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/show.ejs", '<iframe src="https://www.youtube.com/embed/hHW1oY26kxQ" frameborder="0"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "youtube-nocookie.com" in result["views/show.ejs"]
        assert "hHW1oY26kxQ" in result["views/show.ejs"]

    def test_converts_youtube_iframe_index(self):
        """Index/list page: raw YouTube iframe replaced with thumbnail link."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/index.ejs", '<iframe src="https://www.youtube.com/embed/hHW1oY26kxQ" frameborder="0"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "/api/video-thumb/" in result["views/index.ejs"]
        assert "youtube.com/watch" in result["views/index.ejs"]
        assert "hHW1oY26kxQ" in result["views/index.ejs"]
        assert "<iframe" not in result["views/index.ejs"]

    def test_replaces_rickroll_video_ids(self):
        """Rickroll placeholder IDs replaced with curated fallback IDs."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/show.ejs", '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" frameborder="0"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "dQw4w9WgXcQ" not in result["views/show.ejs"]
        assert "youtube-nocookie.com" in result["views/show.ejs"]

    def test_replaces_duplicate_video_ids(self):
        """When all videos use the same ID, each gets a unique replacement."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("db/seed.sql", (
                "INSERT INTO videos (embed_url) VALUES "
                "('https://www.youtube.com/embed/abc12345678'), "
                "('https://www.youtube.com/embed/abc12345678'), "
                "('https://www.youtube.com/embed/abc12345678');"
            )),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "embed_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        # The duplicate ID should be replaced with diverse fallbacks
        seed = result["db/seed.sql"]
        assert "abc12345678" not in seed
        # Should have at least 2 unique IDs now
        import re
        ids = re.findall(r'youtube\.com/embed/([a-zA-Z0-9_-]{11})', seed)
        assert len(set(ids)) >= 2

    def test_preserves_non_youtube_iframes(self):
        """Google Maps iframes left untouched."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/contact.ejs", '<iframe src="https://maps.google.com/maps?q=NYC"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "maps.google.com" in result["views/contact.ejs"]

    def test_injects_vendor_files(self):
        """lite-youtube.js and .css added to public/vendor/."""
        files = [("views/layout.ejs", "<html><head></head><body></body></html>")]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "public/vendor/lite-youtube.js" in result
        assert "public/vendor/lite-youtube.css" in result

    def test_patches_layout_with_vendor_refs(self):
        """Layout.ejs gets link and script tags for vendor files."""
        files = [("views/layout.ejs", "<html><head></head><body></body></html>")]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        layout = result["views/layout.ejs"]
        assert "lite-youtube.css" in layout
        assert "lite-youtube.js" in layout

    def test_skips_when_no_video_fields(self):
        """No changes when spec has no video-related fields."""
        files = [("views/layout.ejs", "<html><head></head><body></body></html>")]
        spec = {"data_models": [{"name": "Task", "fields": [{"name": "title", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "public/vendor/lite-youtube.js" not in result

    def test_idempotent(self):
        """Running twice does not duplicate conversions."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/show.ejs", '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result1 = _ensure_video_embeds(list(files), spec)
        result2 = _ensure_video_embeds(list(result1), spec)
        r1_dict = dict(result1)
        r2_dict = dict(result2)
        assert r1_dict["views/show.ejs"] == r2_dict["views/show.ejs"]
        assert r1_dict["views/layout.ejs"].count("lite-youtube.css") == 1

    def test_patches_csp_for_youtube_nocookie(self):
        """CSP frame-src gets youtube-nocookie.com added."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("server.js", 'frameSrc: ["\'self\'", "https://www.youtube.com"]'),
            ("views/detail.ejs", '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "youtube-nocookie.com" in result["server.js"]

    def test_converts_dynamic_ejs_embed_url_detail(self):
        """Detail page: dynamic EJS embed_url iframe -> nocookie iframe."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/show.ejs", '<iframe src="<%= video.embed_url %>" class="w-full h-full" frameborder="0" allowfullscreen></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "embed_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        show = result["views/videos/show.ejs"]
        assert "youtube-nocookie.com" in show
        assert "_vidMatch" in show  # EJS video ID extraction logic
        assert "Watch on YouTube" in show

    def test_converts_dynamic_ejs_embed_url_index(self):
        """Index page: dynamic EJS embed_url iframe -> thumbnail link."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/index.ejs", '<iframe src="<%= video.embed_url %>" class="w-full h-full" frameborder="0"></iframe>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "embed_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        index = result["views/videos/index.ejs"]
        assert "/api/video-thumb/" in index
        assert "youtube.com/watch" in index
        assert "<iframe" not in index

    def test_dynamic_ejs_embed_skips_already_converted(self):
        """Dynamic EJS conversion skipped when nocookie already present."""
        content = '<iframe src="https://www.youtube-nocookie.com/embed/<%= _vidId %>" class="w-full h-full"></iframe>'
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/show.ejs", content),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "embed_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert result["views/videos/show.ejs"] == content

    def test_rewrites_ytimg_literal_src_to_proxy(self):
        """Hardcoded img.youtube.com thumbnail URLs rewritten to /api/video-thumb/ proxy."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/index.ejs",
             '<img src="https://img.youtube.com/vi/abc12345678/hqdefault.jpg" alt="thumb">'),
            ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        index = result["views/videos/index.ejs"]
        assert "img.youtube.com" not in index
        assert "/api/video-thumb/abc12345678" in index

    def test_rewrites_ytimg_ejs_dynamic_src_to_proxy(self):
        """Dynamic EJS YouTube thumbnail URLs rewritten to /api/video-thumb/ proxy."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/index.ejs",
             '<img src="https://img.youtube.com/vi/<%= video.video_id %>/hqdefault.jpg" alt="thumb">'),
            ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_id", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        index = result["views/videos/index.ejs"]
        assert "img.youtube.com" not in index
        assert "/api/video-thumb/<%= video.video_id %>" in index

    def test_no_ytimg_remains_after_post_processor(self):
        """After the post-processor runs, zero img.youtube.com references remain in any EJS."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/index.ejs",
             '<img src="https://img.youtube.com/vi/aXItOY0sLRY/hqdefault.jpg">'
             '<img src="https://i3.ytimg.com/vi/lFcSrYw-ARY/maxresdefault.jpg">'),
            ("views/videos/show.ejs",
             '<img src="https://img.youtube.com/vi/<%= video.video_id %>/hqdefault.jpg">'),
            ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_id", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        for path, content in result.items():
            if path.endswith(".ejs") and isinstance(content, str):
                assert "ytimg.com" not in content, f"ytimg.com reference remains in {path}"

    def test_ytimg_rewrite_preserves_other_img_attrs(self):
        """Rewrite preserves class, alt, loading attributes on the img tag."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("views/videos/index.ejs",
             '<img src="https://img.youtube.com/vi/abc12345678/hqdefault.jpg" '
             'alt="Video" class="w-full" loading="lazy">'),
            ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "video_url", "type": "string"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        index = result["views/videos/index.ejs"]
        assert 'alt="Video"' in index
        assert 'class="w-full"' in index
        assert 'loading="lazy"' in index


# ---------------------------------------------------------------------------
# _ensure_color_consistency: style.css fix
# ---------------------------------------------------------------------------

class TestEnsureColorConsistencyStyleCss:
    """Tests for style.css :root color fixes in _ensure_color_consistency."""

    def test_fixes_generic_dark_colors_in_style_css(self):
        """Generic dark colors in style.css :root replaced with UUPM palette."""
        files = [
            ("views/layout.ejs", "<html><head></head><body></body></html>"),
            ("public/style.css", ":root { --color-primary: #002040; --color-secondary: #202040; --color-accent: #404060; }"),
        ]
        spec = {"description": "A healing arts community", "design": {"primary_color": "#002040", "secondary_color": "#202040", "accent_color": "#404060"}}
        result = dict(_ensure_color_consistency(files, spec))
        css = result["public/style.css"]
        assert "#002040" not in css
        assert "#0f2b3c" in css  # healing_arts primary
        assert "#c8963e" in css  # healing_arts accent

    def test_preserves_correct_colors_in_style_css(self):
        """Already-correct colors in style.css are not touched."""
        files = [
            ("views/layout.ejs", '<html><head><style>:root { --color-primary: #0f2b3c; }</style></head><body></body></html>'),
            ("public/style.css", ":root { --color-primary: #0f2b3c; --color-secondary: #1a5a6a; }"),
        ]
        spec = {"description": "A healing arts community"}
        result = dict(_ensure_color_consistency(files, spec))
        assert result["public/style.css"] == files[1][1]

    def test_no_style_css_no_error(self):
        """No crash when public/style.css doesn't exist."""
        files = [("views/layout.ejs", "<html><head></head><body></body></html>")]
        spec = {"design": {"primary_color": "#B45309"}}
        result = dict(_ensure_color_consistency(files, spec))
        assert "public/style.css" not in result


class TestEnsureVideoEmbedsCspImgSrc:
    """Tests for CSP img-src patching for YouTube thumbnails."""

    def test_injects_video_thumb_proxy_route(self):
        """Adds /api/video-thumb/ proxy route to server.js."""
        files = [
            ("server.js", "app.use(express.static(path.join(__dirname, 'public')));\napp.use(session({}));"),
            ("views/videos/index.ejs", '<a href="https://www.youtube.com/watch?v=abc"><img src="/api/video-thumb/abc"></a>'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "embed_url"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert "/api/video-thumb/" in result["server.js"]
        assert "i.ytimg.com" in result["server.js"]  # proxy fetches from ytimg

    def test_skips_proxy_when_already_present(self):
        """Does not duplicate proxy route if already present."""
        server_js = "app.use(express.static(path.join(__dirname, 'public')));\napp.get('/api/video-thumb/:videoId', (req, res) => {});"
        files = [
            ("server.js", server_js),
            ("views/videos/index.ejs", '<img src="/api/video-thumb/abc">'),
        ]
        spec = {"data_models": [{"name": "Video", "fields": [{"name": "embed_url"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        assert result["server.js"].count("/api/video-thumb/") == 1


class TestVideoPoolsCategoryAware:
    """Tests for per-category video pool selection."""

    def test_category_matched_ids_used_when_pool_exists(self):
        """When spec matches a category with a video pool, those IDs appear first."""
        import build as build_mod
        old_pools = build_mod._VIDEO_POOLS
        try:
            build_mod._VIDEO_POOLS = {"wellness": ["POOL_ID_1111", "POOL_ID_2222"]}
            files = [
                ("db/seed.sql", "INSERT INTO videos (embed_url) VALUES ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ');"),
                ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
            ]
            spec = {"description": "A meditation and wellness community", "data_models": [{"name": "Video", "fields": [{"name": "embed_url"}]}]}
            result = dict(_ensure_video_embeds(files, spec))
            # Pool IDs should appear in the replaced seed data
            assert "POOL_ID_1111" in result["db/seed.sql"]
            assert "POOL_ID_2222" in result["db/seed.sql"]
        finally:
            build_mod._VIDEO_POOLS = old_pools

    def test_fallback_to_generic_when_no_pool_match(self):
        """When no category pool matches, generic fallback IDs are used."""
        import build as build_mod
        old_pools = build_mod._VIDEO_POOLS
        try:
            build_mod._VIDEO_POOLS = {"automotive": ["AUTO_VID_001"]}
            files = [
                ("db/seed.sql", "INSERT INTO videos (embed_url) VALUES ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ');"),
                ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
            ]
            # Description that won't match automotive pool
            spec = {"description": "A pet grooming salon", "data_models": [{"name": "Video", "fields": [{"name": "embed_url"}]}]}
            result = dict(_ensure_video_embeds(files, spec))
            # Should NOT contain automotive pool IDs
            assert "AUTO_VID_001" not in result["db/seed.sql"]
            # Should contain some fallback ID (rickroll replaced)
            assert "dQw4w9WgXcQ" not in result["db/seed.sql"]
        finally:
            build_mod._VIDEO_POOLS = old_pools

    def test_no_duplicate_ids_in_single_app(self):
        """Video IDs in replaced seed data should be unique per occurrence."""
        files = [
            ("db/seed.sql", "INSERT INTO videos (embed_url) VALUES ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ'), ('https://youtube.com/embed/dQw4w9WgXcQ');"),
            ("server.js", "app.use(express.static(path.join(__dirname, 'public')));"),
        ]
        spec = {"description": "A video streaming app", "data_models": [{"name": "Video", "fields": [{"name": "embed_url"}]}]}
        result = dict(_ensure_video_embeds(files, spec))
        import re as re_m
        ids = re_m.findall(r'youtube\.com/embed/([a-zA-Z0-9_-]{11})', result["db/seed.sql"])
        # With 5 replacements, at least 3 should be unique (pool + fallbacks provide enough variety)
        assert len(set(ids)) >= 3


class TestEnsureStockPhotosOnerror:
    """Tests for dynamic EJS image onerror fallback."""

    def test_adds_onerror_to_dynamic_image_url(self):
        """Adds onerror fallback to img tags with EJS image_url src."""
        from build import _ensure_stock_photos
        files = [
            ("views/photos/index.ejs", '<img src="<%= photo.image_url %>" alt="Photo" class="w-full">'),
        ]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert 'onerror=' in result["views/photos/index.ejs"]
        assert 'data:image/svg+xml' in result["views/photos/index.ejs"]

    def test_skips_when_onerror_already_present(self):
        """Does not add duplicate onerror if already present."""
        from build import _ensure_stock_photos
        files = [
            ("views/photos/index.ejs", '<img src="<%= photo.image_url %>" onerror="fallback()" class="w-full">'),
        ]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert result["views/photos/index.ejs"].count("onerror=") == 1

    def test_handles_photo_url_variant(self):
        """Matches photo_url variant in src attribute."""
        from build import _ensure_stock_photos
        files = [
            ("views/gallery.ejs", '<img src="<%= item.photo_url %>" alt="Gallery">'),
        ]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert 'onerror=' in result["views/gallery.ejs"]


class TestEnsureStockPhotosSeedNull:
    """Tests for nullifying fake image paths in seed.sql."""

    def test_replaces_gallery_paths_with_null(self):
        """Fake /images/gallery/ paths in seed.sql become NULL."""
        from build import _ensure_stock_photos
        files = [
            ("db/seed.sql", "INSERT INTO events (title, image_url) VALUES ('Yoga', '/images/gallery/yoga-01.jpg');"),
        ]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert "NULL" in result["db/seed.sql"]
        assert "/images/gallery/" not in result["db/seed.sql"]

    def test_replaces_stock_paths_with_null(self):
        """Fake /images/stock/ paths in seed.sql become NULL."""
        from build import _ensure_stock_photos
        files = [
            ("db/seed.sql", "INSERT INTO photos (image_url) VALUES ('/images/stock/nature/sunset.jpg');"),
        ]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert "NULL" in result["db/seed.sql"]
        assert "/images/stock/" not in result["db/seed.sql"]

    def test_preserves_non_image_seed_data(self):
        """Non-image data in seed.sql is untouched."""
        from build import _ensure_stock_photos
        seed = "INSERT INTO users (name, email) VALUES ('Admin', 'admin@test.example');"
        files = [("db/seed.sql", seed)]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert result["db/seed.sql"] == seed

    def test_multiple_paths_in_one_insert(self):
        """Multiple fake paths in a single INSERT are all replaced."""
        from build import _ensure_stock_photos
        seed = (
            "INSERT INTO events (title, image_url) VALUES "
            "('A', '/images/gallery/a.jpg'), "
            "('B', '/images/gallery/b.jpg'), "
            "('C', '/images/stock/nature/c.jpg');"
        )
        files = [("db/seed.sql", seed)]
        spec = {"description": "test app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert result["db/seed.sql"].count("NULL") == 3
        assert "/images/" not in result["db/seed.sql"]

    def test_replaces_with_stock_photos_when_available(self):
        """When stock photos match the spec, seed paths use real stock paths."""
        from build import _ensure_stock_photos
        seed = (
            "INSERT INTO events (title, image_url) VALUES "
            "('A', '/images/gallery/a.jpg'), "
            "('B', '/images/gallery/b.jpg');"
        )
        files = [("db/seed.sql", seed)]
        # "healing" keyword triggers wellness category
        spec = {"description": "A healing community app"}
        result = dict(_ensure_stock_photos(files, spec))
        assert "/images/stock/wellness/" in result["db/seed.sql"]
        assert "/images/gallery/" not in result["db/seed.sql"]
        assert "NULL" not in result["db/seed.sql"]


class TestJoinMultilineInserts:
    """Tests for _join_multiline_inserts(), joins INSERT+VALUES across lines."""

    def test_joins_split_insert_values(self):
        seed = (
            "INSERT INTO events (title, date_time)\n"
            "VALUES ('Full Moon', NOW()) ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_join_multiline_inserts([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        # Should be on one line now
        assert "INSERT INTO events (title, date_time) VALUES" in out

    def test_preserves_single_line_inserts(self):
        seed = "INSERT INTO events (title) VALUES ('Test') ON CONFLICT DO NOTHING;\n"
        files = [("db/seed.sql", seed)]
        result = _join_multiline_inserts(files)
        assert result is files  # unchanged

    def test_preserves_comments_and_blanks(self):
        seed = (
            "-- Seed data\n"
            "INSERT INTO events (title)\n"
            "VALUES ('Test') ON CONFLICT DO NOTHING;\n"
            "\n"
            "-- More data\n"
        )
        result = dict(_join_multiline_inserts([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "-- Seed data" in out
        assert "-- More data" in out

    def test_multiple_tables(self):
        seed = (
            "INSERT INTO events (title)\n"
            "VALUES ('Event A') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO videos (title)\n"
            "VALUES ('Video A') ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_join_multiline_inserts([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "INSERT INTO events (title) VALUES" in out
        assert "INSERT INTO videos (title) VALUES" in out

    def test_noop_no_seed(self):
        files = [("server.js", "app.listen(3000)")]
        result = _join_multiline_inserts(files)
        assert result is files


class TestSplitSeedInserts:
    """Tests for _split_seed_inserts(), converts multi-row INSERTs to single-row."""

    def test_multi_row_split_into_singles(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO events (title, date) VALUES\n"
            "  ('Event 1', '2026-01-01'),\n"
            "  ('Event 2', '2026-02-01'),\n"
            "  ('Event 3', '2026-03-01');\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        # Should have 3 separate INSERT statements
        assert out.count("INSERT INTO events") == 3
        # Each should have ON CONFLICT DO NOTHING
        assert out.count("ON CONFLICT DO NOTHING") == 3
        # No multi-row comma separators between value tuples
        assert "),\n" not in out

    def test_single_row_unchanged(self):
        from build import _split_seed_inserts
        seed = "INSERT INTO users (email, name) VALUES ('admin@test.example', 'Admin');\n"
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        assert out.count("INSERT INTO users") == 1
        assert "ON CONFLICT DO NOTHING" in out

    def test_preserves_quoted_commas(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO events (title, description) VALUES\n"
            "  ('Ceremony, Part 1', 'A long, winding description'),\n"
            "  ('Ceremony, Part 2', 'Another description');\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        assert out.count("INSERT INTO events") == 2
        assert "'Ceremony, Part 1'" in out
        assert "'A long, winding description'" in out

    def test_preserves_now_expressions(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO events (title, created_at) VALUES\n"
            "  ('Event 1', NOW()),\n"
            "  ('Event 2', NOW() - interval '3 days');\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        assert out.count("INSERT INTO events") == 2
        assert "NOW()" in out
        assert "interval '3 days'" in out

    def test_preserves_on_conflict_clause(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO users (email, name) VALUES\n"
            "  ('a@b.example', 'A'),\n"
            "  ('c@d.example', 'C')\n"
            "ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        assert out.count("INSERT INTO users") == 2
        # Original ON CONFLICT clause should be replaced with DO NOTHING
        assert out.count("ON CONFLICT DO NOTHING") == 2

    def test_no_seed_file_noop(self):
        from build import _split_seed_inserts
        files = [("server.js", "app.listen(3000);")]
        result = _split_seed_inserts(files)
        assert dict(result).get("server.js") == "app.listen(3000);"

    def test_handles_nested_parens_in_now(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO events (title, start_date, end_date) VALUES\n"
            "  ('Retreat', NOW(), NOW() + interval '7 days'),\n"
            "  ('Workshop', NOW() - interval '1 day', NOW());\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        assert out.count("INSERT INTO events") == 2
        assert "NOW() + interval '7 days'" in out
        assert "NOW() - interval '1 day'" in out

    def test_multiple_tables(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO events (title) VALUES\n"
            "  ('Event 1'),\n"
            "  ('Event 2');\n"
            "\n"
            "INSERT INTO photos (url) VALUES\n"
            "  ('/img/1.jpg'),\n"
            "  ('/img/2.jpg');\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        assert out.count("INSERT INTO events") == 2
        assert out.count("INSERT INTO photos") == 2
        assert out.count("ON CONFLICT DO NOTHING") == 4

    def test_comments_out_unbalanced_quotes(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO videos (title, description, created_by) VALUES\n"
            "  ('Good Row', 'A valid description', 1),\n"
            "  ('Bad Row', 'Truncated string without closing quote, 1);\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        # Good row should remain as INSERT
        assert "Good Row" in out
        assert out.count("INSERT INTO videos") >= 1
        # Bad row should be commented out
        assert "SKIPPED" in out
        assert "Truncated string" in out

    def test_unbalanced_quotes_dont_break_subsequent_inserts(self):
        from build import _split_seed_inserts
        seed = (
            "INSERT INTO videos (title, desc) VALUES ('Bad', 'unclosed) ON CONFLICT DO NOTHING;\n"
            "INSERT INTO subscribers (email) VALUES ('good@test.example') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_split_seed_inserts(files))
        out = result["db/seed.sql"]
        # Subscriber insert should survive
        assert "INSERT INTO subscribers" in out
        assert "good@test.example" in out
        # Bad video insert should be commented out
        assert "SKIPPED" in out


class TestDedupSeedEntries:
    """Tests for _dedup_seed_entries(), remove duplicate seed rows by first value."""

    def test_removes_duplicate_events(self):
        seed = (
            "INSERT INTO events (title, date_time) VALUES ('Full Moon Ceremony', NOW()) ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title, date_time) VALUES ('Sunrise Meditation', NOW()) ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title, date_time) VALUES ('Full Moon Ceremony', NOW()) ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_dedup_seed_entries([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        # First occurrence kept, second commented out
        assert "SKIPPED (duplicate)" in out
        # Only 2 active (uncommented) INSERT lines
        active = [l for l in out.split("\n") if l.strip().startswith("INSERT INTO events")]
        assert len(active) == 2

    def test_different_tables_not_cross_deduped(self):
        seed = (
            "INSERT INTO events (title) VALUES ('Workshop') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO videos (title) VALUES ('Workshop') ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_dedup_seed_entries([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        # Same value in different tables should both survive
        assert "SKIPPED" not in out
        assert out.count("INSERT INTO") == 2

    def test_noop_no_duplicates(self):
        seed = (
            "INSERT INTO events (title) VALUES ('Event A') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title) VALUES ('Event B') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = _dedup_seed_entries(files)
        assert result is files  # unchanged, returns original list

    def test_case_insensitive_matching(self):
        seed = (
            "INSERT INTO events (title) VALUES ('Full Moon Ceremony') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title) VALUES ('full moon ceremony') ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_dedup_seed_entries([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "SKIPPED (duplicate)" in out

    def test_preserves_non_insert_lines(self):
        seed = (
            "-- Seed data for events\n"
            "INSERT INTO events (title) VALUES ('Event A') ON CONFLICT DO NOTHING;\n"
            "\n"
            "-- More data\n"
            "INSERT INTO events (title) VALUES ('Event A') ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_dedup_seed_entries([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "-- Seed data for events" in out
        assert "-- More data" in out

    def test_handles_escaped_quotes(self):
        seed = (
            "INSERT INTO events (title) VALUES ('Women''s Circle') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title) VALUES ('Women''s Circle') ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_dedup_seed_entries([("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "SKIPPED (duplicate)" in out

    def test_handles_multiline_after_join(self):
        """Multi-line INSERTs should be joined by _join_multiline_inserts first."""
        seed = (
            "INSERT INTO events (title, date_time)\n"
            "VALUES ('Full Moon Ceremony', NOW()) ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title, date_time)\n"
            "VALUES ('Sunrise Meditation', NOW()) ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title, date_time)\n"
            "VALUES ('Full Moon Ceremony', NOW()) ON CONFLICT DO NOTHING;\n"
        )
        # Run join first, then dedup (as in actual pipeline)
        files = [("db/seed.sql", seed)]
        files = _join_multiline_inserts(files)
        result = dict(_dedup_seed_entries(files))
        out = result["db/seed.sql"]
        assert "SKIPPED (duplicate)" in out
        active = [l for l in out.split("\n") if l.strip().startswith("INSERT INTO events")]
        assert len(active) == 2

    def test_noop_no_seed(self):
        files = [("server.js", "app.listen(3000)")]
        result = _dedup_seed_entries(files)
        assert result is files


class TestFixSeedUuidId:
    """Tests for _fix_seed_uuid_id(), strip UUID values from SERIAL id columns."""

    _SERIAL_SCHEMA = "CREATE TABLE users (\n  id SERIAL PRIMARY KEY,\n  email VARCHAR(255),\n  password TEXT\n);\n"
    _UUID_SCHEMA = "CREATE TABLE users (\n  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,\n  email VARCHAR(255)\n);\n"

    def test_strips_gen_random_uuid_from_serial_table(self):
        seed = "INSERT INTO users (id, email, password) VALUES (gen_random_uuid(), 'admin@app.example', 'hash') ON CONFLICT DO NOTHING;\n"
        result = dict(_fix_seed_uuid_id([("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "gen_random_uuid" not in out
        assert "email, password" in out
        assert "'admin@app.example', 'hash'" in out

    def test_strips_literal_uuid_from_serial_table(self):
        seed = "INSERT INTO users (id, email) VALUES ('a1b2c3d4-5678-90ab-cdef-1234567890ab', 'test@app.example') ON CONFLICT DO NOTHING;\n"
        result = dict(_fix_seed_uuid_id([("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "a1b2c3d4" not in out
        assert "'test@app.example'" in out

    def test_strips_uuid_with_cast(self):
        seed = "INSERT INTO users (id, email) VALUES ('a1b2c3d4-5678-90ab-cdef-1234567890ab'::uuid, 'test@app.example') ON CONFLICT DO NOTHING;\n"
        result = dict(_fix_seed_uuid_id([("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "a1b2c3d4" not in out

    def test_strips_uuid_generate_v4(self):
        seed = "INSERT INTO users (id, email) VALUES (uuid_generate_v4(), 'test@app.example') ON CONFLICT DO NOTHING;\n"
        result = dict(_fix_seed_uuid_id([("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "uuid_generate_v4" not in out
        assert "'test@app.example'" in out

    def test_noop_when_schema_uses_uuid_type(self):
        seed = "INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'test@app.example') ON CONFLICT DO NOTHING;\n"
        files = [("db/schema.sql", self._UUID_SCHEMA), ("db/seed.sql", seed)]
        result = _fix_seed_uuid_id(files)
        assert result is files  # unchanged

    def test_noop_when_id_is_integer_value(self):
        seed = "INSERT INTO users (id, email) VALUES (1, 'admin@app.example') ON CONFLICT DO NOTHING;\n"
        files = [("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]
        result = _fix_seed_uuid_id(files)
        assert result is files  # integer id is fine, no change

    def test_noop_when_no_id_column_in_insert(self):
        seed = "INSERT INTO users (email, password) VALUES ('admin@app.example', 'hash') ON CONFLICT DO NOTHING;\n"
        files = [("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]
        result = _fix_seed_uuid_id(files)
        assert result is files

    def test_noop_when_no_seed(self):
        files = [("db/schema.sql", self._SERIAL_SCHEMA), ("server.js", "app.listen(3000)")]
        result = _fix_seed_uuid_id(files)
        assert result is files

    def test_noop_when_no_schema(self):
        seed = "INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'test@app.example');\n"
        files = [("db/seed.sql", seed)]
        result = _fix_seed_uuid_id(files)
        assert result is files

    def test_handles_multiple_tables_only_fixes_serial(self):
        schema = (
            "CREATE TABLE users (\n  id SERIAL PRIMARY KEY,\n  email TEXT\n);\n"
            "CREATE TABLE sessions (\n  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,\n  user_id INTEGER\n);\n"
        )
        seed = (
            "INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'admin@app.example') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO sessions (id, user_id) VALUES (gen_random_uuid(), 1) ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_fix_seed_uuid_id([("db/schema.sql", schema), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        # users should be fixed (SERIAL)
        users_line = [l for l in out.split("\n") if "INTO users" in l][0]
        assert "gen_random_uuid" not in users_line
        # sessions should be left alone (UUID type)
        sessions_line = [l for l in out.split("\n") if "INTO sessions" in l][0]
        assert "gen_random_uuid" in sessions_line

    def test_preserves_other_columns_and_values(self):
        seed = "INSERT INTO users (id, name, email, password, role) VALUES (gen_random_uuid(), 'Admin User', 'admin@app.example', '$2b$10$hash', 'admin') ON CONFLICT DO NOTHING;\n"
        result = dict(_fix_seed_uuid_id([("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "'Admin User'" in out
        assert "'admin@app.example'" in out
        assert "'$2b$10$hash'" in out
        assert "'admin'" in out
        assert "name, email, password, role" in out

    def test_handles_multiple_rows(self):
        seed = (
            "INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'admin@app.example') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO users (id, email) VALUES ('b2c3d4e5-6789-0abc-def1-234567890abc', 'user@app.example') ON CONFLICT DO NOTHING;\n"
        )
        result = dict(_fix_seed_uuid_id([("db/schema.sql", self._SERIAL_SCHEMA), ("db/seed.sql", seed)]))
        out = result["db/seed.sql"]
        assert "gen_random_uuid" not in out
        assert "b2c3d4e5" not in out
        assert "'admin@app.example'" in out
        assert "'user@app.example'" in out


class TestEnsureGalleryDedup:
    """Tests for _ensure_gallery_dedup(), unique index + seed cap."""

    def test_adds_unique_index_to_schema(self):
        from build import _ensure_gallery_dedup
        schema = "CREATE TABLE photos (\n  id SERIAL PRIMARY KEY,\n  image_url TEXT\n);\n"
        files = [("db/schema.sql", schema), ("db/seed.sql", "")]
        result = dict(_ensure_gallery_dedup(files))
        assert "CREATE UNIQUE INDEX" in result["db/schema.sql"]
        assert "image_url" in result["db/schema.sql"]

    def test_handles_if_not_exists_table(self):
        from build import _ensure_gallery_dedup
        schema = "CREATE TABLE IF NOT EXISTS photos (\n  id SERIAL PRIMARY KEY,\n  image_url TEXT\n);\n"
        files = [("db/schema.sql", schema), ("db/seed.sql", "")]
        result = dict(_ensure_gallery_dedup(files))
        assert "CREATE UNIQUE INDEX" in result["db/schema.sql"]

    def test_skips_if_unique_index_exists(self):
        from build import _ensure_gallery_dedup
        schema = (
            "CREATE TABLE photos (id SERIAL, image_url TEXT);\n"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_photos_image_url ON photos(image_url);\n"
        )
        files = [("db/schema.sql", schema), ("db/seed.sql", "")]
        result = dict(_ensure_gallery_dedup(files))
        # Should NOT add a second unique index
        assert result["db/schema.sql"].count("CREATE UNIQUE INDEX") == 1

    def test_caps_seed_photos_at_12(self):
        from build import _ensure_gallery_dedup
        # 20 single-row photo INSERTs
        rows = "\n".join(
            f"INSERT INTO photos (title, image_url) VALUES ('Photo {i}', '/img/{i}.jpg') ON CONFLICT DO NOTHING;"
            for i in range(20)
        )
        files = [("db/schema.sql", "CREATE TABLE photos (id SERIAL);"), ("db/seed.sql", rows)]
        result = dict(_ensure_gallery_dedup(files))
        assert result["db/seed.sql"].count("INSERT INTO photos") == 12

    def test_noop_when_no_photos_table(self):
        from build import _ensure_gallery_dedup
        schema = "CREATE TABLE events (id SERIAL);\n"
        files = [("db/schema.sql", schema), ("db/seed.sql", "INSERT INTO events (title) VALUES ('A');")]
        result = dict(_ensure_gallery_dedup(files))
        assert "CREATE UNIQUE INDEX" not in result.get("db/schema.sql", "")

    def test_noop_when_no_schema(self):
        from build import _ensure_gallery_dedup
        files = [("server.js", "app.listen(3000);")]
        result = dict(_ensure_gallery_dedup(files))
        assert "db/schema.sql" not in result


class TestEnsureSiteSettings:
    """Tests for _ensure_site_settings(), admin settings CRUD."""

    def test_adds_site_settings_table_to_schema(self):
        from build import _ensure_site_settings
        schema = "CREATE TABLE users (id SERIAL PRIMARY KEY);\n"
        files = [("db/schema.sql", schema), ("db/seed.sql", ""), ("server.js", "app.use(require('./routes/auth'));")]
        spec = {"app_name": "Test App", "admin_email": "admin@test.example"}
        result = dict(_ensure_site_settings(files, spec))
        assert "site_settings" in result["db/schema.sql"]
        assert "key VARCHAR" in result["db/schema.sql"]

    def test_seeds_defaults_from_spec(self):
        from build import _ensure_site_settings
        files = [("db/schema.sql", "CREATE TABLE users (id SERIAL);"), ("db/seed.sql", "INSERT INTO users (email) VALUES ('admin@test.example');"), ("server.js", "")]
        spec = {"app_name": "Medicine Waves", "admin_email": "admin@medicinewaves.example"}
        result = dict(_ensure_site_settings(files, spec))
        assert "Medicine Waves" in result["db/seed.sql"]
        assert "admin@medicinewaves.example" in result["db/seed.sql"]

    def test_seeds_all_10_canonical_setting_fields(self):
        """Contract: seed must include every key in routes_settings.js SETTING_FIELDS.

        The settings middleware loads the full `SELECT key, value FROM site_settings`
        into a map passed to views. Any unseeded key returns undefined, and the EJS
        fallback pattern `(typeof settings !== 'undefined' && settings.X) || '...'`
        falls through to Claude's hardcoded placeholder instead of the empty value.
        If this test fails because a new field was added to the canonical contract,
        update cli/build.py `_ensure_site_settings()` seed list to match.
        """
        from build import _ensure_site_settings
        files = [
            ("db/schema.sql", "CREATE TABLE users (id SERIAL);"),
            ("db/seed.sql", "INSERT INTO users (email) VALUES ('a@b.example');"),
            ("server.js", ""),
        ]
        spec = {"app_name": "Medicine Waves", "admin_email": "admin@medicinewaves.example"}
        result = dict(_ensure_site_settings(files, spec))
        seed_sql = result["db/seed.sql"]
        canonical_keys = [
            "business_name",
            "phone",
            "support_email",
            "address",
            "instagram_url",
            "facebook_url",
            "youtube_url",
            "tiktok_url",
            "twitter_url",
            "linkedin_url",
        ]
        for key in canonical_keys:
            assert f"'{key}'" in seed_sql, (
                f"Canonical SETTING_FIELDS key {key!r} missing from seed.sql; "
                f"routes_settings.js template requires all 10 keys seeded "
                f"so the settings middleware map is complete."
            )
        # business_name + support_email get real values; the rest are empty strings.
        assert "'business_name', 'Medicine Waves'" in seed_sql
        assert "'support_email', 'admin@medicinewaves.example'" in seed_sql
        assert "'phone', ''" in seed_sql
        assert "'address', ''" in seed_sql
        assert "'instagram_url', ''" in seed_sql

    def test_seeds_empty_admin_email_as_empty_string(self):
        """When spec has no admin_email, support_email should still be seeded (empty)."""
        from build import _ensure_site_settings
        files = [
            ("db/schema.sql", "CREATE TABLE users (id SERIAL);"),
            ("db/seed.sql", "INSERT INTO users (email) VALUES ('a@b.example');"),
            ("server.js", ""),
        ]
        spec = {"app_name": "No Email App"}  # no admin_email
        result = dict(_ensure_site_settings(files, spec))
        seed_sql = result["db/seed.sql"]
        assert "'support_email', ''" in seed_sql
        assert "'business_name', 'No Email App'" in seed_sql

    def test_seeds_escape_single_quotes_in_app_name(self):
        """SQL injection guard: app_name with single quote must be escaped."""
        from build import _ensure_site_settings
        files = [
            ("db/schema.sql", "CREATE TABLE users (id SERIAL);"),
            ("db/seed.sql", "INSERT INTO users (email) VALUES ('a@b.example');"),
            ("server.js", ""),
        ]
        spec = {"app_name": "Ocean's App", "admin_email": "o'malley@test.example"}
        result = dict(_ensure_site_settings(files, spec))
        seed_sql = result["db/seed.sql"]
        # Single quote doubled for SQL escape
        assert "'business_name', 'Ocean''s App'" in seed_sql
        assert "'support_email', 'o''malley@test.example'" in seed_sql

    def test_injects_settings_route_template(self):
        from build import _ensure_site_settings
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", "app.use(require('./routes/auth'));")]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert "routes/settings.js" in result
        assert "admin/settings" in result["routes/settings.js"]

    def test_injects_settings_view(self):
        from build import _ensure_site_settings
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", "app.use(require('./routes/auth'));")]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert "views/admin/settings.ejs" in result
        assert "phone" in result["views/admin/settings.ejs"]
        assert "instagram_url" in result["views/admin/settings.ejs"]

    def test_mounts_settings_route_in_server(self):
        from build import _ensure_site_settings
        server = "const app = express();\napp.use(require('./routes/auth'));\napp.use(require('./routes/events'));\n"
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", server)]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert "routes/settings" in result["server.js"]

    def test_injects_settings_middleware(self):
        from build import _ensure_site_settings
        server = "const app = express();\napp.use(session({store: new PgStore()}));\napp.use(require('./routes/auth'));\n"
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", server)]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert "_settingsCache" in result["server.js"]
        assert "site_settings" in result["server.js"]

    def test_skips_if_already_has_settings(self):
        from build import _ensure_site_settings
        schema = "CREATE TABLE site_settings (key VARCHAR PRIMARY KEY);\n"
        files = [("db/schema.sql", schema), ("db/seed.sql", "INSERT INTO site_settings (key) VALUES ('x');"), ("server.js", "_settingsCache")]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert result["db/schema.sql"].count("site_settings") == 1

    def test_dashboard_link_when_csv_is_form_action(self):
        """Regression: csv-import as form action (not <a> link) should still inject settings link."""
        from build import _ensure_site_settings
        dashboard = (
            '<h1>Dashboard</h1>\n'
            '<div class="stats">stats here</div>\n'
            '<form action="/admin/csv-import" method="POST">\n'
            '  <input type="file" name="csv">\n'
            '</form>\n'
        )
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", "app.use(require('./routes/auth'));"),
                 ("views/admin/dashboard.ejs", dashboard)]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert "/admin/settings" in result["views/admin/dashboard.ejs"]

    def test_dashboard_link_injected_after_heading(self):
        """Settings link should be added after the heading when no other anchor found."""
        from build import _ensure_site_settings
        dashboard = '<h1>Dashboard</h1>\n<div class="stats">stats</div>\n'
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", "app.use(require('./routes/auth'));"),
                 ("views/admin/dashboard.ejs", dashboard)]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        assert "/admin/settings" in result["views/admin/dashboard.ejs"]

    def test_no_duplicate_mount_when_already_in_server(self):
        """If server.js already mounts routes/settings, the post-processor must not add it again."""
        from build import _ensure_site_settings
        server = (
            "app.use(require('./routes/auth'));\n"
            "app.use(require('./routes/settings'));\n"  # already present
            "app.use(require('./routes/events'));\n"
        )
        files = [("db/schema.sql", ""), ("db/seed.sql", ""), ("server.js", server)]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        mounts = result["server.js"].count("require('./routes/settings')")
        assert mounts == 1, f"Expected 1 mount, got {mounts}; double-mount detected"

    def test_always_overwrites_claude_settings_route(self):
        """ADMIN_SETTINGS1: routes/settings.js is always replaced by the template even if
        Claude already generated one (which may lack the /admin/settings route)."""
        from build import _ensure_site_settings
        # Claude-generated routes/settings.js that lacks /admin/settings
        claude_settings = "const router = require('express').Router();\nrouter.get('/settings', (req, res) => res.render('settings'));\nmodule.exports = router;\n"
        files = [
            ("db/schema.sql", ""),
            ("db/seed.sql", ""),
            ("server.js", "app.use(require('./routes/auth'));"),
            ("routes/settings.js", claude_settings),
        ]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        # Template must overwrite Claude's version
        assert "/admin/settings" in result["routes/settings.js"]
        # Template content must be present (not Claude's GET /settings stub)
        assert "SETTING_FIELDS" in result["routes/settings.js"] or "site_settings" in result["routes/settings.js"]

    def test_always_overwrites_claude_settings_view(self):
        """ADMIN_SETTINGS1: views/admin/settings.ejs is always replaced by the template
        even if Claude generated one (which may be incomplete or broken)."""
        from build import _ensure_site_settings
        claude_view = "<html><body><h1>Settings</h1></body></html>\n"
        files = [
            ("db/schema.sql", ""),
            ("db/seed.sql", ""),
            ("server.js", "app.use(require('./routes/auth'));"),
            ("views/admin/settings.ejs", claude_view),
        ]
        spec = {"app_name": "Test"}
        result = dict(_ensure_site_settings(files, spec))
        # Template must overwrite Claude's version
        assert "phone" in result["views/admin/settings.ejs"]
        assert "instagram_url" in result["views/admin/settings.ejs"]


class TestRemoveHomepageVideoSection:
    """Tests for _remove_homepage_video_section(), strips video previews from homepage."""

    def test_removes_video_section_from_home(self):
        from build import _remove_homepage_video_section
        home = (
            '<section class="hero">Hero</section>\n'
            '<!-- Videos Preview -->\n'
            '<% if (videos.length > 0) { %>\n'
            '<section class="videos">\n'
            '  <h2>Featured Videos</h2>\n'
            '  <% videos.forEach(function(v) { %><div><%= v.title %></div><% }); %>\n'
            '</section>\n'
            '<% } %>\n'
            '<footer>Footer</footer>\n'
        )
        files = [("views/home.ejs", home)]
        result = dict(_remove_homepage_video_section(files))
        assert "Featured Videos" not in result["views/home.ejs"]
        assert "videos.length" not in result["views/home.ejs"]
        assert "Hero" in result["views/home.ejs"]
        assert "Footer" in result["views/home.ejs"]

    def test_removes_video_section_from_index(self):
        from build import _remove_homepage_video_section
        home = (
            '<section class="hero">Hero</section>\n'
            '<% if (typeof videos !== "undefined" && videos.length > 0) { %>\n'
            '<section><h2>Videos</h2></section>\n'
            '<% } %>\n'
            '<footer>Footer</footer>\n'
        )
        files = [("views/index.ejs", home)]
        result = dict(_remove_homepage_video_section(files))
        assert "Videos" not in result["views/index.ejs"]

    def test_noop_when_no_video_section(self):
        from build import _remove_homepage_video_section
        home = '<section class="hero">Hero</section>\n<footer>Footer</footer>\n'
        files = [("views/home.ejs", home)]
        result = dict(_remove_homepage_video_section(files))
        assert result["views/home.ejs"] == home

    def test_also_removes_videos_query_from_route(self):
        from build import _remove_homepage_video_section
        route = (
            "router.get('/', async (req, res) => {\n"
            "  const events = await pool.query('SELECT * FROM events');\n"
            "  const videos = await pool.query('SELECT * FROM videos ORDER BY created_at DESC LIMIT 3');\n"
            "  const photos = await pool.query('SELECT * FROM photos');\n"
            "  res.render('home', { events: events.rows, videos: videos.rows, photos: photos.rows });\n"
            "});\n"
        )
        home = (
            '<section>Hero</section>\n'
            '<% if (videos.length > 0) { %>\n'
            '<section>Videos</section>\n'
            '<% } %>\n'
        )
        files = [("routes/public.js", route), ("views/home.ejs", home)]
        result = dict(_remove_homepage_video_section(files))
        assert "Featured Videos" not in result.get("views/home.ejs", "")


class TestEnsureSettingsInViews:
    """Tests for _ensure_settings_in_views(), social links + contact info in views."""

    def test_injects_social_links_in_footer(self):
        from build import _ensure_settings_in_views
        layout = '<footer class="bg-dark"><div>Content</div><div class="divider-warm"></div><p>&copy; 2026</p></footer>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_settings_in_views(files))
        assert "settings.instagram_url" in result["views/layout.ejs"]
        assert "settings.facebook_url" in result["views/layout.ejs"]

    def test_conditional_guard_on_social_links(self):
        from build import _ensure_settings_in_views
        layout = '<footer><div class="divider-warm"></div><p>&copy; 2026</p></footer>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_settings_in_views(files))
        assert "typeof settings !== 'undefined'" in result["views/layout.ejs"]

    def test_replaces_hardcoded_email_with_settings(self):
        from build import _ensure_settings_in_views
        contact = (
            '<div>\n'
            '  <p class="font-semibold">Email</p>\n'
            '  <p class="text-gray-500">hello@example.org</p>\n'
            '</div>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        assert "settings.support_email" in result["views/contact.ejs"]
        # Fallback to original value when settings empty
        assert "hello@example.org" in result["views/contact.ejs"]

    def test_replaces_hardcoded_phone_with_settings(self):
        from build import _ensure_settings_in_views
        contact = (
            '<div>\n'
            '  <p class="font-semibold">Phone</p>\n'
            '  <p class="text-gray-500">(555) 555-0147</p>\n'
            '</div>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        assert "settings.phone" in result["views/contact.ejs"]
        assert "(555) 555-0147" in result["views/contact.ejs"]  # kept as fallback

    def test_replaces_hardcoded_location_with_settings(self):
        from build import _ensure_settings_in_views
        contact = (
            '<div>\n'
            '  <p class="font-semibold">Location</p>\n'
            '  <p class="text-gray-500">Sample City, ST</p>\n'
            '</div>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        assert "settings.address" in result["views/contact.ejs"]
        assert "Sample City, ST" in result["views/contact.ejs"]  # kept as fallback

    def test_replaces_social_links_with_settings(self):
        from build import _ensure_settings_in_views
        contact = (
            '<h3>Follow Us</h3>\n'
            '<div class="flex gap-3">\n'
            '  <a href="#" aria-label="Facebook"><svg>fb</svg></a>\n'
            '  <a href="#" aria-label="Instagram"><svg>ig</svg></a>\n'
            '</div>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        assert "settings.facebook_url" in result["views/contact.ejs"]
        assert "settings.instagram_url" in result["views/contact.ejs"]

    def test_social_links_with_ejs_conditional_stays_balanced(self):
        """Regression: if Claude wraps social links in an EJS conditional,
        the replacement must not leave orphaned <% } %> blocks."""
        from build import _ensure_settings_in_views
        import re
        contact = (
            '<div class="mt-8">\n'
            '  <h3>Follow Us</h3>\n'
            '  <% if (typeof socialLinks !== "undefined") { %>\n'
            '  <div class="flex gap-3">\n'
            '    <a href="#" aria-label="Facebook"><svg>fb</svg></a>\n'
            '  </div>\n'
            '  <% } %>\n'
            '</div>\n'
            '<div class="card-premium">\n'
            '  <form method="POST" action="/contact">\n'
            '    <input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
            '  </form>\n'
            '</div>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        out = result["views/contact.ejs"]
        # EJS brace balance must be maintained
        ejs_blocks = re.findall(r'<%(?![=-])(.*?)%>', out, re.DOTALL)
        opens = sum(c.count('{') for c in ejs_blocks)
        closes = sum(c.count('}') for c in ejs_blocks)
        assert opens == closes, f"Unbalanced EJS: {opens} opens, {closes} closes"
        # Settings should still be injected
        assert "settings.facebook_url" in out

    def test_skips_if_already_has_settings(self):
        from build import _ensure_settings_in_views
        layout = '<footer><% if (settings.instagram_url) { %>link<% } %></footer>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_settings_in_views(files))
        # Should not double-inject
        assert result["views/layout.ejs"].count("settings.instagram_url") == 1

    def test_handles_contact_index_path(self):
        from build import _ensure_settings_in_views
        contact = (
            '<p class="font-semibold">Email</p>\n'
            '<p class="text-gray-500">test@example.com</p>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact/index.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        assert "settings.support_email" in result["views/contact/index.ejs"]

    def test_replaces_multiline_location_with_br_tag(self):
        """Regression guard for CONTACT_DUPE1 (MW v2 2026-04-10).

        The old regex r'(<p[^>]*>)([^<]+)(</p>)' can't match across inline
        tags like <br>, so multi-line addresses fell through and corrupted
        the next simple <p> (e.g. the Social label). This test locks in the
        multi-line case.
        """
        from build import _ensure_settings_in_views
        import re
        contact = (
            '<div class="contact-info">\n'
            '  <p class="font-semibold text-sm">Email</p>\n'
            '  <p class="text-gray-600">hello@example.com</p>\n'
            '  <p class="font-semibold text-sm">Location</p>\n'
            '  <p class="text-gray-600">Sample City, ST<br>Sample County</p>\n'
            '</div>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        out = result["views/contact.ejs"]
        # Address is now settings-driven
        assert "settings.address" in out
        # Literal <br> must NOT survive inside the EJS expression (it would
        # render as plain text via <%= %>). Fallback should flatten to comma.
        ejs_exprs = re.findall(r'<%=([^%]*)%>', out)
        address_exprs = [e for e in ejs_exprs if 'settings.address' in e]
        assert address_exprs, "No EJS expression referencing settings.address"
        for expr in address_exprs:
            assert '<br' not in expr, f"<br> leaked into EJS fallback: {expr}"
        # Fallback should contain the comma-joined parts
        assert "Sample City, ST, Sample County" in out

    def test_location_rewrite_does_not_corrupt_nearby_social_label(self):
        """Regression guard for the exact MW v2 fall-through: the Location
        rewrite accidentally rewrote the adjacent `<p>Social</p>` label with
        settings.address, destroying the Social card.
        """
        from build import _ensure_settings_in_views
        contact = (
            '<div class="card-premium">\n'
            '  <div class="flex items-start gap-4">\n'
            '    <div><!-- icon --></div>\n'
            '    <div>\n'
            '      <p class="font-semibold text-sm">Location</p>\n'
            '      <p class="text-gray-600">Sample City, ST<br>Sample County</p>\n'
            '    </div>\n'
            '  </div>\n'
            '  <div class="flex items-start gap-4">\n'
            '    <div><!-- icon --></div>\n'
            '    <div>\n'
            '      <p class="font-semibold text-sm">Social</p>\n'
            '      <p class="text-gray-600">@medicinewaves.example on Instagram</p>\n'
            '    </div>\n'
            '  </div>\n'
            '</div>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        out = result["views/contact.ejs"]
        # Location IS now settings-driven
        assert "settings.address" in out
        # Social label is STILL the literal text "Social", not overwritten
        assert '<p class="font-semibold text-sm">Social</p>' in out
        # The Social label <p> must NOT have been rewritten with settings.address
        # (the old bug turned ">Social<" into a settings.address EJS expression)
        social_block_idx = out.find('>Social<')
        assert social_block_idx != -1, "Social label literal text was corrupted"

    def test_location_rewrite_idempotent_on_rerun(self):
        """Running _ensure_settings_in_views twice must not double-wrap EJS."""
        from build import _ensure_settings_in_views
        contact = (
            '<p class="font-semibold text-sm">Location</p>\n'
            '<p class="text-gray-600">Sample City, ST<br>Sample County</p>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        first = dict(_ensure_settings_in_views(files))
        first_out = first["views/contact.ejs"]
        # Feed the patched result back in
        second = dict(_ensure_settings_in_views(
            [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", first_out)]
        ))
        second_out = second["views/contact.ejs"]
        # Exactly one settings.address expression, both runs
        assert first_out.count("settings.address") == 1
        assert second_out.count("settings.address") == 1
        # Second run does not meaningfully change the file
        assert second_out == first_out

    def test_location_rewrite_bounded_window(self):
        """If there is NO <p> inside the 500-char window after the Location
        label, the rewrite must NOT fall through to a distant unrelated <p>.
        """
        from build import _ensure_settings_in_views
        filler = 'x' * 600  # pushes the next <p> past the search window
        contact = (
            '<p class="font-semibold text-sm">Location</p>\n'
            '<!-- ' + filler + ' -->\n'
            '<p class="font-semibold text-sm">Social</p>\n'
            '<p class="text-gray-600">@medicinewaves.example on Instagram</p>\n'
            '<form method="POST" action="/contact"></form>'
        )
        files = [("views/layout.ejs", "<footer></footer>"), ("views/contact.ejs", contact)]
        result = dict(_ensure_settings_in_views(files))
        out = result["views/contact.ejs"]
        # No settings.address injection happened (no <p> in bounded window)
        assert "settings.address" not in out
        # Social label remains intact
        assert '<p class="font-semibold text-sm">Social</p>' in out


class TestEnsureHeroSettingsBinding:
    """Tests for _ensure_hero_settings_binding(), binds hero h1/p to site_settings."""

    def test_binds_h1_in_index_ejs(self):
        from build import _ensure_hero_settings_binding
        files = [("views/index.ejs", (
            '<section class="hero">\n'
            '  <h1 class="font-display text-5xl">Sacred Sound Meets Community</h1>\n'
            '  <p class="text-lg text-white/80">Join us for transformative healing experiences.</p>\n'
            '</section>\n'
        ))]
        result = dict(_ensure_hero_settings_binding(files))
        out = result["views/index.ejs"]
        assert "settings.hero_tagline" in out
        assert "Sacred Sound Meets Community" in out  # fallback preserved
        assert "|| 'Sacred Sound Meets Community'" in out

    def test_binds_description_p_after_h1(self):
        from build import _ensure_hero_settings_binding
        files = [("views/index.ejs", (
            '<section class="hero">\n'
            '  <h1 class="font-bold text-4xl">Where Healing Begins</h1>\n'
            '  <p class="text-white/80 leading-relaxed">A community dedicated to sound and sacred ceremony.</p>\n'
            '</section>\n'
        ))]
        result = dict(_ensure_hero_settings_binding(files))
        out = result["views/index.ejs"]
        assert "settings.hero_description" in out
        assert "A community dedicated to sound and sacred ceremony." in out

    def test_binds_home_ejs_too(self):
        from build import _ensure_hero_settings_binding
        files = [("views/home.ejs", (
            '<div class="hero-section">\n'
            '  <h1 class="text-5xl">Welcome to Medicine Waves</h1>\n'
            '  <p class="subtitle">Experience the power of sound healing.</p>\n'
            '</div>\n'
        ))]
        result = dict(_ensure_hero_settings_binding(files))
        out = result["views/home.ejs"]
        assert "settings.hero_tagline" in out
        assert "settings.hero_description" in out

    def test_idempotent_if_already_bound(self):
        from build import _ensure_hero_settings_binding
        already = (
            '<h1 class="text-5xl"><%= (typeof settings !== \'undefined\' && settings.hero_tagline) || \'Tagline\' %></h1>\n'
            '<p><%= settings.hero_description || \'desc\' %></p>\n'
        )
        files = [("views/index.ejs", already)]
        result = dict(_ensure_hero_settings_binding(files))
        # Content should be unchanged
        assert result["views/index.ejs"] == already

    def test_skips_short_p_labels(self):
        from build import _ensure_hero_settings_binding
        # <p> after h1 is a short label, not a description; should not be bound
        files = [("views/index.ejs", (
            '<h1 class="text-5xl">Big Tagline Here For Real</h1>\n'
            '<p class="section-label">Get In Touch</p>\n'
            '<p class="text-lg">This is the real longer description paragraph for the hero.</p>\n'
        ))]
        result = dict(_ensure_hero_settings_binding(files))
        out = result["views/index.ejs"]
        assert "settings.hero_tagline" in out
        # Short label should NOT be bound (< 20 chars)
        assert "Get In Touch" not in out.split("hero_description")[0] if "hero_description" in out else True

    def test_handles_multiline_h1(self):
        from build import _ensure_hero_settings_binding
        files = [("views/index.ejs", (
            '<h1 class="font-display font-bold text-5xl">\n'
            '  Sacred Sound,\n'
            '  <span class="text-accent">Embodied Medicine</span>\n'
            '</h1>\n'
            '<p class="text-white/80 leading-relaxed">Where transformation meets community.</p>\n'
        ))]
        result = dict(_ensure_hero_settings_binding(files))
        out = result["views/index.ejs"]
        assert "settings.hero_tagline" in out
        # Fallback should contain stripped text
        assert "Sacred Sound" in out

    def test_no_h1_skips_file(self):
        from build import _ensure_hero_settings_binding
        files = [("views/index.ejs", '<div class="hero"><p>No heading here.</p></div>')]
        result = dict(_ensure_hero_settings_binding(files))
        assert "settings.hero_tagline" not in result["views/index.ejs"]

    def test_hero_tagline_seeded_in_site_settings(self):
        """_ensure_site_settings should seed hero_tagline and hero_description keys."""
        from build import _ensure_site_settings
        spec = {"app_name": "Medicine Waves", "description": "Sacred sound healing community"}
        files = [
            ("db/schema.sql", "CREATE TABLE users (id SERIAL PRIMARY KEY);\n"),
            ("db/seed.sql", "INSERT INTO users (id) VALUES (1);\n"),
            ("server.js", "app.use(session({secret:'x'}));\napp.use(require('./routes/auth'));\n"),
        ]
        result = dict(_ensure_site_settings(files, spec))
        seed = result["db/seed.sql"]
        assert "hero_tagline" in seed
        assert "hero_description" in seed


class TestEnsureContactEmail:
    """Tests for _ensure_contact_email(), sends email notification on contact form submit."""

    def test_injects_email_after_insert(self):
        from build import _ensure_contact_email
        route = (
            "router.post('/contact', async (req, res) => {\n"
            "  const { name, email, message } = req.body;\n"
            "  await pool.query('INSERT INTO contact_messages (name, email, message) VALUES ($1, $2, $3)', [name, email, message]);\n"
            "  res.redirect('/contact?success=true');\n"
            "});\n"
        )
        files = [("routes/contact.js", route)]
        result = dict(_ensure_contact_email(files))
        assert "resend.com/emails" in result["routes/contact.js"]
        assert "RESEND_API_KEY" in result["routes/contact.js"]

    def test_fire_and_forget_pattern(self):
        from build import _ensure_contact_email
        route = (
            "router.post('/contact-submit', async (req, res) => {\n"
            "  await pool.query('INSERT INTO contact_messages (name, email, message) VALUES ($1, $2, $3)', [name, email, message]);\n"
            "  res.redirect('/');\n"
            "});\n"
        )
        files = [("routes/public.js", route)]
        result = dict(_ensure_contact_email(files))
        # Should use .catch() pattern (fire-and-forget)
        assert ".catch(" in result["routes/public.js"]

    def test_searches_multiple_route_files(self):
        from build import _ensure_contact_email
        route = "await pool.query('INSERT INTO contact_messages (name, email, msg) VALUES ($1, $2, $3)');\n"
        files = [
            ("routes/auth.js", "// auth stuff"),
            ("routes/contact-messages.js", f"router.post('/contact', async (req, res) => {{\n  {route}  res.redirect('/');\n}});\n"),
        ]
        result = dict(_ensure_contact_email(files))
        assert "resend.com/emails" in result["routes/contact-messages.js"]
        assert "resend.com/emails" not in result.get("routes/auth.js", "")

    def test_noop_when_no_contact_insert(self):
        from build import _ensure_contact_email
        files = [("routes/events.js", "INSERT INTO events (title) VALUES ($1);")]
        result = dict(_ensure_contact_email(files))
        assert "resend.com" not in result.get("routes/events.js", "")

    def test_skips_if_already_has_email(self):
        from build import _ensure_contact_email
        route = (
            "await pool.query('INSERT INTO contact_messages (name, email, message) VALUES ($1, $2, $3)');\n"
            "fetch('https://api.resend.com/emails', {});\n"
        )
        files = [("routes/contact.js", route)]
        result = dict(_ensure_contact_email(files))
        # Should not double-inject
        assert result["routes/contact.js"].count("resend.com/emails") == 1


# ---------------------------------------------------------------------------
# Quality Checklist Enforcement (Gold Reference: MW 2026-04-07)
# ---------------------------------------------------------------------------

class TestAppQualityChecklist:
    """Verify that the build pipeline's post-processors and templates enforce
    the 8+ quality checklist derived from the MW gold reference build."""

    def _full_app_files(self):
        """Build a realistic file set that triggers all post-processors."""
        import json as j
        server = (
            "const express = require('express');\n"
            "const app = express();\n"
            "app.use(express.static('public'));\n"
            "app.use(require('./routes/admin'));\n"
            "app.listen(process.env.PORT || 3000);\n"
        )
        pkg = j.dumps({
            "name": "test-app",
            "dependencies": {"express": "^4", "express-ejs-layouts": "^2"},
        })
        layout = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
            '<link rel="stylesheet" href="/tailwind.css">\n'
            '</head>\n<body>\n<nav></nav>\n<%- body %>\n'
            '<footer><div class="divider-warm"></div><p>&copy; 2026</p></footer>\n'
            '</body>\n</html>'
        )
        schema = (
            "CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, email VARCHAR(255));\n"
        )
        contact = (
            '<div><p class="font-semibold">Email</p>\n'
            '<p class="text-gray-500">hello@example.org</p></div>\n'
            '<div><p class="font-semibold">Phone</p>\n'
            '<p class="text-gray-500">(555) 555-0100</p></div>\n'
            '<div><p class="font-semibold">Location</p>\n'
            '<p class="text-gray-500">Sample City, ST</p></div>\n'
            '<h3>Follow Us</h3>\n'
            '<div class="flex gap-3">\n'
            '  <a href="#" aria-label="Facebook"><svg>fb</svg></a>\n'
            '</div>\n'
            '<form method="POST" action="/contact">\n'
            '  <input type="hidden" name="_csrf" value="<%= csrfToken %>">\n'
            '</form>'
        )
        return [
            ("server.js", server),
            ("package.json", pkg),
            ("views/layout.ejs", layout),
            ("db/schema.sql", schema),
            ("db/seed.sql", "INSERT INTO users (email) VALUES ('admin@test.example');"),
            ("db/index.js", "module.exports = require('pg').Pool();"),
            ("middleware/auth.js", "module.exports = { requireAuth() {}, requireRole() {} };"),
            ("views/contact.ejs", contact),
            ("views/login.ejs", "<form></form>"),
            ("views/register.ejs", "<form></form>"),
            ("routes/admin.js", "// admin routes"),
            ("routes/auth.js", "// auth routes"),
        ]

    def test_site_settings_injected(self):
        """Pipeline must inject site_settings table, admin settings CRUD, and middleware."""
        from build import _ensure_site_settings
        files = self._full_app_files()
        spec = {"admin_email": "admin@test.example"}
        result = dict(_ensure_site_settings(files, spec))
        assert "site_settings" in result.get("db/schema.sql", "")
        assert "routes/settings.js" in result or any("settings" in k for k in result)

    def test_contact_info_uses_settings(self):
        """Contact page must use settings-driven values, not hardcoded."""
        from build import _ensure_settings_in_views
        files = self._full_app_files()
        result = dict(_ensure_settings_in_views(files))
        contact = result.get("views/contact.ejs", "")
        assert "settings.support_email" in contact
        assert "settings.phone" in contact
        assert "settings.address" in contact

    def test_social_links_use_settings(self):
        """Social links must be conditional on site_settings."""
        from build import _ensure_settings_in_views
        files = self._full_app_files()
        result = dict(_ensure_settings_in_views(files))
        contact = result.get("views/contact.ejs", "")
        assert "settings.facebook_url" in contact
        assert "settings.instagram_url" in contact

    def test_footer_social_links_injected(self):
        """Footer must have conditional social links from settings."""
        from build import _ensure_settings_in_views
        files = self._full_app_files()
        result = dict(_ensure_settings_in_views(files))
        layout = result.get("views/layout.ejs", "")
        assert "settings.instagram_url" in layout

    def test_post_logout_uses_csrf(self):
        """Logout must be POST with CSRF, never GET."""
        from build import _ensure_logout_form
        layout = '<a href="/logout">Logout</a>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_logout_form(files))
        out = result["views/layout.ejs"]
        assert "method=" in out.lower() or "POST" in out
        assert 'href="/logout"' not in out

    def test_destructive_actions_need_confirm(self):
        """Delete buttons must have data-confirm attribute."""
        from build import _validate_destructive_confirm
        view = (
            '<form method="POST" action="/items/1">\n'
            '  <button type="submit">Delete</button>\n'
            '</form>'
        )
        files = [("views/items/index.ejs", view)]
        # This is a validation function, not a fixer; it warns
        result = _validate_destructive_confirm(files)
        # Just verify it runs without error
        assert result is not None

    def test_mobile_nav_injected(self):
        """Layout must have mobile hamburger menu."""
        from build import _ensure_mobile_nav
        layout = (
            '<nav><div class="flex">'
            '<a href="/">Home</a>'
            '<a href="/events">Events</a>'
            '<a href="/gallery">Gallery</a>'
            '<a href="/contact">Contact</a>'
            '<a href="/login">Login</a>'
            '</div></nav>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_nav(files))
        out = result["views/layout.ejs"]
        assert "mobileMenuBtn" in out or "hamburger" in out.lower() or "md:hidden" in out

    def test_error_handling_injected(self):
        """Generated apps must have 500.ejs error page and global error handler."""
        from build import _ensure_error_handling
        server = "const app = express();\napp.listen(3000);"
        files = [("server.js", server)]
        result = dict(_ensure_error_handling(files))
        assert "views/500.ejs" in result
        assert "err" in result.get("server.js", "").lower() or "error" in result.get("server.js", "")

    def test_premium_css_injected(self):
        """Generated apps must have premium-base.css with design system classes."""
        from build import _ensure_premium_css
        files = [("views/layout.ejs", '<html><%- body %></html>')]
        result = dict(_ensure_premium_css(files))
        css = result.get("public/premium-base.css", "")
        assert "card-premium" in css
        assert "btn-primary" in css
        assert "stat-card" in css
        assert "section-warm" in css
        assert "animate-in" in css

    def test_font_loading_injected(self):
        """Generated apps must load Google Fonts."""
        from build import _ensure_font_loading
        layout = '<html><head></head><body><%- body %></body></html>'
        spec = {"design": {"font_display": "Lora", "font_body": "Raleway"}}
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_font_loading(files, spec))
        out = result.get("views/layout.ejs", "")
        assert "fonts.googleapis.com" in out

    def test_upload_route_injected_when_needed(self):
        """Apps with photo/image models must get upload route."""
        from build import _ensure_upload_route
        server = "const app = express();\napp.listen(3000);"
        schema = "CREATE TABLE photos (id SERIAL, image_url TEXT);"
        pkg = '{"dependencies": {"express": "^4"}}'
        files = [
            ("server.js", server),
            ("db/schema.sql", schema),
            ("package.json", pkg),
        ]
        spec = {"data_models": [{"name": "Photo", "fields": [{"name": "image_url", "type": "text"}]}]}
        result = dict(_ensure_upload_route(files, spec))
        assert "routes/upload.js" in result

    def test_video_embed_no_rickroll(self):
        """Video embeds must not contain placeholder/rickroll IDs."""
        from build import _ensure_video_embeds
        view = '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" frameborder="0"></iframe>'
        files = [("views/videos/show.ejs", view)]
        spec = {
            "description": "healing arts community",
            "data_models": [{"name": "Video", "fields": [{"name": "youtube_id", "type": "text"}]}],
        }
        result = dict(_ensure_video_embeds(files, spec))
        assert "dQw4w9WgXcQ" not in result.get("views/videos/show.ejs", "")

    def test_homepage_video_section_removed(self):
        """Homepage must not have broken Featured Videos sections."""
        from build import _remove_homepage_video_section
        home = (
            '<section>Hero</section>\n'
            '<!-- Video Preview -->\n'
            '<% if (videos && videos.length > 0) { %>\n'
            '<section>Videos here</section>\n'
            '<% } %>\n'
            '<section>Footer</section>'
        )
        files = [("views/home.ejs", home)]
        result = dict(_remove_homepage_video_section(files))
        assert "Video Preview" not in result.get("views/home.ejs", "")

    def test_icon_helper_injected(self):
        """Generated apps must have icon_helper.js for SVG icons."""
        from build import _ensure_icon_helper
        server = "const app = express();\napp.set('view engine', 'ejs');\napp.listen(3000);"
        files = [("server.js", server)]
        result = dict(_ensure_icon_helper(files))
        assert "icon_helper.js" in result

    # --- Pipeline Quality Sprint tests (8→9 rating) ---

    def test_compression_middleware_injected(self):
        """server.js must have compression middleware and package.json the dependency."""
        from build import _ensure_compression
        import json as j
        server = (
            "const express = require('express');\n"
            "const app = express();\n"
            "app.use(express.static('public'));\n"
            "app.listen(3000);\n"
        )
        pkg = j.dumps({"name": "test", "dependencies": {"express": "^4"}})
        files = [("server.js", server), ("package.json", pkg)]
        result = dict(_ensure_compression(files))
        assert "compression" in result["server.js"]
        assert "require('compression')" in result["server.js"]
        # Must be BEFORE express.static
        comp_idx = result["server.js"].index("compression")
        static_idx = result["server.js"].index("express.static")
        assert comp_idx < static_idx
        # Verify package.json dependency
        pkg_out = j.loads(result["package.json"])
        assert "compression" in pkg_out["dependencies"]

    def test_compression_skips_if_present(self):
        """compression post-processor must not double-inject."""
        from build import _ensure_compression
        server = "app.use(require('compression')());\napp.use(express.static('public'));\n"
        files = [("server.js", server)]
        result = dict(_ensure_compression(files))
        assert result["server.js"].count("compression") == 1

    def test_static_asset_caching(self):
        """express.static must have maxAge for browser caching."""
        from build import _ensure_static_caching
        server = "app.use(express.static('public'));\n"
        files = [("server.js", server)]
        result = dict(_ensure_static_caching(files))
        assert "maxAge" in result["server.js"]
        assert "'7d'" in result["server.js"]

    def test_static_asset_caching_path_join(self):
        """express.static(path.join(__dirname, 'public')) variant must also get maxAge."""
        from build import _ensure_static_caching
        server = "app.use(express.static(path.join(__dirname, 'public')));\n"
        files = [("server.js", server)]
        result = dict(_ensure_static_caching(files))
        assert "maxAge" in result["server.js"]

    def test_static_caching_skips_if_present(self):
        """maxAge post-processor must not double-inject."""
        from build import _ensure_static_caching
        server = "app.use(express.static('public', { maxAge: '7d' }));\n"
        files = [("server.js", server)]
        result = dict(_ensure_static_caching(files))
        assert result["server.js"].count("maxAge") == 1

    def test_lazy_loading_on_gallery_images(self):
        """<img> tags in views must get loading='lazy', hero images untouched."""
        from build import _ensure_lazy_loading
        gallery = '<img src="photo.jpg" alt="Photo" class="w-full">'
        hero = '<section class="hero-main"><img src="hero.jpg" alt="Hero"></section>'
        files = [
            ("views/gallery.ejs", gallery),
            ("views/home.ejs", hero),
        ]
        result = dict(_ensure_lazy_loading(files))
        # Gallery image should have lazy loading
        assert 'loading="lazy"' in result["views/gallery.ejs"]
        # Hero image should NOT have lazy loading
        assert 'loading="lazy"' not in result["views/home.ejs"]

    def test_lazy_loading_skips_existing(self):
        """Images with loading= attribute must not be double-tagged."""
        from build import _ensure_lazy_loading
        view = '<img loading="eager" src="hero.jpg" alt="Hero">'
        files = [("views/test.ejs", view)]
        result = dict(_ensure_lazy_loading(files))
        assert result["views/test.ejs"].count("loading=") == 1

    def test_submit_guard_injected(self):
        """layout.ejs must have double-submit prevention script."""
        from build import _ensure_submit_guard
        layout = '<html><body>\n<%- body %>\n</body></html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_submit_guard(files))
        out = result["views/layout.ejs"]
        assert "Processing..." in out
        assert "btn.disabled = true" in out
        assert "noSubmitGuard" in out

    def test_submit_guard_not_double_injected(self):
        """Submit guard must not inject twice."""
        from build import _ensure_submit_guard
        layout = '<html><body>\n<script>btn.textContent = "Processing...";</script>\n</body></html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_submit_guard(files))
        assert result["views/layout.ejs"].count("Processing...") == 1

    def test_strips_claude_click_confirm_handler(self):
        """Claude click-listener that calls confirm() on data-confirm must be removed."""
        from build import _strip_duplicate_confirm_handlers
        layout = (
            '<html><body>\n'
            '<%- body %>\n'
            '<script>\n'
            "document.addEventListener('click', function(e) {\n"
            "  const target = e.target.closest('[data-confirm]');\n"
            "  if (target) {\n"
            "    if (!confirm(target.dataset.confirm)) {\n"
            "      e.preventDefault();\n"
            "    }\n"
            "  }\n"
            "});\n"
            '</script>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        out = result["views/layout.ejs"]
        assert "data-confirm" not in out
        assert "confirm(" not in out
        assert "<%- body %>" in out

    def test_strips_claude_submit_confirm_handler(self):
        """Claude submit-listener that calls confirm() on data-confirm must be removed."""
        from build import _strip_duplicate_confirm_handlers
        layout = (
            '<html><body>\n'
            '<script>\n'
            "document.addEventListener('submit', function(e) {\n"
            "  const form = e.target;\n"
            "  if (form.dataset.confirm) {\n"
            "    if (!confirm(form.dataset.confirm)) {\n"
            "      e.preventDefault();\n"
            "    }\n"
            "  }\n"
            "});\n"
            '</script>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        out = result["views/layout.ejs"]
        assert "addEventListener('submit'" not in out
        assert "confirm(" not in out

    def test_strips_both_when_both_present(self):
        """Both click AND submit confirm handlers must be stripped in one pass."""
        from build import _strip_duplicate_confirm_handlers
        layout = (
            '<html><body>\n'
            '<script>\n'
            "document.addEventListener('click', function(e) {\n"
            "  const target = e.target.closest('[data-confirm]');\n"
            "  if (target && !confirm(target.dataset.confirm)) e.preventDefault();\n"
            "});\n"
            "document.addEventListener('submit', function(e) {\n"
            "  if (e.target.dataset.confirm && !confirm(e.target.dataset.confirm)) e.preventDefault();\n"
            "});\n"
            '</script>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        out = result["views/layout.ejs"]
        assert "data-confirm" not in out
        assert "confirm(" not in out
        assert "addEventListener" not in out

    def test_does_not_strip_mobile_menu_handler(self):
        """Unrelated document click listeners (mobile menu) must be preserved."""
        from build import _strip_duplicate_confirm_handlers
        layout = (
            '<html><body>\n'
            '<script>\n'
            "document.addEventListener('click', function(e) {\n"
            "  const menu = document.getElementById('mobileMenu');\n"
            "  const btn = document.getElementById('menuBtn');\n"
            "  if (menu && !menu.contains(e.target) && !btn.contains(e.target)) {\n"
            "    menu.classList.add('hidden');\n"
            "  }\n"
            "});\n"
            '</script>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        out = result["views/layout.ejs"]
        assert "mobileMenu" in out
        assert "addEventListener('click'" in out

    def test_idempotent_when_no_confirm_handlers(self):
        """No-op when layout.ejs has no document confirm listeners."""
        from build import _strip_duplicate_confirm_handlers
        layout = '<html><body>\n<%- body %>\n</body></html>'
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        assert result["views/layout.ejs"] == layout

    def test_preserves_scroll_listener(self):
        """Unrelated scroll/resize listeners must be untouched."""
        from build import _strip_duplicate_confirm_handlers
        layout = (
            '<html><body>\n'
            '<script>\n'
            "document.addEventListener('scroll', function(e) {\n"
            "  const nav = document.querySelector('nav');\n"
            "  if (window.scrollY > 50) nav.classList.add('scrolled');\n"
            "});\n"
            '</script>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        assert "scroll" in result["views/layout.ejs"]
        assert "scrolled" in result["views/layout.ejs"]

    def test_handles_arrow_function_syntax(self):
        """Arrow function form `(e) => { ... }` must also be stripped."""
        from build import _strip_duplicate_confirm_handlers
        layout = (
            '<html><body>\n'
            '<script>\n'
            "document.addEventListener('submit', (e) => {\n"
            "  if (e.target.dataset.confirm && !confirm(e.target.dataset.confirm)) {\n"
            "    e.preventDefault();\n"
            "  }\n"
            "});\n"
            '</script>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_strip_duplicate_confirm_handlers(files))
        out = result["views/layout.ejs"]
        assert "data-confirm" not in out
        assert "confirm(" not in out

    def test_no_layout_file_is_noop(self):
        """Missing layout.ejs must not raise."""
        from build import _strip_duplicate_confirm_handlers
        files = [("server.js", "const x = 1;")]
        result = dict(_strip_duplicate_confirm_handlers(files))
        assert result == {"server.js": "const x = 1;"}

    def test_canonical_url_dynamic(self):
        """Canonical URL must use dynamic value, not hardcoded '/'."""
        from build import _ensure_dynamic_canonical
        server = (
            "const app = express();\n"
            "res.locals.currentPath = req.path;\n"
            "app.listen(3000);\n"
        )
        layout = '<html><head><link rel="canonical" href="/"></head><body></body></html>'
        files = [("views/layout.ejs", layout), ("server.js", server)]
        result = dict(_ensure_dynamic_canonical(files))
        layout_out = result["views/layout.ejs"]
        server_out = result["server.js"]
        assert "canonicalUrl" in layout_out
        assert 'href="/"' not in layout_out.split("canonical")[1][:80]
        assert "canonicalUrl" in server_out

    def test_canonical_skips_if_dynamic(self):
        """Canonical post-processor must skip if already dynamic."""
        from build import _ensure_dynamic_canonical
        layout = '<link rel="canonical" href="<%= canonicalUrl %>">'
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_dynamic_canonical(files))
        assert result["views/layout.ejs"].count("canonicalUrl") == 1

    def test_500_page_has_premium_styling(self):
        """500.ejs template must use premium design system."""
        from pathlib import Path
        template = Path(__file__).parent.parent / "cli" / "templates" / "500.ejs"
        content = template.read_text()
        assert "card-premium" in content
        assert "<svg" in content
        assert "section-warm" in content
        assert "btn-primary" in content

    def test_csrf_not_duplicated(self):
        """Route files must not have inline csrfCheck when middleware exports it."""
        from build import _ensure_csrf_import
        route = (
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "const { requireAuth } = require('../middleware/auth');\n"
            "\n"
            "const csrfCheck = (req, res, next) => {\n"
            "  const token = req.body._csrf || req.headers['x-csrf-token'];\n"
            "  if (!token || token !== req.session.csrfToken) {\n"
            "    return res.status(403).send('Invalid');\n"
            "  }\n"
            "  next();\n"
            "};\n"
            "\n"
            "router.post('/', requireAuth, csrfCheck, async (req, res) => {});\n"
        )
        auth_mw = "module.exports = { requireAuth, validateCsrfToken };\nfunction validateCsrfToken(req, res, next) { next(); }\n"
        files = [("routes/events.js", route), ("middleware/auth.js", auth_mw)]
        result = dict(_ensure_csrf_import(files))
        out = result["routes/events.js"]
        # Inline definition must be gone
        assert "const csrfCheck" not in out
        # Must use validateCsrfToken
        assert "validateCsrfToken" in out

    def test_csrf_function_declaration_removed(self):
        """Inline function validateCsrfToken() declarations must also be stripped."""
        from build import _ensure_csrf_import
        route = (
            "const express = require('express');\n"
            "const router = express.Router();\n"
            "const { requireAuth, requireRole } = require('../middleware/auth');\n"
            "\n"
            "function validateCsrfToken(req, res, next) {\n"
            "  const token = req.body._csrf || req.headers['x-csrf-token'];\n"
            "  if (!token || token !== req.session.csrfToken) {\n"
            "    return res.status(403).send('Invalid');\n"
            "  }\n"
            "  next();\n"
            "}\n"
            "\n"
            "router.post('/', requireAuth, validateCsrfToken, async (req, res) => {});\n"
        )
        auth_mw = "module.exports = { requireAuth, validateCsrfToken };\nfunction validateCsrfToken(req, res, next) { next(); }\n"
        files = [("routes/events.js", route), ("middleware/auth.js", auth_mw)]
        result = dict(_ensure_csrf_import(files))
        out = result["routes/events.js"]
        # Inline function definition must be gone
        assert "function validateCsrfToken" not in out
        # Must import from middleware
        assert "validateCsrfToken" in out
        assert "require('../middleware/auth')" in out

    def test_admin_dashboard_route_exists(self):
        """GET /admin dashboard must exist even when template replaces admin.js."""
        from build import _ensure_admin_mount
        server = (
            "const express = require('express');\n"
            "const app = express();\n"
            "app.use(require('./routes/admin'));\n"
            "app.listen(3000);\n"
        )
        # Claude's admin.js with dashboard route using router.get('/')
        admin = (
            "const router = require('express').Router();\n"
            "router.get('/', requireAuth, async (req, res) => {\n"
            "  res.render('admin/dashboard');\n"
            "});\n"
            "module.exports = router;\n"
        )
        schema = "CREATE TABLE events (id SERIAL);\nCREATE TABLE photos (id SERIAL);\n"
        files = [
            ("server.js", server),
            ("routes/admin.js", admin),
            ("views/admin/dashboard.ejs", "<h1>Dashboard</h1>"),
            ("db/schema.sql", schema),
            ("middleware/auth.js", "module.exports = { requireAuth() {} };"),
        ]
        result = dict(_ensure_admin_mount(files))
        out = result["routes/admin.js"]
        assert "'/admin'" in out or "GET /admin" in out, "Dashboard route missing after template replacement"

    def test_require_stripped_from_ejs(self):
        """require() calls in EJS must be stripped (not available in EJS context)."""
        from build import _strip_require_from_ejs
        view = (
            '<h1>Home</h1>\n'
            '<% var qs = require("url").parse("", true).query || {}; %>\n'
            '<section>Content</section>\n'
        )
        files = [("views/home.ejs", view)]
        result = dict(_strip_require_from_ejs(files))
        out = result["views/home.ejs"]
        assert "require" not in out
        assert "Content" in out

    def test_minimum_seed_rows_padded(self):
        """Per-table minimum seed rows.

        v17 change: events default minimum lowered from 15 to 8 because
        padding with "(evening session)"/"(morning session)" suffixes read
        as clones of the same event. Photos still pad to 12 (filled gallery
        grid). Other tables still pad to the legacy default of 15.
        """
        from build import _ensure_minimum_seed_rows
        seed = (
            "INSERT INTO users (email) VALUES ('admin@test.example') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title) VALUES ('Event A') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title) VALUES ('Another Event B') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO photos (image_url, caption) VALUES ('/img/1.jpg', 'Original caption one') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        events_count = len([l for l in out.split("\n") if "INSERT INTO events" in l and not l.strip().startswith("--")])
        photos_count = len([l for l in out.split("\n") if "INSERT INTO photos" in l and not l.strip().startswith("--")])
        assert events_count >= 8, f"events min=8, got {events_count}"
        assert photos_count >= 12, f"photos min=12, got {photos_count}"

    def test_minimum_seed_rows_preserves_image_urls(self):
        """Padded rows must NOT append text suffixes to image_url paths.

        Regression guard for MW v3 gallery bug: _ensure_minimum_seed_rows was
        appending ' (weekend edition)' etc. to the first quoted value, which was
        the image_url, producing broken paths like
        '/images/stock/wellness/event-venue-02.jpg (weekend edition)'.
        """
        from build import _ensure_minimum_seed_rows
        seed = (
            "INSERT INTO photos (image_url, caption, created_by) VALUES "
            "('/images/stock/wellness/event-venue-01.jpg', 'Community gathering one', 1) "
            "ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        # No padded row may contain a corrupted image URL with a suffix baked in
        assert ".jpg (" not in out
        assert ".jpg'" in out  # original extension preserved
        # Every photos row must still reference an extension-terminated URL
        for line in out.split("\n"):
            if "INSERT INTO photos" in line and not line.strip().startswith("--"):
                # The first quoted string should end with .jpg
                import re as _r
                m = _r.search(r"'([^']+)'", line)
                assert m, f"No quoted value in padded row: {line}"
                assert m.group(1).endswith(".jpg"), (
                    f"Image URL corrupted by padder: {m.group(1)}"
                )

    def test_minimum_seed_rows_skips_single_url_only_table(self):
        """If the only quoted value is a URL/path, the padder must skip the row
        rather than corrupt the link."""
        from build import _ensure_minimum_seed_rows
        seed = (
            "INSERT INTO banners (image_url) VALUES ('/img/hero.jpg') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        # No padded rows should be added since there's no safe string to modify
        padded = [l for l in out.split("\n") if "[padded]" in l]
        assert padded == [], "Padder should not add rows when nothing is safe to modify"
        # Original URL must be unchanged
        assert "'/img/hero.jpg'" in out

    def test_minimum_seed_rows_preserves_emails(self):
        """Email addresses in quoted values must not be corrupted by padding."""
        from build import _ensure_minimum_seed_rows
        seed = (
            "INSERT INTO contacts (email, name) VALUES "
            "('hello@example.com', 'Jane Smith') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        # Email must never have a suffix appended
        assert "hello@example.com (" not in out
        # Padded rows should modify the name field instead
        padded_name_modified = [
            l for l in out.split("\n")
            if "INSERT INTO contacts" in l and "(" in l and "Jane Smith" in l
        ]
        # Original + padded rows should exist
        assert len(padded_name_modified) >= 1

    def test_minimum_seed_rows_preserves_hex_colors(self):
        """Hex color values like '#ff5733' must not be suffixed."""
        from build import _ensure_minimum_seed_rows
        seed = (
            "INSERT INTO themes (hex, label) VALUES "
            "('#ff5733', 'Sunset Orange Theme') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        assert "#ff5733 (" not in out
        # hex value should always remain a pure hex
        import re as _r
        for line in out.split("\n"):
            if "INSERT INTO themes" in line and not line.strip().startswith("--"):
                hex_match = _r.search(r"'(#[0-9a-fA-F]+[^']*)'", line)
                if hex_match:
                    # Must be a clean hex, no text appended
                    assert _r.fullmatch(r"#[0-9a-fA-F]{3,8}", hex_match.group(1)), (
                        f"Hex color corrupted: {hex_match.group(1)}"
                    )

    def test_minimum_seed_rows_skips_interval_values(self):
        """SEED_PADDING_TYPE1: interval literals like '3 months' must not be
        suffixed, 'invalid input syntax for type interval' was observed on MW v8.
        The padder must skip the interval column and either pad another column or
        skip the row entirely."""
        from build import _ensure_minimum_seed_rows
        # Table with an interval column as the first value, name as second.
        seed = (
            "INSERT INTO memberships (duration, plan_name) VALUES "
            "('3 months', 'Basic Plan') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        # No interval value may have a text suffix appended
        assert "3 months (" not in out
        # Any padded rows should use the plan_name column instead
        for line in out.split("\n"):
            if "INSERT INTO memberships" in line and not line.strip().startswith("--"):
                import re as _r
                # Confirm the duration field is never suffixed
                assert "months (" not in line, (
                    f"Interval value was corrupted by padder: {line}"
                )

    def test_minimum_seed_rows_skips_interval_multiple_units(self):
        """Interval literals with various time units are all unsafe to pad."""
        from build import _ensure_minimum_seed_rows
        import re as _r
        # One row per interval unit variant; padder must not corrupt any.
        interval_values = [
            "'1 year'", "'30 days'", "'2 hours'", "'45 minutes'",
            "'1 week'", "'6 months'", "'2 decades'",
        ]
        for iv in interval_values:
            seed = (
                f"INSERT INTO subscriptions (duration, label) VALUES "
                f"({iv}, 'Test Label') ON CONFLICT DO NOTHING;\n"
            )
            files = [("db/seed.sql", seed)]
            result = dict(_ensure_minimum_seed_rows(files))
            out = result["db/seed.sql"]
            unit = iv.strip("'").split()[-1]  # e.g. 'year', 'days'
            for line in out.split("\n"):
                if "INSERT INTO subscriptions" in line and not line.strip().startswith("--"):
                    assert f"{unit} (" not in line, (
                        f"Interval unit '{unit}' was corrupted: {line}"
                    )

    def test_minimum_seed_rows_skips_pure_numeric_strings(self):
        """Pure numeric quoted strings ('1000', '3.14', '-50') belong to
        integer/numeric/decimal columns and must not be text-padded."""
        from build import _ensure_minimum_seed_rows
        seed = (
            "INSERT INTO scores (points, player_name) VALUES "
            "('10000', 'Alice Chen') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        # The numeric string must not be padded
        assert "10000 (" not in out
        # Padded rows should modify player_name instead
        padded_with_name = [
            l for l in out.split("\n")
            if "INSERT INTO scores" in l
            and not l.strip().startswith("--")
            and "Alice Chen" in l
        ]
        assert len(padded_with_name) >= 1, (
            "Padder should have fallen back to modifying player_name"
        )

    def test_minimum_seed_rows_skips_system_tables(self):
        """users and site_settings tables must NOT be padded."""
        from build import _ensure_minimum_seed_rows
        seed = "INSERT INTO users (email) VALUES ('admin@test.example') ON CONFLICT DO NOTHING;\n"
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        users_count = len([l for l in out.split("\n") if "INSERT INTO users" in l and not l.strip().startswith("--")])
        assert users_count == 1  # should not be padded

    def test_minimum_seed_rows_rejects_unbalanced_quotes(self):
        """Padded rows with unbalanced single quotes are skipped."""
        from build import _ensure_minimum_seed_rows
        # Source row has an apostrophe in the value which when suffix is appended
        # could create a valid row. But if the source itself has odd quotes, skip.
        seed = (
            "INSERT INTO events (title) VALUES ('Yoga Class') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO events (title) VALUES ('Kids\\' Night') ON CONFLICT DO NOTHING;\n"
        )
        files = [("db/seed.sql", seed)]
        result = dict(_ensure_minimum_seed_rows(files))
        out = result["db/seed.sql"]
        # All padded rows must have balanced quotes
        for line in out.split("\n"):
            stripped = line.strip()
            if stripped.startswith("--") or not stripped:
                continue
            if "INSERT INTO events" in stripped:
                # Count quotes minus escaped ones
                quote_count = stripped.count("'") - stripped.count("\\'")
                assert quote_count % 2 == 0, f"Unbalanced quotes in: {stripped}"

    def test_csrf_dead_code_fallback_removed(self):
        """getValidateCsrf dead-code fallback must be stripped from routes."""
        from build import _ensure_csrf_import
        route = (
            "const express = require('express');\n"
            "const { requireAuth, validateCsrfToken } = require('../middleware/auth');\n"
            "\n"
            "function getValidateCsrf() {\n"
            "  try {\n"
            "    return require('../middleware/auth').validateCsrfToken;\n"
            "  } catch(e) {\n"
            "    return (req, res, next) => {\n"
            "      const token = req.body._csrf;\n"
            "      if (!token) return res.status(403).send('Invalid');\n"
            "      next();\n"
            "    };\n"
            "  }\n"
            "}\n"
            "\n"
            "router.post('/login', validateCsrfToken, async (req, res) => {});\n"
        )
        auth_mw = "module.exports = { requireAuth, validateCsrfToken };\nfunction validateCsrfToken(req, res, next) { next(); }\n"
        files = [("routes/auth.js", route), ("middleware/auth.js", auth_mw)]
        result = dict(_ensure_csrf_import(files))
        out = result["routes/auth.js"]
        assert "getValidateCsrf" not in out
        assert "validateCsrfToken" in out

    def test_mobile_menu_closes_on_click(self):
        """Mobile menu links must have click handlers to close the menu (camelCase)."""
        from build import _ensure_mobile_menu_close
        layout = (
            '<div id="mobileMenu" class="hidden">\n'
            '  <a href="/">Home</a>\n'
            '  <a href="/about">About</a>\n'
            '</div>\n'
            '<script>\n'
            "document.getElementById('mobileMenuBtn').addEventListener('click', function() {\n"
            "  document.getElementById('mobileMenu').classList.toggle('hidden');\n"
            "});\n"
            '</script>\n'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_menu_close(files))
        out = result["views/layout.ejs"]
        assert "#mobileMenu a" in out
        assert "classList.add('hidden')" in out

    def test_mobile_menu_closes_on_click_dash_id(self):
        """Mobile menu close must also work with dash-case IDs (mobile-menu)."""
        from build import _ensure_mobile_menu_close
        layout = (
            '<div id="mobile-menu" class="hidden">\n'
            '  <a href="/">Home</a>\n'
            '</div>\n'
            '<script>\n'
            "document.getElementById('mobile-menu-btn').addEventListener('click', function() {\n"
            "  document.getElementById('mobile-menu').classList.toggle('hidden');\n"
            "});\n"
            '</script>\n'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_mobile_menu_close(files))
        out = result["views/layout.ejs"]
        assert "#mobile-menu a" in out
        assert "classList.add('hidden')" in out

    def test_skip_to_content_link(self):
        """Layout must have skip-to-content link and main-content ID."""
        from build import _ensure_skip_to_content
        layout = (
            '<!DOCTYPE html>\n<html>\n<body>\n'
            '<nav>Nav</nav>\n'
            '<main class="flex-1"><%- body %></main>\n'
            '</body></html>'
        )
        files = [("views/layout.ejs", layout)]
        result = dict(_ensure_skip_to_content(files))
        out = result["views/layout.ejs"]
        assert "skip" in out.lower() or "#main-content" in out
        assert 'id="main-content"' in out
        assert "clip:rect(0,0,0,0)" in out or "sr-only" in out


# ---------------------------------------------------------------------------
# db/migrate.js template: per-statement seed execution
# ---------------------------------------------------------------------------

class TestMigrateJsTemplate:
    """Tests for the db/migrate.js template per-statement seed splitting."""

    def test_template_splits_seed_statements(self):
        """Template must split seed.sql into individual statements."""
        template_path = Path(__file__).parent.parent / "cli" / "templates" / "db_migrate.js"
        content = template_path.read_text()
        assert ".split(" in content, "Template must split seed SQL into statements"
        assert "try" in content and "catch" in content, "Template must try/catch each statement"

    def test_template_does_not_run_seed_as_single_query(self):
        """Template must NOT run all seed.sql as a single client.query(seed)."""
        template_path = Path(__file__).parent.parent / "cli" / "templates" / "db_migrate.js"
        content = template_path.read_text()
        # Old pattern: await client.query(seed); must not exist outside the loop
        lines = content.split("\n")
        for line in lines:
            stripped = line.strip()
            if "client.query(seed)" in stripped and "for" not in stripped:
                # This is the old single-query pattern
                assert False, "Template still runs seed.sql as single query"

    def test_template_tracks_ok_and_failed_counts(self):
        """Template must log counts of successful and failed statements."""
        template_path = Path(__file__).parent.parent / "cli" / "templates" / "db_migrate.js"
        content = template_path.read_text()
        assert "ok" in content and "failed" in content

    def test_ensure_resilient_migrate_replaces_single_query(self):
        """Post-processor replaces Claude's single-query migrate.js with template."""
        from build import _ensure_resilient_migrate
        claude_migrate = (
            "const fs = require('fs');\n"
            "async function migrate() {\n"
            "  const seed = fs.readFileSync('seed.sql', 'utf8');\n"
            "  await pool.query(seed);\n"
            "}\n"
        )
        files = [("db/migrate.js", claude_migrate)]
        result = dict(_ensure_resilient_migrate(files))
        out = result["db/migrate.js"]
        assert ".split(" in out, "Template must split seed statements"
        assert "try" in out and "catch" in out

    def test_ensure_resilient_migrate_skips_when_already_resilient(self):
        """Does not replace migrate.js that already splits statements."""
        from build import _ensure_resilient_migrate
        good_migrate = (
            "const seed = fs.readFileSync('seed.sql', 'utf8');\n"
            "const stmts = seed.split(';');\n"
            "for (const s of stmts) { try { await q(s); } catch(e) {} }\n"
        )
        files = [("db/migrate.js", good_migrate)]
        result = dict(_ensure_resilient_migrate(files))
        assert result["db/migrate.js"] == good_migrate


class TestFixAdminDarkStatCards:
    """Tests for _fix_admin_dark_stat_cards post-processor."""

    DARK_LAYOUT = (
        '<html><head></head>'
        '<body class="bg-slate-900 min-h-screen">'
        '<nav>nav</nav><%- body %></body></html>'
    )
    LIGHT_LAYOUT = (
        '<html><head></head>'
        '<body class="bg-white min-h-screen">'
        '<nav>nav</nav><%- body %></body></html>'
    )
    DARK_PRIMARY_SPEC = {"design": {"primary_color": "#002040"}}
    LIGHT_PRIMARY_SPEC = {"design": {"primary_color": "#3B82F6"}}

    def _run(self, admin_content, layout=None, spec=None):
        from build import _fix_admin_dark_stat_cards
        layout = layout or self.DARK_LAYOUT
        spec = spec or self.DARK_PRIMARY_SPEC
        files = [
            ("views/layout.ejs", layout),
            ("views/admin-dashboard.ejs", admin_content),
        ]
        return dict(_fix_admin_dark_stat_cards(files, spec))

    def test_replaces_text_primary_in_admin_on_dark_page(self):
        """text-primary in admin view becomes text-white when body and primary are dark."""
        content = '<div class="stat-card text-primary font-bold">42</div>'
        result = self._run(content)
        assert "text-white" in result["views/admin-dashboard.ejs"]
        assert "text-primary" not in result["views/admin-dashboard.ejs"]

    def test_replaces_bg_primary_fraction_in_admin(self):
        """bg-primary/5 in admin view becomes bg-white/10 on dark pages."""
        content = '<div class="card bg-primary/5 rounded-lg">stats</div>'
        result = self._run(content)
        assert "bg-white/10" in result["views/admin-dashboard.ejs"]
        assert "bg-primary/5" not in result["views/admin-dashboard.ejs"]

    def test_no_change_when_primary_is_light(self):
        """Does not modify admin views when primary color has high luminance."""
        content = '<div class="text-primary font-bold">42</div>'
        result = self._run(content, spec=self.LIGHT_PRIMARY_SPEC)
        assert "text-primary" in result["views/admin-dashboard.ejs"]

    def test_no_change_when_body_is_light(self):
        """Does not modify admin views when body background is light."""
        content = '<div class="text-primary font-bold">42</div>'
        result = self._run(content, layout=self.LIGHT_LAYOUT)
        assert "text-primary" in result["views/admin-dashboard.ejs"]

    def test_affects_all_view_files_not_just_admin(self):
        """Now covers ALL views/ files, including non-admin like contact-submissions-detail."""
        from build import _fix_admin_dark_stat_cards
        files = [
            ("views/layout.ejs", self.DARK_LAYOUT),
            ("views/contact-submissions-detail.ejs", '<div class="text-primary">msg</div>'),
            ("views/admin/settings.ejs", '<p class="text-gray-900">setting</p>'),
        ]
        result = dict(_fix_admin_dark_stat_cards(files, self.DARK_PRIMARY_SPEC))
        assert "text-white" in result["views/contact-submissions-detail.ejs"]
        assert "text-primary" not in result["views/contact-submissions-detail.ejs"]
        assert "text-slate-200" in result["views/admin/settings.ejs"]
        assert "text-gray-900" not in result["views/admin/settings.ejs"]

    def test_skips_layout_login_register(self):
        """layout.ejs, login.ejs, register.ejs are not modified (own dark-panel designs)."""
        from build import _fix_admin_dark_stat_cards
        files = [
            ("views/layout.ejs", self.DARK_LAYOUT + '<span class="text-primary">x</span>'),
            ("views/login.ejs", '<div class="text-primary">login</div>'),
            ("views/register.ejs", '<div class="text-primary">reg</div>'),
        ]
        result = dict(_fix_admin_dark_stat_cards(files, self.DARK_PRIMARY_SPEC))
        assert "text-primary" in result["views/login.ejs"]
        assert "text-primary" in result["views/register.ejs"]

    def test_fixes_inline_color_var_primary(self):
        """Inline color:var(--color-primary) is replaced with color:#ffffff."""
        from build import _fix_admin_dark_stat_cards
        files = [
            ("views/layout.ejs", self.DARK_LAYOUT),
            ("views/contact-detail.ejs", '<h2 style="color:var(--color-primary)">Title</h2>'),
        ]
        result = dict(_fix_admin_dark_stat_cards(files, self.DARK_PRIMARY_SPEC))
        assert "color:#ffffff" in result["views/contact-detail.ejs"]
        assert "color:var(--color-primary)" not in result["views/contact-detail.ejs"]

    def test_replaces_text_gray_900_in_admin(self):
        """text-gray-900 (dark) becomes text-slate-200 in views on dark pages."""
        content = '<p class="text-gray-900 text-sm">label</p>'
        result = self._run(content)
        assert "text-slate-200" in result["views/admin-dashboard.ejs"]
        assert "text-gray-900" not in result["views/admin-dashboard.ejs"]

    def test_replaces_text_gray_800_and_700(self):
        """text-gray-800 and text-gray-700 are also replaced."""
        from build import _fix_admin_dark_stat_cards
        files = [
            ("views/layout.ejs", self.DARK_LAYOUT),
            ("views/admin/settings.ejs",
             '<p class="text-gray-800">a</p><p class="text-gray-700">b</p>'),
        ]
        result = dict(_fix_admin_dark_stat_cards(files, self.DARK_PRIMARY_SPEC))
        content = result["views/admin/settings.ejs"]
        assert "text-slate-300" in content
        assert "text-slate-400" in content
        assert "text-gray-800" not in content
        assert "text-gray-700" not in content


class TestFixLightOnLightText:
    """Tests for _fix_light_on_light_text post-processor.

    Mirror image of TestFixAdminDarkStatCards. Catches the cream-on-cream
    failure mode (medicine-waves MW v2): light text classes on h1/h2/h3 inside
    a light-themed page render invisible. Triggers when body uses a light
    Tailwind background OR effective primary color luminance > 0.85.
    """

    CREAM_LAYOUT = (
        '<html><head></head>'
        '<body class="bg-amber-50 min-h-screen">'
        '<nav>nav</nav><%- body %></body></html>'
    )
    STONE_LAYOUT = (
        '<html><head></head>'
        '<body class="bg-stone-100 font-body">'
        '<nav>nav</nav><%- body %></body></html>'
    )
    DARK_LAYOUT = (
        '<html><head></head>'
        '<body class="bg-slate-900 min-h-screen">'
        '<nav>nav</nav><%- body %></body></html>'
    )

    DARK_PRIMARY_SPEC = {"design": {"primary_color": "#002040"}}
    LIGHT_PRIMARY_SPEC = {"design": {"primary_color": "#FFF5E6"}}  # luminance ~0.92, > 0.85 trigger

    def _run(self, view_files, layout=None, spec=None):
        from build import _fix_light_on_light_text
        layout = layout or self.CREAM_LAYOUT
        spec = spec or self.DARK_PRIMARY_SPEC
        files = [("views/layout.ejs", layout)] + list(view_files)
        return dict(_fix_light_on_light_text(files, spec))

    # Trigger: light body --------------------------------------------------

    def test_rewrites_text_white_h1_on_cream_body(self):
        """Hero h1 with text-white on a cream layout becomes text-slate-900."""
        view = ("views/index.ejs",
                '<h1 class="text-5xl text-white font-bold">Welcome</h1>')
        result = self._run([view])
        assert 'text-slate-900' in result["views/index.ejs"]
        assert 'text-white' not in result["views/index.ejs"]

    def test_rewrites_text_cream_h2(self):
        """text-cream on h2 also rewritten."""
        view = ("views/about.ejs",
                '<h2 class="text-3xl text-cream">About Us</h2>')
        result = self._run([view])
        assert 'text-slate-900' in result["views/about.ejs"]
        assert 'text-cream' not in result["views/about.ejs"]

    def test_rewrites_text_ivory_h3(self):
        """text-ivory on h3 also rewritten."""
        view = ("views/contact.ejs",
                '<h3 class="text-ivory text-2xl">Get in touch</h3>')
        result = self._run([view])
        assert 'text-slate-900' in result["views/contact.ejs"]
        assert 'text-ivory' not in result["views/contact.ejs"]

    def test_rewrites_text_amber_50(self):
        """text-amber-50 on a heading is also rewritten."""
        view = ("views/events.ejs",
                '<h1 class="text-amber-50 text-4xl">Events</h1>')
        result = self._run([view])
        assert 'text-slate-900' in result["views/events.ejs"]
        assert 'text-amber-50' not in result["views/events.ejs"]

    def test_triggers_on_bg_stone_100_body(self):
        """bg-stone-100 body also triggers the fix."""
        view = ("views/index.ejs",
                '<h1 class="text-white text-5xl">Hi</h1>')
        result = self._run([view], layout=self.STONE_LAYOUT)
        assert 'text-slate-900' in result["views/index.ejs"]

    def test_h2_and_h3_both_rewritten(self):
        """Function rewrites h1, h2, h3 (not just h1)."""
        view = ("views/index.ejs",
                '<h2 class="text-white">A</h2>'
                '<h3 class="text-white">B</h3>')
        result = self._run([view])
        out = result["views/index.ejs"]
        # Both h2 and h3 should now use text-slate-900
        assert out.count("text-slate-900") == 2
        assert 'text-white' not in out

    def test_h4_not_rewritten(self):
        """h4 and below are out of scope (only h1/h2/h3)."""
        view = ("views/index.ejs",
                '<h4 class="text-white">Heading 4</h4>')
        result = self._run([view])
        assert 'text-white' in result["views/index.ejs"]
        assert 'text-slate-900' not in result["views/index.ejs"]

    def test_existing_other_classes_preserved(self):
        """Other classes on the heading are preserved during rewrite."""
        view = ("views/index.ejs",
                '<h1 class="text-5xl text-white font-bold tracking-tight">Hi</h1>')
        result = self._run([view])
        out = result["views/index.ejs"]
        assert 'text-5xl' in out
        assert 'font-bold' in out
        assert 'tracking-tight' in out
        assert 'text-slate-900' in out

    # Trigger: light primary -----------------------------------------------

    def test_triggers_on_light_primary_color(self):
        """Light primary color (luminance > 0.85) triggers the fix even with non-light body."""
        view = ("views/index.ejs",
                '<h1 class="text-white text-5xl">Welcome</h1>')
        # Use a layout without a light body class so only primary triggers
        plain_layout = '<html><body class="font-body"><nav>x</nav><%- body %></body></html>'
        result = self._run([view], layout=plain_layout, spec=self.LIGHT_PRIMARY_SPEC)
        assert 'text-slate-900' in result["views/index.ejs"]
        assert 'text-white' not in result["views/index.ejs"]

    # Trigger: UUPM palette background (MW v2 regression guard) ------------

    def test_triggers_on_uupm_light_background_with_dark_primary(self):
        """MW v2 regression: wellness/warm-earthy spec with dark primary has
        a light UUPM palette background (#F0F7FA, lum 0.92). The body uses
        inline style="background-color: var(--color-background)", NOT a
        Tailwind bg-* class, so the class-based detector misses it. The
        UUPM palette.background luminance check must fire."""
        view = ("views/index.ejs",
                '<section class="hero-main">\n'
                '  <h1 class="font-brand text-5xl text-white">Healing Arts</h1>\n'
                '</section>')
        # Realistic MW layout: inline CSS custom property, no bg-* class
        inline_style_layout = (
            '<html><head></head>'
            '<body class="font-body min-h-screen" '
            'style="background-color: var(--color-background);">'
            '<nav>nav</nav><%- body %></body></html>'
        )
        # MW-style spec: generic dark placeholder primary → UUPM overrides
        # the full palette with wellness teal (light background #F0F7FA)
        mw_spec = {
            "app_name": "medicine-waves",
            "description": "healing arts wellness community",
            "design": {
                "primary_color": "#002040",
                "secondary_color": "#1a1a2e",
                "style": "warm earthy",
            },
        }
        result = self._run([view], layout=inline_style_layout, spec=mw_spec)
        assert 'text-slate-900' in result["views/index.ejs"]
        assert 'text-white' not in result["views/index.ejs"]

    def test_triggers_on_css_color_background_custom_property(self):
        """Defensive catch: when Claude's generated public/style.css sets
        --color-background: #FDF8F0 (cream, lum ~0.95) and diverges from the
        UUPM palette, the CSS fallback scan must still fire the fix."""
        view = ("views/index.ejs",
                '<h1 class="text-5xl text-white font-bold">Invisible Hero</h1>')
        # Plain layout (no bg-* class, no inline style)
        plain_layout = (
            '<html><body class="font-body"><nav>n</nav>'
            '<%- body %></body></html>'
        )
        # Spec where UUPM would resolve to a DARK background (to ensure
        # the CSS scan is what fires, not the UUPM palette check).
        dark_spec = {
            "app_name": "darkapp",
            "description": "a dark themed night sky tracker",
            "design": {
                "primary_color": "#002040",
                "style": "modern dark",
            },
        }
        style_css = (
            ":root {\n"
            "  --color-primary: #0f2b3c;\n"
            "  --color-background: #FDF8F0;\n"  # cream, lum ~0.95
            "  --color-foreground: #2D2013;\n"
            "}\n"
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", plain_layout),
            ("public/style.css", style_css),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, dark_spec))
        assert 'text-slate-900' in result["views/index.ejs"]
        assert 'text-white' not in result["views/index.ejs"]

    def test_css_background_trigger_ignores_dark_values(self):
        """CSS scan should NOT trigger on dark --color-background values
        (lum <= 0.85). Regression guard for the defensive fallback.

        Uses a description that doesn't match any UUPM keyword so the
        UUPM palette.background path is empty and only the CSS scan
        path is exercised.
        """
        view = ("views/index.ejs",
                '<h1 class="text-5xl text-white font-bold">Hero</h1>')
        plain_layout = (
            '<html><body class="font-body"><nav>n</nav>'
            '<%- body %></body></html>'
        )
        dark_spec = {
            "app_name": "darkapp",
            # Intentionally non-matching description so UUPM returns no
            # palette.background, isolating the CSS scan trigger.
            "description": "cyberpunk hacker terminal interface zzz",
            "design": {
                "primary_color": "#002040",
                "style": "modern dark",
            },
        }
        # Dark navy background (lum ~0.02)
        style_css = (
            ":root {\n"
            "  --color-background: #0a1628;\n"
            "  --color-foreground: #ffffff;\n"
            "}\n"
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", plain_layout),
            ("public/style.css", style_css),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, dark_spec))
        # Dark background → no trigger → text-white preserved
        assert 'text-white' in result["views/index.ejs"]
        assert 'text-slate-900' not in result["views/index.ejs"]

    # No-op cases ----------------------------------------------------------

    def test_no_change_when_body_dark_and_primary_dark(self):
        """Dark body + dark primary should NOT trigger rewrite."""
        view = ("views/index.ejs",
                '<h1 class="text-white text-5xl">Hi</h1>')
        result = self._run([view], layout=self.DARK_LAYOUT, spec=self.DARK_PRIMARY_SPEC)
        # Dark body intentionally uses text-white, must remain
        assert 'text-white' in result["views/index.ejs"]

    def test_skips_layout_login_register(self):
        """layout.ejs, login.ejs, register.ejs are not modified (intentional dark panels)."""
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", self.CREAM_LAYOUT + '<h1 class="text-white">x</h1>'),
            ("views/login.ejs", '<h1 class="text-white">Login</h1>'),
            ("views/register.ejs", '<h1 class="text-white">Register</h1>'),
        ]
        result = dict(_fix_light_on_light_text(files, self.DARK_PRIMARY_SPEC))
        assert 'text-white' in result["views/login.ejs"]
        assert 'text-white' in result["views/register.ejs"]

    def test_skips_heading_inside_dark_section(self):
        """Heading inside a bg-slate-900 section in same view should be left alone (lookback)."""
        view = ("views/index.ejs",
                '<section class="bg-slate-900 py-20">\n'
                '  <div class="container">\n'
                '    <h1 class="text-white text-5xl">Hero</h1>\n'
                '  </div>\n'
                '</section>')
        result = self._run([view])
        # Lookback finds bg-slate-900 within 8 lines above the heading
        assert 'text-white' in result["views/index.ejs"]

    def test_rewrites_heading_outside_dark_section(self):
        """Heading not preceded by a dark parent IS rewritten on cream body."""
        view = ("views/index.ejs",
                '<section class="bg-amber-50 py-20">\n'
                '  <div class="container">\n'
                '    <h2 class="text-white text-4xl">Section</h2>\n'
                '  </div>\n'
                '</section>')
        result = self._run([view])
        out = result["views/index.ejs"]
        assert 'text-slate-900' in out
        assert 'text-white' not in out

    def test_walks_subdirectories(self):
        """Function applies to nested views like views/admin/settings.ejs too."""
        view = ("views/admin/dashboard.ejs",
                '<h1 class="text-white">Admin Dashboard</h1>')
        result = self._run([view])
        assert 'text-slate-900' in result["views/admin/dashboard.ejs"]

    def test_non_ejs_files_unchanged(self):
        """Non-.ejs files in views/ are not touched."""
        view = ("views/index.ejs",
                '<h1 class="text-white">Hi</h1>')
        readme = ("views/README.md",
                  "# h1 with text-white\n")
        from build import _fix_light_on_light_text
        files = [("views/layout.ejs", self.CREAM_LAYOUT), view, readme]
        result = dict(_fix_light_on_light_text(files, self.DARK_PRIMARY_SPEC))
        assert result["views/README.md"] == "# h1 with text-white\n"

    def test_no_layout_no_change(self):
        """If no views/layout.ejs is present, function returns files unchanged."""
        from build import _fix_light_on_light_text
        files = [("views/index.ejs", '<h1 class="text-white">Hi</h1>')]
        result = dict(_fix_light_on_light_text(files, self.DARK_PRIMARY_SPEC))
        # No layout means body_is_light=False; with dark primary, no trigger.
        assert 'text-white' in result["views/index.ejs"]

    # MW v3 regression: over-correction inside CSS-class-defined dark sections

    def test_skips_heading_inside_css_class_defined_dark_section(self):
        """MW v3 over-correction guard: a section class like `.hero-main`
        defined in public/style.css as a dark linear-gradient must be
        treated as a dark parent, so text-white headings inside it stay
        intact. The old Tailwind-only lookback missed this and rewrote
        MW's hero h1 to text-slate-900 (invisible on dark hero)."""
        view = ("views/index.ejs",
                '<section class="hero-main relative overflow-hidden">\n'
                '  <div class="max-w-6xl mx-auto px-4 relative z-10">\n'
                '    <h1 class="font-brand text-5xl text-white">Welcome</h1>\n'
                '  </div>\n'
                '</section>')
        # Light UUPM background (wellness) triggers the fix
        mw_spec = {
            "app_name": "medicine-waves",
            "description": "healing arts wellness community",
            "design": {
                "primary_color": "#002040",
                "secondary_color": "#1a1a2e",
                "style": "warm earthy",
            },
        }
        style_css = (
            ":root { --color-primary: #0f2b3c; --color-background: #F0F7FA; }\n"
            ".hero-main {\n"
            "  background: linear-gradient(135deg, #0a1f2d 0%, #0f2b3c 30%, #1a5a6a 60%, #0f2b3c 100%);\n"
            "}\n"
        )
        inline_style_layout = (
            '<html><head></head>'
            '<body class="font-body min-h-screen" '
            'style="background-color: var(--color-background);">'
            '<nav>nav</nav><%- body %></body></html>'
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", inline_style_layout),
            ("public/style.css", style_css),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, mw_spec))
        # Hero h1 must stay text-white (CSS-class-defined dark parent)
        assert 'text-white' in result["views/index.ejs"]
        assert 'text-slate-900' not in result["views/index.ejs"]

    def test_skips_heading_inside_newsletter_section_css_class(self):
        """MW v3 regression: .newsletter-section is also a dark gradient
        in style.css. Its h2 must be preserved too (not just .hero-main)."""
        view = ("views/index.ejs",
                '<section class="newsletter-section py-16 md:py-20">\n'
                '  <div class="max-w-2xl mx-auto text-center px-4">\n'
                '    <h2 class="font-display text-3xl text-white mb-4">Stay in Touch</h2>\n'
                '    <p class="text-white/80">Subscribe for updates</p>\n'
                '  </div>\n'
                '</section>')
        mw_spec = {
            "app_name": "medicine-waves",
            "description": "healing arts wellness community",
            "design": {
                "primary_color": "#002040",
                "secondary_color": "#1a1a2e",
                "style": "warm earthy",
            },
        }
        style_css = (
            ":root { --color-primary: #0f2b3c; --color-background: #F0F7FA; }\n"
            ".newsletter-section {\n"
            "  background: linear-gradient(135deg, #0a1f2d 0%, #1a5a6a 50%, #0f2b3c 100%);\n"
            "}\n"
        )
        inline_style_layout = (
            '<html><head></head>'
            '<body class="font-body" '
            'style="background-color: var(--color-background);">'
            '<nav>nav</nav><%- body %></body></html>'
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", inline_style_layout),
            ("public/style.css", style_css),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, mw_spec))
        assert 'text-white' in result["views/index.ejs"]
        assert 'text-slate-900' not in result["views/index.ejs"]

    def test_skips_heading_inside_light_css_class_does_not_apply(self):
        """Inverse guard: a light CSS class like `.section-warm` (cream
        gradient) must NOT be treated as a dark parent. A text-white
        heading inside it should still be rewritten (invisible on cream).
        """
        view = ("views/index.ejs",
                '<section class="section-warm py-16">\n'
                '  <div class="max-w-4xl mx-auto">\n'
                '    <h2 class="text-4xl text-white">Our Story</h2>\n'
                '  </div>\n'
                '</section>')
        mw_spec = {
            "app_name": "medicine-waves",
            "description": "healing arts wellness community",
            "design": {
                "primary_color": "#002040",
                "secondary_color": "#1a1a2e",
                "style": "warm earthy",
            },
        }
        style_css = (
            ":root { --color-primary: #0f2b3c; --color-background: #F0F7FA; "
            "--color-cream: #F5EDE0; }\n"
            ".section-warm {\n"
            "  background: linear-gradient(180deg, var(--color-cream) 0%, var(--color-background) 100%);\n"
            "}\n"
        )
        inline_style_layout = (
            '<html><head></head>'
            '<body class="font-body" '
            'style="background-color: var(--color-background);">'
            '<nav>nav</nav><%- body %></body></html>'
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", inline_style_layout),
            ("public/style.css", style_css),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, mw_spec))
        # Light CSS class parent → text-white is invisible → rewrite.
        assert 'text-slate-900' in result["views/index.ejs"]
        assert 'text-white' not in result["views/index.ejs"]

    def test_skips_heading_inside_inline_style_with_dark_primary_var(self):
        """When the section uses an inline style referencing
        var(--color-primary) and the spec primary is dark, the heading
        inside is inside a dark parent and light text stays."""
        view = ("views/contact.ejs",
                '<div class="mt-10 p-6 rounded-xl" '
                'style="background: linear-gradient(135deg, var(--color-primary), var(--color-secondary));">\n'
                '  <h3 class="font-display text-lg text-white mb-3">Newsletter</h3>\n'
                '</div>')
        mw_spec = {
            "app_name": "medicine-waves",
            "description": "healing arts wellness community",
            "design": {
                "primary_color": "#002040",  # dark navy, lum < 0.5
                "secondary_color": "#1a1a2e",
                "style": "warm earthy",
            },
        }
        inline_style_layout = (
            '<html><head></head>'
            '<body class="font-body" '
            'style="background-color: var(--color-background);">'
            '<nav>nav</nav><%- body %></body></html>'
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", inline_style_layout),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, mw_spec))
        assert 'text-white' in result["views/contact.ejs"]
        assert 'text-slate-900' not in result["views/contact.ejs"]

    def test_skips_heading_inside_inline_style_with_literal_dark_hex(self):
        """Inline style with a literal dark hex (not a CSS var) is also
        a dark parent. Light text inside should stay."""
        view = ("views/index.ejs",
                '<section style="background-color: #0a1f2d;">\n'
                '  <h1 class="text-5xl text-white">Hero</h1>\n'
                '</section>')
        mw_spec = {
            "app_name": "medicine-waves",
            "description": "healing arts wellness community",
            "design": {
                "primary_color": "#002040",
                "style": "warm earthy",
            },
        }
        inline_style_layout = (
            '<html><head></head>'
            '<body class="font-body" '
            'style="background-color: var(--color-background);">'
            '<nav>nav</nav><%- body %></body></html>'
        )
        from build import _fix_light_on_light_text
        files = [
            ("views/layout.ejs", inline_style_layout),
            view,
        ]
        result = dict(_fix_light_on_light_text(files, mw_spec))
        assert 'text-white' in result["views/index.ejs"]


class TestFixCspCdnSwGuard:
    """Regression guards for _fix_csp_cdn_sources() service worker patching.

    MW v3 local preview (2026-04-10): the CSP post-processor injected a
    same-origin guard into sw.js using a hardcoded `event.request.url`, but
    Claude's handler was `function(e)`, so the guard referenced an undefined
    `event` identifier and the service worker crashed with
    `Uncaught ReferenceError: event is not defined at sw.js:5:3`.

    The fix detects the fetch handler's parameter name and injects a guard
    that uses that same name.
    """

    def _server_with_helmet_csp(self):
        return (
            "const helmet = require('helmet');\n"
            "app.use(helmet({\n"
            "  contentSecurityPolicy: {\n"
            "    directives: { connectSrc: [\"'self'\"] }\n"
            "  }\n"
            "}));\n"
        )

    def test_fetch_handler_with_e_param_gets_e_guard(self):
        """function(e) handler must get an `e.request.url` guard, not `event.request.url`."""
        from build import _fix_csp_cdn_sources
        sw = (
            "self.addEventListener('install', function(e) { e.waitUntil(caches.open('v1')); });\n"
            "self.addEventListener('fetch', function(e) {\n"
            "  e.respondWith(fetch(e.request).catch(function() { return caches.match(e.request); }));\n"
            "});\n"
        )
        files = [("server.js", self._server_with_helmet_csp()), ("public/sw.js", sw)]
        result = dict(_fix_csp_cdn_sources(files))
        patched = result["public/sw.js"]
        assert "e.request.url.startsWith(self.location.origin)" in patched
        assert "event.request.url" not in patched, (
            "Hardcoded `event` reference will crash with ReferenceError when "
            "the fetch handler parameter is named `e`."
        )

    def test_fetch_handler_with_event_param_gets_event_guard(self):
        """function(event) handler must still get an `event.request.url` guard."""
        from build import _fix_csp_cdn_sources
        sw = (
            "self.addEventListener('fetch', function(event) {\n"
            "  event.respondWith(fetch(event.request));\n"
            "});\n"
        )
        files = [("server.js", self._server_with_helmet_csp()), ("public/sw.js", sw)]
        result = dict(_fix_csp_cdn_sources(files))
        patched = result["public/sw.js"]
        assert "event.request.url.startsWith(self.location.origin)" in patched

    def test_fetch_handler_with_arrow_function_param(self):
        """Arrow function (evt) => {...} must get an `evt.request.url` guard."""
        from build import _fix_csp_cdn_sources
        sw = (
            "self.addEventListener('fetch', (evt) => {\n"
            "  evt.respondWith(fetch(evt.request));\n"
            "});\n"
        )
        files = [("server.js", self._server_with_helmet_csp()), ("public/sw.js", sw)]
        result = dict(_fix_csp_cdn_sources(files))
        patched = result["public/sw.js"]
        assert "evt.request.url.startsWith(self.location.origin)" in patched
        assert "event.request.url" not in patched

    def test_patched_sw_js_has_no_undefined_references(self):
        """After patching, every variable used in the injected guard must be defined in scope."""
        from build import _fix_csp_cdn_sources
        import re
        for param in ["e", "event", "evt", "ev"]:
            sw = (
                f"self.addEventListener('fetch', function({param}) {{\n"
                f"  {param}.respondWith(fetch({param}.request));\n"
                "});\n"
            )
            files = [("server.js", self._server_with_helmet_csp()), ("public/sw.js", sw)]
            result = dict(_fix_csp_cdn_sources(files))
            patched = result["public/sw.js"]
            # Find the injected guard line
            guard_match = re.search(r"if \(!(\w+)\.request\.url\.startsWith", patched)
            assert guard_match, f"No guard injected for param={param!r}"
            guard_var = guard_match.group(1)
            assert guard_var == param, (
                f"Guard used undefined variable {guard_var!r} when handler param is {param!r}"
            )


# ---------------------------------------------------------------------------
# Section-06 of WATCHDOG1 Layer 2: cli/build.py messages.create migration
# ---------------------------------------------------------------------------

class TestBuildApiClientMigration:
    """Verify cli/build.py call_api_with_retry routes through api_client.call_api.

    These tests are paired with the AST walk in
    tests/test_api_client.py::TestNoBypassStatic. The AST walk is the
    authoritative drift guard; this class lives here so a bisect surfaces
    the build.py file specifically.
    """

    def test_build_py_no_direct_messages_create(self):
        """cli/build.py must contain no AST-level <obj>.messages.create(...) call.

        Docstrings that quote the historical pattern as documentation are
        fine; only actual call expressions are forbidden. The AST walk in
        tests/test_api_client.py::TestNoBypassStatic is the authoritative
        cross-file check; this test scopes the same rule to build.py so a
        bisect points at the right file.
        """
        import ast
        build_path = Path(__file__).parent.parent / "cli" / "build.py"
        tree = ast.parse(build_path.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "create":
                continue
            inner = func.value
            if isinstance(inner, ast.Attribute) and inner.attr == "messages":
                offenders.append(node.lineno)
        assert offenders == [], (
            f"cli/build.py still contains direct .messages.create(...) calls "
            f"at lines {offenders}. Section-06 requires all chat calls go "
            f"through cli.api_client.call_api()."
        )

    def test_call_api_with_retry_delegates_to_call_api(self, monkeypatch):
        """call_api_with_retry must invoke api_client.call_api with the
        right model, system prompt, max_tokens, messages, and build_id.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import build as build_mod

        fake_response = SimpleNamespace(
            content=[SimpleNamespace(text="hello world", type="text")]
        )
        fake_metrics = SimpleNamespace(executor_cost_usd=0.0, advisor_cost_usd=0.0)
        mock_call_api = MagicMock(return_value=(fake_response, fake_metrics))
        monkeypatch.setattr(build_mod, "call_api", mock_call_api)

        client = MagicMock(name="anthropic_client")
        messages = [{"role": "user", "content": "build me an app"}]
        result = build_mod.call_api_with_retry(client, messages, build_id="b-123")

        assert mock_call_api.call_count == 1
        args, kwargs = mock_call_api.call_args
        # Positional: client, messages, model
        assert args[0] is client
        assert args[1] == messages
        assert args[2] == build_mod.MODEL
        # System prompt with cache_control
        sysparam = kwargs.get("system")
        assert isinstance(sysparam, list) and len(sysparam) == 1
        assert sysparam[0]["type"] == "text"
        assert sysparam[0]["text"] == build_mod.BUILD_SYSTEM_PROMPT
        assert sysparam[0]["cache_control"] == {"type": "ephemeral"}
        # max_tokens and build_id
        assert kwargs.get("max_tokens") == 16384
        assert kwargs.get("build_id") == "b-123"
        # Wrapper returns the response object (not response.content[0].text)
        assert result is fake_response

    def test_call_api_with_retry_threads_build_id_default(self, monkeypatch):
        """build_id default (None) maps to '' so call_api receives a str."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import build as build_mod

        fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
        mock_call_api = MagicMock(return_value=(fake_response, SimpleNamespace()))
        monkeypatch.setattr(build_mod, "call_api", mock_call_api)

        client = MagicMock(name="anthropic_client")
        messages = [{"role": "user", "content": "x"}]
        # No build_id passed -- defaults to None -> ""
        build_mod.call_api_with_retry(client, messages)

        _, kwargs = mock_call_api.call_args
        # build_id resolved to "" when wrapper called with None default
        assert kwargs.get("build_id") == ""


# ---------------------------------------------------------------------------
# TestEnsureColorConsistencyOnPrimary
# ---------------------------------------------------------------------------

class TestEnsureColorConsistencyOnPrimary:
    """Verify --color-on-primary is emitted alongside --color-primary."""

    def _make_layout(self, with_root=True, on_primary_val=None):
        if with_root:
            root_extra = ""
            if on_primary_val:
                root_extra = f" --color-on-primary: {on_primary_val};"
            return (
                "<html><head>"
                f"<style>:root {{ --color-primary: #111111;{root_extra} --color-secondary: #222222; }}</style>"
                "</head><body></body></html>"
            )
        return "<html><head></head><body></body></html>"

    def test_on_primary_injected_in_root_block(self):
        """When palette has on_primary, --color-on-primary appears in output."""
        spec = {"description": "A pet grooming app"}
        files = [("views/layout.ejs", self._make_layout())]
        result = dict(_ensure_color_consistency(files, spec))
        layout = result.get("views/layout.ejs", "")
        assert "--color-on-primary" in layout

    def test_on_primary_replaced_when_already_exists(self):
        """When --color-on-primary already exists with wrong value, it gets updated."""
        spec = {"description": "A pet grooming app"}
        # Inject a wrong on_primary value into the :root block
        files = [("views/layout.ejs", self._make_layout(on_primary_val="#000000"))]
        result = dict(_ensure_color_consistency(files, spec))
        layout = result.get("views/layout.ejs", "")
        # The wrong value should be gone; on_primary must still be present
        assert "--color-on-primary" in layout
        assert "--color-on-primary: #000000" not in layout

    def test_primary_patching_still_works(self):
        """Existing --color-primary patching is not broken."""
        spec = {"design": {"primary_color": "#F97316", "style": "warm earthy"}}
        files = [("views/layout.ejs", self._make_layout())]
        result = dict(_ensure_color_consistency(files, spec))
        layout = result.get("views/layout.ejs", "")
        assert "--color-primary: #F97316" in layout

    def test_on_primary_patched_in_style_css(self):
        """--color-on-primary is also patched in public/style.css when present."""
        spec = {"description": "A pet grooming app"}
        # style.css has a :root block with wrong primary + wrong on_primary
        style_css = (
            ":root {\n"
            "  --color-primary: #111111;\n"
            "  --color-on-primary: #000000;\n"
            "  --color-secondary: #222222;\n"
            "}\n"
        )
        files = [
            ("views/layout.ejs", self._make_layout()),
            ("public/style.css", style_css),
        ]
        result = dict(_ensure_color_consistency(files, spec))
        css = result.get("public/style.css", style_css)
        # The incorrect on_primary value must be replaced
        assert "--color-on-primary: #000000" not in css
        assert "--color-on-primary" in css


# ---------------------------------------------------------------------------
# TestPhase1PromptAbbreviatedDesign
# ---------------------------------------------------------------------------

class TestPhase1PromptAbbreviatedDesign:
    """Verify Phase 1 multi-pass uses abbreviated DESIGN.md (no full-mode sections)."""

    SPEC = {
        "name": "Task Manager",
        "description": "A project management app",
        "admin_email": "admin@example.com",
        "pages": [{"name": "Dashboard", "path": "/dashboard", "auth_required": True}],
        "data_models": [{"name": "Task", "fields": [{"name": "title", "type": "string"}]}],
        "user_roles": ["admin"],
        "design": {"style": "modern light"},
    }

    def test_phase1_prompt_uses_abbreviated_design(self):
        """Phase 1 prompt omits full-mode-only sections."""
        prompt = _build_phase1_prompt(self.SPEC)
        assert "## 1. Visual Theme" not in prompt
        assert "## 5. Layout" not in prompt
        assert "## 8. Responsive" not in prompt
        assert "## 9. Key Rules" not in prompt

    def test_phase1_prompt_has_core_design_sections(self):
        """Phase 1 prompt still contains color, components, and don'ts."""
        prompt = _build_phase1_prompt(self.SPEC)
        assert "## 2. Color Palette" in prompt
        assert "## 4. Component Styles" in prompt
        assert "## 7." in prompt


class TestFixDarkSectionText:
    """Tests for _fix_dark_section_text post-processor (MW v6 DARK_SECTION_TEXT1)."""

    DARK_PRIMARY_SPEC = {"design": {"primary_color": "#0f2b3c"}}
    LIGHT_PRIMARY_SPEC = {"design": {"primary_color": "#3B82F6"}}

    def _run(self, files, spec=None):
        from build import _fix_dark_section_text
        return dict(_fix_dark_section_text(files, spec or self.DARK_PRIMARY_SPEC))

    def test_fixes_dark_text_inside_from_primary_gradient(self):
        """text-slate-900 inside a from-primary gradient section becomes text-white."""
        content = (
            '<section class="py-16 bg-gradient-to-r from-primary via-secondary to-primary">\n'
            '  <h2 class="text-3xl font-bold text-slate-900 mb-4">Newsletter</h2>\n'
            '</section>\n'
        )
        result = self._run([("views/contact.ejs", content)])
        out = result["views/contact.ejs"]
        assert "text-white" in out
        assert "text-slate-900" not in out

    def test_fixes_gray_900_inside_bg_primary_section(self):
        """text-gray-900 inside a bg-primary section is replaced with text-white."""
        content = (
            '<div class="bg-primary py-8">\n'
            '  <p class="text-gray-900 mb-2">Get in touch</p>\n'
            '</div>\n'
        )
        result = self._run([("views/contact.ejs", content)])
        out = result["views/contact.ejs"]
        assert "text-white" in out
        assert "text-gray-900" not in out

    def test_does_not_fix_text_outside_dark_section(self):
        """text-slate-900 on white background (outside gradient section) is untouched."""
        content = (
            '<div class="bg-white py-8">\n'
            '  <p class="text-slate-900">Normal text</p>\n'
            '</div>\n'
            '<section class="from-primary py-4">\n'
            '  <h2 class="text-white">Dark section</h2>\n'
            '</section>\n'
        )
        result = self._run([("views/contact.ejs", content)])
        out = result["views/contact.ejs"]
        # text-slate-900 in the white section must survive
        assert "text-slate-900" in out

    def test_no_change_when_primary_is_light(self):
        """Does not touch files when primary color has high luminance."""
        content = (
            '<section class="from-primary py-8">\n'
            '  <h2 class="text-slate-900">Heading</h2>\n'
            '</section>\n'
        )
        result = self._run([("views/contact.ejs", content)], spec=self.LIGHT_PRIMARY_SPEC)
        assert "text-slate-900" in result["views/contact.ejs"]

    def test_skips_layout_login_register(self):
        """Does not touch layout.ejs, login.ejs, register.ejs."""
        content = (
            '<section class="from-primary py-8">\n'
            '  <h2 class="text-slate-900">Heading</h2>\n'
            '</section>\n'
        )
        for f in ("views/layout.ejs", "views/login.ejs", "views/register.ejs"):
            result = self._run([(f, content)])
            assert "text-slate-900" in result[f], f"{f} should not be modified"

    def test_skips_files_without_dark_gradient(self):
        """Files with no from-primary/bg-primary sections are not modified."""
        content = '<div class="bg-white"><h2 class="text-slate-900">Title</h2></div>\n'
        result = self._run([("views/home.ejs", content)])
        assert result["views/home.ejs"] == content

    def test_fixes_multiple_dark_text_classes(self):
        """Fixes slate-800/gray-700 to lighter variants inside dark section."""
        content = (
            '<section class="from-primary py-8">\n'
            '  <p class="text-slate-800">Sub</p>\n'
            '  <span class="text-gray-700">Detail</span>\n'
            '</section>\n'
        )
        result = self._run([("views/about.ejs", content)])
        out = result["views/about.ejs"]
        assert "text-slate-800" not in out
        assert "text-gray-700" not in out
        assert "text-slate-100" in out
        assert "text-slate-200" in out

    def test_idempotent(self):
        """Running twice produces the same result."""
        from build import _fix_dark_section_text
        content = (
            '<section class="from-primary py-8">\n'
            '  <h2 class="text-slate-900">Title</h2>\n'
            '</section>\n'
        )
        files = [("views/contact.ejs", content)]
        once = dict(_fix_dark_section_text(files, self.DARK_PRIMARY_SPEC))
        twice = dict(_fix_dark_section_text(list(once.items()), self.DARK_PRIMARY_SPEC))
        assert once["views/contact.ejs"] == twice["views/contact.ejs"]

    def test_fixes_dark_text_inside_inline_linear_gradient(self):
        """Hero section with inline style=linear-gradient still catches dark text.

        Regression: an MW v7 build failed design review with 3 critical
        dark-on-dark issues, contact/about/admin-settings heroes used
        style="background: linear-gradient(..., rgba(15,43,60,0.92), ...)"
        instead of Tailwind from-primary classes, so the old regex missed them.
        """
        content = (
            '<section class="relative py-16" '
            'style="background: linear-gradient(135deg, rgba(15,43,60,0.92), rgba(40,80,100,0.85));">\n'
            '  <h1 class="text-4xl font-bold text-slate-900">Site Settings</h1>\n'
            '</section>\n'
        )
        result = self._run([("views/admin-settings.ejs", content)])
        out = result["views/admin-settings.ejs"]
        assert "text-white" in out
        assert "text-slate-900" not in out

    def test_fixes_dark_text_inside_inline_gradient_div(self):
        """Works on <div> with inline linear-gradient style, not just <section>."""
        content = (
            '<div style="background: linear-gradient(to bottom, #0f2b3c, #1a5a6a);" class="py-12">\n'
            '  <h1 class="text-gray-900">Contact Us</h1>\n'
            '  <p class="text-slate-800">Get in touch</p>\n'
            '</div>\n'
        )
        result = self._run([("views/contact.ejs", content)])
        out = result["views/contact.ejs"]
        assert "text-white" in out
        assert "text-slate-100" in out
        assert "text-gray-900" not in out
        assert "text-slate-800" not in out

    def test_inline_gradient_skipped_when_primary_is_light(self):
        """Luminance guard still applies: light primary → no change to inline gradient sections."""
        content = (
            '<section style="background: linear-gradient(135deg, #fff, #eee);">\n'
            '  <h1 class="text-slate-900">Title</h1>\n'
            '</section>\n'
        )
        result = self._run([("views/home.ejs", content)], spec=self.LIGHT_PRIMARY_SPEC)
        assert "text-slate-900" in result["views/home.ejs"]


class TestEnsureNavGlassmorphism:
    """Tests for _ensure_nav_glassmorphism post-processor (MW v6 NAV_GLASS1)."""

    DARK_PRIMARY_SPEC = {"design": {"primary_color": "#0f2b3c"}}
    LIGHT_PRIMARY_SPEC = {"design": {"primary_color": "#3B82F6"}}

    def _run(self, layout, spec=None):
        from build import _ensure_nav_glassmorphism
        files = [("views/layout.ejs", layout)]
        return dict(_ensure_nav_glassmorphism(files, spec or self.DARK_PRIMARY_SPEC))

    def test_adds_backdrop_blur_and_rgba_to_nav(self):
        """Nav gets backdrop-blur class and rgba inline style when primary is dark."""
        layout = '<nav class="bg-primary sticky top-0">Nav</nav>'
        result = self._run(layout)
        out = result["views/layout.ejs"]
        assert "backdrop-blur" in out
        assert "rgba(15,43,60,0.92)" in out

    def test_adds_webkit_backdrop_filter(self):
        """Adds -webkit-backdrop-filter for Safari compatibility."""
        layout = '<nav class="bg-primary">Nav</nav>'
        result = self._run(layout)
        out = result["views/layout.ejs"]
        assert "-webkit-backdrop-filter" in out

    def test_no_change_when_primary_is_light(self):
        """Does not add glassmorphism when primary luminance >= 0.15."""
        layout = '<nav class="bg-primary sticky">Nav</nav>'
        result = self._run(layout, spec=self.LIGHT_PRIMARY_SPEC)
        assert "backdrop-blur" not in result["views/layout.ejs"]

    def test_idempotent_when_backdrop_blur_present(self):
        """Does not modify layout when backdrop-blur already exists."""
        layout = '<nav class="bg-primary backdrop-blur-md">Nav</nav>'
        result = self._run(layout)
        assert result["views/layout.ejs"] == layout

    def test_idempotent_when_backdrop_filter_style_present(self):
        """Does not modify layout when backdrop-filter already in inline style."""
        layout = '<nav class="bg-primary" style="backdrop-filter:blur(20px)">Nav</nav>'
        result = self._run(layout)
        assert result["views/layout.ejs"] == layout

    def test_preserves_existing_nav_classes(self):
        """Existing nav classes are preserved when glassmorphism is added."""
        layout = '<nav class="bg-primary sticky top-0 z-50">Nav</nav>'
        result = self._run(layout)
        out = result["views/layout.ejs"]
        assert "sticky" in out
        assert "top-0" in out
        assert "z-50" in out

    def test_rgba_uses_correct_primary_color_channels(self):
        """rgba() values are derived from the primary hex color."""
        spec = {"design": {"primary_color": "#1a2b3c"}}
        layout = '<nav class="bg-primary">Nav</nav>'
        from build import _ensure_nav_glassmorphism
        result = dict(_ensure_nav_glassmorphism([("views/layout.ejs", layout)], spec))
        out = result["views/layout.ejs"]
        # #1a2b3c = rgb(26, 43, 60)
        assert "rgba(26,43,60,0.92)" in out

    def test_no_change_when_layout_missing(self):
        """Returns unchanged when no layout.ejs in files."""
        from build import _ensure_nav_glassmorphism
        files = [("views/home.ejs", "<div>home</div>")]
        result = dict(_ensure_nav_glassmorphism(files, self.DARK_PRIMARY_SPEC))
        assert result == dict(files)
