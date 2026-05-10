"""Heads roster tests."""
from __future__ import annotations

from claude_swarm.heads import (
    Auditor,
    Builder,
    HeadKind,
    Merger,
    Reviewer,
    Scanner,
    TestRunner,
    default_roster,
)


def test_default_roster_has_six_heads() -> None:
    roster = default_roster()
    assert set(roster.keys()) == {
        "scanner",
        "reviewer",
        "builder",
        "merger",
        "test-runner",
        "auditor",
    }


def test_each_head_has_unique_kind() -> None:
    roster = default_roster()
    kinds = {h.kind for h in roster.values()}
    assert kinds == {
        HeadKind.SCANNER,
        HeadKind.REVIEWER,
        HeadKind.BUILDER,
        HeadKind.MERGER,
        HeadKind.TEST_RUNNER,
        HeadKind.AUDITOR,
    }


def test_scanner_is_read_only() -> None:
    h = Scanner()
    assert "Edit" not in h.allowed_tools
    assert "Write" not in h.allowed_tools


def test_builder_has_full_toolkit() -> None:
    h = Builder()
    for tool in ("Read", "Edit", "Write", "Bash"):
        assert tool in h.allowed_tools


def test_merger_is_bash_only() -> None:
    assert Merger().allowed_tools == ("Bash",)


def test_reviewer_and_auditor_dont_run_arbitrary_bash() -> None:
    assert "Bash" not in Reviewer().allowed_tools
    assert "Bash" not in Auditor().allowed_tools


def test_testrunner_scoped_bash() -> None:
    tools = TestRunner().allowed_tools
    assert any("pytest" in t or "test" in t for t in tools)
    assert "Edit" not in tools
