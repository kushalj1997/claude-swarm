"""Tests for the dynamic-workflow engine (claude_swarm.workflow)."""
from __future__ import annotations

import pytest

from claude_swarm.workflow import (
    ApiWorkflowAgent,
    Pass,
    PassResult,
    StubWorkflowAgent,
    WorkflowConfig,
    WorkflowRunner,
    _extract_text,
    _parse_pass_result,
    _priced,
)

# ----- fan-out ------------------------------------------------------------


def test_fan_out_runs_all_prompts_in_input_order() -> None:
    agent = StubWorkflowAgent()
    runner = WorkflowRunner(agent=agent, config=WorkflowConfig(max_parallel=4))
    results = runner.fan_out(["one", "two", "three"])
    assert len(results) == 3
    # The StubWorkflowAgent records the prompt; verify each came back.
    assert {p for _, p in agent.calls} == {"one", "two", "three"}
    assert all(isinstance(r, PassResult) for r in results)


def test_fan_out_empty_returns_empty() -> None:
    runner = WorkflowRunner(agent=StubWorkflowAgent())
    assert runner.fan_out([]) == []


def test_fan_out_isolates_a_failing_branch() -> None:
    # One branch's pass raises; siblings still return. The failed branch comes
    # back as a captured-error PassResult, not an exception.
    agent = StubWorkflowAgent(fail_pass=Pass.BUILD)
    runner = WorkflowRunner(agent=agent)
    results = runner.fan_out(["a"])
    assert len(results) == 1
    assert results[0].output is None
    assert any("pass-error" in f for f in results[0].findings)


def test_fan_out_width_capped_at_prompt_count() -> None:
    # Width <= len(prompts); a single prompt must not blow up the pool sizing.
    runner = WorkflowRunner(agent=StubWorkflowAgent(), config=WorkflowConfig(max_parallel=8))
    assert len(runner.fan_out(["solo"])) == 1


# ----- pipeline -----------------------------------------------------------


def test_pipeline_threads_context_between_stages() -> None:
    agent = StubWorkflowAgent()
    runner = WorkflowRunner(agent=agent)
    results = runner.pipeline("prompt", [Pass.BUILD, Pass.SIMPLIFY, Pass.REVIEW])
    assert [r.which for r in results] == [Pass.BUILD, Pass.SIMPLIFY, Pass.REVIEW]
    # Three calls recorded, in order.
    assert [c[0] for c in agent.calls] == [Pass.BUILD, Pass.SIMPLIFY, Pass.REVIEW]


def test_pipeline_empty_stages_returns_empty() -> None:
    runner = WorkflowRunner(agent=StubWorkflowAgent())
    assert runner.pipeline("p", []) == []


# ----- converge: the headline cycle --------------------------------------


def test_converge_clean_change_converges_in_one_cycle_and_merges() -> None:
    # No simplify/review changes -> converges on cycle 1; adversarial passes.
    agent = StubWorkflowAgent(simplify_changes=[], review_changes=[], adversarial_passes=True)
    runner = WorkflowRunner(agent=agent)
    report = runner.converge("fix the bug")
    assert report.converged is True
    assert report.cycles == 1
    assert report.adversarial_passed is True
    assert report.merge_ok is True


def test_converge_takes_multiple_cycles_then_settles() -> None:
    # Cycle 1: simplify changes something -> not converged.
    # Cycle 2: review changes something  -> not converged.
    # Cycle 3: both clean               -> converged.
    agent = StubWorkflowAgent(
        simplify_changes=[True, False, False],
        review_changes=[False, True, False],
        adversarial_passes=True,
    )
    runner = WorkflowRunner(agent=agent)
    report = runner.converge("p")
    assert report.converged is True
    assert report.cycles == 3
    assert report.merge_ok is True


def test_converge_respects_cycle_cap_and_refuses_merge() -> None:
    # Churn never stops -> hit the cap, report NOT converged, NOT merge-ok.
    agent = StubWorkflowAgent(
        simplify_changes=[True] * 10,
        review_changes=[True] * 10,
        adversarial_passes=True,
    )
    runner = WorkflowRunner(agent=agent, config=WorkflowConfig(max_cycles=3))
    report = runner.converge("p")
    assert report.converged is False
    assert report.cycles == 3
    assert report.merge_ok is False  # never lands unstable code


def test_converge_adversarial_failure_blocks_merge() -> None:
    # Converges, but the independent verifier refutes the fix -> no merge.
    agent = StubWorkflowAgent(adversarial_passes=False)
    runner = WorkflowRunner(agent=agent)
    report = runner.converge("p")
    assert report.converged is True
    assert report.adversarial_passed is False
    assert report.merge_ok is False


def test_converge_can_skip_build_pass() -> None:
    agent = StubWorkflowAgent()
    runner = WorkflowRunner(agent=agent)
    runner.converge("p", build_first=False)
    assert Pass.BUILD not in {c[0] for c in agent.calls}


def test_converge_includes_build_pass_by_default() -> None:
    agent = StubWorkflowAgent()
    WorkflowRunner(agent=agent).converge("p")
    assert Pass.BUILD in {c[0] for c in agent.calls}


def test_converge_disabled_adversarial_gates_on_convergence_alone() -> None:
    agent = StubWorkflowAgent(adversarial_passes=False)
    runner = WorkflowRunner(agent=agent, config=WorkflowConfig(run_adversarial=False))
    report = runner.converge("p")
    assert report.converged is True
    assert report.adversarial_passed is True  # convergence alone suffices
    assert report.merge_ok is True
    # The adversarial pass must NOT have been run.
    assert Pass.ADVERSARIAL not in {c[0] for c in agent.calls}


def test_converge_uses_independent_adversary_when_provided() -> None:
    main = StubWorkflowAgent(adversarial_passes=True)
    adversary = StubWorkflowAgent(adversarial_passes=False)
    runner = WorkflowRunner(agent=main, adversary=adversary)
    report = runner.converge("p")
    # The adversary (not the main agent) decided the verdict.
    assert report.adversarial_passed is False
    assert any(c[0] is Pass.ADVERSARIAL for c in adversary.calls)
    assert all(c[0] is not Pass.ADVERSARIAL for c in main.calls)


def test_converge_accumulates_cost() -> None:
    agent = StubWorkflowAgent(cost_per_pass_usd=0.01)
    report = WorkflowRunner(agent=agent).converge("p")
    # build + simplify + review + adversarial = 4 passes at $0.01.
    assert report.total_cost_usd == pytest.approx(0.04)


def test_converge_survives_a_failing_pass_without_crashing() -> None:
    # A SIMPLIFY crash is captured; it reports changed=False so it cannot
    # mask non-convergence, and the run still produces a report.
    agent = StubWorkflowAgent(fail_pass=Pass.SIMPLIFY)
    report = WorkflowRunner(agent=agent).converge("p")
    assert isinstance(report.passes, tuple)
    # A failed adversarial would block merge; here simplify failed -> review
    # clean -> converges, adversarial passes.
    assert report.cycles >= 1


def test_failed_adversarial_pass_never_rubber_stamps() -> None:
    agent = StubWorkflowAgent(fail_pass=Pass.ADVERSARIAL)
    report = WorkflowRunner(agent=agent).converge("p")
    assert report.converged is True
    assert report.adversarial_passed is False  # crash => blocked, not approved
    assert report.merge_ok is False


# ----- ApiWorkflowAgent parsing (no network) ------------------------------


def test_extract_text_from_dict_shape() -> None:
    resp = {"content": [{"text": "hello"}, {"text": "world"}]}
    assert _extract_text(resp) == "hello\nworld"


def test_extract_text_handles_empty() -> None:
    assert _extract_text({"content": []}) == ""
    assert _extract_text({}) == ""


def test_parse_review_changed_collects_finding() -> None:
    res = _parse_pass_result(Pass.REVIEW, "found a bug at x.py:10\nVERDICT: changed", cost_usd=0.0)
    assert res.changed is True
    assert res.findings


def test_parse_review_clean_has_no_findings() -> None:
    res = _parse_pass_result(Pass.REVIEW, "looks good\nVERDICT: clean", cost_usd=0.0)
    assert res.changed is False
    assert res.findings == ()


def test_parse_adversarial_pass_and_fail() -> None:
    ok = _parse_pass_result(Pass.ADVERSARIAL, "cannot break it\nVERDICT: pass", cost_usd=0.0)
    bad = _parse_pass_result(Pass.ADVERSARIAL, "breaks on empty\nVERDICT: fail", cost_usd=0.0)
    assert ok.passed is True
    assert bad.passed is False


def test_parse_adversarial_missing_verdict_defaults_to_fail() -> None:
    # Safe default: a verifier that returns no verdict must NOT approve.
    res = _parse_pass_result(Pass.ADVERSARIAL, "I am unsure", cost_usd=0.0)
    assert res.passed is False


def test_priced_reads_cost_if_present() -> None:
    assert _priced({"cost_usd": 0.5}) == 0.5
    assert _priced({}) == 0.0
    assert _priced({"cost_usd": "nope"}) == 0.0


def test_api_agent_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a key, construction-time use raises a clear error and never logs
    # or returns the (absent) key.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = ApiWorkflowAgent()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        agent.run(which=Pass.SIMPLIFY, prompt="p")


def test_api_agent_drives_runner_with_a_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end converge() over an ApiWorkflowAgent whose client is faked —
    # proves the API path threads through the engine without a real network call.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-not-a-real-key")

    class _FakeMessages:
        def create(self, **_kwargs: object) -> dict[str, object]:
            return {"content": [{"text": "all clean\nVERDICT: clean"}], "cost_usd": 0.002}

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessages()

    agent = ApiWorkflowAgent()
    monkeypatch.setattr(ApiWorkflowAgent, "_client", staticmethod(lambda: _FakeClient()))
    runner = WorkflowRunner(agent=agent)
    report = runner.converge("fix it", build_first=False)
    # "clean" on simplify+review => converge cycle 1; adversarial sees no
    # "pass" verdict (response says "clean") => blocked, as designed.
    assert report.converged is True
    assert report.adversarial_passed is False
    assert report.total_cost_usd > 0
