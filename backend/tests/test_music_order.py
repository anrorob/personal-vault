from dataclasses import replace
from uuid import uuid4

import pytest

from app.music_order import metadata_sequence
from tests.test_music import configure, catalogue_track, authenticate
from tests.test_music_groups import intent


@pytest.mark.parametrize('coordinates', [
    [(1, None), (1, 2)], [(None, 1), (1, 2)], [(1, 1), (1, 1)],
    [(1, 0), (1, 2)], [(True, 1), (1, 2)], [(1, 'bad'), (1, 2)],
])
def test_ambiguous_metadata_never_establishes_sequence(coordinates):
    assert metadata_sequence([(uuid4(), {'disc_number': d, 'track_number': t}) for d, t in coordinates]) is None


def test_sequence_routes_and_listing_preserve_explicit_order(client, tmp_path):
    root, store = configure(tmp_path)
    assets = []
    for name, disc, track in [('A.flac', 2, 1), ('Z.flac', 1, '02'), ('M.flac', 1, 1)]:
        (root / name).write_bytes(b'synthetic')
        assets.append(catalogue_track(store, root, name, metadata_overrides={'display_title': name, 'disc_number': disc, 'track_number': track}))
    owner = assets[0].owner_user_id
    album = store.declare_music_album(owner, intent())
    store.bind_music_album(album.id, owner, [a.id for a in assets])
    assert store.get_music_album_order(album.id)['asset_ids'] == [assets[2].id, assets[1].id, assets[0].id]
    endpoint = f'/api/vault-master/music/albums/groups/{album.id}/sequence'
    sequence = [str(a.id) for a in assets]
    assert client.put(endpoint, json={'asset_ids': sequence}).status_code == 401
    authenticate(client)
    assert client.put(endpoint, json={'asset_ids': sequence[:-1]}).status_code == 409
    assert client.put(endpoint, json={'asset_ids': [sequence[0]] * 3}).status_code == 409
    response = client.put(endpoint, json={'asset_ids': sequence})
    assert response.status_code == 200 and response.json()['asset_ids'] == sequence
    history_count = len(store.music_album_history)
    assert client.put(endpoint, json={'asset_ids': sequence}).status_code == 200
    assert len(store.music_album_history) == history_count
    with pytest.raises(ValueError):
        store.set_music_album_order(album.id, uuid4(), [a.id for a in assets])
    # Enrichment/renaming cannot replace explicit positions.
    asset = assets[0]
    store.catalogued_assets[asset.vault_path] = replace(asset, effective_metadata={**asset.effective_metadata, 'track_number': 99, 'display_title': 'ZZZ'})
    tracks = client.get('/api/music').json()
    assert [t['asset_id'] for t in sorted(tracks, key=lambda t: t['album_position'])] == sequence
    assert all(t['album_order_state'] == 'ready' and t['album_member_count'] == 3 for t in tracks)
    # A visible subset must never be advertised as the complete album queue.
    del store.catalogued_assets[assets[1].vault_path]
    assert all(t['album_order_state'] == 'unresolved' and t['album_position'] is None for t in client.get('/api/music').json())


def test_missing_metadata_membership_survives_and_explicit_sequence_accepts_new_members(client, tmp_path):
    root, store = configure(tmp_path)
    assets = []
    for n in range(3):
        name = f'{n}.flac'; (root / name).write_bytes(b'synthetic')
        assets.append(catalogue_track(store, root, name, metadata_overrides={'disc_number': None, 'track_number': None}))
    owner = assets[0].owner_user_id
    album = store.declare_music_album(owner, intent())
    store.bind_music_album(album.id, owner, [a.id for a in assets[:2]])
    assert store.get_music_album_order(album.id) == {'state': 'unresolved', 'asset_ids': [], 'member_count': 2}
    store.set_music_album_order(album.id, owner, [a.id for a in assets[:2]])
    store.bind_music_album(album.id, owner, [assets[2].id])
    assert store.get_music_album_order(album.id)['state'] == 'unresolved'
    with pytest.raises(ValueError):
        store.set_music_album_order(album.id, owner, [a.id for a in assets[:2]])
    store.set_music_album_order(album.id, owner, [a.id for a in reversed(assets)])
    assert store.get_music_album_order(album.id)['asset_ids'] == [a.id for a in reversed(assets)]
