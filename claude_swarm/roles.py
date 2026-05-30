"""Roles — the swarm taxonomy ladder as explicit, testable descriptors.

Where :mod:`claude_swarm.heads` answers *"what tools + prompt does one worker
get?"*, this module answers *"where does that worker sit in the org?"*. The
two are orthogonal: a ``builder`` head can be embodied by an **agent** role
(executes one task end-to-end) or by a **lead** role (decomposes a compound
task and reviews children).

The ladder, highest authority first (mirrors ``docs/AUTONOMY_ARCHITECTURE.md``
§2.1):

    meta-supervisor  — health of all supervisors; election; rate-mode; restart
    supervisor       — one perpetual loop; claims work; delegates TASKS to
                       agents OR leads (the hybridization rule, §2.2)
    lead             — decomposes a *large* task into a sub-DAG; reviews + merges
    agent            — executes ONE task end-to-end; commits; opens a PR
    ephemeral-agent  — one bounded sub-step (a single review/test); returns a
                       structured verdict; no PR
    dynamic-workflow — a fan-out of independent agents that converge (the
                       ``/simplify`` + ``/code-review`` until-convergence cycle)

Encoding the taxonomy as data (not implicit behavior) makes the operator's
explicit asks — "supervisors hand small TASKS to agents, not only leads" and
"a souped-up dynamic workflow" — first-class, testable rules rather than
conventions. Each role carries the *maximum delegation depth* it is allowed to
spawn to, which the supervisor/lead enforce when they decompose work.

Dependency-light: stdlib only, same frozen-dataclass + factory-function style
as :mod:`claude_swarm.heads`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RoleKind(str, Enum):
    """The rung a worker occupies in the swarm org ladder."""

    META_SUPERVISOR = "meta-supervisor"
    SUPERVISOR = "supervisor"
    LEAD = "lead"
    AGENT = "agent"
    EPHEMERAL_AGENT = "ephemeral-agent"
    DYNAMIC_WORKFLOW = "dynamic-workflow"


# Each rung may spawn the rungs strictly below it. We encode the *set* of
# rungs a role may directly spawn so a delegation request that jumps a rung
# (e.g. a supervisor trying to spawn another supervisor) is rejectable.
_SPAWNABLE: dict[RoleKind, frozenset[RoleKind]] = {
    RoleKind.META_SUPERVISOR: frozenset({RoleKind.SUPERVISOR}),
    RoleKind.SUPERVISOR: frozenset(
        {RoleKind.LEAD, RoleKind.AGENT, RoleKind.EPHEMERAL_AGENT, RoleKind.DYNAMIC_WORKFLOW}
    ),
    RoleKind.LEAD: frozenset(
        {RoleKind.AGENT, RoleKind.EPHEMERAL_AGENT, RoleKind.DYNAMIC_WORKFLOW}
    ),
    RoleKind.AGENT: frozenset({RoleKind.EPHEMERAL_AGENT}),
    RoleKind.EPHEMERAL_AGENT: frozenset(),
    RoleKind.DYNAMIC_WORKFLOW: frozenset({RoleKind.AGENT, RoleKind.EPHEMERAL_AGENT}),
}


@dataclass(frozen=True)
class Role:
    """Declarative descriptor for one rung of the swarm taxonomy.

    Attributes:
        kind: which rung this is.
        name: instance name (so two supervisors — ``scout`` / ``dispatch`` —
            are distinguishable on the bus + in the kanban).
        max_delegation_depth: how many rungs of children this role may spawn
            beneath itself before the chain must terminate. ``0`` means a
            leaf — it does the work itself and spawns nothing. Caps runaway
            recursion (deep-ai ``MAX_DELEGATION_DEPTH`` pattern).
        perpetual: ``True`` for the never-sleep loops (supervisors, meta);
            ``False`` for one-shot workers (agents, ephemerals). A perpetual
            role generates its own work when the kanban drains; a one-shot
            role returns a result and exits.
        opens_pr: ``True`` only for roles that land code (agent, lead via its
            merged children). Ephemerals + supervisors never open PRs; they
            return verdicts / file kanban tasks.
        description: human-readable one-liner.
    """

    kind: RoleKind
    name: str
    max_delegation_depth: int
    perpetual: bool
    opens_pr: bool
    description: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def can_spawn(self, child: RoleKind) -> bool:
        """Return ``True`` iff this role may directly spawn ``child``.

        Enforces the ladder: a rung may only spawn rungs strictly below it
        (per ``_SPAWNABLE``). Used by the supervisor/lead decomposition path
        to reject an illegal delegation (e.g. supervisor → supervisor) before
        it ever reaches the conductor.
        """
        return child in _SPAWNABLE[self.kind]


# --- factory functions (one per rung) -----------------------------------
#
# TitleCase factories match the ``heads`` roster convention (ruff N802 is
# already ignored project-wide for exactly this). The defaults encode the
# §2 design; callers override ``name`` to instantiate distinct loops.


def MetaSupervisor(*, name: str = "meta") -> Role:
    return Role(
        kind=RoleKind.META_SUPERVISOR,
        name=name,
        max_delegation_depth=4,
        perpetual=True,
        opens_pr=False,
        description="Watches the watchers: health, election, rate-mode, restart-on-silence.",
    )


def Supervisor(*, name: str = "dispatch") -> Role:
    return Role(
        kind=RoleKind.SUPERVISOR,
        name=name,
        max_delegation_depth=3,
        perpetual=True,
        opens_pr=False,
        description="Perpetual loop; claims work; delegates small TASKS to agents OR leads.",
    )


def Lead(*, name: str = "lead") -> Role:
    return Role(
        kind=RoleKind.LEAD,
        name=name,
        max_delegation_depth=2,
        perpetual=False,
        opens_pr=True,
        description="Decomposes a large task into a sub-DAG; reviews + merges children.",
    )


def Agent(*, name: str = "agent") -> Role:
    return Role(
        kind=RoleKind.AGENT,
        name=name,
        max_delegation_depth=1,
        perpetual=False,
        opens_pr=True,
        description="Executes ONE task end-to-end as a tool-use loop; commits; opens a PR.",
    )


def EphemeralAgent(*, name: str = "ephemeral") -> Role:
    return Role(
        kind=RoleKind.EPHEMERAL_AGENT,
        name=name,
        max_delegation_depth=0,
        perpetual=False,
        opens_pr=False,
        description="One bounded sub-step (a single review/test); returns a structured verdict.",
    )


def DynamicWorkflow(*, name: str = "workflow") -> Role:
    return Role(
        kind=RoleKind.DYNAMIC_WORKFLOW,
        name=name,
        max_delegation_depth=2,
        perpetual=False,
        opens_pr=True,
        description="A fan-out of independent agents that converge (simplify+review cycle).",
    )


def default_ladder() -> dict[str, Role]:
    """Return the canonical six-rung taxonomy keyed by role name.

    Names are unique across the default ladder, so the dict is lossless.
    """
    roles = [
        MetaSupervisor(),
        Supervisor(),
        Lead(),
        Agent(),
        EphemeralAgent(),
        DynamicWorkflow(),
    ]
    return {r.name: r for r in roles}


__all__ = [
    "Agent",
    "DynamicWorkflow",
    "EphemeralAgent",
    "Lead",
    "MetaSupervisor",
    "Role",
    "RoleKind",
    "Supervisor",
    "default_ladder",
]
