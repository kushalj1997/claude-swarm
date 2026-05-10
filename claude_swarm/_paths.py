"""Resolve canonical filesystem locations for swarm state.

We never hardcode absolute paths. Defaults derive from the current working
directory and ``$CLAUDE_SWARM_HOME`` (override via env). Tests pass explicit
paths into the public APIs.
"""
from __future__ import annotations

import os
from pathlib import Path


#: Root for swarm state directories. Derive from ``$CLAUDE_SWARM_HOME`` if set,
#: otherwise the current working directory.
def swarm_home(override: Path | None = None) -> Path:
    """Return the directory under which swarm state lives.

    Resolution order:
        1. explicit ``override`` argument
        2. ``$CLAUDE_SWARM_HOME`` env var
        3. ``<cwd>/.claude-swarm``
    """
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get("CLAUDE_SWARM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / ".claude-swarm").resolve()


def state_dir(home: Path | None = None) -> Path:
    return swarm_home(home) / "state"


def kanban_path(home: Path | None = None) -> Path:
    return state_dir(home) / "kanban.sqlite"


def inboxes_dir(home: Path | None = None) -> Path:
    return state_dir(home) / "inboxes"


def pull_requests_dir(home: Path | None = None) -> Path:
    return state_dir(home) / "pull_requests"


def worktrees_dir(home: Path | None = None) -> Path:
    return swarm_home(home) / "worktrees"


def stale_meta_dir(home: Path | None = None) -> Path:
    return state_dir(home) / "worktrees_meta"


def status_file(home: Path | None = None) -> Path:
    """Stable JSON status feed (per feature #14 — mind page subscribers)."""
    return state_dir(home) / "status.json"


__all__ = [
    "inboxes_dir",
    "kanban_path",
    "pull_requests_dir",
    "stale_meta_dir",
    "state_dir",
    "status_file",
    "swarm_home",
    "worktrees_dir",
]
