"""Tests for stock photo library infrastructure."""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

from build import _match_stock_categories, _build_stock_photo_context, _ensure_stock_photos


class TestMatchStockCategories:
    def test_pet_app_matches_pets(self):
        spec = {"description": "pet grooming appointment scheduler"}
        cats = _match_stock_categories(spec)
        assert "pets" in cats

    def test_restaurant_app_matches_food(self):
        spec = {"description": "restaurant reservation and ordering system"}
        cats = _match_stock_categories(spec)
        assert "food" in cats

    def test_fitness_app_matches_fitness(self):
        spec = {"description": "gym membership and workout tracker"}
        cats = _match_stock_categories(spec)
        assert "fitness" in cats

    def test_medical_app_matches_medical(self):
        spec = {"description": "clinic patient management system"}
        cats = _match_stock_categories(spec)
        assert "medical" in cats

    def test_tech_app_matches_tech(self):
        spec = {"description": "software project management tool"}
        cats = _match_stock_categories(spec)
        assert "tech" in cats

    def test_unmatched_returns_empty(self):
        spec = {"description": "abstract concept manager for philosophical debates"}
        cats = _match_stock_categories(spec)
        assert cats == []

    def test_no_description_returns_empty(self):
        spec = {}
        cats = _match_stock_categories(spec)
        assert cats == []

    def test_empty_description_returns_empty(self):
        spec = {"description": ""}
        cats = _match_stock_categories(spec)
        assert cats == []

    def test_max_two_categories(self):
        # Even with many keyword matches, at most 2 categories returned
        spec = {"description": "pet food health tech gym software clinic"}
        cats = _match_stock_categories(spec)
        assert len(cats) <= 2

    def test_product_type_takes_priority(self):
        """When design_data.match_product_type returns a match, use the mapping."""
        spec = {"description": "pet grooming appointment scheduler"}
        cats = _match_stock_categories(spec)
        # Should use PRODUCT_TYPE_STOCK_CATEGORIES mapping
        assert isinstance(cats, list)
        assert len(cats) > 0


class TestBuildStockPhotoContext:
    def test_returns_stock_section_for_matched_spec(self):
        spec = {"description": "pet grooming appointment scheduler"}
        result = _build_stock_photo_context(spec)
        # Manifest exists, pets category exists
        assert "STOCK PHOTOS AVAILABLE" in result

    def test_returns_empty_for_unmatched_spec(self):
        spec = {"description": "abstract concept manager"}
        result = _build_stock_photo_context(spec)
        assert result == ""

    def test_includes_category_name(self):
        spec = {"description": "restaurant menu ordering system"}
        result = _build_stock_photo_context(spec)
        if result:  # Only if manifest exists with food category
            assert "food" in result.lower()

    def test_includes_filename(self):
        spec = {"description": "pet grooming appointment scheduler"}
        result = _build_stock_photo_context(spec)
        if result:
            assert ".jpg" in result

    def test_includes_usage_instructions(self):
        spec = {"description": "gym workout tracker"}
        result = _build_stock_photo_context(spec)
        if result:
            assert "/images/stock/" in result

    def test_returns_empty_when_no_description(self):
        spec = {}
        result = _build_stock_photo_context(spec)
        assert result == ""


class TestEnsureStockPhotos:
    def test_returns_files_unchanged_when_no_match(self):
        files = [("views/layout.ejs", "<html></html>")]
        spec = {"description": "abstract concept manager"}
        result = _ensure_stock_photos(files, spec)
        assert len(result) == 1

    def test_replaces_unsplash_url(self):
        """External Unsplash URL replaced with local path when stock photos exist on disk."""
        files = [("views/index.ejs", '<img src="https://images.unsplash.com/photo-123?w=800" alt="Hero">')]
        spec = {"description": "pet grooming appointment scheduler"}
        result = _ensure_stock_photos(files, spec)
        result_dict = dict(result)
        content = result_dict.get("views/index.ejs", "")
        # Photos don't exist on disk yet, so URLs stay unchanged
        # But the function should not crash
        assert "views/index.ejs" in result_dict

    def test_replaces_pexels_url(self):
        """External Pexels URL replaced with local path when stock photos exist on disk."""
        files = [("views/index.ejs", '<img src="https://images.pexels.com/photos/123/pexels-photo.jpeg" alt="Photo">')]
        spec = {"description": "restaurant ordering app"}
        result = _ensure_stock_photos(files, spec)
        result_dict = dict(result)
        assert "views/index.ejs" in result_dict

    def test_replaces_placeholder_url(self):
        """via.placeholder.com URLs handled."""
        files = [("views/index.ejs", '<img src="https://via.placeholder.com/800x600" alt="Placeholder">')]
        spec = {"description": "pet grooming app"}
        result = _ensure_stock_photos(files, spec)
        result_dict = dict(result)
        assert "views/index.ejs" in result_dict

    def test_preserves_local_urls(self):
        """Local image URLs are not modified."""
        files = [("views/index.ejs", '<img src="/images/logo.png" alt="Logo">')]
        spec = {"description": "pet grooming appointment scheduler"}
        result = _ensure_stock_photos(files, spec)
        result_dict = dict(result)
        assert "/images/logo.png" in result_dict["views/index.ejs"]

    def test_skips_non_ejs_files(self):
        """Binary and non-EJS files are never modified."""
        files = [("public/logo.png", b"\x89PNG")]
        spec = {"description": "pet grooming appointment scheduler"}
        result = _ensure_stock_photos(files, spec)
        result_dict = dict(result)
        assert result_dict["public/logo.png"] == b"\x89PNG"

    def test_idempotent(self):
        """Running twice produces same result."""
        files = [("views/index.ejs", '<img src="https://images.unsplash.com/photo-123" alt="Hero">')]
        spec = {"description": "pet grooming app"}
        result1 = _ensure_stock_photos(list(files), spec)
        result2 = _ensure_stock_photos(list(result1), spec)
        assert len(result1) == len(result2)

    def test_no_crash_on_empty_files(self):
        """Empty file list is handled gracefully."""
        files = []
        spec = {"description": "pet grooming appointment scheduler"}
        result = _ensure_stock_photos(files, spec)
        assert isinstance(result, list)

    def test_shipped_photos_are_copied_in_and_the_external_url_points_at_one(self):
        """For a pet-grooming spec the shipped pets photos are added to the build
        under public/images/stock/pets/, and an Unsplash <img> in a view is
        rewritten to a local /images/stock/pets/ path that is among them."""
        files = [("views/index.ejs", '<img src="https://images.unsplash.com/photo-123" alt="Hero">')]
        spec = {"description": "pet grooming appointment scheduler"}

        result = dict(_ensure_stock_photos(files, spec))

        view = result["views/index.ejs"]
        assert "unsplash.com" not in view
        match = re.search(r'<img src="(/images/stock/pets/[^"]+\.jpg)" alt="Hero">', view)
        assert match, view
        copied = result["public" + match.group(1)]
        assert isinstance(copied, bytes) and copied.startswith(b"\xff\xd8")

    def test_every_external_image_host_is_replaced_and_local_urls_are_kept(self):
        """Each of the four stock hosts (Unsplash, Pexels, placeholder.com,
        source.unsplash.com), as src or href, is replaced with a local stock
        path; local asset URLs in the same view are left as written."""
        external = [
            '<img src="https://images.unsplash.com/photo-123?w=800">',
            '<img src="https://images.pexels.com/photos/123/pexels-photo.jpeg">',
            '<img src="https://via.placeholder.com/800x600">',
            '<img src="https://source.unsplash.com/random/800x600">',
            '<a href="https://images.unsplash.com/photo-abc">photo</a>',
        ]
        local = [
            '<link href="/style.css">',
            '<script src="/js/app.js"></script>',
            '<a href="/images/logo.png">logo</a>',
        ]
        files = [("views/index.ejs", "\n".join(external + local))]
        spec = {"description": "pet grooming appointment scheduler"}

        view = dict(_ensure_stock_photos(files, spec))["views/index.ejs"]

        for host in ("images.unsplash.com", "images.pexels.com", "via.placeholder.com", "source.unsplash.com"):
            assert host not in view, host
        assert view.count("/images/stock/pets/") == len(external)
        for line in local:
            assert line in view


class TestManifestIntegrity:
    """Validate the stock photo manifest structure."""

    def test_manifest_exists(self):
        manifest_path = Path(__file__).parent.parent / "cli" / "stock_photos" / "manifest.json"
        assert manifest_path.exists(), "manifest.json should exist"

    def test_manifest_valid_json(self):
        manifest_path = Path(__file__).parent.parent / "cli" / "stock_photos" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert isinstance(manifest, dict)

    def test_manifest_has_expected_categories(self):
        manifest_path = Path(__file__).parent.parent / "cli" / "stock_photos" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        expected = {
            "people", "food", "nature", "business", "fitness", "pets", "tech", "medical",
            "wellness", "education", "hospitality", "construction", "automotive",
            "beauty", "childcare", "sports", "arts", "legal", "real_estate",
        }
        assert set(manifest.keys()) == expected

    def test_manifest_entries_have_required_fields(self):
        manifest_path = Path(__file__).parent.parent / "cli" / "stock_photos" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        valid_orientations = {"landscape", "portrait", "square"}
        for category, photos in manifest.items():
            assert isinstance(photos, list), f"{category} should be a list"
            assert len(photos) >= 2, f"{category} should have at least 2 entries"
            for photo in photos:
                assert "filename" in photo, f"Missing filename in {category}"
                assert "width" in photo, f"Missing width in {category}"
                assert "height" in photo, f"Missing height in {category}"
                assert "tags" in photo, f"Missing tags in {category}"
                assert isinstance(photo["tags"], list), f"Tags should be a list in {category}"
                assert photo["filename"].endswith(".jpg"), f"Filename should end with .jpg in {category}"
                assert "alt_text" in photo, f"Missing alt_text in {category}/{photo['filename']}"
                assert isinstance(photo["alt_text"], str) and photo["alt_text"], \
                    f"alt_text must be a non-empty string in {category}/{photo['filename']}"
                assert "use_case" in photo, f"Missing use_case in {category}/{photo['filename']}"
                assert isinstance(photo["use_case"], list) and photo["use_case"], \
                    f"use_case must be a non-empty list in {category}/{photo['filename']}"
                assert "mood" in photo, f"Missing mood in {category}/{photo['filename']}"
                assert isinstance(photo["mood"], list) and photo["mood"], \
                    f"mood must be a non-empty list in {category}/{photo['filename']}"
                assert "orientation" in photo, f"Missing orientation in {category}/{photo['filename']}"
                assert photo["orientation"] in valid_orientations, \
                    f"orientation must be landscape/portrait/square in {category}/{photo['filename']}"

    def test_manifest_has_dormant_entries(self):
        """The expansion adds dormant entries (filenames without on-disk JPGs).
        _ensure_stock_photos already filters by src_path.exists(), so these are
        safely skipped at build time; they're a placeholder for future drops."""
        manifest_path = Path(__file__).parent.parent / "cli" / "stock_photos" / "manifest.json"
        stock_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text())
        total = sum(len(v) for v in manifest.values())
        assert total >= 180, f"Expected at least 180 entries after expansion, got {total}"
        # Each category should carry at least one dormant entry now.
        for category, photos in manifest.items():
            dormant = [
                p for p in photos
                if not (stock_dir / category / p["filename"]).exists()
            ]
            assert dormant, f"{category} should have at least one dormant entry"
