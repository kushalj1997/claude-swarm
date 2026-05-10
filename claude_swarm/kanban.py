"""SQLite-backed kanban with first-class DAG dependencies.

This is a generic, dependency-light port of the swarm kanban primitive. The
defining feature is :meth:`Kanban.unblocked` — a topological iterator over
tasks whose declared blockers are all done. Auto-unblock cascade fires when
a task transitions to :attr:`TaskStatus.DONE`.

Concurrency model:
    * one writer (the supervisor process), many readers
    * SQLite WAL mode + ``BEGIN IMMEDIATE`` on the claim path
    * ``select_for_update``-equivalent: a single transaction picks one
      pending unblocked task and marks it ``in_progress`` atomically

The schema is deliberately small and stable; downstream tools (the plugin,
the dashboard, examples) depend on this contract.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ._paths import kanban_path as default_kanban_path


class TaskStatus(str, Enum):
    """Lifecycle states of a kanban task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"
    AWAITING_CHILDREN = "awaiting_children"


def _ulid() -> str:
    """Sortable, URL-safe id (48-bit ms timestamp + 80 random bits)."""
    return f"{int(time.time() * 1000):013x}-{uuid.uuid4().hex[:12]}"


@dataclass
class Task:
    """A unit of work the swarm can pick up.

    The fields are deliberately generic — there is nothing here about any
    particular domain. Downstream callers stash domain context in
    :attr:`metadata` (a free-form JSON dict) or :attr:`tags`.
    """

    id: str = field(default_factory=_ulid)
    type: str = "default"
    title: str = ""
    prompt: str = ""
    priority: int = 5
    role: str = "default"
    required_head: str = "builder"
    max_turns: int = 100
    max_tokens: int = 4096
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    parent_task_id: str | None = None
    worker_id: str | None = None
    cost_usd: float = 0.0
    result: str | None = None
    error: str | None = None
    worktree_path: str | None = None
    pr_path: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    delegation_depth: int = 0
    files_owned: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Serialize for persistence. Lists/dicts become JSON strings."""
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "prompt": self.prompt,
            "priority": self.priority,
            "role": self.role,
            "required_head": self.required_head,
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "parent_task_id": self.parent_task_id,
            "worker_id": self.worker_id,
            "cost_usd": self.cost_usd,
            "result": self.result,
            "error": self.error,
            "worktree_path": self.worktree_path,
            "pr_path": self.pr_path,
            "blocked_by": json.dumps(self.blocked_by) if self.blocked_by else None,
            "blocks": json.dumps(self.blocks) if self.blocks else None,
            "delegation_depth": self.delegation_depth,
            "files_owned": json.dumps(self.files_owned) if self.files_owned else None,
            "tags": json.dumps(self.tags) if self.tags else None,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> Task:
        d: dict[str, Any] = dict(row)
        for col in ("blocked_by", "blocks", "files_owned", "tags"):
            raw = d.get(col)
            if raw:
                try:
                    d[col] = json.loads(raw)
                except (TypeError, ValueError):
                    d[col] = []
            else:
                d[col] = []
        if d.get("metadata"):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (TypeError, ValueError):
                d["metadata"] = {}
        else:
            d["metadata"] = {}
        d["status"] = TaskStatus(d["status"])
        return cls(**d)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    title             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 5,
    role              TEXT NOT NULL DEFAULT 'default',
    required_head     TEXT NOT NULL DEFAULT 'builder',
    max_turns         INTEGER NOT NULL DEFAULT 100,
    max_tokens        INTEGER NOT NULL DEFAULT 4096,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        REAL NOT NULL,
    started_at        REAL NULL,
    completed_at      REAL NULL,
    parent_task_id    TEXT NULL,
    worker_id         TEXT NULL,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    result            TEXT NULL,
    error             TEXT NULL,
    worktree_path     TEXT NULL,
    pr_path           TEXT NULL,
    blocked_by        TEXT NULL,
    blocks            TEXT NULL,
    delegation_depth  INTEGER NOT NULL DEFAULT 0,
    files_owned       TEXT NULL,
    tags              TEXT NULL,
    metadata          TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS status_timeline (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    from_status  TEXT NULL,
    to_status    TEXT NOT NULL,
    reason       TEXT NULL,
    ts           REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_timeline_task ON status_timeline(task_id, ts);
"""


class Kanban:
    """SQLite-backed task board with DAG dependencies + status timeline."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_kanban_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    # ----- connection helpers ----------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    # ----- write paths ------------------------------------------------

    def submit(self, task: Task) -> Task:
        """Persist a new task. Returns the task as stored.

        ``submit`` is idempotent on the primary key; a duplicate ``id``
        raises :class:`sqlite3.IntegrityError` to surface mistakes.
        """
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            self._timeline(conn, task.id, None, task.status.value, "submit")
        return task

    def update(self, task_id: str, **fields: Any) -> Task | None:
        """Patch a subset of columns. ``status`` writes a timeline row.

        ``reason`` is a virtual field: extracted before the UPDATE and
        forwarded to the status timeline. It is not a column on ``tasks``.
        """
        if not fields:
            return self.get(task_id)
        reason = fields.pop("reason", None)
        # Serialise list/dict values for json columns.
        for col in ("blocked_by", "blocks", "files_owned", "tags", "metadata"):
            if col in fields and fields[col] is not None and not isinstance(fields[col], str):
                fields[col] = json.dumps(fields[col]) if fields[col] else None
        if "status" in fields and isinstance(fields["status"], TaskStatus):
            fields["status"] = fields["status"].value
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if existing is None:
                return None
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE tasks SET {sets} WHERE id=?",
                    (*fields.values(), task_id),
                )
            if "status" in fields and fields["status"] != existing["status"]:
                self._timeline(
                    conn, task_id, existing["status"], fields["status"], reason
                )
                # Auto-unblock cascade.
                if fields["status"] == TaskStatus.DONE.value:
                    self._cascade_unblock(conn, task_id)
        return self.get(task_id)

    def transition(
        self,
        task_id: str,
        to_status: TaskStatus,
        *,
        reason: str | None = None,
    ) -> Task | None:
        """Convenience wrapper: status-only update with timeline reason."""
        return self.update(task_id, status=to_status, reason=reason)

    def add_blocked_by(self, task_id: str, blocker_ids: Iterable[str]) -> None:
        """Append blockers to a task and mirror the inverse on the blockers."""
        ids = [b for b in blocker_ids if b]
        if not ids:
            return
        with self._conn() as conn:
            row = conn.execute(
                "SELECT blocked_by FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = json.loads(row["blocked_by"]) if row["blocked_by"] else []
            merged = sorted(set(current) | set(ids))
            conn.execute(
                "UPDATE tasks SET blocked_by=? WHERE id=?",
                (json.dumps(merged) if merged else None, task_id),
            )
            for blocker in ids:
                brow = conn.execute(
                    "SELECT blocks FROM tasks WHERE id=?", (blocker,)
                ).fetchone()
                if brow is None:
                    continue
                blist = json.loads(brow["blocks"]) if brow["blocks"] else []
                if task_id not in blist:
                    blist.append(task_id)
                    conn.execute(
                        "UPDATE tasks SET blocks=? WHERE id=?",
                        (json.dumps(sorted(blist)), blocker),
                    )

    def add_blocks(self, task_id: str, blocked_ids: Iterable[str]) -> None:
        """Inverse of :meth:`add_blocked_by` — symmetric DAG edges."""
        ids = [b for b in blocked_ids if b]
        for blocked in ids:
            self.add_blocked_by(blocked, [task_id])

    # ----- read paths -------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        parent_task_id: str | None = None,
        tag: str | None = None,
    ) -> list[Task]:
        """Return tasks matching the given filters (alias of historical ``list``)."""
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if status is not None:
            sql += " AND status=?"
            args.append(status.value)
        if parent_task_id is not None:
            sql += " AND parent_task_id=?"
            args.append(parent_task_id)
        sql += " ORDER BY priority ASC, created_at ASC"
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        tasks = [Task.from_row(r) for r in rows]
        if tag is not None:
            tasks = [t for t in tasks if tag in t.tags]
        return tasks

    def unblocked(
        self, *, required_head: str | None = None, limit: int | None = None
    ) -> list[Task]:
        """Return pending tasks whose blockers are all done.

        This is the topological iterator that makes the DAG a first-class
        primitive. Highest priority + oldest first.
        """
        pending = self.list_tasks(status=TaskStatus.PENDING)
        out: list[Task] = []
        for t in pending:
            if required_head is not None and t.required_head != required_head:
                continue
            if not t.blocked_by:
                out.append(t)
                continue
            ok = True
            for blocker in t.blocked_by:
                btask = self.get(blocker)
                if btask is None or btask.status != TaskStatus.DONE:
                    ok = False
                    break
            if ok:
                out.append(t)
            if limit is not None and len(out) >= limit:
                break
        return out

    def claim_one(self, *, worker_id: str, required_head: str | None = None) -> Task | None:
        """Atomically claim the next unblocked task.

        Uses ``BEGIN IMMEDIATE`` so two simultaneous claims can't both win
        the same task. Returns ``None`` if no task is available.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status='pending' "
                    "ORDER BY priority ASC, created_at ASC"
                ).fetchall()
                pick: sqlite3.Row | None = None
                for row in rows:
                    if required_head is not None and row["required_head"] != required_head:
                        continue
                    blockers = json.loads(row["blocked_by"]) if row["blocked_by"] else []
                    if blockers:
                        any_open = False
                        for b in blockers:
                            br = conn.execute(
                                "SELECT status FROM tasks WHERE id=?", (b,)
                            ).fetchone()
                            if br is None or br["status"] != "done":
                                any_open = True
                                break
                        if any_open:
                            continue
                    pick = row
                    break
                if pick is None:
                    conn.execute("ROLLBACK")
                    return None
                now = time.time()
                conn.execute(
                    "UPDATE tasks SET status='in_progress', worker_id=?, started_at=? WHERE id=?",
                    (worker_id, now, pick["id"]),
                )
                self._timeline(
                    conn, pick["id"], pick["status"], "in_progress", f"claim:{worker_id}"
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        # Fetch fresh row outside the transaction so caller sees the new state.
        return self.get(pick["id"])

    def timeline(self, task_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM status_timeline WHERE task_id=? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- internals --------------------------------------------------

    def _timeline(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        from_status: str | None,
        to_status: str,
        reason: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO status_timeline(task_id, from_status, to_status, reason, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, from_status, to_status, reason, time.time()),
        )

    def _cascade_unblock(self, conn: sqlite3.Connection, just_done_id: str) -> None:
        """No-op for now; ``unblocked()`` is recomputed on demand.

        Reserved as the canonical hook for future cache invalidation if
        we move the DAG into a denormalised view.
        """
        return None


__all__ = ["Kanban", "Task", "TaskStatus"]
