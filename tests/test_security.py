"""Security-property tests for the CraftAndShip Python CLI.

These tests verify structural security properties of the source files:
- No hardcoded secrets or tokens in any source file.
- .env is present in .gitignore (secrets excluded from version control).
- subprocess calls in test.py do not use shell=True with variable input.
- build.py write_files raises on path traversal attempts.
- test.py database name validation rejects unsafe names.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

# Project root and source directories
ROOT = Path(__file__).parent.parent
CLI_DIR = ROOT / "cli"
MONITOR_DIR = ROOT / "monitor"
QUEUE_DIR = ROOT / "queue"

# All Python source files to audit (stub files have no code, still checked)
SOURCE_FILES = list(CLI_DIR.glob("*.py")) + list(MONITOR_DIR.glob("*.py")) + list(QUEUE_DIR.glob("*.py"))

# Patterns that indicate a hardcoded secret (regex applied to file content)
# Matches things like: sk-ant-api03-..., a UUID-shaped token, Bearer <token>, etc.
_SECRET_PATTERNS = [
    # Anthropic API key pattern
    re.compile(r'sk-ant-api\d+-[A-Za-z0-9_\-]{20,}'),
    # UUID-style tokens (e.g. Railway API token)
    re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'),
    # Generic long base64/hex secrets (>= 40 chars) assigned to a variable
    re.compile(r'(?:token|secret|password|api_key)\s*=\s*["\'][A-Za-z0-9+/=_\-]{40,}["\']', re.IGNORECASE),
]

# Paths to the individual source files
BUILD_PY = CLI_DIR / "build.py"
TEST_PY = CLI_DIR / "test.py"
DEPLOY_PY = CLI_DIR / "deploy.py"
VALIDATE_PY = CLI_DIR / "validate.py"
GITIGNORE = ROOT / ".gitignore"


# ---------------------------------------------------------------------------
# No hardcoded secrets
# ---------------------------------------------------------------------------

class TestNoHardcodedSecrets:
    """Verify that no source file contains known secret patterns."""

    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.name)
    def test_no_hardcoded_secret_patterns(self, source_file):
        """Source file must not contain patterns that look like live secrets."""
        content = source_file.read_text(encoding="utf-8")
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(content)
            assert match is None, (
                f"{source_file.name}: possible hardcoded secret at "
                f"position {match.start()} matching {pattern.pattern!r}: "
                f"'{match.group()[:40]}...'"
            )


# ---------------------------------------------------------------------------
# .gitignore coverage
# ---------------------------------------------------------------------------

class TestGitignore:
    def test_gitignore_exists(self):
        assert GITIGNORE.exists(), ".gitignore file must exist at project root"

    def test_env_is_in_gitignore(self):
        """.env must be excluded from version control."""
        content = GITIGNORE.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.splitlines()]
        # Accept '.env', '*.env', or '**/.env' as valid exclusions
        matched = any(
            line in (".env", "*.env", "**/.env") or line.endswith("/.env")
            for line in lines
        )
        assert matched, ".env must appear in .gitignore"

    def test_venv_is_in_gitignore(self):
        """Virtual environment directory should be excluded."""
        content = GITIGNORE.read_text(encoding="utf-8")
        assert ".venv" in content or "venv" in content, \
            ".venv or venv should be in .gitignore"


# ---------------------------------------------------------------------------
# No shell=True with variable input (AST-based check)
# ---------------------------------------------------------------------------

class TestNoShellInjection:
    """Parse Python AST to find subprocess calls with shell=True."""

    def _find_shell_true_calls(self, source_file: Path):
        """Return list of line numbers where subprocess is called with shell=True."""
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Look for subprocess.run(...) or subprocess.Popen(...)
            func = node.func
            is_subprocess = (
                (isinstance(func, ast.Attribute)
                 and func.attr in ("run", "Popen", "call", "check_call", "check_output")
                 and isinstance(func.value, ast.Name)
                 and func.value.id == "subprocess")
                or
                (isinstance(func, ast.Name)
                 and func.id in ("run", "Popen"))
            )
            if not is_subprocess:
                continue
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    hits.append(node.lineno)
        return hits

    def test_test_py_no_shell_true(self):
        """cli/test.py must not use shell=True after the security fix."""
        hits = self._find_shell_true_calls(TEST_PY)
        assert hits == [], (
            f"cli/test.py uses shell=True at line(s): {hits}. "
            "Use list-form subprocess calls instead."
        )

    def test_deploy_py_no_shell_true(self):
        """cli/deploy.py must not use shell=True."""
        hits = self._find_shell_true_calls(DEPLOY_PY)
        assert hits == [], (
            f"cli/deploy.py uses shell=True at line(s): {hits}."
        )

    def test_build_py_no_shell_true(self):
        """cli/build.py must not use shell=True."""
        hits = self._find_shell_true_calls(BUILD_PY)
        assert hits == [], (
            f"cli/build.py uses shell=True at line(s): {hits}."
        )


# ---------------------------------------------------------------------------
# Path traversal protection in build.py
# ---------------------------------------------------------------------------

class TestBuildPathTraversal:
    """write_files() must refuse paths that escape the output directory."""

    def test_path_traversal_raises(self, tmp_path):
        """A path containing '..' that escapes the output dir must raise ValueError."""
        sys.path.insert(0, str(CLI_DIR))
        from build import write_files

        output_dir = tmp_path / "builds" / "myapp"
        output_dir.mkdir(parents=True)

        evil_files = [("../../etc/evil.txt", "pwned")]
        with pytest.raises(ValueError, match="[Pp]ath traversal"):
            write_files(evil_files, output_dir)

    def test_normal_path_written_successfully(self, tmp_path):
        """A normal relative path inside the output dir must be written."""
        sys.path.insert(0, str(CLI_DIR))
        from build import write_files

        output_dir = tmp_path / "builds" / "myapp"
        output_dir.mkdir(parents=True)

        write_files([("server.js", "// hello")], output_dir)
        assert (output_dir / "server.js").read_text() == "// hello"

    def test_nested_path_written_successfully(self, tmp_path):
        """Nested paths inside the output dir must be allowed."""
        sys.path.insert(0, str(CLI_DIR))
        from build import write_files

        output_dir = tmp_path / "builds" / "myapp"
        output_dir.mkdir(parents=True)

        write_files([("routes/auth.js", "// auth")], output_dir)
        assert (output_dir / "routes" / "auth.js").read_text() == "// auth"

    def test_absolute_path_outside_dir_raises(self, tmp_path):
        """An absolute path outside output_dir must also be rejected."""
        sys.path.insert(0, str(CLI_DIR))
        from build import write_files

        output_dir = tmp_path / "builds" / "myapp"
        output_dir.mkdir(parents=True)

        evil_files = [("/etc/evil.txt", "pwned")]
        # /etc/evil.txt resolves outside tmp_path; should raise
        with pytest.raises((ValueError, OSError)):
            write_files(evil_files, output_dir)


# ---------------------------------------------------------------------------
# Database name validation in test.py
# ---------------------------------------------------------------------------

class TestDbNameValidation:
    """_validate_db_name() must reject names with shell-unsafe characters."""

    def setup_method(self):
        sys.path.insert(0, str(CLI_DIR))
        from test import _validate_db_name
        self._validate = _validate_db_name

    def test_clean_name_passes(self):
        self._validate("test_my_app")   # should not raise

    def test_hyphen_allowed(self):
        self._validate("test-my-app")   # should not raise

    def test_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self._validate("mydb; DROP TABLE users")

    def test_space_rejected(self):
        with pytest.raises(ValueError):
            self._validate("my db")

    def test_dollar_rejected(self):
        with pytest.raises(ValueError):
            self._validate("my$db")

    def test_backtick_rejected(self):
        with pytest.raises(ValueError):
            self._validate("`mydb`")

    def test_newline_rejected(self):
        with pytest.raises(ValueError):
            self._validate("my\ndb")


# ---------------------------------------------------------------------------
# deploy.py never prints raw DATABASE_URL
# ---------------------------------------------------------------------------

def _is_wrapped_in_redact(node, var_name="database_url"):
    """Return True if the AST Name node ``var_name`` is an argument to redact_url().

    Walks upward from the Name node by checking whether it is a direct argument
    to a Call whose function is named 'redact_url'.
    """
    # We can't walk upward in the default AST, so instead we check the
    # parent Call context by looking at the whole f-string FormattedValue chain.
    # This helper is used as a per-FormattedValue check below.
    if not isinstance(node, ast.FormattedValue):
        return False
    value = node.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if not (isinstance(func, ast.Name) and func.id == "redact_url"):
        return False
    # Verify database_url is one of its arguments
    for arg in value.args:
        if isinstance(arg, ast.Name) and arg.id == var_name:
            return True
    return False


class TestDeployNoRawDbUrlLog:
    """Verify deploy.py uses redact_url before any print of the database URL."""

    def test_raw_database_url_not_printed_in_deploy(self):
        """database_url must not appear bare in a print() - only wrapped in redact_url()."""
        content = DEPLOY_PY.read_text(encoding="utf-8")
        tree = ast.parse(content)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "print"):
                continue
            for arg in node.args:
                # Direct: print(database_url) - always a leak
                if isinstance(arg, ast.Name) and arg.id == "database_url":
                    pytest.fail(
                        f"deploy.py line {node.lineno}: raw 'database_url' passed "
                        "to print() without redaction"
                    )
                # f-string: scan each FormattedValue slot
                if isinstance(arg, ast.JoinedStr):
                    for fv in ast.walk(arg):
                        if not isinstance(fv, ast.FormattedValue):
                            continue
                        # If it's wrapped in redact_url(), it's safe
                        if _is_wrapped_in_redact(fv, "database_url"):
                            continue
                        # Otherwise check whether database_url appears bare inside
                        for inner in ast.walk(fv.value):
                            if isinstance(inner, ast.Name) and inner.id == "database_url":
                                pytest.fail(
                                    f"deploy.py line {node.lineno}: 'database_url' "
                                    "appears unredacted inside an f-string passed to print()"
                                )
