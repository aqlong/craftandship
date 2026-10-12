"""Tests for monitor/cli.py restore command.

All backup operations are mocked; no real database operations happen.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestCmdRestore:
    def test_restore_command_calls_restore_function(self):
        from monitor.cli import cmd_restore

        mock_app = {
            "app_name": "test-app",
            "live_url": "https://test-app.up.railway.app",
        }

        mock_args = MagicMock()
        mock_args.app_id = "app-123"
        mock_args.backup_path = "/tmp/backup.sql.gz"

        with patch("monitor.registry.get_app", return_value=mock_app) as mock_get:
            with patch("monitor.backup.restore") as mock_restore:
                cmd_restore(mock_args)

        mock_get.assert_called_once_with("app-123")
        mock_restore.assert_called_once_with(mock_app, "/tmp/backup.sql.gz")

    def test_restore_command_in_parser(self):
        from monitor.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["restore", "app-123", "/tmp/backup.sql.gz"])

        assert args.command == "restore"
        assert args.app_id == "app-123"
        assert args.backup_path == "/tmp/backup.sql.gz"

    def test_restore_requires_both_args(self):
        from monitor.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["restore", "app-123"])  # missing backup_path
