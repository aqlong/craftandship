# v2 compose-archetype generated-app accessibility static scan: 2026-05-27

> **What this is:** a static review of the markup. A script scans the EJS and the palette values of the emitted sites for a short rule set; no browser, screen reader or other assistive technology was used, and the live axe and Playwright pass described below never ran.

**Parent roadmap item:** `harbor-usability-4-generated-app-a11y-sweep` (Sub-4 of `harbor-usability-live-browser-followups`).

> **Status at public export:** all three static findings (Groups A and B) are closed. The live axe + Playwright pass over generated archetype apps was never run; it remains an **open** follow-up.

**Scope:** every v2 compose archetype emitted with **zero customer overrides**, the archetype-default site a brand-new keeper would receive if Haven dispatched immediately after intake. 9 archetypes covered (`healer-wellness`, `coach-consultant`, `local-service`, `therapist-counselor`, `fitness-studio`, `salon-spa`, `restaurant-cafe`, `tradesperson-contractor`, `event-producer`). The `author` archetype is out of scope until the Harbor flow lights it up.

**Method:** static EJS + palette scan (no DB, no browser, no server) via `scripts/generated_app_a11y_scan.py`.

1. Build all 9 archetypes into a tmpdir with `compose.intake.scaffold_spec` + `compose.build.build_app`, no customer overrides.
2. For each archetype, walk `home.ejs` + `about.ejs` + `contact.ejs` + `layout-public.ejs` plus their **transitive widget includes** (the page files are include-only shells; the actual a11y surface is in the partials they pull in).
3. Run the rule set below against the stripped (EJS-tag-free) text.
4. Compute palette contrast for every entry in `compose/data/palettes.json` - `--color-foreground` on `--color-background` and `--color-foreground-muted` on `--color-background` against the WCAG AA-normal floor of 4.5:1.

**Rule set (deps-free static checks):**

- `missing-alt-on-img` - `<img>` with no `alt=` attribute.
- `input-missing-label` - `<input>` (non-hidden/submit) with no `aria-label`, `aria-labelledby`, enclosing `<label>`, or `label[for=id]`.
- `button-empty-content` - `<button>` with empty body and no `aria-label`.
- `link-generic-text`: anchor body is "click here" / "read more" / "learn more" / "here" / "more" / "details".
- `heading-skip-level` - `h{n}` after `h{m}` with `n > m + 1` (within a page file; widgets are not flagged individually because the composing page controls the leading level).
- `missing-html-lang` - `<html>` without `lang=`.
- `missing-main-landmark` - `layout-public.ejs` without a `<main>` element.
- `missing-skip-link` - `layout-public.ejs` without the `skip-to-content` include.
- `palette-foreground-fails-aa` / `palette-foreground-muted-fails-aa`: palette `--color-foreground` (or muted) on `--color-background` below 4.5:1.

**Live pass:** axe-core + Playwright sweep against running generated apps is intentionally NOT in this pass, see "Live pass, prerequisites" below. The parent item was decomposed precisely because the live run needs 9 Postgres DBs, 9 Express servers, and a headless browser the executor cannot stand up.

## Static pass: results

Run via `python3 scripts/generated_app_a11y_scan.py` (deterministic: re-run anytime to refresh).

### View-rule incidence

| Rule | Archetypes hit | Source file | Notes |
|---|---|---|---|
| `input-missing-label` | 0/9 (was 4/9: healer-wellness, fitness-studio, restaurant-cafe, event-producer) | `compose/widgets/cta/newsletter-inline.ejs` | **CLOSED 2026-05-28 via `v2-a11y-newsletter-widget-aria-label`.** Newsletter `<input type="email">` now carries `aria-label="Email address"` plus a sibling `<label for="newsletter-inline-email" class="sr-only">Email address</label>`. Static scanner confirms 0/9 archetypes after fix. Future visible-label promotion is a single-class swap (`sr-only` → visible utility). |
| All other view rules | 0/9 | - | `missing-alt-on-img`, `button-empty-content`, `link-generic-text`, `heading-skip-level`, `missing-html-lang`, `missing-main-landmark`, `missing-skip-link` all clear across the 9 archetype defaults. The compose engine emits `alt=`, named buttons, descriptive link copy, sequential headings, `<html lang="en">`, `<main id="main-content">`, and the `skip-to-content` widget out of the box. |

### Palette contrast

| Palette | Pair | FG | BG | Ratio | Target | Status |
|---|---|---|---|---|---|---|
| `sanctuary` | foreground-muted on background | `#5a665f` (was `#6e7d76`) | `#efe8dc` | 4.93:1 (was 3.55:1) | AA-normal (4.5:1) | **PASS (closed 2026-05-28)** |
| `stone-neutral` | foreground-muted on background | `#6a6a6a` (was `#7a7a78`) | `#f8f8f6` | 5.09:1 (was 4.04:1) | AA-normal (4.5:1) | **PASS (closed 2026-05-28)** |
| All other palettes | foreground / foreground-muted on background | - | - | ≥4.5:1 | AA-normal | PASS |

Every palette's primary `--color-foreground` on `--color-background` passes AA-normal. After the 2026-05-28 sanctuary + stone-neutral darkenings, every palette's `--color-foreground-muted` also clears the 4.5:1 floor that body / secondary copy needs.

## Findings: grouped

### Group A, newsletter widget input missing label (cross-archetype, compose-engine fix), **CLOSED 2026-05-28**

Single root cause was in `compose/widgets/cta/newsletter-inline.ejs`. The `<input type="email" name="email" required placeholder="…">` lacked any accessible name: no `<label>`, no `aria-label`, no `aria-labelledby`. Screen-reader users heard only the field type (`edit email`) with no semantic context. Placeholder copy is also stripped by some assistive tech once the user starts typing, so it could not stand in for a label even when present.

**Fix shipped (roadmap item `v2-a11y-newsletter-widget-aria-label`):** added `aria-label="Email address"` to the input AND a sibling `<label for="newsletter-inline-email" class="sr-only">Email address</label>`. Belt-and-suspenders: aria-label covers the immediate a11y gap; the sibling sr-only label lets a future visible-label promotion be a CSS-class swap (remove `sr-only`) without further widget edits. Static scanner confirms `input-missing-label` at 0/9 archetypes post-fix.

### Group B, `sanctuary` + `stone-neutral` foreground-muted (cross-palette, palette-config fix), **CLOSED 2026-05-28**

Two palettes' `--color-foreground-muted` token failed AA-normal on its own `--color-background`. The muted token is used pervasively across compose widgets for secondary copy, helper text, byline meta, and price labels; any of those usages on these two palettes drops below the 4.5:1 floor.

- `sanctuary` `#6e7d76` → `#5a665f` on `#efe8dc` → 3.55:1 → **4.93:1 (closed 2026-05-28 via `v2-a11y-palette-sanctuary-muted-darken`).** The new token preserves the sage undertone (g > r and g > b in RGB) so the palette's "deep-teal-and-sage editorial" mood is unchanged.
- `stone-neutral` `#7a7a78` → `#6a6a6a` on `#f8f8f6` → 4.04:1 → **5.09:1 (closed 2026-05-28 via `v2-a11y-palette-stone-neutral-muted-darken`).** The new token is a neutral grey (r = g = b) so the palette's near-white minimalist mood is unchanged.

Both fixes are token-only edits in `compose/data/palettes.json`. No widget changes needed; the entire compose surface inherits the new token value.

### Group C: no archetype-specific findings

No rule fires for a single archetype only in this pass. Every view-level finding either hits ≥4 archetypes (Group A) or zero. The compose engine's archetype-default surface is consistent across archetypes for the rules covered here.

## Roadmap entries spawned

Per the parent item's contract: each cross-archetype finding becomes a compose-engine roadmap entry (high priority), each archetype-specific finding becomes an archetype-config entry. Group C confirmed zero archetype-specific findings.

| Roadmap id | Group | Priority | One-line fix |
|---|---|---|---|
| `v2-a11y-newsletter-widget-aria-label` | A | high | **DONE 2026-05-28.** Added `aria-label="Email address"` to the email input in `compose/widgets/cta/newsletter-inline.ejs` plus a sibling `<label for="newsletter-inline-email" class="sr-only">Email address</label>`. Scanner confirms 0/9 archetypes. |
| `v2-a11y-palette-sanctuary-muted-darken` | B | medium | **DONE 2026-05-28.** Darkened `sanctuary --color-foreground-muted` from `#6e7d76` to `#5a665f` (4.93:1 on `#efe8dc`). Scanner confirms `sanctuary` is no longer in the palette failures list. |
| `v2-a11y-palette-stone-neutral-muted-darken` | B | medium | **DONE 2026-05-28.** Darkened `stone-neutral --color-foreground-muted` from `#7a7a78` to `#6a6a6a` (5.09:1 on `#f8f8f6`). Scanner confirms `stone-neutral` is no longer in the palette failures list. |

## Regression contract

`tests/test_generated_app_a11y_scan.py` pins the static-scan logic AND the known-bad pairings. As of 2026-05-28, all three findings (newsletter input-missing-label, `sanctuary` palette-muted, `stone-neutral` palette-muted) are expected-passing. A future palette lightening or widget edit that re-introduces one of these will fail the test with a diagnostic that names the pairing and points back at this document; the fix-the-doc-too contract.

## Live pass: prerequisites

A Playwright + `@axe-core/playwright` sweep across the 9 generated archetype apps would catch:

- Dynamic ARIA-live behavior (toast/flash messages on form submit).
- Hover / focus state contrast.
- Composited background (gradients, paper-grain textures) that the static palette check cannot reason about.
- Tap-target size at the rendered viewport.
- Color-contrast regressions inside the rendered DOM (e.g. Tailwind utility class drift).

It requires:

1. A Postgres role with `CREATEDB` (one fresh DB per archetype build).
2. The composed app's own schema applied per app (compose emits `db/schema.sql`; the `migrate.sql` name used in the original draft of this audit does not exist).
3. `node server.js` running on a unique port per archetype (9 ports).
4. `npx playwright install chromium`.
5. `@axe-core/playwright` and `axe-core` (they were held back when this was written and are now devDependencies in `web/package.json`, used by `web/tests/ui/lighthouse-a11y.spec.ts`).
6. A driver spec (proposed in the original draft; it was never written, so no such file exists), that for each of the 9 archetypes navigates `/`, `/about`, `/contact`, plus one archetype-flagship route (e.g. `/schedule` for fitness-studio, `/menu` for restaurant-cafe, `/services` for tradesperson-contractor), injects axe, and writes raw violations to `docs/audits/generated-app-archetype-a11y-<date>-<archetype>.json`.

When run, append a "Live pass; results" section to this document mirroring the static table. The roadmap item `v2-a11y-live-browser-axe-sweep` tracks this follow-up.

## Lighthouse score

Google Lighthouse a11y score is NOT captured in this pass; it requires the same running-server prerequisites as the live axe sweep, plus headless Chrome. The static pass covers the contrast + structural surface the parent roadmap item flagged. Lighthouse-score-only follow-up is tracked under `v2-a11y-live-browser-axe-sweep` alongside the axe run (one server stand-up covers both).
