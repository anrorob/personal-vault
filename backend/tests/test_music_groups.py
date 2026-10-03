from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.main import app
from app.music import _to_track, resolve_visible_track, _track_id
from app.music_groups import item_album
from app.vault_master import MemoryVaultMasterStore, get_vault_master_store, asset_is_visible_to, asset_is_editable_by
from app.vault_master_music import get_musicbrainz_client
from tests.test_vault_master_music import album_track, authenticate, FakeProvider, selected_release


def intent(title="Example Release", source=None):
    return {"import_group_id": str(source or uuid4()), "artist_name": "Example Artist", "album_title": title}


def root_tracks(store, count=12):
    result = []
    for number in range(1, count + 1):
        asset = album_track(store, number)
        del store.catalogued_assets[asset.vault_path]
        asset = replace(asset, vault_path=f"/vault/Music/{asset.filename}")
        store.catalogued_assets[asset.vault_path] = asset
        result.append(asset)
    return result


def test_explicit_groups_owner_scope_membership_and_corrections():
    store = MemoryVaultMasterStore()
    tracks = root_tracks(store)
    owner = tracks[0].owner_user_id
    declaration = intent()
    example_release = store.declare_music_album(owner, declaration)
    assert store.declare_music_album(owner, declaration) == example_release
    store.bind_music_album(example_release.id, owner, [track.id for track in tracks])
    store.bind_music_album(example_release.id, owner, [track.id for track in tracks])
    origins = store.declare_music_album(owner, intent("Origins"))
    duplicate_name = store.declare_music_album(uuid4(), declaration)
    another_transfer = store.declare_music_album(owner, intent())
    assert len({example_release.id, origins.id, duplicate_name.id, another_transfer.id}) == 4
    assert len(store.music_album_asset_ids(example_release.id)) == 12
    origins_tracks = [album_track(store, number) for number in (13, 14)]
    store.bind_music_album(origins.id, owner, [track.id for track in origins_tracks])
    assert set(store.music_album_asset_ids(origins.id)) == {track.id for track in origins_tracks}
    with pytest.raises(ValueError):
        store.bind_music_album(origins.id, owner, [tracks[0].id])
    with pytest.raises(ValueError):
        store.bind_music_album(duplicate_name.id, duplicate_name.owner_user_id, [tracks[0].id])
    with pytest.raises(ValueError):
        store.declare_music_album(owner, {**declaration, "album_title": "Other"})
    updated = store.correct_music_album(example_release.id, owner, "Example Artist", "Example Release (my edition)")
    assert updated.id == example_release.id
    assert store.declare_music_album(owner, declaration) == updated  # Retried import cannot undo a correction.
    relocated = replace(tracks[0], vault_path="/vault/Music/elsewhere/renamed.wma")
    del store.catalogued_assets[tracks[0].vault_path]
    store.catalogued_assets[relocated.vault_path] = relocated
    assert store.get_asset_music_album(relocated.id).id == example_release.id
    assert len([entry for entry in store.music_album_history if entry[2] == "member_added" and entry[0] == example_release.id]) == 12


def test_real_group_routes_and_provider_enrichment_do_not_regroup(client, tmp_path):
    store = MemoryVaultMasterStore()
    tracks = root_tracks(store)
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_musicbrainz_client] = FakeProvider
    authenticate(client)
    declaration = intent()
    created = client.post("/api/vault-master/music/albums/groups", json=declaration)
    assert created.status_code == 200
    group_id = created.json()["id"]
    members = client.post(f"/api/vault-master/music/albums/groups/{group_id}/members", json={"asset_ids": [str(track.id) for track in tracks]})
    assert members.status_code == 200 and members.json()["member_count"] == 12
    request = {"folder": ".", "album_group_id": group_id, "release_id": selected_release().release_id}
    preview = client.post("/api/vault-master/music/albums/preview", json=request)
    assert preview.status_code == 200
    assert "Your album identity remains Example Release" in " ".join(preview.json()["identity_conflicts"])
    from app.config import get_metadata_storage_root
    app.dependency_overrides[get_metadata_storage_root] = lambda: tmp_path
    approved = client.post("/api/vault-master/music/albums/approve", json=request)
    assert approved.status_code == 200
    assert set(store.music_album_asset_ids(UUID(group_id))) == {track.id for track in tracks}
    album = store.get_music_album(UUID(group_id))
    assert (album.artist_name, album.album_title) == ("Example Artist", "Example Release")
    asset = store.get_catalogued_asset_by_id(tracks[0].id)
    assert asset.owner_user_id == tracks[0].owner_user_id and asset.sha256 == tracks[0].sha256
    track = _to_track(tmp_path / "01.wma", tmp_path, asset, album)
    assert (track.artist, track.album, track.album_group_id) == ("Example Artist", "Example Release", group_id)
    assert asset.imported_metadata["musicbrainz"]["suggested_album"] == selected_release().title
    assert client.post("/api/vault-master/music/albums/approve", json={"folder": ".", "release_id": selected_release().release_id}).status_code == 422
    assert client.post("/api/vault-master/music/albums/preview", json={**request, "album_group_id": str(uuid4())}).status_code == 422


def test_local_visibility_uses_theatre_policy_without_owner_or_state_transfer(tmp_path):
    store = MemoryVaultMasterStore()
    first = root_tracks(store, 1)[0]
    published = replace(first, visibility="vault-wide")
    store.catalogued_assets[first.vault_path] = published
    other = SimpleNamespace(user_id=uuid4())
    assert asset_is_visible_to(published, other)
    assert not asset_is_editable_by(published, other)
    assert not asset_is_visible_to(published, SimpleNamespace(vault_id=uuid4(), remote_user_id=uuid4()))
    assert published.owner_user_id == first.owner_user_id and published.size_bytes == first.size_bytes
    assert published.sha256 == first.sha256 and published.id == first.id
    root = tmp_path / "Music"
    root.mkdir()
    path = root / published.filename
    path.write_bytes(b"synthetic")
    assert resolve_visible_track(_track_id(path, root), other, root, store)[1].id == first.id
    assert len(store.catalogued_assets) == 1


def test_managed_music_routes_and_jellyfin_enrichment_use_authoritative_slot(client, tmp_path, monkeypatch):
    import json
    from app.music import get_music_library_path
    from app import music_playback_cache
    from app.jellyfin import JellyfinAudio
    from app.vault_master_jellyfin import import_jellyfin_music_library
    store = MemoryVaultMasterStore()
    asset = root_tracks(store, 1)[0]
    # The logged-in fixture user is a local recipient, not the owner.
    owner = uuid4()
    album = store.declare_music_album(owner, intent())
    legacy_root, slot = tmp_path / "Music", tmp_path / "slot"
    legacy_root.mkdir()
    path = slot / "Music" / asset.filename
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic audio")
    placement = {"slot_id": "PV-DEV-DISK-001", "relative_path": f"Music/{asset.filename}"}
    asset = replace(asset, owner_user_id=owner, visibility="vault-wide", metadata={"storage_placement": placement}, effective_metadata={"storage_placement": placement})
    store.catalogued_assets[asset.vault_path] = asset
    store.bind_music_album(album.id, owner, [asset.id])
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({"PV-DEV-DISK-001": str(slot)}))
    class Provider:
        def find_audio_by_path(self, candidate):
            assert candidate == path
            return JellyfinAudio("audio", "source", str(path), "flac", "flac", True, "album")
        def get_audio_details(self, audio):
            return {"artist": "Different provider artist", "album": "Provider edition", "release_year": 2017}
        def get_image_url(self, *args, **kwargs):
            return "https://jellyfin.invalid/cover"
    provider = Provider()
    assert import_jellyfin_music_library(store, legacy_root, provider) == (1, 0)
    assert store.get_asset_music_album(asset.id) == album
    imported = store.get_catalogued_asset_by_id(asset.id)
    assert imported.imported_metadata["music_identity_suggestion"]["album"] == "Provider edition"
    assert "album" not in imported.imported_metadata
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_music_library_path] = lambda: legacy_root
    def native_probe(candidate, locks=()):
        assert candidate == path  # Native playback resolves the same managed slot.
        return ["flac"], {"codec_name": "flac"}
    monkeypatch.setattr(music_playback_cache, "probe", native_probe)
    assert client.get("/api/music").status_code == 401
    authenticate(client)
    response = client.get("/api/music")
    assert response.status_code == 200 and len(response.json()) == 1
    track = response.json()[0]
    assert track["album"] == "Example Release" and track["artist"] == "Example Artist"
    assert track["owner_user_id"] == str(owner)
    assert track["enrichment_status"] == "needs_review"
    assert "Provider edition" in " ".join(track["identity_conflicts"])
    stream = client.get(track["playback_url"])
    assert stream.status_code == 200 and stream.content == b"synthetic audio"
    # Missing managed bytes must never fall back to an old logical-path copy.
    (legacy_root / asset.filename).write_bytes(b"stale")
    path.unlink()
    assert client.get(track["playback_url"]).status_code == 404


@pytest.mark.parametrize("upload_kind", ["explicit", "manual", "ambiguous"])
def test_supplier_group_contract_survives_transfer_and_arrival(client, authentication_store, tmp_path, monkeypatch, upload_kind):
    import hashlib
    from app.incoming import get_arrival_hall_file_source_context
    from tests.test_vault_supplier_transfer import _configure_arrival_hall, _authorized_headers
    from app.vault_master import enqueue_root, process_next_batch
    _configure_arrival_hall(tmp_path)
    headers = _authorized_headers(client, authentication_store)
    declaration = intent()
    source_id = str(uuid4())
    store = app.dependency_overrides[get_vault_master_store]()
    for number in range(2):
        payload = f"synthetic audio {number}".encode()
        body = {"protocol_version": 1, "filename": f"{number+1:02d}.wma", "total_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "media_type": "audio/x-ms-wma", "source_context": {"music_album": declaration, "source_label": "Original album folder"}}
        if upload_kind != "explicit":
            body["source_context"] = {"source_kind": "manual_upload", "source_id": source_id,
                "source_label": "Example Artist - Example Album Act 1" if upload_kind == "manual" else "Unsplit label",
                "relative_path": body["filename"]}
        created = client.post("/api/vault-supplier/transfers", headers=headers, json=body)
        assert created.status_code == 201, created.text
        assert len(store._music_albums) == 1  # Group exists before bytes/finalization/review.
        transfer = created.json()["transfer_id"]
        assert client.put(f"/api/vault-supplier/transfers/{transfer}/data", headers={**headers, "X-PV-Upload-Offset": "0", "Content-Length": str(len(payload))}, content=payload).status_code == 200
        finalized = client.post(f"/api/vault-supplier/transfers/{transfer}/finalize", headers=headers)
        assert finalized.status_code == 200
        path = tmp_path / "Arrival Hall" / finalized.json()["arrival_hall_filename"]
        context = get_arrival_hall_file_source_context(path.parent, path)
        if upload_kind != "explicit":
            if number == 0:
                declaration = context["music_album"]
            assert context["source_id"] == source_id
            assert declaration["artist_name"] == ("Example Artist" if upload_kind == "manual" else "")
        assert context["music_album"] == declaration
        assert context["original_filename"] == body["filename"]
    root = tmp_path / "Arrival Hall"
    enqueue_root(store, root, "incoming")
    process_next_batch(store, owner_lookup=lambda path: authentication_store.get_account("owner").user_id, source_context_lookup=lambda path: get_arrival_hall_file_source_context(root, path))
    albums = [item_album(item) for item in store.items.values()]
    assert len(albums) == 2 and albums[0].id == albums[1].id
    assert all(str(albums[0].id) in item.proposed_destination for item in store.items.values())
    for item in list(store.items.values()):
        store.items[item.source_path] = replace(item, metadata={**item.metadata, "disc_number": 1,
            "track_number": int(item.metadata["source_context"]["original_filename"][:2])})
    # Publication operates below grouping/search. It must never use the old direct mover.
    from app.vault_master import process_next_move
    from app import vault_master
    from app.arrival_managed_publisher import ArrivalManagedPublicationRequest
    def direct_move_forbidden(*args, **kwargs):
        raise AssertionError("Music must use managed publication")
    monkeypatch.setattr(vault_master, "safely_move_approved_file", direct_move_forbidden)
    requests = []
    def queue(item):
        request = ArrivalManagedPublicationRequest.create(item=item)
        requests.append(request)
        return request.request_id
    if upload_kind != "explicit":
        authenticate(client)
        ids = [str(item.id) for item in store.items.values()]
        url = f"/api/vault-master/music/imports/{albums[0].id}/approve"
        assert client.post(url, json={"item_ids": ids[:1]}).status_code == 409
        assert all(item.state == "needs_review" for item in store.items.values())
        assert client.get("/api/vault-master/recovery").json()["items"] == []
        assert client.post(url, json={"item_ids": ids}).status_code == 200
        assert client.post(url, json={"item_ids": ids}).status_code == 200
        assert client.get("/api/vault-master/recovery").json()["items"] == []
    for item in list(store.items.values()):
        if upload_kind == "explicit":
            store.items[item.source_path] = replace(item, state="approved")
            assert store.queue_move(item.id, "owner") is not None
        process_next_move(store, root, {}, theatre_queue=queue)
    assert len(requests) == 2
    for request in requests:
        receipt = {"request_id": str(request.request_id), "item_id": str(request.item_id), "owner_user_id": str(request.owner_user_id), "logical_destination": request.logical_destination, "logical_area": "Music", "slot_id": "PV-DEV-DISK-001", "relative_path": request.logical_destination.removeprefix("/vault/"), "expected_sha256": request.expected_sha256, "expected_size_bytes": request.expected_size_bytes}
        published = store.publish_arrival_managed_receipt(request.item_id, receipt)
        assert published is not None and published.visibility == "vault-wide"
        assert published.owner_user_id == request.owner_user_id
        assert published.sha256 == request.expected_sha256
        assert published.metadata["source_context"]["music_album"] == declaration
        assert store.get_asset_music_album(published.id).id == albums[0].id
        assert store.publish_arrival_managed_receipt(request.item_id, receipt) is None
        if len(store.music_album_asset_ids(albums[0].id)) == 1:
            assert store.get_music_album_order(albums[0].id)["state"] == "unresolved"
    assert len(store.music_album_asset_ids(albums[0].id)) == 2
    order = store.get_music_album_order(albums[0].id)
    assert order["state"] == "ready"
    assert [store.get_catalogued_asset_by_id(asset_id).effective_metadata["track_number"] for asset_id in order["asset_ids"]] == [1, 2]
