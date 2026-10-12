"""Browser-based visual quality verification for generated apps (Stage 3.75).

Two-layer testing:
  Layer 1: Programmatic CSS assertions via Playwright (determines pass/fail)
  Layer 2: Claude Vision scoring (data-collection only, no gating)

Non-blocking: low scores are warnings, not build failures.
"""

import json
import os
import re
import base64
from pathlib import Path
from typing import Optional

from api_client import call_api, make_client

UI_TEST_PORT = 4569
VISION_MODEL = "claude-sonnet-4-6"

# Check weights (total = 12)
CHECK_WEIGHTS = {
    "nav_dark_bg": 2.0,
    "body_not_flat_white": 1.5,
    "cards_border_radius": 1.0,
    "buttons_visible_bg": 1.5,
    "google_fonts": 1.5,
    "viewport_meta": 1.0,
    "gradient_classes": 1.5,
    "text_contrast": 2.0,
}

# Flat white RGB values that indicate missing design work
FLAT_WHITE_COLORS = {
    (255, 255, 255),  # #ffffff
    (250, 250, 250),  # #fafafa
    (249, 250, 251),  # #f9fafb
}

PASS_THRESHOLD = 7.0


# ---------------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------------

def _contrast_ratio(l1: float, l2: float) -> float:
    """Compute WCAG 2.0 contrast ratio between two relative luminance values.

    Returns a value >= 1.0. WCAG AA requires >= 4.5 for normal text.
    """
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# JS snippet that walks up from an element to find the first opaque background.
_JS_EFFECTIVE_BG = """
(el) => {
    let node = el;
    while (node && node !== document.documentElement) {
        const bg = window.getComputedStyle(node).backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return bg;
        node = node.parentElement;
    }
    return window.getComputedStyle(document.body).backgroundColor || 'rgb(255,255,255)';
}
"""

WCAG_AA_RATIO = 4.5


def _luminance_from_rgb(r: int, g: int, b: int) -> float:
    """Compute WCAG 2.0 relative luminance from RGB values (0-255).

    Formula: for each channel, linearize via sRGB transfer function,
    then combine with luminance coefficients.
    """
    channels = []
    for c in (r, g, b):
        c_linear = c / 255.0
        if c_linear <= 0.03928:
            c_linear = c_linear / 12.92
        else:
            c_linear = ((c_linear + 0.055) / 1.055) ** 2.4
        channels.append(c_linear)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _parse_rgb(css_color: str) -> Optional[tuple]:
    """Parse CSS rgb(r,g,b) or rgba(r,g,b,a) into (r, g, b) tuple.

    Returns None if the string cannot be parsed.
    """
    if not css_color or not isinstance(css_color, str):
        return None
    match = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", css_color)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _parse_vision_score(response_text: str) -> Optional[dict]:
    """Extract JSON {score, strengths, weaknesses} from Claude Vision response.

    Handles raw JSON, JSON in markdown fences, and JSON surrounded by text.
    Returns parsed dict or None.
    """
    if not response_text:
        return None

    # Try markdown code fence first
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response_text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Try the whole string as JSON
    try:
        return json.loads(response_text.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find JSON object in the text
    brace_match = re.search(r"\{[^{}]*\"score\"[^{}]*\}", response_text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    return None


# ---------------------------------------------------------------------------
# Programmatic CSS checks (Layer 1)
# Each accepts a Playwright page object, returns {name, passed, detail}.
# ---------------------------------------------------------------------------

def _check_nav_dark_bg(page) -> dict:
    """Check that the nav element has a dark background (luminance < 0.3).

    Uses page.evaluate() to get computed background-color of the nav element.
    """
    name = "nav_dark_bg"
    try:
        bg_color = page.evaluate("""() => {
            const nav = document.querySelector('nav');
            if (!nav) return null;
            return window.getComputedStyle(nav).backgroundColor;
        }""")

        if bg_color is None:
            return {"name": name, "passed": False, "detail": "Nav element not found"}

        rgb = _parse_rgb(bg_color)
        if rgb is None:
            return {"name": name, "passed": False, "detail": f"Could not parse color: {bg_color}"}

        lum = _luminance_from_rgb(*rgb)
        passed = lum < 0.3
        return {
            "name": name,
            "passed": passed,
            "detail": f"luminance={lum:.3f}, bg={bg_color}",
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_body_not_flat_white(page) -> dict:
    """Check that the body background is not flat white.

    Flat white means #ffffff, #fafafa, or #f9fafb. Also checks for gradient
    utility classes in the page HTML which override the bg color check.
    """
    name = "body_not_flat_white"
    try:
        bg_color = page.evaluate("""() => {
            const body = document.querySelector('body');
            if (!body) return null;
            return window.getComputedStyle(body).backgroundColor;
        }""")

        has_gradient = page.evaluate("""() => {
            const html = document.documentElement.innerHTML;
            return /bg-gradient|from-|via-|to-/.test(html);
        }""")

        if has_gradient:
            return {"name": name, "passed": True, "detail": "Gradient classes found"}

        if bg_color is None:
            return {"name": name, "passed": False, "detail": "Body element not found"}

        rgb = _parse_rgb(bg_color)
        if rgb is None:
            return {"name": name, "passed": False, "detail": f"Could not parse color: {bg_color}"}

        is_flat = rgb in FLAT_WHITE_COLORS
        return {
            "name": name,
            "passed": not is_flat,
            "detail": f"bg={bg_color}, flat_white={'yes' if is_flat else 'no'}",
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_cards_border_radius(page) -> dict:
    """Check that card-like elements (with shadow) have non-zero border-radius."""
    name = "cards_border_radius"
    try:
        cards = page.evaluate("""() => {
            const els = document.querySelectorAll(
                '.shadow, .shadow-md, .shadow-lg, .shadow-xl, [class*="shadow"]'
            );
            return Array.from(els).slice(0, 20).map(el => ({
                borderRadius: window.getComputedStyle(el).borderRadius,
                tag: el.tagName.toLowerCase(),
            }));
        }""")

        if not cards:
            return {"name": name, "passed": True, "detail": "No shadow elements found"}

        sharp_count = 0
        for card in cards:
            br = card.get("borderRadius", "0px")
            # "0px" or "0" means no rounding
            if br in ("0px", "0", "0em", "0rem"):
                sharp_count += 1

        total = len(cards)
        rounded_pct = ((total - sharp_count) / total) * 100 if total else 100
        passed = sharp_count < (total / 2)  # Majority should be rounded
        return {
            "name": name,
            "passed": passed,
            "detail": f"{total - sharp_count}/{total} cards rounded ({rounded_pct:.0f}%)",
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_buttons_visible_bg(page) -> dict:
    """Check that submit/action buttons have a visible background color.

    Transparent (rgba(0,0,0,0)) or white backgrounds on buttons suggest
    missing styling.
    """
    name = "buttons_visible_bg"
    try:
        buttons = page.evaluate("""() => {
            const btns = document.querySelectorAll(
                'button[type="submit"], button:not([type]), input[type="submit"]'
            );
            return Array.from(btns).slice(0, 20).map(btn => ({
                bg: window.getComputedStyle(btn).backgroundColor,
                text: (btn.textContent || btn.value || '').trim().substring(0, 30),
                tag: btn.tagName.toLowerCase(),
            }));
        }""")

        if not buttons:
            return {"name": name, "passed": True, "detail": "No buttons found"}

        invisible_count = 0
        for btn in buttons:
            bg = btn.get("bg", "")
            rgb = _parse_rgb(bg)
            if rgb is None:
                invisible_count += 1
                continue
            # Check for transparent (rgba with alpha 0)
            if "rgba" in bg:
                alpha_match = re.search(r"rgba\([^)]*,\s*([\d.]+)\s*\)", bg)
                if alpha_match and float(alpha_match.group(1)) == 0:
                    invisible_count += 1
                    continue
            # Check for white bg on buttons (usually unstyled)
            if rgb == (255, 255, 255):
                invisible_count += 1

        total = len(buttons)
        passed = invisible_count < (total / 2)  # Majority should have visible bg
        return {
            "name": name,
            "passed": passed,
            "detail": f"{total - invisible_count}/{total} buttons have visible background",
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_google_fonts(page) -> dict:
    """Check for a Google Fonts link tag in the document head."""
    name = "google_fonts"
    try:
        has_fonts = page.evaluate("""() => {
            const links = document.querySelectorAll('link[href*="fonts.googleapis.com"]');
            return links.length > 0;
        }""")
        return {
            "name": name,
            "passed": bool(has_fonts),
            "detail": "Google Fonts link " + ("found" if has_fonts else "not found"),
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_viewport_meta(page) -> dict:
    """Check for a viewport meta tag in the document head."""
    name = "viewport_meta"
    try:
        has_viewport = page.evaluate("""() => {
            const meta = document.querySelector('meta[name="viewport"]');
            return !!meta;
        }""")
        return {
            "name": name,
            "passed": bool(has_viewport),
            "detail": "Viewport meta " + ("found" if has_viewport else "not found"),
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_gradient_classes(page) -> dict:
    """Check for Tailwind gradient utility classes in the page HTML."""
    name = "gradient_classes"
    try:
        has_gradient = page.evaluate("""() => {
            const html = document.documentElement.innerHTML;
            return /bg-gradient|from-|via-|to-/.test(html);
        }""")
        return {
            "name": name,
            "passed": bool(has_gradient),
            "detail": "Gradient classes " + ("found" if has_gradient else "not found"),
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


def _check_text_contrast(page) -> dict:
    """Check rendered text contrast against WCAG AA (4.5:1 ratio).

    Samples up to 3 elements: first h1, first h2, and first body paragraph.
    Uses getComputedStyle to read actual rendered colors, then walks up the
    DOM to find the effective background color (first non-transparent ancestor).
    Fails if ANY sampled element has contrast ratio below WCAG_AA_RATIO.
    """
    name = "text_contrast"
    try:
        samples = page.evaluate("""() => {
            function effectiveBg(el) {
                let node = el;
                while (node && node !== document.documentElement) {
                    const bg = window.getComputedStyle(node).backgroundColor;
                    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return bg;
                    node = node.parentElement;
                }
                const bodyBg = window.getComputedStyle(document.body).backgroundColor;
                return (bodyBg && bodyBg !== 'rgba(0, 0, 0, 0)' && bodyBg !== 'transparent')
                    ? bodyBg : 'rgb(255, 255, 255)';
            }
            const selectors = ['h1', 'h2', 'main p, section p, .hero p, [class*="hero"] p, p'];
            const results = [];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (!el) continue;
                const fg = window.getComputedStyle(el).color;
                const bg = effectiveBg(el);
                results.push({selector: sel, fg: fg, bg: bg, tag: el.tagName.toLowerCase()});
            }
            return results;
        }""")

        if not samples:
            return {"name": name, "passed": True, "detail": "No text elements found to check"}

        failures = []
        min_ratio = 21.0  # max possible
        for s in samples:
            fg_rgb = _parse_rgb(s.get("fg", ""))
            bg_rgb = _parse_rgb(s.get("bg", ""))
            if fg_rgb is None or bg_rgb is None:
                continue
            fg_lum = _luminance_from_rgb(*fg_rgb)
            bg_lum = _luminance_from_rgb(*bg_rgb)
            ratio = _contrast_ratio(fg_lum, bg_lum)
            min_ratio = min(min_ratio, ratio)
            if ratio < WCAG_AA_RATIO:
                failures.append(
                    f"{s['tag']}({s['selector']}): {ratio:.2f}:1 "
                    f"(fg={s['fg']}, bg={s['bg']})"
                )

        if failures:
            return {
                "name": name,
                "passed": False,
                "detail": f"Low contrast: {'; '.join(failures)}",
            }
        return {
            "name": name,
            "passed": True,
            "detail": f"All sampled text >= {WCAG_AA_RATIO}:1 (min {min_ratio:.2f}:1)",
        }
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"Error: {exc}"}


# ---------------------------------------------------------------------------
# Vision scoring (Layer 2, data-collection only)
# ---------------------------------------------------------------------------

def _score_vision(screenshots: dict, spec: dict, cancel_key: Optional[str] = None) -> dict:
    """Send a screenshot to Claude Vision for a design quality rating.

    Returns {"vision_score": float|None, "vision_details": dict|None}.
    Non-blocking: catches all exceptions and returns None scores on failure.
    """
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return {"vision_score": None, "vision_details": None}

        # Pick the best screenshot (prefer dashboard, then first available)
        screenshot_path = None
        for name in ("dashboard", "landing", "login"):
            if name in screenshots:
                screenshot_path = screenshots[name]
                break
        if screenshot_path is None and screenshots:
            screenshot_path = next(iter(screenshots.values()))
        if screenshot_path is None:
            return {"vision_score": None, "vision_details": None}

        # Read and encode screenshot
        with open(screenshot_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")

        app_name = spec.get("app_name", "unknown app")
        style = spec.get("design", {}).get("style", "modern")

        client = make_client(api_key=api_key)
        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_data,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"Rate this web app ({app_name}, {style} style) "
                        "on design quality from 1-10. Consider: visual hierarchy, "
                        "spacing consistency, color harmony, typography, and overall "
                        "professional feel. Respond with ONLY a JSON object:\n"
                        '{"score": <number>, "strengths": ["..."], "weaknesses": ["..."]}'
                    ),
                },
            ],
        }]
        # Vision scoring goes through api_client.call_api so the explicit
        # httpx.Timeout, wall-clock budget, cooperative cancellation, and cost
        # metrics apply uniformly. No `system=` param here: the vision call
        # has no system prompt. call_api takes the attempt's cancel key as its
        # `build_id` (it looks the cancel event up by it and labels its log
        # lines with it); an empty string means "no supervisor to listen to".
        response, _metrics = call_api(
            client,
            messages,
            VISION_MODEL,
            max_tokens=1024,
            module="ui_test",
            build_id=cancel_key or "",
        )

        response_text = response.content[0].text if response.content else ""
        parsed = _parse_vision_score(response_text)
        if parsed and "score" in parsed:
            return {
                "vision_score": float(parsed["score"]),
                "vision_details": parsed,
            }
        return {"vision_score": None, "vision_details": None}
    except Exception:
        return {"vision_score": None, "vision_details": None}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _compute_score(results: list) -> float:
    """Compute weighted score (0-10) from programmatic check results."""
    total = 0.0
    for r in results:
        weight = CHECK_WEIGHTS.get(r["name"], 0)
        if r["passed"]:
            total += weight
    return round(total, 1)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_ui_tests(
    build_dir: str,
    spec: dict,
    port: int = UI_TEST_PORT,
    app_url: Optional[str] = None,
    *,
    cancel_key: Optional[str] = None,
) -> dict:
    """Run browser-based visual quality tests on a generated app.

    Args:
        build_dir: Path to the build directory containing the generated app.
        spec: The app spec dict.
        port: Port to run the app on (default 4569).
        app_url: Optional URL of an already-running app instance. If provided,
                 skips app lifecycle management (start/stop).
        cancel_key: Key of the attempt's cancel event, passed on to the model
                 call that scores the screenshots.

    Returns:
        Dict with keys: passed, score, programmatic_results, vision_score,
        vision_details, screenshots, error.
    """
    error_result = {
        "passed": False,
        "score": 0.0,
        "programmatic_results": [],
        "vision_score": None,
        "vision_details": None,
        "screenshots": {},
        "error": None,
    }

    process = None
    db_name = None
    browser = None
    pw = None
    manage_app = app_url is None
    base_url = app_url or f"http://localhost:{port}"

    try:
        # Lazy imports to avoid loading Playwright/test.py at module level
        from playwright.sync_api import sync_playwright
        from test import (
            install_deps,
            setup_database,
            start_app,
            stop_app,
            wait_for_app,
            teardown_database,
            STARTUP_TIMEOUT_SECONDS,
        )

        build_path = Path(build_dir)

        # Start app if not provided externally
        if manage_app:
            success, err, test_dir = install_deps(str(build_path))
            if not success:
                error_result["error"] = f"Install failed: {err}"
                return error_result

            # run_dir: /tmp test dir on Railway (source + node_modules), or build_path locally.
            # setup_database reads db/schema.sql from the ORIGINAL build_path (Path-typed),
            # while start_app runs the node process from run_dir so node_modules resolve.
            run_dir = Path(test_dir) if test_dir is not None else build_path
            app_name = spec.get("app_name", "test-app")
            db_name = f"test_ui_{app_name.replace('-', '_')}"

            if not setup_database(build_path, db_name):
                error_result["error"] = "Database setup failed"
                teardown_database(db_name)
                db_name = None
                return error_result

            process = start_app(run_dir, db_name, port)
            if not wait_for_app(port):
                error_result["error"] = "App failed to start"
                if process:
                    stop_app(process)
                teardown_database(db_name)
                db_name = None
                return error_result

        # Launch Playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Navigate to login page first
        page.goto(f"{base_url}/login", wait_until="networkidle", timeout=15000)

        # Capture screenshots
        screenshots = {}
        screenshot_dir = build_path / "screenshots" / "ui_test"
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        login_path = screenshot_dir / "login.png"
        page.screenshot(path=str(login_path))
        screenshots["login"] = str(login_path)

        # Try to authenticate
        try:
            page.fill('input[name="email"]', spec.get("admin_email", "admin@example.com"))
            page.fill('input[name="password"]', "test_password_for_ci")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=10000)

            dashboard_path = screenshot_dir / "dashboard.png"
            page.screenshot(path=str(dashboard_path))
            screenshots["dashboard"] = str(dashboard_path)
        except Exception:
            pass  # Auth failure is non-blocking

        # Run programmatic checks
        results = []
        checks = [
            _check_nav_dark_bg,
            _check_body_not_flat_white,
            _check_cards_border_radius,
            _check_buttons_visible_bg,
            _check_google_fonts,
            _check_viewport_meta,
            _check_gradient_classes,
            _check_text_contrast,
        ]
        for check_fn in checks:
            try:
                result = check_fn(page)
                results.append(result)
            except Exception as exc:
                results.append({
                    "name": check_fn.__name__.lstrip("_check_"),
                    "passed": False,
                    "detail": f"Unexpected error: {exc}",
                })

        score = _compute_score(results)

        # Check if contrast failure should hard-fail the stage
        contrast_failed = any(
            r["name"] == "text_contrast" and not r["passed"] for r in results
        )
        blocking_contrast = os.environ.get("BLOCKING_UI_TEST_CONTRAST", "").lower() == "true"

        # Vision scoring (Layer 2, non-blocking)
        vision_result = _score_vision(screenshots, spec, cancel_key)

        passed = score >= PASS_THRESHOLD
        if contrast_failed and blocking_contrast:
            passed = False

        return {
            "passed": passed,
            "score": score,
            "programmatic_results": results,
            "contrast_failed": contrast_failed,
            "vision_score": vision_result.get("vision_score"),
            "vision_details": vision_result.get("vision_details"),
            "screenshots": screenshots,
            "error": None,
        }

    except Exception as exc:
        error_result["error"] = str(exc)
        return error_result

    finally:
        # Cleanup: browser, app, database
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
        if manage_app:
            if process:
                try:
                    from test import stop_app
                    stop_app(process)
                except Exception:
                    pass
            if db_name:
                try:
                    from test import teardown_database
                    teardown_database(db_name)
                except Exception:
                    pass
