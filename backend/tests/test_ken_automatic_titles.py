"""Automatic titles use synthetic prose and isolated PostgreSQL only."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
from app import ken_service as lab
from tests.test_ken_titles import titled_ken, TitleAdapter
from tests.test_postgres_ken import pg_ken
from tests.test_ken_config import config, complete


def pending(store,asset,owner,description='Synthetic pool activity',**kwargs):
    run=store.queue_ken(asset,owner,config(),**kwargs)
    run=complete(store,run,description)
    cfg={**run['configuration'],'automatic_title':{'status':'pending','attempts':0}}
    store.update(run['id'],'completed',config=cfg)
    return store.get(run['id'])


def test_automatic_title_persists_updates_after_correction_and_preserves_canonical(titled_ken):
    store,a,o=titled_ken;adapter=TitleAdapter()
    with store.connect() as c: before=c.execute('SELECT canonical FROM vault_assets WHERE id=%s',(a,)).fetchone()
    first=pending(store,a,o)
    with store.worker_lock(): assert store.process_pending_title(adapter)==first['id']
    with store.connect() as c:
        asset=c.execute('SELECT * FROM vault_assets WHERE id=%s',(a,)).fetchone()
        assert asset['display_title']=='The Synthetic Pool Challenge'
        assert asset['metadata_provenance']['display_title']=='ken_generated'
        assert asset['canonical']==before['canonical']
    assert store.titles(a,o)[0]['accepted_at'] is not None
    assert store.process_pending_title(adapter) is None
    # Simulate restart between atomic title commit and completion status update.
    store.update(first['id'],'completed',config=first['configuration'])
    store.process_pending_title(adapter)
    assert len(adapter.requests)==1 and len(store.titles(a,o))==1
    revised=pending(store,a,o,'Synthetic revised swimming',text='Focus on swimming.',parent_run_id=first['id'])
    store.process_pending_title(adapter)
    ctx=adapter.requests[-1]['context']
    assert ctx['accepted_description']=='Synthetic revised swimming'
    assert ctx['corrections'][0]['text']=='Focus on swimming.'
    assert len(store.titles(a,o))==2
    assert store.get(revised['id'])['status']=='completed'


def test_manual_title_before_and_during_inference_wins(titled_ken):
    store,a,o=titled_ken
    first=pending(store,a,o)
    adapter=TitleAdapter()
    def racing(body):
        store.set_video_title(a,o,'user',manual_title='Manual wins')
        return adapter.generate_title(body)
    store.process_pending_title(SimpleNamespace(generate_title=racing))
    with store.connect() as c: assert c.execute('SELECT display_title FROM vault_assets WHERE id=%s',(a,)).fetchone()['display_title']=='Manual wins'
    second=pending(store,a,o,parent_run_id=first['id'])
    store.process_pending_title(adapter)
    assert len(adapter.requests)==1
    assert store.get(second['id'])['configuration']['automatic_title']['status']=='manual_title_preserved'


def test_title_failure_is_bounded_and_preserves_successful_description(titled_ken):
    store,a,o=titled_ken;run=pending(store,a,o)
    adapter=SimpleNamespace(generate_title=lambda body: (_ for _ in ()).throw(RuntimeError('Synthetic failure')))
    for _ in range(3):store.process_pending_title(adapter)
    saved=store.get(run['id'])
    assert saved['status']=='completed' and saved['result']==run['result']
    assert saved['configuration']['automatic_title']=={'status':'failed','attempts':3}
    assert store.process_pending_title(adapter) is None
    assert store.titles(a,o)==[]


def test_worker_schedules_title_only_after_success(titled_ken,monkeypatch):
    store,a,o=titled_ken
    run=store.queue(a,o,config())
    def prepare(asset,cfg,path):
        cfg['input_integrity']={'input_fingerprint':'a'*64}
        return {'asset_id':str(a),'run_id':str(run['id']),'input_fingerprint':'a'*64,'input_mode':'native_video','correction_fingerprint':cfg['correction_fingerprint']}
    adapter=TitleAdapter()
    adapter.analyse=lambda body:{**body,'description':'Synthetic main activity'}
    monkeypatch.setattr(lab, 'ADAPTER', adapter)
    monkeypatch.setattr(lab,'prepare_input',prepare)
    monkeypatch.setattr(lab,'verify_input',lambda *args:None)
    monkeypatch.setattr(lab,'enforce_attestation',lambda payload,result:result)
    vault=SimpleNamespace(get_catalogued_asset_by_id=lambda _:SimpleNamespace(id=a,owner_user_id=o))
    assert lab.process_next(store,vault)==run['id']
    assert store.get(run['id'])['configuration']['automatic_title']['status']=='pending'
    assert lab.process_next(store,vault)==run['id']
    assert len(adapter.requests)==1
