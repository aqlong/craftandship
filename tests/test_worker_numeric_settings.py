"""A mistyped numeric setting does not stop the worker or the monitor.

The worker reads MAX_CONCURRENT_BUILDS and DAILY_API_SPEND_CAP when it is
imported, and MONITOR_INTERVAL, OPS_SCAN_INTERVAL, MAX_DESIGN_FIXES and
MAX_QA_FIXES when it uses them; the monitor reads
MONITOR_MAX_LLM_CALLS_PER_DAY for each diagnosis. Each goes through
coordinator.config.env_int / env_float, which log a bad value and use the
default.

The import-time cases start a fresh interpreter, because that is the only
place an import-time read can be observed. No database, no network.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
for _entry in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import worker_pg

_PRINT_CAPS = (
    "import sys; sys.path[:0] = [{queue!r}, {cli!r}, {root!r}]; "
    "import worker_pg; print(worker_pg._MAX_CONCURRENT, worker_pg._DAILY_SPEND_CAP)"
).format(queue=str(ROOT / "queue"), cli=str(ROOT / "cli"), root=str(ROOT))


def _import_worker_with(settings):
    """Import the worker in a fresh interpreter with these settings; its
    return code and the two caps it printed."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(settings)
    done = subprocess.run(
        [sys.executable, "-c", _PRINT_CAPS], env=env, cwd=str(ROOT),
        capture_output=True, text=True, timeout=60,
    )
    last_line = done.stdout.strip().split("\n")[-1] if done.stdout.strip() else ""
    return done.returncode, last_line, done.stderr


class TestWorkerImportSurvivesABadSetting:
    @pytest.mark.parametrize("settings, expected", [
        ({}, "1 50.0"),
        ({"MAX_CONCURRENT_BUILDS": "3", "DAILY_API_SPEND_CAP": "12.5"}, "3 12.5"),
        ({"MAX_CONCURRENT_BUILDS": "two"}, "1 50.0"),
        ({"MAX_CONCURRENT_BUILDS": ""}, "1 50.0"),
        ({"MAX_CONCURRENT_BUILDS": "0"}, "1 50.0"),
        ({"MAX_CONCURRENT_BUILDS": "2.5"}, "1 50.0"),
        ({"DAILY_API_SPEND_CAP": "fifty"}, "1 50.0"),
        ({"DAILY_API_SPEND_CAP": ""}, "1 50.0"),
        # Not a number at all: read as the default, so the cap stays on.
        ({"DAILY_API_SPEND_CAP": "nan"}, "1 50.0"),
        ({"DAILY_API_SPEND_CAP": "inf"}, "1 50.0"),
        # 0 is the documented way to switch the cap off; below 0 reads as 0.
        ({"DAILY_API_SPEND_CAP": "0"}, "1 0.0"),
        ({"DAILY_API_SPEND_CAP": "-5"}, "1 0.0"),
    ])
    def test_the_worker_imports_and_holds_a_usable_cap(self, settings, expected):
        code, printed, stderr = _import_worker_with(settings)
        assert code == 0, stderr[-800:]
        assert printed == expected


class _RecordedThread:
    """Stands in for threading.Thread: keeps the target, starts nothing."""
    target = None

    def __init__(self, target=None, **_kwargs):
        type(self).target = staticmethod(target)

    def start(self):
        pass


class TestIntervalsReadWhenAThreadStarts:
    @pytest.mark.parametrize("raw, expected", [
        (None, 900), ("300", 300), ("fifteen minutes", 900), ("", 900), ("0", 900), ("-60", 900), ("12.5", 900),
    ])
    def test_the_monitor_interval(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("MONITOR_INTERVAL", raising=False)
        else:
            monkeypatch.setenv("MONITOR_INTERVAL", raw)
        seen = {}

        def run_checks(interval_seconds, stop_event):
            seen["interval"] = interval_seconds
            stop_event.set()  # one pass, then the loop ends

        was_set = worker_pg._shutdown_event.is_set()
        worker_pg._shutdown_event.clear()
        try:
            with patch.object(worker_pg.threading, "Thread", _RecordedThread):
                worker_pg._start_monitor_thread()
            with patch("monitor.ping.run_checks", run_checks):
                _RecordedThread.target()
        finally:
            if was_set:
                worker_pg._shutdown_event.set()
            else:
                worker_pg._shutdown_event.clear()
        assert seen["interval"] == expected

    @pytest.mark.parametrize("raw, expected", [
        (None, 1800), ("600", 600), ("half an hour", 1800), ("0", 1800), ("-1", 1800),
    ])
    def test_the_ops_scan_interval(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("OPS_SCAN_INTERVAL", raising=False)
        else:
            monkeypatch.setenv("OPS_SCAN_INTERVAL", raw)
        seen = {}
        with patch.object(worker_pg.threading, "Thread", _RecordedThread):
            worker_pg._start_ops_agent_thread()
        with patch("ops.agent.run_agent", lambda **kwargs: seen.update(kwargs)):
            _RecordedThread.target()
        assert seen["interval_seconds"] == expected


class TestTheDiagnosisCallCap:
    @pytest.fixture(autouse=True)
    def _empty_window(self):
        import monitor.diagnose as diagnose
        with diagnose._llm_lock:
            saved = list(diagnose._llm_call_times)
            diagnose._llm_call_times.clear()
        yield
        with diagnose._llm_lock:
            diagnose._llm_call_times.clear()
            diagnose._llm_call_times.extend(saved)

    def _granted(self, monkeypatch, raw, asks):
        import monitor.diagnose as diagnose
        if raw is None:
            monkeypatch.delenv("MONITOR_MAX_LLM_CALLS_PER_DAY", raising=False)
        else:
            monkeypatch.setenv("MONITOR_MAX_LLM_CALLS_PER_DAY", raw)
        return sum(1 for _ in range(asks) if diagnose._llm_budget_allows())

    def test_a_mistyped_cap_means_sixty(self, monkeypatch):
        assert self._granted(monkeypatch, "sixty", 70) == 60

    def test_an_unset_cap_means_sixty(self, monkeypatch):
        assert self._granted(monkeypatch, None, 70) == 60

    def test_a_cap_is_honored(self, monkeypatch):
        assert self._granted(monkeypatch, "2", 5) == 2

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_a_cap_of_zero_or_below_allows_no_call(self, monkeypatch, raw):
        assert self._granted(monkeypatch, raw, 5) == 0


def _bare_numeric_env_reads(source):
    """Line numbers of int(...) / float(...) calls whose argument reads the
    environment (os.environ or os.getenv)."""
    def reads_environment(node):
        return any(
            isinstance(inner, ast.Attribute) and inner.attr in {"environ", "getenv"}
            for inner in ast.walk(node)
        )

    return [
        node.lineno for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"int", "float"} and any(reads_environment(arg) for arg in node.args)
    ]


def test_the_scan_recognises_a_bare_read():
    sample = 'import os\nLIMIT = int(os.environ.get("X", "3"))\nRATE = float(os.getenv("Y", "1"))\nOK = int("3")\n'
    assert _bare_numeric_env_reads(sample) == [2, 3]


def test_no_numeric_setting_is_parsed_with_a_bare_int_or_float():
    """int(os.environ...) or float(os.environ...) raises on a mistyped value,
    at import when the read is at module level. None is left in the worker,
    coordinator, monitor, ops and deploy modules."""
    files = [ROOT / "cli" / "deploy.py"]
    for package in ("queue", "coordinator", "monitor", "ops"):
        files.extend(sorted((ROOT / package).rglob("*.py")))
    assert len(files) > 30
    offenders = [
        f"{path.relative_to(ROOT)}:{lineno}"
        for path in files for lineno in _bare_numeric_env_reads(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
