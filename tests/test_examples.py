"""Smoke tests for the bundled examples."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_todo_app_example_runs() -> None:
    env = {**__import__("os").environ}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "todo_app" / "run.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "running supervisor" in proc.stdout


def test_doc_writer_example_runs() -> None:
    env = {**__import__("os").environ}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "doc_writer_team" / "run.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "inbox snapshot" in proc.stdout
