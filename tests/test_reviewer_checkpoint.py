"""Reviewer checkpoint tests."""
from __future__ import annotations

from claude_swarm.reviewer_checkpoint import ReviewerCheckpoint, render


def test_should_fire_at_interval() -> None:
    cp = ReviewerCheckpoint(interval=3, max_turns=10)
    assert not cp.should_fire(0)
    assert not cp.should_fire(1)
    assert not cp.should_fire(2)
    assert cp.should_fire(3)
    assert not cp.should_fire(4)
    assert cp.should_fire(6)


def test_render_template_includes_stats() -> None:
    cp = ReviewerCheckpoint(interval=3, max_turns=10, cost_cap_usd=1.50)
    out = cp.render(turn=3, cost_so_far_usd=0.42)
    assert "turn 3/10" in out
    assert "$0.4200" in out
    assert "$1.50 cap" in out
    assert "every 3 turns" in out


def test_top_level_render_callable() -> None:
    out = render(
        turn=2, max_turns=5, cost_so_far_usd=0.1, cost_cap_usd=1.0, interval=1
    )
    assert "turn 2/5" in out
