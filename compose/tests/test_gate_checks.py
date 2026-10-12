"""Single checks of the spec gate, one test each.

compose/tests/test_spec_hardening.py attacks the gate with hostile specs.
This file comes from the other direction: take ONE condition out of
`_spec_safety_errors`, `_is_safe_route_path`, `_is_safe_sql_type` or
`_fill_template` and see whether any test notices. Each test here is the
one that notices for a condition nothing else pinned (the name of the
condition is in the test's docstring), plus the checks added with the
shape rules: ASCII-only allowlists, the bounds on a spec's size, and the
types of the values the emitters read.

No node, no network, no database.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, compose_files, resolve_spec, validate_spec

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Built at run time so this file holds only ASCII: full-width digits one and
# zero, an Arabic-Indic three, a dotless i, a long s, a no-break space.
FULLWIDTH_10 = chr(0xFF11) + chr(0xFF10)
ARABIC_3 = chr(0x0663)
DOTLESS_I = chr(0x0131)
LONG_S = chr(0x017F)
NBSP = chr(0x00A0)


def _base() -> dict:
    return json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))


def _errors(spec: dict) -> list:
    return validate_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def _refused(spec: dict, fragment: str) -> None:
    errors = _errors(spec)
    assert any(fragment in e for e in errors), errors
    with pytest.raises(ValueError, match="Spec validation failed"):
        resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def _with_model(**model) -> dict:
    spec = _base()
    spec["data_models"] = [{"name": "Thing", "fields": [{"name": "title", "sql_type": "TEXT"}], **model}]
    return spec


def _with_field(**field) -> dict:
    return _with_model(fields=[{"name": "title", "sql_type": "TEXT"}, {"name": "extra", **field}])


# --- conditions that no test pinned ----------------------------------------

def test_an_admin_email_longer_than_254_characters_is_refused():
    """`len(spec.admin_email) > 254`. The address below fits the pattern
    (64-character local part, labels of at most 63) and is 320 long; it is
    written into db/seed.sql and .env.example."""
    spec = _base()
    spec["admin_email"] = "a" * 64 + "@" + ".".join(["b" * 63] * 4)
    assert B._ADMIN_EMAIL_RE.fullmatch(spec["admin_email"]) and len(spec["admin_email"]) == 320
    _refused(spec, "admin_email is not a plain email address")
    spec["admin_email"] = "a" * 64 + "@" + ".".join(["b" * 62] * 3)
    assert len(spec["admin_email"]) == 253
    assert _errors(spec) == []


def test_fill_template_never_rescans_what_it_substituted():
    """`_fill_template` replaces every token in ONE pass. A value that
    contains a token of another part stays as written; filled part by part
    it would pull that part into itself."""
    out = B._fill_template("a __FIRST__ b __SECOND__", {"FIRST": "__SECOND__", "SECOND": "secret"})
    assert out == "a __SECOND__ b secret"
    with pytest.raises(KeyError):
        B._fill_template("__MISSING__", {})


def test_a_form_message_that_names_a_template_token_stays_text():
    """The same property through the emitter that uses it: the success
    message of a public form is spec text, and the handler's template has an
    `__INSERT_SQL__` token. The message must come out as a string, not as
    the SQL."""
    spec = _base()
    spec["data_models"] = [{
        "name": "Booking",
        "fields": [{"name": "name", "sql_type": "VARCHAR(100)"}, {"name": "published", "sql_type": "BOOLEAN"}],
    }]
    spec["custom_pages"] = [{
        "path": "/book", "title": "__GUARDS__", "form_target": "Booking", "allowed_fields": ["name"],
        "success_message": "__INSERT_SQL__ __FIELDS__",
    }]
    files = dict(compose_files(resolve_spec(BuildSpec.from_dict(spec))))
    pages = files["routes/pages.js"]
    assert 'req.session.flash_success = "__INSERT_SQL__ __FIELDS__";' in pages
    assert "subject: 'New __GUARDS__ submission'" in pages
    assert pages.count("INSERT INTO bookings") == 1


@pytest.mark.parametrize("value", [["a row"], "rows", 7, True])
def test_seed_content_that_is_not_an_object_is_refused(value):
    """`for attr in ("settings", "pages", "seed_content")`: seed_content."""
    spec = _base()
    spec["seed_content"] = value
    _refused(spec, "seed_content must be an object")


@pytest.mark.parametrize("key, value, message", [
    ("custom_pages", {"path": "/x", "title": "X"}, "custom_pages must be a list"),
    ("custom_pages", "/x", "custom_pages must be a list"),
    ("user_roles", {"role": "editor"}, "user_roles must be a list"),
    ("user_roles", "editor", "user_roles must be a list"),
    ("data_models", {"name": "Thing"}, "data_models must be a list"),
    ("_translation_warnings", {"reason": "r"}, "translation_warnings must be a list"),
])
def test_a_list_valued_key_that_is_not_a_list_is_refused(key, value, message):
    """`for attr in ("data_models", "custom_pages", "user_roles",
    "translation_warnings")`: each of the four."""
    spec = _base()
    spec[key] = value
    _refused(spec, message)


def test_a_route_path_longer_than_120_characters_is_not_a_route_path():
    """`len(path) <= 120` in `_is_safe_route_path`. Eight segments of 20
    characters fit the pattern and are 168 long."""
    long_path = "/" + "/".join(["a" * 20] * 8)
    assert B._SAFE_PATH_RE.fullmatch(long_path) and len(long_path) == 168
    assert B._is_safe_route_path(long_path) is False
    assert B._is_safe_route_path("/" + "a" * 119) is True
    # As a public listing path it is refused; as a custom page path the page
    # is dropped and the report says so.
    _refused(_with_model(public_listing_path=long_path), "is not a plain absolute path")
    spec = _base()
    spec["custom_pages"] = [{"path": long_path, "title": "Long"}]
    rs = resolve_spec(BuildSpec.from_dict(spec))
    assert [w["reason"] for w in rs.spec.engine_warnings] == ["custom_page_path_dropped"]
    assert "router.get('/aaaa" not in dict(compose_files(rs))["routes/pages.js"]


def test_a_column_type_longer_than_40_characters_is_refused():
    """`len(sql_type) <= 40` in `_is_safe_sql_type`. The pattern allows any
    amount of space inside the parentheses, so this type fits it."""
    padded = "varchar(" + " " * 40 + "10)"
    assert B._SAFE_SQL_TYPE_RE.fullmatch(padded) and len(padded) > 40
    assert B._is_safe_sql_type(padded) is False
    _refused(_with_field(sql_type=padded), "sql_type is not a plain column type")
    assert B._is_safe_sql_type("double precision[]") is True


@pytest.mark.parametrize("key", ["belongs_to", "on_delete"])
@pytest.mark.parametrize("value", [7, ["Booking"], {"model": "Booking"}, True])
def test_a_relation_key_that_is_not_a_string_is_refused(key, value):
    """`if val is not None and not isinstance(val, str)` for belongs_to and
    on_delete."""
    _refused(_with_model(**{key: value}), f"data_model 'Thing' {key} must be a string")


# --- allowlists match ASCII only ---------------------------------------------

@pytest.mark.parametrize("sql_type", [
    "VARCHAR(" + FULLWIDTH_10 + ")",       # digits of another script
    "varchar(" + ARABIC_3 + ")",
    DOTLESS_I + "nteger",                  # folds onto "integer" without re.ASCII
    "j" + LONG_S + "on",                   # folds onto "json"
    "t" + DOTLESS_I + "me" + LONG_S + "tamp",
    "numeric(10," + NBSP + "2)",           # a space that is not an ASCII space
])
def test_a_column_type_that_only_looks_like_ascii_is_refused(sql_type):
    assert B._is_safe_sql_type(sql_type) is False
    _refused(_with_field(sql_type=sql_type), "sql_type is not a plain column type")
    with pytest.raises(ValueError, match="Spec validation failed"):
        B._safe_sql_type({"name": "extra", "sql_type": sql_type})


@pytest.mark.parametrize("sql_type", ["VARCHAR(10)", "numeric( 10 , 2 )", "Timestamptz", "text[]", "DOUBLE PRECISION"])
def test_plain_column_types_still_pass(sql_type):
    assert B._is_safe_sql_type(sql_type) is True


def test_a_default_in_another_scripts_digits_is_text_not_a_number():
    """Postgres reads only ASCII digits as a number. Written bare, these
    would be a syntax error in db/schema.sql at first boot."""
    assert B._format_sql_default(ARABIC_3 * 3) == "'" + ARABIC_3 * 3 + "'"
    assert B._format_sql_default(FULLWIDTH_10) == "'" + FULLWIDTH_10 + "'"
    assert B._format_sql_default("123") == "123" and B._format_sql_default("-1.5e3") == "-1.5e3"


def test_every_allowlist_pattern_of_the_gate_is_ascii_only():
    """The patterns that decide what may become an identifier, a column
    type, a route path, an address or a number, each against a string that
    only a Unicode-aware match would accept."""
    lookalike_digit = "a" + FULLWIDTH_10
    for pattern, probe in (
        (B._APP_NAME_RE, lookalike_digit),
        (B._MODEL_NAME_RE, "A" + ARABIC_3),
        (B._FIELD_NAME_RE, "a" + ARABIC_3),
        (B._MODEL_SLUG_RE, lookalike_digit),
        (B._PAGE_ID_RE, lookalike_digit),
        (B._SAFE_PATH_RE, "/" + lookalike_digit),
        (B._SAFE_SQL_TYPE_RE, "varchar(" + FULLWIDTH_10 + ")"),
        (B._ADMIN_EMAIL_RE, "a" + ARABIC_3 + "@example.com"),
        (B._URL_LEADING_SCHEME_RE, "a" + ARABIC_3 + ":"),
        (B._SQL_NUMERIC_LITERAL_RE, ARABIC_3),
        (B._SQL_WIDTH_RE, "varchar(" + FULLWIDTH_10 + ")"),
        (B._NAV_HREF_RE, "/" + lookalike_digit),
    ):
        assert pattern.flags & B.re.ASCII, pattern.pattern
        assert pattern.fullmatch(probe) is None, (pattern.pattern, probe)


def _put_lookalike(spec: dict, where: str) -> None:
    """Write a name that only a Unicode-aware match would accept into the
    spec at `where`."""
    if where == "app_name":
        spec["app_name"] = "site" + FULLWIDTH_10
    elif where == "admin_email":
        spec["admin_email"] = "a" + ARABIC_3 + "@example.com"
    elif where == "model name":
        spec["data_models"] = [{"name": "Thing" + ARABIC_3, "fields": [{"name": "title", "sql_type": "TEXT"}]}]
    elif where == "model slug":
        spec["data_models"] = [{"name": "Thing", "plural": "things" + FULLWIDTH_10,
                                "fields": [{"name": "title", "sql_type": "TEXT"}]}]
    elif where == "field name":
        spec["data_models"] = [{"name": "Thing", "fields": [{"name": "title" + ARABIC_3, "sql_type": "TEXT"}]}]
    else:
        spec["pages"]["page" + FULLWIDTH_10] = copy.deepcopy(spec["pages"]["home"])


@pytest.mark.parametrize("where", ["app_name", "admin_email", "model name", "model slug", "field name", "page id"])
def test_the_gate_itself_refuses_a_name_that_only_looks_like_ascii(where):
    """The test above holds each pattern to ASCII; this one holds the gate
    to the patterns: the same lookalike, written into a spec where the name
    is read, is refused by `validate_spec` and by `resolve_spec`, and the
    spec without it is not."""
    spec = _base()
    assert _errors(spec) == []
    _put_lookalike(spec, where)
    errors = _errors(spec)
    assert errors, where
    with pytest.raises(ValueError, match="Spec validation failed"):
        resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


def test_the_url_neutralizer_treats_a_lookalike_scheme_as_the_scheme():
    """The two patterns that are NOT allowlists stay Unicode-aware: a value
    they match is neutralized, so matching more is the safe side."""
    lookalike = "java" + LONG_S + "cript:alert(1)"
    assert B._neutralize_url(lookalike) == "#"
    assert B._sanitize_spec_content({"l" + DOTLESS_I + "nk": "javascript:alert(1)"}) == {"l" + DOTLESS_I + "nk": "#"}


# --- the size of a spec is bounded ---------------------------------------------

def _nested(levels: int):
    value: object = "leaf"
    for _ in range(levels):
        value = {"a": value}
    return value


def test_one_string_may_not_be_longer_than_the_bound():
    spec = _base()
    spec["settings"]["about_body"] = "x" * B._MAX_SPEC_STRING
    assert _errors(spec) == []
    spec["settings"]["about_body"] = "x" * (B._MAX_SPEC_STRING + 1)
    assert _errors(spec) == [
        f"settings.about_body is {B._MAX_SPEC_STRING + 1} characters long; one string of a spec "
        f"may have at most {B._MAX_SPEC_STRING}"
    ]
    # A key is text too.
    spec = _base()
    spec["pages"]["home"]["hero"]["k" * (B._MAX_SPEC_STRING + 1)] = "v"
    assert "characters long" in _errors(spec)[0]


def test_the_text_of_a_spec_is_bounded_in_total():
    spec = _base()
    chunk = "x" * B._MAX_SPEC_STRING
    spec["seed_content"]["notes"] = [{"body": chunk} for _ in range(B._MAX_SPEC_TEXT // B._MAX_SPEC_STRING + 1)]
    assert _errors(spec) == [f"the spec holds more than {B._MAX_SPEC_TEXT} characters of text"]


def test_nesting_is_bounded_and_the_check_does_not_recurse():
    spec = _base()
    # `pages` is level 1 and `home` level 2, so the outermost of the nested
    # objects is level 3 and the innermost of n is level n + 2.
    spec["pages"]["home"]["extra"] = _nested(B._MAX_SPEC_DEPTH - 2)
    assert _errors(spec) == []
    spec["pages"]["home"]["extra"] = _nested(B._MAX_SPEC_DEPTH - 1)
    errors = _errors(spec)
    assert len(errors) == 1 and errors[0].endswith(f"is nested more than {B._MAX_SPEC_DEPTH} levels deep")
    # Deep enough that a recursive walk (or json.dumps, or the deep copy the
    # helpers above make) would raise RecursionError; the gate answers with
    # the same refusal.
    spec["pages"]["home"]["extra"] = _nested(50_000)
    assert validate_spec(BuildSpec.from_dict(spec))[0].endswith("levels deep")
    with pytest.raises(ValueError, match="levels deep"):
        resolve_spec(BuildSpec.from_dict(spec))


def test_a_spec_file_nested_too_deeply_to_parse_is_refused(tmp_path):
    """Python's JSON reader recurses per level and gives up with
    RecursionError long before the gate could count. build_app turns that
    into the refusal it is."""
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    with pytest.raises(ValueError, match="Spec validation failed: the spec file is nested too deeply"):
        B.build_app(str(deep), output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_the_number_of_values_is_bounded():
    spec = _base()
    spec["seed_content"]["numbers"] = list(range(B._MAX_SPEC_VALUES + 1))
    assert _errors(spec) == [f"the spec holds more than {B._MAX_SPEC_VALUES} values"]


@pytest.mark.parametrize("key, make, limit_name", [
    ("custom_pages", lambda i: {"path": f"/p{i}", "title": "t"}, "_MAX_CUSTOM_PAGES"),
    ("data_models", lambda i: {"name": f"Model{i}x", "fields": [{"name": "title", "sql_type": "TEXT"}]},
     "_MAX_DATA_MODELS"),
    ("user_roles", lambda i: {"role": f"role{i}", "description": "d"}, "_MAX_USER_ROLES"),
])
def test_the_number_of_pages_models_and_roles_is_bounded(key, make, limit_name):
    limit = getattr(B, limit_name)
    spec = _base()
    spec[key] = [make(i) for i in range(limit + 1)]
    assert _errors(spec) == [f"{key} has {limit + 1} entries; a spec may have at most {limit}"]
    spec[key] = spec[key][:limit]
    assert not [e for e in _errors(spec) if "at most" in e]


def test_the_number_of_fields_of_a_model_is_bounded():
    fields = [{"name": f"f{i}", "sql_type": "TEXT"} for i in range(B._MAX_MODEL_FIELDS + 1)]
    assert _errors(_with_model(fields=fields)) == [
        f"a data_model has {B._MAX_MODEL_FIELDS + 1} fields; a model may have at most {B._MAX_MODEL_FIELDS}"
    ]
    assert _errors(_with_model(fields=fields[:B._MAX_MODEL_FIELDS])) == []


@pytest.mark.parametrize("place", ["a value", "a key", "a list entry", "app_name's neighbour"])
def test_a_lone_surrogate_anywhere_in_a_spec_is_refused(place):
    """JSON can spell half of a surrogate pair. It is not text and cannot
    be written to any file; an emitter met it only at write time."""
    lone = "x" + chr(0xD800)
    spec = _base()
    if place == "a value":
        spec["settings"]["site_name"] = lone
    elif place == "a key":
        spec["pages"]["home"][lone] = "v"
    elif place == "a list entry":
        spec["_harbor_meta"] = {"mood_keywords": ["warm", lone]}
    else:
        spec["palette"] = lone
    errors = _errors(spec)
    assert len(errors) >= 1 and any("lone surrogate" in e for e in errors), errors


def test_what_is_not_json_is_refused():
    spec = _base()
    spec["pages"]["home"]["extra"] = {1: "an integer key"}
    assert "has a key that is not a string" in _errors(spec)[0]
    spec = _base()
    spec["pages"]["home"]["extra"] = {"when": b"bytes"}
    assert _errors(spec) == ["pages.home.extra.when is not a JSON value (bytes)"]


def test_a_spec_that_is_not_an_object_or_lacks_a_required_key_is_refused_not_a_traceback():
    for not_a_spec in (None, [], "spec", 7):
        with pytest.raises(ValueError, match="Spec validation failed: the spec must be a JSON object"):
            BuildSpec.from_dict(not_a_spec)
    errors = validate_spec(BuildSpec.from_dict({}))
    assert "app_name is required" in errors and "admin_email is required" in errors
    assert any(e.startswith("Unknown archetype") for e in errors)


# --- the type of each value the emitters read ------------------------------------

@pytest.mark.parametrize("value", [7, 2.5, True, "First visit", {"a": 1}, ["ok", 7], [None]])
def test_service_options_must_be_a_list_of_strings(value):
    spec = _base()
    spec["settings"]["service_options"] = value
    _refused(spec, "settings.service_options must be a list of strings")


@pytest.mark.parametrize("key, value, message", [
    ("allowed_fields", True, "custom_page allowed_fields must be a list of strings"),
    ("allowed_fields", 7, "custom_page allowed_fields must be a list of strings"),
    ("allowed_fields", "name", "custom_page allowed_fields must be a list of strings"),
    ("allowed_fields", ["name", 7], "custom_page allowed_fields must be a list of strings"),
    ("auth_required", "yes", "custom_page auth_required must be true or false"),
    ("auth_required", 1, "custom_page auth_required must be true or false"),
    ("title", 7, "custom_page title must be a string"),
    ("success_message", ["ok"], "custom_page success_message must be a string"),
])
def test_custom_page_keys_have_one_type_each(key, value, message):
    spec = _base()
    spec["data_models"] = [{"name": "Booking", "fields": [{"name": "name", "sql_type": "TEXT"}]}]
    spec["custom_pages"] = [{"path": "/book", "title": "Book", "form_target": "Booking",
                             "allowed_fields": ["name"], key: value}]
    _refused(spec, message)


@pytest.mark.parametrize("key, value, message", [
    ("label", 5, "label must be a string"),
    ("help", ["h"], "help must be a string"),
    ("placeholder", {"p": 1}, "placeholder must be a string"),
    ("group", 7, "group must be a string"),
    ("field_type", 7, "field_type must be a string"),
    ("field_type", "not-a-type", "field_type is not a known field type: 'not-a-type'"),
    ("required", "yes", "required must be true or false"),
    ("required", 1, "required must be true or false"),
    ("rows", "4", "rows must be a whole number"),
    ("rows", 2.5, "rows must be a whole number"),
    ("rows", True, "rows must be a whole number"),
    ("rows", 0, "rows must be between 1 and 100"),
    ("rows", 101, "rows must be between 1 and 100"),
    ("options", "a,b", "options must be a list of strings, numbers or {value, label} objects"),
    ("options", [None], "options must be a list of strings, numbers or {value, label} objects"),
    ("options", [["a"]], "options must be a list of strings, numbers or {value, label} objects"),
    ("options", [{"label": "no value"}], "options must be a list of strings, numbers or {value, label} objects"),
    ("options", [{"value": "v", "label": ["l"]}], "options must be a list of strings, numbers or {value, label} objects"),
    ("default", ["a"], "default must be a string, a number or a boolean"),
    ("default", {"a": 1}, "default must be a string, a number or a boolean"),
])
def test_model_field_keys_have_one_type_each(key, value, message):
    _refused(_with_field(sql_type="TEXT", **{key: value}), f"data_model 'Thing' field 'extra' {message}")


def test_well_formed_field_keys_pass_and_null_means_absent():
    spec = _with_field(
        field_type="select", label="Kind", help="Pick one.", placeholder="Choose", group="Details",
        required=True, options=["a", 2, {"value": "c", "label": "Sea"}, {"value": 4}], default="a",
    )
    assert _errors(spec) == []
    spec = _with_field(sql_type="TEXT", label=None, help=None, required=None, rows=None, options=None, default=None)
    assert _errors(spec) == []
    spec = _with_field(field_type="long_text", rows=12)
    assert _errors(spec) == []


def test_the_choices_of_a_select_are_emitted_as_strings():
    """The form templates test `typeof opt === 'string'`; a number would be
    read as an object and render an empty choice."""
    field = {"options": [1, "a", 2.5, {"value": 2, "label": 3}, {"value": "x"}]}
    assert B._select_choices(field) == ["1", "a", "2.5", {"value": "2", "label": "3"}, {"value": "x"}]
    assert B._select_choices({"options": []}) is None
    assert B._select_choices({"options": "a,b"}) is None
    assert B._select_choices({}) is None
    # Through the two emitters that hand choices to a template.
    spec = _base()
    spec["data_models"] = [{"name": "Booking", "fields": [
        {"name": "name", "sql_type": "TEXT"},
        {"name": "party", "field_type": "select", "options": [1, 2, {"value": 3, "label": "Three"}]},
    ]}]
    spec["custom_pages"] = [{"path": "/book", "title": "Book", "form_target": "Booking",
                             "allowed_fields": ["name", "party"]}]
    rs = resolve_spec(BuildSpec.from_dict(spec))
    model = [m for m in rs.data_models if m.get("name") == "Booking"][0]
    form_fields = {f["name"]: f for f in B._public_form_fields(spec["custom_pages"][0], model)}
    assert form_fields["party"]["options"] == ["1", "2", {"value": "3", "label": "Three"}]
    files = dict(compose_files(rs))
    for view in ("views/bookings/new.ejs", "views/bookings/edit.ejs"):
        assert '"options": ["1", "2", {"value": "3", "label": "Three"}]' in files[view], view


def test_the_footer_line_of_the_social_card_is_bounded():
    """Pillow draws the whole string it is given. A site_url of 99,000
    characters (inside the bound on one string) used to be drawn in full:
    seconds of work and, a little longer, an image Pillow refuses."""
    pytest.importorskip("PIL")
    spec = _base()
    spec["settings"]["site_url"] = "https://sample.example/" + "a" * 99_000
    rs = resolve_spec(BuildSpec.from_dict(spec))
    drawn = []
    from PIL import ImageDraw
    real_text = ImageDraw.ImageDraw.text

    def recording_text(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return real_text(self, xy, text, *args, **kwargs)

    ImageDraw.ImageDraw.text = recording_text
    try:
        emitted = B.emit_og_default_image(rs)
    finally:
        ImageDraw.ImageDraw.text = real_text
    assert emitted is not None and emitted[0] == "public/brand/og-default.jpg"
    assert drawn and max(len(text) for text in drawn) <= 140
    assert ("sample.example/" + "a" * 75) in drawn


# --- the v1 translator reads a model's JSON first --------------------------------

def _v1() -> dict:
    return json.loads((FIXTURES / "v1-sample-yoga-studio-spec.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.__setitem__("description", 7), "description must be a string"),
    (lambda d: d.__setitem__("pages", "all of them"), "pages must be a list"),
    (lambda d: d.__setitem__("data_models", {"name": "X"}), "data_models must be a list"),
    (lambda d: d.__setitem__("user_roles", 3), "user_roles must be a list"),
    (lambda d: d["data_models"].append({"name": True}), "].name must be a string"),
    (lambda d: d["data_models"].append({"name": "T", "fields": "title"}), "].fields must be a list"),
    (lambda d: d["data_models"].append({"name": "T", "fields": [True]}), "].fields[0] must be an object"),
    (lambda d: d["data_models"].append({"name": "T", "fields": [{"name": "t", "type": 5}]}),
     "].fields[0].type must be a string"),
    (lambda d: d["pages"].append({"path": "/x", "form_target": 2.5}), "].form_target must be a string"),
    (lambda d: d["pages"].append({"path": "/x", "submit_text": ["Go"]}), "].submit_text must be a string"),
    (lambda d: d["user_roles"].append({"role": 7}), "].role must be a string"),
    (lambda d: d.__setitem__("_seed_settings", {"contact_phone": 5125550100}),
     "_seed_settings.contact_phone must be a string"),
    (lambda d: d.__setitem__("_seed_settings", {"social": 7}), "_seed_settings.social must be an object"),
    (lambda d: d.__setitem__("_seed_settings", {"credentials_lines": 7}),
     "_seed_settings.credentials_lines must be a list"),
    (lambda d: d.__setitem__("_harbor_meta", {"mood_keywords": 7}),
     "_harbor_meta.mood_keywords must be a list of strings"),
    (lambda d: d.__setitem__("_harbor_meta", {"services": [{"title": 7}]}),
     "_harbor_meta.services[0].title must be a string"),
    (lambda d: d.__setitem__("_harbor_meta", {"testimonials": [{"quote": ["q"]}]}),
     "_harbor_meta.testimonials[0].quote must be a string"),
])
def test_the_v1_translator_refuses_a_value_of_the_wrong_type(mutate, message):
    """Each of these used to end in an AttributeError or TypeError from
    somewhere inside translate_v1_spec."""
    from compose.intake import translate_v1_spec
    spec = _v1()
    mutate(spec)
    with pytest.raises(ValueError) as refused:
        translate_v1_spec(spec, admin_email="owner@example.com")
    assert str(refused.value).startswith("Spec validation failed: ")
    assert message in str(refused.value)


def test_the_v1_translator_refuses_what_is_not_an_object():
    from compose.intake import translate_v1_spec
    for not_a_spec in (None, [], "spec", 7):
        with pytest.raises(ValueError, match="Spec validation failed: the spec must be a JSON object"):
            translate_v1_spec(not_a_spec)


def test_the_v1_shape_check_leaves_what_the_translator_handles_itself():
    """Entries of the three lists that are not objects are dropped by the
    translator with a warning each; null means absent. Neither is refused."""
    from compose.intake import _v1_shape_errors, translate_v1_spec
    spec = _v1()
    spec["data_models"] = list(spec.get("data_models") or []) + ["not-a-model", None]
    spec["pages"] = list(spec.get("pages") or []) + [None, 7]
    spec["user_roles"] = list(spec.get("user_roles") or []) + [123]
    spec["pages"].append({"path": 7, "title": "A path that is not text"})
    spec["description"] = None
    spec["_harbor_meta"] = ["not", "an", "object"]
    spec["_seed_settings"] = "not an object"
    assert _v1_shape_errors(spec) == []
    v2 = translate_v1_spec(spec, admin_email="owner@example.com")
    reasons = {w["reason"] for w in v2["_translation_warnings"]}
    assert {"invalid_data_model", "invalid_page", "invalid_page_path", "invalid_user_role"} <= reasons
    assert validate_spec(BuildSpec.from_dict(v2)) == []


def test_the_v1_translator_applies_the_same_bounds_before_it_reads_anything():
    """The translator walks a v1 spec before the engine's gate sees the
    result, so the bound on size has to hold there as well."""
    from compose.intake import translate_v1_spec
    cases = {
        "characters long": lambda d: d.__setitem__("description", "x" * (B._MAX_SPEC_STRING + 1)),
        "levels deep": lambda d: d.__setitem__("_harbor_meta", {"services": _nested(50_000)}),
        "lone surrogate": lambda d: d["pages"].append({"path": "/x", "title": "t" + chr(0xD800)}),
        f"more than {B._MAX_SPEC_VALUES} values": lambda d: d.__setitem__(
            "pages", [{"path": f"/p{i}"} for i in range(B._MAX_SPEC_VALUES)]),
    }
    for fragment, mutate in cases.items():
        spec = _v1()
        mutate(spec)
        with pytest.raises(ValueError) as refused:
            translate_v1_spec(spec, admin_email="owner@example.com")
        assert str(refused.value).startswith("Spec validation failed: "), fragment
        assert fragment in str(refused.value), (fragment, str(refused.value)[:200])


def test_a_spec_file_larger_than_the_bound_is_refused_before_it_is_read(tmp_path, monkeypatch):
    # The real bound leaves room for any spec the other bounds accept: all
    # of its text with every character written as a six-byte JSON escape,
    # and twenty bytes of punctuation for every value.
    assert B._MAX_SPEC_FILE_BYTES >= 6 * B._MAX_SPEC_TEXT + 20 * B._MAX_SPEC_VALUES
    big = tmp_path / "big.json"
    big.write_text("{}" + " " * 4096, encoding="utf-8")
    monkeypatch.setattr(B, "_MAX_SPEC_FILE_BYTES", 4096)
    opened = []
    real_open = open

    def recording_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", recording_open)
    with pytest.raises(ValueError, match="Spec validation failed: the spec file is larger than 4096 bytes"):
        B.build_app(str(big), output_dir=tmp_path / "out")
    assert str(big) not in opened
    assert not (tmp_path / "out").exists()


# The bounds above are applied to the containers BuildSpec keeps. A spec
# document also carries keys the engine never reads, and `build_app` walks
# those: the link lint walks the whole document. So the document is bounded
# as a whole, before anything reads it.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

UNKNOWN_KEY_VALUES = {
    "deep": ("levels deep", lambda: _deep_lists(5_000)),
    "wide": (f"more than {B._MAX_SPEC_VALUES} values", lambda: list(range(B._MAX_SPEC_VALUES + 1))),
    "huge string": ("characters long", lambda: {"a": "x" * (B._MAX_SPEC_STRING + 1)}),
    "huge string under a deep key": (
        "characters long", lambda: _wrap_in_lists("x" * (B._MAX_SPEC_STRING + 1), 20)),
    "text in all": (
        f"more than {B._MAX_SPEC_TEXT} characters of text",
        lambda: ["x" * B._MAX_SPEC_STRING for _ in range(B._MAX_SPEC_TEXT // B._MAX_SPEC_STRING + 1)]),
}


def _deep_lists(levels: int):
    """A list nested `levels` deep, built without recursion."""
    root: list = []
    cursor = root
    for _ in range(levels - 1):
        inner: list = []
        cursor.append(inner)
        cursor = inner
    return root


def _wrap_in_lists(value, levels: int):
    for _ in range(levels):
        value = [value]
    return value


def _link_lint_on(monkeypatch) -> list:
    """Switch the link lint ON with the network replaced by a recorder, and
    return the list of URLs it was asked to check. The test suite defaults
    the lint to "skip", where nothing walks the document."""
    import compose.lint_external_urls as lint_module
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "warn")
    asked: list = []

    def recorder(url, timeout=5.0):
        asked.append(url)
        return {"url": url, "ok": True, "status": 200, "error": None}

    monkeypatch.setattr(lint_module, "check_url", recorder)
    return asked


@pytest.mark.parametrize("key", ["notes", "tier", "_harbor_notes"])
@pytest.mark.parametrize("name", sorted(UNKNOWN_KEY_VALUES))
def test_build_app_bounds_the_whole_document_unknown_keys_included(name, key, tmp_path, monkeypatch):
    """`_spec_size_errors` looks at the containers BuildSpec keeps; the lint
    and the report read the raw document. A 5,000-deep value under a key the
    engine does not know used to reach the lint, which raised RecursionError,
    and `build_app` turned that into one warning and built the site."""
    fragment, make = UNKNOWN_KEY_VALUES[name]
    asked = _link_lint_on(monkeypatch)
    spec = _base()
    spec["settings"]["amazon_url"] = "https://shop.example.com/dp/ONE"
    spec[key] = make()
    with pytest.raises(ValueError, match="Spec validation failed: ") as refused:
        B.build_app(spec, output_dir=tmp_path / "out")
    assert fragment in str(refused.value)
    assert not (tmp_path / "out").exists(), "nothing is written for a refused document"
    assert asked == [], "the lint did not run on a document the gate had refused"


def test_a_document_within_the_bounds_is_still_linted_whole(tmp_path, monkeypatch):
    """The control: the same spec with a small value under an unknown key
    builds, and the lint still reads the document (the URL it was given)."""
    asked = _link_lint_on(monkeypatch)
    spec = _base()
    spec["settings"]["amazon_url"] = "https://shop.example.com/dp/ONE"
    spec["notes"] = _deep_lists(B._MAX_SPEC_DEPTH - 1)
    out = B.build_app(spec, output_dir=tmp_path / "out")
    assert (out / "compose-manifest.txt").is_file()
    assert "https://shop.example.com/dp/ONE" in asked


def test_a_spec_file_with_an_unknown_key_over_the_bounds_is_refused(tmp_path, monkeypatch):
    """The same bound for a document that arrives as a file: nesting that
    json can still read (600 levels), and a huge string."""
    _link_lint_on(monkeypatch)
    for fragment, value in (("levels deep", _deep_lists(600)), ("characters long", "x" * (B._MAX_SPEC_STRING + 1))):
        spec = _base()
        spec["notes"] = value
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        with pytest.raises(ValueError, match="Spec validation failed: ") as refused:
            B.build_app(str(path), output_dir=tmp_path / "out")
        assert fragment in str(refused.value)
        assert not (tmp_path / "out").exists()


def _run_build_cli(tmp_path: Path, spec: dict):
    """`python -m compose.build <spec>` as a subprocess, the working
    directory a scratch one (the default output is builds/<app_name>)."""
    import os
    import subprocess
    import sys
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONDONTWRITEBYTECODE="1",
               CAS_LINT_EXTERNAL_URLS="skip")
    return subprocess.run(
        [sys.executable, "-m", "compose.build", str(path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_the_command_line_refuses_an_unknown_key_over_the_bounds(tmp_path):
    for fragment, value in (("levels deep", _deep_lists(600)), ("characters long", "x" * (B._MAX_SPEC_STRING + 1))):
        spec = _base()
        spec["notes"] = value
        done = _run_build_cli(tmp_path, spec)
        assert done.returncode != 0, done.stdout
        assert "Spec validation failed: " in done.stderr and fragment in done.stderr, done.stderr[-400:]
        assert not (tmp_path / "builds").exists()
    # The same spec without the key builds, so the refusal above was the key.
    done = _run_build_cli(tmp_path, _base())
    assert done.returncode == 0, done.stderr[-400:]
    assert done.stdout.startswith("Composed to ") and (tmp_path / "builds").is_dir()


# --- gate conditions that only a second layer used to catch ----------------------
# Each of these has a restatement further on (resolve_spec, an emitter) or
# no effect an existing test looked at, so taking the gate's own condition
# out changed no test. The gate is asked directly.

@pytest.mark.parametrize("page_id", ["../x", "Bad Id", "a.b", "x" * 65, ""])
def test_a_page_id_that_is_not_a_slug_is_refused_even_when_no_layout_reads_the_page(page_id):
    """`if not isinstance(page_id, str) or not _PAGE_ID_RE.fullmatch(page_id)`.
    For a faithful-copy page resolve_spec repeats the check; a page nothing
    emits is refused by the gate alone."""
    spec = _base()
    spec["pages"][page_id] = {"note": "no layout reads this"}
    errors = B._spec_safety_errors(BuildSpec.from_dict(spec))
    assert errors == [f"page id is not a safe slug: {page_id!r}"]
    assert _errors(spec) == errors


@pytest.mark.parametrize("entry", [7, "Booking", None, ["name"], True])
def test_a_data_model_that_is_not_an_object_is_refused(entry):
    """`if not isinstance(m, dict): errors.append("data_model entries must be
    objects")`."""
    spec = _base()
    spec["data_models"] = [{"name": "Thing", "fields": [{"name": "title", "sql_type": "TEXT"}]}, entry]
    _refused(spec, "data_model entries must be objects")


def test_two_spec_models_on_one_table_are_refused_by_the_gate_itself():
    """`elif table in seen_tables`. resolve_spec has its own check on the
    merged model list (spec plus archetype); this is the gate's, on the
    spec's own list, and it is the gate that is asked here."""
    spec = _base()
    spec["data_models"] = [
        {"name": "Note", "plural": "notes", "fields": [{"name": "body", "sql_type": "TEXT"}]},
        {"name": "Memo", "plural": "notes", "fields": [{"name": "body", "sql_type": "TEXT"}]},
    ]
    errors = B._spec_safety_errors(BuildSpec.from_dict(spec))
    assert errors == ["data_models 'Note' and 'Memo' map to the same table 'notes'"]


@pytest.mark.parametrize("fields", ["title", 7, {"name": "title"}, True])
def test_model_fields_that_are_not_a_list_are_refused(fields):
    """`if fields is not None and not isinstance(fields, list)`. Skipped
    silently, the model would be built with no columns of its own."""
    _refused(_with_model(fields=fields), "data_model 'Thing' fields must be a list")


@pytest.mark.parametrize("entry", ["title", 7, None, ["name", "title"]])
def test_a_model_field_that_is_not_an_object_is_refused(entry):
    """`if not isinstance(f, dict): errors.append(... field entries must be
    objects)`. Skipped silently, the column would simply be missing."""
    _refused(
        _with_model(fields=[{"name": "title", "sql_type": "TEXT"}, entry]),
        "data_model 'Thing' field entries must be objects",
    )
