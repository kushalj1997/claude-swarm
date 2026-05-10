"""``claude-swarm`` — top-level CLI.

Subcommands:
    submit       file a new task
    status       print the kanban + supervisor status
    list         list tasks (filterable by status)
    unblocked    print the topological iterator
    inbox        send / receive directed messages
    merge        run the auto-merge pipeline
    abort        set/clear an abort marker
    run          run the supervisor loop (with the stub conductor by default)

Designed so a downstream plugin can wrap or extend each subcommand without
forking the package.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from .. import __version__
from .._paths import (
    inboxes_dir,
    kanban_path,
    pull_requests_dir,
    state_dir,
    status_file,
    swarm_home,
    worktrees_dir,
)
from ..abort import AbortMarker
from ..heads import default_roster
from ..kanban import Kanban, Task, TaskStatus
from ..merge_pipeline import run_pipeline
from ..messaging import MessageBus
from ..conductor import ClaudeCLIConductor
from ..supervisor import StubConductor, Supervisor, SupervisorConfig
from ..worktree import WorktreeManager


def _pid_file(home: Path | None) -> Path:
    return state_dir(home) / "supervisor.pid"


def _daemon_log(home: Path | None) -> Path:
    return state_dir(home) / "daemon.log"


def _spawn_daemon_or_exit(home: Path | None) -> None:
    """Fork once; parent prints PID + paths and exits, child continues.

    Single-fork is enough for survival across shell exit on POSIX. We
    additionally call ``os.setsid()`` in the child so the daemon detaches
    from the controlling terminal's process group. stdin/stdout/stderr
    are redirected to a log file under the swarm home.
    """
    import os
    import sys

    state_dir(home).mkdir(parents=True, exist_ok=True)
    pidfile = _pid_file(home)
    logfile = _daemon_log(home)

    pid = os.fork()
    if pid > 0:
        # Parent: write the child's PID, print useful info, exit.
        pidfile.write_text(str(pid))
        click.echo(
            json.dumps(
                {
                    "status": "daemonized",
                    "pid": pid,
                    "pid_file": str(pidfile),
                    "log_file": str(logfile),
                    "home": str(swarm_home(home)),
                    "stop_with": f"claude-swarm daemon-stop --home {swarm_home(home)}",
                    "status_with": f"claude-swarm daemon-status --home {swarm_home(home)}",
                },
                indent=2,
            )
        )
        sys.exit(0)
    # Child: detach from controlling terminal + redirect IO + run.
    os.setsid()
    fd_log = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd_log, sys.stdout.fileno())
    os.dup2(fd_log, sys.stderr.fileno())
    fd_null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(fd_null, sys.stdin.fileno())
    os.close(fd_null)
    if fd_log > 2:
        os.close(fd_log)


def _home_option(f: Any) -> Any:
    return click.option(
        "--home",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Override the swarm home directory (default: $CLAUDE_SWARM_HOME or ./.claude-swarm).",
    )(f)


@click.group(help="claude-swarm — generic swarm orchestration for Claude Code.")
@click.version_option(__version__, prog_name="claude-swarm")
def main() -> None:
    pass


@main.command()
@_home_option
def init(home: Path | None) -> None:
    """Create the swarm home directory tree."""
    base = swarm_home(home)
    for d in (
        state_dir(home),
        inboxes_dir(home),
        pull_requests_dir(home),
        worktrees_dir(home),
    ):
        d.mkdir(parents=True, exist_ok=True)
    Kanban(kanban_path(home))
    click.echo(f"initialised swarm at {base}")


@main.command()
@_home_option
@click.option("--title", required=True, help="Short, human-readable task title.")
@click.option("--prompt", required=True, help="Prompt the worker will receive.")
@click.option("--type", "type_", default="default", help="Task type tag.")
@click.option("--head", "required_head", default="builder", help="Required head name.")
@click.option("--priority", default=5, type=int)
@click.option("--blocked-by", multiple=True, help="Task id(s) this depends on.")
@click.option("--tag", "tags", multiple=True, help="Free-form tags for filtering.")
def submit(
    home: Path | None,
    title: str,
    prompt: str,
    type_: str,
    required_head: str,
    priority: int,
    blocked_by: tuple[str, ...],
    tags: tuple[str, ...],
) -> None:
    """Submit a task to the kanban."""
    kb = Kanban(kanban_path(home))
    t = kb.submit(
        Task(
            type=type_,
            title=title,
            prompt=prompt,
            required_head=required_head,
            priority=priority,
            blocked_by=list(blocked_by),
            tags=list(tags),
        )
    )
    click.echo(t.id)


@main.command(name="list")
@_home_option
@click.option(
    "--status",
    type=click.Choice([s.value for s in TaskStatus]),
    default=None,
)
@click.option("--tag", default=None)
def list_cmd(home: Path | None, status: str | None, tag: str | None) -> None:
    """List tasks, optionally filtered by status or tag."""
    kb = Kanban(kanban_path(home))
    tasks = kb.list_tasks(status=TaskStatus(status) if status else None, tag=tag)
    for t in tasks:
        click.echo(
            f"{t.id}  {t.status.value:<14} {t.required_head:<10} {t.title[:60]}"
        )


@main.command()
@_home_option
@click.option("--head", "required_head", default=None)
def unblocked(home: Path | None, required_head: str | None) -> None:
    """Print pending tasks whose blockers are all done."""
    kb = Kanban(kanban_path(home))
    for t in kb.unblocked(required_head=required_head):
        click.echo(f"{t.id}  {t.required_head:<10} {t.title[:60]}")


@main.command()
@_home_option
def status(home: Path | None) -> None:
    """Print a JSON snapshot of the kanban + supervisor status."""
    kb = Kanban(kanban_path(home))
    sup = Supervisor(kanban=kb)
    snap = sup.status()
    snap["swarm_home"] = str(swarm_home(home))
    click.echo(json.dumps(snap, indent=2))
    # Also persist for subscribers (mind page / dashboard).
    sf = status_file(home)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(snap, indent=2), encoding="utf-8")


@main.group()
def inbox() -> None:
    """Send / receive directed messages between heads."""


@inbox.command("send")
@_home_option
@click.option("--from", "sender", required=True)
@click.option("--to", "recipient", required=True)
@click.option("--kind", default="note")
@click.option("--body", default="{}", help="JSON object body.")
def inbox_send(
    home: Path | None,
    sender: str,
    recipient: str,
    kind: str,
    body: str,
) -> None:
    bus = MessageBus(root=inboxes_dir(home))
    try:
        body_obj = json.loads(body)
    except ValueError as exc:
        raise click.ClickException(f"--body must be JSON: {exc}") from exc
    msg = bus.send(sender=sender, recipient=recipient, kind=kind, body=body_obj)
    click.echo(msg.id)


@inbox.command("recv")
@_home_option
@click.argument("recipient")
@click.option("--drain/--peek", default=False)
def inbox_recv(home: Path | None, recipient: str, drain: bool) -> None:
    bus = MessageBus(root=inboxes_dir(home))
    msgs = bus.drain(recipient) if drain else bus.recv(recipient)
    click.echo(json.dumps([m.to_dict() for m in msgs], indent=2))


@main.command()
@_home_option
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--test-cmd", default=None, help="Shell command run after each merge.")
@click.option("--no-overlap-reject/--overlap-reject", default=False)
def merge(
    home: Path | None,
    repo: str,
    test_cmd: str | None,
    no_overlap_reject: bool,
) -> None:
    """Run the auto-merge pipeline against open pull requests."""
    mgr = WorktreeManager(
        repo_root=Path(repo),
        worktrees_dir=worktrees_dir(home),
        prs_dir=pull_requests_dir(home),
    )
    cmd = test_cmd.split() if test_cmd else None
    report = run_pipeline(
        mgr,
        test_command=cmd,
        reject_overlap=not no_overlap_reject,
    )
    click.echo(
        json.dumps(
            {
                "merged": report.merged,
                "rejected": report.rejected,
                "test_failures": list(report.test_failures.keys()),
            },
            indent=2,
        )
    )


@main.group()
def abort() -> None:
    """Set or clear an abort marker for a teammate."""


@abort.command("set")
@click.option("--worktree", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--teammate", required=True)
@click.option("--reason", default="operator")
def abort_set(worktree: Path, teammate: str, reason: str) -> None:
    AbortMarker(worktree_root=worktree, teammate=teammate).set(reason=reason)
    click.echo(f"set marker for {teammate} at {worktree}")


@abort.command("clear")
@click.option("--worktree", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--teammate", required=True)
def abort_clear(worktree: Path, teammate: str) -> None:
    AbortMarker(worktree_root=worktree, teammate=teammate).clear()
    click.echo(f"cleared marker for {teammate} at {worktree}")


@abort.command("check")
@click.option("--worktree", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--teammate", required=True)
def abort_check(worktree: Path, teammate: str) -> None:
    is_set = AbortMarker(worktree_root=worktree, teammate=teammate).is_set()
    click.echo("set" if is_set else "clear")
    sys.exit(0 if is_set else 1)


@main.command()
@_home_option
@click.option("--max-iterations", default=None, type=int)
@click.option("--poll-s", default=1.0, type=float)
@click.option(
    "--conductor",
    type=click.Choice(["stub", "claude"]),
    default="stub",
    help="stub = no LLM calls (deterministic); claude = real dispatch via `claude --print`.",
)
@click.option(
    "--global-mind-log",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Append a JSONL event line per supervisor step (the swarm's global-mind transcript).",
)
@click.option(
    "--demo-delay-s",
    type=float,
    default=0.0,
    help="When conductor=stub, sleep N seconds before each dispatch so the dashboard renders visibly. 0 = instant (tests).",
)
@click.option(
    "--daemon",
    is_flag=True,
    default=False,
    help="Detach from the controlling terminal. Parent exits immediately; child keeps running and survives shell exit. PID + log paths printed.",
)
@click.option(
    "--max-parallel",
    type=int,
    default=1,
    help="Dispatch this many ready tasks concurrently (thread pool). Defaults to serial dispatch (1). Set to 3+ for a 'live' demo where multiple in-progress rows are visible simultaneously.",
)
def run(
    home: Path | None,
    max_iterations: int | None,
    poll_s: float,
    conductor: str,
    global_mind_log: Path | None,
    demo_delay_s: float,
    daemon: bool,
    max_parallel: int,
) -> None:
    """Run the supervisor loop.

    With ``--conductor=stub`` (default) no LLM calls are made — useful for
    smoke tests and CI. With ``--conductor=claude`` each dispatched task
    shells out to the ``claude`` CLI (requires the binary on PATH and an
    authenticated session).

    With ``--daemon`` the supervisor detaches from the controlling
    terminal: parent prints the PID + log path then exits, child keeps
    running. Stop with ``claude-swarm daemon-stop --home <path>``.
    """
    import os as _os
    import time as _time

    if daemon:
        _spawn_daemon_or_exit(home)
        # The child of the fork continues below; the parent has exited.

    kb = Kanban(kanban_path(home))
    cond: Any
    if conductor == "claude":
        # Demo path: force Haiku for fast cheap dispatch (~3-5s per task).
        # Production callers wire ClaudeCLIConductor() with model_override=None
        # via the Python API to use each head's role-appropriate default.
        import os as _os
        model_override = _os.environ.get("CLAUDE_SWARM_MODEL_OVERRIDE", "claude-haiku-4-5")
        cond = ClaudeCLIConductor(model_override=model_override or None)
    else:
        cond = StubConductor(demo_delay_s=demo_delay_s)
    sup = Supervisor(
        kanban=kb,
        roster=default_roster(),
        conductor=cond,
        config=SupervisorConfig(
            poll_interval_s=poll_s,
            max_iterations=max_iterations,
            wait_for_work=daemon,  # daemon mode = keep polling forever
            max_parallel=max_parallel,
        ),
    )

    # Wrap step() so each dispatch emits a global-mind event line.
    if global_mind_log is not None:
        global_mind_log.parent.mkdir(parents=True, exist_ok=True)
        _orig_step = sup.step

        def _logged_step() -> Task | None:
            t0 = _time.time()
            dispatched = _orig_step()
            if dispatched is None:
                return None
            try:
                fresh = kb.get(dispatched.id)
            except Exception:
                fresh = dispatched
            event = {
                "ts": _time.time(),
                "turn": sup._turn,
                "task_id": dispatched.id,
                "head": dispatched.required_head,
                "title": dispatched.title,
                "status": fresh.status.value if hasattr(fresh.status, "value") else str(fresh.status),
                "elapsed_s": round(_time.time() - t0, 3),
                "cost_so_far_usd": round(sup._cost_so_far_usd, 6),
            }
            with global_mind_log.open("a") as fh:
                fh.write(json.dumps(event) + "\n")
            return dispatched

        sup.step = _logged_step  # type: ignore[method-assign]

    sup.run()
    click.echo(json.dumps(sup.status(), indent=2))


@main.command()
def heads() -> None:
    """List the built-in heads."""
    roster = default_roster()
    for name, h in sorted(roster.items()):
        click.echo(
            f"{name:<14} {h.kind.value:<14} model={h.default_model:<24} {h.description}"
        )


@main.command("daemon-status")
@_home_option
def daemon_status_cmd(home: Path | None) -> None:
    """Show whether a supervisor daemon is alive for this home."""
    import os

    pidfile = _pid_file(home)
    if not pidfile.exists():
        click.echo(json.dumps({"alive": False, "reason": "no pid file"}, indent=2))
        sys.exit(1)
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError) as exc:
        click.echo(json.dumps({"alive": False, "reason": f"unreadable pid file: {exc}"}, indent=2))
        sys.exit(1)
    try:
        os.kill(pid, 0)  # 0 = check existence without signalling
        alive = True
        reason = "process responsive"
    except ProcessLookupError:
        alive = False
        reason = "process not found (stale pid file)"
    except PermissionError:
        alive = True
        reason = "process exists (owned by another user)"
    click.echo(
        json.dumps(
            {
                "alive": alive,
                "pid": pid,
                "pid_file": str(pidfile),
                "log_file": str(_daemon_log(home)),
                "reason": reason,
            },
            indent=2,
        )
    )
    sys.exit(0 if alive else 1)


@main.command("daemon-stop")
@_home_option
@click.option("--timeout-s", default=5.0, type=float, help="Wait this long for SIGTERM before SIGKILL.")
def daemon_stop_cmd(home: Path | None, timeout_s: float) -> None:
    """Send SIGTERM to the supervisor daemon; SIGKILL after timeout."""
    import os
    import signal
    import time as _time

    pidfile = _pid_file(home)
    if not pidfile.exists():
        click.echo(json.dumps({"stopped": False, "reason": "no pid file"}, indent=2))
        sys.exit(1)
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError) as exc:
        click.echo(json.dumps({"stopped": False, "reason": f"unreadable pid file: {exc}"}, indent=2))
        sys.exit(1)
    # SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        click.echo(json.dumps({"stopped": True, "pid": pid, "reason": "already exited"}, indent=2))
        return
    # Wait for clean exit
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            os.kill(pid, 0)
            _time.sleep(0.1)
        except ProcessLookupError:
            pidfile.unlink(missing_ok=True)
            click.echo(json.dumps({"stopped": True, "pid": pid, "method": "SIGTERM"}, indent=2))
            return
    # Force-kill
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pidfile.unlink(missing_ok=True)
    click.echo(json.dumps({"stopped": True, "pid": pid, "method": "SIGKILL", "reason": "didn't exit within timeout"}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
