"""Tests for cli/test.py Railway-specific behavior.

Covers:
- install_deps() returning 3-tuple on Railway path (no copytree to volume)
- setup_app() returning 4-tuple and passing test_dir to start_app
- teardown_app() cleaning up test_dir
- test_app() reading logs from test_dir
- qa.py correctly detecting install_deps failure (not truthiness check)
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add cli/ to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app_dir(tmp_path, extra_files=None):
    """Create a minimal app directory with package.json and server.js."""
    app_dir = tmp_path / "myapp"
    app_dir.mkdir()
    spec = {
        "app_name": "myapp",
        "tier": "build",
        "pages": [{"title": "Home", "slug": "home", "auth_required": False,
                   "purpose": "landing", "allowed_roles": ["public"]}],
        "data_models": [],
        "user_roles": [{"role": "admin", "description": "Admin",
                        "can": ["manage everything"]}],
        "actions": [],
        "settings": {"seed_data": True},
        "design": {},
        "spec_version": "1.0",
    }
    (app_dir / "spec.json").write_text(json.dumps(spec))
    (app_dir / "package.json").write_text(json.dumps({
        "name": "myapp", "version": "1.0.0", "main": "server.js",
        "scripts": {"start": "node server.js"},
        "dependencies": {"express": "^4.18.0"}
    }))
    (app_dir / "server.js").write_text("const express = require('express');\n")
    if extra_files:
        for name, content in extra_files.items():
            (app_dir / name).write_text(content)
    return app_dir


def _fake_run_cmd_success(*args, **kwargs):
    """Mock run_cmd that always succeeds."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def _fake_run_cmd_failure(*args, **kwargs):
    """Mock run_cmd that always fails with an npm error."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = "npm ERR! code ENOENT\nnpm ERR! missing package.json"
    return result


# ---------------------------------------------------------------------------
# install_deps(): Railway path
# ---------------------------------------------------------------------------

class TestInstallDepsRailway:

    def test_returns_three_tuple(self, tmp_path):
        """install_deps on Railway returns (True, '', test_dir) on success."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}):
            with patch.object(test_module, "run_cmd", side_effect=_fake_run_cmd_success):
                result = test_module.install_deps(app_dir)

        assert len(result) == 3
        success, err, test_dir = result
        assert success is True
        assert err == ""
        assert test_dir is not None
        # On macOS tmpdir may be under /var/folders; on Linux it's /tmp
        assert "npm_test_" in str(test_dir)
        # Cleanup
        if test_dir and Path(test_dir).exists():
            shutil.rmtree(str(test_dir), ignore_errors=True)

    def test_source_files_copied_to_test_dir(self, tmp_path):
        """Source files (package.json, server.js) appear in the returned test_dir."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}):
            with patch.object(test_module, "run_cmd", side_effect=_fake_run_cmd_success):
                success, err, test_dir = test_module.install_deps(app_dir)

        assert success is True
        assert (Path(test_dir) / "package.json").exists()
        assert (Path(test_dir) / "server.js").exists()
        # Cleanup
        shutil.rmtree(str(test_dir), ignore_errors=True)

    def test_no_node_modules_written_to_volume(self, tmp_path):
        """node_modules is NOT created inside app_dir (the volume) on Railway."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}):
            with patch.object(test_module, "run_cmd", side_effect=_fake_run_cmd_success):
                success, err, test_dir = test_module.install_deps(app_dir)

        assert success is True
        # Volume dir must NOT have node_modules
        assert not (app_dir / "node_modules").exists()
        # Cleanup
        if test_dir:
            shutil.rmtree(str(test_dir), ignore_errors=True)

    def test_npm_failure_returns_false_and_cleans_up(self, tmp_path, monkeypatch):
        """npm install failure: returns (False, error, None) and tmp_dir is deleted."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        # Make the working directory land in a private scratch location and
        # record it, so the leak check looks at exactly the directory this
        # call created (not a shared /tmp that other runs also write to, and
        # not a path that is empty by construction where TMPDIR is elsewhere).
        scratch = tmp_path / "scratch-tmp"
        scratch.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch))
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def recording_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)

        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}):
            with patch.object(test_module, "run_cmd", side_effect=_fake_run_cmd_failure):
                result = test_module.install_deps(app_dir)

        assert len(result) == 3
        success, err, test_dir = result
        assert success is False
        assert "ENOENT" in err or err != ""
        assert test_dir is None
        # A working directory was created for the failed install, and it is gone.
        assert len(created) == 1 and created[0].name.startswith("npm_test_")
        assert not created[0].exists(), f"Leaked tmp dir: {created[0]}"
        assert list(scratch.iterdir()) == []

    def test_package_lock_copied_back_to_volume(self, tmp_path):
        """package-lock.json is copied back to app_dir after successful install."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        def run_cmd_that_creates_lock(*args, **kwargs):
            # Simulate npm creating package-lock.json in cwd
            cwd = kwargs.get("cwd") or (args[1] if len(args) > 1 else None)
            if cwd:
                (Path(cwd) / "package-lock.json").write_text('{"lockfileVersion": 2}')
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}):
            with patch.object(test_module, "run_cmd", side_effect=run_cmd_that_creates_lock):
                success, err, test_dir = test_module.install_deps(app_dir)

        assert success is True
        assert (app_dir / "package-lock.json").exists()
        # Cleanup
        if test_dir:
            shutil.rmtree(str(test_dir), ignore_errors=True)


# ---------------------------------------------------------------------------
# install_deps(): local path
# ---------------------------------------------------------------------------

class TestInstallDepsLocal:

    def test_local_returns_none_test_dir(self, tmp_path):
        """Without RAILWAY_ENVIRONMENT, test_dir is None (local path unchanged)."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        env = {k: v for k, v in os.environ.items() if k != "RAILWAY_ENVIRONMENT"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(test_module, "run_cmd", side_effect=_fake_run_cmd_success):
                result = test_module.install_deps(app_dir)

        assert len(result) == 3
        success, err, test_dir = result
        assert success is True
        assert test_dir is None

    def test_local_failure_returns_false_and_none(self, tmp_path):
        """Local npm failure returns (False, err, None)."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        env = {k: v for k, v in os.environ.items() if k != "RAILWAY_ENVIRONMENT"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(test_module, "run_cmd", side_effect=_fake_run_cmd_failure):
                result = test_module.install_deps(app_dir)

        assert len(result) == 3
        success, err, test_dir = result
        assert success is False
        assert test_dir is None


# ---------------------------------------------------------------------------
# teardown_app(): test_dir cleanup
# ---------------------------------------------------------------------------

class TestTeardownApp:

    def test_cleans_test_dir_when_provided(self, tmp_path):
        """teardown_app removes test_dir after process termination."""
        import test as test_module
        test_dir = tmp_path / "npm_test_abc123"
        test_dir.mkdir()
        (test_dir / "server.js").write_text("// test")

        with patch.object(test_module, "stop_app"):
            with patch.object(test_module, "teardown_database"):
                test_module.teardown_app(None, None, None, test_dir)

        assert not test_dir.exists()

    def test_no_test_dir_is_safe(self, tmp_path):
        """teardown_app(process, db, app_dir) with no test_dir arg still works."""
        import test as test_module
        with patch.object(test_module, "stop_app"):
            with patch.object(test_module, "teardown_database"):
                # Should not raise
                test_module.teardown_app(None, None, None)

    def test_nonexistent_test_dir_is_safe(self, tmp_path):
        """teardown_app with a test_dir that doesn't exist doesn't raise."""
        import test as test_module
        ghost_dir = tmp_path / "npm_test_ghost"  # does not exist

        with patch.object(test_module, "stop_app"):
            with patch.object(test_module, "teardown_database"):
                # Should not raise
                test_module.teardown_app(None, None, None, ghost_dir)


# ---------------------------------------------------------------------------
# qa.py: truthiness bug fixed
# ---------------------------------------------------------------------------

class TestQaInstallDepsFix:

    def test_qa_detects_install_deps_failure(self, tmp_path):
        """qa.py correctly detects install_deps failure via success bool, not tuple truthiness."""
        import qa as qa_module
        app_dir = _make_app_dir(tmp_path)

        with patch("qa.install_deps", return_value=(False, "npm ERR! code ENOENT", None)):
            with patch("qa.start_app") as mock_start, patch("qa.teardown_database"):
                result = qa_module.run_qa(str(app_dir), {}, port=None)

        # start_app must NOT have been called (install failed)
        mock_start.assert_not_called()
        # Result should be skipped/failed, not a crash
        assert result is not None
        assert result.get("skipped") is True or result.get("success") is False

    def test_qa_cleans_test_dir_on_success(self, tmp_path):
        """qa.py removes test_dir in its finally block."""
        import qa as qa_module
        app_dir = _make_app_dir(tmp_path)

        # Create a fake test_dir that install_deps would return
        fake_test_dir = tmp_path / "npm_test_qa_fake"
        fake_test_dir.mkdir()
        (fake_test_dir / "server.js").write_text("// fake")

        # qa.py imports install_deps/setup_database/etc at module level, so patch via qa module
        with patch("qa.install_deps", return_value=(True, "", fake_test_dir)):
            with patch("qa.setup_database", return_value=True):
                with patch("qa.start_app", return_value=MagicMock()):
                    with patch("qa.wait_for_app", return_value=False):
                        with patch("qa.stop_app"):
                            with patch("qa.teardown_database"):
                                qa_module.run_qa(str(app_dir), {}, port=None)

        # test_dir must be cleaned up in the finally block
        assert not fake_test_dir.exists()


# ---------------------------------------------------------------------------
# setup_app(): npm audit advisory step
# ---------------------------------------------------------------------------

class TestNpmAuditAdvisory:

    def test_audit_runs_after_install_deps(self, tmp_path):
        """npm audit is called after successful install_deps in setup_app."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        run_cmd_calls = []

        def tracking_run_cmd(*args, **kwargs):
            run_cmd_calls.append(args[0] if args else kwargs.get("args"))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch.object(test_module, "run_cmd", side_effect=tracking_run_cmd):
            with patch.object(test_module, "setup_database", return_value=True):
                with patch.object(test_module, "start_app", return_value=MagicMock()):
                    with patch.object(test_module, "wait_for_app", return_value=True):
                        try:
                            test_module.setup_app(str(app_dir))
                        except Exception:
                            pass  # setup_app may fail on app start; we only care about run_cmd calls

        # Check that npm audit was called
        audit_calls = [c for c in run_cmd_calls if c and "audit" in str(c)]
        assert len(audit_calls) >= 1, f"Expected npm audit call, got: {run_cmd_calls}"
        assert "--audit-level=high" in str(audit_calls[0])

    def test_audit_failure_does_not_block_build(self, tmp_path):
        """npm audit failure (high vulns) does not raise or block setup_app."""
        import test as test_module
        app_dir = _make_app_dir(tmp_path)

        call_count = [0]
        audit_commands = []

        def run_cmd_with_audit_failure(*args, **kwargs):
            call_count[0] += 1
            cmd = args[0] if args else kwargs.get("args", [])
            if "audit" in str(cmd):
                audit_commands.append(cmd)
            result = MagicMock()
            if "audit" in str(cmd):
                result.returncode = 1
                result.stdout = '{"metadata":{"vulnerabilities":{"high":3,"critical":1}}}'
                result.stderr = ""
            else:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            return result

        with patch.object(test_module, "run_cmd", side_effect=run_cmd_with_audit_failure):
            with patch.object(test_module, "setup_database", return_value=True):
                with patch.object(test_module, "start_app", return_value=MagicMock()):
                    with patch.object(test_module, "wait_for_app", return_value=True):
                        # The audit reports 1 critical + 3 high findings and
                        # exits non-zero; setup_app must still return normally.
                        result = test_module.setup_app(str(app_dir))

        assert result is not None
        assert call_count[0] >= 2, "npm install and npm audit should both have run"
        assert any("audit" in str(c) for c in audit_commands)

# ---------------------------------------------------------------------------
# _build_seed_users(): credential mapping for smoke tests
# ---------------------------------------------------------------------------

class TestBuildSeedUsers:
    """Regression tests for _build_seed_users() in test.py.

    The function maps spec.user_roles to {email, password} dicts for smoke tests.
    Critical invariant: any role named 'admin' (case-insensitive) must map to
    admin_email, not admin@example.com -- because Claude often seeds the admin
    user directly with the owner email rather than admin@example.com placeholder,
    so admin@example.com would not exist in the DB.
    """

    def test_first_role_uses_admin_email(self):
        """Role at index 0 always maps to admin_email when available."""
        import test as test_module
        spec = {
            "admin_email": "owner@example.com",
            "user_roles": [{"role": "admin", "description": "Site owner"}],
        }
        result = test_module._build_seed_users(spec)
        assert result["admin"]["email"] == "owner@example.com"
        assert result["admin"]["password"] == test_module.TEST_MODE_PASSWORD

    def test_admin_role_at_index_1_uses_admin_email(self):
        """Regression: when visitor is role 0 and admin is role 1, admin gets admin_email.

        Medicine-waves failure: spec has roles [visitor, admin]. _build_seed_users
        was mapping admin -> admin@example.com, but seed only had owner@craftandship.example
        with role admin. TEST_MODE backdoor found no user -> login re-rendered at /login.
        """
        import test as test_module
        spec = {
            "admin_email": "owner@craftandship.example",
            "user_roles": [
                {"role": "visitor", "description": "Public visitor"},
                {"role": "admin", "description": "Site owner"},
            ],
        }
        result = test_module._build_seed_users(spec)
        # visitor (first role) -> admin_email
        assert result["visitor"]["email"] == "owner@craftandship.example"
        # admin (second role, named 'admin') -> ALSO admin_email, not admin@example.com
        assert result["admin"]["email"] == "owner@craftandship.example", (
            "admin role must use admin_email (not admin@example.com) since Claude "
            "seeds admin directly as admin_email without using the placeholder"
        )

    def test_non_admin_non_first_role_uses_role_at_example(self):
        """Roles that are not admin and not first use {role}@example.com."""
        import test as test_module
        spec = {
            "admin_email": "owner@example.com",
            "user_roles": [
                {"role": "admin", "description": "Admin"},
                {"role": "member", "description": "Regular member"},
                {"role": "moderator", "description": "Content moderator"},
            ],
        }
        result = test_module._build_seed_users(spec)
        assert result["admin"]["email"] == "owner@example.com"
        assert result["member"]["email"] == "member@example.com"
        assert result["moderator"]["email"] == "moderator@example.com"

    def test_no_admin_email_in_spec_falls_back_to_role_at_example(self):
        """When admin_email is absent, all roles use {role}@example.com."""
        import test as test_module
        spec = {
            "user_roles": [
                {"role": "admin", "description": "Admin"},
                {"role": "user", "description": "User"},
            ],
        }
        result = test_module._build_seed_users(spec)
        assert result["admin"]["email"] == "admin@example.com"
        assert result["user"]["email"] == "user@example.com"

    def test_empty_spec_returns_empty_dict(self):
        """None spec returns empty dict without error."""
        import test as test_module
        assert test_module._build_seed_users(None) == {}
        assert test_module._build_seed_users({}) == {}

    def test_admin_role_case_insensitive(self):
        """Role named 'Admin' or 'ADMIN' (any case) maps to admin_email."""
        import test as test_module
        spec = {
            "admin_email": "boss@company.example",
            "user_roles": [
                {"role": "user", "description": "Regular user"},
                {"role": "Admin", "description": "Administrator"},
            ],
        }
        result = test_module._build_seed_users(spec)
        assert result["user"]["email"] == "boss@company.example"  # first role
        assert result["Admin"]["email"] == "boss@company.example"  # admin by name


class TestAdminPages:
    """Tests for _admin_pages() role-exclusive page detection.

    Prevents false-positive Test 8 failures where the test expects 401/403
    on a page that the spec explicitly allows the second role to access.
    """

    def test_returns_exclusive_pages_when_they_exist(self):
        """When admin has exclusive pages, return only those."""
        import test as test_module
        spec = {
            "user_roles": [{"role": "admin"}, {"role": "member"}],
            "pages": [
                {"path": "/admin-panel", "auth_required": True, "allowed_roles": ["admin"]},
                {"path": "/feed", "auth_required": True, "allowed_roles": ["admin", "member"]},
            ],
        }
        result = test_module._admin_pages(spec)
        assert result == ["/admin-panel"]

    def test_returns_empty_when_all_pages_shared(self):
        """When all pages are shared between roles, return empty (skip Test 8)."""
        import test as test_module
        spec = {
            "user_roles": [{"role": "admin"}, {"role": "user"}],
            "pages": [
                {"path": "/dashboard", "auth_required": True, "allowed_roles": ["admin", "user"]},
                {"path": "/tasks", "auth_required": True, "allowed_roles": ["admin", "user"]},
            ],
        }
        result = test_module._admin_pages(spec)
        assert result == [], "Shared pages must NOT be returned as admin-exclusive"

    def test_returns_empty_when_no_auth_required_pages(self):
        """Public-only specs have no admin pages."""
        import test as test_module
        spec = {
            "user_roles": [{"role": "visitor"}, {"role": "admin"}],
            "pages": [
                {"path": "/", "auth_required": False, "allowed_roles": ["visitor", "admin"]},
                {"path": "/gallery", "auth_required": False, "allowed_roles": ["visitor", "admin"]},
            ],
        }
        result = test_module._admin_pages(spec)
        assert result == []

    def test_single_role_returns_all_auth_pages(self):
        """With only one role, all auth_required pages are 'exclusive'."""
        import test as test_module
        spec = {
            "user_roles": [{"role": "admin"}],
            "pages": [
                {"path": "/dashboard", "auth_required": True, "allowed_roles": ["admin"]},
                {"path": "/settings", "auth_required": True, "allowed_roles": ["admin"]},
            ],
        }
        result = test_module._admin_pages(spec)
        assert result == ["/dashboard", "/settings"]

    def test_none_spec_returns_empty(self):
        import test as test_module
        assert test_module._admin_pages(None) == []

    def test_no_roles_returns_empty(self):
        import test as test_module
        assert test_module._admin_pages({"user_roles": [], "pages": []}) == []

    def test_mixed_exclusive_and_shared(self):
        """Only exclusive pages returned, shared pages excluded."""
        import test as test_module
        spec = {
            "user_roles": [{"role": "admin"}, {"role": "user"}],
            "pages": [
                {"path": "/admin", "auth_required": True, "allowed_roles": ["admin"]},
                {"path": "/dashboard", "auth_required": True, "allowed_roles": ["admin", "user"]},
                {"path": "/reports", "auth_required": True, "allowed_roles": ["admin"]},
            ],
        }
        result = test_module._admin_pages(spec)
        assert result == ["/admin", "/reports"]
        assert "/dashboard" not in result


class TestQaModelConfig:
    """Verify qa.py model configuration after Haiku downgrade."""

    def test_qa_uses_haiku_model(self):
        """qa.py uses claude-haiku-4-5, not sonnet."""
        from qa import MODEL
        assert MODEL == "claude-haiku-4-5"

    def test_review_still_uses_sonnet(self):
        """review.py must remain on sonnet (catches real bugs)."""
        from review import MODEL
        assert MODEL == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Child environment: the app under test and `npm install` must not inherit
# the worker's credentials.
# ---------------------------------------------------------------------------

# Deliberately not secret-shaped: only the variable NAMES matter to the filter.
_WORKER_ENV = {
    "ANTHROPIC_API_KEY": "dummy-value",
    "RAILWAY_API_TOKEN": "dummy-value",
    "RAILWAY_ENVIRONMENT": "production",
    "RESEND_API_KEY": "dummy-value",
    "STRIPE_SECRET_KEY": "dummy-value",
    "OPENAI_API_KEY": "dummy-value",
    "GITHUB_TOKEN": "dummy-value",
    "SENTRY_DSN": "dummy-value",
    "OPS_API_KEY": "dummy-value",
    "SESSION_SECRET": "dummy-value",
    "PGPASSWORD": "dummy-value",
    "AWS_ACCESS_KEY_ID": "dummy-value",
    "DATABASE_URL": "postgres://worker:dummy@localhost:5432/main",
}
_KEPT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "LANG": "C", "NODE_OPTIONS": "--no-warnings"}


class TestChildEnvironmentHasNoSecrets:

    def test_filter_drops_credentials_and_keeps_the_rest(self):
        import test as test_module
        out = test_module._env_without_secrets({**_WORKER_ENV, **_KEPT_ENV})
        assert out == _KEPT_ENV

    def test_start_app_env_has_no_worker_secrets(self, tmp_path):
        import test as test_module
        captured = {}

        class _Proc:
            def poll(self):
                return 0

        def fake_popen(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return _Proc()

        with patch.dict(os.environ, {**_WORKER_ENV, **_KEPT_ENV}, clear=True):
            with patch.object(test_module.subprocess, "Popen", side_effect=fake_popen):
                proc = test_module.start_app(tmp_path, "cas_test_db", 4999)
        test_module.stop_app(proc)

        env = captured["env"]
        for name in _WORKER_ENV:
            if name in ("DATABASE_URL", "SESSION_SECRET"):
                continue  # re-set below for the throwaway test database / test session
            assert name not in env, f"{name} leaked into the app under test"
        assert env["DATABASE_URL"].endswith("/cas_test_db")
        assert "/main" not in env["DATABASE_URL"]
        assert env["SESSION_SECRET"] == "test_session_secret"
        assert env["PORT"] == "4999"
        assert env["PATH"] == _KEPT_ENV["PATH"]

    @pytest.mark.parametrize("railway", [True, False], ids=["railway-tmpdir", "local"])
    def test_npm_install_env_has_no_worker_secrets(self, tmp_path, railway):
        import test as test_module
        app_dir = _make_app_dir(tmp_path)
        seen = []

        def fake_run_cmd(command, **kwargs):
            seen.append(kwargs.get("env"))
            return _fake_run_cmd_success()

        worker_env = {**_WORKER_ENV, **_KEPT_ENV}
        if not railway:
            worker_env.pop("RAILWAY_ENVIRONMENT")
        with patch.dict(os.environ, worker_env, clear=True):
            with patch.object(test_module, "run_cmd", side_effect=fake_run_cmd):
                success, _err, test_dir = test_module.install_deps(app_dir)
        if test_dir:
            shutil.rmtree(str(test_dir), ignore_errors=True)

        assert success is True and len(seen) == 1
        assert seen[0] is not None, "npm install must get an explicit scrubbed env, not the inherited one"
        assert not (set(seen[0]) & set(_WORKER_ENV))
        assert seen[0]["PATH"] == _KEPT_ENV["PATH"]
