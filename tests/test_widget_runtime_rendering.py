"""Node-subprocess render harness: spawn `node` with EJS loaded from
web/node_modules (or a build's node_modules), feed it every widget-emitted .ejs
from the canonical MW emit_all output, and assert no ReferenceError or
TypeError is raised during render.

This test is the FINAL LINE OF DEFENSE against the v21-class bug:
widget output that looks correct statically but crashes at EJS render
time because a helper or local identifier is not in scope. Static
scans (cli/contracts/widget_helpers.py) catch undeclared references;
this harness catches subtler runtime issues a static scan can miss
(type coercion, missing properties on the locals object, etc.).

The harness skips cleanly when node or an ejs install is unavailable
(a checkout where `cd web && npm ci` has not been run).
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _find_ejs_install() -> str | None:
    """Locate an installed `ejs` package: web/node_modules (`cd web && npm ci`)
    first, then builds/*/node_modules. Returns the node_modules path (suitable
    for NODE_PATH) or None."""
    web_modules = REPO / "web" / "node_modules"
    if (web_modules / "ejs" / "package.json").is_file():
        return str(web_modules)
    builds_dir = REPO / "builds"
    if not builds_dir.is_dir():
        return None
    for build in builds_dir.iterdir():
        ejs_pkg = build / "node_modules" / "ejs" / "package.json"
        if ejs_pkg.is_file():
            return str(build / "node_modules")
    return None


def _node_available() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


NODE_MODULES_PATH = _find_ejs_install()

# A list, not a second `pytestmark =` assignment: that would silently drop the
# v1_only mark set at the top of this file.
pytestmark = [
    pytest.mark.v1_only,
    pytest.mark.skipif(
        not _node_available() or NODE_MODULES_PATH is None,
        reason=(
            "needs node and the ejs package: run `cd web && npm ci` (or npm "
            "install a generated app under builds/)"
        ),
    ),
]


def _render_ejs_via_node(source: str, locals_dict: dict) -> tuple[int, dict, str]:
    """Render EJS in a node subprocess under a neighboring build's
    node_modules. Returns (exit_code, parsed_stdout_dict, stderr)."""
    harness = (
        "const ejs = require('ejs');\n"
        f"const source = {json.dumps(source)};\n"
        f"const locals = {json.dumps(locals_dict)};\n"
        "try {\n"
        "    const rendered = ejs.render(source, locals);\n"
        "    process.stdout.write(JSON.stringify({ status: 'ok', length: rendered.length }));\n"
        "} catch (e) {\n"
        "    process.stdout.write(JSON.stringify({\n"
        "        status: 'error',\n"
        "        name: String(e.name || 'Error'),\n"
        "        message: String(e.message || e)\n"
        "    }));\n"
        "    process.exit(1);\n"
        "}\n"
    )
    env_node_path = NODE_MODULES_PATH or ""
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "NODE_PATH": env_node_path,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    out = result.stdout.strip()
    try:
        parsed = json.loads(out) if out else {}
    except json.JSONDecodeError:
        parsed = {"status": "parse_error", "raw": out[:500]}
    return result.returncode, parsed, result.stderr


def _mock_locals() -> dict:
    """Minimum-viable locals dict Claude route handlers typically pass.
    Provides a row under each common singular name so alias fall-through
    has something to resolve to."""
    row = {
        "id": 1,
        "title": "Test Title",
        "description": "Test description",
        "date_time": "2026-05-13T00:00:00Z",
        "location": "Town Hall",
        "image_url": "/img.jpg",
        "embed_url": "https://youtu.be/dQw4w9WgXcQ",
        "rsvp_link": "https://tickets.example/e/x",
        "posh_embed_url": "",
        "posh_embed_height": 700,
        "is_featured": False,
    }
    return {
        "csrfToken": "test-csrf",
        "title": "Edit Test",
        "error": None,
        "message": None,
        "item": row,
        "event": row,
        "photo": row,
        "video": row,
        "contact_message": row,
        "contactmessage": row,
        "settings": {
            "site_name": "MW",
            "hero_tagline": "Test",
            "hero_image_url": "/hero.jpg",
            "about_title": "About",
        },
    }


def _emit_canonical_artifact() -> dict:
    """Run emit_all on a canonical-ish MW spec and return
    dict[path, content] of widget-emitted files only."""
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "cli"))
    from cli.widgets.forms import emit_all, WIDGET_ADMIN_MARKER

    spec = {
        "app_name": "medicine-waves",
        "description": "Medicine Waves",
        "data_models": [
            {"name": "Event", "fields": [
                {"name": "title", "type": "text"},
                {"name": "description", "type": "text"},
                {"name": "date_time", "type": "timestamp"},
                {"name": "location", "type": "text"},
                {"name": "image_url", "type": "image_url"},
            ]},
            {"name": "Photo", "fields": [
                {"name": "title", "type": "text"},
                {"name": "image_url", "type": "image_url"},
            ]},
            {"name": "Video", "fields": [
                {"name": "title", "type": "text"},
                {"name": "embed_url", "type": "url"},
            ]},
        ],
        "settings": {
            "site_name": "MW",
            "hero_image_url": "/hero.jpg",
            "hero_tagline": "Test",
        },
    }
    artifact = types.SimpleNamespace(
        files={}, spec=spec, build_dir="/tmp/x",
        widgets_snippets={}, metadata={},
    )
    emit_all(artifact)
    files = artifact.files if isinstance(artifact.files, dict) else dict(artifact.files)
    return {p: c for p, c in files.items()
            if isinstance(c, str) and WIDGET_ADMIN_MARKER in c}


def test_every_widget_emitted_admin_file_renders_without_error():
    """Every widget-owned .ejs from emit_all on a canonical MW spec
    must render under node+ejs with minimum-viable locals, without
    ReferenceError / TypeError.

    This test would have failed at pytest time on v21 for both
    `_dt is not defined` and `item is not defined`. With Phase 1-3
    (declared required_helpers + helper registry + widget_helpers
    contract) in place, widget output includes the necessary var
    defs; this test verifies the runtime actually accepts them."""
    widget_files = _emit_canonical_artifact()
    assert widget_files, "emit_all produced no widget-marked files"

    locals_dict = _mock_locals()
    failures = []
    for path, content in sorted(widget_files.items()):
        code, parsed, stderr = _render_ejs_via_node(content, locals_dict)
        if code != 0 or parsed.get("status") != "ok":
            failures.append(
                f"{path}: status={parsed.get('status')} "
                f"name={parsed.get('name')} message={parsed.get('message')} "
                f"stderr={stderr[:200]}"
            )

    assert not failures, (
        "widget-emitted .ejs files failed to render under node+ejs:\n  "
        + "\n  ".join(failures)
    )


def test_dt_helper_actually_defined_in_widget_files_that_use_it():
    """Positive smoke: datetime/timestamp fields in the spec MUST cause
    _dt helper definition to appear in the rendered file. Confirms the
    Phase 1 required_helpers plumbing runs end-to-end."""
    files = _emit_canonical_artifact()
    # Event has date_time (timestamp) → its new.ejs + edit.ejs must
    # both include _dt helper definition.
    for p in ["views/admin/events/new.ejs", "views/admin/events/edit.ejs"]:
        content = files.get(p)
        assert content, f"emit_all did not produce {p}"
        assert "var _dt = function" in content, (
            f"{p}: required _dt helper definition missing"
        )


def test_item_alias_present_in_edit_files():
    """Every widget-emitted edit.ejs must carry the `item` alias block
    so route handlers that pass `event:` / `photo:` / `video:` still
    populate `item.X` property references."""
    files = _emit_canonical_artifact()
    for model_dir in ["events", "photos", "videos"]:
        p = f"views/admin/{model_dir}/edit.ejs"
        content = files.get(p)
        assert content, f"emit_all did not produce {p}"
        assert "var item =" in content
        # Lowercase-singular fall-through must be present (event/photo/video)
        singular = model_dir[:-1]  # events -> event
        assert f"typeof {singular}" in content, (
            f"{p}: item alias missing `{singular}` fall-through"
        )
