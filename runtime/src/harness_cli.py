"""``vouchstone harness`` CLI (C4) — pull a signed bundle once, then run the
agent runtime completely offline against it.

    vouchstone harness pull <agent_id> --tenant-id <id> --out <bundle_dir>
    vouchstone harness verify <bundle_dir>
    vouchstone harness status <bundle_dir>
    vouchstone harness sync <bundle_dir>
    vouchstone harness scan [--requirement <path>]   # C8: dependency vuln scan
    vouchstone harness push <bundle_dir>   # not yet available -- see below

`push`'s compatibility-gated merge-back depends on the gate itself (C7 /
Vouchstone Forge), which doesn't exist yet. Rather than ship a `push` that
silently no-ops or fakes a passing gate, it exits non-zero with a clear
"not yet available" message until that gate is real.

`verify` and `scan` are the two CI/CD-native artifact validation points
(C8): both use real, distinct exit codes so a pipeline step can gate on
them directly -- `verify`: 0 valid / 1 invalid; `scan`: 0 clean / 1
vulnerabilities found / 2 could not scan (pip-audit unavailable --
deliberately distinct from "clean", never silently treated as passing).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .bundle import (
    BundleManifest, BundleVerificationError, compute_kg_snapshot_hash,
    save_bundle, verify_bundle,
)
from .dependency_scan import DependencyScanError, PipAuditNotAvailableError, run_dependency_scan
from .local_kg import CURRENT_SCHEMA_VERSION, _META_SCHEMA, _SCHEMA


def _build_kg_snapshot_sqlite(entities: list, edges: list) -> bytes:
    """Build a sqlite file (as bytes) from the JSON entities/edges the
    control plane returned. Kept independent of LocalKGStore's async
    wrapper since the CLI runs synchronously.

    Writes the current schema shape directly (including the ``_meta``
    schema-version stamp), matching what ``LocalKGStore.initialize()``
    would produce for a brand-new snapshot -- so a freshly pulled bundle
    never has to go through the legacy-v1-detection migration path to
    reach a consistent state. Entity/edge inserts are upserts on their
    natural identity for the same reason ``LocalKGStore`` uses upserts:
    idempotent under a retried or overlapping pull.
    """
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        conn.executescript(_SCHEMA)
        conn.executescript(_META_SCHEMA)
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(CURRENT_SCHEMA_VERSION),),
        )
        for e in entities:
            conn.execute(
                """INSERT INTO entities (id, entity_type, entity_key, attributes, confidence, source_trace_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     entity_type=excluded.entity_type, entity_key=excluded.entity_key,
                     attributes=excluded.attributes, confidence=excluded.confidence,
                     source_trace_id=excluded.source_trace_id, updated_at=excluded.updated_at""",
                (e["id"], e["entity_type"], e["entity_key"], json.dumps(e.get("attributes", {})),
                 e.get("confidence", 1.0), e.get("source_trace_id"), e["created_at"], e["created_at"]),
            )
        for ed in edges:
            conn.execute(
                """INSERT INTO edges (source_id, target_id, edge_type, attributes) VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_id, target_id, edge_type) DO UPDATE SET attributes=excluded.attributes""",
                (ed["source_id"], ed["target_id"], ed["edge_type"], json.dumps(ed.get("attributes", {}))),
            )
        conn.commit()
        conn.close()
        return Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _fetch_bundle(control_plane_url: str, api_key: str, agent_id: str, tenant_id: str) -> Dict[str, Any]:
    resp = httpx.get(
        f"{control_plane_url.rstrip('/')}/api/v1/harness/bundle/{agent_id}",
        params={"tenant_id": tenant_id},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def cmd_pull(args: argparse.Namespace) -> int:
    print(f"Pulling bundle for agent {args.agent_id} from {args.control_plane_url} ...")
    try:
        payload = _fetch_bundle(args.control_plane_url, args.api_key, args.agent_id, args.tenant_id)
    except httpx.HTTPStatusError as e:
        print(f"error: control plane returned {e.response.status_code}: {e.response.text[:300]}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"error: could not reach control plane: {e}", file=sys.stderr)
        return 1

    manifest = BundleManifest.from_dict(payload["manifest"])
    kg = payload["kg_snapshot"]
    kg_bytes = _build_kg_snapshot_sqlite(kg["entities"], kg["edges"])

    save_bundle(args.out, manifest, kg_bytes)

    try:
        verify_bundle(args.out, public_key_pem=args.public_key, hmac_secret=args.hmac_secret)
    except BundleVerificationError as e:
        print(f"error: pulled bundle failed verification: {e}", file=sys.stderr)
        return 1

    print(f"Bundle pulled and verified: {len(manifest.agents)} agent(s), "
          f"{manifest.kg_entity_count} KG entities -> {args.out}")
    print(f"Run with VOUCHSTONE_BUNDLE_PATH={args.out} to operate fully offline.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        manifest = verify_bundle(args.bundle_dir, public_key_pem=args.public_key, hmac_secret=args.hmac_secret)
    except (BundleVerificationError, FileNotFoundError) as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print(f"VALID — bundle_ref={manifest.bundle_ref} agents={len(manifest.agents)} "
          f"kg_entities={manifest.kg_entity_count} signed_by={manifest.signer_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        from .bundle import load_manifest
        manifest = load_manifest(args.bundle_dir)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    payload = manifest.to_dict()

    snapshot_path = Path(args.bundle_dir) / manifest.kg_snapshot_filename
    if snapshot_path.exists():
        conn = sqlite3.connect(str(snapshot_path))
        row = conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone() \
            if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'").fetchone() \
            else None
        conn.close()
        payload["kg_schema_version"] = int(row[0]) if row else 1  # pre-versioning snapshot, implicit v1

    print(json.dumps(payload, indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .bundle import load_manifest
    try:
        old_manifest = load_manifest(args.bundle_dir)
    except FileNotFoundError:
        print(f"error: no existing bundle at {args.bundle_dir} -- use `pull` first", file=sys.stderr)
        return 1

    agent_id = old_manifest.agents[0]["id"] if old_manifest.agents else None
    if not agent_id:
        print("error: existing bundle has no agents to sync", file=sys.stderr)
        return 1

    print(f"Syncing bundle for agent {agent_id} ...")
    pull_args = argparse.Namespace(
        agent_id=agent_id, tenant_id=old_manifest.tenant_id, out=args.bundle_dir,
        control_plane_url=args.control_plane_url, api_key=args.api_key,
        public_key=args.public_key, hmac_secret=args.hmac_secret,
    )
    rc = cmd_pull(pull_args)
    if rc == 0:
        new_manifest = load_manifest(args.bundle_dir)
        delta = new_manifest.kg_entity_count - old_manifest.kg_entity_count
        print(f"KG entities: {old_manifest.kg_entity_count} -> {new_manifest.kg_entity_count} ({delta:+d})")
    return rc


def cmd_scan(args: argparse.Namespace) -> int:
    try:
        result = run_dependency_scan(requirement_path=args.requirement)
    except PipAuditNotAvailableError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DependencyScanError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"Scanned {result.scanned_package_count} package(s) with {result.tool} {result.tool_version}")
        if not result.has_vulnerabilities:
            print("No known vulnerabilities found.")
        else:
            for pkg in result.vulnerable_packages:
                for vuln in pkg.vulnerabilities:
                    fix = f" (fixed in {', '.join(vuln.fix_versions)})" if vuln.fix_versions else ""
                    print(f"  {pkg.name}=={pkg.version}: {vuln.id}{fix}")
            print(f"{result.total_vulnerability_count} known vulnerabilit"
                  f"{'y' if result.total_vulnerability_count == 1 else 'ies'} "
                  f"across {len(result.vulnerable_packages)} package(s).")

    return 1 if result.has_vulnerabilities else 0


def cmd_push(args: argparse.Namespace) -> int:
    print(
        "`push` is not yet available. Compatibility-gated merge-back depends on the "
        "compatibility gate itself (Vouchstone Forge, C7), which hasn't shipped yet -- "
        "rather than accept a push with no real gate behind it, this refuses outright.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vouchstone", description="Vouchstone harness CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    harness = sub.add_parser("harness", help="offline harness bundle operations")
    harness_sub = harness.add_subparsers(dest="harness_command", required=True)

    def common_pull_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--control-plane-url", required=True, help="control plane base URL")
        p.add_argument("--api-key", required=True, help="API key/bearer token")
        p.add_argument("--public-key", default=None, help="PEM path or literal PEM for Ed25519 verification")
        p.add_argument("--hmac-secret", default=None, help="shared secret for HMAC verification (dev/test only)")

    p_pull = harness_sub.add_parser("pull", help="pull a signed bundle for one agent")
    p_pull.add_argument("agent_id")
    p_pull.add_argument("--tenant-id", required=True)
    p_pull.add_argument("--out", required=True, help="local bundle directory to write")
    common_pull_args(p_pull)
    p_pull.set_defaults(func=cmd_pull)

    p_verify = harness_sub.add_parser("verify", help="verify a local bundle's hash + signature")
    p_verify.add_argument("bundle_dir")
    p_verify.add_argument("--public-key", default=None)
    p_verify.add_argument("--hmac-secret", default=None)
    p_verify.set_defaults(func=cmd_verify)

    p_status = harness_sub.add_parser("status", help="print a local bundle's manifest")
    p_status.add_argument("bundle_dir")
    p_status.set_defaults(func=cmd_status)

    p_sync = harness_sub.add_parser("sync", help="re-pull the latest bundle for an existing local bundle dir")
    p_sync.add_argument("bundle_dir")
    common_pull_args(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_scan = harness_sub.add_parser("scan", help="scan dependencies for known vulnerabilities (via pip-audit)")
    p_scan.add_argument("--requirement", default=None, help="requirements file to scan; defaults to the active environment")
    p_scan.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a summary")
    p_scan.set_defaults(func=cmd_scan)

    p_push = harness_sub.add_parser("push", help="(not yet available) push local bundle changes back")
    p_push.add_argument("bundle_dir")
    p_push.set_defaults(func=cmd_push)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    public_key = getattr(args, "public_key", None)
    if public_key and Path(public_key).exists():
        args.public_key = Path(public_key).read_text()

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
