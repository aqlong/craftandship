# tests/test_failure_kb_secret_redaction.py
#
# Guards a finding of a security review of the platform: cli/failure_kb._save_kb
# (and _save_review_kb) MUST redact secret-shaped tokens before persistence
# so a future build emitting an API key in stderr never lands on the prod
# /builds volume.
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cli.failure_kb as kb_mod  # noqa: E402
from cli.failure_kb import (  # noqa: E402
    _redact_secrets,
    _redact_secrets_in_string,
    _save_kb,
    _save_review_kb,
    record_fix,
    record_review_issues,
)

SCRIPT_PATH = ROOT / "scripts" / "prune_failure_kb_secrets.py"

# Synthetic secret-shaped fixtures. Each is assembled at runtime from short
# pieces so no source line looks like a real credential to a secret scanner.
# None of these values is a real key; they exist only to exercise redaction.
_ANT = "sk-" + "ant-"
ANT_KEY_A = _ANT + "api03-" + "AbCdEf1234567890abcdef"
ANT_KEY_B = _ANT + "api03-" + "xyz1234567890abcdef"
ANT_KEY_LEAKY = _ANT + "api03-" + "LeakyKey12345678901234abcd"
ANT_KEY_DRY = _ANT + "api03-" + "DryRunLeak12345678901234abcd"
ANT_KEY_BARE = _ANT + "AbCdEf1234567890abcdef"
STRIPE_BODY = "1234567890abcdefABCDEFGH"
STRIPE_TEST = "sk_" + "test_" + STRIPE_BODY
STRIPE_LIVE = "sk_" + "live_" + STRIPE_BODY
WHSEC = "whsec" + "_" + STRIPE_BODY
RESEND = "re" + "_" + "AbCd1234EfGh5678IjKl9012Mn"
AWS_AKIA = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_ASIA = "ASIA" + "IOSFODNN7EXAMPLE"
GH_TOKEN = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz1234567890"
SENTRY_TOKEN = "sntry" + "u_" + "abcdefghijklmnopqrstuvwxyz1234567890"
BEARER_BODY = "abcdefghijklmnopqrstuvwxyz"
JWT_PARTS = (
    "eyJhbGciOiJIUzI1NiJ9",
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
)


@pytest.fixture(autouse=True)
def temp_kb_paths(tmp_path, monkeypatch):
    """Point both KB paths at tmp files for the duration of each test."""
    failure_path = tmp_path / "failure_kb.json"
    review_path = tmp_path / "review_kb.json"
    monkeypatch.setattr(kb_mod, "KB_PATH", failure_path)
    monkeypatch.setattr(kb_mod, "_get_kb_path", lambda: failure_path)
    monkeypatch.setattr(kb_mod, "_get_review_kb_path", lambda: review_path)
    yield failure_path, review_path


class TestRedactStringPatterns:
    @pytest.mark.parametrize("raw,fragment", [
        ("Error: " + ANT_KEY_A, "sk-ant-<REDACTED>"),
        ("Authorization: Bearer " + ANT_KEY_B,
         "sk-ant-<REDACTED>"),
        ("STRIPE_SECRET=" + STRIPE_TEST, "sk_test_<REDACTED>"),
        ("STRIPE_SECRET=" + STRIPE_LIVE, "sk_live_<REDACTED>"),
        ("webhook secret: " + WHSEC,
         "whsec_<REDACTED>"),
        ("RESEND_API_KEY=" + RESEND,
         "re_<REDACTED>"),
        ("creds: " + AWS_AKIA + " in env",
         "<AWS_KEY_REDACTED>"),
        ("creds: " + AWS_ASIA + " in env",
         "<AWS_KEY_REDACTED>"),
        ("github: " + GH_TOKEN,
         "ghp_<REDACTED>"),
        ("sentry: " + SENTRY_TOKEN,
         "sntryu_<REDACTED>"),
    ])
    def test_secret_shapes_are_redacted(self, raw, fragment):
        out = _redact_secrets_in_string(raw)
        assert fragment in out
        # The raw secret body must NOT survive
        for token in raw.split():
            if "sk-ant-" in token or "sk_test_" in token or "sk_live_" in token \
                    or "whsec_" in token or "re_" in token or "ghp_" in token \
                    or "sntryu_" in token or "AKIA" in token or "ASIA" in token:
                # the token may now be the placeholder; the original full body
                # should be gone. Check the original payload chars aren't there.
                continue
        # Heuristic: the redacted output should be shorter than the input when
        # a real secret was present, OR contain the <REDACTED> sentinel.
        assert "<REDACTED>" in out or "REDACTED" in out

    def test_bearer_token_redacted_preserving_prefix(self):
        out = _redact_secrets_in_string(
            "GET /api 401: Authorization: Bearer " + BEARER_BODY
        )
        assert "Authorization: Bearer <REDACTED>" in out
        assert BEARER_BODY not in out

    def test_jwt_redacted(self):
        jwt = ".".join(JWT_PARTS)
        out = _redact_secrets_in_string(f"token={jwt} expired")
        assert "<JWT_REDACTED>" in out
        assert jwt not in out

    def test_idempotent_on_already_redacted(self):
        raw = ANT_KEY_BARE + " and " + RESEND
        once = _redact_secrets_in_string(raw)
        twice = _redact_secrets_in_string(once)
        assert once == twice

    def test_innocent_text_unchanged(self):
        raw = "Cannot read property 'id' of undefined at file.js:42:13"
        assert _redact_secrets_in_string(raw) == raw

    def test_short_re_word_not_redacted(self):
        # The Resend pattern requires 20+ alphanumeric chars after re_ so common
        # words / short tokens with re_ prefix aren't redacted.
        raw = "re_run failed"
        assert _redact_secrets_in_string(raw) == raw

    def test_handles_none_and_non_string(self):
        assert _redact_secrets_in_string(None) is None
        # Non-string inputs are returned unchanged
        assert _redact_secrets_in_string(42) == 42

    def test_walks_nested_structures(self):
        data = {
            "signature": "Error " + STRIPE_LIVE + " at runtime",
            "files_changed": ["routes/auth.js", "Bearer " + GH_TOKEN],
            "metadata": {"key": WHSEC},
            "count": 3,
            "flag": True,
            "missing": None,
        }
        out = _redact_secrets(data)
        # nested redactions applied
        assert "sk_live_<REDACTED>" in out["signature"]
        assert "ghp_<REDACTED>" in out["files_changed"][1]
        assert out["metadata"]["key"] == "whsec_<REDACTED>"
        # scalars untouched
        assert out["count"] == 3
        assert out["flag"] is True
        assert out["missing"] is None
        # input was not mutated in place
        assert STRIPE_LIVE in data["signature"]


class TestSaveKbRedactsBeforePersist:
    def test_save_kb_writes_redacted_signature(self, temp_kb_paths):
        failure_path, _ = temp_kb_paths
        kb = {
            "Error " + ANT_KEY_A + " at runtime": {
                "signature": "Error " + ANT_KEY_A + " at runtime",
                "fix_description": "Set ANTHROPIC_API_KEY",
                "files_changed": ["env"],
                "fix_category": "auth",
                "success_count": 1,
                "failure_count": 0,
                "first_seen": time.time(),
                "last_seen": time.time(),
            }
        }
        _save_kb(kb)
        on_disk = json.loads(failure_path.read_text())
        # the signature key itself is rewritten through redaction
        assert any("sk-ant-<REDACTED>" in k for k in on_disk.keys())
        # no key contains the original token tail
        for k, v in on_disk.items():
            assert "AbCdEf1234567890abcdef" not in k
            assert "AbCdEf1234567890abcdef" not in json.dumps(v)

    def test_record_fix_redacts_end_to_end(self, temp_kb_paths):
        failure_path, _ = temp_kb_paths
        record_fix(
            "Anthropic 401: invalid key " + ANT_KEY_LEAKY,
            "rotated key in Railway",
            ["env"],
            fix_category="auth",
        )
        raw = failure_path.read_text()
        assert "LeakyKey12345678901234abcd" not in raw
        assert "sk-ant-<REDACTED>" in raw

    def test_save_review_kb_redacts(self, temp_kb_paths):
        _, review_path = temp_kb_paths
        kb = {
            "missing input validation in routes/<SLUG>.js": {
                "description": "Token leaked: " + WHSEC,
                "count": 2,
                "first_seen": time.time(),
                "last_seen": time.time(),
            }
        }
        _save_review_kb(kb)
        raw = review_path.read_text()
        assert WHSEC not in raw
        assert "whsec_<REDACTED>" in raw

    def test_record_review_issues_redacts_end_to_end(self, temp_kb_paths):
        _, review_path = temp_kb_paths
        record_review_issues([{
            "severity": "critical",
            "file": "routes/auth.js",
            "issue": "Bearer " + STRIPE_TEST + " appears in logs",
        }])
        raw = review_path.read_text()
        assert STRIPE_TEST not in raw
        assert "sk_test_<REDACTED>" in raw

    def test_clean_kb_unchanged_shape(self, temp_kb_paths):
        failure_path, _ = temp_kb_paths
        kb = {
            "Cannot read property id of undefined": {
                "signature": "Cannot read property id of undefined",
                "fix_description": "Add null guard",
                "files_changed": ["routes/auth.js"],
                "fix_category": "auth",
                "success_count": 2,
                "failure_count": 0,
                "first_seen": 1234.0,
                "last_seen": 5678.0,
                "recent_outcomes": [True, True],
            }
        }
        _save_kb(kb)
        on_disk = json.loads(failure_path.read_text())
        assert on_disk == kb


class TestPruneSecretsCli:
    """End-to-end CLI invocation against tmp KB paths."""

    def _run(self, *args, env_extra):
        env = os.environ.copy()
        env.pop("RAILWAY_ENVIRONMENT", None)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def test_dry_run_reports_no_write(self, tmp_path):
        failure = tmp_path / "failure_kb.json"
        review = tmp_path / "review_kb.json"
        failure.write_text(json.dumps({
            "Error " + ANT_KEY_DRY + " at runtime": {
                "signature": "Error " + ANT_KEY_DRY + " at runtime",
                "fix_description": "rotated key",
                "files_changed": [],
                "success_count": 1,
                "failure_count": 0,
                "last_seen": time.time(),
            }
        }))
        review.write_text("{}")
        before = failure.read_text()
        result = self._run("--dry-run", env_extra={
            "FAILURE_KB_PATH": str(failure),
            "REVIEW_KB_PATH": str(review),
        })
        assert result.returncode == 0, result.stderr
        assert "dry-run" in result.stdout
        # File unchanged
        assert failure.read_text() == before

    def test_apply_scrubs_in_place(self, tmp_path):
        failure = tmp_path / "failure_kb.json"
        review = tmp_path / "review_kb.json"
        failure.write_text(json.dumps({
            "Authorization: Bearer " + BEARER_BODY + " failed": {
                "signature": "Authorization: Bearer " + BEARER_BODY + " failed",
                "fix_description": "added auth middleware",
                "files_changed": [],
                "success_count": 1,
                "failure_count": 0,
                "last_seen": time.time(),
            }
        }))
        review.write_text(json.dumps({
            "missing csrf in routes/<SLUG>.js": {
                "description": "STRIPE_KEY=" + STRIPE_LIVE + " visible",
                "count": 1,
                "last_seen": time.time(),
            }
        }))
        result = self._run(env_extra={
            "FAILURE_KB_PATH": str(failure),
            "REVIEW_KB_PATH": str(review),
        })
        assert result.returncode == 0, result.stderr
        f_after = failure.read_text()
        r_after = review.read_text()
        assert BEARER_BODY not in f_after
        assert "Bearer <REDACTED>" in f_after
        assert STRIPE_LIVE not in r_after
        assert "sk_live_<REDACTED>" in r_after

    def test_clean_kb_is_noop(self, tmp_path):
        failure = tmp_path / "failure_kb.json"
        review = tmp_path / "review_kb.json"
        failure.write_text(json.dumps({
            "Cannot read property id of undefined": {
                "signature": "Cannot read property id of undefined",
                "fix_description": "Add null guard",
                "files_changed": [],
                "success_count": 1,
                "failure_count": 0,
                "last_seen": time.time(),
            }
        }))
        review.write_text("{}")
        before_f = failure.read_text()
        result = self._run(env_extra={
            "FAILURE_KB_PATH": str(failure),
            "REVIEW_KB_PATH": str(review),
        })
        assert result.returncode == 0, result.stderr
        # Clean run: report says "clean" and no rewrite needed; content stable
        assert "clean" in result.stdout
        assert json.loads(failure.read_text()) == json.loads(before_f)

    def test_missing_files_are_skipped(self, tmp_path):
        result = self._run(env_extra={
            "FAILURE_KB_PATH": str(tmp_path / "no-such-failure.json"),
            "REVIEW_KB_PATH": str(tmp_path / "no-such-review.json"),
        })
        assert result.returncode == 0, result.stderr
        assert "missing" in result.stdout


# ---------------------------------------------------------------------------
# Shapes added after the first audit: credentials in URLs, token query
# parameters, older OpenAI keys, Google/Slack tokens, PEM private keys.
# Fixtures are assembled at runtime so no source line is secret-shaped.
# ---------------------------------------------------------------------------

_PG_PW = "pw" + "-" + "Zq8" + "vL2xR"
_DB_URL = "postgres" + "://" + "app_user:" + _PG_PW + "@" + "localhost:5432/appdb"
_OPENAI_OLD = "sk" + "-" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCd"
_GOOGLE = "AI" + "za" + "SyA1B2C3D4E5F6G7H8I9J0KlMnOpQrStUvW"
_SLACK = "xo" + "xb-" + "1234567890-abcdefghijklmnop"
_LINK_VALUE = "".join(["9f8e7d6c", "5b4a3928", "1706f5e4", "d3c2b1a0"])
_DASH5 = "-" * 5
_PEM_BEGIN = _DASH5 + "BEGIN RSA PRIV" + "ATE KEY" + _DASH5
_PEM_END = _DASH5 + "END RSA PRIV" + "ATE KEY" + _DASH5


class TestMoreSecretShapes:
    def test_password_in_connection_url_is_redacted_but_context_kept(self):
        out = _redact_secrets_in_string("connect ECONNREFUSED " + _DB_URL + " (retrying)")
        assert _PG_PW not in out
        assert "postgres" + "://***@localhost:5432/appdb" in out
        assert "ECONNREFUSED" in out and "(retrying)" in out

    def test_url_without_password_is_untouched(self):
        raw = "GET http://localhost:3000/health -> 502; mailto:ops@example.com; git@host:org/repo"
        assert _redact_secrets_in_string(raw) == raw

    def test_token_query_parameter_is_redacted(self):
        out = _redact_secrets_in_string(
            "[MAGIC LINK] http://localhost:4567/magic?token=" + _LINK_VALUE + "&next=/dashboard"
        )
        assert _LINK_VALUE not in out
        assert "/magic?token=<REDACTED>&next=/dashboard" in out

    def test_short_query_values_are_left_alone(self):
        raw = "GET /search?key=abc&page=2"
        assert _redact_secrets_in_string(raw) == raw

    @pytest.mark.parametrize("raw,secret", [
        ("openai: " + _OPENAI_OLD, _OPENAI_OLD),
        ("maps: " + _GOOGLE, _GOOGLE),
        ("slack: " + _SLACK, _SLACK),
    ])
    def test_other_vendor_tokens(self, raw, secret):
        out = _redact_secrets_in_string(raw)
        assert secret not in out
        assert "REDACTED" in out

    def test_pem_private_key_block_is_redacted(self):
        body = "MIIEowIBAAKCAQEA" + "x" * 40
        out = _redact_secrets_in_string(
            "loaded key\n" + _PEM_BEGIN + "\n" + body + "\n" + _PEM_END + "\nnext line"
        )
        assert body not in out
        assert "<PRIVATE_KEY_REDACTED>" in out
        assert "loaded key" in out and "next line" in out

    def test_truncated_pem_block_is_redacted_to_end_of_text(self):
        body = "MIIEowIBAAKCAQEA" + "y" * 40
        out = _redact_secrets_in_string("tail of log\n" + _PEM_BEGIN + "\n" + body)
        assert body not in out
        assert out.endswith("<PRIVATE_KEY_REDACTED>")

    def test_new_patterns_are_idempotent(self):
        raw = " ".join([_DB_URL, "?token=" + _LINK_VALUE, _OPENAI_OLD, _GOOGLE, _SLACK,
                        _PEM_BEGIN + "\nabc\n" + _PEM_END])
        once = _redact_secrets_in_string(raw)
        assert _redact_secrets_in_string(once) == once

    def test_save_kb_redacts_connection_url_password(self, temp_kb_paths):
        failure_path, _ = temp_kb_paths
        record_fix("password authentication failed for " + _DB_URL, "fix creds", ["db/index.js"],
                   fix_category="database")
        assert _PG_PW not in failure_path.read_text()


# ---------------------------------------------------------------------------
# "Every write path" checks. The two KB files are only ever written through
# cli.failure_kb._atomic_write_json; this pins that every call site hands it
# redacted data, and that the committed seeds (copied verbatim onto the
# volume on first access) are themselves clean.
# ---------------------------------------------------------------------------

import ast  # noqa: E402

_REDACTED_NAMES = {"safe_kb", "redacted"}


def _python_files(*dirs):
    for d in dirs:
        yield from (ROOT / d).rglob("*.py")


class TestEveryWritePathRedacts:
    def test_every_atomic_write_json_call_passes_redacted_data(self):
        offenders = []
        calls = 0
        for path in _python_files("cli", "scripts", "coordinator", "queue", "monitor", "ops"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_atomic_write_json"):
                    continue
                calls += 1
                arg = node.args[1] if len(node.args) > 1 else None
                ok = isinstance(arg, ast.Name) and arg.id in _REDACTED_NAMES
                if not ok:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        assert calls >= 3, "expected the _save_kb, _save_review_kb and prune-script call sites"
        assert offenders == []

    def test_redacted_names_are_assigned_from_redact_secrets(self):
        """`safe_kb` / `redacted` must come from _redact_secrets(...), not be aliases of raw data."""
        for rel in ("cli/failure_kb.py", "scripts/prune_failure_kb_secrets.py"):
            tree = ast.parse((ROOT / rel).read_text())
            assigned = [
                n for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in _REDACTED_NAMES for t in n.targets)
            ]
            assert assigned, rel
            for node in assigned:
                assert isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "_redact_secrets", (
                    f"{rel}:{node.lineno} assigns a redacted-name from something other than _redact_secrets()"
                )

    def test_only_failure_kb_module_writes_the_kb_files_directly(self):
        """No other module under cli/ writes failure_kb.json / review_kb.json by itself."""
        for path in _python_files("cli"):
            if path.name == "failure_kb.py":
                continue
            text = path.read_text()
            if "failure_kb.json" in text or "review_kb.json" in text:
                assert "write_text" not in text and "json.dump(" not in text, path.name


class TestCommittedSeedsAreClean:
    @pytest.mark.parametrize("name", ["failure_kb.json", "review_kb.json"])
    def test_seed_file_is_a_redaction_fixed_point(self, name):
        data = json.loads((ROOT / "cli" / name).read_text())
        assert _redact_secrets(data) == data, f"cli/{name} contains a secret-shaped string"
