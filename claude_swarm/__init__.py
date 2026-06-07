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
from .autoresearch_ingest import (
    AutoresearchChunkingPolicy,
    AutoresearchIngestRequest,
    AutoresearchIngestResult,
    build_autoresearch_ingest_requests,
    merge_autoresearch_ingest_results,
)
from .bus import (
    AgentClass,
    Delegation,
    DelegationStatus,
    TaskBus,
    validate_send,
)
from .governor import Governor, GovernorConfig, GovernorDecision, Mode
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
from .perpetual import (
    CallableWorkSource,
    DuplicateTeamError,
    NullWorkSource,
    PerpetualConfig,
    PerpetualStats,
    PerpetualSupervisor,
    PidfileGuard,
    WorkSource,
    build_cached_blocks,
    run_perpetual_team,
)
from .resilience import (
    BackoffPolicy,
    KeyRotator,
    ResilientCallStats,
    TransientError,
    cache_safe_sleep,
    classify_error,
    resilient_call,
    retry_after_from_headers,
)
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
from .usage import (
    Lane,
    LaneState,
    LaneView,
    UsageSnapshot,
    UsageTracker,
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
    "AgentClass",
    "ApiWorkflowAgent",
    "AutoresearchChunkingPolicy",
    "AutoresearchIngestRequest",
    "AutoresearchIngestResult",
    "Auditor",
    "BackoffPolicy",
    "Builder",
    "CallableWorkSource",
    "Conductor",
    "Delegation",
    "DelegationStatus",
    "DispatchResult",
    "DuplicateTeamError",
    "DynamicWorkflow",
    "EphemeralAgent",
    "Governor",
    "GovernorConfig",
    "GovernorDecision",
    "Head",
    "HeadKind",
    "Inbox",
    "Kanban",
    "KeyRotator",
    "Lane",
    "LaneState",
    "LaneView",
    "Lead",
    "Merger",
    "Message",
    "MessageBus",
    "MetaSupervisor",
    "Mode",
    "NullWorkSource",
    "Pass",
    "PassResult",
    "PerpetualConfig",
    "PerpetualStats",
    "PerpetualSupervisor",
    "PidfileGuard",
    "PullRequest",
    "ResilientCallStats",
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
    "TaskBus",
    "TaskStatus",
    "TestRunner",
    "TransientError",
    "UsageSnapshot",
    "UsageTracker",
    "WorkSource",
    "WorkflowAgent",
    "WorkflowConfig",
    "WorkflowReport",
    "WorkflowRunner",
    "WorktreeManager",
    "abort_marker_path",
    "build_autoresearch_ingest_requests",
    "build_cached_blocks",
    "cache_safe_sleep",
    "check_abort",
    "classify_error",
    "default_ladder",
    "merge_autoresearch_ingest_results",
    "raise_if_aborted",
    "resilient_call",
    "retry_after_from_headers",
    "route_task",
    "run_perpetual_team",
    "validate_send",
]

__version__ = "0.1.0"
