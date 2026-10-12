"""The executor's configuration has one reader, and it reads strictly.

scripts/cas-executor-config.json is read by three programs: the loop
(scripts/cas-roadmap-loop.sh), the watchdog (scripts/cas-executor-watchdog.sh)
and the convenience CLI (scripts/cas-exec). The loop and the watchdog used to
read "enabled" each in their own way, and disagreed: the JSON string "True"
enabled the loop and not the watchdog. All three now ask
scripts/cas-pick-item.py, and only a JSON true enables.

These tests run that reader (as a function, as the picker's command line, as
the watchdog's own shell function and as `cas-exec status`) over the same
table of values. That the loop then starts no session is tested by running
it, in test_executor_loop_behavior.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
BASH = shutil.which("bash") or "/bin/bash"

_spec = importlib.util.spec_from_file_location("cas_pick_item_config", SCRIPTS / "cas-pick-item.py")
picker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(picker)

# value of "enabled" as written in the file -> is the executor on
ENABLED_VALUES = [
    ("true", True),
    ("false", False),
    ('"True"', False),
    ('"true"', False),
    ('"yes"', False),
    ("1", False),
    ("1.0", False),
    ("null", False),
    ("[true]", False),
    ('{"value": true}', False),
]
MISSING = object()


@pytest.fixture
def checkout(tmp_path):
    """A directory laid out like the repository's scripts/, holding the real
    picker, watchdog and cas-exec next to a config the test writes."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("cas-pick-item.py", "cas-executor-watchdog.sh", "cas-exec"):
        shutil.copy(SCRIPTS / name, scripts / name)
    return tmp_path


def _write(checkout, text):
    (checkout / "scripts" / "cas-executor-config.json").write_text(text)


def _picker(checkout, *args):
    return subprocess.run([sys.executable, str(checkout / "scripts" / "cas-pick-item.py"), *args],
                          capture_output=True, text=True, timeout=60)


def _watchdog_says_enabled(checkout):
    """Run the watchdog's own _executor_enabled, cut out of the script, in bash."""
    text = (checkout / "scripts" / "cas-executor-watchdog.sh").read_text()
    start = text.index("_executor_enabled() {")
    function = text[start:text.index("\n}\n", start) + 3]
    probe = checkout / "probe.sh"
    probe.write_text(f'SCRIPT_DIR="{checkout}/scripts"\nPROJECT_DIR="{checkout}"\n{function}\n_executor_enabled\n')
    env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(checkout)}
    return subprocess.run([BASH, str(probe)], capture_output=True, text=True, env=env, timeout=60).returncode == 0


@pytest.mark.parametrize("written,enabled", ENABLED_VALUES, ids=[w for w, _ in ENABLED_VALUES])
def test_every_reader_agrees_and_only_a_json_true_enables(checkout, written, enabled):
    _write(checkout, '{"enabled": %s, "interval_minutes": 5, "max_items_per_day": 1, '
                     '"max_budget_per_item_usd": 1, "daily_spend_ceiling_usd": 1}' % written)
    config = picker.read_config(checkout / "scripts" / "cas-executor-config.json")
    assert picker.executor_enabled(config) is enabled
    assert (_picker(checkout, "--enabled").returncode == 0) is enabled
    lines = _picker(checkout, "--loop-config").stdout.splitlines()
    assert lines[0] == str(enabled), "the first line is what the loop compares with True"
    assert _watchdog_says_enabled(checkout) is enabled
    status = subprocess.run([BASH, str(checkout / "scripts" / "cas-exec"), "status"], capture_output=True,
                            text=True, timeout=60, env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                                                         "HOME": str(checkout)})
    assert f"Enabled:   {enabled}" in status.stdout, status.stdout + status.stderr


@pytest.mark.parametrize("text", ["{}", "", "{", "[true]", "true", '{"enabled": true,', "null"],
                         ids=["no key", "empty file", "cut short", "a list", "a bare true", "trailing comma", "null"])
def test_a_config_without_a_usable_enabled_is_disabled_for_every_reader(checkout, text):
    _write(checkout, text)
    assert _picker(checkout, "--enabled").returncode == 1
    assert _watchdog_says_enabled(checkout) is False


def test_no_config_file_is_disabled(checkout):
    assert _picker(checkout, "--enabled").returncode == 1
    assert _watchdog_says_enabled(checkout) is False
    assert _picker(checkout, "--loop-config").returncode == 2


def test_the_shipped_config_is_disabled_and_prints_the_lines_the_loop_reads():
    result = subprocess.run([sys.executable, str(SCRIPTS / "cas-pick-item.py"), "--loop-config"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    shipped = json.loads((SCRIPTS / "cas-executor-config.json").read_text())
    values = dict(zip(picker.LOOP_CONFIG_LINES, result.stdout.splitlines()))
    assert len(result.stdout.splitlines()) == len(picker.LOOP_CONFIG_LINES)
    assert values["enabled"] == "False" and shipped["enabled"] is False
    assert values["pause_state"] == "active"
    assert values["tiered_governance"] == "False"
    assert values["skip_complex_items"] == "True"
    for key in ("max_items_per_day", "daily_spend_ceiling_usd", "interval_minutes", "max_parallel",
                "max_budget_per_item_usd", "max_total_failures_before_defer", "target_branch"):
        assert values[key] == str(shipped[key]), key


def test_the_loop_reads_the_lines_in_the_order_the_picker_prints_them():
    # preflight_check takes the lines by position (sed -n 'Np'): the order
    # here and the order of LOOP_CONFIG_LINES are one contract.
    loop = (SCRIPTS / "cas-roadmap-loop.sh").read_text()
    preflight = loop[loop.index("preflight_check() {"):]
    read = re.findall(r"^\s+(\w+)=\$\(echo \"\$config_output\" \| sed -n '(\d+)p'\)", preflight, re.M)
    assert [int(n) for _, n in read] == list(range(1, len(picker.LOOP_CONFIG_LINES) + 1))
    names = {"enabled": "enabled", "pause_state": "pause_state", "max_items": "max_items_per_day",
             "ceiling": "daily_spend_ceiling_usd", "interval": "interval_minutes", "max_par": "max_parallel",
             "max_budget": "max_budget_per_item_usd", "max_total_failures": "max_total_failures_before_defer",
             "skip_complex": "skip_complex_items", "target_branch": "target_branch",
             "tiered_gov": "tiered_governance"}
    assert tuple(names[variable] for variable, _ in read) == picker.LOOP_CONFIG_LINES
    assert 'python3 "$SCRIPT_DIR/cas-pick-item.py" --loop-config' in loop
    # no reader of the config file is left in the loop or the watchdog
    watchdog = (SCRIPTS / "cas-executor-watchdog.sh").read_text()
    assert 'python3 "$SCRIPT_DIR/cas-pick-item.py" --enabled' in watchdog
    for text in (loop, watchdog):
        code = "\n".join(ln for ln in text.split("\n") if not ln.strip().startswith("#"))
        assert "json.load(open(" not in code


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value,expected", [
    (None, "active"),
    ("", "active"),
    ((NOW - timedelta(hours=1)).isoformat(), "active"),
    ((NOW + timedelta(hours=1)).isoformat(), "paused until"),
    ("2999-01-01", "paused until"),               # a date without a time or an offset: local midnight
    ("tomorrow", "paused: pause_until is not"),
    ("2026-13-45T00:00:00+00:00", "paused: pause_until is not"),
    (20260601, "paused: pause_until is not"),
    (True, "paused: pause_until is not"),
    (["2999-01-01"], "paused: pause_until is not"),
])
def test_a_pause_that_cannot_be_read_as_a_time_is_a_pause(value, expected):
    state = picker.pause_state(value, now=NOW)
    assert state.startswith(expected), state
    assert "\n" not in state


@pytest.mark.parametrize("key,value", [
    ("max_items_per_day", "3"), ("max_items_per_day", 2.5), ("max_items_per_day", -1), ("max_items_per_day", True),
    ("daily_spend_ceiling_usd", "10"), ("daily_spend_ceiling_usd", -0.01), ("daily_spend_ceiling_usd", None),
    ("interval_minutes", 0), ("interval_minutes", "120"), ("max_parallel", 0), ("max_parallel", 1.5),
    ("max_budget_per_item_usd", 0), ("max_budget_per_item_usd", "3"), ("max_budget_per_item_usd", False),
    ("max_total_failures_before_defer", 0),
    ("target_branch", ""), ("target_branch", "-f"), ("target_branch", "a b"), ("target_branch", "a..b"),
    ("target_branch", "master:refs/heads/x"), ("target_branch", "refs/heads/master.lock"), ("target_branch", 7),
])
def test_a_value_of_the_wrong_kind_is_refused_with_the_key_named(key, value):
    with pytest.raises(picker.ConfigError, match=key):
        picker.loop_config({"enabled": True, key: value})


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_numbers_that_are_not_json_are_refused(checkout, literal):
    # Python's json module would read these; a NaN ceiling compares false
    # against every figure, which is no ceiling at all.
    _write(checkout, '{"enabled": true, "daily_spend_ceiling_usd": %s}' % literal)
    result = _picker(checkout, "--loop-config")
    assert result.returncode == 2 and "is not JSON" in result.stderr
    assert _picker(checkout, "--enabled").returncode == 1


def test_missing_keys_fall_back_to_the_shipped_values_and_to_disabled():
    values = picker.loop_config({})
    shipped = json.loads((SCRIPTS / "cas-executor-config.json").read_text())
    assert values["enabled"] is False
    for key in ("max_items_per_day", "daily_spend_ceiling_usd", "interval_minutes", "max_parallel",
                "max_budget_per_item_usd", "max_total_failures_before_defer", "skip_complex_items",
                "target_branch", "tiered_governance"):
        assert values[key] == shipped[key], key


@pytest.mark.parametrize("value,on", [(False, False), (True, True), ("false", True), ("true", True),
                                      (0, True), (None, True), (MISSING, False)])
def test_tiered_governance_is_off_only_for_false_or_no_key(value, on):
    config = {} if value is MISSING else {"tiered_governance": value}
    assert picker.loop_config(config)["tiered_governance"] is on


@pytest.mark.parametrize("value,on", [(False, False), (True, True), ("false", True), (0, True), (MISSING, True)])
def test_skip_complex_items_is_off_only_for_false(value, on):
    config = {} if value is MISSING else {"skip_complex_items": value}
    assert picker.loop_config(config)["skip_complex_items"] is on
