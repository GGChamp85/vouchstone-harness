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
pipeline applies unconditionally. Two production-real adapters ship here
(ClaudeEngineAdapter, using the `anthropic` package directly; and
OpenCodeEngineAdapter, invoking the `opencode` CLI as a subprocess -- the
default engine as of Phase 5, see get_default_engine_adapter()) plus one
deliberately-trivial reference adapter for testing/demos
(EchoEngineAdapter) — implementing real ADK/LangChain/CrewAI adapters
without those being verified, intentionally-installed dependencies would
be exactly the kind of unverified, unrunnable code this project's
"no stubs" standard forbids. See EngineAdapter's docstring for how a real
integration plugs in.

OpenCodeEngineAdapter was written in an environment where the `opencode`
binary is not installed, so its exact CLI flags/output format could not
be verified end-to-end against the real thing -- see its class docstring
for the precise, narrow assumption this makes and how it fails loudly
(EngineExecutionError) rather than guessing if that assumption is wrong.
An absent `opencode` binary itself raises EngineUnavailableError, never a
silent fallback to a different engine or a fabricated Diff.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    changes: list[FileChange]
    engine_name: str
    # Provenance for reproducibility (C7c). A templated diff sets this to
    # {"template_id": ..., "params": {...}} -- enough to deterministically
    # re-render the exact same output later and confirm a past decision
    # replays identically. Free-form ("improvised") diffs leave this empty,
    # which is itself a signal: nothing to replay against, so the
    # compatibility gate / a human reviewer should weigh it more heavily.
    metadata: dict[str, Any] = field(default_factory=dict)


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
    async def propose_change(self, instruction: str, context: dict[str, Any]) -> Diff:
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
    `anthropic` package, installed via the `llm-anthropic` extra). Not a
    reimplementation of the Claude Agent SDK's tool-use loop -- this is
    Forge's own thin wrapper for the narrow "propose a file-level diff"
    task, which is all the compatibility-gate pipeline needs from an
    engine."""

    engine_name = "claude-direct"

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key
        self.model = model

    async def propose_change(self, instruction: str, context: dict[str, Any]) -> Diff:
        try:
            import anthropic
        except ImportError as exc:
            raise EngineUnavailableError(
                "ClaudeEngineAdapter requires the Anthropic client. "
                "Install it with: pip install 'vouchstone-sdk[llm-anthropic]'"
            ) from exc

        current_files: dict[str, str] = context.get("files", {})
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
        text = "".join(getattr(block, "text", "") for block in response.content)
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

    def __init__(self, transform: Callable[[str, dict[str, str]], Diff]):
        self._transform = transform

    async def propose_change(self, instruction: str, context: dict[str, Any]) -> Diff:
        current_files: dict[str, str] = context.get("files", {})
        return self._transform(instruction, current_files)


# ============================================================
# OpenCodeEngineAdapter — subprocess-invoked CLI engine (Phase 5)
# ============================================================

class EngineUnavailableError(RuntimeError):
    """Raised when an engine's underlying dependency (a CLI binary, an
    SDK, a service) is not present/reachable at all. This must never be
    caught and silently papered over with a fallback engine or a
    fabricated Diff -- the entire point of a named exception here is to
    make a missing dependency loud and unambiguous to the caller, exactly
    like the rest of this codebase's "no stubs / no fabricated success"
    standard (see harness_cli.py's PipAuditNotAvailableError for the same
    pattern applied to pip-audit)."""


class EngineExecutionError(RuntimeError):
    """Raised when an engine's underlying dependency IS present but a
    specific invocation failed -- non-zero exit code, a timeout, or
    output that could not be interpreted under the invocation contract
    this adapter assumes. Deliberately a different exception than
    EngineUnavailableError: "the tool isn't installed" and "the tool ran
    and failed" have different remediations, so a caller catching one
    should not assume it also covers the other."""


class OpenCodeEngineAdapter(EngineAdapter):
    """Wraps the OpenCode CLI (https://opencode.ai) as a Forge engine by
    shelling out to it, the way ``ClaudeEngineAdapter`` shells out to the
    Anthropic API but at process granularity instead of HTTP.

    VERIFIED CLI INVOCATION CONTRACT (against opencode 1.18.15, real
    binary, real edits applied to a real working directory):

        opencode run "<instruction>" --dir <working_dir>

    exits 0 on success having mutated files in place under
    ``<working_dir>`` (non-interactive is the CLI's default -- there is no
    ``--non-interactive`` flag; ``-i``/``--interactive`` opts *into*
    interactive mode instead), and exits non-zero with a usage/error
    message on stderr otherwise (e.g. an unrecognized flag, which is
    exactly how the previous ``--cwd``/``--non-interactive`` guess was
    caught). ``opencode --version`` prints a version string to stdout.

    Deliberately, this adapter does NOT try to parse OpenCode's stdout
    into a structured diff -- that output format is undocumented from
    here, and parsing it would be exactly the kind of guessed, unverified
    behavior this project's "no fabricated success" standard forbids.
    Instead OpenCode is treated as an opaque black box that mutates a
    directory: files are snapshotted before invocation, OpenCode is run,
    and the working directory is re-read afterward and diffed against the
    snapshot. The only real assumptions baked in are (a) the invocation
    flags above and (b) that a non-zero exit code means failure -- both
    narrow, and both fail loudly via EngineExecutionError (not a silent
    guess) if the real installed binary disagrees. If ``opencode run``'s
    actual flags differ, expect a non-zero exit / usage error on first
    real invocation -- fix ``_build_run_command`` below, not this adapter's
    diffing logic, which is independent of OpenCode's exact CLI surface.

    Does not currently detect file *deletions* (a file OpenCode removes
    from the working directory) -- only new/changed file content becomes
    a FileChange. Documented gap, not a silent one.
    """

    engine_name = "opencode"

    def __init__(
        self,
        binary_path: str | None = None,
        timeout_seconds: float = 120.0,
        version_timeout_seconds: float = 5.0,
    ):
        self.binary_path = binary_path
        self.timeout_seconds = timeout_seconds
        self.version_timeout_seconds = version_timeout_seconds

    def _resolve_binary(self) -> str:
        """Never falls back to a different engine or a fake success --
        an absent/unusable binary always raises EngineUnavailableError."""
        configured = self.binary_path or os.environ.get("VOUCHSTONE_OPENCODE_PATH")
        if configured:
            if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
                raise EngineUnavailableError(
                    f"configured OpenCode binary '{configured}' does not exist or is "
                    "not executable (check binary_path= / VOUCHSTONE_OPENCODE_PATH)."
                )
            return configured

        found = shutil.which("opencode")
        if not found:
            raise EngineUnavailableError(
                "OpenCode CLI not found on PATH -- install it and ensure `opencode` "
                "is runnable, or set VOUCHSTONE_OPENCODE_PATH to its binary path, or "
                "pass binary_path= to OpenCodeEngineAdapter explicitly."
            )
        return found

    def _build_run_command(self, binary: str, instruction: str, workdir: Path) -> list[str]:
        # VERIFIED against the real opencode 1.18.15 binary -- see class
        # docstring. `--cwd`/`--non-interactive` don't exist on the real
        # CLI (usage-error, exit 1); `--dir` is correct and non-interactive
        # is already the default.
        return [binary, "run", instruction, "--dir", str(workdir)]

    async def _get_version(self, binary: str) -> str | None:
        """Best-effort version probe for Diff.metadata -- never load-bearing.
        A failed/unparseable version probe must not block a real
        propose_change() call, so failures here are swallowed to None
        rather than raised."""
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.version_timeout_seconds)
            if proc.returncode != 0:
                return None
            return stdout.decode(errors="replace").strip() or None
        except Exception:
            return None

    @staticmethod
    def _materialize_files(workdir: Path, current_files: dict[str, str]) -> None:
        """Write the context's file set into the temp workdir (sync; called
        via asyncio.to_thread)."""
        for rel_path, content in current_files.items():
            target = workdir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    @staticmethod
    def _diff_workdir(workdir: Path, current_files: dict[str, str]) -> list[FileChange]:
        """Re-read the workdir after the engine ran and diff it against the
        original file set (sync; called via asyncio.to_thread).

        A file present in the input set but absent from the tree afterwards
        is a DELETION, represented as a FileChange with new_content="" --
        previously deletions were silently invisible in the diff."""
        changes: list[FileChange] = []
        seen: set[str] = set()
        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(workdir).as_posix()
            seen.add(rel)
            new_content = path.read_text(errors="replace")
            original_content = current_files.get(rel, "")
            if new_content != original_content:
                changes.append(FileChange(
                    file_path=rel, original_content=original_content, new_content=new_content,
                ))
        for rel in sorted(set(current_files) - seen):
            changes.append(FileChange(
                file_path=rel, original_content=current_files[rel], new_content="",
            ))
        return changes

    async def propose_change(self, instruction: str, context: dict[str, Any]) -> Diff:
        binary = self._resolve_binary()
        current_files: dict[str, str] = context.get("files", {})
        engine_version = await self._get_version(binary)

        with tempfile.TemporaryDirectory(prefix="vouchstone-forge-opencode-") as workdir_str:
            workdir = Path(workdir_str)
            # Blocking filesystem work (materialize the input tree, and later
            # re-read it) runs off the event loop: an engine call can carry an
            # arbitrarily large file set, and this adapter runs inside async
            # request handlers.
            await asyncio.to_thread(self._materialize_files, workdir, current_files)

            cmd = self._build_run_command(binary, instruction, workdir)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                # Binary resolved above but disappeared/became unexecutable
                # between resolution and exec -- still an availability
                # problem, not an execution one.
                raise EngineUnavailableError(f"OpenCode binary '{binary}' could not be executed: {exc}") from exc

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise EngineExecutionError(
                    f"opencode timed out after {self.timeout_seconds}s on instruction {instruction!r}"
                ) from exc

            exit_code = proc.returncode
            if exit_code != 0:
                raise EngineExecutionError(
                    f"opencode exited with code {exit_code} for instruction {instruction!r}: "
                    f"{stderr.decode(errors='replace').strip()}"
                )

            changes: list[FileChange] = await asyncio.to_thread(
                self._diff_workdir, workdir, current_files
            )

        # Empty `changes` here is honest, not an error: OpenCode ran
        # successfully (exit 0) and simply didn't change anything.
        return Diff(
            description=instruction,
            changes=changes,
            engine_name=self.engine_name,
            metadata={"engine_version": engine_version, "exit_code": exit_code},
        )


def get_default_engine_adapter(**kwargs: Any) -> EngineAdapter:
    """Resolve and construct the Forge engine adapter a caller should use
    when engine selection is driven by name/config rather than the caller
    directly instantiating a specific adapter class (e.g. `ClaudeEngineAdapter()`
    calls are unaffected by this and remain fully first-class).

    Reads VOUCHSTONE_FORGE_ENGINE (values are ENGINE_ADAPTERS registry
    names -- "opencode", "claude", "echo", or any third-party-registered
    name), defaulting to "opencode" per Phase 5 ("OpenCode as default
    Forge engine"). There is no bundle-manifest or harness-config field
    that currently selects a Forge engine by name -- this factory is the
    intended call site for whichever future mechanism (bundle manifest
    field, CLI flag, control-plane setting) adds one; until then, callers
    that want the configured default call this directly.

    kwargs are forwarded to the resolved adapter class's constructor
    (e.g. binary_path=/timeout_seconds= for opencode, api_key=/model= for
    claude). Note this only *constructs* the adapter -- for
    OpenCodeEngineAdapter specifically, an absent binary is not detected
    until propose_change() is actually called (see
    OpenCodeEngineAdapter._resolve_binary); callers wanting to fail fast
    should call harness_cli's `status` engine-presence check or invoke
    propose_change immediately rather than assuming construction alone
    proves availability. This default never silently substitutes a
    different engine if the configured one is unavailable -- that failure
    surfaces from propose_change() as EngineUnavailableError, same as any
    other call site.
    """
    from .plugins import (
        ENGINE_ADAPTERS,  # local import: plugins imports from forge at module load time
    )

    name = os.environ.get("VOUCHSTONE_FORGE_ENGINE", "opencode")
    adapter_cls = ENGINE_ADAPTERS.get(name)
    return adapter_cls(**kwargs)


def describe_forge_engine() -> dict[str, Any]:
    """Synchronous, side-effect-light status descriptor for whichever
    engine VOUCHSTONE_FORGE_ENGINE resolves to. Used by
    `vouchstone harness status` (see runtime/src/harness_cli.py) to
    surface the Forge engine prerequisite the same way bundle/dependency
    status is surfaced -- so "opencode isn't installed" is visible from
    `status` output rather than only failing later, mid-request, inside
    propose_change()."""
    name = os.environ.get("VOUCHSTONE_FORGE_ENGINE", "opencode")
    info: dict[str, Any] = {"configured_engine": name}
    if name == "opencode":
        binary = os.environ.get("VOUCHSTONE_OPENCODE_PATH") or shutil.which("opencode")
        info["opencode_binary_present"] = binary is not None
        info["opencode_binary_path"] = binary
        version: str | None = None
        if binary:
            try:
                result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
                version = result.stdout.strip() if result.returncode == 0 else None
            except Exception:
                version = None
        info["opencode_version"] = version
    return info


# ============================================================
# Compatibility gate
# ============================================================

@dataclass
class GateResult:
    allow: bool
    reason: str
    structural_errors: list[str] = field(default_factory=list)
    policy_decision: PolicyDecision | None = None


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

    def __init__(self, policy_graph: PolicyGraph | None = None):
        self.policy_graph = policy_graph or PolicyGraph()

    def check_structural_validity(self, change: FileChange) -> str | None:
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
        self, diff: Diff, *, principal: dict[str, Any] | None = None, environment: str = "dev",
    ) -> GateResult:
        structural_errors = [
            f"{change.file_path}: {err}"
            for change in diff.changes
            if (err := self.check_structural_validity(change)) is not None
        ]
        if structural_errors:
            return GateResult(allow=False, reason="structural validation failed", structural_errors=structural_errors)

        principal = principal or {"engine": diff.engine_name}
        obligations: list[str] = []
        matched_policy_names: list[str] = []
        for change in diff.changes:
            decision = self.policy_graph.evaluate(
                principal=principal, action="forge.apply_change",
                resource={"file_path": change.file_path},
                context={"environment": environment},
            )
            if not decision.allow:
                return GateResult(allow=False, reason=decision.reason, policy_decision=decision)
            for o in decision.obligations:
                if o not in obligations:
                    obligations.append(o)
            for name in decision.matched_policy_names:
                if name not in matched_policy_names:
                    matched_policy_names.append(name)

        # Union of obligations/matched policies across every changed file's
        # decision -- surfaced on the allow path too (previously dropped
        # entirely: GateResult.policy_decision was only ever populated on
        # denial, silently discarding obligations a permit policy attached,
        # e.g. require_dual_signoff). A caller/executor consuming
        # GateResult on an allowed change needs these obligations to know
        # what it still must do before treating the change as fully clear.
        aggregate_decision = PolicyDecision(
            allow=True, obligations=obligations, matched_policy_names=matched_policy_names, reason="allow",
        ) if diff.changes else None
        return GateResult(allow=True, reason="allow", policy_decision=aggregate_decision)


def opencode_dual_signoff_policy_graph() -> PolicyGraph:
    """Reference/example PolicyGraph demonstrating a gate policy that
    discriminates on which Forge engine produced a diff -- mirrors the
    shape of the control plane's shipped ABAC policies (see
    control-plane/backend/scripts/seed_abac_policies.py's SHIPPED_POLICIES:
    named permit/forbid specs with conditions/obligations) but expressed
    against this SDK's own PolicyGraph/Policy, which is what
    CompatibilityGate actually evaluates against.

    CompatibilityGate.evaluate() defaults ``principal`` to
    ``{"engine": diff.engine_name}`` whenever no explicit principal is
    passed in, so ``principal.engine`` is always available to condition
    on without any extra wiring from the caller.

    Rule encoded here: an OpenCode-authored change to a ``*.py`` file
    under ``/prod/`` carries an extra ``require_dual_signoff`` obligation
    that a Claude-direct-authored change to the exact same file does not.
    Rationale: OpenCode is a newer, unattended, non-Anthropic-hosted
    engine with no equivalent production track record yet, so changes it
    proposes to production Python get an extra human-signoff obligation
    until that changes. Both engines are still *allowed* to make the
    change (this is not a deny) -- the divergence is in the obligations
    the gate attaches, same as the control plane's ABAC obligations being
    surfaced for the action-gateway/executor layer to actually honor
    rather than enforced inside the policy engine itself.
    """
    graph = PolicyGraph()
    graph.add_policy(Policy(
        name="permit all forge changes",
        effect="permit",
        priority=100,
        action={"eq": "forge.apply_change"},
    ))
    graph.add_policy(Policy(
        name="opencode changes to prod python require dual sign-off",
        effect="permit",
        priority=10,
        action={"eq": "forge.apply_change"},
        conditions=[
            {"path": "principal.engine", "op": "eq", "value": "opencode"},
            {"path": "resource.file_path", "op": "regex", "value": r"^/prod/.*\.py$"},
        ],
        obligations=["require_dual_signoff", "log_to_audit"],
    ))
    return graph


# ============================================================
# Sandbox runner — pluggable, one honest reference implementation
# ============================================================

@dataclass
class SandboxResult:
    file_path: str
    success: bool
    output: str = ""
    error: str | None = None


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
            await asyncio.to_thread(Path(tmp_path).unlink, missing_ok=True)


# ============================================================
# Forge — the orchestrator
# ============================================================

@dataclass
class ForgeResult:
    diff: Diff
    gate_result: GateResult
    sandbox_results: list[SandboxResult]
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
        self, *, gate: CompatibilityGate | None = None,
        sandbox_runner: SandboxRunner | None = None,
        trace: WorkflowTrace | None = None,
    ):
        self.gate = gate or CompatibilityGate()
        self.sandbox_runner = sandbox_runner
        self.trace = trace or WorkflowTrace()

    async def request_change(
        self, instruction: str, context: dict[str, Any], *,
        engine: EngineAdapter, run_sandbox: bool = True,
        principal: dict[str, Any] | None = None, environment: str = "dev",
    ) -> ForgeResult:
        with span("vouchstone.forge.request_change", {
            "vouchstone.forge.environment": environment,
        }) as current_span:
            try:
                diff = await engine.propose_change(instruction, context)
                if current_span is not None:
                    current_span.set_attribute("vouchstone.forge.engine", diff.engine_name)

                gate_result = self.gate.evaluate(diff, principal=principal, environment=environment)

                sandbox_results: list[SandboxResult] = []
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
