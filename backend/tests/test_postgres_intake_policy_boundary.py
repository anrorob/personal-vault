from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from PIL import Image

from tests.test_postgres_vault_master import postgres_conninfo, postgres_store, _arrival_hall_owner_user_id, _catalogued_asset
from tests.test_intake_policy_boundary import grant_for
from app.vault_master import scan_root, INCOMING_SOURCE, PostgresVaultMasterStore
from app.vault_master_autopilot import PostgresAutopilotStore
from app.vault_master_ingestion_ai import MemoryIngestionAiStore
from app.arrival_photo_approval import queue_automatic_gallery_photo
from app.intake_recovery_authorization import initialize_recovery_authorizations, register_recovery_authorization, queue_recovery_photo
from app.gallery_florence import PostgresGalleryFlorenceStore


def camera(postgres_store,postgres_conninfo,tmp_path):
    arrival=tmp_path/'arrival';arrival.mkdir();gallery=tmp_path/'gallery';gallery.mkdir()
    Image.new('RGB',(16,16),'blue').save(arrival/'example.jpg')
    owner=_arrival_hall_owner_user_id(postgres_conninfo)
    scan_root(postgres_store,arrival,INCOMING_SOURCE,owner_lookup=lambda _:owner)
    item=postgres_store.list_items()[0]
    metadata={**item.metadata,'camera_make':'Example','camera_model':'Synthetic','exif_original_at':'2026:01:01 12:00:00'}
    with psycopg.connect(postgres_conninfo) as c:
        c.execute('UPDATE vault_master_items SET metadata=%s WHERE id=%s',(Jsonb(metadata),item.id))
    return postgres_store.get_item(item.id),arrival,gallery


def test_atomic_gallery_approval_checks_persisted_policy(postgres_store,postgres_conninfo,tmp_path):
    item,arrival,gallery=camera(postgres_store,postgres_conninfo,tmp_path)
    policies=PostgresAutopilotStore(postgres_conninfo)
    policy=policies.upsert_policy(item.owner_user_id,'owner','personal_photo','Gallery',80,50,2,5)
    enabled=policies.set_policy_status(policy.id,item.owner_user_id,'enabled')
    policies.set_policy_status(policy.id,item.owner_user_id,'disabled')
    assert queue_automatic_gallery_photo(postgres_store,item,enabled,'camera-photo-v1','worker') is None
    enabled=policies.set_policy_status(policy.id,item.owner_user_id,'enabled')
    assert queue_automatic_gallery_photo(postgres_store,item,replace(enabled,id=uuid4()),'camera-photo-v1','worker') is None
    assert postgres_store.get_item(item.id).state=='needs_review'
    assert queue_automatic_gallery_photo(postgres_store,item,enabled,'camera-photo-v1','worker') is None
    assert postgres_store.get_item(item.id).state=='needs_review'


def test_recovery_authorization_survives_restart_and_consumes_once(postgres_store,postgres_conninfo,tmp_path):
    item,arrival,gallery=camera(postgres_store,postgres_conninfo,tmp_path)
    with psycopg.connect(postgres_conninfo) as c:
        initialize_recovery_authorizations(c)
    now=datetime.now(timezone.utc);manifest=grant_for(item,now)
    grant=register_recovery_authorization(postgres_store,manifest,now=now)
    restarted=PostgresVaultMasterStore(postgres_conninfo)
    ai=MemoryIngestionAiStore()
    assert queue_recovery_photo(restarted,grant,item.id,arrival,{'Gallery':gallery},ai,now=now+timedelta(hours=2)) is None
    assert queue_recovery_photo(restarted,grant,uuid4(),arrival,{'Gallery':gallery},ai,now=now) is None
    assert queue_recovery_photo(restarted,grant,item.id,arrival,{'Gallery':gallery},ai,now=now)
    assert queue_recovery_photo(postgres_store,grant,item.id,arrival,{'Gallery':gallery},ai,now=now) is None
    with psycopg.connect(postgres_conninfo) as c:
        assert c.execute('SELECT status,consumed FROM vault_intake_recovery_authorizations WHERE id=%s',(grant,)).fetchone()==('completed',[str(item.id)])
        assert c.execute('SELECT count(*) FROM vault_autopilot_policies').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM vault_master_decisions WHERE item_id=%s",(item.id,)).fetchone()[0]==1
        c.execute('DELETE FROM vault_intake_recovery_authorizations WHERE id=%s',(grant,))


def test_florence_queue_is_owner_bound_concurrent_and_current_success_idempotent(postgres_store,postgres_conninfo):
    owner=_arrival_hall_owner_user_id(postgres_conninfo)
    asset=postgres_store.restore_catalogued_asset(replace(_catalogued_asset(uuid4(),'/vault/Gallery/example.jpg','owner'),owner_user_id=owner),'owner')
    store=PostgresGalleryFlorenceStore(postgres_conninfo);store.initialize()
    def queue(_): return PostgresGalleryFlorenceStore(postgres_conninfo).queue(asset.id,owner,'owner')
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs=list(executor.map(queue,range(4)))
    assert len({job.id for job in jobs})==1
    import pytest
    with pytest.raises(ValueError): store.queue(asset.id,uuid4(),'owner')
    job=store.claim_next_job();assert job and job.asset_id==asset.id
    store.fail(job.id,'Synthetic transient failure')
    retry=store.queue(asset.id,owner,'owner');assert retry.id!=job.id
    job=store.claim_next_job();store.complete(job,'Synthetic current caption','',1)
    assert store.queue(asset.id,owner,'owner') is None
    with psycopg.connect(postgres_conninfo) as c:
        assert c.execute('SELECT count(*) FROM vault_gallery_florence_jobs WHERE asset_id=%s',(asset.id,)).fetchone()[0]==2
