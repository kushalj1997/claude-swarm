"""claude-swarm — generic swarm orchestration for Claude Code teammates.

A dependency-light Python library + CLI that turns a single Claude Code
session into a coordinated swarm of named "heads" (Scanner, Reviewer,
Builder, Merger, Test-Runner, Auditor) operating on a DAG-aware kanban,
with per-task git worktrees, an abort-marker contract, and an auto-merge
pipeline.

The public surface is intentionally narrow: import what you need from the
top-level namespace and the package will not pull in heavy optional deps
unless you ask for them.
"""
from __future__ import annotations

from .abort import (
    AbortMarker,
    AbortRequested,
    abort_marker_path,
    check_abort,
    raise_if_aborted,
)
from .heads import (
    Auditor,
    Builder,
    Head,
    HeadKind,
    Merger,
    Reviewer,
    Scanner,
    TestRunner,
)
from .kanban import Kanban, Task, TaskStatus
from .messaging import Inbox, Message, MessageBus
from .reviewer_checkpoint import ReviewerCheckpoint
from .supervisor import (
    Conductor,
    DispatchResult,
    StubConductor,
    Supervisor,
    SupervisorConfig,
)
from .worktree import PullRequest, WorktreeManager

__all__ = [
    "AbortMarker",
    "AbortRequested",
    "Auditor",
    "Builder",
    "Conductor",
    "DispatchResult",
    "Head",
    "HeadKind",
    "Inbox",
    "Kanban",
    "Merger",
    "Message",
    "MessageBus",
    "PullRequest",
    "Reviewer",
    "ReviewerCheckpoint",
    "Scanner",
    "StubConductor",
    "Supervisor",
    "SupervisorConfig",
    "Task",
    "TaskStatus",
    "TestRunner",
    "WorktreeManager",
    "abort_marker_path",
    "check_abort",
    "raise_if_aborted",
]

__version__ = "0.1.0"
