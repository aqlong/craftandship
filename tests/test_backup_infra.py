"""Tests for monitor/backup.py: restore(), prune_backups(), run_daily_backups(),
the per-app backup directory, and failure reporting.

The Railway API is always mocked. Most tests also mock subprocess and the
filesystem; TestSnapshotAndRestoreAgainstRealPostgres runs the real pg_dump
and psql against a throwaway local database and skips when they, or a local
PostgreSQL, are not available.
"""

import gzip
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests._fake_pg_tools import SAMPLE_DUMP, pg_run, write_dump

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import monitor.backup as _backup_module_for_reference

# The autouse fixture below replaces _record_backup_audit; one test needs the
# real function.
_REAL_RECORD_BACKUP_AUDIT = _backup_module_for_reference._record_backup_audit


@pytest.fixture(autouse=True)
def _no_audit_writes():
    """run_daily_backups records an ops_actions row when DATABASE_URL is set;
    these tests must never write one, whatever the developer's shell exports."""
    with patch("monitor.backup._record_backup_audit"):
        yield


# ---------------------------------------------------------------------------
# prune_backups()
# ---------------------------------------------------------------------------

class TestPruneBackups:
    def test_deletes_beyond_keep_limit(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)

        app_dir = tmp_path / "test-app"
        app_dir.mkdir()

        files = []
        for i in range(10):
            f = write_dump(app_dir / f"2024010{i}T000000Z.sql.gz")
            os.utime(str(f), (1000 + i * 100, 1000 + i * 100))
            files.append(f)

        deleted = backup_module.prune_backups("test-app", keep=3)

        assert len(deleted) == 7
        remaining = list(app_dir.glob("*.sql.gz"))
        assert len(remaining) == 3

    def test_keep_all_when_under_limit(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)

        app_dir = tmp_path / "test-app"
        app_dir.mkdir()

        for i in range(3):
            write_dump(app_dir / f"2024010{i}T000000Z.sql.gz")

        deleted = backup_module.prune_backups("test-app", keep=5)
        assert len(deleted) == 0
        assert len(list(app_dir.glob("*.sql.gz"))) == 3

    def test_empty_dir_returns_empty(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)

        deleted = backup_module.prune_backups("nonexistent-app", keep=7)
        assert deleted == []

    def test_keep_zero_still_keeps_the_newest_complete_dump(self, tmp_path, monkeypatch):
        """This test used to pin keep=0 as "delete everything". Pruning never
        deletes the newest complete dump, whatever it is asked to keep."""
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)

        app_dir = tmp_path / "test-app"
        app_dir.mkdir()
        for i in range(3):
            f = write_dump(app_dir / f"2024010{i}T000000Z.sql.gz")
            os.utime(str(f), (1000 + i, 1000 + i))

        deleted = backup_module.prune_backups("test-app", keep=0)
        assert len(deleted) == 2
        assert [p.name for p in app_dir.glob("*.sql.gz")] == ["20240102T000000Z.sql.gz"]


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------

class TestRestore:
    def _make_app(self):
        return {
            "app_name": "test-app",
            "railway_project_id": "proj-1",
            "railway_service_id": "svc-1",
            "railway_environment_id": "env-1",
        }

    def test_restore_calls_psql_with_decompressed_data(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module

        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        # Create a gzipped backup file
        backup_path = tmp_path / "test.sql.gz"
        sql_data = SAMPLE_DUMP
        write_dump(backup_path, sql_data)

        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/psql"):
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stderr = b""
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(mock_proc)) as mock_run:
                    backup_module.restore(self._make_app(), str(backup_path))

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "/usr/bin/psql"
        # M2: the password must NOT appear in argv (visible via `ps aux`); it
        # is passed through PGPASSWORD in the env instead. Host/user/db stay in
        # the connection URL.
        url_arg = cmd[1]
        assert ":pass@" not in url_arg, "password must not be in the psql argv"
        assert "user@host" in url_arg, "host/user must be preserved (password stripped)"
        assert call_args[1]["env"]["PGPASSWORD"] == "pass"
        assert call_args[1]["input"] == sql_data

    def test_snapshot_retries_on_auth_failure_and_keeps_password_out_of_argv(
        self, tmp_path, monkeypatch
    ):
        """M2/M6/M9: snapshot retries on a credential-rotation auth failure,
        passes the password via PGPASSWORD env (never in argv), and writes the
        backup file 0o600 on the successful attempt."""
        import stat
        import monitor.backup as backup_module
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
        fake_db_url = "postgresql://user:pass@host:5432/db"

        auth_fail = MagicMock(returncode=1, stdout=b"",
                              stderr=b"FATAL: password authentication failed")
        success = MagicMock(returncode=0, stdout=SAMPLE_DUMP, stderr=b"")

        with patch.object(backup_module, "_ensure_pg_dump", return_value="/usr/bin/pg_dump"), \
             patch.object(backup_module, "_backup_dir_for", return_value=tmp_path), \
             patch.object(backup_module, "get_service_database_url", return_value=fake_db_url), \
             patch.object(backup_module, "check_pg_dump_is_new_enough"), \
             patch("monitor.backup.time.sleep"), \
             patch("monitor.backup.subprocess.run", side_effect=pg_run(auth_fail, success)) as mock_run:
            path = backup_module.snapshot(self._make_app(), "pre-fix-test")

        assert mock_run.call_count == 2, "should retry once after the auth failure"
        for call in mock_run.call_args_list:
            cmd = call[0][0]  # [pg_dump, "--dbname", safe_url, "--no-password"]
            assert ":pass@" not in cmd[2], "password must not be in the pg_dump argv"
            assert call[1]["env"]["PGPASSWORD"] == "pass"
        assert Path(path).exists()
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600

    def test_snapshot_does_not_retry_permanent_fatal(self, tmp_path, monkeypatch):
        """M6: a permanent FATAL (e.g. database does not exist) must NOT be
        retried (the old broad 'FATAL' match burned 60s of sleeps first)."""
        import monitor.backup as backup_module
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")
        perm_fail = MagicMock(returncode=1, stdout=b"",
                              stderr=b'FATAL: database "db" does not exist')

        with patch.object(backup_module, "_ensure_pg_dump", return_value="/usr/bin/pg_dump"), \
             patch.object(backup_module, "_backup_dir_for", return_value=tmp_path), \
             patch.object(backup_module, "get_service_database_url",
                          return_value="postgresql://user:pass@host:5432/db"), \
             patch.object(backup_module, "check_pg_dump_is_new_enough"), \
             patch("monitor.backup.time.sleep") as mock_sleep, \
             patch("monitor.backup.subprocess.run", side_effect=pg_run(perm_fail)) as mock_run:
            with pytest.raises(RuntimeError):
                backup_module.snapshot(self._make_app(), "pre-fix-test")

        assert mock_run.call_count == 1, "permanent FATAL must not retry"
        mock_sleep.assert_not_called()

    def test_restore_raises_if_backup_file_missing(self, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        with pytest.raises(RuntimeError, match="Backup file not found"):
            backup_module.restore(self._make_app(), "/nonexistent/path.sql.gz")

    def test_restore_raises_if_psql_missing(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        backup_path = write_dump(tmp_path / "test.sql.gz")

        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value=None):
                with pytest.raises(RuntimeError, match="psql not found"):
                    backup_module.restore(self._make_app(), str(backup_path))

    def test_restore_raises_if_no_railway_token(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)

        backup_path = write_dump(tmp_path / "test.sql.gz")

        with patch("monitor.backup.shutil.which", return_value="/usr/bin/psql"):
            with pytest.raises(RuntimeError, match="RAILWAY_API_TOKEN not set"):
                backup_module.restore(self._make_app(), str(backup_path))

    def test_restore_raises_if_psql_fails(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setenv("RAILWAY_API_TOKEN", "fake-token")

        backup_path = write_dump(tmp_path / "test.sql.gz")

        fake_db_url = "postgresql://user:pass@host:5432/db"

        with patch.object(backup_module, "get_service_database_url", return_value=fake_db_url):
            with patch("monitor.backup.shutil.which", return_value="/usr/bin/psql"):
                mock_proc = MagicMock()
                mock_proc.returncode = 1
                mock_proc.stderr = b"connection refused"
                with patch("monitor.backup.subprocess.run", side_effect=pg_run(mock_proc)):
                    with pytest.raises(RuntimeError, match="psql restore failed"):
                        backup_module.restore(self._make_app(), str(backup_path))


# ---------------------------------------------------------------------------
# run_daily_backups()
# ---------------------------------------------------------------------------

class TestRunDailyBackups:
    def test_snapshots_all_registered_apps(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        import monitor.backup as backup_module

        apps = [
            {"app_name": "app-a", "live_url": "https://a.example"},
            {"app_name": "app-b", "live_url": "https://b.example"},
        ]

        with patch("monitor.registry.list_apps", return_value=apps):
            with patch.object(backup_module, "snapshot", return_value="/tmp/backup.sql.gz") as mock_snap:
                with patch.object(backup_module, "prune_backups") as mock_prune:
                    results = backup_module.run_daily_backups()

        assert len(results) == 2
        assert mock_snap.call_count == 2
        assert mock_prune.call_count == 2
        for r in results:
            assert "backup_path" in r

    def test_continues_on_individual_failure(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        import monitor.backup as backup_module

        apps = [
            {"app_name": "app-fail", "live_url": "https://fail.example"},
            {"app_name": "app-ok", "live_url": "https://ok.example"},
        ]

        def side_effect(app, reason):
            if app["app_name"] == "app-fail":
                raise RuntimeError("no token")
            return "/tmp/backup.sql.gz"

        with patch("monitor.registry.list_apps", return_value=apps):
            with patch.object(backup_module, "snapshot", side_effect=side_effect):
                with patch.object(backup_module, "prune_backups"):
                    results = backup_module.run_daily_backups()

        assert len(results) == 2
        assert "error" in results[0]
        assert "backup_path" in results[1]

    def test_empty_app_list(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        with patch("monitor.registry.list_apps", return_value=[]):
            import monitor.backup as backup_module
            results = backup_module.run_daily_backups()

        assert results == []


class TestRunDailyBackupsNoToken:
    """Regression guard: when RAILWAY_API_TOKEN is unset (e.g. the 2026-04-15
    coordinator cost-incident safety response), ``run_daily_backups()`` must
    return a single {skipped: True} marker, NOT raise, NOT iterate apps,
    NOT spam Sentry per-app. The worker's backup thread reads the marker and
    logs once at info level."""

    def test_returns_skipped_marker_when_token_missing(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        import monitor.backup as backup_module

        with patch("monitor.registry.list_apps") as mock_list:
            with patch.object(backup_module, "snapshot") as mock_snap:
                results = backup_module.run_daily_backups()

        # Exactly one result: the skip marker
        assert len(results) == 1
        assert results[0].get("skipped") is True
        assert "RAILWAY_API_TOKEN" in results[0].get("reason", "")

        # Must NOT have iterated apps or called snapshot
        mock_list.assert_not_called()
        mock_snap.assert_not_called()

    def test_returns_skipped_marker_when_token_empty_string(self, monkeypatch):
        """Empty string should be treated as unset (both are falsy)."""
        monkeypatch.setenv("RAILWAY_API_TOKEN", "")
        import monitor.backup as backup_module

        with patch("monitor.registry.list_apps") as mock_list:
            results = backup_module.run_daily_backups()

        assert len(results) == 1
        assert results[0].get("skipped") is True
        mock_list.assert_not_called()

    def test_skip_does_not_raise(self, monkeypatch):
        """Contract: graceful skip, never raise. The worker's backup thread
        must be able to call this without a try/except for missing token."""
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        import monitor.backup as backup_module
        # Should not raise
        backup_module.run_daily_backups()


class TestRunDailyBackupsFailureSeverity:
    """A dump that was not taken is an error, whatever the cause.

    That includes a rejected password. Nothing in this repository rotates a
    customer database's password, and a rejection can also mean pg_dump
    reached some other database than the app's, so that failure is not logged
    more quietly than any other."""

    @pytest.mark.parametrize("message", [
        'pg_dump failed (exit 1): FATAL: password authentication failed for user "railway"',
        "pg_dump not found; install PostgreSQL client tools",
        "DATABASE_URL not found for service svc-1",
    ])
    def test_every_failed_dump_is_logged_at_error_and_reported(self, monkeypatch, caplog, message):
        import logging
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        import monitor.backup as backup_module

        apps = [{"app_name": "unlucky-app", "live_url": "https://unlucky.example.test"}]

        def fail(app, reason):
            raise RuntimeError(message)

        with patch("monitor.registry.list_apps", return_value=apps):
            with patch.object(backup_module, "snapshot", side_effect=fail):
                with caplog.at_level(logging.WARNING, logger="monitor.backup"):
                    results = backup_module.run_daily_backups()

        assert results == [{"app_name": "unlucky-app", "error": message}]
        relevant = [r for r in caplog.records if "unlucky-app" in r.getMessage()]
        assert len(relevant) >= 1, "expected at least one log record for unlucky-app"
        assert all(r.levelno == logging.ERROR for r in relevant), (
            f"a failed dump should log at ERROR; got {[r.levelname for r in relevant]}"
        )

    def test_the_audit_row_says_the_run_did_not_succeed(self, monkeypatch):
        """_record_backup_audit is what the coordinator's stale-backup check
        reads: one failed app makes the run unsuccessful."""
        import monitor.backup as backup_module
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/unused-by-this-test")
        recorded = {}

        def fake_log_action(action, description, success=True, metadata=None, **_kwargs):
            recorded.update(action=action, success=success, metadata=metadata)

        results = [{"app_name": "a", "backup_path": "/x/a.sql.gz"}, {"app_name": "b", "error": "boom"}]
        with patch("ops.audit.log_action", side_effect=fake_log_action):
            # The autouse fixture replaces the module attribute; call the real function.
            _REAL_RECORD_BACKUP_AUDIT(results)
        assert recorded == {"action": "backup", "success": False,
                            "metadata": {"apps": 2, "succeeded": 1}}


class TestBackupDirectoryPerRegisteredApp:
    """app_name is not unique across customers; the backup directory is."""

    def test_two_apps_with_the_same_name_get_different_directories(self):
        import monitor.backup as backup_module
        first = {"id": "11111111-aaaa-bbbb-cccc-000000000001", "app_name": "garden-tracker"}
        second = {"id": "22222222-aaaa-bbbb-cccc-000000000002", "app_name": "garden-tracker"}
        assert backup_module.backup_key(first) != backup_module.backup_key(second)
        assert backup_module.backup_key(first) == "garden-tracker__11111111-aaaa-bbbb-cccc-000000000001"

    def test_key_falls_back_to_the_railway_project_then_the_name(self):
        import monitor.backup as backup_module
        assert backup_module.backup_key({"app_name": "a", "railway_project_id": "proj-9"}) == "a__proj-9"
        assert backup_module.backup_key({"app_name": "a"}) == "a"

    def test_key_cannot_leave_the_backups_directory(self, tmp_path, monkeypatch):
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        key = backup_module.backup_key({"id": "../../x", "app_name": "../../evil"})
        directory = backup_module._backup_dir_for(key)
        assert directory.parent == tmp_path

    def test_a_daily_run_keeps_the_dumps_of_two_namesakes_apart(self, tmp_path, monkeypatch):
        """Through run_daily_backups and the real snapshot(), with only Railway
        and pg_dump replaced. In one shared directory the two dumps of a run
        would even get the same file name (a timestamp to the second), and the
        second customer's dump would replace the first."""
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        apps = [
            {"id": "11111111-aaaa-bbbb-cccc-000000000001", "app_name": "garden-tracker",
             "railway_project_id": "proj-1", "railway_service_id": "svc-1", "railway_environment_id": "env-1"},
            {"id": "22222222-aaaa-bbbb-cccc-000000000002", "app_name": "garden-tracker",
             "railway_project_id": "proj-2", "railway_service_id": "svc-2", "railway_environment_id": "env-2"},
        ]

        def url_for(project_id, service_id, environment_id, token):
            return f"postgresql://railway@db-of-{project_id}.example.test:5432/railway"

        def fake_pg_dump(cmd, **kwargs):
            # What pg_dump does: write the script to the file it was given.
            kwargs["stdout"].write(SAMPLE_DUMP.replace(b"widgets", ("dump of " + cmd[2]).encode()))
            kwargs["stdout"].flush()
            return MagicMock(returncode=0)

        with patch("monitor.registry.list_apps", return_value=apps), \
             patch.object(backup_module, "get_service_database_url", side_effect=url_for), \
             patch.object(backup_module, "_ensure_pg_dump", return_value="/usr/bin/pg_dump"), \
             patch("monitor.backup.subprocess.run", side_effect=fake_pg_dump):
            results = backup_module.run_daily_backups()

        paths = [Path(result["backup_path"]) for result in results]
        assert len(paths) == 2 and paths[0].parent != paths[1].parent
        for path, project in zip(paths, ("proj-1", "proj-2")):
            with gzip.open(str(path), "rb") as gz:
                assert f"db-of-{project}".encode() in gz.read()

    def test_pruning_one_app_does_not_touch_its_namesake(self, tmp_path, monkeypatch):
        """keep=7 applies to each registered app's own directory. In a shared
        directory it would apply to both apps' dumps together."""
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        apps = [
            {"id": "11111111-aaaa-bbbb-cccc-000000000001", "app_name": "garden-tracker"},
            {"id": "22222222-aaaa-bbbb-cccc-000000000002", "app_name": "garden-tracker"},
        ]
        counter = iter(range(1000))

        def fake_snapshot(app, reason):
            directory = backup_module._backup_dir_for(backup_module.backup_key(app))
            serial = next(counter)
            path = write_dump(directory / f"dump-{serial:04d}.sql.gz")
            os.utime(path, (1_700_000_000 + serial, 1_700_000_000 + serial))
            return str(path)

        with patch("monitor.registry.list_apps", return_value=apps), \
             patch.object(backup_module, "snapshot", side_effect=fake_snapshot):
            for _day in range(9):
                backup_module.run_daily_backups()

        for app in apps:
            kept = backup_module.list_backups(backup_module.backup_key(app))
            assert len(kept) == 7, (app["id"], len(kept))


@pytest.mark.requires_tools("pg_dump", "psql")
class TestSnapshotAndRestoreAgainstRealPostgres:
    """snapshot() and restore() with the real pg_dump and psql against a
    throwaway database. Only the Railway lookup is replaced."""

    APP = {
        "id": "33333333-aaaa-bbbb-cccc-000000000003", "app_name": "restore-probe",
        "railway_project_id": "proj-1", "railway_service_id": "svc-1",
        "railway_environment_id": "env-1",
    }

    @staticmethod
    def _as_url(dsn: str) -> str:
        """The scratch fixture hands out a key=value DSN; the module under test
        is given a URL, as Railway would report it."""
        from urllib.parse import quote
        from psycopg2 import extensions

        params = extensions.parse_dsn(dsn)
        host = params.get("host", "localhost")
        credentials = ""
        if params.get("user"):
            credentials = quote(params["user"], safe="")
            if params.get("password"):
                credentials += ":" + quote(params["password"], safe="")
            credentials += "@"
        database = quote(params["dbname"], safe="")
        if host.startswith("/"):
            return f"postgresql://{credentials}/{database}?host={quote(host, safe='')}"
        port = f":{params['port']}" if params.get("port") else ""
        return f"postgresql://{credentials}{host}{port}/{database}"

    @pytest.fixture()
    def db(self, scratch_pg_url, tmp_path, monkeypatch):
        import psycopg2
        import monitor.backup as backup_module

        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path / "backups")
        self.database_url = self._as_url(scratch_pg_url)
        connection = psycopg2.connect(scratch_pg_url)
        connection.autocommit = True
        with connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS backup_probe_child, backup_probe")
            cur.execute("CREATE TABLE backup_probe (id INT PRIMARY KEY, note TEXT)")
            cur.execute(
                "CREATE TABLE backup_probe_child (id INT PRIMARY KEY, "
                "parent INT NOT NULL REFERENCES backup_probe(id))")
            cur.execute("INSERT INTO backup_probe VALUES (1, 'original one'), (2, 'original two')")
            cur.execute("INSERT INTO backup_probe_child VALUES (10, 1)")
        with patch.object(backup_module, "get_service_database_url", return_value=self.database_url):
            yield connection
        with connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS backup_probe_child, backup_probe")
        connection.close()

    def _rows(self, connection):
        with connection.cursor() as cur:
            cur.execute("SELECT id, note FROM backup_probe ORDER BY id")
            return cur.fetchall()

    def _damage(self, connection):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM backup_probe_child")
            cur.execute("UPDATE backup_probe SET note = 'DAMAGED' WHERE id = 1")
            cur.execute("DELETE FROM backup_probe WHERE id = 2")
            cur.execute("INSERT INTO backup_probe VALUES (3, 'written after the snapshot')")

    def test_restore_brings_back_the_snapshot(self, db):
        import monitor.backup as backup_module

        path = backup_module.snapshot(self.APP, reason="test")
        assert Path(path).parent.name == backup_module.backup_key(self.APP)
        self._damage(db)
        assert self._rows(db) == [(1, "DAMAGED"), (3, "written after the snapshot")]

        backup_module.restore(self.APP, path)

        assert self._rows(db) == [(1, "original one"), (2, "original two")]
        with db.cursor() as cur:
            cur.execute("SELECT id, parent FROM backup_probe_child")
            assert cur.fetchall() == [(10, 1)]

    def test_a_restore_that_cannot_apply_raises_and_changes_nothing(self, db, tmp_path):
        """A plain dump without DROP statements replayed onto the populated
        database: every CREATE and COPY fails. Without ON_ERROR_STOP psql
        exits 0 on that and the restore would be logged as completed; it must
        raise, and the single transaction must leave the database as it was."""
        import subprocess
        import monitor.backup as backup_module

        safe_url, env = backup_module._libpq_conn_args(self.database_url)
        plain = subprocess.run(
            ["pg_dump", "--dbname", safe_url, "--no-password", "--table", "backup_probe"],
            capture_output=True, env={**os.environ, **env}, check=True,
        ).stdout
        old_style = tmp_path / "old-style.sql.gz"
        with gzip.open(str(old_style), "wb") as gz:
            gz.write(plain)
        self._damage(db)
        damaged = self._rows(db)

        with pytest.raises(RuntimeError, match="psql restore failed"):
            backup_module.restore(self.APP, str(old_style))

        assert self._rows(db) == damaged

    def test_the_real_dump_is_complete_and_a_cut_copy_of_it_is_refused(self, db, tmp_path):
        """dump_problem against what the installed pg_dump really writes
        (newer versions end the script with a line after the closing
        comment), and against the same file cut short the way a full disk or
        a killed process leaves it."""
        import monitor.backup as backup_module

        path = Path(backup_module.snapshot(self.APP, reason="test"))
        assert backup_module.dump_problem(path) is None
        assert [p.name for p in path.parent.iterdir()] == [path.name], "a temporary file was left behind"

        whole = path.read_bytes()
        cut = tmp_path / "cut.sql.gz"
        cut.write_bytes(whole[:len(whole) // 2])
        assert "not a readable gzip file" in backup_module.dump_problem(cut)

        with gzip.open(str(path), "rb") as gz:
            script = gz.read()
        without_the_end = write_dump(tmp_path / "short.sql.gz", script[:script.index(b"dump complete")])
        assert backup_module.dump_problem(without_the_end) == "ends before pg_dump's 'dump complete' line"

        self._damage(db)
        damaged = self._rows(db)
        for broken in (cut, without_the_end):
            with pytest.raises(RuntimeError, match="Refusing to restore"):
                backup_module.restore(self.APP, str(broken))
        assert self._rows(db) == damaged, "a refused restore changed the database"


# ---------------------------------------------------------------------------
# A file named <timestamp>.sql.gz is a complete dump, or it does not exist
# ---------------------------------------------------------------------------

APP = {"id": "44444444-aaaa-bbbb-cccc-000000000004", "app_name": "atomic-probe",
       "railway_project_id": "proj-1", "railway_service_id": "svc-1", "railway_environment_id": "env-1"}


def _fake_pg_dump(directory: Path, script_body: str) -> str:
    """An executable that stands in for pg_dump: a real child process that
    writes to the stdout it is given, the way pg_dump does."""
    executable = directory / "fake_pg_dump"
    executable.write_text("#!/bin/sh\n" + script_body)
    executable.chmod(0o755)
    return str(executable)


@pytest.fixture()
def dumps(tmp_path, monkeypatch):
    """BACKUPS_DIR in a temp directory and Railway's answer replaced; pg_dump
    is whatever executable the test installs with `use`."""
    import monitor.backup as backup_module

    monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
    monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup_module, "get_service_database_url",
                        lambda *ids: "postgresql://railway@db.example.test:5432/railway")
    monkeypatch.setattr(backup_module.time, "sleep", lambda _seconds: None)
    # These tests are about what happens to the dump files. The comparison of
    # pg_dump's version with the server's needs a reachable server and a
    # pg_dump that answers --version; tests/test_postgres_client_version.py
    # runs it for real.
    monkeypatch.setattr(backup_module, "check_pg_dump_is_new_enough", lambda pg_dump, database_url: None)
    sample = tmp_path / "sample.sql"
    sample.write_bytes(SAMPLE_DUMP)
    directory = backup_module._backup_dir_for(backup_module.backup_key(APP))

    class Dumps:
        module = backup_module
        folder = directory
        sample_path = sample

        @staticmethod
        def use(script_body: str):
            monkeypatch.setattr(backup_module, "_ensure_pg_dump",
                                lambda: _fake_pg_dump(tmp_path, script_body))

        @staticmethod
        def names():
            return sorted(p.name for p in directory.iterdir())

    return Dumps


@pytest.mark.requires_tools("sh")
class TestSnapshotIsAtomicAndValidated:
    def test_a_good_dump_gets_its_name_only_after_it_was_checked(self, dumps):
        """While pg_dump runs, nothing in the directory is named like a dump:
        the child process itself lists the directory and finds no *.sql.gz.
        (The earlier code created the final name before pg_dump started.)"""
        seen = dumps.folder.parent / "seen-during-the-dump.txt"
        dumps.use(f'ls "{dumps.folder}" | grep "sql.gz$" > "{seen}"\ncat "{dumps.sample_path}"\n')

        path = Path(dumps.module.snapshot(APP, "test"))

        assert seen.read_text() == "", "a file with a dump's name existed before the dump was complete"
        assert dumps.names() == [path.name] and path.name.endswith(".sql.gz")
        assert dumps.module.dump_problem(path) is None
        assert (path.stat().st_mode & 0o777) == 0o600
        with gzip.open(str(path), "rb") as gz:
            assert gz.read() == SAMPLE_DUMP

    def test_the_file_is_synced_under_its_temporary_name_then_renamed_then_the_directory_is_synced(
            self, dumps, monkeypatch):
        """The order of the durable steps. A rename of a file whose bytes are
        still in the page cache can survive a crash as an empty file with a
        dump's name."""
        import stat
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            status = os.fstat(fd)
            events.append(("fsync directory", None) if stat.S_ISDIR(status.st_mode)
                          else ("fsync file", status.st_size))
            return real_fsync(fd)

        def replace(source, target):
            events.append(("rename", (Path(source).name, Path(target).name)))
            return real_replace(source, target)

        monkeypatch.setattr(dumps.module.os, "fsync", fsync)
        monkeypatch.setattr(dumps.module.os, "replace", replace)
        dumps.use(f'cat "{dumps.sample_path}"\n')

        path = Path(dumps.module.snapshot(APP, "test"))

        assert [kind for kind, _detail in events] == ["fsync file", "rename", "fsync directory"], events
        assert events[0][1] == path.stat().st_size, "the file was synced before its last byte was written"
        source, target = events[1][1]
        assert source.endswith(".sql.gz.partial") and target == path.name

    @pytest.mark.parametrize("script_body, why", [
        ("exit 0\n", "empty"),
        ('printf -- "--\\n-- PostgreSQL database dump\\n--\\n\\nCREATE TABLE t (id int);\\n"\n',
         "ends before pg_dump's 'dump complete' line"),
        ('echo "connection to server was lost"\n', "does not start with the pg_dump header"),
    ])
    def test_a_pg_dump_that_exits_0_without_a_complete_dump_leaves_no_file(self, dumps, script_body, why):
        """pg_dump's exit status is not the test: the earlier code named
        whatever arrived, an empty file included, a dump."""
        dumps.use(script_body)
        with pytest.raises(RuntimeError, match="not a complete dump") as refused:
            dumps.module.snapshot(APP, "test")
        assert why in str(refused.value)
        assert dumps.names() == [], "an incomplete dump (or a temporary file) was left in the backup directory"
        assert dumps.module.list_backups(dumps.module.backup_key(APP)) == []

    def test_a_pg_dump_that_dies_halfway_leaves_no_file(self, dumps):
        dumps.use(f'head -c 40 "{dumps.sample_path}"\necho "FATAL: terminating connection" >&2\nexit 1\n')
        with pytest.raises(RuntimeError, match="pg_dump failed .exit 1.: FATAL: terminating connection"):
            dumps.module.snapshot(APP, "test")
        assert dumps.names() == []

    def test_a_pg_dump_that_hangs_is_killed_and_leaves_no_file(self, dumps, monkeypatch):
        monkeypatch.setattr(dumps.module, "_SUBPROCESS_TIMEOUT_S", 1)
        dumps.use(f'head -c 40 "{dumps.sample_path}"\nexec sleep 30\n')
        with pytest.raises(RuntimeError, match="timed out after 1s"):
            dumps.module.snapshot(APP, "test")
        assert dumps.names() == []

    def test_error_text_is_kept_short_however_much_pg_dump_writes(self, dumps):
        dumps.use('i=0; while [ $i -lt 2000 ]; do echo "pg_dump: error: line $i of a very long complaint" >&2; '
                  'i=$((i+1)); done; exit 1\n')
        with pytest.raises(RuntimeError) as failed:
            dumps.module.snapshot(APP, "test")
        assert len(str(failed.value)) < dumps.module._STDERR_KEEP_BYTES + 200

    def test_a_week_of_failing_dumps_does_not_cost_the_last_good_one(self, dumps):
        """The sequence the review reproduced: one good dump, then seven
        daily runs in which pg_dump produced nothing. The earlier code wrote
        seven empty files and pruned the good dump as the eighth newest."""
        dumps.use(f'cat "{dumps.sample_path}"\n')
        with patch("monitor.registry.list_apps", return_value=[APP]):
            good = Path(dumps.module.run_daily_backups()[0]["backup_path"])
            os.utime(good, (1_700_000_000, 1_700_000_000))
            dumps.use("exit 0\n")
            for _day in range(7):
                results = dumps.module.run_daily_backups()
                assert "not a complete dump" in results[0]["error"]
        assert dumps.names() == [good.name]
        assert dumps.module.dump_problem(good) is None

    def test_a_stale_partial_file_from_a_killed_run_is_cleared(self, dumps):
        stale = dumps.folder / "20240101T000000Z.sql.gz.partial"
        stale.write_bytes(b"half a dump")
        os.utime(stale, (1_600_000_000, 1_600_000_000))
        fresh = dumps.folder / "20991231T000000Z.sql.partial"
        fresh.write_bytes(b"another run's file, still being written")
        dumps.use(f'cat "{dumps.sample_path}"\n')
        path = Path(dumps.module.snapshot(APP, "test"))
        assert dumps.names() == sorted([fresh.name, path.name])


class TestPruneCountsOnlyCompleteDumps:
    def _aged(self, path, age_rank, content=None):
        """Give `path` a modification time by rank (higher is newer); with
        `content`, write those bytes first (b"" makes an empty file)."""
        if content is not None:
            path.write_bytes(content)
        os.utime(path, (1_700_000_000 + age_rank, 1_700_000_000 + age_rank))
        return path

    def test_empty_files_do_not_push_the_good_dump_out(self, tmp_path, monkeypatch):
        """What the earlier code left on disk after a bad week: one good dump
        and seven newer empty files. keep=7 used to keep the seven empty ones."""
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        folder = tmp_path / "app"
        folder.mkdir()
        good = self._aged(write_dump(folder / "20240101T000000Z.sql.gz"), 0)
        empties = [self._aged(folder / f"2024010{day}T000000Z.sql.gz", day, b"") for day in range(2, 9)]

        deleted = backup_module.prune_backups("app", keep=7)

        assert deleted == []
        assert good.exists(), "the only complete dump was deleted"
        assert all(empty.exists() for empty in empties), "files that are not dumps are left for a person"

    def test_only_complete_dumps_are_counted_and_only_they_are_deleted(self, tmp_path, monkeypatch, caplog):
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        folder = tmp_path / "app"
        folder.mkdir()
        complete = [self._aged(write_dump(folder / f"good-{n}.sql.gz"), n * 10) for n in range(9)]
        cut_short = self._aged(write_dump(folder / "cut.sql.gz", SAMPLE_DUMP[:60]), 85)      # newest but one
        not_gzip = self._aged(folder / "junk.sql.gz", 45, b"x")
        half = self._aged(folder / "half.sql.gz", 5,                                           # among the oldest
                          write_dump(tmp_path / "whole.gz").read_bytes()[:20])

        with caplog.at_level("ERROR", logger="monitor.backup"):
            deleted = backup_module.prune_backups("app", keep=7)

        assert sorted(deleted) == sorted(str(p) for p in complete[:2]), "the two oldest complete dumps go"
        assert all(p.exists() for p in complete[2:]) and len(complete[2:]) == 7
        assert cut_short.exists() and not_gzip.exists() and half.exists()
        reported = [record.getMessage() for record in caplog.records]
        assert sum("is not a complete dump" in line for line in reported) == 3

    @pytest.mark.parametrize("keep", [0, -3, 1])
    def test_the_newest_complete_dump_is_never_deleted(self, tmp_path, monkeypatch, keep):
        import monitor.backup as backup_module
        monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path)
        folder = tmp_path / "app"
        folder.mkdir()
        dumps_written = [self._aged(write_dump(folder / f"d{n}.sql.gz"), n) for n in range(4)]
        newer_junk = self._aged(folder / "junk.sql.gz", 99, b"")
        backup_module.prune_backups("app", keep=keep)
        assert [p.exists() for p in dumps_written] == [False, False, False, True]
        assert newer_junk.exists()


class TestDumpProblem:
    @pytest.mark.parametrize("body, problem", [
        (SAMPLE_DUMP, None),
        (SAMPLE_DUMP + b"\\unrestrict abcDEF123\n\n", None),
        (b"", "empty"),
        (b"\n", "does not start with the pg_dump header"),
        (SAMPLE_DUMP[:40], "ends before pg_dump's 'dump complete' line"),
        (b"-- PostgreSQL database dump complete\n", "does not start with the pg_dump header"),
        (SAMPLE_DUMP + b"x" * 2000, "ends before pg_dump's 'dump complete' line"),
    ])
    def test_what_counts_as_a_complete_dump(self, tmp_path, body, problem):
        import monitor.backup as backup_module
        assert backup_module.dump_problem(write_dump(tmp_path / "d.sql.gz", body)) == problem

    def test_files_that_are_not_gzip_or_are_cut_inside_the_stream(self, tmp_path):
        import monitor.backup as backup_module
        empty = tmp_path / "empty.sql.gz"
        empty.touch()
        assert backup_module.dump_problem(empty) == "empty"
        plain = tmp_path / "plain.sql.gz"
        plain.write_bytes(SAMPLE_DUMP)
        assert "not a readable gzip file" in backup_module.dump_problem(plain)
        whole = write_dump(tmp_path / "whole.sql.gz").read_bytes()
        cut = tmp_path / "cut.sql.gz"
        cut.write_bytes(whole[:-6])
        assert "not a readable gzip file" in backup_module.dump_problem(cut)
        assert "not a readable gzip file" in backup_module.dump_problem(tmp_path / "missing.sql.gz")

    def test_a_large_dump_is_checked_without_being_held(self, tmp_path):
        """Read in pieces: only the first and the last bytes are kept."""
        import tracemalloc
        import monitor.backup as backup_module
        body = SAMPLE_DUMP[:40] + b"INSERT INTO widgets VALUES (1);\n" * 1_500_000 + SAMPLE_DUMP[40:]
        big = write_dump(tmp_path / "big.sql.gz", body)
        size = len(body)
        del body
        tracemalloc.start()
        try:
            assert backup_module.dump_problem(big) is None
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert size > 40 * 1024 * 1024 and peak < 8 * 1024 * 1024, f"peak {peak} bytes for a {size} byte dump"


class TestRestoreRefusesAnIncompleteDump:
    @pytest.mark.parametrize("body", [b"", SAMPLE_DUMP[:50], b"select 1;"])
    def test_nothing_is_contacted_for_a_file_that_is_not_a_complete_dump(self, tmp_path, monkeypatch, body):
        """psql exits 0 on an empty script, so the earlier code reported a
        restore of an empty file as completed."""
        import monitor.backup as backup_module
        monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
        broken = write_dump(tmp_path / "broken.sql.gz", body)
        with patch.object(backup_module, "get_service_database_url") as lookup, \
             patch("monitor.backup.shutil.which", return_value="/usr/bin/psql"), \
             patch("monitor.backup.subprocess.run") as run:
            with pytest.raises(RuntimeError, match="Refusing to restore"):
                backup_module.restore(APP, str(broken))
        lookup.assert_not_called()
        run.assert_not_called()


class TestBackupTimeoutSetting:
    @pytest.mark.parametrize("raw, seconds", [
        (None, 900), ("", 900), ("120", 120), (" 45 ", 45), ("15m", 900), ("0", 900), ("-5", 900), ("1e3", 900),
    ])
    def test_a_value_that_is_not_a_positive_whole_number_means_the_default(self, monkeypatch, raw, seconds):
        import monitor.backup as backup_module
        if raw is None:
            monkeypatch.delenv("CAS_BACKUP_TIMEOUT_S", raising=False)
        else:
            monkeypatch.setenv("CAS_BACKUP_TIMEOUT_S", raw)
        assert backup_module._timeout_seconds() == seconds

    def test_the_module_imports_with_a_mistyped_value(self):
        """The value is read at import, in the worker process."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c", "import monitor.backup as b; print(b._SUBPROCESS_TIMEOUT_S)"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=60,
            env={**os.environ, "CAS_BACKUP_TIMEOUT_S": "fifteen minutes", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert result.returncode == 0, result.stderr[-400:]
        assert result.stdout.strip().splitlines()[-1] == "900"
