"""Synthetic explicit albums keep identification separate from playback order."""
from dataclasses import replace

import pytest

from app.main import app
from app.music import _to_track, get_music_library_path
from app.config import get_metadata_storage_root
from app.vault_master import MemoryVaultMasterStore, get_vault_master_store
from app.vault_master_music import get_musicbrainz_client, _match_release
from tests.test_music_groups import root_tracks, intent
from tests.test_vault_master_music import authenticate, FakeProvider, selected_release


@pytest.mark.parametrize("ordered", [False, True])
def test_group_search_preview_approval_preserves_order_and_manual_metadata(client, tmp_path, ordered):
    store = MemoryVaultMasterStore()
    assets = []
    root = tmp_path / "Music"
    root.mkdir()
    for number, original in enumerate(root_tracks(store, 14), 1):
        # Numeric filenames deliberately cannot supply absent track evidence.
        metadata = {"display_title": f"Example Song {number}", "duration_seconds": 12.0}
        if ordered:
            metadata.update(disc_number=1, track_number=number)
        asset = replace(original, detected_metadata=metadata, effective_metadata=metadata,
                        imported_metadata={"provider": {"name": "jellyfin"},
                                           "music_identity_suggestion": {"artist": None, "album": None}})
        store.catalogued_assets[asset.vault_path] = asset
        (root / asset.filename).write_bytes(b"synthetic audio")
        assets.append(asset)
    owner = assets[0].owner_user_id
    album = store.declare_music_album(owner, intent("Example Album Act 1"))
    store.bind_music_album(album.id, owner, [asset.id for asset in assets])
    before = store.get_music_album_order(album.id)
    store.update_catalogued_asset_metadata(assets[0].id, {"display_title": "My title", "release_year": 1999}, "owner")
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_music_library_path] = lambda: root
    app.dependency_overrides[get_musicbrainz_client] = FakeProvider
    app.dependency_overrides[get_metadata_storage_root] = lambda: tmp_path / "metadata"
    authenticate(client)
    request = {"folder": ".", "album_group_id": str(album.id)}
    search = client.post("/api/vault-master/music/albums/search", json={**request, "artist": "Example Artist", "album": "Example Album Act 1"})
    assert search.status_code == 200 and search.json()["local_track_count"] == 14
    selection = {**request, "release_id": selected_release().release_id}
    preview = client.post("/api/vault-master/music/albums/preview", json=selection)
    assert preview.status_code == 200
    # Reviewed matcher flags the deliberately large duration/title conflicts.
    assert preview.json()["matched_track_count"] == 0
    if ordered:
        assert any(row["status"] == "review" for row in preview.json()["local_matches"])
    assert all("musicbrainz" not in store.get_catalogued_asset_by_id(asset.id).imported_metadata for asset in assets)
    approved = client.post("/api/vault-master/music/albums/approve", json=selection)
    assert approved.status_code == 200 and approved.json()["updated_track_count"] == 14
    assert approved.json()["artwork_retained"] is True
    assert store.get_music_album_order(album.id) == before
    assert before["state"] == ("ready" if ordered else "unresolved")
    assert store.get_music_album(album.id) == album
    assert set(store.music_album_asset_ids(album.id)) == {asset.id for asset in assets}
    for original in assets:
        current = store.get_catalogued_asset_by_id(original.id)
        assert (current.id, current.sha256, current.vault_path, current.owner_user_id) == (original.id, original.sha256, original.vault_path, original.owner_user_id)
        assert (root / current.filename).read_bytes() == b"synthetic audio"
        if not ordered:
            assert "track_number" not in current.effective_metadata
            assert "disc_number" not in current.effective_metadata
            assert current.effective_metadata["duration_seconds"] == 12.0
        assert current.imported_metadata["musicbrainz"]["release_id"] == selection["release_id"]
    corrected = store.get_catalogued_asset_by_id(assets[0].id)
    assert corrected.display_title == "My title" and corrected.effective_metadata["release_year"] == 1999
    listing = client.get("/api/music").json()
    assert len(listing) == 14 and {track["album_group_id"] for track in listing} == {str(album.id)}
    assert {track["album"] for track in listing} == {"Example Album Act 1"}
    assert all(track["playback_url"] for track in listing)
    assert {track["album_order_state"] for track in listing} == {before["state"]}
    class NoCoverProvider(FakeProvider):
        def get_release(self, release_id):
            return replace(super().get_release(release_id), cover_art_available=False)
    previous_artwork = corrected.imported_metadata["artwork"]
    app.dependency_overrides[get_musicbrainz_client] = NoCoverProvider
    assert client.post("/api/vault-master/music/albums/approve", json=selection).status_code == 200
    assert store.get_catalogued_asset_by_id(assets[0].id).imported_metadata["artwork"] == previous_artwork
    assert store.get_music_album_order(album.id) == before


def test_playback_provider_marker_does_not_identify_a_declared_album(tmp_path):
    store = MemoryVaultMasterStore()
    asset = root_tracks(store, 1)[0]
    album = store.declare_music_album(asset.owner_user_id, intent())
    imported = {"provider": {"name": "jellyfin"}, "music_identity_suggestion": {"artist": None, "album": None}}
    asset = replace(asset, imported_metadata=imported)
    assert _to_track(tmp_path / asset.filename, tmp_path, asset, album).enrichment_status == "needs_review"
    approved = replace(asset, imported_metadata={**imported, "musicbrainz": {"release_id": selected_release().release_id}})
    assert _to_track(tmp_path / asset.filename, tmp_path, approved, album).enrichment_status == "identified"


def test_ambiguous_coordinates_are_unmatched_for_explicit_groups():
    store = MemoryVaultMasterStore()
    assets = [replace(asset, effective_metadata={"disc_number": 1, "track_number": 1}) for asset in root_tracks(store, 2)]
    assert all(asset is None for _, asset in _match_release(assets, selected_release(), explicit_group=True))
    release = selected_release()
    duplicated = replace(release, tracks=(release.tracks[0], release.tracks[0]))
    assert all(asset is None for _, asset in _match_release(assets[:1], duplicated, explicit_group=True))
