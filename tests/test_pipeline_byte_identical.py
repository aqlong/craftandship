"""Byte-identical acceptance guards for section-02-foundation.

These are the load-bearing tests for section 02. They prove that replacing
the explicit post-processor call blocks with run_pipeline() leaves the
pipeline output unchanged to the byte on a canonical MW spec fixture.

The "before" snapshot is pinned in tests/fixtures/ as sha256-maps generated
against cli/build.py at the commit right before section 02 landed. The
"after" snapshot is computed here by running build_app (or
reapply_post_processors) under a Claude stub and hashing the result.

If either of these tests fails after a pipeline change, something in the
DAG ordering is different from the old explicit call sequence. Investigate
the diff before committing.
"""

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Allow importing from cli/ without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))


FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_RAW = FIXTURES / "claude_mw_v19_raw.json"
BUILD_BASELINE = FIXTURES / "pipeline_output_pre_s02.json"
REAPPLY_BASELINE = FIXTURES / "reapply_output_pre_s02.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_raw_fixture():
    """Load the pinned Claude-output fixture (spec + files) from section-01."""
    if not CLAUDE_RAW.exists():
        pytest.skip("claude_mw_v19_raw.json fixture not present")
    return json.loads(CLAUDE_RAW.read_text())


def _hash_dir(dir_path: Path) -> dict[str, str]:
    """Build a {relpath: sha256hex} map over every file in dir_path.

    Excludes .build-trace.jsonl because it contains per-run timestamps
    (verified non-deterministic during baseline capture).
    """
    result: dict[str, str] = {}
    for file_path in sorted(dir_path.rglob("*")):
        if not file_path.is_file():
            continue
        rel = str(file_path.relative_to(dir_path))
        if rel == ".build-trace.jsonl":
            continue
        result[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# build_app byte-identical
# ---------------------------------------------------------------------------


def test_build_app_byte_identical_on_canonical_mw():
    """build_app() must produce the same sha256-map on the canonical MW
    fixture as it did before section-02 landed.

    Uses the _claude_response_override seam (added in section 02) to skip
    the live Claude call and feed in the pinned Claude output captured on
    HEAD~1. Validates both that the post-processor pipeline still runs
    every processor and that the overall order produces byte-identical
    output.
    """
    if not BUILD_BASELINE.exists():
        pytest.skip("tests/fixtures/pipeline_output_pre_s02.json (hash map of the pre-DAG pipeline output) is not part of this repository")

    raw = _load_raw_fixture()
    spec = raw["spec"]
    files_list = [(path, content) for path, content in raw["files"].items()]

    tmpdir = Path(tempfile.mkdtemp(prefix="s02_build_byte_identical_"))
    try:
        spec_path = tmpdir / "spec.json"
        spec_path.write_text(json.dumps(spec))
        build_out = tmpdir / "build_out"
        build_out.mkdir()

        import build as build_mod  # noqa: E402

        # Stub completeness validator + template pre-load so the test does
        # not require every required file to be in the fixture.
        with patch.object(build_mod, "validate_completeness", lambda files: []), \
             patch.object(build_mod, "_load_templates", lambda: {}):
            build_mod.build_app(
                str(spec_path),
                output_dir=str(build_out),
                tier="build",
                _claude_response_override=files_list,
            )

        actual = _hash_dir(build_out)
        expected = json.loads(BUILD_BASELINE.read_text())

        assert actual == expected, _describe_diff(expected, actual)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# reapply_post_processors byte-identical
# ---------------------------------------------------------------------------


def test_reapply_post_processors_byte_identical_on_canonical_mw():
    """reapply_post_processors() must produce the same sha256-map on the
    canonical MW on-disk artifact as it did before section-02 landed."""
    if not REAPPLY_BASELINE.exists():
        pytest.skip("tests/fixtures/reapply_output_pre_s02.json (hash map of the pre-DAG reapply output) is not part of this repository")

    raw = _load_raw_fixture()
    spec = raw["spec"]

    tmpdir = Path(tempfile.mkdtemp(prefix="s02_reapply_byte_identical_"))
    try:
        # Seed the temp build dir with the raw pre-post-processor Claude output.
        for path, content in raw["files"].items():
            dest = tmpdir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                dest.write_text(content)
            else:
                dest.write_bytes(content)

        import build as build_mod  # noqa: E402

        build_mod.reapply_post_processors(str(tmpdir), spec)

        actual = _hash_dir(tmpdir)
        expected = json.loads(REAPPLY_BASELINE.read_text())

        assert actual == expected, _describe_diff(expected, actual)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Diff helper (cleaner failure messages)
# ---------------------------------------------------------------------------


def _describe_diff(expected: dict[str, str], actual: dict[str, str]) -> str:
    """Produce a readable summary of the first few differences."""
    expected_files = set(expected)
    actual_files = set(actual)
    missing = expected_files - actual_files
    extra = actual_files - expected_files
    changed = {path for path in expected_files & actual_files if expected[path] != actual[path]}

    parts: list[str] = []
    if missing:
        parts.append(f"Missing files (expected, not produced): {sorted(missing)[:5]}")
    if extra:
        parts.append(f"Extra files (produced, not expected): {sorted(extra)[:5]}")
    if changed:
        parts.append(f"Changed files (hash differs): {sorted(changed)[:5]}")
    if not parts:
        parts.append("No structural diff; check sha256 values directly.")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Seam validation
# ---------------------------------------------------------------------------


def test_claude_response_override_kwarg_accepts_list_of_tuples():
    """The test-only seam must preserve production behavior when unused
    and must bypass the live Claude call when provided."""
    raw = _load_raw_fixture()
    spec = raw["spec"]

    tmpdir = Path(tempfile.mkdtemp(prefix="s02_seam_smoke_"))
    try:
        spec_path = tmpdir / "spec.json"
        spec_path.write_text(json.dumps(spec))
        build_out = tmpdir / "out"
        build_out.mkdir()

        import build as build_mod  # noqa: E402

        # Setting the seam with an empty list short-circuits the pipeline
        # at the "No files were generated" guard, proving the override
        # IS the file source when supplied.
        with patch.object(build_mod, "validate_completeness", lambda files: []), \
             patch.object(build_mod, "_load_templates", lambda: {}):
            with pytest.raises(RuntimeError, match="No files were generated"):
                build_mod.build_app(
                    str(spec_path),
                    output_dir=str(build_out),
                    tier="build",
                    _claude_response_override=[],
                )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
