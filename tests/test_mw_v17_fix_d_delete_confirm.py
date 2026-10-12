"""MW v17 FIX D, _ensure_delete_confirm_single_handler.

Regression: MW v16 forms popped confirm() TWICE. Root cause: layout.ejs has
both a querySelectorAll('[data-confirm]').forEach(el => el.addEventListener(
'submit', confirm()))` block AND the document-level submit_guard handler.

A separate form (views/events/show.ejs) uses `onsubmit="return confirm(...)"`
which is fine alone but duplicates submit_guard's confirm if the form also
happens to have data-confirm (Claude sometimes emits both).

Fix:
  - Strip the per-element `querySelectorAll(...).forEach(el => el.addEventListener)`
    confirm wiring from layout.ejs (submit_guard already handles data-confirm).
  - Strip `onsubmit="return confirm(...)"` from any <form> that also has
    `data-confirm` (ambiguous double-bind).
  - Do NOT touch onsubmit-only forms (single source of truth, working).
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


LAYOUT_WITH_PER_ELEM_CONFIRM = """<!DOCTYPE html>
<html><head><title>App</title></head><body>
<%- body %>
<script>
  document.addEventListener('DOMContentLoaded', function() {
    // Confirm dialogs
    document.querySelectorAll('[data-confirm]').forEach(function(el) {
      el.addEventListener('submit', function(e) {
        if (!confirm(el.getAttribute('data-confirm'))) {
          e.preventDefault();
        }
      });
    });
  });
</script>
</body></html>
"""


class TestEnsureDeleteConfirmSingleHandler:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_delete_confirm_single_handler"), (
            "FIX D: add _ensure_delete_confirm_single_handler(files, spec=None)"
        )
        assert callable(build._ensure_delete_confirm_single_handler)

    def test_per_element_confirm_wiring_stripped_from_layout(self):
        """The querySelectorAll('[data-confirm]').forEach(...addEventListener('submit', confirm()))
        block must be removed so only the document-level submit_guard fires."""
        import build
        files = [("views/layout.ejs", LAYOUT_WITH_PER_ELEM_CONFIRM)]
        files = build._ensure_delete_confirm_single_handler(files, {})
        layout = dict(files)["views/layout.ejs"]
        # The specific per-element wiring must be gone
        assert "querySelectorAll('[data-confirm]')" not in layout, (
            "Per-element confirm wiring must be stripped from layout.ejs"
        )
        assert ".forEach(function(el)" not in layout or "confirm(el.getAttribute" not in layout, (
            "The forEach-based confirm wiring must be removed"
        )

    def test_onsubmit_stripped_when_data_confirm_present(self):
        """A form with both onsubmit=confirm and data-confirm should have
        onsubmit stripped (submit_guard will pick up data-confirm)."""
        import build
        view = (
            '<form method="POST" action="/events/1/delete" '
            'data-confirm="Delete?" onsubmit="return confirm(\'Delete?\')">'
            '<button>Delete</button></form>'
        )
        files = [("views/events/index.ejs", view)]
        files = build._ensure_delete_confirm_single_handler(files, {})
        content = dict(files)["views/events/index.ejs"]
        assert "data-confirm" in content, "data-confirm should remain (single source of truth)"
        assert "onsubmit" not in content, "onsubmit=confirm must be stripped when data-confirm present"

    def test_onsubmit_alone_left_alone(self):
        """A form with onsubmit=confirm but no data-confirm is working (single handler), leave it."""
        import build
        view = (
            '<form method="POST" action="/events/1/delete" '
            'onsubmit="return confirm(\'Delete?\')">'
            '<button>Delete</button></form>'
        )
        files = [("views/events/show.ejs", view)]
        before = dict(files)["views/events/show.ejs"]
        files = build._ensure_delete_confirm_single_handler(files, {})
        after = dict(files)["views/events/show.ejs"]
        assert before == after, "onsubmit-only form must not be modified"

    def test_data_confirm_alone_left_alone(self):
        """A form with only data-confirm should keep it (submit_guard handles it)."""
        import build
        view = '<form method="POST" action="/e/1" data-confirm="ok"><button>Go</button></form>'
        files = [("views/admin/events.ejs", view)]
        before = dict(files)["views/admin/events.ejs"]
        files = build._ensure_delete_confirm_single_handler(files, {})
        after = dict(files)["views/admin/events.ejs"]
        assert before == after, "data-confirm-only form must not be modified"

    def test_idempotent(self):
        import build
        files = [("views/layout.ejs", LAYOUT_WITH_PER_ELEM_CONFIRM)]
        first = dict(build._ensure_delete_confirm_single_handler(files, {}))
        files2 = list(first.items())
        second = dict(build._ensure_delete_confirm_single_handler(files2, {}))
        assert first["views/layout.ejs"] == second["views/layout.ejs"]


class TestPipelineWiring:
    """FIX D must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_delete_confirm_single_handler must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_delete_confirm_single_handler" in pipeline.REGISTRY, (
            "FIX D: _ensure_delete_confirm_single_handler must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_delete_confirm_single_handler"]
        assert "build" in proc.phase, (
            "FIX D: _ensure_delete_confirm_single_handler must run in the build phase"
        )

    def test_runs_before_ensure_submit_guard(self):
        """Ordering: strip duplicates BEFORE submit_guard injects its script.
        (submit_guard is the single canonical handler so we need the layout
        clean before its insertion.)"""
        import pipeline
        import build  # noqa: F401

        order = pipeline.pipeline_order("build")
        dc_pos = order.index("_ensure_delete_confirm_single_handler")
        sg_pos = order.index("_ensure_submit_guard")
        assert dc_pos < sg_pos, (
            "_ensure_delete_confirm_single_handler must run BEFORE _ensure_submit_guard"
        )

    def test_wired_into_reapply(self):
        """_ensure_delete_confirm_single_handler must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_delete_confirm_single_handler"]
        assert "reapply" in proc.phase, (
            "FIX D: _ensure_delete_confirm_single_handler must also run in the reapply phase"
        )
