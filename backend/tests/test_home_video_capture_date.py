"""Date-only UI uses the existing audited metadata route and effective consumers."""
from dataclasses import replace
from datetime import date
from tests.test_home_video_custom_tags import video


def test_date_override_updates_details_sort_and_filter_without_analysis(client, video, monkeypatch):
    vault, intelligence, asset, source = video
    import app.vault_libraries as libraries
    def forbidden(*args, **kwargs):
        raise AssertionError('Date correction must not enqueue analysis')
    monkeypatch.setattr(intelligence, 'queue', forbidden)
    from app import ken_corrections
    monkeypatch.setattr(ken_corrections.CorrectionsStore, 'queue_ken', forbidden)
    file_id = client.get('/api/personal-videos').json()[0]['id']
    detail_url = f'/api/personal-videos/{file_id}/details'
    assert client.get(detail_url).json()['captured_on'] is None
    original = replace(asset, metadata={'captured_at':'2015-02-03', 'capture_date_source':'embedded'},
        detected_metadata={'captured_on':'2015-02-03'}, imported_metadata={'captured_on':'2016-02-03'},
        user_overrides={'display_title':'Manual title'}, captured_on=date(2016,2,3))
    vault.catalogued_assets[asset.vault_path] = original
    jobs = dict(intelligence.jobs)
    before_bytes = source.read_bytes()
    response = client.patch(f'/api/vault-master/assets/{asset.id}/metadata',json={'captured_on':'2020-04-05'})
    assert response.status_code == 200, response.text
    saved = vault.get_catalogued_asset_by_id(asset.id)
    assert saved.id == asset.id and saved.owner_user_id == asset.owner_user_id
    assert saved.sha256 == asset.sha256 and source.read_bytes() == before_bytes
    assert saved.metadata == original.metadata and saved.detected_metadata == original.detected_metadata
    assert saved.imported_metadata == original.imported_metadata
    assert saved.user_overrides == {'display_title':'Manual title','captured_on':'2020-04-05'}
    assert saved.metadata_provenance['captured_on'] == 'user_override'
    assert saved.effective_metadata['captured_on'] == '2020-04-05'
    assert client.get(detail_url).json()['captured_on'] == '2020-04-05'
    assert client.get('/api/personal-videos').json()[0]['captured_on'] == '2020-04-05'
    assert len(client.get('/api/personal-videos?date_from=2020-04-05&date_to=2020-04-05').json()) == 1
    assert client.get('/api/personal-videos?date_to=2019-12-31').json() == []
    history = vault.list_catalogued_asset_history(asset.id)
    assert history[0]['action'] == 'metadata_updated'
    assert history[0]['previous_values'] == {'captured_on':'2016-02-03'}
    assert history[0]['current_values'] == {'captured_on':'2020-04-05'}
    assert intelligence.jobs == jobs
    # A second video demonstrates ordering uses the corrected canonical value.
    another = replace(original, id=__import__('uuid').uuid4(), vault_path='/vault/Home Videos/second.mp4', filename='second.mp4', captured_on=date(2018,1,1))
    (source.parent/'second.mp4').write_bytes(b'synthetic second video')
    vault.catalogued_assets[another.vault_path] = another
    assert [r['asset_id'] for r in client.get('/api/personal-videos?sort=newest').json()] == [str(asset.id),str(another.id)]
    assert [r['asset_id'] for r in client.get('/api/personal-videos?sort=oldest').json()] == [str(another.id),str(asset.id)]


def test_invalid_capture_date_is_rejected_without_mutation(client, video):
    vault, _, asset, _ = video
    assert client.patch(f'/api/vault-master/assets/{asset.id}/metadata',json={'captured_on':'2024-02-31'}).status_code == 422
    assert vault.get_catalogued_asset_by_id(asset.id) == asset
    assert vault.list_catalogued_asset_history(asset.id) == []


def test_postgres_date_and_private_routes_preserve_authority(client, postgres_store, postgres_conninfo, tmp_path, monkeypatch):
    from app.auth import AuthenticatedIdentity, require_authenticated_user
    from app.auth_store import PostgresAuthenticationStore
    from app.gallery_custom_tags import PostgresGalleryCustomTagStore, get_gallery_custom_tag_store
    from app.vault_master import get_vault_master_store, PostgresVaultMasterStore
    from app.vault_libraries import get_personal_videos_path
    from tests.test_postgres_vault_master import _catalogued_asset
    from uuid import uuid4
    from app import ken_corrections
    monkeypatch.setattr(ken_corrections.CorrectionsStore,'queue_ken',lambda *a,**k: (_ for _ in ()).throw(AssertionError('No KEN job')))
    owner = PostgresAuthenticationStore(postgres_conninfo).get_account('owner')
    recipient = PostgresAuthenticationStore(postgres_conninfo).get_account('son')
    user = AuthenticatedIdentity(owner)
    source = tmp_path/'synthetic.mp4'; source.write_bytes(b'synthetic-video')
    asset = replace(_catalogued_asset(uuid4(),'/vault/Home Videos/synthetic.mp4','owner'),asset_type='Home Videos',mime_type='video/mp4',metadata={'captured_at':'2010-01-02','capture_date_source':'embedded'},detected_metadata={'captured_on':'2010-01-02'},captured_on=date(2010,1,2))
    asset = postgres_store.restore_catalogued_asset(asset,'owner')
    private = PostgresGalleryCustomTagStore(postgres_conninfo); private.initialize()
    client.app.dependency_overrides[require_authenticated_user] = lambda: user
    client.app.dependency_overrides[get_vault_master_store] = lambda: postgres_store
    client.app.dependency_overrides[get_gallery_custom_tag_store] = lambda: private
    client.app.dependency_overrides[get_personal_videos_path] = lambda: tmp_path
    response = client.patch(f'/api/vault-master/assets/{asset.id}/metadata',json={'captured_on':'2022-03-04'})
    assert response.status_code == 200, response.text
    saved = PostgresVaultMasterStore(postgres_conninfo).get_catalogued_asset_by_id(asset.id)
    assert saved.captured_on == date(2022,3,4)
    assert saved.metadata == asset.metadata and saved.detected_metadata == asset.detected_metadata
    assert saved.user_overrides['captured_on'] == '2022-03-04'
    assert saved.metadata_provenance['captured_on'] == 'user_override'
    assert saved.sha256 == asset.sha256 and saved.owner_user_id == owner.user_id
    assert source.read_bytes() == b'synthetic-video'
    assert client.get('/api/personal-videos?date_to=2020-01-01').json() == []
    assert len(client.get('/api/personal-videos?date_from=2022-03-04').json()) == 1
    path = f'/api/personal-videos/assets/{asset.id}/private-tags'
    first = client.post(path,json={'display_name':'Private date test'}).json()
    assert client.post(path,json={'display_name':'Private date test'}).json()['id'] == first['id']
    postgres_store.update_catalogued_asset_access(asset.id,'shared',(recipient.username,),'owner')
    user = AuthenticatedIdentity(recipient)
    assert client.get(path).json() == []
    second = client.post(path,json={'display_name':'Private date test'})
    assert second.status_code == 201, second.text
    assert second.json()['id'] != first['id']
    assert len(client.get('/api/personal-videos',params={'private_tag':second.json()['id']}).json()) == 1
    assert client.get('/api/personal-videos',params={'private_tag':first['id']}).json() == []
    assert client.patch(f'/api/vault-master/assets/{asset.id}/metadata',json={'captured_on':'2023-01-01'}).status_code == 404
    postgres_store.update_catalogued_asset_access(asset.id,'private',(),'owner')
    assert client.get(path).status_code == 404
    assert client.get('/api/personal-videos',params={'private_tag':second.json()['id']}).json() == []


from tests.test_postgres_vault_master import postgres_store, postgres_conninfo
