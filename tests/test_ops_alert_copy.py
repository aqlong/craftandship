"""What the ops agent's emails tell the operator it did.

The "fix applied" and "scan summary" emails used to say that fixes "were
committed to master" and that "Railway will auto-deploy from master". No code
in ops/ or coordinator/ commits to a repository: the two playbooks set an
environment variable on, or redeploy, one of the platform's own Railway
services (ops/fixer.py). The copy says that.
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ops.alerter as alerter

ENV = {"OPS_DRY_RUN": "false", "ADMIN_EMAIL": "ops@example.test"}


def _emails_sent_by(call):
    with patch.dict(os.environ, ENV), patch.object(alerter, "_send_email", return_value=True) as send:
        call()
    return [c[0][2] for c in send.call_args_list]


def test_the_fix_applied_email_does_not_claim_a_commit_or_a_deploy_from_master():
    (html,) = _emails_sent_by(lambda: alerter.alert_fix_applied("env_config", "set a variable"))
    assert "master" not in html
    assert "committed nothing to the repository" in html and "Railway service" in html


def test_the_live_scan_summary_email_does_not_claim_a_commit_to_master():
    summary = {"patterns_found": 1, "fixes_applied": 1, "fixes_skipped": 0, "escalations": 0}
    details = [{"category": "env_config", "description": "d", "action": "fixed", "build_count": 1}]
    (html,) = _emails_sent_by(lambda: alerter.alert_scan_summary(summary, details, dry_run=False))
    assert "master" not in html
    assert "Nothing was committed to the repository" in html


def test_what_the_emails_say_is_what_the_code_does_nothing_in_ops_or_the_coordinator_commits():
    """The other side of the claim: no module of ops/ or coordinator/ runs a git
    commit or push (a tripwire on source text, like the one in
    tests/test_coordinator_actuator_gates.py)."""
    commits = []
    for directory in ("ops", "coordinator"):
        for path in (ROOT / directory).rglob("*.py"):
            if re.search(r"""["']git["']\s*,\s*["'](?:commit|push)["']|\bgit\s+(?:commit|push)\b""",
                         path.read_text(encoding="utf-8")):
                commits.append(str(path.relative_to(ROOT)))
    assert commits == []
