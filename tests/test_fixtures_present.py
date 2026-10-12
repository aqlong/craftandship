"""Section-01 fixture-presence tests.

Confirms every file shipped by section-01-fixtures is on disk and shaped
correctly before later sections consume them. 16 assertions total, one per
acceptance bullet for the fixtures listed in the file layout of
docs/plans/b-plus-c/claude-plan.md.

Bodies are deliberately trivial (path.exists, json.loads, filecmp.cmp,
hashlib.sha256), section-05/06/07 own the tests that actually exercise
fixture contents.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import filecmp
import hashlib
import json
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
GOLDEN = REPO_ROOT / "tests" / "golden"
CANONICAL = GOLDEN / "medicine-waves-canonical"

EJS_FIXTURES = [
    FIXTURES / "v12_admin_settings.ejs",
    FIXTURES / "v14_admin_settings.ejs",
    FIXTURES / "v18_event_form.ejs",
    FIXTURES / "v19_admin_dashboard.ejs",
]
CLAUDE_JSON = FIXTURES / "claude_mw_v19_raw.json"
CLAUDE_SHA = FIXTURES / "claude_mw_v19_raw.sha256"
GOLDEN_SPEC = CANONICAL / "spec.json"
GOLDEN_README = CANONICAL / "README.md"
GOLDEN_LOGO = GOLDEN / "mw-logo.png"
SOURCE_LOGO = REPO_ROOT / "cli" / "assets" / "mw_logo.png"


def test_v12_admin_settings_ejs_exists_and_nonempty():
    assert EJS_FIXTURES[0].exists()
    assert EJS_FIXTURES[0].stat().st_size > 0


def test_v14_admin_settings_ejs_exists_and_nonempty():
    assert EJS_FIXTURES[1].exists()
    assert EJS_FIXTURES[1].stat().st_size > 0


def test_v18_event_form_ejs_exists_and_nonempty():
    assert EJS_FIXTURES[2].exists()
    assert EJS_FIXTURES[2].stat().st_size > 0


def test_v19_admin_dashboard_ejs_exists_and_nonempty():
    assert EJS_FIXTURES[3].exists()
    assert EJS_FIXTURES[3].stat().st_size > 0


def test_every_ejs_fixture_has_ejs_tags():
    for path in EJS_FIXTURES:
        body = path.read_text(encoding="utf-8")
        assert "<%" in body and "%>" in body, f"{path.name} has no EJS tags"


def test_claude_mw_v19_raw_is_valid_json():
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_claude_mw_v19_raw_top_level_keys():
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"_provenance", "spec", "files"}


def test_claude_mw_v19_raw_files_has_key_views():
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    for key in ("views/admin/settings.ejs", "views/home.ejs", "views/layout.ejs"):
        assert key in data["files"], f"missing file key: {key}"


def test_claude_mw_v19_raw_captured_at_is_iso8601():
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    captured_at = data["_provenance"]["captured_at"]
    # Accept "Z" suffix by swapping to "+00:00" for fromisoformat compatibility.
    datetime.fromisoformat(captured_at.replace("Z", "+00:00"))


def test_claude_mw_v19_raw_sha256_pin_matches():
    recorded = CLAUDE_SHA.read_text(encoding="utf-8").strip()
    digest = hashlib.sha256(CLAUDE_JSON.read_bytes()).hexdigest()
    assert recorded == digest


def test_golden_spec_is_valid_json():
    data = json.loads(GOLDEN_SPEC.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_golden_spec_passes_validate_spec():
    from cli.validate import validate_spec
    data = json.loads(GOLDEN_SPEC.read_text(encoding="utf-8"))
    errors = validate_spec(data)
    assert errors == [], f"validate_spec returned errors: {errors}"


def test_golden_spec_has_mw_archetype_marker():
    data = json.loads(GOLDEN_SPEC.read_text(encoding="utf-8"))
    desc = data.get("description", "")
    app_name = data.get("app_name", "")
    assert "Medicine Waves" in desc or app_name.startswith("medicine-waves")


def test_golden_spec_has_image_url_field():
    data = json.loads(GOLDEN_SPEC.read_text(encoding="utf-8"))
    hits = [
        field
        for model in data.get("data_models", [])
        for field in model.get("fields", [])
        if field.get("name") == "image_url"
    ]
    assert hits, "no data_model field named image_url"


def test_golden_readme_exists():
    assert GOLDEN_README.exists()
    assert GOLDEN_README.stat().st_size > 0


def test_golden_logo_byte_identical_to_source():
    assert GOLDEN_LOGO.exists()
    assert SOURCE_LOGO.exists()
    assert filecmp.cmp(str(SOURCE_LOGO), str(GOLDEN_LOGO), shallow=False)
