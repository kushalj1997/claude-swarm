"""``claude-swarm scenario ...`` subcommand.

Bridges to the binding-agnostic substrate at ``tests/scenarios/``. The
scenarios + runner code is not part of the installed wheel — it ships in
the repo's ``tests/scenarios/`` so all three bindings (this CLI, the
claude-code plugin, our internal swarm) share one canonical copy.

We import lazily via filesystem path so this subcommand works out of a
source checkout, an editable install, or a vendored mirror.

Implemented with stdlib ``argparse`` to keep the substrate dependency-free
even before the rest of the CLI (which uses click) is fully wired up.
The ``main.py`` group can register this via::

    from .scenario import scenario as scenario_group_click  # if click is up
    main.add_command(scenario_group_click)

or shell out via ``python -m claude_swarm.cli.scenario`` directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path


def _runner_root() -> Path:
    """Locate the substrate's runner package.

    Tries (in order):
      1. ``$CLAUDE_SWARM_SCENARIOS_DIR``
      2. The package-relative ``tests/scenarios/`` (source checkout)
      3. A user-installed mirror under ``~/.claude-swarm/scenarios``
    """
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
    raise FileNotFoundError(
        "scenarios directory not found. Set $CLAUDE_SWARM_SCENARIOS_DIR or "
        "checkout the source repo."
    )


def _load_runner_pkg(root: Path):
    """Load ``runner`` as a real package so its relative imports work."""
    sys.path.insert(0, str(root))
    for stale in [k for k in list(sys.modules) if k.startswith("runner.")]:
        del sys.modules[stale]
    sys.modules.pop("runner", None)
    from runner import harness, stub  # type: ignore[import-not-found]
    return harness, stub


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-swarm scenario",
        description="Run binding-agnostic toy swarm scenarios.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list scenario names")
    rp = sub.add_parser("run", help="run a scenario (or --all)")
    rp.add_argument("name", nargs="?", help="scenario name; matches scenarios/<name>.json")
    rp.add_argument("--all", action="store_true", dest="run_all")
    rp.add_argument("--keep-workspace", action="store_true")
    rp.add_argument("--json", action="store_true", dest="as_json")
    rp.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = _runner_root()
    if args.cmd == "list":
        for p in sorted((root / "scenarios").glob("*.json")):
            print(p.stem)
        return 0

    harness, stub = _load_runner_pkg(root)
    engine = stub.InProcessScenarioEngine()

    if args.run_all:
        reports = harness.run_all(root / "scenarios", engine=engine, verbose=args.verbose)
    else:
        if not args.name:
            print("scenario name required (or --all)", file=sys.stderr)
            return 2
        path = root / "scenarios" / f"{args.name}.json"
        if not path.exists():
            print(f"scenario not found: {path}", file=sys.stderr)
            return 2
        reports = [
            harness.run_scenario(
                path,
                engine=engine,
                keep_workspace=args.keep_workspace,
                verbose=args.verbose,
            )
        ]

    if args.as_json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        for r in reports:
            head = "PASS" if r.ok else "FAIL"
            print(
                f"[{head}] {r.scenario} (binding={r.binding}) "
                f"passed={len(r.passed)} failed={len(r.failed)}"
            )
            for x in r.failed:
                print(f"    - {x}")

    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
