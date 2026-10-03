"""Normal KEN metadata integration, using synthetic descriptions only."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb

from app import ken_service as lab, ken_api as api
from app.home_video_ken import DESTINATION, description_for
from tests.test_postgres_ken import pg_ken
from tests.test_ken_titles import titled_ken, TitleAdapter
from tests.test_ken_config import config, complete
from tests.test_ken_service import ken_api_fixture, FakeAdapter
from tests.test_vault_libraries import authenticate, catalogue_file
from tests.test_video_intelligence import _configure_video_api


@pytest.fixture
def normal_ken(titled_ken):
    store, asset, owner = titled_ken
    with store.connect() as c:
        c.execute("ALTER TABLE vault_assets ADD COLUMN asset_type TEXT DEFAULT 'Home Videos', ADD COLUMN detected_metadata JSONB DEFAULT '{}'::jsonb")
    return store, asset, owner


def normal_config():
    return {**config(), 'metadata_destination': DESTINATION}


def asset_record(store, asset):
    with store.connect() as c:
        return SimpleNamespace(**c.execute('SELECT * FROM vault_assets WHERE id=%s', (asset,)).fetchone())


def test_atomic_normal_description_correction_manual_authority_and_titles(normal_ken):
    store, a, o = normal_ken
    original = asset_record(store, a).canonical
    run = complete(store, store.queue(a, o, normal_config()), 'Synthetic tennis match.')
    assert description_for(asset_record(store, a), 'Florence fallback') == ('Synthetic tennis match.', 'ken_generated')
    assert asset_record(store, a).metadata['ken_description']['run_id'] == str(run['id'])
    adjusted = complete(store, store.queue_ken(a, o, normal_config(), text='The camera moves.', parent_run_id=run['id']), 'Synthetic corrected match.')
    assert description_for(asset_record(store, a)) == ('Synthetic corrected match.', 'ken_adjusted')
    assert len(store.corrections(a, o)) == 1
    # A replay cannot replace a newer adjusted record.
    store.update(run['id'], 'completed')
    assert description_for(asset_record(store, a))[0] == 'Synthetic corrected match.'
    with store.connect() as c:
        c.execute('UPDATE vault_assets SET user_overrides=%s WHERE id=%s', (Jsonb({'video_narrative':'Manual final.'}), a))
    third = complete(store, store.queue_ken(a, o, normal_config(), text='Make it shorter.', parent_run_id=adjusted['id']), 'Synthetic short match.')
    assert description_for(asset_record(store, a)) == ('Manual final.', 'user')
    assert len(store.corrections(a, o)) == 2
    store.update(third['id'], 'completed', config={**third['configuration'], 'automatic_title':{'status':'pending','attempts':0}})
    adapter = TitleAdapter()
    store.process_pending_title(adapter)
    assert adapter.requests[-1]['context']['accepted_description'] == 'Manual final.'
    assert asset_record(store, a).display_title == 'The Synthetic Pool Challenge'
    assert asset_record(store, a).canonical == original


def test_title_failure_cannot_undo_normal_description(normal_ken):
    store, a, o = normal_ken
    run = complete(store, store.queue(a, o, normal_config()))
    store.update(run['id'], 'completed', config={**run['configuration'], 'automatic_title':{'status':'pending','attempts':0}})
    adapter = SimpleNamespace(generate_title=lambda _: (_ for _ in ()).throw(ValueError('Synthetic failure')))
    for _ in range(3): store.process_pending_title(adapter)
    assert store.get(run['id'])['status'] == 'completed'
    assert description_for(asset_record(store, a))[1] == 'ken_generated'


def test_normal_worker_publishes_description_then_generates_title(normal_ken, monkeypatch):
    store, a, o = normal_ken
    run = store.queue(a, o, normal_config())
    def prepare(asset, cfg, work):
        cfg['input_integrity'] = {'input_fingerprint':'a'*64}
        return {'asset_id':str(a),'run_id':str(run['id']),'input_fingerprint':'a'*64,
                'input_mode':'native_video','correction_fingerprint':cfg['correction_fingerprint']}
    adapter = TitleAdapter()
    adapter.analyse = lambda payload: {**payload,'description':'Synthetic swimming race.'}
    monkeypatch.setattr(lab, 'ADAPTER', adapter)
    monkeypatch.setattr(lab, 'prepare_input', prepare)
    monkeypatch.setattr(lab, 'verify_input', lambda *a: None)
    monkeypatch.setattr(lab, 'enforce_attestation', lambda payload,result: result)
    vault = SimpleNamespace(get_catalogued_asset_by_id=lambda _:SimpleNamespace(id=a,owner_user_id=o))
    assert lab.process_next(store, vault) == run['id']
    assert description_for(asset_record(store, a)) == ('Synthetic swimming race.', 'ken_generated')
    assert not adapter.requests
    assert lab.process_next(store, vault) == run['id']
    assert len(adapter.requests) == 1
    assert adapter.requests[0]['context']['accepted_description'] == 'Synthetic swimming race.'


def test_bad_binding_rolls_back_completion_and_canonical_publication(normal_ken):
    store, a, o = normal_ken
    run = store.queue(a, o, normal_config())
    cfg = {**run['configuration'], 'input_integrity':{'input_fingerprint':'a'*64}}
    result = {'asset_id':str(uuid4()), 'run_id':str(run['id']), 'description':'Synthetic wrong video.',
              'input_fingerprint':'a'*64,'correction_fingerprint':cfg['correction_fingerprint']}
    with pytest.raises(Exception): store.update(run['id'], 'completed', config=cfg, result=result)
    assert store.get(run['id'])['status'] == 'queued'
    assert 'ken_description' not in asset_record(store, a).metadata


@pytest.mark.parametrize('field', ['asset_id', 'owner_user_id'])
def test_normal_metadata_cannot_cross_asset_or_owner(field):
    asset = SimpleNamespace(id=uuid4(), owner_user_id=uuid4(), user_overrides={}, metadata={})
    record = {'version':DESTINATION,'asset_id':str(asset.id),'owner_user_id':str(asset.owner_user_id),
              'description':'Synthetic match.','source':'ken_generated'}
    asset.metadata['ken_description'] = {**record, field:str(uuid4())}
    assert description_for(asset, 'Fallback') == ('Fallback', 'vault_master')


def test_normal_api_fixes_model_and_input_and_enforces_owner(ken_api_fixture, monkeypatch):
    client, store, vault, asset, source = ken_api_fixture
    monkeypatch.setattr(api, 'ADAPTER', FakeAdapter())
    store.queue_ken = lambda a,o,c,**kw: store.queue(a,o,c)
    base = f'/api/personal-videos/ken/assets/{asset.id}'
    assert client.post(base+'/runs').status_code == 401
    authenticate(client)
    response = client.post(base+'/runs', json={'candidate_id':'unsupported','input_mode':'sampled_frames'})
    assert response.status_code == 202
    row = response.json()
    assert row['configuration']['model_id'] == lab.ENGINE.model_id and row['input_mode'] == 'native_video'
    assert row['configuration']['metadata_destination'] == DESTINATION
    assert row['configuration']['prompt'] == config()['prompt']
    assert client.get(base+'/runs').json()[0]['id'] == row['id']
    vault.catalogued_assets[asset.vault_path] = replace(asset, owner_user_id=uuid4())
    assert client.post(base+'/runs').status_code == 404
    assert client.get(base+'/runs').status_code == 404
    assert client.post(base+'/corrections',json={'parent_run_id':row['id'],'text':'Synthetic correction'}).status_code == 404
    assert source.read_bytes() == b'original video'


def test_normal_details_api_resolves_ken_and_manual_reset(client, tmp_path, monkeypatch):
    store, intelligence, videos = _configure_video_api(tmp_path, monkeypatch)
    source = videos/'clip.mp4'; source.write_bytes(b'original')
    asset = catalogue_file(store, videos, source, asset_type='Home Videos', vault_root='/vault/Home Videos', mime_type='video/mp4')
    record = {'version':DESTINATION,'asset_id':str(asset.id),'owner_user_id':str(asset.owner_user_id),
              'description':'Synthetic swimming.','source':'ken_adjusted'}
    store.catalogued_assets[asset.vault_path] = replace(asset, metadata={**asset.metadata,'ken_description':record})
    authenticate(client)
    file_id = client.get('/api/personal-videos').json()[0]['id']
    path = f'/api/personal-videos/{file_id}/details'
    assert client.get(path).json()['narrative'] == 'Synthetic swimming.'
    edit = f'/api/personal-videos/intelligence/{asset.id}/narrative'
    assert client.patch(edit,json={'narrative':'Manual description.'}).json()['narrative_source'] == 'user'
    assert client.get(path).json()['narrative'] == 'Manual description.'
    reset = client.patch(edit,json={'narrative':None}).json()
    assert reset['narrative'] == 'Synthetic swimming.' and reset['narrative_source'] == 'ken_adjusted'
    assert source.read_bytes() == b'original'
