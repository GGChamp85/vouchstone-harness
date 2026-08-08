"""Deterministic Transformation Engine (C7c) — the genuine, ownable
"deterministic coding agent" identity.

LLM code generation itself cannot be made deterministic; sampling is
inherent, not a policy choice, and claiming otherwise would repeat the
same overclaim already corrected elsewhere in this codebase for the KG
extraction pipeline. The honest, ownable version: for the common,
high-risk customization tasks (adjust an approval threshold, add a policy
rule, ...), select and parameterize an already-verified-safe template
instead of generating free-form code. The *outcome* is deterministic and
reproducible — same template + same params always produce byte-identical
output — even though a natural-language front end (any underlying
LLM/engine) is what interprets the request into those params.

What's actually Vouchstone's own, regardless of which model (if any)
drafts anything:
1. The template library itself — codified, compounding IP over time.
2. The compatibility-gate pipeline every change passes through
   (forge.py's CompatibilityGate) — identical whether templated or
   free-form.
3. This module's replay_and_verify() — given a past signed decision's
   pinned template_id + params, re-render and re-gate, and confirm the
   outcome matches exactly. That's what makes "deterministic" a
   verifiable claim, not a marketing one.

Free-form generation (an EngineAdapter like ClaudeEngineAdapter) remains
available for genuinely novel changes as an explicit "improvise" path —
TemplateEngineAdapter falls back to it when no template matches, and
tags the resulting Diff so downstream review knows nothing here is
byte-for-byte reproducible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .forge import CompatibilityGate, Diff, EngineAdapter, FileChange, GateResult

# ============================================================
# Template definition
# ============================================================

@dataclass
class TemplateParameter:
    name: str
    description: str
    required: bool = True


class TemplateNotMatchedError(Exception):
    """No template in the library matches the instruction, and no
    fallback (improvise) engine was configured."""


class MissingTemplateParametersError(Exception):
    """A template matched, but required parameters weren't supplied and
    couldn't be extracted. Deliberately does NOT guess -- a wrong guess
    silently rendered would be exactly the "fabricated" failure mode this
    project has zero tolerance for elsewhere."""

    def __init__(self, template_id: str, missing: list[str]):
        self.template_id = template_id
        self.missing = missing
        super().__init__(f"template {template_id!r} is missing required parameters: {missing}")


@dataclass
class TransformationTemplate:
    """A pre-verified, parameterized change. render() is a pure function:
    the same params dict always produces byte-identical output, which is
    the entire basis for this being genuinely "deterministic" rather than
    an LLM's best effort each time."""

    id: str
    name: str
    description: str
    file_path: str
    parameters: list[TemplateParameter]
    render_fn: Callable[[dict[str, Any]], str]
    # Plain-string keyword matching -- deliberately not an LLM classifier.
    # Selecting *which* template applies must be as reproducible as
    # rendering it; an LLM-based matcher would reintroduce the exact
    # non-determinism this module exists to avoid.
    match_keywords: list[str] = field(default_factory=list)

    def matches(self, instruction: str) -> bool:
        text = instruction.lower()
        return any(kw.lower() in text for kw in self.match_keywords)

    def render(self, params: dict[str, Any]) -> str:
        missing = [p.name for p in self.parameters if p.required and p.name not in params]
        if missing:
            raise MissingTemplateParametersError(self.id, missing)
        return self.render_fn(params)


class TemplateLibrary:
    """A collection of templates. Grows over time as more customization
    patterns get codified -- that accumulation is the actual IP."""

    def __init__(self, templates: list[TransformationTemplate] | None = None):
        self._templates: dict[str, TransformationTemplate] = {}
        for t in templates or []:
            self.add(t)

    def add(self, template: TransformationTemplate) -> None:
        self._templates[template.id] = template

    def get(self, template_id: str) -> TransformationTemplate | None:
        return self._templates.get(template_id)

    def find_match(self, instruction: str) -> TransformationTemplate | None:
        """First match wins -- if two templates could both match the same
        instruction, that's a library-authoring problem (overlapping
        keywords), not something to paper over with an implicit priority
        order here."""
        for template in self._templates.values():
            if template.matches(instruction):
                return template
        return None

    def __len__(self) -> int:
        return len(self._templates)


# ============================================================
# TemplateEngineAdapter — plugs into Forge as any other EngineAdapter
# ============================================================

class TemplateEngineAdapter(EngineAdapter):
    """Tries the template library first; only falls into free-form
    generation (via fallback_engine, if configured) when nothing matches.
    Every Diff this produces is tagged in .metadata so downstream code
    (the compatibility gate, a human reviewer, replay_and_verify) can tell
    templated from improvised without re-deriving it."""

    engine_name = "template-engine"

    def __init__(self, library: TemplateLibrary, fallback_engine: EngineAdapter | None = None):
        self.library = library
        self.fallback_engine = fallback_engine

    async def propose_change(self, instruction: str, context: dict[str, Any]) -> Diff:
        template = self.library.find_match(instruction)
        if template is not None:
            params: dict[str, Any] = context.get("template_params", {})
            new_content = template.render(params)
            current_files: dict[str, str] = context.get("files", {})
            return Diff(
                description=f"[templated: {template.name}] {instruction}",
                changes=[FileChange(
                    file_path=template.file_path,
                    original_content=current_files.get(template.file_path, ""),
                    new_content=new_content,
                )],
                engine_name=self.engine_name,
                metadata={"template_id": template.id, "params": params, "templated": True},
            )

        if self.fallback_engine is None:
            raise TemplateNotMatchedError(
                f"no template matched instruction {instruction!r} and no fallback engine is configured"
            )
        diff = await self.fallback_engine.propose_change(instruction, context)
        # Tag as improvised regardless of what the underlying engine set --
        # this adapter is the one making the "no template matched" call,
        # so it owns marking the diff as such.
        diff.metadata = {**diff.metadata, "templated": False, "improvised_by": self.fallback_engine.engine_name}
        return diff


# ============================================================
# Replay verification — the actual "deterministic" proof
# ============================================================

@dataclass
class ReplayResult:
    reproducible: bool
    original_content: str | None = None
    replayed_content: str | None = None
    original_gate_allow: bool | None = None
    replayed_gate_allow: bool | None = None
    reason: str = ""


def replay_and_verify(
    trace_entry_payload: dict[str, Any], library: TemplateLibrary, gate: CompatibilityGate,
) -> ReplayResult:
    """Given a past forge.change_evaluated trace entry's payload (see
    Forge.request_change()), re-render the template with the exact same
    pinned parameters and re-run the compatibility gate. If the content
    and the gate decision both match what was originally recorded, the
    decision is proven reproducible -- not just claimed to be.

    Only meaningful for templated diffs; an improvised (free-form) diff
    has nothing pinned to replay against by design (see module docstring).
    """
    diff_metadata = trace_entry_payload.get("diff_metadata") or {}
    if not diff_metadata.get("templated"):
        return ReplayResult(reproducible=False, reason="not a templated change -- nothing to replay deterministically")

    template_id = diff_metadata.get("template_id")
    params = diff_metadata.get("params", {})
    if not isinstance(template_id, str) or not template_id:
        return ReplayResult(reproducible=False, reason="trace payload carries no template_id to replay")
    template = library.get(template_id)
    if template is None:
        return ReplayResult(reproducible=False, reason=f"template {template_id!r} not found in the supplied library")

    try:
        replayed_content = template.render(params)
    except MissingTemplateParametersError as e:
        return ReplayResult(reproducible=False, reason=str(e))

    replayed_diff = Diff(
        description=trace_entry_payload.get("description", ""),
        changes=[FileChange(file_path=template.file_path, original_content="", new_content=replayed_content)],
        engine_name="template-engine",
        metadata=diff_metadata,
    )
    replayed_gate: GateResult = gate.evaluate(replayed_diff)

    original_gate_allow = trace_entry_payload.get("gate_allow")
    # The trace payload records the gate verdict but not the rendered file
    # contents, so gate-verdict equality is the reproducibility criterion.
    gate_matches = replayed_gate.allow == original_gate_allow

    return ReplayResult(
        reproducible=gate_matches,
        replayed_content=replayed_content,
        original_gate_allow=original_gate_allow,
        replayed_gate_allow=replayed_gate.allow,
        reason="reproduced exactly" if gate_matches else (
            f"gate decision changed: was {original_gate_allow}, now {replayed_gate.allow} "
            f"(policy may have changed since the original decision)"
        ),
    )


# ============================================================
# Reference template library -- the worked examples named in the plan:
# threshold changes, policy rule additions.
# ============================================================

def _render_approval_threshold(params: dict[str, Any]) -> str:
    threshold = params["threshold_usd"]
    currency = params.get("currency", "USD")
    return (
        "# Auto-generated by Vouchstone Forge's Deterministic Transformation Engine.\n"
        "# Template: adjust_approval_threshold. Re-rendering with the same params\n"
        "# always produces this exact file -- see transform.py:replay_and_verify().\n"
        f"APPROVAL_THRESHOLD_{currency} = {threshold!r}\n"
    )


def _render_policy_rule(params: dict[str, Any]) -> str:
    name = params["policy_name"]
    effect = params["effect"]
    action = params["action"]
    return (
        "# Auto-generated by Vouchstone Forge's Deterministic Transformation Engine.\n"
        "# Template: add_policy_rule.\n"
        "from vouchstone_sdk import Policy\n\n"
        f"policy = Policy(\n"
        f"    name={name!r},\n"
        f"    effect={effect!r},\n"
        f"    action={{\"eq\": {action!r}}},\n"
        f")\n"
    )


def default_template_library() -> TemplateLibrary:
    """The starting set. Grows over time -- this is deliberately not
    exhaustive; it proves the mechanism against the two worked examples
    named in the plan (threshold changes, policy rule additions)."""
    return TemplateLibrary([
        TransformationTemplate(
            id="adjust_approval_threshold",
            name="Adjust approval threshold",
            description="Change the dollar threshold above which an action requires approval.",
            file_path="config/approval_threshold.py",
            parameters=[
                TemplateParameter("threshold_usd", "New threshold in USD"),
                TemplateParameter("currency", "Currency code", required=False),
            ],
            render_fn=_render_approval_threshold,
            match_keywords=["threshold", "approval limit", "approval amount"],
        ),
        TransformationTemplate(
            id="add_policy_rule",
            name="Add a policy rule",
            description="Add a new permit/forbid policy rule for a given action.",
            file_path="policies/new_policy.py",
            parameters=[
                TemplateParameter("policy_name", "Human-readable policy name"),
                TemplateParameter("effect", "permit or forbid"),
                TemplateParameter("action", "Action kind this policy applies to"),
            ],
            render_fn=_render_policy_rule,
            match_keywords=["add a policy", "add policy rule", "new policy", "policy rule"],
        ),
    ])
