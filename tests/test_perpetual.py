"""Tests for the never-sleep supervisor loop runtime."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_swarm.abort import AbortMarker
from claude_swarm.kanban import Kanban, Task, TaskStatus
from claude_swarm.perpetual import (
    CallableWorkSource,
    DuplicateTeamError,
    NullWorkSource,
    PerpetualConfig,
    PerpetualStats,
    PerpetualSupervisor,
    PidfileGuard,
    build_cached_blocks,
    run_perpetual_team,
)
from claude_swarm.resilience import BackoffPolicy, TransientError
from claude_swarm.supervisor import (
    DispatchResult,
    StubConductor,
    Supervisor,
    SupervisorConfig,
)


def _supervisor(kanban: Kanban, conductor: object | None = None) -> Supervisor:
    return Supervisor(
        kanban=kanban,
        conductor=conductor or StubConductor(),
        config=SupervisorConfig(poll_interval_s=0.0, wait_for_work=True),
    )


# ----- build_cached_blocks --------------------------------------------


def test_cached_blocks_attaches_cache_control_to_last() -> None:
    blocks = build_cached_blocks([("corpus", "AAA"), ("schema", "BBB")])
    assert len(blocks) == 2
    assert "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert blocks[0]["text"] == "AAA"


def test_cached_blocks_skips_empty_text() -> None:
    blocks = build_cached_blocks([("a", ""), ("b", "kept"), ("c", "")])
    assert len(blocks) == 1
    assert blocks[0]["text"] == "kept"
    assert "cache_control" in blocks[0]


def test_cached_blocks_empty_input() -> None:
    assert build_cached_blocks([]) == []
    assert build_cached_blocks([("a", ""), ("b", "")]) == []


def test_cached_blocks_custom_ttl() -> None:
    blocks = build_cached_blocks([("a", "x")], ttl="1h")
    assert blocks[-1]["cache_control"]["ttl"] == "1h"


# ----- single tick ----------------------------------------------------


def test_tick_dispatches_ready_task(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    loop = PerpetualSupervisor(supervisor=_supervisor(kanban))
    worked = loop.tick()
    assert worked is True
    assert loop.stats.dispatched == 1


def test_tick_idle_invokes_work_source(kanban: Kanban) -> None:
    filed: list[str] = []

    def gen(kb: Kanban) -> list[str]:
        t = kb.submit(Task(title="generated", prompt="g"))
        filed.append(t.id)
        return [t.id]

    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        work_source=CallableWorkSource(gen),
    )
    # Empty board -> tick generates work.
    worked = loop.tick()
    assert worked is False
    assert loop.stats.generated == 1
    assert kanban.list_tasks(status=TaskStatus.PENDING)[0].id == filed[0]


def test_null_work_source_files_nothing(kanban: Kanban) -> None:
    loop = PerpetualSupervisor(supervisor=_supervisor(kanban), work_source=NullWorkSource())
    loop.tick()
    assert loop.stats.generated == 0
    assert loop.stats.idle_ticks == 1


def test_work_source_exception_does_not_crash_loop(kanban: Kanban) -> None:
    def boom(_kb: Kanban) -> list[str]:
        raise RuntimeError("scan exploded")

    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        work_source=CallableWorkSource(boom),
    )
    # Tick swallows the work-source failure and continues.
    loop.tick()
    assert loop.stats.generated == 0
    assert loop.stats.idle_ticks == 1


def test_verify_hook_runs_after_dispatch(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    verified: list[str] = []
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        verify=verified.append,
    )
    loop.tick()
    assert len(verified) == 1


def test_verify_exception_does_not_crash_loop(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))

    def boom(_task_id: str) -> None:
        raise RuntimeError("verify exploded")

    loop = PerpetualSupervisor(supervisor=_supervisor(kanban), verify=boom)
    worked = loop.tick()  # must not raise
    assert worked is True


def test_tick_cost_preflight_rejects_before_claim(kanban: Kanban) -> None:
    task = kanban.submit(Task(title="costly", prompt="x", metadata={"cost_cap_usd": 0.01}))
    calls = StubConductor()
    loop = PerpetualSupervisor(supervisor=_supervisor(kanban, conductor=calls))

    worked = loop.tick()

    fresh = kanban.get(task.id)
    assert worked is False
    assert loop.stats.dispatched == 0
    assert fresh is not None
    assert fresh.status is TaskStatus.FAILED
    assert "cost_preflight" in (fresh.error or "")
    assert calls.calls == []
    timeline_statuses = [row["to_status"] for row in kanban.timeline(task.id)]
    assert timeline_statuses == [TaskStatus.PENDING.value, TaskStatus.FAILED.value]


# ----- the perpetual loop ---------------------------------------------


def test_run_stops_at_max_ticks(kanban: Kanban) -> None:
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        config=PerpetualConfig(max_ticks=3, idle_heartbeat_s=0.0, busy_poll_s=0.0),
    )
    stats = loop.run()
    assert stats.ticks == 3


def test_run_never_returns_on_empty_board_until_max_ticks(kanban: Kanban) -> None:
    # Empty board + NullWorkSource: a base supervisor would return immediately,
    # the perpetual loop keeps ticking until max_ticks.
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        config=PerpetualConfig(max_ticks=5, idle_heartbeat_s=0.0, busy_poll_s=0.0),
    )
    stats = loop.run()
    assert stats.ticks == 5
    assert stats.idle_ticks == 5


def test_run_drains_then_keeps_looping(kanban: Kanban) -> None:
    kanban.submit(Task(title="a", prompt="a"))
    kanban.submit(Task(title="b", prompt="b"))
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        config=PerpetualConfig(max_ticks=10, idle_heartbeat_s=0.0, busy_poll_s=0.0),
    )
    stats = loop.run()
    assert stats.dispatched == 2
    assert len(kanban.list_tasks(status=TaskStatus.DONE)) == 2


def test_stop_flag_exits_loop(kanban: Kanban) -> None:
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        config=PerpetualConfig(max_ticks=None, idle_heartbeat_s=0.0, busy_poll_s=0.0),
    )
    loop.stop()  # pre-set the stop flag
    stats = loop.run()
    # Stop is checked at the top of the loop -> zero ticks executed.
    assert stats.ticks == 0


def test_abort_marker_exits_loop(kanban: Kanban, tmp_path: Path) -> None:
    AbortMarker(worktree_root=tmp_path, teammate="perpetual").set()
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        config=PerpetualConfig(
            max_ticks=None,
            idle_heartbeat_s=0.0,
            busy_poll_s=0.0,
            abort_root=tmp_path,
        ),
    )
    stats = loop.run()
    assert stats.ticks == 0


def test_status_checkpoint_written(kanban: Kanban, tmp_path: Path) -> None:
    status_path = tmp_path / "perpetual.status.json"
    kanban.submit(Task(title="a", prompt="a"))
    loop = PerpetualSupervisor(
        supervisor=_supervisor(kanban),
        config=PerpetualConfig(
            max_ticks=1, idle_heartbeat_s=0.0, busy_poll_s=0.0, status_path=status_path
        ),
    )
    loop.run()
    assert status_path.exists()
    import json

    snap = json.loads(status_path.read_text())
    assert snap["name"] == "perpetual"
    assert "perpetual" in snap
    assert snap["perpetual"]["ticks"] >= 1


def test_status_includes_perpetual_block(kanban: Kanban) -> None:
    loop = PerpetualSupervisor(supervisor=_supervisor(kanban), config=PerpetualConfig(name="scout"))
    snap = loop.status()
    assert snap["name"] == "scout"
    assert "perpetual" in snap
    assert set(snap["perpetual"]).issuperset({"ticks", "dispatched", "generated"})


# ----- rate-limit resilience inside the loop --------------------------


def test_loop_recovers_from_transient_dispatch_failure(kanban: Kanban) -> None:
    """A throttled conductor dispatch is retried, not fatal.

    The conductor raises a :class:`TransientError` on its first dispatch. The
    perpetual loop returns the task to PENDING, backs off, and re-drives it on
    the next attempt — completing it and counting the recovery. A genuine
    (non-transient) bug would instead mark the task FAILED without killing the
    loop; that path is covered separately.
    """
    kanban.submit(Task(title="a", prompt="a"))

    class FlakyConductor:
        def __init__(self) -> None:
            self.n = 0

        def dispatch(self, *, head: object, task: object) -> DispatchResult:
            self.n += 1
            if self.n == 1:
                raise TransientError(message="throttled", status=429)
            return DispatchResult(status=TaskStatus.DONE)

    sup = Supervisor(
        kanban=kanban,
        conductor=FlakyConductor(),
        config=SupervisorConfig(poll_interval_s=0.0, wait_for_work=True),
    )
    loop = PerpetualSupervisor(
        supervisor=sup,
        config=PerpetualConfig(
            max_ticks=1,
            idle_heartbeat_s=0.0,
            busy_poll_s=0.0,
            backoff=BackoffPolicy(base_s=0.0, jitter=False),
        ),
    )
    loop.run()
    assert len(kanban.list_tasks(status=TaskStatus.DONE)) == 1
    assert loop.stats.transient_recoveries == 1
    assert loop.stats.dispatched == 1


def test_loop_marks_task_failed_on_non_transient_bug(kanban: Kanban) -> None:
    """A real (non-transient) conductor bug fails the task, not the loop."""
    kanban.submit(Task(title="a", prompt="a"))

    class BuggyConductor:
        def dispatch(self, *, head: object, task: object) -> DispatchResult:
            raise ValueError("real bug in dispatch")

    sup = Supervisor(
        kanban=kanban,
        conductor=BuggyConductor(),
        config=SupervisorConfig(poll_interval_s=0.0, wait_for_work=True),
    )
    loop = PerpetualSupervisor(
        supervisor=sup,
        config=PerpetualConfig(max_ticks=1, idle_heartbeat_s=0.0, busy_poll_s=0.0),
    )
    loop.run()  # must not raise
    failed = kanban.list_tasks(status=TaskStatus.FAILED)
    assert len(failed) == 1
    assert "real bug" in (failed[0].error or "")


# ----- pidfile guard --------------------------------------------------


def test_pidfile_guard_acquires_and_releases(tmp_path: Path) -> None:
    pidfile = tmp_path / "team.pid"
    guard = PidfileGuard(pidfile)
    guard.acquire()
    assert pidfile.read_text().strip() == str(os.getpid())
    guard.release()
    assert not pidfile.exists()


def test_pidfile_guard_rejects_live_duplicate(tmp_path: Path) -> None:
    pidfile = tmp_path / "team.pid"
    # Simulate a live owner == our own pid is excluded; use a parent-ish live pid.
    # The current process is alive, so writing our pid then a fresh guard with a
    # DIFFERENT live pid should reject. We fake a live owner by writing pid 1
    # (init, always alive) — acquire must reject.
    pidfile.write_text("1")
    guard = PidfileGuard(pidfile)
    with pytest.raises(DuplicateTeamError):
        guard.acquire()


def test_pidfile_guard_reclaims_stale(tmp_path: Path) -> None:
    pidfile = tmp_path / "team.pid"
    # A pid that is essentially never alive.
    pidfile.write_text("999999")
    guard = PidfileGuard(pidfile)
    guard.acquire()  # stale -> reclaimed
    assert pidfile.read_text().strip() == str(os.getpid())
    guard.release()


def test_pidfile_guard_handles_garbage(tmp_path: Path) -> None:
    pidfile = tmp_path / "team.pid"
    pidfile.write_text("not-a-pid")
    guard = PidfileGuard(pidfile)
    guard.acquire()  # unreadable -> treat as free
    assert pidfile.read_text().strip() == str(os.getpid())


# ----- team launcher --------------------------------------------------


def test_run_perpetual_team_starts_n_supervisors(kanban: Kanban, tmp_path: Path) -> None:
    for i in range(4):
        kanban.submit(Task(title=f"t{i}", prompt="x"))

    def factory(i: int) -> PerpetualSupervisor:
        return PerpetualSupervisor(
            supervisor=_supervisor(kanban),
            config=PerpetualConfig(
                name=f"sup-{i}", max_ticks=20, idle_heartbeat_s=0.0, busy_poll_s=0.0
            ),
        )

    sups = run_perpetual_team(
        kanban=kanban,
        count=2,
        supervisor_factory=factory,
        pidfile=tmp_path / "team.pid",
        join=True,
    )
    assert len(sups) == 2
    # All four tasks done; the atomic claim ensures no double-dispatch.
    assert len(kanban.list_tasks(status=TaskStatus.DONE)) == 4
    total = sum(s.stats.dispatched for s in sups)
    assert total == 4
    # Pidfile released after the team joined.
    assert not (tmp_path / "team.pid").exists()


def test_perpetual_team_never_orphans_tasks_under_contention(
    kanban: Kanban, tmp_path: Path
) -> None:
    """N concurrent loops over one kanban must complete every task.

    Regression for the claim-abandon leak: ``Supervisor.step`` peeks then
    claims then bails when a sibling won the peeked task, orphaning the
    claimed task in ``in_progress`` forever. The perpetual loop's claim-first
    driver must instead dispatch whatever it claimed, so a fully-drained run
    leaves zero ``in_progress`` and zero ``pending`` tasks.
    """
    n_tasks = 24
    for i in range(n_tasks):
        kanban.submit(Task(title=f"t{i}", prompt="x"))

    def factory(i: int) -> PerpetualSupervisor:
        return PerpetualSupervisor(
            supervisor=_supervisor(kanban),
            config=PerpetualConfig(
                name=f"sup-{i}", max_ticks=400, idle_heartbeat_s=0.0, busy_poll_s=0.0
            ),
        )

    sups = run_perpetual_team(
        kanban=kanban,
        count=4,
        supervisor_factory=factory,
        pidfile=tmp_path / "team.pid",
        join=True,
    )
    assert len(kanban.list_tasks(status=TaskStatus.DONE)) == n_tasks
    assert kanban.list_tasks(status=TaskStatus.IN_PROGRESS) == []
    assert kanban.list_tasks(status=TaskStatus.PENDING) == []
    # Every task is accounted for by exactly one loop (no double-dispatch).
    assert sum(s.stats.dispatched for s in sups) == n_tasks


def test_run_perpetual_team_rejects_zero_count(kanban: Kanban) -> None:
    with pytest.raises(ValueError):
        run_perpetual_team(
            kanban=kanban,
            count=0,
            supervisor_factory=lambda _i: PerpetualSupervisor(supervisor=_supervisor(kanban)),
        )


def test_run_perpetual_team_releases_pidfile_on_factory_error(
    kanban: Kanban, tmp_path: Path
) -> None:
    pidfile = tmp_path / "team.pid"

    def boom(_i: int) -> PerpetualSupervisor:
        raise RuntimeError("factory exploded")

    with pytest.raises(RuntimeError, match="factory exploded"):
        run_perpetual_team(
            kanban=kanban,
            count=1,
            supervisor_factory=boom,
            pidfile=pidfile,
        )
    # The pidfile must not be stranded — a later team can launch.
    assert not pidfile.exists()


def test_run_perpetual_team_no_join_releases_pidfile(kanban: Kanban, tmp_path: Path) -> None:
    import threading

    pidfile = tmp_path / "team.pid"

    def factory(i: int) -> PerpetualSupervisor:
        return PerpetualSupervisor(
            supervisor=_supervisor(kanban),
            config=PerpetualConfig(name=f"sup-{i}", max_ticks=None, idle_heartbeat_s=0.05),
        )

    sups = run_perpetual_team(
        kanban=kanban, count=1, supervisor_factory=factory, pidfile=pidfile, join=False
    )
    try:
        # join=False releases the guard immediately (it gated only the launch).
        assert not pidfile.exists()
    finally:
        for s in sups:
            s.stop()
        for t in [t for t in threading.enumerate() if t.name.startswith("sup-")]:
            t.join(timeout=5.0)


def test_run_perpetual_team_no_join_returns_live(kanban: Kanban, tmp_path: Path) -> None:
    import threading

    def factory(i: int) -> PerpetualSupervisor:
        return PerpetualSupervisor(
            supervisor=_supervisor(kanban),
            config=PerpetualConfig(
                name=f"sup-{i}", max_ticks=None, idle_heartbeat_s=0.05, busy_poll_s=0.0
            ),
        )

    sups = run_perpetual_team(
        kanban=kanban,
        count=2,
        supervisor_factory=factory,
        pidfile=tmp_path / "team.pid",
        join=False,
    )
    try:
        assert len(sups) == 2
    finally:
        # Stop + join every loop thread so it cannot outlive this test and
        # hit the torn-down kanban (the fixture wipes the sqlite file).
        for s in sups:
            s.stop()
        deadline = 5.0
        for t in list(threading.enumerate()):
            if t.name.startswith("sup-"):
                t.join(timeout=deadline)


def test_perpetual_stats_to_dict_shape() -> None:
    stats = PerpetualStats(ticks=3, dispatched=2)
    d = stats.to_dict()
    assert d["ticks"] == 3
    assert d["dispatched"] == 2
    assert "uptime_s" in d
