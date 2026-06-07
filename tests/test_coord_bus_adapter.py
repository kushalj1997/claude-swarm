"""Tests for CoordBusAdapter -- no-op mode and interface contract."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from claude_swarm.coord_bus_adapter import CoordBusAdapter, CoordMessage
from claude_swarm.kanban import Task, TaskStatus


# ---------- helpers ---------------------------------------------------------

def _done_task(**kw) -> Task:
    return Task(status=TaskStatus.DONE, title="Test task", cost_usd=0.05, **kw)


def _failed_task(**kw) -> Task:
    return Task(status=TaskStatus.FAILED, title="Failed task", error="boom!", **kw)


# ---------- no-op mode (default, no DSN set) --------------------------------

class TestNoOpMode:
    def test_not_active_without_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert not adapter.is_active

    def test_announce_task_done_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert not adapter.announce_task_done(_done_task(), head_name="builder")

    def test_announce_task_failed_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert not adapter.announce_task_failed(_failed_task(), head_name="builder")

    def test_announce_heartbeat_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert not adapter.announce_heartbeat(queue_depth=3, in_progress=1, running_cost_usd=0.10)

    def test_recent_messages_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert adapter.recent_messages() == []

    def test_try_claim_area_returns_true_no_bus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No bus = no contention; claim always succeeds."""
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert adapter.try_claim_area("my/area.py")

    def test_release_area_returns_true_no_bus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert adapter.release_area("my/area.py")


# ---------- explicit disabled mode ------------------------------------------

class TestExplicitlyDisabled:
    def test_enabled_false_overrides_dsn(self) -> None:
        adapter = CoordBusAdapter(dsn="postgresql://fake", enabled=False)
        assert not adapter.is_active
        assert not adapter.announce_task_done(_done_task(), head_name="builder")
        assert adapter.recent_messages() == []
        assert adapter.try_claim_area("any")


# ---------- repr -----------------------------------------------------------

class TestRepr:
    def test_repr_contains_sender(self) -> None:
        adapter = CoordBusAdapter(swarm_sender="my-swarm")
        assert "my-swarm" in repr(adapter)

    def test_repr_shows_dsn_set_not_value(self) -> None:
        adapter = CoordBusAdapter(dsn="postgresql://secret:pw@host/db")
        assert "secret" not in repr(adapter)
        assert "<set>" in repr(adapter)

    def test_repr_shows_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_COORD_BUS_DSN", raising=False)
        adapter = CoordBusAdapter()
        assert "<not set>" in repr(adapter)


# ---------- CoordMessage dataclass -----------------------------------------

class TestCoordMessage:
    def test_defaults(self) -> None:
        msg = CoordMessage()
        assert msg.sender == ""
        assert msg.payload == {}
        assert not msg.read

    def test_construction(self) -> None:
        msg = CoordMessage(sender="codex", recipient="swarm", type="work_completed", summary="done")
        assert msg.sender == "codex"
        assert msg.type == "work_completed"
