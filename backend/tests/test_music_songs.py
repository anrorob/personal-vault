from dataclasses import replace
from tests.test_music import configure, catalogue_track, authenticate
from tests.test_music_groups import intent


def test_song_correction_is_owner_only_and_never_creates_membership(client, tmp_path):
    root, store = configure(tmp_path)
    (root / "solo.flac").write_bytes(b"synthetic")
    asset = catalogue_track(store, root, "solo.flac", metadata_overrides={"album": None})
    url = f"/api/vault-master/assets/{asset.id}/metadata"
    changes = {"display_title": "My song", "artist": "My artist", "release_year": 2003}
    assert client.patch(url, json=changes).status_code == 401
    authenticate(client)
    assert client.get("/api/music").json()[0]["can_edit"] is True
    assert client.patch(url, json=changes).status_code == 200
    song = client.get("/api/music").json()[0]
    assert (song["title"], song["artist"], song["release_year"]) == ("My song", "My artist", 2003)
    assert song["album_group_id"] is None
    assert song["album"] == ""
    corrected = store.get_catalogued_asset_by_id(asset.id)
    assert corrected.owner_user_id == asset.owner_user_id
    assert corrected.vault_path == asset.vault_path and corrected.sha256 == asset.sha256
    assert corrected.user_overrides["artist"] == "My artist"
    assert corrected.metadata_provenance["artist"] == "user_override"
    assert store.asset_history[-1]["action"] == "metadata_updated"
    store.import_catalogued_asset_metadata(asset.id, {"artist": "Provider artist", "display_title": "Provider title", "release_year": 1990}, "test-provider")
    assert client.get("/api/music").json()[0]["artist"] == "My artist"
    assert store.get_asset_music_album(asset.id) is None
    assert (root / "solo.flac").read_bytes() == b"synthetic"


def test_non_owner_visibility_does_not_grant_correction_and_album_sequence_survives(client, tmp_path):
    root, store = configure(tmp_path)
    (root / "shared.flac").write_bytes(b"synthetic")
    foreign = catalogue_track(store, root, "shared.flac", owner="other-owner")
    store.catalogued_assets[foreign.vault_path] = replace(foreign, visibility="vault-wide")
    authenticate(client)
    assert client.get("/api/music").json()[0]["can_edit"] is False
    assert client.patch(f"/api/vault-master/assets/{foreign.id}/metadata", json={"artist": "Forbidden"}).status_code == 404
    (root / "member.flac").write_bytes(b"synthetic")
    member = catalogue_track(store, root, "member.flac")
    album = store.declare_music_album(member.owner_user_id, intent())
    store.bind_music_album(album.id, member.owner_user_id, [member.id])
    before = store.get_music_album_order(album.id)
    assert client.patch(f"/api/vault-master/assets/{member.id}/metadata", json={"display_title": "Corrected member", "track_number": 20}).status_code == 200
    assert store.get_asset_music_album(member.id).id == album.id
    assert store.get_music_album_order(album.id) == before
    tracks = client.get("/api/music").json()
    assert [t["asset_id"] for t in tracks if not t["album_group_id"]] == [str(foreign.id)]
