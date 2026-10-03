from types import SimpleNamespace
from uuid import uuid4
from dataclasses import replace
import pytest
from app import home_video_backfill as bulk, ken_service as lab
from tests.test_postgres_ken import pg_ken
from tests.test_ken_config import config, complete
from tests.test_postgres_vault_master import postgres_store, postgres_conninfo, _catalogued_asset
from app.auth_store import PostgresAuthenticationStore
from app.auth import AuthenticatedIdentity
from app.vault_master import PostgresVaultMasterStore


def test_bulk_survives_restart_owner_scope_capacity_and_retry(pg_ken):
    store,first,owner=pg_ken
    cfg=config()
    assets=[]
    with store.connect() as conn:
        for i in range(35):
            id=first if i==0 else uuid4()
            if i: conn.execute("INSERT INTO vault_assets(id,owner_user_id) VALUES(%s,%s)",(id,owner))
            assets.append(SimpleNamespace(id=id,owner_user_id=owner,asset_type='Home Videos',lifecycle_state='active',sha256='a'*64,vault_path=f'/vault/Home Videos/{id}.mp4'))
    foreign=SimpleNamespace(**{**vars(assets[0]),'owner_user_id':uuid4()})
    hidden=SimpleNamespace(**{**vars(assets[0]),'lifecycle_state':'hidden'})
    result=bulk.enqueue(store,[*assets,foreign,hidden],owner,cfg)
    assert result['pending']==35
    assert bulk.enqueue(store,assets,owner,cfg)['pending']==35
    store=lab.KenStore(store.conninfo)
    vault=SimpleNamespace(get_catalogued_asset_by_id=lambda id:next(a for a in assets if a.id==id))
    for _ in range(40): bulk.admit_next(store,vault,cfg)
    assert bulk.progress(store,owner,cfg)['queued']==32
    assert bulk.progress(store,owner,cfg)['pending']==3
    assert sum(bulk.progress(store,uuid4(),cfg).values())==0
    with store.connect() as conn:
        failed=conn.execute("SELECT id FROM vault_ken_runs LIMIT 1").fetchone()['id']
    store.update(failed,'failed',error='Synthetic failure')
    result=bulk.enqueue(store,assets,owner,cfg)
    assert result['pending']==3
    assert result['failed_existing_requires_attention']==1
    bulk.admit_next(store,vault,cfg)
    assert bulk.progress(store,owner,cfg)['queued']==32
    with store.connect() as conn:
        assert conn.execute('SELECT canonical FROM vault_assets WHERE id=%s',(first,)).fetchone()['canonical']['description']=='original'


def test_current_version_skipped_old_version_reprocessed_and_history_preserved(pg_ken):
    store,id,owner=pg_ken; cfg=config()
    asset=SimpleNamespace(id=id,owner_user_id=owner,asset_type='Home Videos',lifecycle_state='active',sha256='a'*64,vault_path='/vault/Home Videos/synthetic.mp4')
    run=store.queue_ken(id,owner,{**cfg,'source':{'verified_sha256':asset.sha256}})
    complete(store,run)
    result=bulk.enqueue(store,[asset],owner,cfg)
    assert result['already_current']==1 and result['pending']==0
    newer={**cfg,'prompt_version':'synthetic-next'}
    assert bulk.enqueue(store,[asset],owner,newer)['pending']==1
    bulk.admit_next(store,SimpleNamespace(get_catalogued_asset_by_id=lambda _:asset),newer)
    assert len(store.history(id,owner))==2
    assert store.get(run['id'])['status']=='completed'


def test_postgres_hidden_share_interlock_and_uuid_visibility(postgres_store,postgres_conninfo):
    auth=PostgresAuthenticationStore(postgres_conninfo)
    owner=auth.get_account('owner'); recipient=auth.get_account('son')
    asset=replace(_catalogued_asset(uuid4(),'/vault/Home Videos/synthetic.mp4','owner'),asset_type='Home Videos',mime_type='video/mp4')
    asset=postgres_store.restore_catalogued_asset(asset,'owner')
    # A pending share is a permission too: it cannot activate after Hide.
    postgres_store.update_catalogued_asset_access(asset.id,'shared',(recipient.username,),'owner',share_mode='standard')
    with pytest.raises(ValueError,match='This video is shared'):
        postgres_store.set_catalogued_asset_lifecycle_state(asset.id,owner.user_id,'owner','hidden')
    postgres_store.update_catalogued_asset_access(asset.id,'private',(),'owner')
    postgres_store.set_catalogued_asset_lifecycle_state(asset.id,owner.user_id,'owner','hidden')
    with pytest.raises(ValueError,match='Hidden videos'):
        postgres_store.update_catalogued_asset_access(asset.id,'shared',(recipient.username,),'owner')
    restarted=PostgresVaultMasterStore(postgres_conninfo)
    locked=AuthenticatedIdentity(owner)
    assert restarted.get_visible_catalogued_asset_by_id(asset.id,locked) is None
    unlocked=AuthenticatedIdentity(owner)
    unlocked.hidden_videos_authorized=True
    visible=restarted.get_visible_catalogued_asset_by_id(asset.id,unlocked)
    assert visible.id==asset.id and visible.owner_user_id==asset.owner_user_id and visible.sha256==asset.sha256


def test_collection_permissions_interlock_with_hidden_videos(postgres_store,postgres_conninfo):
    from app.share_grants import PostgresShareGrantStore
    auth=PostgresAuthenticationStore(postgres_conninfo)
    owner=auth.get_account('owner')
    asset=postgres_store.restore_catalogued_asset(replace(_catalogued_asset(uuid4(),'/vault/Home Videos/collection.mp4','owner'),asset_type='Home Videos'),'owner')
    grants=PostgresShareGrantStore(postgres_conninfo)
    collection=grants.create_collection(owner.user_id,'Synthetic videos',[asset.id])
    postgres_store.set_catalogued_asset_lifecycle_state(asset.id,owner.user_id,'owner','hidden')
    with pytest.raises(ValueError,match='Hidden videos'):
        grants.share_collection(collection.collection_id,owner.user_id,'local_all')
    postgres_store.set_catalogued_asset_lifecycle_state(asset.id,owner.user_id,'owner','active')
    grants.share_collection(collection.collection_id,owner.user_id,'local_all',share_mode='standard')
    with pytest.raises(ValueError,match='This video is shared'):
        postgres_store.set_catalogued_asset_lifecycle_state(asset.id,owner.user_id,'owner','hidden')
