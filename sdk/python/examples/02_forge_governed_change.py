"""A governed code change through Vouchstone Forge, fully offline.

Demonstrates the harness pillar's core loop on one file:

  engine proposes a diff  ->  compatibility gate (structural check +
  deny-by-default PolicyGraph)  ->  sandbox run  ->  one hash-chained,
  verifiable WorkflowTrace entry recording the whole decision.

Uses EchoEngineAdapter (no LLM, no network) so the governance machinery --
the actual point -- is observable deterministically:

    python examples/02_forge_governed_change.py

Swap in OpenCodeEngineAdapter() or ClaudeEngineAdapter() and nothing else
changes: the gate and the signed trace apply to every engine equally.
"""
import asyncio

from vouchstone_sdk import (
    CompatibilityGate,
    Diff,
    EchoEngineAdapter,
    FileChange,
    Forge,
    Policy,
    PolicyGraph,
    WorkflowTrace,
)

ORIGINAL = 'GREETING = "hello"\n'


def add_exclamation(instruction: str, files: dict[str, str]) -> Diff:
    """The 'engine': appends an exclamation to the greeting constant."""
    new_content = files["config.py"].replace('"hello"', '"hello!"')
    return Diff(
        description=instruction,
        changes=[FileChange("config.py", files["config.py"], new_content)],
        engine_name="echo-reference",
    )


async def main() -> None:
    # Deny-by-default: without this permit, the gate rejects EVERY change.
    policies = PolicyGraph()
    policies.add_policy(Policy(
        name="allow-config-edits",
        effect="permit",
        action={"eq": "forge.apply_change"},
        conditions=[{"path": "resource.file_path", "op": "startswith", "value": "config"}],
        obligations=["log_to_audit"],
    ))

    forge = Forge(
        gate=CompatibilityGate(policy_graph=policies),
        trace=WorkflowTrace(),
    )

    result = await forge.request_change(
        "Make the greeting more enthusiastic",
        {"files": {"config.py": ORIGINAL}},
        engine=EchoEngineAdapter(add_exclamation),
    )

    print(f"gate allowed:    {result.gate_result.allow}")
    decision = result.gate_result.policy_decision
    print(f"obligations:     {decision.obligations if decision else []}")
    print(f"sandbox passed:  {all(r.success for r in result.sandbox_results)}")
    print(f"overall passed:  {result.passed}")

    # The signed audit trail: verifiable by anyone holding the trace.
    print(f"trace verifies:  {forge.trace.verify_chain()}")
    print(f"trace tip hash:  {forge.trace.tip_hash}")


if __name__ == "__main__":
    asyncio.run(main())
