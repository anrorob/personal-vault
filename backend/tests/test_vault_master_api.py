import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient
import pytest

from app.config import get_metadata_storage_root
import app.gallery as gallery_module
from app.gallery import get_gallery_path, scan_gallery
from app.incoming import get_incoming_path
from app.auth import get_authentication_store, require_authenticated_user
from app.auth_store import Account, MemoryAuthenticationStore
from app.main import app
from app.share_grants import SharedWithMeAsset
import app.vault_master_api as vault_master_api
from app.vault_master import (
    CataloguedAsset,
    INCOMING_SOURCE,
    INVENTORY_SOURCE,
    MemoryVaultMasterStore,
    ScannedFile,
    get_inventory_paths,
    get_vault_master_store,
    process_next_batch,
    process_next_move,
)
from app.vault_master_api import (
    get_catalogue_preview_roots,
    get_destination_paths,
    get_quarantine_root,
    to_api_asset,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
        },
    )
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())


def authenticate_regular_user(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    from app.security import hash_password

    authentication_store.create_account(
        Account(
            username="member", display_name="Member", email="member@example.test",
            password_hash=hash_password("member-password"), role="user", active=True,
            password_change_required=False, created_at=datetime.now(timezone.utc),
            last_sign_in_at=None,
        )
    )
    assert client.post(
        "/api/auth/login",
        json={"username": "member", "password": "member-password"},
    ).status_code == 200


def relationship_api_asset(
    asset_number: int,
    filename: str,
    sha256: str,
    *,
    owner: str = TEST_USERNAME,
    metadata: dict[str, object] | None = None,
) -> CataloguedAsset:
    return CataloguedAsset(
        id=UUID(int=asset_number),
        asset_type="Movies",
        display_title=Path(filename).stem,
        captured_on=None,
        location=None,
        vault_path=f"/vault/Theatre/Movies/{filename}",
        filename=filename,
        size_bytes=1_000,
        mime_type="video/x-matroska",
        sha256=sha256,
        metadata=metadata or {},
        metadata_provenance={},
        effective_metadata=metadata or {},
        owner_username=owner,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{owner}"),
    )


def test_owner_asset_api_representation_includes_origin_vault_id() -> None:
    origin_vault_id = uuid4()
    asset = CataloguedAsset(
        **{
            **relationship_api_asset(99, "provenance.mkv", "9" * 64).__dict__,
            "origin_vault_id": origin_vault_id,
        }
    )

    owner = SimpleNamespace(user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"))
    assert to_api_asset(asset, owner).origin_vault_id == origin_vault_id


def test_owner_can_analyse_relationship_candidates_without_mutation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    selected = relationship_api_asset(
        101,
        "Family Film 1080p.mkv",
        "a" * 64,
        metadata={"duration_seconds": 120.0},
    )
    probable = relationship_api_asset(
        102,
        "Family Film copy.mkv",
        "b" * 64,
        metadata={"duration_seconds": 121.0},
    )
    unrelated = relationship_api_asset(103, "Holiday.mkv", "c" * 64)
    another_owner = relationship_api_asset(
        104,
        "Family Film duplicate.mkv",
        "a" * 64,
        owner="another-owner",
    )
    for asset in (selected, probable, unrelated, another_owner):
        store.restore_catalogued_asset(asset, asset.owner_username)
    history_before = list(store.asset_history)
    authenticate(client)

    response = client.get(
        f"/api/vault-master/assets/{selected.id}/relationships/analysis"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "candidates": [
            {
                "classification": "probable_duplicate",
                "confidence": "high",
                "evidence": [
                    "Normalised filename identity matches: family film",
                    "Durations differ by only 1.000 seconds",
                    "File sizes differ by no more than 2 percent",
                ],
                "affected_files": [
                    {
                        "asset_id": str(selected.id),
                        "vault_path": selected.vault_path,
                        "filename": selected.filename,
                        "size_bytes": selected.size_bytes,
                        "mime_type": selected.mime_type,
                        "sha256": selected.sha256,
                    },
                    {
                        "asset_id": str(probable.id),
                        "vault_path": probable.vault_path,
                        "filename": probable.filename,
                        "size_bytes": probable.size_bytes,
                        "mime_type": probable.mime_type,
                        "sha256": probable.sha256,
                    },
                ],
            }
        ]
    }
    assert store.asset_history == history_before


def test_owner_can_request_relationship_review_without_file_mutation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    selected = relationship_api_asset(
        111,
        "Family Film 1080p.mkv",
        "d" * 64,
        metadata={"duration_seconds": 120.0},
    )
    candidate = relationship_api_asset(
        112,
        "Family Film copy.mkv",
        "e" * 64,
        metadata={"duration_seconds": 121.0},
    )
    for asset in (selected, candidate):
        store.restore_catalogued_asset(asset, TEST_USERNAME)
    authenticate(client)

    response = client.post(
        f"/api/vault-master/assets/{selected.id}/relationships/review",
        json={"candidate_asset_id": str(candidate.id)},
    )

    assert response.status_code == 201
    assert response.json()["action"] == "relationship_review_requested"
    selected_entry = store.list_catalogued_asset_history(selected.id)[0]
    candidate_entry = store.list_catalogued_asset_history(candidate.id)[0]
    assert selected_entry["current_values"] == {
        "candidate_asset_id": str(candidate.id),
        "classification": "probable_duplicate",
        "confidence": "high",
        "evidence": '["Normalised filename identity matches: family film", "Durations differ by only 1.000 seconds", "File sizes differ by no more than 2 percent"]',
        "state": "pending_review",
    }
    assert candidate_entry["current_values"]["candidate_asset_id"] == str(
        selected.id
    )
    assert store.get_catalogued_asset_by_id(selected.id) == selected
    assert store.get_catalogued_asset_by_id(candidate.id) == candidate

    retain_response = client.post(
        f"/api/vault-master/assets/{selected.id}/relationships/review/retain",
        json={"candidate_asset_id": str(candidate.id)},
    )

    assert retain_response.status_code == 200
    assert retain_response.json()["action"] == "relationship_review_retained"
    assert store.list_catalogued_asset_history(selected.id)[0]["current_values"] == {
        "candidate_asset_id": str(candidate.id),
        "decision": "retain_separately",
        "state": "resolved",
    }
    assert store.list_catalogued_asset_history(candidate.id)[0]["current_values"][
        "candidate_asset_id"
    ] == str(selected.id)
    assert client.post(
        f"/api/vault-master/assets/{selected.id}/relationships/review/retain",
        json={"candidate_asset_id": str(candidate.id)},
    ).status_code == 409
    assert store.get_catalogued_asset_by_id(selected.id) == selected
    assert store.get_catalogued_asset_by_id(candidate.id) == candidate


def test_owner_can_link_pending_relationship_without_merging_assets(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    selected = relationship_api_asset(121, "Family Film.mkv", "f" * 64)
    candidate = relationship_api_asset(122, "Family Film copy.mkv", "1" * 64)
    for asset in (selected, candidate):
        store.restore_catalogued_asset(asset, TEST_USERNAME)
    authenticate(client)
    request_url = f"/api/vault-master/assets/{selected.id}/relationships/review"
    body = {"candidate_asset_id": str(candidate.id)}
    assert client.post(request_url, json=body).status_code == 201
    assert client.post(request_url, json=body).status_code == 409

    response = client.post(f"{request_url}/link", json=body)

    assert response.status_code == 200
    assert response.json()["action"] == "relationship_review_linked"
    relationship = next(iter(store.asset_relationships.values()))
    assert relationship.relationship_type == "duplicate"
    assert relationship.confidence == "high"
    for visible_asset, other_asset in ((selected, candidate), (candidate, selected)):
        listing = client.get(
            f"/api/vault-master/assets/{visible_asset.id}/relationships"
        )
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "private, no-store"
        assert listing.json()["relationships"][0]["relationship_type"] == "duplicate"
        assert [
            file["asset_id"]
            for file in listing.json()["relationships"][0]["affected_files"]
        ] == [str(visible_asset.id), str(other_asset.id)]
    assert store.list_catalogued_asset_history(selected.id)[0]["current_values"] == {
        "candidate_asset_id": str(candidate.id),
        "relationship_type": "duplicate",
        "state": "resolved",
    }
    assert store.list_catalogued_asset_history(candidate.id)[0]["current_values"][
        "candidate_asset_id"
    ] == str(selected.id)
    assert client.post(f"{request_url}/link", json=body).status_code == 409
    assert store.get_catalogued_asset_by_id(selected.id) == selected
    assert store.get_catalogued_asset_by_id(candidate.id) == candidate

def configure(
    tmp_path: Path,
    store: MemoryVaultMasterStore,
) -> tuple[Path, Path]:
    incoming = tmp_path / "Incoming"
    documents = tmp_path / "Documents"
    incoming.mkdir()
    documents.mkdir()
    store._default_asset_owner = TEST_USERNAME
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_incoming_path] = lambda: incoming
    app.dependency_overrides[get_metadata_storage_root] = lambda: (
        tmp_path / "metadata"
    )
    app.dependency_overrides[get_inventory_paths] = lambda: (documents,)
    app.dependency_overrides[get_destination_paths] = lambda: {
        "Gallery": tmp_path / "Gallery",
        "Home Videos": tmp_path / "Home Videos",
        "Documents": documents,
        "Archives": tmp_path / "Archives",
    }
    app.dependency_overrides[get_catalogue_preview_roots] = lambda: {
        "/vault/Gallery": tmp_path / "Gallery",
        "/vault/Home Videos": tmp_path / "Home Videos",
        "/vault/Documents": documents,
        "/vault/Archives": tmp_path / "Archives",
    }
    for destination in app.dependency_overrides[get_destination_paths]().values():
        destination.mkdir(exist_ok=True)
    return incoming, documents


def test_vault_master_endpoints_require_authentication(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure(tmp_path, MemoryVaultMasterStore())

    assert client.get("/api/vault-master/items").status_code == 401
    assert client.get("/api/vault-master/activity").status_code == 401
    assert (
        client.post("/api/vault-master/scan/incoming").status_code
        == 401
    )
    assert (
        client.post("/api/vault-master/scan/arrival-hall").status_code
        == 401
    )
    assert (
        client.post("/api/vault-master/scan/inventory").status_code
        == 401
    )
    assert (
        client.post("/api/vault-master/catalogue/backfill").status_code
        == 401
    )
    assert (
        client.post("/api/vault-master/sidecars/reconcile").status_code
        == 401
    )
    assert (
        client.get(
            "/api/vault-master/sidecars/recovery/assessment"
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/vault-master/sidecars/recovery/"
            "00000000-0000-0000-0000-000000000000/restore",
            json={"confirm": True},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/vault-master/assets/search",
            params={"query": "photo"},
        ).status_code
        == 401
    )
    assert (
        client.patch(
            "/api/vault-master/assets/00000000-0000-0000-0000-000000000000/metadata",
            json={"display_title": "Changed"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/vault-master/assets/00000000-0000-0000-0000-000000000000/preview"
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/vault-master/assets/00000000-0000-0000-0000-000000000000/history"
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/vault-master/assets/00000000-0000-0000-0000-000000000000/"
            "lifecycle/quarantine-review",
            json={"reason": "Duplicate review"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/vault-master/assets/00000000-0000-0000-0000-000000000000/artwork/poster"
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/vault-master/assets/00000000-0000-0000-0000-000000000000/people/0000000000000000"
        ).status_code
        == 401
    )


def test_commons_gallery_preview_uses_active_authorized_canonical_asset(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    gallery_root = tmp_path / "Gallery"
    asset_id = uuid4()
    owner_user_id = uuid4()
    recipient_user_id = uuid5(NAMESPACE_URL, "personal-vault-test:member")
    image_path = gallery_root / "recipient-photo.jpg"
    image_path.write_bytes(b"authoritative-image")
    asset = CataloguedAsset(
        id=asset_id,
        asset_type="Gallery",
        display_title="Recipient photo",
        captured_on=date(2024, 2, 3),
        location=None,
        vault_path="/vault/Gallery/recipient-photo.jpg",
        filename="recipient-photo.jpg",
        size_bytes=image_path.stat().st_size,
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={},
        effective_metadata={},
        owner_username="recipient",
        owner_user_id=owner_user_id,
    )
    store.restore_catalogued_asset(asset, "recipient")
    active = {"value": True}

    class FakeShareGrantStore:
        def __init__(self, _conninfo: str) -> None:
            pass

        def list_assets_shared_with_user(
            self, user_id: UUID, asset_types: tuple[str, ...] = ()
        ) -> list[SharedWithMeAsset]:
            if user_id != recipient_user_id or not active["value"]:
                return []
            if asset_types and "gallery" not in asset_types:
                return []
            return [
                SharedWithMeAsset(
                    asset_id=asset.id,
                    asset_type=asset.asset_type,
                    display_title=asset.display_title,
                    captured_on=asset.captured_on,
                    owner_user_id=owner_user_id,
                    owner_display_name="Recipient",
                    origin_vault_id=uuid4(),
                )
            ]

    monkeypatch.setattr(vault_master_api, "PostgresShareGrantStore", FakeShareGrantStore)
    monkeypatch.setattr(vault_master_api, "get_database_conninfo", lambda: "test")
    authenticate_regular_user(client, authentication_store)
    recipient = authentication_store.get_account("member")
    assert recipient is not None
    recipient_user_id = recipient.user_id
    store.catalogued_assets[asset.vault_path] = replace(
        asset, shared_with_user_ids=(recipient_user_id,)
    )

    listing = client.get("/api/vault-master/commons/shared-with-me?category=gallery")

    assert listing.status_code == 200
    assert listing.json()["assets"] == [
        {
            "asset_id": str(asset_id),
            "asset_type": "Gallery",
            "display_title": "Recipient photo",
            "captured_on": "2024-02-03",
            "owner_display_name": "Recipient",
            "preview_url": f"/api/vault-master/commons/shared-with-me/{asset_id}/preview",
        }
    ]
    preview = client.get(listing.json()["assets"][0]["preview_url"])
    assert preview.status_code == 200
    assert preview.content == b"authoritative-image"
    assert preview.headers["cache-control"] == "private, no-store"

    active["value"] = False
    assert client.get(f"/api/vault-master/commons/shared-with-me/{asset_id}/preview").status_code == 404


def test_concurrent_gallery_and_commons_previews_are_deterministic_and_fail_closed(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both preview authority modes stay deterministic under concurrent load.

    The request-time share evaluator is deliberately a no-DDL fake here; the
    PostgreSQL companion test proves construction itself cannot invoke DDL.
    """
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    gallery_root = tmp_path / "Gallery"
    image_path = gallery_root / "recipient-photo.jpg"
    image_bytes = b"\xff\xd8authoritative-image\xff\xd9"
    image_path.write_bytes(image_bytes)
    asset_id = uuid4()
    owner_user_id = uuid4()
    recipient_user_id = uuid5(NAMESPACE_URL, "personal-vault-test:member")
    asset = CataloguedAsset(
        id=asset_id,
        asset_type="Gallery",
        display_title="Recipient photo",
        captured_on=date(2024, 2, 3),
        location=None,
        vault_path="/vault/Gallery/recipient-photo.jpg",
        filename="recipient-photo.jpg",
        size_bytes=len(image_bytes),
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={},
        effective_metadata={},
        owner_username="recipient",
        owner_user_id=owner_user_id,
        visibility="shared",
        shared_with=("member",),
        shared_with_user_ids=(recipient_user_id,),
    )
    store.restore_catalogued_asset(asset, "recipient")
    active = {"value": True}

    class FakeShareGrantStore:
        def __init__(self, _conninfo: str) -> None:
            pass

        def included_gallery_assets(self, user_id: UUID) -> dict[UUID, str]:
            return {asset.id: "Recipient"} if active["value"] and user_id == recipient_user_id else {}

        def list_assets_shared_with_user(
            self, user_id: UUID, asset_types: tuple[str, ...] = ()
        ) -> list[SharedWithMeAsset]:
            if user_id != recipient_user_id or not active["value"]:
                return []
            if asset_types and "gallery" not in asset_types:
                return []
            return [
                SharedWithMeAsset(
                    asset_id=asset.id,
                    asset_type=asset.asset_type,
                    display_title=asset.display_title,
                    captured_on=asset.captured_on,
                    owner_user_id=owner_user_id,
                    owner_display_name="Recipient",
                    origin_vault_id=uuid4(),
                )
            ]

    monkeypatch.setattr(gallery_module, "PostgresShareGrantStore", FakeShareGrantStore)
    monkeypatch.setattr(gallery_module, "get_database_conninfo", lambda: "test")
    monkeypatch.setattr(vault_master_api, "PostgresShareGrantStore", FakeShareGrantStore)
    monkeypatch.setattr(vault_master_api, "get_database_conninfo", lambda: "test")
    app.dependency_overrides[get_gallery_path] = lambda: gallery_root
    authenticate_regular_user(client, authentication_store)
    recipient = authentication_store.get_account("member")
    assert recipient is not None
    recipient_user_id = recipient.user_id
    store.catalogued_assets[asset.vault_path] = replace(
        asset, shared_with_user_ids=(recipient_user_id,)
    )
    gallery_id = scan_gallery(gallery_root)[0].id
    gallery_url = f"/api/gallery/{gallery_id}/preview"
    commons_url = f"/api/vault-master/commons/shared-with-me/{asset_id}/preview"

    def fetch(url: str) -> tuple[int, str, bytes]:
        response = client.get(url)
        return response.status_code, response.headers["content-type"], response.content

    with ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(fetch, [gallery_url, commons_url] * 12))

    assert responses == [(200, "image/jpeg", image_bytes)] * 24

    active["value"] = False
    assert client.get(gallery_url).status_code == 404
    assert client.get(commons_url).status_code == 404


def test_shared_asset_search_exposes_only_basic_published_metadata(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    document = documents / "family-record.pdf"
    document.write_bytes(b"family-record")
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset(
        "/vault/Documents/family-record.pdf"
    )
    assert asset is not None
    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "display_title": "Family record",
            "captured_on": date(2024, 1, 2),
            "location": "London",
            "visibility": "shared",
            "shared_with": ("son",),
            "shared_with_user_ids": (uuid5(NAMESPACE_URL, "personal-vault-test:son"),),
        }
    )
    app.dependency_overrides[require_authenticated_user] = lambda: SimpleNamespace(
        username="son", user_id=uuid5(NAMESPACE_URL, "personal-vault-test:son")
    )

    response = client.get(
        "/api/vault-master/assets/search",
        params={"query": "family"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "assets": [
            {
                "id": str(asset.id),
                "asset_type": "Documents",
                "display_title": "Family record",
                "captured_on": "2024-01-02",
                "location": "London",
            }
        ]
    }


def test_owner_can_set_asset_sharing_policy_and_shared_user_cannot_change_it(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    document = documents / "family-record.pdf"
    document.write_bytes(b"family-record")
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    authenticate(client)
    authentication_store.create_account(
        Account(
            username="son", display_name="Son", email=None, password_hash="test-hash",
            role="user", active=True, password_change_required=False,
            created_at=datetime.now(timezone.utc), last_sign_in_at=None,
        )
    )
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset(
        "/vault/Documents/family-record.pdf"
    )
    assert asset is not None

    response = client.patch(
        f"/api/vault-master/assets/{asset.id}/access",
        json={"visibility": "shared", "shared_with": ["son"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "owner_username": TEST_USERNAME,
        "visibility": "shared",
        "shared_with": ["son"],
    }
    assert store.get_catalogued_asset(asset.vault_path).shared_with == ("son",)  # type: ignore[union-attr]
    assert store.list_catalogued_asset_history(asset.id)[0]["action"] == (
        "access_policy_updated"
    )

    app.dependency_overrides[require_authenticated_user] = lambda: "son"
    response = client.patch(
        f"/api/vault-master/assets/{asset.id}/metadata",
        json={"display_title": "Unauthorised change"},
    )

    assert response.status_code == 404
    current = store.get_catalogued_asset(asset.vault_path)
    assert current is not None
    assert current.display_title != "Unauthorised change"

    response = client.patch(
        f"/api/vault-master/assets/{asset.id}/access",
        json={"visibility": "private"},
    )

    assert response.status_code == 404


def test_bulk_quick_share_is_atomic_and_legacy_single_asset_sharing_is_unchanged(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    authenticate(client)
    recipient = Account(
        username="son", display_name="Son", email=None, password_hash="test-hash",
        role="user", active=True, password_change_required=False,
        created_at=datetime.now(timezone.utc), last_sign_in_at=None,
    )
    authentication_store.create_account(recipient)
    recipient = authentication_store.get_account("son")
    assert recipient is not None
    first = relationship_api_asset(701, "first.mkv", "1" * 64)
    second = relationship_api_asset(702, "second.mkv", "2" * 64)
    foreign = relationship_api_asset(703, "foreign.mkv", "3" * 64, owner="member")
    for asset in (first, second, foreign):
        store.restore_catalogued_asset(asset, asset.owner_username)

    shared = client.put(
        "/api/vault-master/assets/sharing/bulk",
        json={"asset_ids": [str(first.id), str(second.id)], "mode": "specific",
              "recipient_user_ids": [str(recipient.user_id)], "share_mode": "quick"},
    )
    assert shared.status_code == 200
    assert shared.json() == {"asset_ids": [str(first.id), str(second.id)]}
    assert store.get_catalogued_asset_by_id(first.id).shared_with == ("son",)  # type: ignore[union-attr]
    assert store.get_catalogued_asset_by_id(second.id).shared_with == ("son",)  # type: ignore[union-attr]

    before_history = list(store.asset_history)
    invalid = client.put(
        "/api/vault-master/assets/sharing/bulk",
        json={"asset_ids": [str(first.id), str(uuid4())], "mode": "private"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["asset_id"]
    mixed_owner = client.put(
        "/api/vault-master/assets/sharing/bulk",
        json={"asset_ids": [str(first.id), str(foreign.id)], "mode": "private"},
    )
    assert mixed_owner.status_code == 422
    assert mixed_owner.json()["detail"]["asset_id"] == str(foreign.id)
    assert store.asset_history == before_history
    assert store.get_catalogued_asset_by_id(first.id).shared_with == ("son",)  # type: ignore[union-attr]

    # The established one-asset endpoint remains independent and compatible.
    import app.vault_master_api as vault_master_api_module
    monkeypatch.setattr(vault_master_api_module, "_local_sharing_state", lambda *_args: ("private", []))
    legacy = client.put(
        f"/api/vault-master/assets/{first.id}/sharing",
        json={"mode": "private", "recipient_user_ids": [], "share_mode": "quick"},
    )
    assert legacy.status_code == 200
    assert legacy.json()["mode"] == "private"
    assert store.get_catalogued_asset_by_id(first.id).shared_with == ()  # type: ignore[union-attr]


def test_owner_can_request_non_destructive_quarantine_review(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    document = documents / "duplicate-record.pdf"
    document.write_bytes(b"duplicate-record")
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset(
        "/vault/Documents/duplicate-record.pdf"
    )
    assert asset is not None

    response = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review",
        json={"reason": "Keep one of two identical records"},
    )

    assert response.status_code == 201
    assert response.json()["action"] == "quarantine_review_requested"
    assert response.json()["current_values"] == {
        "reason": "Keep one of two identical records",
        "state": "pending_review",
    }
    # This first lifecycle checkpoint is deliberately non-destructive.
    assert document.read_bytes() == b"duplicate-record"
    assert store.get_catalogued_asset(asset.vault_path) == asset
    assert store.list_catalogued_asset_history(asset.id)[0]["action"] == (
        "quarantine_review_requested"
    )

    withdrawn_response = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review/cancel",
    )
    assert withdrawn_response.status_code == 201
    assert withdrawn_response.json()["action"] == "quarantine_review_cancelled"
    assert withdrawn_response.json()["current_values"] == {"state": "cancelled"}
    assert document.read_bytes() == b"duplicate-record"
    assert store.get_catalogued_asset(asset.vault_path) == asset
    assert [entry["action"] for entry in store.list_catalogued_asset_history(asset.id)[:2]] == [
        "quarantine_review_cancelled",
        "quarantine_review_requested",
    ]

    repeated_withdrawal = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review/cancel",
    )
    assert repeated_withdrawal.status_code == 409

    app.dependency_overrides[require_authenticated_user] = lambda: "son"
    hidden_response = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review",
        json={"reason": "Not allowed"},
    )
    assert hidden_response.status_code == 404
    assert len(store.list_catalogued_asset_history(asset.id)) == 2


def test_owner_can_preflight_recoverable_quarantine_without_mutation(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    document = documents / "duplicate-record.pdf"
    document.write_bytes(b"duplicate-record")
    quarantine = tmp_path / "Quarantine"
    quarantine.mkdir()
    app.dependency_overrides[get_quarantine_root] = lambda: quarantine
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset(
        "/vault/Documents/duplicate-record.pdf"
    )
    assert asset is not None

    response = client.get(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-preflight"
    )

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "source_path": "/vault/Documents/duplicate-record.pdf",
        "proposed_quarantine_path": (
            "/vault/Quarantine/Documents/duplicate-record.pdf"
        ),
        "checksum_verified": True,
        "reason": None,
    }
    # This preflight deliberately leaves the source, destination and history
    # untouched; a future owner-confirmed execution has to revalidate it all.
    assert document.read_bytes() == b"duplicate-record"
    assert not (quarantine / "Documents" / document.name).exists()
    assert store.list_catalogued_asset_history(asset.id) == []

    document.write_bytes(b"changed-after-cataloguing")
    failed_response = client.get(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-preflight"
    )

    assert failed_response.status_code == 200
    assert failed_response.json() == {
        "ready": False,
        "source_path": "/vault/Documents/duplicate-record.pdf",
        "proposed_quarantine_path": None,
        "checksum_verified": False,
        "reason": "The file checksum no longer matches the catalogue",
    }


def test_owner_can_confirm_verified_recoverable_quarantine(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    document = documents / "duplicate-record.pdf"
    document.write_bytes(b"duplicate-record")
    quarantine = tmp_path / "Quarantine"
    quarantine.mkdir()
    app.dependency_overrides[get_quarantine_root] = lambda: quarantine
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/duplicate-record.pdf")
    assert asset is not None
    assert client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review",
        json={"reason": "Keep one verified copy"},
    ).status_code == 201

    response = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-confirm",
        json={"confirm": True},
    )

    quarantine_file = quarantine / "Documents" / document.name
    assert response.status_code == 201
    assert response.json()["vault_path"] == (
        "/vault/Quarantine/Documents/duplicate-record.pdf"
    )
    assert not document.exists()
    assert quarantine_file.read_bytes() == b"duplicate-record"
    assert store.get_catalogued_asset(asset.vault_path) is None
    assert store.get_catalogued_asset(
        "/vault/Quarantine/Documents/duplicate-record.pdf"
    ) is not None
    assert [entry["action"] for entry in store.list_catalogued_asset_history(asset.id)[:2]] == [
        "quarantined",
        "quarantine_review_requested",
    ]


def test_owner_can_move_only_to_an_existing_folder_with_checksum_verification(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    destination_folder = documents / "Receipts"
    destination_folder.mkdir()
    document = documents / "receipt.pdf"
    document.write_bytes(b"verified-document")
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/receipt.pdf")
    assert asset is not None

    listing = client.get("/api/vault-master/lifecycle/move-destinations?category=Documents")
    assert listing.status_code == 200
    assert listing.json() == {"destinations": ["/vault/Documents", "/vault/Documents/Receipts"]}

    preflight = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/move-preflight",
        json={"category": "Documents", "destination_folder": "Receipts"},
    )
    assert preflight.status_code == 200
    assert preflight.json()["ready"] is True
    assert preflight.json()["destination_path"] == "/vault/Documents/Receipts/receipt.pdf"
    assert document.exists()

    confirmed = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/move-confirm",
        json={"category": "Documents", "destination_folder": "Receipts", "confirm": True},
    )
    assert confirmed.status_code == 200
    assert not document.exists()
    assert (destination_folder / "receipt.pdf").read_bytes() == b"verified-document"
    assert store.get_catalogued_asset("/vault/Documents/Receipts/receipt.pdf") is not None
    assert store.list_catalogued_asset_history(asset.id)[0]["action"] == "moved_to_folder"


def test_folder_move_refuses_missing_folders_traversal_and_collisions(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    destination_folder = documents / "Receipts"
    destination_folder.mkdir()
    document = documents / "receipt.pdf"
    document.write_bytes(b"original")
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/receipt.pdf")
    assert asset is not None

    for destination in ("Missing", "../Archives"):
        response = client.post(
            f"/api/vault-master/assets/{asset.id}/lifecycle/move-confirm",
            json={"category": "Documents", "destination_folder": destination, "confirm": True},
        )
        assert response.status_code in {409, 422}
        assert document.read_bytes() == b"original"

    (destination_folder / document.name).write_bytes(b"existing")
    response = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/move-confirm",
        json={"category": "Documents", "destination_folder": "Receipts", "confirm": True},
    )
    assert response.status_code == 409
    assert document.read_bytes() == b"original"
    assert (destination_folder / document.name).read_bytes() == b"existing"


def test_bin_restore_returns_only_to_the_recorded_original_path(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    document = documents / "recoverable.pdf"
    document.write_bytes(b"recoverable")
    quarantine = tmp_path / "Quarantine"
    quarantine.mkdir()
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    app.dependency_overrides[get_quarantine_root] = lambda: quarantine
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/recoverable.pdf")
    assert asset is not None
    assert client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review", json={"reason": "Recoverable Bin test"}).status_code == 201
    assert client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-confirm", json={"confirm": True}).status_code == 201

    preflight = client.get(f"/api/vault-master/assets/{asset.id}/lifecycle/bin-restore-preflight")
    assert preflight.status_code == 200
    assert preflight.json()["destination_path"] == "/vault/Documents/recoverable.pdf"
    restored = client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/bin-restore-confirm", json={"confirm": True})
    assert restored.status_code == 200
    assert document.read_bytes() == b"recoverable"
    assert not (quarantine / "Documents" / document.name).exists()
    assert store.list_catalogued_asset_history(asset.id)[0]["action"] == "restored_from_bin"


def test_owner_can_preflight_direct_permanent_deletion_and_preserves_quarantine_retention(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    document = documents / "duplicate-record.pdf"
    document.write_bytes(b"duplicate-record")
    quarantine = tmp_path / "Quarantine"
    quarantine.mkdir()
    app.dependency_overrides[get_quarantine_root] = lambda: quarantine
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/duplicate-record.pdf")
    assert asset is not None

    before_response = client.get(
        f"/api/vault-master/assets/{asset.id}/lifecycle/permanent-deletion-preflight"
    )

    assert before_response.status_code == 200
    assert before_response.headers["cache-control"] == "private, no-store"
    before = before_response.json()
    assert before["ready"] is True
    assert before["source_path"] == "/vault/Documents/duplicate-record.pdf"
    assert before["proposed_permanent_deletion_path"] == "/vault/Documents/duplicate-record.pdf"
    assert before["checksum_verified"] is True
    assert before["quarantined_at"] is None
    assert before["eligible_at"] is not None
    assert before["reason"] is None
    assert client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review",
        json={"reason": "Keep one verified copy"},
    ).status_code == 201
    assert client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-confirm",
        json={"confirm": True},
    ).status_code == 201
    quarantined = store.get_catalogued_asset(
        "/vault/Quarantine/Documents/duplicate-record.pdf"
    )
    assert quarantined is not None

    retained_response = client.get(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-preflight"
    )

    quarantine_entry = store.list_catalogued_asset_history(quarantined.id)[0]
    quarantined_at = quarantine_entry["created_at"]
    assert isinstance(quarantined_at, datetime)
    assert retained_response.status_code == 200
    assert retained_response.json() == {
        "ready": False,
        "source_path": "/vault/Quarantine/Documents/duplicate-record.pdf",
        "proposed_permanent_deletion_path": None,
        "checksum_verified": False,
        "quarantined_at": quarantined_at.isoformat().replace("+00:00", "Z"),
        "eligible_at": (quarantined_at + timedelta(days=30))
        .isoformat()
        .replace("+00:00", "Z"),
        "reason": "The 30-day Quarantine retention period has not elapsed",
    }
    retained_request = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-review",
        json={"reason": "A verified duplicate is retained elsewhere"},
    )
    assert retained_request.status_code == 409
    assert retained_request.json()["detail"] == (
        "The 30-day Quarantine retention period has not elapsed"
    )
    quarantine_entry["created_at"] = datetime.now(timezone.utc) - timedelta(days=31)
    store.asset_history[-1]["created_at"] = quarantine_entry["created_at"]

    ready_response = client.get(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-preflight"
    )

    assert ready_response.status_code == 200
    assert ready_response.json() == {
        "ready": True,
        "source_path": "/vault/Quarantine/Documents/duplicate-record.pdf",
        "proposed_permanent_deletion_path": (
            "/vault/Quarantine/Documents/duplicate-record.pdf"
        ),
        "checksum_verified": True,
        "quarantined_at": quarantine_entry["created_at"]
        .isoformat()
        .replace("+00:00", "Z"),
        "eligible_at": (quarantine_entry["created_at"] + timedelta(days=30))
        .isoformat()
        .replace("+00:00", "Z"),
        "reason": None,
    }
    request_response = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-review",
        json={"reason": "  A verified duplicate is retained elsewhere  "},
    )
    assert request_response.status_code == 201
    assert request_response.json()["action"] == "permanent_deletion_review_requested"
    assert request_response.json()["previous_values"] == {"state": "quarantined"}
    assert request_response.json()["current_values"] == {
        "reason": "A verified duplicate is retained elsewhere",
        "state": "pending_permanent_deletion_review",
        "eligible_at": ready_response.json()["eligible_at"].replace("Z", "+00:00"),
    }
    cancel_response = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-review/cancel"
    )
    assert cancel_response.status_code == 201
    assert cancel_response.json()["action"] == "permanent_deletion_review_cancelled"
    assert cancel_response.json()["previous_values"] == {
        "state": "pending_permanent_deletion_review"
    }
    assert cancel_response.json()["current_values"] == {"state": "quarantined"}
    repeated_cancel = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-review/cancel"
    )
    assert repeated_cancel.status_code == 409
    assert repeated_cancel.json()["detail"] == (
        "No pending permanent-deletion review can be withdrawn"
    )
    assert client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-review",
        json={"reason": "A verified duplicate is retained elsewhere"},
    ).status_code == 201
    rejected_confirmation = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-confirm",
        json={"confirm": False},
    )
    assert rejected_confirmation.status_code == 422
    confirmation_response = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-confirm",
        json={"confirm": True},
    )
    assert confirmation_response.status_code == 201
    assert confirmation_response.json()["action"] == "permanent_deletion_confirmed"
    assert confirmation_response.json()["previous_values"] == {
        "state": "pending_permanent_deletion_review"
    }
    assert confirmation_response.json()["current_values"] == {
        "state": "approved_for_permanent_deletion",
        "checksum": quarantined.sha256,
    }
    repeated_confirmation = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-confirm",
        json={"confirm": True},
    )
    assert repeated_confirmation.status_code == 409
    cancel_after_confirmation = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-review/cancel"
    )
    assert cancel_after_confirmation.status_code == 409
    assert (quarantine / "Documents" / document.name).read_bytes() == b"duplicate-record"
    assert store.get_catalogued_asset(quarantined.vault_path) == quarantined
    assert "quarantined" in {
        entry["action"] for entry in store.list_catalogued_asset_history(quarantined.id)
    }

    sidecar = tmp_path / "metadata" / "sidecars" / f"{quarantined.id}.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("retained canonical metadata", encoding="utf-8")

    rejected_execution = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-execute",
        json={"execute": False},
    )
    assert rejected_execution.status_code == 422
    record_deletion = store.record_catalogued_asset_permanent_deletion
    monkeypatch.setattr(
        store,
        "record_catalogued_asset_permanent_deletion",
        lambda *_args: None,
    )
    stale_execution = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-execute",
        json={"execute": True},
    )
    assert stale_execution.status_code == 409
    assert (quarantine / "Documents" / document.name).read_bytes() == b"duplicate-record"
    monkeypatch.setattr(
        store,
        "record_catalogued_asset_permanent_deletion",
        record_deletion,
    )
    deletion_response = client.post(
        f"/api/vault-master/assets/{quarantined.id}/lifecycle/permanent-deletion-execute",
        json={"execute": True},
    )

    assert deletion_response.status_code == 201
    assert deletion_response.json()["action"] == "permanently_deleted"
    assert store.get_catalogued_asset(quarantined.vault_path) is None
    assert store.deleted_assets[quarantined.id]["vault_path"] == quarantined.vault_path
    assert not (quarantine / "Documents" / document.name).exists()
    assert not sidecar.exists()
    assert not (
        quarantine
        / "Documents"
        / f".vault-master-delete-{quarantined.id}.pending"
    ).exists()


def test_owner_can_explicitly_purge_an_active_asset_without_quarantine(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    document = documents / "purge-me.pdf"
    document.write_bytes(b"purge-me")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/purge-me.pdf")
    assert asset is not None

    assert client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/permanent-deletion-review",
        json={"reason": "Reclaim storage"},
    ).status_code == 201
    assert client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/permanent-deletion-confirm",
        json={"confirm": True},
    ).status_code == 201
    deleted = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/permanent-deletion-execute",
        json={"execute": True},
    )

    assert deleted.status_code == 201
    assert store.get_catalogued_asset(asset.vault_path) is None
    assert store.deleted_assets[asset.id]["sha256"] == asset.sha256
    assert not document.exists()


def test_confirm_quarantine_never_overwrites_an_existing_target(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    document = documents / "duplicate-record.pdf"
    document.write_bytes(b"original")
    quarantine = tmp_path / "Quarantine"
    existing = quarantine / "Documents" / document.name
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing quarantine file")
    app.dependency_overrides[get_quarantine_root] = lambda: quarantine
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/duplicate-record.pdf")
    assert asset is not None
    assert client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-review",
        json={"reason": "Review duplicate"},
    ).status_code == 201

    response = client.post(
        f"/api/vault-master/assets/{asset.id}/lifecycle/quarantine-confirm",
        json={"confirm": True},
    )

    assert response.status_code == 409
    assert document.read_bytes() == b"original"
    assert existing.read_bytes() == b"existing quarantine file"
    assert store.get_catalogued_asset(asset.vault_path) == asset
    assert [entry["action"] for entry in store.list_catalogued_asset_history(asset.id)] == [
        "quarantine_review_requested"
    ]


def test_asset_sharing_policy_rejects_empty_or_owner_scope(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    document = documents / "family-record.pdf"
    document.write_bytes(b"family-record")
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset(
        "/vault/Documents/family-record.pdf"
    )
    assert asset is not None

    assert client.patch(
        f"/api/vault-master/assets/{asset.id}/access",
        json={"visibility": "shared"},
    ).status_code == 422
    assert client.patch(
        f"/api/vault-master/assets/{asset.id}/access",
        json={
            "visibility": "shared",
            "shared_with": [TEST_USERNAME],
        },
    ).status_code == 422


def test_lists_authenticated_activity_with_validation_and_no_store(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "photo.jpg").write_bytes(b"photo")
    authenticate(client)
    scan_response = client.post("/api/vault-master/scan/arrival-hall")
    assert scan_response.status_code == 200
    assert process_next_batch(store) is not None
    item = store.list_items()[0]
    approve_response = client.post(
        f"/api/vault-master/items/{item.id}/approve"
    )
    assert approve_response.status_code == 200

    response = client.get(
        "/api/vault-master/activity",
        params={"limit": 2},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    events = response.json()["events"]
    assert len(events) == 2
    assert events[0]["action"] == "proposal_approved"
    assert events[0]["username"] == TEST_USERNAME
    assert events[0]["filename"] == "photo.jpg"
    assert events[0]["source_kind"] == "incoming"
    assert events[0]["succeeded"] is True
    assert events[1]["action"] == "scan_completed"
    assert client.get(
        "/api/vault-master/activity",
        params={"limit": 0},
    ).status_code == 422
    assert client.get(
        "/api/vault-master/activity",
        params={"limit": 201},
    ).status_code == 422


def test_reconciles_sidecars_for_authenticated_user(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure(tmp_path, MemoryVaultMasterStore())
    authenticate(client)

    response = client.post("/api/vault-master/sidecars/reconcile")

    assert response.status_code == 200
    assert response.json() == {
        "checked": 0,
        "current": 0,
        "repaired": 0,
        "failed": 0,
    }


def test_global_vault_master_controls_require_administrator_role(
    client: TestClient, tmp_path: Path, authentication_store: MemoryAuthenticationStore,
) -> None:
    configure(tmp_path, MemoryVaultMasterStore())
    authenticate_regular_user(client, authentication_store)

    assert client.post("/api/vault-master/scan/incoming").status_code == 403
    assert client.get("/api/vault-master/jobs").status_code == 403
    assert client.post("/api/vault-master/sidecars/reconcile").status_code == 403


def test_assesses_sidecar_recovery_without_mutating_storage(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure(tmp_path, MemoryVaultMasterStore())
    sidecar_root = tmp_path / "metadata" / "sidecars"
    sidecar_root.mkdir(parents=True)
    invalid = sidecar_root / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json"
    invalid.write_text("{invalid", encoding="utf-8")
    authenticate(client)

    response = client.get(
        "/api/vault-master/sidecars/recovery/assessment"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "discovered": 1,
        "valid": 0,
        "invalid": 1,
            "unsupported": 0,
            "current": 0,
            "hidden": 0,
            "recoverable": 0,
            "intentionally_deleted": 0,
            "media_missing": 0,
            "restorable": 0,
        "conflicting": 0,
        "path_conflicts": 0,
        "candidates": [
            {
                "sidecar_name": invalid.name,
                "status": "invalid",
                "detail": "Sidecar is not valid UTF-8 JSON",
                "asset_id": None,
                "display_title": None,
                "vault_path": None,
                "filename": None,
            }
        ],
    }
    assert invalid.read_text(encoding="utf-8") == "{invalid"


def test_restores_only_a_verified_permanent_asset_from_sidecar(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = MemoryVaultMasterStore(tmp_path / "metadata")
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    permanent_file = documents / "record.pdf"
    permanent_file.write_bytes(b"permanent-record")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/record.pdf")
    assert asset is not None
    del store.catalogued_assets[asset.vault_path]

    response = client.post(
        f"/api/vault-master/sidecars/recovery/{asset.id}/restore",
        json={"confirm": True},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(asset.id)
    assert response.json()["vault_path"] == asset.vault_path
    restored = store.get_catalogued_asset(asset.vault_path)
    assert restored is not None
    assert restored.detected_metadata == asset.detected_metadata
    assert restored.imported_metadata == asset.imported_metadata
    assert restored.user_overrides == asset.user_overrides
    assert restored.effective_metadata == asset.effective_metadata
    event = store.list_activity(limit=1)[0]
    assert event.action == "sidecar_restored"
    assert event.username == TEST_USERNAME
    assert event.succeeded is True


def test_sidecar_restore_refuses_changed_or_already_catalogued_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = MemoryVaultMasterStore(tmp_path / "metadata")
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    permanent_file = documents / "record.pdf"
    permanent_file.write_bytes(b"original")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/record.pdf")
    assert asset is not None

    existing = client.post(
        f"/api/vault-master/sidecars/recovery/{asset.id}/restore",
        json={"confirm": True},
    )
    assert existing.status_code == 409
    assert "already exists" in existing.json()["detail"]

    del store.catalogued_assets[asset.vault_path]
    permanent_file.write_bytes(b"changed")
    changed = client.post(
        f"/api/vault-master/sidecars/recovery/{asset.id}/restore",
        json={"confirm": True},
    )
    assert changed.status_code == 409
    assert "size does not match" in changed.json()["detail"]
    assert store.get_catalogued_asset(asset.vault_path) is None
    event = store.list_activity(limit=1)[0]
    assert event.action == "sidecar_restore_failed"
    assert event.username == TEST_USERNAME
    assert event.succeeded is False


def test_sidecar_restore_requires_explicit_confirmation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure(tmp_path, MemoryVaultMasterStore(tmp_path / "metadata"))
    authenticate(client)

    response = client.post(
        "/api/vault-master/sidecars/recovery/"
        "00000000-0000-0000-0000-000000000000/restore",
        json={"confirm": False},
    )

    assert response.status_code == 422


def test_activity_consolidates_inventory_files_into_scan_summary(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    authenticate(client)
    for filename in ("one.pdf", "two.pdf", "three.pdf"):
        (documents / filename).write_bytes(filename.encode())

    client.post("/api/vault-master/scan/inventory")
    while process_next_batch(store) is not None:
        pass

    stored_actions = [event.action for event in store.list_activity()]
    response = client.get("/api/vault-master/activity")
    timeline_events = response.json()["events"]

    assert stored_actions.count("file_inventoried") == 3
    assert response.status_code == 200
    assert [event["action"] for event in timeline_events] == [
        "scan_completed"
    ]
    assert timeline_events[0]["source_kind"] == "inventory"
    assert timeline_events[0]["detail"] == "3 file(s) analysed"


def test_activity_hides_routine_empty_arrival_hall_scans(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    authenticate(client)

    client.post("/api/vault-master/scan/arrival-hall")
    assert process_next_batch(store) is not None

    stored_actions = [event.action for event in store.list_activity()]
    response = client.get("/api/vault-master/activity")

    assert "scan_completed" in stored_actions
    assert response.status_code == 200
    assert response.json()["events"] == []


def test_activity_consolidates_arrival_hall_analysis_into_scan_summary(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "track.wma").write_bytes(b"audio")
    authenticate(client)

    client.post("/api/vault-master/scan/arrival-hall")
    assert process_next_batch(store) is not None

    stored_actions = [event.action for event in store.list_activity()]
    response = client.get("/api/vault-master/activity")

    assert "file_analysed" in stored_actions
    assert response.status_code == 200
    assert [event["action"] for event in response.json()["events"]] == [
        "scan_completed"
    ]


def test_searches_permanent_catalogue_by_title_filename_path_and_location(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    (documents / "Family_Record.pdf").write_bytes(b"family-record")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset(
        "/vault/Documents/Family_Record.pdf"
    )
    assert asset is not None
    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "display_title": "Household accounts",
            "location": "London",
        }
    )

    for query in (
        "household",
        "family_record",
        "/vault/documents",
        "london",
    ):
        response = client.get(
            "/api/vault-master/assets/search",
            params={"query": query},
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert [item["id"] for item in response.json()["assets"]] == [
            str(asset.id)
        ]

    assert client.get(
        "/api/vault-master/assets/search",
        params={"query": "missing"},
    ).json() == {"assets": []}

    update_response = client.patch(
        f"/api/vault-master/assets/{asset.id}/metadata",
        json={
            "display_title": "Corrected household record",
            "captured_on": "1995-09-03",
            "captured_at": "1995-09-03T10:11:12+00:00",
            "location": "Gdansk",
        },
    )
    updated = update_response.json()

    assert update_response.status_code == 200
    assert updated["display_title"] == "Corrected household record"
    assert updated["captured_on"] == "1995-09-03"
    assert updated["location"] == "Gdansk"
    assert updated["sha256"] == asset.sha256
    assert updated["metadata_provenance"] == {
        "display_title": "user_override",
        "captured_on": "user_override",
        "captured_at": "user_override",
        "location": "user_override",
    }
    assert store.asset_history[-1]["username"] == TEST_USERNAME
    assert store.asset_history[-1]["previous_values"]["display_title"] == (
        "Household accounts"
    )
    history_response = client.get(
        f"/api/vault-master/assets/{asset.id}/history"
    )
    history = history_response.json()["entries"]
    assert history_response.status_code == 200
    assert history_response.headers["cache-control"] == "private, no-store"
    assert len(history) == 1
    assert history[0]["username"] == TEST_USERNAME
    assert history[0]["previous_values"]["location"] == "London"
    assert history[0]["current_values"]["location"] == "Gdansk"

    preview_response = client.get(
        f"/api/vault-master/assets/{asset.id}/preview"
    )
    assert preview_response.status_code == 200
    assert preview_response.content == b"family-record"
    assert preview_response.headers["content-type"] == "application/pdf"
    assert preview_response.headers["cache-control"] == "private, no-store"

    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "vault_path": "/vault/Documents/../../outside.pdf",
        }
    )
    assert (
        client.get(
            f"/api/vault-master/assets/{asset.id}/preview"
        ).status_code
        == 404
    )


def test_serves_only_catalogue_linked_owned_artwork(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    (documents / "movie.pdf").write_bytes(b"movie")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/movie.pdf")
    assert asset is not None

    metadata_root = tmp_path / "metadata"
    artwork_path = metadata_root / "artwork" / str(asset.id) / "poster"
    artwork_path.parent.mkdir(parents=True)
    artwork_path.write_bytes(b"owned-poster")
    app.dependency_overrides[get_metadata_storage_root] = (
        lambda: metadata_root
    )
    owned_record = {
        "storage_key": f"artwork/{asset.id}/poster",
        "mime_type": "image/jpeg",
        "size_bytes": 12,
    }
    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "imported_metadata": {
                "artwork": {"owned": {"poster": owned_record}}
            },
        }
    )

    response = client.get(
        f"/api/vault-master/assets/{asset.id}/artwork/poster"
    )

    assert response.status_code == 200
    assert response.content == b"owned-poster"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    primary_path = metadata_root / "artwork" / str(asset.id) / "primary"
    primary_path.write_bytes(b"owned-album-cover")
    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "imported_metadata": {
                "artwork": {
                    "owned": {
                        "primary": {
                            "storage_key": f"artwork/{asset.id}/primary",
                            "mime_type": "image/jpeg",
                            "size_bytes": 17,
                        }
                    }
                }
            },
        }
    )
    primary = client.get(
        f"/api/vault-master/assets/{asset.id}/artwork/primary"
    )
    assert primary.status_code == 200
    assert primary.content == b"owned-album-cover"
    assert (
        client.get(
            f"/api/vault-master/assets/{asset.id}/artwork/backdrop"
        ).status_code
        == 404
    )

    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "imported_metadata": {
                "artwork": {
                    "owned": {
                        "poster": {
                            **owned_record,
                            "storage_key": "../../outside",
                        }
                    }
                }
            },
        }
    )
    assert (
        client.get(
            f"/api/vault-master/assets/{asset.id}/artwork/poster"
        ).status_code
        == 404
    )


def test_serves_only_catalogue_linked_person_portraits(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = MemoryVaultMasterStore()
    _, documents = configure(tmp_path, store)
    monkeypatch.setenv("PV_DOCUMENTS_PATH", str(documents))
    (documents / "movie.pdf").write_bytes(b"movie")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    assert process_next_batch(store) is not None
    asset = store.get_catalogued_asset("/vault/Documents/movie.pdf")
    assert asset is not None

    provider_item_id = "person-1"
    portrait_id = hashlib.sha256(
        provider_item_id.encode("utf-8")
    ).hexdigest()[:16]
    storage_key = f"artwork/{asset.id}/people/{portrait_id}"
    metadata_root = tmp_path / "metadata"
    portrait_path = metadata_root / storage_key
    portrait_path.parent.mkdir(parents=True)
    portrait_path.write_bytes(b"owned-portrait")
    app.dependency_overrides[get_metadata_storage_root] = (
        lambda: metadata_root
    )
    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "imported_metadata": {
                "people": [
                    {
                        "provider_item_id": provider_item_id,
                        "name": "Example Director",
                        "owned_image": {
                            "storage_key": storage_key,
                            "mime_type": "image/jpeg",
                            "size_bytes": 14,
                        },
                    }
                ]
            },
        }
    )

    response = client.get(
        f"/api/vault-master/assets/{asset.id}/people/{portrait_id}"
    )

    assert response.status_code == 200
    assert response.content == b"owned-portrait"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        client.get(
            f"/api/vault-master/assets/{asset.id}/people/0000000000000000"
        ).status_code
        == 404
    )


def test_scans_and_lists_incoming_and_existing_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, documents = configure(tmp_path, store)
    (documents / "original.txt").write_text("same", encoding="utf-8")
    (incoming / "copy.txt").write_text("same", encoding="utf-8")
    authenticate(client)

    inventory_response = client.post(
        "/api/vault-master/scan/inventory"
    )
    assert inventory_response.json()["status"] == "queued"
    assert process_next_batch(store) is not None
    arrival_hall_response = client.post(
        "/api/vault-master/scan/arrival-hall"
    )
    assert arrival_hall_response.json()["status"] == "queued"
    assert process_next_batch(store) is not None
    listing_response = client.get("/api/vault-master/items")

    assert inventory_response.status_code == 200
    assert arrival_hall_response.status_code == 200
    assert listing_response.status_code == 200
    items = {
        item["filename"]: item
        for item in listing_response.json()["items"]
    }
    assert items["original.txt"]["state"] == "inventoried"
    assert items["copy.txt"]["state"] == "needs_review"
    assert (
        items["copy.txt"]["duplicate_of_id"]
        == items["original.txt"]["id"]
    )


def test_theatre_duplicate_listing_does_not_disclose_another_owners_inventory_file(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    owner_user_id = uuid4()
    sha256 = "f" * 64
    inventory = store.record_file(
        store.create_batch(INVENTORY_SOURCE, "/vault/Theatre/Movies"),
        INVENTORY_SOURCE,
        ScannedFile(
            "/vault/Theatre/Movies/private-film.mkv", "private-film.mkv",
            "private-film.mkv", 9, "video/x-matroska", datetime.now(timezone.utc),
            sha256, {}, owner_username="owner", owner_user_id=owner_user_id,
        ),
    )
    store.restore_catalogued_asset(
        CataloguedAsset(
            id=uuid4(), asset_type="Movies", display_title="Private film",
            captured_on=None, location=None,
            vault_path="/vault/Theatre/Movies/private-film.mkv",
            filename="private-film.mkv", size_bytes=9, mime_type="video/x-matroska",
            sha256=sha256, metadata={}, metadata_provenance={},
            owner_username="owner", owner_user_id=owner_user_id,
        ),
        "owner",
    )
    authenticate_regular_user(client, authentication_store)
    member = authentication_store.get_account("member")
    assert member is not None
    incoming = store.record_file(
        store.create_batch(INCOMING_SOURCE, "/arrival"),
        INCOMING_SOURCE,
        ScannedFile(
            "/arrival/film.mkv", "film.mkv", "film.mkv", 9,
            "video/x-matroska", datetime.now(timezone.utc), sha256, {},
            owner_username="member", owner_user_id=member.user_id,
        ),
    )
    theatre = store.update_proposal(incoming.id, "Movies", "member")
    assert theatre is not None and theatre.duplicate_of_id == inventory.id

    listing = client.get("/api/vault-master/items")
    assert listing.status_code == 200
    listed_items = listing.json()["items"]
    assert [item["id"] for item in listed_items] == [str(incoming.id)]
    assert listed_items[0]["duplicate_of_id"] == str(inventory.id)
    assert "private-film.mkv" not in listing.text

    approval = client.post(f"/api/vault-master/items/{incoming.id}/approve")
    assert approval.status_code == 409
    assert approval.json()["detail"] == "This item already exists in Theatre"

    personal = store.update_proposal(incoming.id, "Home Videos", "member")
    assert personal is not None and personal.duplicate_of_id is None
    approved = client.post(
        "/api/vault-master/bulk/approve",
        json={"item_ids": [str(incoming.id)]},
    )
    assert approved.status_code == 200, approved.json()
    queued = client.post(
        "/api/vault-master/bulk/move",
        json={"item_ids": [str(incoming.id)]},
    )
    assert queued.status_code == 200, queued.json()


def test_catalogue_backfill_endpoint_reuses_active_batches(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    configure(tmp_path, store)
    authenticate(client)

    first = client.post("/api/vault-master/catalogue/backfill")
    second = client.post("/api/vault-master/catalogue/backfill")

    assert first.status_code == 200
    assert first.json()["reused_active_batches"] == 0
    assert second.status_code == 200
    assert second.json()["batch_ids"] == first.json()["batch_ids"]
    assert second.json()["reused_active_batches"] == 1
    assert len(store.list_batches()) == 1


def test_edit_and_approval_are_persistent_without_moving_file(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / "photo.jpeg"
    source.write_bytes(b"photo")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    edit_response = client.patch(
        f"/api/vault-master/items/{item.id}/proposal",
        json={"category": "Documents"},
    )
    approve_response = client.post(
        f"/api/vault-master/items/{item.id}/approve"
    )
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    persisted = store.list_items()[0]

    assert edit_response.status_code == 200
    assert edit_response.json()["proposed_destination"] == (
        "/vault/Documents/photo.jpeg"
    )
    assert approve_response.status_code == 200
    assert persisted.state == "approved"
    assert persisted.proposed_category == "Documents"
    assert source.read_bytes() == b"photo"


def test_theatre_arrival_proposals_accept_movies_and_tv_shows_without_publishing(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "Foundation S01E01.mp4").write_bytes(b"episode")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    movie = client.patch(
        f"/api/vault-master/items/{item.id}/proposal",
        json={"category": "Movies", "publication_audience": "private"},
    )
    tv_show = client.patch(
        f"/api/vault-master/items/{item.id}/proposal",
        json={"category": "TV Shows", "publication_audience": "vault-wide"},
    )
    persisted = store.get_item(item.id)

    assert movie.status_code == 200
    assert movie.json()["proposed_destination"] == "/vault/Theatre/Movies/Foundation S01E01.mp4"
    assert movie.json()["publication_audience"] == "private"
    assert tv_show.status_code == 200
    assert tv_show.json()["proposed_destination"] == "/vault/Theatre/TV Shows/Foundation S01E01.mp4"
    assert tv_show.json()["publication_audience"] == "vault-wide"
    assert persisted is not None
    assert persisted.state == "needs_review"
    assert persisted.metadata.get("tv_publication_set") is None


def test_browser_cannot_submit_arbitrary_destination(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "photo.jpeg").write_bytes(b"photo")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    response = client.patch(
        f"/api/vault-master/items/{item.id}/proposal",
        json={"category": "../../etc", "destination": "/etc/passwd"},
    )

    assert response.status_code == 422


def test_music_is_an_approved_nested_arrival_hall_destination(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    album = incoming / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 Track.wma").write_bytes(b"audio")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    response = client.patch(
        f"/api/vault-master/items/{item.id}/proposal",
        json={"category": "Music"},
    )

    assert response.status_code == 200
    assert response.json()["proposed_category"] == "Music"
    assert response.json()["proposed_destination"] == (
        "/vault/Music/Artist/Album/01 Track.wma"
    )


def test_editable_metadata_is_separate_from_detected_file_facts(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / "photo.jpeg"
    source.write_bytes(b"photo")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]
    original_checksum = item.sha256

    response = client.patch(
        f"/api/vault-master/items/{item.id}/metadata",
        json={
            "display_title": "Family photograph",
            "captured_on": "2017-10-15",
            "location": "London, United Kingdom",
        },
    )

    assert response.status_code == 200
    assert response.json()["metadata_overrides"] == {
        "display_title": "Family photograph",
        "captured_on": "2017-10-15",
        "location": "London, United Kingdom",
    }
    assert response.json()["sha256"] == original_checksum
    assert source.read_bytes() == b"photo"


def test_music_metadata_overrides_accept_catalogue_fields(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / "track.wma"
    source.write_bytes(b"audio")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    response = client.patch(
        f"/api/vault-master/items/{item.id}/metadata",
        json={
            "display_title": "One Day",
            "artist": "Imagine Dragons",
            "album": "Mercury - Act 1",
            "album_artist": "Imagine Dragons",
            "track_number": 13,
            "disc_number": 1,
            "release_year": 2021,
        },
    )

    assert response.status_code == 200
    assert response.json()["metadata_overrides"] == {
        "display_title": "One Day",
        "artist": "Imagine Dragons",
        "album": "Mercury - Act 1",
        "album_artist": "Imagine Dragons",
        "track_number": 13,
        "disc_number": 1,
        "release_year": 2021,
    }
    assert source.read_bytes() == b"audio"


def test_music_metadata_rejects_future_release_year(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "track.wma").write_bytes(b"audio")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    response = client.patch(
        f"/api/vault-master/items/{item.id}/metadata",
        json={"release_year": str(date.today().year + 1)},
    )

    assert response.status_code == 422


def test_metadata_override_can_be_restored_and_survives_rescan(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "photo.jpeg").write_bytes(b"photo")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]
    client.patch(
        f"/api/vault-master/items/{item.id}/metadata",
        json={
            "display_title": "Corrected title",
            "location": "York, United Kingdom",
        },
    )

    restored = client.patch(
        f"/api/vault-master/items/{item.id}/metadata",
        json={"location": None},
    )
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    persisted = store.get_item(item.id)

    assert restored.status_code == 200
    assert persisted is not None
    assert persisted.metadata_overrides == {
        "display_title": "Corrected title"
    }


def test_metadata_endpoint_rejects_file_fact_edits(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "photo.jpeg").write_bytes(b"photo")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    response = client.patch(
        f"/api/vault-master/items/{item.id}/metadata",
        json={"sha256": "0" * 64},
    )

    assert response.status_code == 422


def test_approved_move_is_explicit_and_persisted(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / "photo.jpeg"
    source.write_bytes(b"photo")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]

    assert client.post(f"/api/vault-master/items/{item.id}/move").status_code == 409
    client.post(f"/api/vault-master/items/{item.id}/approve")
    response = client.post(f"/api/vault-master/items/{item.id}/move")

    assert response.status_code == 200
    assert response.json()["state"] == "move_queued"
    assert source.exists()
    assert (
        process_next_move(
            store,
            incoming,
            app.dependency_overrides[get_destination_paths](),
        )
        == item.id
    )
    assert not source.exists()
    assert (tmp_path / "Gallery" / "photo.jpeg").read_bytes() == b"photo"
    assert any(
        batch["source_kind"] == "inventory"
        and batch["status"] == "completed"
        and batch["item_count"] == 1
        for batch in store.batches.values()
    )


def test_movie_publication_set_move_reports_incomplete_group(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    tron = incoming / "TRON"
    tron.mkdir()
    (tron / "Tron_t00.mkv").write_bytes(b"main feature")
    (tron / "Tron_t01.mkv").write_bytes(b"extra")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    items = sorted(store.list_items(), key=lambda item: item.relative_path)
    for index, item in enumerate(items):
        updated = replace(
            item,
            proposed_category="Movies",
            size_bytes=200 - index,
            metadata={
                **item.metadata,
                "duration_seconds": 200 - index,
                "width": 1920 - index,
                "height": 1080 - index,
            },
        )
        store.items[updated.source_path] = updated
    approved = store.record_decision(items[0].id, "approved", TEST_USERNAME)
    assert approved is not None

    response = client.post(f"/api/vault-master/items/{approved.id}/move")

    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            "Every file in this Movie publication set must be approved "
            "before any member can be moved"
        )
    }
    assert store.get_item(approved.id).state == "approved"


def test_pending_theatre_promotion_can_reissue_only_a_fresh_signed_request(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "manual.mkv").write_bytes(b"manual-theatre")
    queue = tmp_path / "theatre-requests"
    receipts = tmp_path / "theatre-receipts"
    key = tmp_path / "arrival-theatre-publisher.key"
    key.write_bytes(b"test signing key")
    receipts.mkdir()
    monkeypatch.setenv("PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE", str(queue))
    monkeypatch.setenv("PV_ARRIVAL_MANAGED_PUBLISHER_RECEIPTS", str(receipts))
    monkeypatch.setenv("PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH", str(key))
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]
    assert client.patch(
        f"/api/vault-master/items/{item.id}/proposal", json={"category": "Movies"}
    ).status_code == 200
    assert client.post(f"/api/vault-master/items/{item.id}/approve").status_code == 200
    assert client.post(f"/api/vault-master/items/{item.id}/move").status_code == 200
    assert process_next_move(store, incoming, {}, theatre_queue=lambda _: None) == item.id
    assert store.get_item(item.id).state == "theatre_promotion_pending"

    response = client.post(f"/api/vault-master/items/{item.id}/theatre-promotion/reissue")

    assert response.status_code == 200
    assert response.json()["state"] == "theatre_promotion_pending"
    assert len(list(queue.glob("*.json"))) == 1
    assert (incoming / "manual.mkv").read_bytes() == b"manual-theatre"
    assert client.post(f"/api/vault-master/items/{item.id}/theatre-promotion/reissue").status_code == 409


def test_move_collision_preserves_both_files_and_records_failure(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / "photo.jpeg"
    source.write_bytes(b"photo")
    existing = tmp_path / "Gallery" / "photo.jpeg"
    existing.write_bytes(b"existing")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = store.list_items()[0]
    client.post(f"/api/vault-master/items/{item.id}/approve")

    response = client.post(f"/api/vault-master/items/{item.id}/move")

    assert response.status_code == 200
    assert response.json()["state"] == "move_queued"
    process_next_move(
        store,
        incoming,
        app.dependency_overrides[get_destination_paths](),
    )
    assert store.list_items()[0].state == "move_failed"
    assert source.read_bytes() == b"photo"
    assert existing.read_bytes() == b"existing"


def test_duplicate_can_be_kept_without_changing_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, documents = configure(tmp_path, store)
    vault_file = documents / "original.txt"
    incoming_file = incoming / "copy.txt"
    vault_file.write_bytes(b"same")
    incoming_file.write_bytes(b"same")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    process_next_batch(store)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = next(item for item in store.list_items() if item.filename == "copy.txt")

    response = client.post(
        f"/api/vault-master/items/{item.id}/duplicate/keep"
    )

    assert response.status_code == 200
    assert response.json()["state"] == "duplicate_kept"
    assert vault_file.read_bytes() == b"same"
    assert incoming_file.read_bytes() == b"same"


def test_duplicate_removal_is_explicit_and_preserves_vault_copy(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, documents = configure(tmp_path, store)
    vault_file = documents / "original.txt"
    incoming_file = incoming / "copy.txt"
    vault_file.write_bytes(b"same")
    incoming_file.write_bytes(b"same")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    process_next_batch(store)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = next(item for item in store.list_items() if item.filename == "copy.txt")

    response = client.post(
        f"/api/vault-master/items/{item.id}/duplicate/remove"
    )

    assert response.status_code == 200
    assert response.json()["state"] == "duplicate_removed"
    assert not incoming_file.exists()
    assert vault_file.read_bytes() == b"same"


def test_rejected_item_can_return_to_review_or_be_explicitly_removed(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    source = incoming / "rejected.mov"
    source.write_bytes(b"original-video")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item = next(item for item in store.list_items() if item.filename == source.name)
    assert client.post(f"/api/vault-master/items/{item.id}/reject").status_code == 200

    restored = client.post(
        f"/api/vault-master/items/{item.id}/return-to-review"
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "needs_review"
    assert source.read_bytes() == b"original-video"

    assert client.post(f"/api/vault-master/items/{item.id}/reject").status_code == 200
    refused = client.post(
        f"/api/vault-master/items/{item.id}/rejected/remove",
        json={"confirmation": "REMOVE"},
    )
    assert refused.status_code == 422
    assert source.read_bytes() == b"original-video"

    removed = client.post(
        f"/api/vault-master/items/{item.id}/rejected/remove",
        json={"confirmation": "REMOVE FROM ARRIVAL HALL"},
    )
    assert removed.status_code == 200
    assert removed.json()["state"] == "arrival_removed"
    assert not source.exists()


def test_bulk_approval_and_move_queue_preserve_per_item_state(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    (incoming / "one.jpg").write_bytes(b"one")
    (incoming / "two.jpg").write_bytes(b"two")
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    item_ids = [str(item.id) for item in store.list_items()]

    approved = client.post(
        "/api/vault-master/bulk/approve",
        json={"item_ids": item_ids},
    )
    queued = client.post(
        "/api/vault-master/bulk/move",
        json={"item_ids": item_ids},
    )

    assert approved.status_code == 200, approved.json()
    assert {item["state"] for item in approved.json()["items"]} == {
        "approved"
    }
    assert queued.status_code == 200
    assert {item["state"] for item in queued.json()["items"]} == {
        "move_queued"
    }


def test_bulk_approval_rejects_exact_duplicates_without_approving_anything(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, documents = configure(tmp_path, store)
    (documents / "original.jpg").write_bytes(b"same")
    (incoming / "copy.jpg").write_bytes(b"same")
    authenticate(client)
    client.post("/api/vault-master/scan/inventory")
    process_next_batch(store)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    duplicate = next(
        item for item in store.list_items() if item.source_kind == "incoming"
    )

    response = client.post(
        "/api/vault-master/bulk/approve",
        json={"item_ids": [str(duplicate.id)]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Every selected file must be an owned, non-duplicate proposal awaiting review"
    assert store.get_item(duplicate.id).state == "needs_review"


def test_bulk_approval_requires_a_complete_consistent_tv_season(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    incoming, _ = configure(tmp_path, store)
    for episode in range(1, 3):
        (incoming / f"Foundation S01E{episode:02d}.mp4").write_bytes(f"episode-{episode}".encode())
    authenticate(client)
    client.post("/api/vault-master/scan/incoming")
    process_next_batch(store)
    episodes = [item for item in store.list_items() if item.source_kind == "incoming"]
    assert len(episodes) == 2
    for episode in episodes:
        proposal = client.patch(
            f"/api/vault-master/items/{episode.id}/proposal",
            json={"category": "TV Shows", "publication_audience": "vault-wide"},
        )
        assert proposal.status_code == 200

    incomplete = client.post(
        "/api/vault-master/bulk/approve",
        json={"item_ids": [str(episodes[0].id)]},
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"] == "Select every Episode in this TV Show Season before approving it together"
    assert {store.get_item(episode.id).state for episode in episodes} == {"needs_review"}

    approved = client.post(
        "/api/vault-master/bulk/approve",
        json={"item_ids": [str(episode.id) for episode in episodes]},
    )
    assert approved.status_code == 200, approved.json()
    assert {item["state"] for item in approved.json()["items"]} == {"approved"}
    assert {store.get_item(episode.id).state for episode in episodes} == {"approved"}
    markers = [store.get_item(episode.id).metadata["tv_publication_set"] for episode in episodes]
    assert markers[0] == markers[1]
    assert {store.get_item(episode.id).proposed_destination for episode in episodes} == {
        "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E01.mp4",
        "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E02.mp4",
    }
