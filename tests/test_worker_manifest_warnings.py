"""The worker's reading of compose-manifest.txt: which warning lines of a v2
build reach the build log.

Covers `_read_manifest`, `_manifest_warning_checks` and the manifest part of
`_lint_v2_build_output` in queue/worker_pg.py:
- every engine warning (`warning: `) is copied, wherever it stands in the file
  and however many lines come before it;
- spec-supplied lines (`spec-warning: `) are copied after the engine's;
- a long list is packed, not dropped; an overlong line is cut and marked;
- the manifest is read up to a byte cap, and a larger one is a lint error;
- an `engine_warnings_count` header that disagrees with the lines found is a
  lint error;
- the same holds for a manifest written by the real compose engine.

No database and no network: files are written under tmp_path and the engine
is run in process.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
QUEUE_DIR = str(ROOT / "queue")
CLI_DIR = str(ROOT / "cli")
for _entry in (QUEUE_DIR, CLI_DIR, str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import worker_pg

HEADER = "archetype: x\npalette: x\nfont_pair: x\ntier: build\nfiles: 3\nsha256: x\n"


def _lint(tmp_path, manifest_text):
    """The lint's answer for a build directory that holds only this manifest."""
    (tmp_path / "compose-manifest.txt").write_text(manifest_text, encoding="utf-8")
    return worker_pg._lint_v2_build_output(tmp_path)


def _copied_lines(result):
    """Every warning line the lint put in the log, one per element."""
    lines = []
    for entry in result["checks"]:
        for line in entry.split("\n"):
            if line.startswith("  "):
                lines.append(line[2:])
    return lines


class TestEveryEngineWarningIsCopied:
    def test_an_engine_warning_after_sixty_other_warning_lines_is_copied(self, tmp_path):
        """Sixty lines of text the spec carried, then the one line that says
        a protected page was not emitted. A reader that stops after a fixed
        number of leading lines never shows it to the reviewer."""
        spec_text = [f"warning: note_{n}: something a translator dropped" for n in range(60)]
        last = "warning: auth_required_page_not_emitted: /members is not in this build"
        manifest = HEADER + "translation_warnings_count: 61\n" + "\n".join(spec_text + [last]) + "\n"
        result = _lint(tmp_path, manifest)
        copied = _copied_lines(result)
        assert last in copied
        assert copied == spec_text + [last]

    def test_three_hundred_engine_warnings_are_all_copied_once(self, tmp_path):
        engine = [f"warning: page_named_like_model: custom page '/p{n}' is named like a model" for n in range(300)]
        manifest = (HEADER + "translation_warnings_count: 300\nengine_warnings_count: 300\n"
                    "spec_warnings_count: 0\nspec_warnings_omitted: 0\n" + "\n".join(engine) + "\n")
        result = _lint(tmp_path, manifest)
        assert _copied_lines(result) == engine
        assert not [error for error in result["errors"] if "engine warning" in error]

    def test_engine_lines_are_listed_before_spec_lines_whatever_the_file_order(self, tmp_path):
        from_spec = [f"spec-warning: note_{n}: text" for n in range(70)]
        engine = ["warning: spec_model_shown_by_archetype_page: model 'Testimonial' is shown to visitors"]
        manifest = HEADER + "translation_warnings_count: 71\n" + "\n".join(from_spec + engine) + "\n"
        copied = _copied_lines(_lint(tmp_path, manifest))
        assert copied == engine + from_spec

    def test_a_long_list_is_packed_into_fewer_entries_and_nothing_is_lost(self):
        engine = [f"warning: r: {n}" for n in range(30)]
        from_spec = [f"spec-warning: {n}" for n in range(275)]
        entries, engine_found = worker_pg._manifest_warning_checks("\n".join(engine + from_spec) + "\n")
        assert engine_found == 30
        singly = worker_pg._MANIFEST_WARNINGS_LISTED_SINGLY
        packed = worker_pg._MANIFEST_WARNINGS_PER_PACKED_ENTRY
        assert (singly, packed) == (50, 100)
        # 305 lines: 50 entries of one line, then 100 + 100 + 55.
        assert [entry.count("\n") + 1 for entry in entries] == [1] * 50 + [100, 100, 55]
        flat = [line[2:] for entry in entries for line in entry.split("\n")]
        assert flat == engine + from_spec

    def test_an_overlong_line_is_cut_and_says_so(self):
        cap = worker_pg._MANIFEST_WARNING_MAX_CHARS
        assert cap == 1200
        long_line = "warning: css_coverage: " + "x" * 5000
        entries, engine_found = worker_pg._manifest_warning_checks(long_line + "\n")
        assert engine_found == 1
        assert entries == ["  " + long_line[:cap] + " ...(cut)"]

    def test_a_line_that_only_contains_the_prefix_is_not_a_warning(self):
        text = ("tier: warning: not a warning line\n"
                " warning: indented, so not one either\n"
                "Warning: wrong case\n"
                "warning:no space after the colon\n")
        assert worker_pg._manifest_warning_checks(text) == ([], 0)

    def test_a_manifest_with_no_warnings_says_so(self, tmp_path):
        result = _lint(tmp_path, HEADER + "translation_warnings_count: 0\nengine_warnings_count: 0\n")
        assert any(check.startswith("No translation warnings") for check in result["checks"])
        assert _copied_lines(result) == []

    def test_warning_lines_are_copied_even_when_the_count_header_says_zero(self, tmp_path):
        """The count is a header; the lines are the facts."""
        line = "warning: auth_required_page_not_emitted: /members is not in this build"
        result = _lint(tmp_path, HEADER + "translation_warnings_count: 0\n" + line + "\n")
        assert _copied_lines(result) == [line]
        assert not any(check.startswith("No translation warnings") for check in result["checks"])

    def test_warning_lines_are_copied_when_there_is_no_count_header_at_all(self, tmp_path):
        line = "warning: auth_required_page_not_emitted: /members is not in this build"
        result = _lint(tmp_path, HEADER + line + "\n")
        assert _copied_lines(result) == [line]

    def test_an_announced_count_with_no_warning_line_is_a_lint_error(self, tmp_path):
        errors = _lint(tmp_path, HEADER + "translation_warnings_count: 4\n")["errors"]
        assert any("announces 4 warning(s) and has no warning line" in error for error in errors), errors


class TestTheCountHeaderIsChecked:
    def test_fewer_engine_lines_than_announced_is_a_lint_error(self, tmp_path):
        manifest = (HEADER + "translation_warnings_count: 3\nengine_warnings_count: 3\n"
                    "warning: a: 1\nwarning: b: 2\n")
        errors = _lint(tmp_path, manifest)["errors"]
        assert any("announces 3 engine warning(s) and 2 were found" in error for error in errors), errors

    def test_more_engine_lines_than_announced_is_a_lint_error(self, tmp_path):
        manifest = (HEADER + "translation_warnings_count: 1\nengine_warnings_count: 1\n"
                    "warning: a: 1\nwarning: b: 2\n")
        errors = _lint(tmp_path, manifest)["errors"]
        assert any("announces 1 engine warning(s) and 2 were found" in error for error in errors), errors

    def test_a_matching_count_is_no_error(self, tmp_path):
        manifest = (HEADER + "translation_warnings_count: 2\nengine_warnings_count: 2\n"
                    "warning: a: 1\nwarning: b: 2\n")
        assert not [error for error in _lint(tmp_path, manifest)["errors"] if "engine warning" in error]

    def test_a_count_inside_a_warning_line_is_not_read_as_the_header(self, tmp_path):
        """The header is matched at the start of a line: text inside a warning
        cannot announce a count."""
        manifest = (HEADER + "translation_warnings_count: 1\nengine_warnings_count: 1\n"
                    "warning: note: engine_warnings_count: 99 translation_warnings_count: 0\n")
        result = _lint(tmp_path, manifest)
        assert not [error for error in result["errors"] if "engine warning" in error]
        assert any("emitted 1 warning(s)" in check for check in result["checks"])


class TestTheManifestIsReadUpToACap:
    def test_the_cap_is_256_kib(self):
        assert worker_pg._MANIFEST_MAX_BYTES == 256 * 1024

    def test_a_small_manifest_is_read_whole(self, tmp_path):
        path = tmp_path / "compose-manifest.txt"
        path.write_text(HEADER, encoding="utf-8")
        assert worker_pg._read_manifest(path) == (HEADER, False)

    def test_a_manifest_of_exactly_the_cap_is_not_reported_as_cut(self, tmp_path):
        path = tmp_path / "compose-manifest.txt"
        body = HEADER + "x" * (worker_pg._MANIFEST_MAX_BYTES - len(HEADER) - 1) + "\n"
        path.write_bytes(body.encode("utf-8"))
        text, was_cut = worker_pg._read_manifest(path)
        assert (len(text), was_cut) == (worker_pg._MANIFEST_MAX_BYTES, False)

    def test_a_larger_manifest_is_cut_at_a_line_end_and_the_lint_says_so(self, tmp_path):
        line = "warning: css_coverage: " + "y" * 200
        count = (2 * worker_pg._MANIFEST_MAX_BYTES) // (len(line) + 1)
        manifest = HEADER + f"translation_warnings_count: {count}\n" + (line + "\n") * count
        path = tmp_path / "compose-manifest.txt"
        path.write_text(manifest, encoding="utf-8")
        assert path.stat().st_size > worker_pg._MANIFEST_MAX_BYTES

        text, was_cut = worker_pg._read_manifest(path)
        assert was_cut is True
        assert len(text.encode("utf-8")) <= worker_pg._MANIFEST_MAX_BYTES
        assert text.endswith(line + "\n")

        result = worker_pg._lint_v2_build_output(tmp_path)
        assert any("larger than 262144 bytes" in error for error in result["errors"]), result["errors"]
        copied = _copied_lines(result)
        # Every line inside the cap, whole; none past it and no half line.
        assert set(copied) == {line}
        assert 0 < len(copied) < count
        assert len(copied) == text.count(line)

    def test_no_more_than_the_cap_is_read_from_a_huge_file(self, tmp_path, monkeypatch):
        """The read itself is bounded: the worker asks for the cap plus one
        byte, whatever the file holds."""
        path = tmp_path / "compose-manifest.txt"
        path.write_bytes(b"warning: a: b\n" * 200_000)  # about 2.8 MB
        asked = []
        real_open = open

        class Counting:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                asked.append(size)
                return self._handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                self._handle.close()

        def counting_open(target, mode="r", *args, **kwargs):
            handle = real_open(target, mode, *args, **kwargs)
            return Counting(handle) if str(target) == str(path) else handle

        monkeypatch.setattr("builtins.open", counting_open)
        text, was_cut = worker_pg._read_manifest(path)
        assert asked == [worker_pg._MANIFEST_MAX_BYTES + 1]
        assert was_cut is True and len(text) <= worker_pg._MANIFEST_MAX_BYTES

    def test_bytes_that_are_not_utf8_do_not_stop_the_lint(self, tmp_path):
        path = tmp_path / "compose-manifest.txt"
        path.write_bytes(HEADER.encode() + b"translation_warnings_count: 1\nwarning: a: caf\xe9 \xff\n")
        result = worker_pg._lint_v2_build_output(tmp_path)
        copied = _copied_lines(result)
        assert len(copied) == 1 and copied[0].startswith("warning: a: caf")


class TestAgainstTheRealEngine:
    """The manifest the compose engine writes today, read by the worker."""

    @pytest.fixture()
    def built(self, tmp_path):
        from compose.build import build_app
        spec = json.loads((ROOT / "compose" / "fixtures" / "sample-healer-spec.json").read_text())
        # Two things the engine itself warns about: a custom page named like a
        # model, and a spec model that an archetype page shows to visitors.
        spec["data_models"] = [
            {"name": "Offering", "fields": [{"name": "title", "type": "string"}]},
            {"name": "Testimonial", "fields": [{"name": "quote", "type": "text"}]},
        ]
        spec["custom_pages"] = [{"path": "/offerings", "title": "Offerings", "sections": []}]
        # And far more spec-supplied notes than fit in fifty lines.
        spec["_translation_warnings"] = [
            {"reason": f"spec_note_{n}", "detail": f"something the translator dropped, number {n}"}
            for n in range(80)
        ]
        out = tmp_path / "site"
        build_app(spec, output_dir=out)
        return out

    def test_every_warning_line_of_a_real_manifest_reaches_the_log(self, built):
        manifest_lines = (built / "compose-manifest.txt").read_text(encoding="utf-8").split("\n")
        engine = [line for line in manifest_lines if line.startswith("warning: ")]
        from_spec = [line for line in manifest_lines if line.startswith("spec-warning: ")]
        assert len(engine) >= 2, manifest_lines[:12]
        assert from_spec, "the engine wrote no spec-warning line for a spec that carried eighty notes"

        result = worker_pg._lint_v2_build_output(built)
        assert _copied_lines(result) == engine + from_spec
        assert not [error for error in result["errors"] if "compose-manifest.txt" in error], result["errors"]
        total = len(engine) + len(from_spec)
        assert any(f"emitted {total} warning(s)" in check for check in result["checks"]), result["checks"]

    def test_the_real_manifest_has_the_shape_the_worker_relies_on(self, built):
        """Header lines `key: value` first, then warning lines only, each on
        one line, engine lines before spec lines, and counts that agree."""
        text = (built / "compose-manifest.txt").read_text(encoding="utf-8")
        assert text.endswith("\n")
        lines = text.split("\n")[:-1]
        is_warning = [line.startswith(("warning: ", "spec-warning: ")) for line in lines]
        first_warning = is_warning.index(True)
        assert all(is_warning[first_warning:]), "something other than a warning follows the first warning line"
        header = dict(line.split(": ", 1) for line in lines[:first_warning])
        for key in ("archetype", "palette", "font_pair", "files", "sha256",
                    "translation_warnings_count", "engine_warnings_count"):
            assert key in header, (key, sorted(header))
        kinds = ["engine" if line.startswith("warning: ") else "spec" for line in lines[first_warning:]]
        assert kinds == sorted(kinds), "an engine line stands after a spec line"
        assert int(header["engine_warnings_count"]) == kinds.count("engine")
        assert int(header["translation_warnings_count"]) == len(kinds)
        longest = max(len(line) for line in lines[first_warning:])
        assert longest <= worker_pg._MANIFEST_WARNING_MAX_CHARS, longest
