# Changelog

All notable changes to this project will be documented in this file. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-05-10

### Added
- DAG-aware SQLite kanban with `Task`, `TaskStatus`, `Kanban.unblocked()`,
  `claim_one()`, `add_blocked_by()`, status timeline.
- Six-head default roster (Scanner, Reviewer, Builder, Merger,
  Test-Runner, Auditor) with role-specific prompts and tool allowlists.
- Bounded JSON inbox transport (`Inbox`, `MessageBus`) with atomic writes.
- Abort-marker contract (`AbortMarker`, `raise_if_aborted`).
- Per-task `WorktreeManager` with JSON pull-request envelopes,
  cherry-pick merge, post-merge GC, and stale-marker sweep.
- Auto-merge pipeline with file-overlap rejection, topological ordering,
  configurable test gate.
- `Supervisor` + `SupervisorConfig` with pluggable `Conductor` protocol.
- `StubConductor`, `SubprocessConductor`, `ClaudeCLIConductor`.
- `ReviewerCheckpoint` with configurable interval + render template.
- `claude-swarm` CLI: `init`, `submit`, `list`, `unblocked`, `status`,
  `heads`, `inbox send|recv`, `merge`, `abort set|clear|check`, `run`.
- Two worked examples (`examples/todo_app`, `examples/doc_writer_team`).
- Apache 2.0 license, mypy strict, ruff config, pytest suite.
