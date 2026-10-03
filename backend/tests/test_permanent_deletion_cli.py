from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.vault_master import MemoryVaultMasterStore
from tests.test_vault_master_api import authenticate, configure


def test_permanent_deletion_routes_are_not_registered_for_authenticated_users(
    client: TestClient, tmp_path: Path
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    document = documents / "keep.pdf"
    document.write_bytes(b"keep")
    authenticate(client)
    asset_id = uuid4()
    for suffix in ("preflight", "review", "review/cancel", "confirm", "execute"):
        response = client.post(
            f"/api/vault-master/assets/{asset_id}/lifecycle/permanent-deletion-{suffix}",
            json={"reason": "test", "confirm": True, "execute": True},
        )
        assert response.status_code == 404
    assert document.read_bytes() == b"keep"
