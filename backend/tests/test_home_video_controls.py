from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.auth import get_passkey_store
from app.passkeys import PasskeyCredential
from app.gallery_custom_tags import get_gallery_custom_tag_store
from app.gallery_people import get_gallery_people_store, MemoryGalleryPeopleStore
from app.vault_master_api import get_share_grant_store
from tests.test_vault_libraries import authenticate, catalogue_file, configure_libraries

@pytest.fixture
def videos(client, tmp_path):
    root, _, _, store = configure_libraries(tmp_path)
    people = MemoryGalleryPeopleStore()
    client.app.dependency_overrides[get_gallery_people_store] = lambda: people
    assets = []
    for name, day in [('one.mp4',date(2024,1,1)), ('two.mp4',date(2025,2,2))]:
        path = root / name
        path.write_bytes(b'synthetic-video')
        asset = catalogue_file(store,root,path,asset_type='Home Videos',vault_root='/vault/Home Videos',mime_type='video/mp4')
        asset = replace(asset,captured_on=day)
        store.catalogued_assets[asset.vault_path] = asset
        assets.append(asset)
    authenticate(client)
    return store, assets, people


def test_hidden_video_session_protects_every_delivery_path(client,videos,authentication_store):
    store, assets, _ = videos
    listing = client.get('/api/personal-videos').json()
    item = next(x for x in listing if x['asset_id']==str(assets[0].id))
    asset = assets[0]
    store.set_catalogued_asset_lifecycle_state(asset.id,asset.owner_user_id,asset.owner_username,'hidden')
    assert len(client.get('/api/personal-videos').json()) == 1
    assert client.get('/api/personal-videos?include_hidden=true').status_code == 403
    for suffix in ['content','details','thumbnail','playback','playback/content']:
        assert client.get(f"/api/personal-videos/{item['id']}/{suffix}").status_code in (403,404)
    for suffix in ['history','sharing']:
        assert client.get(f'/api/vault-master/assets/{asset.id}/{suffix}').status_code in (403,404)
    token = client.cookies.get('pv_session')
    assert not authentication_store.authorize_hidden_videos_session(token,uuid4())
    assert authentication_store.authorize_hidden_photos_session(token,asset.owner_user_id)
    assert client.get('/api/personal-videos?include_hidden=true').status_code == 403
    assert authentication_store.authorize_hidden_videos_session(token,asset.owner_user_id)
    for _ in range(2):
        assert client.get('/api/auth/hidden-videos/authorization').json()=={'authorized':True}
        assert [x['asset_id'] for x in client.get('/api/personal-videos?include_hidden=true').json()]==[str(asset.id)]
        assert len(client.get('/api/personal-videos').json())==1
    response=client.get(f"/api/personal-videos/{item['id']}/content")
    assert response.status_code==200 and response.content==b'synthetic-video'
    assert client.put(f'/api/vault-master/assets/{asset.id}/sharing',json={'mode':'everyone','recipient_user_ids':[]}).status_code==409
    client.post('/api/auth/logout'); authenticate(client)
    assert client.get('/api/auth/hidden-videos/authorization').json()=={'authorized':False}
    assert client.get(f"/api/personal-videos/{item['id']}/content").status_code in (403,404)


@pytest.mark.parametrize('shared', [True,False,None])
def test_hide_confirms_share_state_and_preserves_bytes(client,videos,shared):
    store,assets,_=videos
    def check(*args):
        if shared is None: raise RuntimeError('synthetic unavailable')
        return shared
    client.app.dependency_overrides[get_share_grant_store]=lambda:SimpleNamespace(has_active_share_for_asset=check)
    asset=assets[0]
    response=client.post(f'/api/vault-master/assets/{asset.id}/lifecycle/hide')
    assert response.status_code == (503 if shared is None else 409 if shared else 200)
    saved=store.get_catalogued_asset_by_id(asset.id)
    assert saved.sha256==asset.sha256 and saved.owner_user_id==asset.owner_user_id
    assert saved.vault_path==asset.vault_path and saved.metadata_provenance==asset.metadata_provenance
    assert saved.lifecycle_state==('hidden' if shared is False else 'active')
    if shared: assert response.json()['detail']=='This video is shared. Unshare it before hiding.'
    assert len(client.get('/api/personal-videos').json())==(1 if shared is False else 2)


def test_filters_use_private_uuid_people_effective_date_location(client,videos):
    _,assets,people=videos
    owner=assets[0].owner_user_id
    tags=client.app.dependency_overrides[get_gallery_custom_tag_store]()
    tag=tags.create(owner,'Trip'); unused=tags.create(owner,'Unused'); foreign=tags.create(uuid4(),'Trip')
    tags.assign(owner,tag.id,assets[0].id)
    person=people.create_person(assets[0].owner_username,'Synthetic person',owner_user_id=owner)
    people.associate(assets[0].id,person.id,'manual')
    choices=client.get('/api/personal-videos/filter-options')
    assert choices.status_code==200
    assert {x['id'] for x in choices.json()['private_tags']}=={str(tag.id),str(unused.id)}
    assert choices.json()['people']==[{'id':str(person.id),'display_name':'Synthetic person'}]
    assert set(choices.json())=={'people','private_tags','content_tags','locations'}
    for query in [f'private_tag={tag.id}',f'person={person.id}','date_to=2024-12-31','date_from=2024-01-01&date_to=2024-01-01&location=gdansk']:
        assert [x['asset_id'] for x in client.get('/api/personal-videos?'+query).json()]==[str(assets[0].id)]
    for query in [f'private_tag={unused.id}',f'private_tag={foreign.id}',f'person={uuid4()}','location=absent']:
        assert client.get('/api/personal-videos?'+query).json()==[]
    assert client.get('/api/personal-videos?date_from=2025-01-01&date_to=2024-01-01').status_code==422
    assert [x['asset_id'] for x in client.get('/api/personal-videos?sort=oldest').json()]==[str(x.id) for x in assets]


def test_passkey_unlock_is_user_verified_purpose_bound_and_one_time(client,videos,authentication_store,monkeypatch):
    import app.auth as auth
    owner=videos[1][0].owner_user_id
    keys=client.app.dependency_overrides[get_passkey_store]()
    keys.create_credential(PasskeyCredential(uuid4(),owner,b'credential-auth',b'public-key',0,(),'platform','Synthetic',datetime.now(timezone.utc),None))
    def verify(**kwargs):
        assert kwargs['require_user_verification'] is True
        return SimpleNamespace(new_sign_count=1)
    monkeypatch.setattr(auth,'verify_authentication_response',verify)
    challenge=client.post('/api/auth/hidden-videos/authorization/options').json()
    body={'challenge_id':challenge['challenge_id'],'credential':{'id':'Y3JlZGVudGlhbC1hdXRo','rawId':'Y3JlZGVudGlhbC1hdXRo','response':{}}}
    assert client.post('/api/auth/hidden-photos/authorization/verify',json=body).status_code==400
    assert client.post('/api/auth/hidden-videos/authorization/verify',json=body).status_code==200
    assert client.post('/api/auth/hidden-videos/authorization/verify',json=body).status_code==400
    assert client.get('/api/auth/hidden-photos/authorization').json()=={'authorized':False}
    assert client.get('/api/auth/hidden-videos/authorization').json()=={'authorized':True}


def test_bulk_api_passes_only_current_owner_eligible_assets_to_existing_ken(ken_api_fixture,monkeypatch):
    from app import home_video_backfill, ken_api
    client,store,vault,asset,source=ken_api_fixture
    for changes in [{'id':uuid4(),'owner_user_id':uuid4(),'vault_path':'/vault/Home Videos/foreign.mp4'}, {'id':uuid4(),'lifecycle_state':'hidden','vault_path':'/vault/Home Videos/hidden.mp4'}, {'id':uuid4(),'asset_type':'Movies','vault_path':'/vault/Theatre/movie.mp4'}]:
        other=replace(asset,**changes); vault.catalogued_assets[other.vault_path]=other
    def enqueue(s,assets,owner,cfg):
        assert s is store and [x.id for x in assets]==[asset.id] and owner==asset.owner_user_id
        assert cfg['metadata_destination']=='home-video-ken-v1'
        return {'pending':1,'queued':0,'processing':0,'completed':0,'failed':0,'skipped':0,'already_current':0}
    monkeypatch.setattr(home_video_backfill,'enqueue',enqueue)
    authenticate(client)
    assert client.post('/api/personal-videos/ken/backfill').status_code==202
    assert source.read_bytes()==b'original video'
    monkeypatch.setattr(ken_api.ADAPTER,'health',lambda:{'status':'unavailable'})
    assert client.post('/api/personal-videos/ken/backfill').status_code==503
from tests.test_ken_service import ken_api_fixture


@pytest.mark.parametrize('route', ['origin-content','origin-download','origin-preview'])
def test_federated_direct_delivery_never_exposes_hidden_video(client,videos,monkeypatch,route):
    import app.vault_master_api as api
    store,assets,_=videos
    asset=assets[0]
    store.set_catalogued_asset_lifecycle_state(asset.id,asset.owner_user_id,asset.owner_username,'hidden')
    monkeypatch.setattr(api,'get_database_conninfo',lambda:'synthetic')
    monkeypatch.setattr(api,'FederationStore',lambda _:SimpleNamespace(authorizes_origin_content=lambda *a:True,authorizes_origin_download=lambda *a:True,authorizes_origin_preview=lambda *a:True))
    client.app.dependency_overrides[api.get_catalogue_preview_roots]=lambda:{}
    response=client.get(f'/api/vault-master/federation/{route}/{uuid4()}/{asset.id}',headers={'x-pv-federation-signature':'synthetic','x-pv-federation-timestamp':'1','x-pv-requester-vault':str(uuid4())})
    assert response.status_code==404
