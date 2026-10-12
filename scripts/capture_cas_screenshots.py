#!/usr/bin/env python3
"""Capture full-page screenshots of key CAS pages.

Uses Playwright to render 5 key CAS pages and save as PNG files.
Used by promote_staging.sh for visual regression testing.

Usage (base URLs come from CAS_PROD_URL / CAS_STAGING_URL; there is no default host):
    python3 scripts/capture_cas_screenshots.py --env production --output /tmp/vr-baseline/
    python3 scripts/capture_cas_screenshots.py --env staging --output /tmp/vr-candidate/
"""
import argparse
import os
import sys
from pathlib import Path

PROD_URL = os.environ.get("CAS_PROD_URL", "")
STAGING_URL = os.environ.get("CAS_STAGING_URL", "")

PAGES = [
    ("homepage", "/"),
    ("pricing", "/pricing"),
    ("gallery", "/gallery"),
    ("login", "/login"),
    ("how-it-works", "/how-it-works"),
]

VIEWPORT = {"width": 1280, "height": 800}


def capture(env: str, output_dir: str) -> dict:
    """Capture screenshots of key CAS pages.

    Returns: {"success": bool, "pages": [{"name", "path", "error"}]}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
        return {"success": False, "pages": []}

    base = PROD_URL if env == "production" else STAGING_URL
    if not base:
        print(f"ERROR: No URL configured for {env}")
        return {"success": False, "pages": []}

    base = base.rstrip("/")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=VIEWPORT)
        page = context.new_page()

        for name, path in PAGES:
            url = f"{base}{path}"
            screenshot_path = out / f"{name}.png"
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.screenshot(path=str(screenshot_path), full_page=True)
                results.append({"name": name, "path": str(screenshot_path), "error": None})
                print(f"  Captured: {name} -> {screenshot_path}")
            except Exception as e:
                results.append({"name": name, "path": None, "error": str(e)})
                print(f"  FAILED: {name} - {e}")

        browser.close()

    success = all(r["error"] is None for r in results)
    return {"success": success, "pages": results}


def main():
    parser = argparse.ArgumentParser(description="Capture CAS page screenshots")
    parser.add_argument("--env", choices=["production", "staging"], default="production")
    parser.add_argument("--output", required=True, help="Output directory for PNG files")
    args = parser.parse_args()

    result = capture(args.env, args.output)
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
