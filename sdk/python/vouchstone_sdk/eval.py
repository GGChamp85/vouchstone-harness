"""First-class local eval harness (C8) — runnable entirely without the
hosted control plane.

The control plane has its own eval harness (Evals v2:
control-plane/backend/app/services/eval_service.py) for golden-dataset
evals against agent versions stored server-side. This is the data-plane
counterpart: a customer running the offline harness (C4) or developing an
agent locally needs to evaluate it without any network dependency at all,
same "no stubs, actually executing" bar as everything else in this SDK --
each EvalCase genuinely runs through the real Agent.process() path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .agent import Agent
from .types import Message


@dataclass
class EvalCase:
    name: str
    input_content: str
    expected_output: str | None = None
    # Custom grader overrides the default (exact-match / substring)
    # comparison -- e.g. for cases needing structural or fuzzy grading.
    # Receives (actual_output, expected_output) -> (score 0..1, passed, reason).
    grader: Callable[[str, str | None], GradeResult] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GradeResult:
    score: float
    passed: bool
    reason: str = ""


@dataclass
class EvalCaseResult:
    case_name: str
    actual_output: str
    grade: GradeResult
    latency_ms: int
    error: str | None = None


@dataclass
class EvalReport:
    total: int
    passed: int
    failed: int
    average_score: float
    results: list[EvalCaseResult]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def default_grader(actual: str, expected: str | None) -> GradeResult:
    """Substring match: expected text appearing anywhere in actual output
    passes. Deliberately simple and predictable -- a real quality bar
    should use a domain-specific grader (exact match, structural
    comparison, or an LLM-judge grader supplied via EvalCase.grader), not
    rely on this default for anything beyond a smoke test."""
    if expected is None:
        return GradeResult(score=1.0, passed=True, reason="no expected_output set -- ungraded, presence-only check")
    passed = expected.strip().lower() in actual.strip().lower()
    return GradeResult(score=1.0 if passed else 0.0, passed=passed, reason="substring match" if passed else "expected text not found in output")


@dataclass
class EvalSuite:
    name: str
    cases: list[EvalCase] = field(default_factory=list)

    def add(self, case: EvalCase) -> EvalSuite:
        self.cases.append(case)
        return self


async def run_eval_suite(
    agent: Agent, suite: EvalSuite, *,
    grader: Callable[[str, str | None], GradeResult] | None = None,
    session_id: str | None = None,
) -> EvalReport:
    """Actually runs every case through agent.process() -- the real
    5-layer memory pipeline, the real run() implementation -- not a mock
    of the agent's behavior. Each case gets its own session by default (no
    cross-case memory leakage skewing results) unless session_id is
    explicitly shared."""
    import time

    results: list[EvalCaseResult] = []
    for case in suite.cases:
        case_session = session_id or agent.start_session()
        start = time.monotonic()
        try:
            response = await agent.process(Message(content=case.input_content), session_id=case_session)
            latency_ms = int((time.monotonic() - start) * 1000)
            case_grader = case.grader or grader or default_grader
            grade = case_grader(response.content, case.expected_output)
            results.append(EvalCaseResult(
                case_name=case.name, actual_output=response.content,
                grade=grade, latency_ms=latency_ms,
            ))
        except Exception as e:
            latency_ms = int((time.monotonic() - start) * 1000)
            results.append(EvalCaseResult(
                case_name=case.name, actual_output="",
                grade=GradeResult(score=0.0, passed=False, reason=f"execution error: {e}"),
                latency_ms=latency_ms, error=str(e),
            ))

    passed = sum(1 for r in results if r.grade.passed)
    avg_score = sum(r.grade.score for r in results) / len(results) if results else 0.0

    return EvalReport(
        total=len(results), passed=passed, failed=len(results) - passed,
        average_score=avg_score, results=results,
    )
