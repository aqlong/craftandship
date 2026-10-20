# monitor/backup.py - Database snapshots before any self-healing action
#
# Security notes:
# - DATABASE_URL is fetched fresh from Railway API each time; never stored locally.
# - DATABASE_URL is never logged in full; redact_url() is used before any logging.
# - Backup files are created with mode 0o600 (owner read/write only).
# - pg_dump is called with list-form subprocess (no shell=True); the password
#   travels in PGPASSWORD, not argv, and the child gets a minimal environment
#   (no API keys), a connect timeout and a wall-clock timeout.
# - IMPORTANT: backup.snapshot() MUST be called before any DB-touching fix.
#
# Where dumps go: monitor/backups/<app name>__<registry id>/ next to this file,
# or the same directory name under CAS_BACKUP_DIR if set. On a container with
# an ephemeral filesystem (Railway without a volume) that location is lost on
# every redeploy: point CAS_BACKUP_DIR at a mounted volume if the daily dumps
# are meant to survive. Dumps are not encrypted.
#
# What a file named <timestamp>.sql.gz means. snapshot() writes pg_dump's
# output and the compressed copy under temporary names (*.partial), flushes
# and fsyncs, checks the result with dump_problem() (a readable gzip stream
# that starts with pg_dump's header and ends with its "dump complete" line)
# and only then renames it to its final name. A crash, a full disk or a
# pg_dump that died halfway therefore leaves a *.partial file, never a
# *.sql.gz. prune_backups() makes the same check on every file before it
# counts it: it deletes only complete dumps beyond the newest `keep` complete
# ones, leaves anything else in place (and says so), and never deletes the
# newest complete dump. restore() refuses a file that fails the check.
#
# Bounds: one pg_dump or psql run gets CAS_BACKUP_TIMEOUT_S (default 900
# seconds). pg_dump writes to disk, not into this process, and the copy is
# compressed in 1 MiB pieces, so a dump's size is limited by the volume and
# not by memory; when the volume fills, the snapshot fails and its partial
# files are removed. restore() is the exception: it holds the uncompressed
# dump in memory while psql reads it (an operator command, not a loop).
#
# What this module does not establish. A dump is taken by running pg_dump in
# this process against the DATABASE_URL that Railway reports for the app's
# service. cli/deploy.py sets that variable to a URL whose host is the private
# name postgres.railway.internal, the same host for every customer app, and
# pg_dump runs in the platform's worker, not inside the customer's project.
# Whether that name is reachable from the worker, and which database answers
# there, depends on Railway's networking and cannot be told from this
# repository (a comment on _store_railway_metadata in queue/worker_pg.py also
# says the variables query came back empty after a deploy). So treat a failed
# dump as a failed dump: run_daily_backups reports each one as an error, and
# nothing here retries through another route.

import gzip
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from cli.deploy import redact_url
from monitor.bounded_fetch import provider_post

load_dotenv()

logger = logging.getLogger(__name__)

RAILWAY_API = "https://backboard.railway.app/graphql/v2"
# Deadline of the one Railway call a dump or a restore starts with.
RAILWAY_DEADLINE_S = 20
BACKUPS_DIR = Path(os.environ.get("CAS_BACKUP_DIR") or Path(__file__).parent / "backups")


def _timeout_seconds() -> int:
    """CAS_BACKUP_TIMEOUT_S as a whole number of seconds, at least 1; 900
    when unset or not such a number (a typo must not stop the worker from
    importing this module)."""
    raw = os.environ.get("CAS_BACKUP_TIMEOUT_S", "").strip()
    try:
        value = int(raw) if raw else 900
    except ValueError:
        logger.warning("CAS_BACKUP_TIMEOUT_S=%r is not a whole number; using 900", raw[:40])
        return 900
    if value < 1:
        logger.warning("CAS_BACKUP_TIMEOUT_S=%d is below 1; using 900", value)
        return 900
    return value


# Wall-clock limit for one pg_dump / psql run, in seconds (CAS_BACKUP_TIMEOUT_S).
# libpq has no default connect timeout, so an unreachable database would
# otherwise hang the daily backup thread forever.
_SUBPROCESS_TIMEOUT_S = _timeout_seconds()
_CONNECT_TIMEOUT_S = "30"

# How a complete plain-format pg_dump script begins and ends. Newer pg_dump
# versions follow the closing comment with a "\unrestrict" line, so the
# closing marker is looked for near the end, not at it.
_DUMP_HEADER = b"--\n-- PostgreSQL database dump\n--"
_DUMP_COMPLETE = b"-- PostgreSQL database dump complete"
_DUMP_TAIL_BYTES = 512
_COPY_CHUNK_BYTES = 1024 * 1024
# How much of pg_dump's or psql's stderr is kept for the error message.
_STDERR_KEEP_BYTES = 8192
# Suffix of the files snapshot() writes before a dump has been validated.
# list_backups() matches "*.sql.gz", which these names do not end in.
_PARTIAL_SUFFIX = ".partial"
# The only parent-environment variables a libpq child needs.
_CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE",
                   "SSL_CERT_DIR", "PGSSLMODE", "PGSSLROOTCERT")


# ---------------------------------------------------------------------------
# Railway helper
# ---------------------------------------------------------------------------

def get_service_database_url(
    railway_project_id: str,
    railway_service_id: str,
    railway_environment_id: str,
    token: str,
) -> str:
    """Fetch DATABASE_URL from Railway service environment variables via GraphQL.

    The DATABASE_URL is returned to the caller but never stored in the
    monitor's registry database. The token is sent only in the Authorization
    header and never logged.

    Args:
        railway_project_id: Railway project ID.
        railway_service_id: Railway service ID.
        railway_environment_id: Railway environment ID.
        token: Railway API token.

    Returns:
        DATABASE_URL string for the service.

    Raises:
        RuntimeError: If the API call fails or DATABASE_URL is not set.
    """
    query = """
        query Variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
            variables(
                projectId: $projectId
                environmentId: $environmentId
                serviceId: $serviceId
            )
        }
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    response = provider_post(
        RAILWAY_API,
        json={
            "query": query,
            "variables": {
                "projectId": railway_project_id,
                "environmentId": railway_environment_id,
                "serviceId": railway_service_id,
            },
        },
        headers=headers,
        timeout=RAILWAY_DEADLINE_S,
    )
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors'][0]['message']}")

    variables = data.get("data", {}).get("variables") or {}
    database_url = variables.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            f"DATABASE_URL not found for service {railway_service_id}"
        )
    return database_url


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

def _ensure_pg_dump() -> str:
    """Return the path to pg_dump; raise if not available."""
    pg_dump_path = shutil.which("pg_dump")
    if pg_dump_path is None:
        raise RuntimeError(
            "pg_dump not found; install PostgreSQL client tools to enable backups"
        )
    return pg_dump_path


# Bound on asking pg_dump for its version, and on opening a connection to ask
# the server for its own: 15 seconds each. Both are asked once per snapshot
# attempt.
_VERSION_PROBE_TIMEOUT_S = 15
_PG_DUMP_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?")


def pg_dump_version(pg_dump: str) -> tuple[int, str]:
    """(major, text) of the pg_dump at `pg_dump`, from `pg_dump --version`.

    The text is the version as pg_dump prints it ("15.17"). Raises
    RuntimeError when the program cannot be run in time or prints something
    that is not a version.
    """
    try:
        proc = subprocess.run(
            [pg_dump, "--version"], capture_output=True, text=True,
            timeout=_VERSION_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot run {pg_dump} --version: {type(exc).__name__}") from None
    found = _PG_DUMP_VERSION_RE.search(proc.stdout or "")
    if proc.returncode != 0 or found is None:
        raise RuntimeError(f"cannot read the version of {pg_dump} from: {(proc.stdout or '')[:80].strip()!r}")
    major, minor = int(found.group(1)), found.group(2)
    return major, f"{major}.{minor}" if minor is not None else str(major)


def server_version(database_url: str) -> tuple[int, str]:
    """(major, text) of the PostgreSQL server `database_url` points at.

    Asks the server itself over a short-lived connection (psycopg2 reports the
    version the server announced at connection time). Raises psycopg2.Error
    when no connection can be made within _VERSION_PROBE_TIMEOUT_S.
    """
    import psycopg2

    connection = psycopg2.connect(database_url, connect_timeout=_VERSION_PROBE_TIMEOUT_S)
    try:
        number = connection.server_version
    finally:
        connection.close()
    if number >= 100000:
        return number // 10000, f"{number // 10000}.{number % 10000}"
    return number // 10000, f"{number // 10000}.{number // 100 % 100}.{number % 100}"


def check_pg_dump_is_new_enough(pg_dump: str, database_url: str) -> None:
    """Refuse, naming both versions, when `pg_dump` is older than the server it is to dump.

    pg_dump will not dump a server whose major version is newer than its own
    ("aborting because of server version mismatch"), and that will not
    change between attempts, so this is a failure of the snapshot, not a
    reason to retry. A server that is older than pg_dump is fine.

    If either version cannot be read, nothing is refused here: pg_dump itself
    still refuses a server it cannot dump, and the failure is then its own
    message, reported like any other failed dump. (A server that cannot be
    reached is the case the retry loop in snapshot() is for.)
    """
    try:
        client_major, client_text = pg_dump_version(pg_dump)
        server_major, server_text = server_version(database_url)
    except Exception as exc:
        logger.warning("snapshot: could not compare the versions of pg_dump and the server "
                       "(%s: %s); leaving it to pg_dump", type(exc).__name__, exc)
        return
    if client_major < server_major:
        raise RuntimeError(
            f"pg_dump is version {client_text} but the database server is version {server_text}: "
            f"pg_dump will not dump a server newer than itself. Install a PostgreSQL client of "
            f"major version {server_major} or newer in the worker image (Dockerfile.worker, "
            f"POSTGRES_CLIENT_MAJOR)."
        )


def _safe_dir_name(app_name: str) -> str:
    """Map an app name to one safe directory name (no separators, no traversal)."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(app_name or "")).strip(".")
    return name or "app"


def backup_key(app: dict) -> str:
    """Directory name that holds one registered app's dumps.

    app_name alone is not unique: two customers can register apps with the
    same name, and their dumps would then share a directory, where
    prune_backups would count them together and nothing in a file name would
    say whose database a dump came from. The key therefore appends the app's
    registry id (monitor_apps.id), falling back to its Railway project id for
    an app dict that carries no id.
    """
    name = _safe_dir_name(app.get("app_name"))
    unique = app.get("id") or app.get("railway_project_id")
    if not unique:
        return name
    return f"{name}__{_safe_dir_name(str(unique))}"


def dump_problem(path) -> Optional[str]:
    """Why the file at `path` is not a complete dump, or None when it is one.

    A complete dump is a gzip stream that decompresses to its end without an
    error (the stream's own length and checksum trailer are checked on the
    way), is not empty, starts with pg_dump's plain-format header and carries
    pg_dump's "dump complete" line within its last _DUMP_TAIL_BYTES bytes.
    pg_dump writes that line last, so a dump that was cut short (a killed
    process, a full disk, a dropped connection) does not have it.

    The file is read in pieces; nothing but its first and last bytes is kept.
    `pg_restore --list` is not used: it reads the custom, tar and directory
    formats, and these dumps are plain SQL scripts for psql.
    """
    head = b""
    tail = b""
    try:
        with gzip.open(str(path), "rb") as stream:
            while True:
                piece = stream.read(_COPY_CHUNK_BYTES)
                if not piece:
                    break
                if len(head) < len(_DUMP_HEADER):
                    head = (head + piece)[:len(_DUMP_HEADER)]
                tail = (tail + piece)[-_DUMP_TAIL_BYTES:]
    except (OSError, EOFError, zlib.error) as exc:
        return f"not a readable gzip file ({type(exc).__name__}: {exc})"
    if not tail:
        return "empty"
    if head != _DUMP_HEADER:
        return "does not start with the pg_dump header"
    if _DUMP_COMPLETE not in tail:
        return "ends before pg_dump's 'dump complete' line"
    return None


def _fsync_directory(directory: Path) -> None:
    """Make a rename inside `directory` durable (best effort where the
    platform does not allow opening a directory)."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError as exc:
        logger.debug("could not open %s to fsync it: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("could not fsync %s: %s", directory, exc)
    finally:
        os.close(fd)


def _remove_stale_partials(backup_dir: Path) -> None:
    """Delete *.partial files an earlier snapshot left behind when it was
    killed. Only files older than two dump timeouts are touched, so a
    snapshot of the same app that is still running keeps its own."""
    cutoff = time.time() - 2 * _SUBPROCESS_TIMEOUT_S
    for leftover in backup_dir.glob(f"*{_PARTIAL_SUFFIX}"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
                logger.warning("snapshot: removed stale partial file %s", leftover)
        except OSError as exc:
            logger.warning("snapshot: could not remove stale partial file %s: %s", leftover, exc)


def _backup_dir_for(key: str) -> Path:
    """Return (and create) the backup directory for a backup key or app name.

    The name is sanitized: it comes from the registry, which is fed by customer
    app names, and must never be able to write outside BACKUPS_DIR.
    """
    backup_dir = BACKUPS_DIR / _safe_dir_name(key)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _libpq_conn_args(database_url: str):
    """Split a postgres URL into a password-stripped connection URL plus an env
    dict carrying the password as PGPASSWORD. Passing the password via env (not
    argv) keeps it out of `ps aux` output, while the returned URL preserves
    host, user, port, database, and query params (e.g. sslmode) so the
    connection is unchanged. Returns (safe_url, env)."""
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(database_url)
    env = {k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ}
    env["PGCONNECT_TIMEOUT"] = _CONNECT_TIMEOUT_S
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    netloc = parsed.hostname or ""
    # urlparse drops the brackets of an IPv6 literal; a URL needs them back,
    # or "::1" followed by ":5432" no longer says where the port starts.
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if parsed.username:
        netloc = f"{parsed.username}@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    safe_url = urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    return safe_url, env


def snapshot(app: dict, reason: str) -> str:
    """Take a pg_dump of the app's database and compress it.

    Fetches the DATABASE_URL fresh from the Railway API (never from the
    monitor registry). Saves the dump to:
        <backups dir>/{backup_key(app)}/{timestamp}.sql.gz

    The dump is a plain SQL script taken with --clean --if-exists: it drops
    each object it is about to recreate, so restore() can replay it onto the
    database it was taken from.

    The backup file is written with mode 0o600 (owner-only read/write).
    DATABASE_URL is logged only in redacted form.

    IMPORTANT: This function MUST be called before any DB-touching self-healing
    fix. The ordering is enforced by run_diagnosis() in diagnose.py.

    Args:
        app: App dict from the registry (must include railway_* fields).
        reason: Short label describing why the snapshot was taken
                (e.g. 'pre-fix-tier3').

    Returns:
        Absolute path to the created backup file.

    Raises:
        RuntimeError: If pg_dump is not installed, DATABASE_URL is missing,
                      or pg_dump exits with a non-zero return code.
    """
    token = os.getenv("RAILWAY_API_TOKEN")
    if not token:
        raise RuntimeError("RAILWAY_API_TOKEN not set in environment")

    pg_dump = _ensure_pg_dump()
    backup_dir = _backup_dir_for(backup_key(app))
    _remove_stale_partials(backup_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{timestamp}.sql.gz"
    # Neither temporary name ends in ".sql.gz": until the dump has been
    # validated and renamed, list_backups() and prune_backups() do not see it.
    raw_path = backup_dir / f"{timestamp}.sql{_PARTIAL_SUFFIX}"
    packed_path = backup_dir / f"{timestamp}.sql.gz{_PARTIAL_SUFFIX}"

    def _discard_partials():
        raw_path.unlink(missing_ok=True)
        packed_path.unlink(missing_ok=True)

    # Up to 3 attempts for the failures listed as retryable below. Each
    # attempt fetches DATABASE_URL again, in case it changed.
    max_attempts = 3
    last_error = None
    try:
        for attempt in range(1, max_attempts + 1):
            database_url = get_service_database_url(
                app["railway_project_id"],
                app["railway_service_id"],
                app["railway_environment_id"],
                token,
            )
            logger.info(
                "snapshot: fetched DATABASE_URL for %s (attempt %d): %s",
                app["app_name"], attempt,
                redact_url(database_url),
            )

            check_pg_dump_is_new_enough(pg_dump, database_url)

            logger.info(
                "snapshot: dumping %s (reason: %s) -> %s",
                app["app_name"], reason, backup_path,
            )

            # pg_dump is called with list form (no shell=True) to prevent shell
            # injection. The password is passed via PGPASSWORD in the env (not in
            # argv) so it never appears in `ps aux`; the --dbname URL is stripped
            # of the password. Its output goes to a file created with mode 0o600
            # and its stderr to an unnamed temporary file: neither is held in
            # this process's memory, whatever the size of the database.
            safe_url, dump_env = _libpq_conn_args(database_url)
            raw_fd = os.open(str(raw_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(raw_fd, "wb") as raw_file, tempfile.TemporaryFile() as stderr_file:
                try:
                    proc = subprocess.run(
                        [pg_dump, "--dbname", safe_url, "--no-password", "--clean", "--if-exists"],
                        stdout=raw_file,
                        stderr=stderr_file,
                        env=dump_env,
                        timeout=_SUBPROCESS_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"pg_dump timed out after {_SUBPROCESS_TIMEOUT_S}s") from None
                stderr_file.seek(0)
                stderr = stderr_file.read(_STDERR_KEEP_BYTES).decode(errors="replace").strip()

            if proc.returncode == 0:
                last_error = None
                break

            last_error = f"pg_dump failed (exit {proc.returncode}): {stderr}"

            # Retry only on failures that can clear by themselves: a rejected
            # password (the URL is fetched again on the next attempt), the DB
            # still starting, or connection saturation. A broad "FATAL" match
            # would also retry permanent errors like "FATAL: database ... does not
            # exist", burning up to 60s of sleeps before failing anyway.
            retryable = (
                "password authentication failed" in stderr
                or "the database system is starting up" in stderr
                or "too many connections" in stderr
            )
            if retryable and attempt < max_attempts:
                delay = 10 * attempt
                logger.warning(
                    "snapshot: retryable pg_dump failure for %s (attempt %d/%d), retrying in %ds",
                    app["app_name"], attempt, max_attempts, delay,
                )
                time.sleep(delay)
                continue

            # Anything else, or the last attempt, fails the snapshot.
            raise RuntimeError(last_error)

        if last_error:
            raise RuntimeError(last_error)

        # Compress the script into the second temporary file, piece by piece,
        # and make it durable before it is looked at.
        packed_fd = os.open(str(packed_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(packed_fd, "wb") as packed_file:
            with open(raw_path, "rb") as raw_file, gzip.GzipFile(fileobj=packed_file, mode="wb") as gz_file:
                shutil.copyfileobj(raw_file, gz_file, _COPY_CHUNK_BYTES)
            packed_file.flush()
            os.fsync(packed_file.fileno())
        raw_path.unlink(missing_ok=True)

        # pg_dump exiting 0 is not the test. The file that is about to get a
        # dump's name must be one.
        problem = dump_problem(packed_path)
        if problem:
            raise RuntimeError(f"pg_dump exited 0 but its output is not a complete dump: {problem}")

        os.chmod(str(packed_path), 0o600)
        os.replace(str(packed_path), str(backup_path))
        _fsync_directory(backup_dir)
    except BaseException:
        _discard_partials()
        raise

    logger.info("snapshot: backup written to %s", backup_path)
    return str(backup_path)


def restore(app: dict, backup_path: str) -> None:
    """Restore a database from a backup file.

    Fetches the DATABASE_URL fresh from the Railway API, decompresses the
    backup, and pipes it into psql. The DATABASE_URL is never logged in full.

    The script runs with ON_ERROR_STOP in a single transaction: the first
    statement that fails aborts the restore, psql exits non-zero, this
    function raises, and the database is left exactly as it was. Without those
    two switches psql carries on after an error and exits 0, so a restore that
    restored nothing would be reported as completed.

    A dump taken by snapshot() drops each object before recreating it and so
    can be replayed onto the database it came from. A plain dump taken without
    --clean only restores into an empty database; replayed onto a populated
    one it fails at its first CREATE, and this function raises.

    Only a complete dump is replayed: a file that fails dump_problem() (empty,
    cut off, not a pg_dump script) is refused before anything is contacted.

    Args:
        app: App dict from the registry (must include railway_* fields).
        backup_path: Absolute path to the .sql.gz backup file.

    Raises:
        RuntimeError: If psql is not installed, DATABASE_URL is missing,
                      backup file does not exist, or psql exits non-zero.
    """
    backup_file = Path(backup_path)
    if not backup_file.exists():
        raise RuntimeError(f"Backup file not found: {backup_path}")
    # An empty or cut-off file would be replayed "successfully": psql exits 0
    # on a script that ends early, and the database would be reported restored.
    problem = dump_problem(backup_file)
    if problem:
        raise RuntimeError(f"Refusing to restore {backup_path}: {problem}")

    psql_path = shutil.which("psql")
    if psql_path is None:
        raise RuntimeError(
            "psql not found; install PostgreSQL client tools to enable restore"
        )

    token = os.getenv("RAILWAY_API_TOKEN")
    if not token:
        raise RuntimeError("RAILWAY_API_TOKEN not set in environment")

    database_url = get_service_database_url(
        app["railway_project_id"],
        app["railway_service_id"],
        app["railway_environment_id"],
        token,
    )
    logger.info(
        "restore: fetched DATABASE_URL for %s: %s",
        app["app_name"],
        redact_url(database_url),
    )

    logger.info("restore: restoring %s from %s", app["app_name"], backup_path)

    with gzip.open(str(backup_file), "rb") as gz_file:
        sql_data = gz_file.read()

    # Password via PGPASSWORD env, password-stripped URL in argv (keeps the
    # secret out of `ps aux`).
    safe_url, restore_env = _libpq_conn_args(database_url)
    try:
        proc = subprocess.run(
            [psql_path, safe_url, "--no-password", "--no-psqlrc",
             "--set", "ON_ERROR_STOP=1", "--single-transaction"],
            input=sql_data,
            capture_output=True,
            env=restore_env,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"psql restore timed out after {_SUBPROCESS_TIMEOUT_S}s")

    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"psql restore failed (exit {proc.returncode}): {stderr}")

    logger.info("restore: completed for %s", app["app_name"])


def prune_backups(app_name: str, keep: int = 7) -> list:
    """Delete complete dumps beyond the newest `keep` complete ones.

    Every file is checked with dump_problem() first, and only files that pass
    take part: they are the ones that are counted and the only ones that are
    deleted. A file that fails (empty, cut off, not a dump) is left where it
    is and logged as an error. It does not use up one of the `keep` places,
    so a week of empty files cannot push the last good dump out. The newest
    complete dump is never deleted, with keep=0 as well.

    Args:
        app_name: The directory name under the backups dir: backup_key(app)
            for a registered app (what snapshot() writes to).
        keep: Number of most recent complete dumps to retain (default 7).

    Returns:
        List of deleted file paths.
    """
    complete, incomplete = [], []
    for path in list_backups(app_name):          # newest first
        problem = dump_problem(path)
        if problem:
            incomplete.append((path, problem))
        else:
            complete.append(path)
    for path, problem in incomplete:
        logger.error("prune_backups: %s is not a complete dump (%s); left in place, not counted",
                     path, problem)
    to_delete = complete[max(1, int(keep)):]
    for path in to_delete:
        Path(path).unlink(missing_ok=True)
        logger.info("prune_backups: deleted %s", path)
    return to_delete


def _record_backup_audit(results: list) -> None:
    """Write one ops_actions 'backup' row per run so the coordinator can see it.

    coordinator/work_detection.py:_check_stale_backup alerts when the newest
    successful 'backup' row is over 48h old; without this writer that trigger
    could never fire. success is True only if every app was dumped. Best
    effort: an audit failure never fails the backup run.
    """
    real = [r for r in results if not r.get("skipped")]
    if not real or not os.environ.get("DATABASE_URL"):
        return
    ok = [r for r in real if "backup_path" in r]
    try:
        from ops.audit import log_action
        log_action(
            "backup",
            f"Daily backups: {len(ok)} of {len(real)} apps dumped",
            success=len(ok) == len(real),
            metadata={"apps": len(real), "succeeded": len(ok)},
        )
    except Exception as exc:
        logger.warning("run_daily_backups: could not record audit row: %s", exc)


def run_daily_backups() -> list:
    """Snapshot all registered apps and prune old backups.

    Graceful-skip behaviour: if ``RAILWAY_API_TOKEN`` is missing from the
    environment (e.g. intentionally unset during a cost-incident response; 
    see the 2026-04-15 coordinator-cost event), return an empty list with a
    single ``skipped=True`` marker instead of failing per-app. The caller
    (worker's backup thread) then logs a single info-level message and
    avoids per-app Sentry alerts for what is a config state, not a bug.

    Returns:
        List of dicts. Each dict has ``app_name`` and either
        ``backup_path`` (success), ``error`` (per-app failure), or
        ``skipped`` (when no token is configured). Callers should treat
        ``skipped`` as non-fatal and NOT alert.
    """
    from monitor.registry import list_apps

    # Config-level precondition: no Railway token = nothing to back up.
    # Fail fast with a single structured result instead of iterating through
    # every registered app and raising the same RuntimeError N times.
    if not os.getenv("RAILWAY_API_TOKEN"):
        logger.warning(
            "run_daily_backups: RAILWAY_API_TOKEN not set; skipping all backups "
            "(config state, not an error)"
        )
        return [{"app_name": "*", "skipped": True, "reason": "RAILWAY_API_TOKEN not set"}]

    apps = list_apps()
    results = []

    for app in apps:
        try:
            backup_path = snapshot(app, reason="daily")
            prune_backups(backup_key(app))
            results.append({"app_name": app["app_name"], "backup_path": backup_path})
            logger.info("run_daily_backups: backed up %s", app["app_name"])
        except Exception as exc:
            results.append({"app_name": app["app_name"], "error": str(exc)})
            # A dump that was not taken is an error whatever the cause,
            # including a rejected password: nothing in this repository
            # rotates a customer database password, so that rejection is not a
            # state that explains itself, and it may mean pg_dump reached a
            # different database than the app's (see the note at the top of
            # this file). The caller alerts on every result that has "error".
            logger.error("run_daily_backups: failed for %s: %s", app["app_name"], exc)

    _record_backup_audit(results)
    return results


def list_backups(app_name: str) -> list:
    """List all backup files in one backup directory, newest first.

    Args:
        app_name: The directory name under the backups dir: backup_key(app)
            for a registered app. It is sanitized by _safe_dir_name.

    Returns:
        List of absolute path strings for .sql.gz files, sorted newest first.
        Returns an empty list if no backups exist. The list is of file names:
        a file written by snapshot() was validated before it got its name,
        but a file put there by other means may not be a dump (dump_problem()
        tells).
    """
    backup_dir = BACKUPS_DIR / _safe_dir_name(app_name)
    if not backup_dir.exists():
        return []
    files = sorted(
        backup_dir.glob("*.sql.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [str(file) for file in files]
