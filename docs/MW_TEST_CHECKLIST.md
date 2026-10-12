# Medicine Waves: Exhaustive Test Checklist

> Snapshot of 2026-04-17 (post-v13 QA, pre-v14 planning), kept as an example of a regression checklist. The "still open" and "NEW for v15" entries describe MW builds v12 to v15 of the v1 engine as they were then; they were not re-checked against the frozen v1 code or the v2 engine. The canonical spec it builds from is `tests/golden/medicine-waves-canonical/`.

**Purpose:** Living test doc for every MW build. Runs before ANY v?? is deemed ready. Always add new tests when a regression is found so it never slips again.

**Owners:** CAS pipeline engineer (Claude) + Ocean.
**Last major update:** 2026-04-17 (post-v13 QA, pre-v14 planning).

---

## How to use this doc

1. Build MW v?? locally (seed with the canonical MW spec under `tests/golden/medicine-waves-canonical/`).
2. Walk through every test below in order.
3. Mark each as ✅ / ❌ / ⚠️ / ⏭️ (skipped, with reason).
4. Any ❌ must have a ticket or a known-followup entry.
5. Any NEW regression found during QA gets added to this doc in the "Regression-based" section so the next build catches it automatically.

Test priority: **P0** = blocks release, **P1** = must-fix before prod, **P2** = should-fix, **P3** = nice-to-have.

---

## A. Public pages (desktop 1440×900)

### A1. Home (`/`)

| # | Test | Priority |
|---|------|----------|
| A1.1 | HTTP 200 | P0 |
| A1.2 | Nav logo renders (real MW PNG, rectangular, NOT circular-cropped) | P0 |
| A1.3 | Hero tagline = `A somatic sound journey held inside a dome` (from site_settings.hero_tagline) | P0 |
| A1.4 | Hero description visible, from `site_settings.hero_description` | P0 |
| A1.5 | Hero has either a background image (hero_image_url) OR a multi-stop radial gradient using 2+ accent colors | P1 |
| A1.6 | "Upcoming Events" section shows 3 featured/soonest events | P1 |
| A1.7 | Sample Featured Event card shows a **Featured** badge in sun-yellow (`#c9a84c`-ish, NOT cornflower blue) | P1 |
| A1.8 | Video section renders iframe with valid YouTube ID `<video-id>` (NOT empty `youtube-nocookie.com/embed/`) OR hides entirely when no videos exist | P0 |
| A1.9 | CTA section at bottom with button | P2 |
| A1.10 | Footer 3-column: brand, Explore nav, Connect social icons | P1 |
| A1.11 | Footer has Instagram + Facebook icons (from site_settings) | P1 |
| A1.12 | No console errors (ignore Google Fonts network sandbox errors) | P1 |
| A1.13 | Background bg-slate-950 or spec primary, NOT flat white | P1 |

### A2. Events list (`/events`)

| # | Test | Priority |
|---|------|----------|
| A2.1 | HTTP 200 | P0 |
| A2.2 | "Upcoming Events" section heading | P1 |
| A2.3 | "Past Events" section heading | P1 |
| A2.4 | Upcoming events show in ascending date order | P1 |
| A2.5 | Past events show in descending date order | P2 |
| A2.6 | Each card has image, title, date, location | P1 |
| A2.7 | Each card has **Get Tickets** button (color: sun-yellow accent-warm) | P1 |
| A2.8 | Each card has **Learn More** / **View Details** secondary button | P2 |
| A2.9 | Admin-only: each card has **Edit** link + **Delete** button (destructive-outline red) | P1 |
| A2.10 | Sample Featured Event card has visible **Featured** badge in sun-yellow | P1 |
| A2.11 | Dates display in local Central time, Sample Sound Journey = "Jan 11, 2030, 6:00 PM CST" NOT "Jan 12, 12:00 AM CST" | P1, KNOWN TZ BUG |
| A2.12 | Ticketing embed iframe compact on list page for events with `posh_embed_url` set | P2; spec-compliance |

### A3. Event detail (`/events/:id`)

| # | Test | Priority |
|---|------|----------|
| A3.1 | HTTP 200 on all 6 events | P0 |
| A3.2 | Title, description, date, location all render | P0 |
| A3.3 | Hero image renders | P1 |
| A3.4 | Date displays in CDT (correct conversion from stored UTC) | P1; KNOWN TZ BUG |
| A3.5 | "Back to Events" link navigates to /events | P2 |
| A3.6 | "Featured Event" badge visible on Sample Featured Event (sun yellow) | P1 |
| A3.7 | Get Tickets button (top) uses `rsvp_link` URL, opens `target="_blank"` | P1 |
| A3.8 | **TICKETING EMBED IFRAME RENDERS** on Sample Featured Event; src contains `tickets.example/embed/checkout` NOT `youtube-nocookie.com/embed/` | **P0 (core feature)** |
| A3.9 | iframe height matches `posh_embed_height` from DB (default 700) | P2 |
| A3.10 | Events without posh_embed_url hide the embed section entirely (no empty white box) | P1 |

### A4. Gallery (`/gallery`)

| # | Test | Priority |
|---|------|----------|
| A4.1 | HTTP 200 | P0 |
| A4.2 | "Captured Moments" or similar section eyebrow (plain text, NOT pill/badge) | P2 |
| A4.3 | 12 photos render in a grid (will be 10 on local dev DB due to UNIQUE collision; acceptable) | P1 |
| A4.4 | Each photo has caption text below | P2 |
| A4.5 | Images load without 404s | P0 |

### A5. Videos (`/videos`)

| # | Test | Priority |
|---|------|----------|
| A5.1 | HTTP 200 | P0 |
| A5.2 | 1 video embed (the seeded sound-bath video) | P1 |
| A5.3 | iframe src = `https://www.youtube-nocookie.com/embed/<video-id>` (NOT the .com/ variant) | P0 |
| A5.4 | Title + description render below video | P2 |
| A5.5 | "No videos yet" message shown when videos empty (not tested on MW; always seeded) | P3 |

### A6. About (`/about`)

| # | Test | Priority |
|---|------|----------|
| A6.1 | HTTP 200 | P0 |
| A6.2 | about_title = `About Medicine Waves` from settings | P1 |
| A6.3 | about_body 3-paragraph Ocean's story renders from settings | P0 |
| A6.4 | about_photo_url image displays (a stock portrait by default) | P1 |
| A6.5 | Two-column layout (photo | text) on desktop | P2 |

### A7. Contact (`/contact`)

| # | Test | Priority |
|---|------|----------|
| A7.1 | HTTP 200 | P0 |
| A7.2 | "Get In Touch" heading | P1 |
| A7.3 | contact_email renders from settings (`mw-admin@craftandship.example`) | P1 |
| A7.4 | contact_phone renders (`555-0100`) | P1 |
| A7.5 | contact_address renders (`City, ST`) | P2 |
| A7.6 | Contact form with name, email, message | P0 |
| A7.7 | Submit form → success message + row in contact_messages table | P0 |
| A7.8 | POST works WITHOUT login (PUBLIC_FORM_PAGES) | P0 |

---

## B. Auth + nav

### B1. Login (`/login`)

| # | Test | Priority |
|---|------|----------|
| B1.1 | HTTP 200 | P0 |
| B1.2 | Split-panel design (brand left, form right) on desktop (Impeccable rule 1) | P1 |
| B1.3 | Brand name in Cormorant italic on left panel | P2 |
| B1.4 | Email + password fields + Sign In button | P0 |
| B1.5 | POST valid creds → 302 to `/admin` | P0 |
| B1.6 | POST invalid creds → error message on page | P1 |
| B1.7 | "Register here" link, IF present, should not link to 404 (MW spec has no signup, link should be removed or guarded) | P1, KNOWN |

### B2. Nav behavior

| # | Test | Priority |
|---|------|----------|
| B2.1 | All nav items (Home, Events, Gallery, Videos, About, Contact) visible on desktop | P1 |
| B2.2 | "Dashboard" + "Logout" links appear ONLY when logged in | P1 |
| B2.3 | Nav logo + brand link to `/` | P2 |
| B2.4 | Active page indicator on current route | P2 |
| B2.5 | Mobile hamburger menu shows all 7 items + Login/Logout | P1 |

---

## C. Admin pages (auth required)

### C1. Admin dashboard (`/admin`)

| # | Test | Priority |
|---|------|----------|
| C1.1 | HTTP 200 with admin session | P0 |
| C1.2 | 4 stat cards: Events, Photos, Videos, Messages with correct counts | P1 |
| C1.3 | Stat cards use varied accents (at least 1 sun-yellow, 1 sun-orange, 1 cornflower, NOT all blue) | P2 |
| C1.4 | Action tiles/links lead to /admin/{model} list pages | P0 |
| C1.5 | Site Settings tile/button links to /admin/settings | P0 |
| C1.6 | Recent Messages preview section with snippets | P2 |
| C1.7 | Upcoming Events preview section | P2 |

### C2. Admin events list (`/admin/events`)

| # | Test | Priority |
|---|------|----------|
| C2.1 | HTTP 200 with admin session | P0 |
| C2.2 | 6 events listed with Title/Date/Location/Featured/Actions columns | P1 |
| C2.3 | **"Create Event" / "Add New" button links to working URL, NOT 404** | **P0, PREVIOUSLY REGRESSED** |
| C2.4 | Each row's **Edit** link works (HTTP 200, NOT 404) | **P0; PREVIOUSLY REGRESSED** |
| C2.5 | Each row's **Delete** button is red-outline destructive | P1 |
| C2.6 | Delete form submit actually deletes + redirects | P0 |
| C2.7 | Featured star/badge visible on Sample Featured Event row | P2 |

### C3. Admin event NEW (`/events/new` OR `/admin/events/new`)

| # | Test | Priority |
|---|------|----------|
| C3.1 | HTTP 200 | P0 |
| C3.2 | All fields present: title, description, date_time, location, image, rsvp_link, posh_embed_url, posh_embed_height (default 700), is_featured | P0 |
| C3.3 | **Image field is a drag-and-drop upload zone** (NOT `<input type="url">`) | **P0; PREVIOUSLY REGRESSED** |
| C3.4 | Form submit POST creates row in events table | P0 |
| C3.5 | After create, redirects to events list with success flash | P1 |
| C3.6 | CSRF token present | P0 |

### C4. Admin event EDIT (`/events/:id/edit`)

| # | Test | Priority |
|---|------|----------|
| C4.1 | HTTP 200 on every event | P0 |
| C4.2 | All fields pre-populated with existing values | P0 |
| C4.3 | **Image upload zone shows PREVIEW of current image** (thumbnail above Choose File) | **P0; NEW TEST** |
| C4.4 | posh_embed_url + posh_embed_height editable | P1 |
| C4.5 | Change value + submit → DB updated, redirect to events list | P0 |
| C4.6 | Cancel button returns to events list | P2 |

### C5. Admin photos list + new + edit

| # | Test | Priority |
|---|------|----------|
| C5.1 | `/admin/photos` HTTP 200, shows 12 (or 10) photos | P1 |
| C5.2 | New Photo link → 200 (not 404) | **P0; PREVIOUSLY REGRESSED** |
| C5.3 | Edit Photo link → 200 (not 404) | **P0; PREVIOUSLY REGRESSED** |
| C5.4 | **Photo image field is upload zone (NOT url text field)** | P0 |
| C5.5 | Edit form shows **preview of current image** thumbnail | P0 |
| C5.6 | Delete works, row removed from DB | P0 |

### C6. Admin videos list + new + edit

| # | Test | Priority |
|---|------|----------|
| C6.1 | `/admin/videos` HTTP 200, shows 1 video | P1 |
| C6.2 | New Video link → 200 | **P0; PREVIOUSLY REGRESSED** |
| C6.3 | Edit Video link → 200 | **P0; PREVIOUSLY REGRESSED** |
| C6.4 | embed_url is text input (YouTube URL, NOT upload) | P1 |
| C6.5 | Title + description editable | P1 |
| C6.6 | Delete works | P0 |

### C7. Admin contact messages (`/admin/contact-messages` OR `/contact-messages`)

| # | Test | Priority |
|---|------|----------|
| C7.1 | HTTP 200 | P0 |
| C7.2 | Shows all contact_messages with name, email, snippet, timestamp | P1 |
| C7.3 | **"Add New" button should NOT be present** (spec says read-only) | P1; KNOWN REGRESSION |
| C7.4 | **"Edit" link should NOT be present** | P1; KNOWN REGRESSION |
| C7.5 | Delete button (admin-only) is destructive-outline red | P2 |
| C7.6 | Path is `/admin/contact-messages` for consistency (current is `/contact-messages`) | P3 |

### C8. Admin settings (`/admin/settings`)

| # | Test | Priority |
|---|------|----------|
| C8.1 | HTTP 200 | P0 |
| C8.2 | Uses dark theme surfaces (admin-settings-card), matches rest of app | P0 |
| C8.3 | **Section: Site Identity**, site_name, site_url (optional site_description) | P1 |
| C8.4 | **Section: Hero**, hero_tagline input + hero_description textarea + hero_image upload | P0 |
| C8.5 | **Section: Logo & About**, nav_logo upload + about_title + about_body textarea + about_photo upload | P0 |
| C8.6 | **Section: Contact**, contact_email + contact_phone + contact_address (NOT just business_name/phone) | P0 |
| C8.7 | **Section: Social Media**, 6 URL inputs (Instagram/Facebook/YouTube/TikTok/Twitter/LinkedIn) | P1 |
| C8.8 | **3 upload zones** for nav_logo_url, hero_image_url, about_photo_url | **P0; PREVIOUSLY REGRESSED** |
| C8.9 | **Each upload zone shows PREVIEW of current image** when a URL is set | **P0; NEW TEST** |
| C8.10 | Values pre-populated from seeded site_settings | P0 |
| C8.11 | Save → success message + DB updated | P0 |
| C8.12 | After save, public pages reflect new values (hero tagline on `/`, contact email on `/contact`) | P1 |

---

## D. Cross-page integration

### D1. Upload flow end-to-end

| # | Test | Priority |
|---|------|----------|
| D1.1 | Upload a new logo via `/admin/settings` → `site_settings.nav_logo_url` updated to `/uploads/<uuid>` | P0 |
| D1.2 | After upload, nav logo on public pages (home, events, etc.) shows the NEW uploaded image | P0 |
| D1.3 | Upload a new hero image → home hero background updates | P1 |
| D1.4 | Upload new about photo → `/about` page updates | P1 |
| D1.5 | Upload a new event image via admin event edit → event card + detail show new image | P0 |

### D2. Content update flow end-to-end

| # | Test | Priority |
|---|------|----------|
| D2.1 | Change hero_tagline in admin settings → home page shows new tagline immediately after page reload | P0 |
| D2.2 | Change about_body in admin settings → /about page reflects | P1 |
| D2.3 | Edit event title in admin → events list + detail show new title | P0 |
| D2.4 | Edit posh_embed_height in admin → iframe height on detail page matches | P1 |
| D2.5 | Toggle is_featured on an event → home "Upcoming Events" + events list badge updates | P1 |

---

## E. Console + network + CSP

| # | Test | Priority |
|---|------|----------|
| E1.1 | No 4xx/5xx network errors on home page load (static resources OK) | P1 |
| E1.2 | No JavaScript errors in console on any public page | P1 |
| E1.3 | CSP allows the ticketing host (`tickets.example` in the canonical spec) in frame-src (iframes load) | P0 |
| E1.4 | CSP allows `youtube-nocookie.com` in frame-src | P1 |
| E1.5 | Service worker registers without errors | P3 |

---

## F. Accessibility basics

| # | Test | Priority |
|---|------|----------|
| F1.1 | Every `<img>` has non-empty alt attribute | P1 |
| F1.2 | Single H1 per page | P2 |
| F1.3 | Heading hierarchy: no skipped levels | P2 |
| F1.4 | Interactive elements have visible focus rings on keyboard Tab | P2 |
| F1.5 | Forms have `<label for>` associations | P2 |
| F1.6 | Contact form works with keyboard only | P2 |

---

## G. Mobile viewport (375×812)

| # | Test | Priority |
|---|------|----------|
| G1.1 | Home renders without horizontal scroll | P0 |
| G1.2 | Hamburger menu opens, shows all 7 nav items | P1 |
| G1.3 | Event cards stack vertically, text legible | P1 |
| G1.4 | Forms (contact, login) usable on mobile | P1 |
| G1.5 | Admin settings form usable on mobile | P2 |
| G1.6 | Sample Featured Event ticketing iframe visible (no layout break) | P1 |

---

## H. Color palette + design-system guardrails

| # | Test | Priority |
|---|------|----------|
| H1.1 | Body background near `#070924` / `#020617` (spec primary / slate-950), NOT flat white | P1 |
| H1.2 | Accent color `#5a80c0` used on primary CTA buttons, links | P2 |
| H1.3 | Sun yellow `#c9a84c` visible on at least 1 UI element (Featured badge, Get Tickets, price emphasis) | P1 |
| H1.4 | Sun orange `#a87850` visible on at least 1 UI element (warm section marker, highlight) | P2 |
| H1.5 | NO single `--color-accent` blue used on every stat card / badge / marker | P2 |
| H1.6 | Typography: Cormorant Garamond on h1/h2/h3, DM Sans on body | P1 |
| H1.7 | Cards have rounded corners (≥ 0.5rem) and subtle borders | P2 |
| H1.8 | No gradient text on headings (Impeccable rule 12) | P2 |

---

## I. Regression-based (add NEW tests here when a bug slips through)

This section grows over time. Every new bug → a permanent test that catches it on the next build.

- **TICKET_IFRAME_REGRESSION** (found in v12, fixed in v13): event detail page with `posh_embed_url` set MUST render iframe with src containing the ticketing embed path (`tickets.example/embed/checkout/` in the canonical spec); never `youtube-nocookie.com/embed/<empty>`. Test = A3.8.
- **ADMIN_SETTINGS_GENERIC_KEYS** (found in v12, fixed in v13): admin settings form must include inputs matching spec settings keys (`hero_tagline`, `hero_description`, `about_body`, `contact_email/phone/address`, `nav_logo_url`, `hero_image_url`, `about_photo_url`). Tests = C8.3–C8.8.
- **UPLOAD_UI_MISSING** (found in v12, fixed in v13): `*_url` image fields in admin forms must be upload zones, not URL text fields. Tests = C3.3, C4.3, C5.4, C8.8.
- **ADMIN_CRUD_404_PREFIX** (found in v13, NEW for v14): admin list views link to `/admin/events/*`, `/admin/photos/*`, `/admin/videos/*` but routes mount at `/events/*` etc. Result: every Edit/New/Delete link 404s. Tests = C2.3, C2.4, C3.1, C4.1, C5.2, C5.3, C6.2, C6.3.
- **ADMIN_IMAGE_PREVIEW_MISSING** (found in v13, NEW for v14): admin settings upload inputs + photo edit form should show a thumbnail preview of the current stored image above the Choose File button when a value is set. Tests = C4.3, C5.5, C8.9, D1.1.
- **TIMEZONE_DISPLAY_INCONSISTENT** (found in v12, still open): event times show UTC-as-CDT inconsistently across views. Tests = A2.11, A3.4.
- **CONTACTMESSAGE_NOT_READONLY** (found in v12, still open): ContactMessage admin shows Add New + Edit despite spec `read-only`. Tests = C7.3, C7.4.
- **DELETE_BUTTON_PRIMARY_STYLING** (found in v12, fixed in v13): Delete buttons must render destructive-outline (red text, transparent bg, red border hover), not solid primary blue. Tests = C2.5, C5.6 visual.
- **VIDEO_SECTION_EMPTY_IFRAME** (found in v12, fixed in v13): home page video section must have a valid YouTube video ID in its iframe src OR hide the section entirely. Test = A1.8.
- **SIGNUP_404_DEAD_LINK** (found in v12, still open): login page "Register here" link goes to `/signup` which 404s because spec has no signup. Test = B1.7.
- **UPLOAD_UI_ORDERING_BUG** (found in v14, NEW for v15): `_ensure_upload_form_ui` runs BEFORE `_ensure_site_settings` injects the admin/settings.ejs template. Result: settings form image fields (`nav_logo_url`, `hero_image_url`, `about_photo_url`) render as `<input type="url">` text boxes instead of drag-and-drop upload zones. Event edit form still works correctly. Fix: call `_ensure_upload_form_ui` a SECOND time AFTER `_ensure_site_settings` in the main pipeline, OR move `_ensure_site_settings` to run before `_ensure_upload_form_ui`. Test = C8.8, C8.9.
- **TIMEZONE_HELPER_PARTIAL_WIRE** (found in v14, NEW for v15): `_ensure_event_time_helper` injects `app.locals.formatEventTime` into server.js, but the view rewrite regex `date_time_var_re` only matched `views/events/show.ejs` in v14 (1 view). Event list cards (`views/events/index.ejs`) and home page upcoming events (`views/home.ejs` or `views/index.ejs`) still format `new Date(event.date_time).toLocaleString(...)` without the helper, so Sample Featured Event still displays "12:00 AM CDT" on the event day instead of "7:00 PM CDT" the previous day. Fix: broaden the regex to match `new Date(...)` + `.toLocaleString(...)` chains and event cards inside `forEach` blocks. Tests = A1.7, A2.11, A3.4.
- **VIDEO_EDIT_404** (found in v14, NEW for v15): `/videos/:id/edit` returns 404 on valid video ID, and `/admin/videos/new` also 404s, even though `/videos/new` and `/videos` work. Routes/videos.js likely missing the edit endpoint or registering with a different param. Tests = C6.2, C6.3.

---

## v16 defects (all NEW regressions for v17 to fix)

- **SEED_EVENT_IMAGE_NULL** (found v16): pipeline seed.sql has `image_url = NULL` on all event INSERTs. Event cards render without hero images. Integration test: `db/seed.sql` event INSERTs must all have non-null image_url. Apply stock_photo rotation if spec lacks `_seed_events[].image_url`.
- **SEED_PHOTO_NOT_NULL_FAIL** (found v16): photos table `image_url NOT NULL` + pipeline seed has NULL image_url → all photo INSERTs fail, 0 photos in DB. Integration test: after full seed, `SELECT COUNT(*) FROM photos` must be > 0.
- **STUB_VIEW_TEXT** (found v16): `views/photos/edit.ejs` and possibly others contain literal stub `"Content for Photos/Edit will appear here."`; renders unusable page. Integration test: assert no view file contains literal `"Content for"` placeholder text. Auto-replace with canonical CRUD form.
- **STUB_EVENT_NEW** (found v16): `/events/new` renders heading + "Back" link but no form. Admin can't create events. Integration test: `GET /events/new` HTML must contain `<form>` + inputs matching spec model fields.
- **MESSAGES_ROUTE_404** (found v16): `/messages` returns 404 but is linked from admin dashboard. Integration test: crawl all `<a href>` from admin views, assert each resolves.
- **DELETE_CONFIRM_DOUBLE_BIND** (found v16): `<form data-confirm="..." onsubmit="return confirm(...)">` binds both mechanisms → two dialogs. Integration test: no form has both `data-confirm` attr AND `onsubmit=confirm` inline.
- **DELETE_CONFIRM_MISSING** (found v16): global `data-confirm` JS handler sometimes doesn't catch the submit because page script loads after the user clicks. Integration test: handler registers on DOMContentLoaded OR is inline in `<head>`.
- **BUTTON_STYLE_INCONSISTENT** (found v16): Unfeature uses white pill, Delete uses red outline, Edit uses plain text, no unified design system. Visual test: screenshot all button types + assert single consistent style per role.
- **ADMIN_SETTINGS_INPUT_CONTRAST** (found v16): input values on `/admin/settings` are near-unreadable (likely white text on white background due to Tailwind class drift). Computed-color test: every input's color vs background must meet WCAG AA (>= 4.5:1).

---

## J. Seed / artifact prep (run before any QA)

| # | Step | Priority |
|---|------|----------|
| J1 | Copy `cli/stock_photos/wellness/*.jpg` → `<artifact>/public/images/stock/wellness/` | P0 |
| J2 | Copy `cli/assets/mw_logo.png` → `<artifact>/public/images/mw_logo.png` | P0 |
| J3 | Create isolated DB `mw_v??_local` | P0 |
| J4 | Boot on unique port (4700, 4800, etc.) | P0 |
| J5 | Apply `/tmp/mw_v??_seed.sql` (users + events + photos + videos from canonical) | P0 |
| J6 | UPDATE site_settings for nav_logo_url, hero_image_url, about_photo_url | P0 |
| J7 | UPDATE CAS DB `builds.preview_url` to `http://localhost:47??` | P1 |

---

## v17 defects: all SHIPPED as pipeline fixes (2026-04-17)

All 11 defects below now have matching post-processors in `cli/build.py` +
failing-first TDD tests + visual verification on `localhost:5101`.

- **SEED_EVENT_IMAGE_NULL / SEED_PHOTO_NOT_NULL_FAIL** (v16 → v17 FIX A): `_ensure_model_seed_images` fills NULL image_url in every seed INSERT for tables whose schema declares `image_url`.
- **STUB_VIEW_TEXT / STUB_EVENT_NEW** (v16 → v17 FIX B): `_ensure_crud_forms_complete` detects stub templates and regenerates a canonical form from `spec.data_models`.
- **MESSAGES_ROUTE_404** (v16 → v17 FIX C): `_fix_route_link_drift` rewrites unresolved `<a href>` to the closest mounted route prefix.
- **DELETE_CONFIRM_DOUBLE_BIND / DELETE_CONFIRM_MISSING** (v16 → v17 FIX D): `_ensure_delete_confirm_single_handler` collapses to one handler.
- **BUTTON_STYLE_INCONSISTENT** (v16 → v17 FIX E): `_ensure_button_design_system` ships `.btn-primary/.btn-secondary/.btn-ghost/.btn-destructive/.btn-warm`.
- **ADMIN_SETTINGS_INPUT_CONTRAST** (v16 → v17 FIX F): `cli/templates/views_admin_settings.ejs` now has dark-theme overrides for `.input-premium`/input/textarea/select.
- **HOME_VIDEO_ERROR_153** (v17 FIX G): `_ensure_home_video_click_to_play` replaces home-page inline iframe with thumbnail click-through; `_INDEX_PATTERNS` now includes "home"/"landing".
- **DEAD_REGISTER_LINK_VS_SPEC** (v17 FIX H): `_strip_dead_signup_links` consults `spec.pages` first, spec intent wins over route existence.
- **STAT_CARD_ACCENT_WEAK** (v17 FIX I): `_ensure_stat_card_accent_variety` thickens borders + colorizes stat numbers.
- **ABOUT_PHOTO_TINY_SVG** (v17 FIX J): `_ensure_about_photo_fallback` picks a stock portrait from the matched-category pool.
- **NAV_LOGO_UNSEEDED** (v17 FIX K): `_ensure_nav_logo_default_seed` wires seed's `nav_logo_url` to any bundled logo file.

## W. B+C pipeline-rewrite invariants (post-2026-04-18)

The B+C rewrite adds three layers (widget registry, BS4 contracts, DAG decorator).
Every MW build after 2026-04-18 must satisfy the invariants in this section
BEFORE any of A-I runtime tests run. Short-circuit reasoning: if artifact-level
invariants fail, no point booting the app.

See `docs/internals/WIDGETS_AND_CONTRACTS.md` for the architectural primer.

### W.1 Artifact structure

| # | Test | Priority |
|---|------|----------|
| W1.1 | `builds/medicine-waves-v<N>-*/` dir exists after pipeline completes | P0 |
| W1.2 | No files under `__widgets/` prefix on disk (the `__widgets/` filter fired) | P0 |
| W1.3 | File count sanity (50-80 files is expected range for canonical MW) | P2 |
| W1.4 | `public/images/mw_logo.png` bundled (FIX O; byte-identical to `cli/assets/mw_logo.png`) | P0 |
| W1.5 | `package.json` contains required dependencies (express, ejs, pg, bcrypt, express-session, connect-pg-simple, helmet, multer) | P0 |
| W1.6 | `server.js` parses (node --check) | P0 |
| W1.7 | Every .ejs file parses as valid EJS (pipeline's `_validate_ejs_templates` ran) | P0 |

### W.2 Widget emission

| # | Test | Priority |
|---|------|----------|
| W2.1 | `views/admin/settings.ejs` exists | P0 |
| W2.2 | For every writable data model: `views/admin/<slug>/new.ejs` + `edit.ejs` exist | P0 |
| W2.3 | For every read-only data model: new.ejs + edit.ejs do NOT exist | P0 |
| W2.4 | Expected marker count in artifact: 1 (settings) + 2×(writable_models) CRUD files | P0 |
| W2.5 | `widgets_emitted_at_generation` timestamp present in build trace metadata | P1 |
| W2.6 | No duplicate `views/admin-settings.ejs` alongside `views/admin/settings.ejs` (v13 regression) | P0 |

### W.3 Marker invariant

| # | Test | Priority |
|---|------|----------|
| W3.1 | Every widget-owned file starts with `<!-- widgets:admin-form -->` as first non-whitespace line | P0 |
| W3.2 | Marker appears EXACTLY ONCE per file (not duplicated by idempotent re-emit) | P0 |
| W3.3 | Total marker count in artifact: exactly as specified by W2.4 | P0 |
| W3.4 | Marker is importable via `cli.widgets.forms.WIDGET_ADMIN_MARKER` constant | P0 |

### W.4 Form-action canonicalization (the contract literal)

| # | Test | Priority |
|---|------|----------|
| W4.1 | settings.ejs: literal `<form action="/admin/settings" method="POST">` | P0 |
| W4.2 | new.ejs: literal `<form action="/admin/<slug>" method="POST">` | P0 |
| W4.3 | edit.ejs: literal `<form action="/admin/<slug>/<%= item.id %>" method="POST">` | P0 |
| W4.4 | No EJS ternary (`<%=  ? : %>`) in any widget-emitted form action attribute | P0 |
| W4.5 | CSRF input `<input type="hidden" name="_csrf" value="<%= csrfToken %>">` in every widget-emitted form | P0 |
| W4.6 | No `/admin/` stripped to `/` in widget-owned files (FIX C didn't fire) | P0 |
| W4.7 | No label with snake_case text in widget-owned files (labels humanized by LABEL_MAP) | P0 |

### W.5 Dashboard shape

| # | Test | Priority |
|---|------|----------|
| W5.1 | `views/admin/dashboard.ejs` has `<a href="/admin/settings">` CTA link | P0 |
| W5.2 | `views/admin/dashboard.ejs` does NOT have `<form action="/admin/settings">` | P0 |
| W5.3 | `<!-- widgets:dashboard-settings -->` markers bracket the CTA snippet | P1 |
| W5.4 | No stray `KEY | VALUE | ACTION` table header (v19 regression) | P1 |

### W.6 Dual-mount in server.js

| # | Test | Priority |
|---|------|----------|
| W6.1 | For every writable model: `app.use('/admin/<slug>', requireAdmin, <slug>Router)` + `app.use('/<slug>', <slug>Router)` both present | P0 |
| W6.2 | Read-only models mount only at `/<slug>` (no `/admin/<slug>` admin path) | P1 |
| W6.3 | `requireAdmin` is defined or imported in server.js (not just referenced) | P0 |
| W6.4 | No undefined router refs (`eventsRouter` etc. all imported) | P0 |

### W.7 Contract re-run on disk artifact (independent verification)

| # | Test | Priority |
|---|------|----------|
| W7.1 | Load the on-disk artifact files into an in-memory `Artifact` | P0 |
| W7.2 | `cli.contracts.run.run_all(artifact, collect_all=True)` raises zero violations | P0 |
| W7.3 | Per-contract pass: admin_settings, admin_crud (writable), admin_dashboard, layout, home, about, login | P0 |
| W7.4 | Contract wall-clock on v<N>-size artifact < 2.0 seconds (plan §6.7) | P1 |

### W.8 Pipeline-trace review

| # | Test | Priority |
|---|------|----------|
| W8.1 | `_emit_widgets` fired (build log) | P0 |
| W8.2 | `_ensure_admin_slug_mount` fired | P0 |
| W8.3 | `_inject_dashboard_settings_section` fired | P0 |
| W8.4 | `_fix_admin_crud_link_prefix` (FIX C) fired BUT rewrite count on widget-owned files = 0 | P0 |
| W8.5 | FIX B/L/M/N fired, produced no diffs on widget-emitted files | P0 |
| W8.6 | `_run_contracts` ran last (no build-failed state from contract violation) | P0 |
| W8.7 | `emit_build_trace` widgets.emit_all.start/.end events present in trace file | P1 |

### W.9 Reapply idempotency on fresh artifact

| # | Test | Priority |
|---|------|----------|
| W9.1 | Copy builds/medicine-waves-v<N>-*/ to tmp | P1 |
| W9.2 | Run `reapply_post_processors(tmp, spec)` once; captures files at T1 | P1 |
| W9.3 | Run `reapply_post_processors(tmp, spec)` again; captures files at T2 | P1 |
| W9.4 | T1 == T2 byte-identical across every file (sha256 map equality) | P0 |
| W9.5 | `_run_contracts` still green on reapply output | P0 |

### W.10 Prompt pruning evidence

| # | Test | Priority |
|---|------|----------|
| W10.1 | Build log shows `_build_explicit_prompt` emitted "Do NOT generate these files" directive | P1 |
| W10.2 | Total Claude token count on generation pass drops ≥10% vs recorded v19 baseline | P2 |
| W10.3 | Claude's raw output did NOT include `views/admin/settings.ejs` / `views/admin/<slug>/new.ejs` / `edit.ejs` (widget overwrote Claude emission) | P2 |

### W.11 Pipeline health (module-import-time)

| # | Test | Priority |
|---|------|----------|
| W11.1 | `cli.pipeline.assert_pipeline_parity()` succeeds (would have crashed worker on import if drift) | P0 |
| W11.2 | `set(EXPECTED_PROCESSORS) == set(REGISTRY.keys())` (guardrail) | P0 |
| W11.3 | `pipeline_order('build')[-1] == '_run_contracts'` + same for reapply | P0 |

### W.12 Seed + migration sanity

| # | Test | Priority |
|---|------|----------|
| W12.1 | db/seed.sql: every events row has non-NULL image_url (FIX A) | P0 |
| W12.2 | db/seed.sql: every photos row has non-NULL image_url (NOT NULL constraint) | P0 |
| W12.3 | Referenced image paths resolve to files under public/ | P1 |
| W12.4 | No `dQw4w9WgXcQ` rickroll placeholder IDs (replaced by FIX G / category pool) | P1 |
| W12.5 | db/seed.sql: seed admin password is bcrypt-hashed (not plaintext) | P0 |
| W12.6 | db/seed.sql has at least one ContactMessage row for the read-only admin list | P2 |
| W12.7 | db/schema.sql uses `TIMESTAMP WITH TIME ZONE` for datetime columns (v14 TZ fix) | P1 |

### W.13 Runtime boot (Node + DB)

| # | Test | Priority |
|---|------|----------|
| W13.1 | `npm install` succeeds | P0 |
| W13.2 | `npx tailwindcss -i src/... -o public/...` CSS build succeeds | P0 |
| W13.3 | Isolated local DB created for this build (never reuse another app's DB) | P0 |
| W13.4 | `node db/migrate.js` runs cleanly | P0 |
| W13.5 | `psql -f db/seed.sql` completes with no NOT NULL / FK violations | P0 |
| W13.6 | `PORT=4600 DATABASE_URL=... node server.js` boots without ReferenceError / crash | P0 |
| W13.7 | `curl http://localhost:4600/health` returns 200 OK | P0 |

### W.14 Browser walkthrough: public

| # | Test | Priority |
|---|------|----------|
| W14.1 | `/` renders, hero + events preview visible, no console errors | P0 |
| W14.2 | Home video section: click-to-play thumbnails only, no live `<iframe src*="youtube.com">` (§6 home contract) | P0 |
| W14.3 | `/events`: cards render with images + titles + timestamps in correct TZ (FIX event_time_helper) | P0 |
| W14.4 | `/events/:id`: ticket link iframe (e.g. a ticketing checkout embed) NOT rewritten to youtube-nocookie | P0 |
| W14.5 | `/about`: photo src is real, not 1×1 SVG (FIX J) | P0 |
| W14.6 | `/gallery`: photos render | P1 |
| W14.7 | `/videos`: thumbnails visible, click-to-play | P1 |
| W14.8 | `/contact`: contact form submits, creates ContactMessage row | P1 |
| W14.9 | Nav shows MW logo (not tiny SVG, not snake_case wordmark) (FIX K) | P0 |
| W14.10 | No U+2014 em-dash visible in brand nav text (§6 layout contract) | P0 |
| W14.11 | Every nav link resolves without 404 | P0 |

### W.15 Browser walkthrough: admin

| # | Test | Priority |
|---|------|----------|
| W15.1 | `/login` renders with email+password+CSRF input, NO demo creds in DOM (§6 login contract) | P0 |
| W15.2 | Log in as admin → redirect to `/admin/dashboard` | P0 |
| W15.3 | `/admin` without login → redirect to `/login` (auth middleware enforcement) | P0 |
| W15.4 | Admin dashboard shows CTA card linking to `/admin/settings` (NOT inline settings form) | P0 |
| W15.5 | Admin dashboard stat cards have accent variety (FIX I: colored numbers + thick border) | P1 |
| W15.6 | Click CTA → `/admin/settings` loads widget-emitted form | P0 |
| W15.7 | Settings form renders every spec field with English labels (LABEL_MAP humanization) | P0 |
| W15.8 | All image fields (hero, about_photo, nav_logo) are drag-drop zones, NOT URL text inputs | P0 |
| W15.9 | Submit settings change → POST `/admin/settings` → 200 → value persists on reload | P0 |
| W15.10 | Settings middleware populates `siteSettings.X` (not raw `settings.X`) consistently across templates (v14 fix) | P1 |
| W15.11 | For each writable model: admin list page renders with New + Edit + Delete buttons | P0 |
| W15.12 | New form submit → POST `/admin/<slug>` → 302 to list → new row visible | P0 |
| W15.13 | Edit form is populated with current values AND shows current image preview in upload zone (v14 fix) | P0 |
| W15.14 | Edit submit → POST `/admin/<slug>/<id>` → update succeeds | P0 |
| W15.15 | Delete → SINGLE confirm dialog, not double-bound (FIX D) | P0 |
| W15.16 | ContactMessage admin page: rows visible, NO New/Edit/Delete buttons (read-only) | P0 |
| W15.17 | Every admin button uses canonical .btn-* classes consistently (FIX E) | P1 |
| W15.18 | Destructive buttons (Delete) visually distinct from primary CTAs (FIX E) | P1 |

### W.16 Security + CSRF

| # | Test | Priority |
|---|------|----------|
| W16.1 | POST `/admin/settings` without `_csrf` → 403 (CSRF negative case) | P0 |
| W16.2 | POST `/admin/settings` with wrong `_csrf` → 403 | P0 |
| W16.3 | POST `/admin/settings` with valid `_csrf` → 200 | P0 |
| W16.4 | Session cookie set after successful login | P0 |
| W16.5 | Session persists across page loads (seed admin stays logged in) | P0 |
| W16.6 | Logout clears session, `/admin` redirects to `/login` | P0 |
| W16.7 | No hardcoded demo credentials visible in DOM or source | P0 |
| W16.8 | Upload endpoint POST accepts a file and returns a URL (drag-drop zone contract) | P0 |
| W16.9 | Spot-check one route handler for parameterized SQL (no string concat) | P1 |

### W.17 Cost + performance

| # | Test | Priority |
|---|------|----------|
| W17.1 | Total Claude cost recorded in `builds.total_api_cost_usd` | P1 |
| W17.2 | Build wall-clock recorded (`completed_at - started_at`) | P1 |
| W17.3 | Pipeline duration within 5% of v19 baseline (plan §9.2) | P2 |
| W17.4 | Number of QA-fix iterations logged | P2 |

### W.18 Self-skeptical attacks (offline, unit-test-driven)

| # | Test | Priority |
|---|------|----------|
| W18.1 | Inject snake_case label into settings.ejs → `admin_settings.check()` raises | P1 |
| W18.2 | Inject `<form action="/admin/settings">` into dashboard.ejs → `admin_dashboard.check()` raises | P1 |
| W18.3 | Inject live `<iframe src="*youtube.com*">` into home.ejs → `home.check()` raises | P1 |
| W18.4 | Inject `<label>hero_image_url</label>` snake_case into settings.ejs → contract raises | P1 |
| W18.5 | Inject U+2014 into brand nav → `layout.check()` raises | P1 |
| W18.6 | Inject demo credentials into login.ejs → `login.check()` raises | P1 |
| W18.7 | Inject 1×1 SVG into about photo src → `about.check()` raises | P1 |

### W.19 Build-terminal state

| # | Test | Priority |
|---|------|----------|
| W19.1 | Terminal status: `preview` (with RAILWAY_API_TOKEN unset, worker short-circuits deploy) | P0 |
| W19.2 | `review_status = 'pending'` (SE review gate in place) | P0 |
| W19.3 | If deploy attempted with RAILWAY_API_TOKEN: `status = 'failed'` with clear error message | P1 |

### W.20 Known-limitation acknowledgment

- Non-MW archetypes: widgets short-circuit via `_widgets_enabled_for(spec) == False`. §5 contracts still run on non-MW artifacts; admin-form contracts may fail on Claude-emitted non-MW output. This is phase-2 scope (see `docs/plans/b-plus-c/claude-plan.md`).
- Widgets-disabled path on v19 Claude fixture: known to produce partial contract violations (xfailed in `tests/test_widgets_safety_net.py`). Sunset (plan §10) depends on this path being green.
- FIX C + widget marker: resolved 2026-04-18 via `WIDGET_ADMIN_MARKER` coordination. FIX C still rewrites Claude-emitted admin list views that legitimately need the `/admin/` prefix stripped; that v13 regression guard stays.

---

## K. Sign-off

After all P0 tests pass + no NEW regressions found:
- [ ] Visual approval via browser
- [ ] Known followups documented + roadmap updated
- [ ] Failure KB updated
- [ ] Push + queue prod v?? (only after explicit operator go-ahead)
