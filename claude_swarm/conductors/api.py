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

    Note: the package also exports ``build_cached_blocks`` (perpetual.py:179)
    which marks the LAST non-empty block and accepts ``(label, text)`` tuples.
    This helper is intentionally different — it marks the FIRST *count* blocks
    operating on pre-built block dicts, which is the right primitive for the
    system-prompt cache-marker pattern used here.  A future consolidation
    should generalise one of the two helpers rather than forking further.
    """
    marked: list[dict[str, Any]] = []
    for i, blk in enumerate(blocks):
        if i < count:
            blk = {**blk, "cache_control": {"type": "ephemeral", "ttl": ttl}}
        marked.append(blk)
    return marked


def _extract_text(content: list[Any]) -> str:
    """Pull the concatenated text from a list of Anthropic SDK content blocks.

    Only processes the object form produced by ``anthropic.Anthropic().messages.create()``;
    ``response.content`` is always a list of typed SDK objects (TextBlock,
    ToolUseBlock, etc.), never plain dicts.
    """
    parts: list[str] = []
    for blk in content:
        blk_type = getattr(blk, "type", None)
        if blk_type == "text":
            text = getattr(blk, "text", None)
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
        a *single* dispatch reaches or exceeds this value *and* the current
        turn is still in a tool-use loop (about to make another API call),
        the conductor returns
        ``DispatchResult(status=FAILED, error="hard_cost_cap_exceeded", cost_usd=running_cost)``.
        A completed ``end_turn`` response is NEVER discarded by this cap —
        if the final turn tips the cost over, the result is returned as DONE
        with the accumulated cost recorded.  Using ``DispatchResult`` rather
        than raising ``RuntimeError`` ensures the supervisor's normal path
        writes ``cost_usd`` to the kanban row (the generic-exception path at
        ``supervisor.py:152-159`` does not record cost).
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
        """Run *head* against *task* via the Anthropic Messages API (synchronous)."""
        import anthropic  # lazy — optional dep; mypy override in pyproject.toml

        client = anthropic.Anthropic()
        model = self.model_override or head.default_model

        # Build the system block list with a 1h cache marker on the leading block.
        # 1h TTL is correct for the system prompt: it is stable across turns and
        # the 1h tier (e.g. $10/M for Opus) is distinctly priced from the 5m tier.
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
        # Tracks whether the loop exited via the normal end_turn break vs
        # exhausting the turn budget while still in a tool-use loop.
        completed = False

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
            # Route cache-creation tokens to the 1h dimension because the
            # system prompt is marked with ttl="1h" above.  The Anthropic API
            # reports the per-TTL breakdown in usage.cache_creation.ephemeral_1h_input_tokens
            # / ephemeral_5m_input_tokens (anthropic>=0.39); the flat
            # cache_creation_input_tokens is the sum of both.  Since this
            # conductor only writes a 1h cache entry we route the entire flat
            # aggregate to cache_write_1h_tokens (the 5m dimension stays 0).
            usage = response.usage
            # Attempt to read the split breakdown if the SDK provides it;
            # fall back to the flat aggregate attributed to 1h.
            cache_creation = getattr(usage, "cache_creation", None)
            cache_write_1h: int
            cache_write_5m: int
            if cache_creation is not None:
                _1h = getattr(cache_creation, "ephemeral_1h_input_tokens", None)
                cache_write_1h = int(_1h) if _1h is not None else getattr(usage, "cache_creation_input_tokens", 0)
                cache_write_5m = int(getattr(cache_creation, "ephemeral_5m_input_tokens", 0))
            else:
                cache_write_1h = getattr(usage, "cache_creation_input_tokens", 0)
                cache_write_5m = 0

            turn_cost = price_call(
                model,
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
                cache_write_1h_tokens=cache_write_1h,
                cache_write_5m_tokens=cache_write_5m,
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

            stop_reason: str = getattr(response, "stop_reason", "") or ""

            if stop_reason != "tool_use":
                # End of turn — extract the text and return.
                # The cost cap is NOT checked here: a completed end_turn
                # response must always be returned with its result; discarding
                # a finished answer because its cost crossed the cap is the
                # wrong behaviour (see bug fix audit finding #2/#6).
                final_text = _extract_text(response.content)
                completed = True
                break

            # Hard cost cap — evaluated ONLY in the tool-use path, i.e. when
            # we are about to make another API call.  Checking here (not before
            # the stop_reason branch) ensures a completed end_turn turn whose
            # cost tips the cap is still returned as DONE, not silently discarded.
            # We return a DispatchResult rather than raising so the supervisor's
            # normal code path records cost_usd on the kanban row.
            if running_cost >= self.cost_cap_usd:
                log.warning(
                    "api-dispatch cost cap exceeded head=%s task=%s running=%.4f cap=%.4f",
                    head.name,
                    task.id,
                    running_cost,
                    self.cost_cap_usd,
                )
                return DispatchResult(
                    status=TaskStatus.FAILED,
                    result=final_text or None,
                    error=(
                        f"hard_cost_cap_exceeded: running=${running_cost:.4f}"
                        f" >= cap=${self.cost_cap_usd:.4f}"
                        f" (head={head.name} task={task.id} model={model})"
                    ),
                    cost_usd=running_cost,
                )

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

        # If the loop exited by turn-budget exhaustion (not via the end_turn
        # break), the task never produced a final answer — report FAILED so
        # the supervisor does NOT cascade-unblock dependent tasks with a
        # spurious DONE/result=None.  The cost so far is recorded so the
        # kanban row has an accurate cost_usd.
        if not completed:
            log.warning(
                "api-dispatch max_turns exhausted head=%s task=%s turns=%d cost_usd=%.4f",
                head.name,
                task.id,
                turn,
                running_cost,
            )
            return DispatchResult(
                status=TaskStatus.FAILED,
                result=None,
                error=f"max_turns ({task.max_turns}) exhausted in tool loop without end_turn",
                cost_usd=running_cost,
            )

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
