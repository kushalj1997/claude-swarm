"""Persistent-agent state — the on-disk substrate for the future native
``Agent(..., persistent=True)`` flag.

This module is intentionally small. It owns one thing: the read/write
contract for ``~/.claude/teams/<team>/agents/<name>.json`` — the
filesystem registry that:

1. The keepalive daemon writes to when it dispatches a worker on behalf
   of a registered agent. The worker's PID, current task id, last
   activity timestamp, and conversation pointer all live here.
2. A future native ``Agent(..., persistent=True)`` implementation in the
   claude-code binary would also write to. When ``claude --resume``
   starts, the binary's startup path would scan
   ``~/.claude/teams/*/agents/*.json``, find any whose ``pid`` is still
   alive (``os.kill(pid, 0)``), and re-bind their stdout/stdin streams
   to surface them in the team-list UI.
3. The plugin's ``/swarm-status`` command reads to surface
   registered agents alongside the daemon's task list.

By shipping the schema + read/write helpers + tests now, the binary-side
refactor can land later as a small, contained change that targets a
known surface. No schema migration required when the binary catches up.
"""
from __future__ import annotations

import json
import os
import time
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
"""Bumped on any breaking change to :class:`AgentState`.

The plugin + library both check this on read and refuse to load mismatched
records. Forward compatibility: unknown fields are preserved on round-trip
via ``AgentState.extra``.
"""


@dataclass
class AgentState:
    """On-disk record for one registered agent.

    Attributes:
        schema_version: serialization-format version, bumped on breakage.
        team: team name (matches the ``~/.claude/teams/<team>/`` path).
        name: agent name within the team (must be filesystem-safe).
        pid: process id of the worker subprocess currently owned by this
            agent, or ``None`` when the agent is registered but idle.
        persistent: hint to the future native ``Agent`` tool — when
            ``True``, the binary should treat this agent as detachable
            (single-fork + setsid on first dispatch).
        head: the role-typed head this agent embodies (``builder``,
            ``scanner``, etc.). Determines tool allowlist.
        current_task_id: kanban task this agent is currently dispatched
            against, or ``None`` if idle.
        conversation_path: path to the agent's persisted conversation
            transcript (the future native Agent's analogue of
            ``~/.claude/conversations/``).
        registered_at: epoch seconds when first registered.
        last_seen: epoch seconds of the most recent state-write.
        extra: free-form forward-compat bag preserved on round-trip.
    """

    team: str
    name: str
    head: str = "builder"
    pid: int | None = None
    persistent: bool = True
    current_task_id: str | None = None
    conversation_path: str | None = None
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None for cleaner JSON
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentState:
        version = d.get("schema_version", 1)
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"AgentState schema version mismatch: file has {version}, "
                f"code expects {SCHEMA_VERSION}. Migrate or bump."
            )
        known = {
            "team", "name", "head", "pid", "persistent",
            "current_task_id", "conversation_path",
            "registered_at", "last_seen", "schema_version",
        }
        # `extra` field round-trips its own contents (nested under "extra" in JSON)
        # AND captures any unknown top-level keys (forward-compat for future fields).
        extra = dict(d.get("extra", {}))
        for k, v in d.items():
            if k not in known and k != "extra":
                extra[k] = v
        return cls(
            team=d["team"],
            name=d["name"],
            head=d.get("head", "builder"),
            pid=d.get("pid"),
            persistent=d.get("persistent", True),
            current_task_id=d.get("current_task_id"),
            conversation_path=d.get("conversation_path"),
            registered_at=d.get("registered_at", time.time()),
            last_seen=d.get("last_seen", time.time()),
            schema_version=version,
            extra=extra,
        )

    def is_alive(self) -> bool:
        """True iff a `pid` is set and the process still exists."""
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def agents_dir(team_root: Path) -> Path:
    """Return ``<team_root>/agents/`` and ensure it exists."""
    d = team_root / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, payload: str) -> None:
    """tmp-file + os.replace so concurrent readers never see torn writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register(team_root: Path, state: AgentState) -> Path:
    """Write (or overwrite) ``<team_root>/agents/<name>.json``.

    Returns the path written. Atomic: concurrent readers always see
    either the prior version or the new one, never a partial.
    """
    state.last_seen = time.time()
    path = agents_dir(team_root) / f"{state.name}.json"
    _atomic_write(path, json.dumps(state.to_dict(), indent=2) + "\n")
    return path


def get(team_root: Path, name: str) -> AgentState | None:
    """Read one agent state, or None if no such record."""
    path = agents_dir(team_root) / f"{name}.json"
    if not path.exists():
        return None
    return AgentState.from_dict(json.loads(path.read_text()))


def list_all(team_root: Path) -> list[AgentState]:
    """Return every agent state for the team (alive or dead)."""
    d = agents_dir(team_root)
    out: list[AgentState] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(AgentState.from_dict(json.loads(p.read_text())))
        except (ValueError, json.JSONDecodeError, OSError):
            # Skip unreadable / mismatched-schema records; don't crash the caller.
            continue
    return out


def restore(team_root: Path) -> list[AgentState]:
    """Return only agents whose ``pid`` is still alive.

    This is the entry point a future native ``Agent`` re-attachment path
    would call on ``claude --resume``: scan, filter, re-bind. The
    binary-side reattachment of stdout/stdin is out of scope here — this
    module only owns the on-disk state.
    """
    return [a for a in list_all(team_root) if a.is_alive()]


def deregister(team_root: Path, name: str) -> bool:
    """Delete an agent's state file. Returns True iff the file existed."""
    path = agents_dir(team_root) / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def record_dispatch(team_root: Path, name: str, *, task_id: str, pid: int | None = None) -> None:
    """Convenience: update ``current_task_id`` + ``pid`` + ``last_seen``.

    The daemon calls this when it picks up a task on behalf of a
    registered agent. If the agent isn't yet registered, this is a no-op
    (the daemon's own kanban remains the source of truth for unregistered
    workers).
    """
    state = get(team_root, name)
    if state is None:
        return
    state.current_task_id = task_id
    if pid is not None:
        state.pid = pid
    state.last_seen = time.time()
    register(team_root, state)


__all__ = [
    "AgentState",
    "SCHEMA_VERSION",
    "agents_dir",
    "register",
    "get",
    "list_all",
    "restore",
    "deregister",
    "record_dispatch",
]
