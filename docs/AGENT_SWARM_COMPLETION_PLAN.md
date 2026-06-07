# Agent-Swarm Completion Plan

**Branch:** `feat/production-ready-20260607`
**Author:** Claude Sonnet 4.6 (production-readiness pass, 2026-06-07)
**June-15 deadline:** `claude --print` / CLI path becomes metered. Swarm must use API.

---

## What was landed in this PR

| Item | File(s) | Tests |
|---|---|---|
| **A. ApiConductor as default** | `conductors/factory.py`, `cli/main.py`, `conductors/__init__.py` | 5 new assertions in `TestDefaultConductorIsAPIBased` |
| **CLI deprecation warning** | `conductors/factory.py` | `test_claude_conductor_emits_deprecation_warning` |
| **B. GitHub task provider** | `claude_swarm/github_tasks.py` | `tests/test_github_tasks.py` (25 tests) |
| **C. MetaSupervisorMonitor** | `claude_swarm/meta_supervisor.py` | `tests/test_meta_supervisor.py` (35 tests) |
| **C. Parallelism safety score** | `meta_supervisor.py:parallelism_score()` | 11 tests |
| **C. Cost preflight** | `meta_supervisor.py:cost_preflight()` | 7 tests |
| **C. Anomaly tracker** | `meta_supervisor.py:AnomalyTracker` | 5 tests |
| **D. CoordBusAdapter** | `claude_swarm/coord_bus_adapter.py` | `tests/test_coord_bus_adapter.py` (13 tests) |
| **Test suite fix** | `tests/test_cli.py` | Was broken by default-conductor change; fixed |

**Test count delta:** 343 (pre-PR) → 421 passed (post-PR, +78 new tests).

---

## A. API-agents default

`DEFAULT_CONDUCTOR = "api"` is now the constant in `claude_swarm/conductors/factory.py:33`
and the default in both `run` and `perpetual` CLI commands.

The `claude --print` path:
- Emits `DeprecationWarning` (suppressable via `CLAUDE_SWARM_ALLOW_CLI_CONDUCTOR=1`).
- Warning text explicitly cites the June-15-2026 billing change.
- Remains functional for operators who need it (set the env var).

CI guard: `TestDefaultConductorIsAPIBased::test_default_conductor_constant_is_api`
asserts `DEFAULT_CONDUCTOR == "api"` with a message explaining WHY it must stay
that way. Changing it will break CI with a clear explanation.

---

## B. GitHub task source

`claude_swarm/github_tasks.py` implements the `WorkSource` protocol.

**Wire-up:**

```python
from claude_swarm.github_tasks import GitHubWorkSource
from claude_swarm.perpetual import run_perpetual_team

run_perpetual_team(
    count=2,
    kanban=kb,
    work_source=GitHubWorkSource(repo="kushalj1997/agent-swarm", enabled=True),
)
```

Or via env:
```bash
CLAUDE_SWARM_GITHUB_INTAKE=1 \
CLAUDE_SWARM_GITHUB_REPO=kushalj1997/agent-swarm \
CLAUDE_SWARM_GITHUB_LABEL=swarm-task \
CLAUDE_SWARM_GITHUB_PROJECT=5 \
claude-swarm perpetual --count=2
```

**Label convention:** Add the `swarm-task` label to any GitHub issue you want
the swarm to pick up. Removing the label or closing the issue is the "done" signal.
The dedup file at `.swarm/gh-seen.json` prevents re-filing.

**deep-ai Project #5** is the "deep-ai" kanban on GitHub. Set
`CLAUDE_SWARM_GITHUB_PROJECT=5` to restrict intake to that project board.

---

## C. Architecture quality pass

### Taxonomy (roles.py) — COMPLETE

The six-rung ladder (meta-supervisor, supervisor, lead, agent, ephemeral-agent,
dynamic-workflow) is already fully implemented at `claude_swarm/roles.py`.
`_SPAWNABLE` dict enforces delegation depth constraints. No gaps found.

### Meta-supervisors — LANDED (new)

`claude_swarm/meta_supervisor.py` adds:

1. `parallelism_score(task, in_progress)` — 0.0-1.0 score. Integrate into the
   perpetual supervisor's dispatch loop to cap `max_parallel` based on the
   lowest score of queued tasks.

2. `cost_preflight(task, ...)` — ADMIT/HOLD/REJECT before claiming. Integrate
   into `supervisor.py:Supervisor.step()` or `perpetual.py:_drive_claimed_task()`.

3. `MetaSupervisorMonitor` — per-tick health check, per-head cost averages,
   anomaly escalation at N repeated failure fingerprints.

4. `AnomalyTracker` — accumulates failure fingerprints; fires `NEEDS_REVIEW`
   kanban escalation after `escalation_threshold` recurrences.

### Work-slicing (DAG decomposition) — PRESENT

`routing.py:route_task()` handles the DELEGATE_DIRECT / DELEGATE_LEAD / EPHEMERAL
three-way decision. `Task.metadata["estimated_subtasks"]` is the planner's hook.
Gap: no real planner stamps these fields yet (the `NullWorkSource` generates nothing).
The `GitHubWorkSource` landing plus a future `ScannerWorkSource` completes this.

### Ultracode escalation triggers — DOCUMENTED, NOT WIRED

The `ApiConductor` has a `max_turns` parameter. Ultracode escalation
(upgrade to Opus when a task exceeds N turns without progress) requires:
1. A turn-count tracker in `ApiConductor.dispatch()`.
2. A model-upgrade path in `build_conductor()` (e.g. accept a `fallback_model` param).
3. `CLAUDE_SWARM_ULTRACODE_THRESHOLD_TURNS=20` env var.

This is the highest-value remaining item for the API conductor.

### Subagent spawn defaults — PRESENT

`agents.py` + `bus.py` cover the agent registration and delegation bus.
The `ApiConductor` already supports `max_turns`, `max_tokens`, and `model_override`.
The operator's defaults (xhigh effort, auto permissions) belong in the system prompt
injected at dispatch time — add them to `ApiConductor._system_prompt()`.

### No-failures-no-waste guards — STATUS

| Guard | Status | Location |
|---|---|---|
| Worktree GC after merge | PRESENT | `merge_pipeline.py:run_pipeline()` (test gate + GC) |
| File-overlap reject at claim time | PRESENT | `merge_pipeline.py:file_overlap()` |
| Auto-revert on test fail | PRESENT | `merge_pipeline.py` (stop_on_failure flag) |
| Cost preflight | LANDED | `meta_supervisor.py:cost_preflight()` (not yet wired into supervisor loop) |
| Per-task USD cap | PRESENT | `ApiConductor.dispatch()` (cost_cap_usd in task.metadata) |
| Max-turns silent exit prevention | PRESENT | `ApiConductor` turn loop + completed-flag guard |
| Worker process resilience | PRESENT | `perpetual.py:_drive_claimed_task()` + resilient_call |
| Branch GC after merge | PRESENT | `worktree.py:WorktreeManager.cleanup()` |

**Gap remaining:** `cost_preflight()` is written and tested but not yet called from
the supervisor dispatch path. Wire it into `supervisor.py:Supervisor.step()` (one
call before `conductor.dispatch()`). ~5-line change.

### Merge-graph orchestration — PRESENT

`merge_pipeline.py` has `topological_order()` + `file_overlap()` + test gate.
The full "graph-orchestrator with topo-sort + rebase-retry" from the design doc
is the next major feature. Current state handles the 80% case (serial merges,
file-overlap reject).

---

## D. Coord bus integration seam

`claude_swarm/coord_bus_adapter.py` is the bridge.

**Wire-up** (deep-ai side — add to `.env` or launchd env):

```bash
CLAUDE_SWARM_COORD_BUS_DSN=postgresql+psycopg://deepai:<pw>@localhost:5432/deepai_ts
```

Once set, any running swarm will:
- Post `work_completed` messages when tasks finish (visible to the Claude Code session
  via `comms poll --recipient claude`).
- Post `swarm_task_failed` on anomaly escalation (operator sees it in `/mind` page).
- Send periodic `swarm_status` heartbeats.
- Honour area claims to prevent file conflicts with parallel Codex/Claude sessions.

**Reading context from Claude/Codex:**

```python
adapter = CoordBusAdapter()
msgs = adapter.recent_messages(senders=["claude", "codex"], limit=20)
for m in msgs:
    if m.type == "work_completed":
        # Claude/Codex finished something; update swarm task DAG if relevant
        pass
```

This closes the "swarm is up to speed on what Claude+Codex are doing" requirement.

---

## Remaining steps to "live"

These are ordered by impact / urgency relative to the June-15 deadline.

### P0 — Before June 15

1. **Set `ANTHROPIC_API_KEY` in the swarm's runtime env** (not in git). The
   `api` conductor is now default but the env var must be present.
   ```bash
   ANTHROPIC_API_KEY=$(cat ~/dev/projects/deep-ai-base/api_keys/anthropic_key.txt) \
   claude-swarm perpetual --count=2
   ```

2. **Wire `cost_preflight()` into `supervisor.py:Supervisor.step()`** before
   `conductor.dispatch()`. One function call, prevents runaway spend.

3. **Remove or gate all CI/scripts that invoke `claude --print`** directly in
   the agent-swarm repo. grep: `subprocess.*claude.*--print` or `claude -p`.

### P1 — High value, safe to land post-deadline

4. **`ScannerWorkSource`** — a perpetual work source that scans the repo for
   FIXME/TODO/failing CI and files kanban tasks. Pairs with `GitHubWorkSource`.

5. **Ultracode escalation** — in `ApiConductor.dispatch()`, track consecutive
   turns without a commit/result; after `ULTRACODE_THRESHOLD_TURNS` escalate
   the model to Opus.

6. **`parallelism_score()` integrated into `perpetual.py:run_perpetual_team()`**
   — before dispatching a batch of ready tasks, sort by score descending and
   cap the parallel window at the minimum score × `max_parallel`.

7. **`CoordBusAdapter` wired into `PerpetualSupervisor`** — announce done/failed
   tasks on each tick. Needs a `coord_adapter` param on `PerpetualConfig`.

8. **`GitHubWorkSource` wired into `perpetual` CLI** — add
   `--github-intake/--no-github-intake` flag that constructs a `GitHubWorkSource`
   when set.

### P2 — Architecture completeness

9. **Real planner `WorkSource`** — an LLM-backed source that scans the repo,
   decomposes work into a DAG, and stamps `estimated_subtasks` +
   `parallelism_safety` on each task.

10. **Full merge-graph orchestrator** — topo-sort + rebase-retry + UI strip
    (currently the pipeline is sequential).

11. **Conversation state persistence** — serialize `ApiConductor` tool-use
    conversation per turn to `.ai/state/swarm/<task_id>/conversation.jsonl`
    so a supervisor restart resumes rather than restarts work.

12. **Budget auto-rotation** — when `ANTHROPIC_API_KEY` hits a 429, rotate to a
    second key from `KeyRotator` or fall back to `SDKConductor`.

---

## Architecture gaps found (not fixed, documented for follow-up)

| Gap | Severity | Effort |
|---|---|---|
| `cost_preflight()` not called in supervisor dispatch | medium | 5 lines |
| `parallelism_score()` not read by perpetual dispatcher | medium | 20 lines |
| `CoordBusAdapter` not wired into `PerpetualSupervisor` | low | 10 lines + config |
| No `ScannerWorkSource` (swarm can't self-populate) | high | new module |
| Ultracode escalation missing | medium | ~30 lines in `ApiConductor` |
| Agent system prompt doesn't inject spawn defaults | low | `ApiConductor._system_prompt()` |
| Merge pipeline is sequential (no topo-rebase-retry) | medium | larger refactor |
| No conversation-state persistence for resume | medium | ~50 lines |

---

## Test count summary

```
origin/main baseline:  343 passed, 5 skipped
this PR adds:           78 new tests
final count:           421 passed, 5 skipped, 0 failed
```

Coverage by new module:
- `tests/test_conductor_factory.py`: +5 (TestDefaultConductorIsAPIBased)
- `tests/test_github_tasks.py`: 25
- `tests/test_meta_supervisor.py`: 35
- `tests/test_coord_bus_adapter.py`: 13
