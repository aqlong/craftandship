"""Tests for deploy.py returning a dict with Railway metadata.

All Railway API calls are mocked; no real deployments happen. The post-deploy
probes (`verify_health`, `verify_auth`) are mocked too: unmocked they poll the
fake `*.up.railway.app` URL for up to two minutes each.
"""

import pytest

# v1-only: test set being retired as v2 engine takes over (see root conftest.py).
pytestmark = pytest.mark.v1_only

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cli"))


@pytest.fixture(autouse=True)
def _no_live_probes(monkeypatch):
    """Keep deploy() from probing the (fictional) live URL over the network."""
    import deploy as deploy_mod

    monkeypatch.setattr(deploy_mod, "verify_health", lambda url, *a, **k: "ok")
    monkeypatch.setattr(deploy_mod, "verify_auth", lambda url, spec, *a, **k: "ok")


class TestDeployReturnsDict:
    @patch("deploy.destroy_project")
    @patch("deploy.generate_domain", return_value="test-app.up.railway.app")
    @patch("deploy.wait_for_deployment")
    @patch("deploy.get_latest_deployment", return_value="deploy-1")
    @patch("deploy.deploy_source", return_value="deploy-1")
    @patch("deploy.set_env_vars")
    @patch("deploy.provision_postgres", return_value=("db-svc-1", "postgresql://x"))
    @patch("deploy.create_service", return_value="svc-123")
    @patch("deploy.get_environment_id", return_value="env-456")
    @patch("deploy.create_project", return_value="proj-789")
    @patch("deploy.read_app_name", return_value="test-app")
    @patch("deploy.get_token", return_value="fake-token")
    @patch("deploy.check_railway_cli")
    def test_deploy_returns_dict_with_railway_ids(
        self, mock_cli, mock_token, mock_name, mock_proj, mock_env,
        mock_svc, mock_pg, mock_vars, mock_src, mock_latest, mock_wait,
        mock_domain, mock_destroy, tmp_path,
    ):
        from deploy import deploy

        build_dir = tmp_path / "builds" / "test-app"
        build_dir.mkdir(parents=True)
        (build_dir / "package.json").write_text('{"name": "test-app"}')

        result = deploy(str(build_dir))

        assert isinstance(result, dict)
        assert result["live_url"] == "https://test-app.up.railway.app"
        assert result["railway_project_id"] == "proj-789"
        assert result["railway_service_id"] == "svc-123"
        assert result["railway_environment_id"] == "env-456"
        # database_url is the auto-generated Postgres connection string;
        # the worker persists it so the password can be recovered for ops
        # access (Railway variables GraphQL returns empty after deploy).
        assert result["database_url"] == "postgresql://x"
        assert result["health_check"] == "ok"

    @patch("deploy.destroy_project")
    @patch("deploy.add_custom_domain", return_value=True)
    @patch("deploy.generate_domain", return_value="test-app.up.railway.app")
    @patch("deploy.wait_for_deployment")
    @patch("deploy.get_latest_deployment", return_value="deploy-1")
    @patch("deploy.deploy_source", return_value="deploy-1")
    @patch("deploy.set_env_vars")
    @patch("deploy.provision_postgres", return_value=("db-svc-1", "postgresql://x"))
    @patch("deploy.create_service", return_value="svc-123")
    @patch("deploy.get_environment_id", return_value="env-456")
    @patch("deploy.create_project", return_value="proj-789")
    @patch("deploy.read_app_name", return_value="test-app")
    @patch("deploy.get_token", return_value="fake-token")
    @patch("deploy.check_railway_cli")
    def test_deploy_with_custom_domain_returns_custom_url(
        self, mock_cli, mock_token, mock_name, mock_proj, mock_env,
        mock_svc, mock_pg, mock_vars, mock_src, mock_latest, mock_wait,
        mock_domain, mock_custom, mock_destroy, tmp_path,
    ):
        from deploy import deploy

        build_dir = tmp_path / "builds" / "test-app"
        build_dir.mkdir(parents=True)
        (build_dir / "package.json").write_text('{"name": "test-app"}')

        result = deploy(str(build_dir), custom_domain="app.mybusiness.example")

        assert result["live_url"] == "https://app.mybusiness.example"
        assert result["railway_project_id"] == "proj-789"

    @patch("deploy.destroy_project")
    @patch("deploy.generate_domain", return_value="test-app.up.railway.app")
    @patch("deploy.wait_for_deployment")
    @patch("deploy.get_latest_deployment", return_value="deploy-1")
    @patch("deploy.deploy_source", return_value="deploy-1")
    @patch("deploy.set_env_vars")
    @patch("deploy.provision_postgres", return_value=("db-svc-1", "postgresql://x"))
    @patch("deploy.create_service", return_value="svc-123")
    @patch("deploy.get_environment_id", return_value="env-456")
    @patch("deploy.create_project", return_value="proj-789")
    @patch("deploy.read_app_name", return_value="test-app")
    @patch("deploy.get_token", return_value="fake-token")
    @patch("deploy.check_railway_cli")
    def test_deploy_never_copies_the_platform_mail_key_into_the_app(
        self, mock_cli, mock_token, mock_name, mock_proj, mock_env,
        mock_svc, mock_pg, mock_vars, mock_src, mock_latest, mock_wait,
        mock_domain, mock_destroy, tmp_path, monkeypatch,
    ):
        """The platform's RESEND_API_KEY stays on the platform: with no
        dedicated GENERATED_APP_RESEND_API_KEY the customer app gets no key."""
        monkeypatch.setenv("RESEND_API_KEY", "platform-mail-key-value")
        monkeypatch.delenv("GENERATED_APP_RESEND_API_KEY", raising=False)
        from deploy import deploy

        build_dir = tmp_path / "builds" / "test-app"
        build_dir.mkdir(parents=True)
        (build_dir / "package.json").write_text('{"name": "test-app"}')

        deploy(str(build_dir))

        call_args = mock_vars.call_args
        env_dict = call_args[0][3]  # 4th positional arg is the variables dict
        assert "RESEND_API_KEY" not in env_dict
        assert "platform-mail-key-value" not in env_dict.values()

    @patch("deploy.destroy_project")
    @patch("deploy.generate_domain", return_value="test-app.up.railway.app")
    @patch("deploy.wait_for_deployment")
    @patch("deploy.get_latest_deployment", return_value="deploy-1")
    @patch("deploy.deploy_source", return_value="deploy-1")
    @patch("deploy.set_env_vars")
    @patch("deploy.provision_postgres", return_value=("db-svc-1", "postgresql://x"))
    @patch("deploy.create_service", return_value="svc-123")
    @patch("deploy.get_environment_id", return_value="env-456")
    @patch("deploy.create_project", return_value="proj-789")
    @patch("deploy.read_app_name", return_value="test-app")
    @patch("deploy.get_token", return_value="fake-token")
    @patch("deploy.check_railway_cli")
    def test_deploy_passes_the_dedicated_app_mail_key_when_set(
        self, mock_cli, mock_token, mock_name, mock_proj, mock_env,
        mock_svc, mock_pg, mock_vars, mock_src, mock_latest, mock_wait,
        mock_domain, mock_destroy, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("RESEND_API_KEY", "platform-mail-key-value")
        monkeypatch.setenv("GENERATED_APP_RESEND_API_KEY", "app-only-mail-key-value")
        from deploy import deploy

        build_dir = tmp_path / "builds" / "test-app"
        build_dir.mkdir(parents=True)
        (build_dir / "package.json").write_text('{"name": "test-app"}')

        deploy(str(build_dir))

        env_dict = mock_vars.call_args[0][3]
        assert env_dict["RESEND_API_KEY"] == "app-only-mail-key-value"
        assert "platform-mail-key-value" not in env_dict.values()

    @patch("deploy.destroy_project")
    @patch("deploy.generate_domain", return_value="test-app.up.railway.app")
    @patch("deploy.wait_for_deployment")
    @patch("deploy.get_latest_deployment", return_value="deploy-1")
    @patch("deploy.deploy_source", return_value="deploy-1")
    @patch("deploy.set_env_vars")
    @patch("deploy.provision_postgres", return_value=("db-svc-1", "postgresql://x"))
    @patch("deploy.create_service", return_value="svc-123")
    @patch("deploy.get_environment_id", return_value="env-456")
    @patch("deploy.create_project", return_value="proj-789")
    @patch("deploy.read_app_name", return_value="test-app")
    @patch("deploy.get_token", return_value="fake-token")
    @patch("deploy.check_railway_cli")
    def test_deploy_omits_resend_api_key_when_not_set(
        self, mock_cli, mock_token, mock_name, mock_proj, mock_env,
        mock_svc, mock_pg, mock_vars, mock_src, mock_latest, mock_wait,
        mock_domain, mock_destroy, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        monkeypatch.delenv("GENERATED_APP_RESEND_API_KEY", raising=False)
        from deploy import deploy

        build_dir = tmp_path / "builds" / "test-app"
        build_dir.mkdir(parents=True)
        (build_dir / "package.json").write_text('{"name": "test-app"}')

        deploy(str(build_dir))

        call_args = mock_vars.call_args
        env_dict = call_args[0][3]
        assert "RESEND_API_KEY" not in env_dict


class TestDestroyProjectGuard:
    """Name-based and ID-based guards in destroy_project.

    destroy_project is the single choke point for all Railway project deletions.
    These tests verify it refuses to delete the CAS platform project or any
    project whose name starts with 'craftandship'.
    """

    def _call_destroy(self, project_id, project_name, env=None):
        import deploy
        from unittest.mock import patch as _patch
        import os

        gql_return = {"project": {"id": project_id, "name": project_name}}
        base_env = {"RAILWAY_PROJECT_ID": "cas-project-id"}
        if env:
            base_env.update(env)

        with _patch.dict(os.environ, base_env, clear=False), \
             _patch("deploy.gql", return_value=gql_return) as mock_gql, \
             _patch("deploy.print") as _:
            deploy.destroy_project(project_id, token=None)
        return mock_gql

    def test_blocks_by_project_id(self):
        """Raises if project_id matches RAILWAY_PROJECT_ID env var."""
        import deploy, os
        from unittest.mock import patch as _patch

        with _patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "cas-project-id"}, clear=False):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                deploy.destroy_project("cas-project-id", token=None)

    def test_blocks_craftandship_web_name(self):
        """Raises if project is named 'craftandship-web'."""
        import deploy, os
        from unittest.mock import patch as _patch

        gql_return = {"project": {"id": "some-id-123", "name": "craftandship-web"}}
        with _patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "different-id"}, clear=False), \
             _patch("deploy.gql", return_value=gql_return):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                deploy.destroy_project("some-id-123", token=None)

    def test_blocks_any_craftandship_prefix(self):
        """Raises for any project name starting with 'craftandship'."""
        import deploy, os
        from unittest.mock import patch as _patch

        gql_return = {"project": {"id": "some-id-456", "name": "craftandship-staging"}}
        with _patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "different-id"}, clear=False), \
             _patch("deploy.gql", return_value=gql_return):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                deploy.destroy_project("some-id-456", token=None)

    def test_allows_customer_app_deletion(self):
        """Customer apps (non-craftandship names) are allowed to be deleted."""
        import deploy, os
        from unittest.mock import patch as _patch

        gql_return = {"project": {"id": "customer-app-id", "name": "medicine-waves"}}
        delete_called = []
        def fake_gql(query, variables=None, token=None):
            if "projectDelete" in query:
                delete_called.append(variables)
                return {"projectDelete": True}
            return gql_return

        with _patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "cas-project-id"}, clear=False), \
             _patch("deploy.gql", side_effect=fake_gql), \
             _patch("builtins.print"):
            # True only because Railway confirmed the delete (see
            # tests/test_probe_guard.py for the outcomes that raise instead).
            assert deploy.destroy_project("customer-app-id", token=None) is True

        assert len(delete_called) == 1
        assert delete_called[0]["id"] == "customer-app-id"

    def test_blocks_protected_names_env_var(self):
        """PROTECTED_RAILWAY_PROJECT_NAMES env var adds extra blocked names."""
        import deploy, os
        from unittest.mock import patch as _patch

        gql_return = {"project": {"id": "some-id-789", "name": "my-prod-platform"}}
        with _patch.dict(os.environ, {
            "RAILWAY_PROJECT_ID": "different-id",
            "PROTECTED_RAILWAY_PROJECT_NAMES": "my-prod-platform,another-protected",
        }, clear=False), \
             _patch("deploy.gql", return_value=gql_return):
            with pytest.raises(RuntimeError, match="BLOCKED"):
                deploy.destroy_project("some-id-789", token=None)
