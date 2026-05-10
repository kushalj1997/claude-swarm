"""Per-task git worktree manager + JSON pull-request envelopes.

Each builder head that edits code gets its own ``<worktree_dir>/swarm-<task>``
directory created via ``git worktree add``. Workers commit inside the
worktree, then submit a "pull request" by writing a JSON envelope to
``<state>/pull_requests/<task-id>.json``. A merger head reads the envelope
and cherry-picks the commits onto the base branch.

We use JSON envelopes (not real GitHub PRs) because the swarm is local-first
and the operator is the human reviewer of last resort. Real PRs are still
possible — just wrap :class:`WorktreeManager` and forward to ``gh``.

Garbage collection:
    * :meth:`WorktreeManager.cleanup_after_merge` removes the worktree +
      branch right after a successful merge.
    * :meth:`WorktreeManager.mark_stale` stamps a marker for a failed /
      cancelled task; nightly :meth:`gc_stale` sweeps anything older than
      :data:`STALE_AGE_SECONDS`.
"""
from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._paths import (
    pull_requests_dir as default_prs_dir,
)
from ._paths import (
    stale_meta_dir as default_stale_meta_dir,
)
from ._paths import (
    worktrees_dir as default_worktrees_dir,
)

log = logging.getLogger(__name__)

#: Tasks marked stale at least this long ago are eligible for nightly GC.
STALE_AGE_SECONDS: int = 24 * 3600


def _git(*args: str, cwd: Path) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


@dataclass
class PullRequest:
    """JSON envelope describing a worker's branch ready to merge."""

    task_id: str
    branch: str
    base_branch: str
    worktree_path: str
    head_sha: str
    base_sha: str
    files_changed: list[str]
    diff_stat: str
    title: str
    body: str
    submitted_at: float = field(default_factory=time.time)
    status: str = "open"
    merged_at: float | None = None
    merged_into_sha: str | None = None
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


class WorktreeManager:
    """Create + tear down per-task git worktrees and PR JSON envelopes."""

    def __init__(
        self,
        *,
        repo_root: Path,
        worktrees_dir: Path | None = None,
        prs_dir: Path | None = None,
        stale_meta_dir: Path | None = None,
        base_branch: str | None = None,
        branch_prefix: str = "swarm",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.worktrees_dir = Path(worktrees_dir) if worktrees_dir else default_worktrees_dir()
        self.prs_dir = Path(prs_dir) if prs_dir else default_prs_dir()
        self.stale_meta_dir = (
            Path(stale_meta_dir) if stale_meta_dir else default_stale_meta_dir()
        )
        self.branch_prefix = branch_prefix
        for d in (self.worktrees_dir, self.prs_dir, self.stale_meta_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.base_branch = base_branch or _git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo_root
        )

    # ----- worktree lifecycle ----------------------------------------

    def _branch(self, task_id: str) -> str:
        return f"{self.branch_prefix}/{task_id}"

    def _worktree_path(self, task_id: str) -> Path:
        return self.worktrees_dir / f"{self.branch_prefix}-{task_id}"

    def create_worktree(self, task_id: str) -> tuple[Path, str]:
        """Create the worktree on a new branch off the base branch."""
        branch = self._branch(task_id)
        path = self._worktree_path(task_id)
        if path.exists():
            return path, branch
        _git(
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            self.base_branch,
            cwd=self.repo_root,
        )
        return path, branch

    def submit_pr(
        self,
        *,
        task_id: str,
        worktree_path: Path,
        title: str,
        body: str,
    ) -> PullRequest:
        """Snapshot worktree state into a PR JSON envelope."""
        branch = self._branch(task_id)
        head = _git("rev-parse", "HEAD", cwd=worktree_path)
        base = _git("merge-base", branch, self.base_branch, cwd=worktree_path)
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", f"{base}..{head}"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        files = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..{head}"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        pr = PullRequest(
            task_id=task_id,
            branch=branch,
            base_branch=self.base_branch,
            worktree_path=str(worktree_path),
            head_sha=head,
            base_sha=base,
            files_changed=[f for f in files if f.strip()],
            diff_stat=diff_stat.strip(),
            title=title,
            body=body,
        )
        path = self.prs_dir / f"{task_id}.json"
        path.write_text(json.dumps(pr.to_dict(), indent=2), encoding="utf-8")
        return pr

    def list_open_prs(self) -> list[PullRequest]:
        out: list[PullRequest] = []
        for p in sorted(self.prs_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("status") == "open":
                out.append(PullRequest(**d))
        return out

    def merge_pr(self, task_id: str) -> PullRequest:
        """Cherry-pick the worker's commits onto the base branch."""
        path = self.prs_dir / f"{task_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"no PR envelope for {task_id}")
        pr = PullRequest(**json.loads(path.read_text(encoding="utf-8")))
        if pr.status != "open":
            return pr
        commits = subprocess.run(
            ["git", "rev-list", "--reverse", f"{pr.base_sha}..{pr.head_sha}"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if not commits:
            pr.status = "rejected"
            pr.rejection_reason = "no commits on the worker branch"
            path.write_text(json.dumps(pr.to_dict(), indent=2), encoding="utf-8")
            return pr
        try:
            for c in commits:
                _git("cherry-pick", c, cwd=self.repo_root)
            pr.status = "merged"
            pr.merged_at = time.time()
            pr.merged_into_sha = _git("rev-parse", "HEAD", cwd=self.repo_root)
            self.cleanup_after_merge(pr.task_id)
        except subprocess.CalledProcessError as exc:
            subprocess.run(
                ["git", "cherry-pick", "--abort"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
            )
            pr.status = "rejected"
            pr.rejection_reason = f"cherry-pick failed: {exc.stderr or exc.stdout}"
        path.write_text(json.dumps(pr.to_dict(), indent=2), encoding="utf-8")
        return pr

    def remove_worktree(self, task_id: str, *, force: bool = False) -> None:
        path = self._worktree_path(task_id)
        if not path.exists():
            return
        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        try:
            _git(*args, cwd=self.repo_root)
        except subprocess.CalledProcessError:
            subprocess.run(["rm", "-rf", str(path)], check=False)
            with contextlib.suppress(subprocess.CalledProcessError):
                _git("worktree", "prune", cwd=self.repo_root)

    # ----- GC --------------------------------------------------------

    def cleanup_after_merge(self, task_id: str) -> bool:
        """Remove worktree + delete branch after a clean merge."""
        path = self._worktree_path(task_id)
        branch = self._branch(task_id)
        cleaned = False
        if path.exists():
            try:
                _git("worktree", "remove", "--force", str(path), cwd=self.repo_root)
                cleaned = True
            except subprocess.CalledProcessError:
                subprocess.run(["rm", "-rf", str(path)], check=False)
                try:
                    _git("worktree", "prune", cwd=self.repo_root)
                    cleaned = True
                except subprocess.CalledProcessError:
                    pass
        with contextlib.suppress(subprocess.CalledProcessError):
            _git("branch", "-D", branch, cwd=self.repo_root)
        marker = self.stale_meta_dir / f"{task_id}.json"
        if marker.exists():
            with contextlib.suppress(OSError):
                marker.unlink()
        return cleaned

    def mark_stale(self, task_id: str, *, reason: str = "failed") -> None:
        """Stamp the worktree as eligible for nightly GC."""
        marker = self.stale_meta_dir / f"{task_id}.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "reason": reason,
                    "marked_at": time.time(),
                }
            ),
            encoding="utf-8",
        )

    def gc_stale(
        self,
        *,
        max_age_s: int = STALE_AGE_SECONDS,
        now: float | None = None,
    ) -> list[str]:
        """Sweep stale worktrees older than ``max_age_s``."""
        cutoff = (now if now is not None else time.time()) - max_age_s
        removed: list[str] = []
        if not self.stale_meta_dir.exists():
            return removed
        for marker in sorted(self.stale_meta_dir.glob("*.json")):
            try:
                meta = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if float(meta.get("marked_at", 0)) > cutoff:
                continue
            task_id = meta.get("task_id") or marker.stem
            try:
                self.cleanup_after_merge(task_id)
                removed.append(task_id)
            except Exception:
                log.exception("gc_stale failed for %s", task_id)
        return removed


__all__ = [
    "STALE_AGE_SECONDS",
    "PullRequest",
    "WorktreeManager",
]
