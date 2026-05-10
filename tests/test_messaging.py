"""Messaging tests."""
from __future__ import annotations

from pathlib import Path

from claude_swarm.messaging import (
    BROADCAST_RECIPIENT,
    Inbox,
    MessageBus,
)


def test_inbox_append_and_list(tmp_path: Path) -> None:
    inbox = Inbox("alice", root=tmp_path)
    bus = MessageBus(root=tmp_path)
    bus.send(sender="bob", recipient="alice", body={"hi": 1})
    msgs = inbox.snapshot()
    assert len(msgs) == 1
    assert msgs[0].sender == "bob"
    assert msgs[0].body == {"hi": 1}


def test_inbox_drop_oldest_when_full(tmp_path: Path) -> None:
    inbox = Inbox("alice", root=tmp_path, max_messages=3)
    bus = MessageBus(root=tmp_path, max_messages=3)
    for i in range(5):
        bus.send(sender="bob", recipient="alice", body={"i": i})
    msgs = inbox.snapshot()
    assert len(msgs) == 3
    assert [m.body["i"] for m in msgs] == [2, 3, 4]


def test_drain_clears_inbox(tmp_path: Path) -> None:
    bus = MessageBus(root=tmp_path)
    bus.send(sender="bob", recipient="alice", body={})
    inbox = Inbox("alice", root=tmp_path)
    drained = inbox.drain()
    assert len(drained) == 1
    assert inbox.snapshot() == []


def test_broadcast_fans_out(tmp_path: Path) -> None:
    bus = MessageBus(root=tmp_path)
    # Seed two inboxes so the broadcast has recipients to find.
    bus.send(sender="seed", recipient="alice", body={})
    bus.send(sender="seed", recipient="bob", body={})
    bus.send(sender="conductor", recipient=BROADCAST_RECIPIENT, body={"ping": True})
    a = Inbox("alice", root=tmp_path).snapshot()
    b = Inbox("bob", root=tmp_path).snapshot()
    assert any(m.body == {"ping": True} for m in a)
    assert any(m.body == {"ping": True} for m in b)


def test_atomic_writes_survive_repeat_calls(tmp_path: Path) -> None:
    bus = MessageBus(root=tmp_path)
    for i in range(20):
        bus.send(sender="x", recipient="y", body={"i": i})
    msgs = Inbox("y", root=tmp_path).snapshot()
    assert [m.body["i"] for m in msgs] == list(range(20))
