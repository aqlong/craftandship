"""Tests for cli/pipeline.py - DAG decorator + topological ordering.

Covers every behavior described in the foundation section of docs/plans/b-plus-c/claude-plan.md:
- @processor registration with full metadata
- Decorator leaves fn unchanged (direct call still works)
- Topological ordering with alphabetical tiebreak
- KeyError on unknown after/before references
- RuntimeError wrapping CycleError on cycles
- Phase filtering (build, reapply)
- @assumes_on_disk satisfying @consumes
- PipelineParityError on unsatisfied consumers
- run_pipeline full, subset-filtered, and return-value semantics
- assert_pipeline_parity happy path + drift detection
- EXPECTED_PROCESSORS ~~ set(REGISTRY.keys()) guardrail
"""

import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import importlib
import sys
from pathlib import Path

import pytest

# Allow importing from cli/ without installing a package.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))


def _fresh_pipeline():
    """Reload pipeline.py to get an empty REGISTRY for isolated tests.

    Every DAG test that registers its own fake procs needs a pristine
    registry. Returning the reloaded module avoids cross-test pollution
    without requiring each test to manually clear it.
    """
    import pipeline

    pipeline.REGISTRY.clear()
    return pipeline


def _real_registry():
    """Reload build and pipeline so the real @processor decorators re-run
    and the REGISTRY is populated with the production DAG.

    Tests that assert about the production registry must call this so they
    are isolated from _fresh_pipeline() clearing side effects earlier in
    the test session.
    """
    import pipeline

    # Drop build from the module cache if it's there so the next import
    # triggers a fresh execution (and thus re-runs the @processor calls
    # against an empty REGISTRY). This is more reliable than importlib.reload
    # because reload on an already-loaded build leaves the module object in
    # an inconsistent state if a prior test partially loaded it.
    sys.modules.pop("build", None)
    pipeline.REGISTRY.clear()
    import build  # noqa: F401 - side effect: decorator calls populate REGISTRY
    return pipeline


# ---------------------------------------------------------------------------
# Decorator registration
# ---------------------------------------------------------------------------


def test_processor_decorator_registers_with_full_metadata():
    pipe = _fresh_pipeline()

    @pipe.processor(
        after=("pred",),
        before=("succ",),
        produces=("views/out.ejs",),
        consumes=("views/in.ejs",),
        phase=("build",),
        assumes_on_disk=("views/disk.ejs",),
    )
    def _fake_proc(artifact):
        return artifact

    assert "_fake_proc" in pipe.REGISTRY
    proc = pipe.REGISTRY["_fake_proc"]
    assert proc.fn is _fake_proc
    assert proc.after == ("pred",)
    assert proc.before == ("succ",)
    assert proc.produces == ("views/out.ejs",)
    assert proc.consumes == ("views/in.ejs",)
    assert proc.phase == frozenset({"build"})
    assert proc.assumes_on_disk == ("views/disk.ejs",)


def test_processor_decorator_returns_fn_unchanged():
    """Decorator must not wrap execution; fn(args) still works directly."""
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _fake_id(x):
        return x + 1

    # Direct callability preserved.
    assert _fake_id(5) == 6
    # And REGISTRY holds the same function object.
    assert pipe.REGISTRY["_fake_id"].fn is _fake_id


def test_processor_duplicate_name_raises_valueerror():
    """Re-decorating the same name must not silently overwrite metadata."""
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _dup(artifact):
        return artifact

    with pytest.raises(ValueError, match="already registered"):

        @pipe.processor(phase=("build",))
        def _dup(artifact):  # noqa: F811 - intentional rebind for the test
            return artifact


def test_processor_default_phase_is_build_plus_reapply():
    pipe = _fresh_pipeline()

    @pipe.processor()
    def _default_phase(artifact):
        return artifact

    assert pipe.REGISTRY["_default_phase"].phase == frozenset({"build", "reapply"})


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def test_pipeline_order_is_topological():
    """Every @after dep must precede its consumer in the resolved order."""
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _fix_a(artifact):
        return artifact

    @pipe.processor(after=("_fix_a",), phase=("build",))
    def _fix_b(artifact):
        return artifact

    @pipe.processor(after=("_fix_b",), phase=("build",))
    def _fix_c(artifact):
        return artifact

    order = pipe.pipeline_order("build")
    assert order.index("_fix_a") < order.index("_fix_b") < order.index("_fix_c")


def test_pipeline_order_respects_before_edges():
    """@before X means this proc must run before X."""
    pipe = _fresh_pipeline()

    @pipe.processor(before=("_run_later",), phase=("build",))
    def _run_earlier(artifact):
        return artifact

    @pipe.processor(phase=("build",))
    def _run_later(artifact):
        return artifact

    order = pipe.pipeline_order("build")
    assert order.index("_run_earlier") < order.index("_run_later")


def test_pipeline_order_alphabetical_tiebreak():
    """Procs with identical topological rank must sort alphabetically."""
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _zzz_last_alpha(artifact):
        return artifact

    @pipe.processor(phase=("build",))
    def _aaa_first_alpha(artifact):
        return artifact

    @pipe.processor(phase=("build",))
    def _mmm_mid_alpha(artifact):
        return artifact

    # Deterministic across runs.
    order = pipe.pipeline_order("build")
    assert order == ["_aaa_first_alpha", "_mmm_mid_alpha", "_zzz_last_alpha"]


def test_pipeline_order_alphabetical_tiebreak_within_topo_level():
    """Alphabetical tiebreak applies WITHIN each get_ready() batch, not
    across the entire run. Procs with dependencies appear after their
    predecessors even when alphabetically earlier."""
    pipe = _fresh_pipeline()

    # _zzz has no deps, _aaa depends on _zzz -> topo says zzz before aaa.
    @pipe.processor(phase=("build",))
    def _zzz_no_dep(artifact):
        return artifact

    @pipe.processor(after=("_zzz_no_dep",), phase=("build",))
    def _aaa_after_zzz(artifact):
        return artifact

    order = pipe.pipeline_order("build")
    assert order == ["_zzz_no_dep", "_aaa_after_zzz"]


def test_pipeline_order_deterministic_across_calls():
    """Same REGISTRY state must produce the same order on every call."""
    pipe = _fresh_pipeline()

    for i in range(5):

        def _factory(idx=i):
            @pipe.processor(phase=("build",))
            def _proc(artifact):
                return artifact

            _proc.__name__ = f"_proc_{idx:02d}"
            # graphlib sees processors by name; re-register with the
            # explicit __name__ so the unique names survive.

    # Reset and build the registry explicitly with distinct names.
    pipe.REGISTRY.clear()
    for i in range(5):
        def _make():
            def _inner(artifact):
                return artifact
            return _inner

        fn = _make()
        fn.__name__ = f"_proc_{i:02d}"
        pipe.processor(phase=("build",))(fn)

    first = pipe.pipeline_order("build")
    second = pipe.pipeline_order("build")
    assert first == second


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_pipeline_order_raises_keyerror_on_unknown_after():
    pipe = _fresh_pipeline()

    @pipe.processor(after=("_does_not_exist",), phase=("build",))
    def _dangling_after(artifact):
        return artifact

    with pytest.raises(KeyError, match="_does_not_exist"):
        pipe.pipeline_order("build")


def test_pipeline_order_raises_keyerror_on_unknown_before():
    pipe = _fresh_pipeline()

    @pipe.processor(before=("_missing_target",), phase=("build",))
    def _dangling_before(artifact):
        return artifact

    with pytest.raises(KeyError, match="_missing_target"):
        pipe.pipeline_order("build")


def test_pipeline_order_wraps_cycle_error_in_runtime_error():
    pipe = _fresh_pipeline()

    @pipe.processor(after=("_fix_b",), phase=("build",))
    def _fix_a(artifact):
        return artifact

    @pipe.processor(after=("_fix_a",), phase=("build",))
    def _fix_b(artifact):
        return artifact

    with pytest.raises(RuntimeError, match="cycle"):
        pipe.pipeline_order("build")


def test_pipeline_order_cycle_error_names_the_nodes():
    """The RuntimeError message should identify the cycle so worker logs
    can diagnose DAG drift without a debugger."""
    pipe = _fresh_pipeline()

    @pipe.processor(after=("_loop_b",), phase=("build",))
    def _loop_a(artifact):
        return artifact

    @pipe.processor(after=("_loop_a",), phase=("build",))
    def _loop_b(artifact):
        return artifact

    with pytest.raises(RuntimeError) as excinfo:
        pipe.pipeline_order("build")

    message = str(excinfo.value)
    assert "_loop_a" in message
    assert "_loop_b" in message


# ---------------------------------------------------------------------------
# Phase filtering
# ---------------------------------------------------------------------------


def test_pipeline_order_build_phase_filters_reapply_only_procs():
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _build_only(artifact):
        return artifact

    @pipe.processor(phase=("reapply",))
    def _reapply_only(artifact):
        return artifact

    @pipe.processor(phase=("build", "reapply"))
    def _both(artifact):
        return artifact

    build_order = pipe.pipeline_order("build")
    assert "_build_only" in build_order
    assert "_both" in build_order
    assert "_reapply_only" not in build_order


def test_pipeline_order_reapply_phase_filters_build_only_procs():
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _build_only(artifact):
        return artifact

    @pipe.processor(phase=("reapply",))
    def _reapply_only(artifact):
        return artifact

    @pipe.processor(phase=("build", "reapply"))
    def _both(artifact):
        return artifact

    reapply_order = pipe.pipeline_order("reapply")
    assert "_reapply_only" in reapply_order
    assert "_both" in reapply_order
    assert "_build_only" not in reapply_order


def test_reapply_subset_auto_derived_from_phase_metadata():
    """No hand-written list - pipeline_order('reapply') returns exactly
    the procs whose phase set contains 'reapply'."""
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _only_build(artifact):
        return artifact

    @pipe.processor(phase=("reapply",))
    def _only_reapply(artifact):
        return artifact

    @pipe.processor(phase=("build", "reapply"))
    def _shared(artifact):
        return artifact

    reapply_order = pipe.pipeline_order("reapply")
    expected = {"_only_reapply", "_shared"}
    assert set(reapply_order) == expected


def test_pipeline_order_drops_edges_to_non_subset_procs():
    """A reapply proc with after=(build_only_proc,) loses that edge in the
    reapply subset - the build-only predecessor is dropped before topo sort.
    This is what lets reapply inherit build-phase chained orderings without
    pulling build-only processors into the reapply subset."""
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _only_in_build(artifact):
        return artifact

    @pipe.processor(after=("_only_in_build",), phase=("build", "reapply"))
    def _shared_with_after_build_only(artifact):
        return artifact

    # In reapply phase, _only_in_build is filtered out and the edge is
    # dropped. _shared_with_after_build_only runs with no predecessors.
    reapply_order = pipe.pipeline_order("reapply")
    assert reapply_order == ["_shared_with_after_build_only"]


# ---------------------------------------------------------------------------
# assumes_on_disk + PipelineParityError
# ---------------------------------------------------------------------------


def test_assumes_on_disk_satisfies_consumes_in_reapply():
    pipe = _fresh_pipeline()

    # Reapply-only consumer with no producer in the reapply subset,
    # but its @consumes file is listed in @assumes_on_disk -> OK.
    @pipe.processor(
        consumes=("views/admin/settings.ejs",),
        assumes_on_disk=("views/admin/settings.ejs",),
        phase=("reapply",),
    )
    def _reapply_consumer(artifact):
        return artifact

    # Should NOT raise.
    order = pipe.pipeline_order("reapply")
    assert order == ["_reapply_consumer"]


def test_pipeline_parity_error_on_unsatisfied_reapply_consumer():
    pipe = _fresh_pipeline()

    # Reapply-only proc consumes a file that no reapply subset member
    # produces AND is not in assumes_on_disk -> parity violation.
    @pipe.processor(
        consumes=("views/widget_emitted.ejs",),
        phase=("reapply",),
    )
    def _bad_consumer(artifact):
        return artifact

    with pytest.raises(pipe.PipelineParityError, match="views/widget_emitted.ejs"):
        pipe.pipeline_order("reapply")


def test_pipeline_parity_satisfied_by_producer_in_subset():
    pipe = _fresh_pipeline()

    @pipe.processor(produces=("views/foo.ejs",), phase=("reapply",))
    def _producer(artifact):
        return artifact

    @pipe.processor(
        after=("_producer",),
        consumes=("views/foo.ejs",),
        phase=("reapply",),
    )
    def _consumer(artifact):
        return artifact

    # Should NOT raise; producer is in subset.
    order = pipe.pipeline_order("reapply")
    assert order == ["_producer", "_consumer"]


# ---------------------------------------------------------------------------
# run_pipeline
# ---------------------------------------------------------------------------


def test_run_pipeline_invokes_every_build_proc_exactly_once_in_order():
    pipe = _fresh_pipeline()
    calls: list[str] = []

    @pipe.processor(phase=("build",))
    def _step_a(artifact):
        calls.append("_step_a")
        return artifact

    @pipe.processor(after=("_step_a",), phase=("build",))
    def _step_b(artifact):
        calls.append("_step_b")
        return artifact

    @pipe.processor(after=("_step_b",), phase=("build",))
    def _step_c(artifact):
        calls.append("_step_c")
        return artifact

    pipe.run_pipeline({"files": {}}, phase="build")
    assert calls == ["_step_a", "_step_b", "_step_c"]


def test_run_pipeline_subset_only_invokes_named_procs():
    pipe = _fresh_pipeline()
    calls: list[str] = []

    @pipe.processor(phase=("build",))
    def _p1(artifact):
        calls.append("_p1")
        return artifact

    @pipe.processor(phase=("build",))
    def _p2(artifact):
        calls.append("_p2")
        return artifact

    @pipe.processor(phase=("build",))
    def _p3(artifact):
        calls.append("_p3")
        return artifact

    pipe.run_pipeline({}, phase="build", subset={"_p1", "_p3"})
    assert calls == ["_p1", "_p3"]


def test_run_pipeline_returns_artifact():
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _mutator(artifact):
        # Mutate in place and return None.
        artifact["visited"] = True
        return None

    artifact = {"files": {}, "visited": False}
    result = pipe.run_pipeline(artifact, phase="build")
    assert result is artifact  # same reference when procs return None
    assert result["visited"] is True


def test_run_pipeline_replaces_artifact_when_proc_returns_non_none():
    pipe = _fresh_pipeline()

    @pipe.processor(phase=("build",))
    def _returns_new(artifact):
        return {"replaced": True}

    result = pipe.run_pipeline({"original": True}, phase="build")
    assert result == {"replaced": True}


def test_run_pipeline_respects_reapply_phase():
    pipe = _fresh_pipeline()
    calls: list[str] = []

    @pipe.processor(phase=("build",))
    def _build_only(artifact):
        calls.append("build_only")
        return artifact

    @pipe.processor(phase=("reapply",))
    def _reapply_only(artifact):
        calls.append("reapply_only")
        return artifact

    pipe.run_pipeline({}, phase="reapply")
    assert calls == ["reapply_only"]


# ---------------------------------------------------------------------------
# assert_pipeline_parity
# ---------------------------------------------------------------------------


def test_assert_pipeline_parity_succeeds_on_realistic_registry():
    pipe = _fresh_pipeline()

    @pipe.processor(produces=("views/a.ejs",), phase=("build", "reapply"))
    def _a(artifact):
        return artifact

    @pipe.processor(
        after=("_a",),
        consumes=("views/a.ejs",),
        phase=("build", "reapply"),
    )
    def _b(artifact):
        return artifact

    # No exception.
    pipe.assert_pipeline_parity()


def test_assert_pipeline_parity_raises_on_drift():
    pipe = _fresh_pipeline()

    # Reapply-only consumer with nothing producing the file -> parity fails.
    @pipe.processor(
        consumes=("views/orphan.ejs",),
        phase=("reapply",),
    )
    def _orphan_consumer(artifact):
        return artifact

    with pytest.raises(pipe.PipelineParityError):
        pipe.assert_pipeline_parity()


# ---------------------------------------------------------------------------
# EXPECTED_PROCESSORS guardrail
# ---------------------------------------------------------------------------


def test_expected_processors_equals_registry_keys_after_build_import():
    """After importing cli/build.py, every decorated function must appear in
    EXPECTED_PROCESSORS, and every name in EXPECTED_PROCESSORS must be in
    REGISTRY. Catches missed decorators AND missed constant edits."""
    pipe = _real_registry()

    expected_set = set(pipe.EXPECTED_PROCESSORS)
    registered_set = set(pipe.REGISTRY)

    missing_from_registry = expected_set - registered_set
    missing_from_expected = registered_set - expected_set

    assert not missing_from_registry, (
        f"EXPECTED_PROCESSORS lists names not in REGISTRY: "
        f"{sorted(missing_from_registry)}"
    )
    assert not missing_from_expected, (
        f"REGISTRY has names missing from EXPECTED_PROCESSORS: "
        f"{sorted(missing_from_expected)}"
    )


def test_expected_processors_tuple_has_no_duplicates():
    """The tuple is intentional friction; duplicates would silently bypass
    the guardrail, so detect that as a first-class failure."""
    import pipeline

    assert len(pipeline.EXPECTED_PROCESSORS) == len(set(pipeline.EXPECTED_PROCESSORS))


def test_assert_pipeline_parity_succeeds_on_real_registry():
    """Import cli/build.py and verify parity holds on the production DAG."""
    pipe = _real_registry()
    pipe.assert_pipeline_parity()


def test_real_registry_build_order_matches_original_explicit_order():
    """The topological order for phase='build' must contain every
    EXPECTED_PROCESSORS entry in the canonical call order defined at
    module top. Regressions that break this ordering would also break
    byte-identical output; catching it at the ordering layer gives a
    clearer failure signal."""
    pipe = _real_registry()

    order = pipe.pipeline_order("build")
    # Every expected name must appear in the build order (build-only or
    # shared procs are all in this phase).
    for name in pipe.EXPECTED_PROCESSORS:
        assert name in order, f"{name} missing from build order"
