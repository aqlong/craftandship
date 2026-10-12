"""Regression tests for cli/build_trace.py, the JSONL trace writer.

Covers the guarantees the observability slice depends on:
  - Happy-path: one event per call, one JSONL line with the expected fields
  - Fail-safe: missing / invalid build dir never raises, silently drops
  - Append: multiple events produce multiple parseable lines
  - Kill switch: BUILD_TRACE_DISABLED env var suppresses all writes
  - Non-serialisable payloads: coerced via repr, never raises

These tests exist specifically so the Slice A/B instrumentation can't
regress the fail-safe contract that the pipeline relies on.
"""
import pytest

# v1-only: exercises cli/build.py + post-processors + Claude-driven QA/design loops.
# Collected only when CAS_BUILD_ENGINE=v1 is set (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

from build_trace import emit_build_trace, _TRACE_FILENAME, _DISABLE_ENV


@pytest.fixture(autouse=True)
def _clear_disable_env(monkeypatch):
    """Ensure BUILD_TRACE_DISABLED is unset for every test by default."""
    monkeypatch.delenv(_DISABLE_ENV, raising=False)
    yield


class TestEmitBuildTraceHappyPath:
    def test_writes_single_jsonl_line_with_expected_fields(self, tmp_path):
        emit_build_trace(tmp_path, "test.event", {"key": "value", "count": 3})

        trace_file = tmp_path / _TRACE_FILENAME
        assert trace_file.exists(), "trace file should be created on first write"

        lines = trace_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["event"] == "test.event"
        assert record["key"] == "value"
        assert record["count"] == 3
        assert "ts" in record
        assert isinstance(record["ts"], (int, float))

    def test_accepts_str_build_dir(self, tmp_path):
        emit_build_trace(str(tmp_path), "str.path", {"ok": True})

        trace_file = tmp_path / _TRACE_FILENAME
        assert trace_file.exists()
        record = json.loads(trace_file.read_text().splitlines()[0])
        assert record["event"] == "str.path"
        assert record["ok"] is True

    def test_empty_payload_still_writes_event(self, tmp_path):
        emit_build_trace(tmp_path, "bare.event")

        trace_file = tmp_path / _TRACE_FILENAME
        assert trace_file.exists()
        record = json.loads(trace_file.read_text().splitlines()[0])
        assert record["event"] == "bare.event"
        assert "ts" in record


class TestEmitBuildTraceFailSafe:
    def test_none_build_dir_is_noop(self):
        # Must not raise; nothing to assert on the filesystem because there's
        # no target dir.
        emit_build_trace(None, "none.dir", {"k": "v"})

    def test_nonexistent_dir_is_noop(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        emit_build_trace(missing, "missing.dir", {"k": "v"})
        assert not missing.exists(), "helper must not create the build dir itself"

    def test_file_instead_of_dir_is_noop(self, tmp_path):
        not_a_dir = tmp_path / "regular.file"
        not_a_dir.write_text("hi")

        emit_build_trace(not_a_dir, "file.not.dir", {"k": "v"})

        # The regular file must be untouched (no trace content appended).
        assert not_a_dir.read_text() == "hi"

    def test_invalid_build_dir_type_is_noop(self):
        # Objects that can't be turned into a Path should silently drop.
        emit_build_trace(object(), "bad.type", {"k": "v"})

    def test_non_serialisable_payload_is_coerced_not_raised(self, tmp_path):
        class Weird:
            def __repr__(self) -> str:
                return "<Weird!>"

        emit_build_trace(tmp_path, "weird.payload", {"obj": Weird()})

        trace_file = tmp_path / _TRACE_FILENAME
        assert trace_file.exists()
        record = json.loads(trace_file.read_text().splitlines()[0])
        # default=repr coerces unknown objects; we should see the repr string.
        assert record["obj"] == "<Weird!>"


class TestEmitBuildTraceAppend:
    def test_multiple_events_produce_one_line_each(self, tmp_path):
        emit_build_trace(tmp_path, "event.one", {"n": 1})
        emit_build_trace(tmp_path, "event.two", {"n": 2})
        emit_build_trace(tmp_path, "event.three", {"n": 3})

        trace_file = tmp_path / _TRACE_FILENAME
        lines = trace_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

        events = [json.loads(line) for line in lines]
        assert [e["event"] for e in events] == ["event.one", "event.two", "event.three"]
        assert [e["n"] for e in events] == [1, 2, 3]

    def test_events_are_monotonic_or_equal_timestamps(self, tmp_path):
        emit_build_trace(tmp_path, "first", None)
        emit_build_trace(tmp_path, "second", None)

        lines = (tmp_path / _TRACE_FILENAME).read_text().splitlines()
        records = [json.loads(line) for line in lines]
        assert records[1]["ts"] >= records[0]["ts"]


class TestEmitBuildTraceKillSwitch:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "YES"])
    def test_disable_env_suppresses_writes(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(_DISABLE_ENV, value)
        emit_build_trace(tmp_path, "should.not.write", {"k": "v"})
        assert not (tmp_path / _TRACE_FILENAME).exists()

    def test_disable_env_off_allows_writes(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_DISABLE_ENV, "0")
        emit_build_trace(tmp_path, "should.write", {"k": "v"})
        assert (tmp_path / _TRACE_FILENAME).exists()

    def test_disable_then_enable_in_same_process(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_DISABLE_ENV, "1")
        emit_build_trace(tmp_path, "dropped", None)
        assert not (tmp_path / _TRACE_FILENAME).exists()

        monkeypatch.delenv(_DISABLE_ENV, raising=False)
        emit_build_trace(tmp_path, "kept", None)
        trace_file = tmp_path / _TRACE_FILENAME
        assert trace_file.exists()
        lines = trace_file.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["event"] == "kept"
