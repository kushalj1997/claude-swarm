"""Reference conductors for running heads against tasks.

The :class:`~claude_swarm.supervisor.Conductor` protocol is the seam between
the orchestration layer (kanban, DAG, dispatch) and the actual LLM call.
Real-world callers ship their own (e.g. a Claude Code plugin that spawns a
subagent). This module ships two reference implementations to make the
library useful out of the box and trivial to test.

* :class:`SubprocessConductor` — runs an arbitrary command per task, pipes
  the prompt on stdin, captures stdout as the result. Good fit for the
  ``claude --print`` CLI.
* :class:`ClaudeCLIConductor` — thin wrapper specialised for ``claude``.

Both are dependency-free (stdlib only).
"""
from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .heads import Head
from .kanban import Task, TaskStatus
from .supervisor import DispatchResult

log = logging.getLogger(__name__)


CommandFactory = Callable[..., Sequence[str]]


@dataclass
class SubprocessConductor:
    """Run an arbitrary command per dispatch.

    The command receives the task prompt on stdin. The exit code maps to the
    task status: 0 -> DONE, anything else -> FAILED. ``stdout`` is captured
    as ``result``; ``stderr`` as ``error`` on failure.
    """

    command_factory: CommandFactory
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    timeout_s: int = 600

    def dispatch(self, *, head: Head, task: Task) -> DispatchResult:
        cmd = list(self.command_factory(head=head, task=task))
        log.info("subprocess-dispatch head=%s task=%s cmd=%s", head.name, task.id, cmd[0])
        try:
            proc = subprocess.run(
                cmd,
                input=task.prompt,
                cwd=str(self.cwd) if self.cwd else None,
                env=dict(self.env) if self.env else None,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return DispatchResult(
                status=TaskStatus.FAILED,
                error=f"subprocess error: {exc}",
            )
        if proc.returncode == 0:
            return DispatchResult(
                status=TaskStatus.DONE,
                result=(proc.stdout or "").strip() or None,
            )
        return DispatchResult(
            status=TaskStatus.FAILED,
            result=(proc.stdout or "").strip() or None,
            error=(proc.stderr or "").strip()[-2000:] or None,
        )


@dataclass
class ClaudeCLIConductor:
    """Conductor that shells out to the ``claude`` CLI in non-interactive mode.

    Each dispatch runs ``claude --print --model <model>`` with the task prompt
    on stdin. The response on stdout becomes the task ``result``.

    ``model_override`` (optional) replaces the head's ``default_model`` for every
    dispatch — useful for the demo, which uses Haiku for all heads so the
    end-to-end run finishes in <30 seconds at minimal cost. Set to ``None`` for
    production runs where each head's role-appropriate default applies.
    """

    cwd: Path | None = None
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    timeout_s: int = 600
    model_override: str | None = None

    def dispatch(self, *, head: Head, task: Task) -> DispatchResult:
        model = self.model_override or head.default_model
        cmd = ["claude", "--print", "--model", model, *self.extra_args]
        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=task.prompt,
                cwd=str(self.cwd) if self.cwd else None,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return DispatchResult(
                status=TaskStatus.FAILED,
                error=f"claude CLI error: {exc}",
            )
        log.info(
            "claude-dispatch head=%s task=%s rc=%d elapsed=%.1fs",
            head.name,
            task.id,
            proc.returncode,
            time.time() - started,
        )
        if proc.returncode == 0:
            return DispatchResult(
                status=TaskStatus.DONE,
                result=(proc.stdout or "").strip() or None,
            )
        return DispatchResult(
            status=TaskStatus.FAILED,
            result=(proc.stdout or "").strip() or None,
            error=(proc.stderr or "").strip()[-2000:] or None,
        )


__all__ = ["ClaudeCLIConductor", "SubprocessConductor"]
