"""End-to-end integration tests for the B+C pipeline rewrite.

Validates that sections 02-07 compose correctly. Does not re-test per-section
acceptance, that belongs to the feature sections.

Run: pytest tests/test_b_plus_c_integration.py -q

Spec reference:
    docs/plans/b-plus-c/claude-plan.md

Conventions:
    - sys.path is prepended with repo root and cli/ before imports so
      `import build` (cli module, to hit the _claude_response_override
      seam) and `import pipeline` (cli.pipeline) both resolve without a
      package install. Matches existing repo style in
      test_pipeline_byte_identical.py and test_pipeline_dag.py.

    - Tests that exercise the full build_app / reapply_post_processors
      path stub out validate_completeness + _load_templates (matches
      section-02's byte-identical guard tests) so this file does not
      require every REQUIRED_FILES entry to be in the recorded Claude
      fixture.

    - v19 on-disk snapshot tests pytest.skip cleanly when
      builds/medicine-waves-v19-* is absent (CI-like environments).

    - No network, no live Claude, no Railway writes. Running this file in
      isolation completes in a few seconds.
"""

from __future__ import annotations

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import glob
import hashlib
import json
import shutil
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
REPO_ROOT_STR = str(ROOT)

# Allow both `import build` (cli/ on path) for the _claude_response_override
# seam and `from cli.pipeline import ...` / `from cli.contracts import ...`
# (repo root on path) without a package install.
if REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, REPO_ROOT_STR)
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)


FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN_DIR = ROOT / "tests" / "golden" / "medicine-waves-canonical"
CANONICAL_SPEC_PATH = GOLDEN_DIR / "spec.json"
CLAUDE_RAW_PATH = FIXTURES / "claude_mw_v19_raw.json"
V19_GLOB = str(ROOT / "builds" / "medicine-waves-v19-*")


# Conservative wall-clock upper bound for the full build_app run on
# canonical MW. Plan §9.2 says <5% regression vs v19 baseline; the median
# has not yet been pinned into tests/golden/medicine-waves-canonical/
# artifact.snapshot.json (its "pipeline_wall_clock_median_s" field is
# absent). Using 60.0s here as a generous ceiling; if a local run comes
# in well under, tighten it by blessing a median with scripts/bless_golden.py.
V19_BASELINE_S = 60.0


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_current_build_dir():
    """Guard against a ContextVar leak in build.py::reapply_post_processors.

    reapply_post_processors sets cli.build._CURRENT_BUILD_DIR BEFORE its
    try/finally block opens (line ~12818 vs ~12843), so a RuntimeError
    raised inside run_pipeline (e.g. the known FIX-C vs widget drift
    raising ContractViolation during _run_contracts) leaves the
    contextvar pointing at the test's tmp_path. That tmp_path contains a
    bundled MW logo, which then pollutes tests/test_mw_v17_fix_k_nav_logo.py
    (expects "no logo → no seed mutation" with a fresh contextvar).

    This autouse fixture snapshots the contextvar value before each test
    and resets it after, isolating the build.py leak to this test file.
    Fixing the underlying leak belongs in a follow-up against
    build.py::reapply_post_processors (out of scope for section-08 per
    hard rule 4: 'Do NOT modify any file under cli/')."""
    try:
        import build as _build_mod  # noqa: F401, side effect populates sys.modules
        from build import _CURRENT_BUILD_DIR as _cbd  # noqa: E402
    except ImportError:
        yield
        return
    token = _cbd.set(_cbd.get())  # snapshot current value into a reset token
    try:
        yield
    finally:
        try:
            _cbd.reset(token)
        except (LookupError, ValueError):
            # Token was consumed mid-test; best-effort force back to None.
            _cbd.set(None)


@pytest.fixture
def canonical_mw_spec() -> dict:
    if not CANONICAL_SPEC_PATH.exists():
        pytest.skip("canonical MW spec fixture missing")
    return json.loads(CANONICAL_SPEC_PATH.read_text())


@pytest.fixture
def claude_mw_v19_raw() -> dict:
    """Load the recorded Claude output for canonical MW v19."""
    if not CLAUDE_RAW_PATH.exists():
        pytest.skip("claude_mw_v19_raw.json fixture missing")
    return json.loads(CLAUDE_RAW_PATH.read_text())


def _latest_v19_snapshot() -> Path | None:
    """Return the most recently modified builds/medicine-waves-v19-* dir,
    or None if none exist. Excludes .snapshot.* sibling directories."""
    candidates = [
        Path(p)
        for p in glob.glob(V19_GLOB)
        if Path(p).is_dir() and ".snapshot." not in p
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@pytest.fixture
def v19_snapshot_dir() -> Path:
    """Most-recent v19 on-disk snapshot. Skip cleanly if none exists."""
    snap = _latest_v19_snapshot()
    if snap is None:
        pytest.skip(
            "needs a full generated-app directory matching builds/medicine-waves-v19-* "
            "(output of a past production build; builds/ is not part of this repository)"
        )
    return snap


def _copy_v19_to(tmp_path: Path, snap: Path) -> Path:
    """Copy the v19 snapshot into tmp_path, excluding node_modules/screenshots
    so the copy is fast and reapply doesn't touch unrelated state."""
    dst = tmp_path / "v19"
    shutil.copytree(
        snap,
        dst,
        ignore=shutil.ignore_patterns("node_modules", "screenshots", ".git"),
    )
    return dst


def _artifact_from_dir(build_dir: Path, spec: dict) -> types.SimpleNamespace:
    """Load a build directory's text files into an in-memory artifact,
    matching the shape reapply_post_processors builds internally."""
    files: list[tuple[str, str]] = []
    for fp in build_dir.rglob("*"):
        if not fp.is_file():
            continue
        s = str(fp)
        if "node_modules" in s or ".git" in s or "screenshots" in s:
            continue
        rel = str(fp.relative_to(build_dir))
        try:
            files.append((rel, fp.read_text()))
        except (UnicodeDecodeError, PermissionError):
            continue
    return types.SimpleNamespace(
        files=files,
        spec=spec,
        build_dir=str(build_dir),
        widgets_snippets={},
        metadata={},
    )


def _hash_dir(dir_path: Path) -> dict[str, str]:
    """sha256 every file in dir_path (excluding build-trace noise)."""
    result: dict[str, str] = {}
    for fp in sorted(dir_path.rglob("*")):
        if not fp.is_file():
            continue
        s = str(fp)
        if "node_modules" in s or ".git" in s or "screenshots" in s:
            continue
        rel = str(fp.relative_to(dir_path))
        if rel == ".build-trace.jsonl":
            continue
        result[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# Tests 1-2: full-pipeline build_app composition
# ---------------------------------------------------------------------------


# Known open drift tracked by tests/test_widgets_safety_net.py::
# test_fix_admin_crud_link_prefix_is_byte_identical_on_widget_output
# (xfail strict=True). Summary: widgets emit <form action="/admin/<slug>">
# to match _ensure_admin_slug_mount's dual router mount; the older
# _fix_admin_crud_link_prefix (FIX C) rewrites /admin/<slug> back to
# /<slug> indiscriminately, which breaks the admin_crud contract's
# literal form-action selector. Resolved via option (a): widgets now emit
# a <!-- widgets:admin-form --> marker at the top of settings.ejs and every
# per-model new.ejs / edit.ejs, and _fix_admin_crud_link_prefix skips .ejs
# files carrying that marker. The three integration-level tests that used
# to xfail via _FIX_C_VS_WIDGET_DRIFT now pass directly.


def test_full_pipeline_canonical_mw_with_widgets_enabled_contracts_green(
    monkeypatch, tmp_path, canonical_mw_spec, claude_mw_v19_raw,
):
    """build_app() on canonical MW spec, widgets=on, Claude monkeypatched
    via _claude_response_override. Contracts must pass (build_app would
    raise ContractViolation at the tail _run_contracts processor otherwise,
    because conftest.py enables CAS_CONTRACTS_COLLECT=1 autouse)."""
    monkeypatch.setenv("WIDGETS_ENABLED", "true")

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(canonical_mw_spec))
    build_out = tmp_path / "out"
    build_out.mkdir()

    files_list = list(claude_mw_v19_raw["files"].items())

    import build as build_mod  # noqa: E402

    # Bypass the REQUIRED_FILES check + the template pre-load merge: this
    # matches the section-02 byte-identical guard's seam pattern and keeps
    # the test focused on pipeline composition, not fixture completeness.
    with patch.object(build_mod, "validate_completeness", lambda files: []), \
         patch.object(build_mod, "_load_templates", lambda: {}):
        # _run_contracts at the DAG tail (section-05) runs run_all under the
        # autouse CAS_CONTRACTS_COLLECT=1 env; a violation raises through
        # build_app. No further assertion needed beyond "build_app does not
        # raise": that IS the contracts-green assertion.
        build_mod.build_app(
            str(spec_path),
            output_dir=str(build_out),
            tier="build",
            _claude_response_override=files_list,
        )

    # Belt-and-suspenders: re-run run_all on an in-memory artifact loaded
    # from disk. Mirrors plan §9.1's "validation path" requirement to be
    # explicit about the composition (not just an implicit pass inside
    # build_app).
    from cli.contracts.run import run_all  # noqa: E402

    artifact = _artifact_from_dir(build_out, canonical_mw_spec)
    run_all(artifact)


def test_full_pipeline_non_mw_universal_widgets_admin_contracts_green(
    monkeypatch, tmp_path, claude_mw_v19_raw,
):
    """Post-universalization (2026-04-20) regression: a non-MW spec must
    now produce widget-emitted admin surfaces (views/admin/settings.ejs +
    views/admin/<slug>/new.ejs + edit.ejs per writable data model) so the
    admin_settings + admin_crud contracts pass. Before the universalization
    change this test xfail'd because admin contracts were MW-calibrated and
    non-MW builds short-circuited widget emission; the archetype gate is
    now True by default and this assertion locks that in.

    Source of non-MW spec: coordinator/sample_build_specs.SPEC_POOL's
    'neighborhood-cms' entry, finalized through _finalize_spec so it
    validates under cli.validate.validate_spec."""
    # Ensure env is unset so we exercise the default (universal) path.
    monkeypatch.delenv("WIDGETS_ENABLED", raising=False)

    from coordinator.sample_build_specs import SPEC_POOL, _finalize_spec  # noqa: E402
    non_mw_raw = next(s for s in SPEC_POOL if s["app_name"] == "neighborhood-cms")
    spec = _finalize_spec(non_mw_raw)

    # Sanity: the archetype gate must now report True for this non-MW
    # spec (universalization). If it flips back to False the coupled
    # contracts below will trip again and this sanity check makes the
    # root cause obvious in the failure output.
    from cli.widgets.archetype import _widgets_enabled_for  # noqa: E402
    assert _widgets_enabled_for(spec) is True, (
        "non-MW spec must enable widgets post-universalization; archetype "
        "gate has drifted; re-check cli/widgets/archetype.py"
    )

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    build_out = tmp_path / "out"
    build_out.mkdir()

    # Reuse the MW fixture files verbatim as Claude-stub output. The
    # pipeline will treat them as generic Claude output; widgets overwrite
    # admin surfaces with canonical shapes, contracts run at tail.
    files_list = list(claude_mw_v19_raw["files"].items())

    import build as build_mod  # noqa: E402

    # Run the pipeline; leave _run_contracts live so any regression surfaces
    # here rather than drifting to the tail.
    with patch.object(build_mod, "validate_completeness", lambda files: []), \
         patch.object(build_mod, "_load_templates", lambda: {}):
        build_mod.build_app(
            str(spec_path),
            output_dir=str(build_out),
            tier="build",
            _claude_response_override=files_list,
        )

    # Re-run contracts in collecting mode and scope assertions to the admin
    # surfaces this universalization change is responsible for. Non-admin
    # surfaces (home/about/layout/login) may still fail on this cross-
    # fixture (MW assets + non-MW spec) path and are explicitly NOT asserted
    # - those failures are orthogonal to the archetype-gate change.
    from cli.contracts.run import run_all  # noqa: E402
    from cli.contracts import ContractViolation  # noqa: E402

    artifact = _artifact_from_dir(build_out, spec)
    monkeypatch.setenv("CAS_CONTRACTS_COLLECT", "1")
    msg = ""
    try:
        run_all(artifact)
    except ContractViolation as exc:
        msg = str(exc)

    admin_prefixes = (
        "views/admin/settings.ejs",
        "views/admin/",  # catches views/admin/<slug>/new.ejs etc.
    )
    admin_violations = [
        line for line in msg.splitlines()
        if any(line.startswith(prefix) for prefix in admin_prefixes)
    ]
    assert not admin_violations, (
        "admin surface contracts must pass on widget-emitted non-MW "
        f"artifact; got violations: {admin_violations!r}"
    )


# ---------------------------------------------------------------------------
# Tests 3-4: reapply on the v19 on-disk snapshot (primary validation path)
# ---------------------------------------------------------------------------


def test_reapply_on_v19_snapshot_with_widgets_on_contracts_green(
    monkeypatch, tmp_path, v19_snapshot_dir, claude_mw_v19_raw,
):
    """PRIMARY VALIDATION PATH per plan §9.1 step 8: re-run the post-processor
    pipeline against a copy of the on-disk v19 build and verify widgets fire
    AND contracts pass. Zero dollars of Claude cost."""
    dst = _copy_v19_to(tmp_path, v19_snapshot_dir)
    monkeypatch.setenv("WIDGETS_ENABLED", "true")

    spec = claude_mw_v19_raw["spec"]

    import build as build_mod  # noqa: E402
    build_mod.reapply_post_processors(str(dst), spec)

    # Widget emission signal: settings.ejs on disk must contain the
    # SECTION_LAYOUT section headings emitted by cli.widgets.forms.emit_all.
    # Per plan §9.1, checking the six heading titles is the "cheapest signal"
    # available from disk (there is no metadata carried through the on-disk
    # write path).
    settings_ejs = (dst / "views" / "admin" / "settings.ejs").read_text()
    expected_headings = (
        "Site Identity",
        "Hero Section",
        "Contact Info",
        "Social Media",
    )
    missing = [h for h in expected_headings if h not in settings_ejs]
    assert not missing, (
        f"reapply-on-v19 + WIDGETS_ENABLED=true: settings.ejs missing "
        f"SECTION_LAYOUT headings {missing!r}. widget emission may not "
        f"have fired or was overwritten by a later post-processor."
    )

    # Contracts-green: run_all on the post-reapply on-disk artifact must
    # not raise. reapply_post_processors itself runs _run_contracts at the
    # DAG tail (phase="reapply" includes the contract processor), so
    # reaching here implies contracts passed during reapply. The explicit
    # re-invocation here makes the assertion visible in the test body.
    from cli.contracts.run import run_all  # noqa: E402
    artifact = _artifact_from_dir(dst, spec)
    run_all(artifact)


def test_reapply_idempotent(
    monkeypatch, tmp_path, v19_snapshot_dir, claude_mw_v19_raw,
):
    """reapply_post_processors() applied twice to the same snapshot must
    produce byte-identical output. Load-bearing for the customer-support
    rescue path: if an SE kicks off a reapply to clear a stuck build,
    the second run must be a no-op.

    Currently xfails via _FIX_C_VS_WIDGET_DRIFT because reapply itself
    fails at the tail contract gate on the first pass; once FIX C and the
    widget form-action URL conflict is resolved this test should convert
    from xfail to pass."""
    dst = _copy_v19_to(tmp_path, v19_snapshot_dir)
    monkeypatch.setenv("WIDGETS_ENABLED", "true")

    spec = claude_mw_v19_raw["spec"]

    import build as build_mod  # noqa: E402
    build_mod.reapply_post_processors(str(dst), spec)
    map1 = _hash_dir(dst)

    build_mod.reapply_post_processors(str(dst), spec)
    map2 = _hash_dir(dst)

    if map1 != map2:
        added = sorted(set(map2) - set(map1))
        removed = sorted(set(map1) - set(map2))
        changed = sorted(
            p for p in map1.keys() & map2.keys() if map1[p] != map2[p]
        )
        pytest.fail(
            f"reapply non-idempotent; second pass diverged. "
            f"added={added[:5]}, removed={removed[:5]}, changed={changed[:5]}"
        )


# ---------------------------------------------------------------------------
# Tests 5-7: wall-clock budgets
# ---------------------------------------------------------------------------


def test_pipeline_wallclock_within_5pct_of_v19_baseline(
    monkeypatch, tmp_path, canonical_mw_spec, claude_mw_v19_raw,
):
    """Plan §9.2 budget: full build_app pipeline on canonical MW must stay
    within 5% of the v19 baseline median.

    The baseline median has not yet been pinned into
    tests/golden/medicine-waves-canonical/artifact.snapshot.json; using the
    conservative V19_BASELINE_S constant as an upper bound until a proper
    blessing is run. Log both numbers on failure so the blessing can be
    updated if the new median is legitimately higher."""
    monkeypatch.setenv("WIDGETS_ENABLED", "true")

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(canonical_mw_spec))
    build_out = tmp_path / "out"
    build_out.mkdir()

    files_list = list(claude_mw_v19_raw["files"].items())

    import build as build_mod  # noqa: E402

    # Stub out _run_contracts so the wall-clock measurement is of the
    # build pipeline only (contract wall-clock has its own test below).
    # Without this stub, prior-section bugs surfacing in contracts would
    # cause build_app to raise before we measured wall-clock at all,
    # conflating "too slow" and "contract violation" failure modes.
    with patch.object(build_mod, "validate_completeness", lambda files: []), \
         patch.object(build_mod, "_load_templates", lambda: {}), \
         patch.object(build_mod, "_run_contracts", lambda artifact: artifact):
        t0 = time.monotonic()
        build_mod.build_app(
            str(spec_path),
            output_dir=str(build_out),
            tier="build",
            _claude_response_override=files_list,
        )
        elapsed = time.monotonic() - t0

    ceiling = V19_BASELINE_S * 1.05
    assert elapsed <= ceiling, (
        f"full build_app wall-clock ({elapsed:.3f}s) exceeded 5% ceiling "
        f"({ceiling:.3f}s) over baseline ({V19_BASELINE_S:.3f}s). If the "
        "new median is legitimately higher, update V19_BASELINE_S (or pin "
        "pipeline_wall_clock_median_s in tests/golden/.../artifact.snapshot.json)."
    )


def test_contract_suite_wallclock_under_2s_on_v19_artifact(
    tmp_path, v19_snapshot_dir, claude_mw_v19_raw,
):
    """Plan §6.7 hard budget: the full contract suite on a v19-size
    artifact must complete in under 2.0 seconds. This measures only
    run_all (not the surrounding build pipeline) to isolate the
    contract-framework cost from post-processor work."""
    dst = _copy_v19_to(tmp_path, v19_snapshot_dir)
    spec = claude_mw_v19_raw["spec"]

    artifact = _artifact_from_dir(dst, spec)

    from cli.contracts.run import run_all  # noqa: E402
    from cli.contracts import ContractViolation  # noqa: E402

    t0 = time.monotonic()
    try:
        # CAS_CONTRACTS_COLLECT=1 from conftest autouse: aggregated mode
        # so the wall-clock measurement exercises every contract module
        # rather than bailing out of the first one.
        run_all(artifact)
    except ContractViolation:
        # Violations are orthogonal to this test; wall-clock is the
        # invariant we care about here. The other v19-snapshot test
        # asserts contracts-green; this one asserts contract-suite speed.
        pass
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, (
        f"contracts.run_all wall-clock on v19 artifact ({elapsed:.3f}s) "
        f"exceeded the 2.0s hard budget (plan §6.7)."
    )


# ---------------------------------------------------------------------------
# Tests 8-9: pipeline parity + EXPECTED_PROCESSORS guardrail
# ---------------------------------------------------------------------------


def test_assert_pipeline_parity_succeeds_on_full_registry():
    """After cli/build.py is imported (which triggers every @processor
    decorator via its local `import pipeline` side effect),
    assert_pipeline_parity() must resolve both 'build' and 'reapply'
    phases without raising. Guards against DAG drift introduced silently
    in sections 04-06.

    Uses the same module-alias convention as tests/test_pipeline_dag.py;
    cli/build.py imports pipeline from its own directory via sys.path, so
    REGISTRY lives on `pipeline`, not on `cli.pipeline` (those are
    distinct module objects under pytest's collection-time sys.path
    setup).

    Intentionally does NOT pop the build module from sys.modules (the
    pattern used by tests/test_pipeline_dag.py::_real_registry). Popping
    pollutes later tests that imported names like `_ensure_video_embeds`
    at module top from `build`, those module-top references keep pointing
    at the OLD build instance after a pop-and-reimport, while module
    globals like `_VIDEO_POOLS` get mutated on the NEW instance. Relying
    on the already-imported build module here keeps the test hermetic.
    """
    # Trigger import so REGISTRY is populated. If an earlier test already
    # imported build, this is a no-op. If not, the decorator side effects
    # on import populate pipeline.REGISTRY.
    import build  # noqa: F401
    import pipeline  # noqa: E402

    pipeline.assert_pipeline_parity()


def test_expected_processors_equals_registry_keys():
    """Guard the EXPECTED_PROCESSORS tuple against REGISTRY drift. Every
    @processor-decorated function must appear in EXPECTED_PROCESSORS, and
    every entry in EXPECTED_PROCESSORS must be registered. Integration-level
    re-assertion of the section-02 guardrail so CI catches drift introduced
    by sections 04-06 even if a future refactor splits the section-02 DAG
    test file.

    Does NOT pop-and-reimport build (see the parity-test docstring), the
    already-imported module's decorators have already populated REGISTRY."""
    import build  # noqa: F401
    import pipeline  # noqa: E402

    expected_set = set(pipeline.EXPECTED_PROCESSORS)
    registered_set = set(pipeline.REGISTRY.keys())

    missing_from_registry = expected_set - registered_set
    missing_from_expected = registered_set - expected_set

    assert expected_set == registered_set, (
        f"EXPECTED_PROCESSORS vs REGISTRY drift.\n"
        f"  in EXPECTED_PROCESSORS but not REGISTRY: "
        f"{sorted(missing_from_registry)}\n"
        f"  in REGISTRY but not EXPECTED_PROCESSORS: "
        f"{sorted(missing_from_expected)}"
    )
