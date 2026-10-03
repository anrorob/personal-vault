from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from PIL import Image
import pytest

from app import ken_service as lab
from app.main import app
from app.vault_master import MemoryVaultMasterStore, get_vault_master_store
from tests.test_vault_libraries import authenticate, catalogue_file


class MemoryKen:
    def __init__(self):
        self.runs = {}

    def queue(self, asset_id, owner_id, config):
        for run in self.runs.values():
            if run["asset_id"] == asset_id and run["status"] in ("queued", *lab.ACTIVE):
                return run
        run = {"id": uuid4(), "asset_id": asset_id, "owner_user_id": owner_id,
               "input_mode": config["input_mode"], "configuration": deepcopy(config), "status": "queued", "result": None, "error": None}
        self.runs[run["id"]] = run
        return run

    def queue_ken(self, asset_id, owner_id, config, **kwargs):
        return self.queue(asset_id, owner_id, config)

    def corrections(self, *args):
        return []

    def get(self, run_id):
        return self.runs.get(run_id)

    def history(self, asset_id, owner_id):
        return [r for r in self.runs.values() if r["asset_id"] == asset_id and r["owner_user_id"] == owner_id]

    @contextmanager
    def worker_lock(self):
        yield True

    def recover(self):
        for run in self.runs.values():
            if run["status"] in lab.ACTIVE:
                run.update(status="failed", error="Interrupted by worker restart")

    def claim(self):
        run = next((r for r in self.runs.values() if r["status"] == "queued"), None)
        if run:
            run["status"] = "preparing_input"
        return run

    def update(self, run_id, status, **kwargs):
        run = self.runs[run_id]
        run["status"] = status
        for key, value in kwargs.items():
            run["configuration" if key == "config" else key] = deepcopy(value)


class FakeAdapter:
    def health(self):
        return {"status": "available"}

    def analyse(self, payload):
        return {**result_binding(payload), "description": "Two people move across the scene.", "raw_response": " exact output ", "warnings": []}


def result_binding(payload):
    from app.ken_attestation import ATTESTATION_VERSION, decoded_manifest, digest_json
    policy=payload['native_parameters']
    frames=decoded_manifest([b'synthetic RGB']*3,[0,1000,2000],[(1,1)]*3)
    proof={'version':ATTESTATION_VERSION,'status':'verified',
           **{k:payload[k] for k in ('asset_id','run_id','input_fingerprint','request_nonce')},
           'resolved_input_sha256':payload['expected_input_sha256'],'resolved_input_size':payload['expected_input_size'],
           'runtime_opened_sha256':payload['expected_input_sha256'],'runtime_opened_size':payload['expected_input_size'],
           'decoded_manifest':frames,'decoded_input_fingerprint':digest_json(frames),
           'correction_fingerprint':payload['correction_fingerprint']}
    chunk=policy['chunks'][0]
    fp=digest_json({'parent_input_fingerprint':payload['input_fingerprint'],'chunk':chunk,'prepared_sha256':payload['expected_input_sha256']})
    proof['chunks']=[{**chunk,'prepared_sha256':payload['expected_input_sha256'],
                      'prepared_size':payload['expected_input_size'],'input_fingerprint':fp,
                      'input_attestation':{**proof,'input_fingerprint':fp}}]
    return {**{k:payload[k] for k in ('asset_id','run_id','input_fingerprint','correction_fingerprint')},'input_attestation':proof}


@pytest.fixture
def ken_api_fixture(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PV_ENVIRONMENT", "development")
    monkeypatch.setenv("PV_REPOSITORY", "example-owner/personal-vault")
    monkeypatch.setenv("PV_ALLOWED_SOURCE_REPOSITORIES", "example-owner/personal-vault")
    monkeypatch.setenv("PV_KEN_ENABLED", "true")
    monkeypatch.setenv("PV_HOME_VIDEOS_PATH", str(tmp_path))
    store = MemoryKen()
    vault = MemoryVaultMasterStore()
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"original video")
    asset = catalogue_file(vault, tmp_path, source, asset_type="Home Videos", vault_root="/vault/Home Videos", mime_type="video/mp4")
    asset = replace(asset, sha256=lab.file_sha256(source))
    vault.catalogued_assets[asset.vault_path] = asset
    app.dependency_overrides[lab.get_ken_store] = lambda: store
    app.dependency_overrides[get_vault_master_store] = lambda: vault
    from app import ken_api
    adapter=FakeAdapter()
    monkeypatch.setattr(lab, 'ADAPTER', adapter)
    monkeypatch.setattr(ken_api, 'ADAPTER', adapter)
    return client, store, vault, asset, source


def mock_preparation(monkeypatch):
    # Only inference tests use this fake; real native preparation has separate fixtures.
    from app import ken_config as ken
    from app.ken_integrity import seal_input
    from app.ken_attestation import expect_input,digest_json
    def prepare(asset, config, work):
        source=Path(lab.get_home_videos_path())/Path(asset.vault_path).name
        digest=lab.file_sha256(source)
        config['source']={'asset_id':str(asset.id),'size_bytes':source.stat().st_size,'duration_ms':3000,'verified_sha256':digest}
        config['runtime_video']={**ken.policy(3000),'prepared_stream':{'duration_ms':3000},'prepared_sha256':digest}
        context=ken.correction_context('',[])
        config['correction_context']=context
        config['correction_fingerprint']=digest_json(context)
        payload={'asset_id':str(asset.id),'run_id':config['run_id'],'input_mode':'native_video',
                 'native_parameters':config['runtime_video'],'native_sha256':digest,
                 'correction_context':context,'correction_fingerprint':digest_json(context)}
        seal_input(asset.id,config,payload)
        expect_input(config,payload,digest,source.stat().st_size)
        return payload
    monkeypatch.setattr(lab,'prepare_input',prepare)


def test_engine_queue_authentication_and_retired_routes(ken_api_fixture):
    client,store,vault,asset,_=ken_api_fixture
    url=f'/api/personal-videos/ken/assets/{asset.id}/runs'
    assert client.post(url).status_code==401
    authenticate(client)
    assert client.get('/api/personal-videos/ken/engine').json()['model_revision']==lab.ENGINE.model_revision
    assert client.post(url).status_code==202
    assert client.get(url).json()[0]['input_mode']=='native_video'
    assert client.get('/api/video-lab/candidates').status_code==404
    vault.catalogued_assets[asset.vault_path]=replace(asset,owner_user_id=uuid4())
    assert client.post(url).status_code==404


@pytest.mark.parametrize('environment,flag',[('production','true'),('development','false')])
def test_unavailable_without_environment_source_admission_or_feature_flag(ken_api_fixture,monkeypatch,environment,flag):
    client,store,_,asset,_=ken_api_fixture
    authenticate(client)
    monkeypatch.setenv('PV_ENVIRONMENT',environment);monkeypatch.setenv('PV_KEN_ENABLED',flag)
    if environment == 'production':
        monkeypatch.setenv('PV_ALLOWED_SOURCE_REPOSITORIES', 'example-owner/production-source')
    assert client.post(f'/api/personal-videos/ken/assets/{asset.id}/runs').status_code==404
    assert not store.runs


def test_worker_persists_binding_before_inference_and_does_not_change_media(ken_api_fixture,monkeypatch):
    _,store,vault,asset,source=ken_api_fixture
    mock_preparation(monkeypatch)
    original=deepcopy(vault.__dict__)
    run=store.queue(asset.id,asset.owner_user_id,lab.configuration())
    def analyse(payload):
        saved=store.get(run['id'])
        assert saved['status']=='analysing'
        assert saved['configuration']['input_integrity']['input_fingerprint']==payload['input_fingerprint']
        return {**result_binding(payload),'description':'Synthetic match.'}
    monkeypatch.setattr(lab.ADAPTER,'analyse',analyse)
    lab.process_next(store,vault)
    assert run['status']=='completed'
    assert vault.__dict__==original
    assert source.read_bytes()==b'original video'
