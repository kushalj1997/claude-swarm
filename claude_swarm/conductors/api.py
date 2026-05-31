"""ApiConductor — dispatch via the Anthropic Messages API with an inline tool loop.

Satisfies the :class:`~claude_swarm.supervisor.Conductor` protocol exactly:
one ``dispatch(*, head, task) -> DispatchResult`` method, zero additional
public surface.

The conductor is intentionally *scope-limited*: it ships without a registered
tool set, so ``stop_reason`` is always ``end_turn`` on the first turn (the
API never sends ``tool_use`` blocks when the ``tools`` list is empty).  The
tool-loop plumbing is wired and tested regardless — a downstream caller can
subclass and override ``_tools()`` to register a full tool registry.

Deferred to Phase 6 (noted in PR):
  - Batch API
  - Files API
  - full agentic tool registry
  - Memory tool / structured outputs / server tools
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cost import price_call
from ..heads import Head
from ..kanban import Task, TaskStatus
from ..supervisor import DispatchResult

log = logging.getLogger(__name__)

# Number of leading system-blocks to mark with a cache-control header.
# One block covers the system prompt; extend to 2+ if you prepend a large
# tool-spec block before the narrative prompt.
_CACHE_BLOCK_COUNT = 1


def _mark_cache_prefix(
    blocks: list[dict[str, Any]],
    *,
    count: int,
    ttl: str = "5m",
) -> list[dict[str, Any]]:
    """Return *blocks* with ``cache_control`` set on the first *count* entries.

    Ported from deep-ai worker.py:1407-1428 (cache marker helper).
    The ``ttl`` values ``"5m"`` and ``"1h"`` map to Anthropic's ephemeral
    cache tiers; use ``"1h"`` for the system prompt (stable across turns).
    """
    marked: list[dict[str, Any]] = []
    for i, blk in enumerate(blocks):
        if i < count:
            blk = {**blk, "cache_control": {"type": "ephemeral", "ttl": ttl}}
        marked.append(blk)
    return marked


def _extract_text(content: list[Any]) -> str:
    """Pull the concatenated text from a list of Anthropic content blocks."""
    parts: list[str] = []
    for blk in content:
        # Support both SDK objects (blk.type / blk.text) and plain dicts.
        blk_type = getattr(blk, "type", None) or (blk.get("type") if isinstance(blk, dict) else None)
        if blk_type == "text":
            text = getattr(blk, "text", None) or (blk.get("text") if isinstance(blk, dict) else None)
            if text:
                parts.append(str(text))
    return "\n".join(parts)


@dataclass
class ApiConductor:
    """Conduct a task via the Anthropic Messages API with a built-in tool loop.

    Fields
    ------
    model_override:
        If set, overrides ``head.default_model`` for every dispatch.
        Useful for demos or cost-capped runs (e.g. force Haiku while
        experimenting).
    cost_cap_usd:
        Hard per-task cost cap.  When the accumulated cost of API calls for
        a *single* dispatch reaches or exceeds this value the conductor raises
        ``RuntimeError("hard_cost_cap_exceeded: ...")``.  The supervisor
        catches this at ``supervisor.py:153`` and marks the task ``FAILED``.
        A plain ``RuntimeError`` is deliberate — ``AbortRequested`` would
        re-queue to ``PENDING`` (supervisor.py:149-151), looping forever on a
        task that always over-runs.
    cwd:
        Working directory hint (unused by the API path today; reserved for
        future tool-registry integration).
    """

    model_override: str | None = None
    cost_cap_usd: float = 5.0
    cwd: Path | None = None
    _extra_tools: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    # Conductor protocol
    # ------------------------------------------------------------------

    def dispatch(self, *, head: Head, task: Task) -> DispatchResult:
        """Run *head* against *task* via the Anthropic Messages API."""
        return asyncio.run(self._run(head, task))

    async def _run(self, head: Head, task: Task) -> DispatchResult:
        import anthropic  # lazy — optional dep; mypy override in pyproject.toml

        client = anthropic.Anthropic()
        model = self.model_override or head.default_model

        # Build the system block list with cache markers on the leading block.
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": head.system_prompt},
        ]
        system_blocks = _mark_cache_prefix(system_blocks, count=_CACHE_BLOCK_COUNT, ttl="1h")

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": task.prompt},
        ]

        tools = self._tools()
        running_cost: float = 0.0
        final_text: str = ""
        turn = 0

        while turn < task.max_turns:
            turn += 1
            kwargs: dict[str, Any] = {
                "model": model,
                "system": system_blocks,
                "messages": messages,
                "max_tokens": task.max_tokens,
            }
            if tools:
                kwargs["tools"] = tools

            response = client.messages.create(**kwargs)

            # Accumulate cost from this response.
            usage = response.usage
            turn_cost = price_call(
                model,
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
                cache_write_5m_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            )
            running_cost += turn_cost

            log.debug(
                "api-dispatch turn=%d head=%s task=%s model=%s cost=%.4f running=%.4f",
                turn,
                head.name,
                task.id,
                model,
                turn_cost,
                running_cost,
            )

            # Hard cost cap — raise a plain RuntimeError so the supervisor
            # marks the task FAILED rather than re-queueing (AbortRequested
            # triggers a re-queue via supervisor.py:149-151).
            if running_cost >= self.cost_cap_usd:
                raise RuntimeError(
                    f"hard_cost_cap_exceeded: running=${running_cost:.4f}"
                    f" >= cap=${self.cost_cap_usd:.4f}"
                    f" (head={head.name} task={task.id} model={model})"
                )

            stop_reason: str = getattr(response, "stop_reason", "") or ""

            if stop_reason != "tool_use":
                # End of turn — extract the text and return.
                final_text = _extract_text(response.content)
                break

            # Tool-use loop: append assistant content + synthesised tool
            # results, then loop back.
            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, Any]] = []
            for blk in response.content:
                blk_type = getattr(blk, "type", None)
                if blk_type == "tool_use":
                    tool_id = getattr(blk, "id", "unknown")
                    tool_name = getattr(blk, "name", "unknown")
                    # No tool registry in this scope — return a neutral error
                    # so the model can gracefully conclude.
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": (
                                f"Tool '{tool_name}' is not registered in this "
                                "ApiConductor. Conclude your response without "
                                "further tool calls."
                            ),
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        log.info(
            "api-dispatch done head=%s task=%s turns=%d cost_usd=%.4f",
            head.name,
            task.id,
            turn,
            running_cost,
        )
        return DispatchResult(
            status=TaskStatus.DONE,
            result=final_text or None,
            cost_usd=running_cost,
        )

    def _tools(self) -> list[dict[str, Any]]:
        """Return the tool-spec list for the API call.

        Empty by default — subclass and override to register a real tool
        registry.  An empty list prevents the API from ever returning
        ``stop_reason='tool_use'``, keeping the base-class loop simple.
        """
        return list(self._extra_tools)


__all__ = ["ApiConductor"]
