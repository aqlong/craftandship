# tests/test_failure_kb_sync.py: Coverage for the weekly sync that
# turns top failure_kb patterns into support_kb Q&A rows.

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from coordinator.actuators import Actuators
from coordinator.agents.infra_engineer import InfraEngineer
from coordinator.budget import ModelCallMeter, TickBudget
from coordinator.failure_kb_sync import (
    DEFAULT_TOP_N,
    MIN_TOTAL_COUNT,
    _build_drafter_prompt,
    _build_question,
    _entry_total_count,
    draft_failure_answer,
    sync_failure_kb_to_support_kb,
    top_failure_patterns,
)
from coordinator.state import WorkItem, WorldState


def _allowance(calls=50, models=("claude-haiku-4-5",)):
    """A tick's model-call allowance good for `calls` cheap-tier calls."""
    return ModelCallMeter(TickBudget(max_api_calls=calls, max_cost_usd=calls * 0.05,
                                     allowed_models=list(models)))


def _make_entry(signature, success=0, failure=0, category="routing",
                fix_description="Fixed something."):
    return {
        "signature": signature,
        "success_count": success,
        "failure_count": failure,
        "fix_category": category,
        "fix_description": fix_description,
    }


def _seed_kb(tmp_path, monkeypatch, entries):
    """Write entries (a dict of sig -> entry) to a tmp KB and patch
    cli.failure_kb to use it."""
    kb_file = tmp_path / "failure_kb.json"
    kb_file.write_text(json.dumps(entries))
    import cli.failure_kb as kb_mod
    monkeypatch.setattr(kb_mod, "KB_PATH", kb_file)
    monkeypatch.setattr(kb_mod, "_get_kb_path", lambda: kb_file)
    return kb_file


class TestEntryRanking:
    def test_total_count_sums_success_and_failure(self):
        assert _entry_total_count(_make_entry("x", success=3, failure=2)) == 5
        assert _entry_total_count(_make_entry("x")) == 0

    def test_total_count_handles_missing_keys(self):
        assert _entry_total_count({"signature": "x"}) == 0

    def test_total_count_coerces_strings(self):
        # Defensive: KB JSON is sometimes hand-edited; ints may arrive
        # as strings. Fail loudly is fine, but the production code uses
        # int() so verify it doesn't blow up on stringly-typed counts.
        assert _entry_total_count({"success_count": "4", "failure_count": "1"}) == 5


class TestTopFailurePatterns:
    def test_orders_by_total_count_desc(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "low": _make_entry("low", success=2),
            "high": _make_entry("high", success=10, failure=5),
            "mid": _make_entry("mid", success=4, failure=2),
        })
        result = top_failure_patterns(top_n=10)
        sigs = [e["signature"] for e in result]
        assert sigs == ["high", "mid", "low"]

    def test_drops_entries_below_min_count(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "kept": _make_entry("kept", success=2),
            "drop": _make_entry("drop", success=1),
            "drop_zero": _make_entry("drop_zero"),
        })
        result = top_failure_patterns(top_n=10, min_count=2)
        sigs = [e["signature"] for e in result]
        assert sigs == ["kept"]

    def test_caps_at_top_n(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            f"sig-{i}": _make_entry(f"sig-{i}", success=10 + i)
            for i in range(20)
        })
        result = top_failure_patterns(top_n=5)
        assert len(result) == 5
        # Highest counts win
        assert result[0]["signature"] == "sig-19"
        assert result[-1]["signature"] == "sig-15"

    def test_empty_kb(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {})
        assert top_failure_patterns(top_n=10) == []

    def test_signature_is_present_even_when_only_in_dict_key(
            self, tmp_path, monkeypatch):
        # Older KB rows might omit the signature field on the value side.
        _seed_kb(tmp_path, monkeypatch, {
            "key-only-sig": {"success_count": 3, "fix_category": "x",
                              "fix_description": "y"},
        })
        result = top_failure_patterns(top_n=5, min_count=1)
        assert result[0]["signature"] == "key-only-sig"


class TestQuestionAndPrompt:
    def test_build_question_anchors_on_signature(self):
        q = _build_question(_make_entry("EADDRINUSE :::3000"))
        assert "EADDRINUSE" in q
        assert q.startswith("What does this build error mean")

    def test_build_question_truncates_long_signatures(self):
        q = _build_question(_make_entry("x" * 500))
        # 200-char signature cap + the prefix
        assert len(q) < 260

    def test_build_question_returns_empty_for_empty_signature(self):
        assert _build_question({"signature": ""}) == ""
        assert _build_question({}) == ""

    def test_drafter_prompt_includes_signature_and_fix(self):
        body = _build_drafter_prompt(_make_entry(
            "csrf token mismatch on /admin",
            success=4, failure=1,
            category="csrf",
            fix_description="Fixed CSRF middleware ordering.",
        ))
        assert "csrf token mismatch on /admin" in body
        assert "csrf" in body
        assert "Fixed CSRF middleware ordering." in body
        assert "succeeded 4" in body
        assert "failed 1" in body

    def test_drafter_prompt_omits_fix_line_when_unknown(self):
        body = _build_drafter_prompt(_make_entry(
            "something", success=2, fix_description=""))
        assert "Known fix:" not in body


class TestDraftFailureAnswer:
    def test_returns_none_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert draft_failure_answer(
            _make_entry("err"), "claude-haiku-4-5") is None

    def test_returns_none_for_empty_signature(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
        assert draft_failure_answer({"signature": ""}, "claude-haiku-4-5") is None
        assert draft_failure_answer({}, "claude-haiku-4-5") is None

    def test_returns_text_from_anthropic_response(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
        fake_block = MagicMock(text="The build hit a known routing issue.")
        fake_response = MagicMock(content=[fake_block])
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client

        import sys
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        result = draft_failure_answer(
            _make_entry("/foo 404", success=4, fix_description="fixed routes"),
            "claude-haiku-4-5",
        )
        assert result == "The build hit a known routing issue."

        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-haiku-4-5"
        # The user message must include the signature so Haiku has context.
        assert "/foo 404" in kwargs["messages"][0]["content"]
        # System prompt must include the support knowledge corpus.
        assert "Craft & Ship" in kwargs["system"]
        # Brand voice rule: no em dashes anywhere in the prompt.
        assert "\u2014" not in kwargs["system"]
        assert "\u2014" not in kwargs["messages"][0]["content"]

    def test_swallows_anthropic_exception(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("boom")
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client

        import sys
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        assert draft_failure_answer(
            _make_entry("err"), "claude-haiku-4-5") is None


class TestSyncOrchestrator:
    def test_drafts_and_inserts_top_n(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            f"sig-{i}": _make_entry(f"sig-{i}", success=5 + i)
            for i in range(3)
        })

        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)

        drafter = MagicMock(return_value="A drafted answer.")
        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, model="claude-haiku-4-5",
            top_n=2, min_count=1, drafter=drafter,
        )

        assert summary["considered"] == 2
        assert summary["drafted"] == 2
        assert summary["inserted"] == 2
        assert drafter.call_count == 2
        # Drafter sees the highest-count entries first
        first_call_entry = drafter.call_args_list[0].args[0]
        assert first_call_entry["signature"] == "sig-2"
        # Actuator was called with the keeper-facing question + the answer
        first_insert = actuators.support_kb_insert.call_args_list[0].args
        assert first_insert[0].startswith("What does this build error mean")
        assert first_insert[1] == "A drafted answer."

    def test_dry_run_asks_no_model_and_inserts_nothing(self, tmp_path, monkeypatch):
        """A dry run (the coordinator's default) must not pay for drafts it is
        not going to store."""
        _seed_kb(tmp_path, monkeypatch, {
            "a": _make_entry("a", success=5),
            "b": _make_entry("b", success=4),
        })
        actuators = Actuators("infra_engineer", "task-x", dry_run=True)
        actuators.support_kb_insert = MagicMock(return_value=True)
        drafter = MagicMock(return_value="A drafted answer.")

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=drafter, top_n=10, min_count=1,
        )

        drafter.assert_not_called()
        actuators.support_kb_insert.assert_not_called()
        assert summary["considered"] == 2
        assert summary["skipped_not_authorized"] == 2
        assert summary["drafted"] == 0 and summary["inserted"] == 0

    def test_drafts_stop_when_the_ticks_allowance_is_used_up(self, tmp_path, monkeypatch):
        """Each draft is one model call of the tick's allowance: with two calls
        left, two of five entries are drafted and the rest are left alone."""
        _seed_kb(tmp_path, monkeypatch, {
            f"sig-{n}": _make_entry(f"sig-{n}", success=10 - n) for n in range(5)
        })
        allowance = _allowance(calls=2)
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=allowance)
        actuators.support_kb_insert = MagicMock(return_value=True)
        drafter = MagicMock(return_value="A drafted answer.")

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=drafter, top_n=10, min_count=1,
        )

        assert drafter.call_count == 2
        assert summary["drafted"] == 2 and summary["inserted"] == 2
        assert summary["skipped_not_authorized"] == 3
        assert allowance.granted() == (2, pytest.approx(0.10))

    def test_no_allowance_means_no_draft(self, tmp_path, monkeypatch):
        """Actuators that were handed no model-call meter grant no call, live or not."""
        _seed_kb(tmp_path, monkeypatch, {"a": _make_entry("a", success=5)})
        actuators = Actuators("infra_engineer", "task-x", dry_run=False)
        actuators.support_kb_insert = MagicMock(return_value=True)
        drafter = MagicMock(return_value="A drafted answer.")

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=drafter, top_n=10, min_count=1,
        )

        drafter.assert_not_called()
        actuators.support_kb_insert.assert_not_called()
        assert summary["skipped_not_authorized"] == 1

    def test_each_draft_is_granted_for_the_model_that_writes_it(self, tmp_path, monkeypatch):
        """The allowance is asked about the model the drafter will use, so a
        tick that allows only the cheap tier refuses a dearer one."""
        _seed_kb(tmp_path, monkeypatch, {"a": _make_entry("a", success=5)})
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)
        drafter = MagicMock(return_value="A drafted answer.")

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, model="claude-sonnet-4-6", drafter=drafter, top_n=10, min_count=1,
        )

        drafter.assert_not_called()
        assert summary["skipped_not_authorized"] == 1

    def test_an_agent_without_model_call_gets_no_draft(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {"a": _make_entry("a", success=5)})
        # financial holds neither model_call nor kb_record
        actuators = Actuators("financial", "task-x", dry_run=False)
        drafter = MagicMock(return_value="A drafted answer.")

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=drafter, min_count=1,
        )

        drafter.assert_not_called()
        assert summary["skipped_permission"] == 1
        assert summary["drafted"] == 0

    def test_skips_when_drafter_returns_none(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "a": _make_entry("a", success=5),
            "b": _make_entry("b", success=4),
        })

        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)

        # First entry drafts; second returns None (e.g. transient API error).
        drafter = MagicMock(side_effect=["First answer.", None])
        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=drafter, top_n=10, min_count=1,
        )

        assert summary["considered"] == 2
        assert summary["drafted"] == 1
        assert summary["inserted"] == 1
        assert summary["skipped_no_draft"] == 1
        assert actuators.support_kb_insert.call_count == 1

    def test_counts_actuator_permission_errors(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "a": _make_entry("a", success=5),
        })
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(side_effect=PermissionError("nope"))

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=lambda e, m: "ans", min_count=1,
        )

        assert summary["drafted"] == 1
        assert summary["inserted"] == 0
        assert summary["skipped_permission"] == 1

    def test_counts_actuator_runtime_errors(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "a": _make_entry("a", success=5),
        })
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(side_effect=RuntimeError("boom"))

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=lambda e, m: "ans", min_count=1,
        )

        assert summary["skipped_error"] == 1
        assert summary["inserted"] == 0

    def test_drafter_exception_treated_as_no_draft(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "a": _make_entry("a", success=5),
        })
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)

        def bad_drafter(entry, model):
            raise RuntimeError("anthropic timed out")

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=bad_drafter, min_count=1,
        )

        assert summary["drafted"] == 0
        assert summary["skipped_no_draft"] == 1
        actuators.support_kb_insert.assert_not_called()

    def test_inserted_false_does_not_count_as_inserted(self, tmp_path, monkeypatch):
        # support_kb_insert returns False when a row already exists
        # (WHERE NOT EXISTS skipped). That's a successful idempotent
        # outcome from the agent's perspective but should NOT inflate
        # the inserted counter.
        _seed_kb(tmp_path, monkeypatch, {
            "a": _make_entry("a", success=5),
        })
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=False)

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=lambda e, m: "ans", min_count=1,
        )

        assert summary["drafted"] == 1
        assert summary["inserted"] == 0
        assert summary["skipped_permission"] == 0
        assert summary["skipped_error"] == 0

    def test_empty_kb_returns_zero_summary(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {})
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=lambda e, m: "ans",
        )

        assert summary == {
            "considered": 0,
            "drafted": 0,
            "inserted": 0,
            "skipped_no_draft": 0,
            "skipped_not_authorized": 0,
            "skipped_permission": 0,
            "skipped_error": 0,
            "questions_inserted": [],
        }
        actuators.support_kb_insert.assert_not_called()

    def test_default_top_n_and_min_count_are_used(self, tmp_path, monkeypatch):
        # Build DEFAULT_TOP_N+5 qualifying entries; default top_n caps the slice.
        entries = {
            f"sig-{i}": _make_entry(f"sig-{i}", success=20 + i)
            for i in range(DEFAULT_TOP_N + 5)
        }
        # And one below MIN_TOTAL_COUNT to verify the default min_count drops it.
        entries["below-min"] = _make_entry("below-min", success=MIN_TOTAL_COUNT - 1)
        _seed_kb(tmp_path, monkeypatch, entries)

        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)

        summary = sync_failure_kb_to_support_kb(
            actuators=actuators, drafter=lambda e, m: "answer",
        )
        assert summary["considered"] == DEFAULT_TOP_N
        assert summary["inserted"] == DEFAULT_TOP_N
        # below-min must NOT show up in the inserted questions
        assert all("below-min" not in q for q in summary["questions_inserted"])


def _make_state(**overrides):
    defaults = dict(
        tick_id="test-tick", tick_number=1,
        tick_start=datetime.now(timezone.utc),
        queued_builds=0, active_builds=0,
        failed_builds_24h=[], preview_awaiting=0,
        abandoned_intakes_24h=0, pending_feedback=0,
        monitored_apps_down=[], monitored_apps_degraded=[],
        api_cost_today_usd=0, daily_budget_remaining_usd=20.0,
        agent_success_rates={}, agent_avg_cost={},
        pending_work=[], previous_tick_summary={},
    )
    defaults.update(overrides)
    return WorldState(**defaults)


def _make_task(category, context=None):
    return WorkItem(
        category=category,
        target_agent="infra_engineer",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestInfraEngineerInitiative:
    """The infra_engineer initiative path now runs the failure_kb →
    support_kb sync instead of just sending a pipeline-idle alert."""

    def test_agent_initiative_runs_sync(self, tmp_path, monkeypatch):
        _seed_kb(tmp_path, monkeypatch, {
            "sig-A": _make_entry("sig-A", success=5),
            "sig-B": _make_entry("sig-B", success=3),
        })
        agent = InfraEngineer()
        state = _make_state()
        task = _make_task("agent_initiative",
                          {"agent": "infra_engineer"})
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=_allowance())
        actuators.support_kb_insert = MagicMock(return_value=True)

        with patch(
            "coordinator.failure_kb_sync.draft_failure_answer",
            return_value="A plain-English answer.",
        ):
            result = agent.execute(
                task, state, "claude-haiku-4-5", actuators,
            )

        assert result.success
        assert result.output["initiative"] == "failure_kb_to_support_kb_sync"
        assert result.output["sync"]["considered"] == 2
        assert result.output["sync"]["inserted"] == 2
        assert any("Failure KB" in lng for lng in result.learnings)

    def test_initiative_drafts_with_the_cheap_model_whatever_the_task_was_given(
            self, tmp_path, monkeypatch):
        """The tick dispatches this agent on its own tier (Sonnet). One
        initiative task can write up to DEFAULT_TOP_N drafts, and they are
        written by the sync's cheap-tier model, not the task's."""
        _seed_kb(tmp_path, monkeypatch, {
            "sig-A": _make_entry("sig-A", success=5),
            "sig-B": _make_entry("sig-B", success=3),
        })
        allowance = _allowance()
        actuators = Actuators("infra_engineer", "task-x", dry_run=False, model_calls=allowance)
        actuators.support_kb_insert = MagicMock(return_value=True)

        with patch(
            "coordinator.failure_kb_sync.draft_failure_answer",
            return_value="A plain-English answer.",
        ) as drafter:
            result = InfraEngineer().execute(
                _make_task("agent_initiative", {"agent": "infra_engineer"}),
                _make_state(), "claude-sonnet-4-6", actuators,
            )

        assert result.success
        assert [call.args[1] for call in drafter.call_args_list] == ["claude-haiku-4-5"] * 2
        assert allowance.granted() == (2, pytest.approx(0.10))

