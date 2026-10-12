# CLI build.py: Post-processors

> **Scope:** detailed list of the deterministic fixes applied after Claude-generated code lands in the build dir. The list below is grouped by concern and is not exhaustive: the registry holds 117 `@processor` functions. Extracted from CLAUDE.md to keep the root context file focused on load-bearing decisions. Consult this file when investigating why a specific fix-site behaves the way it does, or when adding a new post-processor.
>
> **Source of truth:** `cli/build.py`. Live count of registered processors:
>
> ```bash
> grep -c '^@processor' cli/build.py
> ```
>
> Each `@processor(after=..., phase=...)` decoration puts the function in the `REGISTRY` in `cli/pipeline.py`, which orders them as a DAG; 60 also run in the `reapply` phase (`reapply_post_processors`), the other 57 only during the build. See `docs/internals/POST_PROCESSOR_ORDERING.md`.

The deterministic fixes run after all phases complete. Grouped below by concern.

**MW v16 prevention additions (2026-04-17):**

Designed from RCA at `docs/internals/MW_BUG_RECURRENCE_RCA.md`. Each addresses a category of bug that recurred across v12→v15.

- `_enforce_admin_view_path_convention()` NEW: routes are ground truth for view paths. Scans `res.render('admin/SLUG')` calls and ensures `views/admin/SLUG.ejs` exists. If not, renames from drift variants (`admin/SLUG-index.ejs`, `admin-SLUG.ejs`, etc.). Catches convention drift (§ 1 of RCA).
- `_ensure_admin_model_handlers()` NEW: dashboard.js must have `GET /admin/{slug}` for every data_model. Auto-injects missing ones (v15 had videos handler missing; resulted in 404). Catches generation incompleteness (§ 6 of RCA).
- `_ensure_settings_middleware_contract()` NEW: server.js must populate `res.locals.settings` (templates use `settings.X`). Aliases from `res.locals.siteSettings` or injects fresh middleware. Catches contract mismatch (§ 2 of RCA).
- `_verify_template_middleware_contract()` NEW: build-time lint that warns when views reference `settings.X` but no middleware populates it. Belt-and-suspenders partner to the ensure function.
- `_ensure_tz_aware_datetime_schema()` NEW: converts `TIMESTAMP` columns (date_time, starts_at, created_at, etc.) to `TIMESTAMPTZ` in schema.sql; anchors naive `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'` seed literals to UTC. Catches timezone drift (§ 3 of RCA).
- Integration test suite (`tests/test_mw_pipeline_integration.py`): runs the full pipeline on a Claude-mimicking input and asserts end-to-end properties. Catches ordering + interaction bugs that unit tests miss.

**MW v13 additions (2026-04-17):**
- `_fix_admin_crud_link_prefix()` NEW: strips `/admin/` from `href|action` attributes targeting data_model CRUD slugs. Prevents the 404 regression where admin list views linked to `/admin/events/new` etc. but routes mounted at `/events/new`. Registered in main pipeline + `reapply_post_processors`.
- `_UPLOAD_ZONE_HTML` + `_ensure_upload_form_ui()._replace_input()`: preview thumbnail now renders at server-side render time when the hidden input's value is non-empty. Uses `<%= ({value_expr}) ? 'block' : 'none' %>` for visibility toggle. `_replace_input` extracts a bare JS expression from the captured EJS value so it can be re-used safely inside outer `<%=`.
- `_strip_dead_signup_links()` NEW, when no server-side route handles `/signup`, `/register`, or `/sign-up`, strips the "Register here" block from login views. Admin-only apps no longer ship with dead 404 links.

**MW v12 additions (2026-04-16):**
- `_privacy_safe_youtube()` gained `_NON_YOUTUBE_VAR_FRAGMENTS` skip filter so Posh/Vimeo/Stripe/Calendly iframes keep their original src (previously got rewritten to empty `youtube-nocookie.com/embed/` URL, killing ticket checkouts).
- `_ensure_accent_color_variety()` NEW: injects `--color-accent-warm` + `--color-accent-earth` CSS vars plus utility classes (`.badge-featured`, `.stat-card-warm`, `.stat-card-earth`, etc.) so generated apps distribute accent usage across three semantic colors instead of defaulting to a single `--color-accent` hue everywhere. Values come from optional `spec.design.accent_2_color` / `accent_3_color`, with muted-gold / muted-terracotta defaults.
- `_fix_delete_button_styling()` gained `DELETE_BUTTON_UPGRADE` block: every `form[action*="/delete"] button[type="submit"]` now renders as destructive-outline (red text on transparent, red-tinted fill on hover) regardless of existing Tailwind classes. Previously solid-blue delete buttons identical to primary CTAs were possible.
- `reapply_post_processors()` now reruns the content-binding layer (`_ensure_upload_route`, `_ensure_upload_form_ui`, `_strip_redundant_image_upload_forms`, `_ensure_site_settings`, `_ensure_settings_in_views`, `_ensure_hero_settings_binding`, `_ensure_contact_email`, `_fix_delete_button_styling`) after QA/design fix loops so admin templates created during fix passes get the same treatment.
- `views_admin_settings.ejs` template rewritten as theme-neutral with semi-dark navy surfaces; only renders inputs for keys actually seeded, eliminating the "generic placeholder form disconnected from spec" regression.


## Seed data normalization

- `_join_multiline_inserts()`: normalizes multi-line `INSERT+VALUES` statements into single lines for uniform downstream processing.
- `_split_seed_inserts()`: splits multi-row INSERTs into individual single-row statements with `ON CONFLICT DO NOTHING` (bad rows fail independently); a second pass comments out INSERTs with unbalanced quotes (prevents psql consuming subsequent statements as string).
- `_dedup_seed_entries()`: removes duplicate seed rows by first quoted value per table (case-insensitive).
- `_fix_seed_column_mismatch()`: detects column/value count mismatches. Truncates extra values, comments out rows with too few (quote/paren-aware counting handles `NOW()` and quoted commas).
- `_fix_seed_uuid_id()`, detects SERIAL/BIGSERIAL/INTEGER id columns in `schema.sql` and strips UUID values (`gen_random_uuid()`, `uuid_generate_v4()`, literal UUID strings, `::uuid` casts) from seed INSERTs into those tables, removes the id column and its value so SERIAL auto-generates sequential integers. Fixes **USER_SEED1**: Claude generates UUID seed values for integer-type id columns, causing type mismatch and empty tables.
- `_ensure_minimum_seed_rows()`: pads data tables below 15 rows by duplicating existing seed rows with modified values (offset dates, appended text suffixes like "evening session", "weekend edition").
- `_fix_seed_admin_email()`: ensures seed data uses spec admin email.

## Site settings + hero binding

- `_ensure_gallery_dedup()`: adds UNIQUE index on `photos.image_url`, caps seed at 12 photos.
- `_ensure_site_settings()`, injects `site_settings` table, seed defaults (**ADMIN_SETTINGS_SUPERSET: 23 canonical keys**, site identity, hero, logo+about, contact block with both new `contact_*` and legacy generic keys, uploadable image `*_url` keys, 6 social URLs), admin settings CRUD route+view, settings middleware (60s cache), dashboard link. Spec values from `spec.settings` or `spec._seed_settings` arrays override generic defaults. The injected `views/admin/settings.ejs` renders one input per seeded key, grouped into Site Identity / Hero / Logo+About / Contact / Social sections, sections hide when no keys of that group are seeded. MW v12 fix for the "hero/about/logo cannot be edited from the admin UI" regression.
- `_ensure_settings_in_views()`: injects conditional social link SVG icons in footer and contact info block.
- `_ensure_hero_settings_binding()`, rewires hero `<h1>` and first `<p>` in `views/index.ejs` + `views/home.ejs` to use `<%= settings.hero_tagline %>` / `<%= settings.hero_description %>` with original text as fallback. Fixes **HERO_SETTINGS1**: Claude hardcodes hero text, ignoring site_settings. Idempotent, skips if already bound.
- `_ensure_contact_email()`: injects Resend email notification after `contact_messages` INSERT (fire-and-forget).

## DB pool + CSRF + auth plumbing

- `_ensure_shared_db_pool()`: creates `db/index.js` with SSL config and patches route files to use the shared pool.
- `_fix_tailwind_config()`: fixes CDN variable name.
- `_fix_csrf_pattern()`: removes `csrf-csrf`/`csurf` and fixes `csrfToken` references.
- `_ensure_validate_csrf_export()`, appends `validateCsrfToken` function + export to `middleware/auth.js` if Claude omitted it (the static `routes/admin.js` template always requires it, missing export causes startup crash).
- `_ensure_csrf_import()`: removes inline `csrfCheck` function definitions from route files and replaces with `const { validateCsrfToken } = require('../middleware/auth')`; updates all `csrfCheck` references to `validateCsrfToken`.
- `_fix_circular_imports()`: redirects `require('../server')` in route files to `require('../middleware/auth')`.
- `_ensure_route_views()`: scaffolds missing `.ejs` files.
- `_strip_demo_credentials()`: removes demo account blocks from login views.
- `_fix_login_form_fields()`: fixes login forms where Claude generates `name="username"` instead of `name="email"` (fixes silent login failures).
- `_ensure_login_completeness()`: detects truncated `login.ejs` (from token continuation issues) and replaces with a complete split-panel login page.
- `_ensure_register_completeness()`: same as above for `register.ejs`.
- `_ensure_admin_mount()`: patches `server.js` to `require routes/admin.js` if missing AND injects the `routes/admin.js` template file itself if absent (single-pass builds may not generate it). Now preserves Claude's GET `/admin` dashboard route during template replacement using broader regex (matches both `router.get('/admin'` and `router.get('/'` patterns), strips `requirePasswordSet` from preserved routes (not in template scope), and injects a fallback dashboard route from schema when no route was preserved but a dashboard view exists.
- `_ensure_error_handling()`: adds `views/500.ejs` error page template and centralized Express error handler.
- `_validate_ejs_templates()`: checks for missing CSRF tokens and mismatched EJS tags (non-blocking warnings).
- `_ensure_package_deps()`: adds `csv-parser` and `multer` to `package.json` if missing.

## Views + HTML normalization

- `_fix_view_html_shells()`: strips full HTML shells from Phase 2 views.
- `_fix_view_slug_paths()`: fixes singular slug paths in EJS views to use correct English plurals.
- `_ensure_logout_form()`: converts GET-based logout links to POST forms with CSRF tokens (security fix).
- `_fix_null_property_access()`, scans route files for unguarded `.rows[0]` property accesses and injects `if (!result.rows.length) return res.status(404)` guards to prevent TypeError crashes. Skips object-literal / function-arg contexts where the matched line ends with `,` or starts with a property-key pattern, injecting a return statement inside `res.render('x', { stats: { key: r.rows[0].count } })` would produce invalid JS. Regression from an event-site build.
- `_fix_brand_nowrap()`: adds `whitespace-nowrap` to brand/logo links, preventing text wrapping in crowded navs.
- `_ensure_mobile_nav()`: adds hamburger menu for mobile. Handles both `hidden md:flex` pattern AND flat flex navs with 4+ links that overflow on mobile.
- `_strip_require_from_ejs()`: removes `require()` calls from EJS templates (Claude sometimes generates `require('url')` which crashes at runtime since EJS context has no `require`).

## Tailwind + fonts + color system

- `_ensure_tailwind_compiled()`, replaces Tailwind CDN with compiled CSS (adds `tailwindcss` dependency, `tailwind.config.js`, postinstall build script). Now uses `get_design_context()` for UUPM-aware color/font resolution. When `tailwind.config.js` already exists but lacks `fontFamily`, injects `display`/`body`/`brand` entries matching the loaded Google Fonts (Claude often generates colors-only configs, missing `fontFamily` means `font-body`/`font-display` classes produce no output).
- `_ensure_font_loading()`: injects Google Fonts link tags, falls back to `get_design_context()` when `spec.design` lacks explicit font fields.
- `_ensure_color_consistency()`: patches CSS custom properties to match spec design colors, uses `get_design_context()` for UUPM-aware color resolution instead of raw `spec.design`.

## Dark-theme contrast fixes

- `_fix_admin_dark_stat_cards()`, fixes dark-on-dark contrast in ALL `views/` files when primary color luminance < 0.15 AND body is `bg-slate-900`/dark. Replaces `text-primary`→`text-white`, `bg-primary/5|10|20|25`→`bg-white/10`, `color:var(--color-primary)` inline style→`color:#ffffff`, `text-gray-900/800/700`→`text-slate-200/300/400`, `bg-slate-50`→`bg-slate-700/50`, `bg-gray-50`→`bg-slate-700/50` (light card panels inside dark sections) across all view files including subdirectories (`views/admin/settings.ejs`) and non-admin files (`views/contact-submissions-detail.ejs`). Skips `layout.ejs`/`login.ejs`/`register.ejs` which have intentional dark-panel designs. Prevents design review critical failures from invisible navy text on dark pages, root cause of early event-site (MW v2) build failures 2026-04-09.
- `_fix_dark_section_text()`: complements `_fix_admin_dark_stat_cards` for apps with LIGHT body but dark-primary gradient **sections** (e.g. `from-primary` newsletter section on `contact.ejs`). Depth-tracks open `<section>/<div>` with from-primary/via-primary/bg-primary class **OR inline `style="…linear-gradient(…)…"`** (broadened 2026-04-14 after an MW v7 design review failure: Claude emitted contact/about/admin-settings hero headers with inline `rgba()` gradients instead of Tailwind classes, and dark `h1` text was missed). Rewrites dark text (`text-slate-900/800/700`, `text-gray-900/800/700`) → `text-white` within those containers. Only fires when primary luminance < 0.15. Skips layout/login/register. Fixes **DARK_TEXT_GRADIENT1** + **INLINE_GRADIENT_DARK_TEXT1**.
- `_ensure_nav_glassmorphism()`: adds glassmorphism to first `<nav>` in `layout.ejs` when primary is dark: computes `rgba(r,g,b,0.92)` from hex primary, injects `backdrop-blur-[20px] backdrop-saturate-[1.4]` classes + inline `background-color + backdrop-filter + -webkit-backdrop-filter` style. Idempotent (bails if `backdrop-blur` already present).
- `_ensure_nav_solid_surface()`: bails when inline background-color already present so the two processors compose cleanly. Both registered in main chain AND `reapply_post_processors()`.

## Media + assets

- `_ensure_stock_photos()`, copies real stock photos from `cli/stock_photos/` (103 photos, 19 categories incl. wellness, wellness pool expanded with event/ceremony/portrait photos 2026-04-09; in this public export the photos are replaced by generated placeholders, see `cli/stock_photos/README.md`) into `public/images/stock/` when category matches spec description. Replaces fake `/images/gallery/...` paths in `seed.sql` with real `/images/stock/...` paths (or NULL when no category matches), replaces broken local image paths in EJS with SVG placeholders, injects `onerror` fallback on dynamic EJS image tags, and adds server-side `/images/:type(gallery|stock)/*` SVG placeholder route to `server.js`.
- `_ensure_nav_logo()`, rewires the first `<nav>` in `views/layout.ejs` to use `public/logo.png`, locates the brand anchor containing an inline `<svg viewBox>` or gradient-background `<div>` and replaces only that brand mark with `<img src="/logo.png" alt="{business_name}" class="w-10 h-10 md:w-12 md:h-12 rounded-full object-cover">`. Preserves sibling wordmark span, idempotent (bails if `<img src="/logo.png"` already present), footer-safe (only first nav block), bails if `public/logo.png` missing. Fixes **NAV_LOGO1**: Claude emits hardcoded gradient-circle SVG and the real customer logo PNG is orphaned from the nav in every build.
- `_strip_broken_logo_refs()`: removes `<img src="/logo.png">` tags and favicon links from EJS when no logo asset exists in the build (prevents broken-image icons on login, register, nav). Replaces logo favicon with `/favicon.svg` fallback.
- `_ensure_logo_placement()`: injects logo into nav/login/register/footer and replaces the favicon link. Favicon substitution uses `[^<>]*` (not `[^>]*`) to skip EJS-expression hrefs like `href="<%= siteSettings.nav_logo_url %>"` which contain `>` inside `%>` and would leave a dangling `">` text fragment. Fixes **EJS_FAVICON1**: an MW v6 build rendered `">` characters at top of every page. Also strips EJS-conditional favicon blocks via `[^\n]*`-aware regex after replacing static ones.
- `_ensure_video_embeds()`: replaces rickroll/placeholder video IDs (`dQw4w9WgXcQ` etc.) and duplicate IDs with curated fallbacks, converts embed format (index thumbnail links vs detail nocookie iframe, CSP patching), injects ad-blocker-safe `/api/video-thumb/:videoId` proxy route (server-side fetch from `i.ytimg.com` with SVG fallback), and Phase 3 rewrites `<img src="https://img.youtube.com/vi/{id}/...">` and `<img src="https://i.ytimg.com/vi/{id}/...">` to `/api/video-thumb/{id}` in all EJS files (handles both hardcoded IDs and dynamic EJS expressions like `<%= video.video_id %>`).

## Upload pipeline

- `_ensure_upload_route()`: injects image upload backend (`routes/upload.js` with multer+sharp auto-resize to 1200px max progressive JPEG quality 82, uploads table with base64 storage, `server.js` mount) when spec has image/photo models.
- `_ensure_upload_form_ui()`, replaces `<input type="text|url" name="image_url">` (and `photo_url`, `avatar_url`, `cover_image`, `about_photo_url`, `nav_logo_url`, `hero_image_url`, `banner`, `thumbnail`, `picture`, `icon`, broadened 2026-04-13 with optional `_url` suffix + `type="url"` match) in new/edit `.ejs` forms with a drag-and-drop file upload zone. Injects hidden input + upload zone HTML + client-side JS (`cli/templates/image-upload.js`) that POSTs to `/api/upload` with CSRF token (`x-csrf-token` header), sends file as `fd.append('file', file)` (matching multer `upload.single('file')`), checks `data.url` directly in response (backend returns `{url}`, no `success` key), shows progress bar and preview, validates file size (5MB) and type (JPEG/PNG/WebP/GIF), and populates hidden field with returned URL. Edit forms show current image preview with Remove button. Uses EJS sentinel technique to prevent `%>` from breaking HTML tag regex matching. Guarded by a name-keyword whitelist so generic url fields (`instagram_url`, `rsvp_link`) and adjacent text fields (`image_alt`, `logo_text`) are skipped.
- `_strip_redundant_image_upload_forms()`: removes parallel `<form enctype="multipart/form-data">` blocks with `<input type="file" accept="image/*">` from any `.ejs` view that already has an `image-upload-zone` injected. Prevents MW v6 regression where admin settings pages rendered both the drag-drop zone AND a legacy "Choose File / Upload" submit form for the same field (logo, hero, about_photo), giving customers two competing upload UIs.

## Deployment + infra hygiene

- `_ensure_railwayignore()`: adds `.railwayignore` excluding `node_modules`/`.git`/`.log` (prevents accidental upload via `railway up`).
- `_ensure_magic_link_hash()`: fixes magic link token hashing for cross-language consistency.
- `_ensure_magic_link_auth()`, also makes sure `routes/auth.js` POST `/login` carries the build-time `TEST_MODE` shortcut so the smoke tests can authenticate with `test_password_for_ci`. The shortcut is gated: `_harden_test_mode_gates` rewrites every `process.env.TEST_MODE &&` condition to also require `process.env.NODE_ENV !== 'production'`, and `_ensure_test_mode_production_guard` injects a startup guard into `server.js` that exits when `TEST_MODE` is set while `NODE_ENV` is `production`. Both are applied deterministically (not left to the prompt); `deploy.py` sets `NODE_ENV=production` and never sets `TEST_MODE`, and the test runner starts the app with `NODE_ENV=test`.
- `_ensure_mobile_responsive()`: adds viewport meta and responsive utilities.
- `_ensure_resend_from_address()`: fixes Resend from-address format.
- `_fix_grid_classes()`: fixes Tailwind grid class issues.

## Pipeline quality sprint (8→9 rating)

- `_ensure_compression()`: adds `compression` npm package to dependencies and injects `app.use(require('compression')())` before `express.static` in `server.js` (60-70% smaller responses).
- `_ensure_static_caching()`: patches `express.static('public')` to include `{ maxAge: '7d' }` for browser caching (skip-check scoped to `express.static` context to avoid false positive from session store `maxAge`).
- `_ensure_lazy_loading()`: adds `loading="lazy"` to `<img>` tags in EJS views, skips images inside hero sections.
- `_strip_duplicate_confirm_handlers()`: removes Claude-generated `document.addEventListener('click'|'submit', ...)` blocks whose body calls `confirm(` (uses a brace-depth scanner for arbitrary nesting). Runs BEFORE submit_guard so the canonical listener is the single source of confirmation dialogs. Leaves mobile menu/scroll/resize listeners untouched. Fixes **DELETE_CONFIRM1** regression where a single delete button click fired 2-3 confirmation dialogs.
- `_ensure_submit_guard()`: injects double-submit prevention script into `layout.ejs` before `</body>` (disables button, shows "Processing...", respects `data-no-submit-guard` and `data-confirm`).
- `_ensure_dynamic_canonical()`: injects `res.locals.canonicalUrl` into `server.js` user-context middleware and replaces hardcoded `<link rel="canonical" href="/">` with EJS variable (uses `res.locals` because `req` is not available in `express-ejs-layouts` context).
- `_ensure_mobile_menu_close()`: adds click event listeners on `#mobileMenu a` links to close the hamburger menu on nav link click.
- `_ensure_skip_to_content()`: injects visually hidden skip-to-content link after `<body>` (inline CSS for `sr-only` with `onfocus`/`onblur` visibility toggle since Tailwind `sr-only` class may not be in compiled CSS) and adds `id="main-content"` to `<main>` element (WCAG 2.1 AA).
- `500.ejs` template updated to premium design system (section-warm, card-premium, SVG error icon, btn-primary).

## Iteration hardening (6 fresh builds)

Covered above: `_strip_require_from_ejs`, `_ensure_minimum_seed_rows`, and `_ensure_admin_mount` enhancements.
