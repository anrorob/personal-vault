"""Synthetic adoption and logical placement regression coverage."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from app.documents_storage_migration import VERSION, copy_files, inspect_files, registration_documents, catalogue
from app.storage_placement import commissioned_destination_slots, resolve_metadata_placement
from tests.test_postgres_vault_master import postgres_store, postgres_conninfo, _catalogued_asset, _arrival_hall_owner_user_id


@pytest.fixture
def files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    manifest = {"version": VERSION, "environment": "development", "vault_id": str(uuid4()),
                "slot_id": "PV-DEV-DISK-091", "filesystem_uuid": "synthetic-filesystem",
                "previous_assigned_areas": ["Archives"], "files": []}
    for i in range(2):
        name = f"test-document-{i}.pdf"
        content = f"synthetic document {i}".encode()
        (source / name).write_bytes(content)
        manifest["files"].append({"asset_id": str(uuid4()), "file_id": str(uuid4()), "owner_user_id": str(uuid4()),
                                  "vault_path": "/vault/Documents/" + name, "filename": name,
                                  "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    return manifest, source, target


def test_copy_is_verified_retryable_and_does_not_remove_sources(files):
    manifest, source, target = files
    copy_files(manifest, source, target)
    inodes = [(target / "Documents" / row["filename"]).stat().st_ino for row in manifest["files"]]
    copy_files(manifest, source, target)
    assert inodes == [(target / "Documents" / row["filename"]).stat().st_ino for row in manifest["files"]]
    assert all((source / row["filename"]).is_file() for row in manifest["files"])
    inspect_files(manifest, source, target, require_copies=True)


def test_collision_and_bad_checksum_fail_before_copy(files):
    manifest, source, target = files
    (source / manifest["files"][0]["filename"]).write_bytes(b"changed")
    with pytest.raises(ValueError): copy_files(manifest, source, target)
    assert not list(target.iterdir())


def test_existing_target_directory_is_never_adopted_implicitly(files):
    manifest, source, target = files
    (target / "Documents").mkdir()
    with pytest.raises(ValueError, match="separate review"):
        copy_files(manifest, source, target)


def test_interrupted_copy_can_resume_without_overwriting_verified_file(files, monkeypatch):
    import app.documents_storage_migration as migration
    manifest, source, target = files
    original = migration.shutil.copyfileobj
    count = 0
    def interrupted(incoming, outgoing):
        nonlocal count
        count += 1
        if count == 2:
            outgoing.write(b"incomplete")
            raise OSError("synthetic interruption")
        original(incoming, outgoing)
    monkeypatch.setattr(migration.shutil, "copyfileobj", interrupted)
    with pytest.raises(OSError, match="interruption"):
        copy_files(manifest, source, target)
    first = target / "Documents" / manifest["files"][0]["filename"]
    inode = first.stat().st_ino
    monkeypatch.setattr(migration.shutil, "copyfileobj", original)
    copy_files(manifest, source, target)
    assert first.stat().st_ino == inode
    assert not list(target.rglob("*.part"))


def test_wrong_environment_and_path_fail_closed(files):
    manifest, source, target = files
    manifest["environment"] = "production"
    with pytest.raises(ValueError, match="environment"):
        copy_files(manifest, source, target)
    manifest["environment"] = "development"
    manifest["files"][0]["vault_path"] = "/vault/Documents/../test-document-0.pdf"
    with pytest.raises(ValueError):
        copy_files(manifest, source, target)
    assert not list(target.iterdir())


def test_registration_is_idempotent_and_distinct_from_reading_room(files, monkeypatch, tmp_path):
    manifest, source, target = files
    slot_id = manifest["slot_id"]
    config = {"disks": [{"id": slot_id, "state": "Active", "integration_mode": "slot_managed",
                         "uuid": "synthetic-filesystem", "hardware_id": "synthetic-hardware",
                         "areas": {"Archives": "/vault/Archives", "Library": "/vault/Library"}}]}
    authority = {"schema": "personal-vault.slot-managed-manifest.v1", "slots": {slot_id: {
        "state": "active", "integration_mode": "slot_managed", "filesystem_uuid": "synthetic-filesystem",
        "hardware_id": "synthetic-hardware", "areas": ["Archives", "Library"],
        "logical_mappings": {"Archives": "/vault/Archives", "Library": "/vault/Library"}}}}
    before = deepcopy(authority)
    path = tmp_path / "manifest.json"; path.write_text(json.dumps(authority))
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({slot_id: str(target)}))
    with pytest.raises(ValueError): commissioned_destination_slots("Documents", 1, path)
    updated, mapped = registration_documents(config, authority, manifest)
    assert registration_documents(updated, mapped, manifest) == (updated, mapped)
    assert authority == before
    assert mapped["slots"][slot_id]["logical_mappings"]["Library"] == "/vault/Library"
    assert "Reading Room" not in mapped["slots"][slot_id]["logical_mappings"]
    path.write_text(json.dumps(mapped))
    with pytest.raises(ValueError): commissioned_destination_slots("Documents", 1, path)
    monkeypatch.setattr(Path, "is_mount", lambda path: path.is_dir())
    assert commissioned_destination_slots("Documents", 1, path)[0].slot_id == slot_id
    other = tmp_path / "replacement-slot"; other.mkdir()
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({slot_id: str(other)}))
    assert commissioned_destination_slots("Documents", 1, path)[0].managed_root == other
    other.rmdir()
    with pytest.raises(ValueError): commissioned_destination_slots("Documents", 1, path)


def test_postgres_cutover_rollback_and_identity(files, postgres_store, postgres_conninfo, monkeypatch):
    manifest, source, target = files
    manifest["vault_id"] = str(postgres_store.get_local_vault_id())
    owner = _arrival_hall_owner_user_id(postgres_conninfo)
    slot_id = manifest["slot_id"]
    try:
        with psycopg.connect(postgres_conninfo) as c:
            c.execute("INSERT INTO vault_storage_slots(slot_id,state,hardware,assigned_areas) VALUES(%s,'active',%s,%s)",
                      (slot_id, Jsonb({"filesystem_uuid": manifest["filesystem_uuid"]}), Jsonb(["Archives"])))
        for row in manifest["files"]:
            asset = postgres_store.restore_catalogued_asset(replace(
                _catalogued_asset(uuid4(), row["vault_path"], "owner"), asset_type="Documents", mime_type="application/pdf",
                filename=row["filename"], size_bytes=row["size_bytes"], sha256=row["sha256"],
                owner_user_id=owner, origin_vault_id=postgres_store.get_local_vault_id()), "owner")
            with psycopg.connect(postgres_conninfo, row_factory=dict_row) as c:
                record = c.execute("""SELECT a.id asset_id,f.id file_id,a.owner_user_id,
                    md5(jsonb_build_array(a.metadata-'storage_placement',a.detected_metadata,a.imported_metadata,
                        a.user_overrides,a.effective_metadata,a.metadata_provenance,a.visibility,a.shared_with)::text) state_fingerprint
                    FROM vault_assets a JOIN vault_files f ON f.asset_id=a.id WHERE a.id=%s""", (asset.id,)).fetchone()
                row.update({k: str(v) for k, v in record.items()})
        assert catalogue(postgres_conninfo, manifest, source, target)["files"] == 2
        copy_files(manifest, source, target)
        changed = deepcopy(manifest)
        changed["files"][-1]["state_fingerprint"] = "changed"
        with pytest.raises(ValueError, match="metadata changed"):
            catalogue(postgres_conninfo, changed, source, target, phase="place")
        with psycopg.connect(postgres_conninfo) as c:
            assert c.execute("SELECT count(*) FROM vault_file_storage_placements WHERE slot_id=%s", (slot_id,)).fetchone()[0] == 0
        assert not catalogue(postgres_conninfo, manifest, source, target, phase="place")["already_placed"]
        assert catalogue(postgres_conninfo, manifest, source, target, phase="place")["already_placed"]
        monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({slot_id: str(target)}))
        for row in manifest["files"]:
            asset = postgres_store.get_catalogued_asset_by_id(UUID(row["asset_id"]))
            assert str(asset.id) == row["asset_id"] and str(asset.owner_user_id) == row["owner_user_id"]
            assert asset.sha256 == row["sha256"]
            assert resolve_metadata_placement(asset.metadata) == target / "Documents" / row["filename"]
            with psycopg.connect(postgres_conninfo) as c:
                assert str(c.execute("SELECT id FROM vault_files WHERE asset_id=%s AND file_role='primary'", (asset.id,)).fetchone()[0]) == row["file_id"]
        catalogue(postgres_conninfo, manifest, source, target, phase="rollback")
        assert not catalogue(postgres_conninfo, manifest, source, target, phase="rollback")["already_placed"]
        with psycopg.connect(postgres_conninfo) as c:
            assert c.execute("SELECT count(*) FROM vault_file_storage_placements WHERE slot_id=%s", (slot_id,)).fetchone()[0] == 0
    finally:
        # Assets are managed by the enclosing disposable DB fixture; remove only
        # this test's placement/slot records, including after a failing assertion.
        with psycopg.connect(postgres_conninfo) as c:
            c.execute("DELETE FROM vault_file_storage_placements WHERE slot_id=%s", (slot_id,))
            c.execute("DELETE FROM vault_storage_slots WHERE slot_id=%s", (slot_id,))
