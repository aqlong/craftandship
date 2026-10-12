# cli/pipeline.py - DAG foundation for the post-processor pipeline.
#
# Part of the B+C pipeline rewrite (docs/plans/b-plus-c/claude-plan.md; see
# "Plan section labels" in cli/README.md). Introduces a @processor decorator
# that registers every post-processor with ordering metadata (after, before,
# produces, consumes, phase, assumes_on_disk) in a module-level REGISTRY.
# The widget emission and the contracts consume this metadata; the module
# changes no output and exists to enforce a topologically-sorted call order
# with byte-identical results.
#
# Key design notes:
# - The decorator does NOT wrap execution. fn remains directly callable so
#   unit tests and older call sites keep working.
# - Re-decorating the same __name__ raises ValueError to prevent silent
#   drift when someone forgets a rename.
# - Topological sort uses graphlib.TopologicalSorter. Ties inside a
#   topologically-ready group are broken by sorting names alphabetically so
#   ordering is deterministic across runs.
# - Phase filtering drops edges whose predecessor/successor is not in the
#   subset. This lets reapply inherit chained "after" edges from the build
#   phase without pulling build-only processors into the reapply subset.
# - @assumes_on_disk lets a reapply-only consumer declare that a given file
#   is present on the filesystem from an earlier build_app run, so the
#   parity checker treats the consume edge as satisfied even when no
#   in-subset producer exists.

"""LEGACY v1 build engine (frozen): the post-processor DAG that orders cli/build.py.
Superseded by the v2 engine in compose/ and kept for reference only; do not
extend it. scripts/bless_golden.py still imports it. See cli/README.md."""

from __future__ import annotations

import graphlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Artifact(Protocol):
    """Descriptive Protocol documenting the artifact shape.

    The Protocol is intentionally not enforced at runtime. Today's pipeline
    threads a list[tuple[str, str|bytes]] plus a separate spec dict through
    the post-processors; later sections will migrate that to a proper object
    carrying the fields below. The Protocol exists so static analysis and
    future code can rely on a stable shape without a breaking refactor now.
    """

    files: dict[str, str | bytes]
    spec: dict
    build_dir: str
    # Scratch storage for widget output that is NOT written to disk. Filled
    # by section 04's widget emission; consumed by section 05's contracts.
    widgets_snippets: dict[str, str]
    # Free-form metadata, e.g. widgets_emitted_at_generation timestamp.
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Proc:
    """One registered post-processor plus its DAG metadata."""

    fn: Callable[..., Any]
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    # phase membership: frozenset of {"build", "reapply"} subset names.
    phase: frozenset = frozenset({"build", "reapply"})
    assumes_on_disk: tuple[str, ...] = ()


REGISTRY: dict[str, Proc] = {}


class PipelineParityError(RuntimeError):
    """Raised when pipeline_order(phase) detects an unsatisfied @consumes in
    the phase-filtered subset.

    The message names the offending processor, the missing file(s), and the
    phase, so worker logs surface drift without requiring a debugger.
    """


def processor(
    *,
    after: tuple[str, ...] = (),
    before: tuple[str, ...] = (),
    produces: tuple[str, ...] = (),
    consumes: tuple[str, ...] = (),
    phase: tuple[str, ...] = ("build", "reapply"),
    assumes_on_disk: tuple[str, ...] = (),
) -> Callable:
    """Register fn in REGISTRY under fn.__name__ with full DAG metadata.

    The decorator returns fn unchanged so legacy call sites (build_app,
    reapply_post_processors, unit tests) keep working during the migration.
    Only the registration is side-effectful.

    Re-decorating an already-registered name raises ValueError. This catches
    accidental duplicate definitions and copy-paste drift early.
    """

    def _register(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        new_proc = Proc(
            fn=fn,
            after=tuple(after),
            before=tuple(before),
            produces=tuple(produces),
            consumes=tuple(consumes),
            phase=frozenset(phase),
            assumes_on_disk=tuple(assumes_on_disk),
        )
        if name in REGISTRY:
            existing = REGISTRY[name]
            existing_code = getattr(existing.fn, "__code__", None)
            new_code = getattr(fn, "__code__", None)
            same_source = (
                existing_code is not None
                and new_code is not None
                and existing_code.co_filename == new_code.co_filename
                and existing_code.co_firstlineno == new_code.co_firstlineno
            )
            same_metadata = (
                existing.after == new_proc.after
                and existing.before == new_proc.before
                and existing.produces == new_proc.produces
                and existing.consumes == new_proc.consumes
                and existing.phase == new_proc.phase
                and existing.assumes_on_disk == new_proc.assumes_on_disk
            )
            if same_source and same_metadata:
                return fn
            raise ValueError(
                f"@processor: {name} is already registered; duplicate "
                f"decoration would silently overwrite earlier metadata"
            )
        REGISTRY[name] = new_proc
        return fn

    return _register


def _phase_subset(phase: str) -> dict[str, Proc]:
    """Return the dict of (name -> Proc) whose phase set contains `phase`."""
    return {name: proc for name, proc in REGISTRY.items() if phase in proc.phase}


def _validate_references(subset: dict[str, Proc]) -> None:
    """Raise KeyError if any after/before entry references a name that does
    not exist in REGISTRY at all. References to registered names that are
    simply not in the phase subset are tolerated; they are silently dropped
    during graph construction (see _build_phase_graph)."""
    all_names = set(REGISTRY)
    for name, proc in subset.items():
        for dep in proc.after:
            if dep not in all_names:
                raise KeyError(
                    f"{name} declares after={dep!r}, but {dep!r} is not a "
                    f"registered processor"
                )
        for dep in proc.before:
            if dep not in all_names:
                raise KeyError(
                    f"{name} declares before={dep!r}, but {dep!r} is not a "
                    f"registered processor"
                )


def _build_phase_graph(subset: dict[str, Proc]) -> dict[str, set[str]]:
    """Build graphlib-style predecessor map for the phase subset.

    The returned dict maps name -> set of predecessor names. Edges from
    processors outside the subset are dropped; this is how reapply inherits
    build-phase `after` chains without pulling in build-only processors.
    """
    graph: dict[str, set[str]] = {name: set() for name in subset}
    for name, proc in subset.items():
        for dep in proc.after:
            if dep in subset:
                graph[name].add(dep)
        for succ in proc.before:
            # "proc runs before succ" means succ depends on proc.
            if succ in subset:
                graph[succ].add(name)
    return graph


def _check_parity(phase: str, subset: dict[str, Proc]) -> None:
    """Raise PipelineParityError if any @consumes edge in the subset is not
    satisfied by an earlier subset producer AND not covered by the proc's
    own @assumes_on_disk list."""
    subset_producers: set[str] = set()
    for proc in subset.values():
        subset_producers.update(proc.produces)

    for name, proc in subset.items():
        missing = [
            path
            for path in proc.consumes
            if path not in subset_producers and path not in proc.assumes_on_disk
        ]
        if missing:
            raise PipelineParityError(
                f"{name} consumes {missing!r} - no producer in phase={phase!r} "
                f"and not in assumes_on_disk"
            )


def pipeline_order(phase: str = "build") -> list[str]:
    """Return a deterministic topological ordering of processor names for
    the given phase.

    - Filters REGISTRY to procs whose phase set contains `phase`.
    - Raises KeyError if any after/before refers to an unregistered name.
    - Raises RuntimeError wrapping graphlib.CycleError on a cyclic subgraph.
    - Raises PipelineParityError if any @consumes edge is unsatisfied.
    - Ties within a topologically-ready group are broken alphabetically by
      __name__, so ordering is reproducible across Python process runs.
    """
    subset = _phase_subset(phase)
    _validate_references(subset)
    _check_parity(phase, subset)

    graph = _build_phase_graph(subset)
    sorter = graphlib.TopologicalSorter(graph)
    try:
        sorter.prepare()
    except graphlib.CycleError as err:
        # graphlib.CycleError inherits from ValueError. Re-raise as
        # RuntimeError so worker-side exception logs do not confuse this
        # with a user-input validation error.
        cycle_nodes = err.args[1] if len(err.args) > 1 else err.args
        raise RuntimeError(
            f"pipeline_order: cycle detected in phase={phase!r}: {cycle_nodes!r}"
        ) from err

    order: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        for name in ready:
            order.append(name)
            sorter.done(name)
    return order


def _invoke_proc(fn: Callable[..., Any], artifact: Any) -> Any:
    """Call fn against artifact using the right argument shape.

    During the section-02 transition, post-processors still have their
    legacy `(files)` or `(files, spec)` signatures. run_pipeline bridges
    the two shapes: if the artifact has a `files` attribute we dispatch
    using the legacy shape; otherwise we fall back to `fn(artifact)` (the
    target shape for future sections).

    Section-04 introduced processors that take the full artifact even
    inside a legacy-shaped pipeline so they can mutate `.widgets_snippets`
    and `.metadata`. These declare an `artifact` parameter, which takes
    priority over the legacy files/spec dispatch.
    """
    import inspect

    sig = inspect.signature(fn)
    params = sig.parameters
    if "artifact" in params:
        return fn(artifact)
    if hasattr(artifact, "files") and hasattr(artifact, "spec"):
        if "spec" in params:
            return fn(artifact.files, artifact.spec)
        return fn(artifact.files)
    return fn(artifact)


def run_pipeline(
    artifact: Any,
    *,
    phase: str = "build",
    subset: set[str] | None = None,
) -> Any:
    """Invoke each processor in phase-filtered topological order.

    If `subset` is given, only those procs are run (intended for unit tests
    that want to exercise a slice of the pipeline). Subset still runs in
    the phase's topological order.

    Each processor is called with the artifact. If the artifact carries the
    legacy (files, spec) shape (exposed as .files and .spec attributes),
    run_pipeline calls fn(files) or fn(files, spec) depending on the proc's
    signature, and the return value replaces artifact.files. Otherwise the
    proc is called as fn(artifact) and a non-None return replaces the
    running artifact.

    Returns the final artifact.
    """
    order = pipeline_order(phase)
    if subset is not None:
        order = [name for name in order if name in subset]

    import sys

    legacy_shape = hasattr(artifact, "files") and hasattr(artifact, "spec")
    import inspect

    for name in order:
        proc = REGISTRY[name]
        module = sys.modules.get(proc.fn.__module__)
        fn = getattr(module, name, proc.fn) if module is not None else proc.fn
        # Section-04: processors that take `artifact` directly already mutate
        # the artifact in place; their return value IS the artifact and must
        # not be assigned to artifact.files.
        takes_artifact = "artifact" in inspect.signature(fn).parameters
        result = _invoke_proc(fn, artifact)
        if result is not None:
            if takes_artifact:
                artifact = result
            elif legacy_shape:
                artifact.files = result
            else:
                artifact = result
    return artifact


def assert_pipeline_parity() -> None:
    """Validate that both 'build' and 'reapply' phases resolve cleanly.

    Called once at cli/build.py import time so DAG drift fails fast at
    pytest collection and worker startup, rather than at customer-build time.
    """
    pipeline_order("build")
    pipeline_order("reapply")


# Explicit-registry guardrail. Every currently-decorated processor is
# listed here; adding a new @processor requires ALSO editing this tuple.
# The friction is intentional: it catches silent registration drift and
# forces the implementer to choose phase metadata deliberately.
#
# The list below mirrors the call order used inside build_app() so diff
# review is straightforward. Processors called only in the build block
# come first; those also reused by reapply are interleaved where they
# appear in build order.
#
# NOTE ON _fix_seed_column_mismatch: cli/build.py has two top-level
# def _fix_seed_column_mismatch blocks (lines 2957 and 4560 pre-S02). In
# Python the later def shadows the earlier one, so only one Proc ends up
# registered (the second def) and the name appears exactly once here.
EXPECTED_PROCESSORS: tuple[str, ...] = (
    "_ensure_shared_db_pool",
    "_fix_tailwind_config",
    "_fix_csrf_pattern",
    "_ensure_validate_csrf_export",
    "_fix_circular_imports",
    "_ensure_route_views",
    "_normalize_password_column_to_canonical",
    "_strip_demo_credentials",
    "_fix_login_form_fields",
    "_ensure_login_completeness",
    "_ensure_register_completeness",
    "_ensure_admin_mount",
    "_ensure_error_handling",
    "_strip_process_exit_on_db",
    "_validate_ejs_templates",
    "_validate_destructive_confirm",
    "_validate_seed_sql",
    "_join_multiline_inserts",
    "_split_seed_inserts",
    "_dedup_seed_entries",
    "_ensure_gallery_dedup",
    "_fix_seed_column_mismatch",
    "_fix_seed_uuid_id",
    "_ensure_package_deps",
    "_fix_view_html_shells",
    "_fix_view_slug_paths",
    "_ensure_logout_form",
    "_fix_null_property_access",
    "_fix_brand_nowrap",
    "_ensure_mobile_nav",
    "_ensure_mobile_responsive",
    "_ensure_tailwind_compiled",
    "_ensure_font_loading",
    "_ensure_color_consistency",
    "_fix_admin_dark_stat_cards",
    "_fix_dark_section_text",
    "_fix_light_on_light_text",
    "_ensure_nav_glass",
    "_ensure_nav_solid_surface",
    "_ensure_nav_glassmorphism",
    "_ensure_logo_placement",
    "_ensure_stock_photos",
    "_ensure_model_seed_images",
    "_ensure_nav_logo",
    "_strip_broken_logo_refs",
    "_ensure_nav_brand_element",
    "_fix_nav_brand_contrast_and_dedupe",
    "_ensure_icon_helper",
    "_ensure_favicon_ico_fallback",
    "_ensure_sitemap_xml",
    "_bump_service_worker_cache_version",
    "_ensure_video_embeds",
    "_ensure_home_video_click_to_play",
    "_remove_homepage_video_section",
    "_ensure_premium_css",
    "_ensure_layout_html_skeleton",
    "_align_secondary_palette_to_spec",
    "_ensure_accent_color_variety",
    "_ensure_stat_card_accent_variety",
    "_ensure_about_photo_fallback",
    "_ensure_mw_archetype_logo_bundled",
    "_ensure_nav_logo_default_seed",
    "_ensure_site_settings",
    "_ensure_settings_in_views",
    "_ensure_hero_settings_binding",
    "_ensure_contact_email",
    "_ensure_upload_route",
    # Section-04: widget emission pipeline. Runs before the FIX B/L/M/N
    # post-processors (which become byte-identical no-ops on widget output).
    "_emit_widgets",
    "_ensure_admin_slug_mount",
    "_ensure_require_admin_defined",
    "_inject_dashboard_settings_section",
    "_rewrite_admin_router_template_switch",
    "_ensure_crud_forms_complete",
    "_fix_route_link_drift",
    "_strip_inline_dashboard_settings_form",
    "_humanize_settings_labels",
    "_ensure_upload_form_ui",
    "_strip_redundant_image_upload_forms",
    "_fix_admin_crud_link_prefix",
    "_fix_admin_layout_nav_prefix",
    "_unify_admin_dashboard_handler",
    "_strip_dead_signup_links",
    "_normalize_admin_view_paths",
    "_enforce_admin_view_path_convention",
    "_ensure_admin_model_handlers",
    "_ensure_settings_middleware_contract",
    "_convert_raw_file_inputs_to_upload_zones",
    "_ensure_event_time_helper",
    "_ensure_tz_aware_datetime_schema",
    "_verify_schema_column_references",
    "_verify_template_middleware_contract",
    "_ensure_railwayignore",
    "_fix_seed_admin_email",
    "_ensure_magic_link_auth",
    "_scrub_bcrypt_from_seed",
    "_ensure_magic_link_hash",
    "_ensure_resend_from_address",
    "_fix_delete_button_styling",
    "_fix_csp_cdn_sources",
    "_fix_cdn_script_graceful",
    "_fix_locale_string_options",
    "_strip_require_from_ejs",
    "_scrub_em_dashes_in_ejs",
    "_ensure_resilient_migrate",
    "_ensure_minimum_seed_rows",
    "_ensure_compression",
    "_ensure_static_caching",
    "_ensure_lazy_loading",
    "_strip_duplicate_confirm_handlers",
    "_ensure_delete_confirm_single_handler",
    "_ensure_button_design_system",
    "_ensure_submit_guard",
    "_ensure_dynamic_canonical",
    "_ensure_csrf_import",
    "_ensure_mobile_menu_close",
    "_ensure_skip_to_content",
    # Section-05: contract framework runs LAST. Its after=tuple is built
    # dynamically from REGISTRY.keys() at decoration time, so this name
    # is guaranteed to be the tail of pipeline_order() for both phases.
    "_run_contracts",
)
