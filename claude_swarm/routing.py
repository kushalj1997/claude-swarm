"""Routing — the supervisor's "agent vs. lead vs. ephemeral" decision.

This encodes the operator's explicit hybridization ask
(``docs/AUTONOMY_ARCHITECTURE.md`` §2.2):

    "supervisors hand small TASK chunks to agents (not only leads)"

Today every dispatch goes to one head with no decomposition at the supervisor
layer. The target: the supervisor itself fans a small chunk straight to an
**agent**, escalating to a **lead** only when the task is genuinely compound,
and short-circuiting a one-shot verdict (review/test/audit) to an
**ephemeral-agent**. This mirrors how a senior engineer hands a one-file fix
straight to a junior but routes a feature through a tech-lead.

The decision is a pure function of cheap, planner-stamped heuristics on the
task — no LLM call, no side effects — so it is deterministic and trivially
testable. It is also **backwards-compatible**: a task with no routing metadata
defaults to :attr:`Route.DELEGATE_DIRECT`, i.e. exactly today's single-head
dispatch path.

Dependency-light: stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .kanban import Task

# Metadata keys the planner stamps onto ``Task.metadata`` (§3.2). Kept as
# named constants so the planner and the router cannot drift on string keys.
META_ESTIMATED_SUBTASKS = "estimated_subtasks"
META_EPHEMERAL = "ephemeral"
META_ROUTE_OVERRIDE = "route"

# A task touching more than this many distinct files is "compound" even if its
# subtask estimate is low, because spanning many owners needs a lead to
# sequence the sub-DAG. Mirrors the §2.2 "spans multiple owners" clause.
DEFAULT_FILE_FANOUT_THRESHOLD = 3


class Route(str, Enum):
    """Where a supervisor sends a task."""

    DELEGATE_DIRECT = "delegate_direct"  # supervisor -> agent (the common case)
    DELEGATE_LEAD = "delegate_lead"      # supervisor -> lead -> decompose -> child agents
    EPHEMERAL = "ephemeral"              # one-shot verdict (review / test / audit)


@dataclass(frozen=True)
class RoutingDecision:
    """The router's verdict for one task, with the reason it chose it."""

    route: Route
    reason: str


def _estimated_subtasks(task: Task) -> int:
    """Read the planner's subtask estimate, defaulting to 1 (atomic).

    A non-integer or negative value is coerced to ``1`` so a malformed stamp
    degrades to the safe, backwards-compatible direct path rather than raising.
    """
    raw = task.metadata.get(META_ESTIMATED_SUBTASKS, 1)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _is_ephemeral(task: Task) -> bool:
    """A task is one-shot iff the planner flagged it OR its head is a pure
    verdict-producer (reviewer / test-runner / auditor never open a PR).
    """
    if bool(task.metadata.get(META_EPHEMERAL, False)):
        return True
    return task.required_head in {"reviewer", "test-runner", "auditor"}


def _override(task: Task) -> Route | None:
    """Honour an explicit ``metadata['route']`` override if it is a valid Route."""
    raw = task.metadata.get(META_ROUTE_OVERRIDE)
    if raw is None:
        return None
    try:
        return Route(raw)
    except ValueError:
        return None


def route_task(
    task: Task,
    *,
    file_fanout_threshold: int = DEFAULT_FILE_FANOUT_THRESHOLD,
) -> RoutingDecision:
    """Decide where ``task`` goes. Pure function — no side effects.

    Decision order (first match wins):

    1. An explicit, valid ``metadata['route']`` override is obeyed verbatim
       (lets the planner force a route when its richer model disagrees with
       the heuristic).
    2. A one-shot verdict task (planner-flagged ``ephemeral`` or a
       review/test/audit head) -> :attr:`Route.EPHEMERAL`.
    3. A compound task — more than one estimated subtask, OR spanning more
       than ``file_fanout_threshold`` owned files -> :attr:`Route.DELEGATE_LEAD`.
    4. Otherwise -> :attr:`Route.DELEGATE_DIRECT` (the backwards-compatible
       default: supervisor hands the chunk straight to an agent).
    """
    forced = _override(task)
    if forced is not None:
        return RoutingDecision(forced, reason=f"explicit override metadata[route]={forced.value}")

    if _is_ephemeral(task):
        return RoutingDecision(
            Route.EPHEMERAL,
            reason="one-shot verdict (ephemeral flag or review/test/audit head)",
        )

    subtasks = _estimated_subtasks(task)
    n_files = len(task.files_owned)
    if subtasks > 1:
        return RoutingDecision(
            Route.DELEGATE_LEAD,
            reason=f"compound: estimated_subtasks={subtasks} > 1",
        )
    if n_files > file_fanout_threshold:
        return RoutingDecision(
            Route.DELEGATE_LEAD,
            reason=f"compound: files_owned={n_files} > threshold {file_fanout_threshold}",
        )
    return RoutingDecision(
        Route.DELEGATE_DIRECT,
        reason="atomic chunk -> direct-to-agent",
    )


__all__ = [
    "DEFAULT_FILE_FANOUT_THRESHOLD",
    "META_EPHEMERAL",
    "META_ESTIMATED_SUBTASKS",
    "META_ROUTE_OVERRIDE",
    "Route",
    "RoutingDecision",
    "route_task",
]
