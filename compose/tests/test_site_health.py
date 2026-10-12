"""A generated site's /health also answers for its home page.

The worker announces a preview when the deployed site's /health says ok.
That used to mean "the database answers": a site whose home page threw on
every request passed. The emitted services/health.js asks the site for its
own home page once the listener is up; /health is ok only when the
database answers AND that check has passed.

Here the emitted module is run for real under node against a stand-in for
the home page (`_health_check_probe.js`), which needs nothing but node.
The whole emitted server, with a home view that throws, is booted against
Postgres in compose/tests/test_compose_end_to_end.py
(`test_health_fails_while_the_home_page_cannot_render`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from compose.build import BuildSpec, compose_files, resolve_spec

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR.parent / "fixtures"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


@pytest.fixture(scope="module")
def emitted() -> dict:
    spec = json.loads((FIXTURES / "sample-restaurant-spec.json").read_text(encoding="utf-8"))
    return dict(compose_files(resolve_spec(BuildSpec.from_dict(spec))))


@pytest.fixture(scope="module")
def observed(emitted, tmp_path_factory) -> dict:
    """What `_health_check_probe.js` saw when it drove the emitted module."""
    if NODE is None:
        pytest.skip("node is not on PATH")
    module_path = tmp_path_factory.mktemp("health") / "health.js"
    module_path.write_text(emitted["services/health.js"], encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(TESTS_DIR / "_health_check_probe.js"), str(module_path)],
        capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout)


@needs_node
def test_nothing_is_ok_before_the_listener_is_up(observed):
    assert observed["beforeStart"] is False
    assert observed["hitsBeforeStart"] == 0


@needs_node
def test_a_home_page_that_answers_500_fails_the_check_and_is_not_asked_again_at_once(observed):
    assert observed["startWith500"] is False
    assert observed["okRightAfterFailure"] is False
    assert observed["hitsAfterFailure"] == 1, "a probe inside the retry gap must not render the page again"
    assert any("home page check failed: GET / answered 500" in line for line in observed["logged"])


@needs_node
def test_a_failure_is_retried_later_and_concurrent_probes_share_one_request(observed):
    assert observed["okAfterRecovery"] == [True, True, True]
    assert observed["hitsAfterRecovery"] == 2, "three probes at once made more than one request"


@needs_node
def test_a_pass_is_remembered_so_a_probe_does_not_render_the_page(observed):
    """The cost of the design: after one pass /health never asks for the
    home page again, so a page that breaks later is not noticed."""
    assert observed["okAfterLaterBreak"] == [True, True]
    assert observed["hitsAfterLaterBreak"] == observed["hitsAfterRecovery"]


@needs_node
def test_a_home_page_that_never_answers_fails_within_the_timeout(observed):
    assert observed["hangResult"] is False
    assert 250 <= observed["hangTookMs"] < 2000, observed["hangTookMs"]
    assert any("answered nothing in time" in line for line in observed["logged"])


@needs_node
def test_only_a_server_error_or_silence_counts_as_a_broken_home_page(observed):
    assert observed["byStatus"] == {
        "200": True, "302": True, "404": True, "499": True, "500": False, "503": False,
    }
    assert observed["closedPort"] is False


def test_health_route_requires_the_database_and_the_home_page(emitted):
    """Reads the emitted server.js (the route itself runs in the booted-site
    test named in this file's docstring)."""
    server = emitted["server.js"]
    health = server[server.index("app.get('/health'"):server.index("// Auth routes")]
    database, home, ok = (
        health.index("await db.query('SELECT 1')"),
        health.index("if (!(await homeCheck.ok()))"),
        health.index("res.json({status: 'ok'})"),
    )
    assert database < home < ok
    assert "res.status(503).json({status: 'home_page_error'});" in health
    assert "const homeCheck = createHomeCheck();" in server
    listen = server[server.index("app.listen(PORT"):]
    assert "homeCheck.start(PORT);" in listen
