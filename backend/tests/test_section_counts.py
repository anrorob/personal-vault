from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
from app.auth import AuthenticatedIdentity
from app.auth_store import PostgresAuthenticationStore
from app.config import get_database_conninfo
from app.main import app
from .test_gallery_section_move import move_setup  # real isolated PostgreSQL catalogue fixture


def test_authorized_totals_ignore_filters_hidden_view_and_count_only_movies(move_setup):
    store, _, asset, _, _, account, conninfo = move_setup
    app.dependency_overrides[get_database_conninfo] = lambda: conninfo
    owner = AuthenticatedIdentity(account)
    def add(path, kind, **changes):
        item = replace(asset, id=uuid4(), vault_path=path, filename=path.rsplit('/', 1)[-1], asset_type=kind, **changes)
        store.restore_catalogued_asset(item, owner)
        return item
    hidden = add('/vault/Gallery/hidden.jpg', 'Gallery')
    store.set_catalogued_asset_lifecycle_state(hidden.id, account.user_id, owner, 'hidden')
    add('/vault/Gallery/scan.pdf', 'Gallery')
    add('/vault/Music/song.mp3', 'Music')
    add('/vault/Music/cover.jpg', 'Music')
    add('/vault/Home Videos/holiday.mp4', 'Home Videos')
    add('/vault/Theatre/Movies/Film/movie.mkv', 'Movie')
    add('/vault/Theatre/Movies/Film/extras/trailer.mkv', 'Movie')
    add('/vault/Theatre/TV Shows/Series/episode.mkv', 'Movie')
    add('/vault/Theatre/Movies/series.mkv', 'TV Show')
    other = replace(account, username="other", user_id=uuid4(), email="other@example.test")
    PostgresAuthenticationStore(conninfo).create_account(other)
    foreign = add('/vault/Gallery/private.jpg', 'Gallery', owner_user_id=other.user_id, owner_username=other.username)
    client = TestClient(app, base_url='https://testserver')
    expected = dict(gallery=3, music=1, home_videos=1, movies=1)
    for query in ('', '?include_hidden=true', '?photo_type=document&private_tag=ignored&shared=false'):
        response = client.get('/api/section-counts' + query)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.json() == expected
    # Counts use the same current grant evaluator, with no per-asset reads.
    with store._connect() as connection:
        connection.execute("UPDATE vault_assets SET visibility='vault-wide' WHERE id=%s", (foreign.id,))
    assert client.get('/api/section-counts').json()['gallery'] == 4
    with store._connect() as connection:
        connection.execute("UPDATE vault_assets SET lifecycle_state='hidden' WHERE id=%s", (foreign.id,))
    assert client.get('/api/section-counts').json()['gallery'] == 3

    with store._connect() as connection:
        connection.execute("UPDATE vault_assets SET visibility='private', lifecycle_state='active' WHERE id=%s", (foreign.id,))
    store.update_catalogued_asset_access(foreign.id, 'shared', (), AuthenticatedIdentity(other), local_all=True)
    assert client.get('/api/section-counts').json()['gallery'] == 4
    with store._connect() as connection:
        connection.execute("UPDATE vault_share_grants SET state='pending', activated_at=NULL WHERE asset_id=%s", (foreign.id,))
    assert client.get('/api/section-counts').json()['gallery'] == 3
    with store._connect() as connection:
        connection.execute("UPDATE vault_share_grants SET state='active', activated_at=CURRENT_TIMESTAMP, target_type='local_user', target_local_user_id=%s WHERE asset_id=%s", (account.user_id, foreign.id))
    assert client.get('/api/section-counts').json()['gallery'] == 4
    with store._connect() as connection:
        connection.execute("UPDATE vault_share_grants SET target_local_user_id=%s WHERE asset_id=%s", (other.user_id, foreign.id))
    assert client.get('/api/section-counts').json()['gallery'] == 3
    with store._connect() as connection:
        connection.execute("UPDATE vault_share_grants SET state='revoked', revoked_at=CURRENT_TIMESTAMP WHERE asset_id=%s", (foreign.id,))
    assert client.get('/api/section-counts').json()['gallery'] == 3
