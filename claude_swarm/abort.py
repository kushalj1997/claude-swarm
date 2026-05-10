"""Abort-marker contract.

Plugins, the supervisor, or a human operator can ask a long-running teammate
to stop cleanly by writing an empty file at::

    <worktree>/.claude/abort-<teammate-name>

The teammate is expected to poll for that file at phase boundaries and, if
present, commit any work-in-progress, push, and exit cleanly. This module
encapsulates the contract so every head can reuse it the same way.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path


class AbortRequested(RuntimeError):
    """Raised when an abort marker is observed.

    The supervisor and individual heads should catch this between phases,
    commit any pending work, then re-raise (or exit cleanly).
    """

    def __init__(self, marker: Path, teammate: str) -> None:
        super().__init__(f"abort marker present for {teammate!r} at {marker}")
        self.marker = marker
        self.teammate = teammate


def abort_marker_path(worktree_root: Path, teammate: str) -> Path:
    """Return the canonical abort-marker path for ``teammate``.

    The directory layout is ``<worktree_root>/.claude/abort-<teammate>`` —
    the same convention Claude Code's plugin contract documents. We keep the
    function pure so callers can both check and create the marker.
    """
    return Path(worktree_root) / ".claude" / f"abort-{teammate}"


def check_abort(worktree_root: Path, teammate: str) -> bool:
    """Return ``True`` if an abort marker exists for ``teammate``."""
    return abort_marker_path(worktree_root, teammate).exists()


def raise_if_aborted(worktree_root: Path, teammate: str) -> None:
    """Raise :class:`AbortRequested` if an abort marker is present."""
    marker = abort_marker_path(worktree_root, teammate)
    if marker.exists():
        raise AbortRequested(marker, teammate)


@dataclass(frozen=True)
class AbortMarker:
    """A small helper bundling a teammate name with its worktree root.

    Useful when a head is constructed once and then polls the marker many
    times during a long-running loop.
    """

    worktree_root: Path
    teammate: str

    @property
    def path(self) -> Path:
        return abort_marker_path(self.worktree_root, self.teammate)

    def is_set(self) -> bool:
        return self.path.exists()

    def set(self, *, reason: str = "operator") -> None:
        """Create the marker. Idempotent."""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(reason, encoding="utf-8")

    def clear(self) -> None:
        """Remove the marker if present."""
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def raise_if_set(self) -> None:
        if self.is_set():
            raise AbortRequested(self.path, self.teammate)


__all__ = [
    "AbortMarker",
    "AbortRequested",
    "abort_marker_path",
    "check_abort",
    "raise_if_aborted",
]
