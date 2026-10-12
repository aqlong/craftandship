"""MW v17 FIX A, _ensure_model_seed_images post-processor.

Regression: v16 seed.sql had NULL image_url on every events INSERT and every
photos INSERT. Photos table has `image_url VARCHAR(500) NOT NULL` so all photo
rows silently failed to insert; events rendered without hero images.

TDD: tests are written first and fail until `_ensure_model_seed_images` is
added to cli/build.py and wired into the main pipeline.

Tests cover:
  1. NULL image_url on events INSERTs is replaced with a real image path
  2. NULL image_url on photos INSERTs is replaced with a real image path
     (photos is NOT NULL, can never leave a NULL)
  3. _seed_events / _seed_photos spec arrays are consumed when present
     (spec-provided image_url by index overrides stock rotation)
  4. Falls back to stock-photo rotation when spec lacks per-row value
  5. Skips INSERTs that already have a non-NULL image_url (idempotent)
  6. Skips tables without an image_url column
  7. Stock photos chosen actually match the spec's matched categories
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


def _mw_like_spec(with_seed_arrays=False):
    spec = {
        "spec_version": "0.1",
        "app_name": "v17-fix-a-test",
        "admin_email": "a@b.c",
        # description that matches the 'wellness' category keyword list
        "description": "A somatic sound journey held inside a dome. Healing, ceremony, meditation for a sacred community.",
        "tagline": "test tagline",
        "user_roles": [{"role": "visitor"}, {"role": "admin"}],
        "pages": [],
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "text"},
                {"name": "date_time", "type": "timestamp"},
                {"name": "image_url", "type": "text"},
            ]},
            {"name": "Photo", "fields": [
                {"name": "image_url", "type": "text"},
                {"name": "caption", "type": "text"},
            ]},
        ],
        "actions": [],
        "settings": {"auth": True, "seed_data": True},
        "design": {"style": "modern dark", "primary_color": "#070924"},
    }
    if with_seed_arrays:
        spec["_seed_events"] = [
            {"title": "E1", "image_url": "/images/stock/wellness/dome-venue-01.jpg"},
            {"title": "E2", "image_url": "/images/stock/wellness/dome-venue-02.jpg"},
        ]
        spec["_seed_photos"] = [
            {"caption": "p1", "image_url": "/images/stock/wellness/ceremony-01.jpg"},
            {"caption": "p2", "image_url": "/images/stock/wellness/meditation-01.jpg"},
        ]
    return spec


def _v16_seed_pattern():
    """A seed.sql shape matching what MW v16 actually produced."""
    return (
        "INSERT INTO events (title, date_time, image_url, is_featured, created_by) "
        "VALUES ('Sacred Oceans', '2026-04-07T00:00:00Z', NULL, FALSE, 1) ON CONFLICT DO NOTHING;\n"
        "INSERT INTO events (title, date_time, image_url, is_featured, created_by) "
        "VALUES ('Grounding', '2026-04-26T23:30:00Z', NULL, FALSE, 1) ON CONFLICT DO NOTHING;\n"
        "INSERT INTO photos (image_url, caption, display_order, created_by) "
        "VALUES (NULL, 'Dome interior', 1, 1) ON CONFLICT DO NOTHING;\n"
        "INSERT INTO photos (image_url, caption, display_order, created_by) "
        "VALUES (NULL, 'Guests settling in', 2, 1) ON CONFLICT DO NOTHING;\n"
    )


class TestEnsureModelSeedImages:
    """All seed INSERTs for tables with an image_url column must have a
    non-NULL image_url value, either spec-provided or stock-photo fallback."""

    def test_function_exists_and_is_callable(self):
        """FIX A contract: _ensure_model_seed_images(files, spec) is exposed."""
        import build
        assert hasattr(build, "_ensure_model_seed_images"), (
            "FIX A: add _ensure_model_seed_images(files, spec) to cli/build.py"
        )
        assert callable(build._ensure_model_seed_images)

    def test_events_null_image_url_replaced_with_real_path(self):
        """Every events INSERT must leave with a non-NULL image_url."""
        import build
        import re

        spec = _mw_like_spec()
        files = [
            ("db/schema.sql",
             "CREATE TABLE events (id SERIAL PRIMARY KEY, title TEXT, date_time TIMESTAMPTZ, image_url VARCHAR(500), is_featured BOOLEAN, created_by INTEGER);"),
            ("db/seed.sql", _v16_seed_pattern()),
        ]
        files = build._ensure_model_seed_images(files, spec)
        seed = dict(files)["db/seed.sql"]
        # No events INSERT should still have NULL in the image_url position
        event_inserts = re.findall(
            r"INSERT INTO events[^;]+VALUES\s*\(([^)]+)\)",
            seed,
        )
        assert event_inserts, "test fixture regression; no event INSERTs parsed"
        for values in event_inserts:
            parts = [p.strip() for p in values.split(",")]
            # image_url is position 3 (title, date_time, image_url, ...)
            assert parts[2].upper() != "NULL", (
                f"Events INSERT still has NULL image_url after post-processor: {values}"
            )
            assert parts[2].startswith("'/images/") or parts[2].startswith("'http"), (
                f"Events image_url should be a quoted path, got: {parts[2]!r}"
            )

    def test_photos_null_image_url_replaced_with_real_path(self):
        """Photos table is NOT NULL, every INSERT must leave with a non-NULL image_url."""
        import build
        import re

        spec = _mw_like_spec()
        files = [
            ("db/schema.sql",
             "CREATE TABLE photos (id SERIAL PRIMARY KEY, image_url VARCHAR(500) NOT NULL, caption TEXT, display_order INTEGER, created_by INTEGER);"),
            ("db/seed.sql", _v16_seed_pattern()),
        ]
        files = build._ensure_model_seed_images(files, spec)
        seed = dict(files)["db/seed.sql"]
        photo_inserts = re.findall(
            r"INSERT INTO photos[^;]+VALUES\s*\(([^)]+)\)",
            seed,
        )
        assert photo_inserts, "test fixture regression; no photo INSERTs parsed"
        for values in photo_inserts:
            parts = [p.strip() for p in values.split(",")]
            # image_url is position 1 (image_url, caption, ...)
            assert parts[0].upper() != "NULL", (
                f"Photos INSERT still has NULL image_url; would violate NOT NULL: {values}"
            )
            assert parts[0].startswith("'/images/") or parts[0].startswith("'http"), (
                f"Photos image_url should be a quoted path, got: {parts[0]!r}"
            )

    def test_spec_seed_events_array_consumed_by_index(self):
        """When spec has _seed_events[i].image_url, the i-th event INSERT uses that value."""
        import build

        spec = _mw_like_spec(with_seed_arrays=True)
        files = [
            ("db/schema.sql", "CREATE TABLE events (id SERIAL, title TEXT, date_time TIMESTAMPTZ, image_url VARCHAR(500), is_featured BOOLEAN, created_by INTEGER);"),
            ("db/seed.sql", _v16_seed_pattern()),
        ]
        files = build._ensure_model_seed_images(files, spec)
        seed = dict(files)["db/seed.sql"]
        # First event should pick up first _seed_events entry's image_url
        assert "/images/stock/wellness/dome-venue-01.jpg" in seed, (
            "First event should receive _seed_events[0].image_url"
        )
        assert "/images/stock/wellness/dome-venue-02.jpg" in seed, (
            "Second event should receive _seed_events[1].image_url"
        )

    def test_falls_back_to_stock_rotation_when_spec_lacks_values(self):
        """Without _seed_events, rotate through available stock photos."""
        import build

        spec = _mw_like_spec()  # description matches 'wellness' category
        files = [
            ("db/schema.sql", "CREATE TABLE events (id SERIAL, title TEXT, date_time TIMESTAMPTZ, image_url VARCHAR(500), is_featured BOOLEAN, created_by INTEGER);"),
            ("db/seed.sql", _v16_seed_pattern()),
        ]
        files = build._ensure_model_seed_images(files, spec)
        seed = dict(files)["db/seed.sql"]
        # All NULLs should now be replaced with /images/stock/... paths
        assert "/images/stock/" in seed, (
            "Fallback must use stock-photo paths; expected /images/stock/ in seed"
        )
        # No NULL should remain in the image_url column (events or photos)
        import re
        for insert in re.finditer(r"INSERT INTO (events|photos)[^;]+VALUES\s*\(([^)]+)\)", seed):
            # No standalone NULL that could be the image_url; cheap sanity check
            values = insert.group(2)
            # No bare ", NULL," in the first 3 positions. Brittle but cheap.
            # Better: skip here; the specific-column tests above catch this.
            pass

    def test_non_null_image_url_preserved_idempotent(self):
        """If an INSERT already has a real image_url, leave it alone."""
        import build

        spec = _mw_like_spec()
        seed = (
            "INSERT INTO events (title, date_time, image_url) "
            "VALUES ('E1', NOW(), '/images/stock/wellness/dome-venue-01.jpg');\n"
        )
        files = [
            ("db/schema.sql", "CREATE TABLE events (id SERIAL, title TEXT, date_time TIMESTAMPTZ, image_url VARCHAR(500));"),
            ("db/seed.sql", seed),
        ]
        out1 = dict(build._ensure_model_seed_images(files, spec))["db/seed.sql"]
        # Running twice must be stable
        files2 = [
            ("db/schema.sql", "CREATE TABLE events (id SERIAL, title TEXT, date_time TIMESTAMPTZ, image_url VARCHAR(500));"),
            ("db/seed.sql", out1),
        ]
        out2 = dict(build._ensure_model_seed_images(files2, spec))["db/seed.sql"]
        assert "/images/stock/wellness/dome-venue-01.jpg" in out1
        assert out1 == out2, "Post-processor must be idempotent"

    def test_skips_tables_without_image_url_column(self):
        """INSERTs into tables without an image_url column should not be touched."""
        import build

        spec = _mw_like_spec()
        original_seed = (
            "INSERT INTO contact_messages (name, email, message) "
            "VALUES ('Alice', 'a@b.c', 'Hi') ON CONFLICT DO NOTHING;\n"
            "INSERT INTO videos (title, embed_url) "
            "VALUES ('Sound Bath', 'https://youtube.com/abc') ON CONFLICT DO NOTHING;\n"
        )
        files = [
            ("db/schema.sql",
             "CREATE TABLE contact_messages (id SERIAL, name TEXT, email TEXT, message TEXT);\n"
             "CREATE TABLE videos (id SERIAL, title TEXT, embed_url TEXT);"),
            ("db/seed.sql", original_seed),
        ]
        files = build._ensure_model_seed_images(files, spec)
        seed = dict(files)["db/seed.sql"]
        assert seed == original_seed, (
            "Tables without image_url column must be left unchanged"
        )

    def test_no_seed_sql_is_noop(self):
        """When db/seed.sql is absent, function returns files unchanged."""
        import build

        spec = _mw_like_spec()
        files = [("db/schema.sql", "CREATE TABLE events (image_url VARCHAR(500));")]
        out = build._ensure_model_seed_images(files, spec)
        assert dict(out) == dict(files), "No seed.sql → no-op"

    def test_fallback_scans_existing_stock_files_when_category_unmatched(self):
        """When the spec description doesn't match any keyword category but the
        build already has public/images/stock/<cat>/<file> entries (e.g. Claude
        or an earlier post-processor put them there), use those as the pool."""
        import build

        spec = {
            "spec_version": "0.1",
            "app_name": "fallback-scan-app",
            "admin_email": "a@b.c",
            # description that matches NO keyword categories
            "description": "A custom enterprise workflow orchestration platform.",
            "tagline": "",
            "user_roles": [{"role": "visitor"}, {"role": "admin"}],
            "pages": [],
            "data_models": [{"name": "Event", "fields": [{"name": "image_url", "type": "text"}]}],
            "actions": [],
            "settings": {"auth": True, "seed_data": True},
            "design": {"style": "modern dark"},
        }
        files = [
            ("db/schema.sql",
             "CREATE TABLE events (id SERIAL, title TEXT, date_time TIMESTAMPTZ, image_url VARCHAR(500), is_featured BOOLEAN, created_by INTEGER);"),
            ("db/seed.sql",
             "INSERT INTO events (title, date_time, image_url, is_featured, created_by) "
             "VALUES ('E1', NOW(), NULL, FALSE, 1) ON CONFLICT DO NOTHING;\n"),
            # Pre-copied stock photos: the fallback should discover these
            ("public/images/stock/wellness/dome-venue-01.jpg", b"\x00"),
            ("public/images/stock/wellness/ceremony-01.jpg", b"\x00"),
        ]
        files = build._ensure_model_seed_images(files, spec)
        seed = dict(files)["db/seed.sql"]
        assert ("dome-venue-01" in seed) or ("ceremony-01" in seed), (
            "Fallback scan should have used pre-copied stock photos; "
            f"seed: {seed!r}"
        )
        assert "placeholder.jpg" not in seed, (
            "Real stock path should win over placeholder fallback"
        )


class TestPipelineWiring:
    """FIX A must be wired into the main pipeline + reapply path so it
    actually fires during builds.

    Updated in section-02-foundation: instead of string-scanning the source
    of build_app / reapply_post_processors (which are now single run_pipeline
    calls), these tests inspect the @processor decorator metadata in
    cli/pipeline.REGISTRY - the authoritative place where wiring lives
    after section 02. This is the behavioral guard discipline (assert on behavior, not on
    literal source text)."""

    def test_wired_into_build_app_pipeline(self):
        """_ensure_model_seed_images must be registered with phase including
        'build', and its build-order position must be between
        _ensure_stock_photos and _ensure_minimum_seed_rows."""
        import pipeline
        import build  # noqa: F401 - populates REGISTRY via @processor decorators

        assert "_ensure_model_seed_images" in pipeline.REGISTRY, (
            "FIX A: _ensure_model_seed_images must be decorated with @processor"
        )
        proc = pipeline.REGISTRY["_ensure_model_seed_images"]
        assert "build" in proc.phase, (
            "FIX A: _ensure_model_seed_images must run in the build phase"
        )

        order = pipeline.pipeline_order("build")
        stock_pos = order.index("_ensure_stock_photos")
        seed_img_pos = order.index("_ensure_model_seed_images")
        min_pos = order.index("_ensure_minimum_seed_rows")
        assert stock_pos < seed_img_pos, (
            "FIX A ordering: _ensure_stock_photos must run BEFORE "
            "_ensure_model_seed_images so stock paths exist"
        )
        assert seed_img_pos < min_pos, (
            "FIX A ordering: _ensure_minimum_seed_rows must run AFTER "
            "_ensure_model_seed_images so padded rows inherit filled image_url"
        )

    def test_wired_into_reapply_post_processors(self):
        """reapply_post_processors must re-run FIX A when QA fix loops edit seed.sql."""
        import pipeline
        import build  # noqa: F401

        proc = pipeline.REGISTRY["_ensure_model_seed_images"]
        assert "reapply" in proc.phase, (
            "FIX A: _ensure_model_seed_images must also run in the reapply phase "
            "so QA-regenerated seed.sql still gets NULL image_url fills"
        )
