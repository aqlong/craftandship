"""The browser the worker points at a deployed app is bounded.

cli/deploy.py opens a customer's site in Chromium twice: verify_render (two
full-page screenshots) and capture_deploy_screenshots (the login page and up
to three pages behind the login). The page decides how long it takes and how
tall it is, so each page object gets explicit step and navigation timeouts,
each full-page screenshot is cut at a maximum height, and each function has a
wall-clock budget it checks before starting another page.

Playwright itself is replaced here by a stand-in that records what the
product code asks of it: these tests are about which limits the code sets and
when, not about Chromium. No browser is started and nothing is sent anywhere.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
for _entry in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import deploy

SITE = "https://app.example.com"


class _Clock:
    """Stands in for deploy.time: a clock the test moves by hand."""

    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture()
def browser(monkeypatch):
    """A recording Playwright. `page` is the one page the code opens; every
    call on it is in page.mock_calls in order."""
    page = MagicMock(name="page")
    page.url = SITE + "/dashboard"
    page.goto.return_value = types.SimpleNamespace(status=200)
    page.locator.return_value.count.return_value = 0

    def screenshot(**kwargs):
        Path(kwargs["path"]).write_bytes(b"not a real image")

    page.screenshot.side_effect = screenshot
    launched = MagicMock(name="browser")
    launched.new_page.return_value = page
    playwright = MagicMock(name="playwright")
    playwright.chromium.launch.return_value = launched

    class _Started:
        def __enter__(self):
            return playwright

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        types.SimpleNamespace(sync_playwright=lambda: _Started()))
    clock = _Clock()
    monkeypatch.setattr(deploy, "time", clock)
    return types.SimpleNamespace(page=page, browser=launched, clock=clock)


def _names(page):
    return [call[0] for call in page.mock_calls]


def _limits_come_first(page):
    """Both limits are set on the page before anything is loaded in it."""
    names = _names(page)
    first_load = names.index("goto")
    assert names.index("set_default_timeout") < first_load
    assert names.index("set_default_navigation_timeout") < first_load
    page.set_default_timeout.assert_called_once_with(10_000)
    page.set_default_navigation_timeout.assert_called_once_with(20_000)


class TestVerifyRender:
    def test_the_page_gets_its_limits_before_it_loads_anything(self, browser, tmp_path):
        result = deploy.verify_render(SITE, {"pages": [{"path": "/about"}]}, tmp_path)
        assert result["status"] == "ok", result
        _limits_come_first(browser.page)
        assert browser.browser.new_page.call_args.kwargs["viewport"] == {"width": 1280, "height": 800}

    def test_every_full_page_screenshot_is_cut_at_the_maximum_height(self, browser, tmp_path):
        deploy.verify_render(SITE, {"pages": [{"path": "/about"}]}, tmp_path)
        shots = [call.kwargs for call in browser.page.screenshot.call_args_list]
        assert len(shots) == 2
        for shot in shots:
            assert shot["full_page"] is True
            assert shot["clip"] == {"x": 0, "y": 0, "width": 1280, "height": 16_000}
        assert deploy._SCREENSHOT_MAX_HEIGHT_PX == 16_000

    def test_no_further_page_is_started_once_the_budget_is_spent(self, browser, tmp_path):
        """The first page takes longer than the whole budget. The second is
        not loaded."""
        def slow_page(url, **kwargs):
            browser.clock.now += 100
            return types.SimpleNamespace(status=200)

        browser.page.goto.side_effect = slow_page
        result = deploy.verify_render(SITE, {"pages": [{"path": "/about"}]}, tmp_path, timeout=60)
        assert browser.page.goto.call_count == 1
        assert result["status"] == "warning" and result["details"] == "verify_render timeout reached"
        browser.browser.close.assert_called_once()

    def test_a_page_that_raises_does_not_leave_the_browser_open(self, browser, tmp_path):
        browser.page.goto.side_effect = RuntimeError("Timeout 20000ms exceeded")
        result = deploy.verify_render(SITE, {"pages": []}, tmp_path)
        assert result["status"] == "warning" and "Timeout 20000ms exceeded" in result["details"]
        browser.browser.close.assert_called_once()

    def test_a_url_the_probe_guard_refuses_starts_no_browser(self, browser, tmp_path):
        result = deploy.verify_render("http://127.0.0.1:8080", {"pages": []}, tmp_path)
        assert result["status"] == "warning" and "render refused" in result["details"]
        browser.browser.new_page.assert_not_called()


class TestCaptureDeployScreenshots:
    SPEC = {
        "user_roles": [{"role": "admin"}],
        "pages": [{"path": f"/page-{n}", "auth_required": True, "allowed_roles": ["admin"]} for n in range(5)],
    }

    def test_the_page_gets_its_limits_before_it_loads_anything(self, browser, tmp_path, monkeypatch):
        monkeypatch.setenv("USE_MAGIC_LINK_AUTH", "false")
        captured = deploy.capture_deploy_screenshots(SITE, self.SPEC, tmp_path)
        _limits_come_first(browser.page)
        # The login page and three pages behind it, never more.
        assert browser.page.goto.call_count == 4
        assert len(captured) == 4
        for call in browser.page.screenshot.call_args_list:
            assert call.kwargs["full_page"] is False   # a viewport-sized image

    def test_no_further_page_is_started_once_the_budget_is_spent(self, browser, tmp_path, monkeypatch):
        monkeypatch.setenv("USE_MAGIC_LINK_AUTH", "false")
        assert deploy._SCREENSHOT_RUN_BUDGET_S == 90

        def slow_click(*args, **kwargs):
            browser.clock.now += 100   # the login alone used up the budget

        browser.page.click.side_effect = slow_click
        captured = deploy.capture_deploy_screenshots(SITE, self.SPEC, tmp_path)
        assert browser.page.goto.call_count == 1, "a page behind the login was loaded after the budget ran out"
        assert [Path(shot).name for shot in captured] == ["login.png"]

    def test_each_page_is_checked_against_the_budget(self, browser, tmp_path, monkeypatch):
        """Each page behind the login takes 40 seconds: the first two start
        inside the 90 second budget, the third does not."""
        monkeypatch.setenv("USE_MAGIC_LINK_AUTH", "false")

        def page_load(url, **kwargs):
            if not url.endswith("/login"):
                browser.clock.now += 40
            return types.SimpleNamespace(status=200)

        browser.page.goto.side_effect = page_load
        # Two loads leave the clock at 80s, inside the budget, so a third
        # starts and ends at 120s; the limit of three pages then ends the run.
        deploy.capture_deploy_screenshots(SITE, self.SPEC, tmp_path)
        assert browser.page.goto.call_count == 4

        browser.page.goto.reset_mock()
        browser.clock.now = 1_000.0

        def slower_load(url, **kwargs):
            if not url.endswith("/login"):
                browser.clock.now += 50
            return types.SimpleNamespace(status=200)

        browser.page.goto.side_effect = slower_load
        # 50s each: after two the clock is at 100s, past the budget.
        deploy.capture_deploy_screenshots(SITE, self.SPEC, tmp_path)
        assert browser.page.goto.call_count == 3
