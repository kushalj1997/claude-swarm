# claude-swarm

> Generic, dependency-light swarm orchestration for Claude Code teammates.

`claude-swarm` is a small Python library + CLI that turns a single
[Claude Code] session into a coordinated swarm of named **heads** —
Scanner, Reviewer, Builder, Merger, Test-Runner, Auditor — each with
role-specific prompts, tool restrictions, and a shared DAG-aware kanban.

The headline value-add over vanilla Claude Code Teams is a **first-class
DAG iterator** (`Kanban.unblocked()`), an **abort-marker contract** for
graceful long-running cancellation, a **per-task git worktree** workflow
with JSON pull-request envelopes, and an **auto-merge pipeline** that
detects file overlap and runs your test command before pushing.

[Claude Code]: https://docs.claude.com/en/docs/claude-code/overview

## Install

```bash
pip install claude-swarm
# or, from a checkout
pip install -e .
```

Python ≥ 3.11 only. The single required runtime dependency is `click`;
optional `httpx` is loaded only if you opt into the HTTP transport.

## Quickstart

```python
from claude_swarm import Kanban, Task, Supervisor, default_roster

kb = Kanban("/tmp/swarm/kanban.sqlite")

a = kb.submit(Task(title="design", prompt="Sketch the API.", required_head="builder"))
b = kb.submit(Task(title="implement", prompt="Build it.", required_head="builder",
                   blocked_by=[a.id]))
c = kb.submit(Task(title="review", prompt="LGTM?", required_head="reviewer",
                   blocked_by=[b.id]))

# Only `a` is unblocked initially; `b` and `c` unlock as their blockers complete.
sup = Supervisor(kanban=kb)
sup.run()
print(sup.status())
```

…or from the shell:

```bash
claude-swarm init
claude-swarm submit --title "design"   --prompt "Sketch the API." --head builder
claude-swarm submit --title "build"    --prompt "Implement it."  --head builder --blocked-by <id>
claude-swarm unblocked
claude-swarm run --max-iterations 10
claude-swarm status
```

## Architecture

```text
                       claude-swarm
        +----------------------------------------+
        |                                        |
        |    Kanban (sqlite/WAL, DAG-aware)      |
        |    - submit / claim / transition       |
        |    - unblocked()  <-- topo iterator    |
        |    - status_timeline                   |
        |                                        |
        +-------+--------------------+-----------+
                |                    |
                v                    v
        Supervisor              MessageBus (JSON inboxes)
        - picks unblocked       - bounded (drop-oldest)
        - matches required_head - atomic writes
        - dispatches via        - broadcast or directed
          Conductor protocol
                |
                v
        Heads (Scanner, Reviewer, Builder, Merger, Test-Runner, Auditor)
                |
                v
        WorktreeManager  ---->  PR envelopes  ----> merge_pipeline
        - per-task worktree     (.json files)        - file-overlap reject
        - cherry-pick merge                          - topo order
        - GC on success/stale                        - test gate
```

State lives under `$CLAUDE_SWARM_HOME` (default `./.claude-swarm`):

```text
.claude-swarm/
├── state/
│   ├── kanban.sqlite         # tasks, dependencies, status timeline
│   ├── inboxes/<name>.json   # bounded directed message queues
│   ├── pull_requests/<id>.json
│   ├── worktrees_meta/<id>.json   # GC markers
│   └── status.json           # mind-page-friendly snapshot
└── worktrees/swarm-<id>/     # per-task git worktrees
```

## CLI reference

| Command | Description |
| --- | --- |
| `claude-swarm init` | Create the swarm home directory tree. |
| `claude-swarm submit --title T --prompt P [--head H] [--blocked-by ID]` | File a task. |
| `claude-swarm list [--status S] [--tag X]` | List tasks. |
| `claude-swarm unblocked [--head H]` | Print the topological iterator. |
| `claude-swarm status` | JSON snapshot of kanban + supervisor; also writes `status.json`. |
| `claude-swarm heads` | List the built-in heads. |
| `claude-swarm inbox send --from A --to B --body '{...}'` | Send a directed message. |
| `claude-swarm inbox recv NAME [--drain]` | Read messages targeted at NAME. |
| `claude-swarm merge --repo PATH [--test-cmd "..."]` | Run the auto-merge pipeline. |
| `claude-swarm abort set --worktree DIR --teammate NAME` | Set an abort marker. |
| `claude-swarm abort clear --worktree DIR --teammate NAME` | Clear it. |
| `claude-swarm abort check --worktree DIR --teammate NAME` | Exit 0 if set, 1 if clear. |
| `claude-swarm run [--max-iterations N]` | Run the supervisor loop with the stub conductor. |

## Heads

The default roster:

| Name | Role | Allowed tools | Default model |
| --- | --- | --- | --- |
| `scanner` | Read-only; files new tasks. | `Read`, `Grep`, `Glob`, `Bash(git log\|diff)` | `claude-sonnet-4-6` |
| `reviewer` | Periodic checkpoints, no edits. | `Read`, `Grep`, `Bash(git log\|status)` | `claude-sonnet-4-6` |
| `builder` | Default worker; full toolkit. | `Read`, `Edit`, `Write`, `Grep`, `Glob`, `Bash` | `claude-opus-4-7` |
| `merger` | Git + bash only. | `Bash` | `claude-haiku-4-5` |
| `test-runner` | Read + scoped test commands. | `Read`, `Bash(pytest\|npm test\|cargo test)` | `claude-haiku-4-5` |
| `auditor` | Read-only; produces audit docs. | `Read`, `Grep`, `Glob`, `Write` | `claude-sonnet-4-6` |

Override any of these — pass your own `roster` to `Supervisor` or use the
constructor functions in `claude_swarm.heads` to mint a custom head.

## Conductors

A **Conductor** is the pluggable strategy that actually runs a head against
a task. The library ships three:

* `StubConductor` — records dispatches and immediately marks done. Useful
  for tests + the toy examples in `examples/`.
* `SubprocessConductor(command_factory=…)` — runs an arbitrary command per
  task with the prompt on stdin.
* `ClaudeCLIConductor()` — convenience wrapper for `claude --print`.

A downstream Claude Code plugin can ship its own conductor that spawns
subagents directly inside an existing session.

## Reviewer checkpoint

Configure a reviewer-checkpoint to inject a self-review prompt every N
turns of a long-running head:

```python
from claude_swarm import ReviewerCheckpoint

cp = ReviewerCheckpoint(interval=3, max_turns=100, cost_cap_usd=5.0)
if cp.should_fire(turn=current_turn):
    print(cp.render(turn=current_turn, cost_so_far_usd=spent))
```

The default template forces the worker to (1) list what was accomplished,
(2) confirm pending work is committed, (3) surface blockers, (4) account
for cost vs. budget, and (5) state the next concrete tool call.

## Abort-marker contract

Long-running heads should poll for `<worktree>/.claude/abort-<name>`
between phases. When set, commit any work-in-progress, push, and exit
cleanly. The `AbortMarker` helper bundles the contract:

```python
from claude_swarm import AbortMarker, AbortRequested

marker = AbortMarker(worktree_root=Path.cwd(), teammate="builder-1")
try:
    marker.raise_if_set()
    do_work()
except AbortRequested:
    git_commit_wip()
    sys.exit(0)
```

## Auto-merge pipeline

```python
from pathlib import Path
from claude_swarm import WorktreeManager
from claude_swarm.merge_pipeline import run_pipeline

mgr = WorktreeManager(repo_root=Path("/path/to/repo"))
report = run_pipeline(
    mgr,
    test_command=["pytest", "-x", "-q"],
    reject_overlap=True,
)
print(report.merged, report.rejected, report.test_failures)
```

Behaviour:

* **File-overlap reject** — if two open PRs touch the same file, the batch
  is refused; retry serially.
* **Topological order** — smallest diffs merge first to minimise rebases.
* **Test gate** — the configured command runs after each merge; failures
  trigger an automatic `git revert` of the just-merged commits.
* **GC on success** — the worktree + branch are deleted when the merge
  lands cleanly.

## Comparison vs vanilla Teams

| Feature | Vanilla Teams | claude-swarm |
| --- | --- | --- |
| `addBlocks` / `addBlockedBy` | yes | yes |
| Topological iterator | no | **`Kanban.unblocked()`** |
| Auto-unblock cascade | manual | yes (status timeline) |
| Named heads with tool allowlists | manual | **default 6-head roster** |
| Abort-marker contract | ad-hoc | **first-class** |
| Per-task worktree + PR envelopes | manual | **`WorktreeManager`** |
| Auto-merge with overlap reject | manual | **`merge_pipeline`** |
| Reviewer checkpoints | manual | **`ReviewerCheckpoint`** |
| Status timeline | n/a | yes |

## FAQ

**Why SQLite for the kanban?**
WAL mode gives us concurrent reads + a single writer. The schema is small,
the file is portable, and `sqlite3` is in the stdlib. No daemon to run.

**Why JSON envelopes for PRs instead of GitHub PRs?**
Local-first. The operator is the human reviewer of last resort. The
envelope captures everything a merger needs (head sha, diff stat, files
changed, body). Wrap `WorktreeManager` if you want real GitHub PRs.

**Does it work without Claude?**
Yes — the `StubConductor` runs the orchestration end-to-end with no LLM
calls. Ship your own `Conductor` to plug in any backend.

## Examples

* [`examples/todo_app`](examples/todo_app) — three tasks (design → build →
  review) with DAG dependencies, all using the stub conductor.
* [`examples/doc_writer_team`](examples/doc_writer_team) — a roster of
  scanners + builders feeding directed messages through the inbox.

## Performance targets

* Poll latency: < 100 ms (sqlite WAL select)
* Dispatch time: < 5 s including worktree creation
* Inbox round-trip: < 50 ms (atomic JSON write)

Run the benchmarks under `tests/benchmarks/` to verify locally.

## License

Apache 2.0. See `LICENSE`.
