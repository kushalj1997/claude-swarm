"""Supervisor + StubConductor tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_swarm.abort import AbortMarker, AbortRequested
from claude_swarm.heads import default_roster
from claude_swarm.kanban import Kanban, Task, TaskStatus
from claude_swarm.supervisor import (
    DispatchResult,
    StubConductor,
    Supervisor,
    SupervisorConfig,
)


def test_step_dispatches_unblocked_task(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    sup = Supervisor(kanban=kanban, conductor=StubConductor())
    dispatched = sup.step()
    assert dispatched is not None
    assert dispatched.id == a.id
    fresh = kanban.get(a.id)
    assert fresh is not None
    assert fresh.status is TaskStatus.DONE


def test_step_skips_blocked_tasks(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    b = kanban.submit(Task(title="b", prompt="b", blocked_by=[a.id]))
    sup = Supervisor(kanban=kanban, conductor=StubConductor())
    sup.step()
    fresh_b = kanban.get(b.id)
    assert fresh_b is not None
    assert fresh_b.status is TaskStatus.PENDING


def test_run_drains_a_dag(kanban: Kanban) -> None:
    a = kanban.submit(Task(title="a", prompt="a"))
    b = kanban.submit(Task(title="b", prompt="b", blocked_by=[a.id]))
    c = kanban.submit(Task(title="c", prompt="c", blocked_by=[b.id]))
    sup = Supervisor(
        kanban=kanban,
        conductor=StubConductor(),
        config=SupervisorConfig(poll_interval_s=0.0, max_iterations=10),
    )
    sup.run()
    assert sup.status()["kanban"]["done"] == 3
    assert all(
        kanban.get(t.id).status is TaskStatus.DONE  # type: ignore[union-attr]
        for t in (a, b, c)
    )


def test_failed_dispatch_marks_task_failed(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    failing = StubConductor(completion_status=TaskStatus.FAILED)
    sup = Supervisor(kanban=kanban, conductor=failing)
    sup.step()
    pending = kanban.list_tasks(status=TaskStatus.FAILED)
    assert len(pending) == 1


def test_required_head_dispatch(kanban: Kanban) -> None:
    bld = kanban.submit(Task(title="b", prompt="b", required_head="builder"))
    rev = kanban.submit(Task(title="r", prompt="r", required_head="reviewer"))
    calls = StubConductor()
    sup = Supervisor(kanban=kanban, conductor=calls)
    sup.step()
    sup.step()
    head_names = {h for h, _ in calls.calls}
    assert "builder" in head_names
    assert "reviewer" in head_names
    assert {tid for _, tid in calls.calls} == {bld.id, rev.id}


def test_abort_marker_terminates_run(kanban: Kanban, tmp_path: Path) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    marker = AbortMarker(worktree_root=tmp_path, teammate="supervisor")
    marker.set()
    sup = Supervisor(
        kanban=kanban,
        conductor=StubConductor(),
        config=SupervisorConfig(
            poll_interval_s=0.0,
            max_iterations=5,
            abort_root=tmp_path,
        ),
    )
    sup.run()
    # Task remains pending — abort fired before dispatch.
    pending = kanban.list_tasks(status=TaskStatus.PENDING)
    assert len(pending) == 1


def test_step_raises_abort_outside_run(kanban: Kanban, tmp_path: Path) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    AbortMarker(worktree_root=tmp_path, teammate="supervisor").set()
    sup = Supervisor(
        kanban=kanban,
        conductor=StubConductor(),
        config=SupervisorConfig(
            poll_interval_s=0.0,
            abort_root=tmp_path,
        ),
    )
    with pytest.raises(AbortRequested):
        sup.step()


def test_status_includes_kanban_counts(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    sup = Supervisor(kanban=kanban, roster=default_roster(), conductor=StubConductor())
    snap = sup.status()
    assert snap["kanban"]["pending"] == 1
    assert "scanner" in snap["heads"]


def test_conductor_raises_marks_failed(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))

    class Boom:
        def dispatch(self, *, head, task) -> DispatchResult:  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    sup = Supervisor(kanban=kanban, conductor=Boom())
    sup.step()
    failed = kanban.list_tasks(status=TaskStatus.FAILED)
    assert len(failed) == 1
    assert "boom" in (failed[0].error or "")
