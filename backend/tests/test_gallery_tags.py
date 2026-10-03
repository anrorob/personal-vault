"""Canonical Gallery taxonomy and private filter contracts, using synthetic photos."""
from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

from app.auth import AuthenticatedIdentity, require_authenticated_user
from app.gallery_custom_tags import get_gallery_custom_tag_store
from app.gallery_intelligence import GalleryConcept, GalleryIntelligenceClassification, MemoryGalleryIntelligenceStore, get_gallery_intelligence_store
from app.gallery_taxonomy import gallery_taxonomy_terms
from app.main import app
from tests.conftest import TEST_USERNAME
from tests.test_gallery import authenticate, catalogue_image, configure_gallery, create_image
import app.gallery as gallery

PHOTO_TYPES = ["Animal", "Building / Architecture", "Document", "Food", "Landscape", "Night photo", "Portrait", "Screenshot", "Selfie", "Vehicle"]
TAGS = ["Animal", "Beach", "Building", "Cat", "Motorcycle", "Outdoors", "Sea"]


def test_gallery_taxonomy_ignores_legacy_database_and_raw_model_extras(client, tmp_path):
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, create_image(tmp_path, "synthetic.jpg"))
    intelligence = MemoryGalleryIntelligenceStore()
    intelligence.create_custom_tag(asset.id, asset.owner_user_id, "Legacy private label")
    intelligence.terms[("photo_type", "arbitrary")] = "Arbitrary database type"
    intelligence.terms[("content_tag", "sea")] = "Wrong historical wording"
    intelligence.decide(asset.id, "photo_type", "arbitrary", "include", TEST_USERNAME)
    intelligence.decide(asset.id, "content_tag", "sea", "include", TEST_USERNAME)
    intelligence.concepts.append(GalleryConcept("unmapped"))
    intelligence.concept_terms.append(("unmapped", "content_tag", "legacy-private-label"))
    assert intelligence.resolve_raw_tags(["unmapped", "unrecognized Florence free text"]) == ()
    job = intelligence.queue(asset.id, TEST_USERNAME)
    evidence = GalleryIntelligenceClassification("unmapped raw evidence", "fake", "fake", "fake", 1)
    intelligence.complete(job.id, (("photo_type", "arbitrary"),), None, evidence)
    assert intelligence.latest_evidence(asset.id).raw_classification == "unmapped raw evidence"
    assert (asset.id, "photo_type", "arbitrary") not in intelligence.assignments
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)
    terms = client.get("/api/gallery/intelligence/terms").json()
    assert [t["display_name"] for t in terms if t["namespace"] == "photo_type"] == PHOTO_TYPES
    assert [t["display_name"] for t in terms if t["namespace"] == "content_tag"] == TAGS
    image_id = client.get("/api/gallery").json()[0]["id"]
    assert client.get(f"/api/gallery/{image_id}").json()["intelligence"] == [{"namespace": "content_tag", "slug": "sea", "display_name": "Sea"}]
    assert client.get("/api/gallery?content_tag=legacy-private-label").json() == []
    assert client.patch(f"/api/gallery/{image_id}/intelligence", json={"namespace": "content_tag", "slug": "legacy-private-label", "decision": "include"}).status_code == 422


def test_private_filter_all_tags_identity_isolation_and_viewer_scope(client, tmp_path, authentication_store):
    store = configure_gallery(tmp_path)
    assets = [catalogue_image(store, tmp_path, create_image(tmp_path, f"photo-{i}.jpg"), captured_on=date(2024, 1, i+1)) for i in range(3)]
    authenticate(client)
    cards = client.get("/api/gallery?sort=oldest").json()
    bikes = client.post("/api/gallery/custom-tags", json={"display_name": "Bikes"}).json()
    unused = client.post("/api/gallery/custom-tags", json={"display_name": "Unused"}).json()
    for card in cards[:2]:
        assert client.put(f"/api/gallery/{card['id']}/custom-tags/{bikes['id']}").status_code == 204
    assert {tag["display_name"] for tag in client.get("/api/gallery/custom-tags").json()} == {"Bikes", "Unused"}
    assert client.get("/api/gallery", params={"private_tag": unused["id"]}).json() == []
    assert [c["id"] for c in client.get("/api/gallery", params={"private_tag": bikes["id"], "sort": "oldest"}).json()] == [c["id"] for c in cards[:2]]
    detail = client.get(f"/api/gallery/{cards[1]['id']}", params={"private_tag": bikes["id"], "sort": "oldest"}).json()
    assert detail["previous_id"] == cards[0]["id"] and detail["next_id"] is None
    assert client.get(f"/api/gallery/{cards[2]['id']}", params={"private_tag": bikes["id"]}).status_code == 404
    assert client.get("/api/gallery?private_tag=not-a-uuid").status_code == 422
    account_a = authentication_store.get_account(TEST_USERNAME)
    account_b = replace(account_a, user_id=uuid4())  # Same display/username cannot confer ownership.
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(account_b)
    assert client.get("/api/gallery/custom-tags").json() == []
    other = client.post("/api/gallery/custom-tags", json={"display_name": "Bikes"}).json()
    assert other["id"] != bikes["id"]
    assert client.get("/api/gallery", params={"private_tag": bikes["id"]}).json() == []
    assert client.delete(f"/api/gallery/custom-tags/{bikes['id']}").status_code == 404
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(account_a)
    assert client.patch(f"/api/gallery/custom-tags/{bikes['id']}", json={"display_name": "Cycling"}).status_code == 200
    assert client.get("/api/gallery", params={"private_tag": other["id"]}).json() == []
    assert client.delete(f"/api/gallery/custom-tags/{bikes['id']}").status_code == 204
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(account_b)
    assert client.get("/api/gallery/custom-tags").json() == [other]


def test_recipient_private_filter_preserves_sharing_and_hidden_boundaries(client, tmp_path, authentication_store, monkeypatch):
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, create_image(tmp_path, "shared.jpg"))
    authenticate(client)
    image_id = client.get("/api/gallery").json()[0]["id"]
    owner = authentication_store.get_account(TEST_USERNAME)
    recipient = replace(owner, user_id=uuid4())
    other = replace(owner, user_id=uuid4())
    store.catalogued_assets[asset.vault_path] = replace(asset, visibility="shared", shared_with_user_ids=(recipient.user_id, other.user_id))
    included = {asset.id: "Synthetic owner"}
    monkeypatch.setattr(gallery, "included_gallery_assets", lambda user: included)
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(recipient)
    tag = client.post("/api/gallery/custom-tags", json={"display_name": "Bikes"}).json()
    assert client.put(f"/api/gallery/{image_id}/custom-tags/{tag['id']}").status_code == 204
    assert len(client.get("/api/gallery", params={"private_tag": tag["id"]}).json()) == 1
    for account in (owner, other):
        app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(account)
        assert client.get("/api/gallery/custom-tags").json() == []
        assert client.get(f"/api/gallery/{image_id}").json()["custom_tags"] == []
        assert client.get("/api/gallery", params={"private_tag": tag["id"]}).json() == []
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(recipient)
    included.clear()
    assert client.get("/api/gallery", params={"private_tag": tag["id"]}).json() == []
    assert client.get(f"/api/gallery/{image_id}", params={"private_tag": tag["id"]}).status_code == 404
    # Owner's existing private assignment remains subject to Hidden Photos authorization.
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(owner)
    owner_tag = client.post("/api/gallery/custom-tags", json={"display_name": "Owner private"}).json()
    assert client.put(f"/api/gallery/{image_id}/custom-tags/{owner_tag['id']}").status_code == 204
    store.catalogued_assets[asset.vault_path] = replace(asset, lifecycle_state="hidden")
    params = {"private_tag": owner_tag["id"]}
    assert client.get("/api/gallery", params=params).json() == []
    params["include_hidden"] = "true"
    assert client.get("/api/gallery", params=params).status_code == 403
    assert authentication_store.authorize_hidden_photos_session(client.cookies.get("pv_session"), owner.user_id)
    assert len(client.get("/api/gallery", params=params).json()) == 1
    assert client.get(f"/api/gallery/{image_id}", params=params).status_code == 200
