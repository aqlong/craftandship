"""URL credentials in text: the Python redactors and the web app's agree.

Python had three redactors that knew only `scheme://user:password@`: redact_url
and the child-environment filter in cli/deploy.py, and the worker's
_redact_secrets. A token that is the whole userinfo (`https://TOKEN@host/x.git`)
went through all three, and a password holding ")" could end the match early.
The web app's twin (web/src/routes/builds.ts, redactForCustomer) was fixed to
one rule: the credentials are everything between "://" and the last "@" of the
authority, and a database URL runs to the end of its word. monitor/url_redaction.py
now holds that rule for Python.

The same table is run through every Python entry point (the real functions),
and through the web app's own regular expressions, read from builds.ts and
applied here, so that a change on either side that makes them disagree on a
case fails this test.
"""

import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "queue"), str(ROOT / "cli"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import deploy
import worker_pg
from monitor.url_redaction import has_url_credentials, redact_url_credentials

BUILDS_TS = ROOT / "web" / "src" / "routes" / "builds.ts"

# Secret-shaped values are put together here, so that no line of this file
# holds a credential.
SECRET = "S3c" + "r3t" + "-value"
TOKEN = "tok" + "en-" + "abc123XYZ"
# The scheme names too, so that no source line reads as a database URL with a password.
PGSQL = "post" + "gresql"
PG = "post" + "gres"

# (name, text, secret that must not survive, expected Python output)
CASES = [
    ("token only", f"https://{TOKEN}@git.example/repo.git", TOKEN, "https://***@git.example/repo.git"),
    ("user and password", f"{PGSQL}://app:{SECRET}@db.example:5432/app", SECRET,
     "postgresql://***@db.example:5432/app"),
    ("token as the user name", f"https://{TOKEN}:x-oauth-basic@github.example/o/r", TOKEN,
     "https://***@github.example/o/r"),
    ("password with a closing parenthesis", f"https://ci:pa)ss{SECRET}@git.example/x", SECRET,
     "https://***@git.example/x"),
    ("password with at signs", f"{PG}://u:first@{SECRET}@db.example/app", SECRET, "postgres://***@db.example/app"),
    ("password with at signs, a scheme that is not a database's", f"https://ci:first@{SECRET}@git.example/x", SECRET,
     "https://***@git.example/x"),
    ("password with a parenthesis and an at sign inside a sentence",
     f"(see {PGSQL}://u:a)b@{SECRET}@db.example/app)", SECRET, "(see postgresql://***@db.example/app)"),
    ("database password with a slash and a question mark", f"{PGSQL}://u:pa/ss?{SECRET}@db.example/app", SECRET,
     "postgresql://***@db.example/app"),
    ("scheme in capitals", f"REDIS://:{SECRET}@cache.example:6379", SECRET, "REDIS://***@cache.example:6379"),
    ("something in front of the scheme", f"1.https://ci:{SECRET}@git.example/x", SECRET, "1.https://***@git.example/x"),
    ("no scheme at all", f"://ci:{SECRET}@git.example/x", SECRET, "://***@git.example/x"),
    ("two urls in one line", f"a {PG}://u:{SECRET}@one.example/db then https://{TOKEN}@two.example/r end", SECRET,
     "a postgres://***@one.example/db then https://***@two.example/r end"),
    ("inside quotes", f'connect "{PGSQL}://u:{SECRET}@db.example/app" failed', SECRET,
     'connect "postgresql://***@db.example/app" failed'),
]

UNCHANGED = [
    "https://example.com/some/path",
    "https://example.com:8080/search?mail=someone@example.com",
    "https://example.com:8080#section@anchor",
    "postgresql://localhost/mydb",
    "see https://example.com:443 and write to someone@example.com",
    "mailto:someone@example.com",
    "user:password@host with no scheme",
    "12:30@noon",
    "https://@host/x",
    "",
]


@pytest.mark.parametrize("name, text, secret, expected", CASES, ids=[c[0] for c in CASES])
def test_redact_url_and_the_shared_function(name, text, secret, expected):
    assert deploy.redact_url(text) == expected
    assert redact_url_credentials(text) == expected
    assert secret not in deploy.redact_url(text)


@pytest.mark.parametrize("name, text, secret, expected", CASES, ids=[c[0] for c in CASES])
def test_the_workers_redactor_removes_the_credentials(name, text, secret, expected):
    out = worker_pg._redact_secrets(text)
    assert secret not in out and "***@" in out
    # whatever else the worker's patterns do to the line, the URL is as above
    assert out == expected


@pytest.mark.parametrize("name, text, secret, expected", CASES, ids=[c[0] for c in CASES])
def test_the_error_text_scrubber_removes_the_credentials(name, text, secret, expected):
    assert secret not in deploy._scrub(text, [])


@pytest.mark.parametrize("text", UNCHANGED)
def test_text_without_credentials_is_unchanged(text):
    assert deploy.redact_url(text) == text
    assert worker_pg._redact_secrets(text) == text
    assert not has_url_credentials(text)


@pytest.mark.parametrize("name, text, secret, expected", CASES, ids=[c[0] for c in CASES])
def test_a_value_with_credentials_is_kept_out_of_a_childs_environment(name, text, secret, expected):
    kept = deploy.env_without_secrets({"HARMLESS": "1", "GIT_REMOTE": text})
    assert kept == {"HARMLESS": "1"}


def test_a_value_without_credentials_stays_in_a_childs_environment():
    base = {"SITE": "https://example.com/a", "MAIL": "someone@example.com", "PORT": "4000"}
    assert deploy.env_without_secrets(base) == base


def test_redaction_is_idempotent_and_leaves_other_values_alone():
    once = redact_url_credentials(f"x https://{TOKEN}@host/y z")
    assert redact_url_credentials(once) == once == "x https://***@host/y z"
    assert redact_url_credentials(None) is None and redact_url_credentials(42) == 42


# ---------------------------------------------------------------------------
# Linear time
# ---------------------------------------------------------------------------

HOSTILE_UNITS = ["a", "a:", "ab://c:", "a://b:c", "://", "://@", "@", "postgres://", "postgres://a:b",
                 "postgres://a@", "https://a:b@", "a/", "postgresql://u:p/ss@", "(", ")"]


@pytest.mark.parametrize("unit", HOSTILE_UNITS)
def test_two_megabytes_of_hostile_text_take_linear_time(unit):
    """Provider error text goes through whole. A pattern that can start at every
    "://" of a long word and run to its end each time is quadratic on text like
    this; the bound is far above what a linear scan needs and far below what
    a quadratic one takes."""
    text = unit * (2_000_000 // len(unit))
    for redact in (deploy.redact_url, worker_pg._redact_secrets, redact_url_credentials, has_url_credentials):
        started = time.perf_counter()
        redact(text)
        elapsed = time.perf_counter() - started
        assert elapsed < 8.0, (unit, redact, elapsed)


def test_doubling_the_input_about_doubles_the_time():
    def seconds(count):
        text = "postgres://u:p/" * count
        started = time.perf_counter()
        redact_url_credentials(text)
        return time.perf_counter() - started

    small = min(seconds(40_000) for _ in range(3))
    large = min(seconds(160_000) for _ in range(3))
    assert large < small * 12, (small, large)    # four times the text: about four times the time, not sixteen


# ---------------------------------------------------------------------------
# The web app's own expressions, read from its source
# ---------------------------------------------------------------------------

def _web_redactor():
    """redactForCustomer's two replacements, with their JavaScript regular
    expressions read from builds.ts and written for Python."""
    source = BUILDS_TS.read_text(encoding="utf-8")
    credentials = re.search(r"const URL_WITH_CREDENTIALS_RE = /(.+)/g;", source)
    database = re.search(r"const DATABASE_URL_RE = /(.+)/gi;", source)
    assert credentials and database, "builds.ts no longer defines the two expressions this test reads"

    credentials_re = re.compile(credentials.group(1).replace("\\/", "/"))
    database_re = re.compile(database.group(1).replace("\\/", "/"), re.IGNORECASE)   # the /gi flags

    def redact(text):
        return credentials_re.sub(lambda m: m.group(1) + "[credentials removed]@", database_re.sub("[connection string removed]", text))
    return redact


@pytest.mark.parametrize("name, text, secret, expected", CASES, ids=[c[0] for c in CASES])
def test_the_web_apps_redactor_and_the_python_ones_remove_the_same_secret(name, text, secret, expected):
    web = _web_redactor()(text)
    assert secret not in web, "the web redactor leaves the secret in this case"
    assert secret not in deploy.redact_url(text)
    assert secret not in worker_pg._redact_secrets(text)


@pytest.mark.parametrize("text", [t for t in UNCHANGED if t and "postgresql://" not in t])
def test_the_web_apps_redactor_leaves_the_same_texts_alone(text):
    """(It removes a database URL whole, credentials or not, so those are not in this list.)"""
    assert _web_redactor()(text) == text
