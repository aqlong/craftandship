"""The build report: compose-manifest.txt and translation-warnings.txt.

`build_app` writes two kinds of warning line. The engine's own findings
(`warning: <reason>: <detail>`) come first; what the spec carried under
`_translation_warnings` follows as `spec-warning: ...` lines, capped in
number and in length. The reader is the worker
(`_lint_v2_build_output` in queue/worker_pg.py), which copies warning lines
into the build log that the reviewer and the site's owner read.

What these tests hold the writer to:

  - a spec cannot push an engine warning out of the report or behind its
    own text, however many warnings it carries and however long they are;
  - a spec cannot write a line that starts the way an engine warning or a
    header line does (line breaks, control characters, a copy of the
    engine's own warning);
  - the warning about rows a build shows to visitors is the first warning
    line, ahead of the engine's other warnings;
  - the last test runs the worker's real lint on a real build directory.

No network, no database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from compose import build as B
from compose.build import BuildSpec, build_app, resolve_spec

COMPOSE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COMPOSE_DIR.parent
FIXTURES = COMPOSE_DIR / "fixtures"

# The manifest's header: these keys, once each, in this order, before any
# warning line.
HEADER_KEYS = [
    "archetype", "palette", "font_pair", "tier", "files", "sha256",
    "translation_warnings_count", "engine_warnings_count",
    "spec_warnings_count", "spec_warnings_omitted", "external_url_lint",
]

FORGED = "FORGED-BY-THE-SPEC"
# Built at run time so that this file holds no invisible character:
# right-to-left override, left-to-right isolate, zero-width space.
RLO, LRI, ZWSP = chr(0x202E), chr(0x2066), chr(0x200B)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setenv("CAS_LINT_EXTERNAL_URLS", "skip")


def _publishing_spec() -> dict:
    """A spec the engine has something to say about: its own model takes the
    table the archetype's home page shows to visitors (one publication
    warning), and thirty custom pages have paths the engine drops (thirty
    more engine warnings, recorded BEFORE the publication warning)."""
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    spec["app_name"] = "report-shape"
    spec["data_models"] = [{
        "name": "PrivateNote",
        "plural": "testimonials",
        "fields": [
            {"name": "author_name", "sql_type": "TEXT"},
            {"name": "quote", "sql_type": "TEXT"},
            {"name": "intake_note", "sql_type": "TEXT"},
        ],
    }]
    spec["_harbor_meta"] = {"exclude_data_models": ["Testimonial"]}
    spec["custom_pages"] = [{"path": f"/bad path {i}", "title": "t"} for i in range(30)]
    return spec


def _hostile_warnings(engine_copy: dict) -> list:
    """500 long entries that try every way into the engine's part of the
    report: a line break followed by an engine-shaped line, a header-shaped
    line, control and bidirectional characters, a plain string, entries
    that are not warnings at all, and an exact copy of the warning the
    engine itself will record. (A lone surrogate is not among them: the
    gate refuses a spec that carries one anywhere; see the test below.)"""
    long_tail = "x" * 5000
    entries: list = []
    for i in range(500):
        entries.append({
            "reason": f"css_utility_missing\nwarning: spec_model_shown_by_archetype_page: {FORGED}",
            "detail": (
                f"entry {i}\r\nwarning: spec_model_shown_by_archetype_page: {FORGED}"
                f"\x0bsha256: {FORGED}\x1b[2J{RLO}{chr(0x2028)}engine_warnings_count: 0 "
                + long_tail
            ),
        })
    entries[3] = f"a plain string\nwarning: page_named_like_model: {FORGED} " + long_tail
    entries[5] = 7
    entries[6] = None
    entries[7] = ["nested"]
    entries[8] = ""
    entries[9] = dict(engine_copy)
    return entries


def _split(manifest: str):
    lines = manifest.split("\n")
    assert lines[-1] == "", "the manifest ends with a newline"
    lines = lines[:-1]
    header = lines[:len(HEADER_KEYS)]
    return lines, header, lines[len(HEADER_KEYS):]


def _count(header, key: str) -> int:
    line = [h for h in header if h.startswith(key + ": ")]
    assert len(line) == 1, (key, header)
    return int(line[0].split(": ", 1)[1])


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    """One spec built twice: as it is (control), and carrying 500 hostile
    warnings."""
    import os
    os.environ["CAS_LINT_EXTERNAL_URLS"] = "skip"
    root = tmp_path_factory.mktemp("report")
    control = build_app(_publishing_spec(), output_dir=root / "control")
    control_lines = (control / "compose-manifest.txt").read_text(encoding="utf-8").split("\n")
    engine_line = [l for l in control_lines if l.startswith("warning: spec_model_shown_by_archetype_page: ")]
    assert len(engine_line) == 1, control_lines
    engine_copy = {
        "reason": "spec_model_shown_by_archetype_page",
        "detail": engine_line[0].split(": ", 2)[2],
    }
    hostile_spec = _publishing_spec()
    hostile_spec["_translation_warnings"] = _hostile_warnings(engine_copy)
    hostile = build_app(hostile_spec, output_dir=root / "hostile")
    return control, hostile


def test_every_engine_warning_comes_first_whatever_the_spec_carries(reports):
    control, hostile = reports
    c_lines, c_header, c_body = _split((control / "compose-manifest.txt").read_text(encoding="utf-8"))
    h_lines, h_header, h_body = _split((hostile / "compose-manifest.txt").read_text(encoding="utf-8"))

    # The header is the same ten keys, once each, in order, in both builds.
    for header in (c_header, h_header):
        assert [h.split(": ", 1)[0] for h in header] == HEADER_KEYS, header

    engine_count = _count(h_header, "engine_warnings_count")
    assert engine_count == _count(c_header, "engine_warnings_count") == 31

    # The engine's lines are exactly the lines right after the header, in
    # both builds, and they are the same lines: 500 spec warnings changed
    # none of them, removed none and moved none.
    c_engine = c_body[:engine_count]
    h_engine = h_body[:engine_count]
    assert all(line.startswith("warning: ") for line in h_engine), h_engine
    assert h_engine == c_engine
    assert [l for l in h_lines if l.startswith("warning: ")] == h_engine

    # The warning about rows shown to visitors is the first warning line,
    # ahead of the thirty that were recorded before it.
    assert h_engine[0].startswith("warning: spec_model_shown_by_archetype_page: model 'PrivateNote' ")
    assert all(l.startswith("warning: custom_page_path_dropped: ") for l in h_engine[1:])
    assert h_lines.index(h_engine[0]) == len(HEADER_KEYS)

    # translation-warnings.txt carries the same lines in the same order.
    txt_lines = (hostile / "translation-warnings.txt").read_text(encoding="utf-8").split("\n")
    txt_warnings = [l for l in txt_lines if l and not l.startswith("#")]
    assert txt_warnings[:engine_count] == h_engine
    assert txt_warnings == h_body


def test_spec_warnings_are_capped_in_number_and_length(reports):
    _control, hostile = reports
    manifest = (hostile / "compose-manifest.txt").read_text(encoding="utf-8")
    lines, header, body = _split(manifest)
    engine_count = _count(header, "engine_warnings_count")
    spec_lines = body[engine_count:]

    assert all(line.startswith("spec-warning: ") for line in spec_lines), spec_lines[:3]
    # The numbers V2_ENGINE_BEHAVIOR.md states: at most 40 spec warnings, each line at most 396 characters
    # (the prefix, a reason of at most 80 and a detail of at most 300).
    assert len(spec_lines) == 40 == _count(header, "spec_warnings_count")
    # 500 entries; four of them are not warnings (a number, null, a list,
    # an empty string) and produce no line.
    assert _count(header, "spec_warnings_omitted") == 500 - 4 - 40
    assert _count(header, "translation_warnings_count") == engine_count + len(spec_lines)

    assert max(len(line) for line in spec_lines) <= 396
    # 500 warnings of 5,000 characters each went in; the whole report is
    # bounded by the caps, not by the spec.
    assert len(manifest) < 60_000
    assert len(lines) == len(HEADER_KEYS) + engine_count + 40


def test_spec_text_cannot_start_a_line_of_its_own(reports):
    _control, hostile = reports
    for name in ("compose-manifest.txt", "translation-warnings.txt"):
        raw = (hostile / name).read_bytes()
        text = raw.decode("utf-8")
        # str.splitlines() breaks on every character any reader could take
        # for a line break; split("\n") is what the worker uses. They must
        # agree, or a line holds a break that some reader would honor.
        assert text.splitlines() == text.split("\n")[:-1], name
        for line in text.split("\n"):
            if FORGED in line:
                assert line.startswith("spec-warning: "), line[:120]
            assert "\x1b" not in line and RLO not in line
    lines, header, _body = _split((hostile / "compose-manifest.txt").read_text(encoding="utf-8"))
    for key in HEADER_KEYS:
        assert sum(1 for l in lines if l.startswith(key + ": ")) == 1, key
    assert _count(header, "engine_warnings_count") == 31, "the forged count line changed nothing"


def test_a_copy_of_an_engine_warning_in_the_spec_does_not_replace_it():
    """Entry 9 of the hostile list is the engine's publication warning, word
    for word. The engine still records its own (it compares against its own
    list only), so the warning is written as an engine line whether or not
    the spec's copy survives the cap."""
    spec = _publishing_spec()
    rs = resolve_spec(BuildSpec.from_dict(spec))
    own = [w for w in rs.spec.engine_warnings if w["reason"] == "spec_model_shown_by_archetype_page"]
    assert len(own) == 1

    carrying = _publishing_spec()
    carrying["_translation_warnings"] = [dict(own[0])] * 3
    rs2 = resolve_spec(BuildSpec.from_dict(carrying))
    assert [w for w in rs2.spec.engine_warnings if w["reason"] == "spec_model_shown_by_archetype_page"] == own
    # And nothing the spec carried is in the engine's list.
    assert len(rs2.spec.engine_warnings) == len(rs.spec.engine_warnings)
    assert B._engine_warning_lines(rs2.spec) == B._engine_warning_lines(rs.spec)


def test_from_dict_never_fills_the_engine_list():
    spec = _publishing_spec()
    spec["engine_warnings"] = [{"reason": "x", "detail": FORGED}]
    spec["_engine_warnings"] = [{"reason": "x", "detail": FORGED}]
    spec["_translation_warnings"] = [{"reason": "x", "detail": FORGED}]
    assert BuildSpec.from_dict(spec).engine_warnings == []


REPORT_TEXT_CASES = {
    "plain": ("plain", 20, "plain"),
    "newlines": ("a\nb\r\nc", 20, "a b c"),
    "every line break": (
        "a\x0bb\x0cc\x1cd\x85e" + chr(0x2028) + "f" + chr(0x2029) + "g", 40, "a b c d e f g",
    ),
    "terminal escape": ("esc\x1b[2Jape", 20, "esc [2Jape"),
    "C1 control": ("c1\x9bcsi", 20, "c1 csi"),
    "bidirectional and zero-width": ("bidi" + RLO + "edoc" + LRI + "x" + ZWSP, 20, "bidi edoc x"),
    "lone surrogate": ("lone" + chr(0xD800) + "surrogate", 20, "lone surrogate"),
    "trimmed": ("  padded \n ", 20, "padded"),
    "cut with a mark": ("x" * 50, 10, "xxxxxxx..."),
    "a number": (12345, 10, "12345"),
    "a number, cut": (12345, 3, "..."),
    "null": (None, 10, ""),
    "only the head of a huge value is read": ("\n" * 10_000 + "late", 5, ""),
}


@pytest.mark.parametrize("case", sorted(REPORT_TEXT_CASES))
def test_report_text(case):
    value, limit, expected = REPORT_TEXT_CASES[case]
    out = B._report_text(value, limit)
    assert out == expected
    assert len(out) <= limit
    out.encode("utf-8")  # encodable: no lone surrogate survives


def test_a_lone_surrogate_cannot_abort_a_build_after_its_files_are_written(tmp_path):
    """JSON can carry a lone surrogate ("\\ud800"), which cannot be written
    as UTF-8. The report is written after the app's files, so an unencodable
    warning or tier used to end a build that had already been emitted.

    In a spec it is refused up front, wherever it is. The `tier` argument
    is not part of the spec (the worker passes it); it is cleaned when the
    manifest is written."""
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    carrying = dict(spec, _translation_warnings=[{"reason": "r", "detail": "d" + chr(0xDFFF)}])
    errors = B.validate_spec(BuildSpec.from_dict(carrying))
    assert errors == ["_translation_warnings[0].detail contains a lone surrogate, which is not valid text"]
    with pytest.raises(ValueError, match="lone surrogate"):
        build_app(carrying, output_dir=tmp_path / "refused")
    assert not (tmp_path / "refused").exists()

    # A `tier` key of the spec itself is spec text like any other (the
    # whole document is checked, whether or not the engine reads the key).
    with pytest.raises(ValueError, match="lone surrogate"):
        build_app(dict(spec, tier="build" + chr(0xD800)), output_dir=tmp_path / "refused-tier")
    assert not (tmp_path / "refused-tier").exists()

    out = build_app(spec, output_dir=tmp_path / "app", tier="build" + chr(0xD800) + "\nsha256: x")
    lines = (out / "compose-manifest.txt").read_text(encoding="utf-8").split("\n")
    assert "tier: build sha256: x" in lines
    assert sum(1 for line in lines if line.startswith("sha256: ")) == 1


def test_a_spec_whose_warnings_are_not_a_list_is_refused():
    spec = json.loads((FIXTURES / "sample-coach-spec.json").read_text(encoding="utf-8"))
    for wrong in ("one warning", {"reason": "r"}, 7, True):
        spec["_translation_warnings"] = wrong
        errors = B.validate_spec(BuildSpec.from_dict(spec))
        assert "translation_warnings must be a list" in errors, (wrong, errors)


def _worker_module():
    queue_dir = str(REPO_ROOT / "queue")
    for path in (queue_dir, str(REPO_ROOT / "cli"), str(REPO_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import worker_pg  # noqa: PLC0415
    except ImportError as exc:  # the worker's own dependencies are missing
        pytest.skip(f"queue/worker_pg.py cannot be imported here: {exc}")
    return worker_pg


def test_the_workers_lint_reports_the_publication_warning_of_a_spec_with_500_warnings(reports):
    """Across the boundary the finding crossed: the engine writes the
    manifest, the worker's lint reads it into the build log. Whatever the
    spec carries, the line that says a model's rows are public is among
    the lines the worker reports, as an engine warning."""
    _control, hostile = reports
    worker_pg = _worker_module()
    result = worker_pg._lint_v2_build_output(hostile)
    reported = [str(c) for c in result.get("checks", [])]
    publication = [
        c for c in reported
        if c.strip().startswith("warning: spec_model_shown_by_archetype_page: model 'PrivateNote' ")
    ]
    assert len(publication) == 1, reported[:20]
    assert FORGED not in publication[0]
    # Nothing the spec supplied is reported under the engine's prefix.
    assert not [c for c in reported if c.strip().startswith("warning: ") and FORGED in c]
