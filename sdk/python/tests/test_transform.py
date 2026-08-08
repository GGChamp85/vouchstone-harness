"""Tests for the Deterministic Transformation Engine (C7c).

Acceptance criteria: for the top N most common customization requests
(threshold changes, policy rule additions), the engine produces a
templated, parameterized change instead of free-form generation; a
replay of a signed past decision against the same pinned inputs
reproduces the same verification outcome.
"""
import pytest

from vouchstone_sdk import (
    CompatibilityGate,
    Diff,
    EchoEngineAdapter,
    FileChange,
    Forge,
    MissingTemplateParametersError,
    Policy,
    PolicyGraph,
    TemplateEngineAdapter,
    TemplateLibrary,
    TemplateNotMatchedError,
    WorkflowTrace,
    default_template_library,
    replay_and_verify,
)

# ============================================================
# TransformationTemplate — pure, deterministic rendering
# ============================================================

def test_template_render_is_pure_and_deterministic():
    library = default_template_library()
    template = library.get("adjust_approval_threshold")
    params = {"threshold_usd": 5000}

    out1 = template.render(params)
    out2 = template.render(params)
    assert out1 == out2  # byte-identical, every time
    assert "5000" in out1


def test_template_render_raises_on_missing_required_params():
    library = default_template_library()
    template = library.get("adjust_approval_threshold")
    with pytest.raises(MissingTemplateParametersError) as exc_info:
        template.render({})  # missing threshold_usd
    assert "threshold_usd" in exc_info.value.missing


def test_template_matches_by_keyword():
    library = default_template_library()
    template = library.get("adjust_approval_threshold")
    assert template.matches("please raise the approval threshold to 10000")
    assert not template.matches("send me the quarterly report")


# ============================================================
# TemplateLibrary
# ============================================================

def test_library_find_match_returns_first_matching_template():
    library = default_template_library()
    assert library.find_match("increase the approval limit please").id == "adjust_approval_threshold"
    assert library.find_match("add a policy rule for invoice approval").id == "add_policy_rule"
    assert library.find_match("do something totally unrelated") is None


def test_library_starts_with_named_worked_examples():
    """Acceptance criteria explicitly names threshold changes and policy
    rule additions as the starting worked examples."""
    library = default_template_library()
    assert library.get("adjust_approval_threshold") is not None
    assert library.get("add_policy_rule") is not None
    assert len(library) >= 2


# ============================================================
# TemplateEngineAdapter — templated path
# ============================================================

async def test_template_engine_produces_templated_diff_for_threshold_change():
    library = default_template_library()
    engine = TemplateEngineAdapter(library)

    diff = await engine.propose_change(
        "please raise the approval threshold",
        {"template_params": {"threshold_usd": 7500}},
    )

    assert diff.metadata["templated"] is True
    assert diff.metadata["template_id"] == "adjust_approval_threshold"
    assert diff.metadata["params"] == {"threshold_usd": 7500}
    assert "7500" in diff.changes[0].new_content


async def test_template_engine_produces_templated_diff_for_policy_addition():
    library = default_template_library()
    engine = TemplateEngineAdapter(library)

    diff = await engine.propose_change(
        "add a policy rule to permit invoice auto-approval",
        {"template_params": {"policy_name": "auto-approve small invoices", "effect": "permit", "action": "invoice.approve"}},
    )

    assert diff.metadata["template_id"] == "add_policy_rule"
    assert "auto-approve small invoices" in diff.changes[0].new_content


async def test_template_engine_raises_when_no_match_and_no_fallback():
    library = default_template_library()
    engine = TemplateEngineAdapter(library)  # no fallback configured

    with pytest.raises(TemplateNotMatchedError):
        await engine.propose_change("do something completely novel and unforeseen", {})


async def test_template_engine_falls_back_to_free_form_engine_when_no_match():
    library = default_template_library()

    def transform(instruction, current_files):
        return Diff(
            description=instruction,
            changes=[FileChange("novel.py", "", "print('improvised')")],
            engine_name="echo-reference",
        )
    fallback = EchoEngineAdapter(transform)
    engine = TemplateEngineAdapter(library, fallback_engine=fallback)

    diff = await engine.propose_change("do something completely novel and unforeseen", {})

    assert diff.metadata["templated"] is False
    assert diff.metadata["improvised_by"] == "echo-reference"
    assert diff.changes[0].new_content == "print('improvised')"


# ============================================================
# Full Forge pipeline with templated changes
# ============================================================

async def test_forge_with_template_engine_end_to_end():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit config changes", effect="permit", action={"eq": "forge.apply_change"}))
    trace = WorkflowTrace()
    forge = Forge(gate=CompatibilityGate(policy_graph), trace=trace, sandbox_runner=None)

    library = default_template_library()
    engine = TemplateEngineAdapter(library)

    result = await forge.request_change(
        "please raise the approval threshold",
        {"template_params": {"threshold_usd": 9000}},
        engine=engine,
        run_sandbox=False,
    )

    assert result.passed is True
    assert result.trace_entry.payload["diff_metadata"]["templated"] is True
    assert result.trace_entry.payload["diff_metadata"]["template_id"] == "adjust_approval_threshold"


# ============================================================
# Replay verification — the actual reproducibility proof
# ============================================================

async def test_replay_reproduces_identical_gate_outcome():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit config changes", effect="permit", action={"eq": "forge.apply_change"}))
    gate = CompatibilityGate(policy_graph)
    trace = WorkflowTrace()
    forge = Forge(gate=gate, trace=trace, sandbox_runner=None)

    library = default_template_library()
    engine = TemplateEngineAdapter(library)

    result = await forge.request_change(
        "raise the threshold",
        {"template_params": {"threshold_usd": 12000}},
        engine=engine,
        run_sandbox=False,
    )

    # Simulate loading the signed trace entry back later (e.g. from a
    # persisted ledger) and replaying it against the same library + gate.
    replay = replay_and_verify(result.trace_entry.payload, library, gate)

    assert replay.reproducible is True
    assert replay.original_gate_allow == replay.replayed_gate_allow is True
    assert "12000" in replay.replayed_content


async def test_replay_detects_policy_change_since_original_decision():
    policy_graph = PolicyGraph()
    policy_graph.add_policy(Policy(name="permit config changes", effect="permit", action={"eq": "forge.apply_change"}))
    gate = CompatibilityGate(policy_graph)
    trace = WorkflowTrace()
    forge = Forge(gate=gate, trace=trace, sandbox_runner=None)

    library = default_template_library()
    engine = TemplateEngineAdapter(library)

    result = await forge.request_change(
        "raise the threshold", {"template_params": {"threshold_usd": 12000}},
        engine=engine, run_sandbox=False,
    )
    assert result.passed is True

    # Policy tightens after the fact -- a real scenario (compliance
    # requirement added later). Replay against the NEW gate state should
    # now show the decision would come out differently today.
    stricter_policy_graph = PolicyGraph()
    stricter_policy_graph.add_policy(Policy(
        name="forbid config changes", effect="forbid", action={"eq": "forge.apply_change"},
    ))
    stricter_gate = CompatibilityGate(stricter_policy_graph)

    replay = replay_and_verify(result.trace_entry.payload, library, stricter_gate)
    assert replay.reproducible is False
    assert replay.original_gate_allow is True
    assert replay.replayed_gate_allow is False
    assert "gate decision changed" in replay.reason


def test_replay_reports_not_reproducible_for_improvised_changes():
    """Free-form (improvised) diffs have nothing pinned to replay against
    by design -- that's the whole point of the templated/improvised
    distinction."""
    library = default_template_library()
    gate = CompatibilityGate()
    payload = {
        "diff_metadata": {"templated": False, "improvised_by": "claude-direct"},
        "gate_allow": True,
    }
    replay = replay_and_verify(payload, library, gate)
    assert replay.reproducible is False
    assert "nothing to replay" in replay.reason


def test_replay_handles_missing_template_gracefully():
    library = TemplateLibrary()  # empty -- template_id won't be found
    gate = CompatibilityGate()
    payload = {
        "diff_metadata": {"templated": True, "template_id": "does_not_exist", "params": {}},
        "gate_allow": True,
    }
    replay = replay_and_verify(payload, library, gate)
    assert replay.reproducible is False
    assert "not found" in replay.reason
