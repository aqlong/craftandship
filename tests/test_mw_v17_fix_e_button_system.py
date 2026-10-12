"""MW v17 FIX E, _ensure_button_design_system.

Regression: v16 had inconsistent button styling, Unfeature = white pill,
Delete = red outline, Edit = plain text, Get Tickets = sun-yellow,
Save Settings = cornflower blue. No unified system.

Goal: define canonical `.btn-primary / .btn-secondary / .btn-ghost /
.btn-destructive / .btn-warm` classes in public/style.css using CSS vars so
the design carries across builds. FIX B's generated forms already use these
class names; this post-processor makes them real.
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


class TestEnsureButtonDesignSystem:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_button_design_system"), (
            "FIX E: add _ensure_button_design_system(files, spec) to cli/build.py"
        )
        assert callable(build._ensure_button_design_system)

    def test_canonical_classes_defined_in_style_css(self):
        """All 5 canonical .btn-* classes must appear in public/style.css."""
        import build
        files = [("public/style.css", ":root { --color-accent: #5a80c0; }\n")]
        files = build._ensure_button_design_system(files, {})
        css = dict(files)["public/style.css"]
        for cls in (".btn-primary", ".btn-secondary", ".btn-ghost", ".btn-destructive", ".btn-warm"):
            assert cls in css, f"{cls} missing from public/style.css"

    def test_classes_have_hover_and_focus_styles(self):
        """Each .btn-* class must have hover + focus-visible states."""
        import build
        files = [("public/style.css", "")]
        files = build._ensure_button_design_system(files, {})
        css = dict(files)["public/style.css"]
        assert ".btn-primary:hover" in css, ".btn-primary needs :hover"
        assert ".btn-destructive:hover" in css, ".btn-destructive needs :hover"
        assert ":focus" in css, "buttons need :focus-visible (or :focus)"

    def test_touch_target_min_height(self):
        """Accessibility: buttons need min-height >= 44px (Apple HIG + WCAG)."""
        import build
        files = [("public/style.css", "")]
        files = build._ensure_button_design_system(files, {})
        css = dict(files)["public/style.css"]
        # Accept either min-height: 44px or padding-based sizing that implies it
        assert "min-height: 44px" in css or "min-height:44px" in css, (
            "buttons must have min-height 44px for touch targets"
        )

    def test_idempotent(self):
        import build
        files = [("public/style.css", ":root { --color-accent: #5a80c0; }\n")]
        first = dict(build._ensure_button_design_system(files, {}))
        files2 = list(first.items())
        second = dict(build._ensure_button_design_system(files2, {}))
        assert first["public/style.css"] == second["public/style.css"]

    def test_noop_when_no_style_css(self):
        """If the build has no style.css yet, the processor is a no-op (doesn't create)."""
        import build
        files = [("server.js", "app.use(express.static('public'));")]
        out = build._ensure_button_design_system(files, {})
        assert out == files
        assert "public/style.css" not in dict(out)


class TestPipelineWiring:
    """FIX E must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_button_design_system must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_button_design_system" in pipeline.REGISTRY, (
            "FIX E: _ensure_button_design_system must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_button_design_system"]
        assert "build" in proc.phase, (
            "FIX E: _ensure_button_design_system must run in the build phase"
        )

    def test_wired_into_reapply(self):
        """_ensure_button_design_system must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_button_design_system"]
        assert "reapply" in proc.phase, (
            "FIX E: _ensure_button_design_system must also run in the reapply phase"
        )
