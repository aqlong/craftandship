"""MW v17 FIX J, _ensure_about_photo_fallback.

Regression: v16 about page shows a tiny 20px music-note SVG at 20% opacity
when `siteSettings.about_photo_url` is empty. Looks like a stub, not a
designed placeholder.

Fix: post-processor rewrites the `else` branch in views/about.ejs to use a
meaningful fallback, a curated stock photo path. Prefers filenames matching
'portrait'/'ocean'/'founder'/'about' in the spec's matched stock categories;
falls back to the first available stock photo; as last resort, keeps a
gradient panel but replaces the tiny SVG with a decorative element.

The post-processor is a no-op when about.ejs doesn't have an obvious
`else`-branch fallback pattern, so it won't fight a designer who already
customized the template.
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


ABOUT_V16 = """<section>
  <h1><%= siteSettings.about_title %></h1>
  <div class="grid grid-cols-1 lg:grid-cols-5 gap-10">
    <div class="lg:col-span-3">
      <%= siteSettings.about_body %>
    </div>
    <div class="lg:col-span-2">
      <% if (siteSettings.about_photo_url) { %>
        <img src="<%= siteSettings.about_photo_url %>" alt="Medicine Waves" class="rounded-xl">
      <% } else { %>
        <div class="aspect-[3/4] rounded-xl bg-gradient-to-br from-secondary to-primary border flex items-center justify-center">
          <svg class="w-20 h-20 text-accent/20" fill="currentColor" viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
        </div>
      <% } %>
    </div>
  </div>
</section>
"""


class TestEnsureAboutPhotoFallback:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_about_photo_fallback"), (
            "FIX J: add _ensure_about_photo_fallback(files, spec) to cli/build.py"
        )

    def test_fallback_replaced_with_stock_img_when_stock_available(self):
        """With wellness stock photos pre-copied, the else branch renders an
        <img src='/images/stock/wellness/ocean-portrait.jpg'...> (or another
        curated candidate) instead of the tiny SVG."""
        import build
        spec = {"description": "somatic sound ceremony healing wellness"}
        files = [
            ("views/about.ejs", ABOUT_V16),
            ("public/images/stock/wellness/ocean-portrait.jpg", b"\x00"),
            ("public/images/stock/wellness/ceremony-01.jpg", b"\x00"),
        ]
        files = build._ensure_about_photo_fallback(files, spec)
        about = dict(files)["views/about.ejs"]
        # The tiny svg fallback must be gone
        assert 'class="w-20 h-20 text-accent/20"' not in about, (
            "Tiny SVG placeholder must be replaced"
        )
        # And an <img> pointing to a real stock path must be present in the else branch
        assert "/images/stock/" in about, "Fallback must use a real stock photo path"
        assert "<img" in about.split("<% } else {")[1].split("<% } %>")[0], (
            "else branch must contain an <img> tag"
        )

    def test_prefers_portrait_or_ocean_candidate(self):
        """When multiple stock photos available, prefer filenames containing
        'portrait'/'ocean'/'founder' for the about photo."""
        import build
        spec = {"description": "wellness healing"}
        files = [
            ("views/about.ejs", ABOUT_V16),
            ("public/images/stock/wellness/aromatherapy-01.jpg", b"\x00"),
            ("public/images/stock/wellness/ocean-portrait.jpg", b"\x00"),
            ("public/images/stock/wellness/meditation-01.jpg", b"\x00"),
        ]
        files = build._ensure_about_photo_fallback(files, spec)
        about = dict(files)["views/about.ejs"]
        assert "ocean-portrait" in about or "portrait" in about, (
            "Should prefer a portrait/ocean candidate when available"
        )

    def test_no_stock_no_op(self):
        """With no stock photos in the build, leave about.ejs unchanged
        (don't point at a nonexistent image)."""
        import build
        files = [("views/about.ejs", ABOUT_V16)]
        out = build._ensure_about_photo_fallback(files, {})
        assert dict(out)["views/about.ejs"] == ABOUT_V16

    def test_no_about_ejs_no_op(self):
        """No about.ejs in the build → no-op."""
        import build
        files = [("server.js", "app.listen(3000);")]
        out = build._ensure_about_photo_fallback(files, {})
        assert dict(out) == dict(files)

    def test_idempotent(self):
        import build
        spec = {"description": "wellness"}
        files = [
            ("views/about.ejs", ABOUT_V16),
            ("public/images/stock/wellness/ocean-portrait.jpg", b"\x00"),
        ]
        first = dict(build._ensure_about_photo_fallback(files, spec))
        files2 = list(first.items())
        second = dict(build._ensure_about_photo_fallback(files2, spec))
        assert first["views/about.ejs"] == second["views/about.ejs"]


class TestPipelineWiring:
    """FIX J must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_about_photo_fallback must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_about_photo_fallback" in pipeline.REGISTRY, (
            "FIX J: _ensure_about_photo_fallback must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_about_photo_fallback"]
        assert "build" in proc.phase, (
            "FIX J: _ensure_about_photo_fallback must run in the build phase"
        )

    def test_wired_into_reapply(self):
        """_ensure_about_photo_fallback must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_about_photo_fallback"]
        assert "reapply" in proc.phase, (
            "FIX J: _ensure_about_photo_fallback must also run in the reapply phase"
        )
