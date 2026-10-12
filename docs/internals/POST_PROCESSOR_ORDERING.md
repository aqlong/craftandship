# Post-Processor Pipeline Dependency Graph

**Purpose:** Canonical ordering of post-processors in `cli/build.py::build_app`
and `cli/build.py::reapply_post_processors` (both resolve the order from the `@processor` registry in `cli/pipeline.py`; the group lists below are the human-readable form of those edges). Reorderings break silently, 
this doc + the dependency-order tests in `tests/test_mw_v17_fix_*.py::
TestPipelineWiring::test_runs_*_before_*` are the contract.

## Invariants

The pipeline is organized into 7 groups. Invariants below MUST hold across
any refactor.

### 1. Foundation (routes, views, basic completeness)

- `_ensure_shared_db_pool`
- `_fix_tailwind_config`
- `_fix_csrf_pattern`
- `_ensure_validate_csrf_export`
- `_fix_circular_imports`
- `_ensure_route_views`
- `_strip_demo_credentials`
- `_fix_login_form_fields`
- `_ensure_login_completeness`
- `_ensure_register_completeness`
- `_ensure_admin_mount`
- `_ensure_error_handling`
- `_strip_process_exit_on_db`

### 2. Seed data (schema, dedup, column-fix, images)

Order-critical subgroup:
- `_join_multiline_inserts` → `_split_seed_inserts` → `_dedup_seed_entries`
- `_ensure_gallery_dedup`
- `_fix_seed_column_mismatch` (first pass)
- `_fix_seed_uuid_id`

### 3. Views + structure + mobile/responsive

- `_fix_view_html_shells`
- `_fix_view_slug_paths`
- `_ensure_logout_form`
- `_fix_null_property_access`
- `_fix_brand_nowrap`
- `_ensure_mobile_nav` → `_ensure_mobile_responsive`
- `_ensure_tailwind_compiled`

### 4. Design system (fonts, colors, nav, premium CSS)

- `_ensure_font_loading`
- `_ensure_color_consistency`
- `_fix_admin_dark_stat_cards` → `_fix_dark_section_text` → `_fix_light_on_light_text`
- `_ensure_nav_glass` → `_ensure_nav_solid_surface` → `_ensure_nav_glassmorphism`
- `_ensure_logo_placement`
- `_ensure_stock_photos` **MUST run before** `_ensure_model_seed_images` (FIX A needs stock paths in files list)
- `_ensure_model_seed_images` (v17 FIX A)
- `_ensure_nav_logo` → `_strip_broken_logo_refs` → `_ensure_icon_helper`
- `_ensure_video_embeds` **MUST run before** `_ensure_home_video_click_to_play` (FIX G consumes iframe output)
- `_ensure_home_video_click_to_play` (v17 FIX G)
- `_remove_homepage_video_section`
- `_ensure_premium_css`
- `_ensure_accent_color_variety`
- `_ensure_stat_card_accent_variety` (v17 FIX I)
- `_ensure_about_photo_fallback` (v17 FIX J)
- `_ensure_mw_archetype_logo_bundled` (v17 FIX O) **MUST run before** `_ensure_nav_logo_default_seed` (FIX K consumes the bundled logo)
- `_ensure_nav_logo_default_seed` (v17 FIX K)

### 5. Site settings + content forms (WIDGET REGISTRY IS NOW THE PRIMARY PATH)

**B+C phase 1 status:** for MW-archetype specs, the admin settings view,
admin CRUD new/edit forms, and the dashboard settings CTA section are now
produced by `cli/widgets/forms.py::emit_all` (registered as
`_emit_widgets`) directly from `spec.data_models`. The four post-processors
in this group that used to normalize Claude output for these surfaces; 
`_ensure_crud_forms_complete` (FIX B), `_strip_inline_dashboard_settings_form`
(FIX L), `_humanize_settings_labels` (FIX M), `_ensure_upload_form_ui`
(FIX N): are **retained as safety nets only**. They still fire on
Claude-generated admin templates when the archetype gate is off
(`WIDGETS_ENABLED=false` or non-MW archetype), but become byte-identical
no-ops on widget-emitted output.

Sunset is described in
`docs/plans/b-plus-c/claude-plan.md` (section 10). See
`docs/internals/WIDGETS_AND_CONTRACTS.md` for widget authoring, contract
authoring, and the golden-file blessing workflow.

The canonical order (with widget-registry primary path noted):

1. `_ensure_site_settings` **injects** the admin/settings.ejs template
2. `_ensure_settings_in_views` + `_ensure_hero_settings_binding` + `_ensure_contact_email`
3. `_ensure_upload_route` (creates POST /api/uploads)
4. `_emit_widgets` (B+C phase 1): **primary path** for MW: rewrites
   `views/admin/settings.ejs` + `views/admin/<slug>/new.ejs` + edit.ejs
   from `spec.data_models` via the widget registry. Gated by
   `cli.widgets.archetype._widgets_enabled_for`.
5. `_ensure_crud_forms_complete` (v17 FIX B): **safety net** when widgets
   are off; no-op on widget output.
6. `_fix_route_link_drift` (v17 FIX C)
7. `_strip_inline_dashboard_settings_form` (v17 FIX L): **safety net** for
   duplicate dashboard forms; no-op on widget output.
8. `_humanize_settings_labels` (v17 FIX M): **safety net** for
   snake-case labels; no-op on widget output (labels already humanized).
9. `_ensure_upload_form_ui` (v17 FIX N): **safety net** CONSUMES hidden
   inputs when widgets are off and converts to drag-drop zones; no-op on
   widget output (widgets emit zones directly).
10. `_strip_redundant_image_upload_forms`
11. `_fix_admin_crud_link_prefix` (skips files carrying the
    `<!-- widgets:admin-form -->` marker so widget-emitted
    `views/admin/<slug>/new.ejs|edit.ejs` keep their canonical
    `/admin/<slug>` form actions; rewrites Claude-emitted admin list
    views that still need the `/admin/` prefix stripped) + `_strip_dead_signup_links` + `_normalize_admin_view_paths`
12. `_enforce_admin_view_path_convention`
13. `_ensure_admin_model_handlers`
14. `_ensure_settings_middleware_contract`
15. `_convert_raw_file_inputs_to_upload_zones`
16. `_ensure_event_time_helper`
17. `_ensure_tz_aware_datetime_schema`
18. `_verify_template_middleware_contract`
19. `_ensure_model_seed_images` (v17 FIX A, second pass for reapply safety)
20. `_fix_delete_button_styling`

### 6. Runtime + safety (CSP, canonical, auth, email)

- `_ensure_railwayignore`
- `_fix_seed_admin_email`
- `_fix_seed_column_mismatch` (second pass)
- `_ensure_magic_link_auth` → `_ensure_magic_link_hash`
- `_ensure_resend_from_address`

### 7. Quality-sprint layer

- `_fix_csp_cdn_sources` → `_fix_cdn_script_graceful`
- `_fix_locale_string_options`
- `_strip_require_from_ejs`
- `_ensure_resilient_migrate`
- `_ensure_minimum_seed_rows`
- `_ensure_compression` → `_ensure_static_caching` → `_ensure_lazy_loading`
- `_strip_duplicate_confirm_handlers`
- `_ensure_delete_confirm_single_handler` (v17 FIX D) **MUST run before** `_ensure_submit_guard`
- `_ensure_button_design_system` (v17 FIX E)
- `_ensure_submit_guard`
- `_ensure_dynamic_canonical`
- `_ensure_csrf_import`
- `_ensure_mobile_menu_close`
- `_ensure_skip_to_content`

## How to add a new post-processor

1. Identify which group (1–7) it belongs to
2. If it CONSUMES output of another post-processor, it must run AFTER
3. If it PRODUCES input for another post-processor, it must run BEFORE
4. Add a dependency-ordering test in `tests/test_mw_v17_fix_*.py` with
   `src.find(...)` positional assertions (see FIX G's
   `test_runs_after_ensure_video_embeds` as the template)
5. Register it with `@processor(after=..., phase=...)`. The default phase is `('build', 'reapply')`, which makes both `build_app` and `reapply_post_processors` run it (both call `run_pipeline`, which resolves the order from the registry); use `phase=('build',)` only for setup that must not re-run after a fix loop. Also add its name to `EXPECTED_PROCESSORS` in `cli/pipeline.py`.

## Contextvar dependency

`_CURRENT_BUILD_DIR` is a `contextvars.ContextVar` bound to the current
build's output directory. Any post-processor that needs disk access for
binary content (e.g. FIX A's disk-scan fallback, FIX J's about-photo
discovery, FIX K's logo scan) reads the contextvar.

This is set in `build_app` around the whole post-processor block AND in
`reapply_post_processors` around the same block. Editing either site to
remove/reset the contextvar prematurely will break disk-scan fallbacks.

## Binary-write safety

`reapply_post_processors`' write-back loop MUST handle `bytes`/`bytearray`
content separately from `str` - `Path.write_text` raises `TypeError` on
bytes. FIX O (MW logo bundle) returns binary content; writing as bytes is
handled at the branch point.

## Contract coverage policy

Every new UX regression pattern gets a BS4 contract in `cli/contracts/`
BEFORE a regex post-processor is added. Regex is admitted only as a
last resort with bounded complexity.

Rationale: regex on LLM output is a shifting target. A contract asserts the
property we actually care about (e.g. "every image-type field renders as
a drag-drop zone, never as `<input type=url>`") and runs on widget-emitted
AND Claude-emitted output uniformly. Adding a regex post-processor should
happen only when:

1. The contract is already in place and failing visibly.
2. A widget or prompt change cannot eliminate the failure at the source.
3. The regex fires against a bounded, schema-driven substring (not
   against arbitrary prose).

Contract entry points live under `cli/contracts/*.py` (one module per
surface) and are dispatched deterministically by
`cli/contracts/run.py::run_all`. See
`docs/internals/WIDGETS_AND_CONTRACTS.md` for how to add a new contract.

## DAG decorator + phase metadata + parity

Post-processor ordering is encoded as DAG metadata, not implicit call
order in `cli/build.py`. The `@processor` decorator in `cli/pipeline.py`
registers each function with:

- `after=(...)` / `before=(...)`: ordering edges.
- `produces=(...)` / `consumes=(...)`: path-level data flow (used by
  parity assertions).
- `phase={"build", "reapply"}`, which pipeline phases include this
  processor. Most run in both; a few build-only setup procs are
  build-exclusive.
- `assumes_on_disk=(...)`: for reapply-only consumers, the list of
  paths expected to already be on disk from an earlier build, so the
  parity checker does not demand an in-subset producer.

`cli/pipeline.py::pipeline_order(phase)` resolves a deterministic
topological order for the phase-filtered subset. Ties inside a ready
group are broken alphabetically by function name, so the order is
reproducible across Python runs.

`cli/pipeline.py::assert_pipeline_parity()` is called at
`cli/build.py` import time and raises when:

- `after`/`before` references an unregistered name,
- the DAG contains a cycle,
- a processor in a phase subset `consumes` a path that has no in-subset
  producer AND is not covered by `assumes_on_disk`.

This means drift surfaces at pytest collection and worker startup, not
at customer-build time. When adding a new `@processor`, mirror the
canonical call order in `cli/pipeline.py::EXPECTED_PROCESSORS` so the
explicit-registry guardrail stays honest.

## When things still slip through

- Add a new regression test to `tests/test_mw_v17_fix_<letter>.py`
- Add a new entry to `cli/failure_kb.json`
- Append to `docs/internals/MW_BUG_RECURRENCE_RCA.md`
- Consider extending the prompt's "HARD UX RULES" section in `_build_explicit_prompt`
  if the root cause is Claude's generation, not the post-processor
