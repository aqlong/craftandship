# tests/test_screenshots.py - Tests for screenshot capture in test and deploy phases

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add cli/ and queue/ to path
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))
sys.path.insert(0, str(Path(__file__).parent.parent / "queue"))


SAMPLE_SPEC = {
    "app_name": "test-app",
    "user_roles": [
        {"role": "admin"},
        {"role": "user"},
    ],
    "pages": [
        {"path": "/dashboard", "auth_required": True, "allowed_roles": ["admin", "user"]},
        {"path": "/settings", "auth_required": True, "allowed_roles": ["admin"]},
        {"path": "/login", "auth_required": False, "allowed_roles": []},
    ],
}


class TestCaptureScreenshots:
    """Tests for cli/test.py capture_screenshots()."""

    def test_returns_empty_list_on_browser_error(self, tmp_path):
        from test import capture_screenshots

        mock_browser = MagicMock()
        mock_browser.new_page.side_effect = Exception("Browser crashed")

        result = capture_screenshots(mock_browser, 4567, SAMPLE_SPEC, str(tmp_path))
        assert result == []

    def test_creates_screenshots_directory(self, tmp_path):
        from test import capture_screenshots

        mock_page = MagicMock()
        mock_page.goto.return_value = None
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None
        mock_page.fill.return_value = None
        mock_page.click.return_value = None

        # Make goto return a mock response with status 200
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto.return_value = mock_response

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        capture_screenshots(mock_browser, 4567, SAMPLE_SPEC, str(tmp_path))
        assert (tmp_path / "screenshots").is_dir()

    def test_captures_login_page(self, tmp_path):
        from test import capture_screenshots

        mock_page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto.return_value = mock_response
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None
        mock_page.fill.return_value = None
        mock_page.click.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        result = capture_screenshots(mock_browser, 4567, SAMPLE_SPEC, str(tmp_path))

        # Should have login + auth pages
        pages_captured = [s["page"] for s in result]
        assert "/login" in pages_captured

    def test_captures_auth_pages_up_to_limit(self, tmp_path):
        from test import capture_screenshots

        # Spec with 7 auth pages
        big_spec = {
            "app_name": "big-app",
            "user_roles": [{"role": "admin"}],
            "pages": [
                {"path": f"/page{i}", "auth_required": True, "allowed_roles": ["admin"]}
                for i in range(7)
            ],
        }

        mock_page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto.return_value = mock_response
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None
        mock_page.fill.return_value = None
        mock_page.click.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        result = capture_screenshots(mock_browser, 4567, big_spec, str(tmp_path))

        # login + max 5 auth pages = 6
        assert len(result) <= 6

    def test_page_failure_does_not_stop_capture(self, tmp_path):
        from test import capture_screenshots

        call_count = 0

        def goto_side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:  # Third goto fails
                raise Exception("Page timeout")
            mock_resp = MagicMock()
            mock_resp.status = 200
            return mock_resp

        mock_page = MagicMock()
        mock_page.goto.side_effect = goto_side_effect
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None
        mock_page.fill.return_value = None
        mock_page.click.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        # Should not raise, and should still capture other pages
        result = capture_screenshots(mock_browser, 4567, SAMPLE_SPEC, str(tmp_path))
        assert isinstance(result, list)


class TestCaptureDeployScreenshots:
    """Tests for cli/deploy.py capture_deploy_screenshots()."""

    @patch("playwright.sync_api.sync_playwright")
    def test_returns_empty_on_playwright_error(self, mock_pw, tmp_path):
        from deploy import capture_deploy_screenshots

        mock_pw.return_value.__enter__ = MagicMock(side_effect=Exception("No browser"))
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        result = capture_deploy_screenshots("https://app.example.com", SAMPLE_SPEC, str(tmp_path))
        assert result == []

    @patch("playwright.sync_api.sync_playwright")
    def test_creates_deploy_screenshots_directory(self, mock_pw, tmp_path):
        from deploy import capture_deploy_screenshots

        mock_page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto.return_value = mock_response
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None
        mock_page.fill.return_value = None
        mock_page.click.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        capture_deploy_screenshots("https://app.example.com", SAMPLE_SPEC, str(tmp_path))
        assert (tmp_path / "screenshots" / "deploy").is_dir()

    @patch("playwright.sync_api.sync_playwright")
    def test_limits_auth_pages_to_three(self, mock_pw, tmp_path):
        from deploy import capture_deploy_screenshots

        big_spec = {
            "app_name": "big-app",
            "user_roles": [{"role": "admin"}],
            "pages": [
                {"path": f"/page{i}", "auth_required": True, "allowed_roles": ["admin"]}
                for i in range(6)
            ],
        }

        mock_page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto.return_value = mock_response
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None
        mock_page.fill.return_value = None
        mock_page.click.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        result = capture_deploy_screenshots("https://app.example.com", big_spec, str(tmp_path))
        # login + max 3 auth pages = 4
        assert len(result) <= 4


class TestStoreScreenshots:
    """Tests for queue/worker_pg.py _store_screenshots()."""

    def test_stores_base64_data(self, tmp_path):
        from worker_pg import _store_screenshots

        # Create a fake PNG file
        fake_png = tmp_path / "screenshots" / "login.png"
        fake_png.parent.mkdir(parents=True)
        fake_png.write_bytes(b"\x89PNG fake image data")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # Merge contract: SELECT runs before UPDATE; first call returns "no row".
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _store_screenshots(mock_conn, "build-123", [str(fake_png)])

        # SELECT then UPDATE.
        assert mock_cursor.execute.call_count == 2
        update_call = mock_cursor.execute.call_args_list[1]
        stored = json.loads(update_call[0][1][0])
        assert len(stored) == 1
        assert stored[0]["page"] == "/login"
        assert stored[0]["phase"] == "test"
        assert len(stored[0]["data"]) > 0  # base64 data present

    def test_deploy_phase_detected(self, tmp_path):
        from worker_pg import _store_screenshots

        fake_png = tmp_path / "screenshots" / "deploy" / "dashboard.png"
        fake_png.parent.mkdir(parents=True)
        fake_png.write_bytes(b"\x89PNG fake")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _store_screenshots(mock_conn, "build-123", [str(fake_png)])

        # Last execute call is the UPDATE.
        stored = json.loads(mock_cursor.execute.call_args[0][1][0])
        assert stored[0]["phase"] == "deploy"

    def test_skips_empty_list(self):
        from worker_pg import _store_screenshots

        mock_conn = MagicMock()
        _store_screenshots(mock_conn, "build-123", [])
        mock_conn.cursor.assert_not_called()

    def test_handles_missing_file_gracefully(self, tmp_path):
        from worker_pg import _store_screenshots

        mock_conn = MagicMock()
        # Should not raise even with nonexistent file
        _store_screenshots(mock_conn, "build-123", [str(tmp_path / "nonexistent.png")])
        # No entries to store, so cursor should not be called
        mock_conn.cursor.assert_not_called()

    def test_handles_db_error_gracefully(self, tmp_path):
        from worker_pg import _store_screenshots

        fake_png = tmp_path / "screenshots" / "login.png"
        fake_png.parent.mkdir(parents=True)
        fake_png.write_bytes(b"\x89PNG fake")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("DB error")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Should not raise
        _store_screenshots(mock_conn, "build-123", [str(fake_png)])

    def test_merges_with_existing_entries(self, tmp_path):
        """A second call must preserve prior (phase, page) entries instead of
        overwriting builds.screenshots wholesale (regression guard for the
        post-deploy-rendered-screenshot-gate change, 2026-04-10)."""
        from worker_pg import _store_screenshots

        fake_png = tmp_path / "screenshots" / "deploy" / "render-home.png"
        fake_png.parent.mkdir(parents=True)
        fake_png.write_bytes(b"\x89PNG fake")

        prior = [
            {"page": "/login", "data": "AAAA", "phase": "deploy"},
            {"page": "/dashboard", "data": "BBBB", "phase": "test"},
        ]
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (json.dumps(prior),)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _store_screenshots(mock_conn, "build-123", [str(fake_png)])

        # SELECT then UPDATE = 2 execute calls.
        assert mock_cursor.execute.call_count == 2
        update_call = mock_cursor.execute.call_args_list[1]
        merged = json.loads(update_call[0][1][0])
        # All prior entries preserved + 1 new entry appended.
        pages_phases = sorted((e["phase"], e["page"]) for e in merged)
        assert ("deploy", "/login") in pages_phases
        assert ("test", "/dashboard") in pages_phases
        assert any(e.get("phase") == "deploy" and e.get("page") == "/deploy/render/home" for e in merged)
        assert len(merged) == 3

    def test_merge_overwrites_same_phase_page(self, tmp_path):
        """When the same (phase, page) is re-stored the newer entry wins."""
        from worker_pg import _store_screenshots

        fake_png = tmp_path / "screenshots" / "deploy" / "render-home.png"
        fake_png.parent.mkdir(parents=True)
        fake_png.write_bytes(b"\x89PNG newdata")

        prior = [{"page": "/deploy/render/home", "data": "OLDDATA", "phase": "deploy"}]
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (json.dumps(prior),)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _store_screenshots(mock_conn, "build-123", [str(fake_png)])

        merged = json.loads(mock_cursor.execute.call_args_list[1][0][1][0])
        assert len(merged) == 1
        assert merged[0]["data"] != "OLDDATA"
        assert merged[0]["page"] == "/deploy/render/home"

    def test_merge_handles_no_existing_row(self, tmp_path):
        """If the SELECT returns no row (or NULL), behavior matches the
        original single-write contract."""
        from worker_pg import _store_screenshots

        fake_png = tmp_path / "screenshots" / "login.png"
        fake_png.parent.mkdir(parents=True)
        fake_png.write_bytes(b"\x89PNG fake")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _store_screenshots(mock_conn, "build-123", [str(fake_png)])

        merged = json.loads(mock_cursor.execute.call_args_list[1][0][1][0])
        assert len(merged) == 1
        assert merged[0]["page"] == "/login"


class TestPickInternalRenderPath:
    """Tests for cli/deploy.py _pick_internal_render_path()."""

    def test_prefers_public_listing_path_from_data_models(self):
        from deploy import _pick_internal_render_path

        spec = {
            "pages": [
                {"path": "/", "auth_required": False},
                {"path": "/about", "auth_required": False},
                {"path": "/services", "auth_required": False},
            ],
            "data_models": [
                {"name": "Service", "public_listing_path": "/services"},
            ],
        }
        assert _pick_internal_render_path(spec) == "/services"

    def test_falls_back_to_contact(self):
        from deploy import _pick_internal_render_path

        spec = {
            "pages": [
                {"path": "/", "auth_required": False},
                {"path": "/about", "auth_required": False},
                {"path": "/contact", "auth_required": False},
            ],
        }
        assert _pick_internal_render_path(spec) == "/contact"

    def test_skips_auth_required_pages(self):
        from deploy import _pick_internal_render_path

        spec = {
            "pages": [
                {"path": "/", "auth_required": False},
                {"path": "/dashboard", "auth_required": True},
                {"path": "/about", "auth_required": False},
            ],
        }
        # Should pick /about, never /dashboard.
        assert _pick_internal_render_path(spec) == "/about"

    def test_skips_parameterized_paths(self):
        from deploy import _pick_internal_render_path

        spec = {
            "pages": [
                {"path": "/", "auth_required": False},
                {"path": "/items/:id", "auth_required": False},
                {"path": "/about", "auth_required": False},
            ],
        }
        assert _pick_internal_render_path(spec) == "/about"

    def test_returns_contact_default_when_spec_empty(self):
        from deploy import _pick_internal_render_path

        assert _pick_internal_render_path({}) == "/contact"
        assert _pick_internal_render_path(None) == "/contact"


class TestVerifyRender:
    """Tests for cli/deploy.py verify_render()."""

    @patch("playwright.sync_api.sync_playwright")
    def test_returns_warning_on_playwright_setup_failure(self, mock_pw, tmp_path):
        from deploy import verify_render

        mock_pw.return_value.__enter__ = MagicMock(side_effect=Exception("No browser"))
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        result = verify_render("https://app.example.com", SAMPLE_SPEC, str(tmp_path))
        assert result["status"] == "warning"
        assert result["screenshots"] == []
        assert "render setup error" in result["details"]

    @patch("playwright.sync_api.sync_playwright")
    def test_captures_home_and_one_internal_page(self, mock_pw, tmp_path):
        from deploy import verify_render

        mock_response = MagicMock()
        mock_response.status = 200

        mock_page = MagicMock()
        mock_page.goto.return_value = mock_response
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        public_spec = {
            "pages": [
                {"path": "/", "auth_required": False},
                {"path": "/contact", "auth_required": False},
            ],
        }
        result = verify_render("https://app.example.com", public_spec, str(tmp_path))
        assert result["status"] == "ok"
        # Both files are created on disk by the real screenshot call;
        # screenshots list reflects targets attempted.
        assert len(result["screenshots"]) == 2
        assert any("render-home.png" in p for p in result["screenshots"])
        assert any("render-internal-contact.png" in p for p in result["screenshots"])
        # Pages list mirrors the visit order with status_code captured.
        codes = [p["status_code"] for p in result["pages"]]
        assert codes == [200, 200]

    @patch("playwright.sync_api.sync_playwright")
    def test_non_200_downgrades_to_warning_but_keeps_screenshots(self, mock_pw, tmp_path):
        from deploy import verify_render

        responses = [MagicMock(status=200), MagicMock(status=500)]

        def goto_side_effect(url, **kwargs):
            return responses.pop(0)

        mock_page = MagicMock()
        mock_page.goto.side_effect = goto_side_effect
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        spec = {"pages": [{"path": "/", "auth_required": False}, {"path": "/about", "auth_required": False}]}
        result = verify_render("https://app.example.com", spec, str(tmp_path))
        assert result["status"] == "warning"
        # Screenshots still attempted for both pages so the reviewer can see what crashed.
        assert len(result["screenshots"]) == 2
        assert "/about returned 500" in result["details"]

    @patch("playwright.sync_api.sync_playwright")
    def test_navigation_exception_downgrades_to_warning(self, mock_pw, tmp_path):
        from deploy import verify_render

        mock_page = MagicMock()
        mock_page.goto.side_effect = Exception("net::ERR_TIMED_OUT")
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        result = verify_render("https://app.example.com", SAMPLE_SPEC, str(tmp_path))
        assert result["status"] == "warning"
        # Browser was closed cleanly even though every navigation threw.
        mock_browser.close.assert_called()

    @patch("playwright.sync_api.sync_playwright")
    def test_creates_screenshots_deploy_directory(self, mock_pw, tmp_path):
        from deploy import verify_render

        mock_response = MagicMock()
        mock_response.status = 200

        mock_page = MagicMock()
        mock_page.goto.return_value = mock_response
        mock_page.wait_for_load_state.return_value = None
        mock_page.screenshot.return_value = None

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_pw_instance = MagicMock()
        mock_pw_instance.chromium.launch.return_value = mock_browser
        mock_pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
        mock_pw.return_value.__exit__ = MagicMock(return_value=False)

        verify_render("https://app.example.com", {}, str(tmp_path))
        assert (tmp_path / "screenshots" / "deploy").is_dir()
