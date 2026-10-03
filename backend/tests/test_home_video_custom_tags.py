"""Home Video private/global boundaries through real routes; synthetic media only."""
from dataclasses import replace
from uuid import uuid4
import pytest
from app import vault_libraries as libraries
from app.gallery_custom_tags import get_gallery_custom_tag_store
from app.home_video_tags import system_terms, effective_system_tags
from app.gallery_tag_migration import reconcile_legacy_tags
from tests.test_gallery_tag_migration import tag_database
from tests.test_video_intelligence import _configure_video_api
from tests.test_vault_libraries import catalogue_file, authenticate


@pytest.fixture
def video(client, tmp_path, monkeypatch):
    vault, intelligence, root = _configure_video_api(tmp_path, monkeypatch)
    source = root / 'synthetic.mp4'; source.write_bytes(b'original')
    asset = catalogue_file(vault, root, source, asset_type='Home Videos', vault_root='/vault/Home Videos', mime_type='video/mp4')
    authenticate(client)
    return vault, intelligence, asset, source


def test_private_create_is_not_global_and_uuid_scoped(client, video):
    vault, _, asset, source = video
    path = f'/api/personal-videos/assets/{asset.id}/private-tags'
    private = client.app.dependency_overrides[get_gallery_custom_tag_store]()
    first = client.post(path, json={'display_name':'Holiday'}).json()
    assert client.post(f'/api/personal-videos/intelligence/{asset.id}/tags', json={'display_name':'Holiday'}).json()['id'] == first['id']
    other = private.create(uuid4(), 'Holiday')
    unused = private.create(asset.owner_user_id, 'Unused')
    assert str(other.id) != first['id']
    assert [tag['id'] for tag in client.get(path).json()] == [first['id']]
    choices = client.get('/api/personal-videos/filter-options').json()
    assert {tag['id'] for tag in choices['private_tags']} == {first['id'], str(unused.id)}
    assert not any(tag['slug']=='holiday' for tag in choices['content_tags'])
    assert not any(tag['slug']=='holiday' for tag in client.get('/api/personal-videos/intelligence/terms').json())
    file_id = client.get('/api/personal-videos').json()[0]['id']
    assert client.get(f'/api/personal-videos/{file_id}/details').json()['content_tags'] == []
    assert len(client.get('/api/personal-videos', params={'private_tag':first['id']}).json()) == 1
    for tag_id in [other.id, unused.id]:
        assert client.get('/api/personal-videos', params={'private_tag':str(tag_id)}).json() == []
    assert client.put(f'{path}/{other.id}').status_code == 404
    assert client.delete(f"{path}/{first['id']}").status_code == 204
    assert client.get(path).json() == []
    assert source.read_bytes() == b'original'


@pytest.mark.parametrize('bad', ['', ' '*3, 'x'*65, 'a\nb', '!!!'])
def test_private_tag_validation(client, video, bad):
    assert client.post(f'/api/personal-videos/assets/{video[2].id}/private-tags', json={'display_name':bad}).status_code == 422


def test_private_annotations_for_shared_video_do_not_broaden_access(client, video):
    vault, _, asset, _ = video
    foreign_owner = uuid4()
    shared = replace(asset, owner_user_id=foreign_owner, visibility='shared', shared_with_user_ids=(asset.owner_user_id,))
    vault.catalogued_assets[asset.vault_path] = shared
    path = f'/api/personal-videos/assets/{asset.id}/private-tags'
    response = client.post(path, json={'display_name':'My shared recording'})
    assert response.status_code == 201
    tag_id = response.json()['id']
    assert len(client.get('/api/personal-videos', params={'private_tag':tag_id}).json()) == 1
    # Recipient annotations do not authorize owner-only evidence or metadata edits.
    file_id = client.get('/api/personal-videos').json()[0]['id']
    assert client.get(f'/api/personal-videos/{file_id}/details').status_code == 404
    assert client.patch(f'/api/vault-master/assets/{asset.id}/metadata',json={'captured_on':'2024-01-01'}).status_code == 404
    private = client.app.dependency_overrides[get_gallery_custom_tag_store]()
    assert private.for_asset(foreign_owner, asset.id) == []
    vault.catalogued_assets[asset.vault_path] = replace(shared, visibility='private', shared_with_user_ids=())
    assert client.get(path).status_code == 404
    assert client.post(path,json={'display_name':'Denied'}).status_code == 404
    assert client.get('/api/personal-videos',params={'private_tag':tag_id}).json() == []


def test_global_read_and_decision_boundaries_exclude_legacy_custom_rows(client, video):
    _, _, asset, _ = video
    store = libraries.get_gallery_intelligence_store()
    store.create_custom_tag(asset.id, asset.owner_user_id, 'Legacy private')
    store.persist_canonical_assignments(asset.id, (('content_tag','beach'),), model_id='synthetic',model_revision=None,task_version='test')
    path = f'/api/personal-videos/intelligence/{asset.id}/tags'
    assert client.patch(path,json={'namespace':'content_tag','slug':'legacy-private','decision':'include'}).status_code == 422
    file_id = client.get('/api/personal-videos').json()[0]['id']
    assert [t['slug'] for t in client.get(f'/api/personal-videos/{file_id}/details').json()['content_tags']] == ['beach']
    assert len(client.get('/api/personal-videos?content_tag=beach').json()) == 1
    assert client.get('/api/personal-videos?content_tag=legacy-private').status_code == 422
    assert client.patch(path,json={'namespace':'content_tag','slug':'beach','decision':'exclude'}).json() == []
    assert client.get('/api/personal-videos?content_tag=beach').json() == []
    # Historical evidence remains intact, outside current global responses.
    assert any(t['slug']=='legacy-private' for t in store.list_terms())


def test_legacy_video_annotation_reconciliation_preserves_proven_uuid_and_skips_ambiguous(tag_database):
    conninfo, intelligence, private, owner, _, asset, _ = tag_database
    intelligence.create_custom_tag(asset, owner, 'Private recording')
    import psycopg
    with psycopg.connect(conninfo) as c:
        c.execute("INSERT INTO vault_metadata_terms(id,namespace,slug,display_name) VALUES(%s,'content_tag','ambiguous','Ambiguous')", (uuid4(),))
    report = reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)
    assert report['migrated_owners'] == 1 and report['blocked_records'] == 1
    tag = private.list(owner)[0]
    assert tag.display_name == 'Private recording'
    assert private.matching_asset_ids(owner,(tag.id,)) == {asset}
    assert effective_system_tags(intelligence, asset) == []
    assert not any(t['slug'] in ('private-recording','ambiguous') for t in system_terms())
    assert reconcile_legacy_tags(conninfo, apply=True, owner_user_id=owner)['migrated_owners'] == 0


def test_hidden_and_foreign_video_private_routes_fail_closed(client, video):
    vault, _, asset, _ = video
    path = f'/api/personal-videos/assets/{asset.id}/private-tags'
    tag = client.post(path,json={'display_name':'Owner only'}).json()
    vault.catalogued_assets[asset.vault_path] = replace(asset,lifecycle_state='hidden')
    assert client.get(path).status_code == 404
    assert client.post(path,json={'display_name':'Denied'}).status_code == 404
    assert client.put(f"{path}/{tag['id']}").status_code == 404
    assert client.delete(f"{path}/{tag['id']}").status_code == 404
    vault.catalogued_assets[asset.vault_path] = replace(asset,owner_user_id=uuid4())
    assert client.get(path).status_code == 404
    assert client.post(path,json={'display_name':'Denied'}).status_code == 404
