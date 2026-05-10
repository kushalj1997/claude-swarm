"""Auto-merge pipeline for ready pull requests.

Features (subset of the recommended first-PR scope):
    * file-overlap rejection — refuse parallel merges that touch the same file
    * topological ordering — merge in an order that minimises conflicts
    * test gate — run a configurable command before pushing each PR
    * worktree GC — clean up after every successful merge

Test gate is optional; pass ``test_command=None`` to skip. The default policy
is to abort the batch on the first test failure (deterministic, easy to
explain) — change ``stop_on_failure=False`` to keep going.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .worktree import PullRequest, WorktreeManager

log = logging.getLogger(__name__)


@dataclass
class MergeReport:
    """Summary of a single :func:`run_pipeline` invocation."""

    merged: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    test_failures: dict[str, str] = field(default_factory=dict)


def file_overlap(prs: list[PullRequest]) -> list[tuple[str, str, list[str]]]:
    """Return a list of (task_a, task_b, overlapping_files) triples."""
    overlaps: list[tuple[str, str, list[str]]] = []
    for i, a in enumerate(prs):
        for b in prs[i + 1 :]:
            common = sorted(set(a.files_changed) & set(b.files_changed))
            if common:
                overlaps.append((a.task_id, b.task_id, common))
    return overlaps


def topological_order(prs: list[PullRequest]) -> list[PullRequest]:
    """Order PRs to minimise conflict probability.

    Heuristic (cheap + good enough): merge smallest diffs first. Smaller
    surfaces are less likely to bit-rot and let later merges rebase cleanly.
    Ties broken by submitted_at (FIFO).
    """
    return sorted(prs, key=lambda p: (len(p.files_changed), p.submitted_at))


def _run_test_command(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    """Run ``cmd`` in ``cwd`` and return (exit_code, tail_output)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, f"test runner error: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-2000:]


def run_pipeline(
    manager: WorktreeManager,
    *,
    test_command: list[str] | None = None,
    test_timeout_s: int = 900,
    reject_overlap: bool = True,
    stop_on_failure: bool = True,
) -> MergeReport:
    """Merge every open PR in the optimal order, gating on tests.

    Args:
        manager: the worktree manager that owns the PR envelopes
        test_command: command to run after each merge (e.g. ``["pytest", "-x"]``)
        test_timeout_s: per-PR test timeout
        reject_overlap: if True, refuse to batch-merge PRs that touch the
            same file. Caller is expected to retry serially.
        stop_on_failure: abort the batch on the first test failure.
    """
    report = MergeReport()
    prs = manager.list_open_prs()
    if not prs:
        return report

    if reject_overlap:
        overlaps = file_overlap(prs)
        if overlaps:
            for a, b, files in overlaps:
                msg = f"file overlap with {b}: {files[:3]}"
                report.rejected[a] = msg
                log.warning("rejecting %s — %s", a, msg)
            return report

    for pr in topological_order(prs):
        merged = manager.merge_pr(pr.task_id)
        if merged.status != "merged":
            report.rejected[pr.task_id] = merged.rejection_reason or "rejected"
            if stop_on_failure:
                break
            continue
        if test_command is None:
            report.merged.append(pr.task_id)
            continue
        rc, tail = _run_test_command(test_command, manager.repo_root, test_timeout_s)
        if rc == 0:
            report.merged.append(pr.task_id)
            continue
        # Tests failed: revert the just-merged commits to keep base clean.
        try:
            commits = subprocess.run(
                ["git", "rev-list", "--reverse", f"{pr.base_sha}..{pr.head_sha}"],
                cwd=manager.repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            for c in reversed(commits):
                subprocess.run(
                    ["git", "revert", "--no-edit", c],
                    cwd=manager.repo_root,
                    check=False,
                    capture_output=True,
                )
        except subprocess.CalledProcessError:
            log.exception("revert failed for %s", pr.task_id)
        report.test_failures[pr.task_id] = tail
        if stop_on_failure:
            break

    return report


__all__ = ["MergeReport", "file_overlap", "run_pipeline", "topological_order"]
