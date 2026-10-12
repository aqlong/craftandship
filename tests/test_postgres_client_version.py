r"""The PostgreSQL client in the worker image against the server image deploy() provisions.

pg_dump refuses to dump a server whose major version is newer than its own.
Customer databases are `postgres:15` (cli/deploy.py), and Ubuntu 22.04's
`postgresql-client`, which the worker image used to install, is the 14 series:
every nightly dump would have failed, and the monitor's restart or redeploy
(monitor/diagnose.py) waits behind a failed snapshot.

Two kinds of test, and the difference matters:

  * A CONSISTENCY CHECK of text. The server major is stated once, in
    cli/deploy.py (POSTGRES_SERVER_MAJOR, used for the service image); the
    Dockerfile states the client major (ARG POSTGRES_CLIENT_MAJOR). The test
    parses both files and requires client >= server, and that the Dockerfile
    installs that versioned package from the PostgreSQL project's repository
    with a verified key. It does not build the image: Docker is not part of
    this repository's test environment, and nothing here proves what a built
    image contains.
  * A REAL RUN of monitor/backup.py's comparison, with a stand-in `pg_dump`
    executable on PATH and a real PostgreSQL server.

What proves a BUILT image is right is a job that builds it, and none does yet.
The commands such a job should run (Docker and network access; the build takes
several minutes):

    docker build -f Dockerfile.worker -t cas-worker-image-check .
    docker run --rm --entrypoint pg_dump cas-worker-image-check --version
        expected: one line "pg_dump (PostgreSQL) 15.<minor> (Ubuntu 15.<minor>-1.pgdg22.04+1)";
        the first number after "(PostgreSQL)" must be at least POSTGRES_SERVER_MAJOR
    docker run --rm --entrypoint sh cas-worker-image-check -c \
        'test "$(pg_dump --version | sed -E "s/^pg_dump \(PostgreSQL\) ([0-9]+).*/\1/")" -ge 15 && echo ok'
        expected: ok
    docker run -d --name pg-server -e POSTGRES_PASSWORD=<generated at run time> -p 55432:5432 postgres:15
    docker run --rm --network host -e PGPASSWORD=<the same value> --entrypoint pg_dump cas-worker-image-check \
        -h 127.0.0.1 -p 55432 -U postgres --schema-only postgres | tail -n 3
        expected: exit status 0 and a last comment line "-- PostgreSQL database dump complete"

Until then, "the worker image carries a pg_dump of at least the server's major"
is a statement about the Dockerfile's text, not about an image.
"""

import os
import re
import shutil
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
for _p in (str(ROOT / "cli"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests._fake_pg_tools import SAMPLE_DUMP

DOCKERFILE = ROOT / "Dockerfile.worker"
DEPLOY_PY = ROOT / "cli" / "deploy.py"

def _dockerfile_instructions() -> list:
    """The Dockerfile as a list of instructions, line continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def _server_major_stated_in_deploy_py() -> int:
    found = re.search(r"^POSTGRES_SERVER_MAJOR\s*=\s*(\d+)\s*$", DEPLOY_PY.read_text(encoding="utf-8"), re.MULTILINE)
    assert found, "cli/deploy.py no longer states POSTGRES_SERVER_MAJOR as a plain integer"
    return int(found.group(1))


def _client_major_stated_in_dockerfile() -> int:
    found = re.search(r"^ARG POSTGRES_CLIENT_MAJOR=(\d+)\s*$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert found, "Dockerfile.worker no longer states ARG POSTGRES_CLIENT_MAJOR as a plain integer"
    return int(found.group(1))


# ---------------------------------------------------------------------------
# Consistency of the two files (text only)
# ---------------------------------------------------------------------------

def test_the_server_major_is_stated_once_and_is_the_image_deploy_provisions():
    """The one statement of the server major is the one provision_postgres uses."""
    import deploy

    stated = _server_major_stated_in_deploy_py()
    assert deploy.POSTGRES_SERVER_MAJOR == stated
    assert deploy.POSTGRES_IMAGE == f"postgres:{stated}"

    sent = []

    def gql(query, variables, token, **kwargs):
        sent.append(variables)
        return {"serviceCreate": {"id": "svc-db"}}

    with patch.object(deploy, "gql", gql), patch.object(deploy, "set_env_vars"), \
            patch.object(deploy, "get_latest_deployment", return_value=None), \
            patch.object(deploy.time, "sleep"):
        deploy.provision_postgres("proj-1", "env-1", "not-a-token")
    created = [v for v in sent if v and "input" in v and "source" in v["input"]]
    assert created and created[0]["input"]["source"] == {"image": f"postgres:{stated}"}


def test_the_dockerfile_client_is_at_least_the_server_major():
    """A consistency check of two files' text, not a build of the image."""
    assert _client_major_stated_in_dockerfile() >= _server_major_stated_in_deploy_py()


def test_the_dockerfile_installs_the_versioned_client_from_the_postgresql_repository():
    instructions = _dockerfile_instructions()
    installs = [line for line in instructions if line.startswith("RUN") and "apt-get install" in line]
    assert any('"postgresql-client-${POSTGRES_CLIENT_MAJOR}"' in line for line in installs), installs
    for line in installs:
        # the distribution's unversioned meta package is the 14 series on Ubuntu 22.04
        assert not re.search(r"\bpostgresql-client(?![-\w$])", line), line


def test_the_postgresql_repository_is_trusted_by_a_key_whose_fingerprint_is_checked():
    instructions = _dockerfile_instructions()
    pgdg = [line for line in instructions if line.startswith("RUN") and "apt.postgresql.org" in line]
    assert len(pgdg) == 1, pgdg
    run = pgdg[0]
    fingerprint = re.search(r"^ARG PGDG_KEY_FINGERPRINT=([0-9A-F]{40})$", "\n".join(instructions), re.MULTILINE)
    assert fingerprint, "the key fingerprint is not pinned as a 40-digit hex ARG"
    assert 'gpg --show-keys --with-colons /tmp/pgdg.asc' in run and '= "${PGDG_KEY_FINGERPRINT}"' in run
    assert "signed-by=/usr/share/keyrings/postgresql-archive-keyring.gpg" in run
    # the key is saved to a file and checked before it is used: nothing is piped into a shell or into gpg
    assert re.search(r"curl [^|&;]*-o /tmp/pgdg\.asc ", run), run
    assert not re.search(r"\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", run), "the PostgreSQL step pipes into a shell"
    assert not re.search(r"curl[^&;]*\|", run), "curl output is piped somewhere in the PostgreSQL step"
    # the check comes before the key is installed, and the repository after
    assert run.index("--show-keys") < run.index("--dearmor") < run.index("apt.postgresql.org")


# ---------------------------------------------------------------------------
# monitor/backup.py compares the installed pg_dump with the server, for real
# ---------------------------------------------------------------------------

APP = {"id": "55555555-aaaa-bbbb-cccc-000000000005", "app_name": "client-version-probe",
       "railway_project_id": "proj-v", "railway_service_id": "svc-v", "railway_environment_id": "env-v"}


@pytest.fixture()
def database_url(scratch_pg_url):
    """The scratch server's URL exactly as the code under test receives it.
    Rebuilding it from the database name would drop the user and password, and
    a server that asks for a password (the CI service container) then refuses it."""
    return scratch_pg_url


@pytest.fixture()
def server_major(scratch_pg_url):
    """The scratch server's major version, asked of the server in SQL (not through the code under test)."""
    import psycopg2

    connection = psycopg2.connect(scratch_pg_url)
    try:
        with connection.cursor() as cur:
            cur.execute("SHOW server_version_num")
            return int(cur.fetchone()[0]) // 10000
    finally:
        connection.close()


@pytest.fixture()
def stand_in_pg_dump(tmp_path, monkeypatch, database_url):
    """A `pg_dump` on PATH, ahead of any real one. `version_line` is what it
    prints for --version; any other call is a dump: it leaves a marker file and
    writes a complete dump to its stdout."""
    import monitor.backup as backup_module

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sample = tmp_path / "sample.sql"
    sample.write_bytes(SAMPLE_DUMP)
    marker = tmp_path / "a-dump-was-attempted"

    def install(version_line: str):
        script = bin_dir / "pg_dump"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "--version" ]; then echo "{version_line}"; exit 0; fi\n'
            f': > "{marker}"\n'            # a shell builtin: the child's PATH is scrubbed
            f'while IFS= read -r line; do printf "%s\\n" "$line"; done < "{sample}"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("RAILWAY_API_TOKEN", "test-token-present")
    monkeypatch.setattr(backup_module, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup_module, "get_service_database_url", lambda *ids: database_url)

    class StandIn:
        module = backup_module
        dump_attempted = staticmethod(lambda: marker.exists())
        use = staticmethod(install)

    return StandIn


@pytest.mark.requires_tools("sh")
class TestSnapshotComparesTheVersions:
    def test_an_older_pg_dump_is_refused_naming_both_versions_before_any_dump(self, stand_in_pg_dump, server_major):
        stand_in_pg_dump.use("pg_dump (PostgreSQL) 9.6.24 (Ubuntu 9.6.24-1.pgdg22.04+1)")
        with pytest.raises(RuntimeError) as refused:
            stand_in_pg_dump.module.snapshot(APP, "pre-fix-test")
        message = str(refused.value)
        assert "pg_dump is version 9.6" in message, message
        assert f"database server is version {server_major}." in message, message
        assert "Dockerfile.worker" in message
        assert not stand_in_pg_dump.dump_attempted(), "pg_dump was run although it is too old"

    def test_a_refusal_is_not_retried(self, stand_in_pg_dump):
        stand_in_pg_dump.use("pg_dump (PostgreSQL) 9.6.24")
        with patch.object(stand_in_pg_dump.module, "get_service_database_url",
                          wraps=stand_in_pg_dump.module.get_service_database_url) as fetched, \
                pytest.raises(RuntimeError):
            stand_in_pg_dump.module.snapshot(APP, "pre-fix-test")
        assert fetched.call_count == 1

    def test_the_same_major_as_the_server_dumps(self, stand_in_pg_dump, server_major):
        stand_in_pg_dump.use(f"pg_dump (PostgreSQL) {server_major}.0 (stand-in)")
        path = stand_in_pg_dump.module.snapshot(APP, "pre-fix-test")
        assert Path(path).exists() and stand_in_pg_dump.dump_attempted()

    def test_a_newer_pg_dump_than_the_server_dumps(self, stand_in_pg_dump, server_major):
        stand_in_pg_dump.use(f"pg_dump (PostgreSQL) {server_major + 3}.1 (stand-in)")
        path = stand_in_pg_dump.module.snapshot(APP, "pre-fix-test")
        assert Path(path).exists()

    def test_a_pg_dump_whose_version_cannot_be_read_is_left_to_pg_dump_itself(self, stand_in_pg_dump, caplog):
        stand_in_pg_dump.use("this is not a version line")
        with caplog.at_level("WARNING", logger="monitor.backup"):
            path = stand_in_pg_dump.module.snapshot(APP, "pre-fix-test")
        assert Path(path).exists()
        assert any("could not compare the versions" in record.getMessage() for record in caplog.records)

    def test_the_comparison_reads_the_version_the_stand_in_prints(self, stand_in_pg_dump):
        stand_in_pg_dump.use("pg_dump (PostgreSQL) 17beta1 (Debian 17~beta1-1.pgdg120+1)")
        found = stand_in_pg_dump.module.pg_dump_version(shutil.which("pg_dump"))
        assert found == (17, "17")

    def test_the_server_version_is_the_servers_own(self, database_url, server_major):
        import monitor.backup as backup_module

        major, text = backup_module.server_version(database_url)
        assert major == server_major and text.startswith(f"{server_major}.")

    def test_a_server_that_cannot_be_reached_does_not_stop_the_dump_attempt(self, stand_in_pg_dump, monkeypatch):
        """No version to compare: pg_dump runs and fails (or not) on its own terms."""
        monkeypatch.setattr(stand_in_pg_dump.module, "get_service_database_url",
                            lambda *ids: "postgresql://localhost:1/nothing")
        stand_in_pg_dump.use("pg_dump (PostgreSQL) 9.6.24")
        path = stand_in_pg_dump.module.snapshot(APP, "pre-fix-test")
        assert Path(path).exists() and stand_in_pg_dump.dump_attempted()
