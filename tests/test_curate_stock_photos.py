"""Tests for scripts/curate_stock_photos.py Pixabay search + download."""
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT_PATH = ROOT / "scripts" / "curate_stock_photos.py"


def _load_module():
    """Load scripts/curate_stock_photos.py as a module without running main()."""
    spec = importlib.util.spec_from_file_location(
        "curate_stock_photos", str(SCRIPT_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def csp():
    return _load_module()


def _fake_response(payload):
    """Return a context-manager-compatible fake response carrying JSON bytes."""
    body = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=io.BytesIO(body))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestSanitizeFilenameStem:
    def test_lowercases_and_dashes_whitespace(self, csp):
        assert csp._sanitize_filename_stem("Golden Retriever") == "golden-retriever"

    def test_strips_punctuation(self, csp):
        assert csp._sanitize_filename_stem("dog, cat!") == "dog-cat"

    def test_falls_back_to_photo_when_empty(self, csp):
        assert csp._sanitize_filename_stem("") == "photo"
        assert csp._sanitize_filename_stem("   ") == "photo"
        assert csp._sanitize_filename_stem("!!!") == "photo"


class TestDownloadPixabayPhotos:
    def test_missing_api_key_returns_empty(self, csp, tmp_path):
        result = csp.download_pixabay_photos(
            api_key="", query="dogs", target_dir=tmp_path
        )
        assert result == []

    def test_none_api_key_returns_empty(self, csp, tmp_path):
        result = csp.download_pixabay_photos(
            api_key=None, query="dogs", target_dir=tmp_path
        )
        assert result == []

    def test_http_error_returns_empty(self, csp, tmp_path):
        import urllib.error

        def opener(url, timeout=None):
            raise urllib.error.URLError("network down")

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="dogs",
            target_dir=tmp_path,
            url_opener=opener,
        )
        assert result == []

    def test_invalid_json_returns_empty(self, csp, tmp_path):
        def opener(url, timeout=None):
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=io.BytesIO(b"not json"))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="dogs",
            target_dir=tmp_path,
            url_opener=opener,
        )
        assert result == []

    def test_empty_hits_returns_empty(self, csp, tmp_path):
        def opener(url, timeout=None):
            return _fake_response({"hits": []})

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="dogs",
            target_dir=tmp_path,
            url_opener=opener,
        )
        assert result == []

    def test_happy_path_returns_saved_file_paths(self, csp, tmp_path):
        payload = {
            "hits": [
                {
                    "id": 101,
                    "tags": "golden retriever, dog",
                    "largeImageURL": "https://pixabay.example/photos/101.jpg",
                },
                {
                    "id": 202,
                    "tags": "puppy, cute",
                    "largeImageURL": "https://pixabay.example/photos/202.jpg",
                },
            ]
        }

        def opener(url, timeout=None):
            assert "key=abc" in url
            assert "image_type=photo" in url
            assert "safesearch=true" in url
            assert "per_page=" in url
            return _fake_response(payload)

        downloaded = []

        def downloader(url, path):
            downloaded.append((url, path))
            Path(path).write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="dogs",
            target_dir=tmp_path,
            per_page=3,
            url_opener=opener,
            file_downloader=downloader,
        )

        assert len(result) == 2
        assert (tmp_path / "golden-retriever-101.jpg").exists()
        assert (tmp_path / "puppy-202.jpg").exists()
        # Returned paths match
        assert str(tmp_path / "golden-retriever-101.jpg") in result
        assert str(tmp_path / "puppy-202.jpg") in result
        # Downloader was called with the largeImageURL from each hit
        urls = [u for u, _ in downloaded]
        assert "https://pixabay.example/photos/101.jpg" in urls
        assert "https://pixabay.example/photos/202.jpg" in urls

    def test_per_page_minimum_clamped_to_3(self, csp, tmp_path):
        seen = {}

        def opener(url, timeout=None):
            seen["url"] = url
            return _fake_response({"hits": []})

        csp.download_pixabay_photos(
            api_key="abc",
            query="dogs",
            target_dir=tmp_path,
            per_page=1,
            url_opener=opener,
        )
        assert "per_page=3" in seen["url"]

    def test_dry_run_does_not_download(self, csp, tmp_path):
        payload = {
            "hits": [
                {
                    "id": 1,
                    "tags": "cat",
                    "largeImageURL": "https://pixabay.example/photos/1.jpg",
                }
            ]
        }

        def opener(url, timeout=None):
            return _fake_response(payload)

        downloader = MagicMock()

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="cats",
            target_dir=tmp_path,
            dry_run=True,
            url_opener=opener,
            file_downloader=downloader,
        )

        assert len(result) == 1
        downloader.assert_not_called()
        # File should not actually exist on disk in dry-run
        assert not (tmp_path / "cat-1.jpg").exists()

    def test_skips_hits_missing_large_image_url(self, csp, tmp_path):
        payload = {
            "hits": [
                {"id": 1, "tags": "x"},  # no largeImageURL → skip
                {
                    "id": 2,
                    "tags": "y",
                    "largeImageURL": "https://pixabay.example/photos/2.jpg",
                },
            ]
        }

        def opener(url, timeout=None):
            return _fake_response(payload)

        def downloader(url, path):
            Path(path).write_bytes(b"x")

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="q",
            target_dir=tmp_path,
            url_opener=opener,
            file_downloader=downloader,
        )

        assert len(result) == 1
        assert str(tmp_path / "y-2.jpg") in result

    def test_download_failure_skips_that_hit(self, csp, tmp_path):
        import urllib.error

        payload = {
            "hits": [
                {
                    "id": 1,
                    "tags": "ok",
                    "largeImageURL": "https://pixabay.example/photos/1.jpg",
                },
                {
                    "id": 2,
                    "tags": "fail",
                    "largeImageURL": "https://pixabay.example/photos/2.jpg",
                },
            ]
        }

        def opener(url, timeout=None):
            return _fake_response(payload)

        def downloader(url, path):
            if "2.jpg" in url:
                raise urllib.error.URLError("connection reset")
            Path(path).write_bytes(b"x")

        result = csp.download_pixabay_photos(
            api_key="abc",
            query="q",
            target_dir=tmp_path,
            url_opener=opener,
            file_downloader=downloader,
        )

        assert len(result) == 1
        assert str(tmp_path / "ok-1.jpg") in result
        assert str(tmp_path / "fail-2.jpg") not in result

    def test_creates_target_dir_if_missing(self, csp, tmp_path):
        target = tmp_path / "nested" / "newdir"
        assert not target.exists()

        payload = {
            "hits": [
                {
                    "id": 1,
                    "tags": "x",
                    "largeImageURL": "https://pixabay.example/photos/1.jpg",
                }
            ]
        }

        def opener(url, timeout=None):
            return _fake_response(payload)

        def downloader(url, path):
            Path(path).write_bytes(b"x")

        csp.download_pixabay_photos(
            api_key="abc",
            query="q",
            target_dir=target,
            url_opener=opener,
            file_downloader=downloader,
        )

        assert target.exists()
        assert target.is_dir()

    def test_query_is_url_encoded(self, csp, tmp_path):
        seen = {}

        def opener(url, timeout=None):
            seen["url"] = url
            return _fake_response({"hits": []})

        csp.download_pixabay_photos(
            api_key="abc",
            query="business team office professional",
            target_dir=tmp_path,
            url_opener=opener,
        )
        # Either + or %20 is acceptable URL-encoding for the space.
        assert "business" in seen["url"]
        assert ("business+team" in seen["url"]) or ("business%20team" in seen["url"])
