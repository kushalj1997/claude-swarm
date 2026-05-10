"""Reviewer checkpoint — periodic self-review hook for long-running heads.

Every ``interval`` turns the supervisor (or the head itself) injects a small
prompt forcing the worker to:

    1. List what was accomplished since the last checkpoint.
    2. Confirm pending work is committed.
    3. Surface any blockers.
    4. Account for cost vs. budget.
    5. State the next concrete tool call.

This module exposes both a small dataclass (so callers can configure once and
reuse) and a stateless :func:`render` function for one-shot use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_INTERVAL: int = int(os.environ.get("CLAUDE_SWARM_CHECKPOINT_INTERVAL", "3"))


CHECKPOINT_TEMPLATE: str = """\
## Reviewer checkpoint — turn {turn}/{max_turns} (${cost_so_far_usd:.4f} / ${cost_cap_usd:.2f} cap)

This is an automated self-review prompt injected every {interval} turns by
the supervisor. Answer every question below, then continue your work.

### Status questions
1. **What did you accomplish since the last checkpoint?**
   List the files you wrote or edited and summarise each change in one line.

2. **Have you committed your work?**
   If any writes are un-committed, run `git add -A && git commit -m 'wip: ...'`
   NOW before answering further. Un-committed work is invisible to reviewers.

3. **Are you blocked?**
   If yes: describe the blocker, write a memory note explaining what you tried,
   commit your scaffolding, and end the turn with a WIP pull request.

4. **Cost & turns remaining**
   You have used turn {turn} of {max_turns} and ${cost_so_far_usd:.4f} of the
   ${cost_cap_usd:.2f} cap. Adjust scope if you are burning budget too fast.

5. **Next concrete action**
   State the single next tool call you will make after this checkpoint.

### Hard rules reminder
- NEVER end a turn after writing code without committing first.
- NEVER say "I'll do X now" without immediately following with the tool call.
- The deliverable is committed files, not text in chat.
"""


def render(
    *,
    turn: int,
    max_turns: int,
    cost_so_far_usd: float,
    cost_cap_usd: float,
    interval: int = DEFAULT_INTERVAL,
) -> str:
    """Format the checkpoint prompt with current run stats."""
    return CHECKPOINT_TEMPLATE.format(
        turn=turn,
        max_turns=max_turns,
        cost_so_far_usd=cost_so_far_usd,
        cost_cap_usd=cost_cap_usd,
        interval=interval,
    )


@dataclass
class ReviewerCheckpoint:
    """Bundle of checkpoint configuration for a long-running head."""

    interval: int = DEFAULT_INTERVAL
    max_turns: int = 100
    cost_cap_usd: float = 5.0

    def should_fire(self, turn: int) -> bool:
        """Return ``True`` when this turn should fire the checkpoint."""
        return turn > 0 and (turn % self.interval == 0)

    def render(self, *, turn: int, cost_so_far_usd: float) -> str:
        return render(
            turn=turn,
            max_turns=self.max_turns,
            cost_so_far_usd=cost_so_far_usd,
            cost_cap_usd=self.cost_cap_usd,
            interval=self.interval,
        )


__all__ = ["CHECKPOINT_TEMPLATE", "DEFAULT_INTERVAL", "ReviewerCheckpoint", "render"]
