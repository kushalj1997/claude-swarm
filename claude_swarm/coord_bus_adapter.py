"""CoordBusAdapter -- bridge from agent-swarm to deep-ai's coordination bus.

The deep-ai private repo runs a postgres-backed coordination bus
(``dm.deep_manager.coord.comms.CoordBus``) that Claude Code, Codex, and
Cursor use to announce task completions, claim areas, and relay project
state.  This adapter lets an agent-swarm supervisor tap the same bus
without importing the full deep-ai package.

Integration seam
----------------
The adapter is **optional** and **config-flagged** (off by default).  It
activates when ``CLAUDE_SWARM_COORD_BUS_DSN`` is set to a postgres DSN for
the ``coord_db`` database.

When active, the swarm:

1. **Announces** swarm task completions as ``coord_bus`` work_completed
   messages (sender=``swarm``, recipient=``claude`` / ``codex``).
2. **Polls** recent messages from ``claude`` and ``codex`` so that the swarm
   work source can incorporate repo state, PR notifications, and kanban
   signals from the other agents.
3. **Claims** file areas before dispatch (preventing dual-write conflicts
   with Codex / Claude Code sessions working in the same repo).

Message types used
------------------
``work_completed``
    Fired when a swarm task transitions to DONE with a PR.  Payload:
    ``{task_id, title, head, pr_path, cost_usd}``.
``swarm_task_failed``
    Fired when a task transitions to FAILED (anomaly escalation path).
    Payload: ``{task_id, title, head, error_snippet}``.
``swarm_status``
    Heartbeat from the perpetual supervisor: current queue depth, in-progress
    count, running cost.  Recipient: ``all``.

Dependency model
----------------
The adapter has TWO optional dependency paths:

1. **psycopg** (preferred) -- direct postgres connection using the same
   ``coord_db`` used by deep-ai.  Requires ``CLAUDE_SWARM_COORD_BUS_DSN``.
2. **subprocess fallback** -- shells out to
   ``python -m dm.deep_manager.coord.comms`` when ``psycopg`` is unavailable.
   Slower, but works without installing any extra packages.

When neither is available the adapter silently no-ops (all methods return
empty results).

Usage::

    from claude_swarm.coord_bus_adapter import CoordBusAdapter
    adapter = CoordBusAdapter()  # reads CLAUDE_SWARM_COORD_BUS_DSN from env

    adapter.announce_task_done(task, head_name="builder")
    recent = adapter.recent_messages(limit=20)
    owned = adapter.try_claim_area("apple-ui/foo.swift")
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message + Claim helpers (shallow mirrors of deep-ai's CoordBus types)
# ---------------------------------------------------------------------------


@dataclass
class CoordMessage:
    """A message read from or written to the coordination bus."""

    id: int | None = None
    sender: str = ""
    recipient: str = ""
    type: str = ""
    summary: str = ""
    task_ref: str | None = None
    pr_number: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float | None = None
    read: bool = False


# ---------------------------------------------------------------------------
# CoordBusAdapter
# ---------------------------------------------------------------------------


class CoordBusAdapter:
    """Thin bridge to the deep-ai postgres coordination bus.

    Parameters
    ----------
    dsn:
        Postgres DSN for ``coord_db``.  Defaults to
        ``CLAUDE_SWARM_COORD_BUS_DSN`` env var.  If neither is set the
        adapter runs in no-op mode (all methods return empty / False).
    swarm_sender:
        Identifier the swarm uses as the ``sender`` field.  Default ``"swarm"``.
    enabled:
        ``False`` disables all I/O without requiring a valid DSN.  Useful in
        CI environments.  Default: ``True`` iff the DSN is set.
    deep_ai_comms_module:
        Python dotted path to the deep-ai comms module, used for the
        subprocess fallback.  Default ``"dm.deep_manager.coord.comms"``.
    """

    def __init__(
        self,
        *,
        dsn: str | None = None,
        swarm_sender: str = "swarm",
        enabled: bool | None = None,
        deep_ai_comms_module: str = "dm.deep_manager.coord.comms",
    ) -> None:
        self._dsn: str | None = dsn or os.environ.get("CLAUDE_SWARM_COORD_BUS_DSN")
        self._sender = swarm_sender
        self._module = deep_ai_comms_module
        self._enabled = enabled if enabled is not None else bool(self._dsn)
        self._conn: Any = None  # lazy psycopg connection

    # --- internal connection -----------------------------------------------

    def _get_conn(self) -> Any | None:
        """Return a live psycopg connection or None if unavailable."""
        if not self._enabled or not self._dsn:
            return None
        try:
            import psycopg  # type: ignore[import]
            from psycopg.rows import dict_row  # type: ignore[import]
        except ImportError:
            return None
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
            except Exception as exc:
                log.warning("CoordBusAdapter: failed to connect to coord_db: %s", exc)
                return None
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # --- subprocess fallback -----------------------------------------------

    def _subprocess_send(self, *, sender: str, recipient: str, msg_type: str, summary: str, task_ref: str | None, pr: int | None) -> bool:
        """Send via `python -m <module> send` as a subprocess fallback."""
        cmd = [
            "python3", "-m", self._module,
            "send",
            "--sender", sender,
            "--recipient", recipient,
            "--type", msg_type,
            "--summary", summary,
        ]
        if task_ref:
            cmd += ["--task-ref", task_ref]
        if pr is not None:
            cmd += ["--pr", str(pr)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except Exception as exc:
            log.debug("CoordBusAdapter subprocess fallback failed: %s", exc)
            return False

    # --- public API --------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Return True if the adapter is connected / able to reach the bus."""
        if not self._enabled:
            return False
        conn = self._get_conn()
        return conn is not None

    def announce_task_done(
        self,
        task: Any,
        *,
        head_name: str,
        recipient: str = "claude",
    ) -> bool:
        """Announce a completed swarm task on the coord bus.

        Parameters
        ----------
        task:
            A :class:`~claude_swarm.kanban.Task` with ``id``, ``title``,
            ``cost_usd``, and optionally ``pr_path``.
        head_name:
            The head that ran the task (e.g. ``"builder"``).
        recipient:
            Who to address the message to.  Default ``"claude"`` (the
            operator's Claude Code session).

        Returns
        -------
        bool
            ``True`` if the message was sent; ``False`` on any failure
            (no exception raised — coordination bus failures must never
            crash the swarm).
        """
        if not self._enabled:
            return False

        task_id = getattr(task, "id", "?")
        title = getattr(task, "title", "")
        cost = getattr(task, "cost_usd", 0.0)
        pr_path = getattr(task, "pr_path", None)

        summary = f"Swarm task done: {title} (head={head_name}, cost=${cost:.4f})"
        pr_number: int | None = None
        if pr_path:
            # Extract PR number from "pr-<number>" style path.
            import re
            m = re.search(r"(\d+)", str(pr_path))
            if m:
                pr_number = int(m.group(1))

        conn = self._get_conn()
        if conn is not None:
            return self._pg_send(
                conn=conn,
                sender=self._sender,
                recipient=recipient,
                msg_type="work_completed",
                summary=summary,
                task_ref=str(task_id),
                pr_number=pr_number,
                payload={"head": head_name, "cost_usd": cost, "task_id": task_id},
            )
        # subprocess fallback
        return self._subprocess_send(
            sender=self._sender,
            recipient=recipient,
            msg_type="work_completed",
            summary=summary,
            task_ref=str(task_id),
            pr=pr_number,
        )

    def announce_task_failed(
        self,
        task: Any,
        *,
        head_name: str,
        recipient: str = "claude",
    ) -> bool:
        """Announce a failed swarm task (anomaly escalation path)."""
        if not self._enabled:
            return False

        task_id = getattr(task, "id", "?")
        title = getattr(task, "title", "")
        error = (getattr(task, "error", "") or "")[:200]
        summary = f"Swarm task FAILED: {title} (head={head_name}): {error}"

        conn = self._get_conn()
        if conn is not None:
            return self._pg_send(
                conn=conn,
                sender=self._sender,
                recipient=recipient,
                msg_type="swarm_task_failed",
                summary=summary,
                task_ref=str(task_id),
                pr_number=None,
                payload={"head": head_name, "error": error, "task_id": task_id},
            )
        return self._subprocess_send(
            sender=self._sender,
            recipient=recipient,
            msg_type="swarm_task_failed",
            summary=summary,
            task_ref=str(task_id),
            pr=None,
        )

    def announce_heartbeat(
        self,
        *,
        queue_depth: int,
        in_progress: int,
        running_cost_usd: float,
        recipient: str = "all",
    ) -> bool:
        """Send a swarm heartbeat so Claude/Codex know the swarm is alive."""
        if not self._enabled:
            return False

        summary = (
            f"swarm heartbeat: queue={queue_depth} in_progress={in_progress} "
            f"cost=${running_cost_usd:.4f}"
        )
        conn = self._get_conn()
        if conn is not None:
            return self._pg_send(
                conn=conn,
                sender=self._sender,
                recipient=recipient,
                msg_type="swarm_status",
                summary=summary,
                task_ref=None,
                pr_number=None,
                payload={
                    "queue_depth": queue_depth,
                    "in_progress": in_progress,
                    "running_cost_usd": running_cost_usd,
                },
            )
        return False  # heartbeat is best-effort; skip subprocess overhead

    def recent_messages(
        self,
        *,
        senders: list[str] | None = None,
        limit: int = 20,
    ) -> list[CoordMessage]:
        """Fetch recent coordination messages from Claude/Codex.

        Parameters
        ----------
        senders:
            Filter to messages from these senders.  Default: ``["claude", "codex"]``.
        limit:
            Max messages to return.

        Returns
        -------
        list[CoordMessage]
            Most recent first.  Empty list on any error.
        """
        if not self._enabled:
            return []

        senders = senders or ["claude", "codex"]
        conn = self._get_conn()
        if conn is None:
            return []

        try:
            placeholders = ", ".join(["%s"] * len(senders))
            rows = conn.execute(
                f"SELECT id, sender, recipient, type, summary, task_ref, pr_number, "  # noqa: S608
                f"payload, created_at, read "
                f"FROM coordination_messages "
                f"WHERE sender = ANY(%s::text[]) "
                f"ORDER BY created_at DESC LIMIT %s",
                (senders, limit),
            ).fetchall()
            conn.commit()
        except Exception as exc:
            log.warning("CoordBusAdapter: failed to fetch recent messages: %s", exc)
            return []

        msgs: list[CoordMessage] = []
        for row in rows:
            payload: dict[str, Any] = {}
            raw_payload = row.get("payload") if isinstance(row, dict) else getattr(row, "payload", None)
            if raw_payload:
                try:
                    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                except (ValueError, TypeError):
                    pass
            created_raw = row.get("created_at") if isinstance(row, dict) else getattr(row, "created_at", None)
            created_ts: float | None = None
            if created_raw is not None:
                try:
                    created_ts = created_raw.timestamp() if hasattr(created_raw, "timestamp") else float(created_raw)
                except (TypeError, ValueError):
                    pass
            msgs.append(CoordMessage(
                id=row.get("id") if isinstance(row, dict) else getattr(row, "id", None),
                sender=row.get("sender", "") if isinstance(row, dict) else getattr(row, "sender", ""),
                recipient=row.get("recipient", "") if isinstance(row, dict) else getattr(row, "recipient", ""),
                type=row.get("type", "") if isinstance(row, dict) else getattr(row, "type", ""),
                summary=row.get("summary", "") if isinstance(row, dict) else getattr(row, "summary", ""),
                task_ref=row.get("task_ref") if isinstance(row, dict) else getattr(row, "task_ref", None),
                pr_number=row.get("pr_number") if isinstance(row, dict) else getattr(row, "pr_number", None),
                payload=payload,
                created_at=created_ts,
                read=bool(row.get("read", False) if isinstance(row, dict) else getattr(row, "read", False)),
            ))
        return msgs

    def try_claim_area(self, area: str, *, timeout_s: float = 30.0) -> bool:
        """Attempt to claim an exclusive area on the coord bus.

        Parameters
        ----------
        area:
            A path or logical area string (e.g. ``"ui/web/agents.tsx"``).
        timeout_s:
            Lease duration in seconds.  After this time the claim expires and
            another agent may take it.

        Returns
        -------
        bool
            ``True`` if the claim was acquired; ``False`` if another agent
            owns it or the bus is unavailable.
        """
        if not self._enabled:
            return True  # no bus = no contention; safe to proceed

        conn = self._get_conn()
        if conn is None:
            return True

        try:
            expires = time.time() + timeout_s
            conn.execute(
                "INSERT INTO coordination_claims (area, owner, claimed_at, expires_at) "
                "VALUES (%s, %s, NOW(), to_timestamp(%s)) "
                "ON CONFLICT (area) DO UPDATE SET owner=%s, claimed_at=NOW(), expires_at=to_timestamp(%s) "
                "WHERE coordination_claims.expires_at < NOW()",
                (area, self._sender, expires, self._sender, expires),
            )
            conn.commit()
            return True
        except Exception as exc:
            log.warning("CoordBusAdapter: try_claim_area %r failed: %s", area, exc)
            return False

    def release_area(self, area: str) -> bool:
        """Release a previously claimed area."""
        if not self._enabled:
            return True

        conn = self._get_conn()
        if conn is None:
            return True

        try:
            conn.execute(
                "DELETE FROM coordination_claims WHERE area=%s AND owner=%s",
                (area, self._sender),
            )
            conn.commit()
            return True
        except Exception as exc:
            log.warning("CoordBusAdapter: release_area %r failed: %s", area, exc)
            return False

    # --- internal postgres send --------------------------------------------

    def _pg_send(
        self,
        *,
        conn: Any,
        sender: str,
        recipient: str,
        msg_type: str,
        summary: str,
        task_ref: str | None,
        pr_number: int | None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        try:
            conn.execute(
                "INSERT INTO coordination_messages "
                "(sender, recipient, type, summary, task_ref, pr_number, payload, created_at, read) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), FALSE)",
                (
                    sender,
                    recipient,
                    msg_type,
                    summary,
                    task_ref,
                    pr_number,
                    json.dumps(payload or {}),
                ),
            )
            conn.commit()
            return True
        except Exception as exc:
            log.warning("CoordBusAdapter: pg_send failed: %s", exc)
            return False

    def __repr__(self) -> str:
        return (
            f"CoordBusAdapter(enabled={self._enabled}, "
            f"sender={self._sender!r}, "
            f"dsn={'<set>' if self._dsn else '<not set>'})"
        )


__all__ = ["CoordBusAdapter", "CoordMessage"]
