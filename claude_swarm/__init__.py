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
from .roles import (
    Agent,
    DynamicWorkflow,
    EphemeralAgent,
    Lead,
    MetaSupervisor,
    Role,
    RoleKind,
    default_ladder,
)
from .roles import Supervisor as SupervisorRole  # alias: the class wins the bare name
from .routing import Route, RoutingDecision, route_task
from .supervisor import (
    Conductor,
    DispatchResult,
    StubConductor,
    Supervisor,
    SupervisorConfig,
)
from .workflow import (
    ApiWorkflowAgent,
    Pass,
    PassResult,
    StubWorkflowAgent,
    WorkflowAgent,
    WorkflowConfig,
    WorkflowReport,
    WorkflowRunner,
)
from .worktree import PullRequest, WorktreeManager

__all__ = [
    "AbortMarker",
    "AbortRequested",
    "Agent",
    "ApiWorkflowAgent",
    "Auditor",
    "Builder",
    "Conductor",
    "DispatchResult",
    "DynamicWorkflow",
    "EphemeralAgent",
    "Head",
    "HeadKind",
    "Inbox",
    "Kanban",
    "Lead",
    "Merger",
    "Message",
    "MessageBus",
    "MetaSupervisor",
    "Pass",
    "PassResult",
    "PullRequest",
    "Reviewer",
    "ReviewerCheckpoint",
    "Role",
    "RoleKind",
    "Route",
    "RoutingDecision",
    "Scanner",
    "StubConductor",
    "StubWorkflowAgent",
    "Supervisor",
    "SupervisorConfig",
    "SupervisorRole",
    "Task",
    "TaskStatus",
    "TestRunner",
    "WorkflowAgent",
    "WorkflowConfig",
    "WorkflowReport",
    "WorkflowRunner",
    "WorktreeManager",
    "abort_marker_path",
    "check_abort",
    "default_ladder",
    "raise_if_aborted",
    "route_task",
]

__version__ = "0.1.0"
