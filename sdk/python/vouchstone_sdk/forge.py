"""Vouchstone Forge (C7) — a thin, framework-agnostic orchestration layer
for agent customization requests.

Forge does NOT compete with Google ADK, the Claude Agent SDK, or a
customer's own LangChain/CrewAI setup at the tool-use-loop layer — those
are real, well-funded, well-adopted frameworks and reimplementing one from
scratch here would be both unwinnable and pointless. What Forge actually
owns, and what none of those frameworks do:

1. **KG-grounding** — every proposed change is made in the context of the
   customer's own EntityGraph (C6), not a generic prompt.
2. **The compatibility gate** — every diff, regardless of which engine
   produced it, is checked for structural validity and evaluated against a
   PolicyGraph (C6) before it's considered safe. No engine gets to skip
   this by virtue of being "the trusted one."
3. **Ledger-signed attestation** — every gate decision is appended to a
   WorkflowTrace (C6) using the same hash-chaining algorithm as the
   control plane's signed ledger, so "did this change pass the gate" is
   independently verifiable, not just logged.
4. **A sandboxing wrapper** — a proposed change is actually executed
   before being trusted, not just syntax-checked.

Pluggability is the point: swap in a Google ADK adapter, a Claude Agent
SDK adapter, or a customer's own engine, and the same gate + trace
pipeline applies unconditionally. Only one production-real adapter ships
here (ClaudeEngineAdapter, using the `anthropic` package directly) plus
one deliberately-trivial reference adapter for testing/demos
(EchoEngineAdapter) — implementing real ADK/LangChain/CrewAI adapters
without those being verified, intentionally-installed dependencies would
be exactly the kind of unverified, unrunnable code this project's
"no stubs" standard forbids. See EngineAdapter's docstring for how a real
integration plugs in.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .graph import Policy, PolicyDecision, PolicyGraph, WorkflowTrace
from .telemetry import record_exception, span


# ============================================================
# Diff — a proposed change, engine-agnostic
# ============================================================

@dataclass
class FileChange:
    file_path: str
    original_content: str
    new_content: str


@dataclass
class Diff:
    description: str
    changes: List[FileChange]
    engine_name: str
    # Provenance for reproducibility (C7c). A templated diff sets this to
    # {"template_id": ..., "params": {...}} -- enough to deterministically
    # re-render the exact same output later and confirm a past decision
    # replays identically. Free-form ("improvised") diffs leave this empty,
    # which is itself a signal: nothing to replay against, so the
    # compatibility gate / a human reviewer should weigh it more heavily.
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# EngineAdapter — the pluggable interface
# ============================================================

class EngineAdapter(ABC):
    """Implement this to plug any agent engine into Forge. The only
    contract: turn a natural-language instruction (plus context: current
    file contents, KG entities, whatever the engine needs) into a Diff.
    Everything downstream of propose_change() -- the gate, the sandbox,
    the signed trace -- is identical no matter which engine produced the
    diff. A real Google ADK or LangChain/CrewAI adapter follows this same
    shape: wrap that framework's own agent-execution call, translate its
    output into FileChange objects, done."""

    engine_name: str = "unknown"

    @abstractmethod
    async def propose_change(self, instruction: str, context: Dict[str, Any]) -> Diff:
        ...


def _extract_json_object(text: str) -> str:
    """Anthropic/OpenAI-style responses sometimes wrap JSON in prose or
    code fences despite being asked not to -- pull out the first balanced
    {...} block rather than assuming the whole response is clean JSON."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in engine response: {text[:200]!r}")
    return match.group(0)


class ClaudeEngineAdapter(EngineAdapter):
    """Real, working engine backed directly by the Anthropic API (the
    `anthropic` package is already a core SDK dependency). Not a
    reimplementation of the Claude Agent SDK's tool-use loop -- this is
    Forge's own thin wrapper for the narrow "propose a file-level diff"
    task, which is all the compatibility-gate pipeline needs from an
    engine."""

    engine_name = "claude-direct"

    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model

    async def propose_change(self, instruction: str, context: Dict[str, Any]) -> Diff:
        import anthropic

        current_files: Dict[str, str] = context.get("files", {})
        files_text = "\n\n".join(f"=== {path} ===\n{content}" for path, content in current_files.items())
        entity_context = context.get("entity_summary", "")

        prompt = (
            "You are a code-customization engine. Given the current file "
            "contents and an instruction, propose the exact new full "
            "content for each file that needs to change.\n\n"
            + (f"Relevant known entities:\n{entity_context}\n\n" if entity_context else "")
            + f"Current files:\n{files_text}\n\n"
            f"Instruction: {instruction}\n\n"
            'Return ONLY a JSON object of the form '
            '{"description": "...", "changes": [{"file_path": "...", "new_content": "..."}]}. '
            "No prose, no markdown fences."
        )

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        response = await client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        parsed = json.loads(_extract_json_object(text))

        changes = [
            FileChange(
                file_path=c["file_path"],
                original_content=current_files.get(c["file_path"], ""),
                new_content=c["new_content"],
            )
            for c in parsed["changes"]
        ]
        return Diff(description=parsed.get("description", instruction), changes=changes, engine_name=self.engine_name)


class EchoEngineAdapter(EngineAdapter):
    """Deliberately trivial reference adapter: applies a caller-supplied
    transform function instead of calling any LLM. Exists to prove
    pluggability and for tests/demos that must not depend on network
    access or API credentials -- NOT a production engine, and its
    engine_name says so explicitly."""

    engine_name = "echo-reference"

    def __init__(self, transform: Callable[[str, Dict[str, str]], Diff]):
        self._transform = transform

    async def propose_change(self, instruction: str, context: Dict[str, Any]) -> Diff:
        current_files: Dict[str, str] = context.get("files", {})
        return self._transform(instruction, current_files)


# ============================================================
# Compatibility gate
# ============================================================

@dataclass
class GateResult:
    allow: bool
    reason: str
    structural_errors: List[str] = field(default_factory=list)
    policy_decision: Optional[PolicyDecision] = None


class CompatibilityGate:
    """Every proposed change passes through here before being trusted,
    regardless of which engine produced it. Two checks, in order:

    1. Structural validity -- does the new content even parse for its
       file type (Python via ast.parse, JSON via json.loads; other file
       types pass through unchecked -- there's no universal parser).
    2. PolicyGraph evaluation (C6) -- deny-by-default, same posture as the
       control plane's ABAC engine. A customer configures policies for
       which files/change-types are permitted; with no policies
       configured, every change is denied, matching how a governed tenant
       actually behaves.
    """

    def __init__(self, policy_graph: Optional[PolicyGraph] = None):
        self.policy_graph = policy_graph or PolicyGraph()

    def check_structural_validity(self, change: FileChange) -> Optional[str]:
        if change.file_path.endswith(".py"):
            try:
                ast.parse(change.new_content, filename=change.file_path)
            except SyntaxError as e:
                return f"invalid Python syntax: {e}"
        elif change.file_path.endswith(".json"):
            try:
                json.loads(change.new_content)
            except json.JSONDecodeError as e:
                return f"invalid JSON: {e}"
        return None

    def evaluate(
        self, diff: Diff, *, principal: Optional[Dict[str, Any]] = None, environment: str = "dev",
    ) -> GateResult:
        structural_errors = [
            f"{change.file_path}: {err}"
            for change in diff.changes
            if (err := self.check_structural_validity(change)) is not None
        ]
        if structural_errors:
            return GateResult(allow=False, reason="structural validation failed", structural_errors=structural_errors)

        principal = principal or {"engine": diff.engine_name}
        for change in diff.changes:
            decision = self.policy_graph.evaluate(
                principal=principal, action="forge.apply_change",
                resource={"file_path": change.file_path},
                context={"environment": environment},
            )
            if not decision.allow:
                return GateResult(allow=False, reason=decision.reason, policy_decision=decision)

        return GateResult(allow=True, reason="allow")


# ============================================================
# Sandbox runner — pluggable, one honest reference implementation
# ============================================================

@dataclass
class SandboxResult:
    file_path: str
    success: bool
    output: str = ""
    error: Optional[str] = None


class SandboxRunner(ABC):
    @abstractmethod
    async def run(self, change: FileChange) -> SandboxResult:
        ...


class SubprocessSandboxRunner(SandboxRunner):
    """Reference runner: actually executes a proposed Python file as a
    subprocess (smoke-test level -- does it run without raising, not a
    full test suite) so a change is genuinely exercised, not just
    syntax-checked by the gate.

    NOT isolated. Runs with the same filesystem/network/privileges as the
    calling process. This is intentionally NOT
    control-plane/backend/app/services/sandbox.py's Docker-isolated
    runner reused here -- that's a backend service tied to FastAPI/DB and
    the wrong deployment layer for a customer-side data-plane tool.
    Mirrors that module's design principle instead: never silently treat
    unisolated execution as if it were sandboxed. Production Forge
    deployments must supply a real container-isolated SandboxRunner;
    this reference implementation is for CI/local dev/demos only, and
    says so via its own class name.
    """

    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds

    async def run(self, change: FileChange) -> SandboxResult:
        if not change.file_path.endswith(".py"):
            return SandboxResult(file_path=change.file_path, success=True, output="(non-Python file, execution skipped)")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(change.new_content)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(file_path=change.file_path, success=False, error="execution timed out")

            success = proc.returncode == 0
            return SandboxResult(
                file_path=change.file_path, success=success,
                output=stdout.decode(errors="replace"),
                error=stderr.decode(errors="replace") if not success else None,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ============================================================
# Forge — the orchestrator
# ============================================================

@dataclass
class ForgeResult:
    diff: Diff
    gate_result: GateResult
    sandbox_results: List[SandboxResult]
    passed: bool
    trace_entry: Any  # WorkflowTraceEntry


class Forge:
    """Ties an EngineAdapter, a CompatibilityGate, an optional
    SandboxRunner, and a WorkflowTrace together. Every request_change()
    call produces exactly one signed, hash-chained trace entry recording
    what was proposed, whether it passed the gate, and whether it ran
    successfully in the sandbox -- independent of which engine produced
    the diff."""

    def __init__(
        self, *, gate: Optional[CompatibilityGate] = None,
        sandbox_runner: Optional[SandboxRunner] = None,
        trace: Optional[WorkflowTrace] = None,
    ):
        self.gate = gate or CompatibilityGate()
        self.sandbox_runner = sandbox_runner
        self.trace = trace or WorkflowTrace()

    async def request_change(
        self, instruction: str, context: Dict[str, Any], *,
        engine: EngineAdapter, run_sandbox: bool = True,
        principal: Optional[Dict[str, Any]] = None, environment: str = "dev",
    ) -> ForgeResult:
        with span("vouchstone.forge.request_change", {
            "vouchstone.forge.environment": environment,
        }) as current_span:
            try:
                diff = await engine.propose_change(instruction, context)
                if current_span is not None:
                    current_span.set_attribute("vouchstone.forge.engine", diff.engine_name)

                gate_result = self.gate.evaluate(diff, principal=principal, environment=environment)

                sandbox_results: List[SandboxResult] = []
                if gate_result.allow and run_sandbox and self.sandbox_runner is not None:
                    for change in diff.changes:
                        sandbox_results.append(await self.sandbox_runner.run(change))

                sandbox_passed = all(r.success for r in sandbox_results) if sandbox_results else True
                passed = gate_result.allow and sandbox_passed

                if current_span is not None:
                    current_span.set_attribute("vouchstone.forge.gate_allow", gate_result.allow)
                    current_span.set_attribute("vouchstone.forge.passed", passed)
            except Exception as exc:
                record_exception(current_span, exc)
                raise

        trace_entry = self.trace.append(
            "forge.change_evaluated",
            {
                "instruction": instruction,
                "engine": diff.engine_name,
                "description": diff.description,
                "diff_metadata": diff.metadata,
                "files": [c.file_path for c in diff.changes],
                "gate_allow": gate_result.allow,
                "gate_reason": gate_result.reason,
                "structural_errors": gate_result.structural_errors,
                "sandbox_run": bool(sandbox_results),
                "sandbox_passed": sandbox_passed,
                "passed": passed,
            },
            actor=f"forge:{diff.engine_name}",
        )

        return ForgeResult(
            diff=diff, gate_result=gate_result, sandbox_results=sandbox_results,
            passed=passed, trace_entry=trace_entry,
        )
