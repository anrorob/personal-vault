"""Synthetic edition discovery; no real provider identities or Vault data."""
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.main import app
from app.vault_master import MemoryVaultMasterStore, get_vault_master_store
from app.vault_master_music import MusicBrainzClient, get_musicbrainz_client
from tests.test_music_groups import root_tracks, intent
from tests.test_vault_master_music import authenticate


def edition(number, tracks, title="Example Album", qualifier=None):
    return {"id": f"00000000-0000-4000-8000-{number:012d}", "title": title,
            "disambiguation": qualifier, "artist-credit": [{"name": "Example Artist"}],
            "release-group": {"id": "00000000-0000-4000-8000-000000000099"},
            "score": 100, "country": "GB", "date": "2001-02-03",
            "barcode": "0000000000000", "label-info": [{"catalog-number": "EXAMPLE-001"}],
            "media": [{"track-count": tracks, "format": "CD"}]}


def test_paging_keeps_material_editions_and_only_deduplicates_release_ids(monkeypatch):
    client = MusicBrainzClient(minimum_interval_seconds=0)
    rows = [edition(1, 11), edition(2, 15), edition(3, 14, qualifier="international deluxe"),
            edition(4, 14, "Example Album (Special Edition)")]
    pages = [rows[:2], [rows[1], rows[2]], [rows[3]]]
    calls = []
    def fetch(url, **kwargs):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        return {"count": 5, "releases": pages[len(calls)-1]}
    monkeypatch.setattr(client, "_request_json", fetch)
    found = client.search_releases("Example Artist", "Example Album", limit=2)
    assert [r["track_count"] for r in found] == [11, 15, 14, 14]
    assert len({r["release_group_id"] for r in found}) == 1
    assert [c["offset"] for c in calls] == [["0"], ["2"], ["4"]]
    assert all(c["query"] == ['artist:"Example Artist" AND release:(Example Album)'] for c in calls)
    assert found[2]["disambiguation"] == "international deluxe"
    assert found[3]["title"] == "Example Album (Special Edition)"
    assert found[2]["formats"] == ["CD"] and found[2]["catalog_numbers"] == ["EXAMPLE-001"]
    assert found[2]["barcode"] == "0000000000000"


def test_page_cap_does_not_silently_return_an_incomplete_shortlist(monkeypatch):
    client = MusicBrainzClient(minimum_interval_seconds=0)
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return {"count": 6, "releases": [edition(len(calls), 11)]}
    monkeypatch.setattr(client, "_request_json", fetch)
    with pytest.raises(ValueError, match="refine"):
        client.search_releases("Example Artist", "Example Album", limit=1)
    assert len(calls) == 5


def test_unresolved_group_ranks_matching_count_without_hiding_other_editions(client, monkeypatch):
    store = MemoryVaultMasterStore()
    assets = root_tracks(store, 14)
    for asset in assets:
        store.catalogued_assets[asset.vault_path] = replace(asset, detected_metadata={}, effective_metadata={})
    owner = assets[0].owner_user_id
    group = store.declare_music_album(owner, intent())
    store.bind_music_album(group.id, owner, [a.id for a in assets])
    before = store.get_music_album_order(group.id)
    assert before["state"] == "unresolved"
    provider = MusicBrainzClient(minimum_interval_seconds=0)
    rows = [edition(1, 11), edition(2, 15), edition(3, 14, qualifier="international deluxe")]
    monkeypatch.setattr(provider, "_request_json", lambda *a, **k: {"count": 3, "releases": rows})
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_musicbrainz_client] = lambda: provider
    authenticate(client)
    response = client.post("/api/vault-master/music/albums/search", json={
        "folder": ".", "album_group_id": str(group.id), "artist": "Example Artist", "album": "Example Album"})
    assert response.status_code == 200
    assert response.json()["local_track_count"] == 14
    assert [r["track_count"] for r in response.json()["candidates"]] == [14, 15, 11]
    assert store.get_music_album_order(group.id) == before
    assert all("musicbrainz" not in a.imported_metadata for a in store.catalogued_assets.values())
