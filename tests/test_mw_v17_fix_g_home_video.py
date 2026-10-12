"""MW v17 FIX G, home-page video iframe click-to-play (avoid Error 153).

Regression: v16 home page "What is Medicine Waves?" section rendered an
inline YouTube iframe that displayed YouTube "Error 153, Video player
configuration error". Founder's screenshot confirmed.

Root cause: _ensure_video_embeds treats home.ejs as a detail page (basename
"home" isn't in INDEX_PATTERNS) so it emits a live iframe. When the video
ID is invalid, removed, or has embedding disabled, the iframe surfaces the
YouTube error UI instead of failing silently.

Fix:
  1. Widen _ensure_video_embeds INDEX_PATTERNS to include "home" / "landing"
     so home-page video embeds become click-to-play thumbnails going forward.
  2. Add a new _ensure_home_video_click_to_play post-processor that rewrites
     any existing youtube-nocookie iframe in views/home.ejs (or index.ejs)
     into the thumbnail click-through pattern, so a rebuild of an already-
     generated artifact still gets the fix.

Click-to-play behavior:
  - Initial render: /api/video-thumb/<id> image (SVG fallback if missing)
  - Clicks link out to youtube.com/watch?v=<id> (target=_blank)
  - No inline iframe → no Error 153 visible in-page
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


HOME_WITH_IFRAME = """<section class="py-16">
  <h2>What is Medicine Waves?</h2>
  <div class="aspect-video">
    <% var _vidMatch = (featuredVideo.embed_url || "").match(/embed\\/([a-zA-Z0-9_-]{11})/); var _vidId = _vidMatch ? _vidMatch[1] : ""; %>
    <iframe src="https://www.youtube-nocookie.com/embed/<%= _vidId %>" class="w-full h-full" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>
    <p><a href="https://www.youtube.com/watch?v=<%= _vidId %>" target="_blank" rel="noopener">Watch on YouTube</a></p>
  </div>
</section>
"""


class TestEnsureHomeVideoClickToPlay:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_home_video_click_to_play"), (
            "FIX G: add _ensure_home_video_click_to_play(files, spec)"
        )
        assert callable(build._ensure_home_video_click_to_play)

    def test_home_iframe_converted_to_thumbnail(self):
        """Any youtube-nocookie iframe in views/home.ejs gets rewritten to
        a thumbnail + play-icon anchor (no inline iframe)."""
        import build
        files = [("views/home.ejs", HOME_WITH_IFRAME)]
        files = build._ensure_home_video_click_to_play(files, {})
        content = dict(files)["views/home.ejs"]
        assert "youtube-nocookie.com/embed" not in content, (
            "Inline iframe must be replaced (no risk of Error 153)"
        )
        assert "/api/video-thumb/" in content, (
            "Replacement must use the thumbnail proxy route"
        )
        assert 'href="https://www.youtube.com/watch?v=' in content, (
            "Replacement must link out to youtube.com/watch"
        )
        assert 'target="_blank"' in content, "Thumbnail link must open in new tab"

    def test_index_ejs_also_handled(self):
        """Some builds use views/index.ejs instead of views/home.ejs."""
        import build
        files = [("views/index.ejs", HOME_WITH_IFRAME)]
        files = build._ensure_home_video_click_to_play(files, {})
        content = dict(files)["views/index.ejs"]
        assert "youtube-nocookie.com/embed" not in content

    def test_other_pages_untouched(self):
        """iframes on event detail or video detail pages must be left alone;
        those are the intended inline-iframe pages."""
        import build
        files = [("views/events/show.ejs", HOME_WITH_IFRAME)]
        before = dict(files)["views/events/show.ejs"]
        files = build._ensure_home_video_click_to_play(files, {})
        after = dict(files)["views/events/show.ejs"]
        assert before == after, (
            "Event/video detail pages must not be rewritten by FIX G"
        )

    def test_idempotent(self):
        import build
        files = [("views/home.ejs", HOME_WITH_IFRAME)]
        first = dict(build._ensure_home_video_click_to_play(files, {}))
        files2 = list(first.items())
        second = dict(build._ensure_home_video_click_to_play(files2, {}))
        assert first["views/home.ejs"] == second["views/home.ejs"]

    def test_no_iframe_is_noop(self):
        """If home.ejs doesn't have a youtube iframe, processor is a no-op."""
        import build
        plain = "<section><h1>Hero</h1></section>"
        files = [("views/home.ejs", plain)]
        files = build._ensure_home_video_click_to_play(files, {})
        assert dict(files)["views/home.ejs"] == plain


class TestIndexPatternsWiden:
    def test_home_basename_triggers_index_pattern(self):
        """_ensure_video_embeds should treat home/landing like index (prevention)."""
        import build
        import inspect
        src = inspect.getsource(build._ensure_video_embeds)
        # Look for INDEX_PATTERNS definition and verify "home" is present
        assert "home" in src.split("_INDEX_PATTERNS")[1].split("_DETAIL_PATTERNS")[0], (
            "'home' must be in _INDEX_PATTERNS so future builds convert home iframes to thumbnails"
        )


class TestPipelineWiring:
    """FIX G must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_home_video_click_to_play must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_home_video_click_to_play" in pipeline.REGISTRY, (
            "FIX G: _ensure_home_video_click_to_play must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_home_video_click_to_play"]
        assert "build" in proc.phase, (
            "FIX G: _ensure_home_video_click_to_play must run in the build phase"
        )

    def test_runs_after_ensure_video_embeds(self):
        """FIX G must run AFTER _ensure_video_embeds (which creates the iframe
        in the first place via EJS-var rewrite)."""
        import pipeline
        import build  # noqa: F401

        order = pipeline.pipeline_order("build")
        ve_pos = order.index("_ensure_video_embeds")
        hv_pos = order.index("_ensure_home_video_click_to_play")
        assert ve_pos < hv_pos, (
            "_ensure_home_video_click_to_play must run after _ensure_video_embeds"
        )

    def test_wired_into_reapply(self):
        """_ensure_home_video_click_to_play must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_home_video_click_to_play"]
        assert "reapply" in proc.phase, (
            "FIX G: _ensure_home_video_click_to_play must also run in the reapply phase"
        )
