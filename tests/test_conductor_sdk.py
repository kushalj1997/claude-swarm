"""Offline unit tests for SDKConductor.

The SDK's ``query()`` function spawns a real Claude Code CLI subprocess — tests
NEVER let it do so.  Instead we patch ``claude_swarm.conductors.sdk.query``
with an async-generator stub so the conductor's ``asyncio.run(_run(...))``
path is exercised without touching any real process.

``dispatch()`` is synchronous (``asyncio.run()`` internally), so no
``pytest-asyncio`` is needed: just call ``dispatch()`` directly.

If ``claude_agent_sdk`` is not importable (CI without the SDK installed) we
define lightweight stub classes locally and inject them into
``claude_swarm.conductors.sdk`` so every test still runs.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Inline stub classes (used when claude_agent_sdk is not installed in CI).
# ---------------------------------------------------------------------------

class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _AssistantMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _ResultMessage:
    def __init__(
        self,
        *,
        is_error: bool = False,
        result: str | None = None,
        total_cost_usd: float = 0.0,
        subtype: str = "success",
        num_turns: int = 1,
    ) -> None:
        self.is_error = is_error
        self.result = result
        self.total_cost_usd = total_cost_usd
        self.subtype = subtype
        self.num_turns = num_turns


class _ClaudeAgentOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _ClaudeSDKError(Exception):
    pass


class _ProcessError(Exception):
    def __init__(self, msg: str, *, exit_code: int = 1, stderr: str = "") -> None:
        super().__init__(msg)
        self.exit_code = exit_code
        self.stderr = stderr


class _CLINotFoundError(Exception):
    pass


def _make_fake_sdk_module() -> types.ModuleType:
    mod = types.ModuleType("claude_agent_sdk")
    mod.TextBlock = _TextBlock  # type: ignore[attr-defined]
    mod.AssistantMessage = _AssistantMessage  # type: ignore[attr-defined]
    mod.ResultMessage = _ResultMessage  # type: ignore[attr-defined]
    mod.ClaudeAgentOptions = _ClaudeAgentOptions  # type: ignore[attr-defined]
    mod.ClaudeSDKError = _ClaudeSDKError  # type: ignore[attr-defined]
    mod.ProcessError = _ProcessError  # type: ignore[attr-defined]
    mod.CLINotFoundError = _CLINotFoundError  # type: ignore[attr-defined]
    # query will be patched per test
    mod.query = None  # type: ignore[attr-defined]
    return mod


def _make_async_gen(*msgs: Any):  # type: ignore[no-untyped-def]
    """Return an async-generator factory that yields *msgs*."""
    async def _gen(*, prompt: str, options: Any):  # type: ignore[no-untyped-def]
        for m in msgs:
            yield m
    return _gen


async def _raising_iter(exc: BaseException):  # type: ignore[no-untyped-def]
    """Async generator that immediately raises *exc* (helper for exception tests).

    The ``yield`` is placed behind a never-true condition to satisfy Python's
    async-generator syntax requirement without actually yielding any value.
    """
    raise exc
    yield  # pragma: no cover — unreachable; makes this an async generator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def inject_fake_sdk() -> Any:
    """Ensure claude_agent_sdk is in sys.modules for every test."""
    fake = _make_fake_sdk_module()
    with patch.dict(sys.modules, {"claude_agent_sdk": fake}):
        yield fake


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")


# ---------------------------------------------------------------------------
# Import after fixtures so the lazy import sees the patched sys.modules.
# ---------------------------------------------------------------------------

from claude_swarm.conductors.sdk import SDKConductor  # noqa: E402
from claude_swarm.heads import Builder  # noqa: E402
from claude_swarm.kanban import Task, TaskStatus  # noqa: E402

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSDKConductorHappyPath:
    def test_done_result(self, inject_fake_sdk: Any) -> None:
        """Happy path: AssistantMessage + ResultMessage(is_error=False) → DONE."""
        assistant_msg = _AssistantMessage([_TextBlock("intermediate")])
        result_msg = _ResultMessage(
            is_error=False,
            result="final answer",
            total_cost_usd=0.0123,
            subtype="success",
        )
        inject_fake_sdk.query = _make_async_gen(assistant_msg, result_msg)

        cond = SDKConductor(model_override="claude-haiku-4-5")
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="hello"))

        assert result.status is TaskStatus.DONE
        assert result.result == "final answer"

    def test_cost_usd_from_result_message(self, inject_fake_sdk: Any) -> None:
        """cost_usd must come from ResultMessage.total_cost_usd, not price_call."""
        result_msg = _ResultMessage(is_error=False, result="x", total_cost_usd=0.0456)
        inject_fake_sdk.query = _make_async_gen(result_msg)

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.cost_usd == pytest.approx(0.0456, rel=1e-6)

    def test_dispatch_is_synchronous(self, inject_fake_sdk: Any) -> None:
        """dispatch() must not return a coroutine."""
        inject_fake_sdk.query = _make_async_gen(
            _ResultMessage(is_error=False, result="ok", total_cost_usd=0.0)
        )
        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))
        # If result were a coroutine this would raise in the assertion below.
        assert not asyncio.iscoroutine(result)
        assert result.status is TaskStatus.DONE


class TestSDKConductorErrorPaths:
    def test_is_error_true(self, inject_fake_sdk: Any) -> None:
        """ResultMessage(is_error=True) → FAILED with subtype as error field."""
        result_msg = _ResultMessage(
            is_error=True,
            result=None,
            total_cost_usd=0.001,
            subtype="error_max_budget_usd",
        )
        inject_fake_sdk.query = _make_async_gen(result_msg)

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.status is TaskStatus.FAILED
        assert result.error == "error_max_budget_usd"
        assert result.cost_usd == pytest.approx(0.001, rel=1e-6)

    def test_result_none_fallback_to_assistant_text(
        self, inject_fake_sdk: Any
    ) -> None:
        """Zero-NULL guard: when ResultMessage.result is None, fall back to last AssistantMessage."""
        assistant_msg = _AssistantMessage([_TextBlock("fallback text")])
        result_msg = _ResultMessage(is_error=True, result=None, subtype="some_error")
        inject_fake_sdk.query = _make_async_gen(assistant_msg, result_msg)

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.result == "fallback text"

    def test_cli_not_found_error(self, inject_fake_sdk: Any) -> None:
        """CLINotFoundError → FAILED with descriptive error string."""
        async def _raising_gen(*, prompt: str, options: Any):  # type: ignore[no-untyped-def]
            async for item in _raising_iter(_CLINotFoundError("claude not on PATH")):
                yield item

        inject_fake_sdk.query = _raising_gen

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.status is TaskStatus.FAILED
        assert result.error is not None
        assert "not found" in result.error.lower() or "CLI" in result.error

    def test_process_error(self, inject_fake_sdk: Any) -> None:
        """ProcessError → FAILED."""
        async def _raising_gen(*, prompt: str, options: Any):  # type: ignore[no-untyped-def]
            async for item in _raising_iter(_ProcessError("exit 1", exit_code=1)):
                yield item

        inject_fake_sdk.query = _raising_gen

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.status is TaskStatus.FAILED

    def test_sdk_error(self, inject_fake_sdk: Any) -> None:
        """ClaudeSDKError → FAILED."""
        async def _raising_gen(*, prompt: str, options: Any):  # type: ignore[no-untyped-def]
            async for item in _raising_iter(_ClaudeSDKError("generic sdk error")):
                yield item

        inject_fake_sdk.query = _raising_gen

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.status is TaskStatus.FAILED

    def test_missing_api_key(
        self,
        inject_fake_sdk: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset ANTHROPIC_API_KEY → fail-fast FAILED before spawning the CLI."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        call_count = 0

        async def _gen(*, prompt: str, options: Any):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            yield _ResultMessage(is_error=False, result="x")

        inject_fake_sdk.query = _gen

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.status is TaskStatus.FAILED
        assert "ANTHROPIC_API_KEY" in (result.error or "")
        # The generator must NOT have been invoked (fail-fast before query()).
        assert call_count == 0

    def test_no_result_message_received(self, inject_fake_sdk: Any) -> None:
        """If query() completes without a ResultMessage → soft FAILED."""
        # Only an AssistantMessage, no ResultMessage.
        inject_fake_sdk.query = _make_async_gen(
            _AssistantMessage([_TextBlock("partial")])
        )

        cond = SDKConductor()
        result = cond.dispatch(head=Builder(), task=Task(title="t", prompt="x"))

        assert result.status is TaskStatus.FAILED
        assert result.error is not None
