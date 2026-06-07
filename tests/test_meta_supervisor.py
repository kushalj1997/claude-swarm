"""Tests for MetaSupervisorMonitor, parallelism_score, cost_preflight."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_swarm.kanban import Kanban, Task, TaskStatus
from claude_swarm.meta_supervisor import (
    AnomalyTracker,
    MetaSupervisorMonitor,
    PARALLELISM_MOSTLY_SAFE,
    PARALLELISM_RISKY,
    PARALLELISM_SAFE,
    PARALLELISM_UNSAFE,
    PreflightVerdict,
    SupervisorHealthEvent,
    cost_preflight,
    parallelism_score,
)


# ---------- fixtures --------------------------------------------------------

@pytest.fixture()
def kanban(tmp_path: Path) -> Kanban:
    return Kanban(tmp_path / "kanban.db")


@pytest.fixture()
def monitor(kanban: Kanban, tmp_path: Path) -> MetaSupervisorMonitor:
    return MetaSupervisorMonitor(kanban=kanban, home=tmp_path)


# ---------- parallelism_score (standalone) ----------------------------------

class TestParallelismScore:
    def test_serial_role_is_unsafe(self) -> None:
        t = Task(role="supervisor")
        assert parallelism_score(t) == PARALLELISM_UNSAFE

    def test_meta_supervisor_role_is_unsafe(self) -> None:
        t = Task(role="meta-supervisor")
        assert parallelism_score(t) == PARALLELISM_UNSAFE

    def test_ephemeral_agent_always_safe(self) -> None:
        t = Task(role="ephemeral-agent", files_owned=["a.py", "b.py"])
        assert parallelism_score(t) == PARALLELISM_SAFE

    def test_reviewer_head_always_safe(self) -> None:
        t = Task(required_head="reviewer", files_owned=["x.py", "y.py", "z.py"])
        assert parallelism_score(t) == PARALLELISM_SAFE

    def test_no_files_single_file_task_is_safe(self) -> None:
        t = Task(role="agent")
        assert parallelism_score(t) == PARALLELISM_SAFE

    def test_single_file_task_is_safe(self) -> None:
        t = Task(role="agent", files_owned=["src/foo.py"])
        assert parallelism_score(t) == PARALLELISM_SAFE

    def test_few_files_mostly_safe(self) -> None:
        t = Task(role="agent", files_owned=["a.py", "b.py", "c.py"])
        assert parallelism_score(t) == PARALLELISM_MOSTLY_SAFE

    def test_many_files_risky(self) -> None:
        t = Task(role="agent", files_owned=[f"f{i}.py" for i in range(10)])
        assert parallelism_score(t) == PARALLELISM_RISKY

    def test_file_overlap_with_in_progress_is_risky(self) -> None:
        t = Task(role="agent", files_owned=["src/shared.py"])
        other = Task(role="agent", files_owned=["src/shared.py", "src/other.py"])
        assert parallelism_score(t, in_progress=[other]) == PARALLELISM_RISKY

    def test_no_file_overlap_not_risky(self) -> None:
        t = Task(role="agent", files_owned=["src/unique.py"])
        other = Task(role="agent", files_owned=["tests/test_other.py"])
        score = parallelism_score(t, in_progress=[other])
        assert score >= PARALLELISM_MOSTLY_SAFE

    def test_metadata_override_respected(self) -> None:
        t = Task(role="agent", metadata={"parallelism_safety": 0.42})
        assert parallelism_score(t) == pytest.approx(0.42)

    def test_metadata_override_out_of_range_clamped(self) -> None:
        t = Task(metadata={"parallelism_safety": 5.0})
        assert parallelism_score(t) == pytest.approx(1.0)

    def test_metadata_override_invalid_falls_through(self) -> None:
        t = Task(metadata={"parallelism_safety": "bad"})
        # Should not raise; falls through to heuristic path
        score = parallelism_score(t)
        assert 0.0 <= score <= 1.0


# ---------- cost_preflight --------------------------------------------------

class TestCostPreflight:
    def test_admit_within_budget(self) -> None:
        t = Task(metadata={"cost_cap_usd": 1.0})
        result = cost_preflight(t, historical_avg_usd=0.05)
        assert result.verdict == "ADMIT"

    def test_reject_when_estimate_exceeds_cap(self) -> None:
        t = Task(metadata={"cost_cap_usd": 0.01})
        result = cost_preflight(t, historical_avg_usd=0.10)
        assert result.verdict == "REJECT"
        assert "cap" in result.reason

    def test_hold_when_estimate_near_cap(self) -> None:
        t = Task(metadata={"cost_cap_usd": 0.10})
        result = cost_preflight(t, historical_avg_usd=0.085)  # 85% of cap
        assert result.verdict == "HOLD"
        assert ">80%" in result.reason

    def test_reject_exceeds_daily_budget(self) -> None:
        t = Task()
        result = cost_preflight(t, historical_avg_usd=0.10, daily_budget_remaining_usd=0.05)
        assert result.verdict == "REJECT"
        assert "daily budget" in result.reason

    def test_no_cap_no_budget_admits(self) -> None:
        t = Task()
        result = cost_preflight(t, historical_avg_usd=0.05)
        assert result.verdict == "ADMIT"

    def test_estimated_cost_returned(self) -> None:
        t = Task(metadata={"cost_cap_usd": 1.0})
        result = cost_preflight(t, historical_avg_usd=0.123)
        assert result.estimated_cost_usd == pytest.approx(0.123)

    def test_cap_returned_in_result(self) -> None:
        t = Task(metadata={"cost_cap_usd": 0.5})
        result = cost_preflight(t, historical_avg_usd=0.10)
        assert result.cap_usd == pytest.approx(0.5)


# ---------- AnomalyTracker --------------------------------------------------

class TestAnomalyTracker:
    def test_no_escalation_below_threshold(self) -> None:
        tracker = AnomalyTracker(escalation_threshold=3)
        t = Task(error="boom", required_head="builder", type="default", status=TaskStatus.FAILED)
        assert not tracker.record(t)
        assert not tracker.record(t)

    def test_escalation_at_threshold(self) -> None:
        tracker = AnomalyTracker(escalation_threshold=3)
        t = Task(error="boom", required_head="builder", type="default", status=TaskStatus.FAILED)
        tracker.record(t)
        tracker.record(t)
        escalated = tracker.record(t)
        assert escalated

    def test_escalation_fires_only_once(self) -> None:
        tracker = AnomalyTracker(escalation_threshold=2)
        t = Task(error="boom", required_head="builder", type="default", status=TaskStatus.FAILED)
        tracker.record(t)
        assert tracker.record(t)   # threshold hit
        assert not tracker.record(t)  # already escalated

    def test_top_returns_most_common(self) -> None:
        tracker = AnomalyTracker(escalation_threshold=10)
        t1 = Task(error="boom", required_head="builder", type="a", status=TaskStatus.FAILED)
        t2 = Task(error="crash", required_head="scanner", type="b", status=TaskStatus.FAILED)
        for _ in range(3):
            tracker.record(t1)
        tracker.record(t2)
        top = tracker.top(2)
        assert top[0][1] == 3  # t1 is most common

    def test_reset_clears_fingerprint(self) -> None:
        tracker = AnomalyTracker(escalation_threshold=2)
        t = Task(error="boom", required_head="builder", type="a", status=TaskStatus.FAILED)
        tracker.record(t)
        tracker.record(t)  # escalates
        fp = tracker._fingerprint(t)
        tracker.reset(fp)
        # After reset, should escalate again at threshold
        tracker.record(t)
        assert tracker.record(t)


# ---------- MetaSupervisorMonitor -------------------------------------------

class TestMetaSupervisorMonitor:
    def test_parallelism_score_delegate(self, monitor: MetaSupervisorMonitor) -> None:
        t = Task(role="agent", files_owned=["a.py"])
        score = monitor.parallelism_score(t)
        assert score == PARALLELISM_SAFE

    def test_cost_preflight_delegate(self, monitor: MetaSupervisorMonitor) -> None:
        t = Task(metadata={"cost_cap_usd": 1.0})
        verdict = monitor.cost_preflight(t)
        assert verdict.verdict == "ADMIT"

    def test_record_outcome_updates_head_avg(self, monitor: MetaSupervisorMonitor) -> None:
        t1 = Task(cost_usd=0.10, status=TaskStatus.DONE, required_head="builder")
        t2 = Task(cost_usd=0.20, status=TaskStatus.DONE, required_head="builder")
        monitor.record_outcome(t1, head_name="builder")
        monitor.record_outcome(t2, head_name="builder")
        avg = monitor._head_avg_cost("builder")
        assert avg == pytest.approx(0.15)

    def test_record_outcome_returns_false_for_done_task(self, monitor: MetaSupervisorMonitor) -> None:
        t = Task(status=TaskStatus.DONE)
        assert not monitor.record_outcome(t, head_name="builder")

    def test_record_outcome_escalates_repeated_failures(self, monitor: MetaSupervisorMonitor) -> None:
        monitor.escalation_threshold = 2
        monitor._anomaly_tracker = AnomalyTracker(escalation_threshold=2)
        t = Task(error="boom", status=TaskStatus.FAILED, required_head="builder", type="default")
        monitor.record_outcome(t, head_name="builder")
        escalated = monitor.record_outcome(t, head_name="builder")
        assert escalated

    def test_check_supervisor_health_empty_dir(self, monitor: MetaSupervisorMonitor, tmp_path: Path) -> None:
        events = monitor.check_supervisor_health(supervisor_names=[])
        assert events == []

    def test_check_supervisor_health_no_status_file(self, monitor: MetaSupervisorMonitor) -> None:
        events = monitor.check_supervisor_health(supervisor_names=["nonexistent"])
        assert len(events) == 1
        assert events[0].is_silent

    def test_check_supervisor_health_fresh_heartbeat(self, monitor: MetaSupervisorMonitor, tmp_path: Path) -> None:
        from claude_swarm._paths import state_dir
        sd = state_dir(tmp_path)
        sd.mkdir(parents=True, exist_ok=True)
        status_file = sd / "dispatch.status.json"
        status_file.write_text(json.dumps({"last_tick": time.time()}))
        events = monitor.check_supervisor_health(supervisor_names=["dispatch"])
        assert len(events) == 1
        assert not events[0].is_silent

    def test_check_supervisor_health_stale_heartbeat(self, monitor: MetaSupervisorMonitor, tmp_path: Path) -> None:
        from claude_swarm._paths import state_dir
        sd = state_dir(tmp_path)
        sd.mkdir(parents=True, exist_ok=True)
        status_file = sd / "old.status.json"
        status_file.write_text(json.dumps({"last_tick": time.time() - 1000}))
        m = MetaSupervisorMonitor(kanban=monitor.kanban, home=tmp_path, heartbeat_timeout_s=300.0)
        events = m.check_supervisor_health(supervisor_names=["old"])
        assert events[0].is_silent
        assert events[0].silent_for_s >= 1000

    def test_top_failures_delegates_to_tracker(self, monitor: MetaSupervisorMonitor) -> None:
        t = Task(error="crash", status=TaskStatus.FAILED, required_head="builder", type="default")
        monitor.record_outcome(t, "builder")
        top = monitor.top_failures()
        assert isinstance(top, list)
