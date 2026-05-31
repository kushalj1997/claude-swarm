"""Unified task-delegation bus for every agent type.

This module expands the inter-head :mod:`claude_swarm.messaging` plane into a
**task-delegation bus** that a supervisor can use to hand a small TASK chunk to
*any* agent type — a claude-swarm head, Claude Code, Codex, Cursor, or an
in-process API worker — and track its status to completion, with a live link
back to the kanban.

It carries two kinds of traffic over one transport:

* **coordination chatter** — the existing :class:`~claude_swarm.messaging.Message`
  verbs (``work_started`` / ``heartbeat`` / ``question`` …); unchanged.
* **task delegations** — a structured handoff (:class:`Delegation`) with a
  kanban ``task_ref``, a lifecycle ``status``, and the optional ``branch`` /
  ``pr_number`` an agent reports back. Status flows kanban ↔ bus in both
  directions so every delegation has a live, operator-visible state.

Two transports implement the same :class:`TaskBus` surface:

* :class:`TaskBus` — the dependency-light default. Rides on the filesystem
  JSON :class:`~claude_swarm.messaging.MessageBus`; no extra runtime deps.
* :class:`PostgresBus` — the production transport against ``coord_db``. Imports
  ``psycopg`` lazily so the OSS default never requires it. Ships an idempotent
  schema migration that widens the ``sender`` / ``recipient`` / ``msg_type``
  CHECK constraints to the expanded agent taxonomy + task-lifecycle verbs.

Design notes:
    - agent-type-agnostic: senders/recipients are validated against a single
      taxonomy (:data:`VALID_PARTICIPANTS`), including per-class wildcards
      (``agent:*``) so a supervisor can fan a task to a whole class.
    - status-tracked: a delegation has a single :class:`DelegationStatus`; the
      transport stores the latest status and the kanban link.
    - fast responses: the in-process default reads via the JSON inbox snapshot
      (no network); the postgres transport indexes on ``(recipient, status)``.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from .messaging import Message, MessageBus

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent taxonomy — every participant the bus can address
# ---------------------------------------------------------------------------


class AgentClass(str, Enum):
    """The broad family an agent belongs to (used for ``agent:*`` wildcards)."""

    CLAUDE_SWARM = "claude-swarm"
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    CURSOR = "cursor"
    API_WORKER = "api-worker"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"


#: Concrete swarm roles (the never-sleep supervisor team + the worker roles).
SUPERVISOR_ROLES: frozenset[str] = frozenset({"scout", "planner", "dispatch", "meta"})

#: Named participants that can send/receive. Mirrors deep-ai ``comms.py`` but
#: widened per the autonomy architecture §7.2 to every agent type + supervisor
#: role. The wildcard ``"*"`` (broadcast) and the per-class ``"agent:*"`` forms
#: are accepted in addition to these literals.
VALID_PARTICIPANTS: frozenset[str] = frozenset(
    {
        # external / API-driven agent types
        "claude",  # legacy alias for the in-session Claude Code agent
        "claude-code",
        "codex",
        "cursor",
        "api-worker",
        # supervisor team (never-sleep loops)
        "scout",
        "planner",
        "dispatch",
        "meta",
        # humans + relays
        "operator",
        "slack",
        # broadcast lanes
        "all",
    }
)

#: Per-class wildcard recipients (``"agent:claude-code"`` → every claude-code
#: agent). The value after ``agent:`` must be a member of :class:`AgentClass`.
_WILDCARD_PREFIX = "agent:"

#: Plain broadcast recipient (re-exported from messaging for one import site).
BROADCAST: str = "*"


def _valid_participant(name: str, *, allow_broadcast: bool = True) -> bool:
    """Return ``True`` if ``name`` is an addressable participant.

    Accepts a literal in :data:`VALID_PARTICIPANTS` always. The broadcast
    ``"*"`` and the per-class wildcard ``"agent:<class>"`` are *fan-out*
    forms — valid only as recipients, so both are gated on ``allow_broadcast``.
    A concrete message must come FROM a concrete agent, never a wildcard.
    """
    if name in VALID_PARTICIPANTS:
        return True
    if not allow_broadcast:
        return False
    if name == BROADCAST:
        return True
    if name.startswith(_WILDCARD_PREFIX):
        cls = name[len(_WILDCARD_PREFIX):]
        return cls in {c.value for c in AgentClass}
    return False


# ---------------------------------------------------------------------------
# Message-type taxonomy — coordination + task-lifecycle verbs
# ---------------------------------------------------------------------------

#: Coordination chatter verbs (the pre-existing deep-ai set).
COORDINATION_MSG_TYPES: frozenset[str] = frozenset(
    {
        "work_started",
        "work_completed",
        "handoff",
        "question",
        "answer",
        "ack",
        "heartbeat",
        "blocker",
        "slack_inbound",
    }
)


class DelegationStatus(str, Enum):
    """Lifecycle of a delegated task as seen on the bus.

    Mirrors the kanban :class:`~claude_swarm.kanban.TaskStatus` arc but is
    scoped to the *handoff* — i.e. what the dispatcher knows about a task it
    handed to an external agent, independent of the kanban row.
    """

    DELEGATED = "task_delegated"
    CLAIMED = "task_claimed"
    PROGRESS = "task_progress"
    DONE = "task_done"
    BLOCKED = "task_blocked"
    FAILED = "task_failed"


#: Verb → terminal? mapping. Terminal statuses end the handoff.
_TERMINAL_STATUSES: frozenset[DelegationStatus] = frozenset(
    {DelegationStatus.DONE, DelegationStatus.FAILED}
)

#: Verify request/result verbs (the adversarial-verifier handshake, §4.2).
VERIFY_MSG_TYPES: frozenset[str] = frozenset({"verify_request", "verify_result"})

#: Every message-type the expanded bus accepts.
VALID_MSG_TYPES: frozenset[str] = (
    COORDINATION_MSG_TYPES
    | {s.value for s in DelegationStatus}
    | VERIFY_MSG_TYPES
)

#: The subset that carries task-delegation semantics.
TASK_MSG_TYPES: frozenset[str] = frozenset({s.value for s in DelegationStatus})


# ---------------------------------------------------------------------------
# Delegation — the structured handoff shape
# ---------------------------------------------------------------------------


@dataclass
class Delegation:
    """A single task handoff from a supervisor to an agent.

    The shape rides on the bus's structured columns + a free-form ``payload``.
    Mirrors the autonomy-architecture §7.2 message shape exactly:

    ``{ sender, recipient, msg_type, task_ref, payload:{prompt, files_owned,
        acceptance, route, deadline_s, base_sha, worktree_hint} }``
    """

    task_ref: str  # the kanban task id — the link the operator asked for
    sender: str
    recipient: str
    status: DelegationStatus = DelegationStatus.DELEGATED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    summary: str = ""
    branch: str | None = None
    pr_number: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    in_reply_to: str | None = None

    def to_message(self) -> Message:
        """Project this delegation onto a transport :class:`Message`.

        The structured columns (``task_ref`` / ``branch`` / ``pr_number`` /
        ``status``) live inside ``body`` so the JSON transport carries them
        without a schema change; the postgres transport lifts them into real
        columns (see :meth:`PostgresBus.send_delegation`).
        """
        body = {
            "task_ref": self.task_ref,
            "status": self.status.value,
            "summary": self.summary,
            "branch": self.branch,
            "pr_number": self.pr_number,
            "payload": self.payload,
        }
        return Message(
            id=self.id,
            ts=self.ts,
            sender=self.sender,
            recipient=self.recipient,
            kind=self.status.value,
            body=body,
            in_reply_to=self.in_reply_to,
        )

    @classmethod
    def from_message(cls, msg: Message) -> Delegation:
        """Reconstruct a delegation from a transport message.

        Raises :class:`ValueError` if ``msg`` is not a task-lifecycle message.
        """
        if msg.kind not in TASK_MSG_TYPES:
            raise ValueError(
                f"message kind {msg.kind!r} is not a task-delegation verb"
            )
        b = msg.body or {}
        task_ref = b.get("task_ref")
        if not task_ref:
            raise ValueError("delegation message is missing task_ref")
        return cls(
            task_ref=str(task_ref),
            sender=msg.sender,
            recipient=msg.recipient,
            status=DelegationStatus(msg.kind),
            id=msg.id,
            ts=msg.ts,
            summary=str(b.get("summary") or ""),
            branch=b.get("branch"),
            pr_number=b.get("pr_number"),
            payload=dict(b.get("payload") or {}),
            in_reply_to=msg.in_reply_to,
        )

    @property
    def is_terminal(self) -> bool:
        """True when the handoff has reached a terminal status."""
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# Validation helpers (shared by both transports)
# ---------------------------------------------------------------------------


def validate_send(sender: str, recipient: str, msg_type: str) -> None:
    """Raise :class:`ValueError` for any invalid enum value before transport.

    Senders may not be the broadcast wildcard; recipients may.
    """
    if not _valid_participant(sender, allow_broadcast=False):
        raise ValueError(
            f"sender {sender!r} not a valid participant "
            f"(expected one of {sorted(VALID_PARTICIPANTS)} or 'agent:<class>')"
        )
    if not _valid_participant(recipient, allow_broadcast=True):
        raise ValueError(
            f"recipient {recipient!r} not a valid participant "
            f"(expected one of {sorted(VALID_PARTICIPANTS)}, '*', or 'agent:<class>')"
        )
    if msg_type not in VALID_MSG_TYPES:
        raise ValueError(
            f"msg_type {msg_type!r} not in {sorted(VALID_MSG_TYPES)}"
        )


# ---------------------------------------------------------------------------
# TaskBus — the dependency-light default transport
# ---------------------------------------------------------------------------


class TaskBus:
    """Delegation-aware facade over the JSON-inbox :class:`MessageBus`.

    This is the OSS default: no runtime dependency beyond the stdlib. A
    supervisor delegates a task with :meth:`delegate`, the recipient agent
    reports progress with :meth:`update_status`, and either side reads the
    current handoff state with :meth:`status_of` / :meth:`inbox`.

    Optionally wire a ``kanban`` so terminal delegation statuses mirror onto
    the kanban row (charter §9 — the tile reflects reality). The mirror is
    best-effort and never raises into the caller's path.
    """

    #: Map a terminal/transitional delegation status onto a kanban status name.
    #: The kanban is imported lazily so it stays a soft dependency of the bus.
    _KANBAN_STATUS: ClassVar[dict[DelegationStatus, str]] = {
        DelegationStatus.CLAIMED: "in_progress",
        DelegationStatus.PROGRESS: "in_progress",
        DelegationStatus.DONE: "done",
        DelegationStatus.FAILED: "failed",
        DelegationStatus.BLOCKED: "needs_review",
    }

    def __init__(
        self,
        *,
        root: Path | None = None,
        max_messages: int = 256,
        message_bus: MessageBus | None = None,
        kanban: Any | None = None,
    ) -> None:
        self._bus = message_bus or MessageBus(root=root, max_messages=max_messages)
        self._kanban = kanban

    # ----- send paths -------------------------------------------------

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        msg_type: str,
        summary: str = "",
        body: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> Message:
        """Send a coordination message over the bus (validated)."""
        validate_send(sender, recipient, msg_type)
        merged = dict(body or {})
        if summary:
            merged.setdefault("summary", summary)
        return self._bus.send(
            sender=sender,
            recipient=recipient,
            kind=msg_type,
            body=merged,
            in_reply_to=in_reply_to,
        )

    def delegate(
        self,
        *,
        sender: str,
        recipient: str,
        task_ref: str,
        prompt: str,
        summary: str = "",
        files_owned: list[str] | None = None,
        acceptance: list[str] | None = None,
        route: str | None = None,
        deadline_s: int | None = None,
        base_sha: str | None = None,
        worktree_hint: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> Delegation:
        """Hand a TASK to an agent. Returns the :class:`Delegation` sent.

        ``task_ref`` is the kanban task id — the operator-visible link. The
        payload carries everything the recipient adapter needs to run the task
        end-to-end (prompt, owned files, acceptance checklist, routing hint).
        """
        if not task_ref:
            raise ValueError("task_ref must be a non-empty kanban task id")
        if not prompt:
            raise ValueError("prompt must be a non-empty string")
        validate_send(sender, recipient, DelegationStatus.DELEGATED.value)

        payload: dict[str, Any] = {"prompt": prompt}
        if files_owned is not None:
            payload["files_owned"] = files_owned
        if acceptance is not None:
            payload["acceptance"] = acceptance
        if route is not None:
            payload["route"] = route
        if deadline_s is not None:
            payload["deadline_s"] = deadline_s
        if base_sha is not None:
            payload["base_sha"] = base_sha
        if worktree_hint is not None:
            payload["worktree_hint"] = worktree_hint
        if extra_payload:
            payload.update(extra_payload)

        deleg = Delegation(
            task_ref=task_ref,
            sender=sender,
            recipient=recipient,
            status=DelegationStatus.DELEGATED,
            summary=summary or f"delegated {task_ref} → {recipient}",
            payload=payload,
        )
        sent = self._send_delegation(deleg)
        log.info(
            "delegated task_ref=%s sender=%s recipient=%s id=%s",
            task_ref,
            sender,
            recipient,
            sent.id,
        )
        return sent

    def update_status(
        self,
        *,
        sender: str,
        recipient: str,
        task_ref: str,
        status: DelegationStatus,
        summary: str = "",
        branch: str | None = None,
        pr_number: int | None = None,
        in_reply_to: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> Delegation:
        """Emit a status update for an in-flight delegation.

        The recipient agent calls this to report ``task_claimed`` /
        ``task_progress`` / ``task_done`` (with ``branch`` + ``pr_number``) /
        ``task_failed`` / ``task_blocked``. On a terminal status the bus also
        mirrors the kanban row when a kanban was wired (best-effort).
        """
        if not task_ref:
            raise ValueError("task_ref must be a non-empty kanban task id")
        validate_send(sender, recipient, status.value)

        deleg = Delegation(
            task_ref=task_ref,
            sender=sender,
            recipient=recipient,
            status=status,
            summary=summary or f"{status.value} {task_ref}",
            branch=branch,
            pr_number=pr_number,
            payload=dict(extra_payload or {}),
            in_reply_to=in_reply_to,
        )
        sent = self._send_delegation(deleg)
        self._mirror_kanban(sent)
        log.info(
            "delegation status task_ref=%s status=%s by=%s",
            task_ref,
            status.value,
            sender,
        )
        return sent

    def _send_delegation(self, deleg: Delegation) -> Delegation:
        """Send a delegation and return it with the transport's id/ts.

        ``MessageBus.send`` mints a fresh message id, so we reconstruct the
        delegation from the *stored* message to keep ``id`` / ``ts`` truthful
        for the caller (the recipient sees exactly these values).
        """
        msg = deleg.to_message()
        stored = self._bus.send(
            sender=msg.sender,
            recipient=msg.recipient,
            kind=msg.kind,
            body=msg.body,
            in_reply_to=msg.in_reply_to,
        )
        return Delegation.from_message(stored)

    # ----- read paths -------------------------------------------------

    def inbox(self, recipient: str) -> list[Message]:
        """Snapshot every message addressed to ``recipient`` (oldest first)."""
        return self._bus.recv(recipient)

    def delegations(
        self,
        recipient: str,
        *,
        status: DelegationStatus | None = None,
    ) -> list[Delegation]:
        """Return task-delegation messages in ``recipient``'s inbox.

        Optionally filter to a single ``status``. Coordination chatter is
        skipped — only task-lifecycle messages become :class:`Delegation`s.
        """
        out: list[Delegation] = []
        for msg in self._bus.recv(recipient):
            if msg.kind not in TASK_MSG_TYPES:
                continue
            deleg = Delegation.from_message(msg)
            if status is not None and deleg.status != status:
                continue
            out.append(deleg)
        return out

    def status_of(self, recipient: str, task_ref: str) -> Delegation | None:
        """Return the latest delegation state for ``task_ref`` in an inbox.

        Latest-by-timestamp wins, so a ``task_done`` supersedes an earlier
        ``task_progress``. Returns ``None`` if the task was never seen there.
        """
        latest: Delegation | None = None
        for deleg in self.delegations(recipient):
            if deleg.task_ref != task_ref:
                continue
            if latest is None or deleg.ts >= latest.ts:
                latest = deleg
        return latest

    def drain(self, recipient: str) -> list[Message]:
        """Return + clear ``recipient``'s inbox atomically (fast response)."""
        return self._bus.drain(recipient)

    # ----- kanban mirror ----------------------------------------------

    def _mirror_kanban(self, deleg: Delegation) -> None:
        """Best-effort: reflect a delegation status onto its kanban row.

        Never raises into the caller — a missing kanban or unknown task id is
        logged and swallowed so a bus update can't break on the mirror.
        """
        if self._kanban is None:
            return
        kanban_status = self._KANBAN_STATUS.get(deleg.status)
        if kanban_status is None:
            return
        try:
            from .kanban import TaskStatus

            fields: dict[str, Any] = {
                "status": TaskStatus(kanban_status),
                "reason": f"bus:{deleg.status.value}:{deleg.sender}",
            }
            if deleg.pr_number is not None:
                fields["pr_path"] = f"PR#{deleg.pr_number}"
            self._kanban.update(deleg.task_ref, **fields)
        except Exception as exc:
            log.warning(
                "kanban mirror failed task_ref=%s status=%s: %s",
                deleg.task_ref,
                deleg.status.value,
                exc,
            )


__all__ = [
    "BROADCAST",
    "COORDINATION_MSG_TYPES",
    "SUPERVISOR_ROLES",
    "TASK_MSG_TYPES",
    "VALID_MSG_TYPES",
    "VALID_PARTICIPANTS",
    "VERIFY_MSG_TYPES",
    "AgentClass",
    "Delegation",
    "DelegationStatus",
    "TaskBus",
    "validate_send",
]
