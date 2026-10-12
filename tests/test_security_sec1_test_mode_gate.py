"""SEC-1b regression guards: the TEST_MODE login shortcut must never be live in
a deployed app.

Background. The v1 build-time test runner (`cli/test.py`) logs into the
generated app over HTTP with a fixed test password. To make that work, the v1
pipeline puts a shortcut into every magic-link app's POST /login: when the
`TEST_MODE` environment variable is set, the fixed password logs in as any
existing user (including the admin). The shortcut ships inside the deployed
app's source, so it has to be impossible to switch on there. Two layers
enforce that, both deterministic (not left to the model):

1. every `process.env.TEST_MODE` gate also requires `NODE_ENV !== 'production'`
   (`_harden_test_mode_gates`, plus the stricter injected blocks);
2. `server.js` exits at startup when TEST_MODE is set while NODE_ENV is
   production (`_ensure_test_mode_production_guard`).

The deploy step sets NODE_ENV=production and never sets TEST_MODE; that is
asserted below too.
"""
from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

import build  # noqa: E402

_NEEDS_NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _clean_node_path() -> str:
    """The PATH for node under an otherwise empty environment: the system
    directories plus the directory node itself lives in (Homebrew on macOS,
    the toolcache directory on a CI runner)."""
    node = shutil.which("node")
    return os.pathsep.join([os.path.dirname(node), "/usr/bin", "/bin"]) if node else "/usr/bin:/bin"

_SCHEMA = "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"

# The shape Claude (and earlier pipeline versions) emitted: no NODE_ENV check.
_WEAK_AUTH_JS = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const { email, password } = req.body;
  // TEST_MODE backdoor
  if (process.env.TEST_MODE && password === 'test_password_for_ci') {
    const testUser = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
    if (testUser.rows.length > 0) {
      req.session.userId = testUser.rows[0].id;
      return req.session.save(() => res.redirect('/dashboard'));
    }
  }
});
module.exports = router;"""

_BARE_AUTH_JS = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const { email, password } = req.body;
  const result = await pool.query('SELECT * FROM users WHERE email = $1', [email]);
});
module.exports = router;"""

_SEPARATE_DECL_AUTH_JS = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const email = (req.body.email || '').trim();
  const password = req.body.password;
});
module.exports = router;"""

_NO_VARS_AUTH_JS = """const router = require('express').Router();
router.post('/login', async (req, res) => {
  const user = await findUser(req);
});
module.exports = router;"""

_SERVER_JS_NO_GUARD = """require('dotenv').config();
const express = require('express');
const session = require('express-session');

const app = express();
app.listen(3000);
"""

_SERVER_JS_WITH_GUARD = """require('dotenv').config();
const express = require('express');

if (process.env.TEST_MODE && process.env.NODE_ENV === 'production') {
  console.error('TEST_MODE must not be set in production');
  process.exit(1);
}

const app = express();
app.listen(3000);
"""


def _run(monkeypatch, auth_js=None, server_js=None):
    monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
    files = [("db/schema.sql", _SCHEMA)]
    if auth_js is not None:
        files.append(("routes/auth.js", auth_js))
    if server_js is not None:
        files.append(("server.js", server_js))
    return dict(build._ensure_magic_link_auth(files))


def _test_mode_conditions(js: str) -> list[str]:
    """Every `if (...)` condition in `js` that mentions process.env.TEST_MODE."""
    return re.findall(r"if \((process\.env\.TEST_MODE[^\n]*?)\) \{", js)


def _eval_condition(condition: str, env: dict, password: str) -> bool:
    """Evaluate a JS condition under a clean env with node."""
    harness = (
        "const req = { body: { password: process.env.CAS_T_PASSWORD || '' } };"
        "const password = req.body.password;"
        f"console.log(JSON.stringify(Boolean({condition})));"
    )
    run_env = {"PATH": _clean_node_path(), "CAS_T_PASSWORD": password, **env}
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=10, env=run_env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Layer 1: the login shortcut's own condition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "auth_js",
    [_BARE_AUTH_JS, _SEPARATE_DECL_AUTH_JS, _NO_VARS_AUTH_JS],
    ids=["destructured", "separate-declarations", "req-body-fallback"],
)
def test_injected_shortcut_requires_non_production(monkeypatch, auth_js):
    emitted = _run(monkeypatch, auth_js=auth_js)["routes/auth.js"]
    conditions = _test_mode_conditions(emitted)
    assert len(conditions) == 1, emitted
    assert "process.env.NODE_ENV !== 'production'" in conditions[0]


def test_weak_gate_written_by_claude_is_hardened(monkeypatch):
    emitted = _run(monkeypatch, auth_js=_WEAK_AUTH_JS)["routes/auth.js"]
    conditions = _test_mode_conditions(emitted)
    assert len(conditions) == 1
    assert "process.env.NODE_ENV !== 'production'" in conditions[0]
    assert emitted.count("test_password_for_ci") == 1  # hardened in place, not duplicated


def test_weak_gate_with_strict_true_comparison_is_hardened(monkeypatch):
    auth_js = _WEAK_AUTH_JS.replace(
        "process.env.TEST_MODE &&", "process.env.TEST_MODE === 'true' &&"
    )
    emitted = _run(monkeypatch, auth_js=auth_js)["routes/auth.js"]
    assert "process.env.TEST_MODE === 'true' && process.env.NODE_ENV !== 'production' &&" in emitted


def test_hardening_is_idempotent(monkeypatch):
    once = _run(monkeypatch, auth_js=_WEAK_AUTH_JS, server_js=_SERVER_JS_NO_GUARD)
    monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
    twice = dict(build._ensure_magic_link_auth(list(once.items())))
    assert twice["routes/auth.js"] == once["routes/auth.js"]
    assert twice["server.js"] == once["server.js"]
    assert once["routes/auth.js"].count("process.env.NODE_ENV !== 'production'") == 1


def test_production_guard_in_server_js_is_not_rewritten_as_a_weak_gate():
    guard = build._TEST_MODE_PRODUCTION_GUARD_JS
    assert build._WEAK_TEST_MODE_GATE_RE.search(guard) is None


@_NEEDS_NODE
@pytest.mark.parametrize(
    "auth_js", [_BARE_AUTH_JS, _WEAK_AUTH_JS], ids=["injected", "hardened"]
)
def test_shortcut_condition_truth_table(monkeypatch, auth_js):
    """Evaluate the emitted condition in node: live only outside production."""
    condition = _test_mode_conditions(_run(monkeypatch, auth_js=auth_js)["routes/auth.js"])[0]
    secret = "test_password_for_ci"
    assert _eval_condition(condition, {"TEST_MODE": "true", "NODE_ENV": "test"}, secret) is True
    assert _eval_condition(condition, {"TEST_MODE": "true"}, secret) is True  # NODE_ENV unset: build box
    assert _eval_condition(condition, {"TEST_MODE": "true", "NODE_ENV": "production"}, secret) is False
    assert _eval_condition(condition, {"NODE_ENV": "test"}, secret) is False  # TEST_MODE unset
    assert _eval_condition(condition, {"TEST_MODE": "true", "NODE_ENV": "test"}, "wrong") is False


def test_no_weak_gate_left_in_the_pipeline_source():
    """Neither the injected blocks nor the prompt examples may show a
    TEST_MODE gate without the NODE_ENV check (comment lines excluded)."""
    source_lines = (ROOT / "cli" / "build.py").read_text().splitlines()
    offenders = [
        f"cli/build.py:{n}"
        for n, line in enumerate(source_lines, 1)
        if not line.lstrip().startswith("#") and build._WEAK_TEST_MODE_GATE_RE.search(line)
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Layer 2: the server.js startup guard
# ---------------------------------------------------------------------------

def test_server_guard_is_injected_before_the_app_is_created(monkeypatch):
    server = _run(monkeypatch, auth_js=_BARE_AUTH_JS, server_js=_SERVER_JS_NO_GUARD)["server.js"]
    assert "TEST_MODE must not be set in production" in server
    assert server.index("process.exit(1)") < server.index("const app = express()")
    assert server.count("process.exit(1)") == 1


def test_server_guard_not_duplicated_when_already_present(monkeypatch):
    server = _run(monkeypatch, auth_js=_BARE_AUTH_JS, server_js=_SERVER_JS_WITH_GUARD)["server.js"]
    assert server == _SERVER_JS_WITH_GUARD


def test_server_guard_prepended_when_no_app_declaration(monkeypatch):
    server = _run(monkeypatch, auth_js=_BARE_AUTH_JS, server_js="console.log('x');\n")["server.js"]
    assert server.startswith("// TEST_MODE must never be live in production")
    assert server.rstrip().endswith("console.log('x');")


def test_no_guard_when_nothing_references_test_mode(monkeypatch):
    out = _run(monkeypatch, server_js=_SERVER_JS_NO_GUARD)  # no routes/auth.js, so no shortcut
    assert out["server.js"] == _SERVER_JS_NO_GUARD


@_NEEDS_NODE
def test_server_guard_exits_only_when_test_mode_meets_production():
    guard = build._TEST_MODE_PRODUCTION_GUARD_JS

    def run(env):
        return subprocess.run(
            ["node", "-e", guard + "console.log('started');"],
            capture_output=True, text=True, timeout=10,
            env={"PATH": _clean_node_path(), **env},
        )

    refused = run({"TEST_MODE": "1", "NODE_ENV": "production"})
    assert refused.returncode == 1
    assert "TEST_MODE must not be set in production" in refused.stderr
    assert "started" not in refused.stdout

    for env in ({"TEST_MODE": "1", "NODE_ENV": "test"}, {"NODE_ENV": "production"}, {}):
        ok = run(env)
        assert ok.returncode == 0 and "started" in ok.stdout, env


# ---------------------------------------------------------------------------
# The deploy and test-runner sides of the contract
# ---------------------------------------------------------------------------

def test_deploy_sets_production_and_never_sets_test_mode(tmp_path, monkeypatch):
    """deploy() must hand Railway NODE_ENV=production and no TEST_MODE, even
    when the worker's own environment has TEST_MODE set."""
    monkeypatch.setenv("TEST_MODE", "true")
    sys.path.insert(0, str(ROOT / "cli"))
    import deploy as deploy_mod

    build_dir = tmp_path / "builds" / "app"
    build_dir.mkdir(parents=True)
    (build_dir / "package.json").write_text('{"name": "app"}')

    with patch.object(deploy_mod, "check_railway_cli"), \
         patch.object(deploy_mod, "get_token", return_value="t"), \
         patch.object(deploy_mod, "read_app_name", return_value="app"), \
         patch.object(deploy_mod, "create_project", return_value="p"), \
         patch.object(deploy_mod, "get_environment_id", return_value="e"), \
         patch.object(deploy_mod, "create_service", return_value="s"), \
         patch.object(deploy_mod, "provision_postgres", return_value=("d", "postgresql://x")), \
         patch.object(deploy_mod, "set_env_vars") as set_vars, \
         patch.object(deploy_mod, "deploy_source", return_value="dep"), \
         patch.object(deploy_mod, "wait_for_deployment"), \
         patch.object(deploy_mod, "generate_domain", return_value="app.up.railway.app"), \
         patch.object(deploy_mod, "verify_health", return_value="ok"), \
         patch.object(deploy_mod, "verify_auth", return_value="ok"):
        deploy_mod.deploy(str(build_dir))

    env_vars = set_vars.call_args[0][3]
    assert env_vars["NODE_ENV"] == "production"
    assert "TEST_MODE" not in env_vars


def test_test_runner_starts_the_app_outside_production(monkeypatch, tmp_path):
    """The build-time runner is the only place TEST_MODE is set, and it runs
    the app with NODE_ENV=test, so the gate above stays open there."""
    import test as test_runner

    monkeypatch.setenv("NODE_ENV", "production")  # a hostile parent env must not leak through
    captured = {}

    class _FakeProc:
        def poll(self):
            return 0  # already exited, so stop_app only closes the log handles

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProc()

    with patch.object(test_runner.subprocess, "Popen", side_effect=fake_popen):
        proc = test_runner.start_app(tmp_path, "cas_test_db", 4999)
    test_runner.stop_app(proc)  # closes the log file handles start_app opened

    assert captured["env"]["TEST_MODE"] == "true"
    assert captured["env"]["NODE_ENV"] == "test"
