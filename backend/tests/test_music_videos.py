from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import music_videos
from app.arrival_managed_publisher import ArrivalManagedPublicationRequest
from app.music_video_identity import ROOT, declared_intent
from app.vault_master import ScannedFile, MemoryVaultMasterStore, create_deterministic_proposal, process_next_move
from tests.test_music import configure, catalogue_track, authenticate


def video_fixture(tmp_path):
    root, store = configure(tmp_path)
    (root / "test.mp4").write_bytes(b"synthetic video")
    original = catalogue_track(store, root, "test.mp4")
    del store.catalogued_assets[original.vault_path]
    asset = replace(original, asset_type="Music Videos", vault_path=ROOT + "/test.mp4",
                    mime_type="video/mp4", visibility="vault-wide", metadata={},
                    imported_metadata={}, effective_metadata={}, user_overrides={}, metadata_provenance={})
    store.catalogued_assets[asset.vault_path] = asset
    return store, asset


def test_individual_projection_owner_correction_and_no_ken_title(client, tmp_path):
    store, asset = video_fixture(tmp_path)
    store.catalogued_assets[asset.vault_path] = replace(asset, metadata={"ken_accepted_title": "Old derived title"})
    assert client.get("/api/music-videos").status_code == 401
    authenticate(client)
    initial = client.get("/api/music-videos").json()
    assert len(initial) == 1 and initial[0]["content_type"] == "music_video"
    assert initial[0]["artist"] is None and initial[0]["title"] is None
    url = f"/api/music-videos/{asset.id}/metadata"
    for values in ({"artist": "Band"}, {"artist": "Corrected band"}, {"title": "Title"}, {"title": "Corrected title"}):
        assert client.patch(url, json=values).status_code == 200
    result = client.get("/api/music-videos").json()[0]
    assert (result["artist"], result["title"]) == ("Corrected band", "Corrected title")
    after = store.get_catalogued_asset_by_id(asset.id)
    assert (after.id, after.owner_user_id, after.sha256) == (asset.id, asset.owner_user_id, asset.sha256)
    assert after.metadata["ken_accepted_title"] == "Old derived title"
    assert after.metadata_provenance["artist"] == "user_override"
    assert store.asset_history[-1]["action"] == "metadata_updated"
    assert client.patch(url, json={"album": "Forbidden"}).status_code == 422
    assert client.get("/api/music").json() == []


def test_visibility_and_every_media_route_fail_closed(client, tmp_path, monkeypatch):
    store, asset = video_fixture(tmp_path)
    authenticate(client)
    foreign = replace(asset, owner_user_id=uuid4())
    store.catalogued_assets[asset.vault_path] = foreign
    assert client.get("/api/music-videos").json()[0]["can_edit"] is False
    assert client.patch(f"/api/music-videos/{asset.id}/metadata", json={"title": "No"}).status_code == 404
    for changed in (replace(foreign, visibility="private"), replace(asset, asset_type="Home Videos"),
                    replace(asset, lifecycle_state="hidden"), replace(asset, owner_user_id=None)):
        store.catalogued_assets[asset.vault_path] = changed
        assert client.get("/api/music-videos").json() == []
        for route in ("playback", "content", "thumbnail"):
            assert client.get(f"/api/music-videos/{asset.id}/{route}").status_code == 404


def test_music_video_cannot_enter_home_video_ken(client, tmp_path):
    from app.ken_api import owner_asset
    from app.ken_service import prepare_input, KenFailure
    from types import SimpleNamespace
    from fastapi import HTTPException
    store, asset = video_fixture(tmp_path)
    identity = SimpleNamespace(user_id=asset.owner_user_id)
    with pytest.raises(HTTPException) as error:
        owner_asset(asset.id, identity, store)
    assert error.value.status_code == 404
    with pytest.raises(KenFailure):
        prepare_input(asset, {"prompt": "synthetic"}, tmp_path)


def test_pv_receiver_retains_explicit_future_contract_and_rejects_ambiguous_values():
    from app.vault_supplier_transfer import _source_context
    from fastapi import HTTPException
    assert _source_context({"content_type": "music_video", "artist": " Band ", "title": "Title"}) == {
        "content_type": "music_video", "artist": "Band", "title": "Title"}
    with pytest.raises(HTTPException):
        _source_context({"content_type": "music_video", "artist": {"name": "Band"}})


def scanned(tmp_path, context=None):
    path = tmp_path / "Band - Title.mp4"
    path.write_bytes(b"synthetic")
    return ScannedFile(str(path), path.name, path.name, 9, "video/mp4", datetime.now(timezone.utc),
                       "a" * 64, {"source_context": context} if context else {}, owner_user_id=uuid4())


def test_explicit_intent_queues_managed_publication_without_classification_review(tmp_path):
    scan = scanned(tmp_path, {"content_type": "music_video", "artist": "Band", "title": "Song"})
    store = MemoryVaultMasterStore()
    item = store.record_file(store.create_batch("incoming", str(tmp_path)), "incoming", scan)
    assert item.state == "move_queued"
    assert item.proposed_category == "Music Videos"
    requests = []
    process_next_move(store, tmp_path, {}, theatre_queue=lambda value: requests.append(ArrivalManagedPublicationRequest.create(item=value)))
    assert len(requests) == 1
    assert requests[0].logical_destination.startswith(ROOT + "/")
    assert requests[0].owner_user_id == scan.owner_user_id
    assert store.get_item(item.id).state == "theatre_promotion_pending"
    assert (tmp_path / scan.filename).exists()  # Only the privileged publisher moves bytes.


def test_generic_video_needs_review_and_only_explicit_selection_admits(tmp_path):
    scan = scanned(tmp_path)
    assert create_deterministic_proposal(scan)[0] == "Home Videos"
    store = MemoryVaultMasterStore()
    item = store.record_file(store.create_batch("incoming", str(tmp_path)), "incoming", scan)
    assert item.state == "needs_review"
    forged = replace(item, proposed_category="Music Videos", proposed_destination=ROOT + "/test.mp4")
    with pytest.raises(ValueError, match="explicit owner"):
        ArrivalManagedPublicationRequest.create(item=forged)
    selected = store.update_proposal(item.id, "Music Videos", str(scan.owner_user_id))
    assert ArrivalManagedPublicationRequest.create(item=selected).logical_destination.startswith(ROOT + "/")
    with pytest.raises(ValueError):
        declared_intent({"content_type": "music_video"}, "audio/flac")
    with pytest.raises(ValueError):
        declared_intent({"content_type": "music_video", "music_album": {}}, "video/mp4")


def test_playback_reuses_existing_derivative_pipeline(client, tmp_path, monkeypatch):
    store, asset = video_fixture(tmp_path)
    authenticate(client)
    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic playback")
    monkeypatch.setattr(music_videos, "media_source", lambda value: source)
    calls = []
    monkeypatch.setattr(music_videos, "playback_status", lambda path, identity, checksum, cache, retry=False: calls.append((identity, checksum)) or "direct")
    monkeypatch.setattr(music_videos, "playback_file", lambda *args: source)
    response = client.get(f"/api/music-videos/{asset.id}/playback")
    assert calls == [(asset.id, asset.sha256)]
    media = client.get(response.json()["playback_url"], headers={"Range": "bytes=0-3"})
    assert media.status_code == 206 and media.content == b"synt"
    assert media.headers["cache-control"] == "private, no-store"
