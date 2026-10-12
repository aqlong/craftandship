"""MW v17 FIX K, _ensure_nav_logo_default_seed.

Regression: on fresh boot, `site_settings.nav_logo_url = ''` and the nav
shows text-only brand. When the build includes a logo file (via
_ensure_logo_in_build or the spec's design.logo_data), the site should
default to showing it without requiring a manual upload.

Fix: post-processor inspects the files list for a bundled logo at
`public/logo.png` / `logo.svg` / `logo.webp` / `images/<slug>_logo.png`.
If found, rewrites `INSERT INTO site_settings ... ('nav_logo_url', '')`
to use the discovered logo path. If none exists, leaves seed alone.

No-op when no logo is bundled → doesn't point nav at a 404.
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


SEED_WITH_EMPTY_NAV = (
    "INSERT INTO site_settings (key, value) VALUES ('site_name', 'Medicine Waves') ON CONFLICT DO NOTHING;\n"
    "INSERT INTO site_settings (key, value) VALUES ('nav_logo_url', '') ON CONFLICT DO NOTHING;\n"
    "INSERT INTO site_settings (key, value) VALUES ('hero_image_url', '') ON CONFLICT DO NOTHING;\n"
)


class TestEnsureNavLogoDefaultSeed:
    def test_function_exists(self):
        import build
        assert hasattr(build, "_ensure_nav_logo_default_seed"), (
            "FIX K: add _ensure_nav_logo_default_seed(files, spec)"
        )

    def test_bundled_logo_png_wires_to_seed(self):
        """When public/logo.png exists, seed's nav_logo_url becomes '/logo.png'."""
        import build
        files = [
            ("db/seed.sql", SEED_WITH_EMPTY_NAV),
            ("public/logo.png", b"\x89PNG"),
        ]
        files = build._ensure_nav_logo_default_seed(files, {})
        seed = dict(files)["db/seed.sql"]
        assert "('nav_logo_url', '/logo.png')" in seed, (
            "nav_logo_url must be set to '/logo.png' when public/logo.png exists"
        )

    def test_bundled_svg_logo_preferred_over_png_when_both_exist(self):
        """SVG logos are preferred (crisp at all sizes)."""
        import build
        files = [
            ("db/seed.sql", SEED_WITH_EMPTY_NAV),
            ("public/logo.svg", b"<svg/>"),
            ("public/logo.png", b"\x89PNG"),
        ]
        files = build._ensure_nav_logo_default_seed(files, {})
        seed = dict(files)["db/seed.sql"]
        assert "/logo.svg" in seed

    def test_no_logo_is_noop(self):
        """No bundled logo → seed unchanged."""
        import build
        files = [("db/seed.sql", SEED_WITH_EMPTY_NAV)]
        files = build._ensure_nav_logo_default_seed(files, {})
        assert dict(files)["db/seed.sql"] == SEED_WITH_EMPTY_NAV

    def test_populated_seed_value_preserved(self):
        """If nav_logo_url already has a value, leave it alone."""
        import build
        seed_with_value = SEED_WITH_EMPTY_NAV.replace(
            "('nav_logo_url', '')",
            "('nav_logo_url', '/uploads/custom.png')",
        )
        files = [
            ("db/seed.sql", seed_with_value),
            ("public/logo.png", b"\x89PNG"),
        ]
        files = build._ensure_nav_logo_default_seed(files, {})
        seed = dict(files)["db/seed.sql"]
        assert "/uploads/custom.png" in seed, (
            "Existing explicit seed value must be preserved"
        )
        assert "/logo.png" not in seed.split("nav_logo_url")[1][:100], (
            "Default should not overwrite an explicit value"
        )

    def test_checks_build_dir_via_contextvar(self):
        """When the logo exists on disk but not in the files list (e.g. during
        reapply when binaries are skipped), the contextvar-backed disk scan
        still finds it."""
        import build, tempfile, os
        # Create a tmp build dir with a logo
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "public").mkdir()
            (Path(td) / "public" / "logo.png").write_bytes(b"\x89PNG")
            # Set contextvar
            token = build._CURRENT_BUILD_DIR.set(Path(td))
            try:
                files = [("db/seed.sql", SEED_WITH_EMPTY_NAV)]
                files = build._ensure_nav_logo_default_seed(files, {})
            finally:
                build._CURRENT_BUILD_DIR.reset(token)
            seed = dict(files)["db/seed.sql"]
            assert "/logo.png" in seed

    def test_idempotent(self):
        import build
        files = [
            ("db/seed.sql", SEED_WITH_EMPTY_NAV),
            ("public/logo.svg", b"<svg/>"),
        ]
        first = dict(build._ensure_nav_logo_default_seed(files, {}))
        files2 = list(first.items())
        second = dict(build._ensure_nav_logo_default_seed(files2, {}))
        assert first["db/seed.sql"] == second["db/seed.sql"]


class TestPipelineWiring:
    """FIX K must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app(self):
        """_ensure_nav_logo_default_seed must be registered with phase including 'build'."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_nav_logo_default_seed" in pipeline.REGISTRY, (
            "FIX K: _ensure_nav_logo_default_seed must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_nav_logo_default_seed"]
        assert "build" in proc.phase, (
            "FIX K: _ensure_nav_logo_default_seed must run in the build phase"
        )

    def test_wired_into_reapply(self):
        """_ensure_nav_logo_default_seed must also run in reapply phase."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_nav_logo_default_seed"]
        assert "reapply" in proc.phase, (
            "FIX K: _ensure_nav_logo_default_seed must also run in the reapply phase"
        )
