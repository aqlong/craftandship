"""Worker queue guards, exercised against a real PostgreSQL.

The rest of tests/test_worker_pg.py mocks the cursor and inspects SQL text,
which cannot show that a guard actually keeps a row from changing. These tests
load the real schema (web/src/db/schema.sql) into a throwaway database and run
the worker's own functions against it:

  * the worker never writes `live`; stage writes only move a build forward; a
    late write from a stale thread cannot move a build that has since gone
    live, failed, been previewed or been requeued;
  * every arrival at preview puts the build back into review and clears the
    previous verdict, a duplicate call changes nothing, and nothing is
    announced when the preview write matched no row;
  * every terminal failure (pipeline error, supervisor timeout, orphan
    recovery, thread crash) leaves a dead_letter_builds row and a "build
    failed" email, and a build that left the worker's hands is not failed;
  * orphan recovery skips a build whose advisory lease is held, and uses the
    same retry counter and cap as every other requeue;
  * the stage-resume marker written by orphan recovery reaches the pipeline
    through the claim;
  * the supervisor holds no row lock while it probes Railway;
  * a queued build is claimed once, oldest first, never past
    MAX_CONCURRENT_BUILDS and never while the daily spend cap is reached;
  * the spend query fails closed when it cannot run.

They need a reachable local PostgreSQL (see the scratch_pg_url fixture in
tests/conftest.py) and skip otherwise.
"""

import sys
import os
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

SCHEMA_SQL = ROOT / "web" / "src" / "db" / "schema.sql"


@pytest.fixture(scope="module")
def worker_db_url(scratch_pg_url):
    """A second throwaway database holding the production schema."""
    import psycopg2
    from psycopg2 import extensions

    params = extensions.parse_dsn(scratch_pg_url)
    name = "cas_test_worker_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(scratch_pg_url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    url = extensions.make_dsn(**{**params, "dbname": name})
    try:
        loader = psycopg2.connect(url)
        loader.autocommit = True
        with loader.cursor() as cur:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        loader.close()
        yield url
    finally:
        with admin.cursor() as cur:
            try:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            except psycopg2.Error:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


@pytest.fixture()
def conn(worker_db_url, monkeypatch):
    """Connection to the throwaway database, with the worker pointed at it.

    DATABASE_URL is set for the test so that the worker's own connections
    (leases, the supervisor's fresh connection, the mailer's email_log writes)
    land in the same throwaway database. Mail is never sent: RESEND_API_KEY is
    removed, so the mailer records each attempt in email_log as 'skipped'.
    """
    import psycopg2

    connection = psycopg2.connect(worker_db_url)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE builds, customers, email_log, monitor_apps CASCADE")
    connection.commit()
    monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 5)
    monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 0.0)
    monkeypatch.delenv("CAS_BUILD_ENGINE", raising=False)
    monkeypatch.delenv("CAS_HARBOR_AUTO_REVIEW", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", worker_db_url)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    yield connection
    connection.rollback()
    connection.close()
    # Process-wide records the worker keeps between jobs (see worker_pg):
    # one test's builds must not show up in the next test's.
    with worker_pg._UNRECORDED_ATTEMPT_PROJECT_IDS_LOCK:
        worker_pg._UNRECORDED_ATTEMPT_PROJECT_IDS.clear()
    with worker_pg._RETRY_SERIES_LOCK:
        worker_pg._RETRY_SERIES_STARTED.clear()


def _customer(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (email, password, name) VALUES (%s, 'x', 'Test') RETURNING id",
            (f"c-{uuid.uuid4().hex[:8]}@example.test",),
        )
        cid = str(cur.fetchone()[0])
    conn.commit()
    return cid


def _customer_email(conn, customer_id) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM customers WHERE id = %s", (customer_id,))
        return cur.fetchone()[0]


def _build(conn, customer_id, status, *, tier="build", age_minutes=0, review_status=None,
           cost=None, started_today=False, started_age_s=None, retry_count=0,
           supervisor_retry_used=False, reviewed=False, railway_project_id=None,
           error_message=None, app_name="demo-app") -> str:
    """Insert one build row. started_age_s (seconds ago) wins over started_today;
    reviewed=True fills the reviewer columns as an earlier review would have."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO builds (customer_id, tier, status, app_name, spec, created_at, "
            "review_status, total_api_cost_usd, started_at, retry_count, supervisor_retry_used, "
            "reviewed_at, reviewed_by, review_notes, railway_project_id, error_message) "
            "VALUES (%s, %s, %s, %s, '{}'::jsonb, NOW() - make_interval(mins => %s), %s, %s, "
            "CASE WHEN %s::int IS NOT NULL THEN NOW() - make_interval(secs => %s::int) "
            "     WHEN %s THEN NOW() ELSE NULL END, "
            "%s, %s, "
            "CASE WHEN %s THEN NOW() - INTERVAL '1 day' ELSE NULL END, "
            "CASE WHEN %s THEN 'reviewer@example.test' ELSE NULL END, "
            "CASE WHEN %s THEN 'notes about the previous deployment' ELSE NULL END, "
            "%s, %s) RETURNING id",
            (customer_id, tier, status, app_name, age_minutes, review_status, cost,
             started_age_s, started_age_s, started_today,
             retry_count, supervisor_retry_used,
             reviewed, reviewed, reviewed, railway_project_id, error_message),
        )
        bid = str(cur.fetchone()[0])
    conn.commit()
    return bid


_ROW_KEYS = ("status", "review_status", "preview_url", "live_url", "error_message", "started_at",
             "completed_at", "build_engine", "retry_count", "supervisor_retry_used",
             "reviewed_at", "reviewed_by", "review_notes", "railway_project_id")


def _row(conn, build_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_ROW_KEYS)} FROM builds WHERE id = %s", (build_id,))
        row = dict(zip(_ROW_KEYS, cur.fetchone()))
    conn.commit()
    return row


def _token(conn, build_id):
    """The attempt token of the row as it is now: its started_at, or the
    explicit marker for a row that has none (which is how most rows in these
    tests are inserted). Every guarded status write needs one."""
    started = _row(conn, build_id)["started_at"]
    return started if started is not None else worker_pg.ROW_WITHOUT_STARTED_AT


def _dead_letters(conn, build_id) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT error_category, error_message, original_retry_count FROM dead_letter_builds "
            "WHERE build_id = %s ORDER BY moved_at", (build_id,))
        rows = cur.fetchall()
    conn.commit()
    return rows


def _emails(conn, template=None) -> list:
    """(template, to_email, status) of every email the mailer attempted."""
    with conn.cursor() as cur:
        cur.execute("SELECT template, to_email, status FROM email_log ORDER BY created_at, template")
        rows = cur.fetchall()
    conn.commit()
    return [r for r in rows if template is None or r[0] == template]


def _job(conn, build_id, customer_id, **extra) -> dict:
    """A job dict as _claim_next_job would return it for this build, including
    the attempt token (the row's current started_at)."""
    job = {
        "id": build_id, "tier": "build", "app_name": "demo-app", "spec": {},
        "customer_id": customer_id, "customer_email": _customer_email(conn, customer_id),
        "customer_name": "Test", "retry_count": 0, "rebuild_request": None, "error_message": None,
        "attempt": _token(conn, build_id),
    }
    job.update(extra)
    return job


def _requeue_and_claim_again(conn, build_id) -> dict:
    """What happens to a build between two attempts: something requeues it
    (here the statement the web app's retry route uses) and the worker claims
    it again. Returns the new attempt's job."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE builds SET status = 'queued', error_message = NULL, started_at = NULL, "
            "completed_at = NULL, retry_count = 0 WHERE id = %s", (build_id,))
    conn.commit()
    job = worker_pg._claim_next_job(conn)
    assert job is not None and job["id"] == build_id
    return job


# The same expression that decides whether an attempt's lease is held
# (worker_pg._ORPHAN_SELECT_SQL and _LEASE_TAKE_SQL).
_LEASE_HELD_SQL = (
    "SELECT count(*) FROM pg_locks l "
    "WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 2 AND l.classid = %s "
    "AND l.objid = (hashtext(%s || ':' || to_char(%s::timestamp, 'YYYYMMDDHH24MISSUS')))::oid "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


def _wait_until_the_lease_is_gone(conn, build_id, attempt, bound_s=10.0) -> None:
    """Wait (at most bound_s seconds) until no connection holds the lease of this attempt.

    Closing the connection that holds an advisory lock does not release the
    lock in the same instant: the server process is told and ends afterwards,
    and under load the lock was still in pg_locks for up to 165 ms after
    close() returned. A test that closes a lease and then asks what recovery
    does reads that window unless it waits for the lock to leave first. Raises
    AssertionError at the bound, so a lease that is never released fails the
    test instead of hanging it."""
    deadline = time.monotonic() + bound_s
    while True:
        with conn.cursor() as cur:
            cur.execute(_LEASE_HELD_SQL, (worker_pg._LEASE_CLASS_ID, str(build_id), worker_pg._attempt_param(attempt)))
            held = cur.fetchone()[0]
        conn.rollback()
        if not held:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"the lease of build {build_id} was still held {bound_s}s after its connection was closed")
        threading.Event().wait(0.01)


def _release_lease(conn, lease, build_id, attempt) -> None:
    """worker_pg._release_build_lease, then wait until the lock has really left pg_locks."""
    worker_pg._release_build_lease(lease)
    _wait_until_the_lease_is_gone(conn, build_id, attempt)


ACTIVE = ("building", "testing", "deploying")
NOT_WORKER_OWNED = ("intake", "payment_pending", "queued", "preview", "live", "failed", "abandoned")


class TestSetStatusNeverTouchesFinishedBuilds:
    def test_live_cannot_be_written_by_the_worker(self, conn):
        build = _build(conn, _customer(conn), "deploying")
        with pytest.raises(ValueError):
            worker_pg._set_status(conn, build, "live", attempt=_token(conn, build))
        assert _row(conn, build)["status"] == "deploying"
        assert _row(conn, build)["live_url"] is None

    def test_failed_is_not_a_stage_write(self, conn):
        """'failed' has one writer, _fail_build (dead-letter row and email)."""
        build = _build(conn, _customer(conn), "deploying")
        with pytest.raises(ValueError):
            worker_pg._set_status(conn, build, "failed", attempt=_token(conn, build))
        assert _row(conn, build)["status"] == "deploying"

    @pytest.mark.parametrize("current", NOT_WORKER_OWNED)
    @pytest.mark.parametrize("target", ACTIVE)
    def test_a_stale_progress_write_changes_nothing(self, conn, current, target):
        build = _build(conn, _customer(conn), current)
        before = _row(conn, build)
        assert worker_pg._set_status(conn, build, target, attempt=_token(conn, build)) is False
        assert _row(conn, build) == before

    @pytest.mark.parametrize("current, target", [
        ("building", "building"), ("building", "testing"), ("building", "deploying"),
        ("testing", "testing"), ("testing", "deploying"), ("deploying", "deploying"),
    ])
    def test_progress_write_moves_an_active_build_forward(self, conn, current, target):
        build = _build(conn, _customer(conn), current)
        assert worker_pg._set_status(conn, build, target, attempt=_token(conn, build)) is True
        assert _row(conn, build)["status"] == target

    @pytest.mark.parametrize("current, target", [
        ("testing", "building"), ("deploying", "building"), ("deploying", "testing"),
    ])
    def test_a_build_is_never_moved_back_to_an_earlier_stage(self, conn, current, target):
        """A thread left over from an earlier point in the pipeline writes its
        stage after the build has moved on: the write is refused, and
        _advance_stage tells that thread to stop."""
        build = _build(conn, _customer(conn), current)
        assert worker_pg._set_status(conn, build, target, attempt=_token(conn, build)) is False
        assert _row(conn, build)["status"] == current
        with pytest.raises(worker_pg.BuildNoLongerOwnedError):
            worker_pg._advance_stage(conn, build, target, _token(conn, build))
        assert _row(conn, build)["status"] == current


class TestFailBuild:
    """_fail_build, the one terminal-failure path, with the real mailer."""

    @pytest.mark.parametrize("current", ACTIVE)
    def test_an_active_build_fails_with_a_dead_letter_row_and_an_email(self, conn, current):
        customer = _customer(conn)
        build = _build(conn, customer, current, retry_count=3)
        failed = worker_pg._fail_build(
            conn, build, "boom", _customer_email(conn, customer), "Test", "demo-app",
            "admin@example.test", retry_count=3, attempt=_token(conn, build))
        assert failed is True
        row = _row(conn, build)
        assert (row["status"], row["error_message"]) == ("failed", "boom")
        assert row["completed_at"] is not None
        assert _dead_letters(conn, build) == [("code_error", "boom", 3)]
        assert _emails(conn) == [("build_failed", _customer_email(conn, customer), "skipped")]

    @pytest.mark.parametrize("current", NOT_WORKER_OWNED)
    def test_a_build_that_left_the_workers_hands_is_not_failed(self, conn, current):
        """Includes 'queued': a build waiting for its next attempt must not be
        failed by a late write from the previous attempt."""
        customer = _customer(conn)
        build = _build(conn, customer, current, railway_project_id="proj-keep")
        before = _row(conn, build)
        with patch("deploy.destroy_project") as destroy:
            failed = worker_pg._fail_build(
                conn, build, "late", _customer_email(conn, customer), "Test", "demo-app",
                "admin@example.test", attempt=_token(conn, build))
        assert _row(conn, build) == before
        assert _dead_letters(conn, build) == []
        assert _emails(conn) == []
        destroy.assert_not_called()
        assert failed is False

    def test_a_terminal_failure_leaves_the_project_on_the_row_alone(self, conn, monkeypatch):
        """The project a build row records is the deployment the customer was
        last told about (after an in-place requeue of a previewed build: the
        previous, working site). _fail_build used to read it from the row and
        delete it. What a failing attempt created itself is removed by its
        caller, from the attempt's own state; see
        tests/test_worker_lifecycle_realdb.py."""
        monkeypatch.setenv("RAILWAY_API_TOKEN", "not-a-token-" + uuid.uuid4().hex)
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", railway_project_id="proj-on-the-row")
        with patch("deploy.destroy_project") as destroy:
            assert worker_pg._fail_build(
                conn, build, "boom", "", "", "demo-app", "", attempt=_token(conn, build)) is True
        destroy.assert_not_called()
        assert _row(conn, build)["railway_project_id"] == "proj-on-the-row"

    def test_secrets_in_the_error_are_redacted_before_they_are_stored(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "deploying")
        dsn = "post" + "gresql://app:" + "pw-value" + "@db.internal:5432/app"
        worker_pg._fail_build(conn, build, f"connect failed {dsn}", "", "", "demo-app", "", attempt=_token(conn, build))
        stored = _row(conn, build)["error_message"]
        assert "pw-value" not in stored and "***" in stored
        assert "pw-value" not in _dead_letters(conn, build)[0][1]


class TestStaleAttemptCannotWriteToALaterAttempt:
    """A thread left over from an earlier attempt of a build (one the
    supervisor gave up on, or one whose build was killed and retried) still
    holds its old attempt token. Once a newer attempt has claimed the build,
    every status write of the old thread must miss the row, and the newer
    attempt's writes must land undisturbed."""

    def _two_attempts(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "queued")
        first = worker_pg._claim_next_job(conn)
        assert first["id"] == build
        second = _requeue_and_claim_again(conn, build)
        return build, customer, first, second

    def test_each_claim_gets_its_own_attempt_token(self, conn):
        build, _customer_id, first, second = self._two_attempts(conn)
        assert first["attempt"] is not None and second["attempt"] is not None
        assert first["attempt"] != second["attempt"]
        assert _row(conn, build)["started_at"] == second["attempt"]

    def test_a_stale_stage_write_cannot_advance_the_current_attempt(self, conn):
        """Without the attempt guard this forward write (building to
        deploying) matched, and the current attempt's own next stage write
        then looked like a move backwards."""
        build, _customer_id, first, second = self._two_attempts(conn)
        assert worker_pg._set_status(conn, build, "deploying", attempt=first["attempt"]) is False
        assert _row(conn, build)["status"] == "building"
        with pytest.raises(worker_pg.BuildNoLongerOwnedError):
            worker_pg._advance_stage(conn, build, "testing", first["attempt"])
        assert _row(conn, build)["status"] == "building"
        # The current attempt moves on as if nothing had happened.
        worker_pg._advance_stage(conn, build, "testing", second["attempt"])
        worker_pg._advance_stage(conn, build, "deploying", second["attempt"])
        assert _row(conn, build)["status"] == "deploying"

    def test_stale_preview_requeue_and_failure_all_miss(self, conn):
        build, customer, first, second = self._two_attempts(conn)
        stale = first["attempt"]
        before = _row(conn, build)
        assert worker_pg._set_preview_status(conn, build, preview_url="https://old.example.test",
                                             attempt=stale) is False
        assert worker_pg._requeue_for_retry(conn, build, 0, "boom", attempt=stale) is False
        assert worker_pg._fail_build(conn, build, "boom", _customer_email(conn, customer), "Test",
                                     "demo-app", "", attempt=stale) is False
        assert _row(conn, build) == before
        assert _dead_letters(conn, build) == []
        assert _emails(conn) == []
        # The same writes land for the attempt that owns the build.
        assert worker_pg._set_preview_status(conn, build, preview_url="https://new.example.test",
                                             attempt=second["attempt"]) is True
        assert _row(conn, build)["preview_url"] == "https://new.example.test"

    def test_a_stale_jobs_failure_handling_leaves_the_current_attempt_alone(self, conn):
        """_process_job for the old attempt ends in an error. Its retry and
        failure writes carry the old token, so the build stays with the
        current attempt: not requeued, not failed, nobody emailed."""
        build, _customer_id, first, _second = self._two_attempts(conn)
        before = _row(conn, build)
        with patch.object(worker_pg, "_run_pipeline", side_effect=RuntimeError("Connection refused by host")), \
             patch.object(worker_pg, "sentry_sdk"):
            worker_pg._process_job(first, conn)
        after = _row(conn, build)
        assert (after["status"], after["retry_count"], after["started_at"]) == (
            before["status"], before["retry_count"], before["started_at"])
        assert _dead_letters(conn, build) == []
        assert _emails(conn) == []

    def test_the_supervisor_of_a_stale_attempt_does_nothing(self, conn):
        build, _customer_id, first, _second = self._two_attempts(conn)
        before = _row(conn, build)
        with patch.object(worker_pg, "_supervisor_reality_probe") as probe:
            result = worker_pg._supervisor_force_fail(build, "build", 60, attempt=first["attempt"])
        assert result == (False, "failed", None)
        probe.assert_not_called()
        assert _row(conn, build) == before


class TestPreviewTransition:
    def test_first_arrival_puts_the_build_into_review(self, conn):
        build = _build(conn, _customer(conn), "deploying")
        assert worker_pg._set_preview_status(conn, build, preview_url="https://p.example.test", attempt=_token(conn, build)) is True
        row = _row(conn, build)
        assert (row["status"], row["review_status"], row["preview_url"]) == (
            "preview", "pending", "https://p.example.test")
        assert row["completed_at"] is not None

    def test_a_duplicate_call_for_the_same_deployment_changes_nothing(self, conn):
        """The dedup case: the build is already in preview. A second call (a
        zombie thread, a supervisor that lost the race) must not signal a
        second notification and must not reset what happened since: here an
        admin approved the preview between the two calls."""
        build = _build(conn, _customer(conn), "deploying")
        assert worker_pg._set_preview_status(conn, build, preview_url="https://p.example.test", attempt=_token(conn, build)) is True
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE builds SET review_status = 'approved', reviewed_by = 'reviewer@example.test', "
                "reviewed_at = NOW() WHERE id = %s", (build,))
        conn.commit()
        before = _row(conn, build)
        assert worker_pg._set_preview_status(conn, build, preview_url="https://other.example.test", attempt=_token(conn, build)) is False
        assert _row(conn, build) == before
        assert before["review_status"] == "approved"

    @pytest.mark.parametrize("old_verdict", ("approved", "changes_requested", "pending", "ai_review_held"))
    @pytest.mark.parametrize("stage", ACTIVE)
    def test_a_rebuild_goes_back_into_review_and_drops_the_old_verdict(self, conn, old_verdict, stage):
        """A verdict belongs to the deployment that was reviewed. The row still
        carries the verdict of the previous cycle (the web app does not clear
        it when it requeues a build); the new deployment must not inherit it."""
        build = _build(conn, _customer(conn), stage, review_status=old_verdict, reviewed=True)
        assert worker_pg._set_preview_status(conn, build, preview_url="https://new.example.test", attempt=_token(conn, build)) is True
        row = _row(conn, build)
        assert (row["status"], row["review_status"], row["preview_url"]) == (
            "preview", "pending", "https://new.example.test")
        assert (row["reviewed_at"], row["reviewed_by"], row["review_notes"]) == (None, None, None)

    def test_auto_review_queues_the_rebuild_instead(self, conn, monkeypatch):
        monkeypatch.setenv("CAS_HARBOR_AUTO_REVIEW", "true")
        build = _build(conn, _customer(conn), "deploying", review_status="approved", reviewed=True)
        assert worker_pg._set_preview_status(conn, build, preview_url="https://new.example.test", attempt=_token(conn, build)) is True
        assert _row(conn, build)["review_status"] == "ai_review_queued"

    @pytest.mark.parametrize("finished", ("live", "failed", "abandoned", "queued", "preview"))
    def test_a_stale_thread_cannot_pull_a_build_back_to_preview(self, conn, finished):
        build = _build(conn, _customer(conn), finished, review_status="approved", reviewed=True)
        before = _row(conn, build)
        assert worker_pg._set_preview_status(conn, build, preview_url="https://p.example.test", attempt=_token(conn, build)) is False
        assert _row(conn, build) == before


class TestProcessJobTail:
    """_process_job from the end of the pipeline on, against the real row and
    the real mailer. Only _run_pipeline (the build and the deploy) is faked."""

    def _run(self, conn, build, customer, pipeline):
        with patch.object(worker_pg, "_run_pipeline", side_effect=pipeline):
            worker_pg._process_job(_job(conn, build, customer), conn)

    def test_a_rebuilt_approved_build_is_reviewed_again_and_the_admin_is_told_again(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "building", review_status="approved", reviewed=True)

        def pipeline(job, pipeline_conn, metrics):
            worker_pg._set_status(pipeline_conn, job["id"], "deploying", attempt=job["attempt"])
            return {"live_url": "https://rebuilt.example.test"}

        self._run(conn, build, customer, pipeline)
        row = _row(conn, build)
        assert (row["status"], row["review_status"], row["reviewed_by"]) == ("preview", "pending", None)
        assert row["preview_url"] == "https://rebuilt.example.test"
        assert _emails(conn, "review_pending") == [("review_pending", "admin@example.test", "skipped")]
        assert _emails(conn, "preview_ready") == [
            ("preview_ready", _customer_email(conn, customer), "skipped")]

    def test_no_review_email_under_auto_review(self, conn, monkeypatch):
        monkeypatch.setenv("CAS_HARBOR_AUTO_REVIEW", "true")
        customer = _customer(conn)
        build = _build(conn, customer, "deploying")
        self._run(conn, build, customer, lambda *a: {"live_url": "https://x.example.test"})
        assert _row(conn, build)["review_status"] == "ai_review_queued"
        assert _emails(conn, "review_pending") == []

    def test_a_build_killed_while_deploying_is_not_announced(self, conn):
        """The admin kill route sets the row to 'failed' while the worker's
        thread is still inside the deploy. When the pipeline returns, the
        preview write matches no row: the row stays as the admin left it and
        nobody is told a preview is ready."""
        customer = _customer(conn)
        build = _build(conn, customer, "deploying")

        def pipeline(job, pipeline_conn, metrics):
            with pipeline_conn.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET status = 'failed', error_message = 'Force-killed by admin', "
                    "completed_at = NOW() WHERE id = %s", (job["id"],))
            pipeline_conn.commit()
            return {"live_url": "https://killed.example.test", "magic_link_raw_token": "ab" * 32,
                    "railway_project_id": "proj-k", "railway_service_id": "svc-k",
                    "railway_environment_id": "env-k"}

        self._run(conn, build, customer, pipeline)
        row = _row(conn, build)
        assert (row["status"], row["error_message"]) == ("failed", "Force-killed by admin")
        assert row["preview_url"] is None and row["review_status"] is None
        assert _emails(conn) == []
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM build_logs WHERE build_id = %s", (build,))
            logged = " | ".join(r[0] for r in cur.fetchall())
            cur.execute("SELECT COUNT(*) FROM monitor_apps")
            monitored = cur.fetchone()[0]
        conn.commit()
        assert "ready for review" not in logged and "Magic link login URL" not in logged
        assert monitored == 0

    def test_a_pipeline_stops_at_the_next_stage_write_after_a_kill(self, conn):
        """Killed before the deploy stage: _advance_stage raises, the job is
        dropped, and the kill is not overwritten by a retry or a failure."""
        customer = _customer(conn)
        build = _build(conn, customer, "testing")
        reached_deploy = []

        def pipeline(job, pipeline_conn, metrics):
            with pipeline_conn.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET status = 'failed', error_message = 'Force-killed by admin' "
                    "WHERE id = %s", (job["id"],))
            pipeline_conn.commit()
            worker_pg._advance_stage(pipeline_conn, job["id"], "deploying", job["attempt"])
            reached_deploy.append(True)
            return {"live_url": "https://never.example.test"}

        self._run(conn, build, customer, pipeline)
        assert reached_deploy == []
        row = _row(conn, build)
        assert (row["status"], row["error_message"], row["retry_count"]) == (
            "failed", "Force-killed by admin", 0)
        assert _dead_letters(conn, build) == []
        assert _emails(conn) == []


class TestClaimNextJob:
    def test_a_queued_build_is_claimed_once(self, conn):
        build = _build(conn, _customer(conn), "queued")
        job = worker_pg._claim_next_job(conn)
        assert job["id"] == build
        row = _row(conn, build)
        assert row["status"] == "building" and row["started_at"] is not None
        assert row["build_engine"] == "v2"
        assert worker_pg._claim_next_job(conn) is None

    def test_only_queued_builds_are_claimable(self, conn):
        customer = _customer(conn)
        for status in ("intake", "payment_pending", "preview", "live", "failed", "abandoned"):
            _build(conn, customer, status)
        assert worker_pg._claim_next_job(conn) is None

    def test_the_concurrency_limit_blocks_a_claim(self, conn, monkeypatch):
        monkeypatch.setattr(worker_pg, "_MAX_CONCURRENT", 1)
        customer = _customer(conn)
        _build(conn, customer, "testing")
        queued = _build(conn, customer, "queued")
        assert worker_pg._claim_next_job(conn) is None
        assert _row(conn, queued)["status"] == "queued"

    def test_the_daily_spend_cap_blocks_a_claim(self, conn, monkeypatch):
        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 10.0)
        customer = _customer(conn)
        _build(conn, customer, "preview", cost=12.5, started_today=True)
        queued = _build(conn, customer, "queued")
        assert worker_pg._claim_next_job(conn) is None
        assert _row(conn, queued)["status"] == "queued"

    def test_spend_below_the_cap_still_allows_a_claim(self, conn, monkeypatch):
        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 10.0)
        customer = _customer(conn)
        _build(conn, customer, "preview", cost=3.0, started_today=True)
        queued = _build(conn, customer, "queued")
        assert worker_pg._claim_next_job(conn)["id"] == queued

    def test_spend_from_other_days_is_not_counted(self, conn, monkeypatch):
        monkeypatch.setattr(worker_pg, "_DAILY_SPEND_CAP", 10.0)
        customer = _customer(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO builds (customer_id, tier, status, total_api_cost_usd, started_at) "
                "VALUES (%s, 'build', 'preview', 99, NOW() - INTERVAL '2 days')", (customer,))
        conn.commit()
        assert worker_pg._get_daily_api_spend(conn) == 0.0

    def test_the_oldest_queued_build_is_claimed_first_whatever_its_tier(self, conn):
        """The queue is first in, first out. builds.tier no longer buys a
        place in it (tier pricing and its priority were removed)."""
        customer = _customer(conn)
        oldest = _build(conn, customer, "queued", tier="build", age_minutes=300)
        scale_old = _build(conn, customer, "queued", tier="scale", age_minutes=60)
        grow = _build(conn, customer, "queued", tier="grow", age_minutes=10)
        scale_new = _build(conn, customer, "queued", tier="scale", age_minutes=1)
        claimed = [worker_pg._claim_next_job(conn)["id"] for _ in range(4)]
        assert claimed == [oldest, scale_old, grow, scale_new]

    def test_engine_is_stamped_from_the_environment(self, conn, monkeypatch):
        monkeypatch.setenv("CAS_BUILD_ENGINE", "v1")
        build = _build(conn, _customer(conn), "queued")
        worker_pg._claim_next_job(conn)
        assert _row(conn, build)["build_engine"] == "v1"
        monkeypatch.setenv("CAS_BUILD_ENGINE", "bogus")
        second = _build(conn, _customer(conn), "queued")
        worker_pg._claim_next_job(conn)
        assert _row(conn, second)["build_engine"] == "v2"

    def test_the_claim_returns_the_rows_error_message(self, conn):
        build = _build(conn, _customer(conn), "queued", error_message="Requeued: resuming from deploying stage")
        job = worker_pg._claim_next_job(conn)
        assert job["id"] == build
        assert job["error_message"] == "Requeued: resuming from deploying stage"


class TestStageResumeMarker:
    """The marker orphan recovery writes must reach the pipeline. It travels
    in builds.error_message, so the claim has to select that column."""

    @pytest.fixture()
    def builds_root(self, tmp_path, monkeypatch):
        root = tmp_path / "builds"
        root.mkdir()
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", root)
        return root

    @pytest.mark.parametrize("stage", ("testing", "deploying"))
    def test_recovery_marker_survives_the_claim_and_selects_the_stage(self, conn, builds_root, stage):
        build = _build(conn, _customer(conn), stage, started_age_s=300, retry_count=2)
        build_dir = builds_root / f"demo-app-{build[:8]}"
        build_dir.mkdir()
        (build_dir / "package.json").write_text("{}")
        (build_dir / "server.js").write_text("")

        assert worker_pg._recover_orphaned_builds(conn) == 1
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", 2)
        assert row["error_message"] == f"Recovered: resuming from {stage} stage"

        job = worker_pg._claim_next_job(conn)
        assert job["id"] == build
        assert job["error_message"] == f"Recovered: resuming from {stage} stage"
        assert worker_pg._resume_stage(conn, job, f"demo-app-{build[:8]}") == stage

    def test_a_build_requeued_without_a_marker_runs_in_full(self, conn, builds_root):
        build = _build(conn, _customer(conn), "building", started_age_s=300)
        assert worker_pg._recover_orphaned_builds(conn) == 1
        job = worker_pg._claim_next_job(conn)
        assert job["error_message"] == "Recovered: worker restarted during build"
        assert worker_pg._resume_stage(conn, job, f"demo-app-{build[:8]}") is None


class TestOrphanRecovery:
    """_recover_orphaned_builds with real advisory-lock leases."""

    @pytest.fixture(autouse=True)
    def _no_files_on_disk(self, tmp_path, monkeypatch):
        root = tmp_path / "builds"
        root.mkdir()
        monkeypatch.setattr(worker_pg, "_BUILDS_ROOT", root)
        self.builds_root = root

    def test_a_leased_build_is_left_alone_and_an_unleased_one_is_requeued(self, conn):
        customer = _customer(conn)
        leased = [_build(conn, customer, status, started_age_s=300) for status in ACTIVE]
        orphaned = [_build(conn, customer, status, started_age_s=300) for status in ACTIVE]
        leases = [worker_pg._acquire_build_lease(build, _token(conn, build)) for build in leased]
        try:
            assert all(lease is not None for lease in leases)
            # A second lease on a held attempt is refused.
            assert worker_pg._acquire_build_lease(leased[0], _token(conn, leased[0])) is None
            assert worker_pg._recover_orphaned_builds(conn) == len(orphaned)
            for build in leased:
                row = _row(conn, build)
                assert row["status"] in ACTIVE and row["retry_count"] == 0
            for build in orphaned:
                row = _row(conn, build)
                assert (row["status"], row["retry_count"]) == ("queued", 1)
                assert row["started_at"] is None
            # Once the owner is gone (its connection closes), the build is an orphan.
            _release_lease(conn, leases[0], leased[0], _token(conn, leased[0]))
            assert worker_pg._recover_orphaned_builds(conn) == 1
            assert _row(conn, leased[0])["status"] == "queued"
        finally:
            for lease in leases:
                worker_pg._release_build_lease(lease)

    def test_a_build_that_just_started_is_not_an_orphan_yet(self, conn):
        build = _build(conn, _customer(conn), "building", started_age_s=5)
        assert worker_pg._recover_orphaned_builds(conn) == 0
        assert _row(conn, build)["status"] == "building"

    def test_a_build_this_process_is_running_is_never_recovered(self, conn):
        build = _build(conn, _customer(conn), "building", started_age_s=300)
        worker_pg._own_attempt(build, _token(conn, build))
        try:
            assert worker_pg._recover_orphaned_builds(conn) == 0
        finally:
            worker_pg._disown_attempt(build, _token(conn, build))
        assert _row(conn, build)["status"] == "building"

    def test_one_interruption_does_not_fail_a_build_that_has_a_retry_left(self, conn):
        """Five ordinary retries, then one worker restart. The build has one
        retry left under the cap every other requeue uses, so it gets it; one
        interruption is not "interrupted too many times"."""
        build = _build(conn, _customer(conn), "deploying", started_age_s=300,
                       retry_count=worker_pg._MAX_RETRIES - 1)
        assert worker_pg._recover_orphaned_builds(conn) == 1
        row = _row(conn, build)
        assert (row["status"], row["retry_count"]) == ("queued", worker_pg._MAX_RETRIES)
        assert _dead_letters(conn, build) == []

    def test_no_retries_left_is_a_terminal_failure_with_dead_letter_row_and_email(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_age_s=300,
                       retry_count=worker_pg._MAX_RETRIES)
        assert worker_pg._recover_orphaned_builds(conn) == 0
        row = _row(conn, build)
        assert row["status"] == "failed"
        assert len(_dead_letters(conn, build)) == 1
        assert _emails(conn) == [("build_failed", _customer_email(conn, customer), "skipped")]
        expected = (f"Build interrupted by a worker restart with no retries left "
                    f"({worker_pg._MAX_RETRIES} of {worker_pg._MAX_RETRIES} used)")
        assert row["error_message"] == expected
        assert _dead_letters(conn, build) == [("infra", expected, worker_pg._MAX_RETRIES)]

    def test_a_build_that_used_its_supervised_retry_fails_with_dead_letter_row_and_email(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "testing", started_age_s=300, retry_count=1,
                       supervisor_retry_used=True)
        assert worker_pg._recover_orphaned_builds(conn) == 0
        assert _row(conn, build)["status"] == "failed"
        letters = _dead_letters(conn, build)
        assert len(letters) == 1 and letters[0][0] == "infra"
        assert "prior supervisor timeout failure" in letters[0][1]
        assert _emails(conn) == [("build_failed", _customer_email(conn, customer), "skipped")]

    def test_the_resume_cap_fails_with_dead_letter_row_and_email(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "testing", started_age_s=300)
        build_dir = self.builds_root / f"demo-app-{build[:8]}"
        build_dir.mkdir()
        (build_dir / "package.json").write_text("{}")
        (build_dir / "server.js").write_text("")
        for attempt in range(worker_pg._MAX_ARTIFACT_RESUMES):
            assert worker_pg._recover_orphaned_builds(conn) == 1, attempt
            row = _row(conn, build)
            assert (row["status"], row["retry_count"]) == ("queued", 0)
            # The next worker claims it and dies again during the same stage.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE builds SET status = 'testing', started_at = NOW() - INTERVAL '5 minutes' "
                    "WHERE id = %s", (build,))
            conn.commit()
        assert worker_pg._recover_orphaned_builds(conn) == 0
        assert _row(conn, build)["status"] == "failed"
        letters = _dead_letters(conn, build)
        assert len(letters) == 1 and "infinite resume loop" in letters[0][1]
        assert _emails(conn) == [("build_failed", _customer_email(conn, customer), "skipped")]

    def test_a_build_without_a_customer_row_is_still_recovered(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO builds (tier, status, app_name, started_at) "
                "VALUES ('build', 'building', 'demo-app', NOW() - INTERVAL '5 minutes') RETURNING id")
            build = str(cur.fetchone()[0])
        conn.commit()
        assert worker_pg._recover_orphaned_builds(conn) == 1
        assert _row(conn, build)["status"] == "queued"


class TestSupervisorAgainstPostgres:
    """_supervisor_force_fail on its own fresh connection; only the Railway
    probe is faked."""

    def _force_fail(self, build, probe_result=None, probe=None, **extra):
        """Run the supervisor for the attempt the row is on right now."""
        import os
        import psycopg2
        lookup = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            attempt = _token(lookup, build)
        finally:
            lookup.close()
        target = probe if probe is not None else (lambda **_kwargs: probe_result)
        with patch.object(worker_pg, "_supervisor_reality_probe", side_effect=target):
            return worker_pg._supervisor_force_fail(
                build, "build", 60,
                customer_email="owner@example.test", customer_name="Owner",
                admin_email="admin@example.test", attempt=attempt, **extra,
            )

    def test_first_hang_is_requeued_once(self, conn):
        build = _build(conn, _customer(conn), "testing", started_age_s=3700)
        assert self._force_fail(build) == (True, "retry", None)
        row = _row(conn, build)
        assert (row["status"], row["retry_count"], row["supervisor_retry_used"]) == ("queued", 1, True)
        assert _dead_letters(conn, build) == []
        assert _emails(conn) == []

    def test_second_hang_fails_with_dead_letter_row_and_email_and_keeps_the_project(self, conn):
        build = _build(conn, _customer(conn), "deploying", started_age_s=3700, retry_count=1,
                       supervisor_retry_used=True, railway_project_id="proj-stuck")
        with patch("deploy.destroy_project") as destroy:
            assert self._force_fail(build) == (True, "failed", None)
        row = _row(conn, build)
        assert row["status"] == "failed" and "Supervisor timeout" in row["error_message"]
        letters = _dead_letters(conn, build)
        assert len(letters) == 1 and letters[0][0] == "timeout" and letters[0][2] == 1
        assert _emails(conn) == [("build_failed", "owner@example.test", "skipped")]
        destroy.assert_not_called()

    def test_a_deploy_the_stuck_thread_finished_goes_to_preview_and_into_review(self, conn):
        """The probe finds a healthy deployment. The build is a rebuild that
        still carries the previous cycle's approval: the new deployment goes
        into review like any other arrival at preview."""
        build = _build(conn, _customer(conn), "deploying", started_age_s=3700,
                       review_status="approved", reviewed=True)
        probe_result = {"live_url": "https://zombie.example.test", "project_id": "proj-z",
                        "service_id": "svc-z"}
        assert self._force_fail(build, probe_result=probe_result) == (True, "preview", probe_result)
        row = _row(conn, build)
        assert (row["status"], row["review_status"], row["preview_url"]) == (
            "preview", "pending", "https://zombie.example.test")
        assert (row["reviewed_by"], row["live_url"]) == (None, None)
        assert row["railway_project_id"] == "proj-z"
        assert _dead_letters(conn, build) == []

    @pytest.mark.parametrize("current", NOT_WORKER_OWNED)
    def test_a_build_outside_the_worker_owned_statuses_is_left_alone(self, conn, current):
        build = _build(conn, _customer(conn), current)
        before = _row(conn, build)
        probed = []
        result = self._force_fail(build, probe=lambda **_kwargs: probed.append(True))
        assert result == (False, "failed", None)
        assert probed == []
        assert _row(conn, build) == before
        assert _dead_letters(conn, build) == []

    @pytest.mark.parametrize("retry_used", (False, True))
    def test_an_admin_kill_during_the_probe_is_not_blocked_and_not_overwritten(self, conn, retry_used):
        """While Railway is being probed (up to 20 seconds) the supervisor
        holds no row lock: another connection can update the build at once.
        Here the admin kill route's UPDATE runs during the probe, with a
        2 second lock timeout that would fail if the row were locked. The
        supervisor then finds the build out of the worker-owned statuses and
        writes nothing."""
        import psycopg2

        build = _build(conn, _customer(conn), "deploying", started_age_s=3700,
                       supervisor_retry_used=retry_used)

        def kill_during_probe(**_kwargs):
            other = psycopg2.connect(os.environ["DATABASE_URL"])
            try:
                with other.cursor() as cur:
                    cur.execute("SET lock_timeout = '2s'")
                    cur.execute(
                        "UPDATE builds SET status = 'failed', error_message = 'Force-killed by admin', "
                        "completed_at = NOW() WHERE id = %s "
                        "AND status IN ('building', 'testing', 'deploying', 'queued')", (build,))
                    assert cur.rowcount == 1
                other.commit()
            finally:
                other.close()
            return None

        assert self._force_fail(build, probe=kill_during_probe) == (False, "failed", None)
        row = _row(conn, build)
        assert (row["status"], row["error_message"]) == ("failed", "Force-killed by admin")
        assert row["supervisor_retry_used"] is retry_used
        assert _dead_letters(conn, build) == []
        assert _emails(conn) == []


class TestThreadCrash:
    def test_a_crashed_build_thread_fails_the_build_through_the_terminal_path(self, conn):
        customer = _customer(conn)
        build = _build(conn, customer, "deploying", started_today=True, retry_count=2)
        job = _job(conn, build, customer, retry_count=2)
        with patch.object(worker_pg, "_process_job", side_effect=SystemExit(1)):
            worker_pg._run_build_in_thread(job)
        row = _row(conn, build)
        assert row["status"] == "failed"
        assert row["error_message"] == "Worker thread crash (SystemExit): 1"
        assert _dead_letters(conn, build) == [("code_error", "Worker thread crash (SystemExit): 1", 2)]
        assert _emails(conn) == [("build_failed", _customer_email(conn, customer), "skipped")]
        assert build not in worker_pg._own_build_ids()


class TestReviewAndUiTestResultsAreStored:
    """_store_design_review_results and _store_ui_test_results against the
    real columns (tests/test_worker_integration.py checks the statements)."""

    def _stored(self, conn, build_id) -> tuple:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT design_review_status, design_review_results, ui_test_score, ui_test_results "
                "FROM builds WHERE id = %s", (build_id,))
            row = cur.fetchone()
        conn.commit()
        return row

    def test_a_new_build_has_no_review_yet(self, conn):
        build = _build(conn, _customer(conn), "building", started_today=True)
        assert self._stored(conn, build) == ("pending", [], None, [])

    @pytest.mark.parametrize("result, status", [
        ({"critical_count": 2, "issues": [{"issue": "low contrast", "severity": "critical"}]}, "warned"),
        ({"critical_count": 0, "issues": []}, "passed"),
        ({"skipped": True}, "skipped"),
    ])
    def test_the_design_review_lands_on_the_build_row(self, conn, result, status):
        customer = _customer(conn)
        build = _build(conn, customer, "building", started_today=True)
        other = _build(conn, customer, "building", started_today=True, app_name="other-app")

        worker_pg._store_design_review_results(conn, build, result)

        stored = self._stored(conn, build)
        assert stored[0] == status
        assert stored[1] == result.get("issues", [])
        assert self._stored(conn, other)[0] == "pending", "only the build that was reviewed is written"

    def test_the_ui_test_score_and_result_land_on_the_build_row(self, conn):
        build = _build(conn, _customer(conn), "testing", started_today=True)
        result = {"score": 8.5, "checks": [{"name": "text_contrast", "passed": True}]}

        worker_pg._store_ui_test_results(conn, build, result)

        stored = self._stored(conn, build)
        assert float(stored[2]) == 8.5
        assert stored[3] == result

    def test_a_score_the_column_cannot_hold_is_dropped_and_the_connection_stays_usable(self, conn):
        """ui_test_score is NUMERIC(3,1). The store swallows the error and
        rolls back, so the build's next statement on this connection works."""
        build = _build(conn, _customer(conn), "testing", started_today=True)

        worker_pg._store_ui_test_results(conn, build, {"score": 1000})

        assert self._stored(conn, build)[2] is None
        assert worker_pg._set_status(conn, build, "deploying", attempt=_token(conn, build))
        assert _row(conn, build)["status"] == "deploying"


def test_spend_lookup_fails_closed_when_the_query_cannot_run(scratch_pg_url):
    """Against a database with no builds table the query errors; the answer
    must be infinity (pause new builds), never zero."""
    import psycopg2

    connection = psycopg2.connect(scratch_pg_url)
    try:
        assert worker_pg._get_daily_api_spend(connection) == float("inf")
    finally:
        connection.close()
