"""Tests for Vouchstone Forge (C7).

Acceptance criteria: a customization request, run through any of the
supported underlying engines, produces a diff; the diff runs through the
compatibility gate regardless of which engine generated it; pass/fail is
signed and logged as a WorkflowTrace entry.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from vouchstone_sdk import (
    ClaudeEngineAdapter,
    CompatibilityGate,
    Diff,
    EchoEngineAdapter,
    FileChange,
    Forge,
    Policy,
    PolicyGraph,
    SubprocessSandboxRunner,
    WorkflowTrace,
    opencode_dual_signoff_policy_graph,
)


def _echo_engine(new_content: str, file_path: str = "handler.py") -> EchoEngineAdapter:
    def transform(instruction, current_files):
        return Diff(
            description=instruction,
            changes=[FileChange(
                file_path=file_path,
                original_content=current_files.get(file_path, ""),
                new_content=new_content,
            )],
            engine_name="echo-reference",
        )
    return EchoEngineAdapter(transform)


# ============================================================
# EchoEngineAdapter (pluggability)
# ============================================================

async def test_echo_engine_produces_a_diff():
    engine = _echo_engine("print('hello')")
    diff = await engine.propose_change("say hello", {"files": {}})
    assert diff.engine_name == "echo-reference"
    assert len(diff.changes) == 1
    assert diff.changes[0].file_path == "handler.py"
    assert diff.changes[0].new_content == "print('hello')"


# ============================================================
# CompatibilityGate
# ============================================================

def test_gate_rejects_invalid_python_syntax():
    gate = CompatibilityGate()
    diff = Diff(description="bad", changes=[FileChange("h.py", "", "def f(:\n  pass")], engine_name="test")
    result = gate.evaluate(diff)
    assert result.allow is False
    assert "structural validation failed" in result.reason
    assert any("invalid Python syntax" in e for e in result.structural_errors)


def test_gate_rejects_invalid_json():
    gate = CompatibilityGate()
    diff = Diff(description="bad", changes=[FileChange("config.json", "{}", "{not valid json")], engine_name="test")
    result = gate.evaluate(diff)
    assert result.allow is False
    assert any("invalid JSON" in e for e in result.structural_errors)


def test_gate_defaults_to_deny_with_no_policies():
    gate = CompatibilityGate()
    diff = Diff(description="fine", changes=[FileChange("h.py", "", "print(1)")], engine_name="test")
    result = gate.evaluate(diff)
    assert result.allow is False
    assert "default deny" in result.policy_decision.reason


def test_gate_allows_with_matching_permit_policy():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(
        name="permit handler changes", effect="permit",
        action={"eq": "forge.apply_change"},
        conditions=[{"path": "resource.file_path", "op": "eq", "value": "handler.py"}],
    ))
    gate = CompatibilityGate(policy_graph)
    diff = Diff(description="fine", changes=[FileChange("handler.py", "", "print(1)")], engine_name="test")
    result = gate.evaluate(diff)
    assert result.allow is True


def test_gate_branches_obligations_by_engine_opencode_vs_claude():
    """Proves the gate actually branches differently depending on which
    engine produced the diff (item 3 of Phase 5): the same file, same
    instruction, run through opencode_dual_signoff_policy_graph() via two
    Diffs that differ only in engine_name -- opencode picks up an extra
    require_dual_signoff obligation that claude-direct does not, and both
    remain allowed (this is an obligations divergence, not a deny)."""
    gate = CompatibilityGate(opencode_dual_signoff_policy_graph())

    opencode_diff = Diff(
        description="patch prod handler", engine_name="opencode",
        changes=[FileChange("/prod/handler.py", "", "print('patched')")],
    )
    claude_diff = Diff(
        description="patch prod handler", engine_name="claude-direct",
        changes=[FileChange("/prod/handler.py", "", "print('patched')")],
    )

    # No explicit principal passed -- proves the gate's own default
    # (principal = {"engine": diff.engine_name}) is what drives the branch.
    opencode_result = gate.evaluate(opencode_diff)
    claude_result = gate.evaluate(claude_diff)

    assert opencode_result.allow is True
    assert claude_result.allow is True
    assert "require_dual_signoff" in opencode_result.policy_decision.obligations
    assert "require_dual_signoff" not in claude_result.policy_decision.obligations


def test_gate_dual_signoff_policy_does_not_apply_outside_prod():
    """The engine-discriminating policy is scoped to /prod/*.py -- an
    opencode change to a non-prod file must not pick up the obligation."""
    gate = CompatibilityGate(opencode_dual_signoff_policy_graph())
    diff = Diff(
        description="patch dev handler", engine_name="opencode",
        changes=[FileChange("/dev/handler.py", "", "print('patched')")],
    )
    result = gate.evaluate(diff)
    assert result.allow is True
    assert "require_dual_signoff" not in result.policy_decision.obligations


def test_gate_forbid_blocks_even_with_valid_syntax():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit all", effect="permit", action={"eq": "forge.apply_change"}, priority=100))
    policy_graph.add_policy(Policy(
        name="forbid secrets", effect="forbid", action={"eq": "forge.apply_change"}, priority=10,
        conditions=[{"path": "resource.file_path", "op": "eq", "value": "secrets.py"}],
    ))
    gate = CompatibilityGate(policy_graph)
    diff = Diff(description="steal secrets", changes=[FileChange("secrets.py", "", "API_KEY = 'x'")], engine_name="test")
    result = gate.evaluate(diff)
    assert result.allow is False
    assert "forbid secrets" in result.reason


# ============================================================
# SubprocessSandboxRunner
# ============================================================

async def test_sandbox_runner_reports_success_for_working_code():
    runner = SubprocessSandboxRunner()
    result = await runner.run(FileChange("ok.py", "", "print('it works')"))
    assert result.success is True
    assert "it works" in result.output


async def test_sandbox_runner_reports_failure_for_raising_code():
    runner = SubprocessSandboxRunner()
    result = await runner.run(FileChange("bad.py", "", "raise ValueError('boom')"))
    assert result.success is False
    assert "boom" in result.error


async def test_sandbox_runner_skips_non_python_files():
    runner = SubprocessSandboxRunner()
    result = await runner.run(FileChange("config.json", "{}", '{"a": 1}'))
    assert result.success is True
    assert "skipped" in result.output


async def test_sandbox_runner_times_out_long_running_code():
    runner = SubprocessSandboxRunner(timeout_seconds=0.2)
    result = await runner.run(FileChange("slow.py", "", "import time; time.sleep(5)"))
    assert result.success is False
    assert "timed out" in result.error


# ============================================================
# Forge orchestrator end-to-end
# ============================================================

async def test_forge_full_success_path_records_signed_trace():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit handler", effect="permit", action={"eq": "forge.apply_change"}))
    trace = WorkflowTrace()
    forge = Forge(gate=CompatibilityGate(policy_graph), sandbox_runner=SubprocessSandboxRunner(), trace=trace)

    engine = _echo_engine("print('deployed')")
    result = await forge.request_change("deploy the handler", {"files": {}}, engine=engine)

    assert result.passed is True
    assert result.gate_result.allow is True
    assert len(result.sandbox_results) == 1
    assert result.sandbox_results[0].success is True
    assert result.trace_entry.kind == "forge.change_evaluated"
    assert result.trace_entry.payload["passed"] is True
    assert result.trace_entry.payload["engine"] == "echo-reference"
    assert trace.verify_chain() is True


async def test_forge_gate_denial_skips_sandbox_and_records_failure():
    trace = WorkflowTrace()
    # No policies configured -- default deny.
    forge = Forge(sandbox_runner=SubprocessSandboxRunner(), trace=trace)

    engine = _echo_engine("print('should not run')")
    result = await forge.request_change("deploy anyway", {"files": {}}, engine=engine)

    assert result.passed is False
    assert result.gate_result.allow is False
    assert result.sandbox_results == []  # gate denied -- sandbox never invoked
    assert trace.entries[0].payload["passed"] is False
    assert trace.entries[0].payload["gate_allow"] is False


async def test_forge_sandbox_failure_marks_change_not_passed_even_if_gate_allows():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit all", effect="permit", action={"eq": "forge.apply_change"}))
    trace = WorkflowTrace()
    forge = Forge(gate=CompatibilityGate(policy_graph), sandbox_runner=SubprocessSandboxRunner(), trace=trace)

    engine = _echo_engine("raise RuntimeError('deploy broke prod')")
    result = await forge.request_change("deploy the broken handler", {"files": {}}, engine=engine)

    assert result.gate_result.allow is True  # gate said fine structurally + policy-wise
    assert result.sandbox_results[0].success is False  # but it doesn't actually run
    assert result.passed is False  # so the overall result is not passed
    assert trace.entries[0].payload["gate_allow"] is True
    assert trace.entries[0].payload["sandbox_passed"] is False


async def test_forge_multiple_requests_chain_the_same_trace():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit all", effect="permit", action={"eq": "forge.apply_change"}))
    trace = WorkflowTrace()
    forge = Forge(gate=CompatibilityGate(policy_graph), sandbox_runner=SubprocessSandboxRunner(), trace=trace)

    await forge.request_change("first change", {"files": {}}, engine=_echo_engine("print(1)"))
    await forge.request_change("second change", {"files": {}}, engine=_echo_engine("print(2)"))

    assert len(trace.entries) == 2
    assert trace.entries[1].prev_hash == trace.entries[0].entry_hash
    assert trace.verify_chain() is True


async def test_forge_structural_failure_never_reaches_sandbox():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit all", effect="permit", action={"eq": "forge.apply_change"}))
    sandbox = SubprocessSandboxRunner()
    forge = Forge(gate=CompatibilityGate(policy_graph), sandbox_runner=sandbox)

    engine = _echo_engine("def f(:\n  totally broken syntax")
    result = await forge.request_change("bad change", {"files": {}}, engine=engine)

    assert result.gate_result.allow is False
    assert result.sandbox_results == []
    assert result.passed is False


# ============================================================
# ClaudeEngineAdapter — real request/response handling, mocked transport
# (never hits the live API in tests: no network calls, no API costs).
# ============================================================

async def test_claude_engine_adapter_parses_response_into_diff():
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = (
        '{"description": "add logging", '
        '"changes": [{"file_path": "handler.py", "new_content": "import logging\\nlogging.info(1)"}]}'
    )
    fake_response = MagicMock()
    fake_response.content = [fake_text_block]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        adapter = ClaudeEngineAdapter(api_key="test-key")
        diff = await adapter.propose_change(
            "add logging to the handler", {"files": {"handler.py": "def handle(): pass"}},
        )

    assert diff.engine_name == "claude-direct"
    assert diff.description == "add logging"
    assert len(diff.changes) == 1
    assert diff.changes[0].file_path == "handler.py"
    assert diff.changes[0].original_content == "def handle(): pass"
    assert "logging.info" in diff.changes[0].new_content

    # Confirm the adapter actually called the API with the expected shape.
    mock_client.messages.create.assert_awaited_once()
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-6"
    assert "add logging to the handler" in call_kwargs["messages"][0]["content"]


async def test_claude_engine_adapter_handles_response_wrapped_in_prose():
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = (
        "Here's the change:\n\n"
        '{"description": "fix bug", "changes": [{"file_path": "a.py", "new_content": "x = 1"}]}'
        "\n\nLet me know if you need anything else!"
    )
    fake_response = MagicMock()
    fake_response.content = [fake_text_block]

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        adapter = ClaudeEngineAdapter()
        diff = await adapter.propose_change("fix the bug", {"files": {}})

    assert diff.description == "fix bug"
    assert diff.changes[0].new_content == "x = 1"


async def test_forge_end_to_end_with_claude_engine_mocked():
    """Full acceptance-criteria proof using the real (mocked-transport)
    Claude adapter, not just the trivial echo one -- the gate and trace
    pipeline apply identically."""
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = '{"description": "greet", "changes": [{"file_path": "greet.py", "new_content": "print(\\"hi\\")"}]}'
    fake_response = MagicMock()
    fake_response.content = [fake_text_block]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)

    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit all", effect="permit", action={"eq": "forge.apply_change"}))
    trace = WorkflowTrace()
    forge = Forge(gate=CompatibilityGate(policy_graph), sandbox_runner=SubprocessSandboxRunner(), trace=trace)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        result = await forge.request_change("add a greeting", {"files": {}}, engine=ClaudeEngineAdapter())

    assert result.passed is True
    assert result.diff.engine_name == "claude-direct"
    assert result.trace_entry.payload["engine"] == "claude-direct"
    assert trace.verify_chain() is True
