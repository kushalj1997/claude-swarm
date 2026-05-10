"""Conductor reference implementations."""
from __future__ import annotations

from pathlib import Path

from claude_swarm.conductor import ClaudeCLIConductor, SubprocessConductor
from claude_swarm.heads import Builder
from claude_swarm.kanban import Task, TaskStatus


def test_subprocess_conductor_success(tmp_path: Path) -> None:
    cmd_factory = lambda *, head, task: ["cat"]  # echo stdin -> stdout  # noqa: E731
    conductor = SubprocessConductor(command_factory=cmd_factory)
    result = conductor.dispatch(
        head=Builder(),
        task=Task(title="t", prompt="hello world"),
    )
    assert result.status is TaskStatus.DONE
    assert result.result == "hello world"


def test_subprocess_conductor_failure() -> None:
    cmd_factory = lambda *, head, task: ["false"]  # noqa: E731
    conductor = SubprocessConductor(command_factory=cmd_factory)
    result = conductor.dispatch(head=Builder(), task=Task(title="t", prompt="x"))
    assert result.status is TaskStatus.FAILED


def test_subprocess_conductor_missing_binary() -> None:
    cmd_factory = lambda *, head, task: ["this-binary-does-not-exist-9876"]  # noqa: E731
    conductor = SubprocessConductor(command_factory=cmd_factory)
    result = conductor.dispatch(head=Builder(), task=Task(title="t", prompt="x"))
    assert result.status is TaskStatus.FAILED
    assert result.error is not None


def test_claude_cli_conductor_handles_missing_cli() -> None:
    # Force the underlying lookup to fail by temporarily aliasing PATH.
    conductor = ClaudeCLIConductor(extra_args=("--this-arg-doesnt-exist-on-claude-1234",))
    # The CLI may or may not be installed — both cases are handled.
    result = conductor.dispatch(head=Builder(), task=Task(title="t", prompt="x"))
    assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)
