"""The Dynamic Agent Harness — a governed tool-use loop where **no tool
fires unchecked and every turn is hash-chained**.

``HarnessAgent`` is the concrete, batteries-included agent the abstract
``Agent`` base never provided: a real LLM loop (any provider via the
unified LLM core — OpenAI, Anthropic, or OpenRouter's any-model gateway)
in which

1. every tool call is evaluated against a deny-by-default
   :class:`~vouchstone_sdk.graph.PolicyGraph` **before** it executes,
2. every event — turn, tool call, tool result, denial, approval — is
   appended to a :class:`~vouchstone_sdk.graph.WorkflowTrace` (the same
   hash-chain scheme as the control plane's signed ledger), and
3. a security **posture** (QM-inspired) decides what obligations mean:
   ``AUTO`` logs-and-proceeds, ``STRICT`` requires a synchronous human
   approval callback for any obligated call.

:class:`Scope` makes an agent's Knowledge-Graph boundary *enforced*, not
advisory: the same object bounds what memory retrieval can surface
(domains/entity kinds — wired through the existing MemoryPipeline filters)
and what the tool loop may do (allowed tools become the PolicyGraph's only
permits), and Phase D exports it as OpenCode ``permission:`` rules.

:class:`ToolRegistry` derives real JSON-schema parameters from function
signatures — ``Agent.register_tool`` historically recorded empty schemas
and had no dispatch path at all.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, get_type_hints

from .agent import Agent, AgentConfig
from .graph import Policy, PolicyDecision, PolicyGraph, WorkflowTrace
from .llm import LLMProvider, ToolCallRequest, resolve_provider
from .types import AgentResponse, MemoryContext, Message

logger = logging.getLogger("vouchstone.harness")


# ============================================================
# Scope — an enforced KG boundary (user directive #3)
# ============================================================

@dataclass
class Scope:
    """An agent's explicit boundary against the knowledge graph.

    ``domains``/``entity_types``/``tags`` bound what memory retrieval can
    surface (they feed AgentConfig.scoped_domains / scoped_subgraph).
    ``allowed_tools`` bounds what the harness loop may execute: it compiles
    into the *only* permit rules of a deny-by-default PolicyGraph — a tool
    outside the boundary isn't discouraged, it is structurally impossible.
    ``namespace`` isolates memory keys (QM's per-person/per-room isolation):
    two scopes over the same agent share nothing.
    """

    domains: list[str] | None = None
    entity_types: list[str] | None = None
    tags: list[str] | None = None
    allowed_tools: list[str] | None = None
    namespace: str | None = None

    def to_policy_graph(self) -> PolicyGraph:
        graph = PolicyGraph()
        if self.allowed_tools is None:
            graph.add_policy(Policy(
                name="scope-allow-all-tools", effect="permit",
                action={"eq": "tool.invoke"},
            ))
        else:
            for tool in self.allowed_tools:
                graph.add_policy(Policy(
                    name=f"scope-allow-{tool}", effect="permit",
                    action={"eq": "tool.invoke"},
                    conditions=[{"path": "resource.tool", "op": "eq", "value": tool}],
                ))
        return graph

    def memory_kwargs(self) -> dict[str, Any]:
        """Kwargs for AgentConfig / MemoryPipeline scoping fields."""
        scoped_subgraph = None
        if self.entity_types or self.tags:
            scoped_subgraph = [*(self.entity_types or []), *(self.tags or [])]
        return {"scoped_domains": self.domains, "scoped_subgraph": scoped_subgraph}

    def scoped_agent_id(self, agent_id: str) -> str:
        return f"{agent_id}::{self.namespace}" if self.namespace else agent_id


# ============================================================
# ToolRegistry — real parameter schemas, real dispatch
# ============================================================

_JSON_TYPES: dict[type, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _schema_for_callable(func: Callable[..., Any]) -> dict[str, Any]:
    """JSON-schema ``parameters`` derived from the function's signature and
    type hints — unannotated parameters become strings (documented), and
    parameters without defaults are required."""
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # unresolvable forward refs: degrade to strings
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self" or param.kind in (
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        hint = hints.get(name)
        origin = getattr(hint, "__origin__", hint)
        if isinstance(origin, type):
            json_type = _JSON_TYPES.get(origin, "string")
        else:
            json_type = "string"
        properties[name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


@dataclass
class RegisteredTool:
    name: str
    description: str
    func: Callable[..., Any]
    parameters: dict[str, Any]

    def spec(self) -> dict[str, Any]:
        """OpenAI-style tool spec (the LLM core translates per provider)."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
        }}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, func: Callable[..., Any], *, name: str | None = None,
                 description: str | None = None) -> RegisteredTool:
        tool = RegisteredTool(
            name=name or func.__name__,
            description=description or (inspect.getdoc(func) or "").split("\n")[0],
            func=func,
            parameters=_schema_for_callable(func),
        )
        self._tools[tool.name] = tool
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> RegisteredTool:
        if name not in self._tools:
            raise KeyError(f"no tool named {name!r} (registered: {self.names()})")
        return self._tools[name]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.get(name)
        result = tool.func(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, str) else json.dumps(result, default=str)


# ============================================================
# Postures + predeclared command policy (QM-inspired)
# ============================================================

class HarnessPosture(str, Enum):
    #: Obligations are logged and traced; execution proceeds.
    AUTO = "auto"
    #: ANY obligation on a permitted tool call requires the synchronous
    #: approval callback to return True, or the call is denied.
    STRICT = "strict"


ApprovalCallback = Callable[[ToolCallRequest, PolicyDecision], Awaitable[bool]]


@dataclass
class CommandPolicy:
    """Predeclared shell-command policy for :func:`make_shell_tool` —
    allow-patterns first, then deny-patterns override. Deny-by-default:
    a command matching no allow pattern never runs."""

    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)

    def check(self, command: str) -> tuple[bool, str]:
        for pattern in self.deny_patterns:
            if re.search(pattern, command):
                return False, f"command matches deny pattern {pattern!r}"
        for pattern in self.allow_patterns:
            if re.fullmatch(pattern, command):
                return True, f"command matches allow pattern {pattern!r}"
        return False, "command matches no allow pattern (deny-by-default)"


def make_shell_tool(policy: CommandPolicy, *, timeout_seconds: float = 30.0) -> Callable[..., Awaitable[str]]:
    """A governed shell tool: every command is checked against the
    predeclared :class:`CommandPolicy` before it runs; a rejected command
    returns the denial reason to the model instead of executing."""

    async def run_shell(command: str) -> str:
        """Run a shell command (governed by a predeclared command policy)."""
        allowed, reason = policy.check(command)
        if not allowed:
            return f"DENIED: {reason}"
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"ERROR: command timed out after {timeout_seconds}s"
        return stdout.decode(errors="replace")

    return run_shell


# ============================================================
# HarnessAgent — the governed loop
# ============================================================

class HarnessAgent(Agent):
    """A complete agent: LLM loop + policy-gated tools + hash-chained trace.

    Every ``run()`` produces trace entries for the turn itself and for each
    tool call's decision/outcome — making ``graph.py``'s "every run gets an
    append-only, verifiable record" promise actually true.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: ToolRegistry | None = None,
        scope: Scope | None = None,
        policy_graph: PolicyGraph | None = None,
        posture: HarnessPosture = HarnessPosture.AUTO,
        approval_callback: ApprovalCallback | None = None,
        trace: WorkflowTrace | None = None,
        provider: LLMProvider | None = None,
        max_iterations: int = 8,
    ):
        if scope is not None:
            merged = scope.memory_kwargs()
            if config.scoped_domains is None:
                config.scoped_domains = merged["scoped_domains"]
            if config.scoped_subgraph is None:
                config.scoped_subgraph = merged["scoped_subgraph"]
        super().__init__(config)
        self.tools = tools or ToolRegistry()
        self.scope = scope
        # Explicit policy graph wins; otherwise the scope compiles to one;
        # otherwise deny-by-default with no permits -- a HarnessAgent with
        # neither scope nor policies cannot run any tool, on purpose.
        if policy_graph is not None:
            self.policy_graph = policy_graph
        elif scope is not None:
            self.policy_graph = scope.to_policy_graph()
        else:
            self.policy_graph = PolicyGraph()
        self.posture = posture
        self.approval_callback = approval_callback
        self.trace = trace or WorkflowTrace()
        self.provider: LLMProvider
        self.model_id: str
        if provider is not None:
            self.provider = provider
            self.model_id = config.model
        else:
            self.provider, self.model_id = resolve_provider(config.model)
        self.max_iterations = max_iterations

    async def initialize(self, agent_id: str | None = None, **kwargs: Any):  # type: ignore[override]
        if self.scope is not None and agent_id is not None:
            agent_id = self.scope.scoped_agent_id(agent_id)
        return await super().initialize(agent_id=agent_id, **kwargs)

    # ── the loop ───────────────────────────────────────────────────────

    def _context_block(self, context: MemoryContext) -> str:
        parts: list[str] = []
        if context.semantic_entities:
            parts.append("Known entities:\n" + "\n".join(
                f"- [{e.entity_type}] {e.entity_key}" for e in context.semantic_entities[:15]
            ))
        if context.procedural_skills:
            parts.append("Known skills:\n" + "\n".join(
                f"- {s.name}: {s.description}" for s in context.procedural_skills[:10]
            ))
        return "\n\n".join(parts)

    def _evaluate_call(self, call: ToolCallRequest) -> PolicyDecision:
        return self.policy_graph.evaluate(
            principal={
                "agent": self.config.name,
                "scope_namespace": self.scope.namespace if self.scope else None,
            },
            action="tool.invoke",
            resource={"tool": call.name, "arguments": call.arguments},
            context={"posture": self.posture.value},
        )

    async def run(self, message: Message, context: MemoryContext) -> AgentResponse:
        system_parts = [self.config.system_prompt or
                        f"You are {self.config.name}, a governed Vouchstone agent."]
        ctx_block = self._context_block(context)
        if ctx_block:
            system_parts.append(ctx_block)
        system = "\n\n".join(system_parts)

        messages: list[dict[str, Any]] = [{"role": "user", "content": message.content}]
        tools_used: list[str] = []
        usage = {"tokens_in": 0, "tokens_out": 0}
        started = time.monotonic()

        for iteration in range(1, self.max_iterations + 1):
            response = await self.provider.chat(
                messages, model=self.model_id, tools=self.tools.specs() or None,
                system=system, temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
            usage["tokens_in"] += response.usage.get("tokens_in", 0)
            usage["tokens_out"] += response.usage.get("tokens_out", 0)
            self.trace.append("harness.turn", {
                "agent": self.config.name, "iteration": iteration,
                "model": self.model_id, "provider": self.provider.provider_name,
                "tool_calls": [c.name for c in response.tool_calls],
                "content_preview": (response.content or "")[:200],
            }, actor=self.config.name)

            if not response.tool_calls:
                return AgentResponse(
                    content=response.content,
                    tool_calls=[{"name": n} for n in tools_used],
                    usage={**usage, "latency_ms": int((time.monotonic() - started) * 1000)},
                    metadata={"iterations": iteration, "trace_tip": self.trace.tip_hash},
                )

            messages.append(self.provider.assistant_message(response))
            for call in response.tool_calls:
                result_msg = await self._execute_governed(call)
                if call.name not in tools_used:
                    tools_used.append(call.name)
                messages.append(result_msg)

        self.trace.append("harness.max_iterations", {
            "agent": self.config.name, "max_iterations": self.max_iterations,
        }, actor=self.config.name)
        return AgentResponse(
            content=(f"Stopped after {self.max_iterations} iterations without a "
                     "final answer -- see the trace for every governed step."),
            tool_calls=[{"name": n} for n in tools_used],
            usage=usage,
            metadata={"iterations": self.max_iterations, "exhausted": True,
                      "trace_tip": self.trace.tip_hash},
        )

    async def _execute_governed(self, call: ToolCallRequest) -> dict[str, Any]:
        """Policy gate -> posture/approval -> dispatch. Every outcome is
        traced, and denials are returned TO THE MODEL as tool errors so it
        can adapt instead of hallucinating a result."""
        decision = self._evaluate_call(call)

        if not decision.allow:
            self.trace.append("harness.tool_denied", {
                "tool": call.name, "arguments": call.arguments,
                "reason": decision.reason,
            }, actor=self.config.name)
            return self.provider.tool_result_message(
                call, f"denied by policy: {decision.reason}", is_error=True,
            )

        if decision.obligations and self.posture is HarnessPosture.STRICT:
            if self.approval_callback is None:
                self.trace.append("harness.tool_denied", {
                    "tool": call.name, "arguments": call.arguments,
                    "reason": "strict posture: obligations present but no approval callback",
                    "obligations": decision.obligations,
                }, actor=self.config.name)
                return self.provider.tool_result_message(
                    call,
                    "denied: strict posture requires human approval "
                    f"(obligations: {decision.obligations}) and no approver is wired",
                    is_error=True,
                )
            approved = await self.approval_callback(call, decision)
            self.trace.append("harness.approval", {
                "tool": call.name, "approved": approved,
                "obligations": decision.obligations,
            }, actor="human")
            if not approved:
                return self.provider.tool_result_message(
                    call, "denied: human approver rejected this call", is_error=True,
                )
        elif decision.obligations:
            self.trace.append("harness.obligations", {
                "tool": call.name, "obligations": decision.obligations,
            }, actor=self.config.name)

        try:
            result = await self.tools.dispatch(call.name, call.arguments)
        except Exception as exc:
            self.trace.append("harness.tool_error", {
                "tool": call.name, "error": str(exc),
            }, actor=self.config.name)
            return self.provider.tool_result_message(call, str(exc), is_error=True)

        self.trace.append("harness.tool_result", {
            "tool": call.name, "result_preview": result[:200],
        }, actor=self.config.name)
        return self.provider.tool_result_message(call, result)
