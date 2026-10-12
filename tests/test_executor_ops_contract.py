"""The executor loop and the things it reads, checked against each other.

scripts/cas-roadmap-loop.sh decides whether a push may go out from what other
components say, each in another language or another file:

  - the worker's build stages (queue/worker_pg.py),
  - the per-status build counts, the cost report and the executor routes of the
    web app's ops API (web/src/routes/ops-api.ts, web/src/routes/admin.ts),
  - the worker's watchPatterns (railway.toml),
  - the watchdog's idea of which dirty files make the loop yield
    (scripts/cas-executor-watchdog.sh).

Nothing but these tests notices when one side of such a pair changes. They read
source text, so they pin names and shapes, not behavior. The behavior (the real
loop run against a stand-in API that answers with these shapes) is in
test_executor_loop_behavior.py, and this file also checks that the stand-in
answers with the shapes the real routes have.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP = (REPO_ROOT / "scripts" / "cas-roadmap-loop.sh").read_text()
WATCHDOG = (REPO_ROOT / "scripts" / "cas-executor-watchdog.sh").read_text()
OPS_API = (REPO_ROOT / "web" / "src" / "routes" / "ops-api.ts").read_text()
ADMIN = (REPO_ROOT / "web" / "src" / "routes" / "admin.ts").read_text()
WORKER = (REPO_ROOT / "queue" / "worker_pg.py").read_text()


def _loop_value(name: str) -> str:
    return re.search(rf"^{name}=(['\"])(.*)\1$", LOOP, re.M).group(2)


def _loop_function(name: str) -> str:
    start = LOOP.index(f"{name}() {{")
    return LOOP[start:LOOP.index("\n}\n", start)]


def _route(method: str, path: str) -> str:
    """The source of one ops API route handler."""
    start = OPS_API.index(f"router.{method}('{path}'")
    following = OPS_API.find("\nrouter.", start + 1)
    return OPS_API[start:following if following != -1 else len(OPS_API)]


def _worker_stages() -> tuple:
    literal = re.search(r"^_STAGE_ORDER = \(([^)]*)\)", WORKER, re.M).group(1)
    return tuple(re.findall(r'"([a-z_]+)"', literal))


def _payload_keys(function_body: str) -> list:
    """The JSON keys of the `_json_payload key value type \\` block in a loop function."""
    block = function_body[function_body.index("_json_payload \\"):]
    block = block[:block.index("\n    )")]
    return re.findall(r"^\s+([a-z_]+) \"", block, re.M)


def _destructured(handler: str) -> set:
    return set(re.search(r"const \{([^}]*)\} = req\.body", handler).group(1).replace(" ", "").split(","))


# ---------------------------------------------------------------------------
# Which builds a push must wait for
# ---------------------------------------------------------------------------

def test_the_loop_waits_for_every_status_in_which_the_worker_has_a_build():
    stages = _worker_stages()
    assert stages == ("building", "testing", "deploying"), \
        "the worker's stage list changed: decide whether the executor must wait for the new stage"
    assert set(_loop_value("ACTIVE_BUILD_STATUSES").split()) == {"queued", *stages}


def test_the_claude_code_push_hook_script_counts_the_same_stages():
    # scripts/wait_for_worker.py (run by the example PreToolUse hook) asks the
    # same question for a person's own push
    script = (REPO_ROOT / "scripts" / "wait_for_worker.py").read_text()
    literal = re.search(r"^ACTIVE_STATUSES = \{([^}]*)\}", script, re.M).group(1)
    assert set(re.findall(r'"([a-z_]+)"', literal)) == set(_worker_stages())


def test_the_ops_api_reports_each_of_those_counts_where_the_loop_reads_them():
    count = _loop_function("active_build_count")
    assert '"$OPS_BASE/ops/api/v1/builds?limit=1"' in count
    assert 'json.load(sys.stdin)["stats"]' in count
    handler = _route("get", "/v1/builds")
    assert "stats: statsResult.rows[0]" in handler
    for status in _loop_value("ACTIVE_BUILD_STATUSES").split():
        assert f"COUNT(*) FILTER (WHERE status = '{status}') AS {status}" in handler, status
    # the count must not depend on the listing's filters or its row limit
    stats_query = handler[handler.index("const statsResult"):]
    assert "FROM builds\n" in stats_query and "WHERE ${" not in stats_query


def test_a_push_from_the_loop_always_matches_the_workers_watch_patterns():
    # Why the build check cannot be skipped for "web-only" changes: the push
    # also carries the item's roadmap.json commit, and coordinator/ is watched.
    railway = (REPO_ROOT / "railway.toml").read_text()
    start = railway.index("watchPatterns = [")
    patterns = re.findall(r'"([^"]+)"', railway[start:railway.index("]", start)])
    assert "coordinator/**" in patterns
    assert 'local roadmap="coordinator/roadmap.json"' in _loop_function("write_roadmap_status")
    assert 'write_roadmap_status "$@"' in _loop_function("record_roadmap_status")
    merge_path = LOOP[LOOP.index('echo "Merging $branch ($tested_sha) to $TARGET_BRANCH..."'):]
    mark = merge_path.index('record_roadmap_status "$item_id" completed')
    push = merge_path.index('with_timeout "$PUSH_TIMEOUT_SEC" git push')
    assert mark < push, "the completed mark is committed before the push that carries it"


# ---------------------------------------------------------------------------
# The cost report and the executor routes
# ---------------------------------------------------------------------------

def test_the_cost_report_has_the_fields_the_spend_gate_reads():
    verdict = _loop_function("spend_verdict")
    assert '"$OPS_BASE/ops/api/v1/costs"' in verdict
    assert 'summary.get("today_total")' in verdict
    assert 'report.get("platform_costs_partial") is not False' in verdict
    assert 'spend=$(spend_verdict "$ceiling")' in _loop_function("preflight_check")
    assert "getCostData" in _route("get", "/v1/costs")
    cost_data = ADMIN[ADMIN.index("export async function getCostData"):]
    assert re.search(r"summary: \{[^}]*today_total: todayTotal", cost_data, re.S)
    # The route says when its platform figures are a lower bound, as a
    # boolean that is always present; the loop starts no session on true.
    assert "platform_costs_partial: platformCostsPartial," in cost_data
    declared = ADMIN[ADMIN.index("interface CostDashboardData {"):]
    declared = declared[:declared.index("\n}\n")]
    assert re.search(r"^\s+platform_costs_partial: boolean;", declared, re.M)
    assert re.search(r"^\s+summary: CostSummary;", declared, re.M)
    summary = ADMIN[ADMIN.index("interface CostSummary {"):]
    assert re.search(r"^\s+today_total: number;", summary[:summary.index("\n}\n")], re.M)
    # With no Anthropic cost API behind it the report is partial by the
    # route's own rule, so an executor needs that API configured to run.
    assert re.search(r"platformCostsPartial = true;\s+platformCostsPartialReason =\s+"
                     r"platformCostsPartialReason \|\| 'admin_api_unavailable_using_scraped_platform_costs'",
                     cost_data)


def test_every_heartbeat_field_the_loop_sends_is_one_the_route_stores():
    stored = _destructured(_route("post", "/v1/executor/heartbeat"))
    assert stored == {"timestamp", "status", "current_item", "skipped_reason"}
    assert set(_payload_keys(_loop_function("post_heartbeat"))) == stored
    # the two heartbeats that are written out by hand
    crash = re.search(r'-d "\{(.*)\}" \\', _loop_function("on_exit")).group(1)
    assert set(re.findall(r'\\"([a-z_]+)\\":', crash)) <= stored
    watchdog = re.search(r'-d "\{(.*)\}" \\', WATCHDOG).group(1)
    assert set(re.findall(r'\\"([a-z_]+)\\":', watchdog)) <= stored
    # no heartbeat is built anywhere else
    assert LOOP.count("/ops/api/v1/executor/heartbeat") == 2
    assert LOOP.count("_json_payload \\") == 2


def test_every_result_field_the_loop_sends_is_one_the_route_stores():
    stored = _destructured(_route("post", "/v1/executor/result"))
    assert set(_payload_keys(_loop_function("post_result"))) == stored


def test_statuses_the_loop_reports_fit_the_executor_runs_column():
    schema = (REPO_ROOT / "web" / "src" / "db" / "schema.sql").read_text()
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS executor_runs"):]
    width = int(re.search(r"status VARCHAR\((\d+)\)", table).group(1))
    reported = set(re.findall(r'post_heartbeat "([a-z_]+)"', LOOP))
    reported |= set(re.findall(r'post_result "\$item_id" "\$item_title" "([a-z_]+)"', LOOP))
    reported |= {"crashed", "watchdog"}
    assert {"idle", "paused", "running", "working", "failed", "pr_created", "completed", "review"} <= reported
    assert all(len(status) <= width for status in reported), reported
    # the zombie sweep looks for the statuses a live run reports
    sweep = _route("post", "/v1/executor/sweep-zombies")
    swept = set(re.findall(r"'([a-z_]+)'", re.search(r"WHERE status IN \(([^)]*)\)", sweep).group(1)))
    assert swept <= reported


# ---------------------------------------------------------------------------
# The stand-in API of the behavior tests answers with the real shapes
# ---------------------------------------------------------------------------

def test_the_stand_in_ops_api_has_the_shapes_of_the_real_routes():
    from tests.test_executor_loop_behavior import OpsApi

    api = OpsApi()
    try:
        status, listing = api._respond("GET", "/ops/api/v1/builds?limit=1", None)
        real_counts = set(re.findall(r" AS (\w+)", _route("get", "/v1/builds").split("const statsResult")[1]))
        assert status == 200 and set(listing) == {"builds", "stats"}
        assert set(listing["stats"]) == real_counts
        assert all(isinstance(v, str) for v in listing["stats"].values()), "node-postgres returns COUNT(*) as text"

        _, summary = api._respond("GET", "/ops/api/v1/stats", None)
        first_query = _route("get", "/v1/stats").split("const customerStats")[0]
        assert set(summary["builds"]) == set(re.findall(r" AS (\w+)", first_query))

        _, costs = api._respond("GET", "/ops/api/v1/costs", None)
        assert "today_total" in costs["summary"]
        assert costs["platform_costs_partial"] is False
        declared = ADMIN[ADMIN.index("interface CostDashboardData {"):]
        declared = declared[:declared.index("\n}\n")]
        assert set(costs) <= set(re.findall(r"^\s+(\w+): ", declared, re.M))
    finally:
        api.close()


# ---------------------------------------------------------------------------
# Loop and watchdog agree on which dirty files do not stop a cycle
# ---------------------------------------------------------------------------

def test_loop_and_watchdog_tolerate_the_same_dirty_files():
    expression = _loop_value("DIRTY_OK_RE")
    assert re.search(r"^DIRTY_OK_RE='(.*)'$", WATCHDOG, re.M).group(1) == expression
    assert "roadmap" not in expression, "an uncommitted roadmap.json must make the cycle yield"
    for tolerated in (" M cli/failure_kb.json", " M scripts/cas-executor-config.json", " M CLAUDE.md"):
        assert re.search(expression, tolerated)
    for blocking in (" M coordinator/roadmap.json", " M web/src/app.ts", " M scripts/cas-roadmap-loop.sh"):
        assert not re.search(expression, blocking)
