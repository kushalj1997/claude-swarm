"""Tests for the unified task-delegation bus (default JSON-inbox transport).

Covers the agent taxonomy, the delegation lifecycle, status tracking with a
kanban link, validation edge cases, and the kanban mirror. Postgres-transport
tests live in ``test_postgres_bus.py`` (skipped when no coord_db is reachable).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_swarm.bus import (
    BROADCAST,
    AgentClass,
    Delegation,
    DelegationStatus,
    TaskBus,
    validate_send,
)
from claude_swarm.kanban import Kanban, Task, TaskStatus
from claude_swarm.messaging import Message

# --------------------------------------------------------------------------
# Taxonomy + validation
# --------------------------------------------------------------------------


def test_all_agent_types_are_valid_recipients() -> None:
    for recipient in ("claude-code", "codex", "cursor", "api-worker"):
        # Should not raise.
        validate_send("dispatch", recipient, DelegationStatus.DELEGATED.value)


def test_supervisor_roles_are_valid_senders() -> None:
    for sender in ("scout", "planner", "dispatch", "meta"):
        validate_send(sender, "codex", "task_delegated")


def test_per_class_wildcard_recipient_accepted() -> None:
    validate_send("dispatch", "agent:claude-code", "task_delegated")
    validate_send("dispatch", "agent:cursor", "task_delegated")


def test_unknown_agent_class_wildcard_rejected() -> None:
    with pytest.raises(ValueError, match="not a valid participant"):
        validate_send("dispatch", "agent:nonsense", "task_delegated")


def test_broadcast_recipient_allowed_but_not_as_sender() -> None:
    validate_send("dispatch", BROADCAST, "heartbeat")  # ok as recipient
    with pytest.raises(ValueError, match="sender"):
        validate_send(BROADCAST, "codex", "heartbeat")


def test_class_wildcard_rejected_as_sender() -> None:
    # A wildcard is a fan-out recipient; a message must come FROM a concrete
    # agent. ``agent:codex`` as a sender is meaningless and must be rejected.
    validate_send("dispatch", "agent:codex", "task_delegated")  # ok as recipient
    with pytest.raises(ValueError, match="sender"):
        validate_send("agent:codex", "dispatch", "task_done")


def test_invalid_sender_rejected() -> None:
    with pytest.raises(ValueError, match="sender"):
        validate_send("ghost", "codex", "task_delegated")


def test_invalid_msg_type_rejected() -> None:
    with pytest.raises(ValueError, match="msg_type"):
        validate_send("dispatch", "codex", "not_a_real_verb")


def test_task_and_coordination_and_verify_verbs_all_valid() -> None:
    # Task lifecycle
    for verb in (
        "task_delegated",
        "task_claimed",
        "task_progress",
        "task_done",
        "task_blocked",
        "task_failed",
    ):
        validate_send("dispatch", "codex", verb)
    # Coordination chatter (back-compat with deep-ai)
    for verb in ("work_started", "heartbeat", "blocker", "slack_inbound"):
        validate_send("dispatch", "codex", verb)
    # Adversarial-verifier handshake
    for verb in ("verify_request", "verify_result"):
        validate_send("dispatch", "codex", verb)


def test_agent_class_enum_round_trip() -> None:
    assert AgentClass.CODEX.value == "codex"
    assert AgentClass("cursor") is AgentClass.CURSOR


# --------------------------------------------------------------------------
# Delegation dataclass round-trip
# --------------------------------------------------------------------------


def test_delegation_to_message_round_trip() -> None:
    deleg = Delegation(
        task_ref="task-123",
        sender="dispatch",
        recipient="codex",
        summary="fix the thing",
        branch="fix/thing",
        pr_number=42,
        payload={"prompt": "do X", "files_owned": ["a.py"]},
    )
    msg = deleg.to_message()
    assert msg.kind == "task_delegated"
    assert msg.body["task_ref"] == "task-123"
    back = Delegation.from_message(msg)
    assert back.task_ref == "task-123"
    assert back.sender == "dispatch"
    assert back.recipient == "codex"
    assert back.branch == "fix/thing"
    assert back.pr_number == 42
    assert back.payload["prompt"] == "do X"
    assert back.status is DelegationStatus.DELEGATED


def test_from_message_rejects_coordination_message() -> None:
    msg = Message(sender="dispatch", recipient="codex", kind="heartbeat", body={})
    with pytest.raises(ValueError, match="not a task-delegation verb"):
        Delegation.from_message(msg)


def test_from_message_rejects_missing_task_ref() -> None:
    msg = Message(
        sender="dispatch", recipient="codex", kind="task_delegated", body={}
    )
    with pytest.raises(ValueError, match="missing task_ref"):
        Delegation.from_message(msg)


def test_terminal_property() -> None:
    done = Delegation(task_ref="t", sender="codex", recipient="dispatch",
                      status=DelegationStatus.DONE)
    progress = Delegation(task_ref="t", sender="codex", recipient="dispatch",
                          status=DelegationStatus.PROGRESS)
    assert done.is_terminal is True
    assert progress.is_terminal is False


# --------------------------------------------------------------------------
# TaskBus — delegation happy path (default JSON transport)
# --------------------------------------------------------------------------


def test_delegate_lands_in_recipient_inbox(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    deleg = bus.delegate(
        sender="dispatch",
        recipient="codex",
        task_ref="task-1",
        prompt="implement the parser",
        files_owned=["parser.py"],
        acceptance=["tests pass", "mypy clean"],
        route="DELEGATE_DIRECT",
    )
    inbox = bus.delegations("codex")
    assert len(inbox) == 1
    got = inbox[0]
    assert got.task_ref == "task-1"
    assert got.status is DelegationStatus.DELEGATED
    assert got.payload["files_owned"] == ["parser.py"]
    assert got.payload["acceptance"] == ["tests pass", "mypy clean"]
    assert got.payload["route"] == "DELEGATE_DIRECT"
    assert deleg.id == got.id


def test_full_lifecycle_status_progression(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    bus.delegate(
        sender="dispatch", recipient="codex",
        task_ref="task-7", prompt="do work",
    )
    # Codex reports progression.
    bus.update_status(sender="codex", recipient="dispatch", task_ref="task-7",
                      status=DelegationStatus.CLAIMED)
    bus.update_status(sender="codex", recipient="dispatch", task_ref="task-7",
                      status=DelegationStatus.PROGRESS, summary="50% done")
    bus.update_status(sender="codex", recipient="dispatch", task_ref="task-7",
                      status=DelegationStatus.DONE, branch="feat/x", pr_number=99)

    latest = bus.status_of("dispatch", "task-7")
    assert latest is not None
    assert latest.status is DelegationStatus.DONE
    assert latest.branch == "feat/x"
    assert latest.pr_number == 99


def test_status_of_returns_latest_not_first(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    bus.update_status(sender="codex", recipient="dispatch", task_ref="t",
                      status=DelegationStatus.CLAIMED)
    bus.update_status(sender="codex", recipient="dispatch", task_ref="t",
                      status=DelegationStatus.FAILED, summary="oops")
    latest = bus.status_of("dispatch", "t")
    assert latest is not None
    assert latest.status is DelegationStatus.FAILED


def test_status_of_unknown_task_is_none(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    assert bus.status_of("dispatch", "never-existed") is None


def test_delegations_filter_by_status(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    bus.delegate(sender="dispatch", recipient="codex", task_ref="a", prompt="x")
    bus.delegate(sender="dispatch", recipient="codex", task_ref="b", prompt="y")
    bus.update_status(sender="dispatch", recipient="codex", task_ref="a",
                      status=DelegationStatus.PROGRESS)
    delegated = bus.delegations("codex", status=DelegationStatus.DELEGATED)
    progressing = bus.delegations("codex", status=DelegationStatus.PROGRESS)
    assert {d.task_ref for d in delegated} == {"a", "b"}
    assert {d.task_ref for d in progressing} == {"a"}


def test_coordination_chatter_excluded_from_delegations(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    bus.send(sender="dispatch", recipient="codex", msg_type="heartbeat",
             summary="alive")
    bus.delegate(sender="dispatch", recipient="codex", task_ref="real",
                 prompt="do it")
    delegs = bus.delegations("codex")
    assert len(delegs) == 1
    assert delegs[0].task_ref == "real"
    # The heartbeat still lives in the raw inbox though.
    assert len(bus.inbox("codex")) == 2


def test_drain_clears_inbox(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    bus.delegate(sender="dispatch", recipient="codex", task_ref="z", prompt="p")
    drained = bus.drain("codex")
    assert len(drained) == 1
    assert bus.inbox("codex") == []


def test_fan_to_agent_class_wildcard(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    deleg = bus.delegate(
        sender="dispatch", recipient="agent:cursor",
        task_ref="fan-1", prompt="headless check",
    )
    assert deleg.recipient == "agent:cursor"
    inbox = bus.delegations("agent:cursor")
    assert len(inbox) == 1
    assert inbox[0].task_ref == "fan-1"


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


def test_delegate_requires_task_ref(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    with pytest.raises(ValueError, match="task_ref"):
        bus.delegate(sender="dispatch", recipient="codex", task_ref="",
                     prompt="x")


def test_delegate_requires_prompt(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    with pytest.raises(ValueError, match="prompt"):
        bus.delegate(sender="dispatch", recipient="codex", task_ref="t",
                     prompt="")


def test_delegate_rejects_invalid_recipient(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    with pytest.raises(ValueError, match="recipient"):
        bus.delegate(sender="dispatch", recipient="ghost", task_ref="t",
                     prompt="x")


def test_update_status_requires_task_ref(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)
    with pytest.raises(ValueError, match="task_ref"):
        bus.update_status(sender="codex", recipient="dispatch", task_ref="",
                          status=DelegationStatus.DONE)


# --------------------------------------------------------------------------
# Kanban mirror — status flows bus → kanban
# --------------------------------------------------------------------------


def test_kanban_mirror_on_terminal_status(tmp_path: Path) -> None:
    kb = Kanban(tmp_path / "kanban.sqlite")
    task = kb.submit(Task(title="t", prompt="p", required_head="builder"))
    bus = TaskBus(root=tmp_path, kanban=kb)

    bus.delegate(sender="dispatch", recipient="codex", task_ref=task.id,
                 prompt="do it")
    bus.update_status(sender="codex", recipient="dispatch", task_ref=task.id,
                      status=DelegationStatus.CLAIMED)
    assert kb.get(task.id).status is TaskStatus.IN_PROGRESS  # type: ignore[union-attr]

    bus.update_status(sender="codex", recipient="dispatch", task_ref=task.id,
                      status=DelegationStatus.DONE, pr_number=7)
    row = kb.get(task.id)
    assert row is not None
    assert row.status is TaskStatus.DONE
    assert row.pr_path == "PR#7"


def test_kanban_mirror_failed_status(tmp_path: Path) -> None:
    kb = Kanban(tmp_path / "kanban.sqlite")
    task = kb.submit(Task(title="t", prompt="p", required_head="builder"))
    bus = TaskBus(root=tmp_path, kanban=kb)
    bus.update_status(sender="codex", recipient="dispatch", task_ref=task.id,
                      status=DelegationStatus.FAILED, summary="broke")
    assert kb.get(task.id).status is TaskStatus.FAILED  # type: ignore[union-attr]


def test_kanban_mirror_unknown_task_does_not_raise(tmp_path: Path) -> None:
    kb = Kanban(tmp_path / "kanban.sqlite")
    bus = TaskBus(root=tmp_path, kanban=kb)
    # task_ref not in the kanban — mirror must swallow the miss.
    bus.update_status(sender="codex", recipient="dispatch",
                      task_ref="not-in-kanban", status=DelegationStatus.DONE)


def test_no_kanban_means_no_mirror(tmp_path: Path) -> None:
    bus = TaskBus(root=tmp_path)  # no kanban wired
    # Should be a clean no-op (no exception).
    bus.update_status(sender="codex", recipient="dispatch", task_ref="t",
                      status=DelegationStatus.DONE)
    assert bus.status_of("dispatch", "t") is not None
