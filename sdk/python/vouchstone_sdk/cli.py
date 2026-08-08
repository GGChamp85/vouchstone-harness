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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
