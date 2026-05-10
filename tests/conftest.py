"""Shared fixtures."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_swarm._paths import (
    inboxes_dir,
    kanban_path,
    pull_requests_dir,
    state_dir,
    worktrees_dir,
)
from claude_swarm.kanban import Kanban


@pytest.fixture
def swarm_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated swarm home for each test."""
    tmp = Path(tempfile.mkdtemp(prefix="claude-swarm-test-"))
    monkeypatch.setenv("CLAUDE_SWARM_HOME", str(tmp))
    state_dir(None).mkdir(parents=True, exist_ok=True)
    inboxes_dir(None).mkdir(parents=True, exist_ok=True)
    pull_requests_dir(None).mkdir(parents=True, exist_ok=True)
    worktrees_dir(None).mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def kanban(swarm_root: Path) -> Kanban:
    return Kanban(kanban_path(None))


@pytest.fixture
def git_repo() -> Iterator[Path]:
    """A throwaway git repo for worktree + merge tests."""
    tmp = Path(tempfile.mkdtemp(prefix="claude-swarm-repo-"))
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp, check=True)
    (tmp / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp, check=True, capture_output=True
    )
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = ["git_repo", "kanban", "swarm_root"]
