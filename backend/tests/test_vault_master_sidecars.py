from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.vault_master import CataloguedAsset
from app.vault_master import (
    INCOMING_SOURCE,
    MemoryVaultMasterStore,
    process_next_move,
    scan_root,
)
from app.vault_master_sidecars import (
    SIDECAR_SCHEMA,
    SIDECAR_VERSION,
    assess_sidecar_recovery,
    canonical_sidecar_document,
    canonical_sidecar_is_current,
    compare_sidecar_recovery,
    catalogued_asset_from_sidecar,
    read_canonical_sidecar,
    write_canonical_sidecar,
)


def example_asset() -> CataloguedAsset:
    return CataloguedAsset(
        id=UUID("12345678-1234-5678-1234-567812345678"),
        asset_type="Gallery",
        display_title="Family photograph",
        captured_on=date(1995, 9, 3),
        location="Gdańsk, Polska",
        vault_path="/vault/Gallery/photo.jpg",
        filename="photo.jpg",
        size_bytes=1234,
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={"camera_model": "Example Camera"},
        metadata_provenance={
            "display_title": "user_override",
            "captured_on": "embedded",
            "location": "user_override",
        },
        detected_metadata={
            "display_title": "photo",
            "captured_on": "1995-09-03",
            "camera_model": "Example Camera",
        },
        imported_metadata={"keywords": ["family", "wedding"]},
        user_overrides={
            "display_title": "Family photograph",
            "location": "Gdańsk, Polska",
        },
        effective_metadata={
            "display_title": "Family photograph",
            "captured_on": "1995-09-03",
            "location": "Gdańsk, Polska",
            "camera_model": "Example Camera",
            "keywords": ["family", "wedding"],
        },
        owner_username="owner",
        visibility="private",
    )


def test_canonical_sidecar_retains_all_metadata_layers() -> None:
    exported_at = datetime(2026, 7, 29, 12, 30, tzinfo=timezone.utc)

    document = canonical_sidecar_document(
        example_asset(),
        exported_at=exported_at,
    )

    assert document["schema"] == SIDECAR_SCHEMA
    assert document["version"] == SIDECAR_VERSION
    assert document["exported_at"] == "2026-07-29T12:30:00+00:00"
    assert document["asset"] == {
        "id": "12345678-1234-5678-1234-567812345678",
        "asset_type": "Gallery",
        "vault_path": "/vault/Gallery/photo.jpg",
        "filename": "photo.jpg",
        "size_bytes": 1234,
        "mime_type": "image/jpeg",
        "sha256": "a" * 64,
    }
    assert document["access"] == {
        "owner_username": "owner",
        "visibility": "private",
        "shared_with": [],
    }
    metadata = document["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["detected"]["camera_model"] == "Example Camera"
    assert metadata["imported"]["keywords"] == ["family", "wedding"]
    assert metadata["user_overrides"]["location"] == "Gdańsk, Polska"
    assert metadata["effective"]["display_title"] == "Family photograph"
    assert metadata["provenance"]["location"] == "user_override"


def test_legacy_sidecar_is_read_as_private_for_the_restoring_owner(
    tmp_path: Path,
) -> None:
    asset = example_asset()
    destination = write_canonical_sidecar(asset, tmp_path)
    legacy_document = json.loads(destination.read_text(encoding="utf-8"))
    legacy_document["version"] = 1
    del legacy_document["access"]
    destination.write_text(json.dumps(legacy_document), encoding="utf-8")

    restored = catalogued_asset_from_sidecar(
        read_canonical_sidecar(destination),
        legacy_owner_username="owner",
    )

    assert restored.owner_username == "owner"
    assert restored.visibility == "private"
    assert restored.shared_with == ()


def test_ownerless_legacy_sidecar_is_refused_fail_closed(tmp_path: Path) -> None:
    destination = write_canonical_sidecar(example_asset(), tmp_path)
    legacy_document = json.loads(destination.read_text(encoding="utf-8"))
    legacy_document["version"] = 1
    del legacy_document["access"]
    destination.write_text(json.dumps(legacy_document), encoding="utf-8")

    with pytest.raises(ValueError, match="owner identity is unavailable"):
        catalogued_asset_from_sidecar(read_canonical_sidecar(destination))


def test_sidecar_export_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_sidecar_document(
            example_asset(),
            exported_at=datetime(2026, 7, 29),
        )


def test_sidecar_is_atomically_replaced_without_temporary_files(
    tmp_path: Path,
) -> None:
    asset = example_asset()
    destination = write_canonical_sidecar(
        asset,
        tmp_path,
        exported_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    updated = CataloguedAsset(
        **{
            **asset.__dict__,
            "effective_metadata": {
                **asset.effective_metadata,
                "location": "Warszawa, Polska",
            },
        }
    )

    replaced = write_canonical_sidecar(
        updated,
        tmp_path,
        exported_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )

    assert replaced == destination
    assert destination.parent == tmp_path / "sidecars"
    assert destination.name == f"{asset.id}.json"
    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["metadata"]["effective"]["location"] == (
        "Warszawa, Polska"
    )
    assert list(destination.parent.glob("*.part")) == []


def test_catalogue_mutations_publish_current_canonical_sidecar(
    tmp_path: Path,
) -> None:
    arrival_hall = tmp_path / "Arrival Hall"
    gallery = tmp_path / "Gallery"
    metadata_root = tmp_path / "metadata"
    arrival_hall.mkdir()
    gallery.mkdir()
    (arrival_hall / "photo.jpg").write_bytes(b"photograph")
    store = MemoryVaultMasterStore(sidecar_root=metadata_root)

    scan_root(store, arrival_hall, INCOMING_SOURCE)
    item = store.list_items()[0]
    store.record_decision(item.id, "approved", "owner")
    store.queue_move(item.id, "owner")
    assert (
        process_next_move(
            store,
            arrival_hall,
            {"Gallery": gallery},
        )
        == item.id
    )

    asset = store.get_catalogued_asset("/vault/Gallery/photo.jpg")
    assert asset is not None
    destination = metadata_root / "sidecars" / f"{asset.id}.json"
    initial = json.loads(destination.read_text(encoding="utf-8"))
    assert initial["asset"]["vault_path"] == "/vault/Gallery/photo.jpg"

    updated = store.update_catalogued_asset_metadata(
        asset.id,
        {"location": "Gdańsk, Polska"},
        "owner",
    )
    assert updated is not None
    corrected = json.loads(destination.read_text(encoding="utf-8"))
    assert corrected["metadata"]["effective"]["location"] == (
        "Gdańsk, Polska"
    )
    assert corrected["metadata"]["user_overrides"]["location"] == (
        "Gdańsk, Polska"
    )

    imported = store.import_catalogued_asset_metadata(
        asset.id,
        {"keywords": ["family"]},
        "provider:test",
    )
    assert imported is not None
    enriched = json.loads(destination.read_text(encoding="utf-8"))
    assert enriched["metadata"]["imported"]["keywords"] == ["family"]
    assert enriched["metadata"]["effective"]["location"] == "Gdańsk, Polska"


def test_sidecar_export_failure_is_audited_without_rolling_back_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryVaultMasterStore(sidecar_root=tmp_path / "metadata")
    asset = example_asset()
    store.catalogued_assets[asset.vault_path] = asset

    def fail_export(*args: object, **kwargs: object) -> Path:
        raise OSError("storage unavailable")

    monkeypatch.setattr(
        "app.vault_master.write_canonical_sidecar",
        fail_export,
    )

    updated = store.update_catalogued_asset_metadata(
        asset.id,
        {"location": "Warszawa, Polska"},
        "owner",
    )

    assert updated is not None
    assert updated.location == "Warszawa, Polska"
    assert store.get_catalogued_asset(asset.vault_path) == updated
    failure = store.list_activity(limit=1)[0]
    assert failure.action == "sidecar_export_failed"
    assert failure.succeeded is False
    assert "storage unavailable" in failure.detail


def test_reconciliation_repairs_missing_invalid_and_stale_sidecars(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "metadata"
    asset = example_asset()
    store = MemoryVaultMasterStore(sidecar_root=metadata_root)
    store.catalogued_assets[asset.vault_path] = asset

    missing = store.reconcile_sidecars()

    assert missing == type(missing)(
        checked=1,
        current=0,
        repaired=1,
        failed=0,
    )
    assert canonical_sidecar_is_current(asset, metadata_root)
    current = store.reconcile_sidecars()
    assert current.current == 1
    assert current.repaired == 0

    destination = metadata_root / "sidecars" / f"{asset.id}.json"
    destination.write_text("{not valid json", encoding="utf-8")
    invalid = store.reconcile_sidecars()
    assert invalid.repaired == 1
    assert canonical_sidecar_is_current(asset, metadata_root)

    document = json.loads(destination.read_text(encoding="utf-8"))
    document["metadata"]["effective"]["location"] = "Wrong"
    destination.write_text(json.dumps(document), encoding="utf-8")
    stale = store.reconcile_sidecars()
    assert stale.repaired == 1
    assert canonical_sidecar_is_current(asset, metadata_root)


def test_recovery_assessment_validates_sidecars_without_writing(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "metadata"
    asset = example_asset()
    valid_path = write_canonical_sidecar(asset, metadata_root)
    original = valid_path.read_text(encoding="utf-8")

    invalid_path = valid_path.with_name(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json"
    )
    invalid_document = json.loads(original)
    invalid_document["asset"]["sha256"] = "../not-a-checksum"
    invalid_path.write_text(json.dumps(invalid_document), encoding="utf-8")

    unsupported_path = valid_path.with_name(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.json"
    )
    unsupported_document = json.loads(original)
    unsupported_document["version"] = SIDECAR_VERSION + 1
    unsupported_path.write_text(
        json.dumps(unsupported_document),
        encoding="utf-8",
    )

    assessment = assess_sidecar_recovery(metadata_root)

    assert assessment.discovered == 3
    assert assessment.valid == 1
    assert assessment.invalid == 1
    assert assessment.unsupported == 1
    assert valid_path.read_text(encoding="utf-8") == original


def test_recovery_reader_rejects_identity_and_path_mismatches(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "metadata"
    path = write_canonical_sidecar(example_asset(), metadata_root)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["asset"]["vault_path"] = "/vault/Gallery/other.jpg"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="Vault path"):
        read_canonical_sidecar(path)

    document["asset"]["vault_path"] = "/vault/Gallery/photo.jpg"
    document["asset"]["id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="filename"):
        read_canonical_sidecar(path)


def test_recovery_comparison_classifies_current_restorable_and_conflicts(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "metadata"
    current = example_asset()
    write_canonical_sidecar(current, metadata_root)

    restorable = CataloguedAsset(
        **{
            **current.__dict__,
            "id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            "vault_path": "/vault/Gallery/restorable.jpg",
            "filename": "restorable.jpg",
        }
    )
    write_canonical_sidecar(restorable, metadata_root)

    conflict = CataloguedAsset(
        **{
            **current.__dict__,
            "id": UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            "vault_path": "/vault/Gallery/conflict.jpg",
            "filename": "conflict.jpg",
        }
    )
    write_canonical_sidecar(conflict, metadata_root)
    changed_conflict = CataloguedAsset(
        **{
            **conflict.__dict__,
            "effective_metadata": {
                **conflict.effective_metadata,
                "location": "Changed in database",
            },
        }
    )

    path_conflict = CataloguedAsset(
        **{
            **current.__dict__,
            "id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            "vault_path": "/vault/Gallery/path-conflict.jpg",
            "filename": "path-conflict.jpg",
        }
    )
    write_canonical_sidecar(path_conflict, metadata_root)
    different_identity = CataloguedAsset(
        **{
            **path_conflict.__dict__,
            "id": UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        }
    )
    assets_by_id = {
        current.id: current,
        changed_conflict.id: changed_conflict,
        different_identity.id: different_identity,
    }
    assets_by_path = {
        asset.vault_path: asset for asset in assets_by_id.values()
    }

    assessment = compare_sidecar_recovery(
        metadata_root,
        assets_by_id.get,
        assets_by_path.get,
    )

    assert assessment.discovered == 4
    assert assessment.valid == 4
    assert assessment.current == 1
    assert assessment.restorable == 1
    assert assessment.conflicting == 1
    assert assessment.path_conflicts == 1
    assert assessment.invalid == 0
    assert assessment.unsupported == 0
    assert assessment.recoverable == 1
    assert {
        candidate.status for candidate in assessment.candidates
    } == {"current", "recoverable", "conflict", "path_conflict"}
    recoverable_candidate = next(
        candidate
        for candidate in assessment.candidates
        if candidate.status == "recoverable"
    )
    assert recoverable_candidate.asset_id == restorable.id
    assert recoverable_candidate.vault_path == restorable.vault_path


def test_recovery_excludes_tombstoned_and_missing_media_sidecars(tmp_path: Path) -> None:
    metadata_root = tmp_path / "metadata"
    tombstoned = example_asset()
    missing = CataloguedAsset(
        **{
            **tombstoned.__dict__,
            "id": UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            "vault_path": "/vault/Gallery/missing.jpg",
            "filename": "missing.jpg",
        }
    )
    write_canonical_sidecar(tombstoned, metadata_root)
    write_canonical_sidecar(missing, metadata_root)

    assessment = compare_sidecar_recovery(
        metadata_root,
        lambda _: None,
        lambda _: None,
        lambda asset_id: asset_id == tombstoned.id,
        lambda _: False,
    )

    assert assessment.intentionally_deleted == 1
    assert assessment.media_missing == 1
    assert assessment.recoverable == 0
    assert {candidate.status for candidate in assessment.candidates} == {
        "intentionally_deleted",
        "media_missing",
    }
