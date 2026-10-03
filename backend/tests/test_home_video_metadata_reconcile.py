"""Full metadata chain with synthetic values; no real candidate text."""
import json
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from psycopg.types.json import Jsonb
from app import video_location as location
from app.home_video_metadata_reconcile import backfill_locations,backfill_titles,stored_location
from app.vault_libraries import _to_summary,VaultLibraryFile
from app import vault_master
from tests.test_postgres_ken import pg_ken
from tests.test_ken_titles import titled_ken,TitleAdapter
from tests.test_ken_config import complete,config


@pytest.mark.parametrize('gps',['+20.2110-087.4650+004.000/','+20.211-87.465/'])
def test_quicktime_gps_uses_existing_place_lookup_and_preserves_granularity(monkeypatch,gps):
    calls=[]
    monkeypatch.setattr(location.reverse_geocode,'search',lambda points:(calls.append(points) or [{'country':'Mexico'}]))
    probe={'format':{'tags':{'com.apple.quicktime.location.ISO6709':gps}}}
    values=location.gps_location(probe)
    assert values['location']=='Mexico' and calls==[[(20.211,-87.465)]]
    monkeypatch.setattr(vault_master.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(probe).encode()))
    assert vault_master._extract_video_metadata(Path('synthetic.mov'))['location']=='Mexico'


@pytest.mark.parametrize('value',['','Athens','+99.100-087.000/','+20.0-187.0/','not coordinates'])
def test_no_location_guess_without_valid_coordinates(value,monkeypatch):
    monkeypatch.setattr(location.reverse_geocode,'search',lambda _:pytest.fail('Must not geocode absent/invalid GPS'))
    assert location.gps_location({'format':{'tags':{'location':value}}})=={}


def test_location_conflicts_fail_closed(monkeypatch):
    assert location.gps_location({'format':{'tags':{'location':'+20.0-087.0/','location-eng':'+21.0-087.0/'}}})=={}
    row={'has_location_override':True,'location_override':None,'imported_location':'Athens'}
    assert stored_location(row)==(None,None)


@pytest.fixture
def metadata_lab(titled_ken,tmp_path):
    store,a,o=titled_ken
    path=tmp_path/'synthetic.mov';path.write_bytes(b'synthetic source')
    with store.connect() as c:
        c.execute("ALTER TABLE vault_assets ADD COLUMN asset_type TEXT DEFAULT 'Home Videos', ADD COLUMN detected_metadata JSONB DEFAULT '{}', ADD COLUMN imported_metadata JSONB DEFAULT '{}'")
        c.execute('CREATE TABLE vault_files(id UUID,asset_id UUID,vault_path TEXT,sha256 TEXT,file_role TEXT,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)')
        c.execute("INSERT INTO vault_files(id,asset_id,vault_path,sha256,file_role) VALUES(%s,%s,%s,%s,'primary')",(uuid4(),a,str(path),'a'*64))
    return store,a,o,path


def test_historical_suggestion_promotes_to_summary_and_backfill_is_idempotent(metadata_lab):
    store,a,o,path=metadata_lab;adapter=TitleAdapter()
    run=complete(store,store.queue(a,o,config()))
    store.generate_title(a,o,run['id'],adapter)
    assert backfill_titles(store)=={'version':'home-video-title-backfill-v1','eligible':1,'scheduled':0}
    assert backfill_titles(store,apply=True)['scheduled']==1
    assert backfill_titles(store,apply=True)['scheduled']==0
    store.process_pending_title(adapter)
    assert len(adapter.requests)==1  # Existing suggestion promoted, no new inference.
    with store.connect() as c:
        row=c.execute('SELECT * FROM vault_assets WHERE id=%s',(a,)).fetchone()
    summary=_to_summary(VaultLibraryFile('synthetic-id',path.name,Path(path.name),path,path.stat().st_size,datetime.now(timezone.utc),'video'),'personal-videos',SimpleNamespace(**row,captured_on=None))
    assert summary.display_title==row['metadata']['ken_accepted_title']=='The Synthetic Pool Challenge'
    assert summary.location is None
    assert backfill_titles(store,apply=True)['scheduled']==0
    print(json.dumps({'asset_id':str(a),'manual_title':None,'ken_generated_title':row['metadata']['ken_accepted_title'],'resolved_card_title':row['display_title'],'canonical_location':row['location'],'api_title':summary.display_title,'api_location':summary.location}))


def test_location_backfill_existing_metadata_and_original_gps(metadata_lab,monkeypatch):
    store,a,o,path=metadata_lab
    probe={'format':{'tags':{'location':'+20.211-087.465/'}}}
    monkeypatch.setattr(location.reverse_geocode,'search',lambda _:[{'city':'Tulum','country':'Mexico'}])
    monkeypatch.setattr('app.home_video_metadata_reconcile.subprocess.run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(probe).encode()))
    assert backfill_locations(store,root=path.parent)['eligible']==1
    assert backfill_locations(store,apply=True,root=path.parent)['applied']==1
    with store.connect() as c:
        row=c.execute('SELECT * FROM vault_assets WHERE id=%s',(a,)).fetchone()
        assert row['location']=='Tulum, Mexico' and row['effective_metadata']['location']==row['location']
        assert row['metadata_provenance']['location']=='embedded'
        assert row['detected_metadata']['gps_latitude']==20.211
    assert backfill_locations(store,apply=True,root=path.parent)['applied']==0
    assert path.read_bytes()==b'synthetic source'
    with store.connect() as c:
        c.execute("UPDATE vault_assets SET location=NULL, imported_metadata=%s WHERE id=%s",(Jsonb({'location':'Mexico'}),a))
    monkeypatch.setattr('app.home_video_metadata_reconcile.subprocess.run',lambda *a,**k:pytest.fail('Stored place needs no source probe'))
    assert backfill_locations(store,apply=True,root=path.parent)['applied']==1
    with store.connect() as c:assert c.execute('SELECT location FROM vault_assets WHERE id=%s',(a,)).fetchone()['location']=='Mexico'


def test_backfill_preserves_manual_titles_locations_and_wrong_owner_runs(metadata_lab):
    store,a,o,path=metadata_lab
    run=complete(store,store.queue(a,o,config()))
    store.set_video_title(a,o,'user',manual_title='Manual synthetic title')
    with store.connect() as c:c.execute("UPDATE vault_assets SET user_overrides=user_overrides || %s WHERE id=%s",(Jsonb({'location':None}),a))
    assert backfill_titles(store,apply=True)['scheduled']==0
    assert backfill_locations(store,apply=True,root=path.parent)['applied']==0
    with store.connect() as c:
        c.execute("UPDATE vault_assets SET user_overrides='{}',metadata_provenance='{}',owner_user_id=%s WHERE id=%s",(uuid4(),a))
    assert backfill_titles(store,apply=True)['scheduled']==0
