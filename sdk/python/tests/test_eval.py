"""Tests for the local eval harness (C8) -- proves every case genuinely
runs through the real Agent.process() path (real memory pipeline, real
run() implementation), not a mock of the agent's behavior."""
import pytest

from vouchstone_sdk import (
    Agent, AgentConfig, EvalCase, EvalSuite, GradeResult, default_grader,
    run_eval_suite,
)
from vouchstone_sdk.types import AgentResponse, Message, MemoryContext


class EchoAgent(Agent):
    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        return AgentResponse(content=f"the answer is 42, said in response to: {message.content}")


class FailingAgent(Agent):
    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        raise RuntimeError("agent logic blew up")


async def _make_agent(agent_id: str, cls=EchoAgent) -> Agent:
    agent = cls(AgentConfig(name="Eval Test Agent", semantic_memory=False, episodic_memory=False, procedural_memory=False))
    await agent.initialize(agent_id=agent_id, local_only=True)
    return agent


def test_default_grader_substring_match():
    grade = default_grader("the answer is 42, extra text", "answer is 42")
    assert grade.passed is True
    assert grade.score == 1.0


def test_default_grader_no_match():
    grade = default_grader("something else entirely", "answer is 42")
    assert grade.passed is False
    assert grade.score == 0.0


def test_default_grader_no_expected_output_is_ungraded_pass():
    grade = default_grader("anything at all", None)
    assert grade.passed is True
    assert "ungraded" in grade.reason


async def test_run_eval_suite_all_pass():
    agent = await _make_agent("agent-eval-1")
    suite = EvalSuite(name="basic").add(
        EvalCase(name="case-1", input_content="what is the answer?", expected_output="42")
    ).add(
        EvalCase(name="case-2", input_content="tell me again", expected_output="42")
    )

    report = await run_eval_suite(agent, suite)

    assert report.total == 2
    assert report.passed == 2
    assert report.failed == 0
    assert report.pass_rate == 1.0
    assert report.average_score == 1.0
    assert all(r.error is None for r in report.results)
    assert all(r.latency_ms >= 0 for r in report.results)

    await agent.close()


async def test_run_eval_suite_mixed_pass_fail():
    agent = await _make_agent("agent-eval-2")
    suite = EvalSuite(name="mixed").add(
        EvalCase(name="matches", input_content="q1", expected_output="42")
    ).add(
        EvalCase(name="does-not-match", input_content="q2", expected_output="the answer is 99")
    )

    report = await run_eval_suite(agent, suite)

    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.average_score == 0.5

    failed_result = [r for r in report.results if not r.grade.passed][0]
    assert failed_result.case_name == "does-not-match"
    assert failed_result.error is None  # graded failure, not an execution error

    await agent.close()


async def test_run_eval_suite_records_execution_errors_as_failures():
    agent = await _make_agent("agent-eval-3", cls=FailingAgent)
    suite = EvalSuite(name="failing").add(
        EvalCase(name="will-blow-up", input_content="anything")
    )

    report = await run_eval_suite(agent, suite)

    assert report.total == 1
    assert report.passed == 0
    assert report.failed == 1
    result = report.results[0]
    assert result.error is not None
    assert "agent logic blew up" in result.error
    assert result.grade.passed is False
    assert result.grade.score == 0.0

    await agent.close()


async def test_run_eval_suite_with_custom_case_level_grader():
    agent = await _make_agent("agent-eval-4")

    def exact_match_grader(actual: str, expected):
        passed = actual == expected
        return GradeResult(score=1.0 if passed else 0.0, passed=passed, reason="exact match")

    suite = EvalSuite(name="custom-grader").add(
        EvalCase(name="strict", input_content="q", expected_output="won't match exactly", grader=exact_match_grader)
    )

    report = await run_eval_suite(agent, suite)

    assert report.total == 1
    assert report.passed == 0
    assert report.results[0].grade.reason == "exact match"

    await agent.close()


async def test_run_eval_suite_with_suite_level_grader_override():
    agent = await _make_agent("agent-eval-5")

    def always_pass_grader(actual: str, expected):
        return GradeResult(score=1.0, passed=True, reason="always passes")

    suite = EvalSuite(name="lenient").add(
        EvalCase(name="whatever", input_content="q", expected_output="will never match this")
    )

    report = await run_eval_suite(agent, suite, grader=always_pass_grader)

    assert report.passed == 1
    assert report.results[0].grade.reason == "always passes"

    await agent.close()


async def test_run_eval_suite_empty_suite_reports_zero_totals():
    agent = await _make_agent("agent-eval-6")
    suite = EvalSuite(name="empty")

    report = await run_eval_suite(agent, suite)

    assert report.total == 0
    assert report.passed == 0
    assert report.failed == 0
    assert report.average_score == 0.0
    assert report.pass_rate == 0.0

    await agent.close()


async def test_run_eval_suite_isolates_sessions_between_cases_by_default():
    """Each case gets its own session unless session_id is explicitly
    shared -- proves cases don't leak working-memory state into each
    other via the session-scoped episodic/working memory, which would
    silently skew eval results."""
    agent = await _make_agent("agent-eval-7")

    started_sessions = []
    original_start_session = agent.start_session

    def recording_start_session(session_id=None):
        sid = original_start_session(session_id)
        started_sessions.append(sid)
        return sid

    agent.start_session = recording_start_session

    suite = EvalSuite(name="isolation").add(
        EvalCase(name="a", input_content="q1")
    ).add(
        EvalCase(name="b", input_content="q2")
    )

    await run_eval_suite(agent, suite)

    assert len(started_sessions) == 2
    assert started_sessions[0] != started_sessions[1]

    await agent.close()
