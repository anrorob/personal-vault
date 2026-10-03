import json

from fastapi.testclient import TestClient
from app.auth import AuthenticatedIdentity
from app.main import app
from app.gallery_custom_tags import PostgresGalleryCustomTagStore
from tests.test_gallery_section_move import move_setup
from tests.test_postgres_vault_master import postgres_store, postgres_conninfo, _arrival_hall_owner_user_id


def test_real_managed_move_preserves_canonical_identity_and_relationships(move_setup, monkeypatch):
    store, worker, original, source, slot, account, conninfo = move_setup
    import os
    from pathlib import Path
    manifest = Path(os.environ["PV_SECTION_MOVE_MANIFEST"])
    document = json.loads(manifest.read_text())
    spec = document["slots"]["PV-DEV-DISK-001"]
    spec["areas"].append("Music")
    spec["logical_mappings"]["Music"] = "/vault/Music"
    manifest.write_text(json.dumps(document))
    monkeypatch.setenv("PV_HOME_VIDEOS_PATH", str(source.parent))
    home_path = "/vault/Home Videos/" + source.name
    with store._connect() as conn:
        conn.execute("UPDATE vault_assets SET asset_type='Home Videos' WHERE id=%s", (original.id,))
        conn.execute("UPDATE vault_files SET vault_path=%s,mime_type='video/mp4' WHERE asset_id=%s", (home_path, original.id))
    before = store.get_catalogued_asset_by_id(original.id)
    tags = PostgresGalleryCustomTagStore(conninfo)
    tags.initialize()
    tag = tags.create(account.user_id, "Synthetic retained tag")
    tags.assign(account.user_id, tag.id, original.id)
    store.update_catalogued_asset_access(original.id, "shared", (), AuthenticatedIdentity(account), local_all=True)
    with store._connect() as conn:
        grants = conn.execute("SELECT * FROM vault_share_grants WHERE asset_id=%s", (original.id,)).fetchall()
        files = conn.execute("SELECT id FROM vault_files WHERE asset_id=%s", (original.id,)).fetchall()
    client = TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"})
    base = f"/api/vault-master/assets/{original.id}/section-move"
    assert client.get(base + "/destinations").json() == {"destinations": ["Music Videos"]}
    assert client.post(base + "/preflight", json={"destination": "Music Videos"}).json() == {"ready": True}
    assert client.post(base, json={"destination": "Music Videos", "confirm": True}).status_code == 202
    assert worker.process_next()
    after = store.get_catalogued_asset_by_id(original.id)
    assert after.asset_type == "Music Videos"
    assert after.vault_path.startswith("/vault/Music/Music Videos/")
    assert (after.id, after.owner_user_id, after.sha256, after.captured_on, after.location, after.user_overrides) == (
        before.id, before.owner_user_id, before.sha256, before.captured_on, before.location, before.user_overrides)
    assert after.metadata["preserved"] is True
    assert not source.exists()
    assert (slot / after.metadata["storage_placement"]["relative_path"]).is_file()
    assert store.list_catalogued_assets_by_vault_path_prefix("/vault/Home Videos/") == []
    listed = client.get("/api/music-videos").json()
    assert [item["asset_id"] for item in listed] == [str(original.id)]
    assert client.patch(f"/api/music-videos/{original.id}/metadata", json={"artist": "Corrected band", "title": "Corrected video"}).status_code == 200
    with store._connect() as conn:
        assert conn.execute("SELECT * FROM vault_share_grants WHERE asset_id=%s", (original.id,)).fetchall() == grants
        assert conn.execute("SELECT id FROM vault_files WHERE asset_id=%s", (original.id,)).fetchall() == files
        assert conn.execute("SELECT count(*) AS n FROM vault_assets WHERE id=%s", (original.id,)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM vault_asset_history WHERE asset_id=%s AND action='asset_section_moved'", (original.id,)).fetchone()["n"] == 1
    assert tags.for_asset(account.user_id, original.id)


def test_music_video_receipt_publishes_once_without_audio_membership(postgres_store, postgres_conninfo):
    from datetime import datetime, timezone
    from uuid import uuid4
    from app.vault_master import ScannedFile
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    item = postgres_store.record_file(
        postgres_store.create_batch("incoming", "/synthetic"), "incoming",
        ScannedFile("/synthetic/test.mp4", "test.mp4", "test.mp4", 10, "video/mp4",
                    datetime.now(timezone.utc), "1" * 64,
                    {"source_context": {"content_type": "music_video", "artist": "Band", "title": "Title"}},
                    owner_username="owner", owner_user_id=owner))
    assert item.state == "move_queued"
    request_id = uuid4()
    postgres_store.claim_next_move()
    postgres_store.mark_theatre_promotion_pending(item.id, request_id)
    receipt = {"request_id": str(request_id), "item_id": str(item.id), "owner_user_id": str(owner),
               "logical_destination": item.proposed_destination, "logical_area": "Music",
               "slot_id": "PV-DEV-DISK-001", "relative_path": item.proposed_destination.removeprefix("/vault/"),
               "expected_sha256": item.sha256, "expected_size_bytes": item.size_bytes}
    assert postgres_store.publish_arrival_managed_receipt(item.id, {**receipt, "owner_user_id": str(uuid4())}) is None
    asset = postgres_store.publish_arrival_managed_receipt(item.id, receipt)
    assert asset.asset_type == "Music Videos" and asset.owner_user_id == owner
    assert asset.visibility == "vault-wide"
    assert asset.metadata["storage_placement"]["relative_path"] == receipt["relative_path"]
    assert postgres_store.get_asset_music_album(asset.id) is None
    postgres_store.publish_arrival_managed_receipt(item.id, receipt)
    with postgres_store._connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM vault_files WHERE asset_id=%s", (asset.id,)).fetchone()["n"] == 1
