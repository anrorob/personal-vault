"""Manual worker failures expose only safe technical diagnostics."""
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app import ken_service as lab, ken_config as ken
from tests.test_ken_service import MemoryKen

@pytest.mark.parametrize('message,code',[
    (ken.DURATION_LIMIT_ERROR,'ken_duration_limit'),
    ('Synthetic private text /internal/source.mp4','input_preparation_failed'),
])
def test_preparation_failure_has_safe_stage_diagnostic(monkeypatch,message,code,caplog):
    monkeypatch.delenv('PV_KEN_WORK_ROOT',raising=False)
    store=MemoryKen();a,o=uuid4(),uuid4()
    run=store.queue(a,o,lab.configuration())
    vault=SimpleNamespace(get_catalogued_asset_by_id=lambda _:SimpleNamespace(id=a,owner_user_id=o))
    def prepare(*args):raise ValueError(message)
    monkeypatch.setattr(lab,'prepare_input',prepare)
    monkeypatch.setattr(lab, 'ADAPTER', SimpleNamespace(analyse=lambda _:pytest.fail('No inference after preparation failure')))
    assert lab.process_next(store,vault)==run['id']
    saved=store.get(run['id']);diagnostic=saved['configuration']['failure_diagnostic']
    assert diagnostic['stage']=='input_preparation' and diagnostic['exception_type']=='ValueError'
    assert diagnostic['function']=='prepare' and isinstance(diagnostic['line'],int)
    assert diagnostic.get('code')==code
    assert diagnostic['retryable'] is False
    assert 'automatic_title' not in saved['configuration']
    if code=='ken_duration_limit':assert saved['error']==message
    else:
        assert message not in str(saved) and message not in caplog.text
    assert 'input_preparation' in caplog.text


def test_reported_duration_now_has_full_coverage():
    policy=ken.policy(1091220)
    assert policy['fps']==1 and len(policy['chunks'])==28
    assert policy['chunks'][-1]['end_ms']==1091220
    with pytest.raises(ValueError,match='up to thirty minutes'):
        ken.policy(ken.MAX_DURATION_MS+1)

from psycopg.types.json import Jsonb
from tests.test_postgres_ken import pg_ken
from tests.test_ken_titles import titled_ken
from tests.test_ken_config import complete,config

@pytest.mark.parametrize('title_kind',['absent','generated','manual'])
@pytest.mark.parametrize('location',[None,'Synthetic City, Country'])
@pytest.mark.parametrize('tags',[[],['Synthetic custom tag']])
def test_manual_queue_and_reanalysis_independent_of_optional_metadata(titled_ken,title_kind,location,tags):
    store,a,o=titled_ken
    title=None if title_kind=='absent' else 'Synthetic title'
    overrides={'display_title':title} if title_kind=='manual' else {}
    provenance={'location':'embedded'} if location else {}
    if title:provenance['display_title']='user_override' if title_kind=='manual' else 'ken_generated'
    with store.connect() as c:
        c.execute('UPDATE vault_assets SET display_title=%s,user_overrides=%s,location=%s,metadata_provenance=%s,canonical=%s WHERE id=%s',
            (title,Jsonb(overrides),location,Jsonb(provenance),Jsonb({'tags':tags}),a))
    first=store.queue(a,o,config())
    assert first['status']=='queued' and 'automatic_title' not in first['configuration']
    context=first['configuration']['correction_context']
    assert (context['trusted_location']['name'] if context['trusted_location'] else None)==location
    complete(store,first)
    second=store.queue(a,o,config())
    assert second['status']=='queued' and second['id']!=first['id']
    with store.connect() as c:
        row=c.execute('SELECT display_title,user_overrides,canonical FROM vault_assets WHERE id=%s',(a,)).fetchone()
    assert row=={'display_title':title,'user_overrides':overrides,'canonical':{'tags':tags}}
