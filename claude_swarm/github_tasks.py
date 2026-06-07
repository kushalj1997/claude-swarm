"""GitHubWorkSource — pull tasks from GitHub Issues / Projects into the kanban.

This is the canonical task intake for the autonomous swarm, replacing the
superseded postgres kanban source.  It implements the :class:`WorkSource`
protocol from :mod:`claude_swarm.perpetual` so it drops in wherever
``NullWorkSource`` is used today.

Design goals
------------
* Config-flagged — **off by default**.  Set ``CLAUDE_SWARM_GITHUB_INTAKE=1``
  or pass ``enabled=True`` to activate.  Safe in all existing CI environments.
* Idempotent — each GitHub issue is filed at most once per swarm home
  (tracked in a lightweight JSON dedup file under ``.swarm/gh-seen.json``).
* Dependency-light — uses the ``gh`` CLI (already in PATH on any dev machine
  with GitHub auth) rather than a PyGitHub dep.  Falls back gracefully when
  ``gh`` is unavailable.
* Configurable label filter — only issues bearing the ``swarm-task`` label
  (or the value of ``CLAUDE_SWARM_GITHUB_LABEL``) are ingested.  Operators
  add the label to hand work to the swarm; removing it (or closing the issue)
  signals "done".

Wire it into a perpetual loop
------------------------------
::

    from claude_swarm.github_tasks import GitHubWorkSource
    from claude_swarm.perpetual import run_perpetual_team

    run_perpetual_team(
        count=2,
        kanban=kb,
        work_source=GitHubWorkSource(repo="org/repo"),
    )

Or activate via env::

    CLAUDE_SWARM_GITHUB_INTAKE=1 claude-swarm perpetual --count=2

Environment variables
---------------------
CLAUDE_SWARM_GITHUB_INTAKE
    Set to ``1`` to enable intake.  Off by default.
CLAUDE_SWARM_GITHUB_REPO
    ``owner/repo`` slug.  If unset, ``gh`` auto-detects from the current
    working directory's git remote.
CLAUDE_SWARM_GITHUB_LABEL
    Issue label to filter.  Defaults to ``swarm-task``.
CLAUDE_SWARM_GITHUB_PROJECT
    Project number (integer) for the "deep-ai" GitHub Project #5.
    When set, only issues that belong to this project board are ingested
    (in addition to the label filter).
CLAUDE_SWARM_GITHUB_POLL_INTERVAL_S
    Minimum seconds between polls.  Default 60.
CLAUDE_SWARM_GITHUB_HEAD
    Required head to assign ingested tasks to.  Default ``"builder"``.
CLAUDE_SWARM_GITHUB_PRIORITY
    Default task priority (1–10, lower = higher priority).  Default ``5``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._paths import state_dir
from .kanban import Kanban, Task

log = logging.getLogger(__name__)

#: Label applied to GitHub issues that should be pulled into the swarm kanban.
DEFAULT_LABEL: str = "swarm-task"
#: Default poll cadence in seconds.
DEFAULT_POLL_INTERVAL_S: float = 60.0
#: State file that tracks already-ingested issue ids.
_SEEN_FILE: str = "gh-seen.json"


def _run_gh(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run ``gh`` with the given arguments; return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", "gh CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return -1, "", "gh CLI timed out"


def _gh_available() -> bool:
    """Return ``True`` if the ``gh`` binary is reachable and authenticated."""
    rc, _, _ = _run_gh("auth", "status")
    return rc == 0


def _fetch_issues(
    repo: str | None,
    label: str,
    project: int | None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch open issues from GitHub matching *label*.

    Uses ``gh issue list --json`` for a structured payload; no screen-scraping.
    Returns a list of issue dicts (keys: ``number``, ``title``, ``body``,
    ``url``, ``labels``).  Returns ``[]`` on any error so the caller is safe
    to iterate.
    """
    cmd: list[str] = ["issue", "list", "--state", "open", "--label", label, "--limit", str(limit), "--json", "number,title,body,url,labels,projectItems"]
    if repo:
        cmd = ["issue", "list", "--repo", repo, "--state", "open", "--label", label, "--limit", str(limit), "--json", "number,title,body,url,labels,projectItems"]

    rc, stdout, stderr = _run_gh(*cmd)
    if rc != 0:
        log.warning("gh issue list failed (rc=%d): %s", rc, stderr.strip())
        return []
    try:
        issues: list[dict[str, Any]] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.warning("gh issue list JSON decode error: %s", exc)
        return []

    if project is not None:
        # Filter to issues that appear in the specified project number.
        def _in_project(issue: dict[str, Any]) -> bool:
            items: list[dict[str, Any]] = issue.get("projectItems", []) or []
            return any(
                str(item.get("project", {}).get("number")) == str(project)
                for item in items
            )
        issues = [i for i in issues if _in_project(i)]

    return issues


def _load_seen(path: Path) -> set[int]:
    """Load the set of already-ingested issue numbers from *path*."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(int(n) for n in data.get("seen", []))
    except (OSError, ValueError, KeyError):
        return set()


def _save_seen(path: Path, seen: set[int]) -> None:
    """Atomically persist *seen* to *path*."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"seen": sorted(seen)}, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        import contextlib
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _issue_to_task(issue: dict[str, Any], *, required_head: str, priority: int) -> Task:
    """Convert a GitHub issue dict into a :class:`~claude_swarm.kanban.Task`."""
    number = issue["number"]
    title = issue.get("title") or f"GitHub issue #{number}"
    body = issue.get("body") or ""
    url = issue.get("url") or ""

    prompt = f"""GitHub Issue #{number}: {title}

URL: {url}

---

{body}
""".strip()

    return Task(
        type="github-issue",
        title=f"GH#{number}: {title}",
        prompt=prompt,
        priority=priority,
        required_head=required_head,
        metadata={"github_issue_number": number, "github_url": url},
    )


@dataclass
class GitHubWorkSource:
    """Pull GitHub issues into the swarm kanban as tasks.

    Parameters
    ----------
    repo:
        ``owner/repo`` slug.  ``None`` means let ``gh`` auto-detect from the
        current working directory (works when cwd is a git checkout).
    label:
        Issue label to filter.  Defaults to :data:`DEFAULT_LABEL` (``"swarm-task"``).
    project:
        GitHub Project number to restrict to (e.g. 5 for "deep-ai" #5).
        ``None`` disables the project filter — all matching-label issues.
    enabled:
        If ``False`` (the default when no env var), no issues are fetched.
        Set ``CLAUDE_SWARM_GITHUB_INTAKE=1`` or pass ``enabled=True``.
    required_head:
        The head field assigned to ingested tasks.
    priority:
        Default priority for ingested tasks (1=highest, 10=lowest).
    poll_interval_s:
        Minimum seconds between polls.  Calls within the window return ``[]``
        immediately without hitting GitHub.
    home:
        Swarm home directory used to locate the dedup file.  Defaults to the
        directory returned by :func:`~claude_swarm._paths.state_dir`.
    """

    repo: str | None = None
    label: str = field(default_factory=lambda: os.environ.get("CLAUDE_SWARM_GITHUB_LABEL", DEFAULT_LABEL))
    project: int | None = field(default=None)
    enabled: bool = field(default_factory=lambda: bool(os.environ.get("CLAUDE_SWARM_GITHUB_INTAKE")))
    required_head: str = field(default_factory=lambda: os.environ.get("CLAUDE_SWARM_GITHUB_HEAD", "builder"))
    priority: int = field(default_factory=lambda: int(os.environ.get("CLAUDE_SWARM_GITHUB_PRIORITY", "5")))
    poll_interval_s: float = field(default_factory=lambda: float(os.environ.get("CLAUDE_SWARM_GITHUB_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S))))
    home: Path | None = None

    def __post_init__(self) -> None:
        # Resolve repo from env if not passed at construction time.
        if self.repo is None:
            self.repo = os.environ.get("CLAUDE_SWARM_GITHUB_REPO") or None
        # Resolve project from env if not passed at construction time.
        if self.project is None:
            raw = os.environ.get("CLAUDE_SWARM_GITHUB_PROJECT")
            if raw:
                try:
                    self.project = int(raw)
                except ValueError:
                    log.warning("CLAUDE_SWARM_GITHUB_PROJECT=%r is not an integer; ignoring", raw)
        self._last_poll: float = 0.0
        self._seen_path: Path = state_dir(self.home) / _SEEN_FILE

    # WorkSource protocol ------------------------------------------------

    def generate(self, kanban: Kanban) -> Sequence[str]:
        """Fetch open issues and file new ones into *kanban*.

        Returns the ids of tasks filed in this call (possibly empty).
        No-ops when:
        - ``enabled=False`` (the default)
        - ``gh`` CLI is unavailable or unauthenticated
        - polled within ``poll_interval_s`` seconds
        """
        if not self.enabled:
            return ()
        now = time.time()
        if now - self._last_poll < self.poll_interval_s:
            return ()
        self._last_poll = now

        if not _gh_available():
            log.warning("GitHubWorkSource: gh CLI unavailable or not authenticated; skipping intake")
            return ()

        issues = _fetch_issues(
            repo=self.repo,
            label=self.label,
            project=self.project,
        )
        if not issues:
            return ()

        seen = _load_seen(self._seen_path)
        filed: list[str] = []

        for issue in issues:
            number = issue.get("number")
            if number is None:
                continue
            if number in seen:
                continue
            task = _issue_to_task(issue, required_head=self.required_head, priority=self.priority)
            try:
                kanban.submit(task)
                seen.add(number)
                filed.append(task.id)
                log.info(
                    "GitHubWorkSource: filed task %s for GH#%d (%s)",
                    task.id,
                    number,
                    task.title,
                )
            except Exception as exc:
                log.warning("GitHubWorkSource: failed to file task for GH#%d: %s", number, exc)

        if filed:
            _save_seen(self._seen_path, seen)

        return filed

    # Convenience helpers ------------------------------------------------

    def reset_seen(self) -> None:
        """Clear the dedup file so previously-ingested issues can be re-filed.

        Useful in tests or when re-running the swarm against a fresh kanban.
        """
        if self._seen_path.exists():
            self._seen_path.unlink()

    def peek_issues(self) -> list[dict[str, Any]]:
        """Fetch and return raw issue dicts without filing into the kanban.

        Useful for operator inspection: ``source.peek_issues()`` shows what
        would be ingested on the next ``generate()`` call.
        """
        return _fetch_issues(repo=self.repo, label=self.label, project=self.project)


__all__ = [
    "DEFAULT_LABEL",
    "GitHubWorkSource",
]
