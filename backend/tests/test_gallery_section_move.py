from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

from app.auth import AuthenticatedIdentity, require_authenticated_user
from app.auth_store import Account, PostgresAuthenticationStore
from app.gallery_section_move import SectionMoves, eligible_destinations
from app.gallery_custom_tags import PostgresGalleryCustomTagStore
from app.main import app
from app.vault_master import CataloguedAsset, PostgresVaultMasterStore, get_vault_master_store, sha256_file
from app.vault_libraries import get_documents_path, get_archives_path


@pytest.fixture
def move_setup(tmp_path, monkeypatch, client):
    database = os.getenv("PV_TEST_DATABASE_URL")
    if not database:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    schema = "details_" + uuid4().hex
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conninfo = make_conninfo(database, options=f"-c search_path={schema}")
    auth = PostgresAuthenticationStore(conninfo); auth.initialize()
    account = Account("details-owner", "Synthetic owner", "details@example.test", None, "user", True, False, datetime.now(timezone.utc), None)
    auth.create_account(account)
    store = PostgresVaultMasterStore(conninfo); store.initialize()
    worker = SectionMoves(store); worker.initialize()
    gallery, slot, documents, archives = [tmp_path / name for name in ("gallery", "slot", "documents", "archives")]
    for path in (gallery, slot, documents, archives): path.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda path: path == slot)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": "personal-vault.slot-managed-manifest.v1", "slots": {"PV-DEV-DISK-001": {"state": "active", "integration_mode": "slot_managed", "areas": ["Gallery", "Documents", "Archives", "Library"], "logical_mappings": {area: f"/vault/{area}" for area in ("Gallery", "Documents", "Archives", "Library")}}}}))
    monkeypatch.setenv("PV_SECTION_MOVE_MANIFEST", str(manifest))
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({"PV-DEV-DISK-001": str(slot)}))
    monkeypatch.setenv("PV_GALLERY_PATH", str(gallery))
    source = gallery / "receipt.jpg"; source.write_bytes(b"synthetic image content")
    asset = CataloguedAsset(id=uuid4(), asset_type="Gallery", display_title="Manual title", captured_on=date(2024, 2, 3), location="Synthetic location", vault_path="/vault/Gallery/receipt.jpg", filename=source.name, size_bytes=source.stat().st_size, mime_type="image/jpeg", sha256=sha256_file(source), metadata={"preserved": True}, metadata_provenance={"display_title": "user_override"}, user_overrides={"display_title": "Manual title"}, owner_username=account.username, owner_user_id=account.user_id)
    store.restore_catalogued_asset(asset, AuthenticatedIdentity(account))
    asset = store.get_catalogued_asset_by_id(asset.id)
    original = dict(app.dependency_overrides)
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(account)
    app.dependency_overrides[get_documents_path] = lambda: documents
    app.dependency_overrides[get_archives_path] = lambda: archives
    try:
        yield store, worker, asset, source, slot, account, conninfo
    finally:
        app.dependency_overrides.clear(); app.dependency_overrides.update(original)
        with psycopg.connect(database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("destination", ["Documents", "Archives"])
def test_real_route_worker_preserves_identity_and_destination_access(move_setup, destination):
    store, worker, asset, source, slot, account, conninfo = move_setup
    tags = PostgresGalleryCustomTagStore(conninfo); tags.initialize()
    tag = tags.create(account.user_id, "Synthetic private tag"); tags.assign(account.user_id, tag.id, asset.id)
    # Existing linked metadata/history is retained, never copied into a new asset.
    from app.gallery_people import PostgresGalleryPeopleStore
    from app.gallery_intelligence import PostgresGalleryIntelligenceStore
    PostgresGalleryIntelligenceStore(conninfo).initialize()
    people = PostgresGalleryPeopleStore(conninfo); people.initialize()
    face = people.add_face_detection(asset.id, bounding_box={"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.3})
    person = people.create_person(account.username, "Synthetic person", owner_user_id=account.user_id)
    people.associate(asset.id, person.id, "user", face_detection_id=face, created_by=account.username)
    store.update_catalogued_asset_access(asset.id, "shared", (), AuthenticatedIdentity(account), local_all=True)
    with store._connect() as connection:
        grants_before = connection.execute("SELECT * FROM vault_share_grants WHERE asset_id=%s ORDER BY grant_id", (asset.id,)).fetchall()
    assert grants_before
    before = store.get_catalogued_asset_by_id(asset.id)
    client = TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"})
    base = f"/api/vault-master/assets/{asset.id}/section-move"
    assert client.get(base + "/destinations").json() == {"destinations": ["Documents", "Archives"]}
    assert client.post(base + "/preflight", json={"destination": destination}).json() == {"ready": True}
    assert source.exists()
    response = client.post(base, json={"destination": destination, "confirm": True})
    assert response.status_code == 202
    operation_id = response.json()["operation_id"]
    assert worker.process_next()
    assert client.get(base + "/" + operation_id).json()["status"] == "completed"
    after = store.get_catalogued_asset_by_id(asset.id)
    assert after.id == before.id and after.owner_user_id == before.owner_user_id
    assert after.asset_type == destination and after.sha256 == before.sha256
    assert after.display_title == before.display_title and after.captured_on == before.captured_on
    assert after.location == before.location and after.user_overrides == before.user_overrides
    assert after.metadata_provenance == before.metadata_provenance
    assert after.metadata["preserved"] is True
    assert after.visibility == before.visibility and after.shared_with_user_ids == before.shared_with_user_ids
    assert tags.for_asset(account.user_id, asset.id)[0].id == tag.id
    assert people.effective_people(asset.id, account.user_id)[0].person_id == person.id
    with store._connect() as connection:
        assert connection.execute("SELECT * FROM vault_share_grants WHERE asset_id=%s ORDER BY grant_id", (asset.id,)).fetchall() == grants_before
        assert connection.execute("SELECT count(*) AS n FROM vault_assets").fetchone()["n"] == 1
        assert connection.execute("SELECT count(*) AS n FROM vault_master_items").fetchone()["n"] == 0
        assert connection.execute("SELECT count(*) AS n FROM vault_face_detections WHERE id=%s AND asset_id=%s", (face, asset.id)).fetchone()["n"] == 1
    assert "asset_section_moved" in [entry["action"] for entry in store.list_catalogued_asset_history(asset.id)]
    placement = after.metadata["storage_placement"]
    assert placement["slot_id"] == "PV-DEV-DISK-001"
    assert (slot / placement["relative_path"]).read_bytes() == b"synthetic image content"
    assert not source.exists()
    listing = client.get(f"/api/{destination.lower()}")
    assert listing.status_code == 200 and listing.json()[0]["id"] == str(asset.id)
    assert client.get(listing.json()[0]["open_url"]).content == b"synthetic image content"
    # A restart between retirement and final status is idempotent.
    with store._connect() as connection:
        connection.execute("UPDATE vault_section_moves SET status='committed' WHERE id=%s", (UUID(operation_id),))
    assert worker.process_next()
    assert client.get(base + "/" + operation_id).json()["status"] == "completed"


def test_destination_validation_collision_and_owner_authority(move_setup):
    store, worker, asset, source, slot, account, _ = move_setup
    client = TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}); base = f"/api/vault-master/assets/{asset.id}/section-move"
    for destination in ("Reading Room", "Documents / Reading Room", "/vault/Documents", "../Documents", "Music", "Gallery"):
        assert client.post(base, json={"destination": destination, "confirm": True}).status_code == 409
    assert client.post(base, json={"destination": "Documents", "destination_folder": "/tmp", "confirm": True}).status_code == 422
    assert client.post(base, json={"destination": "Documents"}).status_code == 422
    assert eligible_destinations(replace(asset, lifecycle_state="hidden")) == []
    snapshot = worker.preflight(asset, "Documents")
    collision = slot / snapshot["relative_path"]; collision.parent.mkdir(parents=True); collision.write_bytes(b"do not overwrite")
    assert client.post(base, json={"destination": "Documents", "confirm": True}).status_code == 409
    assert collision.read_bytes() == b"do not overwrite" and source.exists()
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(replace(account, user_id=uuid4()))
    assert client.get(base + "/destinations").status_code == 404


def test_worker_rechecks_source_and_collision_after_confirmation(move_setup):
    store, worker, asset, source, slot, account, _ = move_setup
    job = worker.enqueue(asset, "Documents")
    snapshot = worker.preflight(asset, "Documents")
    target = slot / snapshot["relative_path"]; target.parent.mkdir(parents=True); target.write_bytes(b"other")
    worker.process_next()
    assert worker.status(asset.id, UUID(job["operation_id"]), account.user_id)["status"] == "failed"
    assert target.read_bytes() == b"other" and source.exists()
    assert store.get_catalogued_asset_by_id(asset.id).asset_type == "Gallery"


def test_existing_managed_source_uses_same_file_record_and_new_placement(move_setup):
    from psycopg.types.json import Jsonb
    store, worker, asset, legacy, slot, account, _ = move_setup
    managed = slot / "Gallery" / asset.filename
    managed.parent.mkdir(); managed.write_bytes(legacy.read_bytes())
    placement = {"slot_id": "PV-DEV-DISK-001", "relative_path": f"Gallery/{asset.filename}"}
    with store._connect() as connection:
        file_id = connection.execute("SELECT id FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchone()["id"]
        connection.execute("INSERT INTO vault_storage_slots(slot_id,state,assigned_areas) VALUES ('PV-DEV-DISK-001','active','[\"Gallery\",\"Documents\"]')")
        connection.execute("INSERT INTO vault_file_storage_placements(file_id,slot_id,relative_path,assigned_by,placement_reason) VALUES (%s,%s,%s,'synthetic','test')", (file_id, placement["slot_id"], placement["relative_path"]))
        connection.execute("UPDATE vault_assets SET metadata=metadata || %s WHERE id=%s", (Jsonb({"storage_placement": placement}), asset.id))
    asset = store.get_catalogued_asset_by_id(asset.id)
    job = worker.enqueue(asset, "Archives"); worker.process_next()
    assert worker.status(asset.id, UUID(job["operation_id"]), account.user_id)["status"] == "completed"
    after = store.get_catalogued_asset_by_id(asset.id)
    assert not managed.exists()
    assert legacy.exists()  # A non-authoritative copy is never treated as source.
    assert after.id == asset.id and after.owner_user_id == asset.owner_user_id
    with store._connect() as connection:
        assert connection.execute("SELECT id FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchone()["id"] == file_id
        assert connection.execute("SELECT relative_path FROM vault_file_storage_placements WHERE file_id=%s", (file_id,)).fetchone()["relative_path"] == after.metadata["storage_placement"]["relative_path"]


def test_changed_source_fails_closed(move_setup):
    store, worker, asset, source, slot, account, _ = move_setup
    job = worker.enqueue(asset, "Documents")
    source.write_bytes(b"changed source")
    worker.process_next()
    assert worker.status(asset.id, UUID(job["operation_id"]), account.user_id)["status"] == "failed"
    assert source.read_bytes() == b"changed source"
    assert store.get_catalogued_asset_by_id(asset.id).vault_path == asset.vault_path
