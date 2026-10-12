# `?service=<name>` Contact Form Prefill Pattern

**Scope:** v2 compose engine. Every customer build that emits a contact
page (`form-contact` widget) and one or more offering widgets
(`offerings-grid-3col`, `offerings-services-editorial-table`).

**Why it exists:** when a keeper clicks a specific offering CTA, "Book"
on "Reiki Session," "Inquire" on "Team Intensive"; they expect to land
on the contact form with that service already selected. Without the
prefill, every click drops them on a generic contact form and they have
to pick the service themselves. Conversion friction.

**Case zero:** first wired up for a wellness-practitioner build in May 2026.

## The four moving pieces

The pattern is a four-step contract. All four must be in place for the
prefill to work end-to-end. Any one missing and the URL preselect is a
no-op.

### 1. Offering-card CTAs emit `/contact?service=<name>`

Source-of-truth file: `compose/build.py:_normalize_offering_cta_hrefs`.

Called from `emit_page_view` on every widget data block. For any item
shaped like a service card (`{title: "...", cta_href: "/contact", ...}`),
the bare `/contact` href is rewritten to `/contact?service=<urlencoded
title>` (capped at 80 chars). Items that already carry a query string,
external links, `tel:`, `mailto:`, etc. are left alone.

Harbor-side extraction (the path that turns a keeper's Haven
conversation into a compose spec) emits the query string directly via
`compose/intake.py:_normalize_harbor_services` and
`_services_from_offering_seed_rows`, so harbor-driven builds skip the
`/contact` → `/contact?service=` rewrite on the way through. The
build-time rewrite is the safety net for spec-authored fixtures and
archetype `_default_services` that historically used flat `/contact`.

### 2. The `/contact` route reads `req.query.service`

Source-of-truth file: `compose/build.py:emit_routes_pages_js` (look for
`if page_id == "contact"`).

Emits a handler that pulls `req.query.service` into a `preselected_service`
local and passes it to the view via `res.render('contact', {
preselected_service, ... })`. The POST `/contact` handler (form submit)
also re-renders with `preselected_service: service_interest || ''` so a
failed-validation redraw keeps the selection.

The companion `emit_page_view` step injects a dynamic JS expression
into the `form-contact` include call so the widget reads
`locals.preselected_service` at render time, not a build-time literal.

### 3. The form has `<select name="service_interest">` with the matching `<option>`

Source-of-truth file: `compose/widgets/form/contact.ejs`.

When the widget receives `service_options.length > 0`, it includes the
`form/field.ejs` partial with `field_type: 'select'` and `value: _preselect`
where `_preselect = submitted.service_interest || preselected_service || ''`.

`field.ejs` walks `options` and stamps `selected` on the `<option>` whose
`value` equals `_preselect`. Server-side render: no client JS, no
hydration race. The option also acts as the data carrier; the value
attribute carries the service name verbatim.

### 4. `service_options` stays in sync with the offerings list

Source-of-truth file: `compose/build.py:_page_widget_defaults` (the
`form-contact` branch) + `_harvest_service_options_from_offerings`.

Two sources are checked in order:

1. `settings.service_options` (if a keeper or admin explicitly overrides
   the list site-wide).
2. Harvested titles from every `spec.pages.<page>.<offerings|services|items>.items[]`
   block. First-seen wins, case-insensitive dedupe, capped at 24
   entries, suffixed with "General Inquiry."

Spec-supplied `spec.contact.form.service_options` (e.g. from
`scaffold_spec` in `compose/intake.py`, which seeds the list from the
generated `services` array) overrides both defaults via the standard
`emit_page_view` merge (`widget_data = {**defaults, **spec_data}`).

The harvest step is the **standardization guarantee**: if a build emits
`offerings-grid-3col` or `offerings-services-editorial-table` and the
items have `title` fields, the contact form is guaranteed to render
the matching `<select>` so the URL preselect lands somewhere. No silent
no-ops.

## Tests that enforce this contract

- `test_contact_page_handler_forwards_service_query`: route handler
  reads `req.query.service`.
- `test_contact_view_passes_preselected_service_to_widget`: page view
  emits the dynamic injection.
- `test_form_contact_widget_honors_preselect`: widget marks the
  matching option `selected`.
- `test_work_with_me_ctas_link_to_contact_with_service`: sample spec
  offerings emit `/contact?service=` hrefs.
- `test_service_cards_pass_service_to_contact_form_across_archetypes`
  - every archetype fixture's emitted page views have zero flat
  `/contact` cta_hrefs on service-card-shaped items.
- `test_form_contact_service_options_harvested_from_offerings` -
  `_harvest_service_options_from_offerings` returns offering titles,
  deduped + capped + "General Inquiry"-suffixed.
- `test_form_contact_widget_renders_select_when_offerings_present` -
  end-to-end: a spec with offering items but no explicit
  `settings.service_options` and no `spec.contact.form.service_options`
  still produces a `service_interest` `<select>` with all offering
  titles as options.

## When to extend

If a new offering-style widget id is added, the `_normalize_offering_cta_hrefs`
heuristic (`item is dict AND has title AND has cta_href: "/contact"`)
catches it automatically. No registry change needed.

If a new offering data key is introduced (something other than
`offerings`, `services`, `items`), update the key list in
`_harvest_service_options_from_offerings` so the harvest covers it.

If a downstream feature needs to read the preselect on the client
(analytics, A/B selectors, etc.), do NOT re-derive from
`window.location.search`: read the rendered `<option selected>` from
the DOM. The server is authoritative.
