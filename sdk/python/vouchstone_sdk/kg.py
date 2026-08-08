"""Signed, committable Knowledge-Graph artifacts — "point at anything, get
a verifiable knowledge graph."

The KG pillar's offline half. ``build_codebase_artifact()`` turns a local
directory tree into an :class:`EntityGraph` via a **deterministic** stdlib
``ast`` pass (modules, classes, functions, imports — no LLM, no network),
wraps it in a :class:`KGArtifact` whose manifest is hash-chained with the
exact same ``compute_entry_hash`` scheme as the control plane's signed
ledger, and writes one committable JSON file. That signature is the
differentiator over graph tools that stop at visualization: anyone holding
the artifact can verify, offline, that neither the graph nor the recorded
source hashes were tampered with (``verify_artifact``), diff two snapshots
entity-by-entity (``diff_artifacts``), and rebuild incrementally — unchanged
files are never re-parsed (``previous=`` on build).

The same artifact then grounds agents two ways:

- ``seed_pipeline_from_artifact()`` upserts its entities into an agent's
  semantic memory (works in every memory mode: local, ChromaDB, or
  control-plane-backed) so ``prepare_context()`` surfaces codebase entities
  like any other knowledge.
- ``propose_agents_from_artifact()`` derives *agent candidates from the
  graph itself* — one per dominant top-level package — each carrying a
  ready-to-enforce scope (entity kinds + domain slugs) and a persona draft.
  This is the offline counterpart of the control plane's real
  ``POST /workforce/agents/suggest-from-kg``; accepting a candidate yields
  an ``AgentConfig``-shaped dict wired to that scope.

Semantic enrichment (LLM summaries per entity) is deliberately a separate,
optional pass (``semantic_enrich``) requiring the ``llm-openai`` extra —
the deterministic core must work air-gapped.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .graph import EntityGraph, canonical_json, compute_entry_hash
from .types import Entity

logger = logging.getLogger("vouchstone.kg")

ARTIFACT_VERSION = 1
_GENERATOR = "vouchstone-sdk"

# Directories never worth parsing -- virtualenvs, caches, VCS internals.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".tox",
    "*.egg-info",
}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _should_skip(path: Path) -> bool:
    return any(
        part in _SKIP_DIRS or part.endswith(".egg-info") for part in path.parts
    )


# ============================================================
# Deterministic Python extraction (stdlib ast — no dependencies)
# ============================================================

def _extract_python_file(rel_path: str, source: str) -> tuple[list[Entity], list[dict[str, str]]]:
    """One file -> (entities, edge specs). Edge specs reference entity ids.

    Entities: one ``module`` plus a ``class``/``function`` per top-level
    (and class-level) definition. Edges: ``contains`` (module->def,
    class->method) and ``imports`` (module->module, by imported name --
    resolved to an internal module entity when the target is in-tree,
    recorded as an attribute otherwise so external deps are still visible).
    """
    module_id = _stable_id("module", rel_path)
    top_package = rel_path.split("/", 1)[0] if "/" in rel_path else rel_path
    domains = [top_package.removesuffix(".py")]

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # An unparseable file is still a real file: record the module with
        # the error rather than silently dropping it from the graph.
        return ([Entity(
            id=module_id, entity_type="module", entity_key=rel_path,
            attributes={"error": f"syntax error: {exc}", "domains": domains},
        )], [])

    entities: list[Entity] = [Entity(
        id=module_id, entity_type="module", entity_key=rel_path,
        attributes={
            "docstring": (ast.get_docstring(tree) or "")[:500],
            "loc": source.count("\n") + 1,
            "domains": domains,
        },
    )]
    edges: list[dict[str, str]] = []
    imported: list[str] = []

    def _add_def(node: ast.AST, parent_id: str, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            etype = "function"
        elif isinstance(node, ast.ClassDef):
            etype = "class"
        else:
            return
        qualname = f"{prefix}{node.name}"
        ent_id = _stable_id(etype, rel_path, qualname)
        entities.append(Entity(
            id=ent_id, entity_type=etype, entity_key=f"{rel_path}:{qualname}",
            attributes={
                "name": node.name,
                "qualname": qualname,
                "docstring": (ast.get_docstring(node) or "")[:500],
                "lineno": node.lineno,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "domains": domains,
            },
        ))
        edges.append({"source_id": parent_id, "target_id": ent_id, "edge_type": "contains"})
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                _add_def(child, ent_id, f"{qualname}.")

    for node in tree.body:
        _add_def(node, module_id, "")
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.append(node.module)

    entities[0].attributes["imports"] = sorted(set(imported))
    return entities, edges


def _link_imports(graph: EntityGraph, modules_by_dotted: dict[str, str]) -> None:
    """Resolve each module's recorded imports against in-tree modules and
    add ``imports`` edges for the ones that resolve internally."""
    for module in graph.entities_by_type("module"):
        for name in module.attributes.get("imports", []):
            target_id = modules_by_dotted.get(name)
            if target_id and target_id != module.id:
                graph.add_edge(module.id, target_id, "imports")


def _dotted_name(rel_path: str) -> str:
    p = rel_path.removesuffix(".py")
    p = p.removesuffix("/__init__")
    return p.replace("/", ".")


# ============================================================
# KGArtifact — build / sign / save / load / verify / diff
# ============================================================

@dataclass
class KGArtifact:
    """A knowledge graph plus the signed manifest proving what it was built
    from. ``signature`` is the tip of a hash chain over (sorted source
    hashes, then the canonical graph hash) — recomputable by anyone."""

    root_label: str
    source_hashes: dict[str, str]
    graph: EntityGraph
    version: int = ARTIFACT_VERSION
    generator: str = _GENERATOR
    signature: str = ""
    chain: list[dict[str, Any]] = field(default_factory=list)

    # ── hashing ────────────────────────────────────────────────────────

    @staticmethod
    def _canonical_graph_payload(graph: EntityGraph) -> dict[str, Any]:
        """Graph projection with volatile fields (created_at) removed and
        deterministic ordering, so the same tree always hashes the same."""
        data = graph.to_dict()
        entities = sorted(
            (
                {k: v for k, v in e.items() if k != "created_at"}
                for e in data["entities"]
            ),
            key=lambda e: str(e["id"]),
        )
        edges = sorted(
            data["edges"],
            key=lambda e: (str(e["source_id"]), str(e["target_id"]), str(e["edge_type"])),
        )
        return {"entities": entities, "edges": edges}

    def graph_hash(self) -> str:
        return _content_hash(canonical_json(self._canonical_graph_payload(self.graph)))

    def _compute_chain(self) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        prev = ""
        for path in sorted(self.source_hashes):
            payload = {"kind": "kg.source", "path": path,
                       "content_hash": self.source_hashes[path]}
            prev = compute_entry_hash(prev, payload)
            chain.append({**payload, "entry_hash": prev})
        payload = {"kind": "kg.graph", "graph_hash": self.graph_hash()}
        prev = compute_entry_hash(prev, payload)
        chain.append({**payload, "entry_hash": prev})
        return chain

    def sign(self) -> str:
        self.chain = self._compute_chain()
        self.signature = self.chain[-1]["entry_hash"]
        return self.signature

    # ── persistence ────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generator": self.generator,
            "root_label": self.root_label,
            "source_hashes": self.source_hashes,
            "graph": self.graph.to_dict(),
            "chain": self.chain,
            "signature": self.signature,
        }

    def save(self, path: str | Path) -> Path:
        if not self.signature:
            self.sign()
        out = Path(path)
        out.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str))
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KGArtifact:
        if data.get("version") != ARTIFACT_VERSION:
            raise ValueError(
                f"unsupported artifact version {data.get('version')!r} "
                f"(this SDK reads version {ARTIFACT_VERSION})"
            )
        return cls(
            root_label=data["root_label"],
            source_hashes=dict(data["source_hashes"]),
            graph=EntityGraph.from_dict(data["graph"]),
            version=data["version"],
            generator=data.get("generator", _GENERATOR),
            signature=data.get("signature", ""),
            chain=list(data.get("chain", [])),
        )

    @classmethod
    def load(cls, path: str | Path) -> KGArtifact:
        return cls.from_dict(json.loads(Path(path).read_text()))


def build_codebase_artifact(
    root: str | Path,
    *,
    previous: KGArtifact | None = None,
) -> KGArtifact:
    """Deterministically build (or incrementally rebuild) a KG artifact from
    a directory of Python source.

    With ``previous``, files whose content hash is unchanged are **not**
    re-parsed — their entities/edges are carried over from the previous
    graph. Rebuilding an unchanged tree yields a byte-identical signature.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"{root_path} is not a directory")

    files: dict[str, bytes] = {}
    for path in sorted(root_path.rglob("*.py")):
        rel_path = path.relative_to(root_path)
        if _should_skip(rel_path):
            continue
        files[rel_path.as_posix()] = path.read_bytes()

    source_hashes = {rel: _content_hash(data) for rel, data in files.items()}

    prev_entities_by_file: dict[str, list[Entity]] = {}
    prev_edges_by_file: dict[str, list[dict[str, str]]] = {}
    if previous is not None:
        entity_file: dict[str, str] = {}
        for e in previous.graph.all_entities():
            fname = e.entity_key.split(":", 1)[0]
            entity_file[e.id] = fname
            prev_entities_by_file.setdefault(fname, []).append(e)
        for prev_edge in previous.graph._edges:
            edge_file = entity_file.get(prev_edge.source_id)
            if edge_file is not None:
                prev_edges_by_file.setdefault(edge_file, []).append({
                    "source_id": prev_edge.source_id, "target_id": prev_edge.target_id,
                    "edge_type": prev_edge.edge_type,
                })

    graph = EntityGraph()
    pending_edges: list[dict[str, str]] = []
    reused = 0
    parsed = 0

    for rel, data in files.items():
        unchanged = (
            previous is not None
            and previous.source_hashes.get(rel) == source_hashes[rel]
            and rel in prev_entities_by_file
        )
        if unchanged:
            entities = prev_entities_by_file[rel]
            edges = [
                e for e in prev_edges_by_file.get(rel, [])
                if e["edge_type"] != "imports"  # re-linked globally below
            ]
            reused += 1
        else:
            entities, edges = _extract_python_file(rel, data.decode("utf-8", errors="replace"))
            parsed += 1
        for entity in entities:
            graph.add_entity(entity)
        pending_edges.extend(edges)

    for spec in pending_edges:
        if graph.get_entity(spec["source_id"]) and graph.get_entity(spec["target_id"]):
            graph.add_edge(spec["source_id"], spec["target_id"], spec["edge_type"])

    modules_by_dotted = {
        _dotted_name(m.entity_key): m.id for m in graph.entities_by_type("module")
    }
    _link_imports(graph, modules_by_dotted)

    logger.info(
        "kg build: %d files (%d parsed, %d reused), %d entities",
        len(files), parsed, reused, len(graph),
    )
    artifact = KGArtifact(
        root_label=root_path.name, source_hashes=source_hashes, graph=graph,
    )
    artifact.sign()
    return artifact


async def build_source_artifact(
    ingester: Any,
    since: Any,
    *,
    connect: bool = True,
) -> KGArtifact:
    """Build a signed KG artifact from a live source ingester (Slack, Jira,
    Confluence, GitHub, Meetings — anything subclassing ``BaseIngester``).

    The exact same artifact format as ``build_codebase_artifact``: entities
    land in the canonical ``types.Entity`` schema via the ingester's
    ``build_graph()``, and the manifest chain covers every raw event's
    content hash — so "what Slack said on the day this graph was built" is
    tamper-evident, exactly like source files are for a codebase build.

    The ingester's configured extraction strategy applies (``"llm"`` needs
    the llm-openai extra; ``"deterministic"`` runs air-gapped).
    """
    if connect:
        await ingester.connect()
    events = await ingester.fetch_raw(since)
    entities = await ingester.extract_entities(events)
    # Relationship extraction is inherently inferential (LLM) -- the
    # deterministic strategy's contract is zero inference, so skip it there
    # rather than letting the LLM call fail into a logged error.
    if len(entities) > 1 and getattr(ingester, "_extraction_strategy", "llm") != "deterministic":
        relationships = await ingester.extract_relationships(entities)
    else:
        relationships = []
    graph = ingester.build_graph(entities, relationships)

    source_hashes = {
        f"{ingester.source_name}/{event.id}": _content_hash(event.content.encode("utf-8"))
        for event in events
    }
    artifact = KGArtifact(
        root_label=ingester.source_name, source_hashes=source_hashes, graph=graph,
    )
    artifact.sign()
    return artifact


# ============================================================
# Verify + diff
# ============================================================

@dataclass
class VerifyResult:
    valid: bool
    reason: str = ""


def verify_artifact(artifact: KGArtifact) -> VerifyResult:
    """Recompute the full hash chain from the artifact's own data. Any
    tampering with source hashes, entities, edges, or chain order breaks
    the recomputed tip."""
    if not artifact.signature:
        return VerifyResult(False, "artifact is unsigned")
    recomputed = artifact._compute_chain()
    if recomputed[-1]["entry_hash"] != artifact.signature:
        return VerifyResult(False, "signature mismatch: artifact contents were altered")
    if [c.get("entry_hash") for c in artifact.chain] != [c["entry_hash"] for c in recomputed]:
        return VerifyResult(False, "manifest chain mismatch: chain entries were altered")
    return VerifyResult(True, "signature verified")


@dataclass
class ArtifactDiff:
    added_entities: list[str] = field(default_factory=list)
    removed_entities: list[str] = field(default_factory=list)
    changed_entities: list[str] = field(default_factory=list)
    added_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any([
            self.added_entities, self.removed_entities, self.changed_entities,
            self.added_files, self.removed_files, self.changed_files,
        ])


def diff_artifacts(old: KGArtifact, new: KGArtifact) -> ArtifactDiff:
    diff = ArtifactDiff()
    old_files, new_files = old.source_hashes, new.source_hashes
    diff.added_files = sorted(set(new_files) - set(old_files))
    diff.removed_files = sorted(set(old_files) - set(new_files))
    diff.changed_files = sorted(
        f for f in set(old_files) & set(new_files) if old_files[f] != new_files[f]
    )

    def _projection(e: Entity) -> bytes:
        return canonical_json({
            "type": e.entity_type, "key": e.entity_key, "attributes": e.attributes,
        })

    old_map = {e.id: e for e in old.graph.all_entities()}
    new_map = {e.id: e for e in new.graph.all_entities()}
    diff.added_entities = sorted(
        new_map[i].entity_key for i in set(new_map) - set(old_map)
    )
    diff.removed_entities = sorted(
        old_map[i].entity_key for i in set(old_map) - set(new_map)
    )
    diff.changed_entities = sorted(
        new_map[i].entity_key for i in set(old_map) & set(new_map)
        if _projection(old_map[i]) != _projection(new_map[i])
    )
    return diff


# ============================================================
# B4 — agent discovery FROM the graph (offline path)
# ============================================================

@dataclass
class AgentCandidate:
    """A specialist agent the graph itself proposes, with an enforceable
    scope. The online counterpart is the control plane's
    ``POST /workforce/agents/suggest-from-kg``."""

    name: str
    role: str
    domain: str
    entity_count: int
    scoped_domains: list[str]
    scoped_subgraph: list[str]
    persona_prompt: str

    def to_agent_config_kwargs(self) -> dict[str, Any]:
        """Kwargs ready for ``AgentConfig(**candidate.to_agent_config_kwargs())``."""
        return {
            "name": self.name,
            "system_prompt": self.persona_prompt,
            "scoped_domains": self.scoped_domains,
            "scoped_subgraph": self.scoped_subgraph,
        }


def propose_agents_from_artifact(
    artifact: KGArtifact, *, max_candidates: int = 5, min_entities: int = 3,
) -> list[AgentCandidate]:
    """Derive specialist-agent candidates from the artifact's domain
    distribution: one candidate per dominant top-level package, scoped to
    that package's domain slug and to the entity kinds it actually
    contains. Deterministic — no LLM."""
    by_domain: Counter[str] = Counter()
    kinds_by_domain: dict[str, set[str]] = {}
    for e in artifact.graph.all_entities():
        for d in e.attributes.get("domains", []):
            by_domain[d] += 1
            kinds_by_domain.setdefault(d, set()).add(e.entity_type)

    candidates: list[AgentCandidate] = []
    for domain, count in by_domain.most_common(max_candidates):
        if count < min_entities:
            continue
        kinds = sorted(kinds_by_domain.get(domain, set()))
        name = f"{domain}-specialist"
        candidates.append(AgentCandidate(
            name=name,
            role=f"{domain} domain specialist",
            domain=domain,
            entity_count=count,
            scoped_domains=[domain],
            scoped_subgraph=kinds,
            persona_prompt=(
                f"You are {name}, a specialist agent grounded in the "
                f"'{domain}' portion of the {artifact.root_label} knowledge "
                f"graph ({count} entities: {', '.join(kinds)}). Answer and "
                "act only within this scope; when a request falls outside "
                "it, say so explicitly instead of guessing."
            ),
        ))
    return candidates


# ============================================================
# B3 — ground an agent's memory in an artifact
# ============================================================

async def seed_pipeline_from_artifact(
    pipeline: Any, artifact: KGArtifact, *, entity_types: list[str] | None = None,
) -> int:
    """Upsert the artifact's entities into an agent's semantic memory so
    ``prepare_context()`` surfaces them like any other knowledge. Works in
    every memory mode (local, ChromaDB, control-plane) because it goes
    through ``SemanticMemory.upsert_entity``. Returns the number seeded."""
    count = 0
    for entity in artifact.graph.all_entities():
        if entity_types is not None and entity.entity_type not in entity_types:
            continue
        await pipeline.semantic.upsert_entity(pipeline.agent_id, entity)
        count += 1
    return count


# ============================================================
# Optional LLM semantic enrichment (llm-openai extra)
# ============================================================

async def semantic_enrich(
    artifact: KGArtifact, *, model: str = "gpt-4o-mini",
    api_key: str | None = None, max_entities: int = 50,
) -> int:
    """Add one-sentence LLM summaries to the largest un-summarized modules.
    Strictly optional: the deterministic artifact is complete without it.
    Requires the ``llm-openai`` extra; raises a guided ImportError without
    it. Re-sign the artifact afterwards (summaries change the graph hash).
    """
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "semantic enrichment requires the OpenAI client. "
            "Install it with: pip install 'vouchstone-sdk[llm-openai]'"
        ) from exc

    client = openai.AsyncOpenAI(api_key=api_key) if api_key else openai.AsyncOpenAI()
    modules = [
        m for m in artifact.graph.entities_by_type("module")
        if not m.attributes.get("summary")
    ]
    modules.sort(key=lambda m: m.attributes.get("loc", 0), reverse=True)

    enriched = 0
    for module in modules[:max_entities]:
        doc = module.attributes.get("docstring", "")
        members = [
            e.attributes.get("qualname", e.entity_key)
            for e in artifact.graph.related(module.id, "contains")
        ]
        resp = await client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    "In one sentence, state what this Python module does.\n"
                    f"Path: {module.entity_key}\nDocstring: {doc[:300]}\n"
                    f"Definitions: {', '.join(members[:20])}"
                ),
            }],
            max_tokens=80,
        )
        summary = (resp.choices[0].message.content or "").strip()
        if summary:
            module.attributes["summary"] = summary
            enriched += 1

    if enriched:
        artifact.sign()
    return enriched
