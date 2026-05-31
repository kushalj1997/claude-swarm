"""Workflow — a dynamic multi-agent workflow engine, callable by the swarm.

This is the swarm-native analogue of Claude Code's Workflow tool, driven over
the Claude API instead of in-session subagents. It encodes the operator's
standing definition of the "workflow"/"parallel" keyword
(``docs/AUTONOMY_ARCHITECTURE.md`` §4.2, global charter §0 workflow contract):

    (a) fan the work out across agents,
    (b) run ``/simplify`` (behaviour-preserving cleanup) AND ``/code-review``
        (bugs / security / silent-failure / edge cases) in CYCLES until
        convergence — a full pass that finds nothing material to change,
    (c) adversarially verify every fix in an INDEPENDENT, fresh-context agent
        that tries to refute it, BEFORE it lands.

Three composable topologies are exposed:

* :meth:`WorkflowRunner.fan_out`   — N independent prompts, run in parallel,
  results collected (the "parallelize" primitive).
* :meth:`WorkflowRunner.pipeline`  — sequential stages, each sees the prior
  stage's output (refine → review → fix chains).
* :meth:`WorkflowRunner.converge`  — the headline souped-up cycle: simplify +
  review until convergence, then an independent adversarial verify gate.

The engine is **transport-agnostic** via the :class:`WorkflowAgent` protocol,
mirroring the :class:`~claude_swarm.supervisor.Conductor` seam: tests inject a
deterministic stub; production injects :class:`ApiWorkflowAgent`, which calls
``POST /v1/messages`` through the ``anthropic`` SDK. The SDK reads the API key
from the environment at call time — this module never reads, stores, or logs a
key.

Dependency-light: stdlib only at import time. The ``anthropic`` SDK is imported
lazily inside :class:`ApiWorkflowAgent` so the OSS default never pulls it in.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

log = logging.getLogger(__name__)


class _MessagesClient(Protocol):
    """The slice of the Anthropic client surface :class:`ApiWorkflowAgent` uses.

    A structural type so we depend on a *capability* (``.messages.create``)
    rather than the concrete SDK class — keeping this module import-light. The
    members are typed ``Any`` because the SDK's request/response types are
    dynamic and version-dependent; we parse the response defensively in
    :func:`_extract_text` / :func:`_parse_pass_result` instead of trusting a
    static shape. ``messages`` is a read-only property so the structural match
    against the SDK's ``Anthropic`` (whose ``messages`` is a read-only cached
    property) holds under strict mypy.
    """

    @property
    def messages(self) -> Any: ...


class Pass(str, Enum):
    """The named passes a workflow agent can run.

    ``SIMPLIFY`` and ``REVIEW`` are the two convergence passes;
    ``ADVERSARIAL`` is the independent refutation gate; ``BUILD`` is the
    initial fix/implementation pass that the cycle then polishes.
    """

    BUILD = "build"
    SIMPLIFY = "simplify"        # /simplify — behaviour-preserving cleanup
    REVIEW = "code-review"       # /code-review — bugs / security / edge cases
    ADVERSARIAL = "adversarial"  # fresh-context refutation gate


# The role prompt each pass loads. Kept terse + declarative; an ApiWorkflowAgent
# folds these into the system block (and would cache them in production).
_PASS_PROMPTS: dict[Pass, str] = {
    Pass.BUILD: (
        "You implement the requested change end-to-end. Return the patch and a "
        "one-line summary of what you changed."
    ),
    Pass.SIMPLIFY: (
        "You run /simplify: a BEHAVIOUR-PRESERVING cleanup pass (reuse, "
        "simplification, efficiency, altitude). You MUST NOT change behaviour. "
        "If nothing material can be improved, report no changes."
    ),
    Pass.REVIEW: (
        "You run /code-review: hunt bugs, security holes, silent failures, "
        "unhandled edge cases, and convention violations. Report each finding "
        "with file:line evidence. If the change is clean, report no findings."
    ),
    Pass.ADVERSARIAL: (
        "You are an INDEPENDENT adversarial verifier with FRESH context. Your "
        "job is to REFUTE the claimed fix: find the input, edge case, or "
        "interaction that breaks it. Pass ONLY if you cannot refute it."
    ),
}


@dataclass
class PassResult:
    """Structured outcome of one workflow pass.

    Attributes:
        which: the pass that produced this result.
        changed: ``True`` iff this pass altered the artifact materially
            (a simplify/review/build that did something). The convergence
            loop terminates when a full simplify+review cycle reports
            ``changed=False`` on both passes.
        passed: for :attr:`Pass.ADVERSARIAL`, whether the fix survived
            refutation. ``None`` for non-verdict passes.
        findings: human-readable findings (file:line evidence in production).
        output: the artifact/text the pass produced (patch, summary, report).
        cost_usd: priced spend for this pass, if the agent reports it.
    """

    which: Pass
    changed: bool = False
    passed: bool | None = None
    findings: tuple[str, ...] = ()
    output: str | None = None
    cost_usd: float = 0.0


class WorkflowAgent(Protocol):
    """Pluggable strategy for running ONE workflow pass.

    Mirrors :class:`~claude_swarm.supervisor.Conductor`: the engine owns the
    topology + convergence logic; the agent owns the actual model call. A
    fresh-context adversarial pass MUST be served by an agent that does not
    share state with the passes it verifies — implementations enforce this by
    constructing a new client/conversation per :meth:`run` call.
    """

    def run(self, *, which: Pass, prompt: str, context: str | None = None) -> PassResult:
        """Run ``which`` against ``prompt`` (with optional prior ``context``)."""
        ...


@dataclass
class StubWorkflowAgent:
    """Deterministic agent for tests + the toy examples — no network.

    Drives the engine through scripted outcomes:

    * ``simplify_changes`` / ``review_changes`` — a list of booleans consumed
      one per call, controlling whether successive SIMPLIFY / REVIEW passes
      report a material change. Once exhausted, passes report ``changed=False``
      (convergence). This lets a test assert "converges after K cycles".
    * ``adversarial_passes`` — whether the adversarial gate passes (default
      ``True``).
    * ``fail_pass`` — an optional :class:`Pass` whose ``run`` raises, to
      exercise error handling.
    """

    calls: list[tuple[Pass, str]] = field(default_factory=list)
    simplify_changes: list[bool] = field(default_factory=list)
    review_changes: list[bool] = field(default_factory=list)
    adversarial_passes: bool = True
    cost_per_pass_usd: float = 0.0
    fail_pass: Pass | None = None
    _simplify_idx: int = 0
    _review_idx: int = 0

    def run(self, *, which: Pass, prompt: str, context: str | None = None) -> PassResult:
        self.calls.append((which, prompt))
        if self.fail_pass is not None and which is self.fail_pass:
            raise RuntimeError(f"stub failure on pass {which.value}")
        if which is Pass.SIMPLIFY:
            changed = self._next(self.simplify_changes, "_simplify_idx")
            return PassResult(which, changed=changed, cost_usd=self.cost_per_pass_usd,
                              output=f"simplify#{self._simplify_idx}")
        if which is Pass.REVIEW:
            changed = self._next(self.review_changes, "_review_idx")
            findings = ("planted finding",) if changed else ()
            return PassResult(which, changed=changed, findings=findings,
                              cost_usd=self.cost_per_pass_usd, output=f"review#{self._review_idx}")
        if which is Pass.ADVERSARIAL:
            return PassResult(which, passed=self.adversarial_passes,
                              cost_usd=self.cost_per_pass_usd,
                              output="adversarial-verdict")
        # BUILD (or any other) — always "did something".
        return PassResult(which, changed=True, cost_usd=self.cost_per_pass_usd,
                          output=f"{which.value}-output")

    def _next(self, seq: list[bool], idx_attr: str) -> bool:
        idx = getattr(self, idx_attr)
        setattr(self, idx_attr, idx + 1)
        return seq[idx] if idx < len(seq) else False


@dataclass
class ApiWorkflowAgent:
    """Production agent: runs each pass via the Claude API (``anthropic`` SDK).

    The SDK is imported lazily so importing this module never requires it. The
    API key is read by the SDK from ``ANTHROPIC_API_KEY`` at call time; this
    class never reads, stores, or logs the key.

    A fresh client + single-turn conversation is constructed per :meth:`run`,
    which is what makes the adversarial pass genuinely independent of the
    passes it verifies (no shared message history).

    ``changed`` / ``passed`` are inferred from a small structured convention:
    the model is asked to end its reply with a machine-readable verdict line
    (``VERDICT: changed`` / ``VERDICT: clean`` / ``VERDICT: pass|fail``). This
    keeps parsing trivial and avoids a brittle JSON contract while the real
    tool-use worker (a separate slice) is wired in.
    """

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    system_preamble: str = (
        "You are a member of an autonomous engineering swarm. Be precise; "
        "cite file:line evidence; never claim success without verification."
    )

    def run(self, *, which: Pass, prompt: str, context: str | None = None) -> PassResult:
        client = self._client()
        role_prompt = _PASS_PROMPTS[which]
        user_blocks = [prompt]
        if context:
            user_blocks.append(f"\n\n--- prior pass output ---\n{context}")
        user_blocks.append(
            "\n\nEnd your reply with exactly one line: "
            "`VERDICT: changed` or `VERDICT: clean` (for simplify/review/build), "
            "or `VERDICT: pass` / `VERDICT: fail` (for adversarial)."
        )
        resp = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=f"{self.system_preamble}\n\n{role_prompt}",
            messages=[{"role": "user", "content": "".join(user_blocks)}],
        )
        text = _extract_text(resp)
        return _parse_pass_result(which, text, cost_usd=_priced(resp))

    @staticmethod
    def _client() -> _MessagesClient:
        # Check for the API key first — the most actionable error for the user.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; ApiWorkflowAgent reads it at runtime only"
            )
        try:
            import anthropic  # lazy: keeps the OSS default dependency-light
        except ImportError as exc:
            raise RuntimeError(
                "ApiWorkflowAgent requires the 'anthropic' SDK: pip install anthropic"
            ) from exc
        # The SDK's Anthropic client satisfies the _MessagesClient capability
        # (it exposes a read-only ``.messages.create``). Reads the key from env;
        # never read, stored, or logged here.
        return anthropic.Anthropic()


def _extract_text(resp: object) -> str:
    """Best-effort text extraction from an Anthropic Messages response.

    Tolerant of both the SDK object shape (``resp.content[i].text``) and a
    plain dict (used by the lightweight fake in tests), so the parser is
    exercised without the real SDK.
    """
    content = getattr(resp, "content", None)
    if content is None and isinstance(resp, dict):
        content = resp.get("content")
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        txt = getattr(block, "text", None)
        if txt is None and isinstance(block, dict):
            txt = block.get("text")
        if txt:
            parts.append(str(txt))
    return "\n".join(parts)


def _priced(resp: object) -> float:
    """Pull a cost from a response if one is attached, else 0.0.

    The real price table (deep-ai ``cost.py``) lands in a separate slice;
    here we honour a ``cost_usd`` attribute/key if the caller pre-priced.
    """
    cost = getattr(resp, "cost_usd", None)
    if cost is None and isinstance(resp, dict):
        cost = resp.get("cost_usd")
    try:
        return float(cost) if cost is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_pass_result(which: Pass, text: str, *, cost_usd: float) -> PassResult:
    """Parse the trailing ``VERDICT:`` line into a structured result."""
    verdict = ""
    for line in reversed(text.splitlines()):
        s = line.strip().lower()
        if s.startswith("verdict:"):
            verdict = s.split(":", 1)[1].strip()
            break
    if which is Pass.ADVERSARIAL:
        # Default to FAIL when no verdict is present — the safe choice is to
        # block a merge we could not affirmatively verify (charter §16).
        passed = verdict == "pass"
        return PassResult(which, passed=passed, output=text, cost_usd=cost_usd)
    changed = verdict == "changed"
    findings = (text,) if (which is Pass.REVIEW and changed) else ()
    return PassResult(which, changed=changed, findings=findings, output=text, cost_usd=cost_usd)


@dataclass
class WorkflowReport:
    """The full record of one workflow run — every pass, convergence, verdict."""

    passes: tuple[PassResult, ...]
    converged: bool
    cycles: int
    adversarial_passed: bool
    total_cost_usd: float

    @property
    def merge_ok(self) -> bool:
        """A workflow may land iff it converged AND survived adversarial verify."""
        return self.converged and self.adversarial_passed


@dataclass
class WorkflowConfig:
    """Tunables for :class:`WorkflowRunner`."""

    max_cycles: int = 5           # hard cap on simplify+review cycles (always terminates)
    max_parallel: int = 4         # fan-out width
    run_adversarial: bool = True  # gate merges on the independent refutation pass


class WorkflowRunner:
    """Drives dynamic multi-agent workflows over a :class:`WorkflowAgent` seam.

    The runner owns the topology + convergence logic and is fully
    deterministic given a deterministic agent; the agent owns the model call.
    """

    def __init__(
        self,
        *,
        agent: WorkflowAgent,
        adversary: WorkflowAgent | None = None,
        config: WorkflowConfig | None = None,
    ) -> None:
        self.agent = agent
        # The adversary defaults to the SAME agent type but, for ApiWorkflowAgent,
        # each ``run`` builds a fresh client/conversation — so it is independent
        # even when ``adversary is agent`` (charter §0 adversarial pass). Callers
        # who want a different model for refutation pass a distinct ``adversary``.
        self.adversary = adversary or agent
        self.config = config or WorkflowConfig()

    # ----- topology 1: fan-out (parallelize) -------------------------

    def fan_out(self, prompts: list[str], *, which: Pass = Pass.BUILD) -> list[PassResult]:
        """Run ``prompts`` independently in parallel, preserving input order.

        This is the "parallelize" primitive: N independent agents, results
        collected. A prompt whose pass raises yields a ``PassResult`` with the
        error captured in ``findings`` rather than killing the whole fan-out
        (no-lost-work: one bad branch doesn't sink its siblings).
        """
        if not prompts:
            return []
        width = max(1, min(self.config.max_parallel, len(prompts)))
        results: list[PassResult | None] = [None] * len(prompts)
        with _cf.ThreadPoolExecutor(max_workers=width) as pool:
            futs = {
                pool.submit(self._safe_run, self.agent, which, p, None): i
                for i, p in enumerate(prompts)
            }
            for fut in _cf.as_completed(futs):
                results[futs[fut]] = fut.result()
        return [r for r in results if r is not None]

    # ----- topology 2: pipeline (sequential refine) ------------------

    def pipeline(self, prompt: str, stages: list[Pass]) -> list[PassResult]:
        """Run ``stages`` in order; each stage sees the previous stage's output.

        Used for refine → review → fix chains where order matters. Returns one
        :class:`PassResult` per stage, in order.
        """
        results: list[PassResult] = []
        context: str | None = None
        for stage in stages:
            res = self._safe_run(self.agent, stage, prompt, context)
            results.append(res)
            if res.output:
                context = res.output
        return results

    # ----- topology 3: converge (the souped-up cycle) ----------------

    def converge(self, prompt: str, *, build_first: bool = True) -> WorkflowReport:
        """The headline workflow: simplify + review until convergence, then
        an INDEPENDENT adversarial verify gate.

        Steps (``docs/AUTONOMY_ARCHITECTURE.md`` §4.2):
          1. (optional) a BUILD pass produces the initial artifact.
          2. Cycle ``/simplify`` then ``/code-review``. A cycle "converged"
             when BOTH passes report ``changed=False`` (nothing material left).
             Bounded by :attr:`WorkflowConfig.max_cycles` so it always halts.
          3. If converged (and :attr:`WorkflowConfig.run_adversarial`), run the
             adversarial refutation gate in fresh context. ``merge_ok`` is
             ``True`` only on convergence AND adversarial PASS.

        A non-converging run (hit the cycle cap with churn still happening) is
        reported with ``converged=False`` and is NOT eligible to merge — the
        dispatcher bounces it back rather than landing unstable code.
        """
        passes: list[PassResult] = []
        context: str | None = None

        if build_first:
            build = self._safe_run(self.agent, Pass.BUILD, prompt, None)
            passes.append(build)
            if build.output:
                context = build.output

        converged = False
        cycles = 0
        for _ in range(self.config.max_cycles):
            cycles += 1
            simplify = self._safe_run(self.agent, Pass.SIMPLIFY, prompt, context)
            passes.append(simplify)
            if simplify.output:
                context = simplify.output
            review = self._safe_run(self.agent, Pass.REVIEW, prompt, context)
            passes.append(review)
            if review.output:
                context = review.output
            if not simplify.changed and not review.changed:
                converged = True
                break

        adversarial_passed = False
        if converged and self.config.run_adversarial:
            adv = self._safe_run(self.adversary, Pass.ADVERSARIAL, prompt, context)
            passes.append(adv)
            adversarial_passed = bool(adv.passed)
        elif converged:
            # Adversarial disabled by config — convergence alone gates the merge.
            adversarial_passed = True

        total = round(sum(p.cost_usd for p in passes), 6)
        return WorkflowReport(
            passes=tuple(passes),
            converged=converged,
            cycles=cycles,
            adversarial_passed=adversarial_passed,
            total_cost_usd=total,
        )

    # ----- internals --------------------------------------------------

    @staticmethod
    def _safe_run(
        agent: WorkflowAgent, which: Pass, prompt: str, context: str | None
    ) -> PassResult:
        """Run one pass, converting an exception into a captured failure result.

        Keeps one bad pass from aborting an entire fan-out / cycle (no-lost-work).
        A failed SIMPLIFY/REVIEW reports ``changed=False`` so it cannot, by
        itself, mask non-convergence; a failed ADVERSARIAL reports
        ``passed=False`` so a crash never rubber-stamps a merge.
        """
        try:
            return agent.run(which=which, prompt=prompt, context=context)
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("workflow pass %s failed: %s", which.value, exc)
            passed = False if which is Pass.ADVERSARIAL else None
            return PassResult(
                which,
                changed=False,
                passed=passed,
                findings=(f"pass-error: {exc!r}",),
                output=None,
            )


__all__ = [
    "ApiWorkflowAgent",
    "Pass",
    "PassResult",
    "StubWorkflowAgent",
    "WorkflowAgent",
    "WorkflowConfig",
    "WorkflowReport",
    "WorkflowRunner",
]
