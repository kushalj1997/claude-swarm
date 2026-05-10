"""Kanban + DAG iterator tests."""
from __future__ import annotations

import pytest

from claude_swarm.kanban import Kanban, Task, TaskStatus


def test_submit_and_get(kanban: Kanban) -> None:
    t = kanban.submit(Task(title="hello", prompt="hi"))
    fetched = kanban.get(t.id)
    assert fetched is not None
    assert fetched.title == "hello"
    assert fetched.status is TaskStatus.PENDING


def test_unblocked_returns_only_ready_tasks(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    b = kanban.submit(Task(title="b", prompt="b", blocked_by=[a.id]))
    c = kanban.submit(Task(title="c", prompt="c", blocked_by=[b.id]))
    ready = kanban.unblocked()
    ids = [t.id for t in ready]
    assert ids == [a.id]
    # complete a; b should now unblock.
    kanban.transition(a.id, TaskStatus.DONE)
    ready_ids = {t.id for t in kanban.unblocked()}
    assert b.id in ready_ids
    assert c.id not in ready_ids


def test_claim_one_is_atomic(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    claimed = kanban.claim_one(worker_id="w1")
    assert claimed is not None
    assert claimed.id == a.id
    assert claimed.status is TaskStatus.IN_PROGRESS
    second = kanban.claim_one(worker_id="w2")
    assert second is None


def test_required_head_filter(kanban: Kanban) -> None:
    bld = kanban.submit(Task(title="b", prompt="p", required_head="builder"))
    rev = kanban.submit(Task(title="r", prompt="p", required_head="reviewer"))
    only_reviewer = kanban.unblocked(required_head="reviewer")
    assert [t.id for t in only_reviewer] == [rev.id]
    only_builder = kanban.unblocked(required_head="builder")
    assert [t.id for t in only_builder] == [bld.id]


def test_status_timeline_records_transitions(kanban: Kanban) -> None:
    t = kanban.submit(Task(title="t", prompt="p"))
    kanban.transition(t.id, TaskStatus.IN_PROGRESS, reason="claim")
    kanban.transition(t.id, TaskStatus.DONE, reason="finished")
    rows = kanban.timeline(t.id)
    statuses = [r["to_status"] for r in rows]
    assert statuses == ["pending", "in_progress", "done"]


def test_add_blocked_by_mirrors_blocks(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    b = kanban.submit(Task(title="b", prompt="b"))
    kanban.add_blocked_by(b.id, [a.id])
    fetched_b = kanban.get(b.id)
    fetched_a = kanban.get(a.id)
    assert fetched_b is not None and fetched_a is not None
    assert a.id in fetched_b.blocked_by
    assert b.id in fetched_a.blocks


def test_add_blocks_inverse(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    b = kanban.submit(Task(title="b", prompt="b"))
    kanban.add_blocks(a.id, [b.id])
    fetched_b = kanban.get(b.id)
    assert fetched_b is not None
    assert a.id in fetched_b.blocked_by


def test_unknown_task_returns_none(kanban: Kanban) -> None:
    assert kanban.get("nope") is None
    assert kanban.update("nope", status=TaskStatus.DONE) is None


def test_list_filters(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a", tags=["x"]))
    b = kanban.submit(Task(title="b", prompt="b", tags=["y"]))
    kanban.transition(b.id, TaskStatus.DONE)
    pending = kanban.list_tasks(status=TaskStatus.PENDING)
    assert {t.id for t in pending} == {a.id}
    only_x = kanban.list_tasks(tag="x")
    assert {t.id for t in only_x} == {a.id}


def test_duplicate_id_raises(kanban: Kanban) -> None:
    t = kanban.submit(Task(title="a", prompt="a"))
    with pytest.raises(Exception):  # noqa: B017
        kanban.submit(Task(id=t.id, title="dup", prompt="dup"))
