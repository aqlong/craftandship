# tests/test_ops_agent.py - End-to-end tests for the ops agent

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ops.agent import run_scan
from ops.playbook import FixResult


class TestRunScan:
    """Test the main scan cycle with all external calls mocked."""

    @patch("ops.agent.scan_recent_failures", return_value=[])
    @patch("ops.agent.log_action")
    def test_no_patterns_found(self, mock_log, mock_scan):
        conn = MagicMock()
        summary = run_scan(conn, dry_run=True)
        assert summary["patterns_found"] == 0
        assert summary["fixes_applied"] == 0

    @patch("ops.agent.alert_fix_applied")
    @patch("ops.agent.log_action")
    @patch("ops.agent.load_fixer")
    @patch("ops.agent.match_playbook")
    @patch("ops.agent.scan_recent_failures")
    def test_fix_applied_successfully(self, mock_scan, mock_match,
                                       mock_load, mock_log, mock_alert):
        mock_scan.return_value = [{
            "pattern": "service crashed",
            "category": "railway_crash",
            "count": 3,
            "build_ids": ["id1", "id2", "id3"],
            "sample_error": "service crashed: OOMKilled",
            "first_seen": "2024-01-01",
            "last_seen": "2024-01-01",
        }]
        mock_match.return_value = MagicMock(auto_fixable=True)
        mock_fixer = MagicMock(return_value=FixResult(
            success=True,
            description="Triggered worker service redeployment",
        ))
        mock_load.return_value = mock_fixer

        with patch("ops.guardrails.count_actions", return_value=0), \
             patch("ops.guardrails.count_claude_calls", return_value=0):
            conn = MagicMock()
            summary = run_scan(conn, dry_run=False)

        assert summary["fixes_applied"] == 1
        assert summary["escalations"] == 0
        mock_alert.assert_called_once()

    @patch("ops.agent.alert_circuit_breaker")
    @patch("ops.agent.log_action")
    @patch("ops.agent.scan_recent_failures")
    def test_circuit_breaker_blocks_fix(self, mock_scan, mock_log, mock_alert_cb):
        mock_scan.return_value = [{
            "pattern": "some infra error",
            "category": "railway_crash",
            "count": 2,
            "build_ids": ["id1", "id2"],
            "sample_error": "service crashed",
            "first_seen": "2024-01-01",
            "last_seen": "2024-01-01",
        }]

        # Circuit breaker at limit
        with patch("ops.guardrails.count_actions", return_value=3):
            conn = MagicMock()
            summary = run_scan(conn, dry_run=False)

        assert summary["fixes_skipped"] == 1
        assert summary["fixes_applied"] == 0
        mock_alert_cb.assert_called_once()

    @patch("ops.agent.alert_unfixable")
    @patch("ops.agent.log_action")
    @patch("ops.agent.scan_recent_failures")
    def test_app_logic_escalated_not_fixed(self, mock_scan, mock_log, mock_alert):
        mock_scan.return_value = [{
            "pattern": "syntax error",
            "category": "app_logic",
            "count": 2,
            "build_ids": ["id1", "id2"],
            "sample_error": "SyntaxError: unexpected token",
            "first_seen": "2024-01-01",
            "last_seen": "2024-01-01",
        }]

        with patch("ops.guardrails.count_actions", return_value=0), \
             patch("ops.guardrails.count_claude_calls", return_value=0):
            conn = MagicMock()
            summary = run_scan(conn, dry_run=False)

        assert summary["escalations"] == 1
        assert summary["fixes_applied"] == 0
        mock_alert.assert_called_once()

    @patch("ops.agent.alert_unfixable")
    @patch("ops.agent.log_action")
    @patch("ops.agent.match_playbook", return_value=None)
    @patch("ops.agent.scan_recent_failures")
    def test_no_playbook_escalates(self, mock_scan, mock_match, mock_log, mock_alert):
        mock_scan.return_value = [{
            "pattern": "weird infra error",
            "category": "railway_crash",
            "count": 2,
            "build_ids": ["id1", "id2"],
            "sample_error": "OOM killed",
            "first_seen": "2024-01-01",
            "last_seen": "2024-01-01",
        }]

        with patch("ops.guardrails.count_actions", return_value=0), \
             patch("ops.guardrails.count_claude_calls", return_value=0):
            conn = MagicMock()
            summary = run_scan(conn, dry_run=False)

        assert summary["escalations"] == 1
        mock_alert.assert_called_once()

    @patch("ops.agent.log_action")
    @patch("ops.agent.scan_recent_failures")
    def test_scan_failure_handled(self, mock_scan, mock_log):
        mock_scan.side_effect = Exception("DB connection lost")
        conn = MagicMock()
        summary = run_scan(conn, dry_run=True)
        assert summary["patterns_found"] == 0
