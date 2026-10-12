import ipaddress
import os
import shutil
import socket
import tempfile

import pytest

# Prevent test runs from sending errors to production Sentry.
# SENTRY_DSN leaks into the test environment when the dev shell sources web/.env.
os.environ.pop("SENTRY_DSN", None)

# v2-amazon-asin-staleness-postbuild-link-checker (2026-05-17): the
# external-URL linter defaults to "warn" mode in production so dead links
# (e.g. stale Amazon ASINs from a republished book) get flagged before
# deploy. In tests we don't want real HTTP HEAD requests firing against
# arbitrary URLs in fixtures, so default the env var to "skip" if the
# developer hasn't already set it. Tests that exercise the linter
# directly call it with an explicit mode= arg, bypassing the env default.
os.environ.setdefault("CAS_LINT_EXTERNAL_URLS", "skip")


# ---------------------------------------------------------------------------
# No real network from tests
#
# Python-level sockets (requests, urllib, http.client, the Anthropic SDK, ...)
# may only reach loopback during a test run: DNS lookups for any other name
# and connects to any other address raise NetworkBlockedError. A test that
# needs the network must mock it; there is no allow-list. Set
# CAS_TESTS_ALLOW_NETWORK=1 to turn the guard off for a deliberate live run.
#
# Not covered: child processes (curl, git push, npm install, a browser) and
# libpq (psycopg2 opens its own sockets in C). Those are audited by hand; the
# one test file that runs `npm install`, tests/test_v2_local_e2e.py, is opt-in.
# ---------------------------------------------------------------------------

class NetworkBlockedError(OSError):
    """Raised by the test-run network guard."""


_NET_GUARD_ENV = "CAS_TESTS_ALLOW_NETWORK"
_LOOPBACK_NAMES = frozenset({"", "localhost", "localhost.localdomain", "ip6-localhost"})
_real_socket_calls = {}


def _is_ip_literal(host) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback or host in ("0.0.0.0", "::")
    except ValueError:
        return False


_blocked_attempts = []  # (test id, target); reported at the end of the run


def _blocked(target) -> NetworkBlockedError:
    _blocked_attempts.append((os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>"), repr(target)))
    return NetworkBlockedError(
        f"tests may not use the network (tried {target!r}); mock it, or set "
        f"{_NET_GUARD_ENV}=1 for a deliberate live run"
    )


def _guarded_getaddrinfo(host, *args, **kwargs):
    # IP literals involve no DNS lookup; connect() is the checkpoint for those.
    if not _is_loopback_host(host) and not _is_ip_literal(host):
        raise _blocked(host)
    return _real_socket_calls["getaddrinfo"](host, *args, **kwargs)


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback_host(address[0]):
        raise _blocked(address)
    return _real_socket_calls["connect"](self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback_host(address[0]):
        raise _blocked(address)
    return _real_socket_calls["connect_ex"](self, address)


def _install_network_guard():
    if _real_socket_calls:
        return
    _real_socket_calls.update(
        getaddrinfo=socket.getaddrinfo,
        connect=socket.socket.connect,
        connect_ex=socket.socket.connect_ex,
    )
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


# The guard is never removed. A test can leave a thread running past the end
# of the session (a build thread blocked in a model call, for example), and a
# guard lifted at pytest_unconfigure would let that thread reach the network
# after the last test finished. It stays installed until the process exits.


@pytest.fixture(scope="session", autouse=True)
def _session_tempdir(tmp_path_factory):
    """Send tempfile.mkdtemp() / NamedTemporaryFile(delete=False) leftovers to
    a directory under pytest's own base temp, which pytest prunes (it keeps
    the last three runs). Without this, several tests leak a scratch
    directory into the system temp dir on every run."""
    scratch = tmp_path_factory.mktemp("tempfile")
    previous = tempfile.tempdir
    tempfile.tempdir = str(scratch)
    try:
        yield
    finally:
        tempfile.tempdir = previous


def pytest_terminal_summary(terminalreporter):
    """Make blocked attempts visible even when the code under test swallowed the error."""
    if not _blocked_attempts:
        return
    terminalreporter.section("network guard")
    terminalreporter.write_line(f"{len(_blocked_attempts)} network attempt(s) were blocked during this run:")
    for test_id, target in _blocked_attempts[:20]:
        terminalreporter.write_line(f"  {target}  <- {test_id}")


def pytest_configure(config):
    """Register custom markers so tests using them don't emit warnings."""
    if os.environ.get(_NET_GUARD_ENV) != "1":
        _install_network_guard()
    config.addinivalue_line(
        "markers",
        "no_review_kb_patch: skip the auto temp_kb fixture's monkeypatch of "
        "cli.failure_kb._get_review_kb_path (for tests exercising the real function)",
    )
    config.addinivalue_line(
        "markers",
        "requires_tools(*names): skipped, with the missing program named in the "
        "reason, when any of the named programs (git, node, psql, ...) is not on PATH.",
    )
    config.addinivalue_line(
        "markers",
        "v1_only: test exercises the v1 Claude-per-build pipeline (cli/build.py, "
        "post-processors, QA/design-review/fix loops, MW regression guards, BS4 "
        "contracts). The files that carry it are collected only when "
        "CAS_BUILD_ENGINE=v1 is set, so a plain `pytest` run returns the v2 "
        "test set. Inside a v1 run, `pytest -m 'not v1_only'` deselects them.",
    )
    config.addinivalue_line(
        "markers",
        "v2_integration: opt-in local end-to-end test harness for the v2 "
        "compose engine. For each compose/fixtures/sample-*.json: provisions "
        "an isolated local Postgres DB, composes the app, runs db_migrate, "
        "boots node server.js on a free port, probes routes + full magic-"
        "link auth flow. Slow (~30-60s per fixture) and requires local "
        "Postgres + node + npm; `npm install` downloads packages, so it "
        "needs access to the npm registry. Skipped by default; run explicitly via "
        "`pytest -m v2_integration tests/test_v2_local_e2e.py`.",
    )


# Unless CAS_BUILD_ENGINE=v1 is set, the v1-only test files are not even
# COLLECTED: that saves the pytest startup time (BS4 fixture loading, cli/
# module imports, sample-site regression setup) they would cost before every
# one of their tests was skipped. The list is keyed off file-name prefixes
# and must stay in sync with the `pytestmark = pytest.mark.v1_only` headers
# in tests/test_*.py.
_V1_ONLY_FILE_PREFIXES = (
    # Post-processors, Claude-driven QA/design loops, BS4 contracts
    "test_admin_model_handlers_",
    "test_b_plus_c_",
    "test_build_completeness",
    "test_build_prompt",
    "test_build_trace",
    "test_contracts_",
    "test_convert_raw_file",
    "test_design_fix",
    "test_design_review",
    "test_ensure_",
    "test_fix",             # covers test_fix.py + test_fix_nav_brand_* etc
    "test_mw_",
    "test_pipeline_bug_",
    "test_pipeline_byte_identical",
    "test_pipeline_dag",
    "test_post_processor",
    "test_qa_auth_prelude",
    "test_qa_fix",
    "test_review_respects_read_only",
    "test_rewrite_admin_router_template_switch",
    "test_safety_net_catches_known_past_regressions",
    "test_security_sec1_",   # seed_password_hash + test_mode_gate
    "test_site_name_contract",
    "test_stock_photos",
    "test_template_db_migrate",
    "test_tier_resolver",
    "test_ui_test",         # covers test_ui_test.py + test_ui_tests.py
    "test_validate",
    "test_warn_on_no_schema",
    "test_widget_runtime_",
    "test_widget_styling",
    "test_widgets_",        # widgets_forms, widgets_labels, widgets_registry, widgets_safety_net
    "test_youtube_thumb_proxy",
    # MW fixture + golden file tests (MW prod-stable since 2026-04-19)
    "test_deploy_dict",
    "test_fixtures_present",
    "test_golden_files",
)

# v2 is the default pipeline. The v1_only files are left out UNLESS the
# developer explicitly opts in with CAS_BUILD_ENGINE=v1 (any other value,
# or none, means v2).
_engine_env = (os.environ.get("CAS_BUILD_ENGINE") or "").strip().lower()
_v1_explicitly_requested = _engine_env == "v1"

if not _v1_explicitly_requested:
    # Pytest standard: module-level `collect_ignore_glob` lets us drop
    # entire test files from collection. Building the glob list from our
    # prefix set produces ~40 patterns that match the v1-only test files.
    collect_ignore_glob = [f"tests/{prefix}*.py" for prefix in _V1_ONLY_FILE_PREFIXES]


def pytest_runtest_setup(item):
    """Honour @pytest.mark.requires_tools(...): skip instead of failing with
    FileNotFoundError when a program the test shells out to is not installed."""
    for mark in item.iter_markers(name="requires_tools"):
        missing = [tool for tool in mark.args if shutil.which(tool) is None]
        if missing:
            pytest.skip("requires " + ", ".join(missing) + " on PATH")


def pytest_collection_modifyitems(config, items):
    """Skip v1_only-marked tests by default (v2 is the production
    pipeline as of 2026-04-22; default flipped 2026-04-27 so the v1
    pipeline only runs when CAS_BUILD_ENGINE=v1 is explicitly set).

    This lets a developer run `pytest tests/` and get a clean
    v2-relevant result by default. Set CAS_BUILD_ENGINE=v1 to run the
    legacy Claude-per-build test surface for repro / forensics.
    """
    engine = (os.environ.get("CAS_BUILD_ENGINE") or "").strip().lower()
    # v1_only gating: skip unless v1 explicitly requested.
    if engine != "v1":
        skip_v1 = pytest.mark.skip(
            reason=("v1-only test; skipped by default since v1 is legacy. "
                    "Set CAS_BUILD_ENGINE=v1 to opt back into the legacy "
                    "Claude-per-build pipeline test surface.")
        )
        for item in items:
            if "v1_only" in item.keywords:
                item.add_marker(skip_v1)

    # v2_integration opt-in gating: skip by default unless `-m v2_integration`
    # was passed. Running every fixture through the local Postgres + node
    # boot harness takes minutes and needs local infra; we only want it to
    # run when explicitly requested.
    markexpr = (config.getoption("-m", default="") or "")
    if "v2_integration" not in markexpr:
        skip_v2i = pytest.mark.skip(
            reason="v2_integration is opt-in; run explicitly with "
                   "`pytest -m v2_integration tests/test_v2_local_e2e.py`"
        )
        for item in items:
            if "v2_integration" in item.keywords:
                item.add_marker(skip_v2i)
