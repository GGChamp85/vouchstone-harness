"""Offline harness bundle format (C4).

A bundle is a directory pulled once from the control plane and then usable
completely offline:

    <bundle_dir>/
        manifest.json      # BundleManifest -- agent defs, hash, signature
        kg_snapshot.sqlite  # LocalKGStore (see local_kg.py)

The manifest hash uses the exact same canonical-JSON + SHA-256 algorithm as
the control plane's ledger and the SDK's WorkflowTrace (C6) --
``sha256(canonical_json(content))`` with no prev_hash chaining (a bundle is
a single snapshot, not an append-only log). The signature is verified
client-side against the control plane's Ed25519 public key when one is
configured, so a customer can confirm a bundle actually came from their own
control plane without holding its private signing key. HMAC verification
is supported for dev/test only, mirroring the server's own fallback.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vouchstone_sdk import canonical_json, compute_entry_hash

KG_SNAPSHOT_FILENAME = "kg_snapshot.sqlite"
MANIFEST_FILENAME = "manifest.json"


class BundleVerificationError(Exception):
    """Raised when a bundle's hash or signature doesn't check out. Never
    silently accept an unverified bundle -- this must be a hard failure."""


@dataclass
class BundleManifest:
    bundle_ref: str
    tenant_id: str
    agents: List[Dict[str, Any]] = field(default_factory=list)
    sdk_version: str = ""
    kg_snapshot_filename: str = KG_SNAPSHOT_FILENAME
    kg_entity_count: int = 0
    kg_snapshot_hash: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    content_hash: str = ""
    signer_id: str = ""
    signature: str = ""

    def content_for_hash(self) -> Dict[str, Any]:
        """Everything except the hash/signature fields themselves."""
        return {
            "bundle_ref": self.bundle_ref,
            "tenant_id": self.tenant_id,
            "agents": self.agents,
            "sdk_version": self.sdk_version,
            "kg_snapshot_filename": self.kg_snapshot_filename,
            "kg_entity_count": self.kg_entity_count,
            "kg_snapshot_hash": self.kg_snapshot_hash,
            "created_at": self.created_at,
        }

    def compute_hash(self) -> str:
        return compute_entry_hash("", self.content_for_hash())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BundleManifest":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def compute_kg_snapshot_hash(entities: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> str:
    """Hash over the KG snapshot's actual content (not just its entity
    count), so tampering with the snapshot file itself is detectable.
    Callers on both sides (control plane when signing, LocalKGStore when
    verifying) MUST supply entities/edges sorted by id for this to be
    reproducible -- canonical_json sorts dict keys but not list order."""
    return compute_entry_hash("", {"entities": entities, "edges": edges})


def save_bundle(bundle_dir: str, manifest: BundleManifest, kg_snapshot_bytes: bytes) -> None:
    """Write a manifest + KG snapshot to disk. Caller is responsible for
    having already set ``content_hash``/``signer_id``/``signature`` on the
    manifest (the control plane signs it before returning it to the CLI)."""
    path = Path(bundle_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / MANIFEST_FILENAME).write_text(json.dumps(manifest.to_dict(), indent=2))
    (path / manifest.kg_snapshot_filename).write_bytes(kg_snapshot_bytes)


def load_manifest(bundle_dir: str) -> BundleManifest:
    path = Path(bundle_dir) / MANIFEST_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"no bundle manifest at {path}")
    return BundleManifest.from_dict(json.loads(path.read_text()))


def verify_bundle(
    bundle_dir: str,
    *,
    public_key_pem: Optional[str] = None,
    hmac_secret: Optional[str] = None,
) -> BundleManifest:
    """Verify a bundle's content hash and signature. Raises
    BundleVerificationError on any mismatch -- callers must not proceed to
    load an unverified bundle. Returns the manifest on success."""
    manifest = load_manifest(bundle_dir)

    expected_hash = manifest.compute_hash()
    if manifest.content_hash != expected_hash:
        raise BundleVerificationError(
            f"bundle content hash mismatch: manifest claims {manifest.content_hash}, "
            f"recomputed {expected_hash} -- bundle may be corrupted or tampered with"
        )

    snapshot_path = Path(bundle_dir) / manifest.kg_snapshot_filename
    if not snapshot_path.exists():
        raise BundleVerificationError(f"bundle is missing its KG snapshot file: {snapshot_path}")

    if manifest.kg_snapshot_hash:
        actual_hash = _hash_kg_snapshot_file(str(snapshot_path))
        if actual_hash != manifest.kg_snapshot_hash:
            raise BundleVerificationError(
                f"KG snapshot content hash mismatch: manifest claims {manifest.kg_snapshot_hash}, "
                f"recomputed {actual_hash} -- snapshot file may be corrupted or tampered with"
            )

    if not manifest.signature:
        raise BundleVerificationError("bundle has no signature -- refusing to trust an unsigned bundle")

    if manifest.signer_id.startswith("ed25519:"):
        if not public_key_pem:
            raise BundleVerificationError(
                "bundle is Ed25519-signed but no public key was provided to verify against"
            )
        _verify_ed25519(manifest.content_hash, manifest.signature, public_key_pem)
    elif manifest.signer_id.startswith("hmac-sha256:"):
        if not hmac_secret:
            raise BundleVerificationError(
                "bundle is HMAC-signed but no shared secret was provided to verify against"
            )
        _verify_hmac(manifest.content_hash, manifest.signature, hmac_secret)
    else:
        raise BundleVerificationError(f"unrecognized signer_id: {manifest.signer_id!r}")

    return manifest


def _hash_kg_snapshot_file(sqlite_path: str) -> str:
    """Read entities/edges straight out of the sqlite file (ORDER BY id, same
    ordering the control plane uses when signing) and hash them the same
    way compute_kg_snapshot_hash() does -- deliberately not going through
    LocalKGStore's async wrapper since verify_bundle() runs synchronously."""
    import sqlite3

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        entities = [
            {
                "id": r["id"], "entity_type": r["entity_type"], "entity_key": r["entity_key"],
                "attributes": json.loads(r["attributes"]), "confidence": r["confidence"],
                "source_trace_id": r["source_trace_id"], "created_at": r["created_at"],
            }
            for r in conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
        ]
        edges = [
            {"source_id": r["source_id"], "target_id": r["target_id"], "edge_type": r["edge_type"],
             "attributes": json.loads(r["attributes"])}
            for r in conn.execute("SELECT * FROM edges ORDER BY source_id, target_id, edge_type").fetchall()
        ]
    finally:
        conn.close()
    return compute_kg_snapshot_hash(entities, edges)


def _verify_ed25519(entry_hash: str, signature_b64: str, public_key_pem: str) -> None:
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.exceptions import InvalidSignature
    except ImportError as exc:  # pragma: no cover - dependency always present via cryptography
        raise BundleVerificationError(f"cryptography library unavailable: {exc}") from exc

    try:
        public_key = load_pem_public_key(public_key_pem.encode("utf-8"))
        public_key.verify(base64.b64decode(signature_b64), entry_hash.encode("utf-8"))
    except InvalidSignature as exc:
        raise BundleVerificationError("Ed25519 signature verification failed") from exc
    except Exception as exc:
        raise BundleVerificationError(f"could not verify Ed25519 signature: {exc}") from exc


def _verify_hmac(entry_hash: str, signature_b64: str, secret: str) -> None:
    import hashlib
    import hmac

    expected = hmac.new(secret.encode("utf-8"), entry_hash.encode("utf-8"), hashlib.sha256).digest()
    actual = base64.b64decode(signature_b64)
    if not hmac.compare_digest(expected, actual):
        raise BundleVerificationError("HMAC signature verification failed")
