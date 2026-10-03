from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app import home_video_backfill as bulk, ken_service as ken
from app.ken_failure import failure_info
from tests.test_postgres_ken import pg_ken
from tests.test_ken_config import config, complete
from tests.test_ken_service import ken_api_fixture


def asset_fixture(pg_ken):
    store, asset_id, owner = pg_ken
    asset = SimpleNamespace(id=asset_id, owner_user_id=owner, asset_type='Home Videos',
        lifecycle_state='active', sha256='a'*64, vault_path='/vault/Home Videos/synthetic.mp4')
    return store, asset, config(), SimpleNamespace(get_catalogued_asset_by_id=lambda _:asset)


@pytest.mark.parametrize('state', ['queued','preparing_input','analysing','saving_result'])
def test_bulk_never_resets_or_duplicates_active_run(pg_ken,state):
    store,asset,cfg,vault=asset_fixture(pg_ken)
    run=store.queue_ken(asset.id,asset.owner_user_id,cfg)
    store.update(run['id'],state)
    for _ in range(2):
        result=bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
        assert result['skipped_active']==1 and result['queued_new']==0
        bulk.admit_next(store,vault,cfg)
    assert len(store.history(asset.id,asset.owner_user_id))==1
    assert store.get(run['id'])['status']==state
    assert result['queued' if state=='queued' else 'processing']==1


def test_completed_outside_bulk_is_current_and_counted_without_requeue(pg_ken):
    store,asset,cfg,vault=asset_fixture(pg_ken)
    run=store.queue_ken(asset.id,asset.owner_user_id,{**cfg,'source':{'verified_sha256':asset.sha256}})
    complete(store,run)
    assert bulk.progress(store,asset.owner_user_id,cfg)['completed']==1
    for _ in range(3):
        result=bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
        assert result['already_current']==1 and result['queued_new']==0
        bulk.admit_next(store,vault,cfg)
    assert len(store.history(asset.id,asset.owner_user_id))==1


def test_current_completion_between_enqueue_and_dispatch_is_not_lost(pg_ken):
    store,asset,cfg,vault=asset_fixture(pg_ken)
    bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
    run=store.queue_ken(asset.id,asset.owner_user_id,{**cfg,'source':{'verified_sha256':asset.sha256}})
    complete(store,run)
    bulk.admit_next(store,vault,cfg)
    assert bulk.progress(store,asset.owner_user_id,cfg)['completed']==1
    with store.connect() as conn:
        saved=conn.execute('SELECT status,run_id FROM vault_home_video_backfill').fetchone()
    assert saved=={'status':'dispatched','run_id':run['id']}
    assert len(store.history(asset.id,asset.owner_user_id))==1


def test_concurrent_bulk_clicks_admit_one_missing_run_and_preserve_manual_state(pg_ken):
    store,asset,cfg,vault=asset_fixture(pg_ken)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes=list(pool.map(lambda _:bulk.enqueue(store,[asset],asset.owner_user_id,cfg),range(8)))
    assert sum(x['queued_new'] for x in outcomes)==1
    for _ in range(3): bulk.admit_next(store,vault,cfg)
    assert len(store.history(asset.id,asset.owner_user_id))==1
    with store.connect() as conn:
        assert conn.execute('SELECT canonical FROM vault_assets WHERE id=%s',(asset.id,)).fetchone()['canonical']=={
            'description':'original','people':['existing'],'tags':['original'],'routing':'unchanged'}


def test_older_active_version_finishes_before_one_replacement(pg_ken):
    store,asset,cfg,vault=asset_fixture(pg_ken)
    old=store.queue_ken(asset.id,asset.owner_user_id,{**cfg,'prompt_version':'older'})
    bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
    bulk.admit_next(store,vault,cfg)
    assert len(store.history(asset.id,asset.owner_user_id))==1
    complete(store,old)
    for _ in range(2):
        bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
        bulk.admit_next(store,vault,cfg)
    assert len(store.history(asset.id,asset.owner_user_id))==2


def test_failed_retry_is_explicit_bounded_concurrent_and_preserves_history(pg_ken):
    store,asset,cfg,vault=asset_fixture(pg_ken)
    failed=store.queue_ken(asset.id,asset.owner_user_id,cfg)
    store.claim(); store.recover()
    before=store.get(failed['id'])
    assert before['configuration']['failure_diagnostic']['retryable'] is True
    for _ in range(2):
        result=bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
        assert result['failed_existing_requires_attention']==1 and result['queued_new']==0
        bulk.admit_next(store,vault,cfg)
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries=list(pool.map(lambda _:bulk.retry_failed(store,asset,failed['id'],cfg),range(6)))
    assert len({r['id'] for r in retries})==1
    retry=retries[0]
    assert retry['configuration']['recovery_retry_of']==str(failed['id'])
    assert store.get(failed['id'])==before
    store.update(retry['id'],'failed',error='Interrupted by worker restart')
    assert not failure_info(store.get(retry['id']))['retry_allowed']
    with pytest.raises(ValueError,match='technical review'):
        bulk.retry_failed(store,asset,retry['id'],cfg)
    assert bulk.retry_failed(store,asset,failed['id'],cfg)['id']==retry['id']
    assert bulk.enqueue(store,[asset],asset.owner_user_id,cfg)['queued_new']==0
    assert len(store.history(asset.id,asset.owner_user_id))==2


def test_unknown_failure_fails_closed_and_status_api_has_safe_reason(pg_ken):
    from app.ken_api import public_run
    store,asset,cfg,vault=asset_fixture(pg_ken)
    run=store.queue_ken(asset.id,asset.owner_user_id,cfg)
    store.update(run['id'],'failed',error='Synthetic private exception detail')
    result=bulk.enqueue(store,[asset],asset.owner_user_id,cfg)
    assert result['failed']==1 and result['failed_existing_requires_attention']==1
    info=public_run(store.get(run['id']))['failure']
    assert info['retryable'] is False and info['failed_at'] is not None
    assert 'private exception' not in info['message']
    with pytest.raises(ValueError): bulk.retry_failed(store,asset,run['id'],cfg)
    foreign=SimpleNamespace(**{**vars(asset),'owner_user_id':uuid4()})
    with pytest.raises(ValueError): bulk.retry_failed(store,foreign,run['id'],cfg)


@pytest.mark.parametrize('error,diagnostic,retryable',[
    ('Inference timeout',{},True),
    ('Model execution failed',{'service':{'http_status':503}},True),
    ('Model execution failed',{'service':{'http_status':500}},False),
    ('Published Home Video is unavailable',{},False),
    ('Synthetic',{'stage':'input_preparation'},False),
    ('Synthetic',{'stage':'result_integrity'},False),
    ('Synthetic',{'stage':'result_persistence','exception_type':'OperationalError'},True),
    ('Synthetic',{'stage':'result_persistence','exception_type':'CheckViolation'},False),
])
def test_failure_classification_is_conservative(error,diagnostic,retryable):
    info=failure_info({'error':error,'configuration':{'failure_diagnostic':diagnostic}})
    assert info['retryable'] is retryable


def test_real_retry_route_uses_postgres_admission_and_enforces_owner(ken_api_fixture,pg_ken):
    from tests.test_vault_libraries import authenticate
    from dataclasses import replace
    client,_,vault,asset,source=ken_api_fixture
    store,_,_=pg_ken
    client.app.dependency_overrides[ken.get_ken_store]=lambda:store
    with store.connect() as conn:
        conn.execute('INSERT INTO vault_assets(id,owner_user_id) VALUES(%s,%s)',(asset.id,asset.owner_user_id))
    failed=store.queue_ken(asset.id,asset.owner_user_id,ken.configuration())
    store.claim(); store.recover()
    route=f'/api/personal-videos/ken/assets/{asset.id}/runs/{failed["id"]}/retry'
    assert client.post(route).status_code==401
    authenticate(client)
    history=client.get(f'/api/personal-videos/ken/assets/{asset.id}/runs').json()
    assert history[0]['failure']['code']=='worker_interrupted'
    first=client.post(route)
    assert first.status_code==202,first.text
    assert client.post(route).json()['id']==first.json()['id']
    assert len(store.history(asset.id,asset.owner_user_id))==2
    vault.catalogued_assets[asset.vault_path]=replace(asset,owner_user_id=uuid4())
    assert client.post(route).status_code==404
    assert source.read_bytes()==b'original video'
