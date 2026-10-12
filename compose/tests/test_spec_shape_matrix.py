"""The wrong-type matrix: whatever JSON value stands at any position of a
spec, the engine refuses the spec or builds a site that works.

A spec is written by a model. A number where a string was meant, a string
where a list was meant, a null, a missing key: none of these is an attack,
all of them happen, and each used to end in one of two bad ways. Either an
emitter fell over with a traceback after `validate_spec` had returned
nothing, or the build went through and the page threw when a visitor asked
for it (and the site's /health, which the deploy is judged by, said ok).

`compose/tests/_spec_matrix.py` enumerates the cases: every position of
ten sample specs (laid over with one of everything else a spec can
declare) x every wrong value, 16 of them, including "the key is not
there" and two valid column types (a text type and a number type, for the
positions that hold one). For each case `validate_spec` must return errors, or the engine
must compose a site of which `_site_harness.js` can parse every script,
compile every view and render every GET route as a visitor and as the
admin. Nothing in between.

The full product is tens of thousands of cases and takes the better part
of an hour, so the tests that always run take a fixed sample of it: the
cases whose stable hash is 0 modulo `SAMPLE_ONE_IN`. Set
CAS_SPEC_MATRIX=full to run every case (or CAS_SPEC_MATRIX=<n> for one
case in n). A second matrix does the same to v1-shaped specs, which the
worker passes through compose.intake.translate_v1_spec first.

What the main matrix does not reach: rows are a stand-in (one plausible row
per table), so what a spec's own seed rows do to a page that reads them from
the database is not rendered here, and no SQL is run: a column type the
engine cannot work with builds a site that looks right. Two tests below run
the cases on a real PostgreSQL instead, with every built site's own schema
and seed applied and its statements executed (the columns the engine reads
with a fixed meaning, by name; and a smaller fixed sample of the whole
product, `POSTGRES_SAMPLE_ONE_IN`); they need a local PostgreSQL
(`_scratch_pg.py`). POST handlers are parsed but not called; a value is
wrong in one position at a time.

Needs node and web/node_modules; skipped without them.
"""

from __future__ import annotations

import copy
import os

import pytest

from compose import build as B
from compose.tests import _site_check as SC
from compose.tests import _spec_matrix as M
from compose.tests._scratch_pg import scratch_database_url

pytestmark = pytest.mark.skipif(not SC.AVAILABLE, reason=SC.UNAVAILABLE_REASON)

SAMPLE_ONE_IN = 120
V1_SAMPLE_ONE_IN = 20


def _one_in(default: int = SAMPLE_ONE_IN) -> int:
    choice = os.environ.get("CAS_SPEC_MATRIX", "").strip().lower()
    if choice == "full":
        return 1
    if choice.isdigit() and int(choice) >= 1:
        return int(choice)
    return default


def _report(summary: dict) -> str:
    lines = [f"counts: {summary['counts']}"]
    for case, detail in summary["crashes"][:15]:
        lines.append(f"CRASH   {M.describe(case)[:150]} -> {detail[:200]}")
    for case, problems in summary["broken"][:15]:
        lines.append(f"BROKEN  {M.describe(case)[:150]} -> {problems[0][:200]}")
    return "\n".join(lines)


def test_the_case_list_covers_every_part_of_a_spec():
    cases = M.all_cases()
    assert len(cases) > 20_000
    tops = {case[1][0] for case in cases}
    assert {
        "app_name", "archetype", "palette", "font_pair", "admin_email", "settings", "pages",
        "seed_content", "data_models", "custom_pages", "user_roles", "nav_items", "admin_nav_items",
        "_harbor_meta", "_translation_warnings", "template_family_id", "readability_bump", "tier",
    } <= tops
    assert {case[0] for case in cases} == set(M.FIXTURE_NAMES)
    assert {case[2] for case in cases} == set(M.WRONG_VALUES)
    # The sample is a fixed subset: the same cases on every run.
    assert M.sample_cases(cases, SAMPLE_ONE_IN) == M.sample_cases(list(reversed(cases)), SAMPLE_ONE_IN)[::-1]


def test_a_wrong_value_at_any_position_is_refused_or_builds_a_site_that_works(tmp_path):
    cases = M.all_cases()
    one_in = _one_in()
    chosen = cases if one_in == 1 else M.sample_cases(cases, one_in)
    assert len(chosen) >= 200, "the sample is too small to mean anything"
    summary = M.run_cases(chosen, tmp_path / "matrix")
    counts = summary["counts"]
    print(f"spec matrix: {len(chosen)} of {len(cases)} cases: {counts}")
    assert not summary["crashes"] and not summary["broken"], _report(summary)
    assert counts[M.REFUSED] + counts[M.BUILT] == len(chosen)
    # Both outcomes occur, and sites really were built and exercised.
    assert counts[M.REFUSED] >= len(chosen) // 10, counts
    assert counts["checked"] >= len(chosen) // 20, counts


POSTGRES_SAMPLE_ONE_IN = 800
# The positions of the extras that the engine reads with a fixed meaning (the
# `Entry` model of EXTRAS: `id`, `created_at`, `updated_at`, `published_at`, a
# slug, the link to its parent) and of its seed row, with every wrong value of
# the kind that is a column type or a value of one.
ENGINE_COLUMN_POSITIONS = [
    ("data_models", 1, "fields", 0, "sql_type"),      # id
    ("data_models", 1, "fields", 2, "sql_type"),      # slug
    ("data_models", 1, "fields", 4, "sql_type"),      # created_at
    ("data_models", 1, "fields", 5, "sql_type"),      # updated_at
    ("data_models", 1, "fields", 6, "sql_type"),      # published_at
    ("data_models", 1, "fields", 7, "sql_type"),      # the link to the parent
    ("data_models", 1, "fields", 8, "sql_type"),      # published
    ("data_models", 1, "belongs_to_field"),
    ("seed_content", "entries", 0, "slug"),
    ("seed_content", "entries", 0, "published_at"),
    ("seed_content", "entries", 0, "resource_ref"),
    ("seed_content", "entries", 0, "published"),
]


def _engine_column_cases():
    cases = []
    for path in ENGINE_COLUMN_POSITIONS:
        for value_name in ("text column type", "number column type", "integer", "true", "other string", "empty string",
                           "null", "absent", "long string"):
            cases.append(("sample-coach-spec.json", path, value_name))
    return cases


def test_a_wrong_value_at_a_column_the_engine_reads_is_refused_or_builds_a_site_that_works_on_postgres(tmp_path):
    """The same rule, on a real PostgreSQL, for the columns whose meaning the
    engine fixes: the stand-in database answers any SELECT, so a VARCHAR
    `created_at` (the dashboard compares it with NOW() - INTERVAL) or an
    integer slug (compared with a text parameter) builds a site that looks
    right and fails on its first request to a database."""
    with scratch_database_url() as database_url:
        summary = M.run_cases(_engine_column_cases(), tmp_path / "columns", database_url=database_url)
    counts = summary["counts"]
    print(f"engine column matrix: {counts}")
    assert not summary["crashes"] and not summary["broken"], _report(summary)
    assert counts[M.REFUSED] > 0 and counts["checked"] > 0, counts


def test_a_sample_of_the_matrix_on_postgres(tmp_path):
    """A fixed sample of the whole matrix (one case in `POSTGRES_SAMPLE_ONE_IN`, or
    the share CAS_SPEC_MATRIX names) with every built site given its own schema
    and rows in a real database, where a seed value or a default that
    Postgres refuses stops the site's first boot."""
    cases = M.all_cases()
    one_in = _one_in(POSTGRES_SAMPLE_ONE_IN)
    chosen = cases if one_in == 1 else M.sample_cases(cases, one_in)
    assert len(chosen) >= 20, "the sample is too small to mean anything"
    with scratch_database_url() as database_url:
        summary = M.run_cases(chosen, tmp_path / "pg-matrix", database_url=database_url)
    counts = summary["counts"]
    print(f"spec matrix on PostgreSQL: {len(chosen)} of {len(cases)} cases: {counts}")
    assert not summary["crashes"] and not summary["broken"], _report(summary)
    assert counts[M.REFUSED] + counts[M.BUILT] == len(chosen)


def test_a_wrong_value_in_a_v1_spec_is_refused_or_builds_a_site_that_works(tmp_path):
    """The same rule one step earlier. The worker hands a v1-shaped spec to
    compose.intake.translate_v1_spec before the engine sees it, so that
    function reads the model's JSON first. Every position of the three v1
    fixtures and of one carrying everything the web translator can add
    (seeded settings, services, testimonials, copied pages, hints) x every
    wrong value: the translator refuses with the validator's wording, or
    the engine refuses, or the site that is built works. An exception of
    any other kind out of the translator is a failure."""
    cases = M.all_v1_cases()
    assert len(cases) > 2_000
    assert {case[0] for case in cases} == set(M.V1_FIXTURE_NAMES) | {M.V1_RICH}
    one_in = _one_in(V1_SAMPLE_ONE_IN)
    chosen = cases if one_in == 1 else M.sample_cases(cases, one_in)
    assert len(chosen) >= 100
    summary = M.run_cases(chosen, tmp_path / "v1", base_of=M.v1_base_spec, to_spec=M.v1_to_spec)
    counts = summary["counts"]
    print(f"v1 spec matrix: {len(chosen)} of {len(cases)} cases: {counts}")
    assert not summary["crashes"] and not summary["broken"], _report(summary)
    assert counts[M.REFUSED] + counts[M.BUILT] == len(chosen)
    assert counts[M.REFUSED] >= len(chosen) // 10 and counts["checked"] >= 5, counts


# The cases the review reported, by name, whatever the sample holds.
REPORTED = [
    ("sample-restaurant-spec.json", ("pages", "home", "visit", "phone"), "integer"),
    ("sample-restaurant-spec.json", ("pages", "home", "visit", "phone"), "list"),
    ("sample-restaurant-spec.json", ("pages", "home", "visit", "hours"), "other string"),
    ("sample-healer-spec.json", ("pages", "home"), "integer"),
    ("sample-healer-spec.json", ("pages", "home", "hero"), "true"),
    ("sample-healer-spec.json", ("pages", "work-with-me", "offerings"), "fraction"),
    ("sample-healer-spec.json", ("pages", "work-with-me", "offerings", "heading"), "absent"),
    ("sample-coach-spec.json", ("custom_pages", 0, "allowed_fields"), "true"),
    ("sample-coach-spec.json", ("settings", "service_options"), "integer"),
    ("sample-coach-spec.json", ("settings", "site_url"), "huge string"),
    ("sample-coach-spec.json", ("data_models", 0, "fields", 2, "options"), "other string"),
]


def test_the_reported_cases(tmp_path):
    summary = M.run_cases(REPORTED, tmp_path / "reported")
    assert not summary["crashes"] and not summary["broken"], _report(summary)
    assert summary["counts"][M.REFUSED] + summary["counts"][M.BUILT] == len(REPORTED)


def test_the_harness_notices_a_page_that_throws(tmp_path):
    """The other direction: a built site whose home view throws must be
    reported as broken, or a clean matrix would prove nothing. The page
    data check is taken out of the way (as an emitter with no contract
    would behave) and a number is put where the view calls a string
    method."""
    spec = M.base_spec("sample-restaurant-spec.json")
    outcome, _detail, rs, files = M.compose_case(copy.deepcopy(spec))
    assert outcome == M.BUILT
    file_map = dict(files)
    view = file_map["views/home.ejs"]
    assert '"phone": "' in view
    start = view.index('"phone": "')
    end = view.index('"', start + len('"phone": "'))
    file_map["views/home.ejs"] = view[:start] + '"phone": 5125550100' + view[end + 1:]
    app_dir = tmp_path / "broken"
    SC.write_files(app_dir, file_map.items())
    result = SC.check_sites([{"id": "broken", "dir": app_dir, "tables": SC.tables_for(rs)}], tmp_path / "run")
    problems = SC.problems_of(result["broken"])
    assert any("GET / as visitor -> 500: phone.replace is not a function" in p for p in problems), problems

    # And one that does not parse.
    file_map["views/home.ejs"] = view
    file_map["routes/pages.js"] = file_map["routes/pages.js"] + "\nconst = ;\n"
    app_dir = tmp_path / "unparsed"
    SC.write_files(app_dir, file_map.items())
    result = SC.check_sites([{"id": "unparsed", "dir": app_dir, "tables": SC.tables_for(rs)}], tmp_path / "run2")
    problems = SC.problems_of(result["unparsed"])
    assert any(p.startswith("does not parse: routes/pages.js") for p in problems), problems


def test_node_check_agrees_with_the_harness_about_every_emitted_script(tmp_path):
    """The harness parses scripts in-process (the CommonJS wrapper plus
    vm.Script). `node --check`, run on the same files one process at a
    time, is the reference; they must agree on a built site, and on one
    with a syntax error planted in it."""
    import subprocess

    spec = M.base_spec("sample-coach-spec.json")
    outcome, _detail, rs, files = M.compose_case(copy.deepcopy(spec))
    assert outcome == M.BUILT
    app_dir = tmp_path / "site"
    SC.write_files(app_dir, files)
    scripts = sorted(p for p in app_dir.rglob("*.js"))
    assert len(scripts) >= 25
    for script in scripts:
        proc = subprocess.run([SC.NODE, "--check", str(script)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (script, proc.stderr[-300:])
    result = SC.check_sites([{"id": "site", "dir": app_dir, "tables": SC.tables_for(rs)}], tmp_path / "run")
    assert result["site"]["parse"] == []

    bad = app_dir / "middleware" / "csrf.js"
    bad.write_text(bad.read_text(encoding="utf-8") + "\nfunction (\n", encoding="utf-8")
    assert subprocess.run([SC.NODE, "--check", str(bad)], capture_output=True, timeout=60).returncode != 0
    result = SC.check_sites([{"id": "site", "dir": app_dir, "tables": SC.tables_for(rs)}], tmp_path / "run2")
    assert [p["file"] for p in result["site"]["parse"]] == ["middleware/csrf.js"]
