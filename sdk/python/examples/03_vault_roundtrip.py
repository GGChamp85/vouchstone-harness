"""Document Vault round-trip against a real Vouchstone control plane.

The Vault is the moderation gatekeeper: every document passes
Raw -> Workspace (human review) -> Canonical before it can reach the
Knowledge Graph, Wiki, or Company Brain. This example uploads a document,
lists the tree, approves it, and triggers ingestion into the KG.

Requires a running control plane and credentials -- this example does not
fabricate offline behavior for an inherently server-side workflow:

    export VOUCHSTONE_API_URL="https://your-control-plane.example.com"
    export VOUCHSTONE_API_KEY="<your JWT or API key>"
    export VOUCHSTONE_TENANT_ID="<your tenant uuid>"
    python examples/03_vault_roundtrip.py
"""
import asyncio
import os
import sys

from vouchstone_sdk import VaultClient


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"{name} is not set. This example talks to a real control plane -- "
            "see the module docstring for the three required variables."
        )
    return value


async def main() -> None:
    api_url = _require_env("VOUCHSTONE_API_URL")
    api_key = _require_env("VOUCHSTONE_API_KEY")
    tenant_id = _require_env("VOUCHSTONE_TENANT_ID")

    async with VaultClient(api_key, api_url, tenant_id=tenant_id) as vault:
        vaults = await vault.list_vaults()
        if not vaults:
            created = await vault.create_vault(name="sdk-example-vault")
            vault_id = created["id"]
        else:
            vault_id = vaults[0]["id"]
        print(f"vault: {vault_id}")

        upload = await vault.upload_files(
            vault_id,
            [{
                "filename": "sdk-example.md",
                "content": b"# SDK example\n\nUploaded by examples/03_vault_roundtrip.py\n",
                "content_type": "text/markdown",
            }],
        )
        doc_ids = [d["id"] for d in upload.get("documents", []) if "id" in d]
        print(f"uploaded documents: {doc_ids or upload}")

        tree = await vault.list_tree(vault_id, layer="workspace")
        print(f"workspace tree: {len(tree)} document(s)")

        if doc_ids:
            approved = await vault.approve(vault_id, doc_ids)
            print(f"approved: {approved}")

            ingested = await vault.ingest(vault_id, target="kg")
            print(f"ingestion triggered: {ingested}")


if __name__ == "__main__":
    asyncio.run(main())
