"""Unit tests for GitHubWorkSource — GitHub issue intake for the swarm kanban."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swarm.github_tasks import (
    DEFAULT_LABEL,
    GitHubWorkSource,
    _fetch_issues,
    _gh_available,
    _issue_to_task,
    _load_seen,
    _save_seen,
)
from claude_swarm.kanban import Kanban, TaskStatus


# ---------- helpers --------------------------------------------------------

@pytest.fixture()
def tmp_kanban(tmp_path: Path) -> Kanban:
    return Kanban(tmp_path / "kanban.db")


@pytest.fixture()
def tmp_home(tmp_path: Path) -> Path:
    return tmp_path


# ---------- _load_seen / _save_seen ----------------------------------------

class TestSeenPersistence:
    def test_load_empty_when_file_missing(self, tmp_path: Path) -> None:
        seen = _load_seen(tmp_path / "gh-seen.json")
        assert seen == set()

    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "gh-seen.json"
        _save_seen(path, {1, 2, 42})
        loaded = _load_seen(path)
        assert loaded == {1, 2, 42}

    def test_load_corrupt_file_returns_empty_set(self, tmp_path: Path) -> None:
        path = tmp_path / "gh-seen.json"
        path.write_text("not-json", encoding="utf-8")
        assert _load_seen(path) == set()

    def test_save_is_atomic(self, tmp_path: Path) -> None:
        """No partial writes — file either contains valid JSON or nothing."""
        path = tmp_path / "gh-seen.json"
        _save_seen(path, {99})
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["seen"] == [99]


# ---------- _issue_to_task -------------------------------------------------

class TestIssueToTask:
    def test_basic_conversion(self) -> None:
        issue = {"number": 7, "title": "Fix the bug", "body": "details here", "url": "https://github.com/x/y/issues/7"}
        task = _issue_to_task(issue, required_head="builder", priority=3)
        assert task.type == "github-issue"
        assert "GH#7" in task.title
        assert "Fix the bug" in task.title
        assert "#7" in task.prompt
        assert "details here" in task.prompt
        assert task.priority == 3
        assert task.required_head == "builder"
        assert task.metadata["github_issue_number"] == 7

    def test_missing_body_does_not_crash(self) -> None:
        issue = {"number": 1, "title": "No body", "body": None, "url": "https://github.com/x/y/issues/1"}
        task = _issue_to_task(issue, required_head="builder", priority=5)
        assert task.prompt  # should not be empty/None

    def test_missing_title_uses_fallback(self) -> None:
        issue = {"number": 3, "title": None, "body": "body", "url": "https://github.com/x/y/issues/3"}
        task = _issue_to_task(issue, required_head="builder", priority=5)
        assert "3" in task.title


# ---------- GitHubWorkSource.generate — disabled by default ----------------

class TestGitHubWorkSourceDisabledDefault:
    def test_generate_returns_empty_when_disabled(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        source = GitHubWorkSource(enabled=False, home=tmp_home)
        result = source.generate(tmp_kanban)
        assert list(result) == []

    def test_enabled_false_by_default_without_env(self, tmp_kanban: Kanban, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_GITHUB_INTAKE", raising=False)
        source = GitHubWorkSource(home=tmp_home)
        assert not source.enabled

    def test_enabled_true_via_env(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_INTAKE", "1")
        source = GitHubWorkSource(home=tmp_home)
        assert source.enabled

    def test_default_label_matches_constant(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SWARM_GITHUB_LABEL", raising=False)
        source = GitHubWorkSource(home=tmp_home)
        assert source.label == DEFAULT_LABEL


# ---------- GitHubWorkSource.generate — poll-interval guard ----------------

class TestPollIntervalGuard:
    def test_second_call_within_interval_skips_fetch(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        fetch_calls: list[int] = []

        def _fake_fetch(*_a: object, **_kw: object) -> list[dict]:
            fetch_calls.append(1)
            return []

        with patch("claude_swarm.github_tasks._fetch_issues", _fake_fetch), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            source = GitHubWorkSource(enabled=True, poll_interval_s=3600.0, home=tmp_home)
            source.generate(tmp_kanban)  # first call: polls
            source.generate(tmp_kanban)  # second call within 3600s: skips

        assert len(fetch_calls) == 1  # only one real fetch


# ---------- GitHubWorkSource.generate — happy path -------------------------

class TestGitHubWorkSourceGenerate:
    _ISSUES = [
        {"number": 10, "title": "Add feature X", "body": "body text", "url": "https://github.com/o/r/issues/10", "labels": [{"name": "swarm-task"}], "projectItems": []},
        {"number": 20, "title": "Fix bug Y", "body": "another body", "url": "https://github.com/o/r/issues/20", "labels": [{"name": "swarm-task"}], "projectItems": []},
    ]

    def test_issues_filed_into_kanban(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        with patch("claude_swarm.github_tasks._fetch_issues", return_value=self._ISSUES), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            source = GitHubWorkSource(enabled=True, poll_interval_s=0.0, home=tmp_home)
            filed = source.generate(tmp_kanban)

        assert len(filed) == 2
        tasks = tmp_kanban.list_tasks()
        assert len(tasks) == 2
        numbers = {t.metadata.get("github_issue_number") for t in tasks}
        assert numbers == {10, 20}

    def test_already_seen_issues_not_refiled(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        with patch("claude_swarm.github_tasks._fetch_issues", return_value=self._ISSUES), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            source = GitHubWorkSource(enabled=True, poll_interval_s=0.0, home=tmp_home)
            first = list(source.generate(tmp_kanban))
            second = list(source.generate(tmp_kanban))

        assert len(first) == 2
        assert len(second) == 0
        assert len(tmp_kanban.list_tasks()) == 2

    def test_partially_seen_only_new_filed(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        with patch("claude_swarm.github_tasks._fetch_issues", return_value=self._ISSUES[:1]), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            source = GitHubWorkSource(enabled=True, poll_interval_s=0.0, home=tmp_home)
            source.generate(tmp_kanban)

        with patch("claude_swarm.github_tasks._fetch_issues", return_value=self._ISSUES), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            filed = list(source.generate(tmp_kanban))

        assert len(filed) == 1  # only issue #20 is new
        assert len(tmp_kanban.list_tasks()) == 2

    def test_gh_unavailable_returns_empty(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        with patch("claude_swarm.github_tasks._gh_available", return_value=False):
            source = GitHubWorkSource(enabled=True, poll_interval_s=0.0, home=tmp_home)
            filed = source.generate(tmp_kanban)
        assert list(filed) == []

    def test_filed_tasks_have_correct_type(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        with patch("claude_swarm.github_tasks._fetch_issues", return_value=self._ISSUES[:1]), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            source = GitHubWorkSource(enabled=True, poll_interval_s=0.0, home=tmp_home)
            source.generate(tmp_kanban)

        tasks = tmp_kanban.list_tasks()
        assert all(t.type == "github-issue" for t in tasks)
        assert all(t.status == TaskStatus.PENDING for t in tasks)


# ---------- GitHubWorkSource config from env vars --------------------------

class TestEnvVarConfig:
    def test_repo_from_env(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_REPO", "myorg/myrepo")
        source = GitHubWorkSource(home=tmp_home)
        assert source.repo == "myorg/myrepo"

    def test_label_from_env(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_LABEL", "agent-work")
        source = GitHubWorkSource(home=tmp_home)
        assert source.label == "agent-work"

    def test_project_from_env(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_PROJECT", "5")
        source = GitHubWorkSource(home=tmp_home)
        assert source.project == 5

    def test_head_from_env(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_HEAD", "scanner")
        source = GitHubWorkSource(home=tmp_home)
        assert source.required_head == "scanner"

    def test_priority_from_env(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_PRIORITY", "2")
        source = GitHubWorkSource(home=tmp_home)
        assert source.priority == 2

    def test_invalid_project_env_ignored(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SWARM_GITHUB_PROJECT", "not-a-number")
        source = GitHubWorkSource(home=tmp_home)
        assert source.project is None


# ---------- reset_seen / peek_issues ---------------------------------------

class TestHelpers:
    def test_reset_seen_clears_dedup(self, tmp_kanban: Kanban, tmp_home: Path) -> None:
        issues = [
            {"number": 5, "title": "T", "body": "B", "url": "https://github.com/x/y/issues/5", "labels": [], "projectItems": []},
        ]
        with patch("claude_swarm.github_tasks._fetch_issues", return_value=issues), \
             patch("claude_swarm.github_tasks._gh_available", return_value=True):
            source = GitHubWorkSource(enabled=True, poll_interval_s=0.0, home=tmp_home)
            source.generate(tmp_kanban)
            source.reset_seen()
            filed = list(source.generate(tmp_kanban))

        assert len(filed) == 1  # re-filed after reset

    def test_peek_issues_calls_fetch(self, tmp_home: Path) -> None:
        issues = [{"number": 1, "title": "T", "body": "B", "url": "url", "labels": [], "projectItems": []}]
        with patch("claude_swarm.github_tasks._fetch_issues", return_value=issues) as mock_fetch:
            source = GitHubWorkSource(home=tmp_home)
            result = source.peek_issues()
        assert result == issues
        mock_fetch.assert_called_once()
