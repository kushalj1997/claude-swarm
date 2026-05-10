"""Abort-marker contract tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_swarm.abort import (
    AbortMarker,
    AbortRequested,
    abort_marker_path,
    check_abort,
    raise_if_aborted,
)


def test_marker_path_is_canonical(tmp_path: Path) -> None:
    p = abort_marker_path(tmp_path, "alice")
    assert p == tmp_path / ".claude" / "abort-alice"


def test_marker_set_and_clear(tmp_path: Path) -> None:
    m = AbortMarker(worktree_root=tmp_path, teammate="bob")
    assert not m.is_set()
    m.set(reason="operator")
    assert m.is_set()
    assert m.path.read_text(encoding="utf-8") == "operator"
    m.clear()
    assert not m.is_set()
    # idempotent
    m.clear()


def test_raise_if_set_raises(tmp_path: Path) -> None:
    m = AbortMarker(worktree_root=tmp_path, teammate="carol")
    m.set()
    with pytest.raises(AbortRequested):
        m.raise_if_set()


def test_check_abort_helper(tmp_path: Path) -> None:
    assert not check_abort(tmp_path, "dan")
    AbortMarker(worktree_root=tmp_path, teammate="dan").set()
    assert check_abort(tmp_path, "dan")


def test_raise_if_aborted_helper(tmp_path: Path) -> None:
    raise_if_aborted(tmp_path, "eve")  # no marker, no raise
    AbortMarker(worktree_root=tmp_path, teammate="eve").set()
    with pytest.raises(AbortRequested):
        raise_if_aborted(tmp_path, "eve")
