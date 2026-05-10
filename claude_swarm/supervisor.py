"""Supervisor — single-writer dispatch loop over a kanban + roster.

This is the orchestrator's brain. It owns no state of its own; everything
lives on the kanban + filesystem. The loop:

    1. Poll for unblocked pending tasks (DAG-aware).
    2. Pick one whose ``required_head`` matches an idle head in the roster.
    3. Dispatch via the configured :class:`Conductor` (which actually runs
       the worker — by default a stub that just records the dispatch).
    4. On completion, either mark the task done or kick the merge pipeline.

The supervisor is single-writer by design: enforce the singleton at the OS
level (``flock``, pidfile, or systemd) — duplicates corrupt the kanban.

The conductor is intentionally pluggable. The default conductor is a
no-op stub useful for tests + examples; downstream packages (e.g. a
Claude Code plugin) ship a real conductor that spawns subagents.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .abort import AbortMarker, AbortRequested
from .heads import Head, default_roster
from .kanban import Kanban, Task, TaskStatus
from .messaging import MessageBus
from .reviewer_checkpoint import ReviewerCheckpoint

log = logging.getLogger(__name__)


class Conductor(Protocol):
    """Pluggable strategy for actually running a head against a task."""

    def dispatch(self, *, head: Head, task: Task) -> DispatchResult:
        """Run ``head`` against ``task`` and return the outcome."""
        ...


@dataclass
class DispatchResult:
    """Outcome of one dispatch."""

    status: TaskStatus
    cost_usd: float = 0.0
    result: str | None = None
    error: str | None = None
    pr_path: str | None = None


@dataclass
class StubConductor:
    """Default no-op conductor used by tests + the toy examples.

    Records the dispatch in ``calls`` and immediately marks the task done.
    Replace with a real Claude-API or claude-CLI conductor in production.
    """

    calls: list[tuple[str, str]] = field(default_factory=list)
    completion_status: TaskStatus = TaskStatus.DONE

    def dispatch(self, *, head: Head, task: Task) -> DispatchResult:
        self.calls.append((head.name, task.id))
        log.info("stub-dispatch head=%s task=%s", head.name, task.id)
        return DispatchResult(status=self.completion_status, cost_usd=0.0)


@dataclass
class SupervisorConfig:
    """Tunables for :class:`Supervisor`."""

    poll_interval_s: float = 1.0
    max_iterations: int | None = None
    teammate_name: str = "supervisor"
    abort_root: Path | None = None
    cost_cap_usd: float = 10.0
    checkpoint: ReviewerCheckpoint = field(default_factory=ReviewerCheckpoint)


class Supervisor:
    """Picks unblocked tasks off the kanban and dispatches them via heads."""

    def __init__(
        self,
        *,
        kanban: Kanban,
        roster: dict[str, Head] | None = None,
        conductor: Conductor | None = None,
        bus: MessageBus | None = None,
        config: SupervisorConfig | None = None,
    ) -> None:
        self.kanban = kanban
        self.roster = roster or default_roster()
        self.conductor = conductor or StubConductor()
        self.bus = bus or MessageBus()
        self.config = config or SupervisorConfig()
        self._abort: AbortMarker | None = None
        if self.config.abort_root is not None:
            self._abort = AbortMarker(
                worktree_root=self.config.abort_root,
                teammate=self.config.teammate_name,
            )
        self._cost_so_far_usd: float = 0.0
        self._turn: int = 0

    def _pick_head(self, task: Task) -> Head | None:
        # Exact match on required_head wins; fall back to "builder".
        h = self.roster.get(task.required_head)
        if h is not None:
            return h
        return self.roster.get("builder")

    def step(self) -> Task | None:
        """Run a single supervisor iteration. Returns the dispatched task."""
        if self._abort is not None:
            self._abort.raise_if_set()
        unblocked = self.kanban.unblocked(limit=1)
        if not unblocked:
            return None
        task = unblocked[0]
        head = self._pick_head(task)
        if head is None:
            log.warning("no head matches required=%r for task %s", task.required_head, task.id)
            return None
        claimed = self.kanban.claim_one(
            worker_id=f"{self.config.teammate_name}:{head.name}",
            required_head=task.required_head,
        )
        if claimed is None or claimed.id != task.id:
            return None
        try:
            outcome = self.conductor.dispatch(head=head, task=claimed)
        except AbortRequested:
            self.kanban.transition(claimed.id, TaskStatus.PENDING, reason="aborted")
            raise
        except Exception as exc:
            log.exception("conductor crashed for task %s", claimed.id)
            self.kanban.update(
                claimed.id,
                status=TaskStatus.FAILED,
                error=repr(exc),
                completed_at=time.time(),
            )
            return claimed
        self._cost_so_far_usd += outcome.cost_usd
        self._turn += 1
        self.kanban.update(
            claimed.id,
            status=outcome.status,
            cost_usd=outcome.cost_usd,
            result=outcome.result,
            error=outcome.error,
            pr_path=outcome.pr_path,
            completed_at=time.time(),
        )
        return claimed

    def run(self, *, on_idle: Callable[[], None] | None = None) -> None:
        """Run the dispatch loop until the kanban is drained or aborted.

        Honours :attr:`SupervisorConfig.max_iterations` (handy for tests)
        and the abort marker. The optional ``on_idle`` callback is invoked
        when a poll finds no work; it's a hook for the caller to inject
        scanner runs, status writes, etc.
        """
        iterations = 0
        while True:
            if self.config.max_iterations is not None and iterations >= self.config.max_iterations:
                return
            try:
                dispatched = self.step()
            except AbortRequested:
                log.info("supervisor aborted via marker; exiting cleanly")
                return
            if dispatched is None:
                if on_idle is not None:
                    on_idle()
                # Drain check: if no pending and no in-progress, done.
                pending = self.kanban.list_tasks(status=TaskStatus.PENDING)
                in_prog = self.kanban.list_tasks(status=TaskStatus.IN_PROGRESS)
                if not pending and not in_prog:
                    return
                time.sleep(self.config.poll_interval_s)
            iterations += 1

    # ----- introspection ---------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return a snapshot suitable for ``status.json``."""
        return {
            "teammate": self.config.teammate_name,
            "turn": self._turn,
            "cost_so_far_usd": round(self._cost_so_far_usd, 4),
            "cost_cap_usd": self.config.cost_cap_usd,
            "kanban": {
                "pending": len(self.kanban.list_tasks(status=TaskStatus.PENDING)),
                "in_progress": len(self.kanban.list_tasks(status=TaskStatus.IN_PROGRESS)),
                "done": len(self.kanban.list_tasks(status=TaskStatus.DONE)),
                "failed": len(self.kanban.list_tasks(status=TaskStatus.FAILED)),
            },
            "heads": sorted(self.roster.keys()),
        }


__all__ = [
    "Conductor",
    "DispatchResult",
    "StubConductor",
    "Supervisor",
    "SupervisorConfig",
]
