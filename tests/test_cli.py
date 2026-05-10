"""CLI smoke tests via click's CliRunner."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from claude_swarm.cli.main import main


def test_help_runs() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "claude-swarm" in result.output


def test_init_then_submit_then_list(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "swarm"
    monkeypatch.setenv("CLAUDE_SWARM_HOME", str(home))
    runner = CliRunner()
    r = runner.invoke(main, ["init"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        main,
        ["submit", "--title", "t1", "--prompt", "do it", "--head", "builder"],
    )
    assert r.exit_code == 0, r.output
    task_id = r.output.strip()
    r = runner.invoke(main, ["list"])
    assert r.exit_code == 0
    assert task_id in r.output


def test_unblocked_and_status(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "swarm"
    monkeypatch.setenv("CLAUDE_SWARM_HOME", str(home))
    runner = CliRunner()
    runner.invoke(main, ["init"])
    runner.invoke(main, ["submit", "--title", "t1", "--prompt", "p"])
    r = runner.invoke(main, ["unblocked"])
    assert r.exit_code == 0
    assert "t1" in r.output
    r = runner.invoke(main, ["status"])
    assert r.exit_code == 0
    snap = json.loads(r.output)
    assert snap["kanban"]["pending"] == 1


def test_inbox_send_recv(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "swarm"
    monkeypatch.setenv("CLAUDE_SWARM_HOME", str(home))
    runner = CliRunner()
    runner.invoke(main, ["init"])
    r = runner.invoke(
        main,
        [
            "inbox",
            "send",
            "--from",
            "scanner",
            "--to",
            "builder",
            "--body",
            '{"hi": 1}',
        ],
    )
    assert r.exit_code == 0
    r = runner.invoke(main, ["inbox", "recv", "builder"])
    assert r.exit_code == 0
    msgs = json.loads(r.output)
    assert msgs[0]["body"] == {"hi": 1}


def test_run_drains_with_stub(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "swarm"
    monkeypatch.setenv("CLAUDE_SWARM_HOME", str(home))
    runner = CliRunner()
    runner.invoke(main, ["init"])
    runner.invoke(main, ["submit", "--title", "t", "--prompt", "p"])
    r = runner.invoke(main, ["run", "--max-iterations", "5"])
    assert r.exit_code == 0
    snap = json.loads(r.output)
    assert snap["kanban"]["done"] >= 1


def test_heads_lists_six() -> None:
    r = CliRunner().invoke(main, ["heads"])
    assert r.exit_code == 0
    for name in ("scanner", "reviewer", "builder", "merger", "test-runner", "auditor"):
        assert name in r.output


def test_abort_set_clear_check(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(
        main,
        ["abort", "set", "--worktree", str(tmp_path), "--teammate", "alice"],
    )
    assert r.exit_code == 0
    r = runner.invoke(
        main,
        ["abort", "check", "--worktree", str(tmp_path), "--teammate", "alice"],
    )
    assert r.exit_code == 0  # set
    r = runner.invoke(
        main,
        ["abort", "clear", "--worktree", str(tmp_path), "--teammate", "alice"],
    )
    assert r.exit_code == 0
    r = runner.invoke(
        main,
        ["abort", "check", "--worktree", str(tmp_path), "--teammate", "alice"],
    )
    assert r.exit_code == 1  # cleared
