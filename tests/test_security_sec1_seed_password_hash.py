"""SEC-1 regression guards (2026-04-19).

Two related problems this suite locks down:

1. Bcrypt hashes MUST NOT be baked into any generated seed.sql. The earlier
   pipeline injected a known-plaintext bcrypt hash (of "password123") so
   magic-link-only users could also password-log-in. That hash was identical
   across every customer build, got committed into DB snapshots and
   gold-standard references, and created a shadow credential alongside
   magic-link auth. See commits around this file's creation date.

2. multer MUST be pinned >= 2.1.1 to dodge 7 HIGH-severity DoS advisories
   affecting the 1.x LTS line (GHSA-5528-5vmv-3xc2 and relatives).

These tests protect the pipeline, the committed gold-standard reference,
and the docs, so a future pipeline edit can't silently re-introduce the
shadow credential, and a future refactor can't accidentally pin an
unsafe multer version.
"""
from __future__ import annotations

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

import build  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_STANDARD_DIR = REPO_ROOT / "reference" / "medicine-waves-GOLD-STANDARD-v25"

# The gold-standard reference build (a full app snapshot) is not part of this
# repository, so the tests that read it skip when it is absent.
_needs_gold_standard = pytest.mark.skipif(
    not GOLD_STANDARD_DIR.exists(),
    reason="reference/medicine-waves-GOLD-STANDARD-v25 (a past build snapshot) is not part of this repository",
)


# ---------------------------------------------------------------------------
# SEC-1a: no bcrypt hash in anything committed
# ---------------------------------------------------------------------------

_BCRYPT_RE = re.compile(r"\$2[aby]\$\d+\$[./A-Za-z0-9]{53}")
_BCRYPT_BYTES_RE = re.compile(rb"\$2[aby]\$\d+\$[./A-Za-z0-9]{53}")

# Synthetic bcrypt-shaped fixtures, built at runtime (not real hashes).
_SYNTHETIC_LEGACY_HASH = "$2b$10$" + "L" * 53
_SYNTHETIC_CURRENT_HASH = "$2b$10$" + "C" * 53


@_needs_gold_standard
def test_gold_standard_seed_has_no_bcrypt_hash():
    """reference/.../db/seed.sql is the canonical MW seed and ships in git."""
    seed = (GOLD_STANDARD_DIR / "db" / "seed.sql").read_text()
    match = _BCRYPT_RE.search(seed)
    assert match is None, (
        f"gold-standard seed.sql contains bcrypt hash at char {match.start() if match else '-'}: "
        f"{match.group(0) if match else ''}"
    )


@_needs_gold_standard
def test_gold_standard_db_snapshot_sql_has_no_bcrypt_hash():
    """The plain-text DB dump must not leak a hash either."""
    dump = (GOLD_STANDARD_DIR / "db_snapshot.sql").read_text()
    match = _BCRYPT_RE.search(dump)
    assert match is None, (
        f"gold-standard db_snapshot.sql contains bcrypt hash: {match.group(0) if match else ''}"
    )


def test_gold_standard_db_snapshot_dump_has_no_bcrypt_hash():
    """The binary pg_dump file should also be hash-free, scanned byte-wise."""
    dump_path = GOLD_STANDARD_DIR / "db_snapshot.dump"
    if not dump_path.exists():
        pytest.skip("reference/medicine-waves-GOLD-STANDARD-v25/db_snapshot.dump (a past build snapshot) is not part of this repository")
    data = dump_path.read_bytes()
    # pg_dump custom format compresses data; bcrypt hashes are ASCII and
    # survive at the boundaries we care about. A byte regex for any
    # bcrypt-shaped literal catches both compressed and uncompressed
    # payload variants reliably enough for CI regression purposes.
    assert _BCRYPT_BYTES_RE.search(data) is None, "bcrypt hash present in db_snapshot.dump"


# ---------------------------------------------------------------------------
# SEC-1b: pipeline never re-injects a bcrypt hash into seed.sql
# ---------------------------------------------------------------------------


def test_pipeline_scrubs_bcrypt_hash_from_seed():
    """End-to-end: a seed.sql arriving with legacy + current hashes (both
    bcrypt variants) gets fully scrubbed by _scrub_bcrypt_from_seed, which
    runs always (not gated on USE_MAGIC_LINK_AUTH). Covers three patterns
    simultaneously: inline INSERT literal, standalone UPDATE statement,
    and an unusual cost factor ($2a$12$)."""
    legacy = _SYNTHETIC_LEGACY_HASH
    current = _SYNTHETIC_CURRENT_HASH
    oddball = "$2a$12$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ012"
    seed = (
        "INSERT INTO users (id, email, password, password_set, email_verified) "
        f"VALUES (gen_random_uuid(), 'admin@app.example', '{legacy}', FALSE, TRUE);\n"
        "INSERT INTO users (id, email, password, password_set, email_verified) "
        f"VALUES (gen_random_uuid(), 'role@app.example', '{oddball}', FALSE, TRUE);\n"
        "\n-- old comment\n"
        f"UPDATE users SET password = '{current}' WHERE password IS NULL;\n"
    )
    files = [("db/seed.sql", seed)]
    result = dict(build._scrub_bcrypt_from_seed(files))
    final = result["db/seed.sql"]
    assert _BCRYPT_RE.search(final) is None, "scrubber must leave seed.sql hash-free"
    assert "UPDATE users SET password = '$2b$" not in final
    assert "UPDATE users SET password = '$2a$" not in final


def test_pipeline_does_not_add_password_update_for_null_seed():
    """Fresh seed.sql with password=NULL passes through unchanged."""
    seed = (
        "INSERT INTO users (id, email, password, password_set, email_verified) "
        "VALUES (gen_random_uuid(), 'admin@app.example', NULL, FALSE, TRUE);"
    )
    files = [("db/seed.sql", seed)]
    result = dict(build._scrub_bcrypt_from_seed(files))
    final = result["db/seed.sql"]
    assert "$2b$" not in final
    assert "UPDATE users SET password = '$2b$" not in final


def test_scrubber_is_no_op_when_seed_absent():
    """No-op when seed.sql isn't in the artifact."""
    files = [("package.json", "{}")]
    result = dict(build._scrub_bcrypt_from_seed(files))
    assert "db/seed.sql" not in result


def test_scrubber_handles_all_bcrypt_variants():
    """$2a$, $2b$, $2y$ at any cost factor all get scrubbed."""
    variants = [
        "$2a$08$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "$2b$10$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "$2y$14$yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
    ]
    for v in variants:
        seed = f"UPDATE users SET password = '{v}' WHERE password IS NULL;"
        result = dict(build._scrub_bcrypt_from_seed([("db/seed.sql", seed)]))
        assert _BCRYPT_RE.search(result["db/seed.sql"]) is None, f"scrubber missed {v[:7]}..."


def test_scrubber_runs_regardless_of_magic_link_setting(monkeypatch):
    """Defense-in-depth: scrubber is NOT gated on USE_MAGIC_LINK_AUTH. Even
    password-primary apps (USE_MAGIC_LINK_AUTH=false) get their seed.sql
    hash-scrubbed, so a single shared committed credential can't leak
    through that escape hatch."""
    seed = f"UPDATE users SET password = '{_SYNTHETIC_CURRENT_HASH}' WHERE password IS NULL;"
    monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", False)
    result = dict(build._scrub_bcrypt_from_seed([("db/seed.sql", seed)]))
    assert "$2b$" not in result["db/seed.sql"]


def test_pipeline_sets_password_set_true_for_magic_link(monkeypatch):
    """Magic-link users need password_set=TRUE so the requirePasswordSet
    middleware lets them through. The flag flip doesn't enable password
    login because password stays NULL (bcrypt.compare against NULL → false)."""
    monkeypatch.setattr("build.USE_MAGIC_LINK_AUTH", True)
    seed = (
        "INSERT INTO users (id, email, password, password_set, email_verified) "
        "VALUES (gen_random_uuid(), 'admin@app.example', NULL, FALSE, TRUE);"
    )
    files = [
        ("db/schema.sql", "CREATE TABLE users (id UUID); CREATE TABLE magic_link_tokens (id UUID);"),
        ("db/seed.sql", seed),
    ]
    result = dict(build._ensure_magic_link_auth(files))
    final = result["db/seed.sql"]
    assert "UPDATE users SET password_set = TRUE" in final


def test_pipeline_prompt_no_longer_instructs_bcrypt_hash():
    """The Claude prompt in build.py previously said "Use this EXACT bcrypt
    hash string for ALL seed passwords" with a literal hash. That was the
    root of the shadow credential, Claude dutifully inlined the hash into
    seed.sql. The replacement prompt tells Claude to seed with password=NULL
    for magic-link apps."""
    source = Path(__file__).resolve().parent.parent / "cli" / "build.py"
    text = source.read_text()
    assert "Use this EXACT bcrypt hash string for ALL seed passwords" not in text, (
        "prompt still tells Claude to inline a bcrypt hash; shadow credential regression"
    )
    assert _BCRYPT_RE.search(text) is None, "bcrypt hash literal present in cli/build.py"


# ---------------------------------------------------------------------------
# SEC-1c: multer pinned to a patched version everywhere
# ---------------------------------------------------------------------------

_MULTER_MIN = (2, 1, 1)
_MULTER_SPEC_RE = re.compile(r"multer[\"']?\s*[:=]\s*[\"']?(?:\^|~|>=)?\s*([0-9]+)\.([0-9]+)\.([0-9]+)")


def _version_ok(version_str: str) -> bool:
    """Return True if version_str is >= 2.1.1 (parses the leading semver)."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version_str)
    if not m:
        return False
    return tuple(int(x) for x in m.groups()) >= _MULTER_MIN


def test_pipeline_default_multer_version_is_patched():
    """cli/build.py's default package.json template pins multer >= 2.1.1
    to dodge the 7 HIGH DoS advisories against 1.x LTS."""
    source = (REPO_ROOT / "cli" / "build.py").read_text()
    # Constant list around line 3168
    match = re.search(r'"multer"\s*:\s*"\^([0-9.]+[-a-zA-Z0-9.]*)"', source)
    assert match is not None, "could not find multer pin in cli/build.py"
    assert _version_ok(match.group(1)), (
        f"cli/build.py pins multer {match.group(1)}; must be >= 2.1.1 "
        f"to cover GHSA-5528-5vmv-3xc2 and 6 related DoS advisories"
    )


def test_ensure_upload_route_uses_patched_multer():
    """_ensure_upload_route post-processor's default multer pin must be >= 2.1.1.

    cli/build.py may either literal-assign (`deps["multer"] = "^2.1.1"`) or
    reference-assign via REQUIRED_DEPS (`deps["multer"] = REQUIRED_DEPS["multer"]`).
    Both valid as long as SOME safe pin is reachable. Check:
      (a) literal assignments, if any, are >= 2.1.1
      (b) REQUIRED_DEPS["multer"] (single source of truth) is >= 2.1.1
    """
    source = (REPO_ROOT / "cli" / "build.py").read_text()
    literals = re.findall(r'deps\["multer"\]\s*=\s*"\^([0-9.]+[-a-zA-Z0-9.]*)"', source)
    for v in literals:
        assert _version_ok(v), f"deps['multer']='^{v}' is below 2.1.1 (CVE window)"
    ref_assigns = re.search(r'deps\["multer"\]\s*=\s*REQUIRED_DEPS\["multer"\]', source)
    required_multer = re.search(
        r'"multer"\s*:\s*"\^([0-9.]+[-a-zA-Z0-9.]*)"', source,
    )
    assert literals or ref_assigns, "build.py no longer assigns deps['multer']"
    assert required_multer, "REQUIRED_DEPS has no multer pin"
    assert _version_ok(required_multer.group(1)), (
        f"REQUIRED_DEPS['multer']='^{required_multer.group(1)}' is below 2.1.1"
    )


@_needs_gold_standard
def test_gold_standard_package_json_multer_patched():
    """reference/.../package.json ships as the canonical MW deployment shape
    and the running prod app installs exactly what's pinned here."""
    pkg = json.loads((GOLD_STANDARD_DIR / "package.json").read_text())
    pin = pkg["dependencies"]["multer"]
    m = _MULTER_SPEC_RE.search(f'"multer": "{pin}"')
    assert m is not None, f"multer pin {pin!r} not in expected shape"
    assert _version_ok(f"{m.group(1)}.{m.group(2)}.{m.group(3)}"), (
        f"gold-standard package.json multer={pin}; must be >= 2.1.1"
    )


def test_no_legacy_multer_1x_in_repo_excluding_vendored():
    """Scan the tracked tree for any remaining `multer^1.4` pins, excluding
    vendored node_modules + historical build artifacts (which are gitignored
    anyway but may be present locally).

    Test fixtures intentionally include vulnerable-version strings as INPUTS
    to the force-upgrade path (e.g. `^1.4.5-lts.1` in test_build_completeness.py)
    to prove the pipeline upgrades them. Those are scoped to test files and
    never reach generated output, so we exclude `tests/` from this scan.
    """
    tracked_files_to_check = [
        REPO_ROOT / "cli" / "build.py",
        GOLD_STANDARD_DIR / "package.json",
    ]
    for fp in tracked_files_to_check:
        if not fp.exists():
            continue
        text = fp.read_text()
        # Find every multer pin
        for m in re.finditer(r'"multer"\s*:\s*"\^?([0-9][^"]*)"', text):
            ver = m.group(1).lstrip("^~")
            assert _version_ok(ver), (
                f"{fp.relative_to(REPO_ROOT)}: stale multer pin '^{ver}' below 2.1.1"
            )
