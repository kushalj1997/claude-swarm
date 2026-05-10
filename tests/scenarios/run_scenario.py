#!/usr/bin/env python3
"""Standalone runner — exercises a scenario via the claude-swarm engine.

Falls back to the binding-agnostic in-process reference engine until the
package exposes its own engine adapter under
``claude_swarm.scenarios.engine``. The same scenario JSON drives all
three bindings.

Usage::

    python tests/scenarios/run_scenario.py multi-file-rename
    python tests/scenarios/run_scenario.py --all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from runner.harness import run_all, run_scenario  # noqa: E402
from runner.stub import InProcessScenarioEngine  # noqa: E402


def _make_engine():
    """Return the engine for this binding.

    When ``claude_swarm.scenarios`` is wired up, swap in the real
    standalone engine. Until then, the reference implementation keeps
    the substrate green.
    """
    try:
        from claude_swarm.scenarios.engine import StandaloneScenarioEngine  # type: ignore[import-not-found]
        return StandaloneScenarioEngine()
    except Exception:  # noqa: BLE001 — adapter absence is expected pre-1.0
        return InProcessScenarioEngine()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scenario", nargs="?", help="scenario name or path")
    p.add_argument("--all", action="store_true")
    p.add_argument("--scenarios-dir", default=str(THIS_DIR / "scenarios"))
    p.add_argument("--keep-workspace", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    engine = _make_engine()
    if args.all:
        reports = run_all(args.scenarios_dir, engine=engine, verbose=args.verbose)
    else:
        if not args.scenario:
            p.error("scenario name required (or --all)")
        candidate = Path(args.scenario)
        if not candidate.exists():
            candidate = Path(args.scenarios_dir) / f"{args.scenario}.json"
        if not candidate.exists():
            print(f"scenario not found: {args.scenario}", file=sys.stderr)
            return 2
        reports = [run_scenario(candidate, engine=engine, keep_workspace=args.keep_workspace, verbose=args.verbose)]

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        for r in reports:
            head = "PASS" if r.ok else "FAIL"
            print(f"[{head}] {r.scenario} (binding={r.binding}) passed={len(r.passed)} failed={len(r.failed)}")
            for x in r.failed:
                print(f"    - {x}")
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
