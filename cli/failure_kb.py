# cli/failure_kb.py - Build failure knowledge base
#
# Applies the support_kb auto-learning pattern to build failures.
# When a test/deploy/QA fix succeeds, we cache the error signature and
# fix description so future builds with the same error can skip the
# expensive Claude call and apply a known fix immediately.
#
# Storage: JSON file at cli/failure_kb.json (committed to repo so
# learnings persist across deploys). Each entry maps a normalized
# error signature to fix metadata.
#
# Vector similarity: When exact signature match fails, uses TF-IDF
# cosine similarity over character n-grams + word tokens to find
# semantically similar errors. Pure Python, zero external dependencies.

import json
import math
import os
import sys
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional

# monitor/ is a top-level package. The worker and the test suite already have
# the repo root on sys.path; running a cli module as a script does not.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from monitor.url_redaction import redact_url_credentials

KB_PATH = Path(__file__).parent / "failure_kb.json"

# Committed seed: shipped with the repo so a fresh Railway deploy has a
# useful KB on first write. See _get_kb_path() for the actual path used
# at read/write time (may be volume-backed on Railway).
_KB_SEED_PATH = KB_PATH

# Railway worker volume mount point. Verified via Railway API
# volumeInstances.mountPath query on 2026-04-19: the worker volume is
# mounted at /builds (absolute), NOT /app/builds. The latter is ephemeral
# container storage and resets on every deploy; that's the bug this
# fix addresses. Exposed as a module constant so tests can monkeypatch
# without needing to mock Path("/builds") directly.
_RAILWAY_VOLUME_DIR = Path("/builds")

# Minimum success count before a cached fix is auto-applied without Claude
AUTO_APPLY_THRESHOLD = 3

# Maximum entries to keep (oldest by last_seen are pruned)
MAX_ENTRIES = 200

# Sliding window size for recent outcome tracking (prevents inflated historical counts)
RECENT_WINDOW = 20

# Vector similarity threshold (0.0-1.0). Higher = stricter matching.
# 0.45 catches related errors without false positives on unrelated ones.
SIMILARITY_THRESHOLD = 0.45

# Character n-gram size for tokenization
NGRAM_SIZE = 3

# Regex patterns matching app_names that identify test / dogfood / executor-stress
# builds whose failures should NOT pollute the failure knowledge base.
#
# Origin: failure_kb audit (2026-05-04) found `QA failed: test1` (376x) and
# `QA failed: plots page renders; create plot form submits` (918x) at the top
# of the failure-count list. These signatures originate from manually-queued
# test builds with placeholder app_names + page names ("test1", "plots") whose
# Claude-driven QA fix loop never converges (because the spec is too thin to
# build a real app), and each loop iteration calls `record_fix(succeeded=False)`,
# inflating the failure_count. The signatures don't represent real failure
# patterns, so the cached "fix" hint they produce is empty and the entries
# crowd out genuine learnings.
#
# These patterns intentionally do NOT match the coordinator's automated test
# specs (`neighborhood-cms`, `recipe-meal-planner`, etc. see
# coordinator/sample_build_specs.py). Those exercise real archetypes and their
# failures are signal we want.
_TEST_BUILD_APP_NAME_PATTERNS = (
    re.compile(r"^test([-_0-9]|$)", re.IGNORECASE),
    re.compile(r"^coach-simple-bug-verify(-|$)", re.IGNORECASE),
    re.compile(r"^phase\d+-followup-", re.IGNORECASE),
    re.compile(r"^dogfood(-|$)", re.IGNORECASE),
    re.compile(r"^e2e(-|$)", re.IGNORECASE),
    re.compile(r"^plots?(-|$)", re.IGNORECASE),
)


def _is_test_build_app_name(app_name: Optional[str]) -> bool:
    """Return True when `app_name` matches a recognized test-build prefix.

    Used by `record_fix()` and `record_review_issues()` to skip recording
    failures from test / dogfood / executor-stress builds. Returns False for
    None / empty inputs so legacy callers without app_name plumbing still
    record everything (no-regression default).
    """
    if not app_name or not isinstance(app_name, str):
        return False
    name = app_name.strip()
    if not name:
        return False
    return any(p.match(name) for p in _TEST_BUILD_APP_NAME_PATTERNS)


# ---------------------------------------------------------------------------
# Deploy-crash signature extraction
# ---------------------------------------------------------------------------
#
# RCA 2026-05-04: the worker's old inline `"deploy-crash; " + last_5_logs`
# bucketing produced too-generic signatures (e.g. `deploy-crash; CRASHED`,
# `deploy-crash; crash on startup`). Distinct underlying causes (TypeError on
# undefined.id, missing module, port conflict, etc.) all bucketed under the
# same KB entry; the entry's 0% success rate then triggered the deploy_fix
# early-exit gate (line ~3047 in worker_pg.py) for every new customer build
# whose actual error happened to surface only generic status words at the tail
# of the Railway logs. Self-reinforcing: more skipped fixes -> more failures
# in the bucket -> even stronger early-exit -> even more skipped fixes.
#
# The fix is two-pronged:
#   1. `extract_deploy_crash_signature(logs)` walks logs preferring a line
#      that LOOKS like an actual error (Traceback, TypeError, ReferenceError,
#      Cannot, at /file:line, throw, ENOENT, EADDRINUSE, etc.) before
#      falling back to last-N status words.
#   2. `is_generic_crash_signature(sig)` blocks the early-exit gate from
#      firing on signatures that match a known-too-generic pattern; even if
#      the bucket says 0% success, run the fix loop because the bucket is
#      poisoned and we cannot trust the predictor.
#
# The two layers compose: most NEW failures land on better signatures (so the
# generic bucket stops growing); historical generic-bucket entries no longer
# unfairly block fix loops.

# Lines that look like actual errors. Order = priority (first match wins).
_DEPLOY_CRASH_ERROR_INDICATORS = (
    re.compile(r"\bTraceback\s*\(most recent call last\)", re.IGNORECASE),
    re.compile(r"\b(?:Type|Reference|Range|Syntax|URI|Eval)Error\b"),
    re.compile(r"\b(?:Module|Package)Not(?:Found|Resolved)Error\b"),
    re.compile(r"\bCannot find module\b", re.IGNORECASE),
    re.compile(r"\bCannot read propert(?:y|ies)\b", re.IGNORECASE),
    re.compile(r"\bUncaughtException\b", re.IGNORECASE),
    re.compile(r"\bUnhandledPromiseRejection\b", re.IGNORECASE),
    re.compile(r"\bENOENT\b|\bEADDRINUSE\b|\bECONNREFUSED\b|\bETIMEDOUT\b"),
    re.compile(r"\bSyntaxError\b|\bImportError\b|\bAttributeError\b|\bKeyError\b|\bValueError\b"),
    re.compile(r"\bthrow\s+new\s+(?:\w+Error)?", re.IGNORECASE),
    re.compile(r"\bAssertion(?:Error|Failed)\b", re.IGNORECASE),
)

# Status-only words that are NOT useful signatures by themselves.
_DEPLOY_CRASH_GENERIC_WORDS = re.compile(
    r"^(?:CRASHED|crash|crashed|failed|FAILED|exit\s*\d+|exited|killed|"
    r"OOMKilled|terminated|SIGTERM|SIGKILL|stopped|aborted)$",
    re.IGNORECASE,
)


def extract_deploy_crash_signature(logs) -> str:
    """Build a deploy-crash signature that prefers real error lines over status words.

    Walks `logs` (list of strings or anything coercible to str) looking for the
    first line matching one of `_DEPLOY_CRASH_ERROR_INDICATORS`. Returns
    `"deploy-crash; <that line, trimmed to 200 chars>"`. Falls back to the
    last 5 non-status-only lines joined when no error indicator matches; or
    `"deploy-crash; health-check-failed"` when logs are empty.

    Pure function: no I/O, no `time.time()`, no globals beyond regex constants.
    """
    if not logs:
        return "deploy-crash; health-check-failed"
    lines = [str(l).strip() for l in logs if l]
    lines = [l for l in lines if l]  # drop empties
    if not lines:
        return "deploy-crash; health-check-failed"
    # Pass 1: error-indicator preference (most-recent first, since later lines
    # are typically the actual exception while earlier ones are app-startup
    # noise).
    for line in reversed(lines):
        for pat in _DEPLOY_CRASH_ERROR_INDICATORS:
            if pat.search(line):
                return f"deploy-crash; {line[:200]}"
    # Pass 2: skip status-only words; join the most-recent non-generic lines.
    informative = [l for l in lines if not _DEPLOY_CRASH_GENERIC_WORDS.match(l)]
    if informative:
        sig_body = " ".join(informative[-3:])[:200]
        return f"deploy-crash; {sig_body}"
    # Pass 3: everything was generic status words; preserve the legacy shape
    # but flag it so callers know not to trust the bucket.
    return f"deploy-crash; {lines[-1][:60]}"


# Known-too-generic deploy-crash signatures. The early-exit gate must NOT
# fire on these even if the bucket has high failure_count + 0% success rate.
# The bucket is poisoned by historical pollution from when the old inline
# extractor produced these directly; new builds whose actual cause happens
# to surface only status words still bucket here, but they shouldn't be
# denied a fix attempt because of the bucket's history.
_GENERIC_CRASH_SIGNATURE_PATTERNS = (
    # Old inline extractor's typical outputs (crash, crashed, crashing):
    re.compile(r"^deploy-crash;\s*crash(?:ed|ing)?\s*$", re.IGNORECASE),
    re.compile(r"^deploy-crash;\s*crash\s+on\s+startup\s*$", re.IGNORECASE),
    re.compile(r"^deploy-crash;\s*health-check-failed\s*$", re.IGNORECASE),
    # Status-word-only signatures (CRASHED, FAILED, exit codes, signals):
    re.compile(
        r"^deploy-crash;\s*(?:CRASHED|crashed|FAILED|failed|"
        r"exit\s*\d+|exited|killed|OOMKilled|terminated|SIGTERM|SIGKILL)\s*$",
        re.IGNORECASE,
    ),
    # Pure log-line-N noise (Railway sometimes returns "log line 55 log line 56..."):
    re.compile(r"^deploy-crash;\s*(?:log line \d+\s*){2,}$", re.IGNORECASE),
)


def is_generic_crash_signature(sig: str) -> bool:
    """Return True when `sig` is too generic to gate the deploy_fix early-exit on.

    Generic signatures bucket distinct underlying crashes; the bucket's
    historical 0% success rate is not a trustworthy predictor for a new
    build's actual error. Callers should run the fix loop normally for
    generic signatures even when `_effective_rate(...) == (0.0, >=5)`.
    """
    if not sig or not isinstance(sig, str):
        return False
    s = sig.strip()
    if not s:
        return False
    return any(p.match(s) for p in _GENERIC_CRASH_SIGNATURE_PATTERNS)


# ---------------------------------------------------------------------------
# Vector similarity engine (pure Python TF-IDF with cosine similarity)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list:
    """Tokenize text into word tokens + character n-grams.

    Word tokens capture semantic meaning ("CSRF", "session", "redirect").
    Character n-grams capture partial matches and typo resilience.
    """
    if not text:
        return []
    text = text.lower()
    # Word tokens: split on non-alphanumeric, keep tokens 2+ chars
    words = [w for w in re.split(r'[^a-z0-9]+', text) if len(w) >= 2]
    # Prefix word tokens to avoid collision with n-grams
    word_tokens = [f"w:{w}" for w in words]
    # Character n-grams from the full text (including punctuation)
    ngrams = []
    clean = re.sub(r'\s+', ' ', text)
    for i in range(len(clean) - NGRAM_SIZE + 1):
        ngrams.append(f"n:{clean[i:i + NGRAM_SIZE]}")
    return word_tokens + ngrams


def _tf_vector(tokens: list) -> dict:
    """Compute term-frequency vector from token list.

    Uses sub-linear TF (1 + log(count)) to dampen repeated tokens.
    """
    counts = Counter(tokens)
    vec = {}
    for token, count in counts.items():
        vec[token] = 1.0 + math.log(count) if count > 0 else 0.0
    return vec


def _cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    """Compute cosine similarity between two sparse vectors (dicts)."""
    if not vec_a or not vec_b:
        return 0.0
    # Dot product over shared keys
    shared_keys = set(vec_a.keys()) & set(vec_b.keys())
    if not shared_keys:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in shared_keys)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def fuzzy_lookup(error_msg: str, threshold: float = SIMILARITY_THRESHOLD) -> Optional[tuple]:
    """Find the most similar KB entry using TF-IDF cosine similarity.

    Returns (entry, similarity_score) if a match above threshold is found,
    or None if no sufficiently similar entry exists.

    This is the fallback when exact normalized-signature matching fails.
    It catches errors like:
      - "login redirect failed on /dashboard" matching "login redirect to /home failed"
      - "EADDRINUSE :::3000" matching "EADDRINUSE :::8080" (after normalization)
      - "Cannot read property 'name' of undefined" matching "'id' of undefined"
    """
    sig = normalize_error(error_msg)
    if not sig:
        return None
    query_tokens = _tokenize(sig)
    if not query_tokens:
        return None
    query_vec = _tf_vector(query_tokens)

    kb = _load_kb()
    if not kb:
        return None

    best_score = 0.0
    best_entry = None
    for entry_sig, entry in kb.items():
        entry_tokens = _tokenize(entry_sig)
        if not entry_tokens:
            continue
        entry_vec = _tf_vector(entry_tokens)
        score = _cosine_similarity(query_vec, entry_vec)
        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score >= threshold and best_entry is not None:
        return (best_entry, best_score)
    return None


def find_similar(error_msg: str, top_k: int = 5) -> list:
    """Return top-k most similar KB entries with scores.

    Useful for diagnostics and understanding what the KB knows about
    a class of errors. No threshold filtering; returns all ranked results.
    """
    sig = normalize_error(error_msg)
    if not sig:
        return []
    query_tokens = _tokenize(sig)
    if not query_tokens:
        return []
    query_vec = _tf_vector(query_tokens)

    kb = _load_kb()
    results = []
    for entry_sig, entry in kb.items():
        entry_tokens = _tokenize(entry_sig)
        if not entry_tokens:
            continue
        entry_vec = _tf_vector(entry_tokens)
        score = _cosine_similarity(query_vec, entry_vec)
        if score > 0.0:
            results.append({"entry": entry, "similarity": round(score, 3)})
    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# KB persistence
# ---------------------------------------------------------------------------

def _get_kb_path() -> Path:
    """Resolve the failure KB path at call time.

    Precedence:
      1. `FAILURE_KB_PATH` env var: explicit override (tests + advanced ops)
      2. `RAILWAY_ENVIRONMENT` set → `<repo>/builds/failure_kb.json` (volume-mounted)
      3. Local default → `cli/failure_kb.json` (committed seed)

    On the first Railway access where the volume file doesn't yet exist,
    seeds it by copying the committed `cli/failure_kb.json`. After seed,
    the volume path is authoritative and committed file becomes a dev-only
    seed. Mirrors the `_get_review_kb_path()` pattern (same logic for
    review_kb) but adds the bootstrap step and env-var override.

    Called at every load/save, never cached, so env changes mid-process
    (e.g., tests toggling RAILWAY_ENVIRONMENT) take effect immediately.
    """
    override = os.environ.get("FAILURE_KB_PATH")
    if override:
        return Path(override)
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        # Worker volume is mounted at _RAILWAY_VOLUME_DIR (/builds). Path is
        # module-level constant so tests can monkeypatch to tmp_path without
        # needing to mock the filesystem.
        # NOTE: _get_review_kb_path() in this file still uses the (buggy)
        # Path(__file__).parent.parent / "builds" = /app/builds path; 
        # pre-existing, out of scope for this fix. See roadmap item
        # `fix-review-kb-wrong-volume-path`.
        builds_dir = _RAILWAY_VOLUME_DIR
        try:
            builds_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # volume missing is fatal for writes but load() handles gracefully
        volume_path = builds_dir / "failure_kb.json"
        # Bootstrap: copy committed seed into the volume on first access.
        if not volume_path.exists() and _KB_SEED_PATH.exists() and _KB_SEED_PATH != volume_path:
            try:
                volume_path.write_text(_KB_SEED_PATH.read_text())
            except OSError:
                pass  # non-fatal: next save will create the file
        return volume_path
    return KB_PATH


def _load_kb() -> dict:
    """Load the failure KB from disk."""
    path = _get_kb_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Serialize `data` to `path` atomically via tmp-file + os.replace.

    Guards against torn writes when the worker is killed mid-save (SIGTERM
    during Railway deploy drain, OOM, hard crash). Sequence:
      1. Serialize to `path.with_suffix(".json.tmp")`
      2. os.replace() atomically swaps it into place (POSIX rename is atomic
         across directories on the same filesystem)

    If step 1 fails: tmp file may be left behind (orphan, harmless).
    If step 1 succeeds but step 2 fails: old file intact, tmp orphaned.
    Only committed state ever visible at `path`.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
#
# Derived from a security review of the platform, which found that the KB
# persists `signature` (last N lines of build_logs) for every classified
# failure. If a future build emits an API key in stderr (e.g. an SDK error
# message printing `Authorization: Bearer sk-...`), the KB would persist that
# token indefinitely on the prod /builds volume. Apply a regex pass before
# persistence to redact known secret shapes. Patterns are intentionally
# conservative; over-redacting a non-secret is preferable to missing a
# leak.

_PEM_DASHES = "-" * 5  # built, not written out, so secret scanners do not flag this file

_SECRET_PATTERNS: tuple = (
    # Anthropic API keys (sk-ant-..., sk-ant-api03-...)
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-<REDACTED>"),
    # Stripe test secret keys
    (re.compile(r"sk_test_[A-Za-z0-9]{16,}"), "sk_test_<REDACTED>"),
    # Stripe live secret keys
    (re.compile(r"sk_live_[A-Za-z0-9]{16,}"), "sk_live_<REDACTED>"),
    # Stripe restricted keys
    (re.compile(r"rk_(?:test|live)_[A-Za-z0-9]{16,}"), "rk_<REDACTED>"),
    # Stripe publishable keys (less sensitive but still secret-shaped; redact for hygiene)
    (re.compile(r"pk_(?:test|live)_[A-Za-z0-9]{16,}"), "pk_<REDACTED>"),
    # Stripe webhook secrets
    (re.compile(r"whsec_[A-Za-z0-9]{16,}"), "whsec_<REDACTED>"),
    # Resend API keys (re_<group>_<group>, alphanumeric+underscore, 20+ chars after re_)
    (re.compile(r"\bre_[A-Za-z0-9_]{20,}"), "re_<REDACTED>"),
    # OpenAI API keys (sk-proj-..., sk-...)
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "sk-proj-<REDACTED>"),
    # GitHub personal access tokens
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "ghp_<REDACTED>"),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}"), "gho_<REDACTED>"),
    (re.compile(r"\bghs_[A-Za-z0-9]{20,}"), "ghs_<REDACTED>"),
    # Sentry user auth tokens (sntryu_...)
    (re.compile(r"sntryu_[A-Za-z0-9]{20,}"), "sntryu_<REDACTED>"),
    # AWS access keys (AKIA... + ASIA... session tokens)
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<AWS_KEY_REDACTED>"),
    # Authorization: Bearer <token>, capture the prefix, redact the token
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]{12,}"),
     r"\1<REDACTED>"),
    # Standalone Bearer prefix (e.g. in shell-style env: `Bearer xyz`)
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._\-]{20,}"), r"\1<REDACTED>"),
    # JWT-shape tokens (header.payload.signature, base64url)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
     "<JWT_REDACTED>"),
    # Credentials inside a URL (scheme://user:password@host, scheme://TOKEN@host)
    # are not here: `_redact_secrets_in_string` applies the one shared rule
    # (monitor/url_redaction.py) before this list.
    # Secret-bearing query parameters (magic-link tokens, API keys in URLs)
    (re.compile(r"(?i)([?&](?:token|access_token|api_?key|key|secret|password)=)[^&\s\"'<>]{8,}"),
     r"\1<REDACTED>"),
    # Older OpenAI keys (sk-<48 alphanumerics>); sk-ant- and sk-proj- are handled above
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "sk-<REDACTED>"),
    # Google API keys
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), "<GOOGLE_KEY_REDACTED>"),
    # Slack tokens
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "xox-<REDACTED>"),
    # PEM private key blocks (through the END line, or to the end of the string
    # when the log was truncated mid-key)
    (re.compile(_PEM_DASHES + r"BEGIN [A-Z ]*PRIV" + r"ATE KEY" + _PEM_DASHES
                + r".*?(?:" + _PEM_DASHES + r"END [A-Z ]*PRIV" + r"ATE KEY" + _PEM_DASHES + r"|\Z)", re.S),
     "<PRIVATE_KEY_REDACTED>"),
)


def _redact_secrets_in_string(value: str) -> str:
    """Remove the credentials of URLs, then apply all _SECRET_PATTERNS to a
    string; return redacted copy.

    Idempotent: re-running on already-redacted text is a no-op because the
    placeholder tokens don't match any pattern.
    """
    if not value or not isinstance(value, str):
        return value
    out = redact_url_credentials(value)
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_text(value: str) -> str:
    """Redact secret-shaped substrings in one piece of text.

    Public entry point for callers outside the knowledge base: the fix loops
    and monitor.diagnose pass log text from a customer app through this before
    it goes into a model prompt (a third party) or an email.
    """
    return _redact_secrets_in_string(value)


def _redact_secrets(data):
    """Recursively walk JSON-shaped data and redact secrets in every string.

    Handles dicts, lists, tuples, and scalars. Non-string scalars
    (bools, ints, floats, None) pass through unchanged. The original
    structure is not mutated; a new structure is returned.
    """
    if isinstance(data, str):
        return _redact_secrets_in_string(data)
    if isinstance(data, dict):
        # Redact both keys (failure_kb uses signatures as keys; a leaked
        # token in stderr ends up in the signature string itself) and values.
        return {
            (_redact_secrets_in_string(k) if isinstance(k, str) else k):
                _redact_secrets(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact_secrets(v) for v in data]
    if isinstance(data, tuple):
        return tuple(_redact_secrets(v) for v in data)
    return data


def _save_kb(kb: dict) -> None:
    """Save the failure KB to disk. Prunes if over MAX_ENTRIES.

    Applies _redact_secrets() across the entire KB structure (signatures
    + entry bodies) before persisting so no API key or bearer token ever
    lands on the prod /builds volume.
    """
    if len(kb) > MAX_ENTRIES:
        # Keep entries sorted by last_seen (most recent first)
        sorted_keys = sorted(
            kb.keys(),
            key=lambda k: kb[k].get("last_seen", 0),
            reverse=True,
        )
        kb = {k: kb[k] for k in sorted_keys[:MAX_ENTRIES]}
    safe_kb = _redact_secrets(kb)
    try:
        _atomic_write_json(_get_kb_path(), safe_kb)
    except OSError as exc:
        # Non-fatal: the KB is a cache, not critical state. Say so instead of
        # failing silently, so a read-only or full volume is visible in logs.
        print(f"failure_kb: could not save {_get_kb_path()}: {exc}", file=sys.stderr)


def normalize_error(error_msg: str) -> str:
    """Normalize an error message into a stable signature.

    Strips variable parts (UUIDs, timestamps, ports, paths, specific
    values) so the same class of error always produces the same key.
    """
    if not error_msg:
        return ""
    sig = error_msg.strip()
    # Strip ANSI escape codes
    sig = re.sub(r'\x1b\[[0-9;]*m', '', sig)
    # Replace UUIDs
    sig = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', sig)
    # Replace port numbers
    sig = re.sub(r'localhost:\d+', 'localhost:<PORT>', sig)
    # Replace file paths
    sig = re.sub(r'/[^\s:]+\.(js|ejs|css|json|sql)', '<FILE>', sig)
    # Replace line:col references
    sig = re.sub(r':\d+:\d+', ':<LINE>:<COL>', sig)
    # Replace specific numbers in error context
    sig = re.sub(r'got (\d+)', 'got <N>', sig)
    # Collapse whitespace
    sig = re.sub(r'\s+', ' ', sig).strip()
    # Reject signatures that are too short to be meaningful (e.g. "error", "failed")
    # These act as catch-alls and produce useless hints that pollute the KB.
    if len(sig) < 10:
        return ""
    # Truncate to keep signatures manageable
    return sig[:300]


def lookup(error_msg: str, use_fuzzy: bool = True) -> Optional[dict]:
    """Look up a cached fix for a given error message.

    Tries exact normalized-signature match first. If that fails and
    use_fuzzy is True, falls back to vector similarity search.

    Returns the KB entry if found, or None if no match.
    """
    sig = normalize_error(error_msg)
    if not sig:
        return None
    kb = _load_kb()
    entry = kb.get(sig)
    if entry:
        return entry
    # Fallback: vector similarity search
    if use_fuzzy:
        result = fuzzy_lookup(error_msg)
        if result:
            return result[0]
    return None


def record_fix(error_msg: str, fix_description: str, files_changed: list,
               fix_category: str = "unknown", succeeded: bool = True,
               app_name: Optional[str] = None) -> None:
    """Record a fix outcome in the knowledge base.

    Called after fix.py, deploy_fix.py, or qa_fix.py successfully (or
    unsuccessfully) applies a fix. Over time, entries with high success
    rates become candidates for auto-application.

    Args:
        error_msg: The original error message that triggered the fix.
        fix_description: What Claude did to fix it.
        files_changed: List of file paths that were modified.
        fix_category: Classification (auth, session, csrf, seed, crud, etc.)
        succeeded: Whether the fix actually resolved the error.
        app_name: Optional originating build app_name. When it matches a
            known test/dogfood/executor-stress prefix the call is silently
            skipped so noise from placeholder specs (e.g. `test1`, `plots`)
            doesn't dominate the KB.
    """
    if _is_test_build_app_name(app_name):
        return
    sig = normalize_error(error_msg)
    if not sig:
        return
    kb = _load_kb()
    entry = kb.get(sig, {
        "signature": sig,
        "fix_description": "",
        "files_changed": [],
        "fix_category": "unknown",
        "success_count": 0,
        "failure_count": 0,
        "first_seen": time.time(),
        "last_seen": 0,
    })
    entry["last_seen"] = time.time()
    if succeeded:
        entry["success_count"] = entry.get("success_count", 0) + 1
        # Update fix description with most recent successful fix
        entry["fix_description"] = fix_description[:500]
        entry["files_changed"] = files_changed[:10]
        entry["fix_category"] = fix_category
    else:
        entry["failure_count"] = entry.get("failure_count", 0) + 1
    # Maintain a sliding window of recent outcomes for accurate rate calculation.
    # This prevents inflated historical counts from dominating success rate.
    recent = entry.get("recent_outcomes", [])
    recent.append(bool(succeeded))
    entry["recent_outcomes"] = recent[-RECENT_WINDOW:]  # keep last 20
    kb[sig] = entry
    _save_kb(kb)


def _effective_rate(entry: dict) -> tuple:
    """Return (success_rate, total) using recent_outcomes window when available.

    Uses the sliding window (last 20 outcomes) when it has 5+ entries for
    accurate signal; falls back to aggregate counts for sparse entries.
    """
    recent = entry.get("recent_outcomes", [])
    if len(recent) >= 5:
        rate = sum(recent) / len(recent)
        return rate, len(recent)
    success = entry.get("success_count", 0)
    failure = entry.get("failure_count", 0)
    total = success + failure
    rate = (success / total) if total > 0 else 0.0
    return rate, total


def get_prompt_hints(error_msg: str) -> str:
    """Get prompt enhancement hints based on past fix patterns.

    If we've seen this error before and have a high-confidence fix,
    returns a string that can be injected into the fix prompt to
    guide Claude toward the known solution.

    Tries exact match first, then fuzzy similarity. Fuzzy matches
    include the similarity score so Claude can weigh confidence.

    Returns empty string if no relevant hints found.
    """
    # Try exact match first
    sig = normalize_error(error_msg)
    exact_entry = None
    if sig:
        kb = _load_kb()
        exact_entry = kb.get(sig)

    # Determine match type and entry
    match_type = "exact"
    similarity = 1.0
    entry = exact_entry
    if not entry:
        result = fuzzy_lookup(error_msg)
        if result:
            entry, similarity = result
            match_type = "fuzzy"

    if not entry:
        return ""
    success_rate, total = _effective_rate(entry)
    if total == 0:
        return ""
    if success_rate < 0.5 or total < 2:
        return ""

    hint = f"\n[KNOWN FIX PATTERN - {success_rate:.0%} success rate over {total} occurrences"
    if match_type == "fuzzy":
        hint += f", {similarity:.0%} similarity match"
    hint += "]\n"
    hint += f"Category: {entry.get('fix_category', 'unknown')}\n"
    hint += f"Previously resolved by: {entry.get('fix_description', 'N/A')}\n"
    hint += f"Files typically involved: {', '.join(entry.get('files_changed', []))}\n"
    return hint


def get_build_prompt_rules() -> str:
    """Generate additional build prompt rules from high-confidence KB entries.

    Returns rules that should be injected into the build system prompt
    to prevent known failure patterns from recurring.

    Two sources:
    - Machine-learned entries: must meet AUTO_APPLY_THRESHOLD + 70% success rate
    - Seeded entries (seeded=True): bypass threshold, human-curated rules always inject
    """
    kb = _load_kb()
    rules = []
    for sig, entry in kb.items():
        category = entry.get("fix_category", "unknown")
        desc = entry.get("fix_description", "")
        if not desc:
            continue
        if entry.get("seeded"):
            # Human-curated entries always inject regardless of outcome counts
            rules.append(f"- [{category}] {desc}")
            continue
        success_rate, total = _effective_rate(entry)
        if total < AUTO_APPLY_THRESHOLD:
            continue
        if success_rate < 0.7:
            continue
        rules.append(f"- [{category}] {desc}")
    if not rules:
        return ""
    header = "\n\n## Learned rules from past build failures\n\n"
    header += "The following patterns have been identified from previous builds. "
    header += "Apply these rules proactively to avoid known failure modes:\n\n"
    return header + "\n".join(rules[:20])  # Cap at 20 rules


REVIEW_KB_PATH = Path(__file__).parent / "review_kb.json"
_REVIEW_KB_SEED_PATH = REVIEW_KB_PATH


def _get_review_kb_path() -> Path:
    """Resolve review_kb.json path: Railway volume when deployed, cli/ dir locally.

    Mirrors _get_kb_path() exactly: env override takes precedence, Railway
    uses the volume mount (/builds, not /app/builds; bug-fixed 2026-04-19),
    and local dev uses the committed seed under cli/.

    On first Railway access, seeds the volume from the committed file so a
    fresh deploy still benefits from curated review rules.
    """
    override = os.environ.get("REVIEW_KB_PATH")
    if override:
        return Path(override)
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        builds_dir = _RAILWAY_VOLUME_DIR
        try:
            builds_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        volume_path = builds_dir / "review_kb.json"
        if (not volume_path.exists()
                and _REVIEW_KB_SEED_PATH.exists()
                and _REVIEW_KB_SEED_PATH != volume_path):
            try:
                volume_path.write_text(_REVIEW_KB_SEED_PATH.read_text())
            except OSError:
                pass
        return volume_path
    return REVIEW_KB_PATH


def _load_review_kb() -> dict:
    path = _get_review_kb_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_review_kb(kb: dict) -> None:
    if len(kb) > MAX_ENTRIES:
        sorted_keys = sorted(kb.keys(), key=lambda k: kb[k].get("count", 0), reverse=True)
        kb = {k: kb[k] for k in sorted_keys[:MAX_ENTRIES]}
    safe_kb = _redact_secrets(kb)
    try:
        _atomic_write_json(_get_review_kb_path(), safe_kb)
    except OSError:
        pass


def record_review_issues(issues: list, app_name: Optional[str] = None) -> None:
    """Record critical review issues so recurring patterns become build rules.

    Called by worker_pg.py after review_build() finds critical issues.
    Each issue dict has: severity, file, issue.

    `app_name` (when provided) is checked against the same test-build prefix
    list as `record_fix()`; matches are silently skipped so review_kb stays
    free of test/dogfood noise.
    """
    if not issues:
        return
    if _is_test_build_app_name(app_name):
        return
    kb = _load_review_kb()
    for issue in issues:
        if issue.get("severity") != "critical":
            continue
        desc = issue.get("issue", "").strip()
        if not desc:
            continue
        # Normalize: strip specific file names and app names to find patterns
        sig = re.sub(r'routes/[a-z_-]+\.js', 'routes/<SLUG>.js', desc)
        sig = re.sub(r'views/[a-z_-]+/', 'views/<SLUG>/', sig)
        sig = re.sub(r'/[a-z_-]+/(new|edit|list)', '/<SLUG>/\\1', sig)
        sig = sig[:200]
        entry = kb.get(sig, {"description": desc, "count": 0, "first_seen": time.time()})
        entry["count"] = entry.get("count", 0) + 1
        entry["last_seen"] = time.time()
        entry["description"] = desc  # keep the latest specific description
        kb[sig] = entry
    _save_review_kb(kb)


def get_review_rules(min_count: int = 2) -> str:
    """Generate build prompt rules from recurring review issues.

    Patterns seen in 2+ builds become proactive rules in the build prompt.
    """
    kb = _load_review_kb()
    rules = []
    for sig, entry in kb.items():
        if entry.get("count", 0) >= min_count:
            desc = entry.get("description", sig)
            rules.append(f"- REVIEW FINDING (seen {entry['count']}x): {desc}")
    if not rules:
        return ""
    header = "\n\n## Recurring code review issues\n\n"
    header += "These issues have been flagged in multiple past builds. Avoid them:\n\n"
    return header + "\n".join(rules[:15])


def get_stats() -> dict:
    """Return summary statistics about the failure KB."""
    kb = _load_kb()
    if not kb:
        return {"total_entries": 0, "auto_applicable": 0, "categories": {}}
    categories = {}
    auto_applicable = 0
    for entry in kb.values():
        cat = entry.get("fix_category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
        success_rate, total = _effective_rate(entry)
        if total >= AUTO_APPLY_THRESHOLD and success_rate >= 0.7:
            auto_applicable += 1
    return {
        "total_entries": len(kb),
        "auto_applicable": auto_applicable,
        "categories": categories,
    }
