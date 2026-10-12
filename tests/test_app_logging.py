"""Tests for test.py app stdout/stderr log capture."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CLI_DIR = str(ROOT / "cli")
if CLI_DIR not in sys.path:
    sys.path.insert(0, CLI_DIR)

from test import start_app, stop_app


@pytest.mark.requires_tools("node")
class TestAppLogging:
    def test_start_app_creates_log_files(self, tmp_path):
        """start_app writes stdout/stderr to log files in the app directory."""
        # Create a minimal server.js that exits immediately
        server_js = tmp_path / "server.js"
        server_js.write_text('console.log("stdout test"); console.error("stderr test"); process.exit(0);')

        process = start_app(tmp_path, "test_db", 9999)
        process.wait(timeout=5)
        stop_app(process)

        stdout_log = tmp_path / "test_stdout.log"
        stderr_log = tmp_path / "test_stderr.log"
        assert stdout_log.exists()
        assert stderr_log.exists()
        assert "stdout test" in stdout_log.read_text()
        assert "stderr test" in stderr_log.read_text()

    def test_stop_app_closes_file_handles(self, tmp_path):
        """stop_app closes the log file handles."""
        server_js = tmp_path / "server.js"
        server_js.write_text('process.exit(0);')

        process = start_app(tmp_path, "test_db", 9999)
        process.wait(timeout=5)
        stop_app(process)

        assert process._stdout_file.closed
        assert process._stderr_file.closed

    def test_log_files_capture_multiline_output(self, tmp_path):
        """Log files capture multiple lines of output."""
        server_js = tmp_path / "server.js"
        server_js.write_text(
            'for (let i = 0; i < 10; i++) console.error("line " + i);\n'
            'process.exit(0);'
        )

        process = start_app(tmp_path, "test_db", 9999)
        process.wait(timeout=5)
        stop_app(process)

        stderr_log = tmp_path / "test_stderr.log"
        lines = stderr_log.read_text().splitlines()
        assert len(lines) == 10
        assert lines[0] == "line 0"
        assert lines[9] == "line 9"
