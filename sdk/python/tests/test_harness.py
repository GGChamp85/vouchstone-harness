"""HarnessAgent -- the governed tool-use loop. Driven end-to-end through
Agent.process() with a scripted FakeProvider (no network): asserts that
tools with real derived schemas dispatch, that the deny-by-default
PolicyGraph and Scope boundaries actually block out-of-scope calls (the
model receives the denial as a tool error, not a hallucinated result),
that STRICT posture requires human approval for obligated calls, and that
every event lands on a verifiable hash-chained trace."""
from __future__ import annotations

from typing import Any

import pytest

from vouchstone_sdk.agent import AgentConfig
from vouchstone_sdk.graph import Policy, PolicyGraph
from vouchstone_sdk.harness import (
    CommandPolicy,
    HarnessAgent,
    HarnessPosture,
    Scope,
    ToolRegistry,
    make_shell_tool,
)
from vouchstone_sdk.llm import ChatResponse, LLMProvider, ToolCallRequest, resolve_provider
from vouchstone_sdk.types import Message


class FakeProvider(LLMProvider):
    """Scripted provider: pops one ChatResponse per chat() call and records
    every message list it was given."""

    provider_name = "fake"

    def __init__(self, script: list[ChatResponse]):
        self.script = list(script)
        self.seen_messages: list[list[dict[str, Any]]] = []
        self.seen_tools: list[list[dict[str, Any]] | None] = []

    async def chat(self, messages, *, model, tools=None, system=None,
                   temperature=0.2, max_tokens=4096) -> ChatResponse:
        self.seen_messages.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        return self.script.pop(0)

    def assistant_message(self, response: ChatResponse) -> dict[str, Any]:
        return {"role": "assistant", "content": response.content or "",
                "tool_calls": [c.name for c in response.tool_calls]}

    def tool_result_message(self, call, result, *, is_error=False) -> dict[str, Any]:
        return {"role": "tool", "tool": call.name, "content": result, "is_error": is_error}


def _lookup_invoice(invoice_id: str, include_lines: bool = False) -> dict[str, Any]:
    """Look up an invoice by id."""
    return {"invoice_id": invoice_id, "amount": 1200, "lines": [] if include_lines else None}


async def _erase_database(confirm: bool) -> str:
    """Dangerous: erase the database."""
    return "erased"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_lookup_invoice, name="lookup_invoice")
    registry.register(_erase_database, name="erase_database")
    return registry


async def _make_agent(**kwargs: Any) -> HarnessAgent:
    agent = HarnessAgent(AgentConfig(name="test-harness"), **kwargs)
    await agent.initialize(agent_id="test-harness", local_only=True)
    agent.start_session()
    return agent


# ── ToolRegistry ─────────────────────────────────────────────────────


def test_tool_schema_derived_from_signature():
    registry = _registry()
    spec = registry.get("lookup_invoice").spec()
    params = spec["function"]["parameters"]
    assert params["properties"]["invoice_id"] == {"type": "string"}
    assert params["properties"]["include_lines"] == {"type": "boolean"}
    assert params["required"] == ["invoice_id"]  # default => optional
    assert spec["function"]["description"] == "Look up an invoice by id."


@pytest.mark.asyncio
async def test_dispatch_handles_sync_and_async_tools():
    registry = _registry()
    result = await registry.dispatch("lookup_invoice", {"invoice_id": "INV-1"})
    assert '"invoice_id": "INV-1"' in result
    result2 = await registry.dispatch("erase_database", {"confirm": True})
    assert result2 == "erased"


# ── the governed loop ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_executes_permitted_tool_and_chains_trace():
    provider = FakeProvider([
        ChatResponse(content="", tool_calls=[
            ToolCallRequest(id="c1", name="lookup_invoice",
                            arguments={"invoice_id": "INV-9"}),
        ], usage={"tokens_in": 10, "tokens_out": 5}),
        ChatResponse(content="Invoice INV-9 is $1200.",
                     usage={"tokens_in": 20, "tokens_out": 8}),
    ])
    agent = await _make_agent(
        tools=_registry(),
        scope=Scope(allowed_tools=["lookup_invoice"]),
        provider=provider,
    )
    response = await agent.process(Message(content="How much is INV-9?"))

    assert response.content == "Invoice INV-9 is $1200."
    assert response.tool_calls == [{"name": "lookup_invoice"}]
    assert response.usage["tokens_in"] == 30
    # the tool result reached the model
    tool_msgs = [m for m in provider.seen_messages[1] if m.get("role") == "tool"]
    assert tool_msgs and not tool_msgs[0]["is_error"]

    kinds = [e.kind for e in agent.trace._entries]
    assert kinds == ["harness.turn", "harness.tool_result", "harness.turn"]
    assert agent.trace.verify_chain()
    assert response.metadata["trace_tip"] == agent.trace.tip_hash
    await agent.close()


@pytest.mark.asyncio
async def test_out_of_scope_tool_denied_and_model_told():
    provider = FakeProvider([
        ChatResponse(content="", tool_calls=[
            ToolCallRequest(id="c1", name="erase_database", arguments={"confirm": True}),
        ]),
        ChatResponse(content="I could not erase the database: policy denied it."),
    ])
    agent = await _make_agent(
        tools=_registry(),
        scope=Scope(allowed_tools=["lookup_invoice"]),  # erase_database NOT allowed
        provider=provider,
    )
    response = await agent.process(Message(content="wipe everything"))

    assert "policy denied" in response.content
    tool_msgs = [m for m in provider.seen_messages[1] if m.get("role") == "tool"]
    assert tool_msgs[0]["is_error"] is True
    assert "denied by policy" in tool_msgs[0]["content"]
    kinds = [e.kind for e in agent.trace._entries]
    assert "harness.tool_denied" in kinds
    assert agent.trace.verify_chain()
    await agent.close()


@pytest.mark.asyncio
async def test_no_scope_no_policies_means_deny_by_default():
    provider = FakeProvider([
        ChatResponse(content="", tool_calls=[
            ToolCallRequest(id="c1", name="lookup_invoice",
                            arguments={"invoice_id": "INV-1"}),
        ]),
        ChatResponse(content="done"),
    ])
    agent = await _make_agent(tools=_registry(), provider=provider)
    await agent.process(Message(content="anything"))
    kinds = [e.kind for e in agent.trace._entries]
    assert "harness.tool_denied" in kinds
    assert "harness.tool_result" not in kinds
    await agent.close()


@pytest.mark.asyncio
async def test_strict_posture_requires_approval_for_obligated_calls():
    policies = PolicyGraph()
    policies.add_policy(Policy(
        name="allow-erase-with-signoff", effect="permit",
        action={"eq": "tool.invoke"},
        conditions=[{"path": "resource.tool", "op": "eq", "value": "erase_database"}],
        obligations=["require_dual_signoff"],
    ))

    async def approve(call: ToolCallRequest, decision) -> bool:
        approve.calls.append(call.name)
        return call.name != "erase_database"  # human rejects the erase

    approve.calls = []  # type: ignore[attr-defined]

    provider = FakeProvider([
        ChatResponse(content="", tool_calls=[
            ToolCallRequest(id="c1", name="erase_database", arguments={"confirm": True}),
        ]),
        ChatResponse(content="The human rejected the erase."),
    ])
    agent = await _make_agent(
        tools=_registry(), policy_graph=policies,
        posture=HarnessPosture.STRICT, approval_callback=approve,
        provider=provider,
    )
    await agent.process(Message(content="erase it"))

    assert approve.calls == ["erase_database"]  # type: ignore[attr-defined]
    entries = {e.kind: e for e in agent.trace._entries}
    assert entries["harness.approval"].payload["approved"] is False
    assert entries["harness.approval"].actor == "human"
    assert "harness.tool_result" not in entries
    await agent.close()


@pytest.mark.asyncio
async def test_strict_posture_without_approver_denies():
    policies = PolicyGraph()
    policies.add_policy(Policy(
        name="allow-with-obligation", effect="permit",
        action={"eq": "tool.invoke"}, obligations=["log_to_audit"],
    ))
    provider = FakeProvider([
        ChatResponse(content="", tool_calls=[
            ToolCallRequest(id="c1", name="lookup_invoice",
                            arguments={"invoice_id": "INV-1"}),
        ]),
        ChatResponse(content="blocked"),
    ])
    agent = await _make_agent(
        tools=_registry(), policy_graph=policies,
        posture=HarnessPosture.STRICT, provider=provider,
    )
    await agent.process(Message(content="lookup"))
    denied = [e for e in agent.trace._entries if e.kind == "harness.tool_denied"]
    assert denied and "no approval callback" in denied[0].payload["reason"]
    await agent.close()


@pytest.mark.asyncio
async def test_tool_exception_traced_and_returned_as_error():
    def boom() -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    registry = ToolRegistry()
    registry.register(boom)
    provider = FakeProvider([
        ChatResponse(content="", tool_calls=[
            ToolCallRequest(id="c1", name="boom", arguments={}),
        ]),
        ChatResponse(content="the tool failed"),
    ])
    agent = await _make_agent(tools=registry, scope=Scope(), provider=provider)
    await agent.process(Message(content="go"))
    kinds = [e.kind for e in agent.trace._entries]
    assert "harness.tool_error" in kinds
    tool_msgs = [m for m in provider.seen_messages[1] if m.get("role") == "tool"]
    assert tool_msgs[0]["is_error"] and "kaboom" in tool_msgs[0]["content"]
    await agent.close()


# ── Scope ────────────────────────────────────────────────────────────


def test_scope_memory_kwargs_and_namespace():
    scope = Scope(domains=["finance"], entity_types=["invoice"], tags=["ap"],
                  namespace="team-a")
    kwargs = scope.memory_kwargs()
    assert kwargs["scoped_domains"] == ["finance"]
    assert kwargs["scoped_subgraph"] == ["invoice", "ap"]
    assert scope.scoped_agent_id("agent-1") == "agent-1::team-a"
    assert Scope().scoped_agent_id("agent-1") == "agent-1"


def test_scope_seeds_agent_config_memory_boundaries():
    agent = HarnessAgent(
        AgentConfig(name="scoped"),
        scope=Scope(domains=["billing"], entity_types=["system"]),
        provider=FakeProvider([]),
    )
    assert agent.config.scoped_domains == ["billing"]
    assert agent.config.scoped_subgraph == ["system"]


# ── CommandPolicy / shell tool ───────────────────────────────────────


def test_command_policy_deny_by_default_and_deny_overrides_allow():
    policy = CommandPolicy(allow_patterns=[r"echo .*", r"ls( .*)?"],
                           deny_patterns=[r".*rm .*"])
    assert policy.check("echo hi")[0] is True
    assert policy.check("ls -la")[0] is True
    assert policy.check("curl evil.example")[0] is False
    allowed, reason = policy.check("echo hi && rm -rf /")
    assert allowed is False and "deny pattern" in reason


@pytest.mark.asyncio
async def test_shell_tool_runs_allowed_and_refuses_denied():
    tool = make_shell_tool(CommandPolicy(allow_patterns=[r"echo [\w ]+"]))
    out = await tool("echo governed")
    assert out.strip() == "governed"
    denied = await tool("cat /etc/passwd")
    assert denied.startswith("DENIED:")


# ── provider resolution ──────────────────────────────────────────────


def test_resolve_provider_model_string_routing():
    p, m = resolve_provider("openrouter/meta-llama/llama-3.3-70b-instruct")
    assert p.provider_name == "openrouter"
    assert m == "meta-llama/llama-3.3-70b-instruct"

    p, m = resolve_provider("claude-sonnet-4-6")
    assert p.provider_name == "anthropic" and m == "claude-sonnet-4-6"

    p, m = resolve_provider("anthropic/claude-sonnet-4-6")
    assert p.provider_name == "anthropic" and m == "claude-sonnet-4-6"

    p, m = resolve_provider("gpt-4o-mini")
    assert p.provider_name == "openai" and m == "gpt-4o-mini"

    p, m = resolve_provider("openai/gpt-4o")
    assert p.provider_name == "openai" and m == "gpt-4o"
