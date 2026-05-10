"""Directed inter-head messaging with bounded JSON inboxes.

Two transports live here:

* :class:`Inbox` — a per-recipient JSON file capped at ``max_messages``
  (drop-oldest on overflow). Filesystem-only, atomic via tmp+rename. Useful
  for cross-process / cross-host coordination where SQLite isn't desired.
* :class:`MessageBus` — a thin facade so heads call ``bus.send(...)`` /
  ``bus.recv(...)`` regardless of transport. Ships with the JSON inbox by
  default; extensible to SQLite, HTTP, etc.

Design notes:
    - directed: every message has a single named recipient (or ``"*"``)
    - bounded: full inboxes drop the oldest message and emit a warning
    - atomic: writes go via tmp file + ``os.replace``
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ._paths import inboxes_dir as default_inboxes_dir

log = logging.getLogger(__name__)

DEFAULT_MAX_MESSAGES: int = 256
BROADCAST_RECIPIENT: str = "*"


@dataclass
class Message:
    """A single directed message between heads."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    sender: str = ""
    recipient: str = ""
    kind: str = "note"
    body: dict[str, Any] = field(default_factory=dict)
    in_reply_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(**d)


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` via a same-dir tmp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class Inbox:
    """A bounded JSON-file inbox for one named recipient.

    The on-disk format is a JSON array of message dicts, oldest-first. Reads
    return a snapshot; writes append (with drop-oldest if over capacity).
    """

    def __init__(
        self,
        recipient: str,
        *,
        root: Path | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.recipient = recipient
        self.root = Path(root) if root is not None else default_inboxes_dir()
        self.max_messages = max_messages
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / f"{self.recipient}.json"

    def _read_raw(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [m for m in data if isinstance(m, dict)]

    def append(self, message: Message) -> None:
        msgs = self._read_raw()
        msgs.append(message.to_dict())
        if len(msgs) > self.max_messages:
            dropped = len(msgs) - self.max_messages
            msgs = msgs[-self.max_messages :]
            log.warning(
                "inbox %s over capacity; dropped %d oldest message(s)",
                self.recipient,
                dropped,
            )
        _atomic_write(self.path, json.dumps(msgs, indent=2))

    def snapshot(self) -> list[Message]:
        """Return a snapshot of every message in this inbox, oldest first."""
        return [Message.from_dict(m) for m in self._read_raw()]

    def drain(self) -> list[Message]:
        """Return all messages and clear the inbox atomically."""
        msgs = self.snapshot()
        _atomic_write(self.path, "[]")
        return msgs


class MessageBus:
    """Facade for sending + receiving messages between heads.

    The default backend is the JSON :class:`Inbox`. Subclass + override
    ``_inbox`` to swap in SQLite, HTTP, or a remote-control transport.
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.root = Path(root) if root is not None else default_inboxes_dir()
        self.max_messages = max_messages

    def _inbox(self, recipient: str) -> Inbox:
        return Inbox(recipient, root=self.root, max_messages=self.max_messages)

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        kind: str = "note",
        body: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> Message:
        msg = Message(
            sender=sender,
            recipient=recipient,
            kind=kind,
            body=body or {},
            in_reply_to=in_reply_to,
        )
        if recipient == BROADCAST_RECIPIENT:
            for f in sorted(self.root.glob("*.json")):
                inbox = Inbox(f.stem, root=self.root, max_messages=self.max_messages)
                inbox.append(msg)
        else:
            self._inbox(recipient).append(msg)
        return msg

    def recv(self, recipient: str) -> list[Message]:
        return self._inbox(recipient).snapshot()

    def drain(self, recipient: str) -> list[Message]:
        return self._inbox(recipient).drain()


__all__ = ["BROADCAST_RECIPIENT", "Inbox", "Message", "MessageBus"]
