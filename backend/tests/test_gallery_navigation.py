"""Disposable synthetic catalogue tests for direct Gallery navigation."""
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import app.gallery as gallery
from tests.test_gallery import authenticate, catalogue_image, configure_gallery, create_image


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_direct_anchor_month_seek_and_reverse_pages(client, tmp_path: Path, monkeypatch, sort):
    store = configure_gallery(tmp_path)
    assets = [catalogue_image(store, tmp_path, create_image(tmp_path, f"Synthetic-{i:04d}.jpg"),
              captured_on=date(2018 + i // 24, i % 12 + 1, 1)) for i in range(96)]
    authenticate(client)
    monkeypatch.setattr(gallery, "scan_gallery", lambda *_: pytest.fail("directory scan"))
    calls = []
    original = store.gallery_page_paths
    def counted(*args, **kwargs):
        calls.append(args[8])
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "gallery_page_paths", counted)
    first = client.get("/api/gallery/pages", params={"sort": sort}).json()
    outside = next(asset for asset in assets if str(asset.id) not in {item["asset_id"] for item in first["items"]})
    calls.clear()
    restored = client.get("/api/gallery/pages", params={"sort": sort, "anchor_asset_id": str(outside.id)})
    assert restored.status_code == 200
    assert str(outside.id) in {item["asset_id"] for item in restored.json()["items"]}
    assert len(restored.json()["items"]) <= 60
    assert calls == [12, 61, 1]  # bounded surrounding/page/probe, no sequential walk
    first_anchor = client.get("/api/gallery/pages", params={"sort": sort, "anchor_asset_id": first["items"][0]["asset_id"]}).json()
    assert first_anchor["items"][0]["id"] == first["items"][0]["id"]
    assert first_anchor["previous_cursor"] is None
    periods = client.get("/api/gallery/chronology", params={"sort": sort}).json()
    assert len(periods) == 48
    assert sum(period["count"] for period in periods) == 96
    assert {period["year"] for period in periods} == {2018, 2019, 2020, 2021}
    assert periods[0]["year"] == (2021 if sort == "newest" else 2018)
    assert periods[0]["month"] == (12 if sort == "newest" else 1)
    march = next(period for period in periods if period["year"] == 2020 and period["month"] == 3)
    jumped = client.get("/api/gallery/pages", params={"sort": sort, "start": march["start"]}).json()
    assert next(item for item in jumped["items"] if item["id"] == jumped["seek_anchor_id"])["captured_on"] == "2020-03-01"
    previous = client.get("/api/gallery/pages", params={"sort": sort, "before": jumped["previous_cursor"]}).json()
    expected = original(assets[0].owner_user_id, [], "active", sort, None, None, None, None, 96)
    combined = [item["name"] for item in previous["items"] + jumped["items"]]
    start_index = expected.index("/vault/Gallery/" + combined[0])
    assert combined == [Path(path).name for path in expected[start_index:start_index + len(combined)]]


def test_chronology_hidden_shared_filtered_and_undated(client, tmp_path, authentication_store, monkeypatch):
    store = configure_gallery(tmp_path)
    owned = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-owned.jpg"), captured_on=date(2018, 3, 1))
    hidden = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-hidden.jpg"), captured_on=date(1999, 1, 1))
    shared = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-shared.jpg"), captured_on=date(2020, 6, 1))
    from uuid import UUID
    store.catalogued_assets[hidden.vault_path] = replace(hidden, lifecycle_state="hidden")
    store.catalogued_assets[shared.vault_path] = replace(shared, owner_user_id=UUID(int=3000001),
        owner_username="synthetic-other", visibility="shared", shared_with_user_ids=(owned.owner_user_id,))
    authenticate(client)
    assert [p["year"] for p in client.get("/api/gallery/chronology").json()] == [2018]
    assert client.get("/api/gallery/pages", params={"anchor_asset_id": str(shared.id)}).status_code == 404
    assert client.get("/api/gallery/chronology?include_hidden=true").status_code == 403
    monkeypatch.setattr(gallery, "included_gallery_assets", lambda _: {shared.id: "Synthetic owner"})
    assert [p["year"] for p in client.get("/api/gallery/chronology").json()] == [2020, 2018]
    assert authentication_store.authorize_hidden_photos_session(client.cookies.get("pv_session"), owned.owner_user_id)
    assert [p["year"] for p in client.get("/api/gallery/chronology?include_hidden=true").json()] == [1999]
    assert client.get("/api/gallery/pages", params={"anchor_asset_id": str(hidden.id)}).status_code == 404
    assert client.get("/api/gallery/chronology?photo_type=invalid").json() == []
    from types import SimpleNamespace
    from app.main import app
    valid_slug = next(slug for namespace, slug in gallery.GALLERY_TERM_NAMES if namespace == "photo_type")
    app.dependency_overrides[gallery.get_gallery_intelligence_store] = lambda: SimpleNamespace(matching_asset_ids=lambda *_: [owned.id])
    filtered = client.get("/api/gallery/chronology", params={"photo_type": valid_slug}).json()
    assert [p["year"] for p in filtered] == [2018]
    assert client.get("/api/gallery/pages", params={"photo_type": valid_slug, "anchor_asset_id": str(shared.id)}).status_code == 404
    assert client.get("/api/gallery/pages", params={"anchor_asset_id": str(owned.id), "date_from": "2024-01-01"}).status_code == 404
    catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-undated.jpg"), captured_on=None)
    periods = client.get("/api/gallery/chronology").json()
    assert periods[-1]["year"] is None and periods[-1]["month"] is None
    undated = client.get("/api/gallery/pages", params={"start": periods[-1]["start"]}).json()
    assert next(item for item in undated["items"] if item["id"] == undated["seek_anchor_id"])["captured_on"] is None


def test_chronology_and_deep_anchor_scale_without_asset_hydration(client, tmp_path, monkeypatch):
    store = configure_gallery(tmp_path)
    original = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-base.jpg"))
    from uuid import UUID
    for i in range(3000):
        path = f"/vault/Gallery/Synthetic-scale-{i:04d}.jpg"
        store.catalogued_assets[path] = replace(original, id=UUID(int=i + 1), vault_path=path,
            filename=Path(path).name, captured_on=date(2018 + i % 8, i % 12 + 1, 1))
    authenticate(client)
    monkeypatch.setattr(gallery, "scan_gallery", lambda *_: pytest.fail("directory scan"))
    monkeypatch.setattr(gallery, "_gallery_image_from_asset", lambda *_: pytest.fail("file access"))
    monkeypatch.setattr(store, "get_visible_catalogued_assets", lambda *_: pytest.fail("asset hydration"))
    periods = client.get("/api/gallery/chronology").json()
    assert sum(period["count"] for period in periods) == 3001
    assert len(periods) <= 97
