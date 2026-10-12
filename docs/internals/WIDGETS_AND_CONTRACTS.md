# Widgets and Contracts

**Audience:** anyone touching `cli/widgets/`, `cli/contracts/`,
`cli/pipeline.py`, `tests/golden/`, or the admin-surface regex
post-processors in `cli/build.py`.

**Scope:** B+C pipeline rewrite. Widget emission is **universal since
2026-04-20**: every spec gets canonical admin surfaces. The earlier
phase-1 archetype gate (MW-only) was flipped after two consecutive
non-MW customer builds failed the `admin_settings` +
`admin_crud` contracts with `file missing`. The `WIDGETS_ENABLED=false` env var is retained as an
emergency kill switch; no other build-time knobs exist.

## 1. What this replaces

For every spec, widget emission + declarative contracts replace four
of the most regression-prone regex post-processors:

- `_ensure_crud_forms_complete` (FIX B): injected image_url hidden inputs.
- `_humanize_settings_labels` (FIX M): rewrote snake-case labels.
- `_strip_inline_dashboard_settings_form` (FIX L): deduped inline forms.
- `_ensure_upload_form_ui` (FIX N): converted `<input type=url>` into
  drag-drop zones.

The four functions are retained as safety nets for the
`WIDGETS_ENABLED=false` rollback path and become byte-identical no-ops
on widget-emitted output. Sunset is described in
`docs/plans/b-plus-c/claude-plan.md` (section 10).

### 1.1 Widget marker contract

Widget-emitted admin surfaces (`views/admin/settings.ejs` and every
`views/admin/<slug>/new.ejs` / `edit.ejs`) carry the HTML comment marker

    <!-- widgets:admin-form -->

at the top of the file. The marker is a signal to downstream
post-processors that the file is widget-owned and should not be
rewritten. Today only one post-processor reads the marker:

- `_fix_admin_crud_link_prefix` (FIX C) strips `/admin/<slug>` from
  href and action attributes on Claude-emitted admin list views (a v13
  regression-fix for `<a href="/admin/events/new">` linking to a
  `/events/new` route). Widgets deliberately emit
  `<form action="/admin/<slug>">` because `_ensure_admin_slug_mount`
  dual-mounts every router at `/admin/<slug>` alongside `/<slug>`, and
  `cli/contracts/admin_crud.py` asserts the literal `/admin/<slug>`
  URL. Without the marker, FIX C would rewrite widget-emitted form
  actions back to `/<slug>` and the contract would fail.

The marker is an HTML comment (not EJS), so it survives BS4 parsing
untouched and is invisible in the rendered DOM. New post-processors
that want to participate should import
`cli.widgets.forms.WIDGET_ADMIN_MARKER` (the canonical constant) and
check for substring presence before rewriting admin EJS.

### 1.2 Runtime wiring (artifact emission alone is NOT enough)

Writing a widget file to `artifact.files['views/admin/<slug>/new.ejs']`
is only half the job. Claude typically emits `routes/<slug>.js` with a
plain `res.render('<slug>/new', ...)` call, which resolves to
`views/<slug>/new.ejs` (the public template). Dual-mount via
`_ensure_admin_slug_mount` at `/admin/<slug>` reuses the same router,
so BOTH mounts render Claude's public view; the widget-emitted admin
view is orphaned on disk.

The fix is a router-rewrite post-processor,
`_rewrite_admin_router_template_switch` (cli/build.py), that detects
`res.render('<slug>/new', ...)` / `res.render('<slug>/edit', ...)` in
Claude-generated admin CRUD route files and inserts a conditional:

    const viewPrefix = req.baseUrl && req.baseUrl.startsWith('/admin/')
      ? 'admin/'
      : '';
    res.render(viewPrefix + '<slug>/new', { ... });

At runtime, `/admin/<slug>/new` resolves `viewPrefix` to `'admin/'` and
renders the widget-emitted view. `/<slug>/new` (public path) keeps
rendering Claude's template unchanged.

**Why this matters:** `contracts.run_all(artifact)` reads
`artifact.files` and passes when the widget file has canonical
`/admin/<slug>` actions. But the CONTRACT has no visibility into
runtime routing. Without the router rewrite, the contract can be
green while the running app serves Claude's form. Always pair widget
emission with (a) a router rewrite processor and (b) a runtime
integration test that actually HTTP-GETs `/admin/<slug>/new` and
asserts the widget marker is present in the response.

Artifact-only contracts are insufficient because they say nothing about what
the widget runtime actually emits.

## 2. Widget registry usage

`cli/widgets/widgets.py` registers one `Widget` subclass per accepted
field type via `@register("<type>")`. Fourteen types ship today, matching
`cli/validate.py::VALID_FIELD_TYPES`:

- Text-like: `text`, `string`, `url`, `email`, `password`, `json`.
- Numeric/boolean: `integer`, `float`, `boolean`.
- Temporal: `timestamp`, `datetime`.
- Structured: `enum`, `foreign_key`.
- Media: `image_url` (renders as a drag-drop zone, not a URL input).

The top-level dispatcher is `cli.widgets.render_field(field_spec)`: it
looks up the registry by `field_spec["type"]`, raises `UnknownWidgetError`
on an unrecognised type, and returns rendered EJS. Sibling template files
live under `cli/templates/widgets/` with a `.ejs.tmpl` convention so
diffs stay reviewable.

Layout composition is owned by `cli/widgets/forms.py::emit_all`:

- `views/admin/settings.ejs` is composed from the `SECTION_LAYOUT`
  tuple plus widget output per key in `spec.settings`.
- `views/admin/<slug>/new.ejs` and `edit.ejs` are composed per writable
  `spec.data_models[*]`, sorted alphabetically for determinism.
- A dashboard CTA snippet is stashed on
  `artifact.widgets_snippets['dashboard_settings_section']` for
  `_inject_dashboard_settings_section` to splice in later.

## 3. Adding a new widget type

1. **Spec validator:** add the type string to `VALID_FIELD_TYPES` in
   `cli/validate.py`.
2. **Widget class:** implement a `Widget` subclass in
   `cli/widgets/widgets.py` and decorate with `@register("<type>")`.
   Follow the existing conventions: `render(field_spec, value_expr)`
   returns EJS with widget-owned IDs and Tailwind classes.
3. **Label map:** if the type introduces a new humanization pattern, add
   it to `cli/widgets/labels.py::LABEL_MAP`.
4. **Tests:** add a case to `tests/test_widgets_registry.py` (happy path
   + round-trip determinism) and extend
   `tests/test_widgets_forms.py::test_emit_all_*` if the new type changes
   which sections render.
5. **Contract:** if the type has a per-surface UX requirement (e.g.
   "renders as a drag-drop zone"), add or extend a contract in
   `cli/contracts/<surface>.py` so the property is asserted, not just
   hoped for.
6. **Golden re-bless:** run `make bless-golden SPEC=medicine-waves-canonical`,
   review the diff, and commit with a `bless:` prefix (see section 5).

## 4. Contract authoring

Contracts live in `cli/contracts/`, one module per surface
(`admin_settings`, `admin_crud`, `admin_dashboard`, `layout`, `home`,
`about`, `login`). Each exposes `check(artifact, collect_all, violations)`
and is dispatched alphabetically by `cli/contracts/run.py::run_all`.

Two primitives:

- `contract(ejs_src, selector, count=N)`: BS4 CSS select + count
  assertion. Raises `ContractViolation` when the count does not match.
- `contains(ejs_src, needle, absent=True)`: substring presence/absence
  check (used for EJS snippets that BS4 would garble, like `<%= ... %>`
  expressions in attribute values).

Example: to forbid em-dashes in the brand nav (a brand-voice
rule), a `layout` contract could read:

```python
def check(artifact, *, collect_all, violations):
    layout = _get_file(artifact, "views/layout.ejs")
    contains(
        layout,
        chr(0x2014),  # em-dash
        absent=True,
        context="views/layout.ejs brand nav",
        collect_all=collect_all,
        violations=violations,
    )
```

**EJS strip fallback:** when a selector needs to match inside EJS
interpolation, the contracts strip `<% ... %>` blocks with a regex before
feeding the source to BS4. The stripped form is cached per-call so the
BS4 parse only runs once per file.

## 5. Golden file blessing

`tests/golden/medicine-waves-canonical/artifact.snapshot.json` is a
`{filename: sha256hex}` map for every file the pipeline produces from
`spec.json`. Its counterpart test
`tests/test_golden_files.py::test_golden_file_hash_matches_pipeline_output`
re-runs the pipeline in-memory and fails if any hash drifts.

To intentionally change the pinned output:

1. Run `make bless-golden SPEC=medicine-waves-canonical`.
2. Review `git diff tests/golden/medicine-waves-canonical/`.
3. Commit with a `bless:` message prefix (e.g.
   `bless: medicine-waves-canonical - new image-crop widget`).

Pre-push gate: `.githooks/pre-push` refuses any push whose commits touch
`tests/golden/**` without a `bless:` prefix on every commit in the
range. The only sanctioned bypass is `CAS_PROMOTE=1` for
docs/CI/hooks/tests-only pushes that incidentally re-stage a fixture.

Determinism: `scripts/bless_golden.py` refuses to run on a dirty working
tree so WIP never lands inside a bless commit. The bootstrap
`BLESS_GOLDEN_ALLOW_DIRTY=1` escape hatch exists only for the first-ever
bless (documented in the script's docstring; do not enable in CI).

## 6. DAG decorator rules

Every post-processor is registered in `cli/pipeline.py::REGISTRY` via
`@processor(...)`. Metadata fields:

- `phase={"build", "reapply"}`, which pipeline phases include the
  processor. A build-only setup step has `phase=("build",)`.
- `after=(...)` / `before=(...)`: name-level ordering edges. References
  to unknown names raise `KeyError` at import.
- `produces=(...)` / `consumes=(...)`: path-level data flow. The parity
  checker asserts every consumed path has an in-subset producer.
- `assumes_on_disk=(...)`: for reapply-only consumers, the list of paths
  expected to exist on disk from an earlier build. The parity checker
  treats these as satisfied without demanding an in-subset producer.

Ordering ties inside a topologically-ready group break alphabetically
by function name, so `pipeline_order(phase)` is reproducible across
Python process runs.

Parity is enforced at `cli/build.py` import time by
`cli/pipeline.py::assert_pipeline_parity()`, which resolves both the
build and reapply phases and raises on any inconsistency. Drift
surfaces at pytest collection and worker startup.

When adding a new `@processor`, mirror the canonical call order in
`cli/pipeline.py::EXPECTED_PROCESSORS` (the explicit-registry guardrail
tuple). This small amount of friction prevents silent registration
drift and forces the implementer to choose phase metadata deliberately.

## 7. Env-var tri-state: `WIDGETS_ENABLED`

Read on every `_widgets_enabled_for(spec)` call (no caching) by
`cli/widgets/archetype.py`:

| Value    | Behavior                                              |
|----------|-------------------------------------------------------|
| unset    | Universal (every spec emits widget admin surfaces)    |
| `"true"` | Same as unset (explicit on for audit-log clarity)     |
| `"false"`| Emergency kill switch; legacy regex path for this build |

Any other value raises `ValueError` at build start. This tri-state
deliberately avoids the "WIDGETS_ENABLED=1/yes/on/True" drift trap
where truthy-looking strings silently flip to the wrong branch.
Production worker leaves this unset so every spec flows through
widgets. The `"false"` override lets an operator rescue a single build
without redeploying the worker.

## 8. Grid-item sizing gotcha (`<figure>` collapses to 0x0)

**Symptom:** A `<figure>` placed as a direct child of a flex/grid
container (e.g. `offerings/photo-gallery-grid`, `staff-card-grid`)
collapses to `0 x 0` width; its inner image disappears with it.
Reproduces most reliably below the mobile single-column breakpoint
(viewport `< 880px` was where this was first noticed in the
reference design pass on 2026-05-11).

**Cause:** A circular intrinsic-size resolution. When the figure has
no explicit width and its only sized child carries
`width: 100% + aspect-ratio: N`, the browser computes the figure
width from its children, but the only child needs the parent width
to resolve `100%`. Some browsers (Safari + recent Chrome) resolve
the cycle to `0`, others (Firefox) fall back to the available track
width. The result is inconsistent and on the mobile single-column
path, frequently catastrophic.

**Fix:** A baseline `figure { margin: 0; width: 100%; min-width: 0 }`
rule in `compose/build.py::emit_public_assets()` plus a parallel
`figure { width: 100% }` re-assertion inside the
`@media (max-width: 580px)` block. The `width: 100%` is the
load-bearing piece: it breaks the cycle by giving the figure an
explicit size from its parent track. `min-width: 0` is the
defense-in-depth piece: it prevents intrinsic-min-content sizing
from re-introducing the zero-width state in flex containers when
the child overflows. Tests:
`compose/tests/test_build_pipeline.py::test_figure_base_rule_has_width_100_to_prevent_grid_item_collapse`.

**Widget authoring rule:** when adding a new widget that places a
`<figure>` as a flex/grid child, do NOT rely on the child's
`w-full` / `width: 100%` to size the figure. Either set the figure
width explicitly in the widget, or rely on the global base rule. If
you need to override (e.g. `<figure>` inside a static
`max-w-md mx-auto` container where `width: 100%` would over-stretch),
use a wrapper `<div>` to carry the constraint and let the figure
itself stay `width: 100%`.
