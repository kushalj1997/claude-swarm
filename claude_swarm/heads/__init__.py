"""Heads — named subagent roles with role-specific prompts + tool allowlists.

Heads are stateless declarative descriptors: each head names a role
(Scanner, Reviewer, Builder, Merger, Test-Runner, Auditor), an allowed
tool list (read-only / read+write / git-only / etc.), and a base system
prompt. The supervisor consults the head when dispatching a task to pick
the right model and the right tool restrictions.

Designed so a downstream plugin can subclass + override ``system_prompt``
without rewriting the dispatch logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HeadKind(str, Enum):
    SCANNER = "scanner"
    REVIEWER = "reviewer"
    BUILDER = "builder"
    MERGER = "merger"
    TEST_RUNNER = "test-runner"
    AUDITOR = "auditor"


@dataclass(frozen=True)
class Head:
    """Declarative descriptor for a named head."""

    kind: HeadKind
    name: str
    allowed_tools: tuple[str, ...]
    system_prompt: str
    description: str = ""
    default_model: str = "claude-sonnet-4-6"
    extra: dict[str, str] = field(default_factory=dict)


_SCANNER_PROMPT = """\
You are a Scanner head in a claude-swarm. Your only job is to find work that
needs to happen and file new tasks describing it. You are read-only — you do
not edit code. When you spot something worth doing:

  1. Confirm by reading nearby files (no shell side-effects).
  2. File a single self-contained task via `kanban.submit` with a clear title,
     prompt, owned files (`files_owned`), and any blockers (`blocked_by`).
  3. Stop. Another head will pick the task up.
"""


_REVIEWER_PROMPT = """\
You are a Reviewer head. You run between turns of long-running heads to
inspect their progress. You are read-only. Your output is a short report
with: (a) commits since last review, (b) cost vs budget, (c) blockers,
(d) the single next concrete action the worker should take. Do not edit
files. Do not file tasks unless explicitly asked.
"""


_BUILDER_PROMPT = """\
You are a Builder head — the default worker. You have full read+write access
to your worktree. End every turn after a meaningful change with a commit.
When the task is done, submit a pull request via the worktree manager. If
you get blocked, write a memory note explaining what you tried and submit
a WIP pull request rather than holding your changes hostage.
"""


_MERGER_PROMPT = """\
You are a Merger head. Your toolkit is git + bash only. For every open pull
request in priority order: (1) fetch and rebase onto the base branch, (2)
run the configured test command, (3) push if green, mark `needs_review` on
red. Never edit code. Never resolve conflicts manually — bounce the PR back
to its author with a clear comment.
"""


_TEST_RUNNER_PROMPT = """\
You are a Test-Runner head. You may read files and run the project's test
command. You do not edit code. Your one deliverable per task is a JSON
report containing pass/fail counts, the failing test ids, and the tail of
the failure output. The merger uses this to gate merges.
"""


_AUDITOR_PROMPT = """\
You are an Auditor head. You produce a written audit document mapping
findings to file:line evidence. You are read-only. Quote the actual source
text — never paraphrase without citation. Surface ambiguities rather than
guessing. The audit lands as a markdown document at the path the task
prescribes.
"""


def Scanner(*, name: str = "scanner", model: str | None = None) -> Head:
    return Head(
        kind=HeadKind.SCANNER,
        name=name,
        allowed_tools=("Read", "Grep", "Glob", "Bash(git log)", "Bash(git diff)"),
        system_prompt=_SCANNER_PROMPT,
        description="Read-only worker that files new tasks.",
        default_model=model or "claude-sonnet-4-6",
    )


def Reviewer(*, name: str = "reviewer", model: str | None = None) -> Head:
    return Head(
        kind=HeadKind.REVIEWER,
        name=name,
        allowed_tools=("Read", "Grep", "Bash(git log)", "Bash(git status)"),
        system_prompt=_REVIEWER_PROMPT,
        description="Read-only periodic checkpointer.",
        default_model=model or "claude-sonnet-4-6",
    )


def Builder(*, name: str = "builder", model: str | None = None) -> Head:
    return Head(
        kind=HeadKind.BUILDER,
        name=name,
        allowed_tools=("Read", "Edit", "Write", "Grep", "Glob", "Bash"),
        system_prompt=_BUILDER_PROMPT,
        description="Default full-toolkit worker.",
        default_model=model or "claude-opus-4-7",
    )


def Merger(*, name: str = "merger", model: str | None = None) -> Head:
    return Head(
        kind=HeadKind.MERGER,
        name=name,
        allowed_tools=("Bash",),
        system_prompt=_MERGER_PROMPT,
        description="Git + bash only; runs the merge pipeline.",
        default_model=model or "claude-haiku-4-5",
    )


def TestRunner(*, name: str = "test-runner", model: str | None = None) -> Head:
    return Head(
        kind=HeadKind.TEST_RUNNER,
        name=name,
        allowed_tools=("Read", "Bash(pytest)", "Bash(npm test)", "Bash(cargo test)"),
        system_prompt=_TEST_RUNNER_PROMPT,
        description="Read + scoped test commands; gates merges.",
        default_model=model or "claude-haiku-4-5",
    )


def Auditor(*, name: str = "auditor", model: str | None = None) -> Head:
    return Head(
        kind=HeadKind.AUDITOR,
        name=name,
        allowed_tools=("Read", "Grep", "Glob", "Write"),
        system_prompt=_AUDITOR_PROMPT,
        description="Read-only researcher producing audit docs.",
        default_model=model or "claude-sonnet-4-6",
    )


def default_roster() -> dict[str, Head]:
    """Return the canonical six-head roster keyed by head name."""
    heads = [Scanner(), Reviewer(), Builder(), Merger(), TestRunner(), Auditor()]
    return {h.name: h for h in heads}


__all__ = [
    "Auditor",
    "Builder",
    "Head",
    "HeadKind",
    "Merger",
    "Reviewer",
    "Scanner",
    "TestRunner",
    "default_roster",
]
