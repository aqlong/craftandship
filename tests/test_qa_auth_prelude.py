"""Regression tests for the pipeline-injected _cas_login_as_admin auth
prelude in cli/qa.py.

Root cause (detected v21+v22, 2026-04-18): Claude-generated qa_tests.py
used `admin@example.com` + password auth, but MW uses magic-link auth
with `e2e@craftandship.example`. QA self-correction loop attempted to
fix artifact based on false-positive test failures, sometimes
worsening it before rollback.

Fix: cli/qa.py::generate_qa_tests now prepends a fixed auth helper
(_cas_login_as_admin) to the returned code. The helper mints a
magic-link token via psql and visits /magic?token=X. QA prompt was
also updated to tell Claude the helper is pre-defined and MUST be
called via `_cas_login_as_admin(page, base_url)` rather than writing
its own login code.

Per founder directive 2026-04-18: standard widgets for elements that
repeatedly fail. Auth in qa_tests is the standard-widget target.
"""
import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))


def test_auth_prelude_constant_exported():
    """cli.qa defines _CAS_LOGIN_AS_ADMIN_PRELUDE as a module constant
    holding a multi-line Python source string with the helper function."""
    import qa
    assert hasattr(qa, "_CAS_LOGIN_AS_ADMIN_PRELUDE")
    prelude = qa._CAS_LOGIN_AS_ADMIN_PRELUDE
    assert isinstance(prelude, str)
    assert "def _cas_login_as_admin(page, base_url):" in prelude


def test_prelude_mints_magic_link_token_via_psql():
    """The auth helper body must use psql INSERT INTO magic_link_tokens
    (not password-form submission) so it works on magic-link-only apps."""
    import qa
    prelude = qa._CAS_LOGIN_AS_ADMIN_PRELUDE
    assert "INSERT INTO magic_link_tokens" in prelude
    assert "WHERE role = 'admin'" in prelude


def test_prelude_hashes_token_with_sha256():
    """Token stored in DB must be sha256-hashed; raw token is what the
    /magic endpoint expects in the querystring."""
    import qa
    prelude = qa._CAS_LOGIN_AS_ADMIN_PRELUDE
    assert "sha256" in prelude
    assert "/magic?token=" in prelude


def test_prelude_uses_database_url_env():
    """Helper must read DATABASE_URL from the Playwright-runner env so
    the caller doesn't have to pass it in. Pipeline-level test.py sets
    DATABASE_URL before spawning Playwright."""
    import qa
    prelude = qa._CAS_LOGIN_AS_ADMIN_PRELUDE
    assert "DATABASE_URL" in prelude


def test_prelude_raises_when_database_url_missing():
    """Helper must fail loudly (not silently skip) when DATABASE_URL is
    missing, silent skip makes later protected-page tests fail with
    opaque timeouts instead of a clear auth-setup error."""
    import qa
    prelude = qa._CAS_LOGIN_AS_ADMIN_PRELUDE
    assert "raise RuntimeError" in prelude


def test_generate_qa_tests_prepends_prelude_to_claude_output(monkeypatch):
    """Integration: generate_qa_tests calls Claude (mocked here to
    return a body-only function), then prepends the prelude before
    returning. Claude's body remains intact; the prelude sits above."""
    import qa

    fake_claude_body = (
        "def run_qa_tests(page, base_url):\n"
        "    _cas_login_as_admin(page, base_url)\n"
        "    results = {'passed': [], 'failed': []}\n"
        "    return results\n"
    )

    class _Resp:
        def __init__(self, text):
            self.content = [type("M", (), {"text": text})()]

    def _fake_call_api(client, messages, use_batch=False):
        return _Resp(fake_claude_body)

    monkeypatch.setattr(qa, "_call_api", _fake_call_api)
    monkeypatch.setattr(qa, "_create_client", lambda: object())

    spec = {
        "app_name": "medicine-waves-test",
        "admin_email": "e2e@craftandship.example",
        "pages": [],
        "data_models": [],
    }

    out = qa.generate_qa_tests(spec, use_batch=False)
    # Prelude present at the top
    assert "def _cas_login_as_admin(page, base_url):" in out
    # Claude's body preserved unchanged, below the prelude
    assert "def run_qa_tests(page, base_url):" in out
    assert out.index("def _cas_login_as_admin") < out.index("def run_qa_tests")
    # Both compile as valid Python
    compile(out, "<qa_tests_with_prelude>", "exec")


def test_generate_qa_tests_does_not_double_inject_prelude(monkeypatch):
    """If Claude somehow emits its own _cas_login_as_admin (unlikely but
    possible if we teach the prompt harder), the pipeline must NOT
    prepend a duplicate."""
    import qa

    # Simulate Claude returning code that already contains the helper
    claude_with_helper = (
        qa._CAS_LOGIN_AS_ADMIN_PRELUDE
        + "def run_qa_tests(page, base_url):\n"
        + "    _cas_login_as_admin(page, base_url)\n"
        + "    return {'passed': [], 'failed': []}\n"
    )

    class _Resp:
        def __init__(self, text):
            self.content = [type("M", (), {"text": text})()]

    def _fake_call_api(client, messages, use_batch=False):
        return _Resp(claude_with_helper)

    monkeypatch.setattr(qa, "_call_api", _fake_call_api)
    monkeypatch.setattr(qa, "_create_client", lambda: object())

    spec = {"app_name": "test", "pages": [], "data_models": []}
    out = qa.generate_qa_tests(spec, use_batch=False)

    # Only ONE definition of the helper
    assert out.count("def _cas_login_as_admin(page, base_url):") == 1


def test_qa_system_prompt_instructs_claude_to_call_prelude_helper():
    """Claude must be told NOT to write its own login code and to call
    _cas_login_as_admin(page, base_url) instead. This is a prompt-text
    assertion to prevent drift on prompt edits."""
    import qa
    prompt = qa.QA_SYSTEM_PROMPT
    assert "_cas_login_as_admin" in prompt
    # Either explicitly discourages hardcoded email/password, OR tells
    # Claude password auth won't work. Check for at least one.
    discouragement_signals = [
        "DO NOT write your own login code",
        "magic-link auth",
        "NO password set",
        "admin@example.com",  # mentioned only to say "do not use this"
    ]
    assert any(sig.lower() in prompt.lower() for sig in discouragement_signals), (
        "QA_SYSTEM_PROMPT must steer Claude away from password-auth login"
    )


def test_run_qa_hands_the_test_db_url_to_the_child_not_to_os_environ():
    """Regression (v23 QA failure 2026-04-18): _cas_login_as_admin
    helper's psql INSERT failed with `relation "magic_link_tokens"
    does not exist`. Root cause: start_app sets DATABASE_URL for the
    Node subprocess only; the Python caller's env keeps pointing to the
    worker's queue DB (e.g. craftandship_web) where the generated
    app's tables don't exist.

    The generated tests now run in a child process, so run_qa builds the
    test-DB URL from db_name and passes it to the child's environment
    (_run_tests_in_child(database_url=...)). The worker's own os.environ
    is no longer patched and restored around the run. Behavior is covered
    by tests/test_qa.py::TestRunTestsInChild; this asserts the wiring."""
    import qa
    import inspect
    run_qa_source = inspect.getsource(qa.run_qa)
    assert "database_url=test_db_url" in run_qa_source
    assert "db_name" in run_qa_source
    # No process-wide environment mutation any more.
    assert 'os.environ["DATABASE_URL"] =' not in run_qa_source
    child_source = inspect.getsource(qa._run_tests_in_child)
    assert 'env["DATABASE_URL"] = database_url' in child_source


def test_qa_system_prompt_no_longer_recommends_admin_example_com_and_password():
    """The old prompt (v21 and earlier) told Claude to use
    `admin@example.com` + `test_password_for_ci` as if they were real
    seed creds. That's the class-of-bug root. Prompt must no longer
    RECOMMEND this pattern (may still MENTION it as a don't)."""
    import qa
    prompt = qa.QA_SYSTEM_PROMPT
    # The old lines verbatim
    banned = [
        'page.fill("input[name=\'email\']", "admin@example.com")',
        'page.fill("input[name=\'password\']", "test_password_for_ci")',
    ]
    for line in banned:
        assert line not in prompt, (
            f"QA_SYSTEM_PROMPT still contains legacy password-auth recipe: {line}"
        )
