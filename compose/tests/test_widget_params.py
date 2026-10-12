"""What a page widget takes from a spec: the `params` declared in
compose/data/widget-registry.json and `_spec_widget_data`, which applies
them.

The widget templates call string and list methods on their parameters
(`phone.replace(...)`, `items.forEach(...)`). A spec is JSON written by a
model, so any value can stand where a string or a list was meant; a view
that receives it throws when the page is requested, which the site's
/health used not to notice. The rule these tests hold the engine to: a
template is only ever given a parameter it declares, in the shape it
declares, and a spec that cannot be put into that shape is refused.

The rendering tests run the real templates with the real EJS
(web/node_modules/ejs); they are skipped without node or that module. The
tests of `_spec_widget_data` itself need neither.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, resolve_spec, validate_spec

COMPOSE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COMPOSE_DIR.parent
FIXTURES = COMPOSE_DIR / "fixtures"
EJS_DIR = REPO_ROOT / "web" / "node_modules" / "ejs"
NODE = shutil.which("node")
RENDERER = Path(__file__).resolve().parent / "_render_widgets.js"

needs_ejs = pytest.mark.skipif(
    NODE is None or not EJS_DIR.exists(),
    reason="needs node and web/node_modules/ejs (run `cd web && npm ci`)",
)


def _layout_widget_ids() -> list:
    ids = set()
    for layout in B.load_layouts()["layouts"].values():
        for w in layout.get("widgets") or []:
            ids.add(w["id"])
    return sorted(ids)


LAYOUT_WIDGETS = _layout_widget_ids()
SHAPE_WORDS = {"text", "flag", "number", "text_or_list"}

# Every JSON type, both empty containers, and a key that is not there.
MISSING = "<the key is absent>"
WRONG = {
    "integer": 5125550100,
    "fraction": 2.5,
    "true": True,
    "false": False,
    "null": None,
    "empty list": [],
    "list": ["a", 1, None],
    "list of objects": [{"x": 1}, {}],
    "empty object": {},
    "object": {"x": 1, "length": 2},
    "empty string": "",
    "string": "plain",
    "long string": "long " * 4000,
    "absent": MISSING,
}

# Names a template may read that no spec supplies: the page's own locals,
# the rows and flags the engine adds to a widget that reads a table, the
# contact form's inputs and the values of a rejected submission.
ENGINE_SUPPLIED = {
    "settings", "csrfToken", "cspNonce", "currentPath", "user", "nav_items", "locals",
    # what EJS itself passes to a compiled template
    "escapeFn", "include", "rethrow",
    "items", "detail_path_prefix", "expand_inline", "form_fields", "preselected_service",
    "submitted", "flashSuccess", "flashError",
}
# Second names a few templates fall back to when `items` is absent. Nothing
# supplies them from a spec (they are not declared), so they stay unset.
UNSUPPLIED_ALTERNATES = {"rows", "books", "sections"}


def _valid_shape(shape) -> bool:
    if isinstance(shape, str):
        return shape in SHAPE_WORDS
    if isinstance(shape, list):
        return len(shape) == 1 and _valid_shape(shape[0])
    if isinstance(shape, dict):
        return all(isinstance(k, str) and _valid_shape(v) for k, v in shape.items())
    return False


def test_every_widget_a_layout_places_declares_its_parameters():
    assert len(LAYOUT_WIDGETS) >= 28
    for widget_id in LAYOUT_WIDGETS:
        widget = B.get_widget(widget_id)
        params = widget.get("params")
        assert isinstance(params, dict), f"{widget_id} declares no params"
        assert _valid_shape(params), f"{widget_id}: a shape is not in the notation build.py reads"
        for name in widget.get("required_params") or []:
            assert name in params, f"{widget_id}: required parameter {name!r} is not declared"


def _sample(shape):
    if shape == "text":
        return "Sample text"
    if shape == "flag":
        return True
    if shape == "number":
        return 3
    if shape == "text_or_list":
        return ["one", "two"]
    if isinstance(shape, list):
        return [_sample(shape[0]), _sample(shape[0])]
    return {key: _sample(sub) for key, sub in shape.items()}


def _paths(shape, prefix=()):
    """Every position of a sample built from `shape`: each key, and the
    first element of each list, containers included."""
    if isinstance(shape, dict):
        for key, sub in shape.items():
            yield prefix + (key,)
            yield from _paths(sub, prefix + (key,))
    elif isinstance(shape, list):
        yield prefix + (0,)
        yield from _paths(shape[0], prefix + (0,))


def _put(data, path, value):
    node = data
    for part in path[:-1]:
        node = node[part]
    if value is MISSING:
        if isinstance(node, dict):
            node.pop(path[-1], None)
        else:
            del node[path[-1]]
    else:
        node[path[-1]] = value


SAMPLE_ROW = {
    "id": 1, "title": "Sample row", "name": "Sample row", "slug": "sample-row", "description": "Text.",
    "body": "Text.", "quote": "A quote.", "author_name": "Sam Sample", "published": True,
    "image_url": "/images/stock/placeholder.jpg", "cover_image_url": "/images/stock/placeholder.jpg",
    "day_of_week": "Monday", "start_time": "09:00", "city": "Sample City", "rating": 5,
    "section_name": "Mains", "price_display": "$10", "created_at": "2030-01-15T00:00:00Z",
}


def _locals_for(widget: dict, data: dict) -> dict:
    """What a page view hands the widget: the spec's data in its declared
    shape, plus rows when the widget reads them from a table."""
    out = dict(data)
    if "items" not in (widget.get("params") or {}):
        out["items"] = [dict(SAMPLE_ROW), dict(SAMPLE_ROW, id=2)]
    return out


def _render(jobs: list, tmp_path: Path) -> dict:
    jobs_path = tmp_path / "jobs.json"
    out_path = tmp_path / "out.json"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(RENDERER), str(jobs_path), str(out_path)],
        capture_output=True, text=True, timeout=300,
        env={"PATH": "/usr/bin:/bin", "NODE_PATH": str(EJS_DIR.parent)},
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return {r["id"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))}


@needs_ejs
def test_a_template_reads_nothing_from_a_spec_that_its_widget_does_not_declare(tmp_path):
    """Each template is run with every declared parameter present and with
    none, and every name it looks up among its locals is collected. A name
    that is not declared, and that the engine does not supply, would be a
    parameter the contract does not know: it could never arrive, or (worse)
    would have to be let through unchecked."""
    jobs = []
    for widget_id in LAYOUT_WIDGETS:
        widget = B.get_widget(widget_id)
        file = str(COMPOSE_DIR / widget["file"])
        full, errors = B._spec_widget_data(widget_id, widget, _sample(widget["params"]), "pages.p.k")
        assert errors == [], (widget_id, errors)
        jobs.append({"id": widget_id + "|full", "file": file, "data": _locals_for(widget, full)})
        jobs.append({"id": widget_id + "|bare", "file": file, "data": {}})
    results = _render(jobs, tmp_path)
    for widget_id in LAYOUT_WIDGETS:
        params = set(B.get_widget(widget_id)["params"])
        reads = set()
        for variant in ("full", "bare"):
            result = results[f"{widget_id}|{variant}"]
            assert result["ok"], (widget_id, variant, result["error"])
            reads.update(name for name in result["reads"] if not name.startswith("_"))
        unknown = reads - params - ENGINE_SUPPLIED - UNSUPPLIED_ALTERNATES
        assert not unknown, f"{widget_id} reads {sorted(unknown)}, which its params do not declare"


@needs_ejs
def test_no_value_of_any_json_type_at_any_declared_position_breaks_a_template(tmp_path):
    """The whole product, per widget: every position of its declared
    parameters x every wrong value. `_spec_widget_data` either refuses the
    data or returns data the template renders."""
    jobs = []
    refused = 0
    cases = 0
    for widget_id in LAYOUT_WIDGETS:
        widget = B.get_widget(widget_id)
        file = str(COMPOSE_DIR / widget["file"])
        for path in _paths(widget["params"]):
            for value_name, value in WRONG.items():
                cases += 1
                raw = _sample(widget["params"])
                _put(raw, path, copy.deepcopy(value) if value is not MISSING else MISSING)
                data, errors = B._spec_widget_data(widget_id, widget, raw, "pages.p.k")
                if errors:
                    refused += 1
                    continue
                where = ".".join(str(part) for part in path)
                jobs.append({
                    "id": f"{widget_id}|{where}|{value_name}", "file": file,
                    "data": _locals_for(widget, data),
                })
        # The widget's data as a whole: empty, and of every wrong type.
        for value_name, value in WRONG.items():
            if value is MISSING:
                continue
            cases += 1
            data, errors = B._spec_widget_data(widget_id, widget, copy.deepcopy(value), "pages.p.k")
            if errors:
                refused += 1
                continue
            jobs.append({"id": f"{widget_id}|<all>|{value_name}", "file": file, "data": _locals_for(widget, data)})
    results = _render(jobs, tmp_path)
    broken = [(job_id, r["error"]) for job_id, r in results.items() if not r["ok"]]
    assert not broken, f"{len(broken)} of {len(jobs)} accepted cases throw at render: {broken[:8]}"
    # Not vacuous: thousands of cases, a good share refused and a good share rendered.
    assert cases > 3000 and refused > 800 and len(jobs) > 800, (cases, refused, len(jobs))


HERO = "hero-centered"


def _data(widget_id: str, raw):
    return B._spec_widget_data(widget_id, B.get_widget(widget_id), raw, "pages.home.x")


def test_a_number_where_text_is_declared_becomes_its_text():
    data, errors = _data("hours-location-block", {"phone": 5125550100, "address": 2.5, "email": 7.0})
    assert errors == []
    assert data == {"phone": "5125550100", "address": "2.5", "email": "7"}


@pytest.mark.parametrize("value, kind", [
    (True, "a boolean"), (["a"], "a list"), ({"a": 1}, "an object"), (float("nan"), "a number"),
])
def test_what_cannot_be_text_is_refused_with_its_position(value, kind):
    data, errors = _data("hours-location-block", {"phone": value})
    assert errors == [f"pages.home.x.phone must be text, not {kind}"]
    assert "phone" not in data


def test_null_and_empty_string_mean_absent_where_text_is_not_declared():
    data, errors = _data(HERO, {
        "heading": None, "eyebrow": "", "cta_primary": "", "cta_secondary": None,
        "credential_lines": "", "compact": "", "drop_cap": None,
    })
    assert errors == []
    # An empty string is still text; everything else here is simply absent.
    assert data == {"eyebrow": ""}


def test_a_key_the_widget_does_not_declare_never_reaches_the_view():
    data, errors = _data(HERO, {"heading": "H", "onload": "x", "items": [1, 2], "__proto__": {"a": 1}})
    assert errors == []
    assert data == {"heading": "H"}


def test_the_specs_own_key_order_is_kept():
    data, _ = _data(HERO, {"description": "d", "heading": "h", "eyebrow": "e"})
    assert list(data) == ["description", "heading", "eyebrow"]


def test_list_and_object_positions_are_named_in_the_refusal():
    data, errors = _data("offerings-grid-3col", {
        "heading": "H",
        "items": [{"title": "ok"}, None, "a string", {"title": ["x"], "cta_href": {"a": 1}}],
    })
    assert errors == [
        "pages.home.x.items[1] is null",
        "pages.home.x.items[2] must be an object, not a string",
        "pages.home.x.items[3].title must be text, not a list",
        "pages.home.x.items[3].cta_href must be text, not an object",
    ]
    data, errors = _data("offerings-grid-3col", {"items": "all of them"})
    assert errors == ["pages.home.x.items must be a list, not a string"]
    data, errors = _data(HERO, {"cta_primary": ["Book", "/book"], "compact": "yes"})
    assert errors == [
        "pages.home.x.cta_primary must be an object, not a list",
        "pages.home.x.compact must be true or false, not a string",
    ]


def test_text_or_list_takes_either():
    widget_id = "pricing-tier-table"
    data, errors = _data(widget_id, {"items": [{"features": "one\ntwo"}, {"features": ["a", 2]}, {"features": {"a": 1}}]})
    assert errors == ["pages.home.x.items[2].features must be text, not an object"]
    assert data["items"][:2] == [{"features": "one\ntwo"}, {"features": ["a", "2"]}]


@pytest.mark.parametrize("raw", [7, True, "text", ["a"]])
def test_widget_data_that_is_not_an_object_is_refused(raw):
    data, errors = _data(HERO, raw)
    assert data == {} and len(errors) == 1
    assert errors[0].startswith("pages.home.x must be an object (the data of the hero-centered widget), not ")


def test_a_required_parameter_is_given_its_empty_value():
    required = {
        widget_id: B.get_widget(widget_id).get("required_params") or []
        for widget_id in LAYOUT_WIDGETS
    }
    assert any(required.values()), "no widget declares a required parameter"
    for widget_id, names in required.items():
        data, errors = _data(widget_id, {})
        assert errors == []
        assert data == {name: "" for name in names}, widget_id


def _spec(fixture: str = "sample-restaurant-spec.json") -> dict:
    return json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))


def test_a_spec_whose_page_data_does_not_fit_is_refused_by_validate_spec():
    """The finding this file exists for: `visit.phone` as a JSON number
    built, and the home page threw at render. A number is now written as
    text; what has no reading as text is refused, by validate_spec, with
    the position."""
    spec = _spec()
    spec["pages"]["home"]["visit"]["phone"] = 5125550100
    assert validate_spec(BuildSpec.from_dict(copy.deepcopy(spec))) == []
    rs = resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    _path, view = B.emit_page_view(rs, "home", rs.page_layouts["home"])
    assert '"phone": "5125550100"' in view

    spec["pages"]["home"]["visit"]["phone"] = ["512", "555", "0100"]
    errors = validate_spec(BuildSpec.from_dict(copy.deepcopy(spec)))
    assert errors == ["pages.home.visit.phone must be text, not a list"]
    with pytest.raises(ValueError, match="Spec validation failed: pages.home.visit.phone must be text"):
        resolve_spec(BuildSpec.from_dict(copy.deepcopy(spec)))


@pytest.mark.parametrize("page", [7, "home", ["x"], True])
def test_a_page_that_is_not_an_object_is_refused(page):
    spec = _spec()
    spec["pages"]["home"] = page
    errors = validate_spec(BuildSpec.from_dict(spec))
    assert errors == ["pages.home must be an object"]
    # The same for a page no layout of this build reads: nothing resolves
    # its widgets, so only the gate's own check can refuse it.
    spec = _spec()
    spec["pages"]["notes_for_later"] = page
    assert validate_spec(BuildSpec.from_dict(spec)) == ["pages.notes_for_later must be an object"]


def test_the_emitter_refuses_page_data_that_does_not_fit_whoever_built_the_resolved_spec():
    """resolve_spec refuses such a spec, so an emitter never sees one from
    it. The view emitter checks again for itself: a ResolvedSpec put
    together some other way gets the same refusal, not a broken view."""
    spec = _spec()
    rs = resolve_spec(BuildSpec.from_dict(spec))
    rs.spec.pages["home"]["visit"]["hours"] = "nine to five"
    with pytest.raises(ValueError, match="Spec validation failed: pages.home.visit.hours must be a list"):
        B.emit_page_view(rs, "home", rs.page_layouts["home"])


def test_validate_spec_leaves_the_spec_it_was_given_alone():
    """validate_spec runs resolve_spec to find the refusals that need the
    resolved layouts. resolve_spec records engine warnings and re-keys
    faithful-copy pages; neither may show on the caller's spec."""
    spec_dict = _spec("sample-coach-spec.json")
    spec_dict["custom_pages"] = [{"path": "/bad path", "title": "t"}]
    spec_dict["pages"]["story"] = {"_copy_blocks": ["# Our story", "Text."], "_copy_source_url": "https://example.com/"}
    spec = BuildSpec.from_dict(spec_dict)
    pages_before = dict(spec.pages)
    assert validate_spec(spec) == []
    assert spec.engine_warnings == []
    assert spec.pages == pages_before and "copy" not in spec.pages["story"]
    # resolve_spec itself does both, on the spec it is given.
    resolve_spec(spec)
    assert [w["reason"] for w in spec.engine_warnings] == ["custom_page_path_dropped"]
    assert "copy" in spec.pages["story"]
