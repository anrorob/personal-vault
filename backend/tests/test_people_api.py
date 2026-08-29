from datetime import date, datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient

from app.auth import AuthenticatedIdentity, require_authenticated_user
from app.auth_store import Account
from app.gallery_people import MemoryGalleryPeopleStore, get_gallery_people_store
from app.main import app
from app.people import get_share_grant_store
from app.vault_master import CataloguedAsset, MemoryVaultMasterStore, get_vault_master_store
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200


def configure_people_api() -> tuple[MemoryGalleryPeopleStore, MemoryVaultMasterStore]:
    people = MemoryGalleryPeopleStore()
    vault = MemoryVaultMasterStore()
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    app.dependency_overrides[get_vault_master_store] = lambda: vault
    app.dependency_overrides[get_share_grant_store] = lambda: EmptyShareGrantStore()
    return people, vault


class EmptyShareGrantStore:
    def included_gallery_assets_for_local_people(
        self, _recipient_user_id: UUID, _person_ids: tuple[UUID, ...]
    ) -> set[UUID]:
        return set()


def owner_id(username: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"personal-vault-test:{username}")


def catalogue_asset(
    vault: MemoryVaultMasterStore, *, username: str = TEST_USERNAME
) -> CataloguedAsset:
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type="Gallery",
        display_title="Family portrait",
        captured_on=date(2024, 5, 6),
        location=None,
        vault_path=f"/vault/Gallery/{uuid4()}.jpg",
        filename="family-portrait.jpg",
        size_bytes=100,
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={},
        owner_username=username,
        owner_user_id=owner_id(username),
    )
    vault.catalogued_assets[asset.vault_path] = asset
    return asset


def identity(username: str) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        Account(
            username=username,
            display_name=username.title(),
            email=None,
            password_hash=None,
            role="member",
            active=True,
            password_change_required=False,
            created_at=datetime.now(timezone.utc),
            last_sign_in_at=None,
            user_id=owner_id(username),
        )
    )


def test_people_api_lists_searches_details_and_returns_only_safe_asset_summaries(
    client: TestClient,
) -> None:
    people, vault = configure_people_api()
    asset = catalogue_asset(vault)
    authenticate(client)

    owner = client.post(
        "/api/people",
        json={
            "full_name": "Owner Kowalski",
            "preferred_name": "Rob",
            "aliases": ["Bobby"],
            "date_of_birth": "1980-01-02",
            "profile_asset_id": str(asset.id),
        },
    )
    duplicate = client.post("/api/people", json={"full_name": "Owner Kowalski"})
    assert owner.status_code == 201 and duplicate.status_code == 201
    assert owner.json()["person_id"] != duplicate.json()["person_id"]
    person_id = UUID(owner.json()["person_id"])
    people.associate(asset.id, person_id, "user")

    assert len(client.get("/api/people").json()) == 2
    assert len(client.get("/api/people", params={"query": "kowalski"}).json()) == 2
    for query in ("rob", "bobby"):
        assert str(person_id) in {
            item["person_id"]
            for item in client.get("/api/people", params={"query": query}).json()
        }

    detail = client.get(f"/api/people/{person_id}")
    assert detail.status_code == 200
    assert detail.json()["associated_asset_count"] == 1
    assert detail.json()["associated_assets"] == [{
        "asset_id": str(asset.id), "display_title": "Family portrait",
        "asset_type": "Gallery", "vault_path": asset.vault_path,
    }]
    assert "embedding" not in detail.text and "bounding_box" not in detail.text


def test_people_api_updates_me_relationships_and_rejects_invalid_profile_assets(
    client: TestClient,
) -> None:
    _, vault = configure_people_api()
    profile = catalogue_asset(vault)
    authenticate(client)
    me = client.post("/api/people", json={"full_name": "Owner"}).json()
    anna = client.post("/api/people", json={"full_name": "Anna"}).json()

    assert client.post(
        "/api/people",
        json={"full_name": "Needs Me", "relationship_label": "friend"},
    ).status_code == 409

    updated = client.patch(
        f"/api/people/{anna['person_id']}",
        json={"preferred_name": "Annie", "aliases": ["Ann"], "profile_asset_id": str(profile.id)},
    )
    assert updated.status_code == 200
    assert updated.json()["person_id"] == anna["person_id"]
    assert updated.json()["preferred_name"] == "Annie"
    assert client.patch(f"/api/people/{anna['person_id']}", json={"profile_asset_id": str(uuid4())}).status_code == 422

    assert client.get("/api/people/me").json() is None
    assert client.put("/api/people/me", json={"person_id": me["person_id"]}).status_code == 200
    assert client.put(f"/api/people/{anna['person_id']}/relationship", json={"relationship_label": "friend"}).status_code == 200
    assert client.get(f"/api/people/{anna['person_id']}/relationship").json() == {"relationship_label": "friend"}
    assert client.put(f"/api/people/{anna['person_id']}/relationship", json={"relationship_label": "colleague"}).json() == {"relationship_label": "colleague"}
    assert client.delete(f"/api/people/{anna['person_id']}/relationship").status_code == 204
    assert client.get(f"/api/people/{anna['person_id']}/relationship").json() is None
    assert client.delete("/api/people/me").status_code == 204
    assert client.get("/api/people/me").json() is None


def test_people_api_denies_foreign_people_me_relationships_and_assets(
    client: TestClient,
) -> None:
    people, vault = configure_people_api()
    foreign_asset = catalogue_asset(vault, username="other")
    foreign = people.create_person(
        "other", owner_user_id=owner_id("other"), full_name="Private Person"
    )
    people.associate(foreign_asset.id, foreign.id, "user")
    authenticate(client)

    assert client.get(f"/api/people/{foreign.id}").status_code == 404
    assert client.patch(f"/api/people/{foreign.id}", json={"full_name": "Nope"}).status_code == 404
    assert client.put("/api/people/me", json={"person_id": str(foreign.id)}).status_code == 404
    assert client.get(f"/api/people/{foreign.id}/assets").status_code == 404
    assert client.post("/api/people", json={"full_name": "Owner", "profile_asset_id": str(foreign_asset.id)}).status_code == 422

    app.dependency_overrides[require_authenticated_user] = lambda: identity("other")
    assert client.get(f"/api/people/{foreign.id}").status_code == 200


def test_people_api_includes_only_currently_shared_assets_explicitly_linked_to_local_people(
    client: TestClient,
) -> None:
    """A shared origin Person label never name-merges into the recipient's People."""
    people, vault = configure_people_api()
    shared = catalogue_asset(vault, username="recipient")
    inaccessible = catalogue_asset(vault, username="someone-else")
    authenticate(client)
    recipient = client.post("/api/people", json={"full_name": "Recipient"}).json()
    same_name = client.post("/api/people", json={"full_name": "Recipient"}).json()
    local_anita_id = UUID(recipient["person_id"])
    active = {"value": True}

    class SharedLocalPeople:
        def included_gallery_assets_for_local_people(
            self, recipient_user_id: UUID, person_ids: tuple[UUID, ...]
        ) -> set[UUID]:
            if active["value"] and recipient_user_id == owner_id(TEST_USERNAME) and person_ids == (local_anita_id,):
                return {shared.id}
            return set()

    app.dependency_overrides[get_share_grant_store] = lambda: SharedLocalPeople()
    detail = client.get(f"/api/people/{local_anita_id}")
    assert detail.status_code == 200
    assert detail.json()["associated_assets"] == [{
        "asset_id": str(shared.id), "display_title": "Family portrait",
        "asset_type": "Gallery", "vault_path": None,
    }]
    assert inaccessible.id != shared.id
    assert "embedding" not in detail.text and "bounding_box" not in detail.text

    # Same display names are unrelated UUID-scoped People and do not name-merge.
    assert client.get(f"/api/people/{same_name['person_id']}").json()["associated_assets"] == []

    # Recipient cannot use the owner-only correction endpoint to change origin metadata.
    assert client.put(
        f"/api/people/{local_anita_id}/assets",
        json={"asset_id": str(shared.id), "person_id": str(local_anita_id), "decision": "include"},
    ).status_code == 404

    # A revoked share is not retained by the local association.
    active["value"] = False
    assert client.get(f"/api/people/{local_anita_id}").json()["associated_assets"] == []


def test_people_profile_frame_merge_and_asset_correction_preserve_owner_scoped_identity(
    client: TestClient,
) -> None:
    people, vault = configure_people_api()
    asset = catalogue_asset(vault)
    second_asset = catalogue_asset(vault)
    authenticate(client)
    retained = client.post("/api/people", json={"full_name": "Alex", "profile_asset_id": str(asset.id), "profile_frame": {"scale": 1.4, "x": 42, "y": 61}}).json()
    duplicate = client.post("/api/people", json={"full_name": "Alex duplicate"}).json()
    related = client.post("/api/people", json={"full_name": "Casey"}).json()
    retained_id, duplicate_id, related_id = (UUID(retained["person_id"]), UUID(duplicate["person_id"]), UUID(related["person_id"]))
    assert retained["profile_frame"] == {"scale": 1.4, "x": 42.0, "y": 61.0}
    assert client.patch(f"/api/people/{retained_id}", json={"profile_frame": {"scale": 4, "x": 50, "y": 50}}).status_code == 422
    people.associate(second_asset.id, duplicate_id, "user")
    face_id = people.add_face_detection(second_asset.id)
    people.identify_face(second_asset.id, face_id, duplicate_id, owner_id(TEST_USERNAME))
    people.set_me_person(owner_id(TEST_USERNAME), duplicate_id)
    people.set_relationship(owner_id(TEST_USERNAME), duplicate_id, related_id, "friend")
    response = client.post(f"/api/people/{retained_id}/merge", json={"source_person_id": str(duplicate_id)})
    assert response.status_code == 200
    assert {item["person_id"] for item in client.get("/api/people").json()} == {retained["person_id"], related["person_id"]}
    assert client.get("/api/people/me").json()["person_id"] == retained["person_id"]
    assert people.matching_asset_ids((retained_id,), owner_id(TEST_USERNAME)) == {second_asset.id}
    assert people.face_detections[face_id]["reference_person_id"] == retained_id
    assert people.relationships_for_person(owner_id(TEST_USERNAME), retained_id)[0].related_person_id == related_id

    replacement = client.post("/api/people", json={"full_name": "Replacement"}).json()
    corrected = client.put(f"/api/people/{retained_id}/assets", json={"asset_id": str(second_asset.id), "person_id": replacement["person_id"], "decision": "include"})
    assert corrected.status_code == 204
    assert people.matching_asset_ids((UUID(replacement["person_id"]),), owner_id(TEST_USERNAME)) == {second_asset.id}
