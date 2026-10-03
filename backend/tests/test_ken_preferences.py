"""Structured owner profiles and current-video locality; synthetic fixtures only."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from app import ken_owner_style as store, ken_style as style, ken_config as ken
from app.video_location import ken_location_display
from app.ken_titles import trusted_location
from tests.test_postgres_ken import pg_ken
from tests.test_ken_config import config, complete
from tests.test_ken_service import ken_api_fixture
from tests.test_vault_libraries import authenticate


def test_preferences_persist_seed_idempotently_and_do_not_cross_owners(pg_ken):
    lab, asset, owner=pg_ken
    other=uuid4()
    with lab.connect() as c:
        store.save_preferences(c,owner,style.DEFAULTS,seed=True)
        assert store.preferences(c,owner)['preferences']==style.DEFAULTS
        assert store.preferences(c,other)=={'preferences':{},'revision':0}
        store.save_preferences(c,owner,{'activity_first':True})
        store.save_preferences(c,owner,style.DEFAULTS,seed=True)
    with lab.connect() as c:
        assert store.preferences(c,owner)=={'preferences':{'activity_first':True},'revision':2}
        assert c.execute("SELECT to_regclass('vault_ken_style_exemplars') AS t").fetchone()['t'] is None
    with pytest.raises(ValueError):store.preferences(None,'administrator')


def test_queue_freezes_owner_profile_and_keeps_facts_asset_scoped(pg_ken):
    lab,asset,owner=pg_ken
    other=uuid4()
    with lab.connect() as c:
        store.save_preferences(c,owner,style.DEFAULTS,seed=True)
        c.execute('INSERT INTO vault_assets(id,owner_user_id) VALUES(%s,%s)',(other,owner))
        before=c.execute('SELECT * FROM vault_assets ORDER BY id').fetchall()
    initial=lab.queue_ken(asset,owner,config())
    ctx=initial['configuration']['correction_context']
    assert ctx['owner_style']['preferences']==style.DEFAULTS
    assert ctx['previous_description']=='' and ctx['corrections']==[]
    assert initial['configuration']['grounded_prompt_sha256']==hashlib.sha256(ken.grounded_prompt(ctx).encode()).hexdigest()
    first=complete(lab,initial,'Synthetic sailors in OldTown.')
    adjusted=lab.queue_ken(asset,owner,config(),text='Exactly seven sailors stand behind a submarine.',parent_run_id=first['id'])
    prompt=ken.grounded_prompt(adjusted['configuration']['correction_context'])
    assert 'seven sailors' in prompt and 'authoritative' in prompt
    assert 'Name a clearly supported activity directly' in prompt
    second=lab.queue_ken(other,owner,config())
    other_prompt=ken.grounded_prompt(second['configuration']['correction_context'])
    for fact in ('seven sailors','submarine','OldTown'):assert fact not in other_prompt
    with lab.connect() as c:
        store.save_preferences(c,owner,{})
        assert c.execute('SELECT * FROM vault_assets ORDER BY id').fetchall()==before
    assert lab.get(initial['id'])['configuration']['correction_context']['owner_style']==ctx['owner_style']


@pytest.mark.parametrize('city,country',[('Example Borough','Example Country A'),('Example Town','Example Country B'),('Example City','Example Country C'),('Example Locality Á','Example Country D'),('Example Island','Example Country D')])
def test_existing_gps_location_locality_only_without_canonical_mutation(monkeypatch,city,country):
    from app import video_location
    monkeypatch.setattr(video_location.reverse_geocode,'search',lambda _: [{'city':city,'country':country}])
    canonical=f'{city}, {country}'
    metadata={'gps_latitude':1.0,'gps_longitude':2.0,'location':canonical}
    original=deepcopy(metadata)
    assert ken_location_display(canonical,metadata)==city
    assert metadata==original
    assert ken_location_display('Different Place',metadata)=='Different Place'


@pytest.mark.parametrize('key',['city','town','locality','municipality','island'])
def test_trusted_structured_components_and_fallback(key):
    assert ken_location_display('Example Town, Example Country B',{'location_details':{'name':'Example Town, Example Country B',key:'Example Town'}})=='Example Town'
    assert ken_location_display('Example Country D',{})=='Example Country D'
    assert ken_location_display(None,{}) is None
    assert ken_location_display('Unstructured, Ambiguous',{})=='Unstructured, Ambiguous'


def test_title_and_description_receive_current_owner_locality(pg_ken,monkeypatch):
    lab,asset,owner=pg_ken
    from app import video_location
    monkeypatch.setattr(video_location.reverse_geocode,'search',lambda _: [{'city':'Example City','country':'Example Country C'}])
    with lab.connect() as c:
        c.execute("ALTER TABLE vault_assets ADD COLUMN metadata JSONB")
        c.execute("UPDATE vault_assets SET location='Example City, Example Country C',metadata_provenance=%s,metadata=%s WHERE id=%s",
                  (Jsonb({'location':'embedded'}),Jsonb({'gps_latitude':1,'gps_longitude':2}),asset))
        location=trusted_location(c,asset,owner)
        assert location=={'name':'Example City','source':'embedded'}
        assert trusted_location(c,asset,uuid4()) is None
        assert trusted_location(c,uuid4(),owner) is None
        assert c.execute('SELECT location FROM vault_assets WHERE id=%s',(asset,)).fetchone()['location']=='Example City, Example Country C'
    run=lab.queue_ken(asset,owner,config())
    ctx=run['configuration']['correction_context']
    for text in (ken.grounded_prompt(ctx),ken.title_prompt(ctx)):
        assert 'Example City' in text and 'Example Country C' not in text


def test_preference_api_authentication_and_no_exemplar_routes(monkeypatch):
    from app.ken_api import ken_router
    routes={r.path for r in ken_router.routes}
    assert any(r.endswith('/preferences') for r in routes)
    assert not any('exemplar' in r for r in routes)
    for value in ({'positions':'behind'},{'use_style_exemplars':True},{'activity_first':'true'}):
        with pytest.raises(ValueError):style.validate_preferences(value)


def test_prompt_hash_survives_jsonb_key_reordering():
    import json
    ctx=ken.correction_context('',[],[{'name':'Current Person','person_id':str(uuid4())}],{'name':'Example City','source':'embedded'})
    ctx['owner_style']={'version':style.VERSION,'preference_revision':1,'preferences':dict(style.DEFAULTS)}
    assert ken.grounded_prompt(ctx)==ken.grounded_prompt(json.loads(json.dumps(ctx,sort_keys=True)))


def test_preference_api_is_self_scoped(ken_api_fixture,pg_ken):
    from app.main import app
    from app.ken_service import get_ken_store
    client,_,_,asset,_=ken_api_fixture
    lab,_,other_owner=pg_ken
    app.dependency_overrides[get_ken_store]=lambda:lab
    url='/api/ken/preferences'
    # Resolve prefix from the actual router; no authenticated browser involved.
    from app.ken_api import ken_router
    url=next(r.path for r in ken_router.routes if r.path.endswith('/preferences'))
    assert client.get(url).status_code==401
    authenticate(client)
    with lab.connect() as c:store.save_preferences(c,other_owner,style.DEFAULTS)
    assert client.get(url).json()['preferences']=={}
    response=client.put(url,json={'preferences':{'activity_first':True}})
    assert response.status_code==200
    assert client.get(url).json()['preferences']=={'activity_first':True}
    assert client.put(url,json={'preferences':{'participant_count':7}}).status_code==422
    assert client.put(url,json={'preferences':{},'owner_user_id':str(other_owner)}).status_code==422
    with lab.connect() as c:assert store.preferences(c,other_owner)['preferences']==style.DEFAULTS
