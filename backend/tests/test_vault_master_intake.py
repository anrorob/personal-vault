import hashlib
from pathlib import Path
from collections import namedtuple

from fastapi.testclient import TestClient

from app.incoming import get_incoming_path
from app.main import app
from app.auth import get_authentication_store
from app.vault_master_intake import MemoryIntakeStore, get_intake_store, get_operational_health
import app.vault_master_intake as intake_module
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def login(client: TestClient):
    assert client.post("/api/auth/login",json={"username":TEST_USERNAME,"password":TEST_PASSWORD}).status_code==200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())


def setup(client: TestClient,tmp_path: Path):
    store=MemoryIntakeStore(); app.dependency_overrides[get_intake_store]=lambda:store
    app.dependency_overrides[get_operational_health]=lambda:{"database":{"status":"ok"},"arrival_hall":{"status":"ok","writable":True,"free_bytes":1000000},"florence":{"status":"ok","model":"test","device":"CPU"},"backup":{"status":"host-managed"}}
    app.dependency_overrides[get_incoming_path]=lambda:tmp_path
    login(client)
    created=client.post("/api/vault-master/control/sources",json={"name":"Test Supplier"})
    assert created.status_code==200
    body=created.json(); source_id=body["source"]["id"]; token=body["token"]
    assert body["source"]["status"]=="disabled"
    assert client.patch(f"/api/vault-master/control/sources/{source_id}",json={"status":"enabled"}).status_code==200
    assert client.post("/api/vault-master/control/intake/enable").status_code==200
    return store,source_id,token


def submit(client,source_id,token,key,filename,content,sha=None):
    return client.post("/api/vault-master/intake",content=content,headers={
        "Authorization":f"Bearer {token}","X-PV-Source-ID":source_id,
        "X-PV-Idempotency-Key":key,"X-PV-Filename":filename,
        "X-PV-SHA256":sha or hashlib.sha256(content).hexdigest(),
        "Content-Type":"application/octet-stream"})


def test_source_starts_disabled_and_control_is_private(client: TestClient,tmp_path: Path):
    store,source_id,_=setup(client,tmp_path)
    assert store.sources[next(iter(store.sources))].status=="enabled"
    assert client.get("/api/vault-master/control").headers["cache-control"]=="private, no-store"
    assert client.post("/api/auth/logout").status_code==200
    assert client.get("/api/vault-master/control").status_code==401


def test_intake_verifies_checksum_and_idempotently_publishes_once(client: TestClient,tmp_path: Path):
    _,source_id,token=setup(client,tmp_path); key="supplier-item-0001"; content=b"private file"
    first=submit(client,source_id,token,key,"photo.jpg",content)
    second=submit(client,source_id,token,key,"photo.jpg",content)
    assert first.status_code==200 and first.json()["idempotent_replay"] is False
    assert second.status_code==200 and second.json()["idempotent_replay"] is True
    assert [p.name for p in tmp_path.iterdir()]==["photo.jpg"]


def test_conflicting_key_and_bad_checksum_leave_no_partial(client: TestClient,tmp_path: Path):
    _,source_id,token=setup(client,tmp_path); key="supplier-item-0002"
    bad=submit(client,source_id,token,key,"bad.jpg",b"wrong",sha="0"*64)
    assert bad.status_code==422 and not list(tmp_path.iterdir())
    conflict=submit(client,source_id,token,key,"different.jpg",b"other")
    assert conflict.status_code==409 and not list(tmp_path.iterdir())


def test_global_and_source_pause_apply_backpressure(client: TestClient,tmp_path: Path):
    _,source_id,token=setup(client,tmp_path)
    assert client.post("/api/vault-master/control/intake/pause").status_code==200
    blocked=submit(client,source_id,token,"supplier-item-0003","photo.jpg",b"x")
    assert blocked.status_code==503 and blocked.headers["retry-after"]=="60"


def test_rate_daily_byte_and_backlog_limits_are_enforced(client: TestClient,tmp_path: Path):
    store,source_id,token=setup(client,tmp_path)
    source=store.sources[next(iter(store.sources))]
    store.sources[source.id]=source.__class__(**{**source.__dict__,"rate_per_minute":1})
    assert submit(client,source_id,token,"supplier-limit-0001","one.jpg",b"1").status_code==200
    limited=submit(client,source_id,token,"supplier-limit-0002","two.jpg",b"2")
    assert limited.status_code==429 and limited.headers["retry-after"]=="60"

    store.receipts.clear()
    source=store.sources[source.id]
    store.sources[source.id]=source.__class__(**{**source.__dict__,"rate_per_minute":60,"bytes_per_day":1})
    assert submit(client,source_id,token,"supplier-limit-0003","three.jpg",b"12").status_code==429

    source=store.sources[source.id]
    store.sources[source.id]=source.__class__(**{**source.__dict__,"bytes_per_day":100,"max_pending":1})
    store.pending=1
    blocked=submit(client,source_id,token,"supplier-limit-0004","four.jpg",b"4")
    assert blocked.status_code==503 and blocked.headers["retry-after"]=="300"


def test_duplicate_storm_pauses_source_without_extra_files(client: TestClient,tmp_path: Path):
    store,source_id,token=setup(client,tmp_path)
    content=b"same payload"
    assert submit(client,source_id,token,"duplicate-request","original.jpg",content).status_code==200
    for number in range(10):
        response=submit(client,source_id,token,f"supplier-duplicate-{number+1:04d}",f"copy-{number}.jpg",content)
        assert response.status_code==409
    assert store.sources[next(iter(store.sources))].status=="paused"
    assert [path.name for path in tmp_path.iterdir()]==["original.jpg"]


def test_repeated_malformed_payloads_pause_source_and_leave_no_partials(client: TestClient,tmp_path: Path):
    store,source_id,token=setup(client,tmp_path)
    for number in range(5):
        response=submit(client,source_id,token,f"supplier-malformed-{number:04d}",f"bad-{number}.jpg",b"bad",sha="0"*64)
        assert response.status_code==422
    assert store.sources[next(iter(store.sources))].status=="paused"
    assert not list(tmp_path.iterdir())


def test_full_disk_gate_fails_closed_before_writing(client: TestClient,tmp_path: Path,monkeypatch):
    store,source_id,token=setup(client,tmp_path)
    source=store.sources[next(iter(store.sources))]
    store.sources[source.id]=source.__class__(**{**source.__dict__,"min_free_bytes":10})
    Usage=namedtuple("Usage","total used free")
    monkeypatch.setattr(intake_module.shutil,"disk_usage",lambda _:Usage(100,99,1))
    blocked=submit(client,source_id,token,"capacity-request","photo.jpg",b"x")
    assert blocked.status_code==507
    assert not list(tmp_path.iterdir())
    assert next(iter(store.receipts.values())).rejection_reason=="free_space_reserve"


def test_operational_health_exposes_mount_and_fails_closed_for_florence(tmp_path: Path,monkeypatch):
    monkeypatch.setattr(intake_module,"urlopen",lambda *_args,**_kwargs:(_ for _ in ()).throw(OSError("offline")))
    health=get_operational_health(tmp_path)
    assert health["database"]["status"]=="ok"
    assert health["arrival_hall"]["status"]=="ok"
    assert health["arrival_hall"]["writable"] is True
    assert health["florence"]["status"]=="unavailable"
    assert health["backup"]["status"]=="host-managed"


def test_operational_health_reports_missing_arrival_hall(tmp_path: Path,monkeypatch):
    missing=tmp_path/"missing"
    monkeypatch.setattr(intake_module,"urlopen",lambda *_args,**_kwargs:(_ for _ in ()).throw(OSError("offline")))
    health=get_operational_health(missing)
    assert health["arrival_hall"]=={"status":"unavailable","writable":False,"free_bytes":None}
