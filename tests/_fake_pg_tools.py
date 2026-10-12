"""Test helpers: stand in for pg_dump and psql, and write dump files.

monitor.backup.snapshot() gives pg_dump FILES for its output and its error
text (nothing is piped into the worker's memory) and then checks that what
was written is a complete dump before the file gets its final name. A
stand-in for subprocess.run therefore has to behave like the child process:
write to the files it was handed.

    with patch("monitor.backup.subprocess.run", side_effect=pg_run(process)) as run:
        backup.snapshot(app, "reason")

`process` is any object with returncode, stdout and stderr (bytes), for
example a MagicMock; several are used in order and the last one repeats. An
exception instance is raised instead.
"""

import gzip
from pathlib import Path

# What pg_dump's plain format looks like to monitor.backup.dump_problem: its
# header, some SQL, and the closing comment it writes last.
SAMPLE_DUMP = (
    b"--\n-- PostgreSQL database dump\n--\n\n"
    b"CREATE TABLE widgets (id integer);\n\n"
    b"--\n-- PostgreSQL database dump complete\n--\n\n"
)


def write_dump(path, body: bytes = SAMPLE_DUMP) -> Path:
    """Write `body` gzip-compressed to `path` (a complete dump by default)."""
    path = Path(path)
    with gzip.open(str(path), "wb") as stream:
        stream.write(body)
    return path


def pg_run(*processes):
    """A side_effect for a patched subprocess.run (see the module docstring)."""
    pending = list(processes)

    def run(argv, **kwargs):
        process = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(process, BaseException):
            raise process
        for stream in ("stdout", "stderr"):
            target, data = kwargs.get(stream), getattr(process, stream, b"")
            if hasattr(target, "write") and isinstance(data, bytes):
                target.write(data)
                target.flush()
        return process

    return run
