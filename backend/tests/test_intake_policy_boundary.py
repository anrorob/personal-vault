from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.test_bulk_intake import camera_case
from app.arrival_photo_approval import queue_automatic_gallery_photo
from app.vault_master_autopilot import MemoryAutopilotStore, process_autopilot_batch
from app.vault_master_ingestion_ai import MemoryIngestionAiStore
from app.intake_recovery_authorization import register_recovery_authorization, queue_recovery_photo
from app.gallery_publication import queue_gallery_publication, queue_missing_gallery_florence
from app.gallery_florence import process_next_gallery_florence_job, GALLERY_FLORENCE_TASK_VERSION
from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION


def grant_for(item, now):
    return dict(schema='personal-vault.intake-recovery-authorization.v1',id=str(uuid4()),
                owner_user_id=str(item.owner_user_id),requested_by='synthetic operator approval',
                issued_at=now.isoformat(),expires_at=(now+timedelta(hours=1)).isoformat(),
                items=[dict(item_id=str(item.id),sha256=item.sha256,size_bytes=item.size_bytes)])


@pytest.mark.parametrize('status', ['absent','disabled','paused','other_owner','enabled'])
def test_fresh_upload_requires_current_matching_owner_policy(tmp_path,status):
    store, policies, policy, item, arrival, gallery, source = camera_case(tmp_path)
    if status == 'absent':
        policies = MemoryAutopilotStore()
    elif status == 'other_owner':
        store.items[item.source_path] = replace(item,owner_user_id=uuid4())
    elif status != 'enabled':
        policies.set_policy_status(policy.id,item.owner_user_id,status)
    assert process_autopilot_batch(policies,MemoryIngestionAiStore(),store,arrival,{'Gallery':gallery}) is None
    assert queue_automatic_gallery_photo(store,item,policy,'camera-photo-v1','worker',policy_store=policies) is None
    assert store.get_item(item.id).state == 'needs_review' and source.exists()
    assert not store.catalogued_assets


@pytest.mark.parametrize('score',[None,65,79,80,100])
def test_untrusted_numeric_metadata_never_substitutes_for_score_authority(tmp_path,score):
    store, policies, policy, item, arrival, gallery, source = camera_case(tmp_path)
    item=replace(item,metadata={**item.metadata,'deterministic_score':score,'decision_score':score})
    store.items[item.source_path]=item
    assert process_autopilot_batch(policies,MemoryIngestionAiStore(),store,arrival,{'Gallery':gallery}) is None
    assert queue_automatic_gallery_photo(store,item,policy,'claimed-deterministic-100','worker',policy_store=policies) is None
    assert store.get_item(item.id).state=='needs_review' and source.exists()


def test_explicit_recovery_is_single_use_and_never_authorizes_fresh_intake(tmp_path):
    store, _, _, item, arrival, gallery, _ = camera_case(tmp_path)
    policies=MemoryAutopilotStore(); ai=MemoryIngestionAiStore(); now=datetime.now(timezone.utc)
    manifest=grant_for(item,now)
    grant=register_recovery_authorization(store,manifest,now=now)
    fresh=replace(item,id=uuid4(),source_path=str(arrival/'fresh.jpg'),relative_path='fresh.jpg',sha256='a'*64)
    store.items[fresh.source_path]=fresh
    assert queue_recovery_photo(store,grant,fresh.id,arrival,{'Gallery':gallery},ai,now=now) is None
    assert process_autopilot_batch(policies,ai,store,arrival,{'Gallery':gallery}) is None
    assert queue_recovery_photo(store,grant,item.id,arrival,{'Gallery':gallery},ai,now=now)
    assert store._recovery_authorizations[grant]['status']=='completed'
    assert queue_recovery_photo(store,grant,item.id,arrival,{'Gallery':gallery},ai,now=now) is None
    assert queue_recovery_photo(store,grant,fresh.id,arrival,{'Gallery':gallery},ai,now=now) is None
    assert process_autopilot_batch(policies,ai,store,arrival,{'Gallery':gallery}) is None
    assert store.get_item(fresh.id).state=='needs_review' and policies.list_policies()==[]
    assert register_recovery_authorization(store,manifest,now=now)==grant
    assert store._recovery_authorizations[grant]['status']=='completed'


def test_expired_or_changed_recovery_cannot_queue(tmp_path):
    store, _, _, item, arrival, gallery, _ = camera_case(tmp_path)
    now=datetime.now(timezone.utc); manifest=grant_for(item,now)
    grant=register_recovery_authorization(store,manifest,now=now)
    ai=MemoryIngestionAiStore()
    assert queue_recovery_photo(store,grant,item.id,arrival,{'Gallery':gallery},ai,now=now+timedelta(hours=2)) is None
    store.items[item.source_path]=replace(item,size_bytes=item.size_bytes+1)
    assert queue_recovery_photo(store,grant,item.id,arrival,{'Gallery':gallery},ai,now=now) is None
    with pytest.raises(ValueError): register_recovery_authorization(store,manifest,now=now)


class Florence:
    def __init__(self): self.evidence=None; self.job=None; self.queued=0
    def latest_evidence(self,*args): return self.evidence
    def active_or_latest_job(self,*args): return self.job
    def queue(self,asset_id,owner,username):
        self.queued+=1
        self.job=SimpleNamespace(id=uuid4(),asset_id=asset_id,owner_user_id=owner,status='queued')
        return self.job
    def claim_next_job(self): return self.job if self.job and self.job.status=='queued' else None
    def fail(self,job_id,error): self.job.status='failed'


@pytest.mark.parametrize('publication', ['manual','held_autopilot_then_manual','recovery'])
def test_all_publication_authorities_queue_same_async_intelligence(tmp_path,publication):
    from app.vault_master import process_next_move
    from app.gallery_intelligence import MemoryGalleryIntelligenceStore
    store, policies, _, item, arrival, gallery, _ = camera_case(tmp_path)
    ai=MemoryIngestionAiStore()
    if publication=='manual':
        store.record_decision(item.id,'approved','owner'); store.queue_move(item.id,'owner')
    elif publication=='held_autopilot_then_manual':
        assert process_autopilot_batch(policies,ai,store,arrival,{'Gallery':gallery}) is None
        assert store.get_item(item.id).state=='needs_review'
        store.record_decision(item.id,'approved','owner'); store.queue_move(item.id,'owner')
    else:
        now=datetime.now(timezone.utc); grant=register_recovery_authorization(store,grant_for(item,now),now=now)
        queue_recovery_photo(store,grant,item.id,arrival,{'Gallery':gallery},ai,now=now)
    request=uuid4()
    process_next_move(store,arrival,{'Gallery':gallery},theatre_queue=lambda _:request)
    receipt=dict(request_id=str(request),item_id=str(item.id),owner_user_id=str(item.owner_user_id),
                 logical_destination=item.proposed_destination,logical_area='Gallery',slot_id='PV-DISK-002',
                 relative_path=item.proposed_destination.removeprefix('/vault/'),expected_sha256=item.sha256,
                 expected_size_bytes=item.size_bytes)
    asset=store.publish_arrival_managed_receipt(item.id,receipt)
    florence=Florence(); gi=MemoryGalleryIntelligenceStore()
    assert queue_gallery_publication(store,gi,ai,florence,asset,'worker')
    assert florence.queued==1 and len(gi.jobs)==1
    assert queue_gallery_publication(store,gi,ai,florence,asset,'worker')
    assert florence.queued==1 and len(gi.jobs)==1
    # A missing canonical test file simulates a provider/source failure.
    process_next_gallery_florence_job(florence,store)
    assert florence.job.status=='failed'
    assert store.get_item(item.id).state=='moved' and store.get_catalogued_asset_by_id(asset.id)
    assert queue_missing_gallery_florence(store,ai,florence,asset,'owner')
    assert florence.queued==2
    florence.job.status='completed'
    florence.evidence=SimpleNamespace(caption='Synthetic caption',model_id=AI_MODEL_ID,
        model_revision=AI_MODEL_REVISION,task_version=GALLERY_FLORENCE_TASK_VERSION)
    assert not queue_missing_gallery_florence(store,ai,florence,asset,'owner')
    assert florence.queued==2
    florence.evidence.task_version='old-version'
    assert queue_missing_gallery_florence(store,ai,florence,asset,'owner')


def test_busy_and_failed_model_queues_cannot_starve_other_stages(monkeypatch):
    import asyncio
    from app import main, gallery_florence
    calls=[]
    dummy=SimpleNamespace(global_enabled=lambda:True)
    for name in ('get_vault_master_store','get_intake_store','get_gallery_intelligence_store',
                 'get_ingestion_ai_store','get_ai_store','get_video_intelligence_store','get_gallery_people_store'):
        monkeypatch.setattr(main,name,lambda:dummy)
    monkeypatch.setattr(main,'get_admin_username',lambda:'synthetic worker')
    monkeypatch.setenv('PV_VAULT_MASTER_AI_ENABLED','true')
    monkeypatch.setattr(gallery_florence,'get_gallery_florence_store',lambda:dummy)
    def failed(*args):
        calls.append('florence');raise ValueError('Synthetic provider failure')
    monkeypatch.setattr(gallery_florence,'process_next_gallery_florence_job',failed)
    for name,label in [('process_next_gallery_intelligence_job','gallery'),('process_next_ai_job','ai'),
                       ('queue_pending_ingestion_image_analysis','queue_ingestion'),
                       ('process_next_ingestion_ai_job','ingestion'),('process_next_video_analysis_job','video'),
                       ('reconcile_video_analysis_job','video_reconciliation')]:
        monkeypatch.setattr(main,name,lambda *args,label=label:calls.append(label) or uuid4())
    async def stop(_): raise asyncio.CancelledError()
    monkeypatch.setattr(main.asyncio,'sleep',stop)
    with pytest.raises(asyncio.CancelledError): asyncio.run(main.run_vault_master_intelligence_worker())
    assert calls==['florence','gallery','ai','queue_ingestion','ingestion','video','video_reconciliation']


def test_scoped_backfill_validates_every_identity_before_queuing_and_is_idempotent(tmp_path):
    from app.gallery_scoped_backfill import scoped_florence_backfill
    from tests.test_gallery_intelligence import gallery_asset
    vault,asset=gallery_asset(tmp_path)
    row=dict(asset_id=str(asset.id),owner_user_id=str(asset.owner_user_id),sha256=asset.sha256,size_bytes=asset.size_bytes)
    manifest=dict(schema='personal-vault.gallery-intelligence-backfill.v1',assets=[row])
    florence=Florence();ai=MemoryIngestionAiStore()
    bad={**manifest,'assets':[row,{**row,'asset_id':str(uuid4())}]}
    with pytest.raises(ValueError): scoped_florence_backfill(vault,ai,florence,bad,queue=True)
    assert florence.queued==0
    assert scoped_florence_backfill(vault,ai,florence,manifest)['missing_or_outdated']==1
    assert florence.queued==0
    assert scoped_florence_backfill(vault,ai,florence,manifest,queue=True)['queued']==1
    assert scoped_florence_backfill(vault,ai,florence,manifest,queue=True)['queued']==0
    assert florence.queued==1 and vault.get_catalogued_asset_by_id(asset.id)==asset


def test_current_canonical_evidence_replaces_outdated_retained_evidence(tmp_path):
    from tests.test_gallery_intelligence import gallery_asset,retained_florence_description
    from app.gallery_reconciliation import latest_retained_florence_visual_evidence
    vault,asset=gallery_asset(tmp_path)
    ingestion=retained_florence_description(vault,asset,'Synthetic old caption')
    florence=Florence()
    florence.evidence=SimpleNamespace(id=uuid4(),caption='Synthetic current caption',model_id=AI_MODEL_ID,
        model_revision=AI_MODEL_REVISION,task_version=GALLERY_FLORENCE_TASK_VERSION,created_at=datetime.now(timezone.utc))
    selected=latest_retained_florence_visual_evidence(vault,ingestion,asset,florence)
    assert selected.id==florence.evidence.id
    assert not queue_missing_gallery_florence(vault,ingestion,florence,asset,'owner')
