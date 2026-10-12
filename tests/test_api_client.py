"""Tests for cli/api_client.py, cost tracking foundation.

Section 01 tests cover the dataclass, pricing table, and estimate_cost().
Section 02 adds tests for call_api(), retry, continuation, etc.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "cli"))

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock
import anthropic


class TestApiCallMetrics:
    """Verify the ApiCallMetrics dataclass has all required fields."""

    def test_metrics_creation(self):
        from api_client import ApiCallMetrics
        m = ApiCallMetrics(
            module="build",
            model="claude-opus-4-6",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
            api_mode="realtime",
            estimated_cost_usd=0.0175,
            build_id="abc-123",
            stage="build_phase_1",
            timestamp=datetime(2026, 3, 30, 12, 0, 0),
        )
        assert m.module == "build"
        assert m.model == "claude-opus-4-6"
        assert m.input_tokens == 1000
        assert m.output_tokens == 500
        assert m.cache_read_tokens == 200
        assert m.cache_write_tokens == 100
        assert m.api_mode == "realtime"
        assert m.build_id == "abc-123"
        assert m.stage == "build_phase_1"

    def test_metrics_field_types(self):
        from api_client import ApiCallMetrics
        m = ApiCallMetrics(
            module="review", model="claude-sonnet-4-6",
            input_tokens=500, output_tokens=200,
            cache_read_tokens=0, cache_write_tokens=0,
            api_mode="batch", estimated_cost_usd=1.23,
            build_id="def-456", stage="review",
            timestamp=datetime.now(),
        )
        assert isinstance(m.input_tokens, int)
        assert isinstance(m.output_tokens, int)
        assert isinstance(m.estimated_cost_usd, float)
        assert isinstance(m.module, str)
        assert isinstance(m.timestamp, datetime)


class TestPricingTable:
    """Verify the PRICING dict covers all model/mode/token_type combinations."""

    def test_pricing_completeness(self):
        from api_client import PRICING
        models = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]
        modes = ["realtime", "batch"]
        token_types = ["input", "output"]
        for model in models:
            for mode in modes:
                for tt in token_types:
                    key = (model, mode, tt)
                    assert key in PRICING, f"Missing pricing for {key}"
                    assert PRICING[key] > 0, f"Pricing for {key} must be positive"

    def test_opus_realtime_rates(self):
        from api_client import PRICING
        assert PRICING[("claude-opus-4-6", "realtime", "input")] == pytest.approx(5.00 / 1_000_000)
        assert PRICING[("claude-opus-4-6", "realtime", "output")] == pytest.approx(25.00 / 1_000_000)

    def test_opus_batch_rates(self):
        from api_client import PRICING
        assert PRICING[("claude-opus-4-6", "batch", "input")] == pytest.approx(2.50 / 1_000_000)
        assert PRICING[("claude-opus-4-6", "batch", "output")] == pytest.approx(12.50 / 1_000_000)

    def test_sonnet_realtime_rates(self):
        from api_client import PRICING
        assert PRICING[("claude-sonnet-4-6", "realtime", "input")] == pytest.approx(3.00 / 1_000_000)
        assert PRICING[("claude-sonnet-4-6", "realtime", "output")] == pytest.approx(15.00 / 1_000_000)

    def test_haiku_realtime_rates(self):
        from api_client import PRICING
        assert PRICING[("claude-haiku-4-5", "realtime", "input")] == pytest.approx(1.00 / 1_000_000)
        assert PRICING[("claude-haiku-4-5", "realtime", "output")] == pytest.approx(5.00 / 1_000_000)

    def test_batch_is_half_price(self):
        from api_client import PRICING
        for model in ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]:
            for tt in ["input", "output"]:
                batch = PRICING[(model, "batch", tt)]
                realtime = PRICING[(model, "realtime", tt)]
                assert batch == pytest.approx(realtime * 0.5), f"{model} batch {tt} should be 50% of realtime"


class TestEstimateCost:
    """Verify estimate_cost() correctly computes USD from token counts."""

    def test_realtime_opus_input(self):
        from api_client import estimate_cost
        # 1000 input tokens at $5/MTok = $0.005
        cost = estimate_cost("claude-opus-4-6", "realtime", input_tokens=1000, output_tokens=0)
        assert cost == pytest.approx(0.005)

    def test_batch_opus_input(self):
        from api_client import estimate_cost
        # 1000 input tokens at $2.50/MTok = $0.0025
        cost = estimate_cost("claude-opus-4-6", "batch", input_tokens=1000, output_tokens=0)
        assert cost == pytest.approx(0.0025)

    def test_cache_read_tokens(self):
        from api_client import estimate_cost
        # 1000 cache_read tokens at 10% of realtime input ($5/MTok) = $0.0005
        cost = estimate_cost("claude-opus-4-6", "realtime",
                             input_tokens=0, output_tokens=0, cache_read_tokens=1000)
        assert cost == pytest.approx(0.0005)

    def test_cache_write_tokens(self):
        from api_client import estimate_cost
        # 1000 cache_write tokens at 125% of realtime input ($5/MTok) = $0.00625
        cost = estimate_cost("claude-opus-4-6", "realtime",
                             input_tokens=0, output_tokens=0, cache_write_tokens=1000)
        assert cost == pytest.approx(0.00625)

    def test_cache_uses_realtime_rate_even_in_batch_mode(self):
        from api_client import estimate_cost
        # Cache read in batch mode still uses realtime input rate
        cost = estimate_cost("claude-opus-4-6", "batch",
                             input_tokens=0, output_tokens=0, cache_read_tokens=1000)
        # 1000 * ($5/MTok * 0.1) = $0.0005
        assert cost == pytest.approx(0.0005)

    def test_haiku_output_realtime(self):
        from api_client import estimate_cost
        # 1000 output tokens at $5/MTok = $0.005
        cost = estimate_cost("claude-haiku-4-5", "realtime", input_tokens=0, output_tokens=1000)
        assert cost == pytest.approx(0.005)

    def test_combined_cost(self):
        from api_client import estimate_cost
        # Opus realtime: 1000 input ($0.005) + 500 output ($0.0125) = $0.0175
        cost = estimate_cost("claude-opus-4-6", "realtime", input_tokens=1000, output_tokens=500)
        assert cost == pytest.approx(0.0175)

    def test_combined_cost_with_cache(self):
        from api_client import estimate_cost
        # Opus realtime: 1000 input + 500 output + 800 cache_read + 200 cache_write
        cost = estimate_cost("claude-opus-4-6", "realtime",
                             input_tokens=1000, output_tokens=500,
                             cache_read_tokens=800, cache_write_tokens=200)
        expected = (
            1000 * 5.00 / 1_000_000      # input
            + 500 * 25.00 / 1_000_000    # output
            + 800 * 5.00 / 1_000_000 * 0.1   # cache read
            + 200 * 5.00 / 1_000_000 * 1.25  # cache write
        )
        assert cost == pytest.approx(expected)

    def test_unknown_model_returns_zero(self):
        from api_client import estimate_cost
        cost = estimate_cost("claude-unknown-99", "realtime", input_tokens=1000, output_tokens=500)
        assert cost == 0.0

    def test_unknown_mode_returns_zero(self):
        from api_client import estimate_cost
        cost = estimate_cost("claude-opus-4-6", "streaming", input_tokens=1000, output_tokens=500)
        assert cost == 0.0


# ---------------------------------------------------------------------------
# Section 02: call_api() tests
# ---------------------------------------------------------------------------

def _mock_response(text="OK", stop_reason="end_turn", input_tokens=1000, output_tokens=500):
    """Create a mock Anthropic API response."""
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return resp


class TestCallApiRealtime:
    """Realtime core behavior of call_api."""

    def test_delegates_to_messages_create(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        response, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")

        client.messages.create.assert_called_once()
        assert response.content[0].text == "OK"

    def test_passes_all_parameters(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            system="test system", max_tokens=4096, temperature=0.5,
        )

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-6"
        assert kwargs["max_tokens"] == 4096
        assert kwargs["system"] == "test system"
        assert kwargs["temperature"] == 0.5

    @patch("api_client.anthropic.Anthropic")
    def test_accepts_client_as_parameter(self, mock_constructor):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")

        # call_api should never create its own Anthropic client
        mock_constructor.assert_not_called()

    def test_returns_tuple_of_response_and_metrics(self):
        from api_client import call_api, ApiCallMetrics
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        result = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            module="build", build_id="abc-123", stage="build_phase_1",
        )

        assert isinstance(result, tuple)
        assert len(result) == 2
        response, metrics = result
        assert isinstance(metrics, ApiCallMetrics)
        assert metrics.module == "build"
        assert metrics.build_id == "abc-123"
        assert metrics.stage == "build_phase_1"
        assert metrics.api_mode == "realtime"

    @patch("api_client.time.sleep")
    def test_never_calls_sys_exit(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.side_effect = anthropic.BadRequestError(
            message="bad request",
            response=MagicMock(status_code=400),
            body={"error": {"message": "bad"}},
        )

        with patch("sys.exit") as mock_exit:
            with pytest.raises(anthropic.BadRequestError):
                call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
            mock_exit.assert_not_called()


class TestCallApiRetry:
    """Retry logic for call_api."""

    @patch("api_client.time.sleep")
    def test_rate_limit_retries(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        error = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body={"error": {"message": "rate limited"}},
        )
        client.messages.create.side_effect = [error, error, _mock_response()]

        response, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == 3
        assert response.content[0].text == "OK"

    @patch("api_client.time.sleep")
    def test_internal_server_error_retries(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        error = anthropic.InternalServerError(
            message="internal",
            response=MagicMock(status_code=500),
            body={"error": {"message": "internal"}},
        )
        client.messages.create.side_effect = [error, _mock_response()]

        response, _ = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == 2

    @patch("api_client.time.sleep")
    def test_max_retries_raises(self, mock_sleep):
        from api_client import call_api, MAX_RETRIES
        client = MagicMock()
        error = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body={"error": {"message": "rate limited"}},
        )
        client.messages.create.side_effect = error

        with pytest.raises(anthropic.RateLimitError):
            call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == MAX_RETRIES

    def test_non_retryable_raises_immediately(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.side_effect = anthropic.BadRequestError(
            message="bad",
            response=MagicMock(status_code=400),
            body={"error": {"message": "bad"}},
        )

        with pytest.raises(anthropic.BadRequestError):
            call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == 1

    @patch("api_client.time.sleep")
    def test_exponential_backoff(self, mock_sleep):
        from api_client import call_api, INITIAL_BACKOFF_SECONDS
        client = MagicMock()
        error = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body={"error": {"message": "rate limited"}},
        )
        client.messages.create.side_effect = [error, error, _mock_response()]

        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")

        sleeps = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleeps[0] == INITIAL_BACKOFF_SECONDS
        assert sleeps[1] == INITIAL_BACKOFF_SECONDS * 2


class TestCallApiContinuation:
    """Token continuation behavior."""

    @patch("api_client.time.sleep")
    def test_end_turn_no_continuation(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="complete", stop_reason="end_turn")

        response, _ = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == 1
        assert response.content[0].text == "complete"

    @patch("api_client.time.sleep")
    def test_max_tokens_triggers_continuation(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        first = _mock_response(text="partial...", stop_reason="max_tokens")
        second = _mock_response(text="...rest", stop_reason="end_turn")
        client.messages.create.side_effect = [first, second]

        response, _ = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == 2
        assert "partial..." in response.content[0].text
        assert "...rest" in response.content[0].text

    @patch("api_client.time.sleep")
    def test_multiple_continuations(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        r1 = _mock_response(text="part1", stop_reason="max_tokens", input_tokens=100, output_tokens=50)
        r2 = _mock_response(text="part2", stop_reason="max_tokens", input_tokens=80, output_tokens=40)
        r3 = _mock_response(text="part3", stop_reason="end_turn", input_tokens=60, output_tokens=30)
        client.messages.create.side_effect = [r1, r2, r3]

        response, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert client.messages.create.call_count == 3
        assert "part1" in response.content[0].text
        assert "part3" in response.content[0].text

    @patch("api_client.time.sleep")
    def test_continuation_accumulates_tokens(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        r1 = _mock_response(text="p1", stop_reason="max_tokens", input_tokens=1000, output_tokens=500)
        r2 = _mock_response(text="p2", stop_reason="end_turn", input_tokens=800, output_tokens=400)
        client.messages.create.side_effect = [r1, r2]

        _, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert metrics.input_tokens == 1800
        assert metrics.output_tokens == 900

    @patch("api_client.time.sleep")
    def test_no_continuation_when_disabled(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="truncated", stop_reason="max_tokens")

        response, _ = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            handle_continuation=False,
        )
        assert client.messages.create.call_count == 1


class TestCallApiMetrics:
    """Cost metrics returned by call_api."""

    def test_metrics_populated_from_usage(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response(input_tokens=1000, output_tokens=500)

        _, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            module="build", build_id="abc", stage="phase1",
        )
        assert metrics.input_tokens == 1000
        assert metrics.output_tokens == 500
        assert metrics.model == "claude-opus-4-6"
        assert metrics.api_mode == "realtime"

    def test_cache_tokens_captured(self):
        from api_client import call_api
        client = MagicMock()
        resp = _mock_response()
        resp.usage.cache_creation_input_tokens = 200
        resp.usage.cache_read_input_tokens = 800
        client.messages.create.return_value = resp

        _, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert metrics.cache_write_tokens == 200
        assert metrics.cache_read_tokens == 800

    def test_cache_tokens_default_zero(self):
        from api_client import call_api
        client = MagicMock()
        resp = _mock_response()
        # Simulate missing cache fields
        resp.usage.cache_creation_input_tokens = 0
        resp.usage.cache_read_input_tokens = 0
        client.messages.create.return_value = resp

        _, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert metrics.cache_read_tokens == 0
        assert metrics.cache_write_tokens == 0

    def test_cost_calculated_for_opus_realtime(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response(input_tokens=1000, output_tokens=500)

        _, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        # (1000 * 5.00/1M) + (500 * 25.00/1M) = 0.005 + 0.0125 = 0.0175
        assert metrics.estimated_cost_usd == pytest.approx(0.0175)

    def test_metrics_include_timestamp(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        _, metrics = call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        assert isinstance(metrics.timestamp, datetime)


# ---------------------------------------------------------------------------
# Section 05: Batch API tests
# ---------------------------------------------------------------------------

def _mock_batch_result(text="OK", input_tokens=1000, output_tokens=500):
    """Create a mock batch result with a succeeded message."""
    message = _mock_response(text=text, input_tokens=input_tokens, output_tokens=output_tokens)
    result_obj = MagicMock()
    result_obj.result.type = "succeeded"
    result_obj.result.message = message
    return result_obj


class TestTierConfig:
    """Tier-based batch configuration."""

    def test_build_tier_no_batch(self):
        from api_client import should_use_batch
        assert should_use_batch("build") is False

    def test_grow_tier_uses_batch(self):
        from api_client import should_use_batch
        assert should_use_batch("grow") is True

    def test_scale_tier_uses_batch(self):
        from api_client import should_use_batch
        assert should_use_batch("scale") is True

    def test_unknown_tier_defaults_false(self):
        from api_client import should_use_batch
        assert should_use_batch("enterprise") is False

    def test_disable_batch_env_overrides(self):
        from api_client import should_use_batch
        with patch.dict("os.environ", {"DISABLE_BATCH_API": "true"}):
            assert should_use_batch("grow") is False
            assert should_use_batch("scale") is False

    def test_disable_batch_case_insensitive(self):
        from api_client import should_use_batch
        with patch.dict("os.environ", {"DISABLE_BATCH_API": "True"}):
            assert should_use_batch("grow") is False

    def test_disable_batch_false_allows_batch(self):
        from api_client import should_use_batch
        with patch.dict("os.environ", {"DISABLE_BATCH_API": "false"}):
            assert should_use_batch("grow") is True


class TestBatchMode:
    """Batch API submit, poll, and fallback."""

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_batch_calls_batches_create(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_123"
        client.messages.batches.create.return_value = batch_obj

        status = MagicMock()
        status.processing_status = "ended"
        client.messages.batches.retrieve.return_value = status
        client.messages.batches.results.return_value = [_mock_batch_result()]

        mock_mono.side_effect = [0, 5]  # start=0, elapsed=5 (under 300s limit)

        response, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            use_batch=True,
        )

        client.messages.batches.create.assert_called_once()
        assert response.content[0].text == "OK"
        assert metrics.api_mode == "batch"

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_batch_polls_with_backoff(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_123"
        client.messages.batches.create.return_value = batch_obj

        in_progress = MagicMock()
        in_progress.processing_status = "in_progress"
        ended = MagicMock()
        ended.processing_status = "ended"
        client.messages.batches.retrieve.side_effect = [in_progress, in_progress, ended]
        client.messages.batches.results.return_value = [_mock_batch_result()]

        # monotonic: start=0, then checks at 11, 31, 71 (all under 300)
        mock_mono.side_effect = [0, 11, 31, 71]

        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6", use_batch=True)

        assert client.messages.batches.retrieve.call_count == 3
        sleeps = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleeps[0] == 10   # initial
        assert sleeps[1] == 20   # doubled
        assert sleeps[2] == 40   # doubled again

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_batch_timeout_cancels_and_falls_back(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_123"
        client.messages.batches.create.return_value = batch_obj

        in_progress = MagicMock()
        in_progress.processing_status = "in_progress"
        client.messages.batches.retrieve.return_value = in_progress

        # Exceed MAX_BATCH_WAIT_SECONDS on second check
        mock_mono.side_effect = [0, 301]

        client.messages.create.return_value = _mock_response(text="fallback")

        response, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            use_batch=True,
        )

        client.messages.batches.cancel.assert_called_once_with("batch_123")
        client.messages.create.assert_called_once()
        assert response.content[0].text == "fallback"
        assert metrics.api_mode == "realtime"

    @patch("api_client.time.sleep")
    def test_batch_creation_failure_falls_back(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        client.messages.batches.create.side_effect = anthropic.BadRequestError(
            message="bad", response=MagicMock(status_code=400),
            body={"error": {"message": "bad"}},
        )
        client.messages.create.return_value = _mock_response(text="realtime_fallback")

        response, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            use_batch=True,
        )

        assert response.content[0].text == "realtime_fallback"
        assert metrics.api_mode == "realtime"

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_watchdog_timeout_cancels_and_reraises(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_456"
        client.messages.batches.create.return_value = batch_obj

        mock_mono.side_effect = [0, 5]
        mock_sleep.side_effect = TimeoutError("watchdog")

        with pytest.raises(TimeoutError):
            call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6", use_batch=True)

        client.messages.batches.cancel.assert_called_once_with("batch_456")

    @patch("api_client.time.sleep")
    def test_disable_batch_env_forces_realtime(self, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        with patch.dict("os.environ", {"DISABLE_BATCH_API": "true"}):
            _, metrics = call_api(
                client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
                use_batch=True,
            )

        client.messages.batches.create.assert_not_called()
        client.messages.create.assert_called_once()
        assert metrics.api_mode == "realtime"

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_batch_cost_uses_batch_pricing(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_789"
        client.messages.batches.create.return_value = batch_obj

        status = MagicMock()
        status.processing_status = "ended"
        client.messages.batches.retrieve.return_value = status
        client.messages.batches.results.return_value = [
            _mock_batch_result(input_tokens=1000, output_tokens=500)
        ]
        mock_mono.side_effect = [0, 5]

        _, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            use_batch=True,
        )

        # Batch Opus: (1000 * 2.50/1M) + (500 * 12.50/1M) = 0.0025 + 0.00625 = 0.00875
        assert metrics.estimated_cost_usd == pytest.approx(0.00875)
        assert metrics.api_mode == "batch"

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_network_error_during_poll_retries_poll(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_net"
        client.messages.batches.create.return_value = batch_obj

        ended = MagicMock()
        ended.processing_status = "ended"
        client.messages.batches.retrieve.side_effect = [
            anthropic.APIConnectionError(request=MagicMock()),
            ended,
        ]
        client.messages.batches.results.return_value = [_mock_batch_result()]
        mock_mono.side_effect = [0, 11, 31]

        response, _ = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
            use_batch=True,
        )

        assert client.messages.batches.retrieve.call_count == 2
        assert client.messages.batches.create.call_count == 1
        assert response.content[0].text == "OK"


class TestMetricsCollector:
    """Thread-local metrics collection."""

    def test_start_and_get_collects_metrics(self):
        from api_client import call_api, start_metrics_collection, get_collected_metrics
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        start_metrics_collection()
        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        call_api(client, [{"role": "user", "content": "bye"}], "claude-opus-4-6")
        metrics = get_collected_metrics()

        assert len(metrics) == 2
        assert all(m.model == "claude-opus-4-6" for m in metrics)

    def test_no_collection_without_start(self):
        from api_client import call_api, get_collected_metrics
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        metrics = get_collected_metrics()

        assert len(metrics) == 0

    def test_get_clears_and_stops(self):
        from api_client import call_api, start_metrics_collection, get_collected_metrics
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        start_metrics_collection()
        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")
        first = get_collected_metrics()
        assert len(first) == 1

        # After get, collection is stopped
        call_api(client, [{"role": "user", "content": "bye"}], "claude-opus-4-6")
        second = get_collected_metrics()
        assert len(second) == 0

    def test_total_cost_sum(self):
        from api_client import call_api, start_metrics_collection, get_collected_metrics
        client = MagicMock()
        client.messages.create.return_value = _mock_response(input_tokens=1000, output_tokens=500)

        start_metrics_collection()
        call_api(client, [{"role": "user", "content": "a"}], "claude-opus-4-6")
        call_api(client, [{"role": "user", "content": "b"}], "claude-opus-4-6")
        metrics = get_collected_metrics()

        total = sum(m.estimated_cost_usd for m in metrics)
        # Each call: (1000*5/1M) + (500*25/1M) = 0.0175; two calls = 0.035
        assert total == pytest.approx(0.035)


# ---------------------------------------------------------------------------
# Advisor mode tests
# ---------------------------------------------------------------------------

def _mock_advisor_response(
    texts=("part1", "part2"),
    stop_reason="end_turn",
    input_tokens=1000,
    output_tokens=500,
    advisor_input_tokens=800,
    advisor_output_tokens=200,
):
    """Create a mock response with interleaved text and advisor blocks.

    Returns a response where content contains TextBlock and advisor tool
    blocks, and usage includes an iterations array for split billing.
    """
    content = []
    for t in texts:
        block = MagicMock()
        block.type = "text"
        block.text = t
        content.append(block)
    # Interleave an advisor tool_use and tool_result block
    tool_use = MagicMock()
    tool_use.type = "server_tool_use"
    tool_use.name = "advisor"
    content.insert(1, tool_use)
    tool_result = MagicMock()
    tool_result.type = "tool_result"
    tool_result.is_error = False
    content.insert(2, tool_result)

    resp = MagicMock()
    resp.content = content
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(
        input_tokens=input_tokens + advisor_input_tokens,
        output_tokens=output_tokens + advisor_output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    # Add iterations for split billing
    resp.usage.iterations = [
        {"type": "message", "input_tokens": input_tokens, "output_tokens": output_tokens},
        {"type": "advisor_message", "input_tokens": advisor_input_tokens, "output_tokens": advisor_output_tokens},
    ]
    return resp


class TestAdvisorMode:
    """Advisor tool integration in call_api."""

    def test_advisor_model_injects_advisor_tool(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        kwargs = client.beta.messages.create.call_args.kwargs
        tools = kwargs.get("tools", [])
        advisor_tools = [t for t in tools if t.get("type") == "advisor_20260301"]
        assert len(advisor_tools) == 1
        assert advisor_tools[0]["name"] == "advisor"
        assert advisor_tools[0]["model"] == "claude-opus-4-6"
        assert advisor_tools[0]["max_uses"] == 2

    def test_advisor_model_uses_beta_endpoint(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        client.beta.messages.create.assert_called_once()
        client.messages.create.assert_not_called()

    def test_advisor_model_passes_betas_param(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        kwargs = client.beta.messages.create.call_args.kwargs
        assert "advisor-tool-2026-03-01" in kwargs.get("betas", [])

    def test_no_advisor_uses_standard_endpoint(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        call_api(client, [{"role": "user", "content": "hi"}], "claude-opus-4-6")

        client.messages.create.assert_called_once()
        client.beta.messages.create.assert_not_called()
        kwargs = client.messages.create.call_args.kwargs
        tools = kwargs.get("tools", [])
        advisor_tools = [t for t in tools if isinstance(t, dict) and t.get("type") == "advisor_20260301"]
        assert len(advisor_tools) == 0

    def test_advisor_with_continuation_raises_valueerror(self):
        from api_client import call_api
        client = MagicMock()

        with pytest.raises(ValueError, match="not compatible with token continuation"):
            call_api(
                client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
                advisor_model="claude-opus-4-6",
                handle_continuation=True,
            )

    def test_advisor_without_continuation_no_error(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        # Should not raise
        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

    def test_response_normalization_strips_advisor_blocks(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response(texts=("part1", "part2"))

        response, _ = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        # Normalization should concatenate text blocks and strip advisor blocks
        assert response.content[0].text == "part1part2"

    def test_response_normalization_handles_text_only(self):
        from api_client import call_api
        client = MagicMock()
        # Response with only text blocks (no advisor invocation)
        resp = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "just text"
        resp.content = [text_block]
        resp.stop_reason = "end_turn"
        resp.usage = MagicMock(
            input_tokens=1000, output_tokens=500,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        resp.usage.iterations = []
        client.beta.messages.create.return_value = resp

        response, _ = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        assert response.content[0].text == "just text"

    def test_advisor_error_block_does_not_crash(self):
        from api_client import call_api
        client = MagicMock()
        resp = _mock_advisor_response(texts=("before", "after"))
        # Add an error tool_result block
        error_block = MagicMock()
        error_block.type = "tool_result"
        error_block.is_error = True
        resp.content.append(error_block)
        client.beta.messages.create.return_value = resp

        response, _ = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        assert response.content[0].text == "beforeafter"

    def test_custom_advisor_max_uses(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            advisor_max_uses=5,
            handle_continuation=False,
        )

        kwargs = client.beta.messages.create.call_args.kwargs
        advisor_tools = [t for t in kwargs["tools"] if t.get("type") == "advisor_20260301"]
        assert advisor_tools[0]["max_uses"] == 5

    def test_advisor_caching_in_tool_declaration(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            advisor_caching={"type": "ephemeral", "ttl": "5m"},
            handle_continuation=False,
        )

        kwargs = client.beta.messages.create.call_args.kwargs
        advisor_tools = [t for t in kwargs["tools"] if t.get("type") == "advisor_20260301"]
        assert advisor_tools[0]["caching"] == {"type": "ephemeral", "ttl": "5m"}

    def test_no_caching_when_not_set(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        kwargs = client.beta.messages.create.call_args.kwargs
        advisor_tools = [t for t in kwargs["tools"] if t.get("type") == "advisor_20260301"]
        assert "caching" not in advisor_tools[0]

    def test_advisor_tool_prepended_to_existing_tools(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response()

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
            tools=[{"type": "custom_tool", "name": "my_tool"}],
        )

        kwargs = client.beta.messages.create.call_args.kwargs
        tools = kwargs["tools"]
        assert tools[0]["type"] == "advisor_20260301"
        assert tools[1]["type"] == "custom_tool"


class TestAdvisorBilling:
    """Split billing and cost calculation for advisor calls."""

    def test_extract_advisor_usage_parses_iterations(self):
        from api_client import _extract_advisor_usage
        resp = MagicMock()
        resp.usage.iterations = [
            {"type": "message", "input_tokens": 5000, "output_tokens": 3000},
            {"type": "advisor_message", "input_tokens": 8000, "output_tokens": 500},
            {"type": "message", "input_tokens": 2000, "output_tokens": 1000},
        ]

        result = _extract_advisor_usage(resp)
        assert result["advisor_input_tokens"] == 8000
        assert result["advisor_output_tokens"] == 500
        assert result["executor_input_tokens"] == 7000
        assert result["executor_output_tokens"] == 4000

    def test_estimate_cost_with_advisor_split_billing(self):
        from api_client import estimate_cost_with_advisor
        # Executor (Sonnet realtime): 7000 in * $3/MTok + 4000 out * $15/MTok
        # = 0.021 + 0.060 = 0.081
        # Advisor (Opus realtime): 8000 in * $5/MTok + 500 out * $25/MTok
        # = 0.040 + 0.0125 = 0.0525
        # Total = 0.1335
        total, exec_cost, adv_cost = estimate_cost_with_advisor(
            executor_model="claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            api_mode="realtime",
            executor_input_tokens=7000,
            executor_output_tokens=4000,
            advisor_input_tokens=8000,
            advisor_output_tokens=500,
        )
        assert exec_cost == pytest.approx(0.081)
        assert adv_cost == pytest.approx(0.0525)
        assert total == pytest.approx(0.1335)

    def test_metrics_include_advisor_fields_when_used(self):
        from api_client import call_api
        client = MagicMock()
        client.beta.messages.create.return_value = _mock_advisor_response(
            input_tokens=7000, output_tokens=4000,
            advisor_input_tokens=8000, advisor_output_tokens=500,
        )

        _, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            handle_continuation=False,
        )

        assert metrics.advisor_model == "claude-opus-4-6"
        assert metrics.advisor_input_tokens == 8000
        assert metrics.advisor_output_tokens == 500
        assert metrics.advisor_cost_usd > 0
        assert metrics.executor_cost_usd > 0
        assert metrics.estimated_cost_usd == pytest.approx(
            metrics.advisor_cost_usd + metrics.executor_cost_usd
        )

    def test_metrics_advisor_fields_default_when_no_advisor(self):
        from api_client import call_api
        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        _, metrics = call_api(
            client, [{"role": "user", "content": "hi"}], "claude-opus-4-6",
        )

        assert metrics.advisor_model == ""
        assert metrics.advisor_input_tokens == 0
        assert metrics.advisor_cost_usd == 0.0
        assert metrics.executor_cost_usd == metrics.estimated_cost_usd

    def test_extract_advisor_usage_handles_missing_iterations(self):
        from api_client import _extract_advisor_usage
        resp = MagicMock(spec=[])  # No attributes at all
        resp.usage = MagicMock(spec=[])  # No iterations attribute

        result = _extract_advisor_usage(resp)
        assert result["advisor_input_tokens"] == 0
        assert result["advisor_output_tokens"] == 0
        assert result["executor_input_tokens"] == 0
        assert result["executor_output_tokens"] == 0

    @patch("api_client.time.sleep")
    @patch("api_client.time.monotonic")
    def test_batch_with_advisor_includes_advisor_tool(self, mock_mono, mock_sleep):
        from api_client import call_api
        client = MagicMock()
        batch_obj = MagicMock()
        batch_obj.id = "batch_adv"
        client.beta.messages.batches.create.return_value = batch_obj

        status = MagicMock()
        status.processing_status = "ended"
        client.beta.messages.batches.retrieve.return_value = status
        client.beta.messages.batches.results.return_value = [_mock_batch_result()]

        mock_mono.side_effect = [0, 5]

        call_api(
            client, [{"role": "user", "content": "hi"}], "claude-sonnet-4-6",
            advisor_model="claude-opus-4-6",
            use_batch=True,
            handle_continuation=False,
        )

        # Should use beta endpoint for batches too
        client.beta.messages.batches.create.assert_called_once()
        params = client.beta.messages.batches.create.call_args.kwargs
        request_params = params["requests"][0]["params"]
        advisor_tools = [t for t in request_params.get("tools", []) if t.get("type") == "advisor_20260301"]
        assert len(advisor_tools) == 1


class TestAdvisorEnvVarWiring:
    """End-to-end wiring test: verify env var toggles in fix modules
    result in correct parameters reaching api_client.call_api."""

    def _make_mock_response(self):
        resp = MagicMock()
        resp.content = [MagicMock(text="No fixes needed.")]
        metrics = MagicMock()
        return (resp, metrics)

    def test_fix_with_advisor_env_passes_correct_params(self, tmp_path):
        """USE_ADVISOR_FIX=true -> model=sonnet, advisor_model=opus."""
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {"USE_ADVISOR_FIX": "true"}):
            from fix import attempt_fix
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        assert "sonnet" in captured.get("model", ""), "Expected Sonnet as executor model"
        assert captured.get("advisor_model") == "claude-opus-4-6", "Expected Opus as advisor"
        assert captured.get("advisor_max_uses") == 2

    def test_fix_without_advisor_env_uses_opus_direct(self, tmp_path):
        """USE_ADVISOR_FIX not set -> model=opus, no advisor_model."""
        (tmp_path / "server.js").write_text("const express = require('express');")

        captured = {}
        def capture(**kwargs):
            captured.update(kwargs)
            return self._make_mock_response()

        with patch("fix._shared_call_api", side_effect=capture), \
             patch("fix._create_client"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USE_ADVISOR_FIX", None)
            from fix import attempt_fix
            attempt_fix(str(tmp_path), [("test1", False, "error1")])

        assert "opus" in captured.get("model", ""), "Expected Opus as direct model"
        assert "advisor_model" not in captured, "Expected no advisor_model param"


# ---------------------------------------------------------------------------
# Section 02: make_client factory, wall-clock budget, cancellation, test hook
# ---------------------------------------------------------------------------


import logging  # noqa: E402
import threading  # noqa: E402
import time as _real_time  # noqa: E402

import httpx  # noqa: E402


class TestMakeClient:
    """Tests for cli.api_client.make_client factory."""

    def test_reads_env_var_when_no_arg(self, monkeypatch):
        """make_client() with no args reads ANTHROPIC_API_KEY from env
        and returns an anthropic.Anthropic instance."""
        from api_client import make_client

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        client = make_client()
        assert isinstance(client, anthropic.Anthropic)

    def test_explicit_api_key_overrides_env(self, monkeypatch):
        """make_client(api_key='explicit') uses explicit key, ignores env."""
        import api_client as api_client_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        captured = {}

        def fake_ctor(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(api_client_mod.anthropic, "Anthropic", fake_ctor)
        api_client_mod.make_client(api_key="sk-ant-explicit")
        assert captured["api_key"] == "sk-ant-explicit"

    def test_timeout_is_default_httpx_timeout(self, monkeypatch):
        """Constructed client's ``timeout`` is an httpx.Timeout with
        connect=10, read=300, write=60, pool=10."""
        import api_client as api_client_mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        captured = {}

        def fake_ctor(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(api_client_mod.anthropic, "Anthropic", fake_ctor)
        api_client_mod.make_client()
        timeout = captured["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == 10.0
        assert timeout.read == 300.0
        assert timeout.write == 60.0
        assert timeout.pool == 10.0

    def test_max_retries_is_sdk_default(self, monkeypatch):
        """Constructed client's max_retries is the SDK default (2).

        Documents that we deliberately leave SDK retries on, the
        wall-clock budget in call_api is the real bound on compounding
        retries."""
        from api_client import make_client

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        client = make_client()
        assert client.max_retries == 2

    def test_raises_when_no_key_available(self, monkeypatch):
        """make_client() raises ValueError when neither arg nor env is set."""
        from api_client import make_client

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            make_client()


class TestWallClockBudget:
    """Tests for the wall_clock_budget_s parameter on call_api."""

    def test_budget_exceeded_raises_new_exception(self, monkeypatch):
        """call_api with wall_clock_budget_s=5 and a mocked
        messages.create that sleeps past the budget raises
        CallApiWallClockExceeded."""
        from api_client import call_api, CallApiWallClockExceeded

        # Drive fake clock so entry is t=0 and the post-call boundary is t=10.
        ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        with pytest.raises(CallApiWallClockExceeded):
            call_api(
                client,
                [{"role": "user", "content": "hi"}],
                "claude-opus-4-6",
                wall_clock_budget_s=5.0,
                build_id="bid-budget-1",
            )

    def test_exception_has_all_attributes(self, monkeypatch):
        """Raised exception carries elapsed_s, budget_s, build_id, model."""
        from api_client import call_api, CallApiWallClockExceeded

        ticks = iter([0.0, 0.0, 12.5, 12.5, 12.5, 12.5])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        try:
            call_api(
                client,
                [{"role": "user", "content": "hi"}],
                "claude-opus-4-6",
                wall_clock_budget_s=5.0,
                build_id="bid-attrs",
            )
        except CallApiWallClockExceeded as exc:
            assert exc.build_id == "bid-attrs"
            assert exc.model == "claude-opus-4-6"
            assert exc.budget_s == 5.0
            assert exc.elapsed_s >= 5.0
        else:
            pytest.fail("expected CallApiWallClockExceeded")

    def test_exception_is_api_timeout_error_subclass(self):
        """isinstance(exc, anthropic.APITimeoutError) is True so legacy
        ``except APITimeoutError`` handlers still catch it."""
        from api_client import CallApiWallClockExceeded

        exc = CallApiWallClockExceeded(
            build_id="bid-1", model="claude-opus-4-6",
            elapsed_s=99.9, budget_s=5.0,
        )
        assert isinstance(exc, anthropic.APITimeoutError)
        assert "budget exceeded" in str(exc)

    def test_fast_call_under_budget_returns_normally(self, monkeypatch):
        """A fast mock with wall_clock_budget_s=5 returns normally."""
        from api_client import call_api

        ticks = iter([0.0, 0.0, 0.01, 0.02, 0.03, 0.04])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="fast")

        response, metrics = call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
            wall_clock_budget_s=5.0,
            build_id="bid-fast",
        )
        assert response.content[0].text == "fast"

    def test_env_var_overrides_default(self, monkeypatch):
        """CAS_CALL_API_WALL_CLOCK_S=3 overrides default when
        wall_clock_budget_s is not passed explicitly."""
        from api_client import call_api, CallApiWallClockExceeded

        monkeypatch.setenv("CAS_CALL_API_WALL_CLOCK_S", "3")
        ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        client.messages.create.return_value = _mock_response()

        with pytest.raises(CallApiWallClockExceeded) as excinfo:
            call_api(
                client,
                [{"role": "user", "content": "hi"}],
                "claude-opus-4-6",
                build_id="bid-env",
            )
        assert excinfo.value.budget_s == 3.0

    def test_explicit_kwarg_overrides_env_var(self, monkeypatch):
        """wall_clock_budget_s kwarg overrides CAS_CALL_API_WALL_CLOCK_S."""
        from api_client import call_api

        monkeypatch.setenv("CAS_CALL_API_WALL_CLOCK_S", "1")
        ticks = iter([0.0, 0.0, 0.5, 0.6, 0.7, 0.8])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="fast-explicit")

        # Env says 1s, but we pass 999s explicitly; should not raise.
        response, _metrics = call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
            wall_clock_budget_s=999.0,
            build_id="bid-explicit",
        )
        assert response.content[0].text == "fast-explicit"

    @patch("api_client.time.sleep")
    def test_backoff_respects_wall_clock_budget(self, mock_sleep, monkeypatch):
        """Retry loop checks remaining budget BEFORE sleeping. If the
        backoff alone would exceed the budget, raise
        CallApiWallClockExceeded instead of sleeping."""
        from api_client import call_api, CallApiWallClockExceeded

        # perf_counter readings:
        #   1. call_api entry start_ts = 0.0
        #   2. boundary _check_budget (pre-initial) = 0.0
        #   3. inner retry pre-sleep fail-fast = 9.5 (elapsed)
        #     9.5 + 2.0 backoff = 11.5 > 10.0 budget -> raise
        ticks = iter([0.0, 0.0, 9.5, 9.5, 9.5, 9.5])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        client.messages.create.side_effect = anthropic.APITimeoutError(
            request=MagicMock()
        )

        with pytest.raises(CallApiWallClockExceeded):
            call_api(
                client,
                [{"role": "user", "content": "hi"}],
                "claude-opus-4-6",
                wall_clock_budget_s=10.0,
                build_id="bid-backoff",
            )
        # Key property: we fail-fast BEFORE sleeping.
        mock_sleep.assert_not_called()

    def test_continuation_loop_shares_budget(self, monkeypatch):
        """Continuation loop (response due to max_tokens) enforces the
        SAME shared budget across the initial call and every continuation
        call."""
        from api_client import call_api, CallApiWallClockExceeded

        # Budget = 5.0s. First call t=0.0 ok. Continuation pre-call check
        # sees t=99.0 elapsed and raises.
        ticks = iter([0.0, 0.0, 0.01, 99.0, 99.0, 99.0, 99.0])
        monkeypatch.setattr("api_client.time.perf_counter", lambda: next(ticks))

        client = MagicMock()
        # First response truncates (max_tokens), triggering continuation loop.
        first = _mock_response(text="part1", stop_reason="max_tokens")
        second = _mock_response(text="part2", stop_reason="end_turn")
        client.messages.create.side_effect = [first, second]

        with pytest.raises(CallApiWallClockExceeded):
            call_api(
                client,
                [{"role": "user", "content": "hi"}],
                "claude-opus-4-6",
                wall_clock_budget_s=5.0,
                build_id="bid-cont",
            )


class TestCancellationIntegration:
    """Tests wiring cli.cancellation into call_api."""

    @pytest.fixture(autouse=True)
    def _isolate_registry(self):
        """Snapshot + restore the cancellation registry so tests don't leak."""
        from cli import cancellation

        snapshot = dict(cancellation._registry)
        try:
            yield
        finally:
            cancellation._registry.clear()
            cancellation._registry.update(snapshot)

    def test_unfired_cancel_returns_normally(self, monkeypatch):
        """call_api with a registered build_id returns normally when
        the cancel event is never fired."""
        from api_client import call_api
        from cli import cancellation

        cancellation.register("bid-quiet")
        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="quiet-ok")

        response, _metrics = call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
            build_id="bid-quiet",
        )
        assert response.content[0].text == "quiet-ok"

    def test_cancel_fired_from_other_thread_raises(self, monkeypatch):
        """cli.cancellation.fire(build_id) from a second thread while the
        first thread is inside a slow-mocked call_api causes the first
        thread to raise BuildCancelled with the correct build_id."""
        from api_client import call_api, BuildCancelled
        from cli import cancellation

        cancellation.register("bid-fired")
        fire_gate = threading.Event()

        def slow_create(**kwargs):
            # Signal the firing thread, then block until cancel fires.
            fire_gate.set()
            # Tiny wait to let the other thread run fire().
            _real_time.sleep(0.2)
            return _mock_response(text="late-reply")

        client = MagicMock()
        client.messages.create.side_effect = slow_create

        results = {}

        def worker():
            try:
                call_api(
                    client,
                    [{"role": "user", "content": "hi"}],
                    "claude-opus-4-6",
                    build_id="bid-fired",
                )
                results["exc"] = None
            except BaseException as exc:  # noqa: BLE001
                results["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        assert fire_gate.wait(timeout=1.0)
        cancellation.fire("bid-fired")
        t.join(timeout=2.0)

        exc = results.get("exc")
        assert isinstance(exc, BuildCancelled)
        assert exc.build_id == "bid-fired"

    def test_no_build_id_uses_no_op_event(self, monkeypatch):
        """call_api called with no build_id resolves to _NO_OP_EVENT
        sentinel and never raises BuildCancelled even if an unrelated
        cancel event is fired."""
        from api_client import call_api
        from cli import cancellation

        cancellation.register("bid-other")
        cancellation.fire("bid-other")  # unrelated firing

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="no-bid-ok")

        # Default build_id="" -> _NO_OP_EVENT -> never is_set().
        response, _metrics = call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
        )
        assert response.content[0].text == "no-bid-ok"

    def test_register_after_clear_is_fresh_event(self):
        """clear(build_id) followed by fresh register(build_id) yields
        a brand-new never-set event (no stuck state from prior cycle)."""
        from cli import cancellation

        first = cancellation.register("bid-cycle")
        cancellation.fire("bid-cycle")
        assert first.is_set() is True
        cancellation.clear("bid-cycle")
        second = cancellation.register("bid-cycle")
        assert second is not first
        assert second.is_set() is False

    def test_cancel_checked_at_all_boundaries(self, monkeypatch):
        """call_api checks the cancel event at four boundaries:
        before initial call, after initial call, before each
        continuation call, and after each continuation call.
        Use a wrapper that records is_set() call sites and asserts
        each boundary is hit."""
        from api_client import call_api
        from cli import cancellation

        real_event = cancellation.register("bid-boundaries")

        class _SpyEvent:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.is_set_calls = 0

            def is_set(self):
                self.is_set_calls += 1
                return self._wrapped.is_set()

            def wait(self, timeout=None):
                return self._wrapped.wait(timeout)

            def set(self):
                self._wrapped.set()

            def clear(self):
                self._wrapped.clear()

        spy = _SpyEvent(real_event)

        def fake_get(bid):
            if bid == "bid-boundaries":
                return spy
            return cancellation._NO_OP_EVENT

        monkeypatch.setattr(cancellation, "get", fake_get)

        client = MagicMock()
        first = _mock_response(text="p1", stop_reason="max_tokens")
        second = _mock_response(text="p2", stop_reason="end_turn")
        client.messages.create.side_effect = [first, second]

        call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
            build_id="bid-boundaries",
        )
        # Expect four boundary checks: pre-initial, post-initial,
        # pre-continuation, post-continuation.
        assert spy.is_set_calls >= 4


class TestSimulateHangHook:
    """Tests for the ANTHROPIC_SIMULATE_HANG_SECONDS test hook."""

    @pytest.fixture(autouse=True)
    def _isolate_registry(self):
        from cli import cancellation

        snapshot = dict(cancellation._registry)
        try:
            yield
        finally:
            cancellation._registry.clear()
            cancellation._registry.update(snapshot)

    def test_hook_honored_when_both_env_vars_set(self, monkeypatch):
        """With ANTHROPIC_SIMULATE_HANG_SECONDS=2 and CAS_ALLOW_TEST_HOOKS=1,
        call_api takes ~2s before the real API call (allow 1s slop)."""
        from api_client import call_api

        monkeypatch.setenv("CAS_ALLOW_TEST_HOOKS", "1")
        monkeypatch.setenv("ANTHROPIC_SIMULATE_HANG_SECONDS", "2")
        # Raise budget so the hang doesn't blow it.
        monkeypatch.setenv("CAS_CALL_API_WALL_CLOCK_S", "60")

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="after-hang")

        start = _real_time.monotonic()
        response, _metrics = call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
            build_id="bid-hook-honored",
        )
        elapsed = _real_time.monotonic() - start
        assert response.content[0].text == "after-hang"
        assert 1.5 <= elapsed <= 3.5, f"expected ~2s hang, got {elapsed:.2f}s"

    def test_hook_ignored_without_allow_test_hooks(self, monkeypatch):
        """With only ANTHROPIC_SIMULATE_HANG_SECONDS set (no
        CAS_ALLOW_TEST_HOOKS), the hook is IGNORED, call_api proceeds
        to the mocked API call with no extra delay. Safety gate."""
        from api_client import call_api

        monkeypatch.delenv("CAS_ALLOW_TEST_HOOKS", raising=False)
        monkeypatch.setenv("ANTHROPIC_SIMULATE_HANG_SECONDS", "5")

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="no-hang")

        start = _real_time.monotonic()
        response, _metrics = call_api(
            client,
            [{"role": "user", "content": "hi"}],
            "claude-opus-4-6",
            build_id="bid-hook-ignored",
        )
        elapsed = _real_time.monotonic() - start
        assert response.content[0].text == "no-hang"
        # Without the allow gate, we must NOT sleep.
        assert elapsed < 1.0, f"expected fast path, got {elapsed:.2f}s"

    def test_hook_cancellable_mid_hang(self, monkeypatch):
        """Firing the cancel event mid-hang causes call_api to raise
        BuildCancelled promptly (within ~1 second of the fire). The
        hook must be cancellable via cancel_event.wait(), not time.sleep."""
        from api_client import call_api, BuildCancelled
        from cli import cancellation

        monkeypatch.setenv("CAS_ALLOW_TEST_HOOKS", "1")
        monkeypatch.setenv("ANTHROPIC_SIMULATE_HANG_SECONDS", "30")
        monkeypatch.setenv("CAS_CALL_API_WALL_CLOCK_S", "120")

        cancellation.register("bid-hang-cancel")
        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="never")

        results = {}

        def worker():
            try:
                call_api(
                    client,
                    [{"role": "user", "content": "hi"}],
                    "claude-opus-4-6",
                    build_id="bid-hang-cancel",
                )
                results["exc"] = None
            except BaseException as exc:  # noqa: BLE001
                results["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        # Let the hook enter its wait loop.
        _real_time.sleep(0.3)
        fire_ts = _real_time.monotonic()
        cancellation.fire("bid-hang-cancel")
        t.join(timeout=3.0)
        unblock_elapsed = _real_time.monotonic() - fire_ts

        exc = results.get("exc")
        assert isinstance(exc, BuildCancelled), f"got {type(exc).__name__}"
        assert exc.build_id == "bid-hang-cancel"
        # Cancellable within ~1s of the fire.
        assert unblock_elapsed < 1.5, (
            f"cancel took {unblock_elapsed:.2f}s; hook must use "
            f"cancel_event.wait(1.0), not time.sleep"
        )

    def test_hook_emits_log_line(self, monkeypatch, caplog):
        """Log line '[test-hook] simulating hang for N seconds' emitted
        when the hook runs."""
        from api_client import call_api

        monkeypatch.setenv("CAS_ALLOW_TEST_HOOKS", "1")
        monkeypatch.setenv("ANTHROPIC_SIMULATE_HANG_SECONDS", "1")
        monkeypatch.setenv("CAS_CALL_API_WALL_CLOCK_S", "60")

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="logged")

        with caplog.at_level(logging.WARNING, logger="api_client"):
            call_api(
                client,
                [{"role": "user", "content": "hi"}],
                "claude-opus-4-6",
                build_id="bid-hook-log",
            )

        assert any(
            "[test-hook] simulating hang for 1 seconds" in rec.getMessage()
            for rec in caplog.records
        ), "expected [test-hook] log line"


class TestCallApiVisionPayload:
    """Guards that call_api passes vision-style messages through
    unchanged. Blocking requirement for the ui_test.py migration in §06.
    """

    def test_base64_image_block_forwarded_unchanged(self, monkeypatch):
        """call_api called with a vision-style messages list (base64
        image block) forwards the exact messages list to
        client.messages.create unchanged."""
        from api_client import call_api

        vision_messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "aGVsbG8td29ybGQ=",
                        },
                    },
                    {"type": "text", "text": "Describe this screenshot."},
                ],
            }
        ]

        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _mock_response(text="a cat")

        client = MagicMock()
        client.messages.create.side_effect = capture

        call_api(client, vision_messages, "claude-opus-4-6")

        # Exact pass-through: the messages kwarg equals the input list.
        assert captured["messages"] == vision_messages
        # And the nested base64 data is byte-identical.
        inner = captured["messages"][0]["content"][0]
        assert inner["source"]["data"] == "aGVsbG8td29ybGQ="

    def test_vision_response_is_combined_response(self, monkeypatch):
        """The vision response is returned with the same .content[0].text
        access pattern as non-vision calls."""
        from api_client import call_api

        client = MagicMock()
        client.messages.create.return_value = _mock_response(text="a dog")

        vision_messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "Zm9v",
                        },
                    }
                ],
            }
        ]

        response, metrics = call_api(client, vision_messages, "claude-opus-4-6")
        # Same access pattern as non-vision responses.
        assert response.content[0].text == "a dog"
        assert metrics.model == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# WATCHDOG1 Layer 2 §04: Static bypass-prevention tests.
#
# This test class is the forcing function for §05 and §06. It AST-walks every
# cli/*.py file (except cli/api_client.py itself) and enforces that:
#
#   1. No Anthropic constructor call escapes cli/api_client.py; everyone must
#      go through cli.api_client.make_client() so every client inherits the
#      explicit httpx.Timeout added in §02.
#   2. No direct `<anything>.messages.create(...)` call escapes cli/api_client.py
#      - every Anthropic request must go through call_api() so it gets the
#      wall-clock budget, cancel polling, and retry wrapper.
#
# These two tests WILL land RED when §04 commits, listing every offender by
# file+line. §05 migrates the 9 constructor sites to make_client(); §06
# migrates the 3 direct messages.create sites (cli/build.py ~1352,
# cli/intake.py ~243, cli/ui_test.py ~356) to call_api(). After §06, both
# methods go green. The red bar on §04 is documented in its commit message
# so future bisects know the failure is intentional.
#
# Pure static analysis: no network, no disk I/O beyond reading cli/*.py,
# no subprocess exec, no fixtures. Runtime under one second.
# ---------------------------------------------------------------------------


import ast

_CLI_DIR = Path(__file__).resolve().parent.parent / "cli"
# The single legitimate home for the Anthropic constructor and
# .messages.create. Every other cli/*.py file is subject to the ban.
_ALLOWLIST = {"api_client.py"}


def _walk_cli_files():
    """Yield (path, ast.Module) for every cli/*.py under enforcement.

    Excludes cli/api_client.py (where the single legitimate Anthropic call
    lives) and cli/__init__.py (package marker, no content).
    """
    for path in sorted(_CLI_DIR.glob("*.py")):
        if path.name in _ALLOWLIST or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        yield path, tree


class TestNoBypassStatic:
    """Drift-prevention layer for WATCHDOG1 Layer 2.

    Guarantees that every Anthropic call in cli/ routes through
    cli.api_client.make_client() + call_api(), so the wall-clock budget
    and cooperative cancel path added in §02 can never be bypassed by a
    future copy-paste. Allowlist is exactly cli/api_client.py.
    """

    def test_no_anthropic_constructor_outside_api_client(self):
        """Ban any `anthropic.Anthropic(...)` or bare `Anthropic(...)` call
        outside cli/api_client.py. Report every offender in one assertion.

        Matches both forms:
          - `anthropic.Anthropic(...)` , ast.Attribute chain
          - `Anthropic(...)` after `from anthropic import Anthropic`
           , ast.Name, but ONLY when the file actually imports Anthropic
            from the anthropic package (avoids false positives on unrelated
            local classes named Anthropic).
        """
        offenders: list[str] = []

        for path, tree in _walk_cli_files():
            # Pre-scan ImportFrom nodes to see if this file imports
            # Anthropic from the anthropic package. If not, a bare
            # `Anthropic(...)` call is not actually the SDK constructor
            # and we should not flag it.
            imports_bare_anthropic = False
            for stmt in ast.walk(tree):
                if isinstance(stmt, ast.ImportFrom) and stmt.module == "anthropic":
                    for alias in stmt.names:
                        if alias.name == "Anthropic":
                            imports_bare_anthropic = True
                            break

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func

                # Form 1: anthropic.Anthropic(...)
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "Anthropic"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "anthropic"
                ):
                    rel = path.relative_to(_CLI_DIR.parent)
                    offenders.append(
                        f"{rel}:{node.lineno}; anthropic.Anthropic(...)"
                    )
                    continue

                # Form 2: bare Anthropic(...) when imported from anthropic
                if (
                    imports_bare_anthropic
                    and isinstance(func, ast.Name)
                    and func.id == "Anthropic"
                ):
                    rel = path.relative_to(_CLI_DIR.parent)
                    offenders.append(
                        f"{rel}:{node.lineno}; Anthropic(...)"
                    )

        assert not offenders, (
            "Found Anthropic constructor call(s) outside cli/api_client.py. "
            "Every Anthropic client must be created via "
            "cli.api_client.make_client() so the explicit httpx.Timeout "
            "from §02 is applied.\n\nOffenders:\n  "
            + "\n  ".join(offenders)
        )

    def test_no_direct_messages_create_outside_api_client(self):
        """Ban any `<anything>.messages.create(...)` call outside
        cli/api_client.py. Report every offender in one assertion.

        The `<anything>` is intentional, it catches
        `client.messages.create`, `self.client.messages.create`,
        `make_client().messages.create`, etc. The only legitimate home
        for `client.messages.create` is inside call_api() in
        cli/api_client.py, which is excluded by the allowlist.
        """
        offenders: list[str] = []

        for path, tree in _walk_cli_files():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr != "create":
                    continue
                parent = func.value
                if not isinstance(parent, ast.Attribute):
                    continue
                if parent.attr != "messages":
                    continue
                rel = path.relative_to(_CLI_DIR.parent)
                offenders.append(
                    f"{rel}:{node.lineno}; <...>.messages.create(...)"
                )

        assert not offenders, (
            "Found direct .messages.create() call(s) outside "
            "cli/api_client.py. Every Anthropic request must go through "
            "cli.api_client.call_api() so it gets the wall-clock budget, "
            "cancel polling, and retry wrapper from §02.\n\nOffenders:\n  "
            + "\n  ".join(offenders)
        )


class TestClientFactoryAdoption:
    """Smoke tests proving each migrated cli module routes through
    make_client() at runtime. Pair with TestNoBypassStatic which enforces
    the same thing statically (§04).

    Six tests, one per §05-migrated module whose client-factory path is
    cheap to reach. The three §06-pending modules (build.py, intake.py,
    ui_test.py) will get their own migration tests in §06 and are
    intentionally omitted here.
    """

    def test_design_fix_uses_make_client(self, monkeypatch):
        """Stub heavy helpers, invoke fix_design(), assert make_client called."""
        import design_fix as mod

        mock_mc = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mod, "make_client", mock_mc)
        monkeypatch.setattr(
            mod,
            "_collect_relevant_files",
            lambda build_dir, issues: [{"path": "views/index.ejs", "content": "x"}],
        )
        monkeypatch.setattr(
            mod,
            "_shared_call_api",
            MagicMock(return_value=(MagicMock(content=[MagicMock(text="")]), None)),
        )
        monkeypatch.setattr(mod, "_parse_fix_files", lambda text: [])

        issues = [{
            "severity": "critical",
            "category": "contrast",
            "file": "views/index.ejs",
            "issue": "x",
            "suggestion": "y",
        }]
        mod.fix_design("/tmp/nonexistent-build", {"design": {}}, issues)

        assert mock_mc.called is True

    def test_design_review_uses_make_client(self, monkeypatch):
        """Call _create_client() directly, assert make_client was called."""
        import design_review as mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_mc = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mod, "make_client", mock_mc)

        mod._create_client()

        assert mock_mc.called is True

    def test_fix_uses_make_client(self, monkeypatch):
        """Call _create_client() directly, assert make_client was called."""
        import fix as mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_mc = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mod, "make_client", mock_mc)

        mod._create_client()

        assert mock_mc.called is True

    def test_qa_uses_make_client(self, monkeypatch):
        """Call _create_client() directly, assert make_client was called."""
        import qa as mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_mc = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mod, "make_client", mock_mc)

        mod._create_client()

        assert mock_mc.called is True

    def test_qa_fix_uses_make_client(self, monkeypatch):
        """Call _create_client() directly, assert make_client was called."""
        import qa_fix as mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_mc = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mod, "make_client", mock_mc)

        mod._create_client()

        assert mock_mc.called is True

    def test_review_uses_make_client(self, monkeypatch):
        """Call _create_client() directly, assert make_client was called."""
        import review as mod

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_mc = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(mod, "make_client", mock_mc)

        mod._create_client()

        assert mock_mc.called is True
