"""Toy three-task DAG demo using the stub conductor.

Run with::

    python examples/todo_app/run.py
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from claude_swarm import Kanban, Supervisor, Task
from claude_swarm._paths import kanban_path
from claude_swarm.heads import default_roster
from claude_swarm.supervisor import StubConductor, SupervisorConfig


def main() -> None:
    home = Path(tempfile.mkdtemp(prefix="claude-swarm-example-"))
    try:
        kb = Kanban(kanban_path(home))

        a = kb.submit(
            Task(
                title="design",
                prompt="Sketch the API for a Todo app.",
                required_head="builder",
            )
        )
        b = kb.submit(
            Task(
                title="build",
                prompt="Implement the API.",
                required_head="builder",
                blocked_by=[a.id],
            )
        )
        c = kb.submit(
            Task(
                title="review",
                prompt="Skim the diff and approve.",
                required_head="reviewer",
                blocked_by=[b.id],
            )
        )
        for label, task in (("design", a), ("build", b), ("review", c)):
            print(f"submitted: {label:<6} ({task.id})  blocked_by={task.blocked_by}")

        unblocked_ids = [t.title for t in kb.unblocked()]
        print(f"unblocked initially: {unblocked_ids}")

        sup = Supervisor(
            kanban=kb,
            roster=default_roster(),
            conductor=StubConductor(),
            config=SupervisorConfig(poll_interval_s=0.0, max_iterations=10),
        )
        print("running supervisor...")
        sup.run()
        print(json.dumps(sup.status(), indent=2))
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
