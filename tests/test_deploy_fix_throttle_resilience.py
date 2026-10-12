"""Tests for deploy-fix-anthropic-throttle-resilience.

A build can block on Anthropic retry backoff in
deploy_fix, consuming most of the build-tier supervisor budget and
burning the one free supervised retry. Guards:
  (a) per-call wall-clock cap inside deploy_fix (7 min default) so a
      single Anthropic call cannot exhaust the loop.
  (b) loop wall-clock capped at 15 min (was 20), well under the build-
      tier supervisor (60 min) so the
      supervised retry stays intact.
  (c) graceful: when iteration 1 raises CallApiWallClockExceeded, the
      worker's exception handler breaks out before iteration 2 runs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow importing from cli/ + queue/.
sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))
sys.path.insert(0, str(Path(__file__).parent.parent / "queue"))


# ---------------------------------------------------------------------------
# Worker constants: loop budget cap
# ---------------------------------------------------------------------------


def test_deploy_fix_loop_budget_is_tightened_to_15_min():
    """_FIX_LOOP_BUDGETS_S['deploy_fix'] must be at MOST 15 min (900s)
    so the loop budget lives well under the build-tier supervisor cap."""
    import worker_pg
    assert worker_pg._FIX_LOOP_BUDGETS_S["deploy_fix"] <= 15 * 60, (
        "deploy_fix loop budget must be <= 15 min (tightened from 20 "
        "min so a throttled call cannot exhaust the supervisor budget)"
    )


def test_deploy_fix_loop_budget_leaves_supervisor_headroom():
    """Loop budget must leave at least 30 min of headroom under the
    build-tier supervisor so the supervised retry window stays functional
    even when one full deploy_fix attempt is consumed. Reads the actual
    build-tier timeout from _TIER_TIMEOUTS so the test tracks the live
    tier ladder (bumped 45→60→... as needed)."""
    import worker_pg
    supervisor_budget = worker_pg._TIER_TIMEOUTS["build"] * 60
    loop_budget = worker_pg._FIX_LOOP_BUDGETS_S["deploy_fix"]
    headroom = supervisor_budget - loop_budget
    assert headroom >= 30 * 60, (
        f"deploy_fix loop ({loop_budget}s) must leave >= 1800s headroom "
        f"under build-tier supervisor ({supervisor_budget}s); actual "
        f"headroom = {headroom}s"
    )


# ---------------------------------------------------------------------------
# Per-call budget resolver
# ---------------------------------------------------------------------------


def test_deploy_fix_per_call_budget_default():
    import deploy_fix
    os.environ.pop(deploy_fix._DEPLOY_FIX_PER_CALL_BUDGET_S_ENV, None)
    assert deploy_fix._deploy_fix_per_call_budget_s() == 7 * 60


def test_deploy_fix_per_call_budget_env_override(monkeypatch):
    import deploy_fix
    monkeypatch.setenv(deploy_fix._DEPLOY_FIX_PER_CALL_BUDGET_S_ENV, "120")
    assert deploy_fix._deploy_fix_per_call_budget_s() == 120.0


def test_deploy_fix_per_call_budget_clamps_too_small(monkeypatch):
    import deploy_fix
    monkeypatch.setenv(deploy_fix._DEPLOY_FIX_PER_CALL_BUDGET_S_ENV, "0.5")
    assert deploy_fix._deploy_fix_per_call_budget_s() == 5.0


def test_deploy_fix_per_call_budget_clamps_too_large(monkeypatch):
    """Reject per-call > 15 min, otherwise the per-call cap would
    exceed the loop budget and lose its guard-rail value."""
    import deploy_fix
    monkeypatch.setenv(deploy_fix._DEPLOY_FIX_PER_CALL_BUDGET_S_ENV, "99999")
    assert deploy_fix._deploy_fix_per_call_budget_s() == 900.0


def test_deploy_fix_per_call_budget_ignores_junk(monkeypatch):
    """Malformed env var falls back to default rather than crashing."""
    import deploy_fix
    monkeypatch.setenv(deploy_fix._DEPLOY_FIX_PER_CALL_BUDGET_S_ENV, "nope")
    assert deploy_fix._deploy_fix_per_call_budget_s() == 7 * 60


# ---------------------------------------------------------------------------
# attempt_deploy_fix forwards wall_clock_budget_s to _call_api
# ---------------------------------------------------------------------------


@patch("deploy_fix._call_api")
@patch("deploy_fix._create_client")
def test_attempt_deploy_fix_passes_wall_clock_budget(
    mock_client, mock_api, tmp_path, monkeypatch
):
    """Regression guard: the call path from attempt_deploy_fix must
    explicitly pass wall_clock_budget_s to _call_api so the api_client
    per-call cap fires."""
    monkeypatch.setenv("CAS_DEPLOY_FIX_CALL_BUDGET_S", "300")

    (tmp_path / "server.js").write_text("app.listen(3000);")
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="no fix found")]
    mock_api.return_value = mock_response

    from deploy_fix import attempt_deploy_fix

    attempt_deploy_fix(
        build_dir=str(tmp_path),
        deploy_logs=["crash"],
        health_status="failed",
    )

    # _call_api was called; the call includes wall_clock_budget_s=300.
    assert mock_api.called
    _, kwargs = mock_api.call_args
    assert kwargs.get("wall_clock_budget_s") == 300.0


# ---------------------------------------------------------------------------
# End-to-end throttle simulation: client that sleeps past the budget
# must cause the call to raise CallApiWallClockExceeded within the cap.
# ---------------------------------------------------------------------------


def test_attempt_deploy_fix_bails_on_retryable_throttle_storm(
    tmp_path, monkeypatch
):
    """Simulate sustained throttling: Anthropic keeps raising
    a retryable error (rate limit / internal server error). The retry
    loop computes increasing backoff waits (2,4,8,16,32s). With a
    per-call wall-clock of 10s, the pre-sleep fail-fast MUST raise
    CallApiWallClockExceeded within seconds, not burn the full 62s
    retry schedule, and not burn the supervisor budget."""
    # Very tight budget so the test completes fast.
    monkeypatch.setenv("CAS_DEPLOY_FIX_CALL_BUDGET_S", "10")

    (tmp_path / "server.js").write_text("app.listen(3000);")
    (tmp_path / "package.json").write_text('{"name":"x"}')

    # Fake client where messages.create always raises a retryable error.
    # Use APIConnectionError so we don't need to construct a full
    # RateLimitError with httpx response body mock.
    import anthropic

    fake_client = MagicMock()
    err = anthropic.APIConnectionError(request=MagicMock())
    fake_client.messages.create.side_effect = err

    with patch("deploy_fix._create_client", return_value=fake_client):
        from deploy_fix import attempt_deploy_fix

        t0 = time.monotonic()
        with pytest.raises(anthropic.APITimeoutError):
            # CallApiWallClockExceeded subclasses APITimeoutError so the
            # legacy handlers catch it unchanged; testing the parent is
            # enough.
            attempt_deploy_fix(
                build_dir=str(tmp_path),
                deploy_logs=["crashed"],
                health_status="failed",
            )
        elapsed = time.monotonic() - t0

    # The retry schedule (2+4+8+16+32=62s total) must not run to
    # completion. Budget is 10s, so the pre-sleep check should fire
    # around the 3rd retry (after ~6s of sleep). Allow generous margin
    # for loaded CI machines: 30s still proves the budget fired well
    # below the 62s worst-case and the supervisor deadline.
    assert elapsed < 30, (
        f"deploy_fix did not bail within the per-call wall-clock "
        f"(took {elapsed:.1f}s, expected < 30s under 10s budget)"
    )


def test_attempt_deploy_fix_iteration2_skipped_when_iter1_fails(
    tmp_path, monkeypatch
):
    """When iteration 1 raises via the
    per-call budget, the worker's except-break path prevents iteration
    2 from running. Here we verify the exception PROPAGATES out of
    attempt_deploy_fix, the worker's try/except turns it into a break."""
    monkeypatch.setenv("CAS_DEPLOY_FIX_CALL_BUDGET_S", "10")
    (tmp_path / "server.js").write_text("app.listen(3000);")

    import anthropic

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIConnectionError(
        request=MagicMock()
    )

    with patch("deploy_fix._create_client", return_value=fake_client):
        from deploy_fix import attempt_deploy_fix
        with pytest.raises(anthropic.APITimeoutError):
            attempt_deploy_fix(
                build_dir=str(tmp_path),
                deploy_logs=["crashed"],
                health_status="failed",
            )
    # No assertion on files: the point is that attempt_deploy_fix
    # RAISED rather than silently returning {fixed: False}. That raise
    # is what the worker's `except Exception: break` relies on to skip
    # subsequent iterations, preserving existing content (no partial
    # Claude rewrites leaked through).
