"""The never-sleep supervisor loop runtime.

The base :class:`~claude_swarm.supervisor.Supervisor` *drains* a kanban and
returns when it is empty (unless ``wait_for_work=True``, in which case it
only ever *polls*). An autonomous org needs a loop that, on an empty board,
**generates its own work** instead of returning — scan → file kanban →
delegate to agents → fast-verify → loop, forever, rate-limit-resilient and
prompt-cache-aware.

This module adds exactly that, as a thin layer over the existing supervisor
so nothing in the proven dispatch path changes:

* :class:`WorkSource` — the pluggable seam that turns an idle tick into new
  kanban tasks (a scout's ``/deep-research`` scan, a planner's reshaping, or
  an operator-supplied generator). Returns the ids it filed; an empty return
  is fine — the loop still continues.
* :func:`build_cached_blocks` — assembles Anthropic ``cache_control`` system
  blocks so a perpetual loop's stable context (repo corpus, schema, role
  prompt) is cached once and read near-free on every subsequent tick. This is
  the single biggest cost lever for a loop that scans repeatedly.
* :class:`PerpetualSupervisor` — wraps a :class:`Supervisor`; each tick drains
  ready work, and when the board is idle invokes the :class:`WorkSource` to
  refill it. Every LLM-bearing step runs under :func:`resilient_call` so a
  ``429`` backs off + rotates rather than killing the loop. It checkpoints a
  JSON status file every tick and honours the abort marker between ticks so a
  restart resumes exactly where it stopped (no lost work).
* :func:`run_perpetual_team` — start *N* perpetual supervisors over one shared
  kanban, guarded by an OS-level pidfile so a duplicate team can never both
  write the board (the single-supervisor invariant — the $155 incident).

Stdlib + click only; provider calls are injected, never imported here.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .abort import AbortMarker, AbortRequested
from .kanban import Kanban, Task, TaskStatus
from .resilience import (
    BackoffPolicy,
    KeyRotator,
    ResilientCallStats,
    classify_error,
    resilient_call,
)
from .supervisor import Supervisor

log = logging.getLogger(__name__)


class WorkSource(Protocol):
    """Generates new kanban work when the board goes idle.

    Implementations scan the product/codebase (scout), reshape un-routed
    findings into a DAG (planner), or wrap any operator generator. The
    contract is deliberately tiny: given the kanban, file zero or more tasks
    and return their ids. Returning ``[]`` is valid — the loop continues and
    tries again next tick. Raising is allowed; the loop logs and keeps going
    (a flaky scan must not crash the org).
    """

    def generate(self, kanban: Kanban) -> Sequence[str]:
        """File new tasks on ``kanban``; return the ids filed."""
        ...


@dataclass
class NullWorkSource:
    """A work source that files nothing — the safe default.

    With this source a :class:`PerpetualSupervisor` behaves like the base
    supervisor in daemon mode: it drains what is there and idles, ready for
    an external producer to fill the board. Swap in a real scout/planner to
    make the loop self-populating.
    """

    def generate(self, kanban: Kanban) -> Sequence[str]:
        del kanban  # NullWorkSource files nothing by design
        return ()


@dataclass
class CallableWorkSource:
    """Adapt a plain callable into a :class:`WorkSource`.

    The callable receives the kanban and returns the filed task ids. This is
    the cheapest way to wire a closure (or a bound scout method) into the
    perpetual loop without writing a class.
    """

    fn: Callable[[Kanban], Sequence[str]]

    def generate(self, kanban: Kanban) -> Sequence[str]:
        return self.fn(kanban)


def _drive_claimed_task(*, supervisor: Supervisor, **_extra: Any) -> Task | None:
    """Atomically claim one ready task and dispatch it — never abandoning it.

    This is the concurrency-safe alternative to :meth:`Supervisor.step`. It
    claims first (``claim_one`` is atomic), then dispatches *whatever* was
    claimed and writes the outcome back. ``step`` instead peeks then claims
    then bails when a sibling won the peeked task — orphaning the claimed one
    in ``in_progress``. Returns the claimed task (with its final status) or
    ``None`` when the board has no ready work.

    ``_extra`` absorbs any ``api_key`` the rotator injects (the credential is
    consumed by the real LLM call inside the conductor, not by the driver).
    """
    kb = supervisor.kanban
    teammate = supervisor.config.teammate_name
    # Peek only to learn which head/required_head to claim for; the claim is
    # what actually moves a task to in_progress, atomically.
    ready = kb.unblocked(limit=1)
    if not ready:
        return None
    required_head = ready[0].required_head
    claimed = kb.claim_one(
        worker_id=f"{teammate}:{required_head}",
        required_head=required_head,
    )
    if claimed is None:
        return None
    head = supervisor._pick_head(claimed)
    if head is None:
        log.warning("no head matches required=%r for task %s", claimed.required_head, claimed.id)
        kb.update(
            claimed.id,
            status=TaskStatus.FAILED,
            error="no matching head",
            completed_at=time.time(),
        )
        return claimed
    try:
        outcome = supervisor.conductor.dispatch(head=head, task=claimed)
    except AbortRequested:
        kb.transition(claimed.id, TaskStatus.PENDING, reason="aborted")
        raise
    except Exception as exc:
        # A transient (429/overload) returns the task to PENDING and bubbles to
        # resilient_call for backoff + rotation, so the retry re-claims a ready
        # task (this one or a newly-higher-priority one) and re-drives it. A
        # task left in_progress would be invisible to the next claim and thus
        # orphaned, so we must release it. A genuine bug instead marks the task
        # FAILED so the loop is not torn down (mirrors Supervisor.step).
        if classify_error(exc) is not None:
            kb.transition(claimed.id, TaskStatus.PENDING, reason="transient; will retry")
            raise
        log.exception("conductor crashed for task %s", claimed.id)
        kb.update(
            claimed.id,
            status=TaskStatus.FAILED,
            error=repr(exc),
            completed_at=time.time(),
        )
        return claimed
    supervisor._cost_so_far_usd += outcome.cost_usd
    supervisor._turn += 1
    kb.update(
        claimed.id,
        status=outcome.status,
        cost_usd=outcome.cost_usd,
        result=outcome.result,
        error=outcome.error,
        pr_path=outcome.pr_path,
        completed_at=time.time(),
    )
    return claimed


def build_cached_blocks(
    blocks: Sequence[tuple[str, str]],
    *,
    ttl: str = "5m",
) -> list[dict[str, Any]]:
    """Build Anthropic ``cache_control`` system blocks from labelled text.

    ``blocks`` is an ordered sequence of ``(label, text)`` pairs — the stable
    context a perpetual loop reuses every tick (repo corpus, schema, role
    prompt, skill bundle). We attach ``cache_control: {type: ephemeral, ttl}``
    to the **last non-empty block** so everything up to and including it is
    cached as one prefix: the first call pays a cache *write*, every
    subsequent tick pays only cache *reads* (a fraction of input price).

    The ``label`` is dropped from the wire payload (Anthropic blocks carry no
    label field) but documents intent at the call site. Empty-text blocks are
    skipped. Returns ``[]`` for empty input so the caller can splat it
    unconditionally into ``system=[...]``.
    """
    out: list[dict[str, Any]] = []
    for _label, text in blocks:
        if not text:
            continue
        out.append({"type": "text", "text": text})
    if out:
        out[-1]["cache_control"] = {"type": "ephemeral", "ttl": ttl}
    return out


@dataclass
class PerpetualConfig:
    """Tunables for :class:`PerpetualSupervisor`.

    ``idle_heartbeat_s`` is the sleep between ticks when the board is idle and
    the work source filed nothing — deliberately **not** 300s (charter §10:
    that exact value straddles the prompt-cache TTL). ``max_ticks`` bounds the
    loop for tests; ``None`` means truly perpetual. ``status_path`` receives a
    JSON checkpoint every tick for the operator-visible surface.
    """

    name: str = "perpetual"
    idle_heartbeat_s: float = 270.0
    busy_poll_s: float = 0.5
    max_ticks: int | None = None
    status_path: Path | None = None
    abort_root: Path | None = None
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    rotator: KeyRotator | None = None
    max_attempts: int = 6


@dataclass
class PerpetualStats:
    """Live counters for the loop — surfaced in the status checkpoint."""

    ticks: int = 0
    dispatched: int = 0
    generated: int = 0
    idle_ticks: int = 0
    transient_recoveries: int = 0
    started_at: float = field(default_factory=time.time)
    last_tick_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "dispatched": self.dispatched,
            "generated": self.generated,
            "idle_ticks": self.idle_ticks,
            "transient_recoveries": self.transient_recoveries,
            "uptime_s": round(time.time() - self.started_at, 1),
            "last_tick_at": self.last_tick_at,
        }


class PerpetualSupervisor:
    """A supervisor that never returns on an empty board — it makes work.

    One tick is:

      1. Abort check (between-phase, work-preserving — :class:`AbortRequested`
         exits the loop cleanly; the in-flight task was already checkpointed
         on the kanban by the base supervisor's ``step``).
      2. ``supervisor.step()`` — dispatch one ready task if any. Wrapped in
         :func:`resilient_call` so a throttled dispatch backs off + rotates.
      3. If nothing dispatched and the board is fully idle, ask the
         :class:`WorkSource` to file new tasks (the never-sleep behaviour).
      4. Optional fast-verify hook on the just-dispatched task.
      5. Write a JSON status checkpoint.
      6. Sleep: a short busy-poll when work flowed, the cache-safe idle
         heartbeat when the board is empty.

    The loop is single-writer by contract; run it under the pidfile guard in
    :func:`run_perpetual_team` (or your own OS singleton).
    """

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        work_source: WorkSource | None = None,
        config: PerpetualConfig | None = None,
        verify: Callable[[str], None] | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.work_source = work_source or NullWorkSource()
        self.config = config or PerpetualConfig()
        self.verify = verify
        self.stats = PerpetualStats()
        self._stop = threading.Event()
        self._abort: AbortMarker | None = None
        if self.config.abort_root is not None:
            self._abort = AbortMarker(
                worktree_root=self.config.abort_root,
                teammate=self.config.name,
            )

    # ----- lifecycle --------------------------------------------------

    def stop(self) -> None:
        """Request a clean stop after the current tick (thread-safe)."""
        self._stop.set()

    def _should_abort(self) -> bool:
        return self._stop.is_set() or (self._abort is not None and self._abort.is_set())

    def _board_idle(self) -> bool:
        kb = self.supervisor.kanban
        pending = kb.list_tasks(status=TaskStatus.PENDING)
        in_prog = kb.list_tasks(status=TaskStatus.IN_PROGRESS)
        return not pending and not in_prog

    # ----- one tick ---------------------------------------------------

    def tick(self) -> bool:
        """Run one loop iteration. Returns ``True`` if a task was dispatched.

        Used directly by tests; :meth:`run` calls it in a loop. Generation
        and verification failures are swallowed-with-log so a flaky scan or
        verifier never tears the loop down (they file follow-up work instead
        of crashing the org).
        """
        self.stats.ticks += 1
        self.stats.last_tick_at = time.time()

        dispatched = self._resilient_drive()
        if dispatched is not None:
            self.stats.dispatched += 1
            if self.verify is not None:
                self._safe_verify(dispatched.id)
            return True

        # Board produced no ready work this tick. If it is fully idle,
        # generate more — this is the never-sleep behaviour.
        if self._board_idle():
            self.stats.idle_ticks += 1
            filed = self._safe_generate()
            self.stats.generated += len(filed)
        return False

    def _resilient_drive(self) -> Task | None:
        """Claim + dispatch one task under the rate-limit resilience wrapper.

        We pass our own :class:`ResilientCallStats` in so we can tell whether
        the dispatch had to back off / rotate this tick, and roll that into
        :attr:`PerpetualStats.transient_recoveries` for the status surface
        (charter §16: surface the actual recovery count, not a label).

        We deliberately do **not** call ``supervisor.step()`` here. ``step``
        peeks the next unblocked task, then claims, then *abandons* the claim
        if a sibling grabbed a different task first (``claimed.id != task.id``)
        — which orphans that claimed task in ``in_progress`` forever under
        concurrency. The perpetual team runs N loops over one kanban, so we
        use a claim-first driver (:func:`_drive_claimed_task`) that dispatches
        whatever ``claim_one`` atomically returns and never leaks a claim.
        """
        call_stats = ResilientCallStats()
        result = resilient_call(
            _drive_claimed_task,
            supervisor=self.supervisor,
            backoff=self.config.backoff,
            rotator=self.config.rotator,
            max_attempts=self.config.max_attempts,
            should_abort=self._should_abort,
            stats=call_stats,
        )
        if call_stats.attempts > 1:
            self.stats.transient_recoveries += 1
        return result

    def _safe_generate(self) -> Sequence[str]:
        try:
            filed = self.work_source.generate(self.supervisor.kanban)
        except Exception:
            # A flaky scan must not crash the loop — log + continue.
            log.exception("work source raised during generate(); continuing")
            return ()
        if filed:
            log.info("work source filed %d task(s)", len(filed))
        return list(filed)

    def _safe_verify(self, task_id: str) -> None:
        if self.verify is None:
            return
        try:
            self.verify(task_id)
        except Exception:
            # A verify failure must not crash the loop — log + continue.
            log.exception("verify hook raised for task %s; continuing", task_id)

    # ----- the perpetual loop ----------------------------------------

    def run(self) -> PerpetualStats:
        """Loop until stopped or aborted. Returns the final stats snapshot.

        Honours :attr:`PerpetualConfig.max_ticks` (tests) and the abort
        marker + :meth:`stop` flag (operator / meta-supervisor). It only
        *returns* on stop/abort/max-ticks — never on an empty board.
        """
        while True:
            if self.config.max_ticks is not None and self.stats.ticks >= self.config.max_ticks:
                break
            if self._should_abort():
                log.info("perpetual loop %s aborted via marker/stop; exiting", self.config.name)
                break
            try:
                worked = self.tick()
            except AbortRequested:
                log.info("perpetual loop %s aborted mid-tick; exiting cleanly", self.config.name)
                break
            self._checkpoint()
            # Cache-aware sleep: short when work is flowing, the safe idle
            # heartbeat when the board is empty.
            nap = self.config.busy_poll_s if worked else self.config.idle_heartbeat_s
            self._interruptible_sleep(nap)
        self._checkpoint()
        return self.stats

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on a stop request.

        ``Event.wait`` returns as soon as :meth:`stop` is called, so a long
        idle heartbeat never blocks a shutdown.
        """
        if seconds <= 0:
            return
        self._stop.wait(timeout=seconds)

    # ----- status surface --------------------------------------------

    def status(self) -> dict[str, Any]:
        base = self.supervisor.status()
        base["perpetual"] = self.stats.to_dict()
        base["name"] = self.config.name
        return base

    def _checkpoint(self) -> None:
        if self.config.status_path is None:
            return
        try:
            self.config.status_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.status_path.write_text(
                json.dumps(self.status(), indent=2), encoding="utf-8"
            )
        except OSError:
            log.exception("failed to write status checkpoint")


# ----- team launcher (OS-level singleton) -----------------------------


class DuplicateTeamError(RuntimeError):
    """Raised when a perpetual team is already running for this home."""


@dataclass
class PidfileGuard:
    """A best-effort OS-level singleton via an exclusive pidfile.

    Acquisition writes the current pid to ``path`` only if no *live* process
    already owns it. A stale pidfile (the owner has exited) is reclaimed.
    This is the single-supervisor invariant at the OS layer — coordinator
    election alone does not survive a hard crash (charter §7/§10).
    """

    path: Path

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            owner = self._read_pid()
            if owner is not None and _pid_alive(owner) and owner != os.getpid():
                raise DuplicateTeamError(
                    f"perpetual team already running (pid {owner}) — {self.path}"
                )
        self.path.write_text(str(os.getpid()), encoding="utf-8")

    def release(self) -> None:
        if not self.path.exists():
            return
        if self._read_pid() == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                log.exception("failed to remove pidfile %s", self.path)

    def _read_pid(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    else:
        return True


def run_perpetual_team(
    *,
    kanban: Kanban,
    count: int,
    supervisor_factory: Callable[[int], PerpetualSupervisor],
    pidfile: Path | None = None,
    join: bool = True,
) -> list[PerpetualSupervisor]:
    """Start ``count`` perpetual supervisors over one shared kanban.

    ``supervisor_factory(i)`` builds the i-th :class:`PerpetualSupervisor`
    (so each can carry a distinct name / role / work source). All share the
    one kanban — the atomic ``claim_one`` ensures two loops never grab the
    same task even though they run concurrently.

    When ``pidfile`` is given, an OS-level :class:`PidfileGuard` rejects a
    second team for the same home (the single-supervisor invariant). With
    ``join=True`` this blocks until every loop stops (or the process is
    signalled), holding the pidfile for the whole run. With ``join=False`` it
    returns the live supervisors immediately for the caller to manage — and
    releases the pidfile then, since no handle is returned to release it later
    (the guard spans the *launch*, not the detached lifetime). A failure while
    building or starting any loop always releases the guard before re-raising.

    Returns the list of supervisors (also useful for introspection while
    ``join=False``).
    """
    if count < 1:
        raise ValueError("count must be >= 1")

    guard = PidfileGuard(pidfile) if pidfile is not None else None
    if guard is not None:
        guard.acquire()

    try:
        supervisors = [supervisor_factory(i) for i in range(count)]
        threads = [
            threading.Thread(target=s.run, name=f"{s.config.name}-{i}", daemon=True)
            for i, s in enumerate(supervisors)
        ]
        for t in threads:
            t.start()
    except BaseException:
        # Building or starting a loop failed — never strand the pidfile.
        if guard is not None:
            guard.release()
        raise

    if not join:
        # The caller manages the detached loops. The pidfile guarded only the
        # launch (we hold no handle to release later), so we release it now —
        # a non-joining caller does not retain the OS-level singleton.
        if guard is not None:
            guard.release()
        return supervisors

    try:
        # Block until all loops finish. Each is daemonic so a process signal
        # still exits; the join keeps the foreground caller alive meanwhile.
        for t in threads:
            while t.is_alive():
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        log.info("interrupt received; stopping perpetual team")
        for s in supervisors:
            s.stop()
        for t in threads:
            t.join(timeout=10.0)
    finally:
        if guard is not None:
            guard.release()
    return supervisors


__all__ = [
    "CallableWorkSource",
    "DuplicateTeamError",
    "NullWorkSource",
    "PerpetualConfig",
    "PerpetualStats",
    "PerpetualSupervisor",
    "PidfileGuard",
    "WorkSource",
    "build_cached_blocks",
    "run_perpetual_team",
]
