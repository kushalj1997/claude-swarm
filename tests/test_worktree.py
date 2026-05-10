"""WorktreeManager + merge pipeline tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claude_swarm.merge_pipeline import (
    file_overlap,
    run_pipeline,
    topological_order,
)
from claude_swarm.worktree import (
    STALE_AGE_SECONDS,
    PullRequest,
    WorktreeManager,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_in_worktree(worktree: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=worktree,
        check=True,
        capture_output=True,
    )


def test_create_worktree_then_submit_pr(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    path, branch = mgr.create_worktree("t1")
    assert path.exists()
    _write(path / "feature.txt", "hello\n")
    _commit_in_worktree(path, "feat: t1")
    pr = mgr.submit_pr(
        task_id="t1", worktree_path=path, title="t1", body="body"
    )
    assert pr.status == "open"
    assert "feature.txt" in pr.files_changed
    assert pr.branch == branch


def test_merge_pr_cherry_picks_then_cleans_up(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    path, _ = mgr.create_worktree("t1")
    _write(path / "f.txt", "a\n")
    _commit_in_worktree(path, "feat: f")
    mgr.submit_pr(task_id="t1", worktree_path=path, title="t", body="b")
    merged = mgr.merge_pr("t1")
    assert merged.status == "merged"
    # base branch now has the file
    assert (git_repo / "f.txt").exists()
    # worktree was cleaned
    assert not path.exists()


def test_merge_pr_rejects_when_no_commits(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    path, _ = mgr.create_worktree("t1")
    # No new commits — the head sha == base sha, no commits to cherry-pick.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()
    pr = PullRequest(
        task_id="t1",
        branch="swarm/t1",
        base_branch=mgr.base_branch,
        worktree_path=str(path),
        head_sha=head,
        base_sha=head,
        files_changed=[],
        diff_stat="",
        title="empty",
        body="",
    )
    (mgr.prs_dir / "t1.json").write_text(
        json.dumps(pr.to_dict(), indent=2), encoding="utf-8"
    )
    after = mgr.merge_pr("t1")
    assert after.status == "rejected"
    assert "no commits" in (after.rejection_reason or "")


def test_gc_stale_sweeps_old_markers(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    mgr.create_worktree("ancient")
    mgr.mark_stale("ancient", reason="failed")
    # Pretend the marker is older than the threshold.
    marker = mgr.stale_meta_dir / "ancient.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["marked_at"] = 0.0
    marker.write_text(json.dumps(payload), encoding="utf-8")
    removed = mgr.gc_stale(max_age_s=STALE_AGE_SECONDS)
    assert "ancient" in removed


def test_topological_order_smaller_first() -> None:
    big = PullRequest(
        task_id="big",
        branch="b",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=["a", "b", "c"],
        diff_stat="",
        title="",
        body="",
        submitted_at=1.0,
    )
    small = PullRequest(
        task_id="small",
        branch="b",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=["a"],
        diff_stat="",
        title="",
        body="",
        submitted_at=2.0,
    )
    ordered = topological_order([big, small])
    assert ordered[0].task_id == "small"


def test_file_overlap_detection() -> None:
    a = PullRequest(
        task_id="a",
        branch="b",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=["x.py", "y.py"],
        diff_stat="",
        title="",
        body="",
    )
    b = PullRequest(
        task_id="b",
        branch="b",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=["y.py", "z.py"],
        diff_stat="",
        title="",
        body="",
    )
    overlaps = file_overlap([a, b])
    assert overlaps == [("a", "b", ["y.py"])]


def test_run_pipeline_merges_and_runs_test_command(
    git_repo: Path, tmp_path: Path
) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    path, _ = mgr.create_worktree("t1")
    _write(path / "feature_a.txt", "hi\n")
    _commit_in_worktree(path, "feat: a")
    mgr.submit_pr(task_id="t1", worktree_path=path, title="a", body="")
    report = run_pipeline(mgr, test_command=["true"])
    assert report.merged == ["t1"]
    assert not report.test_failures


def test_run_pipeline_reverts_on_test_failure(
    git_repo: Path, tmp_path: Path
) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    path, _ = mgr.create_worktree("t1")
    _write(path / "feature_a.txt", "hi\n")
    _commit_in_worktree(path, "feat: a")
    mgr.submit_pr(task_id="t1", worktree_path=path, title="a", body="")
    report = run_pipeline(mgr, test_command=["false"])
    assert "t1" in report.test_failures
    # The revert should have happened — file removed from base again.
    # (the cherry-pick added it; the revert removes it)
    assert not (git_repo / "feature_a.txt").exists()


def test_merge_pr_missing_envelope_raises(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    import pytest

    with pytest.raises(FileNotFoundError):
        mgr.merge_pr("missing")


def test_merge_pr_skips_already_merged(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    pr = PullRequest(
        task_id="t",
        branch="swarm/t",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=[],
        diff_stat="",
        title="",
        body="",
        status="merged",
    )
    (mgr.prs_dir / "t.json").write_text(
        json.dumps(pr.to_dict()), encoding="utf-8"
    )
    after = mgr.merge_pr("t")
    assert after.status == "merged"


def test_list_open_prs_skips_merged(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    open_pr = PullRequest(
        task_id="open",
        branch="b",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=[],
        diff_stat="",
        title="",
        body="",
        status="open",
    )
    closed_pr = PullRequest(
        task_id="closed",
        branch="b",
        base_branch="main",
        worktree_path="/x",
        head_sha="h",
        base_sha="b",
        files_changed=[],
        diff_stat="",
        title="",
        body="",
        status="merged",
    )
    (mgr.prs_dir / "open.json").write_text(json.dumps(open_pr.to_dict()), encoding="utf-8")
    (mgr.prs_dir / "closed.json").write_text(
        json.dumps(closed_pr.to_dict()), encoding="utf-8"
    )
    # Add a corrupt file too
    (mgr.prs_dir / "junk.json").write_text("not valid json", encoding="utf-8")
    open_prs = mgr.list_open_prs()
    assert [p.task_id for p in open_prs] == ["open"]


def test_remove_worktree_is_idempotent(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    # No worktree yet — should be a no-op.
    mgr.remove_worktree("nope")
    path, _ = mgr.create_worktree("t")
    mgr.remove_worktree("t")
    assert not path.exists()


def test_run_pipeline_rejects_on_overlap(git_repo: Path, tmp_path: Path) -> None:
    mgr = WorktreeManager(
        repo_root=git_repo,
        worktrees_dir=tmp_path / "worktrees",
        prs_dir=tmp_path / "prs",
        stale_meta_dir=tmp_path / "stale",
    )
    p1, _ = mgr.create_worktree("t1")
    _write(p1 / "shared.txt", "v1\n")
    _commit_in_worktree(p1, "feat: t1")
    mgr.submit_pr(task_id="t1", worktree_path=p1, title="t1", body="")

    p2, _ = mgr.create_worktree("t2")
    _write(p2 / "shared.txt", "v2\n")
    _commit_in_worktree(p2, "feat: t2")
    mgr.submit_pr(task_id="t2", worktree_path=p2, title="t2", body="")

    report = run_pipeline(mgr, test_command=None, reject_overlap=True)
    assert report.merged == []
    assert "t1" in report.rejected or "t2" in report.rejected
