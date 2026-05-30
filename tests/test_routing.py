"""Tests for the supervisor routing decision (claude_swarm.routing)."""
from __future__ import annotations

from claude_swarm.kanban import Task
from claude_swarm.routing import (
    META_EPHEMERAL,
    META_ESTIMATED_SUBTASKS,
    META_ROUTE_OVERRIDE,
    Route,
    route_task,
)


def test_unstamped_task_defaults_to_direct() -> None:
    # Backwards-compat: a task with no routing metadata behaves like today's
    # single-head dispatch (direct to an agent).
    d = route_task(Task(title="fix typo", prompt="p"))
    assert d.route is Route.DELEGATE_DIRECT


def test_single_subtask_goes_direct() -> None:
    t = Task(title="t", prompt="p", metadata={META_ESTIMATED_SUBTASKS: 1})
    assert route_task(t).route is Route.DELEGATE_DIRECT


def test_multi_subtask_escalates_to_lead() -> None:
    t = Task(title="feature", prompt="p", metadata={META_ESTIMATED_SUBTASKS: 4})
    d = route_task(t)
    assert d.route is Route.DELEGATE_LEAD
    assert "estimated_subtasks=4" in d.reason


def test_many_files_escalates_to_lead_even_with_one_subtask() -> None:
    t = Task(
        title="cross-cutting",
        prompt="p",
        metadata={META_ESTIMATED_SUBTASKS: 1},
        files_owned=["a.py", "b.py", "c.py", "d.py"],  # > default threshold 3
    )
    d = route_task(t)
    assert d.route is Route.DELEGATE_LEAD
    assert "files_owned=4" in d.reason


def test_file_count_at_threshold_stays_direct() -> None:
    # Boundary: exactly the threshold is NOT compound (strictly greater-than).
    t = Task(title="t", prompt="p", files_owned=["a.py", "b.py", "c.py"])
    assert route_task(t).route is Route.DELEGATE_DIRECT


def test_custom_threshold_is_honoured() -> None:
    t = Task(title="t", prompt="p", files_owned=["a.py", "b.py"])
    assert route_task(t, file_fanout_threshold=1).route is Route.DELEGATE_LEAD
    assert route_task(t, file_fanout_threshold=5).route is Route.DELEGATE_DIRECT


def test_reviewer_head_routes_ephemeral() -> None:
    t = Task(title="review", prompt="p", required_head="reviewer")
    assert route_task(t).route is Route.EPHEMERAL


def test_test_runner_and_auditor_heads_route_ephemeral() -> None:
    assert route_task(Task(required_head="test-runner")).route is Route.EPHEMERAL
    assert route_task(Task(required_head="auditor")).route is Route.EPHEMERAL


def test_explicit_ephemeral_flag_routes_ephemeral() -> None:
    t = Task(title="t", prompt="p", metadata={META_EPHEMERAL: True})
    assert route_task(t).route is Route.EPHEMERAL


def test_ephemeral_wins_over_compound_subtask_count() -> None:
    # A one-shot verdict task is ephemeral even if mis-stamped as compound.
    t = Task(
        required_head="reviewer",
        metadata={META_ESTIMATED_SUBTASKS: 9},
    )
    assert route_task(t).route is Route.EPHEMERAL


def test_explicit_route_override_is_obeyed() -> None:
    t = Task(
        title="t",
        prompt="p",
        metadata={META_ROUTE_OVERRIDE: "delegate_lead", META_ESTIMATED_SUBTASKS: 1},
    )
    d = route_task(t)
    assert d.route is Route.DELEGATE_LEAD
    assert "override" in d.reason


def test_override_beats_ephemeral_head() -> None:
    t = Task(
        required_head="reviewer",
        metadata={META_ROUTE_OVERRIDE: "delegate_direct"},
    )
    assert route_task(t).route is Route.DELEGATE_DIRECT


def test_invalid_override_is_ignored_and_falls_through() -> None:
    t = Task(title="t", prompt="p", metadata={META_ROUTE_OVERRIDE: "not_a_route"})
    assert route_task(t).route is Route.DELEGATE_DIRECT


def test_malformed_or_degenerate_subtask_estimate_degrades_to_direct() -> None:
    # Non-numeric or <1 estimates coerce to the safe atomic default.
    for bad in ("lots", None, -3, 0, 1.4):
        t = Task(title="t", prompt="p", metadata={META_ESTIMATED_SUBTASKS: bad})
        assert route_task(t).route is Route.DELEGATE_DIRECT, bad


def test_float_estimate_truncates_to_an_integer_count() -> None:
    # A float >= 2 truncates to a valid compound estimate (int(2.9) == 2 > 1).
    t = Task(title="t", prompt="p", metadata={META_ESTIMATED_SUBTASKS: 2.9})
    assert route_task(t).route is Route.DELEGATE_LEAD


def test_decision_is_pure_no_mutation() -> None:
    t = Task(title="t", prompt="p", metadata={META_ESTIMATED_SUBTASKS: 4}, files_owned=["a.py"])
    before_meta = dict(t.metadata)
    before_files = list(t.files_owned)
    route_task(t)
    assert t.metadata == before_meta
    assert t.files_owned == before_files
