from dataclasses import replace
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from PIL import Image

from app import arrival_incremental_scan as incremental
from app.vault_master import MemoryVaultMasterStore, scan_root, INCOMING_SOURCE, process_next_move
from app.vault_master_autopilot import MemoryAutopilotStore, process_autopilot_batch, camera_photo_eligible, camera_photo_safety_eligible
from app.vault_master_ingestion_ai import MemoryIngestionAiStore


def test_two_thousand_files_are_bounded_and_current_scans_create_no_batches(tmp_path):
    for n in range(2000):
        (tmp_path / f"image-{n:04}.txt").write_text(str(n))
    store = MemoryVaultMasterStore()
    for _ in range(40):
        assert incremental.scan_arrival_incrementally(store, tmp_path, limit=50)
    assert len(store.list_items()) == 2000
    assert len(store.batches) == 40
    assert incremental.scan_arrival_incrementally(store, tmp_path) is None
    assert len(store.batches) == 40
    ids = {x.id for x in store.list_items()}
    (tmp_path / "image-0000.txt").write_text("changed")
    assert incremental.scan_arrival_incrementally(store, tmp_path)
    assert {x.id for x in store.list_items()} == ids
    assert incremental.scan_arrival_incrementally(store, tmp_path) is None
    assert incremental.scan_arrival_incrementally(store, tmp_path, version="next-version", limit=50)
    before = len(store.batches)
    scan_root(store, tmp_path, INCOMING_SOURCE)  # explicit rescan still runs
    assert len(store.batches) == before + 1


def test_failed_scan_backs_off_and_does_not_starve_later_files(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    store = MemoryVaultMasterStore()
    original = incremental.scan_file
    def scan(path, *args, **kwargs):
        if path.name == "a.txt":
            raise ValueError("synthetic bad file")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(incremental, "scan_file", scan)
    now = datetime.now(timezone.utc)
    incremental.scan_arrival_incrementally(store, tmp_path, limit=1, now=now)
    incremental.scan_arrival_incrementally(store, tmp_path, limit=1, now=now)
    assert [x.filename for x in store.list_items()] == ["b.txt"]
    assert incremental.scan_arrival_incrementally(store, tmp_path, now=now) is None
    assert incremental.scan_arrival_incrementally(store, tmp_path, now=now + timedelta(hours=2))
    assert len(store.list_items()) == 1


def camera_case(tmp_path):
    arrival = tmp_path / "arrival"; arrival.mkdir()
    gallery = tmp_path / "gallery"; gallery.mkdir()
    source = arrival / "camera.jpg"
    Image.new("RGB", (16, 16), "blue").save(source)
    store = MemoryVaultMasterStore(); owner = uuid4()
    scan_root(store, arrival, INCOMING_SOURCE, owner_lookup=lambda _: owner)
    item = store.list_items()[0]
    item = replace(item, metadata={**item.metadata, "camera_make":"Example", "camera_model":"Example camera", "exif_original_at":"2026-01-01T12:00:00"})
    store.items[item.source_path] = item
    policies = MemoryAutopilotStore()
    policy = policies.upsert_policy(owner, "example", "personal_photo", "Gallery", 80, 50, 2, 5)
    policy = policies.set_policy_status(policy.id, owner, "enabled")
    return store, policies, policy, item, arrival, gallery, source


def authorize_camera_recovery(store, item, arrival, gallery):
    from app.intake_recovery_authorization import register_recovery_authorization, queue_recovery_photo
    now=datetime.now(timezone.utc)
    manifest=dict(schema='personal-vault.intake-recovery-authorization.v1',id=str(uuid4()),
        owner_user_id=str(item.owner_user_id),requested_by='synthetic incident approval',
        issued_at=now.isoformat(),expires_at=(now+timedelta(hours=1)).isoformat(),
        items=[dict(item_id=str(item.id),sha256=item.sha256,size_bytes=item.size_bytes)])
    grant=register_recovery_authorization(store,manifest,now=now)
    return queue_recovery_photo(store,grant,item.id,arrival,{'Gallery':gallery},MemoryIngestionAiStore(),now=now)


def test_explicit_camera_recovery_publishes_only_via_receipt_without_ai(tmp_path):
    store, policies, policy, item, arrival, gallery, source = camera_case(tmp_path)
    assert not camera_photo_eligible(item, None, policy, set())
    ai = MemoryIngestionAiStore()
    assert authorize_camera_recovery(store,item,arrival,gallery)
    request = uuid4()
    assert process_next_move(store, arrival, {"Gallery":gallery}, theatre_queue=lambda _:request) == item.id
    assert source.exists() and not list(gallery.iterdir())
    assert store.get_item(item.id).state == "theatre_promotion_pending"
    receipt = dict(request_id=str(request), item_id=str(item.id), owner_user_id=str(item.owner_user_id), logical_destination=item.proposed_destination, logical_area="Gallery", slot_id="PV-DISK-002", relative_path=item.proposed_destination.removeprefix("/vault/"), expected_sha256=item.sha256, expected_size_bytes=item.size_bytes)
    published = store.publish_arrival_managed_receipt(item.id, receipt)
    assert published and published.visibility == "private"
    assert store.publish_arrival_managed_receipt(item.id, receipt) is None
    assert len(store.catalogued_assets) == 1
    assert process_autopilot_batch(policies, ai, store, arrival, {"Gallery":gallery}) is None


def test_camera_rule_respects_policy_identity_threshold_and_screenshot(tmp_path):
    _, _, policy, item, *_ = camera_case(tmp_path)
    assert not camera_photo_eligible(item, None, replace(policy, threshold=90), set())
    assert not camera_photo_eligible(replace(item, owner_user_id=uuid4()), None, policy, set())
    assert not camera_photo_eligible(replace(item, filename="Screenshot.jpg"), None, policy, set())
    assert not camera_photo_eligible(replace(item, duplicate_of_id=uuid4()), None, policy, set())
    assert not camera_photo_eligible(replace(item, metadata={}), None, policy, set())
    assert camera_photo_safety_eligible(replace(item, metadata={**item.metadata, "exif_original_at":"2026:01:01 12:00:00"}), None, set())
    assert not camera_photo_eligible(replace(item, metadata={**item.metadata, "exif_original_at":"invalid"}), None, policy, set())
    assert not camera_photo_eligible(replace(item, metadata={**item.metadata, "logical_filename":"Screenshot.jpg"}), None, policy, set())

def test_publisher_burst_coalesces_and_bad_request_cannot_block_later_items(tmp_path):
    from app.arrival_publisher_loop import drain_queues
    primary=tmp_path/'requests'; primary.mkdir()
    secondary=tmp_path/'secondary'; secondary.mkdir()
    for n in range(2000): (primary/f'{n:04}.json').write_text('{}')
    (secondary/'one.json').write_text('{}')
    clock=[0.0]; succeeded=[]; failed=[]; late=[False]
    def handle(path):
        if path.name=='0000.json':raise ValueError('synthetic poison item')
        succeeded.append(path.name);path.rename(path.with_suffix('.processed'))
    def reject(path,reason):
        failed.append(path.name);path.rename(path.with_suffix('.rejected.request'))
    def sleep(seconds):
        clock[0]+=seconds
        if clock[0]>=1 and not late[0]:
            late[0]=True;(primary/'late.json').write_text('{}')
    assert drain_queues(((primary,handle),(secondary,handle)),reject,clock=lambda:clock[0],sleep=sleep)==1
    assert len(succeeded)==2001 and failed==['0000.json']
    assert succeeded[0]=='one.json'  # the secondary queue cannot starve
    assert clock[0]>=4 and not list(primary.glob('*.json'))

def test_bulk_recovery_requires_explicit_ids_and_is_idempotent(tmp_path):
    from app.intake_bulk_recovery import recover_bulk_once, preview_bulk_recovery
    store, policies, _, item, arrival, gallery, _ = camera_case(tmp_path)
    ai = MemoryIngestionAiStore()
    assert preview_bulk_recovery(store, {item.id}) == {'needs_review':1}
    assert recover_bulk_once(policies,ai,store,arrival,{'Gallery':gallery},{item.id}) is None
    assert authorize_camera_recovery(store,item,arrival,gallery)
    assert recover_bulk_once(policies,ai,store,arrival,{'Gallery':gallery},{item.id}) is None
    assert preview_bulk_recovery(store, {item.id}) == {'move_queued':1}

def test_automatic_photo_duplicate_is_retained_without_a_second_publication(tmp_path):
    from app.arrival_photo_approval import queue_automatic_gallery_photo
    store, policies, policy, item, arrival, gallery, source = camera_case(tmp_path)
    second = arrival/'copy.jpg'; second.write_bytes(source.read_bytes())
    scan_root(store, arrival, INCOMING_SOURCE, owner_lookup=lambda _:item.owner_user_id)
    duplicate = next(x for x in store.list_items() if x.filename=='copy.jpg')
    store.items[item.source_path] = replace(store.get_item(item.id),metadata=item.metadata)
    assert authorize_camera_recovery(store,item,arrival,gallery)
    assert queue_automatic_gallery_photo(store,duplicate,policy,'camera-photo-v1','worker',policy_store=policies) is None
    assert second.exists() and store.get_item(duplicate.id).state=='needs_review'
    queued=store.get_item(item.id)
    assert queued.metadata['arrival_publication_rule']['version']=='incident-camera-photo-v1'
    request=uuid4()
    process_next_move(store,arrival,{'Gallery':gallery},theatre_queue=lambda _:request)
    receipt=dict(request_id=str(request),item_id=str(item.id),owner_user_id=str(item.owner_user_id),logical_destination=item.proposed_destination,logical_area='Gallery',slot_id='PV-DISK-002',relative_path=item.proposed_destination.removeprefix('/vault/'),expected_sha256=item.sha256,expected_size_bytes=item.size_bytes)
    assert store.publish_arrival_managed_receipt(item.id,receipt)
    assert queue_automatic_gallery_photo(store,duplicate,policy,'camera-photo-v1','worker',policy_store=policies) is None
    assert len(store.catalogued_assets)==1


def test_publisher_restart_reconciles_partial_completion_without_copying_again(tmp_path):
    import pytest
    from app.arrival_publisher_loop import drain_queues
    queue=tmp_path/'queue';queue.mkdir()
    for name in ('a','b'):(queue/f'{name}.json').write_text('{}')
    published=set();clock=[0.0]
    def interrupted(path):
        published.add(path.stem)
        raise KeyboardInterrupt('synthetic interruption after durable receipt')
    with pytest.raises(KeyboardInterrupt):
        drain_queues(((queue,interrupted),),lambda *_:None)
    assert published=={'a'} and len(list(queue.glob('*.json')))==2
    copies=[]
    def resume(path):
        if path.stem not in published:copies.append(path.stem);published.add(path.stem)
        path.rename(path.with_suffix('.processed'))
    assert drain_queues(((queue,resume),),lambda *_:None,clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+s))==0
    assert published=={'a','b'} and copies==['b']


def test_owner_lookup_failure_backs_off_without_blocking_good_source(tmp_path):
    for name in ('a.txt','b.txt'):(tmp_path/name).write_text(name)
    store=MemoryVaultMasterStore()
    def owner(path):
        if path.name=='a.txt':raise ValueError('synthetic provenance unavailable')
        return None
    now=datetime.now(timezone.utc)
    incremental.scan_arrival_incrementally(store,tmp_path,owner,limit=1,now=now)
    incremental.scan_arrival_incrementally(store,tmp_path,owner,limit=1,now=now)
    assert [x.filename for x in store.list_items()]==['b.txt']
    assert incremental.scan_arrival_incrementally(store,tmp_path,owner,now=now) is None

def test_rescan_cannot_replace_inflight_publication_evidence(tmp_path):
    from app.arrival_photo_approval import queue_automatic_gallery_photo
    store, policies, policy, item, arrival, gallery, source = camera_case(tmp_path)
    assert authorize_camera_recovery(store,item,arrival,gallery)
    request=uuid4()
    process_next_move(store,arrival,{'Gallery':gallery},theatre_queue=lambda _:request)
    pending=store.get_item(item.id)
    source.write_bytes(b'synthetic changed source')
    assert incremental.scan_arrival_incrementally(store,arrival) is None
    scan_root(store,arrival,INCOMING_SOURCE,owner_lookup=lambda _:item.owner_user_id)
    assert store.get_item(item.id)==pending

def test_legacy_publisher_adapter_preserves_executor_and_checks_signed_cancellation(tmp_path, monkeypatch):
    import ast,hashlib,hmac,json,os,sys
    from types import SimpleNamespace
    from app import arrival_publisher_loop, arrival_publication_coordination
    from app.intake_publisher_patch import prepare_patch
    import pytest
    source='''def canonical(value):
 return json.dumps(value,sort_keys=True,separators=(",", ":")).encode()
def process_request(path):
 executed.append(path.name)
 path.rename(path.with_suffix(".processed"))
def reject(path, reason):
 path.rename(path.with_suffix(".rejected"))
def supplier_roots():
 return supplier, None
def process_supplier_remediation(path):
 path.rename(path.with_suffix(".processed"))
def main():
 return "legacy next"
if __name__ == "__main__": main()
'''
    digest=hashlib.sha256(source.encode()).hexdigest()
    with pytest.raises(ValueError,match='changed'):
        prepare_patch(source,'0'*64)
    patched=prepare_patch(source,digest)
    original=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='process_request')
    retained=next(n for n in ast.parse(patched).body if isinstance(n,ast.FunctionDef) and n.name=='process_request')
    assert ast.dump(original)==ast.dump(retained)
    monkeypatch.setitem(sys.modules,'arrival_publisher_loop',arrival_publisher_loop)
    monkeypatch.setitem(sys.modules,'arrival_publication_coordination',arrival_publication_coordination)
    queue=tmp_path/'queue';queue.mkdir();supplier=tmp_path/'supplier';supplier.mkdir()
    key=tmp_path/'key';key.write_bytes(b'synthetic-key')
    namespace=dict(__name__='synthetic_publisher',QUEUE=queue,KEY=key,supplier=supplier,
        json=json,os=SimpleNamespace(geteuid=lambda:0,replace=os.replace),hmac=hmac,hashlib=hashlib,
        sys=SimpleNamespace(argv=['publisher','--next']),executed=[])
    exec(patched,namespace)
    assert namespace['main']()=='legacy next'
    item=uuid4();request={'item_id':str(item)}
    envelope={'request':request,'signature':hmac.new(key.read_bytes(),namespace['canonical'](request),hashlib.sha256).hexdigest()}
    path=queue/'one.json';path.write_text(json.dumps(envelope))
    with arrival_publication_coordination.publication_lock(queue):
        arrival_publication_coordination.cancel(queue,{'version':arrival_publication_coordination.VERSION,'item_id':str(item)},key.read_bytes())
    namespace['process_request'](path)
    assert namespace['executed']==[] and path.with_suffix('.cancelled.request').exists()

def test_blocked_model_job_cannot_gate_photo_publication(tmp_path, monkeypatch):
    import asyncio,threading
    from types import SimpleNamespace
    from app import main
    store,policies,_,item,arrival,gallery,_=camera_case(tmp_path)
    ai=MemoryIngestionAiStore();release=threading.Event()
    monkeypatch.setenv('PV_VAULT_MASTER_WORKER_ENABLED','true')
    monkeypatch.setenv('PV_VAULT_MASTER_AI_ENABLED','true')
    monkeypatch.setattr(main,'bootstrap_application_schema',lambda:None)
    monkeypatch.setattr(main,'lan_endpoint_hint',lambda:None)
    monkeypatch.setattr(main,'ken_enabled',lambda:False)
    monkeypatch.setattr(main,'get_vault_master_store',lambda:store)
    monkeypatch.setattr(main,'get_intake_store',lambda:SimpleNamespace(global_enabled=lambda:True))
    monkeypatch.setattr(main,'get_ai_store',lambda:None)
    monkeypatch.setattr(main,'get_ingestion_ai_store',lambda:ai)
    monkeypatch.setattr(main,'get_admin_username',lambda:'synthetic-owner')
    monkeypatch.setattr(main,'process_next_ai_job',lambda *_:None)
    monkeypatch.setattr(main,'queue_pending_ingestion_image_analysis',lambda *_:None)
    async def scenario():
        started=asyncio.Event();published=asyncio.Event();loop=asyncio.get_running_loop()
        def blocked(*_):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            return item.id
        monkeypatch.setattr(main,'process_next_ingestion_ai_job',blocked)
        async def publication():
            await started.wait()
            assert await asyncio.to_thread(store.record_decision,item.id,'approved','owner')
            assert await asyncio.to_thread(store.queue_move,item.id,'owner')
            published.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(main,'run_vault_master_worker',publication)
        try:
            async with main.lifespan(main.app):
                await asyncio.wait_for(published.wait(),timeout=2)
                assert not release.is_set()
                assert store.get_item(item.id).state=='move_queued'
                release.set()
        finally:
            release.set()
    asyncio.run(scenario())

def test_reconciler_skips_durable_receipts_and_reaches_new_work(tmp_path, monkeypatch):
    import hashlib,hmac,json
    from types import SimpleNamespace
    from app import arrival_managed_publisher as publisher
    root=tmp_path/'receipts';root.mkdir();key=b'synthetic-key'
    completed={str(uuid4()) for _ in range(200)}
    for request in completed:(root/f'{request}.json').write_text('{}')
    monkeypatch.setattr(publisher,'_receipt_root',lambda:root)
    monkeypatch.setattr(publisher,'_key',lambda:key)
    seen=[];asset=uuid4()
    store=SimpleNamespace(completed_arrival_request_ids=lambda:completed,
        publish_arrival_managed_receipt=lambda item,receipt:(seen.append(item) or SimpleNamespace(id=asset)))
    assert publisher.reconcile_next_receipt(store) is None and seen==[]
    request=uuid4();item=uuid4()
    receipt=dict(request_id=str(request),item_id=str(item),owner_user_id=str(uuid4()),logical_destination='/vault/Gallery/example.jpg',logical_area='Gallery',slot_id='PV-DISK-002',relative_path='Gallery/example.jpg',expected_sha256='a'*64,expected_size_bytes=1,verified_at=datetime.now(timezone.utc).isoformat())
    signature=hmac.new(key,publisher._payload(receipt),hashlib.sha256).hexdigest()
    (root/f'{request}.json').write_text(json.dumps({'receipt':receipt,'signature':signature}))
    assert publisher.reconcile_next_receipt(store)==asset and seen==[item]
