# CAS v2: static compose plan

**Decided 2026-04-20.** After the seventh iteration of a non-MW customer build surfaced 8 regressions (F1–F8), two caused by same-
session pipeline commits, Ocean committed to a fundamental pivot: **remove AI
from the CAS build pipeline entirely.** AI stays in the intake conversation +
copy drafting + creative suggestion layer; the build layer is pure composition
of static widgets + decision files.

> **Status note.** This is the original plan, kept as a record. The layout it proposes was not built as written: the directories and test files named without code formatting below (for example compose/audit/, compose/assets/, compose/content_generators/, compose/widgets/__tests__/, and the per-concern compose/tests/test_*.py files) were planned and never created, and the v1 tests were not moved to tests/legacy/v1/ (they stayed in `tests/`, gated by `CAS_BUILD_ENGINE=v1`). The shipped v2 code and tests are `compose/` and `compose/tests/` (`ls compose/tests`); `docs/internals/V2_ENGINE_BEHAVIOR.md` and `docs/internals/cas_v2_architecture.html` describe what exists.

This document is the comprehensive plan. It is intentionally unsized in
wall-clock time: phases have defined completion criteria (not durations).
We will move as fast as we can hold quality.

> **Scope contract.** v1 code stays frozen in current top-level dirs. v2
> work lives in `compose/`. A worker env flag `CAS_BUILD_ENGINE=v1|v2`
> routes each build to either pipeline. This preserves v1 as fallback
> during transition and permits v2 rollout one-customer-at-a-time.

---

## Guiding principles

1. **Three-layer static decision system: decision files + widgets + templates.**
   Decision files (JSON) define WHAT options exist. Widgets are the UI
   kit (EJS + CSS). Templates compose widgets into page shapes.
2. **Creativity at the CSS/graphics/copy layer, not the code layer.**
   Palette, fonts, layouts, imagery, copy: curated options + AI drafts.
3. **Static decision files limit the option space.** Customer picks from
   a few per decision point; library grows by JSON edits, not code.
4. **"If it has one correct answer, it becomes a widget."** If it requires
   creative judgment, it stays in the intake conversation.
5. **Unit-test every widget once; it works forever.** No LLM variance in
   the build path = no regressions.
6. **v1 stays accessible for reference.** Widget ideas, post-processor
   logic, templates, test fixtures all remain available to copy forward.

---

## Phase 0: Freeze v1 as beta

**Goal:** land the current pipeline polish work into master as a
clean v1-beta baseline, merge all helpful unmerged branches, tag the commit,
and freeze the v1 code for reference.

### Steps

1. **Fix Stage 8 regressions** on the polish branch:
   - **F2 first** (narrowest, highest-leverage): add negative lookbehind
     to the contrast-fix regex in `_fix_nav_brand_contrast_and_dedupe`.
     New test: login button + `background-color: var(--color-primary)`
     inside primary-bg nav → `background-color` NOT rewritten.
   - **F1**: gate `_fix_admin_layout_nav_prefix` on EITHER
     `views/layout-public.ejs` existing OR the link being inside an
     admin role guard `<% if (locals.user) %>`. New test: single-layout
     build (no `layout-public.ejs`) with bare `/events` in unconditional
     nav → no rewrite.
   - **F4**: new post-processor `_unify_admin_dashboard_handler`, rewrite
     `routes/admin.js`'s dashboard `res.render` to `res.redirect('/dashboard')`.
     New test: the resulting admin route returns 302 to `/dashboard`.
2. **Run full pytest** (must be ≥ 4220 passed) + MW canonical smoke.
3. **Merge the local MW SEC-1 work** into master (3 commits: no-bcrypt-in-
   seeds + multer DoS patch + refactor into `_scrub_bcrypt_from_seed`).
   If the current `cli/build.py` has already incorporated the broad
   strokes, cherry-pick only the net-new processor + tests.
4. **Merge the pipeline polish branch** into master after F1/F2/F4 land green.
5. **Prune stale branches** after confirmation (merged or shipped ones;
   `staging` is kept for the continuous deploy gate).
6. **Tag master** as `v1-beta-frozen` with a commit message noting this
   is the last mainline v1 release; all future pipeline work goes to v2.
7. **Update `CLAUDE.md`** top-of-file note: "v1 is frozen as `v1-beta-
   frozen`. All new pipeline work is in `compose/` (v2). See
   `docs/internals/CAS_V2_STATIC_COMPOSE_PLAN.md`."

### Completion criteria

- [ ] F1 + F2 + F4 pipeline-level fixes land with tests.
- [ ] The pipeline polish branch merged into master.
- [ ] The local MW SEC-1 work merged.
- [ ] Master tagged `v1-beta-frozen`.
- [ ] CLAUDE.md carries the v2 pointer.
- [ ] No new work lands on v1 codepaths after the tag.

---

## Phase 1: Prior art audit (do this FIRST, before any widget code)

**Goal:** catalog what real small-biz webapps look like in the market so
the widget library is sized correctly from day one. Without this, we'd
build widgets speculatively and discover gaps mid-sprint.

### Sources: where to pull the first 20 sites

Ranked by leverage (best-first):

1. **Squarespace template gallery** (https://www.squarespace.com/templates), 5 sites.
   Filter categories: Wellness, Health & Beauty, Coaching, Local Business,
   Creative. Each template is already decomposed into stripes: hero,
   about, services, testimonials, CTA, contact, footer. Commercially
   validated for small-biz customers. Starting here gives us the proven
   composition grammar for free.
2. **Psychology Today therapist/healer directory** (https://www.psychologytoday.com/us/therapists), 5 sites.
   Every practitioner has a link to their real site. Real small-biz
   content, not demo content. Pull 5 that represent different offering
   shapes (1:1, group, retreats, hybrid).
3. **Competitors of the reference healer build**: 3 sites.
   Find 3 direct competitors (energy workers, Reiki teachers, breathwork facilitators)
   and decompose. This tells us what that audience expects a site to DO.
4. **Webflow Showcase** (https://webflow.com/made-in-webflow) + **Wix gallery**, 3 sites.
   Coach / consultant / creative portfolio archetypes. Higher-polish
   than Squarespace so we see the ceiling of what's achievable.
5. **WordPress + Elementor real customer sites**; 2 sites.
   Local service biz (plumber, electrician, massage therapist, tax
   preparer). Represents the "less polished but functional" tier; 
   helps us avoid over-designing for the top of the market.
6. **Shopify theme store examples**: 2 sites.
   Small retail. Covers the e-commerce archetype we'll eventually need
   (Phase 6+ scope, but good to see patterns early).

**Deliberate non-sources (for this audit):** pure AI-generated site output.
That output is what CAS currently produces;
we want to learn what the MARKET looks like, not what LLMs produce.

Also non-sources: enterprise SaaS dashboards, complex web apps (Notion,
Figma, etc.). Different segment; would pull patterns we don't need.

### Audit output

One file per site in `compose/audit/<archetype>/<site-slug>.md`. Each file
catalogs:

- URL + archetype + one-line positioning.
- Page inventory (home, about, services, blog, events, contact, etc.).
- Per-page widget inventory (hero variant, about style, testimonials
  format, CTA placement, etc.) with screenshots.
- Data shapes observed (what the forms collect, what the listings show).
- Common patterns across the 3-5 sites in the same archetype.

Rollup file compose/audit/FINDINGS.md with:

- Widget list (every distinct widget observed across all 20 sites, with
  count of appearances).
- Archetype taxonomy (list + defining characteristics).
- Variation axes per widget (e.g. Hero: centered / split / video / carousel).
- Data-shape taxonomy (what content models repeat: Service, Testimonial,
  Event, BlogPost, GalleryItem, etc.).

### Completion criteria

- [ ] 20 site audits written to compose/audit/.
- [ ] Rollup `FINDINGS.md` lists every observed widget + variation axis.
- [ ] Archetype taxonomy lists 7–10 archetypes with assignment of each
      audited site to one of them.
- [ ] Gut check: for each of the 5 target MVP archetypes (Healer, Coach,
      Local Service, Creator, Professional Services), we have ≥ 3 real
      sites audited in that archetype.

---

## Phase 2: Decision files + widget registry

**Goal:** translate the Phase 1 findings into the static JSON layer that
drives Phase 3's widget builds and Phase 4's compose pipeline.

### Files to create

All under `compose/data/`:

- **`archetypes.json`**: list of archetypes. Each entry:
  ```json
  {
    "id": "healer-wellness",
    "name": "Healer / Wellness Practitioner",
    "default_pages": ["home", "work-with-me", "about", "journal", "events", "contact"],
    "default_data_models": ["BlogPost", "Event", "Testimonial", "ContactInquiry", "NewsletterSubscriber"],
    "default_widget_bundle": "healer-v1",
    "default_palette_suggestions": ["warm-earth", "soft-rose", "forest-emerald"],
    "default_font_pair_suggestions": ["rubik-nunito", "lora-raleway"],
    "copy_tone": "warm, grounded, intuitive"
  }
  ```
- **`palettes.json`**: curated palettes. Each is a dict of CSS vars:
  ```json
  {
    "id": "warm-earth",
    "name": "Warm Earth",
    "vars": {
      "--color-primary": "#6b1519",
      "--color-on-primary": "#ffffff",
      "--color-secondary": "#fdf0f0",
      "--color-accent": "#d4a5a5",
      "--color-accent-warm": "#c9a84c",
      "--color-accent-earth": "#a87850",
      "--color-foreground": "#1f2937",
      "--color-background": "#ffffff"
    }
  }
  ```
  Target 20–30 palettes at MVP covering warm, cool, neutral, bold, pastel.
- **`font-pairs.json`**: curated `{display, body}` Google Fonts pairs:
  ```json
  {
    "id": "rubik-nunito",
    "display": {"family": "Rubik", "weights": [400, 500, 600, 700, 900]},
    "body": {"family": "Nunito Sans", "weights": [300, 400, 500, 600, 700]}
  }
  ```
  Target 15–20 pairs at MVP.
- **`page-layouts.json`**: per-page-type variants:
  ```json
  {
    "page": "home",
    "variants": [
      {"id": "healer-v1-centered",
       "widgets": ["nav", "hero-centered", "offerings-grid-3col",
                   "testimonials-stack", "cta-newsletter", "footer"]},
      {"id": "healer-v1-split",
       "widgets": ["nav", "hero-split-image", "offerings-grid-3col",
                   "testimonials-carousel", "cta-inquiry", "footer"]}
    ]
  }
  ```
  Target 3–5 variants per page type per MVP archetype.
- **`widget-registry.json`**: canonical widget catalog with data contracts:
  ```json
  {
    "id": "hero-centered",
    "file": "widgets/hero/centered.ejs",
    "data_contract": {
      "tagline": "string",
      "description": "string",
      "cta_primary": {"text": "string", "href": "string"},
      "cta_secondary": {"text": "string", "href": "string", "optional": true}
    }
  }
  ```

### Completion criteria

- [ ] 5 MVP archetypes defined in `archetypes.json`.
- [ ] ≥ 20 palettes + ≥ 15 font pairs + ≥ 3 variants per page type per
      archetype.
- [ ] Widget registry lists every widget needed to compose the MVP page
      layouts.
- [ ] Data contracts documented for every widget (what fields it consumes).

---

## Phase 3: Widget library MVP

**Goal:** build the ~40–60 widgets the MVP archetypes need, each as a
unit-tested static EJS + CSS pair.

### Widget structure

`compose/widgets/<category>/<name>.ejs` + `.css`. Each widget is a
self-contained component that renders from its data contract and nothing
else. No side effects, no global state, no Claude calls.

### MVP widget list (rough, refine during Phase 1 audit)

**Shell widgets** (every page has these):
- `nav/admin-public-shared` (single-layout, role-guarded Dashboard)
- `nav/admin-only` (two-layout pattern)
- `nav/public-only`
- `footer/standard`
- `footer/minimal`
- `skip-to-content`
- `mobile-menu-drawer`

**Hero widgets:**
- `hero/centered`
- `hero/split-image`
- `hero/video-bg`
- `hero/image-carousel`

**Content grid widgets:**
- `offerings-grid/3col`
- `offerings-grid/2col-card-stack`
- `services-list/with-pricing`
- `services-list/inquiry-only`

**Social proof widgets:**
- `testimonials/stack`
- `testimonials/carousel`
- `testimonials/grid-3col`

**CTA widgets:**
- `cta/newsletter-inline`
- `cta/inquiry-form`
- `cta/book-now-primary`

**Resource list widgets** (for blog/events/etc.):
- `resource-list/card-grid`
- `resource-list/minimal-list`
- `resource-detail/article-style`
- `resource-detail/event-style`

**Form widgets:**
- `form/contact-inquiry`
- `form/newsletter-signup`
- `form/login`
- `form/magic-link-request`

**Admin widgets** (logged-in only):
- `admin/dashboard-stats-grid`
- `admin/recent-items-table`
- `admin/settings-form-identity`
- `admin/settings-form-hero`
- `admin/settings-form-logo-about`
- `admin/settings-form-contact`
- `admin/settings-form-social`
- `admin/settings-form-image-uploads` (drag-drop zones; solves F5 permanently)
- `admin/crud-list` (generic, slug-driven)
- `admin/crud-new`
- `admin/crud-edit`
- `admin/crud-detail`
- `admin/crud-delete-confirm`

**Error pages:**
- `error/404`
- `error/403`
- `error/500`

**Utilities:**
- `date-format/humanized` (solves F3 permanently)

### Testing contract

Every widget gets a unit test fixture in compose/widgets/__tests__/.
Test renders the widget with minimum-valid data + maximum-valid data +
missing-optional-fields, asserts output DOM structure + no console
errors + accessibility attrs present.

### Completion criteria

- [ ] All widgets above built + tested.
- [ ] Test coverage ≥ 95% of widget render branches.
- [ ] No widget calls Claude, no widget has global state.
- [ ] Every widget renders correctly with at least 3 palettes + 3 font pairs.

---

## Phase 4: Compose pipeline

**Goal:** replace the v1 `cli/build.py` pipeline with a pure-composition
engine that reads spec → decision files → widget registry → emits files.

### Architecture

`compose/build.py`:

1. Load spec (validated by same validator as v1, possibly a slightly
   trimmed spec shape since the widget system needs less flexibility).
2. Resolve archetype → widget bundle + page layout set.
3. Resolve palette + font pair (customer-picked or archetype default).
4. Compose each page from its layout variant → ordered list of widgets.
5. For each widget, prepare data from spec content + settings.
6. Render widget EJS + bundle its CSS.
7. Emit files: `schema.sql` (from model definitions), `seed.sql` (from
   content), `server.js` (standard skeleton + resource router mounts),
   `routes/*` (standard CRUD per model), `views/*` (composed pages),
   `public/*` (bundled CSS + favicon + sitemap + robots.txt +
   manifest.json + service worker).

### What's NOT in the compose pipeline

- No Claude API calls.
- No post-processors that re-parse emitted output. (Widgets ARE the
  post-processors; they produce correct output at emission time.)
- No QA loop, no design-fix loop, no advisor tool. Those exist only
  because v1 emission is unreliable.

### Migration of useful v1 logic

Copy-forward these v1 components as widget inputs or shared utilities:

- `cli/design_data.py` (palette extraction, archetype detection) → becomes
  the intake-side palette suggester in Phase 5.
- `cli/assets/` (logo, stock photos) → planned compose/assets/.
- Seed generators (synthetic testimonial + event + blog post data) →
  planned compose/content_generators/ (still AI-assisted for copy drafting).
- The canonical reference spec → a planned fixture under `compose/fixtures/` for regression.

### Worker integration

`queue/worker_pg.py` consults `CAS_BUILD_ENGINE` env var. If `v2`,
dispatches to `compose.build.build_app(spec)` instead of
`cli.build.build_app(spec)`. Both write to `builds/<app_name>/`. Both
return the same result shape.

### Completion criteria

- [ ] `compose/build.py` composes the MW spec and the second customer's spec to
      byte-identical (or functionally-equivalent) output vs v1's hot-patched result.
- [ ] Worker flag `CAS_BUILD_ENGINE=v2` works end-to-end for a local build.
- [ ] Compose pipeline has zero Claude API calls in its critical path.
- [ ] Per-build cost < $0.10 (basically free).
- [ ] Build wall time < 2 min (down from 12-15).

---

## Phase 5: Intake + choice UI

**Goal:** customer-facing picker UI for archetype + palette + fonts +
layouts + imagery + copy drafting.

### New intake flow

1. **Archetype detection** (reuse v1 `cli/design_data.py`):
   AI reads the free-form customer description, suggests 1-3 archetypes.
   Customer picks one.
2. **Palette picker**: show 4-8 palettes curated for the chosen archetype.
   Live preview via iframe. Customer picks one or asks AI for a custom
   adjustment.
3. **Font-pair picker**: show 3-5 pairs. Live preview.
4. **Page-layout picker**: per page (home, about, services), show 2-3
   variants. Live preview.
5. **Imagery**: AI suggests stock photos from curated archetype-tagged
   collection. Customer uploads their own optionally.
6. **Copy drafting**: AI writes tagline, about body, initial testimonials,
   initial blog post. Customer edits inline before build.
7. **Review + build**: full spec visualized; customer confirms; build
   kicks off.

### Build time perception

Build is now <2 min instead of 12-15. Customer stays on the review page;
watches a progress bar; gets "your site is live" within a minute or two.

### Completion criteria

- [ ] Intake flow lets customers pick archetype/palette/fonts/layouts/imagery.
- [ ] AI writes copy per-page and customer can edit inline.
- [ ] Build triggers with full v2 spec shape.
- [ ] Customer sees live preview during picker steps (iframe against a
      preview instance).

---

## Phase 6: Shadow validation + cutover

**Goal:** before cutting over production, validate v2 against 5-10 real
specs and compare against v1 outputs.

### Steps

1. **Re-run 10 historical specs** (MW canonical, the second customer's
   spec, plus 8 more: queued test specs) through both v1 and v2
   pipelines with the flag.
2. **Diff outputs**: per spec, what files differ? What renders
   differently? Are v2 outputs strictly better (no bugs), equivalent
   (same UX), or worse (widget gaps)?
3. **Fix widget gaps** surfaced by the shadow comparison.
4. **Re-run** until all 10 specs produce v2 outputs ≥ equivalent to v1.
5. **Cutover**: set default `CAS_BUILD_ENGINE=v2` for new customers.
   Existing customer rebuilds stay on v1 until their next scheduled
   rebuild (opt-in v2 via admin tool per-customer).
6. **Monitor**: cost per build, defect rate per build, time per build.

### Completion criteria

- [ ] 10 shadow-validation specs produce v2 outputs ≥ equivalent to v1.
- [ ] No shadow-validation build reveals a widget gap we can't fill in
      under 1 hour.
- [ ] Cost/time/defect metrics for v2 meet targets (< $0.10, < 2 min,
      < 0.5 defects/build).

---

## Phase 7: Library growth + v1 sunset

**Goal:** grow the widget/archetype library organically from real
customer demand; retire v1 once v2 has proven stable for enough
customers.

### Ongoing

- Every new customer spec that doesn't fit existing archetypes → new
  archetype + widget additions (JSON edit + widget builds; unit tests).
- Every new design request from customers → new palette / font pair /
  layout variant (JSON edit only).
- Quarterly: trim unused archetype / widget variants.
- Eventual: delete v1 `cli/`, `queue/worker_pg.py` code paths once all
  customers have been migrated to v2 or explicitly opted to stay on v1.

### Sunset criteria

- [ ] v1 handles 0 customer builds for 60 consecutive days.
- [ ] v1 has no remaining bug reports in the coordinator roadmap.
- [ ] Tag `v1-retired` on the last commit that touches `cli/` or
      `queue/worker_pg.py`.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Widget library is missing a pattern we need for an MVP customer | Phase 6 shadow validation surfaces gaps BEFORE cutover; Phase 1 audit sized library correctly |
| Composed output feels "templated" / less unique than v1 | Palette × font × layout × imagery variation produces 100+ distinct visual outcomes per archetype; v1 output already looks similar across customers |
| Customer expects AI to "invent" something we haven't widgetized | Intake sets expectation: "customize from our library"; waitlist for features out of scope |
| v1 can't be cleanly frozen because the pipeline polish branch has unmerged regressions | Phase 0 fixes F1/F2/F4 before the tag |
| Losing v1's learnings | Keep v1 code accessible in-repo; compose/audit/ pulls widget ideas from v1 templates |
| Time to MVP stretches | Phase 0 ships immediately; every subsequent phase ships incrementally and customers can trial v2 one at a time |

---

## Immediate next actions

1. **Fix F1 + F2 + F4** on the polish branch (Phase 0 step 1).
2. **Merge the local MW SEC-1 work** into master.
3. **Merge the pipeline polish branch** into master.
4. **Tag `v1-beta-frozen`.**
5. **Create `compose/` top-level dir + scaffold** (`compose/data/`,
   `compose/widgets/`, compose/audit/, `compose/build.py` stub).
6. **Start Phase 1 audit**: first 20 sites following the source list above.

Everything under steps 1-4 is Phase 0 work. Steps 5-6 kick off Phase 1.

---

## Adversarial review (2026-04-21)

This section documents a deep adversarial review of the plan above.
It is intentionally harsh: the goal is to catch over-optimism, name
the real risks, and tighten the plan before any `compose/` code is
written.

**TL;DR**: the plan is directionally sound, but the headline numbers
are too rosy, several technical + customer-experience concerns are
under-specified, and the "no AI in build" framing obscures that intake
AI stays substantial. Attacks + mitigations below (the cost, positioning,
moat and timeline attacks were removed from this public export).

### Attack 2: "Nearly impossible mistakes" is false

**Claimed:** "Unit-tested widgets = near-zero regression surface."

**Problem:** Widget-based composition introduces NEW failure modes
that unit tests miss:
- **CSS cascade conflicts**: widget A's `.btn` collides with widget
  B's global `button` rule. Not caught by per-widget tests.
- **Data contract violations**: widget expects `event.event_date`
  (string); intake generates `event.date` (Date). Compose-time
  crash. New failure class.
- **Accessibility regressions**: widget A skips heading level 2,
  widget B starts at level 2 → h1 → h3 jump. Breaks screen readers.
- **Page-level integration**: widgets individually valid but
  composed page is visually broken (hero + offerings have
  incompatible spacing, mobile layout breaks at certain widget
  combinations).
- **Browser-specific bugs**: Safari 17 handles `@container` queries
  differently than Chrome 120. Unit tests in headless Chrome pass;
  real browsers fail.
- **Content-length edge cases**: testimonial widget designed for
  200-char quote breaks layout with 800-char quote.
- **Widget library updates**: bumping nav widget to v2 to fix a bug
  changes semantics; every build against the new library diverges
  from older builds. Need versioning strategy.

**Revised target:** "near zero for covered patterns; 1-3 regressions
per build expected in early compose output, trending toward <1 as
the widget library matures + integration-test coverage grows."

**Mitigation:** add explicit Phase 3.5 "Integration test suite":
smoke-build 10-20 real specs end-to-end, run browser tests across
Chromium/WebKit/Firefox, Lighthouse for perf + a11y, axe-core for
WCAG.

### Attack 4: Widget library size is probably wrong

**Claimed:** ~40-60 widgets covers 80% of small-biz specs.

**Problem:** The 80/20 is speculative. WordPress + Elementor ships
~100 widgets + a marketplace with thousands. Squarespace has ~130
section templates across archetypes. Shopify theme library has
similar scale. CAS's "80%" may actually be 50-60% without a
library 3-4× larger than estimated.

**Also:** each NEW archetype (the audit may reveal 10-12 real
archetypes, not 7-10) multiplies widget needs. A coach archetype
needs "booking widget" variants that a healer archetype doesn't.

**Revised estimate:** Phase 1 audit should explicitly count
widgets-per-archetype-per-page in the audit rollup, and the MVP
library is sized based on that; probably **60-120 widgets at
MVP**, not 40-60. Complicates Phase 3 scope.

**Mitigation:** Phase 1 audit deliverable adds a "realistic widget
count" section with a bottoms-up estimate. If the number is above
100, reassess whether MVP should cover 3 archetypes instead of 5.

### Attack 6: Transition risk is under-specified

**Claimed:** `CAS_BUILD_ENGINE=v1|v2` flag enables per-customer
rollout.

**Problem:** Customers built with v1 still need ongoing rebuilds
(content edits, security patches, minor fixes). Three scenarios:
- Customer A (on v1, wants content edit): rebuild via v1 pipeline
  OR migrate to v2. Migration may change visual output; customer
  gets "their site redesigned without asking."
- Customer B (on v1, CVE in v1 dep): patch v1 pipeline, rebuild via
  v1. We're maintaining v1 indefinitely.
- Customer C (on v2, wants feature that's only in v1 widgets): can't
  serve from v2 until widget added. Feature velocity bottleneck.

**Revised approach:** explicit per-customer migration flag +
"your rebuild will change visuals; preview before confirming" UX.
Document sunset criteria for v1 (e.g., "6 months after last
build, convert to read-only static export").

### Attack 7: Missing concerns

The plan doesn't address:
- **Accessibility** (WCAG 2.1 AA audit per widget, focus management,
  screen reader testing, keyboard navigation).
- **Internationalization** (multilingual support; some healers +
  coaches serve non-English clients).
- **Performance budgets** (Lighthouse scores ≥ 90, page weight
  limits, Core Web Vitals per widget).
- **Responsive breakpoints** (strategy for mobile/tablet/desktop per
  widget; each widget must handle all three).
- **Dark mode** (opt-in per archetype or uniform?).
- **Print stylesheets** (healer intake forms, event tickets).
- **SEO** (per-widget meta contribution: OG tags, sitemap entries,
  structured data JSON-LD for testimonials, events, services).
- **Analytics** (GA4 vs Plausible vs none, privacy-first default).
- **Email deliverability** (per-customer sender reputation via
  Resend domains or shared).
- **Domain purchase + DNS** (customer-provided vs CAS-managed).
- **Legal pages** (ToS, privacy, GDPR, cookie banner, accessibility
  statement).
- **Data retention + deletion** (GDPR right-to-erasure per-app).
- **Widget versioning** (semver? breaking changes? customer
  rebuild side effects?).
- **CSS strategy** (CSS modules, BEM, CSS-in-JS, or scoped styles
  per widget: avoid cascade conflicts).
- **JavaScript scoping** (namespacing, event cleanup, SSR
  compatibility).

**Mitigation:** add Phase 2.5 "Cross-cutting concerns spec" between
decision files and widget library, documenting how each concern
above is addressed. A widget isn't "done" until all cross-cutting
concerns are satisfied.

### Attack 8: Wall-time claim is rosy

**Claimed:** 12-15 min → <2 min.

**Problem:** Even with zero AI in the build path, wall time includes:
- `npm install` (cold cache: 60-90s; warm: 20-30s).
- Schema + seed SQL execution.
- Tailwind compile (~5-15s for a typical app).
- Railway project create + service create via GraphQL (~20-40s).
- `git push` to Railway remote (~10-30s).
- Railway build + deploy (~60-180s: outside our control).
- Health check wait (~30-60s).
- Post-deploy smoke test (~20-30s).

Sum: 3-5 min minimum. "<2 min" is unrealistic unless we change the
deploy story (e.g., pre-built Docker images per archetype).

**Revised target:** **3-5 min total wall time** is honest; "<2 min"
is aspirational after further optimization.

### Attack 11: "No Claude in build path" may be too rigid

**Claimed:** Zero Claude calls in build path.

**Problem:** Some one-shot Claude calls during build would be
CHEAPER + MORE RELIABLE than trying to bake the same logic into
widgets:
- Palette refinement: "this customer's brand is sunset-orange +
  deep teal; propose 3 palette variants that riff on that."
  Hard to encode statically.
- Copy tone adjustment: "customer edited the about section; smooth
  the transitions." One call; deterministic widgets can't do this.
- Image selection: "pick 3 stock photos that match this healer's
  vibe from the 2,000-photo library." An embedding-based search
  + one Claude call beats hand-curated tags.

**Revised principle:** "No Claude in the HOT PATH of code
generation. Narrow Claude calls at the intake boundary (palette
riffs, copy polish, image search) are allowed if they're cheap +
well-contained + don't affect generated code semantics." This
softens the pillar but lets us keep genuinely useful AI assists
in intake.

---

## Non-goals (explicit)

Saying "no" up front prevents scope creep later:

- **NOT building a drag-drop editor.** No inline content editing or
  generalized template-builder UX.
- **NOT replacing human review.** SE Review Gate stays.
- **NOT handling all customer requests.** Customers whose needs
  fall outside the curated option space are out of scope for v2.
- **NOT targeting zero regressions.** Target is "≤1 regression per
  build on average", not perfection.
- **NOT eliminating AI.** Intake + copy drafting + narrow
  creative assists stay. "No AI in build path" ≠ "no AI in CAS."
- **NOT doing ongoing customer site edits through CAS.** Once
  shipped, customers edit via their admin CMS (CRUD UIs). CAS does
  rebuilds for structural changes (new archetype, new section),
  not line-by-line content edits.
- **NOT maintaining v1 forever.** v1 is a frozen fallback with a
  sunset path.
- **NOT optimizing for the edge case.** Most specs fit the curated
  options; the rest are out of scope.

---

## Expanded risk register (post-review)

| Risk | Severity | Mitigation |
|---|---|---|
| New failure modes (CSS conflicts, data contract, a11y) emerge | High | Phase 3.5 integration testing; axe-core + Lighthouse CI |
| Widget library estimate is 2-3× too low | High | Phase 1 audit sizes library bottoms-up; MVP covers 3 archetypes, not 5 |
| v1 sunset forces customer migrations with visual diffs | Medium | Preview-before-confirm UX; optional frozen-v1 static export |
| Cross-cutting concerns (a11y, i18n, perf, SEO) under-scoped | High | Phase 2.5 spec; widgets not "done" without all concerns covered |
| Wall-time <2 min target unrealistic | Low | Revised to 3-5 min; keep <2 min as aspirational |
| "No Claude in build" principle too rigid | Low | Softened to "no Claude in hot path"; narrow intake assists allowed |
| Customer widget customization breaks library upgrades | Medium | Widget versioning strategy; customer-applied CSS scoped to override layer |

---

## Revised headline numbers

The visuals and plan previously claimed aggressive targets. Honest
ranges for the v1→v2 transition:

| Metric | v1 measured | v2 realistic | v2 aspirational |
|---|---|---|---|
| Build wall time | 12-15 min | 3-5 min | <2 min |
| Regressions per build (median) | 3-8 | 1-3 early, <1 trending | 0 |
| MVP archetypes | 1-2 covered by v1 | 3 (Healer, Coach, Local Service) | 5 |
| MVP widget library size | n/a | 60-120 | 40-60 |
| Claude-call sites per build | 9 | 2-4 (intake + copy + optional polish) | 1 |

---

## Scope guard

To keep the plan executable, new proposals face this filter:

1. Does it serve the MVP 3 archetypes? If no, post-MVP.
2. Does a single widget + JSON change cover it? If no, flag as
   "new archetype" scope, defer.
3. Does it introduce a new failure mode not in the integration
   test matrix? If yes, integration test FIRST, then feature.
4. Does it require Claude in the build hot path? If yes, narrow
   it to intake or decline.
5. Is it a one-off customer ask? If yes, defer or decline.

---

## Updated "what done looks like"

Original criteria remain but revised with honest metrics:

- v2 builds 3 archetypes (Healer, Coach, Local Service) reproducibly
  against 10+ real specs each.
- Widget library passes WCAG 2.1 AA audit per widget + Lighthouse
  ≥ 90 on all real-spec renders.
- Cross-cutting concerns (Phase 2.5) documented + enforced in every
  widget's test matrix.
- Shadow validation shows v2 output ≥ quality of v1 equivalent.
- Wall time ≤ 5 min (not <2 min).
- v1 has a documented sunset timeline + per-customer migration
  UX.

---

## Test strategy: transition + v1 sunset

**Principle:** v1 tests stay green while v1 still runs for any
customer. v2 tests accrue as widgets + decision files + compose
pipeline land. At v1 sunset, v1 tests get disabled and v2
becomes the primary test suite.

### Test categories today (v1)

| Category | Location | Count | What it tests |
|---|---|---|---|
| Python unit (pipeline) | `tests/test_*.py` | 4242 | Every post-processor, build helper, validator, worker, coordinator, monitor, etc. |
| Python pipeline-integration | `tests/test_mw_pipeline_integration.py` etc. | included above | Full post-processor chain against canonical specs |
| Web unit (Vitest) | `web/tests/unit/` | ~700 | Intake routes, dashboard, admin tools, auth, Stripe, email |
| Web E2E (Playwright) | `web/tests/ui/` | ~dozens | CAS website end-to-end flows |
| Per-build QA tests (qa.py-generated) | runtime-generated | per-build | Customer app CRUD + UI smoke; re-generated for every build |
| Per-build design review (design_review.py) | runtime | per-build | Visual QA against the design rules |

### Test categories for v2

| Category | Location | What it tests | Replaces |
|---|---|---|---|
| Widget unit | compose/widgets/__tests__/ | Per-widget render: min/max data, optional fields, all variants, a11y attrs | Post-processor unit tests |
| Decision-file schema | compose/data/__tests__/ | Archetype JSON schema, palette CSS-var completeness, font-pair validity, layout references | N/A (new) |
| Compose-pipeline integration | compose/tests/test_compose_integration.py | Full spec → widgets → output for each archetype | `tests/test_mw_pipeline_integration.py` |
| Widget-composition integration | compose/tests/test_widget_integration.py | Pairs of widgets compose without CSS/JS/a11y conflicts | New failure class |
| Cross-browser Playwright | compose/tests/test_rendered_output.py | Chromium + WebKit + Firefox render matching golden screenshots | Per-build qa.py |
| Accessibility (axe-core) | compose/tests/test_a11y.py | WCAG 2.1 AA on every rendered page per archetype | N/A (new) |
| Performance (Lighthouse CI) | compose/tests/test_lighthouse.py | Scores ≥ 90 per archetype; Core Web Vitals budgets | N/A (new) |
| Smoke against real specs | compose/tests/test_real_specs.py | MW, the second customer spec, 8 audit-seeded specs all build + deploy cleanly | Per-customer manual verification |
| Web unit (Vitest) | `web/tests/unit/` | Extended with new choice-UI routes, AI copy-drafting | Extended, not replaced |
| Web E2E (Playwright) | `web/tests/ui/` | New intake flow with archetype/palette/font/layout pickers | Extended, not replaced |

### Per-phase test obligations

- **Phase 1 (audit):** no test changes. `tests/` v1 suite stays
  green. Audit produces widget + archetype catalog, informs Phase 3
  test matrix.
- **Phase 2 (decision files + registry):** schema validators shipped
  with tests asserting JSON validity + required keys. Added to a
  new `compose/tests/` dir.
- **Phase 2.5 (cross-cutting concerns spec):** defines the test
  obligations per widget (a11y + perf + responsive + dark mode +
  SEO + etc). Each widget's test file asserts each obligation.
- **Phase 3 (widget library):** every widget ships with unit tests
  + a11y test + cross-browser render test. No widget merges
  without ≥95% test coverage + green on all obligations.
- **Phase 4 (compose pipeline):** compose-integration tests + real-
  spec smoke tests. MW canonical spec runs through v2 and output
  is compared against known-good golden files.
- **Phase 5 (intake choice UI):** web/ unit + Playwright tests
  extended for the new picker screens. Existing web tests stay
  green.
- **Phase 6 (shadow validation):** test flag `CAS_TEST_V2_SHADOW=1`
  runs BOTH pipelines on every spec and diffs output. Fail if v2
  output is visibly worse than v1 (by lighthouse score, a11y
  violations, or human-reviewed golden-image diff).
- **Phase 7 (library growth + v1 sunset):** see sunset
  criteria below.

### Parallel test suite during transition

Both pipelines have tests running in CI simultaneously:

```
make test-v1     → python3 -m pytest tests/ -q
make test-v2     → python3 -m pytest compose/tests/ -q
make test-web    → cd web && npm test
make test        → all three, sequentially
```

`.github/workflows/test.yml` runs all three as separate jobs; any
failure blocks merge. Pre-push hook runs the fast subset of both.

### v1 test sunset sequence

Trigger: **v1 handles 0 customer builds for 60 consecutive days AND
has no unresolved bug reports in the coordinator roadmap.** Only
then does v1 retire.

Sequence (once triggered):
1. Announce to in-flight v1 customers: "Your app will be rebuilt
   on v2 in N days; preview will be sent; confirm or stay on v1
   frozen snapshot."
2. Customers who confirm: rebuild on v2, deploy, verify.
3. Customers who decline: their v1 build is exported as a static
   site snapshot + served from R2 / a frozen deploy slot.
4. `tests/` v1 suite gets moved to tests/legacy/v1/ with
   `@pytest.mark.skip(reason="v1 retired 2026-XX-XX")` decorators.
5. CI dropper: `make test-v1` becomes a no-op (or kept as
   `make test-legacy` for rare git-archaeology needs).
6. `cli/`, `queue/worker_pg.py`'s v1 dispatch, `coordinator/`
   actuators for v1, etc. enter code-freeze (no new commits, no
   security patches). After another 6 months, they can be deleted
   entirely.
7. Tag the sunset commit `v1-retired`.

### What does NOT change

- `web/` test suite stays active throughout. Intake, dashboard,
  admin review, gallery: none of these are v1-specific.
- Worker test suite (`tests/test_worker_pg.py`) stays active; the
  worker serves both v1 and v2 during transition and continues to
  serve v2 after sunset.
- Monitor + deploy tests stay.
- Coordinator tests stay (coordinator's role shrinks but doesn't
  disappear).

### Cost estimate for test migration

- Widget test authoring: 30-60 min per widget × 60-120 widgets
  = 30-120 hours.
- Integration test authoring: ~20-40 hours.
- CI pipeline updates: ~10 hours.
- Shadow validation setup: ~15 hours.
- Total: **~75-185 hours of test work** over the MVP timeline.
  Most of this happens in Phase 3 alongside widget authoring.

### What this does to the 4242 current tests

They stay GREEN during the entire transition. Only after Phase 7
sunset trigger do they move to tests/legacy/v1/ + get
skip-marked. That means developers working on v2 still have full
v1 regression coverage as a backstop: if v2 + v1 both validate
against the same test spec and v1 passes but v2 fails, we have a
clear signal that v2's output is worse than v1's for that case.
