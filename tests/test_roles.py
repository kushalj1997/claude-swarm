"""Tests for the taxonomy ladder (claude_swarm.roles)."""
from __future__ import annotations

from claude_swarm.roles import (
    Agent,
    DynamicWorkflow,
    EphemeralAgent,
    Lead,
    MetaSupervisor,
    Role,
    RoleKind,
    Supervisor,
    default_ladder,
)


def test_default_ladder_has_six_rungs() -> None:
    ladder = default_ladder()
    kinds = {r.kind for r in ladder.values()}
    assert kinds == {
        RoleKind.META_SUPERVISOR,
        RoleKind.SUPERVISOR,
        RoleKind.LEAD,
        RoleKind.AGENT,
        RoleKind.EPHEMERAL_AGENT,
        RoleKind.DYNAMIC_WORKFLOW,
    }


def test_default_ladder_keys_are_unique() -> None:
    # Names must be unique or the dict comprehension silently drops a rung.
    roles = [MetaSupervisor(), Supervisor(), Lead(), Agent(), EphemeralAgent(), DynamicWorkflow()]
    names = [r.name for r in roles]
    assert len(names) == len(set(names))
    assert len(default_ladder()) == 6


def test_supervisors_are_perpetual_workers_are_not() -> None:
    assert MetaSupervisor().perpetual is True
    assert Supervisor().perpetual is True
    assert Agent().perpetual is False
    assert EphemeralAgent().perpetual is False
    assert Lead().perpetual is False


def test_only_pr_landing_roles_open_prs() -> None:
    # Agents + leads + workflows land code; supervisors + ephemerals never do.
    assert Agent().opens_pr is True
    assert Lead().opens_pr is True
    assert DynamicWorkflow().opens_pr is True
    assert MetaSupervisor().opens_pr is False
    assert Supervisor().opens_pr is False
    assert EphemeralAgent().opens_pr is False


def test_delegation_depth_decreases_down_the_ladder() -> None:
    assert MetaSupervisor().max_delegation_depth > Supervisor().max_delegation_depth
    assert Supervisor().max_delegation_depth > Lead().max_delegation_depth
    assert Lead().max_delegation_depth > Agent().max_delegation_depth
    assert EphemeralAgent().max_delegation_depth == 0  # leaf: spawns nothing


def test_can_spawn_enforces_the_ladder() -> None:
    # Supervisor may hand a TASK directly to an agent OR escalate to a lead
    # (the §2.2 hybridization rule) — both are legal direct spawns.
    sup = Supervisor()
    assert sup.can_spawn(RoleKind.AGENT) is True
    assert sup.can_spawn(RoleKind.LEAD) is True
    assert sup.can_spawn(RoleKind.EPHEMERAL_AGENT) is True
    # But a supervisor may not spawn another supervisor (single-writer invariant).
    assert sup.can_spawn(RoleKind.SUPERVISOR) is False
    assert sup.can_spawn(RoleKind.META_SUPERVISOR) is False


def test_meta_supervisor_only_spawns_supervisors() -> None:
    meta = MetaSupervisor()
    assert meta.can_spawn(RoleKind.SUPERVISOR) is True
    assert meta.can_spawn(RoleKind.AGENT) is False
    assert meta.can_spawn(RoleKind.LEAD) is False


def test_ephemeral_is_a_leaf_and_spawns_nothing() -> None:
    eph = EphemeralAgent()
    for kind in RoleKind:
        assert eph.can_spawn(kind) is False


def test_agent_may_spawn_only_ephemerals() -> None:
    agent = Agent()
    assert agent.can_spawn(RoleKind.EPHEMERAL_AGENT) is True
    assert agent.can_spawn(RoleKind.AGENT) is False
    assert agent.can_spawn(RoleKind.LEAD) is False


def test_dynamic_workflow_fans_out_to_agents() -> None:
    wf = DynamicWorkflow()
    assert wf.can_spawn(RoleKind.AGENT) is True
    assert wf.can_spawn(RoleKind.EPHEMERAL_AGENT) is True
    assert wf.can_spawn(RoleKind.SUPERVISOR) is False


def test_role_is_frozen() -> None:
    import dataclasses

    import pytest

    r = Agent()
    assert isinstance(r, Role)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.name = "mutated"  # type: ignore[misc]


def test_custom_name_distinguishes_instances() -> None:
    scout = Supervisor(name="scout")
    dispatch = Supervisor(name="dispatch")
    assert scout.kind is dispatch.kind
    assert scout.name != dispatch.name
