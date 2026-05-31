"""SDKConductor — dispatch via the ``claude-agent-sdk`` query() generator.

Satisfies the :class:`~claude_swarm.supervisor.Conductor` protocol:
one ``dispatch(*, head, task) -> DispatchResult`` method.

The SDK's ``query()`` function spawns the bundled Claude Code CLI subprocess
(~213 MB binary) that executes the full agentic loop internally — so this
conductor does *not* re-implement a tool loop.

``asyncio.run()`` safety note: ``dispatch()`` is synchronous and calls
``asyncio.run()`` internally.  This is safe under the current synchronous
:class:`~claude_swarm.supervisor.Supervisor`; both reference conductors
(``SubprocessConductor``, ``ClaudeCLIConductor``) are also synchronous.  A
future async supervisor would need to call ``_run()`` directly with ``await``
rather than going through ``dispatch()``.

``permission_mode`` note: the default is ``"bypassPermissions"``.  The
non-interactive CLI subprocess will hang indefinitely on the first "dangerous
tool" confirmation prompt when run in any other mode, which looks like a
running-but-stuck process (charter §1 / global CLAUDE.md §16).

Cost source: ``ResultMessage.total_cost_usd`` (the CLI already prices the
run).  Do *not* call ``cost.py.price_call()`` here — that would double-count.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..heads import Head
from ..kanban import Task, TaskStatus
from ..supervisor import DispatchResult

log = logging.getLogger(__name__)


@dataclass
class SDKConductor:
    """Conduct a task via the ``claude-agent-sdk`` ``query()`` generator.

    Fields
    ------
    model_override:
        If set, overrides ``head.default_model`` for every dispatch.
    max_budget_usd:
        Per-task budget passed to the SDK's native cost-cap mechanism.
        When the budget is exceeded the SDK returns a ``ResultMessage``
        with ``is_error=True`` and ``subtype='error_max_budget_usd'``.
    cwd:
        Working directory for the CLI subprocess.
    permission_mode:
        Passed verbatim to ``ClaudeAgentOptions``.  Use
        ``"bypassPermissions"`` (default) for autonomous non-interactive
        runs; any other mode risks a hanging subprocess.
    """

    model_override: str | None = None
    max_budget_usd: float = 5.0
    cwd: Path | None = None
    permission_mode: str = "bypassPermissions"

    # ------------------------------------------------------------------
    # Conductor protocol
    # ------------------------------------------------------------------

    def dispatch(self, *, head: Head, task: Task) -> DispatchResult:
        """Run *head* against *task* via the claude-agent-sdk."""
        return asyncio.run(self._run(head, task))

    async def _run(self, head: Head, task: Task) -> DispatchResult:
        # Lazy import — claude_agent_sdk is an optional dep (not in CI).
        # The mypy override in pyproject.toml covers `claude_agent_sdk.*`.
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKError,
                CLINotFoundError,
                ProcessError,
                ResultMessage,
                TextBlock,
                query,
            )
        except ImportError as exc:  # pragma: no cover
            return DispatchResult(
                status=TaskStatus.FAILED,
                error=f"claude_agent_sdk not installed: {exc}",
            )

        # Fail-fast on missing API key (June-15 API-key-only policy).
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return DispatchResult(
                status=TaskStatus.FAILED,
                error="ANTHROPIC_API_KEY is not set; SDKConductor requires an explicit key.",
            )

        model = self.model_override or head.default_model

        opts = ClaudeAgentOptions(
            model=model,
            system_prompt=head.system_prompt,
            allowed_tools=list(head.allowed_tools),
            cwd=self.cwd,
            max_turns=task.max_turns,
            max_budget_usd=self.max_budget_usd,
            permission_mode=self.permission_mode,
            env={"ANTHROPIC_API_KEY": api_key},
        )

        last_assistant_text: str = ""
        result_msg: ResultMessage | None = None

        try:
            async for msg in query(prompt=task.prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    parts: list[str] = []
                    for blk in msg.content:
                        if isinstance(blk, TextBlock) and blk.text:
                            parts.append(blk.text)
                    if parts:
                        last_assistant_text = "\n".join(parts)
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
        except CLINotFoundError as exc:
            return DispatchResult(
                status=TaskStatus.FAILED,
                error=f"claude CLI not found: {exc}",
            )
        except ProcessError as exc:
            return DispatchResult(
                status=TaskStatus.FAILED,
                error=f"claude process error (exit={getattr(exc, 'exit_code', '?')}): {exc}",
            )
        except ClaudeSDKError as exc:
            return DispatchResult(
                status=TaskStatus.FAILED,
                error=f"claude SDK error: {exc}",
            )

        if result_msg is None:
            # No ResultMessage received — treat as a soft failure.
            return DispatchResult(
                status=TaskStatus.FAILED,
                error="SDKConductor: query() completed without a ResultMessage.",
                result=last_assistant_text or None,
            )

        # Zero-NULL guard: result can be None on error subtypes.
        result_text: str | None = result_msg.result
        if not result_text:
            result_text = last_assistant_text or None

        cost_usd: float = getattr(result_msg, "total_cost_usd", 0.0) or 0.0
        subtype: str = getattr(result_msg, "subtype", "") or ""
        is_error: bool = bool(getattr(result_msg, "is_error", False))

        log.info(
            "sdk-dispatch done head=%s task=%s model=%s subtype=%s cost_usd=%.4f",
            head.name,
            task.id,
            model,
            subtype,
            cost_usd,
        )

        if is_error:
            return DispatchResult(
                status=TaskStatus.FAILED,
                result=result_text,
                error=subtype or "sdk_error",
                cost_usd=cost_usd,
            )

        return DispatchResult(
            status=TaskStatus.DONE,
            result=result_text,
            cost_usd=cost_usd,
        )


__all__ = ["SDKConductor"]
