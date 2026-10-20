#!/usr/bin/env python3
"""The roadmap executor's merge guard: which paths a branch may not touch.

scripts/cas-roadmap-loop.sh pipes a branch's diff into this script and merges
nothing when it prints a line or exits non-zero.

  cas-merge-guard.py            reads `git diff -z --name-status --no-renames`
                                records (STATUS NUL PATH NUL) on stdin
  cas-merge-guard.py --raw      reads `git diff -z --raw --no-renames` records
                                (":SRCMODE DSTMODE SRCSHA DSTSHA STATUS" NUL
                                PATH NUL): the same path rules, plus the file
                                mode each path ends up with

Output: one line per refused path, plain ASCII (other bytes are shown as
escapes). Exit 0 when the whole input was read and judged, whether or not
anything was refused. Exit 2 when the input is not what git writes (a record
cut short, a status that --no-renames cannot produce, an empty path) or is
larger than MAX_INPUT_BYTES: the caller treats that as a refusal, because a
diff that cannot be read cannot be cleared.

What is refused:
  - a change of any kind (added, modified, deleted, type changed) to a path in
    PROTECTED_PATHS;
  - the deletion of a file under a directory named tests, test or __tests__
    (a branch may add and edit tests; it may not remove one);
  - a path that is not valid UTF-8;
  - a path with a byte outside printable ASCII (0x20 to 0x7E). Git stores
    paths as bytes, but the checkout the merge lands in may be on a filesystem
    that folds case and Unicode normalization (APFS by default): there
    `.githoo<U+212A>s/pre-push`, spelled with KELVIN SIGN, is the same file as
    `.githooks/pre-push`. No tracked path in this repository needs such a
    byte, so the rule costs nothing; a path that also folds to a protected
    one is reported as such;
  - with --raw: a path the branch leaves as a symlink (mode 120000; the
    executor links web/node_modules into its worktrees, and a committed
    symlink once broke production deploys) or as a submodule link (160000).

Protected paths are compared as bytes, ASCII letters without regard to case
(`.GitHooks/pre-push` is `.githooks/pre-push` on a case-insensitive
filesystem). Nothing here depends on the locale.

Run with `python3 -I`: it needs only the standard library, and isolated mode
keeps PYTHONPATH, the user site directory and the script's own directory off
sys.path.
"""

import re
import sys
import unicodedata

# The largest diff listing this script reads. A listing is one short record
# per changed path; 16 MiB is hundreds of thousands of paths.
MAX_INPUT_BYTES = 16 * 1024 * 1024

# Paths an executor branch may never add, modify, delete or rename. Each entry
# is a regular expression that must match the whole path.
PROTECTED_PATHS = (
    # What every Claude Code session in this repository reads or obeys.
    rb"(.*/)?CLAUDE(\.local)?\.md",
    rb"\.claude/.*",
    rb"\.mcp\.json",
    # The executor's own queue (the loop updates item status itself).
    rb"coordinator/roadmap\.json",
    # scripts/: the loop, this guard, the picker, the prompt, the config and
    # the risk classifier, and every script the git hooks, the CI workflows
    # and the Claude Code hooks run (tests/test_executor_loop_behavior.py
    # checks that each one they name is covered).
    rb"scripts/.*",
    # The git hooks, the CI workflows and the secret scanner's allowlist.
    rb"\.githooks/.*",
    rb"\.github/.*",
    rb"\.gitleaks\.toml",
    # What `npm install` and `npm test` read or run before a web test: any
    # package.json, lockfile or shrinkwrap (the test command, lifecycle
    # scripts, the dependency set, a workspace root above web/), any .npmrc
    # (script-shell there replaces the shell npm runs the test command with)
    # and anything under a node_modules (a .bin shim is first on the script
    # PATH).
    rb"(.*/)?(package|package-lock|npm-shrinkwrap)\.json",
    rb"(.*/)?\.npmrc",
    rb"(.*/)?node_modules(/.*)?",
    # What Vitest evaluates before a test file: its own configuration, the
    # PostCSS configuration it loads at startup, and the setup file the
    # configuration names (tests/test_executor_loop_behavior.py reads the real
    # vitest.config.ts and checks that every setup file it names is covered).
    rb"web/vite(st)?\.[^/]*",
    rb"web/postcss\.config\.[^/]*",
    rb"web/\.postcssrc[^/]*",
    rb"web/tests/unit/setup-no-dotenv\.ts",
    # What pytest reads or imports before it collects a test: every
    # conftest.py and pytest configuration file, top-level Python modules
    # (source, bytecode and extension modules), the __init__.py of top-level
    # packages (pytest puts the repository root first on sys.path before it
    # imports conftest.py, so a top-level module shadows the standard library
    # for whatever conftest.py imports, and tests/ and compose/ are
    # themselves packages), and top-level *.dist-info and *.egg-info
    # directories (plugin entry points).
    rb"(.*/)?(conftest\.py|\.?pytest\.(ini|toml)|pyproject\.toml|tox\.ini|setup\.cfg)",
    rb"[^/]+\.(py|pyc|pyd|so|pth)",
    rb"[^/]+/__init__\.py",
    rb"[^/]+\.(dist|egg)-info/.*",
    # Compiled Python at any depth. Python imports a module from its bytecode
    # in __pycache__ (a hash-based .pyc marked unchecked is never compared with
    # the source), from a .pyc where the source would be, and from an
    # extension module ahead of a .py of the same name, so committed bytecode
    # would run code that no source in the diff shows (the bytecode of
    # tests/__init__.py, for one, would run before the first test).
    rb"(.*/)?__pycache__/.*",
    rb"(.*/)?[^/]+\.(pyc|pyo|pyd|so)",
    # Files a version-manager shim reads to choose the interpreter.
    rb"(.*/)?\.(python-version|tool-versions|node-version|nvmrc)",
    # JavaScript that tools evaluate while the gate or a deploy runs them:
    # `tailwindcss` loads web/tailwind.config.js (tests/test_web_committed_css.py
    # runs it in the gate, and `npm run build` runs it at deploy), Playwright
    # loads its configuration in the CI `ui` job.
    rb"web/tailwind\.config\.[^/]*",
    rb"web/playwright\.config\.[^/]*",
    # What is built into the deployed images and how the platform builds and
    # starts them: every Dockerfile, the Docker build context's ignore file, the
    # Railway and nixpacks build configuration (build and start commands, the
    # Dockerfile to use, the watch patterns that decide whether the worker
    # redeploys), and what the worker image and the CI python job install
    # (requirements*.txt).
    rb"(.*/)?Dockerfile[^/]*",
    rb"(.*/)?\.dockerignore",
    rb"(.*/)?(railway\.(toml|json)|nixpacks\.toml)",
    rb"(.*/)?requirements[^/]*\.txt",
    # What a person runs after following the README: `make hooks` installs the
    # git hooks (core.hooksPath), and `docker compose up` starts the local
    # database from a compose file, an override file next to it included.
    rb"Makefile",
    rb"(.*/)?(docker-)?compose(\.[^/]*)?\.ya?ml",
    # web/src/db/schema.sql is applied at every boot of the deployed web app,
    # on every row in the database, and nothing but the web unit tests (which a
    # branch may edit) and the pre-commit hook (which a merge does not run)
    # judges it; migrate.ts is what applies it.
    rb"web/src/db/schema\.sql",
    rb"web/src/db/migrate\.ts",
)

# Directories whose files may be added and edited but not deleted (a rename is
# a deletion plus an addition, because the diff is read with --no-renames).
# tests/test_executor_loop_behavior.py walks the repository and fails when a
# test file lives somewhere this does not cover.
TEST_DIRS = rb"(.*/)?(tests|test|__tests__)/.+"

_PROTECTED_RE = re.compile(b"(?:" + b"|".join(PROTECTED_PATHS) + b")", re.IGNORECASE | re.DOTALL)
_TEST_DIRS_RE = re.compile(TEST_DIRS, re.IGNORECASE | re.DOTALL)

# Statuses `git diff --no-renames` writes for a tree-to-tree comparison:
# added, modified, deleted, type changed.
STATUSES = (b"A", b"M", b"D", b"T")
SYMLINK_MODE = b"120000"
GITLINK_MODE = b"160000"


class UnreadableDiff(Exception):
    """The input is not a diff listing this script can judge."""


def is_protected(path: bytes) -> bool:
    return _PROTECTED_RE.fullmatch(path) is not None


def is_under_tests(path: bytes) -> bool:
    return _TEST_DIRS_RE.fullmatch(path) is not None


def is_plain_ascii(path: bytes) -> bool:
    return all(0x20 <= byte <= 0x7E for byte in path)


def shown(path: bytes) -> str:
    """The path as printable ASCII: every other byte becomes an escape."""
    try:
        text = path.decode("utf-8")
    except UnicodeDecodeError:
        text = path.decode("utf-8", "backslashreplace")
    return "".join(ch if " " <= ch <= "~" else ch.encode("unicode_escape").decode("ascii") for ch in text)


def folded(text: str) -> str:
    """What a filesystem that ignores case, normalization and invisible
    characters may take `text` for: format characters removed (zero-width
    joiners and the like, which HFS+ ignores in a name), compatibility
    normalized and case folded until that changes nothing more."""
    current = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    for _ in range(4):
        step = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", current).casefold())
        if step == current:
            break
        current = step
    return current


def path_violation(status: bytes, path: bytes):
    """The line to print for one changed path, or None when it is allowed."""
    try:
        text = path.decode("utf-8")
    except UnicodeDecodeError:
        return f"{shown(path)} (path is not valid UTF-8)"
    if is_plain_ascii(path):
        if is_protected(path):
            return shown(path)
        if status == b"D" and is_under_tests(path):
            return f"{shown(path)} (test file deleted)"
        return None
    reads_as = folded(text).encode("utf-8")
    if is_protected(path) or is_protected(reads_as) or (status == b"D" and is_under_tests(reads_as)):
        return f"{shown(path)} (not plain ASCII; reads as the protected path {shown(reads_as)})"
    return f"{shown(path)} (path is not plain ASCII)"


def mode_violation(mode: bytes, path: bytes):
    if mode == SYMLINK_MODE:
        return f"{shown(path)} (symlink)"
    if mode == GITLINK_MODE:
        return f"{shown(path)} (submodule link)"
    return None


def _fields(data: bytes):
    """The NUL-terminated fields of `data`. Git ends every field with a NUL,
    so anything after the last one is a record that was cut short."""
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise UnreadableDiff("the listing does not end with a NUL")
    return data[:-1].split(b"\0")


def _pairs(data: bytes):
    fields = _fields(data)
    if len(fields) % 2:
        raise UnreadableDiff("a record has no path")
    for index in range(0, len(fields), 2):
        if not fields[index + 1]:
            raise UnreadableDiff("a record has an empty path")
        yield fields[index], fields[index + 1]


def name_status_violations(data: bytes):
    """Refusals for `git diff -z --name-status --no-renames` output."""
    lines = []
    for status, path in _pairs(data):
        if status not in STATUSES:
            raise UnreadableDiff(f"unexpected status {shown(status)!r}")
        line = path_violation(status, path)
        if line is not None:
            lines.append(line)
    return lines


def raw_violations(data: bytes):
    """Refusals for `git diff -z --raw --no-renames` output."""
    lines = []
    for meta, path in _pairs(data):
        parts = meta.split(b" ")
        if len(parts) != 5 or not parts[0].startswith(b":"):
            raise UnreadableDiff(f"unexpected record {shown(meta)!r}")
        new_mode, status = parts[1], parts[4]
        if status not in STATUSES:
            raise UnreadableDiff(f"unexpected status {shown(status)!r}")
        if not re.fullmatch(rb"[0-7]{6}", new_mode):
            raise UnreadableDiff(f"unexpected file mode {shown(new_mode)!r}")
        for line in (path_violation(status, path), mode_violation(new_mode, path)):
            if line is not None:
                lines.append(line)
    return lines


def main(argv):
    if argv not in ([], ["--raw"]):
        print("usage: cas-merge-guard.py [--raw] < diff-listing", file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        if len(data) > MAX_INPUT_BYTES:
            raise UnreadableDiff(f"the listing is larger than {MAX_INPUT_BYTES} bytes")
        lines = raw_violations(data) if argv else name_status_violations(data)
    except UnreadableDiff as exc:
        print(f"merge guard: cannot read the diff: {exc}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # any fault here must read as a refusal, never as "nothing refused"
        print(f"merge guard: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
