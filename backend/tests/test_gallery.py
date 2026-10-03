from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from PIL import Image
import pytest

from app.auth import AuthenticatedIdentity, require_authenticated_user
import app.gallery as gallery_module
from app.auth_store import Account, MemoryAuthenticationStore
from app.gallery import get_gallery_path, scan_gallery, to_summary
from app.gallery_florence import get_gallery_florence_store
from app.gallery_intelligence import BULK_COMPLETION_GRACE, MemoryGalleryIntelligenceStore, get_gallery_intelligence_store
from app.gallery_people import MemoryGalleryPeopleStore, get_gallery_people_store
from app.people import get_share_grant_store
from app.main import app
from app.vault_master import (
    CataloguedAsset,
    MemoryVaultMasterStore,
    apply_catalogue_metadata_changes,
    get_vault_master_store,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
        },
    )
    assert response.status_code == 200


def synthetic_recipient() -> AuthenticatedIdentity:
    return AuthenticatedIdentity(Account(
        username="son", display_name="Synthetic recipient", email=None,
        password_hash=None, role="user", active=True, password_change_required=False,
        created_at=datetime.now(timezone.utc), last_sign_in_at=None,
        user_id=uuid5(NAMESPACE_URL, "test:gallery:son"),
    ))


def configure_gallery(tmp_path: Path) -> MemoryVaultMasterStore:
    store = MemoryVaultMasterStore()
    app.dependency_overrides[get_gallery_path] = lambda: tmp_path
    app.dependency_overrides[get_vault_master_store] = (
        lambda: store
    )
    app.dependency_overrides[get_gallery_intelligence_store] = (
        lambda: MemoryGalleryIntelligenceStore()
    )
    app.dependency_overrides[get_gallery_people_store] = lambda: MemoryGalleryPeopleStore()
    return store


def create_image(
    gallery_path: Path,
    relative_path: str,
    content: bytes = b"test-image",
) -> Path:
    image_path = gallery_path / relative_path
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(content)
    return image_path


def catalogue_image(
    store: MemoryVaultMasterStore,
    gallery_path: Path,
    image_path: Path,
    *,
    captured_on: date | None = date(2024, 5, 6),
) -> CataloguedAsset:
    vault_path = (
        "/vault/Gallery/"
        f"{image_path.relative_to(gallery_path).as_posix()}"
    )
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type="Gallery",
        display_title=image_path.stem.replace("_", " "),
        captured_on=captured_on,
        location=None,
        vault_path=vault_path,
        filename=image_path.name,
        size_bytes=image_path.stat().st_size,
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={
            "display_title": "filename",
            "captured_on": (
                "embedded" if captured_on is not None else "unavailable"
            ),
            "location": "unavailable",
        },
        owner_username=TEST_USERNAME,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),
    )
    store.catalogued_assets[vault_path] = asset
    return asset


class GalleryHideShareAuthority:
    def __init__(self, *, shared: bool = False, failure: Exception | None = None) -> None:
        self.shared = shared
        self.failure = failure

    def has_active_share_for_asset(self, asset_id: UUID, owner_user_id: UUID) -> bool:
        if self.failure is not None:
            raise self.failure
        return self.shared


def test_gallery_hide_uses_share_authority_and_fails_closed(client: TestClient, tmp_path: Path) -> None:
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, create_image(tmp_path, "hide.jpg"))
    authenticate(client)
    app.dependency_overrides[get_share_grant_store] = lambda: GalleryHideShareAuthority()
    assert client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/hide").status_code == 200
    assert store.get_catalogued_asset(asset.vault_path).lifecycle_state == "hidden"
    store.catalogued_assets[asset.vault_path] = replace(asset)
    app.dependency_overrides[get_share_grant_store] = lambda: GalleryHideShareAuthority(shared=True)
    shared = client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/hide")
    assert shared.status_code == 409
    assert shared.json()["detail"] == "This photo is shared. Unshare it before hiding."
    app.dependency_overrides[get_share_grant_store] = lambda: GalleryHideShareAuthority(failure=RuntimeError("database unavailable"))
    unavailable = client.post(f"/api/vault-master/assets/{asset.id}/lifecycle/hide")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "Sharing state could not be confirmed; try again later."


def test_gallery_ux_uses_complete_filter_scroll_tile_selection_and_desktop_navigation() -> None:
    root = Path(__file__).parents[2]
    gallery_source = (root / "src" / "routes" / "app.gallery.index.tsx").read_text(encoding="utf-8")
    viewer_source = (root / "src" / "routes" / "app.gallery.$photoId.tsx").read_text(
        encoding="utf-8"
    )

    assert "max-h-[calc(100dvh-2rem)]" in gallery_source
    assert "overflow-x-hidden overflow-y-auto" in gallery_source
    assert "if (selectionMode && image.asset_id)" in gallery_source
    assert "event.preventDefault();" in gallery_source
    assert "hidden md:flex transition-transform" in viewer_source


def create_pdf(gallery_path: Path, relative_path: str) -> Path:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.write(output)
    return create_image(gallery_path, relative_path, output.getvalue())


def test_scanner_finds_supported_images_recursively(
    tmp_path: Path,
) -> None:
    create_image(tmp_path, "IMG_0002.JPEG")
    create_image(tmp_path, "Album/IMG_0001.jpg")
    create_image(tmp_path, "Album/notes.txt")

    images = scan_gallery(tmp_path)

    assert [image.name for image in images] == [
        "IMG_0001.jpg",
        "IMG_0002.JPEG",
        "notes.txt",
    ]
    assert all(len(image.id) == 20 for image in images)


def test_summary_uses_only_catalogued_vault_master_metadata(
    tmp_path: Path,
) -> None:
    image_path = create_image(tmp_path, "2024-05-01_photo.jpg")
    image = scan_gallery(tmp_path)[0]
    store = MemoryVaultMasterStore()
    asset = catalogue_image(
        store,
        tmp_path,
        image_path,
        captured_on=date(1995, 9, 3),
    )
    asset = CataloguedAsset(
        **{
            **asset.__dict__,
            "display_title": "Wedding photograph",
            "location": "Starogard Gdanski, Polska",
            "metadata_provenance": {
                "display_title": "user_override",
                "captured_on": "user_override",
                "location": "user_override",
            },
        }
    )

    summary = to_summary(image, asset)

    assert summary.display_title == "Wedding photograph"
    assert summary.captured_on.isoformat() == "1995-09-03"
    assert summary.date_source == "user_override"
    assert summary.location == "Starogard Gdanski, Polska"


def test_gallery_placement_confirms_a_pdf_as_a_photo(
    tmp_path: Path,
) -> None:
    pdf_path = create_pdf(tmp_path, "scanned-family-photo.pdf")
    image = scan_gallery(tmp_path)[0]
    store = MemoryVaultMasterStore()
    asset = catalogue_image(store, tmp_path, pdf_path)
    asset = CataloguedAsset(
        **{**asset.__dict__, "mime_type": "application/pdf"}
    )

    summary = to_summary(image, asset)

    assert summary.media_type == "application/pdf"
    assert summary.photo_display is True
    assert summary.warning is None


def test_gallery_endpoints_require_authentication(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_gallery(tmp_path)
    image = create_image(tmp_path, "photo.jpg")
    image_id = scan_gallery(tmp_path)[0].id

    assert image.exists()
    assert client.get("/api/gallery").status_code == 401
    assert client.get(f"/api/gallery/{image_id}").status_code == 401
    assert (
        client.get(f"/api/gallery/{image_id}/content").status_code
        == 401
    )
    assert client.get(f"/api/gallery/{image_id}/preview").status_code == 401


def test_gallery_returns_private_linked_images(
    client: TestClient,
    tmp_path: Path,
) -> None:
    first_path = create_image(tmp_path, "A.jpg", b"first")
    second_path = create_image(tmp_path, "B.jpg", b"second")
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, first_path)
    catalogue_image(store, tmp_path, second_path)
    authenticate(client)

    response = client.get("/api/gallery")
    body = response.json()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert [image["name"] for image in body] == ["A.jpg", "B.jpg"]
    # Gallery routing IDs are path-derived presentation IDs.  Selection and
    # sharing must use the separately exposed canonical catalogue UUID.
    assert body[0]["id"] != str(store.get_catalogued_asset("/vault/Gallery/A.jpg").id)  # type: ignore[union-attr]
    assert body[0]["asset_id"] == str(store.get_catalogued_asset("/vault/Gallery/A.jpg").id)  # type: ignore[union-attr]
    assert body[0]["captured_on"]
    assert body[0]["location"] is None
    assert body[0]["thumbnail_url"].startswith("/api/gallery/")
    assert str(tmp_path) not in response.text

    first_details = client.get(f"/api/gallery/{body[0]['id']}")
    first_body = first_details.json()

    assert first_details.status_code == 200
    assert first_details.headers["cache-control"] == "private, no-store"
    assert first_body["previous_id"] is None
    assert first_body["next_id"] == body[1]["id"]
    assert first_body["image_url"].endswith("/content")
    assert first_body["can_edit"] is True
    assert first_body["asset_id"]
    assert first_body["vault_path"] == "/vault/Gallery/A.jpg"
    assert first_body["mime_type"] == "image/jpeg"
    assert first_body["sha256"] == "a" * 64
    assert first_body["metadata_provenance"]["captured_on"] == "embedded"
    assert str(tmp_path) not in first_details.text

    second_details = client.get(f"/api/gallery/{body[1]['id']}")
    assert second_details.json()["previous_id"] == body[0]["id"]
    assert second_details.json()["next_id"] is None


def test_gallery_list_and_detail_reuse_bulk_catalogue_rows(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = configure_gallery(tmp_path)
    image = create_image(tmp_path, "Example Artist/photo.jpg")
    asset = catalogue_image(store, tmp_path, image)
    authenticate(client)

    def unexpected_reload(_paths, _username):
        raise AssertionError("Gallery reloaded catalogue rows already scanned")

    monkeypatch.setattr(store, "get_visible_catalogued_assets", unexpected_reload)
    listing = client.get("/api/gallery")
    assert listing.status_code == 200
    assert [row["asset_id"] for row in listing.json()] == [str(asset.id)]
    detail = client.get(f"/api/gallery/{listing.json()[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["asset_id"] == str(asset.id)


def test_gallery_metadata_falls_back_for_paths_not_in_bulk_catalogue(
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    image = create_image(tmp_path, "Example Artist/legacy.jpg")
    asset = catalogue_image(store, tmp_path, image)
    images = scan_gallery(tmp_path)
    metadata = gallery_module.get_gallery_metadata(
        store, tmp_path, images, asset.owner_user_id, {}
    )
    assert metadata[str(image)] == asset


def test_gallery_lists_and_serves_catalogued_slot_placement(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    slot = tmp_path / "slot"
    slot_image = slot / "Gallery" / "example.jpg"
    slot_image.parent.mkdir(parents=True)
    picture = BytesIO()
    Image.new("RGB", (24, 24), "blue").save(picture, format="JPEG")
    slot_image.write_bytes(picture.getvalue())
    store = configure_gallery(legacy)
    asset = CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="Example",
        captured_on=date(2024, 5, 6), location=None,
        vault_path="/vault/Gallery/example.jpg", filename="example.jpg",
        size_bytes=slot_image.stat().st_size, mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={"storage_placement": {
            "slot_id": "PV-TEST-SLOT-001", "relative_path": "Gallery/example.jpg",
        }}, metadata_provenance={}, owner_username=TEST_USERNAME,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),
    )
    store.catalogued_assets[asset.vault_path] = asset
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({"PV-TEST-SLOT-001": str(slot)}))
    monkeypatch.setattr(gallery_module, "gallery_thumbnail", (
        lambda source, _asset, _cache: (slot_image, "test-key") if source == slot_image else None
    ))
    authenticate(client)

    page = client.get("/api/gallery/pages")
    assert page.status_code == 200
    assert [image["name"] for image in page.json()["items"]] == ["example.jpg"]
    image_id = page.json()["items"][0]["id"]
    full_scan, full_index = gallery_module.find_gallery_image(legacy, image_id, store)
    assert gallery_module.find_gallery_content_image(legacy, image_id, store) == full_scan[full_index]
    assert client.get(f"/api/gallery/{image_id}").status_code == 200
    assert client.get(f"/api/gallery/{image_id}/content").content == picture.getvalue()
    assert client.get(f"/api/gallery/assets/{asset.id}/preview").status_code == 200


def test_gallery_content_lookup_preserves_legacy_first_for_duplicate_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    legacy_image = create_image(legacy, "example.jpg", b"legacy")
    slot = tmp_path / "slot"
    slot_image = create_image(slot, "Gallery/example.jpg", b"placed")
    store = MemoryVaultMasterStore()
    asset = catalogue_image(store, legacy, legacy_image)
    store.catalogued_assets[asset.vault_path] = replace(asset, metadata={
        "storage_placement": {
            "slot_id": "PV-TEST-SLOT-001", "relative_path": "Gallery/example.jpg",
        },
    })
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({"PV-TEST-SLOT-001": str(slot)}))
    image_id = gallery_module.get_image_id(Path("example.jpg"))
    full_scan, full_index = gallery_module.find_gallery_image(legacy, image_id, store)
    assert full_scan[full_index].path == legacy_image
    assert gallery_module.find_gallery_content_image(legacy, image_id, store) == full_scan[full_index]
    assert slot_image.exists()


def test_gallery_orders_and_navigates_by_canonical_capture_date(
    client: TestClient,
    tmp_path: Path,
) -> None:
    oldest_path = create_image(tmp_path, "A.jpg")
    newest_path = create_image(tmp_path, "B.jpg")
    undated_path = create_image(tmp_path, "C.jpg")
    store = configure_gallery(tmp_path)
    catalogue_image(
        store,
        tmp_path,
        oldest_path,
        captured_on=date(1995, 9, 3),
    )
    catalogue_image(
        store,
        tmp_path,
        newest_path,
        captured_on=date(2024, 5, 6),
    )
    catalogue_image(store, tmp_path, undated_path, captured_on=None)
    authenticate(client)

    newest = client.get("/api/gallery?sort=newest")
    oldest = client.get("/api/gallery?sort=oldest")

    assert [image["name"] for image in newest.json()] == [
        "B.jpg",
        "A.jpg",
        "C.jpg",
    ]
    assert [image["name"] for image in oldest.json()] == [
        "A.jpg",
        "B.jpg",
        "C.jpg",
    ]

    newest_first = newest.json()[0]
    details = client.get(
        f"/api/gallery/{newest_first['id']}?sort=newest"
    ).json()
    assert details["previous_id"] is None
    assert details["next_id"] == newest.json()[1]["id"]


def test_gallery_hides_uncatalogued_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    catalogued = create_image(tmp_path, "catalogued.jpg")
    uncatalogued = create_image(
        tmp_path,
        "not-yet-inventoried.jpg",
    )
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, catalogued)
    authenticate(client)

    response = client.get("/api/gallery")

    assert response.status_code == 200
    assert [image["name"] for image in response.json()] == [
        "catalogued.jpg"
    ]

    catalogued_id = scan_gallery(tmp_path)[0].id
    details = client.get(f"/api/gallery/{catalogued_id}")
    assert details.status_code == 200
    assert details.json()["previous_id"] is None
    assert details.json()["next_id"] is None

    uncatalogued_id = next(
        image.id
        for image in scan_gallery(tmp_path)
        if image.path == uncatalogued
    )
    missing = client.get(f"/api/gallery/{uncatalogued_id}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == (
        "Photo is not catalogued by Vault Master"
    )

    content = client.get(f"/api/gallery/{uncatalogued_id}/content")
    assert content.status_code == 404
    assert content.json()["detail"] == "Photo was not found"


def test_gallery_preserves_missing_catalogue_date(
    client: TestClient,
    tmp_path: Path,
) -> None:
    image_path = create_image(tmp_path, "unknown-date.jpg")
    store = configure_gallery(tmp_path)
    catalogue_image(
        store,
        tmp_path,
        image_path,
        captured_on=None,
    )
    authenticate(client)

    response = client.get("/api/gallery")

    assert response.status_code == 200
    assert response.json()[0]["captured_on"] is None
    assert response.json()[0]["date_source"] == "unavailable"


def test_gallery_intelligence_filters_are_catalogue_backed_and_resettable(
    client: TestClient, tmp_path: Path
) -> None:
    portrait_path = create_image(tmp_path, "portrait.jpg")
    vehicle_path = create_image(tmp_path, "vehicle.jpg")
    unanalysed_path = create_image(tmp_path, "unanalysed.jpg")
    store = configure_gallery(tmp_path)
    portrait = catalogue_image(store, tmp_path, portrait_path)
    vehicle = catalogue_image(store, tmp_path, vehicle_path)
    catalogue_image(store, tmp_path, unanalysed_path)
    intelligence = MemoryGalleryIntelligenceStore()
    intelligence.decide(portrait.id, "photo_type", "portrait", "include")
    intelligence.decide(portrait.id, "content_tag", "outdoors", "include")
    intelligence.decide(vehicle.id, "photo_type", "vehicle", "include")
    intelligence.decide(vehicle.id, "content_tag", "motorcycle", "include")
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    assert [row["name"] for row in client.get("/api/gallery").json()] == [
        "portrait.jpg", "unanalysed.jpg", "vehicle.jpg"
    ]
    assert [row["name"] for row in client.get("/api/gallery?photo_type=portrait").json()] == ["portrait.jpg"]
    assert [row["name"] for row in client.get("/api/gallery?content_tag=motorcycle").json()] == ["vehicle.jpg"]
    assert client.get("/api/gallery?photo_type=portrait&content_tag=motorcycle").json() == []


def test_people_data_foundation_supports_manual_no_face_authority_and_owner_isolation(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "no-face.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    people = MemoryGalleryPeopleStore()
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    authenticate(client)

    owner = client.post("/api/gallery/people", json={"display_name": "Owner"})
    anna = client.post("/api/gallery/people", json={"display_name": "Anna"})
    assert owner.status_code == 201 and anna.status_code == 201
    assert client.patch(f"/api/gallery/people/{owner.json()['id']}", json={"display_name": "Owner Updated"}).json()["display_name"] == "Owner Updated"
    added = client.patch(f"/api/gallery/people/assets/{asset.id}", json={"person_id": owner.json()["id"], "decision": "include"})
    assert added.status_code == 200
    assert added.json() == [{"id": owner.json()["id"], "display_name": "Owner Updated", "active": True, "source": "user"}]
    multiple = client.patch(f"/api/gallery/people/assets/{asset.id}", json={"person_id": anna.json()["id"], "decision": "include"})
    assert {row["display_name"] for row in multiple.json()} == {"Owner Updated", "Anna"}
    people.associate(asset.id, UUID(owner.json()["id"]), "vault_master")
    excluded = client.patch(f"/api/gallery/people/assets/{asset.id}", json={"person_id": owner.json()["id"], "decision": "exclude"})
    assert [row["display_name"] for row in excluded.json()] == ["Anna"]
    people.add_face_detection(asset.id, bounding_box={"x": 1}, detector_provider="yunet")
    people.add_person_detection(asset.id, bounding_box={"x": 2}, detector_provider="yolox")
    assert people.face_detections and people.person_detections
    app.dependency_overrides[require_authenticated_user] = lambda: synthetic_recipient()
    assert client.get("/api/gallery/people").json() == []
    assert client.get(f"/api/gallery/people/assets/{asset.id}").status_code == 404


def test_gallery_people_controls_are_owner_only_and_filter_by_canonical_person_id(
    client: TestClient, tmp_path: Path
) -> None:
    first_path = create_image(tmp_path, "first.jpg")
    second_path = create_image(tmp_path, "second.jpg")
    store = configure_gallery(tmp_path)
    first = catalogue_image(store, tmp_path, first_path)
    second = catalogue_image(store, tmp_path, second_path)
    people = MemoryGalleryPeopleStore()
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    person = client.post("/api/gallery/people", json={"display_name": "Owner"}).json()
    face_id = people.add_face_detection(first.id, bounding_box={"x": 1}, recognition_result="unknown")
    identified = client.post(
        f"/api/gallery/people/assets/{first.id}/identify",
        json={"person_id": person["id"], "decision": "include", "face_detection_id": str(face_id)},
    )
    assert identified.status_code == 200
    assert identified.json()[0]["display_name"] == "Owner"
    assert people.face_detections[face_id]["reference_person_id"] == UUID(person["id"])
    assert [row["name"] for row in client.get(f"/api/gallery?person={person['id']}").json()] == ["first.jpg"]

    # A regular Gallery Intelligence job must not hide the latest People attempt.
    intelligence.queue(first.id, TEST_USERNAME)
    queued = client.post(f"/api/gallery/people/assets/{first.id}/analyse")
    assert queued.status_code == 202
    people_job = next(job for job in intelligence.jobs.values() if job.people_only)
    assert client.get(f"/api/gallery/people/assets/{first.id}/status").json()["job"] == {
        "id": str(people_job.id), "status": "queued", "error": None
    }
    claimed = intelligence.claim_next_job()
    assert claimed is not None
    if claimed.id != people_job.id:
        intelligence.complete(claimed.id, (), None)
        claimed = intelligence.claim_next_job()
    assert claimed is not None and claimed.id == people_job.id
    assert client.get(f"/api/gallery/people/assets/{first.id}/status").json()["job"] == {
        "id": str(people_job.id), "status": "processing", "error": None
    }
    intelligence.complete(claimed.id, (), None)
    intelligence.mark_people_status(claimed.id, "completed")
    assert client.get(f"/api/gallery/people/assets/{first.id}/status").json()["job"]["status"] == "completed"

    client.patch(f"/api/gallery/people/assets/{first.id}", json={"person_id": person["id"], "decision": "exclude"})
    assert client.get(f"/api/gallery?person={person['id']}").json() == []
    assert second.id != first.id


def test_gallery_people_status_reports_failure_and_explicit_retry(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "retry.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    queued = client.post(f"/api/gallery/people/assets/{asset.id}/analyse")
    assert queued.status_code == 202
    first = intelligence.claim_next_job()
    assert first is not None
    intelligence.complete(first.id, (), None)
    intelligence.mark_people_status(first.id, "failed", "People service unavailable")
    assert client.get(f"/api/gallery/people/assets/{asset.id}/status").json()["job"] == {
        "id": str(first.id), "status": "failed", "error": "People service unavailable"
    }

    retry = client.post(f"/api/gallery/people/assets/{asset.id}/analyse")
    assert retry.status_code == 202
    assert retry.json()["id"] != str(first.id)
    assert client.get(f"/api/gallery/people/assets/{asset.id}/status").json()["job"]["status"] == "queued"


def test_owner_can_identify_and_correct_one_specific_face_without_contaminating_manual_presence(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "group.jpg", b"unchanged-original")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    people = MemoryGalleryPeopleStore()
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    authenticate(client)

    owner = client.post("/api/gallery/people", json={"display_name": "Owner"}).json()
    anna = client.post("/api/gallery/people", json={"display_name": "Anna"}).json()
    michael = client.post("/api/gallery/people", json={"display_name": "Michael"}).json()
    first_face = people.add_face_detection(asset.id, bounding_box={"x": 10, "y": 20, "w": 30, "h": 40}, embedding=b"face-one", recognition_result="unknown")
    second_face = people.add_face_detection(asset.id, bounding_box={"x": 50, "y": 20, "w": 30, "h": 40}, embedding=b"face-two", recognition_result="unknown")

    details = client.get(f"/api/gallery/{scan_gallery(tmp_path)[0].id}").json()
    assert [face["id"] for face in details["face_detections"]] == [str(first_face), str(second_face)]
    assert details["face_detections"][0]["bounding_box"] == {"x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0}

    assert client.post(f"/api/gallery/people/assets/{asset.id}/faces/{first_face}/identify", json={"person_id": owner["id"], "decision": "include"}).status_code == 200
    assert client.post(f"/api/gallery/people/assets/{asset.id}/faces/{second_face}/identify", json={"person_id": anna["id"], "decision": "include"}).status_code == 200
    # Add person is intentionally photo-level only: it must not choose a face or reference embedding.
    assert client.patch(f"/api/gallery/people/assets/{asset.id}", json={"person_id": michael["id"], "decision": "include"}).status_code == 200
    assert people.face_detections[first_face]["reference_person_id"] == UUID(owner["id"])
    assert people.face_detections[second_face]["reference_person_id"] == UUID(anna["id"])
    assert michael["id"] not in {str(reference.person_id) for reference in people.reference_embeddings(TEST_USERNAME)}
    assert {person.display_name for person in people.effective_people(asset.id, TEST_USERNAME)} == {"Owner", "Anna", "Michael"}
    assert [row["name"] for row in client.get(f"/api/gallery?person={owner['id']}").json()] == ["group.jpg"]

    # A correction changes only the selected face; model evidence stays retained in the record.
    assert client.post(f"/api/gallery/people/assets/{asset.id}/faces/{first_face}/identify", json={"person_id": anna["id"], "decision": "include"}).status_code == 200
    assert people.face_detections[first_face]["reference_person_id"] == UUID(anna["id"])
    assert "recognition_result" in people.face_detections[first_face]
    assert {person.display_name for person in people.effective_people(asset.id, TEST_USERNAME)} == {"Anna", "Michael"}
    assert client.delete(f"/api/gallery/people/assets/{asset.id}/faces/{first_face}/identity").status_code == 204
    assert people.face_detections[first_face]["reference_person_id"] is None
    assert {person.display_name for person in people.effective_people(asset.id, TEST_USERNAME)} == {"Anna", "Michael"}
    assert image_path.read_bytes() == b"unchanged-original"


def test_later_face_identification_supersedes_exclusion_and_automatic_evidence_cannot(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "authority.jpg", b"unchanged-original")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    people = MemoryGalleryPeopleStore()
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    authenticate(client)

    owner = client.post("/api/gallery/people", json={"display_name": "Owner"}).json()
    ela = client.post("/api/gallery/people", json={"display_name": "Ela"}).json()
    example_owner_face = people.add_face_detection(asset.id, bounding_box={"x": 1}, embedding=b"owner")
    ela_face = people.add_face_detection(asset.id, bounding_box={"x": 2}, embedding=b"ela")
    # An older manual exclusion suppresses automated evidence.
    people.associate(asset.id, UUID(owner["id"]), "vault_master")
    assert client.patch(f"/api/gallery/people/assets/{asset.id}", json={"person_id": owner["id"], "decision": "exclude"}).status_code == 200
    assert people.effective_people(asset.id, TEST_USERNAME) == []

    # Identifying this exact face is later user authority: reference, canonical
    # association and effective photo-level inclusion are established together.
    assert client.post(
        f"/api/gallery/people/assets/{asset.id}/faces/{example_owner_face}/identify",
        json={"person_id": owner["id"], "decision": "include"},
    ).status_code == 200
    assert people.face_detections[example_owner_face]["reference_person_id"] == UUID(owner["id"])
    assert people.decisions[(asset.id, UUID(owner["id"]))] == "include"
    assert [person.display_name for person in people.effective_people(asset.id, TEST_USERNAME)] == ["Owner"]
    assert any(
        key == (asset.id, UUID(owner["id"]), "user_face", example_owner_face)
        for key in people.associations
    )

    # A later explicit removal wins again, and a model association cannot undo it.
    assert client.patch(f"/api/gallery/people/assets/{asset.id}", json={"person_id": owner["id"], "decision": "exclude"}).status_code == 200
    people.associate(asset.id, UUID(owner["id"]), "vault_master")
    assert people.effective_people(asset.id, TEST_USERNAME) == []
    # Other People and the original image are untouched by this authority change.
    assert client.post(
        f"/api/gallery/people/assets/{asset.id}/faces/{ela_face}/identify",
        json={"person_id": ela["id"], "decision": "include"},
    ).status_code == 200
    assert [person.display_name for person in people.effective_people(asset.id, TEST_USERNAME)] == ["Ela"]
    assert image_path.read_bytes() == b"unchanged-original"


def test_gallery_only_exposes_latest_successful_people_detection_run(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "rerun.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    authenticate(client)

    first = intelligence.queue(asset.id, TEST_USERNAME, people_only=True)
    intelligence.complete(first.id, (), None)
    intelligence.mark_people_status(first.id, "completed")
    first_faces = [people.add_face_detection(asset.id, producing_job_id=first.id, bounding_box={"x": index}) for index in range(3)]
    second = intelligence.queue(asset.id, TEST_USERNAME, force=True, people_only=True)
    intelligence.complete(second.id, (), None)
    intelligence.mark_people_status(second.id, "completed")
    second_faces = [people.add_face_detection(asset.id, producing_job_id=second.id, bounding_box={"x": index + 10}) for index in range(3)]

    image_id = scan_gallery(tmp_path)[0].id
    detail = client.get(f"/api/gallery/{image_id}").json()
    assert [face["id"] for face in detail["face_detections"]] == [str(face) for face in second_faces]
    assert detail["unknown_people_count"] == 3
    assert len(people.face_detections) == 6
    owner = client.post("/api/gallery/people", json={"display_name": "Owner"}).json()
    assert client.post(
        f"/api/gallery/people/assets/{asset.id}/faces/{first_faces[0]}/identify",
        json={"person_id": owner["id"], "decision": "include"},
    ).status_code == 404
    assert client.post(
        f"/api/gallery/people/assets/{asset.id}/faces/{second_faces[1]}/identify",
        json={"person_id": owner["id"], "decision": "include"},
    ).status_code == 200
    current = client.get(f"/api/gallery/{image_id}").json()["face_detections"]
    assert [face.get("person_name") for face in current] == [None, "Owner", None]
    assert people.face_detections[second_faces[1]]["reference_person_id"] == UUID(owner["id"])
    assert people.face_detections[second_faces[0]].get("reference_person_id") is None
    assert people.face_detections[second_faces[2]].get("reference_person_id") is None


def test_shared_gallery_detail_hides_face_boxes_and_unknown_people(client: TestClient, tmp_path: Path) -> None:
    image_path = create_image(tmp_path, "shared-face.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    store.catalogued_assets[asset.vault_path] = CataloguedAsset(**{**asset.__dict__, "visibility": "shared", "shared_with": ("son",)})
    people = MemoryGalleryPeopleStore()
    people.add_face_detection(asset.id, bounding_box={"x": 1, "y": 2, "w": 3, "h": 4})
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    app.dependency_overrides[require_authenticated_user] = lambda: synthetic_recipient()

    detail = client.get(f"/api/gallery/{scan_gallery(tmp_path)[0].id}").json()
    assert "face_detections" not in detail
    assert "unknown_people_count" not in detail


def test_shared_gallery_composes_effective_origin_metadata_and_recipient_annotations(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner metadata and recipient annotations use separate UUID authorities."""
    image_path = create_image(tmp_path, "shared-effective.jpg")
    store = configure_gallery(tmp_path)
    owner_id, recipient_id = uuid4(), uuid4()
    asset = CataloguedAsset(
        id=uuid4(), asset_type="Gallery", display_title="Recipient corrected title",
        captured_on=date(2020, 1, 2), location="Imported place",
        vault_path="/vault/Gallery/shared-effective.jpg", filename=image_path.name,
        size_bytes=image_path.stat().st_size, mime_type="image/jpeg", sha256="a" * 64,
        metadata={"captured_at": "2020-01-02T03:04:05+00:00"},
        metadata_provenance={},
        detected_metadata={"description": "Owner caption", "captured_at": "2020-01-02T03:04:05+00:00"},
        effective_metadata={"description": "Owner caption", "captured_at": "2020-01-02T03:04:05+00:00"},
        owner_username="recipient", owner_user_id=owner_id, visibility="shared",
        shared_with_user_ids=(recipient_id,),
    )
    asset = apply_catalogue_metadata_changes(
        asset,
        {
            "display_title": "Recipient corrected title",
            "captured_at": "2024-05-06T03:04:05+00:00",
            "location": "Recipient corrected place",
        },
    )
    store.restore_catalogued_asset(asset, "recipient")
    people = MemoryGalleryPeopleStore()
    intelligence = MemoryGalleryIntelligenceStore()
    intelligence.decide(asset.id, "photo_type", "portrait", "include", "recipient")
    intelligence.decide(asset.id, "content_tag", "outdoors", "include", "recipient")
    origin_person = people.create_person("recipient", "Owner", owner_id)
    local_person = people.create_person("recipient", "Owner", recipient_id)
    people.associate(asset.id, origin_person.id, "user")
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    recipient = Account(
        username="recipient", display_name="Recipient", email=None, password_hash="test",
        role="user", active=True, password_change_required=False,
        created_at=datetime.now(timezone.utc), last_sign_in_at=None, user_id=recipient_id,
    )
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(recipient)
    active = {"value": True}
    annotation: dict[str, object] = {"note": None, "tags": [], "people": []}

    class FakeShareGrantStore:
        def __init__(self, _conninfo: str) -> None:
            pass

        def included_gallery_assets(self, user_id: UUID) -> dict[UUID, str]:
            return {asset.id: "Recipient"} if active["value"] and user_id == recipient_id else {}

        def get_local_gallery_annotation(self, asset_uuid: UUID, user_id: UUID) -> dict[str, object] | None:
            return dict(annotation) if active["value"] and asset_uuid == asset.id and user_id == recipient_id else None

        def set_local_gallery_annotation(self, asset_uuid: UUID, user_id: UUID, *, note: str | None, tags: list[str], person_ids: list[UUID]) -> dict[str, object]:
            if not active["value"] or asset_uuid != asset.id or user_id != recipient_id or person_ids != [local_person.id]:
                raise ValueError("Shared Gallery photo is unavailable")
            annotation.update({"note": note, "tags": tags, "people": [{"id": str(local_person.id), "display_name": "Owner"}]})
            return dict(annotation)

    monkeypatch.setattr(gallery_module, "PostgresShareGrantStore", FakeShareGrantStore)
    monkeypatch.setattr(gallery_module, "get_database_conninfo", lambda: "test")
    image_id = scan_gallery(tmp_path)[0].id
    detail = client.get(f"/api/gallery/{image_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["display_title"] == "Recipient corrected title"
    assert body["captured_on"] == "2024-05-06"
    assert body["captured_at"] == "2024-05-06T03:04:05+00:00"
    assert body["location"] == "Recipient corrected place"
    assert body["description"] == "Owner caption"
    assert body["intelligence"] == [
        {"namespace": "photo_type", "slug": "portrait", "display_name": "Portrait"},
        {"namespace": "content_tag", "slug": "outdoors", "display_name": "Outdoors"},
    ]
    assert body["origin_people"] == [{"id": str(origin_person.id), "display_name": "Owner", "active": True}]
    assert body["local_annotation"] == {"tags": [], "people": []}
    assert "face_detections" not in body and "metadata_provenance" not in body and "sha256" not in body

    saved = client.put(
        f"/api/gallery/{image_id}/local-annotation",
        json={"note": "My note", "tags": ["recipient-tag"], "person_ids": [str(local_person.id)]},
    )
    assert saved.status_code == 200
    assert saved.json() == {"note": "My note", "tags": ["recipient-tag"], "people": [{"id": str(local_person.id), "display_name": "Owner", "active": True}]}
    assert people.effective_people(asset.id, owner_id)[0].person_id == origin_person.id
    assert people.effective_people(asset.id, recipient_id) == []

    # The canonical metadata API remains owner-only even though the photo is shared.
    assert client.patch(
        f"/api/vault-master/assets/{asset.id}/metadata", json={"display_title": "Recipient overwrite"}
    ).status_code in {403, 404}
    active["value"] = False
    assert client.get(f"/api/gallery/{image_id}").status_code == 404
    assert client.put(
        f"/api/gallery/{image_id}/local-annotation",
        json={"note": "must not restore access", "tags": [], "person_ids": []},
    ).status_code == 404


def test_gallery_face_identification_ui_uses_owner_face_detections_only_when_enabled() -> None:
    source = (Path(__file__).parents[2] / "src" / "routes" / "app.gallery.$photoId.tsx").read_text(encoding="utf-8")
    assert "Identify a face" in source
    assert "faceIdentification.active" in source
    assert "photo.face_detections" in source
    assert "/faces/${faceId}/identify" in source
    assert "Selected face:" in source
    assert "Identify selected face as" in source
    assert "Add person to photo" in source
    assert "It does not identify a specific face." in source
    assert "Create and identify face" in source
    assert "DropdownMenuTrigger" in source
    assert "Actions for ${person.display_name}" in source
    assert 'title="Remove from photo"' in source
    assert '{person.display_name}{" "}' not in source


def test_gallery_people_filter_uses_compact_searchable_selector() -> None:
    source = (Path(__file__).parents[2] / "src" / "routes" / "app.gallery.index.tsx").read_text(encoding="utf-8")
    assert 'aria-label="Search People"' in source
    assert 'placeholder="Search People"' in source
    assert "setPeopleOpen" in source
    assert ".sort((left, right) => left.display_name.localeCompare(right.display_name))" in source
    assert "selectedPeople.includes(entry.id)" in source
    assert "max-h-48" not in source
    assert "max-h-[calc(100dvh-2rem)]" in source


def test_owner_can_correct_gallery_intelligence_and_unincluded_shared_view_is_hidden(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "car.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    job = intelligence.queue(asset.id, TEST_USERNAME)
    intelligence.complete(job.id, (("photo_type", "vehicle"),), 0.01)
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    image_id = scan_gallery(tmp_path)[0].id
    authenticate(client)

    excluded = client.patch(
        f"/api/gallery/{image_id}/intelligence",
        json={"namespace": "photo_type", "slug": "vehicle", "decision": "exclude"},
    )
    assert excluded.status_code == 200 and excluded.json() == []
    included = client.patch(
        f"/api/gallery/{image_id}/intelligence",
        json={"namespace": "photo_type", "slug": "portrait", "decision": "include"},
    )
    assert {(term["namespace"], term["slug"]) for term in included.json()} == {("photo_type", "portrait")}

    store.catalogued_assets[asset.vault_path] = CataloguedAsset(
        **{**asset.__dict__, "visibility": "shared", "shared_with": ("son",)}
    )
    app.dependency_overrides[require_authenticated_user] = lambda: synthetic_recipient()
    detail = client.get(f"/api/gallery/{image_id}")
    assert detail.status_code == 404


def test_backfill_is_explicit_and_opening_gallery_never_queues_historical_assets(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "historical.jpg")
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    assert client.get("/api/gallery").status_code == 200
    assert not intelligence.jobs
    queued = client.post("/api/gallery/intelligence/backfill?limit=1")
    assert queued.status_code == 200
    assert queued.json()["queued"] == 1
    assert len(intelligence.jobs) == 1


def test_backfill_queues_missing_canonical_florence_evidence_without_arrival_reanalysis(
    client: TestClient, tmp_path: Path
) -> None:
    class MissingFlorenceEvidenceStore:
        def __init__(self) -> None:
            self.queued: list[tuple[UUID, UUID, str]] = []

        def latest_evidence(self, *_args: object) -> None:
            return None

        def active_or_latest_job(self, _asset_id: UUID) -> None:
            return None

        def queue(self, asset_id: UUID, owner_user_id: UUID, requested_by: str) -> object:
            self.queued.append((asset_id, owner_user_id, requested_by))
            return object()  # persisted queue contract returns the created job

    image_path = create_image(tmp_path, "missing-florence.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    florence = MissingFlorenceEvidenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    app.dependency_overrides[get_gallery_florence_store] = lambda: florence
    authenticate(client)

    response = client.post("/api/gallery/intelligence/backfill?limit=2")

    assert response.status_code == 200
    assert response.json()["queued"] == 2
    assert [job.asset_id for job in intelligence.jobs.values()] == [asset.id]
    assert florence.queued == [(asset.id, asset.owner_user_id, TEST_USERNAME)]


def test_gallery_backfill_status_and_historical_queue_are_owner_uuid_scoped(
    client: TestClient, tmp_path: Path, authentication_store: MemoryAuthenticationStore
) -> None:
    """Every owner receives the action status before a prior bulk run exists.

    The status and the explicit backfill must agree on the same owner UUID;
    neither Owner's administrator role nor either owner's historical run may
    make the other owner's photos visible or queueable.
    """
    owner = authentication_store.ensure_initial_administrator(TEST_USERNAME, "hash")
    recipient = Account(
        "recipient", "Recipient", None, "hash", "member", True, False,
        datetime.now(timezone.utc), None,
    )
    authentication_store.create_account(recipient)
    example_owner_path = create_image(tmp_path, "owner-historical.jpg")
    example_recipient_path = create_image(tmp_path, "recipient-historical.jpg")
    store = configure_gallery(tmp_path)
    example_owner_asset = catalogue_image(store, tmp_path, example_owner_path)
    example_recipient_asset = catalogue_image(store, tmp_path, example_recipient_path)
    store.catalogued_assets[example_owner_asset.vault_path] = replace(
        example_owner_asset, owner_user_id=owner.user_id
    )
    store.catalogued_assets[example_recipient_asset.vault_path] = replace(
        example_recipient_asset, owner_username="recipient", owner_user_id=recipient.user_id
    )
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence

    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(owner)
    assert client.get("/api/gallery/intelligence/status").status_code == 200
    assert client.get("/api/gallery/intelligence/backfill/status").json() == {
        "eligible_count": 1, "run": None
    }
    example_owner_backfill = client.post("/api/gallery/intelligence/backfill?limit=50")
    assert example_owner_backfill.status_code == 200
    assert [job.asset_id for job in intelligence.jobs.values()] == [example_owner_asset.id]

    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(recipient)
    # The global worker endpoint remains administrative, but owner action
    # status is available without a prior run and only counts Recipient's asset.
    assert client.get("/api/gallery/intelligence/status").status_code == 403
    assert client.get("/api/gallery/intelligence/backfill/status").json() == {
        "eligible_count": 1, "run": None
    }
    example_recipient_backfill = client.post("/api/gallery/intelligence/backfill?limit=50")
    assert example_recipient_backfill.status_code == 200
    assert {job.asset_id for job in intelligence.jobs.values()} == {example_owner_asset.id, example_recipient_asset.id}
    assert client.get("/api/gallery/intelligence/backfill/status").json()["run"] == example_recipient_backfill.json()["run"]


def test_bulk_backfill_progress_is_persisted_and_tracks_job_states(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "historical.jpg")
    second_path = create_image(tmp_path, "second.jpg")
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, image_path)
    catalogue_image(store, tmp_path, second_path)
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    queued = client.post("/api/gallery/intelligence/backfill?limit=2")
    assert queued.status_code == 200
    assert queued.json()["run"] == {
        "id": queued.json()["run"]["id"],
        "total": 2,
        "completed": 0,
        "processing": 0,
        "queued": 2,
        "failed": 0,
    }
    assert client.get("/api/gallery/intelligence/backfill/latest").json() == {"run": queued.json()["run"]}

    claimed = intelligence.claim_next_job()
    assert claimed is not None
    intelligence.complete(claimed.id, (), None)
    failed = intelligence.claim_next_job()
    assert failed is not None
    intelligence.fail(failed.id, "RAM++ unavailable")
    progress = client.get("/api/gallery/intelligence/backfill/latest").json()["run"]
    assert progress == {
        "id": queued.json()["run"]["id"],
        "total": 2,
        "completed": 1,
        "processing": 0,
        "queued": 0,
        "failed": 1,
    }


def test_completed_bulk_progress_is_transient_and_does_not_resurrect_from_history(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "historical.jpg")
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    queued = client.post("/api/gallery/intelligence/backfill?limit=1")
    job = intelligence.claim_next_job()
    assert queued.status_code == 200 and job is not None
    intelligence.complete(job.id, (), None)
    assert client.get("/api/gallery/intelligence/backfill/latest").json()["run"] is not None

    completed = intelligence.latest_job(job.asset_id)
    assert completed and completed.completed_at
    intelligence.jobs[completed.id] = replace(
        completed,
        completed_at=completed.completed_at - BULK_COMPLETION_GRACE - timedelta(seconds=1),
    )
    assert client.get("/api/gallery/intelligence/backfill/latest").json() == {"run": None}


def test_bulk_progress_reports_people_stage_failure_without_hiding_it_as_completed(
    client: TestClient, tmp_path: Path
) -> None:
    image_path = create_image(tmp_path, "people-failure.jpg")
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, image_path)
    intelligence = MemoryGalleryIntelligenceStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    queued = client.post("/api/gallery/intelligence/backfill?limit=1")
    job = intelligence.claim_next_job()
    assert queued.status_code == 200 and job is not None
    intelligence.complete(job.id, (), None)
    intelligence.mark_people_status(job.id, "failed", "People service unavailable")

    progress = client.get("/api/gallery/intelligence/backfill/latest").json()["run"]
    assert progress == {
        "id": queued.json()["run"]["id"],
        "total": 1,
        "completed": 0,
        "processing": 0,
        "queued": 0,
        "failed": 1,
    }


def test_owner_selected_gallery_reanalysis_queues_exactly_one_canonical_asset_job(
    client: TestClient, tmp_path: Path
) -> None:
    selected_path = create_image(tmp_path, "selected.jpg")
    other_path = create_image(tmp_path, "other.jpg")
    store = configure_gallery(tmp_path)
    selected = catalogue_image(store, tmp_path, selected_path)
    other = catalogue_image(store, tmp_path, other_path)
    intelligence = MemoryGalleryIntelligenceStore()
    intelligence.decide(selected.id, "photo_type", "portrait", "exclude")
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    authenticate(client)

    response = client.post(f"/api/gallery/intelligence/assets/{selected.id}/reanalyse")

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert len(intelligence.jobs) == 1
    job = next(iter(intelligence.jobs.values()))
    assert job.asset_id == selected.id
    assert job.asset_id != other.id
    assert intelligence.decisions[(selected.id, "photo_type", "portrait")] == "exclude"

    queued = client.get(f"/api/gallery/intelligence/assets/{selected.id}/status")
    assert queued.json()["job"] == {"id": str(job.id), "status": "queued", "error": None}
    claimed = intelligence.claim_next_job()
    assert claimed is not None
    assert client.get(f"/api/gallery/intelligence/assets/{selected.id}/status").json()["job"]["status"] == "processing"
    intelligence.complete(claimed.id, (("photo_type", "portrait"),), None)
    assert client.get(f"/api/gallery/intelligence/assets/{selected.id}/status").json()["job"]["status"] == "completed"
    retried = client.post(f"/api/gallery/intelligence/assets/{selected.id}/reanalyse")
    retry_job_id = retried.json()["id"]
    failed = intelligence.claim_next_job()
    assert failed is not None and str(failed.id) == retry_job_id
    intelligence.fail(failed.id, "Florence unavailable")
    assert client.get(f"/api/gallery/intelligence/assets/{selected.id}/status").json()["job"] == {
        "id": retry_job_id,
        "status": "failed",
        "error": "Florence unavailable",
    }


def test_gallery_details_remove_per_photo_analysis_and_keep_hide_restore() -> None:
    # Actual dialog behaviour is covered in tests/gallery-details.test.tsx.
    source = (Path(__file__).parents[2] / "src" / "routes" / "app.gallery.$photoId.tsx").read_text(encoding="utf-8")
    assert "Analyse photo" not in source
    assert "Analyse text / OCR" not in source
    assert "Use the arrow keys or viewer controls to move between photos." not in source
    assert 'photo.lifecycle_state === "hidden" ? "unhide" : "hide"' in source
    assert 'photo.lifecycle_state === "hidden" ? "Restore" : "Hide"' in source
    assert "Move to Bin" not in source
    assert "quarantine-" not in source


def test_hidden_gallery_assets_require_session_authorization(client: TestClient, tmp_path: Path, authentication_store) -> None:
    image_path = create_image(tmp_path, "hidden-photo.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    assert asset.owner_user_id is not None
    assert (
        store.set_catalogued_asset_lifecycle_state(
            asset.id, asset.owner_user_id, TEST_USERNAME, "hidden"
        )
        is not None
    )
    authenticate(client)

    image_id = scan_gallery(tmp_path)[0].id
    response = client.get(f"/api/gallery/{image_id}")
    assert response.status_code == 403
    assert client.get("/api/gallery?include_hidden=true").status_code == 403
    token = client.cookies.get("pv_session")
    assert token is not None
    hidden_status = client.get("/api/auth/hidden-photos/authorization")
    assert hidden_status.json() == {"authorized": False}
    assert hidden_status.headers["cache-control"] == "private, no-store"
    assert authentication_store.authorize_hidden_photos_session(token, asset.owner_user_id)
    assert client.get("/api/auth/hidden-photos/authorization").json() == {"authorized": True}
    assert client.get("/api/gallery").json() == []
    assert client.get("/api/gallery?include_hidden=true").status_code == 200
    response = client.get(f"/api/gallery/{image_id}")
    assert response.status_code == 200
    assert response.json()["lifecycle_state"] == "hidden"
    assert client.get(f"/api/gallery/{image_id}/content").status_code == 200
    assert client.get(f"/api/gallery/{image_id}/preview").status_code == 200
    client.post("/api/auth/logout")
    authenticate(client)
    assert client.get("/api/auth/hidden-photos/authorization").json() == {"authorized": False}
    assert client.get("/api/gallery?include_hidden=true").status_code == 403


def test_gallery_people_analysis_uses_shared_progress_and_polls_live_job_state() -> None:
    source = (
        Path(__file__).parents[2] / "src" / "routes" / "app.gallery.$photoId.tsx"
    ).read_text(encoding="utf-8")

    assert 'import { ActionProgress } from "@/components/pv/ActionProgress";' in source
    assert "const peopleAnalysisStatusLabel" in source
    assert 'processing: "Analysing faces"' in source
    assert "label={peopleAnalysisStatusLabel(status)}" in source
    assert "People: ${galleryIntelligenceStatusLabel(status)}" not in source
    assert "/api/gallery/people/assets/${photo.asset_id}/status" in source
    assert "timer = window.setTimeout(poll, 1500)" in source
    assert "window.clearTimeout(timer)" in source
    assert "await refresh();" in source
    assert "People analysis failed. This photo remains published." in source
    assert "const peopleAnalysisActive" in source
    assert "peopleAnalysisRequestInFlight.current" in source
    assert "disabled={busy || peopleAnalysisActive}" in source


def test_gallery_uses_permanent_catalogue_metadata(
    client: TestClient,
    tmp_path: Path,
) -> None:
    image_path = create_image(tmp_path, "Album/A.jpg", b"first")
    store = configure_gallery(tmp_path)
    scanned = scan_gallery(tmp_path)[0]
    store.catalogued_assets["/vault/Gallery/Album/A.jpg"] = CataloguedAsset(
        id=uuid4(),
        asset_type="Gallery",
        display_title="Corrected title",
        captured_on=date(1995, 9, 3),
        location="Starogard Gdanski, Polska",
        vault_path="/vault/Gallery/Album/A.jpg",
        filename=image_path.name,
        size_bytes=image_path.stat().st_size,
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={
            "display_title": "user_override",
            "captured_on": "user_override",
            "location": "user_override",
        },
        owner_username=TEST_USERNAME,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),
    )
    authenticate(client)

    listing = client.get("/api/gallery").json()
    details = client.get(f"/api/gallery/{scanned.id}").json()

    for photo in (listing[0], details):
        assert photo["display_title"] == "Corrected title"
        assert photo["captured_on"] == "1995-09-03"
        assert photo["date_source"] == "user_override"
        assert photo["location"] == "Starogard Gdanski, Polska"


def test_unincluded_shared_gallery_details_are_not_discoverable(
    client: TestClient,
    tmp_path: Path,
) -> None:
    image_path = create_image(tmp_path, "shared.jpg")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, image_path)
    store.catalogued_assets[asset.vault_path] = CataloguedAsset(
        **{
            **asset.__dict__,
            "visibility": "shared",
            "shared_with": ("son",),
        }
    )
    app.dependency_overrides[require_authenticated_user] = lambda: synthetic_recipient()
    image_id = scan_gallery(tmp_path)[0].id

    response = client.get(f"/api/gallery/{image_id}")

    assert response.status_code == 404


def test_gallery_content_is_private_inline_image(
    client: TestClient,
    tmp_path: Path,
) -> None:
    image_path = create_image(
        tmp_path, "owned-photo.JPEG", b"private-image"
    )
    store = configure_gallery(tmp_path)
    catalogue_image(store, tmp_path, image_path)
    authenticate(client)
    image_id = scan_gallery(tmp_path)[0].id

    response = client.get(f"/api/gallery/{image_id}/content")

    assert response.status_code == 200
    assert response.content == b"private-image"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("inline")
    assert str(tmp_path) not in response.text


def test_gallery_renders_a_private_pdf_first_page_preview(
    client: TestClient,
    tmp_path: Path,
) -> None:
    pdf_path = create_pdf(tmp_path, "family-photo.pdf")
    store = configure_gallery(tmp_path)
    asset = catalogue_image(store, tmp_path, pdf_path)
    store.catalogued_assets[asset.vault_path] = CataloguedAsset(
        **{**asset.__dict__, "mime_type": "application/pdf"}
    )
    authenticate(client)
    image_id = scan_gallery(tmp_path)[0].id

    response = client.get(f"/api/gallery/{image_id}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content.startswith(b"\xff\xd8")


def test_unknown_photo_returns_not_found(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_gallery(tmp_path)
    authenticate(client)

    response = client.get("/api/gallery/not-a-real-photo")

    assert response.status_code == 404
    assert response.json() == {"detail": "Photo was not found"}


def test_missing_gallery_storage_returns_service_unavailable(
    client: TestClient,
    tmp_path: Path,
) -> None:
    app.dependency_overrides[get_gallery_path] = (
        lambda: tmp_path / "missing"
    )
    authenticate(client)

    response = client.get("/api/gallery")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Gallery storage is unavailable"
    }


@pytest.mark.parametrize("sort", ["oldest", "newest"])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("filter_kind", [None, "photo_type", "content_tag", "person"])
def test_viewer_navigation_matches_gallery_scope(
    client: TestClient, tmp_path: Path, authentication_store, sort, hidden, filter_kind
) -> None:
    store = configure_gallery(tmp_path)
    intelligence = MemoryGalleryIntelligenceStore()
    people = MemoryGalleryPeopleStore()
    app.dependency_overrides[get_gallery_intelligence_store] = lambda: intelligence
    app.dependency_overrides[get_gallery_people_store] = lambda: people
    assets = []
    for index in range(6):
        path = create_image(tmp_path, f"scope-{index}.jpg")
        asset = catalogue_image(store, tmp_path, path, captured_on=date(2024, 1, index + 1))
        if index % 2:
            store.set_catalogued_asset_lifecycle_state(asset.id, asset.owner_user_id, TEST_USERNAME, "hidden")
        assets.append(asset)
    person = people.create_person(TEST_USERNAME, "Synthetic person", assets[0].owner_user_id)
    for asset in assets[:4]:
        intelligence.decide(asset.id, "photo_type", "portrait", "include", TEST_USERNAME)
        intelligence.decide(asset.id, "content_tag", "outdoors", "include", TEST_USERNAME)
        people.associate(asset.id, person.id, "user")
    authenticate(client)
    token = client.cookies.get("pv_session")
    assert authentication_store.authorize_hidden_photos_session(token, assets[0].owner_user_id)
    params = {"sort": sort, "include_hidden": str(hidden).lower()}
    if filter_kind:
        params[filter_kind] = {"photo_type": "portrait", "content_tag": "outdoors", "person": str(person.id)}[filter_kind]
    response = client.get("/api/gallery", params=params)
    assert response.status_code == 200
    cards = response.json()
    expected = [f"scope-{i}.jpg" for i in range(6) if bool(i % 2) == hidden and (not filter_kind or i < 4)]
    if sort == "newest":
        expected.reverse()
    assert [card["name"] for card in cards] == expected
    for index, card in enumerate(cards):
        detail = client.get(f"/api/gallery/{card['id']}", params=params)
        assert detail.status_code == 200
        body = detail.json()
        assert body["previous_id"] == (cards[index - 1]["id"] if index else None)
        assert body["next_id"] == (cards[index + 1]["id"] if index + 1 < len(cards) else None)
    # A direct authorized hidden detail has no normal-Gallery neighbours.
    hidden_id = next(image.id for image in scan_gallery(tmp_path) if image.name == "scope-1.jpg")
    direct = client.get(f"/api/gallery/{hidden_id}")
    assert direct.status_code == 200
    assert direct.json()["previous_id"] is None
    assert direct.json()["next_id"] is None
    # An active asset cannot enter a Hidden Photos sequence via a direct ID.
    active_id = next(image.id for image in scan_gallery(tmp_path) if image.name == "scope-0.jpg")
    assert client.get(f"/api/gallery/{active_id}?include_hidden=true").status_code == 404


def test_viewer_navigation_preserves_route_scope() -> None:
    source = (Path(__file__).parents[2] / "src/routes/app.gallery.$photoId.tsx").read_text(encoding="utf-8")
    # Swipe, keyboard arrows, and both desktop/full-screen links must retain scope.
    assert source.count("search: { sort, photo_type, content_tag, person, private_tag, hidden },") == 3
    navigation = source[source.index("function PhotoNavigation("):]
    assert "search={{ sort, photo_type, content_tag, person, private_tag, hidden }}" in navigation
    assert "photo_type: []" not in navigation
