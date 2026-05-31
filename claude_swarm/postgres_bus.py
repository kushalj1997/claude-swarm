"""Production task-delegation transport against ``coord_db`` (postgres).

This is the production sibling of :class:`claude_swarm.bus.TaskBus`. It speaks
the same delegation vocabulary but persists to a postgres ``coordination_messages``
table so a supervisor on one host can hand a TASK to Codex / Cursor / an API
worker on another, and so the deep-ai operator-facing surfaces (Slack relay,
dashboards) can read the same stream.

``psycopg`` is imported **lazily** inside the constructor so the dependency-light
OSS default (:class:`TaskBus`) never requires it. Install the ``postgres`` extra
to use this transport::

    pip install "claude-swarm[postgres]"

Schema parity with deep-ai ``dm.deep_manager.coord.comms`` is intentional: this
transport widens the existing ``sender`` / ``recipient`` / ``msg_type`` CHECK
constraints to the expanded agent taxonomy + task-lifecycle verbs via an
**idempotent migration** that is safe to run on a fresh database OR on an
existing deep-ai ``coord_db`` that still has the narrow 2026-05-29 constraints.

Safety (charter §6): the migration only ``ADD``s columns and ``DROP``s + re-``ADD``s
CHECK constraints (never the table); it never ``DROP TABLE`` / ``TRUNCATE``.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .bus import (
    BROADCAST,
    TASK_MSG_TYPES,
    VALID_MSG_TYPES,
    VALID_PARTICIPANTS,
    Delegation,
    DelegationStatus,
    validate_send,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    import psycopg

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Expanded enum value lists (rendered into the CHECK constraints below)
# ---------------------------------------------------------------------------

# Concrete addressable participants (the broadcast/wildcard forms are handled in
# Python by validate_send; postgres only stores literal sender/recipient names).
_SENDER_VALUES: tuple[str, ...] = tuple(
    sorted(VALID_PARTICIPANTS - {"all"})  # senders can't be the broadcast lane
)
_RECIPIENT_VALUES: tuple[str, ...] = tuple(sorted(VALID_PARTICIPANTS))
_MSG_TYPE_VALUES: tuple[str, ...] = tuple(sorted(VALID_MSG_TYPES))


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Render a tuple of strings as a SQL ``IN`` value list (single-quoted)."""
    # Values are module constants (never user input); still reject any quote to
    # be defensive against a future taxonomy typo introducing an injection.
    for v in values:
        if "'" in v:
            raise ValueError(f"taxonomy value {v!r} contains a single quote")
    return ", ".join(f"'{v}'" for v in values)


_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS coordination_messages (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sender      TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    msg_type    TEXT NOT NULL,
    task_ref    TEXT,
    branch      TEXT,
    pr_number   INT,
    summary     TEXT NOT NULL DEFAULT '',
    payload     JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'unread',
    ack_of      BIGINT,
    read_at     TIMESTAMPTZ,
    acked_at    TIMESTAMPTZ
);
"""

# Idempotent CHECK-constraint widening. For each of sender/recipient/msg_type:
# drop any pre-existing CHECK that mentions the column (by definition text),
# then add the named one with the expanded value list. Safe on a fresh table
# (no constraints yet) and on an existing deep-ai coord_db (narrow constraints).
_DDL_WIDEN_TEMPLATE = """
DO $$
DECLARE
    _old TEXT;
BEGIN
    FOR _old IN
        SELECT cc.conname
          FROM pg_constraint cc
          JOIN pg_class      cl ON cl.oid = cc.conrelid
         WHERE cl.relname = 'coordination_messages'
           AND cc.contype = 'c'
           AND pg_get_constraintdef(cc.oid) LIKE '%{column}%'
           AND cc.conname <> '{conname}'
    LOOP
        EXECUTE format('ALTER TABLE coordination_messages DROP CONSTRAINT IF EXISTS %I', _old);
    END LOOP;
    ALTER TABLE coordination_messages DROP CONSTRAINT IF EXISTS {conname};
    ALTER TABLE coordination_messages
        ADD CONSTRAINT {conname}
        CHECK ({column} IN ({values}));
END;
$$;
"""

_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_coord_msg_recipient_status_created
    ON coordination_messages (recipient, status, created_at);

CREATE INDEX IF NOT EXISTS ix_coord_msg_task_ref
    ON coordination_messages (task_ref)
    WHERE task_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_coord_msg_type
    ON coordination_messages (msg_type);
"""

_DDL_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION _notify_coord_msg() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('coord_msg', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DDL_TRIGGER = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'tg_coord_msg_notify'
          AND tgrelid = 'coordination_messages'::regclass
    ) THEN
        CREATE TRIGGER tg_coord_msg_notify
            AFTER INSERT ON coordination_messages
            FOR EACH ROW EXECUTE FUNCTION _notify_coord_msg();
    END IF;
END;
$$;
"""


def _plain_dsn(url: str) -> str:
    """Convert a SQLAlchemy-style URL to a plain psycopg3-compatible DSN."""
    prefix = "postgresql+psycopg://"
    if url.startswith(prefix):
        return "postgresql://" + url[len(prefix):]
    return url


def _widen_sql() -> list[str]:
    """Render the three idempotent CHECK-widening statements."""
    return [
        _DDL_WIDEN_TEMPLATE.format(
            column="sender",
            conname="chk_coord_msg_sender",
            values=_sql_in_list(_SENDER_VALUES),
        ),
        _DDL_WIDEN_TEMPLATE.format(
            column="recipient",
            conname="chk_coord_msg_recipient",
            values=_sql_in_list(_RECIPIENT_VALUES),
        ),
        _DDL_WIDEN_TEMPLATE.format(
            column="msg_type",
            conname="chk_coord_msg_type",
            values=_sql_in_list(_MSG_TYPE_VALUES),
        ),
    ]


class PostgresBus:
    """Postgres-backed task-delegation bus against ``coord_db``.

    Parameters
    ----------
    dsn:
        psycopg connection URL/DSN. ``postgresql+psycopg://`` is normalised to
        a plain ``postgresql://`` DSN. Required — there is no default here so
        the OSS package never reaches for a connection string it doesn't have.
    auto_migrate:
        When ``True`` (default), :meth:`ensure_schema` runs lazily on the first
        method that touches the table.
    """

    def __init__(self, dsn: str, *, auto_migrate: bool = True) -> None:
        # psycopg is NOT imported here — importing this module must never fail
        # just because psycopg is absent (the test_postgres_bus_import_does_not_
        # require_db contract). The ImportError surfaces on first connect() call.
        self._dsn = _plain_dsn(dsn)
        self._auto_migrate = auto_migrate
        self._migrated = False

    # ----- schema -----------------------------------------------------

    def ensure_schema(self) -> None:
        """Apply idempotent DDL: table, widened constraints, indexes, trigger.

        Safe to call repeatedly and safe to run against an existing deep-ai
        ``coord_db`` — the constraint-widening only expands the accepted value
        set; it never narrows or drops data.
        """
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "PostgresBus requires psycopg; install with "
                "`pip install \"claude-swarm[postgres]\"`"
            ) from exc

        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute(_DDL_TABLE)
            for stmt in _widen_sql():
                conn.execute(stmt)
            conn.execute(_DDL_INDEXES)
            conn.execute(_DDL_TRIGGER_FN)
            conn.execute(_DDL_TRIGGER)
        self._migrated = True
        log.debug("coord_db delegation schema ensured")

    def _maybe_migrate(self) -> None:
        if self._auto_migrate and not self._migrated:
            self.ensure_schema()

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ImportError(
                "PostgresBus requires psycopg; install with "
                "`pip install \"claude-swarm[postgres]\"`"
            ) from exc
        return psycopg.connect(self._dsn, row_factory=dict_row)

    # ----- send paths -------------------------------------------------

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        msg_type: str,
        summary: str,
        task_ref: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        payload: dict[str, Any] | None = None,
        ack_of: int | None = None,
    ) -> int:
        """Insert a coordination message; return its id. Validated first."""
        if not summary:
            raise ValueError("summary must be a non-empty string")
        validate_send(sender, recipient, msg_type)
        if recipient == BROADCAST:
            # Postgres uses the literal 'all' lane for broadcast (parity with
            # deep-ai); '*' is the in-process JSON-bus broadcast token.
            recipient = "all"
        self._maybe_migrate()

        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO coordination_messages
                    (sender, recipient, msg_type, task_ref, branch, pr_number,
                     summary, payload, ack_of)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    sender,
                    recipient,
                    msg_type,
                    task_ref,
                    branch,
                    pr_number,
                    summary,
                    json.dumps(payload or {}),
                    ack_of,
                ),
            ).fetchone()
        assert row is not None
        msg_id = int(row["id"])
        log.info(
            "coord_msg id=%d sender=%s recipient=%s type=%s task_ref=%s",
            msg_id,
            sender,
            recipient,
            msg_type,
            task_ref,
        )
        return msg_id

    def send_delegation(self, deleg: Delegation) -> int:
        """Persist a :class:`Delegation`, lifting its fields into real columns."""
        return self.send(
            sender=deleg.sender,
            recipient=deleg.recipient,
            msg_type=deleg.status.value,
            summary=deleg.summary or f"{deleg.status.value} {deleg.task_ref}",
            task_ref=deleg.task_ref,
            branch=deleg.branch,
            pr_number=deleg.pr_number,
            payload=deleg.payload,
        )

    # ----- read paths -------------------------------------------------

    def poll(
        self,
        recipient: str,
        *,
        unread_only: bool = True,
        since_id: int | None = None,
        msg_types: frozenset[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return messages addressed to ``recipient`` (or the ``all`` lane)."""
        if recipient not in VALID_PARTICIPANTS and not recipient.startswith("agent:"):
            raise ValueError(f"recipient {recipient!r} not a valid participant")
        self._maybe_migrate()

        clauses = ["(recipient = %s OR recipient = 'all')"]
        params: list[Any] = [recipient]
        if unread_only:
            clauses.append("status = 'unread'")
        if since_id is not None:
            clauses.append("id > %s")
            params.append(since_id)
        if msg_types:
            clauses.append("msg_type = ANY(%s)")
            params.append(list(msg_types))
        where = " AND ".join(clauses)
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM coordination_messages WHERE {where} "
                f"ORDER BY id ASC LIMIT %s",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def delegations(
        self,
        recipient: str,
        *,
        status: DelegationStatus | None = None,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[Delegation]:
        """Return task-delegation rows addressed to ``recipient``."""
        types = frozenset({status.value}) if status is not None else TASK_MSG_TYPES
        rows = self.poll(
            recipient,
            unread_only=unread_only,
            msg_types=types,
            limit=limit,
        )
        out: list[Delegation] = []
        for r in rows:
            payload = r.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(
                Delegation(
                    task_ref=str(r.get("task_ref") or ""),
                    sender=str(r["sender"]),
                    recipient=str(r["recipient"]),
                    status=DelegationStatus(r["msg_type"]),
                    summary=str(r.get("summary") or ""),
                    branch=r.get("branch"),
                    pr_number=r.get("pr_number"),
                    payload=dict(payload),
                )
            )
        return out

    def status_of(self, task_ref: str) -> Delegation | None:
        """Return the latest delegation row for ``task_ref`` across all agents."""
        self._maybe_migrate()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM coordination_messages
                 WHERE task_ref = %s AND msg_type = ANY(%s)
                 ORDER BY id DESC LIMIT 1
                """,
                (task_ref, list(TASK_MSG_TYPES)),
            ).fetchone()
        if row is None:
            return None
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return Delegation(
            task_ref=str(row["task_ref"]),
            sender=str(row["sender"]),
            recipient=str(row["recipient"]),
            status=DelegationStatus(row["msg_type"]),
            summary=str(row.get("summary") or ""),
            branch=row.get("branch"),
            pr_number=row.get("pr_number"),
            payload=dict(payload),
        )

    def mark_read(self, ids: list[int]) -> None:
        """Transition the given message ids from ``unread`` to ``read``."""
        if not ids:
            return
        self._maybe_migrate()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE coordination_messages
                   SET status = 'read', read_at = now()
                 WHERE id = ANY(%s) AND status = 'unread'
                """,
                (ids,),
            )


__all__ = ["PostgresBus"]
