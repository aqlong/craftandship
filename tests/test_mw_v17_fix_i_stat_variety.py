"""MW v17 FIX I, _ensure_stat_card_accent_variety.

Regression: v16 admin dashboard stat cards all visually read as cornflower
blue even when markup includes varied accent border classes, because:
  - top border is only 2px (barely visible against a dark card)
  - stat NUMBER text is always text-white, so no color anchors the variety

Fix: when a stat card already carries a `border-t-accent` / accent-warm /
accent-earth / red top border, (a) thicken that border to at least 4px,
and (b) color the stat-number paragraph inside the card with the matching
accent color. This makes the variety visually unambiguous without changing
semantics.
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


DASHBOARD_MARKUP = """<div class="grid grid-cols-4 gap-4 mb-10">
  <div class="bg-slate-800 border border-slate-700 rounded-xl p-6 border-t-2 border-t-accent">
    <p class="text-xs uppercase text-slate-500 mb-1">Events</p>
    <p class="text-3xl font-bold text-white font-display">10</p>
    <a href="/events" class="text-xs text-accent">Manage</a>
  </div>
  <div class="bg-slate-800 border border-slate-700 rounded-xl p-6 border-t-2 border-t-accent-warm">
    <p class="text-xs uppercase text-slate-500 mb-1">Photos</p>
    <p class="text-3xl font-bold text-white font-display">12</p>
    <a href="/photos" class="text-xs text-accent">Manage</a>
  </div>
  <div class="bg-slate-800 border border-slate-700 rounded-xl p-6 border-t-2 border-t-accent-earth">
    <p class="text-xs uppercase text-slate-500 mb-1">Videos</p>
    <p class="text-3xl font-bold text-white font-display">6</p>
    <a href="/videos" class="text-xs text-accent">Manage</a>
  </div>
  <div class="bg-slate-800 border border-slate-700 rounded-xl p-6 border-t-2 border-t-red-500">
    <p class="text-xs uppercase text-slate-500 mb-1">Messages</p>
    <p class="text-3xl font-bold text-white font-display">10</p>
    <a href="/messages" class="text-xs text-accent">View</a>
  </div>
</div>
"""


class TestEnsureStatCardAccentVariety:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_stat_card_accent_variety"), (
            "FIX I: add _ensure_stat_card_accent_variety(files, spec)"
        )
        assert callable(build._ensure_stat_card_accent_variety)

    def test_thin_top_border_upgraded_to_4px(self):
        """border-t-2 on cards with accent top-border classes becomes border-t-4."""
        import build
        files = [("views/admin/dashboard.ejs", DASHBOARD_MARKUP)]
        files = build._ensure_stat_card_accent_variety(files, {})
        content = dict(files)["views/admin/dashboard.ejs"]
        # None of the 4 accent-colored cards should still have border-t-2
        assert content.count("border-t-2 border-t-accent") == 0
        assert content.count("border-t-2 border-t-red") == 0
        # All should now be border-t-4
        assert "border-t-4 border-t-accent" in content
        assert "border-t-4 border-t-accent-warm" in content
        assert "border-t-4 border-t-accent-earth" in content
        assert "border-t-4 border-t-red-500" in content

    def test_stat_numbers_get_matching_accent_color(self):
        """The <p class="...text-white...">NN</p> inside each card gets the
        matching accent class so the NUMBER carries the color variety."""
        import build
        files = [("views/admin/dashboard.ejs", DASHBOARD_MARKUP)]
        files = build._ensure_stat_card_accent_variety(files, {})
        content = dict(files)["views/admin/dashboard.ejs"]
        # Accent (cornflower) card: text-accent applied
        assert "text-accent" in content
        # Warm (yellow) card: text-accent-warm applied
        assert "text-accent-warm" in content
        # Earth (orange) card
        assert "text-accent-earth" in content
        # Red (messages) card
        assert "text-red-400" in content or "text-red-500" in content

    def test_non_admin_view_untouched(self):
        """Only admin views are targeted. A public home page with similar
        markup must not be modified."""
        import build
        files = [("views/home.ejs", DASHBOARD_MARKUP)]
        before = dict(files)["views/home.ejs"]
        files = build._ensure_stat_card_accent_variety(files, {})
        after = dict(files)["views/home.ejs"]
        assert before == after, "Only admin views are in scope"

    def test_idempotent(self):
        import build
        files = [("views/admin/dashboard.ejs", DASHBOARD_MARKUP)]
        first = dict(build._ensure_stat_card_accent_variety(files, {}))
        files2 = list(first.items())
        second = dict(build._ensure_stat_card_accent_variety(files2, {}))
        assert first["views/admin/dashboard.ejs"] == second["views/admin/dashboard.ejs"]

    def test_non_accent_cards_left_alone(self):
        """Cards with no accent top-border class (plain card) must not be touched."""
        import build
        plain = (
            '<div class="bg-slate-800 rounded-xl p-6">'
            '<p class="text-3xl text-white">99</p></div>'
        )
        files = [("views/admin/dashboard.ejs", plain)]
        before = dict(files)["views/admin/dashboard.ejs"]
        files = build._ensure_stat_card_accent_variety(files, {})
        after = dict(files)["views/admin/dashboard.ejs"]
        assert before == after


class TestPipelineWiring:
    """FIX I must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_stat_card_accent_variety must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_stat_card_accent_variety" in pipeline.REGISTRY, (
            "FIX I: _ensure_stat_card_accent_variety must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_stat_card_accent_variety"]
        assert "build" in proc.phase, (
            "FIX I: _ensure_stat_card_accent_variety must run in the build phase"
        )

    def test_wired_into_reapply(self):
        """_ensure_stat_card_accent_variety must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_stat_card_accent_variety"]
        assert "reapply" in proc.phase, (
            "FIX I: _ensure_stat_card_accent_variety must also run in the reapply phase"
        )
