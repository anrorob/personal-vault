from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.music_groups import supplier_manual_album, declared_album
from app.music_imports import approve_import
from app.vault_master import MemoryVaultMasterStore, ScannedFile


def context(source=None, label="Artist - Album"):
    return {"source_kind": "manual_upload", "source_id": str(source or uuid4()),
            "source_label": label, "relative_path": "track.wma"}


def test_current_supplier_boundary_is_selection_owner_and_installation_scoped():
    owner, installation = uuid4(), uuid4()
    first = context()
    intent = supplier_manual_album(first, "audio/x-ms-wma", installation)["music_album"]
    retry = supplier_manual_album({**first, "relative_path": "another.wma"}, "audio/x-ms-wma", installation)["music_album"]
    assert intent == retry
    assert declared_album(owner, intent).id != declared_album(uuid4(), intent).id
    assert supplier_manual_album(first, "audio/x-ms-wma", uuid4())["music_album"] != intent
    assert supplier_manual_album(context(), "audio/x-ms-wma", installation)["music_album"] != intent
    for ungrouped, mime in [({"source_kind": "manual_upload"}, "audio/flac"),
                            ({**first, "source_kind": "automatic_source"}, "audio/flac"),
                            (first, "video/mp4")]:
        assert "music_album" not in supplier_manual_album(ungrouped, mime, installation)


def seed(store, owner, root):
    declaration = supplier_manual_album(context(), "audio/flac", uuid4())["music_album"]
    album = store.declare_music_album(owner, declaration)
    batch = store.create_batch("incoming", str(root))
    items = []
    for number in range(13):
        filename = f"synthetic-{number}.flac"
        items.append(store.record_file(batch, "incoming", ScannedFile(
            source_path=str(root / filename), relative_path=filename, filename=filename,
            size_bytes=1, mime_type="audio/flac", modified_at=datetime.now(timezone.utc),
            sha256=f"{number:064x}", owner_user_id=owner, owner_username="owner",
            metadata={"source_context": {"music_album": declaration, "original_filename": filename},
                      "track_number": number + 1, "disc_number": 1})))
    return album, items


def test_group_approval_13_members_atomic_validation_owner_collision_and_retry(tmp_path):
    store, owner = MemoryVaultMasterStore(), uuid4()
    album, items = seed(store, owner, tmp_path)
    ids = [item.id for item in items]
    with pytest.raises(LookupError):
        approve_import(store, uuid4(), album.id, ids)
    with pytest.raises(ValueError):
        approve_import(store, owner, album.id, ids[:-1])
    store.items[items[-1].source_path] = replace(items[-1], proposed_destination=items[0].proposed_destination)
    with pytest.raises(ValueError):
        approve_import(store, owner, album.id, ids)
    assert all(item.state == "needs_review" for item in store.list_items())
    store.items[items[-1].source_path] = items[-1]
    assert {item.state for item in approve_import(store, owner, album.id, ids)} == {"move_queued"}
    assert {item.id for item in approve_import(store, owner, album.id, ids)} == set(ids)
    assert all(item.owner_user_id == owner for item in store.list_items())
