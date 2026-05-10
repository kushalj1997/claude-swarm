"""Doc-writer team demo: scanner files tasks, builders consume them.

Run with::

    python examples/doc_writer_team/run.py
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from claude_swarm import Kanban, MessageBus, Supervisor, Task
from claude_swarm._paths import inboxes_dir, kanban_path
from claude_swarm.heads import default_roster
from claude_swarm.supervisor import StubConductor, SupervisorConfig


def main() -> None:
    home = Path(tempfile.mkdtemp(prefix="claude-swarm-doc-team-"))
    try:
        kb = Kanban(kanban_path(home))
        bus = MessageBus(root=inboxes_dir(home))

        sections = ["intro", "install", "quickstart", "architecture", "faq"]
        ids: list[str] = []
        for s in sections:
            t = kb.submit(
                Task(
                    title=f"write section: {s}",
                    prompt=f"Draft the {s} section in 3-5 bullet points.",
                    required_head="builder",
                    tags=["doc", s],
                )
            )
            ids.append(t.id)
            bus.send(
                sender="scanner",
                recipient="builder",
                kind="hint",
                body={"section": s, "task_id": t.id, "tone": "calm"},
            )

        sup = Supervisor(
            kanban=kb,
            roster=default_roster(),
            conductor=StubConductor(),
            bus=bus,
            config=SupervisorConfig(poll_interval_s=0.0, max_iterations=20),
        )
        sup.run()

        print(json.dumps(sup.status(), indent=2))
        print("inbox snapshot:")
        for m in bus.recv("builder"):
            print(f"  {m.sender} -> {m.recipient} kind={m.kind} body={m.body}")
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
