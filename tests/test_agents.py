"""Tests for the persistent-agent state module (claude_swarm.agents)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claude_swarm.agents import (
    SCHEMA_VERSION,
    AgentState,
    deregister,
    get,
    list_all,
    record_dispatch,
    register,
    restore,
)


def test_agent_state_roundtrip_minimal() -> None:
    s = AgentState(team="t1", name="worker-a")
    d = s.to_dict()
    s2 = AgentState.from_dict(d)
    assert s2.team == "t1"
    assert s2.name == "worker-a"
    assert s2.head == "builder"
    assert s2.persistent is True


def test_agent_state_roundtrip_full(tmp_path: Path) -> None:
    s = AgentState(
        team="visibility",
        name="auditor-1",
        head="auditor",
        pid=12345,
        current_task_id="0019xxxx-aaaa",
        conversation_path="/tmp/conv.jsonl",
    )
    s.extra["custom_metric"] = 42
    d = s.to_dict()
    s2 = AgentState.from_dict(d)
    assert s2.head == "auditor"
    assert s2.pid == 12345
    assert s2.current_task_id == "0019xxxx-aaaa"
    assert s2.extra["custom_metric"] == 42


def test_schema_version_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="schema version mismatch"):
        AgentState.from_dict({
            "team": "t",
            "name": "n",
            "schema_version": SCHEMA_VERSION + 1,
        })


def test_register_creates_file(tmp_path: Path) -> None:
    s = AgentState(team="t", name="alpha")
    path = register(tmp_path, s)
    assert path.exists()
    assert path.name == "alpha.json"
    loaded = json.loads(path.read_text())
    assert loaded["name"] == "alpha"
    assert loaded["team"] == "t"


def test_register_is_atomic(tmp_path: Path) -> None:
    # tmpfile + os.replace should never leave a half-written file visible.
    s = AgentState(team="t", name="atomic-test")
    path = register(tmp_path, s)
    # No leftover tmp files in the agents dir
    leftovers = list(path.parent.glob("atomic-test.json.*"))
    assert leftovers == []


def test_get_missing_returns_none(tmp_path: Path) -> None:
    assert get(tmp_path, "nonexistent") is None


def test_get_returns_state(tmp_path: Path) -> None:
    register(tmp_path, AgentState(team="t", name="x", head="merger"))
    s = get(tmp_path, "x")
    assert s is not None
    assert s.head == "merger"


def test_list_all_returns_sorted(tmp_path: Path) -> None:
    register(tmp_path, AgentState(team="t", name="z"))
    register(tmp_path, AgentState(team="t", name="a"))
    register(tmp_path, AgentState(team="t", name="m"))
    names = [a.name for a in list_all(tmp_path)]
    assert names == ["a", "m", "z"]


def test_list_all_skips_unreadable(tmp_path: Path) -> None:
    register(tmp_path, AgentState(team="t", name="good"))
    bad = (tmp_path / "agents") / "broken.json"
    bad.write_text("{not json")
    states = list_all(tmp_path)
    # The broken file is skipped, not crashed
    assert len(states) == 1
    assert states[0].name == "good"


def test_is_alive_no_pid() -> None:
    s = AgentState(team="t", name="x", pid=None)
    assert s.is_alive() is False


def test_is_alive_with_real_pid() -> None:
    # Our own pid is definitely alive
    s = AgentState(team="t", name="x", pid=os.getpid())
    assert s.is_alive() is True


def test_is_alive_with_dead_pid() -> None:
    # PID 1 (init) exists but PID 999999 is very unlikely to
    s = AgentState(team="t", name="x", pid=999999)
    assert s.is_alive() is False


def test_restore_filters_dead(tmp_path: Path) -> None:
    register(tmp_path, AgentState(team="t", name="alive", pid=os.getpid()))
    register(tmp_path, AgentState(team="t", name="dead", pid=999999))
    register(tmp_path, AgentState(team="t", name="never-had-pid", pid=None))
    alive = restore(tmp_path)
    names = [a.name for a in alive]
    assert names == ["alive"]


def test_deregister_returns_true_when_exists(tmp_path: Path) -> None:
    register(tmp_path, AgentState(team="t", name="goner"))
    assert deregister(tmp_path, "goner") is True
    assert deregister(tmp_path, "goner") is False  # second call


def test_record_dispatch_updates_in_place(tmp_path: Path) -> None:
    register(tmp_path, AgentState(team="t", name="dispatcher", pid=None))
    before = get(tmp_path, "dispatcher")
    assert before is not None
    assert before.current_task_id is None

    record_dispatch(tmp_path, "dispatcher", task_id="task-42", pid=os.getpid())
    after = get(tmp_path, "dispatcher")
    assert after is not None
    assert after.current_task_id == "task-42"
    assert after.pid == os.getpid()
    assert after.last_seen >= before.last_seen


def test_record_dispatch_unknown_agent_is_noop(tmp_path: Path) -> None:
    # No exception, no file created
    record_dispatch(tmp_path, "nobody-home", task_id="task-1")
    assert get(tmp_path, "nobody-home") is None


def test_last_seen_advances_on_re_register(tmp_path: Path) -> None:
    s = AgentState(team="t", name="x")
    register(tmp_path, s)
    first_seen = get(tmp_path, "x").last_seen  # type: ignore[union-attr]
    time.sleep(0.01)
    register(tmp_path, s)
    second_seen = get(tmp_path, "x").last_seen  # type: ignore[union-attr]
    assert second_seen >= first_seen


def test_extra_field_round_trips_unknown_keys(tmp_path: Path) -> None:
    path = register(tmp_path, AgentState(team="t", name="x"))
    # Manually inject an unknown key on disk
    raw = json.loads(path.read_text())
    raw["future_native_field"] = "anthropic-side-only"
    path.write_text(json.dumps(raw))

    loaded = get(tmp_path, "x")
    assert loaded is not None
    assert loaded.extra.get("future_native_field") == "anthropic-side-only"


def test_schema_is_filesystem_safe(tmp_path: Path) -> None:
    # The name field becomes the filename — verify the round-trip preserves
    # standard filesystem-safe characters
    for name in ["worker-a", "worker_b", "worker.c", "worker123"]:
        register(tmp_path, AgentState(team="t", name=name))
        assert get(tmp_path, name) is not None
