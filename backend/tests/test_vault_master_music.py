import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from uuid import uuid4, uuid5, NAMESPACE_URL

from fastapi.testclient import TestClient

from app.config import get_metadata_storage_root
from app.main import app
from app.vault_master import CataloguedAsset, MemoryVaultMasterStore, get_vault_master_store
from app.vault_master_music import (
    MusicBrainzClient,
    MusicMetadataProviderError,
    ProviderRelease,
    ProviderTrack,
    get_musicbrainz_client,
)
from app import vault_master_music
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200


def album_track(
    store: MemoryVaultMasterStore,
    number: int,
    *,
    owner: str = TEST_USERNAME,
) -> CataloguedAsset:
    filename = f"{number:02d} Track{number:02d}.wma"
    vault_path = f"/vault/Music/ID - Example Album Act 1/{filename}"
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type="Music",
        display_title=f"Track{number:02d}",
        captured_on=None,
        location=None,
        vault_path=vault_path,
        filename=filename,
        size_bytes=10,
        mime_type="audio/x-ms-wma",
        sha256=f"{number:064x}",
        metadata={},
        metadata_provenance={"display_title": "filename"},
        detected_metadata={
            "display_title": f"Track{number:02d}",
            "track_number": str(number),
        },
        effective_metadata={
            "display_title": f"Track{number:02d}",
            "track_number": str(number),
        },
        owner_username=owner,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{owner}"),
    )
    store.catalogued_assets[vault_path] = asset
    return asset


def selected_release() -> ProviderRelease:
    return ProviderRelease(
        release_id="12345678-1234-4123-8123-123456789abc",
        release_group_id="87654321-4321-4321-8321-cba987654321",
        title="Example Album – Act 1",
        artist="Example Artist",
        date="2001-02-03",
        country="GB",
        genres=("Alternative rock",),
        tracks=(
            ProviderTrack(1, 1, "1", "Example Song One", "Example Artist", "recording-1", 224.0),
            ProviderTrack(1, 2, "2", "Example Song Two", "Example Artist", "recording-2", 159.0),
            ProviderTrack(1, 7, "7", "Example Song Seven", "Example Artist", "recording-7", 207.0),
        ),
        cover_art_available=True,
    )


class FakeProvider:
    def search_releases(self, artist: str, album: str, limit: int = 5):
        assert (artist, album, limit) == ("Example Artist", "Example Album Act 1", 5)
        return [
            {
                "release_id": selected_release().release_id,
                "title": selected_release().title,
                "artist": selected_release().artist,
                "date": selected_release().date,
                "country": selected_release().country,
                "track_count": 3,
                "score": 100,
                "cover_art_available": True,
            }
        ]

    def get_release(self, release_id: str) -> ProviderRelease:
        assert release_id == selected_release().release_id
        return selected_release()

    def get_front_cover(self, release_id: str, max_bytes: int):
        assert release_id == selected_release().release_id
        assert max_bytes >= 10
        return b"jpeg-cover", "image/jpeg"


def configure(tmp_path: Path):
    store = MemoryVaultMasterStore()
    first = album_track(store, 1)
    seventh = album_track(store, 7)
    album_track(store, 11, owner="another-family-member")
    metadata_root = tmp_path / "metadata"
    metadata_root.mkdir()
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_musicbrainz_client] = lambda: FakeProvider()
    app.dependency_overrides[get_metadata_storage_root] = lambda: metadata_root
    return store, first, seventh, metadata_root


def test_album_search_and_preview_are_review_only(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store, first, seventh, _ = configure(tmp_path)
    authenticate(client)

    search = client.post(
        "/api/vault-master/music/albums/search",
        json={
            "folder": "ID - Example Album Act 1",
            "artist": "Example Artist",
            "album": "Example Album Act 1",
        },
    )
    preview = client.post(
        "/api/vault-master/music/albums/preview",
        json={
            "folder": "ID - Example Album Act 1",
            "release_id": selected_release().release_id,
        },
    )

    assert search.status_code == 200
    assert search.json()["local_track_count"] == 2
    assert search.json()["candidates"][0]["artist"] == "Example Artist"
    assert preview.status_code == 200
    assert preview.json()["matched_track_count"] == 2
    assert [track["matched"] for track in preview.json()["tracks"]] == [True, False, True]
    assert store.get_catalogued_asset_by_id(first.id).user_overrides == {}
    assert store.get_catalogued_asset_by_id(seventh.id).user_overrides == {}


def test_provider_outage_does_not_change_album_catalogue(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store, first, _, _ = configure(tmp_path)

    class UnavailableProvider(FakeProvider):
        def search_releases(self, artist: str, album: str, limit: int = 5):
            raise MusicMetadataProviderError("The online music catalogue is unavailable")

    app.dependency_overrides[get_musicbrainz_client] = lambda: UnavailableProvider()
    authenticate(client)

    response = client.post(
        "/api/vault-master/music/albums/search",
        json={
            "folder": "ID - Example Album Act 1",
            "artist": "Example Artist",
            "album": "Example Album Act 1",
        },
    )

    assert response.status_code == 503
    assert store.get_catalogued_asset_by_id(first.id).user_overrides == {}


def test_approved_album_retains_metadata_and_cover_without_changing_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store, first, seventh, metadata_root = configure(tmp_path)
    authenticate(client)

    response = client.post(
        "/api/vault-master/music/albums/approve",
        json={
            "folder": "ID - Example Album Act 1",
            "release_id": selected_release().release_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "folder": "ID - Example Album Act 1",
        "release_id": selected_release().release_id,
        "updated_track_count": 2,
        "artwork_retained": True,
    }
    updated_first = store.get_catalogued_asset_by_id(first.id)
    updated_seventh = store.get_catalogued_asset_by_id(seventh.id)
    assert updated_first is not None
    assert updated_seventh is not None
    assert updated_first.effective_metadata["display_title"] == "Example Song One"
    assert updated_seventh.effective_metadata["display_title"] == "Example Song Seven"
    assert updated_first.effective_metadata["artist"] == "Example Artist"
    assert updated_first.effective_metadata["album"] == "Example Album – Act 1"
    assert updated_first.imported_metadata["musicbrainz"]["release_id"] == selected_release().release_id
    assert updated_first.user_overrides["artist"] == "Example Artist"
    assert updated_first.sha256 == first.sha256
    history = store.list_catalogued_asset_history(first.id)
    assert history[0]["current_values"]["artist"] == "Example Artist"
    assert (metadata_root / "artwork" / str(first.id) / "primary").read_bytes() == b"jpeg-cover"


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


def test_musicbrainz_search_uses_fixed_release_api(
    monkeypatch,
) -> None:
    requested: list[Request] = []

    def fake_urlopen(request: Request, timeout: float):
        requested.append(request)
        assert timeout == 15
        return FakeResponse(
            {
                "releases": [
                    {
                        "id": selected_release().release_id,
                        "title": "Example Album – Act 1",
                        "artist-credit": [{"name": "Example Artist"}],
                        "date": "2001-02-03",
                        "country": "GB",
                        "score": 100,
                        "media": [{"track-count": 13}],
                        "cover-art-archive": {"front": True},
                    }
                ]
            }
        )

    monkeypatch.setattr(vault_master_music, "urlopen", fake_urlopen)
    provider = MusicBrainzClient(minimum_interval_seconds=0)

    results = provider.search_releases("Example Artist", "Example Album Act 1")

    assert results[0]["track_count"] == 13
    parts = urlsplit(requested[0].full_url)
    assert parts.scheme == "https"
    assert parts.hostname == "musicbrainz.org"
    assert parts.path == "/ws/2/release/"
    assert parse_qs(parts.query)["fmt"] == ["json"]
    assert "Example Artist" in parse_qs(parts.query)["query"][0]

def test_root_music_search_uses_real_client_but_never_treats_root_as_one_album(client, tmp_path, monkeypatch):
    from dataclasses import replace
    store, first, _, _ = configure(tmp_path)
    root = replace(first, vault_path='/vault/Music/01 root.wma', filename='01 root.wma',owner_username='old-label')
    store.catalogued_assets[root.vault_path] = root
    foreign = replace(root,id=uuid4(),owner_user_id=uuid4(),owner_username=TEST_USERNAME,vault_path='/vault/Music/02 foreign.wma')
    store.catalogued_assets[foreign.vault_path] = foreign
    calls=[]
    def reply(request, timeout):
        calls.append(request)
        assert parse_qs(urlsplit(request.full_url).query)['query']==['artist:"Example Artist" AND release:(Example Release)']
        return FakeResponse({'releases':[{'id':selected_release().release_id,'title':'Example Release','artist-credit':[{'name':'Example Artist'}],'date':'2002-03-04','media':[{'track-count':12}]}]})
    monkeypatch.setattr(vault_master_music,'urlopen',reply)
    app.dependency_overrides[get_musicbrainz_client]=lambda:MusicBrainzClient(minimum_interval_seconds=0)
    authenticate(client)
    original=dict(store.catalogued_assets)
    result=client.post('/api/vault-master/music/albums/search',json={'folder':'.','artist':'Example Artist','album':'Example Release'})
    assert result.status_code==200
    assert result.json()['local_track_count']==1  # Excludes nested folders and a same-name different UUID owner.
    assert result.json()['candidates'][0]['title']=='Example Release'
    assert result.json()['candidates'][0]['track_count']==12
    for action in ('preview','approve'):
        denied=client.post(f'/api/vault-master/music/albums/{action}',json={'folder':'.','release_id':selected_release().release_id})
        assert denied.status_code==422
        assert 'Album grouping is required' in denied.json()['detail']
    assert len(calls)==1
    assert store.catalogued_assets==original


def test_root_search_requires_owned_root_tracks_and_rejects_traversal(client,tmp_path,monkeypatch):
    store,_,_,_=configure(tmp_path)
    authenticate(client)
    def never(*args,**kwargs): raise AssertionError('Invalid scope must not reach provider')
    monkeypatch.setattr(vault_master_music,'urlopen',never)
    app.dependency_overrides[get_musicbrainz_client]=lambda:MusicBrainzClient(minimum_interval_seconds=0)
    for folder in ('.','../Music','/vault/Music','album/../other'):
        result=client.post('/api/vault-master/music/albums/search',json={'folder':folder,'artist':'Example Artist','album':'Example Release'})
        assert result.status_code==422


def test_real_provider_boundary_distinguishes_zero_results_from_failure(client,tmp_path,monkeypatch):
    from urllib.error import HTTPError
    configure(tmp_path); authenticate(client)
    app.dependency_overrides[get_musicbrainz_client]=lambda:MusicBrainzClient(minimum_interval_seconds=0)
    request={'folder':'ID - Example Album Act 1','artist':'Example Artist','album':'Example Release'}
    monkeypatch.setattr(vault_master_music,'urlopen',lambda *a,**k:FakeResponse({'releases':[]}))
    result=client.post('/api/vault-master/music/albums/search',json=request)
    assert result.status_code==200 and result.json()['candidates']==[]
    for error in [TimeoutError(),HTTPError('https://musicbrainz.org',503,'Unavailable',{},None)]:
        def fail(*args,**kwargs): raise error
        monkeypatch.setattr(vault_master_music,'urlopen',fail)
        assert client.post('/api/vault-master/music/albums/search',json=request).status_code==503


def test_example_album_ui_request_retries_transient_provider_failure_without_changing_album(client, tmp_path, monkeypatch):
    from urllib.error import HTTPError
    from app.music import _to_track
    store, first, _, _ = configure(tmp_path)
    album = store.declare_music_album(first.owner_user_id, {
        'import_group_id': str(uuid4()), 'artist_name': 'EXAMPLE ARTIST', 'album_title': 'Example Album Act 1',
    })
    store.bind_music_album(album.id, first.owner_user_id, [first.id])
    track = _to_track(tmp_path / 'Albums' / str(album.id) / first.filename, tmp_path, first, album)
    request = {'folder': track.album_folder, 'album_group_id': track.album_group_id,
               'artist': track.artist, 'album': track.album}
    assert request == {'folder': f'Albums/{album.id}', 'album_group_id': str(album.id),
                       'artist': 'EXAMPLE ARTIST', 'album': 'Example Album Act 1'}
    calls = []
    delays = []
    def reply(req, timeout):
        calls.append(req.full_url)
        assert parse_qs(urlsplit(req.full_url).query)['query'] == ['artist:"EXAMPLE ARTIST" AND release:(Example Album Act 1)']
        if len(calls) == 1:
            raise HTTPError(req.full_url, 503, 'Service Temporarily Unavailable', {'Retry-After': '2'}, None)
        return FakeResponse({'releases': [{'id': selected_release().release_id, 'title': 'Example Album – Act 1',
                            'artist-credit': [{'name': 'Example Artist'}], 'media': [{'track-count': 13}]}]})
    monkeypatch.setattr(vault_master_music, 'urlopen', reply)
    monkeypatch.setattr(vault_master_music.time, 'sleep', delays.append)
    app.dependency_overrides[get_musicbrainz_client] = lambda: MusicBrainzClient(minimum_interval_seconds=0)
    authenticate(client)
    original = dict(store.catalogued_assets)
    result = client.post('/api/vault-master/music/albums/search', json=request)
    assert result.status_code == 200
    assert result.json()['candidates'][0]['track_count'] == 13
    assert calls[0] == calls[1] and len(calls) == 2 and delays == [2]
    assert store.catalogued_assets == original
    assert store.music_album_asset_ids(album.id) == [first.id]


def test_search_provider_errors_are_bounded_and_distinct_from_validation(client, tmp_path, monkeypatch):
    from urllib.error import HTTPError
    configure(tmp_path); authenticate(client)
    app.dependency_overrides[get_musicbrainz_client] = lambda: MusicBrainzClient(minimum_interval_seconds=0)
    monkeypatch.setattr(vault_master_music.time, 'sleep', lambda _: None)
    request = {'folder': 'ID - Example Album Act 1', 'artist': 'EXAMPLE ARTIST', 'album': 'Example Album Act 1'}
    for code, headers, expected, attempts in [(503, {}, 503, 3), (429, {'Retry-After': '60'}, 503, 1), (400, {}, 502, 1)]:
        calls = []
        def fail(req, timeout):
            calls.append(req.full_url)
            raise HTTPError(req.full_url, code, 'Provider error', headers, None)
        monkeypatch.setattr(vault_master_music, 'urlopen', fail)
        result = client.post('/api/vault-master/music/albums/search', json=request)
        assert result.status_code == expected
        assert len(calls) == attempts
    monkeypatch.setattr(vault_master_music, 'urlopen', lambda *a, **k: FakeResponse({'error': 'invalid upstream payload'}))
    assert client.post('/api/vault-master/music/albums/search', json=request).status_code == 502
    monkeypatch.setattr(vault_master_music, 'urlopen', lambda *a, **k: FakeResponse({'releases': []}))
    result = client.post('/api/vault-master/music/albums/search', json=request)
    assert result.status_code == 200 and result.json()['candidates'] == []
    assert client.post('/api/vault-master/music/albums/search', json={**request, 'album_group_id': 'invalid'}).status_code == 422
