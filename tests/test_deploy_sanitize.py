"""Tests for input sanitization helpers in cli/deploy.py.

Covers:
- sanitize_app_name(): ensures Railway-safe names are produced.
- redact_url(): the password of a scheme://user:password@host URL is hidden
  before logging, for any scheme in any case, whole even when it contains
  "@", and in time linear in the text.
- get_token() and check_railway_cli(): a machine that cannot deploy raises
  DeployConfigError; only the command-line entry point exits.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import deploy
from deploy import sanitize_app_name, redact_url


# ---------------------------------------------------------------------------
# sanitize_app_name
# ---------------------------------------------------------------------------

class TestSanitizeAppName:
    """Verify that sanitize_app_name produces Railway-safe project names."""

    def test_clean_name_unchanged(self):
        """A name already using only alphanumerics and hyphens is returned as-is."""
        assert sanitize_app_name("my-app-123") == "my-app-123"

    def test_underscores_replaced_with_hyphens(self):
        result = sanitize_app_name("my_app_name")
        assert "_" not in result
        assert result == "my-app-name"

    def test_spaces_replaced_with_hyphens(self):
        result = sanitize_app_name("my app name")
        assert " " not in result
        assert result == "my-app-name"

    def test_special_chars_stripped(self):
        result = sanitize_app_name("app@name!v2.0")
        # @ . ! are all unsafe; letters/digits/hyphens remain
        assert result == "app-name-v2-0"

    def test_consecutive_hyphens_collapsed(self):
        """Multiple adjacent hyphens collapse to a single hyphen."""
        result = sanitize_app_name("my--app---name")
        assert "--" not in result
        assert result == "my-app-name"

    def test_leading_trailing_hyphens_stripped(self):
        result = sanitize_app_name("-hello-world-")
        assert not result.startswith("-")
        assert not result.endswith("-")
        assert result == "hello-world"

    def test_truncated_to_50_chars(self):
        long_name = "a" * 100
        result = sanitize_app_name(long_name)
        assert len(result) <= 50

    def test_empty_after_sanitization_raises(self):
        """A name that becomes empty after stripping must raise ValueError."""
        with pytest.raises(ValueError, match="empty string"):
            sanitize_app_name("---")

    def test_all_special_chars_raises(self):
        with pytest.raises(ValueError):
            sanitize_app_name("@@@!!!")

    def test_numeric_only_name_allowed(self):
        result = sanitize_app_name("12345")
        assert result == "12345"

    def test_unicode_chars_removed(self):
        """Non-ASCII characters are replaced so the name stays safe."""
        result = sanitize_app_name("café-app")
        # 'é' is not in [a-zA-Z0-9-] so it becomes a hyphen, then collapses
        assert "é" not in result
        # The safe letters are preserved
        assert "caf" in result
        assert "app" in result

    def test_50_char_boundary_exact(self):
        name = "a" * 50
        result = sanitize_app_name(name)
        assert result == name
        assert len(result) == 50

    def test_51_chars_truncated(self):
        name = "a" * 51
        result = sanitize_app_name(name)
        assert len(result) == 50


# ---------------------------------------------------------------------------
# redact_url
# ---------------------------------------------------------------------------

class TestRedactUrl:
    """Verify that redact_url masks the password in postgres URLs."""

    def test_password_redacted_in_standard_url(self):
        # Synthetic credentials, assembled at runtime so no source line looks like a real DSN.
        url = "postgresql://user:" + "s3cr3tpass" + "@host:5432/db"
        result = redact_url(url)
        assert "s3cr3tpass" not in result
        assert result == "postgresql://***@host:5432/db"

    def test_short_password_redacted(self):
        url = "postgresql://admin:x@host/mydb"
        result = redact_url(url)
        assert ":x@" not in result
        assert result == "postgresql://***@host/mydb"

    def test_postgresql_with_port(self):
        url = "postgresql://railway:" + "abc123def" + "@postgres.railway.internal:5432/railway"
        result = redact_url(url)
        assert "abc123def" not in result
        assert result == "postgresql://***@postgres.railway.internal:5432/railway"

    def test_url_without_password_unchanged(self):
        """A URL with no password component is returned unmodified."""
        url = "postgresql://localhost/mydb"
        result = redact_url(url)
        assert result == url

    def test_non_postgres_url_unchanged(self):
        """Non-Postgres URLs are returned unmodified."""
        url = "https://example.com/some/path"
        result = redact_url(url)
        assert result == url

    def test_empty_string_unchanged(self):
        assert redact_url("") == ""

    def test_token_urlsafe_style_password_redacted(self):
        """Passwords generated by secrets.token_urlsafe contain hyphens and underscores."""
        url = "postgresql://railway:" + "Ab-Cd_Ef1234567890abcdef" + "@postgres.railway.internal:5432/railway"
        result = redact_url(url)
        assert "Ab-Cd_Ef1234567890abcdef" not in result
        assert "***" in result

    def test_redact_preserves_scheme(self):
        url = "postgresql://user:" + "hunter2" + "@db.example.com:5432/prod"
        result = redact_url(url)
        assert result.startswith("postgresql://")


def _dsn(scheme, password, user="railway", rest="@db.internal.example:5432/railway"):
    """A URL with credentials, assembled at runtime so that no source line
    holds one."""
    return scheme + "://" + user + ":" + password + rest


class TestRedactUrlAnyScheme:
    """The scheme spellings and password shapes a narrower pattern let through."""

    PASSWORD = "Zq7" + "-pass-" + "word9"

    @pytest.mark.parametrize("scheme", [
        "postgres", "postgresql", "postgresqls", "POSTGRES", "POSTGRESQL", "Postgres", "PostgreSQL",
        "redis", "rediss", "amqp", "mysql", "mongodb+srv", "http", "HTTPS",
    ])
    def test_the_password_is_removed_whatever_the_scheme(self, scheme):
        result = redact_url(_dsn(scheme, self.PASSWORD))
        assert self.PASSWORD not in result
        assert result == scheme + "://***@db.internal.example:5432/railway"

    def test_a_password_that_contains_an_at_sign_is_removed_whole(self):
        password = "first" + "@" + "second" + "@" + "third"
        result = redact_url(_dsn("postgresql", password))
        assert result == "postgresql://***@db.internal.example:5432/railway"
        for part in ("first", "second", "third"):
            assert part not in result

    def test_an_empty_user_name_is_no_obstacle(self):
        result = redact_url(_dsn("redis", self.PASSWORD, user=""))
        assert result == "redis://***@db.internal.example:5432/railway"

    def test_every_url_in_a_line_of_text_is_redacted(self):
        line = ("connect failed for " + _dsn("postgres", self.PASSWORD) + " then for "
                + _dsn("POSTGRESQL", "other" + self.PASSWORD) + " (gave up)")
        result = redact_url(line)
        assert self.PASSWORD not in result
        assert result.count("://***@") == 2
        assert result.startswith("connect failed for postgres://***@") and result.endswith(" (gave up)")

    @pytest.mark.parametrize("text", [
        "https://example.com:8080/path",
        "http://example.com:8080/search?mail=someone@example.com",
        "http://example.com:8080#section@anchor",
        "postgresql://localhost/mydb",
        "see https://example.com:443 and write to someone@example.com",
        "user:password@host with no scheme",
        "12:30@noon",
    ])
    def test_text_without_a_url_password_is_unchanged(self, text):
        assert redact_url(text) == text

    def test_two_megabytes_of_unbroken_text_take_linear_time(self):
        """Error text from a provider is passed through whole. A pattern that
        can start a scheme at every character of a long token and run to its
        end each time is quadratic on text like this."""
        for unit in ("a", "a:", "ab://c:", "a://b:c"):
            text = unit * (2_000_000 // len(unit))
            started = time.perf_counter()
            result = redact_url(text)
            elapsed = time.perf_counter() - started
            assert elapsed < 5.0, (unit, elapsed)
            assert len(result) >= len(text)


class TestDeployConfigError:
    def test_get_token_raises_when_the_token_is_unset(self, monkeypatch):
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        with pytest.raises(deploy.DeployConfigError, match="RAILWAY_API_TOKEN is not set"):
            deploy.get_token()

    def test_get_token_raises_when_the_token_is_empty(self, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "")
        with pytest.raises(deploy.DeployConfigError):
            deploy.get_token()

    def test_get_token_returns_the_token(self, monkeypatch):
        value = "not-" + "a-real-" + "token"
        monkeypatch.setenv("RAILWAY_API_TOKEN", value)
        assert deploy.get_token() == value

    def test_the_error_is_an_ordinary_exception(self):
        """`except Exception` in the worker and the probes catches it; a
        SystemExit went past every such handler."""
        assert issubclass(deploy.DeployConfigError, RuntimeError)
        assert not issubclass(deploy.DeployConfigError, SystemExit)

    def test_a_missing_cli_raises(self, monkeypatch):
        def missing(*_args, **_kwargs):
            raise FileNotFoundError("railway")
        monkeypatch.setattr(deploy.subprocess, "run", missing)
        with pytest.raises(deploy.DeployConfigError, match="Railway CLI not found or not working"):
            deploy.check_railway_cli()

    def test_a_cli_that_does_not_answer_raises(self, monkeypatch):
        def hangs(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        monkeypatch.setattr(deploy.subprocess, "run", hangs)
        with pytest.raises(deploy.DeployConfigError):
            deploy.check_railway_cli()

    def test_a_cli_that_exits_non_zero_raises(self, monkeypatch):
        monkeypatch.setattr(
            deploy.subprocess, "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "broken"))
        with pytest.raises(deploy.DeployConfigError):
            deploy.check_railway_cli()

    def test_a_working_cli_passes_and_is_given_ten_seconds(self, monkeypatch):
        seen = {}

        def answers(command, **kwargs):
            seen.update(kwargs, command=command)
            return subprocess.CompletedProcess(command, 0, "railway 4.0.0", "")
        monkeypatch.setattr(deploy.subprocess, "run", answers)
        assert deploy.check_railway_cli() is None
        assert seen["command"] == ["railway", "--version"]
        assert seen["timeout"] == 10

    def test_the_command_line_entry_point_prints_the_reason_and_exits_1(self, monkeypatch, tmp_path, capsys):
        """main() is the one place that turns the error into an exit code."""
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        monkeypatch.setattr(deploy.subprocess, "run",
                            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "railway 4.0.0", ""))
        monkeypatch.setattr(sys, "argv", ["deploy.py", str(tmp_path)])
        with pytest.raises(SystemExit) as exit_info:
            deploy.main()
        assert exit_info.value.code == 1
        assert "RAILWAY_API_TOKEN is not set" in capsys.readouterr().out

    def test_no_function_but_main_exits_the_process(self):
        """A scan of cli/deploy.py: sys.exit, exit, quit and os._exit appear
        only inside main()."""
        import ast
        source = (Path(__file__).parent.parent / "cli" / "deploy.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        exits = []
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                dotted = (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                          and (target.value.id, target.attr) in {("sys", "exit"), ("os", "_exit")})
                plain = isinstance(target, ast.Name) and target.id in {"exit", "quit"}
                if dotted or plain:
                    exits.append(function.name)
            for node in ast.walk(function):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    raised_name = node.exc.func.id if isinstance(node.exc, ast.Call) and isinstance(
                        node.exc.func, ast.Name) else getattr(node.exc, "id", None)
                    if raised_name == "SystemExit":
                        exits.append(function.name)
        assert exits and set(exits) == {"main"}, exits
