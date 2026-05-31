"""Offline unit tests for ApiConductor.

All tests mock at the ``anthropic.Anthropic`` import boundary — NO real API
calls, NO tokens spent.

The lazy import path (``import anthropic`` inside ``dispatch()``) is exercised
by monkeypatching ``claude_swarm.conductors.api.anthropic`` after the module
has been imported.
"""
from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from claude_swarm.conductors.api import ApiConductor, _mark_cache_prefix
from claude_swarm.heads import Builder
from claude_swarm.kanban import Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers — fake Anthropic response objects
# ---------------------------------------------------------------------------

def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Any:
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read_input_tokens
    u.cache_creation_input_tokens = cache_creation_input_tokens
    # No split cache_creation breakdown by default (flat only).
    u.cache_creation = None
    return u


def _text_block(text: str) -> Any:
    blk = MagicMock()
    blk.type = "text"
    blk.text = text
    return blk


def _tool_use_block(tool_id: str = "tu_1", name: str = "read_file") -> Any:
    blk = MagicMock()
    blk.type = "tool_use"
    blk.id = tool_id
    blk.name = name
    return blk


def _make_response(stop_reason: str, text: str = "hello", tools: list[Any] | None = None) -> Any:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    if tools:
        resp.content = [*tools, _text_block(text)]
    else:
        resp.content = [_text_block(text)]
    resp.usage = _make_usage()
    return resp


# ---------------------------------------------------------------------------
# Fake anthropic module injected via monkeypatch
# ---------------------------------------------------------------------------

def _make_fake_anthropic(create_side_effect: Any) -> types.ModuleType:
    """Return a minimal fake `anthropic` module."""
    fake_mod = types.ModuleType("anthropic")
    client_instance = MagicMock()
    client_instance.messages.create.side_effect = create_side_effect
    fake_cls = MagicMock(return_value=client_instance)
    fake_mod.Anthropic = fake_cls  # type: ignore[attr-defined]
    return fake_mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestApiConductorHappyPath:
    def test_single_turn_end_turn(self) -> None:
        """One turn, stop_reason='end_turn' → DONE with non-empty result."""
        responses = [_make_response("end_turn", "the answer")]
        fake_anthropic = _make_fake_anthropic(responses)

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-haiku-4-5")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="what is 2+2?"),
            )

        assert result.status is TaskStatus.DONE
        assert result.result == "the answer"
        assert result.cost_usd > 0.0, "cost_usd must be non-zero for a known model"

    def test_cost_usd_populated(self) -> None:
        """price_call must be used to populate cost_usd from response.usage."""
        responses = [_make_response("end_turn", "x")]
        fake_anthropic = _make_fake_anthropic(responses)

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="hello"),
            )

        # Sonnet: input=$3/M, output=$15/M; 100 input + 50 output = (300+750)/1e6.
        # cache_creation_input_tokens=0, cache_read_input_tokens=0 by default.
        expected = (100 * 3.0 + 50 * 15.0) / 1_000_000
        assert result.cost_usd == pytest.approx(expected, rel=1e-4)

    def test_dispatch_is_synchronous(self) -> None:
        """dispatch() must be synchronous (no asyncio.run overhead)."""
        import inspect
        assert not inspect.iscoroutinefunction(ApiConductor.dispatch)


class TestApiConductorToolLoop:
    def test_tool_use_then_end_turn(self) -> None:
        """First response has tool_use; second is end_turn → loop iterates."""
        tool_resp = _make_response("tool_use", tools=[_tool_use_block()])
        final_resp = _make_response("end_turn", "final answer")
        responses = [tool_resp, final_resp]
        fake_anthropic = _make_fake_anthropic(responses)

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-haiku-4-5")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="use a tool"),
            )

        assert result.status is TaskStatus.DONE
        assert result.result == "final answer"
        # Two API calls were made.
        assert fake_anthropic.Anthropic().messages.create.call_count == 2

    def test_cost_accumulates_across_turns(self) -> None:
        """Running cost is the sum across all turns."""
        tool_resp = _make_response("tool_use", tools=[_tool_use_block()])
        final_resp = _make_response("end_turn", "done")
        responses = [tool_resp, final_resp]
        fake_anthropic = _make_fake_anthropic(responses)

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="x"),
            )

        # Two turns, each with 100 input + 50 output at sonnet rates.
        per_turn = (100 * 3.0 + 50 * 15.0) / 1_000_000
        assert result.cost_usd == pytest.approx(2 * per_turn, rel=1e-4)

    def test_max_turns_exhaustion_returns_failed(self) -> None:
        """When tool_use loops exhaust max_turns, return FAILED not DONE.

        Bug: without the completed-flag guard, the while loop exits by guard
        condition and falls through to the unconditional DONE return, marking
        the task DONE with result=None — a silent false success that cascades
        unblocking of DAG-dependent tasks.
        """
        # Every response is tool_use so the loop never hits the end_turn branch.
        always_tool = _make_response("tool_use", tools=[_tool_use_block()])
        fake_anthropic = _make_fake_anthropic([always_tool] * 5)

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-haiku-4-5")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="loop forever", max_turns=3),
            )

        assert result.status is TaskStatus.FAILED
        assert result.result is None
        assert result.error is not None
        assert "max_turns" in result.error
        assert "(3)" in result.error
        # Cost for 3 turns must be recorded (not 0) so the kanban ledger is accurate.
        assert result.cost_usd > 0.0
        # Exactly max_turns API calls were made.
        assert fake_anthropic.Anthropic().messages.create.call_count == 3


class TestApiConductorCostCap:
    def test_cost_cap_in_tool_loop_returns_failed(self) -> None:
        """Cost cap fires only inside the tool-use loop (not on end_turn).

        When running_cost >= cap AND the current response is tool_use (about
        to make another API call), the conductor returns FAILED — not raises —
        so the supervisor's normal path writes cost_usd to the kanban row.
        """
        # Set a very low cap so even one turn exceeds it.
        tool_resp = _make_response("tool_use", tools=[_tool_use_block()])
        # Only one response — the cap fires after processing the first tool_use turn.
        fake_anthropic = _make_fake_anthropic([tool_resp])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6", cost_cap_usd=0.000001)
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="y"),
            )

        assert result.status is TaskStatus.FAILED
        assert result.error is not None
        assert "hard_cost_cap_exceeded" in result.error
        # Cost must be recorded on the DispatchResult so the supervisor writes it.
        assert result.cost_usd > 0.0

    def test_cost_cap_does_not_discard_end_turn_result(self) -> None:
        """A completed end_turn response is NEVER discarded by the cost cap.

        Bug: the original implementation checked the cap unconditionally before
        the stop_reason branch, so a successful end_turn turn whose cost crossed
        the cap raised RuntimeError and threw away the finished answer.
        """
        # A single end_turn response — set a cap so low any cost exceeds it.
        end_resp = _make_response("end_turn", "the final answer")
        fake_anthropic = _make_fake_anthropic([end_resp])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6", cost_cap_usd=0.000001)
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="z"),
            )

        # The completed answer must be returned as DONE regardless of the cap.
        assert result.status is TaskStatus.DONE
        assert result.result == "the final answer"
        assert result.cost_usd > 0.0

    def test_cost_cap_does_not_raise_runtime_error(self) -> None:
        """The cap must return DispatchResult(FAILED), not raise RuntimeError.

        RuntimeError on the exception path in supervisor.py:152-159 does NOT
        record cost_usd — using DispatchResult ensures the normal kanban.update
        path fires and cost is preserved.
        """
        tool_resp = _make_response("tool_use", tools=[_tool_use_block()])
        fake_anthropic = _make_fake_anthropic([tool_resp])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6", cost_cap_usd=0.000001)
            # Must NOT raise any exception.
            try:
                result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="z"))
            except Exception as exc:  # pylint: disable=broad-except
                pytest.fail(f"dispatch() raised unexpectedly: {exc!r}")
            assert result.status is TaskStatus.FAILED


class TestApiConductorCacheTokenPricing:
    def test_cache_creation_tokens_priced_as_1h(self) -> None:
        """Cache-creation tokens from a 1h-TTL system prompt must use the 1h price.

        Bug: the original code passed cache_creation_input_tokens to the 5m
        dimension of price_call, under-pricing 1h cache writes by ~37% (Opus).
        """
        from claude_swarm.cost import MODEL_PRICING

        # Build a response where 1000 tokens were written to cache.
        resp = MagicMock()
        resp.stop_reason = "end_turn"
        resp.content = [_text_block("ok")]
        usage = MagicMock()
        usage.input_tokens = 0
        usage.output_tokens = 0
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 1000  # flat aggregate
        usage.cache_creation = None  # no split breakdown (flat only)
        resp.usage = usage

        fake_anthropic = _make_fake_anthropic([resp])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-opus-4-8")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="cache test"),
            )

        # 1000 tokens at the 1h rate ($10/M for Opus-4-8).
        price = MODEL_PRICING["claude-opus-4-8"]
        expected_1h = 1000 * price.cache_write_1h / 1_000_000
        wrong_5m = 1000 * price.cache_write_5m / 1_000_000
        assert result.cost_usd == pytest.approx(expected_1h, rel=1e-4), (
            f"Expected 1h price {expected_1h:.8f}, got {result.cost_usd:.8f} "
            f"(5m wrong price would be {wrong_5m:.8f})"
        )

    def test_cache_creation_tokens_split_breakdown_preferred(self) -> None:
        """When the SDK provides per-TTL breakdown, each bucket uses its own rate."""
        from claude_swarm.cost import MODEL_PRICING

        resp = MagicMock()
        resp.stop_reason = "end_turn"
        resp.content = [_text_block("ok")]
        usage = MagicMock()
        usage.input_tokens = 0
        usage.output_tokens = 0
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 1500  # sum of both tiers
        # Simulate the SDK breakdown (anthropic >= 0.39)
        cache_creation = MagicMock()
        cache_creation.ephemeral_1h_input_tokens = 1000
        cache_creation.ephemeral_5m_input_tokens = 500
        usage.cache_creation = cache_creation
        resp.usage = usage

        fake_anthropic = _make_fake_anthropic([resp])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-opus-4-8")
            result = cond.dispatch(
                head=Builder(),
                task=Task(title="t", prompt="split cache test"),
            )

        price = MODEL_PRICING["claude-opus-4-8"]
        expected = (1000 * price.cache_write_1h + 500 * price.cache_write_5m) / 1_000_000
        assert result.cost_usd == pytest.approx(expected, rel=1e-4)


class TestApiConductorErrorPropagation:
    def test_api_error_propagates(self) -> None:
        """If messages.create() raises, the exception propagates to the supervisor."""
        fake_anthropic = _make_fake_anthropic(ValueError("API error"))

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-haiku-4-5")
            with pytest.raises(ValueError, match="API error"):
                cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))


class TestMarkCachePrefix:
    def test_marks_first_n_blocks(self) -> None:
        blocks = [{"type": "text", "text": f"block {i}"} for i in range(3)]
        marked = _mark_cache_prefix(blocks, count=2, ttl="1h")
        assert "cache_control" in marked[0]
        assert "cache_control" in marked[1]
        assert "cache_control" not in marked[2]

    def test_ttl_propagated(self) -> None:
        blocks = [{"type": "text", "text": "a"}]
        marked = _mark_cache_prefix(blocks, count=1, ttl="5m")
        assert marked[0]["cache_control"]["ttl"] == "5m"

    def test_empty_blocks(self) -> None:
        assert _mark_cache_prefix([], count=2) == []

    def test_original_not_mutated(self) -> None:
        orig = [{"type": "text", "text": "x"}]
        _mark_cache_prefix(orig, count=1)
        assert "cache_control" not in orig[0]
