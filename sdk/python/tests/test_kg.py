"""KG artifacts (kg.py) -- build determinism, incremental reuse, signature
verification/tamper detection, diff, agent-candidate derivation, and
memory grounding. All offline (deterministic ast pass; no LLM)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouchstone_sdk.kg import (
    KGArtifact,
    build_codebase_artifact,
    diff_artifacts,
    propose_agents_from_artifact,
    seed_pipeline_from_artifact,
    verify_artifact,
)
from vouchstone_sdk.memory import MemoryPipeline


def _write_tree(root: Path) -> None:
    billing = root / "billing"
    billing.mkdir(parents=True)
    (billing / "__init__.py").write_text('"""Billing package."""\n')
    (billing / "invoices.py").write_text(
        '"""Invoice matching."""\n'
        "import json\n\n"
        "class InvoiceMatcher:\n"
        '    """Matches invoices to POs."""\n'
        "    def match(self, invoice):\n"
        "        return json.dumps(invoice)\n\n"
        "def load_invoices(path):\n"
        "    return []\n"
    )
    ops = root / "ops"
    ops.mkdir()
    (ops / "deploy.py").write_text(
        "from billing import invoices\n\n"
        "async def deploy():\n"
        "    pass\n"
    )


def test_build_extracts_modules_classes_functions(tmp_path: Path):
    _write_tree(tmp_path)
    artifact = build_codebase_artifact(tmp_path)

    modules = artifact.graph.entities_by_type("module")
    classes = artifact.graph.entities_by_type("class")
    functions = artifact.graph.entities_by_type("function")
    assert {m.entity_key for m in modules} == {
        "billing/__init__.py", "billing/invoices.py", "ops/deploy.py",
    }
    assert [c.attributes["qualname"] for c in classes] == ["InvoiceMatcher"]
    qualnames = {f.attributes["qualname"] for f in functions}
    assert {"InvoiceMatcher.match", "load_invoices", "deploy"} <= qualnames
    deploy = next(f for f in functions if f.attributes["qualname"] == "deploy")
    assert deploy.attributes["async"] is True

    # contains edges: module -> class -> method
    invoices_mod = next(m for m in modules if m.entity_key == "billing/invoices.py")
    contained = artifact.graph.related(invoices_mod.id, "contains")
    assert {e.attributes.get("qualname") for e in contained} == {"InvoiceMatcher", "load_invoices"}


def test_build_is_deterministic_and_signed(tmp_path: Path):
    _write_tree(tmp_path)
    a = build_codebase_artifact(tmp_path)
    b = build_codebase_artifact(tmp_path)
    assert a.signature and a.signature == b.signature
    assert verify_artifact(a).valid


def test_save_load_verify_round_trip_and_tamper_detection(tmp_path: Path):
    _write_tree(tmp_path)
    artifact = build_codebase_artifact(tmp_path)
    out = tmp_path / "kg.json"
    artifact.save(out)

    loaded = KGArtifact.load(out)
    assert verify_artifact(loaded).valid
    assert loaded.signature == artifact.signature

    # Tamper with an entity attribute in the JSON -> verification fails.
    data = json.loads(out.read_text())
    data["graph"]["entities"][0]["attributes"]["docstring"] = "FORGED"
    tampered = KGArtifact.from_dict(data)
    result = verify_artifact(tampered)
    assert not result.valid
    assert "altered" in result.reason


def test_incremental_rebuild_reuses_unchanged_and_detects_changes(tmp_path: Path):
    _write_tree(tmp_path)
    first = build_codebase_artifact(tmp_path)

    # Unchanged tree: identical signature.
    again = build_codebase_artifact(tmp_path, previous=first)
    assert again.signature == first.signature

    # Change one file: only that file's entities differ.
    (tmp_path / "ops" / "deploy.py").write_text(
        "from billing import invoices\n\n"
        "async def deploy():\n    pass\n\n"
        "def rollback():\n    pass\n"
    )
    third = build_codebase_artifact(tmp_path, previous=first)
    assert third.signature != first.signature
    diff = diff_artifacts(first, third)
    assert diff.changed_files == ["ops/deploy.py"]
    assert any("rollback" in key for key in diff.added_entities)
    assert not diff.removed_entities
    assert verify_artifact(third).valid


def test_import_edges_link_in_tree_modules(tmp_path: Path):
    _write_tree(tmp_path)
    artifact = build_codebase_artifact(tmp_path)
    deploy_mod = next(
        m for m in artifact.graph.entities_by_type("module")
        if m.entity_key == "ops/deploy.py"
    )
    # "from billing import invoices" imports the in-tree "billing" package.
    imported = artifact.graph.related(deploy_mod.id, "imports")
    assert {m.entity_key for m in imported} == {"billing/__init__.py"}


def test_skip_dirs_excluded(tmp_path: Path):
    _write_tree(tmp_path)
    junk = tmp_path / "__pycache__"
    junk.mkdir()
    (junk / "cached.py").write_text("x = 1\n")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "vendored.py").write_text("y = 2\n")

    artifact = build_codebase_artifact(tmp_path)
    keys = {m.entity_key for m in artifact.graph.entities_by_type("module")}
    assert not any("__pycache__" in k or ".venv" in k for k in keys)


def test_propose_agents_scoped_to_dominant_domains(tmp_path: Path):
    _write_tree(tmp_path)
    artifact = build_codebase_artifact(tmp_path)
    candidates = propose_agents_from_artifact(artifact, min_entities=1)

    names = [c.name for c in candidates]
    assert "billing-specialist" in names
    billing = next(c for c in candidates if c.name == "billing-specialist")
    assert billing.scoped_domains == ["billing"]
    assert set(billing.scoped_subgraph) <= {"module", "class", "function"}
    kwargs = billing.to_agent_config_kwargs()
    assert kwargs["scoped_domains"] == ["billing"]
    assert "billing" in kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_seed_pipeline_from_artifact_grounds_semantic_memory(tmp_path: Path):
    _write_tree(tmp_path)
    artifact = build_codebase_artifact(tmp_path)

    pipeline = MemoryPipeline(agent_id="kg-agent", local_only=True)
    await pipeline.initialize()
    seeded = await seed_pipeline_from_artifact(pipeline, artifact)
    assert seeded == len(artifact.graph)

    ctx = await pipeline.prepare_context("sess-1", "InvoiceMatcher")
    keys = {e.entity_key for e in ctx.semantic_entities}
    assert any("InvoiceMatcher" in k for k in keys)
    await pipeline.close()


def test_unparseable_file_recorded_not_dropped(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    artifact = build_codebase_artifact(tmp_path)
    broken = next(
        m for m in artifact.graph.entities_by_type("module")
        if m.entity_key == "broken.py"
    )
    assert "syntax error" in broken.attributes["error"]
