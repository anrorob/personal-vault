from dataclasses import replace
from datetime import date
from datetime import datetime, timezone
from uuid import uuid4

from PIL import Image
import pytest

from app.photo_dates import resolve_gallery_effective_date
from app.vault_master import (
    apply_catalogue_metadata_changes, apply_imported_asset_metadata,
    apply_source_timestamp_provenance, extract_basic_metadata,
    MemoryVaultMasterStore, get_vault_master_store,
    ImportItem, refresh_catalogued_asset_detection,
)
from app.gallery import get_gallery_path
from app.main import app
from tests.test_gallery import catalogue_image, create_image, authenticate, configure_gallery


@pytest.mark.parametrize("evidence,expected,source", [
    ({"source_created_at": "2018-06-12T09:00:00Z", "source_modified_at": "2024-02-18T00:00:00Z"}, "2018-06-12", "source_file_created"),
    ({"source_created_at": "2018-06-12", "source_modified_at": "2017-02-18T00:00:00"}, "2017-02-18", "source_file_modified"),
    ({"exif_original_at": "2018:06:10 12:00:00", "source_created_at": "2010-06-12"}, "2018-06-10", "embedded"),
    ({"exif_created_at": "2017:06:10 12:00:00", "source_context": {"source_created_at": "2010-06-12"}}, "2010-06-12", "source_file_created"),
    ({"document_created_at": "2017-01-01T00:00:00+00:00", "document_modified_at": "2020-01-01T00:00:00+00:00"}, "2017-01-01", "embedded_file_date"),
    ({"source_created_at": "1601-01-01T00:00:00Z", "source_modified_at": "1970-01-01T00:00:00Z", "exif_original_at": "0000:00:00 00:00:00"}, None, "unavailable"),
    ({"source_created_at": "not a date", "source_modified_at": "2099-12-01"}, None, "unavailable"),
    ({"created_at": "2018-06-12", "publication_at": "2010-01-01", "modified_at": "2000-01-01"}, None, "unavailable"),
])
def test_authority(evidence, expected, source):
    value, actual_source = resolve_gallery_effective_date((evidence,))
    assert (value.isoformat() if value else None, actual_source) == (expected, source)


def test_multiple_layers_choose_oldest_deterministically():
    layers = ({"source_created_at": "2018-01-01"}, {"source_modified_at": "2010-01-01"}, {"exif_modified_at": "2012:01:01 00:00:00"})
    assert resolve_gallery_effective_date(layers) == resolve_gallery_effective_date(reversed(layers)) == (date(2010, 1, 1), "source_file_modified")


def test_real_exif_subdirectory_is_extracted(tmp_path):
    path = tmp_path / "fixture.jpg"
    exif = Image.Exif()
    exif[34665] = {36867: "2010:07:11 11:03:01", 36868: "2011:01:01 00:00:00"}
    exif[306] = "2009:01:01 00:00:00"
    Image.new("RGB", (8, 8)).save(path, exif=exif)
    before = path.read_bytes()
    metadata = extract_basic_metadata(path)
    assert metadata["captured_at"] == "2010-07-11"
    assert metadata["exif_original_at"] == "2010:07:11 11:03:01"
    assert path.read_bytes() == before


def test_manual_clear_and_historical_import(tmp_path):
    store = MemoryVaultMasterStore()
    asset = catalogue_image(store, tmp_path, create_image(tmp_path, "fixture.jpg"), captured_on=None)
    historical = replace(asset, imported_metadata={"source_created_at": "2018-06-12", "source_modified_at": "2024-02-18"})
    assert historical.captured_on == date(2018, 6, 12)
    newer = apply_imported_asset_metadata(historical, {"source_created_at": "2020-06-12"}, "supplier")
    assert newer.captured_on == date(2018, 6, 12)
    assert newer.imported_metadata["source_created_at"] == "2018-06-12"
    corrected = apply_catalogue_metadata_changes(historical, {"captured_on": "2018-06-09"})
    assert corrected.captured_on == date(2018, 6, 9)
    assert corrected.imported_metadata == historical.imported_metadata
    cleared = apply_catalogue_metadata_changes(corrected, {"captured_on": None})
    assert cleared.captured_on == historical.captured_on
    exact = apply_imported_asset_metadata(corrected, {"exif_original_at": "2018:06:10 01:02:03"}, "fixture")
    assert exact.captured_on == corrected.captured_on
    assert apply_catalogue_metadata_changes(exact, {"captured_on": None}).captured_on == date(2018, 6, 10)


def test_source_context_survives_to_canonical_date(tmp_path):
    source = {"source_created_at": "2018-06-12", "source_modified_at": "2024-02-18T10:20:30"}
    metadata = apply_source_timestamp_provenance({}, source)
    assert all(metadata[k] == v for k, v in source.items())
    asset = catalogue_image(MemoryVaultMasterStore(), tmp_path, create_image(tmp_path, "fixture.jpg"), captured_on=None)
    assert replace(asset, detected_metadata=metadata).captured_on == date(2018, 6, 12)


def test_inventory_refresh_preserves_supplier_dates(tmp_path):
    asset = catalogue_image(MemoryVaultMasterStore(), tmp_path, create_image(tmp_path, "fixture.jpg"), captured_on=None)
    original = {"source_created_at": "2018-06-12T10:00:00Z", "source_modified_at": "2024-02-18T10:00:00Z"}
    asset = replace(asset, metadata=original, detected_metadata=original)
    item = ImportItem(id=uuid4(), batch_id=uuid4(), source_kind="inventory", source_path=str(tmp_path / "fixture.jpg"),
        relative_path="fixture.jpg", filename="fixture.jpg", size_bytes=10, mime_type="image/jpeg", modified_at=datetime.now(timezone.utc),
        sha256=asset.sha256, state="inventoried", duplicate_of_id=None, proposed_category="Gallery", proposed_destination=asset.vault_path,
        proposal_reason=None, proposal_confidence=None, metadata={"capture_date_source": "unavailable"}, metadata_overrides={})
    refreshed = refresh_catalogued_asset_detection(asset, item)
    assert refreshed.captured_on == date(2018, 6, 12)
    assert all(refreshed.detected_metadata[k] == v for k, v in original.items())
    assert refresh_catalogued_asset_detection(refreshed, item).captured_on == refreshed.captured_on


@pytest.mark.parametrize("source_kind", ["automatic_source", "manual_upload"])
def test_supplier_existing_contract_accepts_both_dates(client, authentication_store, tmp_path, source_kind):
    from tests.test_vault_supplier_transfer import _configure_arrival_hall, _authorized_headers
    from app.vault_supplier_transfer import get_transfer_store
    _configure_arrival_hall(tmp_path)
    headers = _authorized_headers(client, authentication_store)
    response = client.post("/api/vault-supplier/transfers", headers=headers, json={
        "protocol_version": 1, "filename": "fixture.jpg", "total_size": 1, "sha256": "a" * 64,
        "media_type": "image/jpeg", "source_created_at": "2018-06-12T10:00:00Z", "source_modified_at": "2024-02-18T10:00:00Z",
        "source_context": {"source_kind": source_kind}})
    assert response.status_code == 201, response.text
    session = next(iter(app.dependency_overrides[get_transfer_store]().sessions.values()))
    assert resolve_gallery_effective_date((session.source_context,)) == (date(2018, 6, 12), "source_file_created")


def test_card_detail_sort_and_range_share_canonical_value(client, tmp_path, monkeypatch):
    store = configure_gallery(tmp_path)
    paths = [create_image(tmp_path, f"fixture-{i}.jpg") for i in range(3)]
    for path, evidence in zip(paths, ({"source_created_at": "2018-06-12"}, {"exif_original_at": "2010:07:11 11:03:01"}, {})):
        asset = catalogue_image(store, tmp_path, path, captured_on=None)
        store.catalogued_assets[asset.vault_path] = replace(asset, detected_metadata=evidence)
    app.dependency_overrides[get_gallery_path] = lambda: tmp_path
    app.dependency_overrides[get_vault_master_store] = lambda: store
    monkeypatch.setattr("app.gallery.included_gallery_assets", lambda username: {})
    authenticate(client)
    oldest = client.get("/api/gallery?sort=oldest")
    assert oldest.status_code == 200
    cards = oldest.json()
    assert [p["captured_on"] for p in cards] == ["2010-07-11", "2018-06-12", None]
    newest = client.get("/api/gallery?sort=newest").json()
    assert [p["captured_on"] for p in newest] == ["2018-06-12", "2010-07-11", None]
    for card in cards:
        detail = client.get(f"/api/gallery/{card['id']}")
        assert detail.status_code == 200
        assert detail.json()["captured_on"] == card["captured_on"]
    filtered = client.get("/api/gallery?date_from=2018-01-01&date_to=2018-12-31").json()
    assert [p["captured_on"] for p in filtered] == ["2018-06-12"]
    assert client.get("/api/gallery?date_from=2020-01-01&date_to=2010-01-01").status_code == 422
