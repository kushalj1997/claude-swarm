# claude-swarm Autonomy Architecture

**Status:** design — branch `feat/autonomy-architecture-20260529`
**Author:** Chief Architect synthesis of 4 audits (capability map · Claude-API surface · Claude-Code parity · bus expansion)
**Goal:** turn `claude-swarm` from a clean orchestration *skeleton* into an autonomous, API-driven, rate-resilient engineering org that **equals or exceeds Claude Code** and **coordinates Claude Code + Codex + Cursor over one bus**.

Every file:line in this doc is verified against the two repos it draws from:
- `claude-swarm` (`/Users/jollygama/dev/projects/claude-swarm`) — the OSS skeleton we are extending.
- `deep-ai` (`dm/deep_manager/swarm/*`, `dm/deep_manager/coord/comms.py`) — the mature private reference implementation we port patterns *from*.

---

## 0. The one-paragraph thesis

`claude-swarm` today is a **single-writer supervisor poll loop** (`supervisor.py:129 step()`) over a **SQLite DAG kanban** (`kanban.py:343 unblocked()`, `:371 claim_one()`) dispatching to **six declarative heads** (`heads/__init__.py:162 default_roster()`) through a **pluggable Conductor seam** (`supervisor.py:37 Conductor` protocol; default `StubConductor` at `:57`, real `ClaudeCLIConductor` at `conductor.py:80`). It is correct, tested (86 test fns, ≥80% gate), and dependency-light — but it is **inert**: it drains a kanban someone else fills and stops. The deep-ai swarm already solved the hard parts privately — a real Anthropic tool-use worker loop with prompt caching (`worker.py:748 tool_use_loop`, cache_control at `:1413`), a hard cost cap that actually aborts (`worker.py:455`), a must-ship guard (`worker.py:991`), hierarchical delegation with depth limits (`delegation.py:191 decompose`, `MAX_DELEGATION_DEPTH`), a per-model price table (`cost.py:24 ModelPrice`), token-rate snapshots (`token_rates.py:102 compute_token_rates`), federation/coordinator-election (`federation.py:141 elect_coordinator`), and a postgres coord bus for Claude↔Codex↔Slack (`comms.py`). **This architecture ports those proven patterns into claude-swarm's clean public surface, then adds the three things neither repo has yet: (1) a never-sleep supervisor *team* that generates its own work, (2) usage-limit-aware auto-switching between cheap subscription plans and the metered API, and (3) one expanded bus that lets a supervisor hand a small TASK to *any* agent type — Claude Code, Codex, Cursor, or an API worker — and get a fast, kanban-linked, status-tracked response.**

---

## 1. The end-state picture (what "done" looks like)

```
                         ┌─────────────────────────────────────────────────┐
                         │           NEVER-SLEEP SUPERVISOR TEAM            │
                         │  (perpetual async loops, never idle, rate-aware) │
                         ├─────────────────────────────────────────────────┤
   scans product+code →  │  ScoutSupervisor   — finds bugs/gaps, /deep-research
   plans & decomposes →  │  PlannerSupervisor — turns findings into a TASK DAG
   dispatches & verifies→ │  DispatchSupervisor — claims, delegates, verifies, merges
   watches the watchers → │  MetaSupervisor    — health, election, rate-mode, restart
                         └───────────────┬─────────────────────────────────┘
                                         │ submit / claim / status / delegate
                              ┌──────────▼───────────┐
                              │   KANBAN (DAG, SQLite)│  ← single source of truth for WORK
                              │   kanban.py           │     (one writer; many readers)
                              └──────────┬───────────┘
                                         │
                ┌────────────────────────┼────────────────────────┐
                │                        │                         │
   ┌────────────▼──────────┐ ┌───────────▼───────────┐ ┌───────────▼──────────┐
   │  API WORKER POOL       │ │  EXTERNAL-AGENT ADAPTERS │ │  RATE / USAGE GOVERNOR │
   │  (Anthropic tool-loop) │ │  Claude Code · Codex ·   │ │  plan-limits tracker + │
   │  conductor=ApiConductor│ │  Cursor   (over the bus) │ │  auto-switch policy    │
   └────────────┬──────────┘ └───────────┬───────────┘ └───────────┬──────────┘
                │                         │                         │
                └─────────────────────────┴──── BUS (coord, all agent types) ───┘
                                         │
                              ┌──────────▼───────────┐
                              │  MERGE PIPELINE       │  rebase → /code-review + adversarial
                              │  merge_pipeline.py    │  → test gate → push → master-clean
                              └──────────┬───────────┘
                                         │
                              endpoints redeploy → LIVE (operator-visible surface)
```

Invariants the end-state must hold (charter §16 "status labels lie", §21 merge discipline, §0 wholeness):
1. **Never idle.** A loop with empty kanban *generates* work, it does not return.
2. **Never stalled by rate limits.** A 429 backs off + rotates model/plan/key; it never kills the org.
3. **Master always deployable.** Every merge passes `/code-review` + an **independent adversarial verifier** before landing (charter §0 Directive-3 adversarial pass; global §0 workflow contract).
4. **No lost work.** Every loop checkpoints WIP to the kanban + an abort marker before any fragility point.
5. **End at the consumer.** "done" means the operator-visible surface changed, not that a label flipped.

---

## 2. Taxonomy as code

The audit (Audit 3) maps Claude Code primitives onto a swarm taxonomy. We encode that taxonomy as **explicit roles**, not implicit behavior, so it is testable and the hybridization the operator wants ("supervisors delegate small TASKS to agents, not only to leads") is a first-class rule rather than a convention.

### 2.1 The role ladder (new module `claude_swarm/roles.py`)

| Role | Owns | Spawns | Backing pattern (deep-ai reference) |
|---|---|---|---|
| **meta-supervisor** | health of all supervisors, coordinator election, rate-mode, restart-on-silence | supervisors | `federation.py:141 elect_coordinator`, `:317 HeartbeatLoop` |
| **supervisor** | one perpetual loop (scout/planner/dispatch); claims work; **delegates small TASKS directly to agents OR to leads** | leads, agents, ephemerals | `supervisor.py:129 step()` extended to perpetual |
| **lead** (manager) | decomposes a *large* task into a sub-DAG; reviews children; merges children | agents, ephemerals | `delegation.py:191 decompose`, `:233 await_subtasks` |
| **agent** | executes ONE task end-to-end as a tool-use loop; commits; opens a PR | ephemerals (sub-tasks ≤ depth limit) | `worker.py:187 run()` tool-use loop |
| **ephemeral-agent** | one bounded sub-step (a single review, a single test run); no PR; returns a structured verdict | — | reviewer/test-runner heads, `worker.py:991 must-ship` guard |
| **dynamic-workflow** | a fan-out of independent agents that converge (the `/simplify`+`/code-review`-until-convergence cycle) | agents | `supervisor.py _run_parallel` + new `WorkflowRunner` |

### 2.2 The hybridization rule (the operator's explicit ask)

> "supervisors hand small TASK chunks to agents (not only leads)"

Encoded as a **routing decision on the supervisor**, in a new `claude_swarm/routing.py`:

```
route(task) ->
  if task.estimated_subtasks <= 1 and task.files_owned bounded:
      DELEGATE_DIRECT  # supervisor → agent (ApiConductor or external adapter)
  elif task.estimated_subtasks > 1 or task.spans multiple owners:
      DELEGATE_LEAD    # supervisor → lead → decompose() → child agents
  else:
      EPHEMERAL        # one-shot verdict (review / test / audit)
```

- `estimated_subtasks` is a cheap heuristic (token-budget × file-count) the **planner** stamps onto `Task.metadata` when it files the task.
- This is the difference between the current code (every dispatch goes to one head, no decomposition at the supervisor layer) and the target: the supervisor itself fans small chunks straight to agents, escalating to a lead only when the task is genuinely compound. Mirrors how a senior engineer hands a one-file fix straight to a junior but routes a feature through a tech-lead.

### 2.3 Why "agent" and "lead" are both first-class

The current `Supervisor._pick_head` (`supervisor.py:122`) only maps `required_head → Head`. We extend `Task` with a `route` field (`DELEGATE_DIRECT | DELEGATE_LEAD | EPHEMERAL`, default `DELEGATE_DIRECT`) so the supervisor's dispatch path branches without breaking the existing single-head path. Backwards-compatible: an un-stamped task defaults to direct dispatch exactly as today.

---

## 3. The never-sleep supervisor team

The single biggest gap: `Supervisor.run()` (`supervisor.py:174`) **returns** when the kanban drains unless `wait_for_work=True` (`:206`), and even in daemon mode it only *polls* — it never *creates* work. The autonomous org needs loops that generate their own backlog. We add three perpetual supervisor specializations + a meta-supervisor, each a thin subclass over the existing `Supervisor`.

### 3.1 ScoutSupervisor — finds work

New `claude_swarm/supervisors/scout.py`. On every tick (with jittered cadence, default 15 min heartbeat, event-driven when the bus signals):
1. Pulls a **rotating slice** of the product surface + codebase (one subsystem per tick, round-robin, so no single tick is huge and the prompt cache stays warm).
2. Runs the **`/deep-research` harness end-to-end** on that slice (the `deep-research` skill is available: fan-out web/code searches → fetch sources → adversarially verify → cited synthesis). Scope is "what is missing, wrong, stale, or undertested in subsystem X?"
3. For every finding with file:line evidence, files a kanban task (`kanban.submit`) tagged `source:scout`, with `estimated_subtasks` and `files_owned` stamped.
4. **Dedups before filing** (global memory: `feedback_kanban_dedup_before_file`) — greps open tasks for an overlapping title/files before `submit`.
5. Returns *nothing to caller* — it loops. Empty findings → still loops on the next slice.

Rate-resilience: Scout uses the **cheapest model that can do the job** (Haiku-class for the scan pass, escalating to Sonnet only for the verify pass) and runs under the rate governor (§6). Its deep-research calls use **prompt caching** on the repo-corpus + schema system blocks so repeated scans are near-free on cache reads (`cost.py` cache_read SKUs: Opus $0.50, Sonnet $0.30, Haiku $0.08 per 1M).

### 3.2 PlannerSupervisor — shapes work into a DAG

New `claude_swarm/supervisors/planner.py`. Consumes `source:scout` (and operator-filed) tasks that are still un-shaped:
1. Reads the finding, decides `route` (§2.2) and decomposition.
2. For compound findings, lays out the sub-DAG edges (`kanban.add_blocked_by` / `add_blocks`, already implemented at `kanban.py:275/306`) so the topological iterator (`unblocked()`) releases children in order.
3. Stamps `estimated_subtasks`, `files_owned`, `priority`, `max_tokens`, and the **acceptance criteria as a checklist** into `Task.metadata` (global §3.5: concrete observable criteria, not "tests pass").
4. Marks the task ready for dispatch.

This is the seam where TodoWrite/plan parity lives (Audit 3 maps plan → DAG + lead). The planner is what makes the DAG *self-populating* instead of operator-populated.

### 3.3 DispatchSupervisor — executes, verifies, merges

New `claude_swarm/supervisors/dispatch.py`, the perpetual evolution of today's `Supervisor`:
1. `claim_one()` the next unblocked task (existing atomic path, `kanban.py:371`).
2. Route per §2.2: direct→agent, compound→lead, one-shot→ephemeral.
3. On agent completion, run the **convergence workflow** (§4.2): `/simplify` + `/code-review` in cycles until a pass finds nothing material, then an **independent adversarial verifier** in a fresh-context agent that tries to *refute* the fix.
4. Only on adversarial PASS, hand to the **merge pipeline** (`merge_pipeline.py`, already present): rebase → test gate → push.
5. After merge: trigger endpoint redeploy hook, then verify the operator-visible surface changed (charter §16 — "one hop past the label").
6. Loop. Never returns on empty; emits a heartbeat and waits on the bus / poll.

### 3.4 MetaSupervisor — watches the watchers

New `claude_swarm/supervisors/meta.py`, porting `federation.py` patterns:
- **Singleton election** (`federation.py:141 elect_coordinator`) so duplicate supervisors can't both write the kanban (charter §10 single-supervisor invariant — the $155 incident).
- **Silent-heartbeat detection** (`federation.py` `MetaSupervisorWatch` stub → finished here): a supervisor that hasn't ticked in N× its cadence is declared stuck (charter §1 "running ≠ working"); meta restarts it after committing its WIP via the abort marker.
- **Rate-mode authority** (§6): meta owns the global "are we in cheap-plan mode or API mode?" flag and broadcasts switches on the bus.
- Enforced **at the OS level too** with a pidfile/flock (charter §7 singleton lockfile), because election alone doesn't survive a hard crash.

### 3.5 Backoff / rotation so rate limits never stall the team

Every perpetual loop wraps its LLM calls in a **resilient call** helper (new `claude_swarm/resilience.py`):
- On HTTP 429 / `overloaded_error` / `pause_turn`: exponential backoff with jitter (cache-aware — never a bare `sleep(300)` that wastes the 5-min cache TTL; charter §10 → use 270s or ≥1800s).
- On repeated 429 within a window: **rotate** to the next entry in a priority list (model → plan → API key → external adapter). The governor (§6) supplies the rotation order.
- `pause_turn` (server-tool 10-iteration cap, Audit 2) is *resumed*, not treated as failure: re-send `[user, assistant(response.content)]`.
- Backoff state is per-loop so one throttled loop doesn't block the others.

---

## 4. Claude-Code-parity features

The target is "equals/exceeds Claude Code." Each Claude Code primitive gets a swarm-native implementation. The deferred-tool list in this very environment (CronCreate, Monitor, TeamCreate, SendMessage, EnterWorktree, Skill, …) *is* the Claude Code feature surface; we replicate each over the raw API + the kanban/bus state plane.

### 4.1 Skills — including `/simplify` and `/code-review`

- **Mechanism:** a skill is a named capability bundle. In the swarm it becomes a **role prompt + a dedicated pass in the agent loop** (Audit 3). New `claude_swarm/skills/` holds markdown bundles loaded into the worker's system blocks **as cached blocks** (so loading a skill costs one cache write, then cache reads).
- **`/code-review` parity** = the reviewer role + a structured verdict written back to the PR record: `{"verdict": "approve|request-changes|block", "findings": [{file, line, severity, why}]}`. This is the bug/security/edge-case/convention pass.
- **`/simplify` parity** = a separate behavior-preserving cleanup pass (reuse, simplification, efficiency, altitude) that **must not change behavior** — gated by re-running the test suite after it applies (any test delta = reject the simplify diff).
- Both are available as standalone skill bundles AND wired into the convergence workflow (§4.2).

### 4.2 Workflows — the simplify+review-until-convergence cycle

New `claude_swarm/workflow.py` `WorkflowRunner`. Encodes the global-charter "workflow keyword" contract:
1. **Fan-out** the work across agents (uses the existing `_run_parallel` thread pool, `supervisor.py:211`, generalized to the API worker pool).
2. **Cycle** `/simplify` then `/code-review` until a full pass finds nothing material to change (convergence). Each cycle re-runs tests; a regression bounces the diff.
3. **Adversarially verify** every fix in an INDEPENDENT, fresh-context agent that tries to refute it, *before* it lands a PR.
This is the operator's standing definition of "workflow"/"parallel" and binds every agent, including the external Codex/Cursor adapters.

### 4.3 Monitors — react to events, don't poll

The deferred `Monitor` tool is Claude Code's event-reactor. Swarm equivalent: the bus's `LISTEN/NOTIFY` (deep-ai `comms.py:240-263` `pg_notify('coord_msg', …)`) wakes loops on real events (Jenkins done, master advanced, kanban label, PR check flip) with heartbeat fallbacks — exactly the deep-ai loop-supervisor model (CLAUDE.md §23). Charter §10: don't poll in a sleep loop. New `claude_swarm/monitor.py` exposes `on(event, handler)` over the bus.

### 4.4 Loops + goals

- **Loops** (Claude Code `/loop`) = the perpetual supervisors themselves (§3). A loop has a **goal** (a standing acceptance predicate) and runs until the goal holds AND no new work is found — then heartbeats.
- **Goals** are encoded as predicates the loop evaluates each tick (e.g. "every operator-visible field is non-NULL and fresh" — deep-ai zero-NULL invariant). A goal that fails files a kanban task; a goal that holds lets the loop idle-heartbeat.

### 4.5 Tools + MCP

- The current heads carry `allowed_tools` tuples (`heads/__init__.py:100-158`) that are **descriptive only** — Audit 1 confirms they are never passed to the CLI (`conductor.py:99` builds the command with no `--allowedTools`). **Fix:** the new `ApiConductor` (§5) passes each head's `allowed_tools` as the actual tool schema list to `messages.create`, so the allowlist becomes *enforced* tool restriction (charter §6 destructive-tool gating). For the CLI conductor, pass `--allowedTools`.
- **MCP** (Audit 2/3): the worker registers MCP servers (postgres-ro, chrome-devtools, computer-use, slack, cloudflare — all present in this environment's deferred list) as tool sources. **Tool search** (server tool) appends only the relevant schemas per task instead of loading all upfront, preserving the cache (Audit 2). **Programmatic tool calling** runs multi-tool chains in the code-execution container so intermediate results never hit context (token cost scales with final output, not the chain).

### 4.6 Parity scorecard

| Claude Code primitive | Swarm module | Status |
|---|---|---|
| session/agent loop | `worker.py` (port) → `ApiConductor` | port from deep-ai |
| Task/Agent spawn | `delegation.py decompose` (port) | port |
| skills (`/simplify`,`/code-review`) | `claude_swarm/skills/` + `workflow.py` | new |
| workflows / parallel | `workflow.py WorkflowRunner` | new |
| monitors | `monitor.py` over bus NOTIFY | new |
| loops / goals | `supervisors/*` perpetual | new |
| tools / allowlists | `ApiConductor` enforces `allowed_tools` | fix existing |
| MCP / tool-search / PTC | worker MCP registration | port + new |

---

## 5. Every Claude-API power for cost / rate resilience

All of these route through one endpoint (`POST /v1/messages`, Audit 2). The deep-ai worker already uses the core ones; we port them and add the rest.

| API power | Mechanism | Swarm use | Reference |
|---|---|---|---|
| **Prompt caching** | `cache_control:{type:ephemeral, ttl}` on stable system/tool/context blocks | Cache the repo corpus, schema, skill bundles, role prompts once → near-free cache reads on every subsequent worker. The biggest single cost lever. | deep-ai `worker.py:1413 _mark_cache_blocks`, `:1426` |
| **Batch API** | submit many independent tasks as one batch (50% cheaper, async) | Scout's per-subsystem scans + bulk reviews go through batch when latency-insensitive. | deep-ai `batch_runner.py` |
| **Memory tool** | persistent cross-session memory directory the model reads/writes | Each loop's durable memory (what it scanned, what it filed) so it doesn't re-derive across sessions. | deep-ai `agent_memory.py` |
| **Parallel tool use** | one response carries N `tool_use` blocks; execute all, return all in one user turn (on by default) | Free throughput inside every agent. | Audit 2 |
| **Extended thinking** | `thinking` budget for hard planning/verification steps | Planner decomposition + adversarial verifier get a thinking budget; cheap scans don't. | Audit 2 |
| **MCP** | external tool sources over MCP | postgres-ro, chrome-devtools, computer-use, slack, cloudflare. | §4.5 |
| **Files API** | upload large artifacts once, reference by id | Big diffs / datasets passed by file id, not inline tokens. | deep-ai `anthropic_files.py` |
| **Tool search / PTC** | append only relevant tool schemas; run chains in code-exec container | Keeps the cache warm + keeps intermediate tool results out of context. | Audit 2 |
| **Manual agentic loop** | not the beta tool-runner — the swarm drives the loop itself | Lets the supervisor gate destructive tools + audit each call (charter §6). | Audit 2 |

**Cost model is already specified** in deep-ai `cost.py:24 ModelPrice` (USD per 1M tokens, input / cache-write-5m / cache-write-1h / cache-read / output for Opus/Sonnet/Haiku/Gemini). Port `cost.py` so the governor (§6) can price every call and the `swarm-cost` rollup is real.

---

## 6. Usage-limits tracking + the auto-switch policy

The operator wants: **run on cheap subscription plans by default; when those hit their usage limit, switch to the metered API swarm to stay autonomous; switch back when the cheap plan resets.** This is the rate-resilience heart.

### 6.1 What we track (new `claude_swarm/usage.py`)

Port + extend deep-ai `token_rates.py:41 AgentRateEntry`, `:71 TokenRateSnapshot`, `:102 compute_token_rates`:
- Per **provider lane** (Claude-Code-Max-plan-A, Max-plan-B, Codex-plan, Cursor-plan, metered-API-key): rolling token + request counts, last-429 timestamp, observed reset window.
- Per **model**: priced spend via `cost.py` so we know real USD, not just token counts.
- A **headroom estimate**: tokens-remaining-before-limit per lane, inferred from observed 429s + plan caps.

### 6.2 The auto-switch state machine (new `claude_swarm/governor.py`)

```
            ┌───────────────┐  cheap-plan headroom > threshold
   START ──▶│  CHEAP_PLANS  │◀──────────────────────────────┐
            └──────┬────────┘                                │
                   │ any cheap lane 429 / headroom ≤ 0       │ cheap-plan reset
                   ▼                                          │  window elapsed
            ┌───────────────┐                                │
            │  API_SWARM    │────────────────────────────────┘
            └──────┬────────┘
                   │ API spend ≥ daily budget cap (charter §10)
                   ▼
            ┌───────────────┐
            │  THROTTLED    │  batch-only + Haiku-only until a lane frees up
            └───────────────┘
```

- **CHEAP_PLANS** (default): dispatch to Claude Code / Codex / Cursor adapters via the bus (subscription-billed, ~free at the margin). Rotate among plan lanes by headroom.
- **API_SWARM**: dispatch to the in-process `ApiConductor` worker pool (metered, but never rate-stalled because we own the backoff/rotation and have a budget cap).
- **THROTTLED**: both exhausted — drop to Batch-API + Haiku only, lengthen cadences, keep the org *alive* but slow. Never fully stops.
- The **MetaSupervisor owns the flag** and broadcasts transitions on the bus so every loop and adapter respects the current mode. Transitions are logged so the operator can see *why* the org switched lanes (charter §16: surface the actual numbers).

### 6.3 The cheap-plan adapters are the same bus that talks to external agents

Critical insight: "switch to cheap plans" and "delegate to Claude Code / Codex / Cursor" are the **same mechanism** — a TASK handed over the bus to an external agent that bills against a subscription. The governor just changes *which lane* the dispatcher prefers. This collapses two operator asks (rate-resilience + multi-agent-coordination) into one subsystem.

---

## 7. The expanded bus (all agent types + task delegation)

### 7.1 The gap (Audit 4, verified)

There are **two disconnected coordination systems** in deep-ai:
- the **postgres coord bus** (`comms.py`) for Claude↔Codex↔Slack↔operator — human-facing messaging, with restrictive enums: `VALID_SENDERS={claude,codex,slack,operator}` (`comms.py:89`), `VALID_RECIPIENTS={claude,codex,all,operator}` (`comms.py:90`), enforced by both Python `_validate_send_args` (`comms.py:349`) AND postgres CHECK constraints.
- the **SQLite kanban** (`swarm/kanban.py`) for intra-swarm-worker delegation.

The swarm supervisor/worker **do not use the coord bus at all** — only `slack/relay.py` and `__init__.py` import `CoordBus`. So a supervisor cannot hand a TASK to Codex or Cursor over a bus today; the two planes never meet.

### 7.2 The fix — a unified TASK-delegation bus

The bus must carry both **messages** (coordination chatter) and **task delegations** (work handoffs with a kanban link + status), to/from **any agent type**.

**Expand the enums** (in claude-swarm's `messaging.py`, and mirror in deep-ai `comms.py`):
- `senders / recipients` gain: `cursor`, `api-worker`, `scout`, `planner`, `dispatch`, `meta`, plus a wildcard per-class (`agent:*`).
- `msg_types` gain task-lifecycle verbs: `task_delegated`, `task_claimed`, `task_progress`, `task_done`, `task_blocked`, `task_failed`, `verify_request`, `verify_result` — alongside existing `work_started/work_completed/handoff/question/answer/ack/heartbeat/blocker`.

**New message shape for a delegation** (rides on the existing `payload` JSONB + structured columns `task_ref`, `branch`, `pr_number`, `summary`, `status`):
```
{ sender: "dispatch", recipient: "codex",
  msg_type: "task_delegated",
  task_ref: "<kanban-task-id>",          # the kanban link the operator asked for
  payload: { prompt, files_owned, acceptance, route, deadline_s,
             base_sha, worktree_hint } }
```
The recipient adapter (Claude Code / Codex / Cursor / api-worker) claims it, runs it, and emits `task_progress` heartbeats then `task_done` with `pr_number` + `branch`. The dispatcher updates the kanban row from those messages — so **status flows kanban ↔ bus in both directions** and every delegation has a live, operator-visible status (charter §9 tile UX reflects reality).

### 7.3 External-agent adapters (new `claude_swarm/adapters/`)

One adapter per agent type, each translating a `task_delegated` bus message into that agent's native invocation and translating its result back to `task_done`:
- `adapters/claude_code.py` — invokes `claude --print` (or the in-session Agent tool when running inside Claude Code) with the task prompt + allowlist; this is the deep-ai `keepalive` path (`swarm-submit` skill → `claude --print`).
- `adapters/codex.py` — uses the existing `codex:rescue` / `codex-cli-runtime` seam (the Codex plugin in this environment) so a TASK can be handed to Codex's GPT-5.4 lane.
- `adapters/cursor.py` — posts a `task_delegated` to the `cursor` recipient; Cursor's headless loop (deep-ai CLAUDE.md §25 ownership) consumes it. Respects Cursor's headless constraints (no GUI/computer-use — §25).
- `adapters/api_worker.py` — the in-process `ApiConductor` pool (§5); this is the API-swarm lane.

All four implement one `Adapter` protocol: `dispatch(task) -> stream of bus messages`. The governor (§6) picks which adapter gets each task by current rate-mode + the task's `required_head`/`route`.

### 7.4 Backwards-compatibility

The deep-ai postgres CHECK constraints (`comms.py`) currently reject the new enum values. The bus expansion ships as: (1) alembic migration to widen the CHECK constraints in `coord_db`, (2) Python enum widening in lockstep (charter §20 five-artifact contract for any schema change), (3) a compatibility shim so existing `{claude,codex,slack,operator}` traffic is unaffected. The claude-swarm `MessageBus` (filesystem JSON inbox, `messaging.py:130`) stays as the dependency-light default for OSS users; a `PostgresBus` subclass (Audit 4) is the production transport that talks to `coord_db`.

---

## 8. The end-to-end autonomous loop

The full chain the operator wants — **bug → kanban → delegate → fix → /code-review + adversarial → merge → endpoints → redeploy → LIVE** — autonomous, fast, master-clean, no-lost-work:

```
 1. SCOUT      finds a bug/gap via /deep-research on a code+product slice
                → files kanban task (file:line evidence, dedup-checked)         [§3.1]
 2. PLANNER    shapes it: route + sub-DAG + acceptance checklist + files_owned  [§3.2]
 3. DISPATCH   claim_one() → route():
                  small  → delegate TASK directly to an agent (governor picks lane) [§2.2,§6]
                  compound → lead.decompose() → child agents                     [§2.1]
 4. AGENT      tool-use loop in an isolated worktree; commits; opens a PR        [§5]
                  (must-ship guard: end_turn with zero artifacts = failure)      worker.py:991
 5. WORKFLOW   /simplify + /code-review in cycles until convergence             [§4.2]
                  then INDEPENDENT adversarial verifier (fresh context) refutes  [§4.2]
 6. MERGE      only on adversarial PASS: rebase → test gate → push (master-clean)[§3.3]
                  topo-order by file overlap (merge_pipeline.py)
 7. REDEPLOY   trigger endpoint redeploy hook
 8. VERIFY     confirm the operator-visible surface changed — one hop past the   [§4.4]
                  label (charter §16); if not, file a follow-up task → back to 1
 9. LOOP       never returns; heartbeat + wait on bus events                     [§3]
```

No-lost-work guarantees at every hop: each agent declares an abort marker (`abort.py`, present) and checks it before major actions; WIP is committed + a draft PR opened before any session-fragility point (global §19 end-of-session hygiene); the kanban status timeline (`kanban.py:173 status_timeline`) records every transition so a restarted loop resumes exactly where it stopped.

**Speed** comes from: parallel agents (`_run_parallel`), prompt-cache reuse (cheap repeated scans), batch API for latency-insensitive work, event-driven monitors (no polling latency), and direct-to-agent delegation for small tasks (skipping the lead hop).

---

## 9. Phased build plan (ordered by leverage, testable in small batches)

Each phase is independently mergeable, ships with tests (≥80% gate, charter §5), and leaves master deployable. Phases are ordered so the **highest-leverage, lowest-risk** slices land first and each unblocks the next.

### Phase 1 — Make the existing loop *real and safe* (the seam)
**Leverage: highest. Unblocks everything. Lowest risk (no new subsystems).**
- 1.1 `ApiConductor` — port deep-ai `worker.py` tool-use loop behind the existing `Conductor` protocol (`supervisor.py:37`). Drop-in replacement for `StubConductor`; nothing else changes.
- 1.2 **Enforce `cost_cap_usd`** — Audit 1 confirms it's stored (`supervisor.py:90`) but never gates dispatch. Add the check + hard abort (port `worker.py:455`).
- 1.3 **Enforce `allowed_tools`** — pass the head's allowlist into the actual API/CLI call (fix `conductor.py:99`).
- 1.4 Port `cost.py` price table + wire `swarm-cost` to real USD.
*Test batch: ApiConductor dispatch with mocked Anthropic client; cost-cap abort fires at threshold; allowlist appears in the call; price math.*

### Phase 2 — The unified bus + one external adapter
**Leverage: high. Enables multi-agent coordination + the cheap-plan lanes.**
- 2.1 Widen `messaging.py` enums + add task-lifecycle msg types; keep JSON-inbox default.
- 2.2 `adapters/claude_code.py` (the simplest adapter — `claude --print`) + the `Adapter` protocol.
- 2.3 Bidirectional kanban↔bus status sync (`task_delegated` → claim → `task_done` updates the row).
*Test batch: delegate a task over the bus to a stub adapter, watch the kanban row transition; enum validation; round-trip status.*

### Phase 3 — Usage tracking + auto-switch governor
**Leverage: high. This is the rate-resilience the operator named explicitly.**
- 3.1 Port `token_rates.py` → `usage.py`; per-lane rolling counters + 429 timestamps.
- 3.2 `governor.py` state machine (CHEAP_PLANS ↔ API_SWARM ↔ THROTTLED).
- 3.3 `resilience.py` backoff/rotation wrapper (cache-aware sleeps, `pause_turn` resume).
*Test batch: simulate 429 → state transition; budget cap → THROTTLED; reset window → back to CHEAP; backoff never sleeps exactly 300s.*

### Phase 4 — The never-sleep supervisor team
**Leverage: high. This is what makes it autonomous vs. a drainer.**
- 4.1 `roles.py` + `routing.py` (taxonomy + hybridization rule).
- 4.2 `supervisors/dispatch.py` (perpetual, routes direct-vs-lead).
- 4.3 `supervisors/scout.py` (/deep-research → kanban, dedup-checked).
- 4.4 `supervisors/planner.py` (shape → DAG).
- 4.5 `supervisors/meta.py` (election, silent-heartbeat restart, owns rate-mode).
*Test batch: scout files a task from a fixture finding; planner stamps route+DAG; dispatch routes small→agent, compound→lead; meta restarts a stuck supervisor.*

### Phase 5 — Convergence workflow + skills
**Leverage: medium-high. This is the quality gate that keeps master clean.**
- 5.1 `skills/` bundles for `/simplify` + `/code-review` (cached system blocks).
- 5.2 `workflow.py WorkflowRunner` — simplify+review cycles + independent adversarial verifier.
- 5.3 Wire into `dispatch.py` step 5; gate merge on adversarial PASS.
*Test batch: a fix with a planted regression is bounced by the simplify test-delta; a subtly-wrong fix is caught by the adversarial verifier; a clean fix converges and merges.*

### Phase 6 — Monitors, goals, remaining adapters, full Claude-API powers
**Leverage: medium. Polish to genuinely exceed Claude Code.**
- 6.1 `monitor.py` over bus NOTIFY (event-driven wakes, no polling).
- 6.2 Goal predicates per loop (zero-NULL / freshness goals).
- 6.3 `adapters/codex.py`, `adapters/cursor.py`, `adapters/api_worker.py`.
- 6.4 Batch API, Memory tool, Files API, extended thinking, tool-search/PTC, MCP registration.
- 6.5 `PostgresBus` production transport against `coord_db` (+ alembic CHECK-constraint widen).
*Test batch: monitor fires a handler on a NOTIFY; goal failure files a task; each adapter round-trips; batch submit; postgres bus parity with JSON bus.*

### Phase 7 — End-to-end + redeploy + live verification
**Leverage: completes the wholeness loop.**
- 7.1 Redeploy hook + operator-visible-surface verification (charter §16 one-hop-past-the-label).
- 7.2 Full bug→…→LIVE integration test in a scratch repo.
- 7.3 Session-resume + no-lost-work guarantees (abort markers, WIP-commit-before-exit).
*Test batch: a seeded bug flows end-to-end to a merged+redeployed+verified state with zero manual steps; kill a loop mid-task and confirm clean resume.*

---

## 10. Risks + mitigations

- **Duplicate supervisors corrupt the kanban** ($155 incident, charter §10). → Phase 4.5 election + OS-level pidfile/flock; meta is the only writer-of-record.
- **A rate-mode flap thrashes** (cheap↔API every tick). → Hysteresis in the governor (separate enter/exit thresholds + a minimum dwell time per mode).
- **Adversarial verifier rubber-stamps** (charter §16 "agent reports done"). → Verifier runs in *fresh context*, is scored on refutations found, and the dispatcher re-runs its claimed checks (global §0 truth pass).
- **Scout floods the kanban** with low-value tasks. → Dedup-before-file (§3.1) + a priority floor + planner can reject/merge findings.
- **Bus enum widening breaks the deep-ai postgres CHECK constraints.** → Phase 6.5 alembic migration in lockstep with the Python enum (charter §20 five-artifact contract).
- **Lost work on crash.** → status_timeline + abort markers + WIP-commit-before-fragility at every hop (§8).

---

## 11. What we are explicitly NOT doing (scope discipline)

- Not rewriting the kanban — `kanban.py` DAG primitives (`unblocked`, `claim_one`, `add_blocked_by`) are correct and reused as-is.
- Not adding heavy deps — stays stdlib + `anthropic` SDK; OSS default keeps the JSON bus.
- Not auto-flipping anything in the deep-ai trading domain — charter §14 binds; the swarm builds software, the operator promotes models.
- Not building a new UI — status surfaces through the existing kanban + bus + `swarm-status`/`swarm-cost` skills.
```
