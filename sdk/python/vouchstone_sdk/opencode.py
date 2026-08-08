"""Full OpenCode integration — edit Vouchstone agents, customize them, and
copy skills through OpenCode (opencode.ai), with every change governed.

OpenCode defines agents as markdown files with YAML frontmatter
(``description``, ``mode``, ``model``, ``temperature``, ``permission``
allow/ask/deny rules) whose body is the system prompt — per
opencode.ai/docs/agents (project location ``.opencode/agents/<name>.md``).
That maps ~1:1 onto a Vouchstone agent (role, llm model, temperature,
persona prompt, Scope + posture), and this module is the bidirectional
bridge:

- :func:`export_agent` — Vouchstone ``AgentConfig`` (+ :class:`Scope` +
  posture) → an OpenCode agent file. The agent's enforced boundary becomes
  ``permission:`` rules: OpenCode tools outside ``scope.allowed_tools`` are
  written ``deny``; STRICT posture escalates every ``allow`` to ``ask``.
- :func:`import_agent` — parse an (edited) OpenCode agent file back into
  ``AgentConfig`` kwargs, with :func:`diff_agent_markdown` for preview.
- :func:`governed_import` — an import is a first-class governed change: the
  file diff runs through Forge's :class:`CompatibilityGate` against
  :func:`agent_definition_policy_graph` and lands on a hash-chained
  ``WorkflowTrace`` — OpenCode edits are never a side door around
  governance.
- :func:`export_skill` / :func:`copy_skill` — skill runbooks as OpenCode
  skill files, and real skill copying between agents through
  ``ProceduralMemory``.
- :func:`init_workspace` — scaffold a complete ``.opencode/`` workspace:
  every agent exported, skills, ``opencode.json`` pre-wired with
  Vouchstone's MCP server (32 tools: live KG/memory/vault access inside
  OpenCode sessions), and ``.opencode/command/`` entries for common
  Vouchstone workflows.

Frontmatter handling is a deliberate minimal subset (flat keys + one
nested ``permission`` map) implemented with stdlib only — the SDK's core
install must not grow a YAML dependency for this. Files this module writes
always round-trip through :func:`import_agent`; hand-written files using
YAML features beyond the subset raise a clear error rather than being
mis-parsed.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentConfig
from .forge import CompatibilityGate, Diff, FileChange, GateResult
from .graph import Policy, PolicyGraph, WorkflowTrace
from .harness import HarnessPosture, Scope
from .types import Skill

#: OpenCode's built-in tool permission keys (opencode.ai/docs/permissions).
OPENCODE_TOOL_NAMES = (
    "read", "edit", "bash", "glob", "grep", "list", "task",
    "webfetch", "websearch", "external_directory",
)

AGENTS_DIR = "agents"
SKILLS_DIR = "skills"
COMMANDS_DIR = "command"


# ============================================================
# Agent spec + markdown round-trip
# ============================================================

@dataclass
class OpenCodeAgentSpec:
    name: str
    description: str
    prompt: str
    mode: str = "primary"
    model: str | None = None
    temperature: float | None = None
    permission: dict[str, str] = field(default_factory=dict)
    # Vouchstone-namespaced metadata OpenCode ignores but we round-trip:
    # scope boundary + posture, so an exported agent re-imports losslessly.
    vouchstone: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines = ["---"]
        lines.append(f"description: {self.description}")
        lines.append(f"mode: {self.mode}")
        if self.model:
            lines.append(f"model: {self.model}")
        if self.temperature is not None:
            lines.append(f"temperature: {self.temperature}")
        if self.permission:
            lines.append("permission:")
            for key in sorted(self.permission):
                lines.append(f"  {key}: {self.permission[key]}")
        if self.vouchstone:
            lines.append(f"x-vouchstone: {json.dumps(self.vouchstone, sort_keys=True)}")
        lines.append("---")
        lines.append("")
        lines.append(self.prompt.rstrip() + "\n")
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, name: str, text: str) -> OpenCodeAgentSpec:
        if not text.startswith("---"):
            raise ValueError(f"agent file for {name!r} has no frontmatter block")
        try:
            _, front, body = text.split("---", 2)
        except ValueError as exc:
            raise ValueError(f"agent file for {name!r} has an unterminated frontmatter block") from exc

        spec = cls(name=name, description="", prompt=body.strip())
        current_map: dict[str, str] | None = None
        for raw_line in front.splitlines():
            if not raw_line.strip():
                continue
            if raw_line.startswith("  ") and current_map is not None:
                key, _, value = raw_line.strip().partition(":")
                if not _:
                    raise ValueError(f"unparseable permission line in {name!r}: {raw_line!r}")
                current_map[key.strip()] = value.strip()
                continue
            current_map = None
            key, sep, value = raw_line.partition(":")
            if not sep:
                raise ValueError(f"unparseable frontmatter line in {name!r}: {raw_line!r}")
            key, value = key.strip(), value.strip()
            if key == "permission":
                current_map = spec.permission
            elif key == "description":
                spec.description = value
            elif key == "mode":
                spec.mode = value
            elif key == "model":
                spec.model = value
            elif key == "temperature":
                spec.temperature = float(value)
            elif key == "x-vouchstone":
                spec.vouchstone = json.loads(value)
            elif key in ("top_p", "hidden"):
                # Known OpenCode fields we don't map -- preserved semantics
                # are OpenCode's own; nothing on the Vouchstone side reads
                # them, so they're accepted and ignored.
                continue
            else:
                raise ValueError(
                    f"unsupported frontmatter key {key!r} in agent file {name!r} -- "
                    "this bridge parses the documented OpenCode agent subset "
                    "(description/mode/model/temperature/permission) plus x-vouchstone"
                )
        if not spec.description:
            raise ValueError(f"agent file for {name!r} is missing the required description")
        return spec


def _opencode_model(model: str) -> str:
    """Vouchstone model string -> OpenCode's ``provider/model`` form."""
    if "/" in model:
        return model
    if model.startswith("claude-"):
        return f"anthropic/{model}"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return f"openai/{model}"
    return model


def derive_permissions(scope: Scope | None, posture: HarnessPosture) -> dict[str, str]:
    """The agent's enforced boundary, translated into OpenCode
    ``permission:`` rules.

    - ``scope.allowed_tools`` restricts which OpenCode tools stay enabled:
      any OpenCode tool NOT in the list is ``deny`` (read stays allowed —
      an agent that cannot read its own workspace is unusable in an
      editor). No ``allowed_tools`` means no tool restriction.
    - STRICT posture escalates every non-denied mutating tool to ``ask``
      (bash/edit/task/webfetch/websearch), mirroring the harness's
      approval-required behavior.
    """
    perms: dict[str, str] = {}
    if scope is not None and scope.allowed_tools is not None:
        allowed = set(scope.allowed_tools) | {"read", "glob", "grep", "list"}
        for tool in OPENCODE_TOOL_NAMES:
            perms[tool] = "allow" if tool in allowed else "deny"
    if posture is HarnessPosture.STRICT:
        for tool in ("bash", "edit", "task", "webfetch", "websearch", "external_directory"):
            if perms.get(tool) != "deny":
                perms[tool] = "ask"
    return perms


def agent_to_spec(
    config: AgentConfig,
    *,
    scope: Scope | None = None,
    posture: HarnessPosture = HarnessPosture.AUTO,
    role: str | None = None,
    mode: str = "primary",
) -> OpenCodeAgentSpec:
    vouchstone_meta: dict[str, Any] = {"posture": posture.value}
    if scope is not None:
        vouchstone_meta["scope"] = {
            "domains": scope.domains, "entity_types": scope.entity_types,
            "tags": scope.tags, "allowed_tools": scope.allowed_tools,
            "namespace": scope.namespace,
        }
    prompt = config.system_prompt or f"You are {config.name}, a Vouchstone agent."
    if scope is not None and (scope.domains or scope.entity_types):
        prompt += (
            "\n\n## Knowledge-graph boundary\n"
            f"You are scoped to domains={scope.domains or 'all'}, "
            f"entity kinds={scope.entity_types or 'all'}. Requests outside "
            "this boundary must be declined explicitly, never guessed at."
        )
    return OpenCodeAgentSpec(
        name=config.name,
        description=role or f"{config.name} — governed Vouchstone agent",
        prompt=prompt,
        mode=mode,
        model=_opencode_model(config.model),
        temperature=config.temperature,
        permission=derive_permissions(scope, posture),
        vouchstone=vouchstone_meta,
    )


def export_agent(
    config: AgentConfig,
    workspace: str | Path,
    *,
    scope: Scope | None = None,
    posture: HarnessPosture = HarnessPosture.AUTO,
    role: str | None = None,
) -> Path:
    """Write ``<workspace>/.opencode/agents/<name>.md``. Returns the path."""
    spec = agent_to_spec(config, scope=scope, posture=posture, role=role)
    target = Path(workspace) / ".opencode" / AGENTS_DIR / f"{config.name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec.to_markdown())
    return target


def import_agent(path: str | Path) -> tuple[dict[str, Any], OpenCodeAgentSpec]:
    """Parse an OpenCode agent file back into ``AgentConfig`` kwargs (plus
    the raw spec). The x-vouchstone metadata restores scope + posture."""
    p = Path(path)
    spec = OpenCodeAgentSpec.from_markdown(p.stem, p.read_text())
    model = spec.model or "claude-sonnet-4-6"
    if model.startswith("anthropic/"):
        model = model.split("/", 1)[1]
    kwargs: dict[str, Any] = {
        "name": spec.name,
        "system_prompt": spec.prompt,
        "model": model,
    }
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature
    scope_meta = spec.vouchstone.get("scope")
    if scope_meta:
        scope = Scope(**scope_meta)
        kwargs.update(scope.memory_kwargs())
    return kwargs, spec


def diff_agent_markdown(old_markdown: str, new_markdown: str, name: str) -> str:
    """Unified-diff preview of an agent-definition change."""
    return "".join(difflib.unified_diff(
        old_markdown.splitlines(keepends=True),
        new_markdown.splitlines(keepends=True),
        fromfile=f"agents/{name}.md (current)",
        tofile=f"agents/{name}.md (imported)",
    ))


# ============================================================
# D5 — governance: imports are first-class governed changes
# ============================================================

def agent_definition_policy_graph() -> PolicyGraph:
    """Default policy for agent-definition edits: permitted, but every
    change carries audit + dual-signoff obligations — an agent's persona
    and permissions ARE its security boundary, so edits are the most
    consequential change class the bridge handles. Extends the same
    pattern as forge.opencode_dual_signoff_policy_graph."""
    graph = PolicyGraph()
    graph.add_policy(Policy(
        name="agent-definition-edits-governed",
        effect="permit",
        action={"eq": "forge.apply_change"},
        conditions=[{
            "path": "resource.file_path", "op": "regex",
            "value": r"(^|/)\.opencode/agents/.+\.md$",
        }],
        obligations=["log_to_audit", "require_dual_signoff"],
    ))
    return graph


def governed_import(
    path: str | Path,
    *,
    previous_markdown: str = "",
    trace: WorkflowTrace | None = None,
    gate: CompatibilityGate | None = None,
) -> tuple[dict[str, Any] | None, GateResult, WorkflowTrace]:
    """Import an edited agent file through the Forge gate + signed trace.

    Returns ``(config_kwargs | None, gate_result, trace)`` —
    ``config_kwargs`` is None when the gate denies. The change is traced
    either way (denials are evidence too).
    """
    p = Path(path)
    new_markdown = p.read_text()
    trace = trace or WorkflowTrace()
    gate = gate or CompatibilityGate(policy_graph=agent_definition_policy_graph())

    rel_path = f".opencode/{AGENTS_DIR}/{p.name}"
    diff = Diff(
        description=f"OpenCode edit of agent definition {p.stem!r}",
        changes=[FileChange(
            file_path=rel_path,
            original_content=previous_markdown,
            new_content=new_markdown,
        )],
        engine_name="opencode",
    )
    result = gate.evaluate(diff)
    entry_payload: dict[str, Any] = {
        "agent_file": rel_path,
        "gate_allow": result.allow,
        "gate_reason": result.reason,
        "obligations": result.policy_decision.obligations if result.policy_decision else [],
        "diff_preview": diff_agent_markdown(previous_markdown, new_markdown, p.stem)[:2000],
    }

    if not result.allow:
        trace.append("opencode.agent_import_denied", entry_payload, actor="opencode")
        return None, result, trace

    try:
        kwargs, _spec = import_agent(p)
    except ValueError as exc:
        entry_payload["parse_error"] = str(exc)
        trace.append("opencode.agent_import_invalid", entry_payload, actor="opencode")
        return None, result, trace

    trace.append("opencode.agent_imported", entry_payload, actor="opencode")
    return kwargs, result, trace


# ============================================================
# D3 — skills interop
# ============================================================

def skill_to_markdown(skill: Skill) -> str:
    lines = [
        "---",
        f"name: {skill.name}",
        f"description: {skill.description}",
        f"x-vouchstone: {json.dumps({'version': skill.version, 'tags': skill.tags, 'tools_required': skill.tools_required, 'success_rate': skill.success_rate}, sort_keys=True)}",
        "---",
        "",
        f"# {skill.name}",
        "",
        skill.description,
        "",
        "## Steps",
        "",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(skill.steps, 1)]
    if skill.prerequisites:
        lines += ["", "## Prerequisites", ""]
        lines += [f"- {p}" for p in skill.prerequisites]
    return "\n".join(lines) + "\n"


def export_skill(skill: Skill, workspace: str | Path) -> Path:
    """Write ``<workspace>/.opencode/skills/<name>/SKILL.md``."""
    target = Path(workspace) / ".opencode" / SKILLS_DIR / skill.name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(skill_to_markdown(skill))
    return target


async def copy_skill(
    procedural: Any, skill_name: str, *, from_agent: str, to_agent: str,
) -> Skill:
    """Copy a skill between agents through ``ProceduralMemory`` — the real
    registry (graph-backed or control-plane-backed), not a file copy. The
    copy starts with a fresh track record (execution stats describe the
    SOURCE agent's history, not the recipient's)."""
    matches = await procedural.find_skill(from_agent, skill_name)
    exact = [s for s in matches if s.name == skill_name]
    if not exact:
        raise KeyError(
            f"agent {from_agent!r} has no skill named {skill_name!r} "
            f"(found: {[s.name for s in matches]})"
        )
    source = exact[0]
    clone = Skill(
        id=f"{source.id}-copy-{to_agent}",
        name=source.name,
        description=source.description,
        steps=list(source.steps),
        tools_required=list(source.tools_required),
        prerequisites=list(source.prerequisites),
        version=source.version,
        tags=list(source.tags),
    )
    await procedural.register_skill(to_agent, clone)
    return clone


# ============================================================
# D4 + D6 — workspace scaffold with MCP + commands
# ============================================================

_COMMANDS: dict[str, str] = {
    "verify-kg": (
        "---\ndescription: Verify the Vouchstone KG artifact's signature\n---\n"
        "Run `vouchstone kg verify vouchstone-kg.json` and report whether the "
        "signature is valid. If it is invalid, diff against the last known-good "
        "artifact and summarize what changed.\n"
    ),
    "run-evals": (
        "---\ndescription: Run the Vouchstone eval suite for this workspace's agents\n---\n"
        "Run the project's eval suite (pytest tests/ or the eval harness via "
        "vouchstone_sdk.run_eval_suite) and summarize pass/fail per case with "
        "regressions called out first.\n"
    ),
    "forge-change": (
        "---\ndescription: Propose a governed change through Vouchstone Forge\n---\n"
        "Take the requested change and run it through Forge (engine -> "
        "compatibility gate -> sandbox -> signed trace) using "
        "examples/02_forge_governed_change.py as the template. Report the gate "
        "decision, obligations, and the trace tip hash.\n"
    ),
}


def init_workspace(
    workspace: str | Path,
    *,
    agents: list[tuple[AgentConfig, Scope | None]] | None = None,
    skills: list[Skill] | None = None,
    posture: HarnessPosture = HarnessPosture.AUTO,
    mcp_command: list[str] | None = None,
    mcp_environment: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Scaffold a complete ``.opencode/`` workspace.

    Writes every agent (exported with its scope/posture), every skill, the
    Vouchstone command entries, and ``opencode.json`` wiring Vouchstone's
    MCP server (`mcp-server/`, 32 tools) so OpenCode sessions can query the
    live KG / 5-layer memory / Vault while editing agents. ``mcp_command``
    defaults to the published server started via node; pass your deployment's
    actual command. Returns a manifest of everything written.
    """
    ws = Path(workspace)
    written: dict[str, list[str]] = {"agents": [], "skills": [], "commands": [], "config": []}

    for config, scope in agents or []:
        path = export_agent(config, ws, scope=scope, posture=posture)
        written["agents"].append(str(path))

    for skill in skills or []:
        path = export_skill(skill, ws)
        written["skills"].append(str(path))

    commands_dir = ws / ".opencode" / COMMANDS_DIR
    commands_dir.mkdir(parents=True, exist_ok=True)
    for name, content in _COMMANDS.items():
        cmd_path = commands_dir / f"{name}.md"
        cmd_path.write_text(content)
        written["commands"].append(str(cmd_path))

    config_path = ws / "opencode.json"
    existing: dict[str, Any] = {}
    if config_path.exists():
        existing = json.loads(config_path.read_text())
    existing.setdefault("$schema", "https://opencode.ai/config.json")
    mcp = existing.setdefault("mcp", {})
    mcp["vouchstone"] = {
        "type": "local",
        "command": mcp_command or ["npx", "-y", "@vouchstone/mcp-server"],
        "environment": mcp_environment or {
            "VOUCHSTONE_API_URL": "${VOUCHSTONE_API_URL}",
            "VOUCHSTONE_API_KEY": "${VOUCHSTONE_API_KEY}",
            "VOUCHSTONE_TENANT_ID": "${VOUCHSTONE_TENANT_ID}",
        },
    }
    config_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    written["config"].append(str(config_path))
    return written
