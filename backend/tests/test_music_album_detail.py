from dataclasses import replace
from uuid import uuid4, uuid5, NAMESPACE_URL

from tests.test_music import configure, catalogue_track, authenticate
from app.main import app
from app.movie_playback import get_jellyfin_client
from tests.test_music_groups import intent
from tests.conftest import TEST_USERNAME


def test_album_detail_uses_uuid_sequence_and_existing_visibility(client, tmp_path):
    root, store = configure(tmp_path)
    assets = []
    for name, number in [("A.flac", 2), ("Z.flac", 1)]:
        (root / name).write_bytes(b"synthetic")
        assets.append(catalogue_track(store, root, name, owner="other-owner", metadata_overrides={"display_title": name, "track_number": number}))
    album = store.declare_music_album(assets[0].owner_user_id, intent())
    store.bind_music_album(album.id, album.owner_user_id, [a.id for a in assets])
    url = f"/api/music/albums/{album.id}"
    assert client.get(url).status_code == 401
    authenticate(client)
    assert client.get(url).status_code == 404  # Private members remain private.
    for asset in assets:
        store.catalogued_assets[asset.vault_path] = replace(asset, visibility="vault-wide")
    result = client.get(url)
    assert result.status_code == 200  # Existing local vault-wide Music visibility.
    assert result.headers["cache-control"] == "private, no-store"
    body = result.json()
    assert body["id"] == str(album.id)
    assert body["title"] == album.album_title
    assert body["can_edit"] is False
    assert [t["asset_id"] for t in body["tracks"]] == [str(assets[1].id), str(assets[0].id)]
    assert body["order_state"] == "ready"
    assert body["release_year"] == 1998
    assert client.patch(f"/api/vault-master/music/albums/groups/{album.id}", json={"album_title": "Forbidden", "artist_name": "Forbidden"}).status_code == 409
    # Missing catalogue authority denies both detail metadata and direct content.
    for asset in assets:
        del store.catalogued_assets[asset.vault_path]
    assert client.get(url).status_code == 404
    app.dependency_overrides[get_jellyfin_client] = lambda: object()
    assert client.get(body["tracks"][0]["playback_url"]).status_code == 404


def test_owner_empty_unresolved_and_correction_preserve_membership(client, tmp_path):
    root, store = configure(tmp_path)
    owner = uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}")
    album = store.declare_music_album(owner, intent())
    authenticate(client)
    url = f"/api/music/albums/{album.id}"
    empty = client.get(url).json()
    assert empty["tracks"] == [] and empty["order_state"] == "unresolved"
    assert empty["artwork_url"] is None and empty["release_year"] is None
    assert empty["can_edit"] is True
    assert client.get(f"/api/music/albums/{uuid4()}").status_code == 404
    other = store.declare_music_album(uuid4(), intent())
    assert client.get(f"/api/music/albums/{other.id}").status_code == 404
    (root / "synthetic.flac").write_bytes(b"synthetic")
    asset = catalogue_track(store, root, "synthetic.flac", metadata_overrides={"track_number": None, "disc_number": None, "artwork": None, "release_year": None})
    store.bind_music_album(album.id, owner, [asset.id])
    body = client.get(url).json()
    assert body["order_state"] == "unresolved"
    assert len(body["tracks"]) == 1 and body["tracks"][0]["album_position"] is None
    assert client.patch(f"/api/vault-master/music/albums/groups/{album.id}", json={"album_title": "Corrected", "artist_name": "Corrected artist"}).status_code == 200
    corrected = client.get(url).json()
    assert corrected["title"] == "Corrected" and corrected["artist"] == "Corrected artist"
    assert corrected["tracks"][0]["asset_id"] == str(asset.id)
    assert store.get_music_album(album.id).owner_user_id == owner
    assert store.music_album_history[-1][2] == "identity_corrected"
