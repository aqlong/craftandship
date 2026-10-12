# MW v12 → v15 Bug Recurrence RCA

> "MW" is the canonical event-site reference build used as the regression baseline throughout this repo.


**Purpose:** Understand why the same bugs (or near-same) kept surfacing across 4 builds. Design prevention so v16+ is the last cycle.

## The recurring-bug pattern

| Bug | v12 | v13 | v14 | v15 | Root category |
|---|---|---|---|---|---|
| Ticketing iframe broken | ❌ critical | ✅ fixed | ✅ | ✅ | One-off pipeline bug, stayed fixed |
| Admin settings form disconnected | ❌ | ✅ template | ✅ | ✅ | Template fix, held |
| Admin CRUD URL 404s | - | ❌ introduced | ✅ | ⚠ hotpatched | **Pipeline ordering** |
| Upload preview missing | - | ❌ | ⚠ partial | ✅ | **Template path drift** |
| Double admin-settings.ejs | - | ❌ | ✅ | ⚠ NEW: admin/slug-index.ejs | **Convention drift** |
| Timezone UTC-as-CDT | ❌ | ❌ | ❌ | ❌ (partial) | **Regex scope** |
| Dead /register link | ❌ | ⚠ partial | ⚠ | ✅ (real route) | Heuristic drift |
| `/admin/{model}` handler missing | - | - | - | ❌ NEW videos | **Generation completeness** |
| res.locals.settings missing | - | - | - | ❌ surfaced | **Contract mismatch** |
| Hero image not wired | - | - | ❌ | ⚠ hotpatched | **Silent data binding gap** |

## Categorized root causes

### 1. CONVENTION DRIFT (highest frequency)

Each Opus build generates files with slightly different naming/path conventions. Post-processors assume ONE convention and miss variants.

**Examples:**
- v12: admin/settings.ejs (subdir)
- v13: admin-settings.ejs (hyphen) AND admin/settings.ejs both present
- v14: admin/settings.ejs (normalized)
- v15: admin/events-index.ejs + admin/events-new.ejs + admin/events-edit.ejs (NEW `-index` suffix)

**Why prevention matters:** The existing `_normalize_admin_view_paths` only handled hyphen→subdir conversion. It didn't anticipate `-index` suffix variant.

**Prevention design:**
- Pipeline post-processor `_enforce_admin_view_path_convention` that reads route render calls as ground truth, then ensures matching files exist at those EXACT paths. Rename drift variants automatically. Symmetric guarantee: for every `res.render('admin/X')` call, `views/admin/X.ejs` must exist post-processing.

### 2. CONTRACT MISMATCH (insidious: silent failures)

Templates assume shapes that middleware doesn't provide. Fallback text in templates masks the problem; page looks right, but editing settings does nothing.

**Example:** Templates use `settings.hero_image_url`, middleware sets `res.locals.siteSettings.hero_image_url`. Views with fallback like `settings.hero_tagline || 'default text'` work by coincidence when fallback ≡ DB value.

**Why prevention matters:** Tests pass because defaults render. Only diverging content (new hero image, new tagline) reveals the broken wiring.

**Prevention design:**
- Pipeline validator `_verify_template_middleware_contract` that scans all views for `settings.X` references and fails the build if `server.js` middleware doesn't populate `res.locals.settings.X`.

### 3. REGEX SCOPE (catch-22)

A regex matches the observed pattern but misses variants Claude generates next build.

**Example:** Timezone regex in v14 matched `<%= event.date_time %>` but not `<%= new Date(event.date_time).toLocaleDateString(...) %>`. v15 regex matched 4 patterns but an event detail page used yet another: `new Date(event.date_time).toLocaleString('...', { optsA: v, optsB: v })` with new opts.

**Prevention design:**
- Use AST-aware rewrite (or broader semantic match) instead of specific syntax patterns
- Or: enforce template convention at GENERATION prompt time, require ALL date_time displays to use `formatEventTime(x.date_time)` helper, never bare Date formatting. Add to DESIGN_CHECKLIST.

### 4. POST-PROCESSOR ORDERING (hidden dependencies)

Processor A creates file X; Processor B transforms file X; but they run in wrong order → B runs on nothing.

**Example:** `_ensure_upload_form_ui` transforms admin/settings.ejs inputs. `_ensure_site_settings` CREATES admin/settings.ejs. Main pipeline had correct order; reapply_post_processors (called after QA fix) had WRONG order.

**Prevention design:**
- Add explicit dependency declarations. Each post-processor declares `before=[]` / `after=[]` / `produces=[]` / `consumes=[]`. Validator ensures ordering satisfies all dependencies. Fail test if reordering violates a declaration.

### 5. HOTPATCH-NOT-FEEDBACK (meta-process bug)

The running artifact was hotpatched to verify a fix, but the fix was not always ported to the pipeline. Next build regenerates with same bug.

**Example:** v13 hotpatched admin CRUD URL prefix → fed back to pipeline as `_fix_admin_crud_link_prefix`. v15 hotpatched admin/events-index→admin/events rename → NOT YET in pipeline.

**Prevention design:**
- Rule: any artifact hotpatch MUST have a corresponding pipeline edit in the same session OR explicit deferment to the followup tracker with concrete acceptance criteria.

### 6. GENERATION COMPLETENESS (missing pieces)

Claude generates most CRUD for a model but omits an edge. We scaffold tests for "model X has list view" but not "model X has admin dashboard shortcut handler."

**Example:** v15 had `routes/videos.js` with full CRUD + `views/admin/videos.ejs` template, but `routes/dashboard.js` had `/admin/events` and `/admin/photos` handlers and NOT `/admin/videos`. Result: 404.

**Prevention design:**
- Post-processor `_ensure_admin_model_handlers` that iterates spec.data_models and injects a `/admin/{slug}` handler in dashboard.js for each model if missing. Symmetric with the admin view existence check.

---

## Prevention system: 6 new guards for v16+

1. **`_enforce_admin_view_path_convention`**: route render calls are ground truth; rename/redirect view files to match. Catches convention drift.

2. **`_verify_template_middleware_contract`**: lint that fails build when views reference `settings.X` and no middleware populates it. Catches contract mismatch.

3. **`_ensure_admin_model_handlers`**: dashboard.js must have `/admin/{slug}` GET for every data_model. Auto-injects missing ones. Catches generation incompleteness.

4. **Pipeline dependency validator**: declared before/after constraints enforced programmatically. Catches ordering bugs.

5. **Integration test suite**: `tests/test_mw_pipeline_integration.py` runs the FULL pipeline on a realistic spec + asserts post-conditions (admin URLs resolve, upload zones rendered, settings middleware wired). Catches everything else.

6. **Hotpatch audit helper**: at end of session, diff the running artifact against what `reapply_post_processors` would produce. Any non-empty diff = a hotpatch not fed back to pipeline.

---

## v17+v18 addendum: lessons from the LMNO cycle

After shipping A–K and building v18 via the real pipeline, reviewers
surfaced three classes of regression the FIXes didn't catch:

### Lesson 1: Duplicate admin surfaces

Claude's generated `views/admin/dashboard.ejs` shipped its own inline
`<form action="/admin/settings">`: a second, worse UX for the same
settings. The post-processors only ran on the canonical `settings.ejs`.
**Fix:** FIX L strips inline forms and replaces with a CTA to /admin/settings.
**Prevention:** prompt's HARD UX RULES now explicitly forbids a second
settings form anywhere but /admin/settings.

### Lesson 2: Snake_case DB keys leaking as labels

When Claude iterated `<% settings.forEach(s => <label><%= s.key %></label>)`,
the label text was the DB column name. FIX F made those inputs READABLE
but didn't HUMANIZE the labels. `hero_image_url` was visible on screen.
**Fix:** FIX M rewrites literal snake_case labels AND the `<%= s.key %>`
pattern via an injected `_humanLabel()` helper.
**Prevention:** prompt HARD UX RULES enumerates "labels are plain English,
never snake_case DB names."

### Lesson 3: URL text input + Choose File hybrid

Claude rendered `<input type="text" name="hero_image_url">` sibling-paired
with `<input type="file" name="hero_image_file">`. FIX N's
`_ensure_upload_form_ui` regex only converted the text input; the orphan
file input stayed. The hybrid was visible.
**Fix:** FIX N broadened with an orphan-file-input strip pass.
**Prevention:** prompt's HARD UX RULES shows the canonical drag-drop zone
shape and explicitly forbids the hybrid.

### Lesson 4: Seed-padding clone titles

`_ensure_minimum_seed_rows` padded events by appending "(evening session)"
etc. On a 6-event spec that became 15 rows with the visible list reading
as clones. Real customer-facing quality was hurt.
**Fix:** per-table minimums (events=8, photos=12, others=15); events use
PREFIXES ("Full Moon: ", "Solstice: ") not parenthetical suffixes.

### Category 7: "Dual Render Surface"

A new category for this RCA: when Claude generates TWO different templates
that both render the same model/setting (e.g. admin dashboard's inline
settings + /admin/settings), post-processors typically only target one.
The other silently ships the broken pattern. **Prevention:** enforce
"one surface per concept" at the prompt level AND add a scan step that
detects duplicate form actions across views.

## The process rule for v16

**v16 ships only after the integration test suite passes WITHOUT HOTPATCHING the artifact.** If any hotpatch is needed during QA, it's a pipeline bug; add a failing test, fix the pipeline, rebuild. Iterate until zero hotpatches. THAT is production-ready.
