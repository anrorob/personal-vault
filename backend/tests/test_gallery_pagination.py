"""Synthetic Gallery pagination and private derivative contracts."""

from dataclasses import replace
from datetime import date
from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi.testclient import TestClient
from PIL import Image

import app.gallery as gallery_module
from app.gallery_thumbnails import get_gallery_thumbnail_cache_path
from app.main import app
from tests.test_gallery import authenticate, catalogue_image, configure_gallery, create_image


def _jpeg(color: tuple[int, int, int] = (120, 40, 20)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (1200, 800), color).save(output, format="JPEG")
    return output.getvalue()


def test_gallery_pages_are_bounded_stable_and_filter_before_paging(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    store = configure_gallery(tmp_path)
    for index in range(137):
        path = create_image(tmp_path, f"Synthetic-Example-{index:04d}.jpg", _jpeg())
        catalogue_image(store, tmp_path, path, captured_on=date(2024, 1, index % 28 + 1))
    authenticate(client)
    monkeypatch.setattr(gallery_module, "scan_gallery", lambda _: (_ for _ in ()).throw(AssertionError("tree scan")))

    first = client.get("/api/gallery/pages?sort=newest")
    assert first.status_code == 200
    assert len(first.json()["items"]) == 60
    assert first.json()["next_cursor"]
    second = client.get("/api/gallery/pages", params={"sort": "newest", "cursor": first.json()["next_cursor"]})
    third = client.get("/api/gallery/pages", params={"sort": "newest", "cursor": second.json()["next_cursor"]})
    cards = first.json()["items"] + second.json()["items"] + third.json()["items"]
    assert len(cards) == 137
    assert len({card["id"] for card in cards}) == 137
    assert third.json()["next_cursor"] is None
    assert [card["captured_on"] for card in cards] == sorted(
        [card["captured_on"] for card in cards], reverse=True,
    )
    oldest = client.get("/api/gallery/pages?sort=oldest").json()["items"]
    assert oldest[0]["captured_on"] == "2024-01-01"
    assert oldest[0]["id"] != cards[0]["id"]
    assert client.get("/api/gallery/pages?cursor=bad").status_code == 422


def test_gallery_page_owner_hidden_and_shared_inclusion(client: TestClient, tmp_path: Path, authentication_store, monkeypatch) -> None:
    store = configure_gallery(tmp_path)
    owned = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-owned.jpg"))
    hidden = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-hidden.jpg"))
    store.catalogued_assets[hidden.vault_path] = replace(hidden, lifecycle_state="hidden")
    other = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-shared.jpg"))
    other = replace(
        other, owner_user_id=uuid5(NAMESPACE_URL, "synthetic-other-owner"),
        owner_username="synthetic-other", visibility="shared",
        shared_with_user_ids=(owned.owner_user_id,),
    )
    store.catalogued_assets[other.vault_path] = other
    authenticate(client)

    assert [card["asset_id"] for card in client.get("/api/gallery/pages").json()["items"]] == [str(owned.id)]
    monkeypatch.setattr(gallery_module, "included_gallery_assets", lambda _: {other.id: "Synthetic owner"})
    assert {card["name"] for card in client.get("/api/gallery/pages").json()["items"]} == {
        "Synthetic-owned.jpg", "Synthetic-shared.jpg",
    }
    token = client.cookies.get("pv_session")
    assert authentication_store.authorize_hidden_photos_session(token, owned.owner_user_id)
    hidden_cards = client.get("/api/gallery/pages?include_hidden=true").json()["items"]
    assert [card["name"] for card in hidden_cards] == ["Synthetic-hidden.jpg"]


def test_gallery_thumbnail_is_bounded_reused_revalidated_and_read_only(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    store = configure_gallery(tmp_path)
    source = create_image(tmp_path, "Synthetic-thumbnail.jpg", _jpeg())
    asset = catalogue_image(store, tmp_path, source)
    app.dependency_overrides[get_gallery_thumbnail_cache_path] = lambda: tmp_path / "derivatives"
    authenticate(client)
    monkeypatch.setattr(gallery_module, "scan_gallery", lambda _: (_ for _ in ()).throw(AssertionError("tree scan")))
    generated = []
    def synthetic_encoder(command, **_kwargs):
        generated.append(command)
        with Image.open(source) as image:
            image.thumbnail((640, 640))
            image.save(command[-1], format="JPEG")
    monkeypatch.setattr("app.gallery_thumbnails.subprocess.run", synthetic_encoder)
    url = f"/api/gallery/assets/{asset.id}/preview"
    original = source.read_bytes()
    first = client.get(url)
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert len(first.content) < len(original)
    assert first.headers["cache-control"].startswith("private, no-cache")
    with Image.open(BytesIO(first.content)) as image:
        assert max(image.size) <= 640
    assert source.read_bytes() == original
    assert len(list((tmp_path / "derivatives").rglob("*.jpg"))) == 1
    assert len(generated) == 1
    second = client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert len(list((tmp_path / "derivatives").rglob("*.jpg"))) == 1
    assert len(generated) == 1
    source.write_bytes(_jpeg((20, 90, 140)))
    replaced = client.get(url, headers={"If-None-Match": first.headers["etag"]})
    assert replaced.status_code == 200
    assert replaced.headers["etag"] != first.headers["etag"]
    assert len(list((tmp_path / "derivatives").rglob("*.jpg"))) == 2
    assert len(generated) == 2


def test_gallery_thumbnail_fails_closed_for_other_owner_and_traversal(client: TestClient, tmp_path: Path) -> None:
    store = configure_gallery(tmp_path)
    source = create_image(tmp_path, "Synthetic-private.jpg", _jpeg())
    asset = catalogue_image(store, tmp_path, source)
    authenticate(client)
    alien = replace(asset, owner_user_id=uuid5(NAMESPACE_URL, "synthetic-alien-owner"), owner_username="alien")
    store.catalogued_assets[asset.vault_path] = alien
    assert client.get(f"/api/gallery/assets/{asset.id}/preview").status_code == 404
    traversal = replace(asset, vault_path="/vault/Gallery/../Synthetic-private.jpg")
    store.catalogued_assets = {traversal.vault_path: traversal}
    assert client.get(f"/api/gallery/assets/{asset.id}/preview").status_code == 404


def test_failed_thumbnail_does_not_break_listing_or_retry_storm(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    store = configure_gallery(tmp_path)
    source = create_image(tmp_path, "Synthetic-bad-image.jpg", b"invalid synthetic image")
    asset = catalogue_image(store, tmp_path, source)
    app.dependency_overrides[get_gallery_thumbnail_cache_path] = lambda: tmp_path / "derivatives"
    authenticate(client)
    attempts = []
    def fail_encoder(*_args, **_kwargs):
        attempts.append(True)
        raise OSError("Synthetic decoder failure")
    monkeypatch.setattr("app.gallery_thumbnails.subprocess.run", fail_encoder)
    url = f"/api/gallery/assets/{asset.id}/preview"
    assert client.get(url).status_code == 503
    assert client.get(url).status_code == 503
    assert len(attempts) == 1
    assert [card["asset_id"] for card in client.get("/api/gallery/pages").json()["items"]] == [str(asset.id)]
    assert source.read_bytes() == b"invalid synthetic image"


def test_shared_thumbnail_rechecks_inclusion_before_conditional_cache(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    store = configure_gallery(tmp_path)
    owned = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-owner.jpg", _jpeg()))
    shared = catalogue_image(store, tmp_path, create_image(tmp_path, "Synthetic-shared.jpg", _jpeg()))
    shared = replace(
        shared, owner_user_id=uuid5(NAMESPACE_URL, "synthetic-remote-owner"),
        owner_username="synthetic-remote", visibility="shared",
        shared_with_user_ids=(owned.owner_user_id,),
    )
    store.catalogued_assets[shared.vault_path] = shared
    included = {shared.id: "Synthetic owner"}
    monkeypatch.setattr(gallery_module, "included_gallery_assets", lambda _: included)
    app.dependency_overrides[get_gallery_thumbnail_cache_path] = lambda: tmp_path / "derivatives"
    def synthetic_encoder(command, **_kwargs):
        with Image.open(tmp_path / "Synthetic-shared.jpg") as image:
            image.thumbnail((640, 640))
            image.save(command[-1], format="JPEG")
    monkeypatch.setattr("app.gallery_thumbnails.subprocess.run", synthetic_encoder)
    authenticate(client)
    url = f"/api/gallery/assets/{shared.id}/preview"
    response = client.get(url)
    assert response.status_code == 200
    included.clear()
    assert client.get(url, headers={"If-None-Match": response.headers["etag"]}).status_code == 404
