"""KEN-016: normal and worker paths must share the snapshotted prompts."""
import hashlib
import inspect
import runpy
from pathlib import Path
from app import ken_service as lab, ken_api as api, ken_config as ken
from tests.test_ken_service import ken_api_fixture, FakeAdapter
from tests.test_vault_libraries import authenticate

EXPECTED = '26382bf9f68915c3aed8c81b1aafee1a1a55db5167b335d178f08af16a932c01'


def test_normal_and_worker_queue_identical_final_prompt(ken_api_fixture, monkeypatch):
    client, store, vault, asset, _ = ken_api_fixture
    for module in (lab, api):
        monkeypatch.setattr(module, 'ADAPTER', FakeAdapter())
    store.queue_ken = lambda a,o,c,**kw: store.queue(a,o,c)
    authenticate(client)
    normal = client.post(f'/api/personal-videos/ken/assets/{asset.id}/runs')
    assert normal.status_code == 202
    a = normal.json()['configuration']
    for key in ('prompt','prompt_version','prompt_sha256','parameters','temporal_policy','model_revision'):
        assert a[key] == lab.configuration()[key]
    assert a['prompt_version'] == 'ken-language-context-v4'
    assert a['prompt_sha256'] == hashlib.sha256(a['prompt'].encode()).hexdigest() == EXPECTED
    for rule in ('MAIN ACTIVITY','Name the visible activity directly when identifiable',
                 'Prefer the specific action over vague categories', "'genital area'", "'sexual activity'",
                 'authoritative context','Use trusted names consistently throughout',
                 'Do not mention that the camera is static','Do not list absent',
                 'Prefer ending the description rather than adding low-value observations'):
        assert rule in a['prompt']
