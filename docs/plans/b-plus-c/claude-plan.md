# Implementation Plan: B+C Pipeline Rewrite (v2, post-review)

**Project:** CraftAndShip (CAS) build pipeline structural fix
**Status:** draft v2
**Scope:** the "MW" reference event-site archetype (the canonical build used throughout the tests as the regression baseline), phase 1 (phase 2 + phase 3 are separate plans)

> Export note: this plan came out of a sectionized planning workflow. The per-section files (`sections/section-NN-*.md`), the spec and research inputs, and the review transcripts are not included in this export. Inline ids such as C2, I10 and M7 refer to items in those review notes. The slash commands it names (`/deep-plan`, `/deep-implement`) belong to third-party Claude Code plugins listed in `THIRD_PARTY_NOTICES.md`; no plugin code is part of this repository.

## Reader on-ramp

This plan assumes no prior conversation context. You are modifying
`cli/build.py` in the `craftandship` repo to replace a fragile regex-based
post-processor layer with (a) schema-driven widget generation, (b) BS4
contract assertions, and (c) a DAG decorator for post-processor ordering.
The plan does not write code; it specifies *what* to write, *where*, and
*in what order*. Tests come with each section; implementation is handled
by `/deep-implement`.

### Background in one paragraph

The CAS pipeline generates Express/EJS/Postgres web apps. Claude Opus emits
the initial code; Python post-processors (`FIX A–O` in `cli/build.py`,
~65 total) normalize the output before deploying. Over eight MW builds
(v12–v19), each cycle shipped ~7 improvements while regressing ~3 items; 
root cause is that regex on LLM output is a shifting target. This plan
replaces the three most regression-prone surfaces (admin settings, admin
CRUD forms, admin dashboard settings section) with Python-emitted templates
driven by `spec.data_models`, and adds declarative BS4 contracts that fail
the build before it reaches `preview` status. A DAG decorator layer
underneath eliminates a second drift vector (main-pipeline vs reapply order)
via phase-aware `@processor` metadata.

### Reading order

1. §1 "What we're building": overview + architecture diagram.
2. §2 "Module layout": directory tree of everything new.
3. §3–§8: the six implementation sections, in order.
4. §9 "Cross-cutting concerns": rollout, testing, docs.
5. §10 "Sunset milestone": separate PR after phase 1 soaks.

---

## 1. What we're building

### 1.1 Three-layer rewrite

Each layer is independently useful and gracefully degrades:

- **Layer D (foundation, ships first):** `cli/pipeline.py` - `@processor`
  decorator with `phase={"build", "reapply"}` + `assumes_on_disk=(...)` +
  `produces/consumes/before/after` metadata, backed by
  `graphlib.TopologicalSorter`. Both `build_app()` and
  `reapply_post_processors()` derive their subset from the decorator's
  `phase` metadata: no hand-written subset list. Zero behavior change on
  day one (declared order === today's manual order).
- **Layer B (ships second):** `cli/widgets/`, dataclass-based widget
  registry keyed on `spec.data_models[i].fields[j].type`. Generates
  `views/admin/settings.ejs`, `views/admin/<slug>/new.ejs`, and `edit.ejs`
  deterministically. CRUD form `action` attributes use canonical
  `/admin/<slug>` URLs, paired with a new processor
  `_ensure_admin_slug_mount` that dual-registers routes at `/admin/<slug>`
  alongside existing `/<slug>` mounts. Archetype-gated on MW for phase 1.
- **Layer C (ships third):** `cli/contracts/`, BS4/CSS-selector assertions
  on raw EJS source (with pre-parse EJS stripping as a fallback for
  pathological fragments). `run_all(artifact)` called at the end of both
  pipeline entry points; fail-fast in production, collect-all in pytest
  (via `collect_all` kwarg + `CAS_CONTRACTS_COLLECT=1` env var).

### 1.2 Control flow after phase 1

```
build_app(spec):
  validate_spec(spec)                       [extended VALID_FIELD_TYPES]
  code = claude_generate(spec, prompt)      [prompt no longer asks for
                                             admin forms/settings/CRUD]
  artifact = build_artifact(spec, code)
  artifact = run_pipeline(artifact, phase="build")
      # processors whose @processor(phase) includes "build" run here;
      # _emit_widgets is one of them, after _ensure_site_settings,
      # before _ensure_upload_form_ui / FIX B/L/M/N.
      # _ensure_admin_slug_mount runs after _emit_widgets.
      # Contract processor runs last in pipeline.
  validate_completeness(artifact)
  write_files(artifact)

reapply_post_processors(build_dir, spec):
  artifact = load_from_disk(build_dir)
  artifact = run_pipeline(artifact, phase="reapply")
      # processors whose @processor(phase) includes "reapply" run.
      # REAPPLY_SUBSET_NAMES is DERIVED from decorator metadata, not
      # hand-maintained. parity checker asserts every @consumes in the
      # subset is satisfied either by an earlier subset member or by
      # @assumes_on_disk.
      # _emit_widgets re-emits idempotently when widget_meta is present
      # on the artifact (or when _widgets_enabled_for(spec) on a
      # pre-widget artifact, see I10/§5.1 migration).
      # Contracts run last as in build.
  write_back(artifact)
```

### 1.3 Failure modes

| Condition | Behavior |
|-----------|----------|
| Spec field type not in registry | `UnknownWidgetError` → build fails, admin email, `failure_kb` entry |
| Contract violation (production) | `ContractViolation` → build fails with selector + context, admin email |
| Contract violation (pytest with collect-all) | All violations collected + reported together |
| Pipeline cycle detected | `CycleError` raised at `pipeline_order()` → worker marks build failed |
| Unknown `@runs_after(name)` reference | `KeyError` at first pipeline resolve → surfaced in test suite |
| Reapply consumer has unsatisfied `@consumes` | `PipelineParityError` at pipeline_order; subset is inconsistent |
| `WIDGETS_ENABLED=false` on a spec | Pipeline runs with widgets short-circuited; FIX B/L/M/N run normally |
| `WIDGETS_ENABLED` set to any non-canonical value | `ValueError` at build start with "expected: unset, 'true', or 'false'" |

---

## 2. Module layout

```
cli/
├── build.py                                        # MODIFIED
├── validate.py                                     # MODIFIED: 14 field types
├── pipeline.py                                     # NEW: @processor + graphlib + parity
├── widgets/
│   ├── __init__.py                                 # NEW: register() + render_field() + UnknownWidgetError
│   ├── widgets.py                                  # NEW: 14 Widget subclasses
│   ├── forms.py                                    # NEW: emit_admin_settings / emit_crud_new_edit / emit_dashboard_settings_section / emit_all
│   ├── archetype.py                                # NEW: _widgets_enabled_for(spec)
│   └── labels.py                                   # NEW: humanize() + LABEL_MAP
├── contracts/
│   ├── __init__.py                                 # NEW: contract(), ContractViolation, collect_all kwarg
│   ├── admin_settings.py                           # NEW
│   ├── admin_crud.py                               # NEW
│   ├── admin_dashboard.py                          # NEW
│   ├── layout.py                                   # NEW
│   ├── home.py                                     # NEW
│   ├── about.py                                    # NEW
│   ├── login.py                                    # NEW
│   └── run.py                                      # NEW: run_all(artifact, collect_all=...)
└── templates/
    ├── views_admin_settings.ejs                    # KEPT as reference; not runtime when widgets on
    └── widgets/                                    # NEW: sibling widget templates
        ├── image_upload_zone.ejs.tmpl
        ├── enum_select.ejs.tmpl
        ├── datetime_input.ejs.tmpl
        └── text_input.ejs.tmpl
tests/
├── test_pipeline_dag.py                            # NEW: decorator + topo-sort + parity + phase
├── test_widgets_registry.py                        # NEW
├── test_widgets_labels.py                          # NEW
├── test_widgets_forms.py                           # NEW
├── test_widgets_safety_net.py                      # NEW: widgets on/off parity tests
├── test_contracts_framework.py                    # NEW: BS4 smoke + real-EJS per-version fixtures
├── test_contracts_admin_settings.py                # NEW
├── test_contracts_admin_crud.py                    # NEW
├── test_contracts_admin_dashboard.py               # NEW
├── test_contracts_layout.py                        # NEW
├── test_contracts_non_form_surfaces.py             # NEW: home/about/login combined
├── test_safety_net_catches_known_past_regressions.py  # NEW: regression-injection
├── test_golden_files.py                            # NEW
├── test_mw_v17_fix_b_crud_forms.py                 # MODIFIED: byte-identical no-op on widget output
├── test_mw_v17_fixes_lmno.py                       # MODIFIED: byte-identical no-op
├── test_validate.py                                # MODIFIED: 14 field types
└── fixtures/
    ├── claude_mw_v19_raw.json                      # NEW: recorded Claude output for safety-net tests
    ├── v12_admin_settings.ejs                      # NEW: pinned real-EJS snippets
    ├── v14_admin_settings.ejs
    ├── v18_event_form.ejs
    └── v19_admin_dashboard.ejs
Makefile                                            # NEW TARGET: bless-golden
.githooks/pre-push                                  # MODIFIED: bless: enforcement on tests/golden/
.githooks/pre-commit                                # MODIFIED: failure_kb.json parse guard
docs/internals/POST_PROCESSOR_ORDERING.md           # MODIFIED
docs/internals/WIDGETS_AND_CONTRACTS.md             # NEW: short primer
CLAUDE.md                                           # MODIFIED: test counts, widgets/contracts mention, WIDGETS_ENABLED
cli/failure_kb.json                                 # MODIFIED: UnknownWidgetError + sunset markers
coordinator/roadmap.json                            # MODIFIED: phase 2 + sunset milestones
requirements.worker.txt                             # MODIFIED: add beautifulsoup4, soupsieve
requirements.txt OR requirements-dev.txt            # MODIFIED: same
.github/workflows/test.yml                          # MODIFIED: pip install BS4
```

---

## 3. Section 1: Foundation: DAG decorator + validator extension

**Why first:** zero behavior change, unlocks parity enforcement with phase
awareness, and gives later sections a clean hook to declare ordering intent.
Also lowest risk, if something goes wrong, we roll back a single commit
with no widget or contract state to unwind.

### 3.1 `cli/pipeline.py`

A small module, ~120 LOC with the phase extension. Public API:

```python
@dataclass(frozen=True)
class Proc:
    fn: Callable[[Artifact], Artifact | None]
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    phase: frozenset[str] = frozenset({"build", "reapply"})   # default: runs in both
    assumes_on_disk: tuple[str, ...] = ()                      # for reapply-only consumers

REGISTRY: dict[str, Proc] = {}

class PipelineParityError(RuntimeError):
    """Raised when pipeline_order(phase) detects an unsatisfied @consumes in a subset."""

def processor(*, after=(), before=(), produces=(), consumes=(),
              phase=("build", "reapply"), assumes_on_disk=()) -> Callable:
    """Decorator. Registers fn in REGISTRY with full metadata.

    Decorator does NOT wrap execution: fn remains directly callable for unit tests.
    """

def pipeline_order(phase: str = "build") -> list[str]:
    """Resolve topological order from REGISTRY, filtered by phase.

    Raises KeyError if @runs_after/@runs_before references non-registered proc.
    Raises RuntimeError(CycleError) on cycle.
    Raises PipelineParityError if any @consumes in the phase-filtered subset
    is not satisfied by an earlier subset member AND not in @assumes_on_disk.
    Deterministic: ties broken alphabetically by __name__.
    """

def run_pipeline(artifact: Artifact, *, phase: str = "build",
                 subset: set[str] | None = None) -> Artifact:
    """Resolve order via pipeline_order(phase) (or explicit subset for testing)
    and invoke each processor. Returns the mutated artifact."""

def assert_pipeline_parity() -> None:
    """Validate both 'build' and 'reapply' phases resolve consistently.
    Called once at cli/build.py import to fail-fast on DAG drift."""
```

### 3.2 `Artifact` protocol

Descriptive only (no runtime check):

```python
class Artifact(Protocol):
    files: dict[str, str | bytes]
    spec: dict
    build_dir: str
    widgets_snippets: dict[str, str]            # NEW, scratch for widget emission, never written to disk
    metadata: dict[str, Any]                    # NEW, e.g. widgets_emitted_at_generation timestamp
```

### 3.3 Wrapping existing post-processors

Every top-level function in `cli/build.py` named `_ensure_*`, `_fix_*`,
`_strip_*` gets the `@processor` decorator. Order constraints mirror today's
explicit-call order. **phase metadata**: each processor declares whether it
runs in build, reapply, or both (today's subset in `reapply_post_processors`
is the source of truth for which get `phase={"build","reapply"}` vs
`phase={"build"}`).

**Explicit-registry guardrail** (replaces the loose grep-based check):

```python
# cli/pipeline.py
EXPECTED_PROCESSORS: tuple[str, ...] = (
    "_ensure_shared_db_pool",
    "_fix_tailwind_config",
    # ... 65 names total, enumerated explicitly ...
    "_run_contracts",
)
```

Test asserts every name in `EXPECTED_PROCESSORS` is in `REGISTRY` AND every
name in `REGISTRY` is in `EXPECTED_PROCESSORS`. Adding a new processor
requires both the decorator AND a constant edit; intentional friction to
catch silent registration drift.

### 3.4 Extend `cli/validate.py`

```python
VALID_FIELD_TYPES = {
    "text", "string", "integer", "float", "boolean",
    "timestamp", "datetime",
    "email", "password",
    "image_url", "url",
    "enum", "json", "foreign_key",
}
```

No other validator logic changes. `coordinator._finalize_spec` continues
to pass types through unchanged.

### 3.5 Tests for Section 1

File: `tests/test_pipeline_dag.py`

- `test_processor_decorator_registers_with_full_metadata`.
- `test_processor_decorator_preserves_callability`.
- `test_pipeline_order_is_topological`.
- `test_pipeline_order_alphabetical_tiebreak`.
- `test_unknown_runs_after_raises_keyerror`.
- `test_pipeline_order_detects_cycles`.
- `test_pipeline_order_build_phase_filters_build_only_procs`.
- `test_pipeline_order_reapply_phase_filters_reapply_only_procs`.
- `test_reapply_subset_auto_derives_from_phase`.
- `test_reapply_assumes_on_disk_satisfies_consumes`: reapply-only proc
  with `@consumes("views/admin/settings.ejs")` must declare
  `@assumes_on_disk=("views/admin/settings.ejs",)` or be preceded by a
  reapply producer.
- `test_pipeline_parity_error_on_unsatisfied_reapply_consumer`.
- `test_run_pipeline_full_invokes_all_processors`.
- `test_run_pipeline_subset_respects_subset_arg`.
- `test_assert_pipeline_parity_on_realistic_registry_succeeds`.
- `test_every_expected_processor_is_registered`: EXPECTED_PROCESSORS
  constant vs REGISTRY equality.

File: `tests/test_validate.py` (extended)

- `test_validate_accepts_extended_field_types`.
- `test_validate_rejects_unknown_field_type`.
- `test_existing_spec_fixtures_still_validate`.

### 3.6 Acceptance for Section 1

1. All existing tests pass.
2. New tests pass.
3. `pytest tests/ -q` wall-clock stays under 5 min.
4. `build_app()` and `reapply_post_processors()` produce byte-identical
   output on a canonical MW spec fixture before vs after this section.

---

## 4. Section 2: Widget registry core

**Why second:** DAG is in place; widget emission can declare itself as a
processor with clean ordering.

### 4.1 `cli/widgets/__init__.py`

```python
class Widget:
    def __init__(self, spec: dict) -> None: ...
    def render(self) -> str: ...

class UnknownWidgetError(ValueError):
    """Raised by render_field() on unregistered type."""

WIDGETS: dict[str, type[Widget]] = {}

def register(type_name: str) -> Callable[[type[Widget]], type[Widget]]: ...

def render_field(spec: dict, *, strict: bool = True) -> str: ...
```

### 4.2 `cli/widgets/widgets.py`

One dataclass per type, each `@register(...)`-decorated. 14 types:

`text, string, integer, float, boolean, timestamp, datetime, email,
password, url, enum, json, foreign_key, image_url`.

### 4.3 Sibling templates: `cli/templates/widgets/`

Large widgets keep HTML in sibling `.ejs.tmpl` files loaded at import time;
the class `.render()` does `.format(...)` substitution.

Critical: `image_upload_zone.ejs.tmpl`, copied verbatim from current
`_ensure_upload_form_ui` output so the upload-zone contract holds
by construction. Widget emits the hidden `<input type="hidden" name="...">`
that `_convert_raw_file_inputs_to_upload_zones` looks for, so that
post-processor runs as a byte-identical no-op on widget output.

### 4.4 Label humanization: `cli/widgets/labels.py`

```python
LABEL_MAP: dict[str, str] = {
    "hero_image_url": "Hero Background Image",
    # ... moved from current _humanize_settings_labels
}

def humanize(name: str) -> str: ...   # fallback: Title Case of snake_case
```

### 4.5 Archetype gate: `cli/widgets/archetype.py`

```python
# Three-state env var:
#   unset       → archetype-gated via _widgets_enabled_for(spec)
#   "true"      → force-on for ALL specs
#   "false"     → force-off for ALL specs (emergency kill)
# Any other value raises ValueError at build start.

def _widgets_enabled_for(spec: dict) -> bool:
    """Phase 1: MW archetype only (unless env override).

    Defensive lookups: uses str(spec.get('description','')).lower() etc.
    to handle reapply on specs that may not have been re-validated.
    """
```

Defensive implementation guards against missing `description`/`app_name`
keys.

### 4.6 Tests for Section 2

File: `tests/test_widgets_registry.py`

- `test_register_decorator_populates_registry`.
- `test_render_field_dispatches_by_type`: all 14 types.
- `test_render_field_unknown_type_strict_raises`: message mentions name,
  type, fix.
- `test_render_field_unknown_type_lenient_renders_placeholder`.
- `test_every_widget_emits_humanized_label`: no snake_case `<label>`.
- `test_every_image_widget_emits_dragdrop_zone`: no raw url/text/file inputs.
- `test_image_widget_emits_hidden_input_for_convert_raw_file_inputs_compat`.
- `test_image_widget_preserves_current_value`.
- `test_enum_widget_renders_select_with_choices`.
- `test_boolean_widget_renders_checkbox_with_correct_checked_state`.
- `test_foreign_key_widget_renders_ejs_loop_over_options_model`.
- `test_archetype_gate_enables_for_mw_by_description`.
- `test_archetype_gate_enables_for_mw_by_app_name`.
- `test_archetype_gate_disabled_by_default_for_other_specs`.
- `test_archetype_gate_override_via_env_true`.
- `test_archetype_gate_override_via_env_false`.
- `test_archetype_gate_rejects_invalid_env_value`.
- `test_archetype_gate_defensive_against_missing_spec_keys`.

File: `tests/test_widgets_labels.py`

- `test_humanize_canonical_keys_match_label_map`.
- `test_humanize_unknown_key_falls_back_to_title_case`.

### 4.7 Acceptance for Section 2

1. All 14 widget types render valid EJS.
2. Strict mode raises `UnknownWidgetError` with actionable message.
3. Archetype gate correct across unset/true/false/invalid states.
4. Registry importable with zero side effects beyond `WIDGETS` population.

---

## 5. Section 3: Widget emission pipeline

**Why third:** individual widgets render; compose into three target surfaces
and wire into the build pipeline.

### 5.1 `cli/widgets/forms.py`

```python
# Six-section structure ported verbatim from current canonical template.
# Option 3 per integration note C2; Python owns the layout decision.
SECTION_LAYOUT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Site Identity",   ("site_name", "site_url", "site_description")),
    ("Hero Section",    ("hero_tagline", "hero_description", "hero_image_url")),
    ("Logo & About",    ("nav_logo_url", "about_title", "about_body", "about_photo_url")),
    ("Contact Info",    ("business_name", "contact_email", "contact_phone", "contact_address")),
    ("Social Media",    ("instagram_url", "facebook_url", "youtube_url",
                         "tiktok_url", "twitter_url", "linkedin_url")),
)

def emit_admin_settings(spec: dict) -> str:
    """Emit views/admin/settings.ejs.

    Iterates SECTION_LAYOUT. For each key, widget-render gated on <% if (_has(key)) { %>.
    Keys present in spec['settings'] but not in any SECTION_LAYOUT group go to
    an auto-created 'Additional Settings' section at the end.
    Preserves top-of-file `_has`/`_v` helper definitions from current template.
    """

def emit_crud_new_edit(spec: dict, model: dict) -> tuple[str, str]:
    """Emit (new.ejs, edit.ejs) for a writable model.

    Form action is canonical /admin/<slug> (or /admin/<slug>/<%= item.id %> for edit).
    Routes are dual-mounted at /admin/<slug> by _ensure_admin_slug_mount so the
    canonical URL resolves.
    Skips read_only models (no new/edit at all).
    """

def emit_dashboard_settings_section(spec: dict) -> str:
    """Emit the 'Site Settings' CTA card for dashboard.ejs.

    Returned as a snippet: stored in artifact.widgets_snippets, not artifact.files.
    Consumed by _inject_dashboard_settings_section processor in group 5.
    """

def emit_all(artifact: Artifact) -> Artifact:
    """Top-level entrypoint.

    Writes the three surfaces into artifact.files (settings + per-model new/edit)
    and into artifact.widgets_snippets (dashboard section).
    Writes artifact.metadata['widgets_emitted_at_generation'] = <ISO timestamp>.
    Sorted iteration: sorted(spec['data_models'], key=lambda m: m['name']) for
    deterministic output across runs.
    """
```

### 5.2 Integration into `cli/build.py`

Replace the explicit 65-call block (roughly line 12585–12615) with a single
`run_pipeline(artifact, phase="build")`. Contracts are the last processor
in the DAG (see §6.4). Same for `reapply_post_processors`:
`run_pipeline(artifact, phase="reapply")`.

### 5.3 Processor annotations

```python
@processor(
    after=("_ensure_site_settings",),  # M7: settings seeded before widget emission
    before=("_ensure_upload_form_ui", "_ensure_crud_forms_complete",
            "_strip_inline_dashboard_settings_form", "_humanize_settings_labels",
            "_fix_admin_crud_link_prefix"),  # FIX C reads our output; must run after
    produces=("views/admin/settings.ejs", "views/admin/*/new.ejs",
              "views/admin/*/edit.ejs"),
    phase=("build", "reapply"),
)
def _emit_widgets(artifact): ...

@processor(
    after=("_emit_widgets",),
    produces=("server.js",),
    consumes=("server.js",),
    phase=("build", "reapply"),
)
def _ensure_admin_slug_mount(artifact):
    """Dual-mount every writable model's router at /admin/<slug>.

    For each model M with route file routes/<slug>.js, ensure server.js has
    BOTH  app.use('/<slug>', ...) AND  app.use('/admin/<slug>', requireAdmin, ...)
    If /admin/<slug> already mounted, no-op.
    """

@processor(
    after=("_emit_widgets",),
    consumes=("views/admin/dashboard.ejs",),
    phase=("build", "reapply"),
)
def _inject_dashboard_settings_section(artifact):
    """Replace or insert the dashboard's settings section with the widget-emitted
    CTA snippet from artifact.widgets_snippets['dashboard_settings_section']."""
```

When `_widgets_enabled_for(spec)` is False, `_emit_widgets` short-circuits.
Subsequent FIX B/L/M/N run on Claude output as today. When True, widget
output flows through; FIX B/L/M/N see widget markup and match nothing,
producing **byte-identical output** (tested explicitly).

`reapply_post_processors` migration path: if artifact lacks
`metadata['widgets_emitted_at_generation']` AND `_widgets_enabled_for(spec)`
returns True, `_emit_widgets` emits fresh (overwrites Claude's + post-
processed output). Explicit doc note acknowledges this visible change is
intentional: widget output is a strict improvement over Claude + regex
output.

### 5.4 Tests for Section 3

File: `tests/test_widgets_forms.py`

- `test_emit_admin_settings_includes_all_expected_sections`.
- `test_emit_admin_settings_respects_has_gating`.
- `test_emit_admin_settings_has_drag_drop_zone_for_every_image_field`.
- `test_emit_admin_settings_labels_are_all_english`.
- `test_emit_admin_settings_form_action_is_literal_admin_settings`.
- `test_emit_admin_settings_preserves_has_and_v_helpers`.
- `test_emit_admin_settings_additional_settings_section_auto_created`.
- `test_emit_crud_new_edit_skips_read_only_models`.
- `test_emit_crud_new_edit_has_csrf_token`.
- `test_emit_crud_new_edit_action_url_is_canonical_admin_prefix`.
- `test_emit_dashboard_settings_section_is_cta_not_form`.
- `test_emit_dashboard_snippet_stored_in_widgets_snippets_not_files`.
- `test_emit_all_overwrites_existing_admin_settings`.
- `test_emit_all_creates_new_edit_for_each_writable_model`.
- `test_emit_all_is_idempotent` - `emit_all(emit_all(a))` == `emit_all(a)`
  byte-for-byte.
- `test_emit_all_writes_widget_metadata_timestamp`.
- `test_emit_all_deterministic_across_data_models_order`.
- `test_ensure_admin_slug_mount_idempotent`.
- `test_ensure_admin_slug_mount_skips_readonly_models`.

File: `tests/test_mw_v17_fix_b_crud_forms.py` (modified)

- `test_fix_b_is_byte_identical_noop_on_widget_emitted_markup`.

File: `tests/test_mw_v17_fixes_lmno.py` (modified)

- `test_fix_l_is_byte_identical_noop_on_widget_emitted_dashboard`.
- `test_fix_m_is_byte_identical_noop_on_widget_emitted_labels`.
- `test_fix_n_is_byte_identical_noop_on_widget_emitted_zones`.

### 5.5 Acceptance for Section 3

1. On a canonical MW spec fixture, `widgets.emit_all(artifact)` produces
   expected shape (form actions are canonical, zones present, CTA on
   dashboard).
2. `_ensure_admin_slug_mount` ensures every widget-emitted form action
   resolves to a mounted route.
3. Running FIX B/L/M/N after widget emission is byte-identical to input
   (not just functionally equivalent).
4. `emit_all(emit_all(a))` == `emit_all(a)`: idempotent.
5. Non-MW specs unchanged (no widget emission triggered).
6. Token usage on generation pass drops ≥15% vs v19 baseline.

---

## 6. Section 4: Contract framework

### 6.1 `cli/contracts/__init__.py`

```python
class ContractViolation(AssertionError): ...

def contract(
    ejs_src: str,
    selector: str,
    *,
    count: int | None = None,
    absent: bool = False,
    path: str | None = None,
    collector: list[str] | None = None,   # if provided, violations appended instead of raised
) -> None: ...

def contains(ejs_src: str, needle: str, *, absent: bool = False,
             path: str | None = None, collector: list[str] | None = None) -> None: ...

def _parse_ejs(ejs_src: str) -> BeautifulSoup:
    """Parse EJS as HTML via html.parser.

    Fallback: if parse produces degenerate tree (e.g. unclosed <% left entire
    remainder as one text node), pre-strip <% ... %> blocks and re-parse.
    The stripped tree is used only for contracts that don't care about
    conditional structure (which is most of them).
    """
```

### 6.2 Per-view contract modules

Each exposes `def check(artifact: Artifact, *, collect_all: bool = False,
violations: list[str] | None = None) -> None`.

**`cli/contracts/admin_settings.py`**:

- Canonical literal `<form action="/admin/settings" method="POST">` present.
- CSRF token input present.
- For every spec image field: `.upload-zone[data-field=<name>]` present
  AND no raw `<input type="text|url|file">` with matching `name`.
- No `<label>` with snake_case text.
- `_v()` and `_has()` helper definitions at top.

**`cli/contracts/admin_crud.py`** (per writable model):

- `views/admin/<slug>/new.ejs` contains literal
  `<form action="/admin/<slug>" method="POST">`.
- `views/admin/<slug>/edit.ejs` contains
  `<form action="/admin/<slug>/<%= item.id %>" method="POST">` (literal
  URL prefix; the `<%= %>` inside attribute value is fine; widget canonicalizes).
- CSRF token in both.
- Image fields: drag-drop zone, no raw file/url/text inputs.
- Read-only models: no `new.ejs` / `edit.ejs` at all.

**`cli/contracts/admin_dashboard.py`**:

- No `<form action="/admin/settings">` anywhere in `views/admin/dashboard.ejs`.
- `<a href="/admin/settings">` CTA present.

**`cli/contracts/layout.py`**:

- `<nav>` with brand element.
- **U+2014 em-dash only** absent in brand nav: `nav.brand :-soup-contains("\u2014")`.
  En-dash U+2013, figure-dash U+2012, horizontal-bar U+2015 are allowed.
- Logo `<img src>` OR word-mark present.
- `<main>` landmark present.

**`cli/contracts/home.py`**:

- No live `<iframe>` pointing at YouTube (click-to-play thumbnail required).
- No dead `/register` links when `/register` route not mounted.
- No `<form action="/admin/settings">`.

**`cli/contracts/about.py`**:

- About photo has real `src`, not 1×1 SVG placeholder.

**`cli/contracts/login.py`**:

- Email/username input + password input present.
- CSRF token present.
- No hardcoded demo credentials visible.

### 6.3 `cli/contracts/run.py`

```python
def run_all(artifact: Artifact, *, collect_all: bool = False) -> None:
    """Invoke every contract module's check().

    collect_all=False (production default): re-raise first ContractViolation.
    collect_all=True (pytest): collect all violations, raise one
      aggregated ContractViolation("N violations: ...") at the end.

    Env var CAS_CONTRACTS_COLLECT=1 forces collect_all=True regardless
    of kwarg (used by pytest fixture auto-conftest).
    """
```

### 6.4 Integration into pipeline

```python
@processor(
    after=tuple(sorted(REGISTRY.keys())),   # computed at decorator apply time via deferred hook
    consumes=("views/admin/settings.ejs", "views/admin/*/new.ejs",
              "views/admin/*/edit.ejs", "views/admin/dashboard.ejs",
              "views/layout.ejs", "views/home.ejs",
              "views/about.ejs", "views/login.ejs"),
    phase=("build", "reapply"),
)
def _run_contracts(artifact):
    collect_all = os.environ.get("CAS_CONTRACTS_COLLECT") == "1"
    run_all(artifact, collect_all=collect_all)
    return artifact
```

The "runs after every other processor" semantics is handled by an
`@runs_last=True` helper on the decorator; pipeline resolver treats
`runs_last=True` procs as having implicit `after` edges from every other
registered proc.

### 6.5 BS4 dependency: all install sites

Add to every requirements file:

- `requirements.worker.txt` - `beautifulsoup4>=4.12,<5`, `soupsieve>=2.5,<3`
- `requirements.txt` (if present) or `requirements-dev.txt`: same
- `.github/workflows/test.yml`: pip install the same
- `CLAUDE.md` "Running locally" note about installing these

Smoke test in `tests/test_contracts_framework.py` imports BS4 and parses
canonical EJS fragments with embedded `<%= %>`.

### 6.6 Tests for Section 4

File: `tests/test_contracts_framework.py`

- `test_contract_selects_exact_count`.
- `test_contract_absent_passes_when_no_match`.
- `test_contract_absent_fails_when_match_exists`.
- `test_contract_default_passes_with_any_match`.
- `test_contract_default_fails_with_zero_matches`.
- `test_contract_raises_contract_violation_with_path`.
- `test_contract_appends_to_collector_when_provided`.
- `test_bs4_handles_embedded_ejs_expressions` (`<%= %>`).
- `test_bs4_handles_ejs_control_flow` (`<% if { %>…<% } %>`).
- `test_bs4_fallback_strips_ejs_on_degenerate_parse`.
- `test_contract_on_v12_admin_settings`: real EJS from `fixtures/v12_admin_settings.ejs`.
- `test_contract_on_v14_admin_settings`.
- `test_contract_on_v18_event_form_with_ternary_action`.
- `test_contract_on_v19_admin_dashboard`.
- `test_contract_attribute_literal_url_match_ignores_ejs_ternary` -
  widget canonicalizes, contract asserts literal; confirm v18's EJS
  ternary would FAIL the contract (driving the canonicalization design).
- `test_contract_malformed_ejs_fails_loudly`: unclosed `<%` produces
  a clear error, not silent zero matches.

File: `tests/test_contracts_admin_settings.py`

- `test_contract_passes_on_widget_emitted_settings`.
- `test_contract_fails_on_missing_csrf_token`.
- `test_contract_fails_on_snake_case_label`.
- `test_contract_fails_on_raw_url_input_for_image_field`.
- `test_contract_fails_on_missing_drag_drop_zone`.
- `test_contract_fails_on_missing_form_action`.

File: `tests/test_contracts_admin_crud.py`

- `test_contract_requires_new_and_edit_for_each_writable_model`.
- `test_contract_forbids_new_and_edit_for_read_only_model`.
- `test_contract_fails_on_orphan_file_input`.
- `test_contract_fails_on_non_canonical_action_url`, if widget emitted
  `/events` instead of `/admin/events`, contract fails.
- `test_contract_matches_action_url_literal_via_widget_canonicalization`.

File: `tests/test_contracts_admin_dashboard.py`

- `test_contract_fails_if_dashboard_has_settings_form`.
- `test_contract_passes_with_cta_link_only`.

File: `tests/test_contracts_layout.py`

- `test_contract_fails_on_u2014_em_dash_in_brand_nav`.
- `test_contract_passes_with_u2013_en_dash_in_number_range`.
- `test_contract_fails_on_missing_main_landmark`.
- `test_contract_passes_on_canonical_mw_layout`.

File: `tests/test_contracts_non_form_surfaces.py`

- `test_home_contract_fails_on_live_youtube_iframe`.
- `test_home_contract_fails_on_dead_register_link_when_route_missing`.
- `test_about_contract_fails_on_1x1_svg_photo`.
- `test_login_contract_fails_on_hardcoded_demo_credentials`.

### 6.7 Acceptance for Section 4

1. `run_all()` passes on v19 snapshot artifact loaded from disk + re-
   emitted via widgets.
2. Injected regressions fail the corresponding contract with clear message.
3. `run_all()` wall-clock on v19-size artifact < 2 seconds.
4. BS4 + soupsieve pinned in all requirements files; Railway worker rebuild
   succeeds; CI green.

---

## 7. Section 5: Prompt pruning + safety-net wiring

### 7.1 Prompt changes in `cli/build.py::_build_explicit_prompt`

Remove sections that ask Claude to generate:

- `views/admin/settings.ejs` markup.
- Admin CRUD new/edit form specifics.
- Dashboard settings section specifics.

Replace with:

> "The Python widget registry emits `views/admin/settings.ejs`,
>  `views/admin/<slug>/new.ejs`, `views/admin/<slug>/edit.ejs`, and the
>  settings section of `views/admin/dashboard.ejs`. Do NOT generate these
>  files: they will be overwritten."

HARD UX RULES section stays (applies to non-form surfaces Claude still writes).

### 7.2 `WIDGETS_ENABLED` env var: documented tri-state

`cli/widgets/archetype.py` top-of-file comment enumerates:
- Unset → archetype-gated via `_widgets_enabled_for(spec)`.
- `"true"` → force-on.
- `"false"` → force-off (emergency kill).
- Anything else → `ValueError` at build start.

`CLAUDE.md` worker-env-vars list gets the entry.

### 7.3 Safety-net mode: byte-identical proof

The safety-net claim requires:

1. Unit tests per FIX (B/L/M/N): byte-identical output when run on widget-
   emitted markup (§5.4).
2. Integration test `test_widgets_safety_net.py`:
   - Run pipeline with `WIDGETS_ENABLED=true` on canonical MW spec.
   - Capture artifact A.
   - Run pipeline with `WIDGETS_ENABLED=false` on same spec (reads
     recorded Claude fixture from `tests/fixtures/claude_mw_v19_raw.json`).
   - Capture artifact B.
   - Assert: `contracts.run_all(A)` passes AND `contracts.run_all(B)` passes.
   - Assert: `_fix_admin_crud_link_prefix(A)` == A byte-for-byte.
   - Assert: `_ensure_crud_forms_complete(A)` == A byte-for-byte.
   - Assert: `_strip_inline_dashboard_settings_form(A)` == A byte-for-byte.
   - Assert: `_humanize_settings_labels(A)` == A byte-for-byte.
   - Assert: `_ensure_upload_form_ui(A)` == A byte-for-byte.

### 7.4 Failure mode: widget emits bad markup

Widget bug → contract fails → build fails before preview. Safety net
(FIX B/L/M/N) can't help because widget ran first. Acceptable: widget
bugs are louder and easier to fix than silent regressions.

### 7.5 Tests for Section 5

File: `tests/test_widgets_safety_net.py`

- `test_pipeline_with_widgets_enabled_passes_contracts`.
- `test_pipeline_with_widgets_disabled_on_claude_fixture_passes_contracts`
  - loads `fixtures/claude_mw_v19_raw.json`, runs full pipeline, asserts
  contracts.
- `test_each_safety_net_fix_is_byte_identical_noop_on_widget_output`.

File: `tests/test_build_prompt.py` (extended)

- `test_prompt_no_longer_describes_admin_settings_markup`.
- `test_prompt_retains_hard_ux_rules_for_non_form_surfaces`.

File: `tests/test_safety_net_catches_known_past_regressions.py`

- `test_safety_net_catches_v19_inline_settings_on_dashboard`.
- `test_safety_net_catches_v14_timezone_utc_as_cdt`.
- `test_safety_net_catches_v13_admin_settings_paths_disconnected`.

Each test injects a known past regression into a spec or intermediate
artifact and asserts the contract suite flags it.

### 7.6 Acceptance for Section 5

1. Widgets-disabled path exercises full FIX B/L/M/N and passes contracts
   using real Claude fixture.
2. Widgets-enabled path passes contracts with widget output.
3. FIX B/L/M/N are byte-identical no-ops on widget output.
4. Prompt diff removes admin-settings/CRUD instructions.
5. Token count on generation-pass prompt drops ≥10%.

---

## 8. Section 6: Golden-file blessing + rollout

### 8.1 Golden file layout

```
tests/
└── golden/
    ├── medicine-waves-canonical/
    │   ├── spec.json
    │   ├── artifact.snapshot.json          # filename → sha256 map
    │   └── README.md
    ├── mw-logo.png                         # binary, byte-identical check
    └── non-mw-canonical/                   # phase 2
```

### 8.2 Makefile target

```makefile
.PHONY: bless-golden
PYTHON := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

bless-golden:
    @if [ -z "$(SPEC)" ]; then \
        echo "usage: make bless-golden SPEC=medicine-waves-canonical"; \
        exit 1; \
    fi
    @$(PYTHON) scripts/bless_golden.py $(SPEC)
    @echo ""
    @echo "Golden files regenerated for $(SPEC)."
    @echo "Review:  git diff tests/golden/$(SPEC)/"
    @echo "Commit:  git commit -m 'bless: $(SPEC) - <reason>'"
```

### 8.3 `scripts/bless_golden.py`

- Refuses to run on dirty working tree (prevents blessing WIP).
- Loads `tests/golden/<name>/spec.json`.
- Runs pipeline in-memory.
- Recomputes sha256 for every file in `artifact.files`.
- Writes `tests/golden/<name>/artifact.snapshot.json`.
- Summary to stdout.

### 8.4 Pre-push hook golden enforcement

`.githooks/pre-push` rejects pushes that include staged changes to
`tests/golden/**` when the most-recent commit message doesn't start with
`bless:`. Escape hatch: `CAS_PROMOTE=1`.

**PR template addition:** checkbox "does this touch `tests/golden/`?
If yes: [ ] maintainer approved this design change".

### 8.5 Pre-commit `failure_kb.json` parse guard

`.githooks/pre-commit` adds:

```bash
if git diff --cached --name-only | grep -q "^cli/failure_kb.json$"; then
    python3 -c "import json; json.load(open('cli/failure_kb.json'))" || \
      { echo "ERROR: failure_kb.json is invalid JSON"; exit 1; }
fi
```

### 8.6 Docs updates

**`docs/internals/POST_PROCESSOR_ORDERING.md`**:

- Group 5 rewritten: "FIX B/L/M/N are retained as safety nets only, widget
  registry (see `docs/internals/WIDGETS_AND_CONTRACTS.md`) is the primary
  path."
- Add "Contract coverage policy" section.
- Add "DAG decorator usage + phase metadata + parity enforcement" section.

**New:** `docs/internals/WIDGETS_AND_CONTRACTS.md`, primer covering:
widget registry usage, adding a new type, contract authoring, golden file
blessing, DAG decorator rules.

**`CLAUDE.md`**:

- Update test count (3,496 → new number).
- Mention widgets/contracts/DAG layers.
- Add `WIDGETS_ENABLED` to worker-env-vars list.

### 8.7 `coordinator/roadmap.json` entries

- `"b-plus-c-phase-1-ship"`: tracked during implementation.
- `"b-plus-c-sunset"`: blocked on 2 green MW + 1 green non-MW.
- `"b-plus-c-phase-2-universal"`: blocked on 3 archetypes green.

### 8.8 `cli/failure_kb.json` entries

One `UnknownWidgetError` entry added in Section 5 ship.
Four sunset markers added in sunset PR.

### 8.9 Tests for Section 6

File: `tests/test_golden_files.py`

- `test_golden_file_hash_matches_pipeline_output`.
- `test_bless_golden_script_regenerates_deterministically`.
- `test_bless_golden_script_refuses_on_dirty_tree`.
- `test_golden_binary_assets_unchanged`.

### 8.10 Acceptance for Section 6

1. `make bless-golden SPEC=medicine-waves-canonical` runs successfully.
2. `pytest tests/test_golden_files.py -q` passes.
3. Pre-push hook rejects golden commits without `bless:` prefix.
4. Pre-commit hook rejects `failure_kb.json` with invalid JSON.
5. Docs + roadmap updated.

---

## 9. Cross-cutting concerns

### 9.1 Rollout order

1. **Commit 1: DAG decorator + validator extension (§3).** Zero behavior
   change. Verify byte-identical output on canonical MW spec fixture.
2. **Commit 2: Widget registry core (§4).** Registry + 14 widgets
   importable. No integration yet.
3. **Commit 3: Widget emission pipeline (§5).** `emit_all()` wired.
   `_ensure_admin_slug_mount` added. FIX B/L/M/N still run.
4. **Commit 4: Contract framework (§6).** `contracts.run_all()` at end
   of pipeline. BS4 added to all requirements.
5. **Commit 5: Prompt pruning + safety-net tests (§7).**
6. **Commit 6: Golden files + Makefile + hooks + docs (§8).**
7. **Staging promotion** (through a push wrapper script that has since been removed).
8. **Primary validation: disk-only reapply on v19 snapshot artifact.** Run
   widget emission + contracts against `builds/medicine-waves-v19-*/`
   artifact on disk, no fresh build required. Success criteria (contracts
   green, byte-identical FIX B/L/M/N on widget output, idempotent
   `emit_all`) validated without a fresh Claude run.
9. **Fresh v20 MW test build: confidence step, not primary gate.** Queue
   after disk-only validation passes. Customer-visible but marked test.
10. **Observe 2 green MW builds + 1 green non-MW build.** During this
    period, `test_safety_net_catches_known_past_regressions.py` also runs
    per-build.
11. **Sunset PR.** Deletes FIX B/L/M/N + their tests.
12. **Phase 2 plan** (separate `/deep-plan` session).

### 9.2 Testing strategy

- Every section has its own test files.
- `pytest tests/ -q` locally after every commit (pre-push hook doesn't
  run pytest).
- `scripts/run_web_tests.sh` (named `run_web_tests_parity.sh` when this plan was written) before every push (pre-push DOES run
  this).
- Wall-clock budget: total pytest suite stays under 5 min.
- Contract suite completes in <2s on v19-size artifact.
- `CAS_CONTRACTS_COLLECT=1` automatically set in pytest conftest for
  developer UX (multiple violations reported per run).

### 9.3 Observability

- `emit_build_trace(output_dir, "widgets.emit_all.start", {...})` +
  `.end` with payload dict (matches existing signature).
- `emit_build_trace(output_dir, "contracts.run_all.start", {...})` + `.end`.
- Contract failures emit structured log line with `selector`, `path`,
  `expected`, `actual`.
- `coordinator/roadmap.json` tracks milestones.

### 9.4 Rollback paths

- **Emergency disable:** `WIDGETS_ENABLED=false` on worker env. Old pipeline
  runs end-to-end; contracts on non-form surfaces still run but can't
  regress vs today.
- **Full rollback:** revert commits 4–6 (contracts, prompt, golden). Commits
  1–3 stay (zero behavior change).
- **Sunset rollback:** revert sunset PR. Widgets + FIX B/L/M/N
  both active; byte-identical no-op tests keep them compatible.

---

## 10. Sunset milestone (separate PR, after 2+1 green builds)

Not part of phase 1. Documented here for downstream clarity.

### 10.1 Scope

Delete:

- `_ensure_crud_forms_complete` (FIX B).
- `_strip_inline_dashboard_settings_form` (FIX L).
- `_humanize_settings_labels` (FIX M). Label map fully lives in
  `cli/widgets/labels.py`.
- `_ensure_upload_form_ui` (FIX N). Orphan file-input strip becomes a
  contract absence check.
- `tests/test_mw_v17_fix_b_crud_forms.py`.
- `tests/test_mw_v17_fixes_lmno.py` (M/N/L classes: FIX O stays or moves).

Retain: A, C, D, E, F, G, H, I, J, K, O.

### 10.2 Acceptance

1. `pytest tests/ -q` green after deletions.
2. `scripts/run_web_tests.sh` green (`run_web_tests_parity.sh` when this plan was written).
3. Full MW build pipeline produces widget-emitted markup + contracts pass.
4. `failure_kb.json` updated with `retired_in=<commit-sha>`.
5. `POST_PROCESSOR_ORDERING.md` group 5 section rewritten.
6. Expected test count documented in sunset PR description
   (e.g. "3,496 → 3,4XX, delta explained by FIX B/L/M/N test removal").

---

## 11. Summary for the reader

**What we're doing:** replacing Claude's emission of three regression-prone
admin surfaces with Python-emitted templates driven by `spec.data_models`,
adding a BS4 contract layer that fails builds before preview, and
introducing a phase-aware DAG decorator that enforces parity between
`build_app()` and `reapply_post_processors()` by construction.

**What we're keeping:** 11 of 15 post-processors stay. They handle
non-form concerns (seed images, route link drift, design system, logo
bundling) outside B's scope.

**What we're retiring:** FIX B, L, M, N, in a separate PR after 2 green
MW + 1 green non-MW build soak period.

**What's out of scope:** phase 2 universal rollout, phase 3 non-form
template rewrite, `failure_kb` module rewrite, CI infrastructure beyond
BS4 install sites.

**The north star:** after this ships, MW builds stop regressing on admin
forms. Each new regression lands with a contract that blocks future
recurrence. The pipeline becomes a DAG we can reason about, not a
line-ordered block we're afraid to reorder. And we never again have to
choose between `build_app()` order and `reapply_post_processors()` order; 
they share a registry and the DAG enforces it.
