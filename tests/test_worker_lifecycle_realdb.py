"""The build worker's lifecycle across its seams, on a real PostgreSQL.

tests/test_worker_pg_realdb.py checks each guarded statement on its own. The
tests here follow one build through several of them, together with the things
on the other side of each seam: the statement the web app's ops API uses to
requeue a build (read from its TypeScript source and executed), the schema's
ON DELETE rules, the monitor registry, the mailer, and Railway (the one thing
that is faked, at the functions of cli/deploy.py that talk to it).

  * Railway projects: a failing attempt deletes the project it created itself
    and never the one the build row records, which after an in-place requeue of
    a previewed build is the previous, working, announced site.
  * The health gate: a deployed app that never answers does not become a
    preview on either engine, the attempt is retried as a transient failure,
    and no model-driven fix loop runs for it.
  * The worker loop: a build is claimed only when a pool thread is free, so a
    build never sits claimed, unowned and unleased while another build's thread
    is still busy after its row left the pipeline statuses.
  * The janitor: it deletes old failed duplicates without erasing ancestry,
    hand-off stamps or a build that was retried in the meantime.
  * The stuck-thread path: a deploy the supervisor recovers is announced like
    any other preview, and a thread nobody waits for removes what it created.

The pipeline itself is the real _run_pipeline wherever a test says so: the
build row carries a stored artifact, so the worker restores it and resumes
from the test stage, exactly as it does for a previewed build that is requeued
in place.

Needs a local PostgreSQL (see scratch_pg_url in tests/conftest.py); skips
otherwise.
"""

import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
for _p in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import worker_pg

from tests.test_worker_pg_realdb import (  # conn and worker_db_url are fixtures
    _build,
    _customer,
    _customer_email,
    _dead_letters,
    _emails,
    _release_lease,
    _row,
    _token,
    _wait_until_the_lease_is_gone,
    conn,
    worker_db_url,
)

OPS_API_TS = ROOT / "web" / "src" / "routes" / "ops-api.ts"
BUILDS_TS = ROOT / "web" / "src" / "routes" / "builds.ts"


# ---------------------------------------------------------------------------
# The web app's side of the seam
# ---------------------------------------------------------------------------

def _ts_template(source: str, after: str) -> str:
    """The first backtick template literal that follows `after` in TypeScript source."""
    start = source.index("`", source.index(after)) + 1
    return source[start:source.index("`", start)]


def _ops_requeue_sql() -> str:
    """The statement POST /ops/api/v1/builds/:buildId/requeue executes, as psycopg2 SQL.

    Read from the route's source so that this test runs what the web app runs,
    whatever that statement becomes."""
    reset = _ts_template(BUILDS_TS.read_text(encoding="utf-8"), "export const REVIEW_RESET_ASSIGNMENTS =")
    sql = _ts_template(OPS_API_TS.read_text(encoding="utf-8"), "router.post('/v1/builds/:buildId/requeue'")
    assert "UPDATE builds" in sql and "status = 'queued'" in sql, sql
    sql = sql.replace("${REVIEW_RESET_ASSIGNMENTS}", reset)
    assert "${" not in sql, sql
    assert "$2" not in sql, "the requeue statement takes one parameter (the build id)"
    return sql.replace("$1", "%s")


def _requeue_through_the_ops_api_statement(conn, build_id) -> None:
    with conn.cursor() as cur:
        cur.execute(_ops_requeue_sql(), (build_id,))
        assert cur.rowcount == 1, "the ops API statement did not requeue the build"
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def builds_root(tmp_path, monkeypatch):
    root = tmp_path / "builds"
    root.mkdir()
    monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", root)
    return root


@pytest.fixture()
def railway_token(monkeypatch):
    """A Railway token is present (the value is made up at run time), so the
    pipeline takes the deployed path and may call the faked Railway functions."""
    monkeypatch.setenv("RAILWAY_API_TOKEN", "not-a-token-" + uuid.uuid4().hex)


@pytest.fixture()
def no_waiting(monkeypatch):
    """The pipeline waits between health polls; none of that is under test."""
    monkeypatch.setattr(worker_pg.time, "sleep", lambda _seconds: None)


class FakeRailway:
    """Stands in for the functions of cli/deploy.py that talk to Railway or to
    the deployed app. Everything the worker does with their results is real."""

    def __init__(self, *, health="ok", later_health=None, deploy_error=None, project="proj-new",
                 deployment_status=None, deploy_logs=None, unreadable_logs=False):
        self.project = project
        self.health = health
        self.later_health = list(later_health or [])
        self.deploy_error = deploy_error
        # Railway's own end state for the deployment, as deploy() reports it
        # (None: a deploy result without the key), and the deployment's log.
        self.deployment_status = deployment_status
        self.deploy_logs = list(deploy_logs or [])
        # The deployment exists but asking Railway for its log fails.
        self.unreadable_logs = unreadable_logs
        self.deploys = 0
        self.destroyed = []
        self.health_probes = 0
        self.fix_loops = 0

    def deploy(self, build_dir, custom_domain=None, *, password_login_probe=True):
        self.deploys += 1
        if self.deploy_error is not None:
            # deploy() deletes the project of its own failed call before it raises.
            raise self.deploy_error
        result = {
            "live_url": f"https://{self.project}.example.test",
            "railway_project_id": self.project,
            "railway_service_id": f"svc-{self.project}",
            "railway_environment_id": f"env-{self.project}",
            "database_url": None,
            "health_check": self.health,
        }
        if self.deployment_status is not None:
            result["deployment_status"] = self.deployment_status
        return result

    def verify_health(self, url, timeout=120):
        self.health_probes += 1
        if self.later_health:
            return self.later_health.pop(0)
        return self.health

    def destroy_project(self, project_id, token):
        self.destroyed.append(project_id)
        return True

    def deployment_logs(self, *args, raise_on_error=False, **kwargs):
        import deploy
        if self.unreadable_logs:
            if raise_on_error:
                raise deploy.DeploymentLogsUnreadable("the log could not be read")
            return []
        return list(self.deploy_logs)

    def fix_loop(self, *args, **kwargs):
        self.fix_loops += 1
        return {"fixed": False, "files_changed": [], "description": "must never run"}

    def installed(self):
        import deploy
        import deploy_fix
        return _all(
            patch.object(deploy, "deploy", self.deploy),
            patch.object(deploy, "verify_health", self.verify_health),
            patch.object(deploy, "destroy_project", self.destroy_project),
            patch.object(deploy, "verify_magic_link_auth", lambda *a, **k: "ok"),
            patch.object(deploy, "verify_routes", lambda *a, **k: {"status": "ok", "passed": 0}),
            patch.object(deploy, "verify_auth", lambda *a, **k: "ok"),
            patch.object(deploy, "verify_data", lambda *a, **k: {"status": "ok", "details": []}),
            patch.object(deploy, "capture_deploy_screenshots", lambda *a, **k: []),
            patch.object(deploy, "verify_render", lambda *a, **k: {"status": "ok", "screenshots": []}),
            patch.object(deploy, "get_latest_deployment",
                         lambda *a, **k: "dep-1" if (self.deploy_logs or self.unreadable_logs) else None),
            patch.object(deploy, "get_deployment_logs", self.deployment_logs),
            patch.object(deploy_fix, "attempt_deploy_fix", self.fix_loop),
            patch.object(worker_pg, "sentry_sdk"),
        )


class _all:
    """Enter several context managers as one."""

    def __init__(self, *managers):
        self.managers = managers

    def __enter__(self):
        for manager in self.managers:
            manager.__enter__()
        return self

    def __exit__(self, *exc):
        for manager in reversed(self.managers):
            manager.__exit__(*exc)
        return False


def _store_site_artifact(conn, builds_root, build_id) -> None:
    """Give the build row a stored artifact, made by the worker's own function
    from a minimal site, and leave nothing on disk: the next attempt has to
    restore it, as it would in a fresh worker container."""
    import shutil

    site = builds_root / f"demo-app-{build_id[:8]}"
    (site / "db").mkdir(parents=True)
    (site / "package.json").write_text('{"name": "demo-app"}')
    (site / "server.js").write_text("// site\n")
    (site / "db" / "seed.sql").write_text("-- seed\n")
    worker_pg._store_build_artifact(conn, build_id, str(site))
    shutil.rmtree(site)


def _previewed_build(conn, builds_root, customer, *, project="proj-announced") -> str:
    """A build that reached preview on `project`, was reviewed and approved,
    and is registered with the monitor: the state the ops requeue starts from."""
    build = _build(conn, customer, "preview", review_status="approved", reviewed=True,
                   railway_project_id=project, started_age_s=7200)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE builds SET preview_url = %s, railway_service_id = %s, "
            "railway_environment_id = %s WHERE id = %s",
            (f"https://{project}.example.test", f"svc-{project}", f"env-{project}", build),
        )
        cur.execute(
            "INSERT INTO monitor_apps (app_name, live_url, railway_project_id, railway_service_id, "
            "railway_environment_id) VALUES ('demo-app', %s, %s, %s, %s)",
            (f"https://{project}.example.test", project, f"svc-{project}", f"env-{project}"),
        )
    conn.commit()
    _store_site_artifact(conn, builds_root, build)
    return build


def _railway_columns(conn, build_id) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT railway_project_id, railway_service_id, railway_environment_id, preview_url "
            "FROM builds WHERE id = %s", (build_id,))
        row = cur.fetchone()
    conn.commit()
    return row


def _monitored_projects(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT railway_project_id FROM monitor_apps ORDER BY railway_project_id")
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


def _logged(conn, build_id) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT message FROM build_logs WHERE build_id = %s ORDER BY id", (build_id,))
        text = " | ".join(r[0] for r in cur.fetchall())
    conn.commit()
    return text


# ---------------------------------------------------------------------------
# Railway projects: whose project may the worker delete?
# ---------------------------------------------------------------------------

class TestAFailedRedeployNeverTouchesTheAnnouncedProject:
    """The reviewer's scenario, end to end: a build is in preview on a working
    project, the ops API requeues the row in place, and the redeploy fails."""

    def _requeue_and_run(self, conn, build, railway):
        _requeue_through_the_ops_api_statement(conn, build)
        job = worker_pg._claim_next_job(conn)
        assert job is not None and job["id"] == build
        with railway.installed():
            worker_pg._process_job(job, conn)
        return job

    def test_deploy_fails_before_a_new_project_exists(self, conn, builds_root, railway_token,
                                                      no_waiting, monkeypatch):
        """deploy() itself failed (and cleaned up its own project). The only
        project id anywhere near this build is the announced one on the row.
        The old failure handler read that id from the row and deleted it."""
        monkeypatch.setattr(worker_pg, "_MAX_RETRIES", 0)
        customer = _customer(conn)
        build = _previewed_build(conn, builds_root, customer)
        railway = FakeRailway(deploy_error=RuntimeError("Railway rejected the upload"))

        self._requeue_and_run(conn, build, railway)

        assert railway.deploys == 1, "the pipeline must have reached the deploy"
        assert railway.destroyed == [], "the announced project was deleted"
        row = _row(conn, build)
        assert row["status"] == "failed"
        assert _railway_columns(conn, build) == (
            "proj-announced", "svc-proj-announced", "env-proj-announced",
            "https://proj-announced.example.test")
        assert _monitored_projects(conn) == ["proj-announced"]
        assert _emails(conn, "build_failed") == [
            ("build_failed", _customer_email(conn, customer), "skipped")]

    def test_deploy_succeeds_and_the_app_is_broken(self, conn, builds_root, railway_token,
                                                   no_waiting):
        """The attempt created its own project and the app crashes on start.
        That project, and only that one, goes; the row keeps naming the
        announced deployment."""
        customer = _customer(conn)
        build = _previewed_build(conn, builds_root, customer)
        railway = FakeRailway(health="failed", project="proj-of-this-attempt")

        self._requeue_and_run(conn, build, railway)

        assert railway.destroyed == ["proj-of-this-attempt"]
        assert railway.fix_loops == 0, "no model-driven fix loop on v2 output"
        row = _row(conn, build)
        assert row["status"] == "failed" and "[v2-deterministic]" in row["error_message"]
        assert _railway_columns(conn, build)[0] == "proj-announced"
        assert _railway_columns(conn, build)[3] == "https://proj-announced.example.test"
        assert _monitored_projects(conn) == ["proj-announced"]

    def test_a_successful_redeploy_is_recorded_by_the_preview_write(self, conn, builds_root,
                                                                    railway_token, no_waiting, caplog):
        """The new project becomes the row's project in the statement that
        moves the build to preview. The superseded one was announced, so the
        worker leaves it alone and names it in a warning."""
        customer = _customer(conn)
        build = _previewed_build(conn, builds_root, customer)
        railway = FakeRailway(health="ok", project="proj-redeployed")

        with caplog.at_level(logging.WARNING, logger="worker_pg"):
            self._requeue_and_run(conn, build, railway)

        assert railway.destroyed == []
        row = _row(conn, build)
        assert (row["status"], row["review_status"]) == ("preview", "pending")
        assert _railway_columns(conn, build) == (
            "proj-redeployed", "svc-proj-redeployed", "env-proj-redeployed",
            "https://proj-redeployed.example.test")
        assert _monitored_projects(conn) == ["proj-announced", "proj-redeployed"]
        assert any("proj-announced" in record.getMessage() and "operator" in record.getMessage()
                   for record in caplog.records), "the superseded project must be named"
        assert _emails(conn, "preview_ready") == [
            ("preview_ready", _customer_email(conn, customer), "skipped")]
        assert "Magic link login URL: https://proj-redeployed.example.test/magic?token=" in _logged(conn, build)

    def test_a_retried_attempt_removes_its_project_before_the_next_one_deploys(
            self, conn, builds_root, railway_token, no_waiting):
        """A transient failure after the deploy sends the build back to the
        queue. The project of that attempt was never announced and the next
        attempt deploys a new one, so it is deleted instead of leaking."""
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        _store_site_artifact(conn, builds_root, build)
        railway = FakeRailway(health="infra", project="proj-first-try")

        job = worker_pg._claim_next_job(conn)
        with railway.installed():
            worker_pg._process_job(job, conn)

        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1)
        assert railway.destroyed == ["proj-first-try"]
        assert _railway_columns(conn, build)[0] is None, "an unannounced project is never on the row"


class TestWhoMayDeleteAProject:
    def test_a_build_killed_while_deploying_loses_its_unannounced_project(self, conn, railway_token):
        """The admin kill lands while the attempt is deploying. The preview
        write then matches no row; the project the attempt deployed is on no
        row and nobody was told about it."""
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_age_s=60, railway_project_id="proj-announced")
        railway = FakeRailway()

        def pipeline(job, pipeline_conn, metrics):
            with pipeline_conn.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET status = 'failed', error_message = 'Force-killed by admin', "
                    "completed_at = NOW() WHERE id = %s", (job["id"],))
            pipeline_conn.commit()
            return railway.deploy(None)

        job = {"id": build, "tier": "build", "app_name": "demo-app", "spec": {},
               "customer_id": customer, "customer_email": _customer_email(conn, customer),
               "customer_name": "Test", "retry_count": 0, "rebuild_request": None,
               "error_message": None, "attempt": _token(conn, build)}
        with railway.installed(), patch.object(worker_pg, "_run_pipeline", side_effect=pipeline):
            worker_pg._process_job(job, conn)

        assert railway.destroyed == ["proj-new"]
        assert _railway_columns(conn, build)[0] == "proj-announced"
        assert _row(conn, build)["error_message"] == "Force-killed by admin"
        assert _emails(conn) == []

    def test_orphan_recovery_deletes_no_project(self, conn, builds_root, railway_token):
        """Recovery fails a build whose worker died. It has no record of what
        that worker's attempt created, and the project on the row is an
        announced deployment: nothing is deleted."""
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_age_s=300,
                       retry_count=worker_pg._MAX_RETRIES, railway_project_id="proj-announced")
        railway = FakeRailway()
        with railway.installed():
            assert worker_pg._recover_orphaned_builds(conn) == 0
        assert _row(conn, build)["status"] == "failed"
        assert railway.destroyed == []
        assert _railway_columns(conn, build)[0] == "proj-announced"

    def test_a_project_recorded_on_any_build_row_is_never_discarded(self, conn, railway_token):
        """The last check before a delete: if a build row names the project,
        it was announced (this is how a project the supervisor's probe adopted
        is recognized), whatever the attempt's own state says."""
        customer = _customer(conn)
        build = _build(conn, customer, "preview", railway_project_id="proj-adopted")
        job = {"id": build}
        worker_pg._note_attempt_project(job, {"railway_project_id": "proj-adopted"})
        railway = FakeRailway()
        with railway.installed():
            assert worker_pg._discard_unannounced_project(conn, job, "test") is False
        assert railway.destroyed == []

    def test_a_delete_that_fails_is_not_reported_as_done(self, conn, railway_token, caplog):
        customer = _customer(conn)
        build = _build(conn, customer, "failed")
        job = {"id": build}
        worker_pg._note_attempt_project(job, {"railway_project_id": "proj-stays"})
        import deploy
        with patch.object(deploy, "destroy_project",
                          side_effect=RuntimeError("destroy_project FAILED: Railway said no")), \
                caplog.at_level(logging.INFO, logger="worker_pg"):
            assert worker_pg._discard_unannounced_project(conn, job, "test") is False
        text = " ".join(record.getMessage() for record in caplog.records)
        assert "proj-stays was NOT deleted" in text and "deleted Railway project proj-stays" not in text
        assert worker_pg._attempt_state(job)["discarded"] is False


# ---------------------------------------------------------------------------
# The health gate
# ---------------------------------------------------------------------------

class TestSilenceIsNotAPreview:
    def _run(self, conn, builds_root, railway, *, marker=None):
        customer = _customer(conn)
        build = _build(conn, customer, "queued", error_message=marker)
        _store_site_artifact(conn, builds_root, build)
        job = worker_pg._claim_next_job(conn)
        assert job["id"] == build
        with railway.installed():
            worker_pg._process_job(job, conn)
        return build, customer

    @pytest.mark.parametrize("engine", ["v2", "v1"])
    def test_an_app_that_never_answers_is_retried_not_previewed(self, conn, builds_root,
                                                                railway_token, no_waiting,
                                                                monkeypatch, engine):
        """verify_health reports "infra" (no response ever arrived) for as long
        as the worker asks. The old code then set health to "ok", moved the
        build to preview, paged the reviewer and emailed the customer a login
        link."""
        monkeypatch.setenv("CAS_BUILD_ENGINE", engine)
        railway = FakeRailway(health="infra", project="proj-silent")
        # Under v1 the row resumes from the deploy stage (the v1 test stage
        # needs npm and a model); under v2 from the test stage, as usual.
        marker = "Requeued: resuming from deploying stage" if engine == "v1" else None

        build, customer = self._run(conn, builds_root, railway, marker=marker)

        row = _row(conn, build)
        assert row["status"] == "queued", row
        assert row["retry_count"] == 1
        assert row["error_message"].startswith("Retry 1/6: Deploy health check timeout")
        assert row["preview_url"] is None and row["review_status"] is None
        assert railway.health_probes >= 6, "the worker must have waited for the app first"
        assert railway.fix_loops == 0, "silence is not a code bug: no fix loop on either engine"
        assert _emails(conn) == [], "nobody is told about a site that never answered"
        assert "Magic link login URL" not in _logged(conn, build)
        assert _monitored_projects(conn) == []
        assert railway.destroyed == ["proj-silent"]

    def test_an_app_that_answers_late_still_reaches_preview(self, conn, builds_root,
                                                            railway_token, no_waiting):
        railway = FakeRailway(health="infra", later_health=["infra", "infra", "ok"],
                              project="proj-slow")
        build, customer = self._run(conn, builds_root, railway)
        row = _row(conn, build)
        assert (row["status"], row["preview_url"]) == ("preview", "https://proj-slow.example.test")
        assert railway.destroyed == []
        assert _emails(conn, "preview_ready") == [
            ("preview_ready", _customer_email(conn, customer), "skipped")]

    def test_a_v1_app_whose_database_never_comes_up_is_retried_without_a_fix_loop(
            self, conn, builds_root, railway_token, no_waiting, monkeypatch):
        """v1: deploy reports "failed", the deploy log shows a database that
        refused connections, and the re-probes keep saying "infra". The old
        code counted "infra" as recovered here and went to preview."""
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v1")
        railway = FakeRailway(health="failed", later_health=["infra"] * 20, project="proj-nodb")
        import deploy
        customer = _customer(conn)
        build = _build(conn, customer, "queued",
                       error_message="Requeued: resuming from deploying stage")
        _store_site_artifact(conn, builds_root, build)
        job = worker_pg._claim_next_job(conn)
        with railway.installed(), \
                patch.object(deploy, "get_latest_deployment", lambda *a, **k: "dep-1"), \
                patch.object(deploy, "get_deployment_logs",
                             lambda *a, **k: ["Error: connect ECONNREFUSED 10.0.0.2:5432"]):
            worker_pg._process_job(job, conn)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1), row
        assert row["preview_url"] is None
        assert railway.fix_loops == 0
        assert _emails(conn) == []

    def test_the_never_healthy_error_is_transient_and_not_deterministic(self):
        message = worker_pg._DEPLOY_NEVER_HEALTHY.format(last="infra")
        assert worker_pg._is_transient_error(message)
        assert "[v2-deterministic]" not in message
        assert worker_pg._classify_error(message) == "transient"


# ---------------------------------------------------------------------------
# What ended the deploy decides whether the build is tried again
# ---------------------------------------------------------------------------

class TestWhatEndedTheDeployDecidesTheRetry:
    """A v2 build is deterministic, so "the generated app crashes on start" is
    final: no retry, the customer is told, the attempt's project is removed.
    That verdict used to be given to every deploy Railway reported as CRASHED
    or FAILED, and to any error whose text contained those words."""

    def _attempt(self, conn, builds_root, build, railway):
        job = worker_pg._claim_next_job(conn)
        assert job is not None and job["id"] == build
        with railway.installed():
            worker_pg._process_job(job, conn)

    def _queued_build(self, conn, builds_root):
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        _store_site_artifact(conn, builds_root, build)
        return customer, build

    def test_a_deployment_railway_could_not_start_is_tried_once_more_then_failed(
            self, conn, builds_root, railway_token, no_waiting):
        """FAILED: Railway's build or start step did not complete, so the app
        never ran. The build gets a second attempt; when Railway says the same
        again the series ends, without the deterministic label."""
        customer, build = self._queued_build(conn, builds_root)
        railway = FakeRailway(health="failed", deployment_status="FAILED", project="proj-first")

        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1), row
        assert "[v2-deterministic]" not in row["error_message"]
        assert railway.destroyed == ["proj-first"], "the attempt's unannounced project goes with it"
        assert _emails(conn, "build_failed") == [], "nobody is told a build failed while it is retried"

        # The first attempt cleared the stored artifact (a non-transient
        # failure rebuilds from scratch); give the row one again so that this
        # test stays about the deploy and does not run the compose engine.
        _store_site_artifact(conn, builds_root, build)
        railway.project = "proj-second"
        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert row["status"] == "failed" and "[v2-deterministic]" not in row["error_message"]
        assert "Railway reported the deployment FAILED" in row["error_message"]
        assert railway.deploys == 2 and railway.destroyed == ["proj-first", "proj-second"]
        assert [letter[0] for letter in _dead_letters(conn, build)] == ["infra"]
        assert railway.fix_loops == 0

    def test_an_app_that_crashes_on_start_is_final(self, conn, builds_root, railway_token, no_waiting):
        customer, build = self._queued_build(conn, builds_root)
        railway = FakeRailway(health="failed", deployment_status="CRASHED",
                              deploy_logs=["Error: Cannot find module './routes/missing'"])
        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("failed", 0), row
        assert "[v2-deterministic]" in row["error_message"]
        assert railway.deploys == 1 and railway.destroyed == ["proj-new"]
        assert _emails(conn, "build_failed") == [
            ("build_failed", _customer_email(conn, customer), "skipped")]

    @pytest.mark.parametrize("how", ["the log request fails", "there is no deployment to ask"])
    def test_a_crash_whose_log_cannot_be_read_is_not_called_the_apps_fault(
            self, conn, builds_root, railway_token, no_waiting, how):
        """CRASHED, and nothing could be read about why. The old code treated
        the empty answer as "the app crashed for a reason of its own": tagged
        deterministic, never retried, the customer told. Now the attempt is
        retried once, and a second unreadable crash ends the series, still
        without the deterministic label."""
        customer, build = self._queued_build(conn, builds_root)
        railway = FakeRailway(health="failed", deployment_status="CRASHED", project="proj-first",
                              unreadable_logs=(how == "the log request fails"))
        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1), row
        assert "[v2-deterministic]" not in row["error_message"]
        assert "no log of it could be read" in row["error_message"]
        assert railway.destroyed == ["proj-first"]
        assert _emails(conn, "build_failed") == [], "nobody is told a build failed while it is retried"

        _store_site_artifact(conn, builds_root, build)
        railway.project = "proj-second"
        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert row["status"] == "failed" and "[v2-deterministic]" not in row["error_message"]
        assert railway.deploys == 2 and railway.destroyed == ["proj-first", "proj-second"]
        assert [letter[0] for letter in _dead_letters(conn, build)] == ["infra"]
        assert _emails(conn, "build_failed") == [
            ("build_failed", _customer_email(conn, customer), "skipped")]

    def test_a_crash_because_the_database_was_unreachable_is_retried(self, conn, builds_root,
                                                                     railway_token, no_waiting):
        """CRASHED, and the deployment's own log says why: it could not reach
        the Postgres service the platform had just provisioned."""
        customer, build = self._queued_build(conn, builds_root)
        railway = FakeRailway(health="failed", deployment_status="CRASHED",
                              deploy_logs=["Starting server", "Error: connect ECONNREFUSED 10.0.0.2:5432"])
        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1), row
        assert "[v2-deterministic]" not in row["error_message"]
        assert worker_pg._is_transient_error(row["error_message"])
        assert _emails(conn, "build_failed") == []

    def test_an_unconfirmed_project_creation_is_named_in_the_builds_log(self, conn, builds_root,
                                                                        railway_token, no_waiting):
        """deploy.create_project could not learn whether Railway created the
        project. Nothing records such a project, so the attempt says which
        ones it could be where an operator looks."""
        import deploy
        customer, build = self._queued_build(conn, builds_root)
        unknown = deploy.RailwayOutcomeUnknown("Railway did not confirm the mutation ProjectCreate")
        unknown.unrecorded_project_candidates = ["proj-made-unseen"]
        railway = FakeRailway(deploy_error=unknown)
        self._attempt(conn, builds_root, build, railway)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1), row
        assert railway.destroyed == [], "the worker deletes nothing it has no record of"
        assert "proj-made-unseen" in _logged(conn, build)


class TestTheSupervisorDeadlineIsNotAnExceptionClass:
    """_process_job waits for the pipeline until the supervisor deadline. From
    Python 3.11 the timeout that wait raises IS the builtin TimeoutError, and
    the old `except` around future.result(timeout=...) also caught a
    TimeoutError raised by the pipeline itself."""

    def test_a_timeout_error_raised_by_the_pipeline_is_an_ordinary_failed_attempt(
            self, conn, builds_root, monkeypatch):
        build = _build(conn, _customer(conn), "queued")

        def pipeline(job, pipeline_conn, metrics):
            raise TimeoutError("The read operation timed out")   # what a socket read raises

        monkeypatch.setattr(worker_pg, "_run_pipeline", pipeline)
        job = worker_pg._claim_next_job(conn)
        with patch.object(worker_pg, "_supervisor_force_fail") as supervisor, \
                patch.object(worker_pg, "sentry_sdk"):
            worker_pg._process_job(job, conn)
        supervisor.assert_not_called()
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 1), row
        assert row["error_message"] == "Retry 1/6: The read operation timed out"
        assert not _has_supervisor_retry(conn, build)

    @pytest.mark.parametrize("timeout_class", ["builtin", "a distinct class, as before Python 3.11"])
    def test_a_pipeline_that_is_still_running_at_the_deadline_goes_to_the_supervisor(
            self, conn, builds_root, monkeypatch, timeout_class):
        """Whatever class the wait raises: what decides is that the pipeline
        has not finished."""
        import concurrent.futures

        class _LegacyTimeout(concurrent.futures._base.Error):
            pass

        raised = TimeoutError() if timeout_class == "builtin" else _LegacyTimeout()
        build = _build(conn, _customer(conn), "queued")
        release = threading.Event()

        def pipeline(job, pipeline_conn, metrics):
            release.wait(20)
            return {"live_url": None, "local_preview": True}

        real_executor = worker_pg.ThreadPoolExecutor

        class _Executor(real_executor):
            def submit(self, fn, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                real_exception = future.exception

                def exception(timeout=None):
                    try:
                        return real_exception(timeout=0.3)
                    except BaseException:
                        raise raised from None
                future.exception = exception
                return future

        monkeypatch.setattr(worker_pg, "_run_pipeline", pipeline)
        monkeypatch.setattr(worker_pg, "ThreadPoolExecutor", _Executor)
        job = worker_pg._claim_next_job(conn)
        try:
            with patch.object(worker_pg, "_supervisor_force_fail",
                              return_value=(True, "retry", None)) as supervisor, \
                    patch.object(worker_pg, "sentry_sdk"):
                worker_pg._process_job(job, conn)
        finally:
            release.set()
        supervisor.assert_called_once()
        assert supervisor.call_args.args[0] == build


def _has_supervisor_retry(conn, build_id) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(supervisor_retry_used, FALSE) FROM builds WHERE id = %s", (build_id,))
        used = cur.fetchone()[0]
    conn.commit()
    return bool(used)


# ---------------------------------------------------------------------------
# The worker loop: claims, slots and ownership
# ---------------------------------------------------------------------------

class TestAClaimNeedsAFreeThread:
    @pytest.fixture()
    def quiet_worker(self, monkeypatch):
        """run_worker with everything but the build loop switched off, one
        build thread, and recovery passes that would normally be minutes apart
        compressed into fractions of a second."""
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 1)
        monkeypatch.setattr(worker_pg, "_POLL_INTERVAL", 0.05)
        monkeypatch.setattr(worker_pg, "_ORPHAN_RECOVERY_INTERVAL_S", 0.05)
        monkeypatch.setattr(worker_pg, "_ORPHAN_MIN_AGE_S", 0)
        monkeypatch.setattr(worker_pg, "_MAX_RETRIES", 2)
        for name in ("_start_monitor_thread", "_start_backup_thread", "_start_ops_agent_thread",
                     "_start_coordinator_thread", "_startup_disk_cleanup"):
            monkeypatch.setattr(worker_pg, name, lambda: None)
        monkeypatch.setattr(worker_pg, "_sweep_orphan_railway_projects", lambda _conn: 0)
        monkeypatch.setattr(worker_pg.signal, "signal", lambda *_args: None)
        worker_pg._shutdown_event.clear()
        yield
        worker_pg._shutdown_event.set()

    def test_a_queued_build_waits_while_the_only_thread_finishes_its_tail(self, conn, quiet_worker,
                                                                          monkeypatch):
        """Build A is in preview, but its thread is still sending the
        customer's email (held here on purpose). The status count says no
        build is active. The old loop claimed B at once: B sat in the pool's
        queue as 'building', owned and leased by nobody, each recovery pass
        requeued it and took one of its retries, and with none left it was
        failed and its customer was told so, for a build that never ran."""
        customer = _customer(conn)
        first = _build(conn, customer, "queued", age_minutes=10, app_name="first-app")
        second = _build(conn, customer, "queued", age_minutes=5, app_name="second-app")
        started = []
        tail_reached = threading.Event()
        release_tail = threading.Event()

        def pipeline(job, pipeline_conn, metrics):
            started.append(job["id"])
            worker_pg._set_status(pipeline_conn, job["id"], "deploying", attempt=job["attempt"])
            return {"live_url": f"https://{job['app_name']}.example.test", "health_check": "ok"}

        real_send = worker_pg.send_preview_ready

        def slow_customer_email(*args, **kwargs):
            if not tail_reached.is_set():
                tail_reached.set()
                release_tail.wait(20)
            return real_send(*args, **kwargs)

        monkeypatch.setattr(worker_pg, "_run_pipeline", pipeline)
        monkeypatch.setattr(worker_pg, "send_preview_ready", slow_customer_email)

        worker = threading.Thread(target=worker_pg.run_worker, daemon=True)
        worker.start()
        try:
            assert tail_reached.wait(15), "the first build never reached its tail"
            assert _row(conn, first)["status"] == "preview"
            # Many polls and recovery passes go by while the tail is held.
            time.sleep(2.5)
            waiting = _row(conn, second)
            assert (waiting["status"], waiting["retry_count"]) == ("queued", 0), waiting
            assert _dead_letters(conn, second) == []
            assert first in worker_pg._own_build_ids(), "a build is owned until its thread is done"
            release_tail.set()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and _row(conn, second)["status"] != "preview":
                time.sleep(0.05)
        finally:
            release_tail.set()
            worker_pg._shutdown_event.set()
            worker.join(20)

        assert not worker.is_alive()
        final = _row(conn, second)
        assert (final["status"], final["retry_count"]) == ("preview", 0), final
        assert started == [first, second], "each build ran exactly once"
        assert _emails(conn, "build_failed") == []
        assert _dead_letters(conn, second) == []
        assert worker_pg._own_build_ids() == []

    def test_a_claimed_build_is_owned_and_leased_before_it_is_submitted(self, conn, monkeypatch):
        """Ownership starts on the claiming thread. The submit is where the old
        code handed an unowned build to the pool."""
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 1)
        monkeypatch.setattr(worker_pg, "_ORPHAN_MIN_AGE_S", 0)
        build = _build(conn, _customer(conn), "queued")
        seen = {}

        class Pool:
            def submit(self, fn, job, lease_conn):
                seen["owned"] = job["id"] in worker_pg._own_build_ids()
                seen["leased"] = lease_conn is not None
                # A recovery pass at this very moment must leave the build alone.
                seen["recovered"] = worker_pg._recover_orphaned_builds(conn)
                seen["status"] = _row(conn, build)["status"]
                worker_pg._release_build_lease(lease_conn)
                worker_pg._disown_attempt(job["id"], job["attempt"])
                return threading.Event()  # stands in for a Future; never inspected here

        futures = []
        job = worker_pg._claim_and_dispatch(Pool(), futures)
        assert job["id"] == build
        assert seen == {"owned": True, "leased": True, "recovered": 0, "status": "building"}

    def test_nothing_is_claimed_when_every_thread_is_busy(self, conn, monkeypatch):
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 1)
        build = _build(conn, _customer(conn), "queued")

        class Busy:
            def done(self):
                return False

        class Pool:
            def submit(self, *args):
                raise AssertionError("a build was handed to a pool with no free thread")

        assert worker_pg._claim_and_dispatch(Pool(), [Busy()]) is None
        assert _row(conn, build)["status"] == "queued"

    def test_a_build_that_cannot_be_handed_to_the_pool_is_given_up(self, conn, monkeypatch):
        """submit() raises (the pool is shutting down). The claim stands, but
        this process must not keep saying it runs the build."""
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 1)
        build = _build(conn, _customer(conn), "queued")

        class Pool:
            def submit(self, *args):
                raise RuntimeError("cannot schedule new futures after shutdown")

        with pytest.raises(RuntimeError):
            worker_pg._claim_and_dispatch(Pool(), [])
        assert build not in worker_pg._own_build_ids()
        attempt = _token(conn, build)
        _wait_until_the_lease_is_gone(conn, build, attempt)
        lease = worker_pg._acquire_build_lease(build, attempt)
        assert lease is not None, "the lease must have been released"
        worker_pg._release_build_lease(lease)

    def test_a_reclaimed_build_stays_owned_when_the_earlier_attempts_tail_ends(self, conn, quiet_worker,
                                                                               monkeypatch):
        """Two build threads. Attempt 1 of a build fails with a transient error
        and is requeued; its thread is still in its tail (held here on
        purpose) when the free thread claims the build again. Ownership and
        the lease used to be keyed by build id: attempt 2 could not take the
        lease attempt 1 still held, and when attempt 1's tail ended it
        released the lease and disowned the BUILD. The next recovery pass saw
        a 'building' row that nobody leased or owned, requeued it under the
        running attempt, and a third attempt started beside the second."""
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 2)
        monkeypatch.setattr(worker_pg, "_MAX_RETRIES", 5)
        build = _build(conn, _customer(conn), "queued", app_name="twice-app")
        attempts = []
        first_tail_reached, release_first_tail = threading.Event(), threading.Event()
        second_running, release_second = threading.Event(), threading.Event()

        def pipeline(job, pipeline_conn, metrics):
            attempts.append(job["attempt"])
            if len(attempts) == 1:
                raise RuntimeError("connection reset by peer")   # transient: the build is requeued
            second_running.set()
            release_second.wait(30)
            worker_pg._set_status(pipeline_conn, job["id"], "deploying", attempt=job["attempt"])
            return {"live_url": "https://twice-app.example.test", "health_check": "ok"}

        real_discard = worker_pg._discard_unannounced_project

        def held_tail(discard_conn, job, why):
            if attempts and job["attempt"] == attempts[0] and not first_tail_reached.is_set():
                first_tail_reached.set()
                release_first_tail.wait(30)
            return real_discard(discard_conn, job, why)

        monkeypatch.setattr(worker_pg, "_run_pipeline", pipeline)
        monkeypatch.setattr(worker_pg, "_discard_unannounced_project", held_tail)

        worker = threading.Thread(target=worker_pg.run_worker, daemon=True)
        worker.start()
        try:
            assert first_tail_reached.wait(15), "attempt 1 never reached its tail"
            assert second_running.wait(15), "the build was not claimed again while attempt 1's tail ran"
            running = _row(conn, build)
            assert (running["status"], running["retry_count"]) == ("building", 1), running
            assert running["started_at"] == attempts[1]
            assert (build, attempts[1]) in worker_pg._own_attempts()

            release_first_tail.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (build, attempts[0]) in worker_pg._own_attempts():
                time.sleep(0.02)
            assert (build, attempts[0]) not in worker_pg._own_attempts(), "attempt 1's thread never finished"
            # Many recovery passes go by while attempt 2 is still in its pipeline.
            time.sleep(1.5)
            still_running = _row(conn, build)
            observed = (still_running["status"], still_running["retry_count"], still_running["started_at"])
            assert observed == ("building", 1, attempts[1]), f"attempt 2 was taken away mid-run: {still_running}"
            assert len(attempts) == 2, "a third attempt started beside the second"
            assert (build, attempts[1]) in worker_pg._own_attempts()
        finally:
            release_first_tail.set()
            release_second.set()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and _row(conn, build)["status"] != "preview":
                time.sleep(0.05)
            worker_pg._shutdown_event.set()
            worker.join(20)

        assert not worker.is_alive()
        final = _row(conn, build)
        assert (final["status"], final["retry_count"]) == ("preview", 1), final
        assert len(attempts) == 2
        assert worker_pg._own_attempts() == []

    def test_the_lease_of_an_earlier_attempt_says_nothing_about_a_later_one(self, conn, monkeypatch):
        """Seen from another worker process (nothing owned here): the row is on
        attempt 2. A lease on attempt 1, held by a thread still in that
        attempt's tail, neither blocks attempt 2's own lease nor protects the
        row; attempt 2's lease does."""
        monkeypatch.setattr(worker_pg, "_ORPHAN_MIN_AGE_S", 0)
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", Path("/nonexistent-builds-root-for-tests"))
        build = _build(conn, _customer(conn), "queued")
        first = worker_pg._claim_next_job(conn)["attempt"]
        tail_lease = worker_pg._acquire_build_lease(build, first)
        assert tail_lease is not None
        with conn.cursor() as cur:
            cur.execute("UPDATE builds SET status = 'queued', started_at = NULL WHERE id = %s", (build,))
        conn.commit()
        second = worker_pg._claim_next_job(conn)["attempt"]
        assert second != first
        try:
            running_lease = worker_pg._acquire_build_lease(build, second)
            assert running_lease is not None, "attempt 2 could not take a lease while attempt 1's was held"
            assert worker_pg._recover_orphaned_builds(conn) == 0
            assert _row(conn, build)["status"] == "building"
            _release_lease(conn, running_lease, build, second)
            # Only the old attempt's lease is left: the running attempt is unprotected.
            assert worker_pg._recover_orphaned_builds(conn) == 1
            assert _row(conn, build)["status"] == "queued"
        finally:
            worker_pg._release_build_lease(tail_lease)


# ---------------------------------------------------------------------------
# The janitor
# ---------------------------------------------------------------------------

def _child(conn, customer, parent, status, *, age_minutes, app_name="demo-app") -> str:
    build = _build(conn, customer, status, age_minutes=age_minutes, app_name=app_name)
    with conn.cursor() as cur:
        cur.execute("UPDATE builds SET parent_build_id = %s WHERE id = %s", (parent, build))
    conn.commit()
    return build


def _exists(conn, build_id) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM builds WHERE id = %s", (build_id,))
        found = cur.fetchone() is not None
    conn.commit()
    return found


def _parent(conn, build_id):
    with conn.cursor() as cur:
        cur.execute("SELECT parent_build_id FROM builds WHERE id = %s", (build_id,))
        value = cur.fetchone()[0]
    conn.commit()
    return str(value) if value else None


class TestJanitorKeepsWhatOtherRulesRead:
    def test_the_rebuild_chain_of_a_trial_survives_the_cleanup(self, conn):
        """A trial's build fails, is rebuilt, fails again, is rebuilt and lands
        at preview. The janitor runs when the third build is announced. It used
        to delete the first build, cut the second loose from it, and (through
        ON DELETE SET NULL) clear the trial's hand-off stamp: the three facts
        the web app could tell a Harbor build by."""
        customer = _customer(conn)
        root = _build(conn, customer, "failed", age_minutes=30)
        second = _child(conn, customer, root, "failed", age_minutes=20)
        third = _child(conn, customer, second, "preview", age_minutes=10)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trial_keepers (cookie_token, companion_name, companion_assigned, "
                "expires_at, handoff_build_id) VALUES (%s, 'Haven', 'Haven', NOW() + INTERVAL '7 days', %s) "
                "RETURNING trial_id", (uuid.uuid4().hex, root))
            trial = cur.fetchone()[0]
        conn.commit()

        deleted = worker_pg._cleanup_old_builds(conn, third, "demo-app")

        assert _exists(conn, root) and _exists(conn, second)
        assert _parent(conn, second) == root and _parent(conn, third) == second
        with conn.cursor() as cur:
            cur.execute("SELECT handoff_build_id FROM trial_keepers WHERE trial_id = %s", (trial,))
            assert str(cur.fetchone()[0]) == root
        conn.commit()
        assert deleted == 0

    def test_a_failed_ancestor_of_a_surviving_build_is_kept_with_its_links(self, conn):
        """No stamp involved: the chain alone protects its failed ancestors,
        and no parent_build_id on a surviving row is rewritten."""
        customer = _customer(conn)
        root = _build(conn, customer, "failed", age_minutes=40)
        middle = _child(conn, customer, root, "failed", age_minutes=30)
        stray = _build(conn, customer, "failed", age_minutes=25)
        tip = _child(conn, customer, middle, "preview", age_minutes=20)
        newest = _build(conn, customer, "queued", age_minutes=10)

        deleted = worker_pg._cleanup_old_builds(conn, newest, "demo-app")

        assert _exists(conn, root) and _exists(conn, middle) and _exists(conn, tip)
        assert _parent(conn, middle) == root and _parent(conn, tip) == middle
        assert not _exists(conn, stray), "a failed build nothing descends from is still cleaned up"
        assert deleted == 1

    def test_a_keepers_handoff_build_is_never_deleted(self, conn):
        customer = _customer(conn)
        stamped = _build(conn, customer, "failed", age_minutes=30)
        _build(conn, customer, "failed", age_minutes=20)
        newest = _build(conn, customer, "preview", age_minutes=10)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO keepers (customer_id, companion_name, companion_assigned, handoff_build_id) "
                "VALUES (%s, 'Haven', 'Haven', %s)", (customer, stamped))
        conn.commit()
        deleted = worker_pg._cleanup_old_builds(conn, newest, "demo-app")
        assert _exists(conn, stamped)
        with conn.cursor() as cur:
            cur.execute("SELECT handoff_build_id FROM keepers WHERE customer_id = %s", (customer,))
            assert str(cur.fetchone()[0]) == stamped
        conn.commit()
        assert deleted == 0

    def test_a_build_that_records_a_railway_project_is_kept_for_the_operator(self, conn, railway_token):
        """A failed row with a project on it is a deployment that was once
        announced. The janitor neither deletes the row (the only pointer to
        the project) nor the project."""
        customer = _customer(conn)
        announced = _build(conn, customer, "failed", age_minutes=30, railway_project_id="proj-once-announced")
        plain = _build(conn, customer, "failed", age_minutes=25)
        _build(conn, customer, "failed", age_minutes=20)
        newest = _build(conn, customer, "preview", age_minutes=10)
        railway = FakeRailway()
        with railway.installed():
            deleted = worker_pg._cleanup_old_builds(conn, newest, "demo-app")
        assert _exists(conn, announced), "the only pointer to an announced project was deleted"
        assert not _exists(conn, plain)
        assert railway.destroyed == []
        assert deleted == 1

    def test_old_failed_leaves_are_deleted_with_what_belongs_to_them(self, conn, tmp_path, monkeypatch):
        """What the janitor is for still works: a failed build with no
        children, no stamp and no project goes, with its messages, logs,
        feature requests and dead-letter row, and so does a failed parent whose
        only child goes with it. The start-event ledger is not touched."""
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", tmp_path)
        customer = _customer(conn)
        parent = _build(conn, customer, "failed", age_minutes=40)
        child = _child(conn, customer, parent, "failed", age_minutes=35)
        (tmp_path / f"demo-app-{child[:8]}").mkdir()
        _build(conn, customer, "failed", age_minutes=20)
        newest = _build(conn, customer, "preview", age_minutes=10)
        other_customer = _build(conn, _customer(conn), "failed", age_minutes=50)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO intake_messages (build_id, role, content) VALUES (%s, 'user', 'hi')", (child,))
            cur.execute("INSERT INTO build_logs (build_id, stage, message) VALUES (%s, 'testing', 'x')", (child,))
            cur.execute("INSERT INTO dead_letter_builds (build_id, error_category, error_message) "
                        "VALUES (%s, 'code_error', 'x')", (child,))
            cur.execute("INSERT INTO feature_requests (build_id, customer_id) VALUES (%s, %s) RETURNING id",
                        (child, customer))
            cur.execute("INSERT INTO build_start_events (customer_id, build_id, kind) VALUES (%s, %s, 'retry')",
                        (customer, child))
        conn.commit()

        deleted = worker_pg._cleanup_old_builds(conn, newest, "demo-app")

        assert not _exists(conn, parent) and not _exists(conn, child)
        assert _exists(conn, other_customer), "another customer's same-named build is not this app's"
        assert not (tmp_path / f"demo-app-{child[:8]}").exists()
        leftovers = {
            "intake_messages": "SELECT COUNT(*) FROM intake_messages WHERE build_id = %s",
            "build_logs": "SELECT COUNT(*) FROM build_logs WHERE build_id = %s",
            "dead_letter_builds": "SELECT COUNT(*) FROM dead_letter_builds WHERE build_id = %s",
            "feature_requests": "SELECT COUNT(*) FROM feature_requests WHERE build_id = %s",
        }
        with conn.cursor() as cur:
            for table, count_sql in leftovers.items():
                cur.execute(count_sql, (child,))
                assert cur.fetchone()[0] == 0, table
            cur.execute("SELECT COUNT(*) FROM build_start_events WHERE build_id = %s", (child,))
            assert cur.fetchone()[0] == 1, "the start ledger must outlive the build"
        conn.commit()
        assert deleted == 2

    def test_a_build_retried_during_the_cleanup_is_not_deleted(self, conn):
        """The web app's retry (failed to queued) is in flight on another
        connection when the janitor runs, and commits a moment later. The old
        DELETE named the build by id only: it waited for the retry's row lock
        and then deleted the build that had just been queued."""
        import psycopg2

        customer = _customer(conn)
        retried = _build(conn, customer, "failed", age_minutes=30)
        _build(conn, customer, "failed", age_minutes=20)
        newest = _build(conn, customer, "preview", age_minutes=10)

        web = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with web.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET status = 'queued', error_message = NULL, started_at = NULL, "
                    "completed_at = NULL, retry_count = 0 WHERE id = %s AND status = 'failed'",
                    (retried,))
                assert cur.rowcount == 1
            # Not committed yet: the row is locked by the retry.
            outcome = {}

            def janitor():
                own = psycopg2.connect(os.environ["DATABASE_URL"])
                try:
                    outcome["deleted"] = worker_pg._cleanup_old_builds(own, newest, "demo-app")
                finally:
                    own.close()

            thread = threading.Thread(target=janitor)
            thread.start()
            time.sleep(0.7)
            web.commit()
            thread.join(15)
            assert not thread.is_alive()
        finally:
            web.close()

        assert _exists(conn, retried), "the retried build was deleted from the queue"
        assert _row(conn, retried)["status"] == "queued"
        assert outcome["deleted"] == 0


# ---------------------------------------------------------------------------
# The stuck-thread path
# ---------------------------------------------------------------------------

class TestSupervisedPreviewIsAnnouncedLikeAnyOther:
    def test_monitor_registration_and_customer_email_follow_a_recovered_deploy(
            self, conn, railway_token, monkeypatch):
        """The build thread hangs past its deadline; the probe finds the
        deploy it finished. The old path moved the build to preview and told
        the reviewer, but never registered the site for health checks and
        backups and never told the customer."""
        monkeypatch.setattr(worker_pg, "_TIER_TIMEOUTS", {"build": 0})
        monkeypatch.setattr(worker_pg, "_SUPERVISOR_GRACE_SECONDS", 0.4)
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_age_s=30)
        release = threading.Event()
        thread_done = threading.Event()

        def stuck_pipeline(job, pipeline_conn, metrics):
            try:
                release.wait(20)  # blocked "inside deploy()": no project reported yet
                return {"live_url": "https://too-late.example.test"}
            finally:
                thread_done.set()

        probe = {"live_url": "https://zombie.example.test", "project_id": "proj-z",
                 "service_id": "svc-z"}
        job = {"id": build, "tier": "build", "app_name": "demo-app", "spec": {},
               "customer_id": customer, "customer_email": _customer_email(conn, customer),
               "customer_name": "Test", "retry_count": 0, "rebuild_request": None,
               "error_message": None, "attempt": _token(conn, build)}
        import deploy
        try:
            with patch.object(worker_pg, "_run_pipeline", side_effect=stuck_pipeline), \
                    patch.object(worker_pg, "_supervisor_reality_probe", return_value=probe), \
                    patch.object(deploy, "get_environment_id", return_value="env-z"), \
                    patch.object(deploy, "destroy_project") as destroy, \
                    patch.object(worker_pg, "sentry_sdk"):
                worker_pg._process_job(job, conn)
                release.set()
                assert thread_done.wait(10)
        finally:
            release.set()

        row = _row(conn, build)
        assert (row["status"], row["review_status"], row["preview_url"]) == (
            "preview", "pending", "https://zombie.example.test")
        assert _emails(conn, "review_pending") == [("review_pending", "admin@example.test", "skipped")]
        assert _emails(conn, "preview_ready") == [
            ("preview_ready", _customer_email(conn, customer), "skipped")], "the customer was not told"
        assert _monitored_projects(conn) == ["proj-z"], "no health checks or backups for the preview"
        assert _railway_columns(conn, build)[:3] == ("proj-z", "svc-z", "env-z")
        destroy.assert_not_called()

    def test_a_thread_nobody_waits_for_removes_the_project_it_created(self, conn, railway_token,
                                                                      builds_root, no_waiting,
                                                                      monkeypatch):
        """The supervisor gave up on the attempt and requeued the build. When
        the abandoned thread finally returns from deploy(), the project it
        created is on no row and nobody will ever look at what the thread
        returns: it deletes the project itself and stops."""
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        _store_site_artifact(conn, builds_root, build)
        job = worker_pg._claim_next_job(conn)
        railway = FakeRailway(project="proj-abandoned")

        def deploy_while_the_supervisor_gives_up(build_dir, custom_domain=None, *, password_login_probe=True):
            # What _process_job does on a supervisor timeout, then the free retry.
            worker_pg._attempt_state(job)["abandoned"] = True
            with conn.cursor() as cur:
                cur.execute("UPDATE builds SET status = 'queued', started_at = NULL WHERE id = %s", (build,))
            conn.commit()
            return FakeRailway.deploy(railway, build_dir)

        import psycopg2
        pipeline_conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with railway.installed():
                import deploy
                with patch.object(deploy, "deploy", deploy_while_the_supervisor_gives_up):
                    with pytest.raises(worker_pg.BuildNoLongerOwnedError):
                        worker_pg._run_pipeline(job, pipeline_conn, [])
        finally:
            pipeline_conn.close()

        assert railway.destroyed == ["proj-abandoned"]
        assert _row(conn, build)["status"] == "queued"
        assert _railway_columns(conn, build)[0] is None

# ---------------------------------------------------------------------------
# An attempt the supervisor gave up on: whose connection, whose project
# ---------------------------------------------------------------------------

class _StuckHealthRailway(FakeRailway):
    """Railway as seen by two attempts of one build. The first deployment's
    health probe blocks until released (the attempt's thread is then "stuck"
    in the post-deploy wait, the way a thread blocked in a socket read is);
    every later deployment is healthy."""

    def __init__(self, projects):
        super().__init__(health="infra", project=projects[0])
        self.projects = list(projects)
        self.reached = threading.Event()   # the first attempt's thread is inside its health probe
        self.release = threading.Event()   # let it return
        self._lock = threading.Lock()

    def deploy(self, build_dir, custom_domain=None, *, password_login_probe=True):
        with self._lock:
            index = self.deploys
            self.deploys += 1
        project = self.projects[min(index, len(self.projects) - 1)]
        return {
            "live_url": f"https://{project}.example.test",
            "railway_project_id": project,
            "railway_service_id": f"svc-{project}",
            "railway_environment_id": f"env-{project}",
            "database_url": None,
            "health_check": "infra" if index == 0 else "ok",
        }

    def verify_health(self, url, timeout=120):
        if self.projects[0] in url:
            self.reached.set()
            assert self.release.wait(60), "the test never released the stuck health probe"
            return "infra"
        return "ok"


def _supervisor_gives_up_when(reached):
    """Make the supervisor's wait for the pipeline end as soon as `reached` is
    set, without waiting for a real deadline. The pipeline future itself is
    the real one, still running."""
    real_executor = worker_pg.ThreadPoolExecutor

    class _Executor(real_executor):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)

            def exception(timeout=None):
                assert reached.wait(60), "the pipeline never reached the point where it blocks"
                raise TimeoutError("supervisor deadline")
            future.exception = exception
            return future

    return patch.object(worker_pg, "ThreadPoolExecutor", _Executor)


def _wait_until_the_build_threads_end(build_id, bound_s=60):
    """Wait (at most bound_s) until no pipeline thread of this build is alive."""
    prefix = f"build-{build_id[:8]}"
    deadline = time.monotonic() + bound_s
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith(prefix)]:
            return
        threading.Event().wait(0.05)
    raise AssertionError(f"a pipeline thread of build {build_id} is still running after {bound_s}s")


class TestAnAbandonedAttemptCleansUpThroughItsOwnConnection:
    """ARCHITECTURE.md, "Railway projects": an attempt deletes the project it
    created when it ends without reaching preview, also when its thread ends
    after the supervisor gave up on it.

    These tests drive the real wrapper (_run_build_in_thread, which closes the
    connection it opened as soon as _process_job returns) and the real
    pipeline thread it leaves behind, on a real database. Railway is faked at
    the functions of cli/deploy.py. The two sides of the seam are the
    wrapper's connection handling and the thread's late clean-up."""

    def test_the_late_cleanup_deletes_the_project_although_the_wrapper_closed_its_connection(
            self, conn, builds_root, railway_token, no_waiting, caplog):
        railway = _StuckHealthRailway(["proj-first"])
        with railway.installed():
            build = _build(conn, _customer(conn), "queued")
            _store_site_artifact(conn, builds_root, build)
            job = worker_pg._claim_next_job(conn)
            with _supervisor_gives_up_when(railway.reached), \
                    patch.object(worker_pg, "_supervisor_reality_probe", return_value=None), \
                    caplog.at_level(logging.INFO, logger="worker_pg"):
                worker_pg._run_build_in_thread(job)
                # The supervisor decided (a free retry) and the wrapper closed
                # the connection of the attempt; the attempt's thread is still
                # inside its health probe.
                assert _row(conn, build)["status"] == "queued"
                assert railway.destroyed == []
                railway.release.set()
                _wait_until_the_build_threads_end(build)

        text = " ".join(record.getMessage() for record in caplog.records)
        assert "NOT deleting it" not in text, text
        assert railway.destroyed == ["proj-first"]
        assert _railway_columns(conn, build)[0] is None, "an unannounced project is never on the row"

    def test_a_newer_attempt_that_reached_preview_keeps_its_project(
            self, conn, builds_root, railway_token, no_waiting):
        """The first attempt is abandoned and its build requeued; the second
        attempt of the same build deploys its own project and reaches preview.
        When the first attempt's thread ends, only its own project is deleted:
        the row keeps naming the second attempt's."""
        railway = _StuckHealthRailway(["proj-first", "proj-second"])
        with railway.installed():
            build = _build(conn, _customer(conn), "queued")
            _store_site_artifact(conn, builds_root, build)
            first = worker_pg._claim_next_job(conn)
            with _supervisor_gives_up_when(railway.reached), \
                    patch.object(worker_pg, "_supervisor_reality_probe", return_value=None):
                worker_pg._run_build_in_thread(first)
            assert _row(conn, build)["status"] == "queued"

            second = worker_pg._claim_next_job(conn)
            assert second is not None and second["attempt"] != first["attempt"]
            worker_pg._run_build_in_thread(second)
            assert _row(conn, build)["status"] == "preview"
            assert _railway_columns(conn, build)[0] == "proj-second"

            railway.release.set()
            _wait_until_the_build_threads_end(build)

        assert "proj-second" not in railway.destroyed, "the newer attempt's project was deleted"
        assert railway.destroyed == ["proj-first"], "the abandoned attempt's own project was not deleted"
        assert _row(conn, build)["status"] == "preview"
        assert _railway_columns(conn, build)[0] == "proj-second"
        assert _monitored_projects(conn) == ["proj-second"]

    def test_a_project_the_supervisor_announced_survives_a_newer_preview(
            self, conn, builds_root, railway_token, no_waiting):
        """The probe finds the deployment while the thread is still inside
        deploy() and announces it; the build is then redeployed in place and a
        newer attempt replaces the row's project. The first thread's late
        clean-up must still recognize the project it was announced with: it
        reached preview, so the worker never deletes it."""
        release_deploy = threading.Event()
        in_deploy = threading.Event()
        railway = FakeRailway(project="proj-announced-by-probe")

        def deploy_when_released(build_dir, custom_domain=None, *, password_login_probe=True):
            in_deploy.set()
            assert release_deploy.wait(60)
            return FakeRailway.deploy(railway, build_dir)

        probe = {"live_url": "https://proj-announced-by-probe.example.test",
                 "project_id": "proj-announced-by-probe", "service_id": "svc-proj-announced-by-probe"}
        import deploy
        with railway.installed(), patch.object(deploy, "deploy", deploy_when_released), \
                patch.object(deploy, "get_environment_id", return_value="env-probe"):
            build = _build(conn, _customer(conn), "queued")
            _store_site_artifact(conn, builds_root, build)
            job = worker_pg._claim_next_job(conn)
            with _supervisor_gives_up_when(in_deploy), \
                    patch.object(worker_pg, "_supervisor_reality_probe", return_value=probe):
                worker_pg._run_build_in_thread(job)
            assert _row(conn, build)["status"] == "preview"
            assert _railway_columns(conn, build)[0] == "proj-announced-by-probe"
            # A newer attempt of the build reached preview with its own project.
            with conn.cursor() as cur:
                cur.execute("UPDATE builds SET railway_project_id = 'proj-newer' WHERE id = %s", (build,))
            conn.commit()

            release_deploy.set()
            _wait_until_the_build_threads_end(build)

        assert railway.destroyed == [], "a project that was announced as a preview was deleted"
        assert _railway_columns(conn, build)[0] == "proj-newer"


class TestTheLastLookOfTheThreadAndTheDeadlineOfTheSupervisor:
    def test_a_supervisor_that_gives_up_after_the_thread_passed_its_last_look_does_the_cleanup_itself(
            self, conn, builds_root, railway_token, no_waiting):
        """The thread has noted its project and passed the point at which it
        looks at `abandoned` for the last time (thread_ended), but its future
        is not done yet: the supervisor gives up in that gap. Nobody would look
        at the project any more unless the supervisor does it, and it does."""
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        _store_site_artifact(conn, builds_root, build)
        job = worker_pg._claim_next_job(conn)
        reached, release = threading.Event(), threading.Event()
        railway = FakeRailway(project="proj-in-the-gap")

        def pipeline(pipeline_job, pipeline_conn, metrics):
            worker_pg._note_attempt_project(pipeline_job, railway.deploy(None))
            state = worker_pg._attempt_state(pipeline_job)
            with state["handoff"]:
                state["thread_ended"] = True     # what the end of _run_pipeline does
            reached.set()
            assert release.wait(60)

        try:
            with railway.installed(), patch.object(worker_pg, "_run_pipeline", pipeline), \
                    _supervisor_gives_up_when(reached), \
                    patch.object(worker_pg, "_supervisor_reality_probe", return_value=None):
                worker_pg._process_job(job, conn)
        finally:
            release.set()
        assert railway.destroyed == ["proj-in-the-gap"]
        assert worker_pg._attempt_state(job)["abandoned"] is False, "the thread had already ended its look"
        assert _row(conn, build)["status"] == "queued"


class TestThePreviewWriteAndTheLateCleanupAreSerialized:
    """Both sides take the attempt's lock: the supervisor's preview write for
    a probed project, and the thread's look-up and delete of the project it
    created."""

    def test_the_supervisor_does_not_announce_a_project_the_thread_is_deleting(
            self, conn, railway_token):
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_age_s=60)
        job = {"id": build, "tier": "build", "app_name": "demo-app", "spec": {},
               "customer_id": customer, "customer_email": _customer_email(conn, customer),
               "customer_name": "Test", "retry_count": 0, "rebuild_request": None,
               "error_message": None, "attempt": _token(conn, build)}
        worker_pg._note_attempt_project(job, {"railway_project_id": "proj-being-deleted"})
        state = worker_pg._attempt_state(job)
        delete_started = threading.Event()
        let_delete_finish = threading.Event()

        def slow_destroy(project_id, token):
            delete_started.set()
            assert let_delete_finish.wait(60)
            return True

        probe = {"live_url": "https://proj-being-deleted.example.test",
                 "project_id": "proj-being-deleted", "service_id": "svc-x"}
        import deploy
        import psycopg2
        thread_conn = psycopg2.connect(os.environ["DATABASE_URL"])
        with patch.object(deploy, "destroy_project", slow_destroy), \
                patch.object(worker_pg, "_supervisor_reality_probe", return_value=probe), \
                patch.object(worker_pg, "sentry_sdk"):
            cleaner = threading.Thread(
                target=worker_pg._discard_unannounced_project,
                args=(thread_conn, job, "its build thread finished after the supervisor gave up on it"))
            cleaner.start()
            try:
                assert delete_started.wait(30)
                threading.Timer(0.5, let_delete_finish.set).start()
                result = worker_pg._supervisor_force_fail(
                    build, "build", 60, attempt=job["attempt"],
                    attempt_railway=state["railway"], attempt_state=state)
            finally:
                let_delete_finish.set()
                cleaner.join(30)
                thread_conn.close()

        assert state["discarded"] is True
        row = _row(conn, build)
        assert row["status"] == "queued", "the build was announced as a preview of a deleted project"
        assert _railway_columns(conn, build)[0] is None
        assert result[1] == "retry", result

    def test_a_project_the_supervisor_announced_is_not_deleted_afterwards(self, conn, railway_token):
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_age_s=60)
        job = {"id": build, "tier": "build", "app_name": "demo-app", "spec": {},
               "customer_id": customer, "customer_email": _customer_email(conn, customer),
               "customer_name": "Test", "retry_count": 0, "rebuild_request": None,
               "error_message": None, "attempt": _token(conn, build)}
        worker_pg._note_attempt_project(job, {"railway_project_id": "proj-announced-first"})
        state = worker_pg._attempt_state(job)
        probe = {"live_url": "https://proj-announced-first.example.test",
                 "project_id": "proj-announced-first", "service_id": "svc-x"}
        import deploy
        with patch.object(worker_pg, "_supervisor_reality_probe", return_value=probe), \
                patch.object(deploy, "destroy_project") as destroy, patch.object(worker_pg, "sentry_sdk"):
            result = worker_pg._supervisor_force_fail(
                build, "build", 60, attempt=job["attempt"],
                attempt_railway=state["railway"], attempt_state=state)
            assert result[:2] == (True, "preview")
            # The row is replaced behind the attempt's back (a newer preview):
            # only the attempt's own record can still say the project was announced.
            with conn.cursor() as cur:
                cur.execute("UPDATE builds SET railway_project_id = 'proj-newer' WHERE id = %s", (build,))
            conn.commit()
            assert worker_pg._discard_unannounced_project(conn, job, "late") is False
        destroy.assert_not_called()


class TestALeftoverAttemptDoesNotClearTheNewerAttemptsFiles:
    """The failure handler of an attempt that lost the build (an admin killed
    it and requeued it, a newer attempt was claimed) must not remove the
    stored artifact or the working directory the newer attempt uses."""

    def test_a_code_error_of_the_old_attempt_leaves_artifact_and_directory_alone(
            self, conn, builds_root):
        build = _build(conn, _customer(conn), "queued")
        _store_site_artifact(conn, builds_root, build)
        first = worker_pg._claim_next_job(conn)
        site_dir = builds_root / f"demo-app-{build[:8]}"
        seen = {}

        def pipeline(job, pipeline_conn, metrics):
            with conn.cursor() as cur:   # the admin's kill
                cur.execute("UPDATE builds SET status = 'failed', error_message = 'Force-killed by admin', "
                            "completed_at = NOW() WHERE id = %s", (build,))
            conn.commit()
            _requeue_through_the_ops_api_statement(conn, build)
            seen["second"] = worker_pg._claim_next_job(conn)
            # The second attempt's working directory (what restoring the artifact leaves).
            site_dir.mkdir()
            (site_dir / "package.json").write_text('{"name": "demo-app"}')
            raise RuntimeError("SyntaxError in generated code")   # a code error: the handler clears for a fresh retry

        with patch.object(worker_pg, "_run_pipeline", pipeline), patch.object(worker_pg, "sentry_sdk"):
            worker_pg._process_job(first, conn)

        with conn.cursor() as cur:
            cur.execute("SELECT build_artifact IS NOT NULL, status FROM builds WHERE id = %s", (build,))
            has_artifact, status = cur.fetchone()
        conn.commit()
        assert status == "building", "the newer attempt still owns the build"
        assert has_artifact, "the old attempt's handler cleared the stored artifact of the newer attempt"
        assert (site_dir / "package.json").exists(), "the old attempt's handler removed the newer attempt's directory"
        assert seen["second"]["attempt"] != first["attempt"]


# ---------------------------------------------------------------------------
# Spend cap, attempt tokens, resume marker
# ---------------------------------------------------------------------------

class TestSpendOfARetryingBuildCounts:
    def test_a_build_requeued_today_keeps_counting_against_the_cap(self, conn, monkeypatch):
        """A build spent past the cap and was requeued for a retry. Every
        requeue clears started_at, and the old sum only saw rows with a
        started_at of today: the spend vanished and the queue reopened, for
        this very build first."""
        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 10.0)
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        job = worker_pg._claim_next_job(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE builds SET total_api_cost_usd = 12.5 WHERE id = %s", (build,))
        conn.commit()
        assert worker_pg._requeue_for_retry(conn, build, 0, "Connection reset by peer",
                                            attempt=job["attempt"]) is True
        assert _row(conn, build)["started_at"] is None

        assert worker_pg._get_daily_api_spend(conn) == 12.5
        assert worker_pg._claim_next_job(conn) is None, "the cap must hold the retry back"
        assert _row(conn, build)["status"] == "queued"

    def test_spend_recorded_on_the_pipeline_thread_reaches_the_column_the_cap_reads(
            self, conn, builds_root, monkeypatch):
        """The whole chain the daily cap depends on, with nothing of it faked.
        cli/api_client.py keeps each model call's cost in thread-local storage;
        the pipeline runs on a thread of its own; _run_pipeline hands the list
        over in _metrics_out; _process_job adds the sum to
        builds.total_api_cost_usd; _get_daily_api_spend sums that column and
        _claim_next_job compares it with the cap. Only the model call is stood
        in for: one cost record is filed on the pipeline thread, through the
        client's own collector, at the first log line of the real
        _run_pipeline."""
        from datetime import datetime

        import api_client

        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        waiting = _build(conn, customer, "queued", app_name="waiting-app")
        call = api_client.ApiCallMetrics(
            module="build", model="claude-opus-4-6", input_tokens=1000, output_tokens=500,
            cache_read_tokens=0, cache_write_tokens=0, api_mode="realtime",
            estimated_cost_usd=3.25, build_id=build, stage="build_phase_1", timestamp=datetime.now())
        real_append_log = worker_pg._append_log
        filed_on = []

        def append_log(log_conn, build_id, stage, message):
            if message.startswith("Validating app specification") and not filed_on:
                filed_on.append(threading.current_thread().name)
                api_client._record_metrics(call)
            return real_append_log(log_conn, build_id, stage, message)

        monkeypatch.setattr(worker_pg, "_append_log", append_log)
        job = worker_pg._claim_next_job(conn)
        assert job["id"] == build
        with patch.object(worker_pg, "sentry_sdk"):
            worker_pg._process_job(job, conn)

        assert filed_on and filed_on[0] != threading.current_thread().name, \
            "the cost was meant to be filed on the pipeline's own thread"
        with conn.cursor() as cur:
            cur.execute("SELECT total_api_cost_usd FROM builds WHERE id = %s", (build,))
            recorded = cur.fetchone()[0]
        conn.commit()
        assert float(recorded) == 3.25
        assert '"cost_usd": 3.25' in _logged(conn, build), "the api_costs log row"
        assert worker_pg._get_daily_api_spend(conn) == 3.25

        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 3.0)
        assert worker_pg._claim_next_job(conn) is None, "today's recorded spend is over the cap"
        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 5.0)
        claimed = worker_pg._claim_next_job(conn)
        assert claimed is not None and claimed["id"] in (build, waiting)

    def test_a_build_requeued_on_an_earlier_day_does_not_count_today(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "queued", cost=40)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE builds DISABLE TRIGGER update_builds_updated_at")
            cur.execute("UPDATE builds SET updated_at = NOW() - INTERVAL '2 days' WHERE id = %s", (build,))
            cur.execute("ALTER TABLE builds ENABLE TRIGGER update_builds_updated_at")
        conn.commit()
        assert worker_pg._get_daily_api_spend(conn) == 0.0


class TestTheAttemptTokenIsNotOptional:
    def test_every_guarded_write_refuses_a_missing_token(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_today=True)
        before = _row(conn, build)
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._set_status(conn, build, "deploying", attempt=None)
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._advance_stage(conn, build, "deploying", None)
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._set_preview_status(conn, build, preview_url="https://x.example.test", attempt=None)
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._requeue_for_retry(conn, build, 0, "boom", attempt=None)
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._fail_build(conn, build, "boom", "", "", "demo-app", "", attempt=None)
        with pytest.raises(ValueError, match="attempt token missing"):
            worker_pg._supervisor_force_fail(build, "build", 60, attempt=None)
        assert _row(conn, build) == before
        assert _dead_letters(conn, build) == [] and _emails(conn) == []

    def test_a_job_without_a_token_is_refused_before_anything_runs(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "building", started_today=True)
        job = {"id": build, "tier": "build", "app_name": "demo-app", "spec": {},
               "customer_id": customer, "customer_email": "x@example.test", "customer_name": "T",
               "retry_count": 0}
        with patch.object(worker_pg, "_run_pipeline") as pipeline:
            with pytest.raises(ValueError, match="attempt token missing"):
                worker_pg._process_job(job, conn)
        pipeline.assert_not_called()
        assert _row(conn, build)["status"] == "building"

    def test_the_marker_for_a_row_without_started_at_matches_only_such_a_row(self, conn):
        customer = _customer(conn)
        never_started = _build(conn, customer, "building")
        started = _build(conn, customer, "building", started_today=True)
        marker = worker_pg.ROW_WITHOUT_STARTED_AT
        assert worker_pg._set_status(conn, never_started, "testing", attempt=marker) is True
        assert worker_pg._set_status(conn, started, "testing", attempt=marker) is False
        assert _row(conn, started)["status"] == "building"
        assert worker_pg._fail_build(conn, started, "x", "", "", "", "", attempt=marker) is False
        assert _row(conn, started)["status"] == "building"

    def test_orphan_recovery_handles_a_row_that_was_never_claimed(self, conn, tmp_path, monkeypatch):
        """A pipeline status without a started_at (something other than the
        claim wrote it): recovery still fails it when it has no retry left,
        with the explicit marker as its token."""
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", tmp_path)
        customer = _customer(conn)
        build = _build(conn, customer, "building", retry_count=worker_pg._MAX_RETRIES)
        assert _row(conn, build)["started_at"] is None
        assert worker_pg._recover_orphaned_builds(conn) == 0
        assert _row(conn, build)["status"] == "failed"
        assert len(_dead_letters(conn, build)) == 1


class TestResumeMarkerContract:
    def _stages_in_the_ops_api(self) -> list:
        import re
        source = OPS_API_TS.read_text(encoding="utf-8")
        stages = re.search(r"VALID_STAGES = \[([^\]]+)\]", source)
        assert stages, "VALID_STAGES not found in ops-api.ts"
        return re.findall(r"'([a-z]+)'", stages.group(1))

    def test_the_marker_the_ops_api_writes_is_the_one_the_worker_parses(self):
        """The contract, read from the other side: the template the route
        writes and the stages it accepts."""
        source = OPS_API_TS.read_text(encoding="utf-8")
        assert "`Requeued: resuming from ${stage} stage`" in source, (
            "the ops API no longer writes the stage-resume marker in the agreed form")
        stages = self._stages_in_the_ops_api()
        assert sorted(stages) == sorted(worker_pg._RESUMABLE_STAGES)
        for stage in stages:
            assert worker_pg._parse_resume_marker(f"Requeued: resuming from {stage} stage") == stage
            assert worker_pg._parse_resume_marker(worker_pg._resume_marker(stage)) == stage

    @pytest.mark.parametrize("look_alike", [
        "Retry 1/6: test 'resuming from testing' failed",
        "Requeued: resuming from testing stage ",
        " Requeued: resuming from testing stage",
        "requeued: resuming from testing stage",
        "Requeued: resuming from testing stage\n",
        "Requeued: resuming from building stage",
        "Requeued: resuming from testing",
        "Supervisor timeout; resuming from deploying stage",
        "Recovered: resuming from testing stage; then failed",
        "", None, 7,
    ])
    def test_look_alikes_are_not_markers(self, look_alike):
        assert worker_pg._parse_resume_marker(look_alike) is None

    def test_a_retry_note_that_quotes_the_words_does_not_skip_the_build_stage(self, conn, builds_root):
        """A failed attempt left files on disk, and its error text happens to
        contain the words. The old substring match resumed from 'testing' on
        those files; without a marker and without a stored artifact the build
        must start over."""
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        job = worker_pg._claim_next_job(conn)
        site = builds_root / f"demo-app-{build[:8]}"
        site.mkdir()
        (site / "package.json").write_text("{}")
        assert worker_pg._requeue_for_retry(
            conn, build, 0, "smoke test 'resuming from testing' returned 500",
            attempt=job["attempt"]) is True
        retry = worker_pg._claim_next_job(conn)
        assert "resuming from testing" in retry["error_message"]
        assert worker_pg._resume_stage(conn, retry, f"demo-app-{build[:8]}") is None


class TestJanitorScope:
    """Whose rows the janitor may look at, on the real schema."""

    def _ids(self, conn, app):
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM builds WHERE app_name = %s", (app,))
            rows = {str(r[0]) for r in cur.fetchall()}
        conn.commit()
        return rows

    def test_customers_sharing_an_app_name_never_delete_each_others_builds(self, conn):
        a, b = _customer(conn), _customer(conn)
        # Customer A: a live site, a reviewed preview and an old failure, all
        # older than everything of customer B's.
        a_live = _build(conn, a, "live", age_minutes=900, app_name="bakery")
        a_prev = _build(conn, a, "preview", age_minutes=800, app_name="bakery")
        a_fail = _build(conn, a, "failed", age_minutes=700, app_name="bakery")
        # Customer B: an in-flight build, a live one and a stack of failures.
        b_building = _build(conn, b, "building", age_minutes=600, app_name="bakery")
        b_live = _build(conn, b, "live", age_minutes=500, app_name="bakery")
        b_old_1 = _build(conn, b, "failed", age_minutes=400, app_name="bakery")
        b_old_2 = _build(conn, b, "abandoned", age_minutes=300, app_name="bakery")
        b_keep_1 = _build(conn, b, "failed", age_minutes=20, app_name="bakery")
        b_newest = _build(conn, b, "failed", age_minutes=10, app_name="bakery")

        deleted = worker_pg._cleanup_old_builds(conn, b_newest, "bakery")

        remaining = self._ids(conn, "bakery")
        # Nothing of customer A's is touched, whatever its age or status.
        assert {a_live, a_prev, a_fail} <= remaining
        # Customer B's live and in-flight builds survive; so do B's two newest rows.
        assert {b_building, b_live, b_keep_1, b_newest} <= remaining
        # Only B's own old failed and abandoned builds were removed.
        assert b_old_1 not in remaining and b_old_2 not in remaining
        assert deleted == 2

    def test_live_in_flight_and_queued_builds_are_never_deleted(self, conn):
        a = _customer(conn)
        protected = [
            _build(conn, a, status, age_minutes=1000 - i, app_name="site")
            for i, status in enumerate(("live", "preview", "queued", "building", "testing", "deploying"))
        ]
        newest = [_build(conn, a, "failed", age_minutes=10, app_name="site"),
                  _build(conn, a, "failed", age_minutes=5, app_name="site")]

        assert worker_pg._cleanup_old_builds(conn, newest[-1], "site") == 0
        assert set(protected) | set(newest) <= self._ids(conn, "site")


# ---------------------------------------------------------------------------
# Cooperative cancellation, end to end
# ---------------------------------------------------------------------------

class TestTheSupervisorStopsARunningFixLoop:
    def test_a_fix_loop_stuck_in_a_model_call_ends_when_the_supervisor_gives_up(
            self, conn, builds_root, monkeypatch):
        """The whole chain, with nothing of it mocked: _process_job registers
        the attempt's cancel event and hands the key to the pipeline; the v1
        pipeline passes it to cli/fix.py's attempt_fix; attempt_fix passes it
        to the model client; the model client is inside a (simulated) hung
        call when the supervisor's deadline passes and it fires the event.

        Before, the supervisor fired an event that no consumer could find:
        the consumers looked for their key in spec["build_id"], which the
        worker never set. The hung call ran its full length.

        Faked here: the test stage's app run (it needs npm), and the model
        call itself. api_client replaces the call with a cancellable wait when
        its test hook is switched on, and the client object handed to it is a
        stand-in that has no transport: whatever happens to the wait, no
        request can be sent, not even by a thread that outlives this test.
        """
        import types
        from unittest.mock import MagicMock

        import api_client
        import fix as fix_module

        monkeypatch.setenv("CAS_BUILD_ENGINE", "v1")
        monkeypatch.setenv("CAS_ALLOW_TEST_HOOKS", "1")
        monkeypatch.setenv("ANTHROPIC_SIMULATE_HANG_SECONDS", "12")
        offline_client = MagicMock(name="model client without a transport")
        monkeypatch.setattr(fix_module, "_create_client", lambda: offline_client)
        monkeypatch.setattr(worker_pg, "_TIER_TIMEOUTS", {"build": 0})
        monkeypatch.setattr(worker_pg, "_SUPERVISOR_GRACE_SECONDS", 1.0)

        # The v1 test stage: the app's smoke test fails, which starts the fix loop.
        failing = {"success": False, "failed": 1, "results": [("GET /", False, "500")],
                   "app_stderr": "", "screenshots": []}
        monkeypatch.setitem(sys.modules, "test", types.SimpleNamespace(test_app=lambda _dir: failing))

        fix_calls = []
        real_attempt_fix = fix_module.attempt_fix

        loop_ended = threading.Event()

        def watched_attempt_fix(*args, **kwargs):
            call = {"started": time.monotonic(), "cancel_key": kwargs.get("cancel_key")}
            fix_calls.append(call)
            try:
                return real_attempt_fix(*args, **kwargs)
            except BaseException as exc:
                call["raised"] = exc
                raise
            finally:
                call["ended"] = time.monotonic()
                loop_ended.set()

        monkeypatch.setattr(fix_module, "attempt_fix", watched_attempt_fix)

        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        _store_site_artifact(conn, builds_root, build)
        job = worker_pg._claim_next_job(conn)

        import psycopg2
        pipeline_conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with patch.object(worker_pg, "_supervisor_reality_probe", return_value=None), \
                    patch.object(worker_pg, "sentry_sdk"):
                supervisor_started = time.monotonic()
                worker_pg._process_job(job, pipeline_conn)
                supervisor_returned = time.monotonic()
                assert loop_ended.wait(6), (
                    "the fix loop was still inside the hung model call six seconds after the "
                    "supervisor gave up on the build")
        finally:
            loop_ended.wait(20)
            pipeline_conn.close()

        assert len(fix_calls) == 1
        call = fix_calls[0]
        assert call["cancel_key"] == job["cancel_key"] and call["cancel_key"].startswith(build + "#")
        assert isinstance(call.get("raised"), api_client.BuildCancelled), call.get("raised")
        # It ended because it was cancelled, not because the hang ran out.
        assert call["ended"] - call["started"] < 8
        assert supervisor_returned - supervisor_started < 8
        # The supervisor's verdict stands: first hang, one free retry.
        row = _row(conn, build)
        assert (row["status"], row["retry_count"], row["supervisor_retry_used"]) == ("queued", 1, True)
        # Nothing was written by the cancelled fix, and the event is gone with its thread.
        site = builds_root / f"demo-app-{build[:8]}"
        assert (site / "server.js").read_text() == "// site\n"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and job["cancel_key"] in worker_pg.cli_cancellation._registry:
            time.sleep(0.05)
        assert job["cancel_key"] not in worker_pg.cli_cancellation._registry
