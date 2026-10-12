# tests/test_customer_service_agent.py: Customer Service agent tests

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from coordinator.agents.customer_service import CustomerService
from coordinator.state import WorkItem, WorldState
from coordinator.actuators import Actuators
from coordinator.budget import ModelCallMeter, TickBudget


def _allowance(calls=50, models=("claude-haiku-4-5",)):
    """A tick's model-call allowance good for `calls` cheap-tier calls."""
    return ModelCallMeter(TickBudget(max_api_calls=calls, max_cost_usd=calls * 0.05,
                                     allowed_models=list(models)))


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
        target_agent="customer_service",
        description=f"Test {category}",
        context=context or {},
        context_hash="testhash",
        created_at=datetime.now(timezone.utc),
    )


class TestFeedbackTriage:
    def test_classifies_bug_high_severity(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("customer_feedback", {
            "feedback_id": "fb-123",
            "feedback_category": "bug",
            "message_preview": "The app crashed when I tried to submit",
        })
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        # Mock DB: return feedback + no KB match
        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": "The app crashed when I tried to submit",
              "category": "bug", "page_url": "/intake", "email": "test@test.example",
              "name": "Test User"}],
            [],  # no KB match
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["severity"] == "high"
        assert result.escalate  # high severity bugs escalate

    def test_classifies_suggestion_low(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("customer_feedback", {
            "feedback_id": "fb-456",
            "feedback_category": "suggestion",
            "message_preview": "Would be nice to have dark mode",
        })
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": "Would be nice to have dark mode",
              "category": "suggestion", "page_url": "/dashboard",
              "email": "test@test.example", "name": "Test User"}],
            [],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["severity"] == "low"
        assert not result.escalate

    def test_detects_kb_gap(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("customer_feedback", {
            "feedback_id": "fb-789",
            "feedback_category": "question",
            "message_preview": "How do I export my data?",
        })
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": "How do I export my data?",
              "category": "question", "page_url": "/dashboard",
              "email": "test@test.example", "name": "Test User"}],
            [],  # no KB match = KB gap
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is True
        assert any("KB gap" in l for l in result.learnings)

    def test_question_with_kb_match(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("customer_feedback", {
            "feedback_id": "fb-101",
            "feedback_category": "question",
            "message_preview": "What are the pricing tiers?",
        })
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": "What are the pricing tiers?",
              "category": "question", "page_url": "/pricing",
              "email": "test@test.example", "name": "Test User"}],
            [{"question": "What are the pricing tiers?", "answer": "Build/Grow/Scale"}],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_match"] is True
        assert result.output["kb_gap"] is False

    def test_feedback_not_found(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("customer_feedback", {"feedback_id": "missing"})
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(return_value=[])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success

    def test_unknown_category(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("unknown")
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert not result.success

    def test_bug_sends_alert(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("customer_feedback", {
            "feedback_id": "fb-alert",
            "feedback_category": "bug",
            "message_preview": "Page is broken",
        })
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": "Page is broken and stuck on loading",
              "category": "bug", "page_url": "/status",
              "email": "t@t.example", "name": "Tester"}],
            [],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        # "broken" and "stuck" are high-severity keywords
        assert result.output["severity"] == "high"


class TestAgentInitiative:
    def test_initiative_succeeds_with_no_feedback(self):
        """agent_initiative must not fail when there is no recent feedback.

        Regression: previously customer_service routed agent_initiative to
        _handle_feedback, which required a feedback_id. Initiative tasks have
        no feedback_id, so every run returned success=False, yielding a 100%
        failure rate and causing the self-learning loop to skip the agent.
        """
        agent = CustomerService()
        state = _make_state()
        task = _make_task("agent_initiative", {
            "agent": "customer_service",
            "initiative": "Review feedback patterns",
            "rotation_position": 0,
        })
        actuators = Actuators("customer_service", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(return_value=[])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["initiative"] == "feedback_pattern_review"
        assert result.output["feedback_reviewed"] == 0

    def test_initiative_surfaces_high_severity_bugs(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("agent_initiative", {"agent": "customer_service"})
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        # First query returns 7-day feedback; later queries are KB lookups.
        actuators.query_readonly = MagicMock(side_effect=[
            [
                {"category": "bug", "message": "App crashed on login", "page_url": "/login"},
                {"category": "bug", "message": "Everything is broken", "page_url": "/login"},
                {"category": "suggestion", "message": "Dark mode please", "page_url": "/settings"},
            ],
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["high_severity_bugs"] == 2
        assert result.output["bugs"] == 2
        assert any("high-severity" in l for l in result.learnings)

    def test_initiative_detects_kb_gap(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("agent_initiative", {"agent": "customer_service"})
        actuators = Actuators("customer_service", "test-task", dry_run=True)

        # 1st: recent feedback (1 question). 2nd: KB lookup returns empty.
        actuators.query_readonly = MagicMock(side_effect=[
            [{"category": "question", "message": "How do I export my data?",
              "page_url": "/dashboard"}],
            [],  # no KB match for the question
        ])

        result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap_count"] == 1
        assert any("KB gap" in l for l in result.learnings)


class TestRegistration:
    def test_register(self):
        with patch("coordinator.tick.register_agent") as mock_reg:
            from coordinator.agents.customer_service import register
            register()
            mock_reg.assert_called_once_with("customer_service", CustomerService)


class TestKbGapWriter:
    """The customer_service agent must close the support-KB loop: when a
    gap is detected, it drafts a Haiku-grounded answer AND inserts it
    into support_kb so the next keeper who asks the same thing gets a
    cached response.
    """

    def _feedback_question_task(self, message="How do I export my data?"):
        return _make_task("customer_feedback", {
            "feedback_id": "fb-gap",
            "feedback_category": "question",
            "message_preview": message,
        })

    def _setup_question_actuator(self, message="How do I export my data?", dry_run=False,
                                 model_calls="default"):
        # Live by default (a dry run drafts nothing), with a model-call
        # allowance unless the test passes its own; the alert is mocked and
        # each test mocks support_kb_insert, so nothing leaves the process.
        if model_calls == "default":
            model_calls = _allowance()
        actuators = Actuators("customer_service", "test-task", dry_run=dry_run,
                              model_calls=model_calls)
        actuators.send_alert = MagicMock(return_value=True)
        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": message, "category": "question",
              "page_url": "/dashboard", "email": "t@t.example", "name": "T"}],
            [],  # no KB match
        ])
        return actuators

    def test_kb_gap_calls_drafter_with_question_and_model(self):
        agent = CustomerService()
        state = _make_state()
        task = self._feedback_question_task("How do I export my data?")
        actuators = self._setup_question_actuator("How do I export my data?")

        with patch.object(
            CustomerService, "_draft_kb_answer",
            return_value="You can export your data from the dashboard as JSON.",
        ) as mock_draft, patch.object(
            actuators, "support_kb_insert", return_value=True,
        ) as mock_insert:
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is True
        assert result.output["kb_drafted"] is True
        mock_draft.assert_called_once()
        # First arg is question, second is model
        args, kwargs = mock_draft.call_args
        assert "export" in (args[0] if args else kwargs.get("question", ""))
        assert (args[1] if len(args) > 1 else kwargs.get("model")) == "claude-haiku-4-5"
        mock_insert.assert_called_once()
        insert_args, _ = mock_insert.call_args
        assert insert_args[0] == "How do I export my data?"
        assert "JSON" in insert_args[1]

    @pytest.mark.parametrize("model_calls", [None, ModelCallMeter(TickBudget())])
    def test_no_draft_without_a_model_call_allowance(self, model_calls):
        """Live and permitted, but the tick has no allowance for the call (or
        handed none over): the gap is reported and nothing is drafted."""
        agent = CustomerService()
        task = self._feedback_question_task()
        actuators = self._setup_question_actuator(model_calls=model_calls)

        with patch.object(
            CustomerService, "_draft_kb_answer", return_value="An answer.",
        ) as mock_draft, patch.object(
            actuators, "support_kb_insert", return_value=True,
        ) as mock_insert:
            result = agent.execute(task, _make_state(), "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is True
        assert result.output["kb_drafted"] is False
        mock_draft.assert_not_called()
        mock_insert.assert_not_called()

    def test_dry_run_asks_no_model_and_reports_no_draft(self):
        """A dry run (the coordinator's default) spends nothing: the gap is
        still reported, but no draft is requested or stored."""
        agent = CustomerService()
        state = _make_state()
        task = self._feedback_question_task()
        actuators = self._setup_question_actuator(dry_run=True)

        with patch.object(
            CustomerService, "_draft_kb_answer", return_value="An answer.",
        ) as mock_draft, patch.object(
            actuators, "support_kb_insert", return_value=True,
        ) as mock_insert:
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is True
        assert result.output["kb_drafted"] is False
        mock_draft.assert_not_called()
        mock_insert.assert_not_called()
        assert [a.details for a in actuators.actions if a.action_type == "model_call"] == [
            {"purpose": "support_kb_draft", "dry_run": True}]

    def test_kb_gap_skips_insert_when_drafter_returns_none(self):
        """Graceful degrade: when ANTHROPIC_API_KEY is missing or the
        Haiku call fails, the agent must NOT attempt the insert and must
        NOT mark the gap as drafted. Triage still succeeds.
        """
        agent = CustomerService()
        state = _make_state()
        task = self._feedback_question_task()
        actuators = self._setup_question_actuator()

        with patch.object(
            CustomerService, "_draft_kb_answer", return_value=None,
        ), patch.object(
            actuators, "support_kb_insert", return_value=True,
        ) as mock_insert:
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is True
        assert result.output["kb_drafted"] is False
        mock_insert.assert_not_called()

    def test_kb_match_does_not_call_drafter(self):
        """No gap, no draft. Drafter must not fire when KB already has
        an answer for this question."""
        agent = CustomerService()
        state = _make_state()
        task = self._feedback_question_task("What are the prices?")
        actuators = Actuators("customer_service", "test-task", dry_run=True)
        actuators.query_readonly = MagicMock(side_effect=[
            [{"message": "What are the prices?", "category": "question",
              "page_url": "/pricing", "email": "t@t.example", "name": "T"}],
            [{"question": "What are the prices?", "answer": "$19-$39 a month"}],
        ])

        with patch.object(
            CustomerService, "_draft_kb_answer", return_value="x",
        ) as mock_draft:
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is False
        assert result.output["kb_drafted"] is False
        mock_draft.assert_not_called()

    def test_kb_gap_swallows_actuator_permission_error(self):
        agent = CustomerService()
        state = _make_state()
        task = self._feedback_question_task()
        actuators = self._setup_question_actuator()

        with patch.object(
            CustomerService, "_draft_kb_answer", return_value="An answer.",
        ), patch.object(
            actuators, "support_kb_insert",
            side_effect=PermissionError("denied"),
        ):
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap"] is True
        assert result.output["kb_drafted"] is False

    def test_initiative_drafts_up_to_cap_and_skips_capped_gaps(self):
        from coordinator.agents.customer_service import _KB_GAP_DRAFT_CAP_PER_RUN

        # Build N+2 unanswered question rows to confirm the cap.
        excess = _KB_GAP_DRAFT_CAP_PER_RUN + 2
        questions_feedback = [
            {"category": "question", "message": f"Question number {i}",
             "page_url": "/x"}
            for i in range(excess)
        ]
        # KB lookups: every one returns empty (gap on every question).
        kb_lookups = [[] for _ in range(excess)]

        agent = CustomerService()
        state = _make_state()
        task = _make_task("agent_initiative", {"agent": "customer_service"})
        actuators = Actuators("customer_service", "test-task", dry_run=False,
                              model_calls=_allowance())
        actuators.send_alert = MagicMock(return_value=True)
        actuators.query_readonly = MagicMock(
            side_effect=[questions_feedback, *kb_lookups]
        )

        with patch.object(
            CustomerService, "_draft_kb_answer",
            return_value="Drafted reply.",
        ) as mock_draft, patch.object(
            actuators, "support_kb_insert", return_value=True,
        ) as mock_insert:
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap_count"] == excess
        assert result.output["kb_drafted_count"] == _KB_GAP_DRAFT_CAP_PER_RUN
        assert mock_draft.call_count == _KB_GAP_DRAFT_CAP_PER_RUN
        assert mock_insert.call_count == _KB_GAP_DRAFT_CAP_PER_RUN

    def test_initiative_no_draft_when_no_gaps(self):
        agent = CustomerService()
        state = _make_state()
        task = _make_task("agent_initiative", {"agent": "customer_service"})
        actuators = Actuators("customer_service", "test-task", dry_run=True)
        # Recent feedback: 1 question; KB lookup: matched.
        actuators.query_readonly = MagicMock(side_effect=[
            [{"category": "question", "message": "Already answered?",
              "page_url": "/x"}],
            [{"question": "Already answered?"}],
        ])

        with patch.object(
            CustomerService, "_draft_kb_answer", return_value="x",
        ) as mock_draft:
            result = agent.execute(task, state, "claude-haiku-4-5", actuators)

        assert result.success
        assert result.output["kb_gap_count"] == 0
        assert result.output["kb_drafted_count"] == 0
        mock_draft.assert_not_called()


class TestDraftKbAnswer:
    """Behavior of CustomerService._draft_kb_answer."""

    def test_returns_none_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        agent = CustomerService()
        assert agent._draft_kb_answer("How do I export?", "claude-haiku-4-5") is None

    def test_returns_none_for_empty_question(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
        agent = CustomerService()
        assert agent._draft_kb_answer("", "claude-haiku-4-5") is None
        assert agent._draft_kb_answer("   ", "claude-haiku-4-5") is None

    def test_returns_text_from_anthropic_response(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
        # Build a fake anthropic module with the expected call shape.
        fake_block = MagicMock(text="You can export from the dashboard.")
        fake_response = MagicMock(content=[fake_block])
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client

        import sys
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        agent = CustomerService()
        result = agent._draft_kb_answer(
            "How do I export?", "claude-haiku-4-5"
        )
        assert result == "You can export from the dashboard."

        # Confirm the model + messages shape
        call = fake_client.messages.create.call_args
        kwargs = call.kwargs
        assert kwargs["model"] == "claude-haiku-4-5"
        assert kwargs["messages"][0]["role"] == "user"
        assert "export" in kwargs["messages"][0]["content"]
        # System prompt must include the support-knowledge corpus
        assert "Craft & Ship" in kwargs["system"]

    def test_swallows_anthropic_exception(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stub-key")
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("boom")
        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client

        import sys
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        agent = CustomerService()
        assert agent._draft_kb_answer("Q?", "claude-haiku-4-5") is None


class TestSupportKbInsertActuator:
    """Behavior of Actuators.support_kb_insert."""

    def test_dry_run_logs_and_records_without_db(self):
        act = Actuators("customer_service", "task-x", dry_run=True)
        ok = act.support_kb_insert("How do I export?", "Use the dashboard.")
        assert ok is True
        assert any(a.action_type == "support_kb_insert" for a in act.actions)
        action = [a for a in act.actions if a.action_type == "support_kb_insert"][0]
        assert action.details["dry_run"] is True

    @pytest.mark.parametrize("analysis_only", ["", "true"])
    def test_dry_run_never_opens_a_connection(self, monkeypatch, analysis_only):
        """support_kb rows are served to customers. With a database configured,
        a dry run must not write one, and COORDINATOR_ANALYSIS_ONLY does not
        change that (it used to: the insert ran under dry run in that mode)."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/does_not_matter")
        monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", analysis_only)
        act = Actuators("customer_service", "task-x", dry_run=True)
        with patch("psycopg2.connect") as connect:
            ok = act.support_kb_insert("How do I export?", "Use the dashboard.")
        connect.assert_not_called()
        assert ok is True
        assert act.actions[-1].details["dry_run"] is True

    def test_live_insert_in_analysis_only_mode_writes_the_row(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/does_not_matter")
        monkeypatch.setenv("COORDINATOR_ANALYSIS_ONLY", "true")
        act = Actuators("customer_service", "task-x", dry_run=False)
        with patch("psycopg2.connect") as connect:
            connect.return_value.cursor.return_value.fetchone.return_value = ("row-id",)
            assert act.support_kb_insert("How do I export?", "Use the dashboard.") is True
        sql = connect.return_value.cursor.return_value.execute.call_args.args[0]
        assert "INSERT INTO support_kb" in sql
        connect.return_value.commit.assert_called_once()

    def test_rejects_when_agent_lacks_kb_record_permission(self):
        # Architect agent does NOT have kb_record permission.
        act = Actuators("architect", "task-x", dry_run=True)
        with pytest.raises(PermissionError):
            act.support_kb_insert("Q", "A")

    def test_skips_empty_question_or_answer(self):
        act = Actuators("customer_service", "task-x", dry_run=True)
        assert act.support_kb_insert("", "A") is False
        assert act.support_kb_insert("Q", "") is False
        assert act.support_kb_insert("   ", "A") is False

    def test_infra_engineer_can_call_support_kb_insert(self):
        # infra_engineer also has kb_record permission; the actuator must
        # accept the call from any agent in AGENT_PERMISSIONS with kb_record.
        act = Actuators("infra_engineer", "task-x", dry_run=True)
        assert act.support_kb_insert("Q", "A") is True


class TestSupportKnowledgeCorpus:
    def test_normalize_question_lower_strips_punct_collapses_ws(self):
        from coordinator.support_knowledge import normalize_question

        assert normalize_question("How do I Export?  My data!!") == \
            "how do i export my data"
        assert normalize_question("") == ""
        assert normalize_question("   \n  ") == ""

    def test_generate_knowledge_carries_pricing_constants(self):
        from coordinator.support_knowledge import (
            generate_knowledge, HARBOR_DEFAULT_PRICE, HARBOR_FLOOR_PRICE,
        )

        body = generate_knowledge()
        assert HARBOR_DEFAULT_PRICE in body
        assert HARBOR_FLOOR_PRICE in body
        assert "Craft & Ship" in body
        # Brand voice: no em dashes
        assert "\u2014" not in body
