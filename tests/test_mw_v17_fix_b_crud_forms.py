"""MW v17 FIX B, _ensure_crud_forms_complete post-processor.

Regression: v16 shipped `views/photos/edit.ejs` as a 4-line stub containing
literal "Content for Photos/Edit will appear here." and `views/events/new.ejs`
as heading + error-block with no actual form body. Admins could not create
events OR edit photos.

Goal: detect stub/incomplete CRUD views and replace them with a canonical
form derived from the spec's data_models. Runs AFTER Claude generates the
app and BEFORE upload-form-UI converts image inputs to drag zones.

TDD: all tests fail until _ensure_crud_forms_complete is implemented.
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


def _mw_like_spec():
    return {
        "spec_version": "0.1",
        "app_name": "v17-fix-b-test",
        "admin_email": "a@b.c",
        "description": "Somatic sound ceremony app with events and gallery.",
        "tagline": "",
        "user_roles": [{"role": "visitor"}, {"role": "admin"}],
        "pages": [],
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "text", "required": True},
                {"name": "description", "type": "text"},
                {"name": "date_time", "type": "timestamp", "required": True},
                {"name": "location", "type": "text"},
                {"name": "image_url", "type": "text"},
                {"name": "rsvp_link", "type": "text"},
                {"name": "posh_embed_height", "type": "integer"},
                {"name": "is_featured", "type": "boolean"},
            ]},
            {"name": "Photo", "fields": [
                {"name": "image_url", "type": "text", "required": True},
                {"name": "caption", "type": "text"},
                {"name": "display_order", "type": "integer"},
            ]},
            {"name": "Video", "fields": [
                {"name": "title", "type": "text", "required": True},
                {"name": "description", "type": "text"},
                {"name": "embed_url", "type": "text", "required": True},
                {"name": "display_order", "type": "integer"},
            ]},
        ],
        "actions": [],
        "settings": {"auth": True, "seed_data": True},
        "design": {"style": "modern dark"},
    }


STUB_PHOTO_EDIT = (
    '<h1 class="text-3xl font-bold text-slate-300 mb-8">Photos/Edit</h1>\n'
    '<div class="bg-white rounded-xl shadow-md p-6">\n'
    '  <p class="text-gray-600">Content for Photos/Edit will appear here.</p>\n'
    '</div>\n'
)

STUB_EVENT_NEW = (
    '<section class="py-12"><div>\n'
    '  <a href="/events">Back</a>\n'
    '  <h1 class="font-display text-3xl">Create New Event</h1>\n'
    '  <% if (locals.error) { %><div class="bg-red-900"><%= error %></div><% } %>\n'
    '  </div></section>\n'
)


def _routes_with_standard_pattern():
    """Canonical event/photo/video routes, POST /:id/update for event/video, POST /:id for photo."""
    return [
        ("routes/events.js",
         "const express = require('express');\nconst router = express.Router();\n"
         "router.get('/new', () => {});\n"
         "router.post('/', () => {});\n"
         "router.get('/:id/edit', () => {});\n"
         "router.post('/:id/update', () => {});\n"
         "router.post('/:id/delete', () => {});\n"
         "module.exports = router;\n"),
        ("routes/photos.js",
         "const express = require('express');\nconst router = express.Router();\n"
         "router.get('/new', () => {});\n"
         "router.post('/', () => {});\n"
         "router.get('/:id/edit', () => {});\n"
         "router.post('/:id', () => {});\n"
         "router.post('/:id/delete', () => {});\n"
         "module.exports = router;\n"),
        ("routes/videos.js",
         "const express = require('express');\nconst router = express.Router();\n"
         "router.get('/new', () => {});\n"
         "router.post('/', () => {});\n"
         "router.get('/:id/edit', () => {});\n"
         "router.post('/:id/update', () => {});\n"
         "router.post('/:id/delete', () => {});\n"
         "module.exports = router;\n"),
    ]


class TestEnsureCrudFormsComplete:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_crud_forms_complete"), (
            "FIX B: add _ensure_crud_forms_complete(files, spec) to cli/build.py"
        )
        assert callable(build._ensure_crud_forms_complete)

    def test_photo_edit_stub_is_replaced(self):
        """views/photos/edit.ejs literal 'Content for ...' must be replaced with a form."""
        import build
        spec = _mw_like_spec()
        files = [("views/photos/edit.ejs", STUB_PHOTO_EDIT)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/photos/edit.ejs"]
        assert "Content for" not in content, "Stub placeholder text must be gone"
        assert "<form" in content, "Replaced template must have a <form> element"
        assert 'name="_csrf"' in content, "Form must include CSRF hidden input"
        assert 'method="POST"' in content or 'method="post"' in content, (
            "Form must POST"
        )

    def test_event_new_heading_only_gets_form(self):
        """views/events/new.ejs with heading but no <form> must have form injected."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        assert "<form" in content, "Form must be injected"
        assert 'name="title"' in content, "title field input expected"
        assert 'name="date_time"' in content, "date_time field input expected"
        assert 'name="_csrf"' in content, "CSRF token input expected"

    def test_new_form_action_points_to_collection_root(self):
        """POST action for 'new' must be '/{slug}' (e.g. /events, /photos)."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        assert 'action="/events"' in content, (
            f"New form must POST to '/events'; got: {content[:500]!r}"
        )

    def test_edit_form_action_matches_route(self):
        """POST action for 'edit' must match the actual route (photos /:id, events /:id/update)."""
        import build
        spec = _mw_like_spec()
        files = [("views/photos/edit.ejs", STUB_PHOTO_EDIT)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/photos/edit.ejs"]
        assert 'action="/photos/<%= photo.id %>"' in content, (
            "Photo edit must POST to /photos/:id; the existing photos route "
            "contract; not /update"
        )

    def test_edit_fields_prepopulated(self):
        """Edit form inputs must have value=<%= entity.fieldname %>."""
        import build
        spec = _mw_like_spec()
        files = [("views/photos/edit.ejs", STUB_PHOTO_EDIT)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/photos/edit.ejs"]
        assert 'value="<%= photo.caption' in content or "photo.caption" in content, (
            "Edit form must pre-populate caption from the entity"
        )

    def test_required_fields_get_required_attribute(self):
        """Fields marked required in the spec must have `required` on the input."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        # title is required
        import re
        title_input = re.search(r'<input[^>]*name="title"[^>]*>', content)
        assert title_input, "title input must exist"
        assert "required" in title_input.group(0), "required fields must have `required`"

    def test_image_url_field_is_upload_zone(self):
        """image_url field should become a hidden input so _ensure_upload_form_ui
        can convert it into a drag-and-drop zone downstream. (Post-processor
        chaining: we produce a stable shape that upload_form_ui recognizes.)"""
        import build
        spec = _mw_like_spec()
        files = [("views/photos/edit.ejs", STUB_PHOTO_EDIT)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/photos/edit.ejs"]
        # Either a hidden input (ready for upload_form_ui) or already converted
        assert (
            'type="hidden" id="image_url" name="image_url"' in content
            or 'type="hidden" name="image_url" id="image_url"' in content
            or "image-upload-zone" in content
        ), "image_url field must be a hidden input (or already converted to upload zone)"

    def test_boolean_field_becomes_checkbox(self):
        """is_featured (boolean) should render as <input type='checkbox'>."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        import re
        feat = re.search(r'<input[^>]*name="is_featured"[^>]*>', content)
        assert feat, "is_featured input must exist"
        assert 'type="checkbox"' in feat.group(0), (
            f"is_featured must render as checkbox: {feat.group(0)!r}"
        )

    def test_integer_field_becomes_number_input(self):
        """Integer fields should render as <input type='number'>."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        import re
        peh = re.search(r'<input[^>]*name="posh_embed_height"[^>]*>', content)
        assert peh, "posh_embed_height input must exist"
        assert 'type="number"' in peh.group(0)

    def test_datetime_field_becomes_datetime_local(self):
        """Timestamp fields should use <input type='datetime-local'>."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        import re
        dt = re.search(r'<input[^>]*name="date_time"[^>]*>', content)
        assert dt, "date_time input must exist"
        assert 'type="datetime-local"' in dt.group(0)

    def test_description_field_becomes_textarea(self):
        """Long-text conventional field names (description, body, message) become textarea."""
        import build
        spec = _mw_like_spec()
        files = [("views/events/new.ejs", STUB_EVENT_NEW)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/events/new.ejs"]
        assert '<textarea' in content, "description should render as textarea"
        assert 'name="description"' in content

    def test_idempotent(self):
        """Running the processor twice produces the same output."""
        import build
        spec = _mw_like_spec()
        routes = _routes_with_standard_pattern()
        files = [("views/photos/edit.ejs", STUB_PHOTO_EDIT)] + routes
        first = dict(build._ensure_crud_forms_complete(files, spec))
        # Run again on the first output
        files2 = list(first.items())
        second = dict(build._ensure_crud_forms_complete(files2, spec))
        assert first["views/photos/edit.ejs"] == second["views/photos/edit.ejs"], (
            "Processor must be idempotent"
        )

    def test_non_stub_templates_untouched(self):
        """A template that already has a <form> must be left alone."""
        import build
        spec = _mw_like_spec()
        good_form = (
            '<section><h1>Edit</h1><form method="POST" action="/photos/<%= photo.id %>">'
            '<input type="hidden" name="_csrf" value="<%= csrfToken %>">'
            '<input type="text" name="caption" value="<%= photo.caption %>">'
            '<button>Save</button></form></section>'
        )
        files = [("views/photos/edit.ejs", good_form)] + _routes_with_standard_pattern()
        files = build._ensure_crud_forms_complete(files, spec)
        result = dict(files)["views/photos/edit.ejs"]
        assert result == good_form, "Non-stub template must be left unchanged"

    def test_no_data_model_match_is_noop(self):
        """A stub view for a model not in data_models should be left as-is
        (we can't confidently generate a form without field definitions)."""
        import build
        spec = _mw_like_spec()
        files = [("views/widgets/edit.ejs",
                  '<h1>Widgets/Edit</h1><p>Content for Widgets/Edit will appear here.</p>')]
        files = build._ensure_crud_forms_complete(files, spec)
        content = dict(files)["views/widgets/edit.ejs"]
        # Still a stub: post-processor couldn't infer fields
        assert "Content for" in content, (
            "When no matching data model is found, leave the view alone"
        )


class TestPipelineWiring:
    """FIX B must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_crud_forms_complete must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_crud_forms_complete" in pipeline.REGISTRY, (
            "FIX B: _ensure_crud_forms_complete must be decorated with @processor. "
            "Must run BEFORE _ensure_upload_form_ui so image fields get converted to zones."
        )
        proc = pipeline.REGISTRY["_ensure_crud_forms_complete"]
        assert "build" in proc.phase, (
            "FIX B: _ensure_crud_forms_complete must run in the build phase"
        )

    def test_runs_before_upload_form_ui(self):
        """FIX B creates image_url hidden inputs; _ensure_upload_form_ui converts
        them to drag zones. Ordering is load-bearing."""
        import pipeline
        import build  # noqa: F401

        order = pipeline.pipeline_order("build")
        crud_pos = order.index("_ensure_crud_forms_complete")
        upload_pos = order.index("_ensure_upload_form_ui")
        assert crud_pos < upload_pos, (
            "Ordering regression: _ensure_crud_forms_complete must run BEFORE "
            "_ensure_upload_form_ui"
        )

    def test_wired_into_reapply(self):
        """_ensure_crud_forms_complete must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_crud_forms_complete"]
        assert "reapply" in proc.phase, (
            "FIX B: _ensure_crud_forms_complete must also run in the reapply phase"
        )


# ---------------------------------------------------------------------------
# Section-04: FIX B is a byte-identical no-op on widget-emitted CRUD views
# ---------------------------------------------------------------------------

class TestFixBByteIdenticalOnWidgetOutput:
    """After section-04 lands, widget emission produces CRUD new/edit views
    that already have `<form>`. FIX B's `_is_stub` returns False on these,
    so FIX B early-returns with byte-identical output. These tests pin
    that contract so a future FIX B change can't silently regress it.

    See docs/plans/b-plus-c/claude-plan.md (widget emission).
    """

    def _widget_model(self):
        return {
            "name": "Event",
            "fields": [
                {"name": "title", "type": "string", "required": True},
                {"name": "description", "type": "text"},
                {"name": "image_url", "type": "image_url"},
            ],
        }

    def test_fix_b_byte_identical_on_widget_new_ejs(self):
        """Widget-emitted new.ejs passes through FIX B unchanged."""
        import build
        from cli.widgets.forms import emit_crud_new_edit
        spec = _mw_like_spec()
        model = self._widget_model()
        new_ejs, _ = emit_crud_new_edit(spec, model)

        files_before = [
            ("views/events/new.ejs", new_ejs),
        ] + _routes_with_standard_pattern()
        files_after = dict(build._ensure_crud_forms_complete(files_before, spec))
        assert files_after["views/events/new.ejs"] == new_ejs, (
            "FIX B must be byte-identical no-op on widget-emitted new.ejs"
        )

    def test_fix_b_byte_identical_on_widget_edit_ejs(self):
        """Widget-emitted edit.ejs passes through FIX B unchanged."""
        import build
        from cli.widgets.forms import emit_crud_new_edit
        spec = _mw_like_spec()
        model = self._widget_model()
        _, edit_ejs = emit_crud_new_edit(spec, model)

        files_before = [
            ("views/events/edit.ejs", edit_ejs),
        ] + _routes_with_standard_pattern()
        files_after = dict(build._ensure_crud_forms_complete(files_before, spec))
        assert files_after["views/events/edit.ejs"] == edit_ejs, (
            "FIX B must be byte-identical no-op on widget-emitted edit.ejs"
        )

    def test_fix_b_still_activates_on_stub_claude_output(self):
        """Backwards-compatibility: when widgets are disabled (the path
        Claude output still takes on non-MW specs), FIX B must still
        replace stub templates as it does today."""
        import build
        spec = _mw_like_spec()
        files = (
            [("views/photos/edit.ejs", STUB_PHOTO_EDIT)]
            + _routes_with_standard_pattern()
        )
        out = dict(build._ensure_crud_forms_complete(files, spec))
        assert "Content for" not in out["views/photos/edit.ejs"], (
            "FIX B stub-replacement regression: stub text should be gone"
        )
        assert "<form" in out["views/photos/edit.ejs"]
