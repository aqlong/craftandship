# CAS v2: Template Families Plan

**Created 2026-04-28** after the layout-variants work shipped Phases 0 to 3.

> Planning document, not updated afterwards. Part of it shipped: `compose/data/templates.json` holds the template families and `/lighthouse/preview` is the customer-facing picker (see `docs/internals/design-decisions.md`). Read the phase list below as a plan, not as a record of what exists.

## Why this exists

The 2026-04-28 layout-variants work shipped page-order rearrangement and one new hero widget. Review feedback was clear: "layout" meant different website *design layouts* (different styles of how a website should look), not just data placed in different parts of the page. All built sites looked too much alike, with the menus and headers looking and working the same.

The unfixed reality: every CAS-built site uses `compose/widgets/shell/nav-public.ejs` + `compose/widgets/shell/footer.ejs` + `views/layout-public.ejs`. Page contents vary by archetype + (newly) by sibling layout variant; the *shell*, *typography scale*, and *page rhythm* do not vary at all. A barber shop and a multi-location HVAC business get the same nav bar, the same footer, the same baseline font sizes, the same section spacing.

This plan establishes the next layer up: **template families** that bundle shell + nav + footer + typography scale + per-page layout overrides into a coherent design system, with several distinct families a customer can land in based on their archetype, their stated complexity, and Haven-extracted preferences.

## Non-goals

- Replacing the v2 deterministic-compose contract. Templates are still pure-data + static EJS, no AI in the build path.
- Replacing palette/font-pair rotation, page-layout sibling variants, or the harbor_meta hint pipeline shipped 2026-04-28. Template families wrap those, they don't replace them.
- Customer-authored CSS or arbitrary component injection. Curated families only.
- Custom domain rendering or multi-page builders.

## Goals

1. **Visible variety.** Two CAS-built healer sites for two different keepers should look like distinctly different websites, not the same template with two color palettes. Same for the other archetypes.
2. **Complexity tiering.** A solo practitioner with two services should be able to land in a "simple statement" template, large text, single CTA, minimal nav, very few sections per page. A multi-location service business should land in a "directory rich" template, persistent side nav, dense card grids, more page sections.
3. **Deterministic.** Same `_harbor_meta` + same archetype + same `app_name` -> same template family, every rebuild. Pure function. No AI in build path.
4. **Reusable.** Template family choice flows through resolve_spec to all downstream emitters (shell, page layouts, CSS generator, animation set).
5. **Extensible.** Adding a new template family is a data edit (`templates.json`) + a few new EJS partials, not a code change.

## Initial template families (5)

| Family | Audience signal | Shell nav | Footer | Type scale (h1) | Page rhythm | Accent density |
|---|---|---|---|---|---|---|
| `classic-business` | Default. Multi-service business with standard pages. | `nav-public-bar` (current) | `footer-three-col` (current) | `2.5–3.5rem` | balanced 4-section pages | medium |
| `editorial-statement` | Author/coach/healer who values typography. Hint matches: `airy`, `editorial`, `quiet`, `literary`. | `nav-thin-statement` (logo left, single right CTA) | `footer-minimal-line` (single horizontal rule + small column) | `4–6rem` | very airy, 2-3 large sections per page | one accent only |
| `directory-rich` | Multi-location, multi-service, information-dense business. Hint matches: `practical`, `local`, `dense`, `structured`. | `nav-side-rail` (persistent left nav on lg+, hamburger sm) | `footer-grid-five-col` (full sitemap) | `1.875–2.5rem` | dense 5-7 section pages with card grids | high (multiple accents) |
| `simple-statement` | Solo practitioner, one service, one CTA. Hint matches: `simple`, `minimal`, complexity score low. | `nav-centered-minimal` (3 links centered, no logo wordmark) | `footer-single-line` (one centered line, copyright + contact) | `3.5–5rem` | one-page focus; home contains everything | none beyond primary |
| `event-led` | Event producer or class-based business where dates/location are the offer. Hint matches: archetype is event-producer OR `event`, `class`, `gathering`, `calendar`. | `nav-image-banner` (full-width image strip with nav overlaid) | `footer-three-col` | `3–4.5rem` | image-led hero takes 80vh; sections beneath flow chronologically | medium |

Each family is keyed to one or more **compatible archetypes**. The picker can never assign `editorial-statement` to `tradesperson-contractor`, for example, because the structural fit is wrong.

## Schema additions

### `compose/data/templates.json` (new)

```jsonc
{
  "version": 1,
  "updated_at": "2026-04-28",
  "templates": [
    {
      "id": "classic-business",
      "compatible_archetypes": ["*"],         // wildcard = all
      "shell_nav_id": "shell-nav-public-bar",
      "shell_footer_id": "shell-footer-three-col",
      "layout_overrides": {},                 // page_id -> layout_id (overrides archetype default)
      "typography_scale": {
        "base_rem": 1.0,
        "h1_rem": 3.0,
        "h2_rem": 2.0,
        "section_y_rem": 6.0
      },
      "accent_density": "medium",
      "flourish_set": "subtle",
      "tags": ["balanced", "default"],
      "complexity_band": "medium"             // simple | medium | rich
    },
    {
      "id": "editorial-statement",
      "compatible_archetypes": ["healer-wellness", "coach-consultant", "therapist-counselor"],
      "shell_nav_id": "shell-nav-thin-statement",
      "shell_footer_id": "shell-footer-minimal-line",
      "layout_overrides": {
        "home": "{archetype}-home-editorial"  // pattern resolved at compose time
      },
      "typography_scale": {
        "base_rem": 1.125,
        "h1_rem": 5.0,
        "h2_rem": 3.0,
        "section_y_rem": 9.0
      },
      "accent_density": "low",
      "flourish_set": "literary",
      "tags": ["airy", "editorial", "quiet", "literary"],
      "complexity_band": "simple"
    }
    // ... 3 more
  ]
}
```

### New `_harbor_meta` field

```ts
template_family_hint?: string;   // 'classic-business' | 'editorial-statement' | ...
complexity_hint?: 'simple' | 'medium' | 'rich';
```

Both validated against `templates.json` registry by Python. Drop-on-unknown like every other hint.

### `BuildSpec`

```python
template_family_id: str = "classic-business"  # always present after resolve
```

## Picker

```python
def pick_template_family(archetype, harbor_meta, seed) -> str:
    # 1. Explicit hint that names a real, archetype-compatible family wins.
    # 2. Otherwise score families by:
    #      - compatibility_archetypes overlap (must be present; else filtered out)
    #      - mood_keywords overlap with template tags  (+3 each)
    #      - complexity_hint match with complexity_band (+5)
    #      - hero_style/density already-shipped hints align with template tags (+2 each)
    #      - avoid_keywords overlap with tags (-5 each)
    # 3. Tie-break with deterministic md5(seed + 'template-family').
```

Same contract as `pick_layout_variant`: pure function, deterministic, falls back to `classic-business` when no signal.

## Wire-through

```
Haven extraction
  -> spec_extraction.ts: prompt asks Opus for template_family_hint + complexity_hint
  -> harbor_to_compose.ts: normalizers + _harbor_meta wiring (TS side)
  -> compose/intake.py: scaffold_spec calls pick_template_family
  -> BuildSpec.template_family_id stamped
  -> compose/build.py: resolve_spec applies layout_overrides, swaps shell partials,
                       generates CSS with template's typography scale + accent_density
  -> emit_layout_public_ejs reads chosen shell_nav + shell_footer
  -> emit_styles emits typography-scale CSS variables consumed by all widgets
```

## Shell variants to author (Phase 1 delivery)

Beyond the existing `nav-public.ejs` + `footer.ejs`:

**Nav variants** (`compose/widgets/shell/`):
- `nav-thin-statement.ejs` (logo left wordmark only, one right CTA, 60px tall)
- `nav-side-rail.ejs` (persistent left nav 200px wide on lg+, sticky, hamburger on sm)
- `nav-centered-minimal.ejs` (3 links centered, no logo wordmark, very thin bar)
- `nav-image-banner.ejs` (full-width image hero strip with nav overlaid in flex row at top)

**Footer variants:**
- `footer-minimal-line.ejs` (single thin rule + one small line)
- `footer-grid-five-col.ejs` (full sitemap grid, 5 columns)
- `footer-single-line.ejs` (one centered line)

All variants honor the same data contract as today's nav-public/footer (logo URL, nav_items array, social URLs, contact info from settings). A template family declares which variant to use; the data plumbing doesn't change.

## Per-template CSS generation

Today `compose/build.py` emits one `style.css` per build with the chosen palette + font-pair injected as CSS variables and a fixed set of utility classes. The change for templates:

- Each family declares a `typography_scale` object. The CSS generator emits matching `--type-base`, `--type-h1`, `--type-h2`, `--type-section-y` variables.
- Widgets reference these variables via `font-size: var(--type-h1)` instead of hardcoded Tailwind classes (`text-5xl`).
- This is a layered migration: the *new* widget variants authored under template families use the variables; the existing widgets keep their Tailwind classes for backward compatibility. Over time, retrofit existing widgets.

## Phasing

| Phase | Scope | Outcome |
|---|---|---|
| 1 | Schema + 2 families (`classic-business` reskin, `editorial-statement` greenfield) + their nav + footer variants + picker + wire-through + tests | One healer build can land in `editorial-statement`; the rest still in classic. Visibly different sites.  |
| 2 | 3 more families (`directory-rich`, `simple-statement`, `event-led`) with full nav/footer/typography + complexity scoring | All 5 families live; archetype-compatibility map honored.  |
| 3 | Phase 4 customer-facing preview UI showing 3 template options with thumbnails (deferred from the layout-variants work) | Customer picks; choice persisted on keeper.  |
| 4 | Flourish-sets integration | Animations + glow + gradient options layer onto template families.  |

## Open questions

1. Five families enough, or want more? Each family is real authoring work; cap matters.
2. Archetype-compatibility table: any pairings that should NOT be allowed? (e.g. is `simple-statement` ever right for `tradesperson-contractor`? probably no.)
3. Complexity-tier signal source: extract from Haven conversation only, or also expose a "more options vs simpler" toggle on the customer-facing picker?
4. Backward compatibility, when a keeper rebuilds an existing app, should the template family stay locked to whatever they had before, or re-pick? One option: lock once chosen; only re-pick if they explicitly request "redesign."
5. Should template family choice surface on the admin dashboard (so SE reviewers can see "this is editorial-statement family" at a glance).

## Open question: design system tokens

The five families above implicitly define different design systems. To keep this maintainable, families should share a small set of base tokens (spacing scale, color roles, motion easing) and override only what they meaningfully differ on. A proposed design-tokens data file (compose/data/design-tokens.json, never created) formalizing the base set would help; can be a Phase 1 deliverable or pushed to Phase 2.

---

**This plan does not authorize implementation.** It exists so the work can be planned in detail before any code is written. The 2026-04-28 layout-variants work is the foundation; this is the layer above.
