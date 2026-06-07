"""MetaSupervisorMonitor -- health check, parallelism scoring, cost preflight.

Implements the operator's meta-supervision directive (memory
feedback_meta_supervision_directive.md): a thin monitor layer that sits above
the supervisor processes and:

1. **Parallelism safety scoring** -- scores every pending task 0.0-1.0 on
   how safely it can run in parallel with current in-progress tasks. Score is
   derived from file-overlap heuristics, role kind, and task metadata.

2. **Supervisor health checks** -- periodically ping each named supervisor's
   status file; detect silence (no heartbeat within a configurable window) and
   emit a health event. The meta-supervisor can restart a silent loop via a
   supplied factory callable (default: log and skip).

3. **Cost preflight** -- before a task is claimed, estimate its expected cost
   from historical per-head averages and compare to the task's cost_cap_usd.
   Return ADMIT / HOLD / REJECT with a reason.

4. **Anomaly accumulation** -- record repeated failure fingerprints; when a
   fingerprint appears 3+ times, escalate to NEEDS_REVIEW (surfaced to
   operator).

All classes are dependency-light (stdlib only). No LLM calls; no I/O beyond
reading the kanban + writing to the status dir. The monitor is meant to be
called on each supervisor tick (or on a bus event) and should complete in <5ms.

Usage::

    monitor = MetaSupervisorMonitor(kanban=kb, home=my_home)

    # Before dispatching a task:
    pf = monitor.cost_preflight(task, head_name="builder")
    if pf.verdict == "REJECT":
        kb.update(task.id, status=TaskStatus.FAILED, error=pf.reason)
    else:
        parallel_score = monitor.parallelism_score(task)
        # Use score to decide max_parallel ceiling

    # On each tick, check supervisor health:
    events = monitor.check_supervisor_health()
    for ev in events:
        if ev.is_silent:
            log.warning("supervisor %s silent for %.0fs", ev.name, ev.silent_for_s)
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._paths import state_dir
from .kanban import Kanban, Task, TaskStatus

log = logging.getLogger(__name__)

# --- parallelism scoring constants -----------------------------------------

#: Isolated single-file edits or pure docs: fully parallelisable.
PARALLELISM_SAFE = 1.0
#: Multi-file but separable (per-surface UI, different modules): usually safe.
PARALLELISM_MOSTLY_SAFE = 0.7
#: Shares files with an in-progress task: risky.
PARALLELISM_RISKY = 0.3
#: Repo-wide refactor or known-serial role: must serialise.
PARALLELISM_UNSAFE = 0.0

#: Roles that are inherently serial (only one should run at a time).
SERIAL_ROLES: frozenset[str] = frozenset({"meta-supervisor", "supervisor"})

#: Roles that never write files (verdicts only) and are always fully parallel.
VERDICT_ONLY_ROLES: frozenset[str] = frozenset({"ephemeral-agent", "reviewer", "auditor"})


# --- cost preflight constants -----------------------------------------------

#: Sentinel: no historical average available, use this fallback (USD).
_DEFAULT_COST_PER_TASK = 0.05


# ---------------------------------------------------------------------------
# Parallelism score
# ---------------------------------------------------------------------------


def parallelism_score(
    task: Task,
    *,
    in_progress: list[Task] | None = None,
) -> float:
    """Return a 0.0-1.0 parallelism safety score for *task*.

    Parameters
    ----------
    task:
        The candidate task (PENDING) being evaluated.
    in_progress:
        Currently-running tasks.  When ``None``, the score is derived from
        task-intrinsic signals only (no cross-task file overlap check).

    Returns
    -------
    float
        - ``1.0`` — fully parallelisable (isolated, no shared state)
        - ``0.7`` — mostly safe (multi-file but probably separable)
        - ``0.3`` — risky (overlaps files with an in-progress task)
        - ``0.0`` — must serialise (repo-wide refactor, serial role)

    The metadata key ``parallelism_safety`` is honoured as a hard override
    when present — it lets the planner stamp the correct score without the
    heuristic ever running (e.g. for known-dangerous cross-cutting tasks).
    """
    # Planner override takes precedence.
    override = task.metadata.get("parallelism_safety")
    if override is not None:
        try:
            v = float(override)
            return max(0.0, min(1.0, v))
        except (TypeError, ValueError):
            pass

    role = task.role or ""
    if role in SERIAL_ROLES:
        return PARALLELISM_UNSAFE

    # Verdict-only roles never write; fully parallel regardless of files.
    if role in VERDICT_ONLY_ROLES or task.required_head in {"reviewer", "auditor", "test-runner"}:
        return PARALLELISM_SAFE

    # File-overlap check (most expensive but most accurate).
    if in_progress:
        task_files = set(task.files_owned) if task.files_owned else set()
        if task_files:
            for other in in_progress:
                other_files = set(other.files_owned) if other.files_owned else set()
                if task_files & other_files:
                    return PARALLELISM_RISKY
        else:
            # Unknown files — be conservative when many tasks are running.
            if len(in_progress) >= 3:
                return PARALLELISM_MOSTLY_SAFE

    n_files = len(task.files_owned)
    if n_files == 0:
        # No files declared; assume single-file (typical atomic agent task).
        return PARALLELISM_SAFE
    if n_files == 1:
        return PARALLELISM_SAFE
    if n_files <= 5:
        return PARALLELISM_MOSTLY_SAFE
    # Large multi-file task: treat as potentially serial.
    return PARALLELISM_RISKY


# ---------------------------------------------------------------------------
# Cost preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightVerdict:
    """Result of a cost preflight check."""

    verdict: str        # "ADMIT" | "HOLD" | "REJECT"
    reason: str
    estimated_cost_usd: float
    cap_usd: float | None


def cost_preflight(
    task: Task,
    *,
    head_name: str | None = None,
    historical_avg_usd: float | None = None,
    daily_budget_remaining_usd: float | None = None,
) -> PreflightVerdict:
    """Estimate task cost and compare to cap + daily budget.

    Parameters
    ----------
    task:
        Task to evaluate.
    head_name:
        Name of the head that would run this task; used for per-head average.
    historical_avg_usd:
        Caller-supplied average cost for this head/task-type.  If ``None``,
        falls back to :data:`_DEFAULT_COST_PER_TASK`.
    daily_budget_remaining_usd:
        How much daily budget is left.  ``None`` means no budget cap is active.

    Returns
    -------
    PreflightVerdict
        - ``ADMIT`` — estimated cost is within bounds; safe to claim.
        - ``HOLD``  — estimated cost is close to cap (>80%); caution.
        - ``REJECT`` — estimated cost exceeds hard cap or daily budget.
    """
    estimate = historical_avg_usd if historical_avg_usd is not None else _DEFAULT_COST_PER_TASK
    cap = task.metadata.get("cost_cap_usd") or getattr(task, "cost_cap_usd", None)
    if cap is None:
        cap = task.metadata.get("cost_cap_usd")

    if cap is not None:
        try:
            cap_f = float(cap)
        except (TypeError, ValueError):
            cap_f = None
    else:
        cap_f = None

    # Hard cap check.
    if cap_f is not None and estimate >= cap_f:
        return PreflightVerdict(
            verdict="REJECT",
            reason=f"estimated cost ${estimate:.4f} >= task cap ${cap_f:.4f}",
            estimated_cost_usd=estimate,
            cap_usd=cap_f,
        )

    # Daily budget check.
    if daily_budget_remaining_usd is not None and estimate > daily_budget_remaining_usd:
        return PreflightVerdict(
            verdict="REJECT",
            reason=f"estimated cost ${estimate:.4f} > daily budget remaining ${daily_budget_remaining_usd:.4f}",
            estimated_cost_usd=estimate,
            cap_usd=cap_f,
        )

    # Warn if estimate > 80% of cap.
    if cap_f is not None and estimate >= 0.8 * cap_f:
        return PreflightVerdict(
            verdict="HOLD",
            reason=f"estimated cost ${estimate:.4f} is >80% of cap ${cap_f:.4f}",
            estimated_cost_usd=estimate,
            cap_usd=cap_f,
        )

    return PreflightVerdict(
        verdict="ADMIT",
        reason="within bounds",
        estimated_cost_usd=estimate,
        cap_usd=cap_f,
    )


# ---------------------------------------------------------------------------
# Supervisor health event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorHealthEvent:
    """One health observation for a named supervisor."""

    name: str
    last_heartbeat: float | None
    now: float
    heartbeat_timeout_s: float
    is_silent: bool

    @property
    def silent_for_s(self) -> float:
        if self.last_heartbeat is None:
            return float("inf")
        return max(0.0, self.now - self.last_heartbeat)


# ---------------------------------------------------------------------------
# Anomaly tracker
# ---------------------------------------------------------------------------


@dataclass
class AnomalyTracker:
    """Accumulate failure fingerprints; escalate after N recurrences.

    The fingerprint is the task's ``error`` field (or a truncated version of
    it). When the same fingerprint appears ``escalation_threshold`` times, a
    dict entry is returned for operator review.
    """

    escalation_threshold: int = 3
    _counts: Counter[str] = field(default_factory=Counter)
    _escalated: set[str] = field(default_factory=set)

    def record(self, task: Task) -> bool:
        """Record a failed task and return True if an escalation just fired."""
        fp = self._fingerprint(task)
        self._counts[fp] += 1
        if fp not in self._escalated and self._counts[fp] >= self.escalation_threshold:
            self._escalated.add(fp)
            log.warning(
                "anomaly escalated: fingerprint %r appeared %d times (threshold=%d)",
                fp,
                self._counts[fp],
                self.escalation_threshold,
            )
            return True
        return False

    def _fingerprint(self, task: Task) -> str:
        error = (task.error or "")[:120]
        return f"{task.required_head}:{task.type}:{error}"

    def top(self, n: int = 5) -> list[tuple[str, int]]:
        """Return the top-n failure fingerprints by count."""
        return self._counts.most_common(n)

    def reset(self, fingerprint: str) -> None:
        """Clear a fingerprint (e.g. after the operator acknowledges it)."""
        self._counts.pop(fingerprint, None)
        self._escalated.discard(fingerprint)


# ---------------------------------------------------------------------------
# MetaSupervisorMonitor
# ---------------------------------------------------------------------------


@dataclass
class MetaSupervisorMonitor:
    """Thin monitor layer: health, parallelism scoring, cost preflight.

    Instantiate once and call methods on each supervisor tick.  All methods
    are synchronous and complete in <5ms (no network I/O).

    Parameters
    ----------
    kanban:
        The shared kanban (read-only from the monitor's perspective).
    home:
        Swarm home directory.  Status files are read from ``<home>/state/``.
    heartbeat_timeout_s:
        A supervisor is declared silent after this many seconds without a
        heartbeat update. Default: 300 (5 minutes).
    escalation_threshold:
        Number of recurrences of the same failure fingerprint before an anomaly
        escalation fires. Default: 3.
    """

    kanban: Kanban
    home: Path | None = None
    heartbeat_timeout_s: float = 300.0
    escalation_threshold: int = 3

    def __post_init__(self) -> None:
        self._state_dir: Path = state_dir(self.home)
        self._anomaly_tracker: AnomalyTracker = AnomalyTracker(
            escalation_threshold=self.escalation_threshold
        )
        # Per-head rolling average cost (updated by record_outcome).
        self._head_cost_sum: dict[str, float] = {}
        self._head_cost_count: dict[str, int] = {}

    # --- parallelism scoring -----------------------------------------------

    def parallelism_score(self, task: Task) -> float:
        """Score how safely *task* can run in parallel with current work.

        Reads the kanban's current in_progress tasks for the file-overlap check.
        """
        try:
            in_progress = self.kanban.list_tasks(status=TaskStatus.IN_PROGRESS)
        except Exception:
            in_progress = []
        return parallelism_score(task, in_progress=in_progress)

    # --- cost preflight -----------------------------------------------------

    def cost_preflight(
        self,
        task: Task,
        *,
        head_name: str | None = None,
        daily_budget_remaining_usd: float | None = None,
    ) -> PreflightVerdict:
        """Preflight *task* against per-head historical averages + budget."""
        avg = self._head_avg_cost(head_name or task.required_head)
        return cost_preflight(
            task,
            head_name=head_name,
            historical_avg_usd=avg,
            daily_budget_remaining_usd=daily_budget_remaining_usd,
        )

    def _head_avg_cost(self, head: str) -> float:
        count = self._head_cost_count.get(head, 0)
        if count == 0:
            return _DEFAULT_COST_PER_TASK
        return self._head_cost_sum[head] / count

    def record_outcome(self, task: Task, head_name: str) -> bool:
        """Update per-head cost averages; return True if anomaly escalated."""
        cost = task.cost_usd or 0.0
        self._head_cost_sum[head_name] = self._head_cost_sum.get(head_name, 0.0) + cost
        self._head_cost_count[head_name] = self._head_cost_count.get(head_name, 0) + 1
        if task.status == TaskStatus.FAILED:
            return self._anomaly_tracker.record(task)
        return False

    # --- supervisor health check -------------------------------------------

    def check_supervisor_health(
        self,
        supervisor_names: list[str] | None = None,
        *,
        now: float | None = None,
    ) -> list[SupervisorHealthEvent]:
        """Check heartbeat files for each named supervisor.

        Parameters
        ----------
        supervisor_names:
            Names to check.  Defaults to all ``*.status.json`` files under the
            state directory.
        now:
            Current timestamp.  Defaults to ``time.time()``.

        Returns
        -------
        list[SupervisorHealthEvent]
            One event per checked supervisor.  ``is_silent=True`` means the
            supervisor has not written a heartbeat within ``heartbeat_timeout_s``.
        """
        now = time.time() if now is None else now
        if supervisor_names is None:
            supervisor_names = self._discover_supervisor_names()

        events: list[SupervisorHealthEvent] = []
        for name in supervisor_names:
            last = self._read_heartbeat(name)
            is_silent = (last is None) or ((now - last) >= self.heartbeat_timeout_s)
            ev = SupervisorHealthEvent(
                name=name,
                last_heartbeat=last,
                now=now,
                heartbeat_timeout_s=self.heartbeat_timeout_s,
                is_silent=is_silent,
            )
            if is_silent:
                log.warning(
                    "meta-supervisor: %s silent for %.0fs (threshold=%.0fs)",
                    name,
                    ev.silent_for_s,
                    self.heartbeat_timeout_s,
                )
            events.append(ev)
        return events

    def _discover_supervisor_names(self) -> list[str]:
        """Glob for status JSON files in the state directory."""
        try:
            return [
                p.stem.removesuffix(".status")
                for p in self._state_dir.glob("*.status.json")
            ]
        except OSError:
            return []

    def _read_heartbeat(self, name: str) -> float | None:
        path = self._state_dir / f"{name}.status.json"
        if not path.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("last_tick") or data.get("heartbeat") or data.get("updated_at")
            return float(raw) if raw is not None else None
        except (OSError, ValueError, KeyError):
            return None

    # --- anomaly access ----------------------------------------------------

    @property
    def anomaly_tracker(self) -> AnomalyTracker:
        return self._anomaly_tracker

    def top_failures(self, n: int = 5) -> list[tuple[str, int]]:
        """Return the top-n failure fingerprints by count."""
        return self._anomaly_tracker.top(n)


__all__ = [
    "AnomalyTracker",
    "MetaSupervisorMonitor",
    "PARALLELISM_SAFE",
    "PARALLELISM_MOSTLY_SAFE",
    "PARALLELISM_RISKY",
    "PARALLELISM_UNSAFE",
    "PreflightVerdict",
    "SupervisorHealthEvent",
    "cost_preflight",
    "parallelism_score",
]
