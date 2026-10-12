#!/usr/bin/env python3
"""Regenerate Playwright screenshots for the /lighthouse/preview picker.

For each of the 5 template families in ``compose/data/templates.json``,
this script:

  1. Loads a representative archetype's sample spec from
     ``compose/fixtures/``.
  2. Overrides ``template_family_id`` (and the archetype, when needed
     for compatibility) so compose emits the family's shell + nav +
     footer + typography scale.
  3. Runs ``compose.build.build_app()`` to write the generated app to a
     temp dir.
  4. Boots the generated app on a per-family port (``BASE_PORT + i``)
     against an isolated throwaway Postgres database, waits for
     ``/health`` to answer, then uses Playwright to screenshot
     ``/``, ``/about``, and ``/services`` (or each family's
     equivalent landing route: falls back to ``/`` only when the
     other routes 404).
  5. Persists each screenshot to
     ``web/public/template-thumbnails/<family-id>/{home,about,services}.png``
     so the EJS picker can render real renders instead of CSS mocks.
  6. Tears down the server + DB.

Pre-requisites:

  - ``node`` and ``npm`` on PATH; one warm ``node_modules`` cache (the
    script re-uses ``builds/sample-healer/node_modules`` if present, the
    same convention as ``compose/tests/test_compose_end_to_end.py``).
  - ``psql`` + ``createdb`` on PATH (a local Postgres).
  - ``playwright`` installed in the active Python env, with the
    chromium browser fetched (``playwright install chromium``).

This is a maintenance script, not a CI test. It is intentionally
conservative: any family that fails to compose / boot / answer is
skipped with a printed warning, and the previous PNGs (if any) are
kept. Re-run after a template-family or shell-partial change.

Usage:
    python3 scripts/regen-template-thumbnails.py
    python3 scripts/regen-template-thumbnails.py --only editorial-statement
    python3 scripts/regen-template-thumbnails.py --base-port 4810
    python3 scripts/regen-template-thumbnails.py --keep-tmp  # leave temp dirs for debug
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_JSON = REPO_ROOT / "compose" / "data" / "templates.json"
FIXTURES_DIR = REPO_ROOT / "compose" / "fixtures"
THUMBNAILS_DIR = REPO_ROOT / "web" / "public" / "template-thumbnails"
SAMPLE_NODE_MODULES = REPO_ROOT / "builds" / "sample-healer" / "node_modules"
DEFAULT_BASE_PORT = 4810
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
DEFAULT_THUMB_VIEWPORT = {"width": 1024, "height": 720}

# (family_id, fixture_basename, archetype_override). archetype_override
# is None when the fixture's archetype is already in the family's
# compatible_archetypes list. Otherwise we swap the archetype field
# of the spec dict to the first compatible one before composing; the
# fixture's content stays the same; the family's shell + typography
# is what we're showcasing in the thumbnail.
FAMILY_FIXTURES = [
    ("classic-business", "sample-coach-spec.json", None),
    ("editorial-statement", "sample-healer-spec.json", "healer-wellness"),
    ("directory-rich", "sample-tradesperson-spec.json", "tradesperson-contractor"),
    ("simple-statement", "sample-healer-spec.json", "healer-wellness"),
    ("event-led", "sample-event-producer-spec.json", "event-producer"),
]

# Routes to screenshot per family. /about + /services are the most
# canonical "supporting" pages; many archetypes also have
# /book / /menu / /events but we keep this list short so the picker
# UI stays consistent across families.
ROUTES_TO_SCREENSHOT = [
    ("home", "/"),
    ("about", "/about"),
    ("services", "/services"),
]


def load_families() -> List[dict]:
    with open(TEMPLATES_JSON) as f:
        return json.load(f)["templates"]


def build_spec_dict(fixture_path: Path, family_id: str, archetype_override: Optional[str]) -> dict:
    """Return a spec dict ready for ``compose.build.build_app``.

    We deep-copy the on-disk fixture and stamp ``template_family_id`` +
    optionally swap ``archetype``. Other fields (settings, pages,
    seed_content, palette, font_pair) are preserved so the thumbnail
    looks like a real keeper site, not a barren skeleton.
    """
    with open(fixture_path) as f:
        spec = json.load(f)
    spec["template_family_id"] = family_id
    if archetype_override:
        spec["archetype"] = archetype_override
    # Stamp app_name with family suffix so rebuilds don't collide.
    spec["app_name"] = f"thumbnail-{family_id}"
    return spec


def compose_to_dir(spec: dict, out_dir: Path) -> None:
    """Run compose.build.build_app on the in-memory spec."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from compose.build import build_app  # type: ignore
    finally:
        sys.path.pop(0)
    build_app(spec, output_dir=out_dir)


def link_node_modules(app_dir: Path) -> bool:
    """Symlink the cached sample-healer node_modules into ``app_dir``.

    Returns True on success. Returns False (with a printed warning) if
    the cache is missing: caller should skip the family.
    """
    if not SAMPLE_NODE_MODULES.exists():
        print(f"  [skip] cached node_modules missing at {SAMPLE_NODE_MODULES}")
        print("         compose a sample build first:")
        print("         python -m compose.build compose/fixtures/sample-healer-spec.json")
        return False
    nm_target = app_dir / "node_modules"
    if nm_target.exists():
        if nm_target.is_symlink():
            nm_target.unlink()
        else:
            shutil.rmtree(nm_target)
    nm_target.symlink_to(SAMPLE_NODE_MODULES.resolve())
    return True


def boot_server(app_dir: Path, port: int, db_name: str) -> Optional[subprocess.Popen]:
    """Boot ``node server.js`` in ``app_dir`` against ``db_name`` on ``port``.

    Returns the Popen handle on success, None on boot failure.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = f"postgresql://{env.get('USER', 'postgres')}@localhost:5432/{db_name}"
    env["SESSION_SECRET"] = f"thumbnail_secret_{port}"
    env["PORT"] = str(port)
    env["NODE_ENV"] = "development"
    env["ADMIN_EMAIL"] = "admin@thumbnails.local"

    log_path = app_dir / "server.log"
    log_f = open(log_path, "w")
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=app_dir,
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )

    import urllib.request
    for _ in range(60):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=1) as r:  # nosec B310  # fixed http://localhost:<port>/health URL of the app this script just started
                if r.status == 200:
                    return proc
        except Exception:
            continue

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    log_f.close()
    print(f"  [skip] server failed to boot on port {port}; tail of {log_path}:")
    if log_path.exists():
        print("    " + "\n    ".join(log_path.read_text().splitlines()[-15:]))
    return None


def screenshot_routes(port: int, out_dir: Path, viewport: dict) -> List[str]:
    """Screenshot each route in ROUTES_TO_SCREENSHOT to ``out_dir``.

    Returns a list of saved route names (e.g. ['home', 'about']). Routes
    that 404 / 5xx are skipped silently.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("  [skip] playwright not installed: pip install playwright && playwright install chromium")
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: List[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        for name, path in ROUTES_TO_SCREENSHOT:
            try:
                resp = page.goto(f"http://localhost:{port}{path}", wait_until="networkidle", timeout=10000)
                if resp is None or resp.status >= 400:
                    print(f"  [warn] {path} -> {resp.status if resp else '?'} (skipped)")
                    continue
                page.wait_for_timeout(400)
                target = out_dir / f"{name}.png"
                page.screenshot(path=str(target), full_page=False)
                saved.append(name)
                print(f"  [ok] {path} -> {target.relative_to(REPO_ROOT)}")
            except Exception as e:
                print(f"  [warn] {path} screenshot failed: {e}")
        browser.close()
    return saved


def teardown(proc: subprocess.Popen, db_name: str) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    subprocess.run(["dropdb", db_name], capture_output=True)


def regen_one(family_id: str, fixture_basename: str, archetype_override: Optional[str], port: int, keep_tmp: bool, viewport: dict) -> bool:
    """Regenerate thumbnails for a single family. Returns True on success."""
    print(f"\n=== {family_id} ===")
    fixture_path = FIXTURES_DIR / fixture_basename
    if not fixture_path.exists():
        print(f"  [skip] fixture missing: {fixture_path}")
        return False
    spec = build_spec_dict(fixture_path, family_id, archetype_override)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"thumbnail-{family_id}-"))
    try:
        app_dir = tmp_dir / "app"
        compose_to_dir(spec, app_dir)
        if not link_node_modules(app_dir):
            return False
        db_name = f"cas_thumbnail_{family_id.replace('-', '_')}"
        subprocess.run(["dropdb", db_name], capture_output=True)
        r = subprocess.run(["createdb", db_name], capture_output=True)
        if r.returncode != 0:
            print(f"  [skip] createdb {db_name} failed: {r.stderr.decode()}")
            return False
        proc = boot_server(app_dir, port, db_name)
        if proc is None:
            subprocess.run(["dropdb", db_name], capture_output=True)
            return False
        try:
            saved = screenshot_routes(port, THUMBNAILS_DIR / family_id, viewport)
            return len(saved) > 0
        finally:
            teardown(proc, db_name)
    finally:
        if not keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            print(f"  [keep-tmp] left compose output at {tmp_dir}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="regen only this family id (e.g. editorial-statement)")
    ap.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT, help=f"port for first family (default {DEFAULT_BASE_PORT}); subsequent families increment")
    ap.add_argument("--keep-tmp", action="store_true", help="keep compose temp dirs for debugging")
    ap.add_argument("--width", type=int, default=DEFAULT_THUMB_VIEWPORT["width"])
    ap.add_argument("--height", type=int, default=DEFAULT_THUMB_VIEWPORT["height"])
    args = ap.parse_args()
    viewport = {"width": args.width, "height": args.height}

    THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)
    families_on_disk = {f["id"] for f in load_families()}
    targets = [(fid, fix, arch) for fid, fix, arch in FAMILY_FIXTURES if fid in families_on_disk]
    if args.only:
        targets = [t for t in targets if t[0] == args.only]
        if not targets:
            print(f"ERROR: no family matches --only={args.only}")
            return 2

    successes = 0
    for i, (family_id, fixture, arch) in enumerate(targets):
        port = args.base_port + i
        ok = regen_one(family_id, fixture, arch, port, args.keep_tmp, viewport)
        if ok:
            successes += 1
    print(f"\nDone. {successes}/{len(targets)} families regenerated.")
    return 0 if successes == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
