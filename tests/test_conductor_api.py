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

        # Sonnet: input=$3/M, output=$15/M; 100 input + 50 output = (300+750)/1e6
        expected = (100 * 3.0 + 50 * 15.0) / 1_000_000
        assert result.cost_usd == pytest.approx(expected, rel=1e-4)


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


class TestApiConductorCostCap:
    def test_cost_cap_raises_runtime_error(self) -> None:
        """When running_cost >= cap, raise RuntimeError (not AbortRequested).

        RuntimeError is caught at supervisor.py:153 → FAILED.
        AbortRequested would re-queue → infinite retry for an always-overrun task.
        """
        # Set a very low cap so even one turn exceeds it.
        fake_anthropic = _make_fake_anthropic([_make_response("end_turn", "x")])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6", cost_cap_usd=0.000001)
            with pytest.raises(RuntimeError, match="hard_cost_cap_exceeded"):
                cond.dispatch(
                    head=Builder(),
                    task=Task(title="t", prompt="y"),
                )

    def test_cost_cap_is_not_abort_requested(self) -> None:
        """Verify the exception is NOT AbortRequested (which would loop forever)."""
        from claude_swarm.abort import AbortRequested

        fake_anthropic = _make_fake_anthropic([_make_response("end_turn", "x")])

        with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
            cond = ApiConductor(model_override="claude-sonnet-4-6", cost_cap_usd=0.000001)
            try:
                cond.dispatch(head=Builder(), task=Task(title="t", prompt="z"))
            except RuntimeError:
                pass  # expected
            except AbortRequested:
                pytest.fail("Cost cap must raise RuntimeError, not AbortRequested")


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
