"""``claude_swarm.scenarios`` — binding-agnostic scenario substrate.

The implementation lives at ``tests/scenarios/runner/`` so the same code
ships verbatim into all three target locations (this package, the
internal swarm at ``dm/swarm_tests/``, and the claude-code plugin at
``plugins/swarm-orchestrator/tests/swarming/``). This module re-exports
the public surface for callers who prefer the canonical import path
(``from claude_swarm.scenarios import ScenarioEngine``).

We resolve ``runner`` lazily because it isn't part of the installed
wheel — it's repo-local. See ``claude_swarm.cli.scenario`` for the same
trick used by the CLI.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _runner_root() -> Path:
    cand = os.environ.get("CLAUDE_SWARM_SCENARIOS_DIR")
    if cand:
        return Path(cand).expanduser().resolve()
    pkg = Path(__file__).resolve().parent.parent  # claude_swarm/
    here = pkg.parent / "tests" / "scenarios"
    if here.exists():
        return here
    user = Path.home() / ".claude-swarm" / "scenarios"
    if user.exists():
        return user
    raise ImportError(
        "claude_swarm.scenarios: cannot locate tests/scenarios/. "
        "Set $CLAUDE_SWARM_SCENARIOS_DIR or use a source checkout."
    )


def _bootstrap() -> tuple[Any, Any, Any]:
    root = _runner_root()
    sys.path.insert(0, str(root))
    # Drop any stale partial loads so relative imports re-resolve cleanly.
    for stale in [k for k in list(sys.modules) if k.startswith("runner.")]:
        del sys.modules[stale]
    sys.modules.pop("runner", None)
    from runner import assertions as _assertions  # type: ignore[import-not-found]
    from runner import harness as _harness  # type: ignore[import-not-found]
    from runner import stub as _stub  # type: ignore[import-not-found]
    return _stub, _assertions, _harness


stub, assertions, harness = _bootstrap()

ScenarioEngine = stub.ScenarioEngine
InProcessScenarioEngine = stub.InProcessScenarioEngine
RunResult = stub.RunResult
Scenario = stub.Scenario
TaskSpec = stub.TaskSpec
TeammateSpec = stub.TeammateSpec
evaluate = assertions.evaluate
AssertionReport = assertions.AssertionReport
run_scenario = harness.run_scenario
run_all = harness.run_all

__all__ = [
    "AssertionReport",
    "InProcessScenarioEngine",
    "RunResult",
    "Scenario",
    "ScenarioEngine",
    "TaskSpec",
    "TeammateSpec",
    "assertions",
    "evaluate",
    "harness",
    "run_all",
    "run_scenario",
    "stub",
]
