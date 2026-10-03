from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
import pytest
import psycopg
from app import ken_service as lab, ken_config as ken
from app.ken_attestation import digest_json
from tests.test_postgres_ken import pg_ken
from tests.test_ken_native import video
from tests.test_ken_service import ken_api_fixture, FakeAdapter
from tests.test_vault_libraries import authenticate


def config():
    value=lab.configuration()
    value.pop('metadata_destination')  # Store/prompt unit fixtures; normal publication tested separately.
    return value


def complete(store, run, description='Synthetic concise draft'):
    cfg = {**run['configuration'], 'input_integrity': {'input_fingerprint': 'a'*64}}
    result = {'asset_id': str(run['asset_id']), 'run_id': str(run['id']), 'input_fingerprint': 'a'*64,
              'correction_fingerprint': cfg['correction_fingerprint'], 'description': description}
    store.update(run['id'], 'completed', config=cfg, result=result)
    return store.get(run['id'])


def test_ken_role_model_prompt_and_output_contract():
    cfg = config()
    assert cfg['role'] == 'KEN' and cfg['model_id'] == ken.MODEL_ID
    assert cfg['model_revision'] == ken.REVISION and cfg['parameters']['max_tokens'] == 384
    assert cfg['prompt_version'] == ken.PROMPT_VERSION
    for phrase in ('MAIN ACTIVITY', 'camera movement', 'incidental background', 'Brief appearances', '80-150 words', 'uncertainty'):
        assert phrase in cfg['prompt']
    assert 'sample answer' not in cfg['prompt']


@pytest.mark.parametrize('duration', [1000, 46000, 90000, 600000, 1091220, 1800000])
def test_whole_video_sampling_and_deterministic_chunks(duration):
    policy = ken.policy(duration)
    assert policy == ken.policy(duration) and policy['fps'] == 1
    chunks = policy['chunks']
    assert chunks[0]['start_ms'] == 0 and chunks[-1]['end_ms'] == duration
    for a,b in zip(chunks,chunks[1:]):
        assert b['start_ms'] == a['end_ms'] - 2000
    assert all(0 < c['end_ms']-c['start_ms'] <= 42000 for c in chunks)
    assert max((c['end_ms']-c['start_ms'])/1000/2*192 for c in chunks) < 5000
    with pytest.raises(ValueError):
        ken.policy(ken.MAX_DURATION_MS+1)


def test_corrections_are_separate_ordered_and_survive_new_store(pg_ken):
    store, asset, owner = pg_ken
    first = complete(store, store.queue(asset, owner, config()))
    second = store.queue_ken(asset, owner, config(), text='There are exactly two people.', parent_run_id=first['id'])
    assert second['configuration']['draft_state'] == 'user_adjusted'
    assert second['configuration']['correction_context']['previous_description'] == 'Synthetic concise draft'
    assert store.get(first['id'])['result']['description'] == 'Synthetic concise draft'
    complete(store, second, 'Synthetic adjusted draft')
    third = store.queue_ken(asset, owner, config(), text='The camera moves; the person does not walk.', parent_run_id=second['id'])
    rows = lab.KenStore(store.conninfo).corrections(asset, owner)
    assert [r['sequence'] for r in rows] == [1,2]
    assert rows[0]['run_id'] == second['id'] and rows[1]['run_id'] == third['id']
    assert all(r['owner_user_id'] == owner and r['model_revision'] == ken.REVISION for r in rows)
    context = third['configuration']['correction_context']
    assert len(context['corrections']) == 2
    prompt = ken.grounded_prompt(context)
    assert 'authoritative' in prompt and 'exactly two people' in prompt and 'camera moves' in prompt
    assert third['configuration']['correction_fingerprint'] == digest_json(context)


def test_wrong_owner_stale_parent_and_busy_adjustments_fail_atomically(pg_ken):
    store, asset, owner = pg_ken
    first = complete(store, store.queue(asset, owner, config()))
    with pytest.raises(ValueError):
        store.queue_ken(asset, uuid4(), config(), text='Wrong owner', parent_run_id=first['id'])
    with pytest.raises(ValueError):
        store.queue_ken(asset, owner, config(), text='Stale draft', parent_run_id=uuid4())
    assert store.corrections(asset, owner) == []
    second = store.queue_ken(asset, owner, config(), text='Shorter', parent_run_id=first['id'])
    with pytest.raises(ValueError):
        store.queue_ken(asset, owner, config(), text='Duplicate', parent_run_id=first['id'])
    assert len(store.corrections(asset, owner)) == 1
    assert store.corrections(asset, uuid4()) == []
    with store.connect() as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute('UPDATE vault_ken_corrections SET owner_user_id=%s WHERE run_id=%s', (uuid4(),second['id']))


def test_video_corrections_never_transfer_and_canonical_authority_unchanged(pg_ken):
    store, asset, owner = pg_ken
    other = uuid4()
    with store.connect() as conn:
        conn.execute("INSERT INTO vault_assets VALUES(%s,%s,'{\"manual_final\":\"user final\",\"florence\":\"evidence\",\"people\":[1],\"tags\":[2],\"playback_proxy\":\"same\",\"thumbnail\":\"same\"}')", (other,owner))
        before = conn.execute('SELECT * FROM vault_assets ORDER BY id').fetchall()
    first = complete(store, store.queue(asset, owner, config()))
    store.queue_ken(asset, owner, config(), text='Exactly two people', parent_run_id=first['id'])
    other_run = store.queue(other, owner, config())
    assert other_run['configuration']['correction_context']['corrections'] == []
    with store.connect() as conn:
        assert conn.execute('SELECT * FROM vault_assets ORDER BY id').fetchall() == before




def test_correction_changes_input_fingerprint_but_not_video(video, tmp_path):
    source, asset = video
    before = lab.file_sha256(source)
    values = []
    for text in ('Two people','Camera moves'):
        cfg = config(); cfg['run_id'] = str(uuid4())
        cfg['correction_context'] = ken.correction_context('Synthetic previous draft', [{'id':uuid4(),'sequence':1,'text':text}])
        work = tmp_path/f'pv-ken-{uuid4()}-00000000'; work.mkdir()
        payload = lab.prepare_input(asset,cfg,work)
        lab.verify_input(asset.id,cfg,payload)
        values.append(payload)
    assert values[0]['input_fingerprint'] != values[1]['input_fingerprint']
    assert values[0]['native_sha256'] == values[1]['native_sha256']
    assert lab.file_sha256(source) == before


def test_correction_api_owner_scope_and_reset_hides_old_runs(ken_api_fixture, pg_ken, monkeypatch):
    from app.main import app
    from app import ken_api as api
    client, _, vault, asset, _ = ken_api_fixture
    store, old_asset, old_owner = pg_ken
    with store.connect() as conn:
        conn.execute('UPDATE vault_assets SET id=%s,owner_user_id=%s', (asset.id,asset.owner_user_id))
    app.dependency_overrides[lab.get_ken_store] = lambda: store
    monkeypatch.setattr(api, 'ADAPTER', FakeAdapter())
    url=f'/api/personal-videos/ken/assets/{asset.id}/corrections'
    assert client.get(url).status_code == 401
    authenticate(client)
    first = complete(store,store.queue(asset.id,asset.owner_user_id,config()))
    result = client.post(url,json={'parent_run_id':str(first['id']),'text':'There are two people.'})
    assert result.status_code == 202
    assert client.get(url).json()[0]['text'] == 'There are two people.'
    other = uuid4()
    foreign = replace(asset, id=other, owner_user_id=uuid4(), vault_path='/vault/Home Videos/foreign.mp4')
    vault.catalogued_assets[foreign.vault_path] = foreign
    assert client.get(f'/api/personal-videos/ken/assets/{other}/corrections').status_code == 404
    assert client.post(f'/api/personal-videos/ken/assets/{other}/corrections',json={'parent_run_id':str(first['id']),'text':'No'}).status_code == 404


def test_database_rejects_changed_correction_binding(pg_ken):
    from psycopg.types.json import Jsonb
    store, asset, owner = pg_ken
    run = complete(store, store.queue(asset, owner, config()))
    wrong = {**run['result'], 'correction_fingerprint': 'b'*64}
    with store.connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute('UPDATE vault_ken_runs SET result=%s WHERE id=%s', (Jsonb(wrong),run['id']))
    assert store.get(run['id'])['result'] == run['result']




def test_correction_limits_are_atomic_and_do_not_truncate_history(pg_ken):
    store, asset, owner = pg_ken
    run = complete(store,store.queue(asset,owner,config()))
    for text in ('a'*2000,'b'*2000):
        run = complete(store,store.queue_ken(asset,owner,config(),text=text,parent_run_id=run['id']))
    with pytest.raises(ValueError, match='context is full'):
        store.queue_ken(asset,owner,config(),text='one more',parent_run_id=run['id'])
    assert len(store.corrections(asset,owner)) == 2
    assert len(store.history(asset,owner)) == 3
    with pytest.raises(ValueError, match='context is full'):
        ken.correction_context('',[{'text':'x'}]*21)
