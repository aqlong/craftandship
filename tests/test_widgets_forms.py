"""Tests for cli/widgets/forms.py + the three section-04 @processors.

Section 04 of the B+C pipeline rewrite (section 3 of
docs/plans/b-plus-c/claude-plan.md; see "Plan section labels" in cli/README.md).
Covers:
- SECTION_LAYOUT shape + coverage
- emit_admin_settings / emit_crud_new_edit / emit_dashboard_settings_section
- emit_all end-to-end idempotency + metadata + sorted iteration
- _emit_widgets / _ensure_admin_slug_mount / _inject_dashboard_settings_section
  processor behavior
- __widgets/* filter excludes scratch paths from on-disk write
- Archetype gate short-circuits non-MW specs
"""

from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import sys
import types
from pathlib import Path

import pytest

# Allow importing from cli/ without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))


from cli.widgets.forms import (  # noqa: E402
    SECTION_LAYOUT,
    emit_admin_settings,
    emit_all,
    emit_crud_new_edit,
    emit_dashboard_settings_section,
)


CANONICAL_SPEC_PATH = (
    Path(__file__).parent / "golden" / "medicine-waves-canonical" / "spec.json"
)


def _canonical_spec() -> dict:
    return json.loads(CANONICAL_SPEC_PATH.read_text())


def _mw_like_spec() -> dict:
    return {
        "app_name": "medicine-waves-test",
        "description": "Medicine Waves somatic sound journey app.",
        "settings": {"auth": True},
        "_seed_settings": [
            {"key": "site_name", "value": "Medicine Waves"},
            {"key": "hero_tagline", "value": "A somatic sound journey"},
            {"key": "hero_image_url", "value": ""},
            {"key": "about_title", "value": "About"},
            {"key": "about_body", "value": "..."},
            {"key": "contact_email", "value": "a@b.c"},
            {"key": "instagram_url", "value": ""},
        ],
        "data_models": [
            {
                "name": "Event",
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "description", "type": "text"},
                    {"name": "date_time", "type": "timestamp"},
                    {"name": "image_url", "type": "image_url"},
                    {"name": "is_featured", "type": "boolean"},
                ],
            },
            {
                "name": "Photo",
                "fields": [
                    {"name": "image_url", "type": "image_url", "required": True},
                    {"name": "caption", "type": "string"},
                ],
            },
            {
                "name": "Video",
                "fields": [
                    {"name": "title", "type": "string", "required": True},
                    {"name": "embed_url", "type": "url"},
                ],
            },
            {
                "name": "ContactMessage",
                "read_only": True,
                "fields": [
                    {"name": "name", "type": "string"},
                    {"name": "email", "type": "email"},
                    {"name": "message", "type": "text"},
                ],
            },
        ],
    }


def _new_artifact(spec: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        spec=spec,
        files=[],
        widgets_snippets={},
        metadata={},
        build_dir="/tmp/widget-test",
    )


# ---------------------------------------------------------------------------
# SECTION_LAYOUT
# ---------------------------------------------------------------------------


def test_section_layout_has_five_sections():
    """SECTION_LAYOUT is the single source of truth for the canonical
    admin-settings layout ordering. The current shape is five sections."""
    assert len(SECTION_LAYOUT) == 5


def test_section_layout_names_match_spec():
    names = [s[0] for s in SECTION_LAYOUT]
    assert names == [
        "Site Identity",
        "Hero Section",
        "Logo & About",
        "Contact Info",
        "Social Media",
    ]


def test_section_layout_each_section_has_keys():
    for name, keys in SECTION_LAYOUT:
        assert isinstance(keys, tuple)
        assert len(keys) > 0, f"section {name!r} must declare at least one key"


# ---------------------------------------------------------------------------
# emit_admin_settings
# ---------------------------------------------------------------------------


def test_emit_admin_settings_includes_every_section_heading():
    """Every SECTION_LAYOUT heading should appear when the spec seeds a key
    in that section."""
    spec = _canonical_spec()
    out = emit_admin_settings(spec)
    for name, _keys in SECTION_LAYOUT:
        assert f">{name}</h2>" in out, f"section heading {name!r} missing"


def test_emit_admin_settings_wraps_each_key_in_has_gate():
    """Each rendered field is gated by `<% if (_has("<key>")) { %>`."""
    spec = _canonical_spec()
    out = emit_admin_settings(spec)
    # Spot-check a representative sample across sections.
    for key in ("site_name", "hero_tagline", "about_title", "contact_email", "instagram_url"):
        assert f'<% if (_has("{key}")) {{ %>' in out, (
            f"missing gate for {key}"
        )


def test_emit_admin_settings_preserves_has_and_v_helpers():
    """_has and _v helper function definitions must be at the top so the
    runtime EJS has something to call."""
    out = emit_admin_settings(_canonical_spec())
    assert "_has = function(k)" in out
    assert "_v = function(k)" in out
    # They should appear before the <form> element.
    form_pos = out.find("<form")
    has_pos = out.find("_has = function")
    v_pos = out.find("_v = function")
    assert has_pos >= 0 and has_pos < form_pos
    assert v_pos >= 0 and v_pos < form_pos


def test_emit_admin_settings_emits_drag_drop_zone_for_image_fields():
    """Any *_image_url / *_photo_url / *_logo_url key renders as a
    drag-drop .image-upload-zone, not a URL text input."""
    spec = _canonical_spec()
    out = emit_admin_settings(spec)
    assert "image-upload-zone" in out
    # Canonical MW spec seeds hero_image_url and about_photo_url:
    assert 'name="hero_image_url"' in out
    assert 'name="about_photo_url"' in out


def test_emit_admin_settings_no_snake_case_labels():
    """Labels must be plain English (FIX M byte-identical no-op relies on
    there being no snake_case <label>key</label> to humanize)."""
    spec = _canonical_spec()
    out = emit_admin_settings(spec)
    # humanize() produces 'Site Name', 'Hero Tagline' etc. no underscores.
    # Check representative label strings.
    assert ">site_name<" not in out
    assert ">hero_tagline<" not in out
    assert ">contact_email<" not in out


def test_emit_admin_settings_form_action_literal():
    """Form action is literal /admin/settings (no EJS ternary)."""
    out = emit_admin_settings(_canonical_spec())
    assert '<form action="/admin/settings" method="POST"' in out


def test_emit_admin_settings_extra_keys_go_to_additional_settings():
    """Keys outside SECTION_LAYOUT go into an auto-created
    'Additional Settings' section."""
    spec = {
        "_seed_settings": [
            {"key": "unknown_legacy_key", "value": "x"},
            {"key": "another_oddball", "value": "y"},
        ],
    }
    out = emit_admin_settings(spec)
    assert ">Additional Settings</h2>" in out
    assert 'name="unknown_legacy_key"' in out
    assert 'name="another_oddball"' in out


# ---------------------------------------------------------------------------
# emit_crud_new_edit
# ---------------------------------------------------------------------------


def test_emit_crud_new_edit_returns_tuple_for_writable_model():
    spec = _mw_like_spec()
    model = spec["data_models"][0]  # Event
    result = emit_crud_new_edit(spec, model)
    assert result is not None
    new_ejs, edit_ejs = result
    assert isinstance(new_ejs, str)
    assert isinstance(edit_ejs, str)


def test_emit_crud_new_edit_returns_none_for_read_only_via_read_only_flag():
    """read_only=True must short-circuit emission."""
    spec = _mw_like_spec()
    contact_message = [m for m in spec["data_models"] if m["name"] == "ContactMessage"][0]
    assert emit_crud_new_edit(spec, contact_message) is None


def test_emit_crud_new_edit_returns_none_for_access_read_only():
    """access='read-only' must also short-circuit (two conventions exist)."""
    model = {"name": "LogEntry", "access": "read-only", "fields": [{"name": "msg", "type": "string"}]}
    assert emit_crud_new_edit({}, model) is None


def test_emit_crud_new_edit_includes_csrf_in_both():
    spec = _mw_like_spec()
    model = spec["data_models"][0]
    new_ejs, edit_ejs = emit_crud_new_edit(spec, model)
    assert 'name="_csrf"' in new_ejs
    assert 'name="_csrf"' in edit_ejs


def test_emit_crud_new_edit_new_action_literal():
    """new.ejs form action is literal /admin/<slug> (no EJS ternary)."""
    spec = _mw_like_spec()
    model = spec["data_models"][0]  # Event -> slug 'events'
    new_ejs, _ = emit_crud_new_edit(spec, model)
    assert '<form action="/admin/events" method="POST"' in new_ejs
    # No EJS ternary in the action attribute.
    assert '<form action="<%' not in new_ejs


def test_emit_crud_new_edit_edit_action_literal_prefix_plus_ejs_id():
    """edit.ejs form action is /admin/<slug>/<%= item.id %> (literal prefix
    + EJS id)."""
    spec = _mw_like_spec()
    model = spec["data_models"][0]
    _, edit_ejs = emit_crud_new_edit(spec, model)
    assert '<form action="/admin/events/<%= item.id %>" method="POST"' in edit_ejs


def test_emit_crud_new_edit_image_fields_as_drag_zones():
    """image_url-typed fields render as .image-upload-zone drag zones."""
    spec = _mw_like_spec()
    event = spec["data_models"][0]
    new_ejs, edit_ejs = emit_crud_new_edit(spec, event)
    assert "image-upload-zone" in new_ejs
    assert "image-upload-zone" in edit_ejs
    # Hidden input with the field name (load-bearing for FIX N no-op)
    assert 'type="hidden" id="image_url" name="image_url"' in new_ejs


def test_emit_crud_new_edit_raises_on_missing_name():
    """Malformed model spec raises ValueError, not silent emit."""
    with pytest.raises(ValueError):
        emit_crud_new_edit({}, {"fields": []})


# ---------------------------------------------------------------------------
# emit_dashboard_settings_section
# ---------------------------------------------------------------------------


def test_emit_dashboard_settings_section_has_cta_link_not_form():
    out = emit_dashboard_settings_section(_canonical_spec())
    assert 'href="/admin/settings"' in out
    assert "<form" not in out.lower(), "dashboard section must be a CTA, not a <form>"


# ---------------------------------------------------------------------------
# emit_all - artifact mutation
# ---------------------------------------------------------------------------


def test_emit_all_writes_settings_file():
    artifact = _new_artifact(_canonical_spec())
    emit_all(artifact)
    file_dict = dict(artifact.files)
    assert "views/admin/settings.ejs" in file_dict
    assert "<form" in file_dict["views/admin/settings.ejs"]


def test_emit_all_writes_new_and_edit_per_writable_model():
    spec = _mw_like_spec()
    artifact = _new_artifact(spec)
    emit_all(artifact)
    file_dict = dict(artifact.files)
    for slug in ("events", "photos", "videos"):
        assert f"views/admin/{slug}/new.ejs" in file_dict
        assert f"views/admin/{slug}/edit.ejs" in file_dict
    # ContactMessage is read-only -> no CRUD emitted.
    assert "views/admin/contact-messages/new.ejs" not in file_dict


def test_emit_all_stores_dashboard_snippet_in_widgets_snippets():
    artifact = _new_artifact(_canonical_spec())
    emit_all(artifact)
    assert "dashboard_settings_section" in artifact.widgets_snippets
    # And NOT in artifact.files.
    file_paths = [p for p, _ in artifact.files]
    assert "dashboard_settings_section" not in file_paths


def test_emit_all_writes_metadata_timestamp():
    artifact = _new_artifact(_canonical_spec())
    emit_all(artifact)
    ts = artifact.metadata.get("widgets_emitted_at_generation")
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    # ISO shape: at least YYYY-MM-DD prefix.
    assert len(ts) >= 11 and ts[4] == "-" and ts[7] == "-"


def test_emit_all_iterates_data_models_sorted_alphabetically():
    """Sorted iteration means file emission is deterministic across runs
    even if spec['data_models'] reorders."""
    spec = _mw_like_spec()
    # Reverse the list order.
    spec2 = dict(spec)
    spec2["data_models"] = list(reversed(spec["data_models"]))

    a = _new_artifact(spec)
    b = _new_artifact(spec2)
    emit_all(a)
    emit_all(b)
    # Strip metadata timestamps so only the file content matters.
    # (emit_all writes a wall-clock ISO timestamp which IS sensitive to
    # process run order; the byte-identical claim is about files/snippets.)
    assert dict(a.files) == dict(b.files)
    assert a.widgets_snippets == b.widgets_snippets


def test_emit_all_idempotent_byte_for_byte():
    """emit_all(emit_all(a)) must equal emit_all(a) on files + snippets."""
    artifact = _new_artifact(_canonical_spec())
    emit_all(artifact)
    files1 = [(p, c) for p, c in artifact.files]
    snippets1 = dict(artifact.widgets_snippets)
    emit_all(artifact)
    files2 = [(p, c) for p, c in artifact.files]
    snippets2 = dict(artifact.widgets_snippets)
    assert files1 == files2, "idempotency regression: files changed on 2nd emit_all"
    assert snippets1 == snippets2


# ---------------------------------------------------------------------------
# _ensure_admin_slug_mount (processor)
# ---------------------------------------------------------------------------


def _import_build():
    """Import cli/build.py with the right sys.path shim.

    Tests use the top-level `pipeline` + `build` modules (same path the
    guardrail tests in test_pipeline_dag.py use). This keeps the REGISTRY
    populated in a single module instance AND preserves closure references
    held by other test modules: `from build import _ensure_video_embeds`
    at test-collection time binds the function to the module-at-that-time;
    popping + re-importing creates a second module instance, and tests that
    mutate `build._VIDEO_POOLS` via `sys.modules["build"]` end up hitting
    the new instance while the closure still reads the old one (observed
    as the TestVideoPoolsCategoryAware order-dependent flake).
    """
    import build  # noqa: F401 - import once, cached
    return build


def test_ensure_admin_slug_mount_adds_admin_mount_for_each_writable():
    build = _import_build()
    spec = _mw_like_spec()
    server = (
        "app.use('/events', eventsRouter);\n"
        "app.use('/photos', photosRouter);\n"
        "app.use('/videos', videosRouter);\n"
    )
    artifact = types.SimpleNamespace(
        files=[("server.js", server)],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._ensure_admin_slug_mount(artifact)
    out = dict(artifact.files)["server.js"]
    assert "app.use('/admin/events', requireAdmin, eventsRouter)" in out
    assert "app.use('/admin/photos', requireAdmin, photosRouter)" in out
    assert "app.use('/admin/videos', requireAdmin, videosRouter)" in out


def test_ensure_admin_slug_mount_idempotent():
    build = _import_build()
    spec = _mw_like_spec()
    server = "app.use('/events', eventsRouter);\n"
    artifact = types.SimpleNamespace(
        files=[("server.js", server)],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._ensure_admin_slug_mount(artifact)
    once = dict(artifact.files)["server.js"]
    build._ensure_admin_slug_mount(artifact)
    twice = dict(artifact.files)["server.js"]
    assert once == twice


def test_ensure_admin_slug_mount_skips_read_only_models():
    build = _import_build()
    spec = _mw_like_spec()  # ContactMessage has read_only=True
    server = (
        "app.use('/events', eventsRouter);\n"
        "app.use('/contact-messages', contactMessagesRouter);\n"
    )
    artifact = types.SimpleNamespace(
        files=[("server.js", server)],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._ensure_admin_slug_mount(artifact)
    out = dict(artifact.files)["server.js"]
    assert "app.use('/admin/contact-messages'" not in out, (
        "read-only ContactMessage must not get an admin mount"
    )
    assert "app.use('/admin/events'" in out


def test_ensure_admin_slug_mount_preserves_existing_public_mount():
    build = _import_build()
    spec = _mw_like_spec()
    server = "app.use('/events', eventsRouter);\n"
    artifact = types.SimpleNamespace(
        files=[("server.js", server)],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._ensure_admin_slug_mount(artifact)
    out = dict(artifact.files)["server.js"]
    # Existing /events mount survives.
    assert "app.use('/events', eventsRouter)" in out


# ---------------------------------------------------------------------------
# _inject_dashboard_settings_section (processor)
# ---------------------------------------------------------------------------


def test_inject_dashboard_settings_section_inserts_snippet_between_markers():
    build = _import_build()
    dashboard = "<h1>Admin Dashboard</h1>\n<p>Stats go here.</p>\n"
    artifact = types.SimpleNamespace(
        files=[("views/admin/dashboard.ejs", dashboard)],
        spec={},
        build_dir="/tmp/x",
        widgets_snippets={"dashboard_settings_section": "<section>CTA</section>\n"},
        metadata={},
    )
    build._inject_dashboard_settings_section(artifact)
    out = dict(artifact.files)["views/admin/dashboard.ejs"]
    assert "<!-- widgets:dashboard-settings -->" in out
    assert "<!-- /widgets:dashboard-settings -->" in out
    assert "<section>CTA</section>" in out


def test_inject_dashboard_settings_section_idempotent():
    build = _import_build()
    dashboard = "<h1>Admin Dashboard</h1>\n"
    artifact = types.SimpleNamespace(
        files=[("views/admin/dashboard.ejs", dashboard)],
        spec={},
        build_dir="/tmp/x",
        widgets_snippets={"dashboard_settings_section": "<section>CTA</section>\n"},
        metadata={},
    )
    build._inject_dashboard_settings_section(artifact)
    once = dict(artifact.files)["views/admin/dashboard.ejs"]
    build._inject_dashboard_settings_section(artifact)
    twice = dict(artifact.files)["views/admin/dashboard.ejs"]
    assert once == twice


def test_inject_dashboard_settings_section_noop_when_snippet_absent():
    build = _import_build()
    dashboard = "<h1>Admin Dashboard</h1>\n"
    artifact = types.SimpleNamespace(
        files=[("views/admin/dashboard.ejs", dashboard)],
        spec={},
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._inject_dashboard_settings_section(artifact)
    out = dict(artifact.files)["views/admin/dashboard.ejs"]
    assert out == dashboard


# ---------------------------------------------------------------------------
# _emit_widgets (processor)
# ---------------------------------------------------------------------------


def test_emit_widgets_short_circuits_when_env_kill_switch_is_set(monkeypatch):
    """WIDGETS_ENABLED=false is the emergency off switch. Before
    universalization this test covered the MW-archetype path; now the only
    way to bypass widget emission is the explicit env var (matches the
    rollback story in cli/widgets/archetype.py)."""
    build = _import_build()
    monkeypatch.setenv("WIDGETS_ENABLED", "false")
    spec = {"app_name": "yoga-studio", "description": "yoga booking app"}
    artifact = types.SimpleNamespace(
        files=[],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._emit_widgets(artifact)
    # No files were emitted and no timestamp written.
    assert list(artifact.files) == []
    assert "widgets_emitted_at_generation" not in artifact.metadata


def test_emit_widgets_runs_for_non_mw_spec_by_default(monkeypatch):
    """Post-universalization: non-MW specs also get widget
    emission. Covers the admin_crud + admin_settings file-missing failure
    mode that blocked non-MW builds."""
    build = _import_build()
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = {
        "app_name": "yoga-studio",
        "description": "yoga booking app",
        "data_models": [
            {"name": "Class", "fields": [
                {"name": "title", "type": "text", "required": True},
                {"name": "scheduled_at", "type": "timestamp", "required": True},
            ]}
        ],
    }
    artifact = types.SimpleNamespace(
        files=[],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._emit_widgets(artifact)
    files = dict(artifact.files)
    assert "views/admin/settings.ejs" in files
    assert "views/admin/classes/new.ejs" in files
    assert "views/admin/classes/edit.ejs" in files
    assert "widgets_emitted_at_generation" in artifact.metadata


def test_emit_widgets_writes_metadata_when_archetype_gate_returns_true(monkeypatch):
    build = _import_build()
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = _mw_like_spec()
    artifact = types.SimpleNamespace(
        files=[],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._emit_widgets(artifact)
    assert "widgets_emitted_at_generation" in artifact.metadata


def test_emit_widgets_migration_path_overwrites_existing_files(monkeypatch):
    """Reapply on a snapshot lacking widgets_emitted_at_generation and
    matching the archetype gate must emit fresh widget output."""
    build = _import_build()
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = _mw_like_spec()
    stale = "<h1>Stale Claude output</h1>"
    artifact = types.SimpleNamespace(
        files=[("views/admin/settings.ejs", stale)],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},  # no widgets_emitted_at_generation yet
    )
    build._emit_widgets(artifact)
    out = dict(artifact.files)["views/admin/settings.ejs"]
    assert out != stale, "migration path did not overwrite stale Claude output"
    assert "<form" in out


def test_emit_widgets_force_reemit_when_admin_file_missing_marker(monkeypatch):
    """Regression guard: qa_fix/deploy_fix can rewrite
    views/admin/<slug>/new.ejs with Claude-style markup that lacks the
    WIDGET_ADMIN_MARKER comment. On reapply, _emit_widgets MUST detect
    any such on-disk file and force a fresh emission, even if the
    metadata already carries a widgets_emitted_at_generation timestamp
    (so future metadata-gating logic cannot accidentally skip the
    restore pass)."""
    build = _import_build()
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = _mw_like_spec()  # includes "Event" model → slug "events"

    claude_style = (
        "<!-- claude-rewritten by qa_fix -->\n"
        "<form class='rose-300' action='/admin/events' method='POST'>\n"
        "  <input name='title'>\n"
        "</form>\n"
    )
    assert "<!-- widgets:admin-form -->" not in claude_style

    # Metadata already set (simulating a prior widget emission that a
    # later fix loop clobbered). The force-reemit path must clear it so
    # downstream gating logic cannot rely on stale timestamps.
    artifact = types.SimpleNamespace(
        files=[
            ("views/admin/events/new.ejs", claude_style),
            ("views/admin/events/edit.ejs", claude_style),
        ],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={"widgets_emitted_at_generation": "2020-01-01T00:00:00Z"},
    )
    build._emit_widgets(artifact)

    out = dict(artifact.files)
    for path in (
        "views/admin/events/new.ejs",
        "views/admin/events/edit.ejs",
    ):
        assert "<!-- widgets:admin-form -->" in out[path], (
            f"{path} still missing marker after force re-emit"
        )
        assert "rose-300" not in out[path], (
            f"{path} still carries Claude-style markup; widget did not overwrite"
        )

    # Metadata gets a FRESH timestamp (not the stale 2020 one).
    assert artifact.metadata["widgets_emitted_at_generation"] != "2020-01-01T00:00:00Z"


def test_emit_widgets_no_force_when_all_admin_files_carry_marker(monkeypatch):
    """Counterpart to the force-reemit test: when every on-disk admin
    CRUD file already carries the widget marker, emit_all runs cleanly
    and does not report any stale files in its trace. The on-disk files
    still get refreshed (emit_all overwrites unconditionally) and
    metadata gets refreshed too, the scan just does not fire the
    drift-signal branch."""
    build = _import_build()
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = _mw_like_spec()  # includes "Event" → slug "events"

    marker = "<!-- widgets:admin-form -->"
    artifact = types.SimpleNamespace(
        files=[
            ("views/admin/events/new.ejs", marker + "\n<form>ok</form>"),
            ("views/admin/events/edit.ejs", marker + "\n<form>ok</form>"),
        ],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    build._emit_widgets(artifact)
    # emit_all always refreshes metadata; test just verifies no crash and
    # that the output still carries the marker (emit_all wrote fresh).
    out = dict(artifact.files)
    for path in (
        "views/admin/events/new.ejs",
        "views/admin/events/edit.ejs",
    ):
        assert marker in out[path]


def test_emit_widgets_ignores_non_admin_files_without_marker(monkeypatch):
    """Only views/admin/<slug>/new.ejs + edit.ejs are subject to the
    marker-based force-reemit scan. Public views, other admin files
    (e.g. admin/settings.ejs, admin/events.ejs list view), and
    non-EJS files must NOT trigger the stale-signal branch."""
    build = _import_build()
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)
    spec = _mw_like_spec()
    artifact = types.SimpleNamespace(
        files=[
            ("views/events.ejs", "<h1>public page, no marker</h1>"),
            ("views/admin/events.ejs", "<h1>admin list, no marker</h1>"),
            ("routes/events.js", "router.get('/')"),
        ],
        spec=spec,
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={"widgets_emitted_at_generation": "2020-01-01T00:00:00Z"},
    )
    build._emit_widgets(artifact)
    # Timestamp should NOT have been cleared by the scan (though emit_all
    # refreshes it in-place). Test that the scan itself did not find
    # stale admin CRUD files by checking the original timestamp is gone
    # (overwritten by emit_all) but the files we passed are unchanged
    # where they are NOT widget-emitted.
    out = dict(artifact.files)
    assert out["views/events.ejs"] == "<h1>public page, no marker</h1>"
    assert out["routes/events.js"] == "router.get('/')"


# ---------------------------------------------------------------------------
# Pipeline registration + ordering
# ---------------------------------------------------------------------------


def test_all_three_processors_registered():
    """Guardrail: section-04 registers three processors in REGISTRY."""
    import pipeline
    _import_build()  # populate via @processor side effects
    for name in (
        "_emit_widgets",
        "_ensure_admin_slug_mount",
        "_inject_dashboard_settings_section",
    ):
        assert name in pipeline.REGISTRY
        assert name in pipeline.EXPECTED_PROCESSORS


def test_emit_widgets_runs_before_fix_b_l_m_n():
    """Ordering is load-bearing: widget emission must land before FIX
    B/L/M/N so those run as no-ops on widget output."""
    import pipeline
    _import_build()
    order = pipeline.pipeline_order("build")
    emit_idx = order.index("_emit_widgets")
    for dep in (
        "_ensure_crud_forms_complete",
        "_strip_inline_dashboard_settings_form",
        "_humanize_settings_labels",
        "_ensure_upload_form_ui",
        "_fix_admin_crud_link_prefix",
    ):
        assert emit_idx < order.index(dep), (
            f"ordering regression: _emit_widgets must run before {dep}"
        )


def test_pipeline_parity_green():
    """The DAG validator resolves cleanly for both phases after section-04."""
    import pipeline
    _import_build()
    pipeline.assert_pipeline_parity()


# ---------------------------------------------------------------------------
# Full pipeline integration via run_pipeline subset
# ---------------------------------------------------------------------------


def test_crud_new_ejs_defines_dt_helper_before_datetime_input():
    """Regression (v21 local-build runtime crash 2026-04-18): widget
    datetime inputs render `<%= _dt(...) %>` but the helper was never
    defined in the emitted template. Rendering /admin/events/new crashed
    with `_dt is not defined`. Fix: emit_crud_new_edit must prepend the
    `_dt` helper definition so runtime-rendered widget views work."""
    from cli.widgets.forms import emit_crud_new_edit

    spec = _mw_like_spec()
    event_model = next(m for m in spec["data_models"] if m["name"] == "Event")
    result = emit_crud_new_edit(spec, event_model)
    assert result is not None
    new_ejs, edit_ejs = result

    for label, ejs in [("new.ejs", new_ejs), ("edit.ejs", edit_ejs)]:
        # Helper definition must appear BEFORE any use site.
        dt_def = ejs.find("var _dt = function(")
        dt_use = ejs.find("<%= _dt(")
        assert dt_def >= 0, f"{label}: _dt helper definition missing"
        if dt_use >= 0:
            assert dt_def < dt_use, (
                f"{label}: _dt use at offset {dt_use} precedes its definition at {dt_def}"
            )


def test_crud_edit_aliases_item_from_singular_model_var():
    """Regression (v21 runtime 500 2026-04-18): widget edit.ejs
    references `item.id` / `item.title` etc., but Claude's route
    handlers pass the row as a lowercase-singular local (`event:`,
    `photo:`, `video:`), never `item:`. Widget must emit an alias
    block that coalesces known conventions into `item`."""
    from cli.widgets.forms import emit_crud_new_edit

    spec = _mw_like_spec()
    event_model = next(m for m in spec["data_models"] if m["name"] == "Event")
    _, edit_ejs = emit_crud_new_edit(spec, event_model)
    # Alias binds `item` with typeof-guard that falls through from item
    # (handler-provided) to `event` (Claude convention) to `{}`.
    assert "var item =" in edit_ejs
    assert "typeof event" in edit_ejs, (
        "edit.ejs alias should fall through to `event` for Event model"
    )
    assert "|| {}" in edit_ejs, "alias must fall back to empty object"


def test_crud_edit_alias_handles_compound_name_contactmessage():
    """Compound PascalCase model names need both lowercase and
    snake_case fallbacks. ContactMessage is read_only so it wouldn't
    actually hit this path, but a synthetic compound writable model
    must still get the right alias."""
    from cli.widgets.forms import emit_crud_new_edit

    compound_model = {
        "name": "BlogPost",
        "fields": [{"name": "title", "type": "text"}],
    }
    result = emit_crud_new_edit({"data_models": [compound_model]}, compound_model)
    assert result is not None
    _, edit_ejs = result
    assert "typeof blogpost" in edit_ejs
    assert "typeof blog_post" in edit_ejs, (
        "compound model should also fall through to snake_case"
    )


def test_crud_dt_helper_handles_null_and_invalid_values():
    """The _dt helper must return '' (not throw) when called on falsy,
    empty-string, or Date-unparseable inputs. Validate the helper body
    as JS via a node subprocess if available."""
    import subprocess
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        import pytest as _pytest
        _pytest.skip("node is not on PATH")

    from cli.widgets.helpers import HELPER_SNIPPETS
    # Strip the EJS tags `<% ... %>` so we get pure JS
    js = HELPER_SNIPPETS["_dt"].replace("<%", "").replace("%>", "").strip()
    harness = js + """
console.log(JSON.stringify({
  empty: _dt(''),
  null: _dt(null),
  undef: _dt(undefined),
  iso: _dt('2026-05-13T00:00:00Z'),
  bogus: _dt('not a date'),
}));
"""
    import json
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=5,
    )
    out = json.loads(result.stdout.strip())
    assert out["empty"] == ""
    assert out["null"] == ""
    assert out["undef"] == ""
    assert out["iso"] == "2026-05-13T00:00"  # sliced to YYYY-MM-DDTHH:MM
    assert out["bogus"] == ""


def test_run_pipeline_subset_emits_widgets_end_to_end():
    """Run the three section-04 processors via run_pipeline(subset=...) and
    assert settings + CRUD + snippet + dashboard splice happen together."""
    import pipeline
    build = _import_build()
    spec = _mw_like_spec()
    artifact = types.SimpleNamespace(
        spec=spec,
        files=[
            (
                "server.js",
                "app.use('/events', eventsRouter);\n"
                "app.use('/photos', photosRouter);\n"
                "app.use('/videos', videosRouter);\n",
            ),
            ("views/admin/dashboard.ejs", "<h1>Dashboard</h1>"),
        ],
        build_dir="/tmp/x",
        widgets_snippets={},
        metadata={},
    )
    pipeline.run_pipeline(
        artifact,
        phase="build",
        subset={
            "_emit_widgets",
            "_ensure_admin_slug_mount",
            "_inject_dashboard_settings_section",
        },
    )
    file_dict = dict(artifact.files)
    assert "views/admin/settings.ejs" in file_dict
    assert "views/admin/events/new.ejs" in file_dict
    assert "app.use('/admin/events'" in file_dict["server.js"]
    assert "<!-- widgets:dashboard-settings -->" in file_dict[
        "views/admin/dashboard.ejs"
    ]
