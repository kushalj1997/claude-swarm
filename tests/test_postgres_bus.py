"""Tests for the postgres production transport.

Two tiers:

* **Pure tests** (always run, no DB): exercise DSN normalisation, the SQL the
  migration renders, and the lazy-import contract. These cover the
  constraint-widening logic deterministically.
* **Live tests** (opt-in): run only when ``CLAUDE_SWARM_TEST_PG_DSN`` points at
  a *throwaway scratch database* the test owns. They are skipped otherwise so
  CI never depends on — and never touches — the shared production ``coord_db``.

To run the live tier against a disposable database::

    createdb claude_swarm_bus_test
    CLAUDE_SWARM_TEST_PG_DSN=postgresql://localhost/claude_swarm_bus_test \\
        python -m pytest tests/test_postgres_bus.py -q
    dropdb claude_swarm_bus_test
"""
from __future__ import annotations

import os

import pytest

from claude_swarm.bus import Delegation, DelegationStatus
from claude_swarm.postgres_bus import (
    _MSG_TYPE_VALUES,
    _RECIPIENT_VALUES,
    _SENDER_VALUES,
    _plain_dsn,
    _sql_in_list,
    _widen_sql,
)

LIVE_DSN = os.environ.get("CLAUDE_SWARM_TEST_PG_DSN")


# --------------------------------------------------------------------------
# Pure tests (no database) — always run
# --------------------------------------------------------------------------


def test_plain_dsn_strips_sqlalchemy_driver() -> None:
    assert (
        _plain_dsn("postgresql+psycopg://u:p@h:5432/coord_db")
        == "postgresql://u:p@h:5432/coord_db"
    )
    # Already-plain DSN is unchanged.
    assert _plain_dsn("postgresql://h/db") == "postgresql://h/db"


def test_sql_in_list_quotes_values() -> None:
    assert _sql_in_list(("a", "b")) == "'a', 'b'"


def test_sql_in_list_rejects_embedded_quote() -> None:
    with pytest.raises(ValueError, match="single quote"):
        _sql_in_list(("ok", "bad'value"))


def test_widen_sql_covers_all_three_columns() -> None:
    stmts = _widen_sql()
    assert len(stmts) == 3
    joined = "\n".join(stmts)
    assert "chk_coord_msg_sender" in joined
    assert "chk_coord_msg_recipient" in joined
    assert "chk_coord_msg_type" in joined
    # Each widening must be idempotent (drop-if-exists before add).
    assert joined.count("DROP CONSTRAINT IF EXISTS") >= 6


def test_widen_sql_includes_new_agent_types() -> None:
    joined = "\n".join(_widen_sql())
    for agent in ("codex", "cursor", "api-worker", "claude-code", "scout"):
        assert f"'{agent}'" in joined
    for verb in ("task_delegated", "task_done", "verify_request"):
        assert f"'{verb}'" in joined


def test_sender_values_exclude_broadcast_lane() -> None:
    # 'all' is a recipient-only broadcast lane, never a sender.
    assert "all" not in _SENDER_VALUES
    assert "all" in _RECIPIENT_VALUES


def test_legacy_deepai_values_preserved() -> None:
    # Back-compat: the original deep-ai senders/recipients/types still validate.
    for v in ("claude", "codex", "operator", "slack"):
        assert v in _RECIPIENT_VALUES
    for v in ("work_started", "heartbeat", "slack_inbound"):
        assert v in _MSG_TYPE_VALUES


def test_postgres_bus_import_does_not_require_db() -> None:
    # Constructing requires psycopg (present) but NOT a live connection.
    from claude_swarm.postgres_bus import PostgresBus

    bus = PostgresBus("postgresql://localhost/does-not-connect-yet",
                      auto_migrate=False)
    assert bus._dsn == "postgresql://localhost/does-not-connect-yet"


# --------------------------------------------------------------------------
# Live tests (opt-in scratch DB) — skipped unless CLAUDE_SWARM_TEST_PG_DSN set
# --------------------------------------------------------------------------

pytestmark_live = pytest.mark.skipif(
    LIVE_DSN is None,
    reason="set CLAUDE_SWARM_TEST_PG_DSN to a throwaway DB to run live pg tests",
)


@pytest.fixture
def pg_bus():  # type: ignore[no-untyped-def]
    """A PostgresBus against the scratch DB, schema ensured, rows cleared."""
    from claude_swarm.postgres_bus import PostgresBus

    assert LIVE_DSN is not None
    bus = PostgresBus(LIVE_DSN, auto_migrate=True)
    bus.ensure_schema()
    # Clear only OUR test table rows (never DROP/TRUNCATE shared infra; the
    # scratch DB is owned by the test so a DELETE of all rows is safe here).
    import psycopg

    with psycopg.connect(_plain_dsn(LIVE_DSN), autocommit=True) as conn:
        conn.execute("DELETE FROM coordination_messages")
    return bus


@pytestmark_live
def test_ensure_schema_is_idempotent(pg_bus) -> None:  # type: ignore[no-untyped-def]
    # Running twice must not raise.
    pg_bus.ensure_schema()
    pg_bus.ensure_schema()


@pytestmark_live
def test_send_and_poll_delegation(pg_bus) -> None:  # type: ignore[no-untyped-def]
    deleg = Delegation(
        task_ref="pg-task-1", sender="dispatch", recipient="codex",
        summary="do it", payload={"prompt": "x"},
    )
    msg_id = pg_bus.send_delegation(deleg)
    assert msg_id > 0
    rows = pg_bus.delegations("codex")
    assert len(rows) == 1
    assert rows[0].task_ref == "pg-task-1"
    assert rows[0].payload["prompt"] == "x"


@pytestmark_live
def test_status_of_returns_latest(pg_bus) -> None:  # type: ignore[no-untyped-def]
    pg_bus.send_delegation(Delegation(
        task_ref="pg-2", sender="dispatch", recipient="codex"))
    pg_bus.send_delegation(Delegation(
        task_ref="pg-2", sender="codex", recipient="dispatch",
        status=DelegationStatus.DONE, branch="b", pr_number=5))
    latest = pg_bus.status_of("pg-2")
    assert latest is not None
    assert latest.status is DelegationStatus.DONE
    assert latest.pr_number == 5


@pytestmark_live
def test_new_agent_types_pass_check_constraints(pg_bus) -> None:  # type: ignore[no-untyped-def]
    # The widened constraints must accept codex/cursor/api-worker + task verbs.
    for recipient in ("codex", "cursor", "api-worker", "claude-code"):
        mid = pg_bus.send(
            sender="dispatch", recipient=recipient, msg_type="task_delegated",
            summary="x", task_ref="t",
        )
        assert mid > 0


@pytestmark_live
def test_poll_filters_by_msg_type(pg_bus) -> None:  # type: ignore[no-untyped-def]
    pg_bus.send(sender="dispatch", recipient="codex", msg_type="heartbeat",
                summary="hb")
    pg_bus.send(sender="dispatch", recipient="codex", msg_type="task_delegated",
                summary="task", task_ref="t")
    delegs = pg_bus.delegations("codex")
    assert len(delegs) == 1
    assert delegs[0].task_ref == "t"
