"""Synthetic names/prose only. No real candidate descriptions."""
from types import SimpleNamespace
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
from app import ken_service as lab, ken_config as ken
from app.ken_titles import trusted_people
from tests.test_postgres_ken import pg_ken
from tests.test_ken_config import complete, config


@pytest.fixture
def titled_ken(pg_ken):
    store,asset,owner=pg_ken
    with store.connect() as c:
        c.execute("ALTER TABLE vault_assets ADD COLUMN user_overrides JSONB DEFAULT '{}'::jsonb, ADD COLUMN display_title TEXT DEFAULT 'Filename fallback', ADD COLUMN metadata JSONB DEFAULT '{}'::jsonb, ADD COLUMN effective_metadata JSONB DEFAULT '{}'::jsonb, ADD COLUMN updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")
        c.execute('CREATE TABLE vault_asset_history(id UUID PRIMARY KEY,asset_id UUID,action TEXT,username TEXT,previous_values JSONB,current_values JSONB)')
    return store,asset,owner


class TitleAdapter:
    def __init__(self):self.requests=[]
    def generate_title(self,body):
        self.requests.append(body)
        return {**body,'title':'The Synthetic Pool Challenge','model_revision':ken.REVISION,'runtime':ken.RUNTIME}


def test_trusted_people_snapshot_excludes_candidates_exclusions_and_other_owners(pg_ken):
    store,asset,owner=pg_ken
    accepted,excluded,foreign,candidate,decision=[uuid4() for _ in range(5)]
    with store.connect() as c:
        for pid,name,powner in [(accepted,'Synthetic Ada',owner),(excluded,'Excluded',owner),(foreign,'Foreign',uuid4()),(candidate,'Speculative',owner),(decision,'Synthetic Ben',owner)]:
            c.execute('INSERT INTO vault_people(id,owner_user_id,display_name) VALUES(%s,%s,%s)',(pid,powner,name))
        for pid in (accepted,excluded,foreign):
            c.execute("INSERT INTO vault_asset_people(asset_id,person_id,owner_user_id,source) VALUES(%s,%s,%s,'user_face')",(asset,pid,owner))
        c.execute("INSERT INTO vault_asset_people_decisions(asset_id,person_id,owner_user_id,decision) VALUES(%s,%s,%s,'exclude'),(%s,%s,%s,'include')",(asset,excluded,owner,asset,decision,owner))
        assert trusted_people(c,asset,uuid4())==[]
    run=store.queue(asset,owner,config()); context=run['configuration']['correction_context']
    assert {p['name'] for p in context['trusted_people']}=={'Synthetic Ada','Synthetic Ben'}
    assert 'Speculative' not in ken.grounded_prompt(context)
    complete(store,run)
    revision=store.queue_ken(asset,owner,config(),text='There are exactly three people. The person in front is Ada.',parent_run_id=run['id'])
    prompt=ken.grounded_prompt(revision['configuration']['correction_context'])
    assert 'exactly three people' in prompt and 'authoritative' in prompt and 'latest explicit change wins' in prompt
    assert store.corrections(asset,owner)[0]['text'].startswith('There are exactly three')
    assert revision['configuration']['correction_fingerprint']!=run['configuration']['correction_fingerprint']


def test_title_generation_acceptance_manual_authority_and_no_unrelated_writes(titled_ken):
    store,asset,owner=titled_ken;adapter=TitleAdapter()
    first=complete(store,store.queue(asset,owner,config()))
    with store.connect() as c:before=c.execute('SELECT * FROM vault_assets WHERE id=%s',(asset,)).fetchone()
    suggestion=store.generate_title(asset,owner,first['id'],adapter)
    assert adapter.requests[0]['context']['accepted_description']=='Synthetic concise draft'
    assert adapter.requests[0]['context']['description_source']=='user_selected_ken_draft'
    with store.connect() as c:assert c.execute('SELECT * FROM vault_assets WHERE id=%s',(asset,)).fetchone()==before
    applied=store.set_video_title(asset,owner,'synthetic-owner',suggestion_id=suggestion['id'])
    assert applied['title_source']=='ken_accepted'
    assert store.titles(asset,owner)[0]['accepted_at'] is not None
    store.set_video_title(asset,owner,'synthetic-owner',manual_title='My authoritative title')
    second=store.generate_title(asset,owner,first['id'],adapter)
    with pytest.raises(ValueError,match='manual title'):
        store.set_video_title(asset,owner,'synthetic-owner',suggestion_id=second['id'])
    assert len(store.titles(asset,owner))==2 and store.titles(asset,uuid4())==[]
    with store.connect() as c:
        after=c.execute('SELECT * FROM vault_assets WHERE id=%s',(asset,)).fetchone()
        assert after['display_title']=='My authoritative title' and after['canonical']==before['canonical']
        assert after['user_overrides']=={'display_title':'My authoritative title'}
        assert c.execute('SELECT count(*) AS n FROM vault_asset_history').fetchone()['n']==2
    next_run=store.queue(asset,owner,config());complete(store,next_run)
    with store.connect() as c:assert c.execute('SELECT display_title FROM vault_assets WHERE id=%s',(asset,)).fetchone()['display_title']=='My authoritative title'


def test_title_manual_description_corrections_and_trusted_metadata(titled_ken):
    store,asset,owner=titled_ken;adapter=TitleAdapter()
    first=complete(store,store.queue(asset,owner,config()))
    revision=store.queue_ken(asset,owner,config(),text='Three people, including Synthetic Ada.',parent_run_id=first['id']);complete(store,revision)
    with store.connect() as c:
        c.execute("UPDATE vault_assets SET location='Synthetic location', metadata_provenance='{\"location\":\"user_override\"}', user_overrides=%s WHERE id=%s",(Jsonb({'video_narrative':'Synthetic final accepted narrative','location':'Synthetic location','captured_on':'2026-01-01'}),asset))
    store.generate_title(asset,owner,revision['id'],adapter)
    ctx=adapter.requests[0]['context']
    assert ctx['description_source']=='manual_final' and ctx['accepted_description']=='Synthetic final accepted narrative'
    assert ctx['corrections'][0]['text'].startswith('Three people') and ctx['trusted_location']['name']=='Synthetic location'
    with pytest.raises(ValueError):store.generate_title(asset,uuid4(),revision['id'],adapter)
    with pytest.raises(ValueError):store.set_video_title(asset,uuid4(),'other',manual_title='Wrong owner')
    with pytest.raises(ValueError):store.generate_title(asset,owner,first['id'],adapter)


def test_title_stale_context_wrong_bindings_and_busy_do_not_persist(titled_ken):
    store,asset,owner=titled_ken;first=complete(store,store.queue(asset,owner,config()))
    adapter=TitleAdapter(); suggestion=store.generate_title(asset,owner,first['id'],adapter)
    with store.worker_lock() as acquired:
        assert acquired
        with pytest.raises(ValueError,match='busy'):store.generate_title(asset,owner,first['id'],adapter)
    wrong=SimpleNamespace(generate_title=lambda body:{**adapter.generate_title(body),'asset_id':str(uuid4())})
    with pytest.raises(ValueError,match='identity'):store.generate_title(asset,owner,first['id'],wrong)
    def change(body):
        with store.connect() as c:c.execute("UPDATE vault_assets SET user_overrides='{"+'"video_narrative":"Changed synthetic final"'+"}' WHERE id=%s",(asset,))
        return adapter.generate_title(body)
    with pytest.raises(ValueError,match='context changed'):store.generate_title(asset,owner,first['id'],SimpleNamespace(generate_title=change))
    with pytest.raises(ValueError,match='stale'):store.set_video_title(asset,owner,'owner',suggestion_id=suggestion['id'])
    assert len(store.titles(asset,owner))==1


def test_prompt_contract_and_title_bounds():
    for text in ('70-80%', '80-150 words', 'what IS happening', 'If removing a detail', 'camera movement', 'explicitly', 'neutral', 'uncertainty'):
        assert text in ken.PROMPT
    assert 'Pool Grand Prix' not in ken.TITLE_PROMPT and 'sample answer' not in ken.PROMPT
    assert ken.PARAMETERS['max_tokens']==384 and ken.TITLE_TOKENS==64
    assert ken.validate_title('The Synthetic Pool Challenge')=='The Synthetic Pool Challenge'
    for bad in ('', 'x'*121, 'one\ntwo', 'one '*13, None):
        with pytest.raises(ValueError):ken.validate_title(bad)
    assert 'trusted' in ken.TITLE_PROMPT and 'never invent names' in ken.TITLE_PROMPT

from tests.test_ken_service import ken_api_fixture
from tests.test_vault_libraries import authenticate


def test_title_api_owner_scope_and_explicit_acceptance(ken_api_fixture,titled_ken,monkeypatch):
    from dataclasses import replace
    from app.main import app
    from app import ken_api as api
    client,_,vault,asset,_=ken_api_fixture
    store,old_asset,owner=titled_ken
    with store.connect() as c:c.execute('UPDATE vault_assets SET id=%s,owner_user_id=%s',(asset.id,asset.owner_user_id))
    app.dependency_overrides[lab.get_ken_store]=lambda:store
    monkeypatch.setattr(api, 'ADAPTER', TitleAdapter())
    base=f'/api/personal-videos/ken/assets/{asset.id}'
    run=complete(store,store.queue(asset.id,asset.owner_user_id,config()))
    assert client.get(base+'/titles').status_code==401
    assert client.post(base+'/titles',json={'run_id':str(run['id'])}).status_code==401
    authenticate(client)
    response=client.post(base+'/titles',json={'run_id':str(run['id'])})
    assert response.status_code==200
    sid=response.json()['id']
    assert client.get(base+'/titles').json()['suggestions'][0]['id']==sid
    assert client.post(base+f'/titles/{sid}/accept').status_code==200
    assert client.patch(base+'/title',json={'title':'My final synthetic title'}).status_code==200
    assert client.post(base+f'/titles/{sid}/accept').status_code==409
    foreign=replace(asset,id=uuid4(),owner_user_id=uuid4(),vault_path='/vault/Home Videos/other.mp4')
    vault.catalogued_assets[foreign.vault_path]=foreign
    other=f'/api/personal-videos/ken/assets/{foreign.id}'
    assert client.get(other+'/titles').status_code==404
    assert client.post(other+'/titles',json={'run_id':str(run['id'])}).status_code==404
    assert client.post(other+f'/titles/{sid}/accept').status_code==404
    assert client.patch(other+'/title',json={'title':'No'}).status_code==404
    with store.connect() as c:assert c.execute('SELECT display_title FROM vault_assets').fetchone()['display_title']=='My final synthetic title'


def test_trusted_names_change_prepared_fingerprint_without_changing_source(video,tmp_path):
    from app.ken_attestation import digest_json
    source,asset=video
    original=lab.file_sha256(source);fingerprints=[]
    for name in ('Synthetic Ada','Synthetic Ben'):
        cfg=config();cfg['run_id']=str(uuid4())
        cfg['correction_context']=ken.correction_context('',[],[{'person_id':str(uuid4()),'name':name,'source':'accepted_people_association'}])
        work=tmp_path/f'pv-ken-{uuid4()}-00000000';work.mkdir()
        payload=lab.prepare_input(asset,cfg,work)
        lab.verify_input(asset.id,cfg,payload)
        assert payload['correction_fingerprint']==digest_json(cfg['correction_context'])
        fingerprints.append(payload['input_fingerprint'])
    assert fingerprints[0]!=fingerprints[1] and lab.file_sha256(source)==original


def test_manual_title_clear_restores_accepted_ken_title(ken_api_fixture):
    from dataclasses import replace
    from app.vault_master import apply_catalogue_metadata_changes
    _,_,_,asset,_=ken_api_fixture
    asset=replace(asset,display_title='Manual title',metadata={**asset.metadata,'ken_accepted_title':'Accepted synthetic title'},
                  user_overrides={'display_title':'Manual title'},metadata_provenance={'display_title':'user_override'})
    updated=apply_catalogue_metadata_changes(asset,{'display_title':None})
    assert updated.display_title=='Accepted synthetic title'
    assert updated.metadata_provenance['display_title']=='ken_accepted'
    assert updated.effective_metadata['display_title']=='Accepted synthetic title'


from tests.test_ken_native import video


def test_accepted_title_survives_metadata_import_and_detection_refresh(ken_api_fixture,monkeypatch):
    from dataclasses import replace
    from app import vault_master as vm
    _,_,_,asset,_=ken_api_fixture
    asset=replace(asset,display_title='Accepted synthetic title',metadata={'ken_accepted_title':'Accepted synthetic title'},
                  user_overrides={},metadata_provenance={'display_title':'ken_accepted'})
    imported=vm.apply_imported_asset_metadata(asset,{'display_title':'New fallback','location':'Synthetic place'},'fixture')
    assert imported.display_title=='Accepted synthetic title' and imported.location=='Synthetic place'
    assert imported.metadata_provenance['display_title']=='ken_accepted'
    monkeypatch.setattr(vm,'canonical_asset_metadata_layers',lambda item:({'display_title':'Detected fallback'},{},{},{}))
    item=SimpleNamespace(filename=asset.filename,size_bytes=asset.size_bytes,mime_type=asset.mime_type,sha256=asset.sha256,metadata={})
    refreshed=vm.refresh_catalogued_asset_detection(imported,item)
    assert refreshed.display_title=='Accepted synthetic title' and refreshed.metadata['ken_accepted_title']=='Accepted synthetic title'
    assert refreshed.effective_metadata['display_title']=='Accepted synthetic title'
    manual=vm.apply_catalogue_metadata_changes(refreshed,{'display_title':'Manual wins'})
    assert vm.refresh_catalogued_asset_detection(manual,item).display_title=='Manual wins'
    assert vm.apply_imported_asset_metadata(manual,{'display_title':'Fallback'},'fixture').display_title=='Manual wins'


def test_location_is_snapshotted_for_initial_and_correction_runs_and_titles(titled_ken):
    from app.ken_titles import trusted_location
    store,asset,owner=titled_ken
    with store.connect() as c:
        c.execute("UPDATE vault_assets SET location='Greece',metadata_provenance='{\"location\":\"embedded\"}' WHERE id=%s",(asset,))
        assert trusted_location(c,asset,uuid4()) is None
    initial=store.queue(asset,owner,config())
    assert initial['configuration']['correction_context']['trusted_location']=={'name':'Greece','source':'embedded'}
    complete(store,initial)
    revised=store.queue_ken(asset,owner,config(),text='Two participants are swimming.',parent_run_id=initial['id'])
    assert revised['configuration']['correction_context']['trusted_location']['name']=='Greece'
    complete(store,revised)
    adapter=TitleAdapter();store.generate_title(asset,owner,revised['id'],adapter)
    assert adapter.requests[0]['context']['trusted_location']['name']=='Greece'
