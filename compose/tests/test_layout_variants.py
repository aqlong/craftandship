"""Tests for the layout variants.

Covers:
  - _stable_index + _rotate_suggestion (deterministic non-crypto hashing)
  - scaffold_spec rotation across archetype default_palette_suggestions
    and default_font_pair_suggestions (different app_names get different
    palettes/fonts)
  - _harbor_meta_hint + _validate_registry_id pure helpers
  - _extract_palette_from_v1 + _extract_font_from_v1 honor harbor hints
    BEFORE legacy hex/style mapping
  - pick_layout_variant: explicit hint, mood scoring, avoid penalties,
    determinism, unknown archetype/page returns None, bogus hint falls
    through
  - BuildSpec.from_dict carries _harbor_meta into harbor_meta field
  - resolve_spec uses pick_layout_variant to override default home layout
  - cross-archetype: every archetype has at least one variant_group
    sibling for home, AND every variant_group's siblings share archetype
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from compose.intake import (
    _extract_font_from_v1,
    _extract_palette_from_v1,
    _harbor_meta_hint,
    _harbor_meta_keyword_list,
    _rotate_suggestion,
    _score_layout_against_meta,
    _stable_index,
    _validate_registry_id,
    pick_layout_variant,
    scaffold_spec,
)
from compose.build import BuildSpec, get_layout, load_layouts, resolve_spec


# ============================================================
# Deterministic rotation
# ============================================================

class TestStableIndex:
    def test_returns_zero_when_modulo_is_zero(self):
        assert _stable_index("anything", "salt", 0) == 0

    def test_returns_zero_when_modulo_is_negative(self):
        assert _stable_index("anything", "salt", -3) == 0

    def test_in_range(self):
        for n in range(1, 50):
            v = _stable_index("seed", "salt", n)
            assert 0 <= v < n

    def test_deterministic(self):
        a = _stable_index("maya", "palette", 4)
        b = _stable_index("maya", "palette", 4)
        assert a == b

    def test_salt_changes_index(self):
        # On the same seed, different salts should not all collapse
        # to the same index (across at least 4 salts on a 4-list).
        picks = {_stable_index("maya", s, 4) for s in
                 ("palette", "font_pair", "layout", "image")}
        assert len(picks) >= 2

    def test_seed_spreads_across_modulo(self):
        # 12 short distinct seeds on a 4-list should hit all 4 indices.
        picks = {_stable_index(n, "palette", 4) for n in
                 ("Maya","Lena","Theo","River","Lily","Ash",
                  "Wren","Ivy","Sol","Juno","Fern","Brook")}
        assert picks == {0, 1, 2, 3}, f"poor spread: {picks}"


class TestRotateSuggestion:
    def test_single_item_list_returns_that_item(self):
        assert _rotate_suggestion(["only"], "any-seed", "any-salt") == "only"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _rotate_suggestion([], "x", "y")

    def test_deterministic(self):
        a = _rotate_suggestion(["a","b","c"], "maya", "palette")
        b = _rotate_suggestion(["a","b","c"], "maya", "palette")
        assert a == b


class TestScaffoldSpecRotation:
    def test_different_app_names_get_different_palettes(self):
        """The headline contract of the rotation: 12 healer builds with distinct
        app_names should NOT all pick the same palette."""
        outs = set()
        for name in ("Maya","Lena","Theo","River","Lily","Ash",
                     "Wren","Ivy","Sol","Juno","Fern","Brook"):
            s = scaffold_spec({
                "app_name": name, "archetype": "healer-wellness",
                "site_name": name, "admin_email": f"{name}@x.example",
            })
            outs.add(s["palette"])
        # 4 palettes available; expect at least 3 distinct picks across 12.
        assert len(outs) >= 3

    def test_different_app_names_get_different_fonts(self):
        outs = set()
        for name in ("Maya","Lena","Theo","River","Lily","Ash",
                     "Wren","Ivy","Sol","Juno","Fern","Brook"):
            s = scaffold_spec({
                "app_name": name, "archetype": "healer-wellness",
                "site_name": name, "admin_email": f"{name}@x.example",
            })
            outs.add(s["font_pair"])
        assert len(outs) >= 3

    def test_same_app_name_same_palette(self):
        s1 = scaffold_spec({"app_name":"Maya","archetype":"healer-wellness",
                            "site_name":"Maya","admin_email":"m@x.example"})
        s2 = scaffold_spec({"app_name":"Maya","archetype":"healer-wellness",
                            "site_name":"Maya","admin_email":"m@x.example"})
        assert s1["palette"] == s2["palette"]
        assert s1["font_pair"] == s2["font_pair"]

    def test_explicit_palette_in_answers_wins(self):
        s = scaffold_spec({
            "app_name":"Maya","archetype":"healer-wellness",
            "site_name":"Maya","admin_email":"m@x.example",
            "palette": "deep-plum",
        })
        assert s["palette"] == "deep-plum"

    def test_archetype_with_only_one_suggestion_still_works(self):
        # Defensive: even a single-item list shouldn't crash rotation.
        # Every archetype has 3+ suggestions; this
        # test guards against regressions on that audit.
        s = scaffold_spec({"app_name":"X","archetype":"healer-wellness",
                           "site_name":"X","admin_email":"x@x.example"})
        assert s["palette"] != ""


# ============================================================
# harbor_meta hint helpers + registry validation
# ============================================================

class TestHarborMetaHint:
    def test_returns_lowercase_trimmed_string(self):
        assert _harbor_meta_hint({"_harbor_meta":{"k":"  Foo-Bar  "}}, "k") == "foo-bar"

    def test_missing_meta_returns_none(self):
        assert _harbor_meta_hint({}, "k") is None

    def test_non_string_returns_none(self):
        assert _harbor_meta_hint({"_harbor_meta":{"k":123}}, "k") is None
        assert _harbor_meta_hint({"_harbor_meta":{"k":None}}, "k") is None
        assert _harbor_meta_hint({"_harbor_meta":{"k":["a"]}}, "k") is None

    def test_empty_string_returns_none(self):
        assert _harbor_meta_hint({"_harbor_meta":{"k":"   "}}, "k") is None

    def test_meta_not_dict_returns_none(self):
        assert _harbor_meta_hint({"_harbor_meta":"not-a-dict"}, "k") is None
        assert _harbor_meta_hint({"_harbor_meta":[]}, "k") is None


class TestHarborMetaKeywordList:
    def test_returns_lowercase_trimmed_strings(self):
        out = _harbor_meta_keyword_list(
            {"_harbor_meta":{"mood_keywords":["  Warm ","Earthy","  "]}},
            "mood_keywords",
        )
        assert out == ["warm", "earthy"]

    def test_non_list_returns_empty(self):
        assert _harbor_meta_keyword_list({"_harbor_meta":{"k":"warm"}}, "k") == []

    def test_missing_returns_empty(self):
        assert _harbor_meta_keyword_list({}, "k") == []


class TestValidateRegistryId:
    def test_valid_id_returned(self):
        assert _validate_registry_id("a", ["a","b","c"]) == "a"

    def test_invalid_id_returns_none(self):
        assert _validate_registry_id("z", ["a","b","c"]) is None

    def test_empty_list_returns_none(self):
        assert _validate_registry_id("a", []) is None


class TestExtractPaletteFromV1:
    def test_valid_harbor_hint_wins(self):
        v = {"_harbor_meta":{"palette_hint":"sage-clay"}}
        assert _extract_palette_from_v1(v) == "sage-clay"

    def test_invalid_harbor_hint_falls_through(self):
        v = {"_harbor_meta":{"palette_hint":"fake-palette"},
             "design":{"primary_color":"#6b1519"}}
        assert _extract_palette_from_v1(v) == "warm-earth"

    def test_no_meta_uses_legacy_hex(self):
        assert _extract_palette_from_v1({"design":{"primary_color":"#064e3b"}}) == "forest-emerald"

    def test_nothing_returns_none(self):
        assert _extract_palette_from_v1({}) is None


class TestExtractFontFromV1:
    def test_valid_harbor_hint_wins(self):
        v = {"_harbor_meta":{"font_pair_hint":"instrument-serif-inter"}}
        assert _extract_font_from_v1(v) == "instrument-serif-inter"

    def test_invalid_harbor_hint_falls_through(self):
        v = {"_harbor_meta":{"font_pair_hint":"comic-sans-arial"},
             "design":{"style":"editorial"}}
        assert _extract_font_from_v1(v) == "lora-inter"

    def test_no_meta_uses_legacy_style(self):
        assert _extract_font_from_v1({"design":{"style":"luxury sophisticated"}}) == "playfair-source-sans"

    def test_nothing_returns_none(self):
        assert _extract_font_from_v1({}) is None


# ============================================================
# pick_layout_variant scorer
# ============================================================

class TestPickLayoutVariant:
    def test_returns_none_for_unknown_archetype(self):
        assert pick_layout_variant("not-real", "home", {}) is None

    def test_returns_none_for_unknown_page(self):
        assert pick_layout_variant("healer-wellness", "no-such-page", {}) is None

    def test_explicit_hint_wins(self):
        assert pick_layout_variant(
            "healer-wellness", "home",
            {"layout_variant_hint": "healer-home-editorial"},
            seed="maya",
        ) == "healer-home-editorial"

    def test_bogus_hint_falls_through_to_scoring(self):
        # Hint names a layout that doesn't exist -- should NOT crash;
        # scorer should pick from candidates.
        out = pick_layout_variant(
            "healer-wellness", "home",
            {"layout_variant_hint": "fake-id", "mood_keywords":["warm"]},
            seed="lily",
        )
        assert out is not None and out.startswith("healer-home-")

    def test_mood_keywords_drive_editorial(self):
        assert pick_layout_variant(
            "healer-wellness", "home",
            {"mood_keywords":["airy","editorial","quiet"]},
            seed="maya",
        ) == "healer-home-editorial"

    def test_mood_keywords_drive_trust_first(self):
        out = pick_layout_variant(
            "coach-consultant", "home",
            {"mood_keywords":["trust-first","credibility"]},
            seed="theo",
        )
        assert out == "coach-home-trust-first"

    def test_avoid_keywords_penalize(self):
        out = pick_layout_variant(
            "healer-wellness", "home",
            {"avoid_keywords":["airy","editorial"]},
            seed="maya",
        )
        assert out != "healer-home-editorial"

    def test_density_hint_scored(self):
        assert pick_layout_variant(
            "coach-consultant", "home",
            {"density_hint":"airy", "hero_style_hint":"editorial"},
            seed="theo",
        ) == "coach-home-editorial"

    def test_no_hints_deterministic_across_seeds(self):
        # No hints → all candidates score 0 → tie-break by stable_index
        # over seed. Should be deterministic but spread across siblings.
        outs = set()
        for seed in ("a","b","c","d","e","f","g","h","i","j","k","l"):
            out = pick_layout_variant("healer-wellness", "home", {}, seed=seed)
            outs.add(out)
        assert len(outs) >= 2  # spread

    def test_determinism(self):
        a = pick_layout_variant("coach-consultant","home",
                                 {"mood_keywords":["credibility"]}, seed="theo")
        b = pick_layout_variant("coach-consultant","home",
                                 {"mood_keywords":["credibility"]}, seed="theo")
        assert a == b

    def test_handles_non_dict_meta(self):
        # None / list / string should not crash.
        out = pick_layout_variant("healer-wellness","home", None, seed="x")
        assert out is not None
        out = pick_layout_variant("healer-wellness","home", "string", seed="x")
        assert out is not None
        out = pick_layout_variant("healer-wellness","home", [], seed="x")
        assert out is not None


class TestScoreLayoutAgainstMeta:
    def test_empty_meta_scores_zero(self):
        layout = {"tags":["warm","balanced"], "mood":"warm", "density":"standard", "hero_style":"centered"}
        assert _score_layout_against_meta(layout, {}) == 0

    def test_overlap_scores_positive(self):
        layout = {"tags":["editorial","airy"], "mood":"quiet", "density":"airy", "hero_style":"editorial"}
        meta = {"mood_keywords":["editorial","airy","quiet"]}
        # 3 overlaps × 3 = 9
        assert _score_layout_against_meta(layout, meta) == 9

    def test_avoid_subtracts(self):
        layout = {"tags":["editorial","airy"], "mood":"quiet", "density":"airy", "hero_style":"editorial"}
        meta = {"avoid_keywords":["editorial","airy"]}
        # 2 overlaps × -5 = -10
        assert _score_layout_against_meta(layout, meta) == -10

    def test_score_handles_missing_fields(self):
        assert _score_layout_against_meta({}, {"mood_keywords":["x"]}) == 0
        assert _score_layout_against_meta({"tags":["x"]}, {}) == 0


# ============================================================
# Build wire-through
# ============================================================

class TestBuildSpecHarborMeta:
    def test_from_dict_reads_harbor_meta_key(self):
        spec = scaffold_spec({
            "app_name":"x","archetype":"healer-wellness","site_name":"X","admin_email":"x@x.example",
            "_harbor_meta":{"mood_keywords":["airy"]},
        })
        b = BuildSpec.from_dict(spec)
        assert b.harbor_meta == {"mood_keywords":["airy"]}

    def test_from_dict_handles_missing_meta(self):
        spec = scaffold_spec({"app_name":"x","archetype":"healer-wellness",
                              "site_name":"X","admin_email":"x@x.example"})
        b = BuildSpec.from_dict(spec)
        assert b.harbor_meta == {}


class TestResolveSpecLayoutOverride:
    def test_no_meta_uses_archetype_default_or_sibling(self):
        # Without hints, scorer ties; tie-break may pick a sibling. Either
        # the default OR a sibling in the same group is acceptable.
        spec = scaffold_spec({"app_name":"maya","archetype":"healer-wellness",
                              "site_name":"M","admin_email":"m@x.example"})
        b = BuildSpec.from_dict(spec)
        r = resolve_spec(b)
        home = r.page_layouts.get("home")
        assert home is not None
        assert home.get("variant_group") == "healer-home"

    def test_mood_hint_drives_editorial_at_build_time(self):
        spec = scaffold_spec({
            "app_name":"maya","archetype":"healer-wellness",
            "site_name":"M","admin_email":"m@x.example",
            "_harbor_meta":{"mood_keywords":["airy","editorial","quiet"]},
        })
        b = BuildSpec.from_dict(spec)
        r = resolve_spec(b)
        assert r.page_layouts["home"].get("variant_id") == "editorial"

    def test_avoid_hint_avoids_editorial_at_build_time(self):
        spec = scaffold_spec({
            "app_name":"maya","archetype":"healer-wellness",
            "site_name":"M","admin_email":"m@x.example",
            "_harbor_meta":{"avoid_keywords":["editorial","airy"]},
        })
        b = BuildSpec.from_dict(spec)
        r = resolve_spec(b)
        assert r.page_layouts["home"].get("variant_id") != "editorial"


# ============================================================
# Cross-archetype contract: every variant group is internally consistent
# ============================================================

class TestVariantGroupContracts:
    def test_every_archetype_with_variants_has_at_least_two_siblings(self):
        layouts = load_layouts().get("layouts", {})
        groups: dict = {}
        for k, v in layouts.items():
            g = v.get("variant_group")
            if g:
                groups.setdefault(g, []).append(k)
        # All 8 explicitly-extended groups must have ≥2 siblings.
        # therapist-home + tradesperson-home gained a statement-portrait
        # sibling on 2026-05-07 to match the existing 6-group set.
        for g in ("healer-home","coach-home","fitness-home","salon-home",
                  "restaurant-home","local-service-home",
                  "therapist-home","tradesperson-home"):
            assert g in groups, f"missing variant group: {g}"
            assert len(groups[g]) >= 2, f"{g} has fewer than 2 siblings: {groups[g]}"

    def test_variant_group_siblings_share_archetype_and_page(self):
        """Belt-and-suspenders: a variant group whose siblings come from
        different archetypes is incoherent. The scorer would happily mix
        them since it filters by archetype+page first, but this guards
        against authoring slips."""
        layouts = load_layouts().get("layouts", {})
        groups: dict = {}
        for k, v in layouts.items():
            g = v.get("variant_group")
            if not g:
                continue
            entry = (v.get("archetype"), v.get("page"))
            groups.setdefault(g, set()).add(entry)
        for g, entries in groups.items():
            assert len(entries) == 1, f"{g} siblings span archetype/page: {entries}"

    def test_every_layout_with_tags_has_string_list(self):
        layouts = load_layouts().get("layouts", {})
        for k, v in layouts.items():
            tags = v.get("tags")
            if tags is None:
                continue
            assert isinstance(tags, list), f"{k} tags not a list"
            assert all(isinstance(t, str) for t in tags), f"{k} tags has non-string"

    def test_every_variant_layout_widgets_resolve(self):
        """Every widget id referenced in any variant layout must exist
        in widget-registry.json. Catches typos in authored variants."""
        from compose.build import load_widgets
        widgets = load_widgets().get("widgets", {})
        layouts = load_layouts().get("layouts", {})
        for layout_key, layout in layouts.items():
            if not layout.get("variant_group"):
                continue
            for w in layout.get("widgets") or []:
                wid = w.get("id")
                assert wid in widgets, f"layout {layout_key} references unknown widget {wid}"


# ============================================================
# Db-source coverage for CRUD-backed listing widgets (2026-04-28 fix)
# ============================================================
# Closes the gap left by test_admin_listing_db_source_coverage_across_archetypes
# in test_build_pipeline.py: that harness only checks layouts whose page_id
# matches a data_model plural (e.g. /reviews -> reviews). It skips pages
# named for the THING displayed rather than the table (e.g. /gallery
# displays photos, /journal displays blog_posts). This family of bugs has
# bitten us repeatedly: the prior healer-gallery-db-source bug was open in
# the roadmap because the harness above didn't catch /gallery.

# Widget id -> implied table name (when present, source MUST be db:<table>
# unless the page legitimately serves spec-static content).
_LISTING_WIDGET_IMPLIED_TABLE = {
    "photo-gallery-grid":  "photos",
    "menu-grouped-list":   "menu_items",
    "instructor-grid":     "instructors",
    "staff-card-grid":     "staff",
    "service-area-list":   "service_areas",
    "reviews-grid":        "reviews",
    "weekly-schedule-grid":"schedule_slots",
    # Added 2026-04-29: testimonials-stack reads from db:testimonials via
    # spec_overlay. An earlier manual fix flipped the 7
    # affected archetype layouts BEFORE this harness existed, so the
    # regression class was never covered. Adding here so any future
    # archetype that ships testimonials-stack with spec.* source fails
    # this test before merge.
    "testimonials-stack":  "testimonials",
}


class TestListingWidgetDbSource:
    """For every layout in page-layouts.json that uses a CRUD-backed
    listing widget, the source MUST start with `db:`. This catches the
    full family of bugs where a listing widget points at static spec
    data while admin CRUD writes elsewhere -- the bug class that hit
    healer/blog, restaurant/menu, and
    healer/gallery.
    """

    def test_listing_widgets_use_db_source(self):
        layouts = load_layouts().get("layouts", {})
        failures = []
        for key, layout in layouts.items():
            for w in layout.get("widgets") or []:
                wid = w.get("id")
                if wid not in _LISTING_WIDGET_IMPLIED_TABLE:
                    continue
                source = w.get("source", "")
                if not source.startswith("db:"):
                    failures.append(
                        f"{key}: widget {wid!r} reads {source!r}, expected db:<table>"
                    )
        assert not failures, "\n  ".join(["listing-widget db-source violations:"] + failures)

    # resource-list-card-grid is the generic CRUD-backed listing widget
    # (blog_posts, case_studies, events, reviews, ...). It does not map to ONE
    # table, so it is excluded from _LISTING_WIDGET_IMPLIED_TABLE and checked
    # separately here: every layout using it must read a db:<table> source
    # (admin CRUD writes to the table; a spec.* source renders empty). This is
    # the widget that regressed on event-producer-home (W3, 2026-06-18) and the
    # historical /blog bug. Genuinely spec-static pages go in the
    # allowlist; today there are none.
    _RESOURCE_LIST_STATIC_ALLOWLIST: set = set()

    def test_resource_list_card_grid_uses_db_source(self):
        layouts = load_layouts().get("layouts", {})
        failures = []
        for key, layout in layouts.items():
            if key in self._RESOURCE_LIST_STATIC_ALLOWLIST:
                continue
            for w in layout.get("widgets") or []:
                if w.get("id") != "resource-list-card-grid":
                    continue
                source = w.get("source", "")
                if not source.startswith("db:"):
                    failures.append(
                        f"{key}: resource-list-card-grid reads {source!r}, expected db:<table>"
                    )
        assert not failures, "\n  ".join(
            ["resource-list-card-grid must read db:<table> (or be allowlisted):"] + failures
        )


# ============================================================
# Cross-archetype statement-portrait sibling regression (2026-05-07)
# ============================================================
# Generalizes the tradesperson-home-statement-portrait sibling pattern
# across coach/healer/therapist/local-service/salon so a
# single keeper signal -- _harbor_meta.layout_variant_hint='statement-portrait'
# OR mood_keywords containing minimal/spare -- selects the same minimal
# single-page-feel home in every solo-operator archetype.

_STATEMENT_PORTRAIT_ARCHETYPE_KEYS = {
    "tradesperson-contractor": "tradesperson-home-statement-portrait",
    "coach-consultant":        "coach-home-statement-portrait",
    "healer-wellness":         "healer-home-statement-portrait",
    "therapist-counselor":     "therapist-home-statement-portrait",
    "local-service":           "local-service-home-statement-portrait",
    "salon-spa":               "salon-home-statement-portrait",
}


class TestStatementPortraitCrossArchetype:
    """The statement-portrait sibling layout must exist + be discoverable
    via the layout picker for every solo-operator archetype."""

    def test_every_archetype_has_statement_portrait_sibling(self):
        layouts = load_layouts().get("layouts", {})
        missing = []
        for archetype, expected_key in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS.items():
            if expected_key not in layouts:
                missing.append(f"{archetype}: missing {expected_key!r}")
                continue
            layout = layouts[expected_key]
            if layout.get("archetype") != archetype:
                missing.append(
                    f"{expected_key}: archetype={layout.get('archetype')!r} "
                    f"expected {archetype!r}"
                )
            if layout.get("variant_id") != "statement-portrait":
                missing.append(
                    f"{expected_key}: variant_id={layout.get('variant_id')!r} "
                    f"expected 'statement-portrait'"
                )
        assert not missing, "\n  ".join(["statement-portrait gaps:"] + missing)

    def test_every_sibling_uses_only_hero_two_col_statement(self):
        """The single-page-feel doctrine: the statement-portrait variants
        deliberately render ONE widget (hero-two-col-statement) so the page
        feels intentional and quiet. A future contributor adding a second
        widget should re-think whether the variant_id still fits."""
        layouts = load_layouts().get("layouts", {})
        violations = []
        for expected_key in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS.values():
            widgets = layouts[expected_key].get("widgets") or []
            ids = [w.get("id") for w in widgets]
            if ids != ["hero-two-col-statement"]:
                violations.append(f"{expected_key}: widgets={ids}")
        assert not violations, "\n  ".join(
            ["statement-portrait widget-shape violations:"] + violations
        )

    def test_every_sibling_carries_canonical_tag_set(self):
        """Tag intersection drives the picker's mood-keyword scoring.
        Every sibling should carry the same canonical tag set so a single
        cross-archetype hint produces the same result everywhere."""
        canonical = {
            "minimal", "single-page-feel", "portrait-led",
            "spare", "real", "solo-operator",
        }
        layouts = load_layouts().get("layouts", {})
        violations = []
        for expected_key in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS.values():
            tags = set(layouts[expected_key].get("tags") or [])
            missing = canonical - tags
            if missing:
                violations.append(f"{expected_key}: missing tags {sorted(missing)}")
        assert not violations, "\n  ".join(
            ["statement-portrait tag-set violations:"] + violations
        )

    def test_bare_variant_hint_picks_sibling_in_every_archetype(self):
        """layout_variant_hint='statement-portrait' (the bare variant_id)
        must select the right sibling in every archetype. This is the
        cross-archetype keeper signal: one hint, six archetypes, six
        consistent picks."""
        for archetype, expected_key in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS.items():
            picked = pick_layout_variant(
                archetype, "home",
                {"layout_variant_hint": "statement-portrait"},
                seed=archetype,
            )
            assert picked == expected_key, (
                f"{archetype}: picker returned {picked!r}, "
                f"expected {expected_key!r}"
            )

    def test_full_key_hint_still_wins_over_bare_variant(self):
        """If the keeper supplies the full key, the picker must honor it
        verbatim (full-key match precedes bare variant_id match)."""
        picked = pick_layout_variant(
            "coach-consultant", "home",
            {"layout_variant_hint": "coach-home-statement-portrait"},
            seed="theo",
        )
        assert picked == "coach-home-statement-portrait"

    def test_minimal_spare_mood_keywords_drive_sibling(self):
        """Without the explicit hint, mood_keywords containing minimal/spare
        should still steer the picker toward the statement-portrait sibling
        in every archetype where the alternatives don't share those tags."""
        for archetype, expected_key in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS.items():
            picked = pick_layout_variant(
                archetype, "home",
                {"mood_keywords": ["minimal", "spare", "single-page-feel"]},
                seed=archetype,
            )
            assert picked == expected_key, (
                f"{archetype}: mood_keywords picker returned {picked!r}, "
                f"expected {expected_key!r}"
            )

    def test_no_signal_does_not_random_walk_to_statement_portrait(self):
        """Regression: before 2026-05-07 the picker tie-break would
        random-walk into the statement-portrait sibling on a no-signal
        build, stripping the home page of every widget except the hero.
        Statement-portrait must be opt-in via explicit hint or positive
        mood scoring; a bare scaffold_spec with no _harbor_meta must
        never produce it."""
        seeds = [
            "coach-threading-sample", "coach-alias-sample",
            "healer-threading-sample", "healer-alias-sample",
            "salon-sample-a", "salon-sample-b",
            "therapist-sample", "local-service-sample",
        ]
        for archetype in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS:
            for seed in seeds:
                picked = pick_layout_variant(archetype, "home", {}, seed=seed)
                assert picked is not None
                assert picked != _STATEMENT_PORTRAIT_ARCHETYPE_KEYS[archetype], (
                    f"{archetype} (seed={seed}): no-signal picker landed on "
                    f"statement-portrait sibling; should be opt-in only"
                )

    def test_resolve_spec_uses_statement_portrait_when_hinted(self):
        """End-to-end: a v2 spec with layout_variant_hint='statement-portrait'
        resolves to the statement-portrait sibling at build time, in every
        archetype that ships one."""
        for archetype, expected_key in _STATEMENT_PORTRAIT_ARCHETYPE_KEYS.items():
            spec = scaffold_spec({
                "app_name": f"sample-{archetype}",
                "archetype": archetype,
                "site_name": "Sample",
                "admin_email": "x@x.example",
                "_harbor_meta": {"layout_variant_hint": "statement-portrait"},
            })
            b = BuildSpec.from_dict(spec)
            r = resolve_spec(b)
            home = r.page_layouts.get("home") or {}
            assert home.get("variant_id") == "statement-portrait", (
                f"{archetype}: resolve_spec did not pick statement-portrait sibling"
            )


# ============================================================
# 
# ============================================================

from compose.intake import (
    pick_template_family,
    _candidate_template_families,
    _score_template_against_meta,
)
from compose.build import load_templates, get_template_family


class TestTemplateRegistry:
    def test_classic_business_exists(self):
        f = get_template_family("classic-business")
        assert f is not None
        assert f["shell_nav_id"] == "shell-nav-public"
        assert f["shell_footer_id"] == "shell-footer"
        assert f["compatible_archetypes"] == ["*"]

    def test_editorial_statement_exists(self):
        f = get_template_family("editorial-statement")
        assert f is not None
        assert f["shell_nav_id"] == "shell-nav-thin-statement"
        assert f["shell_footer_id"] == "shell-footer-minimal-line"
        # Editorial only compatible with select archetypes (not '*').
        assert "*" not in f["compatible_archetypes"]
        assert "healer-wellness" in f["compatible_archetypes"]

    def test_directory_rich_exists(self):
        f = get_template_family("directory-rich")
        assert f is not None
        assert f["shell_nav_id"] == "shell-nav-side-rail"
        assert f["shell_footer_id"] == "shell-footer-grid-five-col"
        assert "tradesperson-contractor" in f["compatible_archetypes"]
        # Directory-rich pads main content to clear the 240px side rail.
        assert "padding-left: 240px" in f.get("extra_body_css", "")

    def test_simple_statement_exists(self):
        f = get_template_family("simple-statement")
        assert f is not None
        assert f["shell_nav_id"] == "shell-nav-centered-minimal"
        assert f["shell_footer_id"] == "shell-footer-single-line"
        assert "healer-wellness" in f["compatible_archetypes"]
        # Hugest type scale of any family (intentional for the brand).
        assert f["typography_scale"]["h1_rem"] >= 5.5

    def test_event_led_exists(self):
        f = get_template_family("event-led")
        assert f is not None
        assert f["shell_nav_id"] == "shell-nav-image-banner"
        assert f["shell_footer_id"] == "shell-footer"
        assert "event-producer" in f["compatible_archetypes"]

    def test_all_six_families_present(self):
        from compose.build import load_templates
        ids = {t["id"] for t in load_templates().get("templates", [])}
        assert ids == {
            "classic-business", "editorial-statement",
            "directory-rich", "simple-statement", "event-led",
            "editorial-sanctuary",
        }

    def test_every_family_has_distinct_shell_pair(self):
        """Two families MUST NOT share both shell-nav AND shell-footer
        ids (otherwise they're the same family wearing a different name).
        Sharing one component is fine: event-led reuses shell-footer."""
        from compose.build import load_templates
        seen = {}
        for t in load_templates().get("templates", []):
            pair = (t["shell_nav_id"], t["shell_footer_id"])
            assert pair not in seen, (
                f"{t['id']} duplicates shell pair of {seen[pair]}"
            )
            seen[pair] = t["id"]

    def test_every_family_has_typography_scale(self):
        from compose.build import load_templates
        for t in load_templates().get("templates", []):
            ts = t.get("typography_scale")
            assert isinstance(ts, dict), f"{t['id']} missing typography_scale"
            for required in ("base_rem", "h1_rem", "h2_rem", "section_y_rem"):
                assert required in ts, f"{t['id']} typography_scale missing {required}"

    def test_every_family_shell_widget_files_exist(self):
        """Every family's shell_nav + shell_footer must point at a widget
        file that actually exists on disk. Catches authoring slips."""
        from pathlib import Path
        from compose.build import load_templates, load_widgets
        widgets = load_widgets().get("widgets", {})
        widget_dir = Path(__file__).resolve().parent.parent / "widgets"
        for t in load_templates().get("templates", []):
            for key in ("shell_nav_id", "shell_footer_id"):
                wid = t.get(key)
                assert wid in widgets, f"{t['id']} {key}={wid} not in widget-registry"
                rel = widgets[wid]["file"]
                full = widget_dir.parent / rel
                assert full.exists(), f"{t['id']} {key} -> {rel} missing on disk"

    def test_unknown_id_returns_none(self):
        assert get_template_family("does-not-exist") is None


class TestPickTemplateFamily:
    def test_no_hints_locks_to_default_family(self):
        """No hints -> classic-business wins by 'default' tie-break,
        regardless of seed. Customers without expressed preference get
        a stable, predictable shell. Customers WITH preference still
        get variety (other tests cover that)."""
        for seed in ("a","b","c","tf-classic","Maya","Theo","River"):
            out = pick_template_family("healer-wellness", None, seed=seed)
            assert out == "classic-business", f"seed={seed!r} drifted to {out}"

    def test_hint_wins_when_compatible(self):
        out = pick_template_family(
            "healer-wellness",
            {"template_family_hint": "editorial-statement"},
            seed="anything",
        )
        assert out == "editorial-statement"

    def test_hint_falls_through_when_incompatible(self):
        # editorial-statement is not compatible with fitness-studio.
        # Picker MUST refuse the hint and fall back to compatible candidates.
        out = pick_template_family(
            "fitness-studio",
            {"template_family_hint": "editorial-statement"},
            seed="anything",
        )
        assert out == "classic-business"

    def test_mood_keywords_score_editorial_higher(self):
        out = pick_template_family(
            "healer-wellness",
            {"mood_keywords": ["airy", "editorial", "quiet", "minimal", "statement"]},
            seed="anything",
        )
        assert out == "editorial-statement"

    def test_avoid_keywords_penalize(self):
        out = pick_template_family(
            "healer-wellness",
            {"avoid_keywords": ["airy", "editorial", "minimal", "statement"]},
            seed="anything",
        )
        assert out == "classic-business"

    def test_complexity_hint_simple_picks_editorial(self):
        out = pick_template_family(
            "healer-wellness",
            {"complexity_hint": "simple"},
            seed="anything",
        )
        # Multiple "simple"-band families compete here (editorial-statement,
        # simple-statement, editorial-sanctuary). With no other signal,
        # any of the three is acceptable; the picker must avoid the
        # medium-band classic-business default.
        assert out in {"editorial-statement", "simple-statement", "editorial-sanctuary"}

    def test_unknown_archetype_falls_back_to_classic(self):
        # Even an entirely unknown archetype gets a sane default.
        # candidates list is non-empty (classic-business has '*' compat).
        out = pick_template_family("not-a-real-archetype", None, seed="x")
        assert out == "classic-business"

    def test_determinism(self):
        a = pick_template_family("healer-wellness", {"mood_keywords":["warm"]}, seed="theo")
        b = pick_template_family("healer-wellness", {"mood_keywords":["warm"]}, seed="theo")
        assert a == b

    def test_handles_non_dict_meta(self):
        for bad in (None, "string", [], 0, True):
            out = pick_template_family("healer-wellness", bad, seed="x")
            assert out in (
                "classic-business", "editorial-statement", "simple-statement",
            )

    def test_directory_rich_picked_for_dense_archetype_with_dense_hint(self):
        out = pick_template_family(
            "tradesperson-contractor",
            {"mood_keywords": ["dense", "directory", "structured", "practical"]},
            seed="hvac-co",
        )
        assert out == "directory-rich"

    def test_simple_statement_picked_for_huge_type_signal(self):
        out = pick_template_family(
            "coach-consultant",
            {"mood_keywords": ["simple", "minimal", "huge-type", "statement", "one-page"]},
            seed="solo-coach",
        )
        assert out == "simple-statement"

    def test_event_led_picked_for_event_archetype_no_hints(self):
        # event-producer has both classic-business (wildcard) and event-led
        # as candidates. With NO hints, the default tie-break prefers
        # `tags: ['default']` -> classic-business. With the event-led hint
        # OR matching mood, event-led should win.
        out_no_hint = pick_template_family("event-producer", None, seed="any")
        assert out_no_hint == "classic-business"  # default-pref tie-break
        out_hinted = pick_template_family(
            "event-producer",
            {"mood_keywords": ["event", "image-led", "hero-banner", "energetic"]},
            seed="any",
        )
        assert out_hinted == "event-led"

    def test_directory_rich_not_offered_to_solo_archetype(self):
        # directory-rich is incompatible with healer-wellness; even if
        # the keeper hints for it, picker must refuse.
        out = pick_template_family(
            "healer-wellness",
            {"template_family_hint": "directory-rich"},
            seed="any",
        )
        assert out != "directory-rich"

    def test_event_led_only_for_event_compatible_archetypes(self):
        # event-led compat: event-producer, fitness-studio, restaurant-cafe.
        # Healer should NOT land in event-led even with explicit hint.
        out = pick_template_family(
            "healer-wellness",
            {"template_family_hint": "event-led"},
            seed="any",
        )
        assert out != "event-led"

    def test_archetype_compatibility_coverage(self):
        """For every archetype that has a compatible family beyond
        classic-business, that family MUST actually be picked when the
        keeper signals for it. Catches authoring drift in compat lists."""
        from compose.build import load_templates
        archetype_to_compatible = {}
        for t in load_templates().get("templates", []):
            for arch in t["compatible_archetypes"]:
                if arch == "*":
                    continue
                archetype_to_compatible.setdefault(arch, []).append(t["id"])
        for arch, fams in archetype_to_compatible.items():
            for fam in fams:
                out = pick_template_family(
                    arch, {"template_family_hint": fam}, seed="x",
                )
                assert out == fam, f"{arch} should accept hint -> {fam}, got {out!r}"


class TestTemplateFamilyWireThrough:
    def test_buildspec_carries_template_family_id(self):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec
        spec = scaffold_spec({
            "app_name":"test","archetype":"healer-wellness",
            "site_name":"T","admin_email":"a@x.example",
            "_harbor_meta":{"template_family_hint":"editorial-statement"},
        })
        assert spec["template_family_id"] == "editorial-statement"
        b = BuildSpec.from_dict(spec)
        assert b.template_family_id == "editorial-statement"

    def test_resolved_spec_has_template_family_dict(self):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name":"test","archetype":"healer-wellness",
            "site_name":"T","admin_email":"a@x.example",
            "_harbor_meta":{"template_family_hint":"editorial-statement"},
        })
        rs = resolve_spec(BuildSpec.from_dict(spec))
        assert rs.template_family is not None
        assert rs.template_family["id"] == "editorial-statement"
        assert rs.template_family["shell_nav_id"] == "shell-nav-thin-statement"

    def test_resolved_spec_template_family_is_none_when_id_empty(self):
        from compose.build import BuildSpec, resolve_spec
        spec_dict = {
            "app_name":"test","archetype":"healer-wellness",
            "site_name":"T","admin_email":"a@x.example",
            "palette":"warm-earth","font_pair":"rubik-nunito",
            # Deliberately omit template_family_id to exercise
            # backward-compatible legacy path.
        }
        rs = resolve_spec(BuildSpec.from_dict(spec_dict))
        assert rs.template_family is None


class TestEmitLayoutFiles:
    """The layout-public.ejs emitter must use the chosen template family's
    shell partials + typography scale."""

    def _build_resolved(self, template_id: str = ""):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        answers = {
            "app_name":"emit-test","archetype":"healer-wellness",
            "site_name":"T","admin_email":"a@x.example",
        }
        if template_id:
            answers["template_family_id"] = template_id
        spec = scaffold_spec(answers)
        return resolve_spec(BuildSpec.from_dict(spec))

    def test_classic_emits_nav_public(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("classic-business")
        files = dict(emit_layout_files(rs))
        layout = files["views/layout-public.ejs"]
        assert "include('./widgets/shell/nav-public'" in layout
        assert "include('./widgets/shell/footer'," in layout
        # No leftover placeholders.
        assert "__SHELL_NAV_FILE__" not in layout
        assert "__SHELL_FOOTER_FILE__" not in layout

    def test_editorial_emits_thin_statement_nav_and_minimal_footer(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("editorial-statement")
        files = dict(emit_layout_files(rs))
        layout = files["views/layout-public.ejs"]
        assert "include('./widgets/shell/nav-thin-statement'" in layout
        assert "include('./widgets/shell/footer-minimal-line'" in layout
        assert "nav-public" not in layout

    def test_editorial_typography_scale_present(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("editorial-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "--type-h1: 5.0rem" in layout
        assert "--type-base: 1.125rem" in layout
        assert ".cas-type-h1" in layout

    def test_classic_typography_scale_present_with_default_values(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("classic-business")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "--type-h1: 3.0rem" in layout

    def test_editorial_emits_body_class_for_scoped_css(self):
        """Body class is what makes typography scale actually visible
        on existing widgets. Without it, the CSS variables are inert
        because no widget uses .cas-type-h1 directly. The body MAY
        also carry a cas-flourish-<id> class (the design flourishes);
        we assert the template class is present without anchoring to
        the rest of the class list."""
        from compose.build import emit_layout_files
        rs = self._build_resolved("editorial-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        # The body tag carries cas-template-editorial-statement (possibly
        # alongside cas-flourish-literary; this assertion tolerates both).
        import re
        m = re.search(r'<body class="([^"]*)">', layout)
        assert m is not None, "no <body class=\"...\"> tag emitted"
        body_classes = m.group(1).split()
        assert "cas-template-editorial-statement" in body_classes
        # Body-scoped overrides for existing h1/h2 elements.
        assert "body.cas-template-editorial-statement h1" in layout
        assert "body.cas-template-editorial-statement h2" in layout

    def test_classic_emits_body_class_for_scoped_css(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("classic-business")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        import re
        m = re.search(r'<body class="([^"]*)">', layout)
        assert m is not None
        body_classes = m.group(1).split()
        assert "cas-template-classic-business" in body_classes

    def test_directory_rich_emits_side_rail_and_padding(self):
        """Directory-rich's side rail requires main content padding-left:
        240px on lg+ to clear the rail. Without it, content sits under
        the rail and is unreadable."""
        from compose.build import emit_layout_files
        rs = self._build_resolved("directory-rich")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "include('./widgets/shell/nav-side-rail'" in layout
        assert "include('./widgets/shell/footer-grid-five-col'" in layout
        # The extra_body_css must end up in the layout's <style>.
        assert "padding-left: 240px" in layout
        assert "@media (min-width: 1024px)" in layout

    def test_simple_statement_emits_centered_minimal_and_huge_type(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("simple-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "include('./widgets/shell/nav-centered-minimal'" in layout
        assert "include('./widgets/shell/footer-single-line'" in layout
        # Hugest type scale of any family.
        assert "--type-h1: 6.0rem" in layout

    def test_event_led_emits_image_banner_and_classic_footer(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("event-led")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "include('./widgets/shell/nav-image-banner'" in layout
        # event-led intentionally REUSES classic shell-footer.
        assert "include('./widgets/shell/footer'," in layout
        # Should NOT include the editorial or directory footer.
        assert "footer-minimal-line" not in layout
        assert "footer-grid-five-col" not in layout

    def test_no_template_family_falls_back_to_classic_shell(self):
        """A spec with template_family_id='' (legacy) emits classic shells,
        no leftover placeholders, no broken includes."""
        from compose.build import BuildSpec, resolve_spec, emit_layout_files
        spec = BuildSpec.from_dict({
            "app_name":"legacy","archetype":"healer-wellness",
            "site_name":"L","admin_email":"a@x.example",
            "palette":"warm-earth","font_pair":"rubik-nunito",
        })
        rs = resolve_spec(spec)
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "__SHELL_NAV_FILE__" not in layout
        assert "include('./widgets/shell/nav-public'" in layout
        assert "include('./widgets/shell/footer'," in layout


# ============================================================
# Flourish-set system
# ============================================================

class TestFlourishSetRegistry:
    """compose/data/flourish-sets.json defines opt-in CSS-only design
    accents. Each template family declares `flourish_set: <id>` to
    select one. emit_layout_files injects the matching CSS + adds a
    `cas-flourish-<id>` body class.
    """

    def test_all_4_starter_sets_present(self):
        from compose.build import load_flourish_sets
        ids = {s["id"] for s in load_flourish_sets().get("sets", [])}
        assert ids == {"none", "subtle", "literary", "expressive"}

    def test_get_flourish_set_returns_known_set(self):
        from compose.build import get_flourish_set
        assert get_flourish_set("subtle") is not None
        assert get_flourish_set("none") is not None

    def test_get_flourish_set_returns_none_for_unknown(self):
        from compose.build import get_flourish_set
        assert get_flourish_set("does-not-exist") is None

    def test_none_set_has_empty_css(self):
        """The 'none' set is a real registry entry but with empty CSS,
        so emit_layout_files can treat 'flourish_set: none' uniformly
        instead of branching on missing."""
        from compose.build import get_flourish_set
        s = get_flourish_set("none")
        assert s is not None
        assert s["css"] == ""

    def test_every_active_set_honors_prefers_reduced_motion(self):
        """Accessibility contract: every set with non-empty CSS MUST
        wrap its motion rules in @media (prefers-reduced-motion: no-preference)."""
        from compose.build import load_flourish_sets
        for s in load_flourish_sets().get("sets", []):
            if not s.get("css"):
                continue
            assert "prefers-reduced-motion: no-preference" in s["css"], \
                f"flourish set {s['id']!r} missing prefers-reduced-motion guard"

    def test_every_template_family_declares_flourish_set(self):
        """All 5 starter families must opt into a flourish set (or
        explicitly opt out via 'none'). Catches authoring drift where
        a new family forgets to set the field."""
        from compose.build import load_templates, load_flourish_sets
        valid_ids = {s["id"] for s in load_flourish_sets().get("sets", [])}
        for t in load_templates().get("templates", []):
            fs = t.get("flourish_set")
            assert fs is not None, f"family {t['id']!r} missing flourish_set"
            assert fs in valid_ids, f"family {t['id']!r} flourish_set {fs!r} not in registry"


class TestEmitFlourishCss:
    """emit_layout_files must inject the family's flourish-set CSS +
    add a cas-flourish-<id> body class so the set's body-class-scoped
    rules match."""

    def _build(self, template_id):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name": "fl-test", "archetype": "healer-wellness",
            "site_name": "F", "admin_email": "a@x.example",
            "template_family_id": template_id,
        })
        return resolve_spec(BuildSpec.from_dict(spec))

    def test_classic_subtle_flourish_class_and_css_emitted(self):
        from compose.build import emit_layout_files
        rs = self._build("classic-business")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "cas-flourish-subtle" in layout
        assert "Flourish set: subtle" in layout
        assert "@media (prefers-reduced-motion: no-preference)" in layout
        assert "cas-fade-in" in layout

    def test_editorial_literary_flourish_emits_drop_cap_and_link_underline(self):
        from compose.build import emit_layout_files
        rs = self._build("editorial-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "cas-flourish-literary" in layout
        # Drop-cap rule (opt-in via .cas-drop-cap)
        assert "cas-drop-cap" in layout
        # Background-position underline grow
        assert "background-size 0.28s ease" in layout

    def test_simple_statement_none_flourish_no_css_block(self):
        from compose.build import emit_layout_files
        rs = self._build("simple-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        # Body class still added so future rules can target it.
        assert "cas-flourish-none" in layout
        # But no flourish CSS rules emitted.
        assert "Flourish set: none" not in layout
        # Must NOT bleed in expressive/literary rules.
        assert "cas-drop-cap" not in layout
        assert "cas-fade-in" not in layout

    def test_event_led_expressive_flourish_emits_gradient_text_and_glow(self):
        from compose.build import emit_layout_files
        rs = self._build("event-led")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "cas-flourish-expressive" in layout
        # Hero h1 gradient-text rule
        assert "background-clip: text" in layout
        # CTA hover glow ring (color-mix box-shadow)
        assert "color-mix(in oklab, var(--color-primary)" in layout

    def test_body_carries_both_template_and_flourish_classes(self):
        """The body element must wear both cas-template-<id> and
        cas-flourish-<id> classes so family-scoped overrides + flourish
        rules both target correctly."""
        from compose.build import emit_layout_files
        rs = self._build("editorial-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert '<body class="cas-template-editorial-statement cas-flourish-literary">' in layout


# ============================================================
# Font-pairs registry expansion
# ============================================================

class TestFontPairsExpansion:
    """compose/data/font-pairs.json gained 9 new pairs to give Haven
    a wider design palette. Each pair MUST have id/display/body/mood/
    google_fonts_url; the loader contract doesn't change."""

    def test_total_count_after_expansion(self):
        from compose.build import load_font_pairs
        pairs = load_font_pairs().get("font_pairs", [])
        assert len(pairs) >= 18, f"expected >=18 font pairs, got {len(pairs)}"

    def test_the_nine_added_font_pairs_are_present(self):
        from compose.build import load_font_pairs
        ids = {p["id"] for p in load_font_pairs().get("font_pairs", [])}
        for new_id in (
            "fraunces-inter",
            "dm-serif-display-dm-sans",
            "space-grotesk-space-mono",
            "syne-manrope",
            "ibm-plex-serif-ibm-plex-sans",
            "crimson-pro-jost",
            "bricolage-grotesque-inter",
            "anton-merriweather",
            "spectral-jetbrains-mono",
        ):
            assert new_id in ids, f"new font pair {new_id!r} missing from registry"

    def test_every_pair_has_required_shape(self):
        """Every pair must declare: id, display.family, body.family, mood,
        google_fonts_url. compose/build.py emits CSS using these fields."""
        from compose.build import load_font_pairs
        for p in load_font_pairs().get("font_pairs", []):
            assert "id" in p
            assert "display" in p and "family" in p["display"]
            assert "body" in p and "family" in p["body"]
            assert "mood" in p
            assert "google_fonts_url" in p
            assert p["google_fonts_url"].startswith("https://fonts.googleapis.com")

    def test_newsreader_publicsans_pair_registered(self):
        """Newsreader + Public Sans pair (letterpress imprint feel for
        minimal/letterpress builds). Display = Newsreader (optical-sizing + true italic),
        body = Public Sans (editorial sans without Inter over-exposure).
        Google Fonts URL must include the optical-sizing axis on Newsreader
        and italic so emit_layout_files can render the italic form."""
        from compose.build import load_font_pairs
        pairs = {p["id"]: p for p in load_font_pairs().get("font_pairs", [])}
        assert "newsreader-publicsans" in pairs, (
            "newsreader-publicsans pair missing from registry"
        )
        pair = pairs["newsreader-publicsans"]
        assert pair["display"]["family"] == "Newsreader"
        assert pair["display"].get("is_serif") is True
        assert pair["body"]["family"] == "Public Sans"
        assert "Newsreader" in pair["google_fonts_url"]
        assert "Public+Sans" in pair["google_fonts_url"]
        assert "opsz" in pair["google_fonts_url"], (
            "Newsreader needs the optical-sizing axis to read like a "
            "letterpress imprint at hero scale"
        )
        assert "ital" in pair["google_fonts_url"], (
            "Newsreader's true italic is the reason this pair exists"
        )

    def test_newsreader_publicsans_in_targeted_archetype_suggestions(self):
        """The newsreader-publicsans pair must be in default_font_pair_suggestions
        for tradesperson-contractor + local-service + therapist-counselor +
        healer-wellness so the deterministic rotation (or Haven's hint) can land
        on it for minimal/letterpress builds."""
        from compose.build import load_archetypes
        by_id = {a["id"]: a for a in load_archetypes().get("archetypes", [])}
        for arch_id in (
            "tradesperson-contractor",
            "local-service",
            "therapist-counselor",
            "healer-wellness",
        ):
            suggestions = by_id[arch_id].get("default_font_pair_suggestions") or []
            assert "newsreader-publicsans" in suggestions, (
                f"{arch_id} default_font_pair_suggestions missing newsreader-publicsans"
            )


# ============================================================
# Regression: bug-coach-simple-statement-home-500 (2026-04-30)
# ============================================================

class TestTestimonialsStackDefensiveHeadingGuard:
    """When the home layout pulls testimonials via `db:testimonials` source +
    `spec_overlay: "spec.home"` AND the spec has no `spec.home.testimonials`
    sub-block (the case for coach-consultant + simple-statement / editorial-
    statement family combos: simple-statement has empty layout_overrides so
    it falls through to coach-home-editorial which uses db:testimonials with
    spec_overlay=spec.home), `emit_page_view` produces a widget include with
    ONLY items + detail_path_prefix + expand_inline -- no heading/eyebrow.

    The widget MUST have `typeof heading !== 'undefined'` guards or the home
    route 500s with `ReferenceError: heading is not defined`.

    This test asserts:
      1. `coach-consultant + simple-statement` produces a home.ejs that
         includes testimonials-stack with NO heading key in the data.
      2. The emitted testimonials/stack.ejs widget contains the defensive
         guards (typeof heading + typeof eyebrow checks).
      3. The widget renders without crashing when included with no heading
         (i.e. the EJS does not reference a bare `heading` variable
         outside a guard).
    """

    def test_coach_simple_statement_home_emits_testimonials_without_heading(self, tmp_path):
        """Reproduce the exact emit shape that triggered the prod 500."""
        import json as _json
        from compose.intake import scaffold_spec
        from compose.build import build_app
        spec = scaffold_spec({
            "app_name": "regress-coach-simple",
            "archetype": "coach-consultant",
            "site_name": "Regress",
            "admin_email": "a@x.example",
            "_harbor_meta": {"template_family_hint": "simple-statement"},
        })
        build_dir = tmp_path / "out"
        build_app(spec, build_dir)
        home_ejs = (build_dir / "views" / "home.ejs").read_text()
        # Find the testimonials/stack.ejs include line + assert it has
        # no `"heading"` key in the data literal (this is the shape that
        # used to crash).
        import re
        match = re.search(
            r"include\('\./widgets/testimonials/stack\.ejs',\s*(\{[^}]*\})\)",
            home_ejs,
        )
        assert match is not None, \
            "testimonials/stack.ejs include not found in coach+simple home.ejs"
        data_literal = match.group(1)
        # The data passed has items + detail_path_prefix + expand_inline,
        # but NO heading/eyebrow. This is the exact shape that crashed.
        assert '"heading"' not in data_literal, \
            f"emit produced heading key unexpectedly; data: {data_literal}"
        assert '"eyebrow"' not in data_literal, \
            f"emit produced eyebrow key unexpectedly; data: {data_literal}"

    def test_testimonials_stack_widget_guards_heading(self):
        """The widget source MUST guard heading + eyebrow with typeof
        checks. Without the guard the whole widget references a bare
        `heading` variable and EJS crashes when heading isn't passed."""
        from pathlib import Path
        widget = Path(__file__).resolve().parent.parent / "widgets" / "testimonials" / "stack.ejs"
        src = widget.read_text()
        # Must have both typeof guards.
        assert "typeof heading !== 'undefined'" in src, \
            "testimonials/stack.ejs missing typeof heading guard"
        assert "typeof eyebrow !== 'undefined'" in src, \
            "testimonials/stack.ejs missing typeof eyebrow guard"
        # Must NOT have a bare `_accent(heading)` reference outside a
        # typeof-guarded scope. (The fixed widget uses a local _heading
        # variable that's already defaulted.)
        assert "_accent(heading)" not in src, \
            "testimonials/stack.ejs still contains bare heading reference; " \
            "must use the locally-guarded _heading variable instead"


# ============================================================
# Quiet "made with Craft & Ship" footer attribution
# (v2-mw-quiet-craft-and-ship-footer-attribution, 2026-05-03)
# ============================================================

class TestCasFooterAttribution:
    """The shared shell/cas-attribution.ejs partial must be emitted as a
    widget, every footer family must include it, the lighthouse SVG must
    ship under public/brand/, and the .cas-footer-attribution CSS class
    must be defined so the pill renders as designed.
    """

    def _build_resolved(self, template_id: str = ""):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        answers = {
            "app_name": "attr-test", "archetype": "healer-wellness",
            "site_name": "T", "admin_email": "a@x.example",
        }
        if template_id:
            answers["template_family_id"] = template_id
        spec = scaffold_spec(answers)
        return resolve_spec(BuildSpec.from_dict(spec))

    def test_cas_attribution_partial_emitted_as_widget(self):
        """emit_widget_files copies the entire compose/widgets/ tree;
        the shared attribution partial must be present so each footer
        family's include() resolves at runtime."""
        from compose.build import emit_widget_files
        rs = self._build_resolved()
        files = dict(emit_widget_files(rs))
        assert "views/widgets/shell/cas-attribution.ejs" in files
        body = files["views/widgets/shell/cas-attribution.ejs"]
        assert 'href="https://craftandship.io"' in body
        assert 'target="_blank"' in body
        assert 'rel="noopener"' in body
        assert "/brand/cas-lighthouse.svg" in body
        assert "show_cas_attribution" in body, \
            "partial must respect the show_cas_attribution suppression flag"
        # No em dash in the customer-facing copy.
        assert "\u2014" not in body
        assert "&mdash;" not in body

    def test_classic_footer_includes_cas_attribution(self):
        from compose.build import emit_widget_files
        rs = self._build_resolved("classic-business")
        files = dict(emit_widget_files(rs))
        footer = files["views/widgets/shell/footer.ejs"]
        assert "include('./cas-attribution.ejs'" in footer

    def test_minimal_line_footer_includes_cas_attribution(self):
        from compose.build import emit_widget_files
        rs = self._build_resolved("editorial-statement")
        files = dict(emit_widget_files(rs))
        footer = files["views/widgets/shell/footer-minimal-line.ejs"]
        assert "include('./cas-attribution.ejs'" in footer

    def test_grid_five_col_footer_includes_cas_attribution(self):
        from compose.build import emit_widget_files
        rs = self._build_resolved("directory-rich")
        files = dict(emit_widget_files(rs))
        footer = files["views/widgets/shell/footer-grid-five-col.ejs"]
        assert "include('./cas-attribution.ejs'" in footer

    def test_single_line_footer_includes_cas_attribution(self):
        from compose.build import emit_widget_files
        rs = self._build_resolved("simple-statement")
        files = dict(emit_widget_files(rs))
        footer = files["views/widgets/shell/footer-single-line.ejs"]
        assert "include('./cas-attribution.ejs'" in footer

    def test_classic_footer_no_longer_carries_legacy_powered_by_block(self):
        """The legacy inline 'Powered by Craft & Ship' line in shell/footer.ejs
        is replaced by the shared partial. We check the original legacy
        markup is gone so we don't render two attributions side-by-side."""
        from compose.build import emit_widget_files
        rs = self._build_resolved("classic-business")
        footer = dict(emit_widget_files(rs))["views/widgets/shell/footer.ejs"]
        assert "Powered by" not in footer

    def test_lighthouse_svg_emitted_under_public_brand(self):
        """The footer attribution links to /brand/cas-lighthouse.svg.
        emit_public_assets must ship that file."""
        from compose.build import emit_public_assets
        rs = self._build_resolved()
        files = dict(emit_public_assets(rs))
        assert "public/brand/cas-lighthouse.svg" in files
        svg = files["public/brand/cas-lighthouse.svg"]
        assert isinstance(svg, str)
        assert svg.startswith("<svg") or svg.lstrip().startswith("<svg")
        # Must be the lighthouse mark, not a placeholder.
        assert 'aria-label="Craft and Ship"' in svg
        # Must be valid XML (no bare ampersand).
        import xml.etree.ElementTree as ET
        ET.fromstring(svg)

    def test_cas_footer_attribution_css_defined(self):
        """style.css must define the .cas-footer-attribution pill so the
        partial renders as designed (italic display-serif 12-14px hover-pill
        in ink-mute). Font inherits the site's --font-display so it stays
        consistent with the actual loaded display font instead of forcing
        Cormorant Garamond, which is no longer globally loaded."""
        from compose.build import emit_public_assets
        rs = self._build_resolved()
        files = dict(emit_public_assets(rs))
        css = files["public/style.css"]
        assert ".cas-footer-attribution" in css
        assert ".cas-footer-attribution:hover" in css
        assert ".cas-footer-attribution-text" in css
        assert ".cas-footer-attribution-mark" in css
        assert ".cas-footer-attribution-name" in css
        # Attribution must use the site's display font, not a hardcoded
        # Cormorant that may not be loaded; falls back to Georgia.
        assert "var(--font-display, Georgia)" in css
        assert (
            'font-family: "Cormorant Garamond"' not in css
        ), "attribution must not hardcode Cormorant Garamond (regression: v2-footer-attribution-font-mismatch)"

    def test_attribution_partial_renders_via_ejs(self):
        """End-to-end ejs render of the attribution partial. Confirms the
        partial is syntactically valid EJS and that `show_cas_attribution`
        gating actually suppresses output when set false (string OR bool)."""
        import json as _json
        import os
        import shutil
        import subprocess
        from compose.build import COMPOSE_DIR, WIDGET_DIR
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not on PATH")
        helper = COMPOSE_DIR / "tests" / "_ejs_render.js"
        if not helper.exists():
            pytest.skip("ejs render helper not present")
        partial = WIDGET_DIR / "shell" / "cas-attribution.ejs"

        # The helper requires `ejs`, which web/ installs (`cd web && npm ci`).
        node_env = {**os.environ, "NODE_PATH": str(COMPOSE_DIR.parent / "web" / "node_modules")}

        def _run(payload):
            return subprocess.run(
                [node, str(helper), str(partial), _json.dumps(payload)],
                capture_output=True, text=True, env=node_env,
            )

        # Default (no settings) should show.
        r = _run({"settings": {}})
        if r.returncode == 2:
            pytest.skip("web/node_modules/ejs is not installed (run `cd web && npm ci`)")
        assert r.returncode == 0, r.stderr
        assert "craftandship.io" in r.stdout
        assert "made with" in r.stdout
        # Bool false suppresses.
        r = _run({"settings": {"show_cas_attribution": False}})
        assert r.returncode == 0, r.stderr
        assert "craftandship.io" not in r.stdout
        # String "false" (DB-stored) also suppresses.
        r = _run({"settings": {"show_cas_attribution": "false"}})
        assert r.returncode == 0, r.stderr
        assert "craftandship.io" not in r.stdout


# ============================================================
# Editorial Sanctuary template family
# (v2-mw-sanctuary-palette-template-family, 2026-05-04)
# ============================================================


class TestSanctuaryPalette:
    """The 'sanctuary' palette ports MW v25's four-token deep-teal +
    sage + warm-gold + paper-cream system into the v2 palette registry.
    Customers building wellness/healer/coach archetypes can pick it
    via _harbor_meta.palette_hint='sanctuary'.
    """

    def test_sanctuary_palette_present(self):
        from compose.build import load_palettes
        ids = {p["id"] for p in load_palettes().get("palettes", [])}
        assert "sanctuary" in ids

    def test_sanctuary_palette_uses_paper_cream_background(self):
        """MW Sanctuary's signature is paper background, not white. The
        palette's --color-background MUST be the cream tone (#efe8dc),
        not #ffffff."""
        from compose.build import load_palettes
        sanctuary = next(
            p for p in load_palettes().get("palettes", [])
            if p["id"] == "sanctuary"
        )
        assert sanctuary["vars"]["--color-background"].lower() == "#efe8dc"
        assert sanctuary["vars"]["--color-surface"].lower() == "#f1ecdf"

    def test_sanctuary_palette_uses_deep_teal_ink(self):
        from compose.build import load_palettes
        sanctuary = next(
            p for p in load_palettes().get("palettes", [])
            if p["id"] == "sanctuary"
        )
        assert sanctuary["vars"]["--color-foreground"].lower() == "#1e4040"
        assert sanctuary["vars"]["--color-primary"].lower() == "#1e4040"

    def test_sanctuary_palette_carries_sage_and_warm_gold_accents(self):
        """The MW palette has TWO accent tokens (sage + warm gold)
        beyond the deep-teal primary. They land on --color-accent-earth
        (sage) + --color-accent-warm (warm gold) so widgets that read
        those vars (eyebrow dot, badge ring, etc.) get the full
        sanctuary chord."""
        from compose.build import load_palettes
        sanctuary = next(
            p for p in load_palettes().get("palettes", [])
            if p["id"] == "sanctuary"
        )
        assert sanctuary["vars"]["--color-accent-earth"].lower() == "#7ba27e"
        assert sanctuary["vars"]["--color-accent-warm"].lower() == "#c8b390"

    def test_sanctuary_palette_compatible_archetypes(self):
        from compose.build import load_palettes
        sanctuary = next(
            p for p in load_palettes().get("palettes", [])
            if p["id"] == "sanctuary"
        )
        for arch in ("healer-wellness", "coach-consultant", "therapist-counselor"):
            assert arch in sanctuary["archetypes"], (
                f"sanctuary palette must list {arch} as a compatible archetype"
            )


class TestCormorantItalicInterTightFontPair:
    """The 'cormorant-italic-inter-tight' font pair powers the Sanctuary
    family. Distinct from the existing 'cormorant-lato' pair: this one
    pairs Cormorant 300 italic display with Inter Tight body and matches
    the MW v25 font registry exactly.
    """

    def test_pair_present(self):
        from compose.build import load_font_pairs
        ids = {p["id"] for p in load_font_pairs().get("font_pairs", [])}
        assert "cormorant-italic-inter-tight" in ids

    def test_pair_loads_cormorant_garamond_and_inter_tight(self):
        from compose.build import load_font_pairs
        pair = next(
            p for p in load_font_pairs().get("font_pairs", [])
            if p["id"] == "cormorant-italic-inter-tight"
        )
        assert pair["display"]["family"] == "Cormorant Garamond"
        assert pair["display"]["is_serif"] is True
        assert pair["body"]["family"] == "Inter Tight"

    def test_pair_includes_italic_300_in_google_url(self):
        """MW Sanctuary specifically uses italic Cormorant 300 weight.
        The Google Fonts URL MUST request italic axis (1,300) so the
        face is actually downloaded; otherwise the browser falls back
        to faux-italic which looks wrong."""
        from compose.build import load_font_pairs
        pair = next(
            p for p in load_font_pairs().get("font_pairs", [])
            if p["id"] == "cormorant-italic-inter-tight"
        )
        url = pair["google_fonts_url"]
        assert "Cormorant+Garamond" in url
        assert "Inter+Tight" in url
        # ital,wght axes with 1,300 means italic-300 is requested.
        assert "1,300" in url, (
            f"Google Fonts URL must request italic-300 Cormorant; got {url}"
        )


class TestEditorialSanctuaryFamilyRegistry:
    """The 'editorial-sanctuary' family is the 6th starter family.
    It must declare the Sanctuary palette/typography contract and reuse
    the existing footer-minimal-line shell with a new dedicated
    nav-sanctuary partial.
    """

    def test_editorial_sanctuary_family_exists(self):
        from compose.build import get_template_family
        f = get_template_family("editorial-sanctuary")
        assert f is not None
        assert f["shell_nav_id"] == "shell-nav-sanctuary"
        assert f["shell_footer_id"] == "shell-footer-minimal-line"

    def test_editorial_sanctuary_compatible_archetypes(self):
        from compose.build import get_template_family
        f = get_template_family("editorial-sanctuary")
        assert "*" not in f["compatible_archetypes"]
        for arch in ("healer-wellness", "coach-consultant", "therapist-counselor"):
            assert arch in f["compatible_archetypes"]

    def test_editorial_sanctuary_typography_scale_signals_serif_italic(self):
        from compose.build import get_template_family
        ts = get_template_family("editorial-sanctuary")["typography_scale"]
        assert ts["base_rem"] >= 1.1
        assert ts["h1_rem"] >= 4.0
        assert ts["leading_h1"] <= 1.1

    def test_editorial_sanctuary_extra_body_css_emits_italic_headings(self):
        """The italic-Cormorant signature MUST live in extra_body_css so
        every widget's <h1>/<h2>/<h3> picks it up via cascade."""
        from compose.build import get_template_family
        css = get_template_family("editorial-sanctuary").get("extra_body_css") or ""
        assert "font-style: italic" in css
        assert "body.cas-template-editorial-sanctuary h1" in css

    def test_editorial_sanctuary_extra_body_css_emits_paper_card_pattern(self):
        from compose.build import get_template_family
        css = get_template_family("editorial-sanctuary").get("extra_body_css") or ""
        assert ".cas-paper-card" in css
        assert "border: 1px solid var(--color-border)" in css

    def test_editorial_sanctuary_extra_body_css_emits_eyebrow_dot(self):
        """MW Sanctuary's eyebrow has a sage circular dot prefix. The
        Sanctuary family overrides .cas-eyebrow / .cas-eye to draw that
        dot via ::before, so existing widgets that emit a .cas-eyebrow
        span automatically pick up the new look."""
        from compose.build import get_template_family
        css = get_template_family("editorial-sanctuary").get("extra_body_css") or ""
        assert ".cas-eyebrow" in css or ".cas-eye" in css
        assert "border-radius: 50%" in css

    def test_editorial_sanctuary_extra_body_css_emits_deep_teal_cta_strip(self):
        from compose.build import get_template_family
        css = get_template_family("editorial-sanctuary").get("extra_body_css") or ""
        assert ".cas-cta-strip" in css
        assert "var(--color-primary)" in css

    def test_editorial_sanctuary_uses_literary_flourish(self):
        from compose.build import get_template_family
        f = get_template_family("editorial-sanctuary")
        assert f.get("flourish_set") == "literary"


class TestSanctuaryNavPartial:
    """The new shell-nav-sanctuary partial is registered AND lives on
    disk. Re-uses the existing minimal-line footer (no new footer
    partial needed; the cream paper background carries through)."""

    def test_nav_sanctuary_in_widget_registry(self):
        from compose.build import load_widgets
        widgets = load_widgets().get("widgets", {})
        assert "shell-nav-sanctuary" in widgets
        assert (
            widgets["shell-nav-sanctuary"]["file"]
            == "widgets/shell/nav-sanctuary.ejs"
        )

    def test_nav_sanctuary_file_on_disk(self):
        from pathlib import Path
        path = (
            Path(__file__).resolve().parent.parent
            / "widgets" / "shell" / "nav-sanctuary.ejs"
        )
        assert path.exists(), f"missing nav-sanctuary partial at {path}"
        body = path.read_text()
        # Italic Cormorant signature: nav links MUST be italic in inline
        # style so the family shell looks distinct from nav-thin-statement
        # (which is uppercase tracked-out).
        assert "font-style: italic" in body
        assert "settings.site_name" in body


class TestSanctuaryPickerWiring:
    """The picker honors `template_family_hint='editorial-sanctuary'` for
    every compatible archetype, AND mood-keyword scoring promotes the
    family when the keeper signals for paper/sanctuary aesthetics."""

    def test_hint_is_honored_for_healer(self):
        from compose.intake import pick_template_family
        out = pick_template_family(
            "healer-wellness",
            {"template_family_hint": "editorial-sanctuary"},
            seed="any",
        )
        assert out == "editorial-sanctuary"

    def test_hint_is_honored_for_coach(self):
        from compose.intake import pick_template_family
        out = pick_template_family(
            "coach-consultant",
            {"template_family_hint": "editorial-sanctuary"},
            seed="any",
        )
        assert out == "editorial-sanctuary"

    def test_hint_falls_through_for_incompatible_archetype(self):
        """editorial-sanctuary is only compatible with healer/coach/
        therapist. Hinting from fitness-studio MUST NOT pick it."""
        from compose.intake import pick_template_family
        out = pick_template_family(
            "fitness-studio",
            {"template_family_hint": "editorial-sanctuary"},
            seed="any",
        )
        assert out != "editorial-sanctuary"

    def test_sanctuary_mood_keywords_promote_family(self):
        """Strong sanctuary signal: 'sanctuary' + 'paper' + 'italic' +
        'still' all match the family's tags. With NO competing signals
        for editorial-statement, the sanctuary family should win."""
        from compose.intake import pick_template_family
        out = pick_template_family(
            "healer-wellness",
            {"mood_keywords": ["sanctuary", "paper", "italic", "warm", "still"]},
            seed="maya",
        )
        assert out == "editorial-sanctuary"


class TestEmitSanctuaryLayout:
    """The full emit_layout_files pipeline must produce a layout that
    references the new nav partial AND injects the Sanctuary
    extra_body_css. This is the customer-facing acceptance criterion:
    a healer build with template_family_id='editorial-sanctuary'
    actually emits the tokens + classes from the family's CSS."""

    def _build_resolved(self):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name": "sanctuary-test",
            "archetype": "healer-wellness",
            "site_name": "Quiet Practice",
            "admin_email": "a@x.example",
            "template_family_id": "editorial-sanctuary",
        })
        return resolve_spec(BuildSpec.from_dict(spec))

    def test_emits_nav_sanctuary_include(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "include('./widgets/shell/nav-sanctuary'" in layout

    def test_emits_minimal_line_footer_include(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "include('./widgets/shell/footer-minimal-line'" in layout

    def test_emits_template_body_class(self):
        import re
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        m = re.search(r'<body class="([^"]*)">', layout)
        assert m is not None
        assert "cas-template-editorial-sanctuary" in m.group(1).split()

    def test_emits_italic_heading_override_css(self):
        """The acceptance criterion: the emitted layout MUST contain the
        body-scoped italic-heading rule so any widget's <h1>/<h2>
        becomes italic Cormorant without authoring per-widget changes."""
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "body.cas-template-editorial-sanctuary h1" in layout
        # The italic comes from extra_body_css, not from typography_scale.
        assert "font-style: italic" in layout

    def test_emits_paper_card_class(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "cas-paper-card" in layout

    def test_emits_cta_strip_class(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "cas-cta-strip" in layout

    def test_no_unresolved_placeholders(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved()
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "__SHELL_NAV_FILE__" not in layout
        assert "__SHELL_FOOTER_FILE__" not in layout
        assert "__TYPOGRAPHY_SCALE_CSS__" not in layout
        assert "__TYPOGRAPHY_SCALE_CLASSES__" not in layout


# ============================================================
# Italic-eyebrow-with-sage-dot pattern (item
# v2-mw-italic-eyebrow-with-sage-dot, 2026-05-04). Adds an
# optional `eyebrow_style: "italic_serif"` token on template
# families. When set, emit_layout_files emits both a
# .cas-eyebrow-italic utility class (for new widgets) AND a
# body-class-scoped override on the legacy .cas-eyebrow /
# .eyebrow spans so existing widgets pick up the editorial
# pattern automatically. Default (`uppercase_dim` or unset)
# keeps the legacy uppercase-tracked eyebrow.
# ============================================================

class TestEyebrowStyleToken:
    """The eyebrow_style token on template families is optional and
    drives whether emit_layout_files appends italic-serif eyebrow CSS."""

    def test_editorial_sanctuary_declares_italic_serif(self):
        from compose.build import get_template_family
        f = get_template_family("editorial-sanctuary")
        assert f.get("eyebrow_style") == "italic_serif"

    def test_classic_business_does_not_declare_italic(self):
        """Classic should not opt into the italic style; default applies."""
        from compose.build import get_template_family
        f = get_template_family("classic-business")
        assert f.get("eyebrow_style") in (None, "uppercase_dim")

    def test_directory_rich_does_not_declare_italic(self):
        from compose.build import get_template_family
        f = get_template_family("directory-rich")
        assert f.get("eyebrow_style") in (None, "uppercase_dim")


class TestEyebrowStyleEmit:
    """When a family declares eyebrow_style: italic_serif, the emitted
    layout MUST include the .cas-eyebrow-italic utility class AND a
    body.cas-template-<id> .cas-eyebrow override that flips existing
    eyebrow spans to italic-with-sage-dot."""

    def _build_resolved(self, template_id: str):
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec
        spec = scaffold_spec({
            "app_name": "italic-eyebrow-test",
            "archetype": "healer-wellness",
            "site_name": "T",
            "admin_email": "a@x.example",
            "template_family_id": template_id,
        })
        return resolve_spec(BuildSpec.from_dict(spec))

    def test_italic_serif_emits_utility_class(self):
        from compose.build import emit_layout_files
        rs = self._build_resolved("editorial-sanctuary")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert ".cas-eyebrow-italic {" in layout
        assert "font-style: italic" in layout

    def test_italic_serif_utility_includes_dot_via_pseudo_element(self):
        """The pattern is a sage circular dot via ::before pseudo-element,
        not an inline element. Check the dot exists on the utility AND
        uses border-radius: 50% to be circular."""
        from compose.build import emit_layout_files
        rs = self._build_resolved("editorial-sanctuary")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert ".cas-eyebrow-italic::before" in layout
        assert "border-radius: 50%" in layout
        # The dot reads from --color-accent-earth (sage in the sanctuary
        # palette) with primary as a defensive fallback.
        assert "var(--color-accent-earth" in layout

    def test_italic_serif_emits_body_class_scoped_override(self):
        """Existing widgets emitting .cas-eyebrow or .eyebrow upgrade
        automatically when the family selects italic_serif."""
        from compose.build import emit_layout_files
        rs = self._build_resolved("editorial-sanctuary")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert "body.cas-template-editorial-sanctuary .cas-eyebrow" in layout

    def test_default_family_does_not_emit_italic_eyebrow_utility(self):
        """A family without eyebrow_style (or with uppercase_dim) MUST
        NOT emit the .cas-eyebrow-italic class. The static .cas-eyebrow
        uppercase rule lives in CAS_BASE_STYLES and is the default."""
        from compose.build import emit_layout_files
        rs = self._build_resolved("classic-business")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        assert ".cas-eyebrow-italic" not in layout

    def test_unset_eyebrow_style_does_not_break_emission(self):
        """A family with no eyebrow_style key emits a valid layout."""
        from compose.build import emit_layout_files
        # editorial-statement does NOT declare eyebrow_style.
        rs = self._build_resolved("editorial-statement")
        layout = dict(emit_layout_files(rs))["views/layout-public.ejs"]
        # No crash, no italic-eyebrow utility, no leftover placeholder.
        assert ".cas-eyebrow-italic" not in layout
        assert "__TYPOGRAPHY_SCALE_CLASSES__" not in layout


# ============================================================
# Public service-list thumbnail conditional (2026-05-07)
# ============================================================
# Roadmap: v2-public-service-list-thumbnail-conditional. Adds optional
# image rendering to offerings-grid-3col + new services-editorial-table
# sibling layout for tradesperson-contractor + local-service so a
# minimal/spare hint switches them to a text-first layout where each row
# only carries a 64px thumbnail when image_url is present.

class TestServicesEditorialTableLayouts:
    def test_local_service_default_has_variant_group(self):
        layouts = load_layouts().get("layouts", {})
        default = layouts["local-service-services-default"]
        assert default.get("variant_group") == "local-service-services"
        assert default.get("variant_id") == "default"

    def test_local_service_editorial_table_sibling_exists(self):
        layouts = load_layouts().get("layouts", {})
        sibling = layouts["local-service-services-editorial-table"]
        assert sibling["archetype"] == "local-service"
        assert sibling["page"] == "services"
        assert sibling["variant_group"] == "local-service-services"
        widget_ids = {w["id"] for w in sibling["widgets"]}
        assert "offerings-services-editorial-table" in widget_ids
        # Tags must include the keywords that drive selection.
        assert {"minimal", "spare"}.issubset(set(sibling["tags"]))

    def test_tradesperson_default_has_variant_group(self):
        layouts = load_layouts().get("layouts", {})
        default = layouts["tradesperson-services-default"]
        assert default["archetype"] == "tradesperson-contractor"
        assert default["page"] == "services"
        assert default.get("variant_group") == "tradesperson-services"
        assert default.get("variant_id") == "default"

    def test_tradesperson_editorial_table_sibling_exists(self):
        layouts = load_layouts().get("layouts", {})
        sibling = layouts["tradesperson-services-editorial-table"]
        assert sibling["archetype"] == "tradesperson-contractor"
        assert sibling["page"] == "services"
        assert sibling["variant_group"] == "tradesperson-services"
        widget_ids = {w["id"] for w in sibling["widgets"]}
        assert "offerings-services-editorial-table" in widget_ids
        assert {"minimal", "spare"}.issubset(set(sibling["tags"]))

    def test_tradesperson_archetype_points_at_own_default(self):
        from compose.build import get_archetype
        arche = get_archetype("tradesperson-contractor")
        assert arche["page_layouts"]["services"] == "tradesperson-services-default"

    def test_minimal_hint_picks_local_service_editorial_table(self):
        out = pick_layout_variant(
            "local-service", "services",
            {"mood_keywords": ["minimal", "spare"]},
            seed="any",
        )
        assert out == "local-service-services-editorial-table"

    def test_minimal_hint_picks_tradesperson_editorial_table(self):
        out = pick_layout_variant(
            "tradesperson-contractor", "services",
            {"mood_keywords": ["minimal", "spare"]},
            seed="any",
        )
        assert out == "tradesperson-services-editorial-table"

    def test_explicit_layout_variant_hint_wins(self):
        out = pick_layout_variant(
            "local-service", "services",
            {"layout_variant_hint": "local-service-services-editorial-table"},
            seed="any",
        )
        assert out == "local-service-services-editorial-table"

    def test_no_hint_does_not_force_editorial_table(self):
        # The default layout has no penalizing tags; with no signal,
        # tie-break by stable_index. We just assert the picker returns
        # a real candidate.
        out = pick_layout_variant(
            "local-service", "services", {}, seed="any",
        )
        assert out in {
            "local-service-services-default",
            "local-service-services-editorial-table",
        }

    def test_resolve_spec_swaps_local_service_to_editorial_table(self):
        spec = scaffold_spec({
            "app_name": "minimal-cleaner", "archetype": "local-service",
            "site_name": "Minimal Cleaner", "admin_email": "m@x.example",
            "_harbor_meta": {"mood_keywords": ["minimal", "spare", "editorial"]},
        })
        b = BuildSpec.from_dict(spec)
        r = resolve_spec(b)
        layout = r.page_layouts.get("services")
        assert layout is not None
        assert layout.get("variant_id") == "editorial-table"

    def test_resolve_spec_swaps_tradesperson_to_editorial_table(self):
        spec = scaffold_spec({
            "app_name": "sample-irrigation", "archetype": "tradesperson-contractor",
            "site_name": "Sample Irrigation Co", "admin_email": "s@x.example",
            "_harbor_meta": {"mood_keywords": ["minimal", "spare"]},
        })
        b = BuildSpec.from_dict(spec)
        r = resolve_spec(b)
        layout = r.page_layouts.get("services")
        assert layout is not None
        assert layout.get("variant_id") == "editorial-table"

    def test_healer_default_has_variant_group(self):
        layouts = load_layouts().get("layouts", {})
        default = layouts["healer-offerings-default"]
        assert default["archetype"] == "healer-wellness"
        assert default["page"] == "work-with-me"
        assert default.get("variant_group") == "healer-services"
        assert default.get("variant_id") == "default"

    def test_healer_editorial_table_sibling_exists(self):
        layouts = load_layouts().get("layouts", {})
        sibling = layouts["healer-offerings-editorial-table"]
        assert sibling["archetype"] == "healer-wellness"
        assert sibling["page"] == "work-with-me"
        assert sibling["variant_group"] == "healer-services"
        widget_ids = {w["id"] for w in sibling["widgets"]}
        assert "offerings-services-editorial-table" in widget_ids
        assert {"minimal", "spare", "quiet"}.issubset(set(sibling["tags"]))

    def test_healer_archetype_points_at_own_default(self):
        from compose.build import get_archetype
        arche = get_archetype("healer-wellness")
        assert arche["page_layouts"]["work-with-me"] == "healer-offerings-default"

    def test_minimal_hint_picks_healer_editorial_table(self):
        out = pick_layout_variant(
            "healer-wellness", "work-with-me",
            {"mood_keywords": ["minimal", "spare"]},
            seed="any",
        )
        assert out == "healer-offerings-editorial-table"

    def test_quiet_hint_picks_healer_editorial_table(self):
        # The "quiet" mood keyword (added 2026-05-07) must steer the
        # healer work-with-me page to the editorial-table sibling.
        out = pick_layout_variant(
            "healer-wellness", "work-with-me",
            {"mood_keywords": ["quiet"]},
            seed="any",
        )
        assert out == "healer-offerings-editorial-table"

    def test_quiet_hint_picks_local_service_editorial_table(self):
        out = pick_layout_variant(
            "local-service", "services",
            {"mood_keywords": ["quiet"]},
            seed="any",
        )
        assert out == "local-service-services-editorial-table"

    def test_quiet_hint_picks_tradesperson_editorial_table(self):
        out = pick_layout_variant(
            "tradesperson-contractor", "services",
            {"mood_keywords": ["quiet"]},
            seed="any",
        )
        assert out == "tradesperson-services-editorial-table"

    def test_resolve_spec_swaps_healer_to_editorial_table(self):
        spec = scaffold_spec({
            "app_name": "quiet-healer", "archetype": "healer-wellness",
            "site_name": "Quiet Healer", "admin_email": "q@x.example",
            "_harbor_meta": {"mood_keywords": ["minimal", "spare", "quiet"]},
        })
        b = BuildSpec.from_dict(spec)
        r = resolve_spec(b)
        layout = r.page_layouts.get("work-with-me")
        assert layout is not None
        assert layout.get("variant_id") == "editorial-table"


class TestOfferingsGridThumbnailRendering:
    """grid-3col widget MUST render an image when item.image_url is
    present, AND MUST NOT render an <img> tag when image_url is absent
    (text-only card chrome). Also accepts photo_url + cover_url aliases
    matching the resource-list-card-grid convention.
    """

    def _read(self):
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "widgets" / "offerings" / "grid-3col.ejs"
        return p.read_text()

    def test_widget_emits_image_tag_with_image_url(self):
        src = self._read()
        assert "item.image_url" in src
        # The image is rendered inside an <img> tag with lazy loading.
        assert "<img loading=\"lazy\"" in src

    def test_widget_image_render_is_conditional(self):
        src = self._read()
        # The image render is gated on the presence of an image url.
        assert "if (_itemImg)" in src

    def test_widget_accepts_photo_url_and_cover_url_aliases(self):
        src = self._read()
        assert "item.photo_url" in src
        assert "item.cover_url" in src


class TestOfferingsServicesEditorialTableWidget:
    """The new services-editorial-table widget renders a text-only row
    by default; when item.image_url is present it adds a 64px square
    thumbnail to the left of the title.
    """

    def _read(self):
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "widgets" / "offerings" / "services-editorial-table.ejs"
        return p.read_text()

    def test_widget_file_exists(self):
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "widgets" / "offerings" / "services-editorial-table.ejs"
        assert p.exists()

    def test_widget_registry_entry_present(self):
        from compose.build import get_widget
        w = get_widget("offerings-services-editorial-table")
        assert w["file"] == "widgets/offerings/services-editorial-table.ejs"

    def test_widget_renders_thumbnail_at_64px(self):
        src = self._read()
        # The thumbnail container is sized to 64x64.
        assert "width: 64px" in src and "height: 64px" in src

    def test_widget_thumbnail_is_conditional(self):
        src = self._read()
        # The thumbnail block is gated on the presence of an image url.
        assert "if (_itemImg)" in src
        assert "item.image_url" in src

    def test_widget_renders_title_and_price(self):
        src = self._read()
        assert "item.title" in src
        assert "item.price_display" in src

    def test_widget_renders_empty_when_no_items(self):
        src = self._read()
        # The whole section is wrapped in a length>0 guard so empty data
        # produces no markup.
        assert "_items.length > 0" in src


class TestVerticalRhythmClamp:
    """v2-template-vertical-rhythm-clamp. A flat
    --type-section-y constant (8rem on editorial-statement) stamped
    250-360px dead bands between every block; a home page can end up
    twice as tall as its content. The emitted variable must be
    a clamp capped at 4rem, the blanket section rule must scope to
    main > section (nested sections never double-pad), and footers must
    not stack their own margin-top on top of section padding."""

    def _layout(self, template_id: str) -> str:
        from compose.intake import scaffold_spec
        from compose.build import BuildSpec, resolve_spec, emit_layout_files
        answers = {
            "app_name": "rhythm-test", "archetype": "healer-wellness",
            "site_name": "T", "admin_email": "a@x.example",
            "template_family_id": template_id,
        }
        spec = scaffold_spec(answers)
        rs = resolve_spec(BuildSpec.from_dict(spec))
        return dict(emit_layout_files(rs))["views/layout-public.ejs"]

    def test_editorial_section_y_is_clamped_not_flat_8rem(self):
        layout = self._layout("editorial-statement")
        assert "--type-section-y: clamp(2.75rem, 5vw, 4.0rem);" in layout
        assert "--type-section-y: 8.0rem" not in layout

    def test_simple_statement_10rem_also_caps_at_4rem(self):
        layout = self._layout("simple-statement")
        assert "--type-section-y: clamp(2.75rem, 5vw, 4.0rem);" in layout
        assert "--type-section-y: 10.0rem" not in layout

    def test_small_declared_value_keeps_its_own_cap(self):
        # directory-rich declares 3.0rem; the clamp must not inflate it.
        layout = self._layout("directory-rich")
        assert "--type-section-y: clamp(2.75rem, 5vw, 3.0rem);" in layout

    def test_blanket_section_rule_scopes_to_main_child_sections(self):
        layout = self._layout("editorial-statement")
        assert (
            "body.cas-template-editorial-statement main > section {"
            " padding-top: var(--type-section-y);"
            " padding-bottom: var(--type-section-y); }"
        ) in layout
        # The unscoped form would double-pad nested sections.
        assert "body.cas-template-editorial-statement section { padding-top" not in layout

    def test_footer_partials_carry_no_own_margin_top(self):
        from pathlib import Path
        import re
        shell = Path(__file__).resolve().parents[1] / "widgets" / "shell"
        for name in ("footer-single-line.ejs", "footer-minimal-line.ejs",
                     "footer-grid-five-col.ejs"):
            src = (shell / name).read_text()
            m = re.search(r"<footer[^>]*>", src)
            assert m is not None, name
            assert "margin-top" not in m.group(0), (
                f"{name}: footer root must not add margin-top on top of "
                "section padding (hairline + section padding is enough)"
            )
