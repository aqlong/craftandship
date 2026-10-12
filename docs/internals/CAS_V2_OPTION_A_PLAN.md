# CAS v2: Option A: Spec-Honoring Compose Engine

> Planning document. The generic public-form widget proposed as B3a (compose/widgets/forms/public-submission.ejs) was never created under that name; see `compose/widgets/` for the widgets that exist.

**Decision date:** 2026-04-22
**Owner:** Ocean + Claude

---

## Product stance

v2 honors the customer's spec. If the customer declares a page, a data model, a field, or a role, the compose engine must emit it. The archetype provides a visual/copy scaffold (colors, fonts, widget defaults, hero tagline) but does NOT override customer intent on structural elements.

Before this change, v2 behavior was documented in `compose/intake.py` as:
> "Lossy by design: v1's custom data_models + actions + user_roles are dropped in favor of v2's archetype-curated model set. v2 is content-first, not schema-first."

**Option A reverses this.** v2 is now both:
- **Content-first** for visual scaffolding (archetype-driven)
- **Schema-first** for structural elements (spec-driven)

Philosophical principle: the customer gets what they asked for, plus archetype-driven polish. The translator's job is to ENRICH, not TRUNCATE.

---

## Architecture: two-layer compose

### Layer 1: Archetype scaffolding (unchanged)

Archetype provides palette, fonts, widget defaults, hero tagline, site settings, and seed content for archetype-default models (testimonials, contact_inquiries, services, etc.).

### Layer 2: Spec-custom overlay (NEW)

The translator preserves customer-declared structural elements and emits them ON TOP of the archetype scaffold:

| Customer declared | Compose emits |
|---|---|
| `data_models: [{name: "Class", fields: [...]}]` | Table, schema, admin CRUD routes at `/admin/classes`, admin nav entry, seed row from archetype if available |
| `pages: [{path: "/book", auth_required: false}]` | Route in `routes/pages.js`, view in `views/pages/book.ejs`, nav entry |
| Model field of type `timestamp` | Proper Postgres type, admin date/time input, browser-local render in views |
| `belongs_to: "Class"` | FK column, admin UI dropdown, parent link in detail view |
| Public page whose `purpose` mentions form submission → matching data model | Public form widget (GET renders, POST saves) |
| `user_roles: [{role: "instructor"}]` | `users.role` enum value, `requireRole("instructor")` middleware, row-level filtering where applicable |

### Layer 3: Collision resolution

When customer model conflicts with archetype default (e.g., customer's `Class` vs archetype's `class_types`):
- **Prefer customer declaration.** Archetype default is dropped.
- Log a `translation_warnings` entry: "archetype default `class_types` replaced by customer-declared `Class`"
- SE reviewer sees this in the review UI

When customer's page path collides with archetype's (e.g., customer's `/schedule` overrides archetype's `/schedule`):
- **Merge.** Use archetype's layout + widgets, but respect customer's `title`, `auth_required`, `allowed_roles`
- Customer's `purpose` text can optionally override hero copy

---

## Common cases to support (all customer-declared intent)

These are the common patterns we must cover. Each one should have a compose-engine emitter + fixture spec + local e2e test.

### Content browsing patterns
- **Classes / Events / Sessions**: list page + detail page, filters (date, instructor), archetype widgets for list rendering
- **Services / Offerings**: list + detail (already archetype-covered; preserve customer field overrides)
- **Portfolio / Gallery**: image grid (archetype has this; preserve customer category declarations)
- **Team / Instructors / Staff**: people list + bio detail (archetype has variants; preserve customer role declarations)
- **Blog / Journal / News**: post list + detail + archive by date (currently spotty; needs full emitter)
- **FAQ**: question list (archetype has this)
- **Pricing**: plan cards (archetype has this)
- **Testimonials / Reviews**: list with rating (archetype has this)

### Public submission patterns (biggest gap right now)
- **Contact form**: name + email + message → writes to ContactMessage/ContactInquiry. Archetype covers this but must honor customer fields.
- **Booking / RSVP / Reservation form**; name + email + optional notes → writes to Booking belongs_to Class/Event/Session. **Not currently supported.**
- **Event registration**: name + email + party_size → writes to Registration. **Not supported.**
- **Inquiry / Quote request form**: name + email + service_interest + message → writes to Inquiry. Archetype partial.
- **Newsletter signup**: email only → writes to NewsletterSubscriber. Archetype covers.
- **Review submission**: name + rating + text → writes to Review. Need to decide if auth-gated.

### Admin patterns
- **Per-customer-model CRUD**: every customer data_model gets `/admin/<slug>` list + new + edit + detail + delete. Already partially works via spec.data_models flow; needs nav integration.
- **Bulk ops**, not in scope for Option A
- **Admin dashboard**: include counts for ALL models (archetype + custom) not just archetype ones

### Auth + access patterns
- **Magic-link login**: archetype covers this (v2-verify-magic-link-auth-probe-failure is a separate bug)
- **Email verification**: already in schema
- **Admin-only pages**: `auth_required: true` + `allowed_roles: ["admin"]` → `requireAdmin` middleware
- **Multi-role access**: `allowed_roles: ["admin", "instructor"]` → `requireAnyRole(...)`

### Data field patterns
- **All primitive types**: string, text, integer, boolean, email, phone, url, timestamp, date, time
- **Images**: string type with upload widget (archetype has this for hero/about/logo; needs generalization)
- **Rich text**: text field + markdown rendering in views
- **Enum**: dropdown on admin form, badge in list
- **belongs_to**: FK + dropdown + parent-link in detail
- **has_many**: count in parent detail + link to filtered child list

---

## Full bug + feature list (unified from the review findings + Option A scope)

Each item gets: **ID**, **description**, **probable solution**, **testability** (🖥️ local / ☁️ prod / 🧩 both), **priority**, **depends on**.

### Group 0: Related cleanup

| ID | Summary | Probable solution | Test | Priority |
|---|---|---|---|---|
| B0a | `wait_for_deployment` raises on single CRASHED (cli/deploy.py:653) | Only raise after 2 consecutive CRASHED polls with no SUCCESS in between; shortens need for worker recovery probe | 🖥️ Mock `gql` to return CRASHED→SUCCESS, assert returns | HIGH |
| B0b | `verify_auth` + `verify_data` run unnecessarily on v2 (always non-blocking warnings) | Gate both behind `_engine == "v1"` in `queue/worker_pg.py` | 🖥️ Source-string guard; 🖥️ unit (mock engine + assert call not made) | MEDIUM |
| B0c | `verify_magic_link_auth` probe fails on v2 builds end-to-end | Root-cause: hash function mismatch between `cli/deploy.py` token storage and `compose/build.py` `/magic` route; unify | 🧩 Local: run compose + hit /magic?token=<raw> manually; 🧩 Prod: next v2 build should show "Login link verified" | MEDIUM |
| B0d | Admin builds UI doesn't show engine (v1 vs v2) per build | Pull `CAS_BUILD_ENGINE` (effective at claim time) into builds row; render badge in `/admin/builds` + detail | 🖥️ Vitest on admin route + EJS | MEDIUM |
| B0e | Archetype keyword expansion (stretch, yoga, pilates, etc.) | Add missing keywords to `_ARCHETYPE_KEYWORDS["fitness-studio"]` in `compose/intake.py:428` | 🖥️ `TestArchetypeClassification` parametric fixtures (depends on B1b) | HIGH |

### Group 1: Foundation (BLOCKERS for all Option A work)

| ID | Summary | Probable solution | Test | Priority | Depends on |
|---|---|---|---|---|---|
| **B1a** | **No local v2 e2e integration test harness.** Can't iterate on compose changes without burning Railway projects. | Add `tests/test_v2_local_e2e.py` (marked `@pytest.mark.v2_integration`): for each `compose/fixtures/*.json`: (1) `compose.build_app` to tmp, (2) createdb isolated DB, (3) `node db_migrate.js`, (4) `node server.js` on free port, (5) curl /health + all archetype routes + customer-declared routes, (6) assert HTTP status expectations | 🖥️ This IS the test harness | HIGH | - |
| B1b | Archetype classification regression fixtures | `TestArchetypeClassification` parametric; ~30 `(description, expected_archetype)` cases covering each of 12 archetypes | 🖥️ Pure unit | HIGH | - |

### Group 2: Core Option A (translator preserves customer intent)

| ID | Summary | Probable solution | Test | Priority | Depends on |
|---|---|---|---|---|---|
| B2a | `translate_v1_spec` drops `v1.data_models` | In `compose/intake.py:544`, pass `v1.data_models` through into `answers["data_models"]`; `scaffold_spec` merges with archetype defaults | 🖥️ unit: assert translated spec has customer models; 🖥️ e2e: build + assert emitted schema has customer tables | HIGH | B1a |
| B2b | `translate_v1_spec` drops `v1.pages` (uses only description) | Pass `v1.pages` through; `scaffold_spec` emits custom-page layouts for paths not in archetype defaults | 🖥️ unit: translated spec includes custom pages; 🖥️ e2e: build + curl custom path returns 200 | HIGH | B1a |
| B2c | `translate_v1_spec` drops `v1.user_roles` | Emit customer roles into `users.role` enum + `requireRole` middleware | 🖥️ unit; 🖥️ e2e: login as each role + check access | HIGH | B1a, B2a |
| B2d | Archetype/customer model collision (e.g., customer `Class` vs archetype `class_types`) | Prefer customer declaration; log `translation_warnings` entry; skip the archetype model | 🖥️ unit: hand-craft colliding spec + assert custom wins; 🖥️ e2e: build passes lint | HIGH | B2a |
| B2e | Customer page titles overridden by archetype defaults | Use `spec.pages[path].title` when available; fall back to archetype title | 🖥️ unit: build with customer title + assert title in emitted view; 🖥️ e2e: curl + check `<title>` | MEDIUM | B2b |

### Group 3: Public submission forms

| ID | Summary | Probable solution | Test | Priority | Depends on |
|---|---|---|---|---|---|
| B3a | No generic public-form widget | Create compose/widgets/forms/public-submission.ejs; renders a form bound to a customer data_model; fields auto-derived from model; CSRF; client-side validation | 🖥️ unit: widget emits correct form markup; 🖥️ e2e: build + curl GET returns form | HIGH | B1a, B2a, B2b |
| B3b | No public POST handler emitter for spec pages | Extend `emit_routes_pages_js` to emit `GET + POST` pair when a spec page has `form_target: "ModelName"` hint; POST validates + inserts + redirects with flash | 🖥️ unit: emitter test; 🖥️ e2e: POST with valid body → row in DB | HIGH | B3a |
| B3c | CSRF + success flash for public forms | Standard Express middleware + session flash; widget renders `<%= flash.success %>` zone | 🖥️ e2e: submit form + verify success page | MEDIUM | B3a, B3b |
| B3d | Admin email notification on public submission (optional) | `/admin/inquiries` counter; optional email send on create (behind `NOTIFY_ADMIN_ON_SUBMIT` per-spec flag); don't fire on test builds (`SEND_BUILD_PROGRESS_EMAILS=false`) | 🧩 local unit for route; ☁️ prod to test email delivery | LOW | B3b |

### Group 4: Admin CRUD for customer models

| ID | Summary | Probable solution | Test | Priority | Depends on |
|---|---|---|---|---|---|
| B4a | Admin CRUD for customer models not appearing in admin nav | `emit_admin_nav` enumerates `rs.data_models` (not just archetype's); render admin nav group for custom | 🖥️ unit: assert nav has customer model; 🖥️ e2e: curl /admin after login + check nav | HIGH | B2a |
| B4b | `belongs_to` FK not emitted or rendered in admin UI | Compose: schema emits `parent_id INTEGER REFERENCES parent(id)`; admin new/edit form renders parent `<select>` populated from parent rows | 🖥️ unit: schema.sql has FK; 🖥️ e2e: create child via admin form + assert FK populated | HIGH | B2a |
| B4c | Public list view for customer model (browse page) | When spec has a page whose path slug matches a customer model, emit list view using `list-archetype` widget wired to the model | 🖥️ unit + e2e | MEDIUM | B2a, B2b |
| B4d | Detail view for customer model (/<slug>/:id or /<slug>/<id-field>) | Emit `GET /<slug>/:id` → detail view | 🖥️ e2e: create row + curl detail URL | MEDIUM | B4c |

### Group 5: Polish

| ID | Summary | Probable solution | Test | Priority |
|---|---|---|---|---|
| B5a | Customer description not in hero copy | Use `v1.description` as `hero_description` override (not archetype default) | 🖥️ unit | MEDIUM |
| B5b | Nav bar shows only archetype pages, not customer's non-archetype pages | `compose/build.py` nav emitter reads `spec.pages` for customer-declared paths + archetype default nav | 🖥️ e2e: curl home + parse nav | MEDIUM |
| B5c | No `translation_warnings` surfaced to SE reviewer | Translator appends to `spec._translation_warnings` list; worker logs each to `build_logs`; `/admin/reviews` UI shows warnings panel | 🖥️ unit + 🧩 e2e in admin view | MEDIUM |
| B5d | No "engine = v2" indicator in customer-facing /builds/:id status page | EJS branches on `build.spec.archetype` presence → v2 stage labels + "built with curated templates" copy | 🖥️ Vitest | LOW |

### Group 6: Common-case coverage tests (Option A validates by building these)

| ID | Planned fixture spec (not all exist in the repo) | Models | Pages | Priority |
|---|---|---|---|---|
| B6a | `sample-yoga-studio-booking-spec.json` | Class, Booking, Instructor | `/` `/schedule` `/book` `/instructors` `/contact` `/admin` `/login` | HIGH |
| B6b | `sample-event-producer-spec.json` | Event, Registration, Venue | `/` `/events` `/events/:id` `/register` `/contact` `/admin` | HIGH |
| B6c | `sample-massage-therapist-booking-spec.json` | Service, Appointment, Testimonial | `/` `/services` `/book` `/testimonials` `/contact` `/admin` | MEDIUM |
| B6d | `sample-sound-bath-facilitator-spec.json` | Session, Booking, ContactMessage | `/` `/sessions` `/book` `/contact` `/admin` `/login` | HIGH |
| B6e | `sample-restaurant-reservation-spec.json` | MenuItem, Reservation, ContactMessage | `/` `/menu` `/reserve` `/contact` `/admin` | MEDIUM |

Each fixture, once B1a lands, runs through the local e2e harness and must reach HTTP 200 on all declared routes.

---

## Testing philosophy

1. **Local first, always.** B1a is the first task because without it everything else is expensive-to-iterate.
2. **Source-string guards are permissible for gates.** Follows existing `TestV2DeployFixSkip` pattern. Use for "is this branch present in the source?" checks.
3. **Behavioral tests for data flow.** Use for translator-preserves-fields, widget-renders-model, etc.
4. **Integration tests via the local e2e harness.** Use for "does the emitted app actually serve what the spec asked for?"
5. **Prod tests only for Railway-specific behavior** (edge propagation, DB provisioning, magic-link email delivery), not for compose output correctness.

---

## Phase plan

Rather than doing 30 tasks in parallel, here's the sequenced dependency graph:

### Phase 0: Immediate cleanup (can ship independently)
- B0e keyword expansion
- B0b skip verify_auth/verify_data on v2

### Phase 1: Foundation (blocks all Option A work)
- **B1a local v2 e2e test harness** ← **START HERE**
- B1b archetype classification regression

### Phase 2: Translator preservation (Option A core)
- B2a data_models
- B2b pages
- B2c user_roles
- B2d collision resolution
- B2e title preservation

### Phase 3: Public forms (biggest product gap)
- B3a public-submission widget
- B3b GET+POST emitter
- B3c CSRF + flash

### Phase 4: Admin CRUD for customer models
- B4a nav integration
- B4b belongs_to FK
- B4c public list view
- B4d detail view

### Phase 5: Polish + warnings
- B5a hero description
- B5b nav customer pages
- B5c translation warnings
- B3d admin email notification

### Phase 6: Upstream & infra fixes (parallel w/ Phase 2-5)
- B0a wait_for_deployment transient
- B0c verify_magic_link_auth root cause
- B0d admin builds engine badge

### Phase 7: Common-case fixture coverage
- B6a-e fixture specs; each runs through local e2e harness

---

## First task: B1a, Local v2 e2e test harness

Before anything else, we need to be able to run:

```bash
pytest tests/test_v2_local_e2e.py -m v2_integration -v
```

…and have it compose each fixture, spin up a local Postgres instance, migrate + seed, boot `node server.js` on a free port, curl every declared route, and assert expected HTTP status codes.

This is a self-contained scoped task that doesn't require any v2 compose engine changes. Once it lands, we can iterate on the rest confidently.

### Acceptance criteria for B1a

1. New file `tests/test_v2_local_e2e.py` with `@pytest.mark.v2_integration` marker
2. `conftest.py` adds the marker definition
3. Test creates + tears down an isolated local Postgres DB per run (`cas_v2_e2e_<random>`)
4. For each fixture in `compose/fixtures/sample-*-spec.json`:
   - Composes to a tmp dir
   - Runs `npm install --silent` once per test session (cache with pytest fixture)
   - Runs `node db_migrate.js` with injected `DATABASE_URL`
   - Boots `node server.js` in background on a free port
   - Waits for `/health` 200 (retry with 30s budget)
   - Curls every route defined in `spec.pages` + every archetype default route + every admin CRUD path
   - Asserts expected HTTP status (200 for public, 302 for admin when unauthed)
   - Kills server + drops DB
5. Skipped by default (`@pytest.mark.v2_integration` is off in CI unless explicit); runs via `pytest -m v2_integration`
6. Passes for all existing compose/fixtures

### Testability
🖥️ Local: no prod needed. Ships as test file; no runtime code changes.

### Dependencies
None.

### Rough effort
3-5 hours (Postgres lifecycle + Node subprocess + fixture iteration).

