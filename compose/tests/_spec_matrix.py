"""Shared helper: the wrong-type matrix over everything the engine reads
from a spec.

Not a test module (compose/tests/test_spec_shape_matrix.py drives it).

The rule under test: for every position of a spec, whatever JSON value
stands there, the engine either REFUSES the spec (`validate_spec` returns
errors) or BUILDS a site in which every emitted script parses, every
emitted view compiles, and every GET route renders, for a visitor and for
the admin. Nothing in between: no exception out of an emitter, no page that
throws when it is requested.

A case is `(base, position, value)`:

  base      one of the sample fixtures with `EXTRAS` laid over it, so that
            the optional parts of a spec (custom pages of each kind, models
            with every field option, roles, navigation, hints, seed rows)
            are present and can be replaced;
  position  a key path into that spec: every dict key and every list
            element, containers included. Positions of the same shape (the
            same keys, any list index) are taken once: the first element of
            a list that has a key stands for the others;
  value     one of `WRONG_VALUES`.

`run_cases` composes each case in this process and hands the sites that
were built, and differ from their base, to `_site_check.check_sites`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from compose import build as B
from compose.tests import _site_check as SC

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FIXTURE_NAMES = sorted(p.name for p in FIXTURES.glob("sample-*-spec.json"))
V1_FIXTURE_NAMES = sorted(p.name for p in FIXTURES.glob("v1-*-spec.json"))
# Not a file: the v1 yoga fixture with V1_EXTRAS laid over it.
V1_RICH = "v1-rich"

# A string longer than any bound the gate puts on a single value, and one
# that is long for a title yet under every bound on free text.
HUGE_STRING = "very long " * 30_000
LONG_STRING = "long " * 4_000

# Stands for "the key (or the list element) is not there at all".
ABSENT = object()

# name -> value. Every JSON type, the empty and the non-empty container, the
# empty string, a string that is nobody's name (for the positions that hold
# an identifier or one of a fixed set of words), two valid column types (for
# the positions that hold one: the engine reads some columns with a fixed
# meaning, and a valid type is not always one it can work with), the two
# long strings, and the key removed.
WRONG_VALUES: Dict[str, Any] = {
    "integer": 5125550100,
    "fraction": 2.5,
    "true": True,
    "false": False,
    "null": None,
    "empty list": [],
    "list": ["a", 1, None],
    "empty object": {},
    "object": {"x": 1, "items": "y"},
    "empty string": "",
    "other string": "zzz not a name",
    # A column type that is a valid one, where another was written: text where
    # a timestamp was, and a whole number where text was.
    "text column type": "VARCHAR(40)",
    "number column type": "INTEGER",
    "long string": LONG_STRING,
    "huge string": HUGE_STRING,
    "absent": ABSENT,
}

# Laid over every fixture: one of each kind of thing a spec can declare
# beside its pages, with every key the engine reads there (found by running
# the engine over a spec whose objects record each lookup). `settings_extra`
# is merged into the fixture's own settings.
EXTRAS: Dict[str, Any] = {
    "custom_pages": [
        {
            "path": "/book", "title": "Book a session", "purpose": "Book a session with us.",
            "description": "Pick a time that suits you.", "form_target": "Booking",
            "submit_text": "Send request", "success_message": "Thank you. We will be in touch.",
            "allowed_fields": ["name", "email", "kind", "notes"], "auth_required": False,
        },
        {
            "path": "/members", "title": "Members", "purpose": "Member resources.",
            "auth_required": True, "allowed_roles": ["admin", "member"],
        },
        {"path": "/our-story", "title": "Our story", "description": "How we started.", "purpose": "About us."},
        # Roles and no auth_required: a protected page all the same.
        {"path": "/staff-note", "title": "Staff note", "purpose": "For the team.", "allowed_roles": ["admin"]},
    ],
    "data_models": [
        {
            "name": "Booking",
            "fields": [
                {"name": "name", "sql_type": "VARCHAR(100)", "required": True, "label": "Your name",
                 "help": "As you would like to be addressed.", "placeholder": "Sam Sample", "default": "",
                 "group": "About you"},
                {"name": "email", "sql_type": "VARCHAR(255)", "required": True},
                {"name": "kind", "field_type": "select", "options": ["first visit", "follow-up"],
                 "default": "first visit"},
                {"name": "notes", "sql_type": "TEXT", "field_type": "long_text", "rows": 4,
                 "help": "Anything we should know."},
                {"name": "party_size", "sql_type": "INTEGER", "default": 1},
                {"name": "preferred_on", "sql_type": "DATE"},
                {"name": "published", "sql_type": "BOOLEAN", "default": False},
            ],
        },
        # Every column the engine adds or reads with a fixed meaning (`id`,
        # `created_at`, a slug, the dates, the link to its parent), on a
        # model declared BEFORE the model it belongs to.
        {
            "name": "Entry", "plural": "entries", "public_listing_path": "/entries",
            "belongs_to": "Resource", "belongs_to_field": "resource_ref",
            "fields": [
                {"name": "id", "sql_type": "INTEGER"},
                {"name": "title", "sql_type": "VARCHAR(160)", "required": True},
                {"name": "slug", "sql_type": "VARCHAR(160)"},
                {"name": "summary", "field_type": "long_text"},
                {"name": "created_at", "sql_type": "TIMESTAMPTZ"},
                {"name": "updated_at", "sql_type": "TIMESTAMPTZ"},
                {"name": "published_at", "sql_type": "TIMESTAMPTZ"},
                {"name": "resource_ref", "sql_type": "INTEGER"},
                {"name": "published", "sql_type": "BOOLEAN", "default": True},
            ],
        },
        {
            "name": "Resource", "plural": "resources", "public_listing_path": "/resources",
            "belongs_to": "Booking", "belongs_to_field": "booking_id", "on_delete": "SET NULL",
            "fields": [
                {"name": "title", "sql_type": "VARCHAR(160)", "required": True},
                {"name": "slug", "sql_type": "VARCHAR(160)"},
                {"name": "summary", "field_type": "long_text"},
                {"name": "link_url", "field_type": "url"},
                {"name": "image_url", "field_type": "image"},
                {"name": "published", "sql_type": "BOOLEAN", "default": True},
            ],
        },
        {"name": "Note", "slug": "notes", "fields": [{"name": "body", "sql_type": "TEXT"}]},
    ],
    "seed_content_extra": {
        "bookings": [
            {"name": "Sam Sample", "email": "sam@example.com", "kind": "first visit", "notes": "None.",
             "party_size": 2, "preferred_on": "2030-01-15", "published": False},
            {"name": "Lee Sample", "email": "lee@example.com", "kind": "follow-up"},
        ],
        "resources": [
            {"title": "A first resource", "slug": "a-first-resource", "summary": "Short summary.",
             "link_url": "https://example.com/one", "image_url": "/images/stock/placeholder.jpg",
             "published": True},
        ],
        "entries": [
            {"title": "A first entry", "slug": "a-first-entry", "summary": "An entry.", "resource_ref": 1,
             "published_at": "2030-01-15T10:00:00Z", "published": True},
        ],
    },
    "settings_extra": {
        "address": "1 Sample Street, Sample City, ST", "phone": "555-0100", "owner_name": "Sam Sample",
        "venmo_handle": "@sample", "site_url": "https://sample.example", "hero_eyebrow": "Welcome",
        "services_intro": "What we offer.", "contact_intro": "Say hello.", "journal_title": "Journal",
        "events_title": "Events", "gallery_title": "Gallery", "brand_logo_color": "#123456",
        "brand_logo_text_color": "#ffffff", "service_options": ["First visit", "Follow-up"],
    },
    "user_roles": [
        {"role": "admin", "description": "The owner."},
        {"role": "member", "description": "A signed-in member."},
    ],
    "nav_items": [{"label": "Home", "href": "/"}, {"label": "Book", "href": "/book"}],
    "admin_nav_items": [{"label": "Dashboard", "href": "/admin"}, {"label": "Bookings", "href": "/admin/bookings"}],
    "_harbor_meta": {
        "exclude_pages": [], "exclude_data_models": [], "drop_cap": True, "self_host_fonts": False,
        "mood_keywords": ["warm", "minimal"], "avoid_keywords": ["corporate"], "density_hint": "airy",
        "hero_style_hint": "editorial", "layout_variant_hint": "",
    },
    "_translation_warnings": [{"reason": "note", "detail": "carried by the spec"}, "a plain note"],
    "template_family_id": "",
    "readability_bump": False,
    "tier": "build",
}


# What the web translator (web/src/services/harbor_to_compose.ts) can add to a
# v1-shaped spec, laid over one v1 fixture: the settings it seeds, and the
# services, testimonials, copied pages and hints it found in a conversation.
V1_EXTRAS: Dict[str, Any] = {
    "_seed_settings": {
        "site_name": "Sample Studio", "site_description": "A sample studio.", "hero_tagline": "Move well",
        "hero_description": "Classes for every body.", "hero_image_url": "/images/stock/placeholder.jpg",
        "about_title": "About", "about_body": "We opened in a sample year.",
        "about_photo_url": "/images/stock/placeholder.jpg", "business_name": "Sample Studio",
        "city_state": "Sample City, ST", "contact_address": "1 Sample Street",
        "contact_email": "owner@example.com", "contact_phone": "555-0100", "nav_logo_url": "/logo.svg",
        "services_intro": "What we offer.", "services_image_url": "/images/stock/placeholder.jpg",
        "statement_cta_text": "Text us", "venmo_handle": "@sample",
        "instagram_url": "https://instagram.example/sample", "facebook_url": "https://facebook.example/sample",
        "linkedin_url": "https://linkedin.example/sample", "tiktok_url": "https://tiktok.example/sample",
        "twitter_url": "https://twitter.example/sample", "youtube_url": "https://youtube.example/sample",
        "social": {"instagram_url": "https://instagram.example/sample"},
        "credentials_lines": ["Certified sample teacher", "Ten sample years"],
        "home_body_blocks": ["A first paragraph.", ["line one", "line two"]],
    },
    "_harbor_meta": {
        "archetype_hint": "fitness-studio", "template_family_hint": "", "layout_variant_hint": "",
        "complexity_hint": "simple", "mood_keywords": ["warm", "calm"], "avoid_keywords": ["corporate"],
        "services": [
            {"title": "Private session", "description": "One to one.", "price_display": "$90", "cta_text": "Book"},
            {"name": "Group class", "price": 20},
        ],
        "testimonials": [
            {"quote": "Patient and clear.", "author": "S., student"},
            {"quote": "Great.", "author_name": "L."},
        ],
        "copy_source_url": "https://sample.example/",
        "copy_pages": [
            {"path": "/", "markdown_blocks": ["# Sample Studio", "A paragraph.", "- one", "- two"],
             "hero": {"heading": "Sample Studio", "credential_lines": ["One", "Two"],
                      "cta_text_me_phone": "5555550112", "body_html": "<p>Hello</p>",
                      "cta_text": "Text us", "cta_href": "/contact"}},
            {"path": "/story", "markdown_blocks": ["# Our story", "Text."]},
        ],
        "exclude_pages": [], "exclude_data_models": [], "drop_cap": True, "self_host_fonts": False,
    },
}


def v1_base_spec(name: str) -> dict:
    """A v1-shaped fixture as it is, or `V1_RICH`."""
    if name == V1_RICH:
        spec = json.loads((FIXTURES / "v1-sample-yoga-studio-spec.json").read_text(encoding="utf-8"))
        spec.update(copy.deepcopy(V1_EXTRAS))
        return spec
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def v1_to_spec(v1_spec: Any) -> dict:
    """The worker's translation step: a v1-shaped spec to the engine's own."""
    from compose.intake import translate_v1_spec
    admin_email = v1_spec.get("admin_email") if isinstance(v1_spec, dict) else None
    app_name = v1_spec.get("app_name") if isinstance(v1_spec, dict) else None
    return translate_v1_spec(v1_spec, admin_email=admin_email, app_name=app_name)


def all_v1_cases() -> List[Tuple[str, Tuple, str]]:
    """The full product over the v1-shaped bases: every position (one per
    shape and base) x every wrong value."""
    cases: List[Tuple[str, Tuple, str]] = []
    for name in V1_FIXTURE_NAMES + [V1_RICH]:
        spec = v1_base_spec(name)
        seen: set = set()
        for path in positions(spec):
            shape = shape_of(path)
            if shape in seen:
                continue
            seen.add(shape)
            for value_name, value in WRONG_VALUES.items():
                current = get_at(spec, path)
                if value is not ABSENT and type(current) is type(value) and current == value:
                    continue
                cases.append((name, path, value_name))
    return cases


def base_spec(fixture_name: str) -> dict:
    """A fixture with `EXTRAS` laid over it."""
    spec = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    extras = copy.deepcopy(EXTRAS)
    seed_extra = extras.pop("seed_content_extra")
    settings_extra = extras.pop("settings_extra")
    for key, value in extras.items():
        spec[key] = value
    settings = dict(spec.get("settings") or {})
    for key, value in settings_extra.items():
        settings.setdefault(key, value)
    spec["settings"] = settings
    seed = dict(spec.get("seed_content") or {})
    seed.update(seed_extra)
    spec["seed_content"] = seed
    return spec


def positions(value: Any, prefix: Tuple = ()) -> Iterator[Tuple]:
    """Every key path below `value`: each dict key and each list element,
    the containers themselves included."""
    if prefix:
        yield prefix
    if isinstance(value, dict):
        for key, child in value.items():
            yield from positions(child, prefix + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from positions(child, prefix + (index,))


def set_at(spec: dict, path: Tuple, value: Any) -> None:
    node = spec
    for part in path[:-1]:
        node = node[part]
    if value is ABSENT:
        del node[path[-1]]
    else:
        node[path[-1]] = value


def get_at(spec: dict, path: Tuple) -> Any:
    node = spec
    for part in path:
        node = node[part]
    return node


def shape_of(path: Tuple) -> Tuple:
    return tuple("#" if isinstance(part, int) else part for part in path)


def all_cases() -> List[Tuple[str, Tuple, str]]:
    """The full product, in a fixed order: `(fixture, position, value name)`.

    A position's shape is its path with every list index replaced by "#".
    Outside `pages` and `seed_content` a shape means the same in every
    fixture (it comes from EXTRAS or from a key every fixture has), so it is
    taken once, from the first fixture that has it. Under `pages` and
    `seed_content` each shape is taken once PER fixture: each archetype puts
    different widgets and tables behind the same keys.
    """
    cases: List[Tuple[str, Tuple, str]] = []
    seen_everywhere: set = set()
    for fixture_name in FIXTURE_NAMES:
        spec = base_spec(fixture_name)
        seen_here: set = set()
        for path in positions(spec):
            shape = shape_of(path)
            seen = seen_here if path[0] in ("pages", "seed_content") else seen_everywhere
            if shape in seen:
                continue
            seen.add(shape)
            for value_name, value in WRONG_VALUES.items():
                current = get_at(spec, path)
                if value is not ABSENT and type(current) is type(value) and current == value:
                    continue  # not a change
                cases.append((fixture_name, path, value_name))
    return cases


def sample_cases(cases: List[Tuple[str, Tuple, str]], one_in: int) -> List[Tuple[str, Tuple, str]]:
    """A fixed subset: the cases whose stable hash is 0 modulo `one_in`.
    The hash is over the case's text, so the subset does not depend on the
    order of the list or on the interpreter's hash seed."""
    chosen = []
    for case in cases:
        digest = hashlib.sha256(repr(case).encode("utf-8")).digest()
        if int.from_bytes(digest[:4], "big") % one_in == 0:
            chosen.append(case)
    return chosen


REFUSED = "refused"
BUILT = "built"
CRASHED = "crashed"


def compose_case(spec_dict: dict):
    """Run one spec through the engine the way build_app does, without
    writing anything. Returns `(outcome, detail, rs, files)`.

    refused  validate_spec returned errors (detail: the first one)
    built    the files were composed
    crashed  anything else: an exception that is not the validator's
             refusal, or a refusal that only surfaced after validate_spec
             had accepted the spec (detail: where it came from)
    """
    try:
        spec = B.BuildSpec.from_dict(spec_dict)
        errors = B.validate_spec(spec)
    except ValueError as exc:
        if str(exc).startswith("Spec validation failed"):
            return REFUSED, str(exc)[:200], None, None
        return CRASHED, _where(exc), None, None
    except Exception as exc:  # noqa: BLE001 - every other exception is the finding
        return CRASHED, _where(exc), None, None
    if errors:
        return REFUSED, str(errors[0])[:200], None, None
    try:
        rs = B.resolve_spec(spec)
        files = B.compose_files(rs)
    except Exception as exc:  # noqa: BLE001
        return CRASHED, "after validate_spec accepted it: " + _where(exc), None, None
    return BUILT, "", rs, files


def _where(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "/compose/" in f.filename]
    last = frames[-1] if frames else None
    place = f"{Path(last.filename).name}:{last.lineno} in {last.name}" if last else "?"
    return f"{type(exc).__name__} at {place}: {str(exc)[:120]}"


# The build time is part of what is emitted (the sitemap's lastmod, the
# sample dates). It is pinned while cases run, so that a site differs from
# its base only because its spec does, and a run is repeatable.
PINNED_BUILD_TIME = "1700000000"


def run_cases(cases: Iterable[Tuple[str, Tuple, str]], workdir: Path, batch: int = 150,
              base_of=base_spec, to_spec=None, database_url: Optional[str] = None) -> dict:
    """`_run_cases` with SOURCE_DATE_EPOCH pinned for the duration.

    With a `database_url` (a scratch PostgreSQL database) every site that is
    built is also given its tables and rows there, in a schema of its own,
    and its routes read them: a statement Postgres rejects is then a
    failure like a view that throws. Without one the routes read a stand-in
    that answers every SELECT with one plausible row and runs no SQL."""
    previous = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = PINNED_BUILD_TIME
    try:
        return _run_cases(cases, workdir, batch, base_of, to_spec, database_url)
    finally:
        if previous is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous


def _run_cases(cases: Iterable[Tuple[str, Tuple, str]], workdir: Path, batch: int,
               base_of, to_spec, database_url: Optional[str] = None) -> dict:
    """Run cases; returns a summary. `base_of(name)` gives the spec a case
    starts from. For a spec that is not in the engine's own shape, `to_spec`
    turns it into one (for v1-shaped specs: `v1_base_spec` and `v1_to_spec`);
    a "Spec validation failed" from it is a refusal, any other exception a
    crash.

      counts    {outcome: n} plus "checked" (sites handed to the harness)
                and "same as base" (built, byte-identical to the base site)
      crashes   [(case, detail)]
      broken    [(case, [problem lines])]   built sites that do not work
    """
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    counts = {REFUSED: 0, BUILT: 0, CRASHED: 0, "checked": 0, "same as base": 0, "same as an earlier case": 0}
    crashes: List[Tuple[Any, str]] = []
    broken: List[Tuple[Any, List[str]]] = []
    bases: Dict[str, dict] = {}
    pending: List[dict] = []
    seen_digests: set = set()

    def flush() -> None:
        if not pending:
            return
        schemas: List[str] = []
        runnable: List[dict] = []
        for app in pending:
            if database_url is None:
                runnable.append(app)
                continue
            schema = "matrix_" + app["id"].replace("-", "_")
            try:
                app["database_url"] = SC.apply_site_sql(app["dir"], database_url, schema=schema)
            except Exception as exc:  # noqa: BLE001 - psycopg2.Error, or a file the site lacks
                detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
                broken.append((app["case"], [f"postgres rejected the site's schema or seed: {detail[:200]}"]))
                continue
            schemas.append(schema)
            runnable.append(app)
        try:
            results = SC.check_sites(runnable, workdir / "_harness") if runnable else {}
        finally:
            for schema in schemas:
                SC.drop_schema(database_url, schema)
        for app in runnable:
            problems = SC.problems_of(results[app["id"]])
            if problems:
                broken.append((app["case"], problems))
        for app in pending:
            shutil.rmtree(app["dir"], ignore_errors=True)
        pending.clear()

    for number, case in enumerate(cases):
        fixture_name, path, value_name = case
        if fixture_name not in bases:
            base_dict = base_of(fixture_name)
            base_in = copy.deepcopy(base_dict)
            outcome, detail, rs, files = compose_case(to_spec(base_in) if to_spec else base_in)
            if outcome != BUILT:
                raise AssertionError(f"the base spec for {fixture_name} is not accepted: {detail}")
            base_dir = workdir / ("base-" + fixture_name.replace(".json", ""))
            SC.write_files(base_dir, files)
            base_app = {"id": "base", "dir": base_dir, "tables": SC.tables_for(rs)}
            if database_url is not None:
                base_app["database_url"] = SC.apply_site_sql(base_dir, database_url, schema="matrix_base")
            try:
                base_result = SC.check_sites([base_app], workdir / "_harness")["base"]
            finally:
                if database_url is not None:
                    SC.drop_schema(database_url, "matrix_base")
            base_problems = SC.problems_of(base_result)
            if base_problems:
                raise AssertionError(f"the base site for {fixture_name} does not work: {base_problems[:5]}")
            bases[fixture_name] = {"spec": base_dict, "dir": base_dir, "files": dict(files)}
        base = bases[fixture_name]
        spec_dict = copy.deepcopy(base["spec"])
        wrong = WRONG_VALUES[value_name]
        set_at(spec_dict, path, wrong if wrong is ABSENT else copy.deepcopy(wrong))
        if to_spec is not None:
            try:
                spec_dict = to_spec(spec_dict)
            except ValueError as exc:
                if str(exc).startswith("Spec validation failed"):
                    counts[REFUSED] += 1
                else:
                    counts[CRASHED] += 1
                    crashes.append((case, "in the translator: " + _where(exc)))
                continue
            except Exception as exc:  # noqa: BLE001 - any other exception is the finding
                counts[CRASHED] += 1
                crashes.append((case, "in the translator: " + _where(exc)))
                continue
        outcome, detail, rs, files = compose_case(spec_dict)
        counts[outcome] += 1
        if outcome == CRASHED:
            crashes.append((case, detail))
            continue
        if outcome == REFUSED:
            continue
        file_map = dict(files)
        differing = {
            rel: content for rel, content in file_map.items()
            if rel != "spec.json" and base["files"].get(rel) != content
        }
        missing = [rel for rel in base["files"] if rel not in file_map]
        if not differing and not missing:
            counts["same as base"] += 1
            continue
        digest = hashlib.sha256()
        digest.update(fixture_name.encode("utf-8"))
        for rel in sorted(differing):
            content = differing[rel]
            digest.update(rel.encode("utf-8"))
            digest.update(content if isinstance(content, bytes) else content.encode("utf-8", "surrogatepass"))
        for rel in sorted(missing):
            digest.update(b"missing:" + rel.encode("utf-8"))
        key = digest.hexdigest()
        if key in seen_digests:
            counts["same as an earlier case"] += 1
            continue
        seen_digests.add(key)
        app_dir = workdir / f"case-{number}"
        try:
            SC.write_overlay(app_dir, base["dir"], base["files"], file_map)
        except Exception as exc:  # noqa: BLE001 - e.g. a file that cannot be encoded
            crashes.append((case, "while writing the built files: " + _where(exc)))
            counts[CRASHED] += 1
            counts[BUILT] -= 1
            shutil.rmtree(app_dir, ignore_errors=True)
            continue
        counts["checked"] += 1
        pending.append({"id": f"case-{number}", "dir": app_dir, "tables": SC.tables_for(rs), "case": case})
        if len(pending) >= batch:
            flush()
    flush()
    return {"counts": counts, "crashes": crashes, "broken": broken}


def describe(case: Tuple[str, Tuple, str]) -> str:
    fixture_name, path, value_name = case
    return f"{fixture_name}: {'.'.join(str(p) for p in path)} = <{value_name}>"
