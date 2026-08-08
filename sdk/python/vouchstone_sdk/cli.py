"""``vouchstone`` — the SDK's command-line interface.

Knowledge-graph commands (the KG pillar, fully offline):

    vouchstone kg build <path> -o graph.json     # point at a repo -> signed artifact
    vouchstone kg build <path> -o graph.json --incremental
    vouchstone kg verify graph.json              # air-gapped tamper check
    vouchstone kg diff old.json new.json         # entity/file-level diff
    vouchstone kg agents graph.json              # the graph proposes its own agents

stdlib argparse only — the CLI must work on a bare ``pip install
vouchstone-sdk`` with zero extras.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .agent import AgentConfig
from .harness import HarnessPosture, Scope
from .kg import (
    KGArtifact,
    build_codebase_artifact,
    diff_artifacts,
    propose_agents_from_artifact,
    semantic_enrich,
    verify_artifact,
)


def _cmd_kg_build(args: argparse.Namespace) -> int:
    out = Path(args.output)
    previous = None
    if args.incremental and out.exists():
        previous = KGArtifact.load(out)
        print(f"incremental: reusing unchanged files from {out}")

    artifact = build_codebase_artifact(args.path, previous=previous)

    if args.semantic:
        enriched = asyncio.run(semantic_enrich(artifact, model=args.semantic_model))
        print(f"semantic enrichment: {enriched} module summaries added")

    artifact.save(out)
    graph = artifact.graph
    print(f"built {out}: {len(graph)} entities "
          f"({len(graph.entities_by_type('module'))} modules, "
          f"{len(graph.entities_by_type('class'))} classes, "
          f"{len(graph.entities_by_type('function'))} functions) "
          f"from {len(artifact.source_hashes)} files")
    print(f"signature: {artifact.signature}")
    return 0


def _cmd_kg_verify(args: argparse.Namespace) -> int:
    artifact = KGArtifact.load(args.artifact)
    result = verify_artifact(artifact)
    print(f"{args.artifact}: {'VALID' if result.valid else 'INVALID'} — {result.reason}")
    return 0 if result.valid else 1


def _cmd_kg_diff(args: argparse.Namespace) -> int:
    old = KGArtifact.load(args.old)
    new = KGArtifact.load(args.new)
    diff = diff_artifacts(old, new)
    if diff.empty:
        print("no differences")
        return 0
    for label, items in [
        ("files added", diff.added_files),
        ("files removed", diff.removed_files),
        ("files changed", diff.changed_files),
        ("entities added", diff.added_entities),
        ("entities removed", diff.removed_entities),
        ("entities changed", diff.changed_entities),
    ]:
        if items:
            print(f"{label} ({len(items)}):")
            for item in items[: args.limit]:
                print(f"  {item}")
            if len(items) > args.limit:
                print(f"  ... and {len(items) - args.limit} more")
    return 0


def _cmd_kg_agents(args: argparse.Namespace) -> int:
    artifact = KGArtifact.load(args.artifact)
    candidates = propose_agents_from_artifact(
        artifact, max_candidates=args.max_candidates,
    )
    if not candidates:
        print("no agent candidates: the graph has no domain with enough entities")
        return 0
    if args.json:
        print(json.dumps([{
            "name": c.name, "role": c.role, "domain": c.domain,
            "entity_count": c.entity_count,
            "agent_config": c.to_agent_config_kwargs(),
        } for c in candidates], indent=2))
        return 0
    print(f"{len(candidates)} agent candidate(s) proposed by the graph:\n")
    for c in candidates:
        print(f"  {c.name}")
        print(f"    role:    {c.role}")
        print(f"    scope:   domains={c.scoped_domains} kinds={c.scoped_subgraph}")
        print(f"    grounds: {c.entity_count} entities")
        print()
    print("accept one with AgentConfig(**candidate.to_agent_config_kwargs()) "
          "or export it to OpenCode (vouchstone opencode export-agent).")
    return 0


def _cmd_oc_export_agent(args: argparse.Namespace) -> int:
    from .opencode import export_agent

    scope = None
    if args.domains or args.entity_types or args.allowed_tools:
        scope = Scope(
            domains=args.domains.split(",") if args.domains else None,
            entity_types=args.entity_types.split(",") if args.entity_types else None,
            allowed_tools=args.allowed_tools.split(",") if args.allowed_tools else None,
        )
    config = AgentConfig(
        name=args.name,
        model=args.model,
        system_prompt=Path(args.prompt_file).read_text() if args.prompt_file else None,
    )
    path = export_agent(
        config, args.workspace, scope=scope,
        posture=HarnessPosture(args.posture), role=args.role,
    )
    print(f"exported {path}")
    return 0


def _cmd_oc_import_agent(args: argparse.Namespace) -> int:
    from .opencode import diff_agent_markdown, governed_import, import_agent

    previous = Path(args.previous).read_text() if args.previous else ""
    if args.preview:
        new_markdown = Path(args.file).read_text()
        print(diff_agent_markdown(previous, new_markdown, Path(args.file).stem)
              or "no changes")
        return 0
    if args.governed:
        kwargs, gate_result, trace = governed_import(
            args.file, previous_markdown=previous,
        )
        print(f"gate: {'ALLOW' if gate_result.allow else 'DENY'} — {gate_result.reason}")
        if gate_result.policy_decision:
            print(f"obligations: {gate_result.policy_decision.obligations}")
        print(f"trace tip: {trace.tip_hash}")
        if kwargs is None:
            return 1
    else:
        kwargs, _spec = import_agent(args.file)
    print(json.dumps(kwargs, indent=2, default=str))
    return 0


def _cmd_oc_init(args: argparse.Namespace) -> int:
    from .opencode import init_workspace

    agents: list[tuple[AgentConfig, Scope | None]] = []
    if args.from_kg:
        artifact = KGArtifact.load(args.from_kg)
        for candidate in propose_agents_from_artifact(artifact):
            agents.append((
                AgentConfig(**candidate.to_agent_config_kwargs()),
                Scope(domains=candidate.scoped_domains,
                      entity_types=candidate.scoped_subgraph),
            ))
    manifest = init_workspace(
        args.workspace, agents=agents, posture=HarnessPosture(args.posture),
    )
    for kind, paths in manifest.items():
        for path in paths:
            print(f"  {kind}: {path}")
    print(f"workspace ready: open it with `opencode` in {args.workspace}")
    return 0


def _cmd_oc_optimize_agent(args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import os

    from .client import VouchstoneClient

    api_url = args.api_url or os.environ.get("VOUCHSTONE_API_URL")
    api_key = args.api_key or os.environ.get("VOUCHSTONE_API_KEY")
    tenant_id = args.tenant_id or os.environ.get("VOUCHSTONE_TENANT_ID")
    if not (api_url and api_key and tenant_id):
        print("optimize-agent drives the control plane's Optimization Studio -- "
              "set VOUCHSTONE_API_URL / VOUCHSTONE_API_KEY / VOUCHSTONE_TENANT_ID "
              "(or pass --api-url/--api-key/--tenant-id).")
        return 2

    async def _run() -> int:
        async with VouchstoneClient(api_key, api_url, tenant_id=tenant_id) as client:
            run = await client._post("/optimization/runs", {
                "name": f"opencode-optimize-{args.agent_id}",
                "agent_id": args.agent_id,
                "dataset_id": args.dataset_id,
                "optimizer_type": args.optimizer,
            })
            print(f"optimization run started: {run.get('id')} "
                  f"(status: {run.get('status')})")
            print(f"poll with GET /api/v1/optimization/runs/{run.get('id')}")
        return 0

    return _asyncio.run(_run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vouchstone",
        description="Vouchstone SDK CLI — signed knowledge graphs and governed agents.",
    )
    parser.add_argument("--version", action="version", version=f"vouchstone-sdk {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    kg = sub.add_parser("kg", help="knowledge-graph artifacts")
    kg_sub = kg.add_subparsers(dest="kg_command", required=True)

    p_build = kg_sub.add_parser("build", help="build a signed KG artifact from a directory")
    p_build.add_argument("path", help="directory to analyze")
    p_build.add_argument("-o", "--output", default="vouchstone-kg.json")
    p_build.add_argument("--incremental", action="store_true",
                         help="reuse unchanged files from an existing output artifact")
    p_build.add_argument("--semantic", action="store_true",
                         help="add LLM module summaries (requires the llm-openai extra)")
    p_build.add_argument("--semantic-model", default="gpt-4o-mini")
    p_build.set_defaults(func=_cmd_kg_build)

    p_verify = kg_sub.add_parser("verify", help="verify an artifact's hash-chain signature")
    p_verify.add_argument("artifact")
    p_verify.set_defaults(func=_cmd_kg_verify)

    p_diff = kg_sub.add_parser("diff", help="diff two artifacts")
    p_diff.add_argument("old")
    p_diff.add_argument("new")
    p_diff.add_argument("--limit", type=int, default=20, help="max items shown per section")
    p_diff.set_defaults(func=_cmd_kg_diff)

    p_agents = kg_sub.add_parser("agents", help="derive agent candidates from an artifact")
    p_agents.add_argument("artifact")
    p_agents.add_argument("--max-candidates", type=int, default=5)
    p_agents.add_argument("--json", action="store_true", help="machine-readable output")
    p_agents.set_defaults(func=_cmd_kg_agents)

    oc = sub.add_parser("opencode", help="OpenCode integration — edit agents, copy skills")
    oc_sub = oc.add_subparsers(dest="oc_command", required=True)

    p_export = oc_sub.add_parser("export-agent", help="export a Vouchstone agent to .opencode/agents/")
    p_export.add_argument("name")
    p_export.add_argument("--workspace", default=".")
    p_export.add_argument("--model", default="claude-sonnet-4-6")
    p_export.add_argument("--role", default=None)
    p_export.add_argument("--prompt-file", default=None, help="file containing the persona prompt")
    p_export.add_argument("--domains", default=None, help="comma-separated KG domain scope")
    p_export.add_argument("--entity-types", default=None, help="comma-separated entity-kind scope")
    p_export.add_argument("--allowed-tools", default=None, help="comma-separated tool boundary")
    p_export.add_argument("--posture", choices=["auto", "strict"], default="auto")
    p_export.set_defaults(func=_cmd_oc_export_agent)

    p_import = oc_sub.add_parser("import-agent", help="import an (edited) OpenCode agent file")
    p_import.add_argument("file")
    p_import.add_argument("--previous", default=None, help="prior version for diff/governance")
    p_import.add_argument("--preview", action="store_true", help="show the diff, change nothing")
    p_import.add_argument("--governed", action="store_true",
                          help="run the import through the Forge gate + signed trace")
    p_import.set_defaults(func=_cmd_oc_import_agent)

    p_init = oc_sub.add_parser("init", help="scaffold a full .opencode/ workspace (agents, skills, MCP, commands)")
    p_init.add_argument("workspace", nargs="?", default=".")
    p_init.add_argument("--from-kg", default=None,
                        help="KG artifact: every proposed agent candidate is exported, scoped")
    p_init.add_argument("--posture", choices=["auto", "strict"], default="auto")
    p_init.set_defaults(func=_cmd_oc_init)

    p_opt = oc_sub.add_parser("optimize-agent", help="drive the control plane's Optimization Studio for an agent")
    p_opt.add_argument("agent_id")
    p_opt.add_argument("--dataset-id", required=True)
    p_opt.add_argument("--optimizer", default="mipro_v2")
    p_opt.add_argument("--api-url", default=None)
    p_opt.add_argument("--api-key", default=None)
    p_opt.add_argument("--tenant-id", default=None)
    p_opt.set_defaults(func=_cmd_oc_optimize_agent)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
